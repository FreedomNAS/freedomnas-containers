# freedomnas-containers

FreedomNAS community module for containers, built from
[freedomnas-module-template](https://codeberg.org/FreedomNAS/freedomnas-module-template).
Package: `freedomnas-module-containers`. User guide: *Containers* in the
FreedomNAS documentation.

Docker runs from the edition's own packages, on every Linux edition
(Debian, Arch, Artix, Void, Gentoo). On FreeBSD the runtime is AppJail:
containers are jails, apps are AppJail Director projects; the catalogue
follows.

The golden rule holds here as in the core: the Python code knows no
runtime. Every command, its arguments, the way its output reads, the files
the host needs and the words the page shows come from
`profiles/containers/containers-<os>.json`; a new runtime or edition is a
new profile, not new code (see *Profile keys* below).

## What it does

- **Install Docker** from the WebUI: on Debian from Docker's APT repository
  (`runtime_repository` in the profile), then the packages and services of
  freedomnas-profiles' `packages/containers.json` for the edition and init.
- **Put Docker's data on a pool**: every directory in the profile's
  `data_dirs` (`/var/lib/docker`, `/var/lib/containerd`) is mounted from a
  dataset `<pool>/containers/<name>` with `canmount=on`, so a pool imported
  at boot has them mounted by the system's ZFS mount service before any
  service starts, even one that pulls Docker in early. Docker's own
  configuration and image store are left as they are. What a directory
  held is moved aside to `<dir>.pre-freedomnas` (`<dir>.pre-freedomnas-<date>`
  when that exists already) and copied onto the dataset; the old copies
  are never deleted.
- **Start Docker after the pool**: Docker, containerd and `docker.socket`
  are kept from starting at boot by themselves; `reconcile()` mounts the
  datasets of a pool it imported and then starts them, at every boot and
  after a restore.
- **Apps on a pool mounted nowhere**: when the pool has `mountpoint=none`
  or `legacy`, `<pool>/apps` gets `mountpoint=/<pool>/apps`.
- **Containers and images**: list, start, stop, restart, logs and remove
  containers; download, remove and prune images; run a container from an
  image with ports, volumes, environment and a restart policy (Expert).
  Names, image references and every option are checked before they reach
  `docker`, and `run`'s options reach it as separate words.
- **Apps**: Docker Compose projects, each on a dataset `<pool>/apps/<name>`
  holding its `compose.yaml` and the folders it mounts with `./`. The file
  is checked with `docker compose config` before it is written; apps are
  brought up, down and updated (pull, then up) as jobs, and `reconcile()`
  brings up every app recorded as up whose containers are not all running,
  after Docker. A removed app's dataset is kept unless asked otherwise, and
  a new app of the same name uses it again. Compose files are checked with
  `docker compose config` run with the app's dataset as project directory,
  so its `.env` and relative paths are those it will run with.
- **Catalogue**: ready-made apps (Home Assistant, Immich, Jellyfin,
  Nextcloud, Pi-hole, Vaultwarden) in `modules/containers/catalogue/<id>/`,
  each an `app.json` (settings and their types, see `catalogue.py`) and a
  `compose.yaml` reading them as `${VARIABLES}`. Installing one creates an
  ordinary app whose `.env` (0600) holds the settings; ports are checked
  against what listens on the NAS (`listening`), what containers publish and the
  ports of catalogue apps that are down; passwords are generated, shown
  once, and kept when an app is installed again on its kept dataset.

Configuration, `modules.containers` in `config.yaml`:

```yaml
modules:
  containers:
    enabled: true
    pool: tank
    datasets:
      docker: tank/containers/docker
      containerd: tank/containers/containerd
    apps:
      site:
        dataset: tank/apps/site
        state: up
```

## Layout

```
modules/containers/
├── manifest.json        id, supported_os, webui (page /containers/, sidebar entry)
├── module.py            ContainersModule: health(), reconcile()
├── runtime.py           the runtime: install, datasets, host configuration, start after the pool, reconcile
├── containers.py        containers and images: list, actions, logs, pull, run words, input checks, errors
├── records.py           reads a command's output as records, as the profile's <action>_output says
├── apps.py              apps on their own datasets: create, edit, up/down, update, remove, reconcile
├── catalogue.py         ready-made apps: defaults, checks, .env, folders
├── catalogue/<id>/      app.json + compose.yaml for each ready-made app
├── api_routes.py        /api/containers/... (install, setup, pull and run run as jobs, one at a time)
└── frontend/            containers.html, js/containers.js, icons.svg
profiles/containers/
└── containers-<os>.json everything the runtime does (see below)
tests/                   pytest against the freedomnas-core checkout beside this one
```

## Profile keys

Each `containers-<os>.json` holds, under `containers`:

| Key | What |
|-----|------|
| `runtime_name`, `backend` | the name shown, a label |
| `runtime_check`, `running_check` | installed?; running? (without it: every service of `packages/containers.json` running) |
| `info` + `info_output` | records `version`, `data_root`, `driver`, `containers`, `images` |
| `data_dirs`, `data_move`, `data_copy` | the runtime's data directories, put on `<pool>/containers/<name>` |
| `boot_services`, `services_order`, `socket_units` | enable or disable the services at boot; start order; extra units |
| `runtime_repository` | commands run before the packages are installed |
| `facts`, `config_files`, `configure`, `configure_services` | host preparation at setup and every boot: values of the system, files (whole, or a block in a file kept otherwise), commands, services started |
| `ps` + `ps_output` | records `id`, `name`, `image`, `state` (`running`), `status`, `published`, `created`, `address` |
| `ports_pattern`, `name_pattern`, `name_hint` | how `published` reads (groups host, inner, proto); valid names |
| `images` + `images_output` | records `id`, `repository`, `tag`, `size`, `created`, `containers` |
| `start`, `stop`, `restart`, `remove`, `remove_force`, `logs` | `{container}`, `{lines}` |
| `image_pull`, `image_remove`, `image_prune` | `{image}` |
| `run`, `run_words`, `run_after` | `run` reads NUL-separated words on stdin; `run_words` builds them; `run_after` maps a restart policy to an action |
| `error_filter` | `{keep, context, strip}`: the stderr lines that name the cause |
| `listening` + `listening_output` | records `address` (`host:port`) of what listens on the NAS |
| `app_file`, `compose_check`, `compose_write`, `compose_up`, `compose_down`, `compose_pull`, `compose_update`, `compose_remove`, `compose_logs`, `app_members` | apps: file name, checker (stdin, `{dir}`), project commands (`{project}`, `{file}`), the project's containers |
| `env_write`, `folder_prepare` | catalogue `.env` and folders |
| `ui` | words and examples for the page: `container`, `app_tool`, `catalogue`, `name_required`, `volumes_label`, `run_hint`, `examples`, `app_example` |

`<action>_output` (see `records.py`): `format` jsonl (default), json
(`path`), tsv (`columns`), lines, text; `fields` maps the module's names to
templates over the command's own (`"{Name}"`), `values` translates values,
`defaults` fills empty fields, `required` drops records without them.
Docker and buildah print the module's names directly through Go templates;
AppJail's tab-separated listing is mapped. `tests/test_profiles.py` formats
every command of every profile with the real engine.

## Tests

```sh
python3 -m pytest tests
```

The tests load `freedomnas-core` and `freedomnas-profiles` from sibling
checkouts (`../freedomnas-core`, `../freedomnas-profiles`), never from an
installed FreedomNAS.

## Packaging

The package needs freedomnas-core and freedomnas-webui newer than 0.9.12:
0.9.12's module loader reads only one modules directory, so a community
module installed beside it hides the core modules, and its WebUI serves no
module's static files.

`debian/` and `PKGBUILD` install `modules/containers/` to
`/usr/lib/freedomnas/modules/containers/` and `profiles/containers/` to
`/usr/lib/freedomnas/profiles/containers/`. Void, Gentoo and FreeBSD
packages are still to do.
