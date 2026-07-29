"""Disk cache for index startup acceleration."""

from corporate_kb.cache.manager import CACHE_SCHEMA_VERSION, CachedIndex, CacheManager

__all__ = ["CACHE_SCHEMA_VERSION", "CacheManager", "CachedIndex"]
