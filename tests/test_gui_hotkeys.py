import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from gui import (
    HotkeyEdit,
    SettingsWindow,
    TrayApplication,
    WindowsLowLevelHotkeyManager,
    parse_windows_hotkey,
)


class FakeRecorder:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


class FakeSettingsWindow:
    def __init__(self, settings):
        self.settings = settings

    def get_settings(self):
        return dict(self.settings)


class FakeTrayIcon:
    def __init__(self):
        self.messages = []

    def showMessage(self, title, message, icon, duration):
        self.messages.append((title, message, icon, duration))


class FakeHotkeyManager:
    def __init__(self):
        self.cleared = False
        self.registrations = []

    def clear(self):
        self.cleared = True

    def register(self, hotkey, callback):
        self.registrations.append((hotkey, callback))


class HotkeyEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_alt_shift_letter_hotkey_is_captured(self):
        edit = HotkeyEdit()
        edit.begin_capture()

        event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_R.value,
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        edit.keyPressEvent(event)

        self.assertEqual(edit.text(), "alt+shift+r")

    def test_shortcut_override_alt_shift_letter_is_captured(self):
        edit = HotkeyEdit()
        edit.begin_capture()

        event = QKeyEvent(
            QEvent.Type.ShortcutOverride,
            Qt.Key.Key_R.value,
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        QApplication.sendEvent(edit, event)

        self.assertEqual(edit.text(), "alt+shift+r")
        self.assertTrue(event.isAccepted())

    def test_native_virtual_key_fallback_captures_letter(self):
        edit = HotkeyEdit()
        edit.begin_capture()

        event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_unknown.value,
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier,
            0,
            ord("R"),
            0,
        )
        edit.keyPressEvent(event)

        self.assertEqual(edit.text(), "alt+shift+r")

    def test_keyboard_hook_captures_alt_shift_letter(self):
        for alt_name in ("alt", "left alt", "right alt"):
            with self.subTest(alt_name=alt_name):
                edit = HotkeyEdit()
                edit.is_capturing = True

                edit.handle_keyboard_hook(
                    SimpleNamespace(event_type="down", name=alt_name, scan_code=56)
                )
                edit.handle_keyboard_hook(
                    SimpleNamespace(event_type="down", name="shift", scan_code=42)
                )
                edit.handle_keyboard_hook(
                    SimpleNamespace(event_type="down", name="r", scan_code=19)
                )

                self.assertEqual(edit.text(), "alt+shift+r")

    def test_keyboard_hook_captures_physical_alt_when_ctrl_alt_are_swapped(self):
        cases = (
            ("ctrl", 56, "alt+shift+r"),
            ("alt", 29, "ctrl+shift+r"),
        )

        for mapped_name, scan_code, expected in cases:
            with self.subTest(mapped_name=mapped_name, scan_code=scan_code):
                edit = HotkeyEdit()
                edit.is_capturing = True

                edit.handle_keyboard_hook(
                    SimpleNamespace(
                        event_type="down", name=mapped_name, scan_code=scan_code
                    )
                )
                edit.handle_keyboard_hook(
                    SimpleNamespace(event_type="down", name="shift", scan_code=42)
                )
                edit.handle_keyboard_hook(
                    SimpleNamespace(event_type="down", name="r", scan_code=19)
                )

                self.assertEqual(edit.text(), expected)

    def test_capture_prompt_is_visible_and_restores_on_escape(self):
        edit = HotkeyEdit()
        edit.setText("ctrl+alt+r")

        edit.begin_capture()
        self.assertEqual(edit.text(), "Press shortcut...")

        event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Escape.value,
            Qt.KeyboardModifier.NoModifier,
        )
        edit.keyPressEvent(event)

        self.assertEqual(edit.text(), "ctrl+alt+r")

    def test_delete_clears_hotkey(self):
        edit = HotkeyEdit()
        edit.setText("ctrl+alt+r")
        edit.begin_capture()

        event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Delete.value,
            Qt.KeyboardModifier.NoModifier,
        )
        edit.keyPressEvent(event)

        self.assertEqual(edit.text(), "")

    def test_punctuation_hotkeys_are_captured(self):
        cases = (
            (Qt.Key.Key_Minus.value, "ctrl+alt+-"),
            (Qt.Key.Key_Slash.value, "ctrl+alt+/"),
        )

        for key, expected in cases:
            with self.subTest(expected=expected):
                edit = HotkeyEdit()
                edit.begin_capture()

                event = QKeyEvent(
                    QEvent.Type.KeyPress,
                    key,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
                )
                edit.keyPressEvent(event)

                self.assertEqual(edit.text(), expected)

    def test_hotkey_separator_punctuation_uses_keyboard_names(self):
        cases = (
            (Qt.Key.Key_Plus.value, "ctrl+alt+plus"),
            (Qt.Key.Key_Comma.value, "ctrl+alt+comma"),
        )

        for key, expected in cases:
            with self.subTest(expected=expected):
                edit = HotkeyEdit()
                edit.begin_capture()

                event = QKeyEvent(
                    QEvent.Type.KeyPress,
                    key,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
                )
                edit.keyPressEvent(event)

                self.assertEqual(edit.text(), expected)

    def test_modifier_only_unknown_key_is_ignored(self):
        edit = HotkeyEdit()

        self.assertEqual(
            edit.format_hotkey(
                0,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
            ),
            "",
        )


class WindowsHotkeyParserTests(unittest.TestCase):
    def test_parse_alt_shift_letter_for_register_hotkey(self):
        self.assertEqual(parse_windows_hotkey("alt+shift+r"), (0x0001 | 0x0004, 0x52))

    def test_parse_common_keys_for_register_hotkey(self):
        cases = {
            "ctrl+alt+plus": (0x0002 | 0x0001, 0xBB),
            "ctrl+alt+comma": (0x0002 | 0x0001, 0xBC),
            "ctrl+alt+-": (0x0002 | 0x0001, 0xBD),
            "ctrl+alt+/": (0x0002 | 0x0001, 0xBF),
            "windows+shift+f12": (0x0008 | 0x0004, 0x7B),
        }

        for hotkey, expected in cases.items():
            with self.subTest(hotkey=hotkey):
                self.assertEqual(parse_windows_hotkey(hotkey), expected)

    def test_parse_unknown_key_returns_none(self):
        self.assertIsNone(parse_windows_hotkey("alt+shift+unknown-key"))


class WindowsLowLevelHotkeyManagerTests(unittest.TestCase):
    def test_alt_shift_letter_triggers_from_low_level_events(self):
        manager = WindowsLowLevelHotkeyManager(install_hook=False)
        calls = []
        manager.register("alt+shift+r", lambda: calls.append("input"))

        manager.process_key_event(0x0104, 0xA4)
        manager.process_key_event(0x0100, 0xA0)
        manager.process_key_event(0x0100, 0x52)

        self.assertEqual(calls, ["input"])

    def test_repeated_keydown_does_not_repeat_until_keyup(self):
        manager = WindowsLowLevelHotkeyManager(install_hook=False)
        calls = []
        manager.register("alt+shift+r", lambda: calls.append("input"))

        manager.process_key_event(0x0104, 0xA4)
        manager.process_key_event(0x0100, 0xA0)
        manager.process_key_event(0x0100, 0x52)
        manager.process_key_event(0x0100, 0x52)
        manager.process_key_event(0x0101, 0x52)
        manager.process_key_event(0x0100, 0x52)

        self.assertEqual(calls, ["input", "input"])

    def test_clear_removes_low_level_registrations(self):
        manager = WindowsLowLevelHotkeyManager(install_hook=False)
        calls = []
        manager.register("alt+shift+r", lambda: calls.append("input"))

        manager.clear()
        manager.process_key_event(0x0104, 0xA4)
        manager.process_key_event(0x0100, 0xA0)
        manager.process_key_event(0x0100, 0x52)

        self.assertEqual(calls, [])


class SettingsWindowHotkeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_settings_window(self, settings):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        previous_cwd = os.getcwd()
        os.chdir(temp_dir.name)
        self.addCleanup(os.chdir, previous_cwd)

        with open("settings.json", "w", encoding="utf-8") as f:
            json.dump(settings, f)

        patches = [
            patch("gui.device_cache", return_value={
                "inputs": [{"name": "Default Input", "id": "input1", "is_default": True}],
                "speakers": [{"name": "Default Output", "id": "speaker-id", "is_default": True}],
            }),
            patch("gui.get_devices", return_value=[{"name": "Default Input", "id": "input1", "is_default": True}]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        window = SettingsWindow()
        self.addCleanup(window.close)
        return window

    def test_toggle_hotkey_loaded_from_settings(self):
        window = self.make_settings_window({"hk_toggle": "ctrl+alt+t"})
        self.assertEqual(window.hk_toggle.text(), "ctrl+alt+t")

    def test_default_no_toggle_hotkey(self):
        window = self.make_settings_window({})
        self.assertEqual(window.hk_toggle.text(), "")


class TrayApplicationHotkeyTests(unittest.TestCase):
    def _make_subject(self, settings, extra=None):
        subject = SimpleNamespace(
            recorder=None,
            settings_window=FakeSettingsWindow(settings),
            signals=SimpleNamespace(recording_finished=SimpleNamespace(emit=lambda *a: None)),
            action_toggle=SimpleNamespace(setText=lambda v: None),
            tray_icon=SimpleNamespace(setIcon=lambda v: None, setToolTip=lambda v: None),
            icon_rec_path="icon_rec.png",
            show_tray_notification=lambda *a, **k: None,
        )
        if extra:
            for key, value in extra.items():
                setattr(subject, key, value)
        return subject

    def test_start_recording_builds_recorder_from_tracks(self):
        settings = {
            "tracks": [
                {"kind": "input", "input_id": "input1"},
                {"kind": "output", "speaker_id": "speaker-id"},
                {"kind": "app", "target_pid": 1234},
            ],
            "output_folder": ".",
            "format": "wav",
            "sample_rate": "48000",
            "stereo": True,
            "normalize": False,
            "output_mode": "mixed",
        }
        subject = self._make_subject(settings)

        with patch("gui.AudioRecorder") as recorder_cls, patch(
            "gui.psutil.pid_exists", return_value=True
        ):
            recorder_cls.return_value.is_alive.return_value = True
            TrayApplication.start_recording(subject)

        kwargs = recorder_cls.call_args.kwargs
        self.assertEqual(kwargs["tracks"], settings["tracks"])
        self.assertEqual(kwargs["sample_rate"], "48000")
        self.assertNotIn("input_id", kwargs)
        self.assertNotIn("target_pid", kwargs)
        self.assertNotIn("quality", kwargs)

    def test_start_recording_skips_missing_app(self):
        settings = {
            "tracks": [{"kind": "app", "target_pid": 1234}],
            "output_folder": ".",
            "format": "wav",
            "sample_rate": "48000",
            "stereo": False,
            "normalize": False,
            "output_mode": "mixed",
        }
        messages = []
        subject = self._make_subject(
            settings,
            extra={
                "tray_icon": SimpleNamespace(
                    setIcon=lambda v: None,
                    setToolTip=lambda v: None,
                    showMessage=lambda *a: messages.append(a),
                )
            },
        )

        with patch("gui.AudioRecorder") as recorder_cls, patch(
            "gui.psutil.pid_exists", return_value=False
        ):
            TrayApplication.start_recording(subject)

        recorder_cls.assert_not_called()
        self.assertTrue(messages)

    def test_register_hotkeys_uses_single_toggle(self):
        hotkey_manager = FakeHotkeyManager()
        subject = SimpleNamespace(
            hotkey_manager=hotkey_manager,
            settings_window=FakeSettingsWindow({"hk_toggle": "alt+shift+r"}),
            toggled=[],
        )
        subject.toggle_recording = lambda: subject.toggled.append(True)

        with patch("gui.keyboard.add_hotkey") as add_hotkey, patch(
            "gui.keyboard.unhook_all_hotkeys"
        ) as unhook:
            TrayApplication.register_hotkeys(subject)

        self.assertTrue(hotkey_manager.cleared)
        self.assertEqual([item[0] for item in hotkey_manager.registrations], ["alt+shift+r"])
        self.assertFalse(add_hotkey.called)
        self.assertFalse(unhook.called)

        hotkey_manager.registrations[0][1]()
        self.assertEqual(subject.toggled, [True])

    def test_toggle_recording_stops_active_recording(self):
        subject = SimpleNamespace(
            recorder=FakeRecorder(alive=True),
            stopped=False,
            started=False,
        )
        subject.stop_recording = lambda: setattr(subject, "stopped", True)
        subject.start_recording = lambda: setattr(subject, "started", True)

        TrayApplication.toggle_recording(subject)

        self.assertTrue(subject.stopped)
        self.assertFalse(subject.started)

    def test_toggle_recording_starts_when_idle(self):
        subject = SimpleNamespace(
            recorder=None,
            stopped=False,
            started=False,
        )
        subject.stop_recording = lambda: setattr(subject, "stopped", True)
        subject.start_recording = lambda: setattr(subject, "started", True)

        TrayApplication.toggle_recording(subject)

        self.assertFalse(subject.stopped)
        self.assertTrue(subject.started)

    def test_tray_click_toggles_recording(self):
        subject = SimpleNamespace(
            recorder=None,
            toggled=False,
        )
        subject.toggle_recording = lambda: setattr(subject, "toggled", True)

        TrayApplication.on_tray_activated(
            subject, QSystemTrayIcon.ActivationReason.Trigger
        )

        self.assertTrue(subject.toggled)


class TrayApplicationNotificationTests(unittest.TestCase):
    def test_notification_is_skipped_when_disabled(self):
        subject = SimpleNamespace(
            tray_icon=FakeTrayIcon(),
            settings_window=FakeSettingsWindow({"show_notifications": False}),
        )

        TrayApplication.show_tray_notification(subject, "Started", "Recording input")

        self.assertEqual(subject.tray_icon.messages, [])

    def test_notification_is_sent_when_enabled(self):
        subject = SimpleNamespace(
            tray_icon=FakeTrayIcon(),
            settings_window=FakeSettingsWindow({"show_notifications": True}),
        )

        TrayApplication.show_tray_notification(subject, "Started", "Recording input", duration=1234)

        self.assertEqual(len(subject.tray_icon.messages), 1)
        self.assertEqual(subject.tray_icon.messages[0][0], "Started")
        self.assertEqual(subject.tray_icon.messages[0][1], "Recording input")
        self.assertEqual(subject.tray_icon.messages[0][3], 1234)


if __name__ == "__main__":
    unittest.main()
