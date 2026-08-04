"""Make the backend package importable from tests (backend/ is the root)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
