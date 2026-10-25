import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO.parent / "freedomnas-core"
PROFILES = REPO.parent / "freedomnas-profiles"
_LINK = Path(tempfile.mkdtemp(prefix="fnas-core-")) / "freedomnas"
_LINK.symlink_to(CORE)
sys.path.insert(0, str(_LINK.parent))

from freedomnas.engine import CommandExecutionError, CommandTranslationError  # noqa: E402
from freedomnas.package_manager import PackageManager  # noqa: E402

sys.path.insert(0, str(REPO / "modules" / "containers"))
import records  # noqa: E402
import runtime  # noqa: E402
import containers  # noqa: E402
import apps  # noqa: E402
import catalogue  # noqa: E402


class FakeEngine:
    """
    Pools, datasets and Docker in memory; the data directories are real
    directories under tmp, so moving aside and copying really happen.
    """

    def __init__(self, os_type="debian", init_system="systemd", tmp=None):
        self.os_type = os_type
        self.init_system = init_system
        self.tmp = Path(tmp)
        self.profile = json.loads((REPO / "profiles/containers" / f"containers-{os_type}.json").read_text())
        self.profile["containers"]["data_dirs"] = {
            "docker": str(self.tmp / "var-lib-docker"), "containerd": str(self.tmp / "var-lib-containerd")}
        self.calls = []
        self.failing = set()
        self.pools = ["rpool", "tank"]
        self.datasets = {}
        self.installed = True
        self.running = set()
        self.outputs = {}
        self.owners = {}
        self.extra_datasets = []
        self.files = {}
        if os_type != "freebsd":
            for entry in self.profile["containers"].get("config_files", []):
                self.files[entry["path"]] = "\n".join(entry["lines"]) + "\n"

    def get_profile_section(self, category):
        try:
            return self.profile[category]
        except KeyError:
            raise CommandTranslationError(category)

    def get_profile_value(self, category, key):
        try:
            return self.profile[category][key]
        except KeyError:
            raise CommandTranslationError(f"{category}.{key}")

    def path(self, name):
        return self.profile["containers"]["data_dirs"][name]

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((category, action, kw))
        key = (category, action)
        if key in self.failing:
            raise CommandExecutionError(f"{category}.{action} failed")
        if key == ("containers", "runtime_check"):
            if not self.installed:
                raise CommandExecutionError("docker: not found")
            return "Docker version 29", ""
        if key == ("zfs", "pool_list"):
            return "".join(f"{p}\t1\t1\t1\t1\tONLINE\n" for p in self.pools), ""
        if key == ("zfs", "dataset_get_property"):
            ds = self.datasets.get(kw["dataset"])
            if ds is None:
                raise CommandExecutionError("dataset does not exist")
            return f"{ds.get(kw['property'], '-')}\tlocal", ""
        if key == ("zfs", "dataset_create_with"):
            props = dict(w.split("=", 1) for w in kw["options"].split() if "=" in w)
            if "mountpoint" in props:
                self.datasets[kw["dataset"]] = {"mountpoint": props["mountpoint"], "mounted": "no",
                                               "canmount": props.get("canmount", "on")}
            else:
                mountpoint = self.tmp / "mnt" / kw["dataset"]
                mountpoint.mkdir(parents=True, exist_ok=True)
                self.datasets[kw["dataset"]] = {"mountpoint": str(mountpoint), "mounted": "yes", "canmount": "on"}
            return "", ""
        if key == ("zfs", "dataset_set"):
            self.datasets[kw["dataset"]][kw["property"]] = kw["value"]
            return "", ""
        if key == ("zfs", "dataset_destroy_recursive"):
            ds = self.datasets.pop(kw["dataset"])
            shutil.rmtree(ds["mountpoint"], ignore_errors=True)
            return "", ""
        if key == ("zfs", "mount_dataset"):
            ds = self.datasets[kw["dataset"]]
            Path(ds["mountpoint"]).mkdir(parents=True, exist_ok=True)
            ds["mounted"] = "yes"
            return "", ""
        if key == ("containers", "folder_prepare"):
            Path(kw["path"]).mkdir(parents=True, exist_ok=True)
            self.owners[kw["path"]] = (kw["uid"], kw["gid"])
            return "", ""
        if key == ("zfs", "dataset_list"):
            rows = [f"{n}\t1\t1\t1\t{d['mountpoint']}\tfilesystem\t{d['mounted']}\t-"
                    for n, d in self.datasets.items()]
            return "\n".join(rows + self.extra_datasets), ""
        if key == ("containers", "data_move"):
            Path(kw["source"]).rename(kw["target"])
            return "", ""
        if key == ("containers", "data_copy"):
            shutil.copytree(kw["source"], kw["target"], dirs_exist_ok=True)
            return "", ""
        if key in (("containers", "file_read"), ("proxy", "file_read")) and key not in self.outputs:
            return self.files.get(kw["file"], ""), ""
        if key == ("proxy", "file_remove"):
            self.files.pop(kw["file"], None)
        if key == ("services", "is_running"):
            return ("yes" if kw["service_name"] in self.running else ""), ""
        if key == ("services", "start"):
            self.running.add(kw["service_name"])
            return "", ""
        if key == ("services", "stop"):
            self.running.discard(kw["service_name"])
            return "", ""
        if key == ("containers", "info"):
            if "docker" not in self.running:
                raise CommandExecutionError("Cannot connect to the Docker daemon")
            return json.dumps({"data_root": "/var/lib/docker", "driver": "overlayfs",
                               "version": "29.0", "containers": 2, "images": 3}), ""
        return self.outputs.get(key, ("", ""))

    def execute_with_stdin(self, category, action, stdin_data, timeout=30, **kw):
        self.calls.append((category, action, kw))
        if (category, action) in self.failing:
            raise CommandExecutionError(f"Command failed\nCommand: x\nStderr: {category}.{action} refused")
        if (category, action) in (("containers", "compose_write"), ("containers", "env_write")):
            Path(kw["file"]).write_text(stdin_data)
        if (category, action) in (("containers", "file_write"), ("proxy", "site_write")):
            self.files[kw["file"]] = stdin_data
        self.stdin = stdin_data
        return "", ""

    def actions(self):
        return [f"{c}.{a}" for c, a, _ in self.calls]


class FakeConfig:
    def __init__(self, section=None, users=("sarah",), timezone="Europe/Rome"):
        self.section = section or {}
        self.others = {}
        self.users = [{"username": u} for u in users]
        self.timezone = timezone
        self.smb = []

    def to_dict(self):
        return {"users": self.users, "system": {"timezone": self.timezone}}

    def get_module_config(self, module_id):
        if module_id in self.others:
            return json.loads(json.dumps(self.others[module_id]))
        return json.loads(json.dumps(self.section))

    def set_module_config(self, module_id, section):
        self.section = section

    def get_smb_shares(self):
        return self.smb

    def add_smb_share(self, share):
        self.smb = [s for s in self.smb if s["name"] != share["name"]] + [share]

    def get_users(self):
        return self.users

    def get_user(self, name):
        return next((u for u in self.users if u["username"] == name), None)

    def add_user(self, user):
        self.users = [u for u in self.users if u["username"] != user["username"]] + [user]


@pytest.fixture
def engine(tmp_path):
    return FakeEngine(tmp=tmp_path)


@pytest.fixture(autouse=True)
def packages(monkeypatch):
    monkeypatch.setattr(runtime, "PackageManager", lambda: PackageManager(str(PROFILES / "packages")))
    monkeypatch.setattr(runtime, "START_WAIT", 0)
