# syntax=docker/dockerfile:1.7
# Dockerfile for ipworker (Azure Container Apps): server-side mirror of the
# browser's OCR/face/vision/geo pipeline (see backend/ipwork_*.py).
#
# Separate image from backend/worker (which reuse backendImage, differing
# only by APP_ROLE) on purpose: this needs torch/open_clip/onnxruntime/
# opencv/mediapipe/tesseract-ocr -- multiple GB of extra weight that would
# slow every backend/worker cold start (both scale to zero and back up) if
# bundled into their shared image. See the ipworker plan for the full
# rationale.
#
# Build context is the REPO ROOT, not backend/ -- unlike backend/Dockerfile --
# because this also bundles the frontend's browser-ai model files so ipworker
# never depends on the frontend container being reachable at runtime:
#   docker build -f backend/ipworker.Dockerfile .
FROM python:3.11-slim

ENV PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# tesseract-ocr: OCR. libgl1/libglib2.0-0: opencv-python-headless and
# mediapipe both dlopen a handful of X11/GL shared libs at import time even
# in "headless" builds. libimage-exiftool-perl/libraw-bin: shared with
# backend's own EXIF/RAW extraction (exif_utils.py). curl: fetches the
# MediaPipe Face Landmarker task bundle below (no PyPI package ships it).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    libimage-exiftool-perl \
    libraw-bin \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements-ipworker.txt ./
RUN pip install -r requirements.txt -r requirements-ipworker.txt

# Runtime Python modules -- shared backend/*.py (app.py, storage_utils.py,
# exif_utils.py, maps_utils.py, vision_utils.py, ...) plus the ipworker-only
# ipwork_*.py step processors, all in the same directory/import namespace.
COPY backend/*.py ./
COPY backend/entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# Same .onnx weights the frontend ships (frontend/public/models/browser-ai),
# bundled at build time rather than fetched at runtime.
COPY frontend/public/models/browser-ai/models/yolo-face/model.onnx /app/models/yolo-face/model.onnx
COPY frontend/public/models/browser-ai/models/adaface/model.onnx /app/models/adaface/model.onnx
COPY frontend/public/models/browser-ai/vocab/tag-vocabulary.v1.json /app/vocab/tag-vocabulary.v1.json

# MediaPipe's Tasks API (mediapipe>=1.0 removed the old solutions.face_mesh,
# which bundled its own weights) needs this asset explicitly -- Google's
# official hosted bundle, not a project asset. See ipwork_face.py's
# FACE_LANDMARKER_MODEL_PATH comment for why this replaces face-api.js's
# 68-point model (no ONNX/Python equivalent of that one exists).
RUN curl -sL -o /app/models/face_landmarker.task \
    https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task \
    && test -s /app/models/face_landmarker.task

ENV FLASK_APP=app.py
ENV APP_ROLE=ipworker

CMD ["/app/entrypoint.sh"]
