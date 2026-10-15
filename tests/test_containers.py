"""
Containers and images through the profile alone: the same functions read
Docker's and AppJail's listings, build their run words and pick their
errors, with nothing about either runtime in the module's Python.
"""

import json

import pytest

import containers
from conftest import REPO, CommandExecutionError, CommandTranslationError


def profile(os_name):
    return json.loads((REPO / f"profiles/containers/containers-{os_name}.json").read_text())


class Stub:
    def __init__(self, os_name, outputs=None):
        self.profile = profile(os_name)
        self.outputs = outputs or {}
        self.calls = []
        self.stdin = []

    def get_profile_value(self, category, key):
        try:
            return self.profile[category][key]
        except KeyError:
            raise CommandTranslationError(key)

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((action, kw))
        return self.outputs.get(action, ("", ""))

    def execute_with_stdin(self, category, action, data, timeout=30, **kw):
        self.calls.append((action, kw))
        self.stdin.append(data)
        return self.outputs.get(action, ("", ""))


DOCKER_PS = "\n".join(json.dumps(row) for row in [
    {"id": "a1", "name": "web", "image": "nginx:alpine", "state": "exited", "status": "Exited (0)",
     "published": "", "created": "2026-10-11"},
    {"id": "b2", "name": "db", "image": "postgres:17", "state": "running", "status": "Up 2 minutes",
     "published": "0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp, 0.0.0.0:8090->80/tcp", "created": "2026-10-11"},
])
DOCKER_IMAGES = "\n".join(json.dumps(row) for row in [
    {"id": "sha1", "repository": "nginx", "tag": "alpine", "size": "52MB", "created": "2 weeks ago", "containers": "1"},
    {"id": "sha2", "repository": "<none>", "tag": "<none>", "size": "10MB", "created": "a month ago", "containers": "0"},
])
JAILS = (
    "bootprova\tUP\tthin\t15.1-RELEASE\t \t10.231.0.2\t \t2026-10-15 14:38:17\t\n"
    "web\tDOWN\tthick\t15.1-RELEASE\t8099 -> 80/tcp,7359/udp\t10.231.0.3\t"
    "docker.io/freebsd/freebsd-runtime:15.1\t2026-10-15 14:42:22\t\n"
)
BUILDAH = '{"id":"90c4","repository":"docker.io/freebsd/freebsd-runtime","tag":"15.1","size":"34 MB","created":"4 months ago"}'


def test_docker_containers_running_first_with_short_ports():
    db, web = containers.containers(Stub("debian", {"ps": (DOCKER_PS, "")}))
    assert (db["name"], db["ports"], db["host_ports"]) == ("db", "5432, 8090→80", [5432, 8090])
    assert (web["name"], web["state"], web["ports"]) == ("web", "exited", "")


def test_jails_are_listed_in_the_same_shape():
    boot, web = containers.containers(Stub("freebsd", {"ps": (JAILS, "")}))
    assert (boot["name"], boot["state"], boot["image"], boot["ports"]) == (
        "bootprova", "running", "FreeBSD 15.1-RELEASE", "")
    assert (web["state"], web["image"], web["ports"], web["address"], web["host_ports"]) == (
        "exited", "docker.io/freebsd/freebsd-runtime:15.1", "8099→80, 7359/udp", "10.231.0.3", [7359, 8099])


def test_images_without_a_tag_are_named_by_id():
    images = containers.images(Stub("debian", {"images": (DOCKER_IMAGES, "")}))
    assert [i["reference"] for i in images] == ["nginx:alpine", "sha2"]
    [image] = containers.images(Stub("freebsd", {"images": (BUILDAH, "")}))
    assert image["reference"] == "docker.io/freebsd/freebsd-runtime:15.1"


@pytest.mark.parametrize("os_name, name, ok", [
    ("debian", "my_app.v2", True), ("debian", "-rm", False), ("debian", "a b", False), ("debian", None, False),
    ("freebsd", "web-1", True), ("freebsd", "my.app", False), ("freebsd", "x;y", False),
])
def test_names_follow_the_runtimes_pattern(os_name, name, ok):
    if ok:
        assert containers.check_name(Stub(os_name), name) == name
    else:
        with pytest.raises(containers.ContainerError):
            containers.check_name(Stub(os_name), name)


@pytest.mark.parametrize("image", ["", "-x", "nginx alpine", "nginx;rm", "$(id)"])
def test_images_that_are_refused(image):
    with pytest.raises(containers.ContainerError):
        containers.check_image(image)


def test_actions_go_through_the_profile():
    stub = Stub("debian")
    containers.act(stub, "web", "restart")
    containers.remove(stub, "web", force=True)
    assert stub.calls == [("restart", {"container": "web"}), ("remove_force", {"container": "web"})]
    with pytest.raises(containers.ContainerError):
        containers.act(stub, "web", "kill")


def test_logs_join_both_streams_and_bound_the_lines():
    stub = Stub("freebsd", {"logs": ("out line", "err line")})
    assert containers.logs(stub, "web", 99999) == "out line\nerr line"
    assert stub.calls[-1] == ("logs", {"container": "web", "lines": "5000"})


FULL = {"name": "web", "image": "nginx:alpine", "ports": ["8081:80", "53:53/udp"],
        "volumes": ["/tank/media:/media:ro", "/tank/web:/srv"], "env": ["TZ=Europe/Rome", "EMPTY="],
        "restart": "always"}


def test_docker_run_words():
    assert containers.run_words(Stub("debian"), {**FULL, "volumes": FULL["volumes"] + ["webdata:/data"]}) == [
        "--name", "web", "--restart", "always", "-p", "8081:80", "-p", "53:53/udp",
        "-v", "/tank/media:/media:ro", "-v", "/tank/web:/srv", "-v", "webdata:/data",
        "-e", "TZ=Europe/Rome", "-e", "EMPTY=", "nginx:alpine"]
    assert containers.run_words(Stub("debian"), {"image": "nginx", "ports": ["127.0.0.1:8080:80"]}) == [
        "--restart", "unless-stopped", "-p", "127.0.0.1:8080:80", "nginx"]


def test_jail_run_words_keep_their_spaces():
    stub = Stub("freebsd")
    assert containers.run(stub, FULL) == "web"
    assert stub.stdin[0].split("\0") == [
        "-o", "virtualnet=fnasjails:<random> default", "-o", "nat",
        "-o", "template=/usr/local/share/examples/appjail/templates/freebsd-oci.conf",
        "-o", "expose=8081:80", "-o", "expose=53:53 proto:udp",
        "-o", "fstab=/tank/media /media nullfs ro", "-o", "fstab=/tank/web /srv",
        "-e", "TZ=Europe/Rome", "-e", "EMPTY=",
        "nginx:alpine", "web"]
    assert "boot_off" not in [a for a, _ in stub.calls]


def test_a_jail_that_should_not_restart_does_not_start_at_boot():
    stub = Stub("freebsd")
    containers.run(stub, {"image": "nginx", "name": "web", "restart": "no"})
    assert ("boot_off", {"container": "web"}) in stub.calls


def test_docker_run_answers_with_the_new_containers_id():
    stub = Stub("debian", {"run": ("Unable to find image\nabc123", "")})
    assert containers.run(stub, {"image": "nginx"}) == "abc123"
    assert stub.stdin == ["--restart\0unless-stopped\0nginx"]


@pytest.mark.parametrize("os_name, spec, problem", [
    ("debian", {"image": "x", "ports": ["80"]}, "port mapping"),
    ("debian", {"image": "x", "ports": ["8080:80 --privileged"]}, "port mapping"),
    ("debian", {"image": "x", "volumes": ["data"]}, "volume"),
    ("debian", {"image": "x", "volumes": ["/a:/b:rx"]}, "volume"),
    ("debian", {"image": "x", "env": ["X=two words"]}, "NAME=value"),
    ("debian", {"image": "x", "restart": "sometimes"}, "restart policy"),
    ("debian", {"image": "x", "name": "--privileged"}, "container name"),
    ("freebsd", {"image": "x"}, "name is required"),
    ("freebsd", {"image": "x", "name": "a b"}, "jail name"),
    ("freebsd", {"image": "x", "name": "web", "volumes": ["data:/srv"]}, "only folders"),
    ("freebsd", {"image": "x", "name": "web", "ports": ["1.2.3.4:80:80"]}, "one address"),
])
def test_malformed_requests_are_refused(os_name, spec, problem):
    with pytest.raises(containers.ContainerError, match=problem):
        containers.run_words(Stub(os_name), spec)


def test_an_appjail_failure_is_told_without_the_debug_log():
    exc = CommandExecutionError(
        "Command failed. Stderr: [00:00:01] [ debug ] [web] quick parameters: x\n"
        "[00:00:04] [ debug ] [web] Compiling fstab #0: /tank/a /srv nullfs ro 0 0\n"
        "mkdir: /usr/local/appjail/jails/web/jail//srv/x: No such file or directory\n"
        "[00:00:04] [ error ] [web] Error compiling fstab file.\n")
    assert containers.error_text(Stub("freebsd"), exc) == (
        "mkdir: /usr/local/appjail/jails/web/jail//srv/x: No such file or directory\n"
        "Error compiling fstab file.")
    warn = CommandExecutionError("Command failed. Stderr: [00:00:00] [ warn  ] [web] web is currently running.")
    assert containers.error_text(Stub("freebsd"), warn) == "web is currently running."


def test_a_docker_failure_is_its_stderr():
    exc = CommandExecutionError("Command failed\nCommand: docker rm x\nStderr: Error: No such container: x")
    assert containers.error_text(Stub("debian"), exc) == "Error: No such container: x"
