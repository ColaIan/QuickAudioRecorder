import unittest

from audio_recorder import (
    FORMAT_CONFIG,
    QUALITY_CONFIG,
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

    def test_quality_config_matches_required_mapping(self):
        self.assertEqual(set(QUALITY_CONFIG), {"balanced", "high"})
        self.assertEqual(QUALITY_CONFIG["balanced"]["label"], "Balanced")
        self.assertEqual(QUALITY_CONFIG["balanced"]["sample_rate"], 16000)
        self.assertEqual(QUALITY_CONFIG["balanced"]["subtype"], "PCM_16")
        self.assertEqual(QUALITY_CONFIG["balanced"]["mp3_bitrate_kbps"], 64)
        self.assertEqual(QUALITY_CONFIG["high"]["label"], "High Quality")
        self.assertEqual(QUALITY_CONFIG["high"]["sample_rate"], 48000)
        self.assertEqual(QUALITY_CONFIG["high"]["subtype"], "PCM_24")
        self.assertEqual(QUALITY_CONFIG["high"]["mp3_bitrate_kbps"], 128)

    def test_build_output_profile_applies_mono_channels(self):
        profile = build_output_profile(" FLAC ", " BALANCED ", stereo=False)

        self.assertEqual(profile["label"], "FLAC")
        self.assertEqual(profile["format_label"], "FLAC")
        self.assertEqual(profile["quality_label"], "Balanced")
        self.assertEqual(profile["extension"], ".flac")
        self.assertEqual(profile["sample_rate"], 16000)
        self.assertEqual(profile["subtype"], "PCM_16")
        self.assertEqual(profile["channels"], 1)
        self.assertEqual(profile["format_key"], "flac")
        self.assertEqual(profile["quality_key"], "balanced")

    def test_build_output_profile_applies_stereo_channels(self):
        profile = build_output_profile("mp3", "high", stereo=True)

        self.assertEqual(profile["label"], "MP3")
        self.assertEqual(profile["format_label"], "MP3")
        self.assertEqual(profile["quality_label"], "High Quality")
        self.assertEqual(profile["sample_rate"], 48000)
        self.assertEqual(profile["mp3_bitrate_kbps"], 128)
        self.assertEqual(profile["channels"], 2)
        self.assertEqual(profile["format_key"], "mp3")
        self.assertEqual(profile["quality_key"], "high")
        self.assertNotIn("format", profile)

    def test_describe_output_profile_uses_english_preview_text(self):
        self.assertEqual(
            describe_output_profile("flac", "balanced", stereo=False),
            "FLAC / 16 kHz / mono / PCM_16",
        )
        self.assertEqual(
            describe_output_profile("mp3", "high", stereo=True),
            "MP3 / 48 kHz / stereo / 128 kbps",
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

            recorder = self._make_recorder("flac", "balanced", stereo=False)
            recorder._write_final_output(source_wav, final_flac)

            info = sf.info(final_flac)
            self.assertEqual(info.format, "FLAC")
            self.assertEqual(info.samplerate, 16000)
            self.assertEqual(info.channels, 1)
            self.assertEqual(info.subtype, "PCM_16")

    def test_write_final_output_creates_high_quality_wav_file(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            source_wav = os.path.join(temp_dir, "source.wav")
            final_wav = os.path.join(temp_dir, "final.wav")
            data = np.zeros((480, 2), dtype=np.float32)
            sf.write(source_wav, data, 48000, format="WAV", subtype="PCM_24")

            recorder = self._make_recorder("wav", "high", stereo=True)
            recorder._write_final_output(source_wav, final_wav)

            info = sf.info(final_wav)
            self.assertEqual(info.format, "WAV")
            self.assertEqual(info.samplerate, 48000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_24")

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
            sf.write(source_wav, data, 16000, format="WAV", subtype="PCM_16")

            recorder = self._make_recorder("mp3", "balanced", stereo=False)
            with patch.object(recorder, "_convert_to_mp3") as convert_to_mp3:
                recorder._write_final_output(source_wav, final_mp3)

            convert_to_mp3.assert_called_once_with(source_wav, final_mp3, 64)

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

            recorder = self._make_recorder("mp3", "high", stereo=True)
            with patch("audio_recorder.lameenc.Encoder") as encoder_cls:
                encoder = encoder_cls.return_value
                encoder.encode.return_value = b"encoded"
                encoder.flush.return_value = b"flush"

                recorder._convert_to_mp3(source_wav, final_mp3, 128)

            encoder.set_bit_rate.assert_called_once_with(128)
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
            sf.write(file1, data1, 48000, format="WAV", subtype="PCM_24")
            sf.write(file2, data2, 48000, format="WAV", subtype="PCM_24")

            recorder = self._make_recorder("wav", "high", stereo=True)
            recorder._mix_audio(file1, file2, out_file, "PCM_24")

            info = sf.info(out_file)
            self.assertEqual(info.format, "WAV")
            self.assertEqual(info.samplerate, 48000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_24")

    def test_mix_audio_rejects_different_sample_rates(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            file1 = os.path.join(temp_dir, "file1.wav")
            file2 = os.path.join(temp_dir, "file2.wav")
            out_file = os.path.join(temp_dir, "mixed.wav")
            sf.write(file1, np.zeros((2, 2), dtype=np.float32), 48000, format="WAV", subtype="PCM_24")
            sf.write(file2, np.zeros((2, 2), dtype=np.float32), 16000, format="WAV", subtype="PCM_24")

            recorder = self._make_recorder("wav", "high", stereo=True)
            with self.assertRaisesRegex(ValueError, "Cannot mix audio with different sample rates."):
                recorder._mix_audio(file1, file2, out_file, "PCM_24")

    def test_mix_audio_rejects_different_channel_counts(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            file1 = os.path.join(temp_dir, "file1.wav")
            file2 = os.path.join(temp_dir, "file2.wav")
            out_file = os.path.join(temp_dir, "mixed.wav")
            sf.write(file1, np.zeros((2, 2), dtype=np.float32), 48000, format="WAV", subtype="PCM_24")
            sf.write(file2, np.zeros((2, 1), dtype=np.float32), 48000, format="WAV", subtype="PCM_24")

            recorder = self._make_recorder("wav", "high", stereo=True)
            with self.assertRaisesRegex(ValueError, "Cannot mix audio with different channel counts."):
                recorder._mix_audio(file1, file2, out_file, "PCM_24")

    def test_normalize_audio_preserves_format_and_subtype(self):
        import os
        import tempfile

        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "source.flac")
            data = np.array([[0.25], [-0.5], [0.75]], dtype=np.float32)
            sf.write(filepath, data, 48000, format="FLAC", subtype="PCM_24")

            recorder = self._make_recorder("flac", "high", stereo=False)
            recorder._normalize_audio(filepath)

            info = sf.info(filepath)
            self.assertEqual(info.format, "FLAC")
            self.assertEqual(info.subtype, "PCM_24")

    def test_invalid_profile_keys_raise_value_error(self):
        with self.assertRaises(ValueError):
            build_output_profile("ogg", "balanced", stereo=False)
        with self.assertRaises(ValueError):
            build_output_profile("flac", "studio", stereo=False)

    def _make_recorder(self, fmt, quality, stereo):
        from audio_recorder import AudioRecorder

        return AudioRecorder(
            mic_id="mic1",
            source_mode="mic",
            output_folder=".",
            output_format=fmt,
            quality=quality,
            stereo=stereo,
        )


if __name__ == "__main__":
    unittest.main()
