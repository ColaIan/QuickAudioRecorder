import ctypes
import json
import os
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from typing import ClassVar

import keyboard
import psutil
from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from audio_recorder import (
    FORMAT_CONFIG,
    SAMPLE_RATE_CONFIG,
    AudioRecorder,
    describe_output_profile,
    device_cache,
    get_devices,
)
from clipboard_utils import copy_file_to_clipboard
from process_utils import (
    get_active_applications,
    get_foreground_pid,
    get_foreground_window_info,
)

CONFIG_FILE = "QuickAudioRecorder.settings.json"


def window_thumbnail(hwnd, width=48, height=32):
    """Capture a small screenshot of the given window as a QIcon.

    Mirrors how OBS captures windows: the primary method is a GDI ``BitBlt`` of
    the window's own device context (captures the real window, never the
    desktop, with no cropping or overlap). A desktop-DC crop at the window's
    exact rectangle is used only as a fallback for windows where that BitBlt
    comes back blank (e.g. GPU-composited content). Returns None when the window
    can't be captured.
    """
    if not hwnd:
        return None
    # PrintWindow with PW_RENDERFULLCONTENT is the OBS "Windows 10" capture
    # method: it asks the target to paint its full content (including GPU
    # composited clients like Chrome/Vivaldi/Discord) into our DC, so it captures
    # the real window -- never the desktop -- with no cropping or overlap, and
    # works while the window is behind others. This is prioritized over BitBlt,
    # which returns a blank bitmap for most modern windows.
    image = _printwindow_image(hwnd, width, height)
    if _is_good_image(image):
        return QIcon(QPixmap.fromImage(image))
    image = _bitblt_window_image(hwnd, width, height)
    if _is_good_image(image):
        return QIcon(QPixmap.fromImage(image))
    image = _bitblt_desktop_rect(hwnd, width, height)
    if _is_good_image(image):
        return QIcon(QPixmap.fromImage(image))
    return None


def _is_good_image(image):
    """True when ``image`` is a usable, non-blank capture."""
    return image is not None and not image.isNull() and not _is_uniform_image(image)


def _bitblt_window_image(hwnd, width, height):
    """Capture ``hwnd`` by BitBlt-ing its window device context.

    This is the OBS-proven GDI method: ``GetWindowDC`` + ``BitBlt`` of the
    window's own content into a memory bitmap, converted to a ``QImage``. It
    captures the actual window (never the desktop) and works across monitors.

    Returns None on any failure so callers fall back to the desktop-DC crop.
    """
    try:
        import win32con
        import win32gui
        import win32ui
    except Exception:
        return None
    try:
        if win32gui.IsIconic(hwnd):
            rect = win32gui.GetWindowPlacement(hwnd)[4]
        else:
            rect = win32gui.GetWindowRect(hwnd)
        w = max(1, rect[2] - rect[0])
        h = max(1, rect[3] - rect[1])
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        try:
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)
            bmp_info = bitmap.GetInfo()
            bmp_str = bitmap.GetBitmapBits(True)
        finally:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if not bmp_str:
            return None
        image = QImage(
            bmp_str,
            w,
            h,
            bmp_info["bmWidthBytes"],
            QImage.Format.Format_RGB32,
        )
        if image.isNull():
            return None
        return image.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception:
        return None


def _printwindow_image(hwnd, width, height):
    """Capture ``hwnd`` with ``PrintWindow`` (PW_RENDERFULLCONTENT).

    This is the technique OBS and Chromium use for windows where a GDI
    ``BitBlt`` hands back a blank bitmap -- GPU-composited clients (Chrome,
    Discord, Vivaldi), occluded, or minimized windows. ``PrintWindow`` asks the
    target to paint its full content (including the client area) into our memory
    DC, so it captures the real window and not the desktop, with no cropping or
    overlap, and works even when the window is behind others.

    pywin32 does not expose ``PrintWindow``, so we call ``user32.PrintWindow``
    directly via ctypes.

    Returns None on any failure.
    """
    hwnd = int(hwnd)
    try:
        import ctypes

        import win32gui
        import win32ui

        user32 = ctypes.windll.user32
        user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        user32.PrintWindow.restype = ctypes.c_int
    except Exception:
        return None
    try:
        if win32gui.IsIconic(hwnd):
            rect = win32gui.GetWindowPlacement(hwnd)[4]
        else:
            rect = win32gui.GetWindowRect(hwnd)
        w = max(1, rect[2] - rect[0])
        h = max(1, rect[3] - rect[1])
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        try:
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)
            dest = int(save_dc.GetSafeHdc())
            # PW_RENDERFULLCONTENT (2) asks the app to render its full client
            # area; fall back to the plain (0) variant if it is unsupported.
            if not user32.PrintWindow(hwnd, dest, 2) and not user32.PrintWindow(
                hwnd, dest, 0
            ):
                return None
            bmp_info = bitmap.GetInfo()
            bmp_str = bitmap.GetBitmapBits(True)
        finally:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if not bmp_str:
            return None
        image = QImage(
            bmp_str,
            w,
            h,
            bmp_info["bmWidthBytes"],
            QImage.Format.Format_RGB32,
        )
        if image.isNull():
            return None
        return image.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception:
        return None


def _is_uniform_image(image):
    """Return True when ``image`` is blank (a single solid colour).

    PrintWindow frequently hands back an all-black or all-white bitmap for
    GPU-composited windows; such a result should be discarded in favour of a
    screen grab.
    """
    if image is None or image.isNull():
        return True
    try:
        converted = image.convertToFormat(QImage.Format.Format_RGB32)
    except Exception:
        return True
    width = converted.width()
    height = converted.height()
    if width == 0 or height == 0:
        return True
    first = converted.pixelColor(0, 0)
    target = (first.red(), first.green(), first.blue())
    for y in range(height):
        for x in range(width):
            color = converted.pixelColor(x, y)
            if (color.red(), color.green(), color.blue()) != target:
                return False
    return True


def _bitblt_desktop_rect(hwnd, width, height):
    """Fallback capture: BitBlt the desktop DC at the window's exact rect.

    When a direct ``BitBlt`` of the window's own DC comes back blank (GPU
    composited windows, etc.) the desktop device context still holds the
    composited result for the region the window occupies, so sampling that
    region recovers the content. The crop is the window's real rectangle, so it
    is accurate with no overlap or guesswork.

    Returns None on any failure.
    """
    try:
        import win32con
        import win32gui
        import win32ui
    except Exception:
        return None
    try:
        if win32gui.IsIconic(hwnd):
            rect = win32gui.GetWindowPlacement(hwnd)[4]
        else:
            rect = win32gui.GetWindowRect(hwnd)
        left, top = rect[0], rect[1]
        w = max(1, rect[2] - rect[0])
        h = max(1, rect[3] - rect[1])
        desktop_dc = win32gui.GetDC(0)
        if not desktop_dc:
            return None
        try:
            mfc_dc = win32ui.CreateDCFromHandle(desktop_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (left, top), win32con.SRCCOPY)
            bmp_info = bitmap.GetInfo()
            bmp_str = bitmap.GetBitmapBits(True)
        finally:
            win32gui.ReleaseDC(0, desktop_dc)
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if not bmp_str:
            return None
        image = QImage(
            bmp_str,
            w,
            h,
            bmp_info["bmWidthBytes"],
            QImage.Format.Format_RGB32,
        )
        if image.isNull():
            return None
        return image.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception:
        return None


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


WINDOWS_MODIFIER_KEYS = {
    "alt": 0x0001,
    "ctrl": 0x0002,
    "control": 0x0002,
    "shift": 0x0004,
    "windows": 0x0008,
    "win": 0x0008,
}

WINDOWS_SPECIAL_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
    "plus": 0xBB,
    "comma": 0xBC,
    "-": 0xBD,
    "minus": 0xBD,
    ".": 0xBE,
    "period": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
}

for number in range(1, 13):
    WINDOWS_SPECIAL_KEYS[f"f{number}"] = 0x70 + number - 1


def parse_windows_hotkey(hotkey):
    parts = [part.strip().lower() for part in (hotkey or "").split("+") if part.strip()]
    if not parts:
        return None

    modifiers = 0
    keys = []
    for part in parts:
        modifier = WINDOWS_MODIFIER_KEYS.get(part)
        if modifier:
            modifiers |= modifier
        else:
            keys.append(part)

    if len(keys) != 1:
        return None

    key = keys[0]
    if len(key) == 1 and "a" <= key <= "z":
        virtual_key = ord(key.upper())
    elif len(key) == 1 and "0" <= key <= "9":
        virtual_key = ord(key)
    else:
        virtual_key = WINDOWS_SPECIAL_KEYS.get(key)

    if not virtual_key:
        return None

    return modifiers, virtual_key


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


class KeyboardHotkeyManager:
    def clear(self):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass

    def register(self, hotkey, callback):
        keyboard.add_hotkey(hotkey, callback)
        return True


class WindowsLowLevelHotkeyManager:
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    KEY_DOWN_MESSAGES: ClassVar[set] = {WM_KEYDOWN, WM_SYSKEYDOWN}
    KEY_UP_MESSAGES: ClassVar[set] = {WM_KEYUP, WM_SYSKEYUP}
    VK_TO_MODIFIER: ClassVar[dict] = {
        0x10: WINDOWS_MODIFIER_KEYS["shift"],
        0xA0: WINDOWS_MODIFIER_KEYS["shift"],
        0xA1: WINDOWS_MODIFIER_KEYS["shift"],
        0x11: WINDOWS_MODIFIER_KEYS["ctrl"],
        0xA2: WINDOWS_MODIFIER_KEYS["ctrl"],
        0xA3: WINDOWS_MODIFIER_KEYS["ctrl"],
        0x12: WINDOWS_MODIFIER_KEYS["alt"],
        0xA4: WINDOWS_MODIFIER_KEYS["alt"],
        0xA5: WINDOWS_MODIFIER_KEYS["alt"],
        0x5B: WINDOWS_MODIFIER_KEYS["windows"],
        0x5C: WINDOWS_MODIFIER_KEYS["windows"],
    }

    def __init__(self, install_hook=True, fallback=None):
        self.fallback = fallback or KeyboardHotkeyManager()
        self.callbacks = {}
        self.active_modifiers = 0
        self.active_hotkeys = set()
        self.hook = None
        self.user32 = None
        self.kernel32 = None
        self.hook_callback = None
        if sys.platform == "win32":
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.configure_api()
            self.hook_callback = LowLevelKeyboardProc(self.low_level_keyboard_proc)
            if install_hook:
                self.install_hook()

    def configure_api(self):
        assert self.user32 is not None
        self.user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            LowLevelKeyboardProc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self.user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.CallNextHookEx.restype = wintypes.LPARAM
        assert self.kernel32 is not None
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def install_hook(self):
        if self.user32 is None or self.hook:
            return bool(self.hook)

        assert self.kernel32 is not None
        self.hook = self.user32.SetWindowsHookExW(
            self.WH_KEYBOARD_LL,
            self.hook_callback,
            self.kernel32.GetModuleHandleW(None),
            0,
        )
        if not self.hook:
            print(f"Failed to install low-level hotkey hook: {ctypes.get_last_error()}")
        return bool(self.hook)

    def clear(self):
        self.callbacks.clear()
        self.active_modifiers = 0
        self.active_hotkeys.clear()
        try:
            self.fallback.clear()
        except Exception as e:
            print(f"Failed to clear fallback hotkeys: {e}")

    def register(self, hotkey, callback):
        parsed = parse_windows_hotkey(hotkey)
        if parsed is None:
            return self.fallback.register(hotkey, callback)

        if self.user32 is not None and not self.install_hook():
            return self.fallback.register(hotkey, callback)

        self.callbacks.setdefault(parsed, []).append(callback)
        return True

    def low_level_keyboard_proc(self, n_code, w_param, l_param):
        try:
            if n_code >= 0:
                event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                self.process_key_event(int(w_param), int(event.vkCode))
        except Exception as e:
            print(f"Failed to handle low-level hotkey event: {e}")
        return self.user32.CallNextHookEx(None, n_code, w_param, l_param)

    def process_key_event(self, message, virtual_key):
        if message in self.KEY_DOWN_MESSAGES:
            self.handle_key_down(virtual_key)
        elif message in self.KEY_UP_MESSAGES:
            self.handle_key_up(virtual_key)

    def handle_key_down(self, virtual_key):
        modifier = self.VK_TO_MODIFIER.get(virtual_key)
        if modifier:
            self.active_modifiers |= modifier
            return

        hotkey = (self.active_modifiers, virtual_key)
        if hotkey in self.callbacks and hotkey not in self.active_hotkeys:
            self.active_hotkeys.add(hotkey)
            for callback in list(self.callbacks[hotkey]):
                callback()

    def handle_key_up(self, virtual_key):
        modifier = self.VK_TO_MODIFIER.get(virtual_key)
        if modifier:
            self.active_modifiers &= ~modifier
            self.active_hotkeys.clear()
            return

        for hotkey in list(self.active_hotkeys):
            if hotkey[1] == virtual_key:
                self.active_hotkeys.discard(hotkey)


def create_hotkey_manager(app):
    if sys.platform == "win32":
        return WindowsLowLevelHotkeyManager()
    return KeyboardHotkeyManager()


class SignalManager(QObject):
    recording_finished = pyqtSignal(object, str)


class HotkeyEdit(QLineEdit):
    """
    Custom widget to capture hotkeys by pressing them.
    Maps Qt events to 'keyboard' library compatible strings.
    """

    CAPTURE_PROMPT = "Press shortcut..."
    sequence_captured = pyqtSignal(str)
    capture_cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Click to set hotkey...")
        self.setReadOnly(True)
        self.current_sequence = None
        self.is_capturing = False
        self._previous_text = ""
        self._keyboard_hook = None
        self._modifier_scan_codes = {
            "ctrl": set(),
            "alt": set(),
            "shift": set(),
            "windows": set(),
        }
        self._modifier_names_by_scan_code = self.build_modifier_scan_code_lookup()
        self.sequence_captured.connect(self.finish_capture)
        self.capture_cancelled.connect(self.cancel_capture)

    def begin_capture(self):
        if self.is_capturing:
            return
        self.is_capturing = True
        self._previous_text = self.text()
        self.setText(self.CAPTURE_PROMPT)
        self.selectAll()
        self.setStyleSheet("color: #666;")
        self.start_keyboard_capture()

    def finish_capture(self, sequence):
        self.stop_keyboard_capture()
        self.is_capturing = False
        self.current_sequence = sequence or None
        self.setStyleSheet("")
        self.setText(sequence)
        self.clearFocus()

    def cancel_capture(self):
        self.stop_keyboard_capture()
        self.is_capturing = False
        self.setStyleSheet("")
        self.setText(self._previous_text)
        self.clearFocus()

    def mousePressEvent(self, event):
        self.setFocus()
        self.begin_capture()
        super().mousePressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.begin_capture()

    def focusOutEvent(self, event):
        if self.is_capturing:
            self.is_capturing = False
            self.stop_keyboard_capture()
            self.setStyleSheet("")
            self.setText(self._previous_text)
        super().focusOutEvent(event)

    def start_keyboard_capture(self):
        if self._keyboard_hook is not None:
            return
        for scan_codes in self._modifier_scan_codes.values():
            scan_codes.clear()
        try:
            self._keyboard_hook = keyboard.hook(self.handle_keyboard_hook)
        except Exception as e:
            print(f"Failed to start hotkey capture hook: {e}")

    def stop_keyboard_capture(self):
        if self._keyboard_hook is None:
            return
        try:
            keyboard.unhook(self._keyboard_hook)
        except Exception as e:
            print(f"Failed to stop hotkey capture hook: {e}")
        finally:
            self._keyboard_hook = None
            for scan_codes in self._modifier_scan_codes.values():
                scan_codes.clear()

    def handle_keyboard_hook(self, event):
        if not self.is_capturing:
            return

        key_name = self.normalize_hook_key_name(event.name)
        modifier = self.modifier_name_for_hook_event(key_name, event.scan_code)
        scan_code = event.scan_code

        if modifier:
            if event.event_type == "down":
                self._modifier_scan_codes[modifier].add(scan_code)
            elif event.event_type == "up":
                self._modifier_scan_codes[modifier].discard(scan_code)
            return

        if event.event_type != "down":
            return

        if key_name in ("esc", "escape"):
            self.capture_cancelled.emit()
            return

        if key_name in ("backspace", "delete"):
            self.sequence_captured.emit("")
            return

        sequence = self.format_hook_hotkey(key_name)
        if sequence:
            self.sequence_captured.emit(sequence)

    def normalize_hook_key_name(self, key_name):
        key_name = (key_name or "").lower()
        aliases = {
            "left windows": "windows",
            "right windows": "windows",
            "win": "windows",
            "cmd": "windows",
            "+": "plus",
            ",": "comma",
            " ": "space",
            "return": "enter",
        }
        return aliases.get(key_name, key_name)

    def modifier_name_for_hook_key(self, key_name):
        aliases = {
            "ctrl": "ctrl",
            "control": "ctrl",
            "left ctrl": "ctrl",
            "right ctrl": "ctrl",
            "alt": "alt",
            "left alt": "alt",
            "right alt": "alt",
            "shift": "shift",
            "left shift": "shift",
            "right shift": "shift",
            "windows": "windows",
            "left windows": "windows",
            "right windows": "windows",
        }
        return aliases.get(key_name)

    def modifier_name_for_hook_event(self, key_name, scan_code):
        if scan_code in self._modifier_names_by_scan_code:
            return self._modifier_names_by_scan_code[scan_code]
        return self.modifier_name_for_hook_key(key_name)

    def build_modifier_scan_code_lookup(self):
        lookup = {}
        modifier_names = {
            "ctrl": ("ctrl", "control", "left ctrl", "right ctrl"),
            "alt": ("alt", "left alt", "right alt"),
            "shift": ("shift", "left shift", "right shift"),
            "windows": ("windows", "left windows", "right windows"),
        }

        for modifier, names in modifier_names.items():
            for name in names:
                try:
                    scan_codes = keyboard.key_to_scan_codes(name, False)
                except Exception:
                    scan_codes = ()
                for scan_code in scan_codes:
                    lookup[scan_code] = modifier

        return lookup

    def format_hook_hotkey(self, key_name):
        parts = []
        for modifier in ("ctrl", "alt", "shift", "windows"):
            if self._modifier_scan_codes[modifier]:
                parts.append(modifier)

        key_text = self.normalize_hook_key_name(key_name)
        if not key_text or self.modifier_name_for_hook_key(key_text):
            return ""

        parts.append(key_text)
        return "+".join(parts)

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride and self.is_capturing:
            self.handle_hotkey_event(event)
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        self.handle_hotkey_event(event)

    def handle_hotkey_event(self, event):
        key = self.key_from_event(event)
        modifiers = event.modifiers()

        if key in (Qt.Key.Key_Backspace.value, Qt.Key.Key_Delete.value):
            self.finish_capture("")
            return

        if key == Qt.Key.Key_Escape.value:
            self.cancel_capture()
            return

        if key in (
            Qt.Key.Key_Control.value,
            Qt.Key.Key_Shift.value,
            Qt.Key.Key_Alt.value,
            Qt.Key.Key_Meta.value,
        ):
            return

        final_hotkey = self.format_hotkey(key, modifiers)
        if final_hotkey:
            self.finish_capture(final_hotkey)

    def key_from_event(self, event):
        key = event.key()
        if key == Qt.Key.Key_unknown.value and event.nativeVirtualKey():
            return event.nativeVirtualKey()
        return key

    def format_hotkey(self, key, modifiers):
        parts = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("windows")

        key_text = self.key_to_text(key)
        if not key_text:
            return ""

        parts.append(key_text)

        return "+".join(parts)

    def key_to_text(self, key):
        if Qt.Key.Key_A.value <= key <= Qt.Key.Key_Z.value:
            return chr(key).lower()

        if Qt.Key.Key_0.value <= key <= Qt.Key.Key_9.value:
            return chr(key)

        key_map = {
            Qt.Key.Key_F1.value: "f1",
            Qt.Key.Key_F2.value: "f2",
            Qt.Key.Key_F3.value: "f3",
            Qt.Key.Key_F4.value: "f4",
            Qt.Key.Key_F5.value: "f5",
            Qt.Key.Key_F6.value: "f6",
            Qt.Key.Key_F7.value: "f7",
            Qt.Key.Key_F8.value: "f8",
            Qt.Key.Key_F9.value: "f9",
            Qt.Key.Key_F10.value: "f10",
            Qt.Key.Key_F11.value: "f11",
            Qt.Key.Key_F12.value: "f12",
            Qt.Key.Key_Left.value: "left",
            Qt.Key.Key_Right.value: "right",
            Qt.Key.Key_Up.value: "up",
            Qt.Key.Key_Down.value: "down",
            Qt.Key.Key_Space.value: "space",
            Qt.Key.Key_Plus.value: "plus",
            Qt.Key.Key_Comma.value: "comma",
            Qt.Key.Key_Tab.value: "tab",
            Qt.Key.Key_Return.value: "enter",
            Qt.Key.Key_Enter.value: "enter",
            Qt.Key.Key_Insert.value: "insert",
            Qt.Key.Key_Home.value: "home",
            Qt.Key.Key_End.value: "end",
            Qt.Key.Key_PageUp.value: "pageup",
            Qt.Key.Key_PageDown.value: "pagedown",
            Qt.Key.Key_CapsLock.value: "capslock",
            Qt.Key.Key_NumLock.value: "numlock",
            Qt.Key.Key_ScrollLock.value: "scrolllock",
            Qt.Key.Key_Print.value: "print_screen",
            Qt.Key.Key_Pause.value: "pause",
        }
        if key in key_map:
            return key_map[key]

        if 0x20 <= key <= 0x7E:
            return chr(key).lower()

        return ""


class TrackEditor(QWidget):
    changed = pyqtSignal()

    KIND_INPUT = "input"
    KIND_OUTPUT = "output"
    KIND_APP = "app"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.kind_combo = QComboBox()
        self.kind_combo.addItems(
            ["Input Device", "Output (System Audio)", "Application"]
        )
        self.kind_combo.currentIndexChanged.connect(self.on_kind_changed)

        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        self.app_label = QLabel("No application selected")
        self.app_btn = QPushButton("Choose App...")
        for widget in (self.input_combo, self.output_combo, self.app_label):
            widget.setMinimumWidth(80)
        self.app_btn.clicked.connect(self.open_app_picker)
        self.remove_btn = QPushButton("Remove")

        layout.addWidget(self.kind_combo)
        layout.addWidget(self.input_combo, 1)
        layout.addWidget(self.output_combo, 1)
        layout.addWidget(self.app_label, 1)
        layout.addWidget(self.app_btn)
        layout.addWidget(self.remove_btn)

        self.app_pid = None
        self.app_hwnd = None
        self.app_title = ""
        self.app_name = ""

        self.refresh_devices()
        self.refresh_hw()
        self.on_kind_changed(0)

    def refresh_devices(self):
        previous = self.input_combo.currentData()
        self.input_combo.clear()
        try:
            cache = device_cache()
            inputs = (
                cache["inputs"]
                if cache is not None
                else get_devices(include_output=False)
            )
            default_index = 0
            for i, m in enumerate(inputs):
                self.input_combo.addItem(f"{m['name']}", m["id"])
                if m.get("is_default"):
                    default_index = i
            if self.input_combo.count() == 0:
                self.input_combo.addItem("(no input devices)", None)
                return
            if previous is not None:
                idx = self.input_combo.findData(previous)
                if idx >= 0:
                    self.input_combo.setCurrentIndex(idx)
                    return
            self.input_combo.setCurrentIndex(default_index)
        except Exception as e:
            print(f"Error refreshing devices: {e}")

    def refresh_hw(self):
        previous = self.output_combo.currentData()
        self.output_combo.clear()
        try:
            cache = device_cache()
            speakers = (
                cache["speakers"]
                if cache is not None
                else get_devices(include_output=True)
            )
            default_index = 0
            for i, s in enumerate(speakers):
                self.output_combo.addItem(f"{s['name']}", s["id"])
                if s.get("is_default"):
                    default_index = i
            if self.output_combo.count() == 0:
                self.output_combo.addItem("(no output devices)", None)
                return
            if previous is not None:
                idx = self.output_combo.findData(previous)
                if idx >= 0:
                    self.output_combo.setCurrentIndex(idx)
                    return
            self.output_combo.setCurrentIndex(default_index)
        except Exception as e:
            print(f"Error refreshing hardware devices: {e}")

    def on_kind_changed(self, index):
        kind = self.current_kind()
        self.input_combo.setVisible(kind == self.KIND_INPUT)
        self.output_combo.setVisible(kind == self.KIND_OUTPUT)
        self.app_label.setVisible(kind == self.KIND_APP)
        self.app_btn.setVisible(kind == self.KIND_APP)
        if not self._loading:
            self.changed.emit()

    def current_kind(self):
        text = self.kind_combo.currentText()
        if text.startswith("Input"):
            return self.KIND_INPUT
        if text.startswith("Output"):
            return self.KIND_OUTPUT
        return self.KIND_APP

    def open_app_picker(self):
        parent = self.parent()
        recency = getattr(parent, "_app_recency", {}) or {}
        dialog = AppPickerDialog(self, recency=recency)
        if (
            dialog.exec() == QDialog.DialogCode.Accepted
            and dialog.selected_pid is not None
        ):
            self.set_app(
                dialog.selected_pid,
                dialog.selected_hwnd,
                dialog.selected_title,
                dialog.selected_name,
            )

    def set_app(self, pid, hwnd, title, name):
        self.app_pid = pid
        self.app_hwnd = hwnd
        self.app_title = title or ""
        self.app_name = name or ""
        if pid is None:
            self.app_label.setText("No application selected")
        else:
            label = title or name or f"Application (PID {pid})"
            if name and title and name != title:
                label = f"{title} ({name})"
            self.app_label.setText(label)
        if not self._loading:
            self.changed.emit()

    def get_track(self):
        kind = self.current_kind()
        track = {"kind": kind}
        if kind == self.KIND_INPUT:
            track["input_id"] = self.input_combo.currentData()
        elif kind == self.KIND_OUTPUT:
            track["speaker_id"] = self.output_combo.currentData()
        elif kind == self.KIND_APP:
            track["target_pid"] = self.app_pid
            track["target_hwnd"] = self.app_hwnd
            track["target_name"] = self.app_name or self.app_title
        return track

    def set_track(self, track):
        self._loading = True
        kind = track.get("kind", self.KIND_OUTPUT)
        label = (
            "Input Device"
            if kind == self.KIND_INPUT
            else "Output (System Audio)" if kind == self.KIND_OUTPUT else "Application"
        )
        idx = self.kind_combo.findText(label)
        if idx >= 0:
            self.kind_combo.setCurrentIndex(idx)
        if kind == self.KIND_INPUT:
            val = track.get("input_id")
            if val is not None:
                i = self.input_combo.findData(val)
                if i >= 0:
                    self.input_combo.setCurrentIndex(i)
        elif kind == self.KIND_OUTPUT:
            val = track.get("speaker_id")
            if val is not None:
                i = self.output_combo.findData(val)
                if i >= 0:
                    self.output_combo.setCurrentIndex(i)
        elif kind == self.KIND_APP:
            self.set_app(
                track.get("target_pid"),
                track.get("target_hwnd"),
                track.get("target_title") or track.get("target_name", ""),
                track.get("target_name", ""),
            )
        self._loading = False
        self.on_kind_changed(self.kind_combo.currentIndex())


class SettingsWindow(QMainWindow):
    settings_saved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings - Quick Audio Recorder")
        self.setGeometry(100, 100, 520, 640)
        self.setMinimumWidth(520)
        self.init_ui()
        self.load_settings()

    def init_ui(self):
        self._loading = True
        layout = QVBoxLayout()
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        group_tracks = QGroupBox("Capture Tracks")
        layout_tracks = QVBoxLayout()
        self.tracks_container = QWidget()
        self.tracks_layout = QVBoxLayout(self.tracks_container)
        self.tracks_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.tracks_container)
        layout_tracks.addWidget(scroll)
        self._tracks_bottom_spacer = self.tracks_layout.addStretch(1)

        btn_add = QPushButton("Add Track")
        btn_add.clicked.connect(self.add_track)
        layout_tracks.addWidget(btn_add)
        group_tracks.setLayout(layout_tracks)
        layout.addWidget(group_tracks)
        layout.setStretchFactor(group_tracks, 1)

        group_out = QGroupBox("Output Configuration")
        layout_out = QFormLayout()
        layout_folder_inner = QHBoxLayout()
        self.lbl_folder = QLabel(os.getcwd())
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self.browse_folder)
        layout_folder_inner.addWidget(self.lbl_folder)
        layout_folder_inner.addWidget(btn_browse)

        self.combo_fmt = QComboBox()
        for key, config in FORMAT_CONFIG.items():
            self.combo_fmt.addItem(config["label"], key)

        self.combo_sample_rate = QComboBox()
        for key, config in SAMPLE_RATE_CONFIG.items():
            self.combo_sample_rate.addItem(config["label"], key)

        self.combo_output_mode = QComboBox()
        self.combo_output_mode.addItem("Mixed (one file)", "mixed")
        self.combo_output_mode.addItem("Separate files", "separate_files")

        self.chk_stereo = QCheckBox("Keep Stereo")
        self.chk_stereo.setChecked(False)
        self.lbl_preview = QLabel()

        self.combo_fmt.currentIndexChanged.connect(self.update_output_preview)
        self.combo_sample_rate.currentIndexChanged.connect(self.update_output_preview)
        self.chk_stereo.toggled.connect(self.update_output_preview)

        layout_out.addRow("Folder:", layout_folder_inner)
        layout_out.addRow("Format:", self.combo_fmt)
        layout_out.addRow("Sample Rate:", self.combo_sample_rate)
        layout_out.addRow("Output:", self.combo_output_mode)
        layout_out.addRow("Stereo:", self.chk_stereo)
        layout_out.addRow("Preview:", self.lbl_preview)
        group_out.setLayout(layout_out)
        layout.addWidget(group_out)

        group_hotkeys = QGroupBox("Global Hotkey")
        layout_hotkeys = QFormLayout()
        self.hk_toggle = HotkeyEdit()
        layout_hotkeys.addRow("Toggle Recording:", self.hk_toggle)
        group_hotkeys.setLayout(layout_hotkeys)
        layout.addWidget(group_hotkeys)

        group_notifications = QGroupBox("Notifications")
        layout_notifications = QVBoxLayout()
        self.chk_notifications = QCheckBox("Show tray notifications")
        self.chk_notifications.setChecked(True)
        layout_notifications.addWidget(self.chk_notifications)
        group_notifications.setLayout(layout_notifications)
        layout.addWidget(group_notifications)

        group_post = QGroupBox("Post-Processing & Clipboard")
        layout_post = QVBoxLayout()
        self.chk_normalize = QCheckBox("Normalize Audio (Apply first)")
        self.chk_clipboard = QCheckBox("Copy File to Clipboard")
        self.chk_delete = QCheckBox("Delete after Copy (Move to Temp)")
        self.chk_delete.setToolTip(
            "Moves the file to the system temp folder before copying, keeping your output folder clean."
        )
        self.chk_delete.setEnabled(False)
        self.chk_clipboard.toggled.connect(lambda c: self.chk_delete.setEnabled(c))
        layout_post.addWidget(self.chk_normalize)
        layout_post.addWidget(self.chk_clipboard)
        layout_post.addWidget(self.chk_delete)
        group_post.setLayout(layout_post)
        layout.addWidget(group_post)

        self.combo_fmt.currentIndexChanged.connect(self._auto_save)
        self.combo_sample_rate.currentIndexChanged.connect(self._auto_save)
        self.combo_output_mode.currentIndexChanged.connect(self._auto_save)
        self.chk_stereo.toggled.connect(self._auto_save)
        self.chk_notifications.toggled.connect(self._auto_save)
        self.chk_normalize.toggled.connect(self._auto_save)
        self.chk_clipboard.toggled.connect(self._auto_save)
        self.chk_delete.toggled.connect(self._auto_save)
        self.hk_toggle.editingFinished.connect(self._auto_save)
        btn_browse.clicked.connect(self._auto_save)

        self._app_recency = {}
        self._recency_timer = QTimer(self)
        self._recency_timer.setInterval(1000)
        self._recency_timer.timeout.connect(self._track_foreground)
        self._recency_timer.start()

        self._loading = False

    def add_track(self, track=None):
        if track is None:
            track = {"kind": "output"}
        editor = TrackEditor(self)
        editor.changed.connect(self._auto_save)
        editor.remove_btn.clicked.connect(lambda: self.remove_track(editor))
        if track:
            editor.set_track(track)
        spacer_index = self.tracks_layout.indexOf(self._tracks_bottom_spacer)
        self.tracks_layout.insertWidget(spacer_index, editor)
        return editor

    def remove_track(self, editor):
        if self.tracks_layout.count() <= 1:
            return
        self.tracks_layout.removeWidget(editor)
        editor.deleteLater()
        self._auto_save()

    def _track_foreground(self):
        pid = get_foreground_pid()
        if pid:
            self._app_recency[pid] = time.time()

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", options=QFileDialog.Option.DontUseNativeDialog
        )
        if folder:
            self.lbl_folder.setText(folder)

    def update_output_preview(self):
        fmt = self.combo_fmt.currentData() or "wav"
        sample_rate = self.combo_sample_rate.currentData() or "48000"
        stereo = self.chk_stereo.isChecked()
        self.lbl_preview.setText(describe_output_profile(fmt, sample_rate, stereo))

    def get_tracks(self):
        tracks = []
        for i in range(self.tracks_layout.count()):
            widget = self.tracks_layout.itemAt(i).widget()
            if isinstance(widget, TrackEditor):
                tracks.append(widget.get_track())
        return tracks

    def load_settings(self):
        self._loading = True
        data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"Error loading settings: {e}")
        self.lbl_folder.setText(data.get("output_folder", os.getcwd()))
        self._set_combo_by_data(self.combo_fmt, data.get("format"), "wav")
        self._set_combo_by_data(
            self.combo_sample_rate, data.get("sample_rate"), "48000"
        )
        self._set_combo_by_data(
            self.combo_output_mode, data.get("output_mode"), "mixed"
        )
        self.chk_stereo.setChecked(self._parse_bool_setting(data.get("stereo")))
        self.chk_notifications.setChecked(
            self._parse_bool_setting(data.get("show_notifications", True))
        )
        self.chk_normalize.setChecked(
            self._parse_bool_setting(data.get("normalize", False))
        )
        self.chk_clipboard.setChecked(
            self._parse_bool_setting(data.get("clipboard", False))
        )
        self.chk_delete.setChecked(
            self._parse_bool_setting(data.get("delete_after", False))
        )
        self.chk_delete.setEnabled(self.chk_clipboard.isChecked())
        self.hk_toggle.setText(data.get("hk_toggle", ""))

        saved_tracks = data.get("tracks")
        if saved_tracks:
            for t in saved_tracks:
                self.add_track(t)
        else:
            self.add_track({"kind": "output"})
        self.update_output_preview()
        self._loading = False

    def save_settings(self):
        data = self.get_settings()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            QMessageBox.information(self, "Settings", "Settings saved successfully.")
            self.settings_saved.emit()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def _auto_save(self):
        if getattr(self, "_loading", False):
            return
        data = self.get_settings()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.settings_saved.emit()
        except Exception as e:
            print(f"Failed to auto-save settings: {e}")

    def get_settings(self):
        return {
            "tracks": self.get_tracks(),
            "output_folder": self.lbl_folder.text(),
            "format": self.combo_fmt.currentData() or "wav",
            "sample_rate": self.combo_sample_rate.currentData() or "48000",
            "output_mode": self.combo_output_mode.currentData() or "mixed",
            "stereo": self.chk_stereo.isChecked(),
            "show_notifications": self.chk_notifications.isChecked(),
            "normalize": self.chk_normalize.isChecked(),
            "clipboard": self.chk_clipboard.isChecked(),
            "delete_after": self.chk_delete.isChecked(),
            "hk_toggle": self.hk_toggle.text(),
        }

    def _set_combo_by_data(self, combo, value, default_value):
        normalized = str(value or default_value).strip().lower()
        default_normalized = str(default_value or "").strip().lower()
        idx = combo.findData(normalized)
        if idx < 0:
            idx = combo.findData(default_normalized)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _parse_bool_setting(self, value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "on"):
                return True
            if normalized in ("false", "0", "no", "off", ""):
                return False
        return False

    def showEvent(self, event):
        super().showEvent(event)
        self._loading = True
        for i in range(self.tracks_layout.count()):
            widget = self.tracks_layout.itemAt(i).widget()
            if isinstance(widget, TrackEditor):
                widget.refresh_devices()
                widget.refresh_hw()
        self._loading = False


# ── DWM live thumbnail helpers ───────────────────────────────────────────────
# DwmRegisterThumbnail lets the OS composite a live, zero-capture preview of a
# source window straight onto our dialog. That removes the per-second
# PrintWindow/BitBlt capture that made the picker laggy. The struct layout
# below mirrors the native DWM_THUMBNAIL_PROPERTIES (DWORD, RECT, RECT, BYTE,
# BOOL, BOOL) so ctypes packs it correctly.
try:
    _dwmapi = ctypes.windll.dwmapi
    _DWM_AVAILABLE = True
except Exception:
    _dwmapi = None
    _DWM_AVAILABLE = False

_DWM_TNP_RECTDESTINATION = 0x1
_DWM_TNP_OPACITY = 0x4
_DWM_TNP_VISIBLE = 0x8
_DWM_TNP_SOURCECLIENTAREAONLY = 0x10


class _DwmRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _DwmThumbnailProps(ctypes.Structure):
    _fields_ = [
        ("dwFlags", ctypes.c_uint32),
        ("rcDestination", _DwmRect),
        ("rcSource", _DwmRect),
        ("opacity", ctypes.c_byte),
        ("fVisible", ctypes.c_bool),
        ("fSourceClientAreaOnly", ctypes.c_bool),
    ]


class AppPickerDialog(QDialog):
    THUMBNAIL_SIZE = (240, 150)

    def __init__(self, parent=None, recency=None):
        super().__init__(parent)
        self.setWindowTitle("Select Application to Capture")
        self.setMinimumSize(360, 460)
        self.resize(1000, 560)
        self.setWindowIcon(QIcon(resource_path("icon_rec.png")))
        self._recency = recency or {}
        self._loading = True
        self.selected_pid = None
        self.selected_hwnd = None
        self.selected_title = ""
        self.selected_name = ""

        # Incremental state so refreshes never rebuild the whole list.
        self._item_map = {}  # (pid, hwnd) -> QListWidgetItem
        self._thumb_map = {}  # (pid, hwnd) -> DWM thumbnail handle
        self._preview_map = {}  # (pid, hwnd) -> preview QLabel
        self._static_times = {}  # (pid, hwnd) -> last static-capture time

        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setSpacing(10)
        self.list.setWordWrap(True)
        self.list.setIconSize(QSize(*self.THUMBNAIL_SIZE))
        self.list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list, 1)

        hint = QLabel("Click a window to capture it. Close this window to cancel.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self._populate()
        self._loading = False

        # Re-enumerate running applications once per second so newly opened or
        # closed windows surface without rebuilding the widget list.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._populate)
        self._refresh_timer.start()

        # Live DWM thumbnails are cheap to reposition, so refresh their geometry
        # frequently. Static (PrintWindow) fallbacks are throttled separately.
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._update_live_thumbnails)
        self._live_timer.start()

        self._static_timer = QTimer(self)
        self._static_timer.setInterval(1500)
        self._static_timer.timeout.connect(self._refresh_static_visible)
        self._static_timer.start()

    def _populate(self):
        try:
            apps = get_active_applications()
        except Exception:
            apps = []
        apps = self._sort_apps(apps)

        current = self.list.currentItem()
        selected_key = current.data(Qt.ItemDataRole.UserRole) if current else None

        seen = set()
        for app in apps:
            key = (app.get("pid"), app.get("hwnd"))
            seen.add(key)
            title = app.get("title", "")
            name = app.get("name", "")
            text = f"{title}\n{name}" if title else name
            item = self._item_map.get(key)
            if item is None:
                item = QListWidgetItem()
                item.setSizeHint(
                    QSize(self.THUMBNAIL_SIZE[0] + 16, self.THUMBNAIL_SIZE[1] + 44)
                )
                item.setData(Qt.ItemDataRole.UserRole, app.get("pid"))
                item.setData(Qt.ItemDataRole.UserRole + 1, app.get("hwnd"))
                self.list.addItem(item)
                widget = self._make_item_widget(key, text)
                self.list.setItemWidget(item, widget)
                self._item_map[key] = item
                self._preview_map[key] = widget.findChild(QLabel, "preview")
                if _DWM_AVAILABLE:
                    self._register_thumb(key, app.get("hwnd"))
            else:
                item.setText(text)
                widget = self.list.itemWidget(item)
                if widget is not None:
                    label = widget.findChild(QLabel, "label")
                    if label is not None:
                        label.setText(text)
            if key == selected_key:
                self.list.setCurrentItem(item)

        # Drop windows that disappeared since the last refresh.
        for key in list(self._item_map):
            if key not in seen:
                item = self._item_map.pop(key)
                self._unregister_thumb(key)
                self._preview_map.pop(key, None)
                self._static_times.pop(key, None)
                row = self.list.row(item)
                if row >= 0:
                    self.list.takeItem(row)

    def _sort_apps(self, apps):
        # The foreground window is the most recently interacted app; boost it so
        # it floats to the top. The picker itself becomes foreground when shown,
        # so ignore our own window handle.
        fg_hwnd, fg_pid = get_foreground_window_info()
        own_hwnd = int(self.winId()) if self.winId() else 0
        if fg_hwnd == own_hwnd:
            fg_pid = None
        for app in apps:
            app["_fg"] = 1 if app.get("pid") == fg_pid else 0

        groups = {}
        for app in apps:
            groups.setdefault(app.get("name", ""), []).append(app)

        # Order each executable group by the most-recently-interacted window it
        # contains (falling back to process creation time). Then interleave the
        # groups round-robin so windows belonging to the same executable are not
        # listed consecutively.
        def group_key(items):
            return (
                max(a["_fg"] for a in items),
                max(self._recency.get(a.get("pid"), 0.0) for a in items),
                max(a.get("create_time", 0.0) for a in items),
            )

        ordered = sorted(groups.values(), key=group_key, reverse=True)
        queues = [list(group) for group in ordered if group]
        result = []
        while queues:
            next_round = []
            for queue in queues:
                result.append(queue.pop(0))
                if queue:
                    next_round.append(queue)
            queues = next_round
        return result

    def _make_item_widget(self, key, text):
        container = QWidget()
        container.setFixedSize(self.THUMBNAIL_SIZE[0] + 16, self.THUMBNAIL_SIZE[1] + 44)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        preview = QLabel()
        preview.setObjectName("preview")
        preview.setFixedSize(*self.THUMBNAIL_SIZE)
        # Transparent so the OS-composited DWM live thumbnail shows through;
        # when DWM is unavailable we paint a captured pixmap here instead.
        preview.setStyleSheet("background:transparent; border:1px solid #555;")
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel(text)
        label.setObjectName("label")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(preview)
        layout.addWidget(label)

        def _press(event):
            self._select(key)

        container.mousePressEvent = _press
        preview.mousePressEvent = _press
        label.mousePressEvent = _press
        return container

    def _on_item_clicked(self, item):
        if item is None:
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        hwnd = item.data(Qt.ItemDataRole.UserRole + 1)
        self._select((pid, hwnd))

    def _select(self, key, item=None):
        if self._loading:
            return
        if item is None:
            item = self._item_map.get(key)
        if item is None:
            return
        self.list.setCurrentItem(item)
        app_pid, app_hwnd = key
        self.selected_pid = app_pid
        self.selected_hwnd = app_hwnd
        text = item.text().split("\n")
        self.selected_title = text[0] if len(text) > 1 else ""
        self.selected_name = text[-1]
        self.accept()

    # ── Live DWM thumbnails ──────────────────────────────────────────────────

    def _register_thumb(self, key, hwnd):
        if hwnd is None or not _DWM_AVAILABLE:
            return
        try:
            handle = ctypes.c_void_p()
            hr = _dwmapi.DwmRegisterThumbnail(
                ctypes.c_void_p(int(self.winId())),
                ctypes.c_void_p(int(hwnd)),
                ctypes.byref(handle),
            )
            if hr == 0 and handle.value:
                self._thumb_map[key] = handle.value
        except Exception:
            pass

    def _unregister_thumb(self, key):
        handle = self._thumb_map.pop(key, None)
        if handle is None or not _DWM_AVAILABLE:
            return
        try:
            _dwmapi.DwmUnregisterThumbnail(ctypes.c_void_p(handle))
        except Exception:
            pass

    def _update_live_thumbnails(self):
        if not _DWM_AVAILABLE:
            return
        for key, item in list(self._item_map.items()):
            preview = self._preview_map.get(key)
            if preview is None or not preview.isVisible():
                continue
            handle = self._thumb_map.get(key)
            if handle is None:
                self._register_thumb(key, key[1])
                handle = self._thumb_map.get(key)
            if handle is None:
                self._static_fallback(key, preview)
                continue
            top = preview.mapTo(self, QPoint(0, 0))
            props = _DwmThumbnailProps()
            props.dwFlags = (
                _DWM_TNP_RECTDESTINATION
                | _DWM_TNP_VISIBLE
                | _DWM_TNP_SOURCECLIENTAREAONLY
                | _DWM_TNP_OPACITY
            )
            props.rcDestination.left = top.x()
            props.rcDestination.top = top.y()
            props.rcDestination.right = top.x() + preview.width()
            props.rcDestination.bottom = top.y() + preview.height()
            props.opacity = 255
            props.fVisible = True
            props.fSourceClientAreaOnly = True
            try:
                _dwmapi.DwmUpdateThumbnailProperties(
                    ctypes.c_void_p(handle), ctypes.byref(props)
                )
            except Exception:
                self._unregister_thumb(key)
                self._static_fallback(key, preview)

    def _refresh_static_visible(self):
        for key, item in list(self._item_map.items()):
            if key in self._thumb_map:
                continue
            preview = self._preview_map.get(key)
            if preview is None or not preview.isVisible():
                continue
            self._static_fallback(key, preview)

    def _static_fallback(self, key, preview):
        now = time.time()
        last = self._static_times.get(key, 0.0)
        if now - last < 1.5:
            return
        self._static_times[key] = now
        icon = window_thumbnail(key[1], *self.THUMBNAIL_SIZE)
        if icon is not None:
            preview.setPixmap(icon.pixmap(*self.THUMBNAIL_SIZE))

    def _cleanup(self):
        self._refresh_timer.stop()
        self._live_timer.stop()
        self._static_timer.stop()
        for key in list(self._thumb_map):
            self._unregister_thumb(key)

    def accept(self):
        self._cleanup()
        super().accept()

    def reject(self):
        self._cleanup()
        super().reject()

    def closeEvent(self, event):
        self._cleanup()
        super().closeEvent(event)


class TrayApplication(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.recorder = None

        self.signals = SignalManager()
        self.signals.recording_finished.connect(self.on_recording_finished)

        self.icon_idle_path = resource_path("icon_idle.png")
        self.icon_rec_path = resource_path("icon_rec.png")
        self.generate_icons()

        self.tray_icon = QSystemTrayIcon(QIcon(self.icon_idle_path), self.app)
        self.tray_icon.setToolTip("Quick Audio Recorder (Idle)")
        self.tray_icon.activated.connect(self.on_tray_activated)

        self.build_menu()
        self.tray_icon.show()

        self.settings_window = SettingsWindow()
        self.settings_window.settings_saved.connect(self.register_hotkeys)
        self.hotkey_manager = create_hotkey_manager(self.app)

        self.register_hotkeys()

        # Open settings on startup
        self.open_settings()

    def generate_icons(self):
        if not os.path.exists(self.icon_idle_path):
            pix = QPixmap(64, 64)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QBrush(QColor(80, 80, 80)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(4, 4, 56, 56)
            painter.end()
            pix.save(self.icon_idle_path)

        if not os.path.exists(self.icon_rec_path):
            pix = QPixmap(64, 64)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QBrush(QColor(220, 0, 0)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(4, 4, 56, 56)
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.drawEllipse(22, 22, 20, 20)
            painter.end()
            pix.save(self.icon_rec_path)

    def build_menu(self):
        self.menu = QMenu()
        self.action_toggle = QAction("Start Recording", self)
        self.action_toggle.triggered.connect(self.toggle_recording)
        self.action_settings = QAction("Settings", self)
        self.action_settings.triggered.connect(self.open_settings)
        self.action_exit = QAction("Exit", self)
        self.action_exit.triggered.connect(self.exit_app)

        self.menu.addAction(self.action_toggle)
        self.menu.addSeparator()
        self.menu.addAction(self.action_settings)
        self.menu.addAction(self.action_exit)
        self.tray_icon.setContextMenu(self.menu)

    def register_hotkeys(self):
        hotkey_manager = getattr(self, "hotkey_manager", None)
        if hotkey_manager is None:
            hotkey_manager = KeyboardHotkeyManager()
            self.hotkey_manager = hotkey_manager
        try:
            hotkey_manager.clear()
        except Exception as e:
            print(f"Failed to clear hotkeys: {e}")
        settings = self.settings_window.get_settings()
        hk_toggle = settings.get("hk_toggle")
        try:
            if hk_toggle:
                hotkey_manager.register(hk_toggle, self.toggle_recording)
        except Exception as e:
            print(f"Failed to register hotkeys: {e}")

    def notifications_enabled(self):
        try:
            return self.settings_window.get_settings().get("show_notifications", True)
        except Exception:
            return True

    def show_tray_notification(
        self,
        title,
        message,
        icon=QSystemTrayIcon.MessageIcon.Information,
        duration=2000,
    ):
        notifications_enabled = getattr(
            self,
            "notifications_enabled",
            lambda: TrayApplication.notifications_enabled(self),
        )
        if notifications_enabled():
            self.tray_icon.showMessage(title, message, icon, duration)

    def toggle_recording(self):
        if self.recorder and self.recorder.is_alive():
            self.stop_recording()
        else:
            self.start_recording()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_recording()

    def open_settings(self):
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def start_recording(self):
        if self.recorder and self.recorder.is_alive():
            return
        settings = self.settings_window.get_settings()
        tracks = settings.get("tracks") or [{"kind": "output"}]
        for track in tracks:
            if track.get("kind") == "app":
                pid = track.get("target_pid")
                if not pid or not psutil.pid_exists(pid):
                    self.tray_icon.showMessage(
                        "Error",
                        "Application not found. Please refresh the list.",
                        QSystemTrayIcon.MessageIcon.Warning,
                        3000,
                    )
                    return

        def finish_callback(result, error):
            self.signals.recording_finished.emit(result, error or "")

        try:
            self.recorder = AudioRecorder(
                tracks=tracks,
                output_folder=settings["output_folder"],
                output_format=settings["format"],
                sample_rate=settings["sample_rate"],
                stereo=settings["stereo"],
                normalize=settings["normalize"],
                output_mode=settings.get("output_mode", "mixed"),
                on_finish_callback=finish_callback,
            )
        except Exception as e:
            self.tray_icon.showMessage(
                "Error",
                f"Failed to start recording: {e}",
                QSystemTrayIcon.MessageIcon.Critical,
                3000,
            )
            return
        self.recorder.start()
        self.action_toggle.setText("Stop Recording")
        self.tray_icon.setIcon(QIcon(self.icon_rec_path))
        self.tray_icon.setToolTip("Quick Audio Recorder (Recording)")
        self.show_tray_notification(
            "Started", "Recording started", QSystemTrayIcon.MessageIcon.NoIcon, 1000
        )

    def stop_recording(self):
        if self.recorder:
            self.recorder.stop()

    def on_recording_finished(self, result, error):
        self.action_toggle.setText("Start Recording")
        self.tray_icon.setIcon(QIcon(self.icon_idle_path))
        self.tray_icon.setToolTip("Quick Audio Recorder (Idle)")
        self.recorder = None

        if error:
            self.show_tray_notification(
                "Error",
                f"Recording failed: {error}",
                QSystemTrayIcon.MessageIcon.Critical,
                4000,
            )
            return

        if hasattr(result, "paths"):
            paths = list(result.paths)
        elif isinstance(result, (list, tuple)):
            paths = list(result)
        elif result:
            paths = [str(result)]
        else:
            paths = []
        paths = [path for path in paths if path and os.path.exists(path)]
        if not paths:
            self.show_tray_notification(
                "Error",
                "Recording produced no output file.",
                QSystemTrayIcon.MessageIcon.Critical,
                4000,
            )
            return

        settings = self.settings_window.get_settings()
        msg = f"Saved {len(paths)} file{'s' if len(paths) != 1 else ''}: " + ", ".join(
            os.path.basename(path) for path in paths
        )
        if settings["clipboard"]:
            if len(paths) > 1:
                msg += "\nClipboard copy skipped for separate tracks."
            else:
                try:
                    final_path = paths[0]
                    if settings["delete_after"]:
                        temp_dir = tempfile.gettempdir()
                        new_path = os.path.join(temp_dir, os.path.basename(final_path))
                        if os.path.exists(new_path):
                            base, ext = os.path.splitext(new_path)
                            new_path = f"{base}_{int(time.time())}{ext}"
                        shutil.move(final_path, new_path)
                        final_path = new_path
                        msg = "Moved to Temp & Copied to Clipboard."
                    else:
                        msg += "\nCopied to clipboard."
                    success, status = copy_file_to_clipboard(final_path)
                    if not success:
                        msg += f"\nClipboard Error: {status}"
                except Exception as e:
                    msg += f"\nClipboard/Move error: {e}"

        self.show_tray_notification(
            "Finished", msg, QSystemTrayIcon.MessageIcon.Information, 3000
        )

    def exit_app(self):
        if self.recorder:
            self.recorder.stop()
        try:
            self.hotkey_manager.clear()
        except Exception:
            pass
        self.app.quit()
