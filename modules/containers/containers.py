"""
Containers and images through the profile's `containers` commands, whatever
the runtime underneath (Docker containers, AppJail jails): the profile says
how each command is called and how its output reads, this file knows only
the module's own shapes.

Records (see records.py for `<action>_output`):
    ps        id, name, image, state ("running" when it runs), status,
              published (the runtime's text of its published ports), created,
              address
    images    id, repository, tag, size, created, containers

`ports_pattern` reads `published` (groups host, inner, proto),
`name_pattern` is what a container name may look like on this runtime.

`run` reads its words NUL-separated on stdin, so a word may hold spaces;
`run_words` turns a request {name, image, ports, volumes, env, restart}
into them:
    start, end    words around the options ({name}, {image} allowed)
    name          words for a name, when one is given
    restart       restart policy -> words
    port          "tcp"/"udp" -> words with {bind}, {host}, {inner}
    volume        "rw"/"ro" -> words with {source}, {target}
    env           words with {env}
    name_required the runtime needs a name
`run_after` maps a restart policy to an action run on the new container.
Every value is checked before it reaches a command.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore[no-redef]


def _load(stem: str):
    name = f"freedomnas_containers_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{stem}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


records = _load("records")

CATEGORY = "containers"
ACTIONS = ("start", "stop", "restart")
RESTART_POLICIES = ("no", "always", "unless-stopped", "on-failure")
LOG_LINES = 200
PULL_TIMEOUT = 3600
ACTION_TIMEOUT = 300

_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")
_PORT = re.compile(r"(?:((?:\d{1,3}\.){3}\d{1,3}):)?(\d{1,5}(?:-\d{1,5})?):(\d{1,5}(?:-\d{1,5})?)(?:/(tcp|udp))?")
_VOLUME = re.compile(r"([^\s:]+):(/[^\s:]*)(?::(ro|rw))?")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")


class ContainerError(Exception):
    pass


def _value(engine, key: str, default: Any = None) -> Any:
    try:
        return engine.get_profile_value(CATEGORY, key)
    except CommandTranslationError:
        return default


def check_name(engine, value: str) -> str:
    pattern = _value(engine, "name_pattern", _NAME)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ContainerError(f"'{value}' is not a {_value(engine, 'name_hint', 'container name or id')}")
    return value


def check_image(value: str) -> str:
    if not isinstance(value, str) or not _IMAGE.fullmatch(value):
        raise ContainerError(f"'{value}' is not an image reference")
    return value


def port_pairs(engine, text: str) -> List[tuple]:
    """(host, inner, proto) of each port in the runtime's `published` text, each once."""
    pattern = _value(engine, "ports_pattern")
    if not pattern or not text:
        return []
    seen: List[tuple] = []
    for match in re.finditer(pattern, text):
        pair = (match.group("host"), match.group("inner") or match.group("host"), match.group("proto") or "tcp")
        if pair not in seen:
            seen.append(pair)
    return seen


def short_ports(engine, text: str) -> str:
    """The published ports as '8090→80, 7359/udp'."""
    labels = []
    for host, inner, proto in port_pairs(engine, text):
        label = host if inner == host else f"{host}→{inner}"
        labels.append(label + ("/udp" if proto == "udp" else ""))
    return ", ".join(labels)


def containers(engine) -> List[Dict[str, Any]]:
    result = []
    for row in records.run(engine, CATEGORY, "ps"):
        published = row.get("published") or ""
        result.append({
            "id": row.get("id") or row.get("name"),
            "name": row.get("name"),
            "image": row.get("image"),
            "state": row.get("state"),
            "status": row.get("status"),
            "ports": short_ports(engine, published),
            "host_ports": sorted({int(h) for h, _, _ in port_pairs(engine, published) if h.isdigit()}),
            "published": published,
            "created": row.get("created"),
            "address": row.get("address") or "",
        })
    return sorted(result, key=lambda c: (c["state"] != "running", c["name"] or ""))


def images(engine) -> List[Dict[str, Any]]:
    result = []
    for row in records.run(engine, CATEGORY, "images"):
        repository, tag = row.get("repository") or "", row.get("tag") or ""
        named = repository and tag and "<none>" not in (repository, tag)
        result.append({
            "id": row.get("id"),
            "repository": repository or "<none>",
            "tag": tag or "<none>",
            "reference": f"{repository}:{tag}" if named else row.get("id"),
            "size": row.get("size"),
            "created": row.get("created"),
            "containers": row.get("containers") or None,
        })
    return result


def act(engine, name: str, action: str) -> None:
    if action not in ACTIONS:
        raise ContainerError(f"Unknown action '{action}'")
    engine.execute(CATEGORY, action, timeout=ACTION_TIMEOUT, container=check_name(engine, name))


def remove(engine, name: str, force: bool = False) -> None:
    engine.execute(CATEGORY, "remove_force" if force else "remove", timeout=ACTION_TIMEOUT,
                   container=check_name(engine, name))


def logs(engine, name: str, lines: int = LOG_LINES) -> str:
    lines = max(1, min(int(lines), 5000))
    out, err = engine.execute(CATEGORY, "logs", timeout=60, container=check_name(engine, name), lines=str(lines))
    return "\n".join(part for part in (out, err) if part)


def pull(engine, image: str) -> None:
    engine.execute(CATEGORY, "image_pull", timeout=PULL_TIMEOUT, image=check_image(image))


def remove_image(engine, image: str) -> None:
    engine.execute(CATEGORY, "image_remove", timeout=120, image=check_image(image))


def prune_images(engine) -> str:
    out, _ = engine.execute(CATEGORY, "image_prune", timeout=600)
    return out


def _words(templates: Any, values: Dict[str, Any]) -> List[str]:
    return [records.render(t, values) for t in templates or []]


def _variant(words: Dict[str, Any], key: str, variant: str, what: str) -> List[str]:
    choices = words.get(key) or {}
    if variant not in choices:
        raise ContainerError(f"{what} is not supported by this runtime")
    return choices[variant]


def run_words(engine, spec: Dict[str, Any]) -> List[str]:
    """
    The words of `run` for a container described as
    {name, image, ports, volumes, env, restart}; anything malformed is refused.
    """
    words = _value(engine, "run_words") or {}
    image = check_image(spec.get("image") or "")
    name = spec.get("name") or ""
    if name:
        check_name(engine, name)
    elif words.get("name_required"):
        raise ContainerError("A name is required")
    restart = spec.get("restart") or "unless-stopped"
    if restart not in RESTART_POLICIES:
        raise ContainerError(f"Unknown restart policy '{restart}'")
    values = {"name": name, "image": image, "restart": restart}

    result = _words(words.get("start"), values)
    if name:
        result += _words(words.get("name"), values)
    result += _words((words.get("restart") or {}).get(restart, []), values)
    for port in spec.get("ports") or []:
        match = _PORT.fullmatch(str(port))
        if not match:
            raise ContainerError(f"'{port}' is not a port mapping (host:inner, /udp for UDP)")
        bind, host, inner, proto = match.groups()
        template = _variant(words, "port", proto or "tcp", f"'{port}'")
        if bind and not any("{bind}" in t for t in template):
            raise ContainerError(f"'{port}': binding a port to one address is not supported by this runtime")
        result += _words(template, {"bind": f"{bind}:" if bind else "", "host": host, "inner": inner})
    for volume in spec.get("volumes") or []:
        match = _VOLUME.fullmatch(str(volume))
        if not match:
            raise ContainerError(f"'{volume}' is not a volume (source:/path[:ro])")
        source, target, mode = match.groups()
        if not source.startswith("/") and not words.get("named_volumes"):
            raise ContainerError(f"'{volume}': only folders (/path) can be mounted by this runtime")
        result += _words(_variant(words, "volume", mode or "rw", f"'{volume}'"),
                         {"source": source, "target": target})
    for env in spec.get("env") or []:
        if not _ENV.fullmatch(str(env)):
            raise ContainerError(f"'{env}' is not NAME=value (no spaces)")
        result += _words(words.get("env"), {"env": env})
    result += _words(words.get("end"), values)
    for word in result:
        if "\0" in word or "\n" in word:
            raise ContainerError("A value holds a control character")
    return result


def run(engine, spec: Dict[str, Any]) -> str:
    words = run_words(engine, spec)
    out, _ = engine.execute_with_stdin(CATEGORY, "run", "\0".join(words), timeout=PULL_TIMEOUT)
    name = spec.get("name") or (out.strip().splitlines()[-1] if out.strip() else "")
    after = (_value(engine, "run_after") or {}).get(spec.get("restart") or "unless-stopped")
    if after and name:
        engine.execute(CATEGORY, after, container=name)
    return name


def error_text(engine, exc: Exception) -> str:
    """
    What went wrong: the command's stderr, cut down by the profile's
    `error_filter` {keep, context, strip} to the lines that name the cause.
    """
    text = str(exc)
    marker = "Stderr:"
    if isinstance(exc, (CommandExecutionError, CommandTranslationError)) and marker in text:
        text = text.split(marker, 1)[1].strip() or text
    rule: Optional[Dict[str, Any]] = _value(engine, "error_filter") if engine is not None else None
    if not rule:
        return text
    strip = re.compile(rule.get("strip") or "$^")
    keep = re.compile(rule["keep"])
    context = int(rule.get("context", 0))
    lines = text.splitlines()
    picked: List[str] = []
    for number, line in enumerate(lines):
        if keep.search(line):
            for item in lines[max(0, number - context):number + 1]:
                item = strip.sub("", item).strip()
                if item and item not in picked:
                    picked.append(item)
    if not picked:
        picked = [strip.sub("", line).strip() for line in lines if line.strip()]
    return "\n".join(picked)
