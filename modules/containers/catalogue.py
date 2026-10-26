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
    share     a group share of the NAS the app serves files from ("share":
              its name and group, "access": read or write, "account": the
              key holding the uid the app runs as); defaults to that share,
              made at install when missing (a dataset of the apps' pool,
              shared over SMB, its members from <KEY>_WRITERS: "*" for
              every account, or names separated by commas) and kept when
              the app goes; the app gets its own entry in the share's ACL.
              Another directory of a pool is taken as a plain folder, or,
              with <KEY>_SHARE "here" and when it is a dataset's own
              directory, made that share instead
    shares    group shares of the NAS mounted into the app, by name separated
              by commas (default: all of them; empty: none); the recipe's
              "share_mounts" lines (per recipe, {name} and {path}) replace
              its marker lines, and the app's account ("account", per
              recipe) gets its entry in each share's ACL ("access")
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
`scripts` names files of the recipe written, executable, into folders of
the app ({"file": ["./folder", ...]}) instead of beside the recipe; a
recipe without one of them simply has none.
`owned` lists folders whose whole tree belongs to the app's account
({"path": "./name"} or {"setting": KEY}, with "owner" per recipe): on an
install over kept data whose folder belongs to someone else (data that
came from another edition, where the account has another number), the tree
is given to that account.
`fixed` holds values the user never changes (an app's release, such as
NEXTCLOUD_VERSION: the same on every edition, so its data can move between
them); the app's Update writes the catalogue's current ones into its .env
(refresh_fixed) before fetching the images again. The releases an app is
installed or updated with are written into its dataset (RELEASE_FILE), and
`data_version` can name a file of the app's own that records the release
that last wrote its data ({"file", "pattern", "key"}): an install over kept
data written by a newer release than `fixed` names is refused, since apps
upgrade their data and never go back.
`prepare` lists folders
created before the first start ("./name", or {"path", "owner"} with the
owner per recipe; with "tls" the folder also gets a copy of FreedomNAS's
own certificate and key, cert.pem and key.pem, both in one file,
tls.pem, and the NAS's hostname the certificate is for, hostname, for an
app that serves HTTPS itself); `ports` lists ports used without a
setting (host networking), `address_ports` ports published on one address
setting only, which a service listening on loopback does not block.

A reinstall on a kept dataset keeps the passwords and secrets of its old
.env: the databases on the dataset were made with them.

An entry's "local_dns" names a file of its app folder (the app's own syntax)
that FreedomNAS keeps holding "header" and one "line" ({domain}, {address})
per domain the reverse proxy serves the sites by name under (<hostname>.home.arpa,
and the user's own domain once it has its certificate), and "off" otherwise; written at install and again at every reconcile when it changed
(the app is taken down then, its reconcile brings it up). Pi-hole's makes
every name of the local domain the NAS.

With the reverse proxy module set up, an entry whose web block names an
"inner" port for this edition goes behind it (see go_behind and proxy_sync):
the proxy serves the public web port over HTTPS, the app moves where only
the proxy reaches it; "behind_env" goes into its .env meanwhile, and a
recipe's proxied/ folder, when present, replaces files of the recipe.

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

try:
    from freedomnas import datasets as zfs_datasets, groupshares, tls
except ImportError:
    import datasets as zfs_datasets  # type: ignore[no-redef]
    import groupshares  # type: ignore[no-redef]
    import tls  # type: ignore[no-redef]



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
RELEASE_FILE = ".freedomnas-release"
SYSTEM_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run",
               "/sbin", "/sys", "/usr", "/var")

_RELATIVE = re.compile(r"\./[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_ABSOLUTE = re.compile(r"/[A-Za-z0-9._/-]+")
_TEXT = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_PASSWORD = re.compile(r"[A-Za-z0-9!#%&()*+,./:;<=>?@^_{|}~\[\]-]{8,128}")
_HOSTS = re.compile(r"(?:\*\.)?[A-Za-z0-9.:-]+(?: (?:\*\.)?[A-Za-z0-9.:-]+)*")
_TZ = re.compile(r"[A-Za-z0-9_+/-]{1,64}")
_IPV4 = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)")
_LISTEN = re.compile(r":(\d+)$")
_SHARE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


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


def _elsewhere(listen: str, address: str) -> bool:
    host = listen.rsplit(":", 1)[0].strip("[]").split("%")[0]
    return host not in ("*", "0.0.0.0", "::", address)


def used_ports(rt, engine, public_only: bool = False, config=None,
               address: Optional[str] = None) -> Set[int]:
    """
    Ports something on the NAS listens on (TCP or UDP), the ports
    containers publish, stopped ones included, and the ports of catalogue
    apps that are down. public_only leaves out what listens on loopback
    only, for a port published on one address; with that address, also
    what listens on another address of the NAS only (AppJail's dnsmasq for
    the jails, a bridge's own address).
    """
    ports: Set[int] = set()
    if config is not None:
        for app in (config.get_module_config("containers").get("apps") or {}).values():
            ports.update(int(p) for p in app.get("ports") or ([app["web_port"]] if app.get("web_port") else []))
    bind = address
    try:
        for row in records.run(engine, CATEGORY, "listening"):
            address = row.get("address") or ""
            match = _LISTEN.search(address)
            if match and not (public_only and (_loopback(address) or (bind and _elsewhere(address, bind)))):
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


def share_default(engine, config, setting: Dict[str, Any]) -> str:
    """
    Where the share a "share" setting names is, or will be made: the dataset
    <apps' pool>/<share>, under the pool's mountpoint, or at /<pool>/<share>
    when the pool's own dataset mounts nowhere.
    """
    existing = next((s for s in config.get_smb_shares() if s.get("name") == setting["share"]), None)
    if existing:
        return existing["path"]
    pool = (config.get_module_config("containers") or {}).get("pool") or ""
    if not pool:
        return ""
    name = f"{pool}/{setting['share']}"
    try:
        if zfs_datasets.exists(engine, name):
            return zfs_datasets.mountpoint_of(engine, name)[0]
        base, _ = zfs_datasets.mountpoint_of(engine, pool)
    except Exception:
        return ""
    return f"{base.rstrip('/')}/{setting['share']}" if base.startswith("/") else f"/{name}"


def share_info(engine, config, item: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """For the install form: each share setting's share, whether it exists, and its members."""
    info = {}
    for setting in item.get("settings", []):
        if setting["type"] != "share":
            continue
        existing = next((s for s in config.get_smb_shares() if s.get("name") == setting["share"]), None)
        info[setting["key"]] = {
            "name": setting["share"], "path": share_default(engine, config, setting),
            "exists": existing is not None,
            "members": groupshares.members_of(config, existing["group"]) if existing and existing.get("group") else [],
        }
    return info


def _writers(config, setting: Dict[str, Any], value: str) -> List[str]:
    value = value.strip()
    if value in ("", "*"):
        return ["*"]
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in _accounts(config)]
    if unknown:
        raise CatalogueError(f"{setting.get('label', setting['key'])}: no account named {', '.join(unknown)}")
    return names


def group_share_names(config) -> List[str]:
    return [s["name"] for s in config.get_smb_shares() if s.get("group") and _SHARE_NAME.fullmatch(s["name"])]


def _chosen_shares(config, setting: Dict[str, Any], value: str) -> List[Dict[str, Any]]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    known = {s["name"]: s for s in config.get_smb_shares() if s.get("group")}
    unknown = [n for n in names if n not in known or not _SHARE_NAME.fullmatch(n)]
    if unknown:
        raise CatalogueError(f"{setting.get('label', setting['key'])}: no group share named {', '.join(unknown)}")
    return [known[n] for n in names]


def _render_share_mounts(item: Dict[str, Any], recipe: str, text: str, shares: List[Dict[str, Any]]) -> str:
    templates = (item.get("share_mounts") or {}).get(recipe) or {}
    lines = []
    for line in text.splitlines(keepends=True):
        marker = line.strip()
        if marker in templates:
            lines += [templates[marker].format(name=s["name"], path=s["path"]) + "\n" for s in shares]
        else:
            lines.append(line)
    return "".join(lines)


def _app_account(setting: Dict[str, Any], env: Dict[str, str], recipe: str) -> str:
    account = setting["account"]
    return str(account[recipe]) if isinstance(account, dict) else env[account]


def _share_here(values: Dict[str, str], setting: Dict[str, Any]) -> bool:
    return str(values.get(f"{setting['key']}_SHARE", "")).strip() == "here"


def _check_share_here(engine, config, setting: Dict[str, Any], path: str) -> None:
    label = setting.get("label", setting["key"])
    if groupshares.share_at(config, path) is not None:
        return
    taken = next((s for s in config.get_smb_shares() if s.get("name") == setting["share"]), None)
    if taken:
        raise CatalogueError(f"{label}: there is already a share named {setting['share']}, of {taken['path']}: "
                             "remove it on the Shares page first, or choose it")
    if not zfs_datasets.dataset_mounted_at(engine, path):
        raise CatalogueError(f"{label}: {path} is not where a dataset of a data pool is mounted, "
                             "so it cannot become a share of its own")


def _attach_shares(engine, config, item: Dict[str, Any], values: Dict[str, str], env: Dict[str, str],
                   name: str) -> None:
    """Make the shares the app serves when they are missing, and let the app in."""
    recipe = recipe_name(engine)
    for setting in item.get("settings", []):
        if setting["type"] == "shares":
            for share in _chosen_shares(config, setting, str(values.get(setting["key"], ""))):
                groupshares.add_app(engine, config, share["path"], name, _app_account(setting, env, recipe),
                                    setting.get("access") == "write")
        if setting["type"] != "share":
            continue
        path, account = env[setting["key"]], _app_account(setting, env, recipe)
        if path == share_default(engine, config, setting) and not any(
                s.get("name") == setting["share"] for s in config.get_smb_shares()):
            pool = config.get_module_config("containers").get("pool")
            dataset = f"{pool}/{setting['share']}"
            if not zfs_datasets.exists(engine, dataset):
                inherited = f"{zfs_datasets.mountpoint_of(engine, pool)[0].rstrip('/')}/{setting['share']}"
                if inherited == path:
                    zfs_datasets.create(engine, pool, setting["share"])
                else:
                    zfs_datasets.create(engine, pool, setting["share"], "path", path)
            writers = _writers(config, setting, values.get(f"{setting['key']}_WRITERS", "*"))
            groupshares.create(engine, config, setting["share"], path, setting["share"],
                               [] if writers == ["*"] else writers, everyone=writers == ["*"])
        elif _share_here(values, setting) and groupshares.share_at(config, path) is None:
            writers = _writers(config, setting, values.get(f"{setting['key']}_WRITERS", "*"))
            groupshares.create(engine, config, setting["share"], path, setting["share"],
                               [] if writers == ["*"] else writers, everyone=writers == ["*"])
        groupshares.add_app(engine, config, path, name, account, setting.get("access") == "write")


def defaults(item: Dict[str, Any], config, username: str, taken: Set[int], engine=None) -> Dict[str, str]:
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
            own = ((config.get_module_config(PROXY_CATEGORY) or {}).get("acme") or {}).get("domain")
            values[key] = " ".join(h for h in (socket.gethostname(), _local_address(), f"*.{tls.local_domain()}",
                                               f"*.{own}" if own else "") if h)
        elif kind == "address":
            values[key] = _local_address() or ""
        elif kind == "secret":
            continue
        elif kind == "share":
            values[key] = share_default(engine, config, setting) if engine else ""
            values[f"{key}_WRITERS"] = "*"
        elif kind == "shares":
            values[key] = ",".join(group_share_names(config))
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
        if kind == "shares":
            _chosen_shares(config, setting, value)
            continue
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
        elif kind == "share":
            value = value.rstrip("/") or value
            if value != share_default(engine, config, setting):
                _check_folder(engine, value, {})
                if _share_here(values, setting):
                    _check_share_here(engine, config, setting, value)
            _writers(config, setting, str(values.get(f"{key}_WRITERS", "*")))
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


def kept_folders(runtime, apps, engine, config, item: Dict[str, Any], name: str) -> Dict[str, str]:
    """
    The folders the app kept on its dataset used before, from its old .env,
    when it is installed again on that data. Its database still points
    there: a reinstall that takes another folder finds its library empty.
    """
    section = config.get_module_config("containers") or {}
    dataset = apps.dataset_for(section.get("pool") or "", name)
    if name in (section.get("apps") or {}) or not runtime.dataset_state(engine, dataset)["exists"]:
        return {}
    env_path = Path(apps.compose_file(runtime, engine, dataset)).with_name(ENV_FILE)
    if not env_path.exists():
        return {}
    old = parse_env(env_path.read_text())
    return {s["key"]: old[s["key"]] for s in item.get("settings", [])
            if s["type"] in ("folder", "share") and old.get(s["key"])}


def folder_warnings(item: Dict[str, Any], kept: Dict[str, str], env: Dict[str, str]) -> List[str]:
    return [f"{s.get('label', s['key'])}: {item['name']}'s kept data used {kept[s['key']]}, it now uses "
            f"{env[s['key']]}; what it recorded about the files there (a library) will not find them"
            for s in item.get("settings", []) if s["key"] in kept and env.get(s["key"]) != kept[s["key"]]]


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


def _hostname() -> str:
    return socket.gethostname() or "freedomnas"


def _tls_folders(item: Dict[str, Any]) -> List[str]:
    return [entry["path"] for entry in item.get("prepare", []) if isinstance(entry, dict) and entry.get("tls")]


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


def moved(engine, app: Dict[str, Any]) -> bool:
    """A catalogue app last installed by another edition's recipe."""
    return bool(app.get("catalogue")) and (app.get("recipe") or "linux") != recipe_name(engine)


def _account_of(uid: str) -> Optional[str]:
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        return None


def _kept_values(item: Dict[str, Any], env: Dict[str, str], config, engine) -> Dict[str, str]:
    """An install's values from a kept .env: the same folders, ports, account and passwords."""
    accounts = _accounts(config)
    values = defaults(item, config, accounts[0] if accounts else "", set(), engine)
    for setting in item.get("settings", []):
        key = setting["key"]
        if setting["type"] == "user":
            account = _account_of(env.get(setting["uid"], ""))
            if account in accounts:
                values[key] = account
        elif setting["type"] != "secret" and env.get(key):
            values[key] = env[key]
    return values


def reinstall(runtime, apps, rt, engine, config, name: str, app: Dict[str, Any], file: str) -> None:
    """
    Install the app again from the catalogue over the data it kept, with the
    values of its old .env (passwords and secrets come along: see install),
    and bring it up: this edition's recipe, folders' owners, paths and
    reverse proxy address replace those of wherever it ran before. The
    proxy lets go of the app's port first, as when the app is removed from
    the page; when the install fails, the app stays recorded as it was.
    """
    item = entry(app["catalogue"])
    env_path = Path(file).with_name(ENV_FILE)
    env = parse_env(env_path.read_text()) if env_path.exists() else {}
    values = _kept_values(item, env, config, engine)
    record = dict((config.get_module_config("containers").get("apps") or {})[name])
    apps.remove(runtime, engine, config, name)
    try:
        proxy_sync(runtime, apps, rt, engine, config)
        install(runtime, apps, rt, engine, config, item["id"], name, values)
    except Exception:
        section = config.get_module_config("containers")
        section.setdefault("apps", {})[name] = record
        config.set_module_config("containers", section)
        raise
    apps.set_state(runtime, engine, config, name, "up")


def adopt(runtime, apps, rt, engine, config, name: str, app: Dict[str, Any], file: str) -> Optional[str]:
    """
    An app from the catalogue whose pool comes from another edition: when
    the entry lists both recipes under "portable", write this edition's
    recipe beside its data and .env, give the app's own folders (./name,
    never a folder of the pool it only mounts) to this edition's owners,
    and answer None. Any other app is installed again from the catalogue
    over its kept data (reinstall), and None answered once it is up; what
    stops that (data newer than the catalogue's release, a port taken) is
    the answer instead, the data left as they are.
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
        try:
            reinstall(runtime, apps, rt, engine, config, name, app, file)
        except Exception as exc:
            return (f"{item['name']} came from another edition ({was}) and could not be installed again on "
                    f"its kept data: {exc}. Install it again from the catalogue; the dataset is kept")
        return None
    env_path = base / ENV_FILE
    env = parse_env(env_path.read_text()) if env_path.exists() else {}
    env["APP"] = name
    engine.execute_with_stdin(CATEGORY, "env_write", render_env(env), file=str(env_path))
    _write_recipe(engine, folder, base)
    for path_text, uid, gid in _folders(item, env, here):
        if path_text.startswith("./"):
            engine.execute(CATEGORY, "folder_own", timeout=3600, uid=uid, gid=gid, path=str(base / path_text[2:]))
    return None


def name_domains(config) -> List[str]:
    """The domains the reverse proxy serves the sites by name under, local first."""
    proxy = config.get_module_config(PROXY_CATEGORY) or {}
    if not (proxy.get("enabled") and proxy.get("names")):
        return []
    own = (proxy.get("acme") or {}).get("domain")
    return [tls.local_domain()] + ([own] if own else [])


def local_dns_text(item: Dict[str, Any], config) -> Optional[str]:
    """
    What the entry's "local_dns" file holds: "header" and a "line" (the app's
    own syntax, with {domain} and {address}) per domain the reverse proxy
    serves the sites by name under, "off" when there is none; None when the
    entry has no such file.
    """
    spec = item.get("local_dns")
    if not spec:
        return None
    address = _local_address()
    domains = name_domains(config) if address else []
    if not domains:
        return spec.get("off", "")
    return spec.get("header", "") + "".join(spec["line"].format(domain=d, address=address) for d in domains)


def _local_dns_path(base: Path, item: Dict[str, Any]) -> Path:
    path_text = item["local_dns"]["file"]
    return base / path_text[2:] if path_text.startswith("./") else Path(path_text)


def refresh_local_dns(runtime, apps, engine, config, desired: Dict[str, Any]) -> List:
    """
    Write each catalogue app's "local_dns" file again when the names it
    should hold changed (names turned on or off, a new address or hostname),
    and take down the apps that are up so the app reconcile starts them with
    it. An app installed before its entry had the file is left alone.
    """
    if not desired.get("enabled"):
        return []
    results = []
    for name, app in sorted((desired.get("apps") or {}).items()):
        try:
            item = entry(app.get("catalogue") or "")
        except CatalogueError:
            continue
        text = local_dns_text(item, config)
        if text is None:
            continue
        file = apps.compose_file(runtime, engine, app["dataset"])
        path = _local_dns_path(Path(file).parent, item)
        if not path.parent.is_dir():
            continue
        try:
            if path.read_text() == text:
                continue
        except OSError:
            pass
        try:
            engine.execute_with_stdin(CATEGORY, "compose_write", text, file=str(path))
            results.append(apps._result(f"{name}.local_dns", app=name))
            if app.get("state") == "up" and runtime.info(engine) is not None:
                apps._compose(engine, "compose_down", name, file)
        except Exception as exc:
            results.append(apps._result(f"{name}.local_dns", apps._stderr(exc), app=name))
    return results


def refresh_tls(runtime, apps, engine, desired: Dict[str, Any]) -> List:
    """
    Copy FreedomNAS's certificate again into each catalogue app's "tls" folder
    whose copy differs (the certificate was issued again for a new address or
    hostname), and take down the apps that are up so the app reconcile starts
    them with it.
    """
    if not desired.get("enabled"):
        return []
    try:
        cert, key = tls.cert_file().read_bytes(), tls.key_file().read_bytes()
    except OSError:
        return []
    results = []
    running = None
    for name, app in sorted((desired.get("apps") or {}).items()):
        try:
            item = entry(app.get("catalogue") or "")
        except CatalogueError:
            continue
        folders = _tls_folders(item)
        if not folders:
            continue
        file = apps.compose_file(runtime, engine, app["dataset"])
        base = Path(file).parent
        env_path = base / ENV_FILE
        env = parse_env(env_path.read_text()) if env_path.exists() else {}
        owners = {path: (uid, gid) for path, uid, gid in _folders(item, env, app.get("recipe") or recipe_name(engine))}
        changed = False
        for path_text in folders:
            folder = base / path_text[2:] if path_text.startswith("./") else Path(path_text)
            if not folder.is_dir():
                continue
            try:
                if ((folder / "cert.pem").read_bytes() == cert and (folder / "key.pem").read_bytes() == key
                        and (folder / "tls.pem").read_bytes() == key + cert
                        and (folder / "hostname").read_text() == _hostname()):
                    continue
            except OSError:
                pass
            uid, gid = owners.get(path_text, ("0", "0"))
            try:
                engine.execute(CATEGORY, "tls_copy", uid=uid, gid=gid, path=str(folder),
                               cert=str(tls.cert_file()), key=str(tls.key_file()), hostname=_hostname())
                changed = True
            except Exception as exc:
                results.append(apps._result(f"{name}.tls", apps._stderr(exc), app=name))
        if not changed:
            continue
        results.append(apps._result(f"{name}.tls", app=name))
        if app.get("state") != "up":
            continue
        if running is None:
            running = runtime.info(engine) is not None
        if running:
            try:
                apps._compose(engine, "compose_down", name, file)
            except Exception as exc:
                results.append(apps._result(f"{name}.down", apps._stderr(exc), app=name))
    return results


PROXY_CATEGORY = "proxy"
PROXY_OWNER = "app"
PROXIED_DIR = "proxied"
INTERNAL_PORTS = range(20000, 30000)


def _recipe_files(folder: Path, behind: bool = False) -> Dict[str, str]:
    """The recipe's files by name; behind the proxy, those of its proxied/ folder replace them."""
    files = {path.name: path.read_text() for path in sorted(folder.iterdir()) if path.is_file()}
    overlay = folder / PROXIED_DIR
    if behind and overlay.is_dir():
        files.update({path.name: path.read_text() for path in sorted(overlay.iterdir()) if path.is_file()})
    return files


def _proxy_profile(engine) -> Dict[str, Any]:
    try:
        section = engine.get_profile_section(PROXY_CATEGORY)
    except Exception:
        return {}
    return section if section.get("site_template") else {}


def proxy_on(engine, config) -> bool:
    """The reverse proxy module is installed here and set up."""
    return bool(_proxy_profile(engine)) and bool((config.get_module_config(PROXY_CATEGORY) or {}).get("enabled"))


def _upstream_kind(engine) -> str:
    try:
        return engine.get_profile_value(CATEGORY, "proxy_upstream") or ""
    except Exception:
        return ""


def proxyable(engine, item: Dict[str, Any]) -> bool:
    """
    The entry's web interface can sit behind the proxy: its web port is a
    setting, it serves no HTTPS of its own, and its web block names the
    port inside the app for this edition's recipe ("inner").
    """
    web = item.get("web") or {}
    inner = (web.get("inner") or {}).get(recipe_name(engine))
    return bool(inner) and not web.get("fixed") and not _tls_folders(item) and bool(_upstream_kind(engine))


def proxy_wanted(engine, config, item: Dict[str, Any]) -> bool:
    return proxyable(engine, item) and proxy_on(engine, config)


def _internal_port(rt, engine, config) -> int:
    taken = used_ports(rt, engine, config=config)
    for app in (config.get_module_config(CATEGORY).get("apps") or {}).values():
        upstream = (app.get("proxy") or {}).get("upstream") or ""
        if upstream.rpartition(":")[2].isdigit():
            taken.add(int(upstream.rpartition(":")[2]))
    for port in INTERNAL_PORTS:
        if port not in taken:
            return port
    raise CatalogueError("No free internal port left for the reverse proxy")


def go_behind(rt, engine, config, item: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    """
    Put the app's web port behind the proxy: the proxy takes the public
    port (the web setting), the app moves where only the proxy reaches it.
    "publish": the recipe publishes ${WEB_PUBLISH} (127.0.0.1:<internal>);
    "address": the proxy reaches the member named web.service at its own
    address on the inner port (its proxied/ files expose nothing).
    """
    web = item.get("web") or {}
    record = {"port": int(env[web["port"]]), "inner": int(web["inner"][recipe_name(engine)])}
    env.update(web.get("behind_env") or {})
    if _upstream_kind(engine) == "publish":
        internal = _internal_port(rt, engine, config)
        env["WEB_PUBLISH"] = f"127.0.0.1:{internal}"
        record["upstream"] = env["WEB_PUBLISH"]
    else:
        record["member"] = web.get("service") or ""
    return record


def come_back(item: Dict[str, Any], env: Dict[str, str]) -> None:
    for key in ["WEB_PUBLISH", *((item.get("web") or {}).get("behind_env") or {})]:
        env.pop(key, None)


def _site_file(engine, name: str) -> str:
    return f"{_proxy_profile(engine)['sites_dir']}/{PROXY_OWNER}-{name}.caddy"


def _upstream(engine, rt, name: str, record: Dict[str, Any], containers_now: List[Dict[str, Any]]) -> Optional[str]:
    if record.get("upstream"):
        return record["upstream"]
    member = f"{name}-{record['member']}" if record.get("member") else None
    for container in containers_now:
        if (member is None or container.get("name") == member) and container.get("address") \
                and container.get("state") == "running":
            if member is None and not str(container.get("name") or "").startswith(f"{name}-"):
                continue
            return f"{container['address'].split(',')[0].strip()}:{record['inner']}"
    return None


def proxy_sync(runtime, apps, rt, engine, config) -> List:
    """
    Move catalogue apps behind the proxy (or back when it is gone), then
    write one proxy site per app that is up and behind it, drop the sites of
    apps that are not, and reload the proxy when anything changed.
    """
    section = config.get_module_config(CATEGORY)
    if not section.get("enabled") or not apps.supported(engine):
        return []
    results = []
    on = proxy_on(engine, config)
    records = dict(section.get("apps") or {})
    for name, app in sorted(records.items()):
        try:
            item = entry(app.get("catalogue") or "")
        except CatalogueError:
            continue
        behind = bool(app.get("proxy"))
        if behind == (on and proxyable(engine, item)):
            continue
        try:
            _move(runtime, apps, rt, engine, config, name, app, item, not behind)
            results.append(apps._result(f"{name}.proxy", app=name, behind=not behind))
        except Exception as exc:
            results.append(apps._result(f"{name}.proxy", apps._stderr(exc), app=name))
    if not _proxy_profile(engine):
        return results
    profile = _proxy_profile(engine)
    try:
        current, _ = engine.execute(PROXY_CATEGORY, "sites_list", dir=profile["sites_dir"])
    except Exception:
        current = ""
    present = {line.split()[3] for line in current.splitlines()
               if line.startswith("# freedomnas-site ") and len(line.split()) >= 6 and line.split()[2] == PROXY_OWNER}
    try:
        containers_now = rt.containers(engine)
    except Exception:
        containers_now = []
    wanted = {}
    if on:
        for name, app in (config.get_module_config(CATEGORY).get("apps") or {}).items():
            record = app.get("proxy")
            if record and app.get("state") == "up":
                upstream = _upstream(engine, rt, name, record, containers_now)
                if upstream:
                    wanted[name] = profile["site_template"].format(
                        owner=PROXY_OWNER, name=name, port=record["port"], upstream=upstream)
    changed = False
    try:
        for name, text in sorted(wanted.items()):
            file = _site_file(engine, name)
            old, _ = engine.execute(PROXY_CATEGORY, "file_read", check=False, file=file)
            if old.rstrip() != text.rstrip():
                engine.execute_with_stdin(PROXY_CATEGORY, "site_write", text, file=file)
                changed = True
        for name in sorted(present - set(wanted)):
            engine.execute(PROXY_CATEGORY, "file_remove", file=_site_file(engine, name))
            changed = True
        if changed and on:
            engine.execute(PROXY_CATEGORY, "validate", file=profile["config_file"])
            engine.execute(PROXY_CATEGORY, "reload", file=profile["config_file"])
        if changed:
            results.append(apps._result("proxy.sites", sites=sorted(wanted)))
    except Exception as exc:
        results.append(apps._result("proxy.sites", apps._stderr(exc)))
    if changed and on:
        results += _proxy_reconcile(apps, engine, config)
    return results


def _proxy_reconcile(apps, engine, config) -> List:
    """The proxy module's own reconcile, so what follows the sites (the network's DNS records) does too."""
    try:
        from freedomnas.module_loader import ModuleLoader
    except ImportError:
        return []
    try:
        for manifest, module in ModuleLoader(engine, config).instances():
            if manifest.get("id", module.name) == PROXY_CATEGORY:
                return list(module.reconcile(config.get_module_config(PROXY_CATEGORY) or {}, "additive") or [])
    except Exception as exc:
        return [apps._result("proxy.follow", str(exc))]
    return []


def _move(runtime, apps, rt, engine, config, name: str, app: Dict[str, Any], item: Dict[str, Any],
          behind: bool) -> None:
    """An installed app goes behind the proxy or back on its own port, its files and .env rewritten."""
    file = apps.compose_file(runtime, engine, app["dataset"])
    base = Path(file).parent
    env_path = base / ENV_FILE
    env = parse_env(env_path.read_text()) if env_path.exists() else {}
    folder = recipe(engine, item, Path(file).name)
    if folder is None:
        raise CatalogueError(f"{item['name']} has no recipe on this edition")
    was_up = app.get("state") == "up"
    if was_up:
        apps._compose(engine, "compose_down", name, file)
    record = go_behind(rt, engine, config, item, env) if behind else None
    if not behind:
        come_back(item, env)
    engine.execute_with_stdin(CATEGORY, "env_write", render_env(env), file=str(env_path))
    base_files = _recipe_files(folder)
    for file_name, content in _recipe_files(folder, behind).items():
        if (folder / PROXIED_DIR / file_name).is_file() or file_name in base_files:
            engine.execute_with_stdin(CATEGORY, "compose_write", content, file=str(base / file_name))
    records = dict(config.get_module_config(CATEGORY).get("apps") or {})
    current = dict(records.get(name) or app)
    if record:
        current["proxy"] = record
    else:
        current.pop("proxy", None)
    records[name] = current
    section = config.get_module_config(CATEGORY)
    section["apps"] = records
    config.set_module_config(CATEGORY, section)
    if was_up:
        apps._compose(engine, "compose_update" if apps._profile(engine, "compose_update") else "compose_up",
                      name, file)


def _release(text: str) -> tuple:
    return tuple(int(p) for p in text.split(".") if p.isdigit())


def data_release(item: Dict[str, Any], base: Path) -> Optional[str]:
    """The release that last wrote the app's kept data, from its `data_version` file, or None."""
    spec = item.get("data_version")
    if not spec:
        return None
    try:
        match = re.search(spec["pattern"], (base / spec["file"][2:]).read_text())
    except OSError:
        return None
    return match.group(1) if match else None


def _versioned(text: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)*", text or ""))


def kept_releases(item: Dict[str, Any], base: Path) -> Dict[str, str]:
    """The releases that last wrote the app's kept data: RELEASE_FILE, then the app's own file."""
    path = base / RELEASE_FILE
    found = parse_env(path.read_text()) if path.exists() else {}
    own = data_release(item, base)
    if own:
        found[item["data_version"]["key"]] = own
    return found


def check_data_release(item: Dict[str, Any], base: Path, env: Dict[str, str]) -> None:
    for key, found in kept_releases(item, base).items():
        wanted = env.get(key, "")
        if _versioned(found) and _versioned(wanted) and _release(found) > _release(wanted):
            raise CatalogueError(
                f"{item['name']}'s data was last written by release {found}, newer than the {wanted} this NAS "
                f"installs: {item['name']} never goes back to an older release. Update FreedomNAS until its "
                f"catalogue reaches {found} or later, then install it again on the kept data")


def record_releases(engine, item: Dict[str, Any], base: Path, env: Dict[str, str]) -> None:
    fixed = {key: env[key] for key in item.get("fixed") or {} if key in env}
    if fixed:
        engine.execute_with_stdin(CATEGORY, "compose_write", render_env(fixed), file=str(base / RELEASE_FILE))


def refresh_fixed(runtime, apps, engine, config, name: str) -> bool:
    """
    Bring a catalogue app's .env to the catalogue's current `fixed` values
    (a newer release), refusing what would take its data back. Returns
    whether anything changed.
    """
    app = ((config.get_module_config("containers") or {}).get("apps") or {}).get(name) or {}
    if not app.get("catalogue"):
        return False
    item = entry(app["catalogue"])
    fixed = item.get("fixed") or {}
    env_path = Path(apps.compose_file(runtime, engine, app["dataset"])).with_name(ENV_FILE)
    if not fixed or not env_path.exists():
        return False
    env = parse_env(env_path.read_text())
    if all(env.get(k) == v for k, v in fixed.items()):
        return False
    env.update(fixed)
    check_data_release(item, env_path.parent, env)
    engine.execute_with_stdin(CATEGORY, "env_write", render_env(env), file=str(env_path))
    record_releases(engine, item, env_path.parent, env)
    return True


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
    address = next((values.get(s["key"]) for s in item.get("settings", []) if s.get("type") == "address"), None)
    env = resolve(item, values, engine, config, used_ports(rt, engine, config=config),
                  used_ports(rt, engine, public_only=True, config=config, address=address or _local_address()))
    env["APP"] = name
    behind = proxy_wanted(engine, config, item)
    record_proxy = None
    if behind:
        record_proxy = go_behind(rt, engine, config, item, env)
    sources = _recipe_files(folder, behind)
    scripts = {file_name: sources.pop(file_name) for file_name in item.get("scripts", {}) if file_name in sources}
    shares = [s for setting in item.get("settings", []) if setting["type"] == "shares"
              for s in _chosen_shares(config, setting, str(values.get(setting["key"], "")))]
    text = _render_share_mounts(item, recipe_name(engine), sources.pop(app_file), shares)
    editable = {file_name: key for key, file_name in apps.app_files(engine).items()}
    files, others = {}, {}
    for file_name, content in sources.items():
        if file_name in editable:
            files[editable[file_name]] = content
        else:
            others[file_name] = content
    kept = kept_folders(runtime, apps, engine, config, item, name)
    dataset = apps.dataset_for(config.get_module_config("containers").get("pool") or "", name)
    if runtime.dataset_state(engine, dataset)["exists"] and name not in (
            config.get_module_config("containers").get("apps") or {}):
        check_data_release(item, Path(apps.compose_file(runtime, engine, dataset)).parent, env)
    if runtime.dataset_state(engine, dataset)["exists"] and name not in (
            config.get_module_config("containers").get("apps") or {}):
        env.update(_kept_secrets(item, Path(apps.compose_file(runtime, engine, dataset)).with_name(ENV_FILE)))
    extra = {"catalogue": item["id"], "recipe": recipe_name(engine),
             "web_port": web_port(item, env), "ports": app_ports(item, env)}
    if record_proxy:
        extra["proxy"] = record_proxy
    apps.create(runtime, engine, config, name, text, extra=extra, env_text=render_env(env), files=files)
    try:
        base = Path(apps.compose_file(runtime, engine, dataset)).parent
        for file_name, content in others.items():
            engine.execute_with_stdin(CATEGORY, "compose_write", content, file=str(base / file_name))
        for owned in item.get("owned", []):
            path_text = owned["path"] if "path" in owned else env.get(owned["setting"], "")
            owner = _owner_for(owned.get("owner"), recipe_name(engine))
            path = base / path_text[2:] if path_text.startswith("./") else Path(path_text)
            if owner and path_text and path.is_dir() and str(path.stat().st_uid) != owner[0]:
                engine.execute(CATEGORY, "folder_take", uid=owner[0], gid=owner[1], path=str(path), timeout=3600)
        for file_name, content in scripts.items():
            for folder in item["scripts"][file_name]:
                engine.execute_with_stdin(CATEGORY, "script_write", content, file=str(base / folder[2:] / file_name))
        for path_text, uid, gid in _folders(item, env, recipe_name(engine)):
            path = base / path_text[2:] if path_text.startswith("./") else Path(path_text)
            engine.execute(CATEGORY, "folder_prepare", uid=uid, gid=gid, path=str(path))
            if path_text in _tls_folders(item):
                engine.execute(CATEGORY, "tls_copy", uid=uid, gid=gid, path=str(path),
                               cert=str(tls.cert_file()), key=str(tls.key_file()), hostname=_hostname())
        names = local_dns_text(item, config)
        if names is not None:
            engine.execute_with_stdin(CATEGORY, "compose_write", names, file=str(_local_dns_path(base, item)))
        _attach_shares(engine, config, item, values, env, name)
        record_releases(engine, item, base, env)
    except Exception:
        apps.remove(runtime, engine, config, name)
        raise
    shown = {s.get("label", s["key"]): env[s["key"]] for s in item.get("settings", []) if s.get("show")}
    result = {"name": name, "web_port": web_port(item, env), "shown": shown}
    warnings = folder_warnings(item, kept, env)
    if warnings:
        result["warnings"] = warnings
    return result
