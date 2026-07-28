# LaunchBox GamesDB clear-logo source — ported from the Wheels2Add /
# media-pipeline knowledge base (MEDIA_PIPELINE_NOTES.md + sfamicom_art.py):
#   search  https://gamesdb.launchbox-app.com/games/results/<query>
#   gallery https://gamesdb.launchbox-app.com/games/images/<id>-<slug>
#   CDN     https://images.launchbox-app.com/<guid>.png
# No API key — only a browser User-Agent. Known pitfalls (from that
# project): the CDN occasionally serves stale-404/corrupt files that pass
# magic-byte checks, so every candidate gets a REAL PIL decode plus a
# transparency test (a true clear logo is RGBA with a transparent
# background; PSN tiles/banners are not), and the gallery's other URLs
# are kept as fallbacks.
import html as _html
import io
import re
import urllib.parse
import urllib.request

from PIL import Image

REGION_PREF = ["North America", "United States", "USA", "World", "Europe", "Japan"]

_ART = "The|A|An|Le|La|Les|Der|Die|Das"


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def clean_title(name: str) -> str:
    """Strip region tags and fold 'X, The' articles — the exact cleanup
    the knowledge base validated across four systems. MAME-style dual
    names ('Demon Front / Moyu Zhanxian') keep the primary title."""
    s = re.sub(r"\([^)]*\)", "", _html.unescape(name))
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = s.strip(" -")
    if " / " in s:
        s = s.split(" / ")[0]
    m = re.match(rf"^(.*),\s*({_ART})\s+-\s+(.*)$", s)
    if m:
        s = f"{m.group(2)} {m.group(1)} - {m.group(3)}"
    else:
        m = re.match(rf"^(.*),\s*({_ART})$", s)
        if m:
            s = f"{m.group(2)} {m.group(1)}"
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _html.unescape(s).lower())


def _cards(query: str):
    try:
        page = _get("https://gamesdb.launchbox-app.com/games/results/"
                    + urllib.parse.quote(query)).decode("utf-8", "ignore")
    except Exception:
        return []
    return re.findall(
        r'/games/details/(\d+)-([a-z0-9\-]+)".*?<h3[^>]*>([^<]+)</h3>\s*<p[^>]*>([^<]+)</p>',
        page, re.S)


def _platform_hint(system: str) -> str:
    s = system.lower()
    if "mame" in s or "arcade" in s or "final burn" in s or "fbneo" in s:
        return "arcade"
    return s


def find_game(title: str, system: str):
    """(id, slug) via the knowledge base's query/platform ladder:
    platform-exact title, platform contains, any-platform exact, first."""
    hint = _platform_hint(system)
    queries = [title]
    if " - " in title:
        queries += [title.replace(" - ", ": "), title.split(" - ")[0],
                    title.replace(" - ", " ")]
    nt = _norm(title)
    fallback = None
    for q in queries:
        cards = _cards(q)
        if not cards:
            continue
        onplat = [c for c in cards if hint in c[3].lower()]
        for gid, slug, t, _plat in onplat:
            if _norm(t) == nt:
                return gid, slug
        for gid, slug, t, _plat in onplat:
            if nt in _norm(t) or _norm(t) in nt:
                return gid, slug
        if onplat and fallback is None:
            fallback = (onplat[0][0], onplat[0][1])
        for gid, slug, t, _plat in cards:
            if _norm(t) == nt:
                return gid, slug
        if fallback is None:
            fallback = (cards[0][0], cards[0][1])
    return fallback


def logo_urls(gid: str, slug: str):
    """All Clear Logo image URLs from the gallery, best region first."""
    page = _get(f"https://gamesdb.launchbox-app.com/games/images/{gid}-{slug}"
                ).decode("utf-8", "ignore")
    trips = re.findall(
        r'href="(https://images\.launchbox-app\.com/[^"]+\.(?:png|jpg))"[^>]*'
        r'data-title="[^"]*? - (Clear Logo) Image(?: \(([^)]*)\))?"', page)
    urls = [(u, rg or "") for (u, _ty, rg) in trips]
    ordered = []
    for pref in REGION_PREF:
        for u, rg in urls:
            if pref.lower() in rg.lower() and u not in ordered:
                ordered.append(u)
    for u, _rg in urls:
        if u not in ordered:
            ordered.append(u)
    return ordered


def _usable_logo(data: bytes):
    """Real decode + clear-logo test; returns normalized PNG bytes."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = im.convert("RGBA")
    except Exception:
        return None
    alpha = im.getchannel("A")
    lo = sum(1 for a in alpha.getdata() if a < 16)
    if lo < 0.15 * im.width * im.height:
        return None            # opaque tile/banner, not a clear logo
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def fetch_clear_logo(name: str, system: str, log=None):
    """PNG bytes of a transparent clear logo for the game, or None."""
    title = clean_title(name)
    try:
        pg = find_game(title, system)
    except Exception as e:
        if log:
            log(f"    launchbox search error: {e}")
        return None
    if not pg:
        return None
    try:
        urls = logo_urls(*pg)
    except Exception as e:
        if log:
            log(f"    launchbox gallery error: {e}")
        return None
    for u in urls[:4]:
        try:
            data = _usable_logo(_get(u))
        except Exception:
            continue
        if data:
            return data
    return None
