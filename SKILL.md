---
name: pythond
description: Persistent Python runtime for AI agents. Use when the agent needs to keep variables, imports, connections, sockets, threads, servers, or analysis state alive across turns. Send code through pysh run/fire/fork/poll. Attach a human REPL. Operate local or ssh-reached pythond daemon sessions. Also covers stealth web browsing via bundled cloakbrowser reference -- use when the task involves scraping, anti-bot evasion, or browser automation that must avoid detection.
---
# pythond

Persistent Python with a function-call API. Code in, result out.

```bash
pysh run work "x = 42"   # sets x
pysh run work "x + 1"    # 43 (x survived)
```

Variables, connections, threads persist between calls. Output is plain
captured text with exec() semantics.

`pysh` is your function-call surface into that persistent process: source code
in, captured output out.

## Mental Model

One named session is one Python child process with one persistent namespace.

Your cells run through Python `eval`/`exec`. Human attach is a line REPL into
the same namespace. You and the human are operating the same live runtime.

## Default Work Surface

Use the live session as the default work surface. Once a daemon/session exists,
run task logic through `pysh run`, `pysh fire`, or `pysh fork`. The cell is
Python, so host checks such as `ls`, `pwd`, and `git status` should run through
`subprocess.run(...)` inside the session when their result belongs to the task.

The host shell is plumbing for starting the daemon, writing Python files to disk
before loading them, and package management (`pip install`). Everything else goes
through the session so cwd, env, imports, sockets, variables, and visible history
stay together.

Keep state in the Python namespace when you will need it again:

- parsed data, config, DataFrames, compiled regexes, imported modules,
- database handles, HTTP sessions, WebSockets, sockets, SSH tunnels,
- local servers, file watchers, background daemon threads,
- live decisions such as blocked IPs, feature flags, rate limits, and routing
  weights.

Read, patch and inspect them by name in later cells; recompute only when the
task needs fresh state.

Run host commands from inside the session when their output is part of the task:

```python
import subprocess
subprocess.run(["git", "status"], capture_output=True, text=True)
```

## Bootstrap

```bash
pythond daemon          # start daemon (foreground)
pysh new work           # create a persistent session
```

`pythond daemon` starts the daemon. One daemon manages all sessions.
Each session is an isolated subprocess with its own namespace.

`new` creates the session and returns `201`; if the name exists it returns
`409` and leaves the existing state as it is. `pysh new work --replace`
discards the existing session and creates a fresh one.

`pyctl start` is an alias for `pythond daemon`. Prefer `pythond daemon` in
agent workflows; use `pyctl` for stop/status.

## Core Commands

```bash
pysh new <name>              # create session (409 if the name exists)
pysh new <name> --replace    # replace an existing session
pysh run <name> "code"       # sync exec/eval, raw output
pysh fire <name> "code"      # async thread, shared namespace
pysh fork <name> "code"      # async process, POSIX only, killable
pysh poll <name> [cell_id]   # read async result
pysh attach <name>           # human line REPL, Ctrl-D detaches
pysh int <name>              # fire=best effort, fork=kill
pysh kill <name>             # terminate session
pysh kill --all              # terminate current sessions, keep daemon running
pysh ls                      # list sessions
pysh status <name>           # JSON health
pysh vars <name>             # JSON namespace names
pysh complete <name> "text"  # JSON completion candidates
pysh cp <src> <dst>          # copy pickled objects (scp syntax)
```

Session names are canonical lowercase: `a-z`, `0-9`, `_`, or `-`, 1-80
characters. Uppercase, dots and Windows device names (`con`, `nul`, `prn`,
`aux`, `com1`, `lpt1`) are rejected.

## run, fire, fork

Use `run` for short operations and direct inspection.

```bash
pysh run work "import sqlite3; db = sqlite3.connect('app.db')"
pysh run work "db.execute('select count(*) from users').fetchone()"
```

Use `fire` for slow work that must share the live namespace.

```bash
pysh fire work "model = train(X, y)"
pysh poll work <cell_id>
pysh run work "model.score(X_test)"   # model is there
```

Use `fork` for slow or risky work that should be killable. It runs in a child
process and pickles new or reassigned variables back into the parent namespace.
Unpicklable objects (sockets, locks, CUDA tensors) are skipped. A name
merges back when the child reassigns it (`x = new_value`); in-place mutation
of an existing object stays in the child. A failed fork leaves the parent
namespace unchanged.

```bash
pysh fork work "results = expensive_search(params)"
pysh poll work <cell_id>
pysh int work                # kills fork'd process
```

`fire` cells in one session run serially under the session lock. Use multiple
sessions for real parallelism.

`run` waits 30 seconds for the reply. A cell that runs longer keeps running,
but the channel is then out of sync and the way on is `kill` then `new`.
Anything that might take longer goes through `fire` or `fork`.

## State Persists

Variables set in one `run` call are available in the next:

```bash
pysh run work "import pandas as pd; df = pd.read_csv('big.csv')"
pysh run work "len(df)"       # 1000000  (df still in memory)
pysh run work "df.describe()" # summary  (no re-read needed)
```

Connections, threads, servers -- anything in the namespace -- stay alive:

```bash
pysh run work "import sqlite3; db = sqlite3.connect('app.db')"
# ... 100 turns later ...
pysh run work "db.execute('SELECT count(*) FROM users').fetchone()"
# (42,)   (same connection)
```

## File Loading

For code with quotes, f-strings, SQL, or more than a small expression, write a
file and post it as the cell (curl's `@file` syntax):

```bash
cat > /tmp/pythond_task.py << 'EOF'
import pandas as pd
df = pd.read_csv("data.csv")
print(f"rows={len(df)} cols={list(df.columns)}")
EOF
pysh run work @/tmp/pythond_task.py
```

The file is transport. The namespace is the workspace.

`@file` reads the file on the client. When the file lives where the session
runs (e.g. a remote daemon reached over ssh), load it inside a cell instead:
`pysh run work "exec(open('/path/on/server.py').read())"`.

## Moving Objects (cp)

`run` moves source; `pysh cp` moves live objects as pickles, scp syntax.
A side is `session:var`, `session:` (whole picklable namespace), or a file:

```bash
pysh cp work:df df.pkl          # checkpoint one object to disk
pysh cp df.pkl gpu:df           # inject it into another session
pysh cp work:model gpu:model    # session -> session, no temp file
pysh cp work: backup:           # clone the picklable namespace
```

Use it to move parsed data between sessions instead of re-parsing, or to
checkpoint expensive objects across daemon restarts. Unpicklable values
(sockets, locks, modules) are skipped with a warning -- reopen those in the
destination session.

## Output Formats

| Command                    | Output                                         |
| -------------------------- | ---------------------------------------------- |
| `run`                    | raw captured text                              |
| `fire`                   | JSON`{"cell_id": "...", "status": "fired"}`  |
| `fork`                   | JSON`{"cell_id": "...", "status": "forked"}` |
| `poll`                   | JSON cell result                               |
| `status`                 | JSON session health                            |
| `vars`                   | JSON namespace names                           |
| `ls`                     | text listing                                   |
| `new`, `kill`, `int` | text confirmation or error                     |

An error in `run` returns the traceback with exit code 1; the session keeps
running.

## Remote Sessions

Remote access is ssh. The state lives in the remote daemon, so one-shot
calls are enough:

```bash
ssh server pysh run work "x = 42"
ssh server pysh run work "x + 1"     # -> 43 (remote state)
```

For per-call latency, enable ssh connection reuse once in `~/.ssh/config`:

```
Host server
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

Interactive human access: `ssh -t server pysh attach work`.

Tunneled mode (client-side pysh against a forwarded port):

```bash
ssh -L 7984:127.0.0.1:7984 server
export PYTHOND_HOST=127.0.0.1:7984 PYTHOND_TOKEN=<remote-token>
pysh run work "code"
```

## Direct HTTP (clients and debugging)

The daemon speaks plain HTTP; curl is the debug client:

```bash
curl --unix-socket $XDG_RUNTIME_DIR/pythond/pythond.sock \
     --data-binary '1 + 1' http://pythond/run/work    # -> 2
```

Python source goes in the request body, raw. `Content-Length` counts UTF-8
bytes. Adapters can speak HTTP directly instead of spawning the CLI per call.

`new` returns `201`, or `409` if the name exists; `?replace=1` replaces.
`fire` / `fork` return `202` with a JSON receipt and
`Location: /poll/<name>?cell=<id>`. Treat any `2xx` as success. The README
documents the HTTP API and the SSE stream.

## Security Model

Treat pythond like SSH into a Python runtime.

- Code runs with the daemon user's OS permissions.
- A connected client has full access to all sessions, the same as a login
  shell.
- Local POSIX uses a unix socket with file permissions.
- Local Windows uses loopback TCP plus a bearer token in
  `%LOCALAPPDATA%\pythond\daemon.json`.
- The daemon binds only the unix socket or 127.0.0.1; ssh or a reverse proxy
  carries remote access.

Runtime files and durable state are separate:

| Purpose              | Windows                                       | POSIX                                                   |
| -------------------- | --------------------------------------------- | ------------------------------------------------------- |
| daemon metadata      | `%LOCALAPPDATA%\pythond\daemon.json`        | `$XDG_RUNTIME_DIR/pythond/` or `/tmp/pythond-$UID/` |
| session checkpoints  | `~\.pythond\sessions\...`                   | `~/.pythond/sessions/...`                             |

## REPL Patterns

- Import once, then use shorter names in later cells.
- Use expression results directly: `pysh run work "len(items)"`.
- For complex code, write a file and load it with
  `exec(open('/tmp/name.py').read())`.
- For host commands, call `subprocess.run(..., capture_output=True, text=True)`
  inside the session so the output is a string you can parse.
- For hot reload, use `exec(open(...).read())` or `importlib.reload(module)`.
- If a cell fails, fix the function or data and retry in the same namespace.
- Split long workflows into small cells so successful prior state is retained.

## Async Rules

`fire` cells in one session execute serially. Use multiple sessions for
parallel execution. While a cell is running, `run`, `vars`, `complete`,
pickle and the fork snapshot return `409 busy` at once and the refused code
is discarded; `status`, `poll` and `int` keep working (`status` shows
`vars: null` meanwhile). One command is in flight per worker at a time; a
second one also gets `409 busy`. Long work goes through `fire`; write
progress and checkpoints to files.

For agent adapters, subscribe to `GET /events` (SSE) before firing and let the
completion event wake the conversation. Key each job by `X-Pythond-Session-Id`
(from the receipt) plus `cell_id`, persist that and the last event id outside
Python, and keep completions that arrive before their receipt until the
receipt lands.

Events: `ready` (initial cursor), `session_created` (session, session_id, pid),
`cell_done` (output tail of 64 KiB, `error`, `sync`, `code_head` = first 512
bytes of source) and `session_closed` (reason). The retained three carry a
`timestamp` in Unix seconds. `run` completions have `sync: true`
and correlate with the `X-Pythond-Cell-Id` header of the executed `run`;
their full output is the HTTP reply. `fire` /
`fork` completions have `sync: false`; their full output is at the receipt's
`Location` for 300 seconds after completion. Resume with `Last-Event-ID`; an
evicted cursor returns 410 (`reset` on an open stream), a changed epoch 409.
The log holds 256 events / 8 MiB for the daemon's lifetime. After a lost
reply, `poll` the job before deciding anything.

`pysh poll <session> <cell_id>` reads a specific cell.
`pysh poll <session>` reads the most recent cell, or `{"status":"idle"}` if
none exist.

`fork` cells run in a child process. New/changed variables are pickled back
and merged when done. Unpicklable objects (sockets, locks, CUDA tensors) are
skipped. A name merges back when the child reassigns it (`x = new_value`);
in-place mutation of an existing object stays in the child. A failed fork
leaves the parent namespace unchanged. Merge is last-writer-wins: a finished
fork can overwrite a variable that the parent changed while the fork was
running.

## Session Lifecycle

`pysh kill --all` (`POST /kill`) removes the current worker snapshot and returns
`{"killed": [...], "count": N}` (200, also when empty). Later creations and
same-name replacements are left alone. Each removed worker emits
`session_closed` with reason `killed`; daemon, token, epoch, SSE connections
and checkpoint history remain. `pysh kill` requires a name or explicit `--all`.

Sessions survive indefinitely while the daemon runs. If the daemon restarts,
sessions are lost -- replay from checkpoint history (see below). If a session
crashes, create a new one with the same name and replay.

## Checkpoints

Successful synchronous `run` cells are appended to
`~/.pythond/sessions/<name>/history.py`. Successful async `fire`/`fork` cells
are appended on completion. If a session dies, replay:
`pysh run <name> "exec(open(...).read())"`.

Like shell history under SSH, `history.py` holds executed source and the live
process holds assigned values until they are overwritten or the session is
killed. A secret pasted into a cell persists in both.

## Rules

- Output is plain text; read it as is.
- `pysh` is a function call: source in, captured output out.
- Source goes in the request body or an `@file`.
- Once a session exists, task state lives in it.
- Cells that may exceed 30s go through `fire` or `fork`.

## References

Bundled reference docs for specific integration patterns. Read the relevant
file when the task matches.

| File                           | When to read                                                                                                                                                                                                                                                                                                                      |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `references/cloakbrowser.md` | Task involves web scraping, browsing behind anti-bot protection, or interacting with sites that detect automation. Default: `launch()` in a pythond session -- one line, browser lives in the namespace. Advanced: cloakserve daemon for multi-session or Python-independent browser lifecycle. |
