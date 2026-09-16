# vyctl

A free, lightweight, standalone **Terminal UI for running several Claude Code sessions in
parallel** — one real console per project, all of them in one window.

Built with [Textual](https://textual.textualize.io/). No server, no account; everything is
persisted in a local `config.json`.

```
python -m vyctl          # or:  .\run.ps1
```

---

## The layout

One screen, two columns. The left ~30% holds everything textual; the right ~70% is
nothing but the console.

```
┌──────────────────────────┬────────────────────────────────────────────────┐
│ vyctl v1.0.0             │ vyctl   [Idle]  ·  pid 31704                   │
│ 3 projects · interactive ├────────────────────────────────────────────────┤
│ SESSIONS                 │                                                │
│ ● vyctl          Idle    │   ▐▛███▛█   Claude Code v2.1.271               │
│ ● swarm       Working    │  ▝▜██████▀  Opus 5 (1M context)                │
│ ○ golearn     Inactive   │    ▝▝ ▝▝    D:\workspace\personal\swarmdrones  │
│ TODO                     │  ────────────────────────────────────────────  │
│ ○ add a VCS test         │  ❯ how do I log an error?                      │
│   swarm                  │  ────────────────────────────────────────────  │
│ ◐ write the docs         │    ⏵⏵ auto mode on (shift+tab to cycle)        │
│   vyctl                  │                                                │
│ DETAILS                  │                                                │
│ folder   D:\workspace\…  │                                                │
│ status   Idle            │                                                │
│ pid      31704           │                                                │
│ running  2/2             │                                                │
│ todo     Backlog 1 …     │                                                │
└──────────────────────────┴────────────────────────────────────────────────┘
```

* **SESSIONS** — every project with a live status dot. Moving the cursor swaps which
  console the right side shows; the others keep running in the background.
* **TODO** — one flat list ordered Backlog → In Progress → Completed, with a glyph per
  state (`○ ◐ ●`) and the project each task belongs to.
* **DETAILS** — the technical block for the selection: folder, command, extra args,
  status, pid, session id, turn count, cost, and the global running/working counts.
* **The console** — the selected project's Claude session, edge to edge. No borders, no
  input box, no decoration: keystrokes go straight into the real thing.

Press **f9** to hide the sidebar and give the console the whole window.

---

## Install

```powershell
cd D:\workspace\tools\claude_terminals
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m vyctl
```

`run.ps1` does all of that for you on first launch.

Requirements: Windows with `powershell.exe`, Python 3.10+, and the `claude` CLI on
`PATH`. Windows Terminal is recommended (true colour + mouse).

First run has no projects: press **f2**, then **a**, and fill in a name and folder.

---

## Keys

Function keys and `alt+digit` are reserved by vyctl and **never** reach the console,
so you can always get out of a live session. Everything else goes to Claude.

| | |
|---|---|
| `f1` | focus the console |
| `f2` / `f3` | focus the SESSIONS / TODO list |
| `f4` or `?` | help |
| `f5` | restart the selected session (also how you abort a running turn) |
| `f9` | hide/show the sidebar |
| `f10` | switch console mode: real terminal ↔ headless log |
| `f12` | release the keyboard back to the sidebar |
| `alt+1…9` | jump straight to the Nth session |
| `ctrl+q` | quit — kills every background `powershell.exe` and `claude` |
| **In the console** | everything else: typing, arrows, `shift+tab`, `ctrl+c`, `/commands` |
| wheel · `shift+pageup`/`pagedown` | scroll the console's history (`shift+end` = back to live) |
| `ctrl+v` | paste the clipboard into the prompt (multi-line arrives as one block) |
| `f8` | copy what the console shows to the clipboard |
| `ctrl+c` | **interrupt**, as in any terminal — which is why copy is `f8` |
| **SESSIONS** (f2) | `↑↓` select · `enter` take the keyboard · `a` add · `e` edit · `d` delete · `space` active · `s` start · `x` stop · `r` restart |
| **TODO** (f3) | `n` new · `e` edit · `d` delete · `h`/`l` move column · `p` send to Claude · `c` complete |

Pressing **`p`** on a task types it into its project's console and marks it *In Progress* —
the "hand this to Claude" button.

---

## How it works

### Interactive mode (default)

Each project gets a genuine **pseudo-console**, not a pipe:

* **pywinpty** opens a Windows **ConPTY** and spawns `powershell.exe -NoLogo -NoProfile`
  in the project folder. Once the shell draws its prompt, vyctl types the startup
  command for you. Writing earlier would be silently discarded, which is why it waits.
* **pyte** is a terminal emulator: the console's byte stream is fed to it, and it keeps
  the screen contents — characters, colours, cursor — exactly as a real terminal would.
  That screen is then drawn into the pane as Rich text.
* Keystrokes are translated back into the escape sequences a console app expects
  (`enter`→`\r`, `up`→`ESC[A`, `ctrl+c`→`0x03`, …), so the embedded Claude Code behaves
  like the one in your own terminal.
* Resizing the window resizes the ConPTY, and Claude Code reflows live.

Because it is a real console, `/slash` commands, `shift+tab` mode cycling, the trust
prompt for a new folder, and permission dialogs all work normally — answer them right in
the pane.

**Auto-resume**: the startup command is
`claude --continue; if ($LASTEXITCODE -ne 0) { claude }`, so a session reopens the
folder's most recent conversation, and falls back to a fresh one when there is nothing to
resume.

**Nested-session guard**: if vyctl is launched from *inside* a Claude Code session,
the consoles would inherit `CLAUDE_CODE_CHILD_SESSION` and friends and behave as child
sessions — which disables transcript saving and therefore breaks `--continue`. Those
markers are stripped from each console's environment.

### Headless mode (`f10`)

The console is replaced by a clean colour-coded log plus a prompt box. Each prompt is
dispatched as `claude --resume <id> --print --output-format stream-json --verbose`, so
tool calls, results and end-of-turn cost appear as structured lines. Prefix a line with
`!` to run raw PowerShell in the same persistent shell. This mode is better for glancing
at what several projects are doing; interactive mode is better for actually working.

### Clean teardown — no orphans

Every console/shell is placed in a Windows **job object** with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. This matters: `taskkill /T` only walks parent→child
links, so anything a shell detached (the `claude` node process, a dev server started with
`Start-Process`) becomes unreachable the moment the shell exits. Job membership is
inherited by every descendant, so `TerminateJobObject` reaps the whole set no matter what
the tree looks like by then. On quit vyctl asks each session to exit, terminates its
job, then runs `taskkill /PID <pid> /T /F` as a third belt; an `atexit` hook covers a hard
crash.

### A known nuance

Claude Code emits `CSI > 4 ; 2 m` (xterm's "modifyOtherKeys" keyboard negotiation) on
every repaint. pyte does not understand the `>` private prefix, so it parsed that as a
plain SGR with parameters **4 and 2** — "underline on" plus "faint on" — and those stuck
to the cursor attributes, so everything drawn afterwards, including your own typed input,
came out underlined and grey. Every CSI sequence with a `<`, `=` or `>` prefix is
therefore stripped before it reaches the emulator (the kitty keyboard sequences
`CSI > 1 u` / `CSI < u` were the same story, and additionally spilled a stray `u` onto the
screen). DEC private modes like `?25l` and `?2026h` are left intact, since pyte does model
those.

pyte also has no concept of **faint** (SGR 2) at all. A small `Screen`
subclass carries faint in the unused `blink` slot — Claude Code never blinks — and the
renderer draws it as dim. SGR 22 ("normal intensity") is expanded to `22;25` so it clears
both bold and faint, which pyte would otherwise apply to bold alone.

Claude Code's **fullscreen renderer** drives the alternate screen buffer, which pyte does
not model -- the symptom is a pane where everything looks highlighted. vyctl therefore
launches each console with `--settings <console-settings.json>`, a one-line file pinning
`"tui": "default"` (the classic renderer). Your own `~/.claude/settings.json` is never
touched, so normal terminal work keeps whatever renderer you prefer. Set
`"force_classic_tui": false` in `config.json` to disable the override.

---

## Debugging

Off by default. To capture what happened:

```powershell
.\.venv\Scripts\python.exe -m vyctl --debug        # lifecycle log
.\.venv\Scripts\python.exe -m vyctl --debug-raw    # + raw console bytes
```

`vyctl.log` lands next to `config.json` (rotating, 2 MB x 3) and records each
console's pid and working directory, the exact command typed into it, every status
transition, teardown, and any exception a reader task hit. Persist it with
`"log_level": "debug"` in `config.json` instead of the flag.

`--debug-raw` additionally dumps every byte the consoles emit. That is the tool for a
rendering bug — it is how the mis-parsed `CSI > 4 ; 2 m` behind the "everything is
underlined/dim" artifact was found — but it contains the full text of your sessions, so
turn it back off when you are done.

---

## config.json

Written next to the package; override with `--config PATH` or `$env:VYCTL_CONFIG`.
Writes are atomic, and an unreadable file is backed up to `config.json.bak` rather than
overwritten.

```json
{
  "projects": [
    {
      "id": "a1b2c3d4",
      "name": "swarmdrones",
      "path": "D:\\workspace\\personal\\swarmdrones",
      "command": "claude",
      "extra_args": "",
      "active": true
    }
  ],
  "tasks": [
    {
      "id": "9f8e7d6c",
      "title": "add a VCS unit test",
      "column": "backlog",
      "project_id": "a1b2c3d4",
      "notes": ""
    }
  ],
  "pane_mode": "interactive",
  "auto_resume": true,
  "autostart": true,
  "resume_probe": false,
  "stream_json": true,
  "max_log_lines": 2000
}
```

| key | meaning |
|---|---|
| `command` | the Claude invocation; may carry flags (`claude --model opus`) |
| `extra_args` | appended to the invocation — e.g. `--permission-mode acceptEdits` |
| `pane_mode` | `interactive` (real console) or `headless` (pipe-driven log) |
| `auto_resume` | reopen the folder's latest conversation on start |
| `autostart` | bring up all active sessions at launch |
| `resume_probe` | headless only: send one tiny turn at startup to re-attach eagerly |
| `stream_json` | headless only: structured live output vs. plain text at end of turn |
| `force_classic_tui` | launch consoles with `"tui": "default"` so the emulator renders correctly |

### Permissions note

In **interactive** mode permission prompts appear in the console and you just answer them.
In **headless** mode Claude cannot ask, so a turn needing approval will stall or refuse —
put a policy in `extra_args` (`--permission-mode acceptEdits` is the usual choice;
`--dangerously-skip-permissions` hands that folder unrestricted tool access, so opt in
deliberately).

---

## Layout

```
vyctl/
  config.py     dataclasses + atomic config.json persistence
  process.py    headless sessions, job-object teardown
  terminal.py   ConPTY + pyte emulator + key encoding (interactive mode)
  widgets.py    console view, sidebar rows, modal forms
  app.py        the Textual App: sidebar + stage, bindings, wiring, entry point
  app.tcss      stylesheet
```
