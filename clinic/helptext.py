# The in-app Help tab content (kept in sync with README.md).
WARNING = """AN ANTHROPIC (CLAUDE) API KEY IS REQUIRED FOR ALL AI FEATURES.
THE SYSTEMS (ENRICHMENT) TAB WILL NOT WORK WITHOUT ONE.
How to get one:  https://console.anthropic.com/settings/keys
(create an Anthropic account, open API Keys, create a key starting with
sk-ant-, then paste it in the Setup tab. Usage is billed to your account.)
"""

HELP = """HYPERSPIN CLINIC — HOW EVERYTHING WORKS

━━━ SETUP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• HyperSpin folder: select your ACTUAL HyperSpin folder - the one that
  contains the Media and Databases folders (all system art lives under
  Media\\<System>\\). The pick is validated and saved AUTOMATICALLY (no
  Save button), and the table shows each detected system with its game
  count. Picking the Media folder by mistake is corrected to its parent.
• Claude API key: masked field + Add key. The key is stored ENCRYPTED in
  the Windows Credential Manager (per-user DPAPI) — never in a file,
  never logged, following OWASP secrets-management practice. Test key
  makes one minimal API call to verify; Remove deletes it.
• RocketLauncher folder: rom locations are read from RocketLauncher's
  per-system settings (Settings\\<System>\\Emulators.ini, Rom_Path,
  honoring Rom_Extension and multiple | separated paths). Powers the
  rom renaming on the Rename tab - one
  centralized place, no per-tab rom browsing.
• EmuMovies account: username + password for files.emumovies.com (plain
  FTP per the knowledge base). Stored encrypted the same way. Used by
  the Missing Art tab.
• YouTube sign-in: YouTube periodically BLOCKS anonymous downloads for
  a while (HTTP 403 / "sign in to confirm you're not a bot") - instead
  of waiting the block out, give yt-dlp your YouTube sign-in cookies:
   - Browser: log in to youtube.com in that browser, then pick it here.
     yt-dlp reads the cookies straight from the browser's profile.
     FIREFOX IS THE MOST RELIABLE on Windows - Chrome/Edge encrypt
     their cookie store and may need the browser fully closed (all
     background processes) before yt-dlp can read it.
   - cookies.txt: export your youtube.com cookies with a browser
     extension such as "Get cookies.txt LOCALLY" (Netscape format) and
     select the file. When both are set, the FILE wins. The export must
     be made WHILE SIGNED IN to YouTube: a file without youtube.com
     cookies is ignored (it would make every download fail).
  The cookies are only ever read locally by yt-dlp on your machine;
  this app stores just the browser choice / file path in settings.json,
  never the cookies themselves. Treat an exported cookies.txt like a
  password: anyone with that file can act as your YouTube account.
  Clear resets both fields - downloads then run anonymously again.
• Theme Suite: nothing to configure - the suite SHIPS WITH the app
  (theme_suite folder next to the exe) and is located automatically at
  startup. It powers the Themes tab and runs in place, never copied
  into your HyperSpin installation.
• AI model & costs: choose which Claude model does the enrichment work.
  The DEFAULT IS THE CHEAPEST current model (Haiku) on purpose - pick a
  bigger one only if you want it. "Refresh list" pulls the live model
  list from the API once a key is stored; pricing per model is shown.
  Every API call's dollar cost (computed from the call's real token
  usage) appears in the enrichment log, plus a lifetime running total.
  NOTE: the API does not expose your remaining credit to normal keys -
  the "Billing…" button opens the Anthropic console where it lives.

━━━ SYSTEMS (metadata enrichment) ━━━━━━━━━━━━
Reads every system from Databases\\Main Menu\\Main Menu.xml — NOTHING
is pre-selected, on this or any tab, so an accidental Start can never
run on the whole collection; tick what you want (or press All).
Every tab's system list has a SORT selector: by system name (A-Z is
the default, or Z-A) or by the analysis line's missing count,
ascending or descending — sort "Missing desc" to see the systems
needing the most work first. Re-sorting never touches your ticks.
(Legacy Databases\\Main\\Main.xml also recognized.) The
main menu database is the SINGLE source of truth on every tab: a
system folder that exists on disk but is not listed there is never
touched.
For each checked system, Start enrichment:
 1. parses Databases\\<System>\\<System>.xml (tolerant of sloppy XML),
 2. sends games missing year / manufacturer / genre to Claude in
    batches of 40 (RAG-grounded with already-complete entries from the
    same database so vocabulary stays consistent) — each batch's dollar
    cost is printed in the log from its real token usage,
 3. updates the XML surgically — formatting preserved, timestamped
    backup in Databases\\<System>\\clinic_backups\\ first, and by
    default only EMPTY fields are filled (existing values are never
    overwritten),
 4. indexes every game into the vector database and Elasticsearch under
    the system name.
Games the model cannot identify from its own knowledge (typically
recent 2024+ releases past its training data) are automatically
retried in a WEB-SEARCH pass: the AI looks each one up online (one
search per game) and verifies year/publisher/genre before answering.
This is the checkbox "Web-search games the AI can't identify" —
searches cost about $0.01 each on top of tokens (shown in the log per
batch); untick it to skip the pass.
COST CONTROL — every answer is remembered in data\\enrich_cache.json:
 - identified games are reused FREE forever, across runs AND across
   systems (a Favorites wheel that duplicates other wheels' games
   costs nothing for those), keyed by the game's description;
 - clones/duplicates inside a system (same description) are answered
   ONCE and the result applied to every copy;
 - games that stayed unknown even after web search are skipped on
   later runs instead of being re-billed — tick "Retry games that
   previously failed" to deliberately search them again.
The log states exactly how many games were filled from cache, shared
a clone answer, or were skipped as previously-failed.
Status bar + percentage show what is happening at every step; Stop
halts cleanly between batches.

━━━ MISSING ART ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Finds missing wheel art and videos for the systems you tick (nothing
is pre-selected). Before a VIDEO counts as missing, the parent MAME
system's folder is checked too — subset wheels use it as a fallback,
so a video available there is not missing and is never re-downloaded
(the log shows "N covered by the MAME folder fallback"). CLONES count
too: a clone's video IS its parent rom's video (<cloneof> in the XML),
so an available parent video means the clone is not missing.
Sources are tried in the knowledge base's order:
 1. LOCAL FIRST — alias-named files already in the system's own folders
    (case / punctuation / region variants, roman numerals). For videos
    the search EXTENDS to the parent MAME folder, including the clone →
    parent-rom link: a match found there is COPIED into this system's
    folder under this system's rom name. Aliases are always COPIED to
    the canonical rom name; nothing is ever renamed or swapped here.
 2. EMUMOVIES — video snaps and the system's Logos pack for wheels
    (downloaded once, cached). Quality is HIGHEST-FIRST per game:
    HD → HQ → SQ (HD sets are often works-in-progress, so a game
    missing there automatically falls back to HQ/SQ), then the Video
    Themes trees — some systems (NesicaXlive) exist ONLY there.
    System names are matched through a ladder: your trained map →
    vendor-tolerant name matching with known aliases (Capcom Play
    System II = Capcom CPS-2) → semantic search of the indexed folder
    tree. Every resolved location is REMEMBERED in
    data\\emumovies_map.json (hand-editable to train the software).
    The FULL CATALOG — every folder AND every file in it — is
    extracted to data\\emumovies_catalog.json (weekly, or the
    "EmuMovies catalog" button), indexed per file into Elasticsearch
    and summarized in the vector DB, so lookups are direct catalog
    queries instead of runtime guessing; FTP is only used for the
    actual downloads. Region renames are handled (Jet Set Radio ↔
    Jet Grind Radio). If YouTube starts refusing downloads, an
    escalating COOLDOWN (5→90 min, Stop stays responsive) waits the
    block out and retries automatically.
 3. LAUNCHBOX (wheels only) — transparent Clear Logos from the
    LaunchBox Games Database (free, no account). Fills what EmuMovies
    packs don't cover (MAME has no Logos pack). Every download is
    decode-verified and must be genuinely transparent, then curated
    like every wheel.
 4. YOUTUBE — videos only, via yt-dlp when installed. The search is
    ALWAYS "<game> <system>", and a candidate's title must contain
    BOTH the game name AND the system (user rule). Reviews, reactions
    and commentary-over-gameplay are rejected — only original
    gameplay/game music — and known clean longplay channels are
    preferred; trailers/teasers are rejected and EVERY download skips
    at least the first minute so snaps never open on a title screen.
    The result is then processed with FFMPEG (bundled with the Theme
    Suite): black side bars cropped; the target shape comes from the
    ASPECT KNOWLEDGE BASE (data\\aspect_db.json, hand-editable) — 4:3
    systems (everything before ~2000) get a TRUE 4:3 (bars cropped, or
    a stretched widescreen source resized back), widescreen platforms
    (PS3/360/PSP/PC) stay 16:9, and MIXED post-2000 arcade systems
    (NesicaXlive, Taito Type X, Lindbergh…) are decided PER GAME:
    trained entry → pre-2000 year = 4:3 → otherwise the video's own
    bar-cropped content decides, and the verdict is saved back per
    game so the database learns. Vertical arcade content keeps its
    native shape. Cut to a 60-second snap with fade in/out, normalized
    to H.264. If downloads fail with "sign in to confirm" or "the page
    needs to be reloaded", UPDATE yt-dlp and/or configure YouTube
    sign-in on the Setup tab.
    YT-DLP UPDATES: at every startup the app checks for a new yt-dlp
    release and ASKS before installing it (into data\\tools\\ - the
    app's own copy, preferred over any system yt-dlp). Stale yt-dlp
    is the #1 cause of YouTube failures; say yes when offered.
The red analysis line under each system shows missing wheels and
videos - so you can see at a glance which system needs the most work.
Every downloaded wheel is curated: transparent border trimmed,
horizontal ×0.75 squeeze (16:9 display convention), resized to 400 px
wide. Every addition is logged (data\\art_additions.log), tracked in
the database, and shown in the log pane with live status/percentage.

━━━ CLEAR EXTRAS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Finds art files that are NOT part of a system's database XML — extra
wheels, videos and themes left behind by removed games. The system
list works like every other tab (nothing pre-selected, sortable by
name or by the number of extra files) and each system's line shows
how many extras exist per art type. Pick the art types (Wheel art /
Videos / Themes), press Delete, and confirm the warning: the selected
art types' files that do not exist in each selected system's XML are
removed, because the system does not use them. In the VIDEO folder,
jpg/png images are also cleaned: an image whose game has a real
mp4/flv is SUPERSEDED and removed (the video is the only needed
source); an image matching no game is removed; an image serving as
the game's only stand-in is kept. NEVER touched: the
Themes default.zip, any folders inside the art-type folders, and —
in the PARENT MAME system's folder only (the largest system, the one
subset wheels fall back to) — any video that another system lists
and does not hold its own copy of. A subset wheel's own folders are
read by nobody else and get normal per-system cleanup. Matching
always ignores caps; a "[folder not found: …]" note in the analysis
line means the art folder's name/path does not match.
Removed files are MOVED to clinic_backups\\orphans_<stamp>\\ inside
the art folder (restorable by hand), logged to data\\cleanup.log and
tracked in the database. A progress bar shows the run and a pop-up
confirms when everything was deleted. The "Restore backups" button
moves the backed-up files of the SELECTED systems (and selected art
types) back into their original folders — files that exist again are
never overwritten. The "Clear backups" button PERMANENTLY deletes
those orphans_* backup folders (red warning first — restoring is
then no longer possible).

━━━ RENAME ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Matches wrongly-named files to database names — ONE system at a time
(check exactly one; nothing is pre-selected on this tab). Choose what
to work on: Videos, Wheel art, or Roms (Browse assigns the roms
folder). Scan uses Elasticsearch fuzzy matching (difflib fallback when
the server is off) and lists every game whose canonical file is
missing, with candidate files and a similarity percentage. Min %
controls what is auto-selected; the highest match is chosen by default
— double-click a row to pick another candidate or skip. Rename selected
first COPIES every affected file to clinic_backups\\<timestamp>\\ in
that folder, then renames. All changes go to data\\renames.log, the
database, and the scrollable changes box (this run + history).

━━━ THEMES (Theme Suite v3.7) ━━━━━━━━━━━━━━━
Integrates the HyperSpin Theme Suite, driven from the same global
system list — the suite ships with the application (theme_suite folder,
auto-located at startup); nothing is copied into your HyperSpin
installation.
The red analysis line under each system breaks down its themes the way
the suite's converter classifies them — png (normal raster themes),
flash (all-SWF, left untouched by the converter), video (the video IS
the theme), installed (already replaced by a suite recording) — and
shows how many themes are EXPECTED to convert to 16:9 versus how many
would become video themes. The two conversions are separate steps:
themes are NEVER rendered to mp4 unless you tick Record/Install.
Three steps, run per checked system in order:
 • Convert 16:9 — converts the system's 4:3 themes to 16:9
   (widescreen). Originals are backed up to Media\\<System>\\
   Themes_backup first. Themes whose video frame is baked into a
   full-canvas Flash artwork (the converter cannot resize compiled
   SWF) convert normally, and the RECORDER re-registers the flash
   bezel: the flash layer is squeezed x0.75 about the video center
   at render time, so the recorded theme shows the game at true 4:3
   with the bezel hugging it and no flash art stretched (the dmnfrnt
   case). XML-drawn borders (bsize/bcolor) always follow the video
   automatically and never need this. Converted themes are stamped in their
   Info.txt ("Converted to 16:9 by HyperSpin Theme Suite", the
   mrfomt convention) and BYPASSED on future runs - re-running
   only processes themes not yet marked 16:9.
 • Record videos — renders every theme to a 1920x1080 video using the
   suite's recorder (Chrome + ffmpeg; can take a while — the log pane
   streams the recorder's progress live).
 • Install videos — replaces theme zips with the recordings as video
   themes; originals go to Themes_backup_pre_videotheme and
   Video_backup_pre_videotheme.
While running you see TWO progress bars: overall (system × step) and
per-file (n/total files processed, current file name, and a live time
estimate computed from the actual pace). Stop is always available and
kills the whole running process tree cleanly. Everything is revertible
from the Restore Backups tab.

━━━ ASK AI (RAG chat) ━━━━━━━━━━━━━━━━━━━━━━━
Chat about your collection. Every question runs retrieval-augmented
generation: the most relevant indexed facts are fetched with hybrid
search (Elasticsearch BM25 + vector similarity, reciprocal-rank
fusion; vector-only when ES is off) and Claude answers FROM those
chunks, citing them as [n]. What is searchable: game metadata indexed
by the Systems tab, plus the tracked art additions, renames and
reverts. A live list of your installed systems is always included.
Run the Systems tab at least once so there is data to search. Needs
the API key from Setup; each answer shows chunks used, sources, and
the question's dollar cost (same tracking as enrichment). The last 6
turns are kept as conversation context.

━━━ RESTORE BACKUPS (revert) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lists every system where the Clinic made changes, rebuilt from backups
and the database logs. Pick the categories to bring back (XML
metadata, added wheels, added videos, renamed videos / wheel art /
roms, converted themes, installed video themes), checkmark the
systems, and Revert:
 • added art  -> the added files are removed (originals were never
   touched),
 • renames    -> the original-named file is restored from its backup
   and the renamed copy removed,
 • XML        -> the earliest (pre-change) backup is restored and the
   consumed backups retired,
 • converted themes  -> original 4:3 themes restored from
   Themes_backup (kept, so this revert is repeatable),
 • installed video themes -> original theme zips and video snaps
   restored from the *_pre_videotheme backups.
Reverts are themselves logged (data\\reverts.log) and tracked.
The "Clear ALL backups" button PERMANENTLY deletes every backup this
tab's reverts rely on (XML backups, rename backups, theme-suite
backups) after a RED-TEXT warning: THIS DELETES THE SYSTEM'S RESTORE
CAPABILITIES — existing changes can no longer be reverted. A progress
bar shows the run and a completion pop-up confirms the result.

━━━ DATA & SAFETY ━━━━━━━━━━━━━━━━━━━━━━━━━━━
• settings.json: non-secret settings only (folders, engine URLs).
• Secrets: Windows Credential Manager, encrypted per user.
• Backups: every destructive step backs up first — XML backups next to
  the database, file backups inside the affected folder.
• Logs: data\\art_additions.log, data\\renames.log, data\\reverts.log,
  plus tracking records in ChromaDB (data\\chroma) and Elasticsearch.
• Elasticsearch is optional: when the server is unreachable the app
  falls back gracefully (vector-only search, difflib matching) and
  says so in the status line.
"""
