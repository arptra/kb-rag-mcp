"""Copy into a separate package and replace the example post-processing step."""

from gigacode_graph.algorithms import (
    GraphAlgorithmDescriptor,
    GraphBuildContext,
    GraphBuildRequest,
    GraphBuildResult,
)
from gigacode_graph.algorithms.static_v2 import StaticV2Algorithm


class CandidateAlgorithm(StaticV2Algorithm):
    """One increment based on production static-v2."""

    @property
    def descriptor(self) -> GraphAlgorithmDescriptor:
        return GraphAlgorithmDescriptor(
            id="candidate-example",
            version="0.1.0",
            description="Explain the one general extraction rule changed in this increment",
            cache_namespace="candidate-example-v1",
            capabilities=(*super().descriptor.capabilities, "example-rule"),
        )

    def build(
        self,
        request: GraphBuildRequest,
        context: GraphBuildContext,
    ) -> GraphBuildResult:
        baseline = super().build(request, context)
        # Replace this with one general, evidence-backed transformation.
        graph = baseline.graph.model_copy(update={"algorithm": self.descriptor.as_dict()})
        return GraphBuildResult(
            graph=graph,
            descriptor=self.descriptor,
            metrics={**baseline.metrics, "example_rule_matches": 0},
            diagnostics=baseline.diagnostics,
        )
