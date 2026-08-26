#!/bin/sh
# Railway (and most platforms) mount volumes owned by root, which replaces the
# directory the image prepared and leaves a non-root process unable to write.
# Fix ownership while we still have root, then drop privileges to run the app.
set -e

DB_DIR=$(dirname "${QC_DB_PATH:-/app/qc.db}")

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DB_DIR"
    chown -R appuser:appuser "$DB_DIR" 2>/dev/null || true

    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
    fi
    exec su appuser -s /bin/sh -c 'exec "$0" "$@"' -- "$@"
fi

exec "$@"
