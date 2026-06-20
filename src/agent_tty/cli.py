#!/usr/bin/env python3
"""
agent-tty / k -- persistent terminal sessions for AI agents

Usage:
  k new    <session> [cmd...] [--prompt="x"]  spawn session (default: bash)
  k new    <session> <cmd> --prompt=./hook     hook mode (custom frame detect)
  k fire   [-t N] [session] <code>             async fire (default 300s)
  k poll   [session] [cell_id]                poll result (O(1))
  k run    [-j] [-t N] [session] <code>       sync (default 30s)
  k await  ...                                alias for run
  k notify [session] <message>                notification
  k int    [session]                          ctrl-c
  k kill   <session>                          kill + cleanup
  k ls                                        list sessions
  k status [session]                          health check
  k watch  [session]                          live filtered view
  k history [-n N] [session]                  last N*5 lines (default 5)

Session resolves: explicit arg > K_SESSION env > auto-detect.

Frame detection (--prompt):
  not set      -> 5 empty Enters, detect repeated prompt lines (zero config)
  "string"     -> exact prompt match (e.g. --prompt="(gdb)")
  ./file       -> stdin hook: k feeds lines, hook exit = frame end
               hook path canonicalised to absolute at k new time; must exist and be executable

JSON output (-j / fire / poll):
  fired:        {"cell_id": "...", "status": "fired"}
  running:      {"cell_id": "...", "status": "running"}
  done:         {"cell_id": "...", "status": "done", "output": "..."}
  timeout:      {"cell_id": "...", "status": "timeout", "output": ""}
  timeout(2+):  {"cell_id": "...", "status": "timeout", "output": "use k int or k kill"}
  error:        {"status": "error", "output": "..."}
  cell error:   {"cell_id": "...", "status": "error", "output": "..."}

  Errors without cell_id: no session, active cell, pipe failed, send failed, no active cell
  Errors with cell_id:    interrupted, unknown cell, watcher died, lock update failed, interrupt failed

Timeout: lock is NOT released (command may still be running).
  Only k int or k kill releases. k int sends ctrl-c, writes interrupted, releases lock.

Monitor (separate command):
  km <session> [cell_id] [-1]    event stream -- each stdout line is one JSON event
                                 -1 = exit after first completion (one-shot)
  Events: fired, done, timeout, interrupted, notify, closed, error (all include "ts" field)
"""
import fcntl, json, os, re, shlex, signal, shutil, subprocess, sys, time, uuid

# ensure package importable when run as standalone script (_bg subprocess)
_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _src not in sys.path:
    sys.path.insert(0, _src)

from agent_tty._shared import (  # noqa: E402
    TMUX, ANSI_RE, FRAME_ENTERS, CELL_DIR,
    FIRED, DONE, TIMEOUT, INTERRUPTED, RUNNING, ERROR, NOTIFY,
    cell_event, notify_event,
    CELL_EVENT_RE, NOTIFY_EVENT_RE,
    ensure_private_dir, validate_cell_id, validate_name,
)


# ═══════════════════════════════════════════
# TMUX
# ═══════════════════════════════════════════

class T:
    @staticmethod
    def spawn(s, cmd):
        subprocess.run([TMUX, "new-session", "-d", "-s", s, "-x", "10000", "-y", "50"]
                       + ([cmd] if cmd else []), check=True)
    @staticmethod
    def has(s):
        return subprocess.run([TMUX, "has-session", "-t", s], capture_output=True).returncode == 0
    @staticmethod
    def kill(s):
        subprocess.run([TMUX, "kill-session", "-t", s], capture_output=True)
    @staticmethod
    def send(s, text):
        subprocess.run([TMUX, "send-keys", "-t", s, text, "Enter"], check=True)
    @staticmethod
    def send_enter(s):
        subprocess.run([TMUX, "send-keys", "-t", s, "", "Enter"], check=True)
    @staticmethod
    def send_int(s):
        subprocess.run([TMUX, "send-keys", "-t", s, "C-c"], check=True)
    @staticmethod
    def ls():
        r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True)
        return r.stdout.strip()
    @staticmethod
    def pipe_start(s, logfile):
        with _open_private(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, "a"):
            pass
        subprocess.run([TMUX, "pipe-pane", "-t", s, f"cat >> {shlex.quote(logfile)}"], check=True)
    @staticmethod
    def pipe_stop(s):
        subprocess.run([TMUX, "pipe-pane", "-t", s], capture_output=True)


# ═══════════════════════════════════════════
# PATHS + HELPERS
# ═══════════════════════════════════════════

def _session_dir(s):
    validate_name(s)
    return ensure_private_dir(os.path.join(CELL_DIR, s))

def _meta(s):   return os.path.join(_session_dir(s), "_session.json")
def _lock(s):   return os.path.join(_session_dir(s), "_lock.json")
def _lock_guard_path(s): return os.path.join(_session_dir(s), "_lock.guard")
def _log(s):    return os.path.join(_session_dir(s), "_output.log")
def _result(s, cid): return os.path.join(_session_dir(s), f"{validate_cell_id(cid)}_result.json")

def _open_private(path, flags, mode="r"):
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, mode)
    except Exception:
        os.close(fd)
        raise

class LockGuard:
    """Proof token: caller holds the per-session lock-file mutex."""
    def __init__(self, session):
        self.session = session
        self._f = None

    def __enter__(self):
        self._f = _open_private(_lock_guard_path(self.session),
                                os.O_RDWR | os.O_CREAT, "r+")
        fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        finally:
            self._f.close()
        return False

def _log_size(s):
    try: return os.path.getsize(_log(s))
    except FileNotFoundError: return 0

def _ensure_pipe(s):
    """(Re)start pipe-pane. Idempotent — replaces dead/existing pipe."""
    logpath = _log(s)
    T.pipe_start(s, logpath)

def _log_event(s, event):
    try:
        with _open_private(_log(s), os.O_WRONLY | os.O_CREAT | os.O_APPEND, "a") as f:
            f.write(f"\n{event}\n")
    except OSError: pass

def _resolve(explicit=None):
    if explicit:
        validate_name(explicit)
        return explicit
    env = os.environ.get("K_SESSION")
    if env:
        validate_name(env)
        return env
    if os.path.isdir(CELL_DIR):
        ss = [d for d in os.listdir(CELL_DIR) if os.path.isfile(os.path.join(CELL_DIR, d, "_session.json"))]
        if len(ss) == 1:
            validate_name(ss[0])
            return ss[0]
    return None

def _json(d): print(json.dumps(d, ensure_ascii=False))

def _emit(json_out, data, text=None):
    """Unified output: JSON mode → _json(data), text mode → print(text)."""
    if json_out: _json(data)
    else: print(text if text is not None else data.get("output", ""))

def _watcher_pgid(meta):
    """Return watcher process group id."""
    return meta.get("bg_pgid")

def _watcher_alive(meta):
    pgid = _watcher_pgid(meta)
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

def _kill_watcher(meta):
    """Terminate bg watcher process group. Returns True when it is gone."""
    pgid = _watcher_pgid(meta)
    if not pgid:
        return False
    pgid = int(pgid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _watcher_alive(meta):
            return True
        time.sleep(0.05)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _watcher_alive(meta):
            return True
        time.sleep(0.05)
    return not _watcher_alive(meta)

def _write_result(session, cell_id, result):
    """Atomic result write: tmp + fsync + os.replace. No partial reads."""
    rpath = _result(session, cell_id)
    tmp = rpath + ".tmp"
    with _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, "w") as f:
        json.dump(result, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, rpath)

def _update_lock(session, cell_id=None, **kw):
    """Read-modify-write lock file via atomic tmp+replace.
    If cell_id given, verify it matches.
    Returns True on success, False on failure or cell_id mismatch."""
    with LockGuard(session):
        return _update_lock_unlocked(session, cell_id, **kw)

def _update_lock_unlocked(session, cell_id=None, **kw):
    try:
        lock = _lock(session)
        with _open_private(lock, os.O_RDONLY, "r") as f:
            meta = json.load(f)
        if cell_id is not None and meta.get("cell_id") != cell_id:
            return False
        meta.update(kw)
        tmp = lock + ".tmp"
        with _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, "w") as f:
            json.dump(meta, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, lock)
        return True
    except Exception:
        return False

def _mark_terminal(session, cell_id, status):
    """Record terminal state on the lock without deleting it."""
    if status == TIMEOUT:
        return _update_lock(session, cell_id=cell_id,
                            timed_out=True, terminal_status=TIMEOUT)
    if status == DONE:
        return _update_lock(session, cell_id=cell_id,
                            completed=True, terminal_status=DONE)
    return _update_lock(session, cell_id=cell_id, terminal_status=status)


# ═══════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════

def _create(session, cmd, prompt=None):
    T.spawn(session, cmd)
    _session_dir(session)
    _ensure_pipe(session)
    time.sleep(1.0)
    meta = {"name": session, "cmd": cmd}
    if prompt:
        meta["prompt"] = prompt  # explicit prompt → exact match mode
    with _open_private(_meta(session), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, "w") as f:
        json.dump(meta, f)

def _session_exists(session):
    return T.has(session) and os.path.exists(_meta(session))

def _session_prompt(session):
    """Returns explicit prompt if set, None for default repeat-detection."""
    try:
        with _open_private(_meta(session), os.O_RDONLY, "r") as f: return json.load(f).get("prompt")
    except Exception: return None

def _session_cmd(session):
    try:
        with _open_private(_meta(session), os.O_RDONLY, "r") as f:
            return json.load(f).get("cmd")
    except Exception:
        return None


# ═══════════════════════════════════════════
# LOCK = CELL METADATA
# ═══════════════════════════════════════════

def _acquire(session, cell_id, log_offset, echo_count):
    validate_cell_id(cell_id)
    with LockGuard(session):
        return _acquire_unlocked(session, cell_id, log_offset, echo_count)

def _acquire_unlocked(session, cell_id, log_offset, echo_count):
    lock = _lock(session)
    meta = {"cell_id": cell_id, "log_offset": log_offset, "echo_count": echo_count}
    for _ in range(2):
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(lock, flags, 0o600)
            try:
                os.write(fd, json.dumps(meta).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            return None
        except FileExistsError:
            try:
                with _open_private(lock, os.O_RDONLY, "r") as f:
                    held = json.load(f)
            except Exception:
                return "?"
            held_id = held.get("cell_id", "?")
            if held.get("completed") and held.get("terminal_status") == DONE:
                _release_unlocked(session, held_id)
                continue
            return held_id
    return "?"

def _load_cell(session):
    try:
        with _open_private(_lock(session), os.O_RDONLY, "r") as f: return json.load(f)
    except Exception: return None

def _release(session, cell_id):
    with LockGuard(session):
        return _release_unlocked(session, cell_id)

def _release_unlocked(session, cell_id):
    try:
        lock = _lock(session)
        with _open_private(lock, os.O_RDONLY, "r") as f:
            meta = json.load(f)
        # Close the read handle before unlinking the lock file.
        if meta.get("cell_id") == cell_id:
            os.unlink(lock)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def _send_interrupt(session):
    """Send Ctrl-C to REPL + re-send frame enters in repeat mode.
    Returns True if Ctrl-C was delivered (or session is already dead).
    Returns False if Ctrl-C failed but session is still alive — caller must not release.
    """
    prompt = _session_prompt(session)
    try:
        T.send_int(session)
    except Exception:
        # Ctrl-C didn't reach REPL. If session is dead, nothing is running → safe.
        # If session is alive, command may still be running → unsafe to release.
        return not T.has(session)
    time.sleep(0.3)
    # re-frame is best-effort (Ctrl-C already delivered)
    if not prompt:
        try:
            _send_frame_enters(session)
        except Exception:
            pass
    return True


class CellBusy(Exception):
    """Raised by CellLock when the session already has an active cell."""
    def __init__(self, held_id):
        self.held_id = held_id


class CellLock:
    """RAII lock for cell lifecycle. Three states via sent/keep:
      pre-send (default)  → release on any exit
      post-send (sent)    → interrupt recovery on exception, release on normal exit
      keep (timeout/fire) → lock stays held, no cleanup
    """
    def __init__(self, session, cell_id, log_offset, echo_count):
        self.session = session
        self.cell_id = cell_id
        self.sent = False
        self.keep = False
        self.interrupt_failed = False
        held = _acquire(session, cell_id, log_offset, echo_count)
        if held:
            raise CellBusy(held)

    def __enter__(self):
        return self

    def mark_sent(self):
        self.sent = True

    def mark_keep(self):
        self.keep = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.keep:
            return False
        if exc_type is not None and self.sent:
            if not _send_interrupt(self.session):
                # Ctrl-C didn't reach REPL but session is alive — keep lock
                # (same reasoning as timeout: command may still be running)
                self.interrupt_failed = True
                return False
        _release(self.session, self.cell_id)
        # sync mode cleanup: remove result file (nobody will poll it)
        try:
            rpath = _result(self.session, self.cell_id)
            if os.path.exists(rpath): os.unlink(rpath)
        except OSError: pass
        _cleanup_input_script(self.session, self.cell_id)
        return False


# ═══════════════════════════════════════════
# STREAM PROCESSOR
# Frame delimiter: N consecutive identical non-empty lines
# (= REPL redrawing prompt on empty Enter)
# ═══════════════════════════════════════════

def _stream_process(session, cell_id, log_offset, echo_count, timeout=None, prompt=None):
    """
    Stream processor with three modes:
      prompt=None      → frame = N consecutive identical lines (default)
      prompt="string"  → frame = exact prompt match
      prompt="./file"  → frame = hook process (stdin lines, exit=done)
    """
    logpath = _log(session)
    state = "OUTPUT" if echo_count <= 0 else "ECHOING"
    remaining = echo_count
    output = []
    deadline = time.monotonic() + timeout if timeout else None
    repeat_count = 0
    last_clean = None

    # start hook process if prompt is an absolute file path (canonicalised by cmd_new)
    hook = None
    if prompt and os.path.isabs(prompt) and os.path.isfile(prompt):
        hook = subprocess.Popen(
            [prompt], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True
        )
        prompt = None  # don't also do string matching

    last_appended = False  # tracks if last line was appended (not filtered)
    timed_out = False

    try:
        with open(logpath, "r", errors="replace") as f:
            f.seek(log_offset)
            while True:
                if deadline and time.monotonic() > deadline:
                    timed_out = True
                    break

                line = f.readline()
                if not line:
                    if hook and hook.poll() is not None:
                        # hook exited — pop last line only if it was appended
                        if output and last_appended:
                            output.pop()
                        break
                    time.sleep(0.05)
                    continue

                clean = ANSI_RE.sub("", line).strip()
                if not clean:
                    continue

                if clean.startswith("── cell:") or clean.startswith("── notify "):
                    continue

                if state == "ECHOING":
                    remaining -= 1
                    if remaining <= 0:
                        state = "OUTPUT"

                elif state == "OUTPUT":
                    if hook:
                        # hook mode: feed line, exit = frame end
                        # NO filtering — hook user takes full control of output
                        try:
                            hook.stdin.write(clean + "\n")
                            hook.stdin.flush()
                        except (BrokenPipeError, OSError):
                            if output and last_appended:
                                output.pop()
                            break
                        output.append(clean)
                        last_appended = True
                        time.sleep(0.01)
                        if hook.poll() is not None:
                            output.pop()
                            break
                    elif prompt:
                        # string mode: exact match
                        if clean == prompt:
                            break
                        if clean == "..." or clean.startswith("... "):
                            continue
                        output.append(clean)
                    else:
                        # repeat mode: N consecutive identical lines
                        if clean == "..." or clean.startswith("... "):
                            last_clean = clean
                            continue

                        if clean == last_clean:
                            repeat_count += 1
                        else:
                            repeat_count = 0

                        output.append(clean)

                        if repeat_count >= FRAME_ENTERS - 1:
                            for _ in range(repeat_count + 1):
                                output.pop()
                            break
                    last_clean = clean
    finally:
        if hook:
            if hook.poll() is None:
                hook.kill()
            hook.wait()

    result = {
        "cell_id": cell_id,
        "status": TIMEOUT if timed_out else DONE,
        "output": "" if timed_out else "\n".join(output)
    }

    _write_result(session, cell_id, result)
    _mark_terminal(session, cell_id, result["status"])
    if timed_out:
        _log_event(session, cell_event(cell_id, TIMEOUT))
    else:
        _log_event(session, cell_event(cell_id, DONE))
        _cleanup_input_script(session, cell_id)

    return result


def _echo_count(code):
    """Count how many lines the REPL will echo (= non-trailing-blank lines)."""
    code_lines = code.lstrip().split("\n")
    count = len(code_lines)
    while count > 0 and not code_lines[count - 1].strip():
        count -= 1
    return count

def _looks_like_bash(cmd):
    if not cmd:
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts:
        return False
    return os.path.basename(parts[0]) == "bash"

def _has_multiple_code_lines(code):
    return _echo_count(code) > 1

def _input_script(session, cell_id):
    validate_cell_id(cell_id)
    return os.path.join(_session_dir(session), f"{cell_id}_input.sh")

def _write_input_script(session, cell_id, code):
    path = _input_script(session, cell_id)
    with _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, "w") as f:
        f.write(code)
        if code and not code.endswith("\n"):
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    return path

def _cleanup_input_script(session, cell_id):
    try:
        os.unlink(_input_script(session, cell_id))
    except FileNotFoundError:
        pass
    except OSError:
        pass

def _should_source_bash(session, code, prompt):
    return (
        not prompt
        and _has_multiple_code_lines(code)
        and _looks_like_bash(_session_cmd(session))
    )

def _source_command(session, cell_id):
    script = _input_script(session, cell_id)
    return f"source {shlex.quote(script)}"


def _send_frame_enters(session):
    """Send FRAME_ENTERS empty Enters via send-keys (repeat-mode framing)."""
    args = [TMUX, "send-keys", "-t", session]
    for _ in range(FRAME_ENTERS):
        args.extend(["", "Enter"])
    subprocess.run(args, check=True)


def _send_code(session, code, prompt=None):
    """Send code via paste-buffer (no per-char echo) + frame enters."""
    code_lines = code.lstrip().split("\n")

    # paste-buffer: entire text arrives as one write → readline redraws once
    text = "\n".join(code_lines) + "\n"
    buf = f"k_{session}"
    subprocess.run([TMUX, "load-buffer", "-b", buf, "-"], input=text.encode(), check=True)
    subprocess.run([TMUX, "paste-buffer", "-b", buf, "-d", "-t", session], check=True)

    if not prompt:
        _send_frame_enters(session)


# ═══════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════

def cmd_new(session, cmd_parts, prompt=None):
    validate_name(session)
    if T.has(session):
        print(f"OK {session} (alive)")
        return 0
    # hook mode: path contains / or \ → canonicalize and fail early if missing
    if prompt and (os.sep in prompt or "/" in prompt):
        prompt = os.path.abspath(os.path.expanduser(prompt))
        if not os.path.isfile(prompt):
            print(f"ERR hook not found: {prompt}"); return 1
        if not os.access(prompt, os.R_OK):
            print(f"ERR hook not readable: {prompt}"); return 1
        if not os.access(prompt, os.X_OK):
            print(f"ERR hook not executable: {prompt}"); return 1
    cmd = " ".join(cmd_parts) if cmd_parts else "bash"
    _create(session, cmd, prompt)
    if prompt:
        print(f"OK {session} prompt={repr(prompt)}")
    else:
        print(f"OK {session}")
    return 0


def cmd_fire(session, code, timeout=300):
    if not _session_exists(session):
        _json({"status": "error", "output": f"no session '{session}'"}); return 1

    cell_id = uuid.uuid4().hex[:12]
    prompt = _session_prompt(session)
    source_bash = _should_source_bash(session, code, prompt)
    send_code = _source_command(session, cell_id) if source_bash else code
    echo_count = _echo_count(send_code)
    log_offset = _log_size(session)

    try:
        lock = CellLock(session, cell_id, log_offset, echo_count)
    except CellBusy as e:
        _json({"status": "error", "output": f"active cell '{e.held_id}'"}); return 1

    bg = None
    try:
        with lock:
            try:
                _ensure_pipe(session)
            except Exception as e:
                _json({"status": "error", "output": f"pipe failed: {e}"}); return 1

            try:
                if source_bash:
                    _write_input_script(session, cell_id, code)
                _send_code(session, send_code, prompt)
            except Exception as e:
                _json({"status": "error", "output": f"send failed: {e}"}); return 1

            lock.mark_sent()
            _log_event(session, cell_event(cell_id, FIRED))

            bg_args = [sys.executable, os.path.abspath(__file__), "_bg",
                       session, cell_id, str(log_offset), str(echo_count), str(timeout)]
            if prompt:
                bg_args.append(prompt)

            bg = subprocess.Popen(bg_args, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # start_new_session=True makes pid == process group id.
            if not _update_lock(session, cell_id=cell_id, bg_pgid=bg.pid):
                _kill_watcher({"bg_pgid": bg.pid})
                raise RuntimeError("lock update failed")

            lock.mark_keep()  # bg process owns the lock now
    except (Exception, KeyboardInterrupt):
        if bg is not None and not lock.keep:
            _kill_watcher({"bg_pgid": bg.pid})
        msg = "interrupt failed; use k kill" if lock.interrupt_failed else "interrupted"
        _json({"cell_id": cell_id, "status": "error", "output": msg})
        return 1

    _json({"cell_id": cell_id, "status": FIRED})
    return 0


def cmd_poll(session, cell_id=None):
    if cell_id is None:
        meta = _load_cell(session)
        if not meta:
            _json({"status": "error", "output": f"no active cell on '{session}'"}); return 1
        cell_id = meta["cell_id"]
    try:
        cell_id = validate_cell_id(cell_id)
    except ValueError:
        _json({"status": "error", "output": "invalid cell_id"})
        return 1

    rpath = _result(session, cell_id)
    if os.path.exists(rpath):
        try:
            with open(rpath) as f: result = json.load(f)
        except (json.JSONDecodeError, OSError):
            # atomic writes make this near-impossible; if it happens,
            # do NOT release lock — state is unknown, let user k int / k kill
            _json({"cell_id": cell_id, "status": "running"})
            return 0
        if result.get("status") == TIMEOUT:
            # mark lock BEFORE returning; timeout means REPL may still be busy
            if not _mark_terminal(session, cell_id, TIMEOUT):
                _json({"cell_id": cell_id, "status": "error", "output": "lock update failed; use k int or k kill"})
                return 1
            # leave timeout result on disk — subsequent polls re-read it (idempotent)
            # prevents race where k int writes interrupted result between our read and unlink
        else:
            # non-timeout: release first, then consume result
            if not _release(session, cell_id):
                _json({"cell_id": cell_id, "status": "error", "output": "lock release failed"})
                return 1
            try: os.unlink(rpath)
            except OSError: pass
        _json(result)
        return 0

    # check lock state
    meta = _load_cell(session)

    # no lock, or lock is for a different cell → this cell_id is unknown
    if not meta or meta.get("cell_id") != cell_id:
        _json({"cell_id": cell_id, "status": "error", "output": "unknown cell"})
        return 1

    # completed done-lock: bg watcher finished, but nobody polled the result.
    # Release so the next fire/run can proceed; explicit poll <old_cell> can
    # still consume the result file if it exists.
    if meta.get("completed") and meta.get("terminal_status") == DONE:
        _release(session, cell_id)
        _json({"cell_id": cell_id, "status": "error", "output": "result missing"})
        return 1

    # timed_out: command may still be running — only k int / k kill can release
    if meta.get("timed_out"):
        _json({"cell_id": cell_id, "status": "timeout", "output": "use k int or k kill"})
        return 1

    # check if bg process died (orphaned lock)
    if _watcher_pgid(meta):
        if not _watcher_alive(meta):
            # watcher died but REPL command may still be running — mark timed_out
            # so user must k int / k kill to recover safely
            _mark_terminal(session, cell_id, TIMEOUT)
            _log_event(session, cell_event(cell_id, TIMEOUT))
            _json({"cell_id": cell_id, "status": "error", "output": "watcher died"})
            return 1

    _json({"cell_id": cell_id, "status": "running"})
    return 0


def cmd_run(session, code, timeout=30, json_out=False):
    if not _session_exists(session):
        _emit(json_out, {"status": "error", "output": f"no session '{session}'"})
        return 1

    prompt = _session_prompt(session)
    cell_id = uuid.uuid4().hex[:12]
    source_bash = _should_source_bash(session, code, prompt)
    send_code = _source_command(session, cell_id) if source_bash else code
    echo_count = _echo_count(send_code)
    log_offset = _log_size(session)

    try:
        lock = CellLock(session, cell_id, log_offset, echo_count)
    except CellBusy as e:
        _emit(json_out, {"status": "error", "output": f"active cell '{e.held_id}'"})
        return 1

    try:
        with lock:
            try:
                _ensure_pipe(session)
            except Exception as e:
                _emit(json_out, {"status": "error", "output": f"pipe failed: {e}"})
                return 1

            try:
                if source_bash:
                    _write_input_script(session, cell_id, code)
                _send_code(session, send_code, prompt)
            except Exception as e:
                _emit(json_out, {"status": "error", "output": f"send failed: {e}"})
                return 1

            lock.mark_sent()
            result = _stream_process(session, cell_id, log_offset, echo_count, timeout, prompt)

            if result.get("status") == TIMEOUT:
                lock.mark_keep()
    except (Exception, KeyboardInterrupt):
        # CellLock.__exit__ handled cleanup (interrupt recovery or lock kept)
        msg = "interrupt failed; use k kill" if lock.interrupt_failed else "interrupted"
        _emit(json_out, {"cell_id": cell_id, "status": "error", "output": msg})
        return 1

    _emit(json_out, result)
    return 0


def cmd_notify(session, message):
    if not _session_exists(session):
        print(f"ERR no session '{session}'"); return 1
    try: parent = open(f"/proc/{os.getppid()}/comm").read().strip()
    except Exception: parent = "?"
    _log_event(session, notify_event(f"{parent}@k:{os.getpid()}", message))
    print(f"OK notified: {message}")
    return 0


def cmd_int(s):
    meta = _load_cell(s)
    if meta and meta.get("completed") and meta.get("terminal_status") == DONE:
        cell_id = meta["cell_id"]
        if not _release(s, cell_id):
            print("ERR lock release failed"); return 1
        _cleanup_input_script(s, cell_id)
        print("OK"); return 0
    if not _send_interrupt(s):
        print("ERR interrupt failed; use k kill"); return 1
    # kill bg watcher (if any) before releasing lock
    # prevents old watcher from consuming new cell's output
    if meta:
        cell_id = meta["cell_id"]
        if _watcher_pgid(meta) and not _kill_watcher(meta):
            print("ERR watcher kill failed; use k kill"); return 1
        # write result so poll finds closure — overwrites timeout result too
        _write_result(s, cell_id, {"cell_id": cell_id, "status": "error", "output": "interrupted"})
        _log_event(s, cell_event(cell_id, INTERRUPTED))
        if not _release(s, cell_id):
            print("ERR lock release failed"); return 1
        _cleanup_input_script(s, cell_id)
    print("OK"); return 0

def cmd_kill(s):
    # kill bg watcher if running
    meta = _load_cell(s)
    if meta:
        _kill_watcher(meta)
    T.pipe_stop(s); T.kill(s)
    d = os.path.join(CELL_DIR, s)
    if os.path.isdir(d): shutil.rmtree(d, ignore_errors=True)
    print(f"OK killed {s}"); return 0

def cmd_ls():
    s = T.ls(); print(s if s else "no sessions"); return 0

def cmd_status(session):
    if not _session_exists(session): print(f"ERR no session '{session}'"); return 1
    logpath = _log(session)
    pipe_ok = False
    if os.path.exists(logpath):
        before = os.path.getsize(logpath)
        subprocess.run([TMUX, "send-keys", "-t", session, " ", "BSpace"], capture_output=True)
        time.sleep(0.2)
        pipe_ok = (os.path.getsize(logpath) > before)
    if not pipe_ok:
        T.pipe_start(session, logpath)
        print(f"OK {session} pipe=repaired")
    else:
        print(f"OK {session} pipe=ok")
    return 0


# ═══════════════════════════════════════════
# WATCH / HISTORY
# ═══════════════════════════════════════════

# CELL_EVENT_RE and NOTIFY_EVENT_RE imported from _shared (type-sealed)

def _filter_line(raw_line):
    clean = ANSI_RE.sub("", raw_line).strip()
    if not clean: return None
    m = NOTIFY_EVENT_RE.match(clean)
    if m: return f"\033[33m📢 {m.group(2)}\033[0m \033[2m({m.group(1)})\033[0m"
    m = CELL_EVENT_RE.match(clean)
    if m:
        kind = m.group(2)
        if kind == FIRED: return f"\033[2;36m── {m.group(1)[:8]} ──\033[0m"
        elif kind == DONE: return f"\033[2;32m── ✓ ──\033[0m"
        elif kind == TIMEOUT: return f"\033[2;33m── ⏱ ──\033[0m"
        else: return f"\033[2;31m── ✗ ──\033[0m"  # interrupted
    if clean == "..." or clean.startswith("... "): return None
    return ANSI_RE.sub("", raw_line).rstrip()

def cmd_watch(session):
    if not _session_exists(session): print(f"ERR no session '{session}'"); return 1
    logpath = _log(session)
    if not os.path.exists(logpath): print(f"ERR no log"); return 1
    print(f"\033[2mwatching {session} (ctrl-c to stop)\033[0m\n")
    try:
        proc = subprocess.Popen(["tail", "-n", "0", "-f", logpath], stdout=subprocess.PIPE, text=True)
        repeat_buf = []  # buffer identical lines; flush if run < FRAME_ENTERS
        for raw_line in proc.stdout:
            r = _filter_line(raw_line)
            if r is None:
                continue
            if repeat_buf and r.strip() == repeat_buf[0].strip():
                repeat_buf.append(r)
            else:
                # new line differs — flush buffer if it was real output (< FRAME_ENTERS)
                if len(repeat_buf) < FRAME_ENTERS:
                    for line in repeat_buf:
                        print(line)
                repeat_buf = [r]
            # semantic events (cell/notify) are never frame noise — flush immediately
            # so completion ticks don't stall waiting for the next different line
            if r.startswith("\033["):
                for line in repeat_buf:
                    print(line)
                repeat_buf = []
        # flush remaining
        if len(repeat_buf) < FRAME_ENTERS:
            for line in repeat_buf:
                print(line)
    except KeyboardInterrupt: print(f"\n\033[2mstopped\033[0m")
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
    return 0

def cmd_history(session, n=5):
    if not _session_exists(session): print(f"ERR no session '{session}'"); return 1
    logpath = _log(session)
    if not os.path.exists(logpath): print(f"ERR no log"); return 1
    with open(logpath, "r", errors="replace") as f: raw_lines = f.readlines()
    filtered = [r for line in raw_lines if (r := _filter_line(line)) is not None]
    # suppress runs of FRAME_ENTERS+ identical lines (frame noise), keep shorter runs
    out = []
    i = 0
    while i < len(filtered):
        j = i + 1
        while j < len(filtered) and filtered[j].strip() == filtered[i].strip():
            j += 1
        if j - i < FRAME_ENTERS:
            out.extend(filtered[i:j])
        i = j
    for line in out[-n * 5:]: print(line)
    return 0


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip()); return 0
    verb, rest = args[0], args[1:]

    if verb == "_bg" and len(rest) >= 5:
        session, cell_id, offset, echo, tout = rest[:5]
        validate_name(session)
        prompt = rest[5] if len(rest) > 5 else None
        _stream_process(session, cell_id, int(offset), int(echo), timeout=int(tout), prompt=prompt)
        return 0

    if verb == "new" and rest:
        prompt = None; cmd_parts = []
        for a in rest[1:]:
            if a.startswith("--prompt="): prompt = a[len("--prompt="):]
            else: cmd_parts.append(a)
        return cmd_new(rest[0], cmd_parts, prompt)
    if verb == "kill" and rest:
        validate_name(rest[0]); return cmd_kill(rest[0])
    if verb == "ls": return cmd_ls()

    if verb in ("run", "await"):
        timeout, json_out = 30, False
        while rest and rest[0].startswith("-"):
            if rest[0] == "-t":
                if len(rest) < 2: print("usage: k run [-j] [-t N] [session] <code>"); return 1
                timeout = int(rest[1]); rest = rest[2:]
            elif rest[0] == "-j": json_out = True; rest = rest[1:]
            else: break
        if len(rest) >= 2: s, c = rest[0], rest[1]; validate_name(s)
        elif len(rest) == 1: s, c = _resolve(), rest[0]
        else: print("usage: k run [-j] [-t N] [session] <code>"); return 1
        if not s: print("ERR: no session found."); return 1
        return cmd_run(s, c, timeout, json_out)

    if verb == "fire" and rest:
        timeout = 300
        while rest and rest[0].startswith("-"):
            if rest[0] == "-t":
                if len(rest) < 2: print("usage: k fire [-t N] [session] <code>"); return 1
                timeout = int(rest[1]); rest = rest[2:]
            else: break
        if len(rest) >= 2: s, c = rest[0], rest[1]; validate_name(s)
        else: s, c = _resolve(), rest[0]
        if not s: print("ERR: no session found."); return 1
        return cmd_fire(s, c, timeout)

    if verb == "poll":
        s = _resolve(rest[0] if rest else None)
        if not s: print("ERR: no session found."); return 1
        return cmd_poll(s, rest[1] if len(rest) >= 2 else None)

    if verb == "notify" and rest:
        if len(rest) >= 2 and T.has(rest[0]):
            validate_name(rest[0]); s, msg = rest[0], " ".join(rest[1:])
        else: s, msg = _resolve(), " ".join(rest)
        if not s: print("ERR: no session found."); return 1
        return cmd_notify(s, msg)

    if verb == "int":
        s = _resolve(rest[0] if rest else None)
        if not s: print("ERR: no session found."); return 1
        return cmd_int(s)
    if verb == "status":
        s = _resolve(rest[0] if rest else None)
        if not s: print("ERR: no session found."); return 1
        return cmd_status(s)
    if verb == "watch":
        s = _resolve(rest[0] if rest else None)
        if not s: print("ERR: no session found."); return 1
        return cmd_watch(s)
    if verb == "history":
        n = 5
        if rest and rest[0] == "-n":
            if len(rest) < 2: print("usage: k history [-n N] [session]"); return 1
            n = int(rest[1]); rest = rest[2:]
        s = _resolve(rest[0] if rest else None)
        if not s: print("ERR: no session found."); return 1
        return cmd_history(s, n)

    print(__doc__.strip()); return 1

if __name__ == "__main__": sys.exit(main())
