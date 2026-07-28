# EmuMovies access, per the media-pipeline knowledge base:
#   - plain FTP at files.emumovies.com (documented: "ftp, not sftp")
#   - videos:  Official/Video Snaps (HQ|SQ|HD)/<System> (Video Snaps)(...)/<Game>.mp4
#   - wheels:  Official/Artwork/<System>/<Logos pack>.zip  (small, pull whole)
#   - naming is No-Intro-ish/US-centric -> fuzzy matching lives in artfinder
import difflib
import ftplib
import io
import json
import os
import re
import time
import zipfile

HOST = "files.emumovies.com"
# quality priority is HIGHEST first (user rule): HD, then HQ, then SQ
VIDEO_ROOTS = [
    "Official/Video Snaps (HD)",
    "Official/Video Snaps (HQ)",
    "Official/Video Snaps (SQ)",
]
ARTWORK_ROOT = "Official/Artwork"


# ---------- system-name -> folder resolution (user-reported: Dreamcast
# found nothing although "Sega Dreamcast (Video Snaps)(HQ)" exists - the
# vendor prefix broke both the prefix and the fuzzy match) ----------
def _nrm(s):
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


def _toks(s):
    return set(_nrm(s).split())


def resolve_name(system, names):
    """Match a HyperSpin system name to an EmuMovies folder name.
    Token-based so vendor prefixes don't matter (Dreamcast <-> Sega
    Dreamcast, MAME <-> MAME Arcade) and NES can never match SNES."""
    base = {n: n.split(" (")[0] for n in names}
    st = _toks(system)
    if not st:
        return None
    for n, b in base.items():                      # exact base name
        if _nrm(b) == _nrm(system):
            return n
    best, extra = None, 99                          # folder ⊇ system tokens
    for n, b in base.items():
        bt = _toks(b)
        if st <= bt and len(bt - st) < extra:
            best, extra = n, len(bt - st)
    if best:
        return best
    for n, b in base.items():                       # system ⊇ folder tokens
        bt = _toks(b)
        if bt and bt <= st:
            return n
    close = difflib.get_close_matches(
        _nrm(system), [_nrm(b) for b in base.values()], n=1, cutoff=0.8)
    if close:
        for n, b in base.items():
            if _nrm(b) == close[0]:
                return n
    return None


# ---------- trainable folder map + full-tree cache ----------
# data\emumovies_map.json remembers every resolved system -> folder pair
# (and can be hand-edited to "train" the software); the full FTP tree is
# saved to data\emumovies_tree.json for inspection and DB indexing.
def _map_path():
    from . import config
    return os.path.join(config.DATA_DIR, "emumovies_map.json")


def load_map() -> dict:
    try:
        with open(_map_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_map(m: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_map_path()), exist_ok=True)
        with open(_map_path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
        os.replace(_map_path() + ".tmp", _map_path())
    except Exception:
        pass


class _ReuseTLS(ftplib.FTP_TLS):
    """FTP_TLS with TLS session REUSE on data connections - many FTPS
    servers kill data channels that open a fresh TLS session, which
    surfaces as 'EOF occurred in violation of protocol' (user report)."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                conn = self.context.wrap_socket(
                    conn, server_hostname=self.host,
                    session=self.sock.session)
            except Exception:
                conn = self.context.wrap_socket(
                    conn, server_hostname=self.host)
        return conn, size


class EmuMovies:
    def __init__(self, user, password, log=print):
        self.log = log
        self._user, self._pass = user, password
        self._dir_cache = {}
        self._connect(announce=True)

    def _connect(self, announce=False):
        # opportunistic TLS (OWASP transport security): try FTPS with
        # protected data channel first; fall back to plain FTP only when
        # the server refuses, and say so
        # latin-1: EmuMovies has legacy cp1252 filenames (0x86 etc.) that
        # crash ftplib's default utf-8 listing decode; latin-1 is a
        # lossless byte mapping so names round-trip for RETR too
        try:
            ftps = _ReuseTLS(HOST, timeout=30, encoding="latin-1")
            ftps.login(self._user, self._pass)
            ftps.prot_p()
            self.ftp = ftps
            if announce:
                self.log("  EmuMovies: connected over FTPS (encrypted)")
        except Exception:
            self.ftp = ftplib.FTP(HOST, timeout=30, encoding="latin-1")
            self.ftp.login(self._user, self._pass)
            if announce:
                self.log("  EmuMovies: connected over plain FTP (server "
                         "does not offer TLS - credentials/content are "
                         "unencrypted)")

    def _retry(self, fn):
        """Run an FTP operation; on a dropped/foul connection (SSL EOF,
        reset, timeout) reconnect once and run it again - a dying
        session must never kill a whole system's art pass."""
        try:
            return fn()
        except ftplib.error_perm:
            raise
        except Exception as e:
            self.log(f"    EmuMovies connection hiccup "
                     f"({e or type(e).__name__}) — reconnecting…")
            try:
                self.ftp.close()
            except Exception:
                pass
            self._connect()
            return fn()

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            pass

    def listdir(self, path):
        if path in self._dir_cache:
            return self._dir_cache[path]

        def _do():
            try:
                names = self.ftp.nlst(path)
            except ftplib.error_perm:
                names = []
            return [n.rsplit("/", 1)[-1] for n in names]
        out = self._retry(_do)
        self._dir_cache[path] = out
        return out

    # ---------- videos ----------
    def find_video_dirs(self, system):
        """ALL matching snap folders, highest quality first (HD sets are
        often WIP/incomplete - a game missing there must fall through to
        HQ, then SQ)."""
        out = []
        for root in VIDEO_ROOTS:
            entries = self.listdir(root)
            hit = resolve_name(system, entries) if entries else None
            if hit:
                out.append(f"{root}/{hit}")
        return out

    def find_video_dir(self, system):
        """Locate the system's video-snap folder (HQ preferred). The
        trained map wins; otherwise vendor-tolerant token resolution
        (Dreamcast -> 'Sega Dreamcast (Video Snaps)(HQ)'), remembered in
        the map for next time."""
        m = load_map()
        key = f"video::{system}"
        if m.get(key):
            root, _, folder = m[key].rpartition("/")
            if folder in self.listdir(root):
                return m[key]
        for root in VIDEO_ROOTS:
            entries = self.listdir(root)
            if not entries:
                continue
            hit = resolve_name(system, entries)
            if hit:
                m[key] = f"{root}/{hit}"
                save_map(m)
                return m[key]
        return None

    def list_videos(self, video_dir):
        return [f for f in self.listdir(video_dir)
                if f.lower().endswith((".mp4", ".avi", ".flv"))]

    def download(self, remote_path, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        tmp = local_path + ".part"

        def _do():
            with open(tmp, "wb") as f:
                self.ftp.retrbinary(f"RETR {remote_path}", f.write)
        try:
            self._retry(_do)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, local_path)
        return os.path.getsize(local_path)

    # ---------- wheels (logo packs) ----------
    def find_logo_pack(self, system):
        m = load_map()
        key = f"logopack::{system}"
        if m.get(key):
            return m[key]
        entries = self.listdir(ARTWORK_ROOT)
        hit = resolve_name(system, entries)
        if not hit:
            return None
        sysdir = f"{ARTWORK_ROOT}/{hit}"
        packs = [f for f in self.listdir(sysdir)
                 if "logo" in f.lower() and f.lower().endswith((".zip",))]
        if not packs:
            return None
        m[key] = f"{sysdir}/{packs[0]}"
        save_map(m)
        return m[key]

    def fetch_logo_pack(self, pack_path, cache_dir):
        """Download (once) and open the logos zip; returns ZipFile or None.
        Packs are documented as small (~30-50MB) - fine to pull whole."""
        os.makedirs(cache_dir, exist_ok=True)
        local = os.path.join(cache_dir, os.path.basename(pack_path))
        if not os.path.isfile(local):
            self.log(f"  downloading logo pack {os.path.basename(pack_path)}…")
            self.download(pack_path, local)
        try:
            return zipfile.ZipFile(local)
        except zipfile.BadZipFile:
            self.log("  logo pack is not a readable zip (rar?) — skipping")
            return None
