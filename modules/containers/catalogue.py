"""
The app catalogue: ready-made apps installed as ordinary apps (apps.py).

Each entry is a directory under catalogue/ with `app.json` and one recipe
folder per runtime, named by the profile's `catalogue_recipe` (`linux`:
compose.yaml; `freebsd`: appjail-director.yml, its Makejails and what they
copy). The recipe reads the settings as ${VARIABLES} (Director: !ENV), and
`APP` is the app's name. An entry without a recipe for this edition is
listed as not available. Installing writes the recipe to the app's
dataset, the settings to `.env` beside it (0600: it holds passwords), and
creates the app's own folders owned by the account the app runs as.

Setting types in app.json:
    port      a free TCP port on the NAS
    folder    ./name inside the app's dataset, or a directory of a pool;
              offered at every UI level. "owner": "uid:gid" (or
              {recipe: "uid:gid"}) gives the folder to that id instead of
              the app's user; "empty": a
              directory of a pool must be empty or not exist yet (its
              parent on a pool), and is created and given to its owner
    user      a FreedomNAS account; writes its uid and gid to the keys
              named by "uid" and "gid"
    timezone  defaults to the system's
    hosts     names and addresses the app answers to; defaults to the NAS's
    address   one IPv4 address of the NAS; defaults to its current one
    text      a short plain value
    choice    one of "choices"
    password  generated unless given; shown once after the install when
              "show" is set
    secret    always generated, never shown
`fixed` holds values the user never changes; `prepare` lists folders
created before the first start ("./name", or {"path", "owner"} with the
owner per recipe); `ports` lists ports used without a
setting (host networking), `address_ports` ports published on one address
setting only, which a service listening on loopback does not block.

A reinstall on a kept dataset keeps the passwords and secrets of its old
.env: the databases on the dataset were made with them.

An app records the recipe it was installed with. When its pool comes back
on an edition with another recipe, `adopt` writes the new one beside the
data if app.json lists both under "portable" (the same files work with
either runtime); otherwise the app stays down with a message.
"""

from __future__ import annotations

import importlib.util
import json
import pwd
import re
import secrets
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set



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
ROOT = Path(__file__).with_name("catalogue")
ENV_FILE = ".env"
SYSTEM_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run",
               "/sbin", "/sys", "/usr", "/var")

_RELATIVE = re.compile(r"\./[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_ABSOLUTE = re.compile(r"/[A-Za-z0-9._/-]+")
_TEXT = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_PASSWORD = re.compile(r"[A-Za-z0-9!#%&()*+,./:;<=>?@^_{|}~\[\]-]{8,128}")
_HOSTS = re.compile(r"[A-Za-z0-9.:-]+(?: [A-Za-z0-9.:-]+)*")
_TZ = re.compile(r"[A-Za-z0-9_+/-]{1,64}")
_IPV4 = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)")
_LISTEN = re.compile(r":(\d+)$")


class CatalogueError(Exception):
    pass


def entries(root: Path = ROOT) -> List[Dict[str, Any]]:
    result = []
    for path in sorted(root.glob("*/app.json")):
        entry = json.loads(path.read_text())
        entry["_dir"] = path.parent
        result.append(entry)
    return result


def entry(entry_id: str, root: Path = ROOT) -> Dict[str, Any]:
    for item in entries(root):
        if item["id"] == entry_id:
            return item
    raise CatalogueError(f"No app '{entry_id}' in the catalogue")


def recipe_name(engine) -> str:
    try:
        return engine.get_profile_value(CATEGORY, "catalogue_recipe") or ""
    except Exception:
        return ""


def recipe(engine, item: Dict[str, Any], app_file: str = "") -> Optional[Path]:
    """The entry's recipe folder for this edition, None when it has none."""
    name = recipe_name(engine)
    if not app_file:
        try:
            app_file = engine.get_profile_value(CATEGORY, "app_file") or "compose.yaml"
        except Exception:
            app_file = "compose.yaml"
    folder = item["_dir"] / name if name else None
    return folder if folder and (folder / app_file).is_file() else None


def summary(item: Dict[str, Any], available: bool = True) -> Dict[str, Any]:
    return {key: item.get(key) for key in ("id", "name", "description", "website", "note")} | {
        "settings": [s for s in item.get("settings", []) if s.get("type") != "secret"],
        "available": available}


def _loopback(address: str) -> bool:
    return address.startswith(("127.", "[::1]", "::1")) or "%lo" in address


def used_ports(rt, engine, public_only: bool = False, config=None) -> Set[int]:
    """
    Ports something on the NAS listens on (TCP or UDP), the ports
    containers publish, stopped ones included, and the ports of catalogue
    apps that are down. public_only leaves out what listens on loopback
    only, for a port published on one address.
    """
    ports: Set[int] = set()
    if config is not None:
        for app in (config.get_module_config("containers").get("apps") or {}).values():
            ports.update(int(p) for p in app.get("ports") or ([app["web_port"]] if app.get("web_port") else []))
    try:
        for row in records.run(engine, CATEGORY, "listening"):
            address = row.get("address") or ""
            match = _LISTEN.search(address)
            if match and not (public_only and _loopback(address)):
                ports.add(int(match.group(1)))
    except Exception:
        pass
    try:
        for container in rt.containers(engine):
            ports.update(container.get("host_ports") or [])
    except Exception:
        pass
    return ports


def _local_address() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _accounts(config) -> List[str]:
    return [u.get("username") for u in (config.to_dict().get("users") or []) if u.get("username")]


def defaults(item: Dict[str, Any], config, username: str, taken: Set[int]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    timezone = (config.to_dict().get("system") or {}).get("timezone") or "UTC"
    for setting in item.get("settings", []):
        kind, key = setting["type"], setting["key"]
        if kind == "port":
            port = int(setting.get("default", 8000))
            while port in taken:
                port += 1
            values[key] = str(port)
        elif kind == "timezone":
            values[key] = timezone
        elif kind == "user":
            accounts = _accounts(config)
            values[key] = username if username in accounts else (accounts[0] if accounts else username)
        elif kind == "hosts":
            values[key] = " ".join(h for h in (socket.gethostname(), _local_address()) if h)
        elif kind == "address":
            values[key] = _local_address() or ""
        elif kind == "secret":
            continue
        elif kind == "password":
            values[key] = secrets.token_urlsafe(18)
        elif kind == "choice":
            values[key] = setting.get("default") or setting["choices"][0]
        else:
            values[key] = str(setting.get("default", ""))
    return values


def _pool_datasets(engine) -> List[tuple]:
    out, _ = engine.execute("zfs", "dataset_list")
    datasets = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 5 or "/ROOT" in fields[0] or fields[5:6] == ["volume"]:
            continue
        mountpoint = fields[4]
        if mountpoint.startswith("/") and mountpoint != "/" and not mountpoint.startswith(SYSTEM_DIRS):
            datasets.append((fields[0], mountpoint.rstrip("/")))
    return datasets


def _pool_mountpoints(engine) -> List[str]:
    return [mountpoint for _, mountpoint in _pool_datasets(engine)]


def pool_folders(engine) -> List[str]:
    """The pool directories a folder setting offers, without the apps' and the runtime's own."""
    return sorted({mountpoint for name, mountpoint in _pool_datasets(engine)
                   if name.split("/")[1:2] not in (["apps"], ["containers"])})


def _check_folder(engine, value: str, setting: Dict[str, Any]) -> None:
    if _RELATIVE.fullmatch(value) and ".." not in value.split("/"):
        return
    value = value.rstrip("/") or value
    if not _ABSOLUTE.fullmatch(value) or ".." in value.split("/"):
        raise CatalogueError(f"'{value}' is not a folder: ./name or a directory of a pool")
    path = Path(value)
    if setting.get("empty"):
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise CatalogueError(f"{value} is not empty: choose an empty or a new folder")
        if not path.exists() and not path.parent.is_dir():
            raise CatalogueError(f"{path.parent} is not an existing directory")
    elif not path.is_dir():
        raise CatalogueError(f"{value} is not an existing directory")
    if not any(value == m or value.startswith(m + "/") for m in _pool_mountpoints(engine)):
        raise CatalogueError(f"{value} is not on a pool")


def resolve(item: Dict[str, Any], values: Dict[str, str], engine, config, taken: Set[int],
            public: Optional[Set[int]] = None) -> Dict[str, str]:
    """
    The .env of an install: every setting checked, users turned into ids,
    fixed values added. Raises CatalogueError on the first bad value.
    """
    env: Dict[str, str] = {}
    for setting in item.get("settings", []):
        kind, key = setting["type"], setting["key"]
        label = setting.get("label", key)
        value = str(values.get(key, "")).strip()
        if kind == "secret":
            value = value or secrets.token_urlsafe(18)
        if not value:
            raise CatalogueError(f"{label} is required")
        if kind == "port":
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                raise CatalogueError(f"{label}: '{value}' is not a port")
            if int(value) in taken:
                raise CatalogueError(f"{label}: port {value} is already in use")
        elif kind == "folder":
            _check_folder(engine, value, setting)
            value = value if value.startswith("./") else value.rstrip("/") or value
        elif kind == "user":
            if value not in _accounts(config):
                raise CatalogueError(f"{label}: '{value}' is not a FreedomNAS account")
            try:
                account = pwd.getpwnam(value)
            except KeyError:
                raise CatalogueError(f"{label}: '{value}' does not exist on this system")
            env[setting["uid"]] = str(account.pw_uid)
            env[setting["gid"]] = str(account.pw_gid)
            continue
        elif kind == "timezone" and not _TZ.fullmatch(value):
            raise CatalogueError(f"{label}: '{value}' is not a time zone")
        elif kind == "address" and not _IPV4.fullmatch(value):
            raise CatalogueError(f"{label}: '{value}' is not an IPv4 address")
        elif kind == "hosts" and not _HOSTS.fullmatch(value):
            raise CatalogueError(f"{label}: names and addresses separated by spaces")
        elif kind == "choice" and value not in setting["choices"]:
            raise CatalogueError(f"{label}: one of {', '.join(setting['choices'])}")
        elif kind in ("password", "secret") and not _PASSWORD.fullmatch(value):
            raise CatalogueError(f"{label}: 8 to 128 characters, no spaces or quotes")
        elif kind == "text" and not _TEXT.fullmatch(value):
            raise CatalogueError(f"{label}: letters, digits, '.', '_', '@' and '-'")
        env[key] = value
    for port in item.get("ports", []):
        if port in taken:
            raise CatalogueError(f"{item['name']} needs port {port}, which is already in use")
    for port in item.get("address_ports", []):
        if port in (taken if public is None else public):
            raise CatalogueError(f"{item['name']} needs port {port} on the NAS's address, which is already in use")
    env.update(item.get("fixed", {}))
    return env


def render_env(env: Dict[str, str]) -> str:
    return "".join(f"{key}='{value}'\n" for key, value in env.items())


def parse_env(text: str) -> Dict[str, str]:
    env = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            env[key.strip()] = value.strip().strip("'\"")
    return env


def _kept_secrets(item: Dict[str, Any], env_path: Path) -> Dict[str, str]:
    if not env_path.exists():
        return {}
    old = parse_env(env_path.read_text())
    return {s["key"]: old[s["key"]] for s in item.get("settings", [])
            if s["type"] in ("password", "secret") and s["key"] in old}


def _owner_for(owner: Any, recipe_name: str) -> Optional[tuple]:
    if isinstance(owner, dict):
        owner = owner.get(recipe_name)
    return tuple(owner.split(":")) if owner else None


def _folders(item: Dict[str, Any], env: Dict[str, str], recipe_name: str = "") -> List[tuple]:
    """Each folder to create before the first start, with its owner."""
    uid, gid = _owner(item, env)
    folders = []
    for entry in item.get("prepare", []):
        path, owner = (entry, None) if isinstance(entry, str) else (entry["path"], entry.get("owner"))
        folders.append((path, *(_owner_for(owner, recipe_name) or (uid, gid))))
    for setting in item.get("settings", []):
        value = env.get(setting["key"], "")
        if setting["type"] != "folder" or any(value == f[0] for f in folders):
            continue
        if value.startswith("./") or setting.get("empty"):
            folders.append((value, *(_owner_for(setting.get("owner"), recipe_name) or (uid, gid))))
    return folders


def _owner(item: Dict[str, Any], env: Dict[str, str]) -> tuple:
    for setting in item.get("settings", []):
        if setting["type"] == "user":
            return env[setting["uid"]], env[setting["gid"]]
    return "0", "0"


def app_ports(item: Dict[str, Any], env: Dict[str, str]) -> List[int]:
    ports = [int(env[s["key"]]) for s in item.get("settings", []) if s["type"] == "port"]
    return sorted(set(ports + list(item.get("ports", [])) + list(item.get("address_ports", []))))


def web_port(item: Dict[str, Any], env: Dict[str, str]) -> Optional[str]:
    web = item.get("web") or {}
    if web.get("fixed"):
        return str(web.get("port"))
    return env.get(web.get("port", ""))


def _write_recipe(engine, folder: Path, base: Path) -> None:
    for path in sorted(folder.iterdir()):
        if path.is_file():
            engine.execute_with_stdin(CATEGORY, "compose_write", path.read_text(), file=str(base / path.name))


def adopt(engine, name: str, app: Dict[str, Any], file: str) -> Optional[str]:
    """
    An app from the catalogue whose pool comes from another edition: when
    the entry lists both recipes under "portable", write this edition's
    recipe beside its data and .env, give the app's own folders (./name,
    never a folder of the pool it only mounts) to this edition's owners,
    and answer None; otherwise answer why it cannot start here.
    """
    if not app.get("catalogue"):
        return f"{file} is missing"
    try:
        item = entry(app["catalogue"])
    except CatalogueError as exc:
        return str(exc)
    here, was = recipe_name(engine), app.get("recipe") or "linux"
    if here == was:
        return f"{file} is missing"
    base = Path(file).parent
    folder = recipe(engine, item, base.joinpath(file).name)
    if folder is None:
        return f"{item['name']} is not available on this edition yet; its dataset is kept"
    portable = item.get("portable") or []
    if here not in portable or was not in portable:
        return (f"{item['name']} was installed on another edition ({was}): its data cannot be used here. "
                f"Remove it keeping its dataset, or install it again from the catalogue; the dataset is kept")
    env_path = base / ENV_FILE
    env = parse_env(env_path.read_text()) if env_path.exists() else {}
    env["APP"] = name
    engine.execute_with_stdin(CATEGORY, "env_write", render_env(env), file=str(env_path))
    _write_recipe(engine, folder, base)
    for path_text, uid, gid in _folders(item, env, here):
        if path_text.startswith("./"):
            engine.execute(CATEGORY, "folder_own", timeout=3600, uid=uid, gid=gid, path=str(base / path_text[2:]))
    return None


def install(runtime, apps, rt, engine, config, entry_id: str, name: str,
            values: Dict[str, str]) -> Dict[str, Any]:
    """
    Create the app from the catalogue entry; the caller brings it up.
    Returns what to show once: the settings marked "show" and the web port.
    """
    item = entry(entry_id)
    apps.check_name(name)
    app_file = apps.app_file_name(engine)
    folder = recipe(engine, item, app_file)
    if folder is None:
        raise CatalogueError(f"{item['name']} is not available on this edition yet")
    env = resolve(item, values, engine, config, used_ports(rt, engine, config=config),
                  used_ports(rt, engine, public_only=True, config=config))
    env["APP"] = name
    text = (folder / app_file).read_text()
    editable = {file_name: key for key, file_name in apps.app_files(engine).items()}
    files, others = {}, {}
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.name != app_file:
            if path.name in editable:
                files[editable[path.name]] = path.read_text()
            else:
                others[path.name] = path.read_text()
    dataset = apps.dataset_for(config.get_module_config("containers").get("pool") or "", name)
    if runtime.dataset_state(engine, dataset)["exists"] and name not in (
            config.get_module_config("containers").get("apps") or {}):
        env.update(_kept_secrets(item, Path(apps.compose_file(runtime, engine, dataset)).with_name(ENV_FILE)))
    apps.create(runtime, engine, config, name, text,
                extra={"catalogue": item["id"], "recipe": recipe_name(engine),
                       "web_port": web_port(item, env), "ports": app_ports(item, env)},
                env_text=render_env(env), files=files)
    try:
        base = Path(apps.compose_file(runtime, engine, dataset)).parent
        for file_name, content in others.items():
            engine.execute_with_stdin(CATEGORY, "compose_write", content, file=str(base / file_name))
        for path_text, uid, gid in _folders(item, env, recipe_name(engine)):
            path = base / path_text[2:] if path_text.startswith("./") else Path(path_text)
            engine.execute(CATEGORY, "folder_prepare", uid=uid, gid=gid, path=str(path))
    except Exception:
        apps.remove(runtime, engine, config, name)
        raise
    shown = {s.get("label", s["key"]): env[s["key"]] for s in item.get("settings", []) if s.get("show")}
    return {"name": name, "web_port": web_port(item, env), "shown": shown}
