#!/usr/bin/env python3
"""pythond -- persistent Python sessions.  Code in, result out.

The whole idea:

    ns = {}
    while True:
        code = receive()
        exec(code, ns)          # ns stays alive -- variables survive
        send(captured_stdout)

Everything in this file is that loop plus delivery:

  1. thread-safe stdout capture  -- concurrent cells don't interleave output
  2. REPL semantics              -- last expression auto-prints, like >>>
  3. named sessions              -- one plain subprocess per session, isolated
  4. fire / fork                 -- async cells: thread (shares ns) / process (killable)
  5. local HTTP                  -- one-shot CLI calls reach the live process

Transport is borrowed, never built:

  local POSIX    HTTP over AF_UNIX socket -- fs permissions are the auth
  local Windows  HTTP over 127.0.0.1 + bearer token (%LOCALAPPDATA%\\pythond)
  remote         ssh host pysh run work "code"   (ssh ControlMaster for latency;
                 state lives in the remote daemon, not in the connection)
  TLS            nginx / caddy / ssh -L, if a TCP port must exist at all
  attach         pysh attach = client-side line REPL on the same channel;
                 remote humans: ssh -t host pysh attach work

The debug client is curl:

  curl --unix-socket $XDG_RUNTIME_DIR/pythond/pythond.sock \\
       --data-binary '1 + 1' http://pythond/run/work

Commands (pysh):
    pysh new <name>              create a Python session
    pysh run <name> "code"       sync eval/exec, raw output
    pysh fire <name> "code"      async thread -- shares namespace, can't kill C
    pysh fork <name> "code"      async process (POSIX only) -- killable, pickles vars back
    pysh poll <name> [cell_id]   check async result
    pysh int <name>              best-effort interrupt (fire = async exc, fork = SIGKILL)
    pysh kill <name>             terminate session
    pysh ls                      list sessions
    pysh status <name>           session health (JSON)
    pysh vars <name>             namespace names (JSON)
    pysh complete <name> "text"  tab completion (JSON)
    pysh attach <name>           line REPL into the session (Ctrl-D detaches)

Daemon (pyctl / pythond):
    pythond daemon [--show-token]    start daemon in foreground
    pyctl start / stop / status

HTTP API (what pysh speaks; curl speaks it too):
    GET  /ls                     text listing
    POST /new/<name>             create session
    POST /run/<name>   body=code raw output; X-Pythond-Exec-Error: 1 on traceback
    POST /fire/<name>  body=code JSON {"cell_id": ..., "status": "fired"}
    POST /fork/<name>  body=code JSON {"cell_id": ..., "status": "forked"}
    GET  /poll/<name>[?cell=ID]  JSON cell result
    GET  /status/<name>          JSON health
    GET  /vars/<name>            JSON namespace names
    POST /complete/<name> body   JSON completion matches
    POST /int/<name>             JSON interrupt report
    POST /kill/<name>            kill session
    POST /stop                   stop daemon
    404 = no such session/route, 409 = session channel broken, 401 = bad token.

Security (same model as SSH):
  Not a sandbox: code runs with the daemon user's OS permissions.
  The daemon only ever binds an AF_UNIX socket or 127.0.0.1.  There is no
  network listener to harden; exposure is ssh's (or your reverse proxy's) job.

Auto-checkpoint:
  ~/.pythond/sessions/<name>/history.py -- successful sync execs, plus async
  execs when poll observes completion; replayable with exec(open(...).read()).
  History can contain secrets you paste into cells; treat it like shell history.

fire vs fork:
    fire = threading.Thread.  Shares namespace -- fire'd code can set variables
    that later calls read.  Cannot be killed when stuck in C code.
    Exec is serialized (one cell at a time) -- async to the client, not parallel.
    fork = os.fork() child process (POSIX only).  COW copy of namespace.
    Killable (SIGKILL).  New/changed vars are pickled back and merged.
    Unpicklable objects (sockets, locks, CUDA tensors) are skipped.
    In-place mutations (list.append, dict[k]=v) won't merge -- use assignment.
    Merge is last-writer-wins: a completed fork may overwrite variables changed
    in the parent while the fork was running.
"""
from __future__ import annotations

import sys, os, socket, json, threading, uuid, io, traceback, time, tempfile
import argparse
import codeop
import contextlib
import ctypes
import hmac
import http.client
import itertools
import pickle
import queue
import re
import secrets
import signal, subprocess
import socketserver
import typing
import urllib.parse
import ast as _ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.5.0"

JsonDict = dict[str, typing.Any]

_MAX_SESSIONS = int(os.environ.get("PYTHOND_MAX_SESSIONS", "128"))
_MAX_BODY = int(os.environ.get("PYTHOND_MAX_BODY", str(16 * 1024 * 1024)))
_MAX_WORKER_RESPONSE = int(os.environ.get("PYTHOND_MAX_WORKER_RESPONSE",
                                          str(16 * 1024 * 1024)))
_SESSION_NAME_RE = re.compile(r"^[a-z0-9_-]{1,80}$")
_WIN_RESERVED_NAME_RE = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)",
    re.IGNORECASE,
)
_SESSION_NAME_RULE = (
    "Session names: lowercase a-z, 0-9, '_' or '-', 1-80 chars; "
    "Windows device names are rejected."
)
_ASYNC_CELL_TTL = 300
_SESSION_READY_TIMEOUT = 10.0
_SEND_TIMEOUT = 30.0
_CELL_SEQ = itertools.count()
_INTERRUPT_LOCK = threading.Lock()
_WORKER_ENV = "PYTHOND_INTERNAL_WORKER"
_SET_ASYNC_EXC: typing.Any = ctypes.pythonapi.PyThreadState_SetAsyncExc
_SET_ASYNC_EXC.restype = ctypes.c_int
_HAS_AF_UNIX = sys.platform != "win32" and hasattr(socket, "AF_UNIX")

# -----------------------------------------------
# PATHS + METADATA
# -----------------------------------------------

def _runtime_base() -> str:
    """Private runtime dir for the socket / daemon metadata.  Fails loud if it
    cannot be made owner-private."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        path = os.path.join(base, "pythond")
        os.makedirs(path, exist_ok=True)
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and os.path.isdir(xdg):
            path = os.path.join(xdg, "pythond")
        else:
            path = os.path.join(tempfile.gettempdir(), f"pythond-{os.getuid()}")
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def _default_sock() -> str:
    return os.path.join(_runtime_base(), "pythond.sock")


SOCK = os.environ.get("PYTHOND_SOCK") or ""


def _sock_path() -> str:
    return SOCK or _default_sock()


def _meta_path() -> str:
    return os.path.join(_runtime_base(), "daemon.json")


def _write_meta(port: int, token: str) -> None:
    """Persist local TCP connection metadata for client discovery (Windows)."""
    fd, tmp = tempfile.mkstemp(prefix="daemon.json.", suffix=".tmp",
                               dir=os.path.dirname(_meta_path()))
    try:
        os.write(fd, json.dumps(
            {"port": port, "token": token, "pid": os.getpid()}).encode())
    finally:
        os.close(fd)
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, _meta_path())


def _read_meta() -> JsonDict:
    try:
        with open(_meta_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _remove_meta() -> None:
    meta = _read_meta()
    if meta.get("pid") != os.getpid():
        return
    with contextlib.suppress(OSError):
        os.remove(_meta_path())


def _validate_session_name(name: str) -> str:
    """Validate a session name before it becomes a filesystem path."""
    if (not isinstance(name, str) or not _SESSION_NAME_RE.fullmatch(name)
            or name != name.lower()
            or _WIN_RESERVED_NAME_RE.match(name)):
        raise ValueError("invalid session name")
    return name


def _session_dir(name: str) -> str:
    """Return ~/.pythond/sessions/<name>/, creating if needed."""
    _validate_session_name(name)
    path = os.path.join(os.path.expanduser("~"), ".pythond", "sessions", name)
    if sys.platform == "win32":
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _log_history(name: str, src: str) -> None:
    """Append successful exec source to history.py (replayable)."""
    try:
        path = os.path.join(_session_dir(name), "history.py")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"\n# [{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{src}\n")
    except OSError as e:
        print(f"WARN: history log failed for {name}: {e}", file=sys.stderr)


def _public_error(e: BaseException) -> str:
    """Client-facing error text.  Our own ValueError/RuntimeError messages are
    authored strings; anything else is reduced to the class name."""
    if isinstance(e, (ValueError, RuntimeError)):
        return str(e)
    return e.__class__.__name__

# =============================================
# EXEC ENGINE (the actual product)
# =============================================

def _init_namespace() -> JsonDict:
    """Create the persistent namespace for one session.  Convenience imports
    only; code in a session has normal Python process permissions."""
    ns = {"__builtins__": __builtins__}
    exec("import os,sys,json,subprocess,shutil,hashlib,time,re,glob,sqlite3,socket", ns)
    return ns


class _ThreadStdout:
    """Thread-local stdout wrapper.  Each thread captures to its own buffer.

    Main thread (exec cells): set _local.buf -> print captured to cell output.
    Sub-threads (user code): _local.buf is None -> print goes to real stdout
    (which the worker points at stderr, so it never corrupts the protocol).
    """
    def __init__(self, real: typing.TextIO) -> None:
        self._real = real
        self._local = threading.local()
    def write(self, s: str) -> typing.Any:
        buf = getattr(self._local, "buf", None)
        if buf is not None:
            return buf.write(s)
        return self._real.write(s)
    def writelines(self, lines: typing.Iterable[str]) -> None:
        buf = getattr(self._local, "buf", None)
        (buf or self._real).writelines(lines)
    def flush(self) -> None:
        buf = getattr(self._local, "buf", None)
        (buf or self._real).flush()
    def fileno(self) -> int:
        return self._real.fileno()
    def isatty(self) -> bool:
        return self._real.isatty()
    @property
    def encoding(self) -> str:
        return self._real.encoding


class _ExecOutput(str):
    """String output with an internal execution-error flag."""
    error: bool
    def __new__(cls, value: str, error: bool = False) -> "_ExecOutput":
        obj = str.__new__(cls, value)
        obj.error = bool(error)
        return obj


def _eval_exec_cell(src: str, ns: JsonDict) -> None:
    """Run src in ns with REPL-like semantics.

    Single expression -> eval -> print result (str raw, else repr).
    Multi-line with last expression -> exec stmts, eval last, print.
    Multi-line ending in statement -> exec all, no auto-print.
    Exceptions are NOT caught here -- caller decides how to handle.
    """
    try:
        r = eval(compile(src, "<cell>", "eval"), ns)
        if r is not None:
            print(r if isinstance(r, str) else repr(r))
        return
    except SyntaxError:
        pass
    tree = _ast.parse(src, "<cell>")
    last = tree.body[-1] if tree.body else None
    if isinstance(last, _ast.Expr):
        stmts = _ast.Module(body=tree.body[:-1], type_ignores=[])
        _ast.fix_missing_locations(stmts)
        exec(compile(stmts, "<cell>", "exec"), ns)
        expr = _ast.Expression(body=last.value)
        _ast.fix_missing_locations(expr)
        r = eval(compile(expr, "<cell>", "eval"), ns)
        if r is not None:
            print(r if isinstance(r, str) else repr(r))
    else:
        _ast.fix_missing_locations(tree)
        exec(compile(tree, "<cell>", "exec"), ns)


def _make_exec(
    ns: JsonDict,
    lock: threading.Lock,
) -> typing.Callable[[str], _ExecOutput]:
    """Build _exec(src): eval/exec in ns and return captured output.

    Uses _ThreadStdout for thread-safe capture: the exec thread's output goes
    to the cell buffer; child threads spawned by user code write to the real
    stdout instead of bleeding into another cell's buffer.
    """
    # Keep stable wrappers; user code can assign sys.stdout/sys.stderr.
    stdout_wrapper = (
        sys.stdout if isinstance(sys.stdout, _ThreadStdout)
        else _ThreadStdout(sys.stdout)
    )
    stderr_wrapper = (
        sys.stderr if isinstance(sys.stderr, _ThreadStdout)
        else _ThreadStdout(sys.stderr)
    )
    sys.stdout = stdout_wrapper
    sys.stderr = stderr_wrapper

    def _exec(src: str) -> _ExecOutput:
        with lock:
            buf = io.StringIO()
            had_error = False
            sys.stdout = stdout_wrapper
            sys.stderr = stderr_wrapper
            stdout_wrapper._local.buf = buf
            stderr_wrapper._local.buf = buf
            try:
                _eval_exec_cell(src, ns)
            except KeyboardInterrupt:
                had_error = True
                traceback.print_exc()
            except SystemExit as e:
                had_error = True
                code_val = e.code if e.code is not None else 0
                print(f"exit({code_val})")
            except Exception:
                had_error = True
                traceback.print_exc()
            finally:
                stdout_wrapper._local.buf = None
                stderr_wrapper._local.buf = None
                sys.stdout = stdout_wrapper
                sys.stderr = stderr_wrapper
            return _ExecOutput(buf.getvalue().rstrip("\n"), had_error)
    return _exec

# Per-session in practice: each session runs in its own subprocess,
# so each process gets its own copy of this lock and cells dict.
_cells_lock = threading.Lock()


def _evict_stale_cells(cells: dict[str, JsonDict]) -> None:
    """Remove cells done > 5 minutes ago.  Caller must hold _cells_lock."""
    now = time.time()
    stale = [k for k, v in cells.items()
             if v["status"] == "done"
             and now - v.get("_done_at", now) > _ASYNC_CELL_TTL]
    for k in stale:
        del cells[k]


def _write_all(fd: int, data: bytes) -> None:
    """Write all bytes to fd.  os.write() may do partial writes on large data."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n == 0:
            raise OSError("pipe write returned 0")
        view = view[n:]


@contextlib.contextmanager
def _locked(lock: threading.Lock | None) -> typing.Iterator[None]:
    if lock is None:
        yield
    else:
        with lock:
            yield


def _public_names(ns: JsonDict, lock: threading.Lock | None) -> list[str]:
    with _locked(lock):
        return [v for v in ns if not v.startswith("_")]


def _kill_running_fork_pgids(cells: dict[str, JsonDict]) -> int:
    """Best-effort cleanup for live fork cell process groups."""
    if sys.platform == "win32":
        return 0
    killed = 0
    with _INTERRUPT_LOCK:
        with _cells_lock:
            snapshot = list(cells.values())
        for r in snapshot:
            if r.get("status") != "running":
                continue
            pid = r.get("pid")
            pgid = r.get("pgid")
            if not pid:
                continue
            try:
                if pgid:
                    os.killpg(int(pgid), signal.SIGKILL)  # type: ignore[attr-defined]
                else:
                    os.kill(int(pid), signal.SIGKILL)  # type: ignore[attr-defined]
                killed += 1
            except ProcessLookupError:
                try:
                    os.kill(int(pid), signal.SIGKILL)  # type: ignore[attr-defined]
                    killed += 1
                except (OSError, ProcessLookupError):
                    pass  # already dead
            except (OSError, ProcessLookupError):
                pass  # already dead or not killable
    return killed


def _dispatch(
    cmd: str,
    args: list[str],
    _exec: typing.Callable[[str], _ExecOutput],
    cells: dict[str, JsonDict],
    ns: JsonDict,
    lock: threading.Lock | None = None,
) -> JsonDict:
    """Handle one command inside a session worker.  Returns dicts only.
    lock, when provided, serializes fork merge with exec to prevent races."""
    if cmd in ("run", "fire", "fork") and not args:
        return {"error": f"{cmd} requires code"}
    if cmd == "run":
        out = _exec(args[0])
        return {"output": str(out), "_error": bool(getattr(out, "error", False))}
    elif cmd == "fire":
        # threading.Thread: shares the session namespace, so fire'd code can
        # set variables later calls read.  Tradeoff: threads can't be
        # force-killed when stuck in C code.  kill (whole session) is the escape.
        if not args or not str(args[0]).strip():
            return {"error": "fire requires code"}
        cid = uuid.uuid4().hex[:12]
        res = {"output": "", "status": "running", "tid": None,
               "_seq": next(_CELL_SEQ)}
        def _bg(c: str = args[0], r: JsonDict = res) -> None:
            output = "(fire result failed)"
            error = True
            try:
                out = _exec(c)
                output = str(out)
                error = bool(getattr(out, "error", False))
            except BaseException:
                try:
                    output = traceback.format_exc().rstrip()
                except Exception:
                    output = "(traceback formatting failed)"
                error = True
            finally:
                with _cells_lock:
                    r["output"] = output
                    r["_error"] = error
                    r["status"] = "done"
                    r["_done_at"] = time.time()
                    r["tid"] = None
        t = threading.Thread(target=_bg, daemon=True)
        with _cells_lock:
            t.start()
            if res["status"] == "running":
                res["tid"] = t.ident
            cells[cid] = res
            _evict_stale_cells(cells)
        return {"cell_id": cid, "status": "fired"}
    elif cmd == "fork":
        # os.fork() child process.  Child gets a COW copy of the namespace,
        # runs the code, diffs by id(), pickles new/changed vars back through
        # a pipe; parent merges.  Unpicklable objects are skipped.
        # Diff uses id(): in-place mutations don't change id() so they won't
        # be detected -- use assignment.  Assumes CPython (id = address).
        # POSIX fork in a multithreaded process: child inherits only the
        # calling thread; locks held by other threads stay locked forever.
        # Fine for pure-Python data; risky after loading native-thread
        # runtimes (OpenMP, BLAS, CUDA).  kill/recreate the session to escape.
        if sys.platform == "win32":
            return {"error": "fork not supported on Windows (no COW fork)"}
        if not args or not str(args[0]).strip():
            return {"error": "fork requires code"}
        cid = uuid.uuid4().hex[:12]
        # os.fork() + os._exit(), not mp.Process: mp does Python cleanup after
        # fork (join threads, atexit, flush) which deadlocks on locks held by
        # threads that don't exist in the child.  os._exit() skips all of it.
        r_fd, w_fd = os.pipe()
        child_pid = -1
        fork_locked = False
        try:
            # Prevent fork child's subprocesses from inheriting pipe fds;
            # a grandchild holding w_fd open means the parent never sees EOF.
            try:
                os.set_inheritable(r_fd, False)
                os.set_inheritable(w_fd, False)
            except OSError as e:
                print(f"WARN: set_inheritable failed: {e}", file=sys.stderr)
            # Snapshot and fork under the same lock so the diff base and child
            # image match.
            if lock:
                lock.acquire()
                fork_locked = True
            ns_snap = {k: id(v) for k, v in ns.items()}
            child_pid = os.fork()
        except BaseException:
            if fork_locked and lock:
                lock.release()
            for fd in (r_fd, w_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        if child_pid != 0 and fork_locked and lock:
            lock.release()
        if child_pid == 0:
            # --- child process (exits via os._exit, no Python cleanup) ---
            if fork_locked and lock:
                lock.release()
            os.close(r_fd)
            try:
                os.setsid()  # type: ignore[attr-defined]
            except OSError:
                try:
                    payload = pickle.dumps({
                        "output": "fork child setsid failed",
                        "_error": True,
                        "diff": {},
                        "skipped": [],
                    })
                    _write_all(w_fd, payload)
                except Exception:
                    pass  # child is dying anyway
                finally:
                    try:
                        os.close(w_fd)
                    except OSError:
                        pass
                    os._exit(1)
            try:
                buf = io.StringIO()
                sys.stdout = sys.stderr = buf  # capture all output
                had_error = False
                try:
                    _eval_exec_cell(args[0], ns)
                except SystemExit as e:
                    had_error = True
                    code_val = e.code if e.code is not None else 0
                    print(f"exit({code_val})")
                except BaseException:
                    had_error = True
                    traceback.print_exc()
                output = buf.getvalue().rstrip("\n")
                # diff: new or changed vars (by identity)
                diff = {}
                skipped = []
                for k, v in ns.items():
                    if k.startswith("_"):
                        continue
                    if k not in ns_snap or id(v) != ns_snap[k]:
                        try:
                            pickle.dumps(v)
                            diff[k] = v
                        except Exception:
                            skipped.append(k)
                payload = pickle.dumps({"output": output, "_error": had_error,
                                        "diff": diff,
                                        "skipped": skipped})
                _write_all(w_fd, payload)
            except BaseException:
                try:
                    payload = pickle.dumps({"output": traceback.format_exc(),
                                            "_error": True,
                                            "diff": {}, "skipped": []})
                    _write_all(w_fd, payload)
                except Exception:
                    pass  # child is dying anyway
            finally:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
                os._exit(0)  # skip all Python cleanup -- no deadlocks
        # --- parent process ---
        os.close(w_fd)
        res = {"output": "", "status": "running", "pid": child_pid,
               "pgid": child_pid,
               "_seq": next(_CELL_SEQ)}
        def _fork_monitor(r: JsonDict = res, fd: int = r_fd, pid: int = child_pid) -> None:
            """Read pipe first (unblocks child write), then reap child."""
            # Must read before waitpid: a large payload (> pipe buffer) blocks
            # the child's write; waitpid first would deadlock.
            chunks = []
            total = 0
            too_large = False
            try:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_WORKER_RESPONSE:
                        too_large = True
                        continue
                    chunks.append(chunk)
            except OSError as e:
                print(f"WARN: fork pipe broken: {e}", file=sys.stderr)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass  # already reaped
            output = ""
            had_error = False
            merged_keys: list[str] = []
            skipped: list[str] = []
            try:
                if too_large:
                    output = "fork result too large; use smaller output"
                    had_error = True
                elif chunks:
                    # Trust boundary: the child runs arbitrary user code
                    # already; pickle adds no new capability.
                    data = pickle.loads(b"".join(chunks))
                    output = data.get("output", "")
                    had_error = bool(data.get("_error", False))
                    merged = data.get("diff", {})
                    if had_error:
                        merged = {}
                    else:
                        with _locked(lock):
                            ns.update(merged)
                    merged_keys = list(merged.keys())
                    skipped = data.get("skipped", [])
                else:
                    output = "(killed)"
                    had_error = True
            except (EOFError, OSError, pickle.UnpicklingError):
                output = r.get("output", "") or "(killed)"
                had_error = True
            except Exception:
                output = r.get("output", "") or "(fork result read failed)"
                had_error = True
            finally:
                with _cells_lock:
                    r["pid"] = None
                    r["output"] = output
                    r["_error"] = had_error
                    r["_merged"] = merged_keys
                    r["_skipped"] = skipped
                    r["status"] = "done"
                    r["_done_at"] = time.time()
        with _cells_lock:
            cells[cid] = res
            _evict_stale_cells(cells)
        threading.Thread(target=_fork_monitor, daemon=True).start()
        return {"cell_id": cid, "status": "forked"}
    elif cmd == "int":
        # fire'd cells (threads): SetAsyncExc -- best-effort, Python bytecode
        # only; C code won't see it until it returns to Python.
        # fork'd cells (processes): SIGKILL -- stops anything.
        # run blocks the worker loop, so int can't reach it; fork risky code.
        threads = 0
        processes = 0
        with _INTERRUPT_LOCK:
            with _cells_lock:
                snapshot = list(cells.items())
            for cid, r in snapshot:
                if r["status"] != "running":
                    continue
                tid = r.get("tid")
                pid = r.get("pid")
                pgid = r.get("pgid")
                if tid:
                    rc = _SET_ASYNC_EXC(
                        ctypes.c_ulong(tid),
                        ctypes.py_object(KeyboardInterrupt),
                    )
                    if rc > 1:
                        _SET_ASYNC_EXC(ctypes.c_ulong(tid), None)
                    if rc >= 1:
                        threads += 1
                elif pid:
                    try:
                        if pgid:
                            os.killpg(int(pgid), signal.SIGKILL)  # type: ignore[attr-defined]
                        else:
                            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
                        processes += 1
                    except ProcessLookupError:
                        try:
                            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
                            processes += 1
                        except (OSError, ProcessLookupError):
                            pass  # already dead
                    except (OSError, ProcessLookupError):
                        pass  # already dead
        return {"threads": threads, "processes": processes,
                "note": "thread interrupts are best-effort; "
                        "fork processes are hard-killed"}
    elif cmd == "poll":
        target = args[0] if args else None
        if target:
            with _cells_lock:
                cell = cells.get(target)  # lookup before evict: grace period
                _evict_stale_cells(cells)
                if cell is not None:
                    cell = dict(cell)
            if cell is None:
                return {"cell_id": target, "status": "error",
                        "output": "unknown cell"}
            resp = {"cell_id": target, "status": cell["status"],
                    "output": cell["output"]}
            if cell.get("_error"):
                resp["_error"] = True
            if "_merged" in cell:
                resp["merged"] = cell["_merged"]
                resp["skipped"] = cell["_skipped"]
            return resp
        with _cells_lock:
            _evict_stale_cells(cells)
            if not cells:
                return {"status": "idle"}
            last_id, r = max(
                cells.items(),
                key=lambda item: typing.cast(int, item[1].get("_seq", -1)),
            )
            r = dict(r)
        resp = {"cell_id": last_id, "status": r["status"],
                "output": r["output"]}
        if r.get("_error"):
            resp["_error"] = True
        if "_merged" in r:
            resp["merged"] = r["_merged"]
            resp["skipped"] = r["_skipped"]
        return resp
    elif cmd == "status":
        vs = len(_public_names(ns, lock))
        with _cells_lock:
            _evict_stale_cells(cells)
            running = [cid for cid, r in cells.items()
                       if r["status"] == "running"]
            ncells = len(cells)
        return {"state": "running" if running else "idle",
                "running": running, "vars": vs, "cells": ncells}
    elif cmd == "vars":
        return {"vars": _public_names(ns, lock)}
    elif cmd == "complete":
        import rlcompleter
        text = args[0] if args else ""
        with _locked(lock):
            ns_snapshot = dict(ns)
        c = rlcompleter.Completer(ns_snapshot)
        matches: list[str] = []
        for i in range(200):
            try:
                m = c.complete(text, i)
            except Exception:
                return {"matches": matches, "_error": True}
            if m is None:
                break
            matches.append(m)
        return {"matches": matches}
    return {"error": f"unknown cmd: {cmd}"}

# =============================================
# SESSION WORKER (one plain subprocess)
# =============================================

def _worker_main() -> None:
    """Session worker: JSON-lines over stdin/stdout, namespace lives here.

    The protocol channel is duplicated away from fd 0/1 first, then fd 1 is
    pointed at stderr: stray prints from user threads and subprocesses land in
    the daemon's stderr instead of corrupting the protocol stream.
    """
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)  # user-code fd-1 writes go to daemon stderr

    ns = _init_namespace()
    cells: dict[str, JsonDict] = {}
    lock = threading.Lock()

    if sys.platform != "win32":
        def _term_handler(_signum: int, _frame: object) -> None:
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _term_handler)

    _exec = _make_exec(ns, lock)
    proto_out.write(json.dumps({"ready": True, "pid": os.getpid()}) + "\n")
    proto_out.flush()
    try:
        for line in proto_in:
            try:
                msg = json.loads(line)
                resp = _dispatch(msg["cmd"], msg.get("args", []),
                                 _exec, cells, ns, lock)
                payload = json.dumps(resp)
            except (json.JSONDecodeError, KeyError, TypeError):
                payload = json.dumps({"error": "worker protocol error"})
            except Exception:
                payload = json.dumps({"error": "worker protocol error"})
            try:
                proto_out.write(payload + "\n")
                proto_out.flush()
            except OSError:
                break
    except KeyboardInterrupt:
        pass
    finally:
        _kill_running_fork_pgids(cells)

# =============================================
# DAEMON -- session manager + local HTTP
# =============================================

sessions: dict[str, JsonDict] = {}
_sessions_lock = threading.Lock()
_daemon_token: str | None = None
_daemon_server: typing.Any = None


def _get_session(name: str) -> JsonDict | None:
    with _sessions_lock:
        return sessions.get(name)


def _publish_session(name: str, s: JsonDict) -> None:
    _validate_session_name(name)
    with _sessions_lock:
        if name not in sessions and len(sessions) >= _MAX_SESSIONS:
            raise RuntimeError(f"too many sessions (max {_MAX_SESSIONS})")
        old = sessions.get(name)
        sessions[name] = s
    if old is not None and old is not s:
        _close_session(old)


def new_session(name: str) -> JsonDict:
    """Create or replace one named Python session (a plain subprocess)."""
    _validate_session_name(name)
    with _sessions_lock:
        if name not in sessions and len(sessions) >= _MAX_SESSIONS:
            raise RuntimeError(f"too many sessions (max {_MAX_SESSIONS})")
    env = {**os.environ, _WORKER_ENV: "1"}
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
        start_new_session=(sys.platform != "win32"),
    )
    out_q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out_q.put(line)
        except Exception:
            pass  # worker died mid-line
        out_q.put(None)  # EOF sentinel

    threading.Thread(target=_reader, daemon=True).start()
    s: JsonDict = {"proc": proc, "q": out_q, "lock": threading.Lock(),
                   "unhealthy": False, "async_src": {}}
    try:
        line = out_q.get(timeout=_SESSION_READY_TIMEOUT)
        if line is None or not json.loads(line).get("ready"):
            raise RuntimeError("worker failed to start")
    except (queue.Empty, json.JSONDecodeError):
        with contextlib.suppress(Exception):
            proc.kill()
        raise RuntimeError("worker failed to start")
    try:
        _publish_session(name, s)
    except Exception:
        _close_session(s)
        raise
    threading.Thread(target=_monitor_session, args=(name, s),
                     daemon=True).start()
    return s


def _monitor_session(name: str, s: JsonDict) -> None:
    """Reap the session from the map when its worker exits."""
    s["proc"].wait()
    with _sessions_lock:
        if sessions.get(name) is s:
            sessions.pop(name, None)
        else:
            return
    _close_session(s)


def _close_session(s: JsonDict) -> None:
    proc = s["proc"]
    if proc.poll() is None:
        try:
            if sys.platform != "win32":
                # Kill the worker's process group (worker + stray children).
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # type: ignore[attr-defined]
            else:
                proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                if sys.platform != "win32":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[attr-defined]
                else:
                    proc.kill()
                proc.wait(timeout=1)
    for f in (proc.stdin, proc.stdout):
        if f is not None:
            with contextlib.suppress(Exception):
                f.close()


def kill_session(name: str) -> bool:
    with _sessions_lock:
        s = sessions.pop(name, None)
    if s is None:
        return False
    _close_session(s)
    return True


def send_session(name: str, cmd: str, args: list[str],
                 timeout: float = _SEND_TIMEOUT) -> JsonDict:
    """Send one command to a session worker and wait for its response.
    A per-session lock serializes concurrent callers, so an aborted client
    cannot desynchronize the request/response channel."""
    s = _get_session(name)
    if s is None:
        return {"error": f"no session '{name}' -- create it first: new {name}"}
    with s["lock"]:
        if _get_session(name) is not s:
            return {"error": f"no session '{name}'"}
        if s["unhealthy"]:
            return {"error": f"session '{name}' command channel out of sync "
                             f"after timeout; use kill {name}"}
        proc = s["proc"]
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"cmd": cmd, "args": args}) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            return {"error": f"session '{name}' dead -- new {name} to restart"}
        try:
            line = s["q"].get(timeout=timeout)
        except queue.Empty:
            s["unhealthy"] = True
            return {"error": "timeout -- command channel may be out of sync; "
                             f"use int {name} or kill {name} if stuck"}
        if line is None:
            return {"error": f"session '{name}' dead -- new {name} to restart"}
        if len(line) > _MAX_WORKER_RESPONSE:
            s["unhealthy"] = True
            return {"error": f"worker response too large; use kill {name} to restart"}
        try:
            return typing.cast(JsonDict, json.loads(line))
        except json.JSONDecodeError:
            s["unhealthy"] = True
            return {"error": f"malformed worker response; use kill {name} to restart"}


def _list_sessions() -> str:
    with _sessions_lock:
        snapshot = list(sessions.items())
    lines = []
    for n, s in snapshot:
        proc = s["proc"]
        alive = "DEAD" if proc.poll() is not None else "alive"
        lines.append(f"  {n}: {alive} pid={proc.pid}")
    return "\n".join(lines) or "(no sessions)"


# _async_src: retained until poll pops it; dropped with the session
def _note_async_launch(name: str, src: str, resp: JsonDict) -> None:
    cid = resp.get("cell_id")
    s = _get_session(name)
    if cid and s is not None:
        s["async_src"][cid] = src


def _note_async_poll(name: str, resp: JsonDict, exec_error: bool) -> None:
    s = _get_session(name)
    if s is None:
        return
    src = s["async_src"].pop(resp.get("cell_id"), None)
    if src and not exec_error and src.strip():
        _log_history(name, src)

# -----------------------------------------------
# HTTP layer (stdlib; curl is the debug client)
# -----------------------------------------------

_SESSION_CMDS = {"run", "fire", "fork", "poll", "int",
                 "status", "vars", "complete"}


def _daemon_command(method: str, cmd: str, name: str, query: dict[str, list[str]],
                    body: str) -> tuple[int, dict[str, str], str]:
    """Execute one HTTP command.  Returns (status, extra_headers, body_text)."""
    headers: dict[str, str] = {}
    if cmd == "ls" and method == "GET":
        return 200, headers, _list_sessions()
    if cmd == "stop" and method == "POST":
        server = _daemon_server
        if server is not None:
            def _delayed_shutdown() -> None:
                time.sleep(0.2)  # let the response flush first
                server.shutdown()
            threading.Thread(target=_delayed_shutdown, daemon=True).start()
        return 200, {"Connection": "close"}, "OK stopping daemon"
    if not name:
        return 404, headers, f"ERR unknown: {cmd}"
    try:
        _validate_session_name(name)
    except ValueError:
        return 400, headers, f"ERR invalid session name. {_SESSION_NAME_RULE}"
    if cmd == "new" and method == "POST":
        try:
            s = new_session(name)
        except (ValueError, RuntimeError) as e:
            return 409, headers, f"ERR {_public_error(e)}"
        return 200, headers, f"OK {name} pid={s['proc'].pid}"
    if cmd == "kill" and method == "POST":
        if kill_session(name):
            return 200, headers, f"OK killed {name}"
        return 404, headers, f"ERR no session '{name}'"
    if cmd not in _SESSION_CMDS:
        return 404, headers, f"ERR unknown: {cmd}"
    if (cmd in ("run", "fire", "fork", "complete", "int")) != (method == "POST"):
        return 405, headers, "ERR wrong method"

    args: list[str] = []
    if cmd in ("run", "fire", "fork", "complete"):
        args = [body]
    elif cmd == "poll":
        cell = query.get("cell", [""])[0]
        if cell:
            args = [cell]
    resp = send_session(name, cmd, args)

    if "error" in resp and "_error" not in resp:
        msg = str(resp["error"])
        status = 404 if msg.startswith("no session") else 409
        return status, headers, f"ERR {msg}"

    exec_error = bool(resp.pop("_error", False))
    if cmd == "run":
        output = str(resp.get("output", ""))
        if not exec_error and body.strip():
            _log_history(name, body)
        if exec_error:
            headers["X-Pythond-Exec-Error"] = "1"
        return 200, headers, output
    if cmd in ("fire", "fork"):
        _note_async_launch(name, body, resp)
    elif cmd == "poll" and resp.get("status") == "done":
        if exec_error:
            resp["error"] = True
        _note_async_poll(name, resp, exec_error)
    if exec_error:
        headers["X-Pythond-Exec-Error"] = "1"
    return 200, headers, json.dumps(resp)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"pythond/{__version__}"

    def address_string(self) -> str:  # AF_UNIX peer is '' -- keep logs sane
        if isinstance(self.client_address, tuple) and self.client_address:
            return str(self.client_address[0])
        return "local"

    def log_message(self, fmt: str, *args: typing.Any) -> None:
        # One line per request to daemon stderr; never code bodies.
        print(f"ACCESS {self.address_string()} {fmt % args}", file=sys.stderr)

    def _reply(self, status: int, body: str,
               extra: dict[str, str] | None = None,
               content_type: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not _daemon_token:
            return True  # AF_UNIX: the socket's fs permissions are the auth
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        return hmac.compare_digest(token, _daemon_token)

    def _route(self, method: str) -> None:
        if not self._authorized():
            self._reply(401, "ERR auth failed")
            return
        parsed = urllib.parse.urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        cmd = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        query = urllib.parse.parse_qs(parsed.query)
        body = ""
        if method == "POST":
            if self.headers.get("Transfer-Encoding"):
                self._reply(411, "ERR chunked bodies not supported")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                self._reply(413, "ERR body too large")
                return
            body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            status, extra, text = _daemon_command(method, cmd, name, query, body)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._reply(500, "ERR internal error", {"Connection": "close"})
            return
        self._reply(status, text, extra)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")


class _TcpHTTPServer(ThreadingHTTPServer):
    # No SO_REUSEADDR: on Windows it would let a second daemon bind the same
    # port and split traffic between two token sets.  Fail loud instead.
    allow_reuse_address = False
    daemon_threads = True


if _HAS_AF_UNIX:
    class _UnixHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_UNIX  # type: ignore[attr-defined]
        daemon_threads = True

        def server_bind(self) -> None:
            # HTTPServer.server_bind assumes an (addr, port) tuple; skip it.
            socketserver.TCPServer.server_bind(self)
            self.server_name = "pythond"
            self.server_port = 0


def daemon(show_token: bool = False) -> None:
    """Run the daemon in the foreground.  AF_UNIX on POSIX, 127.0.0.1 on
    Windows.  It never binds a non-loopback address -- remote access is ssh's
    job (see module docstring)."""
    global _daemon_token, _daemon_server
    try:
        if _HAS_AF_UNIX:
            sock = _sock_path()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(sock)
            old_umask = os.umask(0o177)
            try:
                server = _UnixHTTPServer(sock, _Handler)
            finally:
                os.umask(old_umask)
            os.chmod(sock, 0o600)
            endpoint = sock
        else:
            port = int(os.environ.get("PYTHOND_PORT", "7984"))
            _daemon_token = secrets.token_hex(16)
            server = _TcpHTTPServer(("127.0.0.1", port), _Handler)
            _write_meta(port, _daemon_token)
            endpoint = f"http://127.0.0.1:{port}"
            if show_token:
                print(f"set PYTHOND_TOKEN={_daemon_token}", file=sys.stderr)
    except OSError as e:
        print(f"ERR cannot start daemon: {e}", file=sys.stderr)
        raise SystemExit(1)
    _daemon_server = server
    print(f"pythond {__version__} pid={os.getpid()} {endpoint}", file=sys.stderr)

    def _stop(_signum: int, _frame: typing.Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signame in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, signame):
            with contextlib.suppress(AttributeError, ValueError):
                signal.signal(getattr(signal, signame), _stop)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # normal shutdown path
    finally:
        with contextlib.suppress(Exception):
            server.server_close()
        for name in list(sessions):
            kill_session(name)
        if _HAS_AF_UNIX:
            with contextlib.suppress(OSError):
                os.unlink(_sock_path())
        else:
            _remove_meta()
        _daemon_server = None
        _daemon_token = None
        print("pythond stopped", file=sys.stderr)

# =============================================
# CLIENT
# =============================================

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 35) -> None:
        super().__init__("pythond", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        s.settimeout(self.timeout)
        s.connect(self._unix_path)
        self.sock = s


def _parse_host_port(value: str, default_port: int = 7984) -> tuple[str, int]:
    if ":" in value:
        host, _, port_s = value.rpartition(":")
        port = int(port_s)
    else:
        host = value
        port = int(os.environ.get("PYTHOND_PORT", str(default_port)))
    if not host:
        raise ValueError("host required")
    if not (1 <= port <= 65535):
        raise ValueError("port out of range")
    return host, port


def _connect() -> tuple[http.client.HTTPConnection, str | None]:
    """Connection + token for the configured daemon.

    PYTHOND_HOST targets a tunneled daemon (e.g. ssh -L); plain HTTP, so keep
    the tunnel loopback-to-loopback.  Otherwise AF_UNIX (POSIX) or the local
    TCP endpoint from daemon.json (Windows).
    """
    host = os.environ.get("PYTHOND_HOST")
    token = os.environ.get("PYTHOND_TOKEN")
    if host:
        h, port = _parse_host_port(host)
        return http.client.HTTPConnection(h, port, timeout=35), token
    if _HAS_AF_UNIX:
        return _UnixHTTPConnection(_sock_path()), None
    meta = _read_meta()
    port = int(os.environ.get("PYTHOND_PORT") or meta.get("port") or "7984")
    return (http.client.HTTPConnection("127.0.0.1", port, timeout=35),
            token or meta.get("token"))


def _request(method: str, path: str,
             body: str | None = None) -> tuple[int, dict[str, str], str]:
    """One HTTP request to the daemon.  Raises OSError-family on no daemon."""
    conn, token = _connect()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, path,
                     body=body.encode("utf-8") if body is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        return resp.status, dict(resp.getheaders()), text
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _quote(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def client(cmd: str, args: list[str], fail_on_err: bool = True) -> None:
    """CLI client.  Exit 1 on any ERR (daemon error or exec error)."""
    try:
        if cmd == "ls":
            status, _h, text = _request("GET", "/ls")
        elif cmd in ("new", "kill", "int"):
            if not args:
                print(f"ERR usage: {cmd} <name>", file=sys.stderr)
                sys.exit(1)
            status, _h, text = _request("POST", f"/{cmd}/{_quote(args[0])}")
            if cmd == "int" and status == 200:
                text = _format_int(args[0], text)
        elif cmd in ("run", "fire", "fork"):
            if len(args) < 2:
                print(f"ERR usage: {cmd} <name> <code>", file=sys.stderr)
                sys.exit(1)
            name, code = args[0], " ".join(args[1:])
            status, hdrs, text = _request("POST", f"/{cmd}/{_quote(name)}", code)
            if (cmd == "run" and status == 200
                    and hdrs.get("X-Pythond-Exec-Error") == "1"):
                print(f"ERR execution failed\n{text}", file=sys.stderr)
                sys.exit(1)
        elif cmd == "poll":
            if not args:
                print("ERR usage: poll <name> [cell_id]", file=sys.stderr)
                sys.exit(1)
            path = f"/poll/{_quote(args[0])}"
            if len(args) > 1:
                path += f"?cell={_quote(args[1])}"
            status, _h, text = _request("GET", path)
        elif cmd in ("status", "vars"):
            if not args:
                print(f"ERR usage: {cmd} <name>", file=sys.stderr)
                sys.exit(1)
            status, _h, text = _request("GET", f"/{cmd}/{_quote(args[0])}")
        elif cmd == "complete":
            if len(args) < 2:
                print("ERR usage: complete <name> <text>", file=sys.stderr)
                sys.exit(1)
            status, _h, text = _request("POST", f"/complete/{_quote(args[0])}",
                                        args[1])
        elif cmd == "stop":
            status, _h, text = _request("POST", "/stop")
        else:
            print(f"ERR unknown: {cmd}", file=sys.stderr)
            sys.exit(1)
    except (OSError, http.client.HTTPException) as e:
        print(f"ERR cannot connect: {_public_error(e)} "
              "-- start the daemon: pythond daemon", file=sys.stderr)
        sys.exit(1)
    if text:
        print(text, file=sys.stderr if status >= 400 else sys.stdout)
    if status >= 400 and fail_on_err:
        sys.exit(1)


def _format_int(name: str, text: str) -> str:
    try:
        resp = json.loads(text)
    except json.JSONDecodeError:
        return text
    t = resp.get("threads", 0)
    p = resp.get("processes", 0)
    parts = []
    if t:
        parts.append(f"{t} {'thread' if t == 1 else 'threads'} (best-effort)")
    if p:
        parts.append(f"{p} {'process' if p == 1 else 'processes'} (killed)")
    if not parts:
        return f"OK no running cells in {name}"
    return f"OK int {name}: {', '.join(parts)}"

# -----------------------------------------------
# ATTACH -- client-side line REPL
# -----------------------------------------------

def _needs_more(src: str, last_line: str) -> bool:
    """REPL continuation rule: a blank line always flushes; otherwise ask
    codeop whether the source is a complete block (compile_command treats a
    multi-line block as incomplete until it ends with a blank line)."""
    if last_line.strip() == "" and "\n" in src:
        return False
    try:
        return codeop.compile_command(src) is None
    except (SyntaxError, ValueError, OverflowError):
        return False  # let the session report the error


def attach(name: str,
           input_fn: typing.Callable[[str], str] = input) -> bool:
    """Line REPL into a session.  Readline history and tab completion live in
    this client; every complete block is one run cell in the shared namespace.
    Ctrl-D detaches; the session stays alive (kill it with pysh kill)."""
    try:
        status, _h, text = _request("GET", f"/status/{_quote(name)}")
    except (OSError, http.client.HTTPException) as e:
        print(f"ERR cannot connect: {_public_error(e)}", file=sys.stderr)
        return False
    if status != 200:
        print(text, file=sys.stderr)
        return False

    try:
        import readline  # noqa: F401  (POSIX line editing + history)

        def _complete(text_frag: str, state: int) -> str | None:
            if state == 0:
                try:
                    _s, _hh, out = _request(
                        "POST", f"/complete/{_quote(name)}", text_frag)
                    _complete.matches = json.loads(out).get("matches", [])  # type: ignore[attr-defined]
                except Exception:
                    _complete.matches = []  # type: ignore[attr-defined]
            matches = getattr(_complete, "matches", [])
            return matches[state] if state < len(matches) else None

        readline.set_completer(_complete)
        readline.parse_and_bind("tab: complete")
    except ImportError:
        pass  # optional -- plain input() still works

    print(f"pysh: attached to \"{name}\" -- shared namespace, "
          "Ctrl-D detaches, pysh kill ends the session", file=sys.stderr)
    buf: list[str] = []
    while True:
        try:
            line = input_fn("... " if buf else ">>> ")
        except EOFError:
            print(file=sys.stderr)
            break
        except KeyboardInterrupt:
            buf = []
            print("\nKeyboardInterrupt", file=sys.stderr)
            continue
        if not buf and not line.strip():
            continue
        buf.append(line)
        src = "\n".join(buf)
        if _needs_more(src, line):
            continue
        buf = []
        try:
            _status, _hh, out = _request("POST", f"/run/{_quote(name)}", src)
        except (OSError, http.client.HTTPException) as e:
            print(f"ERR session request failed: {_public_error(e)}",
                  file=sys.stderr)
            return False
        if out:
            print(out)
    return True

# =============================================
# ENTRY POINTS
# =============================================

def _add_session_subparsers(sub: argparse._SubParsersAction) -> None:
    p_attach = sub.add_parser("attach", help="line REPL into session")
    p_attach.add_argument("name", nargs="?", default="default")
    p_new = sub.add_parser("new", help="create session",
                           description=_SESSION_NAME_RULE)
    p_new.add_argument("name", help="canonical lowercase session name")
    for cname, chelp in (
        ("run", "sync exec, raw output"),
        ("fire", "async thread exec"),
        ("fork", "async process exec"),
    ):
        p_cmd = sub.add_parser(cname, help=chelp)
        p_cmd.add_argument("name")
        p_cmd.add_argument("code", nargs=argparse.REMAINDER)
    p_poll = sub.add_parser("poll", help="check async result")
    p_poll.add_argument("name")
    p_poll.add_argument("cell_id", nargs="?")
    for cname, chelp in (
        ("int", "interrupt running cells"),
        ("kill", "terminate session"),
        ("status", "session health"),
        ("vars", "namespace names"),
    ):
        p_cmd = sub.add_parser(cname, help=chelp)
        p_cmd.add_argument("name")
    sub.add_parser("ls", help="list sessions")
    p_complete = sub.add_parser("complete", help="tab completions")
    p_complete.add_argument("name")
    p_complete.add_argument("text")


def _run_session_command(args: argparse.Namespace, argv: list[str]) -> None:
    if args.command == "attach":
        if not attach(args.name):
            sys.exit(1)
        return
    client(args.command, argv[1:])


def main() -> None:
    """Entry point for `pythond` -- daemon plus full command set."""
    argv = sys.argv[1:]
    if argv and argv[0] == "_worker":
        if os.environ.get(_WORKER_ENV) != "1":
            print("ERR internal worker entry point", file=sys.stderr)
            sys.exit(1)
        _worker_main()
        return

    parser = argparse.ArgumentParser(
        prog="pythond",
        description="Persistent Python session daemon.",
        epilog=f"Use pysh for sessions and pyctl for the daemon. {_SESSION_NAME_RULE}",
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"pythond {__version__}")
    sub = parser.add_subparsers(dest="command")
    p_daemon = sub.add_parser("daemon", help="start daemon in foreground")
    p_daemon.add_argument("--show-token", action="store_true",
                          help="print auth token (Windows local TCP)")
    _add_session_subparsers(sub)

    if not argv:
        parser.print_help()
        sys.exit(0)
    if argv[0] == "version":
        print(f"pythond {__version__}")
        sys.exit(0)
    args = parser.parse_args(argv)
    if args.command == "daemon":
        daemon(show_token=args.show_token)
    else:
        _run_session_command(args, argv)


def pysh_main() -> None:
    """Entry point for `pysh` -- session commands."""
    argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="pysh",
        description="Client for pythond sessions.",
        epilog=f"{_SESSION_NAME_RULE} Remote daemons: ssh host pysh ...",
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"pythond {__version__}")
    sub = parser.add_subparsers(dest="command")
    _add_session_subparsers(sub)

    if not argv:
        parser.print_help()
        sys.exit(0)
    if argv[0] == "version":
        print(f"pythond {__version__}")
        sys.exit(0)
    args = parser.parse_args(argv)
    _run_session_command(args, argv)


def pyctl_main() -> None:
    """Entry point for `pyctl` -- daemon lifecycle."""
    argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="pyctl",
        description="pythond daemon control.",
        epilog="pysh manages sessions; remote access is ssh host pysh ...",
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"pythond {__version__}")
    sub = parser.add_subparsers(dest="command")
    p_start = sub.add_parser("start", help="start daemon in foreground")
    p_start.add_argument("--show-token", action="store_true")
    sub.add_parser("stop", help="stop daemon gracefully")
    sub.add_parser("status", help="daemon liveness")

    if not argv:
        parser.print_help()
        sys.exit(0)
    if argv[0] == "version":
        print(f"pythond {__version__}")
        sys.exit(0)
    args = parser.parse_args(argv)
    if args.command == "start":
        daemon(show_token=args.show_token)
    elif args.command == "stop":
        client("stop", [])
    elif args.command == "status":
        endpoint = (os.environ.get("PYTHOND_HOST")
                    or (_sock_path() if _HAS_AF_UNIX
                        else f"127.0.0.1:{_read_meta().get('port', '?')}"))
        try:
            status, _h, _t = _request("GET", "/ls")
            alive = status == 200
        except Exception as e:
            print(f"endpoint: {endpoint}")
            print("alive: False")
            print(f"error: {_public_error(e)}")
            sys.exit(1)
        print(f"endpoint: {endpoint}")
        print(f"alive: {alive}")
        if not alive:
            sys.exit(1)
    else:
        parser.print_help(sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
