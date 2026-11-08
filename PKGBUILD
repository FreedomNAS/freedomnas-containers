# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgbase=freedomnas-module-containers
pkgname=(freedomnas-module-containers freedomnas-module-containers-openrc freedomnas-module-containers-dinit freedomnas-module-containers-runit)
pkgver=0.1.0
pkgrel=1
pkgdesc="Containers for FreedomNAS: Docker on its own ZFS datasets"
arch=('any')
url="https://codeberg.org/FreedomNAS/freedomnas-containers"
license=('MPL-2.0')
depends=('freedomnas-core>0.9.12' 'freedomnas-webui>0.9.12' 'docker' 'docker-compose' 'containerd')
source=("git+https://codeberg.org/FreedomNAS/freedomnas-containers.git#tag=v${pkgver}")
sha256sums=('SKIP')

_install() {
    cd "${srcdir}/freedomnas-containers"

    mkdir -p "${pkgdir}/usr/lib/freedomnas/modules"
    mkdir -p "${pkgdir}/usr/lib/freedomnas/profiles"
    cp -r modules/containers  "${pkgdir}/usr/lib/freedomnas/modules/containers"
    cp -r profiles/containers "${pkgdir}/usr/lib/freedomnas/profiles/containers"

    if [ -f LICENSE ]; then install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"; fi
    if [ -f README.md ]; then install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"; fi
}

package_freedomnas-module-containers() {
    _install
}

package_freedomnas-module-containers-openrc() {
    pkgdesc+=" (openrc services, Artix)"
    depends+=('docker-openrc' 'containerd-openrc')
    provides=("freedomnas-module-containers=${pkgver}")
    conflicts=(freedomnas-module-containers)
    _install
}

package_freedomnas-module-containers-dinit() {
    pkgdesc+=" (dinit services, Artix)"
    depends+=('docker-dinit')
    provides=("freedomnas-module-containers=${pkgver}")
    conflicts=(freedomnas-module-containers)
    _install
}

package_freedomnas-module-containers-runit() {
    pkgdesc+=" (runit services, Artix)"
    depends+=('docker-runit')
    provides=("freedomnas-module-containers=${pkgver}")
    conflicts=(freedomnas-module-containers)
    _install
}
