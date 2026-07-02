#!/usr/bin/env python3
"""
CLICLO v5.1 - Command Line Interface Comic Library Organizer
Automated comic metadata tagging via ComicTagger's CLI.

THE THREE-PASS PIPELINE (v5.1):
  Pass 1  (default run)     High-confidence auto-tag. The bulk of a library.
  Pass 2  (--auto-retry)    Automated re-match of the leftover queue: drops the
                            year, searches by series name, KEEPS the confidence
                            bar. Recovers files the primary missed on a parse quirk.
  Pass 3  (--review)        Human-adjudicated. Hands you ComicTagger's native
                            candidate list per file (-i); you pick a number or skip.
                            Not data entry; you decide conflicts. Resumable, and it
                            refuses to run without a real terminal so it cannot hang.

  The historical "-i hangs forever" problem was a plumbing bug: capturing output
  pipes stdin, so ComicTagger's input() blocks. Pass 3 inherits the real terminal
  instead of capturing, which is why it works.

WHAT CHANGED FROM v4.0 (the short version):
  - Result parsing no longer greps English log strings. It reads ComicTagger's
    structured JSON (-j) and switches on the Status / MatchStatus enums. This is
    version-proof; the old approach broke every time ComicTagger reworded a log line.
  - Executable discovery falls back to PATH (shutil.which), so a `pip install`
    ComicTagger is found, not just a downloaded binary in a fixed folder.
  - Database migrates from v3.2 / v4.0 schemas in place, AND backs the old file up
    first (db.bak-vN-timestamp) so a botched migration is recoverable.
  - Large-network-file copy can no longer hang forever; it runs under a watchdog
    timeout instead of a blocking shutil.copy2.
  - Resume is correct and cheap: every file ComicTagger has seen lands in the DB
    with a status, and each status has its own reprocessing path. The primary scan
    skips anything already seen with a single in-memory set, not one query per file.
  - Proactive rate-limit pauses now notify Pushover (once per pause), so a throttled
    run doesn't look like a silent hang on your phone.
  - Credentials are no longer hardcoded. Put them in cliclo.ini (run --init-config)
    or in environment variables. Safe to publish.
  - tag_format is validated; only CR is a valid --tags-write value in stock 1.6.x.
  - -m metadata is YAML-escaped properly (survives apostrophes in series names).
  - Pushover stops retrying on 4xx responses (the API says they will never succeed).

Verified against ComicTagger 1.6.0b9 (the 1.6.x line; 1.6.0 is still beta as of
this writing and `pip install comictagger` returns the incompatible 1.5.x CLI).

Requirements:
  pip install requests
  ComicTagger 1.6.0-beta.x  (pip install --pre comictagger, or a 1.6 beta build)
"""

import os
import re
import sys
import time
import json
import shutil
import sqlite3
import logging
import platform
import tempfile
import argparse
import threading
import configparser
import subprocess
import dataclasses
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set

# ---------------------------------------------------------------------------
# Platform-aware file locking
# ---------------------------------------------------------------------------
if platform.system() == "Windows":
    import msvcrt

    def _lock_file(fh):
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh):
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_file(fh):
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh):
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass


logger = logging.getLogger("cliclo")

VERSION = "5.1"
SCHEMA_VERSION = 6

BANNER_WIDTH = 63
_LETTERS = [
    " ██████╗██╗     ██╗ ██████╗██╗      ██████╗",
    "██╔════╝██║     ██║██╔════╝██║     ██╔═══██╗",
    "██║     ██║     ██║██║     ██║     ██║   ██║",
    "██║     ██║     ██║██║     ██║     ██║   ██║",
    "╚██████╗███████╗██║╚██████╗███████╗╚██████╔╝",
    " ╚═════╝╚══════╝╚═╝ ╚═════╝╚══════╝ ╚═════╝",
]


def _supports_color() -> bool:
    """ANSI color only when it's safe: a real TTY, NO_COLOR unset, and on Windows the
    virtual-terminal mode can be switched on. Otherwise we print plain so escape codes
    never end up as garbage in a pipe or a dumb console."""
    if os.environ.get("NO_COLOR") or os.environ.get("CLICLO_NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return False
    return True


def print_banner():
    """Print the startup banner: centered ANSI-shadow CLICLO, retro color, pulp copy.
    Falls back to a plain header if the console can't encode the box-drawing glyphs."""
    W = BANNER_WIDTH
    color = _supports_color()

    def c(s, code):
        return f"\033[{code}m{s}\033[0m" if (color and s.strip()) else s

    RED, CYAN, BORDER = "38;5;196;1", "38;5;51", "38;5;44"
    YELLOW, CREAM, MAGENTA, STAR = "38;5;220;1", "38;5;230", "38;5;205;1", "38;5;226;1"

    def ctr(s):
        return " " * ((W - len(s)) // 2) + s

    def box(s, code):
        inner = W - 2
        s = s[:inner]
        left = (inner - len(s)) // 2
        right = inner - len(s) - left
        return c("║", BORDER) + " " * left + c(s, code) + " " * right + c("║", BORDER)

    top = c("╔" + "═" * (W - 2) + "╗", BORDER)
    bot = c("╚" + "═" * (W - 2) + "╝", BORDER)

    lines = [""]
    for r in _LETTERS:
        lines.append(c(ctr(r.ljust(44)), RED))
    lines.append(c(ctr(f"v{VERSION}  \u00b7  COMMAND-LINE COMIC LIBRARY ORGANIZER"), CYAN))
    lines.append(c(ctr("\u2605  ASTOUNDING TALES OF AUTOMATION  \u2605"), STAR))
    lines.append("")
    lines.append(top)
    lines.append(box("", CREAM))
    lines.append(box('"THIRTY THOUSAND ISSUES LANGUISH IN DIGITAL DARKNESS,', YELLOW))
    lines.append(box('AND ONE COMMAND LINE ANSWERS THE CALL!"', YELLOW))
    lines.append(box("", CREAM))
    lines.append(box("DEVOURS CBR AND CBZ       RESUMES FROM ANY CRASH", CREAM))
    lines.append(box("BRANDS EACH ISSUE TRUE    RESURRECTS THE CORRUPTED", CREAM))
    lines.append(box("OUTRUNS THE RATE-LIMITER  SUMMONS YOU FOR HARD CALLS", CREAM))
    lines.append(box("", CREAM))
    lines.append(box("\u2605  WHERE ORDER MEETS RELENTLESS PRECISION  \u2605", MAGENTA))
    lines.append(bot)
    lines.append("")

    try:
        print("\n".join(lines))
    except (UnicodeEncodeError, OSError):
        print(f"\n=== CLICLO v{VERSION} - Command Line Interface Comic Library Organizer ===\n")



# ComicTagger 1.6.x result.status values (comictaggerlib/resulttypes.py: Status)
CT_STATUS_SUCCESS = {"success", "existing_tags"}
CT_STATUS_PERMANENT = {"read_failure", "write_permission_failure"}
# ComicTagger MatchStatus values: good_match, no_match, multiple_match, low_confidence_match

# Strings that, in a fetch_data_failure, indicate the velocity / hourly limit
RATE_LIMIT_SIGNALS = ("rate limit", "slow down", "420", "status_code\": 107", "107")

# ---------------------------------------------------------------------------
# Defaults. Credentials are intentionally blank: set them in cliclo.ini
# (run --init-config to scaffold one) or via CLICLO_* environment variables.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "comictagger_path": "",          # blank => discover on PATH via shutil.which
    "comics_path": "",
    "comicvine_api_key": "",
    # EXPERIMENTAL, OFF BY DEFAULT. Comma-separated extra ComicVine keys. When set
    # (more than one key total), CLICLO rotates to a key with remaining hourly budget,
    # roughly multiplying throughput by the number of keys. This very likely violates
    # ComicVine's per-user rate limit and may get your keys AND your IP banned, since
    # all requests originate from one machine. Use entirely at your own risk.
    "comicvine_api_keys": "",
    # ComicVine allows 200 requests PER RESOURCE per hour (volumes and issues are
    # separate buckets) plus velocity detection (~1 req/sec). One auto-tag makes
    # several requests. We budget hourly conservatively; ComicTagger paces sub-second
    # internally and caches series data on disk, so real consumption is usually lower.
    "safe_invocations_per_hour": "50",
    "api_calls_per_invocation": "4",
    "max_retries": "3",
    "tag_format": "CR",              # CR is the only valid --tags-write value in stock 1.6.x
    # When a normal conversion fails on a recoverable problem (corrupt per-file timestamp,
    # a container ComicTagger won't identify), try a tolerant repack: extract the pages with
    # independent tooling and write a fresh CBZ with reset metadata. All-or-nothing; never
    # produces a comic missing pages. Set false to disable and just skip failed files.
    "repair_failed_archives": "true",
    # Optional HTTP/HTTPS proxy for ComicTagger's ComicVine requests, e.g.
    # http://192.168.1.10:8118. Routes online calls through the proxy via the standard
    # HTTP(S)_PROXY env vars. NOTE: a proxy on your own LAN egresses through the same
    # internet connection, so ComicVine still sees your same public IP; it changes
    # nothing about rate limits unless the proxy is chained upstream to a VPN/Tor/etc.
    "proxy": "",
    # EXPERIMENTAL, OFF BY DEFAULT. With a proxy set and more than one key, bind each key
    # to its own egress (key 1 -> direct, key 2 -> proxy, ...) so each key always exits the
    # same IP and looks like a separate user. Only meaningful if the proxy is a genuinely
    # different public IP (chained to a VPN/Tor); verify with `curl -x <proxy> ifconfig.me`.
    # This is deliberate IP rotation to get around a per-user limit; own the ban risk.
    "rotate_egress": "false",
    "db_path": "cliclo_progress.db",
    "pushover_api_token": "",
    "pushover_user_key": "",
    "pushover_device": "",
    "pushover_enabled": "true",
}

CONFIG_FILE = "cliclo.ini"


def load_config(config_file: str = CONFIG_FILE) -> Dict[str, str]:
    """defaults -> config file -> environment variables (CLICLO_<KEY>)."""
    config = dict(DEFAULTS)
    if os.path.exists(config_file):
        cp = configparser.ConfigParser()
        cp.read(config_file, encoding="utf-8")
        if cp.has_section("cliclo"):
            for key in config:
                if cp.has_option("cliclo", key):
                    config[key] = cp.get("cliclo", key)
    for key in config:
        env_key = f"CLICLO_{key.upper()}"
        if env_key in os.environ:
            config[key] = os.environ[env_key]
    return config


def write_default_config(path: str):
    cp = configparser.ConfigParser()
    cp.add_section("cliclo")
    for k, v in DEFAULTS.items():
        cp.set("cliclo", k, v)
    with open(path, "w", encoding="utf-8") as f:
        f.write("; CLICLO configuration. Fill in the blanks below.\n")
        f.write("; Required to do anything useful: comicvine_api_key and comics_path.\n")
        f.write("; comictagger_path may be left blank to auto-discover on PATH.\n")
        f.write("; To resume an earlier run, set db_path to that run's database file.\n")
        f.write("; comicvine_api_keys is EXPERIMENTAL key rotation: likely violates ComicVine's\n")
        f.write(";   rate limit and risks key/IP bans. Leave blank unless you accept that.\n")
        f.write("; proxy routes ComicVine calls through an HTTP(S) proxy; same public IP unless\n")
        f.write(";   the proxy is chained to a VPN/Tor upstream.\n")
        f.write("; rotate_egress (with proxy + 2+ keys) binds each key to its own IP, key 1 direct\n")
        f.write(";   and key 2 via proxy. EXPERIMENTAL IP rotation; verify the proxy is a different\n")
        f.write(";   public IP first and own the ban risk.\n")
        f.write("; Keep this file out of version control (add it to .gitignore).\n\n")
        cp.write(f)


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------
class PushoverNotifier:
    def __init__(self, api_token: str, user_key: str,
                 device: str = "", enabled: bool = True):
        self.api_token = (api_token or "").strip()
        self.user_key = (user_key or "").strip()
        self.device = (device or "").strip() or None
        self.enabled = enabled
        self.api_url = "https://api.pushover.net/1/messages.json"
        self.notified_milestones: Set[int] = set()

        if self.enabled and (not self.api_token or not self.user_key):
            logger.warning("Pushover credentials missing; notifications disabled")
            self.enabled = False

    def send(self, message: str, title: str = "CLICLO", priority: int = 0,
             sound: str = "pushover", retry: int = 60, expire: int = 600,
             max_retries: int = 2) -> bool:
        if not self.enabled:
            return False
        try:
            import requests
        except ImportError:
            logger.warning("requests not installed; Pushover disabled")
            self.enabled = False
            return False

        data = {
            "token": self.api_token,
            "user": self.user_key,
            "message": message[:1024],   # API hard limit
            "title": title[:250],
            "priority": priority,
            "sound": sound,
            "html": 1,
        }
        if self.device:
            data["device"] = self.device
        if priority == 2:
            # Emergency priority REQUIRES retry + expire. retry >= 30, expire <= 10800.
            data["retry"] = max(retry, 30)
            data["expire"] = min(expire, 10800)

        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(self.api_url, data=data, timeout=10)
                if resp.status_code == 200:
                    if resp.json().get("status") == 1:
                        return True
                    logger.warning(f"Pushover API error: {resp.json().get('errors')}")
                    return False
                # 4xx means the input is invalid; retrying will never help (per API docs).
                if 400 <= resp.status_code < 500:
                    logger.warning(f"Pushover HTTP {resp.status_code} (not retrying): {resp.text[:120]}")
                    return False
                logger.warning(f"Pushover HTTP {resp.status_code}: {resp.text[:120]}")
            except Exception as e:
                logger.warning(f"Pushover attempt {attempt + 1} failed: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        return False

    # -- semantic notifications --------------------------------------------

    def notify_startup(self, total_files: int, resume: bool = False):
        icon = "\U0001f504" if resume else "\U0001f680"
        action = "Resuming" if resume else "Starting"
        msg = (f"{icon} <b>{action} comic tagging</b>\n"
               f"\U0001f4da {total_files:,} files to process\n"
               f"\u23f0 Started {datetime.now().strftime('%H:%M')}")
        self.send(msg, title="CLICLO Started", sound="bike")

    def notify_milestone(self, processed: int, total: int,
                         stats: Dict[str, int], elapsed_hours: float):
        if processed in self.notified_milestones or processed not in self._milestones(total):
            return
        self.notified_milestones.add(processed)
        sc = stats.get("success", 0)
        sr = (sc / processed * 100) if processed else 0
        rph = processed / elapsed_hours if elapsed_hours > 0 else 0
        eta = (total - processed) / rph if rph > 0 else 0
        prio = 1 if processed >= 10000 else 0
        snd = "magic" if processed >= 20000 else "cashregister" if processed >= 10000 else "bike"
        msg = (f"\U0001f4ca <b>Milestone: {processed:,}/{total:,}</b>\n"
               f"Success: <b>{sc:,}</b> ({sr:.1f}%)\n"
               f"Errors: {stats.get('error', 0):,}\n"
               f"Needs review: {stats.get('needs_followup', 0):,}\n"
               f"CBR converted: {stats.get('cbr_converted', 0):,}\n"
               f"Rate: {rph:.1f}/hr, ETA: {eta:.1f}h")
        self.send(msg, title=f"Progress: {processed / total * 100:.1f}%", priority=prio, sound=snd)

    def notify_rate_pause(self, wait_minutes: int, reason: str = "hourly budget"):
        resume_at = (datetime.now() + timedelta(minutes=wait_minutes)).strftime("%H:%M")
        msg = (f"\u23f8\ufe0f <b>Pausing for rate limit</b>\n"
               f"Reason: {reason}\n"
               f"Waiting ~{wait_minutes} min, resuming around {resume_at}")
        self.send(msg, title="Rate Limited", priority=0, sound="falling")

    def notify_errors(self, count: int, sample: str):
        msg = (f"\u26a0\ufe0f <b>{count} consecutive API errors</b>\n"
               f"Likely rate limited or a ComicVine hiccup\n"
               f"Sample: {sample[:80]}")
        self.send(msg, title="API Errors", priority=1, sound="siren")

    def notify_completion(self, total: int, stats: Dict[str, int], hours: float):
        sc = stats.get("success", 0)
        sr = (sc / total * 100) if total else 0
        icon = "\U0001f389" if sr >= 90 else "\u2705" if sr >= 75 else "\u26a0\ufe0f"
        rate = f"{total / hours:.1f}/hr" if hours > 0 else "N/A"
        msg = (f"{icon} <b>Tagging complete</b>\n"
               f"Processed: <b>{total:,}</b>\n"
               f"Success: <b>{sc:,}</b> ({sr:.1f}%)\n"
               f"Errors: {stats.get('error', 0):,}\n"
               f"Needs review: {stats.get('needs_followup', 0):,}\n"
               f"CBR converted: {stats.get('cbr_converted', 0):,}\n"
               f"Duration: {hours:.1f}h, Rate: {rate}")
        self.send(msg, title="CLICLO Complete", priority=1, sound="magic")

    def notify_crash(self, error_msg: str):
        self.send(f"\U0001f4a5 <b>Fatal error</b>\n{error_msg[:200]}",
                  title="CLICLO Crashed", priority=2, sound="siren", retry=60, expire=600)

    def notify_interrupted(self):
        self.send("\u23f9\ufe0f <b>Processing interrupted</b>\nProgress saved, resume anytime",
                  title="CLICLO Stopped", priority=1, sound="falling")

    @staticmethod
    def _milestones(total: int) -> List[int]:
        ms = [m for m in (100, 500, 1000, 2500, 5000, 10000, 15000, 20000, 25000) if m < total]
        c = 30000
        while c < total:
            ms.append(c)
            c += 5000
        return ms


# ---------------------------------------------------------------------------
# Database (with migration + pre-migration backup)
# ---------------------------------------------------------------------------
class CLICLODatabase:
    """SQLite progress / rate-limit / stats store.

    Migrates v3.2 and v4.0 databases in place and backs the old file up first.
    Schema version is tracked with PRAGMA user_version.
    """

    def __init__(self, db_path: str = "cliclo_progress.db"):
        self.db_path = db_path
        self._lock_path = db_path + ".lock"
        self._lock_fh = None
        self._acquire_lock()
        self._backup_if_legacy()
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _acquire_lock(self):
        try:
            self._lock_fh = open(self._lock_path, "w")
            _lock_file(self._lock_fh)
        except (OSError, IOError):
            logger.error(f"Another CLICLO instance appears to be running (lock: {self._lock_path}). "
                         "If that's wrong, delete the .lock file and retry.")
            sys.exit(1)

    def _backup_if_legacy(self):
        """If an older-schema DB exists, copy it aside before we migrate it."""
        if not os.path.exists(self.db_path):
            return
        try:
            peek = sqlite3.connect(self.db_path)
            uv = peek.execute("PRAGMA user_version").fetchone()[0]
            has_pf = peek.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processed_files'"
            ).fetchone()
            peek.close()
        except Exception as e:
            logger.warning(f"Could not inspect existing DB for migration backup: {e}")
            return
        if has_pf and uv < SCHEMA_VERSION:
            bak = f"{self.db_path}.bak-v{uv}-{datetime.now():%Y%m%d%H%M%S}"
            try:
                shutil.copy2(self.db_path, bak)
                logger.info(f"Existing DB (schema v{uv}) backed up to {bak} before migration")
            except Exception as e:
                logger.warning(f"Backup before migration failed (continuing): {e}")

    def _init_schema(self):
        cur = self.conn.cursor()
        existed = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processed_files'"
        ).fetchone() is not None
        start_uv = cur.execute("PRAGMA user_version").fetchone()[0]

        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                filepath TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                ct_status TEXT,
                match_status TEXT,
                processed_at TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                converted_from_cbr INTEGER DEFAULT 0,
                file_size_mb REAL,
                tags_written TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at TEXT NOT NULL,
                estimated_calls INTEGER DEFAULT 1,
                key_id TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS statistics (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS low_confidence (
                filepath TEXT PRIMARY KEY,
                added_at TEXT,
                reason TEXT
            )
        """)

        if existed and start_uv < SCHEMA_VERSION:
            logger.info(f"Migrating database schema v{start_uv} -> v{SCHEMA_VERSION}")
            self._migrate(cur)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_pf_status ON processed_files(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_called ON api_calls(called_at)")
        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate(self, cur):
        # 1. Add any columns introduced after the existing DB was created.
        self._ensure_columns(cur, "processed_files", {
            "ct_status": "TEXT",
            "match_status": "TEXT",
            "error_message": "TEXT",
            "processed_at": "TEXT",
            "retry_count": "INTEGER DEFAULT 0",
            "converted_from_cbr": "INTEGER DEFAULT 0",
            "file_size_mb": "REAL",
            "tags_written": "TEXT",
        })
        self._ensure_columns(cur, "api_calls", {"estimated_calls": "INTEGER DEFAULT 1",
                                                "key_id": "TEXT"})

        # 2. v3.2 named the queue table "low_confidence_matches". Carry its rows over.
        legacy = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='low_confidence_matches'"
        ).fetchone()
        if legacy:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(low_confidence_matches)")}
            if "filepath" in cols:
                reason_col = "reason" if "reason" in cols else "''"
                if "added_at" in cols:
                    added_col = "added_at"
                elif "processed_at" in cols:        # v3.2 used this name
                    added_col = "processed_at"
                else:
                    added_col = "NULL"
                moved = cur.execute(f"""
                    INSERT OR IGNORE INTO low_confidence (filepath, added_at, reason)
                    SELECT filepath, COALESCE({added_col}, ?), {reason_col}
                    FROM low_confidence_matches
                """, (datetime.now().isoformat(),)).rowcount
                logger.info(f"  migrated: moved {moved} rows from low_confidence_matches")
                # v3.2 queued low-confidence files WITHOUT marking them processed, so a fresh
                # pass-1 run would re-tag them and waste API budget. Native v5.x marks them
                # 'needs_followup' (and thus skips them in pass 1). Backfill that here so the
                # resumed run behaves identically: queued for --review, skipped by pass 1.
                backfilled = cur.execute("""
                    INSERT OR IGNORE INTO processed_files (filepath, status, match_status, processed_at)
                    SELECT filepath, 'needs_followup', 'low_confidence_match', ?
                    FROM low_confidence
                """, (datetime.now().isoformat(),)).rowcount
                if backfilled:
                    logger.info(f"  migrated: marked {backfilled} queued files 'needs_followup'")

    def _ensure_columns(self, cur, table: str, cols: Dict[str, str]):
        existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                logger.info(f"  migrated: added {table}.{name}")

    # -- processed files ---------------------------------------------------

    def seen_paths(self) -> Set[str]:
        """Every filepath ComicTagger has already touched, any status.
        The primary scan skips these; each status has its own reprocessing path."""
        return {r[0] for r in self.conn.execute("SELECT filepath FROM processed_files")}

    def mark_processed(self, filepath: str, status: str, error_message: str = None,
                       ct_status: str = None, match_status: str = None,
                       converted_from_cbr: bool = False, file_size_mb: float = None,
                       tags_written: str = None):
        """Upsert that preserves retry_count (a separate counter)."""
        self.conn.execute("""
            INSERT INTO processed_files
                (filepath, status, ct_status, match_status, processed_at, error_message,
                 converted_from_cbr, file_size_mb, tags_written, retry_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(filepath) DO UPDATE SET
                status = excluded.status,
                ct_status = excluded.ct_status,
                match_status = excluded.match_status,
                processed_at = excluded.processed_at,
                error_message = excluded.error_message,
                converted_from_cbr = CASE WHEN excluded.converted_from_cbr THEN 1 ELSE converted_from_cbr END,
                file_size_mb = COALESCE(excluded.file_size_mb, file_size_mb),
                tags_written = COALESCE(excluded.tags_written, tags_written)
        """, (filepath, status, ct_status, match_status, datetime.now().isoformat(),
              error_message, int(converted_from_cbr), file_size_mb, tags_written))
        self.conn.commit()

    def get_retry_count(self, filepath: str) -> int:
        row = self.conn.execute(
            "SELECT retry_count FROM processed_files WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row[0] if row else 0

    def increment_retry(self, filepath: str):
        self.conn.execute(
            "UPDATE processed_files SET retry_count = retry_count + 1 WHERE filepath = ?", (filepath,)
        )
        self.conn.commit()

    # -- follow-up queue ---------------------------------------------------

    def add_low_confidence(self, filepath: str, reason: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO low_confidence (filepath, added_at, reason) VALUES (?, ?, ?)",
            (filepath, datetime.now().isoformat(), reason)
        )
        self.conn.commit()

    def get_low_confidence_files(self) -> List[Tuple[str, str]]:
        return self.conn.execute(
            "SELECT filepath, reason FROM low_confidence ORDER BY added_at"
        ).fetchall()

    def remove_low_confidence(self, filepath: str):
        self.conn.execute("DELETE FROM low_confidence WHERE filepath = ?", (filepath,))
        self.conn.commit()

    def remove_path(self, filepath: str):
        """Drop a file from the progress tables (used when deleting duplicates)."""
        self.conn.execute("DELETE FROM processed_files WHERE filepath = ?", (filepath,))
        self.conn.execute("DELETE FROM low_confidence WHERE filepath = ?", (filepath,))
        self.conn.commit()

    # -- failed files ------------------------------------------------------

    def get_failed_files(self, include_permanent: bool = False):
        if include_permanent:
            return self.conn.execute("""
                SELECT filepath, status, retry_count, error_message
                FROM processed_files WHERE status IN ('error', 'permanent_error')
                ORDER BY processed_at DESC
            """).fetchall()
        return self.conn.execute("""
            SELECT filepath, status, retry_count, error_message
            FROM processed_files WHERE status = 'error'
            ORDER BY processed_at DESC
        """).fetchall()

    def get_retryable_paths(self, max_retries: int) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT filepath FROM processed_files WHERE status = 'error' AND retry_count < ?",
            (max_retries,)
        ).fetchall()]

    # -- rate limiting -----------------------------------------------------

    def record_api_invocation(self, estimated_calls: int = 4, key_id: str = None):
        self.conn.execute(
            "INSERT INTO api_calls (called_at, estimated_calls, key_id) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), estimated_calls, key_id)
        )
        self.conn.commit()

    def estimated_calls_last_hour(self, key_id: str = None) -> int:
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        if key_id is None:
            return self.conn.execute(
                "SELECT COALESCE(SUM(estimated_calls), 0) FROM api_calls WHERE called_at > ?",
                (cutoff,)).fetchone()[0]
        return self.conn.execute(
            "SELECT COALESCE(SUM(estimated_calls), 0) FROM api_calls "
            "WHERE called_at > ? AND key_id = ?", (cutoff, key_id)).fetchone()[0]

    def cleanup_old_api_calls(self):
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        self.conn.execute("DELETE FROM api_calls WHERE called_at <= ?", (cutoff,))
        self.conn.commit()

    # -- statistics --------------------------------------------------------

    def increment_stat(self, key: str, amount: int = 1):
        self.conn.execute("""
            INSERT INTO statistics (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + ?
        """, (key, amount, amount))
        self.conn.commit()

    def get_stats(self) -> Dict[str, int]:
        return dict(self.conn.execute("SELECT key, value FROM statistics").fetchall())

    def status_summary(self) -> Dict[str, int]:
        return dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM processed_files GROUP BY status"
        ).fetchall())

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self):
        self.conn.close()
        if self._lock_fh:
            _unlock_file(self._lock_fh)
            self._lock_fh.close()
            try:
                os.unlink(self._lock_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# ComicTagger result
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class CTResult:
    returncode: int
    status: Optional[str]            # ComicTagger Status enum, or None if unparsed
    match_status: Optional[str]      # ComicTagger MatchStatus enum, or None
    tags_written: List[str]
    raw: str                         # combined stdout+stderr, for diagnostics


def _extract_json(stdout: str) -> Optional[dict]:
    """Pull ComicTagger's -j result object out of stdout, ignoring any
    human-readable prefix/suffix lines. Returns the first dict that has a 'status' key."""
    decoder = json.JSONDecoder()
    idx = stdout.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(stdout, idx)
            if isinstance(obj, dict) and "status" in obj:
                return obj
        except json.JSONDecodeError:
            pass
        idx = stdout.find("{", idx + 1)
    return None


# ---------------------------------------------------------------------------
# Core tagger
# ---------------------------------------------------------------------------
class CLICLOTagger:
    def __init__(self, config: Dict[str, str], pushover: PushoverNotifier):
        self.comics_path = Path(config["comics_path"]) if config["comics_path"] else None
        self.comictagger_path = config["comictagger_path"].strip()
        # Effective key list: the single key plus any comma-separated extras, deduped,
        # order preserved. With one key this behaves exactly as before (no rotation).
        keys = [config.get("comicvine_api_key", "").strip()]
        keys += [k.strip() for k in config.get("comicvine_api_keys", "").split(",")]
        seen_k: Set[str] = set()
        self.api_keys = [k for k in keys if k and not (k in seen_k or seen_k.add(k))]
        self.api_key = self.api_keys[0] if self.api_keys else ""   # default / single-key path
        self._key_cooldowns: Dict[str, datetime] = {}              # fingerprint -> cooldown-until
        self.safe_invocations = int(config["safe_invocations_per_hour"])
        self.calls_per_invocation = int(config["api_calls_per_invocation"])
        self.max_retries = int(config["max_retries"])
        self.tag_format = self._validate_tag_format(config["tag_format"])
        self.repair_failed = config.get("repair_failed_archives", "true").strip().lower() == "true"
        # Egress: a "direct" env (proxy vars stripped) and, if a proxy is set, a "proxy" env.
        self.proxy = config.get("proxy", "").strip()
        self.rotate_egress = config.get("rotate_egress", "false").strip().lower() == "true"
        _PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        base = {k: v for k, v in os.environ.items() if k not in _PROXY_VARS}
        self._env_direct = dict(base)
        self._env_proxy = dict(base)
        if self.proxy:
            for var in _PROXY_VARS:
                self._env_proxy[var] = self.proxy
        # Routes for rotation: direct first, then proxy. Each key binds to one route by
        # index, so a given key always exits from the same IP (looks like a stable user).
        self.routes: List[Tuple[str, Dict[str, str]]] = [("direct", self._env_direct)]
        if self.proxy and self.rotate_egress:
            self.routes.append(("proxy", self._env_proxy))
        # Env used when NOT rotating: all calls via proxy if one is set, else direct.
        self._env = self._env_proxy if (self.proxy and not self.rotate_egress) else self._env_direct
        self.db = CLICLODatabase(config["db_path"])
        self.pushover = pushover

        self.exe = self._find_executable()
        self.converted_this_session: Set[str] = set()
        self._consecutive_api_errors = 0
        self.dry_run = False
        self.accept_low_confidence = False   # opt-in blind low-confidence accept in --auto-retry

        # Resolve flag names that drift between ComicTagger betas, then version-check.
        self._flag_abort = "--abort"
        self._flag_accept = "--no-abort"
        self._has_no_year = True
        self._resolve_flags()

        if len(self.api_keys) > 1:
            fps = ", ".join(self._key_fp(k) for k in self.api_keys)
            logger.warning(f"EXPERIMENTAL multi-key rotation ON: {len(self.api_keys)} keys "
                           f"(…{fps}). This likely violates ComicVine's per-user rate limit and "
                           f"can get your keys and IP banned. All traffic is from one machine, so "
                           f"rotation may not even raise your effective ceiling. You accepted this.")

        if self.proxy and self.rotate_egress and len(self.routes) > 1:
            binding = ", ".join(f"…{self._key_fp(k)}→{self.routes[i % len(self.routes)][0]}"
                                for i, k in enumerate(self.api_keys))
            logger.warning(f"EXPERIMENTAL egress rotation ON: keys bound to routes [{binding}]. "
                           f"This is deliberate IP rotation on top of key rotation to get around a "
                           f"per-user limit; VPN/Tor exit IPs can be flagged faster, not slower.")
        elif self.proxy:
            logger.info(f"Routing ComicVine requests through proxy {self.proxy} (same public IP "
                        f"unless this proxy is chained to a VPN/Tor upstream).")

        if not self.api_key:
            logger.warning("No ComicVine API key set. Online tagging will fail. "
                           "Run --init-config and edit cliclo.ini, or set CLICLO_COMICVINE_API_KEY.")
        self._check_version()

    @staticmethod
    def _validate_tag_format(raw: str) -> str:
        fmt = (raw or "CR").strip().upper()
        valid = {f for f in fmt.split(",") if f}
        if valid - {"CR", "CBL", "COMET"}:
            logger.warning(f"tag_format '{raw}' contains values stock ComicTagger 1.6.x rejects. "
                           "CR is the standard target (CIX/ComicInfo.xml is written under CR; "
                           "toggle it with --cr/--no-cr). Falling back to CR.")
            return "CR"
        return fmt

    def _find_executable(self) -> Path:
        # 1. Explicit path: a file, or a directory containing the binary.
        if self.comictagger_path:
            p = Path(self.comictagger_path)
            if p.is_file():
                return p
            for name in ("comictagger.exe", "comictagger"):
                cand = p / name
                if cand.exists():
                    return cand
        # 2. On PATH (covers pip installs).
        found = shutil.which("comictagger") or shutil.which("comictagger.exe")
        if found:
            return Path(found)
        raise FileNotFoundError(
            "ComicTagger executable not found. Set comictagger_path in cliclo.ini to the "
            "install directory or binary, or ensure 'comictagger' is on your PATH "
            "(pip install --pre comictagger)."
        )

    def _check_version(self):
        import re
        try:
            out = subprocess.run([str(self.exe), "--version"],
                                 capture_output=True, text=True, timeout=15)
            lines = [l.strip() for l in (out.stdout + out.stderr).splitlines() if l.strip()]
            # The version sits on a line like "ComicTagger 1.6.0b11.dev0: ...", not on the
            # trailing Apache-license line. Find the line that actually names a version.
            ver_line = next((l for l in lines if re.search(r"comictagger\s+v?\d+\.\d+", l, re.I)), "")
            if not ver_line:
                ver_line = next((l for l in lines if re.search(r"\bv?\d+\.\d+\.\d+", l)
                                 and "apache" not in l.lower()), "")
            logger.info(f"ComicTagger: {ver_line or (lines[0] if lines else 'unknown')}")
            m = re.search(r"(\d+\.\d+\.\d+[A-Za-z0-9.]*)", ver_line)
            ver_num = m.group(1) if m else ""
            if ver_num.startswith("1.5"):
                logger.warning("This looks like ComicTagger 1.5.x, whose CLI is INCOMPATIBLE "
                               "with CLICLO. Install the 1.6.x line: pip install --pre comictagger")
        except Exception as e:
            logger.warning(f"Could not determine ComicTagger version: {e}")

    def _resolve_flags(self):
        """ComicTagger's low-confidence flag was renamed between betas:
            1.6.0b9 and earlier : --abort / --no-abort
            1.6.0b11+           : --no-save-on-low-confidence / --save-on-low-confidence
        Hardcoding either breaks on the other, and argparse prefix-matching makes it
        worse: passing --abort to a b11 build silently resolves to the unrelated
        --abort-on-conflict. So probe --help once and use the names that actually exist."""
        help_text = ""
        try:
            h = subprocess.run([str(self.exe), "--help"], capture_output=True, text=True, timeout=15)
            help_text = h.stdout + h.stderr
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read ComicTagger --help for flag detection: {e}")

        if "--no-save-on-low-confidence" in help_text:          # b11+
            self._flag_abort = "--no-save-on-low-confidence"
            self._flag_accept = "--save-on-low-confidence"
        elif "--no-abort" in help_text:                          # b9 and earlier
            self._flag_abort = "--abort"
            self._flag_accept = "--no-abort"
            logger.warning(
                "ComicTagger looks like a pre-b11 beta. Ambiguous matches (multiple / "
                "low-confidence) can crash this build with an AssertionError instead of "
                "being handled cleanly, and --review may not reach its prompt. This is a "
                "ComicTagger bug, not CLICLO. Upgrade to 1.6.0b11+ for reliable pass-2 and "
                "pass-3 behaviour: pip install --pre comictagger")
        elif help_text:
            self._flag_abort = None
            self._flag_accept = None
            logger.warning("No low-confidence flag found in ComicTagger's help; relying on "
                           "its default match-confidence behaviour.")

        self._has_no_year = (not help_text) or ("--no-use-year-when-identifying" in help_text)

    # -- rate limiting -----------------------------------------------------

    @staticmethod
    def _key_fp(key: str) -> str:
        """Short, log-safe fingerprint of a key (last 4 chars). Never log full keys."""
        return key[-4:] if key else "none"

    def _route_for_key(self, key: str) -> Tuple[str, Dict[str, str]]:
        """Bind each key to one egress route by index, so a key always exits the same IP.
        With rotation off (or no proxy) every key uses the single default env."""
        if not (self.rotate_egress and len(self.routes) > 1):
            return ("proxy" if (self.proxy and not self.rotate_egress) else "direct", self._env)
        try:
            ki = self.api_keys.index(key)
        except ValueError:
            ki = 0
        return self.routes[ki % len(self.routes)]

    def _select_and_wait_key(self) -> str:
        """Return a key with remaining hourly budget, waiting if necessary.

        Single-key setups just enforce the one budget (old behaviour). Multi-key
        setups pick the first key that is both under budget and not in a velocity
        cooldown; if none qualifies, wait until the soonest one frees up. The budget
        is per key because ComicVine's limit is per user/key."""
        budget = self.safe_invocations * self.calls_per_invocation
        notified = False
        while True:
            self.db.cleanup_old_api_calls()
            now = datetime.now()
            best_wait = None
            for key in self.api_keys:
                fp = self._key_fp(key)
                cooldown = self._key_cooldowns.get(fp)
                if cooldown and cooldown > now:
                    best_wait = min(best_wait or cooldown, cooldown)
                    continue
                used = self.db.estimated_calls_last_hour(fp if len(self.api_keys) > 1 else None)
                if used < budget:
                    if len(self.api_keys) > 1:
                        if self.rotate_egress and len(self.routes) > 1:
                            rn, _ = self._route_for_key(key)
                            logger.info(f"  using key …{fp} via {rn} "
                                        f"(~{used}/{budget} est. calls this hour)")
                        else:
                            logger.info(f"  using key …{fp} (~{used}/{budget} est. calls this hour)")
                    return key
            # Every key is exhausted or cooling down. Wait, then re-check.
            if not notified:
                logger.info(f"All {len(self.api_keys)} key(s) at hourly budget (~{budget}/key). Pausing.")
                self.pushover.notify_rate_pause(60, reason="hourly budget (all keys)")
                notified = True
            time.sleep(min(60, max(5, int((best_wait - now).total_seconds())) if best_wait else 60))

    def _mark_key_cooldown(self, key: str, minutes: int = 5):
        fp = self._key_fp(key)
        self._key_cooldowns[fp] = datetime.now() + timedelta(minutes=minutes)
        if len(self.api_keys) > 1:
            logger.warning(f"  key …{fp} hit a velocity limit; cooling it {minutes} min, "
                           f"preferring other keys")

    def _cooldown(self, minutes: int = 10, reason: str = "consecutive API errors"):
        logger.warning(f"Cooling down {minutes} min ({reason})")
        self.pushover.notify_rate_pause(minutes, reason=reason)
        for remaining in range(minutes * 60, 0, -30):
            logger.info(f"  resuming in {remaining // 60}:{remaining % 60:02d}")
            time.sleep(30)

    # -- discovery ---------------------------------------------------------

    def get_comic_files(self, root: Path, include_cbr: bool = True) -> List[Path]:
        exts = {".cbz", ".cbr"} if include_cbr else {".cbz"}
        logger.info(f"Scanning {root} ...")
        all_files: List[Path] = []
        for ext in exts:
            all_files.extend(root.rglob(f"*{ext}"))
        seen = self.db.seen_paths()
        todo = [f for f in all_files if str(f) not in seen]
        logger.info(f"Found {len(all_files):,} comics; {len(todo):,} new (rest already in DB)")
        return todo

    @staticmethod
    def _size_mb(path: Path) -> float:
        try:
            return path.stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0

    # -- CBR -> CBZ --------------------------------------------------------

    def convert_cbr_to_cbz(self, cbr: Path, keep_original: bool = True) -> Optional[Path]:
        size_mb = self._size_mb(cbr)
        logger.info(f"  Converting CBR -> CBZ: {cbr.name} ({size_mb:.0f} MB)")
        timeout = max(120, 120 + int(size_mb / 100 * 60))

        on_network = str(cbr).startswith("\\\\")
        if size_mb > 1000 and on_network:
            logger.info("    Large network file; copying local for conversion")
            local = self._copy_to_temp(cbr, timeout=timeout)
            if not local:
                return None
            result = self._run_conversion(local, keep_original=False, timeout=timeout)
            if not result:
                self._safe_unlink(local)
                return None
            final = cbr.with_suffix(".cbz")
            try:
                shutil.move(str(result), str(final))
                self._safe_unlink(local)
                if not keep_original:
                    self._safe_unlink(cbr)
                self.converted_this_session.add(str(final))
                self.db.increment_stat("cbr_converted")
                return final
            except Exception as e:
                logger.error(f"    Move back failed: {e}")
                return None

        return self._run_conversion(cbr, keep_original, timeout)

    def _copy_to_temp(self, src: Path, timeout: int) -> Optional[Path]:
        """Copy under a watchdog so a stalled network read can't hang the run forever."""
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "cliclo_temp"
            tmp_dir.mkdir(exist_ok=True)
            dest = tmp_dir / src.name
        except Exception as e:
            logger.error(f"    Temp setup failed: {e}")
            return None

        err: Dict[str, Exception] = {}

        def _do():
            try:
                shutil.copy2(str(src), str(dest))
            except Exception as e:  # noqa: BLE001
                err["e"] = e

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.error(f"    Local copy timed out after {timeout}s; skipping {src.name}")
            return None
        if "e" in err:
            logger.error(f"    Local copy failed: {err['e']}")
            self._safe_unlink(dest)
            return None
        return dest

    def _run_conversion(self, cbr: Path, keep_original: bool, timeout: int) -> Optional[Path]:
        cmd = [str(self.exe), "--no-gui", "-e"]
        if not keep_original:
            cmd.append("--delete-original")
        cmd.append(str(cbr))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
            cbz = cbr.with_suffix(".cbz")
            if cbz.exists():
                logger.info(f"    Converted: {cbz.name}")
                self.converted_this_session.add(str(cbz))
                self.db.increment_stat("cbr_converted")
                return cbz
            # No CBZ produced. Surface ComicTagger's own reason instead of swallowing it.
            combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            low = combined.lower()
            lines = [l for l in combined.splitlines() if l.strip()]

            # Case 1: the ".cbr" is actually a ZIP. It needs no conversion; just give it the
            # right extension so the tagging phase will pick it up (lossless rename, no re-zip).
            if "already a zip" in low:
                target = cbr.with_suffix(".cbz")
                try:
                    if target.exists():
                        logger.info(f"    Already CBZ-compatible (ZIP); using {target.name}")
                    else:
                        cbr.rename(target)
                        logger.info(f"    Already ZIP-format, no conversion needed; "
                                    f"renamed to {target.name}")
                    self.converted_this_session.add(str(target))
                    self.db.increment_stat("cbr_converted")
                    return target
                except OSError as e:
                    logger.error(f"    Already a ZIP but rename failed: {e}")
                    return None

            # Case 2: real RAR, but no unrar binary to extract with (rarfile is only a shim).
            if "rar unavailable" in low or "no module named 'rarfile'" in low or \
               ("copying to zip archive []" in low):
                logger.error("    Conversion failed: real RAR, but ComicTagger can't extract it here.")
                if not getattr(self, "_rar_hint_shown", False):
                    self._rar_hint_shown = True
                    logger.error("    >>> Install RAR support once: `pip install rarfile` AND put an "
                                 "unrar BINARY on PATH (UnRAR.exe from rarlab, or WinRAR). rarfile "
                                 "alone is not enough; it shells out to unrar to extract.")
                # Repack can't help a RAR without an unrar tool, so don't bother trying.
                return None

            # Case 3: some other failure (corrupt timestamp, unidentified container). Try a
            # tolerant repack with independent tooling before giving up.
            reason = lines[-1] if lines else f"exit {proc.returncode}, no output"
            logger.warning(f"    ComicTagger couldn't convert it ({reason})")
            if self.repair_failed:
                repacked = self._repack_to_cbz(cbr, keep_original=keep_original)
                if repacked:
                    return repacked
            return None
        except subprocess.TimeoutExpired:
            logger.error(f"    Conversion timed out ({timeout}s): {cbr.name}")
            return None
        except Exception as e:
            logger.error(f"    Conversion error: {e}")
            return None

    def _repack_to_cbz(self, src: Path, keep_original: bool = True) -> Optional[Path]:
        """Last-resort tolerant repack. When ComicTagger's own export fails on a recoverable
        problem (a corrupt per-file timestamp it tries to copy, or a container it won't
        identify), extract the page images with independent tooling and write a fresh CBZ
        with reset metadata, sidestepping whatever ComicTagger choked on.

        All-or-nothing: if any page can't be read, it aborts rather than produce a comic
        missing pages. Returns the new .cbz path, or None if the file can't be salvaged."""
        import zipfile
        IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif"}

        if self._size_mb(src) > 500:
            logger.error(f"    Repair skipped (>500 MB): {src.name}")
            return None
        try:
            with open(src, "rb") as fh:
                magic = fh.read(8)
        except OSError as e:
            logger.error(f"    Repair could not open {src.name}: {e}")
            return None

        names_data: List[Tuple[str, bytes]] = []
        try:
            if magic[:4] == b"PK\x03\x04":
                with zipfile.ZipFile(src) as zf:
                    members = [n for n in zf.namelist()
                               if not n.endswith("/") and Path(n).suffix.lower() in IMAGE_EXT]
                    for n in members:
                        names_data.append((Path(n).name, zf.read(n)))
            elif magic[:4] == b"Rar!":
                if not (shutil.which("unrar") or shutil.which("unar") or shutil.which("bsdtar")):
                    return None   # RAR needs an extractor; Phase-1 already warned about this
                try:
                    import rarfile
                except ImportError:
                    return None
                with rarfile.RarFile(src) as rf:
                    members = [n for n in rf.namelist()
                               if not n.endswith("/") and Path(n).suffix.lower() in IMAGE_EXT]
                    for n in members:
                        names_data.append((Path(n).name, rf.read(n)))
            else:
                logger.error(f"    Repair can't read {src.name}: not a ZIP or RAR container "
                             f"(magic {magic[:4]!r}).")
                return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"    Repair failed to extract {src.name}: {e}")
            return None

        if not names_data:
            logger.error(f"    Repair found no page images in {src.name}; leaving it alone.")
            return None

        target = src.with_suffix(".cbz")
        tmp = src.with_suffix(".cbz.repacktmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
                for name, data in sorted(names_data, key=lambda x: x[0]):
                    zi = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
                    out.writestr(zi, data)
            os.replace(str(tmp), str(target))
        except Exception as e:  # noqa: BLE001
            logger.error(f"    Repair write failed for {src.name}: {e}")
            self._safe_unlink(tmp)
            return None

        logger.info(f"    Repaired -> {target.name} ({len(names_data)} pages, metadata reset)")
        self.converted_this_session.add(str(target))
        self.db.increment_stat("repaired")
        if src.suffix.lower() == ".cbr" and target != src and not keep_original:
            self._safe_unlink(src)
        return target

    @staticmethod
    def _safe_unlink(p: Path):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    def _build_cmd(self, comic: Path, *, use_abort: bool, use_year: bool,
                   parse_filename: bool = True, interactive: bool = False,
                   extra: List[str] = None) -> List[str]:
        cmd = [str(self.exe), "--no-gui"]
        if interactive:
            cmd.append("-i")                 # native candidate prompt; NOTE: disables -j
        else:
            cmd.append("-j")                 # structured JSON result on stdout
        cmd += [
            "-s",                                # save
            "-o",                                # online
            "--source", "comicvine",
            "--comicvine-key", self.api_key,
            "--cv-use-series-start-as-volume",
            "--tags-write", self.tag_format,
            "--clear-tags",
        ]
        if parse_filename:
            cmd.append("-f")
        if use_year:
            cmd.append("--use-year-when-identifying")
        elif self._has_no_year:
            cmd.append("--no-use-year-when-identifying")
        flag = self._flag_abort if use_abort else self._flag_accept
        if flag:
            cmd.append(flag)
        if self.dry_run and not interactive:    # never dry-run an interactive session; the human expects writes
            cmd.append("-n")
        if extra:
            cmd.extend(extra)
        cmd.append(str(comic))
        return cmd

    @staticmethod
    def _yaml_quote(value: str) -> str:
        # YAML single-quote escaping: wrap in single quotes, double any internal single quote.
        return "'" + value.replace("'", "''") + "'"

    def _run(self, cmd: List[str], timeout: int = 180) -> CTResult:
        key = self._select_and_wait_key()
        cmd = self._with_key(cmd, key)
        fp = self._key_fp(key)
        _, env = self._route_for_key(key)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  env=env, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return CTResult(returncode=-1, status=None, match_status=None,
                            tags_written=[], raw="timeout")
        except Exception as e:  # noqa: BLE001
            return CTResult(returncode=-1, status=None, match_status=None,
                            tags_written=[], raw=str(e))
        finally:
            self.db.record_api_invocation(self.calls_per_invocation,
                                          key_id=fp if len(self.api_keys) > 1 else None)

        raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
        obj = _extract_json(proc.stdout or "")
        # Cool a key ONLY when a request actually FAILED on rate limiting. A transient 420
        # that ComicTagger retried through still lands status=success, and the "slow down"
        # text is in the output; benching the key for that is wrong and starves rotation.
        if len(self.api_keys) > 1 and obj is not None and obj.get("status") == "fetch_data_failure" \
                and any(sig in raw.lower() for sig in RATE_LIMIT_SIGNALS):
            self._mark_key_cooldown(key)
        if obj is None:
            return CTResult(returncode=proc.returncode, status=None, match_status=None,
                            tags_written=[], raw=raw)
        return CTResult(
            returncode=proc.returncode,
            status=obj.get("status"),
            match_status=obj.get("match_status"),
            tags_written=obj.get("tags_written") or [],
            raw=raw,
        )

    @staticmethod
    def _with_key(cmd: List[str], key: str) -> List[str]:
        """Return a copy of cmd with the value after --comicvine-key set to `key`."""
        out = list(cmd)
        try:
            i = out.index("--comicvine-key")
            if i + 1 < len(out):
                out[i + 1] = key
        except ValueError:
            pass
        return out

    def _classify(self, r: CTResult) -> Tuple[str, Optional[str]]:
        """Map a ComicTagger result to a CLICLO outcome:
        success | permanent | api_error | needs_followup | error."""
        if r.status is None:
            # No parseable JSON. With -j, ComicTagger emits a result object for every
            # file it actually processes, so absence means we CANNOT confirm a write:
            # an incompatible build (1.5.x has no -j), a crash mid-save (some 1.6 betas
            # AssertionError on a failed match and still exit 0), or truncated output.
            # Treat as a retryable error, never as success. A false success would silently
            # leave a comic untagged while marking it done.
            tail = r.raw.strip().splitlines()[-1] if r.raw.strip() else ""
            if "assert" in r.raw.lower() and "res.md" in r.raw:
                return "error", "ComicTagger crashed on a failed match (no result emitted)"
            return "error", (f"no JSON result from ComicTagger (exit {r.returncode}); "
                             f"need 1.6.x with -j. {tail[:80]}")

        if r.status in CT_STATUS_SUCCESS:
            return "success", None
        if r.status in CT_STATUS_PERMANENT:
            return "permanent", r.status
        if r.status == "fetch_data_failure":
            low = r.raw.lower()
            if any(sig in low for sig in RATE_LIMIT_SIGNALS):
                return "api_error", "ComicVine rate limit"
            return "api_error", "ComicVine fetch failure"
        if r.status == "write_failure":
            return "error", "write failure"
        if r.status == "match_failure":
            ms = r.match_status
            if ms == "low_confidence_match":
                return "needs_followup", "Low confidence match"
            if ms == "multiple_match":
                return "needs_followup", "Multiple matches"
            if ms == "no_match":
                return "needs_followup", "No match found"
            return "needs_followup", "Match failure"
        return "error", r.status

    def tag_primary(self, comic: Path) -> CTResult:
        return self._run(self._build_cmd(comic, use_abort=True, use_year=True))

    # -- pass 2: automated re-match strategies (confidence bar kept high) ---

    def _auto_retry_strategies(self):
        """Broaden the SEARCH without lowering the match-quality bar.
        Returns (name, description, command-builder) tuples. A builder may return
        None to signal 'not applicable to this file'."""
        strategies = [
            ("broaden-noyear", "drop the year, keep high confidence",
             lambda p: self._build_cmd(p, use_abort=True, use_year=False)),
            ("series-only", "search by series name, keep high confidence",
             self._series_only_cmd),
        ]
        if self.accept_low_confidence:
            # Opt-in: the old blind-accept behaviour. Off by default because the
            # point of --review is that a human decides the ambiguous ones.
            strategies.append(
                ("accept-low", "accept ANY confidence (no human review)",
                 lambda p: self._build_cmd(p, use_abort=False, use_year=True)))
        return strategies

    def _series_only_cmd(self, comic: Path) -> Optional[List[str]]:
        series = self._series_from_filename(comic.stem)
        if not series:
            return None
        return self._build_cmd(comic, use_abort=True, use_year=False, parse_filename=False,
                               extra=["-m", f"series: {self._yaml_quote(series)}"])

    @staticmethod
    def _series_from_filename(stem: str) -> str:
        import re
        # Drop a trailing issue number and anything after it, and any (year) tail.
        s = re.sub(r"\s*\(\d{4}\).*$", "", stem)          # strip (2020)...
        s = re.sub(r"\s+#?\d+.*$", "", s)                  # strip ' 001 ...' / ' #12 ...'
        s = s.replace("_", " ").strip(" -_")
        return s

    def _md_fingerprint(self, comic: Path) -> Optional[tuple]:
        """Fingerprint of the tags currently on the file: (series, issue, title, issue_id).
        None if the file carries no tags in our format. Local print (-p), no ComicVine call.

        Used to tell whether an interactive session actually wrote a NEW match. A boolean
        'does it have tags' is not enough: comics ship with embedded ComicInfo.xml from the
        release group, so presence alone would mark untouched files as success. Comparing
        the fingerprint before and after the prompt catches a real change (at minimum
        issue_id goes from None to a ComicVine id) and treats 'skipped' as unchanged."""
        cmd = [str(self.exe), "--no-gui", "-j", "-p", "--tags-read", self.tag_format, str(comic)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
        obj = _extract_json(proc.stdout)
        if not obj or not obj.get("tags_read"):
            return None
        md = obj.get("md") or {}
        return (md.get("series"), md.get("issue"), md.get("title"), md.get("issue_id"))

    # -- batch -------------------------------------------------------------

    def process_all(self, *, retry_failed: bool = False,
                    convert_cbr: bool = True, keep_cbr: bool = True):
        if not self.comics_path:
            logger.error("No comics path set. Pass one as an argument, set comics_path "
                         "in cliclo.ini, or set CLICLO_COMICS_PATH.")
            return
        comics = self.get_comic_files(self.comics_path, include_cbr=True)

        if retry_failed:
            extra = []
            for p in self.db.get_retryable_paths(self.max_retries):
                pp = Path(p)
                try:
                    present = pp.exists()
                except OSError as e:
                    # A transient SMB/network stat error (e.g. WinError 59) must not abort
                    # the whole run. Keep the path; per-file tagging handles a missing file.
                    logger.debug(f"existence check failed for {pp} ({e}); keeping for retry")
                    present = True
                if present:
                    extra.append(pp)
            have = {str(c) for c in comics}
            comics.extend(c for c in extra if str(c) not in have)
            logger.info(f"Added {len(extra)} retryable files")

        if not comics:
            logger.info("Nothing to process.")
            return

        self.pushover.notify_startup(len(comics))

        # Phase 1: CBR -> CBZ
        if convert_cbr:
            cbrs = [c for c in comics if c.suffix.lower() == ".cbr"]
            if cbrs:
                logger.info(f"\n{'=' * 60}\nPHASE 1: converting {len(cbrs)} CBR files\n{'=' * 60}")
                if not (shutil.which("unrar") or shutil.which("unar") or shutil.which("bsdtar")):
                    logger.warning("  No unrar tool (unrar/unar/bsdtar) found on PATH. True RAR "
                                   "archives will fail to extract; install UnRAR.exe (rarlab) or "
                                   "WinRAR. ZIP-format .cbr files are still handled (renamed to .cbz).")
                for cbr in cbrs:
                    target = cbr.with_suffix(".cbz")
                    # If the CBZ already exists, conversion was done on an earlier run.
                    # Re-running it would overwrite (and blank) the tagged CBZ, so skip it.
                    # Record the CBR as handled so future scans stop re-finding it.
                    if target.exists():
                        logger.info(f"  Already converted, skipping: {cbr.name}")
                        self.db.mark_processed(str(cbr), "converted", "CBZ already present")
                        comics.remove(cbr)
                        if not keep_cbr:
                            self._safe_unlink(cbr)
                        continue
                    cbz = self.convert_cbr_to_cbz(cbr, keep_original=keep_cbr)
                    if cbz:
                        # Record the CBR itself so a later interruption won't re-convert it.
                        self.db.mark_processed(str(cbr), "converted", "converted to CBZ")
                        comics.remove(cbr)
                        if cbz not in comics:
                            comics.append(cbz)
                    else:
                        logger.warning(f"  keeping unconverted CBR: {cbr.name}")

        # Phase 2: tag
        total = len(comics)
        s_ok = s_err = s_skip = s_follow = 0
        logger.info(f"\nPHASE 2: tagging {total} comics  (key {self.api_key[:6] or '----'}…, "
                    f"budget ~{self.safe_invocations}/hr)")
        if self.dry_run:
            logger.info("DRY RUN: no files will be modified")
        start = time.time()

        for i, comic in enumerate(comics, 1):
            if i == 1 or i % 10 == 0:
                el = time.time() - start
                rate = i / el if el else 0
                eta = (total - i) / rate / 60 if rate else 0
                logger.info(f"\n--- {i}/{total} ({i * 100 / total:.1f}%) | "
                            f"{rate:.2f}/s | ETA {eta:.1f} min ---")
                self.pushover.notify_milestone(i, total, self.db.get_stats(), el / 3600)

            try:
                if comic.suffix.lower() == ".cbr":
                    logger.warning(f"[{i}/{total}] SKIP (CBR, unwritable): {comic.name}")
                    self.db.mark_processed(str(comic), "skipped", "CBR format")
                    self.db.increment_stat("cbr_skipped")
                    s_skip += 1
                    continue

                if self.db.get_retry_count(str(comic)) >= self.max_retries:
                    logger.warning(f"[{i}/{total}] SKIP (max retries): {comic.name}")
                    self.db.increment_stat("skip")
                    s_skip += 1
                    continue

                logger.info(f"[{i}/{total}] {comic.name}")
                result = self.tag_primary(comic)
                outcome, msg = self._classify(result)
                converted = str(comic) in self.converted_this_session
                size_mb = self._size_mb(comic)
                tw = ",".join(result.tags_written) or None

                if outcome == "success":
                    logger.info(f"  OK ({tw or 'tags written'})")
                    self.db.mark_processed(str(comic), "success", ct_status=result.status,
                                           match_status=result.match_status, converted_from_cbr=converted,
                                           file_size_mb=size_mb, tags_written=tw)
                    self.db.increment_stat("success")
                    s_ok += 1
                    self._consecutive_api_errors = 0

                elif outcome == "needs_followup":
                    logger.warning(f"  NEEDS REVIEW: {msg}")
                    self.db.mark_processed(str(comic), "needs_followup", msg, ct_status=result.status,
                                           match_status=result.match_status, converted_from_cbr=converted,
                                           file_size_mb=size_mb)
                    self.db.add_low_confidence(str(comic), msg)
                    self.db.increment_stat("needs_followup")
                    s_follow += 1
                    self._consecutive_api_errors = 0

                elif outcome == "api_error":
                    self._consecutive_api_errors += 1
                    logger.error(f"  API ERROR: {msg}")
                    self.db.mark_processed(str(comic), "error", msg, ct_status=result.status,
                                           converted_from_cbr=converted, file_size_mb=size_mb)
                    self.db.increment_retry(str(comic))
                    self.db.increment_stat("error")
                    s_err += 1
                    if self._consecutive_api_errors >= 3:
                        self.pushover.notify_errors(self._consecutive_api_errors, msg)
                        self._cooldown(10, reason="consecutive API errors")
                        self._consecutive_api_errors = 0

                elif outcome == "permanent":
                    logger.error(f"  PERMANENT: {msg}")
                    self.db.mark_processed(str(comic), "permanent_error", msg, ct_status=result.status,
                                           converted_from_cbr=converted, file_size_mb=size_mb)
                    self.db.increment_stat("permanent_error")
                    s_err += 1
                    self._consecutive_api_errors = 0

                else:  # error (retryable)
                    logger.error(f"  FAIL: {msg}")
                    self.db.mark_processed(str(comic), "error", msg, ct_status=result.status,
                                           converted_from_cbr=converted, file_size_mb=size_mb)
                    self.db.increment_retry(str(comic))
                    self.db.increment_stat("error")
                    s_err += 1
                    self._consecutive_api_errors = 0
            except Exception as e:  # noqa: BLE001
                # One file's unexpected failure (a network stat blip, a surprise out of
                # ComicTagger, anything) must never abort the batch. Mark it transient and
                # keep going; --retry-failed picks it up next time.
                logger.error(f"  unexpected error, skipping this file: {e}")
                try:
                    self.db.mark_processed(str(comic), "error", f"unexpected: {e}")
                    self.db.increment_retry(str(comic))
                    self.db.increment_stat("error")
                except Exception:  # noqa: BLE001
                    pass
                s_err += 1

            time.sleep(0.5)  # be polite to the velocity detector

        elapsed_h = (time.time() - start) / 3600
        logger.info(f"\n{'=' * 60}\nBATCH COMPLETE\n{'=' * 60}")
        self._print_stats(s_ok, s_err, s_skip, s_follow)
        logger.info(f"Duration: {elapsed_h:.2f} h")
        self.pushover.notify_completion(total, self.db.get_stats(), elapsed_h)

        if self.db.get_low_confidence_files():
            logger.info("Run --auto-retry to re-match the review queue, then --review to decide "
                        "the rest by hand.")
        if self.db.get_retryable_paths(self.max_retries):
            logger.info("Run --retry-failed to retry transient failures.")

    # -- pass 2: automated re-match runner ---------------------------------

    def process_auto_retry(self):
        """Second pass. Re-match the leftover queue with broadened search,
        keeping the confidence bar. Unresolved files stay queued for --review."""
        queue = self.db.get_low_confidence_files()
        if not queue:
            logger.info("Review queue is empty; nothing to auto-retry.")
            return
        logger.info(f"\n{'=' * 60}\nPASS 2 (auto-retry): {len(queue)} files\n{'=' * 60}")
        if self.accept_low_confidence:
            logger.info("--accept-low-confidence is ON: a final pass will accept any match "
                        "without human review.")
        strategies = self._auto_retry_strategies()
        resolved = 0
        for i, (fp, reason) in enumerate(queue, 1):
            path = Path(fp)
            if not path.exists():
                logger.warning(f"[{i}/{len(queue)}] gone: {path.name}")
                self.db.remove_low_confidence(fp)
                continue
            logger.info(f"\n[{i}/{len(queue)}] {path.name}  (was: {reason})")
            won = False
            for name, desc, builder in strategies:
                cmd = builder(path)
                if cmd is None:
                    continue
                logger.info(f"  trying: {desc}")
                outcome, msg = self._classify(self._run(cmd))
                if outcome == "success":
                    logger.info(f"  OK via {name}")
                    self.db.mark_processed(fp, "success")
                    self.db.increment_stat("followup_success")
                    self.db.remove_low_confidence(fp)
                    resolved += 1
                    won = True
                    break
                logger.info(f"    -> {msg}")
                time.sleep(0.5)
            if not won:
                # Leave it in the queue for the human pass; refresh the reason.
                self.db.add_low_confidence(fp, f"auto-retry exhausted ({reason})")
        remaining = len(self.db.get_low_confidence_files())
        logger.info(f"\nAuto-retry resolved {resolved}; {remaining} remain for --review.")

    # -- pass 3: human-adjudicated review (interactive) --------------------

    def process_review(self):
        """Third pass. Hand the human ComicTagger's native candidate prompt for each
        leftover file. The human picks a match number or skips. Not data entry.

        Runs ComicTagger with inherited stdio (NOT captured), which is the whole
        reason -i works here and hung in every previous attempt: capturing pipes
        stdin so input() blocks forever. Guarded against non-interactive use."""
        if not sys.stdin.isatty():
            logger.error("--review needs a real interactive terminal; it hands you ComicTagger's "
                         "match prompts. Run it directly in a console (not piped, not under cron).")
            return
        if not self.api_key:
            logger.error("--review needs a ComicVine API key to fetch candidate matches.")
            return
        queue = self.db.get_low_confidence_files()
        if not queue:
            logger.info("Review queue is empty.")
            return

        print("\n" + "=" * 70)
        print(f"PASS 3 (review): {len(queue)} files need a human decision")
        print("ComicTagger will show numbered candidate matches per file.")
        print("Type a number to tag, 's' to skip, or Ctrl-C to stop (progress is saved).")
        print("=" * 70)

        resolved = manual = 0
        for i, (fp, reason) in enumerate(queue, 1):
            path = Path(fp)
            if not path.exists():
                print(f"[{i}/{len(queue)}] gone: {path.name}")
                self.db.remove_low_confidence(fp)
                continue

            print(f"\n{'-' * 70}\n[{i}/{len(queue)}] {path.name}\n  (queued because: {reason})\n{'-' * 70}")
            before = self._md_fingerprint(path)
            review_key = self._select_and_wait_key()
            _rn, review_env = self._route_for_key(review_key)
            cmd = self._with_key(
                self._build_cmd(path, use_abort=True, use_year=True, interactive=True), review_key)
            try:
                # Inherit stdio: the human interacts with ComicTagger directly. No timeout,
                # because a person is at the keyboard and 's' is always available to skip.
                subprocess.run(cmd, env=review_env)
            except KeyboardInterrupt:
                print("\nReview paused. Progress saved; run --review again to continue.")
                break
            finally:
                self.db.record_api_invocation(
                    self.calls_per_invocation,
                    key_id=self._key_fp(review_key) if len(self.api_keys) > 1 else None)

            after = self._md_fingerprint(path)
            if after is not None and after != before:
                print("  -> tagged (metadata changed)")
                self.db.mark_processed(fp, "success", match_status="user_selected")
                self.db.increment_stat("review_success")
                self.db.remove_low_confidence(fp)
                resolved += 1
            else:
                # Unchanged: the human skipped, or there was nothing to choose (no_match).
                print("  -> skipped / no match chosen")
                self.db.mark_processed(fp, "manual_required", "skipped during review")
                self.db.increment_stat("manual_required")
                self.db.remove_low_confidence(fp)
                manual += 1

        print(f"\nReview session: tagged {resolved}, set aside {manual}.")
        self._show_manual_queue()

    def _show_manual_queue(self):
        rows = self.db.conn.execute(
            "SELECT filepath FROM processed_files WHERE status = 'manual_required'"
        ).fetchall()
        if rows:
            logger.info(f"\n{'=' * 60}\nSTILL UNTAGGED ({len(rows)}) — no online match to choose from.\n"
                        f"These need manual entry in the ComicTagger GUI, or leaving as-is:\n{'=' * 60}")
            for (fp,) in rows:
                logger.info(f"  {fp}")

    # -- reporting ---------------------------------------------------------

    def _print_stats(self, s=0, e=0, sk=0, f=0):
        st = self.db.get_stats()
        ok = st.get("success", 0)
        err = st.get("error", 0)
        perm = st.get("permanent_error", 0)
        total = sum(st.get(k, 0) for k in
                    ("success", "error", "permanent_error", "skip", "cbr_skipped", "needs_followup"))
        sr = (ok / total * 100) if total else 0
        logger.info(f"This run: ok={s} err={e} skipped={sk} review={f}")
        logger.info(f"Overall:  ok={ok} err={err} permanent={perm} "
                    f"converted={st.get('cbr_converted', 0)} review={st.get('needs_followup', 0)}")
        logger.info(f"Success rate: {sr:.1f}%")

    def show_failed(self):
        rows = self.db.get_failed_files(include_permanent=True)
        if not rows:
            logger.info("No failed files.")
            return
        retry = [(fp, rc, em) for fp, stt, rc, em in rows if stt != "permanent_error"]
        perm = [(fp, em) for fp, stt, _, em in rows if stt == "permanent_error"]
        logger.info(f"\n{'=' * 70}\nFAILED FILES\n{'=' * 70}")
        if retry:
            logger.info(f"\nRETRYABLE ({len(retry)}) — use --retry-failed")
            for fp, rc, em in retry:
                logger.info(f"  [{rc}/{self.max_retries}] {Path(fp).name}: {em}")
        if perm:
            logger.info(f"\nPERMANENT ({len(perm)}) — manual intervention")
            for fp, em in perm:
                logger.info(f"  {Path(fp).name}: {em}")

    _DUP_RE = re.compile(r" \(\d{1,3}\)\.cbz$", re.IGNORECASE)

    def dedupe(self, confirm: bool = False):
        """Find and (with confirm) remove the numbered duplicate CBZ files that earlier
        re-conversion runs spawned. A duplicate is 'Title (N).cbz' where a plain
        'Title.cbz' also exists. N is limited to 1-3 digits so 4-digit years like
        'Title (2016).cbz' are never matched. Dry-run unless confirm=True; the base
        'Title.cbz' is always kept."""
        if not self.comics_path:
            logger.error("No comics_path configured; set it in cliclo.ini.")
            return
        candidates: List[Path] = []
        for p in self.comics_path.rglob("*.cbz"):
            if self._DUP_RE.search(p.name):
                base = p.with_name(self._DUP_RE.sub(".cbz", p.name))
                if base.exists():
                    candidates.append(p)
        if not candidates:
            logger.info("No numbered duplicate CBZ files found.")
            return
        total_mb = sum(self._size_mb(p) for p in candidates)
        logger.info(f"Found {len(candidates):,} duplicate CBZ files (~{total_mb:.0f} MB). Each is a "
                    f"'Title (N).cbz' beside an existing 'Title.cbz' (which is kept).")
        for p in candidates[:40]:
            logger.info(f"  {p.name}")
        if len(candidates) > 40:
            logger.info(f"  ... and {len(candidates) - 40:,} more")
        if not confirm:
            logger.info("DRY RUN: nothing deleted. Review the list, then re-run with "
                        "`--dedupe --confirm` to delete these and clear their database rows.")
            return
        removed = 0
        freed = 0.0
        for p in candidates:
            mb = self._size_mb(p)
            try:
                p.unlink()
                self.db.remove_path(str(p))
                removed += 1
                freed += mb
            except OSError as e:
                logger.error(f"  could not delete {p.name}: {e}")
        logger.info(f"Deleted {removed:,} duplicate CBZ files, freed ~{freed:.0f} MB. "
                    f"Base files left untouched.")

    def show_db_info(self):
        logger.info(f"DB: {self.db.db_path}  (schema v{self.db.schema_version()})")
        total = 0
        for status, count in sorted(self.db.status_summary().items()):
            logger.info(f"  {status}: {count:,}")
            total += count
        logger.info(f"  TOTAL tracked: {total:,}")
        # Show a few stored paths. If these don't look like the paths you're scanning now
        # (e.g. a drive letter vs a UNC path), that's why resume isn't recognizing files.
        sample = self.db.conn.execute(
            "SELECT filepath FROM processed_files ORDER BY processed_at DESC LIMIT 5").fetchall()
        if sample:
            logger.info("  sample stored paths (compare these to your current scan path):")
            for (fp,) in sample:
                logger.info(f"    {fp}")

    def test_single(self, filepath: str, convert_cbr: bool = True):
        path = Path(filepath)
        if not path.exists():
            logger.error(f"Not found: {filepath}")
            return
        if path.suffix.lower() == ".cbr" and convert_cbr:
            cbz = self.convert_cbr_to_cbz(path, keep_original=True)
            if not cbz:
                logger.error("CBR conversion failed")
                return
            path = cbz
        result = self.tag_primary(path)
        outcome, msg = self._classify(result)
        logger.info(f"ComicTagger status: {result.status}  match: {result.match_status}")
        logger.info(f"tags written: {result.tags_written}")
        logger.info(f"CLICLO outcome: {outcome}" + (f" ({msg})" if msg else ""))
        logger.info(f"--- raw output ---\n{result.raw.strip()}")

    def close(self):
        self.db.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"CLICLO v{VERSION} — Command Line Interface Comic Library Organizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Pipeline:\n"
            "  cliclo /path/to/comics        Pass 1: auto-tag new comics (high confidence)\n"
            "  cliclo --auto-retry           Pass 2: re-match the leftover queue (still strict)\n"
            "  cliclo --review               Pass 3: decide conflicts by hand (interactive)\n"
            "\n"
            "Other:\n"
            "  cliclo --retry-failed         Re-run pass 1 over transient failures\n"
            "  cliclo --test comic.cbz       Tag one file, print the full result\n"
            "  cliclo --stats / --db-info    Counts / schema + status breakdown\n"
            "  cliclo --show-failed          List failed files\n"
            "  cliclo --init-config          Scaffold cliclo.ini\n"
        ),
    )
    p.add_argument("path", nargs="?", help="Comics directory (overrides config)")
    p.add_argument("--comics-path", help="Override comics path")
    p.add_argument("--comictagger-path", help="ComicTagger install dir or binary")
    p.add_argument("--api-key", help="ComicVine API key (overrides config/env)")
    p.add_argument("--api-keys", help="EXPERIMENTAL: comma-separated extra ComicVine keys to "
                                      "rotate through (likely violates ComicVine ToS; off by default)")
    p.add_argument("--db-path", help="Progress database file (overrides config). "
                                     "Point this at an existing run's DB to resume it.")
    p.add_argument("--proxy", help="HTTP(S) proxy for ComicVine requests, e.g. http://192.168.1.10:8118")
    p.add_argument("--rotate-egress", action="store_true",
                   help="EXPERIMENTAL: bind each key to its own egress (key 1 direct, key 2 via "
                        "--proxy, ...) so each exits a different IP. Needs a proxy that is a "
                        "genuinely different public IP. Off by default.")
    p.add_argument("--config", default=CONFIG_FILE, help="Config file path")

    actions = p.add_mutually_exclusive_group()
    actions.add_argument("--test", metavar="FILE", help="Tag one file and print the result")
    actions.add_argument("--auto-retry", action="store_true",
                         help="Pass 2: automated re-match of the review queue (keeps high confidence)")
    actions.add_argument("--review", action="store_true",
                         help="Pass 3: interactively decide conflicts (needs a terminal)")
    actions.add_argument("--stats", action="store_true", help="Show statistics")
    actions.add_argument("--db-info", action="store_true", help="Show schema version + status counts")
    actions.add_argument("--show-failed", action="store_true", help="List failed files")
    actions.add_argument("--dedupe", action="store_true",
                         help="Find numbered duplicate CBZs ('Title (N).cbz' beside 'Title.cbz') "
                              "left by older re-conversion runs. Dry-run unless --confirm is given.")
    actions.add_argument("--reset-db", action="store_true", help="Delete the database and exit")
    actions.add_argument("--init-config", action="store_true", help="Write cliclo.ini and exit")

    p.add_argument("--confirm", action="store_true",
                   help="Required to actually delete with --dedupe (otherwise it only lists).")

    p.add_argument("--no-resume", action="store_true", help="Start fresh (deletes the DB)")
    p.add_argument("--retry-failed", action="store_true", help="Include retryable failures in pass 1")
    p.add_argument("--accept-low-confidence", action="store_true",
                   help="In --auto-retry, add a final pass that accepts ANY match without review")
    p.add_argument("--no-convert-cbr", action="store_true", help="Skip CBR->CBZ conversion")
    p.add_argument("--delete-cbr", action="store_true", help="Delete CBR originals after conversion")
    p.add_argument("--dry-run", action="store_true", help="Preview; do not modify files")

    p.add_argument("--no-pushover", action="store_true", help="Disable notifications")
    p.add_argument("--test-pushover", action="store_true", help="Send a test notification and exit")
    return p


def _delete_db(db_path: str):
    for f in (db_path, db_path + ".lock", db_path + "-wal", db_path + "-shm"):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass


# Database filenames used by earlier versions of this tool, in priority order.
LEGACY_DB_NAMES = ("cliclo_progress.db", "comic_tagger_progress.db")


def _warn_if_legacy_db_present(db_path: str, explicit: bool):
    """If the chosen DB doesn't exist but an older run's DB does, point the user at it.
    Silent fresh-starts over a real backlog are the failure mode here."""
    if os.path.exists(db_path) or explicit:
        return
    target_dir = os.path.dirname(os.path.abspath(db_path))
    for name in LEGACY_DB_NAMES:
        cand = os.path.join(target_dir, name)
        if os.path.abspath(cand) == os.path.abspath(db_path):
            continue
        if os.path.exists(cand):
            logger.warning(
                f"No database at '{db_path}', but an existing run's database is here: '{name}'. "
                f"Starting now would re-process everything already done. To resume that run, "
                f"either set db_path = {name} in cliclo.ini, or run with --db-path {name}. "
                f"(CLICLO will migrate it in place and back it up first.)")
            return


def main():
    args = build_parser().parse_args()

    if args.init_config:
        write_default_config(args.config)
        print(f"Wrote {args.config}. Edit it to add your ComicVine key, comics path, "
              f"and Pushover tokens.")
        return

    # First-run convenience: if there's no config yet, scaffold one with sane defaults and
    # say so, then carry on. We never overwrite an existing config. If the essentials are
    # still blank after this, the usual 'set your key / path' messages will guide the user.
    if not os.path.exists(args.config):
        write_default_config(args.config)
        logger.info(f"First run: created {os.path.abspath(args.config)} with default settings. "
                    f"Edit it to set comicvine_api_key and comics_path (or pass them as "
                    f"arguments / CLICLO_* env vars).")

    config = load_config(args.config)
    if args.path:
        config["comics_path"] = args.path
    if args.comics_path:
        config["comics_path"] = args.comics_path
    if args.comictagger_path:
        config["comictagger_path"] = args.comictagger_path
    if args.api_key:
        config["comicvine_api_key"] = args.api_key
    if args.api_keys:
        config["comicvine_api_keys"] = args.api_keys
    if args.db_path:
        config["db_path"] = args.db_path
    if args.proxy:
        config["proxy"] = args.proxy
    if args.rotate_egress:
        config["rotate_egress"] = "true"

    # Resume safety net: if the configured DB doesn't exist yet but a database from an
    # older CLICLO/DarkTagger run is sitting next to it, say so loudly. Starting fresh
    # over an existing backlog would re-process thousands of files and burn API budget.
    _warn_if_legacy_db_present(config["db_path"], explicit=bool(args.db_path))

    po_enabled = config["pushover_enabled"].strip().lower() == "true" and not args.no_pushover
    pushover = PushoverNotifier(config["pushover_api_token"], config["pushover_user_key"],
                                config["pushover_device"], enabled=po_enabled)

    if args.test_pushover:
        ok = pushover.send("\U0001f9ea <b>CLICLO test</b>\nNotifications are working.",
                           title="CLICLO Test", sound="bike")
        print("Sent." if ok else "Failed; check credentials in cliclo.ini.")
        return

    if args.reset_db:
        _delete_db(config["db_path"])
        print("Database reset.")
        return

    if args.no_resume:
        _delete_db(config["db_path"])
        logger.info("Starting fresh.")

    try:
        tagger = CLICLOTagger(config, pushover)
        tagger.dry_run = args.dry_run
        tagger.accept_low_confidence = args.accept_low_confidence

        if args.test:
            tagger.test_single(args.test, convert_cbr=not args.no_convert_cbr)
        elif args.auto_retry:
            tagger.process_auto_retry()
        elif args.review:
            tagger.process_review()
        elif args.stats:
            tagger._print_stats()
        elif args.db_info:
            tagger.show_db_info()
        elif args.show_failed:
            tagger.show_failed()
        elif args.dedupe:
            tagger.dedupe(confirm=args.confirm)
        else:
            tagger.process_all(retry_failed=args.retry_failed,
                               convert_cbr=not args.no_convert_cbr,
                               keep_cbr=not args.delete_cbr)

        tagger.close()
    except KeyboardInterrupt:
        logger.info("\nInterrupted. Progress saved; run again to resume.")
        pushover.notify_interrupted()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Fatal error: {e}", exc_info=True)
        pushover.notify_crash(str(e))
        sys.exit(1)


if __name__ == "__main__":
    print_banner()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler("cliclo.log", encoding="utf-8"), logging.StreamHandler()],
    )
    logger.info(f"CLICLO v{VERSION}")
    main()
