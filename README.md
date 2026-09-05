# Cost dashboards

Two local localhost dashboards live in this repo:

- `github_copilot_dashboard.py` — GitHub Copilot CLI + VS Code Copilot chat (this README)
- `cursor_dashboard.py` — Cursor billed usage (same metering as cursor.com/dashboard), joined to local chat titles from `state.vscdb`

```powershell
py -3 cursor_dashboard.py
py -3 github_copilot_dashboard.py
```

# copilot-cost-dashboard

A refreshable local web dashboard showing **what each GitHub Copilot CLI chat actually cost**,
in USD and tokens, read straight from the Copilot CLI's own local session store.

Unlike the other entries in this repo, this is not a scheduled automation that publishes to
Confluence. It is a developer tool: you run it on your own machine, against your own chat
history, and it serves an HTML page on localhost until you stop it.

| | |
|---|---|
| Script | `github_copilot_dashboard.py` (Copilot) or `cursor_dashboard.py` (Cursor) |
| Runtime | Python 3.9+, **standard library only** - nothing to install |
| Data source | `~/.copilot/session-store.db`, opened **read-only**, plus VS Code Copilot chat transcripts (part measured, part estimated - see below) |
| Serves on | `http://127.0.0.1:8787` (configurable) |

## Quick start

Run it **from a clone of this repository**, so that a `git pull` is all it takes to pick up fixes:

```powershell
git clone https://github.com/WRBerkley/mec-it-github-automations.git
cd mec-it-github-automations
python automations\copilot-cost-dashboard\github_copilot_dashboard.py
```

If you already have a clone, update it first - the estimator and the VS Code parsing in particular
change often:

```powershell
git pull
python automations\copilot-cost-dashboard\github_copilot_dashboard.py
```

That opens your browser at `http://127.0.0.1:8787`. `Ctrl+C` stops it.

> **Please don't copy the script somewhere else and run it from there.** It is a single file with no
> dependencies, so copying it is tempting, but detached copies silently go stale and will report
> different numbers to everyone else - the VS Code cost estimation has already changed substantially
> more than once. Run it from your clone and pull.

On some Windows PowerShell terminals the console output needs an explicit encoding, otherwise
non-ASCII characters in chat titles will raise a `UnicodeEncodeError` on print:

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python automations\copilot-cost-dashboard\github_copilot_dashboard.py
```

## How the cost math works

The session store has an `assistant_usage_events` table that records a `total_nano_aiu` value
for every model request the CLI makes. AIU ("AI Units") is the internal metering unit, and:

```
1 USD == 1e11 nano-AIU
```

The same `total_nano_aiu` value is also what GitHub meters your license allowance against, just
expressed in a coarser unit - **AI credits**:

```
1 AI credit == 1e9 nano-AIU   (so 1 AI credit == $0.01)
```

Both conversions come off the one recorded number; the dashboard shows USD for cost analysis and
AI credits for allowance tracking.

That conversion was validated against Claude Opus 4.6's published rates - $5/M input,
$6.25/M cache-write, $0.50/M cache-read, $25/M output - recomputed from the recorded token
counts, and the result matched the recorded `total_nano_aiu` exactly.

Two caveats worth stating plainly:

- These are **list-price estimates of model consumption**, not a GitHub invoice. What you are
  actually billed depends on your plan, included premium requests, and any org-level agreement.
  Treat the numbers as a relative signal - which chats and which models are expensive - rather
  than as an amount owed.
- Only chats that exist in your **local** session store are counted, plus locally stored VS Code
  Copilot transcripts as an estimate. Sessions run elsewhere (another machine, the cloud agent,
  github.com) are not on this machine and so are invisible here.

### Days are local days

The session store records `created_at` in UTC. The dashboard converts it with
`date(created_at,'localtime')` before bucketing, so a "day" here is a day where you are, running
from local midnight to local midnight. Without this, evening work lands on tomorrow: at 19:45 CDT
the *Today* preset reported 6 requests instead of 985. It also keeps the two data sources on one
calendar, since VS Code transcript timestamps were already being read as local time.

## Features

- **Date range presets** - *All time*, *Today*, *Yesterday*, *This week*, *Last 7 days*, *MTD*,
  *Last month*, *Last 30 days*, *Last 90 days*, plus explicit From / To boxes. Every preset is
  anchored to **local midnight**, weeks start on Monday, and "Last N days" spans N days including
  today. Presets **re-anchor on every refresh**, so a dashboard left open overnight moves *Today*,
  *MTD* and friends onto the new day instead of silently reporting yesterday; dates you type by hand
  are never overwritten. VS Code chats often run across several days; they are clipped to the range
  **day by day**,
  so a chat straddling the boundary contributes only the part of itself that falls inside it.
- **KPI cards** - total cost, AI credits, chats, requests, input / output / cache-write /
  cache-read tokens, average cost per chat and per request. When an allowance is configured the
  cards also show the percentage consumed against it. That percentage can exceed 100%: the cards
  follow the date range and global chat filter, while the allowance is per billing cycle, so an
  all-time view spans more than one cycle.
- **License consumption - usage this cycle**, an allowance banner mirroring Copilot Settings >
  Usage. See below.
- **Daily spend** bar chart. Each column is stacked: measured CLI spend in blue, estimated VS Code
  spend in amber, with a legend and a hover breakdown of the two.
- **Cost by work item**, with *Jira tickets* / *Pull requests* / *Repositories* / *Sessions* tabs,
  plus a keyword search box that filters the active tab. Every tab reports AI credits alongside
  tokens and cost. Credits are exact, not estimated - they are the same recorded
  `total_nano_aiu` in a coarser unit, 1 AI credit == 1e9 nano-AIU == $0.01.
- **Cost by model**, so you can see what an expensive model is really costing you. This table also
  reports AI credits alongside tokens and cost.
- **Per-chat table**, sortable by any column. Clicking a row expands a per-turn cost breakdown
  for that chat.
- **VS Code Copilot chats**, folded into every table. Token counts are used where Copilot recorded
  them and estimated otherwise, so affected rows are flagged `est`. See below.
- **Global chat filter** and **date-range presets**. See below.
- **Manual refresh** button, plus a 5-minute auto-refresh that is on by default, with the time and
  age of the last successful load shown directly under the *auto 5m* box. While data is loading the
  page
  shows a progress bar, a spinner on the *Refresh* button and a "loading" placeholder, so a slow
  first VS Code scan is never mistaken for a frozen page.
- **Daily digest email** (opt-in) - on the first refresh of each day, emails a summary of the most
  recent day that had activity. See below.
- **Collapsible sections** - every section heading carries a caret button that collapses or
  expands it, and the choice persists in `localStorage` across refreshes.

### License consumption

Set an allowance and the dashboard renders a banner above *Daily spend* showing AI credits used
this cycle against that allowance, the percentage consumed, credits remaining and tokens used,
with a meter bar that turns amber at 80% and red at 100%. A footer line reports when the
allowance resets - the billing cycle is treated as the calendar month, deliberately independent
of the dashboard's own date-range filter.

> **Accuracy caveat - read this before trusting the number.** It counts only Copilot **CLI** chats
> recorded in your local session store. Usage from the IDE, from github.com, and from cloud agents
> never reaches that store, so the dashboard reads **lower** than Copilot Settings > Usage - in one
> measured cycle, 9,887 credits locally against 10,709 reported by Settings. Treat it as a **lower
> bound** and a trend indicator, not a replacement for the official figure. The estimated VS Code
> chats are deliberately **not** folded into this banner, even when the `+VS Code (est.)` checkbox
> is ticked - the banner reports measured credits against a real billing cycle.

The allowance is editable in the banner itself: the `/ 60,000` figure beside the credits used is
an input box. Type your own number, press Enter or click away, and the dashboard saves it to
`~/.copilot/cost-dashboard-state.json` and recomputes. It is remembered between runs, so you only
set it once - typically when your plan changes.

It defaults to **20,000** credits ($200). To preset it, pass `--ai-credits 60000` (or `60,000` /
`60_000`) or set `COPILOT_DASH_AI_CREDITS`; an explicit launch value overrides whatever was saved,
so drop the flag from your start command if you would rather manage it from the dashboard.

Set it to `0` and the banner stays visible but reports only measured usage - credits used, tokens,
requests - with `-` for *% used* and *credits remaining*, since those need an allowance to mean
anything. The box remains, so you can always type one back in. Blank input is ignored rather than
treated as zero, so a mistyped entry cannot silently wipe your allowance.

### VS Code Copilot chats (partly measured, partly estimated)

The dashboard also scans VS Code Copilot chat transcripts from
`%APPDATA%\Code\User\workspaceStorage\*\chatSessions\*`, `...\emptyWindowChatSessions\*` (and
`Code - Insiders`), so IDE usage is no longer invisible. Transcripts are stored either as a `.json`
snapshot or as a `.jsonl` **patch log**; the latter is replayed in full, so chats are read at their
final state rather than frozen at the first turn.

> **These figures are part measured, part estimated.** VS Code never records a cost, but newer
> Copilot Chat builds *do* record real token counts. The dashboard prefers what Copilot actually
> wrote - `result.usage`, `result.metadata`, a bare `completionTokens`, reasoning tokens from
> `toolCallRounds[].thinking`, and cached-token counts from conversation summaries - and estimates
> only what is missing. Where `copilotCredits` is present the charge is taken **exactly**, with no
> estimation at all. Everything else falls back to transcript character length (roughly 4 characters
> per token) over the *fully rendered* prompt (`renderedUserMessage`: editor context, terminal state
> and attached instruction files, not just what you typed), with re-sent history capped at the
> context window. Treat any estimated portion as an order-of-magnitude figure, not a bill.

**How much of it is real.** The section heading carries an `ESTIMATED Ã‚Â· N% REAL TOKENS` badge, so
the honesty check is the first thing you see rather than something buried in the notes. `N` is the
share of tokens in view that came from counts Copilot recorded, and its tooltip gives the raw
numbers. The *Likely range* note beneath adds the same share weighted by **spend**, which is the
more useful figure of the two: measured requests skew recent and larger, so on a typical history
32% of requests can account for only ~25% of the dollars. All of these move with the global filter.

Per-model rates are harvested from Copilot's own price list in the transcript
(`inputState.selectedModel.metadata`), which also supplies each model's premium-request multiplier.
Models with neither a harvested nor a published rate fall back to an assumed rate and are flagged as
such in the *VS Code* section.

**Two costing bases.** GitHub meters chat by *premium requests*, not by tokens, so the section shows
both models as a range and a `VS Code basis` selector in the top bar chooses which one drives every
table on the page:

| Basis | How it costs a VS Code chat |
| --- | --- |
| `tokens` (default) | Tokens x per-model rates - measured where recorded, inferred otherwise |
| `premium reqs` | Premium requests x each model's multiplier x the per-request price |

**When your data became measurable.** VS Code does not stamp its own version into chat transcripts,
so the dashboard reports the dates these fields appear in *your own* files rather than quoting
release notes that may not match your build. The *When your VS Code data became measurable* block
lists each recording field with its first/last-seen date and request count, plus a month-by-month
`% measured` bar. A user whose history predates those fields will see a low measured percentage and
a correspondingly rougher estimate; a user on a current build will see most requests measured.

**Merged into the main tables.** A `+VS Code (est.)` checkbox in the top bar - **checked by
default** - folds the VS Code chats into *every* table on the page: the totals cards,
*Daily spend*, *Cost by model*, *Cost by work item* (all four tabs) and the per-chat table. Untick
it for measured-CLI-only numbers.

- Any row or model containing estimated data is marked with an amber `est` badge, and the
  *Daily spend* chart shades the estimated portion of each day amber above the measured blue.
- An amber note under the totals cards states how many VS Code chats and how much spend has been
  folded in, which basis produced it, and what the measured-only figure is.
- The **Total cost** and **AI credits** cards show the combined figure large, with a smaller
  `GH Copilot` / `VS Code est.` breakout beneath.
- Because VS Code has no per-turn records, these chats **cannot be expanded** for a per-turn
  breakdown - the drill-down explains this - and their PR attribution falls back to whole-chat
  grouping rather than the turn-level segment attribution used for measured CLI chats.
- The *License consumption* banner **remains measured-only**, since it maps to a real billing
  cycle.

`--no-vscode` skips VS Code scanning entirely, in which case the checkbox hides itself.
`COPILOT_DASH_VSCODE_DIR` overrides where transcripts are looked for.

### Daily digest email (opt-in)

Pass `--email-to you@example.com` (or set `COPILOT_DASH_EMAIL_TO`) and the dashboard emails a daily
digest. **With no recipient configured the feature stays completely dormant**, so it changes nothing
for anyone who has not asked for it.

- **When** - on the first data refresh of each local day. The auto-refresh keeps the page live, so a
  dashboard left open overnight sends the digest by itself.
- **What day** - the *most recent day that had activity*, not simply yesterday. After a weekend or
  time off, Monday's digest still reports the last day actually worked instead of mailing a row of
  zeroes. Days with no activity are never mailed.
- **Contents** - spend, AI credits, requests, chats and tokens for that day; the Copilot CLI vs
  VS Code split; top models; top Jira tickets, pull requests and repositories; the most expensive
  chats; and a comparison against both the previous active day and a 7-active-day average.
- **Sent once** - the last-mailed day is recorded in `~/.copilot/cost-dashboard-state.json`. The
  guard is written before the message is attempted and the check is serialized, so several browser
  tabs refreshing at once cannot produce duplicate mail. That file is per-machine state and is never
  committed.
- **Never fatal** - the mail is sent on a background thread after the page payload is built, so an
  SMTP problem can never break or slow the dashboard. Failures are printed and surfaced in the flag.

A **flag in the top bar** shows the current state - green when a digest has gone out, amber while
sending, red on failure, with the recipient, relay and last-sent time in its tooltip. **Clicking the
flag switches the digest on or off**; the switch is remembered in the same state file, so it
survives a restart. Switching it back on part-way through a day re-arms that day's digest rather
than skipping it. A separate **Send now** button beside the flag mails the digest immediately,
which is the easiest way to test your relay.

Turning the digest off only stops the automatic mail - the dashboard itself is unaffected. To
disable the feature outright, drop `--email-to` (and clear `COPILOT_DASH_EMAIL_TO`) and restart;
the flag and button then disappear entirely.

`--send-digest` sends one digest and exits without starting the server, which is useful for testing
or for driving the digest from Task Scheduler / cron instead of from a browser refresh. It is an
explicit command, so it sends even when the flag is switched off:

```powershell
python github_copilot_dashboard.py --send-digest --email-to you@example.com
```

SMTP defaults to `smtp.wrberkley.com:25` (unauthenticated internal relay). Override with
`--smtp-host` / `--smtp-port`, and the sender with `--email-from` (defaults to
`copilot-dashboard@<your recipient's domain>`).

> The digest reports only what the dashboard can see: Copilot CLI on this machine and VS Code chat.
> Usage on github.com, cloud agents or another machine is not included.

### Global chat filter

The **Filter chats** box in the top bar is a *global* filter, not a table filter. The keyword is
sent to the server and the whole dashboard is recomputed against only the matching chats - *every*
section moves together: KPI cards, *Daily spend*, *Cost by model*, *Cost by work item*, the
per-chat table and the *VS Code Copilot chat* section, including its own model, day and coverage
breakdowns. A note beside the box reports the active keyword and how many chats matched; clearing
the box restores the full view. The *VS Code Copilot chat* section follows the **date range** in the
same way.

Chats match on id, title, summary, repository, workspace, branch and any Jira/repo/PR reference
found in them, so a keyword can be a project name just as easily as a ticket key.

A chat matches on its title, repository, branch, top model, session id, or any linked Jira
ticket, pull request or repository.

The *License consumption* banner is deliberately exempt - it reports the billing cycle, not the
filtered view, so it reads the same filtered or unfiltered.

### Work item search

The *Cost by work item* tab bar carries a keyword box that filters the active tab's rows on both
the key and the sub-line text (chat titles, or the repository on the *Sessions* tab). A summary
line under the table reports how many rows matched and what they cost in total. Unlike the
top-bar *Filter chats* box, this one only filters rows within this table - it does not change any
other section.

Both keyword boxes show a small **x** at the right-hand edge once they contain text; clicking it
empties the box and applies the change immediately, exactly as if the text had been deleted by
hand. The *Filter chats* **x** therefore triggers a server round-trip and restores the full
dashboard, while the work-item **x** only re-renders that table.

### Why *Cost by work item* can total less than the cards

A work item tab can only account for chats that carry that kind of reference, so it legitimately
sums to less than the KPI cards and *Cost by model*, which cover every chat in view. This is not a
miscalculation, and the table now says so rather than leaving you to spot the gap: a muted **No
Jira ticket / pull request / repository** row closes the difference, and the footer spells out how
much of the visible spend is attributed and how much has nothing to attribute it to.

The remainder has two causes. Most of it is chats that simply never mention a ticket, PR or repo.
On the *Pull requests* tab it also includes the stretches of a chat that happened before or between
PRs, because PRs use turn-level segment attribution rather than whole-chat attribution. The
*Sessions* tab always reconciles exactly, since every chat is itself a row.

VS Code chats pick up a repository when their workspace folder matches a repo the tool already
knows about. A multi-root `.code-workspace` spans several repos, so it is deliberately left
unattributed instead of being invented as a repo of its own.

### Reference linking

The dashboard regex-extracts Jira ticket keys, GitHub repository references and GitHub PR URLs
out of the text of each chat turn **and out of the name you gave the chat**, renders them as
clickable badges on the chat, and rolls the cost up per ticket / PR / repository. A false-positive
fix means documentation URLs such as
`docs.github.com/en/repositories` are no longer mistaken for repository references.

Chat names matter because a ticket is very often recorded by renaming the chat - say
`MEC-1234 - Adopt standard CI/CD workflows` - and never typed into the conversation at all. Names
live in the desktop app's own store (`data.db`, beside the session store) rather than in the CLI
session store, so the dashboard reads both. It is a **read-only, entirely optional** extra: point
`--app-db` elsewhere if your app data lives somewhere unusual, and if the file is missing or
unreadable the dashboard simply carries on with chat text alone.

Names are treated as **additional** text, not as an override, so a chat both keeps the references
it mentions and gains the ones in its name. The name is re-read on every refresh and forms part of
the extraction cache key, so renaming a chat - or adding a ticket to its name mid-flight - shows up
on the next refresh without restarting the server. The trade-off of the additive rule is that
discussing someone else's ticket in an unrelated chat still attributes that chat to the ticket.

Three different attribution models are in play, one per kind of work item, because the three
kinds of reference behave differently.

**Jira and Repositories - even split.** When one chat touches several tickets or repositories its
cost is **split evenly across them**, so the totals add up rather than double counting a chat that
mentioned three tickets. The second column counts *chats*.

**Pull requests - turn-level segment attribution.** An even split is a poor fit for PRs: a long
chat that opens three PRs near the end would give all three an identical slice of the entire
session, which is an artifact of the split rather than a real cost. Instead, the PR tab aligns
`turn_index` between the `turns` table and `assistant_usage_events`, so the cost of each
individual turn is known exactly. Every turn that mentions a PR is an *anchor*, and that PR's
segment is every turn since the **previous** anchor, up to and including its own anchor turn -
the subsection of the chat that actually produced it. Segments are disjoint by construction, so
no turn is ever counted twice. PRs that came out of the **same** segment are rolled into a single
line item carrying that segment's full cost - the work produced them jointly and cannot be
meaningfully divided - labeled `owner/repo#304, #305` when they share a repository, with each
number still an individual link. The second column counts *segments*, and each row carries a
turn-count badge.

> PR-attributed spend is deliberately a **subset** of the grand total. Turns after the final
> anchor in a chat, and chats that produced no PRs at all, are intentionally left unattributed
> rather than being forced onto some PR. The PR tab total is therefore expected to be lower than
> the headline cost - that is by design, not a gap in the data.

**Sessions - no splitting at all.** A chat maps to exactly one session, so each row carries that
chat's **exact** cost. Its second column counts *turns*, and because nothing is split, the tab
reconciles precisely to the grand total. Session rows are not hyperlinked - there is no external
URL for a local chat - so the sub-line shows the repository instead, when the session has one.

A PR that appears to have been *created* during the chat - matched on phrasing like `gh pr create`
or "created a pull request" - gets an extra `+ created` badge, which makes it easy to see which
chats produced work rather than just discussed it.

## Nothing is hardcoded

This tool contains no user-specific or organization-specific values, which is what makes it safe
to share:

- The **Jira base URL** is auto-detected from whatever `*.atlassian.net` links appear in the
  running user's own chat history.
- **Jira project keys** are auto-discovered from `atlassian.net/browse/<KEY>-<n>` links in that
  same history. A small deny-list stops common `ABC-123`-shaped false positives (`UTF-8`,
  `CVE-2024`, `SHA-256`, and similar) from being treated as tickets.
- If no Jira is detected at all, Jira badges simply render unlinked. Nothing breaks.
- The **database path** defaults to `~/.copilot/session-store.db`.
- The **AI credit allowance** defaults to **20,000** credits and is editable in the banner,
  persisting to the state file. `--ai-credits` presets it for people who would rather not click.

Every one of those can be overridden.

## CLI

```
python github_copilot_dashboard.py [--port 8787] [--db PATH] [--jira-base URL]
                                 [--jira-keys ABC,DEF] [--ai-credits 60000] [--no-vscode]
                                 [--no-open]
```

| Flag | Default | Purpose |
|---|---|---|
| `--port` | `8787` | Port to listen on, bound to `127.0.0.1` only |
| `--db` | `~/.copilot/session-store.db` | Path to the Copilot CLI session store |
| `--app-db` | `data.db` beside `--db` | Desktop app store, read only for the names you give chats. Optional; ignored if absent |
| `--jira-base` | auto-detected | Jira base URL, e.g. `https://yourorg.atlassian.net` |
| `--jira-keys` | auto-discovered | Extra comma-separated project keys, for tickets only ever mentioned in bare `ABC-123` form |
| `--ai-credits` | `20000` | Monthly AI credit allowance for the License consumption banner. Accepts `60000`, `60,000` or `60_000`. Also editable in the banner and remembered between runs; passing this overrides the saved value. `0` keeps the banner but blanks *% used* and *remaining* |
| `--no-vscode` | off | Skip scanning VS Code Copilot chat transcripts entirely; the `+VS Code (est.)` checkbox then hides itself |
| `--email-to` | unset | Email a daily digest of the most recent active day. Omitted = feature disabled. Env: `COPILOT_DASH_EMAIL_TO` |
| `--email-from` | `copilot-dashboard@<recipient domain>` | Digest sender address. Env: `COPILOT_DASH_EMAIL_FROM` |
| `--smtp-host` | `smtp.wrberkley.com` | SMTP relay for the digest. Env: `COPILOT_DASH_SMTP_HOST` |
| `--smtp-port` | `25` | SMTP port. Env: `COPILOT_DASH_SMTP_PORT` |
| `--send-digest` | off | Send one digest immediately and exit, for testing or scheduled tasks |
| `--no-open` | off | Do not launch a browser on startup |

### Environment variables

| Variable | Equivalent flag |
|---|---|
| `COPILOT_DASH_JIRA_BASE` | `--jira-base` |
| `COPILOT_DASH_JIRA_KEYS` | `--jira-keys` |
| `COPILOT_DASH_AI_CREDITS` | `--ai-credits` |
| `COPILOT_DASH_VSCODE_DIR` | overrides where VS Code Copilot chat transcripts are looked for |
| `COPILOT_DASH_EMAIL_TO` | `--email-to` |
| `COPILOT_DASH_EMAIL_FROM` | `--email-from` |
| `COPILOT_DASH_SMTP_HOST` | `--smtp-host` |
| `COPILOT_DASH_SMTP_PORT` | `--smtp-port` |

## Privacy

This matters, because the tool reads your chat history.

- The session store is opened **read-only**. The dashboard never writes to, migrates or locks
  the Copilot CLI's database.
- The server binds to **`127.0.0.1`** only. It is not reachable from the network.
- **No data is sent anywhere.** There are no outbound requests, no telemetry, no analytics, no
  external CSS or JavaScript - the page is self-contained HTML generated locally.
- Each user only ever sees **their own** local chat history. There is no shared backend and no
  way to point it at anyone else's sessions.
- Nothing is written to disk. Stopping the process leaves no artifacts behind.

Do not commit `session-store.db`, exported cost data, or any dashboard output into this or any
other repository - it contains the full text of your chats.
