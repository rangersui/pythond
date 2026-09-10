# pythond

<img src="pythond_tray.png" width="128" alt="pythond mascot">

**Persistent Python sessions. Code in, result out.**

```
pip install pythond        # zero dependencies
```

```bash
pythond daemon             # start daemon (foreground)
pysh new work              # create a session
pysh run work "x = 42"
pysh run work "x + 1"      # -> 43  (state persists)
```

## The whole idea

```
ns = {}
while True:
    code = receive()
    exec(code, ns)         # ns stays alive -- variables survive
    send(captured_stdout)
```

Everything pythond adds is that loop plus delivery:

1. **Thread-safe stdout capture** -- concurrent cells don't interleave output.
2. **REPL semantics** -- the last expression auto-prints, like `>>>`.
3. **Named sessions** -- one plain subprocess per session, isolated namespaces.
4. **fire / fork** -- async cells: thread (shares the namespace) or process
   (killable, POSIX).
5. **Local HTTP** -- so one-shot CLI calls reach the live process.

Transport is borrowed: ssh carries remote calls, a reverse proxy terminates
TLS, your terminal runs `attach`. pythond itself listens on a local socket.

## Stateful first

The process is the workspace. Things that stay alive between calls:

- variables, imports, compiled regexes, parsed configs, DataFrames, models,
- database handles, HTTP sessions, sockets, SSH tunnels, browser sessions,
- local servers, file watchers, background threads,
- live control-plane state: feature flags, rate limits, blocked IP sets.

**Connection != state.** The HTTP request is transport; the namespace is state.
Every `pysh` call is a fresh connection to the same live process.

## Commands

```
pysh new <name>              create session (409 if the name exists)
pysh new <name> --replace    replace an existing session
pysh run <name> "code"       sync exec -> raw output
pysh run <name> @task.py     post a file's contents as the cell (curl syntax)
pysh fire <name> "code"      async thread -> shares namespace, can't kill C
pysh fork <name> "code"      async process (POSIX only) -> killable, pickles vars back
pysh poll <name> [cell_id]   check async result
pysh int <name>              best-effort interrupt (fire=async exc, fork=SIGKILL)
pysh kill <name>             terminate session
pysh kill --all              terminate current sessions, keep daemon running
pysh ls                      list sessions
pysh status <name>           session health (JSON)
pysh vars <name>             namespace names (JSON)
pysh complete <name> "text"  tab completion (JSON)
pysh attach <name>           line REPL into the session (Ctrl-D detaches)
pysh cp <src> <dst>          copy pickled objects (scp syntax, see below)

pyctl start [--show-token]   start daemon in foreground
pyctl stop                   stop daemon
pyctl status                 daemon liveness
```

Session names are canonical lowercase: `a-z`, `0-9`, `_`, or `-`, 1-80
characters. Windows device names (`con`, `nul`, ...) are rejected.

`new work` creates the session and returns `201`. If `work` already exists it
returns `409` and leaves the existing process, variables and connections as
they are; under concurrent creators exactly one wins. `pysh new work --replace`
(HTTP `POST /new/work?replace=1`) discards the existing session and creates a
fresh one.

## fire vs fork

```
pysh fire work "model = train(data)"    # thread -- shares namespace
pysh fork work "model = train(data)"    # process -- killable, pickles back
```

**fire** (`threading.Thread`): shares the session namespace -- variables set by
fire'd code are immediately visible to later calls. Cells run one at a time,
so fire is async to the client and serial in the session. A cell stuck in C
code ends with `pysh kill`.

**fork** (`os.fork()`, POSIX only): runs in a child process with a COW copy of
the namespace. `pysh int` kills it (SIGKILL). New/changed variables are
pickled back and merged; unpicklable objects (sockets, locks, CUDA tensors)
are skipped and reported. A name merges back when the child reassigns it
(`x = new_value`); in-place mutation of an existing object stays in the child.
Merge is last-writer-wins.

```json
// poll after fork completes
{"cell_id": "abc", "status": "done", "output": "...",
 "merged": ["model", "results"], "skipped": ["db_conn"]}
```

## Transport

| Mode | Endpoint | Auth |
|------|----------|------|
| Local POSIX | HTTP over `$XDG_RUNTIME_DIR/pythond/pythond.sock` | socket permissions |
| Local Windows | `http://127.0.0.1:7984` | bearer token in `%LOCALAPPDATA%\pythond\daemon.json` |
| Remote | none built in | ssh (below) |

The daemon binds only the unix socket or 127.0.0.1; ssh or a reverse proxy
carries remote access.

### HTTP API

`pysh` speaks plain HTTP; so does curl:

```bash
curl --unix-socket $XDG_RUNTIME_DIR/pythond/pythond.sock \
     --data-binary '1 + 1' http://pythond/run/work        # -> 2

curl --unix-socket ... http://pythond/ls
curl --unix-socket ... --data-binary @task.py http://pythond/run/work
```

```
GET  /ls                      text listing
POST /new/<name>              201 Created; 409 if name exists
POST /new/<name>?replace=1    201 Created; explicitly discard and replace
POST /run/<name>    body=code 200 + raw output; X-Pythond-Exec-Error: 1 on traceback
POST /fire/<name>   body=code 202 Accepted; JSON cell_id + Location: /poll/...
POST /fork/<name>   body=code 202 Accepted; JSON cell_id + Location: /poll/...
GET  /poll/<name>[?cell=ID]   200 + JSON cell result
GET  /events                  200 + SSE completion stream (replay via Last-Event-ID)
GET  /status/<name>           JSON health
GET  /vars/<name>             JSON namespace names
POST /complete/<name> body    JSON completion matches
POST /int/<name>              JSON interrupt report
GET  /pickle/<name>[/<var>]   pickled var (or whole picklable namespace dict)
POST /pickle/<name>[/<var>]   unpickle body into var (or merge a pickled dict)
POST /kill/<name>             kill session
POST /kill                    kill current sessions; JSON killed names + count
POST /stop                    stop daemon
```

Status codes: `2xx` success (`200` result, `201` created, `202` accepted),
`400` bad name or parameter, `401` bad token, `404` no such session, `409`
name exists / session busy / channel out of sync, `410` expired event cursor,
`411` chunked body. Python source goes in the request body, raw.
`Content-Length` counts UTF-8 bytes.

Every response carries `X-Pythond-Protocol: 2`. Once a request has resolved
its session, the response carries `X-Pythond-Session-Id`, the id of the worker
process that handled it (for `kill`, the worker that was removed); a `404`
for a missing session has none. A `run` that executed also carries
`X-Pythond-Cell-Id`.

`new` returns `201`, a text confirmation and `Location: /status/<name>`.
`fire` / `fork` return a receipt:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json
Location: /poll/work?cell=abc123
X-Pythond-Session-Id: <worker id>

{"cell_id": "abc123", "status": "fired"}
```

`Location` is where the result appears
([RFC 9110 section 15.3.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.3)).
Python errors arrive in the poll result and in the completion event.

### Kill all

`pysh kill --all` sends `POST /kill`: `200` JSON
`{"killed": ["work", "train"], "count": 2}`. An empty daemon returns
`{"killed": [], "count": 0}`. Each removed worker emits `session_closed`
with reason `killed`; the daemon, token, event epoch, SSE connections and
checkpoint files remain. The operation snapshots worker instances, so later
creations (including same-name replacements) are left alone. CLI requires
exactly one session name or `--all`.

### Busy

One cell runs at a time per session. `fire` queues behind the running cell.
`run`, `vars`, `complete`, `/pickle` and the fork snapshot return `409 busy`
right away while a cell is running; the code in a refused request is discarded
and the session stays healthy. `status`, `poll` and `int` work during a running
cell (`status` reports `vars: null` while the namespace is in use). One command
is in flight per worker at a time; a second command arriving meanwhile also
gets `409 busy`. `run` waits 30 seconds for the reply; a cell that runs
longer keeps running, but the reply timeout (like a malformed or oversized
reply) leaves the channel out of sync, and the way on is `kill` then `new`.
Longer work goes through `fire`.

### Completion events

```bash
curl -N --unix-socket $XDG_RUNTIME_DIR/pythond/pythond.sock http://pythond/events
# TCP: same Authorization: Bearer <token> header as the other endpoints.
```

The worker pushes a frame over its pipe when a cell completes; the daemon
appends it to a replay log and wakes every subscriber. A comment line every
15 seconds keeps the connection alive. Each event has a JSON `data` body and,
except `reset`, an `id` (`<daemon-epoch>:<sequence>`); the retained ones
(`session_created`, `cell_done`, `session_closed`) also carry a `timestamp`
(Unix seconds at publication). Types:

- `ready`: the starting cursor, also in `X-Pythond-Event-Cursor`. Subscribing
  without a cursor starts from now.
- `session_created`: `session`, `session_id`, `pid`. Sent once the worker
  owns the name.
- `cell_done`: `session`, `session_id`, `cell_id`, `status: "done"`, `error`,
  `output` (last 64 KiB), `output_bytes`, `output_truncated`, `sync`,
  `code_head` (first 512 bytes of the source). `sync` is true for `run`
  (correlate with `X-Pythond-Cell-Id`; the full output is in the HTTP reply)
  and false for `fire` / `fork` (full output at the receipt's `Location`).
  Fork adds `merged_count` / `skipped_count`.
- `session_closed`: `session`, `session_id`, `reason` (`killed`, `replaced`,
  `exited`).

Reconnect with `Last-Event-ID: <last id>` (or `?since=<id>`) to replay the
events after that cursor; order by cursor, deduplicate by id. A cursor from
another daemon epoch or from the future gets `409`, an evicted one `410`; a
live stream that falls behind gets `event: reset` with the current cursor and
closes. The log keeps 256 events / 8 MiB (`PYTHOND_MAX_EVENTS`,
`PYTHOND_MAX_EVENT_BYTES`) for the daemon's lifetime; poll results stay for
300 seconds after completion. Closing a subscription leaves Python running.
Subscribers see every session; `code_head` and `output` share the auth
boundary of execution.

## Objects move as pickles

`run` moves source code; `/pickle` moves live objects. It is the fork
merge-back mechanism, generalized into an import/export surface. Unpickling
runs code, so POSTing a pickle has the same trust boundary as `/run`.

`pysh cp` gives it scp syntax. A side is `session:var`, `session:` (the whole
picklable namespace), or a file path:

```bash
pysh cp work:df df.pkl          # session -> file
pysh cp df.pkl gpu:df           # file -> session
pysh cp work:model gpu:model    # session -> session
pysh cp work: backup:           # clone the picklable namespace
```

Unpicklable values (sockets, locks, modules) are skipped and reported
(`X-Pythond-Skipped` header; `pysh cp` prints a warning). Or speak it raw:

```bash
curl --unix-socket ... http://pythond/pickle/work/df -o df.pkl
curl --unix-socket ... --data-binary @df.pkl http://pythond/pickle/gpu/df
```

## Remote = ssh

A human would `ssh server` and run Python. An agent does the same, one shot
at a time; the state lives in the remote daemon:

```bash
ssh server pysh run work "x = 42"
ssh server pysh run work "x + 1"     # -> 43
```

ssh `ControlMaster` holds one connection open so each call skips the
handshake:

```
# ~/.ssh/config
Host server
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

Interactive access to a remote session:

```bash
ssh -t server pysh attach work
```

Tunneled access (when the client machine should run `pysh` locally):

```bash
ssh -L 7984:127.0.0.1:7984 server            # or -L for the unix socket
export PYTHOND_HOST=127.0.0.1:7984 PYTHOND_TOKEN=<remote-token>
pysh run work "x"
```

For a TLS endpoint, put nginx or caddy in front of the loopback port.

## attach

`pysh attach work` is a client-side line REPL: readline history and tab
completion live in the client, every complete block runs as one cell in the
shared namespace. Ctrl-D detaches; the session stays alive (`pysh kill` ends
it). It is line-oriented; full-screen terminal programs need a real terminal.

## Auto-checkpoint

Successful synchronous `run` cells are appended to
`~/.pythond/sessions/<name>/history.py`. Successful async `fire`/`fork` cells
are appended when the daemon receives completion, even without a subscriber
or a `poll` request. Only successful cells are checkpointed.

```bash
# Process died? Replay:
pysh new work
pysh run work "exec(open(os.path.expanduser('~/.pythond/sessions/work/history.py')).read())"
```

Like shell history, `history.py` can contain secrets you paste into cells;
variables live in the session process until overwritten or killed. Treat both
accordingly.

## Security

Treat pythond like SSH into a Python runtime:

- Code runs with the daemon user's OS permissions.
- A connected client has full access to all sessions, the same as a login
  shell.
- Local POSIX: unix socket, mode `0600`; filesystem permissions are the auth.
- Local Windows: loopback TCP plus a bearer token readable only by the user.
- Remote: ssh.

## Environment knobs

| Variable | Default | Purpose |
|----------|---------|---------|
| `PYTHOND_SOCK` | runtime dir | POSIX unix socket path override |
| `PYTHOND_PORT` | `7984` | local TCP port (Windows / tunnels) |
| `PYTHOND_HOST` | unset | client target `HOST[:PORT]` for tunneled daemons |
| `PYTHOND_TOKEN` | from `daemon.json` | client bearer token override |
| `PYTHOND_MAX_SESSIONS` | `128` | max live sessions per daemon |
| `PYTHOND_MAX_BODY` | `16777216` | max HTTP request body, bytes |
| `PYTHOND_MAX_WORKER_RESPONSE` | `16777216` | max worker response line, bytes |
| `PYTHOND_MAX_EVENTS` | `256` | max retained SSE events (at least 1) |
| `PYTHOND_MAX_EVENT_BYTES` | `8388608` | max retained SSE frame bytes (at least 1) |

`PYTHOND_INTERNAL_WORKER` is reserved for daemon-spawned workers.

## REPL patterns

- Import once; call shorter names in later cells.
- The last expression auto-prints.
- Complex code (quotes, f-strings, SQL): write a file, then post it --
  `pysh run work @/tmp/task.py` (or `curl --data-binary @task.py`). The file
  is transport; the namespace is the workspace. `exec(open(...).read())`
  inside a cell still covers files that live where the session runs.
- Hot reload: `importlib.reload(m)` or `exec(open("module.py").read())`.
- Host commands: `subprocess.run(..., capture_output=True, text=True)` from
  inside the session.
- If step 3 of a workflow fails, fix step 3 -- steps 1 and 2 still exist in
  memory.

## Desktop tray

```bash
pip install "pythond[tray]"    # adds pystray, Pillow and psutil
pythond-tray                   # or: python -m pythond_tray
```

At launch the tray starts the daemon if nothing is listening locally
(`pythond-tray --no-start` only observes). Its icon is green while sessions
exist, gray for an empty daemon, red while disconnected, and a yellow spinner
while a daemon is starting. Right-click gives Start daemon (while offline),
Kill per session, Kill all sessions, Exit (stops the daemon and the tray) and
Quit tray (the daemon keeps running). The menu lists each session with pid,
age and the RSS / CPU of its process tree, sampled when the menu opens (CPU is
averaged between samples, 100% = one core, first sample `n/a`; a `+` after
RSS means part of the tree was unreadable), plus the last five activities.
Existing sessions come from `/ls` at connect; creation time and worker id
fill in from `session_created` events. With `PYTHOND_HOST` set the tray
observes the remote daemon and skips process sampling. On Windows the tray is
per-monitor DPI aware and renders the icon at the taskbar's size.
`import pythond` stays free of GUI imports.

## Tests

```bash
python -B -m py_compile pythond.py test_pythond.py
python -B test_pythond.py
```

The suite runs in a temporary home with private sockets, ports and metadata.
CI runs it on Linux, macOS and Windows; fork tests run on POSIX.

## License

MIT
