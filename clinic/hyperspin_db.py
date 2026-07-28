# HyperSpin database XML access.
#   <root>\Databases\Main\Main.xml            -> the list of systems
#   <root>\Databases\<System>\<System>.xml    -> the games of one system
# HyperSpin XMLs are frequently sloppy (unescaped '&', stray attributes),
# so games are READ tolerantly (regex per <game> block) and WRITTEN
# surgically (per-tag regex edits on the original text - never a full
# XML-parser rewrite, which would reformat or crash). A timestamped backup
# is taken before the first write to each file.
import html as _html
import os
import re
import shutil
import time
from dataclasses import dataclass, field


def read_db_text(path: str):
    """Read a HyperSpin XML preserving its real encoding. Community DBs
    come in UTF-8, cp1252 AND UTF-16 (HyperHQ/front-end exports); decoding
    the wrong way either mangles accents permanently or (UTF-16 read as
    UTF-8) yields NUL-riddled text no regex can match. Returns
    (text, encoding) so writes can round-trip losslessly."""
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if b"\x00" in raw[:200]:
        return raw.decode("utf-16-le", errors="replace"), "utf-16-le"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def write_db_text(path: str, text: str, encoding: str):
    """Atomic write in the file's original encoding (tmp + os.replace so a
    crash can never truncate the database)."""
    tmp = path + ".clinic.tmp"
    with open(tmp, "w", encoding=encoding, errors="strict", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def databases_dir(hyperspin_root: str) -> str:
    """Databases folder for a saved root (accepts root or Media folder)."""
    for base in (hyperspin_root, os.path.dirname(hyperspin_root.rstrip("\\/"))):
        p = os.path.join(base, "Databases")
        if os.path.isdir(p):
            return p
    return ""


_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}


def _safe_system(name: str) -> bool:
    """System names become path components everywhere (Databases\\<Sys>,
    Media\\<Sys>, suite -BaseDir args, revert restore targets). A tampered
    Main.xml must not be able to escape the HyperSpin folder (OWASP path
    traversal) or hit reserved Windows device names. Callers strip
    whitespace BEFORE validating (sloppy real-world Main.xml entries have
    trailing spaces - those are still legitimate systems)."""
    if not name:
        return False
    if name != os.path.basename(name):
        return False
    if ("/" in name) or ("\\" in name) or (".." in name) or (":" in name):
        return False
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        return False
    return True


def main_xml_path(hyperspin_root: str) -> str:
    """Real HyperSpin keeps the system list in Databases\\Main Menu\\
    Main Menu.xml; older/simplified layouts use Databases\\Main\\Main.xml.
    The first one that exists wins."""
    dbs = databases_dir(hyperspin_root)
    if not dbs:
        return ""
    for sub, fname in (("Main Menu", "Main Menu.xml"), ("Main", "Main.xml")):
        p = os.path.join(dbs, sub, fname)
        if os.path.isfile(p):
            return p
    return os.path.join(dbs, "Main Menu", "Main Menu.xml")


def list_systems(hyperspin_root: str) -> list:
    """System names from Databases\\Main\\Main.xml - the SINGLE source of
    truth (user rule): only systems listed there are worked on, on every
    tab. No folder-scan fallback; names that are not a single safe path
    component are dropped. Encoding-tolerant (UTF-8/UTF-16/cp1252) and
    attribute-order-tolerant (name= anywhere inside the <game> tag)."""
    main = main_xml_path(hyperspin_root)
    if not main or not os.path.isfile(main):
        return []
    try:
        text = read_db_text(main)[0]
        systems = re.findall(r'<game\b[^>]*?\bname\s*=\s*"([^"]+)"', text)
    except Exception:
        return []
    out = []
    for s in systems:
        s = _html.unescape(s.strip())
        if _safe_system(s) and s not in out:
            out.append(s)
    return out


def system_xml_path(hyperspin_root: str, system: str) -> str:
    return os.path.join(databases_dir(hyperspin_root), system, system + ".xml")


@dataclass
class Game:
    name: str                  # rom name (the identity key)
    description: str = ""
    year: str = ""
    manufacturer: str = ""
    genre: str = ""
    cloneof: str = ""             # parent rom (MAME clones share its video)
    block_span: tuple = field(default=(0, 0))   # char range of the block


_TAG = r"<{t}>\s*(.*?)\s*</{t}>"


def parse_games(xml_text: str) -> list:
    # names/descriptions are UNESCAPED here (&apos; -> ', &amp; -> &):
    # files on disk use the real characters, and comparing escaped XML
    # names against them wrongly flagged art as missing/extra (user
    # report: 42 Dreamcast games' art deleted as 'not in the XML').
    # apply_updates() unescapes its raw-attribute lookups to match.
    games = []
    for m in re.finditer(r"<game\b[^>]*/>|<game\b[^>]*>.*?</game>", xml_text, re.S):
        block = m.group(0)
        nm = re.search(r'name\s*=\s*"([^"]*)"', block)
        if not nm:
            continue
        def tag(t):
            mm = re.search(_TAG.format(t=t), block, re.S | re.I)
            return _html.unescape(mm.group(1).strip()) if mm else ""
        games.append(Game(
            name=_html.unescape(nm.group(1)),
            description=tag("description"),
            year=tag("year"),
            manufacturer=tag("manufacturer"),
            genre=tag("genre"),
            cloneof=tag("cloneof"),
            block_span=m.span(),
        ))
    return games


def _set_tag(block: str, tag: str, value: str) -> str:
    """Replace the tag's value, or insert the tag after <description>
    (or at block start) when absent. Values are XML-escaped minimally."""
    esc = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    m = re.search(_TAG.format(t=tag), block, re.S | re.I)
    if m:
        return block[:m.start(1)] + esc + block[m.end(1):]
    line = f"<{tag}>{esc}</{tag}>"
    dm = re.search(r"</description>", block, re.I)
    if dm:
        # keep the file's indentation style: reuse the description line's
        indent = ""
        im = re.search(r"(\n[ \t]*)<description>", block)
        if im:
            indent = im.group(1)
        return block[:dm.end()] + indent + line + block[dm.end():]
    if block.rstrip().endswith("/>"):
        # self-closing game tag -> expand it
        head = block.rstrip()[:-2].rstrip()
        return head + f">\n\t\t{line}\n\t</game>"
    gm = re.search(r"<game\b[^>]*>", block)
    return block[:gm.end()] + f"\n\t\t{line}" + block[gm.end():]


def apply_updates(xml_path: str, updates: dict, only_fill_empty=True,
                  backup_dir=None) -> int:
    """updates: {rom_name: {"year":..,"manufacturer":..,"genre":..}}.
    Returns number of games changed. Backs up the file first."""
    text, enc = read_db_text(xml_path)
    changed = 0
    out = []
    pos = 0
    for m in re.finditer(r"<game\b[^>]*/>|<game\b[^>]*>.*?</game>", text, re.S):
        block = m.group(0)
        nm = re.search(r'name\s*=\s*"([^"]*)"', block)
        # updates keys are UNESCAPED (parse_games) - unescape the raw attr
        upd = updates.get(_html.unescape(nm.group(1))) if nm else None
        if upd:
            new_block = block
            for tag_name in ("genre", "manufacturer", "year"):
                val = str(upd.get(tag_name, "") or "").strip()
                if not val:
                    continue
                cur = re.search(_TAG.format(t=tag_name), new_block, re.S | re.I)
                cur_val = cur.group(1).strip() if cur else ""
                if only_fill_empty and cur_val:
                    continue
                new_block = _set_tag(new_block, tag_name, val)
            if new_block != block:
                changed += 1
                block = new_block
        out.append(text[pos:m.start()])
        out.append(block)
        pos = m.end()
    out.append(text[pos:])
    if changed:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bdir = backup_dir or os.path.join(os.path.dirname(xml_path), "clinic_backups")
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(xml_path, os.path.join(
            bdir, os.path.basename(xml_path) + "." + stamp + ".bak"))
        write_db_text(xml_path, "".join(out), enc)
    return changed
