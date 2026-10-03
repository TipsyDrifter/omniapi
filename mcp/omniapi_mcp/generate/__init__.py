"""The GUI's generation jobs (v1.1): see ``manager.py``."""

from .errors import classify
from .manager import GenerationError, GenerationManager
from .options import options

__all__ = ["GenerationError", "GenerationManager", "classify", "options"]
