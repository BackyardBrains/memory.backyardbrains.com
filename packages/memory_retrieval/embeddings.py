"""
Wrapper for SentenceTransformers local embedding generation.
Using BAAI/bge-small-en-v1.5 which is very fast and produces 384-dimensional vectors.
"""
import os
import threading
from typing import List

from sentence_transformers import SentenceTransformer

# Lock to ensure smooth lazy loading
_model_lock = threading.Lock()
_model = None

# We use BAAI bge-small because it performs exceptionally well and matches our Vector(384) schema.
MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                print(f"Loading embedding model {MODEL_NAME}...")
                _model = SentenceTransformer(MODEL_NAME)
                print(f"Model {MODEL_NAME} loaded.")
    return _model


def compute_embedding(text: str) -> List[float]:
    """
    Compute exactly one 384-dimensional dense vector for a single text string.
    BGE models recommend prefixing query contexts, but for chunks, raw string is fine.
    """
    model = get_model()
    # encode() returns a numpy array, we cast to list of floats for pgvector
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def compute_embeddings(texts: List[str]) -> List[List[float]]:
    """Batch compute embeddings for multiple strings."""
    if not texts:
        return []
    model = get_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]
