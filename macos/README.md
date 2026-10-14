# neo for Mac

A menu bar app that runs neo on your Mac: start Claude Code and Codex tasks, see every agent,
run and question in one window, search your past sessions, and keep a memory your agents share.
It uses the `claude` and `codex` CLIs you are already signed in to, so there are no API keys to add.
Slack is optional.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/898anil/neo/main/macos/install.sh | bash
```

Or download `Neo-<version>-arm64.dmg` (Apple silicon) or `-x86_64.dmg` (Intel) from
[Releases](https://github.com/898anil/neo/releases) and drag **Neo** to Applications.

The app is not notarized yet. If macOS says it "can't be opened", open
**System Settings → Privacy & Security** and click **Open Anyway** (once), or run
`xattr -dr com.apple.quarantine /Applications/Neo.app`. The install script does this for you.

Needs macOS 14 (Sonoma) or later, and at least one agent CLI:

| Tool | Why | Install |
|---|---|---|
| `claude` | runs tasks with Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash`, then run `claude` once to sign in |
| `codex` | runs tasks with Codex | `npm install -g @openai/codex`, then run `codex` once to sign in |
| `herdr` | *optional*: live agents you can take over in a terminal | `brew install herdr` |
| `gh` | *optional*: pull requests on the Work tab | `brew install gh && gh auth login` |

The first window checks which of these it can find (using your login shell's `PATH`) and asks a few
questions: your name, your default agent, what background tasks may do, and your projects folder.

## Using it

- **The window** is neo's dashboard: *Now* (what needs you), *Agents*, *Work*, *History* and **+** to
  start a task. It is the same page the phone uses, so the main [README](../README.md) describes it.
- **The menu bar icon** shows whether neo is running, and has Open, Restart, Stop, Settings and the log.
- **Settings** (⌘,) changes the choices from setup and can open neo at login.

### What background tasks may do

| Setting | Claude Code | Codex |
|---|---|---|
| Only read | `--permission-mode default` | `read-only` sandbox |
| Edit files in the project (default) | `acceptEdits` | `workspace-write` sandbox |
| Do anything | `bypassPermissions` | `danger-full-access` |

Each task you start from **+** can still pick its own level.

## Slack

With Slack connected you can mention `@neo` in any channel, continue a run by replying in its
thread, and get a DM when any agent on the Mac needs you.

1. At <https://api.slack.com/apps> choose *Create New App* → *From a manifest*, pick your workspace
   and paste [`manifest.json`](../manifest.json). It sets Socket Mode, the bot scopes and the events.
2. *Install to Workspace*, then copy the bot token (`xoxb-`) from *OAuth & Permissions*.
3. *Basic Information* → *App-Level Tokens*: create one with `connections:write` (the `xapp-` token).
4. In neo's Settings paste both tokens and your Slack user id (your profile → ⋯ → *Copy member ID*).
   Only the user ids listed there can start agents on your Mac. Then invite `@neo` to a channel.

Tokens are kept in your login keychain, not on disk.

## Where things are

| What | Where |
|---|---|
| Settings (any variable from [`.env.example`](../.env.example) works here) | `~/Library/Application Support/Neo/neo.env` |
| Runs, findings, watches | `~/Library/Application Support/Neo/` |
| Memory (shared with every agent neo starts) | `~/.neo/memory/` |
| Log | `~/Library/Logs/Neo/neo.log` |
| Slack tokens | login keychain, service `dev.neo.mac` |

The dashboard listens on `127.0.0.1` only (port 8790, or the next free one) and asks for a token, so
other programs and other accounts on the Mac cannot start agents through it. The app makes a new token
each launch (or uses `COS_TOKEN` from `neo.env`); *Open in browser* signs you in. To reach it from your
phone, see *Dashboard access* in the main README.

Agents neo starts do not see the Slack tokens: neo removes them from its environment at startup.

Uninstall: quit neo, delete `/Applications/Neo.app`, and optionally the folders above.

## Build from source

```bash
brew install uv
macos/scripts/build-app.sh              # this Mac's architecture
ARCH=x86_64 macos/scripts/build-app.sh  # Intel
```

This builds the Swift app, bundles a standalone Python 3.12 with neo's dependencies
([`requirements.txt`](requirements.txt), pinned to `poetry.lock`) and neo's code, signs it ad hoc
(set `SIGN_IDENTITY` for a Developer ID), and writes `macos/dist/Neo.app` and a DMG.
Tests: `swift test --package-path macos`.

Releases are built by [`.github/workflows/macos-app.yml`](../.github/workflows/macos-app.yml) when a
tag `app-v<version>` is pushed.

### How it fits together

```
Neo.app
  Contents/MacOS/Neo            SwiftUI: menu bar, window (WKWebView), setup and settings
  Contents/Resources/python/    standalone Python 3.12 + slack-bolt, aiohttp, python-dotenv
  Contents/Resources/neo/       neo's code (app.py, cos/, prompts/)
```

The app starts `python -m cos serve` (no Slack) or `python app.py` (Slack tokens set) with
`COS_HOME` pointing at Application Support, the `PATH` of your login shell, and the settings from
`neo.env`; restarts it if it stops; and stops it when you quit.
