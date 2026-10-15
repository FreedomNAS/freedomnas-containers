# freedomnas-containers

FreedomNAS community module for containers, built from
[freedomnas-module-template](https://codeberg.org/FreedomNAS/freedomnas-module-template).
Package: `freedomnas-module-containers`. User guide: *Containers* in the
FreedomNAS documentation.

Docker runs from the edition's own packages, on every Linux edition
(Debian, Arch, Artix, Void, Gentoo). On FreeBSD the runtime is AppJail
(`backend: appjail` in containers-freebsd.json, appjail_runtime.py): the
runtime and its datasets are done; jails, apps and the catalogue follow.

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
  against what listens on the NAS (`ss`), what containers publish and the
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
├── runtime.py           the runtime: install, datasets, start after the pool, reconcile
├── docker.py            containers and images: list, actions, logs, pull, run, input checks
├── apps.py              Compose apps on their own datasets: create, edit, up/down, update, remove, reconcile
├── catalogue.py         ready-made apps: defaults, checks, .env, folders
├── catalogue/<id>/      app.json + compose.yaml for each ready-made app
├── api_routes.py        /api/containers/... (install, setup, pull and run run as jobs, one at a time)
└── frontend/            containers.html, js/containers.js, icons.svg
profiles/containers/
└── containers-<os>.json runtime_check, info, data_dirs, data_move, data_copy,
                         socket_units (systemd), runtime_repository (Debian),
                         ps, start/stop/restart, remove(_force), logs, images,
                         image_pull/_remove/_prune, run (run_args is a multi-argument),
                         compose_check/_write/_up/_down/_pull/_logs, env_write,
                         folder_prepare, listening
tests/                   pytest against the freedomnas-core checkout beside this one
```

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
