#!/usr/bin/env bash
# One-time setup for shared-album snapshots (see backend/share_snapshot.py).
#
# A shared link lands on scale-to-zero containers, which can leave a blank page for ~30s. With a
# snapshot the link opens a small page served by the storage account's static website instead --
# always on, no container -- which shows the album's thumbnails and previews while the app wakes.
#
# The static website is a data-plane setting that Bicep cannot switch on, so it is done here once:
#   1. enable the static website on the storage account (creates the $web container)
#   2. redeploy with enableShareSnapshot=true so the apps get SHARE_SNAPSHOT_WEB_URL
#
# The account keeps allowBlobPublicAccess=false: the static-website endpoint serves $web anonymously
# on its own, and the app's other containers stay private.
#
# Usage: deploy/enable-share-snapshot.sh <resource-group> <storage-account>
# Needs permission to change the account's blob service properties (e.g. Storage Account Contributor).
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <resource-group> <storage-account>" >&2
  exit 2
fi
rg="$1"
account="$2"

az storage blob service-properties update \
  --account-name "$account" --auth-mode login \
  --static-website --index-document index.html --404-document 404.html >/dev/null

endpoint="$(az storage account show -g "$rg" -n "$account" --query primaryEndpoints.web -o tsv)"
echo "Static website enabled: ${endpoint}"
echo "Next: redeploy with enableShareSnapshot=true (the apps read this origin as SHARE_SNAPSHOT_WEB_URL)."
