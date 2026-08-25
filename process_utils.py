import psutil

def get_root_pid(pid):
    """
    Traverses the process tree upwards to find the highest-level parent process
    with the same executable name. This is critical for browsers (Firefox/Chrome)
    to ensure the WASAPI loopback captures the entire process tree including
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
    try:
        import win32gui
        import win32process
    except ImportError:
        win32gui = None
        win32process = None

    if win32gui is None:
        # Fallback if pywin32 is not installed, though we lose window title / visibility checks
        apps = []
        for p in psutil.process_iter(['pid', 'name']):
            try:
                if p.info['name'] not in ('svchost.exe', 'System Idle Process', 'System'):
                    apps.append({
                        'pid': p.info['pid'],
                        'name': p.info['name'],
                        'title': p.info['name']
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return apps

    # Using pywin32 for accurate visible window detection
    visible_hwnds = []
    
    def enum_windows_callback(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            visible_hwnds.append(hwnd)

    win32gui.EnumWindows(enum_windows_callback, None)

    # Map HWND to PID
    pid_to_hwnd = {}
    for hwnd in visible_hwnds:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        # Store the first window title found for this PID
        if pid not in pid_to_hwnd:
            pid_to_hwnd[pid] = hwnd

    apps = []
    for pid, hwnd in pid_to_hwnd.items():
        try:
            p = psutil.Process(pid)
            name = p.name()
            title = win32gui.GetWindowText(hwnd)
            apps.append({
                'pid': pid,
                'name': name,
                'title': title
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return apps
