"""Create Jira epics/stories/bugs for Cursor work since 2025-11-29, then close them.

Hierarchy (TIM team-managed project):
  Epic  — one per repository (+ optional cross-repo epic)
  Story — Cursor billed sessions and agent chats (primary work units)
  Bug   — defect / fix / error sessions
  (no subtasks — git commit log lives in the epic description)
"""

import argparse
import base64
import collections
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cursor_dashboard as cd

JIRA_BASE = "https://timberwilde.atlassian.net"
PROJECT = "TIM"
START_DATE = "2025-11-29"
TRANSCRIPT_ROOTS = [
    os.path.join(
        os.environ.get("USERPROFILE", ""),
        ".cursor", "projects",
        "c-Users-mw-OneDrive-timberwilde-net-repos",
        "agent-transcripts",
    ),
    os.path.join(
        os.environ.get("USERPROFILE", ""),
        ".cursor", "projects",
        "c-Users-mw-OneDrive-timberwilde-net-repos-evernote-remarkable",
        "agent-transcripts",
    ),
]
REPOS = [
    r"c:\Users\mw\OneDrive - timberwilde.net\repos\evernote_remarkable",
    r"c:\Users\mw\OneDrive - timberwilde.net\repos\fastcat",
    r"c:\Users\mw\OneDrive - timberwilde.net\repos\jrtca_results",
    r"c:\Users\mw\OneDrive - timberwilde.net\repos\horse_shows",
    r"c:\Users\mw\OneDrive - timberwilde.net\repos\3dprinting",
    r"c:\Users\mw\OneDrive - timberwilde.net\repos\dashboard",
]
REPO_NAMES = [os.path.basename(p) for p in REPOS]
GET_CRED_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "get_atlassian_credential.ps1")
RE_PR_KEY = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
RE_PR_BARE = re.compile(r"\b(?:PR|pull request)\s*#(\d+)\b", re.I)
RE_MERGE_PR = re.compile(r"merge pull request #(\d+)", re.I)

_github_token = None
_pr_title_cache = {}


def _github_auth():
    global _github_token
    if _github_token is not None:
        return _github_token
    try:
        out = subprocess.check_output(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if line.startswith("password="):
                _github_token = line.split("=", 1)[1]
                break
    except Exception:
        _github_token = ""
    return _github_token


def pr_url(p):
    repo = p.get("repo") or ""
    num = p.get("number")
    if not repo or num is None:
        key = p.get("key") or ""
        if "#" in key:
            repo, num = key.split("#", 1)
            num = int(num)
    if repo and num is not None:
        if "/" not in repo:
            repo = f"mwtorq/{repo}"
        return f"https://github.com/{repo}/pull/{num}"
    return ""


def pr_short(p):
    repo = p.get("repo") or ""
    num = p.get("number")
    if repo and num is not None:
        short = repo.split("/")[-1] if "/" in repo else repo
        return f"{short}#{num}"
    return p.get("key") or ""


def _pr_key(p):
    if p.get("key"):
        return p["key"]
    repo = p.get("repo") or ""
    num = p.get("number")
    if repo and num is not None:
        if "/" not in repo:
            repo = f"mwtorq/{repo}"
        return f"{repo}#{num}"
    return ""


def fetch_pr_title(p):
    key = _pr_key(p)
    if not key:
        return ""
    if key in _pr_title_cache:
        return _pr_title_cache[key]
    repo = p.get("repo") or key.split("#")[0]
    num = p.get("number") or int(key.split("#")[1])
    if "/" not in repo:
        repo = f"mwtorq/{repo}"
    token = _github_auth()
    title = ""
    if token:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/pulls/{num}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                title = (json.loads(r.read()).get("title") or "").strip()
        except Exception:
            pass
    _pr_title_cache[key] = title
    return title


def collect_session_prs(s):
    seen = {}
    for p in (s.get("refs") or {}).get("prs") or []:
        if p.get("role") == "skipped":
            continue
        k = _pr_key(p)
        if k:
            seen[k] = dict(p)
    gc = s.get("git_correlation") or {}
    for p in gc.get("prs") or []:
        k = _pr_key(p)
        if k and k not in seen:
            seen[k] = dict(p)
    for row in gc.get("repos") or []:
        for pk in row.get("prs") or []:
            if isinstance(pk, str) and "#" in pk:
                repo, num = pk.split("#", 1)
                k = f"{repo}#{num}"
                if k not in seen:
                    seen[k] = {"key": k, "repo": repo, "number": int(num), "inferred": True}
    return sorted(seen.values(), key=lambda x: (x.get("repo") or "", x.get("number") or 0))


def prs_from_text(text):
    seen = {}
    for owner, repo, num in RE_PR_KEY.findall(text or ""):
        k = f"{owner}/{repo}#{num}"
        seen[k] = {"key": k, "repo": f"{owner}/{repo}", "number": int(num)}
    return list(seen.values())


def prs_for_repo_from_git(repo_path):
    name = os.path.basename(repo_path)
    full = f"mwtorq/{name}"
    seen = {}
    try:
        log = subprocess.check_output(
            ["git", "-C", repo_path, "log", f"--since={START_DATE}",
             "--merges", "--pretty=format:%s"],
            text=True, errors="replace",
        )
        for subject in log.splitlines():
            m = RE_MERGE_PR.search(subject)
            if m:
                num = int(m.group(1))
                k = f"{full}#{num}"
                seen[k] = {"key": k, "repo": full, "number": num, "merge_subject": subject.strip()}
    except Exception:
        pass
    return sorted(seen.values(), key=lambda x: x["number"])


def repo_prs(name, repo_sessions, repo_path):
    seen = {}
    for p in prs_for_repo_from_git(repo_path):
        seen[_pr_key(p)] = p
    for s in repo_sessions:
        for p in collect_session_prs(s):
            seen[_pr_key(p)] = p
    return sorted(seen.values(), key=lambda x: (x.get("repo") or "", x.get("number") or 0))


def format_pr_lines(prs):
    if not prs:
        return ["Pull requests: (none linked)"]
    lines = ["Pull requests:"]
    for p in prs:
        url = pr_url(p)
        short = pr_short(p)
        title = fetch_pr_title(p)
        flags = []
        if p.get("created"):
            flags.append("created in chat")
        if p.get("inferred"):
            flags.append("git-inferred")
        if p.get("bare"):
            flags.append("bare mention")
        extra = f" [{', '.join(flags)}]" if flags else ""
        gc_cost = p.get("cost_usd")
        if gc_cost:
            extra += f" (${gc_cost:.2f} attributed)"
        line = f"  • {short}"
        if title:
            line += f" — {title}"
        if url:
            line += f"\n    {url}"
        line += extra
        lines.append(line)
    return lines


def summary_with_prs(prefix, title, prs, max_len=250):
    base = f"[{prefix}] {title}"
    if not prs:
        return base[:max_len]
    tags = [pr_short(p) for p in prs[:4]]
    suffix = " PR:" + ",".join(tags)
    if len(prs) > 4:
        suffix += f"+{len(prs)-4}"
    room = max_len - len(suffix)
    if len(base) > room:
        base = base[: max(room - 1, 20)] + "…"
    return base + suffix


JIRA_SUMMARY_MAX = 250
JIRA_DESC_MAX = 30000
ADF_TEXT_CHUNK = 8000


def truncate_summary(text, max_len=JIRA_SUMMARY_MAX):
    """Single-line Jira summary within field limit."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _adf_text_parts(text, max_chunk=ADF_TEXT_CHUNK):
    """Split long strings into ADF-safe text nodes."""
    if not text:
        return [{"type": "text", "text": ""}]
    parts = []
    for i in range(0, len(text), max_chunk):
        parts.append({"type": "text", "text": text[i : i + max_chunk]})
    return parts


def adf_description(text):
    """Plain text description with clickable PR URLs; chunk long lines for ADF."""
    text = (text or "").strip() or "(none)"
    if len(text) > JIRA_DESC_MAX:
        text = text[: JIRA_DESC_MAX - 40].rstrip() + "\n\n[Description truncated for Jira size limit]"
    content = []
    for para in text.split("\n"):
        if not para.strip():
            content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})
            continue
        parts = []
        pos = 0
        for m in re.finditer(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", para):
            if m.start() > pos:
                parts.extend(_adf_text_parts(para[pos:m.start()]))
            href = m.group(0)
            parts.append({"type": "text", "text": href,
                          "marks": [{"type": "link", "attrs": {"href": href}}]})
            pos = m.end()
        if pos < len(para):
            parts.extend(_adf_text_parts(para[pos:]))
        if not parts:
            parts = _adf_text_parts(para)
        content.append({"type": "paragraph", "content": parts})
    if not content:
        content = [{"type": "paragraph", "content": _adf_text_parts(text)}]
    return {"type": "doc", "version": 1, "content": content}


def format_git_correlation(gc):
    """Expand git correlation object into readable lines."""
    if not gc.get("matched"):
        return []
    lines = [
        "Git correlation:",
        f"  Matched cost: ${gc.get('matched_cost_usd', 0):.2f}",
        f"  Unmatched cost: ${gc.get('unmatched_cost_usd', 0):.2f}",
        f"  Window: ±{gc.get('window_hours', 0)}h",
    ]
    for row in gc.get("repos") or []:
        prs = ", ".join(row.get("prs") or []) or "(none)"
        lines.append(
            f"  Repo {row.get('name')}: ${row.get('cost_usd', 0):.2f}, "
            f"{row.get('turn_matches', 0)} turn matches, "
            f"{row.get('unique_commits', 0)} commits, PRs: {prs}"
        )
        for sample in row.get("sample") or []:
            lines.append(
                f"    {sample.get('sha')} ({sample.get('delta_h')}h): {sample.get('subject')}"
            )
    for row in gc.get("prs") or []:
        lines.append(
            f"  PR {row.get('key')}: ${row.get('cost_usd', 0):.2f}, "
            f"{row.get('turn_matches', 0)} turn matches"
        )
        for sample in row.get("sample") or []:
            lines.append(
                f"    {sample.get('sha')} ({sample.get('delta_h')}h): {sample.get('subject')}"
            )
    for day in gc.get("days") or []:
        repos = ", ".join(day.get("repos") or [])
        lines.append(f"  Day {day.get('day')}: ${day.get('cost_usd', 0):.2f} — {repos}")
    return lines


def format_model_breakdown(s):
    lines = []
    by_model = s.get("by_model") or {}
    if not by_model:
        return lines
    lines.append("\nCost by model:")
    for model, c in sorted(by_model.items(), key=lambda x: -(x[1].get("cost_usd") or 0)):
        lines.append(
            f"  {model}: ${c.get('cost_usd', 0):.4f}, "
            f"{c.get('requests', 0)} req, {c.get('total_tokens', 0):,} tokens"
        )
    return lines


def format_daily_breakdown(s):
    lines = []
    days = s.get("days") or {}
    if not days:
        return lines
    lines.append("\nDaily breakdown:")
    for day in sorted(days):
        c = days[day]
        lines.append(
            f"  {day}: ${c.get('cost_usd', 0):.4f}, "
            f"{c.get('requests', 0)} req, {c.get('total_tokens', 0):,} tokens"
        )
    return lines


def session_origin(s):
    if s.get("cloud_agent") or (s.get("session_id") or "").startswith("bc-"):
        return "cloud agent"
    if s.get("subagent"):
        return "local IDE (subagent)"
    return "local IDE"


def session_description(s):
    lines = [
        f"Cursor session: {s.get('session_id', '')}",
        f"Origin: {session_origin(s)}",
        f"Account: {s.get('account_label') or s.get('account') or ''}",
        f"Dates: {s.get('first_day')} — {s.get('last_day')}",
        f"Cost USD: ${s.get('cost_usd', 0):.4f}",
        f"On-demand USD: ${s.get('on_demand_usd', 0):.4f}",
        f"Tokens: {s.get('total_tokens', 0):,} (in {s.get('input_tokens', 0):,} / out {s.get('output_tokens', 0):,})",
        f"Requests: {s.get('requests', 0)}",
        f"Top model: {s.get('top_model') or ''}",
        f"Repository: {s.get('repository') or ''}",
        f"Repo source: {s.get('repo_source') or ''}",
        f"Branch: {s.get('branch') or ''}",
        f"Workspace: {s.get('workspace') or ''}",
    ]
    if s.get("cloud_url"):
        lines.append(f"Cloud agent URL: {s['cloud_url']}")
    if s.get("source"):
        lines.append(f"Data source: {s['source']}")
    if s.get("billing_note"):
        lines.append(f"Billing note: {s['billing_note']}")
    if s.get("subtitle"):
        lines.append(f"Subtitle: {s['subtitle']}")
    refs = s.get("refs") or {}
    if refs.get("jira"):
        lines.append(f"Jira refs: {', '.join(refs['jira'])}")
    prs = collect_session_prs(s)
    lines.extend(format_pr_lines(prs))
    if refs.get("repos"):
        lines.append("Repos: " + ", ".join(r.get("name", str(r)) for r in refs["repos"]))
    gc = s.get("git_correlation") or {}
    lines.extend(format_git_correlation(gc))
    lines.extend(format_model_breakdown(s))
    lines.extend(format_daily_breakdown(s))
    if s.get("summary"):
        lines.append(f"\nSummary:\n{s['summary']}")
    return "\n".join(lines)


def chat_summary_title(t):
    """Short title for Jira summary field; full text stays in description."""
    src = t.get("full_query") or t.get("title") or t.get("id") or "Untitled"
    return truncate_summary(src)


def transcript_description(t):
    prs = prs_from_text((t.get("full_query") or t.get("title") or ""))
    lines = [
        "Agent transcript (no separate billed session).",
        f"Origin: {t.get('origin') or 'local IDE'}",
        f"ID: {t['id']}",
        f"Modified: {t['mtime']}",
    ]
    if t.get("path"):
        lines.append(f"Transcript: {t['path']}")
    if t.get("message_count"):
        lines.append(f"Messages: {t['message_count']}")
    queries = t.get("user_queries") or []
    if not queries and t.get("full_query"):
        queries = [t["full_query"]]
    if not queries and t.get("title"):
        queries = [t["title"]]
    if len(queries) == 1:
        lines.append(f"\nUser query:\n{queries[0]}")
    elif queries:
        lines.append(f"\nUser queries ({len(queries)}):")
        for i, q in enumerate(queries, 1):
            lines.append(f"\n--- Query {i} ---\n{q}")
    if t.get("last_assistant"):
        lines.append(f"\nLast assistant reply (excerpt):\n{t['last_assistant']}")
    lines.extend(format_pr_lines(prs))
    return "\n".join(lines)


def repo_epic_description(name, repo_sessions, commits, repo_path):
    cost = sum(s.get("cost_usd") or 0 for s in repo_sessions)
    prs = repo_prs(name, repo_sessions, repo_path)
    desc = textwrap.dedent(f"""\
        Cursor AI work backfill for repository `{name}`.
        Period: {START_DATE} — {{end}}
        Cursor sessions: {len(repo_sessions)}
        Metered cost (sessions): ${cost:.2f}
    """).format(end=datetime.date.today().isoformat())
    desc += "\n" + "\n".join(format_pr_lines(prs)) + "\n"
    desc += "\n" + git_epic_section(name, commits)
    return desc


BUG_WORDS = (
    "bug", "fix", "fixed", "error", "broken", "fail", "failed", "failing",
    "defect", "diagnose", "debug", "regression", "crash", "incorrect", "wrong",
    "not working", "doesn't work", "does not work", "issue with", "repair",
)


def _jira_auth():
    raw = subprocess.check_output(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", GET_CRED_PS1],
        text=True,
    ).strip()
    c = json.loads(raw)
    token = base64.b64encode(f"{c['Email']}:{c['Token']}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def jira_request(method, path, body=None, headers=None):
    hdrs = dict(_jira_auth())
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(JIRA_BASE + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        snippet = exc.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"Jira {exc.code} {path}: {snippet}") from exc


def adf_text(text):
    text = (text or "").strip() or "(none)"
    if len(text) > JIRA_DESC_MAX:
        text = text[: JIRA_DESC_MAX - 40].rstrip() + "\n\n[Description truncated for Jira size limit]"
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": _adf_text_parts(text)}]}


def create_issue(fields):
    return jira_request("POST", "/rest/api/3/issue", {"fields": fields})["key"]


def update_issue(key, fields):
    jira_request("PUT", f"/rest/api/3/issue/{key}", {"fields": fields})


def search_issues(jql, max_results=120):
    issues = []
    token = None
    while len(issues) < max_results:
        body = {
            "jql": jql,
            "maxResults": min(100, max_results - len(issues)),
            "fields": ["summary", "issuetype", "labels"],
        }
        if token:
            body["nextPageToken"] = token
        data = jira_request("POST", "/rest/api/3/search/jql", body)
        issues.extend(data.get("issues") or [])
        if data.get("isLast") or not data.get("nextPageToken"):
            break
        token = data["nextPageToken"]
    return issues[:max_results]


def run_update_prs(sessions, git_data, by_repo, extra_transcripts, end):
    """Patch cursor-backfill-v2 issues with PR summaries and descriptions."""
    issues = search_issues("project=TIM AND labels=cursor-backfill-v2 ORDER BY key ASC", 250)
    print(f"Found {len(issues)} v2 issues to update", flush=True)

    repo_paths = {os.path.basename(p): p for p in REPOS}
    epic_summaries = {f"{n}: Cursor AI work ({START_DATE}": n for n in REPO_NAMES}

    session_by_title = {}
    session_by_id = {}
    for s in sessions:
        title = (s.get("title") or s.get("session_id") or "Untitled").strip()
        if s.get("cloud_agent") and "cloud agent" not in title.lower():
            title = f"{title} (cloud agent)"
        for prefix in ("Cursor", "Defect"):
            session_by_title[f"[{prefix}] {title}"] = s
            session_by_title[normalize_title(f"[{prefix}] {title}")] = s
        session_by_title[normalize_title(title)] = s
        sid = (s.get("session_id") or "").lower()
        if sid:
            session_by_id[sid] = s

    transcript_by_title = {}
    for t in extra_transcripts:
        transcript_by_title[f"[Chat] {t['title']}"] = t
        transcript_by_title[normalize_title(t["title"])] = t

    updated = 0
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        itype = (issue["fields"]["issuetype"] or {}).get("name") or ""

        if itype == "Epic":
            repo = None
            for prefix, name in epic_summaries.items():
                if summary.startswith(prefix):
                    repo = name
                    break
            if not repo:
                continue
            prs = repo_prs(repo, by_repo.get(repo, []), repo_paths[repo])
            pr_tag = f" PR:{','.join(pr_short(p) for p in prs[:6])}" if prs else ""
            if len(prs) > 6:
                pr_tag += f"+{len(prs)-6}"
            new_summary = f"{repo}: Cursor AI work ({START_DATE} — {end}){pr_tag}"[:250]
            desc = repo_epic_description(repo, by_repo.get(repo, []), git_data.get(repo, []), repo_paths[repo])
            update_issue(key, {"summary": new_summary, "description": adf_description(desc)})
            print(f"  {key} epic {repo} ({len(prs)} PRs)", flush=True)
            updated += 1
            time.sleep(0.25)
            continue

        plain = issue_plain_title(summary)
        s = session_by_title.get(summary) or session_by_title.get(normalize_title(plain))
        if not s:
            for m in re.finditer(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|bc-[0-9a-f-]{36})\b", summary, re.I):
                s = session_by_id.get(m.group(1).lower())
                if s:
                    break
        if s:
            title = (s.get("title") or s.get("session_id") or "Untitled")[:240]
            if s.get("cloud_agent") and "cloud agent" not in title.lower():
                title = f"{title} (cloud agent)"[:240]
            prs = collect_session_prs(s)
            bug = is_bug(title, s.get("summary") or "")
            prefix = "Defect" if bug else "Cursor"
            new_summary = summary_with_prs(prefix, title, prs)
            update_issue(key, {"summary": new_summary, "description": adf_description(session_description(s))})
            print(f"  {key} session ({len(prs)} PRs): {title[:45]}", flush=True)
            updated += 1
            time.sleep(0.25)
            continue

        t = transcript_by_title.get(summary) or transcript_by_title.get(normalize_title(issue_plain_title(summary)))
        if not t and not summary.startswith("[Cursor]") and not summary.startswith("[Defect]"):
            for tk, tr in transcript_by_title.items():
                if isinstance(tk, str) and normalize_title(tr["title"]) in normalize_title(summary):
                    t = tr
                    break
        if t:
            prs = prs_from_text(t.get("title") or "")
            new_summary = summary_with_prs("Chat", chat_summary_title(t), prs)
            update_issue(key, {"summary": new_summary, "description": adf_description(transcript_description(t))})
            print(f"  {key} chat ({len(prs)} PRs): {t['title'][:45]}", flush=True)
            updated += 1
            time.sleep(0.25)

    print(f"Updated {updated} issues with PR data.", flush=True)



def close_issue(key):
    trans = jira_request("GET", f"/rest/api/3/issue/{key}/transitions")
    done = None
    for t in trans.get("transitions", []):
        name = (t.get("name") or "").lower()
        if name in ("done", "closed", "complete", "resolved"):
            done = t["id"]
            break
    if not done and trans.get("transitions"):
        done = trans["transitions"][-1]["id"]
    if done:
        jira_request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": done}})


def _extract_message_text(row):
    txt = ""
    for part in (row.get("message") or {}).get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            txt += part.get("text", "")
    txt = re.sub(r"<timestamp>.*?</timestamp>\s*", "", txt, flags=re.S)
    m = re.search(r"<user_query>\s*(.*?)(?:</user_query>|$)", txt, re.S)
    if m:
        return m.group(1).strip()
    return txt.strip()


def _parse_transcript_file(path):
    sid = os.path.basename(path).replace(".jsonl", "")
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    full_query = ""
    user_queries = []
    last_assistant = ""
    message_count = 0
    origin = "local IDE (subagent)" if os.path.sep + "subagents" + os.path.sep in path else "local IDE"
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row.get("role") not in ("user", "assistant"):
                    continue
                message_count += 1
                if row.get("role") == "user":
                    q = _extract_message_text(row)
                    if q:
                        user_queries.append(q)
                        if not full_query:
                            full_query = q
                elif row.get("role") == "assistant":
                    a = _extract_message_text(row)
                    if a:
                        last_assistant = a
    except Exception:
        full_query = sid
    title = truncate_summary(re.sub(r"\s+", " ", full_query.replace("\n", " "))) if full_query else sid
    return {
        "id": sid,
        "title": title or sid,
        "full_query": full_query or sid,
        "user_queries": user_queries,
        "last_assistant": last_assistant[:4000] if last_assistant else "",
        "mtime": mtime.isoformat(),
        "path": path,
        "message_count": message_count,
        "origin": origin,
        "_mtime_dt": mtime,
    }


def extract_transcripts():
    """Collect agent transcripts from all known workspace roots (deduped by id)."""
    out = {}
    cutoff = datetime.date(2025, 11, 29)
    for root in TRANSCRIPT_ROOTS:
        if not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            if os.path.sep + "subagents" + os.path.sep in path:
                continue
            parsed = _parse_transcript_file(path)
            if parsed["_mtime_dt"].date() < cutoff:
                continue
            prev = out.get(parsed["id"])
            if not prev or parsed["_mtime_dt"] > prev["_mtime_dt"]:
                out[parsed["id"]] = parsed
    cleaned = []
    for row in out.values():
        row = dict(row)
        row.pop("_mtime_dt", None)
        cleaned.append(row)
    cleaned.sort(key=lambda r: r.get("mtime") or "")
    return cleaned


def extract_git():
    data = {}
    for repo in REPOS:
        name = os.path.basename(repo)
        try:
            log = subprocess.check_output(
                ["git", "-C", repo, "log", f"--since={START_DATE}",
                 "--pretty=format:%H|%ai|%s"],
                text=True, errors="replace",
            )
            commits = []
            for line in log.splitlines():
                if "|" not in line:
                    continue
                h, d, s = line.split("|", 2)
                commits.append({"hash": h[:8], "date": d[:10], "subject": s})
            data[name] = commits
        except Exception as exc:
            data[name] = {"error": str(exc)}
    return data


def git_epic_section(name, commits):
    if isinstance(commits, dict) and "error" in commits:
        return f"Git log unavailable: {commits['error']}"
    if not commits:
        return "No git commits in range."
    lines = [f"Git commits since {START_DATE} ({len(commits)} total):", ""]
    lines += [f"  {c['date']} {c['hash']} {c['subject']}" for c in commits[:100]]
    if len(commits) > 100:
        lines.append(f"  ... and {len(commits) - 100} more")
    return "\n".join(lines)


def is_bug(title, summary=""):
    text = f"{title} {summary}".lower()
    return any(w in text for w in BUG_WORDS)


def _repo_from_name(name):
    name = (name or "").split("/")[-1]
    return name if name in REPO_NAMES else ""


def _git_repo_folder(item):
    """Git-correlation repo folder using dashboard pick logic."""
    gc = item.get("git_correlation") or {}
    if not gc.get("matched"):
        return ""
    hits = collections.Counter()
    for row in gc.get("repos") or []:
        folder = _repo_from_name(row.get("name") or "")
        if folder:
            hits[folder] += row.get("cost_usd") or 0
    if not hits:
        return ""
    ranked = hits.most_common()
    top_repo, top_cost = ranked[0]
    root = cd.WORKSPACE_ROOT_REPO
    if top_repo != root or len(ranked) == 1:
        return top_repo
    for repo, cost in ranked[1:]:
        if repo != root and cost >= top_cost * 0.2:
            return repo
    return top_repo


def session_repo_folder(item, field=""):
    """One repo per session/chat — uses dashboard assignment when present."""
    folder = _repo_from_name(item.get("repository") or "")
    if folder:
        return folder
    git_folder = _git_repo_folder(item)
    if git_folder:
        return git_folder
    title = field or item.get("full_query") or item.get("title") or ""
    return cd._infer_repo_folder_from_title(title) or ""


def infer_repo(item, field=""):
    """Alias kept for callers — delegates to session_repo_folder."""
    return session_repo_folder(item, field)


def delete_issue(key):
    hdrs = dict(_jira_auth())
    req = urllib.request.Request(
        JIRA_BASE + f"/rest/api/3/issue/{key}?deleteSubtasks=true",
        method="DELETE", headers=hdrs,
    )
    try:
        with urllib.request.urlopen(req, timeout=60):
            pass
    except urllib.error.HTTPError as exc:
        snippet = exc.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"Jira delete {exc.code} {key}: {snippet}") from exc


def bulk_delete(project, lo, hi):
    """Delete TIM-lo .. TIM-hi (highest first so children go before parents)."""
    deleted, failed = [], []
    for n in range(hi, lo - 1, -1):
        key = f"{project}-{n}"
        try:
            delete_issue(key)
            deleted.append(key)
            print(f"  deleted {key}", flush=True)
            time.sleep(0.15)
        except Exception as exc:
            failed.append((key, str(exc)))
            print(f"  FAILED {key}: {exc}", flush=True)
    print(f"Deleted {len(deleted)} issues, {len(failed)} failed.", flush=True)
    return deleted, failed


def epic_key_map():
    """Known v2 epic keys from prior backfill run."""
    return {
        "evernote_remarkable": "TIM-61",
        "fastcat": "TIM-62",
        "jrtca_results": "TIM-63",
        "horse_shows": "TIM-64",
        "3dprinting": "TIM-65",
        "dashboard": "TIM-66",
    }


def run_reparent(sessions, git_data, by_repo, extra_transcripts, end, epic_keys):
    """Move stories/bugs/chats to the correct repo epic; refresh epic bodies."""
    repo_paths = {os.path.basename(p): p for p in REPOS}
    issues = search_issues("project=TIM AND labels=cursor-backfill-v2 ORDER BY key ASC", 250)

    session_by_title = {}
    session_by_id = {}
    for s in sessions:
        title = (s.get("title") or s.get("session_id") or "Untitled").strip()
        if s.get("cloud_agent") and "cloud agent" not in title.lower():
            title = f"{title} (cloud agent)"
        for prefix in ("Cursor", "Defect"):
            session_by_title[f"[{prefix}] {title}"] = s
            session_by_title[normalize_title(title)] = s
        sid = (s.get("session_id") or "").lower()
        if sid:
            session_by_id[sid] = s

    transcript_by_title = {}
    for t in extra_transcripts:
        transcript_by_title[normalize_title(t["title"])] = t
        transcript_by_title[normalize_title(chat_summary_title(t))] = t
        transcript_by_title[normalize_title(t.get("full_query") or "")] = t

    moved = 0
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        itype = (issue["fields"]["issuetype"] or {}).get("name") or ""
        if itype == "Epic":
            continue

        repo = ""
        s = None
        plain = issue_plain_title(summary)
        s = session_by_title.get(summary) or session_by_title.get(normalize_title(plain))
        if not s:
            for m in re.finditer(
                r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|bc-[0-9a-f-]{36})\b",
                summary, re.I,
            ):
                s = session_by_id.get(m.group(1).lower())
                if s:
                    break
        if s:
            repo = session_repo_folder(s)
        if not s:
            t = transcript_by_title.get(normalize_title(plain))
            if not t:
                for tk, tr in transcript_by_title.items():
                    if tk and normalize_title(tr.get("title") or "") in normalize_title(plain):
                        t = tr
                        break
            if t:
                repo = session_repo_folder(t)

        target = epic_keys.get(repo) or epic_keys.get("_cross") or epic_keys.get("dashboard")
        if not target:
            continue
        update_issue(key, {"parent": {"key": target}})
        print(f"  {key} -> {target} ({repo or 'cross'})", flush=True)
        moved += 1
        time.sleep(0.2)

    for name, ekey in epic_keys.items():
        if name.startswith("_"):
            continue
        repo_sessions = by_repo.get(name, [])
        commits = git_data.get(name, [])
        prs = repo_prs(name, repo_sessions, repo_paths[name])
        pr_tag = f" PR:{','.join(pr_short(p) for p in prs[:6])}" if prs else ""
        if len(prs) > 6:
            pr_tag += f"+{len(prs)-6}"
        update_issue(ekey, {
            "summary": f"{name}: Cursor AI work ({START_DATE} — {end}){pr_tag}"[:250],
            "description": adf_description(repo_epic_description(
                name, repo_sessions, commits, repo_paths[name])),
        })
        print(f"  refreshed epic {ekey} ({name}, {len(repo_sessions)} sessions)", flush=True)
        time.sleep(0.25)

    print(f"Reparented/refreshed {moved} issues.", flush=True)


def issue_plain_title(summary):
    s = (summary or "").replace("[Cursor] ", "").replace("[Defect] ", "").replace("[Chat] ", "")
    if " PR:" in s:
        s = s.split(" PR:")[0]
    return s.strip()


def index_existing_issues(issues):
    """Map normalized titles and session/transcript IDs to existing Jira issues."""
    by_title = {}
    by_session_id = {}
    for issue in issues:
        summary = issue["fields"]["summary"]
        by_title[normalize_title(issue_plain_title(summary))] = issue
        # Session/transcript IDs sometimes appear in summaries after renames.
        for m in re.finditer(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|bc-[0-9a-f-]{36})\b", summary, re.I):
            by_session_id[m.group(1).lower()] = issue
    return by_title, by_session_id


def session_already_in_jira(s, by_title, by_session_id):
    sid = (s.get("session_id") or "").lower()
    if sid and sid in by_session_id:
        return True
    title = (s.get("title") or s.get("session_id") or "").strip()
    if normalize_title(title) in by_title:
        return True
    if s.get("cloud_agent"):
        return normalize_title(f"{title} (cloud agent)") in by_title
    return False


def transcript_already_in_jira(t, by_title, by_session_id):
    tid = (t.get("id") or "").lower()
    if tid and tid in by_session_id:
        return True
    return normalize_title(t.get("title") or "") in by_title


def run_sync(sessions, git_data, by_repo, extra_transcripts, end, epic_keys, close_new=True):
    """Create missing stories/bugs, refresh all v2 issue bodies, close new issues."""
    repo_paths = {os.path.basename(p): p for p in REPOS}
    issues = search_issues("project=TIM AND labels=cursor-backfill-v2 ORDER BY key ASC", 250)
    children = [i for i in issues if (i["fields"]["issuetype"] or {}).get("name") != "Epic"]
    by_title, by_session_id = index_existing_issues(children)

    created = []
    print(f"Existing v2 issues: {len(issues)} ({len(children)} stories/bugs)", flush=True)

    missing_sessions = [s for s in sessions if not session_already_in_jira(s, by_title, by_session_id)]
    missing_transcripts = [t for t in extra_transcripts if not transcript_already_in_jira(t, by_title, by_session_id)]
    print(f"Creating {len(missing_sessions)} missing session(s), {len(missing_transcripts)} missing chat(s)...", flush=True)

    for s in missing_sessions:
        repo = session_repo_folder(s) or "_cross"
        epic = epic_keys.get(repo) or epic_keys.get("_cross") or epic_keys.get("dashboard")
        if not epic:
            raise RuntimeError(f"No epic for repo {repo!r}")
        title = (s.get("title") or s.get("session_id") or "Untitled")[:240]
        if s.get("cloud_agent") and "cloud agent" not in title.lower():
            title = f"{title} (cloud agent)"[:240]
        bug = is_bug(title, s.get("summary") or "")
        itype = "Bug" if bug else "Story"
        prefix = "Defect" if bug else "Cursor"
        prs = collect_session_prs(s)
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": itype},
            "summary": summary_with_prs(prefix, title, prs),
            "description": adf_description(session_description(s)),
            "labels": ["cursor-backfill-v2", "cursor-session"] + (["defect"] if bug else []),
            "parent": {"key": epic},
        })
        created.append(key)
        by_title[normalize_title(title)] = {"key": key, "fields": {"summary": summary_with_prs(prefix, title, prs)}}
        sid = (s.get("session_id") or "").lower()
        if sid:
            by_session_id[sid] = by_title[normalize_title(title)]
        print(f"  + {key} {itype}: {title[:55]}", flush=True)
        time.sleep(0.25)

    for t in missing_transcripts:
        repo = session_repo_folder(t) or "dashboard"
        epic = epic_keys.get(repo) or epic_keys.get("dashboard")
        prs = prs_from_text(t.get("title") or "")
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": "Story"},
            "summary": summary_with_prs("Chat", chat_summary_title(t), prs),
            "description": adf_description(transcript_description(t)),
            "labels": ["cursor-backfill-v2", "agent-transcript"],
            "parent": {"key": epic},
        })
        created.append(key)
        by_title[normalize_title(t["title"])] = {"key": key, "fields": {"summary": summary_with_prs("Chat", chat_summary_title(t), prs)}}
        tid = (t.get("id") or "").lower()
        if tid:
            by_session_id[tid] = by_title[normalize_title(t["title"])]
        print(f"  + {key} Chat: {t['title'][:55]}", flush=True)
        time.sleep(0.25)

    print("Refreshing epic and issue descriptions...", flush=True)
    run_update_prs(sessions, git_data, by_repo, extra_transcripts, end)

    if close_new and created:
        print(f"Closing {len(created)} new issue(s)...", flush=True)
        for key in created:
            close_issue(key)
            print(f"  closed {key}", flush=True)
            time.sleep(0.2)

    print(f"Sync done — created {len(created)} issue(s).", flush=True)
    return created


def normalize_title(t):
    return re.sub(r"\s+", " ", (t or "").strip()).lower()[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-close", action="store_true", help="Leave issues open")
    ap.add_argument("--update-prs", action="store_true", help="Update existing v2 issues with PR data")
    ap.add_argument("--delete-range", default="", help="Delete issue numbers in range, e.g. 1-60")
    ap.add_argument("--reparent", action="store_true", help="Fix parent epic on v2 issues (no evernote rollup)")
    ap.add_argument("--sync", action="store_true", help="Add missing sessions/chats and refresh all v2 issues")
    args = ap.parse_args()
    end = datetime.date.today().isoformat()

    print("Extracting Cursor billing data...", flush=True)
    payload = cd.build_payload(START_DATE, end, force=False)
    sessions = payload.get("sessions") or []
    totals = payload.get("totals") or {}
    by_account = totals.get("by_account") or []
    transcripts = extract_transcripts()
    git_data = extract_git()

    # Group sessions by repo
    by_repo = {n: [] for n in REPO_NAMES}
    by_repo["_cross"] = []
    for s in sessions:
        repo = session_repo_folder(s, s.get("title") or "")
        (by_repo[repo] if repo else by_repo["_cross"]).append(s)

    # Transcripts not already covered by a billed session title
    session_titles = {normalize_title(s.get("title")) for s in sessions if s.get("title")}
    extra_transcripts = [t for t in transcripts
                         if normalize_title(t["title"]) not in session_titles]

    if args.delete_range:
        lo, hi = (int(x) for x in args.delete_range.split("-", 1))
        print(f"Deleting {PROJECT}-{lo} .. {PROJECT}-{hi}...", flush=True)
        bulk_delete(PROJECT, lo, hi)
        return

    epic_keys = epic_key_map()
    if by_repo["_cross"] and "_cross" not in epic_keys:
        # Create cross-repo epic if needed for reparent
        pass

    if args.reparent:
        if by_repo["_cross"]:
            print("Note: cross-repo sessions exist but no cross epic — using dashboard epic as fallback.", flush=True)
            epic_keys = dict(epic_keys)
        run_reparent(sessions, git_data, by_repo, extra_transcripts, end, epic_keys)
        return

    if args.update_prs:
        run_update_prs(sessions, git_data, by_repo, extra_transcripts, end)
        return

    if args.sync:
        if by_repo["_cross"]:
            print("Note: cross-repo sessions exist but no cross epic — using dashboard epic as fallback.", flush=True)
        run_sync(sessions, git_data, by_repo, extra_transcripts, end, epic_keys,
                 close_new=not args.no_close)
        return

    if args.dry_run:
        bugs = sum(1 for s in sessions if is_bug(s.get("title"), s.get("summary") or ""))
        print(f"DRY RUN:")
        print(f"  Epics: {len(REPO_NAMES)} repos + {'1 cross-repo' if by_repo['_cross'] else '0 cross-repo'}")
        print(f"  Stories: {len(sessions) - bugs} sessions + {len(extra_transcripts)} extra chats")
        print(f"  Bugs: {bugs}")
        print(f"  Subtasks: 0")
        for n in REPO_NAMES:
            print(f"    {n}: {len(by_repo[n])} sessions")
        if by_repo["_cross"]:
            print(f"    (cross-repo): {len(by_repo['_cross'])} sessions")
        return

    epics = {}
    created = []

    repo_paths = {os.path.basename(p): p for p in REPOS}
    print(f"Creating {len(REPO_NAMES)} repo epics...", flush=True)
    for name in REPO_NAMES:
        commits = git_data.get(name, [])
        repo_sessions = by_repo[name]
        prs = repo_prs(name, repo_sessions, repo_paths[name])
        pr_tag = f" PR:{','.join(pr_short(p) for p in prs[:6])}" if prs else ""
        if len(prs) > 6:
            pr_tag += f"+{len(prs)-6}"
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": "Epic"},
            "summary": f"{name}: Cursor AI work ({START_DATE} — {end}){pr_tag}"[:250],
            "description": adf_description(repo_epic_description(
                name, repo_sessions, commits, repo_paths[name])),
            "labels": ["cursor-backfill-v2", "repo", name],
        })
        epics[name] = key
        created.append(key)
        print(f"  Epic {key} — {name} ({len(repo_sessions)} sessions)", flush=True)
        time.sleep(0.3)

    if by_repo["_cross"]:
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": "Epic"},
            "summary": f"Cross-repo Cursor AI work ({START_DATE} — {end})",
            "description": adf_text(
                f"Cursor sessions spanning multiple repos or without a single repo.\n"
                f"Sessions: {len(by_repo['_cross'])}\n"
                f"Metered: ${sum(s.get('cost_usd') or 0 for s in by_repo['_cross']):.2f}"
            ),
            "labels": ["cursor-backfill-v2", "cross-repo"],
        })
        epics["_cross"] = key
        created.append(key)
        print(f"  Epic {key} — cross-repo ({len(by_repo['_cross'])} sessions)", flush=True)
        time.sleep(0.3)

    print(f"Creating session stories and bugs...", flush=True)
    for s in sessions:
        repo = session_repo_folder(s) or "_cross"
        epic = epics.get(repo) or epics.get("_cross") or epics.get("dashboard")
        if not epic:
            raise RuntimeError(f"No epic for repo {repo!r}")
        title = (s.get("title") or s.get("session_id") or "Untitled")[:240]
        if s.get("cloud_agent") and "cloud agent" not in title.lower():
            title = f"{title} (cloud agent)"[:240]
        bug = is_bug(title, s.get("summary") or "")
        itype = "Bug" if bug else "Story"
        prefix = "Defect" if bug else "Cursor"
        prs = collect_session_prs(s)
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": itype},
            "summary": summary_with_prs(prefix, title, prs),
            "description": adf_description(session_description(s)),
            "labels": ["cursor-backfill-v2", "cursor-session"] + (["defect"] if bug else []),
            "parent": {"key": epic},
        })
        created.append(key)
        print(f"  {key} {itype}: {title[:55]}", flush=True)
        time.sleep(0.25)

    print(f"Creating {len(extra_transcripts)} extra chat stories...", flush=True)
    for t in extra_transcripts:
        repo = session_repo_folder(t)
        for name in REPO_NAMES:
            if name in (t.get("title") or "").lower():
                repo = name
                break
        epic = epics.get(repo or "dashboard")
        prs = prs_from_text(t.get("title") or "")
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": "Story"},
            "summary": summary_with_prs("Chat", chat_summary_title(t), prs),
            "description": adf_description(transcript_description(t)),
            "labels": ["cursor-backfill-v2", "agent-transcript"],
            "parent": {"key": epic},
        })
        created.append(key)
        print(f"  {key}: {t['title'][:55]}", flush=True)
        time.sleep(0.25)

    if not args.no_close:
        print(f"Closing {len(created)} issues...", flush=True)
        for key in created:
            close_issue(key)
            print(f"  closed {key}", flush=True)
            time.sleep(0.2)

    print(f"\nDone — {len(epics)} epics, {len(created) - len(epics)} stories/bugs, 0 subtasks.")
    print(f"Board: {JIRA_BASE}/jira/software/projects/{PROJECT}/boards/1")


if __name__ == "__main__":
    main()
