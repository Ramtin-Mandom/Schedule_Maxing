"""Tests for app/pert.py: dependency graph, cycle detection, and readiness."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.pert import (
    assert_pert_constraints,
    build_dependency_graph,
    get_dependency_end_time,
    get_existing_dependency_names,
    get_ready_tasks,
    get_task_dependencies,
    get_task_name_set,
    get_topological_order,
    has_cycle,
    respects_dependency_order,
    validate_dependencies_exist,
    validate_pert_constraints,
)

# -----------------------------
# get_task_dependencies
# -----------------------------


def test_get_task_dependencies_returns_stripped_names(make_task) -> None:
    task = make_task(dependencies=["Study Math", "  Read Chapter  "])
    assert get_task_dependencies(task) == ["Study Math", "Read Chapter"]


def test_get_task_dependencies_drops_whitespace_only_entries(make_task) -> None:
    task = make_task(dependencies=["Study Math", "   "])
    assert get_task_dependencies(task) == ["Study Math"]


def test_get_task_dependencies_empty_when_no_field() -> None:
    task = SimpleNamespace(name="No Deps Field")
    assert get_task_dependencies(task) == []


def test_get_task_dependencies_accepts_single_string() -> None:
    task = SimpleNamespace(name="X", dependencies="Study Math")
    assert get_task_dependencies(task) == ["Study Math"]


# -----------------------------
# get_task_name_set / get_existing_dependency_names
# -----------------------------


def test_get_task_name_set(make_task) -> None:
    tasks = [make_task("A"), make_task("B")]
    assert get_task_name_set(tasks) == {"A", "B"}


def test_get_existing_dependency_names_filters_missing(make_task) -> None:
    tasks = [
        make_task("Math Exam"),
        make_task("Math Review", dependencies=["Math Exam", "Missing Task"]),
    ]
    review = tasks[1]

    assert get_existing_dependency_names(review, tasks) == ["Math Exam"]


# -----------------------------
# build_dependency_graph / has_cycle
# -----------------------------


def test_build_dependency_graph_direction_is_dependency_to_task(make_task) -> None:
    tasks = [
        make_task("Math Exam"),
        make_task("Math Review", dependencies=["Math Exam"]),
    ]
    graph = build_dependency_graph(tasks)

    assert graph["Math Exam"] == ["Math Review"]
    assert graph["Math Review"] == []


def test_build_dependency_graph_ignores_missing_dependency(make_task) -> None:
    tasks = [make_task("Solo", dependencies=["Ghost Task"])]
    graph = build_dependency_graph(tasks)

    assert graph == {"Solo": []}
    assert "Ghost Task" not in graph


def test_has_cycle_false_for_acyclic_graph(make_task) -> None:
    tasks = [
        make_task("A"),
        make_task("B", dependencies=["A"]),
        make_task("C", dependencies=["B"]),
    ]
    assert has_cycle(tasks) is False


def test_has_cycle_true_for_direct_cycle(make_task) -> None:
    tasks = [
        make_task("A", dependencies=["B"]),
        make_task("B", dependencies=["A"]),
    ]
    assert has_cycle(tasks) is True


def test_has_cycle_true_for_self_dependency(make_task) -> None:
    tasks = [make_task("A", dependencies=["A"])]
    assert has_cycle(tasks) is True


def test_has_cycle_ignores_missing_dependency(make_task) -> None:
    tasks = [make_task("A", dependencies=["Ghost"])]
    assert has_cycle(tasks) is False


# -----------------------------
# get_topological_order
# -----------------------------


def test_get_topological_order_respects_dependencies(make_task) -> None:
    tasks = [
        make_task("Review", dependencies=["Study"]),
        make_task("Study"),
    ]
    order = get_topological_order(tasks)

    assert order.index("Study") < order.index("Review")
    assert set(order) == {"Study", "Review"}


def test_get_topological_order_raises_on_cycle(make_task) -> None:
    tasks = [
        make_task("A", dependencies=["B"]),
        make_task("B", dependencies=["A"]),
    ]
    with pytest.raises(ValueError):
        get_topological_order(tasks)


# -----------------------------
# get_dependency_end_time
# -----------------------------


def test_get_dependency_end_time_zero_when_no_dependencies(make_task) -> None:
    task = make_task("Solo")
    assert get_dependency_end_time(task, [task], []) == 0


def test_get_dependency_end_time_zero_when_dependency_missing(make_task) -> None:
    task = make_task("Solo", dependencies=["Ghost"])
    assert get_dependency_end_time(task, [task], []) == 0


def test_get_dependency_end_time_returns_max_end_when_scheduled(
    make_task, make_scheduled_task
) -> None:
    task = make_task("Review", dependencies=["Study"])
    all_tasks = [task, make_task("Study")]
    scheduled = [make_scheduled_task("Study", start=0, end=90)]

    assert get_dependency_end_time(task, all_tasks, scheduled) == 90


def test_get_dependency_end_time_none_when_dependency_not_yet_scheduled(
    make_task,
) -> None:
    task = make_task("Review", dependencies=["Study"])
    all_tasks = [task, make_task("Study")]

    assert get_dependency_end_time(task, all_tasks, []) is None


def test_get_dependency_end_time_uses_latest_among_multiple_dependencies(
    make_task, make_scheduled_task
) -> None:
    task = make_task("Final", dependencies=["A", "B"])
    all_tasks = [task, make_task("A"), make_task("B")]
    scheduled = [
        make_scheduled_task("A", start=0, end=60),
        make_scheduled_task("B", start=100, end=200),
    ]

    assert get_dependency_end_time(task, all_tasks, scheduled) == 200


# -----------------------------
# get_ready_tasks
# -----------------------------


def test_get_ready_tasks_excludes_tasks_waiting_on_dependency(make_task) -> None:
    study = make_task("Study")
    review = make_task("Review", dependencies=["Study"])

    ready = get_ready_tasks([study, review], [study, review], [])

    assert ready == [study]


def test_get_ready_tasks_includes_task_once_dependency_scheduled(
    make_task, make_scheduled_task
) -> None:
    study = make_task("Study")
    review = make_task("Review", dependencies=["Study"])
    scheduled = [make_scheduled_task("Study", start=0, end=60)]

    ready = get_ready_tasks([review], [study, review], scheduled)

    assert ready == [review]


# -----------------------------
# respects_dependency_order / validate_pert_constraints
# -----------------------------


def test_respects_dependency_order_true_when_valid(make_task, make_scheduled_task) -> None:
    tasks = [make_task("Study"), make_task("Review", dependencies=["Study"])]
    scheduled = [
        make_scheduled_task("Study", start=0, end=60),
        make_scheduled_task("Review", start=60, end=120),
    ]

    assert respects_dependency_order(scheduled, tasks) is True


def test_respects_dependency_order_false_when_dependent_starts_too_early(
    make_task, make_scheduled_task
) -> None:
    tasks = [make_task("Study"), make_task("Review", dependencies=["Study"])]
    scheduled = [
        make_scheduled_task("Study", start=0, end=60),
        make_scheduled_task("Review", start=30, end=90),
    ]

    assert respects_dependency_order(scheduled, tasks) is False


def test_respects_dependency_order_ignores_missing_dependency(
    make_task, make_scheduled_task
) -> None:
    tasks = [make_task("Solo", dependencies=["Ghost"])]
    scheduled = [make_scheduled_task("Solo", start=0, end=60)]

    assert respects_dependency_order(scheduled, tasks) is True


def test_validate_pert_constraints_false_on_cycle(make_task) -> None:
    tasks = [make_task("A", dependencies=["B"]), make_task("B", dependencies=["A"])]
    assert validate_pert_constraints(tasks) is False


def test_validate_pert_constraints_true_for_valid_schedule(
    make_task, make_scheduled_task
) -> None:
    tasks = [make_task("Study"), make_task("Review", dependencies=["Study"])]
    scheduled = [
        make_scheduled_task("Study", start=0, end=60),
        make_scheduled_task("Review", start=60, end=120),
    ]

    assert validate_pert_constraints(tasks, scheduled) is True


def test_assert_pert_constraints_raises_on_cycle(make_task) -> None:
    tasks = [make_task("A", dependencies=["B"]), make_task("B", dependencies=["A"])]

    with pytest.raises(ValueError):
        assert_pert_constraints(tasks)


def test_assert_pert_constraints_raises_on_order_violation(
    make_task, make_scheduled_task
) -> None:
    tasks = [make_task("Study"), make_task("Review", dependencies=["Study"])]
    scheduled = [
        make_scheduled_task("Study", start=0, end=60),
        make_scheduled_task("Review", start=30, end=90),
    ]

    with pytest.raises(ValueError):
        assert_pert_constraints(tasks, scheduled)


def test_validate_dependencies_exist_always_true(make_task) -> None:
    assert validate_dependencies_exist([make_task("A", dependencies=["Ghost"])]) is True
