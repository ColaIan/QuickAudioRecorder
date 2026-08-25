import sys
# Ensure COM is initialized as Single-Threaded Apartment (STA) 
# BEFORE any PyQt or win32 modules attempt to initialize it as MTA.
sys.coinit_flags = 2  

from PyQt6.QtWidgets import QApplication

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    from gui import TrayApplication
    tray = TrayApplication(app)
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
