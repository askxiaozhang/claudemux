# claudemux

> A tmux-native dashboard for your many Claude Code sessions. See every running
> Claude at a glance, switch between them in one keystroke, and never lose track
> of which project is waiting on you.

![claudemux board](docs/board.png)

`claudemux` (command: `cw`) gathers every Claude Code session you have running
into one tmux session. Each session lives in a window split
`[ board 30% │ conversation 70% ]` — the board stays visible on the left while
you talk to Claude on the right, so you always know which session is busy, idle,
blocked on a question, or done, and can jump between them without ever leaving
tmux.

## Why

Run more than one Claude at a time and you quickly lose track: *which terminal
was the API server, which one is waiting for me to confirm, which one already
finished?* `claudemux` turns that chaos into a single screen — and the terminal
stays front and center, because the terminal is where the real work happens.

## Features

- **One-screen overview** — every live Claude session, grouped by status.
- **"Waiting (needs you)"** — surfaces sessions blocked on a question, with the
  actual question shown inline. This is the painkiller.
- **Instant switching** — select a card, land in that conversation. The board
  stays put on the left, so you never lose the overview.
- **Import existing sessions** — `claude --resume <sid>` pulls a bare-terminal
  session into tmux without losing the conversation history.
- **Launch new ones** — pick a project, optional first prompt, done.
- **Live status** — busy/idle, current task, todo progress (`TodoWrite`), last
  message, git branch, age — all read from the transcript.
- **Floating HUD (macOS)** — `cw hud` opens a native always-on-top panel with the
  same overview as card tiles, independent of any terminal window.
- **Zero dependencies** — pure Python 3 stdlib + tmux. No pip, no npm.
  (The optional `cw hud` panel additionally needs PyObjC.)

## Requirements

- macOS or Linux
- tmux ≥ 3.0
- Python 3.8+
- Claude Code CLI (`claude`) on your PATH

## Install

```sh
git clone https://github.com/askxiaozhang/claudemux.git
cd claudemux
chmod +x cw.py
echo "alias cw='python3 $PWD/cw.py'" >> ~/.zshrc   # or ~/.bashrc
```

## Quick start

```sh
cw up        # create/attach the tmux session and bind the hotkeys
```

Then, anywhere inside the `cw` session:

- `Ctrl-b b` — focus / summon the board pane (the left panel)
- `Ctrl-b B` — focus the conversation pane (the right panel)
- `Ctrl-b ←` / `Ctrl-b →` — move focus between panes (tmux default)
- `Ctrl-b z` — zoom the active pane to full width, again to restore
- `Ctrl-b N` — new Claude in a project (prompts for cwd)

## The board

The board is the left 30% of every window; the conversation is the right 70%.
Both panes are always visible — `Ctrl-b b` / `Ctrl-b B` (or `Ctrl-b ←/→`) just
move the keyboard focus between them.

### Two levels: project and status

Sessions have two axes — **which project** they belong to, and **what status**
they're in (running / waiting / external / done). The board shows both, and you
pick which one is the top level:

- **Project view** (default) — one row per project, with a status roll-up
  (`● 1  ✓ 1`) and its git branch. Projects that have a session *waiting on you*
  sort to the top and expand automatically; the rest stay collapsed to one line.
  Expand/collapse a project with `Enter` / `Space` (or click its header); `z`
  toggles all at once.
- **Status view** — the classic grouping: `RUNNING`, `WAITING (needs you)`,
  `EXTERNAL`, `DONE`, each session tagged with its project name.

Press **`g`** to flip between the two. Your selection is preserved across the
switch and across refreshes.

**Mouse works too** (`cw up` turns it on for the `cw` session only):

- **Click a project header** → collapse / expand it.
- **Click** a card → select it (its detail shows below).
- **Click the selected card again**, or **double-click** any card → open it
  (switch to that window / import / reply).
- **Scroll wheel** → move the selection up and down.

Keyboard:

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` / scroll | move selection (over cards and project headers) |
| `g` | toggle project view ↔ status view |
| `Enter` / `Space` | project header → collapse/expand · card → switch / import / reply |
| `z` | collapse / expand all projects |
| `n` | new Claude session (cwd + optional prompt) |
| `i` | import the selected external session |
| `r` | refresh |
| `q` | focus the conversation pane (board stays visible) |

Status groups:

- **Running** — busy interactive sessions and running background agents
- **Waiting (needs you)** — idle sessions and blocked background agents (the
  question is shown inline)
- **External (importable)** — Claude sessions in a bare terminal, not yet in tmux
- **Done** — completed background agents

The selected card's detail panel shows cwd, the full current task, todo list,
last message, and git branch. Selecting a collapsed project header instead shows
a summary of every session in that project.

## Floating HUD (macOS)

`cw hud` opens a native, always-on-top panel that mirrors the board as card
tiles — grouped by project, colored by status, tagged with the `[config]` each
session belongs to. It runs as its own process and stays pinned on top of every
app, so you get the overview without keeping a terminal in view.

Requires PyObjC (`pip install pyobjc`); it's optional and only needed for `cw hud`.

The panel's toolbar (top-right) cycles and toggles:

- **悬浮 / 终端 / 并存** — a three-way mode cycle: HUD only → terminal only
  (HUD collapses to a title bar) → both side by side. In terminal/both modes the
  panel brings the terminal attached to the `cw` session to the front, or opens a
  new window with `tmux attach -t cw` if nothing is connected yet.
- **归档区 / 看板** — switch between the live board and the archive.
- **– / +** — minimize the panel to a title bar and back.
- **✕** — quit the HUD (also `Esc`, `q`, or `Ctrl-C`).

Interactions:

- **Click** a tile to switch to that session's tmux window and bring the terminal
  forward.
- **Drag** the title bar to move the panel; drag an edge to resize.
- **Right-click** a tile → **归档 / 取消归档** to archive or unarchive that
  session. Archived sessions are hidden from the board and kept under the archive
  view; archiving is stored by `sessionId` in `~/.cw_archived.json` and persists
  across restarts.


## Commands

```
cw                         create/attach tmux session + bind keys (default)
cw up                      same as above
cw board                   run the board TUI directly
cw launch <cwd> [prompt] [--config doubao|official]   open a new Claude window in <cwd>
cw import <sid>            import an existing session via claude --resume
cw hud                     floating macOS overview panel (needs PyObjC)
cw list [--by project|status]  print the board as plain text (no TUI)
cw status                  print discovered sessions/jobs as JSON
```

## How it works

`claudemux` only **reads** files Claude already writes — it doesn't patch or
wrap the CLI:

| Source | Read for |
|---|---|
| `sessions/<pid>.json` | live interactive sessions: pid, sessionId, cwd, busy/idle |
| `jobs/<id>/state.json` | background agents: state, the question it's blocked on, tokens |
| `projects/*/<sid>.jsonl` (tail) | session title, current task, last message, `TodoWrite` progress, git branch |
| `.claude.json` | known projects |

It scans `~/.claude-doubao`, `~/.claude-official` and `~/.claude`, dedupes by
`sessionId`, and remembers which config dir each session came from. tmux
windows are named `<project>-<sid8>`; the board uses that 8-char suffix to tell
switchable (in-tmux) sessions apart from external (bare-terminal) ones — no
process-tree walking required.

**Config-aware launch/resume.** If you run Claude through multiple
`CLAUDE_CONFIG_DIR` profiles (e.g. `ccdoubao=CLAUDE_CONFIG_DIR=~/.claude-doubao
claude`), the board tags each card with its config (`[doubao]`, `[official]`, …)
and always relaunches/resumes with the matching `CLAUDE_CONFIG_DIR` — so
importing or replying to a session never switches you to the wrong model.
`cw launch` takes `--config <label>` and the board's **new-session** prompt lets
you pick the config when more than one profile exists.

```
   ┌──────────────── tmux session "cw" ─────────────────┐
   │  every window:  [ board 30% │ conversation 70% ]    │
   │                                                      │
   │  win api-server-3a305475                             │
   │   ┌─board──────┬─conversation─────────────┐          │
   │   │RUNNING (1) │ $ claude                  │          │
   │   │▸ ● api-srv  │ > deploying to staging…  │          │
   │   │WAITING (1) │                           │          │
   │   │  ? web-app  │                           │          │
   │   └────────────┴───────────────────────────┘          │
   │  win web-app-0702e324    win docs-4966e175    …       │
   │   ▲ pick a card on the left → switch window            │
   │     the board stays visible in every window            │
   └────────────────────────────────────────────────────────┘
```

## Limitations

- **You can't attach to a bare terminal retroactively.** macOS (SIP) and tmux
  only let you manage sessions they started. Sessions already running in a plain
  terminal appear under **External** and are imported via `claude --resume`,
  which continues the conversation in a fresh tmux window.
- **After importing, close the old terminal.** `--resume` points the new tmux
  process at the same session; two writers can conflict.

## Troubleshooting

- **`Ctrl-b b` does nothing** — re-run `cw up` (it re-binds the key in the tmux
  prefix table). Run `tmux list-keys -T prefix | grep cw.py` to verify.
- **No board pane in this window** — `Ctrl-b b` creates one on the left if
  missing. Windows from before the latest `cw up` get a board pane on first
  `Ctrl-b b`.
- **No cards show up** — run `cw list`; if empty, make sure Claude sessions are
  actually running and at least one config dir (`~/.claude`,
  `~/.claude-doubao`, `~/.claude-official`) exists.
- **The board pane is blank / errored** — run `python3 cw.py board` directly in
  a terminal to see any traceback.
- **Wrong Claude profile on launch** — the board now pins each launch/resume to
  the session's own `CLAUDE_CONFIG_DIR`, and `cw launch --config <label>` forces
  a specific profile. Only sessions in config dirs `cw` doesn't scan (see the
  list in *How it works*) stay invisible — add yours to `CONFIG_DIRS` in `cw.py`.

## License

MIT — see [LICENSE](LICENSE).
