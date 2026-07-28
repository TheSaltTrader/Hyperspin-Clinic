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
# presence rule (user): ONLY real videos count - a jpg/png (or avi)
# bearing the game's name in the Video folder does not satisfy a snap
VIDEO_PRESENT_EXTS = (".mp4", ".flv")

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
    import html as _html
    # strip only KNOWN media extensions - os.path.splitext truncated any
    # name with a dot in it ('Marvel vs. Capcom 2' became 'marvel vs',
    # so its exactly-named snap could never match; same for Dr. Mario)
    # unescape first: '&apos;' would otherwise pollute the key as 'apos'
    s = re.sub(r"\.(png|jpe?g|gif|bmp|mp4|flv|avi|mkv|webm|zip)$", "",
               _html.unescape(name), flags=re.I).lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)          # strip regions/tags
    for r, a in _ROMAN.items():
        s = s.replace(r, a)
    return re.sub(r"[^a-z0-9]", "", s)


def best_match(target: str, pool: dict, cutoff=0.88):
    """pool: {normalized: original}. Exact key first; then unique
    subtitle-prefix (file 'Marvel vs Capcom 2 - New Age of Heroes' must
    match game 'Marvel vs. Capcom 2'); then close match."""
    t = norm(target)
    if t in pool:
        return pool[t]
    if len(t) >= 8:
        pref = [k for k in pool if k.startswith(t)]
        if len(pref) == 1:
            return pool[pref[0]]
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


_sysdir_cache = {}


def system_media_base(cfg, system):
    """The system's folder under Media. When Media\\<system> does not
    exist, fall back to a NORMALIZED match (spaces/punctuation/case
    ignored) - user report: the 'Hyper Neo Geo 64' wheel's media folder
    was named 'Hyperneogeo64', so every art file counted as missing."""
    media = media_dir(cfg["hyperspin_root"])
    base = os.path.join(media, system)
    if os.path.isdir(base):
        return base
    key = (media, system.lower())
    if key in _sysdir_cache:
        return _sysdir_cache[key]
    want = re.sub(r"[^a-z0-9]", "", system.lower())
    found = base
    try:
        for e in os.scandir(media):
            if e.is_dir() and re.sub(r"[^a-z0-9]", "", e.name.lower()) == want:
                found = e.path
                break
    except OSError:
        pass
    _sysdir_cache[key] = found
    return found


def media_paths(cfg, system):
    base = system_media_base(cfg, system)
    return {
        "wheel": os.path.join(base, "Images", "Wheel"),
        "video": os.path.join(base, "Video"),
    }


def purge_video_placeholders(folder, game_name, log):
    """After a real video was downloaded for the game, delete its jpg/png
    placeholder files from the Video folder (user rule: the new video
    replaces them)."""
    want = game_name.lower()
    try:
        for e in os.scandir(folder):
            if not e.is_file():
                continue
            stem, ext = os.path.splitext(e.name)
            if stem.lower() == want and ext.lower() in (".jpg", ".jpeg", ".png"):
                try:
                    os.remove(e.path)
                    log(f"    replaced placeholder image '{e.name}'")
                except OSError as err:
                    log(f"    could not remove placeholder '{e.name}': {err}")
    except OSError:
        pass


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
    """App-managed copy first (data\\tools\\yt-dlp.exe, kept current by
    the startup update check) so a stale system yt-dlp can't break
    YouTube; PATH as fallback."""
    from . import ytupdate
    p = ytupdate.managed_exe()
    if os.path.isfile(p):
        return p
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


def _ffmpeg():
    """The Theme Suite ships ffmpeg - reuse it; PATH as fallback."""
    suite = config.auto_theme_suite()
    if suite:
        p = os.path.join(suite, "ThemeVideo", "ffmpeg.exe")
        if os.path.isfile(p):
            return p
    return shutil.which("ffmpeg")


# knowledge base (XBLA/SNES media pipeline): titles/channels that always
# produce unusable "snaps" - reviews, reaction faces, commentary over
# gameplay (user rule: original gameplay and game music ONLY), baked
# watermarks
_BAD_TITLE = re.compile(r"review|reaction|commentary|commentated|let.?s play"
                        r"|trailer|teaser|commercial|announce"
                        r"|face ?cam|unboxing|top ?10|ranking|versus"
                        r"|comparison|podcast|interview|vlog|reacts"
                        r"|live ?stream|first look|impressions"
                        r"|why the|hype|retrospective|analysis|explained"
                        r"|history of|worth|before you|hidden gem"
                        r"|underrated|overrated|docu|iceberg|essay|\?", re.I)
_BAD_CHANNEL = re.compile(r"IGN|GameSpot|GameTrailers|Kotaku|Polygon"
                          r"|GameXplain|drunk|Retrospective|Essay", re.I)
# channels the knowledge base's runs rated as clean gameplay/longplays -
# candidates from them (or explicitly 'no commentary'/'longplay' titles)
# are preferred
_GOOD_SOURCE = re.compile(r"World of Longplays|LongplayArchive|AL82"
                          r"|Game Network|Anoba Games|EWA Gamesroom"
                          r"|XCageGame|NintendoComplete", re.I)
_GOOD_TITLE = re.compile(r"longplay|no ?commentary|playthrough", re.I)

# widescreen systems (everything else in a HyperSpin collection is 4:3 -
# user rule: identify the non-widescreen systems so their videos can be
# cropped/resized to 4:3)
_WIDE_TOKENS = ("ps3", "playstation 3", "ps4", "playstation 4", "ps5",
                "playstation 5", "xbox 360", "xbox one", "series x",
                "xbla", "xbox live", "wii u", "switch", "psp", "vita",
                "pc games", "pc engine cd?", "steam", "windows", "teknoparrot")


def system_is_43(system: str) -> bool:
    s = " " + re.sub(r"[^a-z0-9 ]", " ", system.lower()) + " "
    for tok in _WIDE_TOKENS:
        if tok in s:
            return False
    return True


# stop-words that carry no identity when matching the system name inside
# a video title
_SYS_STOP = {"the", "of", "system", "entertainment", "games", "game",
             "computer", "color"}
_SYS_ALIASES = {
    "super nintendo entertainment system": ["snes"],
    "nintendo entertainment system": ["nes"],
    "sega mega drive": ["genesis"],
    "sega genesis": ["mega drive"],
    "nintendo 64": ["n64"],
    "mame": ["arcade"],
    "final burn neo": ["arcade"], "fbneo": ["arcade"],
}


def _system_tokens(system: str):
    base = re.sub(r"[^a-z0-9 ]", " ", system.lower())
    toks = {t for t in base.split() if len(t) >= 3 and t not in _SYS_STOP}
    toks.add(re.sub(r"[^a-z0-9]", "", base))
    for key, aliases in _SYS_ALIASES.items():
        if key in base or base.strip() in key:
            toks.update(aliases)
    if "mame" in base or "arcade" in base:
        toks.add("arcade")
    return {t.replace(" ", "") for t in toks if t}

_YT_HINT_RE = re.compile(r"needs to be reloaded|Sign in to confirm", re.I)
_YT_COOKIE_RE = re.compile(r"cookie database|could not decrypt", re.I)
_YT_BLOCK_RE = re.compile(r"HTTP Error 403|Sign in to confirm|"
                          r"needs to be reloaded|429", re.I)

# YouTube bot-throttle cooldown (knowledge base: refusal waves lift after
# a 50-90 min cooldown; sign-in cookies avoid them entirely). Escalating
# waits; the state is per-run (reset in find_for_system).
_YT_COOLDOWNS = (300, 900, 2700, 5400)          # 5 / 15 / 45 / 90 min
_YT_BLOCK = {"hit": False, "consec": 0, "level": 0, "off": False}


def _yt_reset_block_state():
    _YT_BLOCK.update(hit=False, consec=0, level=0, off=False)


def _tnorm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _yt_hint(stderr_text, log):
    if _YT_BLOCK_RE.search(stderr_text):
        _YT_BLOCK["hit"] = True
    if _YT_COOKIE_RE.search(stderr_text):
        log("    HINT: your browser's cookie store can't be read — "
            "Chrome-family browsers lock/encrypt it (yt-dlp issue 7271). "
            "In Setup > YouTube sign-in, select an exported cookies.txt "
            "(most reliable) or Firefox, or fully close the browser.")
    elif _YT_HINT_RE.search(stderr_text):
        log("    HINT: YouTube is refusing anonymous/outdated clients — "
            "update yt-dlp (pip install -U yt-dlp) and/or configure "
            "YouTube sign-in on the Setup tab")


def _yt_cooldown(log, check_stop, yt, auth):
    """Wait out a refusal wave (Stop stays responsive), then probe. True
    when YouTube answers again."""
    lvl = min(_YT_BLOCK["level"], len(_YT_COOLDOWNS) - 1)
    wait = _YT_COOLDOWNS[lvl]
    _YT_BLOCK["level"] += 1
    log(f"    YOUTUBE COOLDOWN: downloads are being refused — waiting "
        f"{wait // 60} min before retrying (blocks lift on their own; "
        f"YouTube sign-in in Setup avoids them entirely)")
    t0 = time.time()
    last_note = 0
    while time.time() - t0 < wait:
        if check_stop:
            check_stop()
        time.sleep(15)
        gone = time.time() - t0
        if gone - last_note >= 300:
            last_note = gone
            log(f"    cooldown: {int((wait - gone) // 60) + 1} min remaining…")
    _YT_BLOCK["hit"] = False
    probe = _yt_search(yt, "ytsearch1:classic arcade gameplay", auth,
                       lambda m: None)
    if probe and not _YT_BLOCK["hit"]:
        log("    cooldown over — YouTube is responding again")
        return True
    log("    cooldown ended but YouTube is still refusing")
    return False


def _yt_search(yt, query, auth, log):
    """Metadata-only search (simulate-then-pick, knowledge-base pattern:
    one cheap search, then WE choose - a one-shot download takes whatever
    YouTube ranks first). Returns [{id,duration,uploader,title}]."""
    cmd = [yt, query, "--simulate", "--no-playlist", "--no-warnings",
           "--sleep-requests", "1", "--socket-timeout", "30",
           "--print", "C\t%(id)s\t%(duration)s\t%(uploader)s\t%(title)s"] + auth
    r = subprocess.run(cmd, capture_output=True, timeout=120,
                       creationflags=0x08000000)
    _yt_hint(r.stderr.decode(errors="replace"), log)
    out = []
    for line in r.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[0] == "C":
            try:
                dur = float(parts[2])
            except ValueError:
                dur = 0
            out.append({"id": parts[1], "duration": dur,
                        "uploader": parts[3], "title": parts[4]})
    return out


def _pick(cands, title, dur_lo, dur_hi, system=None):
    """Knowledge-base candidate rules: duration window, no review/
    commentary titles, no watermark channels, the video title MUST
    contain the game name AND (user rule) the system's name; clean
    longplay sources are preferred over everything else."""
    nt = _tnorm(title)
    main = _tnorm(title.split(" - ")[0])
    sys_toks = _system_tokens(system) if system else set()
    ok = []
    for c in cands:
        if dur_lo and c["duration"] < dur_lo:
            continue
        if dur_hi and c["duration"] > dur_hi:
            continue
        if _BAD_TITLE.search(c["title"]) or _BAD_CHANNEL.search(c["uploader"]):
            continue
        ct = _tnorm(c["title"])
        if nt not in ct and not (len(main) >= 6 and main in ct):
            continue
        if sys_toks and not any(t in ct for t in sys_toks):
            continue                    # title must name the system
        ok.append(c)
    if not ok:
        return None
    # prefer known clean-gameplay channels / longplay-style titles
    ok.sort(key=lambda c: 0 if (_GOOD_SOURCE.search(c["uploader"])
                                or _GOOD_TITLE.search(c["title"])) else 1)
    return ok[0]


def _yt_download(yt, video_id, tmp, auth, log, section=None):
    cmd = [yt, f"https://www.youtube.com/watch?v={video_id}",
           "-f", "mp4[height<=480]/best[height<=480]", "-o", tmp,
           "--no-playlist", "--quiet", "--no-warnings",
           "--retries", "3", "--fragment-retries", "5",
           "--socket-timeout", "30", "--sleep-requests", "1"] + auth
    if section:
        ff = _ffmpeg()
        cmd += ["--download-sections", section, "--force-keyframes-at-cuts"]
        if ff:
            cmd += ["--ffmpeg-location", os.path.dirname(ff)]
    r = subprocess.run(cmd, capture_output=True, timeout=420,
                       creationflags=0x08000000)
    _yt_hint(r.stderr.decode(errors="replace"), log)
    return os.path.isfile(tmp)


def _crop_union(ff, src, log):
    """Multi-sample cropdetect UNION (knowledge base: a single-shot
    cropdetect is unreliable) - limit 64 first (gray bars), then 24."""
    for limit in (64, 24):
        boxes = []
        for ss in (5, 20, 40):
            r = subprocess.run(
                [ff, "-ss", str(ss), "-i", src, "-t", "2",
                 "-vf", f"cropdetect={limit}:2:0", "-f", "null", "-"],
                capture_output=True, timeout=120, creationflags=0x08000000)
            boxes += re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)",
                                r.stderr.decode(errors="replace"))
        if not boxes:
            continue
        b = [tuple(map(int, x)) for x in boxes]
        x0 = min(x for _w, _h, x, _y in b)
        y0 = min(y for _w, _h, _x, y in b)
        x1 = max(w + x for w, _h, x, _y in b)
        y1 = max(h + y for _w, h, _x, y in b)
        w, h = x1 - x0, y1 - y0
        if w >= 160 and h >= 160:
            return f"crop={w}:{h}:{x0}:{y0}"
    return None


def _src_dims(ff, src):
    r = subprocess.run([ff, "-i", src], capture_output=True, timeout=60,
                       creationflags=0x08000000)
    m = re.search(r"Video:.* (\d{2,5})x(\d{2,5})",
                  r.stderr.decode(errors="replace"))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _snap_transcode(src, dst, log, max_len=60, target_43=False):
    """Knowledge-base post-processing: crop bars, cut to snap length with
    fades, normalize to H.264/AAC. For 4:3 systems (user rule): side
    bars are cropped so the result IS 4:3, and a bar-less widescreen
    source is RESIZED to 4:3 so the game is no longer stretched.
    Vertical (arcade) content is left at its native aspect. Without
    ffmpeg the raw file is kept."""
    ff = _ffmpeg()
    if not ff:
        os.replace(src, dst)
        return "raw (no ffmpeg found)"
    crop = _crop_union(ff, src, log)
    vf = [crop] if crop else []
    note43 = ""
    if target_43:
        if crop:
            m = re.match(r"crop=(\d+):(\d+)", crop)
            eff_w, eff_h = int(m.group(1)), int(m.group(2))
        else:
            eff_w, eff_h = _src_dims(ff, src)
        aspect = (eff_w / eff_h) if eff_h else 0
        if aspect > 1.42:
            # still widescreen after (or without) a crop -> squeeze to 4:3
            vf.append("scale=640:480")
            note43 = ", resized to 4:3"
        elif 1.05 <= aspect <= 1.42:
            # includes consoles whose native signal is ~8:7 (SNES 256x224)
            # but which display as 4:3 on a real TV
            # near-4:3 (cropdetect never lands exactly): normalize to a
            # true 640x480 so the output IS 4:3
            vf.append("scale=640:480")
            note43 = (", bars cropped to 4:3" if crop else ", normalized to 4:3")
        # aspect < 1.15 = vertical (arcade) content: keep native
    vf += ["scale=-2:'trunc(min(480,ih)/2)*2'", "setsar=1",
           "fade=t=in:st=0:d=1", f"fade=t=out:st={max_len - 2}:d=2"]
    r = subprocess.run(
        [ff, "-y", "-i", src, "-t", str(max_len),
         "-vf", ",".join(vf),
         "-af", f"afade=t=in:st=0:d=1,afade=t=out:st={max_len - 2}:d=2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", dst],
        capture_output=True, timeout=300, creationflags=0x08000000)
    if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 50_000:
        try:
            os.remove(src)
        except OSError:
            pass
        return (f"{max_len}s snap" + (", bars cropped" if crop else "") + note43)
    os.replace(src, dst)                     # transcode failed: keep raw
    return "raw (transcode failed)"


def youtube_video(desc, dst_path, log, cfg=None, check_stop=None,
                  system=None):
    """Search-then-pick ladder from the media-pipeline knowledge base:
    gameplay, else a section of a longplay - GAME FOOTAGE ONLY (user
    rule): no trailers and no unfiltered fallback, and every download
    skips at least the first minute so the snap never opens on a title
    screen. Every result is cropped/cut to a 60s snap by ffmpeg. When
    YouTube starts refusing downloads, an escalating COOLDOWN waits the
    block out and retries (user rule)."""
    yt = _ytdlp()
    if not yt:
        return False
    if _YT_BLOCK["off"]:
        log("    youtube: skipped — refusals persisted through every "
            "cooldown this run")
        return False
    from . import launchbox
    title = launchbox.clean_title(desc)
    auth = _yt_auth_args(cfg)
    tmp = dst_path + ".yt.mp4"
    sys_term = ""
    if system:
        sys_term = " " + ("arcade" if ("mame" in system.lower()
                                       or "arcade" in system.lower())
                          else system)
    target_43 = system_is_43(system) if system else False
    if system:
        log(f"    youtube: searching with system term '{sys_term.strip()}' — "
            f"framing target {'4:3' if target_43 else '16:9 (widescreen)'}")
    ladder = (
        # gameplay lower bound 105s: every video must afford the 1-minute
        # intro skip and still leave a 45s+ snap
        (f"ytsearch6:{title}{sys_term} gameplay", 105, 420, "gameplay"),
        (f"ytsearch6:{title}{sys_term} longplay", 600, None, "longplay"),
        (f"ytsearch6:{title}{sys_term} no commentary", 105, 600,
         "gameplay"),
    )

    def pick_section(kind, duration):
        # user rule: EVERY video skips at least the first minute so the
        # snap never opens on a title screen; long videos skip extra
        # minutes to also clear story scenes and menus
        if kind == "longplay":
            return "*00:03:00-00:05:00"
        if duration >= 240:
            return "*00:02:00-00:04:00"
        end = min(int(duration), 180)
        return f"*00:01:00-00:{end // 60:02d}:{end % 60:02d}"

    def attempt():
        for query, lo, hi, kind in ladder:
            pick = _pick(_yt_search(yt, query, auth, log), title, lo, hi,
                         system=system)
            if not pick:
                continue
            section = pick_section(kind, pick["duration"])
            if section:
                log(f"    youtube: skipping intro — sampling {section.strip('*')}")
            if _yt_download(yt, pick["id"], tmp, auth, log, section):
                note = _snap_transcode(tmp, dst_path, log,
                                       target_43=target_43)
                log(f"    youtube: '{pick['title'][:50]}' ({pick['uploader'][:24]}) — {note}")
                return True
        # no unfiltered fallback (user rule): better no video than one
        # showing anything other than the game itself
        log("    youtube: no suitable video — filters keep only pure "
            "gameplay with the system in the title")
        return False

    try:
        while True:
            _YT_BLOCK["hit"] = False
            if attempt():
                _YT_BLOCK["consec"] = 0
                _YT_BLOCK["level"] = 0
                return True
            if not _YT_BLOCK["hit"]:
                return False          # genuine no-result, not a refusal
            _YT_BLOCK["consec"] += 1
            if _YT_BLOCK["consec"] < 2:
                return False          # single flake: move on, no wait yet
            if _YT_BLOCK["level"] >= len(_YT_COOLDOWNS):
                _YT_BLOCK["off"] = True
                log("    youtube: still refused after the longest cooldown "
                    "— disabled for the rest of this run")
                return False
            if not _yt_cooldown(log, check_stop, yt, auth):
                continue              # next round escalates the wait
            # block lifted: retry this same game
    except StopRequested:
        raise
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
    for kind, folder, exts in (("wheel", paths["wheel"], (".png", ".jpg")),
                               ("video", paths["video"], VIDEO_PRESENT_EXTS)):
        present = set()
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                if f.lower().endswith(exts):
                    present.add(os.path.splitext(f)[0].lower())
        if kind == "video":
            # subset wheels fall back to the parent MAME folder (user
            # rule) - videos available there are not missing
            parent, pstems = _parent_video_cover(cfg, system, games)
            if parent:
                present |= pstems
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
        jobs.append(("video", paths["video"], VIDEO_PRESENT_EXTS))

    emu = None
    emu_cat = None
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
            covered, parent = 0, None
            if art == "video":
                # user rule: subset wheels fall back to the parent MAME
                # folder - a video that already exists there is NOT missing
                parent, pstems = _parent_video_cover(cfg, system, games)
                if parent:
                    covered = sum(1 for g in games
                                  if g.name.lower() not in present
                                  and g.name.lower() in pstems)
                    present |= pstems
            missing = [g for g in games if g.name.lower() not in present]
            log(f"[{system}] {art}: {len(games)} games, "
                f"{len(missing)} missing"
                + (f" ({covered} covered by the {parent} folder fallback)"
                   if covered else ""))
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
                            from . import emucatalog
                            emu_cat = emucatalog.ensure(
                                cfg,
                                lambda: emu_mod.EmuMovies(*creds, log=log),
                                log, stop_flag)
                        except Exception as e:
                            log(f"[{system}] EmuMovies: connection failed ({e})")
                            emu = False
                    if emu:
                        try:
                            missing = _from_emumovies(
                                cfg, emu, system, art, folder, missing,
                                added, log, check_stop, cat=emu_cat)
                        except StopRequested:
                            raise
                        except Exception as e:
                            # a dying FTP session (SSL EOF etc.) must not
                            # kill the system's whole art pass
                            log(f"[{system}] EmuMovies error "
                                f"({e or type(e).__name__}) — continuing "
                                f"with the remaining sources")

            # -- 3) LaunchBox GamesDB clear logos (wheels only) --
            # knowledge base: transparent clear logos, free, no API key;
            # fills what EmuMovies packs don't cover (MAME has no pack)
            if missing and art == "wheel" and opts.get("launchbox"):
                from . import launchbox
                still = []
                for g in missing:
                    check_stop()
                    if not safe_name(g.name):
                        still.append(g)
                        continue
                    data = launchbox.fetch_clear_logo(
                        g.description or g.name, system, log)
                    if data:
                        dst = os.path.join(folder, g.name + ".png")
                        try:
                            dims = curate_wheel(data, dst)
                            added.append({"system": system, "game": g.name,
                                          "art": "wheel",
                                          "source": "launchbox clear logo",
                                          "path": dst})
                            log(f"  + {g.name} wheel from LaunchBox (curated {dims})")
                            continue
                        except Exception as e:
                            log(f"    launchbox wheel failed for {g.name}: {e}")
                    still.append(g)
                missing = still

            # -- 4) YouTube (videos only) --
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
                        if youtube_video(title, dst, log, cfg,
                                         check_stop=check_stop,
                                         system=system):
                            added.append({"system": system, "game": g.name,
                                          "art": art, "source": "youtube",
                                          "path": dst})
                            log(f"  + {g.name} video from youtube")
                            purge_video_placeholders(folder, g.name, log)
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


# region-rename aliases from the media-pipeline knowledge base (EU/JP
# titles that EmuMovies files under the US name, and vice versa)
REGION_ALIASES = {
    "jet set radio": "Jet Grind Radio",
    "jet grind radio": "Jet Set Radio",
    "ace golf": "Swingerz Golf",
    "swingerz golf": "Ace Golf",
    "castleween": "Spirits & Spells",
    "spirits & spells": "Castleween",
    "donald duck quack attack": "Donald Duck Goin' Quackers",
    "donald duck goin' quackers": "Donald Duck Quack Attack",
    "mario smash football": "Super Mario Strikers",
    "super mario strikers": "Mario Smash Football",
}


def _alias_title(desc):
    if not desc:
        return None
    key = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", desc).strip().lower()
    return REGION_ALIASES.get(key)


def _mameish(games) -> bool:
    """True when the games look like MAME roms (short lowercase rom
    names) - the signature of a custom MAME-subset wheel."""
    names = [g.name for g in games[:40]]
    if not names:
        return False
    hits = sum(1 for n in names if re.fullmatch(r"[a-z0-9_]{1,12}", n))
    return hits >= 0.7 * len(names)


def _parent_video_cover(cfg, system, games):
    """(parent_name, stems) - video stems available in the PARENT (largest)
    system's folder, which subset wheels use as a fallback (user rule:
    a video is NOT missing when the MAME folder already has it). Empty
    for the parent itself and for non-MAME-style systems."""
    if not _mameish(games):
        return None, frozenset()
    try:
        from . import cleaner
        _usage, parent = cleaner._share_info(cfg)
        if not parent or parent == system:
            return None, frozenset()
        return parent, frozenset(cleaner._own_video_stems(cfg, parent))
    except Exception:
        return None, frozenset()


def _from_emumovies(cfg, emu, system, art, folder, missing, added, log,
                    check_stop, cat=None):
    still = list(missing)
    if art == "video":
        # THE CATALOG FIRST (user rule: no assumptions) - the extracted
        # tree knows every folder and every file; FTP is only used for
        # the actual downloads. Live listing is the fallback.
        raw_pools = []
        from . import emucatalog
        for vd, fl in emucatalog.video_pools(cat, system, cfg):
            raw_pools.append((vd, fl, "catalog"))
        if not raw_pools:
            for vd in emu.find_video_dirs(system):
                raw_pools.append((vd, emu.list_videos(vd), "live"))
        if not raw_pools and _mameish(still):
            # custom MAME-subset wheels (Arcade Shmups, Cave, Capcom Play
            # System…) hold MAME roms - their snaps live in the MAME
            # Arcade folders
            for vd, fl in emucatalog.video_pools(cat, "MAME", cfg):
                raw_pools.append((vd, fl, "catalog, MAME-subset fallback"))
            if not raw_pools:
                for vd in emu.find_video_dirs("MAME"):
                    raw_pools.append((vd, emu.list_videos(vd),
                                      "live, MAME-subset fallback"))
            if raw_pools:
                log(f"[{system}] EmuMovies: rom-style names detected — "
                    f"treating as a MAME subset (MAME Arcade folders)")
        if not raw_pools:
            log(f"[{system}] EmuMovies: no video-snap folder matched "
                f"(trained map, name aliases and the semantic folder "
                f"index were all tried) — if one exists, add it to "
                f"data\\emumovies_map.json under 'video::{system}'")
            return still
        # highest quality first, PER GAME: HD sets are often WIP and
        # incomplete, so each game cascades HD -> HQ -> SQ (user rule)
        pools, filedir = [], {}
        for vd, fl, origin in raw_pools:
            pools.append((vd, {norm(f): f for f in fl}))
            for f in fl:
                filedir.setdefault(f, vd)
            log(f"[{system}] EmuMovies: {len(fl)} snaps in {vd} ({origin})")
        # ES verification (user rule): direct pull from the indexed
        # catalog when available; transient index as fallback; difflib
        # remains the first, offline-safe matcher
        es_map = None
        cat_es = emucatalog.es_candidates(
            cfg, system, [vd for vd, _ in pools], still, n=3)
        if cat_es is not None:
            es_map = cat_es
            log(f"[{system}] EmuMovies: candidates pulled from the "
                f"Elasticsearch catalog index")
        elif es_mod.available(cfg):
            try:
                from . import renamer
                raw = renamer._es_candidates(cfg, list(filedir), still, n=3)
                if raw is not None:
                    es_map = {k: [(filedir[c], c) for c in v if c in filedir]
                              for k, v in raw.items()}
                    log(f"[{system}] EmuMovies: listing indexed in "
                        f"Elasticsearch for fuzzy verification")
            except Exception:
                es_map = None
        out = []
        for g in still:
            check_stop()
            via, src, src_dir = "emumovies", None, None
            alias = _alias_title(g.description)
            for vd, pool in pools:
                src = (best_match(g.name, pool)
                       or (best_match(g.description, pool) if g.description else None)
                       or (best_match(alias, pool) if alias else None))
                if src:
                    src_dir = vd
                    if alias and src and norm(src).startswith(norm(alias)[:6]):
                        via = f"emumovies alias '{alias}'"
                    break
            if not src and es_map:
                from .renamer import _pct
                for fdir, cand in es_map.get(g.name, []):
                    score = max(_pct(g.name, cand),
                                _pct(g.description or g.name, cand))
                    if score >= 60:
                        src, src_dir = cand, fdir
                        via = f"emumovies es-match {score}%"
                        break
            if src and safe_name(g.name):
                ext = os.path.splitext(src)[1].lower()
                dst = os.path.join(folder, g.name + ext)
                try:
                    size = emu.download(f"{src_dir}/{src}", dst)
                    qm = re.search(r"\((HD|HQ|SQ)\)", src_dir)
                    q = qm.group(1) if qm else "?"
                    added.append({"system": system, "game": g.name,
                                  "art": "video", "source": f"{via}: {src}",
                                  "path": dst})
                    log(f"  + {g.name} video from EmuMovies [{q}] "
                        f"({size // 1024} KB"
                        + (", ES-verified" if "es-match" in via else "")
                        + (f", via alias" if "alias" in via else "") + ")")
                    purge_video_placeholders(folder, g.name, log)
                    continue
                except Exception as e:
                    log(f"    download failed for {g.name}: {e}")
            out.append(g)
        return out
    # wheels via the Logos pack (user rule: say what the zip contains -
    # verification and match counts must be visible in the log)
    pack = emu.find_logo_pack(system)
    if not pack:
        log(f"[{system}] EmuMovies: no Logos pack found")
        return still
    log(f"[{system}] EmuMovies: Logos pack located — "
        f"'{os.path.basename(pack)}'")
    zf = emu.fetch_logo_pack(pack, os.path.join(config.DATA_DIR, "emumovies_cache"))
    if not zf:
        log(f"[{system}] EmuMovies: pack could not be opened — skipping "
            f"this source")
        return still
    entries = zf.namelist()
    pool = {norm(n): n for n in entries
            if n.lower().endswith((".png", ".jpg"))}
    log(f"[{system}] EmuMovies: pack verified — {len(pool)} logo "
        f"image(s) inside ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'} total)")
    if not pool:
        log(f"[{system}] EmuMovies: the pack contains no usable png/jpg "
            f"logos — nothing to match")
        try:
            zf.close()
        except Exception:
            pass
        return still
    out = []
    matched = 0
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
                matched += 1
                continue
            except Exception as e:
                log(f"    wheel extract failed for {g.name}: {e}")
        out.append(g)
    if matched:
        log(f"[{system}] EmuMovies pack: {matched} wheel(s) identified "
            f"and added, {len(out)} still missing")
    else:
        log(f"[{system}] EmuMovies pack: NO matching wheels identified "
            f"for the {len(still)} missing game(s)")
    try:
        zf.close()
    except Exception:
        pass
    return out
