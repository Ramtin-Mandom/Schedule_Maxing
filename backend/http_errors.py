"""
backend/http_errors.py

The HTTP side of backend/errors.py: FastAPI exception handlers that turn an
ApiError (and FastAPI's own validation/HTTP errors) into the one JSON error
shape. Only the HTTP adapters (backend/app.py, app/web) import this module;
the reusable mutation, resource and account code raises plain ApiErrors and
never needs FastAPI.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.errors import ApiError

LOG = logging.getLogger("schedule_maxing.api")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, error: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        if error.status == 429 and "retry_after" in error.details:
            headers = {"Retry-After": str(error.details["retry_after"])}
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

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, error: Exception) -> JSONResponse:
        # Never a traceback, a query, a value or a setting: the class name goes to the server log only.
        LOG.error("unhandled server error (%s)", type(error).__name__)
        return JSONResponse({"error": {"code": "internal_error", "message": "An unexpected error occurred."}},
                            status_code=500)
