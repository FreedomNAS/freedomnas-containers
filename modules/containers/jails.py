"""
Jails and OCI images on FreeBSD, through AppJail and buildah: the twin of
docker.py, with the same functions and the same shapes, so the API and the
page do not depend on the runtime underneath.

A container is a jail. `appjail jail list -eHIp` gives one tab-separated
line per jail with the keywords asked for; images come from
`buildah images --json`. Every name, image and option is checked before it
reaches a command. `oci run` takes options holding spaces (`fstab=src dst`,
`expose=7359 proto:udp`), so its words go to `xargs -0` on stdin, NUL
separated: no shell reads them and each stays one argument.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

CATEGORY = "containers"
ACTIONS = ("start", "stop", "restart")
RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")
LOG_LINES = 200
PULL_TIMEOUT = 3600
FIELDS = ("name", "status", "type", "version", "ports", "network_ip4",
          "container_image", "created", "boot", "is_container")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")
_PORT = re.compile(r"(\d{1,5}):(\d{1,5})(?:/(tcp|udp))?")
_VOLUME = re.compile(r"(/[^\s:]*):(/[^\s:]*)(?::(ro|rw))?")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_PREFIX = re.compile(r"^\[[\d:]+\] \[\s*\w+\s*\] (\[[^\]]+\] )?")
_EXPOSED = re.compile(r"(\d+)(?:\s*->\s*(\d+))?/(tcp|udp)")


class ContainerError(Exception):
    pass


def check_name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ContainerError(f"'{value}' is not a jail name (letters, digits, - and _)")
    return value


def check_image(value: str) -> str:
    if not isinstance(value, str) or not _IMAGE.fullmatch(value):
        raise ContainerError(f"'{value}' is not an image reference")
    return value


def short_ports(text: str) -> str:
    """'8099 -> 80/tcp,7359/udp' as '8099→80, 7359/udp', each mapping once."""
    seen: List[str] = []
    for host, inner, proto in _EXPOSED.findall(text or ""):
        label = f"{host}→{inner}" if inner and inner != host else host
        label += "/udp" if proto == "udp" else ""
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


def _rows(out: str) -> List[Dict[str, str]]:
    rows = []
    for line in out.splitlines():
        values = line.split("\t")
        if len(values) >= len(FIELDS) and values[0]:
            rows.append(dict(zip(FIELDS, values)))
    return rows


def containers(engine) -> List[Dict[str, Any]]:
    out, _ = engine.execute(CATEGORY, "ps")
    result = []
    for row in _rows(out):
        image = row["container_image"].strip() or f"FreeBSD {row['version']}"
        running = row["status"] == "UP"
        result.append({
            "id": row["name"],
            "name": row["name"],
            "image": image,
            "state": "running" if running else "exited",
            "status": ("Up" if running else "Down") + f" · {row['type']} jail",
            "ports": short_ports(row["ports"]),
            "published": row["ports"].strip(),
            "created": row["created"],
            "project": None,
            "address": row["network_ip4"].strip(),
        })
    return sorted(result, key=lambda c: (c["state"] != "running", c["name"]))


def images(engine) -> List[Dict[str, Any]]:
    out, _ = engine.execute(CATEGORY, "images")
    result = []
    for row in json.loads(out or "[]") or []:
        names = row.get("names") or []
        reference = names[0] if names else row.get("id")
        repository, _, tag = (names[0].rpartition(":") if names else ("<none>", "", "<none>"))
        if "/" in tag:
            repository, tag = names[0], "latest"
        result.append({
            "id": row.get("id"),
            "repository": repository,
            "tag": tag,
            "reference": reference,
            "size": row.get("size"),
            "created": row.get("createdat"),
            "containers": None,
        })
    return result


def act(engine, name: str, action: str) -> None:
    if action not in ACTIONS:
        raise ContainerError(f"Unknown action '{action}'")
    engine.execute(CATEGORY, action, timeout=300, container=check_name(name))


def remove(engine, name: str, force: bool = False) -> None:
    engine.execute(CATEGORY, "remove_force" if force else "remove", timeout=300, container=check_name(name))


def _logs(engine) -> List[Dict[str, str]]:
    out, _ = engine.execute(CATEGORY, "log_list")
    found = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0] == "jails":
            found.append(dict(zip(("type", "entity", "subtype", "log"), fields)))
    return found


def logs(engine, name: str, lines: int = LOG_LINES) -> str:
    """The newest log of the jail's process (an OCI container), else of its console."""
    lines = max(1, min(int(lines), 5000))
    check_name(name)
    own = [entry for entry in _logs(engine) if entry["entity"] == name]
    for subtype in ("container", "console"):
        candidates = sorted(entry["log"] for entry in own if entry["subtype"] == subtype)
        if candidates:
            out, err = engine.execute(CATEGORY, "log_read", timeout=60,
                                      path=f"jails/{name}/{subtype}/{candidates[-1]}", lines=str(lines))
            return "\n".join(part for part in (out, err) if part)
    return ""


def pull(engine, image: str) -> None:
    engine.execute(CATEGORY, "image_pull", timeout=PULL_TIMEOUT, image=check_image(image))


def remove_image(engine, image: str) -> None:
    engine.execute(CATEGORY, "image_remove", timeout=120, image=check_image(image))


def prune_images(engine) -> str:
    out, _ = engine.execute(CATEGORY, "image_prune", timeout=600)
    return out


def run_args(spec: Dict[str, Any]) -> List[str]:
    """
    The `appjail oci run` options for a container described as
    {name, ports, volumes, env, restart}; anything malformed is refused.
    A jail always needs its name. Volumes are folders of the system (a jail
    has no named volumes).
    """
    check_name(spec.get("name") or "")
    words: List[str] = []
    restart = spec.get("restart") or "unless-stopped"
    if restart not in RESTART_POLICIES:
        raise ContainerError(f"Unknown restart policy '{restart}'")
    for port in spec.get("ports") or []:
        match = _PORT.fullmatch(str(port))
        if not match:
            raise ContainerError(f"'{port}' is not a port mapping (host:jail, /udp for UDP)")
        host, inner, proto = match.groups()
        words += ["-o", f"expose={host}:{inner}" + (" proto:udp" if proto == "udp" else "")]
    for volume in spec.get("volumes") or []:
        match = _VOLUME.fullmatch(str(volume))
        if not match:
            raise ContainerError(f"'{volume}' is not a volume (/folder:/path[:ro])")
        source, target, mode = match.groups()
        words += ["-o", f"fstab={source} {target}" + (" nullfs ro" if mode == "ro" else "")]
    for env in spec.get("env") or []:
        if not _ENV.fullmatch(str(env)):
            raise ContainerError(f"'{env}' is not NAME=value (no spaces)")
        words += ["-e", str(env)]
    return words


def run(engine, spec: Dict[str, Any]) -> str:
    """Create and start a jail from an OCI image, on the jails' NAT network."""
    image = check_image(spec.get("image") or "")
    name = spec.get("name")
    words_after = run_args(spec)
    network = engine.get_profile_value(CATEGORY, "jail_network")["name"]
    template = engine.get_profile_value(CATEGORY, "oci_template")
    words = ["-o", f"virtualnet={network}:<random> default", "-o", "nat",
             "-o", f"template={template}"] + words_after + [image, name]
    out, _ = engine.execute_with_stdin(CATEGORY, "run", "\0".join(words), timeout=PULL_TIMEOUT)
    if (spec.get("restart") or "unless-stopped") == "no":
        engine.execute(CATEGORY, "boot_off", container=name)
    return name


def error_text(exc: Exception) -> str:
    """
    What went wrong, without AppJail's debug log: its `[ error ]` lines,
    each with the line before it, which usually names the cause.
    """
    text = str(exc)
    marker = "Stderr:"
    if isinstance(exc, (CommandExecutionError, CommandTranslationError)) and marker in text:
        text = text.split(marker, 1)[1].strip() or text
    lines = text.splitlines()
    picked: List[str] = []
    for number, line in enumerate(lines):
        if "[ error ]" in line:
            before = lines[number - 1] if number else ""
            for item in (before, line):
                item = _PREFIX.sub("", item).strip()
                if item and item not in picked:
                    picked.append(item)
    if not picked:
        picked = [_PREFIX.sub("", line).strip() for line in lines if line.strip()]
    return "\n".join(picked)
