"""Model catalog package (curated overlay + live discovery)."""

from .catalog import ModelCatalog, ModelEntry, catalog
from .paths import cache_dir, data_home

__all__ = ["ModelCatalog", "ModelEntry", "catalog", "cache_dir", "data_home"]
