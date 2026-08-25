import pytest
from unittest.mock import patch, MagicMock

import process_utils

@patch('psutil.process_iter')
@patch('process_utils.win32gui')
def test_get_active_applications(mock_win32gui, mock_process_iter):
    # Mock some processes
    p1 = MagicMock()
    p1.info = {'pid': 1000, 'name': 'explorer.exe'}
    
    p2 = MagicMock()
    p2.info = {'pid': 2000, 'name': 'svchost.exe'}
    
    p3 = MagicMock()
    p3.info = {'pid': 3000, 'name': 'Discord.exe'}

    mock_process_iter.return_value = [p1, p2, p3]

    # Mock window visibility checks
    # Let's say win32gui.IsWindowVisible returns True for explorer and Discord, False for svchost
    # And we also mock EnumWindows to return the HWNDs, which map to PIDs via GetWindowThreadProcessId
    
    def mock_enum_windows(callback, extra):
        # Pass HWNDs to callback
        callback(1, None) # Explorer
        callback(2, None) # Discord
        
    mock_win32gui.EnumWindows = mock_enum_windows
    mock_win32gui.IsWindowVisible.side_effect = lambda hwnd: True
    mock_win32gui.GetWindowText.side_effect = lambda hwnd: "Window" if hwnd == 1 else "Discord - General"

    # We need to mock win32process as well if process_utils uses it
    with patch('process_utils.win32process') as mock_win32process:
        # HWND 1 -> PID 1000, HWND 2 -> PID 3000
        def mock_get_thread_process_id(hwnd):
            if hwnd == 1:
                return (0, 1000)
            elif hwnd == 2:
                return (0, 3000)
            return (0, 0)
        
        mock_win32process.GetWindowThreadProcessId = mock_get_thread_process_id

        with patch('process_utils.psutil.Process') as mock_psutil_process:
            def mock_process(pid):
                p = MagicMock()
                if pid == 1000:
                    p.name.return_value = 'explorer.exe'
                elif pid == 3000:
                    p.name.return_value = 'Discord.exe'
                else:
                    raise psutil.NoSuchProcess(pid)
                return p
            mock_psutil_process.side_effect = mock_process

            apps = process_utils.get_active_applications()
    
            assert len(apps) == 2
        pids = [app['pid'] for app in apps]
        assert 1000 in pids
        assert 3000 in pids
        assert 2000 not in pids # svchost has no visible window
        
        discord_app = next(app for app in apps if app['pid'] == 3000)
        assert discord_app['name'] == 'Discord.exe'
        assert discord_app['title'] == 'Discord - General'
