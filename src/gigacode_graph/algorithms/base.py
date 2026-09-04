"""Abstract base class for graph construction implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from gigacode_graph.algorithms.contracts import (
    GraphAlgorithmDescriptor,
    GraphBuildContext,
    GraphBuildRequest,
    GraphBuildResult,
)


class BaseGraphBuildAlgorithm(ABC):
    """Nominal base class for teams that prefer inheritance over a Protocol."""

    @property
    @abstractmethod
    def descriptor(self) -> GraphAlgorithmDescriptor:
        """Return immutable implementation identity and capabilities."""

    @abstractmethod
    def build(
        self,
        request: GraphBuildRequest,
        context: GraphBuildContext,
    ) -> GraphBuildResult:
        """Build a graph without writing production stores."""
