"""
Apps on FreeBSD: AppJail Director projects through the same apps.py, with
the Director file, its Makejail and the project's jails all named by the
profile.
"""

import json
from pathlib import Path

import pytest

import apps
import containers
import runtime
from conftest import FakeConfig, FakeEngine

DIRECTOR = "services:\n  web:\n    name: site-web\n    makejail: ./Makejail\n"
MAKEJAIL = "OPTION container=boot\nOPTION from=ghcr.io/appjail-makejails/nginx:latest\n"
DESCRIBE = json.dumps({"name": "site", "services": [{"name": "web", "status": 0, "jail": "site-web"}]})
JAILS = ("site-web\tUP\tthick\t15.1-RELEASE\t8093 -> 80/tcp\t10.231.0.4\tnginx\t2026-10-15\t\n"
         "other\tUP\tthick\t15.1-RELEASE\t \t10.231.0.5\tnginx\t2026-10-15\t\n")


@pytest.fixture
def bsd(tmp_path):
    engine = FakeEngine(os_type="freebsd", init_system="bsd_rc", tmp=tmp_path)
    engine.running.add("docker")
    return engine


@pytest.fixture
def config():
    return FakeConfig({"enabled": True, "pool": "tank", "datasets": {}})


def folder(engine):
    return Path(engine.datasets["tank/apps/site"]["mountpoint"])


def test_an_app_is_a_director_file_with_its_makejail(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    assert (folder(bsd) / "appjail-director.yml").read_text() == DIRECTOR
    assert (folder(bsd) / "Makejail").read_text() == MAKEJAIL
    [check] = [kw for c, a, kw in bsd.calls if a == "compose_check"]
    assert check == {"dir": str(folder(bsd))}
    assert apps.read(runtime, bsd, config, "site")["files"] == {"makejail": MAKEJAIL}


def test_files_the_profile_does_not_name_are_refused(bsd, config, engine):
    with pytest.raises(apps.AppError, match="Unknown app file"):
        apps.create(runtime, bsd, config, "site", DIRECTOR, files={"dockerfile": "FROM x"})
    with pytest.raises(apps.AppError, match="Unknown app file"):
        apps.create(runtime, engine, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})


def test_an_invalid_director_file_writes_nothing(bsd, config):
    bsd.failing.add(("containers", "compose_check"))
    with pytest.raises(apps.AppError, match="not valid"):
        apps.create(runtime, bsd, config, "site", "services: [", files={"makejail": MAKEJAIL})
    assert "tank/apps/site" not in bsd.datasets


def test_editing_replaces_both_files(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    apps.update(runtime, bsd, config, "site", DIRECTOR + "    priority: 1\n", files={"makejail": "OPTION nat\n"})
    assert (folder(bsd) / "Makejail").read_text() == "OPTION nat\n"
    assert (folder(bsd) / "appjail-director.yml").read_text().endswith("priority: 1\n")


def test_the_projects_jails_are_its_members(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    apps.set_state(runtime, bsd, config, "site", "up")
    bsd.outputs[("containers", "ps")] = (JAILS, "")
    bsd.outputs[("containers", "app_members")] = (DESCRIBE, "")
    [app] = apps.listing(runtime, containers, bsd, config)
    assert (app["containers"], app["running"], app["ports"]) == (1, 1, "8093→80")


def test_update_recreates_and_remove_destroys_the_project(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    apps.refresh(runtime, bsd, config, "site")
    assert "compose_update" not in bsd.actions() and "compose_pull" not in bsd.actions()
    apps.set_state(runtime, bsd, config, "site", "up")
    apps.refresh(runtime, bsd, config, "site")
    assert bsd.actions()[-1] == "containers.compose_update"
    apps.remove(runtime, bsd, config, "site")
    assert bsd.actions()[-1] == "containers.compose_remove"


def test_logs_are_each_jails_own(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    bsd.outputs[("containers", "app_members")] = (DESCRIBE, "")
    bsd.outputs[("containers", "logs")] = ("started", "")
    assert apps.logs(runtime, bsd, config, "site", 50, containers) == "== site-web\nstarted"
    assert ("containers", "logs", {"container": "site-web", "lines": "50"}) in bsd.calls


def test_reconcile_brings_up_a_project_whose_jails_are_down(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    bsd.outputs[("containers", "ps")] = (JAILS.replace("site-web\tUP", "site-web\tDOWN"), "")
    bsd.outputs[("containers", "app_members")] = (DESCRIBE, "")
    bsd.outputs[("containers", "info")] = ("5.9.1", "")
    desired = {"enabled": True, "apps": {"site": {"dataset": "tank/apps/site", "state": "up"}}}
    [result] = apps.reconcile(runtime, containers, bsd, desired)
    assert result.target_state == "applied" and bsd.actions()[-1] == "containers.compose_up"
