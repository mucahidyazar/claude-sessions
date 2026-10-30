# claude-sessions

Keep your Claude desktop **Code** sessions when you switch accounts (macOS).

If you use more than one account in the Claude desktop app, your Code sessions vanish from the
sidebar every time you switch. They are not deleted: the app keeps each session's record in a
folder per account and lists only the folder of the account you are signed in to.
`claude-sessions` moves the records into the active account, so all your sessions show up
again, with one command after a switch or automatically.

> Unofficial and not affiliated with Anthropic. It relies on how the desktop app stores
> sessions on disk, which may change with any update. Tested on macOS 26 with Claude desktop
> 4.4212.0.

## Install

You need macOS, the Claude desktop app and Python 3.9 or newer (the `python3` that comes with
the Xcode Command Line Tools works). There are no dependencies.

```bash
git clone https://github.com/mucahidyazar/claude-sessions.git ~/claude-sessions
echo 'alias claude-sessions="python3 ~/claude-sessions/claude-sessions.py"' >> ~/.zshrc
source ~/.zshrc
```

## Usage

After you switch accounts in Claude, run:

```bash
claude-sessions
```

It quits Claude, backs up the session folder, moves the sessions into the account you are
signed in to and starts Claude again:

```
Active account : 5f3c2a1b…/9d8e7f6a…  (Claude log, 2026-10-29 22:40:38)
Already here   : 8 sessions
To move        : 46 sessions
    19  ← 1a2b3c4d…/4d3c2b1a…
     4  ← 7e8f9a0b…/0b9a8f7e…
    23  ← 3c4d5e6f…/6f5e4d3c…
Quitting Claude…
Backup         : ~/.claude-sessions/backups/claude-code-sessions-20261029-224038.tar.gz
46 sessions moved to this account.
Starting Claude.
```

It also works from the terminal inside Claude: the work runs in a separate process, so it
finishes after Claude quits.

| Command | What it does |
| --- | --- |
| `claude-sessions -n` | Show what would be moved, change nothing |
| `claude-sessions --no-restart` | Move without quitting Claude; restart it yourself |
| `claude-sessions --to ACCOUNT/ORG` | Choose the target folder yourself |
| `claude-sessions --restore [BACKUP]` | Restore the latest backup, or the one given |

The first time, macOS asks whether your terminal may control Claude. Allow it so the script can
quit Claude the normal way.

## Automatic mode

```bash
claude-sessions --enable-auto
```

This installs a LaunchAgent (`~/Library/LaunchAgents/local.claude-sessions.plist`) that watches
Claude's log. When you switch accounts, it waits a few seconds for the switch to settle, shows a
notification, then restarts Claude with your sessions moved over. It starts at login and writes
to `~/.claude-sessions/watch.log`. `claude-sessions --disable-auto` removes it.

On the first automatic restart, macOS may ask whether Python may control Claude. If you don't
allow it, the watcher quits Claude with SIGTERM instead, which Electron apps handle as a regular
quit.

## How it works

- Session records live in
  `~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json`.
  The conversations themselves are in `~/.claude/projects` and are shared by all accounts, so
  moving a record is enough to bring a session back.
- The active folder comes from Claude's log (`~/Library/Logs/Claude/main.log`), which records
  every session folder Claude loads. It is checked against the signed-in account in Claude's
  config; if the two disagree, nothing is moved.
- A backup of the whole session folder is written to `~/.claude-sessions/backups` before every
  change (the last 20 are kept).
- If a session has copies in several accounts, the one with the latest activity wins.
- A session you deleted in one account is not brought back from another.
- Pending input and the task list (backlog) move along with the sessions.

### Why not symlinks?

The tidy fix would be to point every account folder at one shared folder. The app refuses to
write session files through symlinks, so saving breaks. Hard links don't last either: the app
saves a record by writing a new file and renaming it over the old one.

### Scratch sessions

A session started without a folder runs in a scratch workspace under
`~/Library/Application Support/Claude/scratch-workspaces/<account>/<org>/`. Claude deletes the
signed-in account's scratch workspaces that no loaded session uses once they have been idle for
7 days. That's why a scratch session's record also stays in its own account: when you switch
back to it, its workspace is still in use and is kept.

## Not covered

- Scheduled tasks stay in their account; the script tells you if there are any.
- Cowork (agent mode) sessions are not moved.
- Windows and Linux.

## License

[MIT](LICENSE)
