# JRTCA Trial Results

A data pipeline for collecting, parsing, normalizing, and storing **Jack Russell Terrier Club of America (JRTCA) trial results** and entry catalog data.

| | |
|---|---|
| **Repository** | [github.com/mwtorq/jrtca_results](https://github.com/mwtorq/jrtca_results) |
| **Database** | `TrialResults` on `localhost\SQLEXPRESS` |
| **Schema** | `sResults` |
| **Authentication** | Windows Authentication (ODBC Driver 17/18) |
| **Automation** | `automation/Run-JrtcaResults.ps1` (weekly) |
| **Reporting** | `PowerBI/TrialResults.pbix` |

## Table of Contents

- [Purpose and Overview](#purpose-and-overview)
- [Database Schema](#database-schema)
  - [Entity Relationship](#entity-relationship)
  - [Table Reference](#table-reference)
  - [Views and SQL Artifacts](#views-and-sql-artifacts)
- [Python Scripts](#python-scripts)
  - [Core Pipeline (Production)](#core-pipeline-production)
  - [Key CLI Flags](#key-cli-flags)
  - [Data Acquisition / Recovery](#data-acquisition--recovery)
  - [Maintenance Utilities](#maintenance-utilities)
- [Automation](#automation)
  - [automation/Run-JrtcaResults.ps1](#automationrun-jrtcaresultsps1)
- [Data Sources](#data-sources)
  - [Year Folders (1984–2026)](#year-folders-19842026)
  - [Entry Catalogs (2008–2025)](#entry-catalogs-20082025)
  - [Special Subfolders](#special-subfolders)
  - [Web Sources](#web-sources)
  - [Report Outputs](#report-outputs)
- [Power BI](#power-bi)
- [Data Flow and Workflows](#data-flow-and-workflows)
  - [Workflow A: Weekly Automated Update](#workflow-a-weekly-automated-update)
  - [Workflow B: Initial Database Setup](#workflow-b-initial-database-setup)
  - [Workflow C: Data Quality / Repair](#workflow-c-data-quality--repair)
  - [Parsing Logic](#parsing-logic)
  - [Key Design Decisions](#key-design-decisions)
- [Script → Database Mapping](#script--database-mapping)

---

## Purpose and Overview

The jrtca_results repository ingests decades of JRTCA trial outcomes and entry catalogs into a normalized SQL Server database for analysis and reporting.

**Primary goals:**

- **Ingest** trial results from local files (HTML, TXT, PDF), web scraping (JRTCA Yearbook, JRTCC, Trial Vault), and historical archives (Wayback Machine)
- **Parse** unstructured result documents into structured placements (trial → division → class → dog/owner → result/time)
- **Load** entry catalogs (2008–2025) with dog pedigree and class entry data
- **Normalize** OCR errors, duplicate names, and inconsistent division/class naming
- **Verify** database content against original source files after normalization
- **Report** via Power BI and SQL views

**Upstream reference:** Schema modeled on `TrialData.sJRTCA` (legacy database).

---

## Database Schema

### Entity Relationship

```
TrialList ──┬── TrialClass ── Class ── Division
            │       │
            │       └── TrialPlacements ── Dog ── Owner
            │               │
            │               └── TrialPlacements_Times
            │
            └── TrialResults (raw blob, legacy)

CatalogEntry ── Dog, Owner, Class
Relationship ── Dog (pedigree graph)
Section ── Class (optional)
```

### Table Reference

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **Division** | Trial divisions (Conformation, Racing, GTG, etc.) | `DivisionID` (PK), `DivisionName` |
| **Section** | Sub-grouping within divisions | `SectionID` (PK), `SectionName` |
| **Class** | Class definitions | `ClassID` (PK), `DivisionID` (FK), `ClassName`, `SectionID` (FK) |
| **TrialList** | One row per trial event | `TrialListID` (PK), `Year`, `Trial`, `TrialName`, `StartDate`, `EndDate`, `TrialResultFilePath`, `Loaded`, `Chairperson`, `LocationCity`, `LocationState`, `JRTCA` |
| **TrialClass** | Classes offered at a specific trial | `TrialClassID` (PK), `TrialListID` (FK), `ClassID` (FK), `EntriesOnly`, `EntryCount`, `ClassNumber` |
| **Owner** | Dog owners/handlers | `OwnerID` (PK), `OwnerName` |
| **Dog** | Dogs in trials/catalog | `DogID` (PK), `DogName`, `OwnerID` (FK), `Sire`, `Dam`, `Sex` |
| **TrialPlacements** | Individual placement per class | `TrialPlacementsID` (PK), `TrialListID` (FK), `TrialClassID` (FK), `DogID` (FK), `Result` |
| **TrialPlacements_Times** | Racing/GTG times | `TrialPlacements_TimesID` (PK), `TrialPlacementsID` (FK), `Time` |
| **CatalogEntry** | Entry catalog rows (2008–2025) | `CatalogEntryID` (PK), `Year`, `EntryNumber`, `DogID`, `DogName`, `OwnerID`, `Sire`, `Dam`, `Sex`, `ClassID`, `ClassName` |
| **Relationship** | Pedigree/family graph | `RelationshipID` (PK), `DogID` (FK), `RelatedDogID` (FK), `RelatedDogName`, `RelationshipType`, `ViaDogName` |
| **TrialResults** | Legacy raw results blob | `TrialResultsID` (PK), `TrialListID` (FK), `Results` |

### Views and SQL Artifacts

| Artifact | Purpose |
|----------|---------|
| `vwResults.sql` | Denormalized view joining trials, placements, dogs, owners, pedigree, and times |
| `create_trialresults_database.py` | Bootstrap `TrialResults.sResults` from `TrialData.sJRTCA` |
| `create_normalization_indexes.py` | Performance indexes on `DogID`, `TrialListID`, `TrialClassID` |

---

## Python Scripts

### Core Pipeline (Production)

| Script | Purpose |
|--------|---------|
| **`scrape_trial_results_fixed.py`** | **Production scraper.** Downloads/parses from JRTCA Yearbook, JRTCC, Trial Vault; scans local year folders. |
| **`populate_trialresults_fixed.py`** | **Production loader.** Parses trial files, inserts into `sResults`; supports catalog loading. |
| **`normalize_trialresults_data.py`** | Post-load normalization: divisions, classes, dog/owner names, duplicate merges, OCR repair. |
| **`verify_trial_normalization.py`** | Re-parses source files and compares to DB; runs normalize → compare → normalize → compare cycle. |
| **`parse_catalog.py`** | Parses entry catalog DOC/PDF files (2008–2025); infers sex, builds pedigree relationships. |

### Key CLI Flags

**`scrape_trial_results_fixed.py`**
- `--web-only` — skip local re-scan; only fetch new web postings
- `--min-year N` — bound Yearbook/JRTCC/Trial Vault walks
- `--trialvault-new` — unattended Trial Vault import (env: `TRIALVAULT_EMAIL`, `TRIALVAULT_PASSWORD`)

**`populate_trialresults_fixed.py`**
- `--year N` — process one year
- `--folder "MO Earthdogs"` — special subfolder only
- `--normalize-at-end` — defer normalization until batch complete
- `--load-catalog` / `--catalog-only` — load entry catalogs
- `--reuse-entities` — match existing dogs/owners; skip merges
- `--clear` / `--clear-trials-only` — wipe data before load

**`verify_trial_normalization.py`**
- `--passes 2` — normalize/compare cycles (default: 2)
- `--year`, `--trial-id`, `--folder` — scope filters
- `--fix-dog-merges`, `--fix-class-assignments` — targeted repairs

### Data Acquisition / Recovery

| Script | Purpose |
|--------|---------|
| `wayback_extractor.py` | Fetches JRTNNC/Cumberland trial results from Wayback Machine |
| `archive_recover/build_schedule.py` | Builds merged trial schedules from archive.org snapshots |
| `archive_recover/parse_old_format.py` | Parses old terrier.com schedule format (~1999–2006) |
| `archive_recover/parse_new_format.py` | Parses therealjackrussell.com schedule format (~2009+) |

### Maintenance Utilities

| Script | Purpose |
|--------|---------|
| `backfill_trial_dates.py` | Fixes Jan-1 placeholder dates by re-scanning source file headers |
| `reload_mo_earthdogs.py` | Deletes and reloads Missouri Earthdogs trials (1999–2019) |
| `split_merged_relatives.py` | Splits dogs incorrectly merged by name similarity |
| `generate_comprehensive_report.py` | Combines catalog + trial reports |

---

## Automation

### `automation/Run-JrtcaResults.ps1`

Weekly orchestration wrapping the full pipeline. Depends on `C:\Users\mw\ResultsAutomation\Common.ps1`.

#### Pipeline Steps

```
1. scrape_trial_results_fixed.py --min-year <Y> [--web-only]
      ↓
2. populate_trialresults_fixed.py --year <Y> --normalize-at-end  (per target year)
      ↓  (normalize → compare → normalize → compare, 2 passes)
3. scrape_trial_results_fixed.py --trialvault-new  (if credentials available)
      ↓
4. verify_trial_normalization.py --year <Y> --passes 2  (if Trial Vault added trials)
```

#### Parameters

| Parameter | Default | Effect |
|-----------|---------|--------|
| `-Years` | Current year + prior year | Years to load |
| `-SkipDownload` | false | Skip step 1 |
| `-RescanLocalFiles` | false | Omit `--web-only` on scraper |
| `-IncludeTrialVault` | true | Run Trial Vault import |
| `-DryRun` | false | Log only, no changes |

#### Metrics Logged

- `sResults.TrialList` count before/after
- `sResults.TrialPlacements` count before/after
- Logs written to `RESULTS_AUTOMATION_HOME`

---

## Data Sources

### Year Folders (1984–2026)

| Location | Content |
|----------|---------|
| `1984/` … `2026/` | Per-year trial result files (HTML, TXT, PDF) |
| Root-level `YYYY Entries Catalog.doc/pdf` | National trial entry catalogs |
| `extracted_text_YYYY.txt` | Cached plain-text extraction from catalogs |
| `jrtcc_downloads/YYYY/` | JRTCC PDFs downloaded by scraper |

### Entry Catalogs (2008–2025)

| Years | Format |
|-------|--------|
| 2008–2017 | `.doc` (`YYYY_Entries_Catalog.doc`) |
| 2018–2025 | `.pdf` (`YYYY Entries Catalog.pdf`) |

### Special Subfolders

| Folder | Coverage | Notes |
|--------|----------|-------|
| **`MO Earthdogs/`** | 1999–2019 | Tab-separated and PDF formats; GTG times in separate files |
| **`Gold Coast/`** | 2011–2018 | Gold Coast Terrier Trial I/II |
| **`JRTCC/`** | 1992+ | Jack Russell Terrier Club of Canada results |

### Web Sources

| Source | URL | Scraper |
|--------|-----|---------|
| JRTCA Yearbook | `https://www.jrtcayearbook.com/` | `scrape_trial_results_fixed.py` |
| JRTCC | `https://www.jrtcc.ca/trials/#results` | Same (PDF to `jrtcc_downloads/`) |
| Trial Vault | `https://v3.trialvault.dog` | `--trialvault-new` (login required) |
| Wayback / JRTNNC | `http://www.jrtnnc.com/` | `wayback_extractor.py`, `archive_recover/` |

### Report Outputs

| File | Generator |
|------|-----------|
| `Trial_Results_Report.txt` | `scrape_trial_results_fixed.py` |
| `Entries_Catalog_Report.txt` | `parse_catalog.py` |
| `Comprehensive_Report.txt` | `generate_comprehensive_report.py` |
| `normalization_skew_report.csv` | `verify_trial_normalization.py` |

---

## Power BI

| File | Purpose |
|------|---------|
| **`PowerBI/TrialResults.pbix`** | Power BI report connected to `TrialResults` database |

**Data model:**
- DirectQuery or Import from `TrialResults.sResults`
- Primary analytical surface: `sResults.vwResults`
- Dimensions: Year, Trial, Division, Class, Dog, Owner, pedigree fields
- Measures: placement counts, results by owner/year, racing times

---

## Data Flow and Workflows

### Workflow A: Weekly Automated Update

1. **Download** — `scrape_trial_results_fixed.py --min-year <Y> --web-only` fetches new Yearbook/JRTCC results
2. **Load** — `populate_trialresults_fixed.py --year <Y> --normalize-at-end` discovers files, skips trials already in DB
3. **Normalize + Verify** — Deferred batch runs `verify_trial_normalization.run_normalize_verify_cycle(passes=2)`
4. **Trial Vault** — If credentials exist, `--trialvault-new` imports new events

### Workflow B: Initial Database Setup

1. `python create_trialresults_database.py` — create schema
2. `python create_normalization_indexes.py` — add indexes
3. `python populate_trialresults_fixed.py` — full historical load
4. `python populate_trialresults_fixed.py --load-catalog` — load 2008–2025 catalogs

### Workflow C: Data Quality / Repair

| Task | Script |
|------|--------|
| Fix placeholder dates | `backfill_trial_dates.py` |
| Reload MO Earthdogs | `reload_mo_earthdogs.py` |
| Undo bad dog merges | `split_merged_relatives.py --apply` |
| Standalone normalization | `normalize_trialresults_data.py` |
| Audit DB vs source | `verify_trial_normalization.py --report-only` |

### Parsing Logic

Both scraper and loader handle:

- **Divisions:** Conformation, Racing (Flat/Steeplechase), Go-To-Ground, Super Earth, Agility, Obedience, Nosework, Youth
- **Placements:** 1st/2nd/3rd, Champion/Reserve, Best; tab-separated and free-text formats
- **Name matching:** `normalize_name()`, `names_are_similar()` for dog/owner deduplication
- **Times:** Racing seconds, GTG times → `TrialPlacements_Times`
- **Pedigree:** Sire/dam from catalog → `Relationship` (sibling, grandsire, half-sibling)

### Key Design Decisions

- **`populate_trialresults_fixed.py`** is the production loader with format-specific parsers
- **Source file path** stored in `TrialList.TrialResultFilePath` enables verification without re-scraping
- **Normalization is iterative** — two passes because first-pass merges can introduce new skew
- **Entity reuse** (`--reuse-entities`) links trial dogs to catalog dogs without destructive merges

---

## Script → Database Mapping

| Source Data | Parser | Target Tables |
|-------------|--------|---------------|
| Trial result HTML/TXT/PDF | `populate_trialresults_fixed.py` | `TrialList`, `TrialClass`, `TrialPlacements`, `TrialPlacements_Times`, `Dog`, `Owner`, `Division`, `Class` |
| Entry catalogs | `parse_catalog.py` → `load_catalog_data()` | `CatalogEntry`, `Dog`, `Owner`, `Class`, `Relationship` |
| Trial Vault | `scrape_trial_results_fixed.py` → loader | Same as trials + times |
