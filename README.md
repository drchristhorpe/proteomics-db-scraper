# proteomics-db-scraper

Scraper pipeline for [https://pci-db.org/psms/](https://pci-db.org/psms/).

The pipeline is split into discrete, resumable steps. Each step writes JSON
into `outputs/step_<n>/`.

- **Step 1** — fetch the PSMS landing page and extract the `disease` dropdown
  options. Output: `outputs/step_1/diseases.json`.
- **Step 2** — for every disease, paginate the search endpoint with
  `mhc_class=I` and persist Class-I HLA epitope rows. Output: one file per
  disease at `outputs/step_2/<disease_slug>.json`. Rows are kept only when
  `best_hla_allele` belongs to a Class I locus (HLA-A, -B, -C, -E, -F, -G).
  Each row is enriched with `hla_locus` and a slugified `hla_slug`
  (e.g. `HLA-A*02:01` → `hla_a_02_01`).

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

# Step 2 — quick smoke test on a single disease, single page, save raw HTML
uv run pci-step-2 --diseases healthy --max-pages 1 --debug

# Step 2 — full scrape of one disease across all pages
uv run pci-step-2 --diseases healthy

# Step 2 — full scrape of every disease (resumes by skipping existing files)
uv run pci-step-2

# Step 2 — re-scrape, overwriting existing outputs
uv run pci-step-2 --overwrite
```

### Step 2 flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--diseases a,b` | all | Limit to a comma-separated list of disease values from step 1 |
| `--max-pages N` | unlimited | Safety cap on pages per disease |
| `--overwrite` | off | Re-scrape diseases whose output file already exists |
| `--delay SEC` | `0.5` | Polite sleep after each HTTP request |
| `--debug` | off | Save raw page-1 HTML to `outputs/step_2/_debug/` for parser tuning |

## Output shape

`outputs/step_1/diseases.json`:

```json
{
  "source_url": "https://pci-db.org/psms/",
  "fetched_at": "2026-04-28T12:34:56Z",
  "database": "DB_release_260407_default",
  "disease_count": 42,
  "diseases": [{"value": "healthy", "label": "Healthy", "selected": false}]
}
```

`outputs/step_2/<disease_slug>.json`:

```json
{
  "disease": {"value": "healthy", "label": "Healthy"},
  "mhc_class": "I",
  "database": "DB_release_260407_default",
  "fetched_at": "...",
  "page_count": 7,
  "site_total_pages": 7,
  "site_total_results": 312,
  "result_count": 312,
  "filtered_out": 0,
  "by_locus": {"A": 140, "B": 130, "C": 40, "E": 2},
  "elapsed_seconds": 12.3,
  "results": [
    {
      "best_hla_allele": "HLA-A*02:01",
      "hla_locus": "A",
      "hla_slug": "hla_a_02_01",
      "...": "...other parsed columns kept verbatim..."
    }
  ]
}
```

## Notes on the table parser

The PSMS results table is parsed heuristically: the largest table on the
page that has a `<thead>` (or first row of `<th>`s) with at least three
columns is treated as the results table, headers are snake-cased, and each
cell becomes `{"text": "...", "href"?: "..."}` so links (UniProt, etc.)
survive. If the upstream column names ever drift, run with `--debug` once
to dump the raw HTML and adjust `parse_results_table` /
`ALLELE_COLUMN_CANDIDATES` in `steps/common.py` and
`steps/step_2_epitopes.py`.

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
    ├── step_1/diseases.json
    └── step_2/<disease_slug>.json
```
