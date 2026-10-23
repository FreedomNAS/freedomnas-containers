import json
import pwd
import re
import subprocess
from pathlib import Path

import pytest

import apps
import catalogue
import containers
import runtime
from conftest import FakeConfig

SECTION = {"enabled": True, "pool": "tank", "datasets": {}}
LISTENING = "127.0.0.53%lo:53\n0.0.0.0:8080\n[::]:22\n"


@pytest.fixture
def config():
    return FakeConfig(dict(SECTION))


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    class Account:
        pw_uid, pw_gid = 1001, 1001
    monkeypatch.setattr(catalogue.pwd, "getpwnam", lambda name: Account())


@pytest.fixture
def nas(engine):
    engine.outputs[("containers", "listening")] = (LISTENING, "")
    return engine


def install(engine, config, entry_id, name=None, **changes):
    taken = catalogue.used_ports(containers, engine, config=config)
    values = catalogue.defaults(catalogue.entry(entry_id), config, "sarah", taken)
    values.update(changes)
    return catalogue.install(runtime, apps, containers, engine, config, entry_id, name or entry_id, values)


def env_of(engine, name):
    path = Path(engine.datasets[f"tank/apps/{name}"]["mountpoint"]) / ".env"
    return catalogue.parse_env(path.read_text())


def test_every_entry_is_complete():
    ids = [e["id"] for e in catalogue.entries()]
    assert ids == ["homeassistant", "immich", "jellyfin", "nextcloud", "pihole", "vaultwarden"]
    for item in catalogue.entries():
        provided = {s["key"] for s in item["settings"] if s["type"] != "user"} | set(item.get("fixed", {}))
        provided |= {s[k] for s in item["settings"] if s["type"] == "user" for k in ("uid", "gid")}
        provided |= {"APP"}
        for recipe in [d for d in item["_dir"].iterdir() if d.is_dir()]:
            for file in recipe.iterdir():
                used = set(re.findall(r"\$\{([A-Z_]+)", file.read_text()))
                assert used <= provided, (item["id"], file.name, used - provided)
        assert item["web"]["port"] in provided or item["web"].get("fixed"), item["id"]


def test_no_entry_names_its_containers_or_project():
    for item in catalogue.entries():
        compose = (item["_dir"] / "linux/compose.yaml").read_text()
        assert "container_name" not in compose and not compose.startswith("name:"), item["id"]


def test_defaults_skip_ports_in_use_and_use_the_system_timezone(nas, config):
    nas.outputs[("containers", "ps")] = (json.dumps({
        "id": "a", "name": "x", "image": "x", "state": "running", "status": "Up",
        "published": "0.0.0.0:8096->8096/tcp, [::]:8096->8096/tcp"}), "")
    values = catalogue.defaults(catalogue.entry("jellyfin"), config, "sarah", catalogue.used_ports(containers, nas))
    assert values["PORT"] == "8097" and values["TZ"] == "Europe/Rome" and values["USER"] == "sarah"


def test_jellyfin_gets_its_env_and_folders_owned_by_its_user(nas, config):
    result = install(nas, config, "jellyfin")
    env = env_of(nas, "jellyfin")
    assert env["PUID"] == "1001" and env["MEDIA"] == "./media" and env["PORT"] == "8096"
    owned = {Path(p).name: o for p, o in nas.owners.items()}
    assert owned == {"config": ("1001", "1001"), "cache": ("1001", "1001"), "media": ("1001", "1001")}
    assert config.section["apps"]["jellyfin"]["catalogue"] == "jellyfin"
    assert result["web_port"] == "8096" and result["shown"] == {}


def test_a_media_folder_must_be_on_a_pool(nas, config, tmp_path):
    media = tmp_path / "tank-media"
    media.mkdir()
    with pytest.raises(catalogue.CatalogueError, match="not on a pool"):
        install(nas, config, "jellyfin", MEDIA=str(media))
    nas.extra_datasets.append(f"tank/media\t1\t1\t1\t{media}\tfilesystem\tyes\t-")
    install(nas, config, "jellyfin", MEDIA=str(media))
    assert env_of(nas, "jellyfin")["MEDIA"] == str(media)


@pytest.mark.parametrize("folder", ["/etc", "../x", "./a/../b", "/tank/missing", "media", "./a b"])
def test_folders_that_are_refused(nas, config, folder):
    with pytest.raises(catalogue.CatalogueError):
        install(nas, config, "jellyfin", MEDIA=folder)


def test_passwords_are_generated_and_shown_once(nas, config):
    result = install(nas, config, "nextcloud")
    env = env_of(nas, "nextcloud")
    assert result["shown"] == {"Administrator password": env["ADMIN_PASSWORD"]}
    assert len(env["DB_PASSWORD"]) >= 20 and env["DB_PASSWORD"] != env["DB_ROOT_PASSWORD"]


@pytest.mark.parametrize("change", [{"PORT": "8080"}, {"PORT": "22"}, {"PORT": "70000"},
                                    {"ADMIN_PASSWORD": "short"}, {"ADMIN_PASSWORD": "has'quote1"},
                                    {"ADMIN_USER": "a b"}, {"TRUSTED_DOMAINS": "x;y"}])
def test_bad_values_create_nothing(nas, config, change):
    with pytest.raises(catalogue.CatalogueError):
        install(nas, config, "nextcloud", **change)
    assert "tank/apps/nextcloud" not in nas.datasets


def test_a_user_that_is_not_a_freedomnas_account_is_refused(nas, config):
    with pytest.raises(catalogue.CatalogueError, match="account"):
        install(nas, config, "jellyfin", USER="root")


def test_pihole_ignores_the_local_resolver_but_not_a_dns_on_the_lan(nas, config):
    install(nas, config, "pihole", DNS_ADDRESS="192.168.1.10")
    assert env_of(nas, "pihole")["DNS_ADDRESS"] == "192.168.1.10"
    apps.remove(runtime, nas, config, "pihole", destroy_data=True)
    nas.outputs[("containers", "listening")] = (LISTENING + "0.0.0.0:53\n", "")
    with pytest.raises(catalogue.CatalogueError, match="port 53"):
        install(nas, config, "pihole", DNS_ADDRESS="192.168.1.10")


def test_pihole_ignores_a_dns_on_another_address_of_the_nas(nas, config):
    nas.outputs[("containers", "listening")] = (LISTENING + "10.231.0.1:53\n", "")
    install(nas, config, "pihole", DNS_ADDRESS="192.168.1.10")
    apps.remove(runtime, nas, config, "pihole", destroy_data=True)
    nas.outputs[("containers", "listening")] = (LISTENING + "192.168.1.10:53\n", "")
    with pytest.raises(catalogue.CatalogueError, match="port 53"):
        install(nas, config, "pihole", DNS_ADDRESS="192.168.1.10")
    nas.outputs[("containers", "listening")] = (LISTENING + "*:53\n", "")
    with pytest.raises(catalogue.CatalogueError, match="port 53"):
        install(nas, config, "pihole", DNS_ADDRESS="192.168.1.10")


def test_home_assistant_needs_its_fixed_port(nas, config):
    nas.outputs[("containers", "listening")] = (LISTENING + "0.0.0.0:8123\n", "")
    with pytest.raises(catalogue.CatalogueError, match="8123"):
        install(nas, config, "homeassistant")


def test_a_reinstall_on_kept_data_keeps_the_passwords(nas, config):
    install(nas, config, "nextcloud")
    first = env_of(nas, "nextcloud")
    apps.remove(runtime, nas, config, "nextcloud")
    install(nas, config, "nextcloud")
    second = env_of(nas, "nextcloud")
    assert second["DB_PASSWORD"] == first["DB_PASSWORD"] and second["ADMIN_PASSWORD"] == first["ADMIN_PASSWORD"]


def test_the_env_file_reads_back_with_docker_compose_quoting():
    env = {"A": "x", "B": "p@ss#w%rd"}
    assert catalogue.parse_env(catalogue.render_env(env)) == env


def test_compose_files_are_valid_for_docker_compose(tmp_path):
    from shutil import which
    if not which("docker"):
        pytest.skip("no docker here")
    for item in catalogue.entries():
        env = {s["key"]: "x" for s in item["settings"]} | {"PORT": "8000", "DNS_ADDRESS": "127.0.0.1",
               "MEDIA": "./m", "UPLOAD_LOCATION": "./l", "PUID": "1000", "PGID": "1000"} | item.get("fixed", {})
        folder = tmp_path / item["id"]
        folder.mkdir()
        (folder / "compose.yaml").write_text((item["_dir"] / "linux/compose.yaml").read_text())
        (folder / ".env").write_text(catalogue.render_env(env))
        run = subprocess.run(["docker", "compose", "-f", str(folder / "compose.yaml"), "config", "--quiet"],
                             capture_output=True, text=True)
        assert run.returncode == 0, (item["id"], run.stderr)
        profile = json.loads((Path(catalogue.__file__).parents[2] / "profiles/containers/containers-debian.json")
                             .read_text())["containers"]["compose_check"]
        argv = profile.replace("{dir}", str(folder)).split()
        run = subprocess.run(argv, input=(item["_dir"] / "linux/compose.yaml").read_text(),
                             capture_output=True, text=True, cwd="/")
        assert run.returncode == 0, (item["id"], "stdin", run.stderr)


def test_secrets_never_leave_the_server_before_the_install(config):
    values = catalogue.defaults(catalogue.entry("nextcloud"), config, "sarah", set())
    assert "DB_PASSWORD" not in values and "DB_ROOT_PASSWORD" not in values
    assert all(s["type"] != "secret" for s in catalogue.summary(catalogue.entry("nextcloud"))["settings"])


def test_a_catalogue_compose_file_is_checked_with_its_env(nas, config):
    seen = []
    original = nas.execute_with_stdin

    def spy(category, action, stdin_data, timeout=30, **kw):
        if action == "compose_check":
            env = Path(kw["dir"]) / ".env"
            seen.append((action, env.read_text() if env.exists() else None))
        return original(category, action, stdin_data, timeout=timeout, **kw)

    nas.execute_with_stdin = spy
    install(nas, config, "jellyfin")
    [(action, env_text)] = seen
    assert action == "compose_check" and "MEDIA='./media'" in env_text


def test_a_failed_check_with_the_env_leaves_no_dataset(nas, config):
    nas.failing.add(("containers", "compose_check"))
    with pytest.raises(apps.AppError):
        install(nas, config, "jellyfin")
    assert "tank/apps/jellyfin" not in nas.datasets and "jellyfin" not in (config.section.get("apps") or {})


def test_editing_an_app_checks_it_in_its_dataset(nas, config):
    install(nas, config, "jellyfin")
    nas.calls.clear()
    compose = (catalogue.entry("jellyfin")["_dir"] / "linux/compose.yaml").read_text()
    apps.update(runtime, nas, config, "jellyfin", compose)
    [check] = [kw for c, a, kw in nas.calls if a == "compose_check"]
    assert (Path(check["dir"]) / ".env").exists()


def test_a_stopped_catalogue_app_keeps_its_ports(nas, config):
    install(nas, config, "jellyfin")
    assert config.section["apps"]["jellyfin"]["ports"] == [8096]
    values = catalogue.defaults(catalogue.entry("jellyfin"), config, "sarah",
                                catalogue.used_ports(containers, nas, config=config))
    assert values["PORT"] == "8097"
    with pytest.raises(catalogue.CatalogueError, match="8096"):
        install(nas, config, "jellyfin", name="jellyfin2", PORT="8096")


def test_pihole_records_its_dns_port(nas, config):
    install(nas, config, "pihole", DNS_ADDRESS="192.168.1.10")
    assert config.section["apps"]["pihole"]["ports"] == [53, 8053]


def test_nextclouds_data_folder_belongs_to_www_data(nas, config):
    install(nas, config, "nextcloud")
    assert env_of(nas, "nextcloud")["DATA"] == "./data"
    owned = {Path(p).name: o for p, o in nas.owners.items()}
    assert owned == {"data": ("33", "33")}


def test_nextclouds_data_folder_on_the_pool_is_new_or_empty(nas, config, tmp_path):
    pool = tmp_path / "tank"
    pool.mkdir()
    nas.extra_datasets.append(f"tank\t1\t1\t1\t{pool}\tfilesystem\tyes\t-")
    install(nas, config, "nextcloud", DATA=f"{pool}/nextcloud/")
    assert env_of(nas, "nextcloud")["DATA"] == f"{pool}/nextcloud"
    assert nas.owners[f"{pool}/nextcloud"] == ("33", "33")

    apps.remove(runtime, nas, config, "nextcloud", destroy_data=True)
    full = pool / "photos"
    full.mkdir()
    (full / "a.jpg").write_text("x")
    with pytest.raises(catalogue.CatalogueError, match="not empty"):
        install(nas, config, "nextcloud", DATA=str(full))
    with pytest.raises(catalogue.CatalogueError, match="not an existing directory"):
        install(nas, config, "nextcloud", DATA=f"{pool}/missing/nextcloud")


def test_the_folders_offered_leave_out_the_apps_and_docker(nas, tmp_path):
    nas.extra_datasets += ["tank\t1\t1\t1\t/tank\tfilesystem\tyes\t-",
                           "tank/media\t1\t1\t1\t/tank/media\tfilesystem\tyes\t-",
                           "tank/apps\t1\t1\t1\t/tank/apps\tfilesystem\tyes\t-",
                           "tank/apps/site\t1\t1\t1\t/tank/apps/site\tfilesystem\tyes\t-",
                           "tank/containers/docker\t1\t1\t1\t/srv/docker\tfilesystem\tyes\t-"]
    folders = catalogue.pool_folders(nas)
    assert "/tank" in folders and "/tank/media" in folders
    assert not any(f.startswith(("/tank/apps", "/srv/docker")) for f in folders)
