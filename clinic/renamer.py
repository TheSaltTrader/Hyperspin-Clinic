# Rename curation (tab 4): match wrongly-named files (videos / wheel art /
# roms) to the canonical rom names of ONE system's database XML.
#   - candidate retrieval via ELASTICSEARCH (a transient filename index,
#     fuzzy multi_match on rom name + description); difflib fallback with
#     a warning when the server is offline
#   - the displayed percentage is a real similarity ratio (difflib on
#     normalized names, 0-100) so a user threshold is meaningful
#   - apply(): backs up every affected file into clinic_backups\<stamp>\
#     BEFORE renaming, then renames to <rom name><original ext>
#   - every change: UI log + data\renames.log + tracking records in the
#     vector DB and Elasticsearch
import difflib
import os
import shutil
import time

from . import config
from . import es as es_mod
from . import hyperspin_db as hdb
from . import store
from .artfinder import media_paths, norm

RENAME_INDEX = "clinic-rename-scan"

TARGETS = {
    "video": {"exts": (".mp4", ".flv", ".avi"), "label": "Videos"},
    "wheel": {"exts": (".png", ".jpg"), "label": "Wheel art"},
    "rom":   {"exts": None, "label": "Roms"},   # any extension
}


def target_folder(cfg, system, target, roms_dir=""):
    if target == "rom":
        return roms_dir
    paths = media_paths(cfg, system)
    return paths["video"] if target == "video" else paths["wheel"]


def _files(folder, exts):
    out = []
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if not os.path.isfile(p):
                continue
            if exts is None or f.lower().endswith(exts):
                out.append(f)
    return out


def _pct(a, b) -> int:
    return round(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() * 100)


def _es_candidates(cfg, files, games, n=5):
    """Index the folder's filenames into a transient ES index, then fuzzy
    query per game. Returns {game_name: [filename, ...]} or None if ES is
    unavailable."""
    if not es_mod.available(cfg):
        return None
    es = es_mod.client(cfg)
    if es.indices.exists(index=RENAME_INDEX):
        es.indices.delete(index=RENAME_INDEX)
    es.indices.create(index=RENAME_INDEX, mappings={
        "properties": {"file": {"type": "keyword"},
                       "stem": {"type": "text"}}})
    ops = []
    for f in files:
        ops.append({"index": {"_index": RENAME_INDEX}})
        ops.append({"file": f, "stem": os.path.splitext(f)[0]})
    if ops:
        es.bulk(operations=ops, refresh=True)
    out = {}
    for g in games:
        q = {
            "bool": {
                "should": [
                    {"match": {"stem": {"query": g.name, "fuzziness": "AUTO"}}},
                    {"match": {"stem": {"query": g.description or g.name,
                                        "fuzziness": "AUTO"}}},
                ]
            }
        }
        try:
            res = es.search(index=RENAME_INDEX, size=n, query=q)
            out[g.name] = [h["_source"]["file"]
                           for h in res.get("hits", {}).get("hits", [])]
        except Exception:
            out[g.name] = []
    return out


def scan(cfg, system, target, roms_dir="", min_pct=60, n_candidates=4, log=print):
    """Returns (rows, es_used). rows: list of dicts
    {game, description, candidates: [(file, pct)...], chosen: file|None}.
    Only games whose canonical file is MISSING are listed, and only files
    not already canonically named are offered as candidates."""
    xml = hdb.system_xml_path(cfg["hyperspin_root"], system)
    games = hdb.parse_games(hdb.read_db_text(xml)[0])
    # hack/translation wheels (user rule, same as the Missing Art tab):
    # titles are near-misses of official games by design - only EXACT
    # name matches may be auto-selected; fuzzy candidates stay listed
    # for manual picking
    from .artfinder import strict_system
    if strict_system(system):
        min_pct = 100
        log(f"[{system}] hack-style wheel — only EXACT matches are "
            f"auto-selected (candidates still listed for manual review)")
    folder = target_folder(cfg, system, target, roms_dir)
    exts = TARGETS[target]["exts"]
    files = _files(folder, exts)
    canonical = {g.name.lower() for g in games}
    stems = {os.path.splitext(f)[0].lower() for f in files}
    loose = [f for f in files
             if os.path.splitext(f)[0].lower() not in canonical]
    missing = [g for g in games if g.name.lower() not in stems]
    log(f"[{system}] {TARGETS[target]['label']}: {len(games)} games, "
        f"{len(missing)} without canonical file, {len(loose)} loose file(s) in "
        f"{folder or '(no folder set)'}")
    if not missing or not loose:
        return [], es_mod.available(cfg)

    es_cands = _es_candidates(cfg, loose, missing, n=n_candidates * 2)
    es_used = es_cands is not None
    if not es_used:
        log("  Elasticsearch offline — difflib fallback matching")

    rows = []
    for g in missing:
        if es_used:
            cand_files = es_cands.get(g.name, [])
            if not cand_files:      # ES found nothing; try difflib to be safe
                pool = {norm(f): f for f in loose}
                close = difflib.get_close_matches(norm(g.name), list(pool), n=n_candidates, cutoff=0.3)
                cand_files = [pool[c] for c in close]
        else:
            pool = {norm(f): f for f in loose}
            close = difflib.get_close_matches(norm(g.name), list(pool), n=n_candidates, cutoff=0.3)
            cand_files = [pool[c] for c in close]
        cands = []
        seen = set()
        for f in cand_files:
            if f in seen:
                continue
            seen.add(f)
            p = max(_pct(g.name, f),
                    _pct(g.description, f) if g.description else 0)
            cands.append((f, p))
        cands.sort(key=lambda c: -c[1])
        cands = cands[:n_candidates]
        chosen = cands[0][0] if cands and cands[0][1] >= min_pct else None
        rows.append({"game": g.name, "description": g.description,
                     "candidates": cands, "chosen": chosen})
    return rows, es_used


def apply(cfg, system, target, roms_dir, decisions, log=print):
    """decisions: [(game, file)] to rename. Backs up, renames, tracks.
    Returns count renamed."""
    folder = target_folder(cfg, system, target, roms_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(folder, "clinic_backups", stamp)
    entries = []
    renamed = 0
    from .artfinder import safe_name
    for game, fname in decisions:
        if not safe_name(game) or fname != os.path.basename(fname):
            log("  ! unsafe name skipped: %r / %r" % (game, fname))
            continue
        src = os.path.join(folder, fname)
        if not os.path.isfile(src):
            log(f"  ! {fname} vanished — skipped")
            continue
        ext = os.path.splitext(fname)[1]
        dst = os.path.join(folder, game + ext)
        if os.path.exists(dst):
            log(f"  ! {game}{ext} already exists — skipped {fname}")
            continue
        os.makedirs(backup, exist_ok=True)
        shutil.copy2(src, os.path.join(backup, fname))   # integrity backup
        os.rename(src, dst)
        renamed += 1
        entries.append({"system": system, "game": game,
                        "target": target, "old": fname,
                        "new": game + ext, "folder": folder})
        log(f"  renamed '{fname}' -> '{game}{ext}'")
    if entries:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(config.DATA_DIR, "renames.log"), "a",
                  encoding="utf-8") as f:
            for e in entries:
                f.write(f"{when}\t{e['system']}\t{e['target']}\t{e['old']}"
                        f"\t->\t{e['new']}\t{e['folder']}\n")
        from .ingest import Chunk
        chunks = [Chunk(
            id=f"rename:{e['system']}:{e['target']}:{e['new']}",
            text=(f"Renamed {when}: {e['target']} '{e['old']}' -> '{e['new']}' "
                  f"for {e['game']} ({e['system']}) in {e['folder']}"),
            source=e["folder"], kind="rename",
            meta={"system": e["system"], "game": e["game"],
                  "target": e["target"], "old": e["old"], "new": e["new"],
                  "when": when},
        ) for e in entries]
        tracked = store.track(cfg, chunks, log)
        log(f"  backup: {backup}")
        if tracked:
            log(f"  tracked {len(entries)} rename(s) in the database")
    return renamed


def history_lines(limit=500):
    """Past renames for the scrollable changes box."""
    p = os.path.join(config.DATA_DIR, "renames.log")
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
        return lines[-limit:]
    except OSError:
        return []
