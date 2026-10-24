"""
Host configuration from the profile (AppJail on FreeBSD): files written
only when they change, pf's block kept apart from the user's rules, the
profile's configure commands run with the facts of the system.
"""

import json

import pytest

import runtime
from conftest import REPO, CommandTranslationError

PROFILE = json.loads((REPO / "profiles/containers/containers-freebsd.json").read_text())
DATASETS = {"appjail": "tank/containers/appjail", "oci": "tank/containers/oci"}
[PF] = [f for f in PROFILE["containers"]["config_files"] if f["path"] == "/etc/pf.conf"]


class BSD:
    os_type = "freebsd"
    init_system = "bsd_rc"

    def __init__(self, files=None, interface="fnas_bridge1\n"):
        self.files = dict(files or {})
        self.interface = interface
        self.calls = []

    def get_profile_value(self, category, key):
        try:
            return PROFILE[category][key]
        except KeyError:
            raise CommandTranslationError(key)

    def execute(self, category, action, check=True, timeout=30, **kw):
        self.calls.append((category, action, kw))
        answers = {"default_interface": self.interface, "system_version": "15.1-RELEASE\n", "system_arch": "amd64\n"}
        if action == "file_read":
            return self.files.get(kw["file"], "").strip(), ""
        return answers.get(action, ""), ""

    def execute_with_stdin(self, category, action, data, **kw):
        self.calls.append((category, action, kw))
        self.files[kw["file"]] = data
        return "", ""

    def actions(self):
        return [a for _, a, _ in self.calls]


def test_appjail_and_buildah_are_pointed_at_the_pools_datasets():
    bsd = BSD()
    assert runtime.configure(bsd, "tank", DATASETS) == [
        "appjail.conf", "storage.conf", "resolv.conf", "freedomnas-appjail-dnsmasq.conf", "pf.conf"]
    assert bsd.files["/usr/local/etc/appjail/resolv.conf"].endswith("nameserver 10.231.0.1\n")
    conf = bsd.files["/usr/local/etc/appjail/appjail.conf"]
    assert "ZPOOL=tank\n" in conf and "ZROOTFS=containers/appjail\n" in conf
    assert "EXT_IF=fnas_bridge1\n" in conf and "FREEBSD_VERSION=15.1-RELEASE\n" in conf
    storage = bsd.files["/usr/local/etc/containers/storage.conf"]
    assert 'fsname = "tank/containers/oci"' in storage
    assert 'graphroot = "/var/db/containers/storage"' in storage
    [configure] = [kw for _, a, kw in bsd.calls if a == "configure"]
    assert (configure["arch"], configure["version"], configure["pool"]) == ("amd64", "15.1-RELEASE", "tank")
    assert [kw["service_name"] for c, a, kw in bsd.calls if (c, a) == ("services", "start")] == [
        "pf", "dnsmasq", "appjail-dns"]
    assert bsd.actions().index("configure") < bsd.actions().index("start")


def test_a_second_run_writes_nothing():
    bsd = BSD()
    runtime.configure(bsd, "tank", DATASETS)
    again = BSD(files=bsd.files)
    assert runtime.configure(again, "tank", DATASETS) == []
    assert "file_write" not in again.actions()


def test_pf_gets_the_anchors_and_a_pass_rule_when_it_has_none():
    entry = PF
    assert runtime.file_text("", entry, {}).splitlines() == [
        "# freedomnas appjail begin",
        'nat-anchor "appjail-nat/jail/*"',
        'nat-anchor "appjail-nat/network/*"',
        'rdr-anchor "appjail-rdr/*"',
        "pass all",
        "# freedomnas appjail end",
    ]


def test_a_users_pf_rules_stay_and_keep_their_own_policy():
    entry = PF
    mine = "set skip on lo0\nblock in all\npass out all keep state\n"
    text = runtime.file_text(mine, entry, {})
    assert text.endswith(mine) and "pass all" not in text
    assert runtime.file_text(text, entry, {}) == text


def test_without_a_default_route_nothing_is_written():
    bsd = BSD(interface="")
    with pytest.raises(runtime.ContainersError, match="default route"):
        runtime.configure(bsd, "tank", DATASETS)
    assert "file_write" not in bsd.actions()


def test_appjails_services_stay_enabled_at_boot():
    bsd = BSD()
    runtime.keep_from_boot(bsd)
    assert [(a, kw["service_name"]) for c, a, kw in bsd.calls if c == "services"] == [
        ("enable", "appjail-natnet"), ("enable", "appjail-health"), ("enable", "appjail")]


def test_linux_raises_the_inotify_instances_once_and_keeps_them(engine):
    engine.files.clear()
    assert runtime.configure(engine, "tank", {"docker": "tank/containers/docker"}) == ["10-freedomnas-containers.conf"]
    assert "fs.inotify.max_user_instances = 1024" in engine.files["/etc/sysctl.d/10-freedomnas-containers.conf"]
    assert "containers.configure" in engine.actions()
    engine.calls.clear()
    assert runtime.configure(engine, "tank", {"docker": "tank/containers/docker"}) == []
    assert "containers.file_write" not in engine.actions()
