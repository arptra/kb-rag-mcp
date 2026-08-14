import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { LocalTransformerEmbedding, LocalTransformersRuntime } from './local-transformer-embedding';

describe('LocalTransformerEmbedding', () => {
    let tempRoot: string;
    let modelPath: string;

    beforeEach(() => {
        jest.clearAllMocks();
        tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'gigacode-local-model-'));
        modelPath = path.join(tempRoot, 'model');
        fs.mkdirSync(modelPath);
    });

    afterEach(() => {
        fs.rmSync(tempRoot, { recursive: true, force: true });
    });

    it('loads only the explicitly bundled local model and applies query/document prefixes', async () => {
        const extractor = jest.fn(async (input: string | string[]) => {
            const rows = Array.isArray(input) ? input : [input];
            return {
                tolist: () => rows.map((value, index) => [value.length, index, 1]),
            };
        });
        const runtime: LocalTransformersRuntime = {
            env: {
                allowLocalModels: false,
                allowRemoteModels: true,
                localModelPath: '',
            },
            pipeline: jest.fn().mockResolvedValue(extractor),
        };

        const embedding = new LocalTransformerEmbedding({
            modelPath,
            dimension: 3,
            queryPrefix: 'query: ',
            documentPrefix: 'passage: ',
            runtimeLoader: async () => runtime,
        });

        await embedding.embed('find controller');
        await embedding.embedBatch(['first chunk', 'second chunk']);

        expect(runtime.env.allowRemoteModels).toBe(false);
        expect(runtime.env.allowLocalModels).toBe(true);
        expect(runtime.env.localModelPath).toBe(fs.realpathSync(tempRoot));
        expect(runtime.pipeline).toHaveBeenCalledWith(
            'feature-extraction',
            fs.realpathSync(modelPath),
            { local_files_only: true, dtype: 'q8' }
        );
        expect(extractor).toHaveBeenNthCalledWith(
            1,
            ['query: find controller'],
            { pooling: 'mean', normalize: true }
        );
        expect(extractor).toHaveBeenNthCalledWith(
            2,
            ['passage: first chunk', 'passage: second chunk'],
            { pooling: 'mean', normalize: true }
        );
    });

    it('fails when configured dimension differs from the model output', async () => {
        const runtime: LocalTransformersRuntime = {
            env: { allowLocalModels: false, allowRemoteModels: true, localModelPath: '' },
            pipeline: jest.fn().mockResolvedValue(async () => ({ tolist: () => [[1, 2]] })),
        };
        const embedding = new LocalTransformerEmbedding({
            modelPath,
            dimension: 3,
            runtimeLoader: async () => runtime,
        });

        await expect(embedding.embed('text')).rejects.toThrow(/dimension mismatch/);
    });

    it('requires an absolute existing model directory', () => {
        expect(() => new LocalTransformerEmbedding({ modelPath: 'relative/model', dimension: 3 }))
            .toThrow(/absolute path/);
        expect(() => new LocalTransformerEmbedding({ modelPath: path.join(tempRoot, 'missing'), dimension: 3 }))
            .toThrow(/does not exist/);
    });
});
