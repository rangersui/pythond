"""Optional desktop observer for pythond. No GUI imports until tray_main()."""
from __future__ import annotations

import argparse
import codecs
from collections import deque
from dataclasses import dataclass
import errno
import http.client
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import pythond


@dataclass(frozen=True)
class Frame:
    event: str
    data: dict[str, Any]
    id: str | None = None


class SSEParser:
    """Incremental UTF-8, CRLF/LF, comments and multiline JSON data."""
    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")()
        self.buffer = ""

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buffer += self.decoder.decode(chunk)
        self.buffer = self.buffer.replace("\r\n", "\n")
        frames = []
        while "\n\n" in self.buffer:
            text, self.buffer = self.buffer.split("\n\n", 1)
            if len(text.encode("utf-8")) > 512 * 1024:
                raise ValueError("SSE frame exceeds 512 KiB")
            event, event_id, data = "message", None, []
            for line in text.split("\n"):
                key, sep, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if not sep or not key:
                    continue
                if key == "event":
                    event = value
                elif key == "id" and "\0" not in value:
                    event_id = value
                elif key == "data":
                    data.append(value)
            if data:
                obj = json.loads("\n".join(data))
                if not isinstance(obj, dict):
                    raise ValueError("SSE data must be an object")
                frames.append(Frame(event, obj, event_id))
        if len(self.buffer.encode("utf-8")) > 512 * 1024:
            raise ValueError("SSE frame exceeds 512 KiB")
        return frames


@dataclass(frozen=True)
class Session:
    session_id: str | None
    pid: int
    created_at: float | None


def age(when: float | None, now: float | None = None) -> str:
    if when is None:
        return "age unknown"
    seconds = max(0, int((time.time() if now is None else now) - when))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def snapshot(text: str) -> dict[str, Session]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([a-z0-9_-]+): (?:alive|DEAD) pid=(\d+)\s*", line)
        if match:
            # /ls supplies no incarnation or creation timestamp. Do not invent them.
            result[match[1]] = Session(None, int(match[2]), None)
        elif line.strip() and line.strip() != "(no sessions)":
            raise ValueError("Unrecognized /ls response")
    return result


class TrayState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.connected = False
        self.connection_error: str | None = None
        self.sessions: dict[str, Session] = {}
        self.recent: deque[tuple[str, float]] = deque(maxlen=5)
        self.cursor: str | None = None
        self.need_snapshot = True
        self.seen: deque[str] = deque(maxlen=512)

    def view(self) -> tuple[bool, dict[str, Session], list[tuple[str, float]]]:
        with self.lock:
            return self.connected and not self.need_snapshot, dict(self.sessions), list(reversed(self.recent))

    def connection_label(self) -> str:
        with self.lock:
            if self.connected and not self.need_snapshot:
                return f"{len(self.sessions)} sessions"
            if self.connection_error and "HTTP 401" in self.connection_error:
                return "Authentication failed - sessions unknown"
            if self.connection_error:
                return "Connection error - sessions unknown"
            return "Offline - sessions unknown"

    def note(self, text: str, when: float | None = None) -> None:
        with self.lock:
            self.recent.append((text, time.time() if when is None else when))

    def reset(self) -> None:
        with self.lock:
            self.connected = False
            self.connection_error = None
            self.sessions.clear()
            self.cursor = None
            self.need_snapshot = True
            self.seen.clear()

    def apply(self, frame: Frame) -> None:
        with self.lock:
            if frame.id and frame.id in self.seen:
                return
            d = frame.data
            name = str(d.get("session", ""))
            when = float(d.get("timestamp", time.time()))
            if frame.event == "session_created":
                self.sessions[name] = Session(str(d["session_id"]), int(d["pid"]), when)
            elif frame.event == "session_closed":
                current = self.sessions.get(name)
                if current and (current.session_id is None or current.session_id == d.get("session_id")):
                    del self.sessions[name]
                self.note(f"{name} closed ({d.get('reason', '?')})", when)
            elif frame.event == "cell_done":
                head = " ".join(str(d.get("code_head", "")).split())[:40]
                mode = "run" if d.get("sync") else "fire"
                mark = "ERR" if d.get("error") else "OK"
                self.note(f"{name} {mode} {mark} {head}", when)
            if frame.id:
                self.cursor = frame.id
                self.seen.append(frame.id)


def offline(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.ENOENT, errno.ECONNREFUSED)


class Resources:
    """Local process-tree samples, on menu refresh only; never daemon polling."""
    def __init__(self, psutil: Any) -> None:
        self.psutil = psutil
        self.lock = threading.Lock()
        self.previous: dict[int, tuple[tuple[int, float], float, dict[tuple[int, float], float], float | None]] = {}

    def sample(self, pid: int) -> str:
        with self.lock:
            return self._sample(pid)

    def _sample(self, pid: int) -> str:
        ps = self.psutil
        now = time.monotonic()
        try:
            root = ps.Process(pid)
            identity = (pid, root.create_time())
            partial = False
            try:
                processes = [root] + root.children(recursive=True)
            except (ps.NoSuchProcess, ps.AccessDenied):
                processes = [root]
                partial = True
            counters, rss = {}, 0
            for proc in processes:
                try:
                    key = (proc.pid, proc.create_time())
                    if key in counters:
                        continue
                    times = proc.cpu_times()
                    memory = proc.memory_info().rss
                    counters[key] = times.user + times.system
                    rss += memory
                except (ps.NoSuchProcess, ps.AccessDenied):
                    partial = True
            if identity not in counters:
                return "RSS n/a  CPU n/a"
        except (ps.NoSuchProcess, ps.AccessDenied):
            self.previous.pop(pid, None)
            return "RSS n/a  CPU n/a"
        prior = self.previous.get(pid)
        cpu = None
        if prior and prior[0] == identity:
            elapsed = now - prior[1]
            if elapsed < .25:
                cpu = prior[3]
            else:
                cpu = 100 * sum(max(0, value - prior[2].get(key, value))
                                for key, value in counters.items()) / elapsed
        if not prior or prior[0] != identity or now - prior[1] >= .25:
            self.previous[pid] = (identity, now, counters, cpu)
        if len(self.previous) > 512:
            self.previous = {pid: self.previous[pid]} if pid in self.previous else {}
        return f"RSS {rss / 1048576:.0f} MiB{'+' if partial else ''}  CPU {f'{cpu:.0f}%' if cpu is not None else 'n/a'}"


class TrayClient:
    """HTTP/SSE controller; GUI callbacks are injected and optional."""
    def __init__(self, state: TrayState | None = None,
                 changed: Callable[[], None] = lambda: None,
                 quit_ui: Callable[[], None] = lambda: None,
                 reconnect_delay: float = 2.0) -> None:
        self.state = state or TrayState()
        self.changed, self.quit_ui = changed, quit_ui
        self.reconnect_delay = reconnect_delay
        self.stopping = threading.Event()
        self.starting = threading.Event()
        self.exiting = threading.Event()
        self.connected_ready = threading.Event()
        self.skip_delay = False
        self.action_lock = threading.Lock()
        self.connection_lock = threading.Lock()
        self.connection: Any = None
        self.stream_socket: Any = None
        self.wake_read, self.wake_write = socket.socketpair()
        self.wake_write.setblocking(False)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.listen, name="pythond-tray-events", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Quit the observer only; never POST stop here."""
        self.stopping.set()
        self.reconnect()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        if not self.thread or not self.thread.is_alive():
            self.wake_read.close()
            self.wake_write.close()

    def reconnect(self) -> None:
        try:
            self.wake_write.send(b"x")
        except OSError:
            pass

    def wait_reconnect(self) -> None:
        if self.stopping.is_set():
            return
        if self.skip_delay:
            self.skip_delay = False
            return
        readable, _, _ = select.select([self.wake_read], [], [], self.reconnect_delay)
        if readable:
            self.wake_read.recv(4096)

    def read_stream(self, response):
        # Core SSE is close-delimited. Read the http.client buffer before select
        # so a ready frame prefetched with HTTP headers is delivered immediately.
        if response.chunked:
            raise ValueError("Chunked SSE transport is not supported; connect directly to pythond")
        stream = self.stream_socket
        stream.setblocking(False)
        while not self.stopping.is_set():
            chunk = response.fp.read1(65536)
            if chunk:
                yield chunk
                continue
            readable, _, _ = select.select([stream, self.wake_read], [], [], 40)
            if self.wake_read in readable:
                self.wake_read.recv(4096)
                self.skip_delay = True
                return
            if not readable:
                raise TimeoutError("SSE heartbeat timed out")
            chunk = response.fp.read1(65536)
            if not chunk:
                raise EOFError("SSE disconnected")
            yield chunk

    def ready(self, frame: Frame) -> None:
        with self.state.lock:
            if self.state.cursor and frame.id and self.state.cursor.split(":")[0] != frame.id.split(":")[0]:
                self.state.reset()
            needs_snapshot = self.state.need_snapshot
        if needs_snapshot:
            status, _, body = pythond._request("GET", "/ls")
            if status != 200:
                raise RuntimeError(f"snapshot HTTP {status}")
            initial = snapshot(body)
            with self.state.lock:
                self.state.sessions = initial
                self.state.need_snapshot = False
        with self.state.lock:
            self.state.connected = True
            self.state.connection_error = None
            self.state.cursor = frame.id
            self.connected_ready.set()
        self.changed()

    def listen(self) -> None:
        while not self.stopping.is_set():
            conn = response = None
            try:
                # Re-read endpoint/token for every reconnect, through core discovery.
                conn, token = pythond._connect()
                conn.timeout = 40  # Heartbeat is every 15 seconds.
                with self.connection_lock:
                    self.connection = conn
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                with self.state.lock:
                    if self.state.cursor:
                        headers["Last-Event-ID"] = self.state.cursor
                conn.request("GET", "/events", headers=headers)
                # getresponse() detaches conn.sock for Connection: close SSE.
                with self.connection_lock:
                    self.stream_socket = conn.sock
                if self.stopping.is_set():
                    break
                response = conn.getresponse()
                if response.status in (400, 409, 410):
                    self.state.reset()
                    self.changed()
                    self.wait_reconnect()
                    continue
                if response.status != 200:
                    raise RuntimeError(f"events HTTP {response.status}")
                if not response.getheader("Content-Type", "").startswith("text/event-stream"):
                    raise ValueError("Expected text/event-stream")
                parser = SSEParser()
                reset = False
                for chunk in self.read_stream(response):
                    for frame in parser.feed(chunk):
                        if frame.event == "reset":
                            self.state.reset()
                            self.changed()
                            reset = True
                            break
                        if frame.event == "ready":
                            self.ready(frame)
                        else:
                            self.state.apply(frame)
                            self.changed()
                    if reset:
                        break
            except Exception as exc:
                if not self.stopping.is_set():
                    with self.state.lock:
                        self.state.connection_error = None if offline(exc) else str(exc)
            finally:
                with self.state.lock:
                    self.state.connected = False
                    self.connected_ready.clear()
                with self.connection_lock:
                    self.connection = None
                    self.stream_socket = None
                if response:
                    response.close()
                if conn:
                    conn.close()
                if not self.stopping.is_set():
                    self.changed()
            self.wait_reconnect()

    def action(self, name: str, session: str | None = None) -> None:
        """Run menu actions outside the desktop event loop."""
        if name not in {"start", "exit", "kill", "kill-all"} or (name == "kill" and not session):
            raise ValueError("Choose an explicit tray action; single kill requires a session")
        if not self.action_lock.acquire(blocking=False):
            return
        if self.stopping.is_set():
            self.action_lock.release()
            return
        activity = self.starting if name == "start" else self.exiting if name == "exit" else None
        if activity is not None:
            activity.set()
            self.changed()
        def work() -> None:
            try:
                if self.stopping.is_set():
                    return
                if name == "start":
                    self.start_daemon()
                    if not self.stopping.is_set() and not self.connected_ready.is_set():
                        self.reconnect()
                        if not self.connected_ready.wait(8) and not self.stopping.is_set():
                            raise RuntimeError("daemon is running, but SSE connection is not ready")
                elif name == "exit":
                    self.exit_daemon()
                else:
                    path = "/kill" if name == "kill-all" else "/kill/" + quote(session or "", safe="")
                    status, _, _ = pythond._request("POST", path)
                    if status == 409:
                        self.state.note("busy")
                    elif not 200 <= status < 300:
                        raise RuntimeError(f"kill HTTP {status}")
            except Exception as exc:
                self.state.note(str(exc))
                print(f"pythond-tray: {exc}", file=sys.stderr)
            finally:
                if activity is not None:
                    activity.clear()
                self.action_lock.release()
                if not self.stopping.is_set():
                    self.changed()
        threading.Thread(target=work, name="pythond-tray-command", daemon=True).start()

    def autostart(self) -> None:
        """Start the daemon at launch when nothing is listening locally."""
        if os.environ.get("PYTHOND_HOST"):
            return
        try:
            pythond._request("GET", "/ls")
        except OSError as exc:
            if offline(exc):
                self.action("start")

    def start_daemon(self) -> None:
        if self.stopping.is_set():
            return
        try:
            status, _, _ = pythond._request("GET", "/ls")
            if status == 200:
                return
            raise RuntimeError(f"start probe HTTP {status}")
        except OSError as exc:
            if not offline(exc):
                raise
        if self.stopping.is_set():
            return
        if os.environ.get("PYTHOND_HOST"):
            raise RuntimeError("Start daemon is local; start the tunneled daemon on its host")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        with tempfile.NamedTemporaryFile(prefix="pythond-tray-start-", suffix=".log", delete=False) as log:
            log_path = log.name
            child = subprocess.Popen(
                [sys.executable, "-c", "from pythond import pyctl_main; pyctl_main()", "start"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
        # Keep/reap our spawn handle without making tray shutdown own the daemon.
        threading.Thread(target=child.wait, name="pythond-tray-reaper", daemon=True).start()
        deadline = time.monotonic() + 15
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                status, _, _ = pythond._request("GET", "/ls")
                if status == 200:
                    return
                if status != 401:
                    raise RuntimeError(f"start probe HTTP {status}; log: {log_path}")
            except OSError as exc:
                if not offline(exc):
                    raise
            if child.poll() is not None:
                raise RuntimeError(f"daemon start exited; log: {log_path}")
            self.stopping.wait(.2)
        if not self.stopping.is_set():
            raise RuntimeError(f"daemon readiness not confirmed; log: {log_path}")

    def exit_daemon(self) -> None:
        status, _, _ = pythond._request("POST", "/stop")
        if status == 409:
            self.state.note("busy")
            return
        if not 200 <= status < 300:
            raise RuntimeError(f"stop HTTP {status}")
        deadline = time.monotonic() + 15
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                status, _, _ = pythond._request("GET", "/ls")
                if status != 200:
                    raise RuntimeError(f"stop probe HTTP {status}")
            except (OSError, http.client.HTTPException) as exc:
                if offline(exc):
                    self.stopping.set()
                    self.quit_ui()
                    return
                # During shutdown a connection can be reset before the listener
                # is gone. Keep checking GET until actual connection refusal.
                if not isinstance(exc, (ConnectionResetError, http.client.RemoteDisconnected)):
                    raise
            self.stopping.wait(.2)
        if not self.stopping.is_set():
            raise RuntimeError("daemon has not stopped yet")


def tray_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pythond-tray", description="pythond desktop tray")
    parser.add_argument("--no-start", dest="auto_start", action="store_false",
                        help="observe only; leave an offline daemon alone at launch")
    args = parser.parse_args(argv)
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("pythond-tray requires a desktop display; use pysh/pyctl on this server.", file=sys.stderr)
        raise SystemExit(1)
    if os.name == "nt":
        import ctypes
        user32 = ctypes.windll.user32
        # Configure before pystray creates any windows; an already configured
        # embedding host keeps its own awareness if Windows refuses this change.
        try:
            awareness = user32.SetProcessDpiAwarenessContext
            awareness.argtypes = [ctypes.c_void_p]
            awareness.restype = ctypes.c_bool
            awareness(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except AttributeError:
            user32.SetProcessDPIAware()
        if not user32.GetShellWindow():
            print("pythond-tray requires an interactive Windows desktop.", file=sys.stderr)
            raise SystemExit(1)
    try:
        import pystray
        import psutil
        from PIL import Image, ImageDraw
    except ImportError:
        print('Install desktop support: pip install "pythond[tray]"', file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"pythond-tray cannot connect to the desktop: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    image_path = Path(__file__).with_name("pythond_tray.png")
    with Image.open(image_path) as source:
        base = source.convert("RGBA")
    def icon_size():
        return 32  # Other backends handle their native status-item scaling.
    def render(size, color):
        # One downsample from the original; overlays are drawn at physical size.
        image = base.resize((size, size), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        bounds = (round(size * .67), round(size * .67), size - 1, size - 1)
        fills = {"red": "#d94b4b", "gray": "#888888", "green": "#32b65c", "starting": "#30343b"}
        draw.ellipse(bounds, fill=fills[color], outline="white", width=max(1, round(size / 32)))
        if color == "starting":
            inset = max(1, round(size / 32))
            arc = (bounds[0] + inset, bounds[1] + inset, bounds[2] - inset, bounds[3] - inset)
            angle = (int(time.monotonic() * 10) % 12) * 30
            draw.arc(arc, angle, angle + 240, fill="#f5c542", width=max(1, round(size / 16)))
        return image

    state = TrayState()
    resources = Resources(psutil) if not os.environ.get("PYTHOND_HOST") else None
    client = TrayClient(state)
    item = pystray.MenuItem

    def menu_items():
        connected, sessions, recent = state.view()
        if client.exiting.is_set() or client.stopping.is_set():
            yield item("Exiting...", None, enabled=False)
            return
        if client.starting.is_set():
            yield item("Starting...", None, enabled=False)
            yield item("Quit tray", lambda icon, entry: client.quit_ui())
            return
        if not connected or state.need_snapshot:
            yield item(state.connection_label(), None, enabled=False)
            yield item("Start daemon", lambda icon, entry: client.action("start"))
            yield item("Quit tray", lambda icon, entry: client.quit_ui())
            return
        yield item(f"pythond {pythond.__version__} - {len(sessions)} sessions", None, enabled=False)
        yield pystray.Menu.SEPARATOR
        for name, session in sessions.items():
            def callback(target):
                return lambda icon, entry: client.action("kill", target)
            usage = resources.sample(session.pid) if resources else "RSS n/a  CPU n/a"
            yield item(f"{name}  pid {session.pid}  {age(session.created_at)}  {usage}",
                       pystray.Menu(item("Kill", callback(name))))
        yield pystray.Menu.SEPARATOR
        yield item("Recent:", None, enabled=False)
        for text, when in recent:
            yield item(f"{text} - {age(when)} ago", None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield item("Kill all sessions", lambda icon, entry: client.action("kill-all"))
        yield item("Exit", lambda icon, entry: client.action("exit"))

    icon_class = pystray.Icon
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        post_message = ctypes.windll.user32.PostMessageW
        post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        post_message.restype = wintypes.BOOL
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        load_image = user32.LoadImageW
        load_image.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
        load_image.restype = wintypes.HANDLE
        try:
            get_dpi = user32.GetDpiForWindow
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            get_metric = user32.GetSystemMetricsForDpi
            get_metric.argtypes = [ctypes.c_int, wintypes.UINT]
            get_metric.restype = ctypes.c_int
        except AttributeError:
            get_dpi = get_metric = None
        def icon_size():
            taskbar = user32.FindWindowW("Shell_TrayWnd", None)
            dpi = (get_dpi(taskbar) or 96) if get_dpi and taskbar else 96
            size = get_metric(49, dpi) if get_metric else user32.GetSystemMetrics(49)
            return max(16, min(256, size))
        class RefreshIcon(pystray.Icon):
            # Windows caches native menus. Refresh on the desktop thread and
            # never destroy a menu while TrackPopupMenuEx is using its handle.
            menu_open = False
            quit_requested = False
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._message_handlers[0x8001] = lambda w, l: changed()
                self._message_handlers[0x8002] = self._quit_on_desktop
                self._message_handlers[0x02E0] = self._dpi_changed  # WM_DPICHANGED
                self._message_handlers[0x007E] = self._dpi_changed  # WM_DISPLAYCHANGE
            def _dpi_changed(self, wparam, lparam):
                changed(force=True)
            def _assert_icon_handle(self):
                if self._icon_handle:
                    return
                size = self.icon.width
                # Pillow's default ICO sizes omit 20/40px. Emit the exact size
                # rather than asking Windows to stretch a default 32px icon.
                with tempfile.NamedTemporaryFile(suffix=".ico", delete=False) as output:
                    path = output.name
                    self.icon.save(output, format="ICO", sizes=[(size, size)])
                try:
                    self._icon_handle = load_image(None, path, 1, size, size, 0x10)
                    if not self._icon_handle:
                        raise ctypes.WinError()
                finally:
                    os.unlink(path)
            def request_refresh(self):
                post_message(self._hwnd, 0x8001, 0, 0)
            def request_quit(self):
                self.quit_requested = True
                post_message(self._hwnd, 0x8002, 0, 0)
            def _quit_on_desktop(self, wparam, lparam):
                # EndMenu must run on the desktop thread. Post WM_STOP only
                # after TrackPopupMenuEx unwinds, not inside its modal loop.
                if self.menu_open:
                    user32.EndMenu()
                else:
                    self.stop()
            def _on_notify(self, wparam, lparam):
                if self.quit_requested:
                    return
                if lparam != 0x0205:  # WM_RBUTTONUP
                    return super()._on_notify(wparam, lparam)
                # Native menu fonts/spacing use their owner's monitor DPI.
                point = wintypes.POINT()
                if user32.GetCursorPos(ctypes.byref(point)):
                    for window in (self._hwnd, self._menu_hwnd):
                        user32.SetWindowPos(window, None, point.x, point.y, 0, 0, 0x15)
                changed()
                self.menu_open = True
                try:
                    return super()._on_notify(wparam, lparam)
                finally:
                    self.menu_open = False
                    if self.quit_requested:
                        self.stop()
                    else:
                        self.update_menu()
        icon_class = RefreshIcon
    icon = icon_class("pythond", render(icon_size(), "red"), "pythond", pystray.Menu(menu_items))
    current_color = "red"
    def changed(force=False):
        nonlocal current_color
        connected, sessions, _ = state.view()
        color = "starting" if client.starting.is_set() else "red" if not connected else "green" if sessions else "gray"
        size = icon_size()
        if force or color == "starting" or color != current_color or icon.icon.size != (size, size):
            icon.icon = render(size, color)
        current_color = color
        title = ("pythond - Exiting..." if client.exiting.is_set() or client.stopping.is_set()
                 else "pythond - Starting..." if color == "starting"
                 else "pythond - " + state.connection_label())
        if icon.title != title:
            icon.title = title
        if not getattr(icon, "menu_open", False):
            icon.update_menu()
    client.changed = icon.request_refresh if sys.platform == "win32" else changed
    run_done = threading.Event()
    def quit_ui():
        client.stopping.set()
        if sys.platform == "win32":
            icon.request_quit()
        else:
            icon.stop()
        # A WM_QUIT can still be lost to a modal loop; re-post until run() returns.
        def retry():
            for _ in range(5):
                if run_done.wait(3):
                    return
                if sys.platform == "win32" and getattr(icon, "menu_open", False):
                    icon.request_quit()
                else:
                    icon._stop()
        threading.Thread(target=retry, name="pythond-tray-quit-retry", daemon=True).start()
    client.quit_ui = quit_ui
    def animate():
        while not client.stopping.is_set():
            client.starting.wait()
            while client.starting.is_set() and not client.stopping.is_set():
                client.changed()
                client.stopping.wait(.1)
    animation = threading.Thread(target=animate, name="pythond-tray-animation", daemon=True)
    def setup(running_icon):
        running_icon.visible = True
        animation.start()
        client.start()
        if args.auto_start:
            client.autostart()
    try:
        icon.run(setup=setup)
    except Exception as exc:
        print(f"pythond-tray desktop error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        run_done.set()
        client.stop()
        client.starting.set()  # Wake the idle animation thread so it can exit.
        if animation.ident is not None:
            animation.join(timeout=2)
        client.starting.clear()


if __name__ == "__main__":
    tray_main()
