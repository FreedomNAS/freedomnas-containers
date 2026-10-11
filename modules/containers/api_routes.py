"""
/api/containers/* — the container runtime.

GET  /status   — Docker installed, running, the pool and datasets it keeps its
                 data on, the pools it can go on, and the job running
POST /install  — install Docker from the edition's packages (a job)
POST /setup    — {"pool": "tank"}: mount <pool>/containers/<name> on each of Docker's
                 data directories, copying what they held, and record it (a job)

GET    /containers                      — every container, running first
POST   /containers/run                  — {image, name, ports, volumes, env, restart} (a job)
POST   /containers/{name}/{action}      — start, stop, restart
DELETE /containers/{name}?force=true    — remove (force: also a running one)
GET    /containers/{name}/logs?lines=N  — the last N lines, with timestamps
GET    /images                          — every image
POST   /images/pull                     — {"image": "nginx:alpine"} (a job)
POST   /images/prune                    — remove the images no container uses and no tag names
DELETE /images/{image}                  — remove one image

Install, setup, pull and run can take minutes (a Gentoo install compiles
Docker, an image download is as slow as the line), so they run in a thread,
one at a time; /status reports how the last one went.
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


def _load(stem: str):
    name = f"freedomnas_containers_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{stem}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


runtime = _load("runtime")
docker = _load("docker")
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
        _job.update(state="failed", error=docker.error_text(exc))
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


def _failure(exc: Exception) -> JSONResponse:
    if isinstance(exc, docker.ContainerError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse(status_code=500, content={"detail": docker.error_text(exc)})


async def _call(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except Exception as exc:
        return _failure(exc)


@router.get("/containers")
async def list_containers(username: str = Depends(authenticate)):
    result = await _call(docker.containers, get_engine())
    return result if isinstance(result, JSONResponse) else {"containers": result}


@router.post("/containers/run")
async def run_container(body: dict, username: str = Depends(authenticate)):
    try:
        docker.check_image(body.get("image") or "")
        docker.run_args(body)
    except docker.ContainerError as exc:
        return _failure(exc)
    engine = get_engine()
    return _start_job("run", lambda: docker.run(engine, body))


@router.post("/containers/{name}/{action}")
async def container_action(name: str, action: str, username: str = Depends(authenticate)):
    result = await _call(docker.act, get_engine(), name, action)
    return result if isinstance(result, JSONResponse) else {"name": name, "action": action}


@router.delete("/containers/{name}")
async def remove_container(name: str, force: bool = False, username: str = Depends(authenticate)):
    result = await _call(docker.remove, get_engine(), name, force)
    return result if isinstance(result, JSONResponse) else {"removed": name}


@router.get("/containers/{name}/logs")
async def container_logs(name: str, lines: int = docker.LOG_LINES, username: str = Depends(authenticate)):
    result = await _call(docker.logs, get_engine(), name, lines)
    return result if isinstance(result, JSONResponse) else {"name": name, "logs": result}


@router.get("/images")
async def list_images(username: str = Depends(authenticate)):
    result = await _call(docker.images, get_engine())
    return result if isinstance(result, JSONResponse) else {"images": result}


@router.post("/images/pull")
async def pull_image(body: dict, username: str = Depends(authenticate)):
    image = str(body.get("image") or "").strip()
    try:
        docker.check_image(image)
    except docker.ContainerError as exc:
        return _failure(exc)
    engine = get_engine()
    return _start_job("pull", lambda: docker.pull(engine, image))


@router.post("/images/prune")
async def prune_images(username: str = Depends(authenticate)):
    result = await _call(docker.prune_images, get_engine())
    return result if isinstance(result, JSONResponse) else {"output": result}


@router.delete("/images/{image:path}")
async def remove_image(image: str, username: str = Depends(authenticate)):
    result = await _call(docker.remove_image, get_engine(), image)
    return result if isinstance(result, JSONResponse) else {"removed": image}
