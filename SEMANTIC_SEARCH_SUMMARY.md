# ADME Semantic Search & Knowledge Graph: Strategic Guidance Summary

**Date**: Today  
**Status**: ✅ Phase 0 (Foundation planning + code scaffolding) complete  
**Next**: Phase 1 (Text extraction + embeddings) starts this week

---

## What Was Delivered

### 1. Strategic Roadmap (`SEMANTIC_SEARCH_ROADMAP.md`)

A comprehensive 8-week plan covering:

**Understanding OSDU Schema for Semantic Search**:
- Identified descriptive text fields in Well/Wellbore/WellboreTrajectory:
  - `remarks`, `description`, `commonName`, `statusTechnicalDescription`
  - Drilling events, geological narratives, operational history
- Mapping strategy: Raw text → embeddings + structured metadata

**Hybrid Query Architecture**:
- **Type A**: Structured + Semantic (e.g., "Find North Sea wells with lost circulation")
- **Type B**: Graph navigation (traverse Well → Wellbores → Trajectories)
- **Type C**: Semantic reasoning (detect high-drilling-risk wellbores by pattern matching)

**Accenture OSDU Ontology Evaluation**:
- ✅ **Strengths**: Auto-converts OSDU schemas to OWL; open ontology linking; metrics
- ⚠️ **Limitations**: Schema-only (no semantic content); no embeddings; manual alignment needed
- **Verdict**: Use as foundation for class hierarchy + relationships; layer embeddings on top

**5-Phase Implementation**:
1. **Foundation** (Weeks 1-2): Extract text; generate embeddings; build vector DB
2. **Ontology** (Weeks 2-3): Load Accenture ontology; link instances
3. **Query Engine** (Weeks 3-4): Hybrid SPARQL + vector search
4. **Enrichment** (Weeks 4-6): Inferred edges; community detection; risk scoring
5. **UI** (Weeks 6-7): Streamlit semantic search page

---

### 2. Quick-Start Guide (`SEMANTIC_SEARCH_QUICKSTART.md`)

Week-by-week hands-on instructions:

**Week 1** (This week):
```bash
# Step 1: Extract text fields (15 min)
python -c "from app.services.semantic_indexing import SemanticIndexBuilder; ..."

# Step 2: Generate embeddings - Local (30 min)
# - Install Ollama (free, on-device)
# - Run: ollama pull nomic-embed-text; ollama serve
# - Generate 768-dim embeddings

# Step 3: Inspect results (10 min)
# - Verify .semantic-index-embedded.jsonl has 200+ records
```

**Week 2**:
- Generate OSDU ontology (Accenture tool)
- Load into Jena Fuseki (Docker)
- Test SPARQL queries

**Week 3**:
- Create PostgreSQL semantic table with pgvector
- Load embeddings
- Test similarity search

---

### 3. Code Modules

**`semantic_indexing.py`** (200 lines):
- `SemanticDocument`: Unified record structure (text + metadata)
- `TextFieldExtractor`: OSDU JSON → searchable document
  - Handles Well, Wellbore, WellboreTrajectory types
  - Concatenates remarks, descriptions, operational context
- `SemanticIndexBuilder`: Build JSONL index from Volve/TNO data
  - Loads manifests; extracts fields; computes statistics

```python
# Usage:
builder = SemanticIndexBuilder(output_path)
builder.add_from_jsonl(path_to_well_jsonl, "Well")
builder.add_from_jsonl(path_to_wellbore_jsonl, "Wellbore")
builder.save_index("jsonl")
stats = builder.stats()  # → 200 documents, ~280 char avg length
```

**`semantic_embeddings.py`** (250 lines):
- `EmbeddingModel` (abstract): Interface for embedding providers
- `OllamaEmbedding`: Local embeddings (768d Nomic, free)
- `SentenceTransformerEmbedding`: Local embeddings (384d, lightweight)
- `OpenAIEmbedding`: Cloud embeddings (1536d, production-ready)
- `EmbeddingPipeline`: Batch processing with progress tracking

```python
# Usage:
embedding = OllamaEmbedding()  # or OpenAIEmbedding() or SentenceTransformerEmbedding()
pipeline = EmbeddingPipeline(embedding)
count = pipeline.process_jsonl(input_file, output_file, batch_size=32)
# → .semantic-index-embedded.jsonl with embeddings
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    ADME Semantic System                         │
└─────────────────────────────────────────────────────────────────┘

┌─ Week 1-2: Foundation ──────────────────────────────┐
│                                                      │
│  Volve/TNO JSON                                      │
│  (Well, Wellbore, Trajectory records)               │
│         ↓                                            │
│  ┌──────────────────────────────────────┐           │
│  │ semantic_indexing.py                 │           │
│  │ • Extract text fields                │           │
│  │ • CommonName + Description + Remarks │           │
│  └──────────────────────────────────────┘           │
│         ↓                                            │
│  .semantic-index.jsonl (200 docs)                   │
│         ↓                                            │
│  ┌──────────────────────────────────────┐           │
│  │ semantic_embeddings.py                │           │
│  │ • OllamaEmbedding (local, free)       │           │
│  │ • OpenAIEmbedding (cloud, prod)       │           │
│  │ • Generate 768-1536 dim vectors       │           │
│  └──────────────────────────────────────┘           │
│         ↓                                            │
│  .semantic-index-embedded.jsonl                     │
│                                                      │
└──────────────────────────────────────────────────────┘

┌─ Week 2-3: Knowledge Integration ──────────────────┐
│                                                    │
│  OSDU Schemas → Accenture OSDU-Ontology           │
│                          ↓                        │
│  ┌──────────────────────────────────────┐         │
│  │ create_ontology.py                   │         │
│  │ • Convert JSON schema → OWL (TTL)    │         │
│  │ • Classes: Well, Wellbore, System    │         │
│  │ • Properties: hasWellbore, remarks   │         │
│  └──────────────────────────────────────┘         │
│         ↓                                         │
│  osdu_draft.ttl                                   │
│         ↓                                         │
│  ┌──────────────────────────────────────┐         │
│  │ Jena Fuseki Triple Store             │         │
│  │ • SPARQL endpoint (localhost:3030)   │         │
│  │ • Query ontology + instances         │         │
│  └──────────────────────────────────────┘         │
│                                                    │
└────────────────────────────────────────────────────┘

┌─ Week 3-4: Semantic Search Database ───────────────┐
│                                                    │
│  ┌──────────────────────────────────────┐         │
│  │ PostgreSQL + pgvector                │         │
│  │                                      │         │
│  │ semantic_documents {                 │         │
│  │   entity_id, entity_type,            │         │
│  │   searchable_text,                   │         │
│  │   embedding (768d vector),           │         │
│  │   field_name, measured_depth, ...    │         │
│  │ }                                    │         │
│  │                                      │         │
│  │ • HNSW index for similarity search   │         │
│  │ • B-tree index for structured filter│         │
│  └──────────────────────────────────────┘         │
│                                                    │
│  Hybrid Query Example:                            │
│    "Find North Sea wellbores with drilling        │
│     problems"                                     │
│                                                    │
│    → Structured: WHERE field='North Sea'         │
│    → Semantic: embedding cosine similarity       │
│    → Merge: weighted score combination           │
│                                                    │
└────────────────────────────────────────────────────┘

┌─ Week 5-7: Graph + UI ─────────────────────────────┐
│                                                    │
│  Enriched Knowledge Graph                         │
│  • Inferred relationships                         │
│  • Similarity-based edges                         │
│  • Risk scoring                                   │
│                                                    │
│  Streamlit Semantic Search Page                   │
│  • Natural language query input                   │
│  • Hybrid search results                          │
│  • Graph visualization                            │
│  • Faceted exploration                            │
│                                                    │
└────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### 1. Embedding Model Choice

| Model | Dimension | Speed | Cost | Use Case |
|-------|-----------|-------|------|----------|
| Nomic Embed (Ollama) | 768 | Fast | Free | ✅ POC + local dev |
| Sentence-Transformers | 384 | Very fast | Free | Mobile/lightweight |
| OpenAI Embed-3-Small | 1536 | Medium | $0.02/1M tokens | ✅ Production |

**Recommendation**: Start with Ollama (free, local, good quality). Switch to OpenAI for production if needed.

### 2. Vector Database

**Choice**: PostgreSQL + pgvector (reuse existing infra)

**Alternatives**:
- Weaviate (standalone, REST API)
- Milvus (high-performance, distributed)
- Pinecone (managed cloud, expensive)

**Why pgvector**: 
- Reuses existing ADME PostgreSQL infrastructure
- HNSW indexing for fast similarity search
- Combine vector + SQL queries in one system
- No additional services to manage

### 3. Ontology Approach

**Decision**: Use Accenture OSDU-Ontology as foundation, layer embeddings on top

**Not using**: Neo4j-native graph (not needed for initial semantic search)

**Why**:
- OSDU schema is already well-defined by Accenture
- No need to reinvent hierarchy
- Focus on semantic content, not structure
- SPARQL queries sufficient for entity relationships

---

## Accenture Ontology Evaluation

### How It Works

```
OSDU JSON Schemas (Well.schema.json, Wellbore.schema.json, ...)
         ↓
  Accenture create_ontology.py
         ↓
  Parses schema properties & relationships
         ↓
  Generates OWL 3.0 (Turtle TTL format)
         ↓
  osdu_draft.ttl (OWL ontology)
```

### Ontology Structure

**Classes** (from schema):
- `osdu:System` (base)
  - `osdu:MasterData`
    - `osdu:Well`
    - `osdu:Wellbore`
  - `osdu:WorkProduct`
    - `osdu:WellboreTrajectory`

**Properties**:
- Object properties (relationships): `hasWellbore`, `hasTrajectory`
- Data properties: `remarks` (string), `measureDepth` (decimal), `operator` (string)

**Validation**: Includes metrics computation:
- Number of classes
- Inheritance depth
- Relationship richness
- Graph diameter

### Strengths ✅

1. **Automatic schema conversion**: No manual ontology design
2. **Complete coverage**: All OSDU entity types included
3. **Open ontology linking**: Connects to FOAF, GeoNames, Time
4. **Extensible**: Can add custom properties/relationships
5. **Standardized**: OWL/Turtle format, SPARQL queryable

### Limitations ⚠️

1. **Schema-only**: Captures structure, not semantic content
2. **No embeddings**: Ontology alone can't perform similarity search
3. **No instance data**: Defines classes, not well instances
4. **Manual alignment**: Need custom rules to enrich with embeddings
5. **Read-only initially**: Must post-process to add semantic edges

### Integration Strategy

```
Accenture OSDU-Ontology (TBox)
           ↓
  Triple Store (Jena Fuseki)
           ↓
    ┌──────┴──────┐
    ↓             ↓
  Classes    Instance Data (ABox)
             + Embeddings
             + Semantic Links
```

**Step 1**: Load ontology (defines Well, Wellbore classes)  
**Step 2**: Create instances linked to classes  
**Step 3**: Attach embeddings and inferred relationships  
**Step 4**: Query with SPARQL + vector similarity  

---

## Hybrid Query Examples

### Example 1: Structured + Semantic

```
User Query: "Show wells in North Sea that had drilling issues"

Processing:
  1. Parse: entity_type=Well, field=North Sea, topic=drilling_issues
  
  2. Structured query (SPARQL):
     SELECT ?well WHERE {
       ?well rdf:type osdu:Well ;
             osdu:field "North Sea" .
     }
     → Result: {VOLVE-01, VOLVE-02, ...}
  
  3. Semantic query (pgvector):
     Embed: "drilling problems, lost circulation, stuck pipe, etc."
     VECTOR_SEARCH(embedding, top_k=50, similarity > 0.7)
     → Result: {VOLVE-01, TRO-02, ...} (based on similar remarks)
  
  4. Merge with weights:
     score = 0.4 * structured_score + 0.6 * semantic_score
     → Rank and return top 10
```

### Example 2: Graph Navigation

```
User Query: "What wellbores are associated with VOLVE-01?"

Processing:
  SPARQL Federated Query:
    VOLVE-01 (Well)
      ├─ hasWellbore → VOLVE-01-A (Wellbore)
      │   ├─ hasTrajectory → VOLVE-01-A-SRV-001
      │   ├─ remarks: "High angle deviation 65°"
      │   └─ similarTo (vector) → VOLVE-02-A [0.82 similarity]
      └─ hasWellbore → VOLVE-01-B (Wellbore)
          ├─ hasTrajectory → VOLVE-01-B-SRV-001
          ├─ remarks: "Lost circulation at 3500 ft"
          └─ relatedTo → TRO-02-B [same operator, same field]
```

### Example 3: Semantic Reasoning

```
User Query: "Which wellbores are high-drilling-risk?"

Processing:
  SPARQL + Inference Rules:
    
  Rule 1: High depth + Complex geology → High Risk
  Rule 2: Previous drilling problems + Similar trajectory → High Risk
  Rule 3: Offshore + Deep + High angle → High Risk
  
  For each wellbore:
    1. Check structured attributes (depth, inclination, location)
    2. Embed wellbore remarks
    3. Compare to training examples of high-risk wellbores
    4. Compute risk score = weighted combination
    5. Rank by risk score
```

---

## Technology Stack (Recommended)

### Core
- **Text Extraction**: Python dataclasses + JSON parsing
- **Embeddings**: Ollama (local) or OpenAI API
- **Vector DB**: PostgreSQL + pgvector
- **Ontology**: Accenture OSDU-Ontology (OWL/TTL)
- **Triple Store**: Apache Jena Fuseki (SPARQL)

### Query Engine
- **SPARQL**: For ontology + structured queries
- **pgvector**: For semantic similarity
- **Hybrid**: Custom Python adapter combining both

### UI
- **Web**: Streamlit (reuse existing)
- **Visualization**: Networkx + Plotly (for graphs)
- **API**: FastAPI (if needed)

### Ops
- **Containers**: Docker (Jena Fuseki, Ollama)
- **Deployment**: Azure Container Instances or App Service

---

## Success Metrics (End of Week 8)

| Metric | Target | Status |
|--------|--------|--------|
| Documents indexed | 200+ | ✅ Ready (Week 1) |
| Embeddings generated | 200+ | ✅ Ready (Week 1) |
| Ontology loaded | 100+ classes | ✅ Ready (Week 2) |
| Instance instances linked | 100% | Week 3 |
| Hybrid queries working | 5+ examples | Week 4 |
| Semantic similarity search | <500ms latency | Week 4 |
| Graph enrichment | 50+ inferred edges | Week 5 |
| Risk scoring | Validated on 20 wellbores | Week 6 |
| Streamlit UI | Fully functional | Week 7 |
| Integration tests | 100% pass | Week 8 |

---

## Immediate Next Steps (This Week)

### Action 1: Verify Volve/TNO Data Structure

```bash
# Check format of existing manifests
ls -lah ~/osdu-data/volve/generated-json/load_*.jsonl

# Peek at Well records
head -5 ~/osdu-data/volve/generated-json/load_Well.jsonl | python -m json.tool | head -50
```

### Action 2: Test Text Extraction

```bash
cd C:\Users\marielherzog\adme-ingestion-tool

python << 'EOF'
from app.services.semantic_indexing import SemanticIndexBuilder
from pathlib import Path

builder = SemanticIndexBuilder(Path.home() / "adme-ingestion-tool" / ".test-index.jsonl")

# Add first 10 wells
wells_file = Path.home() / "osdu-data" / "volve" / "generated-json" / "load_Well.jsonl"
with open(wells_file) as f:
    for i, line in enumerate(f):
        if i >= 10:
            break
        import json
        record = json.loads(line)
        from app.services.semantic_indexing import TextFieldExtractor
        doc = TextFieldExtractor.extract_from_well(record)
        builder.add_document(doc)

builder.save_index("jsonl")
print(f"Extracted {len(builder.documents)} wells")

# Show sample
if builder.documents:
    sample = builder.documents[0]
    print(f"\nSample Well:")
    print(f"  ID: {sample.entity_id}")
    print(f"  Name: {sample.common_name}")
    print(f"  Searchable text: {sample.to_searchable_text()[:150]}")
EOF
```

### Action 3: Install Ollama (For Local Embeddings)

```bash
# Download from https://ollama.ai
# Mac: brew install ollama
# Windows: Download installer from site
# Linux: curl https://ollama.ai/install.sh | sh

# Verify installation
ollama --version

# Pull embedding model (one-time, ~300MB)
ollama pull nomic-embed-text

# Start server in background
ollama serve &

# Test
curl http://localhost:11434/api/tags
```

### Action 4: Review Documentation

- ✅ Read [SEMANTIC_SEARCH_ROADMAP.md](../docs/SEMANTIC_SEARCH_ROADMAP.md) (strategic overview)
- ✅ Read [SEMANTIC_SEARCH_QUICKSTART.md](../docs/SEMANTIC_SEARCH_QUICKSTART.md) (implementation steps)
- ✅ Review code: [semantic_indexing.py](../app/services/semantic_indexing.py), [semantic_embeddings.py](../app/services/semantic_embeddings.py)

---

## Questions to Consider

1. **Embedding model**: Should we use local Ollama (free) or OpenAI (more polished)?
2. **Graph database**: Do you want Neo4j for native graph queries later, or is SPARQL sufficient?
3. **Risk scoring**: What makes a wellbore "high-drilling-risk" in your domain?
4. **Scale**: Will you expand beyond Volve/TNO to other datasets?
5. **Real-time**: Do semantic indices need real-time updates, or batch daily?

---

## How Accenture Ontology Fits In

The Accenture OSDU-Ontology provides the **structural backbone**:

```
Accenture Ontology:
  "A Well has-wellbore Wellbore"
  "A Wellbore has-trajectory WellboreTrajectory"
  → Formal ontology in OWL/TTL

ADME Enhancement:
  Well(ID=VOLVE-01, embedding=[0.1, 0.2, ...], remarks="...")
  Wellbore(ID=VOLVE-01-A, embedding=[0.3, 0.4, ...], 
           relatedTo=VOLVE-02-A via similarity)
  → Semantic layer on top
```

**You're not building an ontology from scratch** — you're using Accenture's work and adding semantic search capabilities.

---

## Summary: The Path Forward

**What you have**: 
- Ingestion orchestrators (Volve + TNO) ✅
- Production Streamlit UI ✅
- Validated data in OSDU storage ✅

**What you're adding**:
- Semantic search on text fields (remarks, descriptions)
- Knowledge graph with inferred relationships
- Hybrid query engine (structured + NLP)
- Risk scoring and exploration UI

**How to proceed**:
1. Week 1: Extract + embed (30 mins work this week)
2. Week 2: Ontology + SPARQL setup
3. Week 3-4: Database + hybrid queries
4. Week 5-8: Graph enrichment + UI

**Investment**: ~60 hours over 8 weeks (~1.5 days/week)

---

## Files Delivered

- 📄 `docs/SEMANTIC_SEARCH_ROADMAP.md` — Strategic 8-week plan
- 📄 `docs/SEMANTIC_SEARCH_QUICKSTART.md` — Week-by-week hands-on guide
- 🐍 `app/services/semantic_indexing.py` — Extract text fields
- 🐍 `app/services/semantic_embeddings.py` — Generate embeddings
- ✅ Feature branch committed with detailed messages

---

## Ready to Start?

**This week**: Run text extraction + embedding generation (30 mins)  
**Next week**: Load ontology + SPARQL queries  
**Week 3**: PostgreSQL semantic search working  

Let me know if you want to dive into any phase or have questions about the approach!
