# yt-dlp self-management (user rule: YouTube breaks whenever yt-dlp gets
# stale - "The page needs to be reloaded" class failures). The app keeps
# its OWN standalone yt-dlp.exe under data\tools\ so updating never
# depends on the user's Python/pip:
#   - at startup the app compares the active copy against the latest
#     GitHub release and ASKS the user before installing
#   - the download is the official standalone exe, written atomically
import json
import os
import re
import subprocess
import urllib.request

from . import config

LATEST_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
EXE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"


def tools_dir() -> str:
    return os.path.join(config.DATA_DIR, "tools")


def managed_exe() -> str:
    return os.path.join(tools_dir(), "yt-dlp.exe")


def installed_version(path) -> "str | None":
    try:
        r = subprocess.run([path, "--version"], capture_output=True,
                           timeout=30, creationflags=0x08000000)
        v = r.stdout.decode(errors="replace").strip()
        return v if re.match(r"^\d{4}\.\d{2}\.\d{2}", v) else None
    except Exception:
        return None


def latest_version() -> "str | None":
    try:
        req = urllib.request.Request(
            LATEST_API, headers={"User-Agent": "HyperSpinClinic",
                                 "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("tag_name", "").strip() or None
    except Exception:
        return None


def _ver_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def is_newer(latest: str, current: "str | None") -> bool:
    if not latest:
        return False
    if not current:
        return True
    try:
        return _ver_tuple(latest) > _ver_tuple(current)
    except Exception:
        return False


def download_latest(log=print) -> bool:
    """Fetch the official standalone exe into data\\tools (atomic)."""
    os.makedirs(tools_dir(), exist_ok=True)
    dst = managed_exe()
    tmp = dst + ".tmp"
    try:
        req = urllib.request.Request(EXE_URL,
                                     headers={"User-Agent": "HyperSpinClinic"})
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        if os.path.getsize(tmp) < 1_000_000:
            raise OSError("download too small")
        os.replace(tmp, dst)
        v = installed_version(dst)
        log(f"yt-dlp {v or '?'} installed to {dst}")
        return True
    except Exception as e:
        log(f"yt-dlp update failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def check(current_path) -> "tuple[str | None, str | None]":
    """(current_version, latest_version_if_newer_or_missing). Meant for a
    background thread at startup; both None means nothing to do."""
    cur = installed_version(current_path) if current_path else None
    latest = latest_version()
    if is_newer(latest, cur):
        return cur, latest
    return cur, None
