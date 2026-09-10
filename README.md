# pythond

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

Transport is borrowed, never built. There is no WebSocket stack, no TLS stack,
no PTY bridge, and no remote proxy in this codebase -- SSH, reverse proxies,
and your terminal already exist.

## Stateful first

Most command tools are stateless: fork, run, die. That is simple for humans
but wasteful for agents, which repeat imports, reopen connections, and rebuild
intermediate data on every call.

pythond flips the default. The process is the workspace. Things that stay
alive between calls:

- variables, imports, compiled regexes, parsed configs, DataFrames, models,
- database handles, HTTP sessions, sockets, SSH tunnels, browser sessions,
- local servers, file watchers, background threads,
- live control-plane state: feature flags, rate limits, blocked IP sets.

**Connection != state.** The HTTP request is transport; the namespace is state.
Every `pysh` call is a fresh connection to the same live process.

## Commands

```
pysh new <name>              create; refuse an existing name
pysh new <name> --replace    explicitly discard and replace existing session state
pysh run <name> "code"       sync exec -> raw output
pysh run <name> @task.py     post a file's contents as the cell (curl syntax)
pysh fire <name> "code"      async thread -> shares namespace, can't kill C
pysh fork <name> "code"      async process (POSIX only) -> killable, pickles vars back
pysh poll <name> [cell_id]   check async result
pysh int <name>              best-effort interrupt (fire=async exc, fork=SIGKILL)
pysh kill <name>             terminate session
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

**Safe by default:** `new work` returns a conflict if `work` already exists;
its process, variables, connections, and browser state are left untouched.
Concurrent creators cannot overwrite each other: one creates, the others
receive `409`. Inspect `ls` / `status` and decide whether the session is yours
to reuse. Do not automatically retry a conflict with `--replace`.

Replacement requires explicit intent: `pysh new work --replace` (HTTP:
`POST /new/work?replace=1`). This deliberately changes the old replace-by-default
behavior. **Design rule: choose the default with the lower cost of being wrong;
reserve destructive options for callers who explicitly intend them.**

## fire vs fork

```
pysh fire work "model = train(data)"    # thread -- shares namespace
pysh fork work "model = train(data)"    # process -- killable, pickles back
```

**fire** (`threading.Thread`): shares the session namespace -- variables set by
fire'd code are immediately visible to later calls. Exec is serialized (one
cell at a time): async to the client, not parallel. Cannot be force-killed
when stuck in C code; `pysh kill` is the escape.

**fork** (`os.fork()`, POSIX only): runs in a child process with a COW copy of
the namespace. `pysh int` kills it (SIGKILL). New/changed variables are
pickled back and merged; unpicklable objects (sockets, locks, CUDA tensors)
are skipped and reported. In-place mutations (`list.append`, `dict[k]=v`)
won't merge -- use assignment. Merge is last-writer-wins.

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

The daemon never binds a non-loopback address. There is no network listener
to harden.

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
POST /stop                    stop daemon
```

`404` no such session, `409` existing name or broken session channel, `401` bad token.
Python source goes in the request body, raw -- never JSON-escaped. Send an
explicit `Content-Length` in **UTF-8 bytes**, not characters; chunked request
bodies are rejected with `411`.

Responses advertise `X-Pythond-Protocol: 2` so clients can distinguish this
safe-new/event API from older replace-by-default daemons, independently of the
package version. Do not mix newly installed workers with a running old daemon.

`new` returns `201 Created`, a text confirmation, and `Location: /status/<name>`.
`fire` / `fork` return **receipts**, not execution results:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json
Location: /poll/work?cell=abc123
X-Pythond-Session-Id: <worker-incarnation>

{"cell_id": "abc123", "status": "fired"}
```

`Location` points to the status monitor, as recommended for asynchronous
responses by [RFC 9110 section 15.3.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.3).
The client can find it without parsing the body. Acceptance is not a promise
of success: Python errors arrive in the completion event / poll result.
Treat **2xx** as HTTP success, not only `200`; `run` and `kill` retain their
`200` text responses. New and session-command responses also identify the
actual worker with `X-Pythond-Session-Id`, so reusing a name cannot alias an old job.

### Completion events (no polling loop)

Subscribe **before** submitting work:

```bash
curl -N --unix-socket $XDG_RUNTIME_DIR/pythond/pythond.sock http://pythond/events
# TCP: use the same Authorization: Bearer <token> header as other endpoints.
```

The worker sends completion frames over its existing pipe; the daemon separates
them from command replies, appends to a bounded replay log, and wakes SSE
subscribers with a condition notification. It does not periodically scan cells
or call `poll`. Fifteen-second SSE comments are connection heartbeats only.

Events use JSON `data` and an opaque `id` (`<daemon-epoch>:<sequence>`):

- `ready`: establishes the starting cursor, including before any job completes.
  The same value is in `X-Pythond-Event-Cursor`. A new subscription without a
  cursor observes future events only.
- `cell_done`: `session`, `session_id` (worker incarnation), `cell_id`,
  `status: "done"`, boolean `error`, `output`, `output_bytes`, `output_truncated`.
  Fork events also include `merged_count` / `skipped_count`; `poll` has the full lists.
- `session_closed`: `session`, `session_id`, and `reason` (`killed`, `replaced`,
  or `exited`). Pending jobs in that incarnation may have no completion result.

Reconnect using `Last-Event-ID: <last-processed-id>` (or `?since=<id>`; the
header wins). Retained events **after** that cursor are replayed; reading does
not consume them. Deduplicate by event ID. An initial invalid cursor returns
JSON with `400`; a changed daemon epoch or a future cursor returns `409`;
an evicted cursor returns `410`. If a connected subscriber falls behind the
retention window, it receives `event: reset` with the error and current cursor,
then the stream closes. Do not silently skip that gap.

Limits and lifecycle:

- At most **256 events / 8 MiB** are retained, whichever limit is reached first.
  This is daemon-lifetime memory, **not durable delivery or exactly-once execution**.
- Each completion includes at most the last **64 KiB** of UTF-8 output (possibly
  less with a smaller worker-response limit). `output_truncated` tells clients
  to fetch the full result via the receipt's `Location` if needed. Large results
  remain subject to the existing worker-response limit.
- Poll results become eligible for eviction **300 seconds after completion**, not
  300 seconds after launch. The event log has its own independent size limits.
- Closing a subscription or reloading a client does **not** interrupt Python or
  kill sessions. Daemon shutdown closes streams; a restart changes the epoch
  and loses sessions. TCP clients must reload the daemon token after restart.
- Subscribers can see all sessions, under the same auth boundary as execution.
  Agent adapters must persist their own job ownership and cursor, match worker
  incarnation + cell ID, buffer early completions that race the HTTP receipt,
  and deliver only to the owning conversation. A gap may require `poll` to
  reconcile; **never blindly resubmit code after a lost response**.

## Objects move as pickles

`run` moves source code; `/pickle` moves live objects. It is the fork
merge-back mechanism, generalized into an import/export surface -- and POSTing
a pickle is arbitrary code loading by design, the same trust boundary as
`/run`.

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
at a time -- the state lives in the remote daemon, not in the connection:

```bash
ssh server pysh run work "x = 42"
ssh server pysh run work "x + 1"     # -> 43
```

Latency bothering you? That is what `ControlMaster` is for -- ssh holds one
connection open so each call skips the handshake:

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

Need a TLS endpoint anyway? Terminate it with nginx or caddy in front of the
loopback port. pythond does not ship a TLS stack.

## attach

`pysh attach work` is a client-side line REPL: readline history and tab
completion live in the client, every complete block runs as one cell in the
shared namespace. Ctrl-D detaches; the session stays alive (`pysh kill` ends
it). It is line-oriented, not a PTY -- for full-screen terminal programs run a
real terminal; for everything stateful, the namespace is the point.

## Auto-checkpoint

Successful synchronous `run` cells are appended to
`~/.pythond/sessions/<name>/history.py`. Successful async `fire`/`fork` cells
are appended when the daemon receives completion, even without a subscriber
or a `poll` request. Errors are never checkpointed.

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

- **Not a sandbox**: code runs with the daemon user's OS permissions.
- Once connected, a client has full access to all sessions -- the same as a
  login shell.
- Local POSIX: unix socket, mode `0600` -- filesystem permissions are the auth.
- Local Windows: loopback TCP plus a bearer token readable only by the user.
- Remote: ssh's problem, on purpose. pythond has no network attack surface of
  its own.

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
- The last expression auto-prints -- no `print()` tax.
- Complex code (quotes, f-strings, SQL): write a file, then post it --
  `pysh run work @/tmp/task.py` (or `curl --data-binary @task.py`). The file
  is transport; the namespace is the workspace. `exec(open(...).read())`
  inside a cell still covers files that live where the session runs.
- Hot reload: `importlib.reload(m)` or `exec(open("module.py").read())`.
- Host commands: `subprocess.run(..., capture_output=True, text=True)` from
  inside the session.
- If step 3 of a workflow fails, fix step 3 -- steps 1 and 2 still exist in
  memory.

## Tests

```bash
python -B -m py_compile pythond.py test_pythond.py
python -B test_pythond.py
```

Tests use temporary homes; integration daemons use isolated sockets/ports and
metadata, with graceful cleanup. Coverage includes safe concurrent creation,
201/202 receipts and monitor locations, fast completion/ACK races, Unicode,
no-poll notifications, replay and gaps, authentication, and subscriber shutdown.
Fork tests run on POSIX; the CI matrix covers Linux, macOS, and Windows.

## License

MIT
