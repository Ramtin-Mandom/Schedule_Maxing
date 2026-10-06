"""Conventional ASGI entry point: uvicorn backend.main:app.

Configuration is read at server import, intentionally. Tests and embedding
call backend.app.create_app with injected settings/engine instead. Migrate
explicitly before starting; importing this module never creates tables.
"""

from backend.app import create_app

app = create_app()
