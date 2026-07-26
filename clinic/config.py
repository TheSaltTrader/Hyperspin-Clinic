# Settings persistence: settings.json next to main.py. NO SECRETS live
# here (the API key is in the Windows Credential Manager - see secrets.py);
# this file only holds the HyperSpin folder and non-sensitive options.
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
DATA_DIR = os.path.join(ROOT, "data")

DEFAULTS = {
    "hyperspin_root": "",
    "roms_dir": "",
    "rocketlauncher_root": "",
    "theme_suite_root": r"C:\Users\renoi\ClaudeCode\Mame Project\Release",
    "es_url": "http://localhost:9200",
    "es_index": "hyperspin-clinic",
    "chroma_collection": "hyperspin_clinic",
    # default deliberately NOT the most expensive model - Haiku handles
    # metadata fill well at a fraction of the cost; pick a bigger model
    # in Setup if you want it
    "model": "claude-haiku-4-5",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        pass  # corrupt settings: fall back to defaults, don't crash the UI
    cfg.pop("anthropic_api_key", None)   # legacy plaintext keys are never kept
    return cfg


def save(cfg: dict) -> None:
    keep = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def chroma_dir() -> str:
    p = os.path.join(DATA_DIR, "chroma")
    os.makedirs(p, exist_ok=True)
    return p


def inspect_hyperspin(root: str) -> dict:
    """Identify what lives under a HyperSpin folder. Accepts either the
    HyperSpin root (containing Media\\) or the Media folder itself.
    Returns a summary dict: {valid, media, systems: [{name, themes, snaps}]}"""
    out = {"valid": False, "media": "", "systems": []}
    if not root or not os.path.isdir(root):
        return out
    media = ""
    if os.path.isdir(os.path.join(root, "Media")):
        media = os.path.join(root, "Media")
    else:
        # maybe the user picked Media itself (subfolders with Themes\)
        try:
            for e in os.scandir(root):
                if e.is_dir() and os.path.isdir(os.path.join(e.path, "Themes")):
                    media = root
                    break
        except OSError:
            return out
    if not media:
        return out
    for e in sorted(os.scandir(media), key=lambda x: x.name.lower()):
        if not e.is_dir():
            continue
        themes_dir = os.path.join(e.path, "Themes")
        if not os.path.isdir(themes_dir):
            continue
        try:
            themes = sum(1 for f in os.scandir(themes_dir)
                         if f.name.lower().endswith(".zip"))
        except OSError:
            themes = 0
        video_dir = os.path.join(e.path, "Video")
        snaps = 0
        if os.path.isdir(video_dir):
            try:
                snaps = sum(1 for f in os.scandir(video_dir)
                            if f.name.lower().endswith((".mp4", ".flv", ".avi")))
            except OSError:
                pass
        out["systems"].append({"name": e.name, "themes": themes, "snaps": snaps})
    out["valid"] = bool(out["systems"])
    out["media"] = media
    return out
