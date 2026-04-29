# proteomics-db-scraper

Scraper pipeline for [https://pci-db.org/psms/](https://pci-db.org/psms/).

The pipeline is split into discrete, resumable steps. Each step writes JSON
into `outputs/step_<n>/`.

- **Step 1** — fetch the PSMS landing page and extract dropdown options.
  Outputs: `outputs/step_1/diseases.json` (from the `disease` select) and
  `outputs/step_1/tissues.json` (from the `biological_material` select).
  Each entry has a slugified `value` (used for filenames and the
  `--diseases` CLI flag), a human-readable `label` (first-letter
  capitalised), and a `query_value` preserving the raw upstream string
  passed back to the search endpoint.
- **Step 2** — for every disease, paginate the search endpoint with
  `mhc_class=I` and persist Class-I HLA epitope rows. Output: one file per
  disease at `outputs/step_2/<disease_slug>.json`. Rows are kept only when
  the upstream allele belongs to a Class I locus (HLA-A, -B, -C, -E, -F, -G).
  Each row is enriched with `hla_locus` and a slugified `hla_slug`
  (e.g. `HLA-A*02:01` → `hla_a_02_01`); `uniprot_ids` is split on commas into
  a list.

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/) for environment management
- Network access to `https://pci-db.org/` (the Claude Code web sandbox blocks
  this host; run from a local CLI/desktop session if you hit
  `Host not in allowlist`).

## Setup

```sh
uv sync
```

This creates a `.venv` and installs `requests`, `beautifulsoup4`, and `lxml`
from `pyproject.toml`.

## Run

```sh
# Step 1 — write outputs/step_1/diseases.json
uv run pci-step-1

# Step 2 — quick smoke test on a single disease, single page
uv run pci-step-2 --diseases healthy --max-pages 1

# Step 2 — full scrape of one disease across all pages
uv run pci-step-2 --diseases healthy

# Step 2 — full scrape of every disease (resumes by skipping existing files)
uv run pci-step-2

# Step 2 — re-scrape a disease (also clears its cached HTML pages)
uv run pci-step-2 --diseases healthy --overwrite
```

### Step 2 flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--diseases a,b` | all | Limit to a comma-separated list of disease values from step 1 |
| `--max-pages N` | unlimited | Safety cap on pages per disease |
| `--overwrite` | off | Re-scrape diseases whose output file already exists (cached HTML is preserved and reused; delete files under `outputs/step_2/_tmp/` manually for a true refresh) |
| `--delay SEC` | `0.5` | Polite sleep after each HTTP request |

### HTML cache and resumability

Every fetched page is written to
`outputs/step_2/_tmp/<YYYY_MM_DD>/<disease>_page<N>.html` before being
parsed. On the next run, step 2 looks for a cached page across all date
folders before making a network request, so an interrupted scrape can
resume from where it stopped without repeating work. The cache is
cumulative; older date folders are searched too. Use `--overwrite` to
force re-fetching for a specific disease, or delete files under
`outputs/step_2/_tmp/` manually.

### 5xx retry strategy

When the upstream returns a 5xx for a single page, the scraper retries
that page up to `RETRY_ATTEMPTS` times (default 5) with a doubling wait
between attempts (1s, 2s, 4s, 8s, 16s — `RETRY_INITIAL_WAIT` × 2^n).
Three terminal outcomes per page:

- **Recovered**: a retry succeeded → that page is kept, but the
  page-to-page delay drops to `--delay × SLOW_DELAY_MULTIPLIER` (default
  5×, so 0.5s if you ran with `--delay 0.1`) until the next first-attempt
  success returns it to the default. Visible as `[retry]` then `[slow]`
  / `[fast]` markers on stderr.
- **Skipped**: all retries returned 5xx → the page is recorded in the
  output's `skipped_pages` list and the scraper moves on. Slow mode
  remains active until a first-attempt success.
- **Aborted**: a non-5xx HTTP error or a connection-level error after
  urllib3's connection retries → that disease aborts with a `[error]`
  message; cached pages are preserved and the JSON output is **not**
  written, so a re-run will resume.

The slow/fast switching is per-disease (not global); each disease starts
in fast mode.

## Output shape

`outputs/step_1/diseases.json`:

```json
{
  "source_url": "https://pci-db.org/psms/",
  "fetched_at": "2026-04-28T12:34:56Z",
  "database": "DB_release_260407_default",
  "disease_count": 42,
  "diseases": [
    {
      "value": "healthy",
      "label": "Healthy",
      "query_value": "healthy"
    }
  ]
}
```

`outputs/step_2/<disease_slug>.json`:

```json
{
  "disease": {
    "value": "healthy",
    "label": "Healthy",
    "query_value": "healthy"
  },
  "mhc_class": "class_i",
  "database": "DB_release_260407_default",
  "fetched_at": "...",
  "page_count": 7,
  "site_total_results": 312,
  "by_locus": {"hla_a": 140, "hla_b": 130, "hla_c": 40, "hla_e": 2},
  "elapsed_seconds": 12.3,
  "results": [
    {
      "peptide_sequence": "AAHLPAEFTPAV",
      "tissue": "spleen",
      "disease": "healthy",
      "mhc_class": "class_i",
      "peptide_modifications": [],
      "uniprot_ids": ["P69905"],
      "affinity_rank": "9.2407",
      "hla_locus": "hla_a",
      "hla_slug": "hla_a_02_01"
    }
  ]
}
```

## Notes on the table parser

The PSMS results table is parsed heuristically: the largest table on the
page that has a `<thead>` (or first row of `<th>`s) with at least three
columns is treated as the results table, headers are snake-cased, and each
cell becomes a flat string. If the upstream column names ever drift,
inspect a cached HTML page under `outputs/step_2/_tmp/<date>/` and adjust
`parse_results_table` / `ALLELE_COLUMN_CANDIDATES` in `steps/common.py`
and `steps/step_2_epitopes.py`.

## Repo layout

```
proteomics-db-scraper/
├── pyproject.toml
├── README.md
├── steps/
│   ├── __init__.py
│   ├── common.py
│   ├── step_1_diseases.py
│   └── step_2_epitopes.py
└── outputs/
    ├── step_1/
    │   ├── diseases.json
    │   └── tissues.json
    └── step_2/
        ├── <disease_slug>.json
        └── _tmp/<YYYY_MM_DD>/<disease>_page<N>.html
```
