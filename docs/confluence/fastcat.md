# FastCAT Results Database

A comprehensive system for scraping, storing, and analyzing **AKC Fast Coursing Ability Test (Fast CAT / FCAT)** results from the American Kennel Club.

| | |
|---|---|
| **Repository** | [github.com/mwtorq/fastcat](https://github.com/mwtorq/fastcat) |
| **Database** | `FastCAT` on `localhost\SQLEXPRESS` |
| **Schema** | `sAKC` |
| **Authentication** | Windows Authentication (Trusted Connection) |
| **Automation** | `automation/Run-FastCat.ps1` (weekly) |

## Table of Contents

- [Purpose and Overview](#purpose-and-overview)
  - [Primary Data Sources](#primary-data-sources)
- [Database Schema](#database-schema)
  - [Table: sAKC.Events](#table-sakcevents)
  - [Table: sAKC.Dogs](#table-sakcdogs)
  - [Table: sAKC.Results](#table-sakcresults)
  - [Database Objects](#database-objects)
- [Python Scripts](#python-scripts)
  - [Core Pipeline (Ongoing Operations)](#core-pipeline-ongoing-operations)
  - [Historical Data Import (One-Time)](#historical-data-import-one-time)
  - [Schema / Migration](#schema--migration)
  - [Data Quality / Maintenance](#data-quality--maintenance)
- [Automation](#automation)
  - [automation/Run-FastCat.ps1](#automationrun-fastcatps1)
- [Data Flow](#data-flow)
  - [Ongoing (Weekly Automation)](#ongoing-weekly-automation)
  - [Initial Setup (One-Time)](#initial-setup-one-time)
  - [scrape_and_import_fastcat.py Detail](#scrape_and_import_fastcatpy-detail)
- [Directory Structure](#directory-structure)
- [Dependencies](#dependencies)
  - [System Requirements](#system-requirements)
  - [Environment Variables](#environment-variables)
- [Performance Notes](#performance-notes)
  - [Manual Maintenance](#manual-maintenance)
- [Key Design Decisions](#key-design-decisions)

---

## Purpose and Overview

FastCAT collects, normalizes, and analyzes AKC Fast CAT trial results. The pipeline:

1. Scrapes the AKC Event Calendar and per-event results pages
2. Stores normalized data in SQL Server (`Events`, `Dogs`, `Results`)
3. Computes derived metrics (time, handicap, MPH average, breed/year rankings)
4. Archives raw HTML result pages for backfilling AKC registration numbers (`AKCDogID`)
5. Supports one-time bulk import from historical Excel workbooks (2021–2025)

### Primary Data Sources

| Source | URL |
|--------|-----|
| Event calendar | `https://www.apps.akc.org/apps/event_calendar/index.cfm?event_type=FCAT` |
| Event results | `https://www.apps.akc.org/apps/events/search/index_results.cfm?action=event_info&comp_type=FCAT&status=RSLT&event_number={EventNumber}` |
| Dog profile | `https://www.apps.akc.org/apps/store/proxy/get_points.cfm?cde_comp_group=CONF&cde_product_type=COMP_REC&regnum={AKCDogID}` |

---

## Database Schema

Third normal form design with three core tables:

```
Events (1) ──< Results >── (1) Dogs
   EventID              DogsID
                        EventID
```

### Table: `sAKC.Events`

| Column | Type | Notes |
|--------|------|-------|
| `EventID` | `INT IDENTITY` | Primary key |
| `EventNumber` | `NVARCHAR(50) NOT NULL` | AKC event number; **UNIQUE** |
| `EventName` | `NVARCHAR(255)` | Club/event name |
| `EventLocation` | `NVARCHAR(255)` | Venue name |
| `EventDate` | `DATE` | Event date |
| `Year` | `INT` | Calendar year |
| `EventAddress` | `NVARCHAR(255)` | Street address |
| `City` | `NVARCHAR(100)` | City |
| `State` | `NVARCHAR(50)` | Two-letter state |
| `Location` | `NVARCHAR(255)` | Inside/Outside |
| `TotalStarters` | `INT` | Starter count from results page |

### Table: `sAKC.Dogs`

| Column | Type | Notes |
|--------|------|-------|
| `DogsID` | `INT IDENTITY` | Primary key |
| `DogName` | `NVARCHAR(255) NOT NULL` | Registered/show name |
| `Breed` | `NVARCHAR(100)` | Breed |
| `Owner` | `NVARCHAR(255)` | Owner name |
| `AKCDogID` | `NVARCHAR(50) NULL` | AKC registration number (e.g. `MA85130701`) |
| `DogProfile` | Computed | AKC profile URL built from `AKCDogID` |

**Unique constraint:** `(DogName, Owner)`

### Table: `sAKC.Results`

| Column | Type | Notes |
|--------|------|-------|
| `ResultID` | `INT IDENTITY` | Primary key |
| `EventID` | `INT NOT NULL` | FK → Events |
| `DogsID` | `INT NOT NULL` | FK → Dogs |
| `Speed` | `DECIMAL(10,2)` | MPH |
| `Time` | Computed | `204.545 / Speed` |
| `Points` | `DECIMAL(10,2)` | Points earned |
| `Ranking` | `INT NULL` | Breed/year ranking by MPHAvg |
| `Handicap` | Computed | `ROUND(Points / Speed, 1)` |
| `MPHAvg` | `DECIMAL(10,2) NULL` | Average of top 3 speeds for same dog in same year |

**Unique constraint:** `(EventID, DogsID)` — one result per dog per event

### Database Objects

| Object | Type | Purpose |
|--------|------|---------|
| `sAKC.sp_UpdateMPHAvgAndRanking` | Stored procedure | Batch-recalculates MPHAvg and Ranking |
| `sAKC.tr_Results_UpdateMPHAvgRanking` | Trigger | Maintains MPHAvg/Ranking on INSERT/UPDATE/DELETE |

**MPHAvg formula:** Average of the top 3 `Speed` values for the same `DogsID` in the same `Year`, where `EventDate <=` current row's event date.

**Ranking formula:** `DENSE_RANK()` on `MPHAvg DESC`, partitioned by `Breed`, `Year`, and `EventDate`.

---

## Python Scripts

All scripts connect to `localhost\SQLEXPRESS`, database `FastCAT`, schema `sAKC`.

### Core Pipeline (Ongoing Operations)

| Script | Purpose |
|--------|---------|
| **`scrape_and_import_fastcat.py`** | **Main production script.** Scrapes AKC calendar (Selenium) for a date range, skips events already in DB, imports Events/Dogs/Results. Args: `--start` / `--end` (YYYY-MM-DD). |
| **`scrape_event_calendar_range.py`** | Calendar-only scraper; writes JSON to `./scraped_results/`. |
| **`download_akc_results.py`** | Downloads HTML for all events in DB to `AKCResults/` as `{Year}_{EventNumber}_{EventName}.html`. |
| **`download_missing_akc_results.py`** | Downloads only missing HTML files. |
| **`update_akcdogid_from_html.py`** | Backfills `AKCDogID` from `dog_id=` links in archived HTML. |
| **`update_remaining_akcdogid_improved.py`** | Aggressive fuzzy matching for remaining NULL AKCDogIDs. |

### Historical Data Import (One-Time)

| Script | Source |
|--------|--------|
| `import_2021_2022_results.py` | `Results/2021-2022 FastCAT Results.xlsx` |
| `import_2023_2024_results.py` | `Results/2023-2024 FastCAT Results.xlsx` |
| `import_2025_results.py` | `Results/2025 FastCAT Results.xlsx` |

### Schema / Migration

| Script | Purpose |
|--------|---------|
| `create_fastcat_tables.py` | Create/drop/clear/verify/recreate tables. Actions: `create`, `drop`, `clear`, `verify`, `recreate`. |
| `add_computed_columns_results.py` | Add computed `Time` and `Handicap` columns |
| `add_dogprofile_column.py` | Add computed `DogProfile` column |
| `add_dogid_column_and_update.py` | Rename `DogID` → `DogsID`; add and populate `AKCDogID` |
| `optimize_mpavg_ranking.py` | **Recommended.** Converts MPHAvg/Ranking to stored columns with indexes, SP, and trigger |

### Data Quality / Maintenance

| Script | Purpose |
|--------|---------|
| `consolidate_dogs.py` | Merge duplicate Dogs (same DogName+Breed+Owner). Supports `--dry-run`. |
| `fix_ranking_calculation.py` | Fix ranking bug (incorrect EventDate partitioning) |
| `check_akcdogid_status.py` | Count NULL/empty AKCDogID values |
| `verify_akcdogid_accuracy.py` | Verify AKCDogID values against HTML archive |

---

## Automation

### `automation/Run-FastCat.ps1`

Weekly orchestration script. Depends on shared helpers at `C:\Users\mw\ResultsAutomation\Common.ps1` (override via `$env:RESULTS_AUTOMATION_HOME`).

#### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `-LookbackDays` | `45` | Days back from today |
| `-StartDate` / `-EndDate` | today / today−45 | Calendar scan window |
| `-SkipHtmlArchive` | off | Skip HTML download + AKCDogID backfill |
| `-SkipRankingRecompute` | off | Skip `sp_UpdateMPHAvgAndRanking` |
| `-RecomputeTimeoutSeconds` | `21600` (6 hr) | Timeout for ranking recompute |
| `-DryRun` | off | Log only, no changes |

#### Execution Steps

1. Load shared helpers from `Common.ps1`
2. Start run log (`Start-RunLog -Name 'fastcat'`)
3. Record baseline counts: `sAKC.Events`, `sAKC.Results`
4. **Disable trigger** `sAKC.tr_Results_UpdateMPHAvgRanking`
5. Run `scrape_and_import_fastcat.py --start {StartDate} --end {EndDate}`
6. If new rows imported → `EXEC sAKC.sp_UpdateMPHAvgAndRanking`
7. **Re-enable trigger**
8. Run `download_akc_results.py` (unless `-SkipHtmlArchive`)
9. Run `update_akcdogid_from_html.py` (unless `-SkipHtmlArchive`)
10. Log metrics and exit via `Complete-RunLog`

---

## Data Flow

### Ongoing (Weekly Automation)

```
AKC Event Calendar
    → scrape_and_import_fastcat.py
        → sAKC.Events, sAKC.Dogs, sAKC.Results
    → EXEC sAKC.sp_UpdateMPHAvgAndRanking
    → download_akc_results.py → AKCResults/*.html
    → update_akcdogid_from_html.py → Dogs.AKCDogID
```

### Initial Setup (One-Time)

```
create_fastcat_tables.py create
add_computed_columns_results.py
add_dogprofile_column.py
optimize_mpavg_ranking.py
import_2021_2022_results.py
import_2023_2024_results.py
import_2025_results.py
add_dogid_column_and_update.py
consolidate_dogs.py
```

### `scrape_and_import_fastcat.py` Detail

1. **Calendar scrape:** For each day in range, load FCAT calendar URL; extract 10-digit event numbers plus metadata.
2. **Deduplication:** Skip events where `Results` already has rows for that `EventNumber`.
3. **Import:** For each new event, fetch results page HTML, parse dog results (name, breed, owner, speed, points), insert into Events → Dogs → Results.

---

## Directory Structure

```
fastcat/
├── AKCResults/              # ~9,900 archived HTML result pages
├── Results/                 # Excel workbooks + event CSVs (Git LFS)
├── automation/
│   └── Run-FastCat.ps1      # Weekly orchestration
├── Playwright/              # MCP browser automation setup (dev tooling)
├── *.py                     # 33 Python scripts
├── Dogs.sql / Events.sql / Results.sql
└── README.md
```

---

## Dependencies

| Package | Used for |
|---------|----------|
| `pyodbc` | SQL Server connectivity |
| `pandas` | Excel read/write, data manipulation |
| `openpyxl` | `.xlsx` read/write |
| `requests` | HTTP scraping |
| `beautifulsoup4` | HTML parsing |
| `selenium` | Headless Chrome for calendar scraping |

### System Requirements

| Component | Requirement |
|-----------|-------------|
| SQL Server | Express at `localhost\SQLEXPRESS` |
| ODBC | Driver 17 for SQL Server |
| Chrome + ChromeDriver | Selenium calendar scraping |
| Git LFS | Required for `.xls*` and `.bak` files |
| PowerShell | Automation runner |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `RESULTS_AUTOMATION_HOME` | Override path to shared automation folder (default: `C:\Users\mw\ResultsAutomation`) |

---

## Performance Notes

- **Ranking recompute:** 10–30 minutes for 1M+ rows; automation allows 6-hour timeout.
- **Trigger disabled during import:** Without this, each inserted row triggers a full-table recalculation (days for small imports).
- **Idempotent scraping:** `scrape_and_import_fastcat.py` skips events already in `Results`; safe to re-run overlapping date windows.

### Manual Maintenance

```sql
-- Refresh rankings after bulk changes
EXEC sAKC.sp_UpdateMPHAvgAndRanking;

-- Re-enable trigger if a run failed mid-import
ENABLE TRIGGER sAKC.tr_Results_UpdateMPHAvgRanking ON sAKC.Results;
```

```powershell
# Custom date range
.\Run-FastCat.ps1 -StartDate 2026-01-01 -EndDate 2026-08-15

# Import only, skip HTML archive
.\Run-FastCat.ps1 -SkipHtmlArchive
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| 3NF schema (Events/Dogs/Results) | Avoid duplication; one dog record per name+owner |
| Computed `Time`/`Handicap`/`DogProfile` | Derived from stored inputs; always consistent |
| Stored `MPHAvg`/`Ranking` with trigger | Computed-column approach was too slow at scale |
| HTML archive in `AKCResults/` | AKC reg numbers not always available at scrape time |
| Shared `Common.ps1` outside repo | Same automation helpers used across three results-collection repos |
