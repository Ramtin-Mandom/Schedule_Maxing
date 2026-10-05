"""The status bar's counts come from COUNT queries that must agree exactly with the record-loading reads."""
from datetime import date

from app.planning.models import Task
from app.ui.app_services import open_app_services
from config import settings


def test_pending_and_conflict_counts_match_the_records_they_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKEND_URL", None)
    services = open_app_services(tmp_path / "counts.db", timezone="UTC", project_root=str(tmp_path),
                                 background_sync=False)
    try:
        store = services.sync_service._engine.store
        mine = store.upsert_account("http://one.invalid", "00000000-0000-0000-0000-000000000001", "one@example.invalid")
        other = store.upsert_account("http://two.invalid", "00000000-0000-0000-0000-000000000002", "two@example.invalid")
        store.set_active(mine.account_key)
        services.switch_workspace()
        planning = services.planning_controller
        tasks = [planning.add_or_update_task(Task(name=f"Synthetic {index}", category="study", priority=5,
                                                  estimated_duration_minutes=15,
                                                  preferred_dates=[date(2026, 9, 21)])).value for index in range(3)]

        def agree(expected: int | None = None) -> int:
            for account in (mine, other):
                records = store.pending_records(account.account_key, account.user_id)
                assert store.pending_count(account.account_key, account.user_id) == len(records)
                for status in ("open", "resolved"):
                    assert store.conflict_count(account.account_key, status) == len(
                        store.conflicts(account.account_key, status))
            count = store.pending_count(mine.account_key, mine.user_id)
            assert expected is None or count == expected
            return count

        dirty = agree()
        assert dirty >= 3  # the three tasks this account owns are waiting
        with store.transaction():
            # Dirty and queued at once: still one waiting record.
            store.add_op(mine.account_key, "task", str(tasks[0].id), str(tasks[0].id), "update", local_rev=1)
            store.add_op(mine.account_key, "task", str(tasks[0].id), str(tasks[0].id), "update", local_rev=1)
        agree(dirty)
        with store.transaction():
            store.add_op(mine.account_key, "task", "queued-only", "queued-only", "delete", local_rev=1)  # queued, not dirty
            store.add_op(mine.account_key, "task_type", "a-type", "a-type", "create", local_rev=1)  # never counted on its own
            store.add_op(other.account_key, "task", "theirs", "theirs", "create", local_rev=1)  # another account's queue
        agree(dirty + 1)
        assert store.pending_count(other.account_key, other.user_id) == 1

        with store.transaction():
            first = store.add_conflict(mine.account_key, "task", str(tasks[1].id), str(tasks[1].id), "push_conflict",
                                       local_record={"name": "local"}, remote_record={"name": "remote"})
            store.add_conflict(mine.account_key, "task", str(tasks[2].id), str(tasks[2].id), "pull_conflict")
            store.add_conflict(other.account_key, "task", "theirs", "theirs", "push_rejected")
        agree()
        assert store.conflict_count(mine.account_key) == 2 and store.conflict_count(other.account_key) == 1
        with store.transaction():
            store.resolve_conflict(first, {"choice": "local"})
        agree()
        assert store.conflict_count(mine.account_key) == 1 and store.conflict_count(mine.account_key, "resolved") == 1

        status = services.sync_service.status()
        assert status.pending == dirty + 1 and status.conflicts == 1
    finally:
        services.close()
