import sys

import pytest

try:
    from PyQt6.QtWidgets import QApplication

    import gui
except Exception:  # pragma: no cover - GUI unavailable in this environment
    pytest.skip("Qt/PyQt6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication(sys.argv)
    yield application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "CONFIG_FILE", str(tmp_path / "settings.json"))
    return gui.SettingsWindow()


def test_settings_window_defaults_to_single_output_track(window):
    tracks = window.get_tracks()
    assert len(tracks) == 1
    assert tracks[0]["kind"] == "output"


def test_add_app_track_exposes_target_pid(window):
    window.add_track({"kind": "app", "target_pid": 4242, "target_name": "name.exe"})
    tracks = window.get_tracks()
    assert any(t.get("target_pid") == 4242 for t in tracks)


def test_get_settings_returns_tracks_and_sample_rate(window):
    settings = window.get_settings()
    assert isinstance(settings["tracks"], list)
    assert settings["tracks"][0]["kind"] == "output"
    assert settings["sample_rate"] == "48000"
    assert "hk_toggle" in settings


def test_remove_track_keeps_at_least_one(window):
    window.add_track({"kind": "input", "input_id": "default"})
    first = window.tracks_layout.itemAt(0).widget()
    window.remove_track(first)
    assert window.tracks_layout.count() >= 1
