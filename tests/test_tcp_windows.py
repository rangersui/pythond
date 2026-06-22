#!/usr/bin/env python3
"""Windows TCP smoke tests.

Starts a real daemon on a loopback TCP port, reads the daemon token from
private daemon.json metadata, then exercises the public CLI client path.
Clients should work without K_TOKEN because the daemon writes that metadata.

Requires: Windows.  The session may use WinPTY when pywinpty is installed, or
the socket-console fallback otherwise.
Skip on non-Windows; POSIX PTY coverage lives in test_pty_posix.py.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_TTY = ROOT / "agent_tty.py"

PASS = 0
FAIL = 0


def check(name, expect, actual):
    global PASS, FAIL
    if expect in actual:
        PASS += 1
    else:
        print(f"  X {name}")
        print(f"    expect: {expect!r}")
        first = actual.splitlines()[0] if actual.splitlines() else actual
        print(f"    actual: {first[:160]!r}")
        FAIL += 1


def check_absent(name, needle, actual):
    global PASS, FAIL
    if needle not in actual:
        PASS += 1
    else:
        print(f"  X {name}")
        print(f"    expect absent: {needle!r}")
        first = actual.splitlines()[0] if actual.splitlines() else actual
        print(f"    actual: {first[:160]!r}")
        FAIL += 1


def check_json(name, actual):
    global PASS, FAIL
    try:
        value = json.loads(actual)
    except json.JSONDecodeError:
        print(f"  X {name}")
        print("    expect: JSON")
        first = actual.splitlines()[0] if actual.splitlines() else actual
        print(f"    actual: {first[:160]!r}")
        FAIL += 1
        return None
    PASS += 1
    return value


def free_tcp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_k(env, *args):
    p = subprocess.run(
        [sys.executable, "-B", str(AGENT_TTY), *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def daemon_meta_path(env):
    base = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
    return Path(base) / "agent-tty" / "daemon.json"


def check_attach_reads_daemon_meta(env, expected_token):
    """Unit-check attach() token selection without entering interactive mode."""
    global PASS, FAIL
    spec = importlib.util.spec_from_file_location("agent_tty_under_test",
                                                  AGENT_TTY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}
    old_localappdata = os.environ.get("LOCALAPPDATA")
    old_token = os.environ.pop("K_TOKEN", None)
    os.environ["LOCALAPPDATA"] = env["LOCALAPPDATA"]
    try:
        mod._send = lambda cmd, args: "12345"
        mod._HAS_PTY = True
        mod._attach_pty_win = (
            lambda port, token, name: captured.update(
                {"port": port, "token": token, "name": name}))
        mod.attach("test1")
        if captured.get("token") == expected_token:
            PASS += 1
        else:
            print("  X attach-reads-daemon-meta-token")
            print(f"    expect: {expected_token!r}")
            print(f"    actual: {captured.get('token')!r}")
            FAIL += 1
    finally:
        if old_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_localappdata
        if old_token is not None:
            os.environ["K_TOKEN"] = old_token


def wait_for_daemon(daemon, stderr_lines, meta_path):
    """Return first stderr lines after daemon writes TCP metadata."""
    lines = []
    deadline = time.time() + 10
    while time.time() < deadline:
        if daemon.poll() is not None:
            break
        try:
            line = stderr_lines.get(timeout=0.1)
        except queue.Empty:
            if meta_path.exists():
                return lines
            continue
        line = line.rstrip("\n")
        lines.append(line)
        if meta_path.exists():
            return lines
    return lines


def check_second_daemon_rejected(env):
    second_env = env.copy()
    second_env["K_PORT"] = str(free_tcp_port())
    try:
        p = subprocess.run(
            [sys.executable, "-B", str(AGENT_TTY), "daemon"],
            env=second_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except subprocess.TimeoutExpired as e:
        global FAIL
        FAIL += 1
        print("  X second-daemon-rejected")
        print("    expect: fast failure")
        print(f"    actual: timeout stdout={e.stdout!r} stderr={e.stderr!r}")
        return
    check("second-daemon-rejected",
          "daemon metadata already points to live daemon",
          p.stdout + p.stderr)
    check("second-daemon-exit-nonzero", "1", str(p.returncode))


def main():
    if sys.platform != "win32":
        print("SKIP: Windows TCP tests not applicable on this platform")
        return 0

    port = free_tcp_port()
    env = os.environ.copy()
    env["K_PORT"] = str(port)
    env["LOCALAPPDATA"] = str(ROOT / ".tmp-agent-tty-test-localappdata")
    env.pop("K_TOKEN", None)
    meta_path = daemon_meta_path(env)
    if meta_path.exists():
        meta_path.unlink()

    daemon = subprocess.Popen(
        [sys.executable, "-B", str(AGENT_TTY), "daemon"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    stderr_lines = queue.Queue()

    def read_stderr():
        for line in daemon.stderr:
            stderr_lines.put(line)

    threading.Thread(target=read_stderr, daemon=True).start()

    try:
        startup_lines = wait_for_daemon(daemon, stderr_lines, meta_path)
        if not meta_path.exists():
            print("  X daemon-token")
            print("    expect: daemon.json metadata")
            print(f"    stderr: {startup_lines!r}")
            return 1

        print("=== Windows TCP regression tests ===")

        joined_startup = "\n".join(startup_lines)
        check_absent("daemon-hides-token-line", "token=", joined_startup)
        check_absent("daemon-hides-set-token", "K_TOKEN=", joined_startup)
        check("daemon-writes-meta", "daemon.json", str(meta_path))
        check("daemon-meta-exists", "True", str(meta_path.exists()))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        token = meta.get("token", "")
        if token:
            global PASS, FAIL
            PASS += 1
        else:
            print("  X daemon-meta-token-present")
            print("    expect: non-empty token")
            print(f"    actual: {meta!r}")
            FAIL += 1
        check("daemon-meta-port", str(port), str(meta.get("port")))
        check_second_daemon_rejected(env)
        check_attach_reads_daemon_meta(env, token)

        # No K_TOKEN env should still work via daemon.json.
        _, out, err = run_k(env, "ls")
        check("token-file-auth", "(no sessions)", out + err)

        client_env = env.copy()
        client_env["K_TOKEN"] = token

        # -- ls empty --
        _, out, err = run_k(client_env, "ls")
        check("ls-empty", "(no sessions)", out + err)

        # -- new session --
        _, out, err = run_k(client_env, "new", "test1")
        check("new-session", "OK test1 pid=", out + err)

        # -- ls shows session --
        _, out, err = run_k(client_env, "ls")
        check("ls-has-session", "test1", out + err)
        check("ls-alive", "alive", out + err)

        # -- run expression --
        _, out, err = run_k(client_env, "run", "test1", "2+2")
        check("run-expr", "4", out + err)

        # -- run statement produces no output but preserves state --
        _, out, err = run_k(client_env, "run", "test1", "x = 99")
        check("run-stmt-empty", "", out + err)

        _, out, err = run_k(client_env, "run", "test1", "x")
        check("run-namespace-persist", "99", out + err)

        # -- print and multiline exec --
        _, out, err = run_k(client_env, "run", "test1", "print('hello world')")
        check("run-print", "hello world", out + err)

        _, out, err = run_k(client_env, "run", "test1",
                            "for i in range(3): print(i)")
        check("run-multiline", "0", out + err)
        check("run-multiline-2", "2", out + err)

        # -- errors are captured, not daemon-fatal --
        _, out, err = run_k(client_env, "run", "test1", "1/0")
        check("run-error", "ZeroDivisionError", out + err)

        # -- fire + poll --
        _, out, err = run_k(client_env, "fire", "test1", "y = 42")
        cell = check_json("fire-returns-json", out + err)
        cell_id = cell.get("cell_id", "") if cell else ""
        check("fire-returns-cell-id", "cell_id", out + err)
        if cell_id:
            deadline = time.time() + 10
            poll = ""
            while time.time() < deadline:
                _, out, err = run_k(client_env, "poll", "test1", cell_id)
                poll = out + err
                if '"done"' in poll:
                    break
                time.sleep(0.1)
            check("poll-done", "done", poll)

        _, out, err = run_k(client_env, "run", "test1", "y")
        check("fire-state-persisted", "42", out + err)

        # -- status / vars / complete --
        _, out, err = run_k(client_env, "status", "test1")
        status = check_json("status-json", out + err)
        if status:
            check("status-idle", "idle", status.get("state", ""))

        _, out, err = run_k(client_env, "vars", "test1")
        check("vars-has-x", "x", out + err)
        check("vars-has-y", "y", out + err)

        _, out, err = run_k(client_env, "run", "test1", "import os")
        _, out, err = run_k(client_env, "complete", "test1", "os.path.")
        check("complete-returns", "join", out + err)

        # -- interrupt async cell --
        _, out, err = run_k(client_env, "fire", "test1",
                            "while True:\n    pass")
        cell = check_json("interrupt-fire-json", out + err)
        _, out, err = run_k(client_env, "int", "test1")
        check("int-ok", "OK interrupted test1", out + err)
        if cell and cell.get("cell_id"):
            deadline = time.time() + 10
            poll = ""
            while time.time() < deadline:
                _, out, err = run_k(client_env, "poll", "test1", cell["cell_id"])
                poll = out + err
                if '"done"' in poll:
                    break
                time.sleep(0.1)
            check("int-cell-done", "done", poll)

        # -- kill session --
        _, out, err = run_k(client_env, "kill", "test1")
        check("kill-ok", "OK killed test1", out + err)

        _, out, err = run_k(client_env, "ls")
        check("ls-after-kill", "(no sessions)", out + err)

        _, out, err = run_k(client_env, "run", "test1", "1")
        check("run-dead-session", "ERR", out + err)

    finally:
        if daemon.poll() is None:
            _, out, err = run_k(env, "stop")
            check("daemon-stop-ok", "OK stopping daemon", out + err)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=2)
        deadline = time.time() + 5
        while time.time() < deadline and meta_path.exists():
            time.sleep(0.1)
        check("daemon-removes-meta", "False", str(meta_path.exists()))

    print()
    print(f"=== {PASS} passed, {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
