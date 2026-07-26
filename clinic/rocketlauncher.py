# RocketLauncher integration: rom locations come from RocketLauncher's own
# per-system settings instead of a hand-picked folder, so every tab shares
# one centralized source of truth.
#   <RL>\Settings\<System>\Emulators.ini
#       [ROMS]
#       Rom_Path=D:\ROMs\MAME|E:\More ROMs\MAME     (| separated)
#       Rom_Extension=zip|7z|chd                    (optional)
#   <RL>\Settings\Global Emulators.ini              (fallback)
# RL inis are parsed tolerantly (regex for the keys, case-insensitive) -
# real-world files often have duplicate keys or stray content that breaks
# configparser.
import os
import re

from . import hyperspin_db as hdb


def settings_dir(rl_root: str) -> str:
    for base in (rl_root, os.path.join(rl_root, "RocketLauncher")):
        p = os.path.join(base, "Settings")
        if os.path.isdir(p):
            return p
    return ""


def looks_valid(rl_root: str) -> bool:
    return bool(settings_dir(rl_root))


def _read_ini_key(path, key):
    """All values of key (case-insensitive) in an ini file, tolerant."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return [m.group(1).strip() for m in
            re.finditer(r"(?im)^\s*" + re.escape(key) + r"\s*=\s*(.+?)\s*$",
                        text)]


def rom_paths(rl_root: str, system: str):
    """The rom folder(s) RocketLauncher uses for a system ('|' separated in
    Rom_Path). Relative paths resolve against the RL root."""
    sd = settings_dir(rl_root)
    if not sd:
        return []
    candidates = [os.path.join(sd, system, "Emulators.ini"),
                  os.path.join(sd, "Global Emulators.ini")]
    raw = []
    for ini in candidates:
        if os.path.isfile(ini):
            raw = _read_ini_key(ini, "Rom_Path")
            if raw:
                break
    out = []
    base = os.path.dirname(sd.rstrip("\\/"))
    for entry in raw:
        for part in entry.split("|"):
            part = part.strip().strip('"')
            if not part:
                continue
            if not os.path.isabs(part):
                part = os.path.normpath(os.path.join(base, part))
            if part not in out:
                out.append(part)
    return out


def rom_extensions(rl_root: str, system: str):
    """Rom_Extension list (lowercased, dotted) or None = any extension."""
    sd = settings_dir(rl_root)
    if not sd:
        return None
    for ini in (os.path.join(sd, system, "Emulators.ini"),
                os.path.join(sd, "Global Emulators.ini")):
        if os.path.isfile(ini):
            vals = _read_ini_key(ini, "Rom_Extension")
            if vals:
                exts = tuple("." + e.strip().lstrip(".").lower()
                             for e in vals[0].split("|") if e.strip())
                return exts or None
    return None


def rom_files(rl_root: str, system: str):
    """{lower stem: (folder, filename)} across all of the system's rom
    folders (first folder wins on stem collisions - RL search order)."""
    exts = rom_extensions(rl_root, system)
    out = {}
    for folder in rom_paths(rl_root, system):
        if not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for f in names:
            if exts and not f.lower().endswith(exts):
                continue
            if not os.path.isfile(os.path.join(folder, f)):
                continue
            stem = os.path.splitext(f)[0].lower()
            out.setdefault(stem, (folder, f))
    return out


def missing_roms(cfg, system):
    """(total_games, missing_count) using RL's configured rom folders;
    returns None when RocketLauncher is not configured or has no rom path
    for this system."""
    rl = cfg.get("rocketlauncher_root", "")
    if not rl or not rom_paths(rl, system):
        return None
    xml = hdb.system_xml_path(cfg["hyperspin_root"], system)
    try:
        games = hdb.parse_games(hdb.read_db_text(xml)[0])
    except OSError:
        return None
    have = rom_files(rl, system)
    missing = sum(1 for g in games if g.name.lower() not in have)
    return (len(games), missing)


def inspect(rl_root: str):
    """Setup-tab summary: how many systems have a rom path configured."""
    sd = settings_dir(rl_root)
    if not sd:
        return {"valid": False, "systems": 0}
    n = 0
    try:
        for e in os.scandir(sd):
            if e.is_dir() and os.path.isfile(os.path.join(e.path, "Emulators.ini")):
                if _read_ini_key(os.path.join(e.path, "Emulators.ini"), "Rom_Path"):
                    n += 1
    except OSError:
        pass
    return {"valid": True, "systems": n, "settings": sd}
