# freedomnas-containers

FreedomNAS community module for containers, built from
[freedomnas-module-template](https://codeberg.org/FreedomNAS/freedomnas-module-template).
Package: `freedomnas-module-containers`. User guide: *Containers* in the
FreedomNAS documentation.

Docker runs from the edition's own packages, on every Linux edition
(Debian, Arch, Artix, Void, Gentoo); FreeBSD (Podman, then jails) comes
later.

## What it does

- **Install Docker** from the WebUI: on Debian from Docker's APT repository
  (`runtime_repository` in the profile), then the packages and services of
  freedomnas-profiles' `packages/containers.json` for the edition and init.
- **Put Docker's data on a pool**: every directory in the profile's
  `data_dirs` (`/var/lib/docker`, `/var/lib/containerd`) is mounted from a
  dataset `<pool>/containers/<name>` with `canmount=noauto`. Docker's own
  configuration and image store are left as they are. What a directory
  held is moved aside to `<dir>.pre-freedomnas` and copied onto the
  dataset; the old copy is never deleted.
- **Start Docker after the pool**: Docker, containerd and `docker.socket`
  are kept from starting at boot; `reconcile()` mounts the datasets and
  then starts them, at every boot and after a restore.
- **Containers and images**: list, start, stop, restart, logs and remove
  containers; download, remove and prune images; run a container from an
  image with ports, volumes, environment and a restart policy (Expert).
  Names, image references and every option are checked before they reach
  `docker`, and `run`'s options reach it as separate words.

Configuration, `modules.containers` in `config.yaml`:

```yaml
modules:
  containers:
    enabled: true
    pool: tank
    datasets:
      docker: tank/containers/docker
      containerd: tank/containers/containerd
```

## Layout

```
modules/containers/
├── manifest.json        id, supported_os, webui (page /containers/, sidebar entry)
├── module.py            ContainersModule: health(), reconcile()
├── runtime.py           the runtime: install, datasets, start after the pool, reconcile
├── docker.py            containers and images: list, actions, logs, pull, run, input checks
├── api_routes.py        /api/containers/... (install, setup, pull and run run as jobs, one at a time)
└── frontend/            containers.html, js/containers.js, icons.svg
profiles/containers/
└── containers-<os>.json runtime_check, info, data_dirs, data_move, data_copy,
                         socket_units (systemd), runtime_repository (Debian),
                         ps, start/stop/restart, remove(_force), logs, images,
                         image_pull/_remove/_prune, run (run_args is a multi-argument)
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
