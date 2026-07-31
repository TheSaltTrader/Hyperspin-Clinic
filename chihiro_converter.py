# Chihiro Converter - tiny standalone UI (user spec): point at a folder
# of MAME Sega Chihiro dumps (game.zip with the security key + the
# game's GD-ROM .chd, zipped or CHD) and convert every game to a
# netboot .bin into a subfolder. The conversion engine is
# chihiro_netboot.py (MIT, github.com/Tovarichtch/chihiro-netboot,
# based on JayFoxRox's Chihiro-Tools) driven through its public
# find_games()/convert_game() functions; chdman.exe (MAME) must sit
# next to this app - exactly like the upstream tool.
import io
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__)))
import chihiro_netboot as cn


class QueueWriter(io.TextIOBase):
    """Captures the engine's prints; \r progress lines become status
    updates instead of log spam."""

    def __init__(self, q):
        self.q = q
        self.buf = ""

    def write(self, s):
        self.buf += s
        while True:
            cut = -1
            for sep in ("\n", "\r"):
                i = self.buf.find(sep)
                if i != -1 and (cut == -1 or i < cut):
                    cut = i
            if cut == -1:
                return len(s)
            line, self.buf = self.buf[:cut], self.buf[cut + 1:]
            if line.strip():
                self.q.put(("log", line.rstrip()))

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Chihiro Converter — MAME dumps ➜ netboot .bin")
        self.geometry("720x460")
        pad = {"padx": 12, "pady": 4}

        r1 = ttk.Frame(self); r1.pack(fill="x", **pad)
        ttk.Label(r1, text="ROMs folder:", width=14).pack(side="left")
        self.var_src = tk.StringVar()
        e = ttk.Entry(r1, textvariable=self.var_src)
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="Browse…", command=self._browse).pack(
            side="left", padx=(6, 0))

        r2 = ttk.Frame(self); r2.pack(fill="x", **pad)
        ttk.Label(r2, text="Subfolder:", width=14).pack(side="left")
        self.var_out = tk.StringVar(value="converted")
        ttk.Entry(r2, textvariable=self.var_out, width=24).pack(side="left")
        ttk.Label(r2, text="  (created inside the ROMs folder)",
                  foreground="#666").pack(side="left")

        r3 = ttk.Frame(self); r3.pack(fill="x", **pad)
        self.btn = ttk.Button(r3, text="⚙  Convert", command=self._start)
        self.btn.pack(side="left")
        self.btn_stop = ttk.Button(r3, text="■ Stop", state="disabled",
                                   command=self._stop)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.lbl = ttk.Label(r3, text="")
        self.lbl.pack(side="left", padx=(12, 0))

        self.pb = ttk.Progressbar(self, maximum=100)
        self.pb.pack(fill="x", **pad)

        lf = ttk.Frame(self); lf.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(lf, height=12, state="disabled",
                           font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._q = queue.Queue()
        self._stop_flag = False
        self.after(100, self._pump)
        if not cn.find_chdman():
            self._log("WARNING: chdman.exe not found next to the app — "
                      "CHD extraction will fail. Keep chdman.exe in the "
                      "same folder.")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.var_src.get() or None)
        if d:
            self.var_src.set(d)
            threading.Thread(target=self._scan, args=(d,),
                             daemon=True).start()

    def _scan(self, d):
        try:
            pairs, orphans = cn.find_games(d)
            self._log(f"{len(pairs)} Chihiro game(s) detected in "
                      f"{os.path.basename(d) or d}: "
                      + (", ".join(p[0] for p in pairs) or "—"))
            for name in orphans:
                self._log(f"  ! {name}: key zip found but no .chd "
                          f"(needs {name}\\<disc>.chd or {name}.chd)")
        except Exception as e:
            self._log(f"scan failed: {e}")

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
                elif kind == "done":
                    self.btn.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    messagebox.showinfo("Chihiro Converter", v)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _stop(self):
        self._stop_flag = True
        self._log("stopping after the current game…")

    def _start(self):
        src = self.var_src.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Chihiro Converter",
                                 "choose the folder with your ROMs first")
            return
        sub = (self.var_out.get().strip() or "converted")
        if not cn.find_chdman():
            messagebox.showerror("Chihiro Converter",
                                 "chdman.exe is missing — put it next to "
                                 "the app (it ships in the zip)")
            return
        self._stop_flag = False
        self.btn.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self._work,
                         args=(src, os.path.join(src, sub)),
                         daemon=True).start()

    def _work(self, src, out_dir):
        old_out = sys.stdout
        sys.stdout = QueueWriter(self._q)
        try:
            pairs, orphans = cn.find_games(src)
            for name in orphans:
                self._q.put(("log", f"  ! {name}: key zip found but no "
                             f".chd — skipped"))
            if not pairs:
                self._q.put(("done", "no Chihiro games found — each game "
                             "needs its .zip (key) and its .chd dump"))
                return
            os.makedirs(out_dir, exist_ok=True)
            total = len(pairs)
            self._q.put(("log", f"converting {total} game(s) into "
                         f"{out_dir}"))
            ok = failed = 0

            for i, (name, zpath, chd) in enumerate(pairs):
                if self._stop_flag:
                    break
                self._q.put(("status", f"{i + 1}/{total}  {name}"))

                def tick(cur, tot, label="", base=i):
                    frac = (cur / tot) if tot else 0
                    self._q.put(("pb", (base + frac) / total * 100))
                cn.progress = tick        # engine's bar -> our progressbar
                try:
                    res = cn.convert_game(name, zpath, chd,
                                          output_dir=out_dir)
                    if res:
                        ok += 1
                        self._q.put(("log", f"  ✔ {name} -> "
                                     f"{os.path.basename(res)} "
                                     f"({os.path.getsize(res) // (1 << 20)} MB)"))
                    else:
                        failed += 1
                        self._q.put(("log", f"  ✘ {name} failed (see above)"))
                except Exception as e:
                    failed += 1
                    self._q.put(("log", f"  ✘ {name} crashed: {e}"))
                self._q.put(("pb", (i + 1) / total * 100))

            verdict = (f"{ok} converted, {failed} failed"
                       + (" — stopped by user" if self._stop_flag else ""))
            self._q.put(("status", ""))
            self._q.put(("done", verdict))
        except Exception as e:
            self._q.put(("done", f"failed: {e}"))
        finally:
            sys.stdout = old_out


if __name__ == "__main__":
    App().mainloop()
