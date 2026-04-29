"""Shared helpers for the PCI-DB PSMS scraper pipeline."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://pci-db.org/psms/"

HLA_CLASS_I_LOCI: tuple[str, ...] = ("A", "B", "C", "E", "F", "G")

OUTPUT_ROOT = Path("outputs")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def step_dir(n: int) -> Path:
    path = OUTPUT_ROOT / f"step_{n}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429,),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    delay: float = 0.5,
    timeout: float = 30.0,
) -> requests.Response:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    if delay:
        time.sleep(delay)
    return response


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify_text(text: str) -> str:
    s = _NON_ALNUM.sub("_", text.strip().lower()).strip("_")
    return s or "unknown"


_HLA_PREFIX = re.compile(r"^hla[-_]?", re.IGNORECASE)


def slugify_hla(allele: str | None) -> str | None:
    """Turn an HLA allele label like ``HLA-A*02:01`` into ``hla_a_02_01``."""
    if not allele:
        return None
    body = _HLA_PREFIX.sub("", allele.strip())
    if not body:
        return None
    return "hla_" + slugify_text(body)


def hla_locus(allele: str | None) -> str | None:
    """Return the single-letter Class I locus (A/B/C/E/F/G) or None."""
    if not allele:
        return None
    body = _HLA_PREFIX.sub("", allele.strip())
    if not body:
        return None
    first = body[0].upper()
    return first if first.isalpha() else None


def is_class_i_allele(allele: str | None) -> bool:
    return hla_locus(allele) in HLA_CLASS_I_LOCI


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def parse_select_options(html: str, name: str) -> list[dict[str, Any]]:
    soup = _make_soup(html)
    select = soup.find("select", attrs={"name": name})
    if select is None:
        return []
    options: list[dict[str, Any]] = []
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        if not value:
            continue
        options.append(
            {
                "value": value,
                "label": opt.get_text(strip=True),
                "selected": opt.has_attr("selected"),
            }
        )
    return options


def extract_default_database(html: str) -> str | None:
    soup = _make_soup(html)
    select = soup.find("select", attrs={"name": "database"})
    if select is None:
        return None
    selected = select.find("option", selected=True)
    if selected is None:
        selected = select.find("option")
    if selected is None:
        return None
    value = (selected.get("value") or "").strip()
    return value or None


def _header_keys(table) -> list[str]:
    head = table.find("thead")
    if head is not None:
        cells = head.find_all(["th", "td"])
    else:
        first_row = table.find("tr")
        cells = first_row.find_all(["th", "td"]) if first_row else []
    keys: list[str] = []
    seen: dict[str, int] = {}
    for cell in cells:
        raw = cell.get_text(" ", strip=True)
        key = slugify_text(raw) if raw else "col"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 0
        keys.append(key)
    return keys


def _cell_value(cell) -> str:
    return cell.get_text(" ", strip=True)


def parse_results_table(html: str) -> list[dict[str, Any]]:
    """Parse the search results table.

    Heuristic: pick the largest data table on the page that has a header
    row with at least 3 cells. Emits one dict per body row keyed by
    snake_cased header text. Each value is ``{"text": ..., "href"?: ...}``
    so callers can keep links (e.g. UniProt) without re-parsing.
    """
    soup = _make_soup(html)
    candidates = []
    for table in soup.find_all("table"):
        keys = _header_keys(table)
        if len(keys) < 3:
            continue
        body = table.find("tbody") or table
        rows = body.find_all("tr")
        body_rows = [r for r in rows if r.find_all("td")]
        if not body_rows:
            continue
        candidates.append((len(body_rows), len(keys), table, keys, body_rows))
    if not candidates:
        return []
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, _, keys, body_rows = candidates[0]
    results: list[dict[str, Any]] = []
    for tr in body_rows:
        cells = tr.find_all("td")
        if not cells:
            continue
        row: dict[str, Any] = {}
        for idx, cell in enumerate(cells):
            key = keys[idx] if idx < len(keys) else f"col_{idx}"
            row[key] = _cell_value(cell)
        results.append(row)
    return results


_PAGE_OF_RE = re.compile(r"page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")


def parse_pagination(html: str) -> dict[str, int | None]:
    """Best-effort pagination parser.

    Returns ``{"current_page", "total_pages", "total_results"}``. Any value
    that cannot be inferred is ``None`` and the caller falls back to the
    "no rows returned" stop signal.
    """
    soup = _make_soup(html)
    text = soup.get_text(" ", strip=True)

    current_page: int | None = None
    total_pages: int | None = None
    total_results: int | None = None

    m = _PAGE_OF_RE.search(text)
    if m:
        current_page = int(m.group(1))
        total_pages = int(m.group(2))

    pagination = soup.find(class_=re.compile(r"pagination", re.IGNORECASE))
    if pagination is not None:
        active = pagination.find(class_=re.compile(r"active|current", re.IGNORECASE))
        if active is not None:
            digits = _DIGITS_RE.search(active.get_text(" ", strip=True) or "")
            if digits:
                current_page = int(digits.group(0))
        page_numbers: list[int] = []
        for a in pagination.find_all(["a", "span", "li"]):
            txt = a.get_text(" ", strip=True)
            if txt and txt.isdigit():
                page_numbers.append(int(txt))
        if page_numbers:
            total_pages = max(total_pages or 0, max(page_numbers))

    m_total = re.search(r"(\d+)\s+(?:results?|entries|hits|matches)", text, re.IGNORECASE)
    if m_total:
        total_results = int(m_total.group(1))

    return {
        "current_page": current_page,
        "total_pages": total_pages,
        "total_results": total_results,
    }


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def coalesce_text(value: Any) -> str | None:
    """Pull a plain string out of a parsed cell dict, or return as-is."""
    if value is None:
        return None
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) and text else None
    if isinstance(value, str):
        return value or None
    return str(value)


def find_allele_in_row(row: dict[str, Any], candidate_keys: Iterable[str]) -> str | None:
    """Find the best_hla_allele value inside a parsed row.

    Tries the provided keys first, then falls back to any key that contains
    "hla" and "allele". This keeps the parser tolerant of column-name drift.
    """
    for key in candidate_keys:
        if key in row:
            text = coalesce_text(row[key])
            if text:
                return text
    for key, value in row.items():
        k = key.lower()
        if "hla" in k and "allele" in k:
            text = coalesce_text(value)
            if text:
                return text
    return None
