import unittest

from audio_recorder import (
    FORMAT_CONFIG,
    SAMPLE_RATE_CONFIG,
    AudioRecorder,
    build_output_profile,
    describe_output_profile,
)


class OutputProfileTests(unittest.TestCase):
    def test_format_config_contains_supported_outputs(self):
        self.assertEqual(set(FORMAT_CONFIG), {"wav", "flac", "mp3"})
        self.assertEqual(FORMAT_CONFIG["wav"]["label"], "WAV")
        self.assertEqual(FORMAT_CONFIG["wav"]["extension"], ".wav")
        self.assertEqual(FORMAT_CONFIG["wav"]["encoder"], "soundfile")
        self.assertEqual(FORMAT_CONFIG["wav"]["format"], "WAV")
        self.assertEqual(FORMAT_CONFIG["flac"]["label"], "FLAC")
        self.assertEqual(FORMAT_CONFIG["flac"]["extension"], ".flac")
        self.assertEqual(FORMAT_CONFIG["flac"]["encoder"], "soundfile")
        self.assertEqual(FORMAT_CONFIG["flac"]["format"], "FLAC")
        self.assertEqual(FORMAT_CONFIG["mp3"]["label"], "MP3")
        self.assertEqual(FORMAT_CONFIG["mp3"]["extension"], ".mp3")
        self.assertEqual(FORMAT_CONFIG["mp3"]["encoder"], "lameenc")

    def test_sample_rate_config_matches_required_mapping(self):
        self.assertEqual(set(SAMPLE_RATE_CONFIG), {"44100", "48000", "96000", "192000"})
        self.assertEqual(SAMPLE_RATE_CONFIG["44100"]["label"], "44.1 kHz")
        self.assertEqual(SAMPLE_RATE_CONFIG["44100"]["sample_rate"], 44100)
        self.assertEqual(SAMPLE_RATE_CONFIG["44100"]["subtype"], "PCM_16")
        self.assertEqual(SAMPLE_RATE_CONFIG["44100"]["mp3_bitrate_kbps"], 128)
        self.assertEqual(SAMPLE_RATE_CONFIG["48000"]["label"], "48 kHz")
        self.assertEqual(SAMPLE_RATE_CONFIG["48000"]["sample_rate"], 48000)
        self.assertEqual(SAMPLE_RATE_CONFIG["48000"]["subtype"], "PCM_16")
        self.assertEqual(SAMPLE_RATE_CONFIG["48000"]["mp3_bitrate_kbps"], 128)
        self.assertEqual(SAMPLE_RATE_CONFIG["96000"]["label"], "96 kHz")
        self.assertEqual(SAMPLE_RATE_CONFIG["96000"]["sample_rate"], 96000)
        self.assertEqual(SAMPLE_RATE_CONFIG["96000"]["subtype"], "PCM_16")
        self.assertEqual(SAMPLE_RATE_CONFIG["96000"]["mp3_bitrate_kbps"], 192)
        self.assertEqual(SAMPLE_RATE_CONFIG["192000"]["label"], "192 kHz")
        self.assertEqual(SAMPLE_RATE_CONFIG["192000"]["sample_rate"], 192000)
        self.assertEqual(SAMPLE_RATE_CONFIG["192000"]["subtype"], "PCM_16")
        self.assertEqual(SAMPLE_RATE_CONFIG["192000"]["mp3_bitrate_kbps"], 256)

    def test_build_output_profile_applies_mono_channels(self):
        profile = build_output_profile(" FLAC ", " 48000 ", stereo=False)

        self.assertEqual(profile["label"], "FLAC")
        self.assertEqual(profile["format_label"], "FLAC")
        self.assertEqual(profile["sample_rate_label"], "48 kHz")
        self.assertEqual(profile["extension"], ".flac")
        self.assertEqual(profile["sample_rate"], 48000)
        self.assertEqual(profile["subtype"], "PCM_16")
        self.assertEqual(profile["channels"], 1)
        self.assertEqual(profile["format_key"], "flac")
        self.assertEqual(profile["sample_rate_key"], "48000")

    def test_build_output_profile_applies_stereo_channels(self):
        profile = build_output_profile("mp3", "96000", stereo=True)

        self.assertEqual(profile["label"], "MP3")
        self.assertEqual(profile["format_label"], "MP3")
        self.assertEqual(profile["sample_rate_label"], "96 kHz")
        self.assertEqual(profile["sample_rate"], 96000)
        self.assertEqual(profile["mp3_bitrate_kbps"], 192)
        self.assertEqual(profile["channels"], 2)
        self.assertEqual(profile["format_key"], "mp3")
        self.assertEqual(profile["sample_rate_key"], "96000")
        self.assertNotIn("format", profile)

    def test_describe_output_profile_uses_english_preview_text(self):
        self.assertEqual(
            describe_output_profile("flac", "48000", stereo=False),
            "FLAC / 48 kHz / mono / PCM_16",
        )
        self.assertEqual(
            describe_output_profile("mp3", "96000", stereo=True),
            "MP3 / 96 kHz / stereo / 192 kbps",
        )
        self.assertEqual(
            describe_output_profile("flac", "44100", stereo=False),
            "FLAC / 44.1 kHz / mono / PCM_16",
        )

    def test_write_final_output_creates_real_flac_file(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            source_wav = os.path.join(temp_dir, "source.wav")
            final_flac = os.path.join(temp_dir, "final.flac")
            data = np.zeros((160, 1), dtype=np.float32)
            sf.write(source_wav, data, 16000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("flac", "48000", stereo=False)
            recorder._write_final_output(source_wav, final_flac)

            info = sf.info(final_flac)
            self.assertEqual(info.format, "FLAC")
            self.assertEqual(info.samplerate, 16000)
            self.assertEqual(info.channels, 1)
            self.assertEqual(info.subtype, "PCM_16")

    def test_write_final_output_creates_wav_file(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            source_wav = os.path.join(temp_dir, "source.wav")
            final_wav = os.path.join(temp_dir, "final.wav")
            data = np.zeros((480, 2), dtype=np.float32)
            sf.write(source_wav, data, 48000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("wav", "48000", stereo=True)
            recorder._write_final_output(source_wav, final_wav)

            info = sf.info(final_wav)
            self.assertEqual(info.format, "WAV")
            self.assertEqual(info.samplerate, 48000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_16")

    def test_write_final_output_uses_profile_mp3_bitrate(self):
        import os
        import tempfile
        from unittest.mock import patch

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            source_wav = os.path.join(temp_dir, "source.wav")
            final_mp3 = os.path.join(temp_dir, "final.mp3")
            data = np.zeros((160, 1), dtype=np.float32)
            sf.write(source_wav, data, 48000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("mp3", "48000", stereo=False)
            with patch.object(recorder, "_convert_to_mp3") as convert_to_mp3:
                recorder._write_final_output(source_wav, final_mp3)

            convert_to_mp3.assert_called_once_with(source_wav, final_mp3, 128)

    def test_convert_to_mp3_configures_encoder_and_writes_interleaved_pcm(self):
        import os
        import tempfile
        from unittest.mock import patch

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            source_wav = os.path.join(temp_dir, "source.wav")
            final_mp3 = os.path.join(temp_dir, "final.mp3")
            data = np.array([[0.5, -0.5], [0.25, -0.25]], dtype=np.float32)
            sf.write(source_wav, data, 48000, format="WAV", subtype="FLOAT")

            recorder = self._make_recorder("mp3", "96000", stereo=True)
            with patch("audio_recorder.lameenc.Encoder") as encoder_cls:
                encoder = encoder_cls.return_value
                encoder.encode.return_value = b"encoded"
                encoder.flush.return_value = b"flush"

                recorder._convert_to_mp3(source_wav, final_mp3, 192)

            encoder.set_bit_rate.assert_called_once_with(192)
            encoder.set_in_sample_rate.assert_called_once_with(48000)
            encoder.set_channels.assert_called_once_with(2)
            encoder.set_quality.assert_called_once_with(2)
            pcm_arg = encoder.encode.call_args.args[0]
            expected_pcm = (data * 32767).clip(-32768, 32767).astype(np.int16)
            self.assertEqual(
                np.frombuffer(pcm_arg, dtype=np.int16).tolist(),
                expected_pcm.reshape(-1).tolist(),
            )
            with open(final_mp3, "rb") as f_mp3:
                self.assertEqual(f_mp3.read(), b"encodedflush")

    def test_mix_audio_preserves_wav_subtype(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            file1 = os.path.join(temp_dir, "file1.wav")
            file2 = os.path.join(temp_dir, "file2.wav")
            out_file = os.path.join(temp_dir, "mixed.wav")
            data1 = np.array([[0.25, -0.25], [0.5, -0.5]], dtype=np.float32)
            data2 = np.array([[0.25, 0.25], [-0.5, 0.5]], dtype=np.float32)
            sf.write(file1, data1, 48000, format="WAV", subtype="PCM_16")
            sf.write(file2, data2, 48000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("wav", "48000", stereo=True)
            recorder._mix_audio(file1, file2, out_file, "PCM_16")

            info = sf.info(out_file)
            self.assertEqual(info.format, "WAV")
            self.assertEqual(info.samplerate, 48000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_16")

    def test_mix_audio_rejects_different_sample_rates(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            file1 = os.path.join(temp_dir, "file1.wav")
            file2 = os.path.join(temp_dir, "file2.wav")
            out_file = os.path.join(temp_dir, "mixed.wav")
            sf.write(file1, np.zeros((2, 2), dtype=np.float32), 48000, format="WAV", subtype="PCM_16")
            sf.write(file2, np.zeros((2, 2), dtype=np.float32), 16000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("wav", "48000", stereo=True)
            with self.assertRaisesRegex(ValueError, "Cannot mix audio with different sample rates."):
                recorder._mix_audio(file1, file2, out_file, "PCM_16")

    def test_mix_audio_rejects_different_channel_counts(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            file1 = os.path.join(temp_dir, "file1.wav")
            file2 = os.path.join(temp_dir, "file2.wav")
            out_file = os.path.join(temp_dir, "mixed.wav")
            sf.write(file1, np.zeros((2, 2), dtype=np.float32), 48000, format="WAV", subtype="PCM_16")
            sf.write(file2, np.zeros((2, 1), dtype=np.float32), 48000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("wav", "48000", stereo=True)
            with self.assertRaisesRegex(ValueError, "Cannot mix audio with different channel counts."):
                recorder._mix_audio(file1, file2, out_file, "PCM_16")

    def test_normalize_audio_preserves_format_and_subtype(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "source.flac")
            data = np.array([[0.25], [-0.5], [0.75]], dtype=np.float32)
            sf.write(filepath, data, 48000, format="FLAC", subtype="PCM_24")

            recorder = self._make_recorder("flac", "48000", stereo=False)
            recorder._normalize_audio(filepath)

            info = sf.info(filepath)
            self.assertEqual(info.format, "FLAC")
            self.assertEqual(info.subtype, "PCM_24")

    def test_normalize_audio_raises_main_voice_despite_single_spike(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "source.wav")
            data = np.full((16000, 1), 0.02, dtype=np.float32)
            data[4000, 0] = 1.0
            sf.write(filepath, data, 16000, format="WAV", subtype="FLOAT")

            recorder = self._make_recorder("wav", "48000", stereo=False)
            recorder._normalize_audio(filepath)

            normalized, _ = sf.read(filepath, always_2d=True)
            body = np.delete(normalized[:, 0], 4000)
            self.assertGreater(np.median(np.abs(body)), 0.10)
            self.assertLessEqual(np.max(np.abs(normalized)), 0.9801)

    def test_prepare_source_wav_normalizes_both_sources_before_mixing(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = os.path.join(temp_dir, "input.wav")
            output_file = os.path.join(temp_dir, "output.wav")
            input_sig = np.full((16000, 1), 0.02, dtype=np.float32)
            input_sig[4000, 0] = 1.0
            output_sig = np.full((16000, 1), 0.01, dtype=np.float32)
            sf.write(input_file, input_sig, 16000, format="WAV", subtype="FLOAT")
            sf.write(output_file, output_sig, 16000, format="WAV", subtype="FLOAT")

            recorder = self._make_recorder("wav", "48000", stereo=False)
            recorder.normalize = True
            recorder.temp_files = [input_file, output_file]

            mixed_file = recorder._prepare_source_wav("FLOAT")
            mixed, _ = sf.read(mixed_file, always_2d=True)

            body = np.delete(mixed[:, 0], 4000)
            self.assertGreater(np.median(np.abs(body)), 0.15)
            self.assertLessEqual(np.max(np.abs(mixed)), 0.9801)
            self.assertIn(mixed_file, recorder.temp_files)

    def test_normalize_audio_preserves_stereo_channel_balance(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "source.wav")
            data = np.tile(np.array([[0.04, 0.02]], dtype=np.float32), (16000, 1))
            data[4000] = [1.0, 0.5]
            sf.write(filepath, data, 16000, format="WAV", subtype="FLOAT")

            recorder = self._make_recorder("wav", "48000", stereo=True)
            recorder._normalize_audio(filepath)

            normalized, _ = sf.read(filepath, always_2d=True)
            self.assertAlmostEqual(
                normalized[1000, 0] / normalized[1000, 1],
                2.0,
                places=5,
            )

    def test_invalid_profile_keys_raise_value_error(self):
        with self.assertRaises(ValueError):
            build_output_profile("ogg", "48000", stereo=False)
        with self.assertRaises(ValueError):
            build_output_profile("flac", "studio", stereo=False)

    def _make_recorder(self, fmt, sample_rate, stereo, tracks=None):
        if tracks is None:
            tracks = [{"kind": "input", "input_id": "input1"}]
        return AudioRecorder(
            tracks=tracks,
            output_folder=".",
            output_format=fmt,
            sample_rate=sample_rate,
            stereo=stereo,
        )


if __name__ == "__main__":
    unittest.main()
