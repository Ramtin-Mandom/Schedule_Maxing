"""
app/ui/ui_settings.py

The desktop's own appearance settings -- light/dark and the interface scale
-- in a small JSON file next to the application database
(ui_settings.json). Tk-free and tested headlessly.

This is deliberately *not* the planning preferences (those live in SQLite
and synchronize) nor the reward/optimizer configuration (config/ and
app/reward.py globals are never touched here), and it never holds a
password, token or account detail. A missing, unreadable or hand-edited file
never prevents startup: each invalid value falls back to its default.
Writes are atomic (a temporary file, then os.replace).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAME = "ui_settings.json"
APPEARANCES = ("light", "dark")
LANGUAGES = {"en": "English"}
#: Interface scale choices (CustomTkinter widget scaling), on top of the system DPI scaling.
SCALES = (0.9, 1.0, 1.15, 1.3)


@dataclass(frozen=True)
class UISettings:
    appearance: str = "light"
    ui_scale: float = 1.0
    language: str = "en"
    #: Updates (app/ui/update_controller.py): look for a new version automatically; a version the user chose
    #: to skip; and when the last automatic check ran (ISO 8601, UTC), so it runs at most once a day.
    check_for_updates: bool = True
    skipped_version: str | None = None
    last_update_check: str | None = None

    def with_appearance(self, appearance: str) -> "UISettings":
        if appearance not in APPEARANCES:
            raise ValueError(f"appearance must be one of {APPEARANCES}")
        return replace(self, appearance=appearance)

    def with_scale(self, ui_scale: float) -> "UISettings":
        if ui_scale not in SCALES:
            raise ValueError(f"the interface scale must be one of {SCALES}")
        return replace(self, ui_scale=ui_scale)


def settings_path_for(db_path: str | Path) -> Path:
    """The appearance file of a database: beside it, so a data directory keeps its own look."""
    return Path(db_path).resolve().parent / FILENAME


class UISettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> UISettings:
        defaults = UISettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return defaults
        except (OSError, ValueError) as error:
            logger.warning("Ignoring unreadable UI settings %s: %s", self.path, error)
            return defaults
        if not isinstance(data, dict):
            return defaults
        appearance = data.get("appearance")
        scale = data.get("ui_scale")
        return UISettings(
            appearance=appearance if appearance in APPEARANCES else defaults.appearance,
            ui_scale=float(scale) if isinstance(scale, (int, float)) and float(scale) in SCALES else defaults.ui_scale,
            language=data.get("language") if data.get("language") in LANGUAGES else defaults.language,
            check_for_updates=data["check_for_updates"] if isinstance(data.get("check_for_updates"), bool)
            else defaults.check_for_updates,
            skipped_version=data.get("skipped_version") if isinstance(data.get("skipped_version"), str) else None,
            last_update_check=data.get("last_update_check") if isinstance(data.get("last_update_check"), str) else None,
        )

    def save(self, settings: UISettings) -> bool:
        """Write atomically; False (logged, nothing raised) if the file cannot be written."""
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            logger.warning("Could not save UI settings to %s: %s", self.path, error)
            return False
        return True
