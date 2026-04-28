"""Step 2 — for each disease, paginate the search and persist Class-I HLA rows.

Loads the disease list from ``outputs/step_1/diseases.json`` and walks the
PSMS search endpoint with ``mhc_class=I`` for each disease, keeping only rows
whose ``best_hla_allele`` belongs to a Class I locus (HLA-A/B/C/E/F/G).
Writes one JSON file per disease into ``outputs/step_2/<disease_slug>.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    BASE_URL,
    HLA_CLASS_I_LOCI,
    atomic_write_json,
    build_session,
    coalesce_text,
    find_allele_in_row,
    get,
    hla_locus,
    is_class_i_allele,
    parse_pagination,
    parse_results_table,
    slugify_hla,
    slugify_text,
    step_dir,
    utcnow_iso,
)

ALLELE_COLUMN_CANDIDATES: tuple[str, ...] = (
    "best_hla_allele",
    "hla_allele",
    "best_allele",
    "allele",
)


def _build_params(*, database: str | None, disease: str, page: int) -> dict[str, str]:
    return {
        "database": database or "",
        "sequence": "",
        "mhc_class": "I",
        "disease": disease,
        "biological_material": "",
        "modifications": "",
        "uniprot": "",
        "dignity": "",
        "best_hla_allele": "",
        "page": str(page),
    }


def _enrich_row(row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    allele = find_allele_in_row(row, ALLELE_COLUMN_CANDIDATES)
    enriched = dict(row)
    enriched["best_hla_allele"] = allele
    enriched["hla_locus"] = hla_locus(allele)
    enriched["hla_slug"] = slugify_hla(allele)
    return enriched, allele


def scrape_disease(
    session,
    *,
    database: str | None,
    disease_value: str,
    disease_label: str,
    delay: float,
    max_pages: int | None,
    debug_dir: Path | None,
) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    filtered_out = 0
    page = 1
    pages_fetched = 0
    total_pages_seen: int | None = None
    total_results_seen: int | None = None
    started = time.monotonic()

    while True:
        if max_pages is not None and page > max_pages:
            break
        params = _build_params(database=database, disease=disease_value, page=page)
        response = get(session, BASE_URL, params=params, delay=delay)
        html = response.text
        pages_fetched += 1

        if debug_dir is not None and page == 1:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{slugify_text(disease_value)}_p1.html").write_text(
                html, encoding="utf-8"
            )

        rows = parse_results_table(html)
        if not rows:
            break

        for row in rows:
            enriched, allele = _enrich_row(row)
            if is_class_i_allele(allele):
                kept.append(enriched)
            else:
                filtered_out += 1

        pg = parse_pagination(html)
        if pg.get("total_pages") is not None:
            total_pages_seen = pg["total_pages"]
        if pg.get("total_results") is not None:
            total_results_seen = pg["total_results"]

        if total_pages_seen is not None and page >= total_pages_seen:
            break
        page += 1

    by_locus = Counter(
        row["hla_locus"] for row in kept if row.get("hla_locus") in HLA_CLASS_I_LOCI
    )
    elapsed = round(time.monotonic() - started, 2)

    return {
        "disease": {"value": disease_value, "label": disease_label},
        "mhc_class": "I",
        "database": database,
        "fetched_at": utcnow_iso(),
        "page_count": pages_fetched,
        "site_total_pages": total_pages_seen,
        "site_total_results": total_results_seen,
        "result_count": len(kept),
        "filtered_out": filtered_out,
        "by_locus": {locus: by_locus[locus] for locus in HLA_CLASS_I_LOCI if by_locus[locus]},
        "elapsed_seconds": elapsed,
        "results": kept,
    }


def _load_step_1() -> dict[str, Any]:
    path = step_dir(1) / "diseases.json"
    if not path.exists():
        raise SystemExit(
            f"Step 1 output missing at {path}. Run `pci-step-1` first."
        )
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _select_diseases(
    all_diseases: list[dict[str, Any]],
    requested: list[str] | None,
) -> list[dict[str, Any]]:
    if not requested:
        return all_diseases
    by_value = {d["value"]: d for d in all_diseases}
    selected = []
    missing = []
    for value in requested:
        if value in by_value:
            selected.append(by_value[value])
        else:
            missing.append(value)
    if missing:
        raise SystemExit(
            f"Unknown disease values: {', '.join(missing)}. "
            f"Available: {', '.join(by_value)}"
        )
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diseases",
        type=str,
        default=None,
        help="Comma-separated disease values to limit to (default: all).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Safety cap on pages per disease (default: unlimited).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-scrape diseases whose output JSON already exists.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to sleep after each HTTP request (default: 0.5).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save raw page-1 HTML per disease to outputs/step_2/_debug/.",
    )
    args = parser.parse_args()

    step1 = _load_step_1()
    database = step1.get("database")
    requested = (
        [v.strip() for v in args.diseases.split(",") if v.strip()]
        if args.diseases
        else None
    )
    diseases = _select_diseases(step1.get("diseases", []), requested)
    if not diseases:
        raise SystemExit("No diseases to process.")

    out_dir = step_dir(2)
    debug_dir = (out_dir / "_debug") if args.debug else None
    session = build_session()

    overall_started = time.monotonic()
    summaries: list[tuple[str, int, int, int]] = []

    for disease in diseases:
        value = disease["value"]
        label = disease.get("label", value)
        out_path = out_dir / f"{slugify_text(value)}.json"
        if out_path.exists() and not args.overwrite:
            print(f"[skip] {value}: {out_path} exists (use --overwrite to refresh)")
            continue
        print(f"[scrape] {value} ({label})")
        payload = scrape_disease(
            session,
            database=database,
            disease_value=value,
            disease_label=label,
            delay=args.delay,
            max_pages=args.max_pages,
            debug_dir=debug_dir,
        )
        atomic_write_json(out_path, payload)
        summaries.append(
            (value, payload["result_count"], payload["filtered_out"], payload["page_count"])
        )
        print(
            f"  -> {payload['result_count']} kept, "
            f"{payload['filtered_out']} filtered, "
            f"{payload['page_count']} pages, "
            f"{payload['elapsed_seconds']}s"
        )

    total_elapsed = round(time.monotonic() - overall_started, 2)
    if summaries:
        kept_total = sum(s[1] for s in summaries)
        filt_total = sum(s[2] for s in summaries)
        print(
            f"\nDone. {len(summaries)} diseases scraped, "
            f"{kept_total} rows kept, {filt_total} filtered, "
            f"{total_elapsed}s total."
        )
    else:
        print("\nNothing scraped (all outputs already present?).", file=sys.stderr)


if __name__ == "__main__":
    main()
