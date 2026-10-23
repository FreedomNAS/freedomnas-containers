"""
Every command of every containers profile formats with the real engine:
a brace that str.format would read as a variable, or a variable the module
never passes, shows up here and not on a NAS.
"""

import json
import shutil

import pytest

from conftest import PROFILES, REPO
from freedomnas.engine import FreedomNASEngine

VALUES = {"container": "web", "lines": "200", "image": "nginx:alpine", "project": "site", "file": "/tank/apps/site/x",
          "dir": "/tank/apps/site", "source": "/a", "target": "/b", "uid": "1000", "gid": "1000", "path": "/p", "cert": "/c.pem", "key": "/k.pem", "hostname": "nas",
          "pool": "tank", "interface": "em0", "version": "15.1-RELEASE", "arch": "amd64"}
EDITIONS = ["arch", "artix", "debian", "freebsd", "gentoo", "void"]


def engine_for(profile_dir, edition):
    FreedomNASEngine._instance = None
    FreedomNASEngine._initialized = False
    try:
        return FreedomNASEngine(profile_dir=str(profile_dir), os_override=edition)
    finally:
        FreedomNASEngine._instance = None
        FreedomNASEngine._initialized = False


@pytest.fixture(scope="module")
def profile_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("profiles") / "profiles"
    shutil.copytree(PROFILES, target)
    shutil.copytree(REPO / "profiles" / "containers", target / "containers")
    return target


@pytest.mark.parametrize("edition", EDITIONS)
def test_every_command_formats(profile_dir, edition):
    engine = engine_for(profile_dir, edition)
    section = json.loads((REPO / f"profiles/containers/containers-{edition}.json").read_text())["containers"]
    commands = [k for k, v in section.items()
                if not k.startswith("_") and (isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v)))
                and k not in ("services_order", "socket_units", "configure_services", "app_file", "backend",
                              "runtime_name", "boot_services", "ports_pattern", "name_pattern", "name_hint")]
    assert "ps" in commands and "run" in commands
    for action in commands:
        engine.get_command("containers", action, **VALUES)


def test_docker_run_reaches_the_runtime_as_words(profile_dir):
    engine = engine_for(profile_dir, "debian")
    assert engine.get_command("containers", "run") == "xargs -0 docker run -d"
    assert engine.get_command("containers", "ps").startswith("docker ps -a --no-trunc --format '{\"id\":{{json .ID}}")
