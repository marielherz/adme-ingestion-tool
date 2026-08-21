# Parallel Execution Guide: Semantic Search Tracks A & B

**Status**: Ready to execute (both tracks approved)  
**Goal**: Build semantic index for Well/Wellbore + WellboreTrajectory by end of week  
**Timeline**: 1-2 hours total (parallel execution)

---

## Overview

Two independent tracks running simultaneously:

| Track | Goal | Duration | Input | Output |
|-------|------|----------|-------|--------|
| **A** | Extract Well + Wellbore for quick semantic index | 15-20 min | Volve manifests (48 Wells + 164 Wellbores) | `.semantic-index.jsonl` (212 documents) |
| **B** | Generate WellboreTrajectory OSDU manifests | 20-30 min | NPD-3145.csv (Volve survey data) | `.wellbore-trajectories.jsonl` (trajectory records) |

Both outputs feed into embedding pipeline (30-60 min depending on model choice).

---

## Track A: Extract Well + Wellbore (Quick Win)

**Purpose**: Use existing Volve manifest files to build searchable text index  
**Dependencies**: `semantic_indexing.py` (already committed)  
**Time**: 15-20 minutes

### Step 1: Run extraction

```powershell
cd C:\Users\marielherzog\adme-ingestion-tool

# Option 1: Run as Python module (recommended)
python -m app.services.semantic_indexing

# Option 2: Run via Python directly
python .\app\services\semantic_indexing.py
```

**Expected output**:
```
ADME Semantic Indexing - Track A: Well/Wellbore Extraction
============================================================

1. Extracting from Volve master-data...
   Source: C:\Users\marielherzog\osdu-data\volve\generated-json\provided\master-data
   ✓ 48 Wells extracted
   ✓ 164 Wellbores extracted
   ✗ Trajectory file not available (run Track B generator first)

2. Saving semantic index...

3. Index Statistics:
   Total documents: 212
   By type: {'Well': 48, 'Wellbore': 164}
   Documents with text: 212
   Avg text length: 145 chars

✓ Ready for embedding generation (semantic_embeddings.py)
```

### Step 2: Verify output

```powershell
# Check that .semantic-index.jsonl was created
ls C:\Users\marielherzog\adme-ingestion-tool\.semantic-index.jsonl

# View first record (should have id, kind, entity_type, description)
Get-Content C:\Users\marielherzog\adme-ingestion-tool\.semantic-index.jsonl -Head 1 | ConvertFrom-Json | Format-List
```

---

## Track B: Generate WellboreTrajectory Manifests (Enhanced Path)

**Purpose**: Convert Volve CSV trajectory data to OSDU-compliant manifests for semantic search  
**Dependencies**: `wellbore_trajectory_generator.py` (new module, already committed)  
**Time**: 20-30 minutes (includes parsing CSV + generating OSDU records)

### Step 1: Run generator

```powershell
cd C:\Users\marielherzog\adme-ingestion-tool

# Run the generator (looks for NPD-3145.csv automatically)
python .\app\services\wellbore_trajectory_generator.py
```

**Expected output**:
```
Wellbore Trajectory Manifest Generator
=====================================

Searching for trajectory data in: C:\Users\marielherzog\osdu-data\volve\Volve\work-products\trajectories_1_1_0\inputdata
Found 1 CSV file(s):
  - NPD-3145.csv

Generating trajectory manifests...
  ✓ NPD-3145.csv → 2289 stations

Manifest Statistics:
  total_trajectories: 1
  total_survey_stations: 2289
  avg_stations_per_trajectory: 2289.0

✓ Ready for semantic indexing with WellboreTrajectory records
```

### Step 2: Verify output

```powershell
# Check that .wellbore-trajectories.jsonl was created
ls C:\Users\marielherzog\adme-ingestion-tool\.wellbore-trajectories.jsonl

# View single trajectory record
Get-Content C:\Users\marielherzog\adme-ingestion-tool\.wellbore-trajectories.jsonl | ConvertFrom-Json | Format-List
```

### Step 3 (Optional): Merge into Track A index

After both tracks complete, re-run Track A to include trajectory data:

```powershell
# Re-run semantic_indexing.py - it will now detect the .wellbore-trajectories.jsonl file
python .\app\services\semantic_indexing.py
```

**Expected updated statistics**:
```
3. Index Statistics:
   Total documents: 213          # Now includes 1 trajectory
   By type: {'Well': 48, 'Wellbore': 164, 'WellboreTrajectory': 1}
   Documents with text: 213
   Avg text length: 1,234 chars  # Trajectory has much richer text
```

---

## Parallel Execution (Two Terminals)

### Terminal 1: Track A (Quick Index)
```powershell
# Terminal 1
cd C:\Users\marielherzog\adme-ingestion-tool
python .\app\services\semantic_indexing.py

# Takes ~15-20 seconds
# Produces: .semantic-index.jsonl (Well + Wellbore)
```

### Terminal 2: Track B (Trajectory Generation)
```powershell
# Terminal 2 (run simultaneously in separate window)
cd C:\Users\marielherzog\adme-ingestion-tool
python .\app\services\wellbore_trajectory_generator.py

# Takes ~20-30 seconds (CSV parsing)
# Produces: .wellbore-trajectories.jsonl
```

**Total time**: ~30-40 seconds (parallel) vs. ~40-50 seconds (sequential)

---

## Next Step: Generate Embeddings (After Both Tracks)

Once both `.semantic-index.jsonl` and `.wellbore-trajectories.jsonl` are created:

### Option 1: Use Only Track A Output (Quick POC)
```powershell
# Use the Well + Wellbore index for immediate embedding proof-of-concept
python -c "
from app.services.semantic_embeddings import OllamaEmbedding, EmbeddingPipeline
from pathlib import Path

embedding = OllamaEmbedding()  # Requires Ollama running on localhost:11434
pipeline = EmbeddingPipeline(embedding)
count = pipeline.process_jsonl(
    Path.home() / 'adme-ingestion-tool' / '.semantic-index.jsonl',
    Path.home() / 'adme-ingestion-tool' / '.semantic-index-embedded.jsonl',
    batch_size=16
)
print(f'Generated embeddings for {count} documents')
"
```

### Option 2: Merge Then Embed (Full POC)
```powershell
# Re-run Track A to include trajectories, then embed all
python .\app\services\semantic_indexing.py

# Then embed combined index
python -c "
from app.services.semantic_embeddings import OllamaEmbedding, EmbeddingPipeline
embedding = OllamaEmbedding()
pipeline = EmbeddingPipeline(embedding)
pipeline.process_jsonl(
    '.semantic-index.jsonl',
    '.semantic-index-embedded.jsonl'
)
"
```

**Embedding Time Estimates** (with local models):
- Ollama (nomic-embed-text, 768d): 30-60 seconds for ~212 docs
- Sentence-Transformers (all-MiniLM-L6-v2, 384d): 10-20 seconds
- OpenAI API (text-embedding-3-small, 1536d): 5-10 seconds + network latency

---

## Data Locations Reference

| Track | Component | Path |
|-------|-----------|------|
| **A** | Well manifests | `~/osdu-data/volve/generated-json/provided/master-data/Well/load_Well.jsonl` |
| **A** | Wellbore manifests | `~/osdu-data/volve/generated-json/provided/master-data/Wellbore/load_Wellbore.jsonl` |
| **A** | Output index | `~/adme-ingestion-tool/.semantic-index.jsonl` |
| **B** | Trajectory CSV | `~/osdu-data/volve/Volve/work-products/trajectories_1_1_0/inputdata/NPD-3145.csv` |
| **B** | Output manifests | `~/adme-ingestion-tool/.wellbore-trajectories.jsonl` |
| **Embedding** | Input | `.semantic-index.jsonl` or merged with trajectories |
| **Embedding** | Output | `.semantic-index-embedded.jsonl` (with embedding vectors) |

---

## Troubleshooting

### Track A: File Not Found
- **Issue**: `Well file not found` or `Wellbore file not found`
- **Solution**: Verify paths in `~/osdu-data/volve/generated-json/provided/master-data/`
  ```powershell
  ls "$env:USERPROFILE\osdu-data\volve\generated-json\provided\master-data\"
  ```
- If files are named differently (e.g., `Well.jsonl` instead of `load_Well.jsonl`), update the path in `semantic_indexing.py`

### Track B: CSV Parsing Errors
- **Issue**: `Row N: Missing measured depth, skipping`
- **Solution**: Check CSV format - column headers should be: MD, TVD, INC, AZ, DLS, TF, or similar
  ```powershell
  gc "$env:USERPROFILE\osdu-data\volve\Volve\work-products\trajectories_1_1_0\inputdata\NPD-3145.csv" -Head 3
  ```

### Embedding: Ollama Not Running
- **Issue**: `Connection refused` when running embedding pipeline
- **Solution**: Start Ollama service before embedding
  ```powershell
  ollama serve
  # In another terminal:
  ollama pull nomic-embed-text
  ```

---

## Success Criteria

✅ **Track A Complete**:
- [ ] `.semantic-index.jsonl` file exists
- [ ] Contains 212 documents (48 Wells + 164 Wellbores)
- [ ] Each record has `entity_id`, `entity_type`, `description` fields

✅ **Track B Complete**:
- [ ] `.wellbore-trajectories.jsonl` file exists  
- [ ] Contains 1 trajectory record with 2289 survey stations
- [ ] Trajectory has Description, AcquisitionRemark, SurveyType fields populated

✅ **Embedding Ready**:
- [ ] Both `.semantic-index.jsonl` and `.wellbore-trajectories.jsonl` exist
- [ ] Ready to run embedding pipeline (Week 1 completion)
- [ ] Next: PostgreSQL + pgvector setup (Week 3)

---

## Week 1-2 Summary

| Week | Track A | Track B |
|------|---------|---------|
| **Week 1** | ✅ Extract Well/Wellbore<br/>✅ Generate embeddings<br/>✅ Save to local JSONL | ✅ Generate trajectory manifests<br/>✅ Parse CSV survey data<br/>✅ Create OSDU records |
| **Week 2** | → Load into PostgreSQL | → Merge with Track A<br/>→ Generate embeddings<br/>→ Load into PostgreSQL |

Both tracks complete by **end of Week 1**, ready for PostgreSQL + pgvector setup **Week 3**.
