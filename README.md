# Quick Audio Recorder

**Quick Audio Recorder** is a minimalist, yet powerful tool for Windows to quickly record audio from your microphone, system audio (loopback), or both simultaneously.  
It sits quietly in your system tray and is always ready with a single click or global hotkey.

<img width="393" height="156" alt="image" src="https://github.com/user-attachments/assets/7e3bfcaf-6f58-4404-b85a-4ba0b6fea085" />

## Features




<img width="575" height="724" alt="image" src="https://github.com/user-attachments/assets/b35131bc-1ff8-41e1-87b5-1e472f9da981" />


-   **Capture Targets:**
    -   🖥️ **Hardware Output Device:** Record system-wide audio directly from your chosen physical speakers.
    -   🎯 **Specific Application:** Isolate and record audio *only* from a selected application (e.g., Discord or Firefox) without capturing background system noises, powered by WASAPI Process Loopback.
-   **Modes:**
    -   🎤 **Microphone:** Record your voice.
    -   🔊 **Loopback:** Record what you hear (or what the app is playing).
    -   🎙️+🔊 **Both:** Record both tracks simultaneously (mixed into one file).
-   **Post-Processing:**
    -   **Auto-Normalize:** Automatically adjusts volume to optimal levels after recording.
    -   **Clipboard Integration:** Automatically copies the file (or file path) to your clipboard.
    -   **Clean Workflow:** Option to move the file to a temp folder and copy it, keeping your desktop clean.
-   **Control:**
    -   **Global Hotkeys:** Start/Stop recording from anywhere (e.g., `Ctrl+Alt+R`).
    -   **Tray Icon:** Left-click to toggle recording immediately.
    -   **Visual Feedback:** Tray icon changes color when recording.

## Installation

1.  Go to the [Releases](https://github.com/lukmay/QuickAudioRecorder/releases) page.
2.  Download `QuickAudioRecorder.exe`.
3.  Run it! (No installation required).

## Usage

1.  The **Settings** window opens immediately upon launch.
2.  Choose your **Capture Target** (Hardware Output or Specific Application).
3.  Select your **Microphone** and **Output Folder**.
4.  Set your **Hotkeys** and **Tray Icon Behavior** (optional).
5.  Close the settings window to minimize to the tray. 
6.  **Left-click** the tray icon or use your configured hotkey to start recording. Click again to stop.

## Development

### Requirements
-   Python 3.12+
-   Install dependencies: `pip install -r requirements.txt`
-   Core libraries include `soundcard`, `soundfile`, `proctap` (for WASAPI loopback isolation), `psutil`, and `PyQt6`.

### Testing
This project follows Test-Driven Development (TDD) for core audio logic. To run the test suite:
```bash
pytest tests/
```

### Build from Source
To create the standalone executable:
```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name QuickAudioRecorder main.py
```
