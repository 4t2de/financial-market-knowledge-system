"""
Embedding Service
-----------------
Generates vector embeddings using fastembed (local models).
"""

from typing import List, Optional
from fastembed import TextEmbedding
from loguru import logger
from config.settings import EMBEDDING_MODEL


class EmbeddingService:
    _instance: Optional["EmbeddingService"] = None
    _model: Optional[TextEmbedding] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EmbeddingService, cls).__new__(cls)
        return cls._instance

    def _get_model(self) -> TextEmbedding:
        if self._model is None:
            logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
            self._model = TextEmbedding(model_name=EMBEDDING_MODEL)
        return self._model

    def get_embeddings(self, text: str) -> List[float]:
        """Generate embedding for a single string."""
        if not text or not text.strip():
            return []

        model = self._get_model()
        # model.embed returns a generator
        embeddings = list(model.embed([text]))
        return embeddings[0].tolist()

    def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a list of strings."""
        if not texts:
            return []

        model = self._get_model()
        embeddings = list(model.embed(texts))
        return [e.tolist() for e in embeddings]


# Singleton instance
embedding_service = EmbeddingService()
