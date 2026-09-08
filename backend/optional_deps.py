"""Shared helper for the ipworker-only optional-dependency import guard that
was previously copy-pasted (cv2/onnxruntime in ipwork_face.py, tesserocr in
ipwork_ocr.py): heavy deps that live only in requirements-ipworker.txt, so
importing the ipwork_*.py processor modules must never hard-crash outside the
ipworker image.
"""
from __future__ import annotations

import importlib
from typing import Any, Optional


def try_import(module_name: str) -> Optional[Any]:
    """Import ``module_name`` and return it, or ``None`` if unavailable.

    Mirrors the ``try: import x except Exception: x = None`` pattern used at
    every ipwork_*.py optional-dependency guard, without changing behavior:
    any failure (missing package, native-lib load error, etc.) is swallowed
    the same way a bare ``except Exception`` would.
    """
    try:
        return importlib.import_module(module_name)
    except Exception:  # pragma: no cover - only absent outside the ipworker image
        return None
