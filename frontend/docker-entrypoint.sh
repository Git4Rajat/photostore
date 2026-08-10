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
ARC_FACE_MODEL_URL="${APP_CONFIG_ARC_FACE_MODEL_URL:-/models/browser-ai/models/adaface/model.onnx}"
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

# connect-src needs the real per-deployment backend origin -- API_BASE_URL
# varies by environment (prod vs. test sandbox both use random Container Apps
# FQDNs), so this is regenerated here rather than baked in at image build
# time, same reasoning as env.js above. See csp.conf for the directives this
# was verified against in a real browser.
CONNECT_SRC="'self' https://*.blob.core.windows.net https://unpkg.com https://tessdata.projectnaptha.com"
if [ -n "$API_BASE_URL" ]; then
  CONNECT_SRC="'self' ${API_BASE_URL} https://*.blob.core.windows.net https://unpkg.com https://tessdata.projectnaptha.com"
fi

cat > /etc/nginx/csp.conf <<EOF
add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https://*.blob.core.windows.net; connect-src ${CONNECT_SRC}; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'" always;
EOF
