import { isLoopbackMilvusAddress } from './milvus-vectordb';

describe('local-only Milvus address validation', () => {
    it.each([
        'localhost:19530',
        '127.0.0.1:19530',
        'http://127.0.0.1:19530',
        'grpc://[::1]:19530',
    ])('accepts loopback endpoint %s', address => {
        expect(isLoopbackMilvusAddress(address)).toBe(true);
    });

    it.each([
        'milvus.example.com:19530',
        '10.0.0.5:19530',
        '192.168.1.5:19530',
        'https://api.cloud.zilliz.com',
        'not an address',
    ])('rejects non-loopback endpoint %s', address => {
        expect(isLoopbackMilvusAddress(address)).toBe(false);
    });
});
