#!/usr/bin/env python3
"""
One-time export: poll Open States once and write JSON + CSV (no SQLite, no Slack, no dashboard).

Uses the same env vars as tracker.py (OPENSTATES_API_KEY, SEARCH_QUERIES, JURISDICTIONS, WATCH_BILLS).

Usage:
  set -a && source .env && set +a
  python export_state_snapshot.py
  python export_state_snapshot.py --output-dir ./exports
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import tracker as t


def _rows_from_bills(bills: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bill_id, b in bills.items():
        ident = b.get("identifier") or ""
        title = b.get("title") or ""
        jur = (b.get("jurisdiction") or {}).get("name") or ""
        sess = b.get("session") or ""
        url = b.get("openstates_url")
        actions = list(b.get("actions") or [])
        milestones = t.derive_milestones(actions)
        rows.append(
            {
                "bill_id": bill_id,
                "identifier": ident,
                "title": title,
                "jurisdiction": jur,
                "session": sess,
                "url": url,
                "updated_at": b.get("updated_at"),
                "stage": t.stage_from_milestones(milestones),
                "milestones": milestones,
            }
        )
    rows.sort(key=lambda r: (r["jurisdiction"].lower(), (r["identifier"] or "").lower()))
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "jurisdiction",
        "identifier",
        "title",
        "session",
        "stage",
        "introduction",
        "lower_passage",
        "upper_passage",
        "signed_or_law",
        "url",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            m = r.get("milestones") or {}
            w.writerow(
                {
                    "jurisdiction": r.get("jurisdiction", ""),
                    "identifier": r.get("identifier", ""),
                    "title": r.get("title", ""),
                    "session": r.get("session", ""),
                    "stage": r.get("stage", ""),
                    "introduction": m.get("introduction") or "",
                    "lower_passage": m.get("lower_passage") or "",
                    "upper_passage": m.get("upper_passage") or "",
                    "signed_or_law": m.get("signed_or_law") or "",
                    "url": r.get("url") or "",
                }
            )


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time Open States export to JSON + CSV.")
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "exports",
        help="Directory for state_snapshot.json and state_snapshot.csv (default: ./exports)",
    )
    args = ap.parse_args()

    api_key = os.environ.get("OPENSTATES_API_KEY", "").strip()
    if not api_key:
        print("Set OPENSTATES_API_KEY (e.g. from .env). See env.example.", file=sys.stderr)
        sys.exit(1)

    queries = t._env_queries()
    jurisdictions = t._env_jurisdictions()
    watch = t._env_watch_bills()

    print("Fetching bills from Open States (this may take a minute)…", file=sys.stderr)
    bills = t.collect_unique_bills(api_key, queries, jurisdictions)

    page_delay, _ = t._request_pacing()
    for idx, (j, sess, ident) in enumerate(watch):
        if idx:
            time.sleep(page_delay)
        try:
            detail = t.fetch_bill_detail(api_key, j, sess, ident)
            bid = detail.get("id")
            if bid:
                bills[bid] = detail
        except urllib.error.HTTPError as e:
            print(f"Warning: could not load WATCH_BILLS {j}:{sess}:{ident}: {e}", file=sys.stderr)

    rows = _rows_from_bills(bills)
    payload: dict[str, Any] = {
        "generated_at": t._utc_now_iso(),
        "source": "Open States API v3 (one-time export; no local database)",
        "bill_count": len(rows),
        "bills": rows,
    }

    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "state_snapshot.json"
    csv_path = out_dir / "state_snapshot.csv"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _write_csv(csv_path, rows)
    print(f"Wrote {json_path}", file=sys.stderr)
    print(f"Wrote {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
