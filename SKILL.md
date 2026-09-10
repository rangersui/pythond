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

Variables, connections, threads persist between calls.
No terminal parsing. No ANSI. exec() semantics.

`pysh` is your function-call surface into that persistent process: source code
in, captured output out. Do not treat it as a terminal transcript.

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

Do not reopen or recompute these on every turn unless the task requires a fresh
state. Read, patch, and inspect them by name in later cells.

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

If daemon is not running, start it first. One daemon manages all sessions.
Each session is an isolated subprocess with its own namespace.

`new` never replaces an existing session by default: it returns a conflict and
preserves live state, including under concurrent calls. Inspect `ls` / `status`
to decide whether it is yours to reuse. Only use `pysh new work --replace`
when discarding that session's variables, connections, and browser state is
explicitly intended. Never automatically escalate a conflict to replacement.

`pyctl start` is an alias for `pythond daemon`. Prefer `pythond daemon` in
agent workflows; use `pyctl` for stop/status.

## Core Commands

```bash
pysh new <name>              # create; refuse an existing name
pysh new <name> --replace    # explicitly discard existing state and replace
pysh run <name> "code"       # sync exec/eval, raw output
pysh fire <name> "code"      # async thread, shared namespace
pysh fork <name> "code"      # async process, POSIX only, killable
pysh poll <name> [cell_id]   # read async result
pysh attach <name>           # human line REPL, Ctrl-D detaches
pysh int <name>              # fire=best effort, fork=kill
pysh kill <name>             # terminate session
pysh ls                      # list sessions
pysh status <name>           # JSON health
pysh vars <name>             # JSON namespace names
pysh complete <name> "text"  # JSON completion candidates
pysh cp <src> <dst>          # copy pickled objects (scp syntax)
```

Session names are canonical lowercase: `a-z`, `0-9`, `_`, or `-`, 1-80
characters. Names with uppercase letters or dots are rejected. Windows device
names such as `con`, `nul`, `prn`, `aux`, `com1`, and `lpt1` are also rejected.

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
Unpicklable objects (sockets, locks, CUDA tensors) are skipped. In-place
mutations (`list.append`, `dict[k]=v`) won't merge -- use assignment
(`x = new_value`). Failed forks do not merge.

```bash
pysh fork work "results = expensive_search(params)"
pysh poll work <cell_id>
pysh int work                # kills fork'd process
```

`fire` cells in one session run serially under the session lock. Use multiple
sessions for real parallelism.

Long `run` cells: the command channel times out at 30s and the session must
then be killed. Anything that might exceed that goes through `fire` or `fork`.

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
# (42,)   (same connection, never closed)
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

Errors in `run` return traceback text (and exit code 1) but do not kill the
session.

## Remote Sessions

Remote access is ssh. The state lives in the remote daemon, not in the
connection, so one-shot calls are enough:

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

Python source goes in the request body, raw -- never JSON-escaped. Send
`Content-Length` in UTF-8 bytes; chunked request bodies are not supported.
Native adapters can use HTTP directly instead of spawning the CLI per call.

`new` returns **201 Created**, or **409** if the name exists. Only `?replace=1`
permits replacement. `fire` / `fork` return **202 Accepted** with a JSON receipt
and `Location: /poll/<name>?cell=<id>`. That header locates the status monitor
without parsing the body. Accept **2xx**, not only `200`. The HTTP API and SSE
wire protocol are documented in the README.

## Security Model

Treat pythond like SSH into a Python runtime.

- Not a sandbox: code runs with the daemon user's OS permissions.
- Once connected, a client has full access to all sessions; there is no
  per-session permission isolation.
- Local POSIX uses a unix socket with file permissions.
- Local Windows uses loopback TCP plus a bearer token in
  `%LOCALAPPDATA%\pythond\daemon.json`.
- The daemon never binds a non-loopback address; remote exposure is ssh's (or
  a reverse proxy's) job.

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

`fire` cells in one session execute serially under the session lock. Use
multiple sessions for parallel execution.

For agent adapters, prefer authenticated **`GET /events` (SSE)** over a polling
loop. Subscribe before firing; completion is pushed by the worker, not found by
a timer. Persist job ownership and the last processed event ID outside Python.
Match `session_id` + `cell_id` to the owning conversation; the HTTP receipt's
`X-Pythond-Session-Id` identifies the worker. Buffer completions that arrive
before the receipt, and deduplicate replayed event IDs. Never broadcast a
completion to whichever conversation happens to be active.

Resume with `Last-Event-ID`. A `ready` event establishes the initial cursor;
`cell_done` carries output/error, and `session_closed` reports worker loss.
Replay is bounded (256 events / 8 MiB), not durable across daemon restarts.
An expired cursor returns 410 (or `reset` on an open stream); a changed epoch
returns 409. Reconcile with `poll` where possible; never automatically rerun
code whose acceptance is uncertain. Closing the stream does not cancel work.

Completion output is a UTF-8 tail of at most 64 KiB. If `output_truncated` is
true, use the receipt's `Location` to fetch the full result while retained.
Poll results are eligible for eviction 300s after **completion**, not launch.

`pysh poll <session> <cell_id>` reads a specific cell.
`pysh poll <session>` reads the most recent cell, or `{"status":"idle"}` if
none exist.

`fork` cells run in a child process. New/changed variables are pickled back
and merged when done. Unpicklable objects (sockets, locks, CUDA tensors) are
skipped. In-place mutations (`list.append`, `dict[k]=v`) won't merge -- use
assignment (`x = new_value`). Failed forks do not merge. Merge is
last-writer-wins: a finished fork can overwrite a variable that the parent
changed while the fork was running.

## Session Lifecycle

Sessions survive indefinitely while the daemon runs. If the daemon restarts,
sessions are lost -- replay from checkpoint history (see below). If a session
crashes, create a new one with the same name and replay.

## Checkpoints

Successful synchronous `run` cells are appended to
`~/.pythond/sessions/<name>/history.py`. Successful async `fire`/`fork` cells
are appended on completion, without requiring `poll` or a subscriber.
If a session dies, replay:
`pysh run <name> "exec(open(...).read())"`.

Like shell history under SSH, pythond session history and live namespaces can
expose secrets. `history.py` may contain executed Python source. Variables
assigned in a session remain in that live Python process until overwritten or
the session is killed. Do not paste API keys, passwords, tokens, or other
secrets into cells unless you are willing for them to persist in that session
and its local files.

## Avoid

- Do not parse ANSI escape sequences; output is already clean text.
- Do not use `pysh` as a terminal transcript.
- Do not put Python source inside JSON; send source as the request body or load
  it from a file.
- Do not move task state back into the host shell once a session exists.
- Do not run cells that may exceed 30s through `run`; use `fire`/`fork`.

## References

Bundled reference docs for specific integration patterns. Read the relevant
file when the task matches.

| File                           | When to read                                                                                                                                                                                                                                                                                                                      |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `references/cloakbrowser.md` | Task involves web scraping, browsing behind anti-bot protection, or interacting with sites that detect automation. Default: `launch()` in a pythond session -- one line, browser lives in the namespace. Advanced: cloakserve daemon for multi-session or Python-independent browser lifecycle. |
