import json

from conftest import REPO, FakeConfig
from freedomnas.module_loader import ModuleLoader

MODULES = REPO / "modules"
PROFILES = REPO / "profiles" / "containers"
MANIFEST = json.loads((MODULES / "containers" / "manifest.json").read_text())


def load(engine, section=None):
    [(manifest, module)] = ModuleLoader(engine, FakeConfig(section), modules_dir=str(MODULES)).instances()
    module.start()
    return manifest, module


def test_the_core_loader_finds_the_module(engine):
    manifest, module = load(engine)
    assert manifest["id"] == module.name == "containers"


def test_not_set_up_is_healthy(engine):
    assert load(engine)[1].health() == (True, "Docker installed, not set up")


def test_set_up_and_running_is_healthy(engine):
    config = FakeConfig()
    module = load(engine)[1]
    module._config = config
    section = {"enabled": True, "pool": "tank", "datasets": {
        "docker": "tank/containers/docker", "containerd": "tank/containers/containerd"}}
    module.reconcile(section)
    module._section = section
    healthy, detail = module.health()
    assert healthy and "on tank" in detail


def test_an_unmounted_dataset_is_unhealthy(engine):
    section = {"enabled": True, "pool": "tank", "datasets": {"docker": "tank/containers/docker"}}
    module = load(engine)[1]
    module.reconcile(section)
    engine.datasets["tank/containers/docker"]["mounted"] = "no"
    module._section = section
    assert module.health() == (False, "not mounted: tank/containers/docker")


def test_stopped_docker_is_unhealthy(engine):
    module = load(engine, {"enabled": True, "datasets": {"docker": "tank/containers/docker"}})[1]
    assert module.health() == (False, "Docker is not running")


def test_every_supported_edition_has_a_profile():
    files = sorted(p.name for p in PROFILES.glob("containers-*.json"))
    assert files == sorted(f"containers-{os}.json" for os in MANIFEST["supported_os"])


def test_profiles_carry_the_same_commands():
    required = {"runtime_check", "info", "data_dirs", "data_move", "data_copy"}
    for path in PROFILES.glob("containers-*.json"):
        assert required <= set(json.loads(path.read_text())["containers"]), path.name


def test_the_sidebar_entry_points_at_the_page():
    webui = MANIFEST["webui"]
    assert webui["nav"]["icon"] == "icons.svg#containers"
    assert (MODULES / "containers" / webui["static_dir"] / "icons.svg").exists()
    assert (MODULES / "containers" / webui["templates_dir"] / webui["page_template"]).exists()
