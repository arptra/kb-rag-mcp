import * as fs from 'node:fs';
import * as path from 'node:path';
import { Embedding, EmbeddingVector } from './base-embedding';

type TensorLike = {
    tolist(): unknown;
};

export type LocalEmbeddingDtype = 'fp32' | 'fp16' | 'int8' | 'uint8' | 'q8' | 'q4' | 'q4f16' | 'bnb4';

type FeatureExtractor = (
    texts: string | string[],
    options: { pooling: 'mean'; normalize: true }
) => Promise<TensorLike>;

export type LocalTransformersRuntime = {
    env: {
        allowLocalModels: boolean;
        allowRemoteModels: boolean;
        localModelPath: string;
    };
    pipeline(
        task: 'feature-extraction',
        model: string,
        options: { local_files_only: true; dtype: LocalEmbeddingDtype }
    ): Promise<FeatureExtractor>;
};

export interface LocalTransformerEmbeddingConfig {
    /** Absolute path to a Transformers.js-compatible model directory. */
    modelPath: string;
    /** Exact output dimension of the bundled model. */
    dimension: number;
    maxTokens?: number;
    queryPrefix?: string;
    documentPrefix?: string;
    dtype?: LocalEmbeddingDtype;
    /** Test seam; production uses the bundled @huggingface/transformers package. */
    runtimeLoader?: () => Promise<LocalTransformersRuntime>;
}

/**
 * Runs embeddings in-process with Transformers.js and a model already present
 * on disk. Remote model resolution is disabled before the model is loaded.
 */
export class LocalTransformerEmbedding extends Embedding {
    private readonly modelPath: string;
    private readonly dimension: number;
    private readonly queryPrefix: string;
    private readonly documentPrefix: string;
    private readonly dtype: LocalEmbeddingDtype;
    private readonly runtimeLoader: () => Promise<LocalTransformersRuntime>;
    private extractorPromise?: Promise<FeatureExtractor>;
    protected maxTokens: number;

    constructor(config: LocalTransformerEmbeddingConfig) {
        super();

        if (!path.isAbsolute(config.modelPath)) {
            throw new Error('LOCAL_EMBEDDING_MODEL_PATH must be an absolute path');
        }
        if (!fs.existsSync(config.modelPath) || !fs.statSync(config.modelPath).isDirectory()) {
            throw new Error(`Local embedding model directory does not exist: ${config.modelPath}`);
        }
        if (!Number.isInteger(config.dimension) || config.dimension <= 0) {
            throw new Error('LOCAL_EMBEDDING_DIMENSION must be a positive integer');
        }
        if (config.maxTokens !== undefined && (!Number.isInteger(config.maxTokens) || config.maxTokens <= 0)) {
            throw new Error('LOCAL_EMBEDDING_MAX_TOKENS must be a positive integer');
        }

        this.modelPath = fs.realpathSync(config.modelPath);
        this.dimension = config.dimension;
        this.maxTokens = config.maxTokens ?? 2048;
        this.queryPrefix = config.queryPrefix ?? '';
        this.documentPrefix = config.documentPrefix ?? '';
        this.dtype = config.dtype ?? 'q8';
        this.runtimeLoader = config.runtimeLoader ?? (() => {
            // Keep native dynamic import in the CommonJS core build because
            // Transformers.js is distributed as ESM.
            const nativeImport = new Function('specifier', 'return import(specifier)') as
                (specifier: string) => Promise<LocalTransformersRuntime>;
            return nativeImport('@huggingface/transformers');
        });
    }

    async detectDimension(testText: string = 'dimension check'): Promise<number> {
        const result = await this.embed(testText);
        return result.dimension;
    }

    async embed(text: string): Promise<EmbeddingVector> {
        const vectors = await this.run([this.queryPrefix + this.preprocessText(text)]);
        return this.toEmbeddingVector(vectors[0]);
    }

    async embedBatch(texts: string[]): Promise<EmbeddingVector[]> {
        if (texts.length === 0) {
            return [];
        }

        const processed = this.preprocessTexts(texts).map(text => this.documentPrefix + text);
        const vectors = await this.run(processed);
        return vectors.map(vector => this.toEmbeddingVector(vector));
    }

    getDimension(): number {
        return this.dimension;
    }

    getProvider(): string {
        return 'LocalTransformer';
    }

    private async getExtractor(): Promise<FeatureExtractor> {
        if (!this.extractorPromise) {
            this.extractorPromise = this.loadExtractor();
        }
        return this.extractorPromise;
    }

    private async loadExtractor(): Promise<FeatureExtractor> {
        const transformers = await this.runtimeLoader();

        transformers.env.allowRemoteModels = false;
        transformers.env.allowLocalModels = true;
        transformers.env.localModelPath = path.dirname(this.modelPath);

        return transformers.pipeline(
            'feature-extraction',
            this.modelPath,
            { local_files_only: true, dtype: this.dtype }
        );
    }

    private async run(texts: string[]): Promise<number[][]> {
        const extractor = await this.getExtractor();
        const output = await extractor(texts, { pooling: 'mean', normalize: true });
        const value = output.tolist();

        if (!Array.isArray(value)) {
            throw new Error('Local embedding model returned a non-array result');
        }

        const vectors = value as unknown[];
        if (vectors.length === 0) {
            throw new Error('Local embedding model returned no vectors');
        }

        if (typeof vectors[0] === 'number') {
            return [this.assertNumericVector(vectors)];
        }

        return vectors.map(vector => {
            if (!Array.isArray(vector)) {
                throw new Error('Local embedding model returned an invalid vector');
            }
            return this.assertNumericVector(vector);
        });
    }

    private assertNumericVector(value: unknown[]): number[] {
        if (!value.every(item => typeof item === 'number' && Number.isFinite(item))) {
            throw new Error('Local embedding model returned a vector with non-numeric values');
        }
        return value as number[];
    }

    private toEmbeddingVector(vector: number[] | undefined): EmbeddingVector {
        if (!vector) {
            throw new Error('Local embedding model returned no vector');
        }
        if (vector.length !== this.dimension) {
            throw new Error(
                `Local embedding dimension mismatch: configured ${this.dimension}, model returned ${vector.length}`
            );
        }
        return { vector, dimension: this.dimension };
    }
}
