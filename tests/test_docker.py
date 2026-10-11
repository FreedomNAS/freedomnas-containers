import json

import pytest

import docker
from freedomnas.engine import FreedomNASEngine

PS = "\n".join(json.dumps(row) for row in [
    {"ID": "a1", "Names": "web", "Image": "nginx:alpine", "State": "exited", "Status": "Exited (0)",
     "Ports": "", "CreatedAt": "2026-10-11", "Labels": ""},
    {"ID": "b2", "Names": "db", "Image": "postgres:17", "State": "running", "Status": "Up 2 minutes",
     "Ports": "0.0.0.0:5432->5432/tcp", "CreatedAt": "2026-10-11",
     "Labels": "com.docker.compose.project=nextcloud,com.docker.compose.service=db"},
])
IMAGES = "\n".join(json.dumps(row) for row in [
    {"ID": "sha1", "Repository": "nginx", "Tag": "alpine", "Size": "52MB", "CreatedSince": "2 weeks ago"},
    {"ID": "sha2", "Repository": "<none>", "Tag": "<none>", "Size": "10MB", "CreatedSince": "a month ago"},
])


def test_containers_running_first_with_their_compose_project(engine):
    engine.outputs[("containers", "ps")] = (PS, "")
    rows = docker.containers(engine)
    assert [c["name"] for c in rows] == ["db", "web"]
    assert rows[0]["project"] == "nextcloud" and rows[1]["project"] is None


def test_images_without_a_tag_are_named_by_id(engine):
    engine.outputs[("containers", "images")] = (IMAGES, "")
    assert [i["reference"] for i in docker.images(engine)] == ["nginx:alpine", "sha2"]


@pytest.mark.parametrize("name", ["web", "db-1", "a1b2c3", "my_app.v2"])
def test_names_that_are_accepted(name):
    assert docker.check_name(name) == name


@pytest.mark.parametrize("name", ["", "-rm", "a b", "../x", "x;y", "web\n", None])
def test_names_that_are_refused(name):
    with pytest.raises(docker.ContainerError):
        docker.check_name(name)


@pytest.mark.parametrize("image", ["nginx", "nginx:alpine", "ghcr.io/home-assistant/home-assistant:stable",
                                   "lscr.io/linuxserver/jellyfin@sha256:abc"])
def test_images_that_are_accepted(image):
    assert docker.check_image(image) == image


@pytest.mark.parametrize("image", ["", "-x", "nginx alpine", "nginx;rm", "$(id)"])
def test_images_that_are_refused(image):
    with pytest.raises(docker.ContainerError):
        docker.check_image(image)


def test_actions_go_through_the_profile(engine):
    docker.act(engine, "web", "restart")
    assert engine.calls[-1] == ("containers", "restart", {"container": "web"})
    with pytest.raises(docker.ContainerError):
        docker.act(engine, "web", "kill")


def test_a_running_container_is_removed_with_force(engine):
    docker.remove(engine, "web", force=True)
    assert engine.calls[-1][:2] == ("containers", "remove_force")


def test_logs_join_both_streams_and_bound_the_lines(engine):
    engine.outputs[("containers", "logs")] = ("out line", "err line")
    assert docker.logs(engine, "web", 99999) == "out line\nerr line"
    assert engine.calls[-1][2]["lines"] == "5000"


def test_run_arguments_for_a_full_spec():
    spec = {"name": "web", "ports": ["8081:80", "127.0.0.1:53:53/udp"], "volumes": ["webdata:/data", "/tank/media:/media:ro"],
            "env": ["TZ=Europe/Rome", "EMPTY="], "restart": "always"}
    assert docker.run_args(spec) == [
        "--name", "web", "--restart", "always", "-p", "8081:80", "-p", "127.0.0.1:53:53/udp",
        "-v", "webdata:/data", "-v", "/tank/media:/media:ro", "-e", "TZ=Europe/Rome", "-e", "EMPTY="]


def test_run_defaults_to_unless_stopped():
    assert docker.run_args({}) == ["--restart", "unless-stopped"]


@pytest.mark.parametrize("spec", [
    {"ports": ["80"]}, {"ports": ["8080:80 --privileged"]}, {"volumes": ["data"]},
    {"volumes": ["/a:/b:rx"]}, {"env": ["A B=1"]}, {"env": ["X=two words"]}, {"restart": "sometimes"},
    {"name": "--privileged"},
])
def test_run_arguments_that_are_refused(spec):
    with pytest.raises(docker.ContainerError):
        docker.run_args(spec)


def test_run_reaches_the_real_profile_as_separate_words(tmp_path):
    from conftest import PROFILES, REPO
    import shutil
    profiles = tmp_path / "profiles"
    shutil.copytree(PROFILES, profiles)
    shutil.copytree(REPO / "profiles" / "containers", profiles / "containers")
    engine = FreedomNASEngine(profile_dir=str(profiles), os_override="debian")
    command = engine.get_command("containers", "run",
                                 run_args=" ".join(docker.run_args({"name": "web", "ports": ["8081:80"]})),
                                 image="nginx:alpine")
    assert command == "docker run -d --name web --restart unless-stopped -p 8081:80 nginx:alpine"
