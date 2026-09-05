"""Refreshable GitHub Copilot chat cost dashboard.

Reads the local Copilot session store (read-only) and serves an HTML dashboard
showing per-chat/session cost in USD and token usage.

Cost model: the store records `total_nano_aiu` per request. Published model
rates (e.g. Claude Opus 4.6: $5/M input, $6.25/M cache-write, $0.50/M cache-read,
$25/M output) map to those values at 1e11 nano-AIU == 1 USD.

Usage:  python copilot_cost_dashboard.py [--port 8787] [--db PATH]
"""

import argparse
import collections
import datetime
import glob
import json
import os
import re
import smtplib
import sqlite3
import threading
import traceback
import webbrowser
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

NANO_AIU_PER_USD = 1e11
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".copilot", "session-store.db")

# --- Jira linking -----------------------------------------------------------
# Base URL is auto-discovered from any Atlassian links found in your own chat
# history. Override with --jira-base or the COPILOT_DASH_JIRA_BASE env var.
JIRA_BASE_DEFAULT = os.environ.get("COPILOT_DASH_JIRA_BASE", "")
# Project keys are discovered automatically from atlassian.net/browse/<KEY>-<n>
# links in your chats. Add extra keys (for tickets only ever mentioned in bare
# form, e.g. "ABC-123") via --jira-keys or COPILOT_DASH_JIRA_KEYS="ABC,DEF".
JIRA_KEY_ALLOW = {k.strip().upper()
                  for k in os.environ.get("COPILOT_DASH_JIRA_KEYS", "").split(",")
                  if k.strip()}
# Never Jira keys, even though they match the ABC-123 shape.
JIRA_KEY_DENY = {"UTF", "CVE", "ISO", "RFC", "SHA", "AES", "RSA", "GPT", "API", "UTC",
                 "TLS", "SSL", "HTTP", "SQL", "JSON", "YAML", "BASE", "X", "IPV", "MD",
                 "ISO8601", "SOC", "PCI", "AD", "V", "PY", "NET", "SP", "EC", "AMD",
                 "ARM", "GB", "MB", "KB", "TB", "US", "EU", "UK", "ID", "IPV4", "IPV6"}
GH_RESERVED = {"settings", "orgs", "apps", "features", "marketplace", "repos", "enterprises",
               "notifications", "pulls", "issues", "search", "topics", "sponsors", "users",
               "collections", "codespaces", "login", "join", "about", "blog", "site",
               "rest", "api", "raw", "gist", "www", "graphql", "assets", "avatars"}

# --- License / billing cycle ------------------------------------------------
# Copilot Settings reports usage as "AI credits" (e.g. 10,709 / 60,000 per
# cycle). The store records total_nano_aiu, where 1 AI credit == 1e9 nano-AIU.
# Your monthly allowance is not in the store, so it is editable in the banner
# and remembered in the state file. --ai-credits or COPILOT_DASH_AI_CREDITS
# override it at launch; with neither, DEFAULT_CREDITS applies.
NANO_AIU_PER_CREDIT = 1e9
DEFAULT_CREDITS = 20000


def _int_env(name, default=0):
    raw = os.environ.get(name, "").replace(",", "").replace("_", "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


CREDIT_BUDGET = _int_env("COPILOT_DASH_AI_CREDITS")

# --- Daily digest email -----------------------------------------------------
# Opt-in: with no recipient configured the whole feature stays dormant, so the
# dashboard behaves identically for everyone who has not asked for mail.
EMAIL_TO = os.environ.get("COPILOT_DASH_EMAIL_TO", "")
EMAIL_FROM = os.environ.get("COPILOT_DASH_EMAIL_FROM", "")
SMTP_HOST = os.environ.get("COPILOT_DASH_SMTP_HOST", "smtp.wrberkley.com")
SMTP_PORT = _int_env("COPILOT_DASH_SMTP_PORT", 25)
# Remembers which day's digest already went out, so a refresh only mails once.
# Kept beside the session store rather than in the repo: it is per-machine state.
STATE_PATH = os.path.join(os.path.dirname(DEFAULT_DB), "cost-dashboard-state.json")
DIGEST_LOCK = threading.Lock()
DIGEST_STATUS = {"last_error": "", "sending": False}
# How far back to hunt for the last day that had activity, and how many active
# days the "average" comparison in the mail is taken over.
DIGEST_LOOKBACK_DAYS = 60
DIGEST_AVG_DAYS = 7

DB_PATH = DEFAULT_DB
JIRA_BASE = JIRA_BASE_DEFAULT


def connect():
    # Read-only so we never interfere with the running Copilot app.
    uri = "file:{}?mode=ro".format(DB_PATH.replace("?", "%3f").replace("#", "%23"))
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


COST = "SUM(u.total_nano_aiu) / {} ".format(NANO_AIU_PER_USD)

# The store writes created_at in UTC ("2026-09-03T00:45:52Z"), but a day on this
# dashboard means a day where the user lives: late-evening work would otherwise be
# filed under tomorrow. VS Code transcripts are already bucketed locally, so this
# also keeps the two sources on the same calendar.
DAY = "date(u.created_at, 'localtime')"

# Open-ended date bounds, used as the "no range selected" sentinels.
MIN_DAY, MAX_DAY = "0000-01-01", "9999-12-31"

SESSION_SQL = f"""
SELECT
  u.session_id                                        AS session_id,
  COALESCE(NULLIF(s.summary, ''), '(untitled)')       AS title,
  COALESCE(NULLIF(s.repository, ''), '')              AS repository,
  COALESCE(NULLIF(s.branch, ''), '')                  AS branch,
  COUNT(*)                                            AS requests,
  COUNT(DISTINCT u.turn_index)                        AS turns,
  COUNT(DISTINCT u.model)                             AS models,
  MAX(u.model)                                        AS top_model,
  SUM(u.input_tokens)                                 AS input_tokens,
  SUM(u.output_tokens)                                AS output_tokens,
  SUM(COALESCE(u.cache_read_tokens, 0))               AS cache_read_tokens,
  SUM(COALESCE(u.cache_write_tokens, 0))              AS cache_write_tokens,
  SUM(COALESCE(u.reasoning_tokens, 0))                AS reasoning_tokens,
  SUM(u.input_tokens + u.output_tokens
      + COALESCE(u.cache_read_tokens, 0)
      + COALESCE(u.cache_write_tokens, 0))            AS total_tokens,
  {COST}                                              AS cost_usd,
  SUM(COALESCE(u.duration_ms, 0)) / 1000.0            AS duration_s,
  MIN({DAY})                                          AS first_day,
  MAX({DAY})                                          AS last_day
FROM assistant_usage_events u
LEFT JOIN sessions s ON s.id = u.session_id
WHERE {DAY} BETWEEN ? AND ?
GROUP BY u.session_id
ORDER BY cost_usd DESC
"""

MODEL_SQL = f"""
SELECT u.model                                        AS model,
       COUNT(*)                                       AS requests,
       SUM(u.input_tokens)                            AS input_tokens,
       SUM(u.output_tokens)                           AS output_tokens,
       SUM(COALESCE(u.cache_read_tokens, 0))          AS cache_read_tokens,
       SUM(COALESCE(u.cache_write_tokens, 0))         AS cache_write_tokens,
       {COST}                                         AS cost_usd
FROM assistant_usage_events u
WHERE {DAY} BETWEEN ? AND ?
GROUP BY u.model
ORDER BY cost_usd DESC
"""

DAILY_SQL = f"""
SELECT {DAY}                                          AS day,
       COUNT(*)                                       AS requests,
       COUNT(DISTINCT u.session_id)                   AS sessions,
       SUM(u.input_tokens + u.output_tokens
           + COALESCE(u.cache_read_tokens, 0)
           + COALESCE(u.cache_write_tokens, 0))       AS total_tokens,
       {COST}                                         AS cost_usd
FROM assistant_usage_events u
WHERE {DAY} BETWEEN ? AND ?
GROUP BY day
ORDER BY day
"""

TURN_SQL = f"""
SELECT u.turn_index                                   AS turn_index,
       COUNT(*)                                       AS requests,
       MAX(u.model)                                   AS model,
       SUM(u.input_tokens)                            AS input_tokens,
       SUM(u.output_tokens)                           AS output_tokens,
       SUM(COALESCE(u.cache_read_tokens, 0))          AS cache_read_tokens,
       SUM(COALESCE(u.cache_write_tokens, 0))         AS cache_write_tokens,
       SUM(u.input_tokens + u.output_tokens
           + COALESCE(u.cache_read_tokens, 0)
           + COALESCE(u.cache_write_tokens, 0))       AS total_tokens,
       {COST}                                         AS cost_usd,
       MIN(u.created_at)                              AS started_at
FROM assistant_usage_events u
WHERE u.session_id = ?
GROUP BY u.turn_index
ORDER BY u.turn_index
"""


def fetch(sql, params):
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# Reference extraction: Jira tickets, GitHub repos and pull requests mentioned
# (or created) inside each chat's turn text.
# --------------------------------------------------------------------------

RE_JIRA_URL = re.compile(r"https?://([\w.-]+\.atlassian\.net)/browse/([A-Z][A-Z0-9]{1,9}-\d+)")
RE_JIRA_ANY = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]{1,9}-\d+)")
RE_JIRA_BARE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")
RE_PR = re.compile(r"(?<![\w.])github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
RE_REPO_URL = re.compile(r"(?<![\w.])github\.com/([\w.-]+)/([\w.-]+)")
RE_CREATED = re.compile(
    r"(gh pr create|(?:created|opened|raised|submitted)\s+(?:a\s+|the\s+|new\s+|draft\s+)*"
    r"(?:pull request|PR)\b|(?:pull request|PR)\s+(?:#\d+\s+)?(?:was\s+)?(?:successfully\s+)?"
    r"(?:created|opened))", re.I)


def clean_repo(owner, repo):
    """Normalize a github.com/<owner>/<repo> pair, or return None if it is not a repo."""
    repo = repo.rstrip(".").removesuffix(".git")
    if not repo or owner.lower() in GH_RESERVED or repo.lower() in GH_RESERVED:
        return None
    return f"{owner}/{repo}"

_REF_CACHE = {"stamp": None, "data": None}

# The desktop app keeps its own store beside the CLI session store. The CLI store
# holds a *generated* summary; the app store holds the name the user actually gave
# the chat, which is where a Jira key usually gets added - often long after the
# conversation itself, and frequently without the key ever being typed in the chat.
APP_DB_PATH = ""


def _app_db_path():
    return APP_DB_PATH or os.path.join(os.path.dirname(DB_PATH), "data.db")


def _app_titles():
    """{session_id: name} from the app store, or {} if it is absent/unreadable.

    Entirely optional: this dashboard must keep working against a bare session
    store, so every failure here degrades to "no names" rather than an error.
    """
    path = _app_db_path()
    if not os.path.exists(path):
        return {}
    con = None
    try:
        uri = "file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23"))
        con = sqlite3.connect(uri, uri=True, timeout=5)
        return {r[0]: r[1] for r in con.execute(
            "SELECT id, title FROM sessions"
            " WHERE title IS NOT NULL AND title != ''").fetchall()}
    except Exception:
        return {}
    finally:
        if con is not None:
            con.close()


def _dynamic_jira_keys(texts):
    """Discover Jira project keys AND the Jira host from the user's own chats."""
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


def _build_refs(rows, sess_repo, allow):
    """Shared ref extraction. rows = [(id, text), ...] -> {id: {jira, prs, repos}}."""
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
        for owner, repo in RE_REPO_URL.findall(text):
            name = clean_repo(owner, repo)
            if name:
                d["repos"].setdefault(name, "mentioned")

    for sid, repo in (sess_repo or {}).items():
        d = out.setdefault(sid, {"jira": {}, "prs": {}, "repos": {}})
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


def extract_refs():
    """Return {session_id: {jira:[...], prs:[...], repos:[...]}} with caching."""
    titles = _app_titles()
    with connect() as con:
        stamp = con.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM turns").fetchone()
        # Renaming a chat touches neither turns nor the CLI store, so the names
        # themselves have to be part of the cache key - otherwise a key added to a
        # session name would not appear until the server was restarted.
        stamp = tuple(stamp) + (tuple(sorted(titles.items())),)
        if _REF_CACHE["stamp"] == stamp:
            return _REF_CACHE["data"]
        rows = con.execute(
            "SELECT session_id, COALESCE(user_message,'') || ' ' ||"
            " COALESCE(assistant_response,'') FROM turns").fetchall()
        sess_repo = {r[0]: r[1] for r in con.execute(
            "SELECT id, repository FROM sessions WHERE repository IS NOT NULL"
            " AND repository != ''").fetchall()}

    # A name is treated as just more text for the session, so a Jira key, PR link
    # or repo link in the name is picked up exactly as one typed in the chat.
    rows = [tuple(r) for r in rows] + sorted(titles.items())
    allow = JIRA_KEY_ALLOW | _dynamic_jira_keys(t for _, t in rows)
    data = _build_refs(rows, sess_repo, allow)
    _REF_CACHE["stamp"], _REF_CACHE["data"] = stamp, data
    return data


# --------------------------------------------------------------------------
# VS Code Copilot Chat (ESTIMATED).
#
# VS Code stores chat transcripts under
#   <appdata>/Code[ - Insiders]/User/workspaceStorage/<id>/chatSessions/*
# but records NO token counts and NO cost/AIU fields - only the model id and
# message text. Everything below is therefore an estimate, kept in its own
# section so it can never contaminate the exact CLI figures.
#
# Estimator: tokens ~ characters / 4. For each request the newly typed prompt is
# charged at the input rate, the whole preceding transcript is charged at the
# cache-read rate (chat resends the conversation every turn), and the generated
# reply is charged at the output rate.
# --------------------------------------------------------------------------

CHARS_PER_TOKEN = 4
# Cents per 1M tokens: (input, output, cache read). claude-sonnet-4.6 is taken
# verbatim from the pricing metadata VS Code itself writes into the transcripts.
VSCODE_RATES = {
    "claude-sonnet-4.6": (300, 1500, 30),
    "claude-sonnet-4.5": (300, 1500, 30),
    "claude-sonnet-4": (300, 1500, 30),
    "claude-opus-4.6": (500, 2500, 50),
    "claude-opus-4.5": (500, 2500, 50),
    "claude-opus-4.1": (1500, 7500, 150),
    "claude-haiku-4.5": (100, 500, 10),
    "gpt-5-mini": (25, 200, 2.5),
    "gpt-5": (125, 1000, 12.5),
    "gpt-4.1": (200, 800, 50),
    "gpt-4o": (250, 1000, 125),
    "o4-mini": (110, 440, 27.5),
    "gemini-2.5-pro": (125, 1000, 31),
}
VSCODE_RATE_DEFAULT = (300, 1500, 30)   # unknown / "auto": assume Sonnet class
# Models cap how much history can be resent; past this VS Code truncates or
# summarizes, so an uncapped running transcript would overstate long chats.
CONTEXT_WINDOW_TOKENS = 128_000

# GitHub bills Copilot chat in VS Code by *premium requests*, not by tokens:
# each request costs one premium request times a per-model multiplier, and
# overage is billed at a flat rate. That is a completely different basis to the
# token model above, so the two are shown side by side as a range rather than
# being averaged. Multipliers change over time - verify against
# docs.github.com before relying on the figure.
PREMIUM_USD = 0.04
PREMIUM_MULTIPLIERS = {
    "gpt-4o": 0.0, "gpt-4.1": 0.0, "gpt-5-mini": 0.0, "gpt-4o-mini": 0.0,
    "o4-mini": 0.33, "o3-mini": 0.33,
    "gpt-5": 1.0, "o3": 1.0, "gemini-2.5-pro": 1.0,
    "claude-sonnet-3.5": 1.0, "claude-sonnet-3.7": 1.0,
    "claude-sonnet-4": 1.0, "claude-sonnet-4.5": 1.0, "claude-sonnet-4.6": 1.0,
    "claude-opus-4": 10.0, "claude-opus-4.1": 10.0, "gpt-4.5": 50.0,
}
PREMIUM_DEFAULT = 1.0
_VS_CACHE = {"stamp": None, "data": None}
VSCODE_ENABLED = True


def _vscode_roots():
    """Candidate VS Code install roots for the *current* user, any OS."""
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    override = os.environ.get("COPILOT_DASH_VSCODE_DIR", "")
    roots = [p for p in override.split(os.pathsep) if p]
    for name in ("Code", "Code - Insiders", "VSCodium"):
        roots += [os.path.join(appdata, name),
                  os.path.join(home, "Library", "Application Support", name),
                  os.path.join(home, ".config", name)]
    return [p for p in roots
            if os.path.isdir(os.path.join(p, "User", "workspaceStorage"))]


def _vscode_files():
    out = []
    for root in _vscode_roots():
        user = os.path.join(root, "User")
        # Workspace-scoped chats, plus chats started in a window with no folder open.
        for pattern in (
                os.path.join(user, "workspaceStorage", "*", "chatSessions", "*"),
                os.path.join(user, "globalStorage", "emptyWindowChatSessions", "*")):
            out += [p for p in glob.glob(pattern) if os.path.isfile(p)]
    return sorted(out)


TEXT_KEYS = ("value", "text", "message", "originMessage", "command")


def _collect_text(node, acc, depth=0):
    """Pull human/assistant-visible strings out of a VS Code response part tree."""
    if depth > 14:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                if k in TEXT_KEYS:
                    acc.append(v)
            else:
                _collect_text(v, acc, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _collect_text(v, acc, depth + 1)


def _prompt_text(req):
    """The text actually sent to the model for one request.

    VS Code stores the typed message in ``message.text``, but that is only a
    fraction of the real prompt. ``result.metadata.renderedUserMessage`` is the
    fully rendered payload - editor context, terminal state, workspace changes
    and attached instruction files - and runs roughly ten times larger. Fall
    back to the typed message plus attached variables when it is absent, which
    is the case for older transcripts and for requests that were interrupted.
    """
    rendered = []
    _collect_text(((req.get("result") or {}).get("metadata") or {})
                  .get("renderedUserMessage"), rendered)
    typed = (req.get("message") or {}).get("text") or ""
    if rendered:
        # renderedUserMessage already embeds the typed message and attachments.
        return " ".join(rendered)
    extra = []
    _collect_text(req.get("variableData"), extra)
    _collect_text(req.get("contentReferences"), extra)
    return " ".join([typed] + extra)


def _apply_patch(base, path, val, kind):
    """Apply one VS Code chat patch record. kind 1 sets a value, kind 2 appends."""
    node = base
    for part in path[:-1]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and -len(node) <= part < len(node):
            node = node[part]
        else:
            return
    last = path[-1]
    if isinstance(node, dict):
        cur = node.get(last)
    elif isinstance(node, list) and isinstance(last, int) and -len(node) <= last < len(node):
        cur = node[last]
    else:
        return
    if kind == 2 and isinstance(cur, list) and isinstance(val, list):
        cur.extend(val)          # append; ordering is irrelevant to a cost sum
        return
    if isinstance(node, dict):
        node[last] = val
    else:
        node[last] = val


def _load_session_blob(path):
    """VS Code writes either a plain .json session or a .jsonl patch log.

    Newer builds append a mutation log: the first record is a full snapshot and
    every later record patches it (adding requests, streaming in response parts,
    recording completionTokens). Replaying the log is essential - reading only
    the snapshot yields a chat frozen at its first turn, which is why heavy
    users previously showed a handful of one-request chats.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if not raw.strip():
        return None
    try:
        d = json.loads(raw)
        return d.get("v", d) if isinstance(d, dict) else d
    except ValueError:
        pass
    base = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        kind, path_, val = d.get("kind"), d.get("k"), d.get("v")
        if base is None:
            if isinstance(val, dict) and not isinstance(path_, list):
                base = val
            continue
        if isinstance(path_, list) and path_:
            _apply_patch(base, path_, val, kind)
    return base


def _workspace_name(path):
    """Resolve workspaceStorage/<hash> back to a friendly folder name."""
    ws = os.path.join(os.path.dirname(os.path.dirname(path)), "workspace.json")
    try:
        with open(ws, encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:
        return ""
    uri = unquote(meta.get("folder") or meta.get("workspace") or "")
    return os.path.basename(uri.rstrip("/")) if uri else ""


def _harvest_model_meta(blob, into):
    """Copilot ships its own price list inside the transcript.

    ``inputState.selectedModel.metadata`` carries the live per-model rates
    (AI credits per 1M tokens) and the premium-request multiplier, so we can use
    GitHub's real numbers for whatever models this user actually ran instead of
    a hardcoded table that silently goes stale.
    """
    meta = ((blob.get("inputState") or {}).get("selectedModel") or {}).get("metadata")
    if not isinstance(meta, dict):
        return
    mid = _norm_model(meta.get("id") or meta.get("family"))
    if not mid or mid in into:
        return
    try:
        rates = (float(meta["inputCost"]), float(meta["outputCost"]),
                 float(meta.get("cacheCost", 0)))
    except (KeyError, TypeError, ValueError):
        rates = None
    mult = meta.get("multiplierNumeric")
    into[mid] = {"rates": rates,
                 "multiplier": float(mult) if isinstance(mult, (int, float)) else None,
                 "name": meta.get("name") or mid}


def _measured_usage(req):
    """Real token counts for one request, if VS Code recorded any.

    Returns ``(prompt_tokens, completion_tokens, cached_tokens)`` with None for
    anything that has to fall back to estimation. Newer Copilot Chat builds
    write ``result.usage``; slightly older ones only write ``result.metadata``
    or a bare ``completionTokens``.
    """
    result = req.get("result") or {}
    prompt = completion = cached = None
    for src in (result.get("usage"), result.get("metadata")):
        if not isinstance(src, dict):
            continue
        for key in ("promptTokens", "prompt_tokens"):
            if prompt is None and isinstance(src.get(key), (int, float)):
                prompt = float(src[key])
        for key in ("completionTokens", "completion_tokens", "outputTokens"):
            if completion is None and isinstance(src.get(key), (int, float)):
                completion = float(src[key])
        det = src.get("prompt_tokens_details")
        if cached is None and isinstance(det, dict) \
                and isinstance(det.get("cached_tokens"), (int, float)):
            cached = float(det["cached_tokens"])
    if completion is None and isinstance(req.get("completionTokens"), (int, float)):
        completion = float(req["completionTokens"])
    # Reasoning happens per tool-call round and is billed as output.
    rounds = (result.get("metadata") or {}).get("toolCallRounds")
    if isinstance(rounds, list):
        think = sum(r["thinking"]["tokens"] for r in rounds
                    if isinstance(r, dict) and isinstance(r.get("thinking"), dict)
                    and isinstance(r["thinking"].get("tokens"), (int, float)))
        if think and completion is not None:
            completion += think
    return prompt, completion, cached


def _usage_fields(req):
    """Which usage-recording fields this request carries, for the coverage timeline.

    VS Code has changed what it persists several times; naming the fields lets
    the dashboard show a user when their own transcripts started carrying real
    numbers rather than quoting release notes that may not match their build.
    """
    result = req.get("result") or {}
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    found = set()
    if usage.get("promptTokens") or usage.get("completionTokens"):
        found.add("result.usage")
    if meta.get("promptTokens") or meta.get("outputTokens"):
        found.add("result.metadata")
    if isinstance(req.get("completionTokens"), (int, float)) and req["completionTokens"]:
        found.add("completionTokens")
    if isinstance(req.get("copilotCredits"), (int, float)):
        found.add("copilotCredits")
    if meta.get("summaries"):
        found.add("summaries.usage")
    if meta.get("renderedUserMessage"):
        found.add("renderedUserMessage")
    return found


# What each recorded field buys us, shown alongside the dates it was seen.
FIELD_NOTES = {
    "result.usage": "Full prompt and completion token counts, plus cache-read counts.",
    "result.metadata": "Prompt and output token counts.",
    "completionTokens": "Output token count recorded directly on the request.",
    "copilotCredits": "The exact AI credits charged — no estimation at all.",
    "summaries.usage": "Real cached-token counts on conversation summaries.",
    "renderedUserMessage": "The fully rendered prompt, so estimated input size stops "
                           "being wrong by ~10x.",
}


def _norm_model(model_id):
    m = (model_id or "").split("/")[-1].strip().lower()
    return m or "auto"


def scan_vscode():
    """Estimate cost/tokens for every VS Code Copilot chat. Cached on file mtimes."""
    files = _vscode_files()
    stamp = tuple((p, os.path.getmtime(p), os.path.getsize(p)) for p in files)
    if _VS_CACHE["stamp"] == stamp:
        return _VS_CACHE["data"]

    sessions, texts = [], []
    blobs, model_meta = [], {}
    for path in files:
        try:
            v = _load_session_blob(path)
        except Exception:
            continue
        if not isinstance(v, dict):
            continue
        if v.get("requests"):
            blobs.append((path, v))
        # Harvest Copilot's own price list from every transcript first, so a
        # model priced in one chat is priced correctly in all of them.
        _harvest_model_meta(v, model_meta)
    for path, v in blobs:
        reqs = v.get("requests") or []
        if not reqs:
            continue
        sid = v.get("sessionId") or os.path.splitext(os.path.basename(path))[0]
        ctx = 0            # tokens of transcript resent as context
        prev_prompt = 0.0  # previous turn's prompt size, used to infer the cached prefix
        agg = collections.Counter()
        used = collections.Counter()
        blob, title = [], ""
        prev_day = ""
        # Everything is bucketed by day, including tokens and per-model figures, so
        # a chat spanning several days can be clipped exactly to any date range
        # instead of being attributed wholesale to one end of itself.
        sdaily = collections.defaultdict(lambda: collections.Counter())
        sdm = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
        sfields = {}
        for r in reqs:
            msg = _prompt_text(r)
            typed = (r.get("message") or {}).get("text") or ""
            acc = []
            _collect_text(r.get("response") or [], acc)
            reply = " ".join(acc)
            if not title and typed.strip():
                title = " ".join(typed.split())[:90]
            blob.append(typed)
            blob.append(reply)

            model = _norm_model(r.get("modelId"))
            info = model_meta.get(model) or {}
            rate_in, rate_out, rate_cache = (info.get("rates")
                                             or VSCODE_RATES.get(model, VSCODE_RATE_DEFAULT))

            # Prefer what Copilot actually recorded; estimate only what is missing.
            m_prompt, m_out, m_cached = _measured_usage(r)
            if m_prompt is not None:
                # promptTokens is the whole prompt, context included. The prefix
                # already sent last turn is the cached part.
                t_cache = m_cached if m_cached is not None else min(prev_prompt, m_prompt)
                t_in = max(m_prompt - t_cache, 0.0)
                prev_prompt = m_prompt
            else:
                t_in = len(msg) / CHARS_PER_TOKEN
                t_cache = min(ctx, CONTEXT_WINDOW_TOKENS)
                prev_prompt = t_in + t_cache
            if m_out is not None:
                t_out = m_out
            else:
                t_out = len(reply) / CHARS_PER_TOKEN
            ctx += t_in + t_out

            credits = r.get("copilotCredits")
            if isinstance(credits, (int, float)) and credits > 0:
                cost = credits / 100.0        # 1 AI credit = $0.01, straight from Copilot
            else:
                cost = (t_in * rate_in + t_out * rate_out + t_cache * rate_cache) / 1e6 / 100
            mult = info.get("multiplier")
            if mult is None:
                mult = PREMIUM_MULTIPLIERS.get(model, PREMIUM_DEFAULT)
            prem = mult * PREMIUM_USD

            # Track, per day, how much of this user's own history carries real
            # numbers, and when each recording field first and last showed up.
            rts = r.get("timestamp")
            rday = (datetime.datetime.fromtimestamp(rts / 1000).strftime("%Y-%m-%d")
                    if rts else prev_day)
            if rday:
                prev_day = rday
            for name in _usage_fields(r):
                f = sfields.setdefault(name, {"field": name, "requests": 0,
                                              "first": None, "last": None})
                f["requests"] += 1
                if rday:
                    f["first"] = min(f["first"] or rday, rday)
                    f["last"] = max(f["last"] or rday, rday)

            agg["requests"] += 1
            used[model] += 1
            meas = m_prompt is not None and m_out is not None
            d = sdaily[rday]
            d["requests"] += 1
            d["cost_usd"] += cost
            d["premium_usd"] += prem
            d["premium_requests"] += mult
            d["input_tokens"] += t_in
            d["output_tokens"] += t_out
            d["cache_read_tokens"] += t_cache
            if meas:
                # Share of spend, not just of request count: measured requests skew
                # recent and larger, so the two percentages differ a lot.
                d["measured_out"] += 1
                d["measured_cost_usd"] += cost
                d["measured_premium_usd"] += prem
                d["measured_tokens"] += t_in + t_out + t_cache
            if isinstance(credits, (int, float)) and credits > 0:
                d["exact_credits"] += 1
            m = sdm[rday][model]
            m["requests"] += 1
            m["cost_usd"] += cost
            m["premium_usd"] += prem
            m["input_tokens"] += t_in
            m["output_tokens"] += t_out
            m["cache_read_tokens"] += t_cache

        texts.append((sid, " ".join(blob)))
        sessions.append(_vs_session(
            sid, v.get("customTitle") or title or "(untitled chat)",
            _workspace_name(path), "Insiders" in path, sdaily, sdm, sfields))

    allow = JIRA_KEY_ALLOW | _dynamic_jira_keys(t for _, t in texts)
    refs = _build_refs(texts, None, allow)
    for s in sessions:
        s["refs"] = refs.get(s["session_id"], {"jira": [], "prs": [], "repos": []})
    sessions.sort(key=lambda s: s["cost_usd"], reverse=True)

    data = dict(_vs_aggregate(sessions), **{
        "available": bool(files),
        "roots": _vscode_roots(),
        "files": len(files),
        "chars_per_token": CHARS_PER_TOKEN,
        "context_window": CONTEXT_WINDOW_TOKENS,
        "premium_usd_each": PREMIUM_USD,
    })
    _VS_CACHE["stamp"], _VS_CACHE["data"] = stamp, data
    return data


def _vs_session(sid, title, workspace, insiders, sdaily, sdm, sfields):
    """Build one VS Code chat record, with every total derived from its day buckets.

    Deriving rather than accumulating separately guarantees the chat's headline
    figures and its per-day breakdown can never drift apart, which is what makes
    clipping to a date range exact.
    """
    days = {d: {"requests": int(c["requests"]), "cost_usd": c["cost_usd"],
                "premium_usd": c["premium_usd"],
                "premium_requests": c["premium_requests"],
                "input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"],
                "cache_read_tokens": c["cache_read_tokens"],
                "measured_out": int(c["measured_out"]),
                "exact_credits": int(c["exact_credits"]),
                "measured_cost_usd": c["measured_cost_usd"],
                "measured_premium_usd": c["measured_premium_usd"],
                "measured_tokens": c["measured_tokens"]}
            for d, c in sdaily.items()}
    by_model_day = {d: {k: {"requests": int(c["requests"]), "cost_usd": c["cost_usd"],
                            "premium_usd": c["premium_usd"],
                            "input_tokens": c["input_tokens"],
                            "output_tokens": c["output_tokens"],
                            "cache_read_tokens": c["cache_read_tokens"]}
                        for k, c in mm.items()}
                    for d, mm in sdm.items()}
    s = {"session_id": sid, "title": title, "workspace": workspace,
         "insiders": insiders, "days": days, "by_model_day": by_model_day,
         "fields": list(sfields.values())}
    return _vs_totals(s, days, by_model_day)


def _vs_totals(base, days, by_model_day):
    """Fill a chat record's headline figures in from a set of day buckets."""
    agg = collections.Counter()
    for c in days.values():
        for k, v in c.items():
            agg[k] += v
    models = collections.defaultdict(lambda: collections.Counter())
    for mm in by_model_day.values():
        for k, c in mm.items():
            for key, v in c.items():
                models[k][key] += v
    dates = sorted(d for d in days if d)
    top = max(models.items(), key=lambda kv: kv[1]["requests"])[0] if models else "auto"
    return dict(base, **{
        "days": days, "by_model_day": by_model_day,
        "by_model": {k: dict(c) for k, c in models.items()},
        "coverage": _vs_coverage(days),
        "requests": int(agg["requests"]),
        "measured_out": int(agg["measured_out"]),
        "exact_credits": int(agg["exact_credits"]),
        "measured_cost_usd": agg["measured_cost_usd"],
        "measured_premium_usd": agg["measured_premium_usd"],
        "measured_tokens": round(agg["measured_tokens"]),
        "premium_requests": agg["premium_requests"],
        "premium_usd": agg["premium_requests"] * PREMIUM_USD,
        "top_model": top,
        "models": len(models),
        "input_tokens": round(agg["input_tokens"]),
        "output_tokens": round(agg["output_tokens"]),
        "cache_read_tokens": round(agg["cache_read_tokens"]),
        "total_tokens": round(agg["input_tokens"] + agg["output_tokens"]
                              + agg["cache_read_tokens"]),
        "cost_usd": agg["cost_usd"],
        "first_day": dates[0] if dates else "",
        "last_day": dates[-1] if dates else "",
    })


def _vs_coverage(days):
    """Monthly measured-vs-inferred counts, rolled up from the day buckets."""
    cov = collections.defaultdict(lambda: collections.Counter())
    for d, c in days.items():
        m = cov[d[:7] if d else "undated"]
        m["requests"] += c["requests"]
        m["measured"] += c["measured_out"]
        m["exact"] += c["exact_credits"]
    return {mo: {"requests": int(c["requests"]), "measured": int(c["measured"]),
                 "exact": int(c["exact"])}
            for mo, c in cov.items()}


def _vs_clip(s, start, end):
    """Restrict one chat to a date range, or return None if it falls outside it.

    A VS Code chat can run for a week, so counting it whole against whichever end
    of itself happens to land in range would badly misstate short ranges.
    """
    if start <= (s["first_day"] or "") and (s["last_day"] or "") <= end:
        return s
    days = {d: c for d, c in s["days"].items() if d and start <= d <= end}
    if not days:
        return None
    bmd = {d: c for d, c in s["by_model_day"].items() if d in days}
    return _vs_totals(s, days, bmd)



def _vs_aggregate(sessions):
    """Roll a list of VS Code chats up into the section's tables.

    Everything is derived from the per-chat breakdowns rather than from scan-wide
    counters, so the same code serves the unfiltered view and any filtered subset
    - the section then responds to the global chat filter like every other table.
    """
    models = collections.defaultdict(lambda: collections.Counter())
    daily = collections.defaultdict(lambda: collections.Counter())
    coverage = collections.defaultdict(lambda: collections.Counter())
    fields = {}
    for s in sessions:
        for k, c in (s.get("by_model") or {}).items():
            m = models[k]
            for key, val in c.items():
                m[key] += val
        for d, c in (s.get("days") or {}).items():
            for key, val in c.items():
                daily[d][key] += val
        for mo, c in (s.get("coverage") or {}).items():
            for key, val in c.items():
                coverage[mo][key] += val
        for f in (s.get("fields") or []):
            e = fields.setdefault(f["field"], {"field": f["field"], "requests": 0,
                                               "first": None, "last": None})
            e["requests"] += f["requests"]
            for key, pick in (("first", min), ("last", max)):
                if f[key]:
                    e[key] = pick(e[key] or f[key], f[key])
    return {
        "sessions": sessions,
        "models": sorted(
            ({"model": k, "requests": int(c["requests"]), "cost_usd": c["cost_usd"],
              "premium_usd": c["premium_usd"],
              "input_tokens": round(c["input_tokens"]),
              "output_tokens": round(c["output_tokens"]),
              "cache_read_tokens": round(c["cache_read_tokens"]),
              "known_rate": k in VSCODE_RATES}
             for k, c in models.items()),
            key=lambda m: -m["cost_usd"]),
        "daily": [{"day": d, "requests": int(c["requests"]), "cost_usd": c["cost_usd"],
                   "premium_usd": c["premium_usd"]}
                  for d, c in sorted(daily.items())],
        "totals": {
            "sessions": len(sessions),
            "requests": sum(s["requests"] for s in sessions),
            "measured_out": sum(s["measured_out"] for s in sessions),
            "exact_credits": sum(s["exact_credits"] for s in sessions),
            "measured_cost_usd": sum(s["measured_cost_usd"] for s in sessions),
            "measured_premium_usd": sum(s["measured_premium_usd"] for s in sessions),
            "measured_tokens": sum(s["measured_tokens"] for s in sessions),
            "premium_requests": sum(s["premium_requests"] for s in sessions),
            "premium_usd": sum(s["premium_usd"] for s in sessions),
            "cost_usd": sum(s["cost_usd"] for s in sessions),
            "total_tokens": sum(s["total_tokens"] for s in sessions),
        },
        "coverage": [{"month": mo, "requests": int(c["requests"]),
                      "measured": int(c["measured"]), "exact": int(c["exact"]),
                      "pct": (c["measured"] / c["requests"] * 100) if c["requests"] else 0}
                     for mo, c in sorted(coverage.items())],
        "fields": sorted((dict(f, note=FIELD_NOTES.get(f["field"], ""))
                          for f in fields.values()),
                         key=lambda f: (f["first"] or "9999")),
    }


def _vs_view(q="", basis="tokens", start=MIN_DAY, end=MAX_DAY):
    """The VS Code section, narrowed to the global filter and the date range."""
    data = scan_vscode()
    sessions = data.get("sessions", [])
    q = (q or "").strip().lower()
    if q:
        sessions = [s for s in sessions if _matches(s, q)]
    ranged = not (start <= MIN_DAY and end >= MAX_DAY)
    if ranged:
        sessions = [c for c in (_vs_clip(s, start, end) for s in sessions) if c]
    if basis == "premium":
        sessions = [dict(s, cost_usd=s["premium_usd"],
                         days={d: dict(c, cost_usd=c["premium_usd"])
                               for d, c in (s.get("days") or {}).items()},
                         by_model={k: dict(c, cost_usd=c["premium_usd"])
                                   for k, c in (s.get("by_model") or {}).items()})
                    for s in sessions]
    if not q and not ranged and basis != "premium":
        return data
    sessions = sorted(sessions, key=lambda s: s["cost_usd"], reverse=True)
    return dict(data, **_vs_aggregate(sessions), filtered=bool(q), basis=basis)


def _vs_sessions_in_range(start, end, basis="tokens"):
    """VS Code chats normalized to the CLI session shape and flagged estimated.

    Chats are clipped to the range day by day, so a chat that ran across the
    boundary contributes only the part of itself that actually falls inside it.

    ``basis`` selects which estimate drives cost_usd, so every downstream table
    aggregates consistently: "tokens" prices the transcript against published
    per-model API rates, "premium" charges GitHub's per-request multipliers.
    """
    out = []
    for full in scan_vscode().get("sessions", []):
        s = _vs_clip(full, start, end)
        if s is None:
            continue
        out.append({
            "session_id": s["session_id"], "title": s["title"],
            "repository": s["workspace"], "branch": "",
            "top_model": s["top_model"], "models": s["models"],
            "turns": s["requests"], "requests": s["requests"],
            "input_tokens": s["input_tokens"], "output_tokens": s["output_tokens"],
            "cache_read_tokens": s["cache_read_tokens"], "cache_write_tokens": 0,
            "total_tokens": s["total_tokens"],
            "cost_usd": s["premium_usd"] if basis == "premium" else s["cost_usd"],
            "last_day": s["last_day"], "refs": s["refs"], "est": True,
        })
    return out


def _merge_models(models, vs_sessions, start, end, basis="tokens"):
    """Fold estimated VS Code per-model usage into the measured model table."""
    by = {m["model"]: dict(m, est=False) for m in models}
    # Recompute VS per-model totals from only the sessions that survived filtering,
    # using each chat's own per-model breakdown: a chat that switched models must
    # not have all of its cost attributed to whichever model it used most.
    keep = {s["session_id"] for s in vs_sessions}
    agg = collections.defaultdict(lambda: collections.Counter())
    for full in scan_vscode().get("sessions", []):
        if full["session_id"] not in keep:
            continue
        s = _vs_clip(full, start, end)
        if s is None:
            continue
        for name, mc in (s.get("by_model") or {}).items():
            c = agg[name]
            c["requests"] += mc["requests"]
            c["cost_usd"] += mc["premium_usd"] if basis == "premium" else mc["cost_usd"]
            c["input_tokens"] += mc["input_tokens"]
            c["output_tokens"] += mc["output_tokens"]
            c["cache_read_tokens"] += mc["cache_read_tokens"]
    for name, c in agg.items():
        e = by.setdefault(name, {"model": name, "requests": 0, "input_tokens": 0,
                                 "output_tokens": 0, "cache_read_tokens": 0,
                                 "cache_write_tokens": 0, "cost_usd": 0.0, "est": False})
        e["requests"] += int(c["requests"])
        e["cost_usd"] += c["cost_usd"]
        e["input_tokens"] += int(c["input_tokens"])
        e["output_tokens"] += int(c["output_tokens"])
        e["cache_read_tokens"] += int(c["cache_read_tokens"])
        e["est"] = True
    return sorted(by.values(), key=lambda m: -m["cost_usd"])


def _merge_daily(daily, vs_sessions, start, end, basis="tokens"):
    """Fold estimated VS Code daily spend into the measured daily series."""
    keep = {s["session_id"] for s in vs_sessions}
    if not keep:
        return daily
    by = {d["day"]: dict(d, est=False, est_usd=0.0) for d in daily}
    # scan_vscode's daily series is global; rebuild it from the kept sessions'
    # own per-day breakdown so multi-day chats land on every day they touched.
    for s in scan_vscode().get("sessions", []):
        if s["session_id"] not in keep:
            continue
        for day, c in (s.get("days") or {}).items():
            if not (start <= day <= end):
                continue
            e = by.setdefault(day, {"day": day, "requests": 0, "sessions": 0,
                                    "total_tokens": 0, "cost_usd": 0.0, "est": False,
                                    "est_usd": 0.0})
            add = c["premium_usd"] if basis == "premium" else c["cost_usd"]
            e["requests"] += c["requests"]
            e["cost_usd"] += add
            e["est_usd"] = e.get("est_usd", 0.0) + add
            e["est"] = True
    return sorted(by.values(), key=lambda d: d["day"])


def rollup(sessions, refs):
    """Aggregate cost per Jira ticket / PR / repo.

    Jira and repo costs are split evenly across the refs a chat touched, so
    totals are not double counted. Pull requests instead use turn-level segment
    attribution (see pr_segments) because a PR is produced by one identifiable
    stretch of a chat, not by the whole chat.

    Estimated VS Code chats carry no turn-level cost, so their PRs fall back to
    a whole-chat grouping. Any bucket touched by an estimated chat is flagged
    est=True so the UI can label it.
    """
    jira, repos = {}, {}
    for s in sessions:
        r = refs.get(s["session_id"])
        if not r:
            continue
        cost, toks = s["cost_usd"] or 0, s["total_tokens"] or 0
        for bucket, items, keyfn in (
                (jira, r["jira"], lambda x: x),
                (repos, r["repos"], lambda x: x["name"])):
            if not items:
                continue
            share_c, share_t = cost / len(items), toks / len(items)
            for it in items:
                k = keyfn(it)
                e = bucket.setdefault(k, {"key": k, "cost_usd": 0.0, "total_tokens": 0,
                                          "chats": 0, "titles": [], "created": False,
                                          "role": "mentioned", "est": False})
                e["cost_usd"] += share_c
                e["total_tokens"] += share_t
                e["chats"] += 1
                e["est"] = e["est"] or bool(s.get("est"))
                if len(e["titles"]) < 5:
                    e["titles"].append(s["title"])
                if isinstance(it, dict) and it.get("role") == "primary":
                    e["role"] = "primary"
    srt = lambda d: sorted(d.values(), key=lambda x: -x["cost_usd"])
    # Per-session rollup: exact cost, no splitting needed.
    sess = [{"key": s["title"], "cost_usd": s["cost_usd"] or 0,
             "total_tokens": s["total_tokens"] or 0, "chats": s["turns"] or 0,
             "titles": [x for x in [s["repository"]] if x],
             "created": False, "role": "mentioned", "est": bool(s.get("est")),
             "session_id": s["session_id"]}
            for s in sessions]
    sess.sort(key=lambda x: -x["cost_usd"])
    measured = {s["session_id"]: s["title"] for s in sessions if not s.get("est")}
    pr_map = {tuple(e["keys"]): e for e in pr_segments(measured)}
    for s in sessions:
        if not s.get("est"):
            continue
        r = refs.get(s["session_id"]) or {}
        keys = tuple(sorted(p["key"] for p in r.get("prs", [])))
        if not keys:
            continue
        e = pr_map.setdefault(keys, {"keys": list(keys), "key": _group_label(keys),
                                     "cost_usd": 0.0, "total_tokens": 0, "chats": 0,
                                     "titles": [], "created": False,
                                     "role": "mentioned", "turns": 0, "est": False})
        e["cost_usd"] += s["cost_usd"] or 0
        e["total_tokens"] += s["total_tokens"] or 0
        e["turns"] += s["turns"] or 0
        e["chats"] += 1
        e["est"] = True
        e["created"] = e["created"] or any(p.get("created") for p in r.get("prs", []))
        if s["title"] not in e["titles"] and len(e["titles"]) < 5:
            e["titles"].append(s["title"])
    prs = sorted(pr_map.values(), key=lambda x: -x["cost_usd"])
    out = {"jira": srt(jira), "prs": prs, "repos": srt(repos), "sessions": sess}
    out["unattributed"] = _unattributed(sessions, refs, out)
    return out


def _unattributed(sessions, refs, tabs):
    """What each work-item tab leaves out, so the page can reconcile itself.

    A work-item tab can only show chats that carry that kind of reference, so it
    legitimately sums to less than the totals above. Rather than let that read as
    a miscalculation, report the remainder explicitly.
    """
    tot_c = sum(s["cost_usd"] or 0 for s in sessions)
    tot_t = sum(s["total_tokens"] or 0 for s in sessions)
    kinds = {"jira": "jira", "prs": "prs", "repos": "repos"}
    out = {}
    for tab, items in tabs.items():
        cost = tot_c - sum(i["cost_usd"] for i in items)
        toks = tot_t - sum(i["total_tokens"] or 0 for i in items)
        kind = kinds.get(tab)
        chats = sum(1 for s in sessions
                    if not (refs.get(s["session_id"]) or {}).get(kind)) if kind else 0
        out[tab] = {"cost_usd": max(0.0, cost), "total_tokens": max(0, round(toks)),
                    "chats": chats, "total_usd": tot_c}
    return out


_SEG_CACHE = {"stamp": None, "data": None}


def _turn_level_data():
    """Per-turn PR mentions and per-turn cost, cached like extract_refs()."""
    with connect() as con:
        stamp = tuple(con.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM turns").fetchone()) + \
            tuple(con.execute(
                "SELECT COUNT(*) FROM assistant_usage_events").fetchone())
        if _SEG_CACHE["stamp"] == stamp:
            return _SEG_CACHE["data"]
        turn_rows = con.execute(
            "SELECT session_id, turn_index, COALESCE(user_message,'') || ' ' ||"
            " COALESCE(assistant_response,'') FROM turns").fetchall()
        cost_rows = con.execute(
            "SELECT session_id, turn_index, SUM(total_nano_aiu),"
            " SUM(input_tokens + output_tokens + COALESCE(cache_read_tokens,0)"
            "     + COALESCE(cache_write_tokens,0))"
            " FROM assistant_usage_events GROUP BY session_id, turn_index").fetchall()

    turn_prs = collections.defaultdict(dict)   # sid -> turn_index -> {pr: created}
    for sid, ti, text in turn_rows:
        created_here = bool(RE_CREATED.search(text))
        for owner, repo, num in RE_PR.findall(text):
            name = clean_repo(owner, repo)
            if not name:
                continue
            k = f"{name}#{num}"
            at = turn_prs[sid].setdefault(ti, {})
            at[k] = at.get(k, False) or created_here
    turn_cost = collections.defaultdict(dict)  # sid -> turn_index -> (nano, tokens)
    for sid, ti, nano, tok in cost_rows:
        turn_cost[sid][ti] = (nano or 0, tok or 0)

    data = (dict(turn_prs), dict(turn_cost))
    _SEG_CACHE["stamp"], _SEG_CACHE["data"] = stamp, data
    return data


def pr_segments(session_titles):
    """Attribute cost to each PR from the stretch of chat that produced it.

    For every turn that mentions a PR (an "anchor"), the attributed segment is
    every turn since the previous anchor, up to and including the anchor itself.
    That is the subsection of the chat that actually generated the PR.

    PRs that came out of the same group of turns are reported as a single line
    item holding that segment's full cost, rather than being split apart -
    the work produced them together and cannot be meaningfully divided.
    Turns after the final anchor are left unattributed.
    """
    turn_prs, turn_cost = _turn_level_data()
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
            e["cost_usd"] += sum(costs[t][0] for t in seg) / NANO_AIU_PER_USD
            e["total_tokens"] += sum(costs[t][1] for t in seg)
            e["turns"] += len(seg)
            e["chats"] += 1
            e["created"] = e["created"] or any(keys.values())
            if title not in e["titles"] and len(e["titles"]) < 5:
                e["titles"].append(title)
    return sorted(out.values(), key=lambda x: -x["cost_usd"])


def _group_label(keys):
    """'owner/repo#1, #2, #3' when a group shares one repo, else full keys."""
    repos = {k.split("#")[0] for k in keys}
    if len(repos) == 1:
        repo = repos.pop()
        nums = sorted((int(k.split("#")[1]) for k in keys))
        return f"{repo}#" + ", #".join(str(n) for n in nums)
    return ", ".join(keys)


def _restrict(sql, ids):
    """Add a session_id predicate to a date-filtered aggregate query."""
    holes = ",".join("?" * len(ids)) or "''"
    return sql.replace("GROUP BY", f"  AND u.session_id IN ({holes})\nGROUP BY", 1)


def _matches(s, q):
    """Keyword test over a chat's title, repo, branch, model, id and refs.

    Handles both CLI chats (``repository``) and VS Code chats (``workspace``) so
    the same filter narrows every section of the page.
    """
    r = s.get("refs") or {}
    parts = [s.get("title") or "", s.get("repository") or "", s.get("workspace") or "",
             s.get("branch") or "", s.get("top_model") or "", s.get("session_id") or ""]
    parts += list(r.get("jira", []))
    parts += [p["key"] for p in r.get("prs", [])]
    parts += [x["name"] for x in r.get("repos", [])]
    return q in " ".join(parts).lower()


def _attach_vs_repos(vs, refs):
    """Give VS Code chats a repository, the way CLI chats already get one.

    CLI chats seed a ``primary`` repo from the session's ``repository`` column,
    but VS Code chats only know a workspace folder, so they could never appear in
    the Repositories tab. Map that folder onto a repo we already know about; a
    multi-root ``.code-workspace`` spans several repos and is deliberately left
    alone rather than invented as a repo of its own.
    """
    known = {}
    for r in refs.values():
        for it in (r or {}).get("repos", []):
            known.setdefault(it["name"].split("/")[-1].lower(), it["name"])
    for s in vs:
        r = s.get("refs") or {}
        if r.get("repos"):
            continue
        name = (s.get("workspace") or "").strip()
        if not name:
            base = s.get("repository") or ""
            name = "" if base.endswith(".code-workspace") else base
        full = known.get(name.split("/")[-1].lower()) if name else None
        if not full:
            continue
        # Copy first: these ref dicts are shared with the VS Code scan cache.
        r = dict(r, repos=[{"name": full, "role": "primary"}])
        s["refs"] = refs[s["session_id"]] = r


def build_payload(start, end, q="", include_vs=False, basis="tokens"):
    sessions = fetch(SESSION_SQL, (start, end))
    refs = extract_refs()
    for s in sessions:
        s["refs"] = refs.get(s["session_id"], {"jira": [], "prs": [], "repos": []})
        s["est"] = False
    vs = []
    if include_vs and VSCODE_ENABLED:
        try:
            vs = _vs_sessions_in_range(start, end, basis)
        except Exception:
            vs = []
        sessions += vs
        refs = dict(refs, **{s["session_id"]: s["refs"] for s in vs})
        _attach_vs_repos(vs, refs)
    q = (q or "").strip().lower()
    if q:
        # A keyword filter narrows the chat set, then every aggregate on the page
        # is recomputed against only those chats.
        sessions = [s for s in sessions if _matches(s, q)]
        vs = [s for s in vs if _matches(s, q)]
        ids = [s["session_id"] for s in sessions if not s.get("est")]
        args = (start, end, *ids)
        models = fetch(_restrict(MODEL_SQL, ids), args)
        daily = fetch(_restrict(DAILY_SQL, ids), args)
    else:
        models = fetch(MODEL_SQL, (start, end))
        daily = fetch(DAILY_SQL, (start, end))
    if vs:
        models = _merge_models(models, vs, start, end, basis)
        daily = _merge_daily(daily, vs, start, end, basis)
    totals = {
        "cost_usd": sum(s["cost_usd"] or 0 for s in sessions),
        "total_tokens": sum(s["total_tokens"] or 0 for s in sessions),
        "input_tokens": sum(s["input_tokens"] or 0 for s in sessions),
        "output_tokens": sum(s["output_tokens"] or 0 for s in sessions),
        "cache_read_tokens": sum(s["cache_read_tokens"] or 0 for s in sessions),
        "cache_write_tokens": sum(s["cache_write_tokens"] or 0 for s in sessions),
        "requests": sum(s["requests"] or 0 for s in sessions),
        "sessions": len(sessions),
    }
    with connect() as con:
        bounds = con.execute(
            "SELECT MIN(date(created_at,'localtime')) a,"
            " MAX(date(created_at,'localtime')) b"
            " FROM assistant_usage_events"
        ).fetchone()
        # The billing cycle is the calendar month, independent of the filter.
        m = con.execute(
            "SELECT COUNT(*) reqs, COUNT(DISTINCT session_id) sess,"
            " COALESCE(SUM(input_tokens + output_tokens"
            "   + COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)),0) tok,"
            " COALESCE(SUM(total_nano_aiu),0) nano"
            " FROM assistant_usage_events"
            " WHERE strftime('%Y-%m', created_at, 'localtime')"
            "       = strftime('%Y-%m','now','localtime')"
        ).fetchone()
    today = datetime.date.today()
    reset = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    credits_used = m["nano"] / NANO_AIU_PER_CREDIT
    mtd = {"month": today.strftime("%Y-%m"),
           "requests": m["reqs"], "sessions": m["sess"],
           "total_tokens": m["tok"],
           "cost_usd": m["nano"] / NANO_AIU_PER_USD,
           "credits_used": credits_used,
           "budget": CREDIT_BUDGET,
           "pct": (credits_used / CREDIT_BUDGET * 100) if CREDIT_BUDGET else None,
           "remaining": (CREDIT_BUDGET - credits_used) if CREDIT_BUDGET else None,
           "reset_date": reset.strftime("%b %-d, %Y") if os.name != "nt"
                         else reset.strftime("%b %#d, %Y"),
           "days_left": (reset - today).days}
    return {
        "totals": totals,
        "sessions": sessions,
        "models": models,
        "daily": daily,
        "mtd": mtd,
        "rollup": rollup(sessions, refs),
        "jira_base": JIRA_BASE,
        "q": q,
        "vscode": {"included": bool(vs), "sessions": len(vs),
                   "cost_usd": sum(s["cost_usd"] for s in vs),
                   "total_tokens": sum(s["total_tokens"] for s in vs),
                   "basis": basis,
                   "available": VSCODE_ENABLED},
        "range": {"start": start, "end": end},
        "bounds": {"min": bounds["a"], "max": bounds["b"]},
        "db": DB_PATH,
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


def credits_set(value):
    """Persist the monthly allowance entered in the banner.

    Kept in the same state file as the digest switch, so an allowance typed
    into the dashboard survives a restart without needing --ai-credits.
    """
    global CREDIT_BUDGET
    try:
        n = int(float(str(value).replace(",", "").replace("_", "").strip()))
    except (TypeError, ValueError):
        return CREDIT_BUDGET
    n = max(0, n)
    CREDIT_BUDGET = n
    with DIGEST_LOCK:                 # the lock guards the state file, not just digests
        state = _state_load()
        state["ai_credits"] = n
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


def _digest_from():
    """Sender address, defaulting to the recipient's own domain."""
    if EMAIL_FROM:
        return EMAIL_FROM
    domain = EMAIL_TO.split("@")[-1] if "@" in EMAIL_TO else "localhost"
    return f"copilot-dashboard@{domain}"


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
    recent = build_payload(lo, hi, "", VSCODE_ENABLED, "tokens")
    active = [d for d in recent.get("daily", []) if (d.get("requests") or 0) > 0]
    if not active:
        return None, recent
    return max(d["day"] for d in active), recent


def _digest_data(day, recent=None):
    """Everything the email needs: the day itself plus trailing context."""
    p = build_payload(day, day, "", VSCODE_ENABLED, "tokens")
    if recent is None:
        lo = (datetime.date.fromisoformat(day)
              - datetime.timedelta(days=DIGEST_LOOKBACK_DAYS)).isoformat()
        recent = build_payload(lo, day, "", VSCODE_ENABLED, "tokens")
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
    return (f"Copilot digest — {pretty}: {_money(t['cost_usd'])}, "
            f"{t['requests']:,} requests")


def _rows(items, n=5):
    return [i for i in (items or [])][:n]


def _digest_html(d):
    p = d["payload"]
    t = p["totals"]
    day_pretty = datetime.date.fromisoformat(d["day"]).strftime("%A, %B %d, %Y")
    vs = p.get("vscode") or {}
    vs_cost = vs.get("cost_usd") or 0
    cli_cost = (t["cost_usd"] or 0) - vs_cost
    credits = (t["cost_usd"] or 0) * 100

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

    cards = [("Total spend", _money(t["cost_usd"])),
             ("AI credits", f"{credits:,.0f}"),
             ("Requests", f"{t['requests']:,}"),
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
            ("Ticket", "left", lambda r: esc(r["key"])),
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
        table("Most expensive chats", _rows(p.get("sessions")), [
            ("Chat", "left", lambda r: esc((r["title"] or "")[:70]) + (
                " <span style='color:#9a6700;font-size:10px'>EST</span>"
                if r.get("est") else "")),
            ("Requests", "right", lambda r: f"{(r.get('requests') or 0):,}"),
            ("Cost", "right", lambda r: _money(r["cost_usd"]))]),
    ]
    split = (f"Copilot CLI {_money(cli_cost)} &nbsp;·&nbsp; "
             f"VS Code {_money(vs_cost)} <span style='color:#9a6700'>(estimated)</span>"
             if vs.get("included") else f"Copilot CLI {_money(cli_cost)}")
    return f"""<html><body style="margin:0;padding:24px;background:#fff;
 font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2328">
<div style="max-width:720px;margin:0 auto">
<div style="font-size:12px;color:#57606a;text-transform:uppercase;letter-spacing:.6px">
GitHub Copilot &mdash; daily digest</div>
<h2 style="margin:4px 0 2px;font-size:20px">{esc(day_pretty)}</h2>
<div style="font-size:12px;color:#57606a">{deltas or "&nbsp;"}</div>
<table cellspacing="0" cellpadding="0" style="margin:16px 0 4px"><tr>{card_html}</tr></table>
<div style="font-size:12px;color:#57606a;margin:10px 0 0">{split}</div>
{"".join(parts)}
<p style="margin:26px 0 0;font-size:11px;color:#8b949e;border-top:1px solid #e6e8eb;
padding-top:10px">
Generated locally by copilot_cost_dashboard.py. Rows marked EST are estimated from VS Code
transcripts; Copilot CLI figures are measured. Covers Copilot CLI on this machine and VS Code
chat only &mdash; usage on github.com or other machines is not included.
</p></div></body></html>"""


def _digest_text(d):
    p = d["payload"]
    t = p["totals"]
    lines = [f"GitHub Copilot daily digest - {d['day']}", "",
             f"Total spend : {_money(t['cost_usd'])}",
             f"AI credits  : {(t['cost_usd'] or 0) * 100:,.0f}",
             f"Requests    : {t['requests']:,}",
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
                lines.append(f"  {r['key']:<40} {_money(r['cost_usd'])}")
    lines += ["", "Most expensive chats:"]
    for s in _rows(p.get("sessions")):
        lines.append(f"  {(s['title'] or '')[:52]:<52} {_money(s['cost_usd'])}")
    lines += ["", "Covers Copilot CLI on this machine and VS Code chat only."]
    return "\n".join(lines)


def send_digest(day, recent=None):
    """Build and deliver the digest for one day. Raises on SMTP failure."""
    d = _digest_data(day, recent)
    msg = EmailMessage()
    msg["Subject"] = _digest_subject(d)
    msg["From"] = _digest_from()
    msg["To"] = EMAIL_TO
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_digest_from().split("@")[-1])
    msg.set_content(_digest_text(d))
    msg.add_alternative(_digest_html(d), subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.send_message(msg)
    return d


def _digest_worker(day, recent, state):
    try:
        send_digest(day, recent)
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
                         daemon=True).start()


def digest_status():
    state = _state_load()
    return {"configured": bool(EMAIL_TO), "enabled": _digest_enabled(state),
            "to": EMAIL_TO, "from": _digest_from(),
            "host": SMTP_HOST, "port": SMTP_PORT,
            "last_digest_day": state.get("last_digest_day"),
            "last_sent_at": state.get("last_sent_at"),
            "sending": DIGEST_STATUS["sending"],
            "error": DIGEST_STATUS["last_error"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, body, ctype):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
                inc = qs.get("vscode", ["0"])[0] == "1"
                basis = "premium" if qs.get("basis", [""])[0] == "premium" else "tokens"
                payload = build_payload(start, end, q, inc, basis)
                # The first refresh of a new day is what triggers the digest; it
                # runs after the payload so a mail problem can never break the page.
                maybe_send_digest()
                payload["digest"] = digest_status()
                self._send(json.dumps(payload), "application/json")
            elif url.path == "/api/digest":
                action = qs.get("action", [""])[0]
                if qs.get("force", ["0"])[0] == "1":
                    action = "send"
                if action in ("on", "off"):
                    digest_set_enabled(action == "on")
                elif action == "send":
                    maybe_send_digest(force=True)
                self._send(json.dumps(digest_status()), "application/json")
            elif url.path == "/api/credits":
                if "value" in qs:
                    credits_set(qs.get("value", [""])[0])
                self._send(json.dumps({"budget": CREDIT_BUDGET}),
                           "application/json")
            elif url.path == "/api/vscode":
                vq = qs.get("q", [""])[0]
                vbasis = "premium" if qs.get("basis", [""])[0] == "premium" else "tokens"
                self._send(json.dumps(_vs_view(vq, vbasis,
                                               qs.get("start", [MIN_DAY])[0],
                                               qs.get("end", [MAX_DAY])[0])
                                      if VSCODE_ENABLED
                                      else {"available": False, "disabled": True}),
                           "application/json")
            elif url.path == "/api/turns":
                sid = qs.get("session_id", [""])[0]
                self._send(json.dumps(fetch(TURN_SQL, (sid,))), "application/json")
            elif url.path in ("/", "/index.html"):
                self._send(PAGE, "text/html; charset=utf-8")
            else:
                self.send_error(404)
        except Exception as exc:  # surface errors in the UI instead of dying
            self._send(json.dumps({"error": str(exc)}), "application/json")


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Copilot Chat Cost Dashboard</title>
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
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:24px;font-weight:700;margin-top:6px}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:20px}
h2{font-size:13px;margin:0 0 12px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--dim);font-weight:600;font-size:12px;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
tbody tr:hover{background:#1c2128}
.cost{color:var(--good);font-weight:600}
.sub{color:var(--dim);font-size:12px}
#vscov h3{font-size:14px;margin:18px 0 8px;font-weight:600}
#vscov code{font-size:12px;background:var(--bg);padding:1px 5px;border-radius:4px}
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
.b.pr{border-color:#3fb950;color:#7ee787}
.b.prnew{border-color:#d29922;color:#e3b341}
.b.repo{border-color:#388bfd;color:#79c0ff}
.b.more{color:var(--dim)}
.b.est{border-color:#d29922;color:#e3b341;background:#1c1710}
.card .split{display:flex;flex-wrap:wrap;gap:3px 12px;margin-top:5px;font-size:11px;
  font-weight:600;letter-spacing:0;color:var(--dim)}
.card .split span{white-space:nowrap}
.card .split .e{color:#e3b341}
.card .split i{font-style:normal;font-weight:400;opacity:.75}
button.fold{background:none;border:0;color:var(--dim);cursor:pointer;font:inherit;
  padding:0 7px 0 0;line-height:1}
button.fold:hover{color:var(--fg)}
section.collapsed > *:not(h2){display:none !important}
.tabs{display:flex;gap:6px;margin-bottom:12px;align-items:center;flex-wrap:wrap}
.warn{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:700;
   border:1px solid #d29922;color:#e3b341;background:#1c1710;letter-spacing:.04em}
.note{border:1px solid #d29922;background:#1c1710;color:#e3b341;border-radius:8px;
   padding:10px 12px;font-size:12px;line-height:1.5;margin-bottom:14px}
.tabs button.on{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.tabs input{margin-left:auto}
/* Overlay clear button. The wrapper carries any layout the input had, so the
   input keeps behaving as it did before it was wrapped. */
.clearable{position:relative;display:inline-block}
.tabs > .clearable{margin-left:auto}
.tabs > .clearable > input{margin-left:0}
.clearable > input{padding-right:24px}
.clearx{position:absolute;right:2px;top:50%;transform:translateY(-50%);display:none;
  border:0;background:none;color:var(--dim);cursor:pointer;font-size:15px;line-height:1;
  padding:0 5px;border-radius:6px}
.clearable.has > .clearx{display:block}
.clearx:hover{color:var(--fg)}
/* Allowance box: an input that reads as part of the "used / allowance" figure. */
.budgetin{font:inherit;color:inherit;background:transparent;border:0;border-bottom:1px dashed var(--dim);
  border-radius:0;padding:0 2px;width:6.5em;text-align:left}
.budgetin:hover{border-bottom-color:var(--fg)}
.budgetin:focus{outline:none;border-bottom:1px solid var(--acc)}
.mtdgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}
.mtdgrid .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.mtdgrid .v{font-size:22px;font-weight:700;margin-top:4px}
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
/* Auto-refresh control stacks its own "last refreshed" line underneath. */
.autobox{display:inline-flex;flex-direction:column;align-items:flex-start;line-height:1.15}
#lastref{font-size:10px;color:var(--dim);white-space:nowrap;padding-left:18px}
</style></head><body>
<div id="busy"></div>
<header>
  <h1>GitHub Copilot &mdash; Chat Cost Dashboard</h1>
  <label class="sub">From <input type="date" id="start"></label>
  <label class="sub">To <input type="date" id="end"></label>
  <select id="preset">
    <option value="all">All time</option>
    <option value="today">Today</option>
    <option value="yesterday">Yesterday</option>
    <option value="week">This week</option>
    <option value="7">Last 7 days</option>
    <option value="mtd">MTD</option>
    <option value="lastmonth">Last month</option>
    <option value="30">Last 30 days</option><option value="90">Last 90 days</option>
  </select>
  <span class="clearable"><input id="q" placeholder="Filter chats…" size="18"><button
    class="clearx" data-for="q" tabindex="-1" title="Clear filter"
    aria-label="Clear filter">&times;</button></span>
  <label class="sub" title="Fold estimated VS Code chat cost into every table below"><input type="checkbox" id="incvs" checked> +VS Code (est.)</label>
  <select id="vsbasis" title="How VS Code chats are costed in every table. Token model prices the transcript at published per-model rates; premium-request model charges GitHub's per-request multipliers.">
    <option value="tokens">VS Code basis: tokens</option>
    <option value="premium">VS Code basis: premium reqs</option>
  </select>
  <span class="sub" id="qnote"></span>
  <span id="digest" title="Daily digest email"></span>
  <button id="digestsend" title="Send the digest now">Send now</button>
  <span class="autobox">
    <label class="sub"><input type="checkbox" id="auto" checked> auto 5m</label>
    <span id="lastref" title="When the data on this page was last loaded"></span>
  </span>
  <button class="primary" id="refresh"><span class="spin"></span>Refresh</button>
</header>
<main>
  <div id="loadnote"><span class="spin"></span><span id="loadmsg">Loading…</span></div>
  <div id="err"></div>
  <div class="note" id="mixnote" style="display:none"></div>
  <div class="cards" id="cards"></div>
  <section id="mtd" style="display:none">
    <h2>License consumption &mdash; usage this cycle</h2>
    <div class="mtdgrid">
      <div><div class="k">AI credits this cycle</div><div class="v"><span id="mtdUsed"></span>
        <span class="sub">/ <input id="budget" class="budgetin" type="text" inputmode="numeric"
          size="7" title="Your AI credit allowance per billing cycle, from Copilot Settings &gt; Usage"
          placeholder="allowance"></span></div></div>
      <div><div class="k">% of allowance used</div><div class="v" id="mtdPct"></div></div>
      <div><div class="k">Credits remaining</div><div class="v" id="mtdLeft"></div></div>
      <div><div class="k">Tokens used</div><div class="v" id="mtdTok"></div></div>
    </div>
    <div class="meter"><div id="mtdBar"></div></div>
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
      <span class="clearable"><input id="rq" placeholder="Search work items…" size="22"><button
        class="clearx" data-for="rq" tabindex="-1" title="Clear search"
        aria-label="Clear search">&times;</button></span>
    </div>
    <table id="rollup"></table>
    <div class="sub" id="rollupfoot"></div>
  </section>
  <section><h2>Cost by model</h2><table id="models"></table></section>
  <section id="vs" style="display:none">
    <h2>VS Code Copilot chat <span class="warn" id="vsbadge">ESTIMATED</span>
      <span class="sub" id="vshead"></span></h2>
    <div class="note" id="vsnote"></div>
    <div class="cards" id="vscards"></div>
    <div id="vscov"></div>
    <table id="vsmodels"></table>
    <div style="height:14px"></div>
    <table id="vssessions"></table>
    <div class="sub" id="vsfoot"></div>
  </section>
  <section><h2>Cost by chat / session <span class="sub">(click a row for per-turn detail)</span></h2>
    <table id="sessions"></table></section>
  <div class="sub" id="foot"></div>
</main>
<script>
const usd=n=>'$'+(n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const usd4=n=>'$'+(n||0).toLocaleString(undefined,{minimumFractionDigits:4,maximumFractionDigits:4});
const num=n=>(n||0).toLocaleString();
const kt=n=>{n=n||0;return n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':n};
// 1 AI credit = 1e9 nano-AIU = $0.01, so credits are cost in cents.
const cred=n=>((n||0)*100).toLocaleString(undefined,{maximumFractionDigits:(n||0)<1?1:0});
// Big combined figure, with the measured CLI / estimated VS Code breakout beneath it.
function split(total,vsi,fmt){
  if(!vsi||!vsi.included) return fmt(total);
  const e=vsi.cost_usd||0, c=total-e;
  return `${fmt(total)}<div class="split">`
    +`<span title="Measured from the Copilot CLI session store">${fmt(c)} <i>GH&nbsp;Copilot</i></span>`
    +`<span class="e" title="Estimated from VS Code transcript length; VS Code records no usage data">${fmt(e)} <i>VS&nbsp;Code est.</i></span>`
    +`</div>`;
}
let DATA=null, sortKey='cost_usd', sortDir=-1, tab='jira', LAST_LOAD=null;

const jiraUrl=k=>(DATA&&DATA.jira_base)?`${DATA.jira_base}/browse/${k}`:null;
const prUrl=k=>{const [r,n]=k.split('#');return `https://github.com/${r}/pull/${n}`;};
const repoUrl=r=>`https://github.com/${r}`;
const MAXB=4;
function badges(refs){
  if(!refs) return '';
  const out=[];
  refs.jira.slice(0,MAXB).forEach(k=>{const u=jiraUrl(k);
    out.push(u?`<a class="b jira" href="${u}" target="_blank" title="Jira ${k}">${k}</a>`
              :`<span class="b jira" title="Set --jira-base to link">${k}</span>`);});
  if(refs.jira.length>MAXB) out.push(`<span class="b more">+${refs.jira.length-MAXB} Jira</span>`);
  refs.prs.slice(0,MAXB).forEach(p=>out.push(
    `<a class="b ${p.created?'prnew':'pr'}" href="${prUrl(p.key)}" target="_blank" title="${p.created?'PR created in this chat':'PR referenced'}: ${p.key}">${p.created?'✚ ':''}#${p.number}</a>`));
  if(refs.prs.length>MAXB) out.push(`<span class="b more">+${refs.prs.length-MAXB} PR</span>`);
  refs.repos.filter(r=>r.role==='primary').slice(0,2).forEach(r=>out.push(
    `<a class="b repo" href="${repoUrl(r.name)}" target="_blank">${r.name.split('/').pop()}</a>`));
  return out.length?`<div class="badges">${out.join('')}</div>`:'';
}
function refText(s){
  const r=s.refs||{jira:[],prs:[],repos:[]};
  return [...r.jira, ...r.prs.map(p=>p.key), ...r.repos.map(x=>x.name)].join(' ');
}
function renderRollup(){
  let items=(DATA.rollup&&DATA.rollup[tab])||[];
  const rq=(document.getElementById('rq').value||'').toLowerCase().trim();
  if(rq) items=items.filter(i=>(i.key+' '+(i.titles||[]).join(' ')).toLowerCase().includes(rq));
  const label={jira:'Jira ticket',prs:'Pull request',repos:'Repository',sessions:'Chat / session'}[tab];
  const link=x=>tab==='jira'?jiraUrl(x.key):tab==='prs'?prUrl(x.key):tab==='repos'?repoUrl(x.key):null;
  const unit=tab==='sessions'?'Turns':tab==='prs'?'Segments':'Chats';
  const mx=Math.max(...items.map(i=>i.cost_usd),0.0001);
  const un=(DATA.rollup&&DATA.rollup.unattributed&&DATA.rollup.unattributed[tab])||null;
  // Only meaningful on the unsearched view: a table search hides rows too, and
  // blaming that on missing references would be misleading.
  const showUn=un&&!rq&&un.cost_usd>0.005&&tab!=='sessions';
  const noun={jira:'Jira ticket',prs:'pull request',repos:'repository'}[tab]||'reference';
  const unRow=showUn?`<tr class="unattr">
      <td>No ${esc(noun)}<div class="sub">Chats in view that reference no ${esc(noun)}</div></td>
      <td>${un.chats?num(un.chats):'-'}</td><td>${kt(un.total_tokens)}</td>
      <td>${cred(un.cost_usd)}</td><td class="cost">${usd(un.cost_usd)}</td>
      <td style="width:160px"><div class="bar dim" style="width:${Math.min(100,un.cost_usd/mx*100)}%"></div></td></tr>`:'';
  document.getElementById('rollup').innerHTML=items.length?
    `<thead><tr><th>${label}</th><th>${unit}</th><th>Tokens</th><th>AI credits</th><th>Cost</th><th>Share</th></tr></thead><tbody>`+
    items.map(i=>{const u=link(i);
      const cell = (i.keys&&i.keys.length)
        ? i.keys.map(k=>`<a class="b pr" href="${prUrl(k)}" target="_blank">${esc(k.split('/').pop())}</a>`).join(' ')
          + (i.keys.length>1?` <span class="sub">${i.keys.length} PRs from one segment</span>`:'')
        : (u?`<a href="${u}" target="_blank">${esc(i.key)}</a>`:esc(i.key));
      return `<tr>
      <td>${cell}
        ${i.est?'<span class="b est" title="Includes estimated VS Code data">est</span>':''}
        ${i.created?'<span class="b prnew">✚ created</span>':''}
        ${i.turns?'<span class="b more">'+i.turns+' turn'+(i.turns===1?'':'s')+'</span>':''}
        ${i.role==='primary'?'<span class="b repo">primary</span>':''}
        <div class="sub">${esc(i.titles.join(' · '))}</div></td>
      <td>${num(i.chats)}</td><td>${kt(i.total_tokens)}</td>
      <td>${cred(i.cost_usd)}</td>
      <td class="cost">${usd(i.cost_usd)}</td>
      <td style="width:160px"><div class="bar" style="width:${i.cost_usd/mx*100}%"></div></td></tr>`;}).join('')+
    unRow+'</tbody>' : `<tbody><tr><td class="sub">No ${label.toLowerCase()} ${rq?'matches "'+esc(rq)+'"':'references found in range'}.</td></tr></tbody>`;
  const tot=items.reduce((a,b)=>a+b.cost_usd,0);
  const tk=items.reduce((a,b)=>a+(b.total_tokens||0),0);
  const plural={jira:'Jira tickets',prs:'Pull requests',repos:'Repositories',sessions:'Chats / sessions'}[tab];
  // Spell out why this table can total less than the cards and Cost by model.
  const rec=showUn
    ? ` · ${usd(tot)} of ${usd(un.total_usd)} in view attributed · ${usd(un.cost_usd)} has no ${noun} to attribute it to`
    : (un&&!rq&&tab!=='sessions'?' · matches the totals above':'');
  document.getElementById('rollupfoot').textContent=
    `${items.length} ${(items.length===1?label:plural).toLowerCase()} · ${kt(tk)} tokens · ${cred(tot)} AI credits · ${usd(tot)}`+rec;
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===tab));
}

let BUSY=0;
function busy(on,msg){
  BUSY=Math.max(0,BUSY+(on?1:-1));
  document.body.classList.toggle('busy',BUSY>0);
  if(on&&msg)document.getElementById('loadmsg').textContent=msg;
}
async function load(){
  syncPreset();
  const s=document.getElementById('start').value||'0000-01-01';
  const e=document.getElementById('end').value||'9999-12-31';
  const q=document.getElementById('q').value.trim();
  const vsOn=document.getElementById('incvs').checked?'1':'0';
  busy(true,vsOn==='1'
    ?'Loading chat costs and scanning VS Code transcripts — the first scan can take up to a minute…'
    :'Loading chat costs…');
  try{
    const r=await fetch(`/api/data?start=${s}&end=${e}&q=${encodeURIComponent(q)}&vscode=${vsOn}&basis=${document.getElementById('vsbasis').value}`);
    DATA=await r.json();
    if(DATA.error){document.getElementById('err').textContent='Error: '+DATA.error;return;}
    document.getElementById('err').textContent='';
    LAST_LOAD=new Date();
    renderLastRef();
    render();
  }catch(err){
    document.getElementById('err').textContent='Error: '+err;
  }finally{ busy(false); }
  loadVs();
}
// Freshness line under the auto-refresh box. Only a successful load moves it, so
// a failed refresh leaves the age of the data on screen honest.
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
  // The whole control stays hidden unless a recipient was configured at launch.
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
    + `Daily digest to ${d.to} via ${d.host}:${d.port}`
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
function render(){
  const t=DATA.totals;
  renderDigest();
  const budget=(DATA.mtd||{}).budget;
  const vsi=DATA.vscode||{};
  const mix=document.getElementById('mixnote');
  document.getElementById('incvs').parentElement.style.display=vsi.available?'':'none';
  if(vsi.included){
    mix.style.display='';
    mix.innerHTML=`Estimated VS Code chat data is folded into every table below: `
      +`<b>${num(vsi.sessions)} chats, ${usd(vsi.cost_usd)} (${cred(vsi.cost_usd)} credits, ${kt(vsi.total_tokens)} tokens)</b>, `
      +`costed on the <b>${vsi.basis==='premium'?'premium-request':'token'}</b> basis. `
      +`Token counts come from Copilot's own records where it wrote them and are inferred from `
      +`transcript length otherwise. Rows containing estimated data are marked <span class="b est">est</span>. `
      +`Measured CLI spend alone is ${usd(t.cost_usd-vsi.cost_usd)}. See the VS Code section for the `
      +`accuracy breakdown, or untick “+VS Code (est.)” for measured-only numbers.`;
  } else { mix.style.display='none'; }
  document.getElementById('cards').innerHTML=[
    ['Total cost',split(t.cost_usd,vsi,usd),null,1],
    ['AI credits',split(t.cost_usd,vsi,cred),null,1],
    ...(budget?[[`% of ${num(budget)} allowance`,(t.cost_usd*100/budget*100).toFixed(1)+'%',
      'Credits in the current view measured against one billing cycle allowance. Over 100% simply means the view spans more than one cycle.']]:[]),
    ['Chats / sessions',num(t.sessions)],
    ['Model requests',num(t.requests)],['Total tokens',kt(t.total_tokens)],
    ['Input',kt(t.input_tokens)],['Output',kt(t.output_tokens)],
    ['Cache write',kt(t.cache_write_tokens)],['Cache read',kt(t.cache_read_tokens)],
    ['Avg $/chat',usd(t.sessions?t.cost_usd/t.sessions:0)],
    ['Avg $/request',usd4(t.requests?t.cost_usd/t.requests:0)]
  ].map(([k,v,h,noest])=>`<div class="card"${h?` title="${h}"`:''}><div class="k">${k}${(vsi.included&&!noest)?' <span class="b est">est</span>':''}</div><div class="v">${v}</div></div>`).join('');

  const m=DATA.mtd||{};
  const mtdEl=document.getElementById('mtd');
  if(m.month){
    mtdEl.style.display='';
    const hasBudget=!!m.budget, pct=m.pct||0;
    document.getElementById('mtdUsed').textContent=num(Math.round(m.credits_used));
    // Never overwrite the box while it is being typed into.
    const bi=document.getElementById('budget');
    if(document.activeElement!==bi) bi.value=hasBudget?num(m.budget):'';
    document.getElementById('mtdPct').textContent=hasBudget?pct.toFixed(1)+'%':'—';
    document.getElementById('mtdLeft').textContent=hasBudget
      ?(m.remaining<0?'-':'')+num(Math.round(Math.abs(m.remaining))):'—';
    document.getElementById('mtdTok').textContent=kt(m.total_tokens);
    const bar=document.getElementById('mtdBar');
    bar.style.width=(hasBudget?Math.min(100,pct):0)+'%';
    bar.className=!hasBudget?'':pct>=100?'over':pct>=80?'warn':'';
    document.getElementById('mtdFoot').textContent=
      `Resets in ${m.days_left} day${m.days_left===1?'':'s'} on ${m.reset_date}`
      +` · ${num(m.requests)} requests across ${num(m.sessions)} chats · ${usd(m.cost_usd)} of model spend`
      +(hasBudget?(m.remaining<0?` · OVER allowance by ${num(Math.round(-m.remaining))} credits`:'')
                 :' · enter your allowance above to see % used and credits remaining');
    document.getElementById('mtdNote').textContent=
      'Counts Copilot CLI chats recorded locally only. Usage from the IDE, github.com or cloud '
      +'agents is not in this store, so this reads slightly lower than Copilot Settings > Usage.';
  } else {
    mtdEl.style.display='none';
  }

  const mx=Math.max(...DATA.daily.map(d=>d.cost_usd),0.0001);
  // Each column stacks measured CLI spend (blue) under estimated VS Code spend (orange).
  document.getElementById('spark').innerHTML=DATA.daily.map(d=>{
    const est=d.est_usd||0, meas=Math.max(d.cost_usd-est,0);
    const h=Math.max(1,d.cost_usd/mx*100);
    const ep=d.cost_usd>0?est/d.cost_usd*100:0;
    return `<div style="height:${h}%" title="${d.day}: ${usd(d.cost_usd)} · ${num(d.requests)} req`
      +(est>0?` · ${usd(meas)} measured + ${usd(est)} est.`:'')+`">`
      +`<i class="m" style="height:${100-ep}%"></i><i class="e" style="height:${ep}%"></i></div>`;
  }).join('');
  const anyEst=DATA.daily.some(d=>(d.est_usd||0)>0);
  document.getElementById('sparklabel').innerHTML=DATA.daily.length
    ? `<span class="sparkkey"><span>${DATA.daily[0].day} → ${DATA.daily[DATA.daily.length-1].day} `
      +`· peak ${usd(mx)}/day</span>`
      +`<span><b style="background:var(--acc)"></b>measured</span>`
      +(anyEst?`<span><b style="background:#d29922"></b>estimated (VS Code)</span>`:'')
      +'</span>' : 'no data';

  const mmax=Math.max(...DATA.models.map(m=>m.cost_usd),0.0001);
  document.getElementById('models').innerHTML=
    '<thead><tr><th>Model</th><th>Requests</th><th>Input</th><th>Cache write</th><th>Cache read</th><th>Output</th><th>AI credits</th><th>Cost</th><th>Share</th></tr></thead><tbody>'+
    DATA.models.map(m=>`<tr><td>${m.model}${m.est?' <span class="b est" title="Includes estimated VS Code data">est</span>':''}</td><td>${num(m.requests)}</td><td>${kt(m.input_tokens)}</td>
      <td>${kt(m.cache_write_tokens)}</td><td>${kt(m.cache_read_tokens)}</td><td>${kt(m.output_tokens)}</td>
      <td>${cred(m.cost_usd)}</td>
      <td class="cost">${usd(m.cost_usd)}</td><td style="width:160px"><div class="bar" style="width:${m.cost_usd/mmax*100}%"></div></td></tr>`).join('')+
    '</tbody>';

  const q=DATA.q||'';
  document.getElementById('qnote').textContent=
    q?`filtered by "${q}" — ${DATA.sessions.length} chat${DATA.sessions.length===1?'':'s'}`:'';
  let rows=DATA.sessions.slice();
  rows.sort((a,b)=>((a[sortKey]>b[sortKey])-(a[sortKey]<b[sortKey]))*sortDir);
  const cols=[['title','Chat'],['top_model','Model'],['turns','Turns'],['requests','Reqs'],
    ['input_tokens','Input'],['cache_write_tokens','Cache W'],['cache_read_tokens','Cache R'],
    ['output_tokens','Output'],['total_tokens','Tokens'],['cost_usd','Cost'],['last_day','Last used']];
  document.getElementById('sessions').innerHTML=
    '<thead><tr>'+cols.map(([k,l])=>`<th data-k="${k}">${l}${sortKey===k?(sortDir<0?' ▼':' ▲'):''}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(s=>`<tr class="row" data-id="${s.session_id}"${s.est?' data-est="1"':''}>
      <td><span class="expand">▸</span> ${esc(s.title)}${s.est?' <span class="b est" title="Estimated from VS Code transcript length; not measured">est</span>':''}<div class="sub">${esc(s.repository||'—')}${s.branch?' · '+esc(s.branch):''}</div>${badges(s.refs)}</td>
      <td>${s.top_model}${s.models>1?' <span class="sub">+'+(s.models-1)+'</span>':''}</td>
      <td>${num(s.turns)}</td><td>${num(s.requests)}</td><td>${kt(s.input_tokens)}</td>
      <td>${kt(s.cache_write_tokens)}</td><td>${kt(s.cache_read_tokens)}</td><td>${kt(s.output_tokens)}</td>
      <td>${kt(s.total_tokens)}</td><td class="cost">${usd(s.cost_usd)}</td><td class="sub">${s.last_day}</td></tr>`).join('')+
    '</tbody>';
  renderRollup();
  document.querySelectorAll('#sessions th').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; sortDir = sortKey===k ? -sortDir : -1; sortKey=k; render();});
  document.querySelectorAll('#sessions tr.row').forEach(tr=>tr.onclick=e=>{
    if(e.target.tagName==='A') return; toggle(tr);});
  document.getElementById('foot').textContent=
    `${rows.length} chats shown · source ${DATA.db} · cost = total_nano_aiu / 1e11 USD`;
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function toggle(tr){
  if(tr.nextElementSibling&&tr.nextElementSibling.classList.contains('turns')){
    tr.nextElementSibling.remove(); tr.querySelector('.expand').textContent='▸'; return;}
  tr.querySelector('.expand').textContent='▾';
  if(tr.dataset.est){
    const td=document.createElement('tr'); td.className='turns';
    td.innerHTML='<td colspan="11">Per-turn detail is not available for estimated VS Code chats — '
      +'VS Code stores no per-request usage records. See the VS Code Copilot chat section for how '
      +'this figure is derived.</td>';
    tr.after(td); return;
  }
  const ph=document.createElement('tr'); ph.className='turns';
  ph.innerHTML='<td colspan="11"><span class="sub">Loading turns…</span></td>';
  tr.after(ph);
  const turns=await (await fetch('/api/turns?session_id='+encodeURIComponent(tr.dataset.id))).json();
  ph.remove();
  if(!tr.isConnected||tr.querySelector('.expand').textContent==='▸')return;
  const td=document.createElement('tr'); td.className='turns';
  td.innerHTML=`<td colspan="11"><table>${turns.map(t=>
    `<tr><td>Turn ${t.turn_index} <span class="sub">${(t.started_at||'').slice(0,16).replace('T',' ')}</span></td>
     <td>${t.model}</td><td>${num(t.requests)} req</td><td>${kt(t.input_tokens)} in</td>
     <td>${kt(t.output_tokens)} out</td><td>${kt(t.total_tokens)} tok</td>
     <td class="cost">${usd4(t.cost_usd)}</td></tr>`).join('')}</table></td>`;
  tr.after(td);
}
document.getElementById('refresh').onclick=load;
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{tab=b.dataset.t;renderRollup();});
document.getElementById('rq').oninput=()=>DATA&&renderRollup();
let qtimer=null;
// Saving the allowance then reloading, so every derived figure (% used,
// remaining, bar) is recomputed by the server rather than patched locally.
document.getElementById('budget').onchange=async e=>{
  try{ await fetch('/api/credits?value='+encodeURIComponent(e.target.value)); }catch(err){}
  load();
};
document.getElementById('q').oninput=()=>{clearTimeout(qtimer);qtimer=setTimeout(load,250);};
// Overlay clear buttons. Clearing dispatches a real input event rather than
// calling each box's handler directly, so a cleared box behaves exactly as if
// the text had been deleted by hand and the two can never drift apart.
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

// --- VS Code (estimated) ---------------------------------------------------
// Loaded lazily and rendered in its own section: it has no real token data, so
// it must never mix into the exact CLI figures above.
let VS=null;
function renderVs(){
  const d=VS; if(!d||!d.available) return;
  const sec=document.getElementById('vs');
  if(!d.totals.sessions){
    // A filter that matches no VS Code chat must clear the section rather than
    // leave the previous, wider result standing.
    if(d.filtered){
      sec.style.display='';
      document.getElementById('vshead').textContent=' — no chats match the current filter';
      document.getElementById('vsbadge').textContent='ESTIMATED';
      ['vsnote','vscards','vscov','vsmodels','vssessions','vsfoot']
        .forEach(id=>document.getElementById(id).innerHTML='');
    }else{ sec.style.display='none'; }
    return;
  }
  sec.style.display='';
  const t=d.totals;
  document.getElementById('vshead').textContent=d.filtered
    ? ` — ${num(t.sessions)} matching chats`
    : ` — ${num(t.sessions)} chats across ${d.files} transcript files`;
  const pct=t.requests?Math.round(t.measured_out/t.requests*100):0;
  const lo=Math.min(t.cost_usd,t.premium_usd), hi=Math.max(t.cost_usd,t.premium_usd);
  // Weight by spend, not request count: measured requests skew recent and larger,
  // so "32% of requests" badly understates how much of the money is real.
  const mc=t.measured_cost_usd||0, mp=t.measured_premium_usd||0;
  const cpct=t.cost_usd?Math.round(mc/t.cost_usd*100):0;
  const ppct=t.premium_usd?Math.round(mp/t.premium_usd*100):0;
  const tpct=t.total_tokens?Math.round((t.measured_tokens||0)/t.total_tokens*100):0;
  // Headline honesty check: how much of this section is real rather than inferred.
  const badge=document.getElementById('vsbadge');
  badge.textContent=`ESTIMATED · ${tpct}% REAL TOKENS`;
  badge.title=`${kt(t.measured_tokens||0)} of ${kt(t.total_tokens)} tokens come from counts `
    +`Copilot recorded (${num(t.measured_out)} of ${num(t.requests)} requests). `
    +`The remainder is inferred from transcript length.`;
  document.getElementById('vsnote').innerHTML=
    `<b>Likely range ${usd(lo)} – ${usd(hi)}.</b> <b>${cpct}% of the token-model total `
    +`(${usd(mc)} of ${usd(t.cost_usd)}) is built from real token counts</b>, as is ${tpct}% of the `
    +`tokens and ${ppct}% of the premium-request total. Two independent models are shown because GitHub `
    +'bills chat on a different basis to raw tokens. Switch which one drives the tables above with '
    +'the “VS Code basis” selector in the header.<br>'
    +`<b>Token model (${usd(t.cost_usd)}):</b> priced with Copilot's own per-model rates where the `
    +"transcript records them, published API rates otherwise. Token counts come from Copilot's "
    +`<code>result.usage</code> where present; otherwise they are inferred as characters / `
    +`${d.chars_per_token} over the fully rendered prompt (editor context, terminal state and attached `
    +`instruction files, not just what you typed), with resent history capped at `
    +`${kt(d.context_window)} tokens.<br>`
    +`<b>Premium-request model (${usd(t.premium_usd)}):</b> ${num(Math.round(t.premium_requests))} premium `
    +`requests × ${usd4(d.premium_usd_each)}, using each model's own multiplier where Copilot recorded `
    +'it. This is how GitHub meters chat, but it ignores conversation length entirely.<br>'
    +`<b>Accuracy:</b> real token counts for <b>${num(t.measured_out)} of ${num(t.requests)} requests `
    +`(${pct}% by count, ${cpct}% by spend)</b>`+(t.exact_credits?`, and the exact credits charged for ${num(t.exact_credits)}`:'')
    +'. Older transcripts predate these fields, so the rest is inferred — treat the total as an '
    +'order-of-magnitude figure, not a bill.';
  document.getElementById('vscards').innerHTML=[
    ['Est. cost (tokens)',usd(t.cost_usd)],['Est. AI credits',cred(t.cost_usd)],
    ['Est. cost (premium reqs)',usd(t.premium_usd)],
    ['Premium requests',num(Math.round(t.premium_requests))],
    ['Chats',num(t.sessions)],['Requests',num(t.requests)],['Est. tokens',kt(t.total_tokens)]
  ].map(([k,v])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
  renderCoverage(d);
  const mmx=Math.max(...d.models.map(m=>m.cost_usd),0.0001);
  document.getElementById('vsmodels').innerHTML=
    '<thead><tr><th>Model</th><th>Reqs</th><th>Est. tokens</th><th>Est. credits</th><th>Est. cost</th><th>Share</th></tr></thead><tbody>'+
    d.models.map(m=>`<tr><td>${esc(m.model)}${m.known_rate?'':' <span class="b more" title="No published rate for this model id; Sonnet-class rates assumed">assumed rate</span>'}</td>
      <td>${num(m.requests)}</td>
      <td>${kt(m.input_tokens+m.output_tokens+m.cache_read_tokens)}</td>
      <td>${cred(m.cost_usd)}</td><td class="cost">${usd(m.cost_usd)}</td>
      <td style="width:160px"><div class="bar" style="width:${m.cost_usd/mmx*100}%"></div></td></tr>`).join('')+
    '</tbody>';
  document.getElementById('vssessions').innerHTML=
    '<thead><tr><th>VS Code chat</th><th>Model</th><th>Reqs</th><th>Est. tokens</th><th>Est. credits</th><th>Est. cost</th><th>Last used</th></tr></thead><tbody>'+
    d.sessions.map(s=>`<tr>
      <td>${esc(s.title)}<div class="sub">${esc(s.workspace||'—')}${s.insiders?' · Insiders':''}</div>${badges(s.refs)}</td>
      <td>${esc(s.top_model)}${s.models>1?' <span class="sub">+'+(s.models-1)+'</span>':''}</td>
      <td>${num(s.requests)}</td><td>${kt(s.total_tokens)}</td>
      <td>${cred(s.cost_usd)}</td><td class="cost">${usd(s.cost_usd)}</td>
      <td class="sub">${s.last_day||'—'}</td></tr>`).join('')+
    '</tbody>';
  document.getElementById('vsfoot').textContent=
    `${num(t.sessions)} chats · ${num(t.requests)} requests · ${kt(t.total_tokens)} est. tokens · `
    +`${cred(t.cost_usd)} est. AI credits · ${usd(t.cost_usd)} est. · scanned ${d.roots.join(', ')}`;
}
// Shows when this user's own transcripts started carrying real token counts,
// so they can tell whether their numbers are estimated because of their VS Code
// build or because of something else.
function renderCoverage(d){
  const el=document.getElementById('vscov');
  const cov=(d.coverage||[]).filter(c=>c.month!=='undated');
  const flds=d.fields||[];
  if(!cov.length&&!flds.length){el.innerHTML='';return;}
  // First month in which at least a quarter of requests carried real counts.
  const turn=cov.find(c=>c.pct>=25);
  const now=cov[cov.length-1];
  let head='';
  if(turn){
    head=`Your transcripts began recording real token counts in <b>${turn.month}</b>`
      +` (${Math.round(turn.pct)}% of that month's requests). `
      +`Your most recent month, <b>${now.month}</b>, is <b>${Math.round(now.pct)}%</b> measured.`;
  }else{
    head='None of your transcripts carry real token counts yet — every figure here is inferred. '
      +'Update VS Code and Copilot Chat to start recording them.';
  }
  el.innerHTML='<h3>When your VS Code data became measurable</h3>'
    +`<div class="note">${head} VS Code does not stamp its own version into chat transcripts, `
    +'so these are the dates the fields actually appear in <i>your</i> files rather than published '
    +'release numbers. Anything before the first date below is estimated from transcript length.</div>'
    +(flds.length?'<table><thead><tr><th>Field Copilot recorded</th><th>First seen</th>'
      +'<th>Last seen</th><th>Reqs</th><th>What it gives us</th></tr></thead><tbody>'
      +flds.map(f=>`<tr><td><code>${esc(f.field)}</code></td>`
        +`<td>${f.first||'—'}</td><td>${f.last||'—'}</td><td>${num(f.requests)}</td>`
        +`<td class="sub">${esc(f.note||'')}</td></tr>`).join('')+'</tbody></table>':'')
    +(cov.length?'<div style="height:10px"></div><table><thead><tr><th>Month</th><th>Reqs</th>'
      +'<th>Measured</th><th>Exact credits</th><th>% measured</th></tr></thead><tbody>'
      +cov.map(c=>`<tr><td>${c.month}</td><td>${num(c.requests)}</td><td>${num(c.measured)}</td>`
        +`<td>${c.exact?num(c.exact):'—'}</td>`
        +`<td style="width:200px"><div class="bar" style="width:${c.pct}%"></div>`
        +`<span class="sub"> ${Math.round(c.pct)}%</span></td></tr>`).join('')
      +'</tbody></table>':'');
}
async function loadVs(){
  busy(true);
  const q=document.getElementById('q').value.trim();
  const basis=document.getElementById('vsbasis').value;
  const s=document.getElementById('start').value||'0000-01-01';
  const e=document.getElementById('end').value||'9999-12-31';
  try{
    VS=await (await fetch(`/api/vscode?q=${encodeURIComponent(q)}&basis=${basis}`
      +`&start=${s}&end=${e}`)).json();
    renderVs();
  }
  catch(e){}
  finally{ busy(false); }
}
document.getElementById('start').onchange=load;
document.getElementById('incvs').onchange=load;
document.getElementById('vsbasis').onchange=load;
document.getElementById('end').onchange=load;
// Resolve a preset to concrete dates *now*. Kept separate from the change handler
// so a page left open across midnight can re-resolve it on the next refresh.
function presetRange(v){
  // Every preset is anchored to local midnight. Formatting must stay local too:
  // toISOString() is UTC and would roll "today" over to tomorrow each evening.
  const ymd=d=>{const p=n=>String(n).padStart(2,'0');
    return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());};
  const n=new Date(), today=new Date(n.getFullYear(),n.getMonth(),n.getDate());
  const back=k=>{const d=new Date(today); d.setDate(d.getDate()-k); return d;};
  const out=(a,b)=>[a?ymd(a):'', b?ymd(b):''];
  if(v==='all') return out(null,null);
  if(v==='today') return out(today,today);
  if(v==='yesterday') return out(back(1),back(1));
  if(v==='week') return out(back((today.getDay()+6)%7),today);  // week starts Monday
  if(v==='mtd') return out(new Date(today.getFullYear(),today.getMonth(),1),today);
  if(v==='lastmonth') return out(new Date(today.getFullYear(),today.getMonth()-1,1),
                                 new Date(today.getFullYear(),today.getMonth(),0));
  return out(back(+v-1),today);   // "Last N days" spans N days, today included
}
// What the active preset last wrote into the date boxes. Anything else in them
// means the user typed their own dates, which are never overwritten.
let PRESET_LOCK=null;
function applyPreset(v){
  const [a,b]=presetRange(v);
  document.getElementById('start').value=a;
  document.getElementById('end').value=b;
  PRESET_LOCK={v:v,start:a,end:b};
}
// Re-anchor a relative preset when the local day has rolled over, so "Today" on a
// page left open overnight means today rather than the day it was selected.
function syncPreset(){
  if(!PRESET_LOCK||PRESET_LOCK.v==='all') return false;
  const S=document.getElementById('start'), E=document.getElementById('end');
  if(S.value!==PRESET_LOCK.start||E.value!==PRESET_LOCK.end) return false;
  const [a,b]=presetRange(PRESET_LOCK.v);
  if(a===PRESET_LOCK.start&&b===PRESET_LOCK.end) return false;
  S.value=a; E.value=b; PRESET_LOCK={v:PRESET_LOCK.v,start:a,end:b};
  return true;
}
document.getElementById('preset').onchange=e=>{ applyPreset(e.target.value); load(); };
let timer=null;
// The checkbox ships checked, but onchange never fires on load - so the interval
// has to be armed explicitly at startup or auto-refresh silently never runs.
function setAuto(on){ clearInterval(timer); timer = on ? setInterval(load,300000) : null; }
document.getElementById('auto').onchange=e=>setAuto(e.target.checked);
setAuto(document.getElementById('auto').checked);
// Every section folds away; the choice sticks across reloads.
document.querySelectorAll('main section').forEach(s=>{
  const h=s.querySelector('h2'); if(!h) return;
  const key='fold:'+h.textContent.trim().slice(0,40);
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
    global DB_PATH, JIRA_BASE, CREDIT_BUDGET, VSCODE_ENABLED, APP_DB_PATH
    global EMAIL_TO, EMAIL_FROM, SMTP_HOST, SMTP_PORT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="Copilot session store (default: ~/.copilot/session-store.db)")
    ap.add_argument("--app-db", default="",
                    help="Copilot desktop app store, read for the names you give "
                         "chats (default: data.db beside --db). Optional.")
    ap.add_argument("--jira-base", default=JIRA_BASE_DEFAULT,
                    help="Jira base URL, e.g. https://acme.atlassian.net. "
                         "Auto-detected from your chat history when omitted.")
    ap.add_argument("--jira-keys", default="",
                    help="Extra Jira project keys to recognize, comma separated (e.g. ABC,DEF)")
    ap.add_argument("--ai-credits", default=None,
                    help="Your AI credit allowance per billing cycle, as shown in Copilot "
                         f"Settings > Usage (e.g. 60000 or 60,000). Default {DEFAULT_CREDITS:,}. "
                         "Editable in the banner and remembered between runs; passing this "
                         "overrides the saved value. Env: COPILOT_DASH_AI_CREDITS")
    ap.add_argument("--no-vscode", action="store_true",
                    help="Skip the estimated VS Code Copilot chat section. Env: "
                         "COPILOT_DASH_VSCODE_DIR overrides where transcripts are found.")
    ap.add_argument("--email-to", default=EMAIL_TO,
                    help="Email a digest of the previous active day's Copilot activity on "
                         "the first refresh of each day. Omit to disable. "
                         "Env: COPILOT_DASH_EMAIL_TO")
    ap.add_argument("--email-from", default=EMAIL_FROM,
                    help="Digest sender address (default: copilot-dashboard@<your domain>). "
                         "Env: COPILOT_DASH_EMAIL_FROM")
    ap.add_argument("--smtp-host", default=SMTP_HOST,
                    help=f"SMTP relay for the digest (default: {SMTP_HOST}). "
                         f"Env: COPILOT_DASH_SMTP_HOST")
    ap.add_argument("--smtp-port", type=int, default=SMTP_PORT,
                    help=f"SMTP port (default: {SMTP_PORT}). Env: COPILOT_DASH_SMTP_PORT")
    ap.add_argument("--send-digest", action="store_true",
                    help="Send the digest immediately on startup, then exit. For testing "
                         "or for driving the digest from a scheduled task.")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    DB_PATH = args.db
    APP_DB_PATH = args.app_db
    EMAIL_TO = args.email_to.strip()
    EMAIL_FROM = args.email_from.strip()
    SMTP_HOST = args.smtp_host.strip()
    SMTP_PORT = args.smtp_port
    VSCODE_ENABLED = not args.no_vscode
    JIRA_BASE = args.jira_base.rstrip("/")
    if args.ai_credits is not None or CREDIT_BUDGET:
        raw = args.ai_credits if args.ai_credits is not None else CREDIT_BUDGET
        try:
            CREDIT_BUDGET = int(float(str(raw).replace(",", "").replace("_", "")))
        except ValueError:
            raise SystemExit(f"--ai-credits must be a number, got: {raw!r}")
        credits_set(CREDIT_BUDGET)               # an explicit launch value wins and sticks
    else:
        CREDIT_BUDGET = int(_state_load().get("ai_credits", DEFAULT_CREDITS))
    JIRA_KEY_ALLOW.update(k.strip().upper() for k in args.jira_keys.split(",") if k.strip())
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Session store not found: {DB_PATH}\n"
                         f"Pass --db PATH if your Copilot data lives elsewhere.")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    if args.send_digest:
        if not EMAIL_TO:
            raise SystemExit("--send-digest needs --email-to")
        day, recent = _find_digest_day(datetime.date.today().isoformat())
        if not day:
            raise SystemExit(f"No Copilot activity in the last {DIGEST_LOOKBACK_DAYS} days")
        send_digest(day, recent)
        print(f"digest for {day} sent to {EMAIL_TO}")
        return
    print(f"Copilot cost dashboard -> {url}  (db: {DB_PATH})")
    if EMAIL_TO:
        off = "" if _digest_enabled() else "  [switched off in the dashboard]"
        print(f"  daily digest -> {EMAIL_TO} via {SMTP_HOST}:{SMTP_PORT}{off}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
