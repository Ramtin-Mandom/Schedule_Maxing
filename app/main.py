import csv
from pathlib import Path

from app.data_processor import load_schedule_from_csv
from app.optimizer import combine_fixed_and_optimized_scheduled_tasks


def minutes_to_time(minutes: int) -> str:
    hour = minutes // 60
    minute = minutes % 60

    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12

    if display_hour == 0:
        display_hour = 12

    return f"{display_hour}:{minute:02d} {suffix}"


def minutes_to_24_hour_time(minutes: int) -> str:
    """
    Converts minutes from the start of the day into 24-hour time.

    Example:
        870 -> "14:30"
    """
    hour = minutes // 60
    minute = minutes % 60

    return f"{hour:02d}:{minute:02d}"


def print_day_schedule(date: int, day_output) -> None:
    print("=" * 60)
    print(f"Optimized Schedule for Day {date}")
    print("=" * 60)

    print(f"Total Score: {day_output.total_score:.2f}")
    print()

    scheduled_tasks = sorted(
        day_output.scheduled_tasks,
        key=lambda task: task.time_window.start_time,
    )

    if not scheduled_tasks:
        print("No tasks were scheduled.")
    else:
        print("Scheduled Tasks:")
        print("-" * 60)

        for task in scheduled_tasks:
            start = minutes_to_time(task.time_window.start_time)
            end = minutes_to_time(task.time_window.end_time)

            print(f"{start} - {end}")
            print(f"  Task:     {task.name}")
            print(f"  Category: {task.category}")
            print(f"  Tag:      {task.tag}")
            print(f"  Score:    {task.score:.2f}")
            print("-" * 60)

    if day_output.unscheduled_tasks:
        print()
        print("Unscheduled Tasks:")
        print("-" * 60)

        for task in day_output.unscheduled_tasks:
            print(f"- {task.name}")
            print(f"  Duration: {task.duration} minutes")
            print(f"  Priority: {task.priority}")
            print(f"  Category: {task.category}")
            print(f"  Tag: {task.tag}")
            print("-" * 60)

    print()


def export_day_schedule_to_csv(day_output, output_path: Path) -> None:
    """
    Exports the final schedule into 30-minute time blocks.

    The CSV contains two columns:
        time, task

    If a task lasts 3 hours, it appears in 6 rows.
    If no task is active during a block, the task value is "-".
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scheduled_tasks = sorted(
        day_output.scheduled_tasks,
        key=lambda task: task.time_window.start_time,
    )

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["time", "task"])

        for block_start in range(0, 24 * 60, 30):
            task_name = "-"

            for task in scheduled_tasks:
                task_start = task.time_window.start_time
                task_end = task.time_window.end_time

                if task_start <= block_start < task_end:
                    task_name = task.name
                    break

            writer.writerow([
                minutes_to_24_hour_time(block_start),
                task_name,
            ])

    print(f"Schedule CSV exported to: {output_path}")


def main() -> None:
    # app/ → go up one level to project root
    base_dir = Path(__file__).resolve().parent.parent

    csv_path = base_dir / "samples" / "inputs" / "day_sample.csv"
    output_path = base_dir / "samples" / "outputs" / "day_sample.csv"

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    schedule_input = load_schedule_from_csv(str(csv_path))

    for date, day_schedule in schedule_input.schedules.items():
        full_schedule = combine_fixed_and_optimized_scheduled_tasks(
            date=date,
            day_schedule=day_schedule,
        )

        print_day_schedule(date, full_schedule)
        export_day_schedule_to_csv(full_schedule, output_path)


if __name__ == "__main__":
    main()
