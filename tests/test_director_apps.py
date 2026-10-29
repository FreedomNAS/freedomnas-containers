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


def linux_app(bsd, name, catalogue_id):
    """A catalogue app as a Linux edition left it on the pool: compose.yaml, .env, data."""
    import catalogue
    base = bsd.tmp / "mnt" / f"tank/apps/{name}"
    (base / "data").mkdir(parents=True)
    (base / "data" / "db.sqlite3").write_text("vault")
    (base / "compose.yaml").write_text((catalogue.entry(catalogue_id)["_dir"] / "linux/compose.yaml").read_text())
    (base / ".env").write_text("PORT='8222'\nADMIN_TOKEN='secret-token'\nSIGNUPS='false'\nTZ='Europe/Rome'\n")
    bsd.datasets[f"tank/apps/{name}"] = {"mountpoint": str(base), "mounted": "yes", "canmount": "on"}
    return base, {"dataset": f"tank/apps/{name}", "state": "up", "catalogue": catalogue_id, "web_port": "8222"}


def test_a_portable_app_from_linux_gets_this_editions_recipe(bsd):
    import catalogue
    base, app = linux_app(bsd, "vw", "vaultwarden")
    bsd.outputs[("containers", "info")] = ("5.9.1", "")
    desired = {"enabled": True, "apps": {"vw": app}}
    adopt = lambda name, record, file: catalogue.adopt(runtime, apps, containers, bsd, FakeConfig(), name, record, file)
    [result] = apps.reconcile(runtime, containers, bsd, desired, adopt)
    assert result.target_state == "applied" and bsd.actions()[-1] == "containers.compose_up"
    assert {"appjail-director.yml", "Makejail", "vaultwarden.rc"} <= {p.name for p in base.iterdir()}
    env = catalogue.parse_env((base / ".env").read_text())
    assert env["APP"] == "vw" and env["ADMIN_TOKEN"] == "secret-token"
    assert ("containers", "folder_own", {"uid": "80", "gid": "80", "path": str(base / "data")}) in bsd.calls
    assert (base / "data" / "db.sqlite3").read_text() == "vault"


def test_an_app_that_cannot_be_installed_again_stays_down_with_a_reason(bsd, monkeypatch):
    import catalogue

    def refuse(*args):
        raise catalogue.CatalogueError("port 8096 is already in use")

    monkeypatch.setattr(catalogue, "reinstall", refuse)
    base, app = linux_app(bsd, "films", "jellyfin")
    problem = catalogue.adopt(runtime, apps, containers, bsd, FakeConfig(), "films", app,
                              str(base / "appjail-director.yml"))
    assert "could not be installed again" in problem and "8096" in problem
    assert not (base / "appjail-director.yml").exists()


def test_an_app_whose_data_does_not_move_is_installed_again_on_it(bsd, monkeypatch):
    import catalogue
    done = []
    monkeypatch.setattr(catalogue, "reinstall", lambda *args: done.append(args[5]))
    base, app = linux_app(bsd, "films", "jellyfin")
    assert catalogue.adopt(runtime, apps, containers, bsd, FakeConfig(), "films", app,
                           str(base / "appjail-director.yml")) is None
    assert done == ["films"]


def test_an_app_moved_from_another_edition_is_adopted_even_with_an_old_file_beside_it(bsd):
    import catalogue
    base, app = linux_app(bsd, "films", "jellyfin")
    (base / "appjail-director.yml").write_text("services: {}\n")
    bsd.outputs[("containers", "info")] = ("5.9.1", "")
    seen = []
    adopt = lambda name, record, file: seen.append(name) or "stays down"
    apps.reconcile(runtime, containers, bsd, {"enabled": True, "apps": {"films": app}}, adopt,
                   lambda record: catalogue.moved(bsd, record))
    assert seen == ["films"]
    assert catalogue.moved(bsd, {**app, "recipe": "freebsd"}) is False


def test_a_record_of_this_edition_over_another_editions_files_is_adopted(bsd):
    import catalogue
    base, app = linux_app(bsd, "vw", "vaultwarden")
    app["recipe"] = "freebsd"
    assert catalogue.adopt(runtime, apps, containers, bsd, FakeConfig(), "vw", app,
                           str(base / "appjail-director.yml")) is None
    assert (base / "appjail-director.yml").is_file()


def test_a_missing_file_with_no_recipe_beside_the_data_is_only_reported(bsd):
    import catalogue
    base, app = linux_app(bsd, "vw", "vaultwarden")
    (base / "compose.yaml").unlink()
    app["recipe"] = "freebsd"
    assert catalogue.adopt(runtime, apps, containers, bsd, FakeConfig(), "vw", app,
                           str(base / "appjail-director.yml")).endswith("is missing")


def test_a_stopped_app_whose_jails_started_at_boot_is_taken_down(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    bsd.outputs[("containers", "ps")] = (JAILS, "")
    bsd.outputs[("containers", "app_members")] = (DESCRIBE, "")
    bsd.outputs[("containers", "info")] = ("5.9.1", "")
    desired = {"enabled": True, "apps": {"site": {"dataset": "tank/apps/site", "state": "down"}}}
    [result] = apps.reconcile(runtime, containers, bsd, desired)
    assert result.action == "containers.apps.site.down" and bsd.actions()[-1] == "containers.compose_down"
    bsd.outputs[("containers", "ps")] = (JAILS.replace("site-web\tUP", "site-web\tDOWN"), "")
    assert apps.reconcile(runtime, containers, bsd, desired) == []


def test_nextcloud_goes_behind_the_proxy_on_freebsd_too(bsd):
    import catalogue
    item = catalogue.entry("nextcloud")
    assert catalogue.proxyable(bsd, item)
    folder = catalogue.recipe(bsd, item, "appjail-director.yml")
    plain = catalogue._recipe_files(folder)["appjail-director.yml"]
    behind = catalogue._recipe_files(folder, behind=True)["appjail-director.yml"]
    assert "expose: !ENV '${PORT}:80'" in plain and "OVERWRITEPROTOCOL" not in plain
    assert "expose" not in behind
    assert "TRUSTED_PROXIES: 10.231.0.1" in behind and "OVERWRITEPROTOCOL" not in behind


def test_a_power_cut_s_leftovers_are_cleared_before_an_app_starts(bsd, config):
    apps.create(runtime, bsd, config, "site", DIRECTOR, files={"makejail": MAKEJAIL})
    bsd.outputs[("containers", "ps")] = (JAILS.replace("site-web\tUP", "site-web\tDOWN"), "")
    bsd.outputs[("containers", "app_members")] = (DESCRIBE, "")
    bsd.outputs[("containers", "info")] = ("5.9.1", "")
    apps.reconcile(runtime, containers, bsd, {"enabled": True, "apps": {"site": {"dataset": "tank/apps/site", "state": "up"}}})
    own = [a for a in bsd.actions() if a.startswith("containers.compose")]
    assert own[-2:] == ["containers.compose_repair", "containers.compose_up"]
    [repair] = [kw for c, a, kw in bsd.calls if a == "compose_repair"]
    assert repair["project"] == "site"


def test_a_reinstall_frees_the_proxy_port_first_and_keeps_the_app_when_it_fails(bsd, monkeypatch):
    import catalogue
    base, app = linux_app(bsd, "films", "jellyfin")
    config = FakeConfig({"enabled": True, "pool": "tank", "datasets": {}, "apps": {"films": app}})
    order = []
    monkeypatch.setattr(catalogue, "proxy_sync", lambda *args: order.append("proxy_sync") or [])

    def fail(*args):
        order.append("install")
        raise catalogue.CatalogueError("port 8096 is already in use")

    monkeypatch.setattr(catalogue, "install", fail)
    with pytest.raises(catalogue.CatalogueError):
        catalogue.reinstall(runtime, apps, containers, bsd, config, "films", app, str(base / "appjail-director.yml"))
    assert order == ["proxy_sync", "install"]
    assert config.get_module_config("containers")["apps"]["films"] == app
