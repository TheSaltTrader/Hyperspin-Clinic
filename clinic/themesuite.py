# HyperSpin Theme Suite v3.7 integration (Themes tab).
# The suite stays where it lives (Setup: "Theme Suite folder"); every
# operation targets a system folder under the configured HyperSpin Media
# directly via the suite's own -BaseDir / base-dir arguments - no copying:
#   convert  powershell Convert-HyperSpin-Themes.ps1 -BaseDir <sys>
#            (4:3 -> 16:9 conversion, backup to <sys>\Themes_backup)
#   record   ThemeVideo\deno.exe render_batch.ts <sys>
#            (theme -> 1920x1080 video recordings in <sys>\ThemeVideos)
#   install  powershell ThemeVideo\Apply-VideoThemes.ps1 -BaseDir <sys>
#            (installs recordings; backs up to *_pre_videotheme folders)
# Reverts (used by the Revert tab) restore from the suite's own backups:
#   theme-convert -> <sys>\Themes_backup\*.zip copied back over Themes\
#   videotheme    -> Themes_backup_pre_videotheme + Video_backup_pre_videotheme
import os
import re
import subprocess
import time

from .artfinder import media_dir

CREATE_NO_WINDOW = 0x08000000


def suite_paths(suite_root):
    return {
        "converter": os.path.join(suite_root, "Convert-HyperSpin-Themes.ps1"),
        "deno": os.path.join(suite_root, "ThemeVideo", "deno.exe"),
        "render": os.path.join(suite_root, "ThemeVideo", "render_batch.ts"),
        "apply": os.path.join(suite_root, "ThemeVideo", "Apply-VideoThemes.ps1"),
        "deno_cache": os.path.join(suite_root, "ThemeVideo", "deno_cache"),
    }


def looks_valid(suite_root):
    p = suite_paths(suite_root)
    return os.path.isfile(p["converter"]) and os.path.isfile(p["render"])


def system_dir(cfg, system):
    return os.path.join(media_dir(cfg["hyperspin_root"]), system)


def _stream(cmd, log, stop_flag, cwd=None, env=None, on_line=None):
    """Run a subprocess, stream stdout lines into log; on stop request the
    whole process tree is killed (taskkill /T - the recorder spawns
    chrome/ffmpeg children). on_line(raw) sees every line first and may
    return True to swallow it from the log (progress markers)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", creationflags=CREATE_NO_WINDOW)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line and on_line and on_line(line):
                line = ""
            if line:
                log("    " + line[-160:])
            if stop_flag():
                log("    stopping - killing process tree…")
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
                return -1
    finally:
        proc.stdout.close()
    return proc.wait()


_PROG_RE = re.compile(r"^PROG (\d+)/(\d+) (.+)$")
_QUEUE_RE = re.compile(r"^QUEUE: (\d+) themes")
_RESULT_RE = re.compile(r"^(.+?\.zip)\t(RENDERED|FAILED)")


def convert_system(cfg, system, log, stop_flag, progress=None):
    """progress(i, total, name, eta_seconds|None) per theme, off-UI-thread."""
    p = suite_paths(cfg["theme_suite_root"])
    base = system_dir(cfg, system)
    if not os.path.isdir(os.path.join(base, "Themes")):
        log(f"[{system}] no Themes folder - skipped")
        return False
    log(f"[{system}] converting themes (backup -> Themes_backup)…")
    t0 = time.time()

    def on_line(line):
        m = _PROG_RE.match(line)
        if not m or not progress:
            return bool(m)
        i, total = int(m.group(1)), int(m.group(2))
        eta = (time.time() - t0) / i * (total - i) if i else None
        progress(i, total, m.group(3), eta)
        return True

    code = _stream(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", p["converter"], "-BaseDir", base, "-Progress"],
                   log, stop_flag, on_line=on_line)
    log(f"[{system}] converter exit code {code}")
    return code == 0


def record_system(cfg, system, log, stop_flag, progress=None):
    """progress(done, total, name, eta_seconds|None) per rendered theme."""
    p = suite_paths(cfg["theme_suite_root"])
    base = system_dir(cfg, system)
    env = dict(os.environ)
    env["DENO_DIR"] = p["deno_cache"]
    log(f"[{system}] recording theme videos (this can take a while)…")
    state = {"total": 0, "done": 0, "t0": None}

    def on_line(line):
        m = _QUEUE_RE.match(line)
        if m:
            state["total"] = int(m.group(1))
            state["t0"] = time.time()
            return False           # keep the QUEUE line in the log
        m = _RESULT_RE.match(line)
        if m and state["total"] and progress:
            state["done"] += 1
            d, tot = state["done"], state["total"]
            eta = ((time.time() - state["t0"]) / d * (tot - d)) if d else None
            progress(d, tot, m.group(1), eta)
        return False               # results always stay visible

    code = _stream([p["deno"], "run", "--cached-only", "-A", p["render"],
                    base, "--workers", "1"],
                   log, stop_flag, cwd=os.path.dirname(p["render"]), env=env,
                   on_line=on_line)
    log(f"[{system}] recorder exit code {code}")
    return code == 0


def install_system(cfg, system, log, stop_flag, progress=None):
    p = suite_paths(cfg["theme_suite_root"])
    base = system_dir(cfg, system)
    if not os.path.isdir(os.path.join(base, "ThemeVideos")):
        log(f"[{system}] no ThemeVideos recordings - skipped")
        return False
    log(f"[{system}] installing video themes (backups -> *_pre_videotheme)…")
    code = _stream(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", p["apply"], "-BaseDir", base],
                   log, stop_flag)
    log(f"[{system}] installer exit code {code}")
    return code == 0


# ---------------- per-system theme stats ----------------
def theme_stats(cfg, system):
    """Composition of a system's Themes folder, mirroring the converter's
    own classification rules (in-memory zip inspection, nothing written):
      flash     all art is SWF (converter leaves these untouched)
      video     the video IS the theme (no art, or a single fully
                transparent background png) - recorder skips these
      installed already replaced by a suite recording (Info.txt marker)
      raster    normal PNG/JPG themes -> 16:9 conversion candidates
    """
    import io
    import zipfile
    base = system_dir(cfg, system)
    tdir = os.path.join(base, "Themes")
    if not os.path.isdir(tdir):
        return None
    n = {"total": 0, "flash": 0, "video": 0, "installed": 0,
         "raster": 0, "c169": 0, "bad": 0}
    for fname in os.listdir(tdir):
        if not fname.lower().endswith(".zip"):
            continue
        n["total"] += 1
        try:
            with zipfile.ZipFile(os.path.join(tdir, fname)) as z:
                names = [i.filename for i in z.infolist()
                         if not i.filename.endswith("/")]
                base_names = [os.path.basename(m) for m in names]
                low = [b.lower() for b in base_names]
                if any(b == "info.txt" for b in low):
                    m = base_names[low.index("info.txt")]
                    idx = [os.path.basename(x) for x in names].index(m)
                    txt = z.read(names[idx]).decode("utf-8", "replace")
                    if "Rendered theme video" in txt:
                        n["installed"] += 1
                        continue
                    # mrfomt-convention marker: theme already 16:9 (the
                    # converter stamps its outputs the same way and
                    # bypasses marked themes on future runs)
                    if re.search(r"16\s*[:x]\s*9", txt):
                        n["c169"] += 1
                        continue
                art = [b for b in low
                       if not b.startswith(".")
                       and not b.endswith((".txt", ".xml", ".ini", ".db"))]
                if not art:
                    n["video"] += 1
                    continue
                if len(art) == 1 and art[0].startswith("background") \
                        and art[0].endswith(".png"):
                    try:
                        from PIL import Image
                        idx = low.index(art[0])
                        im = Image.open(io.BytesIO(z.read(names[idx])))
                        if "A" in im.getbands() and \
                                im.getchannel("A").getextrema()[1] == 0:
                            n["video"] += 1
                            continue
                    except Exception:
                        pass
                raster = [b for b in art if b.endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".bmp"))]
                swf = [b for b in art if b.endswith(".swf")]
                if not raster and swf:
                    n["flash"] += 1
                else:
                    n["raster"] += 1
        except Exception:
            n["bad"] += 1
    n["converted_already"] = os.path.isdir(os.path.join(base, "Themes_backup"))
    # converter processes raster themes; 16:9-marked ones are bypassed
    n["to_169"] = n["raster"]
    # recorder skips native video themes and installed recordings
    n["to_video"] = n["raster"] + n["flash"] + n["c169"]
    return n


def stats_line(cfg, system):
    """(text, ok) for the Themes tab system list."""
    n = theme_stats(cfg, system)
    if n is None:
        return "no Themes folder", False
    if n["total"] == 0:
        return "no themes", False
    comp = [f"{n['total']} themes:"]
    parts = []
    if n["raster"]:
        parts.append(f"{n['raster']} png")
    if n["c169"]:
        parts.append(f"{n['c169']} already 16:9")
    if n["flash"]:
        parts.append(f"{n['flash']} flash")
    if n["video"]:
        parts.append(f"{n['video']} video")
    if n["installed"]:
        parts.append(f"{n['installed']} installed")
    if n["bad"]:
        parts.append(f"{n['bad']} unreadable")
    comp.append(", ".join(parts) if parts else "empty")
    tail = (f" | to 16:9: {n['to_169']}"
            + (" (backup present)" if n["converted_already"] else "")
            + f" | to video theme: {n['to_video']}")
    done = n["converted_already"] and n["to_video"] == 0
    return " ".join(comp) + tail, done


# ---------------- revert support ----------------
def backup_counts(cfg, system):
    """{'theme-convert': n, 'videotheme': n} from the suite's backups."""
    base = system_dir(cfg, system)
    out = {}
    tb = os.path.join(base, "Themes_backup")
    if os.path.isdir(tb):
        n = sum(1 for f in os.listdir(tb) if f.lower().endswith(".zip"))
        if n:
            out["theme-convert"] = n
    n = 0
    for d in ("Themes_backup_pre_videotheme", "Video_backup_pre_videotheme"):
        p = os.path.join(base, d)
        if os.path.isdir(p):
            n += len([f for f in os.listdir(p)
                      if os.path.isfile(os.path.join(p, f))])
    if n:
        out["videotheme"] = n
    return out


def _restore_folder(src, dst, log, label):
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)
    import shutil
    n = 0
    for f in os.listdir(src):
        sp = os.path.join(src, f)
        if os.path.isfile(sp):
            shutil.copy2(sp, os.path.join(dst, f))
            n += 1
    log(f"    restored {n} file(s) from {label}")
    return n


def revert_theme_conversion(cfg, system, log):
    """Copy the pristine originals from Themes_backup back over Themes.
    The backup folder is kept - the suite treats it as the canonical
    rebuild source, so this revert is safely repeatable."""
    base = system_dir(cfg, system)
    log(f"[{system}] restoring original 4:3 themes from Themes_backup…")
    return _restore_folder(os.path.join(base, "Themes_backup"),
                           os.path.join(base, "Themes"), log, "Themes_backup")


def revert_videothemes(cfg, system, log):
    """Undo a video-theme install: original theme zips and original video
    snaps come back from the *_pre_videotheme backups."""
    base = system_dir(cfg, system)
    log(f"[{system}] restoring pre-videotheme themes and snaps…")
    n = _restore_folder(os.path.join(base, "Themes_backup_pre_videotheme"),
                        os.path.join(base, "Themes"), log,
                        "Themes_backup_pre_videotheme")
    n += _restore_folder(os.path.join(base, "Video_backup_pre_videotheme"),
                         os.path.join(base, "Video"), log,
                         "Video_backup_pre_videotheme")
    return n
