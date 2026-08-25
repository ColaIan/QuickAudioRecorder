import pytest
from unittest.mock import patch, MagicMock
import os
import threading

import audio_recorder

@patch('audio_recorder.sc')
@patch('audio_recorder.ProcTapRecorder')
def test_audio_recorder_per_app_output(mock_proctap_recorder, mock_sc, tmp_path):
    # Test recording app output only
    out_dir = str(tmp_path)
    recorder = audio_recorder.AudioRecorder(
        mic_id="default",
        source_mode="loopback",
        output_folder=out_dir,
        output_format="wav",
        target_pid=1234
    )
    
    mock_instance = MagicMock()
    mock_proctap_recorder.return_value = mock_instance
    
    # We mock stop_event.wait to just stop immediately
    recorder.stop_event.wait = lambda: None
    
    recorder.run()
    
    # Assert ProcTapRecorder was used
    mock_proctap_recorder.assert_called_once()
    args, kwargs = mock_proctap_recorder.call_args
    assert args[0] == 1234  # PID
    assert len(recorder.recorders) == 1
    assert recorder.recorders[0] == mock_instance

@patch('audio_recorder.sc')
@patch('audio_recorder.ProcTapRecorder')
@patch('audio_recorder.RawRecorder')
def test_audio_recorder_per_app_both(mock_raw_recorder, mock_proctap_recorder, mock_sc, tmp_path):
    # Test recording app output + mic
    out_dir = str(tmp_path)
    recorder = audio_recorder.AudioRecorder(
        mic_id="mic1",
        source_mode="both",
        output_folder=out_dir,
        output_format="wav",
        target_pid=5678
    )
    
    mock_mic_instance = MagicMock()
    mock_raw_recorder.return_value = mock_mic_instance
    
    mock_app_instance = MagicMock()
    mock_proctap_recorder.return_value = mock_app_instance
    
    # Mock sc to return a mock mic
    mock_sc.get_microphone.return_value = MagicMock()
    
    # We mock stop_event.wait to just stop immediately
    recorder.stop_event.wait = lambda: None
    
    # We need to mock mix_audio so it doesn't try to read fake wavs
    recorder._mix_audio = MagicMock()
    
    recorder.run()
    
    # Both recorders should be initialized
    mock_proctap_recorder.assert_called_once()
    mock_raw_recorder.assert_called_once()
    
    assert args_pid_check(mock_proctap_recorder, 5678)

def args_pid_check(mock_call, expected_pid):
    args, kwargs = mock_call.call_args
    return args[0] == expected_pid

@patch('audio_recorder.sc')
@patch('audio_recorder.ProcTapRecorder')
def test_audio_recorder_app_crash(mock_proctap_recorder, mock_sc, tmp_path):
    # Test that AudioRecorder finishes gracefully if ProcTapRecorder dies
    out_dir = str(tmp_path)
    recorder = audio_recorder.AudioRecorder(
        mic_id="default",
        source_mode="loopback",
        output_folder=out_dir,
        output_format="wav",
        target_pid=9999
    )
    
    mock_instance = MagicMock()
    # Simulate the thread dying immediately with an error
    mock_instance.is_alive.return_value = False
    mock_instance.error = "Process terminated"
    mock_proctap_recorder.return_value = mock_instance
    
    # Run should detect the dead thread and break out of its wait loop
    recorder.run()
    
    # Ensure it still tried to mix/finalize what it had
    assert recorder.final_filepath is not None
    # Ensure error message reflects the crash
    assert "terminated" in str(recorder.error_message).lower() or "closed" in str(recorder.error_message).lower()

@patch('audio_recorder.sc')
def test_audio_recorder_custom_speaker(mock_sc, tmp_path):
    # Test that providing a speaker_id uses that specific speaker for loopback instead of default
    out_dir = str(tmp_path)
    
    mock_speaker1 = MagicMock()
    mock_speaker1.id = "speaker1_id"
    mock_speaker2 = MagicMock()
    mock_speaker2.id = "speaker2_id"
    mock_speaker2.name = "Speaker 2 Name"
    
    mock_loopback_mic = MagicMock()
    mock_loopback_mic.name = "Speaker 2 Name Loopback"
    mock_loopback_mic.isloopback = True
    
    mock_sc.all_speakers.return_value = [mock_speaker1, mock_speaker2]
    mock_sc.default_speaker.return_value = mock_speaker1
    mock_sc.all_microphones.return_value = [mock_loopback_mic]
    
    recorder = audio_recorder.AudioRecorder(
        mic_id="default",
        source_mode="loopback",
        output_folder=out_dir,
        output_format="wav",
        speaker_id="speaker2_id"
    )
    
    dev = recorder._get_device(is_loopback=True)
    assert dev == mock_loopback_mic
    mock_sc.default_speaker.assert_not_called()
