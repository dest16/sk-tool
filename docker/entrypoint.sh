#!/bin/sh
set -eu

target_uid="${PUID:-99}"
target_gid="${PGID:-100}"

case "$target_uid" in
  ''|*[!0-9]*) echo "PUID 必须是数字：$target_uid" >&2; exit 64 ;;
esac
case "$target_gid" in
  ''|*[!0-9]*) echo "PGID 必须是数字：$target_gid" >&2; exit 64 ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  # Docker creates bind-mount source directories as root when they do not
  # exist. Only repair an empty directory; never recursively change ownership
  # of a user's existing library or download data.
  for mount_dir in /config /downloads /library; do
    mkdir -p "$mount_dir"
    if [ -z "$(find "$mount_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
      # Rootless/NFS-root-squash mounts may reject chown; let the explicit
      # writability check below report the actionable PUID/PGID guidance.
      chown "$target_uid:$target_gid" "$mount_dir" 2>/dev/null || true
    fi
  done

  if ! gosu "$target_uid:$target_gid" sh -c 'test -w /config && test -w /downloads && test -w /library'; then
    echo "挂载目录不可写：请将 /config、/downloads、/library 授权给 ${target_uid}:${target_gid}（PUID/PGID）" >&2
    exit 73
  fi
  exec gosu "$target_uid:$target_gid" "$@"
fi

# An explicit docker --user override remains supported, but cannot repair a
# root-owned bind mount because this process is intentionally not privileged.
exec "$@"

