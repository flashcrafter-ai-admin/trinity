#!/bin/sh

# Compare an extracted archive with its source tree while ignoring only
# directory mtimes, which change as archive extraction creates children.

set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: verify-archive-restore.sh SOURCE_ROOT RESTORE_ROOT" >&2
  exit 2
fi

SOURCE_ROOT=$1
RESTORE_ROOT=$2

[ -d "${SOURCE_ROOT}" ] && [ ! -L "${SOURCE_ROOT}" ] \
  || { echo "source root is not a directory" >&2; exit 1; }
[ -d "${RESTORE_ROOT}" ] && [ ! -L "${RESTORE_ROOT}" ] \
  || { echo "restore root is not a directory" >&2; exit 1; }

STATE_ROOT=$(mktemp -d)
trap 'rm -rf "${STATE_ROOT}"' EXIT HUP INT TERM
export LC_ALL=C

canonicalize_tree() {
  root=$1
  state=$2
  mkdir -p "${state}/hardlinks"

  (
    cd "${root}"
    find . -print0 > "${state}/paths.unsorted"
  )
  sort -z "${state}/paths.unsorted" > "${state}/paths"

  (
    cd "${root}"
    while IFS= read -r -d '' path; do
      mode=$(stat -c '%f' "${path}")
      uid=$(stat -c '%u' "${path}")
      gid=$(stat -c '%g' "${path}")
      nlink=$(stat -c '%h' "${path}")
      size=$(stat -c '%s' "${path}")

      if [ -L "${path}" ]; then
        kind=symlink
      elif [ -d "${path}" ]; then
        kind=directory
      elif [ -f "${path}" ]; then
        kind=file
      elif [ -b "${path}" ]; then
        kind=block-device
      elif [ -c "${path}" ]; then
        kind=character-device
      elif [ -p "${path}" ]; then
        kind=fifo
      elif [ -S "${path}" ]; then
        kind=socket
      else
        echo "unsupported archive entry type" >&2
        exit 1
      fi

      printf 'entry\0%s\0%s\0%s\0%s\0%s\0%s\0' \
        "${path}" "${kind}" "${mode}" "${uid}" "${gid}" "${nlink}"

      if [ "${kind}" = directory ]; then
        printf 'directory-mtime-ignored\0directory-size-not-archived\0'
      else
        # POSIX tar stores mtimes at whole-second precision.
        mtime=$(stat -c '%Y' "${path}")
        printf '%s\0%s\0' "${mtime}" "${size}"
      fi

      case "${kind}" in
        file)
          content_line=$(sha256sum "${path}")
          printf 'content\0%s\0' "${content_line%% *}"
          ;;
        symlink)
          printf 'target\0'
          readlink -n "${path}"
          printf '\0'
          ;;
        block-device|character-device)
          printf 'device\0%s:%s\0' \
            "$(stat -c '%t' "${path}")" "$(stat -c '%T' "${path}")"
          ;;
        *)
          printf 'payload-none\0'
          ;;
      esac

      if [ "${kind}" != directory ] && [ "${nlink}" -gt 1 ]; then
        inode_key=$(stat -c '%d:%i' "${path}")
        key_line=$(printf '%s' "${inode_key}" | sha256sum)
        group_file="${state}/hardlinks/${key_line%% *}"
        if [ ! -f "${group_file}" ]; then
          printf '%s' "${path}" > "${group_file}"
        fi
        printf 'hardlink-group\0'
        cat "${group_file}"
        printf '\0'
      else
        printf 'hardlink-none\0'
      fi
    done < "${state}/paths"
  ) > "${state}/manifest"
}

canonicalize_tree "${SOURCE_ROOT}" "${STATE_ROOT}/source"
canonicalize_tree "${RESTORE_ROOT}" "${STATE_ROOT}/restore"
cmp -s "${STATE_ROOT}/source/manifest" "${STATE_ROOT}/restore/manifest"
