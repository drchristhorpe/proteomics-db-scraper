"""Step 1 — fetch the PSMS landing page and persist dropdown options.

Writes ``outputs/step_1/diseases.json`` (from the ``disease`` select) and
``outputs/step_1/tissues.json`` (from the ``biological_material`` select).
"""

from __future__ import annotations

import argparse
import sys

from .common import (
    BASE_URL,
    atomic_write_json,
    build_session,
    extract_default_database,
    get,
    parse_select_options,
    slugify_text,
    step_dir,
    utcnow_iso,
)


def _cap_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _humanize_shouty(s: str) -> str:
    """Lowercase a SHOUTY_SNAKE_CASE label and capitalise the first letter."""
    return _cap_first(s.lower().replace("_", " "))


def _shape_option(opt: dict, label_fn) -> dict:
    raw_value = opt["value"]
    raw_label = opt.get("label") or raw_value
    return {
        "value": slugify_text(raw_value),
        "label": label_fn(raw_label),
        "query_value": raw_value,
    }


def run(*, delay: float = 0.5) -> dict:
    session = build_session()
    response = get(session, BASE_URL, delay=delay)
    html = response.text
    diseases = [
        _shape_option(o, _cap_first)
        for o in parse_select_options(html, "disease")
    ]
    tissues = [
        _shape_option(o, _humanize_shouty)
        for o in parse_select_options(html, "biological_material")
    ]
    database = extract_default_database(html)
    fetched_at = utcnow_iso()

    diseases_payload = {
        "source_url": BASE_URL,
        "fetched_at": fetched_at,
        "database": database,
        "disease_count": len(diseases),
        "diseases": diseases,
    }
    tissues_payload = {
        "source_url": BASE_URL,
        "fetched_at": fetched_at,
        "database": database,
        "tissue_count": len(tissues),
        "tissues": tissues,
    }

    out_dir = step_dir(1)
    diseases_path = out_dir / "diseases.json"
    tissues_path = out_dir / "tissues.json"
    atomic_write_json(diseases_path, diseases_payload)
    atomic_write_json(tissues_path, tissues_payload)
    print(f"Wrote {len(diseases)} diseases to {diseases_path}")
    print(f"Wrote {len(tissues)} tissues to {tissues_path}")
    if database:
        print(f"  database token: {database}")
    else:
        print("  WARNING: could not detect default database token from page", file=sys.stderr)
    return {"diseases": diseases_payload, "tissues": tissues_payload}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to sleep after each HTTP request (default: 0.5)",
    )
    args = parser.parse_args()
    run(delay=args.delay)


if __name__ == "__main__":
    main()
