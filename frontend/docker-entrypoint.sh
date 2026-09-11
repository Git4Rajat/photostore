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
PROCESSING_MODE="${APP_CONFIG_PROCESSING_MODE:-browser}"

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
  buildTimestamp: "${BUILD_TS}",
  processingMode: "${PROCESSING_MODE}"
};
EOF

# connect-src needs the real per-deployment backend origin -- API_BASE_URL
# varies by environment (prod vs. test sandbox both use random Container Apps
# FQDNs), so this is regenerated here rather than baked in at image build
# time, same reasoning as env.js above. See csp.conf for the directives this
# was verified against in a real browser (and for why script-src also needs
# unpkg.com, and connect-src needs the data: scheme --
# tesseract-core.wasm.js fetches its embedded wasm binary as a data: URI --
# added alongside this).
#
# huggingface.co + *.hf.co: the manifest sets allowLocalModels: false for the
# CLIP model (unlike AdaFace/YOLO, it isn't bundled under
# /models/browser-ai/models/), so @xenova/transformers fetches it live from
# HF's default remoteHost (huggingface.co) every time the Tools page's image
# tagging/search warms up. Without both origins here that fetch is
# CSP-blocked outright (confirmed via a live browser console capture). The
# small JSON config/tokenizer files redirect same-origin
# (huggingface.co/api/resolve-cache/...), but the large .onnx weight file
# redirects off to HF's CDN on a *.hf.co subdomain (e.g. us.aws.cdn.hf.co) --
# confirmed via `curl -I .../resolve/main/model_quantized.onnx` -- hence the
# wildcard rather than just huggingface.co.
CONNECT_SRC="'self' https://*.blob.core.windows.net https://unpkg.com https://tessdata.projectnaptha.com https://huggingface.co https://*.hf.co data:"
if [ -n "$API_BASE_URL" ]; then
  CONNECT_SRC="'self' ${API_BASE_URL} https://*.blob.core.windows.net https://unpkg.com https://tessdata.projectnaptha.com https://huggingface.co https://*.hf.co data:"
fi

cat > /etc/nginx/csp.conf <<EOF
add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' https://unpkg.com; worker-src 'self' blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https://*.blob.core.windows.net; media-src 'self' blob: https://*.blob.core.windows.net; connect-src ${CONNECT_SRC}; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'" always;
EOF
