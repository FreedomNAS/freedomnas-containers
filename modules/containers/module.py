"""
ContainersModule — the container runtime and the datasets that hold it.

The work is done in runtime.py, containers.py and apps.py, shared with
api_routes.py; both load them by file path, as freedomnas-core loads this file, so the module needs no
importable package.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Tuple

try:
    from freedomnas.engine import FreedomNASEngine
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule
except ImportError:
    from engine import FreedomNASEngine  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Containers")


def load(stem: str):
    name = f"freedomnas_containers_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{stem}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


runtime = load("runtime")
containers = load("containers")
apps = load("apps")
catalogue = load("catalogue")


class ContainersModule(FreedomNASModule):
    """The container runtime with its data on datasets of a chosen pool."""

    def __init__(self, engine: FreedomNASEngine, config: ConfigurationManager):
        super().__init__(engine, config)
        self._section: dict = {}

    @property
    def name(self) -> str:
        return runtime.MODULE_ID

    def start(self) -> None:
        self._section = self._config.get_module_config(runtime.MODULE_ID)

    def stop(self) -> None:
        pass

    def health_check(self) -> bool:
        return self.health()[0]

    def health(self) -> Tuple[bool, str]:
        try:
            name = runtime.runtime_name(self._engine)
            if not self._section.get("enabled"):
                if runtime.installed(self._engine):
                    return True, f"{name} installed, not set up"
                return True, "not set up"
            details = runtime.info(self._engine) if runtime.running(self._engine) else None
            if not details:
                return False, f"{name} is not running"
            datasets = self._section.get("datasets") or {}
            unmounted = [ds for ds in datasets.values()
                         if not runtime.dataset_state(self._engine, ds)["mounted"]]
            if unmounted:
                return False, f"not mounted: {', '.join(unmounted)}"
            counts = ", ".join(f"{details[key]} {key}" for key in ("containers", "images")
                               if details.get(key) not in (None, ""))
            return True, f"{name} {details.get('version')} on {self._section.get('pool')}" + (f": {counts}" if counts else "")
        except Exception as exc:
            logger.warning("Containers health check failed: %s", exc)
            return False, str(exc)

    def reconcile(self, desired: dict, mode: str = "declarative") -> List:
        results = runtime.reconcile(self._engine, desired, mode)
        if any(r.target_state == "failed" for r in results):
            return results
        adopt = lambda name, app, file: catalogue.adopt(self._engine, name, app, file)
        results += catalogue.refresh_tls(runtime, apps, self._engine, desired)
        results += catalogue.refresh_local_dns(runtime, apps, self._engine, self._config, desired)
        results += apps.reconcile(runtime, containers, self._engine, desired, adopt)
        return results + catalogue.proxy_sync(runtime, apps, containers, self._engine, self._config)
