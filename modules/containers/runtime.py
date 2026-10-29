"""
The container runtime: installed from the edition's packages, with the
directories it keeps its data in (`data_dirs` in the profile, e.g.
/var/lib/docker and /var/lib/containerd for Docker) mounted from ZFS
datasets of a chosen pool, `<pool>/containers/<name>`. They sit outside the
root dataset, so containers and images survive a rollback of the system and
go along with the pool's snapshots and replication.

The datasets are `canmount=on`, so a pool imported at boot gets them
mounted by the system's own ZFS mount service before any service starts.
Whether the runtime's services start at boot by themselves or wait for the
module's reconcile() is the profile's `boot_services`; what the host needs
besides (configuration files, firewall, networks) is its `config_files`,
`configure` and `configure_services`, applied at setup and at every boot.

What a directory held before setup is moved aside to `<dir>.pre-freedomnas`
(or `<dir>.pre-freedomnas-<date>` when that exists already) and copied
onto its dataset; the old copies are never deleted by the module.

Configuration, `modules.containers`:
    enabled   bool  the runtime was set up from the WebUI
    pool      str   the pool holding the datasets
    datasets  dict  directory name -> dataset, e.g. docker -> tank/containers/docker
    apps      dict  see apps.py
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
    from freedomnas.package_manager import PackageManager
    from freedomnas.reconcile import ActionResult
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]
    from package_manager import PackageManager  # type: ignore[no-redef]
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

logger = logging.getLogger("FreedomNAS.Module.Containers")

MODULE_ID = "containers"
CATEGORY = "containers"
PACKAGES = "containers"
ASIDE = ".pre-freedomnas"
ENGINE_ERRORS = (CommandExecutionError, CommandTranslationError)
START_WAIT = 30


class ContainersError(Exception):
    pass


def _value(engine, key: str, default: Any = None) -> Any:
    try:
        return engine.get_profile_value(CATEGORY, key)
    except CommandTranslationError:
        return default


def backend(engine) -> str:
    return _value(engine, "backend", "")


def runtime_name(engine) -> str:
    return _value(engine, "runtime_name", "The container runtime")


def ui(engine) -> Dict[str, Any]:
    return dict(_value(engine, "ui", {}))


def data_dirs(engine) -> Dict[str, str]:
    return dict(_value(engine, "data_dirs", {}))


def datasets_for(engine, pool: str) -> Dict[str, str]:
    return {name: f"{pool}/containers/{name}" for name in data_dirs(engine)}


def services(engine) -> List[str]:
    names = PackageManager().get_services_for_os(PACKAGES, engine.os_type, engine.init_system)
    order = list(_value(engine, "services_order", []))

    def rank(name: str) -> int:
        return next((i for i, part in enumerate(order) if part in name), len(order))
    return sorted(names, key=rank)


def packages(engine) -> List[str]:
    return PackageManager().get_packages_for_os(PACKAGES, engine.os_type, engine.init_system)


def installed(engine) -> bool:
    try:
        engine.execute(CATEGORY, "runtime_check")
        return True
    except ENGINE_ERRORS:
        return False


def pool_names(engine) -> List[str]:
    out, _ = engine.execute("zfs", "pool_list")
    return [line.split("\t")[0] for line in out.splitlines() if line.strip()]


def _property(engine, dataset: str, name: str) -> Optional[str]:
    try:
        out, _ = engine.execute("zfs", "dataset_get_property", property=name, dataset=dataset)
    except ENGINE_ERRORS:
        return None
    return out.split("\t")[0].strip() or None


def dataset_state(engine, dataset: str) -> Dict[str, Any]:
    mountpoint = _property(engine, dataset, "mountpoint")
    return {
        "exists": mountpoint is not None,
        "mountpoint": mountpoint,
        "mounted": _property(engine, dataset, "mounted") == "yes",
    }


def running(engine) -> bool:
    if _value(engine, "running_check"):
        try:
            engine.execute(CATEGORY, "running_check")
            return True
        except ENGINE_ERRORS:
            return False
    for name in services(engine):
        try:
            out, _ = engine.execute("services", "is_running", check=False, service_name=name)
        except ENGINE_ERRORS:
            return False
        if out.strip() != "yes":
            return False
    return True


def info(engine) -> Optional[Dict[str, Any]]:
    """version, data_root, driver, containers, images: None when the runtime does not answer."""
    try:
        rows = records.run(engine, CATEGORY, "info")
    except (ENGINE_ERRORS + (ValueError,)):
        return None
    return rows[0] if rows else None


def _boot_units(engine) -> List[str]:
    return list(_value(engine, "socket_units", [])) + services(engine)


def keep_from_boot(engine) -> None:
    """
    The profile's `boot_services`: "disable" leaves the runtime to the
    reconciler, which mounts its datasets first; "enable" keeps its services
    starting at boot, when the pool is imported and mounted before them.
    """
    action = _value(engine, "boot_services", "disable")
    for name in _boot_units(engine):
        engine.execute("services", action, check=False, service_name=name)


def stop(engine) -> None:
    for name in list(_value(engine, "socket_units", [])) + list(reversed(services(engine))):
        engine.execute("services", "stop", check=False, service_name=name)


def start(engine) -> None:
    for name in services(engine):
        engine.execute("services", "start", service_name=name)
    deadline = time.monotonic() + START_WAIT
    while info(engine) is None:
        if time.monotonic() > deadline:
            raise ContainersError(f"{runtime_name(engine)} did not answer after it was started")
        time.sleep(1)


def _has_entries(path: str) -> bool:
    try:
        return any(Path(path).iterdir())
    except OSError:
        return False


def mount_at_boot(engine, dataset: str) -> List[str]:
    if _property(engine, dataset, "canmount") == "on":
        return []
    engine.execute("zfs", "dataset_set", property="canmount", value="on", dataset=dataset)
    return ["canmount=on"]


def place(engine, dataset: str, path: str) -> List[str]:
    """
    Mount `dataset` on `path`, creating it when missing; whatever `path`
    held on the root dataset is moved aside first and copied onto it. A new
    dataset is created unmounted (FreeBSD would mount it at once, over what
    the directory holds) and gets canmount=on once it is in place.
    Returns what was done. The runtime must be stopped.
    """
    done: List[str] = []
    state = dataset_state(engine, dataset)
    if state["mounted"]:
        if state["mountpoint"] != path:
            raise ContainersError(f"{dataset} is mounted on {state['mountpoint']}, not {path}")
        return done + mount_at_boot(engine, dataset)
    if not state["exists"]:
        engine.execute("zfs", "dataset_create_with",
                       options=f"-p -o canmount=noauto -o mountpoint={path}", dataset=dataset)
        done.append("created")
    elif state["mountpoint"] != path:
        raise ContainersError(f"{dataset} belongs on {state['mountpoint']}, not {path}")
    else:
        done += mount_at_boot(engine, dataset)
    aside = None
    if _has_entries(path):
        aside = path + ASIDE
        if Path(aside).exists():
            aside = f"{path}{ASIDE}-{time.strftime('%Y%m%d-%H%M%S')}"
        if Path(aside).exists():
            raise ContainersError(f"{path} is not empty and {aside} already exists")
        engine.execute(CATEGORY, "data_move", source=path, target=aside)
        done.append("moved aside")
    engine.execute("zfs", "mount_dataset", dataset=dataset)
    done.append("mounted")
    if aside and not _has_entries(path):
        engine.execute(CATEGORY, "data_copy", timeout=7200, source=aside, target=path)
        done.append("copied")
    return done + mount_at_boot(engine, dataset)


def facts(engine) -> Dict[str, str]:
    """
    The profile's `facts`: name -> action (or {action, missing}) printing one
    value of this system, e.g. the interface of the default route.
    """
    found = {}
    for key, entry in (_value(engine, "facts") or {}).items():
        entry = entry if isinstance(entry, dict) else {"action": entry}
        out, _ = engine.execute(CATEGORY, entry["action"], check=False)
        value = out.strip()
        if not value:
            raise ContainersError(entry.get("missing") or f"Cannot find the {key} of this system")
        found[key] = value
    return found


def variables(engine, pool: str, datasets: Dict[str, str]) -> Dict[str, str]:
    values = {"pool": pool, **facts(engine)}
    dirs = data_dirs(engine)
    for name, dataset in datasets.items():
        values[f"{name}_dataset"] = dataset
        values[f"{name}_dataset_name"] = dataset.split("/", 1)[1]
        values[f"{name}_path"] = dirs.get(name, "")
    return values


def file_text(current: str, entry: Dict[str, Any], values: Dict[str, str]) -> str:
    """
    A `config_files` entry: {path, lines} is the whole file; {path, block}
    puts lines between block's begin and end markers, at the start of the
    file (or the end with "position": "end"), keeping the rest as it is, and
    adds `unless.line` to the block when no line outside it matches
    `unless.pattern`.
    """
    block = entry.get("block")
    lines = [records.render(line, values) for line in (block or entry).get("lines", [])]
    if not block:
        return "\n".join(lines) + "\n"
    outside: List[str] = []
    inside = False
    for line in current.splitlines():
        if line.strip() == block["begin"]:
            inside = True
        elif line.strip() == block["end"]:
            inside = False
        elif not inside:
            outside.append(line)
    body = [block["begin"]] + lines
    unless = block.get("unless")
    if unless and not any(re.search(unless["pattern"], line) for line in outside):
        body.append(unless["line"])
    body.append(block["end"])
    text = outside + body if block.get("position") == "end" else body + outside
    return "\n".join(text).rstrip("\n") + "\n"


def configure(engine, pool: str, datasets: Dict[str, str]) -> List[str]:
    """
    Prepare the host for the runtime, as the profile says: write its
    `config_files` (only when they change), run `configure` with the
    variables (pool, facts, <dir>_dataset, <dir>_dataset_name, <dir>_path),
    then start `configure_services`. Returns the files written.
    """
    files = _value(engine, "config_files") or []
    command = _value(engine, "configure")
    if not files and not command:
        return []
    values = variables(engine, pool, datasets)
    done: List[str] = []
    for entry in files:
        path = records.render(entry["path"], values)
        current, _ = engine.execute(CATEGORY, "file_read", check=False, file=path)
        text = file_text(current, entry, values)
        if current.rstrip("\n") != text.rstrip("\n"):
            engine.execute_with_stdin(CATEGORY, "file_write", text, file=path)
            done.append(Path(path).name)
    if command:
        engine.execute(CATEGORY, "configure", timeout=3600, **values)
    for name in _value(engine, "configure_services") or []:
        engine.execute("services", "start", service_name=name)
    return done


def status(engine, config) -> Dict[str, Any]:
    section = config.get_module_config(MODULE_ID)
    result: Dict[str, Any] = {
        "runtime": runtime_name(engine),
        "backend": backend(engine),
        "ui": ui(engine),
        "installed": installed(engine),
        "configured": bool(section.get("enabled")),
        "pool": section.get("pool"),
        "datasets": [],
        "running": False,
        "data_root": None,
        "driver": None,
        "version": None,
        "containers": None,
        "images": None,
        "pools": [],
    }
    try:
        result["pools"] = pool_names(engine)
    except ENGINE_ERRORS as exc:
        logger.warning("Cannot list pools: %s", exc)
    if result["installed"]:
        result["running"] = running(engine)
        details = info(engine) if result["running"] else None
        if details:
            result.update({key: details.get(key) or None
                           for key in ("data_root", "driver", "version", "containers", "images")})
    dirs = data_dirs(engine)
    for name, dataset in (section.get("datasets") or {}).items():
        result["datasets"].append({"name": name, "dataset": dataset, "path": dirs.get(name),
                                   **dataset_state(engine, dataset)})
    return result


def install(engine) -> None:
    engine.execute("packages", "update_cache", timeout=3600, check=False)
    if _value(engine, "runtime_repository"):
        engine.execute(CATEGORY, "runtime_repository", timeout=600)
    for name in packages(engine):
        engine.execute("packages", "install", timeout=7200, package_name=name)
    if not installed(engine):
        raise ContainersError(f"{runtime_name(engine)} is still missing after the installation")


def setup(engine, config, pool: str) -> Dict[str, Any]:
    if not installed(engine):
        raise ContainersError(f"{runtime_name(engine)} is not installed")
    current = config.get_module_config(MODULE_ID)
    if current.get("enabled") and current.get("pool") != pool:
        raise ContainersError(f"Containers already live on {current.get('pool')}")
    if pool not in pool_names(engine):
        raise ContainersError(f"Pool '{pool}' is not imported")
    datasets = datasets_for(engine, pool)
    if not datasets:
        raise ContainersError("The profile names no data directories")

    stop(engine)
    dirs = data_dirs(engine)
    for name, dataset in datasets.items():
        place(engine, dataset, dirs[name])
    configure(engine, pool, datasets)
    keep_from_boot(engine)
    start(engine)

    config.set_module_config(MODULE_ID, {**config.get_module_config(MODULE_ID),
                                         "enabled": True, "pool": pool, "datasets": datasets})
    return status(engine, config)


def _result(action: str, error: Optional[str] = None, **params) -> ActionResult:
    return ActionResult(action=f"containers.{action}", params=params,
                        target_state="failed" if error else "applied", error=error)


def reconcile(engine, desired: Dict[str, Any], mode: str = "declarative", config=None) -> List[ActionResult]:
    """
    Bring the runtime the configuration records back. A system without it
    (a fresh install restored from its pool, another edition) gets it
    installed, as the page's Install does; data directories recorded by the
    other edition's runtime give way to this edition's on the same pool,
    created when missing and recorded in `config` (the others stay on the
    pool, untouched).
    """
    datasets = desired.get("datasets") or {}
    if not desired.get("enabled") or not datasets:
        return []
    results: List[ActionResult] = []
    if not installed(engine):
        try:
            install(engine)
            results.append(_result("install", runtime=runtime_name(engine)))
        except (ContainersError,) + ENGINE_ERRORS as exc:
            return [_result("runtime", f"{runtime_name(engine)} could not be installed: {exc}")]
    pool = desired.get("pool") or next(iter(datasets.values())).split("/")[0]
    try:
        if pool not in pool_names(engine):
            return results + [_result("datasets", f"Pool '{pool}' is not imported", pool=pool)]
    except ENGINE_ERRORS as exc:
        return results + [_result("datasets", str(exc), pool=pool)]
    here = datasets_for(engine, pool)
    if here and set(datasets) != set(here):
        datasets = here
        if config is not None:
            config.set_module_config(MODULE_ID, {**config.get_module_config(MODULE_ID), "datasets": here})
        results.append(_result("datasets", pool=pool, done=f"this edition's: {', '.join(sorted(here.values()))}"))

    dirs = data_dirs(engine)
    try:
        unmounted = [name for name, ds in datasets.items() if not dataset_state(engine, ds)["mounted"]]
        for name, ds in datasets.items():
            if name not in unmounted and mount_at_boot(engine, ds):
                results.append(_result("dataset", dataset=ds, path=dirs.get(name), done="canmount=on"))
        if unmounted:
            stop(engine)
        for name in unmounted:
            if name not in dirs:
                results.append(_result("dataset", f"No data directory '{name}' on this edition",
                                       dataset=datasets[name]))
                continue
            done = place(engine, datasets[name], dirs[name])
            results.append(_result("dataset", dataset=datasets[name], path=dirs[name], done=", ".join(done)))
        done = configure(engine, pool, datasets)
        if done:
            results.append(_result("configure", done=", ".join(done)))
        keep_from_boot(engine)
        if not running(engine) or info(engine) is None:
            start(engine)
            results.append(_result("start"))
    except (ContainersError,) + ENGINE_ERRORS as exc:
        logger.error("Containers not reconciled: %s", exc)
        results.append(_result("runtime", str(exc), pool=pool))
    return results
