#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  printf '%s\n' 'usage: pin-staging-image-tag.sh ENV_FILE rc-COMMIT' >&2
  exit 2
fi

env_file=$1
image_tag=$2

case "$image_tag" in
  rc-*)
    hex=${image_tag#rc-}
    case "$hex" in
      ''|*[!0-9a-f]*)
        printf '%s\n' 'image tag must use lowercase hexadecimal commit' >&2
        exit 2
        ;;
    esac
    hex_length=${#hex}
    if [ "$hex_length" -lt 12 ] || [ "$hex_length" -gt 40 ]; then
      printf '%s\n' 'image tag commit must contain 12 to 40 hexadecimal characters' >&2
      exit 2
    fi
    ;;
  rollback-????????T??????Z)
    rollback_stamp=${image_tag#rollback-}
    case "$rollback_stamp" in
      *[!0-9TZ]*)
        printf '%s\n' 'rollback tag must use UTC timestamp' >&2
        exit 2
        ;;
    esac
    ;;
  *)
    printf '%s\n' 'image tag must use rc-COMMIT or rollback-UTC format' >&2
    exit 2
    ;;
esac
if [ ! -f "$env_file" ] || [ -L "$env_file" ]; then
  printf '%s\n' 'env file must be an existing regular non-symlink file' >&2
  exit 2
fi

env_dir=$(CDPATH= cd -- "$(dirname -- "$env_file")" && pwd -P)
umask 077
temporary=$(mktemp "$env_dir/.staging-image-tag.XXXXXX")
cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

awk -v image_tag="$image_tag" '
BEGIN { pinned = 0 }
/^STAGING_IMAGE_TAG=/ {
  if (!pinned) {
    print "STAGING_IMAGE_TAG=" image_tag
    pinned = 1
  }
  next
}
{ print }
END {
  if (!pinned) {
    print "STAGING_IMAGE_TAG=" image_tag
  }
}
' "$env_file" > "$temporary"

chmod --reference="$env_file" "$temporary"
if [ "$(id -u)" -eq 0 ]; then
  chown --reference="$env_file" "$temporary"
fi
mv -f -- "$temporary" "$env_file"
trap - EXIT HUP INT TERM

printf 'staging_image_tag_pinned=%s\n' "$image_tag"
