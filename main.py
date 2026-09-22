import ctypes
import sys

MUTEX_NAME = "QuickAudioRecorder_SingleInstance_Mutex"


def main():
    import multiprocessing

    from PyQt6.QtWidgets import QApplication

    import audio_recorder

    # Single-instance guard: create a named system mutex. If it already exists
    # (another instance is running), CreateMutex returns a handle to the existing
    # mutex and GetLastError reports ERROR_ALREADY_EXISTS, so we exit silently.
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("QuickAudioRecorder is already running.")
        sys.exit(0)

    multiprocessing.freeze_support()
    # The cache function owns the worker that imports soundcard and
    # initializes WASAPI. Keep this inside main so spawned capture workers do
    # not rerun application startup while importing this module.
    audio_recorder.prime_device_cache()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from gui import TrayApplication
    app.tray = TrayApplication(app)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
