"""FastAPI application.

Routes are intentionally few:

    GET    /                 the dashboard
    GET    /api/health       server + sandbox status
    GET    /api/reference    schemas, strategies, comparison modes, examples
    GET    /api/examples/{id}  one full example payload
    POST   /api/runs         start a comparison, returns a job id
    GET    /api/runs/{id}    job state (progress, then the report)
    DELETE /api/runs/{id}    forget a finished job

Nothing here executes user code; that only ever happens inside
``app.sandbox`` in a separate, limited process.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .config import DEFAULT_LIMITS, MAX_ALLOWED_LIMITS, STATIC_DIR
from .examples import get_example, list_examples
from .jobs import get_store
from .models import JobAccepted, RunRequest
from .pipeline import run_comparison
from .sandbox.capabilities import probe_sandbox
from .testgen import describe_schemas, describe_strategies


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    # Probe isolation support once at start-up so the first comparison is not
    # slower than the rest and the UI can show real capabilities.
    probe_sandbox()
    yield
    get_store().shutdown()


app = FastAPI(
    title="Algorithm Arena",
    version=__version__,
    description="Empirical equivalence and performance comparison for two algorithm implementations.",
    lifespan=lifespan,
)


@app.exception_handler(ValidationError)
async def validation_handler(_request: Request, exc: ValidationError) -> JSONResponse:
    """Turn pydantic errors into something a person can act on."""
    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        messages.append(f"{location or 'request'}: {error.get('msg')}")
    return JSONResponse(status_code=422, content={"detail": "; ".join(messages) or "invalid request"})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    capabilities = probe_sandbox()
    return {
        "status": "ok",
        "version": __version__,
        "python": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
        "time": time.time(),
        "sandbox": capabilities.to_dict(),
        "default_limits": DEFAULT_LIMITS.to_dict(),
        "max_limits": MAX_ALLOWED_LIMITS.to_dict(),
    }


@app.get("/api/reference")
async def reference() -> dict:
    from .compare import COMPARISON_MODES, DEFAULT_ABS_TOL, DEFAULT_REL_TOL

    return {
        "schemas": describe_schemas(),
        "strategies": describe_strategies(),
        "comparison_modes": [{"id": key, **value} for key, value in COMPARISON_MODES.items()],
        "default_tolerances": {"rel": DEFAULT_REL_TOL, "abs": DEFAULT_ABS_TOL},
        "examples": list_examples(),
        "languages": [{"id": "python", "label": "Python", "enabled": True}],
    }


@app.get("/api/examples/{example_id}")
async def example(example_id: str) -> dict:
    payload = get_example(example_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no example called {example_id!r}")
    return payload


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@app.post("/api/runs", response_model=JobAccepted, status_code=202)
async def create_run(payload: RunRequest) -> JobAccepted:
    problems = _validate(payload)
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))

    store = get_store()
    job = store.create()

    def work(progress):
        return run_comparison(payload, job.run_id, progress)

    store.submit(job, work)
    return JobAccepted(run_id=job.run_id, status="queued", created_at=job.created_at)


def _validate(payload: RunRequest) -> list[str]:
    """Catch the mistakes that would otherwise surface as a confusing crash."""
    problems: list[str] = []
    if not payload.algorithm_a.code.strip():
        problems.append("Algorithm A is empty")
    if not payload.algorithm_b.code.strip():
        problems.append("Algorithm B is empty")
    if payload.algorithm_a.code.strip() == payload.algorithm_b.code.strip():
        problems.append(
            "Algorithm A and Algorithm B are identical - paste two different implementations"
        )
    if payload.protocol == "function":
        for side, spec in (("A", payload.algorithm_a), ("B", payload.algorithm_b)):
            if f"def {payload.entry_point}" not in spec.code and f"{payload.entry_point} =" not in spec.code:
                problems.append(
                    f"Algorithm {side} does not appear to define a function called "
                    f"{payload.entry_point!r}"
                )
    return problems


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    job = get_store().get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown run id (it may have expired)")
    return job.to_dict()


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict:
    store = get_store()
    job = store.get(run_id)
    if job is None:
        return {"deleted": False}
    store.update(run_id, result=None)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Static assets (mounted last so the API routes win)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
