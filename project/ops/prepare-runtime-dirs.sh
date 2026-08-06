#!/bin/sh
set -eu

RUNTIME_UID=10001
RUNTIME_GID=10001
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
project_root="$(CDPATH= cd -- "$script_dir/.." && pwd -P)"

if [ "$(id -u)" -ne 0 ]; then
  echo "prepare-runtime-dirs.sh must run as root" >&2
  exit 1
fi
if [ ! -f "$project_root/docker-compose.yml" ]; then
  echo "project root validation failed" >&2
  exit 1
fi

for relative in llm/prompts logs; do
  target="$project_root/$relative"
  install -d -m 0770 "$target"
  resolved="$(CDPATH= cd -- "$target" && pwd -P)"
  case "$resolved" in
    "$project_root"/*) ;;
    *) echo "runtime directory escaped project root" >&2; exit 1 ;;
  esac
  chown "$RUNTIME_UID:$RUNTIME_GID" "$resolved"
  chmod 0770 "$resolved"
done
