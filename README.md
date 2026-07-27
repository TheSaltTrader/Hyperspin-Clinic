# HyperSpin Clinic

**v1.2.0** — Setup streamlined: HyperSpin folder saves automatically
(no Save button), the system table shows game counts, the Theme Suite
ships with the app and is auto-located (no Setup field), system lists
gained a horizontal scrollbar, RocketLauncher inis are read
encoding-tolerantly (UTF-16), and zero-counts no longer clutter the
analysis line. (v1.1.0: Ask AI RAG chat tab, Theme Suite live progress,
security audit round.)

A desktop companion for maintaining a HyperSpin arcade setup: a vertical,
white-themed window with tabs along the top. Under the hood: hybrid search
(Elasticsearch + ChromaDB vector store), RAG, and optional Claude AI via a
user-supplied API key.

> ## 🔴 **IMPORTANT — AN ANTHROPIC (CLAUDE) API KEY IS REQUIRED FOR ALL AI FEATURES** 🔴
>
> **THE SYSTEMS (METADATA ENRICHMENT) TAB WILL NOT WORK WITHOUT AN API KEY.**
> Get one here: **<https://console.anthropic.com/settings/keys>** (create an
> Anthropic account, open *API Keys*, create a key — it starts with
> `sk-ant-`), then paste it in the **Setup** tab. The key is stored
> encrypted on your machine and API usage is billed to your Anthropic
> account. Search, Missing Art and Rename work without a key.

## Run

From source:
```
pip install -r requirements.txt
python main.py
```

As a portable app: run `build_exe.bat` once (PyInstaller) — the result is
a **folder** `dist\HyperSpinClinic\` containing `HyperSpinClinic.exe`
plus all its tools and libraries. Copy the whole folder to any machine
and double-click the exe. The folder layout starts in seconds (a
single-file exe has to unpack ~200 MB to a temp folder on *every*
launch, which took 20+ seconds); settings, logs and the vector database
are created inside a `data\` folder next to the exe.

### Windows support
- **Windows 8.1 / 10 / 11 and beyond**: supported by the standard build.
- **Windows 7**: modern Python (3.9+) and several dependencies dropped
  Win7 support upstream. A Win7-compatible exe requires building with
  Python 3.8 and pinned legacy dependency versions
  (`elasticsearch<8.7`, `chromadb<0.4.15`, `anthropic<0.30`,
  `pillow<10`, `keyring<24`) — same `build_exe.bat` procedure from a
  Python 3.8 environment. Functionality is identical; this is purely a
  toolchain constraint, flagged here honestly rather than promised away.

## Tabs

### Setup (implemented)
The tab's sole goal is configuring the application:
- **HyperSpin folder** — select your *actual* HyperSpin folder (the one
  containing `Media` and `Databases`; all system art lives under
  `Media\<System>\`). The pick is validated and **saved automatically** —
  there is no Save button — and the table lists each detected system
  with its **game count**. Picking `Media` by mistake is auto-corrected
  to its parent.
- **Claude API key** — a masked field + *Add key* button. Security model
  (OWASP secrets-management practice):
  - the key is stored **encrypted in the Windows Credential Manager**
    (DPAPI, per-user) via `keyring` — it is *never* written to
    `settings.json` or any project file;
  - masked input, cleared from the field immediately after storing,
    never logged;
  - format validation before storing; *Test key* performs one explicit,
    user-triggered minimal API call to verify it;
  - *Remove* deletes it from the credential store.

- **Theme Suite** — nothing to configure: the suite **ships with the
  application** (`theme_suite` folder next to the exe) and is located
  automatically at startup. It powers the **Themes** tab and runs in
  place — nothing is ever copied into your HyperSpin installation.
- **AI model & costs** — pick which Claude model does the work from a
  list (pulled live from the API once a key is stored). The **default is
  deliberately the cheapest current model** (Haiku), never the most
  expensive. Per-model pricing is displayed; every API call's dollar
  cost (from its real token usage) is printed in the enrichment log,
  with an app-lifetime running total. The API does not expose remaining
  credit to normal keys — the *Billing…* button opens the Anthropic
  console page where the balance lives.
- **RocketLauncher folder** — rom locations are read from
  RocketLauncher's own per-system settings
  (`Settings\<System>\Emulators.ini` → `Rom_Path`, honoring
  `Rom_Extension` and multiple `|`-separated paths). This centralizes rom
  locations: the Rename tab's rom mode uses these paths directly (its
  own rom browser was removed). Missing roms are not listed on any tab.

`settings.json` holds only non-secret configuration (folder path, engine
URLs, model choice).

### Systems (implemented)
The fun stuff. The tab reads every game system from
`Databases\Main Menu\Main Menu.xml` (legacy `Databases\Main\Main.xml`
also recognized) and lists them with checkboxes — **all pre-selected**.
The main menu database is the **single source of truth on every tab**:
a system folder that exists on disk but is not listed there is never
shown or touched. Press **Start enrichment** and, for each checked system:

1. `Databases\<System>\<System>.xml` is parsed (tolerantly — HyperSpin
   XMLs are often sloppy; unescaped `&`, self-closing `<game/>` tags and
   odd formatting are all handled).
2. Games missing any of **published year / manufacturer / genre** are sent
   to Claude in batches of 40; the model identifies each game from its rom
   name + description and returns structured JSON. Unidentifiable games
   are left blank — never wild guesses.
3. The system XML is updated **surgically** (per-tag edits that preserve
   the file's formatting; a timestamped backup goes to
   `Databases\<System>\clinic_backups\` before any write). By default only
   empty fields are filled — existing values are never overwritten
   (untick the option to allow overwrites).
4. Every game is then upserted into the **vector database** and
   **Elasticsearch**, keyed by system name — the searchable, AI-updatable
   store for the tabs to come.

Progress log, per-system progress bar, and a Stop button (finishes the
current batch, then halts cleanly).

### Missing Art (implemented)
Same pre-selected system list. For each checked system it finds **missing
wheel art** and **missing videos** (games from the system database vs.
`Media\<System>\Images\Wheel` and `Media\<System>\Video`), then works
through the sources in the knowledge base's order:

1. **Local first** — the system's own folders under case / punctuation /
   region-title aliases (fuzzy-normalized names, roman numerals folded).
   Alias files are **copied** to the canonical rom name — existing art is
   never renamed or swapped.
2. **EmuMovies FTP** — `files.emumovies.com` (plain FTP per the knowledge
   base), using the documented layout: `Official/Video Snaps (HQ|SQ|HD)/…`
   for snaps and the system's `Logos` pack under `Official/Artwork/…` for
   wheels (pack downloaded once, cached). Credentials are entered in
   Setup and stored encrypted in the Credential Manager.
3. **YouTube** (videos only) — `yt-dlp` search fallback when installed.

**Wheel curation** (every downloaded wheel): transparent border trimmed,
horizontal **×0.75 squeeze** (so it displays correctly on the 16:9
stretch), normalized to **400 px wide** to match the HyperSpin art set.

**Tracking**: every addition is written to the live log, appended to
`data\art_additions.log` (timestamp, system, game, art type, source,
path), and upserted into the vector database + Elasticsearch so changes
are queryable later.

### Rename (implemented)
System list with **nothing pre-selected** (All / None buttons, like every
tab). Curation is **one system at a time**: check exactly one system,
choose what to work on — **Videos, Wheel art, or Roms** (rom locations
come from the RocketLauncher folder configured in Setup) — and Scan.

The scan reads the system's database XML and, for every game whose
canonical file is missing, uses **Elasticsearch** (a transient fuzzy
filename index; difflib fallback with a warning when the server is off)
to find the closest loose files. Each match row shows the game, the
proposed source file, and a **similarity percentage**; a **Min %**
threshold controls what gets auto-selected. The highest match is chosen
by default — double-click any row to pick one of the other identified
candidates or skip that game.

**Rename selected** first copies every affected file into
`clinic_backups\<timestamp>\` inside that folder (integrity preserved),
then renames to `<rom name><original ext>`. Every change is appended to
`data\renames.log`, upserted into the vector DB + Elasticsearch, and
shown in the scrollable changes box (this run + full history).

### Themes (implemented)
Integrates the **HyperSpin Theme Suite v3.7**, driven from the same
global system list — the suite stays in its own folder (configured in
Setup) and every operation targets the selected system's media folder
directly. Nothing is copied into your HyperSpin installation.

Under each system, a red analysis line breaks its themes down exactly
the way the converter classifies them — **png** (normal raster themes),
**flash** (all-SWF, untouched), **video** (the video *is* the theme),
**installed** (already replaced by a suite recording) — and shows how
many are **expected to convert to 16:9** versus how many **would become
video themes**. The two conversions are distinct: themes are **never
rendered to mp4 unless Record/Install is explicitly ticked** (both are
off by default). While running you get two progress bars — overall and
per-file (n/total processed, current file, live time estimate) — and
Stop always cancels cleanly, killing the whole process tree.

Three steps, each optional, run in order per checked system:

1. **Convert 16:9** — converts the system's 4:3 themes to widescreen.
   Originals are backed up to `Media\<System>\Themes_backup` first.
   Every converted theme is stamped inside its `Info.txt`
   (`Converted to 16:9 by HyperSpin Theme Suite <date>` — the same
   convention mrfomt's themes use), and **stamped themes are bypassed on
   future runs**, so re-running conversion only touches new themes.
2. **Record videos** — renders every theme to a 1920×1080 video with
   the suite's recorder (Chrome + ffmpeg; the log pane streams the
   recorder's progress live).
3. **Install videos** — replaces theme zips with the recordings as
   video themes; originals go to `Themes_backup_pre_videotheme` and
   `Video_backup_pre_videotheme`. Asks for confirmation first.

Live log, status + percentage, and a Stop button that kills the whole
running process tree cleanly. Everything is revertible.

### Ask AI (implemented)
A chat window over your collection, powered by the RAG stack. Every
question retrieves the most relevant indexed facts — game metadata from
enrichment, tracked art additions, renames, reverts — via **hybrid
Elasticsearch + vector search** (reciprocal-rank fusion; vector-only
fallback when ES is off), and Claude answers from those chunks with
`[n]` citations. A live list of installed systems is always injected so
the model knows your setup even before anything is indexed. Each answer
shows how many chunks were used, their sources, and the question's
dollar cost. The last 6 turns carry over as conversation context.

### Revert (implemented)
Lists every system where the Clinic made changes (rebuilt from backups
and the database logs) with a category filter: XML metadata, added
wheels, added videos, renamed videos / wheel art / roms, **converted
themes**, and **installed video themes**. Checkmark systems + categories
and press Revert:
- added art → the added files are removed (originals were never touched),
- renames → originals restored from backup, renamed copies removed,
- XML → the earliest (pre-change) backup restored,
- converted themes → original 4:3 themes restored from `Themes_backup`,
- installed video themes → theme zips and video snaps restored from the
  `*_pre_videotheme` backups.

Reverts are themselves logged (`data\reverts.log`) and tracked.

### Help
The full documentation, in-app.

### RAG
Retrieval-augmented generation is wired in two places:
- `clinic/rag.py` — hybrid ES+vector retrieval feeding Claude with cited
  chunks (ready for an Ask-AI tab).
- The **Systems** enrichment grounds every Claude batch with retrieved
  already-complete entries from the same database, anchoring genre
  vocabulary and manufacturer spellings to what the collection uses.

## Security & quality (audited)

An independent code audit (bugs / OWASP / performance) was run and all
findings fixed:

- **Data safety**: HyperSpin XMLs are read in their true encoding
  (UTF-8 / cp1252 / latin-1 detected — accented characters round-trip
  losslessly), written **atomically** (temp + replace, crash can never
  truncate a database), always after a timestamped backup. A parser flaw
  that could misattribute metadata across self-closing `<game/>` entries
  was found and fixed with regression tests.
- **OWASP**: secrets only in the Windows Credential Manager (never in
  files, logs, or the DB); EmuMovies connects with **FTPS (TLS) when the
  server supports it**, falling back to plain FTP with an explicit
  warning; all rom-derived filenames are sanitized against path
  traversal and reserved device names; yt-dlp runs without a shell; no
  XML parser touches untrusted input (regex-only); zip contents are read
  in memory, never extracted by member path.
- **Robustness**: every worker failure surfaces as a dialog (a
  late-binding bug used to swallow them), stopping a run still records
  what was already added (the revert guarantee holds), the window close
  signals workers before exit, and buttons are guarded against
  double-launch races.
- **Performance at MAME scale (30k+ games)**: single folder scan instead
  of per-game file checks, set-based matching, cached
  ChromaDB/Elasticsearch clients, and no-op enrichment runs skip the
  full re-embed.

## Backend modules (UI-agnostic, ready)

| Module | Role |
|---|---|
| `clinic/config.py` | settings + HyperSpin folder inspection |
| `clinic/secrets.py` | encrypted API-key storage (Credential Manager) |
| `clinic/ingest.py` | corpus chunker: knowledge docs + theme zips (XML/Info extraction) |
| `clinic/store.py` | ChromaDB vector store (embedded; built-in local embeddings) |
| `clinic/es.py` | Elasticsearch index/query (graceful when server absent) |
| `clinic/search.py` | hybrid retrieval (BM25 + vector, reciprocal-rank fusion) |
| `clinic/rag.py` | Claude RAG call with source citations |

Elasticsearch (optional; vector-only fallback otherwise):
`docker run -d --name es-clinic -p 9200:9200 -e "discovery.type=single-node" -e "xpack.security.enabled=false" docker.elastic.co/elasticsearch/elasticsearch:8.14.0`
