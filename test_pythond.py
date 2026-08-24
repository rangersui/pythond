#!/usr/bin/env python3
"""pythond test suite -- unit + integration.

Run:  python -B test_pythond.py

Unit tests run everywhere.  Integration tests start a real daemon subprocess:
AF_UNIX socket on POSIX, 127.0.0.1 + token on Windows -- both paths are
exercised by CI's OS matrix.
"""
import json
import io
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import queue
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
assert (ROOT / "pythond.py").exists(), f"pythond.py not found in {ROOT}"
sys.path.insert(0, str(ROOT))
import pythond

_HAS_AF_UNIX = pythond._HAS_AF_UNIX

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        # sys.__stdout__: several tests mock sys.stdout, and a failure inside
        # such a block must never be swallowed by the mock.
        print(f"  X {name}", file=sys.__stdout__)
        if detail:
            print(f"    {detail}", file=sys.__stdout__)
        FAIL += 1


def section(title):
    print(f"\n--- {title} ---")


# ===========================================
# UNIT TESTS (no daemon needed)
# ===========================================

def test_version():
    section("version")
    check("version is string", isinstance(pythond.__version__, str))
    check("version is 0.5.0", pythond.__version__ == "0.5.0")


def test_zero_dependencies():
    section("zero dependencies")
    src = (ROOT / "pythond.py").read_text(encoding="utf-8")
    for gone in ("websockets", "wsproto", "cryptography", "winpty", "ssl"):
        check(f"no {gone} import",
              f"import {gone}" not in src and f"from {gone}" not in src)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    check("pyproject has no runtime deps", "dependencies = []" in pyproject)


def test_session_name_validation():
    section("session name validation")
    for name in ("work", "work-1", "work_1", "a0"):
        check(f"accept '{name}'", pythond._validate_session_name(name) == name)
    bad = ["../etc", "foo/bar", "a\\b", "x\0y", "", ".", "..",
           "work.", "Work", "WORK", "work.name",
           "con", "CON", "nul", "prn", "aux", "com1", "lpt9", "con.txt"]
    for name in bad:
        try:
            pythond._validate_session_name(name)
            check(f"reject '{name[:20]}'", False)
        except ValueError:
            check(f"reject '{name[:20]}'", True)


def test_parse_host_port():
    section("_parse_host_port")
    check("explicit port",
          pythond._parse_host_port("example.com:443") == ("example.com", 443))
    check("default port",
          pythond._parse_host_port("example.com", default_port=1234) ==
          ("example.com", 1234))
    for value in ("example.com:0", "example.com:65536", ":8080"):
        try:
            pythond._parse_host_port(value)
            check(f"reject {value}", False)
        except ValueError:
            check(f"reject {value}", True)


def test_init_namespace():
    section("_init_namespace")
    ns = pythond._init_namespace()
    check("has builtins", "__builtins__" in ns)
    for mod in ("os", "sys", "json", "subprocess", "re", "sqlite3"):
        check(f"has {mod}", mod in ns, f"missing {mod}")


def test_make_exec_eval():
    section("_make_exec eval")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    check("eval returns repr", _exec("1 + 1") == "2")
    check("eval string returns raw", _exec("'hello'") == "hello")
    check("eval None empty", _exec("None") == "")


def test_make_exec_exec():
    section("_make_exec exec")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    check("exec no output", _exec("x = 42") == "")
    check("exec set var", ns.get("x") == 42)
    check("exec print captured", _exec("print('hello world')") == "hello world")


def test_make_exec_last_expr():
    section("_make_exec auto-print last expression")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    check("last expr auto-print", _exec("x = 6\ny = 7\nx * y") == "42")
    check("last assign no print", _exec("a = 1\nb = 2") == "")
    out = _exec("def fib(n):\n  a, b = 0, 1\n  for _ in range(n): a, b = b, a+b\n  return a\nfib(10)")
    check("func def + last call", out == "55")
    check("single expr still works", _exec("100 + 23") == "123")


def test_make_exec_error():
    section("_make_exec error handling")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    out = _exec("1/0")
    check("exception has traceback", "ZeroDivisionError" in out)
    check("error flag set", out.error is True)
    check("statement via exec", _exec("if True: pass") == "")
    check("KeyboardInterrupt caught", "KeyboardInterrupt" in _exec("raise KeyboardInterrupt"))
    check("SystemExit caught", "exit(42)" in _exec("raise SystemExit(42)"))


def test_make_exec_thread_isolation():
    section("_make_exec thread-local stdout")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    code = (
        "import threading, time\n"
        "def _bg():\n"
        "    time.sleep(0.05)\n"
        "    print('from_child_thread')\n"
        "t = threading.Thread(target=_bg)\n"
        "t.start()\n"
        "time.sleep(0.1)\n"
        "t.join()\n"
        "print('from_main')\n"
    )
    output = _exec(code)
    check("main thread captured", "from_main" in output)
    check("child thread NOT in cell", "from_child_thread" not in output, output)
    check("normal capture works", _exec("print('normal')").strip() == "normal")


def test_make_exec_restores_replaced_stdio():
    section("_make_exec restores replaced stdio")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    _exec("import sys, io\nsys.stdout = io.StringIO()\nsys.stderr = io.StringIO()")
    out = _exec("print('still captured')")
    check("stdout replacement recovered", out == "still captured", out)
    check("global stdout wrapper restored", isinstance(sys.stdout, pythond._ThreadStdout))
    check("global stderr wrapper restored", isinstance(sys.stderr, pythond._ThreadStdout))


def test_thread_stdout_compat_methods():
    section("_ThreadStdout compat methods")
    real = io.StringIO()
    wrapper = pythond._ThreadStdout(real)
    wrapper.writelines(["a", "b"])
    check("writelines forwards", real.getvalue() == "ab")
    check("isatty forwards", wrapper.isatty() is False)
    buf = io.StringIO()
    wrapper._local.buf = buf
    try:
        wrapper.write("captured")
    finally:
        wrapper._local.buf = None
    check("cell buffer captures", buf.getvalue() == "captured")


def test_dispatch_run():
    section("_dispatch run")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    resp = pythond._dispatch("run", ["2+2"], _exec, cells, ns)
    check("run output", resp["output"] == "4")
    check("run success error flag false", resp.get("_error") is False)
    resp = pythond._dispatch("run", ["print('Traceback')"], _exec, cells, ns)
    check("literal Traceback not error",
          resp["output"] == "Traceback" and resp.get("_error") is False)
    resp = pythond._dispatch("run", ["1/0"], _exec, cells, ns)
    check("run exception error flag true", resp.get("_error") is True)


def test_dispatch_fire_poll():
    section("_dispatch fire+poll")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    resp = pythond._dispatch("fire", ["import time; time.sleep(0.1); x=99"],
                             _exec, cells, ns)
    check("fire has cell_id", "cell_id" in resp)
    check("fire status", resp["status"] == "fired")
    cid = resp["cell_id"]
    time.sleep(0.3)
    resp2 = pythond._dispatch("poll", [cid], _exec, cells, ns)
    check("poll done", resp2["status"] == "done")
    check("poll cell_id", resp2["cell_id"] == cid)
    check("fire set var", ns.get("x") == 99)
    check("fire cell carries tid key", "tid" in cells[cid])


def test_dispatch_async_empty_code_rejected():
    section("_dispatch async empty code rejected")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    lock = threading.Lock()
    resp = pythond._dispatch("fire", ["   "], _exec, cells, ns)
    check("fire empty rejected", resp == {"error": "fire requires code"}, resp)
    resp = pythond._dispatch("fork", ["   "], _exec, cells, ns, lock)
    if sys.platform == "win32":
        check("fork windows reports unsupported",
              resp == {"error": "fork not supported on Windows (no COW fork)"}, resp)
    else:
        check("fork empty rejected", resp == {"error": "fork requires code"}, resp)


def test_dispatch_fire_traceback_format_failure():
    section("_dispatch fire traceback fallback")
    ns = pythond._init_namespace()
    cells = {}
    def boom(src):
        raise RuntimeError("boom")
    with mock.patch.object(pythond.traceback, "format_exc",
                           side_effect=RuntimeError("format failed")):
        resp = pythond._dispatch("fire", ["x"], boom, cells, ns)
        cid = resp["cell_id"]
        for _ in range(20):
            time.sleep(0.05)
            polled = pythond._dispatch("poll", [cid], boom, cells, ns)
            if polled["status"] == "done":
                break
    check("fire completed after format failure", polled["status"] == "done", polled)
    check("fallback output", polled["output"] == "(traceback formatting failed)",
          polled)


def test_dispatch_poll_variants():
    section("_dispatch poll variants")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    check("poll empty idle",
          pythond._dispatch("poll", [], _exec, cells, ns) == {"status": "idle"})
    resp = pythond._dispatch("poll", ["nonexistent"], _exec, cells, ns)
    check("poll unknown error", resp["status"] == "error")
    pythond._dispatch("fire", ["y=1"], _exec, cells, ns)
    time.sleep(0.2)
    resp = pythond._dispatch("poll", [], _exec, cells, ns)
    check("poll latest has cell_id", "cell_id" in resp)


def test_dispatch_status_vars_complete():
    section("_dispatch status/vars/complete")
    ns = pythond._init_namespace()
    ns["myvar"] = 42
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    resp = pythond._dispatch("status", [], _exec, cells, ns)
    check("status idle", resp["state"] == "idle")
    check("status vars count", resp["vars"] >= 1)
    resp = pythond._dispatch("vars", [], _exec, cells, ns)
    check("vars includes myvar", "myvar" in resp["vars"])
    check("vars excludes private", not any(v.startswith("_") for v in resp["vars"]))
    resp = pythond._dispatch("complete", ["os.path."], _exec, cells, ns)
    check("complete has join", any("join" in m for m in resp["matches"]))


def test_dispatch_int():
    section("_dispatch int (interrupt)")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    # Python-level loop (not C-level sleep) so PyThreadState_SetAsyncExc works
    resp = pythond._dispatch("fire",
        ["[__import__('time').sleep(0.1) for _ in range(100)]"], _exec, cells, ns)
    cid = resp["cell_id"]
    time.sleep(0.3)
    resp = pythond._dispatch("int", [], _exec, cells, ns)
    check("int has threads", "threads" in resp)
    time.sleep(0.5)
    resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
    check("interrupted cell done", resp["status"] == "done")


def test_dispatch_unknown():
    section("_dispatch unknown command")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    check("unknown error", "error" in pythond._dispatch("bogus", [], _exec, {}, ns))


def test_dispatch_fork():
    section("_dispatch fork (process-based async)")
    if sys.platform == "win32":
        check("fork skipped on windows", True)
        return
    ns = pythond._init_namespace()
    ns["x"] = 10
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}

    resp = pythond._dispatch("fork", ["y = x * 2"], _exec, cells, ns, lock)
    check("fork returns cell_id", "cell_id" in resp)
    check("fork status", resp["status"] == "forked")
    cid = resp["cell_id"]
    for _ in range(20):
        time.sleep(0.2)
        resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
        if resp["status"] == "done":
            break
    check("fork done", resp["status"] == "done")
    check("fork merged y", "y" in resp.get("merged", []))
    check("y merged to namespace", ns.get("y") == 20)

    resp2 = pythond._dispatch("fork", ["print(x + y)"], _exec, cells, ns, lock)
    cid2 = resp2["cell_id"]
    for _ in range(20):
        time.sleep(0.2)
        resp2 = pythond._dispatch("poll", [cid2], _exec, cells, ns)
        if resp2["status"] == "done":
            break
    check("fork output", resp2["output"].strip() == "30")

    resp3 = pythond._dispatch("fork", ["import threading; lk = threading.Lock()"],
                              _exec, cells, ns, lock)
    cid3 = resp3["cell_id"]
    for _ in range(20):
        time.sleep(0.2)
        resp3 = pythond._dispatch("poll", [cid3], _exec, cells, ns)
        if resp3["status"] == "done":
            break
    check("fork skipped unpicklable", "lk" in resp3.get("skipped", []))


def test_dispatch_fork_kill():
    section("_dispatch fork int (killable)")
    if sys.platform == "win32":
        check("fork kill skipped on windows", True)
        return
    ns = pythond._init_namespace()
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}
    resp = pythond._dispatch("fork", ["__import__('time').sleep(30)"],
                             _exec, cells, ns, lock)
    cid = resp["cell_id"]
    time.sleep(0.5)
    resp = pythond._dispatch("int", [], _exec, cells, ns)
    check("fork int count", resp["processes"] >= 1)
    time.sleep(1)
    resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
    check("fork killed done", resp["status"] == "done")
    check("fork killed output", "killed" in resp.get("output", ""))


def test_dispatch_fork_kills_grandchildren():
    section("_dispatch fork int kills grandchildren")
    if sys.platform == "win32":
        check("fork grandchild kill skipped on windows", True)
        return
    ns = pythond._init_namespace()
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "grandchild_survived.txt"
        pidfile = Path(tmp) / "grandchild.pid"
        code = (
            "import pathlib, subprocess, sys, time\n"
            f"marker = {str(marker)!r}\n"
            f"pidfile = {str(pidfile)!r}\n"
            "child_code = \"import pathlib, time; "
            "time.sleep(2); pathlib.Path(%r).write_text('alive')\" % marker\n"
            "p = subprocess.Popen([sys.executable, '-c', child_code])\n"
            "pathlib.Path(pidfile).write_text(str(p.pid))\n"
            "time.sleep(30)\n"
        )
        resp = pythond._dispatch("fork", [code], _exec, cells, ns, lock)
        cid = resp["cell_id"]
        for _ in range(40):
            if pidfile.exists():
                break
            time.sleep(0.05)
        check("grandchild pid published", pidfile.exists())
        resp = pythond._dispatch("int", [], _exec, cells, ns)
        check("fork int counted process group", resp["processes"] >= 1, resp)
        time.sleep(2.5)
        resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
        check("fork group killed done", resp["status"] == "done", resp)
        check("grandchild did not survive", not marker.exists(),
              marker.read_text() if marker.exists() else "")


def test_fork_shutdown_cleanup_kills_grandchildren():
    section("fork shutdown cleanup kills grandchildren")
    if sys.platform == "win32":
        check("fork shutdown cleanup skipped on windows", True)
        return
    ns = pythond._init_namespace()
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "shutdown_grandchild_survived.txt"
        pidfile = Path(tmp) / "shutdown_grandchild.pid"
        code = (
            "import pathlib, subprocess, sys, time\n"
            f"marker = {str(marker)!r}\n"
            f"pidfile = {str(pidfile)!r}\n"
            "child_code = \"import pathlib, time; "
            "time.sleep(2); pathlib.Path(%r).write_text('alive')\" % marker\n"
            "p = subprocess.Popen([sys.executable, '-c', child_code])\n"
            "pathlib.Path(pidfile).write_text(str(p.pid))\n"
            "time.sleep(30)\n"
        )
        resp = pythond._dispatch("fork", [code], _exec, cells, ns, lock)
        cid = resp["cell_id"]
        for _ in range(40):
            if pidfile.exists():
                break
            time.sleep(0.05)
        check("shutdown grandchild pid published", pidfile.exists())
        killed = pythond._kill_running_fork_pgids(cells)
        check("shutdown cleanup counted fork process group", killed >= 1, killed)
        time.sleep(2.5)
        resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
        check("shutdown cleanup fork done", resp["status"] == "done", resp)
        check("shutdown grandchild did not survive", not marker.exists(),
              marker.read_text() if marker.exists() else "")


def test_dispatch_fork_large_payload():
    section("_dispatch fork large payload (pipe buffer test)")
    if sys.platform == "win32":
        check("fork large skipped on windows", True)
        return
    ns = pythond._init_namespace()
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}
    resp = pythond._dispatch("fork",
        ["big = list(range(50000))"],  # ~400KB pickled: exercises _write_all
        _exec, cells, ns, lock)
    cid = resp["cell_id"]
    for _ in range(30):
        time.sleep(0.2)
        resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
        if resp["status"] == "done":
            break
    check("fork large done", resp["status"] == "done")
    check("fork large merged", "big" in resp.get("merged", []))
    check("fork large data intact", ns.get("big") == list(range(50000)))


def test_dispatch_fork_concurrent_fire():
    section("_dispatch fork while fire running")
    if sys.platform == "win32":
        check("fork concurrent skipped on windows", True)
        return
    ns = pythond._init_namespace()
    ns["base"] = 100
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}
    pythond._dispatch("fire",
        ["import time; time.sleep(0.5); fired_val = base + 1"],
        _exec, cells, ns, lock)
    time.sleep(0.1)
    resp = pythond._dispatch("fork", ["forked_val = base + 2"],
                             _exec, cells, ns, lock)
    cid = resp["cell_id"]
    for _ in range(20):
        time.sleep(0.2)
        resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
        if resp["status"] == "done":
            break
    check("fork concurrent done", resp["status"] == "done")
    check("fork concurrent merged", "forked_val" in resp.get("merged", []))
    check("fork concurrent value", ns.get("forked_val") == 102)
    time.sleep(1)
    check("fire also completed", ns.get("fired_val") == 101)


def test_cell_eviction():
    section("cell eviction (time-based)")
    ns = pythond._init_namespace()
    _exec = pythond._make_exec(ns, threading.Lock())
    cells = {}
    resp = pythond._dispatch("fire", ["1+1"], _exec, cells, ns)
    cid = resp["cell_id"]
    time.sleep(0.2)
    resp = pythond._dispatch("poll", [cid], _exec, cells, ns)
    check("cell exists before evict", resp["status"] == "done")
    with pythond._cells_lock:
        cells[cid]["_done_at"] = time.time() - 600
    resp2 = pythond._dispatch("fire", ["2+2"], _exec, cells, ns)
    time.sleep(0.2)
    with pythond._cells_lock:
        check("stale cell evicted", cid not in cells)
    check("new cell exists", resp2["cell_id"] in cells)


def test_session_dir_and_history():
    section("_session_dir + _log_history")
    name = "__test_log_hist__"
    pythond._log_history(name, "x = 42")
    pythond._log_history(name, "y = x + 1")
    path = os.path.join(pythond._session_dir(name), "history.py")
    check("history exists", os.path.exists(path))
    if sys.platform != "win32":
        mode = os.stat(pythond._session_dir(name)).st_mode & 0o777
        check("session dir is private", mode & 0o077 == 0, oct(mode))
    content = open(path, encoding="utf-8").read()
    check("history has x", "x = 42" in content)
    check("history has y", "y = x + 1" in content)
    check("history has timestamp", "# [" in content)
    try:
        compile(content, path, "exec")
        check("history compiles", True)
    except SyntaxError as e:
        check("history compiles", False, str(e))
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                               "sessions", name), ignore_errors=True)


def test_meta_roundtrip():
    section("daemon meta read/write")
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(pythond, "_runtime_base", return_value=td):
            pythond._write_meta(9999, "testtoken")
            meta = pythond._read_meta()
            check("meta port", meta["port"] == 9999)
            check("meta token", meta["token"] == "testtoken")
            check("meta pid", meta["pid"] == os.getpid())
            pythond._remove_meta()
            check("meta removed", pythond._read_meta() == {})
    with mock.patch.object(pythond, "_meta_path",
                           return_value="/nonexistent/daemon.json"):
        check("missing returns empty", pythond._read_meta() == {})


class _FakeStdin:
    def __init__(self):
        self.sent = []
        self.broken = False
    def write(self, data):
        if self.broken:
            raise OSError("pipe closed")
        self.sent.append(data)
    def flush(self):
        pass


def _fake_session(lines=None):
    q = queue.Queue()
    for line in (lines or []):
        q.put(line)
    proc = mock.Mock()
    proc.stdin = _FakeStdin()
    proc.poll.return_value = None
    return {"proc": proc, "q": q, "lock": threading.Lock(),
            "unhealthy": False, "async_src": {}}


def _with_session(name, s):
    return mock.patch.dict(pythond.sessions, {name: s}, clear=False)


def test_send_session_timeout_marks_unhealthy():
    section("send_session timeout marks unhealthy")
    name = "__timeout__"
    s = _fake_session()
    with _with_session(name, s):
        resp = pythond.send_session(name, "status", [], timeout=0.05)
        check("timeout returns error", "timeout" in resp.get("error", ""), resp)
        check("timeout marks unhealthy", s["unhealthy"] is True)
        resp2 = pythond.send_session(name, "status", [], timeout=0.05)
        check("unhealthy session refuses reuse",
              "out of sync" in resp2.get("error", ""), resp2)
    pythond.sessions.pop(name, None)


def test_send_session_malformed_marks_unhealthy():
    section("send_session malformed response marks unhealthy")
    name = "__malformed__"
    s = _fake_session(["{bad json}\n"])
    with _with_session(name, s):
        resp = pythond.send_session(name, "status", [], timeout=1)
        check("malformed returns error",
              "malformed worker response" in resp.get("error", ""), resp)
        check("malformed marks unhealthy", s["unhealthy"] is True)
    pythond.sessions.pop(name, None)


def test_send_session_oversized_marks_unhealthy():
    section("send_session oversized response marks unhealthy")
    name = "__oversized__"
    s = _fake_session(["x" * 32 + "\n"])
    with _with_session(name, s), \
         mock.patch.object(pythond, "_MAX_WORKER_RESPONSE", 10):
        resp = pythond.send_session(name, "status", [], timeout=1)
        check("oversized returns error",
              "worker response too large" in resp.get("error", ""), resp)
        check("oversized marks unhealthy", s["unhealthy"] is True)
    pythond.sessions.pop(name, None)


def test_send_session_dead_worker():
    section("send_session dead worker")
    name = "__dead__"
    s = _fake_session([None])  # EOF sentinel
    with _with_session(name, s):
        resp = pythond.send_session(name, "status", [], timeout=1)
        check("dead worker reported", "dead" in resp.get("error", ""), resp)
    s2 = _fake_session()
    s2["proc"].stdin.broken = True
    with _with_session(name, s2):
        resp = pythond.send_session(name, "status", [], timeout=1)
        check("broken stdin reported", "dead" in resp.get("error", ""), resp)
    pythond.sessions.pop(name, None)
    check("missing session reported",
          "no session" in pythond.send_session("__nope__", "status", [])["error"])


def test_daemon_command_routing():
    section("_daemon_command routing")
    status, _h, text = pythond._daemon_command("GET", "ls", "", {}, "")
    check("ls routes", status == 200)
    status, _h, text = pythond._daemon_command("GET", "bogus", "", {}, "")
    check("unknown route 404", status == 404 and "ERR unknown" in text, text)
    status, _h, text = pythond._daemon_command("POST", "run", "Bad.Name", {}, "x")
    check("invalid name 400", status == 400 and "invalid session name" in text)
    status, _h, text = pythond._daemon_command("GET", "run", "work", {}, "")
    check("run via GET 405", status == 405, text)
    status, _h, text = pythond._daemon_command("GET", "int", "work", {}, "")
    check("int via GET 405", status == 405, text)
    with mock.patch.object(pythond, "send_session",
                           return_value={"error": "no session 'work' -- create it first: new work"}):
        status, _h, text = pythond._daemon_command("POST", "run", "work", {}, "1")
        check("missing session 404", status == 404 and text.startswith("ERR"), text)
    with mock.patch.object(pythond, "send_session",
                           return_value={"error": "timeout -- command channel may be out of sync"}):
        status, _h, text = pythond._daemon_command("POST", "run", "work", {}, "1")
        check("broken channel 409", status == 409, text)


def test_daemon_command_run_exec_error_header():
    section("_daemon_command run exec error header")
    with mock.patch.object(pythond, "send_session",
                           return_value={"output": "Traceback...", "_error": True}), \
         mock.patch.object(pythond, "_log_history") as log:
        status, hdrs, text = pythond._daemon_command("POST", "run", "work", {}, "1/0")
    check("exec error still 200", status == 200)
    check("exec error header set", hdrs.get("X-Pythond-Exec-Error") == "1")
    check("exec error body is output", text == "Traceback...")
    check("exec error not checkpointed", not log.called)
    with mock.patch.object(pythond, "send_session",
                           return_value={"output": "4", "_error": False}), \
         mock.patch.object(pythond, "_log_history") as log:
        status, hdrs, text = pythond._daemon_command("POST", "run", "work", {}, "2+2")
    check("run success 200", status == 200 and text == "4")
    check("no error header on success", "X-Pythond-Exec-Error" not in hdrs)
    check("success checkpointed", log.call_args.args == ("work", "2+2"))


def test_daemon_command_async_history():
    section("_daemon_command async history via poll")
    name = "__async_hist__"
    s = _fake_session()
    with _with_session(name, s):
        with mock.patch.object(pythond, "send_session",
                               return_value={"cell_id": "abc", "status": "fired"}):
            status, _h, text = pythond._daemon_command(
                "POST", "fire", name, {}, "a = 1")
        check("fire 200", status == 200)
        check("async src retained", s["async_src"].get("abc") == "a = 1")
        with mock.patch.object(pythond, "send_session",
                               return_value={"cell_id": "abc", "status": "done",
                                             "output": "", "_error": False}), \
             mock.patch.object(pythond, "_log_history") as log:
            status, _h, text = pythond._daemon_command(
                "GET", "poll", name, {"cell": ["abc"]}, "")
        check("poll done 200", status == 200)
        check("poll checkpoints async src", log.call_args.args == (name, "a = 1"))
        check("async src popped", "abc" not in s["async_src"])
    pythond.sessions.pop(name, None)


def test_worker_subprocess_protocol():
    section("worker subprocess protocol")
    env = {**os.environ, pythond._WORKER_ENV: "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "pythond.py"), "_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8", bufsize=1,
    )
    try:
        ready = json.loads(proc.stdout.readline())
        check("worker ready handshake", ready.get("ready") is True, ready)

        proc.stdin.write("{bad json\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        check("bad json gets protocol error",
              resp == {"error": "worker protocol error"}, resp)

        proc.stdin.write(json.dumps({"cmd": "run", "args": ["x = 42"]}) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        check("worker run ok", resp.get("_error") is False, resp)

        # stray output must not corrupt the protocol stream: a thread that
        # prints after the cell response would land on fd 1 without capture.
        code = ("import threading, time\n"
                "t = threading.Thread(target=lambda: (time.sleep(0.2), "
                "print('stray output')))\n"
                "t.start()")
        proc.stdin.write(json.dumps({"cmd": "run", "args": [code]}) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        check("stray-print cell ok", resp.get("_error") is False, resp)
        time.sleep(0.5)
        proc.stdin.write(json.dumps({"cmd": "run", "args": ["x + 1"]}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        try:
            resp = json.loads(line)
            check("protocol survives stray thread print",
                  resp.get("output") == "43", resp)
        except json.JSONDecodeError:
            check("protocol survives stray thread print", False, line)

        proc.stdin.write(json.dumps({"cmd": "status", "args": []}) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        check("worker still responsive", resp.get("state") == "idle", resp)
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
        stderr = proc.stderr.read()
        proc.stderr.close()
        proc.stdout.close()
    check("stray print went to stderr", "stray output" in stderr, stderr)


def test_worker_entry_requires_env():
    section("worker entry requires env capability")
    env = {k: v for k, v in os.environ.items() if k != pythond._WORKER_ENV}
    result = subprocess.run(
        [sys.executable, str(ROOT / "pythond.py"), "_worker"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    check("bare worker entry rejected", result.returncode == 1, result.stderr)
    check("bare worker entry message", "internal worker" in result.stderr)


def test_needs_more():
    section("attach continuation rule (_needs_more)")
    check("simple expr complete", pythond._needs_more("1 + 1", "1 + 1") is False)
    check("def opens block", pythond._needs_more("def f():", "def f():") is True)
    check("block body continues",
          pythond._needs_more("def f():\n    return 1", "    return 1") is True)
    check("blank line flushes block",
          pythond._needs_more("def f():\n    return 1\n", "") is False)
    check("syntax error flushes", pythond._needs_more("x = ", "x = ") is False)


def test_attach_line_repl():
    section("attach line REPL")
    lines = iter(["x = 1", "def f():", "    return 4", "", "f()"])
    def fake_input(prompt):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError
    calls = []
    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/status/"):
            return 200, {}, "{}"
        return 200, {}, "4" if body and "f()" in body else ""
    with mock.patch.object(pythond, "_request", side_effect=fake_request), \
         mock.patch.object(sys, "stderr", io.StringIO()), \
         mock.patch.object(sys, "stdout", io.StringIO()) as out:
        ok = pythond.attach("work", input_fn=fake_input)
    check("attach returns success", ok is True)
    run_calls = [c for c in calls if c[1] == "/run/work"]
    check("three cells sent", len(run_calls) == 3, run_calls)
    check("multi-line def sent as one cell",
          run_calls[1][2] == "def f():\n    return 4\n", run_calls[1])
    check("result printed", "4" in out.getvalue())


def test_attach_missing_session():
    section("attach missing session")
    with mock.patch.object(pythond, "_request",
                           return_value=(404, {}, "ERR no session 'x'")), \
         mock.patch.object(sys, "stderr", io.StringIO()) as err:
        ok = pythond.attach("x", input_fn=lambda p: "")
    check("attach fails on missing session", ok is False)
    check("attach error printed", "ERR no session" in err.getvalue())


def test_client_exit_codes():
    section("client exit codes")
    with mock.patch.object(pythond, "_request",
                           return_value=(404, {}, "ERR no session 'missing'")), \
         mock.patch.object(sys, "stderr", io.StringIO()) as err, \
         mock.patch.object(sys, "stdout", io.StringIO()):
        try:
            pythond.client("run", ["missing", "x"])
            check("client exits nonzero on ERR", False)
        except SystemExit as e:
            check("client exits nonzero on ERR", e.code == 1, e.code)
        check("ERR printed to stderr", "ERR no session" in err.getvalue())

    with mock.patch.object(pythond, "_request",
                           return_value=(200, {"X-Pythond-Exec-Error": "1"},
                                         "Traceback...")), \
         mock.patch.object(sys, "stderr", io.StringIO()) as err, \
         mock.patch.object(sys, "stdout", io.StringIO()):
        try:
            pythond.client("run", ["work", "1/0"])
            check("exec error exits nonzero", False)
        except SystemExit as e:
            check("exec error exits nonzero", e.code == 1, e.code)
        check("exec error rendered as ERR",
              err.getvalue().startswith("ERR execution failed\nTraceback"),
              err.getvalue())

    with mock.patch.object(pythond, "_request",
                           side_effect=ConnectionRefusedError("refused")), \
         mock.patch.object(sys, "stderr", io.StringIO()) as err, \
         mock.patch.object(sys, "stdout", io.StringIO()):
        try:
            pythond.client("ls", [])
            check("no daemon exits nonzero", False)
        except SystemExit as e:
            check("no daemon exits nonzero", e.code == 1, e.code)
        check("no daemon hint printed", "pythond daemon" in err.getvalue())

    with mock.patch.object(pythond, "_request",
                           return_value=(200, {}, "42")) as req, \
         mock.patch.object(sys, "stdout", io.StringIO()) as out:
        pythond.client("run", ["work", "x", "+", "1"])
    check("run joins code remainder",
          req.call_args.args == ("POST", "/run/work", "x + 1"), req.call_args)
    check("run output printed", "42" in out.getvalue())


def test_client_at_file():
    section("client @file posts file contents")
    src = "cfg = {'quotes': 'a \"b\" c'}\nlen(cfg)\n"
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        with mock.patch.object(pythond, "_request",
                               return_value=(200, {}, "1")) as req, \
             mock.patch.object(sys, "stdout", io.StringIO()):
            pythond.client("run", ["work", f"@{path}"])
        check("@file body is file contents",
              req.call_args.args == ("POST", "/run/work", src), req.call_args)
    finally:
        os.unlink(path)
    with mock.patch.object(pythond, "_request") as req, \
         mock.patch.object(sys, "stderr", io.StringIO()) as err:
        try:
            pythond.client("run", ["work", "@/nonexistent/task.py"])
            check("@missing file exits nonzero", False)
        except SystemExit as e:
            check("@missing file exits nonzero", e.code == 1, e.code)
    check("@missing file fails before any request", not req.called)
    check("@missing file error printed", "cannot read file" in err.getvalue())


def test_format_int():
    section("_format_int rendering")
    check("threads only",
          pythond._format_int("w", '{"threads": 1, "processes": 0}') ==
          "OK int w: 1 thread (best-effort)")
    check("processes only",
          pythond._format_int("w", '{"threads": 0, "processes": 2}') ==
          "OK int w: 2 processes (killed)")
    check("nothing running",
          pythond._format_int("w", '{"threads": 0, "processes": 0}') ==
          "OK no running cells in w")


def test_entry_points_exist():
    section("entry points")
    check("main", callable(pythond.main))
    check("pysh_main", callable(pythond.pysh_main))
    check("pyctl_main", callable(pythond.pyctl_main))


def test_pysh_cli_smoke():
    section("pysh CLI smoke (mocked transport)")
    with mock.patch.object(sys, "argv", ["pysh", "ls"]), \
         mock.patch.object(pythond, "_request",
                           return_value=(200, {}, "(no sessions)")), \
         mock.patch.object(sys, "stdout", io.StringIO()) as out:
        pythond.pysh_main()
    check("pysh ls prints listing", "(no sessions)" in out.getvalue())
    with mock.patch.object(sys, "argv", ["pysh", "attach", "work"]), \
         mock.patch.object(pythond, "attach", return_value=True) as attach_fn:
        pythond.pysh_main()
    check("pysh attach delegates", attach_fn.call_args.args == ("work",))


# ===========================================
# INTEGRATION TESTS (real daemon subprocess)
# ===========================================

def free_tcp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class _Daemon:
    """Start a real daemon subprocess; route this test process's client
    (pythond._request) at it."""
    def __init__(self, tmpdir):
        self.tmp = tmpdir
        self.env = os.environ.copy()
        self.patches = []
        if _HAS_AF_UNIX:
            self.sock = os.path.join(tmpdir, "pythond.sock")
            self.env["PYTHOND_SOCK"] = self.sock
            self.patches.append(mock.patch.object(pythond, "SOCK", self.sock))
        else:
            self.port = free_tcp_port()
            self.env["LOCALAPPDATA"] = tmpdir
            self.env["PYTHOND_PORT"] = str(self.port)
            self.patches.append(mock.patch.dict(
                os.environ, {"LOCALAPPDATA": tmpdir,
                             "PYTHOND_PORT": str(self.port)}))
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "pythond.py"), "daemon"],
            env=self.env, stderr=subprocess.PIPE, text=True,
        )
        self._stderr_chunks = []
        def _drain():
            for line in self.proc.stderr:
                self._stderr_chunks.append(line)
        threading.Thread(target=_drain, daemon=True).start()
        if _HAS_AF_UNIX:
            up = wait_until(lambda: self.proc.poll() is None and
                            os.path.exists(self.sock))
        else:
            meta = os.path.join(self.tmp, "pythond", "daemon.json")
            up = wait_until(lambda: self.proc.poll() is None and
                            os.path.exists(meta))
        if not up:
            raise RuntimeError("daemon failed to start: " + self.stderr())
        for p in self.patches:
            p.start()
        return self

    def stderr(self):
        return "".join(self._stderr_chunks)

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)


def test_integration_lifecycle():
    section("INTEGRATION: daemon lifecycle over HTTP")
    name = "__it_life__"
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                               "sessions", name), ignore_errors=True)
    with tempfile.TemporaryDirectory() as td, _Daemon(td) as d:
        status, _h, text = pythond._request("GET", "/ls")
        check("ls empty", status == 200 and "(no sessions)" in text, text)

        status, _h, text = pythond._request("POST", f"/new/{name}")
        check("new OK", status == 200 and f"OK {name}" in text, text)

        status, _h, text = pythond._request("GET", "/ls")
        check("ls has session", name in text and "alive" in text, text)

        status, _h, text = pythond._request("POST", f"/run/{name}", "1+1")
        check("run output", text.strip() == "2", text)

        pythond._request("POST", f"/run/{name}", "x = 42")
        status, _h, text = pythond._request("POST", f"/run/{name}", "x")
        check("state persists", text.strip() == "42", text)

        status, hdrs, text = pythond._request("POST", f"/run/{name}", "1/0")
        check("exec error 200 + header",
              status == 200 and hdrs.get("X-Pythond-Exec-Error") == "1", hdrs)
        check("exec error body has traceback", "ZeroDivisionError" in text)

        status, _h, text = pythond._request("POST", f"/run/{name}", "x + 1")
        check("session survives exec error", text.strip() == "43", text)

        status, _h, text = pythond._request(
            "POST", f"/fire/{name}", "import time; time.sleep(0.1); y = 99")
        data = json.loads(text)
        check("fire has cell_id", "cell_id" in data, text)
        cid = data["cell_id"]
        time.sleep(0.4)
        status, _h, text = pythond._request("GET", f"/poll/{name}?cell={cid}")
        check("poll done", json.loads(text)["status"] == "done", text)
        status, _h, text = pythond._request("POST", f"/run/{name}", "y")
        check("fire state", text.strip() == "99", text)

        status, _h, text = pythond._request("GET", f"/vars/{name}")
        data = json.loads(text)
        check("vars has x and y", "x" in data["vars"] and "y" in data["vars"])

        status, _h, text = pythond._request("POST", f"/complete/{name}", "os.path.")
        check("complete has matches", len(json.loads(text)["matches"]) > 0)

        status, _h, text = pythond._request("GET", f"/status/{name}")
        check("status idle", json.loads(text)["state"] == "idle", text)

        status, _h, text = pythond._request("POST", f"/run/{name}", "print('Traceback')")
        check("literal Traceback not error",
              text.strip() == "Traceback" and "X-Pythond-Exec-Error" not in _h)

        status, _h, text = pythond._request("POST", "/run/nosuch", "1")
        check("no session 404", status == 404 and "ERR" in text, text)

        # concurrent clients on one session are serialized, not interleaved
        pythond._request("POST", f"/run/{name}", "counter = 0")
        results = []
        def _concurrent():
            results.append(pythond._request(
                "POST", f"/run/{name}",
                "import time; time.sleep(0.02); counter = counter + 1"))
        threads = [threading.Thread(target=_concurrent) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        check("concurrent clients returned", len(results) == 5, results)
        status, _h, text = pythond._request("POST", f"/run/{name}", "counter")
        check("concurrent runs serialized", text.strip() == "5", text)

        # checkpoint: successes in, errors out
        hist = os.path.join(os.path.expanduser("~"), ".pythond",
                            "sessions", name, "history.py")
        check("history.py exists", os.path.exists(hist))
        if os.path.exists(hist):
            content = open(hist, encoding="utf-8").read()
            check("history has x=42", "x = 42" in content)
            check("history no ZeroDivision", "1/0" not in content)
            check("literal Traceback checkpointed", "print('Traceback')" in content)

        status, _h, text = pythond._request("POST", f"/kill/{name}")
        check("kill OK", "OK killed" in text, text)
        status, _h, text = pythond._request("GET", "/ls")
        check("ls empty after kill", "(no sessions)" in text, text)

        status, _h, text = pythond._request("POST", "/stop")
        check("stop OK", "OK stopping daemon" in text, text)
        check("daemon exited", wait_until(lambda: d.proc.poll() is not None),
              d.stderr())
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                               "sessions", name), ignore_errors=True)


def test_integration_crash_isolation():
    section("INTEGRATION: crash isolation + reconnect")
    name = "__it_crash__"
    with tempfile.TemporaryDirectory() as td, _Daemon(td):
        pythond._request("POST", f"/new/{name}")
        pythond._request("POST", f"/run/{name}", "x = 42")
        for i in range(5):
            pythond._request("POST", f"/run/{name}", f"raise ValueError('{i}')")
        status, _h, text = pythond._request("POST", f"/run/{name}", "x + 1")
        check("var after 5 crashes", text.strip() == "43", text)
        _s, _h, text = pythond._request("POST", f"/run/{name}", "import nonexistent_xyz")
        check("import error caught", "ModuleNotFoundError" in text, text)
        # every _request is a fresh connection: connection != state by design
        status, _h, text = pythond._request("POST", f"/run/{name}", "x + 2")
        check("state across connections", text.strip() == "44", text)
        # interrupt a fire'd python loop, session survives
        pythond._request("POST", f"/fire/{name}",
                         "[__import__('time').sleep(0.1) for _ in range(100)]")
        time.sleep(0.3)
        status, _h, text = pythond._request("POST", f"/int/{name}")
        check("int OK", status == 200, text)
        time.sleep(0.5)
        status, _h, text = pythond._request("POST", f"/run/{name}", "x + 3")
        check("var after interrupt", text.strip() == "45", text)
        pythond._request("POST", "/stop")
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                               "sessions", name), ignore_errors=True)


def test_integration_auth():
    section("INTEGRATION: token auth (local TCP mode)")
    if _HAS_AF_UNIX:
        check("auth applies to TCP mode only (AF_UNIX uses fs perms)", True)
        return
    with tempfile.TemporaryDirectory() as td, _Daemon(td) as d:
        meta = json.load(open(os.path.join(td, "pythond", "daemon.json"),
                              encoding="utf-8"))
        check("meta has token", bool(meta.get("token")))
        status, _h, text = pythond._request("GET", "/ls")
        check("right token accepted", status == 200, text)
        with mock.patch.dict(os.environ, {"PYTHOND_TOKEN": "wrong",
                                          "PYTHOND_HOST": f"127.0.0.1:{d.port}"}):
            status, _h, text = pythond._request("GET", "/ls")
        check("wrong token 401", status == 401 and "auth failed" in text, text)
        pythond._request("POST", "/stop")


def test_integration_second_daemon_fails():
    section("INTEGRATION: second daemon fails loud")
    with tempfile.TemporaryDirectory() as td, _Daemon(td) as d:
        second = subprocess.run(
            [sys.executable, str(ROOT / "pythond.py"), "daemon"],
            env=d.env, capture_output=True, text=True, timeout=15,
        )
        if _HAS_AF_UNIX:
            # AF_UNIX: the new daemon unlinks and rebinds the socket path; the
            # old daemon keeps serving existing connections.  Windows TCP: the
            # second bind must fail.  Either way exactly one daemon owns the
            # endpoint afterwards.
            check("unix rebind is a takeover (documented)", True)
            with contextlib.suppress(Exception):
                pythond._request("POST", "/stop")
        else:
            check("second daemon exits nonzero", second.returncode == 1,
                  second.stderr)
            check("second daemon says why",
                  "cannot start daemon" in second.stderr, second.stderr)
            pythond._request("POST", "/stop")


def test_integration_fork():
    section("INTEGRATION: fork over HTTP")
    if sys.platform == "win32":
        check("fork skipped on windows", True)
        return
    name = "__it_fork__"
    with tempfile.TemporaryDirectory() as td, _Daemon(td):
        pythond._request("POST", f"/new/{name}")
        pythond._request("POST", f"/run/{name}", "x = 10")
        status, _h, text = pythond._request("POST", f"/fork/{name}", "y = x * 2")
        data = json.loads(text)
        check("fork forked", data.get("status") == "forked", text)
        cid = data["cell_id"]
        for _ in range(30):
            time.sleep(0.2)
            _s, _h, text = pythond._request("GET", f"/poll/{name}?cell={cid}")
            data = json.loads(text)
            if data["status"] == "done":
                break
        check("fork done", data["status"] == "done", text)
        check("fork merged", "y" in data.get("merged", []), text)
        _s, _h, text = pythond._request("POST", f"/run/{name}", "y")
        check("fork result in namespace", text.strip() == "20", text)
        pythond._request("POST", "/stop")
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                               "sessions", name), ignore_errors=True)


# ===========================================

def main():
    tests = [
        # Unit tests
        test_version,
        test_zero_dependencies,
        test_session_name_validation,
        test_parse_host_port,
        test_init_namespace,
        test_make_exec_eval,
        test_make_exec_exec,
        test_make_exec_last_expr,
        test_make_exec_error,
        test_make_exec_thread_isolation,
        test_make_exec_restores_replaced_stdio,
        test_thread_stdout_compat_methods,
        test_dispatch_run,
        test_dispatch_fire_poll,
        test_dispatch_async_empty_code_rejected,
        test_dispatch_fire_traceback_format_failure,
        test_dispatch_poll_variants,
        test_dispatch_status_vars_complete,
        test_dispatch_int,
        test_dispatch_unknown,
        test_dispatch_fork,
        test_dispatch_fork_kill,
        test_dispatch_fork_kills_grandchildren,
        test_fork_shutdown_cleanup_kills_grandchildren,
        test_dispatch_fork_large_payload,
        test_dispatch_fork_concurrent_fire,
        test_cell_eviction,
        test_session_dir_and_history,
        test_meta_roundtrip,
        test_send_session_timeout_marks_unhealthy,
        test_send_session_malformed_marks_unhealthy,
        test_send_session_oversized_marks_unhealthy,
        test_send_session_dead_worker,
        test_daemon_command_routing,
        test_daemon_command_run_exec_error_header,
        test_daemon_command_async_history,
        test_worker_subprocess_protocol,
        test_worker_entry_requires_env,
        test_needs_more,
        test_attach_line_repl,
        test_attach_missing_session,
        test_client_exit_codes,
        test_client_at_file,
        test_format_int,
        test_entry_points_exist,
        test_pysh_cli_smoke,

        # Integration tests
        test_integration_lifecycle,
        test_integration_crash_isolation,
        test_integration_auth,
        test_integration_second_daemon_fails,
        test_integration_fork,
    ]
    registered = {fn.__name__ for fn in tests}
    discovered = {name for name, obj in globals().items()
                  if name.startswith("test_") and callable(obj)}
    missing = sorted(discovered - registered)
    check("all test functions registered", not missing, ", ".join(missing))
    for fn in tests:
        fn()

    print(f"\n{'='*40}")
    print(f"  {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
    print("  all clear")


if __name__ == "__main__":
    main()
