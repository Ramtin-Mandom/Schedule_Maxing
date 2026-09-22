"""
benchmarks/generate_fixtures.py

Generates the deterministic synthetic "medium" and "larger" day CSV
fixtures used by benchmarks/optimizer_baseline.py. Uses only the
standard library (random with a fixed seed), matches the existing legacy
CSV schema (see README.md's "CSV Input Format"), and writes to
benchmarks/fixtures/.

Re-running this script regenerates byte-identical output (same seed, same
row order), so the checked-in fixtures are reproducible rather than
one-off hand-authored files. This does not touch app/data_processor.py or
the live CSV schema -- it only produces additional sample input files.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

CATEGORIES = ["study", "work", "exercise", "errand", "food", "entertainment", "other"]
TAGS = ["focus", "review", "prep", "admin", "creative", "social", "chore", ""]

FIXED_BLOCKS = [
    # name, start, end, category
    ("Sleep", 0, 480, "sleep"),
    ("Breakfast", 480, 510, "food"),
    ("Lunch", 720, 780, "food"),
    ("Dinner", 1080, 1140, "food"),
]


def _generate_day_rows(
    rng: random.Random,
    date: int,
    task_count: int,
    dependency_fraction: float,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for name, start, end, category in FIXED_BLOCKS:
        rows.append(
            {
                "date": str(date),
                "name": name,
                "category": category,
                "tag": "fixed",
                "fixed": "true",
                "start_time": str(start),
                "end_time": str(end),
                "duration": str(end - start),
                "priority": "0",
                "dependencies": "",
            }
        )

    task_names: list[str] = []
    task_rows: list[dict[str, str]] = []

    for index in range(task_count):
        duration = rng.choice([30, 45, 60, 90, 120])
        priority = rng.randint(1, 10)
        category = rng.choice(CATEGORIES)
        tag = rng.choice(TAGS)
        pref_start = rng.choice(range(510, 1080, 30))
        pref_end = min(1440, pref_start + rng.choice([60, 90, 120, 180]))

        name = f"Task {date}-{index:02d}"
        task_names.append(name)

        dependencies = ""
        # Only allow a task to depend on an already-generated task on the
        # same day, so the dependency graph is guaranteed acyclic.
        if index > 0 and rng.random() < dependency_fraction:
            dependencies = rng.choice(task_names[:index])

        task_rows.append(
            {
                "date": str(date),
                "name": name,
                "category": category,
                "tag": tag,
                "fixed": "false",
                "start_time": str(pref_start),
                "end_time": str(pref_end),
                "duration": str(duration),
                "priority": str(priority),
                "dependencies": dependencies,
            }
        )

    rows.extend(task_rows)
    return rows


def generate_medium_fixture(seed: int = 42) -> list[dict[str, str]]:
    """~15 flexible tasks, ~30% carry a dependency on an earlier task."""
    rng = random.Random(seed)
    return _generate_day_rows(rng, date=1, task_count=15, dependency_fraction=0.3)


def generate_larger_fixture(seed: int = 43) -> list[dict[str, str]]:
    """~40 flexible tasks, ~30% carry a dependency on an earlier task."""
    rng = random.Random(seed)
    return _generate_day_rows(rng, date=1, task_count=40, dependency_fraction=0.3)


FIELDNAMES = ["date", "name", "category", "tag", "fixed", "start_time", "end_time", "duration", "priority", "dependencies"]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    _write_csv(FIXTURES_DIR / "synthetic_medium_day.csv", generate_medium_fixture())
    _write_csv(FIXTURES_DIR / "synthetic_larger_day.csv", generate_larger_fixture())
    print(f"Wrote fixtures to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
