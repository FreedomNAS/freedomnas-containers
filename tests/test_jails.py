"""
Jails as containers on FreeBSD: AppJail's tab-separated listing and
buildah's images in the shapes docker.py gives, and `oci run` words that
reach AppJail one by one, spaces included.
"""

import json

import pytest

import jails
from conftest import REPO

PROFILE = json.loads((REPO / "profiles/containers/containers-freebsd.json").read_text())

LIST = (
    "bootprova\tUP\tthin\t15.1-RELEASE\t \t10.231.0.2\t \t2026-10-15 14:38:17\t1\t0\t\n"
    "web\tDOWN\tthick\t15.1-RELEASE\t8099 -> 80/tcp,7359/udp\t10.231.0.3\t"
    "docker.io/freebsd/freebsd-runtime:15.1\t2026-10-15 14:42:22\t1\t1\t\n"
)
IMAGES = json.dumps([
    {"id": "90c493675429", "names": ["docker.io/freebsd/freebsd-runtime:15.1"],
     "size": "34.1 MB", "createdat": "4 months ago"},
    {"id": "aa11bb22cc33", "names": [], "size": "1 MB", "createdat": "1 day ago"},
])
LOGS = """TYPE   ENTITY     SUBTYPE        LOG
jails  web        console        2026-10-14.log
jails  web        container      2026-10-14.log
jails  web        container      2026-10-15.log
jails  bootprova  console        2026-10-15.log
"""


class BSD:
    def __init__(self):
        self.calls = []
        self.stdin = []

    def get_profile_value(self, category, key):
        return PROFILE[category][key]

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((action, kw))
        return {"ps": LIST, "images": IMAGES, "log_list": LOGS, "log_read": "tick\n"}.get(action, ""), ""

    def execute_with_stdin(self, category, action, data, timeout=30, **kw):
        self.calls.append((action, kw))
        self.stdin.append(data)
        return "", ""


def test_jails_are_listed_like_docker_containers():
    web, boot = sorted(jails.containers(BSD()), key=lambda c: c["name"], reverse=True)
    assert (boot["name"], boot["state"], boot["image"], boot["ports"]) == (
        "bootprova", "running", "FreeBSD 15.1-RELEASE", "")
    assert (web["state"], web["image"], web["ports"], web["address"]) == (
        "exited", "docker.io/freebsd/freebsd-runtime:15.1", "8099→80, 7359/udp", "10.231.0.3")
    assert [c["name"] for c in jails.containers(BSD())] == ["bootprova", "web"]


def test_images_come_from_buildah():
    runtime_image, dangling = jails.images(BSD())
    assert (runtime_image["repository"], runtime_image["tag"], runtime_image["reference"]) == (
        "docker.io/freebsd/freebsd-runtime", "15.1", "docker.io/freebsd/freebsd-runtime:15.1")
    assert dangling["reference"] == "aa11bb22cc33"


def test_the_newest_log_of_the_process_is_read():
    bsd = BSD()
    assert jails.logs(bsd, "web", 50) == "tick\n"
    assert ("log_read", {"path": "jails/web/container/2026-10-15.log", "lines": "50"}) in bsd.calls


def test_a_jail_without_a_process_shows_its_console():
    bsd = BSD()
    jails.logs(bsd, "bootprova")
    assert ("log_read", {"path": "jails/bootprova/console/2026-10-15.log", "lines": "200"}) in bsd.calls


def test_run_words_reach_appjail_whole():
    bsd = BSD()
    name = jails.run(bsd, {"image": "docker.io/freebsd/freebsd-runtime:15.1", "name": "web",
                           "ports": ["8099:80", "7359:7359/udp"],
                           "volumes": ["/tank/media:/media:ro", "/tank/web:/srv"],
                           "env": ["TZ=Europe/Rome"], "restart": "unless-stopped"})
    assert name == "web"
    assert bsd.stdin[0].split("\0") == [
        "-o", "virtualnet=fnasjails:<random> default", "-o", "nat",
        "-o", "template=/usr/local/share/examples/appjail/templates/freebsd-oci.conf",
        "-o", "expose=8099:80", "-o", "expose=7359:7359 proto:udp",
        "-o", "fstab=/tank/media /media nullfs ro", "-o", "fstab=/tank/web /srv",
        "-e", "TZ=Europe/Rome",
        "docker.io/freebsd/freebsd-runtime:15.1", "web"]
    assert "boot_off" not in [a for a, _ in bsd.calls]


def test_a_jail_that_should_not_restart_does_not_start_at_boot():
    bsd = BSD()
    jails.run(bsd, {"image": "docker.io/freebsd/freebsd-runtime:15.1", "name": "web", "restart": "no"})
    assert ("boot_off", {"container": "web"}) in bsd.calls


@pytest.mark.parametrize("spec, problem", [
    ({"image": "x"}, "jail name"),
    ({"image": "x", "name": "a b"}, "jail name"),
    ({"image": "x", "name": "web", "volumes": ["data:/srv"]}, "volume"),
    ({"image": "x", "name": "web", "ports": ["1.2.3.4:80:80"]}, "port"),
    ({"image": "x", "name": "web", "env": ["A=b c"]}, "NAME=value"),
])
def test_malformed_options_are_refused(spec, problem):
    with pytest.raises(jails.ContainerError, match=problem):
        jails.run_args(spec)


def test_removing_a_running_jail_needs_force():
    bsd = BSD()
    jails.remove(bsd, "web")
    jails.remove(bsd, "web", force=True)
    assert [a for a, _ in bsd.calls] == ["remove", "remove_force"]


def test_a_failure_is_told_without_the_debug_log():
    exc = jails.CommandExecutionError(
        "Command failed. Stderr: [00:00:01] [ debug ] [web] quick parameters: x\n"
        "[00:00:04] [ debug ] [web] Compiling fstab #0: /tank/a /srv nullfs ro 0 0\n"
        "mkdir: /usr/local/appjail/jails/web/jail//srv/x: No such file or directory\n"
        "[00:00:04] [ error ] [web] Error compiling fstab file.\n")
    assert jails.error_text(exc) == (
        "mkdir: /usr/local/appjail/jails/web/jail//srv/x: No such file or directory\n"
        "Error compiling fstab file.")


def test_a_warning_alone_is_told_without_its_prefix():
    exc = jails.CommandExecutionError("Command failed. Stderr: [00:00:00] [ warn  ] [web] web is currently running.")
    assert jails.error_text(exc) == "web is currently running."
