#!/usr/bin/env python3
"""Keep your Claude desktop Code sessions when you switch accounts (macOS).

The Claude desktop app stores each Code session's record per account and organization:
    ~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json
and lists only the folder of the account you are signed in to, so sessions from your other
accounts vanish from the sidebar. The conversations themselves (~/.claude/projects) are
shared by all accounts, so moving the records into the active account's folder brings the
sessions back. Symlinking the folders together does not work: the app refuses to write
through symlinks.

Sessions that run in a scratch workspace (started without a folder) also keep a copy of
their record in their own account: the app deletes the scratch workspaces of the signed-in
account that no loaded session uses once they are 7 days old, and the copy keeps them in use.
"""

import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HOME = Path.home()
APP_DIR = Path(os.environ.get("CLAUDE_SESSIONS_APP_DIR", HOME / "Library/Application Support/Claude"))
LOG_DIR = Path(os.environ.get("CLAUDE_SESSIONS_LOG_DIR", HOME / "Library/Logs/Claude"))
STATE_DIR = Path(os.environ.get("CLAUDE_SESSIONS_HOME", HOME / ".claude-sessions"))
SESSIONS_DIR = APP_DIR / "claude-code-sessions"
BACKUP_DIR = STATE_DIR / "backups"
REPLACED_DIR = STATE_DIR / "replaced"
RUN_LOG = STATE_DIR / "last-run.log"
WATCH_LOG = STATE_DIR / "watch.log"
WATCH_STATE = STATE_DIR / "watch-state.json"
LOCK_FILE = STATE_DIR / "lock"

AGENT_LABEL = "local.claude-sessions"
AGENT_PLIST = HOME / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"

BUNDLE_ID = "com.anthropic.claudefordesktop"
PROCESS_NAME = "Claude"
POLITE_QUIT_TIMEOUT = 20
QUIT_TIMEOUT = 90
KEEP_BACKUPS = 20
# An account switch first logs a transient pair (old account, new org), then the real one.
SETTLE_SECONDS = 5
POLL_SECONDS = 2

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Claude writes the session folder it loads to main.log every time an account is loaded.
LOAD_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) .*?"
    r"(?:Loaded \d+ persisted sessions from|Session storage directory does not exist yet, skipping load:) "
    r".*/claude-code-sessions/(" + UUID + r")/(" + UUID + r")(?![0-9a-f-])",
    re.I,
)
SCRATCH_CWD = re.compile(
    r"/scratch-workspaces/(" + UUID + r")/(" + UUID + r")/scratch-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}(?:/|$)",
    re.I,
)
ACTIVITY_KEYS = ("lastActivityAt", "lastTurnEdgeAt", "latestUserFrameAt", "createdAt")
PID_MARK = "__claude_sessions_pid__"
DONE_MARK = "__claude_sessions_done__"

Write = namedtuple("Write", "copy dest kind new")  # kind: "target" | "anchor"


class Abort(Exception):
    pass


class Busy(Abort):
    pass


class Copy:
    """A session record as stored in one account's folder."""

    def __init__(self, path):
        self.path = path
        self.dir = path.parent
        self.data = path.read_bytes()
        self.record = json.loads(self.data.decode("utf-8"))
        if not isinstance(self.record, dict):
            raise ValueError("record is not a JSON object")
        self.mtime = path.stat().st_mtime

    @property
    def title(self):
        return self.record.get("title") or "(untitled)"

    def freshness(self):
        def num(key):
            value = self.record.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

        return (max(num(key) for key in ACTIVITY_KEYS), num("lastFocusedAt"), self.mtime)

    def scratch_home(self):
        """The session folder that owns this session's scratch workspace, if it runs in one."""
        match = SCRATCH_CWD.search(self.record.get("cwd") or "")
        return SESSIONS_DIR / match.group(1).lower() / match.group(2).lower() if match else None


class Plan:
    def __init__(self, target):
        self.target = target
        self.writes = []
        self.removes = []  # (Copy, "moved" | "deleted")
        self.waiting = []  # (source, destination) of the app's waiting-input records
        self.backlog = None  # (destination, merged content, source files, number of tasks added)
        self.notes = []
        self.already = 0

    def moved_in(self):
        return [w for w in self.writes if w.kind == "target" and w.new]

    def refreshed(self):
        return [w for w in self.writes if w.kind == "target" and not w.new]

    def deleted(self):
        return [copy for copy, why in self.removes if why == "deleted"]

    def keeps_scratch_copy(self, write):
        return write.copy.scratch_home() not in (None, self.target)

    def empty(self):
        return not (self.writes or self.removes or self.waiting or self.backlog)

    def changes_visible(self):
        """Whether the active account's folder changes (Claude must restart to show it)."""
        return bool(
            any(w.kind == "target" for w in self.writes)
            or self.waiting
            or self.backlog
            or any(copy.dir == self.target for copy in self.deleted())
        )


def session_dirs():
    if not SESSIONS_DIR.is_dir():
        return []
    dirs = []
    for account in sorted(SESSIONS_DIR.iterdir()):
        if account.is_dir() and not account.is_symlink():
            dirs += [org for org in sorted(account.iterdir()) if org.is_dir() and not org.is_symlink()]
    return dirs


def build_plan(target):
    plan = Plan(target)
    dirs = session_dirs()
    copies, tombstones = {}, {}
    for d in dirs:
        for entry in sorted(d.iterdir()):
            name = entry.name
            if name.startswith("deleted_"):
                tombstones.setdefault(name[len("deleted_"):], set()).add(d)
            elif name.startswith("local_") and name.endswith(".json") and entry.is_file() and not entry.is_symlink():
                try:
                    copies.setdefault(name, []).append(Copy(entry))
                except (OSError, ValueError):
                    plan.notes.append(f"Unreadable record, left alone: {short(entry)}")

    live = set()
    for name, cps in sorted(copies.items()):
        session_id = name[: -len(".json")]
        if tombstones.get(session_id[len("local_"):], set()) - {c.dir for c in cps}:
            # Deleted in another account; don't bring its leftover copies back.
            plan.removes += [(c, "deleted") for c in cps]
            continue
        live.add(session_id)
        by_dir = {c.dir: c for c in cps}
        if target in by_dir:
            plan.already += 1
        winner = max(cps, key=Copy.freshness)
        home = winner.scratch_home()
        if home == target:
            home = None
        for dest_dir, kind in ((target, "target"), (home, "anchor")):
            if dest_dir is None:
                continue
            current = by_dir.get(dest_dir)
            if current is None:
                plan.writes.append(Write(winner, dest_dir / name, kind, True))
            elif current.data != winner.data:
                plan.writes.append(Write(winner, current.path, kind, False))
        plan.removes += [(c, "moved") for c in cps if c.dir not in (target, home)]

    for d in dirs:
        if d == target or not (d / "waiting-input").is_dir():
            continue
        for entry in sorted((d / "waiting-input").iterdir()):
            session_id = entry.name[: -len(".json")] if entry.name.endswith(".json") else entry.name
            dest = target / "waiting-input" / entry.name
            if session_id in live and not dest.exists():
                plan.waiting.append((entry, dest))

    plan_backlog(plan, dirs)

    for d in dirs:
        if d == target:
            continue
        try:
            tasks = json.loads((d / "scheduled-tasks.json").read_text(encoding="utf-8")).get("scheduledTasks")
        except (OSError, ValueError, AttributeError):
            continue
        if tasks:
            plan.notes.append(f"{label(d)}: {len(tasks)} scheduled tasks stay in that account (they are not moved).")
    return plan


def read_backlog(path):
    """The task list: None if there is none, False if it can't be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return False
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else False


def plan_backlog(plan, dirs):
    dest = plan.target / "backlog" / "tasks.json"
    merged = read_backlog(dest)
    if merged is False:
        plan.notes.append(f"This account's task list is unreadable; tasks were not moved: {short(dest)}")
        return
    sources, added = [], 0
    for d in dirs:
        src = d / "backlog" / "tasks.json"
        if d == plan.target or not src.is_file():
            continue
        data = read_backlog(src)
        if not data or (merged and data.get("version") != merged.get("version")):
            plan.notes.append(f"Task list unreadable or from another version, left alone: {short(src)}")
            continue
        if merged is None:
            merged = dict(data, items=[])
        known = {item.get("id") for item in merged["items"] if isinstance(item, dict)}
        new = [item for item in data["items"] if not isinstance(item, dict) or item.get("id") not in known]
        merged["items"].extend(new)
        added += len(new)
        sources.append(src)
    if sources:
        plan.backlog = (dest, merged, sources, added)


def apply_plan(plan):
    for w in plan.writes:
        write_file(w.dest, w.copy.data, w.copy.mtime)
    for src, dest in plan.waiting:
        make_dirs(dest.parent)
        os.rename(src, dest)
    if plan.backlog:
        dest, merged, sources, _ = plan.backlog
        data = json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        write_file(dest, data, time.time())
        for src in sources:
            src.unlink()
    for copy, _ in plan.removes:
        copy.path.unlink(missing_ok=True)


def write_file(dest, data, mtime):
    make_dirs(dest.parent)
    tmp = dest.with_name(f".claude-sessions-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dest)


def make_dirs(path):
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700)


def backup():
    make_dirs(BACKUP_DIR)
    path = BACKUP_DIR / f"claude-code-sessions-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(SESSIONS_DIR, arcname=SESSIONS_DIR.name)
    path.chmod(0o600)
    for old in sorted(BACKUP_DIR.glob("claude-code-sessions-*.tar.gz"))[:-KEEP_BACKUPS]:
        old.unlink()
    return path


def restore_backup(path):
    with tarfile.open(path) as tar:
        members = tar.getmembers()
        for m in members:
            parts = Path(m.name).parts
            if not parts or parts[0] != SESSIONS_DIR.name or ".." in parts or not (m.isfile() or m.isdir()):
                raise Abort(f"The backup contains an unexpected entry; nothing was restored: {m.name}")
        if SESSIONS_DIR.exists():
            aside = REPLACED_DIR / f"claude-code-sessions-{datetime.now():%Y%m%d-%H%M%S}"
            make_dirs(aside.parent)
            shutil.move(str(SESSIONS_DIR), str(aside))
            print(f"Moved the current folder aside: {short(aside)}")
        extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(APP_DIR, members=members, **extra)


def load_dir(match):
    return SESSIONS_DIR / match.group(2).lower() / match.group(3).lower()


def last_load():
    """The session folder Claude loaded most recently and when, or (None, None)."""
    for log in [LOG_DIR / "main.log"] + [LOG_DIR / f"main{i}.log" for i in range(1, 6)]:
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            match = "claude-code-sessions/" in line and LOAD_LINE.match(line)
            if match:
                return load_dir(match), match.group(1)
    return None, None


def find_target(spec):
    if spec:
        return target_from_spec(spec), "chosen with --to"
    target, when = last_load()
    if target is None:
        raise Abort(
            "Could not find the active account: Claude's log has no session-load entry.\n"
            "Open Claude with the new account once and try again, or pass --to ACCOUNT/ORG."
        )
    known = last_known_account()
    if known and known.lower() != target.parent.name:
        raise Abort(
            f"The account Claude loaded last ({target.parent.name[:8]}…) is not the one signed in ({known[:8]}…).\n"
            "Open Claude with the new account, wait for the session list to load, then try again."
        )
    return target, f"Claude log, {when}"


def target_from_spec(spec):
    parts = [p.lower() for p in spec.strip("/").split("/")]
    if len(parts) != 2 or not all(parts):
        raise Abort("--to must be ACCOUNT/ORG (full UUIDs or unique prefixes).")
    if all(re.fullmatch(UUID, p) for p in parts):
        return SESSIONS_DIR / parts[0] / parts[1]
    matches = [d for d in session_dirs() if d.parent.name.startswith(parts[0]) and d.name.startswith(parts[1])]
    if len(matches) != 1:
        raise Abort(f"--to {spec}: {len(matches)} folders match; use full UUIDs.")
    return matches[0]


def last_known_account():
    try:
        value = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8")).get("lastKnownAccountUuid")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def app_running():
    """Whether Claude is running, or None if that can't be told."""
    try:
        result = subprocess.run(["pgrep", "-x", PROCESS_NAME], capture_output=True)
    except OSError:
        return None
    return {0: True, 1: False}.get(result.returncode)


def quit_app():
    """Quit Claude as Cmd+Q does; fall back to SIGTERM if Apple Events are not allowed."""
    proc = subprocess.Popen(
        ["osascript", "-e", f'tell application id "{BUNDLE_ID}" to quit'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    polite_until = time.monotonic() + POLITE_QUIT_TIMEOUT
    try:
        while app_running() is not False:
            rc = proc.poll()
            if rc not in (None, 0):
                if b"-128" in proc.stderr.read():
                    raise Abort("Quitting Claude was cancelled. Nothing was changed.")
                break
            if time.monotonic() > polite_until:
                break
            time.sleep(0.5)
        else:
            return
    finally:
        if proc.poll() is None:
            proc.kill()
    # Electron apps handle SIGTERM as a regular quit.
    subprocess.run(["pkill", "-TERM", "-x", PROCESS_NAME], capture_output=True)
    deadline = time.monotonic() + QUIT_TIMEOUT
    while time.monotonic() < deadline:
        if app_running() is False:
            return
        time.sleep(0.5)
    raise Abort(
        f"Claude did not quit within {QUIT_TIMEOUT} seconds (a confirmation dialog may be open). Nothing was changed."
    )


def open_app():
    subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True)


def notify(text):
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{text}" with title "Claude sessions"'],
        capture_output=True,
    )


@contextmanager
def run_lock():
    make_dirs(STATE_DIR)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("Another claude-sessions run is in progress.") from None
        yield
    finally:
        os.close(fd)


def sync(target, restart):
    """Quit Claude if asked, move the sessions, start Claude again. The caller holds the lock."""
    if restart:
        print("Quitting Claude…")
        quit_app()
    try:
        plan = build_plan(target)  # Claude wrote its final state while quitting
        if plan.empty():
            print("Nothing left to do.")
            return 0
        path = backup()
        print(f"Backup         : {short(path)}")
        try:
            apply_plan(plan)
        except Exception:
            print(f"Stopped halfway. To go back: claude-sessions --restore {short(path)}")
            raise
        summary = done_summary(plan)
        print(summary)
        if restart:
            notify(summary)
        return 0
    finally:
        if restart:
            print("Starting Claude.")
            open_app()


def run_detached(job):
    """Run the job in a process of its own and stream its output here.

    When this runs in Claude's own terminal, that terminal closes as soon as Claude quits;
    the job lives in a separate session so it finishes anyway.
    """
    make_dirs(STATE_DIR)
    sys.stdout.flush()
    sys.stderr.flush()
    with open(RUN_LOG, "w", encoding="utf-8") as log:
        pid = os.fork()
        if pid == 0:
            os.setsid()
            if os.fork() == 0:
                _worker(job, log.fileno())
            os._exit(0)
    os.waitpid(pid, 0)
    return follow_log()


def _worker(job, log_fd):
    rc = 1
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
        print(PID_MARK, os.getpid())
        rc = job()
    except Abort as e:
        print(e)
    except BaseException:
        traceback.print_exc()
    finally:
        print(DONE_MARK, rc, flush=True)
        os._exit(rc)


def follow_log():
    pid, pending = None, ""
    started = quiet_since = time.monotonic()
    try:
        with open(RUN_LOG, encoding="utf-8", errors="replace") as log:
            while True:
                pending += log.readline()
                if pending.endswith("\n"):
                    line, pending = pending, ""
                    quiet_since = time.monotonic()
                    if line.startswith(PID_MARK):
                        pid = int(line.split()[1])
                    elif line.startswith(DONE_MARK):
                        return int(line.split()[1])
                    else:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    continue
                gone = pid is not None and time.monotonic() - quiet_since > 2 and not alive(pid)
                if gone or (pid is None and time.monotonic() - started > 10):
                    print(f"The run ended unexpectedly; see {short(RUN_LOG)}", file=sys.stderr)
                    return 1
                time.sleep(0.2)
    except KeyboardInterrupt:
        print(f"\nStill running in the background; see {short(RUN_LOG)}")
        return 130


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


class LogTail:
    """Follows Claude's main.log from its current end, across log rotations."""

    def __init__(self, path):
        self.path = path
        self.file = None
        self._open(at_end=True)

    def _open(self, at_end):
        self.pending = b""
        try:
            self.file = open(self.path, "rb")
        except OSError:
            self.file = None
            return
        self.inode = os.fstat(self.file.fileno()).st_ino
        if at_end:
            self.file.seek(0, os.SEEK_END)

    def _read(self):
        *lines, self.pending = (self.pending + self.file.read()).split(b"\n")
        return [line.decode("utf-8", errors="replace") for line in lines]

    def lines(self):
        if self.file is None:
            self._open(at_end=False)
            return self._read() if self.file else []
        lines = self._read()
        try:
            st = os.stat(self.path)
        except OSError:
            return lines
        if st.st_ino != self.inode or st.st_size < self.file.tell():
            lines += self._read()
            self.file.close()
            self._open(at_end=False)
            if self.file:
                lines += self._read()
        return lines


def read_watch_state():
    try:
        value = json.loads(WATCH_STATE.read_text(encoding="utf-8")).get("handled")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, str) and re.fullmatch(UUID + "/" + UUID, value):
        return SESSIONS_DIR / value
    return None


def write_watch_state(target):
    data = json.dumps({"handled": f"{target.parent.name}/{target.name}"}).encode("utf-8")
    write_file(WATCH_STATE, data, time.time())


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def handle_switch(target):
    """Move the sessions into a newly loaded account; False if the switch hasn't settled."""
    log(f"Claude loaded {label(target)}")
    known = last_known_account()
    if known and known.lower() != target.parent.name:
        log("The account switch has not settled yet; waiting for the next load.")
        return False
    try:
        with run_lock():
            plan = build_plan(target)
            if plan.empty():
                log("Nothing to move.")
                return True
            restart = app_running() is True and plan.changes_visible()
            if restart:
                notify("Moving your sessions to this account. Claude will restart in a moment.")
                time.sleep(3)
            sync(target, restart)
    except Busy as e:
        log(str(e))
    except Abort as e:
        log(str(e))
        notify(str(e).splitlines()[0])
    return True


def cmd_watch(args):
    sys.stdout.reconfigure(line_buffering=True)
    tail = LogTail(LOG_DIR / "main.log")
    current, _ = last_load()
    handled = read_watch_state()
    if handled is None and current is not None:
        # First start: take the current account as handled and act on the next switch.
        handled = current
        write_watch_state(handled)
    # A switch made while the watcher was not running is handled right away.
    pending = current if current is not None and current != handled else None
    pending_since = time.monotonic() - SETTLE_SECONDS
    log(f"Watching for account switches (current: {label(handled) if handled else 'unknown'})")
    while True:
        try:
            for line in tail.lines():
                match = "claude-code-sessions/" in line and LOAD_LINE.match(line)
                if match:
                    pending, pending_since = load_dir(match), time.monotonic()
            if pending is not None and time.monotonic() - pending_since >= SETTLE_SECONDS:
                if pending != handled and handle_switch(pending):
                    handled = pending
                    write_watch_state(handled)
                pending = None
        except Exception:
            log(traceback.format_exc().rstrip())
        time.sleep(POLL_SECONDS)


def agent_python():
    """Prefer the system Python: its path survives Homebrew upgrades and macOS remembers its permissions."""
    system = "/usr/bin/python3"
    try:
        check = [system, "-c", "import sys; sys.exit(sys.version_info < (3, 9))"]
        if subprocess.run(check, capture_output=True, timeout=30).returncode == 0:
            return system
    except (OSError, subprocess.TimeoutExpired):
        pass
    return sys.executable


def cmd_enable_auto(args):
    make_dirs(STATE_DIR)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [agent_python(), str(Path(__file__).resolve()), "--watch"],
        "EnvironmentVariables": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(WATCH_LOG),
        "StandardErrorPath": str(WATCH_LOG),
    }
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], capture_output=True)
    for _ in range(5):
        result = subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], capture_output=True, text=True)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise Abort(f"launchctl could not start the watcher: {result.stderr.strip()}")
    print("Automatic mode is on: after every account switch your sessions follow you and Claude restarts.")
    print(f"The watcher starts at login. Log: {short(WATCH_LOG)}")
    return 0


def cmd_disable_auto(args):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PLIST.unlink(missing_ok=True)
    WATCH_STATE.unlink(missing_ok=True)
    print("Automatic mode is off.")
    return 0


def label(d):
    return f"{d.parent.name[:8]}…/{d.name[:8]}…"


def short(path):
    text = str(path)
    return "~" + text[len(str(HOME)):] if text.startswith(str(HOME) + os.sep) else text


def print_plan(plan, where, verbose):
    print(f"Active account : {label(plan.target)}  ({where})")
    print(f"Already here   : {plan.already} sessions")
    moved = plan.moved_in()
    if moved:
        print(f"To move        : {len(moved)} sessions")
        by_source = {}
        for w in moved:
            by_source.setdefault(w.copy.dir, []).append(w)
        for src, writes in sorted(by_source.items()):
            print(f"  {len(writes):4d}  ← {label(src)}")
            if verbose:
                for w in sorted(writes, key=lambda w: w.copy.freshness(), reverse=True):
                    flag = "   [scratch: record also stays in its own account]" if plan.keeps_scratch_copy(w) else ""
                    print(f"          • {w.copy.title}{flag}")
        scratch = sum(plan.keeps_scratch_copy(w) for w in moved)
        if scratch:
            print(
                f"Scratch        : {scratch} sessions also keep their record in their own account "
                "(so Claude keeps their scratch folders)"
            )
    if plan.refreshed():
        print(f"To update      : {len(plan.refreshed())} sessions, from a newer copy in another account")
    if plan.backlog and plan.backlog[3]:
        print(f"Tasks          : {plan.backlog[3]} tasks will be added to this account's list")
    if plan.deleted():
        print(f"Deleted        : {len(plan.deleted())} old copies of deleted sessions will be removed")
    for note in plan.notes:
        print(f"Note: {note}")


def done_summary(plan):
    parts = []
    if plan.moved_in():
        parts.append(f"{len(plan.moved_in())} sessions moved to this account")
    if plan.refreshed():
        parts.append(f"{len(plan.refreshed())} sessions updated")
    if plan.backlog and plan.backlog[3]:
        parts.append(f"{plan.backlog[3]} tasks moved")
    if plan.deleted():
        parts.append(f"removed {len(plan.deleted())} old copies of deleted sessions")
    return (", ".join(parts) or "Scratch copies updated") + "."


def cmd_sync(args):
    target, where = find_target(args.to)
    plan = build_plan(target)
    print_plan(plan, where, verbose=args.dry_run)
    if plan.empty():
        print("Nothing to do: every session is already in the active account.")
        return 0
    if args.dry_run:
        print("Dry run: nothing was changed.")
        return 0
    running = app_running()
    restart = running is True and plan.changes_visible() and not args.no_restart
    if not restart and running is not False and plan.changes_visible():
        print("Claude may be open; quit and reopen it to see the moved sessions.")

    def job():
        with run_lock():
            return sync(target, restart)

    return run_detached(job)


def cmd_restore(args):
    if args.restore:
        path = Path(args.restore).expanduser()
    else:
        backups = sorted(BACKUP_DIR.glob("claude-code-sessions-*.tar.gz"))
        if not backups:
            raise Abort(f"No backups in {short(BACKUP_DIR)}")
        path = backups[-1]
    if not path.is_file():
        raise Abort(f"Backup not found: {path}")
    print(f"Restoring backup: {short(path)}")
    if args.dry_run:
        print("Dry run: nothing was changed.")
        return 0
    running = app_running()
    if running and args.no_restart:
        raise Abort("Not restoring while Claude is open; quit Claude and try again.")
    restart = running is True

    def job():
        with run_lock():
            if restart:
                print("Quitting Claude…")
                quit_app()
            try:
                restore_backup(path)
                print("Backup restored.")
                return 0
            finally:
                if restart:
                    print("Starting Claude.")
                    open_app()

    return run_detached(job)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="claude-sessions",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  claude-sessions                after switching accounts: quits Claude, moves your\n"
            "                                 sessions into the active account, starts Claude again\n"
            "  claude-sessions -n             show what would happen\n"
            "  claude-sessions --enable-auto  do it automatically on every account switch\n"
            "  claude-sessions --restore      restore the latest backup\n\n"
            f"A backup is written to {short(BACKUP_DIR)} before every change (the last {KEEP_BACKUPS} are kept)."
        ),
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the plan without changing anything")
    parser.add_argument("--no-restart", action="store_true", help="don't quit and reopen Claude")
    parser.add_argument("--to", metavar="ACCOUNT/ORG", help="choose the target folder (full UUIDs or unique prefixes)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--restore", nargs="?", const="", metavar="BACKUP", help="restore a backup (default: the latest)")
    mode.add_argument("--enable-auto", action="store_true", help="move sessions automatically on every account switch")
    mode.add_argument("--disable-auto", action="store_true", help="turn the automatic mode off")
    mode.add_argument("--watch", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.restore is not None:
            return cmd_restore(args)
        if args.enable_auto:
            return cmd_enable_auto(args)
        if args.disable_auto:
            return cmd_disable_auto(args)
        if args.watch:
            return cmd_watch(args)
        return cmd_sync(args)
    except Abort as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
