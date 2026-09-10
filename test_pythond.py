#!/usr/bin/env python3
"""pythond test suite -- unit + integration.

Run:  python -B test_pythond.py

Unit tests run everywhere.  Integration tests start a real daemon subprocess:
AF_UNIX socket on POSIX, 127.0.0.1 + token on Windows -- both paths are
exercised by CI's OS matrix.
"""
import json
import pickle
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
    check("version is 0.5.3", pythond.__version__ == "0.5.3")


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
    check("fork snapshot refuses busy execution lock", resp.get("busy") is True)
    time.sleep(0.6)
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
    proc.pid = 12345
    proc.stdin = _FakeStdin()
    proc.poll.return_value = None
    return {"proc": proc, "q": q, "lock": threading.Lock(),
            "id": pythond.uuid.uuid4().hex, "unhealthy": False,
            "async_src": {}, "async_done": {}, "async_lock": threading.Lock()}


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


def _cmd(method, cmd, name="", var="", query=None, body=b""):
    return pythond._daemon_command(method, cmd, name, var, query or {}, body)


def test_daemon_command_routing():
    section("_daemon_command routing")
    status, _h, text = _cmd("GET", "ls")
    check("ls routes", status == 200)
    status, _h, text = _cmd("GET", "bogus")
    check("unknown route 404", status == 404 and "ERR unknown" in text, text)
    status, _h, text = _cmd("POST", "run", "Bad.Name", body=b"x")
    check("invalid name 400", status == 400 and "invalid session name" in text)
    status, _h, text = _cmd("GET", "run", "work")
    check("run via GET 405", status == 405, text)
    status, _h, text = _cmd("GET", "int", "work")
    check("int via GET 405", status == 405, text)
    with mock.patch.object(pythond, "send_session",
                           return_value={"error": "no session 'work' -- create it first: new work"}):
        status, _h, text = _cmd("POST", "run", "work", body=b"1")
        check("missing session 404", status == 404 and text.startswith("ERR"), text)
    with mock.patch.object(pythond, "send_session",
                           return_value={"error": "timeout -- command channel may be out of sync"}):
        status, _h, text = _cmd("POST", "run", "work", body=b"1")
        check("broken channel 409", status == 409, text)


def test_daemon_command_run_exec_error_header():
    section("_daemon_command run exec error header")
    with mock.patch.object(pythond, "send_session",
                           return_value={"output": "Traceback...", "_error": True}), \
         mock.patch.object(pythond, "_log_history") as log:
        status, hdrs, text = _cmd("POST", "run", "work", body=b"1/0")
    check("exec error still 200", status == 200)
    check("exec error header set", hdrs.get("X-Pythond-Exec-Error") == "1")
    check("exec error body is output", text == "Traceback...")
    check("exec error not checkpointed", not log.called)
    with mock.patch.object(pythond, "send_session",
                           return_value={"output": "4", "_error": False}), \
         mock.patch.object(pythond, "_log_history") as log:
        status, hdrs, text = _cmd("POST", "run", "work", body=b"2+2")
    check("run success 200", status == 200 and text == "4")
    check("no error header on success", "X-Pythond-Exec-Error" not in hdrs)
    check("success checkpointed", log.call_args.args == ("work", "2+2"))


def test_async_http_receipts():
    section("HTTP async receipts: 202 + Location, not execution results")
    for cmd, state in (("fire", "fired"), ("fork", "forked")):
        receipt = {"cell_id": "abc123", "status": state, "_session_id": "incarnation"}
        with mock.patch.object(pythond, "send_session", return_value=receipt):
            status, headers, body = _cmd("POST", cmd, "work", body=b"1+1")
        check(f"{cmd} accepted", status == 202)
        check(f"{cmd} status monitor in Location", headers.get("Location") ==
              "/poll/work?cell=abc123")
        check(f"{cmd} JSON content type", headers.get("Content-Type") == "application/json")
        check(f"{cmd} worker identity in header",
              headers.get("X-Pythond-Session-Id") == "incarnation")
        check(f"{cmd} receipt body unchanged", json.loads(body) == {
            "cell_id": "abc123", "status": state})
        with mock.patch.object(pythond, "send_session", return_value={"error": "refused"}):
            status, headers, _body = _cmd("POST", cmd, "work", body=b"1+1")
        check(f"rejected {cmd} is not accepted", status == 409 and "Location" not in headers)


def test_async_history_events():
    section("async history and events, including completion before ACK")
    name = "__async_hist__"
    for early in (False, True):
        s = _fake_session([json.dumps({"cell_id": "abc", "status": "fired"})])
        event = {"type": "cell_done", "cell_id": "abc", "status": "done",
                 "error": False, "output": "1"}
        with _with_session(name, s), \
             mock.patch.object(pythond, "_log_history") as history, \
             mock.patch.object(pythond, "_events", pythond._EventLog()) as log:
            if early:
                pythond._worker_event(name, s, event)
                check("early completion waits for source", log.sequence == 0)
            resp = pythond.send_session(name, "fire", ["a = 1"])
            check("fire still receives ACK", resp.get("cell_id") == "abc", resp)
            if not early:
                # Simulate a command in flight: the event reader MUST NOT take
                # the command lock, or it cannot read the command's reply next.
                with s["lock"]:
                    pythond._worker_event(name, s, event)
            check("completion checkpoints without poll",
                  history.call_args is not None and
                  history.call_args.args == (name, "a = 1"))
            check("source and early-result buffers emptied",
                  not s["async_src"] and not s["async_done"])
            check("one completion event published", log.sequence == 1)
            check("no event queued as command reply", s["q"].empty())
            frame = log.next(0, 0)[1].decode("utf-8")
            check("event includes session incarnation", s["id"] in frame)
            with mock.patch.object(pythond, "send_session", return_value={
                    "cell_id": "abc", "status": "done", "output": "1"}):
                _cmd("GET", "poll", name, query={"cell": ["abc"]})
            check("poll cannot duplicate checkpoint or event",
                  history.call_count == 1 and log.sequence == 1)

            pythond._note_async_launch(name, s, "1/0", {"cell_id": "err"})
            pythond._worker_event(name, s, {**event, "cell_id": "err", "error": True})
            check("errors notify but do not checkpoint",
                  log.sequence == 2 and history.call_count == 1)
            replacement = _fake_session()
            with _with_session(name, replacement):
                pythond._worker_event(name, s, {**event, "cell_id": "late"})
            check("old worker cannot notify as replacement", log.sequence == 2)
    pythond.sessions.pop(name, None)


def test_new_safe_defaults():
    section("new refuses existing state; replace is explicit and atomic")
    name = "__safe_new__"
    existing = _fake_session()
    with _with_session(name, existing), \
         mock.patch.object(pythond.subprocess, "Popen") as spawn, \
         mock.patch.object(pythond, "_close_session") as close:
        status, _h, text = _cmd("POST", "new", name)
        check("default new conflicts", status == 409 and "already exists" in text)
        check("existing worker untouched", pythond.sessions[name] is existing and
              not close.called and not spawn.called)
        try:
            pythond._publish_session(name, _fake_session())
            check("publication rechecks existing name", False)
        except RuntimeError:
            check("publication rechecks existing name", pythond.sessions[name] is existing)
    pythond.sessions.pop(name, None)

    with mock.patch.object(pythond, "new_session", return_value=existing) as new:
        for policy, expected in (({}, False), ({"replace": ["0"]}, False),
                                 ({"replace": ["1"]}, True)):
            status, _h, _text = _cmd("POST", "new", name, query=policy)
            check("route passes explicit replacement policy", status == 201 and
                  new.call_args.kwargs == {"replace": expected}, new.call_args)
        before = new.call_count
        for value in ([""], ["true"], ["1", "0"]):
            status, _h, _text = _cmd("POST", "new", name, query={"replace": value})
            check("ambiguous replacement rejected", status == 400)
        check("invalid replacement never spawns", new.call_count == before)

    barrier = threading.Barrier(2)
    winners, conflicts = [], []
    def publish(s):
        barrier.wait(timeout=5)
        try:
            pythond._publish_session(name, s)
            winners.append(s)
        except RuntimeError:
            conflicts.append(s)
    with mock.patch.dict(pythond.sessions, {}, clear=True):
        threads = [threading.Thread(target=publish, args=(_fake_session(),))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        check("concurrent publication has one winner",
              len(winners) == len(conflicts) == 1 and
              pythond.sessions.get(name) is winners[0])

    # A creator that loses publication must reap its unowned worker, not leak it.
    proc = _fake_session()["proc"]
    proc.stdout = io.StringIO('{"ready": true}\n')
    with mock.patch.object(pythond.subprocess, "Popen", return_value=proc) as spawn, \
         mock.patch.object(pythond, "_publish_session", side_effect=RuntimeError("race")), \
         mock.patch.object(pythond, "_close_session") as close:
        try:
            pythond.new_session(name)
            check("losing creation raises", False)
        except RuntimeError:
            check("losing worker reaped", close.call_count == 1 and
                  close.call_args.args[0]["proc"] is proc)
        expected_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        check("worker spawn suppresses Windows console only",
              spawn.call_args.kwargs["creationflags"] == expected_flags)


def test_event_log():
    section("bounded event replay, cursor gaps, shutdown wakeup")
    log = pythond._EventLog(max_events=2)
    origin = log.cursor()
    results = []
    waiting = threading.Event()
    def wait():
        waiting.set()
        results.append(log.next(0, 5))
    t = threading.Thread(target=wait, daemon=True)
    t.start()
    waiting.wait(2)
    log.publish({"type": "cell_done", "output": "你好\nsecond line"})
    t.join(timeout=2)
    check("publisher wakes subscriber", len(results) == 1 and results[0] is not None)
    check("UTF-8 JSON remains one SSE data line",
          b'\\nsecond line' in results[0][1] and
          "你好".encode() in results[0][1])
    check("replay is not consuming", log.next(0, 0) == results[0])
    check("new subscriber starts at current cursor", log.subscribe(None) == 1)
    log.publish({"type": "cell_done", "cell_id": "two"})
    log.publish({"type": "cell_done", "cell_id": "three"})
    for cursor, status in ((origin, 410), (log.cursor(99), 409),
                           (pythond._EventLog().cursor(), 409), ("bad", 400)):
        try:
            log.subscribe(cursor)
            check("bad cursor rejected", False, cursor)
        except pythond._EventCursorError as e:
            check("bad cursor status", e.status == status, e)
    check("retained boundary is replayable", log.subscribe(log.cursor(1)) == 1)
    try:
        log.next(0, 0)
        check("slow subscriber sees gap", False)
    except pythond._EventCursorError as e:
        check("slow subscriber sees gap", e.status == 410)
    tiny = pythond._EventLog(max_bytes=1)
    tiny.publish({"type": "cell_done", "output": "too large"})
    check("byte budget enforced even for one oversized event", tiny._bytes == 0)
    try:
        tiny.subscribe(tiny.cursor(0))
        check("oversized eviction is explicit", False)
    except pythond._EventCursorError as e:
        check("oversized eviction is explicit", e.status == 410)
    results.clear()
    t = threading.Thread(target=lambda: results.append(log.next(3, 5)), daemon=True)
    t.start()
    log.close()
    t.join(timeout=2)
    check("close wakes idle subscription", results == [None])


def test_event_stream_reset():
    section("stream signals retention gap after headers")
    handler = object.__new__(pythond._Handler)
    handler.wfile = io.BytesIO()
    handler.connection = mock.Mock()
    handler.headers = {}
    handler.send_response = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    log = pythond._EventLog()
    with mock.patch.object(pythond, "_events", log), \
         mock.patch.object(log, "next", side_effect=pythond._EventCursorError(
             410, "event_cursor_expired")):
        handler._event_stream({})
    data = handler.wfile.getvalue()
    check("gap is reset event, not silent skip",
          b"event: reset\n" in data and b"event_cursor_expired" in data)
    check("gap closes connection", handler.close_connection)


def test_completion_snapshot():
    section("completion snapshot bounded independently of poll TTL")
    ns = pythond._init_namespace()
    lock = threading.Lock()
    cells = {}
    events = queue.Queue()
    resp = pythond._dispatch("fire", ["print('汉' * 30000)"],
                             pythond._make_exec(ns, lock), cells, ns, lock,
                             notify=events.put)
    event = events.get(timeout=5)
    check("completion cell identity", event["cell_id"] == resp["cell_id"])
    check("large output flagged", event["output_truncated"] and
          event["output_bytes"] == 90000)
    check("snapshot tail is valid UTF-8 and bounded",
          len(event["output"].encode()) <= pythond._EVENT_OUTPUT_BYTES and
          set(event["output"]) == {"汉"})
    cid = resp["cell_id"]
    cells[cid]["_done_at"] = time.time() - pythond._ASYNC_CELL_TTL - 1
    pythond._evict_stale_cells(cells)
    check("eviction doesn't consume delivered snapshot",
          cid not in cells and event["output_bytes"] == 90000)


def test_dispatch_dump_load():
    section("_dispatch dump/load (pickle in/out)")
    import base64
    ns = pythond._init_namespace()
    lock = threading.Lock()
    _exec = pythond._make_exec(ns, lock)
    cells = {}
    ns["df"] = {"rows": 100}
    ns["lk"] = threading.Lock()

    resp = pythond._dispatch("dump", ["df"], _exec, cells, ns, lock)
    check("dump var has pickle", "pickle" in resp, resp)
    import pickle as _p
    check("dump var roundtrips",
          _p.loads(base64.b64decode(resp["pickle"])) == {"rows": 100})

    resp = pythond._dispatch("dump", ["missing"], _exec, cells, ns, lock)
    check("dump missing var errors", "is not defined" in resp.get("error", ""))
    resp = pythond._dispatch("dump", ["lk"], _exec, cells, ns, lock)
    check("dump unpicklable var errors", "not picklable" in resp.get("error", ""))

    resp = pythond._dispatch("dump", [""], _exec, cells, ns, lock)
    whole = _p.loads(base64.b64decode(resp["pickle"]))
    check("whole dump includes df", whole.get("df") == {"rows": 100})
    check("whole dump skips lock", "lk" in resp.get("skipped", []), resp)
    check("whole dump skips modules", "os" in resp.get("skipped", []), resp)
    check("whole dump excludes skipped from payload", "lk" not in whole)

    b64 = base64.b64encode(_p.dumps([1, 2, 3])).decode()
    resp = pythond._dispatch("load", ["items", b64], _exec, cells, ns, lock)
    check("load sets var", resp == {"set": ["items"]}, resp)
    check("loaded var live", ns.get("items") == [1, 2, 3])

    b64 = base64.b64encode(_p.dumps({"a": 1, "_hidden": 2})).decode()
    resp = pythond._dispatch("load", ["", b64], _exec, cells, ns, lock)
    check("whole load merges dict", resp == {"set": ["a"]}, resp)
    check("whole load sets a", ns.get("a") == 1)
    check("whole load skips underscore keys", "_hidden" not in ns)

    b64 = base64.b64encode(_p.dumps([1])).decode()
    resp = pythond._dispatch("load", ["", b64], _exec, cells, ns, lock)
    check("whole load rejects non-dict", "needs a pickled dict" in resp["error"])
    resp = pythond._dispatch("load", ["x", "not-base64-pickle"], _exec, cells, ns, lock)
    check("bad payload errors", "unpickle failed" in resp.get("error", ""))
    resp = pythond._dispatch("load", ["x"], _exec, cells, ns, lock)
    check("load without data errors", "requires pickled data" in resp.get("error", ""))


def test_daemon_command_pickle_route():
    section("_daemon_command /pickle route")
    import base64, pickle as _p
    payload = base64.b64encode(_p.dumps(42)).decode()
    with mock.patch.object(pythond, "send_session",
                           return_value={"pickle": payload, "skipped": []}) as ss:
        status, hdrs, body = _cmd("GET", "pickle", "work", "x")
    check("pickle GET 200", status == 200)
    check("pickle GET raw bytes", body == _p.dumps(42), body)
    check("pickle GET content type",
          hdrs.get("Content-Type") == "application/octet-stream")
    check("pickle GET sends dump", ss.call_args.args == ("work", "dump", ["x"]))

    with mock.patch.object(pythond, "send_session",
                           return_value={"pickle": payload,
                                         "skipped": ["os", "lk"]}):
        status, hdrs, body = _cmd("GET", "pickle", "work")
    check("skipped surfaces in header",
          hdrs.get("X-Pythond-Skipped") == "os,lk", hdrs)

    with mock.patch.object(pythond, "send_session",
                           return_value={"set": ["x"]}) as ss:
        status, _h, text = _cmd("POST", "pickle", "work", "x",
                                body=_p.dumps(42))
    check("pickle POST 200", status == 200 and text == "OK set x", text)
    check("pickle POST sends load b64",
          ss.call_args.args == ("work", "load",
                                ["x", base64.b64encode(_p.dumps(42)).decode()]))

    with mock.patch.object(pythond, "send_session",
                           return_value={"error": "name 'x' is not defined"}):
        status, _h, text = _cmd("GET", "pickle", "work", "x")
    check("missing var 404", status == 404, text)
    status, _h, text = _cmd("GET", "pickle", "work", "not a var!")
    check("invalid var name 400", status == 400, text)


def test_parse_cp_target():
    section("_parse_cp_target (scp syntax)")
    check("session:var", pythond._parse_cp_target("work:df") ==
          ("session", "work", "df"))
    check("session: whole", pythond._parse_cp_target("work:") ==
          ("session", "work", ""))
    check("plain file", pythond._parse_cp_target("df.pkl") ==
          ("file", "df.pkl", ""))
    check("windows drive path is a file",
          pythond._parse_cp_target("C:\\tmp\\df.pkl") ==
          ("file", "C:\\tmp\\df.pkl", ""))
    check("unix path is a file",
          pythond._parse_cp_target("/tmp/df.pkl") == ("file", "/tmp/df.pkl", ""))
    try:
        pythond._parse_cp_target("Bad.Name:x")
        check("invalid session name rejected", False)
    except ValueError:
        check("invalid session name rejected", True)
    try:
        pythond._parse_cp_target("work:not a var")
        check("invalid var rejected", False)
    except ValueError:
        check("invalid var rejected", True)


def test_client_cp():
    section("client cp")
    import pickle as _p
    raw = _p.dumps({"n": 7})
    calls = []
    def fake_request_bytes(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return 200, {}, raw
        return 200, {}, b"OK set df"
    with mock.patch.object(pythond, "_request_bytes",
                           side_effect=fake_request_bytes), \
         mock.patch.object(sys, "stdout", io.StringIO()) as out:
        pythond.client("cp", ["a:df", "b:df"])
    check("cp session->session GET then POST",
          calls == [("GET", "/pickle/a/df", None),
                    ("POST", "/pickle/b/df", raw)], calls)
    check("cp prints result", "OK set df" in out.getvalue())

    fd, path = tempfile.mkstemp(suffix=".pkl")
    os.close(fd)
    try:
        calls.clear()
        with mock.patch.object(pythond, "_request_bytes",
                               side_effect=fake_request_bytes), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            pythond.client("cp", ["a:df", path])
        check("cp session->file wrote pickle",
              open(path, "rb").read() == raw)
        calls.clear()
        with mock.patch.object(pythond, "_request_bytes",
                               side_effect=fake_request_bytes), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            pythond.client("cp", [path, "b:df"])
        check("cp file->session posts bytes",
              calls == [("POST", "/pickle/b/df", raw)], calls)
    finally:
        os.unlink(path)

    with mock.patch.object(sys, "stderr", io.StringIO()) as err:
        try:
            pythond.client("cp", ["a.pkl", "b.pkl"])
            check("cp file->file rejected", False)
        except SystemExit as e:
            check("cp file->file rejected", e.code == 1)
    check("cp file->file hint", "shell's cp" in err.getvalue())


def test_worker_subprocess_protocol():
    section("worker subprocess protocol")
    env = {**os.environ, pythond._WORKER_ENV: "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "pythond.py"), "_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8", bufsize=1,
    )
    activity = []
    def read_reply():
        while True:
            packet = json.loads(proc.stdout.readline())
            if "_event" in packet:
                activity.append(packet["_event"])
            else:
                return packet
    try:
        ready = read_reply()
        check("worker ready handshake", ready.get("ready") is True, ready)

        proc.stdin.write("{bad json\n")
        proc.stdin.flush()
        resp = read_reply()
        check("bad json gets protocol error",
              resp == {"error": "worker protocol error"}, resp)

        proc.stdin.write(json.dumps({"cmd": "run", "args": ["x = 42"]}) + "\n")
        proc.stdin.flush()
        resp = read_reply()
        check("worker run ok", resp.get("_error") is False, resp)

        # stray output must not corrupt the protocol stream: a thread that
        # prints after the cell response would land on fd 1 without capture.
        code = ("import threading, time\n"
                "t = threading.Thread(target=lambda: (time.sleep(0.2), "
                "print('stray output')))\n"
                "t.start()")
        proc.stdin.write(json.dumps({"cmd": "run", "args": [code]}) + "\n")
        proc.stdin.flush()
        resp = read_reply()
        check("stray-print cell ok", resp.get("_error") is False, resp)
        time.sleep(0.5)
        proc.stdin.write(json.dumps({"cmd": "run", "args": ["x + 1"]}) + "\n")
        proc.stdin.flush()
        resp = read_reply()
        check("protocol survives stray thread print",
              resp.get("output") == "43", resp)
        check("sync events are separately framed", len(activity) == 3 and
              all(e.get("sync") is True for e in activity) and
              activity[-1]["cell_id"] == resp["cell_id"])

        proc.stdin.write(json.dumps({"cmd": "status", "args": []}) + "\n")
        proc.stdin.flush()
        resp = read_reply()
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
    for argv, path in ((["new", "work"], "/new/work"),
                       (["new", "work", "--replace"], "/new/work?replace=1"),
                       (["new", "--replace", "work"], "/new/work?replace=1")):
        with mock.patch.object(sys, "argv", ["pysh"] + argv), \
             mock.patch.object(pythond, "_request", return_value=(201, {}, "OK")) as req, \
             mock.patch.object(sys, "stdout", io.StringIO()):
            pythond.pysh_main()
        check("CLI replacement is opt-in", req.call_args.args == ("POST", path))
    with mock.patch.object(sys, "argv", ["pysh", "new", "work"]), \
         mock.patch.object(pythond, "_request", return_value=(409, {}, "ERR already exists")), \
         mock.patch.object(sys, "stderr", io.StringIO()):
        try:
            pythond.pysh_main()
            check("CLI conflict exits nonzero", False)
        except SystemExit as e:
            check("CLI conflict exits nonzero", e.code == 1)
    for cmd in ("fire", "fork"):
        with mock.patch.object(sys, "argv", ["pysh", cmd, "work", "1+1"]), \
             mock.patch.object(pythond, "_request", return_value=(202, {}, '{"cell_id":"abc"}')), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            pythond.pysh_main()
        check(f"CLI accepts {cmd} 202", json.loads(out.getvalue())["cell_id"] == "abc")


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
    def __init__(self, tmpdir, extra_env=None):
        self.tmp = tmpdir
        # Never inherit a production endpoint, token, or checkpoint directory.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHOND_")}
        self.env.update({"HOME": tmpdir, "USERPROFILE": tmpdir, "LOCALAPPDATA": tmpdir,
                         "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        self.env.update(extra_env or {})
        self.patches = []
        if _HAS_AF_UNIX:
            self.sock = os.path.join(tmpdir, "pythond.sock")
            self.env["PYTHOND_SOCK"] = self.sock
            self.patches.append(mock.patch.object(pythond, "SOCK", self.sock))
        else:
            self.port = free_tcp_port()
            self.env["LOCALAPPDATA"] = tmpdir
            self.env["PYTHOND_PORT"] = str(self.port)
        self.patches.append(mock.patch.dict(os.environ, self.env, clear=True))
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "pythond.py"), "daemon"],
            env=self.env, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
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
        try:
            if self.proc.poll() is None:
                # Graceful HTTP stop also reaps workers on Windows.
                with contextlib.suppress(OSError):
                    pythond._request("POST", "/stop")
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
        finally:
            for p in reversed(self.patches):
                p.stop()


class _Events:
    """Blocking SSE test client: waits for pushed events, never calls poll."""
    def __init__(self, cursor=None, path="/events", timeout=8):
        self.request_cursor = cursor
        self.path = path
        self.timeout = timeout

    def __enter__(self):
        self.conn, token = pythond._connect()
        self.conn.timeout = self.timeout
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if self.request_cursor is not None:
            headers["Last-Event-ID"] = self.request_cursor
        self.conn.request("GET", self.path, headers=headers)
        self.response = self.conn.getresponse()
        if self.response.status != 200:
            body = self.response.read().decode("utf-8")
            self.close()
            raise RuntimeError(f"SSE {self.response.status}: {body}")
        self.cursor = self.response.getheader("X-Pythond-Event-Cursor")
        check("SSE content type", self.response.getheader("Content-Type").startswith(
            "text/event-stream"))
        return self

    def read(self, *, include_activity=False):
        """Legacy async/loss view, or all activity for protocol coverage."""
        fields = {}
        while True:
            line = self.response.readline(1024 * 1024)
            if not line:
                raise EOFError("event stream ended before expected event")
            line = line.decode("utf-8").rstrip("\r\n")
            if not line:
                if fields:
                    fields["data"] = json.loads(fields["data"])
                    if fields.get("event") == "ready":
                        check("ready event establishes resume cursor", fields["id"] == self.cursor and
                              fields["data"]["cursor"] == self.cursor)
                        fields = {}
                        continue
                    if not include_activity and (fields.get("event") == "session_created" or
                            fields["data"].get("sync") is True):
                        fields = {}
                        continue
                    return fields
                continue
            if not line.startswith(":"):
                key, _, value = line.partition(":")
                fields[key] = value.lstrip(" ")

    def close(self):
        self.response.close()
        self.conn.close()

    def __exit__(self, *exc):
        self.close()


def test_kill_all_snapshot():
    section("kill-all snapshots worker identity, not just names")
    first, old, newer, later, gone = [_fake_session() for _ in range(5)]
    closed = []
    def close(worker):
        closed.append(worker)
        if worker is first:
            # Concurrent replacement, creation and removal after the snapshot.
            with pythond._sessions_lock:
                pythond.sessions["second"] = newer
                pythond.sessions["later"] = later
                pythond.sessions.pop("gone")
    with mock.patch.dict(pythond.sessions, {"first": first, "second": old, "gone": gone}, clear=True), \
         mock.patch.object(pythond, "_close_session", side_effect=close), \
         mock.patch.object(pythond, "_session_closed") as event:
        check("only original surviving snapshot members are killed",
              pythond.kill_all_sessions() == ["first"])
        check("new same-name incarnation and later creation survive",
              pythond.sessions == {"second": newer, "later": later})
        check("close/event operate exactly once on selected worker",
              len(closed) == 1 and closed[0] is first and event.call_count == 1 and
              event.call_args.args == ("first", first, "killed"))


def test_kill_all_cli():
    section("kill CLI requires explicit name or --all")
    for entry in (pythond.pysh_main, pythond.main):
        for argv, path in ((["kill", "work"], "/kill/work"), (["kill", "--all"], "/kill")):
            with mock.patch.object(sys, "argv", ["pysh"] + argv), \
                 mock.patch.object(pythond, "_request", return_value=(200, {}, '{"killed": [], "count": 0}')) as req, \
                 contextlib.redirect_stdout(io.StringIO()):
                entry()
            check("kill CLI selects exact endpoint", req.call_args.args == ("POST", path))
        for argv in (["kill"], ["kill", "work", "--all"], ["kill", "--all", "work"]):
            with mock.patch.object(sys, "argv", ["pysh"] + argv), \
                 mock.patch.object(pythond, "_request") as req, \
                 contextlib.redirect_stderr(io.StringIO()):
                try:
                    entry()
                    check("ambiguous kill CLI rejected", False)
                except SystemExit as e:
                    check("ambiguous kill CLI rejected before HTTP", e.code == 2 and not req.called)


def test_integration_kill_all():
    section("INTEGRATION: kill-all preserves daemon, token, epoch, stream and history")
    with tempfile.TemporaryDirectory() as td, _Daemon(td):
        ids, history = {}, {}
        for name in ("work", "browser", "train"):
            _, headers, _ = pythond._request("POST", "/new/" + name)
            ids[name] = headers["X-Pythond-Session-Id"]
            pythond._request("POST", "/run/" + name, "value = 42")
            path = Path(td) / ".pythond/sessions" / name / "history.py"
            history[path] = path.read_bytes()
        metadata = pythond._read_meta()
        with _Events() as events:
            epoch = events.cursor.split(":")[0]
            status, headers, body = pythond._request("POST", "/kill")
            check("kill-all returns JSON collection", status == 200 and
                  headers.get("Content-Type") == "application/json" and
                  json.loads(body) == {"killed": list(ids), "count": 3})
            check("collection response has no single-worker identity",
                  "X-Pythond-Session-Id" not in headers)
            closed = [events.read(include_activity=True) for _ in ids]
            check("one killed event for each removed incarnation",
                  all(e["event"] == "session_closed" and e["data"]["reason"] == "killed" for e in closed) and
                  {e["data"]["session"]: e["data"]["session_id"] for e in closed} == ids)
            check("kill-all leaves event epoch unchanged",
                  all(e["id"].split(":")[0] == epoch for e in closed))
            status, _, body = pythond._request("GET", "/ls")
            check("daemon stays alive and empty", status == 200 and body == "(no sessions)")
            check("metadata/token unchanged", pythond._read_meta() == metadata)
            check("kill-all leaves checkpoint files unchanged",
                  all(p.read_bytes() == data for p, data in history.items()))
            status, _, body = pythond._request("POST", "/kill")
            check("empty kill-all succeeds idempotently", status == 200 and
                  json.loads(body) == {"killed": [], "count": 0})
            status, _, _ = pythond._request("POST", "/kill/work")
            check("missing single-session kill remains 404", status == 404)
            pythond._request("POST", "/new/fresh")
            created = events.read(include_activity=True)
            check("original SSE connection continues with new activity",
                  created["event"] == "session_created" and created["data"]["session"] == "fresh" and
                  created["id"].split(":")[0] == epoch)


def test_busy_admission_and_identity():
    section("busy admission, no side effects, identity on errors and removal")
    ns = pythond._init_namespace()
    lock = threading.Lock()
    execute = pythond._make_exec(ns, lock)
    cells = {}
    notifications = []
    with lock, mock.patch.object(pythond.pickle, "loads") as loads:
        for cmd, args in (("run", ["unwanted = 1"]), ("vars", []),
                          ("complete", ["os."]), ("dump", [""]),
                          ("load", ["x", "YWJj"])):
            before = time.monotonic()
            resp = pythond._dispatch(cmd, args, execute, cells, ns, lock,
                                     notify=notifications.append)
            check(f"{cmd} busy refusal is immediate", resp.get("busy") is True and
                  time.monotonic() - before < 1, resp)
        check("busy load never unpickles", not loads.called)
        status = pythond._dispatch("status", [], execute, cells, ns, lock)
        check("status remains available with unknown vars", status.get("vars", 0) is None)
    check("refused run neither executes nor emits completion",
          "unwanted" not in ns and not notifications)
    resp = pythond._dispatch("run", ["21 * 2"], execute, cells, ns, lock,
                             notify=notifications.append)
    check("execution usable after contention", resp["output"] == "42")
    check("sync completion matches result", notifications[0]["sync"] is True and
          notifications[0]["cell_id"] == resp["cell_id"] and
          notifications[0]["code_head"] == "21 * 2")

    s = _fake_session([json.dumps({"state": "idle"})])
    with _with_session("busy_test", s):
        with s["lock"]:
            status, headers, body = _cmd("GET", "status", "busy_test")
            check("daemon contention is 409 with actual incarnation", status == 409 and
                  "busy" in body and headers.get("X-Pythond-Session-Id") == s["id"])
            check("daemon busy neither writes nor poisons", not s["proc"].stdin.sent and
                  not s["unhealthy"])
        check("command usable after daemon contention",
              pythond.send_session("busy_test", "status", [])["state"] == "idle")
        for cmd, method in (("status", "GET"), ("pickle", "GET"), ("pickle", "POST")):
            s["unhealthy"] = True
            status, headers, body = _cmd(method, cmd, "busy_test")
            check("transport failure retains identity", status == 409 and
                  headers.get("X-Pythond-Session-Id") == s["id"])
        newer = _fake_session()
        with mock.patch.object(pythond, "_close_session",
                               side_effect=lambda old: pythond._publish_session("busy_test", newer)):
            status, headers, body = _cmd("POST", "kill", "busy_test")
            check("kill identifies removed worker even if name is reused", status == 200 and
                  headers.get("X-Pythond-Session-Id") == s["id"] and
                  pythond.sessions["busy_test"] is newer)


def test_fire_queue_preserved():
    section("fire remains serialized without blocking the control plane")
    ns = pythond._init_namespace()
    gate, started = threading.Event(), threading.Event()
    ns.update({"_gate": gate, "_started": started})
    lock = threading.Lock()
    execute = pythond._make_exec(ns, lock)
    cells, output = {}, queue.Queue()
    try:
        one = pythond._dispatch("fire", ["_started.set(); _gate.wait(); value = 40"],
                                execute, cells, ns, lock, output.put)
        check("first fire owns execution", started.wait(2))
        two = pythond._dispatch("fire", ["value += 2; print(value)"],
                                execute, cells, ns, lock, output.put)
        check("second fire accepted while first runs", two.get("status") == "fired")
        check("queued fire has not executed", "value" not in ns)
    finally:
        gate.set()
    done = {e["cell_id"]: e for e in (output.get(timeout=3), output.get(timeout=3))}
    check("both queued executions complete", set(done) == {one["cell_id"], two["cell_id"]})
    check("second execution uses first result", done[two["cell_id"]]["output"] == "42" and
          done[two["cell_id"]]["sync"] is False)


def test_integration_activity_and_long_busy():
    section("INTEGRATION: >30s fire, safe control plane, activity protocol and identity")
    with tempfile.TemporaryDirectory() as td, _Daemon(td), _Events(timeout=45) as events:
        status, headers, _ = pythond._request("POST", "/new/train")
        sid = headers["X-Pythond-Session-Id"]
        created = events.read(include_activity=True)
        check("activity has publication timestamp", isinstance(created["data"].get("timestamp"), (int, float)))
        check("created event matches successful new", status == 201 and
              created["event"] == "session_created" and created["data"]["session_id"] == sid and
              isinstance(created["data"]["pid"], int))
        status, headers, _ = pythond._request("POST", "/new/train")
        check("creation conflict identifies preserved worker", status == 409 and
              headers.get("X-Pythond-Session-Id") == sid)
        code = "print('你好')\n# " + "汉" * 600
        status, headers, body = pythond._request("POST", "/run/train", code)
        done = events.read(include_activity=True)
        check("run preserves HTTP body and correlates completion", status == 200 and body == "你好" and
              headers.get("X-Pythond-Cell-Id") == done["data"]["cell_id"] and
              done["data"]["session_id"] == sid and done["data"]["sync"] is True)
        check("code preview bounded UTF-8 prefix", len(done["data"]["code_head"].encode()) <= 512 and
              code.startswith(done["data"]["code_head"]) and done["data"]["output"] == "你好")
        status, headers, body = pythond._request("POST", "/run/train", "1/0")
        failed = events.read(include_activity=True)["data"]
        check("failed run emits synchronous error completion", status == 200 and
              headers.get("X-Pythond-Exec-Error") == "1" and failed["error"] is True and
              failed["sync"] is True and failed["cell_id"] != done["data"]["cell_id"])

        for method, path in (("GET", "/vars/train"), ("GET", "/status/train"),
                             ("GET", "/poll/train"), ("POST", "/complete/train"),
                             ("POST", "/int/train")):
            status, headers, _ = pythond._request(method, path)
            check("all normal session commands expose identity", status == 200 and
                  headers.get("X-Pythond-Session-Id") == sid, path)
        for method, path, expected in (("GET", "/pickle/train/missing", 404),
                                       ("POST", "/status/train", 405),
                                       ("POST", "/new/train?replace=bad", 400)):
            status, headers, _ = pythond._request(method, path)
            check("identified session errors retain identity", status == expected and
                  headers.get("X-Pythond-Session-Id") == sid, path)
        marker = str(Path(td) / "training-started")
        code = f"from pathlib import Path\nPath({marker!r}).touch()\nimport time\ntime.sleep(32)\ntrained = 42"
        status, headers, body = pythond._request("POST", "/fire/train", code)
        cid = json.loads(body)["cell_id"]
        check("long training accepted with incarnation", status == 202 and
              headers.get("X-Pythond-Session-Id") == sid)
        check("training really started", wait_until(lambda: Path(marker).exists()))
        for method, path, body in (("POST", "/run/train", "unwanted = 1"),
                                   ("GET", "/vars/train", ""),
                                   ("POST", "/complete/train", "os."),
                                   ("GET", "/pickle/train", ""),
                                   ("POST", "/pickle/train/x", pickle.dumps(7))):
            before = time.monotonic()
            status, headers, raw = pythond._request_bytes(method, path,
                body.encode("utf-8") if isinstance(body, str) else body)
            body = raw.decode("utf-8", "replace")
            check("busy HTTP operation returns promptly with identity", status == 409 and
                  "busy" in body and time.monotonic() - before < 2 and
                  headers.get("X-Pythond-Session-Id") == sid, (path, status, body))
        status, headers, body = pythond._request("GET", "/status/train")
        check("status works throughout long fire", status == 200 and
              json.loads(body)["vars"] is None and cid in json.loads(body)["running"] and
              headers.get("X-Pythond-Session-Id") == sid)
        status, headers, body = pythond._request("GET", f"/poll/train?cell={cid}")
        check("poll still works after refused commands", status == 200 and
              json.loads(body)["status"] == "running" and headers.get("X-Pythond-Session-Id") == sid)
        done = events.read(include_activity=True)
        check("training survives past command timeout and emits only its completion",
              done["event"] == "cell_done" and done["data"]["cell_id"] == cid and
              done["data"]["sync"] is False and not done["data"]["error"])
        status, headers, body = pythond._request("POST", "/run/train", "trained, 'unwanted' in globals()")
        check("channel and trained objects survive", status == 200 and body == "(42, False)", body)
        events.read(include_activity=True)
        status, headers, body = pythond._request("GET", "/pickle/train/trained")
        check("pickle GET success has identity", status == 200 and headers.get("X-Pythond-Session-Id") == sid)
        status, headers, body = pythond._request_bytes("POST", "/pickle/train/copied", pickle.dumps(9))
        check("pickle POST success has identity", status == 200 and headers.get("X-Pythond-Session-Id") == sid)
        status, headers, body = pythond._request("POST", "/kill/train")
        closed = events.read(include_activity=True)
        check("kill and close event identify exact removed worker", status == 200 and
              headers.get("X-Pythond-Session-Id") == sid and closed["data"]["session_id"] == sid and
              closed["event"] == "session_closed")
        status, headers, body = pythond._request("GET", "/status/train")
        check("missing worker never gets fabricated identity", status == 404 and
              "X-Pythond-Session-Id" not in headers)


def test_integration_safe_new():
    section("INTEGRATION: safe new, explicit replacement, concurrent creators")
    with tempfile.TemporaryDirectory() as td, _Daemon(td):
        status, headers, original = pythond._request("POST", "/new/work")
        check("initial creation returns 201", status == 201, original)
        check("creation Location points to session health", headers.get("Location") == "/status/work")
        check("creation receipt includes incarnation", bool(headers.get("X-Pythond-Session-Id")))
        pythond._request("POST", "/run/work", "sentinel = object(); marker = id(sentinel)")
        for suffix, expected in (("", 409), ("?replace=0", 409),
                                 ("?replace=", 400), ("?replace=true", 400),
                                 ("?replace=1&replace=0", 400)):
            status, _h, text = pythond._request("POST", "/new/work" + suffix)
            check("repeat new preserves state", status == expected, text)
        status, _h, text = pythond._request("POST", "/run/work", "id(sentinel) == marker")
        check("live object identity survives refused new", status == 200 and text == "True")
        status, _h, fresh = pythond._request("POST", "/new/work?replace=1")
        check("explicit replacement has fresh PID", status == 201 and fresh != original)
        _s, _h, text = pythond._request("GET", "/vars/work")
        check("explicit replacement discards variables", "sentinel" not in json.loads(text)["vars"])

        barrier = threading.Barrier(8)
        results = []
        def create():
            barrier.wait(timeout=5)
            results.append(pythond._request("POST", "/new/race"))
        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        check("eight concurrent creators: one success, seven conflicts",
              sorted(r[0] for r in results) == [201] + [409] * 7, results)
        _s, _h, listing = pythond._request("GET", "/ls")
        check("only one race session published", listing.count("race:") == 1, listing)
        _s, _h, text = pythond._request("POST", "/run/race", "6 * 7")
        check("winning worker remains usable", text == "42", text)


def test_integration_events():
    section("INTEGRATION: pushed completion, replay, IPC isolation, session loss")
    with tempfile.TemporaryDirectory() as td, _Daemon(td) as d:
        pythond._request("POST", "/new/work")
        with _Events() as a, _Events() as b:
            status, headers, text = pythond._request("POST", "/fire/work", "print('你好\\nsecond line')")
            cid = json.loads(text)["cell_id"]
            check("fire returns 202 and status Location", status == 202 and
                  headers.get("Location") == f"/poll/work?cell={cid}")
            one, other = a.read(), b.read()
            check("all subscribers receive same event", one == other)
            data = one["data"]
            incarnation = data["session_id"]
            check("completion matches receipt incarnation",
                  incarnation == headers.get("X-Pythond-Session-Id"))
            check("instant Unicode completion associated with ACK",
                  one["event"] == "cell_done" and data["cell_id"] == cid and
                  data["session"] == "work" and data["output"] == "你好\nsecond line" and
                  data["error"] is False, one)

            pythond._request("POST", "/fire/work", "raise ValueError('bad-news')")
            failure = a.read()
            check("exception completion has error flag", failure["data"]["error"] is True and
                  "ValueError" in failure["data"]["output"])
            check("exception multicast", b.read() == failure)

            ids = []
            for i in range(12):
                _s, _h, text = pythond._request("POST", "/fire/work", f"print({i})")
                ids.append(json.loads(text)["cell_id"])
                _s, _h, text = pythond._request("GET", "/status/work")
                check("events never replace command replies", "state" in json.loads(text), text)
            delivered = [a.read() for _ in ids]
            check("rapid completions are neither lost nor duplicated",
                  [e["data"]["cell_id"] for e in delivered] == ids)
            check("independent subscriber cursors", [b.read() for _ in ids] == delivered)
            cursor = delivered[-1]["id"]
            history = (Path(td) / ".pythond/sessions/work/history.py").read_text(encoding="utf-8")
            check("async checkpoint without poll", "print(11)" in history and
                  "bad-news" not in history)

        # No subscribers are required for the cell to run or for log retention.
        _s, _h, text = pythond._request("POST", "/fire/work",
                                       "import time; time.sleep(0.1); survived = 17")
        cid = json.loads(text)["cell_id"]
        with _Events(cursor) as resumed:
            completed = resumed.read()
            check("disconnect doesn't abort work", completed["data"]["cell_id"] == cid and
                  completed["data"]["error"] is False)
        with _Events(cursor) as repeated:
            check("replayed event keeps its deduplication ID", repeated.read() == completed)
            _s, _h, text = pythond._request("POST", "/run/work", "survived")
            check("namespace survives resubscription", text == "17", text)

            _s, _h, text = pythond._request("POST", "/fire/work", "time.sleep(0.1); print('done')")
            cid = json.loads(text)["cell_id"]
            _s, _h, text = pythond._request("POST", "/run/work", "time.sleep(0.2); 42")
            check("run refuses execution-lock contention", _s == 409 and "busy" in text, text)
            check("completion after refused run is correctly framed", repeated.read()["data"]["cell_id"] == cid)
            _s, _h, text = pythond._request("POST", "/run/work", "42")
            check("busy refusal does not poison channel", _s == 200 and text == "42", text)
            check("normal notification path made no poll requests", "/poll/" not in d.stderr())

            _s, headers, _text = pythond._request("POST", "/fire/work", "print('汉' * 30000)")
            large = repeated.read()["data"]
            check("large notification bounded and flagged", large["output_truncated"] and
                  large["output_bytes"] == 90000 and len(large["output"].encode()) <= 65536)
            _s, _h, text = pythond._request("GET", headers["Location"])
            check("Location fetches full output without parsing receipt body", json.loads(text)["output"] == "汉" * 30000)

            pythond._request("POST", "/kill/work")
            ended = repeated.read()
            check("kill emits session loss", ended["event"] == "session_closed" and
                  ended["data"]["session_id"] == incarnation and ended["data"]["reason"] == "killed")
            pythond._request("POST", "/new/work")
            pythond._request("POST", "/fire/work", "1 + 1")
            check("reused name has different incarnation",
                  repeated.read()["data"]["session_id"] != incarnation)
            pythond._request("POST", "/run/work", "os._exit(7)")
            check("worker crash notifies subscribers", repeated.read()["data"]["reason"] == "exited")
            pythond._request("POST", "/stop")
            check("daemon stop closes idle stream promptly", repeated.response.read() == b"")


def test_integration_event_cursors():
    section("INTEGRATION: cursor validation, retention gaps, daemon epoch")
    with tempfile.TemporaryDirectory() as td:
        with _Daemon(td, {"PYTHOND_MAX_EVENTS": "2"}):
            pythond._request("POST", "/new/work")
            with _Events() as events:
                origin = events.cursor
                delivered = []
                for i in range(3):
                    pythond._request("POST", "/fire/work", str(i))
                    delivered.append(events.read())
            for cursor, expected in ((origin, 410), ("bad", 400), ("", 400),
                                     (origin.split(":")[0] + ":99", 409)):
                status, _h, body = pythond._request("GET", "/events?since=" + cursor)
                check("invalid or stale cursor HTTP status", status == expected, body)
                check("cursor errors machine readable", "error" in json.loads(body))
            status, _h, _body = pythond._request("GET", "/events?since=x&since=y")
            check("multiple cursors rejected", status == 400)
            status, _h, _body = pythond._request("POST", "/events")
            check("events is GET only", status == 405)
            with _Events(path="/events?since=" + delivered[-2]["id"]) as events:
                check("query cursor replay", events.read() == delivered[-1])
            with _Events(delivered[-2]["id"], "/events?since=bad") as events:
                check("Last-Event-ID takes precedence", events.read() == delivered[-1])
        with _Daemon(td):
            status, _h, body = pythond._request("GET", "/events?since=" + delivered[-1]["id"])
            check("restart rejects old epoch", status == 409 and
                  json.loads(body)["error"] == "event_epoch_changed", body)


def test_integration_event_fork():
    section("INTEGRATION: fork completion and interrupt events")
    if sys.platform == "win32":
        check("fork events skipped on Windows", True)
        return
    with tempfile.TemporaryDirectory() as td, _Daemon(td):
        pythond._request("POST", "/new/work")
        with _Events() as events:
            status, headers, text = pythond._request("POST", "/fork/work", "answer = 42")
            check("fork returns 202 and status Location", status == 202 and
                  headers.get("Location") == f"/poll/work?cell={json.loads(text)['cell_id']}")
            data = events.read()["data"]
            check("fork completion after merge", not data["error"] and data["merged_count"] >= 1)
            _s, _h, text = pythond._request("POST", "/run/work", "answer")
            check("pushed fork result already merged", text == "42")
            pythond._request("POST", "/fork/work", "time.sleep(30)")
            pythond._request("POST", "/int/work")
            check("killed fork emits failure completion", events.read()["data"]["error"] is True)


def test_integration_lifecycle():
    section("INTEGRATION: daemon lifecycle over HTTP")
    name = "__it_life__"
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                               "sessions", name), ignore_errors=True)
    with tempfile.TemporaryDirectory() as td, _Daemon(td) as d:
        status, _h, text = pythond._request("GET", "/ls")
        check("ls empty", status == 200 and "(no sessions)" in text, text)
        check("HTTP protocol capability advertised", _h.get("X-Pythond-Protocol") == "2")

        status, _h, text = pythond._request("POST", f"/new/{name}")
        check("new Created", status == 201 and f"OK {name}" in text, text)

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

        # Concurrent synchronous callers are admitted or refused, never queued
        # behind an unbounded command or allowed to interleave namespace writes.
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
        accepted = sum(r[0] == 200 for r in results)
        check("concurrent runs execute exactly the admitted count",
              accepted > 0 and text.strip() == str(accepted), (text, results))
        check("other concurrent runs are clean busy refusals",
              all(r[0] == 200 or (r[0] == 409 and "busy" in r[2]) for r in results))

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
            event_status, _h, event_text = pythond._request("GET", "/events")
        check("wrong token 401", status == 401 and "auth failed" in text, text)
        check("event stream uses same authentication", event_status == 401 and
              "auth failed" in event_text)
        pythond._request("POST", "/stop")


def test_integration_second_daemon_fails():
    section("INTEGRATION: second daemon fails loud")
    with tempfile.TemporaryDirectory() as td, _Daemon(td) as d:
        # Windows: the TCP bind fails (no SO_REUSEADDR).  POSIX: the socket
        # liveness probe refuses to take over a live daemon.  Both exit 1.
        second = subprocess.run(
            [sys.executable, str(ROOT / "pythond.py"), "daemon"],
            env=d.env, capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        check("second daemon exits nonzero", second.returncode == 1,
              second.stderr)
        check("second daemon says why",
              "cannot start daemon" in second.stderr, second.stderr)
        status, _h, _t = pythond._request("GET", "/ls")
        check("first daemon still serving", status == 200)
        pythond._request("POST", "/stop")
    if _HAS_AF_UNIX:
        # A stale socket (no daemon accepting) must NOT block startup.
        with tempfile.TemporaryDirectory() as td:
            stale = os.path.join(td, "pythond.sock")
            holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            holder.bind(stale)
            holder.close()  # socket file left behind, nothing accepting
            env = os.environ.copy()
            env["PYTHOND_SOCK"] = stale
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "pythond.py"), "daemon"],
                env=env, stderr=subprocess.PIPE, text=True,
            )
            try:
                up = wait_until(lambda: proc.poll() is None and
                                os.path.exists(stale) and
                                _unix_socket_alive(stale))
                check("stale socket is replaced", up,
                      proc.stderr.read() if proc.poll() is not None else "")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)


def _unix_socket_alive(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_integration_pickle_cp():
    section("INTEGRATION: pickle endpoints + cp")
    import pickle as _p
    a, b = "__it_pk_a__", "__it_pk_b__"
    with tempfile.TemporaryDirectory() as td, _Daemon(td):
        pythond._request("POST", f"/new/{a}")
        pythond._request("POST", f"/new/{b}")
        pythond._request("POST", f"/run/{a}", "df = {'rows': 100}")

        status, hdrs, raw = pythond._request_bytes("GET", f"/pickle/{a}/df")
        check("GET pickle 200", status == 200)
        check("GET pickle roundtrips", _p.loads(raw) == {"rows": 100})

        status, _h, out = pythond._request_bytes("POST", f"/pickle/{b}/df2", raw)
        check("POST pickle 200", status == 200, out)
        _s, _h, text = pythond._request("POST", f"/run/{b}", "df2['rows'] + 1")
        check("posted object live in other session", text.strip() == "101", text)

        status, hdrs, raw = pythond._request_bytes("GET", f"/pickle/{a}")
        check("whole-namespace GET 200", status == 200)
        check("whole-namespace skips modules",
              "os" in hdrs.get("X-Pythond-Skipped", ""), hdrs)
        check("whole-namespace has df", _p.loads(raw).get("df") == {"rows": 100})

        pkl = os.path.join(td, "df.pkl")
        with mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            pythond.client("cp", [f"{a}:df", pkl])
            pythond.client("cp", [pkl, f"{b}:df3"])
        _s, _h, text = pythond._request("POST", f"/run/{b}", "df3 == df2")
        check("cp via file roundtrips", text.strip() == "True", text)

        status, _h, text = pythond._request("GET", f"/pickle/{a}/nope")
        check("missing var 404", status == 404, text)
        pythond._request("POST", "/stop")
    for name in (a, b):
        shutil.rmtree(os.path.join(os.path.expanduser("~"), ".pythond",
                                   "sessions", name), ignore_errors=True)


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
        test_async_http_receipts,
        test_async_history_events,
        test_new_safe_defaults,
        test_event_log,
        test_event_stream_reset,
        test_completion_snapshot,
        test_dispatch_dump_load,
        test_daemon_command_pickle_route,
        test_parse_cp_target,
        test_client_cp,
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
        test_kill_all_snapshot,
        test_kill_all_cli,
        test_integration_kill_all,
        test_busy_admission_and_identity,
        test_fire_queue_preserved,
        test_integration_activity_and_long_busy,
        test_integration_safe_new,
        test_integration_events,
        test_integration_event_cursors,
        test_integration_event_fork,
        test_integration_lifecycle,
        test_integration_crash_isolation,
        test_integration_auth,
        test_integration_second_daemon_fails,
        test_integration_pickle_cp,
        test_integration_fork,
    ]
    registered = {fn.__name__ for fn in tests}
    discovered = {name for name, obj in globals().items()
                  if name.startswith("test_") and callable(obj)}
    missing = sorted(discovered - registered)
    check("all test functions registered", not missing, ", ".join(missing))
    # Unit checkpoint tests must not write/delete anything in the real home.
    with tempfile.TemporaryDirectory(prefix="pythond-tests-") as home, \
         mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
        for fn in tests:
            fn()

    print(f"\n{'='*40}")
    print(f"  {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
    print("  all clear")


if __name__ == "__main__":
    main()
