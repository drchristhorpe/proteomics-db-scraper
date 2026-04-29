"""Step 2 — for each disease, paginate the search and persist Class-I HLA rows.

Loads the disease list from ``outputs/step_1/diseases.json`` and walks the
PSMS search endpoint with ``mhc_class=I`` for each disease, keeping only rows
whose ``best_hla_allele`` belongs to a Class I locus (HLA-A/B/C/E/F/G).
Writes one JSON file per disease into ``outputs/step_2/<disease_slug>.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from requests.exceptions import HTTPError, RequestException

from .common import (
    BASE_URL,
    HLA_CLASS_I_LOCI,
    atomic_write_json,
    build_session,
    find_allele_in_row,
    hla_locus,
    is_class_i_allele,
    parse_pagination,
    parse_results_table,
    slugify_hla,
    slugify_text,
    step_dir,
    utcnow_iso,
)


class ScrapeAborted(RuntimeError):
    """Raised when a disease scrape can't continue (e.g. upstream 5xx)."""

ALLELE_COLUMN_CANDIDATES: tuple[str, ...] = (
    "best_hla_allele",
    "hla_allele",
    "best_allele",
    "allele",
)

HLA_CLASS_I_LOCUS_SLUGS: tuple[str, ...] = tuple(
    f"hla_{locus.lower()}" for locus in HLA_CLASS_I_LOCI
)

MHC_CLASS_SLUG = "class_i"

# Retry strategy for upstream 5xx errors.
RETRY_ATTEMPTS = 5  # total attempts past the first failure
RETRY_INITIAL_WAIT = 1.0  # seconds; doubles after each failed attempt
SLOW_DELAY_MULTIPLIER = 5.0  # multiply --delay by this when server appears stressed

_UNNAMED_COL_RE = re.compile(r"^col(_\d+)?$")
_BLANK_PLACEHOLDERS: frozenset[str] = frozenset({"", "-", "—", "–"})


class _PageSkipped(RuntimeError):
    """Internal: signal that a page was skipped after 5xx retries exhausted."""


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


def _split_uniprot(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    s = value.strip()
    if s in _BLANK_PLACEHOLDERS:
        return []
    return [part.strip() for part in s.split("|") if part.strip()]


def _parse_modifications(value: Any) -> list[Any]:
    """Upstream emits either a blank placeholder or a JSON array literal."""
    if not isinstance(value, str):
        return []
    s = value.strip()
    if s in _BLANK_PLACEHOLDERS:
        return []
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return [s]
    return parsed if isinstance(parsed, list) else [parsed]


def _locus_slug(allele: str | None) -> str | None:
    locus = hla_locus(allele)
    return f"hla_{locus.lower()}" if locus else None


def _enrich_row(row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    allele = find_allele_in_row(row, ALLELE_COLUMN_CANDIDATES)
    enriched = {k: v for k, v in row.items() if not _UNNAMED_COL_RE.match(k)}
    enriched.pop("best_hla_allele", None)
    if "uniprot_ids" in enriched:
        enriched["uniprot_ids"] = _split_uniprot(enriched["uniprot_ids"])
    if "peptide_modifications" in enriched:
        enriched["peptide_modifications"] = _parse_modifications(
            enriched["peptide_modifications"]
        )
    if "tissue" in enriched:
        tissue = enriched["tissue"]
        if isinstance(tissue, str) and tissue.strip():
            enriched["tissue"] = slugify_text(tissue)
    if "mhc_class" in enriched:
        enriched["mhc_class"] = MHC_CLASS_SLUG
    enriched["hla_locus"] = _locus_slug(allele)
    enriched["hla_slug"] = slugify_hla(allele)
    return enriched, allele


def _tmp_dir() -> Path:
    return step_dir(2) / "_tmp"


def _today_dir() -> Path:
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    return _tmp_dir() / today


def _find_cached_page(disease_value: str, page: int) -> Path | None:
    """Search across all date folders for any cached version of this page;
    most recent (lexicographic on YYYY_MM_DD) wins."""
    matches = sorted(_tmp_dir().glob(f"*/{disease_value}_page{page}.html"))
    return matches[-1] if matches else None


def _new_cache_path(disease_value: str, page: int) -> Path:
    return _today_dir() / f"{disease_value}_page{page}.html"


def _clear_cache_for(disease_value: str) -> int:
    removed = 0
    for path in _tmp_dir().glob(f"*/{disease_value}_page*.html"):
        path.unlink()
        removed += 1
    return removed


def _attempt_fetch(
    session,
    *,
    params: dict[str, str],
    disease_value: str,
    page: int,
) -> tuple[str, bool]:
    """Fetch one page with exponential backoff on 5xx.

    Returns (html, used_retry). Raises _PageSkipped after RETRY_ATTEMPTS
    consecutive 5xx responses. Raises ScrapeAborted on non-5xx HTTP errors
    or any other RequestException.
    """
    used_retry = False
    wait = RETRY_INITIAL_WAIT
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            response = session.get(BASE_URL, params=params, timeout=30.0)
            response.raise_for_status()
            return response.text, used_retry
        except HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 500 <= status < 600:
                if attempt < RETRY_ATTEMPTS:
                    used_retry = True
                    print(
                        f"  [retry] {disease_value} page {page}: HTTP {status}; "
                        f"sleeping {wait:.1f}s before retry {attempt + 2}/{RETRY_ATTEMPTS + 1}",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    wait *= 2
                    continue
                print(
                    f"  [skip] {disease_value} page {page}: HTTP {status} persisted "
                    f"after {RETRY_ATTEMPTS} retries; recording gap and continuing",
                    file=sys.stderr,
                )
                raise _PageSkipped() from exc
            raise ScrapeAborted(
                f"{disease_value} page {page}: upstream returned HTTP {status} "
                f"(cache preserved at {_tmp_dir()}; re-run to resume)"
            ) from exc
        except RequestException as exc:
            raise ScrapeAborted(
                f"{disease_value} page {page}: request failed "
                f"({exc.__class__.__name__}: {exc}) "
                f"(cache preserved at {_tmp_dir()}; re-run to resume)"
            ) from exc
    raise AssertionError("unreachable")  # loop always returns or raises


def scrape_disease(
    session,
    *,
    database: str | None,
    disease_value: str,
    disease_label: str,
    disease_query: str,
    delay: float,
    max_pages: int | None,
) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    skipped_pages: list[int] = []
    page = 1
    pages_seen = 0
    total_results_seen: int | None = None
    started = time.monotonic()

    fast_delay = delay
    slow_delay = delay * SLOW_DELAY_MULTIPLIER
    current_delay = fast_delay

    while True:
        if max_pages is not None and page > max_pages:
            break
        cached = _find_cached_page(disease_value, page)
        if cached is not None:
            print(f"  [cache] {cached.name}")
            html = cached.read_text(encoding="utf-8")
        else:
            params = _build_params(database=database, disease=disease_query, page=page)
            try:
                html, used_retry = _attempt_fetch(
                    session,
                    params=params,
                    disease_value=disease_value,
                    page=page,
                )
            except _PageSkipped:
                skipped_pages.append(page)
                if current_delay != slow_delay:
                    print(f"  [slow] dropping to {slow_delay:.2f}s/page", file=sys.stderr)
                current_delay = slow_delay
                if current_delay:
                    time.sleep(current_delay)
                page += 1
                continue
            out_path = _new_cache_path(disease_value, page)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html, encoding="utf-8")
            if used_retry:
                if current_delay != slow_delay:
                    print(f"  [slow] dropping to {slow_delay:.2f}s/page", file=sys.stderr)
                current_delay = slow_delay
            else:
                if current_delay != fast_delay:
                    print(f"  [fast] returning to {fast_delay:.2f}s/page", file=sys.stderr)
                current_delay = fast_delay
            if current_delay:
                time.sleep(current_delay)
        pages_seen += 1

        rows = parse_results_table(html)
        if not rows:
            break

        for row in rows:
            enriched, allele = _enrich_row(row)
            if is_class_i_allele(allele):
                kept.append(enriched)

        pg = parse_pagination(html)
        if pg.get("total_results") is not None:
            total_results_seen = pg["total_results"]

        page += 1

    by_locus = Counter(
        row["hla_locus"] for row in kept if row.get("hla_locus") in HLA_CLASS_I_LOCUS_SLUGS
    )
    elapsed = round(time.monotonic() - started, 2)

    return {
        "disease": {
            "value": disease_value,
            "label": disease_label,
            "query_value": disease_query,
        },
        "mhc_class": MHC_CLASS_SLUG,
        "database": database,
        "fetched_at": utcnow_iso(),
        "page_count": pages_seen,
        "skipped_pages": skipped_pages,
        "site_total_results": total_results_seen,
        "by_locus": {locus: by_locus[locus] for locus in HLA_CLASS_I_LOCUS_SLUGS if by_locus[locus]},
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
    _tmp_dir().mkdir(parents=True, exist_ok=True)
    session = build_session()

    overall_started = time.monotonic()
    summaries: list[tuple[str, int, int]] = []
    failures: list[str] = []

    for disease in diseases:
        value = disease["value"]
        label = disease.get("label", value)
        query_value = disease.get("query_value", value)
        out_path = out_dir / f"{value}.json"
        if out_path.exists() and not args.overwrite:
            print(f"[skip] {value}: {out_path} exists (use --overwrite to refresh)")
            continue
        print(f"[scrape] {value} ({label})")
        try:
            payload = scrape_disease(
                session,
                database=database,
                disease_value=value,
                disease_label=label,
                disease_query=query_value,
                delay=args.delay,
                max_pages=args.max_pages,
            )
        except ScrapeAborted as exc:
            print(f"[error] {exc}", file=sys.stderr)
            failures.append(value)
            continue
        atomic_write_json(out_path, payload)
        kept = len(payload["results"])
        skipped_n = len(payload.get("skipped_pages") or [])
        summaries.append((value, kept, payload["page_count"]))
        skip_note = f", {skipped_n} skipped" if skipped_n else ""
        print(
            f"  -> {kept} kept, "
            f"{payload['page_count']} pages{skip_note}, "
            f"{payload['elapsed_seconds']}s"
        )

    total_elapsed = round(time.monotonic() - overall_started, 2)
    if summaries:
        kept_total = sum(s[1] for s in summaries)
        print(
            f"\nDone. {len(summaries)} diseases scraped, "
            f"{kept_total} rows kept, "
            f"{total_elapsed}s total."
        )
    elif not failures:
        print("\nNothing scraped (all outputs already present?).", file=sys.stderr)
    if failures:
        print(
            f"\n{len(failures)} disease(s) aborted: {', '.join(failures)}. "
            f"Re-run to resume from cached pages.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
