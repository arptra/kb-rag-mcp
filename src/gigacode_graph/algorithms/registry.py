"""Explicit in-process registry for production and experimental algorithms."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

from gigacode_graph.algorithms.contracts import GraphAlgorithmDescriptor, GraphBuildAlgorithm
from gigacode_graph.algorithms.static_v2 import StaticV2Algorithm

AlgorithmFactory = Callable[[], GraphBuildAlgorithm]


class GraphAlgorithmRegistry:
    """Resolve algorithms by stable id and reject accidental duplicate ids."""

    def __init__(self) -> None:
        self._factories: dict[str, AlgorithmFactory] = {}
        self._plugins_loaded = False

    def register(self, factory: AlgorithmFactory) -> None:
        algorithm = factory()
        algorithm_id = algorithm.descriptor.id
        if algorithm_id in self._factories:
            raise ValueError(f"Graph algorithm is already registered: {algorithm_id}")
        self._factories[algorithm_id] = factory

    def create(self, algorithm_id: str) -> GraphBuildAlgorithm:
        self.load_plugins()
        factory = self._factories.get(algorithm_id)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "none"
            raise ValueError(
                f"Unknown graph algorithm {algorithm_id!r}; available: {available}"
            )
        return factory()

    def descriptors(self) -> tuple[GraphAlgorithmDescriptor, ...]:
        self.load_plugins()
        return tuple(
            self._factories[key]().descriptor for key in sorted(self._factories)
        )

    def load_plugins(self) -> None:
        """Load separately installed algorithms from the documented entry-point group."""
        if self._plugins_loaded:
            return
        self._plugins_loaded = True
        for entry_point in entry_points(group="corporate_kb.graph_algorithms"):
            loaded = entry_point.load()
            if not callable(loaded):
                raise TypeError(
                    f"Graph algorithm entry point {entry_point.name!r} is not callable"
                )
            self.register(loaded)


registry = GraphAlgorithmRegistry()
registry.register(StaticV2Algorithm)


def get_graph_algorithm(algorithm_id: str) -> GraphBuildAlgorithm:
    return registry.create(algorithm_id)
