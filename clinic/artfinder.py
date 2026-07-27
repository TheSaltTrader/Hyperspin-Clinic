# Missing-art finder (tab 3). Per selected system:
#   1. games from Databases\<System>\<System>.xml
#   2. missing wheel art  -> Media\<System>\Images\Wheel\<game>.png
#      missing videos     -> Media\<System>\Video\<game>.mp4/flv/avi
#   3. sources, in the knowledge base's order:
#        LOCAL-FIRST: the system's own folders under case/punctuation/
#        region-title aliases (missing-only, files are COPIED to the
#        canonical name, existing art is never swapped)
#        EMUMOVIES FTP: video snaps folder + Logos pack for wheels
#        YOUTUBE (videos only): yt-dlp search fallback, when installed;
#        snap-length (<7 min) results preferred, optional sign-in cookies
#        from Setup when YouTube blocks anonymous downloads
#   4. wheel curation: trim alpha bbox, squeeze horizontally x0.75 (16:9
#      convention) and normalize to 400px wide
#   5. every addition -> UI log + data\art_additions.log + a tracking
#      record in the vector DB / Elasticsearch
import difflib
import io
import os
import re
import shutil
import subprocess
import time

from PIL import Image

from . import config, emumovies as emu_mod
from . import es as es_mod
from . import hyperspin_db as hdb
from . import secrets, store

VIDEO_EXTS = (".mp4", ".flv", ".avi")

_RESERVED = {"con", "prn", "aux", "nul"}
_RESERVED |= {"com%d" % i for i in range(1, 10)}
_RESERVED |= {"lpt%d" % i for i in range(1, 10)}


def safe_name(name):
    """XML-derived rom names become file paths; reject anything that could
    escape the target folder (OWASP path traversal) or hit reserved
    Windows device names."""
    if not name or name != os.path.basename(name):
        return False
    if ("/" in name) or ("\\" in name) or (".." in name) or (":" in name):
        return False
    if name.split(".")[0].lower() in _RESERVED:
        return False
    return True


class StopRequested(Exception):
    pass


# ---------- name normalization (knowledge-base fuzzy rules) ----------
_ROMAN = {" ii": " 2", " iii": " 3", " iv": " 4", " v ": " 5 ", " vi": " 6"}


def norm(name: str) -> str:
    s = os.path.splitext(name)[0].lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)          # strip regions/tags
    for r, a in _ROMAN.items():
        s = s.replace(r, a)
    return re.sub(r"[^a-z0-9]", "", s)


def best_match(target: str, pool: dict, cutoff=0.88):
    """pool: {normalized: original}. Exact key first, then close match."""
    t = norm(target)
    if t in pool:
        return pool[t]
    close = difflib.get_close_matches(t, list(pool.keys()), n=1, cutoff=cutoff)
    return pool[close[0]] if close else None


# ---------- media locations ----------
_media_cache = {}


def media_dir(root):
    """Locate the Media folder without counting every theme/snap (that
    full scan lives in config.inspect_hyperspin and is UI-only)."""
    if root in _media_cache:
        return _media_cache[root]
    media = ""
    if os.path.isdir(os.path.join(root, "Media")):
        media = os.path.join(root, "Media")
    elif os.path.isdir(root):
        try:
            for e in os.scandir(root):
                if e.is_dir() and os.path.isdir(os.path.join(e.path, "Themes")):
                    media = root
                    break
        except OSError:
            pass
    _media_cache[root] = media
    return media


def media_paths(cfg, system):
    media = media_dir(cfg["hyperspin_root"])
    base = os.path.join(media, system)
    return {
        "wheel": os.path.join(base, "Images", "Wheel"),
        "video": os.path.join(base, "Video"),
    }


def existing_map(folder, exts):
    out = {}
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.lower().endswith(exts):
                out[norm(f)] = f
    return out


# ---------- wheel curation ----------
def curate_wheel(src_bytes: bytes, dst_path: str):
    """Trim transparent border, squeeze width x0.75, normalize 400px wide."""
    im = Image.open(io.BytesIO(src_bytes)).convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    squeezed_w = max(1, w * 0.75)
    scale = 400.0 / squeezed_w
    new_h = max(1, round(h * scale))
    im = im.resize((400, new_h), Image.LANCZOS)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    im.save(dst_path, "PNG")
    return f"400x{new_h}"


# ---------- tracking ----------
def _track(cfg, entries, log):
    """entries: list of dicts {system, game, art, source, path}. Writes the
    additions log file and upserts tracking records into Chroma + ES."""
    if not entries:
        return
    os.makedirs(config.DATA_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(config.DATA_DIR, "art_additions.log"), "a",
              encoding="utf-8") as f:
        for e in entries:
            f.write(f"{stamp}\t{e['system']}\t{e['game']}\t{e['art']}\t"
                    f"{e['source']}\t{e['path']}\n")
    from .ingest import Chunk
    chunks = [Chunk(
        id=f"artadd:{e['system']}:{e['art']}:{e['game']}",
        text=(f"Art added {stamp}: {e['art']} for {e['game']} "
              f"({e['system']}) from {e['source']} -> {e['path']}"),
        source=e["path"], kind="art_added",
        meta={"system": e["system"], "game": e["game"], "art": e["art"],
              "source": e["source"], "added": stamp},
    ) for e in entries]
    if store.track(cfg, chunks, log):
        log(f"  tracked {len(entries)} addition(s) in the database")


# ---------- youtube fallback (videos only) ----------
def _ytdlp():
    return shutil.which("yt-dlp")


def cookies_file_usable(path):
    """A cookies.txt without SIGNED-IN youtube.com cookies makes YouTube
    403 every request — worse than staying anonymous (verified live; and
    yt-dlp writes anonymous session cookies back into the file, so the
    mere presence of youtube.com lines proves nothing). Require one of
    the cookies only a signed-in export carries."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(131072)
    except OSError:
        return False
    if "youtube.com" not in text:
        return False
    return any(c in text for c in ("LOGIN_INFO", "SAPISID", "__Secure-3PAPISID"))


def _yt_auth_args(cfg):
    """YouTube sign-in from Setup: an exported cookies.txt wins, else the
    chosen browser's cookies. Empty when nothing is configured (or the
    file is unusable) — anonymous downloads, exactly the pre-1.3.2
    behavior."""
    cfg = cfg or {}
    cookie_file = (cfg.get("youtube_cookies_file") or "").strip()
    browser = (cfg.get("youtube_cookies_browser") or "").strip()
    if cookie_file and os.path.isfile(cookie_file) and cookies_file_usable(cookie_file):
        return ["--cookies", cookie_file]
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def youtube_video(desc, dst_path, log, cfg=None):
    yt = _ytdlp()
    if not yt:
        return False
    tmp = dst_path + ".yt.mp4"
    base = _yt_auth_args(cfg) + [
            "-f", "mp4[height<=480]/best[height<=480]", "-o", tmp,
            "--no-playlist", "--quiet", "--no-warnings",
            "--retries", "3", "--fragment-retries", "3"]
    # Two passes: snap-length videos (<7 min) from the top 5 results first
    # (long-plays are skipped, transient 403s fall through to the next
    # candidate); then the old any-length top result, so a game never
    # loses its video to the duration filter.
    attempts = (
        [yt, f"ytsearch5:{desc} arcade gameplay", "--ignore-errors",
         "--match-filter", "duration<420", "--max-downloads", "1"] + base,
        [yt, f"ytsearch1:{desc} arcade gameplay"] + base,
    )
    try:
        for cmd in attempts:
            # CREATE_NO_WINDOW: yt-dlp (and the ffmpeg it spawns) must never
            # flash a console at the user - activity shows in the log pane
            r = subprocess.run(cmd, capture_output=True, timeout=300,
                              creationflags=0x08000000)
            # yt-dlp only renames its .part file to tmp on a completed
            # download, so the file's existence is the success signal
            # (--max-downloads exits 101 even when it succeeded)
            if os.path.isfile(tmp):
                os.replace(tmp, dst_path)
                return True
        log(f"    youtube: no result ({r.stderr.decode(errors='replace')[-80:].strip()})")
    except Exception as e:
        log(f"    youtube error: {e}")
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return False


def missing_counts(cfg, system):
    """Quick analysis for the system lists: (games, missing_wheels,
    missing_videos). One XML parse + one listdir per art folder."""
    xml = hdb.system_xml_path(cfg["hyperspin_root"], system)
    try:
        games = hdb.parse_games(hdb.read_db_text(xml)[0])
    except OSError:
        return (0, 0, 0, None)
    paths = media_paths(cfg, system)
    out = []
    for folder, exts in ((paths["wheel"], (".png", ".jpg")),
                         (paths["video"], VIDEO_EXTS)):
        present = set()
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                if f.lower().endswith(exts):
                    present.add(os.path.splitext(f)[0].lower())
        out.append(sum(1 for g in games if g.name.lower() not in present))
    # missing roms are NOT listed anywhere (user rule) - the rom slot
    # stays None; RocketLauncher paths are still used by the Rename tab
    return (len(games), out[0], out[1], None)


# ---------- the per-system pass ----------
def find_for_system(cfg, system, opts, log, stop_flag, progress=None):
    """opts: {wheel: bool, video: bool, local: bool, emumovies: bool,
    youtube: bool}. Returns summary dict."""
    xml = hdb.system_xml_path(cfg["hyperspin_root"], system)
    try:
        games = hdb.parse_games(hdb.read_db_text(xml)[0])
    except OSError as e:
        log(f"[{system}] SKIP: {e}")
        return {"system": system, "added": 0}
    paths = media_paths(cfg, system)
    added = []

    def check_stop():
        if stop_flag():
            raise StopRequested()

    jobs = []
    if opts.get("wheel"):
        jobs.append(("wheel", paths["wheel"], (".png",)))
    if opts.get("video"):
        jobs.append(("video", paths["video"], VIDEO_EXTS))

    emu = None
    try:
        for job_i, (art, folder, exts) in enumerate(jobs):
            check_stop()
            if progress:
                progress(job_i / max(1, len(jobs)),
                         f"{system}: scanning {art} folder")
            # presence = the EXACT rom-named file HyperSpin will look for;
            # normalized matches are alias CANDIDATES, not presence
            present = set()
            if os.path.isdir(folder):
                for f in os.listdir(folder):
                    if f.lower().endswith(exts):
                        present.add(os.path.splitext(f)[0].lower())
            missing = [g for g in games if g.name.lower() not in present]
            log(f"[{system}] {art}: {len(games)} games, {len(missing)} missing")
            if not missing:
                continue

            # -- 1) LOCAL-FIRST aliases (copy, never move; missing-only) --
            if opts.get("local") and os.path.isdir(folder):
                pool = {}
                for f in os.listdir(folder):
                    if f.lower().endswith(exts):
                        pool[norm(f)] = f
                still = []
                for g in missing:
                    check_stop()
                    src = (best_match(g.name, pool)
                           or (best_match(g.description, pool)
                               if g.description else None))
                    # skip only when the found file IS the canonical name
                    # (normalized equality is how aliases are found, so it
                    # must NOT disqualify them)
                    if (src and safe_name(g.name)
                            and os.path.splitext(src)[0].lower() != g.name.lower()):
                        ext = os.path.splitext(src)[1]
                        dst = os.path.join(folder, g.name + ext)
                        if not os.path.exists(dst):
                            shutil.copy2(os.path.join(folder, src), dst)
                            added.append({"system": system, "game": g.name,
                                          "art": art, "source": f"local alias: {src}",
                                          "path": dst})
                            log(f"  + {g.name} {art} from local alias '{src}'")
                            continue
                    still.append(g)
                missing = still

            # -- 2) EmuMovies FTP --
            if missing and opts.get("emumovies"):
                creds = secrets.load_emumovies()
                if not creds:
                    log(f"[{system}] EmuMovies: no credentials stored (Setup tab) — skipping")
                else:
                    if emu is None:
                        try:
                            emu = emu_mod.EmuMovies(*creds, log=log)
                            log(f"[{system}] EmuMovies: connected")
                        except Exception as e:
                            log(f"[{system}] EmuMovies: connection failed ({e})")
                            emu = False
                    if emu:
                        missing = _from_emumovies(
                            cfg, emu, system, art, folder, missing,
                            added, log, check_stop)

            # -- 3) YouTube (videos only) --
            if missing and art == "video" and opts.get("youtube"):
                if not _ytdlp():
                    log(f"[{system}] youtube: yt-dlp not installed — skipping")
                else:
                    still = []
                    for g in missing:
                        check_stop()
                        if not safe_name(g.name):
                            log("    unsafe rom name skipped: %r" % g.name)
                            still.append(g)
                            continue
                        dst = os.path.join(folder, g.name + ".mp4")
                        title = g.description or g.name
                        if youtube_video(title, dst, log, cfg):
                            added.append({"system": system, "game": g.name,
                                          "art": art, "source": "youtube",
                                          "path": dst})
                            log(f"  + {g.name} video from youtube")
                        else:
                            still.append(g)
                    missing = still
            if missing:
                log(f"[{system}] {art}: {len(missing)} still missing after all sources")
    finally:
        if emu:
            emu.close()
        _track(cfg, added, log)
    return {"system": system, "added": len(added)}


def _from_emumovies(cfg, emu, system, art, folder, missing, added, log, check_stop):
    still = list(missing)
    if art == "video":
        vdir = emu.find_video_dir(system)
        if not vdir:
            log(f"[{system}] EmuMovies: no video-snap folder found")
            return still
        pool = {norm(f): f for f in emu.list_videos(vdir)}
        log(f"[{system}] EmuMovies: {len(pool)} snaps in {vdir.rsplit('/', 1)[-1]}")
        out = []
        for g in still:
            check_stop()
            src = (best_match(g.name, pool)
                   or (best_match(g.description, pool) if g.description else None))
            if src and safe_name(g.name):
                ext = os.path.splitext(src)[1].lower()
                dst = os.path.join(folder, g.name + ext)
                try:
                    size = emu.download(f"{vdir}/{src}", dst)
                    added.append({"system": system, "game": g.name,
                                  "art": "video", "source": f"emumovies: {src}",
                                  "path": dst})
                    log(f"  + {g.name} video from EmuMovies ({size // 1024} KB)")
                    continue
                except Exception as e:
                    log(f"    download failed for {g.name}: {e}")
            out.append(g)
        return out
    # wheels via the Logos pack
    pack = emu.find_logo_pack(system)
    if not pack:
        log(f"[{system}] EmuMovies: no Logos pack found")
        return still
    zf = emu.fetch_logo_pack(pack, os.path.join(config.DATA_DIR, "emumovies_cache"))
    if not zf:
        return still
    pool = {norm(n): n for n in zf.namelist()
            if n.lower().endswith((".png", ".jpg"))}
    out = []
    for g in still:
        check_stop()
        src = (best_match(g.name, pool)
               or (best_match(g.description, pool) if g.description else None))
        if src and safe_name(g.name):
            dst = os.path.join(folder, g.name + ".png")
            try:
                dims = curate_wheel(zf.read(src), dst)
                added.append({"system": system, "game": g.name,
                              "art": "wheel",
                              "source": f"emumovies pack: {os.path.basename(src)}",
                              "path": dst})
                log(f"  + {g.name} wheel from EmuMovies (curated {dims}, x0.75 squeeze)")
                continue
            except Exception as e:
                log(f"    wheel extract failed for {g.name}: {e}")
        out.append(g)
    try:
        zf.close()
    except Exception:
        pass
    return out
