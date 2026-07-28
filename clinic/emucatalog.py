# EmuMovies full catalog: the COMPLETE extracted tree (every system
# folder AND every file in it), stored per system in the database so
# lookups are direct catalog queries instead of runtime guessing:
#   - data\emumovies_catalog.json  - source of truth on disk
#   - Elasticsearch index <es_index>-emumovies - one doc per FILE, so a
#     needed snap is pulled straight from ES (term/fuzzy on the stem,
#     filtered per system folder, quality-ranked)
#   - vector DB - one summary chunk per folder (Ask AI can answer
#     "which EmuMovies folder has X and how many files")
import json
import os
import time

from . import config
from . import emumovies as emu_mod
from . import es as es_mod
from . import store

MAX_AGE_DAYS = 7


def _path():
    return os.path.join(config.DATA_DIR, "emumovies_catalog.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fresh(cat=None) -> bool:
    try:
        return (time.time() - os.path.getmtime(_path())) < MAX_AGE_DAYS * 86400
    except OSError:
        return False


def _es_index(cfg):
    return f"{cfg.get('es_index', 'hyperspin-clinic')}-emumovies"


def index_es(cfg, cat, log) -> bool:
    """One ES doc per catalog file - the direct-lookup store."""
    if not es_mod.available(cfg):
        return False
    try:
        es = es_mod.client(cfg)
        idx = _es_index(cfg)
        if es.indices.exists(index=idx):
            es.indices.delete(index=idx)
        es.indices.create(index=idx, mappings={"properties": {
            "root": {"type": "keyword"}, "folder": {"type": "keyword"},
            "quality": {"type": "keyword"}, "file": {"type": "keyword"},
            "stem": {"type": "text"}}})
        ops, n = [], 0
        for root, folders in cat["video"].items():
            q = root.split("(")[-1].rstrip(")")
            for folder, files in folders.items():
                for f in files:
                    ops.append({"index": {"_index": idx}})
                    ops.append({"root": root, "folder": folder, "quality": q,
                                "file": f, "stem": os.path.splitext(f)[0]})
                    n += 1
                    if len(ops) >= 2000:
                        es.bulk(operations=ops)
                        ops = []
        for folder, entries in cat["artwork"].items():
            for f in entries:
                ops.append({"index": {"_index": idx}})
                ops.append({"root": emu_mod.ARTWORK_ROOT, "folder": folder,
                            "quality": "art", "file": f,
                            "stem": os.path.splitext(f)[0]})
                n += 1
        if ops:
            es.bulk(operations=ops)
        es.indices.refresh(index=idx)
        log(f"  catalog: {n} files indexed in Elasticsearch ({idx})")
        return True
    except Exception as e:
        log(f"  catalog: ES indexing failed ({e}) — JSON catalog still used")
        return False


def index_vector(cfg, cat, log):
    """One summary chunk per folder for semantic search / Ask AI."""
    from .ingest import Chunk
    chunks = []
    for root, folders in cat["video"].items():
        for folder, files in folders.items():
            chunks.append(Chunk(
                id=f"emucat:{folder}",
                text=(f"EmuMovies folder '{folder}' under {root} holds "
                      f"{len(files)} video snaps."),
                source=_path(), kind="emumovies_catalog",
                meta={"root": root, "folder": folder, "files": len(files)}))
    for folder, entries in cat["artwork"].items():
        chunks.append(Chunk(
            id=f"emucat:art:{folder}",
            text=(f"EmuMovies artwork folder '{folder}' holds "
                  f"{len(entries)} packs/entries: "
                  + "; ".join(entries[:40])),
            source=_path(), kind="emumovies_catalog",
            meta={"root": emu_mod.ARTWORK_ROOT, "folder": folder}))
    store.track(cfg, chunks, log)


def _robust_crawl(make_emu, log, stop_flag):
    """Full crawl that survives dropped FTP control connections (they die
    after a few hundred rapid LISTs): per-folder retry with an automatic
    reconnect; only a folder failing twice is skipped (and logged)."""
    emu = make_emu()
    cat = {"ts": time.strftime("%Y-%m-%d %H:%M"), "video": {}, "artwork": {}}
    dirs = []
    for root in emu_mod.VIDEO_ROOTS:
        for name in emu.listdir(root):
            dirs.append(("video", root, name))
    for name in emu.listdir(emu_mod.ARTWORK_ROOT):
        dirs.append(("artwork", emu_mod.ARTWORK_ROOT, name))
    failed = []
    for i, (kind, root, name) in enumerate(dirs):
        if stop_flag and stop_flag():
            raise RuntimeError("stopped")
        for attempt in (1, 2):
            try:
                entries = emu.listdir(f"{root}/{name}")
                if kind == "video":
                    cat["video"].setdefault(root, {})[name] = [
                        f for f in entries
                        if f.lower().endswith((".mp4", ".avi", ".flv"))]
                else:
                    cat["artwork"][name] = entries
                break
            except Exception as e:
                if attempt == 1:
                    log(f"  catalog: listing '{name}' failed "
                        f"({e or type(e).__name__}) — reconnecting…")
                    try:
                        emu.close()
                    except Exception:
                        pass
                    emu = make_emu()
                else:
                    failed.append(name)
                    if kind == "video":
                        cat["video"].setdefault(root, {})[name] = []
                    else:
                        cat["artwork"][name] = []
        if (i + 1) % 25 == 0:
            log(f"  catalog: {i + 1}/{len(dirs)} folders listed…")
    try:
        emu.close()
    except Exception:
        pass
    if failed:
        log(f"  catalog: {len(failed)} folder(s) could not be listed and "
            f"were skipped: {', '.join(failed[:5])}"
            + ("…" if len(failed) > 5 else ""))
    return cat


def build(cfg, emu, log, stop_flag=None) -> "dict | None":
    """Crawl + persist + index. `emu` may be a connection OR a factory
    callable (enables mid-crawl reconnects). Returns the catalog."""
    log("  EmuMovies: extracting the FULL folder tree (files included) — "
        "one-time, refreshed weekly…")
    make_emu = emu if callable(emu) else (lambda: emu)
    try:
        cat = _robust_crawl(make_emu, log, stop_flag)
    except Exception as e:
        log(f"  catalog crawl failed: {e or type(e).__name__}")
        return None
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_path() + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cat, f)
    os.replace(_path() + ".tmp", _path())
    nv = sum(len(fl) for folders in cat["video"].values()
             for fl in folders.values())
    log(f"  catalog saved: {nv} snap files across "
        f"{sum(len(v) for v in cat['video'].values())} folders "
        f"+ {len(cat['artwork'])} artwork systems")
    index_es(cfg, cat, log)
    try:
        index_vector(cfg, cat, log)
    except Exception:
        pass
    return cat


def ensure(cfg, emu, log, stop_flag=None) -> "dict | None":
    if fresh():
        return load()
    return build(cfg, emu, log, stop_flag)


def semantic_folder(cfg, system):
    """Vector-database fallback (user rule): the whole folder tree is in
    the index, so a system whose name shares no tokens with its EmuMovies
    folder can still be matched semantically. A candidate only wins when
    its model NUMBERING agrees (CPS-2 can never serve Capcom Play System
    III) and it is close enough to trust. Returns (root, folder) or
    None; never raises."""
    try:
        col = store.collection(cfg)
        res = col.query(
            query_texts=[f"video snaps folder of the game system {system}"],
            n_results=8, where={"kind": "emumovies_catalog"})
    except Exception:
        return None
    want = emu_mod._numbers(system)
    cands = []
    for i in range(len(res["ids"][0])):
        meta = res["metadatas"][0][i] or {}
        folder, root = meta.get("folder"), meta.get("root")
        if not folder or root not in emu_mod.VIDEO_ROOTS:
            continue
        if emu_mod._numbers(folder.split(" (")[0]) != want:
            continue
        dist = (res["distances"][0][i]
                if res.get("distances") else 1.0)
        cands.append((dist, emu_mod.VIDEO_ROOTS.index(root), root, folder))
    if not cands:
        return None
    cands.sort()
    dist, _q, root, folder = cands[0]
    return (root, folder) if dist <= 0.55 else None


def video_pools(cat, system, cfg=None):
    """[(vdir, [files])] for the system, highest quality first, straight
    from the catalog — zero FTP round-trips, zero assumptions. Ladder
    (user rule): trained map -> token matcher (+known aliases) ->
    semantic search of the indexed folder tree; a semantic hit is saved
    to the trainable map so a wrong guess can be corrected by hand."""
    out = []
    if not cat:
        return out
    trained = emu_mod.load_map().get(f"video::{system}") or []
    if isinstance(trained, str):
        trained = [trained]
    trained = {t.rsplit("/", 1)[-1] for t in trained}
    for root in emu_mod.VIDEO_ROOTS:
        folders = cat.get("video", {}).get(root, {})
        hit = next((t for t in trained if t in folders), None)
        if not hit:
            hit = emu_mod.resolve_name(system, list(folders))
        if hit:
            out.append((f"{root}/{hit}", folders[hit]))
    if not out and cfg is not None:
        sem = semantic_folder(cfg, system)
        if sem:
            root, folder = sem
            out.append((f"{root}/{folder}", cat["video"][root][folder]))
            m = emu_mod.load_map()
            m[f"video::{system}"] = folder
            emu_mod.save_map(m)
    return out


def es_candidates(cfg, system, folders, games, n=3):
    """Direct ES pull: fuzzy stem search filtered to the system's catalog
    folders. Returns {game_name: [(folder_path, file), ...]} or None."""
    if not es_mod.available(cfg):
        return None
    try:
        es = es_mod.client(cfg)
        idx = _es_index(cfg)
        if not es.indices.exists(index=idx):
            return None
        folder_names = [f.rsplit("/", 1)[-1] for f in folders]
        out = {}
        for g in games:
            q = {"bool": {
                "filter": [{"terms": {"folder": folder_names}}],
                "should": [
                    {"match": {"stem": {"query": g.name, "fuzziness": "AUTO"}}},
                    {"match": {"stem": {"query": g.description or g.name,
                                        "fuzziness": "AUTO"}}}]}}
            res = es.search(index=idx, size=n, query=q)
            out[g.name] = [(f"{h['_source']['root']}/{h['_source']['folder']}",
                            h["_source"]["file"])
                           for h in res.get("hits", {}).get("hits", [])]
        return out
    except Exception:
        return None
