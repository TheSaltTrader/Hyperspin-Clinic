# EmuMovies access, per the media-pipeline knowledge base:
#   - plain FTP at files.emumovies.com (documented: "ftp, not sftp")
#   - videos:  Official/Video Snaps (HQ|SQ|HD)/<System> (Video Snaps)(...)/<Game>.mp4
#   - wheels:  Official/Artwork/<System>/<Logos pack>.zip  (small, pull whole)
#   - naming is No-Intro-ish/US-centric -> fuzzy matching lives in artfinder
import difflib
import ftplib
import io
import os
import zipfile

HOST = "files.emumovies.com"
VIDEO_ROOTS = [
    "Official/Video Snaps (HQ)",
    "Official/Video Snaps (SQ)",
    "Official/Video Snaps (HD)",
]
ARTWORK_ROOT = "Official/Artwork"


class EmuMovies:
    def __init__(self, user, password, log=print):
        self.log = log
        # opportunistic TLS (OWASP transport security): try FTPS with
        # protected data channel first; fall back to plain FTP only when
        # the server refuses, and say so
        try:
            ftps = ftplib.FTP_TLS(HOST, timeout=30)
            ftps.login(user, password)
            ftps.prot_p()
            self.ftp = ftps
            log("  EmuMovies: connected over FTPS (encrypted)")
        except Exception:
            self.ftp = ftplib.FTP(HOST, timeout=30)
            self.ftp.login(user, password)
            log("  EmuMovies: connected over plain FTP (server does not "
                "offer TLS - credentials/content are unencrypted)")
        self._dir_cache = {}

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            pass

    def listdir(self, path):
        if path in self._dir_cache:
            return self._dir_cache[path]
        try:
            names = self.ftp.nlst(path)
        except ftplib.error_perm:
            names = []
        out = [n.rsplit("/", 1)[-1] for n in names]
        self._dir_cache[path] = out
        return out

    # ---------- videos ----------
    def find_video_dir(self, system):
        """Locate the system's video-snap folder (HQ preferred)."""
        for root in VIDEO_ROOTS:
            entries = self.listdir(root)
            if not entries:
                continue
            # exact-prefix first, then fuzzy against the folder names
            cands = [e for e in entries if e.lower().startswith(system.lower())]
            if not cands:
                cands = difflib.get_close_matches(
                    system, [e.split(" (")[0] for e in entries], n=1, cutoff=0.85)
                cands = [e for e in entries
                         if cands and e.split(" (")[0] == cands[0]]
            if cands:
                return f"{root}/{cands[0]}"
        return None

    def list_videos(self, video_dir):
        return [f for f in self.listdir(video_dir)
                if f.lower().endswith((".mp4", ".avi", ".flv"))]

    def download(self, remote_path, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        tmp = local_path + ".part"
        try:
            with open(tmp, "wb") as f:
                self.ftp.retrbinary(f"RETR {remote_path}", f.write)
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
        entries = self.listdir(ARTWORK_ROOT)
        cands = [e for e in entries if e.lower().startswith(system.lower())]
        if not cands:
            close = difflib.get_close_matches(system, entries, n=1, cutoff=0.85)
            cands = close
        if not cands:
            return None
        sysdir = f"{ARTWORK_ROOT}/{cands[0]}"
        packs = [f for f in self.listdir(sysdir)
                 if "logo" in f.lower() and f.lower().endswith((".zip",))]
        if not packs:
            return None
        return f"{sysdir}/{packs[0]}"

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
