"""The legacy data/ ML artifact migration (app/productivity/ml_artifact_migration.py)
and load_model_artifact's default-location fallback: copy-only, idempotent,
never overwrites a destination file, handles half pairs safely, and respects
an explicit SCHEDULE_MAXING_DATA_DIR override.
"""

from __future__ import annotations

from pathlib import Path

from app.productivity import ml_persistence
from app.productivity.ml_artifact_migration import (
    MLArtifactMigrationStatus,
    migrate_default_ml_artifact,
    migrate_legacy_ml_artifact,
)
from config import settings

MODEL, META = settings.ML_MODEL_FILENAME, settings.ML_MODEL_META_FILENAME


def write_pair(directory: Path, *, model: bytes = b"model-bytes", meta: str = '{"meta": 1}', only: str | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    if only in (None, "model"):
        (directory / MODEL).write_bytes(model)
    if only in (None, "meta"):
        (directory / META).write_text(meta, encoding="utf-8")


def contents(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())} if directory.exists() else {}


def test_copies_the_pair_once_and_is_idempotent(tmp_path: Path) -> None:
    source, target = tmp_path / "legacy", tmp_path / "user"
    write_pair(source)

    first = migrate_legacy_ml_artifact(source, target)
    second = migrate_legacy_ml_artifact(source, target)

    assert first.status == MLArtifactMigrationStatus.COPIED and {p.name for p in first.copied} == {MODEL, META}
    assert second.status == MLArtifactMigrationStatus.TARGET_EXISTS
    assert contents(target) == contents(source)
    assert contents(source) == {META: b'{"meta": 1}', MODEL: b"model-bytes"}  # the source is preserved


def test_never_overwrites_an_existing_destination(tmp_path: Path) -> None:
    source, target = tmp_path / "legacy", tmp_path / "user"
    write_pair(source)
    write_pair(target, model=b"retrained", meta='{"meta": 2}')
    assert migrate_legacy_ml_artifact(source, target).status == MLArtifactMigrationStatus.TARGET_EXISTS

    other = tmp_path / "other"
    write_pair(other, model=b"different", only="model")
    result = migrate_legacy_ml_artifact(source, other)
    assert result.status == MLArtifactMigrationStatus.TARGET_CONFLICT
    assert contents(other) == {MODEL: b"different"}  # nothing added next to a conflicting file


def test_incomplete_pairs_are_handled_safely(tmp_path: Path) -> None:
    half_source, target = tmp_path / "half", tmp_path / "user"
    write_pair(half_source, only="meta")
    assert migrate_legacy_ml_artifact(half_source, target).status == MLArtifactMigrationStatus.INCOMPLETE_SOURCE
    assert contents(target) == {}

    # A destination holding one identical half (an interrupted earlier copy) is completed, not overwritten.
    source = tmp_path / "legacy"
    write_pair(source)
    write_pair(target, only="model")
    result = migrate_legacy_ml_artifact(source, target)
    assert result.status == MLArtifactMigrationStatus.COPIED and [p.name for p in result.copied] == [META]
    assert contents(target) == contents(source)


def test_no_artifact_and_same_location_are_no_ops(tmp_path: Path) -> None:
    assert migrate_legacy_ml_artifact(tmp_path / "none", tmp_path / "user").status == (
        MLArtifactMigrationStatus.NO_LEGACY_ARTIFACT
    )
    write_pair(tmp_path / "same")
    assert migrate_legacy_ml_artifact(tmp_path / "same", tmp_path / "same").status == MLArtifactMigrationStatus.SAME_LOCATION


def test_explicit_data_dir_override_is_respected(tmp_path: Path, monkeypatch) -> None:
    write_pair(Path(settings.LEGACY_DATA_DIR))
    monkeypatch.setattr(settings, "DATA_DIR_OVERRIDDEN", True)

    assert migrate_default_ml_artifact().status == MLArtifactMigrationStatus.SKIPPED_OVERRIDE
    assert contents(Path(settings.DATA_DIR)) == {}


def test_default_load_adopts_the_legacy_artifact_and_falls_back_to_it(tmp_path: Path, monkeypatch) -> None:
    write_pair(Path(settings.LEGACY_DATA_DIR))
    loaded: list[Path] = []
    monkeypatch.setattr(ml_persistence, "_load_from", lambda directory: loaded.append(Path(directory)))

    ml_persistence.load_model_artifact()
    assert contents(Path(settings.DATA_DIR)) == contents(Path(settings.LEGACY_DATA_DIR))
    assert loaded == [Path(settings.DATA_DIR)]

    # When the per-user folder cannot receive it, the old location is read in place.
    for path in Path(settings.DATA_DIR).iterdir():
        path.unlink()
    loaded.clear()
    monkeypatch.setattr(ml_persistence, "migrate_default_ml_artifact", _raise)
    ml_persistence.load_model_artifact()
    assert loaded == [Path(settings.LEGACY_DATA_DIR)]


def _raise():
    raise PermissionError("read-only data folder (injected)")
