"""
Apps: Docker Compose projects, one dataset each.

An app named `nextcloud` lives on `<pool>/apps/nextcloud`, on the pool the
runtime uses, with its compose file at `<mountpoint>/compose.yaml`; bind
mounts written relative to it (`./data:/data`) stay inside the app's
dataset, so the app is snapshotted, replicated and restored with it. The
project name is the app's name.

Configuration, under `modules.containers.apps`:
    <name>:
      dataset  str  <pool>/apps/<name>
      state    str  "up" (brought up again at boot) or "down"
      catalogue, web_port, ports   for apps installed from the catalogue: the
                                   ports stay taken while the app is down

The compose file itself is on the pool, not in config.yaml. A removed app
whose dataset was kept is picked up again by a new app of the same name,
with its data; the new compose file replaces the old one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from freedomnas.reconcile import ActionResult
except ImportError:
    from reconcile import ActionResult  # type: ignore[no-redef]

CATEGORY = "containers"
COMPOSE_FILE = "compose.yaml"
ENV_FILE = ".env"
STATES = ("up", "down")
UP_TIMEOUT = 3600
MAX_COMPOSE = 256 * 1024

_APP = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")



class AppError(Exception):
    pass


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _APP.fullmatch(name):
        raise AppError(f"'{name}' is not an app name: lowercase letters, digits, '-' and '_'")
    return name


def _section(config) -> Dict[str, Any]:
    return config.get_module_config("containers")


def _apps(config) -> Dict[str, Dict[str, Any]]:
    return dict(_section(config).get("apps") or {})


def _save(config, apps: Dict[str, Dict[str, Any]]) -> None:
    section = _section(config)
    section["apps"] = apps
    config.set_module_config("containers", section)


def _pool(config) -> str:
    section = _section(config)
    if not section.get("enabled") or not section.get("pool"):
        raise AppError("Set up the containers runtime first")
    return section["pool"]


def dataset_for(pool: str, name: str) -> str:
    return f"{pool}/apps/{name}"


def _mountpoint(runtime, engine, dataset: str) -> str:
    state = runtime.dataset_state(engine, dataset)
    if not state["exists"]:
        raise AppError(f"{dataset} does not exist")
    if not state["mounted"]:
        engine.execute("zfs", "mount_dataset", dataset=dataset)
    if not state["mountpoint"] or not state["mountpoint"].startswith("/"):
        raise AppError(f"{dataset} has no mountpoint")
    return state["mountpoint"]


def compose_file(runtime, engine, dataset: str) -> str:
    return str(Path(_mountpoint(runtime, engine, dataset)) / COMPOSE_FILE)


def check_size(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise AppError("The compose file is empty")
    if len(text.encode()) > MAX_COMPOSE:
        raise AppError("The compose file is larger than 256 KiB")


def validate(engine, text: str, directory: str) -> None:
    """
    Check a compose file with Docker Compose as if it were in `directory`,
    the app's dataset: its .env and relative paths are read from there.
    """
    check_size(text)
    try:
        engine.execute_with_stdin(CATEGORY, "compose_check", text, timeout=60, dir=directory)
    except Exception as exc:
        raise AppError(f"The compose file is not valid: {_stderr(exc)}")


def _stderr(exc: Exception) -> str:
    text = str(exc)
    return text.split("Stderr:", 1)[1].strip() if "Stderr:" in text else text


def _compose(engine, action: str, name: str, file: str, timeout: int = UP_TIMEOUT, **kw):
    return engine.execute(CATEGORY, action, timeout=timeout, project=name, file=file, **kw)


def listing(runtime, docker, engine, config) -> List[Dict[str, Any]]:
    containers: List[Dict[str, Any]] = []
    try:
        containers = docker.containers(engine)
    except Exception:
        pass
    result = []
    for name, app in sorted(_apps(config).items()):
        own = [c for c in containers if c.get("project") == name]
        running = sum(1 for c in own if c["state"] == "running")
        result.append({
            "name": name,
            "dataset": app.get("dataset"),
            "state": app.get("state", "down"),
            "containers": len(own),
            "running": running,
            "ports": docker.short_ports(", ".join(c["published"] for c in own if c["published"])),
            "catalogue": app.get("catalogue"),
            "web_port": app.get("web_port"),
        })
    return result


def read(runtime, engine, config, name: str) -> Dict[str, Any]:
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = Path(compose_file(runtime, engine, app["dataset"]))
    return {"name": name, "dataset": app["dataset"], "state": app.get("state", "down"),
            "compose": file.read_text() if file.exists() else ""}


def create(runtime, engine, config, name: str, text: str, extra: Optional[Dict[str, Any]] = None,
           env_text: Optional[str] = None) -> None:
    """
    Create the app's dataset (or take a kept one) with its compose file,
    checked in the dataset; env_text, when given, is written first as its
    .env (0600). A dataset created here is destroyed again on failure.
    """
    check_name(name)
    if name in _apps(config):
        raise AppError(f"An app named '{name}' already exists")
    pool = _pool(config)
    check_size(text)
    dataset = dataset_for(pool, name)
    reused = runtime.dataset_state(engine, dataset)["exists"]
    if not reused:
        engine.execute("zfs", "dataset_create_with", options="-p", dataset=dataset)
    try:
        file = compose_file(runtime, engine, dataset)
        if env_text is not None:
            engine.execute_with_stdin(CATEGORY, "env_write", env_text, file=str(Path(file).with_name(ENV_FILE)))
        validate(engine, text, str(Path(file).parent))
        engine.execute_with_stdin(CATEGORY, "compose_write", text, file=file)
    except Exception:
        if not reused:
            engine.execute("zfs", "dataset_destroy_recursive", timeout=300, dataset=dataset)
        raise
    apps = _apps(config)
    apps[name] = {**(extra or {}), "dataset": dataset, "state": "down"}
    _save(config, apps)


def update(runtime, engine, config, name: str, text: str) -> None:
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = compose_file(runtime, engine, app["dataset"])
    validate(engine, text, str(Path(file).parent))
    engine.execute_with_stdin(CATEGORY, "compose_write", text, file=file)


def set_state(runtime, engine, config, name: str, state: str) -> None:
    if state not in STATES:
        raise AppError(f"Unknown state '{state}'")
    apps = _apps(config)
    app = apps.get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = compose_file(runtime, engine, app["dataset"])
    _compose(engine, "compose_up" if state == "up" else "compose_down", name, file)
    app["state"] = state
    _save(config, apps)


def refresh(runtime, engine, config, name: str) -> None:
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = compose_file(runtime, engine, app["dataset"])
    _compose(engine, "compose_pull", name, file)
    if app.get("state") == "up":
        _compose(engine, "compose_up", name, file)


def logs(runtime, engine, config, name: str, lines: int = 200) -> str:
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    lines = max(1, min(int(lines), 5000))
    out, err = _compose(engine, "compose_logs", name, compose_file(runtime, engine, app["dataset"]),
                        timeout=60, lines=str(lines))
    return "\n".join(part for part in (out, err) if part)


def remove(runtime, engine, config, name: str, destroy_data: bool = False) -> None:
    apps = _apps(config)
    app = apps.get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    state = runtime.dataset_state(engine, app["dataset"])
    if state["exists"]:
        file = compose_file(runtime, engine, app["dataset"])
        if Path(file).exists():
            _compose(engine, "compose_down", name, file)
        if destroy_data:
            engine.execute("zfs", "dataset_destroy_recursive", timeout=300, dataset=app["dataset"])
    del apps[name]
    _save(config, apps)


def _result(action: str, error: Optional[str] = None, **params) -> ActionResult:
    return ActionResult(action=f"containers.apps.{action}", params=params,
                        target_state="failed" if error else "applied", error=error)


def reconcile(runtime, docker, engine, desired: Dict[str, Any]) -> List[ActionResult]:
    apps = desired.get("apps") or {}
    wanted = {name: app for name, app in apps.items() if app.get("state") == "up"}
    if not wanted or not desired.get("enabled"):
        return []
    if runtime.info(engine) is None:
        return [_result(name, "Docker is not running", app=name) for name in sorted(wanted)]
    try:
        containers = docker.containers(engine)
    except Exception as exc:
        return [_result("list", str(exc))]
    results: List[ActionResult] = []
    for name, app in sorted(wanted.items()):
        own = [c for c in containers if c.get("project") == name]
        if own and all(c["state"] == "running" for c in own):
            continue
        try:
            check_name(name)
            file = compose_file(runtime, engine, app["dataset"])
            if not Path(file).exists():
                results.append(_result(name, f"{file} is missing", app=name))
                continue
            _compose(engine, "compose_up", name, file)
            results.append(_result(name, app=name))
        except Exception as exc:
            results.append(_result(name, _stderr(exc), app=name))
    return results
