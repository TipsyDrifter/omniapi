"""Harness adapters: run any model as a tooled agent via its vendor CLI."""

from .events import EVENT_TYPES, RunEvent, RunSpec, new_run_id
from .registry import HarnessRegistry

__all__ = ["EVENT_TYPES", "RunEvent", "RunSpec", "new_run_id", "HarnessRegistry"]
