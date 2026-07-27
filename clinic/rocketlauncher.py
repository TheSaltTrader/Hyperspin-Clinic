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
    """All values of key (case-insensitive) in an ini file, tolerant.
    RocketLauncher's UI saves some inis as UTF-16; a UTF-8 read turns
    those into NUL-riddled text that matches nothing - detect by BOM and
    embedded NULs and decode accordingly."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif b"\x00" in raw[:200]:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
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


def effective_root(cfg) -> str:
    """The RocketLauncher root: the configured one, or auto-detected
    inside the HyperSpin folder (real cabs keep it right there, e.g.
    Arcade\\Rocketlauncher\\Settings\\...)."""
    rl = (cfg.get("rocketlauncher_root") or "").strip()
    if rl and looks_valid(rl):
        return rl
    hs = (cfg.get("hyperspin_root") or "").strip()
    if hs:
        for name in ("Rocketlauncher", "RocketLauncher", "RL"):
            cand = os.path.join(hs, name)
            if looks_valid(cand):
                return cand
    return ""


def rom_files(rl_root: str, system: str):
    """{lower stem: (folder, name)} across all of the system's rom
    folders (first folder wins on stem collisions - RL search order).
    A rom can be a FILE (stem = name without extension, honoring
    Rom_Extension when set) or a FOLDER named after the rom (disc dumps,
    PC/PS3/Switch games live as directories)."""
    exts = rom_extensions(rl_root, system)
    out = {}
    for folder in rom_paths(rl_root, system):
        if not os.path.isdir(folder):
            continue
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                out.setdefault(e.name.lower(), (folder, e.name))
                continue
            if exts and not e.name.lower().endswith(exts):
                continue
            stem = os.path.splitext(e.name)[0].lower()
            out.setdefault(stem, (folder, e.name))
    return out


def missing_roms(cfg, system):
    """(total_games, missing_count) using RL's configured rom folders;
    returns None when RocketLauncher is not configured or has no rom path
    for this system."""
    rl = effective_root(cfg)
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
