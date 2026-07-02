# CLICLO — Full Reference

Complete documentation of every feature, command, configuration key, and
behavior in CLICLO. For a quick orientation, read the [README](README.md)
first; this document is the exhaustive reference.

Applies to CLICLO v5.1.1.

---

## Table of contents

1. [Architecture overview](#architecture-overview)
2. [Installation](#installation)
3. [Command-line reference](#command-line-reference)
4. [Configuration reference](#configuration-reference)
5. [The three-pass workflow](#the-three-pass-workflow)
6. [The progress database](#the-progress-database)
7. [Rate limiting](#rate-limiting)
8. [CBR conversion and archive repair](#cbr-conversion-and-archive-repair)
9. [De-duplication](#de-duplication)
10. [Pushover notifications](#pushover-notifications)
11. [Experimental features](#experimental-features)
12. [Troubleshooting](#troubleshooting)

---

## Architecture overview

CLICLO is a single Python file (`cliclo.py`) with no third-party runtime
dependencies. It does not talk to ComicVine itself; it drives the
**ComicTagger** CLI as a subprocess, reads ComicTagger's structured JSON
output, and classifies each file's outcome. Everything around that core —
scanning, resumability, rate pacing, CBR conversion, archive repair, the
review queue, dedupe, and notifications — is CLICLO's own.

The main components inside `cliclo.py`:

- **`load_config` / `write_default_config`** — layered configuration
  (defaults → `cliclo.ini` → `CLICLO_*` environment variables → CLI flags).
- **`CLICLODatabase`** — SQLite progress store with file locking, schema
  migration, and a legacy-DB backup path.
- **`CLICLOTagger`** — the engine: finds and version-checks ComicTagger,
  builds invocations, parses JSON results, paces API usage, and runs the
  passes.
- **`PushoverNotifier`** — optional push notifications for startup,
  milestones, rate pauses, errors, completion, crash, and interruption.

---

## Installation

Requirements:

- **Python 3.10+**
- **ComicTagger ≥ 1.6.0b11** on PATH (the 1.5.x CLI is not compatible):

  ```
  pip install --pre "comictagger>=1.6.0b11"
  ```

- A free **ComicVine API key**: https://comicvine.gamespot.com/api/
- Optional, for true-RAR `.cbr` files: `pip install rarfile` **plus** an
  `unrar` binary on PATH. Without it, ZIP-format `.cbr` files still convert;
  true RARs are skipped.

Then:

```
git clone https://github.com/phattbeats/cliclo.git
cd cliclo
python cliclo.py --init-config     # scaffolds cliclo.ini
# edit cliclo.ini: comics_path, comicvine_api_key
python cliclo.py
```

CLICLO logs the detected ComicTagger version on startup. If you see 1.5.x,
a frozen `comictagger.exe` in your working directory may be shadowing the
pip install — run from elsewhere or set `comictagger_path`.

---

## Command-line reference

```
python cliclo.py [PATH] [options]
```

`PATH` is an optional positional argument: the comics directory, overriding
`comics_path` from config.

### Actions (mutually exclusive)

Exactly one of these may be given; with none, CLICLO runs pass 1.

| Flag | What it does |
| --- | --- |
| *(none)* | **Pass 1**: scan recursively, tag every high-confidence match, queue the rest for review. |
| `--auto-retry` | **Pass 2**: re-run the review queue with broadened matching, still refusing low-confidence writes. |
| `--review` | **Pass 3**: interactive resolution of ambiguous files. Requires a real terminal (refuses to run headless, so it is safe in cron). |
| `--test FILE` | Tag a single file and print the full classified result. Diagnostic. |
| `--stats` | Print running statistics from the database. |
| `--db-info` | Print schema version, status counts, and a sample of stored paths. Use this to confirm a resume is matching your files. |
| `--show-failed` | List failed files and their recorded error reasons. |
| `--dedupe` | List numbered duplicate CBZs (` (1).cbz` … ` (999).cbz` next to a base file). Dry-run unless `--confirm` is added. |
| `--reset-db` | Delete the progress database and exit. |
| `--init-config` | Write a default `cliclo.ini` and exit. |
| `--test-pushover` | Send a test notification and exit. |

### Path / connection options

| Flag | What it does |
| --- | --- |
| `--comics-path PATH` | Override the comics root (same as positional `PATH`). |
| `--comictagger-path PATH` | ComicTagger install dir or binary, if not on PATH. |
| `--api-key KEY` | ComicVine API key (overrides config and env). |
| `--api-keys K1,K2` | **Experimental** comma-separated extra keys for rotation. See [Experimental features](#experimental-features). |
| `--db-path FILE` | Progress database file. Point at an older run's DB to resume it. |
| `--config FILE` | Config file path (default `cliclo.ini`). |
| `--proxy URL` | **Experimental** HTTP(S) proxy for ComicVine requests, e.g. `http://192.168.1.10:8118`. |
| `--rotate-egress` | **Experimental** bind each API key to its own egress route (requires proxy + 2+ keys). |

### Behavior modifiers

| Flag | What it does |
| --- | --- |
| `--retry-failed` | Include retryable (transient) failures in pass 1. |
| `--accept-low-confidence` | With `--auto-retry`: append a final pass that accepts *any* match without asking. Use only if an imperfect tag beats none. |
| `--no-resume` | Start fresh — **deletes the database** first. |
| `--no-convert-cbr` | Skip CBR→CBZ conversion. |
| `--delete-cbr` | Delete CBR originals after a successful conversion. |
| `--dry-run` | Preview everything; modify no files. |
| `--confirm` | Required for `--dedupe` to actually delete (otherwise it only lists). |
| `--no-pushover` | Disable notifications for this run. |

`python cliclo.py --help` prints the same pipeline summary with examples.

---

## Configuration reference

Configuration is layered, later wins:

1. Built-in defaults
2. `cliclo.ini` (section `[cliclo]`; path via `--config`)
3. Environment variables — every key maps to `CLICLO_<KEY_UPPERCASED>`
   (e.g. `comicvine_api_key` → `CLICLO_COMICVINE_API_KEY`)
4. Command-line flags

All keys, with defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `comictagger_path` | *(blank)* | Path to the ComicTagger executable. Blank = discover on PATH. |
| `comics_path` | *(blank)* | Root folder of the library, scanned recursively. **Required** (here, env, or CLI). |
| `comicvine_api_key` | *(blank)* | Your ComicVine API key. **Required** for tagging runs; batch entry points refuse to start without one. |
| `comicvine_api_keys` | *(blank)* | Experimental comma-separated extra keys for rotation. Deduped against the primary. |
| `safe_invocations_per_hour` | `50` | Conservative hourly pacing of ComicTagger invocations (ComicVine allows ~200 requests/resource/hour). |
| `api_calls_per_invocation` | `4` | Estimated ComicVine calls per ComicTagger invocation, used for budgeting. |
| `max_retries` | `3` | Attempts per file before a transient failure is left for `--retry-failed`. |
| `tag_format` | `CR` | ComicTagger metadata style to write. `CR` (ComicInfo.xml) is the only valid `--tags-write` value in stock 1.6.x. |
| `repair_failed_archives` | `true` | Attempt the tolerant repack on recoverable conversion failures. |
| `proxy` | *(blank)* | Experimental HTTP(S) proxy for ComicVine calls. |
| `rotate_egress` | `false` | Experimental per-key egress binding (needs proxy + 2+ keys). |
| `db_path` | `cliclo_progress.db` | Progress database file. |
| `pushover_api_token` | *(blank)* | Pushover application token. |
| `pushover_user_key` | *(blank)* | Pushover user key. |
| `pushover_device` | *(blank)* | Optional target device name. |
| `pushover_enabled` | `true` | Master switch for notifications (both keys must also be set). |

`--init-config` scaffolds a commented `cliclo.ini`; the first tagging run
does the same if none exists. **Keep your real `cliclo.ini` out of version
control** — it holds your API key, and `.gitignore` already excludes it.

Colors: set `NO_COLOR` or `CLICLO_NO_COLOR` to disable ANSI output.

---

## The three-pass workflow

Ambiguous matches should neither be guessed at nor block the run, so the
work splits into three passes:

1. **Pass 1** (`python cliclo.py`) — tag everything that matches with high
   confidence. Multiple matches, low-confidence matches, and no-matches go
   into a review queue in the database instead of being written.
2. **Pass 2** (`python cliclo.py --auto-retry`) — re-run the queue with
   broadened matching, still strict about confidence. Add
   `--accept-low-confidence` for a final take-anything sweep.
3. **Pass 3** (`python cliclo.py --review`) — interactive: CLICLO shows each
   genuinely ambiguous file's candidates and you pick. Refuses to run
   without a real terminal so a cron job can never hang on `input()`.

Every pass is resumable — Ctrl-C at any point, run the same command again,
and it continues from the database without re-tagging recorded work.

---

## The progress database

All state lives in one SQLite file (`db_path`).

- **Locking** — the database is file-locked on open, so two CLICLO
  instances cannot corrupt one run.
- **Schema migration** — older databases are migrated in place;
  column additions are idempotent. A legacy pre-CLICLO database is backed
  up before first migration.
- **Legacy detection** — if the default DB is missing but a legacy-named
  database is present, CLICLO warns so you don't silently start from zero.

Per-file outcomes recorded (visible in `--db-info` / `--show-failed`):

| Status | Meaning |
| --- | --- |
| `success` | Tagged with a confident match. |
| `user_selected` | Tagged with a match you chose in `--review`. |
| `converted` | CBR converted to CBZ (tagging outcome recorded separately). |
| `needs_followup` | In the review queue (multiple / low-confidence / no match). |
| `error` | Transient failure; eligible for `--retry-failed` up to `max_retries`. |
| `permanent_error` | Not retryable (e.g. unrecoverable archive). |
| `manual_required` | Needs human action outside CLICLO. |
| `skipped` | Intentionally not processed. |

API invocations are also journaled per key (`api_calls` table) to enforce
the hourly budget across restarts — the rate limiter survives Ctrl-C too.

To resume a different run: `--db-path that_run.db`. To wipe: `--reset-db`
(or `--no-resume`, which deletes the DB then starts a fresh pass 1).

---

## Rate limiting

ComicVine allows roughly 200 requests per resource per hour and also
velocity-throttles. CLICLO:

- paces itself to `safe_invocations_per_hour` (default 50 invocations ≈ 200
  estimated calls at `api_calls_per_invocation = 4`), sleeping when the
  rolling one-hour window is spent;
- detects rate-limit responses in ComicTagger output and backs off with a
  cooldown instead of hammering;
- in multi-key mode, tracks the budget **per key** and benches a
  rate-limited key for a cooldown period while others keep working;
- sends a Pushover notification when it pauses, so an unattended run never
  stalls silently.

---

## CBR conversion and archive repair

Before tagging, `.cbr` files are converted to `.cbz` (unless
`--no-convert-cbr`):

- Most `.cbr` files are actually ZIPs — these are renamed losslessly.
- True RAR files are extracted and repacked, if `rarfile` + an unrar binary
  are available; otherwise they are skipped and recorded.
- Originals are kept unless `--delete-cbr` is passed.

**Tolerant repack** (`repair_failed_archives = true`): when a conversion or
ComicTagger write fails on a recoverable problem — a corrupt timestamp, an
odd container — CLICLO extracts the pages with independent tooling and
writes a fresh CBZ. The repack is **all-or-nothing**: it never produces a
comic missing pages. Unrecoverable archives are marked `permanent_error`.

---

## De-duplication

Older tools and interrupted runs leave numbered copies like
`Comic 001 (1).cbz` beside `Comic 001.cbz`. `--dedupe` finds files matching
`" (N).cbz"` (1–3 digits, case-insensitive) whose base file also exists:

- `python cliclo.py --dedupe` — list them (dry-run, always safe);
- `python cliclo.py --dedupe --confirm` — delete the numbered copies, keep
  each base file, and clear the deleted rows from the database.

---

## Pushover notifications

Optional; enabled when `pushover_api_token` and `pushover_user_key` are set
and `pushover_enabled = true` (kill per-run with `--no-pushover`, verify
with `--test-pushover`). Notifications are sent for:

- **Startup** — run beginning, with total file count;
- **Milestones** — progress checkpoints scaled to library size;
- **Rate pauses** — when the hourly budget forces a wait, with duration;
- **Error clusters** — repeated failures, with a sample;
- **Completion** — totals, status breakdown, and elapsed hours;
- **Crash / interruption** — unhandled errors and Ctrl-C.

Messages respect Pushover's hard API limits (1024-char message, 250-char
title). `pushover_device` targets a specific device; blank = all.

---

## Experimental features

Off by default. These lean against ComicVine's terms of service, which
frame the rate limit per user; using them can get your keys and IP banned.
They announce themselves loudly when enabled.

- **Multi-key rotation** (`comicvine_api_keys` / `--api-keys`) — rotate
  across several keys, each with its own hourly budget and cooldown bench.
  Roughly multiplies throughput by key count, but all traffic exits one IP.
- **Proxy** (`proxy` / `--proxy`) — route ComicVine calls through an
  HTTP(S) proxy. Changes nothing about limits unless the proxy egresses
  from a genuinely different public IP (verify:
  `curl -x <proxy> ifconfig.me`).
- **Egress rotation** (`rotate_egress` / `--rotate-egress`, needs proxy +
  2+ keys) — bind each key to its own egress route so each key always exits
  the same IP. VPN/Tor exit IPs tend to be flagged faster, not slower.

CLICLO is for personal, non-commercial use of the ComicVine API.

---

## Troubleshooting

- **"comictagger not found" / wrong version logged** — install with
  `pip install --pre "comictagger>=1.6.0b11"`; beware a local
  `comictagger.exe` shadowing PATH; set `comictagger_path` explicitly.
- **No API key** — batch runs refuse to start; set `comicvine_api_key` in
  `cliclo.ini` or `CLICLO_COMICVINE_API_KEY`.
- **Resume looks like it's starting over** — run `--db-info` and check the
  sample paths match the tree you're scanning; if you moved the library,
  the stored absolute paths won't match.
- **True `.cbr` files skipped** — install `rarfile` *and* an unrar binary.
- **`--review` exits immediately under cron/CI** — by design; it requires
  an interactive terminal.
- **Garbled output / no colors wanted** — set `NO_COLOR` or
  `CLICLO_NO_COLOR`.
