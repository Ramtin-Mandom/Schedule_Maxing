"""
app/budget -- personal budgeting: a user-entered balance, income and expense
records with recurrence, and per-occurrence edits.

Budget data is deliberately separate from planning: nothing in the
scheduling engines, hard constraints, PERT or reward scoring imports this
package, and budget records never become tasks, fixed blocks or placements.
A budget record may only *name* a project (an optional link); the project
is never changed by it.

Modules:
    money.py       exact amounts in integer minor units (cents), parsing and formatting
    models.py      BudgetBalance, BudgetEntry, BudgetOverride (owner-scoped, sync-ready records)
    recurrence.py  occurrence dates of a one-time / daily / weekly / biweekly / monthly entry
    service.py     BudgetService: validation, CRUD, occurrence edits, range expansion, totals
    repository.py  BudgetRepository: the local SQLite storage (schema v17)
The direct PostgreSQL storage is app/persistence/budget.py; the server's
records are backend/resources.py (BUDGET_* specs).
"""
