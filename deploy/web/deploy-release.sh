#!/usr/bin/env bash
# Root-owned production deployment wrapper.
#
# This is the only command granted through sudoers to the unprivileged deploy
# account.  It validates the immutable input surface, reads the allowed image
# repository from a root-owned data file without sourcing shell code, then
# performs pull -> local tag -> guarded deploy.
set -euo pipefail

BASE=/opt/biodata-web
POLICY_FILE="$BASE/deploy-policy.conf"
DEPLOY_SCRIPT="$BASE/deploy.sh"
TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
IMAGE_RE='^[a-z0-9]+([._-][a-z0-9]+)*(\.[a-z0-9]+([._-][a-z0-9]+)*)+(/[a-z0-9]+([._-][a-z0-9]+)*)+$'

die() {
  printf '[deploy-wrapper] %s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 1 ] || die 'usage: deploy-release.sh <image-tag>'
[ "${EUID:-$(id -u)}" -eq 0 ] || die 'must run as root through the dedicated sudo rule'

tag="$1"
[[ "$tag" =~ $TAG_RE ]] || die 'invalid image tag'
[ -f "$POLICY_FILE" ] || die 'missing root-owned deploy-policy.conf'
[ -x "$DEPLOY_SCRIPT" ] || die 'missing executable deploy.sh'

# The policy file is data, never shell.  Exactly one non-empty
# REGISTRY_IMAGE=<host>/<namespace>/<repository> line is accepted.
registry_image=''
while IFS= read -r raw || [ -n "$raw" ]; do
  line="${raw%$'\r'}"
  case "$line" in
    ''|'#'*) continue ;;
    REGISTRY_IMAGE=*)
      [ -z "$registry_image" ] || die 'duplicate REGISTRY_IMAGE policy entry'
      registry_image="${line#REGISTRY_IMAGE=}"
      ;;
    *) die 'unknown deploy policy entry' ;;
  esac
done < "$POLICY_FILE"

[ -n "$registry_image" ] || die 'REGISTRY_IMAGE is missing from deploy policy'
[[ "$registry_image" =~ $IMAGE_RE ]] || die 'REGISTRY_IMAGE is not an allowed registry path'

remote_image="${registry_image}:${tag}"
local_image="biodata-web:${tag}"

printf '[deploy-wrapper] pulling approved image tag %s\n' "$tag"
/usr/bin/docker pull "$remote_image"
/usr/bin/docker tag "$remote_image" "$local_image"
exec "$DEPLOY_SCRIPT" "$tag"
