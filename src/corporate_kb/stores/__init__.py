"""Knowledge store interfaces and implementations."""

from corporate_kb.stores.base import KnowledgeStore
from corporate_kb.stores.memory import InMemoryKnowledgeStore

__all__ = ["InMemoryKnowledgeStore", "KnowledgeStore"]
