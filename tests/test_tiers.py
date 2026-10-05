"""
tests/test_tiers.py -- which tier every test belongs to (applied by tests/conftest.py at collection).

Markers (registered in pyproject.toml):
    unit         in-process domain/controller tests (no server, no window, no subprocess)
    integration  server-backed, subprocess or real-window tests
    system       multi-system suites: desktop+server sync, the local web profile, direct PostgreSQL mode
    ui           tests that open real Tk/CustomTkinter windows
    slow         ui tests, subprocess probes, full migration-path suites and individually slow tests
    dev          the development suite: `pytest -m dev` (= not slow, not ui, not system)

Real-window modules are found from their source (WINDOW_PATTERN), so a new one is classified without
editing this file. SLOW_TESTS lists the remaining individual tests that took about a second or more in
the profiling run; add a test here (or mark it @pytest.mark.slow) when it becomes slow.
"""

from __future__ import annotations

import re

#: A test module that builds the desktop application or a Tk root opens real windows.
WINDOW_PATTERN = re.compile(r"open_app\(|ScheduleOptimizerApp|ctk\.CTk\(|tk\.Tk\(|CTkToplevel\(")

#: Directories whose tests run a server, a second storage profile or a local web service.
SYSTEM_DIRS = ("sync", "web", "direct")
INTEGRATION_DIRS = ("backend", *SYSTEM_DIRS)

#: Whole modules that are slow by nature: subprocess probes and the real Alembic migration path.
SLOW_MODULES = frozenset({
    "tests/test_desktop_isolation.py",
    "tests/direct/test_direct_isolation.py",
    "tests/backend/test_migrations.py",
    "tests/backend/test_normalized_migration.py",
})
#: Subprocess modules that are integration tests although they live at the top level.
INTEGRATION_MODULES = frozenset({"tests/test_desktop_isolation.py"})

#: Individual tests of otherwise fast modules that took about a second or more (file::test, any parameters).
SLOW_TESTS = frozenset({
    "tests/direct/test_direct_config.py::test_the_migration_cli_reads_an_explicit_env_file_and_fails_safely",
    "tests/direct/test_direct_config.py::test_an_unreachable_server_is_a_safe_error",
    "tests/direct/test_direct_config.py::test_the_migration_cli_still_works_from_the_environment_alone",
    "tests/direct/test_direct_services.py::test_a_thousand_tasks_round_trip_through_the_direct_services_with_bounded_queries",
    "tests/direct/test_direct_services.py::test_direct_services_run_with_every_http_package_unavailable",
    "tests/direct/test_execution_contract.py::test_every_transition_is_legal_or_refused_exactly_as_the_table_says",
    "tests/backend/test_normalized_storage.py::test_a_thousand_tasks_round_trip_with_bounded_relationship_queries",
    "tests/backend/test_planning_api.py::test_generation_needs_no_desktop_packages",
    "tests/backend/test_health_config.py::test_desktop_startup_does_not_import_the_backend",
    "tests/sync/test_http_transport.py::test_http_errors_are_classified",
    "tests/sync/test_milestone3_end_to_end.py::test_two_devices_complete_workflow",
    "tests/productivity/test_schedule_cohort.py::test_the_host_timezone_never_changes_a_report",
    "tests/test_scheduling_modes.py::test_repacking_matches_an_exhaustive_reference_on_a_small_day",
})


def tiers(relative_path: str, test_name: str, module_source: str) -> set[str]:
    """The markers of one test, from its file (relative to the checkout, forward slashes) and name."""
    parts = relative_path.split("/")
    directory = parts[1] if len(parts) > 2 else ""
    ui = bool(WINDOW_PATTERN.search(module_source))
    system = directory in SYSTEM_DIRS
    integration = ui or directory in INTEGRATION_DIRS or relative_path in INTEGRATION_MODULES
    base_name = test_name.split("[", 1)[0]
    slow = ui or relative_path in SLOW_MODULES or f"{relative_path}::{base_name}" in SLOW_TESTS
    markers = {"integration" if integration else "unit"}
    if ui:
        markers.add("ui")
    if system:
        markers.add("system")
    if slow:
        markers.add("slow")
    if not (slow or ui or system):
        markers.add("dev")
    return markers


def test_tier_rules_classify_the_known_kinds_of_tests() -> None:
    window = "from tests.ui.test_desktop_app import open_app\napp = open_app(path, root)"
    assert tiers("tests/ui/test_desktop_shell.py", "test_x", window) == {"integration", "ui", "slow"}
    assert tiers("tests/ui/test_task_form_model.py", "test_x", "") == {"unit", "dev"}
    assert tiers("tests/planning/test_workflow.py", "test_x[a]", "") == {"unit", "dev"}
    assert tiers("tests/backend/test_auth.py", "test_x", "") == {"integration", "dev"}
    assert tiers("tests/sync/test_protocol.py", "test_x", "") == {"integration", "system"}
    assert tiers("tests/backend/test_migrations.py", "test_x", "") == {"integration", "slow"}
    assert tiers("tests/test_desktop_isolation.py", "test_x", "") == {"integration", "slow"}
    assert "slow" in tiers("tests/direct/test_execution_contract.py",
                           "test_every_transition_is_legal_or_refused_exactly_as_the_table_says[rest]", "")
