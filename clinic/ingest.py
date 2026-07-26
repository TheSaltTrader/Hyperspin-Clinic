# Corpus walker + chunker.
# Two source kinds:
#   - knowledge documents (.md / .txt): split on headings then hard-wrap
#     long sections into overlapping chunks
#   - theme zips: one chunk per theme built from Theme.xml + Info.txt
#     (geometry, classification markers like "Converted to 16:9 by ...")
import os
import re
import zipfile
from dataclasses import dataclass, field


@dataclass
class Chunk:
    id: str
    text: str
    source: str            # file path the chunk came from
    kind: str              # "doc" | "theme"
    meta: dict = field(default_factory=dict)


MAX_CHARS = 1800
OVERLAP = 200


def _split_doc(text: str, source: str) -> list:
    # split on markdown headings, keep heading with its body
    parts = re.split(r"(?m)^(#{1,3} .*)$", text)
    sections = []
    cur_head = ""
    for p in parts:
        if re.match(r"^#{1,3} ", p or ""):
            cur_head = p.strip()
        elif p and p.strip():
            sections.append((cur_head, p.strip()))
    chunks = []
    n = 0
    for head, body in sections or [("", text)]:
        blob = (head + "\n" + body).strip()
        start = 0
        while start < len(blob):
            piece = blob[start:start + MAX_CHARS]
            chunks.append(Chunk(
                id=f"{os.path.basename(source)}:{n}",
                text=piece,
                source=source,
                kind="doc",
                meta={"heading": head[:120]},
            ))
            n += 1
            if start + MAX_CHARS >= len(blob):
                break
            start += MAX_CHARS - OVERLAP
    return chunks


def _theme_chunk(zip_path: str) -> "Chunk | None":
    game = os.path.splitext(os.path.basename(zip_path))[0]
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            xml = info = ""
            for nm in names:
                low = nm.lower()
                if low.endswith("theme.xml"):
                    xml = z.read(nm).decode("latin-1", "replace")
                elif low == "info.txt":
                    info = z.read(nm).decode("utf-8", "replace")
            arts = [nm for nm in names if not nm.endswith("/")]
    except Exception as e:
        return Chunk(
            id=f"theme:{game}",
            text=f"Theme {game}: UNREADABLE ZIP ({e})",
            source=zip_path, kind="theme", meta={"game": game, "error": str(e)},
        )
    swfs = [a for a in arts if a.lower().endswith(".swf")]
    imgs = [a for a in arts if re.search(r"\.(png|jpe?g|gif|bmp)$", a, re.I)]
    text = (
        f"Theme {game} ({os.path.basename(zip_path)})\n"
        f"Files: {', '.join(sorted(arts))}\n"
        f"SWF art count: {len(swfs)}; raster art count: {len(imgs)}\n"
        f"Info.txt: {info.strip()[:500]}\n"
        f"Theme.xml: {xml.strip()[:900]}"
    )
    return Chunk(
        id=f"theme:{game}",
        text=text,
        source=zip_path,
        kind="theme",
        meta={
            "game": game,
            "swf_count": len(swfs),
            "raster_count": len(imgs),
            "is_16x9_marked": bool(re.search(r"16\s*[:x]\s*9", info, re.I)),
        },
    )


def walk(doc_sources, theme_sources, max_theme_zips=0, progress=None):
    """Yield Chunk objects from all configured sources.
    progress: optional callable(msg) for UI feedback."""
    for src in doc_sources or []:
        if os.path.isfile(src):
            try:
                text = open(src, encoding="utf-8", errors="replace").read()
            except Exception as e:
                if progress:
                    progress(f"skip {src}: {e}")
                continue
            for c in _split_doc(text, src):
                yield c
            if progress:
                progress(f"doc: {os.path.basename(src)}")
        elif os.path.isdir(src):
            for root, _dirs, files in os.walk(src):
                for f in files:
                    if f.lower().endswith((".md", ".txt")):
                        p = os.path.join(root, f)
                        try:
                            text = open(p, encoding="utf-8", errors="replace").read()
                        except Exception:
                            continue
                        for c in _split_doc(text, p):
                            yield c
            if progress:
                progress(f"doc dir: {src}")
    count = 0
    for src in theme_sources or []:
        if not os.path.isdir(src):
            continue
        for f in sorted(os.listdir(src)):
            if not f.lower().endswith(".zip"):
                continue
            if max_theme_zips and count >= max_theme_zips:
                return
            c = _theme_chunk(os.path.join(src, f))
            if c:
                yield c
                count += 1
                if progress and count % 250 == 0:
                    progress(f"themes indexed: {count}")
