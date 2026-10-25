#!/bin/sh
# FreedomNAS: data that moved from another edition keeps that edition's paths and database host in config.php; this edition's go there.
config=/usr/local/www/html/config/config.php
[ -f "$config" ] || exit 0
php -r '
$file = $argv[1];
$html = $argv[2];
include $file;
if (!isset($CONFIG) || !is_array($CONFIG)) {
    exit(0);
}
$want = $CONFIG;
$want["datadirectory"] = getenv("NEXTCLOUD_DATA_DIR") ?: "$html/data";
if (getenv("MYSQL_HOST")) {
    $want["dbhost"] = getenv("MYSQL_HOST");
}
foreach ($want["apps_paths"] ?? [] as $i => $entry) {
    $want["apps_paths"][$i]["path"] = "$html/" . basename($entry["path"]);
}
if ($want != $CONFIG) {
    file_put_contents($file, "<?php\n\$CONFIG = " . var_export($want, true) . ";\n");
    echo "FreedomNAS: config.php now uses this edition'"'"'s paths\n";
}
' "$config" /usr/local/www/html || true
exit 0
