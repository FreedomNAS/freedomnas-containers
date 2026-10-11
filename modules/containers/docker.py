"""
Containers and images, through the profile's `containers` commands.

Every name, id and image reference is checked before it reaches a command;
`run` takes its options as words (`run_args` is a multi-argument), so a
value cannot hold whitespace.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]

CATEGORY = "containers"
ACTIONS = ("start", "stop", "restart")
RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")
LOG_LINES = 200
PULL_TIMEOUT = 3600

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")
_PORT = re.compile(r"(?:(?:\d{1,3}\.){3}\d{1,3}:)?\d{1,5}(?:-\d{1,5})?:\d{1,5}(?:-\d{1,5})?(?:/(?:tcp|udp))?")
_VOLUME = re.compile(r"[^\s:]+:/[^\s:]*(?::(?:ro|rw))?")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_PUBLISHED = re.compile(r"(?:[\d.]+|\[[0-9a-f:]*\]):(\d+)->(\d+)/(tcp|udp)")


class ContainerError(Exception):
    pass


def check_name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ContainerError(f"'{value}' is not a container name or id")
    return value


def check_image(value: str) -> str:
    if not isinstance(value, str) or not _IMAGE.fullmatch(value):
        raise ContainerError(f"'{value}' is not an image reference")
    return value


def short_ports(text: str) -> str:
    """'0.0.0.0:8090->80/tcp, [::]:8090->80/tcp' as '8090→80', each mapping once."""
    seen: List[str] = []
    for host, inner, proto in _PUBLISHED.findall(text or ""):
        label = f"{host}→{inner}" + ("/udp" if proto == "udp" else "")
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


def _json_lines(out: str) -> List[Dict[str, Any]]:
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _labels(text: str) -> Dict[str, str]:
    labels = {}
    for item in (text or "").split(","):
        key, sep, value = item.partition("=")
        if sep:
            labels[key] = value
    return labels


def containers(engine) -> List[Dict[str, Any]]:
    out, _ = engine.execute(CATEGORY, "ps")
    result = []
    for row in _json_lines(out):
        labels = _labels(row.get("Labels", ""))
        result.append({
            "id": row.get("ID"),
            "name": (row.get("Names") or "").split(",")[0],
            "image": row.get("Image"),
            "state": row.get("State"),
            "status": row.get("Status"),
            "ports": short_ports(row.get("Ports") or ""),
            "published": row.get("Ports") or "",
            "created": row.get("CreatedAt"),
            "project": labels.get("com.docker.compose.project"),
        })
    return sorted(result, key=lambda c: (c["state"] != "running", c["name"]))


def images(engine) -> List[Dict[str, Any]]:
    out, _ = engine.execute(CATEGORY, "images")
    result = []
    for row in _json_lines(out):
        repo, tag = row.get("Repository"), row.get("Tag")
        result.append({
            "id": row.get("ID"),
            "repository": repo,
            "tag": tag,
            "reference": f"{repo}:{tag}" if repo and repo != "<none>" and tag and tag != "<none>" else row.get("ID"),
            "size": row.get("Size"),
            "created": row.get("CreatedSince"),
            "containers": row.get("Containers"),
        })
    return result


def act(engine, name: str, action: str) -> None:
    if action not in ACTIONS:
        raise ContainerError(f"Unknown action '{action}'")
    engine.execute(CATEGORY, action, timeout=120, container=check_name(name))


def remove(engine, name: str, force: bool = False) -> None:
    engine.execute(CATEGORY, "remove_force" if force else "remove", timeout=120, container=check_name(name))


def logs(engine, name: str, lines: int = LOG_LINES) -> str:
    lines = max(1, min(int(lines), 5000))
    out, err = engine.execute(CATEGORY, "logs", timeout=60, container=check_name(name), lines=str(lines))
    return "\n".join(part for part in (out, err) if part)


def pull(engine, image: str) -> None:
    engine.execute(CATEGORY, "image_pull", timeout=PULL_TIMEOUT, image=check_image(image))


def remove_image(engine, image: str) -> None:
    engine.execute(CATEGORY, "image_remove", timeout=120, image=check_image(image))


def prune_images(engine) -> str:
    out, _ = engine.execute(CATEGORY, "image_prune", timeout=600)
    return out


def run_args(spec: Dict[str, Any]) -> List[str]:
    """
    The `docker run` options for a container described as
    {name, ports, volumes, env, restart}; anything malformed is refused.
    """
    words: List[str] = []
    name = spec.get("name")
    if name:
        words += ["--name", check_name(name)]
    restart = spec.get("restart") or "unless-stopped"
    if restart not in RESTART_POLICIES:
        raise ContainerError(f"Unknown restart policy '{restart}'")
    words += ["--restart", restart]
    for port in spec.get("ports") or []:
        if not _PORT.fullmatch(str(port)):
            raise ContainerError(f"'{port}' is not a port mapping (host:container)")
        words += ["-p", str(port)]
    for volume in spec.get("volumes") or []:
        if not _VOLUME.fullmatch(str(volume)):
            raise ContainerError(f"'{volume}' is not a volume (source:/path[:ro])")
        words += ["-v", str(volume)]
    for env in spec.get("env") or []:
        if not _ENV.fullmatch(str(env)):
            raise ContainerError(f"'{env}' is not NAME=value (no spaces)")
        words += ["-e", str(env)]
    return words


def run(engine, spec: Dict[str, Any]) -> str:
    image = check_image(spec.get("image") or "")
    out, _ = engine.execute(CATEGORY, "run", timeout=PULL_TIMEOUT,
                            run_args=" ".join(run_args(spec)), image=image)
    return out.strip()


def error_text(exc: Exception) -> str:
    text = str(exc)
    marker = "Stderr:"
    if isinstance(exc, (CommandExecutionError, CommandTranslationError)) and marker in text:
        text = text.split(marker, 1)[1].strip() or text
    return text
