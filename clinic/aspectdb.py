# Display-aspect knowledge base (user rule): the database knows which
# systems are 4:3, which are 16:9, and which are MIXED so YouTube
# results are cropped correctly per GAME, not just per system.
#
# Confirmed baseline: before ~2000 every standard cabinet and console
# displayed 4:3 (vertical shooters are a rotated 3:4 on the same
# monitor); only rare multi-screen cabinets (the Darius series' triple
# 4:3) fall outside it, and those are handled by per-game overrides.
# From the 2000s on, ARCADE platforms vary by title - NesicaXlive,
# Taito Type X, Sega Lindbergh/RingEdge/RingWide, exA-Arcadia run 4:3
# re-releases next to 16:9 games - so those are classified "mixed" and
# decided per game.
#
# data\aspect_db.json is hand-editable to train the software:
#   {"systems": {"Some System": "16:9"},
#    "games":   {"NESiCAxLive::crimzonclover": "4:3"}}
# Auto-detected decisions (from the downloaded video's own content,
# after bar-cropping) are written back per game and tracked in the
# vector database, so the DB keeps learning.
import json
import os
import re

from . import config

# widescreen-only systems
_WIDE = ("ps3", "playstation 3", "ps4", "playstation 4", "ps5",
         "playstation 5", "xbox 360", "xbox one", "series x",
         "xbla", "xbox live", "wii u", "switch", "psp", "vita",
         "pc games", "steam", "windows", "teknoparrot")
# post-2000 arcade platforms whose games vary by title
_MIXED = ("nesica", "taito type x", "type x", "lindbergh", "ringedge",
          "ringwide", "exa arcadia", "system 357", "system 369")


def _path():
    return os.path.join(config.DATA_DIR, "aspect_db.json")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        db = {}
    db.setdefault("systems", {})
    db.setdefault("games", {})
    return db


def save(db) -> None:
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(_path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(db, f, indent=1)
        os.replace(_path() + ".tmp", _path())
    except Exception:
        pass


def _nrm(s):
    return " " + re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()) + " "


def system_class(system, db=None) -> str:
    """'4:3', '16:9' or 'mixed' for a system. Trained override first,
    then the token tables, else the pre-2000 baseline: 4:3."""
    db = db if db is not None else load()
    o = db["systems"].get(system) or db["systems"].get((system or "").strip())
    if o in ("4:3", "16:9", "mixed"):
        return o
    s = _nrm(system)
    for tok in _MIXED:
        if tok in s:
            return "mixed"
    for tok in _WIDE:
        if tok in s:
            return "16:9"
    return "4:3"


def _year_int(year):
    m = re.search(r"\d{4}", str(year or ""))
    return int(m.group()) if m else None


def game_aspect(system, game, year=None, db=None):
    """'4:3', '16:9' or None (= decide from the video's own content).
    Ladder: per-game override -> system class -> for mixed systems the
    game's year (pre-2000 titles, e.g. re-releases, are 4:3) -> None."""
    db = db if db is not None else load()
    o = db["games"].get(f"{system}::{game}")
    if o in ("4:3", "16:9"):
        return o
    cls = system_class(system, db)
    if cls in ("4:3", "16:9"):
        return cls
    y = _year_int(year)
    if y and y < 2000:
        return "4:3"
    return None


def record(cfg, system, game, aspect, source, log=None):
    """Persist a per-game decision and track it in the database so the
    game is identified next time (user rule). Best-effort: never fails
    the download that produced it."""
    if aspect not in ("4:3", "16:9"):
        return
    db = load()
    if db["games"].get(f"{system}::{game}") == aspect:
        return
    db["games"][f"{system}::{game}"] = aspect
    save(db)
    if log:
        log(f"    aspect learned: {game} ({system}) is {aspect} [{source}]")
    try:
        from . import store
        from .ingest import Chunk
        store.track(cfg or {}, [Chunk(
            id=f"aspect:{system}:{game}",
            text=(f"Display aspect: '{game}' on {system} is {aspect} "
                  f"({source})."),
            source=_path(), kind="aspect",
            meta={"system": system, "game": game, "aspect": aspect},
        )], log or (lambda m: None))
    except Exception:
        pass
