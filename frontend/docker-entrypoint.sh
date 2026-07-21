#!/bin/sh
set -eu

API_BASE_URL="${APP_CONFIG_API_BASE_URL:-}"
UPLOAD_BASE_URL="${APP_CONFIG_UPLOAD_BASE_URL:-$API_BASE_URL}"
SPA_BASE_URL="${APP_CONFIG_SPA_BASE_URL:-}"
AZURE_AD_TENANT_ID="${APP_CONFIG_AZURE_AD_TENANT_ID:-}"
AZURE_AD_CLIENT_ID="${APP_CONFIG_AZURE_AD_CLIENT_ID:-}"
AZURE_AD_API_SCOPE="${APP_CONFIG_AZURE_AD_API_SCOPE:-}"
AUTH_MODE="${APP_CONFIG_AUTH_MODE:-entra}"
BLAZE_FACE_MODEL_URL="${APP_CONFIG_BLAZE_FACE_MODEL_URL:-/models/browser-ai/models/blazeface/model.json}"
ARC_FACE_MODEL_URL="${APP_CONFIG_ARC_FACE_MODEL_URL:-/models/browser-ai/models/arcface/model.onnx}"
ARC_FACE_WASM_PATH="${APP_CONFIG_ARC_FACE_WASM_PATH:-/models/browser-ai/runtime/}"

# Allow an explicit build timestamp override via APP_CONFIG_BUILD_TIMESTAMP.
# If not provided, try to preserve the build-time value embedded in the image's
# prebuilt env.js (written at image build time) so we can show when the app was built.
BUILD_TS="${APP_CONFIG_BUILD_TIMESTAMP:-}"

if [ -z "$BUILD_TS" ]; then
  if [ -f /usr/share/nginx/html/env.js ]; then
    BUILD_TS=$(grep -oE 'buildTimestamp: *"[^"]*"' /usr/share/nginx/html/env.js | head -n1 | sed -E 's/.*"([^"]*)"/\1/') || true
  fi
fi

cat > /usr/share/nginx/html/env.js <<EOF
window.__APP_CONFIG__ = {
	  apiBaseUrl: "${API_BASE_URL}",
	  uploadBaseUrl: "${UPLOAD_BASE_URL}",
	  spaBaseUrl: "${SPA_BASE_URL}",
  azureAdTenantId: "${AZURE_AD_TENANT_ID}",
  azureAdClientId: "${AZURE_AD_CLIENT_ID}",
  azureAdApiScope: "${AZURE_AD_API_SCOPE}",
  authMode: "${AUTH_MODE}",
  blazeFaceModelUrl: "${BLAZE_FACE_MODEL_URL}",
  arcFaceModelUrl: "${ARC_FACE_MODEL_URL}",
  arcFaceWasmPath: "${ARC_FACE_WASM_PATH}",
  buildTimestamp: "${BUILD_TS}"
};
EOF
