# ADME Semantic Search: Quick Start Guide

**Goal**: Build semantic search + knowledge graph for Volve/TNO data in 8 weeks.

---

## Week 1: Foundation Setup (This Week)

### Step 1: Extract Text Fields (15 min)

```bash
cd C:\Users\marielherzog\adme-ingestion-tool

# Run text extraction
python -c "
from app.services.semantic_indexing import SemanticIndexBuilder
from pathlib import Path

builder = SemanticIndexBuilder(
    Path.home() / 'adme-ingestion-tool' / '.semantic-index.jsonl'
)

# Add Volve Wells
builder.add_from_jsonl(
    Path.home() / 'osdu-data' / 'volve' / 'generated-json' / 'load_Well.jsonl',
    'Well'
)

# Add Volve Wellbores
builder.add_from_jsonl(
    Path.home() / 'osdu-data' / 'volve' / 'generated-json' / 'load_Wellbore.jsonl',
    'Wellbore'
)

# Save
builder.save_index('jsonl')

# Show stats
stats = builder.stats()
print('\\nIndex Statistics:')
for key, value in stats.items():
    print(f'  {key}: {value}')
"
```

**Output**:
```
Index Statistics:
  total_documents: 200+ (Well + Wellbore records)
  by_type: {'Well': 48, 'Wellbore': 164}
  documents_with_remarks: ~120
  avg_text_length: 280 characters
```

---

### Step 2: Generate Embeddings (30 min)

#### Option A: Local (Recommended for POC)

Install and run Ollama:
```bash
# Download from https://ollama.ai
ollama pull nomic-embed-text

# Start server (runs on port 11434)
ollama serve
```

Then generate embeddings:
```bash
cd C:\Users\marielherzog\adme-ingestion-tool

python -c "
from app.services.semantic_embeddings import OllamaEmbedding, EmbeddingPipeline
from pathlib import Path

# Create embeddings
embedding_model = OllamaEmbedding()
pipeline = EmbeddingPipeline(embedding_model)

input_file = Path.home() / 'adme-ingestion-tool' / '.semantic-index.jsonl'
output_file = Path.home() / 'adme-ingestion-tool' / '.semantic-index-embedded.jsonl'

count = pipeline.process_jsonl(input_file, output_file, batch_size=16)
print(f'✓ Generated {count} embeddings')
"
```

**Output**: `.semantic-index-embedded.jsonl` with embeddings (768-dim Nomic)

#### Option B: Cloud (For Production)

Set OpenAI API key:
```bash
$env:OPENAI_API_KEY = "sk-..."

python -c "
from app.services.semantic_embeddings import OpenAIEmbedding, EmbeddingPipeline
from pathlib import Path

embedding_model = OpenAIEmbedding('text-embedding-3-small')
pipeline = EmbeddingPipeline(embedding_model)

input_file = Path.home() / 'adme-ingestion-tool' / '.semantic-index.jsonl'
output_file = Path.home() / 'adme-ingestion-tool' / '.semantic-index-embedded.jsonl'

count = pipeline.process_jsonl(input_file, output_file)
print(f'✓ Generated {count} embeddings (Cost: ~$0.01)')
"
```

---

### Step 3: Inspect Embeddings (10 min)

```bash
python -c "
import json
from pathlib import Path

embedded_file = Path.home() / 'adme-ingestion-tool' / '.semantic-index-embedded.jsonl'

with open(embedded_file) as f:
    sample = json.loads(f.readline())

print(f'Sample embedded document:')
print(f'  ID: {sample[\"entity_id\"]}')
print(f'  Type: {sample[\"entity_type\"]}')
print(f'  Name: {sample[\"common_name\"]}')
print(f'  Text preview: {sample[\"searchable_text\"][:150]}...')
print(f'  Embedding dim: {sample[\"embedding_dim\"]}')
print(f'  Model: {sample[\"embedding_model\"]}')
print(f'  First 5 embedding values: {sample[\"embedding\"][:5]}')
"
```

---

## Week 2: Ontology Integration

### Step 1: Generate OSDU Ontology

```bash
cd C:\Users\marielherzog\adme-ingestion-tool

# Clone Accenture ontology tool
git clone https://github.com/Accenture/OSDU-Ontology.git
cd OSDU-Ontology

# Generate TTL from OSDU schemas
python3 create_ontology.py \
    --src "path/to/osdu-schemas/rc--3.0.0/3-schema" \
    --dest ./output/
```

**Output**: `osdu_draft.ttl` (OWL 3.0 ontology)

### Step 2: Load into Triple Store

Using Apache Jena Fuseki (Docker):
```bash
docker run -d \
  -p 3030:3030 \
  --name jena-fuseki \
  -v C:\Users\marielherzog\adme-ingestion-tool\ontology:/ontology \
  stain/jena-fuseki

# Upload TTL via UI at http://localhost:3030
# Or via command line:
curl -X POST -G 'http://localhost:3030/ds/data' \
  --data-urlencode 'graph=http://example.org/osdu' \
  -H 'Content-Type: application/n-triples' \
  --data-binary @osdu_draft.ttl
```

### Step 3: Query Ontology via SPARQL

```bash
python -c "
from rdflib import Graph, Namespace

# Load ontology
g = Graph()
g.parse('OSDU-Ontology/output/osdu_draft.ttl', format='turtle')

# Query: Find all Well-related classes
results = g.query('''
SELECT ?class ?comment WHERE {
    ?class rdfs:subClassOf* ?parent ;
           rdfs:comment ?comment .
    FILTER(CONTAINS(str(?class), 'Well'))
}
LIMIT 10
''')

for row in results:
    print(f'{row.class}: {row.comment}')
"
```

---

## Week 3: Database Setup (PostgreSQL + pgvector)

### Step 1: Create Semantic Index Table

Using existing ADME PostgreSQL:

```sql
-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create semantic documents table
CREATE TABLE semantic_documents (
    id SERIAL PRIMARY KEY,
    entity_id VARCHAR(256) UNIQUE NOT NULL,
    entity_type VARCHAR(50),  -- "Well", "Wellbore", "WellboreTrajectory"
    entity_kind VARCHAR(256),
    common_name TEXT,
    searchable_text TEXT,
    embedding vector(768),  -- Nomic embed dimension, adjust for OpenAI (1536)
    embedding_model VARCHAR(100),
    embedding_dim INT,
    
    -- Structured metadata for filtering
    field_name VARCHAR(256),
    measured_depth FLOAT,
    true_vertical_depth FLOAT,
    inclination FLOAT,
    operator VARCHAR(256),
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Create index for similarity search (HNSW for pgvector)
CREATE INDEX ON semantic_documents 
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- Create index for structured queries
CREATE INDEX ON semantic_documents(entity_type, field_name);
```

### Step 2: Load Embeddings into PostgreSQL

```python
import json
import psycopg2
from psycopg2.extras import execute_values
from pathlib import Path

conn = psycopg2.connect(
    "host=localhost dbname=adme user=postgres password=xxx"
)
cur = conn.cursor()

embedded_file = Path.home() / 'adme-ingestion-tool' / '.semantic-index-embedded.jsonl'

# Read and insert
rows = []
with open(embedded_file) as f:
    for line in f:
        doc = json.loads(line)
        rows.append((
            doc['entity_id'],
            doc['entity_type'],
            doc['entity_kind'],
            doc['common_name'],
            doc['searchable_text'],
            doc['embedding'],  # Will be cast to vector type
            doc['embedding_model'],
            doc['embedding_dim'],
            doc.get('field_name'),
            doc.get('measured_depth'),
            doc.get('true_vertical_depth'),
            doc.get('inclination'),
            doc.get('operator'),
        ))

# Bulk insert
execute_values(
    cur,
    """
    INSERT INTO semantic_documents 
    (entity_id, entity_type, entity_kind, common_name, searchable_text, 
     embedding, embedding_model, embedding_dim, field_name, measured_depth,
     true_vertical_depth, inclination, operator)
    VALUES %s
    ON CONFLICT (entity_id) DO UPDATE SET updated_at = NOW()
    """,
    rows,
    page_size=100
)

conn.commit()
cur.close()
conn.close()

print(f"Inserted {len(rows)} documents into PostgreSQL")
```

### Step 3: Test Similarity Search

```sql
-- Find wells similar to "lost circulation drilling problems"
SELECT 
    entity_id,
    common_name,
    entity_type,
    searchable_text,
    embedding <-> 'YOUR_QUERY_EMBEDDING_HERE'::vector AS distance,
    1 - (embedding <-> 'YOUR_QUERY_EMBEDDING_HERE'::vector) AS similarity
FROM semantic_documents
WHERE entity_type IN ('Well', 'Wellbore')
ORDER BY embedding <-> 'YOUR_QUERY_EMBEDDING_HERE'::vector
LIMIT 10;
```

---

## What You Have Now (End of Week 3)

✅ **200+ documents** extracted and indexed  
✅ **768-dim embeddings** generated (or 1536 for OpenAI)  
✅ **PostgreSQL + pgvector** with similarity search working  
✅ **OSDU Ontology** loaded (structured metadata)  

---

## Hybrid Query Example

```python
import numpy as np
from app.services.semantic_embeddings import OllamaEmbedding

# User query
query = "which wellbores had drilling problems in the North Sea?"

# 1. Generate query embedding
embedding_model = OllamaEmbedding()
query_embedding = embedding_model.embed(query)

# 2. Structured query (PostgreSQL)
structured_sql = """
SELECT entity_id, common_name FROM semantic_documents
WHERE entity_type = 'Wellbore' AND field_name = 'North Sea'
"""

# 3. Semantic query (pgvector similarity)
semantic_sql = f"""
SELECT entity_id, common_name, 
       1 - (embedding <-> ARRAY{query_embedding}::vector) AS similarity
FROM semantic_documents
WHERE entity_type = 'Wellbore'
ORDER BY embedding <-> ARRAY{query_embedding}::vector
LIMIT 20
"""

# 4. Merge results with weighted scoring
# structured_score = 1.0 if in structured results else 0.0
# semantic_score = similarity (0-1)
# final_score = 0.4 * structured_score + 0.6 * semantic_score
```

---

## Next Actions

### This Week:
- [ ] Run text extraction script
- [ ] Generate embeddings (local with Ollama)
- [ ] Verify `.semantic-index-embedded.jsonl` has 200+ records

### Next Week:
- [ ] Install Docker + Jena Fuseki
- [ ] Generate OSDU ontology
- [ ] Load into triple store

### Week 3:
- [ ] Create PostgreSQL semantic table
- [ ] Load embeddings into pgvector
- [ ] Test similarity search

---

## Files Reference

| File | Purpose |
|------|---------|
| [SEMANTIC_SEARCH_ROADMAP.md](../docs/SEMANTIC_SEARCH_ROADMAP.md) | 8-week implementation plan |
| [semantic_indexing.py](../app/services/semantic_indexing.py) | Text extraction from OSDU records |
| [semantic_embeddings.py](../app/services/semantic_embeddings.py) | Embedding generation (Ollama, OpenAI, Sentence-Transformers) |

---

## Glossary

| Term | Definition |
|------|-----------|
| **Embedding** | 768-dimensional vector representing semantic meaning of text |
| **pgvector** | PostgreSQL extension for vector similarity search |
| **Ollama** | Local LLM/embedding server (free, no API key needed) |
| **Fuseki** | Apache triple store for SPARQL queries (RDF/OWL) |
| **Ontology** | Formal representation of OSDU schema structure |
| **Hybrid Query** | Combines structured filters + semantic similarity |

---

## Help & Troubleshooting

### Ollama won't start:
```bash
# Check if port 11434 is in use
netstat -ano | findstr :11434

# Or try different host
OllamaEmbedding(host="http://127.0.0.1:11434")
```

### PostgreSQL connection error:
```bash
# Test connection
psql -h localhost -U postgres -d adme

# Check pgvector is installed
psql -h localhost -U postgres -d adme -c "CREATE EXTENSION vector;"
```

### Embedding file too large:
- Process in batches: `process_jsonl(..., batch_size=16)`
- Use smaller embedding model: `SentenceTransformerEmbedding('all-MiniLM-L6-v2')`

---

**Questions?** See [SEMANTIC_SEARCH_ROADMAP.md](../docs/SEMANTIC_SEARCH_ROADMAP.md) for detailed architecture.
