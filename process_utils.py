import os

import psutil

try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None


def get_root_pid(pid):
    """
    Traverses the process tree upwards to find the highest-level parent process
    with the same executable name. This is critical for browsers (Firefox/Chrome)
    to ensure the WASAPI output captures the entire process tree including
    sandboxed audio utility processes.
    """
    try:
        p = psutil.Process(pid)
        target_name = p.name().lower()
        while True:
            parent = p.parent()
            if parent is None:
                break
            if parent.name().lower() != target_name:
                break
            p = parent
        return p.pid
    except Exception:
        return pid


def get_active_applications():
    """
    Returns a list of dictionaries with 'pid', 'name', and 'title' of all
    currently running user-facing applications.
    """
    if win32gui is None:
        # Fallback if pywin32 is not installed, though we lose window title / visibility checks
        apps = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if p.info["name"] not in (
                    "svchost.exe",
                    "System Idle Process",
                    "System",
                ):
                    apps.append(
                        {
                            "pid": p.info["pid"],
                            "name": p.info["name"],
                            "title": p.info["name"],
                            "hwnd": None,
                        }
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return apps

    # Using pywin32 for accurate visible window detection
    assert win32gui is not None
    win32_api = win32gui
    visible_hwnds = []

    def enum_windows_callback(hwnd, extra):
        try:
            if not win32_api.IsWindowVisible(hwnd):
                return True
            # Windows parks invisible/hidden top-level windows at the off-screen
            # magic coordinate (-32000, -32000). They report as "visible" yet can
            # never be captured, so skip them to avoid blank picker thumbnails.
            try:
                rl, rt, _rr, _rb = win32_api.GetWindowRect(hwnd)
            except Exception:
                return True
            if rl <= -32000 or rt <= -32000:
                return True
            title = win32_api.GetWindowText(hwnd)
            if not title:
                # Frameless apps (e.g. some browsers such as Vivaldi) keep the
                # visible top-level window untitled and place the title on a
                # child control. Recover it from there so the window isn't
                # silently dropped.
                child = win32_api.GetWindow(hwnd, 5)  # GW_CHILD
                while child:
                    child_title = win32_api.GetWindowText(child)
                    if child_title:
                        title = child_title
                        break
                    child = win32_api.GetWindow(child, 2)  # GW_HWNDNEXT
            # Keep the window even if no title could be recovered; the process
            # name is substituted as the display title further down so the window
            # still appears in the picker (e.g. Vivaldi's main window).
            visible_hwnds.append((hwnd, title))
        except Exception:
            pass
        return True

    win32_api.EnumWindows(enum_windows_callback, None)

    # List every visible top-level window (like Alt+Tab does) rather than
    # collapsing to one entry per process -- a single process such as a browser
    # or explorer can own many windows the user wants to pick between.
    apps = []
    own_pid = os.getpid()
    seen = set()
    for hwnd, title in visible_hwnds:
        assert win32process is not None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        # Never offer the recorder's own process as a capture target.
        if pid == own_pid:
            continue
        if hwnd in seen:
            continue
        seen.add(hwnd)
        try:
            p = psutil.Process(pid)
            name = p.name()
            try:
                create_time = p.create_time()
            except Exception:
                create_time = 0.0
            apps.append(
                {
                    "pid": pid,
                    "name": name,
                    "title": title or name,
                    "hwnd": hwnd,
                    "create_time": create_time,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return apps


def get_foreground_pid():
    """Return the PID of the currently foreground (focused) window, or None.

    Used to rank capture candidates by how recently the user interacted with
    them -- the foreground window is, by definition, the most recently used.
    """
    if win32gui is None or win32process is None:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return None


def get_foreground_window_info():
    """Return (hwnd, pid) of the foreground window using user32 directly.

    ctypes is preferred here over the win32 wrappers because it reads the live
    foreground state at call time, which makes window ordering ("most recently
    interacted with") accurate even between the recency timer's ticks. Returns
    (0, None) when nothing is foregrounded or the call fails.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return (0, None)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return (hwnd, pid.value)
    except Exception:
        return (0, None)
