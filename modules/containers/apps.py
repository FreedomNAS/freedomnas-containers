"""
Apps: projects of the runtime's own app tool (Docker Compose, AppJail
Director), one dataset each.

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

What runs an app is the profile: `app_file` names the file (compose.yaml
when it names none), `compose_check` checks it on stdin as if it were in
the app's dataset, `app_files` names other files an app is made of (written
beside it, e.g. a Makejail), compose_up/down/pull/update/remove/logs run the project
(update and remove fall back to pull + up and down when missing; logs to
each member's own), and `app_members` prints the containers of a project,
one record each (see records.py).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from freedomnas import groupshares
    from freedomnas.reconcile import ActionResult
except ImportError:
    import groupshares  # type: ignore[no-redef]
    from reconcile import ActionResult  # type: ignore[no-redef]



def _load(stem: str):
    name = f"freedomnas_containers_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{stem}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


records = _load("records")

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


def _parent(runtime, engine, pool: str) -> None:
    """
    Give `<pool>/apps` a mountpoint of its own when it would inherit none
    from a pool mounted nowhere (`mountpoint=none` or `legacy`).
    """
    parent = f"{pool}/apps"
    fallback = f"/{pool}/apps"
    state = runtime.dataset_state(engine, parent)
    if state["exists"]:
        if not (state["mountpoint"] or "").startswith("/"):
            engine.execute("zfs", "dataset_set", property="mountpoint", value=fallback, dataset=parent)
        return
    pool_mountpoint = runtime.dataset_state(engine, pool)["mountpoint"]
    if pool_mountpoint and not pool_mountpoint.startswith("/"):
        engine.execute("zfs", "dataset_create_with", options=f"-p -o mountpoint={fallback}", dataset=parent)


def _mountpoint(runtime, engine, dataset: str) -> str:
    state = runtime.dataset_state(engine, dataset)
    if not state["exists"]:
        raise AppError(f"{dataset} does not exist")
    if not state["mounted"]:
        engine.execute("zfs", "mount_dataset", dataset=dataset)
    if not state["mountpoint"] or not state["mountpoint"].startswith("/"):
        raise AppError(f"{dataset} has no mountpoint")
    return state["mountpoint"]


def _profile(engine, key: str, default: Any = None) -> Any:
    try:
        return engine.get_profile_value(CATEGORY, key)
    except Exception:
        return default


def app_file_name(engine) -> str:
    return _profile(engine, "app_file", COMPOSE_FILE)


def app_files(engine) -> Dict[str, str]:
    """The profile's `app_files`: other files an app is made of, key -> file name beside the app file."""
    return dict(_profile(engine, "app_files", {}) or {})


def _named_files(engine, files: Optional[Dict[str, Any]]) -> Dict[str, str]:
    allowed = app_files(engine)
    unknown = sorted(set(files or {}) - set(allowed))
    if unknown:
        raise AppError(f"Unknown app file: {', '.join(unknown)}")
    named = {}
    for key, text in (files or {}).items():
        if text is None:
            continue
        if not isinstance(text, str):
            raise AppError(f"{allowed[key]} must be text")
        if len(text.encode()) > MAX_COMPOSE:
            raise AppError(f"{allowed[key]} is larger than 256 KiB")
        named[allowed[key]] = text
    return named


def _write_files(engine, directory: str, named: Dict[str, str]) -> None:
    for file_name, text in named.items():
        engine.execute_with_stdin(CATEGORY, "compose_write", text, file=str(Path(directory) / file_name))


def supported(engine) -> bool:
    return bool(_profile(engine, "compose_up"))


def compose_file(runtime, engine, dataset: str) -> str:
    return str(Path(_mountpoint(runtime, engine, dataset)) / app_file_name(engine))


def check_size(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise AppError("The app file is empty")
    if len(text.encode()) > MAX_COMPOSE:
        raise AppError("The app file is larger than 256 KiB")


def validate(engine, text: str, directory: str) -> None:
    """
    Check an app file with the runtime's own checker as if it were in
    `directory`, the app's dataset: its .env and relative paths are read
    from there.
    """
    check_size(text)
    try:
        engine.execute_with_stdin(CATEGORY, "compose_check", text, timeout=60, dir=directory)
    except Exception as exc:
        raise AppError(f"{app_file_name(engine)} is not valid: {_stderr(exc)}")


def _stderr(exc: Exception) -> str:
    text = str(exc)
    return text.split("Stderr:", 1)[1].strip() if "Stderr:" in text else text


def _compose(engine, action: str, name: str, file: str, timeout: int = UP_TIMEOUT, **kw):
    return engine.execute(CATEGORY, action, timeout=timeout, project=name, file=file, **kw)


def member_names(engine, name: str, file: str) -> List[str]:
    """The names of the containers of the app `name`, as `app_members` gives them."""
    return [row["name"] for row in records.run(engine, CATEGORY, "app_members", project=name, file=file)
            if row.get("name")]


def members(runtime, engine, containers: List[Dict[str, Any]], name: str, app: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        names = set(member_names(engine, name, compose_file(runtime, engine, app["dataset"])))
    except Exception:
        return []
    return [c for c in containers if c.get("name") in names]


def listing(runtime, rt, engine, config) -> List[Dict[str, Any]]:
    containers: List[Dict[str, Any]] = []
    try:
        containers = rt.containers(engine)
    except Exception:
        pass
    result = []
    for name, app in sorted(_apps(config).items()):
        own = members(runtime, engine, containers, name, app)
        running = sum(1 for c in own if c["state"] == "running")
        result.append({
            "name": name,
            "dataset": app.get("dataset"),
            "state": app.get("state", "down"),
            "containers": len(own),
            "running": running,
            "ports": ", ".join(dict.fromkeys(p for c in own for p in c["ports"].split(", ") if p)),
            "catalogue": app.get("catalogue"),
            "web_port": app.get("web_port"),
        })
    return result


def read(runtime, engine, config, name: str) -> Dict[str, Any]:
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = Path(compose_file(runtime, engine, app["dataset"]))
    files = {}
    for key, file_name in app_files(engine).items():
        other = file.with_name(file_name)
        files[key] = other.read_text() if other.exists() else ""
    return {"name": name, "dataset": app["dataset"], "state": app.get("state", "down"),
            "compose": file.read_text() if file.exists() else "", "files": files}


def create(runtime, engine, config, name: str, text: str, extra: Optional[Dict[str, Any]] = None,
           env_text: Optional[str] = None, files: Optional[Dict[str, Any]] = None) -> None:
    """
    Create the app's dataset (or take a kept one) with its compose file,
    checked in the dataset, and the other `files` the profile allows;
    env_text, when given, is written first as its .env (0600). A dataset
    created here is destroyed again on failure.
    """
    check_name(name)
    if name in _apps(config):
        raise AppError(f"An app named '{name}' already exists")
    pool = _pool(config)
    check_size(text)
    named = _named_files(engine, files)
    dataset = dataset_for(pool, name)
    _parent(runtime, engine, pool)
    reused = runtime.dataset_state(engine, dataset)["exists"]
    if not reused:
        engine.execute("zfs", "dataset_create_with", options="-p", dataset=dataset)
    try:
        file = compose_file(runtime, engine, dataset)
        if env_text is not None:
            engine.execute_with_stdin(CATEGORY, "env_write", env_text, file=str(Path(file).with_name(ENV_FILE)))
        validate(engine, text, str(Path(file).parent))
        _write_files(engine, str(Path(file).parent), named)
        engine.execute_with_stdin(CATEGORY, "compose_write", text, file=file)
    except Exception:
        if not reused:
            engine.execute("zfs", "dataset_destroy_recursive", timeout=300, dataset=dataset)
        raise
    apps = _apps(config)
    apps[name] = {**(extra or {}), "dataset": dataset, "state": "down"}
    _save(config, apps)


def update(runtime, engine, config, name: str, text: str, files: Optional[Dict[str, Any]] = None) -> None:
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    named = _named_files(engine, files)
    file = compose_file(runtime, engine, app["dataset"])
    validate(engine, text, str(Path(file).parent))
    _write_files(engine, str(Path(file).parent), named)
    engine.execute_with_stdin(CATEGORY, "compose_write", text, file=file)


def _repair(engine, name: str) -> None:
    """
    Undo what a power cut left half done before starting the app again
    (`compose_repair`: AppJail Director's project lock and an emptied copy
    of its spec, AppJail's jail locks), unless something is working on it.
    Runtimes that leave nothing behind have no such action.
    """
    if _profile(engine, "compose_repair"):
        engine.execute(CATEGORY, "compose_repair", project=name, check=False)


def set_state(runtime, engine, config, name: str, state: str) -> None:
    if state not in STATES:
        raise AppError(f"Unknown state '{state}'")
    apps = _apps(config)
    app = apps.get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = compose_file(runtime, engine, app["dataset"])
    if state == "up":
        _repair(engine, name)
    _compose(engine, "compose_up" if state == "up" else "compose_down", name, file)
    app["state"] = state
    _save(config, apps)


def refresh(runtime, engine, config, name: str) -> None:
    """Fetch the app's images again (compose_pull) and recreate it when up (compose_update, else compose_up)."""
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    file = compose_file(runtime, engine, app["dataset"])
    if _profile(engine, "compose_pull"):
        _compose(engine, "compose_pull", name, file)
    if app.get("state") == "up":
        _repair(engine, name)
        _compose(engine, "compose_update" if _profile(engine, "compose_update") else "compose_up", name, file)


def logs(runtime, engine, config, name: str, lines: int = 200, rt=None) -> str:
    """The app's logs: the profile's compose_logs, else each member's through `rt` (containers.py)."""
    app = _apps(config).get(check_name(name))
    if not app:
        raise AppError(f"No app '{name}'")
    lines = max(1, min(int(lines), 5000))
    if not _profile(engine, "compose_logs"):
        parts = []
        for member in member_names(engine, name, compose_file(runtime, engine, app["dataset"])):
            parts.append(f"== {member}\n" + rt.logs(engine, member, lines))
        return "\n".join(parts)
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
            _compose(engine, "compose_remove" if _profile(engine, "compose_remove") else "compose_down", name, file)
        if destroy_data:
            engine.execute("zfs", "dataset_destroy_recursive", timeout=300, dataset=app["dataset"])
    del apps[name]
    _save(config, apps)
    groupshares.remove_app(engine, config, name)


def _result(action: str, error: Optional[str] = None, **params) -> ActionResult:
    return ActionResult(action=f"containers.apps.{action}", params=params,
                        target_state="failed" if error else "applied", error=error)


def reconcile(runtime, rt, engine, desired: Dict[str, Any], adopt=None, moved=None) -> List[ActionResult]:
    """
    Bring up every app recorded as up whose containers are not all running,
    and take down every app recorded as down whose containers run (a
    runtime may start them at boot by itself). An app whose file for this
    runtime is missing, or that `moved(app)` says was last installed on
    another edition (its pool came from there; a file this edition wrote
    long ago may still be beside the data), is handed to
    `adopt(name, app, file)`, which makes it this edition's or answers why
    it cannot.
    """
    apps = desired.get("apps") or {}
    wanted = {name: app for name, app in apps.items() if app.get("state") == "up"}
    stopped = {name: app for name, app in apps.items() if app.get("state") != "up"}
    if not apps or not desired.get("enabled"):
        return []
    if runtime.info(engine) is None:
        return [_result(name, f"{runtime.runtime_name(engine)} is not running", app=name)
                for name in sorted(wanted)]
    try:
        containers = rt.containers(engine)
    except Exception as exc:
        return [_result("list", str(exc))]
    results: List[ActionResult] = []
    for name, app in sorted(stopped.items()):
        own = members(runtime, engine, containers, name, app)
        if not any(c["state"] == "running" for c in own):
            continue
        try:
            check_name(name)
            _compose(engine, "compose_down", name, compose_file(runtime, engine, app["dataset"]))
            results.append(_result(f"{name}.down", app=name))
        except Exception as exc:
            results.append(_result(f"{name}.down", _stderr(exc), app=name))
    for name, app in sorted(wanted.items()):
        own = members(runtime, engine, containers, name, app)
        if own and all(c["state"] == "running" for c in own):
            continue
        try:
            check_name(name)
            file = compose_file(runtime, engine, app["dataset"])
            if not Path(file).exists() or (moved and moved(app)):
                problem = adopt(name, app, file) if adopt else f"{file} is missing"
                if problem:
                    results.append(_result(name, problem, app=name))
                    continue
            _repair(engine, name)
            _compose(engine, "compose_up", name, file)
            results.append(_result(name, app=name))
        except Exception as exc:
            results.append(_result(name, _stderr(exc), app=name))
    return results
