#!/usr/bin/env bash
#
# Photostore — enable sign-in (Microsoft Entra) for an already-deployed app.
#
# WHAT THIS DOES
#   The one-click "Deploy to Azure" button provisions Photostore with sign-in
#   turned OFF (it can't create Entra app registrations itself — that needs a
#   Microsoft Graph token the deployment doesn't have). This script does that
#   second half: it creates the two app registrations, grants admin consent,
#   and switches the running app over to requiring login — all from a single
#   paste into Azure Cloud Shell.
#
# HOW TO RUN (no install required)
#   1. Open https://portal.azure.com and click the Cloud Shell icon ( >_ ) at
#      the top. Choose "Bash" if prompted.
#   2. Paste ONE of the following:
#
#        curl -sSL https://raw.githubusercontent.com/Git4Rajat/photostore/main/deploy/setup-auth.sh -o setup-auth.sh
#        bash setup-auth.sh
#
#      The script finds your Photostore deployment automatically. If you have
#      more than one, it will ask which resource group to use, or pass it:
#
#        bash setup-auth.sh --resource-group photostore-rg
#
# REQUIREMENTS
#   * You must be able to grant admin consent in your directory (you are, if
#     you created the Azure subscription — you're the tenant admin). If not,
#     ask whoever administers your organisation's Microsoft account to run it.
#
# SAFE TO RE-RUN
#   Every step checks for existing objects and reuses them, so running the
#   script twice will not create duplicates or break a working setup.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Configuration / defaults
# ---------------------------------------------------------------------------
APP_NAME="photostore"
RESOURCE_GROUP=""
SUBSCRIPTION=""
ASSUME_YES="false"

GRAPH="https://graph.microsoft.com/v1.0"
SCOPE_VALUE="API.Writer"

# ---------------------------------------------------------------------------
# Pretty logging (degrades gracefully when not a terminal)
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""
fi
log()   { printf '%s\n' "${C_BLUE}▸${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN}✓${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}!${C_RESET} $*" >&2; }
die()   { printf '%s\n' "${C_RED}✗ $*${C_RESET}" >&2; exit 1; }
step()  { printf '\n%s\n' "${C_BOLD}$*${C_RESET}"; }

trap 'die "Failed at line $LINENO. Nothing above this point was left half-done that a re-run will not fix — you can safely run the script again."' ERR

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Enable Microsoft Entra sign-in for a deployed Photostore instance.

Usage: bash setup-auth.sh [options]

Options:
  -g, --resource-group <name>   Resource group of the deployment (auto-detected if omitted)
  -n, --app-name <name>         App name prefix used at deploy time (default: photostore)
  -s, --subscription <id|name>  Azure subscription to use (default: current)
  -y, --yes                     Do not prompt for confirmation
  -h, --help                    Show this help
EOF
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not found. Run this in Azure Cloud Shell, which has it preinstalled."; }

new_uuid() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import uuid; print(uuid.uuid4())'
  elif [[ -r /proc/sys/kernel/random/uuid ]]; then
    cat /proc/sys/kernel/random/uuid
  else
    die "Cannot generate a UUID (no python3 and no /proc uuid source)."
  fi
}

# retry <attempts> <sleep-seconds> <command...>
# Directory writes (new app registrations, service principals, consent) are not
# instantly consistent, so a step can fail simply because the object it needs
# has not replicated yet. Retrying with backoff makes the script resilient.
retry() {
  local attempts="$1" sleep_s="$2"; shift 2
  local n=1
  until "$@"; do
    if (( n >= attempts )); then
      return 1
    fi
    warn "attempt $n/$attempts failed; retrying in ${sleep_s}s…"
    sleep "$sleep_s"
    ((n++))
  done
}

confirm() {
  [[ "$ASSUME_YES" == "true" ]] && return 0
  local reply
  read -r -p "$1 [y/N] " reply || true
  [[ "$reply" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) RESOURCE_GROUP="${2:-}"; shift 2 ;;
    -n|--app-name)       APP_NAME="${2:-}"; shift 2 ;;
    -s|--subscription)   SUBSCRIPTION="${2:-}"; shift 2 ;;
    -y|--yes)            ASSUME_YES="true"; shift ;;
    -h|--help)           usage; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Checking prerequisites"
require_cmd az

if ! az account show >/dev/null 2>&1; then
  die "You are not signed in to Azure. In Cloud Shell this is automatic; if running locally, run 'az login' first."
fi

if [[ -n "$SUBSCRIPTION" ]]; then
  az account set --subscription "$SUBSCRIPTION" || die "Could not select subscription '$SUBSCRIPTION'."
fi

TENANT_ID="$(az account show --query tenantId -o tsv)"
SUB_NAME="$(az account show --query name -o tsv)"
ok "Signed in — subscription: ${C_BOLD}${SUB_NAME}${C_RESET}, tenant: ${TENANT_ID}"

# ---------------------------------------------------------------------------
# Locate the deployment
# ---------------------------------------------------------------------------
step "Locating your Photostore deployment"

FRONTEND_APP="${APP_NAME}-frontend"
BACKEND_APP="${APP_NAME}-backend"

if [[ -z "$RESOURCE_GROUP" ]]; then
  log "Searching for a container app named '${FRONTEND_APP}'…"
  mapfile -t RGS < <(az containerapp list --query "[?name=='${FRONTEND_APP}'].resourceGroup" -o tsv | sort -u)
  if (( ${#RGS[@]} == 0 )); then
    die "No container app named '${FRONTEND_APP}' found in this subscription. Pass --resource-group, or --app-name if you used a different name at deploy time."
  elif (( ${#RGS[@]} == 1 )); then
    RESOURCE_GROUP="${RGS[0]}"
    ok "Found deployment in resource group: ${C_BOLD}${RESOURCE_GROUP}${C_RESET}"
  else
    echo "Multiple deployments found:"
    i=1
    for rg in "${RGS[@]}"; do echo "  $i) $rg"; ((i++)); done
    read -r -p "Which resource group? [1-${#RGS[@]}] " choice
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#RGS[@]} )) || die "Invalid selection."
    RESOURCE_GROUP="${RGS[$((choice-1))]}"
  fi
fi

# Resolve the public URLs (needed for the SPA redirect URI).
FRONTEND_FQDN="$(az containerapp show -g "$RESOURCE_GROUP" -n "$FRONTEND_APP" \
  --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"
BACKEND_FQDN="$(az containerapp show -g "$RESOURCE_GROUP" -n "$BACKEND_APP" \
  --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"
[[ -n "$FRONTEND_FQDN" ]] || die "Could not find frontend app '${FRONTEND_APP}' in '${RESOURCE_GROUP}'."
[[ -n "$BACKEND_FQDN"  ]] || die "Could not find backend app '${BACKEND_APP}' in '${RESOURCE_GROUP}'."

FRONTEND_URL="https://${FRONTEND_FQDN}"
ok "App URL: ${C_BOLD}${FRONTEND_URL}${C_RESET}"

echo
echo "About to configure Microsoft sign-in for the deployment above:"
echo "  • create/reuse two app registrations in tenant ${TENANT_ID}"
echo "  • grant admin consent so the app can sign users in"
echo "  • turn on required login for '${FRONTEND_APP}' and '${BACKEND_APP}'"
confirm "Proceed?" || die "Cancelled."

# ---------------------------------------------------------------------------
# Backend API app registration (exposes the API.Writer scope)
# ---------------------------------------------------------------------------
step "Backend API app registration"
BACKEND_DISPLAY="${APP_NAME}-backend-api"

BACKEND_APP_ID="$(az ad app list --display-name "$BACKEND_DISPLAY" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "$BACKEND_APP_ID" ]]; then
  log "Creating '${BACKEND_DISPLAY}'…"
  BACKEND_APP_ID="$(az ad app create --display-name "$BACKEND_DISPLAY" \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  ok "Created (appId ${BACKEND_APP_ID})"
else
  ok "Reusing existing registration (appId ${BACKEND_APP_ID})"
fi

# Object id is required for Graph PATCH calls.
BACKEND_OBJ_ID="$(retry 10 3 az ad app show --id "$BACKEND_APP_ID" --query id -o tsv)"

# Ensure the identifier URI and the API.Writer scope exist (idempotent).
SCOPE_ID="$(az ad app show --id "$BACKEND_APP_ID" \
  --query "api.oauth2PermissionScopes[?value=='${SCOPE_VALUE}'].id | [0]" -o tsv 2>/dev/null || true)"
if [[ -z "$SCOPE_ID" ]]; then
  SCOPE_ID="$(new_uuid)"
  log "Exposing API scope '${SCOPE_VALUE}'…"
  BODY="$(cat <<JSON
{
  "identifierUris": ["api://${BACKEND_APP_ID}"],
  "api": {
    "oauth2PermissionScopes": [
      {
        "id": "${SCOPE_ID}",
        "value": "${SCOPE_VALUE}",
        "type": "User",
        "isEnabled": true,
        "adminConsentDisplayName": "Access the Photostore API",
        "adminConsentDescription": "Allow the app to access the Photostore API on behalf of the signed-in user.",
        "userConsentDisplayName": "Access the Photostore API",
        "userConsentDescription": "Allow the app to access the Photostore API on your behalf."
      }
    ]
  }
}
JSON
)"
  retry 5 3 az rest --method PATCH --url "${GRAPH}/applications/${BACKEND_OBJ_ID}" \
    --headers "Content-Type=application/json" --body "$BODY"
  ok "Scope '${SCOPE_VALUE}' exposed"
else
  ok "Scope '${SCOPE_VALUE}' already present"
fi

# Service principal for the backend (needed so the grant can reference it).
if ! az ad sp show --id "$BACKEND_APP_ID" >/dev/null 2>&1; then
  log "Creating backend service principal…"
  retry 5 3 az ad sp create --id "$BACKEND_APP_ID" >/dev/null
fi
ok "Backend service principal ready"

# ---------------------------------------------------------------------------
# Frontend SPA app registration (redirect URI + permission to the backend)
# ---------------------------------------------------------------------------
step "Frontend sign-in app registration"
FRONTEND_DISPLAY="${APP_NAME}-frontend-spa"

FRONTEND_APP_ID="$(az ad app list --display-name "$FRONTEND_DISPLAY" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "$FRONTEND_APP_ID" ]]; then
  log "Creating '${FRONTEND_DISPLAY}'…"
  FRONTEND_APP_ID="$(az ad app create --display-name "$FRONTEND_DISPLAY" \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  ok "Created (appId ${FRONTEND_APP_ID})"
else
  ok "Reusing existing registration (appId ${FRONTEND_APP_ID})"
fi

FRONTEND_OBJ_ID="$(retry 10 3 az ad app show --id "$FRONTEND_APP_ID" --query id -o tsv)"

# Set the SPA redirect URI to this deployment's URL and request the backend scope.
log "Setting redirect URI to ${FRONTEND_URL} and linking the API permission…"
BODY="$(cat <<JSON
{
  "spa": { "redirectUris": ["${FRONTEND_URL}"] },
  "requiredResourceAccess": [
    {
      "resourceAppId": "${BACKEND_APP_ID}",
      "resourceAccess": [ { "id": "${SCOPE_ID}", "type": "Scope" } ]
    }
  ]
}
JSON
)"
retry 5 3 az rest --method PATCH --url "${GRAPH}/applications/${FRONTEND_OBJ_ID}" \
  --headers "Content-Type=application/json" --body "$BODY"
ok "Redirect URI and API permission configured"

# Service principal for the frontend.
if ! az ad sp show --id "$FRONTEND_APP_ID" >/dev/null 2>&1; then
  log "Creating frontend service principal…"
  retry 5 3 az ad sp create --id "$FRONTEND_APP_ID" >/dev/null
fi
ok "Frontend service principal ready"

# ---------------------------------------------------------------------------
# Admin consent (this is the privileged step)
# ---------------------------------------------------------------------------
step "Granting admin consent"
log "Consenting the frontend→backend permission for the whole tenant…"
if retry 8 5 az ad app permission admin-consent --id "$FRONTEND_APP_ID"; then
  ok "Admin consent granted"
else
  warn "Automatic admin consent did not complete."
  warn "Open the Azure portal → Microsoft Entra ID → App registrations → '${FRONTEND_DISPLAY}'"
  warn "→ API permissions → 'Grant admin consent', then re-run this script."
  die  "Cannot finish without admin consent."
fi

# ---------------------------------------------------------------------------
# Point the running app at the new registrations and require login
# ---------------------------------------------------------------------------
step "Turning on sign-in for the running app"
API_SCOPE="${BACKEND_APP_ID}/.default"

log "Updating backend '${BACKEND_APP}'…"
az containerapp update -g "$RESOURCE_GROUP" -n "$BACKEND_APP" --set-env-vars \
  "AUTH_REQUIRED=true" \
  "AUTH_MODE=entra" \
  "AZURE_AD_TENANT_ID=${TENANT_ID}" \
  "AZURE_AD_CLIENT_ID=${BACKEND_APP_ID}" \
  "AZURE_AD_API_AUDIENCE=${BACKEND_APP_ID}" >/dev/null
ok "Backend updated"

log "Updating frontend '${FRONTEND_APP}'…"
az containerapp update -g "$RESOURCE_GROUP" -n "$FRONTEND_APP" --set-env-vars \
  "APP_CONFIG_AUTH_MODE=entra" \
  "APP_CONFIG_AZURE_AD_TENANT_ID=${TENANT_ID}" \
  "APP_CONFIG_AZURE_AD_CLIENT_ID=${FRONTEND_APP_ID}" \
  "APP_CONFIG_AZURE_AD_API_SCOPE=${API_SCOPE}" >/dev/null
ok "Frontend updated"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step "${C_GREEN}Sign-in is now enabled 🎉${C_RESET}"
cat <<EOF

  Open your Photostore:   ${C_BOLD}${FRONTEND_URL}${C_RESET}

  You will be asked to sign in with the Microsoft account in tenant
  ${TENANT_ID}. The two app registrations created for you:

    • ${BACKEND_DISPLAY}   (API)      appId ${BACKEND_APP_ID}
    • ${FRONTEND_DISPLAY}  (sign-in)  appId ${FRONTEND_APP_ID}

  It can take a minute for the new revisions to roll out. If the first
  sign-in shows a redirect error, wait ~60s and refresh.

  Re-running this script is safe and will not create duplicates.
EOF
