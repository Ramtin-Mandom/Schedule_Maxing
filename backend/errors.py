"""
backend/errors.py

One error shape for every failure:

    {"error": {"code": "<machine-readable>", "message": "<human-readable>", ...details}}

Status codes: 400 bad request, 401 unauthenticated (with WWW-Authenticate),
404 not found -- also for another user's records, which are never
distinguished from records that do not exist --, 409 conflict (version
conflict, tombstone, duplicate, in use), 422 validation / invalid
reference, 503 not ready. Messages never contain passwords, tokens, hashes,
or configuration values.

This module is framework-free: the mutation, resource and account code
raises ApiError without importing FastAPI, so it also serves the direct
desktop path (app/persistence). The FastAPI handlers that render an ApiError
as an HTTP response live in backend/http_errors.py.
"""

from __future__ import annotations

from typing import Any

from pydantic_core import to_jsonable_python


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **to_jsonable_python(self.details)}}


def not_found(kind: str) -> ApiError:
    return ApiError(404, "not_found", f"No such {kind}.")


def unauthenticated(message: str = "Authentication is required.") -> ApiError:
    return ApiError(401, "unauthenticated", message)


def invalid_reference(message: str) -> ApiError:
    return ApiError(422, "invalid_reference", message)


def version_conflict(kind: str, supplied: int | None, current: dict) -> ApiError:
    deleted = current.get("deleted_at") is not None
    return ApiError(
        409,
        "deleted" if deleted else "version_conflict",
        f"The {kind} has been deleted." if deleted else f"The {kind} was changed since version {supplied}.",
        supplied_version=supplied,
        current_version=current.get("version"),
        current=current,
    )


def __getattr__(name: str):
    # Compatibility: install_error_handlers used to live here (it needs FastAPI, so it is loaded on demand).
    if name == "install_error_handlers":
        from backend.http_errors import install_error_handlers

        return install_error_handlers
    raise AttributeError(name)
