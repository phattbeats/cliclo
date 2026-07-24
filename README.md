# CLICLO

**C**ommand **L**ine **I**nterface **C**omic **L**ibrary **O**rganizer.

CLICLO batch-tags a comic library with ComicVine metadata by driving the
[ComicTagger](https://github.com/comictagger/comictagger) CLI. It is built for
large libraries (tens of thousands of files), unattended runs, and resuming
cleanly after an interruption. Point it at a folder, walk away, come back to a
tagged library.

This README is the quick tour. **[DOCS.md](DOCS.md)** is the full reference:
every command, every flag, every config key, database behavior, rate
limiting, notifications, and troubleshooting.

It is a single Python file with no third-party dependencies. The only thing it
needs is the ComicTagger CLI on your PATH.

---

## What it does

- Recursively tags every `.cbz` / `.cbr` under a folder with ComicVine metadata.
- **Resumes anywhere.** Progress lives in a SQLite database; interrupt it with
  Ctrl-C and run it again, it picks up where it stopped and never re-tags work
  it has already recorded.
- **Rate-limit aware.** Paces itself under ComicVine's ~200-requests-per-hour
  ceiling and backs off on velocity throttling instead of hammering the API.
- **Converts CBR to CBZ** before tagging. ZIP-format `.cbr` files (common) are
  renamed losslessly; true RAR files are extracted if you have RAR support.
- **Repairs recoverable archives.** When ComicTagger chokes on a corrupt
  timestamp or an odd container, CLICLO attempts a tolerant repack: it extracts
  the pages with independent tooling and writes a fresh CBZ, all-or-nothing, so
  it never produces a comic missing pages.
- **Three-pass workflow** (below) that separates confident auto-tagging from
  the handful of files that need a human decision.
- **De-duplicates** numbered copies left by older tools or runs.
- Optional **Pushover** notifications for milestones, pauses, and completion.

---

## Requirements

- **Python 3.10+**
- **ComicTagger 1.6.0b11 or newer**, on your PATH. The 1.6 line is still a
  pre-release and the 1.5.x CLI is **not** compatible, so install it explicitly:

  ```
  pip install --pre "comictagger>=1.6.0b11"
  ```

- A **ComicVine API key** (free): https://comicvine.gamespot.com/api/
- **Optional, for true-RAR `.cbr` files:** the `rarfile` package *and* an unrar
  binary on PATH (UnRAR.exe from rarlab, or WinRAR). `rarfile` alone is only a
  shim. Without it, ZIP-format `.cbr` files still convert; true RARs are skipped.

  ```
  pip install rarfile
  ```

> **Heads up on `comictagger.exe` shadowing:** if you run CLICLO from a folder
> that contains a frozen/bundled `comictagger.exe`, Windows may use that instead
> of the pip-installed 1.6 build. Either run from elsewhere or set
> `comictagger_path` in `cliclo.ini` to the correct executable. CLICLO logs the
> ComicTagger version on startup, check it says `1.6.0b11` or newer.

---

## Quick start

```
# 1. Install ComicTagger (see above)
pip install --pre "comictagger>=1.6.0b11"

# 2. Get CLICLO
git clone https://github.com/phattbeats/cliclo.git
cd cliclo

# 3. Create your config (or let the first run scaffold one for you)
cp cliclo.ini.example cliclo.ini
#   edit cliclo.ini: set comics_path and comicvine_api_key

# 4. Run it
python cliclo.py
```

That first run tags everything it can match confidently. Interrupt any time;
run it again to resume.

---

## The three-pass workflow

Tagging a large library well means not letting ambiguous matches either get
tagged wrongly or block the whole run. CLICLO splits the work into three passes:

1. **Pass 1, the default run** (`python cliclo.py`). Tags everything that
   matches with high confidence. Anything ambiguous (multiple matches, low
   confidence, no match) is set aside in a review queue, not guessed at.

2. **Pass 2, automated re-match** (`python cliclo.py --auto-retry`). Re-runs the
   review queue with broadened matching, still refusing to write a low-confidence
   guess. Clears the easy stragglers without a human.
   Add `--accept-low-confidence` to append a final pass that accepts *any* match
   without asking, use only if you would rather have an imperfect tag than none.

3. **Pass 3, human review** (`python cliclo.py --review`). Walks you through the
   files that are genuinely ambiguous and lets you pick the right match
   interactively. Requires a real terminal; it will not run headless (so it is
   safe in cron).

A typical sequence for a fresh library:

```
python cliclo.py              # pass 1
python cliclo.py --auto-retry # pass 2
python cliclo.py --review     # pass 3
```

---

## Resuming and switching databases

Progress is stored in the database named by `db_path` (default
`cliclo_progress.db`). To resume an earlier run, or to continue a library you
started tagging with an older tool, point `db_path` at that database:

```
python cliclo.py --db-path comic_tagger_progress.db
```

`python cliclo.py --db-info` shows the schema version, status counts, and a
sample of stored paths, useful for confirming a resume is recognizing your files
(for example, that the database's paths match the path you are scanning now, and
you are not silently re-tagging from zero).

---

## Common commands

| Command | What it does |
| --- | --- |
| `python cliclo.py` | Pass 1: tag everything matchable; queue the rest |
| `python cliclo.py --auto-retry` | Pass 2: re-match the review queue automatically |
| `python cliclo.py --review` | Pass 3: decide ambiguous matches interactively |
| `python cliclo.py --retry-failed` | Retry transient failures from earlier runs |
| `python cliclo.py --stats` | Show running statistics |
| `python cliclo.py --db-info` | Show DB schema, status counts, sample paths |
| `python cliclo.py --show-failed` | List failed files and their reasons |
| `python cliclo.py --dedupe` | List numbered duplicate CBZs (dry-run) |
| `python cliclo.py --dedupe --confirm` | Delete those duplicates, keep each base |
| `python cliclo.py --test FILE` | Tag a single file (diagnostic) |
| `python cliclo.py --init-config` | Write a default `cliclo.ini` and exit |
| `python cliclo.py --dry-run` | Preview; do not modify files |

Other flags: `--no-convert-cbr`, `--delete-cbr`, `--no-resume`, `--no-pushover`,
`--config <file>`. See `python cliclo.py --help` for the full list.

---

## Configuration

CLICLO reads `cliclo.ini` (see `cliclo.ini.example`). Every value can be
overridden by an environment variable (`CLICLO_COMICVINE_API_KEY`, etc.) or a
command-line flag. The keys that matter most:

- `comics_path`, the root folder to scan (recursively).
- `comicvine_api_key`, your ComicVine key.
- `db_path`, the progress database (set it to resume an older run).
- `safe_invocations_per_hour`, conservative hourly pacing (default 50).
- `repair_failed_archives`, attempt the tolerant repack on recoverable
  conversion failures (default `true`).

**Keep your real `cliclo.ini` out of version control.** It holds your API key.
The included `.gitignore` already excludes it; commit `cliclo.ini.example`
instead.

---

## Experimental features (off by default, use at your own risk)

These exist for very large libraries where one key on one connection is slow.
They lean against ComicVine's terms of service, which frame the rate limit
per user. Using them can get your keys and your IP banned. They are off by
default, they announce themselves loudly when on, and turning them on is your
call.

- **Multi-key rotation** (`comicvine_api_keys`): rotate across several API keys,
  using whichever still has hourly budget. Roughly multiplies throughput by the
  number of keys, but all traffic comes from one machine, so it may not raise
  your real ceiling and it is a clear ToS violation.

- **Proxy** (`proxy`): route ComicVine requests through an HTTP(S) proxy. On its
  own this changes nothing about rate limits unless the proxy egresses from a
  genuinely different public IP (chained to a VPN/Tor). Verify with
  `curl -x <proxy> ifconfig.me`.

- **Egress rotation** (`rotate_egress`, with a proxy + 2+ keys): bind each key to
  its own egress, key 1 direct, key 2 via the proxy, so each key always exits the
  same IP and looks like a separate user. This is deliberate IP rotation on top
  of key rotation; VPN/Tor exit IPs tend to be flagged *faster*, not slower.

CLICLO is for personal, non-commercial use of the ComicVine API. Respect their
terms.

---

## How it works (briefly)

CLICLO does not reimplement ComicVine matching; it drives ComicTagger's CLI and
reads its structured JSON output to classify each result (tagged, already
tagged, ambiguous, failed). It records every file's outcome in SQLite so the
work is resumable and idempotent. Conversion, repair, rate limiting, rotation,
and the review queue are CLICLO's; the actual tagging and the ComicVine lookups
are ComicTagger's.

---

## License

MIT. See [LICENSE](LICENSE).

CLICLO is an independent tool and is not affiliated with ComicTagger or
ComicVine. ComicVine is a trademark of its respective owner.
