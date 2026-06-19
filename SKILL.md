# agent-tty — persistent REPL for AI agents, shared live terminal for humans

## Install

```bash
pip install agent-tty            # → k, km, agent-tty in PATH
```

Or without pip: `./scripts/k` works immediately (dev shim, no install needed).

## When to use

Use k when the process must keep memory between agent turns: live connections, imported modules, cwd/env, running servers, SSH sessions, browser/CDP sockets, or debugger state. The session is a real tmux TTY — the human can watch (`k watch`), interrupt (`k int`), or take over (`tmux attach`) without losing state. Use km for callback-style completion of long cells. Use k poll only as a simple fallback for scripts or agent runtimes without a monitor/interrupt path. Use bash_tool for one-shot commands.

## First Steps

```bash
k new work bash
k run -j work "echo hello"
# {"cell_id":"...","status":"done","output":"hello"}

k new py python3 -i
k run -j py "print(42)"

k new dbg "gdb -q ./app" --prompt="(gdb)"
k run -j dbg "break main"
```

Zero config for bash/python. `--prompt` for exact match or custom hook.

## Commands

```
k new    <session> [cmd...] [--prompt="x"]     spawn session (default: bash)
k new    <session> <cmd> --prompt=./hook        hook mode
k fire   [-t N] [session] <code>               async fire (default 300s)
k poll   [session] [cell_id]                   poll result (O(1))
k run    [-j] [-t N] [session] <code>          sync (default 30s)
k await  ...                                   alias for run
k notify [session] <message>                   notification (direct to log)
k int    [session]                             ctrl-c (+ re-frame in repeat mode)
k kill   <session>                             kill + cleanup
k ls                                           list sessions
k status [session]                             health check
k watch  [session]                             live filtered view
k history [-n N] [session]                     last N×5 lines (default 5)
```

Session resolves: explicit arg > K_SESSION env > auto-detect (single session).

## Architecture

```
k new   -> spawn tmux (width 10000) -> start pipe-pane
k fire  -> acquire lock -> paste-buffer (code) + send-keys (frame enters) -> bg watcher
k poll  -> check result file (O(1)) -> return output or "running"
k run   -> acquire lock -> send code + run stream processor inline -> release
```

**Frame detection** has three modes via `--prompt`:

| --prompt= | mode | how it works |
|-----------|------|-------------|
| *(not set)* | repeat | 5 empty Enters → detect 5 identical lines |
| `"string"` | exact | match prompt string exactly |
| `./file` | hook | stdin lines → hook exit = frame end |

**Stream processor**: state machine (ECHOING -> OUTPUT -> DONE). Tails the log in real-time. Classifies each line as it arrives. Writes result file when done.

**Background watcher**: fire spawns a Python subprocess per cell. It runs the stream processor and writes the result. poll reads the result file. O(1).

## Frame Detection

### Default: repeated prompt lines (zero config)

```
k sends: "echo hello" via paste-buffer + 5 empty Enters via send-keys
log shows:
  echo hello              <- echo (skipped by echo_count)
  hello                   <- output (collected)
  root@vm:/#              <- prompt 1 (from command)
  root@vm:/#              <- prompt 2 (from Enter)
  root@vm:/#              <- prompt 3 (from Enter)
  root@vm:/#              <- prompt 4 (from Enter)
  root@vm:/#              <- prompt 5 (from Enter)
                           <- 5 identical = DONE
```

Works after cd, venv activation, prompt theme change.

### Exact match: `--prompt="(gdb)"`

For REPLs where empty Enter has side effects (gdb repeats last command).

### Hook: `--prompt=./detect.py`

k feeds ANSI-stripped lines to hook's stdin. Hook exits when frame ends. k pops the last line (= the boundary). Hook paths must include a path separator (`/`, or `\` on Windows). The path is canonicalised to absolute at `k new` time; the hook file must exist and be executable (`chmod +x`).

```python
#!/usr/bin/env python3
import sys, re
while True:
    line = sys.stdin.readline()
    if not line: break
    if re.match(r'.*[#$]\s*$', line.strip()):
        sys.exit(0)
```

In hook mode, k does NOT filter `...` continuation prompts — the hook user takes full control of output.

## Sync Mode

```bash
k run -j work "echo hello"
# {"cell_id":"...","status":"done","output":"hello"}
```

k does not classify command output. If the REPL returned to its prompt, status is "done" regardless of whether the command succeeded or failed. Agent reads output and decides.

## Async Mode

```bash
k fire work "make build"
# {"cell_id":"abc123","status":"fired"}

k poll work
# {"cell_id":"abc123","status":"running"}

k poll work
# {"cell_id":"abc123","status":"done","output":"..."}
```

poll is O(1): checks if the background watcher wrote a result file.

## Timeout

On timeout, the lock is NOT released — the REPL command may still be running. Subsequent polls return `status: "timeout"` with a hint to use `k int` or `k kill`. Only explicit recovery releases the lock.

```
k fire work "make build -j8"   # takes too long
k poll work                    # → {"status": "timeout", ...}
k poll work                    # → {"status": "timeout", "output": "use k int or k kill"}
k int work                     # sends Ctrl-C, writes result, releases lock
k poll work                    # → {"status": "error", "output": "interrupted"}
```

## ctrl-c

`k int` sends SIGINT, kills any bg watcher, writes an `error`/`interrupted` result for the old cell, and releases the lock. In repeat mode (no `--prompt`), it also re-sends frame enters because SIGINT clears readline's typeahead buffer. In prompt/hook mode, no extra Enters are sent (they could have side effects).

## JSON Schema

```
fired:        {"cell_id": "...", "status": "fired"}
running:      {"cell_id": "...", "status": "running"}
done:         {"cell_id": "...", "status": "done", "output": "..."}
timeout:      {"cell_id": "...", "status": "timeout", "output": ""}
timeout(2+):  {"cell_id": "...", "status": "timeout", "output": "use k int or k kill"}
error:        {"status": "error", "output": "..."}
cell error:   {"cell_id": "...", "status": "error", "output": "..."}
```

Errors without `cell_id`: `no session 'x'`, `active cell 'x'`, `pipe failed: ...`, `send failed: ...`, `no active cell on 'x'`.
Errors with `cell_id`: `interrupted`, `unknown cell`, `watcher died`, `lock update failed; use k int or k kill`, `interrupt failed; use k kill`.

## Safety Invariants

- One cell per session (O_EXCL lock). Second fire/run refused.
- Lock acquired BEFORE send — rejected fire/run never touches the REPL.
- Timeout keeps lock — prevents new commands from mixing with a potentially still-running REPL command. Only `k int` or `k kill` releases.
- Lock stores bg watcher PID. poll detects orphaned watchers (crash/OOM) via `os.kill(pid, 0)` (POSIX-portable).
- Code sent via per-session named paste-buffer (no cross-session collision).
- tmux width 10000: prevents line wrapping that would skew echo_count.
- Session names validated: `[A-Za-z0-9_.-]+`, no path traversal.
- Pipe-pane restarted on every fire/run (idempotent, recovers dead pipes).
- Result files written atomically (tmp + fsync + `os.replace`). poll never reads partial JSON.
- k does not classify output. "done" = prompt appeared, not "command succeeded".

## Metadata on Disk

```
/tmp/k_cells/<session>/
  _session.json       {name} or {name, prompt}
  _lock.json          {cell_id, log_offset, echo_count, bg_pid, timed_out?}
  _output.log         pipe-pane stream (append-only)
  <cell_id>_result.json  stream processor output (deleted after poll)
```

## Known Limitations

**Frame collision (repeat mode)**: if output contains 5+ consecutive identical non-empty lines, the stream processor falsely detects completion. Extremely rare — 5 identical lines = zero information entropy.

**echo_count heuristic**: assumes 1 sent line = 1 echoed line. Mitigated by tmux width 10000 (no wrapping) and continuation prompt filtering.

**Hook mode**: no `...` filtering (user takes full control). Hook paths must include a path separator to distinguish them from string prompts.

**Python 3.13+ `_pyrepl`**: The new Python REPL auto-indents pasted code, doubling indentation on multi-line blocks. Workaround: `k new py "env PYTHON_BASIC_REPL=1 python3 -i"`. Single-line code is unaffected.

## Python Multi-line

Multi-line blocks work naturally. The trailing newline from shell quoting closes Python blocks:

```bash
k run -j py "
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n-1)
"
k run -j py "print(factorial(10))"
# 3628800
```

## Language Notes

k is REPL-agnostic. Any program with a readline prompt works:

```bash
k new work bash                                # zero config (repeat mode)
k new py python3 -i                            # zero config (repeat mode)
k new dbg "gdb -q ./app" --prompt="(gdb)"      # exact match
k new custom ./repl --prompt=./detect.py        # hook
k new redis redis-cli                          # zero config
k new remote "ssh prod"                        # zero config
```

## km — event monitor

Callback-style completion for persistent TTY cells. Tails the session log via pipe-pane. Each stdout line is one JSON event.

Designed for **Claude Code's Monitor tool** — each stdout line becomes an agent interrupt. Other frameworks can spawn `km` as a subprocess and read stdout.

```
km <session> [cell_id] [-1]
```

`-1` exits after first completion (one-shot `.then()`).

### Persistent state plus monitor

k is the stateful terminal. km is the callback channel for long-running cells. Background task support alone is not enough when the process state matters; km lets the persistent TTY keep running and wakes the agent when the cell finishes. Poll loops waste tokens and add latency — every `k poll` is a tool call that returns "running" and accomplishes nothing.

```bash
# poll loop: burns a tool call every N seconds
# k poll → "running" → k poll → "running" → k poll → "done"

# km: one tool call, block until done
km work -1
# {"cell_id": "...", "status": "done", "ts": "..."}
```

Use `km -1` when the task takes longer than a few seconds — fire, start monitor, get interrupted on completion. Use `k poll` for quick checks, shell scripts, or agent frameworks without a monitor/interrupt path.

### Continuous mode

Without `-1`, `km` streams all events indefinitely. For multi-cell orchestration where the agent reacts to each completion.

### Events

```
fired:   {"cell_id": "...", "session": "...", "status": "fired",  "ts": "..."}
done:    {"cell_id": "...", "session": "...", "status": "done",   "ts": "..."}
notify:  {"session": "...", "status": "notify", "from": "...", "message": "...", "ts": "..."}
closed:  {"session": "...", "status": "closed", "ts": "..."}
error:   {"session": "...", "status": "error",  "message": "...", "ts": "..."}
```

## Testing

```bash
python tests/test_contracts.py      # static code contracts, no tmux
python tests/test_docs.py           # README/SKILL drift, no tmux
bash tests/test.sh                  # 34 tests (32 without gdb), runtime smoke suite
python tests/test_regressions.py    # targeted audit regressions
python tests/run_all.py             # all suites
bash tests/test.sh ./scripts/k       # custom k path
```
