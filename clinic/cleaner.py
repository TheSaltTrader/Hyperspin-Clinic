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


# cross-system protection (user rule, refined after the 'MAME 2 Players
# shows zero extras' report): subset wheels fall back to the PARENT MAME
# system's video folder - so only the PARENT's folder is shared. A video
# there may be removed only when every other system that lists the game
# has its own copy. A subset's own folder is read by nobody else and gets
# normal per-system cleanup. Cached per session (a TTL that expired while
# the analysis thread walked ~400 systems made it re-parse every XML over
# and over - the tab looked stuck at 'analyzing').
_share_cache = {"root": None, "usage": {}, "parent": None, "vstems": {}}


def _share_info(cfg):
    """(usage: name->[systems], parent_system) for the current root."""
    root = cfg.get("hyperspin_root", "")
    if _share_cache["root"] == root:
        return _share_cache["usage"], _share_cache["parent"]
    usage, best, best_n = {}, None, 0
    for system in hdb.list_systems(root):
        try:
            xml = hdb.system_xml_path(root, system)
            games = hdb.parse_games(hdb.read_db_text(xml)[0])
        except OSError:
            continue
        for g in games:
            usage.setdefault(g.name.lower(), []).append(system)
        if len(games) > best_n:
            best, best_n = system, len(games)
    _share_cache.update(root=root, usage=usage, parent=best, vstems={})
    return usage, best


def invalidate_cache():
    _share_cache["root"] = None


def _own_video_stems(cfg, system) -> set:
    if system not in _share_cache["vstems"]:
        stems = set()
        folder = _folders(cfg, system)["video"]
        if os.path.isdir(folder):
            for e in os.scandir(folder):
                if e.is_file():
                    stem, ext = os.path.splitext(e.name)
                    if ext.lower() in _EXTS["video"]:
                        stems.add(stem.lower())
        _share_cache["vstems"][system] = stems
    return _share_cache["vstems"][system]


def _folders(cfg, system):
    p = media_paths(cfg, system)
    theme_dir = os.path.join(os.path.dirname(p["video"]), "Themes")
    return {"wheel": p["wheel"], "video": p["video"], "theme": theme_dir}


def orphans(cfg, system, kinds=KINDS):
    """{kind: [filename, ...]} of top-level files whose stem matches no
    game in the system XML (case-insensitive). When SYSTEM IS THE PARENT
    (the largest system - the folder subset wheels fall back to), a video
    is additionally protected while any other system lists the game and
    lacks its own copy; the count goes into out['shared_video'].
    out['missing_dirs'] lists art folders that do not exist (path
    diagnosis). Raises OSError when the XML is unreadable."""
    xml = hdb.system_xml_path(cfg["hyperspin_root"], system)
    games = hdb.parse_games(hdb.read_db_text(xml)[0])
    valid = {g.name.lower() for g in games}
    out = {"shared_video": 0, "missing_dirs": []}
    folders = _folders(cfg, system)
    usage = parent = None
    for kind in kinds:
        folder = folders[kind]
        found = []
        if not os.path.isdir(folder):
            out["missing_dirs"].append(os.path.basename(folder) or kind)
        else:
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
                    if usage is None:
                        usage, parent = _share_info(cfg)
                    if system == parent:
                        others = [t for t in usage.get(s, ())
                                  if t != system]
                        if any(s not in _own_video_stems(cfg, t)
                               for t in others):
                            out["shared_video"] += 1
                            continue            # a fallback user needs it
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
    missing = f" [folder not found: {', '.join(o['missing_dirs'])}]" \
        if o.get("missing_dirs") else ""
    if not parts:
        return ("✓ no extra art (everything matches the XML)" + shared
                + missing, True)
    total = len(o["wheel"]) + len(o["video"]) + len(o["theme"])
    # total FIRST: it is the numeric key the list's Missing sort uses
    return (f"⚠ {total} extra file(s) not in the XML: " + ", ".join(parts)
            + shared + missing, False)


def clean_system(cfg, system, kinds, log, stop_flag, progress=None):
    """Move every orphan of the chosen kinds to a backup folder. Returns
    the number of files removed. progress(done, total) ticks per file."""
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
    total = sum(len(o.get(k, [])) for k in kinds)
    done = 0
    for kind in kinds:
        names = o.get(kind, [])
        if not names:
            continue
        folder = folders[kind]
        bdir = os.path.join(folder, "clinic_backups", f"orphans_{stamp}")
        for name in names:
            if stop_flag():
                raise StopRequested()
            done += 1
            if progress:
                progress(done, max(1, total))
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
        _share_cache["vstems"] = {}     # folder contents changed
    log(f"[{system}] {len(removed)} orphan file(s) removed "
        f"(backed up, restorable)")
    return len(removed)


def backup_dirs(cfg):
    """[(system, kind, path)] of every orphans_* backup folder the Clear
    Extras deletions created, across all systems."""
    out = []
    for system in hdb.list_systems(cfg.get("hyperspin_root", "")):
        for kind, folder in _folders(cfg, system).items():
            cb = os.path.join(folder, "clinic_backups")
            if not os.path.isdir(cb):
                continue
            for s in os.listdir(cb):
                p = os.path.join(cb, s)
                if s.startswith("orphans_") and os.path.isdir(p):
                    out.append((system, kind, p))
    return out


def clear_backups(cfg, log, stop_flag=lambda: False, progress=None):
    """PERMANENTLY delete every Clear Extras backup folder. Returns
    (folders_removed, files_removed)."""
    import shutil
    dirs = backup_dirs(cfg)
    nfiles = ndirs = 0
    for i, (system, kind, p) in enumerate(dirs):
        if stop_flag():
            raise StopRequested()
        if progress:
            progress(i, len(dirs), f"{system} ({kind})")
        try:
            n = sum(len(fs) for _r, _d, fs in os.walk(p))
            shutil.rmtree(p)
            ndirs += 1
            nfiles += n
            log(f"  cleared {system} {kind} backup "
                f"{os.path.basename(p)} ({n} file(s))")
        except OSError as e:
            log(f"  could not clear {p}: {e}")
    log(f"DONE — {ndirs} backup folder(s) / {nfiles} file(s) permanently "
        f"deleted.")
    return ndirs, nfiles


def restore_backups(cfg, systems, kinds, log, stop_flag=lambda: False,
                    progress=None):
    """Move every backed-up extra (clinic_backups\\orphans_*) of the
    selected systems/kinds BACK into its original art folder. A file that
    already exists again in the folder is never overwritten (logged and
    left in the backup). Emptied backup folders are removed. Returns the
    number of files restored."""
    restored_total = 0
    # pre-count for a per-FILE progress bar (user rule)
    todo = []
    for system in systems:
        folders = _folders(cfg, system)
        for kind in kinds:
            cb = os.path.join(folders[kind], "clinic_backups")
            if not os.path.isdir(cb):
                continue
            for stamp in sorted(os.listdir(cb)):
                bdir = os.path.join(cb, stamp)
                if stamp.startswith("orphans_") and os.path.isdir(bdir):
                    todo.append((system, kind, folders[kind], bdir,
                                 len(os.listdir(bdir))))
    total_files = sum(t[4] for t in todo) or 1
    done = 0
    for i, system in enumerate(systems):
        folders = _folders(cfg, system)
        sys_restored = 0
        for kind in kinds:
            folder = folders[kind]
            cb = os.path.join(folder, "clinic_backups")
            if not os.path.isdir(cb):
                continue
            for stamp in sorted(os.listdir(cb)):
                bdir = os.path.join(cb, stamp)
                if not (stamp.startswith("orphans_") and os.path.isdir(bdir)):
                    continue
                for name in sorted(os.listdir(bdir)):
                    if stop_flag():
                        raise StopRequested()
                    done += 1
                    if progress:
                        progress(done, total_files, f"{system} ({kind})")
                    src = os.path.join(bdir, name)
                    dst = os.path.join(folder, name)
                    if not os.path.isfile(src):
                        continue
                    if os.path.exists(dst):
                        log(f"  = {name} ({kind}) already exists — kept in "
                            f"the backup, not overwritten")
                        continue
                    try:
                        os.replace(src, dst)
                        sys_restored += 1
                        log(f"  + {name} ({kind}) restored from {stamp}")
                    except OSError as e:
                        log(f"    could not restore {name}: {e}")
                if not os.listdir(bdir):
                    try:
                        os.rmdir(bdir)
                    except OSError:
                        pass
        if sys_restored:
            _track_restore(cfg, system, sys_restored, log)
        log(f"[{system}] {sys_restored} file(s) restored to their original "
            f"folders")
        restored_total += sys_restored
    return restored_total


def _track_restore(cfg, system, n, log):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(config.DATA_DIR, "cleanup.log"), "a",
              encoding="utf-8") as f:
        f.write(f"{stamp}\t{system}\tRESTORED\t{n} file(s)\t-\n")


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
