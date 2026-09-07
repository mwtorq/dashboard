# Horse Show Rider Results

A Python/Selenium data pipeline that collects hunter/jumper and related horse show results from HorseShowsOnline.com, stores them in SQL Server, optionally enriches records from USEF.org, and feeds a Power BI analytics report.

| | |
|---|---|
| **Repository** | [github.com/mwtorq/horse_shows](https://github.com/mwtorq/horse_shows) |
| **Database** | `HorseShows` on `LDAHSAR\SQLEXPRESS` |
| **Schema** | `sResults` |
| **Authentication** | Windows Authentication |
| **Automation** | Windows Scheduled Tasks under `\ResultsAutomation\` |
| **Shared ops** | `C:\Users\mw\ResultsAutomation\` (logs, locks, `Common.ps1`) |
| **Reporting** | `PowerBI/HorseShows.pbip` |

## Table of Contents

- [Purpose and Overview](#purpose-and-overview)
- [Database Schema](#database-schema)
  - [Entity Relationship](#entity-relationship)
  - [Table: sResults.ShowList](#table-sresultsshowlist)
  - [Table: sResults.ShowClass](#table-sresultsshowclass)
  - [Table: sResults.ShowResults](#table-sresultsshowresults)
  - [Table: sResults.Competitors](#table-sresultscompetitors)
  - [Table: sResults.Horse](#table-sresultshorse)
  - [Table: sResults.ImportLog](#table-sresultsimportlog)
  - [View: sResults.vwResults](#view-sresultsvwresults)
- [Python Scrapers](#python-scrapers)
  - [Core Pipeline](#core-pipeline)
  - [Archive Scrapers (2014 Backfill)](#archive-scrapers-2014-backfill)
  - [Maintenance Scripts](#maintenance-scripts)
- [Automation](#automation)
  - [Run-HorseShows.ps1 — Weekly Collection](#run-horseshowsps1--weekly-collection)
  - [Run-HorseShowsNonPlacingBacklog.ps1 — Backlog Burn-Down](#run-horseshowsnonplacingbacklogps1--backlog-burn-down)
  - [Run-HorseShowsCatchup.ps1 — Year Backfill](#run-horseshowscatchupps1--year-backfill)
  - [NonPlacingQueue.ps1 — Shared Queue Logic](#nonplacingqueueps1--shared-queue-logic)
- [Power BI](#power-bi)
  - [Project Files](#project-files)
  - [Data Model (7 tables, 6+ relationships)](#data-model-7-tables-6-relationships)
  - [Report Pages (6 pages, 20 visuals)](#report-pages-6-pages-20-visuals)
- [Data Flow](#data-flow)
  - [End-to-End Pipeline](#end-to-end-pipeline)
  - [Operational Timing (Measured Rates)](#operational-timing-measured-rates)
- [External Data Sources](#external-data-sources)
  - [HorseShowsOnline.com](#horseshowsonlinecom)
  - [USEF.org](#useforg)
  - [Internet Archive (2014 only)](#internet-archive-2014-only)
- [Directory Structure](#directory-structure)

---

## Purpose and Overview

The horse_shows repository builds a queryable history of show, class, and entry-level results (placing and non-placing) from [HorseShowsOnline.com](https://horseshowsonline.com), with optional enrichment from [USEF.org](https://www.usef.org).

| Aspect | Detail |
|--------|--------|
| **Primary goal** | Queryable history of show, class, and entry-level results |
| **Default year range** | 2015–2025 via live site; 2014 via Internet Archive backfill |
| **Tech stack** | Python 3, Selenium/Chrome, pyodbc, PowerShell, Power BI |

The pipeline is split into stages (discovery → placing results → non-placing entries → USEF enrichment) because each stage has different volume, runtime, and scheduling constraints.

---

## Database Schema

### Entity Relationship

```
ShowList ──< ShowClass ──< ShowResults
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                  Horse   Competitors  (Rider/Trainer/Owner roles)
```

### Table: `sResults.ShowList`

Master list of shows discovered from HorseShowsOnline.

| Column | Type | Description |
|--------|------|-------------|
| `ID` | INT IDENTITY PK | Surrogate key; queue ordering for non-placing sweeps |
| `Year` | INT NOT NULL | Show year |
| `ShowName` | NVARCHAR(500) NOT NULL | Show title |
| `StartDate` | NVARCHAR(50) | Parsed start date |
| `EndDate` | NVARCHAR(50) | Parsed end date |
| `ShowDate` | NVARCHAR(100) | Raw date range from grid |
| `ShowLocation` | NVARCHAR(500) | Location + state |
| `StateProv` | NVARCHAR(50) | State/province |
| `GoverningBody` | NVARCHAR(200) | e.g. USEF |
| `ShowGUID` | NVARCHAR(100) | HorseShowsOnline unique identifier |
| `CreatedDate` | DATETIME | Default `GETDATE()` |
| `UpdatedDate` | DATETIME | Default `GETDATE()` |

### Table: `sResults.ShowClass`

One row per class within a show.

| Column | Type | Description |
|--------|------|-------------|
| `ID` | INT IDENTITY PK | Class surrogate key |
| `ShowListID` | INT NOT NULL FK | Parent show |
| `Class` | NVARCHAR(200) | Class number/code |
| `ClassName` | NVARCHAR(500) | Class description |
| `ClassType` | NVARCHAR(200) | e.g. Hunter, Jumper |
| `DivisionName` | NVARCHAR(200) | Division |
| `Entries` | INT | Total entries |
| `Placings` | INT | Published placing count |
| `NonPlacingComplete` | BIT DEFAULT 0 | Non-placing scrape finished |
| `CreatedDate` | DATETIME | |
| `UpdatedDate` | DATETIME | |

### Table: `sResults.ShowResults`

One row per entry (placing or non-placing).

| Column | Type | Description |
|--------|------|-------------|
| `ID` | INT IDENTITY PK | Result surrogate key |
| `ShowClassID` | INT NOT NULL FK | Parent class |
| `Place` | INT | 1+ = placing; **0 = non-placing (DNP)** |
| `Entry` | NVARCHAR(200) | Entry number |
| `HorseID` | INT FK | |
| `Country` | NVARCHAR(50) | Rider country |
| `Prize` | NVARCHAR(100) | Prize money |
| `AddBack` | NVARCHAR(100) | Add-back amount |
| `Start` | NVARCHAR(100) | Start order |
| `Score` | NVARCHAR(100) | Score |
| `Percent` | NVARCHAR(100) | Percentage |
| `USEF` | NVARCHAR(100) | USEF points from show page |
| `EC` | NVARCHAR(100) | EC points |
| `RiderID` | INT FK → Competitors | |
| `TrainerID` | INT FK → Competitors | |
| `CreatedDate` | DATETIME | |
| `UpdatedDate` | DATETIME | |

**Uniqueness:** Effectively `(ShowClassID, Entry)` — duplicates skipped on insert.

### Table: `sResults.Competitors`

Stores riders, owners, and trainers in one table.

| Column | Type | Description |
|--------|------|-------------|
| `ID` | INT IDENTITY PK | |
| `Rider` | NVARCHAR(500) | Format: `LastName, FirstName` |
| `RiderUSEFID` | NVARCHAR(50) | Populated by `scrape_usef_person.py` |
| `RiderState` | NVARCHAR(10) | USEF state |
| `RiderUSEFStatus` | NVARCHAR(500) | USEF membership status |
| `Owner` | NVARCHAR(500) | Owner name |
| `Trainer` | NVARCHAR(500) | Trainer name |
| `CreatedDate` | DATETIME | |
| `UpdatedDate` | DATETIME | |

### Table: `sResults.Horse`

| Column | Type | Description |
|--------|------|-------------|
| `ID` | INT IDENTITY PK | |
| `HorseName` | NVARCHAR(500) NOT NULL UNIQUE | |
| `Sire` | NVARCHAR(500) | Added by USEF scraper |
| `Dam` | NVARCHAR(500) | |
| `DOB` | NVARCHAR(50) | Foal date |
| `Sex` | NVARCHAR(50) | |
| `Color` | NVARCHAR(50) | |
| `Breed` | NVARCHAR(200) | |
| `USEFID` | NVARCHAR(50) | Populated by `scrape_usef_horse.py` |
| `OwnerID` | INT FK → Competitors | |
| `CreatedDate` | DATETIME | |
| `UpdatedDate` | DATETIME | |

### Table: `sResults.ImportLog`

Audit trail for all scraper activity.

| Column | Type | Description |
|--------|------|-------------|
| `ID` | INT IDENTITY PK | |
| `LogTimestamp` | DATETIME | Default `GETDATE()` |
| `OriginatingScript` | NVARCHAR(200) | e.g. `scrape_class_results.py` |
| `TargetTable` | NVARCHAR(200) | e.g. `ShowResults` |
| `Action` | NVARCHAR(100) | `START`, `COMPLETE`, `ERROR` |
| `RowCount` | INT | Rows affected |
| `ErrorDetail` | NVARCHAR(MAX) | Error message |
| `AdditionalInfo` | NVARCHAR(MAX) | Context (ShowGUID, class, etc.) |

### View: `sResults.vwResults`

Denormalized reporting view joining all core tables:

- **LEFT JOIN** chain from `ShowList` outward
- **Place formatting:** `0` → `DNP`; champion classes map 1/2 to `Champion`/`Reserve`
- **Place display:** Appends `out of {Entries}` (e.g. `3 out of 12`)
- **Rider display:** Appends `(RiderUSEFID)` when present

---

## Python Scrapers

### Core Pipeline

#### `scrape_shows_by_year.py` — Show Discovery

| Item | Detail |
|------|--------|
| **Source** | `https://horseshowsonline.com/ShowSelector.aspx` → "Shows By Year" tab |
| **Target** | `sResults.ShowList` |
| **Captures** | Show date, name, location, state, governing body, `ShowGUID` |
| **Default years** | 2025 down to 2015 |
| **CLI** | `python scrape_shows_by_year.py [YEARS]` — single year, range, comma list |

#### `scrape_class_results.py` — Placing Class Results

| Item | Detail |
|------|--------|
| **Source** | `https://horseshowsonline.com/ClassResults?ShowGUID={guid}` |
| **Target** | `ShowClass`, `ShowResults`, `Competitors`, `Horse`, `ImportLog` |
| **Captures** | Class summary + expanded placing entry rows |
| **Selection** | Completed shows only (`EndDate < today`); skips shows with existing data |

**Key CLI flags:**

| Flag | Purpose |
|------|---------|
| `--direct-url` / `-d` | Navigate directly to ClassResults URL (used by automation) |
| `--load-missing` / `-m` | Re-scrape classes with `Placings > 0` but no `ShowResults` |
| `--year YYYY` | Filter by show year |
| `--month N` | Filter by `StartDate` month |
| `--show-guid GUID` | Single show |
| `--list-only` | Dry run — list matching shows only |

**Resilience:** Stale-element retries, browser reconnection, DevExpress detail grid expansion.

#### `scrape_class_nonplacing_results.py` — Non-Placing Entries

| Item | Detail |
|------|--------|
| **Source** | Same ClassResults page; `grNonPlacing` grid |
| **Target** | `ShowResults` with **`Place = 0`** |
| **Queue** | `Entries > Placings`, show ended, `NonPlacingComplete = 0` |
| **Completion** | Sets `NonPlacingComplete = 1` when expected count reached |

#### `scrape_usef_horse.py` — Horse USEF Enrichment

| Item | Detail |
|------|--------|
| **Source** | `https://www.usef.org/search/horses` |
| **Target** | `Horse` — `Sire`, `Dam`, `DOB`, `Sex`, `Color`, `Breed`, `USEFID` |
| **Scope** | Horses where `USEFID IS NULL` |

#### `scrape_usef_person.py` — Rider USEF Enrichment

| Item | Detail |
|------|--------|
| **Source** | `https://www.usef.org/search/people` |
| **Target** | `Competitors` — `RiderUSEFID`, `RiderState`, `RiderUSEFStatus` |
| **Scope** | Distinct riders without `RiderUSEFID` |

### Archive Scrapers (2014 Backfill)

HorseShowsOnline no longer lists 2014 in the year picker. Archive scripts recover `ShowGUID`s from Internet Archive snapshots:

| Script | Approach |
|--------|----------|
| `parse_archive_2014.py` | HTTP + BeautifulSoup |
| `scrape_archive_links.py` | Selenium — regex GUIDs from page source |
| `scrape_archive_selenium.py` | Selenium — scan `<a>` tags for `ShowGUID=` |
| `insert_archive_guids.py` | Insert GUIDs from `2014_showguids_archive.txt` |
| `insert_2014_shows.py` | Bulk insert 2014 show records |

### Maintenance Scripts

| Script | Purpose |
|--------|---------|
| `cleanup_duplicate_nonplacing_results.py` | Remove duplicate non-placing rows |
| `fix_incorrect_show_associations.py` | Repair show/class linkage errors |
| `fix_missing_classes.py` | Backfill missing class data |

---

## Automation

All scripts in `automation/` dot-source `C:\Users\mw\ResultsAutomation\Common.ps1`.

### `Run-HorseShows.ps1` — Weekly Collection

**Scheduled task:** `ResultsAutomation - Horse Shows` (Mondays, `MultipleInstances=IgnoreNew`)

| Step | Script | Purpose |
|------|--------|---------|
| 1. Discover | `scrape_shows_by_year.py` | Refresh `ShowList` for current year |
| 2. Class results | `scrape_class_results.py --direct-url` | Scrape newly completed shows |
| 3. Missing sweep | `scrape_class_results.py --direct-url --load-missing --year {Y}` | Repair partial loads |
| 4. Non-placing (bounded) | `scrape_class_nonplacing_results.py --start-from {GUID}` | Up to **300 classes** (~2 hours) |

**Key parameters:**

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `-Years` | Current year | Years for missing-class sweep |
| `-DiscoverYears` | Current year | Years for show discovery |
| `-NonPlacingClassLimit` | 300 | Cap non-placing work per run |
| `-SkipDiscovery` / `-SkipMissingSweep` / `-SkipNonPlacing` | off | Skip steps |
| `-DryRun` | off | Log only |

**Scrape lock:** `horse_shows.scrape` — prevents concurrent Chrome/SQL contention with backlog runner.

### `Run-HorseShowsNonPlacingBacklog.ps1` — Backlog Burn-Down

| Item | Detail |
|------|--------|
| **Scale** | ~7,378 shows, ~70,977 classes, ~340K rows (~18 days) |
| **Chunk size** | 1,500 classes (~9.7 hours each) |
| **Order** | Newest shows first |
| **Restart** | Queue state in DB (`NonPlacingComplete`, `Place=0` counts) |
| **Task** | Registered **disabled**, repeats every 4 hours |

### `Run-HorseShowsCatchup.ps1` — Year Backfill

Splits class-result scraping by **month** to survive crashes/reboots:

```
scrape_class_results.py --direct-url --year YYYY --month M   (per month)
scrape_class_results.py --direct-url --load-missing --year YYYY   (final pass)
```

### `NonPlacingQueue.ps1` — Shared Queue Logic

| Function | Purpose |
|----------|---------|
| `Get-NonPlacingBacklog` | Count shows/classes/rows outstanding |
| `Get-NonPlacingEntryPoint` | Find `--start-from` GUID for a class-limit slice |
| `Get-NonPlacingEtaText` | ETA at 155 classes/hour |

---

## Power BI

### Project Files

| File / Folder | Purpose |
|---------------|---------|
| `HorseShows.pbip` | Power BI project entry point |
| `HorseShows.SemanticModel/` | Data model (relationships, DAX) |
| `HorseShows.Report/` | Report pages and visuals |
| `01_PowerBI_Views.sql` | **Run first** — creates star-schema SQL views |
| `03_DAX_Measures.dax` | 17 measure definitions |

### Data Model (7 tables, 6+ relationships)

**Dimension views:**

| View | Key Columns |
|------|-------------|
| `dimShows` | ShowID, Year, ShowName, StartDate, EndDate, ShowLocation, StateProv |
| `dimClasses` | ClassID, ShowID, ClassNumber, ClassName, ClassType, DivisionName |
| `dimRiders` | RiderID, RiderName, USEFID |
| `dimHorses` | HorseID, HorseName, OwnerName |
| `dimTrainers` | TrainerID, TrainerName |
| `dimDate` | Date, Year, MonthNum, Quarter |

**Fact view:** `factResults` — ResultID, ClassID, ShowID, RiderID, HorseID, TrainerID, Place, IsPlacing, PrizeMoney, USEF, etc.

### Report Pages (6 pages, 20 visuals)

| Page | Content |
|------|---------|
| **1. Executive Dashboard** | 4 KPI cards, entries-by-month line chart, top-10 shows bar chart |
| **2. Rider Analysis** | Rider/Year slicers, rider leaderboard table |
| **3. Horse Analysis** | Horse/Owner slicers, horse leaderboard |
| **4. Trainer Analysis** | Trainer slicer, trainer leaderboard |
| **5. Show Analysis** | Show/Division slicers, class results table |
| **6. Trends & Comparisons** | Year/State slicers, year-over-year bar chart |

**Connection:** SQL Server → `HorseShows` database → Import mode recommended.

---

## Data Flow

### End-to-End Pipeline

| Stage | Trigger | Input | Output | Skip Logic |
|-------|---------|-------|--------|------------|
| **1. Discovery** | Weekly | Year grid on ShowSelector | `ShowList` rows with `ShowGUID` | Upsert on known GUID |
| **2. Placing results** | Weekly + catchup | `ShowGUID` from `ShowList` | `ShowClass`, `ShowResults` (Place ≥ 1) | Skip if `ShowClass` exists; only ended shows |
| **3. Missing repair** | Weekly per year | Classes with `Placings > 0` but no results | Fills gaps | `--load-missing` mode |
| **4. Non-placing** | Weekly (bounded) + backlog | Classes where `Entries > Placings` | `ShowResults` with `Place = 0` | `NonPlacingComplete` flag |
| **5. USEF enrichment** | Manual | Horses/riders missing USEF IDs | `Horse.USEFID`, `Competitors.RiderUSEFID` | Skip already enriched |

### Operational Timing (Measured Rates)

| Metric | Rate |
|--------|------|
| Non-placing shows/hour | 17 |
| Non-placing classes/hour | 155 |
| Non-placing rows/hour | 1,300 |
| Weekly non-placing cap | 300 classes (~2 hours) |
| Full backlog ETA | ~435 hours (~18 days continuous) |

---

## External Data Sources

### HorseShowsOnline.com

| Page | URL Pattern | Data Extracted |
|------|-------------|----------------|
| **Show Selector** | `ShowSelector.aspx` | Show list by year; `ShowGUID` from grid row keys |
| **Class Results** | `ClassResults?ShowGUID={guid}` | Class grid + placing (`grPlacing`) + non-placing (`grNonPlacing`) |

**Technology:** ASP.NET DevExpress GridView. Scrapers use Selenium with JavaScript injection for DevExpress client APIs.

### USEF.org

| Page | URL | Data Extracted |
|------|-----|----------------|
| **Horse Search** | `usef.org/search/horses` | USEF ID, sire, dam, foal date, sex, color, breed |
| **People Search** | `usef.org/search/people` | USEF member ID, state, status |

**Requirements:** Cookie consent handling; authenticated session for full search results.

### Internet Archive (2014 only)

`web.archive.org` snapshots of ShowSelector (Jul, Nov, Dec 2014) recover `ShowGUID`s no longer available on the live site.

---

## Directory Structure

```
horse_shows/
├── create_database.sql          # DB + schema bootstrap
├── scrape_shows_by_year.py      # Stage 1: discovery
├── scrape_class_results.py      # Stage 2: placing results
├── scrape_class_nonplacing_results.py  # Stage 3: non-placing
├── scrape_usef_horse.py         # Stage 4a: horse enrichment
├── scrape_usef_person.py        # Stage 4b: rider enrichment
├── vwResults.sql                # Denormalized SQL view
├── automation/
│   ├── Run-HorseShows.ps1       # Weekly runner
│   ├── Run-HorseShowsNonPlacingBacklog.ps1
│   ├── Run-HorseShowsCatchup.ps1
│   ├── NonPlacingQueue.ps1
│   └── Register-HorseShowsBacklogTask.ps1
├── PowerBI/
│   ├── HorseShows.pbip
│   ├── 01_PowerBI_Views.sql
│   └── 03_DAX_Measures.dax
└── SQL/
    └── sp_FuzzyMatchRiders.sql
```
