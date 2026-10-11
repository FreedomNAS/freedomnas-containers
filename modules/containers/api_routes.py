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

GET    /apps                            — every app with its containers running
POST   /apps                            — {name, compose, start}: a dataset <pool>/apps/<name>
                                          holding compose.yaml; start brings it up (a job)
GET    /apps/{name}                     — the app and its compose file
PUT    /apps/{name}                     — {compose}: replace the compose file; a running app
                                          is brought up again with it (a job)
POST   /apps/{name}/{action}            — up, down, update (pull its images, then up) (jobs)
GET    /apps/{name}/logs?lines=N        — the last N lines of every container of the app
DELETE /apps/{name}?destroy_data=true   — take it down and forget it; destroy_data also
                                          destroys its dataset

GET    /catalogue                       — the ready-made apps
GET    /catalogue/{id}                  — one of them, with its settings, their defaults
                                          for this NAS and the accounts it can run as
POST   /catalogue/{id}/install          — {name, values}: create it as an app and bring it
                                          up (a job); answers with the values to show once

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
apps = _load("apps")
catalogue = _load("catalogue")
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
    if isinstance(exc, (docker.ContainerError, apps.AppError, catalogue.CatalogueError)):
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


APP_ACTIONS = ("up", "down", "update")


@router.get("/apps")
async def list_apps(username: str = Depends(authenticate)):
    result = await _call(apps.listing, runtime, docker, get_engine(), get_config())
    return result if isinstance(result, JSONResponse) else {"apps": result}


@router.post("/apps")
async def create_app(body: dict, username: str = Depends(authenticate)):
    name = str(body.get("name") or "").strip()
    engine, config = get_engine(), get_config()
    result = await _call(apps.create, runtime, engine, config, name, body.get("compose") or "")
    if isinstance(result, JSONResponse):
        return result
    if body.get("start"):
        return _start_job("app", lambda: apps.set_state(runtime, engine, config, name, "up"))
    return {"created": name}


@router.get("/apps/{name}")
async def get_app(name: str, username: str = Depends(authenticate)):
    return await _call(apps.read, runtime, get_engine(), get_config(), name)


@router.put("/apps/{name}")
async def replace_app(name: str, body: dict, username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    result = await _call(apps.update, runtime, engine, config, name, body.get("compose") or "")
    if isinstance(result, JSONResponse):
        return result
    current = await _call(apps.read, runtime, engine, config, name)
    if not isinstance(current, JSONResponse) and current["state"] == "up":
        return _start_job("app", lambda: apps.set_state(runtime, engine, config, name, "up"))
    return {"updated": name}


@router.post("/apps/{name}/{action}")
async def app_action(name: str, action: str, username: str = Depends(authenticate)):
    if action not in APP_ACTIONS:
        return JSONResponse(status_code=422, content={"detail": f"Unknown action '{action}'"})
    engine, config = get_engine(), get_config()
    current = await _call(apps.read, runtime, engine, config, name)
    if isinstance(current, JSONResponse):
        return current
    if action == "update":
        return _start_job("app", lambda: apps.refresh(runtime, engine, config, name))
    return _start_job("app", lambda: apps.set_state(runtime, engine, config, name, action))


@router.get("/apps/{name}/logs")
async def app_logs(name: str, lines: int = 200, username: str = Depends(authenticate)):
    result = await _call(apps.logs, runtime, get_engine(), get_config(), name, lines)
    return result if isinstance(result, JSONResponse) else {"name": name, "logs": result}


@router.delete("/apps/{name}")
async def remove_app(name: str, destroy_data: bool = False, username: str = Depends(authenticate)):
    result = await _call(apps.remove, runtime, get_engine(), get_config(), name, destroy_data)
    return result if isinstance(result, JSONResponse) else {"removed": name, "destroyed": destroy_data}


@router.get("/catalogue")
async def list_catalogue(username: str = Depends(authenticate)):
    return {"entries": [catalogue.summary(item) for item in catalogue.entries()]}


def _catalogue_entry(entry_id: str, username: str) -> dict:
    item = catalogue.entry(entry_id)
    engine, config = get_engine(), get_config()
    taken = catalogue.used_ports(docker, engine, config=config)
    installed = set(config.get_module_config("containers").get("apps") or {})
    name = item["id"]
    n = 2
    while name in installed:
        name, n = f"{item['id']}{n}", n + 1
    return {**catalogue.summary(item), "app_name": name,
            "defaults": catalogue.defaults(item, config, username, taken),
            "users": catalogue._accounts(config),
            "folders": catalogue.pool_folders(engine)}


@router.get("/catalogue/{entry_id}")
async def get_catalogue_entry(entry_id: str, username: str = Depends(authenticate)):
    return await _call(_catalogue_entry, entry_id, username)


@router.post("/catalogue/{entry_id}/install")
async def install_from_catalogue(entry_id: str, body: dict, username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    name = str(body.get("name") or entry_id).strip()
    values = body.get("values")
    if values is None:
        defaults = await _call(_catalogue_entry, entry_id, username)
        if isinstance(defaults, JSONResponse):
            return defaults
        values = defaults["defaults"]
    result = await _call(catalogue.install, runtime, apps, docker, engine, config, entry_id, name, values)
    if isinstance(result, JSONResponse):
        return result
    job = _start_job("app", lambda: apps.set_state(runtime, engine, config, name, "up"))
    if isinstance(job, JSONResponse):
        return job
    return {**result, **job}
