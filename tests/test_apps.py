import json
from pathlib import Path

import pytest

import apps
import docker
import runtime
from conftest import FakeConfig

COMPOSE = "services:\n  web:\n    image: nginx:alpine\n"


@pytest.fixture
def config():
    return FakeConfig({"enabled": True, "pool": "tank", "datasets": {}})


def compose_calls(engine, action):
    return [kw for c, a, kw in engine.calls if (c, a) == ("containers", action)]


def test_an_app_gets_a_dataset_holding_its_compose_file(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    assert config.section["apps"] == {"web": {"dataset": "tank/apps/web", "state": "down"}}
    file = Path(engine.datasets["tank/apps/web"]["mountpoint"]) / "compose.yaml"
    assert file.read_text() == COMPOSE
    [check] = [kw for c, a, kw in engine.calls if a == "compose_check"]
    assert check["dir"] == str(file.parent)
    assert engine.actions().index("containers.compose_check") < engine.actions().index("containers.compose_write")


def test_creating_keeps_the_runtime_settings(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    assert config.section["pool"] == "tank" and config.section["enabled"]


def test_an_invalid_compose_file_creates_nothing(engine, config):
    engine.failing.add(("containers", "compose_check"))
    with pytest.raises(apps.AppError, match="refused"):
        apps.create(runtime, engine, config, "web", "services: [")
    assert "tank/apps/web" not in engine.datasets and "apps" not in config.section


def test_a_failed_write_destroys_the_new_dataset(engine, config):
    engine.failing.add(("containers", "compose_write"))
    with pytest.raises(Exception):
        apps.create(runtime, engine, config, "web", COMPOSE)
    assert "tank/apps/web" not in engine.datasets


@pytest.mark.parametrize("name", ["Web", "-x", "a b", "../x", "", "x" * 64])
def test_bad_names_are_refused(engine, config, name):
    with pytest.raises(apps.AppError):
        apps.create(runtime, engine, config, name, COMPOSE)


def test_apps_need_the_runtime(engine):
    with pytest.raises(apps.AppError, match="runtime"):
        apps.create(runtime, engine, FakeConfig(), "web", COMPOSE)


def test_two_apps_cannot_share_a_name(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    with pytest.raises(apps.AppError):
        apps.create(runtime, engine, config, "web", COMPOSE)


def test_a_kept_dataset_is_used_again_with_its_data(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    mountpoint = Path(engine.datasets["tank/apps/web"]["mountpoint"])
    (mountpoint / "data.txt").write_text("kept")
    apps.remove(runtime, engine, config, "web")
    apps.create(runtime, engine, config, "web", COMPOSE + "    restart: always\n")
    assert (mountpoint / "data.txt").read_text() == "kept"
    assert (mountpoint / "compose.yaml").read_text().endswith("restart: always\n")


def test_a_failed_write_keeps_a_reused_dataset(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.remove(runtime, engine, config, "web")
    engine.failing.add(("containers", "compose_write"))
    with pytest.raises(Exception):
        apps.create(runtime, engine, config, "web", COMPOSE)
    assert "tank/apps/web" in engine.datasets


def test_up_and_down_use_the_project_and_record_the_state(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.set_state(runtime, engine, config, "web", "up")
    [up] = compose_calls(engine, "compose_up")
    assert up["project"] == "web" and up["file"].endswith("tank/apps/web/compose.yaml")
    assert config.section["apps"]["web"]["state"] == "up"
    apps.set_state(runtime, engine, config, "web", "down")
    assert config.section["apps"]["web"]["state"] == "down"


def test_a_failed_up_leaves_the_state_alone(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    engine.failing.add(("containers", "compose_up"))
    with pytest.raises(Exception):
        apps.set_state(runtime, engine, config, "web", "up")
    assert config.section["apps"]["web"]["state"] == "down"


def test_update_pulls_and_brings_up_only_a_running_app(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.refresh(runtime, engine, config, "web")
    assert len(compose_calls(engine, "compose_pull")) == 1 and not compose_calls(engine, "compose_up")
    apps.set_state(runtime, engine, config, "web", "up")
    apps.refresh(runtime, engine, config, "web")
    assert len(compose_calls(engine, "compose_up")) == 2


def test_editing_checks_and_replaces_the_file(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.update(runtime, engine, config, "web", COMPOSE + "    restart: always\n")
    assert apps.read(runtime, engine, config, "web")["compose"].endswith("restart: always\n")


def test_removing_keeps_the_dataset_unless_asked(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.remove(runtime, engine, config, "web")
    assert "tank/apps/web" in engine.datasets and config.section["apps"] == {}
    assert compose_calls(engine, "compose_down")


def test_removing_with_the_data_destroys_the_dataset(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.remove(runtime, engine, config, "web", destroy_data=True)
    assert "tank/apps/web" not in engine.datasets


def ps(*rows):
    return ("\n".join(json.dumps({"ID": n, "Names": n, "Image": "x", "State": st, "Status": st, "Ports": p,
                                  "Labels": f"com.docker.compose.project={proj}"}) for n, st, p, proj in rows), "")


def test_the_listing_counts_each_app_s_containers(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    apps.set_state(runtime, engine, config, "web", "up")
    engine.outputs[("containers", "ps")] = ps(("web-web-1", "running", "0.0.0.0:8080->80/tcp", "web"),
                                              ("web-db-1", "exited", "", "web"), ("other", "running", "", "x"))
    [app] = apps.listing(runtime, docker, engine, config)
    assert (app["name"], app["state"], app["containers"], app["running"]) == ("web", "up", 2, 1)
    assert app["ports"] == "8080→80"


def desired(state="up"):
    return {"enabled": True, "pool": "tank", "apps": {"web": {"dataset": "tank/apps/web", "state": state}}}


def test_reconcile_brings_up_an_app_that_is_not_running(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    engine.running.add("docker")
    results = apps.reconcile(runtime, docker, engine, desired())
    assert [(r.action, r.target_state) for r in results] == [("containers.apps.web", "applied")]


def test_reconcile_leaves_a_running_app_alone(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    engine.running.add("docker")
    engine.outputs[("containers", "ps")] = ps(("web-web-1", "running", "", "web"))
    assert apps.reconcile(runtime, docker, engine, desired()) == []


def test_reconcile_ignores_stopped_apps(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    engine.running.add("docker")
    assert apps.reconcile(runtime, docker, engine, desired("down")) == []


def test_reconcile_reports_a_missing_compose_file(engine, config):
    apps.create(runtime, engine, config, "web", COMPOSE)
    engine.running.add("docker")
    (Path(engine.datasets["tank/apps/web"]["mountpoint"]) / "compose.yaml").unlink()
    [result] = apps.reconcile(runtime, docker, engine, desired())
    assert result.target_state == "failed" and "missing" in result.error


def test_reconcile_without_docker_fails_each_app(engine, config):
    [result] = apps.reconcile(runtime, docker, engine, desired())
    assert result.target_state == "failed"


def test_the_module_brings_apps_up_after_the_runtime(engine, config):
    from test_module import load
    module = load(engine)[1]
    module._config = config
    section = {"enabled": True, "pool": "tank",
               "datasets": {"docker": "tank/containers/docker", "containerd": "tank/containers/containerd"}}
    module.reconcile(section)
    apps.create(runtime, engine, config, "web", COMPOSE)
    engine.running.clear()
    results = module.reconcile({**section, "apps": {"web": {"dataset": "tank/apps/web", "state": "up"}}})
    assert [r.action for r in results] == ["containers.start", "containers.apps.web"]


def test_published_ports_are_shown_once_and_short():
    text = "0.0.0.0:8090->80/tcp, [::]:8090->80/tcp, 10.0.2.15:53->53/udp, 0.0.0.0:8091->80/tcp"
    assert docker.short_ports(text) == "8090→80, 53→53/udp, 8091→80"
