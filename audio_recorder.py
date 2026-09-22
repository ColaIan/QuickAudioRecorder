from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

import lameenc
import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

# PortAudio's Pa_Initialize/Pa_Terminate are not safe to call concurrently from
# multiple threads. RawRecorder threads each open their own PyAudio instance, so
# we serialize creation/teardown to avoid an access-violation crash on init.
_PA_LOCK = threading.Lock()


def _new_pyaudio():
    with _PA_LOCK:
        return pyaudio.PyAudio()


def _terminate_pyaudio(p):
    with _PA_LOCK:
        p.terminate()


FORMAT_CONFIG = {
    "wav": {"label": "WAV", "extension": ".wav", "encoder": "soundfile", "format": "WAV"},
    "flac": {"label": "FLAC", "extension": ".flac", "encoder": "soundfile", "format": "FLAC"},
    "mp3": {"label": "MP3", "extension": ".mp3", "encoder": "lameenc"},
}

SAMPLE_RATE_CONFIG = {
    "44100": {"label": "44.1 kHz", "sample_rate": 44100, "subtype": "PCM_16", "mp3_bitrate_kbps": 128},
    "48000": {"label": "48 kHz", "sample_rate": 48000, "subtype": "PCM_16", "mp3_bitrate_kbps": 128},
    "96000": {"label": "96 kHz", "sample_rate": 96000, "subtype": "PCM_16", "mp3_bitrate_kbps": 192},
    "192000": {"label": "192 kHz", "sample_rate": 192000, "subtype": "PCM_16", "mp3_bitrate_kbps": 256},
}

NORMALIZE_ACTIVE_FLOOR = 0.001
NORMALIZE_TARGET_LEVEL = 0.12
NORMALIZE_MAX_GAIN = 8.0
NORMALIZE_REFERENCE_PERCENTILE = 95
NORMALIZE_LIMIT = 0.98
CAPTURE_BLOCK_FRAMES = 512
CAPTURE_BUFFER_FRAMES = 2048


@dataclass(frozen=True)
class RecordingArtifact:
    path: str
    label: str


@dataclass(frozen=True)
class RecordingResult:
    artifacts: tuple
    output_mode: str = "mixed"

    @property
    def paths(self):
        return tuple(artifact.path for artifact in self.artifacts)

    @property
    def primary_path(self):
        return self.artifacts[0].path if self.artifacts else None

    def __bool__(self):
        return bool(self.artifacts)


@dataclass(frozen=True)
class CaptureDeviceSpec:
    device_name: str | None
    is_output: bool = False


def build_output_profile(fmt, sample_rate, stereo):
    fmt_key = str(fmt or "").strip().lower()
    rate_key = str(sample_rate or "").strip().lower()
    if fmt_key not in FORMAT_CONFIG:
        raise ValueError(f"Unsupported output format: {fmt}")
    if rate_key not in SAMPLE_RATE_CONFIG:
        raise ValueError(f"Unsupported output sample rate: {sample_rate}")
    return {
        **SAMPLE_RATE_CONFIG[rate_key],
        **FORMAT_CONFIG[fmt_key],
        "label": FORMAT_CONFIG[fmt_key]["label"],
        "format_label": FORMAT_CONFIG[fmt_key]["label"],
        "sample_rate_label": SAMPLE_RATE_CONFIG[rate_key]["label"],
        "format_key": fmt_key,
        "sample_rate_key": rate_key,
        "channels": 2 if stereo else 1,
    }


def describe_output_profile(fmt, sample_rate, stereo):
    profile = build_output_profile(fmt, sample_rate, stereo)
    rate_khz = f"{profile['sample_rate'] / 1000:g}"
    channels = "stereo" if profile["channels"] == 2 else "mono"
    encoding = (f"{profile['mp3_bitrate_kbps']} kbps" if profile["encoder"] == "lameenc" else profile["subtype"])
    return f"{profile['format_label']} / {rate_khz} kHz / {channels} / {encoding}"


def _get_wasapi_host_api_index(p):
    """Return the host API index for WASAPI, or None if unavailable."""
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        return wasapi_info["index"]
    except Exception:
        return None


def _is_wasapi_device(info, wasapi_index):
    """Check if a device info dict belongs to the WASAPI host API."""
    if wasapi_index is None:
        return True
    return info.get("hostApi") == wasapi_index


def _resample_wav(filepath, target_sr):
    """Resample a WAV file in-place to target sample rate using numpy."""
    data, orig_sr = sf.read(filepath, always_2d=True)
    if orig_sr == target_sr:
        return
    n_samples = int(len(data) * target_sr / orig_sr)
    indices = np.linspace(0, len(data) - 1, n_samples)
    resampled = np.column_stack([
        np.interp(indices, np.arange(len(data)), data[:, ch])
        for ch in range(data.shape[1])
    ]).astype(np.float32)
    info = sf.info(filepath)
    sf.write(filepath, resampled, target_sr, format=info.format, subtype=info.subtype)


class RawRecorder(threading.Thread):
    """Capture a single device to a WAV file using PyAudioWPatch callbacks."""
    def __init__(self, device_name, filepath, is_output=False,
                 samplerate=44100, channels=2, subtype="PCM_16"):
        super().__init__()
        self.device_name = device_name
        self.is_output = is_output
        self.filepath = filepath
        self.samplerate = samplerate
        self.channels = channels
        self.subtype = subtype
        self.stop_event = threading.Event()
        self.error = None
        self._stream = None
        self._p = None

    def run(self):
        try:
            p = _new_pyaudio()
            self._p = p
            device_info = self._find_device(p)
            native_rate = int(device_info["defaultSampleRate"])
            device_channels = int(device_info["maxInputChannels"])

            wf = sf.SoundFile(self.filepath, mode="w", samplerate=native_rate,
                              channels=self.channels, format="WAV", subtype=self.subtype)

            buf = []
            buf_lock = threading.Lock()

            def callback(in_data, frame_count, time_info, status):
                if self.stop_event.is_set():
                    return (None, pyaudio.paComplete)
                data = np.frombuffer(in_data, dtype=np.float32).reshape(-1, device_channels)
                with buf_lock:
                    buf.append(data.copy())
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paFloat32,
                channels=device_channels,
                rate=native_rate,
                input=True,
                input_device_index=device_info["index"],
                frames_per_buffer=CAPTURE_BLOCK_FRAMES,
                stream_callback=callback,
            )
            self._stream = stream
            stream.start_stream()

            while not self.stop_event.is_set() and stream.is_active():
                time.sleep(0.05)

            stream.stop_stream()
            stream.close()

            with buf_lock:
                blocks = buf
            if blocks:
                recording = np.concatenate(blocks, axis=0)
                if self.channels == 1 and recording.shape[1] > 1:
                    recording = np.mean(recording, axis=1, keepdims=True)
                wf.write(recording)
            wf.close()

            if native_rate != self.samplerate:
                _resample_wav(self.filepath, self.samplerate)
        except Exception as e:
            self.error = str(e)
        finally:
            try:
                if self._stream is not None:
                    self._stream.close()
            except Exception:
                pass
            if self._p is not None:
                _terminate_pyaudio(self._p)
                self._p = None

    def _find_device(self, p):
        wasapi_index = _get_wasapi_host_api_index(p)
        if self.is_output:
            target_name = (self.device_name or "").strip()
            for info in p.get_loopback_device_info_generator():
                if target_name and target_name in info["name"]:
                    return info
            default = p.get_default_loopback_device_info()
            return default
        else:
            target_name = (self.device_name or "").strip()
            for info in p.get_device_info_generator():
                if (
                    info["maxInputChannels"] > 0
                    and info["name"] != "Microsoft Sound Mapper - Input"
                    and not info.get("isLoopbackDevice", False)
                    and _is_wasapi_device(info, wasapi_index)
                    and (not target_name or target_name in info["name"])
                ):
                    return info
            # Fallback: find the first WASAPI input device
            for info in p.get_device_info_generator():
                if (
                    info["maxInputChannels"] > 0
                    and info["name"] != "Microsoft Sound Mapper - Input"
                    and not info.get("isLoopbackDevice", False)
                    and _is_wasapi_device(info, wasapi_index)
                ):
                    return info
            return p.get_default_input_device_info()

    def stop(self):
        self.stop_event.set()
        self.join(timeout=3.0)
        if self.is_alive() and not self.error:
            self.error = "Hardware audio capture did not stop promptly."


class ProcTapRecorder(threading.Thread):
    """Record a specific application's audio to a WAV file via proc-tap."""
    def __init__(self, pid, filepath):
        super().__init__()
        self.pid = pid
        self.filepath = filepath
        self.samplerate = 48000
        self.channels = 2
        self.stop_event = threading.Event()
        self.error = None
        self.exit_reason = None
        self.max_amp_seen = 0.0
        self.chunks_read = 0

    def run(self):
        import multiprocessing
        process = None
        receiver = None
        try:
            receiver, sender = multiprocessing.Pipe(duplex=False)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_process_tap_worker, args=(self.pid, sender), daemon=True)
            process.start()
            sender.close()
            with sf.SoundFile(self.filepath, mode="w", samplerate=self.samplerate, channels=self.channels) as f_wav:
                # Write silence while the process is quiet so the recording
                # timeline begins the moment the button is clicked, rather than
                # only once audio actually starts flowing.
                silence = np.zeros((int(0.1 * self.samplerate), self.channels), dtype=np.float32)
                while not self.stop_event.is_set():
                    if receiver.poll(0.1):
                        message = _decode_message(receiver.recv())
                        if message[0] == "error":
                            self.exit_reason = message[1]
                            raise RuntimeError(message[2])
                        np_data = np.frombuffer(message[1], dtype=np.float32).reshape(-1, self.channels)
                        f_wav.write(np_data)
                        self.max_amp_seen = max(self.max_amp_seen, float(np.max(np.abs(np_data))))
                        self.chunks_read += 1
                    elif process is not None and not process.is_alive():
                        if _pid_alive(self.pid):
                            self.exit_reason = "failed"
                            raise RuntimeError("Process audio capture stopped unexpectedly.")
                        self.exit_reason = "closed"
                        raise RuntimeError("The application stopped or exited.")
                    else:
                        f_wav.write(silence)
        except Exception as e:
            self.error = str(e)
        finally:
            if receiver is not None:
                try:
                    receiver.close()
                except Exception:
                    pass
            if process is not None:
                process.terminate()
                process.join(timeout=1.0)

    def stop(self):
        self.stop_event.set()
        self.join()


def _pid_alive(pid):
    """Return True if the target process is still running."""
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:
        return True


def _decode_message(message):
    """Normalize a capture-pipe message to ``("data", bytes)`` or ``("error", reason, detail)``."""
    if isinstance(message, tuple) and message:
        if message[0] == "error":
            return ("error", message[1], message[2])
        if message[0] == "data":
            return ("data", message[1])
    return ("data", message)


def _process_tap_worker(pid, sender):
    """Run native process output capture outside the tray process."""
    capture = None
    try:
        import proctap

        capture = proctap.ProcessAudioCapture(int(pid))
        capture.start()
        while True:
            data = capture.read(timeout=0.1)
            if data:
                sender.send(("data", data))
    except Exception as e:
        # Report the real failure back to the parent instead of dying silently,
        # which would otherwise surface as a misleading "application closed".
        try:
            reason = "failed" if _pid_alive(pid) else "closed"
            detail = str(e) or type(e).__name__
            sender.send(("error", reason, detail))
        except Exception:
            pass
    finally:
        if capture is not None:
            try:
                capture.stop()
                capture.close()
            except Exception:
                pass
        sender.close()


class AudioRecorder(threading.Thread):
    """Capture one or more tracks and publish mixed or separate artifacts."""
    VALID_OUTPUT_MODES: ClassVar[set] = {"mixed", "separate_files"}

    def __init__(self, tracks, output_folder, output_format="wav", sample_rate="48000",
                 stereo=False, normalize=False, output_mode="mixed",
                 on_finish_callback=None):
        super().__init__()
        if not tracks:
            raise ValueError("At least one capture track is required.")
        self.tracks = [dict(track) for track in tracks]
        self.output_folder = output_folder
        self.output_format = str(output_format or "wav").strip().lower()
        self.sample_rate_key = str(sample_rate or "48000").strip().lower()
        self.stereo = bool(stereo)
        self.profile = build_output_profile(self.output_format, self.sample_rate_key, self.stereo)
        self.normalize = bool(normalize)
        self.output_mode = str(output_mode or "mixed").strip().lower()
        if self.output_mode not in self.VALID_OUTPUT_MODES:
            raise ValueError(f"Unsupported output mode: {output_mode}")
        self.callback = on_finish_callback
        self.recording = False
        self.stop_event = threading.Event()
        self.error_message = None
        self.final_filepath = None
        self.output_paths = []
        self.result = RecordingResult((), self.output_mode)
        self.temp_files = []
        self.recorders = []
        self.track_labels = []
        self.track_kinds = []

    def _new_temp_file(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        self.temp_files.append(path)
        return path

    def _label_for_track(self, index, track):
        kind = track.get("kind")
        if kind == "app":
            return (track.get("target_name") or f"app_{track.get('target_pid')}").replace(" ", "_")
        if kind == "input":
            return f"input_{index + 1}" if self._kind_count(track.get("kind")) > 1 else "input"
        if kind == "output":
            return f"output_{index + 1}" if self._kind_count(track.get("kind")) > 1 else "output"
        return f"track_{index + 1}"

    def _kind_count(self, kind):
        return sum(1 for track in self.tracks if track.get("kind") == kind)

    def _build_recorders(self, target_sr):
        for index, track in enumerate(self.tracks):
            kind = track.get("kind")
            path = self._new_temp_file()
            label = self._label_for_track(index, track)
            self.track_labels.append(label)
            self.track_kinds.append(kind)
            if kind == "input":
                self.recorders.append(RawRecorder(track.get("input_id"), path, is_output=False,
                                                  samplerate=target_sr, channels=2,
                                                  subtype=self.profile["subtype"]))
            elif kind == "output":
                self.recorders.append(RawRecorder(track.get("speaker_id"), path, is_output=True,
                                                  samplerate=target_sr, channels=2,
                                                  subtype=self.profile["subtype"]))
            elif kind == "app":
                actual_pid = track.get("target_pid")
                try:
                    from process_utils import get_root_pid
                    actual_pid = get_root_pid(actual_pid)
                except Exception as e:
                    print(f"Error resolving root PID: {e}")
                # Per-app capture is fixed at 48 kHz / 2 channels and is
                # resampled to the target sample rate before mixing.
                self.recorders.append(ProcTapRecorder(actual_pid, path))
            else:
                raise ValueError(f"Unsupported track kind: {kind}")

    def run(self):
        self.recording = True
        self.error_message = None
        self.output_paths = []
        self.result = RecordingResult((), self.output_mode)
        try:
            self.recorders = []
            self.temp_files = []
            self.track_labels = []
            self.track_kinds = []
            target_sr = self.profile["sample_rate"]
            self._build_recorders(target_sr)
            for recorder in self.recorders:
                recorder.start()

            while not self.stop_event.is_set():
                for recorder in self.recorders:
                    if not recorder.is_alive():
                        self.stop_event.set()
                        if getattr(recorder, "error", None):
                            if getattr(recorder, "exit_reason", None) == "closed":
                                self.error_message = (
                                    f"Recording stopped because the application closed. ({recorder.error})"
                                )
                            else:
                                self.error_message = f"Audio capture failed: {recorder.error}"
                        break
                self.stop_event.wait(0.05)

            for recorder in self.recorders:
                try:
                    recorder.stop()
                    recorder.join(timeout=10)
                except Exception as e:
                    if not self.error_message:
                        self.error_message = str(e)
                if getattr(recorder, "error", None) and not self.error_message:
                    self.error_message = f"Audio capture failed: {recorder.error}"

            target_sr = self.profile["sample_rate"]
            for path, kind in zip(self.temp_files, self.track_kinds):
                if kind == "app":
                    _resample_wav(path, target_sr)

            os.makedirs(self.output_folder, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            if self.output_mode == "separate_files" and len(self.temp_files) > 1:
                artifacts = []
                for source_path, label in zip(self.temp_files, self.track_labels):
                    final_path = self._unique_output_path(f"Recording_{timestamp}_{label}")
                    self._prepare_single_source(source_path)
                    self._write_final_output(source_path, final_path)
                    artifacts.append(RecordingArtifact(final_path, label))
            else:
                source_wav = self._prepare_source_wav(self.profile["subtype"])
                final_path = self._unique_output_path(f"Recording_{timestamp}")
                self._write_final_output(source_wav, final_path)
                artifacts = [RecordingArtifact(final_path, "mixed" if len(self.temp_files) > 1 else self.track_labels[0])]

            self.result = RecordingResult(tuple(artifacts), self.output_mode)
            self.output_paths = list(self.result.paths)
            self.final_filepath = self.result.primary_path
        except Exception as e:
            if not self.error_message:
                self.error_message = str(e)
            print(f"Error during recording process: {e}")
        finally:
            self.recording = False
            for path in self.temp_files:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            if self.callback:
                self.callback(self.result, self.error_message)

    def _unique_output_path(self, stem):
        candidate = os.path.join(self.output_folder, stem + self.profile["extension"])
        index = 1
        while os.path.exists(candidate):
            candidate = os.path.join(self.output_folder, f"{stem}_{index}{self.profile['extension']}")
            index += 1
        return candidate

    def stop(self):
        self.stop_event.set()

    def _prepare_single_source(self, source_wav):
        if self.normalize:
            self._normalize_audio(source_wav)

    def _prepare_source_wav(self, subtype):
        if len(self.temp_files) > 1:
            for path in self.temp_files:
                self._prepare_single_source(path)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                mixed_wav = tmp.name
            self.temp_files.append(mixed_wav)
            self._mix_audio(self.temp_files[:-1], mixed_wav, subtype, limit_output=self.normalize)
            if self.normalize:
                self._limit_audio(mixed_wav)
            return mixed_wav
        source_wav = self.temp_files[0]
        self._prepare_single_source(source_wav)
        return source_wav

    def _mix_audio(self, files, *args, subtype=None, limit_output=False):
        if isinstance(files, (str, os.PathLike)):
            if len(args) < 2:
                raise TypeError("Legacy _mix_audio requires two input files and an output file.")
            files = [files, args[0]]
            out_file = args[1]
            subtype = subtype or (args[2] if len(args) > 2 else self.profile["subtype"])
        else:
            if not args:
                raise TypeError("_mix_audio requires an output file.")
            out_file = args[0]
            subtype = subtype or (args[1] if len(args) > 1 else self.profile["subtype"])
        if not files:
            raise ValueError("Cannot mix an empty track list.")

        loaded = [sf.read(path, always_2d=True) for path in files]
        sample_rates = {sr for _, sr in loaded}
        channel_counts = {data.shape[1] for data, _ in loaded}
        if len(sample_rates) != 1:
            raise ValueError("Cannot mix audio with different sample rates.")
        if len(channel_counts) != 1:
            raise ValueError("Cannot mix audio with different channel counts.")
        max_len = max(len(data) for data, _ in loaded)
        channels = loaded[0][0].shape[1]
        mixed = np.zeros((max_len, channels), dtype=np.float64)
        for data, _ in loaded:
            mixed[:len(data)] += data
        peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
        if peak > NORMALIZE_LIMIT:
            mixed *= NORMALIZE_LIMIT / peak
        mixed = self._apply_limiter(mixed) if limit_output else mixed
        sf.write(out_file, mixed, loaded[0][1], format="WAV", subtype=subtype)

    def _normalize_audio(self, filepath):
        try:
            info = sf.info(filepath)
            data, sr = sf.read(filepath, always_2d=True)
            sf.write(filepath, self._normalize_audio_data(data), sr, format=info.format, subtype=info.subtype)
        except Exception as e:
            print(f"Normalization failed: {e}")

    def _normalize_audio_data(self, data):
        active = np.abs(data)
        active = active[active >= NORMALIZE_ACTIVE_FLOOR]
        if active.size == 0:
            return self._apply_limiter(data)
        rms = float(np.sqrt(np.mean(active ** 2)))
        percentile = float(np.percentile(active, NORMALIZE_REFERENCE_PERCENTILE))
        reference_level = max(rms, percentile)
        if not np.isfinite(reference_level) or reference_level <= 0:
            return self._apply_limiter(data)
        gain = min(NORMALIZE_TARGET_LEVEL / reference_level, NORMALIZE_MAX_GAIN)
        return self._apply_limiter(data * gain)

    def _limit_audio(self, filepath):
        info = sf.info(filepath)
        data, sr = sf.read(filepath, always_2d=True)
        sf.write(filepath, self._apply_limiter(data), sr, format=info.format, subtype=info.subtype)

    def _apply_limiter(self, data):
        return np.clip(data, -NORMALIZE_LIMIT, NORMALIZE_LIMIT)

    def _write_final_output(self, source_wav, final_filepath):
        if self.profile["encoder"] == "lameenc":
            self._convert_to_mp3(source_wav, final_filepath, self.profile["mp3_bitrate_kbps"])
            return
        with sf.SoundFile(source_wav, mode="r") as source, sf.SoundFile(
            final_filepath,
            mode="w",
            samplerate=source.samplerate,
            channels=self.profile["channels"],
            format=self.profile["format"],
            subtype=self.profile["subtype"],
        ) as target:
            while True:
                data = source.read(65536, always_2d=True)
                if len(data) == 0:
                    break
                data = self._format_output_channels(data)
                target.write(data)

    def _format_output_channels(self, data):
        if self.profile["channels"] == 1 and data.shape[1] > 1:
            return np.mean(data, axis=1, keepdims=True)
        if self.profile["channels"] == 2 and data.shape[1] == 1:
            return np.repeat(data, 2, axis=1)
        return data

    def _convert_to_mp3(self, src_wav, dst_mp3, bitrate_kbps):
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(bitrate_kbps)
        with sf.SoundFile(src_wav, mode="r") as source:
            encoder.set_in_sample_rate(source.samplerate)
            encoder.set_channels(self.profile["channels"])
            encoder.set_quality(2)
            with open(dst_mp3, "wb") as f_mp3:
                while True:
                    data = source.read(65536, always_2d=True)
                    if len(data) == 0:
                        break
                    data = self._format_output_channels(data)
                    pcm_data = (data * 32767).clip(-32768, 32767).astype(np.int16)
                    f_mp3.write(encoder.encode(pcm_data.tobytes()))
                f_mp3.write(encoder.flush())


def _run_off_gui_thread(fn):
    result = {}

    def runner():
        try:
            result["value"] = fn()
        except Exception as e:
            result["error"] = e

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


_LOOPBACK_SUFFIX = re.compile(r"\s*[\(\[]\s*loopback\s*[\)\]]\s*$", re.IGNORECASE)


def _strip_loopback_suffix(name):
    return _LOOPBACK_SUFFIX.sub("", name).strip()


_DEVICE_CACHE = None


def prime_device_cache():
    """Enumerate all audio devices once, before any GUI exists."""
    global _DEVICE_CACHE
    if _DEVICE_CACHE is None:
        _DEVICE_CACHE = {
            "inputs": get_devices(include_output=False),
            "speakers": get_devices(include_output=True),
        }
    return _DEVICE_CACHE


def device_cache():
    return _DEVICE_CACHE


def get_devices(include_output=False):
    def _enumerate():
        p = pyaudio.PyAudio()
        try:
            wasapi_index = _get_wasapi_host_api_index(p)
            if include_output:
                devices = list(p.get_loopback_device_info_generator())
                try:
                    default_info = p.get_default_loopback_device_info()
                    default_name = default_info["name"]
                except Exception:
                    default_name = None
            else:
                devices = [
                    info for info in p.get_device_info_generator()
                    if info["maxInputChannels"] > 0
                    and not info.get("isLoopbackDevice", False)
                    and info["name"] != "Microsoft Sound Mapper - Input"
                    and _is_wasapi_device(info, wasapi_index)
                ]
                try:
                    default_info = p.get_default_input_device_info()
                    default_name = default_info["name"]
                except Exception:
                    default_name = None
            return [
                {
                    "id": info["name"],
                    "name": _strip_loopback_suffix(info["name"]) if include_output else info["name"],
                    "is_default": info["name"] == default_name,
                }
                for info in devices
            ]
        finally:
            p.terminate()

    try:
        return _run_off_gui_thread(_enumerate)
    except Exception as e:
        print(f"Error fetching devices: {e}")
        return []
