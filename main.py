# HyperSpin Clinic — desktop UI.
# Vertical white window, tabs along the top. The Setup tab configures the
# application: the HyperSpin folder and the (encrypted) Claude API key.
# Run: python main.py
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from clinic import artfinder, config, enrich, hyperspin_db as hdb, secrets


SORT_MODES = ("Name A→Z", "Name Z→A", "Missing asc", "Missing desc")


class SystemListPanel:
    """Reusable scrollable checkbox list of systems. NOTHING is
    pre-selected on any tab (user rule: an accidental Start must never
    run on the whole collection) — use the All button to select all.
    Sortable by system name or by the analysis line's missing count
    (user rule) — the default is ALPHABETICAL ascending, regardless of
    the Main Menu.xml order."""

    def __init__(self, parent, height=220, preselect=False, stats=False,
                 get_cfg=None, stats_fn=None):
        self.vars = {}
        self.preselect = preselect
        self.stats = stats or stats_fn is not None
        self.stats_fn = stats_fn      # optional (cfg, system) -> (text, ok)
        self.get_cfg = get_cfg
        self._stats_gen = 0
        self.missing = {}             # system -> numeric missing count (from stats)
        self._rows = {}               # system -> [checkbutton, stat label|None]
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=16, pady=(0, 4))
        ttk.Button(bar, text="↻ Reload", command=self.reload).pack(side="left")
        ttk.Button(bar, text="All", command=lambda: self.set_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="None", command=lambda: self.set_all(False)).pack(side="left", padx=(4, 0))
        ttk.Label(bar, text="Sort:", style="Sub.TLabel").pack(side="left", padx=(12, 2))
        self.var_sort = tk.StringVar(value=SORT_MODES[0])
        cmb_sort = ttk.Combobox(bar, textvariable=self.var_sort, state="readonly",
                                width=12, values=SORT_MODES)
        cmb_sort.pack(side="left")
        cmb_sort.bind("<<ComboboxSelected>>", lambda e: self._resort())
        self.lbl_count = ttk.Label(bar, text="", style="Sub.TLabel")
        self.lbl_count.pack(side="right")
        wrap = ttk.Frame(parent)
        wrap.pack(fill="x", padx=16)
        self.canvas = tk.Canvas(wrap, height=height, bg=BG, highlightthickness=1,
                                highlightbackground="#dddddd")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.get_root = lambda: ""

    def bind_root(self, getter):
        self.get_root = getter
        return self

    def reload(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.vars = {}
        self.missing = {}
        self._rows = {}
        root = self.get_root()
        systems = hdb.list_systems(root) if root else []
        if not systems:
            ttk.Label(self.inner, style="Sub.TLabel", text=(
                "No systems found — save a valid HyperSpin folder in Setup "
                "(needs Databases\\Main Menu\\Main Menu.xml).")).pack(anchor="w", padx=8, pady=8)
        self._stat_labels = {}
        for s_name in systems:
            var = tk.BooleanVar(value=self.preselect)
            self.vars[s_name] = var
            chk = ttk.Checkbutton(self.inner, text=s_name, variable=var)
            lbl = None
            if self.stats:
                lbl = ttk.Label(self.inner, text="analyzing…",
                                style="Miss.TLabel")
                self._stat_labels[s_name] = lbl
            self._rows[s_name] = [chk, lbl]
        self._resort()
        self.lbl_count.configure(text=f"{len(systems)} system(s)")
        if self.stats and systems and self.get_cfg:
            self._run_stats(systems)

    def _sorted_names(self):
        names = list(self._rows)
        mode = self.var_sort.get()
        if mode == "Name Z→A":
            return sorted(names, key=str.casefold, reverse=True)
        if mode in ("Missing asc", "Missing desc"):
            return sorted(names,
                          key=lambda s: (self.missing.get(s, 0), s.casefold()),
                          reverse=(mode == "Missing desc"))
        return sorted(names, key=str.casefold)

    def _resort(self):
        """Re-pack every row in the current sort order (selection state
        lives in the vars and survives re-packing untouched)."""
        for chk, lbl in self._rows.values():
            chk.pack_forget()
            if lbl:
                lbl.pack_forget()
        for s_name in self._sorted_names():
            chk, lbl = self._rows[s_name]
            chk.pack(anchor="w", padx=8, pady=(1, 0))
            if lbl:
                lbl.pack(anchor="w", padx=28, pady=(0, 2))
        self.canvas.yview_moveto(0)

    def _run_stats(self, systems):
        """Missing wheels/videos per system, computed off the UI thread so
        big collections never freeze the window; labels fill in as each
        system is analyzed. A reload invalidates older runs."""
        from clinic import artfinder
        self._stats_gen += 1
        gen = self._stats_gen
        cfg = dict(self.get_cfg())

        def work():
            for s_name in systems:
                if gen != self._stats_gen:
                    return
                if self.stats_fn:
                    try:
                        text, ok = self.stats_fn(cfg, s_name)
                    except Exception as e:
                        text, ok = f"analysis failed: {e}", False
                    def update_custom(nm=s_name, t=text, k=ok):
                        lbl = self._stat_labels.get(nm)
                        if not lbl or gen != self._stats_gen:
                            return
                        # numeric key for the Missing sort: a complete
                        # system is 0, else the line's first number
                        m = re.search(r"\d+", t)
                        self.missing[nm] = 0 if k else (int(m.group(0)) if m else 0)
                        try:
                            lbl.configure(text=t,
                                          style="OK.TLabel" if k else "Miss.TLabel")
                        except tk.TclError:
                            pass
                    lbl = self._stat_labels.get(s_name)
                    if lbl:
                        try:
                            lbl.after(0, update_custom)
                        except tk.TclError:
                            return   # widget destroyed by a Reload
                    continue
                try:
                    games, mw, mv, mr = artfinder.missing_counts(cfg, s_name)
                except Exception:
                    games, mw, mv, mr = 0, 0, 0, None
                def update(nm=s_name, g=games, a=mw, b=mv, r=mr):
                    lbl = self._stat_labels.get(nm)
                    if not lbl or gen != self._stats_gen:
                        return
                    self.missing[nm] = (a + b + (r or 0)) if g else 0
                    try:
                        if g == 0:
                            lbl.configure(text="no games in database",
                                          style="Miss.TLabel")
                        elif a == 0 and b == 0 and not r:
                            lbl.configure(text=f"✓ complete ({g} games)",
                                          style="OK.TLabel")
                        else:
                            # only list what is actually missing - a
                            # "0 rom(s)" entry reads like nothing was found
                            parts = []
                            if a:
                                parts.append(f"{a} wheel(s)")
                            if b:
                                parts.append(f"{b} video(s)")
                            if r:
                                parts.append(f"{r} rom(s)")
                            lbl.configure(
                                text="⚠ missing: " + ", ".join(parts)
                                     + f" of {g} games",
                                style="Miss.TLabel")
                    except tk.TclError:
                        pass
                lbl = self._stat_labels.get(s_name)
                if lbl:
                    try:
                        lbl.after(0, update)
                    except tk.TclError:
                        return   # widget destroyed by a Reload - stale run
            # analysis done: a Missing sort chosen before the counts were
            # known can now order for real
            def final_resort():
                if gen == self._stats_gen and self.var_sort.get().startswith("Missing"):
                    self._resort()
            try:
                self.canvas.after(0, final_resort)
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def set_all(self, value):
        for var in self.vars.values():
            var.set(value)

    def selected(self):
        return [s for s, v in self.vars.items() if v.get()]

BG = "#ffffff"
FG = "#1a1a1a"
SUBTLE = "#6b6b6b"
ACCENT = "#0e7bd1"
OK = "#1a7f37"
BAD = "#c62828"


def scrolled_log(parent, pack_kw, **text_kw):
    """A console/log Text with a vertical scrollbar (every tab's log)."""
    frame = ttk.Frame(parent)
    frame.pack(**pack_kw)
    txt = tk.Text(frame, bg="#fafafa", fg=FG, relief="flat",
                  state="disabled", highlightthickness=1,
                  highlightbackground="#dddddd", **text_kw)
    sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return txt


class ClinicApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HyperSpin Clinic")
        self.geometry("560x1000")         # vertical rectangle, all 8 tabs visible
        self.minsize(540, 780)
        self.configure(bg=BG)
        self.cfg = config.load()
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # user rule: verify yt-dlp freshness at every startup - stale
        # yt-dlp is the #1 cause of YouTube art failures. Non-blocking:
        # the check runs off-thread, the ASK happens on the UI thread.
        self.after(1500, self._check_ytdlp)

    def _check_ytdlp(self):
        from clinic import artfinder, ytupdate

        def work():
            cur, latest = ytupdate.check(artfinder._ytdlp())
            if latest:
                self.after(0, lambda: self._offer_ytdlp(cur, latest))
        threading.Thread(target=work, daemon=True).start()

    def _offer_ytdlp(self, cur, latest):
        from clinic import ytupdate
        msg = (f"A new yt-dlp version is available: {latest}"
               + (f" (you have {cur})." if cur else
                  " (no yt-dlp found on this machine).")
               + "\n\nYouTube video retrieval breaks when yt-dlp is "
               "outdated. Install the update into the application now?")
        if not messagebox.askyesno("yt-dlp update", msg):
            return

        def work():
            ok = ytupdate.download_latest(log=lambda m: None)
            self.after(0, lambda: messagebox.showinfo(
                "yt-dlp update",
                f"yt-dlp {latest} installed — YouTube retrieval is up to date."
                if ok else
                "The update could not be downloaded — YouTube retrieval "
                "will use the existing version. Check your connection "
                "and restart the app to retry."))
        threading.Thread(target=work, daemon=True).start()

    def _on_close(self):
        # signal workers so no rename/enrichment batch is killed mid-write
        self._stop = True
        self._art_stop = True
        self._ts_stop = True
        # suite children (powershell/deno/chrome/ffmpeg) must not survive
        # the window as orphans - kill the tracked trees directly, the
        # worker's own stop check may never run again after destroy
        try:
            from clinic import themesuite
            themesuite.kill_active()
        except Exception:
            pass
        self.after(150, self.destroy)

    # ---------- theming ----------
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background="#f2f2f2", foreground=FG,
                    padding=(9, 8), font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab",
              background=[("selected", BG)],
              foreground=[("selected", ACCENT)])
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Sub.TLabel", foreground=SUBTLE, font=("Segoe UI", 9))
        s.configure("H.TLabel", font=("Segoe UI", 12, "bold"))
        s.configure("Status.TLabel", font=("Segoe UI", 9, "bold"))
        s.configure("Miss.TLabel", foreground=BAD, background=BG,
                    font=("Segoe UI", 8))
        s.configure("OK.TLabel", foreground=OK, background=BG,
                    font=("Segoe UI", 8))
        s.configure("TButton", padding=(12, 6))
        s.configure("Accent.TButton", padding=(12, 6))
        s.configure("TEntry", fieldbackground="#fafafa")

    # ---------- layout ----------
    def _build(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_setup = ttk.Frame(self.nb)
        self.nb.add(self.tab_setup, text="Setup")
        self._build_setup(self.tab_setup)
        self.tab_systems = ttk.Frame(self.nb)
        self.nb.add(self.tab_systems, text="Systems")
        self._build_systems(self.tab_systems)
        self.tab_art = ttk.Frame(self.nb)
        self.nb.add(self.tab_art, text="Missing Art")
        self._build_art(self.tab_art)
        self.tab_rename = ttk.Frame(self.nb)
        self.nb.add(self.tab_rename, text="Rename")
        self._build_rename(self.tab_rename)
        self.tab_themes = ttk.Frame(self.nb)
        self.nb.add(self.tab_themes, text="Themes")
        self._build_themes(self.tab_themes)
        self.tab_revert = ttk.Frame(self.nb)
        self.tab_ask = ttk.Frame(self.nb)
        self.nb.add(self.tab_ask, text="Ask AI")
        self._build_ask(self.tab_ask)
        self.nb.add(self.tab_revert, text="Revert")
        self._build_revert(self.tab_revert)
        self.tab_help = ttk.Frame(self.nb)
        self.nb.add(self.tab_help, text="Help")
        self._build_help(self.tab_help)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_change)

    # ---------- Help tab ----------
    def _build_help(self, tab):
        import webbrowser
        from clinic.helptext import HELP, WARNING
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=12, pady=12)
        txt = tk.Text(wrap, bg=BG, fg=FG, relief="flat", wrap="word",
                      font=("Segoe UI", 9), padx=8, pady=8,
                      highlightthickness=1, highlightbackground="#dddddd")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.tag_configure("warn", foreground=BAD,
                          font=("Segoe UI", 10, "bold"))
        txt.tag_configure("link", foreground=ACCENT, underline=True)
        txt.insert("1.0", WARNING + "\n", "warn")
        # make the key URL clickable
        idx = txt.search("https://console.anthropic.com/settings/keys", "1.0")
        if idx:
            end = f"{idx}+{len('https://console.anthropic.com/settings/keys')}c"
            txt.tag_add("link", idx, end)
            txt.tag_bind("link", "<Button-1>", lambda e: webbrowser.open(
                "https://console.anthropic.com/settings/keys"))
            txt.tag_bind("link", "<Enter>", lambda e: txt.configure(cursor="hand2"))
            txt.tag_bind("link", "<Leave>", lambda e: txt.configure(cursor=""))
        txt.insert("end", HELP)
        txt.configure(state="disabled")
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ---------- Setup tab ----------
    def _build_setup(self, outer):
        # the whole tab scrolls: its sections outgrow small screens and
        # nothing must ever be unreachable below the fold
        self.setup_canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical",
                           command=self.setup_canvas.yview)
        tab = ttk.Frame(self.setup_canvas)
        win = self.setup_canvas.create_window((0, 0), window=tab, anchor="nw")
        tab.bind("<Configure>", lambda e: self.setup_canvas.configure(
            scrollregion=self.setup_canvas.bbox("all")))
        self.setup_canvas.bind("<Configure>", lambda e:
                               self.setup_canvas.itemconfigure(win, width=e.width))
        self.setup_canvas.configure(yscrollcommand=sb.set)
        self.setup_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pad = {"padx": 16}
        ttk.Label(tab, text="Setup", style="H.TLabel").pack(anchor="w", pady=(16, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Configure the application: point it at your HyperSpin folder and "
            "add your Claude API key. Nothing else happens on this tab.")
        ).pack(anchor="w", pady=(0, 14), **pad)

        # --- HyperSpin folder ---
        ttk.Label(tab, text="HyperSpin folder", style="H.TLabel").pack(anchor="w", **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Select your actual HyperSpin folder (the one containing the "
            "Media and Databases folders). Saved automatically.")
        ).pack(anchor="w", pady=(0, 4), **pad)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(4, 2), **pad)
        self.var_root = tk.StringVar(value=self.cfg.get("hyperspin_root", ""))
        ent_root = ttk.Entry(row, textvariable=self.var_root)
        ent_root.pack(side="left", fill="x", expand=True, ipady=3)
        ent_root.bind("<FocusOut>", lambda e: self._apply_root())
        ent_root.bind("<Return>", lambda e: self._apply_root())
        ttk.Button(row, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))

        self.lbl_root_status = ttk.Label(tab, text="", style="Status.TLabel",
                                         wraplength=500, justify="left")
        self.lbl_root_status.pack(anchor="w", pady=(4, 0), **pad)

        treewrap = ttk.Frame(tab)
        treewrap.pack(fill="x", pady=(8, 6), **pad)
        self.tree = ttk.Treeview(treewrap, columns=("games",),
                                 show="headings", height=6)
        self.tree.heading("games", text="Games")
        self.tree.column("games", width=90, anchor="e")
        self.tree["show"] = ("tree", "headings")
        self.tree.heading("#0", text="System")
        self.tree.column("#0", width=260)
        tree_sb = ttk.Scrollbar(treewrap, orient="vertical",
                                command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        tree_sb.pack(side="right", fill="y")

        # --- RocketLauncher folder (centralized rom locations) ---
        ttk.Label(tab, text="RocketLauncher folder", style="H.TLabel").pack(anchor="w", **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Rom locations are read from RocketLauncher's per-system settings "
            "(Settings\\<System>\\Emulators.ini, Rom_Path) - used by the "
            "missing-rom analysis and the Rename tab.")
        ).pack(anchor="w", pady=(0, 4), **pad)
        rlrow = ttk.Frame(tab)
        rlrow.pack(fill="x", pady=(2, 2), **pad)
        self.var_rl = tk.StringVar(value=self.cfg.get("rocketlauncher_root", ""))
        ttk.Entry(rlrow, textvariable=self.var_rl).pack(
            side="left", fill="x", expand=True, ipady=3)
        ttk.Button(rlrow, text="Browse…", command=self._browse_rl).pack(side="left", padx=(8, 0))
        self.lbl_rl_status = ttk.Label(tab, text="", style="Status.TLabel",
                                       wraplength=500, justify="left")
        self.lbl_rl_status.pack(anchor="w", pady=(4, 6), **pad)
        self._refresh_rl_status()

        ttk.Separator(tab).pack(fill="x", pady=6, **pad)

        # --- Claude API key ---
        ttk.Label(tab, text="Claude API key", style="H.TLabel").pack(anchor="w", **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Stored encrypted in the Windows Credential Manager (never written "
            "to any project file), following OWASP secrets-management practice.")
        ).pack(anchor="w", pady=(0, 6), **pad)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=(2, 2), **pad)
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(row2, textvariable=self.var_key, show="•")
        self.ent_key.pack(side="left", fill="x", expand=True, ipady=3)
        ttk.Button(row2, text="Add key", command=self._add_key).pack(side="left", padx=(8, 0))

        row3 = ttk.Frame(tab)
        row3.pack(fill="x", pady=(6, 2), **pad)
        self.lbl_key_status = ttk.Label(row3, text="", style="Status.TLabel")
        self.lbl_key_status.pack(side="left")
        ttk.Button(row3, text="Test key", command=self._test_key).pack(side="right", padx=(8, 0))
        ttk.Button(row3, text="Remove", command=self._remove_key).pack(side="right")

        # --- AI model & costs ---
        ttk.Label(tab, text="AI model & costs", style="H.TLabel").pack(
            anchor="w", pady=(6, 0), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Pick which Claude model does the work — the default is the "
            "CHEAPEST current model, not the most capable. Every API call's "
            "cost is shown in the enrichment log (from real token usage). "
            "Credit balance can't be read through the API — check your "
            "Anthropic console billing page.")
        ).pack(anchor="w", pady=(0, 4), **pad)
        rowm = ttk.Frame(tab)
        rowm.pack(fill="x", pady=(2, 2), **pad)
        self.var_model = tk.StringVar(value=self.cfg.get("model", "claude-haiku-4-5"))
        self.cmb_model = ttk.Combobox(rowm, textvariable=self.var_model,
                                      state="readonly", width=34)
        from clinic import costs as costs_mod
        self.cmb_model.configure(values=costs_mod.FALLBACK_MODELS)
        self.cmb_model.pack(side="left")
        self.cmb_model.bind("<<ComboboxSelected>>", lambda e: self._save_model())
        ttk.Button(rowm, text="Refresh list", command=self._refresh_models).pack(
            side="left", padx=(8, 0))
        ttk.Button(rowm, text="Billing…", command=lambda: __import__(
            "webbrowser").open(costs_mod.BILLING_URL)).pack(side="left", padx=(4, 0))
        self.lbl_model_cost = ttk.Label(tab, text="", style="Status.TLabel",
                                        wraplength=500, justify="left")
        self.lbl_model_cost.pack(anchor="w", pady=(2, 2), **pad)
        self._update_model_cost_label()

        ttk.Separator(tab).pack(fill="x", pady=6, **pad)

        # --- EmuMovies FTP credentials ---
        ttk.Label(tab, text="EmuMovies account", style="H.TLabel").pack(anchor="w", **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Used by the Missing Art tab (files.emumovies.com — the service "
            "is plain FTP per the knowledge base). Stored encrypted in the "
            "Windows Credential Manager like the API key.")
        ).pack(anchor="w", pady=(0, 6), **pad)
        row4 = ttk.Frame(tab)
        row4.pack(fill="x", pady=(2, 2), **pad)
        self.var_emu_user = tk.StringVar()
        self.var_emu_pass = tk.StringVar()
        ttk.Entry(row4, textvariable=self.var_emu_user, width=16).pack(side="left", ipady=3)
        ttk.Entry(row4, textvariable=self.var_emu_pass, show="•").pack(
            side="left", fill="x", expand=True, padx=(6, 0), ipady=3)
        ttk.Button(row4, text="Save", command=self._save_emu).pack(side="left", padx=(8, 0))
        row5 = ttk.Frame(tab)
        row5.pack(fill="x", pady=(6, 2), **pad)
        self.lbl_emu_status = ttk.Label(row5, text="", style="Status.TLabel")
        self.lbl_emu_status.pack(side="left")
        ttk.Button(row5, text="Remove", command=self._remove_emu).pack(side="right")

        ttk.Separator(tab).pack(fill="x", pady=6, **pad)

        # --- YouTube sign-in (yt-dlp cookies) ---
        ttk.Label(tab, text="YouTube sign-in", style="H.TLabel").pack(anchor="w", **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "The Missing Art tab falls back to YouTube for video snaps. "
            "YouTube periodically blocks anonymous downloads for a while "
            "(HTTP 403 / “sign in to confirm”) — signing in avoids the "
            "wait. Pick the browser where you are logged in to YouTube "
            "(yt-dlp reads its cookies directly; Firefox is the most "
            "reliable on Windows), or select an exported cookies.txt file "
            "(used instead of the browser when both are set). Cookies are "
            "only ever read locally by yt-dlp — nothing is stored by "
            "this app.")
        ).pack(anchor="w", pady=(0, 6), **pad)
        rowy = ttk.Frame(tab)
        rowy.pack(fill="x", pady=(2, 2), **pad)
        ttk.Label(rowy, text="Browser:", style="Sub.TLabel").pack(side="left")
        self.var_yt_browser = tk.StringVar(
            value=self.cfg.get("youtube_cookies_browser", "") or "(none)")
        self.cmb_yt_browser = ttk.Combobox(
            rowy, textvariable=self.var_yt_browser, state="readonly", width=12,
            values=("(none)", "firefox", "chrome", "edge", "brave",
                    "opera", "vivaldi"))
        self.cmb_yt_browser.pack(side="left", padx=(6, 0))
        self.cmb_yt_browser.bind("<<ComboboxSelected>>",
                                 lambda e: self._save_youtube())
        rowy2 = ttk.Frame(tab)
        rowy2.pack(fill="x", pady=(4, 2), **pad)
        ttk.Label(rowy2, text="cookies.txt:", style="Sub.TLabel").pack(side="left")
        self.var_yt_cookies = tk.StringVar(
            value=self.cfg.get("youtube_cookies_file", ""))
        ent_yt = ttk.Entry(rowy2, textvariable=self.var_yt_cookies)
        ent_yt.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=3)
        ent_yt.bind("<FocusOut>", lambda e: self._save_youtube())
        ent_yt.bind("<Return>", lambda e: self._save_youtube())
        ttk.Button(rowy2, text="Browse…",
                   command=self._browse_yt_cookies).pack(side="left", padx=(8, 0))
        ttk.Button(rowy2, text="Clear",
                   command=self._clear_youtube).pack(side="left", padx=(4, 0))
        self.lbl_yt_status = ttk.Label(tab, text="", style="Status.TLabel",
                                       wraplength=500, justify="left")
        self.lbl_yt_status.pack(anchor="w", pady=(4, 6), **pad)

        self._refresh_root_status(initial=True)
        self._refresh_key_status()
        self._refresh_emu_status()
        self._refresh_yt_status()

    def _save_emu(self):
        try:
            secrets.store_emumovies(self.var_emu_user.get(), self.var_emu_pass.get())
        except ValueError as e:
            messagebox.showwarning("EmuMovies", str(e))
            return
        self.var_emu_user.set("")
        self.var_emu_pass.set("")
        self._refresh_emu_status()
        messagebox.showinfo("EmuMovies", "Credentials stored securely.")

    def _remove_emu(self):
        if secrets.emumovies_stored() and messagebox.askyesno(
                "EmuMovies", "Remove the stored EmuMovies credentials?"):
            secrets.clear_emumovies()
        self._refresh_emu_status()

    def _refresh_emu_status(self):
        if secrets.emumovies_stored():
            self.lbl_emu_status.configure(
                text="✓ Credentials stored (encrypted).", foreground=OK)
        else:
            self.lbl_emu_status.configure(
                text="No credentials stored (username + password).", foreground=SUBTLE)

    # ---------- Setup: YouTube sign-in ----------
    def _save_youtube(self):
        browser = self.var_yt_browser.get()
        self.cfg["youtube_cookies_browser"] = ("" if browser == "(none)"
                                               else browser)
        self.cfg["youtube_cookies_file"] = self.var_yt_cookies.get().strip()
        config.save(self.cfg)
        self._refresh_yt_status()

    def _browse_yt_cookies(self):
        p = filedialog.askopenfilename(
            title="Select the exported cookies.txt",
            filetypes=[("cookies.txt", "*.txt"), ("All files", "*.*")])
        if p:
            self.var_yt_cookies.set(p)
            self._save_youtube()

    def _clear_youtube(self):
        self.var_yt_browser.set("(none)")
        self.var_yt_cookies.set("")
        self._save_youtube()

    def _refresh_yt_status(self):
        from clinic.artfinder import cookies_file_usable
        f = self.cfg.get("youtube_cookies_file", "")
        b = self.cfg.get("youtube_cookies_browser", "")
        if f and os.path.isfile(f) and cookies_file_usable(f):
            self.lbl_yt_status.configure(
                text=f"✓ Using cookies.txt: {f}", foreground=OK)
        elif f and os.path.isfile(f):
            self.lbl_yt_status.configure(
                text="That cookies.txt has no signed-in YouTube cookies — "
                     "export it from a browser that is signed in to "
                     "YouTube. It will be IGNORED until then.", foreground=BAD)
        elif f:
            self.lbl_yt_status.configure(
                text=f"cookies.txt not found: {f} — sign-in is OFF until "
                     "the file exists.", foreground=BAD)
        elif b:
            self.lbl_yt_status.configure(
                text=f"✓ Using {b} cookies when YouTube requires sign-in.",
                foreground=OK)
        else:
            self.lbl_yt_status.configure(
                text="No sign-in configured — YouTube downloads run "
                     "anonymously.", foreground=SUBTLE)

    # ---------- Systems tab ----------
    def _build_systems(self, tab):
        pad = {"padx": 16}
        ttk.Label(tab, text="Systems", style="H.TLabel").pack(anchor="w", pady=(16, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Systems from Databases\\Main Menu\\Main Menu.xml. Nothing is "
            "pre-selected — tick the systems to work on (or All), then "
            "Start: every game of each "
            "selected system gets its published year, manufacturer and genre "
            "filled in by AI, the system XML is updated (backup taken), and "
            "the data is stored per system in the vector database and "
            "Elasticsearch.")).pack(anchor="w", pady=(0, 10), **pad)

        # Systems tab analysis = METADATA ONLY (user rule): how many games
        # still miss year/manufacturer/genre - art/rom gaps live on their
        # own tabs
        self.sys_list = SystemListPanel(
            tab, height=260,
            get_cfg=lambda: self.cfg,
            stats_fn=enrich.metadata_stats_line).bind_root(
            lambda: self.cfg.get("hyperspin_root", ""))
        self.sys_canvas = self.sys_list.canvas

        def _wheel(e):
            idx = self.nb.index(self.nb.select())
            canvas = {0: getattr(self, "setup_canvas", None),
                      1: self.sys_list.canvas,
                      2: getattr(getattr(self, "art_list", None), "canvas", None),
                      3: getattr(getattr(self, "rename_list", None), "canvas", None),
                      4: getattr(getattr(self, "themes_list", None), "canvas", None),
                      6: getattr(self, "rv_canvas", None)}.get(idx)
            # idx 5 (Ask AI) intentionally absent: the chat Text widget
            # scrolls itself via the Text class binding
            if canvas:
                canvas.yview_scroll(-1 * (e.delta // 120), "units")
        self.sys_canvas.bind_all("<MouseWheel>", _wheel)

        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=(8, 2), **pad)
        self.var_fill_empty = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Only fill missing fields (never overwrite existing values)",
                        variable=self.var_fill_empty).pack(anchor="w")
        self.var_web_search = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text=("Web-search games the AI can't identify "
                                    "(recent titles — ≈$0.01 per search, "
                                    "cost shown in the log)"),
                        variable=self.var_web_search).pack(anchor="w")
        self.var_retry_failed = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text=("Retry games that previously failed "
                                    "(they are cached and skipped for free "
                                    "otherwise)"),
                        variable=self.var_retry_failed).pack(anchor="w")

        run = ttk.Frame(tab)
        run.pack(fill="x", pady=(6, 4), **pad)
        self.btn_start = ttk.Button(run, text="▶ Start enrichment", command=self._start_enrich)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(run, text="■ Stop", command=self._stop_enrich, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.pb = ttk.Progressbar(run, mode="determinate", length=140)
        self.pb.pack(side="right")
        self.lbl_sys_status = ttk.Label(tab, text="", style="Status.TLabel",
                                        wraplength=500, justify="left")
        self.lbl_sys_status.pack(anchor="w", padx=16)

        self.txt_log = scrolled_log(
            tab, {"fill": "both", "expand": True, "pady": (4, 12), **pad},
            height=10, font=("Consolas", 9), wrap="word")

        self._worker = None
        self._stop = False
        self._logq = queue.Queue()
        self.after(200, self._drain_log)

    def _on_tab_change(self, _e=None):
        idx = self.nb.index(self.nb.select())
        if idx == 1 and not self.sys_list.vars:
            self.sys_list.reload()
        elif idx == 2 and not self.art_list.vars:
            self.art_list.reload()
        elif idx == 3 and not self.rename_list.vars:
            self.rename_list.reload()
        elif idx == 4 and not self.themes_list.vars:
            self.themes_list.reload()
        elif idx == 6:
            self._rv_refresh()

    def _load_systems(self):
        self.sys_list.reload()

    def _check_all(self, value):
        self.sys_list.set_all(value)

    def _log(self, msg):
        self._logq.put(("sys", msg))

    def _drain_log(self):
        try:
            while True:
                tag, msg = self._logq.get_nowait()
                widget = self.txt_art_log if tag == "art" else self.txt_log
                widget.configure(state="normal")
                widget.insert("end", msg + "\n")
                widget.see("end")
                widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(200, self._drain_log)


    def _start_enrich(self):
        selected = self.sys_list.selected()
        if not selected:
            messagebox.showinfo("Systems", "No systems selected.")
            return
        if not secrets.is_stored():
            messagebox.showwarning("Systems", "Add your Claude API key in Setup first.")
            return
        self._stop = False
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        total = len(selected)
        fill_empty = self.var_fill_empty.get()   # read on the UI thread
        web_search = self.var_web_search.get()
        retry_failed = self.var_retry_failed.get()
        self.pb.configure(maximum=total * 100, value=0)

        def status(idx, frac, msg):
            overall = round((idx + frac) / total * 100)
            self.after(0, lambda: (
                self.pb.configure(value=(idx + frac) * 100),
                self.lbl_sys_status.configure(text=f"{overall}% — {msg}")))

        def run():
            done = 0
            try:
                for idx, s_name in enumerate(selected):
                    if self._stop:
                        self._log("— stopped by user —")
                        break
                    status(idx, 0.0, f"{s_name}: reading database")
                    try:
                        enrich.enrich_system(
                            self.cfg, s_name, self._log,
                            stop_flag=lambda: self._stop,
                            only_fill_empty=fill_empty,
                            web_search=web_search,
                            retry_failed=retry_failed,
                            progress=lambda f, m, i=idx: status(i, f, m))
                    except enrich.StopRequested:
                        self._log("— stopped by user —")
                        break
                    except Exception as e:
                        self._log(f"[{s_name}] ERROR: {e}")
                    done += 1
                    status(idx, 1.0, f"{s_name}: done")
                self._log(f"=== finished ({done}/{total} systems) ===")
                self.after(0, lambda: self.lbl_sys_status.configure(
                    text=f"100% — finished {done}/{total} system(s)"))
            finally:
                self.after(0, lambda: (self.btn_start.configure(state="normal"),
                                       self.btn_stop.configure(state="disabled")))

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _stop_enrich(self):
        self._stop = True

    # ---------- Missing Art tab ----------
    def _build_art(self, tab):
        pad = {"padx": 16}
        ttk.Label(tab, text="Missing Art", style="H.TLabel").pack(anchor="w", pady=(16, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Finds missing wheel art and videos for the selected systems. "
            "Sources in knowledge-base order: the system's own folders "
            "(aliases, copy-only), EmuMovies FTP, then YouTube for videos. "
            "Wheels are curated: ×0.75 horizontal squeeze + 400px wide. "
            "Every addition is logged and tracked in the database.")
        ).pack(anchor="w", pady=(0, 8), **pad)

        self.art_list = SystemListPanel(
            tab, height=180, stats=True,
            get_cfg=lambda: self.cfg).bind_root(
            lambda: self.cfg.get("hyperspin_root", ""))

        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=(8, 2), **pad)
        self.art_opts = {
            "wheel": tk.BooleanVar(value=True),
            "video": tk.BooleanVar(value=True),
            "local": tk.BooleanVar(value=True),
            "emumovies": tk.BooleanVar(value=True),
            "launchbox": tk.BooleanVar(value=True),
            "youtube": tk.BooleanVar(value=True),
        }
        r1 = ttk.Frame(opts); r1.pack(anchor="w")
        ttk.Label(r1, text="Look for: ", style="Sub.TLabel").pack(side="left")
        ttk.Checkbutton(r1, text="Wheel art", variable=self.art_opts["wheel"]).pack(side="left")
        ttk.Checkbutton(r1, text="Videos", variable=self.art_opts["video"]).pack(side="left", padx=(8, 0))
        r2 = ttk.Frame(opts); r2.pack(anchor="w")
        ttk.Label(r2, text="Sources: ", style="Sub.TLabel").pack(side="left")
        ttk.Checkbutton(r2, text="Local aliases", variable=self.art_opts["local"]).pack(side="left")
        ttk.Checkbutton(r2, text="EmuMovies", variable=self.art_opts["emumovies"]).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(r2, text="LaunchBox wheels", variable=self.art_opts["launchbox"]).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(r2, text="YouTube", variable=self.art_opts["youtube"]).pack(side="left", padx=(8, 0))

        run = ttk.Frame(tab)
        run.pack(fill="x", pady=(6, 4), **pad)
        self.btn_art_start = ttk.Button(run, text="▶ Find missing art", command=self._start_art)
        self.btn_art_start.pack(side="left")
        self.btn_art_stop = ttk.Button(run, text="■ Stop", command=lambda: setattr(self, "_art_stop", True),
                                       state="disabled")
        self.btn_art_stop.pack(side="left", padx=(8, 0))
        self.pb_art = ttk.Progressbar(run, mode="determinate", length=140)
        self.pb_art.pack(side="right")
        self.lbl_art_status = ttk.Label(tab, text="", style="Status.TLabel",
                                        wraplength=500, justify="left")
        self.lbl_art_status.pack(anchor="w", padx=16)

        self.txt_art_log = scrolled_log(
            tab, {"fill": "both", "expand": True, "pady": (4, 12), **pad},
            height=10, font=("Consolas", 9), wrap="word")
        self._art_stop = False

    def _art_log(self, msg):
        self._logq.put(("art", msg))

    def _start_art(self):
        selected = self.art_list.selected()
        if not selected:
            messagebox.showinfo("Missing Art", "No systems selected.")
            return
        if self.art_opts["emumovies"].get() and not secrets.emumovies_stored():
            self._art_log("note: no EmuMovies credentials stored (Setup tab) — that source will be skipped")
        self._art_stop = False
        self.btn_art_start.configure(state="disabled")
        self.btn_art_stop.configure(state="normal")
        n_sel = len(selected)
        self.pb_art.configure(maximum=n_sel * 100, value=0)
        opts = {k: v.get() for k, v in self.art_opts.items()}

        def status(idx, frac, msg):
            overall = round((idx + frac) / n_sel * 100)
            self.after(0, lambda: (
                self.pb_art.configure(value=(idx + frac) * 100),
                self.lbl_art_status.configure(text=f"{overall}% — {msg}")))

        def run():
            done = 0
            total = 0
            try:
                for idx, s_name in enumerate(selected):
                    if self._art_stop:
                        self._art_log("— stopped by user —")
                        break
                    status(idx, 0.0, f"{s_name}: starting scan")
                    try:
                        res = artfinder.find_for_system(
                            self.cfg, s_name, opts, self._art_log,
                            stop_flag=lambda: self._art_stop,
                            progress=lambda f, m, i=idx: status(i, f, m))
                        total += res.get("added", 0)
                    except artfinder.StopRequested:
                        self._art_log("— stopped by user —")
                        break
                    except Exception as e:
                        self._art_log(f"[{s_name}] ERROR: {e}")
                    done += 1
                    status(idx, 1.0, f"{s_name}: done")
                self._art_log(f"=== finished: {total} item(s) added across {done} system(s) ===")
                self.after(0, lambda: self.lbl_art_status.configure(
                    text=f"100% — {total} item(s) added"))
            finally:
                self.after(0, lambda: (self.btn_art_start.configure(state="normal"),
                                       self.btn_art_stop.configure(state="disabled")))

        threading.Thread(target=run, daemon=True).start()

    # ---------- Rename tab ----------
    def _build_rename(self, tab):
        pad = {"padx": 16}
        ttk.Label(tab, text="Rename", style="H.TLabel").pack(anchor="w", pady=(14, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Match wrongly-named videos, wheel art or roms to the database "
            "names using Elasticsearch — one system at a time. Check exactly "
            "one system, pick what to work on, Scan, review the matches "
            "(double-click a row to pick another candidate or skip), then "
            "Rename. A backup is taken before any rename.")
        ).pack(anchor="w", pady=(0, 6), **pad)

        self.rename_list = SystemListPanel(
            tab, height=110, preselect=False, stats=True,
            get_cfg=lambda: self.cfg).bind_root(
            lambda: self.cfg.get("hyperspin_root", ""))

        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=(6, 2), **pad)
        ttk.Label(opts, text="Work on: ", style="Sub.TLabel").pack(side="left")
        self.rn_target = tk.StringVar(value="video")
        for val, txt in (("video", "Videos"), ("wheel", "Wheel art"), ("rom", "Roms")):
            ttk.Radiobutton(opts, text=txt, value=val, variable=self.rn_target).pack(side="left", padx=(0, 6))
        ttk.Label(opts, text="Min %:", style="Sub.TLabel").pack(side="left", padx=(8, 2))
        self.rn_minpct = tk.IntVar(value=60)
        ttk.Spinbox(opts, from_=0, to=100, width=4, textvariable=self.rn_minpct).pack(side="left")

        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Rom locations come from the RocketLauncher folder configured in "
            "Setup (per-system Rom_Path).")).pack(anchor="w", **pad)

        run = ttk.Frame(tab)
        run.pack(fill="x", pady=(6, 2), **pad)
        self.btn_rn_scan = ttk.Button(run, text="🔍 Scan", command=self._rn_scan)
        self.btn_rn_scan.pack(side="left")
        self.btn_rn_apply = ttk.Button(run, text="✔ Rename selected", command=self._rn_apply,
                                       state="disabled")
        self.btn_rn_apply.pack(side="left", padx=(8, 0))
        self.lbl_rn_status = ttk.Label(run, text="", style="Sub.TLabel")
        self.lbl_rn_status.pack(side="right")

        cols = ("match", "pct")
        self.rn_tree = ttk.Treeview(tab, columns=cols, show="tree headings", height=7)
        self.rn_tree.heading("#0", text="Game (missing file)")
        self.rn_tree.column("#0", width=170)
        self.rn_tree.heading("match", text="Rename from")
        self.rn_tree.column("match", width=170)
        self.rn_tree.heading("pct", text="%")
        self.rn_tree.column("pct", width=40, anchor="e")
        self.rn_tree.pack(fill="x", pady=(4, 2), **pad)
        self.rn_tree.bind("<Double-1>", self._rn_choose)
        self.rn_rows = []

        ttk.Label(tab, text="Changes (this run + history)", style="Sub.TLabel").pack(
            anchor="w", pady=(6, 0), **pad)
        self.txt_rn_log = scrolled_log(
            tab, {"fill": "both", "expand": True, "pady": (2, 12), **pad},
            height=7, font=("Consolas", 9), wrap="none")
        self._rn_log_fill_history()

    def _rn_logline(self, msg):
        self.txt_rn_log.configure(state="normal")
        self.txt_rn_log.insert("end", msg + "\n")
        self.txt_rn_log.see("end")
        self.txt_rn_log.configure(state="disabled")

    def _rn_log_fill_history(self):
        from clinic import renamer
        for line in renamer.history_lines(200):
            self._rn_logline(line)

    def _rn_scan(self):
        from clinic import renamer
        selected = self.rename_list.selected()
        if len(selected) != 1:
            messagebox.showinfo("Rename", "Check exactly ONE system to curate at a time.")
            return
        target = self.rn_target.get()
        roms_dir = ""
        if target == "rom":
            from clinic import rocketlauncher as rl_mod
            rl = rl_mod.effective_root(self.cfg)
            paths = rl_mod.rom_paths(rl, selected[0]) if rl else []
            paths = [q for q in paths if os.path.isdir(q)]
            if not paths:
                messagebox.showwarning(
                    "Rename", "No RocketLauncher rom path found for this "
                    "system - set the RocketLauncher folder in Setup.")
                return
            roms_dir = paths[0]
            if len(paths) > 1:
                self._rn_logline(f"note: {len(paths)} rom paths configured; "
                                 f"working on the first: {roms_dir}")
        try:
            min_pct = self.rn_minpct.get()       # read on the UI thread
        except tk.TclError:
            min_pct = 60
        self.rn_tree.delete(*self.rn_tree.get_children())
        self.rn_rows = []
        self.lbl_rn_status.configure(text="scanning…")
        self.btn_rn_scan.configure(state="disabled")
        self.btn_rn_apply.configure(state="disabled")

        def run():
            try:
                rows, es_used = renamer.scan(
                    self.cfg, selected[0], target, roms_dir,
                    min_pct=min_pct,
                    log=lambda m: self.after(0, self._rn_logline, m))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: (self.lbl_rn_status.configure(text=""),
                                             self.btn_rn_scan.configure(state="normal"),
                                             messagebox.showerror("Rename", m)))
                return

            def fill():
                self.rn_rows = rows
                shown = 0
                for i, r in enumerate(rows):
                    if not r["candidates"]:
                        continue
                    chosen = r["chosen"] or "(skip)"
                    pct = r["candidates"][0][1] if r["chosen"] else ""
                    self.rn_tree.insert("", "end", iid=str(i), text=r["game"],
                                        values=(chosen, pct))
                    shown += 1
                note = "" if es_used else "  (ES offline — difflib fallback)"
                self.lbl_rn_status.configure(
                    text=f"{shown} match row(s){note}")
                self.btn_rn_apply.configure(
                    state="normal" if shown else "disabled")
                self.btn_rn_scan.configure(state="normal")
                self._current_rename_ctx = (selected[0], target, roms_dir)
            self.after(0, fill)

        threading.Thread(target=run, daemon=True).start()

    def _rn_choose(self, _e):
        sel = self.rn_tree.selection()
        if not sel:
            return
        i = int(sel[0])
        row = self.rn_rows[i]
        top = tk.Toplevel(self)
        top.title(row["game"])
        top.configure(bg=BG)
        top.geometry("+%d+%d" % (self.winfo_rootx() + 40, self.winfo_rooty() + 120))
        ttk.Label(top, text=f"Match for '{row['game']}'", style="H.TLabel").pack(
            anchor="w", padx=14, pady=(10, 4))
        if row["description"]:
            ttk.Label(top, text=row["description"], style="Sub.TLabel").pack(
                anchor="w", padx=14)
        var = tk.StringVar(value=row["chosen"] or "(skip)")
        for f, p in row["candidates"]:
            ttk.Radiobutton(top, text=f"{f}   ({p}%)", value=f, variable=var).pack(
                anchor="w", padx=18, pady=2)
        ttk.Radiobutton(top, text="(skip — do not rename)", value="(skip)",
                        variable=var).pack(anchor="w", padx=18, pady=2)

        def ok():
            row["chosen"] = None if var.get() == "(skip)" else var.get()
            pct = ""
            if row["chosen"]:
                pct = dict(row["candidates"]).get(row["chosen"], "")
            self.rn_tree.item(str(i), values=(row["chosen"] or "(skip)", pct))
            top.destroy()
        ttk.Button(top, text="OK", command=ok).pack(pady=10)
        top.transient(self)
        top.grab_set()

    def _rn_apply(self):
        from clinic import renamer
        if not getattr(self, "_current_rename_ctx", None):
            return
        system, target, roms_dir = self._current_rename_ctx
        decisions = [(r["game"], r["chosen"]) for r in self.rn_rows if r["chosen"]]
        if not decisions:
            messagebox.showinfo("Rename", "Nothing selected to rename.")
            return
        if not messagebox.askyesno(
                "Rename", f"Rename {len(decisions)} file(s) in {system} "
                f"({renamer.TARGETS[target]['label']})?\nA backup is taken first."):
            return
        self.btn_rn_apply.configure(state="disabled")

        def run():
            try:
                n = renamer.apply(self.cfg, system, target, roms_dir, decisions,
                                  log=lambda m: self.after(0, self._rn_logline, m))
                self.after(0, lambda: (
                    self.lbl_rn_status.configure(text=f"renamed {n} file(s)"),
                    self._rn_scan()))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: messagebox.showerror("Rename", m))

        threading.Thread(target=run, daemon=True).start()

    # ---------- Ask AI tab (RAG chat) ----------
    def _build_ask(self, tab):
        pad = {"padx": 16}
        ttk.Label(tab, text="Ask AI", style="H.TLabel").pack(anchor="w", pady=(14, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Chat about your collection using RAG: every question retrieves "
            "the most relevant indexed facts (game metadata from enrichment, "
            "art additions, renames, reverts) via hybrid Elasticsearch + "
            "vector search, and Claude answers from them with [n] citations. "
            "Run the Systems tab at least once so there is data to search. "
            "Requires the API key from Setup; per-question cost is shown.")
        ).pack(anchor="w", pady=(0, 6), **pad)

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, **pad)
        self.txt_chat = tk.Text(wrap, bg="#fafafa", fg=FG, relief="flat",
                                wrap="word", font=("Segoe UI", 10),
                                state="disabled", padx=8, pady=8,
                                highlightthickness=1,
                                highlightbackground="#dddddd")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_chat.yview)
        self.txt_chat.configure(yscrollcommand=sb.set)
        self.txt_chat.tag_configure("you", foreground=ACCENT,
                                    font=("Segoe UI", 10, "bold"))
        self.txt_chat.tag_configure("ai", foreground=FG)
        self.txt_chat.tag_configure("meta", foreground=SUBTLE,
                                    font=("Segoe UI", 8))
        self.txt_chat.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(8, 2), **pad)
        self.var_q = tk.StringVar()
        self.ent_q = ttk.Entry(row, textvariable=self.var_q)
        self.ent_q.pack(side="left", fill="x", expand=True, ipady=4)
        self.ent_q.bind("<Return>", lambda e: self._ask_send())
        self.btn_ask = ttk.Button(row, text="Ask", command=self._ask_send)
        self.btn_ask.pack(side="left", padx=(8, 0))
        self.lbl_ask_status = ttk.Label(tab, text="", style="Status.TLabel",
                                        wraplength=500, justify="left")
        self.lbl_ask_status.pack(anchor="w", pady=(2, 10), **pad)
        self._ask_history = []
        self._ask_busy = False

    def _chat_append(self, text, tag):
        self.txt_chat.configure(state="normal")
        self.txt_chat.insert("end", text, tag)
        self.txt_chat.see("end")
        self.txt_chat.configure(state="disabled")

    def _ask_send(self):
        if self._ask_busy:
            return
        question = self.var_q.get().strip()
        if not question:
            return
        if not secrets.is_stored():
            messagebox.showwarning("Ask AI", "Add your Claude API key in "
                                   "Setup first.")
            return
        self.var_q.set("")
        self._ask_busy = True
        self.btn_ask.configure(state="disabled")
        self._chat_append(f"You: {question}\n", "you")
        self.lbl_ask_status.configure(text="Retrieving + asking Claude…",
                                      foreground=SUBTLE)
        cfg = dict(self.cfg)
        history = list(self._ask_history)

        def run():
            try:
                from clinic import rag
                answer, chunks, es_ok, cost = rag.ask(cfg, question,
                                                      history=history)
                def done():
                    self._chat_append(f"AI: {answer}\n\n", "ai")
                    srcs = ", ".join(sorted({c["meta"].get("source", "?")
                                             for c in chunks})) or "none"
                    bits = [f"{len(chunks)} chunk(s) retrieved",
                            "ES+vector" if es_ok else
                            "vector only (Elasticsearch off)"]
                    if cost is not None:
                        bits.append(f"cost ≈ ${cost:.4f}")
                    self._chat_append("    [" + " | ".join(bits)
                                      + f" | sources: {srcs}]\n\n", "meta")
                    self.lbl_ask_status.configure(text="")
                    self._ask_history.append(
                        {"role": "user", "content": question})
                    self._ask_history.append(
                        {"role": "assistant", "content": answer})
                    del self._ask_history[:-12]   # keep the last 6 turns
                self.after(0, done)
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: (
                    self._chat_append(f"AI: (error) {m}\n\n", "meta"),
                    self.lbl_ask_status.configure(text="Failed - see chat.",
                                                  foreground=BAD)))
            finally:
                self.after(0, lambda: (self.btn_ask.configure(state="normal"),
                                       setattr(self, "_ask_busy", False)))

        threading.Thread(target=run, daemon=True).start()

    # ---------- Revert tab ----------
    def _build_revert(self, tab):
        from clinic import reverter
        pad = {"padx": 16}
        ttk.Label(tab, text="Revert", style="H.TLabel").pack(anchor="w", pady=(14, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "Every system where the Clinic made changes, reconstructed from "
            "the backups and the database logs. Pick what to bring back to "
            "its original version, checkmark the systems, and Revert.")
        ).pack(anchor="w", pady=(0, 8), **pad)

        catrow = ttk.Frame(tab)
        catrow.pack(fill="x", pady=(0, 4), **pad)
        ttk.Label(catrow, text="Revert what:", style="Sub.TLabel").pack(anchor="w")
        self.rv_cats = {}
        grid = ttk.Frame(tab)
        grid.pack(fill="x", **pad)
        for i, (key, label) in enumerate(reverter.CATEGORIES.items()):
            var = tk.BooleanVar(value=False)
            self.rv_cats[key] = var
            ttk.Checkbutton(grid, text=label, variable=var,
                            command=self._rv_refresh).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 12), pady=1)

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(8, 2), **pad)
        ttk.Button(bar, text="↻ Reload", command=self._rv_refresh).pack(side="left")
        ttk.Button(bar, text="All", command=lambda: self._rv_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="None", command=lambda: self._rv_all(False)).pack(side="left", padx=(4, 0))
        ttk.Label(bar, text="Sort:", style="Sub.TLabel").pack(side="left", padx=(12, 2))
        self.var_rv_sort = tk.StringVar(value="Name A→Z")
        cmb_rv = ttk.Combobox(bar, textvariable=self.var_rv_sort, state="readonly",
                              width=12, values=("Name A→Z", "Name Z→A",
                                                "Changes asc", "Changes desc"))
        cmb_rv.pack(side="left")
        cmb_rv.bind("<<ComboboxSelected>>", lambda e: self._rv_refresh())
        self.lbl_rv_count = ttk.Label(bar, text="", style="Sub.TLabel")
        self.lbl_rv_count.pack(side="right")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="x", **pad)
        self.rv_canvas = tk.Canvas(wrap, height=170, bg=BG, highlightthickness=1,
                                   highlightbackground="#dddddd")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.rv_canvas.yview)
        self.rv_inner = ttk.Frame(self.rv_canvas)
        self.rv_inner.bind("<Configure>", lambda e: self.rv_canvas.configure(
            scrollregion=self.rv_canvas.bbox("all")))
        self.rv_canvas.create_window((0, 0), window=self.rv_inner, anchor="nw")
        self.rv_canvas.configure(yscrollcommand=sb.set)
        self.rv_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.rv_vars = {}

        run = ttk.Frame(tab)
        run.pack(fill="x", pady=(8, 2), **pad)
        self.btn_revert = ttk.Button(run, text="↩ Revert selected", command=self._rv_apply)
        self.btn_revert.pack(side="left")
        self.lbl_rv_status = ttk.Label(run, text="", style="Sub.TLabel")
        self.lbl_rv_status.pack(side="right")

        self.txt_rv_log = scrolled_log(
            tab, {"fill": "both", "expand": True, "pady": (6, 12), **pad},
            height=8, font=("Consolas", 9), wrap="word")

    def _rv_logline(self, msg):
        self.txt_rv_log.configure(state="normal")
        self.txt_rv_log.insert("end", msg + "\n")
        self.txt_rv_log.see("end")
        self.txt_rv_log.configure(state="disabled")

    def _rv_all(self, value):
        for v in self.rv_vars.values():
            v.set(value)

    def _rv_refresh(self):
        from clinic import reverter
        prev = {s: v.get() for s, v in self.rv_vars.items()}   # keep ticks across re-sorts
        for w in self.rv_inner.winfo_children():
            w.destroy()
        self.rv_vars = {}
        cats = {k for k, v in self.rv_cats.items() if v.get()}
        summary = reverter.changes_summary(self.cfg)
        entries = []
        for system in summary:
            sys_cats = summary[system]
            relevant = {c: n for c, n in sys_cats.items()
                        if not cats or c in cats}
            if not relevant:
                continue
            desc = ", ".join(f"{reverter.CATEGORIES.get(c, c)}: {n}"
                             for c, n in sorted(relevant.items()))
            entries.append((system, desc, sum(relevant.values())))
        mode = self.var_rv_sort.get()
        if mode == "Name Z→A":
            entries.sort(key=lambda e: e[0].casefold(), reverse=True)
        elif mode in ("Changes asc", "Changes desc"):
            entries.sort(key=lambda e: (e[2], e[0].casefold()),
                         reverse=(mode == "Changes desc"))
        else:
            entries.sort(key=lambda e: e[0].casefold())
        shown = 0
        for system, desc, _count in entries:
            var = tk.BooleanVar(value=prev.get(system, False))
            self.rv_vars[system] = var
            ttk.Checkbutton(self.rv_inner, text=f"{system}  ({desc})",
                            variable=var).pack(anchor="w", padx=8, pady=1)
            shown += 1
        if not shown:
            ttk.Label(self.rv_inner, style="Sub.TLabel", text=(
                "No recorded changes" + (" for the selected categories." if cats
                                         else " yet."))).pack(anchor="w", padx=8, pady=8)
        self.lbl_rv_count.configure(text=f"{shown} system(s) with changes")

    def _rv_apply(self):
        from clinic import reverter
        systems = [s for s, v in self.rv_vars.items() if v.get()]
        cats = [k for k, v in self.rv_cats.items() if v.get()]
        if not systems or not cats:
            messagebox.showinfo(
                "Revert", "Pick at least one category AND checkmark at least "
                "one system to revert.")
            return
        labels = ", ".join(reverter.CATEGORIES[c] for c in cats)
        if not messagebox.askyesno(
                "Revert", f"Bring back the ORIGINAL versions?\n\n"
                f"Systems: {', '.join(systems)}\nWhat: {labels}"):
            return
        self.btn_revert.configure(state="disabled")
        self.lbl_rv_status.configure(text="reverting…")

        def run():
            try:
                n = reverter.revert(self.cfg, systems, cats,
                                    log=lambda m: self.after(0, self._rv_logline, m))
                self.after(0, lambda: (
                    self.lbl_rv_status.configure(text=f"reverted {n} change(s)"),
                    self._rv_refresh()))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: messagebox.showerror("Revert", m))
            finally:
                self.after(0, lambda: self.btn_revert.configure(state="normal"))

        threading.Thread(target=run, daemon=True).start()

    def _browse_rl(self):
        from clinic import rocketlauncher as rl_mod
        chosen = filedialog.askdirectory(title="Select your RocketLauncher folder")
        if not chosen:
            return
        self.var_rl.set(chosen.replace("/", "\\"))
        self._refresh_rl_status()

    def _refresh_rl_status(self):
        from clinic import rocketlauncher as rl_mod
        root = self.var_rl.get().strip()
        if not root:
            # a cab keeps RocketLauncher inside the HyperSpin folder -
            # pick it up automatically, no browsing needed
            auto = rl_mod.effective_root(self.cfg)
            if auto:
                info = rl_mod.inspect(auto)
                self.lbl_rl_status.configure(
                    text=f"✓ Auto-detected under the HyperSpin folder "
                         f"({auto}) - rom paths for {info['systems']} "
                         f"system(s).",
                    foreground=OK)
                return
            self.lbl_rl_status.configure(
                text="Not set - rom analysis and rom renaming disabled.",
                foreground=SUBTLE)
            return
        info = rl_mod.inspect(root)
        if not info["valid"]:
            self.lbl_rl_status.configure(
                text="✗ No Settings folder found under this path.",
                foreground=BAD)
        else:
            self.cfg["rocketlauncher_root"] = root
            config.save(self.cfg)
            self.lbl_rl_status.configure(
                text=f"✓ RocketLauncher found - rom paths configured for "
                     f"{info['systems']} system(s).",
                foreground=OK)

    # ---------- Themes tab (Theme Suite v3.7) ----------
    def _build_themes(self, tab):
        pad = {"padx": 16}
        ttk.Label(tab, text="Themes", style="H.TLabel").pack(anchor="w", pady=(14, 2), **pad)
        ttk.Label(tab, style="Sub.TLabel", wraplength=500, justify="left", text=(
            "HyperSpin Theme Suite v3.7, driven from the global system list - "
            "nothing is copied into your folders. Convert = 4:3 themes to "
            "16:9 (backup to Themes_backup). Record = render themes to "
            "1920x1080 videos. Install = replace themes with the recordings "
            "(backups to *_pre_videotheme). Themes are NEVER converted to "
            "mp4 unless Record/Install are ticked. All revertible from the "
            "Revert tab.")
        ).pack(anchor="w", pady=(0, 6), **pad)

        from clinic import themesuite as _ts
        self.themes_list = SystemListPanel(
            tab, height=170, preselect=False,
            get_cfg=lambda: self.cfg,
            stats_fn=_ts.stats_line).bind_root(
            lambda: self.cfg.get("hyperspin_root", ""))

        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=(6, 2), **pad)
        ttk.Label(opts, text="Steps: ", style="Sub.TLabel").pack(side="left")
        self.ts_ops = {
            "convert": tk.BooleanVar(value=True),
            "record": tk.BooleanVar(value=False),
            "install": tk.BooleanVar(value=False),
        }
        ttk.Checkbutton(opts, text="Convert 16:9", variable=self.ts_ops["convert"]).pack(side="left")
        ttk.Checkbutton(opts, text="Record videos", variable=self.ts_ops["record"]).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(opts, text="Install videos", variable=self.ts_ops["install"]).pack(side="left", padx=(6, 0))

        run = ttk.Frame(tab)
        run.pack(fill="x", pady=(6, 2), **pad)
        self.btn_ts_start = ttk.Button(run, text="▶ Run suite", command=self._ts_start)
        self.btn_ts_start.pack(side="left")
        self.btn_ts_stop = ttk.Button(run, text="■ Stop", state="disabled",
                                      command=lambda: setattr(self, "_ts_stop", True))
        self.btn_ts_stop.pack(side="left", padx=(8, 0))
        self.pb_ts = ttk.Progressbar(run, mode="determinate", length=140)
        self.pb_ts.pack(side="right")
        self.lbl_ts_run = ttk.Label(tab, text="", style="Status.TLabel",
                                    wraplength=500, justify="left")
        self.lbl_ts_run.pack(anchor="w", **pad)
        filerow = ttk.Frame(tab)
        filerow.pack(fill="x", pady=(2, 0), **pad)
        self.pb_ts_file = ttk.Progressbar(filerow, mode="determinate", length=200)
        self.pb_ts_file.pack(side="left")
        self.lbl_ts_file = ttk.Label(filerow, text="", style="Sub.TLabel")
        self.lbl_ts_file.pack(side="left", padx=(8, 0))

        self.txt_ts_log = scrolled_log(
            tab, {"fill": "both", "expand": True, "pady": (6, 12), **pad},
            height=11, font=("Consolas", 9), wrap="none")
        self._ts_stop = False

    def _ts_log(self, msg):
        def put():
            self.txt_ts_log.configure(state="normal")
            self.txt_ts_log.insert("end", msg + chr(10))
            self.txt_ts_log.see("end")
            self.txt_ts_log.configure(state="disabled")
        self.after(0, put)

    def _ts_start(self):
        from clinic import themesuite
        selected = self.themes_list.selected()
        if not selected:
            messagebox.showinfo("Themes", "Check at least one system.")
            return
        # the suite ships with the app and is auto-located at startup
        self.cfg["theme_suite_root"] = config.auto_theme_suite()
        if not themesuite.looks_valid(self.cfg.get("theme_suite_root", "")):
            messagebox.showwarning(
                "Themes", "The bundled theme_suite folder is missing next "
                "to the application - reinstall the release package.")
            return
        steps = [k for k in ("convert", "record", "install")
                 if self.ts_ops[k].get()]
        if not steps:
            messagebox.showinfo("Themes", "Pick at least one step.")
            return
        if "install" in steps and not messagebox.askyesno(
                "Themes", "Install replaces theme zips with video-theme "
                "recordings (originals are backed up and revertible). "
                "Continue?"):
            return
        self._ts_stop = False
        self.btn_ts_start.configure(state="disabled")
        self.btn_ts_stop.configure(state="normal")
        cfg = dict(self.cfg)   # snapshot: Setup edits must not redirect a run
        n_total = len(selected) * len(steps)
        self.pb_ts.configure(maximum=n_total, value=0)
        runners = {"convert": themesuite.convert_system,
                   "record": themesuite.record_system,
                   "install": themesuite.install_system}

        def status(done, msg):
            self.after(0, lambda: (
                self.pb_ts.configure(value=done),
                self.lbl_ts_run.configure(
                    text=f"{round(done / n_total * 100)}% - {msg}")))

        def file_progress(i, total, name, eta):
            if eta is not None and eta >= 1:
                m, s = divmod(int(eta), 60)
                left = f" — ~{m}:{s:02d} left" if m else f" — ~{s}s left"
            else:
                left = ""
            self.after(0, lambda: (
                self.pb_ts_file.configure(maximum=max(total, 1), value=i),
                self.lbl_ts_file.configure(
                    text=f"{i}/{total} files — {name}{left}")))

        def reset_file_bar():
            self.after(0, lambda: (
                self.pb_ts_file.configure(maximum=1, value=0),
                self.lbl_ts_file.configure(text="")))

        def run():
            done = 0
            try:
                for s_name in selected:
                    for step in steps:
                        if self._ts_stop:
                            self._ts_log("- stopped by user -")
                            return
                        status(done, f"{s_name}: {step}")
                        reset_file_bar()
                        try:
                            runners[step](cfg, s_name, self._ts_log,
                                          lambda: self._ts_stop,
                                          progress=file_progress)
                        except Exception as e:
                            self._ts_log(f"[{s_name}] {step} ERROR: {e}")
                        done += 1
                        status(done, f"{s_name}: {step} done")
                self._ts_log(f"=== suite finished ({done}/{n_total} steps) ===")
            finally:
                self.after(0, lambda: (self.btn_ts_start.configure(state="normal"),
                                       self.btn_ts_stop.configure(state="disabled")))

        threading.Thread(target=run, daemon=True).start()

    # ---------- actions ----------
    def _browse(self):
        chosen = filedialog.askdirectory(
            title="Select your HyperSpin folder (contains Media + Databases)")
        if chosen:
            self.var_root.set(chosen.replace("/", "\\"))
            self._apply_root()

    def _apply_root(self):
        """Validate + save automatically - no Save button. A pick of the
        Media folder itself is corrected to the HyperSpin root above it."""
        root = self.var_root.get().strip()
        if root and os.path.basename(root.rstrip("\\/")).lower() == "media":
            parent = os.path.dirname(root.rstrip("\\/"))
            if os.path.isdir(os.path.join(parent, "Databases")) or \
                    os.path.isdir(os.path.join(parent, "Media")):
                root = parent
                self.var_root.set(root)
        self._refresh_root_status()
        if root and config.inspect_hyperspin(root)["valid"] and \
                root != self.cfg.get("hyperspin_root", ""):
            self.cfg["hyperspin_root"] = root
            config.save(self.cfg)
            # RocketLauncher may live inside the newly picked folder
            if hasattr(self, "lbl_rl_status"):
                self._refresh_rl_status()

    def _refresh_root_status(self, initial=False):
        root = self.var_root.get().strip()
        self.tree.delete(*self.tree.get_children())
        if not root:
            self.lbl_root_status.configure(
                text="No folder selected yet.", foreground=SUBTLE)
            return
        info = config.inspect_hyperspin(root)
        if not info["valid"]:
            self.lbl_root_status.configure(
                text="✗ " + (info.get("reason") or "No HyperSpin systems "
                     "found - select the HyperSpin folder that contains "
                     "Media and Databases."),
                foreground=BAD)
            return
        n_sys = len(info["systems"])
        n_games = sum(s.get("games", 0) for s in info["systems"])
        self.lbl_root_status.configure(
            text=f"✓ Found {n_sys} system(s), {n_games} game(s). Saved.",
            foreground=OK)
        for s_info in info["systems"]:
            self.tree.insert("", "end", text=s_info["name"],
                             values=(s_info.get("games", 0),))

    def _add_key(self):
        key = self.var_key.get()
        try:
            secrets.store(key)
        except ValueError as e:
            messagebox.showwarning("Claude API key", str(e))
            return
        self.var_key.set("")             # clear the field immediately
        self._refresh_key_status()
        messagebox.showinfo("Claude API key",
                            "Key stored securely in the Windows Credential Manager.")

    def _remove_key(self):
        if secrets.is_stored() and messagebox.askyesno(
                "Claude API key", "Remove the stored API key?"):
            secrets.clear()
        self._refresh_key_status()

    def _test_key(self):
        self.lbl_key_status.configure(text="Testing…", foreground=SUBTLE)

        def run():
            ok, msg = secrets.test_key()
            self.after(0, lambda: self.lbl_key_status.configure(
                text=("✓ " if ok else "✗ ") + msg, foreground=OK if ok else BAD))

        threading.Thread(target=run, daemon=True).start()

    def _refresh_key_status(self):
        if secrets.is_stored():
            self.lbl_key_status.configure(
                text="✓ A key is stored (encrypted).", foreground=OK)
        else:
            self.lbl_key_status.configure(
                text="No key stored.", foreground=SUBTLE)

    # ---------- AI model & costs ----------
    def _save_model(self):
        self.cfg["model"] = self.var_model.get().strip()
        config.save(self.cfg)
        self._update_model_cost_label()

    def _update_model_cost_label(self):
        from clinic import costs
        model = self.var_model.get().strip()
        spend = costs.load_spend()
        self.lbl_model_cost.configure(
            foreground=SUBTLE,
            text=f"{model}: {costs.price_label(model)}   |   spent via this "
                 f"app ≈ ${spend['total_usd']:.2f} over {spend['calls']} "
                 f"call(s)")

    def _refresh_models(self):
        """Pull the live model list from the API (needs a stored key)."""
        from clinic import costs
        key = secrets.load()
        if not key:
            messagebox.showinfo("Models", "Add your Claude API key first — "
                                "the model list comes from the API.")
            return

        def run():
            models, live = costs.list_models(key)
            def apply():
                self.cmb_model.configure(values=models)
                if live:
                    self.lbl_model_cost.configure(
                        text=f"{len(models)} model(s) loaded from the API.",
                        foreground=SUBTLE)
                else:
                    self.lbl_model_cost.configure(
                        text="Could not reach the API - showing the "
                             "built-in list. Check the key / connection.",
                        foreground=BAD)
            self.after(0, apply)

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    ClinicApp().mainloop()
