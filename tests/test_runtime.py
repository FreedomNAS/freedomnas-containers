from pathlib import Path

import pytest

import runtime
from conftest import FakeConfig

DATASETS = {"docker": "tank/containers/docker", "containerd": "tank/containers/containerd"}
DESIRED = {"enabled": True, "pool": "tank", "datasets": DATASETS}


def test_services_start_with_containerd(engine):
    assert runtime.services(engine) == ["containerd", "docker"]


def test_setup_mounts_a_dataset_on_every_data_directory(engine):
    config = FakeConfig()
    result = runtime.setup(engine, config, "tank")
    assert config.section == {"enabled": True, "pool": "tank", "datasets": DATASETS}
    for name, ds in DATASETS.items():
        assert engine.datasets[ds] == {"mountpoint": engine.path(name), "mounted": "yes", "canmount": "on"}
    assert result["running"] and [d["mounted"] for d in result["datasets"]] == [True, True]


def test_setup_keeps_docker_from_starting_at_boot(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    disabled = [kw["service_name"] for c, a, kw in engine.calls if (c, a) == ("services", "disable")]
    assert disabled == ["docker.socket", "containerd", "docker"]


def test_docker_is_stopped_before_and_started_after_the_mounts(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    acts = engine.actions()
    assert acts.index("services.stop") < acts.index("zfs.mount_dataset")
    assert max(i for i, a in enumerate(acts) if a == "zfs.mount_dataset") < acts.index("services.start")


def test_existing_data_is_moved_aside_and_copied_onto_the_dataset(engine):
    old = Path(engine.path("docker"))
    (old / "volumes" / "web").mkdir(parents=True)
    (old / "volumes" / "web" / "index.html").write_text("hello")
    runtime.setup(engine, FakeConfig(), "tank")
    assert (old / "volumes" / "web" / "index.html").read_text() == "hello"
    assert (Path(str(old) + runtime.ASIDE) / "volumes" / "web" / "index.html").exists()
    assert "containers.data_copy" in engine.actions()


def test_empty_directories_are_not_copied(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    assert "containers.data_move" not in engine.actions()
    assert "containers.data_copy" not in engine.actions()


def test_a_previous_copy_set_aside_is_never_overwritten(engine):
    old = Path(engine.path("docker"))
    (old / "x").mkdir(parents=True)
    previous = Path(str(old) + runtime.ASIDE)
    previous.mkdir()
    (previous / "kept").write_text("first copy")
    runtime.setup(engine, FakeConfig(), "tank")
    assert (previous / "kept").read_text() == "first copy"
    [dated] = [p for p in old.parent.iterdir() if p.name.startswith(previous.name + "-")]
    assert (dated / "x").is_dir()


def test_setup_refuses_a_pool_that_is_not_imported(engine):
    with pytest.raises(runtime.ContainersError):
        runtime.setup(engine, FakeConfig(), "backup")
    assert "services.stop" not in engine.actions()


def test_setup_refuses_without_docker(engine):
    engine.installed = False
    with pytest.raises(runtime.ContainersError):
        runtime.setup(engine, FakeConfig(), "tank")


def test_setup_refuses_to_move_to_another_pool(engine):
    with pytest.raises(runtime.ContainersError):
        runtime.setup(engine, FakeConfig(DESIRED), "rpool")


def test_setup_again_on_the_same_pool_changes_nothing(engine):
    config = FakeConfig()
    runtime.setup(engine, config, "tank")
    engine.calls.clear()
    runtime.setup(engine, config, "tank")
    assert "zfs.dataset_create_with" not in engine.actions()
    assert "zfs.mount_dataset" not in engine.actions()


def test_a_dataset_mounted_elsewhere_is_refused(engine):
    engine.datasets["tank/containers/docker"] = {"mountpoint": "/srv/docker", "mounted": "yes"}
    with pytest.raises(runtime.ContainersError):
        runtime.setup(engine, FakeConfig(), "tank")


def test_a_failed_start_records_nothing(engine, monkeypatch):
    config = FakeConfig()
    engine.failing.add(("services", "start"))
    with pytest.raises(Exception):
        runtime.setup(engine, config, "tank")
    assert config.section == {}


def test_install_adds_the_repository_then_the_packages(engine):
    runtime.install(engine)
    installs = [kw["package_name"] for c, a, kw in engine.calls if (c, a) == ("packages", "install")]
    assert engine.actions()[0] == "containers.runtime_repository"
    assert "docker-ce" in installs and "docker-compose-plugin" in installs


def test_install_fails_when_docker_is_still_missing(engine):
    engine.installed = False
    with pytest.raises(runtime.ContainersError):
        runtime.install(engine)


def test_reconcile_does_nothing_when_not_set_up(engine):
    assert runtime.reconcile(engine, {}) == []
    assert engine.calls == []


def test_reconcile_mounts_the_datasets_then_starts_docker(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    engine.running.clear()
    for ds in DATASETS.values():
        engine.datasets[ds]["mounted"] = "no"
    engine.calls.clear()
    results = runtime.reconcile(engine, DESIRED)
    assert [(r.action, r.target_state) for r in results] == [
        ("containers.dataset", "applied"), ("containers.dataset", "applied"), ("containers.start", "applied")]
    acts = engine.actions()
    assert max(i for i, a in enumerate(acts) if a == "zfs.mount_dataset") < acts.index("services.start")


def test_reconcile_on_a_new_pool_creates_empty_datasets(engine):
    results = runtime.reconcile(engine, DESIRED)
    assert [r.action for r in results] == ["containers.dataset", "containers.dataset", "containers.start"]
    assert results[0].params["done"] == "created, mounted"


def test_reconcile_moves_aside_what_docker_wrote_on_the_root(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    engine.running.clear()
    engine.datasets["tank/containers/docker"]["mounted"] = "no"
    (Path(engine.path("docker")) / "stray").write_text("written before the mount")
    results = runtime.reconcile(engine, DESIRED)
    assert "moved aside" in results[0].params["done"]


def test_reconcile_of_a_running_system_changes_nothing(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    engine.calls.clear()
    assert runtime.reconcile(engine, DESIRED) == []
    assert "services.start" not in engine.actions()
    assert "services.stop" not in engine.actions()


def test_reconcile_without_docker_is_one_failure(engine):
    engine.installed = False
    [result] = runtime.reconcile(engine, DESIRED)
    assert (result.action, result.target_state) == ("containers.runtime", "failed")


def test_reconcile_waits_for_a_missing_pool(engine):
    engine.pools = ["rpool"]
    [result] = runtime.reconcile(engine, DESIRED)
    assert result.target_state == "failed" and "tank" in result.error
    assert "zfs.dataset_create_with" not in engine.actions()


def test_status_reports_the_runtime_and_its_datasets(engine):
    config = FakeConfig()
    runtime.setup(engine, config, "tank")
    s = runtime.status(engine, config)
    assert (s["installed"], s["configured"], s["running"], s["containers"]) == (True, True, True, 2)
    assert s["pools"] == ["rpool", "tank"]
    assert [(d["name"], d["dataset"], d["mounted"]) for d in s["datasets"]] == [
        ("docker", "tank/containers/docker", True), ("containerd", "tank/containers/containerd", True)]


def test_reconcile_lets_the_system_mount_old_noauto_datasets_at_boot(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    for ds in DATASETS.values():
        engine.datasets[ds]["canmount"] = "noauto"
    engine.calls.clear()
    results = runtime.reconcile(engine, DESIRED)
    assert [(r.action, r.params["done"]) for r in results] == [("containers.dataset", "canmount=on")] * 2
    assert all(engine.datasets[ds]["canmount"] == "on" for ds in DATASETS.values())
    assert "services.stop" not in engine.actions()


def test_reconcile_after_docker_was_started_early_twice(engine):
    runtime.setup(engine, FakeConfig(), "tank")
    for _ in range(2):
        engine.datasets["tank/containers/docker"]["mounted"] = "no"
        (Path(engine.path("docker")) / "stray").write_text("written before the mount")
        [result, *_] = runtime.reconcile(engine, DESIRED)
        assert result.target_state == "applied" and "moved aside" in result.params["done"]
        engine.running.add("docker")
