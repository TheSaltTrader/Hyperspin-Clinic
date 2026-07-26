# ChromaDB vector store wrapper (embedded, no server). Uses Chroma's
# default embedding function (ONNX MiniLM, downloaded once on first use).
import functools

import chromadb

from . import config


@functools.lru_cache(maxsize=1)
def _client_for(path):
    # PersistentClient construction is heavyweight (SQLite + ONNX embedder
    # context) - cache one per path instead of one per call
    return chromadb.PersistentClient(path=path)


def client():
    return _client_for(config.chroma_dir())


def collection(cfg):
    return client().get_or_create_collection(
        cfg["chroma_collection"],
        metadata={"hnsw:space": "cosine"},
    )


def reset(cfg):
    try:
        client().delete_collection(cfg["chroma_collection"])
    except Exception:
        pass
    return collection(cfg)


def add_chunks(col, chunks, batch=128):
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        col.upsert(
            ids=[c.id for c in part],
            documents=[c.text for c in part],
            metadatas=[{"source": c.source, "kind": c.kind, **{
                k: v for k, v in c.meta.items()
                if isinstance(v, (str, int, float, bool))
            }} for c in part],
        )


def query(col, text, k=8):
    res = col.query(query_texts=[text], n_results=k)
    out = []
    for i in range(len(res["ids"][0])):
        out.append({
            "id": res["ids"][0][i],
            "text": res["documents"][0][i],
            "meta": res["metadatas"][0][i] or {},
            "score": 1.0 - (res["distances"][0][i] if res.get("distances") else 0.0),
            "engine": "vector",
        })
    return out
