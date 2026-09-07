#!/usr/bin/env python3
"""Publish markdown documentation to Confluence.

Reads ATLASSIAN_API_EMAIL and ATLASSIAN_API_TOKEN from the environment
(Cloud Agent secrets). Converts markdown files in docs/confluence/ to
Confluence storage format and creates or updates pages under a parent page.

Each published page includes a Confluence {toc} macro at the top.
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFLUENCE_BASE = "https://timberwilde.atlassian.net/wiki"
PARENT_TINY_LINK = "CgMB"  # https://timberwilde.atlassian.net/wiki/x/CgMB

DOCS = [
    ("fastcat.md", "FastCAT Results Database"),
    ("jrtca_results.md", "JRTCA Trial Results"),
    ("horse_shows.md", "Horse Show Rider Results"),
]

# Confluence storage-format TOC macro (auto-generates from page headings)
TOC_MACRO = (
    '<ac:structured-macro ac:name="toc" ac:schema-version="1" data-layout="default">'
    '<ac:parameter ac:name="maxLevel">3</ac:parameter>'
    '<ac:parameter ac:name="minLevel">1</ac:parameter>'
    '<ac:parameter ac:name="outline">true</ac:parameter>'
    '</ac:structured-macro>'
)


def credentials():
    email = (os.environ.get("ATLASSIAN_API_EMAIL")
             or os.environ.get("ATLASSIAN_EMAIL")
             or os.environ.get("CURSOR_DASH_JIRA_EMAIL") or "").strip()
    token = (os.environ.get("ATLASSIAN_API_TOKEN")
             or os.environ.get("ATLASSIAN_API_TOKEN")
             or os.environ.get("CURSOR_DASH_JIRA_TOKEN") or "").strip()
    if not email or not token:
        sys.exit(
            "Atlassian credentials not found. Set ATLASSIAN_API_EMAIL and "
            "ATLASSIAN_API_TOKEN as Cloud Agent secrets."
        )
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def api(method, path, body=None, params=None):
    url = CONFLUENCE_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=credentials())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        snippet = exc.read()[:800].decode("utf-8", "replace")
        raise RuntimeError(f"Confluence {exc.code} {path}: {snippet}") from exc


def resolve_tiny_link(tiny_id):
    """Resolve /wiki/x/{tiny_id} to a page ID via redirect."""
    url = f"{CONFLUENCE_BASE}/x/{tiny_id}"
    req = urllib.request.Request(url, method="GET", headers=credentials())
    req.add_header("Accept", "text/html")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        final_url = exc.headers.get("Location", "")
    # Extract page ID from URL like /wiki/spaces/KEY/pages/12345/Title
    m = re.search(r"/pages/(\d+)", final_url or "")
    if m:
        return m.group(1)
    # Fallback: search by tiny link metadata
    data = api("GET", f"/rest/api/content", params={"type": "page", "limit": 1})
    raise RuntimeError(f"Could not resolve tiny link {tiny_id}; last URL: {final_url}")


def get_page_by_title(title, space_key=None):
    cql = f'title="{title}" and type=page'
    if space_key:
        cql += f' and space="{space_key}"'
    data = api("GET", "/rest/api/content/search", params={"cql": cql, "limit": 5})
    results = data.get("results") or []
    return results[0] if results else None


def get_page(page_id):
    return api("GET", f"/rest/api/content/{page_id}", params={"expand": "body.storage,version,space"})


def strip_markdown_toc(md):
    """Remove the markdown Table of Contents section (Confluence uses its own macro)."""
    return re.sub(
        r"\n## Table of Contents\n.*?(?=\n---\n)",
        "\n",
        md,
        count=1,
        flags=re.DOTALL,
    )


def md_to_storage(md):
    """Convert markdown to Confluence storage format (simplified)."""
    try:
        import markdown
        html = markdown.markdown(
            md,
            extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
        )
    except ImportError:
        html = _simple_md_to_html(md)
    return TOC_MACRO + html


def _simple_md_to_html(md):
    """Fallback markdown converter when the markdown package is unavailable."""
    lines = md.splitlines()
    out = []
    in_code = False
    in_table = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line)
            continue
        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            row = "".join(f"<{tag}>{c}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        elif in_table:
            out.append("</tbody></table>")
            in_table = False
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.strip() == "---":
            out.append("<hr/>")
        elif line.strip() == "":
            out.append("")
        elif line.startswith("- "):
            out.append(f"<ul><li>{_inline(line[2:])}</li></ul>")
        elif re.match(r"^\d+\. ", line):
            out.append(f"<ol><li>{_inline(re.sub(r'^\d+\.\s*', '', line))}</li></ol>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if in_table:
        out.append("</tbody></table>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )
    return text


def create_page(space_key, parent_id, title, storage_html):
    body = {
        "type": "page",
        "title": title,
        "ancestors": [{"id": parent_id}],
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": storage_html,
                "representation": "storage",
            }
        },
    }
    return api("POST", "/rest/api/content", body)


def update_page(page_id, title, version, storage_html):
    body = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": version + 1},
        "body": {
            "storage": {
                "value": storage_html,
                "representation": "storage",
            }
        },
    }
    return api("PUT", f"/rest/api/content/{page_id}", body)


def publish_file(docs_dir, filename, title, parent_id, space_key, dry_run=False):
    path = os.path.join(docs_dir, filename)
    with open(path, encoding="utf-8") as fh:
        md = fh.read()
    md = strip_markdown_toc(md)
    storage = md_to_storage(md)

    existing = get_page_by_title(title, space_key)
    if dry_run:
        action = "update" if existing else "create"
        print(f"  [dry-run] Would {action} page '{title}' ({len(storage)} bytes storage HTML)")
        return existing or {"id": "(new)"}

    if existing:
        page_id = existing["id"]
        current = get_page(page_id)
        version = current["version"]["number"]
        result = update_page(page_id, title, version, storage)
        print(f"  Updated page '{title}' → {CONFLUENCE_BASE}{result['_links']['webui']}")
    else:
        result = create_page(space_key, parent_id, title, storage)
        print(f"  Created page '{title}' → {CONFLUENCE_BASE}{result['_links']['webui']}")
    return result


def main():
    ap = argparse.ArgumentParser(description="Publish Confluence documentation pages")
    ap.add_argument("--docs-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "confluence"))
    ap.add_argument("--parent-tiny", default=PARENT_TINY_LINK,
                    help="Tiny link ID of parent page (default: CgMB)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("Resolving parent page...", flush=True)
    parent_id = resolve_tiny_link(args.parent_tiny)
    parent = get_page(parent_id)
    space_key = parent["space"]["key"]
    print(f"  Parent: {parent['title']} (id={parent_id}, space={space_key})", flush=True)

    print(f"Publishing {len(DOCS)} pages...", flush=True)
    for filename, title in DOCS:
        publish_file(args.docs_dir, filename, title, parent_id, space_key, args.dry_run)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
