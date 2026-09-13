"""Refreshable local Cursor chat cost dashboard.

Reads Cursor's billed usage events (same metering as cursor.com/dashboard) using
the IDE's logged-in session, then joins them to local chat titles and repos.

Costs are Cursor's own `tokenUsage.totalCents` / `chargedCents` figures:
included plan usage plus on-demand overage. Subscription invoices (Pro / Pro+
monthly fee) are listed separately and are not model usage.

Usage:  python cursor_dashboard.py [--port 8787] [--db PATH]
        [--import-ide] [--merge-db PATH] [--cloud-agents]

Cloud agent chats (bc-*) are merged from the Cloud Agents API when
CLOUD_AGENTS_API_KEY or CURSOR_API_KEY is set (Cursor Dashboard → API Keys),
so local IDE sessions and ALL cloud agent sessions appear in one cost dashboard.

A daily digest emails the signed-in Cursor license address on the first refresh
of each day via the Gmail API (override with --email-to / --no-digest).
"""

import argparse
import base64
import collections
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

CHARS_PER_TOKEN = 4
CONTEXT_WINDOW_TOKENS = 200_000
MIN_DAY, MAX_DAY = "0000-01-01", "9999-12-31"
DEFAULT_BUDGET = 70.0  # USD, Pro Plus included usage; overridden by the billing API when available

# (input, output, cache_read) USD per 1M tokens. Aliases are normalized separately.
MODEL_RATES = {
    "auto": (1.25, 6.00, 0.25),
    "default": (1.25, 6.00, 0.25),
    "composer-1.5": (3.50, 17.50, 0.35),
    "composer-2": (0.50, 2.50, 0.20),
    "composer-2-fast": (1.50, 7.50, 0.35),
    "composer-2.5": (0.50, 2.50, 0.20),
    "composer-2.5-fast": (1.50, 7.50, 0.35),
    "claude-4-sonnet": (3.00, 15.00, 0.30),
    "claude-4.5-sonnet": (3.00, 15.00, 0.30),
    "claude-4.5-sonnet-thinking": (3.00, 15.00, 0.30),
    "claude-4.6-sonnet": (3.00, 15.00, 0.30),
    "claude-sonnet-4.6": (3.00, 15.00, 0.30),
    "claude-sonnet-5": (3.00, 15.00, 0.30),
    "claude-4.6-opus": (5.00, 25.00, 0.50),
    "claude-opus-4.6": (5.00, 25.00, 0.50),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-5-thinking": (5.00, 25.00, 0.50),
    "claude-opus-5-thinking-high": (5.00, 25.00, 0.50),
    "claude-haiku-4.5": (1.00, 5.00, 0.10),
    "grok-4.5": (1.25, 6.00, 0.25),
    "grok-4.6": (1.25, 6.00, 0.25),
    "gpt-5": (1.25, 10.00, 0.125),
    "gpt-4.1": (2.00, 8.00, 0.50),
}
RATE_DEFAULT = MODEL_RATES["auto"]


def _cursor_user_dir_candidates():
    """Known Cursor User dirs (desktop IDE + remote/server installs)."""
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA")
    out = []
    if appdata:
        out.append(os.path.join(appdata, "Cursor", "User"))
    out += [
        os.path.join(home, "Library", "Application Support", "Cursor", "User"),
        os.path.join(home, ".config", "Cursor", "User"),
        os.path.join(home, ".cursor-server", "data", "User"),
    ]
    # Preserve order, drop dupes.
    seen = set()
    uniq = []
    for path in out:
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        uniq.append(norm)
    return uniq


def _store_row_counts(db_path):
    """Return table row counts for a state.vscdb, or None if unreadable."""
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        uri = "file:{}?mode=ro".format(db_path.replace("?", "%3f").replace("#", "%23"))
        with sqlite3.connect(uri, uri=True, timeout=15) as con:
            counts = {}
            for table in ("ItemTable", "cursorDiskKV", "composerHeaders"):
                try:
                    counts[table] = con.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.OperationalError:
                    counts[table] = 0
            return counts
    except sqlite3.Error:
        return None



def _composer_index_stats(db_path):
    """Summarize Cursor 3.0+ ItemTable chat-name index coverage."""
    out = {
        "headers_key": False,
        "headers_named": 0,
        "headers_total": 0,
        "composer_data_named": 0,
        "sql_composer_headers": 0,
    }
    if not db_path or not os.path.isfile(db_path):
        return out
    try:
        with connect(db_path) as con:
            try:
                out["sql_composer_headers"] = con.execute(
                    "SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
            except sqlite3.OperationalError:
                out["sql_composer_headers"] = 0
            for key, named_field, total_field, flag in (
                ("composer.composerHeaders", "headers_named", "headers_total", "headers_key"),
                ("composer.composerData", "composer_data_named", None, None),
            ):
                try:
                    row = con.execute(
                        "SELECT value FROM ItemTable WHERE key = ?", (key,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    continue
                if not row:
                    continue
                if flag:
                    out[flag] = True
                blob = _loads(row[0]) or {}
                composers = []
                if isinstance(blob, dict):
                    composers = blob.get("allComposers") or blob.get("composers") or []
                if not isinstance(composers, list):
                    continue
                if total_field:
                    out[total_field] = len(composers)
                named = 0
                for entry in composers:
                    if isinstance(entry, dict) and _composer_display_name(entry):
                        named += 1
                out[named_field] = named
    except sqlite3.Error:
        pass
    return out


def _store_weight(counts):
    if not counts:
        return -1
    return (counts.get("cursorDiskKV") or 0) + (counts.get("composerHeaders") or 0) * 10 + (
        counts.get("ItemTable") or 0)


def _cursor_user_dir():
    best, best_w = None, -1
    first_existing = None
    for path in _cursor_user_dir_candidates():
        gs = os.path.join(path, "globalStorage")
        if not os.path.isdir(gs):
            continue
        if first_existing is None:
            first_existing = path
        db = os.path.join(gs, "state.vscdb")
        w = _store_weight(_store_row_counts(db))
        if w > best_w:
            best, best_w = path, w
    if best is not None:
        return best
    if first_existing is not None:
        return first_existing
    return _cursor_user_dir_candidates()[0]


def _default_db():
    return os.path.join(_cursor_user_dir(), "globalStorage", "state.vscdb")


def _dashboard_data_dir():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "cursor-dashboard")
    return os.path.join(os.path.expanduser("~"), ".config", "cursor-dashboard")


def _merged_store_path():
    return os.path.join(_dashboard_data_dir(), "merged-state.vscdb")


def ide_store_candidates():
    """Existing state.vscdb paths under known Cursor installs, richest first."""
    found = []
    for user_dir in _cursor_user_dir_candidates():
        db = os.path.join(user_dir, "globalStorage", "state.vscdb")
        counts = _store_row_counts(db)
        if counts is None:
            continue
        found.append({"path": db, "counts": counts, "weight": _store_weight(counts)})
    found.sort(key=lambda x: x["weight"], reverse=True)
    return found


def _ensure_store_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS ItemTable (
            key TEXT PRIMARY KEY,
            value BLOB
        );
        CREATE TABLE IF NOT EXISTS cursorDiskKV (
            key TEXT PRIMARY KEY,
            value BLOB
        );
        CREATE TABLE IF NOT EXISTS composerHeaders (
            composerId TEXT PRIMARY KEY,
            workspaceId TEXT,
            createdAt INTEGER,
            lastUpdatedAt INTEGER,
            isArchived INTEGER,
            isSubagent INTEGER,
            value BLOB
        );
        """
    )


def _open_store_rw(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    _ensure_store_schema(con)
    return con


def _copy_store_via_backup(src, dest):
    """Snapshot a (possibly live) IDE store into dest without locking it for writes."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    uri = "file:{}?mode=ro".format(src.replace("?", "%3f").replace("#", "%23"))
    src_con = sqlite3.connect(uri, uri=True, timeout=60)
    try:
        if os.path.exists(dest):
            os.remove(dest)
        dst_con = sqlite3.connect(dest, timeout=60)
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()


def _composer_index_named_count(raw):
    """How many allComposers entries carry a non-empty display name."""
    blob = _loads(raw) or {}
    if not isinstance(blob, dict):
        return 0
    composers = blob.get("allComposers") or blob.get("composers") or []
    if not isinstance(composers, list):
        return 0
    n = 0
    for entry in composers:
        if isinstance(entry, dict) and _composer_display_name(entry):
            n += 1
    return n


def _item_table_value_richer(key, new_val, old_val):
    if old_val in (None, b"", ""):
        return new_val not in (None, b"", "")
    if new_val in (None, b"", ""):
        return False
    if isinstance(key, str) and key.startswith("composer."):
        # Prefer the index that actually has chat names (Cursor 3.0+ headers).
        return _composer_index_named_count(new_val) > _composer_index_named_count(old_val)
    return False


def _merge_item_table(dst, src):
    added = updated = 0
    try:
        rows = src.execute("SELECT key, value FROM ItemTable").fetchall()
    except sqlite3.OperationalError:
        return added, updated
    for row in rows:
        key, value = row[0], row[1]
        cur = dst.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        if cur is None:
            dst.execute("INSERT INTO ItemTable(key, value) VALUES (?, ?)", (key, value))
            added += 1
        elif _item_table_value_richer(key, value, cur[0]):
            dst.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (value, key))
            updated += 1
    return added, updated


def _merge_cursor_disk_kv(dst, src):
    added = updated = 0
    try:
        rows = src.execute("SELECT key, value FROM cursorDiskKV").fetchall()
    except sqlite3.OperationalError:
        return added, updated
    for row in rows:
        key, value = row[0], row[1]
        cur = dst.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
        if cur is None:
            dst.execute("INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)", (key, value))
            added += 1
            continue
        old = cur[0]
        replace = False
        if old in (None, b"", "") and value not in (None, b"", ""):
            replace = True
        elif isinstance(key, str) and key.startswith("composerData:") and _composer_data_richer(value, old):
            # Re-import must upgrade empty-name composerData left in merged-state.vscdb.
            replace = True
        if replace:
            dst.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (value, key))
            updated += 1
    return added, updated


def _table_columns(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _pick_col(cols, *candidates):
    lower = {c.lower(): c for c in cols}
    for name in candidates:
        if name in cols:
            return name
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _read_composer_header_rows(con):
    """Schema-adaptive read of SQL composerHeaders (column names differ by Cursor build)."""
    cols = _table_columns(con, "composerHeaders")
    if not cols:
        cols = _table_columns(con, "composer_headers")
        table = "composer_headers" if cols else None
    else:
        table = "composerHeaders"
    if not table:
        return []
    cid_c = _pick_col(cols, "composerId", "composer_id", "id")
    val_c = _pick_col(cols, "value", "data", "json")
    if not cid_c:
        return []
    ws_c = _pick_col(cols, "workspaceId", "workspace_id", "workspace")
    created_c = _pick_col(cols, "createdAt", "created_at", "created")
    updated_c = _pick_col(cols, "lastUpdatedAt", "last_updated_at", "updatedAt",
                          "updated_at", "recency", "checkpointAt")
    arch_c = _pick_col(cols, "isArchived", "is_archived", "archived")
    sub_c = _pick_col(cols, "isSubagent", "is_subagent", "subagent")
    select = [cid_c]
    for c in (ws_c, created_c, updated_c, arch_c, sub_c, val_c):
        select.append(c if c else "NULL")
    sql = "SELECT " + ", ".join(select) + f" FROM {table}"
    try:
        raw_rows = con.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in raw_rows:
        out.append({
            "composerId": row[0],
            "workspaceId": row[1] or "",
            "createdAt": row[2] or 0,
            "lastUpdatedAt": row[3] or 0,
            "isArchived": row[4] or 0,
            "isSubagent": row[5] or 0,
            "value": row[6],
        })
    return out


def _merge_composer_headers(dst, src):
    added = updated = 0
    rows = _read_composer_header_rows(src)
    if not rows:
        return added, updated
    for row in rows:
        cid = row["composerId"]
        if not cid:
            continue
        cur = dst.execute(
            "SELECT lastUpdatedAt FROM composerHeaders WHERE composerId = ?",
            (cid,)).fetchone()
        vals = (row["workspaceId"], row["createdAt"], row["lastUpdatedAt"],
                row["isArchived"], row["isSubagent"], row["value"])
        if cur is None:
            dst.execute(
                "INSERT INTO composerHeaders("
                "composerId, workspaceId, createdAt, lastUpdatedAt, "
                "isArchived, isSubagent, value) VALUES (?,?,?,?,?,?,?)",
                (cid,) + vals)
            added += 1
        elif (row["lastUpdatedAt"] or 0) >= (cur[0] or 0):
            dst.execute(
                "UPDATE composerHeaders SET workspaceId=?, createdAt=?, "
                "lastUpdatedAt=?, isArchived=?, isSubagent=?, value=? "
                "WHERE composerId=?",
                vals + (cid,))
            updated += 1
    return added, updated


def merge_stores(base_paths, dest_path, seed_path=None):
    """Merge one or more state.vscdb files into dest_path (never writes sources).

    ``seed_path`` is copied first when dest is missing/empty so "this state"
    remains the baseline and IDE/extra stores layer on top.
    """
    paths = []
    for p in base_paths or []:
        p = os.path.abspath(os.path.expanduser(p))
        if p not in paths:
            paths.append(p)
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError("Store not found: " + ", ".join(missing))
    dest_path = os.path.abspath(os.path.expanduser(dest_path))
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    if seed_path and os.path.isfile(seed_path) and (
            not os.path.isfile(dest_path) or os.path.getsize(dest_path) < 64):
        _copy_store_via_backup(seed_path, dest_path)
    dst = _open_store_rw(dest_path)
    summary = {"dest": dest_path, "sources": [], "tables": {}}
    try:
        for src_path in paths:
            if os.path.abspath(src_path) == dest_path:
                continue
            uri = "file:{}?mode=ro".format(
                src_path.replace("?", "%3f").replace("#", "%23"))
            src = sqlite3.connect(uri, uri=True, timeout=60)
            try:
                item_a, item_u = _merge_item_table(dst, src)
                kv_a, kv_u = _merge_cursor_disk_kv(dst, src)
                hdr_a, hdr_u = _merge_composer_headers(dst, src)
                summary["sources"].append({
                    "path": src_path,
                    "ItemTable": {"added": item_a, "updated": item_u},
                    "cursorDiskKV": {"added": kv_a, "updated": kv_u},
                    "composerHeaders": {"added": hdr_a, "updated": hdr_u},
                })
            finally:
                src.close()
        dst.commit()
        summary["tables"] = _store_row_counts(dest_path) or {}
    finally:
        dst.close()
    return summary


def import_ide_into(current_db, dest_path=None, rebuild=True):
    """Merge local IDE state.vscdb store(s) into dest (default: dashboard merged DB).

    rebuild=True (default) replaces the previous merged copy so stale empty
    composerData names cannot stick around across imports.
    """
    ide = ide_store_candidates()
    if not ide:
        searched = [
            os.path.join(p, "globalStorage", "state.vscdb")
            for p in _cursor_user_dir_candidates()
        ]
        raise FileNotFoundError(
            "No IDE state.vscdb found. Searched:\n  - " + "\n  - ".join(searched))
    dest_path = dest_path or _merged_store_path()
    seed = None
    if rebuild and os.path.isfile(dest_path):
        bak = dest_path + ".bak"
        try:
            if os.path.isfile(bak):
                os.remove(bak)
            os.replace(dest_path, bak)
        except OSError:
            try:
                os.remove(dest_path)
            except OSError:
                pass
    elif not rebuild:
        seed = current_db if current_db and os.path.isfile(current_db) else None
    # Prefer merging every discovered IDE store so multi-install machines keep chats.
    sources = [c["path"] for c in ide]
    return merge_stores(sources, dest_path, seed_path=seed)


def use_store(path):
    """Point the running dashboard at path and drop scan caches."""
    global DB_PATH, STATE_PATH, _CACHE, _BILLING_CACHE
    DB_PATH = os.path.abspath(os.path.expanduser(path))
    STATE_PATH = os.path.join(os.path.dirname(DB_PATH), "cost-dashboard-state.json")
    _CACHE["stamp"] = None
    _CACHE["data"] = None
    _CACHE["at"] = 0
    _BILLING_CACHE["at"] = 0
    _BILLING_CACHE["data"] = None
    _BILLING_CACHE["turns"] = {}
    return DB_PATH


DEFAULT_DB = _default_db()

# --- Jira linking -----------------------------------------------------------
JIRA_HOST_DEFAULT = "timberwilde.atlassian.net"
JIRA_BASE_DEFAULT = (os.environ.get("CURSOR_DASH_JIRA_BASE")
                     or os.environ.get("COPILOT_DASH_JIRA_BASE")
                     or f"https://{JIRA_HOST_DEFAULT}")
JIRA_CRED_RESOURCE = f"Atlassian:{JIRA_HOST_DEFAULT}"
_JIRA_KEYS_DEFAULT = os.environ.get("CURSOR_DASH_JIRA_KEYS") or os.environ.get("COPILOT_DASH_JIRA_KEYS", "")
if not _JIRA_KEYS_DEFAULT.strip():
    _JIRA_KEYS_DEFAULT = "TIM"
JIRA_KEY_ALLOW = {k.strip().upper() for k in _JIRA_KEYS_DEFAULT.split(",") if k.strip()}
JIRA_ISSUE_TTL = 300
JIRA_KEY_DENY = {"UTF", "CVE", "ISO", "RFC", "SHA", "AES", "RSA", "GPT", "API", "UTC",
                 "TLS", "SSL", "HTTP", "SQL", "JSON", "YAML", "BASE", "X", "IPV", "MD",
                 "ISO8601", "SOC", "PCI", "AD", "V", "PY", "NET", "SP", "EC", "AMD",
                 "ARM", "GB", "MB", "KB", "TB", "US", "EU", "UK", "ID", "IPV4", "IPV6"}
GH_RESERVED = {"settings", "orgs", "apps", "features", "marketplace", "repos", "enterprises",
               "notifications", "pulls", "issues", "search", "topics", "sponsors", "users",
               "collections", "codespaces", "login", "join", "about", "blog", "site",
               "rest", "api", "raw", "gist", "www", "graphql", "assets", "avatars"}


def _float_env(name, default=0.0):
    raw = os.environ.get(name, "").replace(",", "").replace("_", "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _int_env(name, default=0):
    return int(_float_env(name, default))


CREDIT_BUDGET = _float_env("CURSOR_DASH_BUDGET") or _float_env("COPILOT_DASH_AI_CREDITS")
_BUDGET_OVERRIDDEN = False

# --- Daily digest email -----------------------------------------------------
# Default recipient is the signed-in Cursor license email. --email-to and
# CURSOR_DASH_EMAIL_TO override it; --no-digest turns the feature off.
# Delivery is the Gmail API (OAuth), not SMTP.
EMAIL_TO = os.environ.get("CURSOR_DASH_EMAIL_TO", "")
EMAIL_FROM = os.environ.get("CURSOR_DASH_EMAIL_FROM") or os.environ.get("COPILOT_DASH_EMAIL_FROM", "")
GMAIL_CREDENTIALS = os.environ.get("CURSOR_DASH_GMAIL_CREDENTIALS", "")
GMAIL_TOKEN_PATH = os.environ.get("CURSOR_DASH_GMAIL_TOKEN", "")
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# Remembers which day's digest already went out, so a refresh only mails once.
# Kept beside the Cursor store rather than in the repo: it is per-machine state.
STATE_PATH = os.path.join(os.path.dirname(DEFAULT_DB), "cost-dashboard-state.json")
DIGEST_LOCK = threading.Lock()
DIGEST_STATUS = {"last_error": "", "sending": False}
DIGEST_LOOKBACK_DAYS = 60
DIGEST_AVG_DAYS = 7
SCAN_LOCK = threading.Lock()
API_ENABLED = True
# Local bubble rows per composer — head+tail by time keeps refs/titles without full scans.
BUBBLE_CAP_PER_COMPOSER = 300
BUBBLE_HEAD_PER_COMPOSER = 150
BUBBLE_TAIL_PER_COMPOSER = 150
_BILLING_CACHE = {"at": 0, "data": None, "turns": {}}

DB_PATH = DEFAULT_DB
JIRA_BASE = JIRA_BASE_DEFAULT
_JIRA_ISSUE_CACHE = {"at": 0.0, "data": {}, "keys": frozenset()}
_JIRA_LAST_ERROR = ""


def connect(path=None):
    path = path or DB_PATH
    uri = "file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23"))
    con = sqlite3.connect(uri, uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _loads(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _norm_model(model_id):
    m = (model_id or "").split("/")[-1].strip().lower().replace(" ", "-")
    return m or "auto"


def _rates(model):
    m = _norm_model(model)
    if m in MODEL_RATES:
        return MODEL_RATES[m]
    for suffix in ("-thinking-high", "-thinking", "-max"):
        if m.endswith(suffix):
            base = m[: -len(suffix)]
            if base in MODEL_RATES:
                return MODEL_RATES[base]
    return RATE_DEFAULT


def _known_rate(model):
    m = _norm_model(model)
    if m in MODEL_RATES:
        return True
    for suffix in ("-thinking-high", "-thinking", "-max"):
        if m.endswith(suffix) and m[: -len(suffix)] in MODEL_RATES:
            return True
    return False


def _local_day(value):
    """Bucket a Cursor timestamp onto the user's local calendar date."""
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).strip().isdigit()):
            n = int(value)
            # Cursor usage events are ms; some invoice fields are unix seconds.
            dt = datetime.datetime.fromtimestamp(n / 1000.0 if n >= 10**12 else n)
            return dt.strftime("%Y-%m-%d")
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return str(value)[:10]


def _cost(t_in, t_out, t_cache, model):
    rate_in, rate_out, rate_cache = _rates(model)
    return (t_in * rate_in + t_out * rate_out + t_cache * rate_cache) / 1e6


def _workspace_map():
    """workspaceStorage/<id> -> friendly folder / workspace name."""
    root = os.path.join(_cursor_user_dir(), "workspaceStorage")
    out = {}
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        meta_path = os.path.join(root, name, "workspace.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            continue
        uri = unquote(meta.get("folder") or meta.get("workspace") or "")
        if uri.startswith("file:///"):
            uri = uri[8:]
            if re.match(r"^/[A-Za-z]:", uri):
                uri = uri[1:]
            uri = uri.replace("/", os.sep)
        out[name] = os.path.basename(uri.rstrip("\\/")) if uri else name
    return out


def _repo_from_path(path):
    if not path:
        return ""
    base = os.path.basename(str(path).rstrip("\\/"))
    if base.endswith(".code-workspace"):
        return base
    return base


def _normalize_fs_path(path):
    """Cursor stores fsPath as /c:/Users/... on Windows — normalize for open()."""
    if not path:
        return ""
    p = str(path).strip()
    if p.startswith("file:///"):
        p = p[8:]
    if re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    return p.replace("/", os.sep)


def _parse_workspace_folders(workspace_path):
    """Folder names from a .code-workspace file (multi-root workspace)."""
    path = _normalize_fs_path(workspace_path)
    if not path or not path.endswith(".code-workspace"):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    base = os.path.dirname(path)
    out = []
    for folder in data.get("folders") or []:
        if not isinstance(folder, dict):
            continue
        rel = (folder.get("path") or "").strip()
        if rel in (".", ".."):
            continue
        if rel:
            full = os.path.normpath(os.path.join(base, rel))
            if full == base:
                continue
        name = (folder.get("name") or "").strip()
        if not name:
            name = _repo_from_path(folder.get("path"))
        if name and not name.endswith(".code-workspace"):
            out.append(name)
    return out


def _tracked_folders_from_meta(meta_row):
    """Local folder names from Cursor composer trackedGitRepos."""
    if not meta_row:
        return []
    folders = list(meta_row.get("tracked_repos") or [])
    if folders:
        return folders
    ws_path = meta_row.get("workspace_path") or ""
    if _normalize_fs_path(ws_path).endswith(".code-workspace"):
        ws_folders = _parse_workspace_folders(ws_path)
        if len(ws_folders) == 1:
            return ws_folders
    repo = (meta_row.get("repository") or "").strip()
    if repo and not repo.endswith(".code-workspace"):
        return [repo.split("/")[-1] if "/" in repo else repo]
    return []


def _known_repo_map(refs):
    known = {}
    for r in (refs or {}).values():
        for it in (r or {}).get("repos") or []:
            name = it.get("name") or ""
            if "/" in name:
                known[name.split("/")[-1].lower()] = name
    return known


def _github_owner_hint(refs):
    counts = collections.Counter()
    for r in (refs or {}).values():
        for it in (r or {}).get("repos") or []:
            name = it.get("name") or ""
            if "/" in name:
                counts[name.split("/")[0]] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _canonical_repo_name(folder, known, owner_hint):
    folder = (folder or "").strip()
    if not folder or folder.endswith(".code-workspace"):
        return ""
    if "/" in folder:
        return folder
    hit = known.get(folder.lower())
    if hit:
        return hit
    if owner_hint:
        return f"{owner_hint}/{folder}"
    return folder


_REPO_FIRST_TS = {}


def _repo_first_commit_ts(repo_path):
    """Unix seconds of the earliest commit, or None if unknown / not a git repo."""
    if not repo_path:
        return None
    if repo_path in _REPO_FIRST_TS:
        return _REPO_FIRST_TS[repo_path]
    ts = None
    git_dir = os.path.join(repo_path, ".git")
    if os.path.isdir(git_dir):
        try:
            r = subprocess.run(
                ["git", "-C", repo_path, "log", "--reverse", "--format=%at", "-1"],
                capture_output=True, text=True, timeout=15, check=False)
            line = (r.stdout or "").strip().splitlines()
            if line:
                ts = int(line[0])
        except (OSError, subprocess.TimeoutExpired, ValueError):
            ts = None
    _REPO_FIRST_TS[repo_path] = ts
    return ts


def _session_end_ts(session):
    """End of the session's last active calendar day (local time)."""
    last = session.get("last_day") or session.get("first_day") or ""
    if not last:
        return None
    try:
        d = datetime.datetime.strptime(last[:10], "%Y-%m-%d")
        return int(d.replace(hour=23, minute=59, second=59).timestamp())
    except ValueError:
        return None


def _paths_for_workspace_folders(row, ws_path):
    """Folder basename -> absolute path from tracked paths and workspace file."""
    paths = {}
    for p in row.get("tracked_repo_paths") or []:
        np = _normalize_fs_path(p)
        if np:
            paths[_repo_from_path(np)] = np
    nws = _normalize_fs_path(ws_path)
    if nws.endswith(".code-workspace"):
        for fp in _parse_workspace_repo_paths(nws):
            paths[_repo_from_path(fp)] = fp
    return paths


def _repo_existed_at(path, ts):
    """True if repo had at least one commit on or before unix ts."""
    if not path or not ts:
        return bool(path)
    first = _repo_first_commit_ts(path)
    if first is None:
        return not os.path.isdir(os.path.join(path, ".git"))
    return first <= ts


def _filter_folders_by_repo_age(folders, paths_by_folder, session_end_ts):
    """Exclude repos that did not exist yet when the session ended."""
    if not session_end_ts or not folders:
        return folders
    kept = []
    for folder in folders:
        path = paths_by_folder.get(folder) or paths_by_folder.get(_repo_from_path(folder))
        if not path:
            kept.append(folder)
            continue
        if not _repo_existed_at(path, session_end_ts):
            continue
        kept.append(folder)
    # Rewritten/squashed git history can make every repo look "too new" — don't zero attribution.
    return kept if kept else folders


def _github_path_map(git_repos=None, meta=None):
    m = {}
    for r in git_repos or _discover_git_repos(meta or {}):
        gh, path = r.get("github"), r.get("path")
        if gh and path:
            m[gh] = path
    return m


def _filter_session_refs_by_repo_age(session, refs_entry, path_by_github):
    """Drop repo/PR refs for repos that did not exist when the session ended."""
    if not refs_entry or not path_by_github:
        return refs_entry
    end_ts = _session_end_ts(session)
    if not end_ts:
        return refs_entry

    def _existed(github_name):
        path = path_by_github.get(github_name or "")
        if not path:
            return True
        return _repo_existed_at(path, end_ts)

    kept_repos = [r for r in (refs_entry.get("repos") or []) if _existed(r.get("name"))]
    if kept_repos or not (refs_entry.get("repos") or []):
        refs_entry["repos"] = kept_repos
    kept_prs = [
        p for p in (refs_entry.get("prs") or [])
        if _existed(p.get("repo") or (p.get("key") or "").split("#")[0])]
    if kept_prs or not (refs_entry.get("prs") or []):
        refs_entry["prs"] = kept_prs
    return refs_entry


def _sync_session_repo_labels(session, refs_entry):
    """Refresh repository / repo_split after refs were filtered."""
    weights = session.get("repo_weights") or {}
    if weights:
        primary = [name for name, w in weights.items() if w > 0]
    else:
        primary = [r["name"] for r in (refs_entry.get("repos") or [])
                   if r.get("role") in ("primary", "inferred", "shared-unconfirmed")]
    if not primary:
        return
    if len(primary) == 1:
        session["repository"] = primary[0]
        session["workspace"] = primary[0]
        session["repo_split"] = 0
    else:
        short = [n.split("/")[-1] for n in primary]
        session["repository"] = " · ".join(short[:5]) + (
            f" +{len(short) - 5}" if len(short) > 5 else "")
        session["repo_split"] = len(primary)


def _session_time_bounds(session, priced_all=None):
    sid = session.get("session_id")
    turns = (priced_all or {}).get(sid) or []
    ts_list = [_parse_turn_ts(t.get("started_at")) for t in turns]
    ts_list = [t for t in ts_list if t is not None]
    if ts_list:
        return min(ts_list), max(ts_list)
    start = session.get("first_day") or ""
    end = session.get("last_day") or start
    if not start:
        return None, None
    try:
        d0 = datetime.datetime.strptime(start[:10], "%Y-%m-%d")
        d1 = datetime.datetime.strptime(end[:10], "%Y-%m-%d")
        return (int(d0.replace(hour=0, minute=0, second=0).timestamp()),
                int(d1.replace(hour=23, minute=59, second=59).timestamp()))
    except ValueError:
        return None, None


def _activity_date_bounds(sessions, billing_events=None):
    days = list(_billing_event_days(billing_events or []))
    for s in sessions:
        for key in ("first_day", "last_day"):
            d = (s.get(key) or "")[:10]
            if d:
                days.append(d)
    if not days:
        return MIN_DAY, MAX_DAY
    return min(days), max(days)


def _git_weights_for_repos(session, repo_names, path_by_github, activities,
                           priced_all=None, window=None):
    """Weight repos by nearby git commits during this session (turn-level when possible)."""
    if not activities or not repo_names:
        return {}
    if window is None:
        window = GIT_CORR_WINDOW
    names = set(repo_names)
    weights = collections.Counter()
    sid = session.get("session_id")
    turns = (priced_all or {}).get(sid) or []

    if turns:
        for turn in turns:
            ts = _parse_turn_ts(turn.get("started_at"))
            if ts is None:
                continue
            mass = turn.get("cost_usd") or 1.0
            best_per_repo = {}
            for act in activities:
                repo = act.get("repo")
                if repo not in names:
                    continue
                repo_path = act.get("repo_path") or path_by_github.get(repo, "")
                if repo_path and not _repo_existed_at(repo_path, ts):
                    continue
                dist = abs(act["ts"] - ts)
                if dist > window:
                    continue
                prev = best_per_repo.get(repo)
                if not prev or dist < prev[0]:
                    best_per_repo[repo] = (dist, act)
            if not best_per_repo:
                continue
            best_repo = min(best_per_repo.items(), key=lambda x: x[1][0])[0]
            weights[best_repo] += mass
    else:
        t0, t1 = _session_time_bounds(session, priced_all)
        if t0 is None:
            return {}
        for act in activities:
            repo = act.get("repo")
            if repo not in names:
                continue
            if t0 - window <= act["ts"] <= t1 + window:
                weights[repo] += 1.0

    if not weights:
        return {}
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def _repo_has_session_activity(repo_name, session, priced_all, activities,
                               path_by_github, window=None):
    """True if repo had a commit/merge PR within ±window of a session turn (or in session dates)."""
    if not activities or not repo_name:
        return False
    if window is None:
        window = GIT_CORR_WINDOW
    sid = session.get("session_id")
    turns = (priced_all or {}).get(sid) or []
    if turns:
        for turn in turns:
            ts = _parse_turn_ts(turn.get("started_at"))
            if ts is None:
                continue
            repo_path = path_by_github.get(repo_name, "")
            if repo_path and not _repo_existed_at(repo_path, ts):
                continue
            for act in activities:
                if act.get("repo") != repo_name:
                    continue
                if abs(act["ts"] - ts) <= window:
                    return True
        return False
    t0, t1 = _session_time_bounds(session, priced_all)
    if t0 is None:
        return False
    for act in activities:
        if act.get("repo") != repo_name:
            continue
        if t0 - window <= act["ts"] <= t1 + window:
            return True
    return False


def _pr_has_session_activity(pr_key, session, priced_all, activities,
                             path_by_github, window=None):
    """True if a merge/PR commit for pr_key fell within ±window of a session turn."""
    if not pr_key or not activities:
        return False
    if window is None:
        window = GIT_CORR_WINDOW
    repo = pr_key.split("#", 1)[0]
    sid = session.get("session_id")
    turns = (priced_all or {}).get(sid) or []
    if turns:
        for turn in turns:
            ts = _parse_turn_ts(turn.get("started_at"))
            if ts is None:
                continue
            repo_path = path_by_github.get(repo, "")
            if repo_path and not _repo_existed_at(repo_path, ts):
                continue
            for act in activities:
                if act.get("pr") != pr_key:
                    continue
                if abs(act["ts"] - ts) <= window:
                    return True
        return False
    t0, t1 = _session_time_bounds(session, priced_all)
    if t0 is None:
        return False
    for act in activities:
        if act.get("pr") != pr_key:
            continue
        if t0 - window <= act["ts"] <= t1 + window:
            return True
    return False


def _turn_pr_git_costs(ts, cost, activities, window, path_by_github):
    """Per-PR turn cost shares from nearby merge commits (same weighting as repos)."""
    if ts is None or not cost or not activities:
        return {}
    best_per_pr = {}
    for act in activities:
        if not act.get("pr"):
            continue
        repo_path = act.get("repo_path") or path_by_github.get(act.get("repo") or "", "")
        if repo_path and not _repo_existed_at(repo_path, ts):
            continue
        dist = abs(act["ts"] - ts)
        if dist > window:
            continue
        pk = act["pr"]
        prev = best_per_pr.get(pk)
        if not prev or dist < prev[0]:
            best_per_pr[pk] = (dist, act)
    if not best_per_pr:
        return {}
    weights = {pk: 1.0 / (dist + 300.0) for pk, (dist, _act) in best_per_pr.items()}
    total_w = sum(weights.values()) or 1.0
    return {pk: cost * (w / total_w) for pk, w in weights.items()}


def _resolve_bare_pr_repo(num, hints, text, session=None, activities=None,
                          priced_all=None, path_by_github=None, refs_entry=None):
    """Pick one repo for a bare 'PR #N' mention instead of fanning out to every hint."""
    if not hints:
        return None
    num_s = str(num)
    # Explicit owner/repo#N or github URL in session text wins.
    for owner, repo, n in RE_PR_SHORT.findall(text or ""):
        if int(n) == num and clean_repo(owner, repo) in hints:
            return clean_repo(owner, repo)
    for owner, repo, n in RE_PR.findall(text or ""):
        if int(n) == num and clean_repo(owner, repo) in hints:
            return clean_repo(owner, repo)
    # Git merge for this PR number near a billed turn.
    if session and activities and priced_all:
        sid = session.get("session_id")
        for turn in (priced_all or {}).get(sid) or []:
            ts = _parse_turn_ts(turn.get("started_at"))
            if ts is None:
                continue
            for repo in hints:
                pk = f"{repo}#{num}"
                for act in activities:
                    if act.get("pr") != pk:
                        continue
                    if abs(act["ts"] - ts) <= GIT_CORR_WINDOW:
                        return repo
    # Any merge for this PR number among candidate repos (no time window).
    if activities:
        merge_repos = []
        for repo in hints:
            pk = f"{repo}#{num}"
            if any(act.get("pr") == pk for act in activities):
                merge_repos.append(repo)
        if len(merge_repos) == 1:
            return merge_repos[0]
        if len(merge_repos) > 1:
            weights = (session or {}).get("repo_weights") or {}
            weighted = [(r, weights[r]) for r in merge_repos if weights.get(r, 0) > 0]
            if weighted:
                return max(weighted, key=lambda x: x[1])[0]
            primaries = {x.get("name") for x in (refs_entry or {}).get("repos") or []
                         if x.get("role") in ("primary", "shared-unconfirmed")}
            primary_hits = [r for r in merge_repos if r in primaries]
            if len(primary_hits) == 1:
                return primary_hits[0]
    # PR already linked to a repo in refs for this number.
    for p in (refs_entry or {}).get("prs") or []:
        if p.get("number") == num and p.get("repo") in hints:
            return p["repo"]
    # Single-repo session or git-weighted top repo.
    weights = (session or {}).get("repo_weights") or {}
    weighted = [(r, weights[r]) for r in hints if weights.get(r, 0) > 0]
    if weighted:
        return max(weighted, key=lambda x: x[1])[0]
    if len(hints) == 1:
        return hints[0]
    return None


def _pr_text_turn_costs(sid, items, turn_prs, turn_cost):
    """Turn-segment costs when a PR is mentioned in that turn's transcript."""
    anchors = sorted((turn_prs or {}).get(sid) or [])
    if not anchors:
        return {}
    billable = {p["key"] for p in items
                if not p.get("inferred") and p.get("role") != "skipped"}
    costs_map = (turn_cost or {}).get(sid) or {}
    out = collections.Counter()
    prev = -1
    for a in anchors:
        seg = [t for t in costs_map if prev < t <= a]
        prev = a
        keys = turn_prs[sid][a]
        seg_cost = sum(costs_map[t][0] for t in seg)
        if seg_cost <= 0:
            continue
        mentioned = [k for k in keys if k in billable]
        if not mentioned:
            continue
        share = seg_cost / len(mentioned)
        for k in mentioned:
            out[k] += share
    return dict(out)


def _pr_cost_entries(session, refs_entry, turn_prs=None, turn_cost=None):
    """Return [(pr_item, cost_usd)] from turn text, git merge correlation, or both.

    Explicitly mentioned (non-inferred) PRs are always included so the PR tab
    lists every captured link — even when spend landed on a later PR in the
    same chat. Inferred git-only PRs still require a positive cost.
    """
    items = [p for p in (refs_entry.get("prs") or []) if p.get("role") != "skipped"]
    if not items:
        return []

    sid = session.get("session_id")
    gc_prs = {
        p["key"]: p.get("cost_usd") or 0
        for p in (session.get("git_correlation") or {}).get("prs") or []
    }
    git_costs = session.get("pr_turn_costs") or {}
    text_costs = _pr_text_turn_costs(sid, items, turn_prs, turn_cost)
    out = []
    seen = set()
    for p in items:
        k = p.get("key")
        if not k or k in seen:
            continue
        if p.get("inferred"):
            abs_cost = gc_prs.get(k) or git_costs.get(k) or 0
            if abs_cost <= 0:
                continue
        else:
            abs_cost = text_costs.get(k) or 0
            if abs_cost <= 0:
                abs_cost = gc_prs.get(k) or git_costs.get(k) or 0
            # Keep $0 explicit mentions so earlier PRs in a long chat still roll up.
        out.append((p, abs_cost))
        seen.add(k)
    return out


def _apply_git_activity_gate(sessions, refs, priced_all, activities, path_by_github):
    """Drop inferred attribution when git did not confirm activity during the session."""
    if not activities:
        return
    for s in sessions:
        sid = s["session_id"]
        r = refs.get(sid)
        if not r:
            continue
        kept = []
        for x in r.get("repos") or []:
            role = x.get("role")
            if role in ("inferred", "shared-unconfirmed"):
                name = x.get("name") or ""
                if _repo_has_session_activity(
                        name, s, priced_all, activities, path_by_github):
                    kept.append(x if role == "inferred"
                                 else {**x, "role": "inferred"})
                else:
                    kept.append({**x, "role": "shared-skipped"})
            else:
                kept.append(x)
        r["repos"] = kept
        weights = s.get("repo_weights") or {}
        if weights:
            active = {x.get("name") for x in kept
                      if x.get("role") in ("primary", "inferred", "shared-unconfirmed")}
            weights = {k: v for k, v in weights.items() if k in active}
            if weights:
                total = sum(weights.values()) or 1.0
                s["repo_weights"] = {k: v / total for k, v in weights.items()}
            else:
                s["repo_weights"] = {}
        billable = [x for x in kept
                    if x.get("role") in ("primary", "inferred", "shared-unconfirmed")]
        if not billable:
            s.pop("shared_attribution", None)
            s["repository"] = ""
            s["workspace"] = ""
            s["repo_split"] = 0
        else:
            _sync_session_repo_labels(s, r)

        kept_prs = []
        for p in r.get("prs") or []:
            pk = p.get("key") or ""
            if p.get("inferred"):
                if _pr_has_session_activity(
                        pk, s, priced_all, activities, path_by_github):
                    kept_prs.append(p)
                else:
                    kept_prs.append({**p, "role": "skipped"})
                continue
            # Keep every explicit mention (URL or bare). Skipping bare PRs without a
            # nearby merge made multi-PR chats look like they only captured the latest
            # PR that happened to sit next to a git merge.
            kept_prs.append(p)
        r["prs"] = kept_prs


def _apply_git_pr_discovery(sessions, refs, priced_all, activities, path_by_github):
    """Discover PRs from merge commits near billed turns; add to refs + pr_turn_costs."""
    if not activities:
        return
    for s in sessions:
        sid = s["session_id"]
        if sid == "_unattributed":
            continue
        turns = (priced_all or {}).get(sid) or []
        if not turns:
            continue
        costs = collections.Counter()
        for turn in turns:
            ts = _parse_turn_ts(turn.get("started_at"))
            cost = turn.get("cost_usd") or 0
            for pk, share in _turn_pr_git_costs(
                    ts, cost, activities, GIT_CORR_WINDOW, path_by_github).items():
                costs[pk] += share
        gc = s.get("git_correlation") or {}
        if gc.get("matched"):
            for gp in gc.get("prs") or []:
                pk = gp.get("key")
                c = gp.get("cost_usd") or 0
                if pk and c > 0 and pk not in costs:
                    costs[pk] = c
        if not costs:
            continue
        r = refs.setdefault(sid, {"jira": [], "prs": [], "repos": []})
        existing = {p["key"]: dict(p) for p in r.get("prs") or []}
        for pk, pcost in costs.items():
            if pcost <= 0:
                continue
            if pk not in existing:
                existing[pk] = {
                    "key": pk, "repo": pk.split("#")[0],
                    "number": int(pk.split("#")[1]),
                    "created": False, "inferred": True,
                }
            elif existing[pk].get("inferred") and not existing[pk].get("created"):
                existing[pk]["inferred"] = True
        r["prs"] = sorted(existing.values(), key=lambda p: (p["repo"], p["number"]))
        refs[sid] = r
        s["pr_turn_costs"] = {k: round(v, 4) for k, v in costs.items() if v > 0}


def _apply_git_weighted_shared_repos(sessions, refs, priced_all, activities, path_by_github):
    """Replace equal multi-root splits with git-activity-weighted attribution."""
    if not activities:
        return
    for s in sessions:
        sid = s["session_id"]
        r = refs.get(sid)
        if not r:
            continue
        primaries = [x["name"] for x in r.get("repos", []) if x.get("role") == "primary"]
        if len(primaries) <= 1 and not (s.get("repo_split") or 0) > 1:
            continue

        weights = _git_weights_for_repos(
            s, primaries, path_by_github, activities, priced_all)
        if not weights:
            s["repo_weights"] = {}
            s["shared_attribution"] = "unconfirmed"
            for x in r.get("repos", []):
                if x.get("role") == "primary":
                    x["role"] = "shared-unconfirmed"
            continue

        s["repo_weights"] = {k: round(v, 6) for k, v in weights.items()}
        s["shared_attribution"] = "git-weighted"
        for x in r.get("repos", []):
            name = x.get("name") or ""
            w = weights.get(name, 0.0)
            if x.get("role") == "primary":
                if w > 0:
                    x["role"] = "primary"
                    x["weight"] = round(w, 4)
                else:
                    x["role"] = "shared-skipped"
        _sync_session_repo_labels(s, r)


def _repo_cost_shares(session, refs_entry):
    """Return [(repo_item, fraction)] — one repo per session at 100%."""
    items = refs_entry.get("repos") or []
    primaries = [it for it in items if it.get("role") == "primary"]
    if len(primaries) == 1:
        return [(primaries[0], 1.0)]
    inferred = [it for it in items if it.get("role") == "inferred"]
    if len(inferred) == 1:
        return [(inferred[0], 1.0)]
    return []


def _apply_repo_age_filters(sessions, refs, path_by_github):
    """Final pass: strip impossible repo refs and fix session labels."""
    for s in sessions:
        sid = s["session_id"]
        r = refs.get(sid)
        if not r:
            continue
        _filter_session_refs_by_repo_age(s, r, path_by_github)
        _sync_session_repo_labels(s, r)
        s["refs"] = r
        refs[sid] = r


def _session_known_repo_map(session_refs, meta_row):
    known = {}
    for it in (session_refs or {}).get("repos") or []:
        name = it.get("name") or ""
        if "/" in name:
            known[name.split("/")[-1].lower()] = name
    for tr in (meta_row or {}).get("tracked_repos") or []:
        if "/" in tr:
            known[tr.split("/")[-1].lower()] = tr
        elif tr:
            known[tr.lower()] = tr
    return known


# --------------------------------------------------------------------------
# Repo attribution — one repo per session, clear priority order.
# --------------------------------------------------------------------------

GIT_CORR_WINDOW = 8 * 3600  # seconds — match billed turns to nearby commits
WORKSPACE_ROOT_REPO = "evernote_remarkable"
REPO_TITLE_ALIASES = {
    "fastcat": ["fastcat", "fast cat", "fast-path", "fast path", "relationship schema"],
    "jrtca_results": ["jrtca", "trial results", "jrtca_results", "grid-keys", "grid keys",
                        "normalization"],
    "horse_shows": ["horse_shows", "horse shows", "horseshows", "scrape class", "show results",
                    "non-placing", "discovery re-walk", "chrome process", "selenium"],
    "3dprinting": ["3dprinting", "3d printing", "3d print", "bambu", "rv pantry",
                   "interlocking shelf"],
    "dashboard": ["dashboard", "cursor_dashboard", "cost dashboard", "copilot",
                  "chunk budget", "atlassian api", "jira ticket", "jira backfill",
                  "repo attribution"],
    "racing_startingbox": ["racing_startingbox", "starting box", "startingbox"],
}
EVERNOTE_TITLE_ALIASES = ()  # dashboard: explicit evernote/remarkable in title only


def _repo_short_name(name):
    return (name or "").split("/")[-1]


def _evernote_from_title(title):
    """Evernote only when the title explicitly names the project — not legacy keyword lists."""
    blob = (title or "").lower()
    if "evernote_remarkable" in blob.replace("-", "_").replace(" ", "_"):
        return True
    return "evernote" in blob and "remarkable" in blob


def _is_passive_evernote_track(tracked, cursor_repo):
    """Evernote listed alone because it sits in the multi-root workspace file."""
    if len(tracked) == 1 and tracked[0] == WORKSPACE_ROOT_REPO:
        return True
    cr = (cursor_repo or "").split("/")[-1]
    return cr == WORKSPACE_ROOT_REPO and len(tracked) <= 1


def _infer_repo_folder_from_title(title):
    """Best-effort repo folder from chat title keywords — fallback only."""
    blob = (title or "").lower().strip()
    if not blob or blob in ("other billed usage", "(untitled)"):
        return ""
    for name, aliases in REPO_TITLE_ALIASES.items():
        if any(a in blob for a in aliases):
            return name
    if _evernote_from_title(title):
        return WORKSPACE_ROOT_REPO
    return ""


def _pick_git_repo(repo_hits):
    """Prefer non-evernote when git activity is split across repos."""
    ranked = repo_hits.most_common()
    if not ranked:
        return ""
    top_repo, top_cost = ranked[0]
    if (_repo_short_name(top_repo) != WORKSPACE_ROOT_REPO
            or len(ranked) == 1):
        return top_repo
    for repo, cost in ranked[1:]:
        if _repo_short_name(repo) != WORKSPACE_ROOT_REPO and cost >= top_cost * 0.2:
            return repo
    return top_repo


def _canonical_repo_from_folder(folder, known, owner, path_by_github=None, paths_by_folder=None):
    folder = (folder or "").strip()
    if not folder or folder.endswith(".code-workspace"):
        return ""
    if "/" in folder:
        return folder
    path = (paths_by_folder or {}).get(folder) or (paths_by_folder or {}).get(_repo_from_path(folder))
    if path and path_by_github:
        np = _normalize_fs_path(path)
        gh = _git_remote_github(path)
        if gh:
            return gh
        for g, p in path_by_github.items():
            if _normalize_fs_path(p) == np:
                return g
    hit = known.get(folder.lower())
    if hit:
        return hit
    if owner:
        return f"{owner}/{folder}"
    return folder


GENERIC_FILENAMES = frozenset({
    "readme.md", "readme.txt", "license", "license.md", "changelog.md",
    "__init__.py", "setup.py", "pyproject.toml", "requirements.txt",
    "package.json", "package-lock.json", "tsconfig.json", "makefile",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "main.py", "main.ts", "main.js", "index.py", "index.ts", "index.js",
    "config.json", "config.yaml", "config.yml", "settings.json",
    ".gitignore", ".cursorrules",
})
RE_FILE_EXT = re.compile(
    r"(?:\b|/)([\w][\w.-]*\.(?:py|ps1|psm1|tsx?|jsx?|rs|go|java|sql|md|json|ya?ml|"
    r"html|css|sh|rb|php|cs|cpp|h|vue|svelte|toml|ini|cfg|xml|csv|ipynb))\b",
    re.I)
RE_EDITED_FILES = re.compile(r"\bEdited\s+(.+)$", re.I | re.M)
RE_BACKTICK_FILE = re.compile(r"`([^`\n]+)`")
_REPO_FILE_INDEX = {}


def _session_text_for_files(session, meta):
    parts = [
        session.get("title") or "",
        session.get("subtitle") or "",
        (meta or {}).get("subtitle") or "",
        session.get("text") or "",
    ]
    return "\n".join(p for p in parts if p)


def _extract_file_refs(text):
    """Basenames mentioned in chat text / Cursor subtitle."""
    if not text:
        return []
    seen = set()
    out = []

    def add(name):
        name = (name or "").strip().strip(",")
        if not name or "/" in name and name.count("/") > 3:
            base = os.path.basename(name.replace("\\", "/"))
        else:
            base = os.path.basename(name.replace("\\", "/")) if "/" in name else name
        key = base.lower()
        if not key or key in GENERIC_FILENAMES or key in seen:
            return
        if not re.search(r"\.\w{1,10}$", key):
            return
        seen.add(key)
        out.append(base)

    for m in RE_EDITED_FILES.finditer(text):
        chunk = m.group(1)
        for part in re.split(r",\s*", chunk):
            add(part.strip())

    for m in RE_FILE_EXT.finditer(text):
        add(m.group(1))

    for m in RE_BACKTICK_FILE.finditer(text):
        token = m.group(1).strip()
        if "." in token and not token.startswith("http"):
            add(token)

    return out


def _repo_file_basenames(repo_path):
    """Cached git-tracked basenames for a local repo."""
    if repo_path in _REPO_FILE_INDEX:
        return _REPO_FILE_INDEX[repo_path]
    names = set()
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        _REPO_FILE_INDEX[repo_path] = names
        return names
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "ls-files"],
            capture_output=True, text=True, timeout=90, check=False)
        for line in (r.stdout or "").splitlines():
            base = os.path.basename(line.strip())
            if base:
                names.add(base.lower())
    except (OSError, subprocess.TimeoutExpired):
        pass
    _REPO_FILE_INDEX[repo_path] = names
    return names


def _resolve_repo_from_file_refs(session, meta, path_by_github, canon):
    """Attribute to the repo that owns files referenced in chat text."""
    if not path_by_github:
        return "", ""
    refs = _extract_file_refs(_session_text_for_files(session, meta))
    if not refs:
        return "", ""
    scores = collections.Counter()
    for basename in refs:
        key = basename.lower()
        if key in GENERIC_FILENAMES:
            continue
        for gh, repo_path in path_by_github.items():
            if key in _repo_file_basenames(repo_path):
                scores[gh] += 1
    if not scores:
        return "", ""
    ranked = scores.most_common()
    top_gh, top_n = ranked[0]
    if len(ranked) == 1 or top_n > ranked[1][1]:
        return top_gh, "file-ref"
    return "", ""


def _resolve_session_repo(session, meta, owner, known, path_by_github=None):
    """One repo per session. Priority: Cursor metadata → file refs → title → unresolved."""
    sid = session.get("session_id") or ""
    if sid == "_unattributed" or session.get("unattributed") or session.get("orphan_billed"):
        return "", "unresolved"

    m = meta or {}
    row = {
        "tracked_repos": session.get("tracked_repos") or m.get("tracked_repos") or [],
        "tracked_repo_paths": session.get("tracked_repo_paths") or m.get("tracked_repo_paths") or [],
        "workspace_path": session.get("workspace_path") or m.get("workspace_path") or "",
        "repository": session.get("repository") or m.get("repository") or "",
    }
    ws_path = row["workspace_path"]
    paths_by_folder = _paths_for_workspace_folders(row, ws_path)
    tracked = [t for t in (row["tracked_repos"] or []) if t]
    cursor_repo = (row["repository"] or "").strip()
    title = session.get("title") or m.get("title") or ""
    title_repo = _infer_repo_folder_from_title(title)

    def canon(name):
        return _canonical_repo_from_folder(name, known, owner, path_by_github, paths_by_folder)

    if len(tracked) == 1 and tracked[0] == WORKSPACE_ROOT_REPO:
        if title_repo and title_repo != WORKSPACE_ROOT_REPO:
            hit = canon(title_repo)
            if hit:
                return hit, "title"
        if _evernote_from_title(title):
            hit = canon(tracked[0])
            if hit:
                return hit, "cursor-tracked"
    elif len(tracked) == 1:
        hit = canon(tracked[0])
        if hit:
            return hit, "cursor-tracked"

    if cursor_repo and not cursor_repo.endswith(".code-workspace"):
        cr_short = cursor_repo.split("/")[-1]
        if cr_short == WORKSPACE_ROOT_REPO and _is_passive_evernote_track(tracked, cursor_repo):
            if title_repo and title_repo != WORKSPACE_ROOT_REPO:
                hit = canon(title_repo)
                if hit:
                    return hit, "title"
            if not _evernote_from_title(title):
                pass  # fall through — don't assign passive evernote repository
            else:
                hit = canon(cursor_repo) if "/" not in cursor_repo else cursor_repo
                if hit:
                    return hit, "cursor-repository"
        else:
            hit = canon(cursor_repo) if "/" not in cursor_repo else cursor_repo
            if hit:
                return hit, "cursor-repository"

    ws_norm = _normalize_fs_path(ws_path)
    if ws_norm and not ws_norm.endswith(".code-workspace"):
        hit = canon(_repo_from_path(ws_norm))
        if hit:
            return hit, "cursor-workspace"

    file_repo, _file_src = _resolve_repo_from_file_refs(session, m, path_by_github, canon)
    if file_repo:
        return file_repo, _file_src

    if title_repo and title_repo != WORKSPACE_ROOT_REPO:
        hit = canon(title_repo)
        if hit:
            return hit, "title"
    if title_repo == WORKSPACE_ROOT_REPO and _evernote_from_title(title):
        hit = canon(title_repo)
        if hit:
            return hit, "title"

    return "", "unresolved"


def _assign_session_repos(sessions, meta, refs, path_by_github=None):
    """Assign exactly one primary repo per session; record provenance in repo_source."""
    _REPO_FILE_INDEX.clear()
    owner = _github_owner_hint(refs)
    for s in sessions:
        sid = s["session_id"]
        m = meta.get(sid) or {}
        session_refs = refs.get(sid) or {}
        known = _session_known_repo_map(session_refs, m)
        repo, source = _resolve_session_repo(s, m, owner, known, path_by_github)
        s["repo_source"] = source
        s["repo_split"] = 0
        if not repo:
            s["repository"] = ""
            s["workspace"] = ""
            continue
        r = refs.get(sid) or {"jira": [], "prs": [], "repos": []}
        merged = {x["name"]: x.get("role") or "mentioned" for x in r.get("repos") or []}
        merged[repo] = "primary"
        r["repos"] = sorted(
            ({"name": k, "role": v} for k, v in merged.items()),
            key=lambda x: (x["role"] != "primary", x["name"]))
        refs[sid] = r
        s["repository"] = repo
        s["workspace"] = repo


# --------------------------------------------------------------------------
# Git activity correlation (fallback when Cursor metadata is missing).
# --------------------------------------------------------------------------

_GIT_CACHE = {}
RE_MERGE_PR = re.compile(r"Merge pull request #(\d+)", re.I)
RE_GH_REMOTE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
COMMON_WORKSPACE_NAMES = ("mwtorq.code-workspace",)


def _sessions_for_git_correlation(sessions):
    """Sessions still lacking a resolved repo after Cursor/title assignment."""
    return [s for s in sessions
            if s.get("repo_source") == "unresolved" and not s.get("repository")]


def _parse_workspace_repo_paths(workspace_path):
    """Absolute local paths for each folder in a .code-workspace file."""
    path = _normalize_fs_path(workspace_path)
    if not path or not path.endswith(".code-workspace"):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    base = os.path.dirname(path)
    out = []
    for folder in data.get("folders") or []:
        if not isinstance(folder, dict):
            continue
        rel = (folder.get("path") or "").strip()
        if not rel or rel in (".", ".."):
            continue
        full = os.path.normpath(os.path.join(base, rel))
        if full == base or not os.path.isdir(full):
            continue
        out.append(full)
    return out


def _git_remote_github(repo_path):
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=False)
        url = (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = RE_GH_REMOTE.search(url.replace("\\", "/"))
    if not m:
        return ""
    return clean_repo(m.group(1), m.group(2)) or ""


def _discover_git_repos(meta):
    """Local git repos from composer trackedGitRepos and multi-root workspaces."""
    by_path = {}
    for m in (meta or {}).values():
        for p in m.get("tracked_repo_paths") or []:
            np = _normalize_fs_path(p)
            if np:
                by_path[np] = by_path.get(np) or _repo_from_path(np)
        ws = _normalize_fs_path(m.get("workspace_path") or "")
        if ws.endswith(".code-workspace"):
            for fp in _parse_workspace_repo_paths(ws):
                by_path[fp] = by_path.get(fp) or _repo_from_path(fp)
    if not by_path:
        parent = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        for ws_name in COMMON_WORKSPACE_NAMES:
            ws = os.path.join(parent, ws_name)
            if os.path.isfile(ws):
                for fp in _parse_workspace_repo_paths(ws):
                    by_path[fp] = by_path.get(fp) or _repo_from_path(fp)
    repos = []
    for path, folder in by_path.items():
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        gh = _git_remote_github(path)
        repos.append({"path": path, "folder": folder, "github": gh or folder})
    return repos


def _parse_turn_ts(started_at):
    if not started_at:
        return None
    try:
        text = str(started_at).strip()
        if text.isdigit():
            n = int(text)
            return n // 1000 if n >= 10**12 else n
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return int(dt.timestamp())
    except (TypeError, ValueError, OSError):
        return None


def _bubble_sort_key(row):
    """Sort key for (created, type, bubble_id) tuples."""
    created, _text, btype, bid = row
    ts = _parse_turn_ts(created)
    return (ts if ts is not None else 0, btype != 1, bid)


def _text_mentions_pr(text):
    """True when transcript text links or names a pull request."""
    if not text:
        return False
    return bool(RE_PR.search(text) or RE_PR_SHORT.search(text) or RE_PR_BARE.search(text))


_PR_RAW_MARKERS = (
    "/pull/", "/pulls/", "pullNumber", "prNumber", "pull_number", "pr_number",
    "pullRequest", "CreatePullRequest", "create_pull_request", "html_url",
    "PR #", "PR#", "pr #", "pull request", "Pull request", "Pull Request",
    "gh pr ",
)


def _cheap_pr_marker(raw):
    """Fast substring gate before regex / JSON walks."""
    if not raw:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return False
    if any(m in raw for m in _PR_RAW_MARKERS):
        return True
    # Short owner/repo#N refs and bare PR mentions.
    return ("#" in raw and ("PR" in raw or "pr" in raw or "/" in raw))


def _raw_mentions_pr(raw):
    """True when bubble JSON contains a PR URL, structured pullNumber, or bare PR #."""
    return bool(_pr_links_from_raw(raw))


def _walk_structured_pr_links(obj, add, depth=0):
    """Pull PR links from toolFormerData / GitHub API-shaped objects."""
    if depth > 14 or obj is None:
        return
    if isinstance(obj, list):
        for item in obj[:200]:
            _walk_structured_pr_links(item, add, depth + 1)
        return
    if not isinstance(obj, dict):
        if isinstance(obj, str) and _cheap_pr_marker(obj):
            for owner, repo, num in RE_PR.findall(obj):
                name = clean_repo(owner, repo)
                if name:
                    add(f"https://github.com/{name}/pull/{num}")
            for owner, repo, num in RE_PR_API.findall(obj):
                name = clean_repo(owner, repo)
                if name:
                    add(f"https://github.com/{name}/pull/{num}")
            for owner, repo, num in RE_PR_SHORT.findall(obj):
                name = clean_repo(owner, repo)
                if name:
                    add(f"{name}#{num}")
        return

    # Nested JSON strings (toolFormerData.params / result / rawArgs).
    for key in ("params", "rawArgs", "result", "additionalData", "toolFormerData",
                "tool_former_data", "data", "payload"):
        val = obj.get(key)
        if isinstance(val, str) and val[:1] in "{[":
            try:
                _walk_structured_pr_links(json.loads(val), add, depth + 1)
            except (TypeError, ValueError):
                _walk_structured_pr_links(val, add, depth + 1)
        elif isinstance(val, (dict, list)):
            _walk_structured_pr_links(val, add, depth + 1)

    lower = {str(k).lower(): v for k, v in obj.items()}
    num = None
    for nk in ("pullnumber", "prnumber", "pull_number", "pr_number",
               "pullrequestnumber", "pr_num"):
        if nk in lower and lower[nk] is not None and lower[nk] != "":
            try:
                num = int(lower[nk])
                break
            except (TypeError, ValueError):
                continue
    owner = None
    repo = None
    for ok in ("owner", "repoowner", "org", "organization"):
        if isinstance(lower.get(ok), str) and lower[ok].strip():
            owner = lower[ok].strip()
            break
    for rk in ("repo", "reponame", "repository"):
        val = lower.get(rk)
        if isinstance(val, str) and val.strip():
            val = val.strip()
            if "/" in val:
                parts = val.split("/", 1)
                owner = owner or parts[0]
                repo = parts[1]
            else:
                repo = val
            break
    if num and owner and repo:
        name = clean_repo(owner, repo)
        if name:
            add(f"https://github.com/{name}/pull/{num}")
    elif num:
        # Bare number from CreatePullRequest-style tools — resolve later via hints.
        add(f"PR #{num}")

    for key in ("html_url", "url", "prurl", "pullrequesturl", "pr_url", "permalink"):
        val = lower.get(key)
        if not isinstance(val, str):
            continue
        for owner, repo, n in RE_PR.findall(val):
            name = clean_repo(owner, repo)
            if name:
                add(f"https://github.com/{name}/pull/{n}")
        for owner, repo, n in RE_PR_API.findall(val):
            name = clean_repo(owner, repo)
            if name:
                add(f"https://github.com/{name}/pull/{n}")

    for val in obj.values():
        if isinstance(val, (dict, list)):
            _walk_structured_pr_links(val, add, depth + 1)
        elif isinstance(val, str) and len(val) >= 12 and _cheap_pr_marker(val):
            _walk_structured_pr_links(val, add, depth + 1)


def _pr_links_from_raw(raw):
    """Extract every PR link from bubble JSON — URLs, API paths, and tool fields.

    Refs must not depend on head/tail bubble sampling. Callers that scan all
    PR-candidate rows use this so older tool-only PRs are not dropped when only
    the latest assistant text still has a visible github.com URL.
    """
    if raw is None:
        return []
    obj = None
    if isinstance(raw, (dict, list)):
        obj = raw
        try:
            text = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            text = ""
    elif isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return []
    if text and text.lstrip()[:1] in "{[":
        try:
            obj = json.loads(text)
        except (TypeError, ValueError):
            pass
    if text and not _cheap_pr_marker(text) and obj is None:
        return []

    out = []
    seen = set()

    def _add(s):
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    if text:
        for owner, repo, num in RE_PR.findall(text):
            name = clean_repo(owner, repo)
            if name:
                _add(f"https://github.com/{name}/pull/{num}")
        for owner, repo, num in RE_PR_API.findall(text):
            name = clean_repo(owner, repo)
            if name:
                _add(f"https://github.com/{name}/pull/{num}")
        for owner, repo, num in RE_PR_SHORT.findall(text):
            name = clean_repo(owner, repo)
            if name:
                _add(f"{name}#{num}")
        for num in RE_PR_BARE.findall(text):
            _add(f"PR #{int(num)}")
    if obj is not None:
        _walk_structured_pr_links(obj, _add)
    return out


def _all_pr_links_for_cid(con, cid):
    """Full-composer PR scan — not head/tail sampled.

    Returns (links, days_by_link) where days_by_link maps each link string to
    local calendar days from bubble createdAt. Refs must not depend on the
    text sample that only keeps recent assistant messages.
    """
    if not cid:
        return [], {}
    prefix = f"bubbleId:{cid}:"
    try:
        rows = con.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ? AND ("
            "value LIKE '%/pull/%' OR value LIKE '%/pulls/%' "
            "OR value LIKE '%pullNumber%' OR value LIKE '%prNumber%' "
            "OR value LIKE '%pull_number%' OR value LIKE '%pr_number%' "
            "OR value LIKE '%CreatePullRequest%' OR value LIKE '%create_pull_request%' "
            "OR value LIKE '%html_url%' OR value LIKE '%PR #%' OR value LIKE '%PR#%' "
            "OR value LIKE '%pr #%' OR value LIKE '%pull request%' "
            "OR value LIKE '%Pull request%' OR value LIKE '%Pull Request%' "
            "OR value LIKE '%gh pr %')",
            (prefix + "%",)).fetchall()
    except sqlite3.OperationalError:
        return [], {}
    out, seen = [], set()
    days_by_link = collections.defaultdict(set)
    for row in rows:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["value"]
        day = ""
        ts = _bubble_ts_from_raw(raw)
        if ts:
            day = _local_day(ts)
        for link in _pr_links_from_raw(raw):
            if link not in seen:
                seen.add(link)
                out.append(link)
            if day:
                days_by_link[link].add(day)
    return out, {k: sorted(v) for k, v in days_by_link.items()}


def _inject_composer_pr_links(con, texts_by_cid, cids):
    """Merge full-scan PR links into per-session text before _build_refs.

    Returns (added_link_count, day_hints) where day_hints is
    {cid: {link: [YYYY-MM-DD, ...]}}.
    """
    if not texts_by_cid and not cids:
        return 0, {}
    added = 0
    hints = {}
    for cid in cids:
        if not cid or cid in ("_unattributed", "empty-state-draft"):
            continue
        links, days_by_link = _all_pr_links_for_cid(con, cid)
        if days_by_link:
            hints[cid] = days_by_link
        if not links:
            continue
        prev = texts_by_cid.get(cid) or ""
        missing = [l for l in links if l not in prev]
        if not missing:
            continue
        texts_by_cid[cid] = (prev + "\n" + "\n".join(missing)).strip()
        added += len(missing)
    return added, hints


def _link_matches_pr_key(link, key):
    """True when a scraped link string refers to refs PR key owner/repo#N."""
    if not link or not key or "#" not in key:
        return False
    if key in link:
        return True
    repo, _, num = key.partition("#")
    if not repo or not num:
        return False
    if link in (f"PR #{num}", f"PR#{num}", f"pull/{num}", f"pulls/{num}"):
        return True
    return (f"/{repo}/pull/{num}" in link or f"/{repo}/pulls/{num}" in link
            or link.endswith(f"#{num}") and repo.split("/")[-1] in link)


def _set_pr_github_days(pr, created_at, merged_at):
    """Set PR day span from GitHub createdAt/mergedAt via `_local_day`.

    Authoritative for agent-created PRs: chat re-mentions must not move a PR
    onto Today just because the conversation still talks about it.
    Returns True when days were set.
    """
    created = _local_day(created_at or "")
    merged = _local_day(merged_at or "")
    days = sorted({d for d in (created, merged) if d})
    if not days:
        return False
    pr["first_day"] = days[0]
    pr["last_day"] = days[-1]
    pr["days"] = days
    pr["day_source"] = "github"
    return True


def _stamp_pr_mention_days(refs, priced_all, turn_prs, pr_day_hints=None):
    """Fill first_day/last_day from turn/bubble text when GitHub has not stamped.

    Skips PRs with day_source=github so a later chat mention cannot drag #20
    (created yesterday local) onto Today. Days use `_local_day` (machine TZ).
    """
    for sid, r in (refs or {}).items():
        if not r:
            continue
        days_by_key = collections.defaultdict(set)
        day_by_ti = {}
        for turn in (priced_all or {}).get(sid) or []:
            day = _local_day(turn.get("started_at") or "")
            ti = turn.get("turn_index")
            if day and ti is not None:
                day_by_ti[ti] = day
        for ti, found in ((turn_prs or {}).get(sid) or {}).items():
            day = day_by_ti.get(ti)
            if not day:
                continue
            for k in found:
                days_by_key[k].add(day)
        for link, days in ((pr_day_hints or {}).get(sid) or {}).items():
            for p in r.get("prs") or []:
                k = p.get("key") or ""
                if _link_matches_pr_key(link, k):
                    days_by_key[k].update(days)
        stamped = []
        for p in r.get("prs") or []:
            p = dict(p)
            if p.get("day_source") == "github" and p.get("first_day") and p.get("last_day"):
                stamped.append(p)
                continue
            k = p.get("key") or ""
            ds = set(days_by_key.get(k) or [])
            ds.update(p.get("days") or [])
            if p.get("first_day"):
                ds.add(p["first_day"])
            if p.get("last_day"):
                ds.add(p["last_day"])
            ds = sorted(d for d in ds if d)
            if ds:
                p["first_day"] = ds[0]
                p["last_day"] = ds[-1]
                p["days"] = ds
                p.setdefault("day_source", "mention")
            stamped.append(p)
        r["prs"] = stamped


def _merge_pr_day_fields(pr, *extra_days):
    """Union local day stamps onto a PR dict; returns the same dict."""
    days = set(pr.get("days") or [])
    if pr.get("first_day"):
        days.add(pr["first_day"])
    if pr.get("last_day"):
        days.add(pr["last_day"])
    for d in extra_days:
        if d:
            days.add(d)
    ds = sorted(d for d in days if d)
    if ds:
        pr["first_day"] = ds[0]
        pr["last_day"] = ds[-1]
        pr["days"] = ds
    return pr


def _normalize_github_repo(repo):
    """owner/name from a repository field that may be a URL or multi-repo label."""
    repo = (repo or "").strip()
    if not repo or " · " in repo:
        return ""
    repo = repo.replace("https://github.com/", "").replace("http://github.com/", "")
    repo = repo.strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo.count("/") != 1:
        return ""
    return repo


def _gh_json(args, timeout=60):
    """Run `gh … --json` and parse stdout; return None on failure."""
    try:
        r = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout or "null")
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        return None


def _list_repo_prs_via_gh(repo, limit=100):
    """List recent PRs for owner/repo via the gh CLI (empty list on failure)."""
    repo = _normalize_github_repo(repo)
    if not repo:
        return []
    data = _gh_json([
        "pr", "list", "--repo", repo, "--state", "all", "--limit", str(limit),
        "--json", "number,url,createdAt,mergedAt,body,title,state,headRefName",
    ])
    return data if isinstance(data, list) else []


def _prs_linked_to_cloud_agent(agent_id, repo):
    """PRs whose body embeds the cloud agent id (Cursor PR footer).

    Stamps first_day/last_day from GitHub createdAt/mergedAt via `_local_day`
    (machine local timezone).
    """
    agent_id = (agent_id or "").strip()
    repo = _normalize_github_repo(repo)
    if not agent_id or not repo:
        return []
    alts = {agent_id}
    if agent_id.startswith("bc-"):
        alts.add(agent_id[3:])
    else:
        alts.add("bc-" + agent_id)
    out = []
    for raw in _list_repo_prs_via_gh(repo):
        body = raw.get("body") or ""
        if not any(a and a in body for a in alts):
            continue
        try:
            num = int(raw.get("number"))
        except (TypeError, ValueError):
            continue
        created = _local_day(raw.get("createdAt") or "")
        merged = _local_day(raw.get("mergedAt") or "")
        days = sorted({d for d in (created, merged) if d})
        out.append({
            "key": f"{repo}#{num}",
            "repo": repo,
            "number": num,
            "created": True,
            "url": raw.get("url") or f"https://github.com/{repo}/pull/{num}",
            "first_day": days[0] if days else "",
            "last_day": days[-1] if days else "",
            "days": days,
            "day_source": "github",
            "source": "cloud-agent-github",
        })
    return out


def _enrich_cloud_agent_prs(sessions, refs):
    """Attach every GitHub PR linked to a cloud agent — not only scraped bubbles.

    Cloud agent ManagePullRequest footers embed bc-*; local bubble scans often
    miss older PRs (#21–#24) while keeping a few recent URLs. GitHub is source
    of truth for the full set and for create/merge local days.
    """
    cache = {}
    added = 0
    for sess in sessions or []:
        aid = (sess.get("cloud_agent_id") or "").strip()
        sid = (sess.get("session_id") or "").strip()
        if not aid and sid.startswith("bc-"):
            aid = sid
        if not (sess.get("cloud_agent") or (aid or "").startswith("bc-")
                or sid.startswith("bc-")):
            continue
        if not aid:
            continue
        repo = _normalize_github_repo(sess.get("repository") or "")
        if not repo:
            for tr in sess.get("tracked_repos") or []:
                repo = _normalize_github_repo(tr)
                if repo:
                    break
        if not repo:
            # Fall back to repo already present on session PR refs.
            for p in (refs.get(sid) or {}).get("prs") or []:
                repo = _normalize_github_repo(p.get("repo") or "")
                if repo:
                    break
        if not repo:
            continue
        key = (repo, aid)
        if key not in cache:
            cache[key] = _prs_linked_to_cloud_agent(aid, repo)
        found = cache[key]
        if not found:
            continue
        r = refs.setdefault(sid, {"jira": [], "prs": [], "repos": []})
        existing = {p["key"]: dict(p) for p in r.get("prs") or []}
        for p in found:
            if p["key"] not in existing:
                existing[p["key"]] = dict(p)
                added += 1
            else:
                e = existing[p["key"]]
                # GitHub create/merge days win — do not union with chat re-mentions.
                if p.get("days"):
                    e["first_day"] = p["first_day"]
                    e["last_day"] = p["last_day"]
                    e["days"] = list(p["days"])
                    e["day_source"] = "github"
                e["created"] = e.get("created") or p.get("created")
                e["source"] = e.get("source") or p.get("source")
        r["prs"] = sorted(existing.values(), key=lambda p: (p["repo"], p["number"]))
        sess["refs"] = r
        # Ensure primary repo badge exists.
        repos = {x.get("name"): dict(x) for x in r.get("repos") or []}
        if repo not in repos:
            repos[repo] = {"name": repo, "role": "primary"}
            r["repos"] = sorted(repos.values(),
                                key=lambda x: (x.get("role") != "primary", x["name"]))
    return added


def _stamp_pr_github_dates(refs):
    """Stamp every resolvable PR from GitHub createdAt/mergedAt (local days).

    Always overwrites mention-based days. Otherwise a Today chat that re-mentions
    #20 (created yesterday local) stamps first_day=today, GitHub is skipped, and
    the day filter drops undated #25–#28 — Today shows only #20.
    """
    need = collections.defaultdict(list)  # repo -> [pr dicts]
    for r in (refs or {}).values():
        for p in (r or {}).get("prs") or []:
            repo = _normalize_github_repo(p.get("repo") or "")
            if repo and p.get("number") is not None:
                need[repo].append(p)
    if not need:
        return 0
    stamped = 0
    for repo, prs in need.items():
        catalog = {int(x["number"]): x for x in _list_repo_prs_via_gh(repo)
                   if x.get("number") is not None}
        for p in prs:
            meta = catalog.get(int(p["number"]))
            if not meta:
                continue
            before = p.get("first_day"), p.get("last_day"), p.get("day_source")
            if _set_pr_github_days(p, meta.get("createdAt") or "",
                                   meta.get("mergedAt") or ""):
                if (p.get("first_day"), p.get("last_day"), p.get("day_source")) != before:
                    stamped += 1
    return stamped


def _filter_refs_to_day_range(refs_entry, start, end):
    """Return refs unchanged.

    Day-filtering PR badges was the regression. Pre-#27 (and Copilot) clip spend
    only and leave refs.prs intact. Filtering hid undated PRs or, after GitHub
    stamps, left Today with a single wrong badge (#20 only).
    """
    del start, end
    return refs_entry





def _truncate_prefer_pr(text, limit=12000):
    """Clip turn text but always retain PR links that would otherwise be cut."""
    if not text or len(text) <= limit:
        return text or ""
    links = _pr_links_from_raw(text)
    # Also keep bare PR # lines when present in the visible text.
    for ln in text.split("\n"):
        if RE_PR_BARE.search(ln) and ln.strip() not in links:
            links.append(ln.strip())
    clipped = text[:limit]
    missing = [p for p in links if p not in clipped]
    if not missing:
        return clipped
    tail = "\n" + "\n".join(missing)
    keep = max(0, limit - len(tail))
    return (clipped[:keep] + tail)[:limit]


def _sample_indices_prefer_pr(n, cap, pr_indices, head_budget=None):
    """Pick up to `cap` indices: all PR hits (prefer ends if too many), else fill head+tail."""
    if n <= cap:
        return list(range(n))
    head_budget = BUBBLE_HEAD_PER_COMPOSER if head_budget is None else head_budget
    head = min(head_budget, cap // 2)
    pr_sorted = sorted({i for i in pr_indices if 0 <= i < n})
    if len(pr_sorted) >= cap:
        tail = cap - head
        if tail <= 0:
            return pr_sorted[:cap]
        return pr_sorted[:head] + pr_sorted[-tail:]
    selected = set(pr_sorted)
    remaining = cap - len(selected)
    head_n = min(head, remaining)
    for i in range(min(head_n, n)):
        selected.add(i)
    remaining = cap - len(selected)
    if remaining > 0:
        for i in range(n - 1, -1, -1):
            if i in selected:
                continue
            selected.add(i)
            remaining -= 1
            if remaining <= 0:
                break
    return sorted(selected)


def _sample_bubble_rows(rows, cap=BUBBLE_CAP_PER_COMPOSER):
    """Keep PR-mention bubbles plus earliest/latest when a composer exceeds the cap."""
    if len(rows) <= cap:
        return rows
    rows = sorted(rows, key=_bubble_sort_key)
    pr_idx = [i for i, r in enumerate(rows) if _text_mentions_pr(r[1])]
    keep = _sample_indices_prefer_pr(len(rows), cap, pr_idx)
    return [rows[i] for i in keep]


def _bubble_ts_from_raw(raw):
    """Extract createdAt from bubble JSON without a full parse."""
    if raw is None:
        return 0
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        idx = raw.find('"createdAt"')
        if idx < 0:
            return 0
        rest = raw[idx + len('"createdAt"'):].lstrip(": \t")
        if rest.startswith('"'):
            end = rest.find('"', 1)
            return _parse_turn_ts(rest[1:end]) or 0
        digits = []
        for ch in rest:
            if ch.isdigit():
                digits.append(ch)
            elif digits:
                break
        if digits:
            return _parse_turn_ts(int("".join(digits))) or 0
    except (TypeError, ValueError):
        pass
    return 0


def _pr_candidate_raw_rows(con, prefix):
    """Bubble rows whose JSON likely contains a PR URL or bare PR mention."""
    try:
        return con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? AND ("
            "value LIKE '%/pull/%' OR value LIKE '%/pulls/%' "
            "OR value LIKE '%PR #%' OR value LIKE '%PR#%' "
            "OR value LIKE '%pr #%' OR value LIKE '%pull request%' "
            "OR value LIKE '%Pull request%' OR value LIKE '%Pull Request%')",
            (prefix + "%",)).fetchall()
    except sqlite3.OperationalError:
        return []


def _sample_cid_bubble_raws(con, cid, cap=BUBBLE_CAP_PER_COMPOSER):
    """Return [(key, raw)] — PR mentions kept, then chronological head+tail fill."""
    prefix = f"bubbleId:{cid}:"
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?",
            (prefix + "%",)).fetchone()[0]
    except sqlite3.OperationalError:
        return []
    if n == 0:
        return []
    if n <= cap:
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (prefix + "%",)).fetchall()
        rows.sort(key=lambda r: (_bubble_ts_from_raw(r[1]), r[0].split(":")[-1]))
        return rows
    head = min(BUBBLE_HEAD_PER_COMPOSER, cap // 2)
    tail = cap - head
    # Huge composers: avoid loading every value; pull PR candidates via LIKE + ends.
    if n > cap * 10:
        by_key = {}
        for key, raw in _pr_candidate_raw_rows(con, prefix):
            if _raw_mentions_pr(raw):
                by_key[key] = raw
        keys = [r[0] for r in con.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE ? ORDER BY key LIMIT ?",
            (prefix + "%", head))]
        keys += [r[0] for r in con.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE ? ORDER BY key DESC LIMIT ?",
            (prefix + "%", tail))]
        for key in keys:
            if key in by_key:
                continue
            raw = con.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
            if raw:
                by_key[key] = raw[0]
        out = list(by_key.items())
        out.sort(key=lambda r: (_bubble_ts_from_raw(r[1]), r[0].split(":")[-1]))
        if len(out) <= cap:
            return out
        pr_idx = [i for i, r in enumerate(out) if _raw_mentions_pr(r[1])]
        keep = _sample_indices_prefer_pr(len(out), cap, pr_idx)
        return [out[i] for i in keep]
    entries = []
    for key, raw in con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (prefix + "%",)):
        entries.append((_bubble_ts_from_raw(raw), key.split(":")[-1], key, raw))
    entries.sort(key=lambda e: (e[0], e[1]))
    pr_idx = [i for i, e in enumerate(entries) if _raw_mentions_pr(e[3])]
    keep = _sample_indices_prefer_pr(len(entries), cap, pr_idx)
    return [(entries[i][2], entries[i][3]) for i in keep]


def _bubble_dict_from_raw(key, raw):
    parts = key.split(":")
    if len(parts) < 3:
        return None
    blob = _loads(raw)
    if not isinstance(blob, dict) or blob.get("isRefunded"):
        return None
    tc = blob.get("tokenCount") or {}
    mi = blob.get("modelInfo") if isinstance(blob.get("modelInfo"), dict) else {}
    text = _bubble_text(blob)
    # Tool / agent payloads often bury PR URLs outside text/richText — fold them in.
    extra = _pr_links_from_raw(raw if raw is not None else blob)
    if extra:
        for link in extra:
            if link not in (text or ""):
                text = f"{text}\n{link}".strip() if text else link
    inn = float(tc.get("inputTokens") or 0)
    out = float(tc.get("outputTokens") or 0)
    btype = blob.get("type") or 2
    if not text and not inn and not out and btype != 1:
        return None
    owner = blob.get("repoOwner") or ""
    owner_uid = owner.split("|")[-1] if isinstance(owner, str) and "user_" in owner else ""
    return {
        "id": parts[-1],
        "type": btype,
        "created": blob.get("createdAt") or "",
        "text": text,
        "inn": inn,
        "out": out,
        "model": _norm_model(mi.get("modelName") or "") if mi.get("modelName") else "",
        "owner_uid": owner_uid,
    }


def _rows_to_bubbles(rows):
    return [{
        "id": r["id"],
        "type": r["type"],
        "created": r["created"],
        "text": r["text"],
        "inn": r["inn"],
        "out": r["out"],
        "model": r["model"],
    } for r in rows]


def _load_bubbles_for_cid(con, cid, cap=BUBBLE_CAP_PER_COMPOSER):
    """Load one composer's bubbles in time order, sampling head+tail if over cap."""
    parsed = []
    owners = collections.Counter()
    for key, raw in _sample_cid_bubble_raws(con, cid, cap=cap):
        row = _bubble_dict_from_raw(key, raw)
        if not row:
            continue
        if row.get("owner_uid"):
            owners[row["owner_uid"]] += 1
        parsed.append(row)
    parsed.sort(key=lambda r: _bubble_sort_key((r["created"], r["text"], r["type"], r["id"])))
    return _rows_to_bubbles(parsed), owners


def _load_bubble_text_one(db_path, cid, cap=BUBBLE_CAP_PER_COMPOSER):
    with connect(db_path) as con:
        kept = []
        for key, raw in _sample_cid_bubble_raws(con, cid, cap=cap):
            row = _bubble_dict_from_raw(key, raw)
            if row and (row["text"] or row["type"] == 1):
                kept.append(row)
        if not kept:
            return cid, "", [], 0
        kept.sort(key=lambda r: _bubble_sort_key((r["created"], r["text"], r["type"], r["id"])))
        text = "\n".join(r["text"] for r in kept if r["text"])
        return cid, text, kept, len(kept)


def _load_bubble_texts_for_cids(con, cids, cap=BUBBLE_CAP_PER_COMPOSER):
    """Chat text for billed composers — per-cid query, no local cost rebuild."""
    del con  # callers hold a connection; workers open their own for SQLite threading
    work = [c for c in cids if c and c not in ("_unattributed", "empty-state-draft")]
    if not work:
        return {}, {}, 0
    texts = {}
    rows_by_cid = {}
    loaded = 0
    workers = min(6, len(work))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, text, kept, n in pool.map(
                lambda c: _load_bubble_text_one(DB_PATH, c, cap), work):
            if kept:
                texts[cid] = text
                rows_by_cid[cid] = kept
                loaded += n
    return texts, rows_by_cid, loaded


def _attach_text_to_priced_turns(priced, bubble_rows):
    """Map bubble text onto billed turns by timestamp."""
    if not priced or not bubble_rows:
        return
    turn_ts = [_parse_turn_ts(t.get("started_at")) or 0 for t in priced]
    chunks = [[] for _ in priced]
    ti = 0
    for row in bubble_rows:
        bts = _parse_turn_ts(row.get("created")) or 0
        while ti + 1 < len(turn_ts) and bts > turn_ts[ti + 1]:
            ti += 1
        text = row.get("text") or ""
        if text:
            chunks[ti].append(text)
    for i, turn in enumerate(priced):
        if chunks[i]:
            turn["text"] = _truncate_prefer_pr("\n".join(chunks[i]), 12000)


def _merge_ref_dicts(base, extra):
    """Merge jira/pr/repo refs from a second _build_refs pass."""
    if not extra:
        return base or {"jira": [], "prs": [], "repos": []}
    if not base:
        return extra
    jira = {k: True for k in base.get("jira") or []}
    for k in extra.get("jira") or []:
        jira[k] = True
    prs = {p["key"]: dict(p) for p in base.get("prs") or []}
    for p in extra.get("prs") or []:
        pk = p["key"]
        if pk in prs:
            prs[pk]["created"] = prs[pk].get("created") or p.get("created")
            prs[pk]["inferred"] = prs[pk].get("inferred") or p.get("inferred")
        else:
            prs[pk] = dict(p)
    repos = {r["name"]: dict(r) for r in base.get("repos") or []}
    for r in extra.get("repos") or []:
        name = r["name"]
        if name in repos:
            if repos[name].get("role") != "primary" and r.get("role") == "primary":
                repos[name]["role"] = "primary"
        else:
            repos[name] = dict(r)
    return {
        "jira": sorted(jira, key=lambda k: (k.split("-")[0], int(k.split("-")[1]))),
        "prs": sorted(prs.values(), key=lambda p: (p["repo"], p["number"])),
        "repos": sorted(repos.values(), key=lambda r: (r["role"] != "primary", r["name"])),
    }


def _fetch_git_activities(repos, since_day, until_day):
    if not repos or not since_day or not until_day:
        return []
    key = (tuple(sorted(r["path"] for r in repos)), since_day, until_day)
    now = time.time()
    cached = _GIT_CACHE.get(key)
    if cached and now - cached[0] < 600:
        return cached[1]
    since = f"{since_day} 00:00:00"
    until = f"{until_day} 23:59:59"
    activities = []

    def _git_log_one(repo):
        path = repo["path"]
        gh = repo["github"]
        out = []
        try:
            r = subprocess.run(
                ["git", "-C", path, "log",
                 f"--since={since}", f"--until={until}",
                 "--format=%H|%at|%s|%ae"],
                capture_output=True, text=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return out
        for line in (r.stdout or "").splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            sha, ts_s, subj, author = parts[0], parts[1], parts[2], parts[3]
            try:
                ts = int(ts_s)
            except ValueError:
                continue
            act = {
                "repo_path": path,
                "repo": gh,
                "sha": sha[:8],
                "ts": ts,
                "subject": subj[:120],
                "author": author,
                "pr": None,
            }
            m = RE_MERGE_PR.search(subj)
            if m and "/" in gh:
                act["pr"] = f"{gh}#{m.group(1)}"
            out.append(act)
        return out

    workers = min(6, max(1, len(repos)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in pool.map(_git_log_one, repos):
            activities.extend(chunk)
    activities.sort(key=lambda a: a["ts"])
    _GIT_CACHE[key] = (now, activities)
    return activities


def _git_nearby_for_turn(ts, activities, window=GIT_CORR_WINDOW, limit=3, path_by_github=None):
    if ts is None:
        return []
    hits = []
    for act in activities:
        repo_path = act.get("repo_path") or ""
        if path_by_github and repo_path and not _repo_existed_at(repo_path, ts):
            continue
        dist = abs(act["ts"] - ts)
        if dist <= window:
            hits.append({
                "repo": act["repo"],
                "sha": act["sha"],
                "subject": act["subject"],
                "pr": act.get("pr"),
                "delta_sec": act["ts"] - ts,
            })
    hits.sort(key=lambda x: abs(x["delta_sec"]))
    seen = set()
    out = []
    for h in hits:
        k = (h["repo"], h["sha"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _correlate_git_to_sessions(sessions, priced_all, refs, turns_api, activities,
                               window=GIT_CORR_WINDOW, path_by_github=None):
    if not activities:
        return
    targets = _sessions_for_git_correlation(sessions)
    if not targets:
        return
    for sess in targets:
        sid = sess["session_id"]
        turns = priced_all.get(sid) or []
        if not turns:
            continue
        repo_hits = collections.Counter()
        repo_commits = collections.defaultdict(list)
        pr_hits = collections.Counter()
        pr_commits = collections.defaultdict(list)
        day_rows = collections.defaultdict(lambda: {"cost_usd": 0.0, "repos": set()})

        for turn in turns:
            ts = _parse_turn_ts(turn.get("started_at") or "")
            cost = turn.get("cost_usd") or 0
            day = (turn.get("started_at") or "")[:10]
            if day:
                day_rows[day]["cost_usd"] += cost
            nearby = _git_nearby_for_turn(ts, activities, window=window, limit=3,
                                          path_by_github=path_by_github)
            if nearby:
                turn["git_nearby"] = nearby
                best_per_repo = {}
                for act in activities:
                    if ts is None:
                        break
                    repo_path = act.get("repo_path") or ""
                    if repo_path and not _repo_existed_at(repo_path, ts):
                        continue
                    dist = abs(act["ts"] - ts)
                    if dist > window:
                        continue
                    repo = act["repo"]
                    prev = best_per_repo.get(repo)
                    if not prev or dist < prev[0]:
                        best_per_repo[repo] = (dist, act)
                if not best_per_repo:
                    continue
                best_repo, (dist, act) = min(best_per_repo.items(), key=lambda x: x[1][0])
                repo_hits[best_repo] += cost
                repo_commits[best_repo].append({
                    "repo": act["repo"], "sha": act["sha"], "subject": act["subject"],
                    "pr": act.get("pr"), "delta_sec": act["ts"] - ts,
                })
                if act.get("pr"):
                    pr_hits[act["pr"]] += cost
                    pr_commits[act["pr"]].append({
                        "repo": act["repo"], "sha": act["sha"], "subject": act["subject"],
                        "pr": act["pr"], "delta_sec": act["ts"] - ts,
                    })
                if day:
                    day_rows[day]["repos"].add(best_repo)

        if turns_api.get(sid):
            api_by_idx = {t["turn_index"]: t for t in turns_api[sid]}
            for turn in turns:
                gn = turn.get("git_nearby")
                if gn and turn["turn_index"] in api_by_idx:
                    api_by_idx[turn["turn_index"]]["git_nearby"] = gn

        if not repo_hits:
            sess["git_correlation"] = {
                "matched": False,
                "window_hours": window / 3600,
                "repos_scanned": len({a["repo"] for a in activities}),
            }
            continue

        r = refs.get(sid) or {"jira": [], "prs": [], "repos": []}
        top_repo = _pick_git_repo(repo_hits)
        _top_cost = repo_hits[top_repo]
        merged_repos = {x["name"]: x.get("role") or "mentioned" for x in r.get("repos") or []}
        merged_repos[top_repo] = "primary"
        r["repos"] = sorted(
            ({"name": k, "role": v} for k, v in merged_repos.items()),
            key=lambda x: (x["role"] != "primary", x["name"]))
        existing_prs = {p["key"]: dict(p) for p in r.get("prs") or []}
        for pk, pcost in pr_hits.items():
            if pcost <= 0:
                continue
            if pk not in existing_prs:
                existing_prs[pk] = {
                    "key": pk, "repo": pk.split("#")[0],
                    "number": int(pk.split("#")[1]),
                    "created": False, "inferred": True,
                }
            else:
                existing_prs[pk]["inferred"] = True
        r["prs"] = sorted(existing_prs.values(), key=lambda p: (p["repo"], p["number"]))
        refs[sid] = r
        sess["refs"] = r
        sess["repository"] = top_repo
        sess["workspace"] = top_repo
        sess["repo_source"] = "git"
        sess["repo_split"] = 0

        matched_cost = sum(repo_hits.values())
        sess_cost = sess.get("cost_usd") or 0
        top_repos = []
        for repo, cost in repo_hits.most_common(8):
            commits = repo_commits.get(repo, [])
            top_repos.append({
                "name": repo,
                "cost_usd": round(cost, 4),
                "turn_matches": len(commits),
                "unique_commits": len({c["sha"] for c in commits}),
                "sample": [{
                    "sha": c["sha"],
                    "subject": c["subject"],
                    "delta_h": round(c["delta_sec"] / 3600, 1),
                } for c in commits[:3]],
                "prs": sorted({c["pr"] for c in commits if c.get("pr")}),
            })

        top_prs = []
        for pk, pcost in pr_hits.most_common(8):
            commits = pr_commits.get(pk, [])
            top_prs.append({
                "key": pk,
                "repo": pk.split("#")[0],
                "number": int(pk.split("#")[1]),
                "cost_usd": round(pcost, 4),
                "inferred": True,
                "turn_matches": len(commits),
                "unique_commits": len({c["sha"] for c in commits}),
                "sample": [{
                    "sha": c["sha"],
                    "subject": c["subject"],
                    "delta_h": round(c["delta_sec"] / 3600, 1),
                } for c in commits[:3]],
            })

        sess["git_correlation"] = {
            "matched": True,
            "window_hours": window / 3600,
            "matched_cost_usd": round(matched_cost, 4),
            "unmatched_cost_usd": round(max(0.0, sess_cost - matched_cost), 4),
            "repos": top_repos,
            "prs": top_prs,
            "days": sorted(
                [{"day": d, "cost_usd": round(v["cost_usd"], 4),
                  "repos": sorted(v["repos"])}
                 for d, v in day_rows.items() if v["repos"]],
                key=lambda x: x["day"]),
        }
        parts = []
        for row in top_repos[:5]:
            label = row["name"].split("/")[-1]
            extra = f", {len(row['prs'])} PR" if row["prs"] else ""
            parts.append(f"{label} ${row['cost_usd']:.2f}{extra}")
        if parts:
            hint = (
                f" Nearby git activity (±{window // 3600}h) suggests: "
                + "; ".join(parts) + ". Inferred from commit timestamps — not from Cursor metadata."
            )
            sess["billing_note"] = (sess.get("billing_note") or "") + hint


def _billing_event_days(events):
    days = []
    for ev in events or []:
        ms = int(ev.get("timestamp") or 0)
        if not ms:
            continue
        dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
        days.append(dt.strftime("%Y-%m-%d"))
    return days


def _plain_from_rich(node, acc, depth=0):
    if depth > 16 or node is None:
        return
    if isinstance(node, dict):
        text = node.get("text")
        if node.get("type") == "text" and isinstance(text, str):
            acc.append(text)
        elif isinstance(node.get("mentionName"), str):
            acc.append(node["mentionName"])
        for val in node.values():
            if val is text:
                continue
            _plain_from_rich(val, acc, depth + 1)
    elif isinstance(node, list):
        for val in node:
            _plain_from_rich(val, acc, depth + 1)
    elif isinstance(node, str) and node[:1] in "{[":
        try:
            _plain_from_rich(json.loads(node), acc, depth + 1)
        except ValueError:
            pass


def _bubble_text(blob):
    text = blob.get("text")
    if isinstance(text, str) and text.strip():
        return text
    acc = []
    _plain_from_rich(blob.get("richText"), acc)
    think = blob.get("allThinkingBlocks") or []
    if isinstance(think, list):
        for block in think:
            if isinstance(block, dict):
                acc.append(block.get("text") or "")
            elif isinstance(block, str):
                acc.append(block)
    return " ".join(a for a in acc if a).strip()


def _logical_stamp():
    """Cheap invalidation key. Ignores checkpoint/WAL churn that is not a new chat."""
    with connect() as con:
        try:
            headers = tuple(con.execute(
                "SELECT COUNT(*), COALESCE(MAX(lastUpdatedAt),0) FROM composerHeaders"
            ).fetchone())
        except sqlite3.OperationalError:
            headers = (0, 0)
        try:
            composers = con.execute(
                "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            composers = 0
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='cursorAuth/cachedEmail'").fetchone()
            email = str(row["value"]).strip() if row and row["value"] else ""
        except sqlite3.OperationalError:
            email = ""
    return (headers, composers, email)


_CACHE = {"stamp": None, "data": None, "at": 0}


def _cursor_session_cookie(con):
    """Build the Cursor dashboard session cookie from the IDE's stored JWT."""
    token = None
    for key in ("cursorAuth/accessToken", "cursorAuth/cachedAccessToken"):
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        if row and row["value"]:
            token = str(row["value"]).strip()
            break
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        user = str(payload.get("sub") or "").split("|")[-1]
    except Exception:
        return None
    if not user:
        return None
    return f"WorkosCursorSessionToken={user}::{token}"


def _jwt_sub(token):
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return str(payload.get("sub") or "").split("|")[-1]
    except Exception:
        return ""


def _signed_in_account(con):
    """Currently signed-in Cursor email, plus git author email if it differs."""
    email = ""
    row = con.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/cachedEmail'").fetchone()
    if row and row["value"]:
        email = str(row["value"]).strip()
    git_email = ""
    row = con.execute("SELECT value FROM ItemTable WHERE key='vscode.git'").fetchone()
    if row and row["value"]:
        try:
            raw = row["value"]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            git_email = (json.loads(raw) or {}).get("userAndEmailCacher.gitAuthorEmail") or ""
        except Exception:
            git_email = ""
    user_id = ""
    for key in ("cursorAuth/accessToken", "cursorAuth/cachedAccessToken"):
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        if row and row["value"]:
            user_id = _jwt_sub(str(row["value"]).strip())
            if user_id:
                break
    previous = git_email.strip() if git_email and git_email.lower() != email.lower() else ""
    return {"email": email, "git_email": git_email.strip(), "user_id": user_id,
            "previous_email": previous}


def _cursor_license_email(path=None):
    """Email on the currently signed-in Cursor license / subscription."""
    try:
        with connect(path) as con:
            return (_signed_in_account(con).get("email") or "").strip()
    except Exception:
        return ""


def _api(cookie, method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Cookie": cookie,
        "Origin": "https://cursor.com",
        "User-Agent": "Mozilla/5.0 (Cursor dashboard)",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        "https://cursor.com" + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        snippet = exc.read()[:240].decode("utf-8", "replace")
        raise RuntimeError(f"Cursor API {exc.code} {path}: {snippet}") from exc
    return json.loads(raw) if raw else {}


def _event_cents(ev):
    kind = (ev.get("kind") or "").upper()
    if "ERRORED" in kind:
        return 0.0
    tu = ev.get("tokenUsage") or {}
    if tu.get("totalCents") is not None:
        return float(tu["totalCents"])
    if ev.get("chargedCents") is not None:
        return float(ev["chargedCents"])
    return 0.0


def _event_kind_label(kind):
    k = (kind or "").upper()
    if "USAGE_BASED" in k:
        return "on-demand"
    if "INCLUDED" in k:
        return "included"
    if "ERRORED" in k:
        return "errored"
    return (kind or "usage").replace("USAGE_EVENT_KIND_", "").replace("_", " ").lower()


def _cents_usd(value):
    try:
        return float(value or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


# Subscription included compute is split into two pools (Cursor Settings →
# Plan & Usage): Cursor Models and Other Models.
POOL_CURSOR = "cursor"
POOL_OTHER = "other"
POOL_LABELS = {
    POOL_CURSOR: "Cursor Models",
    POOL_OTHER: "Other Models",
}
# Match Cursor Settings copy under Plan & Usage.
POOL_DETAILS = {
    POOL_CURSOR: "Includes Cursor Grok and Composer",
    POOL_OTHER: "Named and third-party model APIs",
}
POOL_FOOTNOTES = {
    POOL_CURSOR: "Additional usage beyond limits consumes Other Models quota or on-demand spend.",
    POOL_OTHER: "Additional usage beyond limits consumes on-demand spend.",
}
RE_PCT_MSG = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _int_tokens(value):
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _iso_to_ms(value):
    """RFC3339 / unix seconds / unix ms -> unix milliseconds."""
    if not value:
        return 0
    try:
        if isinstance(value, (int, float)) or (
                isinstance(value, str) and str(value).strip().isdigit()):
            n = int(value)
            return n if n >= 10 ** 12 else n * 1000
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, OSError):
        return 0


def _finite_pct(value):
    """Return a non-negative finite percentage, or None if missing/unusable."""
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return max(0.0, n)


def _parse_percent_from_message(msg):
    """Pull the leading N% out of Cursor display copy, e.g. 'You've used 98%…'."""
    if not msg:
        return None
    m = RE_PCT_MSG.search(str(msg))
    return _finite_pct(m.group(1)) if m else None


def _first_present(obj, *keys):
    """Return the first present value for any key name (API field aliases)."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj and obj.get(key) is not None and obj.get(key) != "":
            return obj.get(key)
    return None


def _model_pool(model, tier=None):
    """Classify a model into the subscription included-usage pool.

    Cursor Models (Settings: "Includes Cursor Grok and Composer") map to
    dashboard `autoPercentUsed`. Other Models map to `apiPercentUsed`.
    `tier` from GetAggregatedUsageEvents: 2 = Cursor Models, 1 = Other Models.
    """
    try:
        t = int(tier)
    except (TypeError, ValueError):
        t = None
    if t == 2:
        return POOL_CURSOR
    if t == 1:
        return POOL_OTHER
    m = _norm_model(model)
    # Cursor Models pool: Composer, Cursor Grok, and Auto (routes into this pool).
    if m in ("auto", "default") or m.startswith("composer") or "grok" in m:
        return POOL_CURSOR
    return POOL_OTHER


def _pool_row(pool_id, label, detail, used_pct, message, unlimited):
    if unlimited:
        return {
            "id": pool_id,
            "label": label,
            "detail": detail,
            "unlimited": True,
            "used_pct": None,
            "remaining_pct": None,
            "allocated_pct": None,
            "message": message or "Unlimited included usage",
        }
    if used_pct is None:
        return None
    used = float(used_pct)
    return {
        "id": pool_id,
        "label": label,
        "detail": detail,
        "unlimited": False,
        "used_pct": round(used, 1),
        "remaining_pct": round(max(0.0, 100.0 - used), 1),
        "allocated_pct": 100.0,
        "message": message or "",
    }


def _model_utilization(summary, period=None, included_used=None, included_limit=None,
                       pool_spend=None):
    """Included model-pool utilization from usage-summary + get-current-period-usage.

    Allocated is 100% of each included pool. Used/remaining prefer Cursor's
    `autoPercentUsed` / `apiPercentUsed` / `totalPercentUsed` (plus display-message
    and field-name aliases). When those are absent, fall back to included
    spend ÷ limit for the total pool and to metered pool share for Auto vs named.
    """
    summary = summary or {}
    period = period or {}
    indiv = (summary.get("individualUsage") or summary.get("individual_usage")
             or summary.get("usage") or {})
    plan = indiv.get("plan") or {}
    plan_usage = ((period or {}).get("planUsage")
                  or (period or {}).get("plan_usage") or {})
    unlimited = bool(_first_present(summary, "isUnlimited", "unlimited")
                     or _first_present(plan, "unlimited"))
    auto_msg = (_first_present(
        summary, "autoModelSelectedDisplayMessage",
        "autoModelSelectedDisplayMessage", "autoModelDisplayMessage")
        or _first_present(
            period, "autoModelSelectedDisplayMessage",
            "autoModelSelectedDisplayMessage", "autoModelDisplayMessage")
        or "")
    named_msg = (_first_present(
        summary, "namedModelSelectedDisplayMessage",
        "namedModelSelectedDisplayMessage", "namedModelDisplayMessage")
        or _first_present(
            period, "namedModelSelectedDisplayMessage",
            "namedModelSelectedDisplayMessage", "namedModelDisplayMessage")
        or "")
    total_msg = (_first_present(period, "displayMessage", "display_message")
                 or _first_present(summary, "displayMessage", "display_message")
                 or "")

    def _pct(*candidates):
        for raw in candidates:
            if isinstance(raw, str) and "%" in raw:
                n = _parse_percent_from_message(raw)
            else:
                n = _finite_pct(raw)
            if n is not None:
                return n
        return None

    auto_keys = ("autoPercentUsed", "autoPercentUsed", "auto_percent_used",
                 "autoPctUsed", "auto_pct")
    api_keys = ("apiPercentUsed", "apiPercentUsed", "api_percent_used",
                "namedPercentUsed", "apiPctUsed", "api_pct")
    total_keys = ("totalPercentUsed", "totalPercentUsed", "total_percent_used",
                  "totalPctUsed", "total_pct")

    auto_pct = _pct(_first_present(plan, *auto_keys),
                    _first_present(plan_usage, *auto_keys),
                    _parse_percent_from_message(auto_msg))
    api_pct = _pct(_first_present(plan, *api_keys),
                   _first_present(plan_usage, *api_keys),
                   _parse_percent_from_message(named_msg))
    total_pct = _pct(_first_present(plan, *total_keys),
                     _first_present(plan_usage, *total_keys),
                     _parse_percent_from_message(total_msg))

    # Dollar-limit fallback for the total bar when Cursor omits percent fields.
    if total_pct is None and included_limit and included_limit > 0 and included_used is not None:
        total_pct = max(0.0, float(included_used) / float(included_limit) * 100.0)
        if not total_msg:
            total_msg = (f"Derived from included spend "
                         f"(${float(included_used):,.2f} of "
                         f"${float(included_limit):,.2f})")

    # If Auto / named percents are missing but we have per-pool metered spend,
    # approximate each pool's share of the included limit (same dollars Cursor
    # already reports for the cycle). Prefer API percents when present.
    spend = pool_spend or {}
    cursor_spend = float(spend.get(POOL_CURSOR) or 0)
    other_spend = float(spend.get(POOL_OTHER) or 0)
    if included_limit and included_limit > 0:
        if auto_pct is None and cursor_spend > 0:
            auto_pct = max(0.0, cursor_spend / float(included_limit) * 100.0)
            if not auto_msg:
                auto_msg = "Estimated from Cursor Models metered spend this cycle"
        if api_pct is None and other_spend > 0:
            api_pct = max(0.0, other_spend / float(included_limit) * 100.0)
            if not named_msg:
                named_msg = "Estimated from Other Models metered spend this cycle"

    specs = (
        ("cursor", POOL_LABELS[POOL_CURSOR], POOL_DETAILS[POOL_CURSOR],
         auto_pct, auto_msg or POOL_FOOTNOTES[POOL_CURSOR]),
        ("other", POOL_LABELS[POOL_OTHER], POOL_DETAILS[POOL_OTHER],
         api_pct, named_msg or POOL_FOOTNOTES[POOL_OTHER]),
        # Keep a blended total for digests; the Plan & Usage UI emphasizes the
        # two pools above (Cursor Models / Other Models).
        ("total", "Total included", "Subscription included compute",
         total_pct, total_msg),
    )
    pools = []
    for spec in specs:
        row = _pool_row(*spec, unlimited)
        if row:
            pools.append(row)

    # Unlimited plans still get the three cards so the section is visible.
    if unlimited and not pools:
        for spec in specs:
            row = _pool_row(spec[0], spec[1], spec[2], 0.0, spec[4], True)
            if row:
                pools.append(row)

    on_demand = (indiv.get("onDemand") or indiv.get("on_demand")
                 or (summary.get("teamUsage") or {}).get("onDemand")
                 or (summary.get("teamUsage") or {}).get("on_demand")
                 or {})
    return {
        "unlimited": unlimited,
        "membership": (_first_present(summary, "membershipType", "membership_type",
                                      "plan") or ""),
        "pools": pools,
        "auto_pct": auto_pct,
        "api_pct": api_pct,
        "total_pct": total_pct,
        "auto_msg": auto_msg,
        "named_msg": named_msg,
        "display_msg": total_msg,
        "on_demand_enabled": bool(on_demand.get("enabled")),
    }


def _parse_aggregated_usage(raw):
    """Normalize GetAggregatedUsageEvents rows into dashboard model rows."""
    rows = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        model = _norm_model(
            item.get("modelIntent") or item.get("model")
            or item.get("modelName") or item.get("name") or "unknown")
        inn = _int_tokens(item.get("inputTokens") or item.get("input_tokens"))
        out = _int_tokens(item.get("outputTokens") or item.get("output_tokens"))
        cwrite = _int_tokens(
            item.get("cacheWriteTokens") or item.get("cache_write_tokens"))
        cread = _int_tokens(
            item.get("cacheReadTokens") or item.get("cache_read_tokens"))
        cents = item.get("totalCents")
        if cents is None:
            cents = item.get("total_cents")
        try:
            cost = float(cents or 0) / 100.0
        except (TypeError, ValueError):
            cost = 0.0
        pool = _model_pool(model, item.get("tier"))
        rows.append({
            "model": model,
            "pool": pool,
            "pool_label": POOL_LABELS.get(pool, "Other Models"),
            "tier": item.get("tier"),
            "input_tokens": inn,
            "output_tokens": out,
            "cache_write_tokens": cwrite,
            "cache_read_tokens": cread,
            "total_tokens": inn + out + cwrite + cread,
            "cost_usd": round(cost, 6),
            "requests": _int_tokens(
                item.get("requests") or item.get("numRequests")
                or item.get("requestCount")),
        })
    rows.sort(key=lambda r: -r["cost_usd"])
    total = sum(r["cost_usd"] for r in rows)
    for row in rows:
        row["share"] = (row["cost_usd"] / total) if total else 0.0
    return rows


def _pool_spend(rows):
    out = {POOL_CURSOR: 0.0, POOL_OTHER: 0.0}
    for row in rows or []:
        pool = row.get("pool") or POOL_OTHER
        out[pool] = out.get(pool, 0.0) + (row.get("cost_usd") or 0)
    return {k: round(v, 4) for k, v in out.items()}


def _fetch_aggregated_usage(cookie, summary):
    """Per-model cycle totals from cursor.com (same table as the official dashboard)."""
    start_ms = _iso_to_ms(
        _first_present(summary, "billingCycleStart", "billing_cycle_start"))
    end_ms = _iso_to_ms(
        _first_present(summary, "billingCycleEnd", "billing_cycle_end")) or int(
            time.time() * 1000)
    if not start_ms:
        return []
    bodies = [
        {"teamId": 0, "startDate": str(start_ms), "endDate": str(end_ms)},
        {"teamId": 0, "startDate": start_ms, "endDate": end_ms},
    ]
    paths = (
        "/api/dashboard/get-aggregated-usage-events",
        "/api/dashboard/get-aggregated-usage-events",
    )
    last_exc = None
    for path in paths:
        for body in bodies:
            try:
                chunk = _api(cookie, "POST", path, body)
            except Exception as exc:
                last_exc = exc
                continue
            rows = (chunk.get("aggregations")
                    or chunk.get("usageAggregations")
                    or chunk.get("aggregatedUsage")
                    or [])
            if rows:
                return rows
    if last_exc:
        raise last_exc
    return []


PLAN_FEE_LABELS = {
    2000: "Pro",
    6000: "Pro Plus",
    20000: "Ultra",
}


RE_INV_CYCLE = re.compile(r"cycle starting ([A-Za-z]+ \d+(?:,\s*\d{4})?)", re.I)


def _parse_month_day(text, fallback_iso_day=""):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    if re.search(r"\d{4}", text):
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    if fallback_iso_day:
        try:
            year = int(str(fallback_iso_day)[:4])
            for fmt in ("%B %d", "%b %d"):
                try:
                    d = datetime.datetime.strptime(text, fmt).replace(year=year)
                    return d.strftime("%Y-%m-%d")
                except ValueError:
                    pass
        except ValueError:
            pass
    return ""


def _invoice_usage_cycle_start(inv):
    """Billing cycle the usage charge is for (from invoice description), not charge date."""
    m = RE_INV_CYCLE.search(inv.get("description") or "")
    if m:
        return _parse_month_day(m.group(1), inv.get("day"))
    return ""


def _usage_invoice_in_view(inv, start, end, cycle_starts=None):
    """Usage invoices belong to the billing cycle they describe, not the Stripe charge date."""
    net = inv.get("net_usd") or 0
    if net <= 0.004:
        return False
    cycle = inv.get("usage_cycle_start") or _invoice_usage_cycle_start(inv)
    if not cycle:
        day = inv.get("day") or ""
        return bool(day and start <= day <= end)
    try:
        cs = datetime.date.fromisoformat(cycle)
        ce = cs + datetime.timedelta(days=31)
        for s in sorted(cycle_starts or []):
            try:
                nxt = datetime.date.fromisoformat(s)
            except ValueError:
                continue
            if nxt > cs:
                ce = nxt - datetime.timedelta(days=1)
                break
        vs = datetime.date.fromisoformat(start)
        ve = datetime.date.fromisoformat(end)
        return cs <= ve and ce >= vs
    except ValueError:
        return start <= cycle <= end


def _invoice_kind(inv):
    desc = (inv.get("description") or "").strip().lower()
    if inv.get("isMidMonthInvoice"):
        return "usage"
    if any(s in desc for s in ("usage", "on-demand", "correction")):
        return "usage"
    return "subscription"


def _plan_fee_label(amount_cents):
    try:
        return PLAN_FEE_LABELS.get(int(amount_cents), "Subscription")
    except (TypeError, ValueError):
        return "Subscription"


def _public_invoices(raw):
    out = []
    for inv in raw or []:
        gross = _cents_usd(inv.get("amountCents"))
        refund = _cents_usd(inv.get("refundAmount"))
        out.append({
            "day": _local_day(inv.get("date")),
            "kind": _invoice_kind(inv),
            "usage_cycle_start": _invoice_usage_cycle_start({
                "description": (inv.get("description") or "").strip(),
                "day": _local_day(inv.get("date")),
            }),
            "gross_usd": round(gross, 4),
            "refund_usd": round(refund, 4),
            "net_usd": round(gross - refund, 4),
            "status": inv.get("status") or "",
            "description": (inv.get("description") or "").strip(),
            "plan": _plan_fee_label(inv.get("amountCents")),
            "account": (inv.get("_account_email") or "").strip(),
        })
    return out


def _fetch_invoices(cookie):
    invoices, page = [], 1
    while True:
        body = {"page": page} if page > 1 else {}
        chunk = _api(cookie, "POST", "/api/dashboard/list-invoices", body)
        invoices.extend(chunk.get("invoices") or [])
        if not chunk.get("hasMore"):
            break
        page += 1
        if page > 20:
            break
    return invoices


BILLING_START = datetime.datetime(2025, 11, 29, tzinfo=datetime.timezone.utc)
LEGACY_INVOICE_EMAIL = "mw@timberwilde.net"
LEGACY_INVOICE_TOTAL = 435.04  # user-reported Stripe total since BILLING_START


def _cursor_sessions_path():
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(appdata, "cursor-dashboard", "cursor-sessions.json")


def _load_cursor_sessions():
    path = _cursor_sessions_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cursor_sessions(data):
    path = _cursor_sessions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remember_cursor_session(email, cookie, user_id=""):
    email = (email or "").strip()
    cookie = (cookie or "").strip()
    if not email or not cookie or "@" not in email:
        return
    data = _load_cursor_sessions()
    accounts = data.setdefault("accounts", {})
    accounts[email.lower()] = {
        "email": email,
        "cookie": cookie,
        "user_id": user_id or "",
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_cursor_sessions(data)


def _env_session_cookies():
    out = []
    for name in ("CURSOR_SESSION_TOKEN", "CURSOR_LEGACY_SESSION_TOKEN"):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        cookie = raw if raw.startswith("WorkosCursorSessionToken=") else (
            "WorkosCursorSessionToken=" + raw)
        out.append(cookie)
    return out


def _billing_cache_path():
    return os.path.join(os.path.dirname(_cursor_sessions_path()), "billing-cache.json")


def _load_billing_cache():
    path = _billing_cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_billing_cache(email, one):
    email = (email or "").strip().lower()
    if not email:
        return
    data = _load_billing_cache()
    accounts = data.setdefault("accounts", {})
    accounts[email] = {
        "email": one.get("email") or email,
        "user_id": one.get("user_id") or "",
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "events": one.get("events") or [],
        "invoices": one.get("invoices") or [],
        "aggregations": one.get("aggregations") or [],
        "summary": one.get("summary") or {},
        "period": one.get("period") or {},
    }
    path = _billing_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _cached_account_billing(email):
    email = (email or "").strip().lower()
    entry = (_load_billing_cache().get("accounts") or {}).get(email)
    if not entry or not entry.get("events"):
        return None
    email = entry.get("email") or email
    for ev in entry["events"]:
        ev["_account_email"] = ev.get("_account_email") or email
    for inv in entry.get("invoices") or []:
        inv["_account_email"] = inv.get("_account_email") or email
    return {
        "me": {"email": email, "id": entry.get("user_id")},
        "email": email,
        "user_id": entry.get("user_id") or "",
        "summary": entry.get("summary") or {},
        "period": entry.get("period") or {},
        "events": entry["events"],
        "invoices": entry.get("invoices") or [],
        "aggregations": entry.get("aggregations") or [],
        "cookie": "",
        "cached_at": entry.get("fetched_at") or "",
    }


def _fetch_account_billing(cookie, fallback_email="", fallback_user_id=""):
    me = _api(cookie, "GET", "/api/auth/me")
    email = (me.get("email") or me.get("primaryEmail") or fallback_email).strip()
    user_id = me.get("id") or me.get("userId") or fallback_user_id
    if not user_id:
        raise RuntimeError("Could not resolve Cursor user id.")
    summary = _api(cookie, "GET", "/api/usage-summary")
    period = _api(cookie, "POST", "/api/dashboard/get-current-period-usage", {})
    aggregations = []
    try:
        aggregations = _fetch_aggregated_usage(cookie, summary)
    except Exception as exc:
        print(f"  aggregated usage unavailable for {email or user_id} ({exc})",
              flush=True)
    start = BILLING_START
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    events, page = [], 1
    while True:
        chunk = _api(cookie, "POST", "/api/dashboard/get-filtered-usage-events", {
            "teamId": 0,
            "userId": int(user_id),
            "startDate": str(int(start.timestamp() * 1000)),
            "endDate": str(int(end.timestamp() * 1000)),
            "page": page,
            "pageSize": 200,
        })
        batch = chunk.get("usageEventsDisplay") or []
        events.extend(batch)
        total = int(chunk.get("totalUsageEventsCount") or 0)
        if not batch or len(events) >= total:
            break
        page += 1
        if page > 100:
            break
    invoices = []
    try:
        invoices = _fetch_invoices(cookie)
    except Exception as exc:
        print(f"  invoices unavailable for {email or user_id} ({exc})", flush=True)
    for ev in events:
        ev["_account_email"] = email
    for inv in invoices:
        inv["_account_email"] = email
    return {
        "me": me,
        "email": email,
        "user_id": user_id,
        "summary": summary,
        "period": period,
        "aggregations": aggregations,
        "events": events,
        "invoices": invoices,
        "cookie": cookie,
    }


def fetch_billing(con, force=False):
    """Pull billed usage + invoices for every Cursor account we have a session for."""
    now = time.time()
    account = _signed_in_account(con)
    current_email = (account.get("email") or "").strip()
    if ((not force) and _BILLING_CACHE["data"]
            and now - _BILLING_CACHE["at"] < 120
            and _BILLING_CACHE.get("email") == current_email):
        return _BILLING_CACHE["data"]
    current_cookie = _cursor_session_cookie(con)
    if current_cookie:
        _remember_cursor_session(current_email, current_cookie, account.get("user_id") or "")
    jobs = []
    seen = set()
    saved = (_load_cursor_sessions().get("accounts") or {})
    for info in saved.values():
        cookie = (info.get("cookie") or "").strip()
        if cookie and cookie not in seen:
            seen.add(cookie)
            jobs.append((info.get("email") or "", cookie, info.get("user_id") or ""))
    if current_cookie and current_cookie not in seen:
        seen.add(current_cookie)
        jobs.append((current_email, current_cookie, account.get("user_id") or ""))
    for cookie in _env_session_cookies():
        if cookie not in seen:
            seen.add(cookie)
            jobs.append(("", cookie, ""))
    if not jobs:
        raise RuntimeError("Cursor is not signed in on this machine (no access token).")
    merged_events, merged_invoices = [], []
    emails, stale, cached_used = [], [], []
    primary = None
    for label, cookie, saved_uid in jobs:
        one = None
        try:
            one = _fetch_account_billing(cookie, fallback_email=label, fallback_user_id=saved_uid)
        except Exception as exc:
            who = label or "saved session"
            print(f"  billing for {who} failed ({exc})", flush=True)
            if label:
                one = _cached_account_billing(label)
                if one:
                    cached_used.append(label)
                    print(f"  using cached billing for {label} "
                          f"(saved {one.get('cached_at') or 'unknown'})", flush=True)
                else:
                    stale.append(label)
            if not one:
                continue
        email = (one.get("email") or label or "").strip()
        if email and cookie:
            for ev in one["events"]:
                ev["_account_email"] = ev.get("_account_email") or email
            for inv in one["invoices"]:
                inv["_account_email"] = inv.get("_account_email") or email
            if not one.get("cached_at"):
                _remember_cursor_session(email, cookie, str(one.get("user_id") or ""))
                _save_billing_cache(email, one)
            emails.append(email)
        print(f"  {email or 'account'}: {len(one['events'])} billed events, "
              f"{len(one['invoices'])} invoices, "
              f"{len(one.get('aggregations') or [])} model aggregations", flush=True)
        if email.lower() == LEGACY_INVOICE_EMAIL:
            inv_net = sum(
                _cents_usd(i.get("amountCents")) - _cents_usd(i.get("refundAmount"))
                for i in one["invoices"])
            print(f"  {email} invoiced since {BILLING_START.date()}: "
                  f"${inv_net:,.2f} (expected ${LEGACY_INVOICE_TOTAL:,.2f})", flush=True)
        merged_events.extend(one["events"])
        merged_invoices.extend(one["invoices"])
        if current_email and email.lower() == current_email.lower():
            primary = one
        elif primary is None:
            primary = one
    if not primary:
        raise RuntimeError("Could not load Cursor billed usage for any saved account.")
    if stale:
        print("  re-sign into Cursor as " + " / ".join(stale)
              + " once to refresh billing (saved to disk for future merges)", flush=True)
    if cached_used:
        print("  merged cached billing for " + " / ".join(cached_used), flush=True)
    _BILLING_CACHE["data"] = {
        "me": primary["me"],
        "summary": primary["summary"],
        "period": primary["period"],
        "aggregations": primary.get("aggregations") or [],
        "events": merged_events,
        "invoices": merged_invoices,
        "emails": emails,
        "fetched_at": now,
    }
    _BILLING_CACHE["turns"] = {}
    _BILLING_CACHE["at"] = now
    _BILLING_CACHE["email"] = current_email
    return _BILLING_CACHE["data"]


# --- Cloud Agents (api.cursor.com/v1/agents) --------------------------------
# Cloud agent conversation IDs are bc-<uuid>. Billed usage events already carry
# those IDs but the local IDE store has no titles for them. Listing every agent
# via the Cloud Agents API fills titles/repos for ALL cloud sessions and adds
# any agents not yet present on the invoice.
CLOUD_AGENTS_ENABLED = True
CLOUD_AGENTS_API_KEY = (
    os.environ.get("CLOUD_AGENTS_API_KEY")
    or os.environ.get("CURSOR_API_KEY")
    or os.environ.get("CURSOR_CLOUD_API_KEY")
    or os.environ.get("CURSOR_DASH_API_KEY")
    or ""
)
CLOUD_AGENTS_CACHE_PATH = ""
_CLOUD_AGENTS_CACHE = {"at": 0, "agents": None, "error": ""}


def _cloud_agents_cache_path():
    return CLOUD_AGENTS_CACHE_PATH or os.path.join(
        _dashboard_data_dir(), "cloud-agents-cache.json")


def _cloud_api_key():
    """Read the key live from the environment (not only at import time)."""
    return (
        os.environ.get("CLOUD_AGENTS_API_KEY")
        or os.environ.get("CURSOR_API_KEY")
        or os.environ.get("CURSOR_CLOUD_API_KEY")
        or os.environ.get("CURSOR_DASH_API_KEY")
        or CLOUD_AGENTS_API_KEY
        or ""
    ).strip()


def _cloud_api(method, path, timeout=60):
    key = _cloud_api_key()
    if not key:
        raise RuntimeError(
            "Set CLOUD_AGENTS_API_KEY or CURSOR_API_KEY "
            "(Cursor Dashboard → API Keys) to load cloud agents")
    url = "https://api.cursor.com" + path
    auth = base64.b64encode((key + ":").encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url, method=method,
        headers={
            "Authorization": "Basic " + auth,
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"Cloud Agents API HTTP {exc.code} for {path}: {body or exc.reason}") from exc


def _load_cloud_agents_cache_file():
    path = _cloud_agents_cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cloud_agents_cache_file(agents, source="api"):
    path = _cloud_agents_cache_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "agents": agents,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _normalize_cloud_agent(raw):
    """Map API / MCP / cookie background-composer shapes onto one agent record."""
    if not isinstance(raw, dict):
        return None
    aid = (raw.get("id") or raw.get("bcId") or raw.get("bc_id")
           or raw.get("composerId") or "").strip()
    if not aid:
        return None
    # Cookie list sometimes returns bare UUIDs; Cloud Agents API uses bc-*.
    if not aid.startswith("bc-") and re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", aid):
        aid = "bc-" + aid
    repos = raw.get("repos") or []
    repo_url = ""
    if repos and isinstance(repos[0], dict):
        repo_url = (repos[0].get("url") or "").strip()
    repo_url = repo_url or (raw.get("repoUrl") or raw.get("repo_url") or "").strip()
    repo_name = (raw.get("repository") or raw.get("repo") or "").strip()
    if not repo_name and repo_url:
        parts = repo_url.rstrip("/").split("/")
        if len(parts) >= 2:
            repo_name = parts[-2] + "/" + parts[-1]
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
        else:
            repo_name = parts[-1]
    created = raw.get("createdAt") or raw.get("created_at") or ""
    if not created and raw.get("createdAtMs"):
        try:
            created = datetime.datetime.fromtimestamp(
                int(raw["createdAtMs"]) / 1000.0,
                tz=datetime.timezone.utc).isoformat()
        except Exception:
            created = ""
    updated = raw.get("updatedAt") or raw.get("updated_at") or ""
    if not updated and raw.get("updatedAtMs"):
        try:
            updated = datetime.datetime.fromtimestamp(
                int(raw["updatedAtMs"]) / 1000.0,
                tz=datetime.timezone.utc).isoformat()
        except Exception:
            updated = ""
    name = ""
    for key in ("name", "title", "composerName", "displayName", "taskTitle"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            name = val.strip()
            break
    if not name:
        # Some cookie payloads bury the prompt in `nudge` / `taskDescription`.
        for key in ("taskDescription", "prompt", "summary", "nudge"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                name = _title_from_text(val) or val.strip()[:90]
                break
    url = (raw.get("url") or f"https://cursor.com/agents/{aid}").strip()
    return {
        "id": aid,
        "name": name or aid,
        "status": raw.get("status") or raw.get("state") or "",
        "url": url,
        "repo_url": repo_url,
        "repository": repo_name,
        "branch": (raw.get("branchName") or raw.get("branch") or "").strip(),
        "created_at": created,
        "updated_at": updated,
        "model": (raw.get("originalModelName") or raw.get("model") or "auto"),
        "usage": raw.get("usage") or raw.get("totalUsage") or {},
    }


def _list_cloud_agents_api():
    """Page through GET /v1/agents for every agent owned by the API key user."""
    agents, cursor = [], None
    for _ in range(200):  # hard stop ~20k agents
        path = "/v1/agents?limit=100&includeArchived=true"
        if cursor:
            path += "&cursor=" + quote(str(cursor))
        data = _cloud_api("GET", path)
        items = data.get("items") or data.get("agents") or []
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected /v1/agents payload keys: {sorted(data)}")
        for raw in items:
            norm = _normalize_cloud_agent(raw)
            if norm:
                agents.append(norm)
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return agents


def _list_background_composers_cookie(cookie):
    """List cloud/background agents via the IDE session cookie (no API key).

    Cursor's website uses POST /api/background-composer/list with the same
    WorkosCursorSessionToken the dashboard already has for billing. This is the
    fallback when CLOUD_AGENTS_API_KEY / CURSOR_API_KEY is unset — without it,
    every billed UUID orphan stays (untitled).
    """
    if not cookie:
        return []
    last_err = None
    payloads = (
        {"n": 200, "include_status": True},
        {"n": 200, "includeStatus": True},
        {"limit": 200, "include_status": True},
    )
    paths = (
        "/api/background-composer/list",
        "/api/background-composer/get-paginated",
        "/api/agents/list",
    )
    for path in paths:
        for body in payloads:
            try:
                data = _api(cookie, "POST", path, body)
            except Exception as exc:
                last_err = exc
                continue
            if not isinstance(data, dict):
                continue
            items = (
                data.get("composers")
                or data.get("backgroundComposers")
                or data.get("agents")
                or data.get("items")
                or data.get("bcs")
                or []
            )
            if not isinstance(items, list) or not items:
                continue
            agents = []
            for raw in items:
                # Some list endpoints nest the composer under `composer` / `bc`.
                if isinstance(raw, dict) and not (
                        raw.get("id") or raw.get("bcId") or raw.get("composerId")):
                    raw = raw.get("composer") or raw.get("bc") or raw.get("agent") or raw
                norm = _normalize_cloud_agent(raw)
                if norm:
                    agents.append(norm)
            if agents:
                return agents
    if last_err:
        raise RuntimeError(f"background-composer list failed: {last_err}") from last_err
    return []


def _fetch_agent_usage(agent_id):
    try:
        data = _cloud_api("GET", f"/v1/agents/{quote(agent_id)}/usage")
    except Exception as exc:
        print(f"  cloud agent usage {agent_id}: {exc}", flush=True)
        return {}
    return data.get("totalUsage") or {}


def fetch_cloud_agents(force=False, with_usage=False, cookie=None):
    """Return all cloud agents (API key, then IDE cookie, then local cache)."""
    global _CLOUD_AGENTS_CACHE, CLOUD_AGENTS_API_KEY
    if not CLOUD_AGENTS_ENABLED:
        return []
    now = time.time()
    if (not force and _CLOUD_AGENTS_CACHE["agents"] is not None
            and now - _CLOUD_AGENTS_CACHE["at"] < 300):
        return _CLOUD_AGENTS_CACHE["agents"]
    # Refresh module copy so later helpers see the live env value.
    CLOUD_AGENTS_API_KEY = _cloud_api_key()
    agents, err, source = [], "", ""
    key = _cloud_api_key()
    if key:
        try:
            print(f"  cloud agents: fetching with key ({len(key)} chars)…", flush=True)
            agents = _list_cloud_agents_api()
            source = "api"
            if with_usage and agents:
                def _one(a):
                    usage = _fetch_agent_usage(a["id"])
                    if usage:
                        a = dict(a, usage=usage)
                    return a
                with ThreadPoolExecutor(max_workers=8) as pool:
                    agents = list(pool.map(_one, agents))
            _save_cloud_agents_cache_file(agents, source="api")
        except Exception as exc:
            err = str(exc)
            print(f"  cloud agents API unavailable ({exc})", flush=True)
    else:
        err = "no CLOUD_AGENTS_API_KEY / CURSOR_API_KEY in process environment"
        print(f"  cloud agents: {err}", flush=True)
    # Cookie fallback: same session the billing fetch already uses. Without this,
    # Pro/solo users who never set an API key keep every UUID row as (untitled).
    if not agents and cookie:
        try:
            print("  cloud agents: trying IDE session cookie (background-composer/list)…",
                  flush=True)
            agents = _list_background_composers_cookie(cookie)
            if agents:
                source = "cookie"
                err = ""
                _save_cloud_agents_cache_file(agents, source="cookie")
                print(f"  cloud agents: {len(agents)} from cursor.com session cookie",
                      flush=True)
        except Exception as exc:
            cookie_err = str(exc)
            err = (err + "; " if err else "") + f"cookie list failed ({cookie_err})"
            print(f"  cloud agents cookie list unavailable ({exc})", flush=True)
    if not agents:
        cached = _load_cloud_agents_cache_file()
        raw_agents = cached.get("agents") or []
        agents = [a for a in (_normalize_cloud_agent(x) for x in raw_agents) if a]
        if agents:
            source = cached.get("source") or "cache"
            if err:
                err = err + f"; using cached {len(agents)} agent(s)"
            print(f"  cloud agents: {len(agents)} from local cache ({source})",
                  flush=True)
    elif source == "api":
        print(f"  cloud agents: {len(agents)} from api.cursor.com", flush=True)
    _CLOUD_AGENTS_CACHE = {"at": now, "agents": agents, "error": err, "source": source}
    return agents


def _parse_iso_ms(value):
    if not value:
        return 0
    try:
        if isinstance(value, (int, float)):
            n = int(value)
            return n if n > 10_000_000_000 else n * 1000
        s = str(value).strip().replace("Z", "+00:00")
        return int(datetime.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def _session_from_cloud_agent(agent):
    """Build an estimate-only session from Cloud Agents API usage."""
    usage = agent.get("usage") or {}
    inn = int(usage.get("inputTokens") or 0)
    out = int(usage.get("outputTokens") or 0)
    cwrite = int(usage.get("cacheWriteTokens") or 0)
    cread = int(usage.get("cacheReadTokens") or 0)
    total = int(usage.get("totalTokens") or (inn + out + cwrite + cread))
    model = _norm_model(agent.get("model") or "auto")
    cost = _cost(inn, out, cread + cwrite, model)
    started_ms = _parse_iso_ms(agent.get("created_at"))
    updated_ms = _parse_iso_ms(agent.get("updated_at")) or started_ms
    started = ""
    if started_ms:
        started = datetime.datetime.fromtimestamp(
            started_ms / 1000.0, tz=datetime.timezone.utc).isoformat()
    day = _local_day(started_ms or updated_ms)
    turn = {
        "turn_index": 0,
        "started_at": started,
        "model": model,
        "requests": 1 if total else 0,
        "input_tokens": inn,
        "output_tokens": out,
        "cache_read_tokens": cread,
        "cache_write_tokens": cwrite,
        "total_tokens": total,
        "cost_usd": cost,
        "est": True,
        "on_demand": False,
        "kind": "cloud-agent",
        "text": "",
    }
    info = {
        "title": agent.get("name") or agent["id"],
        "subtitle": "Cloud agent",
        "repository": agent.get("repository") or "",
        "branch": agent.get("branch") or (agent.get("status") or ""),
        "workspace_path": "",
        "tracked_repos": [agent["repository"]] if agent.get("repository") else [],
        "created_ms": started_ms,
        "updated_ms": updated_ms,
        "mode": "cloud-agent",
        "model": model,
    }
    sess = _session_from_turns(agent["id"], info, [turn] if total or cost else [], {})
    if sess is None:
        # Still surface the agent even with zero recorded usage.
        days = {day: {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "total_tokens": 0,
            "measured_tokens": 0, "cost_usd": 0.0, "est_usd": 0.0, "on_demand_usd": 0.0,
        }} if day else {}
        sess = _fill_totals({
            "session_id": agent["id"],
            "title": info["title"],
            "repository": info["repository"],
            "workspace": info["repository"],
            "branch": info["branch"],
            "subtitle": info["subtitle"],
            "tracked_repos": info["tracked_repos"],
            "workspace_path": "",
            "days": days,
            "by_model_day": {},
            "text": "",
            "subagent": False,
            "draft": False,
        })
    sess["billed"] = False
    sess["est"] = True
    sess["source"] = "cloud-agent"
    sess["cloud_agent"] = True
    sess["cloud_url"] = agent.get("url") or ""
    sess["billing_note"] = (
        "Cloud agent — token totals from Cloud Agents API; dollar figure is a "
        "list-price estimate until Cursor billed usage events attach."
    )
    return sess, [turn] if total or cost else []


def _cloud_agent_lookup(agents):
    """Index Cloud Agents API records by id and common id variants."""
    by_id = {}
    for agent in agents or []:
        aid = (agent.get("id") or "").strip()
        if not aid:
            continue
        by_id[aid] = agent
        if aid.startswith("bc-") and len(aid) > 3:
            by_id.setdefault(aid[3:], agent)
        elif not aid.startswith("bc-"):
            by_id.setdefault("bc-" + aid, agent)
    return by_id


def _resolve_cloud_agent_for_session(sess, by_id):
    """Match a billed session to a Cloud Agents API record.

    Prefer exact session_id (bc-* invoices), then cloudAgentId attached from
    usage events (conversationId is often a plain UUID while cloudAgentId is bc-*).
    """
    if not by_id or not sess:
        return None
    for key in (
        sess.get("session_id") or "",
        sess.get("cloud_agent_id") or "",
    ):
        key = (key or "").strip()
        if not key:
            continue
        agent = by_id.get(key)
        if agent:
            return agent
        if key.startswith("bc-") and key[3:] in by_id:
            return by_id[key[3:]]
        if not key.startswith("bc-") and ("bc-" + key) in by_id:
            return by_id["bc-" + key]
    return None


def _apply_cloud_agent_to_session(sess, agent, match_how="id"):
    """Copy Cloud Agents API name/repo onto a billed session and clear orphan."""
    if not sess or not agent:
        return False
    cid = sess.get("session_id") or ""
    api_id = agent.get("id") or ""
    changed = False
    if (_is_untitled(sess.get("title"))
            or sess.get("orphan_billed") or sess.get("unattributed")):
        sess["title"] = agent.get("name") or sess.get("title") or cid
        changed = True
    if agent.get("repository") and (
            not sess.get("repository") or sess.get("orphan_billed")):
        sess["repository"] = agent["repository"]
        sess["workspace"] = agent["repository"]
        changed = True
    if agent.get("branch") and not sess.get("branch"):
        sess["branch"] = agent["branch"]
        changed = True
    sess["cloud_agent"] = True
    sess["cloud_agent_id"] = sess.get("cloud_agent_id") or api_id
    sess["cloud_url"] = agent.get("url") or sess.get("cloud_url") or ""
    sess["source"] = sess.get("source") or "cursor-billed"
    if sess.get("orphan_billed") or sess.get("unattributed") or changed:
        extra = ""
        if match_how == "time":
            extra = " Matched by overlapping activity window (no cloudAgentId on invoice)."
        elif cid and api_id and cid != api_id:
            extra = f" Billing conversationId {cid[:20]}… maps via cloudAgentId."
        sess["billing_note"] = (
            f"Cloud agent «{agent.get('name') or api_id or cid}» — costs from Cursor "
            f"billed usage; title/repo from Cloud Agents API.{extra}"
        )
        sess["orphan_billed"] = False
        sess["title_source"] = sess.get("title_source") or (
            "cloud-agents-api-time" if match_how == "time" else "cloud-agents-api")
    if not sess.get("subtitle"):
        sess["subtitle"] = "Cloud agent"
    return True


def _session_activity_ms(sess, priced_all=None):
    """Best-effort [start_ms, end_ms] for a session from days / priced turns."""
    priced_all = priced_all or {}
    start_ms = end_ms = 0
    for turn in priced_all.get(sess.get("session_id") or "") or []:
        raw = turn.get("started_at") or turn.get("timestamp") or 0
        try:
            if isinstance(raw, str) and "T" in raw:
                ms = _parse_iso_ms(raw)
            else:
                ms = int(raw or 0)
                if ms and ms < 10_000_000_000:
                    ms *= 1000
        except (TypeError, ValueError):
            ms = 0
        if not ms:
            continue
        start_ms = ms if not start_ms else min(start_ms, ms)
        end_ms = max(end_ms, ms)
    if start_ms:
        return start_ms, end_ms or start_ms
    days = sorted(d for d in (sess.get("days") or {}) if d)
    if not days:
        return 0, 0
    try:
        start_ms = int(datetime.datetime.strptime(days[0], "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.datetime.strptime(days[-1], "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    except Exception:
        return 0, 0
    return start_ms, end_ms


def _fuzzy_match_orphans_to_cloud_agents(sessions, agents, priced_all=None,
                                         window_ms=6 * 3600 * 1000):
    """When invoice UUIDs omit cloudAgentId, match orphans to agents by time overlap."""
    if not sessions or not agents:
        return 0
    claimed = set()
    for s in sessions:
        ca = (s.get("cloud_agent_id") or "").strip()
        if ca:
            claimed.add(ca)
            if ca.startswith("bc-"):
                claimed.add(ca[3:])
            else:
                claimed.add("bc-" + ca)
        sid = (s.get("session_id") or "").strip()
        if sid.startswith("bc-"):
            claimed.add(sid)
            claimed.add(sid[3:])
    candidates = []
    for agent in agents:
        aid = (agent.get("id") or "").strip()
        if not aid or aid in claimed or (aid.startswith("bc-") and aid[3:] in claimed):
            continue
        created = _parse_iso_ms(agent.get("created_at"))
        updated = _parse_iso_ms(agent.get("updated_at")) or created
        if not created and not updated:
            continue
        candidates.append((agent, created or updated, updated or created))
    if not candidates:
        return 0
    matched = 0
    for sess in sessions:
        if not sess.get("orphan_billed") and not _is_untitled(sess.get("title")):
            continue
        if sess.get("cloud_agent_id"):
            continue
        sid = sess.get("session_id") or ""
        if sid.startswith("bc-") or sid in ("_unattributed",):
            continue
        start_ms, end_ms = _session_activity_ms(sess, priced_all)
        if not start_ms:
            continue
        best, best_score, second = None, -1, -1
        for agent, created, updated in candidates:
            aid = agent["id"]
            if aid in claimed or (aid.startswith("bc-") and aid[3:] in claimed):
                continue
            # Overlap between billing activity and agent lifetime (±window).
            a0, a1 = created - window_ms, updated + window_ms
            if end_ms < a0 or start_ms > a1:
                continue
            overlap = min(end_ms, a1) - max(start_ms, a0)
            # Prefer agents whose midpoint is closest to the billed window midpoint.
            mid_s = (start_ms + end_ms) / 2
            mid_a = (created + updated) / 2
            proximity = max(0, window_ms - abs(mid_s - mid_a))
            score = overlap / 1000.0 + proximity / 1000.0
            if score > best_score:
                second = best_score
                best_score, best = score, agent
            elif score > second:
                second = score
        if not best or best_score < 1:
            continue
        # Ambiguous: two agents nearly as good → skip.
        if second >= 0 and best_score - second < best_score * 0.15:
            continue
        if _apply_cloud_agent_to_session(sess, best, match_how="time"):
            claimed.add(best["id"])
            if best["id"].startswith("bc-"):
                claimed.add(best["id"][3:])
            matched += 1
    return matched


def enrich_sessions_with_cloud_agents(sessions, turns_api, priced_all, agents):
    """Attach cloud-agent titles to billed orphans and append missing agents."""
    if not agents:
        return sessions, turns_api, priced_all, 0
    by_id = _cloud_agent_lookup(agents)
    matched = 0
    for sess in sessions:
        agent = _resolve_cloud_agent_for_session(sess, by_id)
        if not agent:
            continue
        matched += 1
        _apply_cloud_agent_to_session(sess, agent, match_how="id")
    # Invoice often has a plain conversation UUID with no cloudAgentId field —
    # fall back to unique time-overlap against Cloud Agents API records.
    matched += _fuzzy_match_orphans_to_cloud_agents(
        sessions, agents, priced_all=priced_all)
    present = {s.get("session_id") for s in sessions}
    # cloud_agent_id hits count as present so we don't double-add the agent row.
    for s in sessions:
        ca = (s.get("cloud_agent_id") or "").strip()
        if not ca:
            continue
        present.add(ca)
        if ca.startswith("bc-"):
            present.add(ca[3:])
        else:
            present.add("bc-" + ca)
    added = 0
    for agent in agents:
        if agent["id"] in present:
            continue
        sess, turns = _session_from_cloud_agent(agent)
        sessions.append(sess)
        if turns:
            turns_api[agent["id"]] = [{k: t[k] for k in (
                "turn_index", "started_at", "model", "requests", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "total_tokens", "cost_usd", "est", "kind")} for t in turns]
            priced_all[agent["id"]] = turns
        added += 1
    if matched or added:
        print(f"  cloud agents: matched {matched} billed chat(s), "
              f"added {added} not yet on invoice", flush=True)
    sessions.sort(key=lambda s: (0 if s.get("billed") else 1, -(s.get("cost_usd") or 0)))
    return sessions, turns_api, priced_all, matched + added


def _is_cloud_session(s):
    """True for Cloud Agent sessions (bc-* IDs or Cloud Agents API)."""
    if s.get("cloud_agent"):
        return True
    sid = s.get("session_id") or ""
    ca = s.get("cloud_agent_id") or ""
    return (
        sid.startswith("bc-")
        or ca.startswith("bc-")
        or s.get("source") == "cloud-agent"
    )


def _session_origin(s):
    return "cloud" if _is_cloud_session(s) else "local-ide"


def _turns_from_events(evs):
    priced = []
    for ev in evs:
        kind = ev.get("kind") or ""
        tu = ev.get("tokenUsage") or {}
        inn = int(tu.get("inputTokens") or 0)
        out = int(tu.get("outputTokens") or 0)
        cwrite = int(tu.get("cacheWriteTokens") or 0)
        cread = int(tu.get("cacheReadTokens") or 0)
        ms = int(ev.get("timestamp") or 0)
        started = datetime.datetime.fromtimestamp(
            ms / 1000.0, tz=datetime.timezone.utc).isoformat() if ms else ""
        priced.append({
            "turn_index": len(priced),
            "started_at": started,
            "model": _norm_model(ev.get("model") or "unknown"),
            "requests": 1,
            "input_tokens": inn,
            "output_tokens": out,
            "cache_read_tokens": cread,
            "cache_write_tokens": cwrite,
            "total_tokens": inn + out + cwrite + cread,
            "cost_usd": _event_cents(ev) / 100.0,
            "est": False,
            "on_demand": "USAGE_BASED" in kind.upper(),
            "kind": _event_kind_label(kind),
            "text": "",
        })
    return priced



UNTITLED_TITLE = "(untitled)"


def _is_untitled(title):
    t = (title or "").strip()
    return (not t) or t == UNTITLED_TITLE


def _title_from_text(text):
    """First non-empty line of chat text, trimmed for a session title."""
    for line in (text or "").splitlines():
        typed = line.strip()
        if typed:
            return " ".join(typed.split())[:90]
    return ""


def _composer_display_name(blob):
    """Best-effort title fields Cursor has used on composerHeaders / composerData."""
    if not isinstance(blob, dict):
        return ""
    for key in ("name", "customTitle", "title", "displayName", "text"):
        val = blob.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _local_context_present(local):
    """True when a local stub/session actually carries title, text, or repo context."""
    if not local:
        return False
    if not _is_untitled(local.get("title")):
        return True
    if (local.get("text") or "").strip():
        return True
    if (local.get("repository") or "").strip():
        return True
    if (local.get("subtitle") or "").strip():
        return True
    return False


def _fill_missing_title(sess, *text_sources):
    """If sess title is blank/(untitled), derive one from chat text sources."""
    if not _is_untitled(sess.get("title")):
        return False
    for src in text_sources:
        if isinstance(src, list):
            for turn in src:
                title = _title_from_text((turn or {}).get("text") or "")
                if title:
                    sess["title"] = title
                    return True
        else:
            title = _title_from_text(src or "")
            if title:
                sess["title"] = title
                return True
    return False


def _composer_data_richer(new_val, old_val):
    """Prefer composerData blobs that carry a real name or are clearly newer/richer."""
    if old_val in (None, b"", ""):
        return new_val not in (None, b"", "")
    if new_val in (None, b"", ""):
        return False
    nb, ob = _loads(new_val), _loads(old_val)
    if not isinstance(nb, dict):
        return False
    if not isinstance(ob, dict):
        return True
    n_name = _composer_display_name(nb)
    o_name = _composer_display_name(ob)
    if n_name and not o_name:
        return True
    n_upd = nb.get("lastUpdatedAt") or nb.get("updatedAt") or 0
    o_upd = ob.get("lastUpdatedAt") or ob.get("updatedAt") or 0
    try:
        n_upd = int(n_upd or 0)
        o_upd = int(o_upd or 0)
    except (TypeError, ValueError):
        n_upd = o_upd = 0
    if n_name and n_upd > o_upd:
        return True
    if n_name and o_name and n_upd > o_upd:
        return True
    # Same emptiness: keep larger payload (more bubbles / metadata).
    if n_name == o_name and len(str(new_val)) > int(len(str(old_val)) * 1.25) + 64:
        return True
    return False


def _billing_match_notes(cid, local, evs):
    """Explain billed usage that can't be matched to a local chat title."""
    cloud_agent_id = _first_event_attr(evs, _billing_event_cloud_agent_id)
    automation_id = _first_event_attr(evs, _billing_event_automation_id)
    headless = sum(1 for e in evs if e.get("isHeadless"))
    if cid == "_unattributed":
        n = len(evs)
        models = collections.Counter(e.get("model") or "?" for e in evs)
        top = ", ".join(f"{m} ({c})" for m, c in models.most_common(3))
        note = (
            f"{n} billed API events with no conversation ID — Cursor did not link these "
            f"calls to a specific chat, so they cannot be matched to titles in your local store. "
            f"Common causes: cloud/background agents, API or headless usage, chats on another "
            f"machine, or older invoices before chat IDs were attached."
        )
        if headless:
            note += f" {headless} of {n} were marked headless by Cursor."
        if top:
            note += f" Models: {top}."
        return {"unattributed": True, "billing_note": note}
    if not _local_context_present(local):
        short = (cid[:20] + "…") if len(cid) > 22 else cid
        notes = {
            "orphan_billed": True,
            "cloud_agent": bool(
                (cid or "").startswith("bc-")
                or (cloud_agent_id or "").startswith("bc-")
            ),
        }
        if cloud_agent_id:
            notes["cloud_agent_id"] = cloud_agent_id
        if automation_id:
            notes["automation_id"] = automation_id
        if (cid or "").startswith("bc-") or (cloud_agent_id or "").startswith("bc-"):
            ca_short = cloud_agent_id or cid
            ca_disp = (ca_short[:20] + "…") if len(ca_short) > 22 else ca_short
            note = (
                f"Billed cloud agent {ca_disp} — title not in the local IDE store yet. "
                f"Enable Cloud Agents API (CLOUD_AGENTS_API_KEY / CURSOR_API_KEY) to name it."
            )
            if cloud_agent_id and cid and cloud_agent_id != cid and not cid.startswith("bc-"):
                note += (
                    f" Invoice conversationId is {short} (not bc-*); "
                    f"cloudAgentId is the Cloud Agents API key."
                )
        elif automation_id:
            note = (
                f"Billed to conversation {short} (Cursor automation "
                f"{automation_id[:12]}…) — not present in this machine's chat store."
            )
        elif headless:
            note = (
                f"Billed to conversation {short} (headless / background usage) — "
                f"no matching chat is in this machine's Cursor store."
            )
        else:
            note = (
                f"Billed to conversation {short} but no matching chat is in this machine's Cursor "
                f"store — likely another device, cleared local history, or a remote/background "
                f"session (not a bc-* cloud agent id)."
            )
        notes["billing_note"] = note
        return notes
    return {}


def _first_event_attr(evs, getter):
    for ev in evs or []:
        val = getter(ev)
        if val:
            return val
    return ""


def _sessions_from_billing(events, local_by_id, meta, ws_names):
    """One session per billed conversationId; titles/repos from the local store."""
    groups = collections.defaultdict(list)
    for ev in events:
        cid = _billing_event_cid(ev)
        groups[cid].append(ev)
    sessions, turns_api, priced_all = [], {}, {}
    for cid, evs in groups.items():
        evs.sort(key=lambda e: int(e.get("timestamp") or 0))
        priced = _turns_from_events(evs)
        if not priced:
            continue
        local = local_by_id.get(cid) or {}
        info = dict(meta.get(cid) or {})
        if not _is_untitled(local.get("title")):
            info["title"] = local["title"]
        elif cid == "_unattributed":
            info["title"] = "Other billed usage"
        if local.get("repository"):
            info["repository"] = local["repository"]
        if local.get("branch"):
            info["branch"] = local["branch"]
        if local.get("subtitle"):
            info["subtitle"] = local["subtitle"]
        sess = _session_from_turns(cid, info, priced, ws_names)
        if sess is None:
            continue
        sess["billed"] = True
        sess["est"] = False
        sess["source"] = "cursor-billed"
        sess["account_label"] = (evs[0].get("_account_email") or "")
        cloud_agent_id = _first_event_attr(evs, _billing_event_cloud_agent_id)
        automation_id = _first_event_attr(evs, _billing_event_automation_id)
        if cloud_agent_id:
            sess["cloud_agent_id"] = cloud_agent_id
        if automation_id:
            sess["automation_id"] = automation_id
        if any(e.get("isHeadless") for e in evs):
            sess["is_headless"] = True
        sess.update(_billing_match_notes(cid, local, evs))
        if local.get("text"):
            sess["text"] = local["text"]
            _fill_missing_title(sess, local["text"])
        sessions.append(sess)
        priced_all[cid] = priced
        turns_api[cid] = [{k: t[k] for k in (
            "turn_index", "started_at", "model", "requests", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "total_tokens", "cost_usd", "est", "kind", "on_demand")} for t in priced]
    sessions.sort(key=lambda s: s["cost_usd"], reverse=True)
    return sessions, turns_api, priced_all



def _billing_str_field(ev, *keys):
    if not isinstance(ev, dict):
        return ""
    for key in keys:
        val = ev.get(key)
        if isinstance(val, str) and val.strip() and val.strip() not in ("-", "null", "None"):
            return val.strip()
        if isinstance(val, (int, float)) and val:
            return str(val)
    return ""


def _billing_event_cloud_agent_id(ev):
    """Cloud-agent run id from a usage event (often bc-*, distinct from conversationId)."""
    aid = _billing_str_field(
        ev,
        "cloudAgentId", "cloud_agent_id", "bcId", "bc_id",
        "backgroundAgentId", "background_agent_id",
        "backgroundComposerId", "background_composer_id",
    )
    if aid:
        return aid
    # agentId is ambiguous (local vs cloud); only accept bc-* values.
    for key in ("agentId", "agent_id", "composerId", "composer_id"):
        val = _billing_str_field(ev, key)
        if val.startswith("bc-"):
            return val
    # Nested shapes seen in some Cursor payloads.
    for nest in ("metadata", "conversation", "agent", "cloudAgent", "backgroundAgent"):
        obj = ev.get(nest) if isinstance(ev, dict) else None
        if isinstance(obj, dict):
            aid = _billing_event_cloud_agent_id(obj)
            if aid:
                return aid
    return ""


def _billing_event_automation_id(ev):
    aid = _billing_str_field(ev, "automationId", "automation_id")
    if aid:
        return aid
    for nest in ("metadata", "automation"):
        obj = ev.get(nest) if isinstance(ev, dict) else None
        if isinstance(obj, dict):
            aid = _billing_event_automation_id(obj)
            if aid:
                return aid
    return ""


def _billing_event_cid(ev):
    """Best-effort conversation/composer id from a usage event.

    Prefer conversation/composer ids. Do NOT fall back to requestId — that is a
    per-request id and invents orphan 'conversations' that never exist locally.
    When conversationId is absent, cloudAgentId (bc-*) is a usable session key.
    """
    if not isinstance(ev, dict):
        return "_unattributed"
    for key in (
        "conversationId", "composerId", "chatId", "composer_id",
        "conversation_id",
    ):
        val = ev.get(key)
        if isinstance(val, str) and val.strip() and val.strip() not in ("-", "null", "None"):
            return val.strip()
        if isinstance(val, (int, float)) and val:
            return str(val)
    # Nested shapes seen in some Cursor payloads.
    for nest in ("conversation", "composer", "chat", "metadata"):
        obj = ev.get(nest)
        if isinstance(obj, dict):
            # Avoid recursing into cloudAgentId via a nested agentId-only object
            # by only considering the conversation/composer keys above.
            for key in (
                "conversationId", "composerId", "chatId", "composer_id",
                "conversation_id", "id",
            ):
                val = obj.get(key)
                if isinstance(val, str) and val.strip() and val.strip() not in (
                        "-", "null", "None"):
                    # Skip generic nested ids that are clearly cloud-agent only;
                    # those are handled below.
                    if nest in ("conversation", "composer", "chat") or key != "id":
                        return val.strip()
    ca = _billing_event_cloud_agent_id(ev)
    if ca:
        return ca
    return "_unattributed"


def _first_user_bubble_title(con, cid, cap=40):
    """Derive a title from the earliest user bubble text for composer cid."""
    if not cid or cid in ("_unattributed", "empty-state-draft"):
        return ""
    try:
        rows = _sample_cid_bubble_raws(con, cid, cap=cap)
    except Exception:
        return ""
    # Prefer type==1 (user) bubbles; fall back to any text.
    user_text = ""
    any_text = ""
    for key, raw in rows:
        row = _bubble_dict_from_raw(key, raw)
        if not row:
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        if not any_text:
            any_text = text
        if row.get("type") == 1 and not user_text:
            user_text = text
            break
    return _title_from_text(user_text or any_text)


def _agent_transcript_roots():
    roots = []
    home = os.path.expanduser("~")
    for base in (
        os.path.join(home, ".cursor", "projects"),
        os.path.join(home, "AppData", "Roaming", "Cursor", "User"),
    ):
        if os.path.isdir(base):
            roots.append(base)
    # Workspace-local .cursor folders under home (shallow).
    return roots


def _title_from_agent_transcripts(cid):
    """Use ~/.cursor/projects/*/agent-transcripts/{cid}.* first line as title."""
    if not cid or cid.startswith("_"):
        return ""
    needle = cid
    for root in _agent_transcript_roots():
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if "agent-transcripts" not in dirpath.replace("\\", "/"):
                    # Fast reject: only descend into agent-transcripts trees.
                    if "agent-transcripts" not in dirnames and not dirpath.endswith("projects"):
                        # still allow walking projects to find agent-transcripts
                        pass
                base = os.path.basename(dirpath)
                if base != "agent-transcripts" and needle not in base:
                    # Keep walking; don't open every file.
                    continue
                for fn in filenames:
                    if needle not in fn and needle not in base:
                        continue
                    path = os.path.join(dirpath, fn)
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                # jsonl: {"role":"user","text":"..."} or plain text
                                if line.startswith("{"):
                                    try:
                                        obj = json.loads(line)
                                    except Exception:
                                        title = _title_from_text(line)
                                        return title
                                    for key in ("text", "content", "title", "message"):
                                        val = obj.get(key)
                                        if isinstance(val, str) and val.strip():
                                            return _title_from_text(val)
                                        if isinstance(val, list):
                                            for part in val:
                                                if isinstance(part, dict) and isinstance(part.get("text"), str):
                                                    t = _title_from_text(part["text"])
                                                    if t:
                                                        return t
                                else:
                                    return _title_from_text(line)
                    except OSError:
                        continue
                # Don't walk forever.
                if dirpath.count(os.sep) - root.count(os.sep) > 6:
                    dirnames[:] = []
        except OSError:
            continue
    return ""


def _fill_titles_from_local_content(con, sessions, meta=None):
    """Last-resort titles for billed/(untitled) rows using bubbles + transcripts."""
    meta = meta or {}
    filled = 0
    for sess in sessions or []:
        if not _is_untitled(sess.get("title")):
            continue
        cid = sess.get("session_id") or ""
        title = ""
        # Prefer meta again (ItemTable may have landed after stub creation).
        if meta.get(cid) and not _is_untitled(meta[cid].get("title")):
            title = meta[cid]["title"]
            src = meta[cid].get("title_source") or "meta"
        if not title:
            title = _first_user_bubble_title(con, cid)
            src = "bubble"
        if not title:
            title = _title_from_agent_transcripts(cid)
            src = "transcript"
        if not title:
            # conversationMap fallback
            title = _title_from_text(_composer_data_fallback_text(con, cid))
            src = "composerData"
        if title:
            sess["title"] = title
            sess["title_source"] = src
            filled += 1
        else:
            sess["title_status"] = "no-local-name-or-text"
    return filled


def _session_time_window_ms(sess, priced=None):
    """(start_ms, end_ms) for a session from turns / metadata."""
    times = []
    for turn in priced or []:
        ms = _iso_to_ms(turn.get("started_at"))
        if ms:
            times.append(ms)
    for key in ("created_ms", "updated_ms", "first_ms", "last_ms"):
        ms = _iso_to_ms(sess.get(key) if sess else None)
        if ms:
            times.append(ms)
    for key in ("first_day", "last_day"):
        day = (sess or {}).get(key)
        if day and isinstance(day, str) and len(day) >= 10:
            ms = _iso_to_ms(day[:10] + "T12:00:00+00:00")
            if ms:
                times.append(ms)
    if not times:
        return 0, 0
    return min(times), max(times)


def _find_composer_embedding_cid(con, orphan_cid):
    """Reverse-map: composerData/bubble blob that embeds the billing UUID."""
    if not con or not orphan_cid or orphan_cid.startswith("_") or len(orphan_cid) < 12:
        return ""
    needle = f"%{orphan_cid}%"
    try:
        rows = con.execute(
            "SELECT key FROM cursorDiskKV WHERE "
            "(key LIKE 'composerData:%' OR key LIKE 'bubbleId:%') "
            "AND CAST(value AS TEXT) LIKE ? LIMIT 8",
            (needle,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    for row in rows:
        key = row[0] if not isinstance(row, sqlite3.Row) else row["key"]
        parts = str(key).split(":")
        if len(parts) < 2:
            continue
        cid = parts[1]
        if cid and cid != orphan_cid:
            return cid
    return ""


def _apply_local_context_to_orphan(sess, local, source, note_prefix=""):
    """Copy title/repo/text from a local composer onto an orphan billed row."""
    if not sess or not local:
        return False
    changed = False
    if not _is_untitled(local.get("title")) and _is_untitled(sess.get("title")):
        sess["title"] = local["title"]
        sess["title_source"] = source
        changed = True
    if local.get("text") and not (sess.get("text") or "").strip():
        sess["text"] = local["text"]
        if _fill_missing_title(sess, local["text"]):
            sess["title_source"] = sess.get("title_source") or source
            changed = True
    for field in ("repository", "workspace", "branch", "subtitle",
                  "workspace_path", "tracked_repos", "tracked_repo_paths"):
        if local.get(field) and not sess.get(field):
            sess[field] = local[field]
            changed = True
    if changed or _local_context_present(local):
        short = (sess.get("session_id") or "")[:20]
        local_id = local.get("session_id") or ""
        sess["orphan_billed"] = False
        sess["matched_local_id"] = local_id or sess.get("matched_local_id") or ""
        sess["billing_note"] = (
            (note_prefix or "Matched to local chat")
            + (f" {local_id[:12]}…" if local_id and local_id != sess.get("session_id") else "")
            + f" (billing id {short}…)."
        )
        sess.pop("title_status", None)
        return True
    return False


def _fuzzy_score_orphan(sess, priced, local, local_priced=None, window_ms=2 * 3600 * 1000):
    """Score a local candidate against an orphan billed session (higher = better)."""
    if not sess or not local:
        return -1
    o0, o1 = _session_time_window_ms(sess, priced)
    l0, l1 = _session_time_window_ms(local, local_priced)
    if not o0 or not l0:
        return -1
    # Require overlapping or near windows.
    if o1 + window_ms < l0 or l1 + window_ms < o0:
        return -1
    overlap = min(o1, l1) - max(o0, l0)
    gap = 0 if overlap >= 0 else min(abs(o0 - l1), abs(l0 - o1))
    score = 0.0
    if overlap >= 0:
        score += 40 + min(40, overlap / max(1, window_ms) * 40)
    else:
        score += max(0, 30 - (gap / window_ms) * 30)
    o_models = {_norm_model(t.get("model") or "") for t in (priced or []) if t.get("model")}
    l_models = {_norm_model(t.get("model") or "") for t in (local_priced or []) if t.get("model")}
    if local.get("top_model"):
        l_models.add(_norm_model(local["top_model"]))
    if o_models and l_models and (o_models & l_models):
        score += 25
    elif o_models and l_models:
        score -= 10
    o_cost = float(sess.get("cost_usd") or 0)
    l_cost = float(local.get("cost_usd") or 0)
    if o_cost > 0 and l_cost > 0:
        ratio = min(o_cost, l_cost) / max(o_cost, l_cost)
        if ratio >= 0.5:
            score += 20 * ratio
    if not _is_untitled(local.get("title")):
        score += 5
    return score


def resolve_orphan_sessions(sessions, local_by_id, priced_all=None, local_priced=None,
                            con=None, meta=None):
    """Try harder to attach local context to orphan billed UUIDs.

    1) Reverse-scan composerData/bubbles for embedded billing UUIDs
    2) Fuzzy-match remaining orphans to nearby unbilled local chats (time+model+cost)
    """
    priced_all = priced_all or {}
    local_priced = local_priced or {}
    meta = meta or {}
    billed_ids = {s.get("session_id") for s in sessions if s.get("billed")}
    resolved = 0

    # Exact reverse embedding map.
    if con is not None:
        for sess in sessions:
            if not sess.get("orphan_billed"):
                continue
            cid = sess.get("session_id") or ""
            if not cid or cid.startswith("bc-") or sess.get("cloud_agent_id"):
                # Leave bc-* / cloudAgentId orphans for Cloud Agents API enrichment.
                continue
            embedded = _find_composer_embedding_cid(con, cid)
            if not embedded:
                continue
            local = local_by_id.get(embedded) or {}
            if not _local_context_present(local) and meta.get(embedded):
                # Build a minimal local stub from meta.
                m = meta[embedded]
                local = {
                    "session_id": embedded,
                    "title": m.get("title") or "",
                    "repository": m.get("repository") or "",
                    "branch": m.get("branch") or "",
                    "subtitle": m.get("subtitle") or "",
                    "workspace_path": m.get("workspace_path") or "",
                    "tracked_repos": m.get("tracked_repos") or [],
                    "text": "",
                }
                if not local["text"]:
                    try:
                        local["text"] = _composer_data_fallback_text(con, embedded) or ""
                    except Exception:
                        pass
            local = dict(local)
            local["session_id"] = embedded
            if _apply_local_context_to_orphan(
                    sess, local, "embedded-id",
                    note_prefix="Billing UUID embedded in local composer"):
                resolved += 1

    # Fuzzy match leftovers that still look local-ish (no bc- / cloudAgentId).
    claimed_locals = set(billed_ids)
    for sess in sessions:
        mid = sess.get("matched_local_id")
        if mid:
            claimed_locals.add(mid)
    candidates = []
    for cid, local in (local_by_id or {}).items():
        if not cid or cid in claimed_locals or cid.startswith("bc-"):
            continue
        if cid in ("_unattributed", "empty-state-draft"):
            continue
        if not _local_context_present(local):
            continue
        # Prefer locals that were NOT already billed under their own id.
        if local.get("billed") is True:
            continue
        candidates.append((cid, local))

    for sess in sessions:
        if not sess.get("orphan_billed"):
            continue
        cid = sess.get("session_id") or ""
        if cid.startswith("bc-") or sess.get("cloud_agent_id"):
            continue
        priced = priced_all.get(cid) or []
        best_score, best = -1, None
        for local_cid, local in candidates:
            if local_cid in claimed_locals:
                continue
            score = _fuzzy_score_orphan(
                sess, priced, local, local_priced.get(local_cid),
                window_ms=2 * 3600 * 1000)
            if score > best_score:
                best_score, best = score, (local_cid, local)
        # Require a reasonably confident unique-ish match.
        if not best or best_score < 55:
            continue
        # Ambiguity guard: second-best within 8 points → skip.
        second = -1
        for local_cid, local in candidates:
            if local_cid == best[0] or local_cid in claimed_locals:
                continue
            score = _fuzzy_score_orphan(
                sess, priced, local, local_priced.get(local_cid),
                window_ms=2 * 3600 * 1000)
            if score > second:
                second = score
        if second >= 0 and best_score - second < 8:
            continue
        local_cid, local = best
        local = dict(local)
        local["session_id"] = local_cid
        if _apply_local_context_to_orphan(
                sess, local, "fuzzy-local",
                note_prefix="Fuzzy-matched to nearby local chat"):
            claimed_locals.add(local_cid)
            resolved += 1
    if resolved:
        print(f"  resolved {resolved} orphan billed chat(s) via local embed/fuzzy match",
              flush=True)
    return resolved


def _header_meta(con):
    """composerId -> metadata from the composerHeaders table and composerData rows."""
    meta = {}
    for row in _read_composer_header_rows(con):
        blob = _loads(row.get("value")) or {}
        cid = row.get("composerId")
        if not cid:
            continue
        ws = blob.get("workspaceIdentifier") or {}
        uri = (ws.get("uri") or ws.get("configPath") or {})
        if not isinstance(uri, dict):
            uri = {}
        repos = blob.get("trackedGitRepos") or []
        tracked_paths = [r.get("repoPath") for r in repos if isinstance(r, dict) and r.get("repoPath")]
        tracked_folders = [_repo_from_path(p) for p in tracked_paths]
        repo = ""
        branch = ""
        if tracked_folders:
            ws_repo = _repo_from_path(uri.get("fsPath") or uri.get("path") or "")
            if ws_repo and not ws_repo.endswith(".code-workspace"):
                repo = ws_repo
            else:
                repo = tracked_folders[0]
            branches = []
            if repos and isinstance(repos[0], dict):
                branches = repos[0].get("branches") or []
            if branches and isinstance(branches[0], dict):
                branch = branches[0].get("branchName") or ""
        elif repos and isinstance(repos[0], dict):
            repo = _repo_from_path(repos[0].get("repoPath"))
            branches = repos[0].get("branches") or []
            if branches and isinstance(branches[0], dict):
                branch = branches[0].get("branchName") or ""
        meta[cid] = {
            "title": _composer_display_name(blob),
            "subtitle": blob.get("subtitle") or "",
            "workspace_id": row.get("workspaceId") or (ws.get("id") or ""),
            "workspace_path": uri.get("fsPath") or uri.get("path") or "",
            "repository": repo,
            "branch": branch,
            "tracked_repos": tracked_folders,
            "tracked_repo_paths": tracked_paths,
            "created_ms": row.get("createdAt") or blob.get("createdAt") or 0,
            "updated_ms": row.get("lastUpdatedAt") or blob.get("lastUpdatedAt") or 0,
            "mode": blob.get("unifiedMode") or "",
            "subagent": bool(row.get("isSubagent") or blob.get("isBestOfNSubcomposer")),
            "draft": bool(blob.get("isDraft")),
            "archived": bool(row.get("isArchived") or blob.get("isArchived")),
            "title_source": "sql:composerHeaders" if _composer_display_name(blob) else "",
        }
    try:
        composer_rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
    except sqlite3.OperationalError:
        composer_rows = []
    for key, raw in composer_rows:
        cid = key.split(":", 1)[-1]
        blob = _loads(raw) or {}
        mc = blob.get("modelConfig") or {}
        entry = meta.setdefault(cid, {
            "title": "", "subtitle": "", "workspace_id": "", "workspace_path": "",
            "repository": "", "branch": "", "tracked_repos": [], "tracked_repo_paths": [],
            "created_ms": 0, "updated_ms": 0,
            "mode": "", "subagent": False, "draft": False, "archived": False,
        })
        entry["title"] = entry["title"] or _composer_display_name(blob)
        entry["created_ms"] = entry["created_ms"] or blob.get("createdAt") or 0
        entry["updated_ms"] = entry["updated_ms"] or blob.get("lastUpdatedAt") or 0
        entry["mode"] = entry["mode"] or blob.get("unifiedMode") or ""
        entry["model"] = _norm_model(mc.get("modelName") or "auto")
        entry["max_mode"] = bool(mc.get("maxMode"))
        if blob.get("isAgentic") and not entry["mode"]:
            entry["mode"] = "agent"
    _apply_itemtable_composer_index(con, meta)
    return meta


def _empty_meta_entry():
    return {
        "title": "", "subtitle": "", "workspace_id": "", "workspace_path": "",
        "repository": "", "branch": "", "tracked_repos": [], "tracked_repo_paths": [],
        "created_ms": 0, "updated_ms": 0,
        "mode": "", "subagent": False, "draft": False, "archived": False,
    }


def _upsert_composer_index_entry(meta, entry, source="itemtable"):
    """Merge one allComposers / composerHeaders entry into meta."""
    if not isinstance(entry, dict):
        return False
    cid = (entry.get("composerId") or entry.get("composer_id") or
           entry.get("id") or "").strip()
    if not cid:
        return False
    title = _composer_display_name(entry)
    ws = entry.get("workspaceIdentifier") or entry.get("workspace") or {}
    if not isinstance(ws, dict):
        ws = {}
    uri = ws.get("uri") if isinstance(ws.get("uri"), dict) else {}
    ws_path = ""
    if isinstance(uri, dict):
        ws_path = uri.get("fsPath") or uri.get("path") or ""
    if not ws_path:
        ws_path = entry.get("workspacePath") or ""
    repo = _repo_from_path(ws_path) if ws_path else ""
    created = entry.get("createdAt") or entry.get("created_ms") or 0
    updated = (entry.get("lastUpdatedAt") or entry.get("updatedAt")
               or entry.get("updated_ms") or created or 0)
    mode = entry.get("unifiedMode") or entry.get("forceMode") or entry.get("mode") or ""
    row = meta.setdefault(cid, _empty_meta_entry())
    changed = False
    if title and (_is_untitled(row.get("title")) or (
            (updated or 0) >= (row.get("updated_ms") or 0) and title != row.get("title"))):
        row["title"] = title
        changed = True
    if ws.get("id") and not row.get("workspace_id"):
        row["workspace_id"] = ws.get("id") or ""
        changed = True
    if ws_path and not row.get("workspace_path"):
        row["workspace_path"] = ws_path
        changed = True
    if repo and not row.get("repository"):
        row["repository"] = repo
        changed = True
    if created and not row.get("created_ms"):
        row["created_ms"] = created
        changed = True
    if updated and (updated or 0) >= (row.get("updated_ms") or 0):
        row["updated_ms"] = updated
        changed = True
    if mode and not row.get("mode"):
        row["mode"] = mode
        changed = True
    row["title_source"] = row.get("title_source") or (source if title else "")
    return changed


def _apply_itemtable_composer_index(con, meta):
    """Cursor 3.0+ keeps chat names in ItemTable composer.composerHeaders.allComposers."""
    keys = (
        "composer.composerHeaders",
        "composer.composerData",
        "composerData",
    )
    applied = 0
    for key in keys:
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            continue
        if not row:
            continue
        blob = _loads(row[0] if not isinstance(row, sqlite3.Row) else row["value"]) or {}
        if not isinstance(blob, dict):
            continue
        composers = blob.get("allComposers") or blob.get("composers") or []
        if not isinstance(composers, list):
            continue
        for entry in composers:
            if _upsert_composer_index_entry(meta, entry, source=f"itemtable:{key}"):
                applied += 1
    return applied


def _workspace_composer_titles(meta):
    """Supplement titles from workspaceStorage/*/state.vscdb (pre-3.0 + unmigrated)."""
    root = os.path.join(_cursor_user_dir(), "workspaceStorage")
    if not os.path.isdir(root):
        return 0
    applied = 0
    for wid in os.listdir(root):
        db = os.path.join(root, wid, "state.vscdb")
        if not os.path.isfile(db):
            continue
        try:
            with connect(db) as con:
                for key in ("composer.composerData", "composer.composerHeaders"):
                    try:
                        row = con.execute(
                            "SELECT value FROM ItemTable WHERE key = ?", (key,)
                        ).fetchone()
                    except sqlite3.OperationalError:
                        continue
                    if not row:
                        continue
                    blob = _loads(row[0] if not isinstance(row, sqlite3.Row) else row["value"]) or {}
                    if not isinstance(blob, dict):
                        continue
                    composers = blob.get("allComposers") or blob.get("composers") or []
                    if not isinstance(composers, list):
                        continue
                    for entry in composers:
                        # Ensure workspace id is present for path mapping.
                        if isinstance(entry, dict) and not entry.get("workspaceIdentifier"):
                            entry = dict(entry)
                            entry["workspaceIdentifier"] = {"id": wid}
                        if _upsert_composer_index_entry(
                                meta, entry, source=f"workspace:{wid}"):
                            applied += 1
        except sqlite3.Error:
            continue
    return applied


def _conversation_map_text(blob):
    """Legacy composerData.conversationMap text when bubbleId rows are missing."""
    if not isinstance(blob, dict):
        return ""
    cmap = blob.get("conversationMap") or blob.get("conversation") or {}
    if not isinstance(cmap, dict):
        return ""
    # Preserve insertion order when available; otherwise sort by key.
    items = list(cmap.items())
    try:
        items.sort(key=lambda kv: (kv[1] or {}).get("createdAt") or kv[0])
    except Exception:
        pass
    texts = []
    for _k, msg in items:
        if not isinstance(msg, dict):
            continue
        t = _bubble_text(msg)
        for link in _pr_links_from_raw(msg):
            if link not in (t or ""):
                t = f"{t}\n{link}".strip() if t else link
        if t:
            texts.append(t)
    if not texts:
        return ""
    # Prefer PR-bearing messages so older PR links survive the legacy map cap.
    cap = 40
    if len(texts) <= cap:
        return "\n".join(texts)
    pr_idx = [i for i, t in enumerate(texts) if _text_mentions_pr(t)]
    keep = _sample_indices_prefer_pr(len(texts), cap, pr_idx, head_budget=8)
    return "\n".join(texts[i] for i in keep)


def _composer_data_fallback_text(con, cid):
    try:
        row = con.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{cid}",)).fetchone()
    except sqlite3.OperationalError:
        return ""
    if not row:
        return ""
    blob = _loads(row[0] if not isinstance(row, sqlite3.Row) else row["value"]) or {}
    return _conversation_map_text(blob)


def _turns_from_bubbles(bubbles, default_model):
    """Group bubbles into user-turns and cost each one."""
    bubbles.sort(key=lambda b: (b["created"] or "", b["type"] != 1, b["id"]))
    turns, current = [], None

    def close():
        nonlocal current
        if current is None:
            return
        if current["input_tokens"] or current["output_tokens"] or current["text"].strip() \
                or current["user_text"].strip():
            turns.append(current)
        current = None

    for bub in bubbles:
        if bub["type"] == 1:
            close()
            current = {
                "turn_index": len(turns),
                "started_at": bub["created"],
                "model": bub["model"] or default_model,
                "user_text": bub["text"],
                "text": bub["text"],
                "input_tokens": 0.0,
                "output_tokens": 0.0,
                "measured_in": None,
                "measured_out": None,
                "requests": 1,
            }
            continue
        if current is None:
            current = {
                "turn_index": len(turns),
                "started_at": bub["created"],
                "model": bub["model"] or default_model,
                "user_text": "",
                "text": "",
                "input_tokens": 0.0,
                "output_tokens": 0.0,
                "measured_in": None,
                "measured_out": None,
                "requests": 1,
            }
        if bub["model"]:
            current["model"] = bub["model"]
        if bub["text"]:
            current["text"] += ("\n" if current["text"] else "") + bub["text"]
        if bub["inn"] or bub["out"]:
            current["measured_in"] = (current["measured_in"] or 0) + bub["inn"]
            current["measured_out"] = (current["measured_out"] or 0) + bub["out"]
    close()

    ctx = 0.0
    prev_prompt = 0.0
    priced = []
    for turn in turns:
        model = turn["model"] or default_model
        if turn["measured_in"] is not None and turn["measured_out"] is not None:
            prompt = float(turn["measured_in"])
            t_out = float(turn["measured_out"])
            t_cache = min(prev_prompt, prompt)
            t_in = max(prompt - t_cache, 0.0)
            prev_prompt = prompt
            ctx = prompt + t_out
            est = False
        else:
            t_in = len(turn["user_text"]) / CHARS_PER_TOKEN
            t_out = len(turn["text"][len(turn["user_text"]):].lstrip()
                        if turn["text"].startswith(turn["user_text"])
                        else turn["text"]) / CHARS_PER_TOKEN
            if not turn["user_text"] and not t_out:
                continue
            t_cache = min(ctx, CONTEXT_WINDOW_TOKENS)
            prev_prompt = t_in + t_cache
            ctx += t_in + t_out
            est = True
        priced.append({
            "turn_index": len(priced),
            "started_at": turn["started_at"],
            "model": model,
            "requests": 1,
            "input_tokens": t_in,
            "output_tokens": t_out,
            "cache_read_tokens": t_cache,
            "cache_write_tokens": 0,
            "total_tokens": t_in + t_out + t_cache,
            "cost_usd": _cost(t_in, t_out, t_cache, model),
            "est": est,
            "text": _truncate_prefer_pr(
                turn["user_text"] + "\n" + turn["text"], 12000),
        })
    return priced


def _session_from_turns(cid, meta, turns, ws_names):
    if not turns:
        return None
    days = collections.defaultdict(lambda: collections.Counter())
    by_model_day = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    used = collections.Counter()
    blob = []
    for turn in turns:
        day = _local_day(turn["started_at"]) or ""
        d = days[day]
        d["requests"] += turn["requests"]
        d["cost_usd"] += turn["cost_usd"]
        d["est_usd"] += turn["cost_usd"] if turn["est"] else 0.0
        d["input_tokens"] += turn["input_tokens"]
        d["output_tokens"] += turn["output_tokens"]
        d["cache_read_tokens"] += turn["cache_read_tokens"]
        d["cache_write_tokens"] += turn.get("cache_write_tokens") or 0
        d["total_tokens"] += turn["total_tokens"]
        d["measured_tokens"] += 0 if turn["est"] else turn["total_tokens"]
        d["on_demand_usd"] += turn["cost_usd"] if turn.get("on_demand") else 0.0
        m = by_model_day[day][turn["model"]]
        m["requests"] += turn["requests"]
        m["cost_usd"] += turn["cost_usd"]
        m["input_tokens"] += turn["input_tokens"]
        m["output_tokens"] += turn["output_tokens"]
        m["cache_read_tokens"] += turn["cache_read_tokens"]
        m["cache_write_tokens"] += turn.get("cache_write_tokens") or 0
        used[turn["model"]] += turn["requests"]
        blob.append(turn.get("text") or "")
    title = (meta.get("title") or "").strip()
    if _is_untitled(title):
        title = _title_from_text("\n".join(t.get("text") or "" for t in turns)) or title
    title = title or UNTITLED_TITLE
    repo = (meta.get("repository") or "").strip()
    if not repo:
        ws_repo = _repo_from_path(meta.get("workspace_path") or "")
        if ws_repo and not ws_repo.endswith(".code-workspace"):
            repo = ws_repo
    if not repo:
        repo = ws_names.get(meta.get("workspace_id") or "", "")
    mode = meta.get("mode") or ""
    extra = []
    if mode:
        extra.append(mode)
    if meta.get("subagent"):
        extra.append("subagent")
    branch = meta.get("branch") or " · ".join(extra)
    if extra and meta.get("branch"):
        branch = meta["branch"] + " · " + " · ".join(extra)
    s = {
        "session_id": cid,
        "title": title,
        "repository": repo,
        "workspace": repo,
        "branch": branch,
        "subtitle": meta.get("subtitle") or "",
        "tracked_repos": list(meta.get("tracked_repos") or []),
        "workspace_path": meta.get("workspace_path") or "",
        "days": {d: dict(c) for d, c in days.items()},
        "by_model_day": {d: {k: dict(c) for k, c in mm.items()}
                         for d, mm in by_model_day.items()},
        "text": " ".join(blob),
        "subagent": bool(meta.get("subagent")),
        "draft": bool(meta.get("draft")),
    }
    return _fill_totals(s)


def _fill_totals(base):
    days = base.get("days") or {}
    by_model_day = base.get("by_model_day") or {}
    agg = collections.Counter()
    for c in days.values():
        for k, v in c.items():
            agg[k] += v
    models = collections.defaultdict(lambda: collections.Counter())
    for mm in by_model_day.values():
        for name, c in mm.items():
            for key, val in c.items():
                models[name][key] += val
    dates = sorted(d for d in days if d)
    top = max(models.items(), key=lambda kv: kv[1]["requests"])[0] if models else "auto"
    est = agg["est_usd"] > 0.0
    return dict(base, **{
        "top_model": top,
        "models": len(models),
        "by_model": {k: dict(c) for k, c in models.items()},
        "turns": int(agg["requests"]),
        "requests": int(agg["requests"]),
        "input_tokens": round(agg["input_tokens"]),
        "output_tokens": round(agg["output_tokens"]),
        "cache_read_tokens": round(agg["cache_read_tokens"]),
        "cache_write_tokens": round(agg["cache_write_tokens"]),
        "total_tokens": round(agg["total_tokens"]),
        "measured_tokens": round(agg["measured_tokens"]),
        "cost_usd": agg["cost_usd"],
        "est_usd": agg["est_usd"],
        "on_demand_usd": agg["on_demand_usd"],
        "est": est,
        "first_day": dates[0] if dates else "",
        "last_day": dates[-1] if dates else "",
    })


def _clip(session, start, end):
    """Restrict one chat to a date range — same contract as Copilot `_vs_clip`.

    Clip spend/days only. Keep refs.prs intact. This is how Cursor behaved before
    the day-filter experiments (#27–#29) and how github_copilot_dashboard still
    behaves.
    """
    first, last = session.get("first_day") or "", session.get("last_day") or ""
    # Undated sessions (common for brand-new cloud agents) must not disappear on
    # All-time views: empty strings fail start<=first string compares.
    if not first and not last:
        if start <= MIN_DAY and end >= MAX_DAY:
            return session
        if session.get("cloud_agent") and start <= MIN_DAY:
            return session
    if first and last and start <= first and last <= end:
        return session
    days = {d: c for d, c in (session.get("days") or {}).items()
            if d and start <= d <= end}
    if not days:
        return None
    bmd = {d: c for d, c in (session.get("by_model_day") or {}).items() if d in days}
    keep = {"session_id", "title", "repository", "workspace", "branch", "subtitle",
            "text", "subagent", "draft", "refs", "billed", "source", "account_label",
            "unattributed", "orphan_billed", "billing_note", "tracked_repos",
            "cloud_agent", "cloud_agent_id", "cloud_url", "automation_id",
            "matched_local_id", "title_source", "title_status", "is_headless",
            "repo_source", "repo_split", "git_correlation",
            "workspace_path", "tracked_repo_paths", "repo_weights",
            "shared_attribution", "pr_turn_costs"}
    base = {k: session[k] for k in keep if k in session}
    base["days"] = days
    base["by_model_day"] = bmd
    # Do not filter refs — same as github_copilot_dashboard._vs_clip / pre-#27.
    return _fill_totals(base)


def _turn_maps_from_priced(priced_by_sid, refs=None, sessions_by_id=None,
                           activities=None, path_by_github=None):
    """priced_by_sid: {sid: [turns with text, cost_usd, total_tokens, turn_index]}"""
    turn_prs = {}
    turn_cost = {}
    for sid, turns in priced_by_sid.items():
        if not turns:
            continue
        sess = (sessions_by_id or {}).get(sid) or {}
        hints = _repo_hints(sess, refs.get(sid) if refs else None)
        prs = {}
        costs = {}
        for turn in turns:
            ti = turn["turn_index"]
            costs[ti] = (turn["cost_usd"] or 0, turn.get("total_tokens") or 0)
            text = turn.get("text") or ""
            created_here = bool(RE_CREATED.search(text))
            found = {}
            for owner, repo, num in RE_PR.findall(text):
                name = clean_repo(owner, repo)
                if not name:
                    continue
                k = f"{name}#{num}"
                found[k] = found.get(k, False) or created_here
            for owner, repo, num in RE_PR_SHORT.findall(text):
                name = clean_repo(owner, repo)
                if not name:
                    continue
                k = f"{name}#{num}"
                found[k] = found.get(k, False) or created_here
            for num_s in RE_PR_BARE.findall(text):
                num = int(num_s)
                repo = _resolve_bare_pr_repo(
                    num, hints, text, sess,
                    activities=activities, priced_all=priced_by_sid,
                    path_by_github=path_by_github,
                    refs_entry=refs.get(sid) if refs else None)
                if not repo:
                    continue
                k = f"{repo}#{num}"
                found[k] = found.get(k, False) or created_here
            if found:
                prs[ti] = found
        turn_prs[sid] = prs
        turn_cost[sid] = costs
    return turn_prs, turn_cost


def _billed_composer_ids(billing):
    ids = set()
    for ev in (billing or {}).get("events") or []:
        cid = _billing_event_cid(ev)
        if cid == "_unattributed":
            cid = None
        if cid:
            ids.add(cid)
    return ids


def _session_stub_from_meta(cid, meta, ws_names):
    """Title/repo fields from composerHeaders without reading chat bubbles."""
    info = meta.get(cid) or {}
    repo = info.get("repository") or _repo_from_path(info.get("workspace_path")) \
        or ws_names.get(info.get("workspace_id") or "", "")
    return {
        "session_id": cid,
        "title": (info.get("title") or "").strip(),
        "subtitle": info.get("subtitle") or "",
        "repository": repo,
        "workspace": repo,
        "branch": info.get("branch") or "",
        "workspace_path": info.get("workspace_path") or "",
        "tracked_repos": info.get("tracked_repos") or [],
        "text": "",
        "subagent": bool(info.get("subagent")),
    }


def _load_bubbles(con, meta, skip_cids=None, cap=BUBBLE_CAP_PER_COMPOSER):
    """One pass over bubbleId rows; heavy composers reload head+tail in time order."""
    skip_cids = skip_cids or set()
    load_cids = {cid for cid in meta if cid and cid not in skip_cids
                 and cid != "empty-state-draft"}
    bubbles = collections.defaultdict(list)
    composer_owners = collections.defaultdict(collections.Counter)
    key_counts = collections.Counter()
    loaded = 0
    try:
        bubble_keys = con.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'")
    except sqlite3.OperationalError:
        bubble_keys = []
    for row in bubble_keys:
        key = row[0]
        parts = key.split(":")
        if len(parts) < 3:
            continue
        cid = parts[1]
        if cid not in load_cids:
            continue
        key_counts[cid] += 1
    heavy = {cid for cid, n in key_counts.items() if n > cap}
    try:
        bubble_rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'")
    except sqlite3.OperationalError:
        bubble_rows = []
    for key, raw in bubble_rows:
        parts = key.split(":")
        if len(parts) < 3:
            continue
        cid = parts[1]
        if cid not in load_cids or cid in heavy:
            continue
        row = _bubble_dict_from_raw(key, raw)
        if not row:
            continue
        if row.get("owner_uid"):
            composer_owners[cid][row["owner_uid"]] += 1
        bubbles[cid].append({
            "id": row["id"],
            "type": row["type"],
            "created": row["created"],
            "text": row["text"],
            "inn": row["inn"],
            "out": row["out"],
            "model": row["model"],
        })
        loaded += 1
    for cid in heavy:
        bubs, owners = _load_bubbles_for_cid(con, cid, cap=cap)
        if bubs:
            bubbles[cid] = bubs
            composer_owners[cid].update(owners)
            loaded += len(bubs)
    for cid in load_cids:
        meta.setdefault(cid, {
            "title": "", "subtitle": "", "workspace_id": "", "workspace_path": "",
            "repository": "", "branch": "", "tracked_repos": [], "tracked_repo_paths": [],
            "created_ms": 0, "updated_ms": 0,
            "mode": "", "subagent": False, "draft": False, "archived": False,
            "model": "auto",
        })
    return bubbles, composer_owners, loaded



def _join_diagnostics(db_path, sample_cids=None):
    """Explain why billed chats may still be (untitled)."""
    out = {
        "db": db_path,
        "composer_index": _composer_index_stats(db_path),
        "local_composer_ids": 0,
        "bubble_composer_ids": 0,
        "sample": [],
        "build": "titles-v6",
    }
    if not db_path or not os.path.isfile(db_path):
        out["error"] = "db-missing"
        return out
    try:
        with connect(db_path) as con:
            try:
                bubble_ids = set()
                for (key,) in con.execute(
                        "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' LIMIT 200000"):
                    parts = key.split(":")
                    if len(parts) >= 2 and parts[1]:
                        bubble_ids.add(parts[1])
                out["bubble_composer_ids"] = len(bubble_ids)
            except sqlite3.OperationalError:
                bubble_ids = set()
            try:
                data_ids = set()
                for (key,) in con.execute(
                        "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%' LIMIT 200000"):
                    cid = key.split(":", 1)[-1]
                    if cid:
                        data_ids.add(cid)
                out["local_composer_ids"] = len(data_ids | bubble_ids)
            except sqlite3.OperationalError:
                data_ids = set()
            meta = _header_meta(con)
            out["meta_titles"] = sum(1 for m in meta.values() if not _is_untitled(m.get("title")))
            out["meta_total"] = len(meta)
            for cid in list(sample_cids or [])[:12]:
                row = {
                    "cid": cid,
                    "in_meta": cid in meta,
                    "meta_title": (meta.get(cid) or {}).get("title") or "",
                    "has_bubbles": cid in bubble_ids,
                    "has_composer_data": cid in data_ids,
                    "bubble_title": "",
                    "transcript_title": "",
                }
                if _is_untitled(row["meta_title"]):
                    row["bubble_title"] = _first_user_bubble_title(con, cid)
                    row["transcript_title"] = _title_from_agent_transcripts(cid)
                out["sample"].append(row)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def scan_cursor(force=False):
    now = time.time()
    if not force and _CACHE["data"] is not None and (now - _CACHE["at"]) < 120:
        return _CACHE["data"]
    stamp = _logical_stamp()
    if not force and _CACHE["stamp"] == stamp and _CACHE["data"] is not None:
        return _CACHE["data"]
    with SCAN_LOCK:
        if not force and _CACHE["data"] is not None and (time.time() - _CACHE["at"]) < 120:
            return _CACHE["data"]
        if not force and _CACHE["stamp"] == stamp and _CACHE["data"] is not None:
            return _CACHE["data"]
        print("Scanning Cursor chat store...", flush=True)
        t_scan = time.perf_counter()
        skip_bubbles = set()
        billed_text_rows = 0
        ws_names = _workspace_map()
        account = {"email": "", "git_email": "", "user_id": "", "previous_email": ""}
        billing_error = None
        billing = None
        with connect() as con:
            account = _signed_in_account(con)
            meta = _header_meta(con)
            _workspace_composer_titles(meta)
            t_meta = time.perf_counter()
            t_billing = t_meta
            if API_ENABLED:
                try:
                    billing = fetch_billing(con, force=force)
                except Exception as exc:
                    billing_error = str(exc)
                    print(f"  billing API unavailable ({exc}); using local transcript estimates",
                          flush=True)
            t_billing = time.perf_counter()
            skip_bubbles = _billed_composer_ids(billing) if billing else set()
            bubbles, composer_owners, bubble_rows = _load_bubbles(
                con, meta, skip_cids=skip_bubbles)
            billed_texts, billed_rows, billed_text_rows = {}, {}, 0
            if skip_bubbles:
                billed_texts, billed_rows, billed_text_rows = _load_bubble_texts_for_cids(
                    con, skip_bubbles)
                # Legacy / sparse chats: pull text from composerData.conversationMap
                # when bubbleId rows are missing so titles/PRs can still resolve.
                for cid in list(skip_bubbles):
                    if billed_texts.get(cid):
                        continue
                    fallback = _composer_data_fallback_text(con, cid)
                    if fallback:
                        billed_texts[cid] = fallback
        t_bubbles = time.perf_counter()
        if skip_bubbles:
            print(f"  billed chat text: {len(billed_texts)} composer(s), "
                  f"{billed_text_rows} bubble rows (costs from API)", flush=True)

        sessions, turns_api, priced_all, texts_by_cid = [], {}, {}, {}
        for cid, bubs in bubbles.items():
            info = meta.get(cid) or {}
            if cid == "empty-state-draft" or (info.get("draft") and not bubs):
                continue
            priced = _turns_from_bubbles(bubs, info.get("model") or "auto")
            sess = _session_from_turns(cid, info, priced, ws_names)
            if sess is None:
                continue
            sessions.append(sess)
            priced_all[cid] = priced
            turns_api[cid] = [{k: t[k] for k in (
                "turn_index", "started_at", "model", "requests", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "total_tokens", "cost_usd", "est")} for t in priced]
            texts_by_cid[cid] = (sess.get("title") or "") + " " + (sess.get("subtitle") or "") \
                + " " + " ".join(t.get("text") or "" for t in priced)

        refs = {}
        if billing:
            local_by_id = {s["session_id"]: s for s in sessions}
            for cid in skip_bubbles:
                stub = _session_stub_from_meta(cid, meta, ws_names)
                if cid in billed_texts:
                    stub["text"] = billed_texts[cid]
                    _fill_missing_title(stub, billed_texts[cid])
                local_by_id[cid] = stub
                if billed_texts.get(cid) or not _is_untitled(stub.get("title")):
                    texts_by_cid[cid] = (
                        (stub.get("title") or "") + " " + (stub.get("subtitle") or "")
                        + " " + (stub.get("text") or billed_texts.get(cid) or "")
                    ).strip()
            local_turns, local_priced = turns_api, priced_all
            billed_sessions, turns_api, priced_all = _sessions_from_billing(
                billing["events"], local_by_id, meta, ws_names)
            for sess in billed_sessions:
                cid = sess["session_id"]
                if cid in billed_rows and cid in priced_all:
                    _attach_text_to_priced_turns(priced_all[cid], billed_rows[cid])
                    if cid in turns_api:
                        for src, dst in zip(priced_all[cid], turns_api[cid]):
                            if src.get("text"):
                                dst["text"] = src["text"]
                if cid in billed_texts:
                    sess["text"] = billed_texts[cid]
                # Billed turns ship with empty text; titles were frozen as (untitled)
                # before bubble text was joined. Re-derive once chat text is present.
                _fill_missing_title(
                    sess,
                    sess.get("text") or "",
                    priced_all.get(cid) or [],
                    (local_by_id.get(cid) or {}).get("text") or "",
                )
                if not _is_untitled(sess.get("title")) or sess.get("text"):
                    texts_by_cid[cid] = (
                        (sess.get("title") or "") + " " + (sess.get("subtitle") or "")
                        + " " + (sess.get("text") or "")
                    ).strip()
            # Orphan UUID recovery: reverse-embed in composerData + fuzzy local match.
            # Cloud-agent orphans (bc-* / cloudAgentId) are left for the API enrich step.
            try:
                with connect() as orphan_con:
                    n_orphan = resolve_orphan_sessions(
                        billed_sessions, local_by_id,
                        priced_all=priced_all, local_priced=local_priced,
                        con=orphan_con, meta=meta)
                if n_orphan:
                    for sess in billed_sessions:
                        if sess.get("matched_local_id") or (
                                sess.get("title_source") in (
                                    "embedded-id", "fuzzy-local")
                                and sess.get("title")):
                            texts_by_cid[sess["session_id"]] = (
                                (texts_by_cid.get(sess["session_id"]) or "")
                                + " " + (sess.get("title") or "")
                                + " " + (sess.get("text") or "")
                            ).strip()
            except Exception as exc:
                print(f"  orphan resolution skipped ({exc})", flush=True)
            billed_ids = {s["session_id"] for s in billed_sessions}
            current_uid = account.get("user_id") or ""
            prev_email = account.get("previous_email") or "previous Cursor account"
            other = []
            for local in sessions:
                cid = local["session_id"]
                if cid in billed_ids or cid == "empty-state-draft":
                    continue
                title = (local.get("title") or "").strip()
                real = title and title != "(untitled)"
                if not real and (local.get("requests") or 0) < 5 and (local.get("cost_usd") or 0) < 0.25:
                    continue
                owners = composer_owners.get(cid) or {}
                top_uid = owners.most_common(1)[0][0] if owners else ""
                if top_uid and current_uid and top_uid != current_uid:
                    label = prev_email
                elif top_uid and current_uid and top_uid == current_uid:
                    continue
                else:
                    label = prev_email or "not on signed-in account"
                extra = dict(local)
                extra["billed"] = False
                extra["est"] = True
                extra["source"] = "local-other-account"
                extra["account_label"] = label
                other.append(extra)
                if cid in local_turns:
                    turns_api[cid] = local_turns[cid]
                if cid in local_priced:
                    priced_all[cid] = local_priced[cid]
            sessions = billed_sessions
            for sess in sessions:
                local = local_by_id.get(sess["session_id"])
                sess["account_label"] = sess.get("account_label") or account.get("email") or ""
                if local and not _is_untitled(local.get("title")):
                    sess["title"] = local["title"]
                if local:
                    if local.get("tracked_repos"):
                        sess["tracked_repos"] = local["tracked_repos"]
                    if local.get("tracked_repo_paths"):
                        sess["tracked_repo_paths"] = local["tracked_repo_paths"]
                    if local.get("workspace_path"):
                        sess["workspace_path"] = local["workspace_path"]
                    if local.get("repository") and not local.get("repo_split"):
                        sess["repository"] = local["repository"]
                        sess["workspace"] = local.get("workspace") or local["repository"]
                else:
                    mm = meta.get(sess["session_id"]) or {}
                    if mm.get("workspace_path"):
                        sess["workspace_path"] = mm["workspace_path"]
                    if mm.get("tracked_repos"):
                        sess["tracked_repos"] = mm["tracked_repos"]
                    if mm.get("tracked_repo_paths"):
                        sess["tracked_repo_paths"] = mm["tracked_repo_paths"]
            other.sort(key=lambda s: s["cost_usd"], reverse=True)
            sessions = sessions + other
            print(f"  billed {len(billed_sessions)} conversations"
                  + (f"; {len(other)} local chats not on {account.get('email') or 'this account'}"
                     if other else ""), flush=True)

        cloud_agents = []
        if CLOUD_AGENTS_ENABLED:
            try:
                cookie = ((billing or {}).get("cookie") or "").strip()
                if not cookie:
                    try:
                        with connect() as c2:
                            cookie = _cursor_session_cookie(c2) or ""
                    except Exception:
                        cookie = ""
                cloud_agents = fetch_cloud_agents(
                    force=force, with_usage=True, cookie=cookie)
            except Exception as exc:
                print(f"  cloud agents skipped ({exc})", flush=True)
        if cloud_agents:
            sessions, turns_api, priced_all, _n = enrich_sessions_with_cloud_agents(
                sessions, turns_api, priced_all, cloud_agents)
            for sess in sessions:
                if sess.get("cloud_agent") and sess.get("title"):
                    texts_by_cid[sess["session_id"]] = (
                        (texts_by_cid.get(sess["session_id"]) or "")
                        + " " + sess.get("title", "") + " " + (sess.get("subtitle") or "")
                    ).strip()

        # Final title pass: ItemTable/SQL names may still be empty while bubble
        # text or agent transcripts exist under the billed conversation id.
        try:
            n_fill = _fill_titles_from_local_content(con, sessions, meta)
            if n_fill:
                print(f"  filled {n_fill} untitled title(s) from local bubbles/transcripts",
                      flush=True)
                for sess in sessions:
                    if sess.get("title_source") and sess.get("session_id"):
                        texts_by_cid[sess["session_id"]] = (
                            (texts_by_cid.get(sess["session_id"]) or "")
                            + " " + (sess.get("title") or "")
                        ).strip()
        except Exception as exc:
            print(f"  title backfill skipped ({exc})", flush=True)

        # Full-composer PR scan (not head/tail sampled) so older tool-only PRs
        # still reach refs — sampling alone kept only the newest visible URLs.
        pr_cids = list({s["session_id"] for s in sessions if s.get("session_id")}
                       | set(texts_by_cid))
        n_pr_links, pr_day_hints = _inject_composer_pr_links(con, texts_by_cid, pr_cids)
        if n_pr_links:
            print(f"  full-scan PR links: +{n_pr_links} across {len(pr_day_hints)} chat(s)",
                  flush=True)

        allow = JIRA_KEY_ALLOW | _dynamic_jira_keys(texts_by_cid.values())
        sess_repo = {s["session_id"]: s["repository"] for s in sessions if s.get("repository")}
        refs = _build_refs(list(texts_by_cid.items()), sess_repo, allow)
        git_repos = _discover_git_repos(meta)
        path_by_github = _github_path_map(git_repos=git_repos)
        _assign_session_repos(sessions, meta, refs, path_by_github)
        for sess in sessions:
            sess["refs"] = refs.get(sess["session_id"], {"jira": [], "prs": [], "repos": []})

        sessions.sort(key=lambda s: (0 if s.get("billed") else 1, -(s.get("cost_usd") or 0)))
        git_since, git_until = _activity_date_bounds(
            sessions, (billing or {}).get("events") if billing else None)
        git_acts = _fetch_git_activities(git_repos, git_since, git_until) if git_repos else []
        _apply_bare_pr_refs(sessions, refs, texts_by_cid, path_by_github,
                            priced_all=priced_all, activities=git_acts)
        if billing and git_acts:
            _correlate_git_to_sessions(
                sessions, priced_all, refs, turns_api, git_acts,
                path_by_github=path_by_github)
            print(f"  git correlation: {len(git_repos)} repos, "
                  f"{len(git_acts)} commits ({git_since}..{git_until})",
                  flush=True)
        _apply_git_pr_discovery(
            sessions, refs, priced_all, git_acts, path_by_github)
        for sess in sessions:
            sess["refs"] = refs.get(sess["session_id"], sess.get("refs"))
        turn_prs, turn_cost = _turn_maps_from_priced(
            priced_all, refs, {s["session_id"]: s for s in sessions},
            git_acts, path_by_github)
        # GitHub create/merge days first (authoritative). Mention stamps only
        # fill gaps afterward — otherwise today's re-mention of #20 wins and
        # undated #25–#28 are dropped by the day filter.
        n_cloud_prs = _enrich_cloud_agent_prs(sessions, refs)
        n_gh_days = _stamp_pr_github_dates(refs)
        _stamp_pr_mention_days(refs, priced_all, turn_prs, pr_day_hints)
        if n_cloud_prs or n_gh_days:
            print(f"  cloud-agent GitHub PRs: +{n_cloud_prs} linked, "
                  f"{n_gh_days} day-stamp(s) from createdAt/mergedAt",
                  flush=True)
        for sess in sessions:
            sess["refs"] = refs.get(sess["session_id"], sess.get("refs"))
        git_pr_catalog = _git_pr_catalog(git_acts) if git_acts else {}
        data = {
            "sessions": sessions,
            "turns": turns_api,
            "turn_prs": turn_prs,
            "turn_cost": turn_cost,
            "refs": refs,
            "git_pr_catalog": git_pr_catalog,
            "path_by_github": path_by_github,
            "db": DB_PATH,
            "billed": bool(billing),
            "billing_error": billing_error,
            "billing_summary": (billing or {}).get("summary"),
            "billing_period": (billing or {}).get("period"),
            "billing_aggregations": _parse_aggregated_usage(
                (billing or {}).get("aggregations")),
            "billing_email": account.get("email") or "",
            "billing_emails": (billing or {}).get("emails") or (
                [account.get("email")] if account.get("email") else []),
            "previous_email": account.get("previous_email") or "",
            "invoices": _public_invoices((billing or {}).get("invoices")),
            "cloud_agents": len(cloud_agents) if CLOUD_AGENTS_ENABLED else 0,
            "cloud_agents_error": (_CLOUD_AGENTS_CACHE.get("error") or ""),
            "cloud_agents_source": (_CLOUD_AGENTS_CACHE.get("source") or ""),
        }
        _CACHE["stamp"], _CACHE["data"], _CACHE["at"] = stamp, data, time.time()
        t_done = time.perf_counter()
        print(f"  indexed {len(sessions)} chats · {bubble_rows + billed_text_rows} bubble rows · "
              f"{t_done - t_scan:.1f}s "
              f"(meta {t_meta - t_scan:.1f}s, billing {t_billing - t_meta:.1f}s, "
              f"bubbles {t_bubbles - t_billing:.1f}s, rest {t_done - t_bubbles:.1f}s)",
              flush=True)
        return data

# --------------------------------------------------------------------------
# Reference extraction: Jira tickets, GitHub repos and pull requests mentioned
# (or created) inside each chat's turn text.
# --------------------------------------------------------------------------

RE_JIRA_URL = re.compile(r"https?://([\w.-]+\.atlassian\.net)/browse/([A-Z][A-Z0-9]{1,9}-\d+)")
RE_JIRA_ANY = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]{1,9}-\d+)")
RE_JIRA_BARE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")
RE_PR = re.compile(r"(?<![\w.])github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
RE_PR_API = re.compile(
    r"(?<![\w.])api\.github\.com/repos/([\w.-]+)/([\w.-]+)/pulls/(\d+)")
RE_PR_SHORT = re.compile(r"(?<![\w./])([\w.-]+)/([\w.-]+)#(\d+)")
RE_PR_BARE = re.compile(r"\b(?:PR|pull request)\s*#?\s*(\d+)\b", re.I)
RE_REPO_URL = re.compile(r"(?<![\w.])github\.com/([\w.-]+)/([\w.-]+)")
RE_CREATED = re.compile(
    r"(gh pr create|(?:created|opened|raised|submitted)\s+(?:a\s+|the\s+|new\s+|draft\s+)*"
    r"(?:pull request|PR)\b|(?:pull request|PR)\s+(?:#\d+\s+)?(?:was\s+)?(?:successfully\s+)?"
    r"(?:created|opened))", re.I)


def clean_repo(owner, repo):
    repo = (repo or "").rstrip(".").removesuffix(".git")
    owner = (owner or "").strip()
    if not repo or not owner or owner.lower() in GH_RESERVED or repo.lower() in GH_RESERVED:
        return None
    if repo.lower() == "pull" or "..." in owner or "..." in repo:
        return None
    if not re.match(r"^[\w.-]+$", owner) or not re.match(r"^[\w.-]+$", repo):
        return None
    return f"{owner}/{repo}"


def _repo_hints(session, refs_entry, path_by_github=None):
    """GitHub repo names for resolving bare PR #123 mentions in chat text."""
    hints = set()
    end_ts = _session_end_ts(session) if session else None
    owner = ""
    for x in (refs_entry or {}).get("repos") or []:
        name = x.get("name") or ""
        if "/" in name:
            owner = owner or name.split("/")[0]
        if "/" not in name or x.get("role") in ("shared-skipped",):
            continue
        if path_by_github and end_ts:
            path = path_by_github.get(name, "")
            if path and not _repo_existed_at(path, end_ts):
                continue
        hints.add(name)
    for p in (refs_entry or {}).get("prs") or []:
        if p.get("role") == "skipped":
            continue
        repo = p.get("repo") or (p.get("key") or "").split("#")[0]
        if repo and "/" in repo:
            hints.add(repo)
    repo_field = (session or {}).get("repository") or ""
    if not owner and "/" in repo_field:
        owner = repo_field.split("/")[0]
    for tr in (session or {}).get("tracked_repos") or []:
        if not tr:
            continue
        if "/" in tr:
            name = tr
        else:
            name = f"{owner}/{tr}" if owner else tr
        if "/" not in name:
            continue
        if path_by_github and end_ts:
            path = path_by_github.get(name, "")
            if path and not _repo_existed_at(path, end_ts):
                continue
        hints.add(name)
    return sorted(hints)


def _git_pr_catalog(activities):
    """All merge PRs seen in git log for the active date range."""
    catalog = {}
    for act in activities or []:
        pk = act.get("pr")
        if not pk:
            continue
        repo = pk.split("#")[0]
        num = int(pk.split("#")[1])
        entry = catalog.setdefault(pk, {
            "key": pk, "repo": repo, "number": num,
            "cost_usd": 0.0, "on_demand_usd": 0.0, "total_tokens": 0,
            "chats": 0, "titles": [], "created": False,
            "role": "git", "inferred": True, "est": False,
        })
        entry["merge_ts"] = min(entry.get("merge_ts") or act["ts"], act["ts"])
    return catalog


def _apply_bare_pr_refs(sessions, refs, texts_by_cid, path_by_github=None,
                         priced_all=None, activities=None):
    """Resolve bare 'PR #17' mentions using session repo context."""
    for s in sessions:
        sid = s["session_id"]
        text = texts_by_cid.get(sid) or s.get("text") or ""
        if not text:
            continue
        hints = _repo_hints(s, refs.get(sid), path_by_github)
        if not hints:
            continue
        r = refs.setdefault(sid, {"jira": [], "prs": [], "repos": []})
        existing = {p["key"]: dict(p) for p in r.get("prs") or []}
        created_here = bool(RE_CREATED.search(text))
        bare_nums = {int(n) for n in RE_PR_BARE.findall(text)}
        for num in bare_nums:
            repo = _resolve_bare_pr_repo(
                num, hints, text, s, activities, priced_all, path_by_github, r)
            if not repo:
                continue
            k = f"{repo}#{num}"
            if k not in existing:
                existing[k] = {
                    "key": k, "repo": repo, "number": num, "created": created_here,
                    "bare": True,
                }
            else:
                existing[k]["created"] = existing[k].get("created") or created_here
                existing[k]["bare"] = True
        for num in bare_nums:
            repo = _resolve_bare_pr_repo(
                num, hints, text, s, activities, priced_all, path_by_github, r)
            if not repo:
                continue
            keep_key = f"{repo}#{num}"
            for k in list(existing.keys()):
                if k.endswith(f"#{num}") and k != keep_key and existing[k].get("bare"):
                    del existing[k]
        r["prs"] = sorted(existing.values(), key=lambda p: (p["repo"], p["number"]))


def _dynamic_jira_keys(texts):
    global JIRA_BASE
    keys, hosts = set(), collections.Counter()
    for t in texts:
        for host, tk in RE_JIRA_URL.findall(t):
            hosts[host] += 1
            keys.add(tk.split("-")[0])
        for tk in RE_JIRA_ANY.findall(t):
            keys.add(tk.split("-")[0])
    if not JIRA_BASE and hosts:
        JIRA_BASE = "https://" + hosts.most_common(1)[0][0]
    return keys


def _jira_credentials():
    """Atlassian email + API token from env vars or Windows Credential Manager."""
    email = (os.environ.get("CURSOR_DASH_JIRA_EMAIL")
             or os.environ.get("ATLASSIAN_EMAIL") or "").strip()
    token = (os.environ.get("CURSOR_DASH_JIRA_TOKEN")
             or os.environ.get("ATLASSIAN_API_TOKEN") or "").strip()
    if email and token:
        return email, token
    if os.name != "nt":
        return "", ""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "get_atlassian_credential.ps1")
    if not os.path.isfile(script):
        return "", ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            capture_output=True, text=True, timeout=15, check=False)
        if out.returncode != 0:
            return "", ""
        raw = out.stdout.strip()
        data = json.loads(raw)
        if isinstance(data, str):
            data = json.loads(data)
        return ((data.get("Email") or "").strip(),
                (data.get("Token") or "").strip())
    except Exception:
        return "", ""


def _jira_field_name(field):
    if field is None:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        return (field.get("name") or field.get("displayName")
                or field.get("value") or "")
    return str(field)


def _jira_parse_issue(issue):
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    itype = fields.get("issuetype") or {}
    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}
    parent = fields.get("parent") or {}
    return {
        "summary": fields.get("summary") or "",
        "status": status.get("name") or "",
        "status_category": ((status.get("statusCategory") or {}).get("key") or ""),
        "type": itype.get("name") or "",
        "assignee": assignee.get("displayName") or "Unassigned",
        "priority": priority.get("name") or "",
        "updated": (fields.get("updated") or "")[:10],
        "created": (fields.get("created") or "")[:10],
        "parent": parent.get("key") or "",
        "labels": fields.get("labels") or [],
    }


def _jira_api(email, token, path, params=None, body=None, method=None, timeout=30):
    base = (JIRA_BASE or JIRA_BASE_DEFAULT).rstrip("/")
    url = base + path
    if params and body is None:
        url += "?" + urlencode(params)
    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        method=method or ("POST" if body is not None else "GET"),
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _jira_verify_auth(email, token):
    """Return None on success, or a short error string."""
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                      (token or "").lower()):
        return ("Stored value looks like a token ID (UUID), not the API secret. "
                "Re-run store_atlassian_token.ps1 with the ATATT… secret from "
                "id.atlassian.com (shown only once at creation).")
    try:
        _jira_api(email, token, "/rest/api/3/myself")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return ("Jira authentication failed — check email and API token, then "
                    "re-run store_atlassian_token.ps1")
        return f"Jira API HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return f"Jira API: {type(exc).__name__}: {exc}"
    return None


def _jira_fetch_issues(keys):
    """Bulk-fetch Jira issue fields for ticket keys. Cached briefly."""
    global _JIRA_LAST_ERROR
    keys = sorted({k for k in keys if k})
    if not keys:
        return {}
    email, token = _jira_credentials()
    if not email or not token:
        _JIRA_LAST_ERROR = ("Jira credentials not configured — run "
                            "store_atlassian_token.ps1 or set "
                            "CURSOR_DASH_JIRA_EMAIL / CURSOR_DASH_JIRA_TOKEN")
        return {}
    auth_err = _jira_verify_auth(email, token)
    if auth_err:
        _JIRA_LAST_ERROR = auth_err
        return {}
    now = time.time()
    cache = _JIRA_ISSUE_CACHE
    keyset = frozenset(keys)
    if (cache["data"] and now - cache["at"] < JIRA_ISSUE_TTL
            and keyset <= cache["keys"]):
        return {k: cache["data"][k] for k in keys if k in cache["data"]}
    out = {}
    field_list = ["summary", "status", "issuetype", "assignee", "priority",
                  "updated", "created", "parent", "labels"]
    for i in range(0, len(keys), 40):
        chunk = keys[i:i + 40]
        jql = "key in (" + ",".join(chunk) + ")"
        try:
            data = _jira_api(email, token, "/rest/api/3/search/jql", body={
                "jql": jql,
                "maxResults": len(chunk),
                "fields": field_list,
            })
        except urllib.error.HTTPError as exc:
            _JIRA_LAST_ERROR = f"Jira API HTTP {exc.code}: {exc.reason}"
            return out
        except Exception as exc:
            _JIRA_LAST_ERROR = f"Jira API: {type(exc).__name__}: {exc}"
            return out
        for issue in data.get("issues") or []:
            key = issue.get("key")
            if key:
                out[key] = _jira_parse_issue(issue)
    _JIRA_LAST_ERROR = ""
    cache["at"], cache["data"] = now, out
    cache["keys"] = keyset
    return out


def _enrich_jira_rollup(rollup):
    items = (rollup or {}).get("jira") or []
    if not items:
        return rollup
    details = _jira_fetch_issues([i.get("key") for i in items if i.get("key")])
    for item in items:
        detail = details.get(item.get("key"))
        if detail:
            item.update(detail)
    return rollup


def _build_refs(rows, sess_repo, allow):
    out = {}
    for sid, text in rows:
        d = out.setdefault(sid, {"jira": {}, "prs": {}, "repos": {}})
        for tk in RE_JIRA_ANY.findall(text):
            d["jira"][tk] = True
        for prefix, num in RE_JIRA_BARE.findall(text):
            if prefix in allow and prefix not in JIRA_KEY_DENY:
                d["jira"].setdefault(f"{prefix}-{num}", False)
        created_here = bool(RE_CREATED.search(text))
        for owner, repo, num in RE_PR.findall(text):
            name = clean_repo(owner, repo)
            if not name:
                continue
            k = f"{name}#{num}"
            d["prs"][k] = d["prs"].get(k, False) or created_here
            d["repos"].setdefault(name, "mentioned")
        for owner, repo, num in RE_PR_SHORT.findall(text):
            name = clean_repo(owner, repo)
            if not name:
                continue
            k = f"{name}#{num}"
            d["prs"][k] = d["prs"].get(k, False) or created_here
            d["repos"].setdefault(name, "mentioned")
        for owner, repo in RE_REPO_URL.findall(text):
            name = clean_repo(owner, repo)
            if name:
                d["repos"].setdefault(name, "mentioned")

    for sid, repo in (sess_repo or {}).items():
        d = out.setdefault(sid, {"jira": {}, "prs": {}, "repos": {}})
        if "/" in repo:
            d["repos"][repo] = "primary"

    data = {}
    for sid, d in out.items():
        data[sid] = {
            "jira": sorted(d["jira"], key=lambda k: (k.split("-")[0], int(k.split("-")[1]))),
            "prs": sorted(
                ({"key": k, "repo": k.split("#")[0], "number": int(k.split("#")[1]),
                  "created": v} for k, v in d["prs"].items()),
                key=lambda p: (p["repo"], p["number"])),
            "repos": sorted(({"name": k, "role": v} for k, v in d["repos"].items()),
                            key=lambda r: (r["role"] != "primary", r["name"])),
        }
    return data


def _attach_folder_repos(sessions, refs):
    """Legacy helper — superseded by _attach_tracked_repos."""
    return


def _matches(s, q):
    r = s.get("refs") or {}
    parts = [s.get("title") or "", s.get("repository") or "", s.get("workspace") or "",
             s.get("branch") or "", s.get("top_model") or "", s.get("session_id") or "",
             s.get("subtitle") or ""]
    parts += list(r.get("jira", []))
    parts += [p["key"] for p in r.get("prs", [])]
    parts += [x["name"] for x in r.get("repos", [])]
    return q in " ".join(parts).lower()


def _group_label(keys):
    repos = {k.split("#")[0] for k in keys}
    if len(repos) == 1:
        repo = repos.pop()
        nums = sorted((int(k.split("#")[1]) for k in keys))
        return f"{repo}#" + ", #".join(str(n) for n in nums)
    return ", ".join(keys)


def pr_segments(session_titles, turn_prs, turn_cost):
    out = {}
    for sid, title in session_titles.items():
        anchors = sorted(turn_prs.get(sid, {}))
        if not anchors:
            continue
        costs = turn_cost.get(sid, {})
        prev = -1
        for a in anchors:
            seg = [t for t in costs if prev < t <= a]
            prev = a
            keys = turn_prs[sid][a]
            gk = tuple(sorted(keys))
            e = out.setdefault(gk, {"keys": list(gk), "key": _group_label(gk),
                                    "cost_usd": 0.0, "total_tokens": 0, "chats": 0,
                                    "titles": [], "created": False,
                                    "role": "mentioned", "turns": 0, "est": False})
            e["cost_usd"] += sum(costs[t][0] for t in seg)
            e["total_tokens"] += sum(costs[t][1] for t in seg)
            e["turns"] += len(seg)
            e["chats"] += 1
            e["created"] = e["created"] or any(keys.values())
            if title not in e["titles"] and len(e["titles"]) < 5:
                e["titles"].append(title)
    return sorted(out.values(), key=lambda x: -x["cost_usd"])


def _unattributed(sessions, refs, tabs):
    tot_c = sum(s["cost_usd"] or 0 for s in sessions)
    tot_od = sum(s.get("on_demand_usd") or 0 for s in sessions)
    tot_t = sum(s["total_tokens"] or 0 for s in sessions)
    kinds = {"jira": "jira", "prs": "prs", "repos": "repos"}
    out = {}
    for tab, items in tabs.items():
        cost = tot_c - sum(i["cost_usd"] for i in items)
        od = tot_od - sum(i.get("on_demand_usd") or 0 for i in items)
        toks = tot_t - sum(i["total_tokens"] or 0 for i in items)
        kind = kinds.get(tab)
        chats = sum(1 for s in sessions
                    if not (refs.get(s["session_id"]) or {}).get(kind)) if kind else 0
        out[tab] = {"cost_usd": max(0.0, cost), "on_demand_usd": max(0.0, od),
                    "total_tokens": max(0, round(toks)),
                    "chats": chats, "total_usd": tot_c}
    return out


def _repo_alias_map(refs, path_by_github=None):
    """Map short folder names to canonical owner/repo keys."""
    aliases = {}
    for gh in (path_by_github or {}):
        aliases[gh.split("/")[-1].lower()] = gh
        aliases[gh.lower()] = gh
    for r in (refs or {}).values():
        for it in (r or {}).get("repos") or []:
            name = it.get("name") or ""
            if "/" in name:
                aliases[name.split("/")[-1].lower()] = name
                aliases[name.lower()] = name
    return aliases


def _norm_repo_key(name, aliases):
    if not name:
        return name
    if "/" in name:
        return aliases.get(name.lower(), name)
    return aliases.get(name.lower(), name)


def rollup(sessions, refs, turn_prs, turn_cost, git_pr_catalog=None, path_by_github=None):
    jira, repos = {}, {}
    repo_aliases = _repo_alias_map(refs, path_by_github)
    for s in sessions:
        r = refs.get(s["session_id"])
        if not r:
            continue
        cost, toks = s["cost_usd"] or 0, s["total_tokens"] or 0
        od = s.get("on_demand_usd") or 0
        jira_items = r.get("jira") or []
        if jira_items:
            share_c, share_t = cost / len(jira_items), toks / len(jira_items)
            share_od = od / len(jira_items)
            for tk in jira_items:
                e = jira.setdefault(tk, {"key": tk, "cost_usd": 0.0, "on_demand_usd": 0.0,
                                           "total_tokens": 0,
                                           "chats": 0, "titles": [], "created": False,
                                           "role": "mentioned", "est": False})
                e["cost_usd"] += share_c
                e["on_demand_usd"] += share_od
                e["total_tokens"] += share_t
                e["chats"] += 1
                e["est"] = e["est"] or bool(s.get("est"))
                if len(e["titles"]) < 5:
                    e["titles"].append(s["title"])
        repo_shares = _repo_cost_shares(s, r)
        if repo_shares:
            for it, frac in repo_shares:
                k = _norm_repo_key(it["name"], repo_aliases)
                e = repos.setdefault(k, {"key": k, "cost_usd": 0.0, "on_demand_usd": 0.0,
                                         "total_tokens": 0,
                                         "chats": 0, "titles": [], "created": False,
                                         "role": "mentioned", "est": False,
                                         "source": s.get("repo_source") or ""})
                e["cost_usd"] += cost * frac
                e["on_demand_usd"] += od * frac
                e["total_tokens"] += int(toks * frac)
                e["chats"] = round(e["chats"] + frac, 4)
                e["est"] = e["est"] or bool(s.get("est"))
                if len(e["titles"]) < 5:
                    e["titles"].append(s["title"])
                if it.get("role") == "primary":
                    e["role"] = "primary"
                elif it.get("role") == "inferred" and e["role"] != "primary":
                    e["role"] = "inferred"
    srt = lambda d: sorted(d.values(), key=lambda x: -x["cost_usd"])
    sess = [{"key": s["title"], "cost_usd": s["cost_usd"] or 0,
             "on_demand_usd": s.get("on_demand_usd") or 0,
             "total_tokens": s["total_tokens"] or 0, "chats": s["turns"] or 0,
             "titles": [x for x in [s["repository"]] if x],
             "created": False, "role": "mentioned", "est": bool(s.get("est")),
             "session_id": s["session_id"],
             "cloud_agent": _is_cloud_session(s),
             "cloud_url": s.get("cloud_url") or "",
             "origin": _session_origin(s)}
            for s in sessions]
    sess.sort(key=lambda x: -x["cost_usd"])
    titles = {s["session_id"]: s["title"] for s in sessions}
    billed = any(s.get("billed") for s in sessions)
    if billed:
        prs_map = {}
        for s in sessions:
            r = refs.get(s["session_id"]) or {}
            entries = _pr_cost_entries(s, r, turn_prs, turn_cost)
            if not entries:
                continue
            cost, toks = s["cost_usd"] or 0, s["total_tokens"] or 0
            od = s.get("on_demand_usd") or 0
            for it, share_c in entries:
                frac = share_c / cost if cost else 0
                share_t = int(toks * frac)
                share_od = od * frac
                k = it["key"] if isinstance(it, dict) else it
                e = prs_map.setdefault(k, {"key": k, "cost_usd": 0.0, "on_demand_usd": 0.0,
                                           "total_tokens": 0,
                                           "chats": 0, "titles": [], "created": False,
                                           "role": "mentioned", "est": False})
                e["cost_usd"] += share_c
                e["on_demand_usd"] += share_od
                e["total_tokens"] += share_t
                e["chats"] += 1
                if len(e["titles"]) < 5:
                    e["titles"].append(s["title"])
                if isinstance(it, dict) and it.get("created"):
                    e["created"] = True
                if isinstance(it, dict) and it.get("first_day"):
                    prev = e.get("first_day") or it["first_day"]
                    e["first_day"] = min(prev, it["first_day"])
                    e["last_day"] = max(e.get("last_day") or it.get("last_day") or it["first_day"],
                                       it.get("last_day") or it["first_day"])
                if isinstance(it, dict) and it.get("inferred"):
                    e["inferred"] = True
        prs = sorted(prs_map.values(), key=lambda x: (-x["cost_usd"], x["key"]))
    else:
        prs = pr_segments(titles, turn_prs, turn_cost)
    out = {"jira": srt(jira), "prs": prs, "repos": srt(repos), "sessions": sess}
    out["unattributed"] = _unattributed(sessions, refs, out)
    return out


def _public_session(s):
    skip = {"days", "by_model_day", "by_model", "text"}
    return {k: v for k, v in s.items() if k not in skip}


def _aggregate(sessions):
    models = collections.defaultdict(lambda: collections.Counter())
    daily = collections.defaultdict(lambda: collections.Counter())
    for s in sessions:
        for name, c in (s.get("by_model") or {}).items():
            m = models[name]
            for key, val in c.items():
                m[key] += val
            m["est"] = m["est"] or (1 if s.get("est") else 0)
        for day, c in (s.get("days") or {}).items():
            d = daily[day]
            for key, val in c.items():
                d[key] += val
            d["sessions"] += 1
    model_rows = sorted(
        ({"model": k,
          "requests": int(c["requests"]),
          "input_tokens": round(c["input_tokens"]),
          "output_tokens": round(c["output_tokens"]),
          "cache_read_tokens": round(c["cache_read_tokens"]),
          "cache_write_tokens": round(c["cache_write_tokens"]),
          "cost_usd": c["cost_usd"],
          "on_demand_usd": c["on_demand_usd"],
          "est": bool(c["est"]),
          "pool": _model_pool(k),
          "pool_label": POOL_LABELS.get(_model_pool(k), "Other Models"),
          "known_rate": True if any(s.get("billed") for s in sessions) else _known_rate(k)}
         for k, c in models.items()),
        key=lambda m: -m["cost_usd"])
    daily_rows = [{"day": d, "requests": int(c["requests"]),
                   "sessions": int(c["sessions"]),
                   "total_tokens": round(c["total_tokens"]),
                   "cost_usd": c["cost_usd"],
                   "est_usd": c["est_usd"],
                   "on_demand_usd": c["on_demand_usd"]}
                  for d, c in sorted(daily.items())]
    return model_rows, daily_rows


def _fmt_day(d):
    if os.name == "nt":
        return d.strftime("%b %#d, %Y")
    return d.strftime("%b %-d, %Y")


def _billing_cycles(data):
    """Current and previous billing cycle date ranges from usage-summary."""
    summary = data.get("billing_summary") or {}
    cur_start = _local_day(summary.get("billingCycleStart"))
    cur_end = _local_day(summary.get("billingCycleEnd"))
    if not cur_start:
        return None
    out = {"current": {"start": cur_start, "end": cur_end or cur_start}}
    try:
        cs = datetime.date.fromisoformat(cur_start)
        ce = datetime.date.fromisoformat(cur_end) if cur_end else cs
        span = max(1, (ce - cs).days)
        last_end = cs - datetime.timedelta(days=1)
        last_start = last_end - datetime.timedelta(days=span - 1)
        out["last"] = {"start": last_start.isoformat(), "end": last_end.isoformat()}
    except ValueError:
        pass
    return out


def _cycle_historical(sessions, start, end, plan="", included_limit=0.0):
    """Closed-cycle totals from billed events (no live allowance API)."""
    rows = [c for c in (_clip(s, start, end) for s in sessions if s.get("billed")) if c]
    od = sum(s.get("on_demand_usd") or 0 for s in rows)
    cost = sum(s["cost_usd"] or 0 for s in rows)
    included = max(0.0, cost - od)
    return {
        "which": "last",
        "start": start,
        "end": end,
        "billed": True,
        "historical": True,
        "plan": plan,
        "sessions": len(rows),
        "requests": sum(s["requests"] or 0 for s in rows),
        "total_tokens": sum(s["total_tokens"] or 0 for s in rows),
        "cost_usd": round(cost, 4),
        "included_usd": round(included, 4),
        "on_demand_usd": round(od, 4),
        "included_limit": included_limit,
        "included_remaining": max(0.0, included_limit - included) if included_limit else None,
        "bonus_usd": 0.0,
        "on_demand_limit": None,
        "on_demand_remaining": None,
        "budget": included_limit,
        "pct": (included / included_limit * 100) if included_limit else None,
        "remaining": max(0.0, included_limit - included) if included_limit else None,
    }


def _cycle_mtd(data, today):
    """Current Cursor billing cycle from usage-summary + get-current-period-usage."""
    summary = data.get("billing_summary") or {}
    period = data.get("billing_period") or {}
    plan = (summary.get("individualUsage") or {}).get("plan") or {}
    on_demand = (summary.get("individualUsage") or {}).get("onDemand") or {}
    plan_usage = period.get("planUsage") or {}
    breakdown = plan.get("breakdown") or {}
    start_s = _local_day(summary.get("billingCycleStart"))
    end_s = _local_day(summary.get("billingCycleEnd"))
    try:
        end_d = datetime.date.fromisoformat(end_s) if end_s else (
            today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    except ValueError:
        end_d = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    included_limit = _cents_usd(plan.get("limit") or plan_usage.get("limit"))
    included_used = _cents_usd(plan.get("used") or plan_usage.get("includedSpend"))
    included_remaining = _cents_usd(plan.get("remaining"))
    if plan.get("remaining") is None and included_limit:
        included_remaining = max(0.0, included_limit - included_used)
    bonus_usd = _cents_usd(breakdown.get("bonus") or plan_usage.get("bonusSpend"))
    plan_total_usd = _cents_usd(breakdown.get("total") or plan_usage.get("totalSpend"))
    od_used = _cents_usd(on_demand.get("used"))
    od_limit = _cents_usd(on_demand.get("limit") or (
        (period.get("spendLimitUsage") or {}).get("individualLimit")))
    od_remaining = _cents_usd(on_demand.get("remaining") or (
        (period.get("spendLimitUsage") or {}).get("individualRemaining")))
    budget = CREDIT_BUDGET if _BUDGET_OVERRIDDEN else (included_limit or CREDIT_BUDGET)
    mtd_sessions = [c for c in (_clip(s, start_s or MIN_DAY, end_s or MAX_DAY)
                                for s in data["sessions"] if s.get("billed")) if c] if start_s else []
    cycle_tokens = sum(s["total_tokens"] or 0 for s in mtd_sessions)
    cycle_models = list(data.get("billing_aggregations") or [])
    if not cycle_models and mtd_sessions:
        cycle_models, _ = _aggregate(mtd_sessions)
    spend = _pool_spend(cycle_models)
    util = _model_utilization(
        summary, period,
        included_used=included_used,
        included_limit=included_limit,
        pool_spend=spend,
    )
    # Last-resort total bar so the section appears whenever plan allowance does.
    if not util["pools"] and included_limit and included_limit > 0:
        pct = (float(included_used or 0) / float(included_limit) * 100.0)
        row = _pool_row(
            "total", "Total included", "Subscription included compute",
            pct, "Derived from included spend ÷ plan limit", False)
        if row:
            util["pools"].append(row)
            util["total_pct"] = pct
    for pool in util["pools"]:
        if pool["id"] in spend:
            pool["metered_usd"] = spend[pool["id"]]
    return {
        "which": "current",
        "month": f"{start_s} → {end_s}" if start_s else today.strftime("%Y-%m"),
        "start": start_s,
        "end": end_s,
        "billed": True,
        "plan": summary.get("membershipType") or "",
        "requests": sum(s["requests"] or 0 for s in mtd_sessions),
        "sessions": len(mtd_sessions),
        "total_tokens": cycle_tokens,
        "cost_usd": round(included_used + bonus_usd + od_used, 4),
        "included_usd": included_used,
        "included_limit": included_limit,
        "included_remaining": included_remaining,
        "bonus_usd": bonus_usd,
        "plan_total_usd": plan_total_usd or round(included_used + bonus_usd, 4),
        "on_demand_usd": od_used,
        "on_demand_limit": od_limit,
        "on_demand_remaining": od_remaining,
        "auto_pct": util.get("auto_pct"),
        "api_pct": util.get("api_pct"),
        "total_pct": util.get("total_pct"),
        "auto_msg": util.get("auto_msg") or "",
        "named_msg": util.get("named_msg") or "",
        "display_msg": util.get("display_msg") or "",
        "unlimited": util.get("unlimited"),
        "pools": util.get("pools") or [],
        "pool_spend": spend,
        "cycle_models": cycle_models,
        "on_demand_enabled": util.get("on_demand_enabled"),
        "budget": budget,
        "pct": (included_used / included_limit * 100) if included_limit else None,
        "remaining": included_remaining,
        "reset_date": _fmt_day(end_d),
        "days_left": max(0, (end_d - today).days),
    }


def _range_allowance(billed_rows, start, end, mtd, cycles, range_is_current_cycle,
                       range_is_last_cycle=False, plan=""):
    """Allowance vs on-demand metered in the selected date range."""
    rows = billed_rows or []
    cost = sum(s.get("cost_usd") or 0 for s in rows)
    od = sum(s.get("on_demand_usd") or 0 for s in rows)
    inc = max(0.0, cost - od)
    cloud_rows = [s for s in rows if _is_cloud_session(s)]
    local_rows = [s for s in rows if not _is_cloud_session(s)]
    out = {
        "start": start,
        "end": end,
        "cost_usd": round(cost, 4),
        "included_usd": round(inc, 4),
        "on_demand_usd": round(od, 4),
        "sessions": len(rows),
        "cloud_sessions": len(cloud_rows),
        "local_ide_sessions": len(local_rows),
        "cloud_usd": round(sum(s.get("cost_usd") or 0 for s in cloud_rows), 4),
        "local_ide_usd": round(sum(s.get("cost_usd") or 0 for s in local_rows), 4),
        "requests": sum(s.get("requests") or 0 for s in rows),
        "total_tokens": sum(s.get("total_tokens") or 0 for s in rows),
        "plan": plan,
        "range_is_current_cycle": range_is_current_cycle,
        "range_is_last_cycle": range_is_last_cycle,
    }
    limit = (mtd or {}).get("included_limit") or (mtd or {}).get("budget") or 0.0
    if range_is_current_cycle and mtd:
        out["included_limit"] = limit
        out["included_remaining"] = mtd.get("included_remaining")
        out["cycle_included_used"] = mtd.get("included_usd")
        out["on_demand_limit"] = mtd.get("on_demand_limit")
        out["on_demand_remaining"] = mtd.get("on_demand_remaining")
        out["bonus_usd"] = mtd.get("bonus_usd")
        out["cycle_start"] = mtd.get("start")
        out["cycle_end"] = mtd.get("end")
        out["reset_date"] = mtd.get("reset_date")
        out["days_left"] = mtd.get("days_left")
        if limit:
            out["pct_of_cycle_limit"] = round(
                (mtd.get("included_usd") or 0) / limit * 100, 1)
    elif range_is_last_cycle and limit:
        out["included_limit"] = limit
        out["historical"] = True
        if limit:
            out["pct_of_cycle_limit"] = round(inc / limit * 100, 1)
    return out


def _summarize_subscriptions(sub_lines):
    """Roll up plan-fee invoices to one row per account + plan."""
    groups = collections.OrderedDict()
    for inv in sub_lines:
        acct = (inv.get("account") or "").strip() or "account"
        plan = inv.get("plan") or "Subscription"
        key = (acct.lower(), plan)
        g = groups.setdefault(key, {
            "account": acct,
            "plan": plan,
            "count": 0,
            "net_usd": 0.0,
            "first_day": inv.get("day") or "",
            "last_day": inv.get("day") or "",
        })
        net = inv.get("net_usd") or 0
        if net > 0.004:
            g["count"] += 1
            g["net_usd"] = round(g["net_usd"] + net, 4)
        day = inv.get("day") or ""
        if day:
            if not g["first_day"] or day < g["first_day"]:
                g["first_day"] = day
            if not g["last_day"] or day > g["last_day"]:
                g["last_day"] = day
    return [g for g in groups.values() if g["count"] > 0]


def _by_account_totals(sub_lines, usage_inv, billed_rows):
    buckets = collections.defaultdict(lambda: {
        "subscription_usd": 0.0,
        "on_demand_invoiced_usd": 0.0,
        "metered_usd": 0.0,
        "included_usd": 0.0,
        "on_demand_metered_usd": 0.0,
    })
    for inv in sub_lines:
        acct = (inv.get("account") or "").strip() or "unknown"
        buckets[acct]["subscription_usd"] += inv.get("net_usd") or 0
    for inv in usage_inv:
        acct = (inv.get("account") or "").strip() or "unknown"
        buckets[acct]["on_demand_invoiced_usd"] += inv.get("net_usd") or 0
    for row in billed_rows:
        acct = (row.get("account_label") or "").strip() or "unknown"
        cost = row.get("cost_usd") or 0
        od = row.get("on_demand_usd") or 0
        buckets[acct]["metered_usd"] += cost
        buckets[acct]["on_demand_metered_usd"] += od
        buckets[acct]["included_usd"] += max(0.0, cost - od)
    out = []
    for acct, b in sorted(buckets.items()):
        sub = round(b["subscription_usd"], 4)
        od_inv = round(b["on_demand_invoiced_usd"], 4)
        out.append({
            "account": acct,
            "subscription_usd": sub,
            "on_demand_invoiced_usd": od_inv,
            "cash_usd": round(sub + od_inv, 4),
            "metered_usd": round(b["metered_usd"], 4),
            "included_usd": round(b["included_usd"], 4),
            "on_demand_metered_usd": round(b["on_demand_metered_usd"], 4),
        })
    return out


def build_payload(start, end, q="", force=False):
    data = scan_cursor(force=force)
    billed = bool(data.get("billed"))
    sessions = []
    for full in data["sessions"]:
        clipped = _clip(full, start, end)
        if clipped:
            sessions.append(clipped)
    q = (q or "").strip().lower()
    if q:
        sessions = [s for s in sessions if _matches(s, q)]
    billed_rows = [s for s in sessions if s.get("billed")] if billed else sessions
    other_rows = [s for s in sessions if billed and not s.get("billed")]
    models, daily = _aggregate(billed_rows)
    refs = {s["session_id"]: s.get("refs") or data["refs"].get(s["session_id"],
            {"jira": [], "prs": [], "repos": []}) for s in billed_rows}
    keep = {s["session_id"] for s in billed_rows}
    turn_prs = {k: v for k, v in data["turn_prs"].items() if k in keep}
    turn_cost = {k: v for k, v in data["turn_cost"].items() if k in keep}
    on_demand = sum(s.get("on_demand_usd") or 0 for s in billed_rows)
    cloud_rows = [s for s in billed_rows if _is_cloud_session(s)]
    local_rows = [s for s in billed_rows if not _is_cloud_session(s)]
    totals = {
        "cost_usd": sum(s["cost_usd"] or 0 for s in billed_rows),
        "est_usd": 0.0 if billed else sum(s.get("est_usd") or 0 for s in billed_rows),
        "on_demand_usd": on_demand,
        "included_usd": 0.0,
        "measured_usd": 0.0,
        "total_tokens": sum(s["total_tokens"] or 0 for s in billed_rows),
        "input_tokens": sum(s["input_tokens"] or 0 for s in billed_rows),
        "output_tokens": sum(s["output_tokens"] or 0 for s in billed_rows),
        "cache_read_tokens": sum(s["cache_read_tokens"] or 0 for s in billed_rows),
        "cache_write_tokens": sum(s["cache_write_tokens"] or 0 for s in billed_rows),
        "measured_tokens": sum(s.get("measured_tokens") or 0 for s in billed_rows),
        "requests": sum(s["requests"] or 0 for s in billed_rows),
        "sessions": len(billed_rows),
        "cloud_sessions": len(cloud_rows),
        "local_ide_sessions": len(local_rows),
        "cloud_usd": round(sum(s["cost_usd"] or 0 for s in cloud_rows), 4),
        "local_ide_usd": round(sum(s["cost_usd"] or 0 for s in local_rows), 4),
        "other_sessions": len(other_rows),
        "other_est_usd": sum(s["cost_usd"] or 0 for s in other_rows),
    }
    totals["included_usd"] = max(0.0, totals["cost_usd"] - on_demand) if billed else (
        totals["cost_usd"] - totals["est_usd"])
    totals["measured_usd"] = totals["included_usd"]
    sub_lines, usage_inv = [], []
    inv_cycle_starts = sorted({
        inv.get("usage_cycle_start") or _invoice_usage_cycle_start(inv)
        for inv in data.get("invoices") or []
        if inv.get("kind") == "usage" and (
            inv.get("usage_cycle_start") or _invoice_usage_cycle_start(inv))
    })
    bc = _billing_cycles(data) if billed else None
    if bc:
        for key in ("current", "last"):
            s = (bc.get(key) or {}).get("start")
            if s:
                inv_cycle_starts.append(s)
    inv_cycle_starts = sorted(set(inv_cycle_starts))
    for inv in data.get("invoices") or []:
        day = inv.get("day") or ""
        if inv.get("kind") == "subscription":
            if day and start <= day <= end:
                sub_lines.append(inv)
        elif inv.get("kind") == "usage" and _usage_invoice_in_view(
                inv, start, end, inv_cycle_starts):
            usage_inv.append(inv)
    sub_lines.sort(key=lambda i: i.get("day") or "")
    totals["subscription_usd"] = round(sum(i.get("net_usd") or 0 for i in sub_lines), 4)
    totals["subscription_lines"] = sub_lines
    totals["subscription_summary"] = _summarize_subscriptions(sub_lines)
    totals["invoice_usage_usd"] = round(sum(i.get("net_usd") or 0 for i in usage_inv), 4)
    totals["cash_usd"] = round(totals["subscription_usd"] + totals["invoice_usage_usd"], 4)
    totals["metered_usd"] = round(totals["cost_usd"], 4)
    totals["grand_usd"] = totals["cash_usd"]
    totals["by_account"] = _by_account_totals(sub_lines, usage_inv, billed_rows)
    totals["unattributed_usd"] = round(
        sum(s["cost_usd"] or 0 for s in billed_rows if s.get("unattributed")), 4)
    totals["orphan_billed_usd"] = round(
        sum(s["cost_usd"] or 0 for s in billed_rows if s.get("orphan_billed")), 4)
    legacy = next((a for a in totals["by_account"]
                   if a["account"].lower() == LEGACY_INVOICE_EMAIL.lower()), None)
    if legacy:
        totals["legacy_compare"] = {
            "email": LEGACY_INVOICE_EMAIL,
            "reported_usd": LEGACY_INVOICE_TOTAL,
            "api_cash_usd": legacy["cash_usd"],
            "metered_usd": legacy["metered_usd"],
            "subscription_usd": legacy["subscription_usd"],
            "on_demand_invoiced_usd": legacy["on_demand_invoiced_usd"],
        }
    bounds_days = [s["first_day"] for s in data["sessions"] if s.get("first_day")] + \
                  [s["last_day"] for s in data["sessions"] if s.get("last_day")]
    today = datetime.date.today()
    cycles = _billing_cycles(data) if billed else None
    if billed:
        mtd = _cycle_mtd(data, today)
        view_cycle = dict(mtd)
        range_is_last_cycle = False
        if cycles and cycles.get("last"):
            last = cycles["last"]
            if start == last["start"] and end == last["end"]:
                range_is_last_cycle = True
                view_cycle = _cycle_historical(
                    data["sessions"], last["start"], last["end"],
                    plan=mtd.get("plan") or "",
                    included_limit=mtd.get("included_limit") or 0.0)
    else:
        month = today.strftime("%Y-%m")
        mtd_sessions = [c for c in (_clip(s, month + "-01", MAX_DAY)
                                    for s in data["sessions"]) if c]
        mtd_cost = sum(s["cost_usd"] or 0 for s in mtd_sessions)
        reset = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        mtd = {"which": "current", "month": month, "start": month + "-01", "end": today.isoformat(),
               "billed": False,
               "requests": sum(s["requests"] or 0 for s in mtd_sessions),
               "sessions": len(mtd_sessions),
               "total_tokens": sum(s["total_tokens"] or 0 for s in mtd_sessions),
               "cost_usd": mtd_cost,
               "budget": CREDIT_BUDGET,
               "pct": (mtd_cost / CREDIT_BUDGET * 100) if CREDIT_BUDGET else None,
               "remaining": (CREDIT_BUDGET - mtd_cost) if CREDIT_BUDGET else None,
               "reset_date": _fmt_day(reset),
               "days_left": (reset - today).days}
        view_cycle = mtd
        range_is_last_cycle = False
    if billed:
        range_is_current_cycle = bool(
            cycles and start == (cycles.get("current") or {}).get("start")
            and end == (cycles.get("current") or {}).get("end"))
        range_allowance = _range_allowance(
            billed_rows, start, end, mtd, cycles, range_is_current_cycle,
            range_is_last_cycle=range_is_last_cycle,
            plan=(mtd or {}).get("plan") or "")
    else:
        range_is_current_cycle = bool(
            mtd.get("start") and start == mtd["start"]
            and end <= (mtd.get("end") or MAX_DAY))
        range_allowance = None
    pub = [_public_session(s) for s in (billed_rows + other_rows)]
    return {
        "totals": totals,
        "sessions": pub,
        "models": models,
        "daily": daily,
        "mtd": mtd,
        "view_cycle": view_cycle,
        "cycles": cycles,
        "range_is_current_cycle": range_is_current_cycle,
        "range_allowance": range_allowance,
        "cycle_models": (mtd or {}).get("cycle_models") or [],
        "rollup": _enrich_jira_rollup(rollup(billed_rows, refs, turn_prs, turn_cost,
                                            data.get("git_pr_catalog"),
                                            data.get("path_by_github"))),
        "jira_base": JIRA_BASE,
        "jira_connected": bool(_jira_credentials()[0]),
        "jira_error": _JIRA_LAST_ERROR,
        "q": q,
        "range": {"start": start, "end": end},
        "bounds": {"min": min(bounds_days) if bounds_days else None,
                   "max": max(bounds_days) if bounds_days else None},
        "db": DB_PATH,
        "billed": billed,
        "billing_error": data.get("billing_error"),
        "billing_email": data.get("billing_email") or "",
        "billing_emails": data.get("billing_emails") or [],
        "previous_email": data.get("previous_email") or "",
        "plan": (data.get("billing_summary") or {}).get("membershipType") or "",
        "cloud_agents": data.get("cloud_agents") or 0,
        "cloud_agents_error": data.get("cloud_agents_error") or "",
        "cloud_agents_source": data.get("cloud_agents_source") or "",
        "rates": {k: {"input": a, "output": b, "cache_read": c}
                  for k, (a, b, c) in MODEL_RATES.items()},
        "chars_per_token": CHARS_PER_TOKEN,
        "context_window": CONTEXT_WINDOW_TOKENS,
    }


def _state_load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _state_save(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        DIGEST_STATUS["last_error"] = f"state write failed: {exc}"


def _digest_enabled(state=None):
    """Automatic digests need a recipient *and* the switch left on."""
    if not EMAIL_TO:
        return False
    if state is None:
        state = _state_load()
    return bool(state.get("digest_enabled", True))


def budget_set(value):
    global CREDIT_BUDGET, _BUDGET_OVERRIDDEN
    try:
        n = float(str(value).replace(",", "").replace("_", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return CREDIT_BUDGET
    n = max(0.0, n)
    CREDIT_BUDGET = n
    _BUDGET_OVERRIDDEN = True
    with DIGEST_LOCK:
        state = _state_load()
        state["monthly_budget_usd"] = n
        _state_save(state)
    return n


def digest_set_enabled(on):
    """Persist the on/off switch so it survives a restart."""
    with DIGEST_LOCK:
        state = _state_load()
        state["digest_enabled"] = bool(on)
        if on:
            # Re-arm today's guard, so switching back on part-way through a day
            # still delivers that day's digest instead of silently skipping it.
            state.pop("last_check_day", None)
        _state_save(state)


def _gmail_app_dir():
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(appdata, "cursor-dashboard")


def _gmail_credentials_path():
    candidates = []
    if GMAIL_CREDENTIALS:
        candidates.append(os.path.expandvars(os.path.expanduser(GMAIL_CREDENTIALS)))
    appdir = _gmail_app_dir()
    candidates += [
        os.path.join(appdir, "gmail-oauth-client.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmail-oauth-client.json"),
        os.path.join(os.path.dirname(STATE_PATH), "gmail-oauth-client.json"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return candidates[0]


def _gmail_token_file():
    if GMAIL_TOKEN_PATH:
        return os.path.expandvars(os.path.expanduser(GMAIL_TOKEN_PATH))
    cred = _gmail_credentials_path()
    if cred and os.path.isfile(cred):
        return os.path.join(os.path.dirname(cred), "gmail-oauth-token.json")
    return os.path.join(_gmail_app_dir(), "gmail-oauth-token.json")


def _gmail_setup_hint():
    dest = os.path.join(_gmail_app_dir(), "gmail-oauth-client.json")
    return (
        "Gmail API is not set up. One-time steps:\n"
        "  1. https://console.cloud.google.com/apis/library/gmail.googleapis.com — enable Gmail API\n"
        "  2. APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app\n"
        "  3. Download the JSON and save it as:\n"
        f"       {dest}\n"
        "  4. If the consent screen is in Testing, add your Gmail as a test user\n"
        "  5. py -3 cursor_dashboard.py --gmail-auth"
    )


def _gmail_load_client():
    path = _gmail_credentials_path()
    if not path or not os.path.isfile(path):
        raise RuntimeError(_gmail_setup_hint())
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = raw.get("installed") or raw.get("web") or raw
    if not cfg.get("client_id"):
        raise RuntimeError(f"No client_id in {path}. Download a Desktop OAuth client JSON from Google Cloud.")
    return cfg, path


def _gmail_token_load():
    path = _gmail_token_file()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _gmail_token_save(token):
    path = _gmail_token_file()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(token, fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _gmail_http(method, url, data=None, headers=None, form=False):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        snippet = exc.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"Gmail API {exc.code}: {snippet}") from exc
    return json.loads(raw) if raw else {}


def _gmail_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _gmail_wait_for_code(httpd, timeout=180):
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items() if v})
            ok = "code" in result and not result.get("error")
            msg = ("Gmail access granted. You can close this tab and return to the dashboard."
                   if ok else "Authorization failed: " + (result.get("error") or "unknown"))
            body = (f"<html><body style='font-family:sans-serif;padding:2rem'>"
                    f"<h2>Cursor dashboard</h2><p>{msg}</p></body></html>").encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd.RequestHandlerClass = Handler
    httpd.timeout = timeout
    httpd.handle_request()
    return result


def gmail_auth(force_consent=True):
    """Open a browser for Gmail OAuth and store a refresh token on disk."""
    cfg, cred_path = _gmail_load_client()
    httpd = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    verifier, challenge = _gmail_pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if force_consent:
        params["prompt"] = "consent"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    print(f"  opening browser for Gmail OAuth (client {cred_path})", flush=True)
    print(f"  if it does not open: {auth_url}", flush=True)
    webbrowser.open(auth_url)
    result = _gmail_wait_for_code(httpd)
    httpd.server_close()
    if result.get("error"):
        raise RuntimeError(f"Gmail OAuth denied: {result.get('error_description') or result['error']}")
    if result.get("state") != state:
        raise RuntimeError("Gmail OAuth state mismatch — try --gmail-auth again.")
    code = result.get("code")
    if not code:
        raise RuntimeError("Gmail OAuth timed out or returned no code. Run --gmail-auth again.")
    token = _gmail_http("POST", cfg.get("token_uri") or "https://oauth2.googleapis.com/token", {
        "client_id": cfg["client_id"],
        **({"client_secret": cfg["client_secret"]} if cfg.get("client_secret") else {}),
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, form=True)
    if not token.get("refresh_token"):
        prev = _gmail_token_load()
        if prev.get("refresh_token"):
            token["refresh_token"] = prev["refresh_token"]
        else:
            raise RuntimeError(
                "Google did not return a refresh token. Re-run with --gmail-auth "
                "(prompt=consent) and make sure you tick the Gmail send permission.")
    token["obtained_at"] = int(time.time())
    token["expiry"] = int(time.time()) + int(token.get("expires_in") or 3600) - 60
    token["email"] = _gmail_profile_email(token.get("access_token"))
    _gmail_token_save({k: token[k] for k in token if k != "id_token"})
    print(f"  Gmail authorized as {token['email']}", flush=True)
    return token


def _gmail_profile_email(access_token):
    if not access_token:
        return ""
    try:
        profile = _gmail_http(
            "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"})
        return (profile.get("emailAddress") or "").strip()
    except Exception:
        return ""


def _gmail_refresh(cfg, token):
    refresh = token.get("refresh_token")
    if not refresh:
        raise RuntimeError("Gmail token has no refresh_token. Run: py -3 cursor_dashboard.py --gmail-auth")
    fresh = _gmail_http("POST", cfg.get("token_uri") or "https://oauth2.googleapis.com/token", {
        "client_id": cfg["client_id"],
        **({"client_secret": cfg["client_secret"]} if cfg.get("client_secret") else {}),
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }, form=True)
    token.update(fresh)
    token["refresh_token"] = refresh
    token["obtained_at"] = int(time.time())
    token["expiry"] = int(time.time()) + int(fresh.get("expires_in") or 3600) - 60
    if not token.get("email"):
        token["email"] = _gmail_profile_email(token.get("access_token"))
    _gmail_token_save({k: token[k] for k in token if k != "id_token"})
    return token


def _gmail_access_token(interactive=False):
    cfg, _ = _gmail_load_client()
    token = _gmail_token_load()
    if not token.get("refresh_token") and not token.get("access_token"):
        if not interactive:
            raise RuntimeError("Gmail is not authorized. Run: py -3 cursor_dashboard.py --gmail-auth")
        token = gmail_auth()
    expiry = int(token.get("expiry") or 0)
    if token.get("access_token") and expiry > time.time() + 30:
        return token["access_token"], token
    token = _gmail_refresh(cfg, token)
    return token["access_token"], token


def _gmail_ready():
    token = _gmail_token_load()
    email = (token.get("email") or "").strip()
    return bool(token.get("refresh_token") or token.get("access_token")), email


def _gmail_send_message(msg, interactive=False):
    access, token = _gmail_access_token(interactive=interactive)
    sender = (token.get("email") or _gmail_profile_email(access) or "").strip()
    if sender:
        if msg.get("From"):
            msg.replace_header("From", sender)
        else:
            msg["From"] = sender
    raw = base64.urlsafe_b64encode(bytes(msg)).decode("ascii")
    headers = {"Authorization": f"Bearer {access}"}
    try:
        return _gmail_http(
            "POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            {"raw": raw}, headers=headers)
    except RuntimeError as exc:
        if "401" not in str(exc):
            raise
        cfg, _ = _gmail_load_client()
        token = _gmail_refresh(cfg, _gmail_token_load())
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        return _gmail_http(
            "POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            {"raw": raw}, headers=headers)


def _digest_from():
    """Sender: Gmail account that authorized, else an explicit --email-from."""
    if EMAIL_FROM:
        return EMAIL_FROM
    _, email = _gmail_ready()
    if email:
        return email
    domain = EMAIL_TO.split("@")[-1] if "@" in EMAIL_TO else "gmail.com"
    return f"cursor-dashboard@{domain}"


def _money(v):
    return f"${v:,.2f}"


def _find_digest_day(before):
    """The most recent day with activity strictly before ``before``.

    Deliberately not "yesterday": after a weekend or time off the digest should
    still report the last day actually worked, rather than mailing an empty day
    or silently skipping it.
    """
    lo = (datetime.date.fromisoformat(before)
          - datetime.timedelta(days=DIGEST_LOOKBACK_DAYS)).isoformat()
    hi = (datetime.date.fromisoformat(before) - datetime.timedelta(days=1)).isoformat()
    if hi < lo:
        return None, None
    recent = build_payload(lo, hi, "")
    active = [d for d in recent.get("daily", []) if (d.get("requests") or 0) > 0]
    if not active:
        return None, recent
    return max(d["day"] for d in active), recent


def _digest_data(day, recent=None):
    """Everything the email needs: the day itself plus trailing context."""
    p = build_payload(day, day, "")
    if recent is None:
        lo = (datetime.date.fromisoformat(day)
              - datetime.timedelta(days=DIGEST_LOOKBACK_DAYS)).isoformat()
        recent = build_payload(lo, day, "")
    days = {d["day"]: d for d in recent.get("daily", []) if (d.get("requests") or 0) > 0}
    prior = [d for d in sorted(days) if d < day]
    # Compare like with like: an average over active days only, so a week off
    # does not make an ordinary day look like a spike.
    window = prior[-DIGEST_AVG_DAYS:]
    avg = (sum(days[d]["cost_usd"] for d in window) / len(window)) if window else None
    return {
        "day": day,
        "payload": p,
        "prev_day": prior[-1] if prior else None,
        "prev_cost": days[prior[-1]]["cost_usd"] if prior else None,
        "avg_cost": avg,
        "avg_days": len(window),
    }


def _delta_note(cost, ref, label):
    if not ref:
        return ""
    pct = (cost - ref) / ref * 100 if ref else 0
    arrow = "▲" if pct >= 0 else "▼"
    return f"{arrow} {abs(pct):,.0f}% vs {label} ({_money(ref)})"


def _digest_subject(d):
    t = d["payload"]["totals"]
    pretty = datetime.date.fromisoformat(d["day"]).strftime("%a %b %d")
    return (f"Cursor digest — {pretty}: {_money(t['cost_usd'])}, "
            f"{t['requests']:,} requests")


def _rows(items, n=5):
    return [i for i in (items or [])][:n]


def _digest_sessions(p):
    rows = p.get("sessions") or []
    if p.get("billed"):
        rows = [s for s in rows if s.get("billed") is not False]
    return rows


def _digest_html(d):
    p = d["payload"]
    t = p["totals"]
    day_pretty = datetime.date.fromisoformat(d["day"]).strftime("%A, %B %d, %Y")
    billed = bool(p.get("billed"))
    email = p.get("billing_email") or EMAIL_TO
    incl = t.get("included_usd") or 0
    od = t.get("on_demand_usd") or 0

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def table(title, rows, cols):
        if not rows:
            return ""
        head = "".join(f"<th align='{a}'>{esc(c)}</th>" for c, a, _ in cols)
        body = ""
        for r in rows:
            body += "<tr>" + "".join(
                f"<td align='{a}' style='padding:4px 10px;border-top:1px solid #e6e8eb'>"
                f"{fn(r)}</td>" for _, a, fn in cols) + "</tr>"
        return (f"<h3 style='margin:22px 0 6px;font-size:14px;color:#57606a'>{esc(title)}</h3>"
                f"<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;"
                f"font-size:13px;width:100%'><tr style='color:#57606a;font-size:11px;"
                f"text-transform:uppercase;letter-spacing:.4px'>{head}</tr>{body}</table>")

    deltas = " &nbsp;·&nbsp; ".join(x for x in (
        _delta_note(t["cost_usd"], d["prev_cost"], f"prior active day ({d['prev_day']})"),
        _delta_note(t["cost_usd"], d["avg_cost"], f"{d['avg_days']}-day avg"),
    ) if x)

    cards = [("Spend", _money(t["cost_usd"]))]
    if billed:
        cards += [("Included", _money(incl)), ("On-demand", _money(od))]
        mtd = p.get("mtd") or {}
        if mtd.get("auto_pct") is not None:
            cards.append(("Cursor Models", f"{mtd['auto_pct']:.0f}% used"))
        if mtd.get("api_pct") is not None:
            cards.append(("Other Models", f"{mtd['api_pct']:.0f}% used"))
    cards += [("Requests", f"{t['requests']:,}"),
              ("Chats", f"{t['sessions']:,}"),
              ("Tokens", f"{(t.get('total_tokens') or 0):,}")]
    card_html = "".join(
        f"<td style='padding:10px 14px;background:#f6f8fa;border-radius:8px'>"
        f"<div style='font-size:11px;color:#57606a;text-transform:uppercase;"
        f"letter-spacing:.4px'>{esc(k)}</div>"
        f"<div style='font-size:19px;font-weight:600;color:#1f2328'>{esc(v)}</div></td>"
        f"<td style='width:8px'></td>" for k, v in cards)

    ro = p.get("rollup") or {}
    parts = [
        table("Cost by model", _rows(p.get("models")), [
            ("Model", "left", lambda r: esc(r["model"]) + (
                " <span style='color:#9a6700;font-size:10px'>EST</span>"
                if r.get("est") else "")),
            ("Requests", "right", lambda r: f"{r['requests']:,}"),
            ("Cost", "right", lambda r: _money(r["cost_usd"]))]),
        table("Top Jira tickets", _rows(ro.get("jira")), [
            ("Ticket", "left", lambda r: esc(r["key"])
             + (f"<div style='font-size:11px;color:#57606a;margin-top:2px'>{esc(r.get('summary') or '')}</div>"
                if r.get("summary") else "")),
            ("Status", "left", lambda r: esc(r.get("status") or "—")),
            ("Chats", "right", lambda r: f"{r['chats']:,}"),
            ("Cost", "right", lambda r: _money(r["cost_usd"]))]),
        table("Top pull requests", _rows(ro.get("prs")), [
            ("PR", "left", lambda r: esc(r["key"])),
            ("Chats", "right", lambda r: f"{r['chats']:,}"),
            ("Cost", "right", lambda r: _money(r["cost_usd"]))]),
        table("Top repositories", _rows(ro.get("repos")), [
            ("Repository", "left", lambda r: esc(r["key"])),
            ("Chats", "right", lambda r: f"{r['chats']:,}"),
            ("Cost", "right", lambda r: _money(r["cost_usd"]))]),
        table("Most expensive chats", _rows(_digest_sessions(p)), [
            ("Chat", "left", lambda r: esc((r["title"] or "")[:70]) + (
                " <span style='color:#9a6700;font-size:10px'>EST</span>"
                if r.get("est") else "")),
            ("Requests", "right", lambda r: f"{(r.get('requests') or 0):,}"),
            ("Cost", "right", lambda r: _money(r["cost_usd"]))]),
    ]
    if billed:
        split = (f"Billed to {esc(email)} &nbsp;·&nbsp; included {_money(incl)} "
                 f"&nbsp;·&nbsp; on-demand {_money(od)}")
        foot = (f"Generated locally by cursor_dashboard.py. Figures are Cursor billed usage "
                f"for {esc(email)} (included plan + on-demand). The Pro / Pro+ subscription "
                f"fee is invoiced separately and is not included. Cloud agents on other "
                f"machines are included in billed events; chats from a previous Cursor login "
                f"on this machine are not.")
    else:
        split = "Local transcript estimate — not a Cursor invoice."
        foot = ("Generated locally by cursor_dashboard.py from this machine's Cursor chat "
                "store. These are list-price estimates, not a Cursor invoice.")
    return f"""<html><body style="margin:0;padding:24px;background:#fff;
 font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2328">
<div style="max-width:720px;margin:0 auto">
<div style="font-size:12px;color:#57606a;text-transform:uppercase;letter-spacing:.6px">
Cursor &mdash; daily digest</div>
<h2 style="margin:4px 0 2px;font-size:20px">{esc(day_pretty)}</h2>
<div style="font-size:12px;color:#57606a">{deltas or "&nbsp;"}</div>
<table cellspacing="0" cellpadding="0" style="margin:16px 0 4px"><tr>{card_html}</tr></table>
<div style="font-size:12px;color:#57606a;margin:10px 0 0">{split}</div>
{"".join(parts)}
<p style="margin:26px 0 0;font-size:11px;color:#8b949e;border-top:1px solid #e6e8eb;
padding-top:10px">
{foot}
</p></div></body></html>"""


def _digest_text(d):
    p = d["payload"]
    t = p["totals"]
    billed = bool(p.get("billed"))
    email = p.get("billing_email") or EMAIL_TO
    lines = [f"Cursor daily digest - {d['day']}", ""]
    if billed:
        lines += [f"Account     : {email}",
                  f"Spend       : {_money(t['cost_usd'])}",
                  f"Included    : {_money(t.get('included_usd') or 0)}",
                  f"On-demand   : {_money(t.get('on_demand_usd') or 0)}"]
    else:
        lines.append(f"Est. spend  : {_money(t['cost_usd'])}")
    lines += [f"Requests    : {t['requests']:,}",
              f"Chats       : {t['sessions']:,}"]
    if d["prev_cost"]:
        lines.append(f"Prior active day ({d['prev_day']}): {_money(d['prev_cost'])}")
    if d["avg_cost"]:
        lines.append(f"{d['avg_days']}-day average: {_money(d['avg_cost'])}")
    lines += ["", "Top models:"]
    for m in _rows(p.get("models")):
        lines.append(f"  {m['model']:<28} {m['requests']:>6,} req  {_money(m['cost_usd'])}")
    ro = p.get("rollup") or {}
    for label, key in (("Jira", "jira"), ("Pull requests", "prs"), ("Repositories", "repos")):
        rows = _rows(ro.get(key))
        if rows:
            lines += ["", f"{label}:"]
            for r in rows:
                extra = ""
                if key == "jira" and r.get("summary"):
                    extra = f"  {r['summary'][:48]}"
                    if r.get("status"):
                        extra += f" [{r['status']}]"
                lines.append(f"  {r['key']:<40} {_money(r['cost_usd'])}{extra}")
    lines += ["", "Most expensive chats:"]
    for s in _rows(_digest_sessions(p)):
        lines.append(f"  {(s['title'] or '')[:52]:<52} {_money(s['cost_usd'])}")
    if billed:
        lines += ["", f"Cursor billed usage for {email} (included plan + on-demand). "
                  "Subscription invoices are separate."]
    else:
        lines += ["", "Local transcript estimates, not a Cursor invoice."]
    return "\n".join(lines)


def send_digest(day, recent=None, interactive=False):
    """Build and deliver the digest for one day via the Gmail API."""
    d = _digest_data(day, recent)
    msg = EmailMessage()
    msg["Subject"] = _digest_subject(d)
    msg["From"] = _digest_from()
    msg["To"] = EMAIL_TO
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_digest_from().split("@")[-1])
    msg.set_content(_digest_text(d))
    msg.add_alternative(_digest_html(d), subtype="html")
    _gmail_send_message(msg, interactive=interactive)
    return d


def _digest_worker(day, recent, state, interactive=True):
    try:
        send_digest(day, recent, interactive=interactive)
        state["last_digest_day"] = day
        state["last_sent_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        DIGEST_STATUS["last_error"] = ""
        print(f"[digest] sent {day} to {EMAIL_TO}")
    except Exception as exc:
        DIGEST_STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[digest] FAILED for {day}: {exc}")
        traceback.print_exc()
    finally:
        DIGEST_STATUS["sending"] = False
        _state_save(state)


def maybe_send_digest(force=False):
    """Send the digest once, on the first refresh of a new local day.

    The guard is written to disk *before* the mail is attempted and the whole
    check is serialized, so several browser tabs refreshing at once cannot
    produce duplicate mail.
    """
    if not EMAIL_TO:
        return
    today = datetime.date.today().isoformat()
    with DIGEST_LOCK:
        state = _state_load()
        if not _digest_enabled(state):
            return
        if not force and state.get("last_check_day") == today:
            return
        state["last_check_day"] = today
        day, recent = _find_digest_day(today)
        if not day or (not force and day == state.get("last_digest_day")):
            _state_save(state)
            return
        DIGEST_STATUS["sending"] = True
        _state_save(state)
        threading.Thread(target=_digest_worker, args=(day, recent, state),
                         kwargs={"interactive": True}, daemon=True).start()


def digest_status():
    state = _state_load()
    ready, gmail_email = _gmail_ready()
    via = f"Gmail API as {gmail_email}" if gmail_email else "Gmail API"
    hint = ""
    if not os.path.isfile(_gmail_credentials_path() or ""):
        hint = _gmail_setup_hint()
    elif not ready:
        hint = "Gmail is not authorized. Run: py -3 cursor_dashboard.py --gmail-auth"
    return {"configured": bool(EMAIL_TO), "enabled": _digest_enabled(state),
            "to": EMAIL_TO, "from": gmail_email or _digest_from(),
            "via": via, "gmail_email": gmail_email, "ready": ready,
            "last_digest_day": state.get("last_digest_day"),
            "last_sent_at": state.get("last_sent_at"),
            "sending": DIGEST_STATUS["sending"],
            "error": DIGEST_STATUS["last_error"] or hint}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, body, ctype):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path == "/api/data":
                start = qs.get("start", ["0000-01-01"])[0]
                end = qs.get("end", ["9999-12-31"])[0]
                q = qs.get("q", [""])[0]
                force = qs.get("refresh", ["0"])[0] == "1"
                payload = build_payload(start, end, q, force=force)
                # The first refresh of a new day is what triggers the digest; it
                # rides along with the data request so a scheduled browser open
                # is enough to send mail.
                maybe_send_digest()
                payload["digest"] = digest_status()
                self._send(json.dumps(payload, default=str), "application/json")
            elif url.path == "/api/digest":
                action = qs.get("action", [""])[0]
                if qs.get("force", ["0"])[0] == "1":
                    action = "send"
                if action in ("on", "off"):
                    digest_set_enabled(action == "on")
                elif action == "send":
                    maybe_send_digest(force=True)
                self._send(json.dumps(digest_status()), "application/json")
            elif url.path in ("/api/budget", "/api/credits"):
                if "value" in qs:
                    budget_set(qs.get("value", [""])[0])
                self._send(json.dumps({"budget": CREDIT_BUDGET}),
                           "application/json")
            elif url.path == "/api/turns":
                sid = qs.get("session_id", [""])[0]
                turns = scan_cursor().get("turns", {}).get(sid, [])
                self._send(json.dumps(turns, default=str), "application/json")
            elif url.path == "/api/store":
                ide = ide_store_candidates()
                sample = []
                try:
                    # Prefer currently loaded billed session ids when available.
                    cached = (_CACHE.get("data") or {}).get("sessions") or []
                    sample = [s.get("session_id") for s in cached
                              if s.get("billed") and _is_untitled(s.get("title"))][:12]
                except Exception:
                    sample = []
                self._send(json.dumps({
                    "db": DB_PATH,
                    "merged": _merged_store_path(),
                    "ide": ide,
                    "counts": _store_row_counts(DB_PATH),
                    "composer_index": _composer_index_stats(DB_PATH),
                    "join": _join_diagnostics(DB_PATH, sample_cids=sample),
                    "build": "titles-v6",
                }, default=str), "application/json")
            elif url.path in ("/", "/index.html"):
                self._send(PAGE, "text/html; charset=utf-8")
            else:
                self.send_error(404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        except Exception as exc:
            traceback.print_exc()
            try:
                self._send(json.dumps({"error": str(exc)}), "application/json")
            except Exception:
                return

    def do_POST(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            if url.path == "/api/import-store":
                mode = (qs.get("mode", ["ide"])[0] or "ide").strip().lower()
                dest = _merged_store_path()
                if mode == "ide":
                    rebuild = (qs.get("rebuild", ["1"])[0] or "1").strip() not in ("0", "false", "no")
                    summary = import_ide_into(DB_PATH, dest_path=dest, rebuild=rebuild)
                elif mode == "upload":
                    if not body:
                        raise ValueError("POST body must be a state.vscdb file")
                    os.makedirs(_dashboard_data_dir(), exist_ok=True)
                    upload_path = os.path.join(_dashboard_data_dir(), "uploaded-state.vscdb")
                    with open(upload_path, "wb") as fh:
                        fh.write(body)
                    summary = merge_stores(
                        [upload_path], dest, seed_path=DB_PATH if os.path.isfile(DB_PATH) else None)
                elif mode == "merge":
                    extra = (qs.get("path", [""])[0] or "").strip()
                    if not extra:
                        raise ValueError("mode=merge requires ?path=")
                    summary = merge_stores(
                        [extra], dest, seed_path=DB_PATH if os.path.isfile(DB_PATH) else None)
                else:
                    raise ValueError("mode must be ide, upload, or merge")
                use_store(dest)
                summary["db"] = DB_PATH
                summary["counts"] = _store_row_counts(DB_PATH)
                self._send(json.dumps(summary, default=str), "application/json")
            else:
                self.send_error(404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        except Exception as exc:
            traceback.print_exc()
            try:
                self._send(json.dumps({"error": str(exc)}), "application/json")
            except Exception:
                return


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Cursor Chat Cost Dashboard</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;--acc:#2f81f7;--good:#3fb950}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);
 padding:12px 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600;flex:1}
input,select,button{background:#0d1117;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:inherit}
button{cursor:pointer}button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
main{padding:20px;max-width:1500px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0;overflow:hidden}
.card.subcard{grid-column:span 1;min-width:190px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:24px;font-weight:700;margin-top:6px}
.card .v .metered-note{display:block;margin-top:6px;font-size:11px;font-weight:500;color:var(--dim);line-height:1.4}
.card .fees{margin-top:10px;display:flex;flex-direction:column;gap:6px;font-weight:500;letter-spacing:0}
.card .fees.compact .fee-l{font-size:12px}
.card .fee{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.card .fee-l{display:flex;flex-direction:column;gap:1px;font-size:13px;font-weight:600;color:var(--fg)}
.card .fee-l span{font-size:11px;font-weight:400;color:var(--dim)}
.card .fee-r{color:#d2a8ff;font-variant-numeric:tabular-nums;white-space:nowrap;font-size:14px}
.card .fee-note{font-size:11px;font-weight:400;color:var(--dim);margin-top:-4px}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:20px}
h2{font-size:13px;margin:0 0 12px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--dim);font-weight:600;font-size:12px;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
tbody tr:hover{background:#1c2128}
.cost{color:var(--good);font-weight:600}
.sub{color:var(--dim);font-size:12px}
.sub.billing-note{font-style:normal;line-height:1.35;max-width:52em;white-space:normal}
#busy{position:fixed;top:0;left:0;right:0;height:3px;background:transparent;z-index:100;
  overflow:hidden;pointer-events:none;opacity:0;transition:opacity .15s}
body.busy #busy{opacity:1}
#busy::after{content:"";position:absolute;top:0;left:0;height:100%;width:40%;
  background:linear-gradient(90deg,transparent,var(--acc),transparent);
  animation:slide 1.1s linear infinite}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(350%)}}
@keyframes spin{to{transform:rotate(360deg)}}
#refresh .spin{display:none;width:11px;height:11px;margin-right:6px;vertical-align:-1px;
  border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;
  animation:spin .7s linear infinite}
body.busy #refresh .spin{display:inline-block}
body.busy #refresh{opacity:.8}
body.busy main > *:not(#loadnote){opacity:.45;transition:opacity .15s}
#loadnote{display:none;align-items:center;gap:8px;padding:14px 16px;margin:0 0 14px;
  border:1px solid var(--line);border-radius:8px;color:var(--dim);font-size:13px}
body.busy #loadnote{display:flex}
#loadnote .spin{width:13px;height:13px;border:2px solid var(--line);border-top-color:var(--acc);
  border-radius:50%;animation:spin .7s linear infinite}
.bar{height:6px;background:var(--acc);border-radius:3px;min-width:2px}
.bar.dim{background:#4a5568}
tr.unattr td{color:var(--dim);font-style:italic}
tr.unattr td.cost{color:var(--dim)}
.spark{display:flex;gap:2px;align-items:flex-end;height:90px}
.spark>div{flex:1;min-height:1px;display:flex;flex-direction:column-reverse;border-radius:2px 2px 0 0;overflow:hidden}
.spark>div>i{display:block;width:100%}
.spark>div>i.m{background:var(--acc)}
.spark>div>i.e{background:#d29922}
.spark>div:hover>i.m{background:#58a6ff}
.spark>div:hover>i.e{background:#e3b341}
.sparkkey{display:flex;gap:14px;align-items:center}
.sparkkey span{display:inline-flex;align-items:center;gap:5px}
.sparkkey b{display:inline-block;width:9px;height:9px;border-radius:2px}
.expand{cursor:pointer;color:var(--acc)}
.turns td{font-size:12px;color:var(--dim);background:#0d1117}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.badges{margin-top:5px;display:flex;flex-wrap:wrap;gap:4px}
.b{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;
   border:1px solid var(--line);background:#0d1117;white-space:nowrap}
.b.jira{border-color:#8957e5;color:#c297ff}
.b.jira-type{border-color:#388bfd;color:#79c0ff}
.b.jira-st{border-color:#8957e5;color:#c297ff}
.b.jira-st.done,.b.jira-st.new{border-color:#3fb950;color:#7ee787}
.b.jira-st.indeterminate{border-color:#d29922;color:#e3b341}
.b.pr{border-color:#3fb950;color:#7ee787}
.b.prnew{border-color:#d29922;color:#e3b341}
.b.repo{border-color:#388bfd;color:#79c0ff}
.b.local{border-color:#8957e5;color:#d2a8ff}
.b.cloud{border-color:#388bfd;color:#79c0ff;background:#0d1929}
.b.gitinf{border-color:#8957e5;color:#d2a8ff}
.git-corr{margin:0 0 10px;padding:8px 10px;background:#1c1425;border:1px solid #8957e5;border-radius:6px;font-size:12px;line-height:1.5}
.git-near{margin-top:4px}
.git-hit{display:block;margin-top:2px;color:var(--dim)}
.b.more{color:var(--dim)}
.b.est{border-color:#d29922;color:#e3b341;background:#1c1710}
.card .split{display:flex;flex-wrap:wrap;gap:3px 12px;margin-top:5px;font-size:11px;
  font-weight:600;letter-spacing:0;color:var(--dim)}
.card .split span{white-space:nowrap}
.card .split .e{color:#e3b341}
.card .split .s{color:#d2a8ff}
.card .split .c{color:#79c0ff}
.card .split i{font-style:normal;font-weight:400;opacity:.75}
td.cost .split,td.cost .cost-lbl{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:2px 6px;
  margin-top:2px;font-size:11px;font-weight:400;color:var(--dim);line-height:1.35;text-align:right;
  white-space:normal}
td.cost .split span{white-space:nowrap}
td.cost .split .sep{color:var(--dim);opacity:.55;padding:0 2px;white-space:pre}
td.cost .split .s,td.cost .cost-lbl.s{color:#d2a8ff}
td.cost .split .e,td.cost .cost-lbl.e{color:#e3b341}
td.cost .split i{font-style:normal;opacity:.75}
button.fold{background:none;border:0;color:var(--dim);cursor:pointer;font:inherit;
  padding:0 7px 0 0;line-height:1}
button.fold:hover{color:var(--fg)}
section.collapsed > *:not(h2){display:none !important}
.tabs{display:flex;gap:6px;margin-bottom:12px;align-items:center;flex-wrap:wrap}
.note{border:1px solid #d29922;background:#1c1710;color:#e3b341;border-radius:8px;
   padding:10px 12px;font-size:12px;line-height:1.5;margin-bottom:14px}
.tabs button.on{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.origin-tabs{display:inline-flex;gap:4px;margin-left:8px}
.origin-tabs button{font-size:11px;padding:2px 8px}
.origin-tabs button.on{background:#388bfd33;border-color:#388bfd;color:#79c0ff}
.tabs input{margin-left:auto}
.clearable{position:relative;display:inline-block}
.tabs > .clearable{margin-left:auto}
.tabs > .clearable > input{margin-left:0}
.clearable > input{padding-right:24px}
.clearx{position:absolute;right:2px;top:50%;transform:translateY(-50%);display:none;
  border:0;background:none;color:var(--dim);cursor:pointer;font-size:15px;line-height:1;
  padding:0 5px;border-radius:6px}
.clearable.has > .clearx{display:block}
.clearx:hover{color:var(--fg)}
.budgetin{font:inherit;color:inherit;background:transparent;border:0;border-bottom:1px dashed var(--dim);
  border-radius:0;padding:0 2px;width:6.5em;text-align:left}
.budgetin:hover{border-bottom-color:var(--fg)}
.budgetin:focus{outline:none;border-bottom:1px solid var(--acc)}
.mtdgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}
.mtdgrid .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.mtdgrid .v{font-size:22px;font-weight:700;margin-top:4px}
.allow-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:12px}
.allow-card{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.allow-card.primary{border-color:#388bfd55}
.allow-card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.allow-card .v{font-size:20px;font-weight:700;margin-top:6px;line-height:1.2}
.allow-card .v .sub{font-size:13px;font-weight:500;color:var(--dim)}
.allow-rem{margin-top:4px;font-size:13px;font-weight:600;color:var(--good)}
.allow-rem.over{color:#f85149}
.allow-card .meter{margin-top:10px;height:8px}
.allow-card .sub{margin-top:6px;font-size:11px;line-height:1.4}
#rangeAllowance{margin-bottom:16px}
#rangeAllowance h2{font-size:15px;margin:0 0 6px;font-weight:600}
#rangeAllowance .range-meta{margin-bottom:10px}
#modelUtil h2{font-size:15px;margin:0 0 6px;font-weight:600}
#modelUtil .range-meta{margin-bottom:10px}
#cycleModels{margin-top:12px}
.pool-badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:8px;
  border:1px solid var(--line);color:var(--dim);margin-left:6px;vertical-align:middle}
.pool-badge.cursor{border-color:#388bfd;color:#79c0ff}
.pool-badge.other{border-color:#d2a8ff;color:#d2a8ff}
.meter{height:12px;background:#0d1117;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.meter>div{height:100%;background:var(--good);transition:width .3s}
.meter>div.warn{background:#d29922}.meter>div.over{background:#f85149}
#err{color:#f85149}
/* Daily digest flag: only ever visible when a recipient is configured. */
#digest{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid #30363d;
  color:#8b949e;cursor:pointer;user-select:none;white-space:nowrap}
#digest.on{border-color:#2ea043;color:#3fb950}
#digest.busy{border-color:#d29922;color:#d29922}
#digest.bad{border-color:#f85149;color:#f85149}
#digest.off{border-style:dashed;color:#6e7681}
#digestsend{font-size:11px;padding:2px 6px;border-radius:10px;border:1px solid #30363d;
  background:none;color:#8b949e;cursor:pointer;white-space:nowrap}
#digestsend:hover{color:#c9d1d9;border-color:#8b949e}
.autobox{display:inline-flex;flex-direction:column;align-items:flex-start;line-height:1.15}
#lastref{font-size:10px;color:var(--dim);white-space:nowrap;padding-left:18px}
</style></head><body>
<div id="busy"></div>
<header>
  <h1>Cursor &mdash; Chat Cost Dashboard <span class="sub" id="buildStamp">· titles-v6</span></h1>
  <label class="sub">From <input type="date" id="start"></label>
  <label class="sub">To <input type="date" id="end"></label>
  <select id="preset">
    <option value="all">All time</option>
    <option value="today">Today</option>
    <option value="yesterday">Yesterday</option>
    <option value="week">This week</option>
    <option value="7">Last 7 days</option>
    <option value="mtd">This cycle</option>
    <option value="lastcycle">Last cycle</option>
    <option value="30">Last 30 days</option><option value="90">Last 90 days</option>
  </select>
  <span class="clearable"><input id="q" placeholder="Filter chats…" size="18"><button
    class="clearx" data-for="q" tabindex="-1" title="Clear filter"
    aria-label="Clear filter">&times;</button></span>
  <span class="sub" id="qnote"></span>
  <span id="digest" title="Daily digest email"></span>
  <button id="digestsend" title="Send the digest now">Send now</button>
  <span class="autobox">
    <label class="sub"><input type="checkbox" id="auto" checked> auto 15m</label>
    <span id="lastref" title="When the data on this page was last loaded"></span>
  </span>
  <button class="primary" id="refresh"><span class="spin"></span>Refresh</button>
  <button id="importIde" title="Rebuild dashboard store from Cursor IDE state.vscdb (replaces stale merged copy)">Rebuild from IDE store</button>
  <label class="sub" title="Upload a state.vscdb to merge into this dashboard store"
    style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
    Upload store<input type="file" id="uploadStore" accept=".vscdb,application/octet-stream" hidden>
  </label>
</header>
<main>
  <div id="loadnote"><span class="spin"></span><span id="loadmsg">Loading…</span></div>
  <div id="err"></div>
  <div class="note" id="mixnote"></div>
  <section id="modelUtil">
    <h2>Included in plan <span class="sub" id="modelUtilBuild">· pools-v3 · titles-v6</span></h2>
    <div class="sub range-meta" id="modelUtilMeta">Loading Cursor Models / Other Models pools…</div>
    <div class="allow-grid" id="modelUtilGrid">
      <div class="allow-card primary">
        <div class="k">Cursor Models <span class="sub">— Includes Cursor Grok and Composer</span></div>
        <div class="v">…</div>
        <div class="sub">Waiting for billed usage-summary (autoPercentUsed / apiPercentUsed).</div>
      </div>
      <div class="allow-card">
        <div class="k">Other Models</div>
        <div class="v">…</div>
        <div class="sub">Named and third-party model APIs.</div>
      </div>
    </div>
    <table id="cycleModels"></table>
    <div class="sub" id="cycleModelsFoot"></div>
  </section>
  <section id="rangeAllowance" style="display:none">
    <h2 id="rangeAllowHeading">Allowance usage</h2>
    <div class="sub range-meta" id="rangeAllowMeta"></div>
    <div class="allow-grid">
      <div class="allow-card primary">
        <div class="k">Allowance (included plan)</div>
        <div class="v" id="rangeIncUsed"></div>
        <div class="allow-rem" id="rangeIncRem"></div>
        <div class="meter"><div id="rangeIncBar"></div></div>
        <div class="sub" id="rangeIncDetail"></div>
      </div>
      <div class="allow-card">
        <div class="k">On-demand metered</div>
        <div class="v" id="rangeOdUsed"></div>
        <div class="allow-rem" id="rangeOdRem"></div>
        <div class="sub">USAGE_BASED events — cash overage beyond included allowance.</div>
      </div>
      <div class="allow-card">
        <div class="k">Total metered in range</div>
        <div class="v" id="rangeTotalUsed"></div>
        <div class="sub" id="rangeTokFoot"></div>
      </div>
    </div>
  </section>
  <div class="cards" id="cards"></div>
  <section id="mtd" style="display:none">
    <h2 id="mtdHeading">Plan allowance this cycle</h2>
    <div class="sub" id="cycleMeta"></div>
    <div class="allow-grid">
      <div class="allow-card primary">
        <div class="k">Included usage allowance</div>
        <div class="v"><span id="cycleIncUsed"></span> <span class="sub">used of <span id="cycleIncLimit"></span> allocated</span></div>
        <div class="allow-rem" id="cycleIncRem"></div>
        <div class="meter"><div id="mtdBar"></div></div>
        <div class="sub" id="cycleIncDetail"></div>
      </div>
      <div class="allow-card" id="cycleBonusCard" style="display:none">
        <div class="k">Bonus usage</div>
        <div class="v" id="cycleBonus"></div>
        <div class="sub">Extra included spend from model providers beyond what you purchased.</div>
      </div>
      <div class="allow-card">
        <div class="k">On-demand pool</div>
        <div class="v"><span id="cycleOdUsed"></span> <span class="sub">used of <span id="cycleOdLimit"></span> allocated</span></div>
        <div class="allow-rem" id="cycleOdRem"></div>
        <div class="sub">Cash overage invoiced when this pool is used.</div>
      </div>
      <div class="allow-card">
        <div class="k">Tokens this cycle</div>
        <div class="v" id="mtdTok"></div>
        <div class="sub" id="cycleTokFoot"></div>
      </div>
    </div>
    <div class="sub" id="mtdFoot"></div>
    <div class="sub" id="mtdNote"></div>
  </section>
  <section><h2>Daily spend</h2><div class="spark" id="spark"></div><div class="sub" id="sparklabel"></div></section>
  <section><h2>Cost by work item</h2>
    <div class="tabs">
      <button data-t="jira" class="on">Jira tickets</button>
      <button data-t="prs">Pull requests</button>
      <button data-t="repos">Repositories</button>
      <button data-t="sessions">Sessions</button>
      <span id="sessionOriginTabs" class="origin-tabs" style="display:none">
        <button data-o="all" class="on">All</button>
        <button data-o="cloud">Cloud</button>
        <button data-o="local">Local IDE</button>
      </span>
      <span class="clearable"><input id="rq" placeholder="Search work items…" size="22"><button
        class="clearx" data-for="rq" tabindex="-1" title="Clear search"
        aria-label="Clear search">&times;</button></span>
    </div>
    <table id="rollup"></table>
    <div class="sub" id="rollupfoot"></div>
  </section>
  <section><h2>Cost by model</h2><table id="models"></table></section>
  <section id="chatSessions"><h2>Cost by chat / session <span class="sub">(click a row for per-turn detail)</span></h2>
    <table id="sessions"></table></section>
  <div class="sub" id="foot"></div>
</main>
<script>
const usd=n=>'$'+(n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const usd4=n=>'$'+(n||0).toLocaleString(undefined,{minimumFractionDigits:4,maximumFractionDigits:4});
const num=n=>{const v=n||0;return Number.isInteger(v)?v.toLocaleString():v.toLocaleString(undefined,{maximumFractionDigits:1});};
const kt=n=>{n=n||0;return n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':n};
function split(total,parts,fmt){
  const f=fmt||usd;
  const shown=(parts||[]).filter(p=>(p.v||0)>0.004);
  if(!shown.length) return f(total);
  return `${f(total)}<div class="split">`+shown.map(p=>
    `<span class="${p.cls||''}"${p.title?` title="${p.title}"`:''}>${f(p.v)} <i>${p.label}</i></span>`
  ).join('<span class="sep"> / </span>')+`</div>`;
}
function costCell(row,billed,prec){
  const fmt=prec?usd4:usd;
  const total=row.cost_usd||0;
  if(!billed){
    const est=row.est_usd||0;
    if(est>0.004){
      const meas=Math.max(total-est,0);
      return split(total,[
        ...(meas>0.004?[{v:meas,label:'measured',cls:'s'}]:[]),
        {v:est,label:'est.',cls:'e'},
      ],fmt);
    }
    return fmt(total);
  }
  const od=row.on_demand_usd||0;
  const inc=Math.max(total-od,0);
  if(od<=0.004)
    return `${fmt(total)}<div class="cost-lbl s"><i>allowance</i></div>`;
  if(inc<=0.004)
    return `${fmt(total)}<div class="cost-lbl e"><i>on-demand</i></div>`;
  return split(total,[
    {v:inc,label:'allowance',cls:'s',title:'Included plan or bonus — not cash on-demand'},
    {v:od,label:'on-demand',cls:'e',title:'Usage-based billing — cash overage pool'},
  ],fmt);
}
const costThTitle=billed=>
  billed
    ? 'Metered cost: allowance (included plan/bonus) vs on-demand (cash overage)'
    : 'Estimated cost from local transcript';
let DATA=null, sortKey='cost_usd', sortDir=-1, tab='jira', sessionOrigin='all', LAST_LOAD=null;

const jiraUrl=k=>(DATA&&DATA.jira_base)?`${DATA.jira_base}/browse/${k}`:null;
const prUrl=k=>{const [r,n]=k.split('#');return `https://github.com/${r}/pull/${n}`;};
const repoUrl=r=>`https://github.com/${r}`;
const MAXB=4;
function badges(refs){
  // Defensive: incomplete refs must never abort the sessions table render.
  if(!refs) return '';
  const jira=refs.jira||[];
  const prs=refs.prs||[];
  const repos=refs.repos||[];
  const out=[];
  jira.slice(0,MAXB).forEach(k=>{const u=jiraUrl(k);
    out.push(u?`<a class="b jira" href="${u}" target="_blank" title="Jira ${k}">${k}</a>`
              :`<span class="b jira" title="Set --jira-base to link">${k}</span>`);});
  if(jira.length>MAXB) out.push(`<span class="b more">+${jira.length-MAXB} Jira</span>`);
  prs.slice(0,MAXB).forEach(p=>out.push(
    `<a class="b ${p.inferred?'gitinf':(p.created?'prnew':'pr')}" href="${prUrl(p.key)}" target="_blank" title="${p.inferred?'PR inferred from merge commit near billed usage':(p.created?'PR created in this chat':'PR referenced')}: ${p.key}">${p.inferred?'≈ ':''}${p.created?'✚ ':''}#${p.number}</a>`));
  if(prs.length>MAXB) out.push(`<span class="b more">+${prs.length-MAXB} PR</span>`);
  repos.filter(r=>r.role==='primary'||r.role==='inferred').slice(0,3).forEach(r=>out.push(
    `<a class="b ${r.role==='inferred'?'gitinf':'repo'}" href="${repoUrl(r.name)}" target="_blank" title="${r.role==='inferred'?'Inferred from nearby git commits (±8h)':'Primary tracked repo'}">${r.role==='inferred'?'≈ ':''}${r.name.split('/').pop()}</a>`));
  return out.length?`<div class="badges">${out.join('')}</div>`:'';
}
function jiraDetailSub(i){
  if(tab!=='jira') return esc((i.titles||[]).join(' · '));
  const parts=[];
  if(i.summary) parts.push(i.summary);
  else if(i.titles&&i.titles.length) parts.push(i.titles.join(' · '));
  const badges=[];
  if(i.type) badges.push(`<span class="b jira-type">${esc(i.type)}</span>`);
  if(i.status) badges.push(`<span class="b jira-st ${esc(i.status_category||'')}" title="Jira status">${esc(i.status)}</span>`);
  if(i.assignee&&i.assignee!=='Unassigned') badges.push(`<span class="b more" title="Assignee">${esc(i.assignee)}</span>`);
  if(i.priority) badges.push(`<span class="b more" title="Priority">${esc(i.priority)}</span>`);
  if(i.parent) badges.push(`<a class="b jira" href="${jiraUrl(i.parent)}" target="_blank" title="Parent">${esc(i.parent)}</a>`);
  const badgeHtml=badges.length?`<div class="badges">${badges.join('')}</div>`:'';
  return esc(parts.join(' · '))+badgeHtml;
}
function renderRollup(){
  let items=(DATA.rollup&&DATA.rollup[tab])||[];
  const rq=(document.getElementById('rq').value||'').toLowerCase().trim();
  if(rq) items=items.filter(i=>(i.key+' '+(i.summary||'')+' '+(i.status||'')+' '+(i.type||'')+' '+(i.assignee||'')+' '+(i.titles||[]).join(' ')).toLowerCase().includes(rq));
  const originTabs=document.getElementById('sessionOriginTabs');
  if(originTabs) originTabs.style.display=tab==='sessions'?'':'none';
  const allSessionItems=tab==='sessions'?items.slice():[];
  if(tab==='sessions'&&sessionOrigin!=='all'){
    items=items.filter(i=>sessionOrigin==='cloud'?i.origin==='cloud':i.origin!=='cloud');
  }
  const billed=!!DATA.billed;
  const label={jira:'Jira ticket',prs:'Pull request',repos:'Repository',sessions:'Chat / session'}[tab];
  const link=x=>tab==='jira'?jiraUrl(x.key):tab==='prs'?prUrl(x.key):tab==='repos'?repoUrl(x.key):null;
  const unit=tab==='sessions'?'Turns':tab==='prs'?'Segments':'Chats';
  const mx=Math.max(...items.map(i=>i.cost_usd),0.0001);
  const un=(DATA.rollup&&DATA.rollup.unattributed&&DATA.rollup.unattributed[tab])||null;
  const showUn=un&&!rq&&un.cost_usd>0.005&&tab!=='sessions';
  const noun={jira:'Jira ticket',prs:'pull request',repos:'repository'}[tab]||'reference';
  const unRow=showUn?`<tr class="unattr">
      <td>No ${esc(noun)}<div class="sub">No chat metadata — expand “Other billed usage” for git-inferred repo hints if commits landed within ±8h of billed calls</div></td>
      <td>${un.chats?num(un.chats):'-'}</td><td>${kt(un.total_tokens)}</td>
      <td class="cost">${costCell(un,billed)}</td>
      <td style="width:160px"><div class="bar dim" style="width:${Math.min(100,un.cost_usd/mx*100)}%"></div></td></tr>`:'';
  document.getElementById('rollup').innerHTML=items.length?
    `<thead><tr><th>${label}</th><th>${unit}</th><th>Tokens</th>`
    +`<th title="${costThTitle(billed)}">Cost</th><th>Share</th></tr></thead><tbody>`+
    items.map(i=>{const u=link(i);
      const cell = (i.keys&&i.keys.length)
        ? i.keys.map(k=>`<a class="b pr" href="${prUrl(k)}" target="_blank">${esc(k.split('/').pop())}</a>`).join(' ')
          + (i.keys.length>1?` <span class="sub">${i.keys.length} PRs from one segment</span>`:'')
        : (tab==='sessions'&&i.cloud_agent&&i.session_id
          ? `<a href="${esc(i.cloud_url||('https://cursor.com/agents/'+i.session_id))}" target="_blank">${esc(i.key)}</a>`
          : (u?`<a href="${u}" target="_blank">${esc(i.key)}</a>`:esc(i.key)));
      const originBadge=tab==='sessions'
        ? (i.origin==='cloud'
          ? ` <span class="b cloud" title="Cloud agent session">cloud</span>`
          : ` <span class="b local" title="Local IDE chat with store history">local IDE</span>`)
        : '';
      return `<tr>
      <td>${cell}${originBadge}
        ${i.est?'<span class="b est" title="Includes estimated token data">est</span>':''}
        ${i.created?'<span class="b prnew">✚ created</span>':''}
        ${i.inferred?'<span class="b gitinf" title="PR inferred from merge commit near billed usage">git ±8h</span>':''}
        ${i.turns?'<span class="b more">'+i.turns+' turn'+(i.turns===1?'':'s')+'</span>':''}
        ${i.role==='inferred'?'<span class="b gitinf" title="Attributed from git commit timestamps near billed usage">git ±8h</span>':''}
        ${i.role==='primary'?'<span class="b repo">primary</span>':''}
        <div class="sub">${jiraDetailSub(i)}</div></td>
      <td>${num(i.chats)}</td><td>${kt(i.total_tokens)}</td>
      <td class="cost">${costCell(i,billed)}</td>
      <td style="width:160px"><div class="bar" style="width:${i.cost_usd/mx*100}%"></div></td></tr>`;}).join('')+
    unRow+'</tbody>' : `<tbody><tr><td class="sub">No ${label.toLowerCase()} ${rq?'matches "'+esc(rq)+'"':'references found in range'}.</td></tr></tbody>`;
  const tot=items.reduce((a,b)=>a+b.cost_usd,0);
  const tk=items.reduce((a,b)=>a+(b.total_tokens||0),0);
  const plural={jira:'Jira tickets',prs:'Pull requests',repos:'Repositories',sessions:'Chats / sessions'}[tab];
  const rec=showUn
    ? ` · ${usd(tot)} of ${usd(un.total_usd)} in view attributed · ${usd(un.cost_usd)} has no ${noun} to attribute it to`
    : (un&&!rq&&tab!=='sessions'?' · matches the totals above':'');
  const costKey=billed
    ? ' · multi-root workspace chats split by git commit/PR activity (±8h), not evenly'
    : '';
  let originNote='';
  if(tab==='sessions'&&allSessionItems.length){
    const cloud=allSessionItems.filter(i=>i.origin==='cloud');
    const local=allSessionItems.filter(i=>i.origin!=='cloud');
    const cloudUsd=cloud.reduce((a,b)=>a+b.cost_usd,0);
    const localUsd=local.reduce((a,b)=>a+b.cost_usd,0);
    originNote=` · ${cloud.length} cloud (${usd(cloudUsd)}) · ${local.length} local IDE (${usd(localUsd)})`;
    if(sessionOrigin!=='all'){
      originNote+=sessionOrigin==='cloud'?' · showing cloud only':' · showing local IDE only';
    }
  }
  let jiraNote='';
  if(tab==='jira'){
    if(DATA.jira_connected) jiraNote=' · Jira API connected';
    else if(DATA.jira_error) jiraNote=' · '+DATA.jira_error;
    else jiraNote=' · run store_atlassian_token.ps1 for Jira summaries';
  }
  document.getElementById('rollupfoot').textContent=
    `${items.length} ${(items.length===1?label:plural).toLowerCase()} · ${kt(tk)} tokens · ${usd(tot)}`+rec+originNote+costKey+jiraNote;
  document.querySelectorAll('.tabs button[data-t]').forEach(b=>b.classList.toggle('on',b.dataset.t===tab));
  document.querySelectorAll('#sessionOriginTabs button').forEach(b=>b.classList.toggle('on',b.dataset.o===sessionOrigin));
}

let BUSY=0;
function busy(on,msg){
  BUSY=Math.max(0,BUSY+(on?1:-1));
  document.body.classList.toggle('busy',BUSY>0);
  if(on&&msg)document.getElementById('loadmsg').textContent=msg;
}
async function load(force){
  const preset=document.getElementById('preset').value;
  if(isCyclePreset(preset)){
    if(DATA&&DATA.cycles) applyPreset(preset);
  } else {
    syncPreset();
  }
  const s=document.getElementById('start').value||'0000-01-01';
  const e=document.getElementById('end').value||'9999-12-31';
  const q=document.getElementById('q').value.trim();
  busy(true,'Loading Cursor billed usage — first load fetches events from cursor.com and scans local chat titles…');
  try{
    const r=await fetch(`/api/data?start=${s}&end=${e}&q=${encodeURIComponent(q)}${force?'&refresh=1':''}`);
    if(!r.ok){
      document.getElementById('err').textContent='Error: HTTP '+r.status+' '+r.statusText;
      return;
    }
    DATA=await r.json();
    if(DATA.error){document.getElementById('err').textContent='Error: '+DATA.error;return;}
    document.getElementById('err').textContent='';
    if(isCyclePreset(preset)&&DATA.cycles){
      const [a,b]=presetRange(preset);
      if(a&&b&&(a!==s||b!==e)){
        document.getElementById('start').value=a;
        document.getElementById('end').value=b;
        PRESET_LOCK={v:preset,start:a,end:b};
        if(!load._cycleFix){
          load._cycleFix=true;
          try{await load(force);}finally{load._cycleFix=false;}
          return;
        }
      } else if(a&&b){
        PRESET_LOCK={v:preset,start:a,end:b};
      }
    }
    LAST_LOAD=new Date();
    renderLastRef();
    render();
  }catch(err){
    document.getElementById('err').textContent='Error: '+err
      +' — is the dashboard still running on this origin?';
  }finally{ busy(false); }
}
function renderLastRef(){
  const el=document.getElementById('lastref');
  if(!LAST_LOAD){ el.textContent=''; return; }
  const mins=Math.floor((Date.now()-LAST_LOAD)/60000);
  const age=mins<1?'just now':(mins+'m ago');
  el.textContent=LAST_LOAD.toLocaleTimeString([], {hour:'numeric',minute:'2-digit',
    second:'2-digit'})+' · '+age;
  el.title='Data last loaded '+LAST_LOAD.toLocaleString();
}
setInterval(renderLastRef, 30000);
function renderDigest(){
  const el=document.getElementById('digest'), btn=document.getElementById('digestsend'),
        d=DATA.digest||{};
  if(!d.configured){ el.style.display='none'; btn.style.display='none'; return; }
  el.style.display=''; btn.style.display=d.enabled?'':'none';
  if(!d.enabled){
    el.className='off'; el.textContent='✉ Digest: off';
    el.title='Daily digest is switched off — no mail will be sent.\nClick to turn it'
      +' back on.';
    return;
  }
  el.className = d.error?'bad' : d.sending?'busy' : d.last_digest_day?'on':'';
  const when=d.last_digest_day?('sent '+d.last_digest_day):'none sent yet';
  el.textContent = '✉ Digest: '+(d.sending?'sending…':when);
  el.title = (d.error?('Last error: '+d.error+'\n'):'')
    + `Daily digest to ${d.to} via ${d.via||'Gmail API'}`
    +(d.from&&d.from!==d.to?`\nFrom ${d.from}`:'')
    +(d.last_sent_at?`\nLast sent ${d.last_sent_at}`:'')
    +'\nSent on the first refresh of each day, covering the most recent day with'
    +' activity.\nClick to switch it off.';
}
async function digestCall(action){
  try{ DATA.digest=await (await fetch('/api/digest?action='+action)).json(); }catch(e){}
  renderDigest();
}
document.getElementById('digest').onclick=()=>
  digestCall((DATA.digest||{}).enabled?'off':'on');
document.getElementById('digestsend').onclick=async()=>{
  const el=document.getElementById('digest');
  el.className='busy'; el.textContent='✉ Digest: sending…';
  await digestCall('send');
  // Delivery happens on a background thread, so re-read the outcome shortly after.
  setTimeout(async()=>{ try{ DATA.digest=await (await fetch('/api/digest')).json();
    renderDigest(); }catch(e){} },4000);
};
function renderRangeAllowance(){
  const ra=DATA.range_allowance;
  const el=document.getElementById('rangeAllowance');
  if(!ra||!DATA.billed){
    el.style.display='none';
    return;
  }
  el.style.display='';
  const inc=ra.included_usd||0;
  const od=ra.on_demand_usd||0;
  const cost=ra.cost_usd||0;
  const limit=ra.included_limit||0;
  const plan=(ra.plan||DATA.plan||'').replace(/_/g,' ');
  const rangeLbl=`${ra.start||''} → ${ra.end||''}`;
  document.getElementById('rangeAllowHeading').textContent='Allowance usage in range';
  let meta=plan?(plan+' · '):'';
  meta+=rangeLbl;
  if(ra.range_is_current_cycle&&ra.cycle_start){
    meta+=` · billing cycle ${ra.cycle_start} → ${ra.cycle_end||''}`;
    if(ra.reset_date) meta+=` · resets ${ra.reset_date} (${ra.days_left} day${ra.days_left===1?'':'s'})`;
  } else if(ra.historical){
    meta+=' · closed billing cycle';
  }
  document.getElementById('rangeAllowMeta').textContent=meta;
  document.getElementById('rangeIncUsed').innerHTML=limit
    ? `${usd(inc)} <span class="sub">in range · ${usd(ra.cycle_included_used!=null?ra.cycle_included_used:inc)} of ${usd(limit)} this cycle</span>`
    : `${usd(inc)} <span class="sub">in range</span>`;
  const incRemEl=document.getElementById('rangeIncRem');
  if(limit&&ra.range_is_current_cycle&&ra.included_remaining!=null){
    const cycleUsed=ra.cycle_included_used||0;
    const exhausted=ra.included_remaining<=0.004;
    incRemEl.textContent=exhausted
      ? 'Cycle allowance exhausted'
      : `${usd(ra.included_remaining)} cycle allowance remaining (${(ra.pct_of_cycle_limit||0).toFixed(0)}% of plan used this cycle)`;
    incRemEl.className='allow-rem'+(exhausted?' over':'');
  } else if(limit&&ra.historical){
    const pct=ra.pct_of_cycle_limit||0;
    incRemEl.textContent=pct>=100
      ? `Over plan allowance by ${usd(inc-limit)}`
      : `${pct.toFixed(0)}% of ${usd(limit)} cycle allowance`;
    incRemEl.className='allow-rem'+(pct>=100?' over':'');
  } else {
    incRemEl.textContent=inc>0.004?'Included plan usage — not extra subscription cash':'';
    incRemEl.className='allow-rem';
  }
  const bar=document.getElementById('rangeIncBar');
  if(limit&&ra.range_is_current_cycle){
    const pct=Math.min(100,((ra.cycle_included_used||0)/limit*100));
    bar.style.width=pct+'%';
    bar.className=pct>=100?'over':pct>=80?'warn':'';
  } else if(limit&&ra.historical){
    const pct=Math.min(100,(inc/limit*100));
    bar.style.width=pct+'%';
    bar.className=pct>=100?'over':pct>=80?'warn':'';
  } else {
    bar.style.width='0%';
    bar.className='';
  }
  document.getElementById('rangeIncDetail').textContent=
    ra.range_is_current_cycle&&limit
      ? `${usd(inc)} allowance metered in this date range. Cycle totals from cursor.com/dashboard.`
      : 'Allowance is included plan usage — metered but not subscription cash charged again.';
  document.getElementById('rangeOdUsed').innerHTML=od>0.004
    ? `${usd(od)}${ra.on_demand_limit?` <span class="sub">of ${usd(ra.on_demand_limit)} pool</span>`:''}`
    : '$0.00';
  const odRemEl=document.getElementById('rangeOdRem');
  if(od>0.004){
    odRemEl.textContent=ra.on_demand_remaining!=null&&ra.on_demand_limit
      ? `${usd(ra.on_demand_remaining)} on-demand pool remaining this cycle`
      : `${usd(od)} on-demand metered in range`;
  } else odRemEl.textContent='';
  odRemEl.className='allow-rem';
  document.getElementById('rangeTotalUsed').innerHTML=
    split(cost,[
      {v:inc,label:'allowance',cls:'s',title:'Included plan usage'},
      {v:od,label:'on-demand',cls:'e',title:'Usage-based overage'},
    ]);
  const originCost=(ra.cloud_usd||0)>0.004||(ra.local_ide_usd||0)>0.004
    ? ` · ${usd(ra.local_ide_usd||0)} local IDE · ${usd(ra.cloud_usd||0)} cloud`
    : '';
  document.getElementById('rangeTokFoot').textContent=
    `${num(ra.requests)} requests · ${num(ra.sessions)} chats`
    +((ra.cloud_sessions||0)+(ra.local_ide_sessions||0)
      ? ` (${num(ra.cloud_sessions||0)} cloud · ${num(ra.local_ide_sessions||0)} local IDE)`
      : '')
    +` · ${kt(ra.total_tokens)} tokens${originCost}`;
}
function poolBadge(pool){
  const id=pool==='cursor'?'cursor':'other';
  const label=id==='cursor'?'Cursor Models':'Other Models';
  const title=id==='cursor'
    ? 'Included subscription pool: Cursor Grok and Composer (Settings → Plan & Usage)'
    : 'Included subscription pool: named / third-party models (Settings → Plan & Usage)';
  return ` <span class="pool-badge ${id}" title="${title}">${label}</span>`;
}
function pctBar(pct){
  const p=Math.max(0, pct||0);
  const w=Math.min(100,p);
  const cls=p>=100?'over':p>=80?'warn':'';
  return `<div class="meter"><div class="${cls}" style="width:${w}%"></div></div>`;
}
function renderModelUtil(){
  const el=document.getElementById('modelUtil');
  if(!el) return;
  // Never hide this section · stamp titles-v6 — if you cannot see "Included in plan · pools-v3",
  // the browser is not talking to this build of cursor_dashboard.py.
  el.style.display='block';
  const build=document.getElementById('modelUtilBuild');
  if(build) build.textContent='· pools-v3';
  const m=DATA.mtd||DATA.view_cycle||{};
  let pools=m.pools||[];
  const rows=DATA.cycle_models||m.cycle_models||[];
  const metaEl=document.getElementById('modelUtilMeta');
  const grid=document.getElementById('modelUtilGrid');
  if(!DATA || DATA.billed==null){
    metaEl.textContent='Waiting for /api/data… · build pools-v3';
    return;
  }
  if(!DATA.billed){
    metaEl.textContent=(DATA.billing_error
      ? (`Billed usage not loaded (${DATA.billing_error}). `)
      : 'Billed usage not loaded. ')
      +'Run without --no-api and sign into Cursor so usage-summary can return '
      +'Cursor Models / Other Models percentages. · build pools-v3';
    grid.innerHTML=`<div class="allow-card primary">
      <div class="k">Cursor Models <span class="sub">— Includes Cursor Grok and Composer</span></div>
      <div class="v">unavailable</div>
      <div class="sub">Need billed usage-summary (autoPercentUsed). Do not pass --no-api.</div>
    </div><div class="allow-card">
      <div class="k">Other Models</div>
      <div class="v">unavailable</div>
      <div class="sub">Need billed usage-summary (apiPercentUsed).</div>
    </div>`;
    const tbl=document.getElementById('cycleModels');
    const foot=document.getElementById('cycleModelsFoot');
    if(tbl) tbl.innerHTML='';
    if(foot) foot.textContent='';
    return;
  }
  // Synthesize cards from top-level percents when pools array is empty.
  if(!pools.length){
    const synth=[];
    const mk=(id,label,detail,pct,msg)=>{
      if(pct==null && !m.unlimited) return;
      const used=pct==null?0:+pct;
      synth.push({
        id, label, detail, unlimited:!!m.unlimited,
        used_pct:m.unlimited?null:used,
        remaining_pct:m.unlimited?null:Math.max(0,100-used),
        allocated_pct:m.unlimited?null:100,
        message:msg||'',
        metered_usd:(m.pool_spend&&m.pool_spend[id])||0,
      });
    };
    mk('cursor','Cursor Models','Includes Cursor Grok and Composer',
      m.auto_pct, m.auto_msg||'Additional usage beyond limits consumes Other Models quota or on-demand spend.');
    mk('other','Other Models','Named and third-party model APIs',
      m.api_pct, m.named_msg||'Additional usage beyond limits consumes on-demand spend.');
    if(m.total_pct!=null || m.unlimited)
      mk('total','Total included','Subscription included compute', m.total_pct, m.display_msg||'');
    pools=synth;
  }
  const plan=(m.plan||DATA.plan||'').replace(/_/g,' ');
  const unlim=!!m.unlimited;
  let meta=plan?(plan+' · '):'';
  meta+=`cycle ${m.start||''} → ${m.end||''}`;
  if(m.reset_date) meta+=` · resets ${m.reset_date}`;
  meta+=' · build pools-v3';
  metaEl.textContent=unlim
    ? meta+' · plan reports unlimited included usage — pool percentages are not a cap.'
    : meta+' · same pools as Cursor Settings → Plan & Usage: '
      +'Cursor Models (Grok + Composer) and Other Models. '
      +'Allocated is 100% of each included pool; used % matches the Settings bars '
      +'(autoPercentUsed / apiPercentUsed).';
  // Prefer the two Plan & Usage pools; keep total as a trailing card if present.
  const showPools=pools.length
    ? [...pools].sort((a,b)=>{
        const order={cursor:0,other:1,total:2};
        return (order[a.id]??9)-(order[b.id]??9);
      })
    : [];
  if(showPools.length){
    grid.innerHTML=showPools.map(p=>{
      const metered=(p.metered_usd||0)>0.004
        ? `${usd(p.metered_usd)} metered this cycle`
        : '';
      const primary=p.id==='cursor'?' primary':'';
      if(p.unlimited){
        return `<div class="allow-card${primary}">
          <div class="k">${esc(p.label)}</div>
          <div class="v">Unlimited <span class="sub">${esc(p.detail||'')}</span></div>
          <div class="sub">${esc([p.message,metered].filter(Boolean).join(' · '))}</div>
        </div>`;
      }
      const used=p.used_pct||0;
      const rem=p.remaining_pct;
      const remTxt=used>=100
        ? 'Included pool exhausted'
        : `${(rem||0).toFixed(0)}% remaining`;
      const alloc=p.allocated_pct==null?100:p.allocated_pct;
      return `<div class="allow-card${primary}">
        <div class="k">${esc(p.label)}${p.detail?` <span class="sub">— ${esc(p.detail)}</span>`:''}</div>
        <div class="v">${used.toFixed(0)}% used <span class="sub">of ${alloc}% allocated</span></div>
        <div class="allow-rem${used>=100?' over':''}">${remTxt}</div>
        ${pctBar(used)}
        <div class="sub">${esc([p.message,metered].filter(Boolean).join(' · '))}</div>
      </div>`;
    }).join('');
  } else if(m.included_limit>0){
    const used=m.included_usd||0, lim=m.included_limit||0;
    const pct=lim?Math.min(999,(used/lim*100)):0;
    grid.innerHTML=`<div class="allow-card primary">
      <div class="k">Total included</div>
      <div class="v">${pct.toFixed(0)}% used <span class="sub">of 100% allocated</span></div>
      <div class="allow-rem${pct>=100?' over':''}">${pct>=100?'Included pool exhausted'
        :`${Math.max(0,100-pct).toFixed(0)}% remaining`}</div>
      ${pctBar(pct)}
      <div class="sub">Derived from included spend (${usd(used)} of ${usd(lim)}). Cursor did not return Auto/named pool percentages for this account.</div>
    </div>`;
  } else {
    grid.innerHTML=`<div class="allow-card primary">
      <div class="k">Cursor Models <span class="sub">— Includes Cursor Grok and Composer</span></div>
      <div class="v">—</div>
      <div class="sub">Billed session is up, but usage-summary did not include autoPercentUsed/apiPercentUsed.
        Click Refresh. ${DATA.billing_error?esc(DATA.billing_error):''}</div>
    </div>`;
  }
  const tbl=document.getElementById('cycleModels');
  const foot=document.getElementById('cycleModelsFoot');
  if(!rows.length){
    tbl.innerHTML='';
    foot.textContent='';
    return;
  }
  const max=Math.max(...rows.map(r=>r.cost_usd||0),0.0001);
  tbl.innerHTML='<thead><tr><th>Model</th><th>Pool</th><th>Input</th><th>Cache write</th>'
    +'<th>Cache read</th><th>Output</th><th>Cost this cycle</th><th>Share</th></tr></thead><tbody>'
    +rows.map(r=>{
      const pool=r.pool||'other';
      return `<tr><td>${esc(r.model)}</td><td>${poolBadge(pool)}</td>`
        +`<td>${kt(r.input_tokens)}</td><td>${kt(r.cache_write_tokens)}</td>`
        +`<td>${kt(r.cache_read_tokens)}</td><td>${kt(r.output_tokens)}</td>`
        +`<td class="cost">${usd(r.cost_usd)}</td>`
        +`<td style="width:160px"><div class="bar" style="width:${(r.cost_usd||0)/max*100}%"></div></td></tr>`;
    }).join('')+'</tbody>';
  const spend=m.pool_spend||{};
  const cursorUsd=spend.cursor||0;
  const otherUsd=spend.other||0;
  foot.textContent=`Official cycle aggregations from Cursor (GetAggregatedUsageEvents) — `
    +`${usd(cursorUsd)} Cursor Models · ${usd(otherUsd)} Other Models. `
    +`These are included-subscription totals for the billing cycle, independent of the date-range filter.`;
}
function render(){
  const t=DATA.totals;
  // Plan & Usage pools first — do not let later render steps abort them.
  try{ renderModelUtil(); }catch(err){ console.error('renderModelUtil', err); }
  try{ renderDigest(); }catch(err){ console.error('renderDigest', err); }
  try{ renderRangeAllowance(); }catch(err){ console.error('renderRangeAllowance', err); }
  const budget=DATA.range_is_current_cycle?(DATA.mtd||{}).budget:null;
  const mix=document.getElementById('mixnote');
  if(DATA.billing_error && !DATA.billed){
    mix.innerHTML=`Could not load Cursor billed usage (${esc(DATA.billing_error)}). `
      +`Showing a local transcript estimate instead — this undercounts invoices.`;
  } else if(DATA.billed){
    const emails=(DATA.billing_emails&&DATA.billing_emails.length)
      ? DATA.billing_emails : [DATA.billing_email].filter(Boolean);
    const emailList=emails.map(e=>`<b>${esc(e)}</b>`).join(' and ');
    let explain='';
    if(emails.length>1){
      explain=`<br><br><b>Two accounts merged:</b> totals combine `
        +(t.by_account||[]).map(a=>`<b>${esc(a.account)}</b> ${usd(a.cash_usd)} cash / ${usd(a.metered_usd)} metered`).join(' · ')
        +`. Filter by account in the chat list (account badge).`;
    }
    const unUsd=t.unattributed_usd||0;
    const orphanUsd=t.orphan_billed_usd||0;
    if(unUsd>0.005){
      explain+=`<br><br><b>Other billed usage (${usd(unUsd)}):</b> Cursor invoiced model calls `
        +`without a conversation/chat ID, so they cannot be tied to a title in your local chat store. `
        +`The dashboard tries to infer likely repos from git commits and merge PRs in your workspace `
        +`repos within ±8 hours of each billed call (purple <span class="b gitinf">≈</span> badges). `
        +`Expand that row for per-turn commit matches.`;
    }
    if(orphanUsd>0.005){
      const orphanRows=(DATA.sessions||[]).filter(s=>s.orphan_billed);
      const withCa=orphanRows.filter(s=>s.cloud_agent_id||(s.session_id||'').startsWith('bc-')).length;
      const allOrphan=orphanRows.length>=Math.max(3, Math.floor((DATA.sessions||[]).length*0.8));
      explain+=`<br><br><b>Orphan billed chats (${usd(orphanUsd)}):</b> conversation ID on the `
        +`invoice but no matching chat history on this machine. `;
      if(allOrphan){
        explain+=`<b>Almost every chat is orphan/(untitled) the same way</b> — this is usually not a `
          +`missing local title, it is a billing↔store ID mismatch. Invoice rows often use a plain `
          +`UUID <code>conversationId</code> while Cloud Agents API ids are <code>bc-*</code>. `;
      }
      explain+=`bc-* ids (or events with <code>cloudAgentId</code>) are cloud agents; plain UUIDs are usually `
        +`cloud agents without that field, another device, cleared local history, or headless/automation. `;
      if(DATA.cloud_agents_error){
        explain+=`<b>Fix:</b> set <code>CLOUD_AGENTS_API_KEY</code> / <code>CURSOR_API_KEY</code> so titles can be `
          +`joined (${esc(DATA.cloud_agents_error)}). `;
      } else if(withCa || DATA.cloud_agents){
        explain+=`Cloud Agents API is loaded — confirm stamp <b>titles-v6</b> (cookie agent list + cloudAgentId join) so `
          +`UUID invoices join via <code>cloudAgentId</code> / time overlap. `;
      } else {
        explain+=`Also rebuild the IDE store and confirm <code>/api/store</code> has local composers. `;
      }
    }
    if(DATA.cloud_agents){
      explain+=`<br><br><b>Cloud agents (${num(DATA.cloud_agents)}):</b> loaded via `
        +esc(DATA.cloud_agents_source||'Cloud Agents API')
        +` and merged into this list (titles for bc-* / cloudAgentId chats + agents not yet on the invoice).`;
    } else if(DATA.cloud_agents_error){
      explain+=`<br><br><b>Cloud agents:</b> not loaded (${esc(DATA.cloud_agents_error)}). `
        +`Set <code>CLOUD_AGENTS_API_KEY</code> or <code>CURSOR_API_KEY</code> `
        +`from Cursor Dashboard → API Keys.`;
    }
    mix.innerHTML=`<b>Cash invoiced</b> in the cards below is what Stripe actually charged `
      +`(subscriptions + on-demand invoices). `
      +`<b>Usage metered</b> is Cursor's token-dollar accounting — included usage is plan allowance, `
      +`not money paid again. `
      +`Data from ${emailList||'signed-in account(s)'} since 29 Nov 2025.`
      +explain;
  } else {
    mix.innerHTML=`List-price estimate from this machine's Cursor chat store — not a Cursor invoice. `
      +`Cloud agents and chats on other machines are invisible here.`;
  }
  const billed=!!DATA.billed;
  const subGroups=t.subscription_summary||[];
  const subHint=subGroups.map(g=>{
    const who=(g.account||'').split('@')[0]||'account';
    return `${g.plan||'Plan'} × ${g.count} (${who}) ${usd(g.net_usd)}`;
  }).join(' · ');
  const subBreak=subGroups.length
    ? `<div class="fees compact">`+subGroups.map(g=>{
        const who=(g.account||'').split('@')[0]||'account';
        const range=(g.first_day&&g.last_day&&g.first_day!==g.last_day)
          ? g.first_day.slice(0,7)+'–'+g.last_day.slice(0,7)
          : (g.first_day||'');
        return `<div class="fee"><div class="fee-l">${esc(g.plan||'Plan')} × ${g.count}`
          +`<span>${esc(who)}${range?' · '+range:''}</span></div>`
          +`<div class="fee-r">${usd(g.net_usd)}</div></div>`;
      }).join('')+`</div>`
    : `<div class="split"><span>no plan invoices in this range</span></div>`;
  const cash=t.cash_usd!=null?t.cash_usd:((t.subscription_usd||0)+(t.invoice_usage_usd||0));
  const odCash=t.invoice_usage_usd!=null?t.invoice_usage_usd:0;
  const odMetered=t.on_demand_usd||0;
  const originSplit=(t.cloud_usd>0.004||t.local_ide_usd>0.004)
    ? ` · ${usd(t.local_ide_usd||0)} local IDE · ${usd(t.cloud_usd||0)} cloud`
    : '';
  const meteredNote=billed&&t.metered_usd
    ? `<span class="metered-note">${usd(t.metered_usd)} usage metered · `
      +`${usd(odMetered)} on-demand metered · `
      +`${usd(t.included_usd||0)} allowance (not extra cash)${originSplit}</span>`
    : '';
  const sessionCounts=(t.cloud_sessions||0)+(t.local_ide_sessions||0)
    ? `<div class="split">`
      +`<span class="c">${num(t.cloud_sessions||0)} <i>cloud</i></span>`
      +`<span class="sep"> / </span>`
      +`<span class="s">${num(t.local_ide_sessions||0)} <i>local IDE</i></span></div>`
    : '';
  const sessionCostSplit=(t.cloud_usd>0.004||t.local_ide_usd>0.004)
    ? split(t.cost_usd,[
        {v:t.local_ide_usd||0,label:'local IDE',cls:'s',title:'Chats with local IDE store history'},
        {v:t.cloud_usd||0,label:'cloud',cls:'c',title:'Cloud agent sessions (bc-* IDs)'},
      ])
    : usd(t.cost_usd);
  document.getElementById('cards').innerHTML=[
    ['Cash invoiced', billed
      ? split(cash, [
          {v:t.subscription_usd,label:'subscription',cls:'s'},
          {v:odCash,label:'on-demand invoiced',cls:'e'}])+meteredNote
      : split(t.cost_usd, [
          {v:t.measured_usd,label:'measured'},
          {v:t.est_usd,label:'estimated',cls:'e'}]),
      billed?'Stripe cash in this view: subscription fees plus on-demand invoices attributed to billing cycles in range (not invoice charge date). Gold daily bars are on-demand metered from usage events.':null],
    ...(billed?[['Subscription',
      usd(t.subscription_usd||0)+subBreak,
      subHint||'No plan-fee invoices in this date range']]:[]),
    ...(budget?[[`% of ${usd(budget)} budget`,(t.cost_usd/budget*100).toFixed(1)+'%',
      billed
        ? 'Spend this billing cycle vs your included-usage allowance ($70 on Pro Plus). Included usage is not extra cash beyond the subscription; on-demand is.'
        : 'Spend this month vs the configured monthly budget.']]:[]),
    ['Chats / sessions',num(t.sessions)+sessionCounts,
      'Cloud agents vs chats with local IDE store history on this machine.'],
    ...(billed&&(t.cloud_usd>0.004||t.local_ide_usd>0.004)?[['Metered by origin',
      sessionCostSplit,
      'Usage metered in this view split by cloud agent sessions vs local IDE chats.']]:[]),
    ...(t.other_sessions?[['Other-account chats',
      usd(t.other_est_usd)+`<div class="split"><span class="e">${num(t.other_sessions)} local est.</span></div>`,
      (DATA.previous_email||'A previous Cursor login')+' — not in the signed-in account invoices. Local transcript estimate only.']]:[]) ,
    ['Model requests',num(t.requests)],['Total tokens',kt(t.total_tokens)],
    ['Input',kt(t.input_tokens)],['Output',kt(t.output_tokens)],
    ['Cache read',kt(t.cache_read_tokens)],
    ['Cache write',kt(t.cache_write_tokens)],
    ['Avg $/chat',usd(t.sessions?t.cost_usd/t.sessions:0)],
    ['Avg $/request',usd4(t.requests?t.cost_usd/t.requests:0)]
  ].map(([k,v,h,cls])=>`<div class="card${cls?' '+cls:''}"${h?` title="${h}"`:''}><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  const m=DATA.view_cycle||DATA.mtd||{};
  const mtdEl=document.getElementById('mtd');
  if(m.start||m.month){
    mtdEl.style.display='';
    const plan=(m.plan||DATA.plan||'').replace(/_/g,' ');
    const isLast=m.which==='last'||m.historical;
    if(m.billed){
      document.getElementById('mtdHeading').textContent=
        isLast?'Previous billing cycle':'Plan allowance this cycle';
      document.getElementById('cycleMeta').textContent=
        isLast
          ? (plan?plan+' · ':'')+`cycle ${m.start||''} → ${m.end||''} · closed`
          : (plan?plan+' · ':'')+`cycle ${m.start||''} → ${m.end||''} · resets ${m.reset_date||''} (${m.days_left} day${m.days_left===1?'':'s'})`;
      const incLimit=m.included_limit||m.budget||0;
      const incUsed=m.included_usd||0;
      const incRem=m.included_remaining!=null?m.included_remaining:Math.max(0,incLimit-incUsed);
      const incPct=incLimit?Math.min(100,incUsed/incLimit*100):0;
      const exhausted=!isLast&&incLimit>0.004&&incRem<=0.004;
      document.getElementById('cycleIncUsed').textContent=usd(incUsed);
      document.getElementById('cycleIncLimit').textContent=m.unlimited
        ? 'unlimited'
        : (incLimit?usd(incLimit):'—');
      const incRemEl=document.getElementById('cycleIncRem');
      incRemEl.textContent=isLast
        ? (incLimit
          ? (incUsed>incLimit+0.004?`OVER plan allowance by ${usd(incUsed-incLimit)}`:`${usd(incUsed)} metered of ${usd(incLimit)} allowance`)
          : `${usd(incUsed)} metered (plan allowance)`)
        : (exhausted
          ? 'Included allowance exhausted'
          : (incRem>0.004
            ? `${usd(incRem)} remaining (${incPct.toFixed(0)}% used)`
            : (incUsed>incLimit+0.004?`OVER by ${usd(incUsed-incLimit)}`:'Included allowance exhausted')));
      incRemEl.className='allow-rem'+(exhausted||incRem<=0.004&&incUsed>incLimit+0.004?' over':'');
      const bar=document.getElementById('mtdBar');
      bar.style.width=(incLimit?Math.min(100,incPct):0)+'%';
      bar.className=incPct>=100?'over':incPct>=80?'warn':'';
      document.getElementById('cycleIncDetail').textContent=
        isLast
          ? 'Metered from billed events in this closed cycle — live allowance API is current-cycle only.'
          : exhausted
            ? [
                `Purchased plan allowance fully used (${usd(incUsed)} of ${usd(incLimit)}).`,
                (m.bonus_usd||0)>0.004
                  ? `${usd(m.bonus_usd)} bonus usage metered — provider allocation beyond your plan allowance, not extra subscription cash.`
                  : '',
                (m.on_demand_usd||0)>0.004
                  ? `${usd(m.on_demand_usd)} on-demand metered beyond included + bonus.`
                  : '',
              ].filter(Boolean).join(' ')
            : [
                `${incPct.toFixed(0)}% of ${usd(incLimit)} included allowance used.`,
                (m.bonus_usd||0)>0.004?`${usd(m.bonus_usd)} bonus usage so far.`:'',
              ].filter(Boolean).join(' ');
      const bonusCard=document.getElementById('cycleBonusCard');
      if(!isLast&&(m.bonus_usd||0)>0.004){
        bonusCard.style.display='';
        document.getElementById('cycleBonus').textContent=usd(m.bonus_usd);
      } else bonusCard.style.display='none';
      const odLimit=m.on_demand_limit||0;
      const odUsed=m.on_demand_usd||0;
      const odRem=m.on_demand_remaining!=null?m.on_demand_remaining:Math.max(0,odLimit-odUsed);
      document.getElementById('cycleOdUsed').textContent=usd(odUsed);
      document.getElementById('cycleOdLimit').textContent=odLimit?usd(odLimit):'—';
      const odRemEl=document.getElementById('cycleOdRem');
      odRemEl.textContent=isLast
        ? (odUsed>0.004?`${usd(odUsed)} on-demand metered`:'')
        : (odLimit?`${usd(odRem)} remaining`:'');
      odRemEl.className='allow-rem';
      document.getElementById('mtdTok').textContent=kt(m.total_tokens);
      document.getElementById('cycleTokFoot').textContent=
        `${num(m.requests)} model requests across ${num(m.sessions)} chats on this machine`;
      document.getElementById('mtdFoot').textContent=
        isLast
          ? `Closed-cycle totals from Cursor billed usage events in ${m.start||''} → ${m.end||''}. `
            +`Cards and charts above match this same date range.`
          : `Included allowance is your plan's ${usd(incLimit)} monthly usage budget (same dollars as cursor.com/dashboard). `
            +`Bonus usage is extra provider allocation — metered but not subscription cash. `
            +(exhausted&&((m.bonus_usd||0)>0.004||(m.on_demand_usd||0)>0.004)
              ? `This cycle continues on${(m.bonus_usd||0)>0.004?` bonus (${usd(m.bonus_usd)})`:''}${(m.bonus_usd||0)>0.004&&(m.on_demand_usd||0)>0.004?' and':''}${(m.on_demand_usd||0)>0.004?` on-demand (${usd(m.on_demand_usd)})`:''}. `
              : '')
            +(m.plan_total_usd&&!exhausted?`Plan + bonus metered this cycle: ${usd(m.plan_total_usd)}. `:'');
      document.getElementById('mtdNote').textContent=
        isLast
          ? 'On-demand here is metered overage from billed events, not necessarily cash invoiced that cycle.'
          : 'On-demand pool is the cash overage cap before Cursor pauses usage-based billing. '
            + 'Token counts are from billed events joined to local chats in this cycle.';
    } else {
      document.getElementById('mtdHeading').textContent='Usage this month';
      document.getElementById('cycleMeta').textContent=m.month||'';
      document.getElementById('cycleIncUsed').textContent=usd(m.cost_usd);
      document.getElementById('cycleIncLimit').textContent=m.budget?usd(m.budget):'—';
      document.getElementById('cycleIncRem').textContent=m.remaining!=null
        ? (m.remaining<0?`OVER by ${usd(-m.remaining)}`:`${usd(m.remaining)} remaining`):'';
      document.getElementById('cycleIncDetail').textContent='Local estimate only';
      document.getElementById('cycleBonusCard').style.display='none';
      document.getElementById('cycleOdUsed').textContent='—';
      document.getElementById('cycleOdLimit').textContent='—';
      document.getElementById('cycleOdRem').textContent='';
      document.getElementById('mtdTok').textContent=kt(m.total_tokens);
      document.getElementById('cycleTokFoot').textContent='';
      document.getElementById('mtdFoot').textContent='';
      document.getElementById('mtdNote').textContent=
        'Calendar month, local chats only — not Cursor billed usage.';
      const bar=document.getElementById('mtdBar');
      bar.style.width=(m.budget?Math.min(100,(m.cost_usd/m.budget*100)):0)+'%';
    }
  } else {
    mtdEl.style.display='none';
  }

  const daily=DATA.daily||[];
  const mx=Math.max(...daily.map(d=>d.cost_usd),0.0001);
  document.getElementById('spark').innerHTML=daily.map(d=>{
    const od=billed?(d.on_demand_usd||0):(d.est_usd||0);
    const base=Math.max(d.cost_usd-od,0);
    const h=Math.max(1,d.cost_usd/mx*100);
    const ep=d.cost_usd>0?od/d.cost_usd*100:0;
    return `<div style="height:${h}%" title="${d.day}: ${usd(d.cost_usd)} · ${num(d.requests)} req`
      +(od>0?` · ${usd(base)} ${billed?'included':'measured'} + ${usd(od)} ${billed?'on-demand':'est.'}`:'')+`">`
      +`<i class="m" style="height:${100-ep}%"></i><i class="e" style="height:${ep}%"></i></div>`;
  }).join('');
  const anySplit=daily.some(d=>billed?(d.on_demand_usd||0)>0:(d.est_usd||0)>0);
  document.getElementById('sparklabel').innerHTML=daily.length
    ? `<span class="sparkkey"><span>${daily[0].day} → ${daily[daily.length-1].day} `
      +`· peak ${usd(mx)}/day</span>`
      +`<span><b style="background:var(--acc)"></b>${billed?'allowance metered':'measured tokens'}</span>`
      +(anySplit?`<span><b style="background:#d29922"></b>${billed?'on-demand metered (USAGE_BASED events)':'estimated from transcript'}</span>`:'')
      +'</span>' : 'no data';

  const models=DATA.models||[];
  const mmax=Math.max(...models.map(m=>m.cost_usd),0.0001);
  document.getElementById('models').innerHTML=
    '<thead><tr><th>Model</th><th>Pool</th><th>Requests</th><th>Input</th><th>Cache write</th><th>Cache read</th><th>Output</th>'
    +`<th title="${costThTitle(billed)}">Cost</th><th>Share</th></tr></thead><tbody>`+
    models.map(m=>`<tr><td>${m.model}${m.est?' <span class="b est" title="Includes estimated token data">est</span>':''}${m.known_rate?'':' <span class="b more" title="No published rate for this model id; Auto rates assumed">assumed rate</span>'}</td><td>${poolBadge(m.pool||'other')}</td><td>${num(m.requests)}</td><td>${kt(m.input_tokens)}</td>
      <td>${kt(m.cache_write_tokens)}</td><td>${kt(m.cache_read_tokens)}</td><td>${kt(m.output_tokens)}</td>
      <td class="cost">${costCell(m,billed)}</td><td style="width:160px"><div class="bar" style="width:${m.cost_usd/mmax*100}%"></div></td></tr>`).join('')+
    '</tbody>';

  // Warn when billed rows lack local titles/repos — usually a stale/empty IDE store join
  // or (when nearly all are orphan) a billing UUID ↔ local composer / bc-* ID mismatch.
  try{
    const sess=DATA.sessions||[];
    if(DATA.billed && sess.length){
      const weak=sess.filter(s=>{
        const t=(s.title||'').trim();
        return s.orphan_billed || s.unattributed || !t || t==='(untitled)';
      }).length;
      if(weak/sess.length>=0.4){
        const mix=document.getElementById('mixnote');
        if(mix){
          const orphanN=sess.filter(s=>s.orphan_billed).length;
          const caHint=DATA.cloud_agents_error
            ? ` Set <code>CLOUD_AGENTS_API_KEY</code> / <code>CURSOR_API_KEY</code> (${esc(DATA.cloud_agents_error)}).`
            : (orphanN/sess.length>=0.8
              ? ` When nearly all rows are orphan/(untitled), invoice <code>conversationId</code> is usually a plain UUID that does not exist in the local store — titles-v6 joins via <code>cloudAgentId</code> or Cloud Agents API time overlap.`
              : '');
          mix.innerHTML+=(mix.innerHTML?'<br><br>':'')
            +`<b>Chat titles / PR context look thin</b> (${weak} of ${sess.length} chats`
            +(orphanN?`, ${orphanN} orphan`:'')+`). `
            +`Pools come from Cursor billing; names/PRs come from the local IDE store `
            +`(Cursor 3.0+ keeps titles in ItemTable <code>composer.composerHeaders</code>). `
            +`Confirm page stamp <b>titles-v6</b>. Click <b>Rebuild from IDE store</b> (or restart with <code>--import-ide</code>), then hard-refresh. `
            +`Check <code>/api/store</code> → <code>composer_index.headers_named</code> &gt; 0.`
            +caHint
            +` Also expand the “Cost by chat / session” section if it is collapsed (▸).`;
        }
      }
    }
  }catch(err){ console.error('storeJoinHint', err); }
  const q=DATA.q||'';
  document.getElementById('qnote').textContent=
    q?`filtered by "${q}" — ${DATA.sessions.length} chat${DATA.sessions.length===1?'':'s'}`:'';
  try{
  let rows=(DATA.sessions||[]).slice();
  rows.sort((a,b)=>((a[sortKey]>b[sortKey])-(a[sortKey]<b[sortKey]))*sortDir);
  const cols=[['title','Chat'],['top_model','Model'],['turns','Turns'],['requests','Reqs'],
    ['input_tokens','Input'],['cache_read_tokens','Cache R'],
    ['output_tokens','Output'],['total_tokens','Tokens'],['cost_usd','Cost'],['last_day','Last used']];
  document.getElementById('sessions').innerHTML=
    '<thead><tr>'+cols.map(([k,l])=>{
      const h=k==='cost_usd'?` title="${costThTitle(billed)}"`:'';
      return `<th data-k="${k}"${h}>${l}${sortKey===k?(sortDir<0?' ▼':' ▲'):''}</th>`;
    }).join('')+'</tr></thead><tbody>'+
    rows.map(s=>{
      const wt=s.shared_attribution==='git-weighted'&&s.repo_weights
        ? ' · '+Object.entries(s.repo_weights).map(([n,w])=>n.split('/').pop()+' '+(w*100).toFixed(0)+'%').join(' · ')
        : '';
      const detailParts=[];
      if(s.repository) detailParts.push(s.repository);
      if(s.branch) detailParts.push(s.branch);
      if(s.subtitle && s.subtitle!==s.title) detailParts.push(s.subtitle);
      const detailLine=detailParts.length?detailParts.join(' · '):'—';
      const sub=s.billing_note
        ? `<div class="sub billing-note">${esc(s.billing_note)}</div>`
          + (detailParts.length?`<div class="sub">${esc(detailLine)}${wt}</div>`:'')
        : `<div class="sub">${esc(detailLine)}${wt}</div>`;
      const sharedBadge=s.shared_attribution==='git-weighted'&&s.repo_weights
        ? ` <span class="b gitinf" title="Multi-root split by git commits/PRs during this chat (not equal shares)">git split · ${Object.keys(s.repo_weights).length} repos</span>`
        : (s.shared_attribution==='unconfirmed'&&(s.repo_split>1||0)
          ? ` <span class="b more" title="Multi-root workspace open but no git activity during this chat to assign repos">shared unconfirmed</span>`
          : (s.repo_split>1?` <span class="b more" title="Multiple tracked repos">${s.repo_split} repos</span>`:''));
      const unBadge=s.unattributed
        ? ` <span class="b more" title="Billed usage with no conversation ID on the invoice">no chat ID</span>`
        : (s.orphan_billed
          ? ` <span class="b more" title="Conversation ID on invoice but no local chat on this machine">orphan</span>`
          : sharedBadge);
      const cloudBadge=s.cloud_agent
        ? ` <a class="b repo" href="${esc(s.cloud_url||('https://cursor.com/agents/'+(s.cloud_agent_id||s.session_id)))}" target="_blank" title="Open cloud agent">cloud</a>`
        : '';
      const gitBadge=(s.git_correlation&&s.git_correlation.matched)
        ? ` <span class="b gitinf" title="Repos inferred from nearby git commits">git matched</span>`:'';
      return `<tr class="row${s.unattributed||s.orphan_billed?' unattr':''}" data-id="${s.session_id}">
      <td><span class="expand">▸</span> ${esc(s.title)}${s.title_source?` <span class="b more" title="Title recovered from ${esc(s.title_source)}">${esc(s.title_source)}</span>`:``}${s.title_status?` <span class="b more" title="${esc(s.title_status)}">no local title</span>`:``}${cloudBadge}${s.est?' <span class="b est" title="Includes tokens inferred from transcript length">est</span>':''}${unBadge}${gitBadge}${s.billed&&s.account_label&&(DATA.billing_emails||[]).length>1?` <span class="b more">${esc(s.account_label)}</span>`:''}${s.billed===false&&DATA.billed?` <span class="b more" title="On this machine but not billed to ${esc((DATA.billing_emails||[DATA.billing_email]).filter(Boolean).join(' / ')||'the signed-in account')}${s.account_label?' — likely '+esc(s.account_label):''}">${esc(s.account_label||'other account')}</span>`:''}${s.subagent?' <span class="b more">subagent</span>':''}${sub}${badges(s.refs)}</td>
      <td>${s.top_model}${s.models>1?' <span class="sub">+'+(s.models-1)+'</span>':''}</td>
      <td>${num(s.turns)}</td><td>${num(s.requests)}</td><td>${kt(s.input_tokens)}</td>
      <td>${kt(s.cache_read_tokens)}</td><td>${kt(s.output_tokens)}</td>
      <td>${kt(s.total_tokens)}</td><td class="cost">${costCell(s,billed)}</td><td class="sub">${s.last_day}</td></tr>`;
    }).join('')+
    '</tbody>';
  renderRollup();
  document.querySelectorAll('#sessions th').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; sortDir = sortKey===k ? -sortDir : -1; sortKey=k; render();});
  document.querySelectorAll('#sessions tr.row').forEach(tr=>tr.onclick=e=>{
    if(e.target.tagName==='A') return; toggle(tr);});
  document.getElementById('foot').textContent=
    `${rows.length} chats shown · ${DATA.billed?'Cursor billed usage events':'local transcript estimate'}`
    +(billed?' · Cost: allowance (purple) + on-demand (gold)':'')
    +` · ${DATA.db}`;
  }catch(err){
    console.error('renderSessions', err);
    const el=document.getElementById('sessions');
    if(el) el.innerHTML='<tr><td class="sub">Could not render chat list ('+esc(err&&err.message||err)
      +'). Check the browser console, then try Rebuild from IDE store + Refresh.</td></tr>';
  }
}
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function toggle(tr){
  if(tr.nextElementSibling&&tr.nextElementSibling.classList.contains('turns')){
    tr.nextElementSibling.remove(); tr.querySelector('.expand').textContent='▸'; return;}
  tr.querySelector('.expand').textContent='▾';
  const ph=document.createElement('tr'); ph.className='turns';
  ph.innerHTML='<td colspan="10"><span class="sub">Loading turns…</span></td>';
  tr.after(ph);
  const turns=await (await fetch('/api/turns?session_id='+encodeURIComponent(tr.dataset.id))).json();
  ph.remove();
  if(!tr.isConnected||tr.querySelector('.expand').textContent==='▸')return;
  const sess=(DATA.sessions||[]).find(s=>s.session_id===tr.dataset.id);
  let gitHdr='';
  if(sess&&sess.git_correlation&&sess.git_correlation.matched){
    const gc=sess.git_correlation;
    gitHdr=`<div class="git-corr"><b>Git correlation</b> (±${gc.window_hours}h of each billed call) — `
      +`${usd(gc.matched_cost_usd||0)} matched`
      +(gc.unmatched_cost_usd>0.005?`, ${usd(gc.unmatched_cost_usd)} with no nearby commits`:``)
      +`<br>`
      +gc.repos.map(r=>{
        const prs=(r.prs||[]).map(p=>`<a href="${prUrl(p)}" target="_blank">#${esc(p.split('#').pop())}</a>`).join(' ');
        const samp=(r.sample||[]).map(c=>`${c.sha} (${c.delta_h>=0?'+':''}${c.delta_h}h) ${esc(c.subject)}`).join('<br>');
        return `<div style="margin-top:6px"><b>${esc(r.name)}</b> · ${usd(r.cost_usd)} inferred · ${r.turn_matches||r.commits||0} billed turn${(r.turn_matches||r.commits||0)===1?'':'s'} near commits`
          +(prs?' · PR '+prs:'')+(samp?'<br><span class="sub">'+samp+'</span>':'')+`</div>`;
      }).join('')+`</div>`;
  }
  const td=document.createElement('tr'); td.className='turns';
  td.innerHTML=`<td colspan="10">${gitHdr}<table>${turns.map(t=>{
    const gn=(t.git_nearby||[]).map(g=>{
      const sign=g.delta_sec>=0?'+':'';
      const pr=g.pr?` · <a href="${prUrl(g.pr)}" target="_blank">PR #${esc(g.pr.split('#').pop())}</a>`:'';
      return `<span class="git-hit">${esc(g.repo.split('/').pop())} `
        +`<a href="https://github.com/${esc(g.repo)}/commit/${g.sha}" target="_blank">${g.sha}</a> `
        +`${sign}${Math.round(g.delta_sec/60)}m${pr} · ${esc(g.subject.slice(0,72))}</span>`;
    }).join('');
    return `<tr><td>Turn ${t.turn_index} <span class="sub">${(t.started_at||'').slice(0,16).replace('T',' ')}</span>`
     +(t.kind?' <span class="b '+(t.on_demand?'est':'more')+'">'+t.kind+'</span>':'')
     +(t.est?' <span class="b est">est</span>':'')+`${gn?`<div class="git-near">${gn}</div>`:''}</td>
     <td>${t.model}</td><td>${num(t.requests)} req</td><td>${kt(t.input_tokens)} in</td>
     <td>${kt(t.cache_write_tokens||0)} cw</td><td>${kt(t.cache_read_tokens||0)} cr</td>
     <td>${kt(t.output_tokens)} out</td><td>${kt(t.total_tokens)} tok</td>
     <td class="cost">${costCell({cost_usd:t.cost_usd,
       on_demand_usd:t.on_demand?(t.cost_usd||0):0,
       est_usd:t.est?(t.cost_usd||0):0}, DATA.billed, true)}</td></tr>`;
  }).join('')}</table></td>`;
  tr.after(td);
}
document.getElementById('refresh').onclick=()=>load(true);
async function importStore(mode, body){
  busy(true, mode==='upload'
    ? 'Merging uploaded state.vscdb into the dashboard store…'
    : 'Merging Cursor IDE state.vscdb into the dashboard store…');
  try{
    const r=await fetch('/api/import-store?mode='+encodeURIComponent(mode),{
      method:'POST', body: body||null,
      headers: body?{'Content-Type':'application/octet-stream'}:{},
    });
    const j=await r.json();
    if(j.error){document.getElementById('err').textContent='Error: '+j.error;return;}
    document.getElementById('err').textContent='';
    await load(true);
  }catch(err){
    document.getElementById('err').textContent='Error: '+err;
  }finally{ busy(false); }
}
document.getElementById('importIde').onclick=()=>importStore('ide');
document.getElementById('uploadStore').onchange=async(ev)=>{
  const f=ev.target.files&&ev.target.files[0];
  ev.target.value='';
  if(!f) return;
  importStore('upload', await f.arrayBuffer());
};
document.querySelectorAll('.tabs button[data-t]').forEach(b=>b.onclick=()=>{tab=b.dataset.t;renderRollup();});
document.querySelectorAll('#sessionOriginTabs button').forEach(b=>b.onclick=()=>{sessionOrigin=b.dataset.o;renderRollup();});
document.getElementById('rq').oninput=()=>DATA&&renderRollup();
let qtimer=null;
document.getElementById('q').oninput=()=>{clearTimeout(qtimer);qtimer=setTimeout(load,250);};
document.querySelectorAll('.clearx').forEach(btn=>{
  const inp=document.getElementById(btn.dataset.for);
  const paint=()=>inp.parentElement.classList.toggle('has', inp.value!=='');
  inp.addEventListener('input', paint);
  btn.onclick=()=>{
    if(inp.value==='') return;
    inp.value=''; paint();
    inp.dispatchEvent(new Event('input',{bubbles:true}));
    inp.focus();
  };
  paint();
});
document.getElementById('start').onchange=load;
document.getElementById('end').onchange=load;
function isCyclePreset(v){return v==='mtd'||v==='lastcycle';}
function presetRange(v){
  const ymd=d=>{const p=n=>String(n).padStart(2,'0');
    return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());};
  const n=new Date(), today=new Date(n.getFullYear(),n.getMonth(),n.getDate());
  const back=k=>{const d=new Date(today); d.setDate(d.getDate()-k); return d;};
  const out=(a,b)=>[a?ymd(a):'', b?ymd(b):''];
  if(v==='all') return out(null,null);
  if(v==='today') return out(today,today);
  if(v==='yesterday') return out(back(1),back(1));
  if(v==='week') return out(back((today.getDay()+6)%7),today);
  if(v==='mtd'){
    if(DATA&&DATA.cycles&&DATA.cycles.current&&DATA.cycles.current.start)
      return [DATA.cycles.current.start, DATA.cycles.current.end||ymd(today)];
    if(DATA&&DATA.mtd&&DATA.mtd.billed&&DATA.mtd.start)
      return [DATA.mtd.start, DATA.mtd.end||ymd(today)];
    return out(new Date(today.getFullYear(),today.getMonth(),1),today);
  }
  if(v==='lastcycle'){
    if(DATA&&DATA.cycles&&DATA.cycles.last)
      return [DATA.cycles.last.start, DATA.cycles.last.end];
    if(DATA&&DATA.mtd&&DATA.mtd.last_start)
      return [DATA.mtd.last_start, DATA.mtd.last_end];
    return out(new Date(today.getFullYear(),today.getMonth()-1,1),
               new Date(today.getFullYear(),today.getMonth(),0));
  }
  return out(back(+v-1),today);
}
let PRESET_LOCK=null;
function applyPreset(v){
  const [a,b]=presetRange(v);
  document.getElementById('start').value=a;
  document.getElementById('end').value=b;
  PRESET_LOCK={v:v,start:a,end:b};
}
function syncPreset(){
  if(!PRESET_LOCK||PRESET_LOCK.v==='all') return false;
  const S=document.getElementById('start'), E=document.getElementById('end');
  if(S.value!==PRESET_LOCK.start||E.value!==PRESET_LOCK.end) return false;
  const [a,b]=presetRange(PRESET_LOCK.v);
  if(a===PRESET_LOCK.start&&b===PRESET_LOCK.end) return false;
  S.value=a; E.value=b; PRESET_LOCK={v:PRESET_LOCK.v,start:a,end:b};
  return true;
}
document.getElementById('preset').onchange=e=>{
  const v=e.target.value;
  if(isCyclePreset(v)){
    if(DATA&&DATA.cycles) applyPreset(v);
    else PRESET_LOCK={v, start:'', end:''};
  } else applyPreset(v);
  load();
};
let timer=null;
function setAuto(on){ clearInterval(timer); timer = on ? setInterval(load,900000) : null; }
document.getElementById('auto').onchange=e=>setAuto(e.target.checked);
setAuto(document.getElementById('auto').checked);
document.querySelectorAll('main section').forEach(s=>{
  const h=s.querySelector('h2'); if(!h) return;
  // Prefer stable section ids so heading stamp changes (pools-vN) do not orphan fold state.
  const key='fold:'+(s.id || h.textContent.trim().slice(0,40));
  const b=document.createElement('button');
  b.className='fold'; b.title='Collapse or expand this section';
  const paint=c=>{b.textContent=c?'▸':'▾'; b.setAttribute('aria-expanded',!c);};
  const start=localStorage.getItem(key)==='1';
  if(start) s.classList.add('collapsed');
  paint(start);
  b.onclick=()=>{const c=s.classList.toggle('collapsed');
    paint(c); localStorage.setItem(key,c?'1':'0');};
  h.prepend(b);
});
load();</script></body></html>
"""


def main():
    global DB_PATH, JIRA_BASE, CREDIT_BUDGET, STATE_PATH, API_ENABLED
    global EMAIL_TO, EMAIL_FROM, GMAIL_CREDENTIALS, GMAIL_TOKEN_PATH
    global CLOUD_AGENTS_ENABLED, CLOUD_AGENTS_API_KEY, CLOUD_AGENTS_CACHE_PATH
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="Cursor state.vscdb (default: <Cursor User>/globalStorage/state.vscdb)")
    ap.add_argument("--merge-db", action="append", default=[],
                    help="Extra state.vscdb to merge into the dashboard working store "
                         "(repeatable). Writes ~/.config/cursor-dashboard/merged-state.vscdb "
                         "(or %%APPDATA%%\\cursor-dashboard). Never modifies the IDE DB.")
    ap.add_argument("--import-ide", action="store_true",
                    help="Find local Cursor IDE state.vscdb install(s) and merge them into "
                         "the dashboard working store before serving.")
    ap.add_argument("--cloud-agents", action="store_true", default=None,
                    help="Load ALL cloud agents via api.cursor.com "
                         "(CLOUD_AGENTS_API_KEY or CURSOR_API_KEY) and "
                         "merge them into the session list. Default: on when a key is set.")
    ap.add_argument("--no-cloud-agents", action="store_true",
                    help="Do not fetch or merge Cloud Agents API sessions.")
    ap.add_argument("--cloud-agents-cache", default="",
                    help="Path to cloud-agents-cache.json (default: "
                         "%%APPDATA%%/cursor-dashboard/cloud-agents-cache.json).")
    ap.add_argument("--jira-base", default=JIRA_BASE_DEFAULT,
                    help="Jira base URL, e.g. https://acme.atlassian.net. "
                         "Auto-detected from your chat history when omitted.")
    ap.add_argument("--jira-keys", default="",
                    help="Extra Jira project keys to recognize, comma separated (e.g. ABC,DEF)")
    ap.add_argument("--budget", default=None,
                    help="Monthly included-usage budget in USD for the Usage this month banner "
                         f"(e.g. 20 or 20.00). Default {DEFAULT_BUDGET:g}. "
                         "Editable in the banner and remembered between runs. "
                         "Env: CURSOR_DASH_BUDGET")
    ap.add_argument("--email-to", default=None,
                    help="Digest recipient. Default: the signed-in Cursor license email "
                         "(cursorAuth/cachedEmail). Override with this flag or "
                         "CURSOR_DASH_EMAIL_TO.")
    ap.add_argument("--email-from", default=EMAIL_FROM,
                    help="Digest sender address override. Default: the Gmail account "
                         "that authorized --gmail-auth. Env: CURSOR_DASH_EMAIL_FROM")
    ap.add_argument("--gmail-credentials", default=GMAIL_CREDENTIALS,
                    help="Google Cloud OAuth Desktop client JSON. Default: "
                         "%%APPDATA%%/cursor-dashboard/gmail-oauth-client.json. "
                         "Env: CURSOR_DASH_GMAIL_CREDENTIALS")
    ap.add_argument("--gmail-token", default=GMAIL_TOKEN_PATH,
                    help="Where to store the Gmail OAuth refresh token. Default: "
                         "beside the client JSON. Env: CURSOR_DASH_GMAIL_TOKEN")
    ap.add_argument("--gmail-auth", action="store_true",
                    help="Open a browser to authorize Gmail send access, save the "
                         "token, then exit.")
    ap.add_argument("--send-digest", action="store_true",
                    help="Send the digest immediately on startup, then exit. For testing "
                         "or for driving the digest from a scheduled task.")
    ap.add_argument("--no-digest", action="store_true",
                    help="Disable the daily digest email.")
    ap.add_argument("--no-api", action="store_true",
                    help="Skip Cursor billed-usage API and use local transcript estimates.")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    API_ENABLED = not args.no_api
    if args.cloud_agents_cache:
        CLOUD_AGENTS_CACHE_PATH = os.path.abspath(os.path.expanduser(args.cloud_agents_cache))
    if args.no_cloud_agents:
        CLOUD_AGENTS_ENABLED = False
    elif args.cloud_agents:
        CLOUD_AGENTS_ENABLED = True
    else:
        # Auto-on when an API key or an existing catalog cache is present.
        CLOUD_AGENTS_ENABLED = bool(_cloud_api_key() or os.path.isfile(_cloud_agents_cache_path()))
    DB_PATH = args.db
    if args.import_ide or args.merge_db:
        dest = _merged_store_path()
        sources = list(args.merge_db or [])
        if args.import_ide:
            ide = ide_store_candidates()
            if not ide and not sources:
                searched = [
                    os.path.join(p, "globalStorage", "state.vscdb")
                    for p in _cursor_user_dir_candidates()
                ]
                raise SystemExit(
                    "No IDE state.vscdb found to merge. Searched:\n  - "
                    + "\n  - ".join(searched)
                    + "\nPass --merge-db PATH or upload via the dashboard.")
            sources = [c["path"] for c in ide] + sources
        seed = DB_PATH if os.path.isfile(DB_PATH) else None
        summary = merge_stores(sources, dest, seed_path=seed)
        DB_PATH = use_store(dest)
        print(f"Merged {len(summary['sources'])} store(s) -> {DB_PATH}", flush=True)
        for src in summary["sources"]:
            print(f"  + {src['path']}", flush=True)
        print(f"  tables: {summary.get('tables')}", flush=True)
    STATE_PATH = os.path.join(os.path.dirname(DB_PATH), "cost-dashboard-state.json")
    EMAIL_FROM = args.email_from.strip()
    GMAIL_CREDENTIALS = (args.gmail_credentials or "").strip()
    GMAIL_TOKEN_PATH = (args.gmail_token or "").strip()
    if args.no_digest:
        EMAIL_TO = ""
    elif args.email_to is not None:
        EMAIL_TO = args.email_to.strip()
    elif not EMAIL_TO:
        EMAIL_TO = _cursor_license_email()
    JIRA_BASE = (args.jira_base or JIRA_BASE_DEFAULT).rstrip("/")
    if args.budget is not None or CREDIT_BUDGET:
        raw = args.budget if args.budget is not None else CREDIT_BUDGET
        try:
            CREDIT_BUDGET = float(str(raw).replace(",", "").replace("_", "").replace("$", ""))
        except ValueError:
            raise SystemExit(f"--budget must be a number, got: {raw!r}")
        budget_set(CREDIT_BUDGET)
    else:
        saved = _state_load()
        if "monthly_budget_usd" in saved:
            CREDIT_BUDGET = float(saved["monthly_budget_usd"])
        elif "ai_credits" in saved:
            CREDIT_BUDGET = float(saved["ai_credits"]) / 100.0
        else:
            CREDIT_BUDGET = DEFAULT_BUDGET
    JIRA_KEY_ALLOW.update(k.strip().upper() for k in args.jira_keys.split(",") if k.strip())
    if args.gmail_auth:
        token = gmail_auth()
        print(f"Gmail token saved for {token.get('email')} at {_gmail_token_file()}")
        if not args.send_digest:
            return
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Cursor store not found: {DB_PATH}\n"
                         f"Pass --db PATH if your Cursor data lives elsewhere.")
    if args.send_digest:
        if not EMAIL_TO:
            raise SystemExit("--send-digest needs a Cursor license email or --email-to")
        day, recent = _find_digest_day(datetime.date.today().isoformat())
        if not day:
            raise SystemExit(f"No Cursor activity in the last {DIGEST_LOOKBACK_DAYS} days")
        send_digest(day, recent, interactive=False)
        print(f"digest for {day} sent to {EMAIL_TO}")
        return
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Cursor cost dashboard -> {url}  (db: {DB_PATH})", flush=True)
    if API_ENABLED:
        print("  costs from Cursor billed usage events (cursor.com)", flush=True)
    else:
        print("  --no-api: local transcript estimates only", flush=True)
    if EMAIL_TO:
        off = "" if _digest_enabled() else "  [switched off in the dashboard]"
        ready, gmail_email = _gmail_ready()
        if ready:
            via = f"Gmail API as {gmail_email}" if gmail_email else "Gmail API"
        elif os.path.isfile(_gmail_credentials_path() or ""):
            via = "Gmail API  [run --gmail-auth]"
        else:
            via = "Gmail API  [save OAuth client JSON, then --gmail-auth]"
        print(f"  daily digest -> {EMAIL_TO} via {via}{off}", flush=True)
    jira_email, _ = _jira_credentials()
    if jira_email:
        print(f"  Jira API -> {JIRA_BASE} as {jira_email}", flush=True)
    else:
        print("  Jira API -> not configured  [run store_atlassian_token.ps1]", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
