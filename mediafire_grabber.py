# MediaFire Grabber - tiny standalone UI (user spec): one line for the
# folder URL, one line for the destination, a Download button. Walks the
# complete MediaFire folder tree (folder/get_content API, chunked) and
# downloads every file one by one, recreating the folder structure.
# v1.1 (user rule): "List folders" shows the link's structure with a
# CHECKBOX on every main folder (all checked by default, plus a "files
# in the folder root" row when present) - Download then grabs only the
# checked ones. Download without listing first still grabs everything.
#
# URL forms accepted:
#   https://www.mediafire.com/folder/<key>/<Name>
#   https://www.mediafire.com/folder/<key>/<Name>#<subfolder key>
#   a bare folder key
# The #fragment (the subfolder open in the browser) wins when present -
# that is the folder the user is actually looking at.
import base64
import json
import os
import queue
import re
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

API = "https://www.mediafire.com/api/1.4/folder/get_content.php"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def parse_key(url: str) -> str:
    url = (url or "").strip()
    m = re.search(r"#(\w+)\s*$", url)
    if m:
        return m.group(1)
    m = re.search(r"/folder/(\w+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"\w{8,}", url):
        return url
    raise ValueError("that does not look like a MediaFire folder link")


def _get(url: str, timeout=60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def api_content(key: str, kind: str):
    """All files or folders of one folder (chunked API, no key needed)."""
    out, chunk = [], 1
    while True:
        q = urllib.parse.urlencode({
            "folder_key": key, "content_type": kind, "filter": "all",
            "order_by": "name", "order_direction": "asc",
            "chunk": chunk, "response_format": "json"})
        data = json.loads(_get(f"{API}?{q}").decode("utf-8", "ignore"))
        fc = data.get("response", {}).get("folder_content", {})
        out += fc.get(kind, [])
        if str(fc.get("more_chunks", "no")).lower() != "yes":
            return out
        chunk += 1


def folder_name(key: str) -> str:
    q = urllib.parse.urlencode({"folder_key": key, "response_format": "json"})
    data = json.loads(_get(
        "https://www.mediafire.com/api/1.4/folder/get_info.php?" + q
    ).decode("utf-8", "ignore"))
    return data.get("response", {}).get("folder_info", {}).get("name", key)


def walk(key: str, rel=""):
    """Yield (relative_dir, file_dict) for the whole tree, depth first."""
    for f in api_content(key, "files"):
        yield rel, f
    for sub in api_content(key, "folders"):
        name = SAFE.sub("_", sub.get("name", sub["folderkey"])).strip() or sub["folderkey"]
        yield from walk(sub["folderkey"], os.path.join(rel, name))


def direct_link(page_url: str) -> str:
    """Resolve the real download URL from a MediaFire file page. Newer
    pages hide it base64-scrambled in the download button; older ones
    link download<n>.mediafire.com directly."""
    html = _get(page_url).decode("utf-8", "ignore")
    m = re.search(r'data-scrambled-url="([^"]+)"', html)
    if m:
        return base64.b64decode(m.group(1)).decode("utf-8", "ignore")
    m = re.search(r'href="(https://download[^"]+)"', html)
    if m:
        return m.group(1)
    raise RuntimeError("no download link on the page (file removed?)")


def download(url: str, dst: str, tick):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, \
            open(dst + ".part", "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            buf = r.read(256 * 1024)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            tick(done, total)
    os.replace(dst + ".part", dst)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MediaFire Grabber")
        self.geometry("680x420")
        self.resizable(True, True)
        pad = {"padx": 12, "pady": 4}

        r1 = ttk.Frame(self); r1.pack(fill="x", **pad)
        ttk.Label(r1, text="Folder URL:", width=12).pack(side="left")
        self.var_url = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_url).pack(
            side="left", fill="x", expand=True)

        r2 = ttk.Frame(self); r2.pack(fill="x", **pad)
        ttk.Label(r2, text="Save to:", width=12).pack(side="left")
        self.var_dst = tk.StringVar(value=os.path.join(
            os.path.expanduser("~"), "Downloads"))
        ttk.Entry(r2, textvariable=self.var_dst).pack(
            side="left", fill="x", expand=True)
        ttk.Button(r2, text="Browse…", command=self._browse).pack(
            side="left", padx=(6, 0))

        r3 = ttk.Frame(self); r3.pack(fill="x", **pad)
        self.btn_list = ttk.Button(r3, text="🗂  List folders",
                                   command=self._list)
        self.btn_list.pack(side="left")
        self.btn = ttk.Button(r3, text="⬇  Download", command=self._start)
        self.btn.pack(side="left", padx=(8, 0))
        self.btn_stop = ttk.Button(r3, text="■ Stop", state="disabled",
                                   command=self._stop)
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(r3, text="All", width=5,
                   command=lambda: self._check_all(True)).pack(side="left", padx=(12, 0))
        ttk.Button(r3, text="None", width=6,
                   command=lambda: self._check_all(False)).pack(side="left", padx=(4, 0))
        self.lbl = ttk.Label(r3, text="")
        self.lbl.pack(side="left", padx=(12, 0))

        # folder-structure panel: one CHECKBOX per main folder (user rule)
        sf = ttk.LabelFrame(self, text="Folders in this link (check what to download)")
        sf.pack(fill="x", padx=12, pady=4)
        self.sel_canvas = tk.Canvas(sf, height=120, highlightthickness=0)
        ssb = ttk.Scrollbar(sf, orient="vertical",
                            command=self.sel_canvas.yview)
        self.sel_inner = ttk.Frame(self.sel_canvas)
        self.sel_inner.bind("<Configure>", lambda e: self.sel_canvas.configure(
            scrollregion=self.sel_canvas.bbox("all")))
        self.sel_canvas.create_window((0, 0), window=self.sel_inner, anchor="nw")
        self.sel_canvas.configure(yscrollcommand=ssb.set)
        self.sel_canvas.pack(side="left", fill="both", expand=True)
        ssb.pack(side="right", fill="y")
        self.sel_canvas.bind_all("<MouseWheel>", self._sel_wheel)
        ttk.Label(self.sel_inner, text="press “List folders” to load the "
                  "structure — or just Download to grab everything"
                  ).pack(anchor="w", padx=6, pady=4)
        self._listing = None      # (key, root_name, [(var, kind, key/name)])

        self.pb = ttk.Progressbar(self, maximum=100)
        self.pb.pack(fill="x", **pad)

        lf = ttk.Frame(self); lf.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(lf, height=10, state="disabled",
                           font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._q = queue.Queue()
        self._stop_flag = False
        self.after(100, self._pump)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.var_dst.get() or None)
        if d:
            self.var_dst.set(d)

    def _sel_wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w is self.sel_canvas or w is self.sel_inner:
                self.sel_canvas.yview_scroll(-1 if e.delta > 0 else 1,
                                             "units")
                return
            w = getattr(w, "master", None)

    def _check_all(self, value):
        if self._listing:
            for var, _kind, _ref, _label in self._listing[2]:
                var.set(value)

    def _list(self):
        try:
            key = parse_key(self.var_url.get())
        except ValueError as e:
            messagebox.showerror("MediaFire Grabber", str(e))
            return
        self.btn_list.configure(state="disabled")
        self.lbl.configure(text="listing folders…")

        def work():
            try:
                name = SAFE.sub("_", folder_name(key)).strip() or key
                subs = api_content(key, "folders")
                nroot = len(api_content(key, "files"))
                self._q.put(("listing", (key, name, subs, nroot)))
            except Exception as e:
                self._q.put(("log", f"listing failed: {e}"))
                self._q.put(("listed", None))
        threading.Thread(target=work, daemon=True).start()

    def _show_listing(self, key, name, subs, nroot):
        for w in self.sel_inner.winfo_children():
            w.destroy()
        rows = []
        if nroot:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.sel_inner, variable=var,
                            text=f"(files in the folder root)  —  {nroot} file(s)"
                            ).pack(anchor="w", padx=6)
            rows.append((var, "root", key, "(root files)"))
        for s in subs:
            var = tk.BooleanVar(value=True)
            fc, dc = s.get("file_count", "?"), s.get("folder_count", "0")
            extra = f", {dc} subfolder(s)" if str(dc) not in ("0", "") else ""
            ttk.Checkbutton(self.sel_inner, variable=var,
                            text=f"{s.get('name', s['folderkey'])}  —  "
                                 f"{fc} file(s){extra}"
                            ).pack(anchor="w", padx=6)
            rows.append((var, "folder", s["folderkey"],
                         SAFE.sub("_", s.get("name", s["folderkey"])).strip()
                         or s["folderkey"]))
        if not rows:
            ttk.Label(self.sel_inner, text="(this folder is empty)"
                      ).pack(anchor="w", padx=6, pady=4)
        self._listing = (key, name, rows)
        self._log(f"folder: {name} — {len(subs)} main folder(s)"
                  + (f", {nroot} file(s) in the root" if nroot else ""))

    def _log(self, msg):
        self._q.put(("log", msg))

    def _pump(self):
        try:
            while True:
                kind, v = self._q.get_nowait()
                if kind == "log":
                    self.txt.configure(state="normal")
                    self.txt.insert("end", v + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif kind == "pb":
                    self.pb.configure(value=v)
                elif kind == "status":
                    self.lbl.configure(text=v)
                elif kind == "listing":
                    self._show_listing(*v)
                    self.btn_list.configure(state="normal")
                    self.lbl.configure(text="")
                elif kind == "listed":
                    self.btn_list.configure(state="normal")
                    self.lbl.configure(text="")
                elif kind == "done":
                    self.btn.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    messagebox.showinfo("MediaFire Grabber", v)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _stop(self):
        self._stop_flag = True
        self._log("stopping after the current file…")

    def _start(self):
        try:
            key = parse_key(self.var_url.get())
        except ValueError as e:
            messagebox.showerror("MediaFire Grabber", str(e))
            return
        dst = self.var_dst.get().strip()
        if not dst:
            messagebox.showerror("MediaFire Grabber",
                                 "choose a destination folder first")
            return
        # selection applies when the listing matches the current URL
        # (user rule): only checked main folders are downloaded
        sel = None
        if self._listing and self._listing[0] == key:
            rows = [r for r in self._listing[2] if r[0].get()]
            if not rows:
                messagebox.showerror("MediaFire Grabber",
                                     "no folders are checked")
                return
            if len(rows) < len(self._listing[2]):
                sel = rows
        self._stop_flag = False
        self.btn.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self._work, args=(key, dst, sel),
                         daemon=True).start()

    def _work(self, key, dst, sel=None):
        try:
            name = (SAFE.sub("_", self._listing[1]).strip()
                    if self._listing and self._listing[0] == key
                    else SAFE.sub("_", folder_name(key)).strip()) or key
            self._log(f"folder: {name}"
                      + (f" — {len(sel)} main folder(s) selected" if sel else ""))
            self._q.put(("status", "listing the folder tree…"))
            if sel is None:
                items = list(walk(key))
            else:
                items = []
                for _var, kind, ref, label in sel:
                    if kind == "root":
                        items += [("", f) for f in api_content(key, "files")]
                    else:
                        items += list(walk(ref, label))
            total = len(items)
            self._log(f"{total} file(s) found in the tree")
            got = skipped = failed = 0
            for i, (rel, f) in enumerate(items):
                if self._stop_flag:
                    break
                fname = SAFE.sub("_", f.get("filename", f["quickkey"])).strip()
                folder = os.path.join(dst, name, rel)
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, fname)
                shown = os.path.join(rel, fname) if rel else fname
                if os.path.isfile(path) and \
                        os.path.getsize(path) == int(f.get("size") or -1):
                    skipped += 1
                    self._log(f"  = {shown} (already complete)")
                    continue
                self._q.put(("status", f"{i + 1}/{total}  {fname}"))
                try:
                    url = direct_link(f.get("links", {}).get(
                        "normal_download",
                        f"https://www.mediafire.com/file/{f['quickkey']}"))

                    def tick(done, size, base=i):
                        frac = (done / size) if size else 0
                        self._q.put(("pb", (base + frac) / total * 100))
                    download(url, path, tick)
                    got += 1
                    self._log(f"  + {shown} "
                              f"({os.path.getsize(path) // 1024} KB)")
                except Exception as e:
                    failed += 1
                    self._log(f"  ! {shown} FAILED: {e}")
                self._q.put(("pb", (i + 1) / total * 100))
            verdict = (f"{got} downloaded, {skipped} already complete, "
                       f"{failed} failed"
                       + (" — stopped by user" if self._stop_flag else ""))
            self._log("DONE: " + verdict)
            self._q.put(("status", ""))
            self._q.put(("done", verdict))
        except Exception as e:
            self._log(f"ERROR: {e}")
            self._q.put(("done", f"failed: {e}"))


if __name__ == "__main__":
    App().mainloop()
