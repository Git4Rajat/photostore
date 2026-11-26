"""Intra-op thread count for the vision models (PyTorch CLIP, ONNX CLIP towers, YOLO and AdaFace sessions).

IPWORK_MODEL_THREADS (default 1) sets it, read when a model or session is created, so a value can be tried on
a running environment by changing the variable alone. Left at 1 this is exactly the long-standing behaviour:
IPWORKER_CONCURRENCY source threads each run their model on one thread, which is what keeps the 2 vCPU from
being oversubscribed (see docs/ipworker-architecture.md, confirmed live 2026-08-27).

Raising it lets one model call use more than one core, but the source threads share those cores, and the
interpreter lock still serialises the Python between model calls: the gain is bounded by the share of a photo
spent inside native inference, and it can be a loss when IPWORKER_CONCURRENCY * threads exceeds the vCPUs.
Whether it helps is for the live environment to say; compare photos/min and vision_source_steps ms per photo.
The vision container also sets OMP_THREAD_LIMIT=1 and OPENBLAS_NUM_THREADS=1, which can cap OpenMP- and
BLAS-based native regions whatever this says; raise them with it when testing.

ONNX inter-op threads stay 1: these graphs are sequential.
"""
import logging
import os

DEFAULT = 1
MAXIMUM = 16


def model_threads() -> int:
    raw = os.getenv('IPWORK_MODEL_THREADS', '').strip()
    if not raw:
        return DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logging.getLogger(__name__).warning('Ignoring non-integer IPWORK_MODEL_THREADS=%r', raw)
        return DEFAULT
    return min(max(value, 1), MAXIMUM)
