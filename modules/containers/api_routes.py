"""
/api/containers/* — the container runtime.

GET  /status   — Docker installed, running, the pool and datasets it keeps its
                 data on, the pools it can go on, and the job running
POST /install  — install Docker from the edition's packages (a job)
POST /setup    — {"pool": "tank"}: mount <pool>/containers/<name> on each of Docker's
                 data directories, copying what they held, and record it (a job)

Install and setup can take minutes (a Gentoo install compiles Docker), so
they run in a thread, one at a time; /status reports how the last one went.
"""

import asyncio
import importlib.util
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.deps import authenticate, get_config, get_engine


def _load_runtime():
    name = "freedomnas_containers_runtime"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("runtime.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


runtime = _load_runtime()
router = APIRouter()

_lock = threading.Lock()
_job: dict = {"kind": None, "state": "idle", "error": None, "started": None, "finished": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(kind: str, work) -> None:
    try:
        work()
        _job.update(state="done", error=None)
    except Exception as exc:
        runtime.logger.error("Containers %s failed: %s", kind, exc)
        _job.update(state="failed", error=str(exc))
    finally:
        _job["finished"] = _now()


def _start_job(kind: str, work):
    with _lock:
        if _job["state"] == "running":
            return JSONResponse(status_code=409, content={"detail": f"A {_job['kind']} is already running"})
        _job.update(kind=kind, state="running", error=None, started=_now(), finished=None)
    threading.Thread(target=_run, args=(kind, work), daemon=True).start()
    return {"job": dict(_job)}


@router.get("/status")
async def status(username: str = Depends(authenticate)):
    result = await asyncio.to_thread(runtime.status, get_engine(), get_config())
    result["job"] = dict(_job)
    return result


@router.post("/install")
async def install(username: str = Depends(authenticate)):
    engine = get_engine()
    if await asyncio.to_thread(runtime.installed, engine):
        return JSONResponse(status_code=409, content={"detail": "Docker is already installed"})
    return _start_job("install", lambda: runtime.install(engine))


@router.post("/setup")
async def setup(body: dict, username: str = Depends(authenticate)):
    pool = str(body.get("pool") or "").strip()
    if not pool:
        return JSONResponse(status_code=422, content={"detail": "pool is required"})
    engine, config = get_engine(), get_config()
    pools = await asyncio.to_thread(runtime.pool_names, engine)
    if pool not in pools:
        return JSONResponse(status_code=422, content={"detail": f"Pool '{pool}' is not imported"})
    return _start_job("setup", lambda: runtime.setup(engine, config, pool))
