"""Local embeddings: turn text into a list of numbers so "similar meaning" is "nearby numbers".

Runs on the laptop CPU (fastembed, ONNX). No API, no cost, nothing leaves the machine.
"""
import numpy as np

MODEL = "BAAI/bge-small-en-v1.5"   # 384 numbers per text, ~130 MB download once
_model = None


def _get():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL)
    return _model


def embed(texts: list[str]) -> np.ndarray:
    vecs = np.asarray(list(_get().embed(texts, batch_size=64)), dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def embed_query(text: str) -> np.ndarray:
    vec = np.asarray(next(iter(_get().query_embed(text))), dtype=np.float32)
    return vec / np.linalg.norm(vec)
