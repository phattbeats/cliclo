# ASTOUNDING TALES OF AUTOMATION!
## ★ THE C.L.I.C.L.O. CHANGELOG ★
### *Every Sensational Revision, Chronicled for Posterity!*

---

*Kept in the four-color spirit of [Keep a Changelog](https://keepachangelog.com/). Versions follow [Semantic Versioning](https://semver.org/), more or less, as befits a serial of this caliber. Newest issues up front, as nature intended.*

---

## ISSUE #5⅞ — v5.1.2
### *"THE DRY RUN THAT WASN'T!"*
**On sale 2026-07-01**

> **NARRATOR BOX:** *The audit passed — or so they believed! But lurking beneath the green checkmarks, a preview mode that left FINGERPRINTS! A second sweep, and this time NOTHING escapes!*

A second full-review pass over v5.1.1.

### Fixed
- **`--dry-run` is now actually a dry run.** Previously a dry run still (1) converted CBR files on disk, (2) wrote `success`/`error`/`skipped` rows into the progress database, and (3) updated the review queue — so files "previewed" in a dry run were silently skipped by the next real run, left untagged forever. A dry run now reports every outcome but touches neither your files nor the database, in pass 1 and `--auto-retry` alike.
- **Uppercase extensions are no longer invisible on Linux/macOS.** The scan globbed `*.cbz`/`*.cbr` case-sensitively, so `FILE.CBZ` was skipped entirely on case-sensitive filesystems (Windows globbing is case-insensitive, which masked it). Discovery and `--dedupe` now match extensions case-insensitively.
- **The API key's first six characters no longer land in the log.** The pass-2 banner printed `key abc123…`; everywhere else CLICLO deliberately logs only a last-4 fingerprint. The banner now uses the same fingerprint, so `cliclo.log` never contains a usable key prefix.

### Docs
- `requirements.txt` claimed pure stdlib; Pushover notifications actually need `requests` (they self-disable without it). Documented as an optional install.

### Verified
- `py_compile` + pyflakes + vulture clean (no dead code found this pass — v5.1.1 got it all).
- Behavior checks: case-insensitive discovery finds `.CBZ`/`.CbR`; a dry-run pass leaves the progress DB byte-identical.

---

## ISSUE #5¾ — v5.1.1
### *"THE AUDIT!"*
**On sale 2026-07-01**

> **NARRATOR BOX:** *With the library conquered and the code laid bare before the world, one question remained: could the machine withstand its own scrutiny? A full review, line by line! Dead code, EXCISED! Nineteen trials, ENDURED!*

A maintenance issue: full code review, dead-code removal, and a capability test suite run against every offline-testable subsystem.

### Fixed
- **No more waiting forever on a key that doesn't exist.** With no ComicVine API key configured, the key selector would loop and sleep indefinitely instead of failing. Batch runs (pass 1, `--auto-retry`) now refuse to start with a clear message, and the selector itself fails fast if reached without a key.
- **Rate-limit detection no longer trips on file names.** The signals `"107"` and `"420"` were matched as bare substrings of ComicTagger's raw output — which includes the file path — so a fetch failure on a comic like `Batman 107.cbz` was misreported as a rate limit (and, in multi-key mode, wrongly benched the key). Signals are now specific (`rate limit`, `slow down`, `status_code": 107`, `http 420`).

### Removed (dead code)
- Duplicate function-local `import re` in `_check_version` and `_series_from_filename` (already imported at module scope).
- Unused `resume` parameter on the startup notification (never passed).
- Unused `include_permanent=False` branch of `get_failed_files` (every caller wanted both).

### Verified
- 19-check capability test pass: config scaffolding + env overrides, JSON result extraction, the full outcome-classification matrix, rate-limit signal specificity, series-name parsing, YAML quoting, database upsert/retry/queue/budget/lock semantics, v3.2 → v6 schema migration with backup and `needs_followup` backfill, tolerant CBZ repack (pages-only, deterministic), Pushover credential self-disable and milestone ladder, and the dedupe regex. Plus CLI smoke: `--help`, `--init-config`, `--stats`, and the new no-key guard.

---

## ISSUE #5½ — v5.1.0
### *"THE HUMAN IN THE LOOP!"*
**On sale 2026-05-31**

> **NARRATOR BOX:** *Twenty-seven thousand comics fell before the machine's relentless advance! But three thousand stubborn holdouts, the ambiguous, the mislabeled, the twins separated at birth, laughed at automation alone! For these, our hero needed an ALLY of flesh and judgment! And in the bargain, he discovered the very ground beneath his feet was SHIFTING!*

This issue completes the founding vision: a library is not done when the easy 90% is tagged. It is done when the hard 10% has been dealt with too. CLICLO now runs as three deliberate passes.

### Added
- **The three-pass pipeline.**
  - **Pass 1** (`cliclo /comics`): high-confidence auto-tag. The bulk.
  - **Pass 2** (`--auto-retry`): automated re-match of the leftover queue. Drops the year, searches by series name, but KEEPS the confidence bar. Recovers files that failed pass 1 on a parsing quirk, not a genuine ambiguity.
  - **Pass 3** (`--review`): the human decides. CLICLO hands you ComicTagger's own numbered candidate list per file; you type a number to tag or `s` to skip. You adjudicate conflicts; you do not type metadata. Resumable across sessions.
- **Resuming an existing run's database, by any name.** `--db-path` points CLICLO at a specific progress database. If you start without one but a database from an older run (`cliclo_progress.db`, or v3.2's `comic_tagger_progress.db`) is sitting in the directory, CLICLO refuses to silently start fresh over your backlog and tells you exactly how to resume it. Migrating a v3.2 database also backfills its queued low-confidence files as `needs_followup`, so pass 1 skips them and they wait for `--review` instead of being re-tagged and re-billed.
- **The white whale, harpooned.** Interactive mode (`-i`) "hung forever" in every prior attempt. The cause was never ComicTagger; it was the wrapper CAPTURING output, which pipes stdin so `input()` blocks on a prompt no one can answer. Pass 3 inherits the real terminal instead of capturing, and the prompt works.
- **A guard against the old curse.** `--review` refuses to start unless it has a real interactive terminal, so it can never silently hang inside a cron job or a pipe.
- **`--accept-low-confidence`** (opt-in): restores the old "accept any match without asking" behaviour as a final pass-2 strategy. Off by default, because the entire point of pass 3 is that a human, not a coin flip, decides the ambiguous ones.

### Changed
- **`--interactive-followup` is now `--auto-retry`.** The old name was a lie: nothing about it was interactive. The honest name describes what it does.
- **Capability probing replaces hardcoded flags.** This issue uncovered that ComicTagger renames core flags between beta point releases: `--abort` / `--no-abort` in 1.6.0b9 became `--no-save-on-low-confidence` / `--save-on-low-confidence` in 1.6.0b11. Worse, argparse prefix-matching would silently resolve a stale `--abort` to the unrelated `--abort-on-conflict`, doing the wrong thing without complaint. CLICLO now reads `comictagger --help` once at startup and uses the flag names that actually exist on YOUR build.

### Fixed
- **Pass 3 reliability tied to the right ComicTagger.** ComicTagger 1.6.0b9 and earlier crash with an `AssertionError` on ambiguous matches (the exact files pass 3 exists for), because `save()` asserts on metadata that a no/multiple/low-confidence match never sets. 1.6.0b11 fixed this by returning before the assert. CLICLO detects a pre-b11 build and warns you plainly: pass 2 and pass 3 are reliable on 1.6.0b11+, and older betas may crash. That is a ComicTagger bug, surfaced honestly rather than papered over.
- **Review no longer mistakes pre-existing tags for success.** Comics routinely ship with an embedded `ComicInfo.xml` from the release group. A naive "does it have tags now?" check would mark such files done the instant they entered review, skipping the human decision they were queued for, the same phantom-success class v5.0 killed elsewhere. Pass 3 now fingerprints the metadata (series, issue, title, and the ComicVine issue id) before and after the prompt and only records success when something actually changed. A skip leaves the file flagged, never silently "tagged."
- **Conversion failures now say why, ZIP-mislabeled CBRs stop being failures, and recoverable files get repaired.** A failed CBR-to-CBZ conversion used to report only "no CBZ produced." It now surfaces ComicTagger's own reason. A `.cbr` that is actually a ZIP (common from some release groups) is renamed to `.cbz` losslessly and tagged, no longer a "failure." When ComicTagger chokes on a recoverable problem (a corrupt per-file timestamp, an odd container it won't identify), CLICLO attempts a tolerant repack: it extracts the page images with independent tooling and writes a fresh CBZ with reset metadata, all-or-nothing so it never produces a comic missing pages, and reports the real format when a file genuinely can't be read. A true RAR still needs `rarfile` plus an unrar *binary* on PATH; CLICLO warns up front when CBRs are present but no unrar tool is found. Repair can be disabled with `repair_failed_archives = false`.

- **No more re-converting (and silently duplicating) the same CBRs every run.** With `keep_cbr` on, a converted `.cbr` stayed on disk but was never recorded, so each scan re-found it as "new" and Phase 1 ran it through ComicTagger again. ComicTagger doesn't overwrite, it writes a uniquely-named file, so every repeat run quietly spawned `Title (1).cbz`, `Title (2).cbz`, and so on, while the log cheerfully said "Converted." CLICLO now skips conversion when the `.cbz` already exists and records the `.cbr` as handled, so a converted file is converted once and never re-found. Existing `(N).cbz` duplicates from before this fix can be cleared with the new `--dedupe` (dry-run by default; `--dedupe --confirm` to delete, keeping each base `Title.cbz` and clearing the duplicate's database row).

### Experimental
- **One flaky file can no longer kill the whole run.** On `--retry-failed`, CLICLO existence-checked every previously-failed path up front; across thousands of paths on a network share, one `stat()` hitting a transient SMB error (Windows `WinError 59`) was near-certain, and `Path.exists()` *raises* that error instead of returning `False`, so the entire run aborted before tagging anything. The existence check now tolerates a stat error (keeps the path for retry), and the per-file tagging loop is wrapped so any single file's unexpected failure is recorded as a transient error and the batch keeps going. A momentary network blip costs you one file, not the run.
- **Subprocess output decoded as UTF-8, not the OS codepage.** ComicTagger's output was captured with Python's default decoder, which on Windows is cp1252; any byte outside that codepage (Japanese text in manga metadata, accented names, and so on) crashed the stdout reader thread mid-read. The truncated output then failed to parse and the file was misfiled as "needs review," so titles like One Piece were wrongly flagged as ambiguous rather than tagged. All capturing subprocess calls now decode UTF-8 with `errors="replace"`, so a stray byte becomes a placeholder instead of killing the read.
- **Multi-key rotation (off by default, use at your own risk).** Setting `comicvine_api_keys` to a comma-separated list makes CLICLO rotate to a key with remaining hourly budget and cool down any key that trips velocity detection, roughly multiplying throughput by the key count. This very likely violates ComicVine's per-user rate limit and can get your keys and your IP banned, since all requests originate from one machine; it may not even raise your effective ceiling. It exists because a 30k+ library takes weeks on one key, but the documentation and the code both say plainly what it is. Single-key behaviour is unchanged.

> **A FRANK WORD FROM THE EDITORS:** *Let it be said plainly. ComicTagger 1.6 has been "beta" since the autumn of 2024, it renames its own controls between point releases, and its newest build is not even on the public package index. CLICLO now bends with that wind instead of snapping in it, but no wrapper can make an unstable foundation stable. Run 1.6.0b11 or newer, confirm the version you actually have, and treat "universal across every ComicTagger build ever shipped" as the fiction it is. Honesty, dear reader, is the better part of valor.*

---


**On sale 2026-05-31**

> **NARRATOR BOX:** *For years our hero divined success by SQUINTING at log lines, reading tea leaves spelled out in English prose that shifted with every passing release! No more, dear reader! In this earth-shaking issue, CLICLO learns to read the MACHINE'S OWN TONGUE, and a phantom menace that masqueraded as victory is unmasked at last!*

This is a ground-up rewrite. The engine still wraps ComicTagger's CLI, but the way it listens to ComicTagger changed completely, and a pile of accumulated barnacles went over the side.

### Added
- **Structured result parsing via `-j`.** CLICLO now reads ComicTagger's JSON result object and switches on the `Status` and `MatchStatus` enums (`success`, `read_failure`, `write_permission_failure`, `fetch_data_failure`, `match_failure`, plus `low_confidence_match` / `multiple_match` / `no_match`). The old code guessed outcomes by string-matching log text, which broke quietly every time ComicTagger reworded a message. The new contract is version-stable.
- **Executable discovery on PATH.** A blank `comictagger_path` now falls back to `shutil.which`, so a `pip install` ComicTagger is found, not only a binary parked in a fixed folder.
- **Database migration with a safety net.** Existing v3.2 and v4.0 databases upgrade in place: new columns are added, the old `low_confidence_matches` queue is carried into `low_confidence`, and schema version is tracked with `PRAGMA user_version`. Before any migration touches a thing, the old file is copied to `db.bak-vN-timestamp`. A botched upgrade is now recoverable instead of fatal.
- **`--db-info`** prints schema version and a per-status breakdown.
- **Rate-pause notifications.** When the proactive hourly budget kicks in, Pushover gets a heads-up (once per pause), so a throttled run no longer looks like a silent hang on your phone.

### Changed
- **Resume is correct and cheap.** Every file ComicTagger touches is recorded with a status, and each status has exactly one reprocessing path: new files run in the primary scan, transient failures wait for `--retry-failed`, the review queue waits for `--interactive-followup`. The scan now skips already-seen files using a single in-memory set instead of one database query per file, which matters at 100k.
- **Credentials are no longer baked into the source.** They live in `cliclo.ini` (run `--init-config` to scaffold it) or in `CLICLO_*` environment variables. The script is now safe to publish without leaking a key.
- **Honest rate-limit accounting.** ComicVine allows 200 requests *per resource* per hour with velocity detection on top; the budget reflects that and stays conservative. Expect weeks, not a weekend, for tens of thousands of files. That is the price of never getting your key throttled, and it is the thing the broken scripts in the forums could never survive.
- **`tag_format` is validated.** `CR` is the only value stock ComicTagger 1.6.x accepts for `--tags-write`; CIX/ComicInfo.xml rides along under CR. Bad values warn and fall back instead of crashing mid-run.

### Fixed
- **The phantom success.** When ComicTagger fails to confirm a write (an incompatible 1.5.x build, a mid-save crash, truncated output), it can exit cleanly while doing nothing. The old fallback called that "success" and marked the comic done. CLICLO now treats any missing JSON result as a retryable error. The source of truth is the machine's report, not its exit code.
- **The copy that could hang forever.** Large network archives are copied locally before conversion under a watchdog timeout; a stalled share is abandoned and logged instead of freezing the run.
- **`-m` metadata** is YAML-escaped properly and survives apostrophes in series names ("Captain America's").
- **Pushover stops retrying on `4xx`** responses, which the API explicitly says will never succeed no matter how many times you ask.

### Removed
- The entire string-matching result classifier and its `PERMANENT_ERRORS` keyword list, replaced by enum checks.
- Dead code and stale references flagged by the linter. The file is shorter than v4.0 despite doing more.

> **A WORD OF CAUTION FROM THE EDITORS:** *ComicTagger 1.6.0 remains a BETA at time of printing, in beta since the autumn of 2024! `pip install comictagger` will hand you the 1.5.x line, whose CLI speaks an entirely different language and will not obey CLICLO. Insist on the 1.6.x line: `pip install --pre comictagger`, or a 1.6 beta build. You have been WARNED!*

---

## ISSUE #4 — v4.0.0
### *"SIX BUGS, FOUR LIES, AND A RECKONING!"*
**From the vault**

> **NARRATOR BOX:** *An executive code review tore the hero's armor open and found the rot beneath the paint! What looked bulletproof had been surviving on luck and coincidence!*

### Fixed
- **`logger` referenced before it existed**, a `NameError` waiting for the wrong import order. Hoisted to module level.
- **`--tags-write` passed twice**, the second silently overriding the first, so only one tag format was actually written. Collapsed to a single configurable value.
- **Rate limiting that counted invocations as if they were single API calls**, budgeting roughly 720 calls an hour against a 200 ceiling. Recalibrated to estimate real calls per invocation.
- **Pushover emergency notifications that never arrived** because priority 2 requires `retry` and `expire` parameters. Supplied them.
- **Emoji mojibake** in notifications, replaced with proper Unicode escapes.
- **`-m` metadata** corrected toward YAML syntax.
- **`INSERT OR REPLACE` that reset `retry_count`** to zero on every write, replaced with an upsert that preserves it.
- **A duplicate `get_comic_files` method** and assorted dead code.
- **`--abort` finally used in the primary pass**, so low-confidence matches were actually detectable; the relaxed follow-up became meaningfully different by using `--no-abort`.

### Added
- Config file support (`cliclo.ini`), environment-variable overrides, cross-platform executable detection, SQLite file locking, WAL journal mode, and `--dry-run` passthrough.

---

## ISSUE #3.2 — v3.2.0
### *"THE RENAMING, AND THE ALL-SEEING EYE!"*
**From the vault**

> **NARRATOR BOX:** *A tool that ran for days in silence was a tool that bred ANXIETY! And so our hero gave the beast a voice, and a new name to match its ambitions!*

### Added
- **Pushover integration** for remote monitoring of multi-day runs: smart milestones that scale to library size, distinct priorities and sounds per event, graceful degradation when the notification service is unreachable.

### Changed
- **"DarkTagger" became "C.L.I.C.L.O."** A name born at 2 AM gave way to a name that describes what the thing does: Command Line Interface Comic Library Organizer.

---

## ISSUE #3.0 — v3.0.0
### *"THE LOW-CONFIDENCE LABYRINTH!"*
**From the vault**

### Added
- **Interactive follow-up mode** for matches the primary pass refused. The discovery that ComicTagger's `-i` flag hangs forever waiting for stdin that a subprocess can never provide led to the multi-strategy answer instead: relax confidence, drop the year, search by series name, and only then flag for manual GUI tagging.

---

## ISSUE #2.0 — v2.0.0
### *"BULLETPROOFING THE BEAST!"*
**From the vault**

> **NARRATOR BOX:** *"Hopefully it works" is no creed for a hero! Every failure mode would meet its handler, or the beast would not ship!*

### Added
- **CBR to CBZ conversion** via ComicTagger's `-e`, since you cannot write tags into a read-only RAR. Large archives on network paths copy locally first to dodge timeouts.
- **Multi-strategy retry logic** and a separate queue for the genuinely stubborn.
- **Failure triage**: permanent (corrupt) distinguished from temporary (network), each handled on its own terms.

---

## ISSUE #1 — v1.0.0
### *"FOUNDATIONS IN FRUSTRATION!"*
**The first sensational issue**

> **NARRATOR BOX:** *Thirty thousand comics. Three minutes of clicking apiece. Fifteen hundred hours of a man's life, demanded by a mouse button! There HAD to be another way, and there WAS!*

### Added
- The original Python wrapper around ComicTagger's CLI, with SQLite progress tracking and resume capability. The founding lesson, learned the hard way: mirror the GUI's proven auto-tag approach instead of inventing clever parsing for problems that did not exist yet.

---

*All return codes, rate limits, and AssertionErrors depicted herein are based on actual events. Any resemblance to a stable 1.6.0 release, living or dead, remains purely aspirational.*

*Letters to the Editor may be submitted via Pushover notification (priority 0, so as not to wake anyone).*
