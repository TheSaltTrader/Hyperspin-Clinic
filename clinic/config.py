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


def auto_theme_suite() -> str:
    """The Theme Suite ships WITH the application - locate it automatically
    (no Setup field). Search order:
      1. theme_suite\\ next to the exe (packaged folder build)
      2. theme_suite\\ in the source tree (running from source)
      3. the development suite location (this machine only)
    Returns "" when none exists; the Themes tab reports that state."""
    import sys
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable),
                                       "theme_suite"))
    candidates.append(os.path.join(ROOT, "theme_suite"))
    candidates.append(DEFAULTS["theme_suite_root"])
    for c in candidates:
        if os.path.isfile(os.path.join(c, "Convert-HyperSpin-Themes.ps1")):
            return c
    return ""


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
    # the suite location is resolved fresh on every load - it travels with
    # the app, the user never configures it
    cfg["theme_suite_root"] = auto_theme_suite()
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


def games_count(root: str, system: str) -> int:
    """Number of <game> entries in a system's database XML (0 if absent).
    Encoding-tolerant via read_db_text (UTF-8 / UTF-16 / cp1252)."""
    import re as _re
    from . import hyperspin_db as hdb
    p = os.path.join(root, "Databases", system, system + ".xml")
    try:
        text = hdb.read_db_text(p)[0]
        return len(_re.findall(r'<game\b[^>]*?\bname\s*=', text))
    except OSError:
        return 0


def inspect_hyperspin(root: str) -> dict:
    """Identify what lives under a HyperSpin folder. The expected pick is
    the ACTUAL HyperSpin root (Media\\ and Databases\\ underneath); the
    Media folder itself is tolerated for robustness.
    The system list is exactly Databases\\Main\\Main.xml (user rule: only
    systems listed there are worked on, on every tab) - Media folders that
    are not in Main.xml are ignored.
    Returns a summary dict: {valid, media, systems: [{name, themes, snaps,
    games}]}"""
    from . import hyperspin_db as hdb
    out = {"valid": False, "media": "", "systems": [], "reason": ""}
    if not root or not os.path.isdir(root):
        out["reason"] = "That folder does not exist."
        return out
    game_root = root
    if os.path.basename(root.rstrip("\\/")).lower() == "media":
        game_root = os.path.dirname(root.rstrip("\\/"))
    media = ""
    if os.path.isdir(os.path.join(game_root, "Media")):
        media = os.path.join(game_root, "Media")
    systems = hdb.list_systems(game_root)
    if not systems:
        # tell the user PRECISELY what failed - "invalid" alone sends
        # people hunting the wrong problem
        if not hdb.databases_dir(game_root):
            out["reason"] = ("No Databases folder found under "
                             f"{game_root}.")
        elif not os.path.isfile(hdb.main_xml_path(game_root)):
            out["reason"] = ("Databases\\Main Menu\\Main Menu.xml not "
                             f"found under {game_root}.")
        else:
            out["reason"] = ("The main menu database contains no "
                             "readable <game name=\"...\"> entries.")
        return out
    for name in systems:
        themes = snaps = 0
        if media:
            themes_dir = os.path.join(media, name, "Themes")
            if os.path.isdir(themes_dir):
                try:
                    themes = sum(1 for f in os.scandir(themes_dir)
                                 if f.name.lower().endswith(".zip"))
                except OSError:
                    pass
            video_dir = os.path.join(media, name, "Video")
            if os.path.isdir(video_dir):
                try:
                    snaps = sum(1 for f in os.scandir(video_dir)
                                if f.name.lower().endswith(
                                    (".mp4", ".flv", ".avi")))
                except OSError:
                    pass
        out["systems"].append({"name": name, "themes": themes,
                               "snaps": snaps,
                               "games": games_count(game_root, name)})
    out["valid"] = True
    out["media"] = media
    return out
