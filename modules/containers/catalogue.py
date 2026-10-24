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
    from freedomnas import tls
except ImportError:
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
            own = ((config.get_module_config(PROXY_CATEGORY) or {}).get("acme") or {}).get("domain")
            values[key] = " ".join(h for h in (socket.gethostname(), _local_address(), f"*.{tls.local_domain()}",
                                               f"*.{own}" if own else "") if h)
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
    return results


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
    text = sources.pop(app_file)
    editable = {file_name: key for key, file_name in apps.app_files(engine).items()}
    files, others = {}, {}
    for file_name, content in sources.items():
        if file_name in editable:
            files[editable[file_name]] = content
        else:
            others[file_name] = content
    dataset = apps.dataset_for(config.get_module_config("containers").get("pool") or "", name)
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
        for path_text, uid, gid in _folders(item, env, recipe_name(engine)):
            path = base / path_text[2:] if path_text.startswith("./") else Path(path_text)
            engine.execute(CATEGORY, "folder_prepare", uid=uid, gid=gid, path=str(path))
            if path_text in _tls_folders(item):
                engine.execute(CATEGORY, "tls_copy", uid=uid, gid=gid, path=str(path),
                               cert=str(tls.cert_file()), key=str(tls.key_file()), hostname=_hostname())
        names = local_dns_text(item, config)
        if names is not None:
            engine.execute_with_stdin(CATEGORY, "compose_write", names, file=str(_local_dns_path(base, item)))
    except Exception:
        apps.remove(runtime, engine, config, name)
        raise
    shown = {s.get("label", s["key"]): env[s["key"]] for s in item.get("settings", []) if s.get("show")}
    return {"name": name, "web_port": web_port(item, env), "shown": shown}
