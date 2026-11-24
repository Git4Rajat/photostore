FROM mirror.gcr.io/library/node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS build

# Keep CI/ACR logs focused on real build failures.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
	NPM_CONFIG_AUDIT=false \
	NPM_CONFIG_FUND=false \
	NPM_CONFIG_LOGLEVEL=error

WORKDIR /app

COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# TEMPORARY (2026-08-12): the repo's Git LFS budget is exceeded, so CI
# checks these two files out with lfs:false (real bytes never materialize --
# only the ~130 B pointer stub does). Fetch the real binaries from a GitHub
# Release mirror instead. Downloaded here, ahead of `COPY . ./`, so the
# ~270 MB layer stays cached across source-only changes; copied over the
# pointer stubs below. Revert once the LFS budget resets: drop this apk add,
# the download RUN and the cp RUN below, and flip lfs:true back on in
# publish-images.yml. node:22-alpine doesn't ship curl, so install it first.
RUN apk add --no-cache curl
# Verify the downloaded weights against pinned digests (fail the build on a
# swapped or corrupted release asset). Update both when the weights change.
RUN mkdir -p /lfs-weights && cd /lfs-weights && \
    curl -fsSL -o adaface.onnx \
        https://github.com/Git4Rajat/photostore/releases/download/model-weights-lfs-workaround/adaface-model.onnx && \
    curl -fsSL -o yolo-face.onnx \
        https://github.com/Git4Rajat/photostore/releases/download/model-weights-lfs-workaround/yolo-face-model.onnx && \
    echo "5235574d2dbd891e12834070eec84b54ffe51197a329d4042ba552d1ba4e94d3  adaface.onnx" | sha256sum -c - && \
    echo "fd27189bfe5750a017648445700473459a6d02e7c3b0a3bfd8a54af77dd3b046  yolo-face.onnx" | sha256sum -c -

COPY . ./
RUN node scripts/sync-browser-ai-runtime.mjs
RUN cp /lfs-weights/adaface.onnx public/models/browser-ai/models/adaface/model.onnx && \
    cp /lfs-weights/yolo-face.onnx public/models/browser-ai/models/yolo-face/model.onnx
RUN chmod -R a+rX public/models
RUN test -f public/models/browser-ai/models/adaface/model.onnx && \
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
RUN node -e 'const fs=require("fs"); const ts=new Date().toISOString(); const content = `window.__APP_CONFIG__ = { apiBaseUrl: "", uploadBaseUrl: "", spaBaseUrl: "", azureAdTenantId: "", azureAdClientId: "", azureAdApiScope: "", authMode: "entra", arcFaceModelUrl: "/models/browser-ai/models/adaface/model.onnx", arcFaceWasmPath: "/models/browser-ai/runtime/", buildTimestamp: "${ts}" };`; try{fs.writeFileSync("public/env.js", content)}catch(e){console.error("failed to write public/env.js",e); process.exit(1);} '
RUN npm run build
RUN mkdir -p build/decoder-licenses && \
    cp node_modules/@colorhythm/libraw-wasm/LibRaw/* build/decoder-licenses/ && \
    cp node_modules/@colorhythm/libraw-wasm/LICENSE build/decoder-licenses/JS-LICENSE && \
    cp decoder-source.txt build/decoder-licenses/
RUN chmod -R a+rX build/models

FROM mirror.gcr.io/library/nginx:1.31-alpine@sha256:b50897d5c7b1aa08067c3d90c5e8b3e47ec4e3c91c91446f67820228f7b41e34

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY csp.conf /etc/nginx/csp.conf
COPY docker-entrypoint.sh /docker-entrypoint.d/40-generate-env.sh
RUN chmod +x /docker-entrypoint.d/40-generate-env.sh

COPY --from=build /app/build /usr/share/nginx/html
RUN chmod -R a+rX /usr/share/nginx/html/models

EXPOSE 80
