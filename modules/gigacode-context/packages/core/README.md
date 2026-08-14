# GigaCode Context Core

Local TypeScript indexing engine derived from `zilliztech/claude-context`.

This build exposes `LocalTransformerEmbedding` and `MilvusVectorDatabase` for
the GigaCode MCP runtime. Cloud embedding clients are intentionally excluded
from the package and its dependency graph.

```ts
import {
  Context,
  LocalTransformerEmbedding,
  MilvusVectorDatabase
} from '@zilliz/claude-context-core';

const context = new Context({
  embedding: new LocalTransformerEmbedding({
    modelPath: '/absolute/path/to/models/multilingual-e5-small',
    dimension: 384,
    dtype: 'q8',
    queryPrefix: 'query: ',
    documentPrefix: 'passage: '
  }),
  vectorDatabase: new MilvusVectorDatabase({
    address: '127.0.0.1:19530',
    localOnly: true
  })
});
```

Transformers.js remote model loading is disabled. The model directory must be
present locally before the process starts.
