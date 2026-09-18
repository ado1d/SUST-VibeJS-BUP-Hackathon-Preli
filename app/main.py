"""GridWise LLM API service.

Endpoints (exact names required by the judge):
    GET  /health          -> {"status": "ok"}
    POST /optimize-energy -> interpretation + 24-hour schedule JSON

Error behavior (Problem Statement 6.1):
    400 — malformed JSON or structurally invalid request (controlled JSON body)
    422 — semantically invalid but well-formed request (optional, we use it)
    500 — controlled internal error; never leaks secrets or stack traces
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .planner import optimize_scenario
from .schemas import ScenarioRequest

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gridwise.api")

app = FastAPI(
    title="GridWise LLM — Smart Campus Energy Optimization",
    description="LLM-assisted operator directive interpretation + exact LP scheduling",
    version="1.0.0",
)


@app.get("/health")
def health():
    """Judge readiness probe. Lightweight by design — no LLM call here."""
    return {"status": "ok"}


@app.post("/optimize-energy")
def optimize_energy(payload: ScenarioRequest):
    # sync def -> FastAPI runs this in a threadpool; the event loop never blocks
    response = optimize_scenario(payload)
    return JSONResponse(status_code=200, content=response)


# ------------------------------------------------------------ error handling

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Structural failures -> controlled 400 (spec: malformed/structurally invalid)."""
    details = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", []))
        details.append(f"{loc}: {e.get('msg', 'invalid value')}")
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "invalid_request", "message": "Request failed schema validation.", "details": details[:10]}},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Semantic dead ends -> controlled 422 (spec: optional semantic code)."""
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "semantically_invalid", "message": str(exc)[:300]}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """Never leak stack traces or secrets. Controlled 500 only."""
    logger.error("unhandled error on %s: %s", request.url.path, type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "The service could not process this request."}},
    )
