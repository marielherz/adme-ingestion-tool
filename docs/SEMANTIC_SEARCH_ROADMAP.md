# ADME Semantic Search & Knowledge Graph Roadmap

**Goal**: Build semantic search and graph capabilities for OSDU data, starting with Well/Wellbore/WellboreTrajectory (WKS) entities.

---

## 1. Schema Analysis: Text Fields for Semantic Search

### Key WKS Entities (from OSDU RC 3.0.0)

**Well** (Master Data)
- **Descriptive Text Fields**:
  - `CommonName`: Human-readable well name (e.g., "VOLVE-01")
  - `Description`: Well purpose/summary (e.g., "Exploration well in North Sea")
  - `Remarks`: Free-form notes on operational history, conditions
  - `StatusTechnicalDescription`: Current technical status narrative
  - `Operator`: Company operating the well
  - `Field`: Geographic/operational context
  
**Wellbore** (Master Data)
- **Descriptive Text Fields**:
  - `CommonName`: Wellbore identifier
  - `Description`: Hole section details
  - `Remarks`: Drilling problems, lost circulation, stuck pipe events
  - `WellboreTrajectoryType`: Vertical/Deviated/Horizontal classification + description
  - `GeosectionDescription`: Stratigraphic/geological narrative
  
**WellboreTrajectory** (Work Product)
- **Descriptive Text Fields**:
  - `MD` (Measured Depth): Associated survey points
  - `Remarks`: Survey quality, tool drift, corrections applied
  - `InclinationDescription`: Well deviation narrative
  - Embedded comments in survey station data

---

## 2. Semantic Field Mapping Strategy

### 2.1 Text Fields → Embeddings

For each descriptive field, capture:
1. **Original text** (raw remarks, descriptions)
2. **Structured metadata** (MD, TVD, inclination, etc.)
3. **Temporal info** (when surveyed, when operations occurred)

**Example: Remarks Field Processing**
```python
well_remarks = "Casing leak at 3500 ft, squeezed casing, installed new liner. 
               Lost circulation 8-10 bbl/hr during drilling, total depth 12,500 ft."

# Extract semantic triples:
structured = {
  "events": [
    {"type": "casing_failure", "depth_ft": 3500, "action": "installed_liner"},
    {"type": "lost_circulation", "rate_bbl_hr": 9, "depth_range": "8-12500 ft"},
  ],
  "final_depth_ft": 12500,
  "text_embedding": <embedding_vector>  # from OpenAI, Ollama, or local model
}
```

---

## 3. Hybrid Query Design

### 3.1 Query Types

**Type A: Structured + Semantic**
```sparql
QUERY: "Find wells in the North Sea where drilling encountered lost circulation"

HYBRID LOGIC:
1. Structured filter:  Field = "North Sea"
2. Semantic search:    Embed "lost circulation" and find similar remarks
3. Combine:            Intersection of structured + semantic results

RESULT EXAMPLE:
- VOLVE-01 (Remarks: "Lost circulation 8-10 bbl/hr during drilling")
- TRO-02 (Remarks: "Severe lost circulation at 4200 ft, used lost-circulation material")
```

**Type B: Navigational (Graph-based)**
```
QUERY: "Show me all wellbores associated with VOLVE-01, their trajectories, 
        and any drilling issues documented"

GRAPH TRAVERSAL:
Well (VOLVE-01)
  ├── hasWellbore → Wellbore(VOLVE-01-A)
  │     ├── hasTrajectory → WellboreTrajectory(01-A-SRV-001)
  │     ├── hasRemarks → "Issues at 3500 ft"
  │     └── relatedTo (similarity) → Wellbore(VOLVE-02-A) [same field, similar issues]
  └── hasWellbore → Wellbore(VOLVE-01-B)
        ├── hasTrajectory → WellboreTrajectory(01-B-SRV-001)
        └── hasRemarks → "High angle deviation"
```

**Type C: Semantic Reasoning**
```
QUERY: "Which wellbores have characteristics suggesting high drilling risk?"

SEMANTIC LOGIC:
1. Embed training examples of "high-risk" wellbores:
   - Deep wells (>12000 ft)
   - High deviation (>60°)
   - Known problematic formations (e.g., "high-pressure sand", "lost circulation zones")
   
2. For each wellbore, compute semantic similarity to risk profile
   
3. Rank results by risk score
```

---

## 4. Accenture OSDU Ontology Evaluation

### 4.1 Architecture Overview

The Accenture tool converts OSDU JSON schemas → **OWL 3.0 Ontology (Turtle/TTL format)**.

**Key Components**:
1. **Class Hierarchy**: `System → MasterData → Well → Wellbore`
2. **Object Properties**: `hasWellbore`, `hasTrajectory`, `relatedTo`
3. **Data Properties**: `remarks` (string), `measureDepth` (decimal), etc.
4. **Metrics**: Richness, inheritance depth, graph diameter

### 4.2 Strengths

✅ **Automatic schema-to-ontology**: Converts all OSDU WKS definitions  
✅ **Open ontology linking**: Links to FOAF, GeoNames, Time  
✅ **Inheritance hierarchy**: Clear Well → Wellbore → WellboreTrajectory chains  
✅ **Validation tools**: Metrics calculation for ontology quality  
✅ **Extensibility**: Can add custom properties and relationships  

### 4.3 Limitations for Semantic Search

⚠️ **Schema-only**: Captures structure but NOT semantic content (remarks, descriptions)  
⚠️ **No embeddings**: OWL alone doesn't handle NLP/similarity search  
⚠️ **Limited instance data**: Ontology describes classes, not actual well instances  
⚠️ **Manual alignment**: Requires custom rules to map remarks → semantic entities  

---

## 5. Recommended Hybrid Architecture

### Phase 1: Foundation (Weeks 1-2)

**Objective**: Extract and analyze real text fields; generate embeddings.

**Tasks**:
1. Query Volve/TNO data for all Well/Wellbore/WellboreTrajectory records
2. Extract text fields:
   - `remarks`, `description`, `commonName`
   - Concatenate into unified "document" per entity
3. Choose embedding model:
   - **Production**: OpenAI `text-embedding-3-small` (1536d, $0.02/1M tokens)
   - **Local**: Ollama + Nomic Embed (7B, free, on-device)
   - **Lightweight**: Sentence-Transformers `all-MiniLM-L6-v2` (384d)
4. Generate embeddings; store in vector DB:
   - **PostgreSQL + pgvector** (reuse existing ADME infrastructure)
   - Or: **Weaviate**, **Milvus** (dedicated vector DBs)

**Deliverable**: Embeddings table + schema linking embeddings to entity IDs

---

### Phase 2: Accenture Ontology Integration (Weeks 2-3)

**Objective**: Load OSDU ontology; add semantic metadata.

**Tasks**:
1. Download/generate Accenture OSDU ontology (TTL):
   ```bash
   git clone https://github.com/Accenture/OSDU-Ontology.git
   python3 create_ontology.py --src path/to/osdu-schemas/ --dest ./osdu.ttl
   ```
2. Load TTL into triple store:
   - **Apache Jena Fuseki** (full SPARQL endpoint)
   - **Blazegraph** (fast, scalable)
   - Or: **Virtuoso** (if using PG, integrated)
3. Map Volve/TNO instances → ontology classes:
   ```turtle
   ex:Well_VOLVE-01 rdf:type osdu:Well ;
       osdu:hasCommonName "VOLVE-01" ;
       osdu:hasRemarks <content-hash> ;  # Link to embedding
       osdu:operator "Equinor" ;
       osdu:field "Volve" .
   ```
4. Add semantic relationships:
   ```turtle
   ex:Well_VOLVE-01 
       osdu:hasDrillingRisk <semantic-score> ;
       skos:related ex:Well_VOLVE-02 .  # Based on embedding similarity
   ```

**Deliverable**: Running SPARQL endpoint with instances + ontology + semantic links

---

### Phase 3: Hybrid Query Engine (Weeks 3-4)

**Objective**: Build query handler combining SPARQL + vector similarity.

**Architecture**:
```
User Query (NLP)
      ↓
[NLP Parser]
      ↓
   ├─→ [SPARQL Generator] → Triple Store (structured query)
   │
   └─→ [Embedding Generator] → Vector DB (semantic query)
      
      ↓ ↓
   [Merge Results]
      ↓
  [Rank/Score]
      ↓
  [Return Top-K]
```

**Example Query Handler (Python)**:
```python
class HybridQueryEngine:
    def __init__(self, sparql_endpoint, vector_db, embed_model):
        self.sparql = sparql_endpoint  # e.g., Fuseki
        self.vectors = vector_db       # e.g., pgvector
        self.embed = embed_model       # e.g., Ollama
    
    def query(self, query_text: str, alpha=0.6):
        """
        alpha=0.6 weights structured 60%, semantic 40%
        """
        # Structured part: extract entities and relationships
        structured_results = self.sparql.query(
            """
            SELECT ?well ?wellbore ?field WHERE {
                ?well rdf:type osdu:Well ;
                      osdu:field "North Sea" ;
                      osdu:hasWellbore ?wellbore .
            }
            """
        )
        
        # Semantic part: embed query, find similar remarks
        query_embedding = self.embed.embed(query_text)
        semantic_results = self.vectors.search(
            embedding=query_embedding,
            top_k=20,
            filter={"entity_type": "Wellbore"}  # Structured filter on vectors too
        )
        
        # Merge: combine scores
        merged = {}
        for entity_id, score in semantic_results:
            merged[entity_id] = score * (1 - alpha)
        
        for entity in structured_results:
            entity_id = entity['wellbore']
            merged[entity_id] = merged.get(entity_id, 0) + alpha
        
        return sorted(merged.items(), key=lambda x: x[1], reverse=True)
```

**Deliverable**: FastAPI endpoint supporting hybrid queries

---

### Phase 4: Knowledge Graph Enrichment (Weeks 4-6)

**Objective**: Add inferred relationships; enable graph reasoning.

**Techniques**:
1. **Similarity-based edges**: Wells with similar remarks → linked in graph
2. **Temporal reasoning**: Wellbores drilled in same season, same field → correlated
3. **Domain expertise**: Rule-based links (high deviation + lost circulation → high risk)
4. **Community detection**: Find clusters of problematic wellbores

**Example Rules (SWRL)**:
```
Well(?w) ∧ Field(?w, ?f) ∧ Well(?w2) ∧ Field(?w2, ?f) ∧ ?w ≠ ?w2
→ SameField(?w, ?w2)

Wellbore(?wb) ∧ hasRemarks(?wb, ?r) ∧ TextContains(?r, "lost circulation") 
    ∧ TextContains(?r, "high pressure")
→ HighRisk(?wb)
```

**Deliverable**: Enriched knowledge graph; inference queries

---

### Phase 5: Semantic Search UI (Weeks 6-7)

**Objective**: Streamlit page for exploring semantic relationships.

**Features**:
- Search box with NLP interpretation
- Results table with hybrid scores
- Graph visualization (networkx)
- Faceted filtering (field, depth, drilling issues)
- Explanation panel ("Why this result?")

**Example**:
```
[Search] "Which wells in the North Sea had drilling problems?"

Results:
1. VOLVE-01 (Score: 0.92)
   Field: North Sea
   Depth: 12,500 ft
   Remarks: "Lost circulation 8-10 bbl/hr, casing leak at 3500 ft"
   Reason: Matches "North Sea" (structured) + high semantic similarity to "drilling problems"
   
2. TRO-02 (Score: 0.88)
   ...

[Expand] → Show graph: VOLVE-01 connected to VOLVE-02 (similar issues)
```

**Deliverable**: Streamlit semantic search page

---

## 6. Implementation Roadmap (8 weeks)

| Week | Phase | Deliverable |
|------|-------|-------------|
| 1 | Schema Analysis | Text field extraction; embedding generation |
| 2 | Ontology | Accenture TTL + triple store loaded |
| 2-3 | Integration | Instances linked to ontology + embeddings |
| 3-4 | Query Engine | Hybrid query handler (SPARQL + vector) |
| 4-5 | Enrichment | Inferred edges; community detection |
| 5-6 | Reasoning | Rule-based classification; risk scoring |
| 6-7 | UI | Streamlit semantic search page |
| 7-8 | Testing | Integration tests; demo with real queries |

---

## 7. Technology Stack

### Core
- **Ontology**: Accenture OSDU-Ontology (OWL/TTL)
- **Triple Store**: Apache Jena Fuseki (SPARQL endpoint)
- **Vector DB**: PostgreSQL + pgvector (reuse existing infra)
- **Embedding Model**: Ollama + Nomic Embed (local, free)
- **Query Engine**: Python FastAPI
- **UI**: Streamlit

### Optional Enhancements
- **GraphDB**: Neo4j for native graph queries (Cypher)
- **Reasoning**: Pellet/HermiT OWL reasoner
- **NLP**: spaCy for NER (extract entities from remarks)
- **Visualization**: Pyvis, Plotly for interactive graphs

---

## 8. Quick Start: Local Demo (This Week)

### Step 1: Load Accenture Ontology
```bash
cd adme-ingestion-tool
git clone https://github.com/Accenture/OSDU-Ontology.git
cd OSDU-Ontology
python3 create_ontology.py --src path/to/osdu-schemas/ --dest ./
# Generates: osdu_draft.ttl
```

### Step 2: Query with SPARQL
```python
from rdflib import Graph

g = Graph()
g.parse("osdu_draft.ttl", format="turtle")

# Query: All classes in the ontology
results = g.query("""
    SELECT ?class ?comment WHERE {
        ?class rdf:type owl:Class .
        OPTIONAL { ?class rdfs:comment ?comment }
    }
    LIMIT 10
""")

for row in results:
    print(f"{row.class} - {row.comment}")
```

### Step 3: Embed Volve Remarks
```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')

remarks = [
    "Lost circulation 8-10 bbl/hr during drilling, total depth 12,500 ft",
    "Casing leak at 3500 ft, squeezed casing, installed new liner",
    "High angle deviation 65°, rotary steering tool used"
]

embeddings = model.encode(remarks)
print(f"Generated {len(embeddings)} embeddings of shape {embeddings[0].shape}")
```

### Step 4: Hybrid Search
```python
query = "wells with drilling problems"
query_embedding = model.encode([query])[0]

# Compare against remarks embeddings
from scipy.spatial.distance import cosine
scores = [1 - cosine(query_embedding, emb) for emb in embeddings]
print(sorted(zip(remarks, scores), key=lambda x: x[1], reverse=True))
```

---

## 9. Next Steps

**Action Items for You**:
1. ✅ Clone Accenture ontology; generate OSDU TTL
2. ✅ Install Jena Fuseki locally (Docker: `docker run -d -p 3030:3030 stain/jena-fuseki`)
3. ✅ Load Volve/TNO Well/Wellbore data; query via SPARQL
4. ✅ Try embedding generation with Ollama or Sentence-Transformers
5. ✅ Design hybrid query examples
6. ✅ Evaluate: Does this approach meet your semantic search goals?

**Questions to Answer**:
- Do you want to use the Accenture ontology as-is, or customize it?
- Should semantic search run on-device (Ollama) or cloud (OpenAI)?
- Do you need real-time graph updates, or is batch sufficient?

---

## References

- **OSDU Schema**: https://community.opengroup.org/osdu/data/open-test-data/-/tree/master/rc--3.0.0/3-schema
- **Accenture Ontology**: https://github.com/Accenture/OSDU-Ontology
- **Apache Jena**: https://jena.apache.org/
- **Sentence-Transformers**: https://www.sbert.net/
- **Ollama**: https://ollama.ai/ (Local LLMs/embeddings)
