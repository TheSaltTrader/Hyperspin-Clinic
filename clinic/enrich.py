# Metadata enrichment: fill year / manufacturer / genre for every game of
# the selected systems (same job as the earlier media projects, now
# in-app). Flow per system:
#   1. parse Databases\<System>\<System>.xml
#   2. games missing any of the three fields go to Claude in batches
#      (compact JSON in/out; the model knows arcade/console catalogs)
#   3. apply updates to the XML (fill-empty only, timestamped backup)
#   4. upsert EVERY game into the vector DB and Elasticsearch under the
#      system's name so the data is searchable and AI-updatable later
import json
import re

import anthropic

from . import es as es_mod
from . import hyperspin_db as hdb
from . import secrets, store

BATCH = 40

PROMPT = (
    "You are filling missing metadata for a {system} game list from a "
    "HyperSpin arcade frontend database. For each entry give the original "
    "release year (4 digits), the manufacturer/publisher, and ONE genre "
    "from common arcade genre names (Shooter, Platform, Fighter, Puzzle, "
    "Racing, Sports, Maze, Beat-'em-Up, Pinball, Gambling, Quiz, "
    "Shoot-'em-Up, Run and Gun, Adventure, RPG, Music, Party, Other). "
    "Use the rom name and description to identify the game. If you truly "
    "cannot identify a game, use empty strings for its fields — never "
    "guess wildly.\n"
    "Reply with ONLY a JSON array, one object per input entry, format: "
    '{{"name": "<rom>", "year": "1987", "manufacturer": "Capcom", '
    '"genre": "Shooter"}}\n\n'
    "Entries:\n{entries}"
)


class StopRequested(Exception):
    pass


def metadata_stats_line(cfg, system):
    """(text, ok) for the Systems tab list: ONLY metadata completeness -
    how many games still miss year / manufacturer / genre."""
    from . import hyperspin_db as hdb
    xml = hdb.system_xml_path(cfg.get("hyperspin_root", ""), system)
    try:
        games = hdb.parse_games(hdb.read_db_text(xml)[0])
    except OSError:
        return "no database XML for this system", False
    if not games:
        return "no games in database", False
    incomplete = sum(1 for g in games
                     if not (g.year and g.manufacturer and g.genre))
    if incomplete == 0:
        return f"✓ metadata complete ({len(games)} games)", True
    return (f"⚠ {incomplete} game(s) missing metadata "
            f"of {len(games)}", False)


def _claude(cfg):
    key = secrets.load()
    if not key:
        raise RuntimeError("No Claude API key stored (Setup tab).")
    return anthropic.Anthropic(api_key=key)


def _ask_batch(client, cfg, system, games, examples=None, log=None):
    entries = "\n".join(
        f'- name="{g.name}" description="{g.description}"' for g in games)
    # RAG grounding: retrieved examples of already-complete entries from
    # the same database anchor the model's genre vocabulary and
    # manufacturer spellings, keeping new values consistent with what the
    # collection already uses
    ground = ""
    if examples:
        ex = "\n".join(
            f'- "{e.description or e.name}": year={e.year}, '
            f"manufacturer={e.manufacturer}, genre={e.genre}" for e in examples)
        ground = ("\nExisting entries from this database (match their style "
                  "and vocabulary):\n" + ex + "\n")
    model = cfg.get("model", "claude-haiku-4-5")
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user",
                   "content": PROMPT.format(system=system, entries=entries)
                   + ground}],
    )
    # cost transparency: price the call from its real token usage and
    # keep the app-lifetime running total (data\api_spend.json)
    if log is not None:
        try:
            from . import costs
            u = resp.usage
            cost = costs.estimate(model, u.input_tokens, u.output_tokens)
            total = costs.record_spend(cost)
            if cost is not None:
                log(f"    API cost ≈ ${cost:.4f} "
                    f"({u.input_tokens} in / {u.output_tokens} out tokens) — "
                    f"app total ≈ ${total['total_usd']:.2f}")
            else:
                log(f"    API usage: {u.input_tokens} in / "
                    f"{u.output_tokens} out tokens (pricing n/a for {model})")
        except Exception:
            pass
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in arr:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = {
                "year": str(item.get("year", "") or "")[:4],
                "manufacturer": str(item.get("manufacturer", "") or "")[:80],
                "genre": str(item.get("genre", "") or "")[:40],
            }
    return out


def _index_games(cfg, system, games):
    """Upsert the system's games into Chroma + ES (per-system name key)."""
    from .ingest import Chunk
    chunks = []
    for g in games:
        text = (f"System {system} game {g.name}: {g.description}. "
                f"Year: {g.year}. Manufacturer: {g.manufacturer}. "
                f"Genre: {g.genre}.")
        chunks.append(Chunk(
            id=f"game:{system}:{g.name}",
            text=text,
            source=hdb.system_xml_path(cfg["hyperspin_root"], system),
            kind="game",
            meta={"game": g.name, "system": system, "year": g.year,
                  "manufacturer": g.manufacturer, "genre": g.genre},
        ))
    col = store.collection(cfg)
    store.add_chunks(col, chunks)
    if es_mod.available(cfg):
        es_mod.ensure_index(cfg)
        es_mod.add_chunks(cfg, chunks)
        return True
    return False


def enrich_system(cfg, system, log, stop_flag, only_fill_empty=True, progress=None):
    """Enrich one system. log: callable(msg). stop_flag: callable() -> bool.
    Returns summary dict."""
    xml_path = hdb.system_xml_path(cfg["hyperspin_root"], system)
    try:
        text = hdb.read_db_text(xml_path)[0]
    except OSError as e:
        log(f"[{system}] SKIP: cannot read {xml_path} ({e})")
        return {"system": system, "games": 0, "updated": 0, "skipped": True}
    games = hdb.parse_games(text)
    missing = [g for g in games
               if not (g.year and g.manufacturer and g.genre)]
    log(f"[{system}] {len(games)} games, {len(missing)} missing metadata")
    client = _claude(cfg) if missing else None
    # retrieval pool for RAG grounding: complete entries from this system
    complete = [g for g in games if g.year and g.manufacturer and g.genre]
    examples = complete[:8]
    updates = {}
    if progress:
        progress(0.0, f"{system}: {len(missing)} game(s) to enrich")
    for i in range(0, len(missing), BATCH):
        if stop_flag():
            raise StopRequested()
        part = missing[i:i + BATCH]
        if progress:
            progress(i / max(1, len(missing)),
                     f"{system}: asking AI — batch {i // BATCH + 1}/"
                     f"{(len(missing) + BATCH - 1) // BATCH}")
        try:
            updates.update(_ask_batch(client, cfg, system, part, examples,
                                      log=log))
        except anthropic.APIError as e:
            log(f"[{system}] batch {i // BATCH + 1}: API error {e.__class__.__name__} — continuing")
        log(f"[{system}] enriched {min(i + BATCH, len(missing))}/{len(missing)}")
    if progress:
        progress(0.9, f"{system}: writing XML + indexing")
    changed = 0
    if updates:
        changed = hdb.apply_updates(xml_path, updates,
                                    only_fill_empty=only_fill_empty)
        log(f"[{system}] XML updated: {changed} game(s) (backup taken)")
    # refresh in-memory games with the new values before indexing
    games = hdb.parse_games(hdb.read_db_text(xml_path)[0])
    # skip the expensive full re-embed when nothing changed and the system
    # is already indexed (a no-op run on MAME would re-embed 30k+ games)
    already = False
    if changed == 0 and games:
        try:
            col = store.collection(cfg)
            probe = col.get(ids=[f"game:{system}:{games[0].name}"])
            already = bool(probe and probe.get("ids"))
        except Exception:
            already = False
    if already:
        log(f"[{system}] no changes - index already up to date, skipping reindex")
        es_ok = es_mod.available(cfg)
    else:
        es_ok = _index_games(cfg, system, games)
        log(f"[{system}] indexed {len(games)} games "
            f"({'vector+elasticsearch' if es_ok else 'vector only — ES offline'})")
    return {"system": system, "games": len(games), "updated": changed,
            "skipped": False}
