"""
ml_artifact_migration.py

Carry an ML duration-model artifact saved by an earlier milestone under the
checkout's `data/` folder (config.settings.LEGACY_DATA_DIR) over to the
per-user data folder (config.settings.DATA_DIR) -- the same move the
database itself made in Milestone 2 (app.execution.db.adopt_legacy_database),
with the same guarantees. Standard library only, so it can run before (and
without) importing joblib/sklearn.

The artifact is a *pair*: ml_duration_model.joblib (the fitted pipeline)
and ml_duration_model.meta.json (its metadata sidecar); neither is usable
alone (app/productivity/ml_persistence.load_model_artifact needs both).
migrate_legacy_ml_artifact is idempotent and never destructive:

    - the source files are only read: never moved, modified, or deleted;
    - an existing destination file is never overwritten: if the destination
      already has the complete pair, nothing happens (TARGET_EXISTS); if it
      has one file of the pair that is byte-identical to the source's, the
      missing half is added (completing an interrupted earlier copy); a
      destination file that differs from the source is left alone and
      reported (TARGET_CONFLICT);
    - an incomplete source pair is not copied (INCOMPLETE_SOURCE) -- half an
      artifact would only look like a model;
    - each file is copied to a temporary name in the destination folder and
      then linked into place, so a crash can never leave a half-written
      destination file (at worst one complete file of the pair, which a
      later run completes);
    - source and destination being the same folder is a no-op.

migrate_default_ml_artifact applies it to the implicit default locations
only: when SCHEDULE_MAXING_DATA_DIR explicitly chooses the data folder, that
choice is respected and nothing is adopted into it (SKIPPED_OVERRIDE).
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


class MLArtifactMigrationStatus(str, Enum):
    NO_LEGACY_ARTIFACT = "no_legacy_artifact"
    COPIED = "copied"
    TARGET_EXISTS = "target_exists"
    TARGET_CONFLICT = "target_conflict"
    INCOMPLETE_SOURCE = "incomplete_source"
    SAME_LOCATION = "same_location"
    SKIPPED_OVERRIDE = "skipped_override"


@dataclass(frozen=True)
class MLArtifactMigrationResult:
    status: MLArtifactMigrationStatus
    source_dir: Path
    target_dir: Path
    copied: tuple[Path, ...] = field(default_factory=tuple)


def _names() -> tuple[str, str]:
    return settings.ML_MODEL_FILENAME, settings.ML_MODEL_META_FILENAME


def _same_directory(first: Path, second: Path) -> bool:
    if first.exists() and second.exists():
        return os.path.samefile(first, second)
    return first.resolve() == second.resolve()


def _link_copy(source: Path, target: Path) -> bool:
    """Copy `source` to `target` via a temporary file; False (nothing written) if `target` appeared meanwhile."""
    temp = target.with_name(f".{target.name}.adopting-{os.getpid()}-{threading.get_ident()}")
    try:
        shutil.copyfile(source, temp)
        try:
            os.link(temp, target)  # never replaces an existing file
        except FileExistsError:
            return False
        except OSError:
            if target.exists():
                return False
            os.rename(temp, target)  # no hard links here; on Windows rename never replaces either
        return True
    finally:
        if temp.exists():
            temp.unlink()


def migrate_legacy_ml_artifact(source_dir: str | Path, target_dir: str | Path) -> MLArtifactMigrationResult:
    """Copy the legacy artifact pair from `source_dir` into `target_dir` (see the module docstring)."""
    source_dir, target_dir = Path(source_dir), Path(target_dir)
    names = _names()
    sources = [source_dir / name for name in names]
    targets = [target_dir / name for name in names]

    def result(status: MLArtifactMigrationStatus, copied: tuple[Path, ...] = ()) -> MLArtifactMigrationResult:
        return MLArtifactMigrationResult(status, source_dir, target_dir, copied)

    if not any(path.exists() for path in sources):
        return result(MLArtifactMigrationStatus.NO_LEGACY_ARTIFACT)
    if _same_directory(source_dir, target_dir):
        return result(MLArtifactMigrationStatus.SAME_LOCATION)
    if not all(path.exists() for path in sources):
        return result(MLArtifactMigrationStatus.INCOMPLETE_SOURCE)
    if all(path.exists() for path in targets):
        return result(MLArtifactMigrationStatus.TARGET_EXISTS)
    for source, target in zip(sources, targets):
        if target.exists() and not filecmp.cmp(source, target, shallow=False):
            return result(MLArtifactMigrationStatus.TARGET_CONFLICT)

    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for source, target in zip(sources, targets):
        if target.exists():
            continue  # identical to the source: an earlier, interrupted copy of this same pair
        if _link_copy(source, target):
            copied.append(target)
        elif not filecmp.cmp(source, target, shallow=False):
            return result(MLArtifactMigrationStatus.TARGET_CONFLICT, tuple(copied))
    return result(MLArtifactMigrationStatus.COPIED, tuple(copied))


def migrate_default_ml_artifact() -> MLArtifactMigrationResult:
    """Adopt the repository-local artifact into the default per-user data folder, unless the folder is overridden."""
    source, target = Path(settings.LEGACY_DATA_DIR), Path(settings.DATA_DIR)
    if settings.DATA_DIR_OVERRIDDEN:
        return MLArtifactMigrationResult(MLArtifactMigrationStatus.SKIPPED_OVERRIDE, source, target)
    outcome = migrate_legacy_ml_artifact(source, target)
    if outcome.status == MLArtifactMigrationStatus.COPIED:
        logger.warning(
            "Copied the repository-local ML duration model from %s to %s; the original was left in place.",
            source, target,
        )
    elif outcome.status in (MLArtifactMigrationStatus.TARGET_CONFLICT, MLArtifactMigrationStatus.INCOMPLETE_SOURCE):
        logger.warning("The repository-local ML duration model in %s was not copied: %s.", source, outcome.status.value)
    return outcome
