"""
AppJail as the container runtime (FreeBSD, profile `backend: appjail`).

runtime.py mounts the data directories from the pool the same way as for
Docker; this file writes what makes AppJail and buildah use them, and
what jails need on the host:

  * appjail.conf: AppJail's ZFS root is the `appjail` dataset, the
    external interface is the one of the default route (fnas_lanN,
    fnas_bridgeN… on FreedomNAS), the release is the system's own.
  * storage.conf: buildah's zfs driver keeps image layers under the `oci`
    dataset. Left to its default, every layer becomes a dataset under the
    root dataset, which grows the system and goes back with its rollback.
  * pf: AppJail's NAT and expose anchors, in a block of /etc/pf.conf
    between `# freedomnas appjail begin` and `# freedomnas appjail end`.
    Lines outside the block are never touched; a file with no `pass` rule
    of its own gets one inside the block, since enabling pf would
    otherwise block everything.
  * gateway_enable and IP forwarding, pf enabled, the jails' NAT network
    (`jail_network` in the profile), the release fetched once.

Every step checks first and only writes what differs, so reconcile() can
run it at every boot.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Containers")

CATEGORY = "containers"
PF_BEGIN = "# freedomnas appjail begin"
PF_END = "# freedomnas appjail end"
PF_ANCHORS = [
    'nat-anchor "appjail-nat/jail/*"',
    'nat-anchor "appjail-nat/network/*"',
    'rdr-anchor "appjail-rdr/*"',
]
SYSRC = ["gateway_enable=YES", "pf_enable=YES"]


def _value(engine, key: str):
    return engine.get_profile_value(CATEGORY, key)


def _run(engine, action: str, check: bool = True, **kw) -> str:
    out, _ = engine.execute(CATEGORY, action, check=check, **kw)
    return out


def default_interface(engine) -> Optional[str]:
    for line in _run(engine, "default_route", check=False).splitlines():
        key, _, value = line.strip().partition(":")
        if key == "interface" and value.strip():
            return value.strip()
    return None


def release(engine) -> Dict[str, str]:
    version = _run(engine, "system_version").strip().split("-p")[0]
    return {"version": version, "arch": _run(engine, "system_arch").strip()}


def appjail_conf(pool: str, dataset: str, interface: str, rel: Dict[str, str]) -> str:
    root = dataset.split("/", 1)[1]
    return "\n".join([
        "# Written by FreedomNAS (containers module): AppJail's ZFS root on the pool's dataset.",
        f"EXT_IF={interface}",
        f"ON_IF={interface}",
        f"FREEBSD_VERSION={rel['version']}",
        f"FREEBSD_ARCH={rel['arch']}",
        f"IMAGE_ARCH={rel['arch']}",
        "SHORTEN_DOMAIN_NAMES=1",
        "ENABLE_ZFS=1",
        f"ZPOOL={pool}",
        f"ZROOTFS={root}",
        'TAR_XZ_ARGS="--xz --options xz:threads=0"',
        'TAR_ZSTD_ARGS="--zstd --options zstd:threads=0"',
        "",
    ])


def storage_conf(dataset: str, graphroot: str) -> str:
    return "\n".join([
        "# Written by FreedomNAS (containers module): image layers on the pool's dataset.",
        "[storage]",
        'driver = "zfs"',
        'runroot = "/var/run/containers/storage"',
        f'graphroot = "{graphroot}/storage"',
        "",
        "[storage.options.zfs]",
        f'fsname = "{dataset}"',
        "",
    ])


def pf_conf(current: str) -> str:
    """`current` with the FreedomNAS block in place, before any other rule."""
    lines = current.splitlines()
    outside: List[str] = []
    inside = False
    for line in lines:
        if line.strip() == PF_BEGIN:
            inside = True
            continue
        if line.strip() == PF_END:
            inside = False
            continue
        if not inside:
            outside.append(line)
    block = [PF_BEGIN] + PF_ANCHORS
    if not any(line.split()[:1] == ["pass"] for line in outside):
        block.append("pass all")
    block.append(PF_END)
    return "\n".join(block + outside).rstrip("\n") + "\n"


def _read(engine, path: str) -> str:
    return _run(engine, "file_read", check=False, file=path)


def _write(engine, path: str, text: str) -> bool:
    if _read(engine, path).rstrip("\n") == text.rstrip("\n"):
        return False
    engine.execute_with_stdin(CATEGORY, "file_write", text, file=path)
    return True


def networks(engine) -> Dict[str, str]:
    found = {}
    for line in _run(engine, "network_list", check=False).splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0]:
            found[fields[0]] = fields[1]
    return found


def configure(engine, pool: str, datasets: Dict[str, str], dirs: Dict[str, str]) -> List[str]:
    """
    Point AppJail and buildah at the pool's datasets and prepare the host.
    Returns what was done. AppJail must not be running jails.
    """
    done: List[str] = []
    interface = default_interface(engine)
    if not interface:
        raise CommandTranslationError("No default route: AppJail needs the interface jails reach the network through")
    rel = release(engine)
    if _write(engine, _value(engine, "appjail_conf"),
              appjail_conf(pool, datasets["appjail"], interface, rel)):
        done.append("appjail.conf")
    if _write(engine, _value(engine, "storage_conf"), storage_conf(datasets["oci"], dirs["oci"])):
        done.append("storage.conf")
    pf_path = _value(engine, "pf_conf")
    if _write(engine, pf_path, pf_conf(_read(engine, pf_path))):
        done.append("pf.conf")
    for setting in SYSRC:
        engine.execute(CATEGORY, "sysrc_set", setting=setting)
    engine.execute(CATEGORY, "sysctl_set", setting="net.inet.ip.forwarding=1")
    engine.execute(CATEGORY, "kmod_load", module="pf")
    engine.execute("services", "start", service_name="pf")
    engine.execute(CATEGORY, "pf_load")
    net = _value(engine, "jail_network")
    if net["name"] not in networks(engine):
        engine.execute(CATEGORY, "network_add", name=net["name"], network=net["network"])
        done.append(f"network {net['name']}")
    try:
        engine.execute(CATEGORY, "release_check", **rel)
    except CommandExecutionError:
        engine.execute(CATEGORY, "release_fetch", timeout=3600)
        done.append(f"release {rel['version']}")
    return done
