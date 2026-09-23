# config/settings.py

import os
import sys
from collections.abc import Mapping
from pathlib import Path

# -------------------------
# Time Model
# -------------------------

TIME_SLOT_MINUTES = 30

DEFAULT_DAY_START = 0    # 0:00 AM
DEFAULT_DAY_END = 1440    # 12:00 PM

DEFAULT_MODE = "multi_day"


# -------------------------
# Reward Weights
# -------------------------

WEIGHT_PRIORITY = 5
WEIGHT_PREFERENCE_TIME = 4
WEIGHT_TAG_RELATION = 3
WEIGHT_SPACING = 2
WEIGHT_DEADLINE_BONUS = 3

WEIGHT_NO_BREAK_PENALTY = -2
WEIGHT_LATE_TASK_PENALTY = -5

# Reward behavior constants

PREFERENCE_TIME_DISTANCE_SCALE = 720
TAG_RELATION_MAX_GAP = 120

MIN_GOOD_BREAK = 15
MAX_GOOD_BREAK = 60
BACK_TO_BACK_GAP = 0


# -------------------------
# Hard Constraints
# -------------------------

ALLOW_OVERLAP = False
ALLOW_TASK_SPLITTING = False

MAX_TASKS_PER_DAY = 12

ENFORCE_FIXED_BLOCKS = True
ENFORCE_DAY_WINDOW = True
ENFORCE_DEPENDENCIES = True
ENFORCE_DEADLINES = True


# -------------------------
# Simulated Annealing
# -------------------------

DEFAULT_OPTIMIZER = "greedy"

INITIAL_TEMPERATURE = 100.0
MIN_TEMPERATURE = 0.1
COOLING_RATE = 0.95

MAX_ITERATIONS = 5000
NO_IMPROVEMENT_LIMIT = 500


# -------------------------
# Neighbor Generation
# -------------------------

ALLOW_MOVE_TASK = True
ALLOW_SWAP_TASKS = True
ALLOW_SHIFT_TASK = True

SHIFT_AMOUNT_MINUTES = 30


# -------------------------
# Local Data Storage
# -------------------------

APP_DATA_DIRNAME = "ScheduleMaxing"


def default_user_data_dir(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """
    The stable per-user application-data directory for this platform,
    independent of the current working directory and of where this
    checkout lives:

        Windows: %LOCALAPPDATA%/ScheduleMaxing (falls back to ~/AppData/Local)
        macOS:   ~/Library/Application Support/ScheduleMaxing
        other:   $XDG_DATA_HOME/ScheduleMaxing (falls back to ~/.local/share)

    Standard library only (no platformdirs dependency). The keyword
    arguments exist so tests can exercise every branch deterministically.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = home or Path.home()

    if platform.startswith("win"):
        base = environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    elif platform == "darwin":
        base = str(home / "Library" / "Application Support")
    else:
        base = environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / APP_DATA_DIRNAME


# Directory for local runtime data (the application SQLite database holding
# execution history *and* persisted planning data, plus the ML artifact).
# Defaults to the per-user application-data directory above. Override with
# the SCHEDULE_MAXING_DATA_DIR environment variable (unchanged from earlier
# milestones); code that opens the database can also inject an explicit
# path (see app.execution.db.get_connection(db_path=...)).
DATA_DIR_ENV_VAR = "SCHEDULE_MAXING_DATA_DIR"


def resolve_data_dir(environ: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """(data directory, whether it came from the SCHEDULE_MAXING_DATA_DIR override)."""
    environ = os.environ if environ is None else environ
    override = environ.get(DATA_DIR_ENV_VAR)
    if override:
        return Path(override), True
    return default_user_data_dir(environ=environ), False


DATA_DIR, DATA_DIR_OVERRIDDEN = resolve_data_dir()

# Where earlier milestones stored runtime data (inside the checkout). Only
# ever *read* -- see app.execution.db.adopt_legacy_database for how an
# existing repository-local executions.db is carried over (copied, never
# moved, merged, or deleted).
LEGACY_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# IANA timezone the desktop app plans in: a legacy "minutes from midnight"
# form value on a page date is a wall-clock time in this zone. Override with
# SCHEDULE_MAXING_TIMEZONE (e.g. "America/Toronto").
DEFAULT_TIMEZONE = os.environ.get("SCHEDULE_MAXING_TIMEZONE") or "UTC"

# The application database. The historical filename is kept so existing
# SCHEDULE_MAXING_DATA_DIR overrides keep pointing at the same file.
EXECUTION_DB_FILENAME = "executions.db"

# Persisted ML duration-predictor artifact (see app/productivity/ml_persistence.py).
# Lives alongside the execution database under the same DATA_DIR.
ML_MODEL_FILENAME = "ml_duration_model.joblib"
ML_MODEL_META_FILENAME = "ml_duration_model.meta.json"
# Optional synchronization backend (app/sync, docs/sync-protocol.md). Unset
# (the default) keeps the desktop app fully offline: sync is inert and no
# network access, account, or backend setting is needed. Credentials are
# never configured here -- they are entered at sign-in and kept in memory.
BACKEND_URL = os.environ.get("SCHEDULE_MAXING_BACKEND_URL", "").strip() or None
