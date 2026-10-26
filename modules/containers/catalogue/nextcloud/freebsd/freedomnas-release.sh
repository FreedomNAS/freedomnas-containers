#!/bin/sh
# FreedomNAS: the jail's entrypoint. It brings /nextcloud.src to the release FreedomNAS's catalogue names (NEXTCLOUD_RELEASE, the same as Linux), from nextcloud.com instead of the ports, then hands over to the image's own entrypoint.
set -eu
version=$(printenv NEXTCLOUD_RELEASE || true)
if [ -n "$version" ] && ! grep -q "OC_VersionString = '$version'" /nextcloud.src/version.php; then
    url="https://download.nextcloud.com/server/releases/nextcloud-$version.tar.bz2"
    rm -rf /tmp/nextcloud /tmp/nextcloud.tar.bz2 /tmp/nextcloud.tar.bz2.sha256
    fetch -q -o /tmp/nextcloud.tar.bz2 "$url"
    fetch -q -o /tmp/nextcloud.tar.bz2.sha256 "$url.sha256"
    grep -q "^$(sha256 -q /tmp/nextcloud.tar.bz2) " /tmp/nextcloud.tar.bz2.sha256
    mkdir /tmp/nextcloud
    tar -xjf /tmp/nextcloud.tar.bz2 -C /tmp/nextcloud
    cp /nextcloud.src/config/*.php /tmp/nextcloud/nextcloud/config/
    rm -rf /nextcloud.src
    mv /tmp/nextcloud/nextcloud /nextcloud.src
    mkdir -p /nextcloud.src/custom_apps
    chown www:www /nextcloud.src/custom_apps
    rm -rf /tmp/nextcloud /tmp/nextcloud.tar.bz2 /tmp/nextcloud.tar.bz2.sha256
    echo "FreedomNAS: Nextcloud $version"
fi
exec /entrypoint.sh "$@"
