import csv
from models import (
    TimeWindow,
    Task,
    FixedBlock,
    DaySchedule,
    ScheduleInput,
)


DEFAULT_DAY_START = 480   # 8:00 AM
DEFAULT_DAY_END = 1320    # 10:00 PM


def read_csv_rows(file_path: str) -> list[dict]:
    with open(file_path, mode="r", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        return list(reader)


def is_fixed(value: str) -> bool:
    return value.strip().lower() == "true"


def build_time_window(start_time: str, end_time: str) -> TimeWindow:
    return TimeWindow(
        start_time=int(start_time),
        end_time=int(end_time),
    )


def load_schedule_from_csv(file_path: str) -> ScheduleInput:
    rows = read_csv_rows(file_path)

    schedules: dict[int, DaySchedule] = {}

    for row in rows:
        date = int(row["date"])

        if date not in schedules:
            schedules[date] = DaySchedule(
                time_window=TimeWindow(
                    start_time=DEFAULT_DAY_START,
                    end_time=DEFAULT_DAY_END,
                ),
                fixed_blocks=[],
                tasks=[],
            )

        fixed = is_fixed(row["fixed"])

        if fixed:
            fixed_block = FixedBlock(
                name=row["name"],
                category=row["category"],
                time_window=build_time_window(
                    row["start_time"],
                    row["end_time"],
                ),
            )

            schedules[date].fixed_blocks.append(fixed_block)

        else:
            task = Task(
                name=row["name"],
                date=date,
                category=row["category"],
                tag=row["tag"],
                fixed=False,
                duration=int(row["duration"]),
                priority=int(row["priority"]),
                preference_time=build_time_window(
                    row["start_time"],
                    row["end_time"],
                ),
            )

            schedules[date].tasks.append(task)

    return ScheduleInput(schedules=schedules)