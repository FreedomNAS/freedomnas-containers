"""
The container runtime: Docker installed from the edition's packages, with
the directories it keeps its data in (`data_dirs` in the profile:
/var/lib/docker and /var/lib/containerd on Linux) mounted from ZFS datasets
of a chosen pool, `<pool>/containers/<name>`. They sit outside the root
dataset, so containers and images survive a rollback of the system and go
along with the pool's snapshots and replication; Docker's own
configuration is left as it is, whichever image store it uses.

The datasets are `canmount=on`, so a pool imported at boot gets them
mounted by the system's own ZFS mount service before any service starts,
also when something that needs Docker (a CI runner, say) starts it early.
Docker is still kept from starting at boot by itself: a pool imported
only by the reconciler has its datasets mounted by the module's
reconcile(), which then starts Docker.

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
import json
import logging
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


appjail_runtime = _load("appjail_runtime")

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
    return _value(engine, "backend", "docker")


def runtime_name(engine) -> str:
    return "AppJail" if backend(engine) == "appjail" else "Docker"


def data_dirs(engine) -> Dict[str, str]:
    return dict(_value(engine, "data_dirs", {}))


def datasets_for(engine, pool: str) -> Dict[str, str]:
    return {name: f"{pool}/containers/{name}" for name in data_dirs(engine)}


def services(engine) -> List[str]:
    names = PackageManager().get_services_for_os(PACKAGES, engine.os_type, engine.init_system)
    return sorted(names, key=lambda name: 0 if "containerd" in name else 1)


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
    if backend(engine) == "appjail":
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
    if backend(engine) == "appjail":
        try:
            out, _ = engine.execute(CATEGORY, "info")
        except ENGINE_ERRORS:
            return None
        return {"DockerRootDir": data_dirs(engine).get("appjail"), "Driver": "zfs",
                "ServerVersion": out.strip()}
    try:
        out, _ = engine.execute(CATEGORY, "info")
        data = json.loads(out)
    except (ENGINE_ERRORS + (ValueError,)):
        return None
    if not isinstance(data, dict) or data.get("ServerErrors") or not data.get("DockerRootDir"):
        return None
    return data


def _boot_units(engine) -> List[str]:
    return list(_value(engine, "socket_units", [])) + services(engine)


def keep_from_boot(engine) -> None:
    """
    Docker waits for the reconciler, which mounts its datasets first.
    AppJail's services stay enabled: the jails' NAT is set up at boot, and
    the pool is imported and mounted before any service starts.
    """
    action = "enable" if backend(engine) == "appjail" else "disable"
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
    Returns what was done. Docker must be stopped.
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


def status(engine, config) -> Dict[str, Any]:
    section = config.get_module_config(MODULE_ID)
    result: Dict[str, Any] = {
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
            result.update(
                data_root=details.get("DockerRootDir"),
                driver=details.get("Driver"),
                version=details.get("ServerVersion"),
                containers=details.get("Containers"),
                images=details.get("Images"),
            )
    dirs = data_dirs(engine)
    for name, dataset in (section.get("datasets") or {}).items():
        result["datasets"].append({"name": name, "dataset": dataset, "path": dirs.get(name),
                                   **dataset_state(engine, dataset)})
    return result


def install(engine) -> None:
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
    if backend(engine) == "appjail":
        appjail_runtime.configure(engine, pool, datasets, dirs)
    keep_from_boot(engine)
    start(engine)

    config.set_module_config(MODULE_ID, {**config.get_module_config(MODULE_ID),
                                         "enabled": True, "pool": pool, "datasets": datasets})
    return status(engine, config)


def _result(action: str, error: Optional[str] = None, **params) -> ActionResult:
    return ActionResult(action=f"containers.{action}", params=params,
                        target_state="failed" if error else "applied", error=error)


def reconcile(engine, desired: Dict[str, Any], mode: str = "declarative") -> List[ActionResult]:
    datasets = desired.get("datasets") or {}
    if not desired.get("enabled") or not datasets:
        return []
    if not installed(engine):
        return [_result("runtime", f"{runtime_name(engine)} is not installed: install it from the Containers page")]
    pool = desired.get("pool") or next(iter(datasets.values())).split("/")[0]
    try:
        if pool not in pool_names(engine):
            return [_result("datasets", f"Pool '{pool}' is not imported", pool=pool)]
    except ENGINE_ERRORS as exc:
        return [_result("datasets", str(exc), pool=pool)]

    results: List[ActionResult] = []
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
        if backend(engine) == "appjail":
            done = appjail_runtime.configure(engine, pool, datasets, dirs)
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
