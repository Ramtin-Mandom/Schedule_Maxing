"""Static layering of the direct path: app/persistence and the backend modules it
reuses import no HTTP framework, server, JWT or local-web code (the runtime
counterpart is test_direct_services.test_direct_services_run_with_every_http_package_unavailable),
and the shared domain layers still import nothing from app/persistence."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTTP_STACK = ("fastapi", "starlette", "uvicorn", "httpx", "jwt", "app.web", "app.ui", "tkinter", "customtkinter",
              "backend.api", "backend.app", "backend.sync", "backend.security", "backend.planning_api",
              "backend.browser_sessions", "backend.http_errors")
#: The backend modules the direct path imports (directly or through each other).
REUSED_BACKEND = ("accounts", "database", "errors", "executions", "migrate", "models", "mutations", "passwords",
                  "planning_repository", "preferences", "record_mapping", "resources", "settings", "snapshots")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _bad(paths: list[Path], forbidden: tuple[str, ...]) -> dict[str, list[str]]:
    result = {}
    for path in paths:
        bad = sorted(name for name in _imports(path) if any(name == p or name.startswith(p + ".") for p in forbidden))
        if bad:
            result[str(path.relative_to(ROOT))] = bad
    return result


def test_the_direct_path_imports_no_http_or_jwt_code() -> None:
    paths = sorted((ROOT / "app" / "persistence").rglob("*.py"))
    paths += [ROOT / "backend" / f"{name}.py" for name in REUSED_BACKEND]
    # backend.errors keeps the old install_error_handlers import path working through an on-demand import
    # inside __getattr__ (it only runs when the HTTP adapter asks for that name).
    assert _bad(paths, HTTP_STACK) == {str(Path("backend/errors.py")): ["backend.http_errors"]}


def test_shared_layers_and_the_desktop_ui_do_not_import_the_direct_path() -> None:
    folders = ("app/planning", "app/execution", "app/sync", "app/productivity", "config")
    paths = [path for folder in folders for path in sorted((ROOT / folder).rglob("*.py"))]
    assert _bad(paths, ("app.persistence",)) == {}
