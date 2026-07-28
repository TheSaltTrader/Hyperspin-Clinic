# Cleanup tab backend: find and remove ORPHAN art - files sitting in a
# system's Media folders that do not belong to any game in the system's
# database XML. Rules (user-set):
#   - only TOP-LEVEL files of each art folder are considered; folders
#     inside an art folder are never touched
#   - default.zip under Themes is never touched
#   - "delete" = move into clinic_backups\orphans_<stamp>\ inside the
#     art folder (app rule: every destructive step backs up first),
#     logged to data\cleanup.log and tracked in the database
import os
import time

from . import config
from . import hyperspin_db as hdb
from . import store
from .artfinder import media_paths

KINDS = ("wheel", "video", "theme")
_EXTS = {
    "wheel": (".png", ".jpg", ".jpeg", ".gif", ".bmp"),
    "video": (".mp4", ".flv", ".avi"),
    "theme": (".zip",),
}


class StopRequested(Exception):
    pass


# cross-system protection (user rule): the MAME video folder is shared -
# subset wheels play videos from it. A video may only be removed when NO
# system in the Main Menu lists a game of that name. Cached per root so
# the per-system analysis doesn't re-parse hundreds of XMLs each line.
_names_cache = {"root": None, "ts": 0.0, "names": frozenset()}


def all_game_names(cfg) -> frozenset:
    root = cfg.get("hyperspin_root", "")
    if (_names_cache["root"] == root
            and time.time() - _names_cache["ts"] < 120):
        return _names_cache["names"]
    names = set()
    for system in hdb.list_systems(root):
        try:
            xml = hdb.system_xml_path(root, system)
            for g in hdb.parse_games(hdb.read_db_text(xml)[0]):
                names.add(g.name.lower())
        except OSError:
            pass
    _names_cache.update(root=root, ts=time.time(), names=frozenset(names))
    return _names_cache["names"]


def _folders(cfg, system):
    p = media_paths(cfg, system)
    theme_dir = os.path.join(os.path.dirname(p["video"]), "Themes")
    return {"wheel": p["wheel"], "video": p["video"], "theme": theme_dir}


def orphans(cfg, system, kinds=KINDS):
    """{kind: [filename, ...]} of top-level files whose stem matches no
    game in the system XML (case-insensitive). VIDEOS are additionally
    checked against EVERY system in the Main Menu - a video any other
    system uses (shared MAME folder) is never an orphan; its count goes
    into out['shared_video']. Raises OSError when the XML is unreadable."""
    xml = hdb.system_xml_path(cfg["hyperspin_root"], system)
    games = hdb.parse_games(hdb.read_db_text(xml)[0])
    valid = {g.name.lower() for g in games}
    out = {"shared_video": 0}
    folders = _folders(cfg, system)
    everywhere = None
    for kind in kinds:
        folder = folders[kind]
        found = []
        if os.path.isdir(folder):
            for e in os.scandir(folder):
                if not e.is_file():
                    continue                    # never touch subfolders
                stem, ext = os.path.splitext(e.name)
                if ext.lower() not in _EXTS[kind]:
                    continue
                if kind == "theme" and e.name.lower() == "default.zip":
                    continue                    # HyperSpin's fallback theme
                s = stem.lower()
                if s in valid:
                    continue
                if kind == "video":
                    if everywhere is None:
                        everywhere = all_game_names(cfg)
                    if s in everywhere:
                        out["shared_video"] += 1
                        continue                # another system uses it
                found.append(e.name)
        out[kind] = sorted(found)
    return out


def stats_line(cfg, system):
    """(text, ok) for the tab's per-system analysis line."""
    try:
        o = orphans(cfg, system)
    except OSError:
        return "no database XML for this system", False
    parts = []
    if o["wheel"]:
        parts.append(f"{len(o['wheel'])} wheel(s)")
    if o["video"]:
        parts.append(f"{len(o['video'])} video(s)")
    if o["theme"]:
        parts.append(f"{len(o['theme'])} theme(s)")
    shared = f" ({o['shared_video']} video(s) shared with other systems, kept)" \
        if o.get("shared_video") else ""
    if not parts:
        return "✓ no extra art (everything matches the XML)" + shared, True
    total = len(o["wheel"]) + len(o["video"]) + len(o["theme"])
    # total FIRST: it is the numeric key the list's Missing sort uses
    return (f"⚠ {total} extra file(s) not in the XML: " + ", ".join(parts)
            + shared, False)


def clean_system(cfg, system, kinds, log, stop_flag):
    """Move every orphan of the chosen kinds to a backup folder. Returns
    the number of files removed."""
    try:
        o = orphans(cfg, system, kinds)
    except OSError as e:
        log(f"[{system}] SKIP: {e}")
        return 0
    folders = _folders(cfg, system)
    if o.get("shared_video"):
        log(f"[{system}] {o['shared_video']} video(s) kept — their names "
            f"are used by OTHER systems (shared folder protection)")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    removed = []
    for kind in kinds:
        names = o.get(kind, [])
        if not names:
            continue
        folder = folders[kind]
        bdir = os.path.join(folder, "clinic_backups", f"orphans_{stamp}")
        for name in names:
            if stop_flag():
                raise StopRequested()
            src = os.path.join(folder, name)
            try:
                os.makedirs(bdir, exist_ok=True)
                os.replace(src, os.path.join(bdir, name))
                removed.append((kind, name, bdir))
                log(f"  - {name} ({kind}) → clinic_backups\\orphans_{stamp}")
            except OSError as e:
                log(f"    could not remove {name}: {e}")
    if removed:
        _track(cfg, system, removed, log)
    log(f"[{system}] {len(removed)} orphan file(s) removed "
        f"(backed up, restorable)")
    return len(removed)


def _track(cfg, system, removed, log):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(config.DATA_DIR, "cleanup.log"), "a",
              encoding="utf-8") as f:
        for kind, name, bdir in removed:
            f.write(f"{stamp}\t{system}\t{kind}\t{name}\t{bdir}\n")
    from .ingest import Chunk
    chunks = [Chunk(
        id=f"cleanup:{system}:{kind}:{name}",
        text=(f"Orphan art removed {stamp}: {kind} '{name}' of {system} "
              f"was not in the system XML — backed up to {bdir}"),
        source=bdir, kind="art_removed",
        meta={"system": system, "art": kind, "file": name,
              "removed": stamp},
    ) for kind, name, bdir in removed]
    if store.track(cfg, chunks, log):
        log(f"  tracked {len(chunks)} removal(s) in the database")
