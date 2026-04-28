"""Step 1 — fetch the PSMS landing page and persist the disease dropdown."""

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
    step_dir,
    utcnow_iso,
)


def run(*, delay: float = 0.5) -> dict:
    session = build_session()
    response = get(session, BASE_URL, delay=delay)
    html = response.text
    diseases = parse_select_options(html, "disease")
    database = extract_default_database(html)
    payload = {
        "source_url": BASE_URL,
        "fetched_at": utcnow_iso(),
        "database": database,
        "disease_count": len(diseases),
        "diseases": diseases,
    }
    out_path = step_dir(1) / "diseases.json"
    atomic_write_json(out_path, payload)
    print(f"Wrote {len(diseases)} diseases to {out_path}")
    if database:
        print(f"  database token: {database}")
    else:
        print("  WARNING: could not detect default database token from page", file=sys.stderr)
    return payload


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
