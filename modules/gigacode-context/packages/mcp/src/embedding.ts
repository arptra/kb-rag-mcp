import { LocalTransformerEmbedding } from "@zilliz/claude-context-core";
import { ContextMcpConfig } from "./config.js";

export function createEmbeddingInstance(config: ContextMcpConfig): LocalTransformerEmbedding {
    console.log(`[EMBEDDING] Loading local model from ${config.embeddingModelPath}`);

    return new LocalTransformerEmbedding({
        modelPath: config.embeddingModelPath,
        dimension: config.embeddingDimension,
        maxTokens: config.embeddingMaxTokens,
        queryPrefix: config.embeddingQueryPrefix,
        documentPrefix: config.embeddingDocumentPrefix,
        dtype: config.embeddingDtype
    });
}

export function logEmbeddingProviderInfo(
    config: ContextMcpConfig,
    embedding: LocalTransformerEmbedding
): void {
    console.log(`[EMBEDDING] ✅ Local in-process provider initialized`);
    console.log(`[EMBEDDING] Provider: ${embedding.getProvider()}`);
    console.log(`[EMBEDDING] Dimension: ${embedding.getDimension()}`);
    console.log(`[EMBEDDING] Remote model loading: disabled`);
    console.log(`[EMBEDDING] Model directory: ${config.embeddingModelPath}`);
}
