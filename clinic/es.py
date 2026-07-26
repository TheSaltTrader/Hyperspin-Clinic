# Elasticsearch wrapper. Everything degrades gracefully when the server
# is missing: available() gates all calls and the UI shows a warning.
import functools

from elasticsearch import Elasticsearch

MAPPING = {
    "properties": {
        "text": {"type": "text"},
        "source": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "game": {"type": "keyword"},
        "heading": {"type": "text"},
        "is_16x9_marked": {"type": "boolean"},
    }
}


@functools.lru_cache(maxsize=2)
def _client_for(url):
    return Elasticsearch(url, request_timeout=10)


def client(cfg):
    return _client_for(cfg["es_url"])


def available(cfg) -> bool:
    try:
        return bool(client(cfg).ping())
    except Exception:
        return False


def ensure_index(cfg, reset=False):
    es = client(cfg)
    idx = cfg["es_index"]
    if reset and es.indices.exists(index=idx):
        es.indices.delete(index=idx)
    if not es.indices.exists(index=idx):
        es.indices.create(index=idx, mappings=MAPPING)
    return es


def add_chunks(cfg, chunks, batch=200):
    es = client(cfg)
    idx = cfg["es_index"]
    ops = []
    for c in chunks:
        ops.append({"index": {"_index": idx, "_id": c.id}})
        ops.append({
            "text": c.text,
            "source": c.source,
            "kind": c.kind,
            "game": c.meta.get("game", ""),
            "heading": c.meta.get("heading", ""),
            "is_16x9_marked": bool(c.meta.get("is_16x9_marked", False)),
        })
        if len(ops) >= batch * 2:
            es.bulk(operations=ops, refresh=False)
            ops = []
    if ops:
        es.bulk(operations=ops, refresh=True)


def query(cfg, text, k=8):
    es = client(cfg)
    res = es.search(index=cfg["es_index"], size=k, query={
        "multi_match": {
            "query": text,
            "fields": ["text^1", "heading^2", "game^3"],
        }
    })
    hits = res.get("hits", {}).get("hits", [])
    top = hits[0]["_score"] if hits else 1.0
    out = []
    for h in hits:
        src = h.get("_source", {})
        out.append({
            "id": h["_id"],
            "text": src.get("text", ""),
            "meta": {k2: v for k2, v in src.items() if k2 != "text"},
            "score": (h["_score"] / top) if top else 0.0,   # normalize 0..1
            "engine": "elasticsearch",
        })
    return out
