# Revert (final tab): list every system where the Clinic made changes -
# reconstructed from the backups on disk and the logs/database - and
# restore selected categories to their original versions.
#
# Categories and their revert semantics:
#   xml           enrichment edits to Databases\<Sys>\<Sys>.xml
#                 -> restore the EARLIEST clinic backup (the pre-change
#                    original), then retire the consumed backups
#   wheel-added / video-added
#                 files ADDED by the Missing Art tab (originals were never
#                 modified) -> delete the added file
#   rename-video / rename-wheel / rename-rom
#                 renames done by the Rename tab -> put the backed-up
#                 original-named file back and remove the renamed one
#
# Every revert is logged to data\reverts.log and tracked in the DB; the
# source logs are rewritten with the reverted entries removed so the
# summary stays accurate (full history lives in reverts.log).
import hashlib
import os
import shutil
import time

from . import config
from . import es as es_mod
from . import hyperspin_db as hdb
from . import store

CATEGORIES = {
    "xml": "XML metadata (enrichment)",
    "wheel-added": "Wheel art added",
    "video-added": "Videos added",
    "rename-video": "Renamed videos",
    "rename-wheel": "Renamed wheel art",
    "rename-rom": "Renamed roms",
    "theme-convert": "Converted themes (suite)",
    "videotheme": "Installed video themes (suite)",
}

ART_LOG = lambda: os.path.join(config.DATA_DIR, "art_additions.log")
REN_LOG = lambda: os.path.join(config.DATA_DIR, "renames.log")
REV_LOG = lambda: os.path.join(config.DATA_DIR, "reverts.log")


def _read_log(path):
    try:
        return [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    except OSError:
        return []


def _art_entries():
    out = []
    for l in _read_log(ART_LOG()):
        p = l.split("\t")
        if len(p) >= 6:
            out.append({"when": p[0], "system": p[1], "game": p[2],
                        "art": p[3], "source": p[4], "path": p[5], "raw": l})
    return out


def _ren_entries():
    out = []
    for l in _read_log(REN_LOG()):
        p = l.split("\t")
        if len(p) >= 7:
            out.append({"when": p[0], "system": p[1], "target": p[2],
                        "old": p[3], "new": p[5], "folder": p[6], "raw": l})
    return out


def _xml_backups(cfg, system):
    d = os.path.join(os.path.dirname(
        hdb.system_xml_path(cfg["hyperspin_root"], system)), "clinic_backups")
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".bak"))


def changes_summary(cfg):
    """{system: {category: count}} across logs + on-disk XML backups."""
    out = {}

    def bump(system, cat):
        out.setdefault(system, {}).setdefault(cat, 0)
        out[system][cat] += 1

    for e in _art_entries():
        bump(e["system"], f"{e['art']}-added")
    for e in _ren_entries():
        bump(e["system"], f"rename-{e['target']}")
    from . import themesuite
    for system in hdb.list_systems(cfg.get("hyperspin_root", "")):
        if _xml_backups(cfg, system):
            bump(system, "xml")
        try:
            for cat, n in themesuite.backup_counts(cfg, system).items():
                out.setdefault(system, {})[cat] = n
        except Exception:
            pass
    return out


def _track_reverts(cfg, lines, log):
    if not lines:
        return
    os.makedirs(config.DATA_DIR, exist_ok=True)
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(REV_LOG(), "a", encoding="utf-8") as f:
        for l in lines:
            f.write(f"{when}\tREVERTED\t{l}\n")
    from .ingest import Chunk
    chunks = [Chunk(
        id="revert:" + hashlib.sha1(l.encode("utf-8", "replace")).hexdigest()[:16],
        text=f"Reverted {when}: {l}",
        source="revert", kind="revert", meta={"when": when},
    ) for l in lines]
    if store.track(cfg, chunks, log):
        log(f"  tracked {len(lines)} revert(s) in the database")


def revert(cfg, systems, categories, log=print):
    """Revert the selected categories for the selected systems."""
    systems = set(systems)
    categories = set(categories)
    reverted_raws_art = []
    reverted_raws_ren = []
    count = 0

    # -- added art: delete the files we added --
    for e in _art_entries():
        cat = f"{e['art']}-added"
        if e["system"] in systems and cat in categories:
            if os.path.isfile(e["path"]):
                try:
                    os.remove(e["path"])
                    log(f"[{e['system']}] removed added {e['art']}: "
                        f"{os.path.basename(e['path'])}")
                except OSError as err:
                    log(f"[{e['system']}] could not remove {e['path']}: {err}")
                    continue
            else:
                log(f"[{e['system']}] already gone: {os.path.basename(e['path'])}")
            reverted_raws_art.append(e["raw"])
            count += 1

    # -- renames: restore original-named file from the rename backup --
    for e in _ren_entries():
        cat = f"rename-{e['target']}"
        if e["system"] in systems and cat in categories:
            folder = e["folder"]
            newp = os.path.join(folder, e["new"])
            bdir = os.path.join(folder, "clinic_backups")
            src = None
            if os.path.isdir(bdir):
                for stamp in sorted(os.listdir(bdir)):
                    c = os.path.join(bdir, stamp, e["old"])
                    if os.path.isfile(c):
                        src = c
                        break
            if not src:
                log(f"[{e['system']}] no backup found for '{e['old']}' — skipped")
                continue
            try:
                shutil.copy2(src, os.path.join(folder, e["old"]))
                if os.path.isfile(newp):
                    os.remove(newp)
                log(f"[{e['system']}] restored '{e['old']}' (removed '{e['new']}')")
                reverted_raws_ren.append(e["raw"])
                count += 1
            except OSError as err:
                log(f"[{e['system']}] revert failed for '{e['old']}': {err}")

    # -- xml: restore the earliest (original) backup --
    if "xml" in categories:
        for system in systems:
            baks = _xml_backups(cfg, system)
            if not baks:
                continue
            xml_path = hdb.system_xml_path(cfg["hyperspin_root"], system)
            bdir = os.path.join(os.path.dirname(xml_path), "clinic_backups")
            earliest = os.path.join(bdir, baks[0])
            try:
                shutil.copy2(earliest, xml_path)
                retired = os.path.join(bdir, "reverted-" +
                                       time.strftime("%Y%m%d-%H%M%S"))
                os.makedirs(retired, exist_ok=True)
                for b in baks:
                    shutil.move(os.path.join(bdir, b), os.path.join(retired, b))
                log(f"[{system}] XML restored from {baks[0]} "
                    f"({len(baks)} backup(s) retired)")
                reverted_raws_ren.append(f"xml-restore\t{system}\t{baks[0]}")
                count += 1
            except OSError as err:
                log(f"[{system}] XML revert failed: {err}")

    # -- suite backups: converted themes / installed video themes --
    from . import themesuite
    for system in systems:
        if "theme-convert" in categories:
            try:
                n = themesuite.revert_theme_conversion(cfg, system, log)
                if n:
                    reverted_raws_ren.append(f"theme-convert\t{system}\t{n} zips")
                    count += n
            except Exception as err:
                log(f"[{system}] theme revert failed: {err}")
        if "videotheme" in categories:
            try:
                n = themesuite.revert_videothemes(cfg, system, log)
                if n:
                    reverted_raws_ren.append(f"videotheme\t{system}\t{n} files")
                    count += n
            except Exception as err:
                log(f"[{system}] videotheme revert failed: {err}")

    # rewrite source logs without the reverted entries
    def _rewrite(path, drop):
        keep = [l for l in _read_log(path) if l not in drop]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
        os.replace(tmp, path)

    if reverted_raws_art:
        _rewrite(ART_LOG(), set(reverted_raws_art))
    ren_set = set(r for r in reverted_raws_ren if "\t->\t" in r)
    if ren_set:
        _rewrite(REN_LOG(), ren_set)

    _track_reverts(cfg, reverted_raws_art + reverted_raws_ren, log)
    return count


class StopRequested(Exception):
    pass


def backup_items(cfg):
    """[(label, path)] of every backup this tab's reverts rely on:
    XML clinic_backups, rename backups (timestamped clinic_backups
    subfolders in art/rom folders - Clear Extras' orphans_* folders are
    NOT included, that tab has its own button), and the Theme Suite's
    Themes_backup / *_pre_videotheme folders."""
    out = []
    root = cfg.get("hyperspin_root", "")
    from .artfinder import media_paths, system_media_base
    seen = set()

    def add(label, path):
        p = os.path.normpath(path)
        if p.lower() not in seen and os.path.isdir(p):
            seen.add(p.lower())
            out.append((label, p))

    for system in hdb.list_systems(root):
        add(f"{system}: XML backups", os.path.join(
            os.path.dirname(hdb.system_xml_path(root, system)),
            "clinic_backups"))
        p = media_paths(cfg, system)
        base = system_media_base(cfg, system)
        for label, folder in (("wheel", p["wheel"]), ("video", p["video"]),
                              ("themes", os.path.join(base, "Themes"))):
            cb = os.path.join(folder, "clinic_backups")
            if os.path.isdir(cb):
                for s in os.listdir(cb):
                    sp = os.path.join(cb, s)
                    if os.path.isdir(sp) and not s.startswith("orphans_"):
                        add(f"{system}: {label} rename backups {s}", sp)
        for name in ("Themes_backup", "Themes_backup_pre_videotheme",
                     "Video_backup_pre_videotheme"):
            add(f"{system}: {name}", os.path.join(base, name))
    # rom-folder rename backups live wherever renames.log points
    for e in _ren_entries():
        cb = os.path.join(e["folder"], "clinic_backups")
        if os.path.isdir(cb):
            for s in os.listdir(cb):
                sp = os.path.join(cb, s)
                if os.path.isdir(sp) and not s.startswith("orphans_"):
                    add(f"{e['system']}: rom rename backups {s}", sp)
    return out


def clear_backups(cfg, log, stop_flag=lambda: False, progress=None):
    """PERMANENTLY delete every backup the Revert tab relies on - after
    this, existing changes can no longer be restored. Returns
    (folders_removed, files_removed)."""
    items = backup_items(cfg)
    ndirs = nfiles = 0
    for i, (label, path) in enumerate(items):
        if stop_flag():
            raise StopRequested()
        if progress:
            progress(i, len(items), label)
        try:
            n = sum(len(fs) for _r, _d, fs in os.walk(path))
            shutil.rmtree(path)
            ndirs += 1
            nfiles += n
            log(f"  cleared {label} ({n} file(s))")
        except OSError as e:
            log(f"  could not clear {label}: {e}")
    log(f"DONE — {ndirs} backup location(s) / {nfiles} file(s) permanently "
        f"deleted. Existing changes can NO LONGER be reverted.")
    return ndirs, nfiles
