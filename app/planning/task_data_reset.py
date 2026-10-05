"""
app/planning/task_data_reset.py

The local half of "Reset All Task Data" (Settings): remove one workspace's
task, schedule and execution data from this device's SQLite database, in
ONE transaction (a failure leaves everything as it was).

Scope: exactly the given OwnerScope -- the signed-in account's records, or
the ownerless local workspace -- never another account's rows.

Removed (rows deleted, not tombstoned: the server keeps the account's
history, and this device must not show or resend any of it):

    executions (their work sessions and wire-id mappings cascade),
    placements (scheduled_tasks), schedule generation records, fixed blocks,
    tasks (tags, preferred dates, dependencies, recurrence weekdays cascade),
    projects.

Kept: preference layers (scheduling settings: default and per-date day
windows, engines), the device's accounts and UI settings.

Synchronization bookkeeping for the removed records goes too, so nothing
can be sent again: their change-capture marks (sync_dirty), and -- for an
account -- the account's shadows, queued operations and open conflicts of
those record types. The writes run with change capture off
(SyncStore.applying_remote), so the deletion itself marks nothing to send.
The caller (app/sync/service.SyncService.reset_task_data) only does this
after the server confirmed its own reset.
"""

from __future__ import annotations

from app.planning.scope import OwnerScope

#: Sync entity type -> local table, in a foreign-key-safe deletion order.
TASK_DATA_TABLES: tuple[tuple[str, str], ...] = (
    ("execution", "executions"),
    ("placement", "scheduled_tasks"),
    ("schedule_generation", "schedule_generations"),
    ("fixed_block", "fixed_blocks"),
    ("task", "tasks"),
    ("project", "projects"),
)
TASK_DATA_ENTITY_TYPES = tuple(entity for entity, _ in TASK_DATA_TABLES)


def _owner_condition(scope: OwnerScope) -> tuple[str, tuple]:
    if scope.user_id is None:
        return "user_id IS NULL", ()
    return "user_id = ?", (str(scope.user_id),)


def wipe_task_data(connection, scope: OwnerScope, *, account_key: str | None = None,
                   cursor: int | None = None) -> dict[str, int]:
    """
    Delete the scope's task data and its sync bookkeeping (see the module
    docstring); with account_key, also the account's shadows / queued
    operations / conflicts of these record types, and move its pull cursor
    to `cursor` (the server's position right after its reset). Returns the
    number of rows removed per record type.
    """
    from app.sync.store import SyncStore

    if not isinstance(scope, OwnerScope):
        raise TypeError("wipe_task_data needs an OwnerScope")
    store = SyncStore(connection)
    condition, params = _owner_condition(scope)
    removed: dict[str, int] = {}
    with store.applying_remote():
        ids = {table: [row[0] for row in connection.execute(f"SELECT id FROM {table} WHERE {condition}", params)]
               for _, table in TASK_DATA_TABLES}
        task_ids = ids["tasks"]
        # A dependency another workspace's task has on one of these tasks goes with it (never a dangling link).
        for start in range(0, len(task_ids), 500):
            chunk = task_ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            connection.execute(f"DELETE FROM task_dependencies WHERE depends_on_task_id IN ({marks})", chunk)
        for entity_type, table in TASK_DATA_TABLES:
            removed[entity_type] = connection.execute(f"DELETE FROM {table} WHERE {condition}", params).rowcount
            for start in range(0, len(ids[table]), 500):
                chunk = ids[table][start:start + 500]
                marks = ", ".join("?" for _ in chunk)
                connection.execute(f"DELETE FROM sync_dirty WHERE entity_type = ? AND entity_id IN ({marks})",
                                   (entity_type, *chunk))
        # The workspace's task types go with its tasks. The server keeps them (a label is not task data), so
        # only their pending change marks are dropped: nothing queues a deletion of a type on the server.
        connection.execute(
            f"DELETE FROM sync_dirty WHERE entity_type = 'task_type' AND entity_id IN "
            f"(SELECT id FROM task_types WHERE {condition})", params)
        connection.execute(f"DELETE FROM task_types WHERE {condition}", params)
        if account_key is not None:
            marks = ", ".join("?" for _ in TASK_DATA_ENTITY_TYPES)
            for table in ("sync_outbox", "sync_shadows", "sync_conflicts"):
                connection.execute(f"DELETE FROM {table} WHERE account_key = ? AND entity_type IN ({marks})",
                                   (account_key, *TASK_DATA_ENTITY_TYPES))
            if cursor is not None:
                store.set_cursor(account_key, cursor)
    return removed
