"""Create Jira epics/stories/bugs for Cursor work since 2025-11-29, then close them.

Hierarchy (TIM team-managed project):
  Epic  — one per repository (+ optional cross-repo epic)
  Story — Cursor billed sessions and agent chats (primary work units)
  Bug   — defect / fix / error sessions
  (no subtasks — git commit log lives in the epic description)
"""

import argparse
import base64
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
TRANSCRIPT_ROOT = os.path.join(
    os.environ.get("USERPROFILE", ""),
    ".cursor", "projects",
    "c-Users-mw-OneDrive-timberwilde-net-repos-evernote-remarkable",
    "agent-transcripts",
)
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


def adf_description(text):
    """Plain text description with clickable PR URLs."""
    text = (text or "").strip() or "(none)"
    content = []
    for para in text.split("\n"):
        if not para.strip():
            continue
        parts = []
        pos = 0
        for m in re.finditer(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", para):
            if m.start() > pos:
                parts.append({"type": "text", "text": para[pos:m.start()]})
            href = m.group(0)
            parts.append({"type": "text", "text": href,
                          "marks": [{"type": "link", "attrs": {"href": href}}]})
            pos = m.end()
        if pos < len(para):
            parts.append({"type": "text", "text": para[pos:]})
        if not parts:
            parts = [{"type": "text", "text": para}]
        content.append({"type": "paragraph", "content": parts})
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": text[:32000]}]}]
    return {"type": "doc", "version": 1, "content": content}


def session_description(s):
    lines = [
        f"Cursor session: {s.get('session_id', '')}",
        f"Account: {s.get('account_label') or s.get('account') or ''}",
        f"Dates: {s.get('first_day')} — {s.get('last_day')}",
        f"Cost USD: ${s.get('cost_usd', 0):.4f}",
        f"On-demand USD: ${s.get('on_demand_usd', 0):.4f}",
        f"Tokens: {s.get('total_tokens', 0):,} (in {s.get('input_tokens', 0):,} / out {s.get('output_tokens', 0):,})",
        f"Requests: {s.get('requests', 0)}",
        f"Top model: {s.get('top_model') or ''}",
        f"Repository: {s.get('repository') or ''}",
        f"Branch: {s.get('branch') or ''}",
        f"Workspace: {s.get('workspace') or ''}",
    ]
    refs = s.get("refs") or {}
    if refs.get("jira"):
        lines.append(f"Jira refs: {', '.join(refs['jira'])}")
    prs = collect_session_prs(s)
    lines.extend(format_pr_lines(prs))
    if refs.get("repos"):
        lines.append("Repos: " + ", ".join(r.get("name", str(r)) for r in refs["repos"]))
    gc = s.get("git_correlation") or {}
    if gc.get("matched"):
        lines.append(
            f"\nGit correlation: ${gc.get('matched_cost_usd', 0):.2f} matched, "
            f"${gc.get('unmatched_cost_usd', 0):.2f} unmatched (±{gc.get('window_hours', 0)}h window)"
        )
    if s.get("summary"):
        lines.append(f"\nSummary:\n{s['summary']}")
    return "\n".join(lines)


def transcript_description(t):
    prs = prs_from_text(t.get("title") or "")
    lines = [
        "Agent transcript (no separate billed session).",
        f"ID: {t['id']}",
        f"Modified: {t['mtime']}",
    ]
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
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                           "content": [{"type": "text", "text": text[:32000]}]}]}


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
    issues = search_issues("project=TIM AND labels=cursor-backfill-v2 ORDER BY key ASC", 120)
    print(f"Found {len(issues)} v2 issues to update", flush=True)

    repo_paths = {os.path.basename(p): p for p in REPOS}
    epic_summaries = {f"{n}: Cursor AI work ({START_DATE}": n for n in REPO_NAMES}

    session_by_title = {}
    for s in sessions:
        title = (s.get("title") or s.get("session_id") or "Untitled").strip()
        for prefix in ("Cursor", "Defect"):
            session_by_title[f"[{prefix}] {title}"] = s
            session_by_title[normalize_title(f"[{prefix}] {title}")] = s
        session_by_title[normalize_title(title)] = s

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

        s = session_by_title.get(summary) or session_by_title.get(normalize_title(summary.replace("[Cursor] ", "").replace("[Defect] ", "")))
        if s:
            title = (s.get("title") or s.get("session_id") or "Untitled")[:240]
            prs = collect_session_prs(s)
            bug = is_bug(title, s.get("summary") or "")
            prefix = "Defect" if bug else "Cursor"
            new_summary = summary_with_prs(prefix, title, prs)
            update_issue(key, {"summary": new_summary, "description": adf_description(session_description(s))})
            print(f"  {key} session ({len(prs)} PRs): {title[:45]}", flush=True)
            updated += 1
            time.sleep(0.25)
            continue

        t = transcript_by_title.get(summary)
        if not t:
            for tk, tr in transcript_by_title.items():
                if isinstance(tk, str) and normalize_title(tr["title"]) in normalize_title(summary):
                    t = tr
                    break
        if t:
            prs = prs_from_text(t.get("title") or "")
            new_summary = summary_with_prs("Chat", t["title"][:200], prs)
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


def extract_transcripts():
    out = []
    if not os.path.isdir(TRANSCRIPT_ROOT):
        return out
    cutoff = datetime.date(2025, 11, 29)
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        if os.path.sep + "subagents" + os.path.sep in path:
            continue
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        if mtime.date() < cutoff:
            continue
        sid = os.path.basename(path).replace(".jsonl", "")
        title = ""
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    row = json.loads(line)
                    if row.get("role") != "user":
                        continue
                    txt = ""
                    for part in (row.get("message") or {}).get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "text":
                            txt += part.get("text", "")
                    m = re.search(r"<user_query>\s*(.*?)(?:</user_query>|$)", txt, re.S)
                    if m:
                        title = re.sub(r"\s+", " ", m.group(1).strip())[:200]
                        break
                    if txt and not title:
                        title = re.sub(r"\s+", " ", txt.strip())[:200]
        except Exception:
            title = sid
        out.append({"id": sid, "title": title or sid, "mtime": mtime.isoformat()})
    return out


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


REPO_ALIASES = {
    "fastcat": ["fastcat", "fast cat", "fast-path", "fast path", "relationship schema"],
    "jrtca_results": ["jrtca", "trial results", "jrtca_results", "grid-keys", "grid keys", "normalization"],
    "horse_shows": ["horse_shows", "horse shows", "horseshows", "scrape class", "show results",
                    "non-placing", "discovery re-walk", "chrome process", "selenium"],
    "3dprinting": ["3dprinting", "3d printing", "3d print", "bambu", "rv pantry", "interlocking shelf"],
    "dashboard": ["dashboard", "cursor_dashboard", "cost dashboard", "copilot", "billed usage",
                  "chunk budget", "atlassian api", "jira ticket", "jira backfill"],
}
# Title keywords for evernote_remarkable project work (never matched from workspace path).
EVERNOTE_TITLE_ALIASES = [
    "evernote_remarkable", "evernote remarkable", "evernote-to-remarkable", "evernote to remarkable",
    "weekly data download", "download agent", "tv dump", "csrf token", "powershell intermediary",
    "cut over to backlog", "remarkable sync", "evernote sync",
]
WORKSPACE_ROOT_REPO = "evernote_remarkable"


def _evernote_from_title(title):
    blob = (title or "").lower()
    if any(a in blob for a in EVERNOTE_TITLE_ALIASES):
        return True
    return "evernote" in blob and "remarkable" in blob


def _is_workspace_primary(ref):
    return (ref.get("role") == "primary"
            and _repo_from_name(ref.get("name") or "") == WORKSPACE_ROOT_REPO)


def _repos_from_refs(item):
    """Repo refs excluding the multi-root workspace folder marker."""
    out = []
    for r in (item.get("refs") or {}).get("repos") or []:
        if _is_workspace_primary(r):
            continue
        rn = _repo_from_name(r.get("name") or "")
        if rn and rn not in out:
            out.append(rn)
    return out


def _repo_from_name(name):
    name = (name or "").split("/")[-1]
    return name if name in REPO_NAMES else ""


def _repos_from_prs(item):
    repos = []
    seen = set()
    if item.get("session_id") is not None or item.get("cost_usd") is not None:
        for p in collect_session_prs(item):
            rn = _repo_from_name(p.get("repo") or (p.get("key") or "").split("#")[0])
            if rn and rn not in seen:
                seen.add(rn)
                repos.append(rn)
    for p in (item.get("refs") or {}).get("prs") or []:
        if p.get("role") == "skipped":
            continue
        rn = _repo_from_name(p.get("repo") or (p.get("key") or "").split("#")[0])
        if rn and rn not in seen:
            seen.add(rn)
            repos.append(rn)
    for p in prs_from_text(item.get("title") or ""):
        rn = _repo_from_name(p.get("repo") or (p.get("key") or "").split("#")[0])
        if rn and rn not in seen:
            seen.add(rn)
            repos.append(rn)
    return repos


def _repos_from_git_correlation(item):
    gc = item.get("git_correlation") or {}
    rows = sorted(gc.get("repos") or [], key=lambda r: -(r.get("cost_usd") or 0))
    out = []
    for row in rows:
        rn = _repo_from_name(row.get("name") or "")
        if rn:
            out.append(rn)
    return out


def _title_repo(title, workspace=""):
    """Match repo from chat title; workspace path never assigns evernote_remarkable."""
    blob = (title or "").lower()
    for name, aliases in REPO_ALIASES.items():
        if any(a in blob for a in aliases):
            return name
    if _evernote_from_title(title):
        return WORKSPACE_ROOT_REPO
    wb = (workspace or "").lower()
    for name, aliases in REPO_ALIASES.items():
        if any(a in wb for a in aliases):
            return name
    for name in REPO_NAMES:
        if name != WORKSPACE_ROOT_REPO and name in wb:
            return name
    return ""


def infer_repo(item, field=""):
    """Assign repo from chat/PR/git signals — never the workspace root folder alone."""
    title = field or item.get("title") or ""
    workspace = item.get("workspace") or ""

    hit = _title_repo(title, workspace)
    if hit:
        return hit

    pr_repos = _repos_from_prs(item)
    git_repos = _repos_from_git_correlation(item)
    ref_repos = _repos_from_refs(item)

    if len(pr_repos) == 1:
        return pr_repos[0]
    if len(git_repos) == 1:
        return git_repos[0]
    if len(ref_repos) == 1:
        return ref_repos[0]

    # Strong evernote git attribution (not just workspace proximity).
    gc = item.get("git_correlation") or {}
    rows = sorted(gc.get("repos") or [], key=lambda r: -(r.get("cost_usd") or 0))
    if rows:
        top = _repo_from_name(rows[0].get("name") or "")
        top_cost = rows[0].get("cost_usd") or 0
        second_cost = (rows[1].get("cost_usd") or 0) if len(rows) > 1 else 0
        if top == WORKSPACE_ROOT_REPO and top_cost > 0.01 and top_cost >= second_cost:
            return WORKSPACE_ROOT_REPO

    non_en = [r for r in pr_repos if r != WORKSPACE_ROOT_REPO]
    if non_en:
        return non_en[0]
    if pr_repos:
        return pr_repos[0]

    non_en = [r for r in git_repos if r != WORKSPACE_ROOT_REPO]
    if non_en:
        return non_en[0]
    if git_repos:
        return git_repos[0]

    if ref_repos:
        return ref_repos[0]

    return ""


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
    issues = search_issues("project=TIM AND labels=cursor-backfill-v2 ORDER BY key ASC", 120)

    session_by_title = {}
    for s in sessions:
        title = (s.get("title") or s.get("session_id") or "Untitled").strip()
        for prefix in ("Cursor", "Defect"):
            session_by_title[f"[{prefix}] {title}"] = s
            session_by_title[normalize_title(title)] = s

    transcript_by_title = {normalize_title(t["title"]): t for t in extra_transcripts}

    moved = 0
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        itype = (issue["fields"]["issuetype"] or {}).get("name") or ""
        if itype == "Epic":
            continue

        repo = ""
        s = None
        for cand in (summary, summary.split(" PR:")[0]):
            plain = cand.replace("[Cursor] ", "").replace("[Defect] ", "").replace("[Chat] ", "")
            s = session_by_title.get(cand) or session_by_title.get(normalize_title(plain))
            if s:
                repo = infer_repo(s, s.get("title") or "")
                break
        if not s:
            plain = summary.replace("[Chat] ", "").split(" PR:")[0]
            t = transcript_by_title.get(normalize_title(plain))
            if t:
                repo = infer_repo(t, t.get("title") or "")

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


def normalize_title(t):
    return re.sub(r"\s+", " ", (t or "").strip()).lower()[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-close", action="store_true", help="Leave issues open")
    ap.add_argument("--update-prs", action="store_true", help="Update existing v2 issues with PR data")
    ap.add_argument("--delete-range", default="", help="Delete issue numbers in range, e.g. 1-60")
    ap.add_argument("--reparent", action="store_true", help="Fix parent epic on v2 issues (no evernote rollup)")
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
        repo = infer_repo(s, s.get("title") or "")
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
        repo = infer_repo(s, s.get("title") or "") or "_cross"
        epic = epics.get(repo) or epics.get("_cross") or epics.get("dashboard")
        if not epic:
            raise RuntimeError(f"No epic for repo {repo!r}")
        title = (s.get("title") or s.get("session_id") or "Untitled")[:240]
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
        repo = infer_repo(t, t.get("title") or "")
        for name in REPO_NAMES:
            if name in (t.get("title") or "").lower():
                repo = name
                break
        epic = epics.get(repo or "dashboard")
        prs = prs_from_text(t.get("title") or "")
        key = create_issue({
            "project": {"key": PROJECT},
            "issuetype": {"name": "Story"},
            "summary": summary_with_prs("Chat", t["title"][:200], prs),
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
