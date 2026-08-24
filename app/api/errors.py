"""Error envelope and exception handlers — SPEC.md §5.

Every non-2xx uses one shape:

    {"error": {"code": ..., "message": ..., ...extra}}

Uniform because a client should need one parser, not one per endpoint. R7
requires failures be named and explicit, and a bare 500 with an HTML body is
neither.

Tracebacks are never returned. `internal_error` returns a generic message and
logs the detail against the job id.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.concepts.aliases import ResolutionError
from app.logging import get_logger

log = get_logger(__name__)


def envelope(code: str, message: str, **extra: object) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


class APIError(Exception):
    """A named, client-safe failure raised from a route."""

    def __init__(self, status_code: int, code: str, message: str, **extra: object):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


def not_found(job_id: str) -> APIError:
    return APIError(404, "not_found", f"No job with id {job_id}.")


_ROUTING_CODES = {404: "not_found", 405: "method_not_allowed"}


def _routing_message(request: Request, exc: StarletteHTTPException) -> str:
    """Starlette's own text is "Not Found" - true, but it does not say what
    was not found. The envelope is the only thing a client sees."""
    if exc.status_code == 404:
        return f"No route matches {request.method} {request.url.path}."
    if exc.status_code == 405:
        return f"{request.method} is not allowed on {request.url.path}."
    return str(exc.detail)


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(ResolutionError)
    async def _resolution(request: Request, exc: ResolutionError) -> JSONResponse:
        """Rejection happens before any spend (SPEC.md §5)."""
        payload = envelope(exc.code, exc.message)
        if exc.supported_concepts:
            payload["error"]["supported_concepts"] = exc.supported_concepts
        return JSONResponse(status_code=400, content=payload)

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(exc.code, exc.message, **exc.extra),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Unknown path, wrong verb, unreadable body.

        Starlette answers these in the router, before any route function
        runs, so they never reach `APIError` and came back as
        `{"detail": ...}` - the one shape that escaped SPEC.md §5's
        "every non-2xx uses one shape".
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(
                _ROUTING_CODES.get(exc.status_code, "invalid_request"),
                _routing_message(request, exc),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's default 422 body is a different shape from everything
        else; mapping it keeps the envelope genuinely uniform."""
        return JSONResponse(
            status_code=400,
            content=envelope(
                "invalid_request",
                "The request body was malformed.",
                detail=[
                    {"field": ".".join(str(p) for p in err["loc"]), "error": err["msg"]}
                    for err in exc.errors()
                ],
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("api.internal_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content=envelope(
                "internal_error", "The server failed to handle the request."
            ),
        )
