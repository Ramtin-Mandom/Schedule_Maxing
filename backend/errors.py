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
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **jsonable_encoder(self.details)}}


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


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, error: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse(error.body(), status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, error: RequestValidationError) -> JSONResponse:
        problems = [
            {"location": [str(part) for part in item.get("loc", ())], "message": str(item.get("msg", ""))}
            for item in error.errors()
        ]
        # Only locations and messages are echoed -- never the submitted values (they may contain a password).
        body = {"error": {"code": "validation_error", "message": "The request is not valid.", "problems": problems}}
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        return JSONResponse({"error": {"code": code, "message": str(error.detail)}}, status_code=error.status_code)
