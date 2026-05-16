# Quick Audio Recorder

**Quick Audio Recorder** is a minimalist, yet powerful tool for Windows to quickly record audio from your microphone, system audio (loopback), or both simultaneously.  
It sits quietly in your system tray and is always ready with a single click or global hotkey.

<img width="393" height="156" alt="image" src="https://github.com/user-attachments/assets/7e3bfcaf-6f58-4404-b85a-4ba0b6fea085" />

## Features




<img width="575" height="724" alt="image" src="https://github.com/user-attachments/assets/b35131bc-1ff8-41e1-87b5-1e472f9da981" />


-   **Modes:**
    -   🎤 **Microphone:** Record your voice.
    -   🔊 **System Audio:** Record what you hear (Loopback).
    -   🎙️+🔊 **Both:** Record both tracks simultaneously (mixed).
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

1.  **Right-click** the tray icon to open **Settings**.
2.  Select your **Microphone** and **Output Folder**.
3.  Set your **Hotkeys** (optional).
4.  **Left-click** the tray icon or use a hotkey to start recording.
5.  Click again to stop. The file is saved and ready to use!

## Development

### Requirements
-   Python 3.12+
-   `pip install -r requirements.txt`

### Build from Source
To create the standalone executable:
```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name QuickAudioRecorder main.py
```
