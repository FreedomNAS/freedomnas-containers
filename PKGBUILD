# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-module-containers
pkgver=0.1.0
pkgrel=1
pkgdesc="Containers for FreedomNAS: Docker on its own ZFS datasets"
arch=('any')
url="https://codeberg.org/FreedomNAS/freedomnas-containers"
license=('MPL-2.0')
depends=('freedomnas-core' 'freedomnas-webui')
source=("git+https://codeberg.org/FreedomNAS/freedomnas-containers.git#tag=v${pkgver}")
sha256sums=('SKIP')

package() {
    cd "${srcdir}/freedomnas-containers"

    mkdir -p "${pkgdir}/usr/lib/freedomnas/modules"
    mkdir -p "${pkgdir}/usr/lib/freedomnas/profiles"
    cp -r modules/containers  "${pkgdir}/usr/lib/freedomnas/modules/containers"
    cp -r profiles/containers "${pkgdir}/usr/lib/freedomnas/profiles/containers"

    if [ -f LICENSE ]; then install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"; fi
    if [ -f README.md ]; then install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"; fi
}
