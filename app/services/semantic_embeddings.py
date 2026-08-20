"""
Generate embeddings for semantic documents and store in PostgreSQL + pgvector.

Supports both local (Ollama) and cloud (OpenAI) embedding models.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from abc import ABC, abstractmethod

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingModel(ABC):
    """Abstract base for embedding models."""
    
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        pass
    
    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        pass


class OllamaEmbedding(EmbeddingModel):
    """Local embeddings via Ollama (nomic-embed-text)."""
    
    def __init__(self, model: str = "nomic-embed-text", host: str = "http://localhost:11434"):
        """Initialize Ollama embedding client.
        
        Args:
            model: Ollama model name (e.g., "nomic-embed-text", "mxbai-embed-large")
            host: Ollama server URL
        """
        self.model = model
        self.host = host
        self.embedding_dim = None
        
        try:
            import requests
            self.requests = requests
        except ImportError:
            raise ImportError("requests library required. Install: pip install requests")
        
        # Test connectivity
        try:
            self._test_connection()
        except Exception as e:
            logger.warning(f"Ollama not available at {host}: {e}")
    
    def _test_connection(self) -> None:
        """Test if Ollama server is reachable."""
        response = self.requests.get(f"{self.host}/api/tags", timeout=5)
        response.raise_for_status()
    
    def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        response = self.requests.post(
            f"{self.host}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=60,
        )
        response.raise_for_status()
        
        result = response.json()
        embedding = result.get("embedding", [])
        
        if not self.embedding_dim and embedding:
            self.embedding_dim = len(embedding)
            logger.info(f"Embedding dimension: {self.embedding_dim}")
        
        return embedding
    
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        embeddings = []
        for i, text in enumerate(texts):
            try:
                emb = self.embed(text)
                embeddings.append(emb)
                if (i + 1) % 10 == 0:
                    logger.info(f"Generated {i + 1}/{len(texts)} embeddings")
            except Exception as e:
                logger.error(f"Error embedding text {i}: {e}")
                embeddings.append([])  # Empty embedding
        
        return embeddings


class SentenceTransformerEmbedding(EmbeddingModel):
    """Local embeddings via Sentence-Transformers."""
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """Initialize Sentence-Transformers model.
        
        Args:
            model_name: HuggingFace model identifier
        """
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers required. Install: pip install sentence-transformers"
            )
        
        self.model = SentenceTransformer(model_name)
        self.embedding_dim = self.model.get_sentence_embedding_dimension()
        logger.info(f"Loaded {model_name} (dim={self.embedding_dim})")
    
    def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        embedding = self.model.encode([text], convert_to_numpy=True)[0]
        return embedding.tolist()
    
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        embeddings = self.model.encode(texts, convert_to_numpy=True, show_progress_bar=True)
        return [emb.tolist() for emb in embeddings]


class OpenAIEmbedding(EmbeddingModel):
    """Cloud embeddings via OpenAI API."""
    
    def __init__(self, model: str = "text-embedding-3-small", api_key: Optional[str] = None):
        """Initialize OpenAI embedding client.
        
        Args:
            model: OpenAI model (e.g., "text-embedding-3-small", "text-embedding-3-large")
            api_key: OpenAI API key (default: OPENAI_API_KEY env var)
        """
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment")
        
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai library required. Install: pip install openai")
        
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.embedding_dim = 1536 if "small" in model else 3072
    
    def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        response = self.client.embeddings.create(input=text, model=self.model)
        return response.data[0].embedding
    
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts (batched for efficiency)."""
        response = self.client.embeddings.create(input=texts, model=self.model)
        # Sort by index to maintain order
        sorted_data = sorted(response.data, key=lambda x: x.index)
        return [item.embedding for item in sorted_data]


@dataclass
class EmbeddedDocument:
    """A semantic document with its embedding."""
    
    entity_id: str
    entity_type: str
    entity_kind: str
    common_name: str
    searchable_text: str
    embedding: list[float]
    embedding_model: str
    embedding_dim: int
    metadata: dict  # Structured fields (field_name, measured_depth, etc.)


class EmbeddingPipeline:
    """Pipeline to generate embeddings for semantic documents."""
    
    def __init__(self, embedding_model: EmbeddingModel):
        self.model = embedding_model
    
    def process_jsonl(self, input_file: Path, output_file: Path, batch_size: int = 32) -> int:
        """Process semantic documents from JSONL, add embeddings, save.
        
        Returns: Number of documents processed
        """
        documents = []
        embedded_count = 0
        
        # Load documents
        logger.info(f"Loading documents from {input_file}...")
        if not input_file.exists():
            logger.error(f"File not found: {input_file}")
            return 0
        
        with open(input_file) as f:
            documents = [json.loads(line) for line in f if line.strip()]
        
        logger.info(f"Loaded {len(documents)} documents")
        
        # Extract texts for embedding
        texts_to_embed = []
        for doc in documents:
            searchable_text = " ".join(filter(None, [
                doc.get("common_name", ""),
                doc.get("description", ""),
                doc.get("remarks", ""),
                f"Field: {doc.get('field_name')}" if doc.get("field_name") else "",
                f"Operator: {doc.get('operator')}" if doc.get("operator") else "",
            ]))
            texts_to_embed.append(searchable_text.strip())
        
        # Generate embeddings in batches
        logger.info(f"Generating embeddings (model: {self.model.__class__.__name__})...")
        all_embeddings = []
        for i in range(0, len(texts_to_embed), batch_size):
            batch_texts = texts_to_embed[i:i+batch_size]
            batch_embeddings = self.model.embed_batch(batch_texts)
            all_embeddings.extend(batch_embeddings)
            
            if (i + batch_size) % (batch_size * 10) == 0 or (i + batch_size) >= len(texts_to_embed):
                logger.info(f"  {min(i + batch_size, len(texts_to_embed))}/{len(texts_to_embed)} embeddings")
        
        # Combine documents with embeddings
        logger.info(f"Writing embedded documents to {output_file}...")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, "w") as f:
            for doc, embedding, text in zip(documents, all_embeddings, texts_to_embed):
                if not embedding:  # Skip failed embeddings
                    logger.warning(f"Skipping document {doc.get('entity_id')} (empty embedding)")
                    continue
                
                doc["embedding"] = embedding
                doc["embedding_model"] = self.model.__class__.__name__
                doc["embedding_dim"] = len(embedding)
                doc["searchable_text"] = text
                
                f.write(json.dumps(doc) + "\n")
                embedded_count += 1
        
        logger.info(f"Saved {embedded_count} embedded documents to {output_file}")
        return embedded_count


# Example usage
if __name__ == "__main__":
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    # Choose embedding model
    model_choice = os.getenv("EMBEDDING_MODEL", "sentence-transformers").lower()
    
    if model_choice == "ollama":
        embedding = OllamaEmbedding()
    elif model_choice == "openai":
        embedding = OpenAIEmbedding()
    else:
        embedding = SentenceTransformerEmbedding()  # Default
    
    # Process Volve semantic index
    input_file = Path.home() / "adme-ingestion-tool" / ".semantic-index.jsonl"
    output_file = Path.home() / "adme-ingestion-tool" / ".semantic-index-embedded.jsonl"
    
    pipeline = EmbeddingPipeline(embedding)
    
    print(f"Embedding documents from {input_file}...")
    count = pipeline.process_jsonl(input_file, output_file)
    print(f"✓ Embedded {count} documents")
    print(f"  Output: {output_file}")
    
    # Show sample
    if output_file.exists():
        print("\nSample embedded document:")
        with open(output_file) as f:
            sample = json.loads(f.readline())
            print(f"  Entity: {sample['entity_id']} ({sample['entity_type']})")
            print(f"  Text: {sample['searchable_text'][:100]}...")
            print(f"  Embedding dim: {sample['embedding_dim']}")
            print(f"  Embedding (first 5): {sample['embedding'][:5]}")
