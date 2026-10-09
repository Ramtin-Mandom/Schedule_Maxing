"""
app/version.py

The one place the application's version is written. The About page, the
executable's Windows metadata, the installer and the updater all read it
(packaging/windows/build_windows.ps1 parses the `__version__` line below),
so a release is: change this line, tag `v<version>`.

Standard library only; importing it has no side effects.
"""

__version__ = "1.1.0"

#: The name shown to people (window title, installer, Start Menu).
APP_NAME = "Schedule Maxing"
#: The identifier used where spaces are unwelcome (executable, data folder, mutex).
APP_ID = "ScheduleMaxing"
#: Publisher shown by Windows (executable properties, installer, Add/Remove Programs).
APP_PUBLISHER = "Ramtin Rezaei"
#: One-line description in the executable's properties.
APP_DESCRIPTION = "Schedule Maxing scheduling and productivity app"
