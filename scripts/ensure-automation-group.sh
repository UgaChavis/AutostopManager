#!/usr/bin/env bash
set -Eeuo pipefail

group_name="autostop-automation"
group_gid="10001"
getent_bin="/usr/bin/getent"
groupadd_bin="/usr/sbin/groupadd"
flock_bin="/usr/bin/flock"
lock_path="/run/autostop-automation-group.lock"

if [[ "${EUID}" -ne 0 ]]; then
  echo "automation_group_requires_root=true" >&2
  exit 1
fi
for required_binary in "${getent_bin}" "${groupadd_bin}" "${flock_bin}"; do
  if [[ ! -x "${required_binary}" ]]; then
    echo "automation_group_tooling_missing=true" >&2
    exit 1
  fi
done
if [[ -L "${lock_path}" || ( -e "${lock_path}" && ! -f "${lock_path}" ) ]]; then
  echo "automation_group_lock_invalid=true" >&2
  exit 1
fi
umask 077
exec 9>"${lock_path}"
"${flock_bin}" -x 9

lookup_group() {
  local key="$1"
  "${getent_bin}" group "${key}" 2>/dev/null
}

entry_matches() {
  local entry="$1"
  local entry_name entry_password entry_gid entry_members extra
  [[ -n "${entry}" && "${entry}" != *$'\n'* ]] || return 1
  IFS=: read -r entry_name entry_password entry_gid entry_members extra <<<"${entry}"
  [[ -z "${extra}" && "${entry_name}" == "${group_name}" && "${entry_gid}" == "${group_gid}" ]]
}

name_entry=""
name_status=0
if name_entry="$(lookup_group "${group_name}")"; then
  name_status=0
else
  name_status=$?
fi
gid_entry=""
gid_status=0
if gid_entry="$(lookup_group "${group_gid}")"; then
  gid_status=0
else
  gid_status=$?
fi

if [[ "${name_status}" -eq 0 ]] && ! entry_matches "${name_entry}"; then
  echo "automation_group_name_conflict=true" >&2
  exit 1
fi
if [[ "${gid_status}" -eq 0 ]] && ! entry_matches "${gid_entry}"; then
  echo "automation_group_gid_conflict=true" >&2
  exit 1
fi
if [[ "${name_status}" -eq 0 || "${gid_status}" -eq 0 ]]; then
  if [[ "${name_status}" -ne 0 || "${gid_status}" -ne 0 ]]; then
    echo "automation_group_lookup_inconsistent=true" >&2
    exit 1
  fi
  echo "automation_group_ready=true"
  exit 0
fi
if [[ "${name_status}" -ne 2 || "${gid_status}" -ne 2 ]]; then
  echo "automation_group_lookup_failed=true" >&2
  exit 1
fi

if ! "${groupadd_bin}" --system --gid "${group_gid}" "${group_name}"; then
  echo "automation_group_create_failed=true" >&2
  exit 1
fi
if ! name_entry="$(lookup_group "${group_name}")" \
  || ! gid_entry="$(lookup_group "${group_gid}")" \
  || ! entry_matches "${name_entry}" \
  || ! entry_matches "${gid_entry}"; then
  echo "automation_group_verification_failed=true" >&2
  exit 1
fi

echo "automation_group_ready=true"
