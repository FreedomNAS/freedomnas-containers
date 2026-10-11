"""
ContainersModule — the container runtime and the datasets that hold it.

The work is done in runtime.py, shared with api_routes.py; both load it by
file path, as freedomnas-core loads this file, so the module needs no
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


def load_runtime():
    name = "freedomnas_containers_runtime"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("runtime.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


runtime = load_runtime()


class ContainersModule(FreedomNASModule):
    """Docker with its data root on a dataset of a chosen pool."""

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
            if not self._section.get("enabled"):
                if runtime.installed(self._engine):
                    return True, "Docker installed, not set up"
                return True, "not set up"
            details = runtime.info(self._engine) if runtime.running(self._engine) else None
            if not details:
                return False, "Docker is not running"
            datasets = self._section.get("datasets") or {}
            unmounted = [ds for ds in datasets.values()
                         if not runtime.dataset_state(self._engine, ds)["mounted"]]
            if unmounted:
                return False, f"not mounted: {', '.join(unmounted)}"
            return True, (f"Docker {details.get('ServerVersion')} on {self._section.get('pool')}: "
                          f"{details.get('Containers', 0)} containers, {details.get('Images', 0)} images")
        except Exception as exc:
            logger.warning("Containers health check failed: %s", exc)
            return False, str(exc)

    def reconcile(self, desired: dict, mode: str = "declarative") -> List:
        return runtime.reconcile(self._engine, desired, mode)
