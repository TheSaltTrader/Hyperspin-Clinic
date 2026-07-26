# Hybrid retrieval: Elasticsearch BM25 + Chroma vector search, merged by
# reciprocal-rank fusion (robust to the two engines' different score
# scales). Falls back to vector-only when Elasticsearch is unavailable.
from . import es as es_mod
from . import store


def hybrid(cfg, text, k=8):
    ranked = {}

    def fuse(results, weight=1.0):
        for rank, r in enumerate(results):
            entry = ranked.setdefault(r["id"], {"item": r, "score": 0.0})
            entry["score"] += weight / (60 + rank)   # RRF constant 60
            # prefer keeping the longer text variant
            if len(r["text"]) > len(entry["item"]["text"]):
                entry["item"] = r

    es_ok = es_mod.available(cfg)
    if es_ok:
        try:
            fuse(es_mod.query(cfg, text, k=k * 2))
        except Exception:
            es_ok = False
    try:
        col = store.collection(cfg)
        fuse(store.query(col, text, k=k * 2))
    except Exception:
        pass

    items = sorted(ranked.values(), key=lambda e: -e["score"])[:k]
    return [dict(e["item"], fused=e["score"]) for e in items], es_ok
