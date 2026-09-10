"""Tray consumer tests. GUI smoke is opt-in: PYTHOND_TRAY_GUI_TEST=1.
All daemon operations use private endpoints and temporary homes.
"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import pythond
import pythond_tray as tray
from test_pythond import _Daemon, wait_until

ROOT = Path(__file__).resolve().parent


class ProtocolTests(unittest.TestCase):
    def test_incremental_sse(self):
        parser = tray.SSEParser()
        data = '\ufeff: heartbeat\r\n\r\nid: epoch:1\r\nevent: cell_done\r\ndata: {"code_head":\r\ndata: "中文😀"}\r\n\r\n'
        frames = []
        for b in data.encode():
            frames.extend(parser.feed(bytes([b])))
        self.assertEqual(frames, [tray.Frame('cell_done', {'code_head': '中文😀'}, 'epoch:1')])
        self.assertRaises(ValueError, tray.SSEParser().feed, b'x' * (512 * 1024 + 1))
        self.assertRaises(ValueError, tray.SSEParser().feed, b'data: []\n\n')

    def test_state_and_incarnation(self):
        state = tray.TrayState()
        state.sessions = tray.snapshot('  old: alive pid=123\n')
        self.assertIsNone(state.sessions['old'].session_id)
        self.assertEqual(tray.age(None), 'age unknown')
        state.apply(tray.Frame('session_created', dict(session='old', session_id='new', pid=456, timestamp=100), 'e:1'))
        state.apply(tray.Frame('session_closed', dict(session='old', session_id='previous', reason='replaced'), 'e:2'))
        self.assertEqual(state.sessions['old'].session_id, 'new')
        for n in range(8):
            event = tray.Frame('cell_done', dict(session='old', sync=True, error=n == 7,
                              code_head='x' * 80, timestamp=100), f'e:{n+3}')
            state.apply(event)
            state.apply(event)
        self.assertEqual(len(state.recent), 5)
        self.assertIn('run ERR ' + 'x' * 40, state.recent[-1][0])
        self.assertEqual(tray.age(100, 220), '2m')
        state.reset()
        self.assertFalse(state.connected)
        self.assertEqual(state.sessions, {})
        self.assertIsNone(state.cursor)

    def test_resource_tree_and_pid_reuse(self):
        from types import SimpleNamespace as NS
        class Missing(Exception):
            pass
        child = mock.Mock(pid=2)
        child.create_time.return_value = 90
        child.cpu_times.return_value = NS(user=1, system=0)
        child.memory_info.return_value = NS(rss=20 * 1048576)
        root = mock.Mock(pid=1)
        root.create_time.return_value = 80
        root.children.return_value = [child]
        root.cpu_times.return_value = NS(user=1, system=0)
        root.memory_info.return_value = NS(rss=10 * 1048576)
        provider = NS(Process=lambda pid: root, NoSuchProcess=Missing, AccessDenied=PermissionError)
        resources = tray.Resources(provider)
        with mock.patch.object(tray.time, 'monotonic', return_value=100):
            self.assertEqual(resources.sample(1), 'RSS 30 MiB  CPU n/a')
        root.cpu_times.return_value = NS(user=2, system=0)
        with mock.patch.object(tray.time, 'monotonic', return_value=102):
            self.assertEqual(resources.sample(1), 'RSS 30 MiB  CPU 50%')
        root.create_time.return_value = 110
        with mock.patch.object(tray.time, 'monotonic', return_value=104):
            self.assertEqual(resources.sample(1), 'RSS 30 MiB  CPU n/a')
        child.memory_info.side_effect = PermissionError()
        self.assertIn('RSS 10 MiB+', resources.sample(1))

    def test_start_state_duplicate_click_and_failure(self):
        client = tray.TrayClient()
        self.addCleanup(client.stop)
        entered, release = threading.Event(), threading.Event()
        changes = []
        client.changed = lambda: changes.append(client.starting.is_set())
        def slow_start():
            entered.set()
            release.wait(3)
            client.connected_ready.set()
        with mock.patch.object(client, 'start_daemon', side_effect=slow_start) as start:
            client.action('start')
            self.assertTrue(client.starting.is_set())
            self.assertTrue(entered.wait(1))
            client.action('start')
            self.assertEqual(start.call_count, 1)
            release.set()
            self.assertTrue(wait_until(lambda: not client.action_lock.locked()))
        self.assertFalse(client.starting.is_set())
        self.assertEqual(changes, [True, False])
        with mock.patch.object(client, 'start_daemon', side_effect=RuntimeError('launch failed')), \
             contextlib.redirect_stderr(io.StringIO()):
            client.action('start')
            self.assertTrue(wait_until(lambda: not client.action_lock.locked()))
        self.assertFalse(client.starting.is_set())
        self.assertIn('launch failed', client.state.recent[-1][0])

    def test_auth_failure_is_unknown_not_empty_and_clears_on_ready(self):
        client = tray.TrayClient()
        self.addCleanup(client.stop)
        self.assertIn('unknown', client.state.connection_label())
        connection = mock.Mock()
        response = mock.Mock(status=401)
        connection.getresponse.return_value = response
        client.changed = lambda: client.stopping.set()
        with mock.patch.object(pythond, '_connect', return_value=(connection, 'fake-test-token')):
            client.listen()
        self.assertFalse(client.state.connected)
        self.assertEqual(client.state.connection_label(), 'Authentication failed - sessions unknown')
        self.assertEqual(list(client.state.recent), [])
        with mock.patch.object(pythond, '_request', return_value=(200, {}, '(no sessions)')):
            client.ready(tray.Frame('ready', {}, 'epoch:0'))
        self.assertIsNone(client.state.connection_error)
        self.assertEqual(client.state.connection_label(), '0 sessions')

    def test_start_online_never_spawns(self):
        client = tray.TrayClient()
        self.addCleanup(client.stop)
        with mock.patch.object(pythond, '_request', return_value=(200, {}, '(no sessions)')), \
             mock.patch.object(tray.subprocess, 'Popen') as spawn:
            client.start_daemon()
            client.start_daemon()
            spawn.assert_not_called()

    def test_reconnect_wakes_backoff_immediately(self):
        client = tray.TrayClient(reconnect_delay=30)
        self.addCleanup(client.stop)
        waiter = threading.Thread(target=client.wait_reconnect, daemon=True)
        waiter.start()
        client.reconnect()
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())

    def test_busy_not_retried(self):
        client = tray.TrayClient()
        self.addCleanup(client.stop)
        with mock.patch.object(pythond, '_request', return_value=(409, {}, 'ERR busy')) as req:
            self.assertRaises(ValueError, client.action, 'kill')
            self.assertRaises(ValueError, client.action, 'unknown')
            req.assert_not_called()
            client.action('kill-all')
            self.assertTrue(wait_until(lambda: not client.action_lock.locked()))
            req.assert_called_once_with('POST', '/kill')
        self.assertEqual(client.state.recent[-1][0], 'busy')

    def test_headless_and_no_optional_imports(self):
        # -S hides site-packages, including pystray/Pillow, in a fresh interpreter.
        script = '''
import os,sys
sys.path.insert(0, sys.argv[1])
import pythond, pythond_tray
assert all(name not in sys.modules for name in ('pystray', 'PIL', 'psutil'))
sys.platform = 'linux'
os.environ.pop('DISPLAY', None)
os.environ.pop('WAYLAND_DISPLAY', None)
pythond_tray.tray_main()
'''
        result = subprocess.run([sys.executable, '-S', '-c', script, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires a desktop display', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        missing = script[:script.index("sys.platform = 'linux'")] + "sys.platform = 'win32'\nif os.name == 'nt':\n import ctypes\n ctypes.windll.user32.GetShellWindow = lambda: 1\npythond_tray.tray_main()\n"
        result = subprocess.run([sys.executable, '-S', '-c', missing, str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('pip install "pythond[tray]"', result.stderr)

    def test_quit_observer_does_not_stop_daemon(self):
        with mock.patch.object(pythond, '_request') as request:
            tray.TrayClient().stop()
            request.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def test_live_events_bootstrap_reconnect_reset_and_exit(self):
        with tempfile.TemporaryDirectory() as td, _Daemon(td) as daemon:
            self.addCleanup(daemon.proc.stderr.close)
            pythond._request('POST', '/new/existing')
            state = tray.TrayState()
            exited = threading.Event()
            client = tray.TrayClient(state, quit_ui=exited.set, reconnect_delay=.05)
            client.start()
            try:
                self.assertTrue(wait_until(lambda: state.connected))
                self.assertIn('existing', state.sessions)
                self.assertIsNone(state.sessions['existing'].created_at)
                self.assertEqual(daemon.stderr().count('GET /ls '), 1)
                _, headers, _ = pythond._request('POST', '/new/work')
                self.assertTrue(wait_until(lambda: 'work' in state.sessions))
                self.assertEqual(state.sessions['work'].session_id, headers['X-Pythond-Session-Id'])
                pythond._request('POST', '/run/work', '1+1')
                self.assertTrue(wait_until(lambda: any('work run OK 1+1' in r[0] for r in state.recent)))
                client.action('kill-all')
                self.assertTrue(wait_until(lambda: not state.sessions))
                self.assertTrue(state.connected)
                time.sleep(.2)
                self.assertEqual(daemon.stderr().count('GET /ls '), 1)
                self.assertNotIn('/poll/', daemon.stderr())
                # Force a stale epoch on this same endpoint; next connection resets.
                with state.lock:
                    state.cursor = 'f' * 32 + ':0'
                client.reconnect()
                self.assertTrue(wait_until(lambda: daemon.stderr().count('GET /ls ') == 2 and state.connected),
                                (state.view(), state.cursor, daemon.stderr()))
                self.assertEqual(state.sessions, {})
                client.action('exit')
                self.assertTrue(exited.wait(8))
                self.assertTrue(wait_until(lambda: daemon.proc.poll() is not None))
            finally:
                client.stop()

    def test_daemon_restart_refreshes_token_and_snapshot(self):
        with tempfile.TemporaryDirectory() as td, _Daemon(td) as first:
            self.addCleanup(first.proc.stderr.close)
            client = tray.TrayClient(reconnect_delay=.05)
            client.start()
            second = None
            try:
                self.assertTrue(wait_until(lambda: client.state.connected))
                old_epoch = client.state.cursor.split(':')[0]
                old_token = pythond._read_meta().get('token')
                pythond._request('POST', '/new/old')
                self.assertTrue(wait_until(lambda: 'old' in client.state.sessions))
                pythond._request('POST', '/stop')
                first.proc.wait(timeout=8)
                self.assertTrue(wait_until(lambda: not client.state.connected))
                second = subprocess.Popen([sys.executable, str(ROOT / 'pythond.py'), 'daemon'],
                                          env=first.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.assertTrue(wait_until(lambda: client.state.connected and
                    client.state.cursor.split(':')[0] != old_epoch))
                self.assertEqual(client.state.sessions, {})
                if old_token:
                    self.assertNotEqual(old_token, pythond._read_meta().get('token'))
            finally:
                client.stop()
                if second:
                    with contextlib.suppress(OSError):
                        pythond._request('POST', '/stop')
                    try:
                        second.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        second.terminate()
                        second.wait(timeout=3)

    def test_ready_without_gap_does_not_query_again(self):
        client = tray.TrayClient()
        self.addCleanup(client.stop)
        with mock.patch.object(pythond, '_request', return_value=(200, {}, '(no sessions)')) as req:
            client.ready(tray.Frame('ready', {}, 'epoch:1'))
            client.ready(tray.Frame('ready', {}, 'epoch:1'))
            self.assertEqual(req.call_count, 1)
            client.state.reset()
            client.ready(tray.Frame('ready', {}, 'new:0'))
            self.assertEqual(req.call_count, 2)


@unittest.skipUnless(os.environ.get('PYTHOND_TRAY_GUI_TEST') == '1' and sys.platform == 'win32',
                     'Windows desktop smoke is opt-in')
class DesktopTests(unittest.TestCase):
    def test_real_icon_and_menu_actions(self):
        import pystray
        original_run = pystray.Icon.run
        original_start = tray.TrayClient.start_daemon
        release_start = threading.Event()
        def slow_start(client):
            if not release_start.wait(5):
                raise RuntimeError('test startup gate timed out')
            if not client.stopping.is_set():
                original_start(client)
        failures = []
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            endpoint = _Daemon(td)  # Configure a private endpoint without starting it.
            for patch in endpoint.patches:
                stack.enter_context(patch)
            def run(icon, setup=None):
                def drive():
                    try:
                        self.assertTrue(wait_until(lambda: icon.visible))
                        def labels():
                            return [entry.text for entry in icon.menu.items]
                        def click(label):
                            next(entry for entry in icon.menu.items if entry.text == label)(icon)
                        def dot():
                            xy = int(icon.icon.width * 53 / 64)
                            return icon.icon.getpixel((xy, xy))[:3]
                        self.assertEqual(labels(), ['Offline - sessions unknown', 'Start daemon', 'Quit tray'])
                        self.assertEqual(dot(), (217, 75, 75))
                        import ctypes
                        user32 = ctypes.windll.user32
                        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
                        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                        self.assertEqual(user32.GetAwarenessFromDpiAwarenessContext(
                            user32.GetThreadDpiAwarenessContext()), 2)
                        user32.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                        user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
                        for window in (icon._hwnd, icon._menu_hwnd):
                            self.assertEqual(user32.GetAwarenessFromDpiAwarenessContext(
                                user32.GetWindowDpiAwarenessContext(window)), 2)
                        taskbar = user32.FindWindowW('Shell_TrayWnd', None)
                        expected_size = user32.GetSystemMetricsForDpi(49, user32.GetDpiForWindow(taskbar))
                        self.assertEqual(icon.icon.size, (expected_size, expected_size))
                        # Verify the actual HICON bitmap, not merely the PIL image.
                        from ctypes import wintypes
                        class IconInfo(ctypes.Structure):
                            _fields_ = [('is_icon', wintypes.BOOL), ('x', wintypes.DWORD),
                                        ('y', wintypes.DWORD), ('mask', wintypes.HANDLE), ('color', wintypes.HANDLE)]
                        class Bitmap(ctypes.Structure):
                            _fields_ = [('kind', wintypes.LONG), ('width', wintypes.LONG),
                                        ('height', wintypes.LONG), ('stride', wintypes.LONG),
                                        ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('data', ctypes.c_void_p)]
                        user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(IconInfo)]
                        info = IconInfo()
                        self.assertTrue(user32.GetIconInfo(icon._icon_handle, ctypes.byref(info)))
                        gdi = ctypes.windll.gdi32
                        gdi.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
                        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
                        try:
                            bitmap = Bitmap()
                            self.assertTrue(gdi.GetObjectW(info.color, ctypes.sizeof(bitmap), ctypes.byref(bitmap)))
                            self.assertEqual((bitmap.width, bitmap.height), (expected_size, expected_size))
                        finally:
                            gdi.DeleteObject(info.mask)
                            gdi.DeleteObject(info.color)
                        click('Start daemon')
                        self.assertEqual(labels(), ['Starting...', 'Quit tray'])
                        self.assertFalse(next(iter(icon.menu.items)).enabled)
                        self.assertTrue(wait_until(lambda: 'Starting' in icon.title))
                        first_frame = icon.icon.tobytes()
                        self.assertTrue(wait_until(lambda: icon.icon.tobytes() != first_frame, timeout=2))
                        release_start.set()
                        self.assertTrue(wait_until(lambda: any('0 sessions' in s for s in labels()), timeout=20))
                        self.assertTrue(wait_until(lambda: dot() == (136, 136, 136)))
                        pythond._request('POST', '/new/work')
                        self.assertTrue(wait_until(lambda: any('work  pid' in s for s in labels())))
                        self.assertTrue(wait_until(lambda: dot() == (50, 182, 92)))
                        status, _, console = pythond._request('POST', '/run/work',
                            "import ctypes; ctypes.windll.kernel32.GetConsoleWindow()")
                        self.assertEqual((status, console), (200, '0'))
                        pythond._request('POST', '/run/work', '1+1')
                        self.assertTrue(wait_until(lambda: any('work run OK 1+1' in s for s in labels())))
                        pythond._request('POST', '/new/other')
                        self.assertTrue(wait_until(lambda: any('other  pid' in s for s in labels())))
                        session_menu = next(entry for entry in icon.menu.items if entry.text.startswith('other  pid'))
                        next(iter(session_menu.submenu.items))(icon)
                        self.assertTrue(wait_until(lambda: not any('other  pid' in s for s in labels())))
                        self.assertTrue(any('RSS ' in s and 'CPU ' in s for s in labels()))
                        click('Kill all sessions')
                        self.assertTrue(wait_until(lambda: any('0 sessions' in s for s in labels())))
                        self.assertTrue(wait_until(lambda: dot() == (136, 136, 136)))
                        click('Exit')
                    except BaseException as exc:
                        failures.append(exc)
                        icon.stop()
                def timed_out():
                    failures.append(AssertionError('desktop smoke did not exit in 40 seconds'))
                    icon.stop()
                watchdog = threading.Timer(40, timed_out)
                watchdog.daemon = True
                watchdog.start()
                threading.Thread(target=drive, daemon=True).start()
                try:
                    return original_run(icon, setup)
                finally:
                    watchdog.cancel()
            with mock.patch.object(pystray.Icon, 'run', run), \
                 mock.patch.object(tray.TrayClient, 'start_daemon', slow_start):
                tray.tray_main()
            # Cleanup only this test's private daemon if a preceding assertion failed.
            try:
                pythond._request('POST', '/stop')
            except OSError:
                pass
            def stopped():
                try:
                    pythond._request('GET', '/ls')
                    return False
                except OSError as exc:
                    return tray.offline(exc)
            self.assertTrue(wait_until(stopped))
        if failures:
            raise failures[0]


if __name__ == '__main__':
    unittest.main(verbosity=2)
