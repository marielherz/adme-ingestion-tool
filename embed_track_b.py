#!/usr/bin/env python
"""Embed Track B (WellboreTrajectory) using OpenAI or Microsoft Foundry."""

import os
from pathlib import Path

from app.services.semantic_embeddings import EmbeddingPipeline, OpenAIEmbedding
from app.services.semantic_indexing import SemanticIndexBuilder

print("Semantic Embedding - OpenAI (Track B: WellboreTrajectory)")
print("=" * 60)
print()

# Check for either a Foundry endpoint or a direct OpenAI API key.
azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("OPENAI_BASE_URL")
openai_api_key = os.getenv("OPENAI_API_KEY")
if not azure_endpoint and not openai_api_key:
    print("[INFO] Embedding provider is not configured.")
    print()
    print("For Microsoft Foundry with Microsoft Entra ID:")
    print("  Set AZURE_OPENAI_ENDPOINT to the deployment endpoint.")
    print("  Run az login, then re-run this script.")
    print()
else:
    deployment_name = os.getenv(
        "AZURE_OPENAI_DEPLOYMENT",
        "text-embedding-3-small",
    )
    print(f"Initializing embedding deployment: {deployment_name}")
    embedding = OpenAIEmbedding(model=deployment_name)
    print(f"Provider: {embedding.provider}")
    print(f"Authentication: {embedding.auth_method}")

    # Extract searchable text from the generated OSDU trajectory manifest.
    project_root = Path(__file__).resolve().parent
    trajectory_manifest = project_root / ".wellbore-trajectories.jsonl"
    semantic_index_base = project_root / ".wellbore-trajectories-semantic"
    index_builder = SemanticIndexBuilder(semantic_index_base)
    extracted_count = index_builder.add_from_jsonl(
        trajectory_manifest,
        "WellboreTrajectory",
    )
    if extracted_count == 0:
        raise RuntimeError("No WellboreTrajectory records were extracted")
    semantic_input = index_builder.save_index("jsonl")
    print(f"Extracted {extracted_count} semantic trajectory document(s)")

    # Process Track B (WellboreTrajectory only).
    print()
    print("TRACK B: Embedding WellboreTrajectory manifests...")
    pipeline_b = EmbeddingPipeline(embedding)
    count_b = pipeline_b.process_jsonl(
        semantic_input,
        project_root / ".wellbore-trajectories-embedded.jsonl",
        batch_size=1,
    )
    print(f"  [OK] Embedded {count_b} trajectory records")
    print("  Output: .wellbore-trajectories-embedded.jsonl")
    print(f"  Dimensions: {embedding.embedding_dim} ({deployment_name})")
    print()
    print("[COMPLETE] Track B embedding ready for PostgreSQL pgvector loading")
