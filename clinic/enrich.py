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
import os
import re
import time

import anthropic

from . import config
from . import es as es_mod
from . import hyperspin_db as hdb
from . import secrets, store

BATCH = 40

PROMPT = (
    "You are filling missing metadata for a {system} game list from a "
    "HyperSpin frontend database. The list can mix classic arcade games "
    "with modern console/PC titles — the description often carries a "
    "platform hint like (PC), (PS5) or (ARC); use it, and take care to "
    "pick the RIGHT game when a title matches several (e.g. a 2024 "
    "reboot vs. a 1991 arcade original). For each entry give the "
    "original release year (4 digits), the manufacturer/publisher, and "
    "ONE genre — prefer these names (Shooter, Platform, Fighter, Puzzle, "
    "Racing, Sports, Maze, Beat-'em-Up, Pinball, Gambling, Quiz, "
    "Shoot-'em-Up, Run and Gun, Adventure, RPG, Music, Party, Other) "
    "but another fitting one-word genre is allowed. If you truly "
    "cannot identify a game, use empty strings for its fields — never "
    "guess wildly.\n"
    "Reply with ONLY a JSON array, one object per input entry, format: "
    '{{"name": "<rom>", "year": "1987", "manufacturer": "Capcom", '
    '"genre": "Shooter"}}\n\n'
    "Entries:\n{entries}"
)

SEARCH_NOTE = (
    "\nYou have a web search tool. USE IT to look up any game you do not "
    "confidently know — many entries are recent (2024-2025) releases "
    "past your training data. Verify the year and publisher before "
    "answering; still reply with ONLY the JSON array at the end.\n"
)

# second pass for games the model could not identify from knowledge:
# smaller batches (searches add tokens fast)
SEARCH_BATCH = 8


# ---------- enrichment cache (cost control) ----------
# Every answer - identified OR unidentifiable - is remembered in
# data\enrich_cache.json, keyed by the game's normalized description:
#  - identified games are reused FREE across runs and systems (Favorites
#    duplicates other wheels' games; clone sets share one description)
#  - games that stayed unknown even after web search are NOT re-searched
#    on later runs (each retry costs real money) unless the user ticks
#    "Retry games that previously failed"
CACHE_FILE = "enrich_cache.json"


def _cache_key(g) -> str:
    s = (g.description or g.name).lower()
    return re.sub(r"[^a-z0-9()]+", " ", s).strip()


def _load_cache() -> dict:
    try:
        with open(os.path.join(config.DATA_DIR, CACHE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        p = os.path.join(config.DATA_DIR, CACHE_FILE)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(p + ".tmp", p)
    except Exception:
        pass   # cache is an optimization - never fail the run over it


class StopRequested(Exception):
    pass


class IndexingUnavailable(Exception):
    """Vector indexing failed (embedder/runtime unavailable). The XML
    update is already complete - callers warn and continue."""


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


def _parse_reply(text, games):
    """Model reply -> {canonical_rom_name: fields}.
    Hardened after the 'Favorites: 136 games never enriched' report:
      - a reply cut off at the token cap has no closing ']' — salvage
        every complete object instead of silently dropping the batch
      - echoed names are matched back to the batch case-insensitively
        (an echo of "PacMan" for rom "pacman" must still land)"""
    m = re.search(r"\[.*\]", text, re.S)
    items = None
    if m:
        try:
            items = json.loads(m.group(0))
        except json.JSONDecodeError:
            items = None
    if items is None:
        items = []
        for om in re.finditer(r"\{[^{}]*\}", text, re.S):
            try:
                items.append(json.loads(om.group(0)))
            except json.JSONDecodeError:
                pass
    real = {}
    for g in games:
        real[g.name.lower()] = g.name
        if g.description:
            real.setdefault(g.description.strip().lower(), g.name)
    out = {}
    for item in items:
        if not (isinstance(item, dict) and item.get("name")):
            continue
        key = real.get(str(item["name"]).strip().lower())
        if key is None:
            continue
        out[key] = {
            "year": str(item.get("year", "") or "")[:4],
            "manufacturer": str(item.get("manufacturer", "") or "")[:80],
            "genre": str(item.get("genre", "") or "")[:40],
        }
    return out


def _ask_batch(client, cfg, system, games, examples=None, log=None, depth=0,
               web_search=False):
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
    kwargs = {}
    if web_search:
        # ONE search per game: at ~$0.01/search a 2x allowance doubled the
        # worst-case bill for no measurable gain (6/6 filled with 1 each)
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                            "max_uses": min(12, len(games))}]
    # 8000 (was 4000): a 40-game reply that outgrew the cap lost the WHOLE
    # batch silently — the cause of large "never enriched" gaps
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        messages=[{"role": "user",
                   "content": PROMPT.format(system=system, entries=entries)
                   + (SEARCH_NOTE if web_search else "") + ground}],
        **kwargs,
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
            searches = getattr(getattr(u, "server_tool_use", None),
                               "web_search_requests", 0) or 0
            if searches:
                log(f"    web searches: {searches} (≈${searches * 0.01:.2f})")
        except Exception:
            pass
    text = "".join(b.text for b in resp.content if b.type == "text")
    out = _parse_reply(text, games)
    truncated = resp.stop_reason == "max_tokens"
    unanswered = [g for g in games if g.name not in out]
    if log and truncated:
        log(f"    WARNING: AI reply hit the length cap — "
            f"{len(unanswered)} game(s) unanswered")
    elif log and not out:
        log(f"    WARNING: could not parse the AI reply — "
            f"{len(games)} game(s) unchanged this batch")
    # a batch must never be lost silently: retry the unanswered remainder
    # in halves (a shorter reply fits the cap; depth-capped so a stubborn
    # failure cannot loop)
    if unanswered and (truncated or not out) and depth < 3 and len(games) > 1:
        if log:
            log(f"    retrying {len(unanswered)} unanswered game(s) in "
                f"smaller batches")
        half = max(1, (len(unanswered) + 1) // 2)
        for i in range(0, len(unanswered), half):
            out.update(_ask_batch(client, cfg, system, unanswered[i:i + half],
                                  examples, log, depth + 1, web_search))
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
    # indexing must NEVER block the real work - the XML update already
    # succeeded; a broken embedder (missing runtime, no model) degrades
    # to keyword-only search instead of failing the run
    try:
        col = store.collection(cfg)
        store.add_chunks(col, chunks)
    except Exception as e:
        raise IndexingUnavailable(str(e))
    if es_mod.available(cfg):
        es_mod.ensure_index(cfg)
        es_mod.add_chunks(cfg, chunks)
        return True
    return False


# cache-reuse fuzziness per UI level (user rule: same selector logic as
# the Missing Art tab). Exact stays today's behavior; the fuzzy levels
# let NEAR-IDENTICAL descriptions (region variants: '(USA)' vs
# '(Europe)') reuse a cached answer for free - guarded so numbering must
# agree (Street Fighter II can never reuse Street Fighter III's answer).
_CACHE_CUTOFFS = {"Exact only": None, "Precise": 0.95,
                  "Standard (recommended)": 0.90, "Loose": 0.85}
DEFAULT_MATCH_LEVEL = "Standard (recommended)"


def _base_key(k):
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", k)).strip()


def _fuzzy_cache_hit(cache, keys, stripped, key, cutoff):
    import difflib
    from .emumovies import _numbers

    def usable(c):
        if _numbers(c) != _numbers(key):
            return None                 # sequels never borrow answers
        hit = cache.get(c)
        return hit if (hit and hit.get("year") and hit.get("manufacturer")
                       and hit.get("genre")) else None
    # region variants FIRST: '(USA)' vs '(Europe)' is an exact match once
    # the parenthesized tags are stripped
    c = stripped.get(_base_key(key))
    if c:
        hit = usable(c)
        if hit:
            return hit
    for c in difflib.get_close_matches(key, keys, n=3, cutoff=cutoff):
        hit = usable(c)
        if hit:
            return hit
    return None


def enrich_system(cfg, system, log, stop_flag, only_fill_empty=True, progress=None,
                  web_search=False, retry_failed=False,
                  match_level=DEFAULT_MATCH_LEVEL):
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
    updates = {}
    # ---- cost control: cache + clone dedup (no API call for any of it) --
    cache = _load_cache()
    cutoff = _CACHE_CUTOFFS.get(match_level,
                                _CACHE_CUTOFFS[DEFAULT_MATCH_LEVEL])
    from .artfinder import strict_system
    if cutoff is not None and strict_system(system):
        cutoff = None       # hack/translation wheels: exact reuse only
        log(f"[{system}] hack-style wheel — cache reuse restricted to "
            f"EXACT descriptions (fuzzy reuse would borrow the base "
            f"game's metadata)")
    cache_keys = list(cache.keys()) if cutoff is not None else []
    stripped = {}
    for c in cache_keys:
        stripped.setdefault(_base_key(c), c)
    cached_hits = fuzzy_hits = skipped_failed = 0
    groups = {}          # cache_key -> [games]  (clones share one answer)
    for g in missing:
        key = _cache_key(g)
        hit = cache.get(key)
        if hit and hit.get("year") and hit.get("manufacturer") and hit.get("genre"):
            updates[g.name] = {"year": hit["year"],
                               "manufacturer": hit["manufacturer"],
                               "genre": hit["genre"]}
            cached_hits += 1
            continue
        if hit and hit.get("status") == "failed" and not retry_failed:
            skipped_failed += 1
            continue
        if not hit and cache_keys:
            fz = _fuzzy_cache_hit(cache, cache_keys, stripped, key, cutoff)
            if fz:
                updates[g.name] = {"year": fz["year"],
                                   "manufacturer": fz["manufacturer"],
                                   "genre": fz["genre"]}
                fuzzy_hits += 1
                continue
        groups.setdefault(key, []).append(g)
    to_ask = [gs[0] for gs in groups.values()]   # one representative per clone group
    dup_saved = sum(len(gs) - 1 for gs in groups.values())
    if cached_hits or fuzzy_hits:
        log(f"[{system}] {cached_hits + fuzzy_hits} game(s) filled FREE "
            f"from the enrichment cache"
            + (f" ({fuzzy_hits} via close-description reuse, "
               f"level: {match_level})" if fuzzy_hits else ""))
    if skipped_failed:
        log(f"[{system}] {skipped_failed} game(s) skipped — previously "
            f"unidentifiable even with web search (tick 'Retry games "
            f"that previously failed' to search them again)")
    if dup_saved:
        log(f"[{system}] {dup_saved} clone/duplicate game(s) share a "
            f"batch entry — answered once, applied to all")
    client = _claude(cfg) if to_ask else None
    # retrieval pool for RAG grounding: complete entries from this system
    complete = [g for g in games if g.year and g.manufacturer and g.genre]
    examples = complete[:8]
    if progress:
        progress(0.0, f"{system}: {len(to_ask)} game(s) to enrich")
    for i in range(0, len(to_ask), BATCH):
        if stop_flag():
            raise StopRequested()
        part = to_ask[i:i + BATCH]
        if progress:
            progress(i / max(1, len(to_ask)),
                     f"{system}: asking AI — batch {i // BATCH + 1}/"
                     f"{(len(to_ask) + BATCH - 1) // BATCH}")
        try:
            updates.update(_ask_batch(client, cfg, system, part, examples,
                                      log=log))
        except anthropic.APIError as e:
            log(f"[{system}] batch {i // BATCH + 1}: API error {e.__class__.__name__} — continuing")
        log(f"[{system}] enriched {min(i + BATCH, len(to_ask))}/{len(to_ask)}")
    # -- second pass: web-search the games the model could not identify
    # from its own knowledge (typically recent 2024+ releases past the
    # model's training data). Opt-out via the Systems tab checkbox.
    def _unresolved():
        return [g for g in to_ask
                if not all((getattr(g, f) or updates.get(g.name, {}).get(f))
                           for f in ("year", "manufacturer", "genre"))]
    web_searched = set()
    if web_search:
        unresolved = _unresolved()
        if unresolved:
            log(f"[{system}] {len(unresolved)} game(s) unidentified from "
                f"model knowledge — retrying with WEB SEARCH "
                f"(≈$0.01 per search, cost shown per batch)")
            for i in range(0, len(unresolved), SEARCH_BATCH):
                if stop_flag():
                    raise StopRequested()
                part = unresolved[i:i + SEARCH_BATCH]
                web_searched.update(g.name for g in part)
                if progress:
                    progress(0.5 + 0.4 * i / max(1, len(unresolved)),
                             f"{system}: web-searching — batch "
                             f"{i // SEARCH_BATCH + 1}/"
                             f"{(len(unresolved) + SEARCH_BATCH - 1) // SEARCH_BATCH}")
                try:
                    updates.update(_ask_batch(client, cfg, system, part,
                                              examples, log=log,
                                              web_search=True))
                except anthropic.APIError as e:
                    log(f"[{system}] web-search batch: API error "
                        f"{e.__class__.__name__} — continuing")
                log(f"[{system}] web-searched "
                    f"{min(i + SEARCH_BATCH, len(unresolved))}/{len(unresolved)}")
    # ---- propagate representative answers to their clone group + cache --
    stamp = time.strftime("%Y-%m-%d")
    for key, gs in groups.items():
        rep = gs[0]
        r = updates.get(rep.name)
        if r and r.get("year") and r.get("manufacturer") and r.get("genre"):
            cache[key] = {"year": r["year"], "manufacturer": r["manufacturer"],
                          "genre": r["genre"], "ts": stamp}
            for g in gs[1:]:
                updates.setdefault(g.name, dict(r))
        elif rep.name in web_searched:
            # web search itself came up empty: remember, don't re-bill
            cache[key] = {"status": "failed", "ts": stamp}
    _save_cache(cache)
    if progress:
        progress(0.9, f"{system}: writing XML + indexing")
    changed = 0
    if updates:
        changed = hdb.apply_updates(xml_path, updates,
                                    only_fill_empty=only_fill_empty)
        log(f"[{system}] XML updated: {changed} game(s) (backup taken)")
    # refresh in-memory games with the new values before indexing
    games = hdb.parse_games(hdb.read_db_text(xml_path)[0])
    # honest end-of-run count: "enriched 136/136" only tracks progress,
    # it must never read as success when fields stayed empty
    still = sum(1 for g in games if not (g.year and g.manufacturer and g.genre))
    if missing and still:
        log(f"[{system}] {still} game(s) STILL missing metadata — the "
            f"model could not identify them (or their batches failed; "
            f"see warnings above). Re-running only retries those games.")
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
        try:
            es_ok = _index_games(cfg, system, games)
            log(f"[{system}] indexed {len(games)} games "
                f"({'vector+elasticsearch' if es_ok else 'vector only — ES offline'})")
        except IndexingUnavailable as e:
            log(f"[{system}] WARNING: search indexing unavailable "
                f"({e}) - the XML metadata update itself SUCCEEDED; "
                f"Ask AI/search will use keyword matching only")
    return {"system": system, "games": len(games), "updated": changed,
            "skipped": False}
