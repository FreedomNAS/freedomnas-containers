#!/bin/sh
# FreedomNAS: every group share mounted under /shares becomes an External storage of the same name.
set -e
occ() { php /var/www/html/occ "$@"; }
occ app:enable files_external >/dev/null
occ config:system:set files_external_allow_create_new_local --value=true --type=boolean >/dev/null
existing=$(occ files_external:list --output=json)
for dir in /shares/*/; do
    [ -d "$dir" ] || continue
    name=$(basename "$dir")
    if printf '%s' "$existing" | grep -q -e "\"mount_point\":\"/$name\"" -e "\"mount_point\":\"\\\\/$name\""; then
        continue
    fi
    occ files_external:create "/$name" local null::null -c datadir="${dir%/}" >/dev/null
    echo "FreedomNAS: $name is an External storage"
done
