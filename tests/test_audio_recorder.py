"""Comprehensive mock-based tests for audio_recorder.py.

Uses MockPyAudio + MockStream to simulate PortAudio callback-based capture
without touching real hardware. Tests the full capture → write → resample pipeline.
"""
import os
import threading
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

import audio_recorder

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_wav(path, frames=480, channels=2, samplerate=48000):
    sf.write(path, np.zeros((frames, channels), dtype=np.float32), samplerate,
             format="WAV", subtype="PCM_16")
    return path


# Generic device-info factories. The pre-commit tests selected devices through
# the mocked audio library's getters (get_microphone / all_speakers) and never
# referenced a concrete hardware name. We mirror that here: test devices are
# produced by these helpers with neutral names so the suite does not depend on
# any specific sound card being installed.
def make_input_device(index=0, name="capture-input", channels=2, rate=48000):
    return {
        "name": name,
        "maxInputChannels": channels,
        "maxOutputChannels": 0,
        "defaultSampleRate": float(rate),
        "index": index,
        "hostApi": 2,
        "isLoopbackDevice": False,
    }


def make_output_device(index=0, name="loopback-output", channels=2, rate=48000):
    return {
        "name": name,
        "maxInputChannels": 0,
        "maxOutputChannels": channels,
        "defaultSampleRate": float(rate),
        "index": index,
        "hostApi": 2,
        "isLoopbackDevice": True,
    }


# Default devices used when a test does not supply its own.
DEFAULT_INPUT = make_input_device(index=48, name="default-capture-input")
DEFAULT_OUTPUT = make_output_device(index=60, name="default-loopback-output")

# OS-reserved MME name that RawRecorder explicitly skips during enumeration.
# Retained verbatim because it exercises that filter branch.
MME_RESERVED_INPUT = {
    "name": "Microsoft Sound Mapper - Input",
    "maxInputChannels": 2,
    "maxOutputChannels": 0,
    "defaultSampleRate": 44100.0,
    "index": 0,
    "hostApi": 0,
    "isLoopbackDevice": False,
}


class MockStream:
    """Simulates a PyAudio callback-based stream.

    The callback is invoked from a background thread in chunks, controlled by
    an audio_queue. Push data as raw float32 bytes; stop with close()/stop_stream().
    """
    def __init__(self, audio_queue=None, chunk_frames=480, channels=2, rate=48000,
                 callback=None, max_chunks=None):
        self._audio_queue = audio_queue or Queue()
        self._chunk_frames = chunk_frames
        self._channels = channels
        self._rate = rate
        self._callback = callback
        self._active = False
        self._closed = False
        self._thread = None
        self._max_chunks = max_chunks  # auto-stop after N chunks
        self._chunk_count = 0

    def start_stream(self):
        self._active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._active and not self._closed:
            try:
                raw = self._audio_queue.get(timeout=0.1)
            except Empty:
                if not self._active:
                    break
                # Generate silence when queue is empty
                raw = b'\x00' * (self._chunk_frames * self._channels * 4)
            if raw is None:
                # None sentinel = stop
                self._active = False
                break
            if self._callback:
                try:
                    result = self._callback(raw, self._chunk_frames, {}, 0)
                    if result and len(result) >= 2:
                        status = result[1]
                        # paComplete = 0, paContinue = 1
                        if status == 0:
                            self._active = False
                            break
                except Exception:
                    pass
            self._chunk_count += 1
            if self._max_chunks and self._chunk_count >= self._max_chunks:
                self._active = False
                break

    def is_active(self):
        return self._active

    def stop_stream(self):
        self._active = False

    def close(self):
        self._closed = True
        self._active = False
        # Send None sentinel to unblock the thread
        self._audio_queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


class MockPyAudio:
    """Simulates pyaudio.PyAudio for WASAPI device enumeration and stream opening."""
    paWASAPI = 13
    paFloat32 = 8
    paContinue = 1
    paComplete = 0

    def __init__(self, input_devices=None, output_devices=None, default_input=None,
                 default_output=None, stream=None, wasapi_index=2):
        self._input_devices = input_devices or [DEFAULT_INPUT]
        self._output_devices = output_devices or [DEFAULT_OUTPUT]
        self._default_input = default_input or self._input_devices[0]
        self._default_output = default_output or (self._output_devices[0]
                                                        if self._output_devices else None)
        self._stream = stream
        self._wasapi_index = wasapi_index

    def get_device_info_generator(self):
        return iter(self._input_devices)

    def get_loopback_device_info_generator(self):
        return iter(self._output_devices)

    def get_default_input_device_info(self):
        return self._default_input

    def get_default_output_device_info(self):
        return {"name": "Speakers", "index": 46, "maxOutputChannels": 2,
                "defaultSampleRate": 48000.0, "hostApi": 2}

    def get_default_loopback_device_info(self):
        if self._default_output:
            return self._default_output
        raise AttributeError("No output device")

    def get_host_api_info_by_type(self, host_api_type):
        return {"index": self._wasapi_index}

    def open(self, **kwargs):
        self._last_open_kwargs = kwargs
        return self._stream

    def terminate(self):
        pass


def _make_mock_pyaudio(audio_chunks, channels=2, rate=48000, chunk_frames=480,
                       devices=None, outputs=None, default_input=None):
    """Create a MockPyAudio + MockStream that feeds audio_chunks via callback."""
    q = Queue()
    for chunk in audio_chunks:
        q.put(chunk.tobytes() if isinstance(chunk, np.ndarray) else chunk)
    # Stop sentinel after all data
    q.put(None)

    mock_stream = MockStream(audio_queue=q, chunk_frames=chunk_frames,
                             channels=channels, rate=rate)
    # We need to capture the callback that RawRecorder passes to p.open()
    # so we replace open() to intercept it
    mock_p = MockPyAudio(
        input_devices=devices or [DEFAULT_INPUT],
        output_devices=outputs or [DEFAULT_OUTPUT],
        default_input=default_input or DEFAULT_INPUT,
        stream=mock_stream,
    )
    # Intercept open() to capture the callback from RawRecorder
    def capturing_open(**kwargs):
        cb = kwargs.get("stream_callback")
        if cb:
            mock_stream._callback = cb
        return mock_stream
    mock_p.open = capturing_open
    return mock_p, mock_stream


def single_wait_stopper(recorder):
    def _wait(timeout=None):
        recorder.stop_event.set()
        return True
    return _wait


# ── Device Enumeration Tests ─────────────────────────────────────────────────

@patch('audio_recorder.pyaudio')
def test_get_devices_output_lists_speakers(mock_pyaudio):
    out_info = make_output_device(name="loopback-output")
    mock_p = MockPyAudio(
        input_devices=[],
        output_devices=[out_info],
        default_output=out_info,
    )
    mock_pyaudio.PyAudio.return_value = mock_p
    mock_pyaudio.paWASAPI = 13

    devices = audio_recorder.get_devices(include_output=True)

    assert len(devices) == 1
    assert devices[0] == {"id": out_info["name"],
                          "name": out_info["name"], "is_default": True}


@patch('audio_recorder.pyaudio')
def test_get_devices_inputs_skip_output(mock_pyaudio):
    input_info = make_input_device(name="capture-input")
    output_info = make_output_device(name="loopback-output")
    mock_p = MockPyAudio(
        input_devices=[input_info, output_info],
        output_devices=[],
        default_input=input_info,
    )
    mock_pyaudio.PyAudio.return_value = mock_p
    mock_pyaudio.paWASAPI = 13

    devices = audio_recorder.get_devices(include_output=False)

    assert len(devices) == 1
    assert devices[0] == {"id": input_info["name"], "name": input_info["name"], "is_default": True}


@patch('audio_recorder.pyaudio')
def test_get_devices_skips_mme_devices(mock_pyaudio):
    """MME devices should not appear in the device list."""
    mock_p = MockPyAudio(
        input_devices=[MME_RESERVED_INPUT, make_input_device(name="capture-input")],
        output_devices=[],
        default_input=make_input_device(name="capture-input"),
    )
    mock_pyaudio.PyAudio.return_value = mock_p
    mock_pyaudio.paWASAPI = 13

    devices = audio_recorder.get_devices(include_output=False)

    assert len(devices) == 1
    assert devices[0]["name"] == "capture-input"


@patch('audio_recorder.pyaudio')
def test_get_devices_enumerates_off_caller_thread(mock_pyaudio):
    seen = {}

    def spy():
        seen["thread"] = threading.current_thread()
        return iter([])

    mock_p = MockPyAudio(input_devices=[], output_devices=[])
    mock_p.get_loopback_device_info_generator = spy
    mock_pyaudio.PyAudio.return_value = mock_p

    audio_recorder.get_devices(include_output=True)

    assert seen["thread"] is not threading.main_thread()


# ── _find_device Tests ──────────────────────────────────────────────────────

def test_find_device_matches_by_name():
    p = MockPyAudio(input_devices=[DEFAULT_INPUT], default_input=DEFAULT_INPUT)
    rec = audio_recorder.RawRecorder(DEFAULT_INPUT["name"], "/tmp/test.wav")
    result = rec._find_device(p)
    assert result["name"] == DEFAULT_INPUT["name"]


def test_find_device_falls_back_to_default():
    p = MockPyAudio(input_devices=[DEFAULT_INPUT], default_input=DEFAULT_INPUT)
    rec = audio_recorder.RawRecorder("Nonexistent Device", "/tmp/test.wav")
    result = rec._find_device(p)
    # Should fall back to first WASAPI input device
    assert result["name"] == DEFAULT_INPUT["name"]


def test_find_device_skips_mme():
    """When only MME + WASAPI exist, should return WASAPI."""
    p = MockPyAudio(
        input_devices=[MME_RESERVED_INPUT, DEFAULT_INPUT],
        default_input=DEFAULT_INPUT,
    )
    rec = audio_recorder.RawRecorder("Input", "/tmp/test.wav")
    result = rec._find_device(p)
    assert result["name"] == DEFAULT_INPUT["name"]


def test_find_device_output():
    out = make_output_device(name="loopback-output")
    p = MockPyAudio(output_devices=[out])
    rec = audio_recorder.RawRecorder(out["name"], "/tmp/test.wav", is_output=True)
    result = rec._find_device(p)
    assert result["name"] == out["name"]


def test_find_device_output_fallback():
    out = make_output_device(name="loopback-output")
    p = MockPyAudio(output_devices=[out])
    rec = audio_recorder.RawRecorder("Nonexistent", "/tmp/test.wav", is_output=True)
    result = rec._find_device(p)
    assert result["name"] == out["name"]


# ── RawRecorder Capture Tests ───────────────────────────────────────────────

def test_raw_recorder_captures_audio_data(tmp_path):
    """RawRecorder callback writes data to WAV file."""
    channels = 2
    sr = 48000
    chunk_frames = 480
    n_chunks = 5

    # Generate test audio: 5 chunks of stereo float32
    chunks = []
    for i in range(n_chunks):
        t = np.linspace(0, chunk_frames / sr, chunk_frames, endpoint=False)
        freq = 440.0 + i * 100
        data = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        data = np.column_stack([data, data * 0.8])  # stereo
        chunks.append(data)

    mock_p, _ = _make_mock_pyaudio(
        chunks, channels=channels, rate=sr, chunk_frames=chunk_frames,
    )

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "capture.wav")
        rec = audio_recorder.RawRecorder("TestInput", path, samplerate=sr, channels=2)
        rec.run()

    assert rec.error is None
    assert os.path.exists(path)
    info = sf.info(path)
    assert info.samplerate == sr
    assert info.channels == 2
    audio, _ = sf.read(path, dtype="float32")
    assert audio.shape[0] > 0  # non-empty
    peak = np.max(np.abs(audio))
    assert peak > 0.01  # actual audio data, not silence


def test_raw_recorder_stereo_to_mono(tmp_path):
    """When channels=1, stereo input is mixed down to mono."""
    channels = 2  # device is stereo
    sr = 48000
    chunk_frames = 480

    # Stereo input with distinct L/R
    data = np.zeros((chunk_frames, 2), dtype=np.float32)
    data[:, 0] = 0.8  # left
    data[:, 1] = 0.2  # right
    chunks = [data]

    mock_p, _ = _make_mock_pyaudio(
        chunks, channels=channels, rate=sr, chunk_frames=chunk_frames,
    )

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "mono.wav")
        rec = audio_recorder.RawRecorder("TestInput", path, samplerate=sr, channels=1)
        rec.run()

    assert rec.error is None
    audio, _ = sf.read(path, dtype="float32")
    assert audio.ndim == 1  # mono
    # Mean of 0.8 and 0.2 = 0.5
    assert np.allclose(audio, 0.5, atol=0.01)


def test_raw_recorder_resamples_native_to_target(tmp_path):
    """When native rate (48000) != target (16000), resampling occurs."""
    native_sr = 48000
    target_sr = 16000
    chunk_frames = 480
    channels = 1

    data = np.sin(np.linspace(0, 2 * np.pi * 440, native_sr, endpoint=False)).astype(np.float32)
    data = data.reshape(-1, 1)
    # Split into chunks of chunk_frames
    chunks = [data[i:i+chunk_frames] for i in range(0, len(data), chunk_frames)]

    mock_p, _ = _make_mock_pyaudio(
        chunks, channels=channels, rate=native_sr, chunk_frames=chunk_frames,
    )

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "resampled.wav")
        rec = audio_recorder.RawRecorder("TestInput", path, samplerate=target_sr, channels=1)
        rec.run()

    assert rec.error is None
    info = sf.info(path)
    assert info.samplerate == target_sr


def test_raw_recorder_no_audio_produces_empty_file(tmp_path):
    """When no audio data arrives, file is created but empty (0 frames)."""
    mock_p, _ = _make_mock_pyaudio(
        [], channels=2, rate=48000, chunk_frames=480,
    )

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "empty.wav")
        rec = audio_recorder.RawRecorder("TestInput", path, samplerate=48000, channels=1)
        rec.run()

    assert rec.error is None
    assert os.path.exists(path)
    info = sf.info(path)
    assert info.frames == 0


def test_raw_recorder_stop_signal(tmp_path):
    """stop_event causes callback to return paComplete, ending the stream."""
    channels = 2
    sr = 48000
    chunk_frames = 480

    data = np.ones((chunk_frames, channels), dtype=np.float32) * 0.5
    chunks = [data] * 20  # lots of data, but we'll stop early

    mock_p, _ = _make_mock_pyaudio(
        chunks, channels=channels, rate=sr, chunk_frames=chunk_frames,
    )

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "stopped.wav")
        rec = audio_recorder.RawRecorder("TestInput", path, samplerate=sr, channels=2)
        # Set stop before starting — callback should return paComplete immediately
        rec.stop_event.set()
        rec.run()

    assert rec.error is None
    assert os.path.exists(path)


def test_raw_recorder_handles_exception(tmp_path):
    """Exceptions during capture are caught and stored in rec.error."""
    mock_p = MockPyAudio(input_devices=[], output_devices=[])

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "error.wav")
        rec = audio_recorder.RawRecorder("Nonexistent", path)
        # Patch _find_device to raise, simulating device resolution failure
        with patch.object(audio_recorder.RawRecorder, '_find_device',
                          side_effect=RuntimeError("No device")):
            rec.run()

    assert rec.error is not None
    assert "No device" in rec.error


def test_raw_recorder_device_resolution_during_run_not_init(tmp_path):
    """_find_device is called during run(), not at __init__ time."""
    find_calls = []

    mock_p = MockPyAudio(
        input_devices=[DEFAULT_INPUT],
        default_input=DEFAULT_INPUT,
    )
    original_find = audio_recorder.RawRecorder._find_device
    def tracked_find(self, p):
        find_calls.append(True)
        return original_find(self, p)

    # Provide audio data + stop sentinel so the stream terminates
    data = np.zeros((480, 2), dtype=np.float32)
    q = Queue()
    q.put(data.tobytes())
    q.put(None)
    mock_stream = MockStream(audio_queue=q, chunk_frames=480, channels=2, rate=48000)
    def capturing_open(**kwargs):
        return mock_stream
    mock_p.open = capturing_open

    with patch('audio_recorder.pyaudio.PyAudio', return_value=mock_p):
        path = str(tmp_path / "thread.wav")
        with patch.object(audio_recorder.RawRecorder, '_find_device', tracked_find):
            rec = audio_recorder.RawRecorder("Input", path, samplerate=48000, channels=1)
            # _find_device not called yet
            assert len(find_calls) == 0
            rec.run()
            # _find_device was called during run()
            assert len(find_calls) == 1


# ── Resampler Tests ──────────────────────────────────────────────────────────

def test_resample_wav_reduces_sample_rate(tmp_path):
    """_resample_wav resamples from native rate to target rate."""
    sr_in = 48000
    sr_out = 16000
    duration = 0.5
    t = np.linspace(0, duration, int(sr_in * duration), endpoint=False)
    data = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32).reshape(-1, 1)

    path = str(tmp_path / "to_resample.wav")
    sf.write(path, data, sr_in, format="WAV", subtype="PCM_16")

    audio_recorder._resample_wav(path, sr_out)

    result, sr = sf.read(path, dtype="float32")
    assert sr == sr_out
    expected_frames = int(sr_out * duration)
    assert abs(result.shape[0] - expected_frames) <= 1


def test_resample_wav_stereo(tmp_path):
    """_resample_wav handles stereo audio."""
    sr_in = 48000
    sr_out = 16000
    t = np.linspace(0, 0.1, int(sr_in * 0.1), endpoint=False)
    data = np.column_stack([
        (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
        (0.3 * np.sin(2 * np.pi * 880 * t)).astype(np.float32),
    ])

    path = str(tmp_path / "stereo_resample.wav")
    sf.write(path, data, sr_in, format="WAV", subtype="PCM_16")

    audio_recorder._resample_wav(path, sr_out)

    result, sr = sf.read(path, dtype="float32")
    assert sr == sr_out
    assert result.shape[1] == 2


# ── Channel Formatting Tests ────────────────────────────────────────────────

def test_format_output_channels_stereo_to_mono():
    recorder = MagicMock()
    recorder.stereo = False
    recorder.profile = audio_recorder.build_output_profile("wav", "48000", False)
    result = audio_recorder.AudioRecorder._format_output_channels(
        recorder, np.array([[0.2, 0.4]], dtype=np.float32)
    )
    assert result.shape == (1, 1)
    assert result[0, 0] == pytest.approx(0.3)


def test_format_output_channels_mono_to_stereo():
    recorder = MagicMock()
    recorder.stereo = True
    recorder.profile = audio_recorder.build_output_profile("wav", "48000", True)
    result = audio_recorder.AudioRecorder._format_output_channels(
        recorder, np.array([[0.25]], dtype=np.float32)
    )
    assert result.shape == (1, 2)
    assert np.array_equal(result, [[0.25, 0.25]])


def test_format_output_channels_already_correct():
    recorder = MagicMock()
    recorder.stereo = True
    recorder.profile = audio_recorder.build_output_profile("wav", "48000", True)
    data = np.array([[0.1, 0.2]], dtype=np.float32)
    result = audio_recorder.AudioRecorder._format_output_channels(recorder, data)
    assert result.shape == (1, 2)
    assert np.allclose(result, data)


# ── AudioRecorder Integration Tests (mocked _build_recorders) ───────────────

def _finish_with_tracks(tracks, output_mode, tmp_path):
    input_wav = make_wav(os.path.join(str(tmp_path), "input_src.wav"))
    output_wav = make_wav(os.path.join(str(tmp_path), "output_src.wav"))
    recorder = audio_recorder.AudioRecorder(
        tracks=tracks,
        output_folder=str(tmp_path),
        output_format="wav",
        output_mode=output_mode,
    )

    def fake_build(target_sr):
        recorder.temp_files = [input_wav, output_wav]
        recorder.track_labels = ["input", "output"]
        recorder.recorders = []

    recorder._build_recorders = fake_build
    recorder.stop_event.set()
    recorder.run()
    return recorder.result


def test_separate_files_mode_writes_one_labeled_file_per_track(tmp_path):
    result = _finish_with_tracks(
        [{"kind": "input", "input_id": "default"},
         {"kind": "output"}],
        "separate_files", tmp_path,
    )
    assert result.output_mode == "separate_files"
    labels = [a.label for a in result.artifacts]
    assert labels == ["input", "output"]
    names = [os.path.basename(p) for p in result.paths]
    assert any("_input.wav" in n for n in names)
    assert any("_output.wav" in n for n in names)
    for p in result.paths:
        assert os.path.exists(p)
        info = sf.info(p)
        assert info.format == "WAV"


def test_mixed_mode_combines_tracks_into_single_artifact(tmp_path):
    result = _finish_with_tracks(
        [{"kind": "input", "input_id": "default"},
         {"kind": "output"}],
        "mixed", tmp_path,
    )
    assert result.output_mode == "mixed"
    assert len(result.paths) == 1
    assert result.artifacts[0].label == "mixed"
    assert result.primary_path is not None
    assert os.path.exists(result.primary_path)


def test_invalid_output_mode_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        audio_recorder.AudioRecorder(
            tracks=[{"kind": "output"}],
            output_folder=str(tmp_path),
            output_format="wav",
            output_mode="one_big_file",
        )


def test_audio_recorder_uses_profile_sample_rate(tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "output"}], output_folder=str(tmp_path),
        output_format="wav", sample_rate="48000", stereo=True,
    )
    captured = {}

    def fake_build(target_sr):
        captured["target_sr"] = target_sr
        recorder.stop_event.set()

    recorder._build_recorders = fake_build
    recorder.run()
    assert captured["target_sr"] == 48000


def test_audio_recorder_uses_profile_sample_rate_high(tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "output"}], output_folder=str(tmp_path),
        output_format="wav", sample_rate="96000", stereo=True,
    )
    captured = {}

    def fake_build(target_sr):
        captured["target_sr"] = target_sr
        recorder.stop_event.set()

    recorder._build_recorders = fake_build
    recorder.run()
    assert captured["target_sr"] == 96000


def test_audio_recorder_custom_speaker(tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "output", "speaker_id": "Speaker 2 Name"}],
        output_folder=str(tmp_path), output_format="wav",
    )
    assert recorder.tracks[0]["speaker_id"] == "Speaker 2 Name"


def test_audio_recorder_output_defaults_to_default_speaker(tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "output"}],
        output_folder=str(tmp_path), output_format="wav",
    )
    assert recorder.tracks[0].get("speaker_id") is None


@patch('audio_recorder.ProcTapRecorder')
def test_audio_recorder_per_app_output(mock_proctap_recorder, tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "app", "target_pid": 1234}],
        output_folder=str(tmp_path), output_format="wav",
    )
    mock_instance = MagicMock()
    mock_proctap_recorder.return_value = mock_instance
    recorder.stop_event.wait = single_wait_stopper(recorder)
    with patch('process_utils.get_root_pid', return_value=1234):
        recorder.run()

    mock_proctap_recorder.assert_called_once()
    args, _ = mock_proctap_recorder.call_args
    assert args[0] == 1234
    assert len(recorder.recorders) == 1


@patch('audio_recorder.ProcTapRecorder')
@patch('audio_recorder.RawRecorder')
def test_audio_recorder_per_app_both(mock_raw_recorder, mock_proctap_recorder, tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "app", "target_pid": 5678},
                {"kind": "input", "input_id": "input1"}],
        output_folder=str(tmp_path), output_format="wav",
    )
    mock_input = MagicMock()
    mock_raw_recorder.return_value = mock_input
    mock_app = MagicMock()
    mock_proctap_recorder.return_value = mock_app

    recorder.stop_event.wait = single_wait_stopper(recorder)
    recorder._mix_audio = MagicMock()
    with patch('process_utils.get_root_pid', return_value=5678):
        recorder.run()

    mock_proctap_recorder.assert_called_once()
    mock_raw_recorder.assert_called_once()
    args, _ = mock_proctap_recorder.call_args
    assert args[0] == 5678


@patch('audio_recorder.ProcTapRecorder')
def test_audio_recorder_app_crash(mock_proctap_recorder, tmp_path):
    captured_wav = make_wav(os.path.join(str(tmp_path), "captured.wav"))
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "app", "target_pid": 9999}],
        output_folder=str(tmp_path), output_format="wav",
    )

    def fake_new_temp_file():
        recorder.temp_files.append(captured_wav)
        return captured_wav
    recorder._new_temp_file = fake_new_temp_file

    mock_instance = MagicMock()
    mock_instance.is_alive.return_value = False
    mock_instance.error = "Process terminated"
    mock_proctap_recorder.return_value = mock_instance

    with patch('process_utils.get_root_pid', return_value=9999):
        recorder.run()

    assert recorder.final_filepath is not None
    assert os.path.exists(recorder.final_filepath)
    assert "terminated" in str(recorder.error_message).lower() or \
           "closed" in str(recorder.error_message).lower()


def test_prime_device_cache_enumerates_once(monkeypatch):
    previous = audio_recorder._DEVICE_CACHE
    audio_recorder._DEVICE_CACHE = None
    try:
        calls = []

        def fake_get_devices(include_output=False):
            calls.append(include_output)
            return [{"id": "x", "name": "X", "is_default": True}]

        monkeypatch.setattr(audio_recorder, "get_devices", fake_get_devices)

        first = audio_recorder.prime_device_cache()
        second = audio_recorder.prime_device_cache()

        assert first is second
        assert sorted(calls) == [False, True]
        assert audio_recorder.device_cache() is first
    finally:
        audio_recorder._DEVICE_CACHE = previous


# ── Build Output Profile Tests ──────────────────────────────────────────────

def test_build_output_profile_balanced_mono():
    profile = audio_recorder.build_output_profile("wav", "48000", False)
    assert profile["sample_rate"] == 48000
    assert profile["channels"] == 1


def test_build_output_profile_balanced_stereo():
    profile = audio_recorder.build_output_profile("wav", "48000", True)
    assert profile["sample_rate"] == 48000
    assert profile["channels"] == 2


def test_build_output_profile_high_quality():
    profile = audio_recorder.build_output_profile("wav", "96000", True)
    assert profile["sample_rate"] == 96000
    assert profile["channels"] == 2


def test_build_output_profile_invalid_sample_rate_raises():
    with pytest.raises(ValueError):
        audio_recorder.build_output_profile("wav", "unknown", False)


# ── Track-based Recorder Tests ──────────────────────────────────────────────

def test_audio_recorder_builds_one_recorder_per_track(tmp_path):
    recorder = audio_recorder.AudioRecorder(
        tracks=[{"kind": "input", "input_id": "input1"},
                {"kind": "output"},
                {"kind": "app", "target_pid": 1234}],
        output_folder=str(tmp_path), output_format="wav",
    )
    with patch('process_utils.get_root_pid', return_value=1234):
        recorder._build_recorders(48000)
    assert len(recorder.recorders) == 3


def test_empty_tracks_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        audio_recorder.AudioRecorder(tracks=[], output_folder=str(tmp_path))
