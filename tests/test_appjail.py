"""
AppJail on FreeBSD: the module points AppJail and buildah at the pool's
datasets, gives pf AppJail's anchors without touching the rest of
/etc/pf.conf, and repeats nothing that is already in place.
"""

import json
from pathlib import Path

import appjail_runtime
from conftest import REPO, CommandExecutionError, CommandTranslationError

PROFILE = json.loads((REPO / "profiles/containers/containers-freebsd.json").read_text())

ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.2
        fib: 0
  interface: fnas_bridge1
      flags: <UP,GATEWAY,DONE,STATIC>
"""


class BSD:
    def __init__(self, files=None, networks="", release=True):
        self.files = dict(files or {})
        self.networks = networks
        self.release = release
        self.calls = []

    def get_profile_value(self, category, key):
        try:
            return PROFILE[category][key]
        except KeyError:
            raise CommandTranslationError(key)

    def execute(self, category, action, check=True, timeout=30, **kw):
        self.calls.append((category, action, kw))
        answers = {
            "default_route": ROUTE,
            "system_version": "15.1-RELEASE-p3\n",
            "system_arch": "amd64\n",
            "network_list": self.networks,
        }
        if action == "file_read":
            return self.files.get(kw["file"], "").strip(), ""
        if action == "release_check" and not self.release:
            raise CommandExecutionError("missing")
        return answers.get(action, ""), ""

    def execute_with_stdin(self, category, action, data, **kw):
        self.calls.append((category, action, kw))
        self.files[kw["file"]] = data
        return "", ""

    def actions(self):
        return [a for _, a, _ in self.calls]


DATASETS = {"appjail": "tank/containers/appjail", "oci": "tank/containers/oci"}
DIRS = PROFILE["containers"]["data_dirs"]


def test_appjail_and_buildah_are_pointed_at_the_pools_datasets():
    bsd = BSD()
    done = appjail_runtime.configure(bsd, "tank", DATASETS, DIRS)
    conf = bsd.files["/usr/local/etc/appjail/appjail.conf"]
    assert "ZPOOL=tank\n" in conf and "ZROOTFS=containers/appjail\n" in conf
    assert "EXT_IF=fnas_bridge1\n" in conf and "FREEBSD_VERSION=15.1-RELEASE\n" in conf
    storage = bsd.files["/usr/local/etc/containers/storage.conf"]
    assert 'fsname = "tank/containers/oci"' in storage
    assert 'graphroot = "/var/db/containers/storage"' in storage
    assert done[:3] == ["appjail.conf", "storage.conf", "pf.conf"]
    assert "network fnasjails" in done
    [add] = [kw for _, a, kw in bsd.calls if a == "network_add"]
    assert add == {"name": "fnasjails", "network": "10.231.0.0/24"}


def test_a_second_run_changes_nothing():
    bsd = BSD()
    appjail_runtime.configure(bsd, "tank", DATASETS, DIRS)
    again = BSD(files=bsd.files, networks="fnasjails\t10.231.0.0\t\n")
    assert appjail_runtime.configure(again, "tank", DATASETS, DIRS) == []
    assert "file_write" not in again.actions() and "network_add" not in again.actions()


def test_a_missing_release_is_fetched():
    bsd = BSD(release=False, networks="fnasjails\t10.231.0.0\t\n")
    assert "release 15.1-RELEASE" in appjail_runtime.configure(bsd, "tank", DATASETS, DIRS)
    assert "release_fetch" in bsd.actions()


def test_pf_gets_the_anchors_and_a_pass_rule_when_it_has_none():
    text = appjail_runtime.pf_conf("")
    assert text.splitlines() == [
        "# freedomnas appjail begin",
        'nat-anchor "appjail-nat/jail/*"',
        'nat-anchor "appjail-nat/network/*"',
        'rdr-anchor "appjail-rdr/*"',
        "pass all",
        "# freedomnas appjail end",
    ]


def test_a_users_pf_rules_stay_and_keep_their_own_policy():
    mine = "set skip on lo0\nblock in all\npass out all keep state\n"
    text = appjail_runtime.pf_conf(mine)
    assert text.endswith(mine)
    assert "pass all" not in text
    assert appjail_runtime.pf_conf(text) == text


def test_without_a_default_route_nothing_is_written():
    bsd = BSD()
    bsd.execute = lambda c, a, check=True, **kw: ("", "")
    try:
        appjail_runtime.configure(bsd, "tank", DATASETS, DIRS)
    except CommandTranslationError as exc:
        assert "default route" in str(exc)
    else:
        raise AssertionError("configured without a default route")


def test_appjails_services_stay_enabled_at_boot():
    import runtime

    class Engine(BSD):
        os_type = "freebsd"
        init_system = "bsd_rc"

    engine = Engine()
    runtime.keep_from_boot(engine)
    assert [(a, kw["service_name"]) for c, a, kw in engine.calls if c == "services"] == [
        ("enable", "appjail-natnet"), ("enable", "appjail-health"), ("enable", "appjail")]
