#!/bin/sh
set -eu

# WHY: archive extraction changes directory mtimes, but every archived datum must still match.

ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
VERIFIER="${ROOT}/scripts/deploy/verify-archive-restore.sh"
TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT HUP INT TERM

SOURCE="${TMP}/source"
RESTORE="${TMP}/restore"
mkdir -p "${SOURCE}/nested/child"
printf 'preserved content\n' > "${SOURCE}/nested/state"
chmod 640 "${SOURCE}/nested/state"
ln "${SOURCE}/nested/state" "${SOURCE}/nested/state-hardlink"
ln -s nested/state "${SOURCE}/state-link"
mkfifo "${SOURCE}/state-pipe"
mknod "${SOURCE}/state-device" c 1 3
touch -t 202601020304.05 "${SOURCE}/nested/state"

restore_source() {
  rm -rf "${RESTORE}"
  mkdir "${RESTORE}"
  (cd "${SOURCE}" && tar -cf - .) | (cd "${RESTORE}" && tar -xf -)
}

expect_rejected() {
  label=$1
  if sh "${VERIFIER}" "${SOURCE}" "${RESTORE}"; then
    echo "${label} was accepted" >&2
    exit 1
  fi
}

restore_source
touch -t 203001020304.05 "${RESTORE}" "${RESTORE}/nested" "${RESTORE}/nested/child"
sh "${VERIFIER}" "${SOURCE}" "${RESTORE}"

restore_source
printf 'changed content\n' > "${RESTORE}/nested/state"
expect_rejected 'content drift'

restore_source
touch -t 203101020304.05 "${RESTORE}/nested/state"
expect_rejected 'file mtime drift'

restore_source
chmod 600 "${RESTORE}/nested/state"
expect_rejected 'file mode drift'

restore_source
chown 123:456 "${RESTORE}/nested/state"
expect_rejected 'file ownership drift'

restore_source
rm "${RESTORE}/state-link"
ln -s nested/child "${RESTORE}/state-link"
expect_rejected 'symlink target drift'

restore_source
cp "${RESTORE}/nested/state-hardlink" "${RESTORE}/nested/state-copy"
rm "${RESTORE}/nested/state-hardlink"
mv "${RESTORE}/nested/state-copy" "${RESTORE}/nested/state-hardlink"
expect_rejected 'hardlink topology drift'

restore_source
rm "${RESTORE}/state-device"
mknod "${RESTORE}/state-device" c 1 5
expect_rejected 'device metadata drift'

restore_source
rm "${RESTORE}/state-pipe"
printf 'not a fifo\n' > "${RESTORE}/state-pipe"
expect_rejected 'entry type drift'

restore_source
printf 'extra\n' > "${RESTORE}/extra"
expect_rejected 'path inventory drift'

echo 'verify-archive-restore: OK'
