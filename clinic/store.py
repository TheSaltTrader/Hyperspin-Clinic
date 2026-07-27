# ChromaDB vector store wrapper (embedded, no server). Uses Chroma's
# default embedding function (ONNX MiniLM). The packaged app ships the
# model AND onnxruntime - the user never installs anything and never
# needs internet for the first index.
import functools
import os
import shutil
import sys

import chromadb

from . import config

_MODEL_SUBDIR = os.path.join("onnx_models", "all-MiniLM-L6-v2", "onnx")


def seed_onnx_cache():
    """Chroma's default embedder looks for its model in
    ~/.cache/chroma/onnx_models/... and DOWNLOADS it when absent. The
    release bundles the model next to the exe; copy it into the cache
    once so first-time indexing works offline and instantly."""
    try:
        dst = os.path.join(os.path.expanduser("~"), ".cache", "chroma",
                           _MODEL_SUBDIR)
        if os.path.isdir(dst) and os.listdir(dst):
            return True
        app_dirs = []
        if getattr(sys, "frozen", False):
            app_dirs.append(os.path.dirname(sys.executable))
        app_dirs.append(config.ROOT)
        for base in app_dirs:
            src = os.path.join(base, _MODEL_SUBDIR)
            if os.path.isdir(src) and os.listdir(src):
                os.makedirs(dst, exist_ok=True)
                for f in os.listdir(src):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
                return True
    except Exception:
        pass
    return False


@functools.lru_cache(maxsize=1)
def _client_for(path):
    # PersistentClient construction is heavyweight (SQLite + ONNX embedder
    # context) - cache one per path instead of one per call
    seed_onnx_cache()
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


def track(cfg, chunks, log=None):
    """Best-effort indexing for TRACKING records (art additions, renames,
    reverts). The file operation being tracked already succeeded - a
    broken embedder (missing onnxruntime, no model) must never fail it.
    Returns True when the vector index was updated."""
    from . import es as es_mod
    ok = True
    try:
        add_chunks(collection(cfg), chunks)
    except Exception as e:
        ok = False
        if log:
            log(f"  note: search indexing unavailable ({e}) - the "
                f"operation itself completed; continuing")
    try:
        if es_mod.available(cfg):
            es_mod.ensure_index(cfg)
            es_mod.add_chunks(cfg, chunks)
    except Exception:
        pass
    return ok


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
