"""
packaging/windows/launcher.py

The script PyInstaller turns into ScheduleMaxing.exe. It only hands over to
the production entry point, app/desktop.py -- kept outside the `app` package
so that package's folder is never put on the import path as a script folder.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    # Any multiprocessing worker re-runs this executable in a packaged program.
    multiprocessing.freeze_support()
    from app.desktop import main

    sys.exit(main(sys.argv[1:]))
