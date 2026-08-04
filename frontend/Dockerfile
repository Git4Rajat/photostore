# syntax=docker/dockerfile:1.7
FROM node:20-alpine AS build

# Keep CI/ACR logs focused on real build failures.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
	NPM_CONFIG_AUDIT=false \
	NPM_CONFIG_FUND=false \
	NPM_CONFIG_LOGLEVEL=error

WORKDIR /app

COPY package*.json ./
RUN npm ci --no-audit --no-fund

COPY . ./
RUN npm run sync:blazeface-model
RUN node scripts/sync-browser-ai-runtime.mjs
RUN chmod -R a+rX public/models
RUN test -f public/models/browser-ai/models/blazeface/model.json && \
    test -f public/models/browser-ai/models/blazeface/group1-shard1of1.bin && \
    test -f public/models/browser-ai/models/adaface/model.onnx && \
    test -f public/models/browser-ai/models/adaface/LICENSE && \
    test -f public/models/browser-ai/models/yolo-face/model.onnx
# Guard against the YOLOv8n-face model shipping as a Git LFS pointer (~130 B)
# instead of the real ~12 MB binary — same failure mode as ArcFace below.
RUN yolo=public/models/browser-ai/models/yolo-face/model.onnx; \
    bytes=$(wc -c < "$yolo"); \
    if [ "$bytes" -lt 1000000 ]; then \
        echo "ERROR: $yolo is $bytes bytes — looks like a Git LFS pointer, not the real model."; \
        head -c 200 "$yolo"; \
        exit 1; \
    fi
# Fail loudly if the AdaFace model is a Git LFS pointer (~130 B) rather than the
# real ~261 MB binary. Without this, a build where LFS did not materialize ships
# a pointer that nginx serves as HTTP 200, and ONNX Runtime fails to parse it
# ("model asset unavailable") with no network/console error to explain it.
RUN onnx=public/models/browser-ai/models/adaface/model.onnx; \
    bytes=$(wc -c < "$onnx"); \
    if [ "$bytes" -lt 1000000 ]; then \
        echo "ERROR: $onnx is $bytes bytes — looks like a Git LFS pointer, not the real model."; \
        echo "The checkout did not materialize LFS. Ensure 'git lfs pull' / checkout lfs:true succeeded."; \
        head -c 200 "$onnx"; \
        exit 1; \
    fi
# NOTE: this baked-in default is only what a plain `docker run` without
# APP_CONFIG_* env vars sees. The real deploy path (docker-entrypoint.sh, run
# at container start) regenerates this file from APP_CONFIG_ARC_FACE_MODEL_URL
# etc., which is what deploy/resources.bicep actually pins -- keep both in sync
# when the model path changes (a mismatch here is exactly how the AdaFace swap
# silently kept loading the old ArcFace model despite the code/version bump).
RUN node -e 'const fs=require("fs"); const ts=new Date().toISOString(); const content = `window.__APP_CONFIG__ = { apiBaseUrl: "", uploadBaseUrl: "", spaBaseUrl: "", azureAdTenantId: "", azureAdClientId: "", azureAdApiScope: "", authMode: "entra", blazeFaceModelUrl: "/models/browser-ai/models/blazeface/model.json", arcFaceModelUrl: "/models/browser-ai/models/adaface/model.onnx", arcFaceWasmPath: "/models/browser-ai/runtime/", buildTimestamp: "${ts}" };`; try{fs.writeFileSync("public/env.js", content)}catch(e){console.error("failed to write public/env.js",e); process.exit(1);} '
RUN npm run build
RUN chmod -R a+rX build/models

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.d/40-generate-env.sh
RUN chmod +x /docker-entrypoint.d/40-generate-env.sh

COPY --from=build /app/build /usr/share/nginx/html
RUN chmod -R a+rX /usr/share/nginx/html/models

EXPOSE 80
