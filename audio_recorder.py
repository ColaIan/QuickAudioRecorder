import soundcard as sc
import soundfile as sf
import threading
import time
import os
import lameenc
import numpy as np
import tempfile

FORMAT_CONFIG = {
    "wav": {
        "label": "WAV",
        "extension": ".wav",
        "encoder": "soundfile",
        "format": "WAV",
    },
    "flac": {
        "label": "FLAC",
        "extension": ".flac",
        "encoder": "soundfile",
        "format": "FLAC",
    },
    "mp3": {
        "label": "MP3",
        "extension": ".mp3",
        "encoder": "lameenc",
    },
}

QUALITY_CONFIG = {
    "balanced": {
        "label": "Balanced",
        "sample_rate": 16000,
        "subtype": "PCM_16",
        "mp3_bitrate_kbps": 64,
    },
    "high": {
        "label": "High Quality",
        "sample_rate": 48000,
        "subtype": "PCM_24",
        "mp3_bitrate_kbps": 128,
    },
}


def build_output_profile(fmt, quality, stereo):
    fmt_key = str(fmt or "").strip().lower()
    quality_key = str(quality or "").strip().lower()

    if fmt_key not in FORMAT_CONFIG:
        raise ValueError(f"Unsupported output format: {fmt}")
    if quality_key not in QUALITY_CONFIG:
        raise ValueError(f"Unsupported output quality: {quality}")

    format_config = FORMAT_CONFIG[fmt_key]
    quality_config = QUALITY_CONFIG[quality_key]
    channels = 2 if stereo else 1
    return {
        **quality_config,
        **format_config,
        "label": format_config["label"],
        "format_label": format_config["label"],
        "quality_label": quality_config["label"],
        "format_key": fmt_key,
        "quality_key": quality_key,
        "channels": channels,
    }


def describe_output_profile(fmt, quality, stereo):
    profile = build_output_profile(fmt, quality, stereo)
    rate_khz = profile["sample_rate"] // 1000
    channels = "stereo" if profile["channels"] == 2 else "mono"
    encoding = (
        f"{profile['mp3_bitrate_kbps']} kbps"
        if profile["encoder"] == "lameenc"
        else profile["subtype"]
    )
    return f"{profile['format_label']} / {rate_khz} kHz / {channels} / {encoding}"


class RawRecorder(threading.Thread):
    """
    Helper thread to record a single device to a WAV file.
    """
    def __init__(self, device, filepath, samplerate=44100, channels=2, subtype="PCM_16"):
        super().__init__()
        self.device = device
        self.filepath = filepath
        self.samplerate = samplerate
        self.channels = channels
        self.subtype = subtype
        self.stop_event = threading.Event()
        self.error = None

    def run(self):
        try:
            with sf.SoundFile(
                self.filepath,
                mode="w",
                samplerate=self.samplerate,
                channels=self.channels,
                format="WAV",
                subtype=self.subtype,
            ) as f_wav:
                with self.device.recorder(samplerate=self.samplerate, channels=self.channels) as mic:
                    while not self.stop_event.is_set():
                        data = mic.record(numframes=2048)
                        f_wav.write(data)
        except Exception as e:
            self.error = str(e)

    def stop(self):
        self.stop_event.set()
        self.join()

class ProcTapRecorder(threading.Thread):
    """
    Helper thread to record a specific application's audio to a WAV file using ProcTap.
    """
    def __init__(self, pid, filepath):
        super().__init__()
        self.pid = pid
        self.filepath = filepath
        self.samplerate = 48000
        self.channels = 2
        self.stop_event = threading.Event()
        self.error = None
        self.max_amp_seen = 0.0
        self.chunks_read = 0

    def run(self):
        try:
            import proctap
            import soundfile as sf
            import numpy as np
            
            print(f"[ProcTapRecorder] Starting capture for PID {self.pid}...")
            capture = proctap.ProcessAudioCapture(self.pid)
            capture.start()
            
            with sf.SoundFile(self.filepath, mode='w', samplerate=self.samplerate, channels=self.channels) as f_wav:
                while not self.stop_event.is_set():
                    data = capture.read(timeout=0.1)
                    if data:
                        np_data = np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels)
                        f_wav.write(np_data)
                        
                        # Debugging amplitude
                        amp = np.max(np.abs(np_data))
                        if amp > self.max_amp_seen:
                            self.max_amp_seen = amp
                        self.chunks_read += 1
                        
            capture.stop()
            capture.close()
            print(f"[ProcTapRecorder] Finished PID {self.pid}. Chunks: {self.chunks_read}, Max Amp: {self.max_amp_seen:.4f}")
            if self.max_amp_seen == 0.0 and self.chunks_read > 0:
                print(f"[ProcTapRecorder] WARNING: All captured chunks were perfect silence (0.0). Application may be bypassing WASAPI loopback.")
        except Exception as e:
            print(f"[ProcTapRecorder] Error: {e}")
            self.error = str(e)

    def stop(self):
        self.stop_event.set()
        self.join()

class AudioRecorder(threading.Thread):
    """
    Orchestrates recording from Microphone, Loopback, or Both.
    """
    def __init__(self, mic_id, source_mode, output_folder, output_format="mp3", 
                 normalize=False, target_pid=None, speaker_id=None, on_finish_callback=None):
        super().__init__()
        self.mic_id = mic_id
        self.source_mode = source_mode # "mic", "loopback", "both"
        self.output_folder = output_folder
        self.output_format = str(output_format or "flac").strip().lower()
        self.quality = str(quality or "balanced").strip().lower()
        self.stereo = bool(stereo)
        self.profile = build_output_profile(self.output_format, self.quality, self.stereo)
        self.normalize = normalize
        self.target_pid = target_pid
        self.speaker_id = speaker_id
        self.callback = on_finish_callback
        
        self.recording = False
        self.stop_event = threading.Event()
        self.error_message = None
        self.final_filepath = None
        
        # Temp files
        self.temp_files = []
        self.recorders = []

    def _get_device(self, is_loopback=False):
        if is_loopback:
            target_speaker_name = None
            if self.speaker_id:
                for speaker in sc.all_speakers():
                    if speaker.id == self.speaker_id:
                        target_speaker_name = speaker.name
                        break
            
            if target_speaker_name is None:
                target_speaker_name = sc.default_speaker().name
                
            mics = sc.all_microphones(include_loopback=True)
            # Try exact name match
            loopback_mic = next((m for m in mics if m.name == target_speaker_name and m.isloopback), None)
            # Try fuzzy match
            if not loopback_mic:
                loopback_mic = next((m for m in mics if target_speaker_name in m.name and m.isloopback), None)
            
            if not loopback_mic:
                raise Exception(f"Could not detect System Audio loopback device for '{target_speaker_name}'.")
            return loopback_mic
        else:
            return sc.get_microphone(self.mic_id, include_loopback=False)

    def run(self):
        self.recording = True
        self.error_message = None
        self.temp_files = []
        self.recorders = []
        
        try:
            samplerate = self.profile["sample_rate"]
            channels = self.profile["channels"]
            subtype = self.profile["subtype"]

            # 1. Setup Recorders
            is_per_app = self.target_pid is not None
            
            # Use root PID for the target to ensure we capture the whole tree (browser sandboxes)
            actual_pid = self.target_pid
            if is_per_app:
                try:
                    from process_utils import get_root_pid
                    actual_pid = get_root_pid(self.target_pid)
                    print(f"Targeting root PID {actual_pid} (derived from selected {self.target_pid})")
                except Exception as e:
                    print(f"Error resolving root PID: {e}")
            
            # If using ProcTap, its fixed sample rate is 48000. 
            # We must match this for hardware mics to avoid mixing different sample rates.
            target_sr = 48000 if is_per_app else 44100
            
            if self.source_mode == "both":
                # Need two recorders
                dev_mic = self._get_device(is_loopback=False)
                
                t1 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                t2 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                self.temp_files = [t1, t2]
                
                self.recorders.append(RawRecorder(dev_mic, t1, samplerate=target_sr))
                
                if is_per_app:
                    self.recorders.append(ProcTapRecorder(actual_pid, t2))
                else:
                    dev_loop = self._get_device(is_loopback=True)
                    self.recorders.append(RawRecorder(dev_loop, t2, samplerate=target_sr))
                
            elif self.source_mode == "loopback":
                t1 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                self.temp_files = [t1]
                
                if is_per_app:
                    self.recorders.append(ProcTapRecorder(actual_pid, t1))
                else:
                    dev = self._get_device(is_loopback=True)
                    self.recorders.append(RawRecorder(dev, t1, samplerate=target_sr))
                
            else: # mic
                dev = self._get_device(is_loopback=False)
                t1 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                self.temp_files = [t1]
                self.recorders.append(RawRecorder(dev, t1, samplerate=target_sr))

            print(f"Starting recording mode: {self.source_mode}")

            # 2. Start Recording
            for r in self.recorders:
                r.start()
            
            # Wait for stop signal or for a recorder to crash
            while not self.stop_event.is_set():
                for r in self.recorders:
                    if not r.is_alive():
                        # A recorder thread died prematurely
                        self.stop_event.set()
                        if r.error:
                            self.error_message = f"Recording stopped because the application closed. ({r.error})"
                        break
                self.stop_event.wait(0.5)
            
            # 3. Stop Recording
            for r in self.recorders:
                r.stop()
                if r.error and not self.error_message:
                    self.error_message = f"Recording stopped because the application closed. ({r.error})"

            # 4. Mix/Process
            if len(self.temp_files) == 2:
                mixed_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
                self._mix_audio(self.temp_files[0], self.temp_files[1], mixed_wav, subtype)
                # Use mixed file as source for next steps
                source_wav = mixed_wav
                self.temp_files.append(mixed_wav) # Mark for cleanup
            else:
                source_wav = self.temp_files[0]

            # 5. Normalization
            if self.normalize:
                self._normalize_audio(source_wav)
            
            # 6. Finalize
            if not os.path.exists(self.output_folder):
                os.makedirs(self.output_folder)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"Recording_{timestamp}{self.profile['extension']}"
            self.final_filepath = os.path.join(self.output_folder, filename)
            self._write_final_output(source_wav, self.final_filepath)

        except Exception as e:
            self.error_message = str(e)
            print(f"Error during recording process: {e}")
        finally:
            self.recording = False
            # Clean up all temp files
            for t in self.temp_files:
                if os.path.exists(t):
                    try:
                        os.remove(t)
                    except: pass
                
            if self.callback:
                self.callback(self.final_filepath, self.error_message)

    def stop(self):
        self.stop_event.set()

    def _mix_audio(self, file1, file2, out_file, subtype):
        d1, sr1 = sf.read(file1, always_2d=True)
        d2, sr2 = sf.read(file2, always_2d=True)

        if sr1 != sr2:
            raise ValueError("Cannot mix audio with different sample rates.")
        if d1.shape[1] != d2.shape[1]:
            raise ValueError("Cannot mix audio with different channel counts.")
        
        # Ensure same length
        max_len = max(len(d1), len(d2))
        
        # Pad d1
        if len(d1) < max_len:
            pad_width = max_len - len(d1)
            # handle mono/stereo padding
            shape = (pad_width, d1.shape[1])
            d1 = np.concatenate((d1, np.zeros(shape, dtype=d1.dtype)))
            
        # Pad d2
        if len(d2) < max_len:
            pad_width = max_len - len(d2)
            shape = (pad_width, d2.shape[1])
            d2 = np.concatenate((d2, np.zeros(shape, dtype=d2.dtype)))
            
        # Mix (Sum)
        mixed = d1 + d2
        # Clip
        mixed = np.clip(mixed, -1.0, 1.0)
        
        sf.write(out_file, mixed, sr1, format="WAV", subtype=subtype)

    def _normalize_audio(self, filepath):
        try:
            info = sf.info(filepath)
            data, sr = sf.read(filepath)
            max_val = np.max(np.abs(data))
            if max_val > 0:
                target_peak = 0.99 
                factor = target_peak / max_val
                data = data * factor
                sf.write(filepath, data, sr, format=info.format, subtype=info.subtype)
        except Exception as e:
            print(f"Normalization failed: {e}")

    def _write_final_output(self, source_wav, final_filepath):
        if self.profile["encoder"] == "lameenc":
            self._convert_to_mp3(source_wav, final_filepath, self.profile["mp3_bitrate_kbps"])
            return

        data, sr = sf.read(source_wav, always_2d=True)
        if self.profile["channels"] == 1 and data.shape[1] > 1:
            data = np.mean(data, axis=1, keepdims=True)

        sf.write(
            final_filepath,
            data,
            sr,
            format=self.profile["format"],
            subtype=self.profile["subtype"],
        )

    def _convert_to_mp3(self, src_wav, dst_mp3, bitrate_kbps):
        data, sr = sf.read(src_wav, always_2d=True)
        if self.profile["channels"] == 1 and data.shape[1] > 1:
            data = np.mean(data, axis=1, keepdims=True)
        channels = data.shape[1]
        
        pcm_data = (data * 32767).clip(-32768, 32767).astype(np.int16)
        
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(bitrate_kbps)
        encoder.set_in_sample_rate(sr)
        encoder.set_channels(channels)
        encoder.set_quality(2)
        
        mp3_data = encoder.encode(pcm_data.tobytes())
        mp3_data += encoder.flush()
        
        with open(dst_mp3, "wb") as f_mp3:
            f_mp3.write(mp3_data)

def get_devices(include_loopback=False):
    try:
        devices = sc.all_microphones(include_loopback=include_loopback)
        return [{"id": d.id, "name": d.name} for d in devices]
    except Exception as e:
        print(f"Error fetching devices: {e}")
        return []
