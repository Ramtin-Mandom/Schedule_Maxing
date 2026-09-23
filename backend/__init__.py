"""
Schedule Maxing server backend (Milestone 3): a FastAPI application with
PostgreSQL persistence (SQLAlchemy 2 + Alembic), password authentication
(argon2) and JWT access tokens, exposing user-scoped, versioned resources
that mirror the desktop's local records (docs/sync-contract.md).

The desktop app never imports this package; nothing here imports Tk, the
local SQLite database, or scikit-learn. Start it with the factory, e.g.
`uvicorn --factory backend.app:create_app` (see docs/backend.md).
"""
