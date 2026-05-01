#!/usr/bin/env python3
"""
Fetch https://www.endjunkfees.com/ and extract the campaign's state-by-state junk-fee
legislation blurbs (active bills + enacted / scheduled policy lines).

Writes dashboard/campaign_snapshot.json for the static UI. Stdlib only; no API key.

The site is Squarespace HTML without a public JSON feed; this parser targets the current
centered <p> blocks. If the site layout changes, update the selectors or fall back to
manually editing campaign_snapshot.json.

Usage:
  python sync_endjunkfees_snapshot.py
  python sync_endjunkfees_snapshot.py --from-html path/to/saved.html   # offline / tests
  python sync_endjunkfees_snapshot.py --stdout                       # print JSON, no write

Env:
  ENDJUNKFEES_URL   default https://www.endjunkfees.com/
  DASHBOARD_DIR     same as tracker.py (default: ./dashboard next to this file)
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "junk-fee-legislation-tracker/1.0 (+https://www.endjunkfees.com/)"
DEFAULT_URL = "https://www.endjunkfees.com/"
P_PARAGRAPH = re.compile(
    r'<p\s+style="text-align:center;white-space:pre-wrap;"[^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
STATE_LEAD = re.compile(
    r'<strong>\s*([^:<]+):\s*</strong>',
    re.IGNORECASE,
)
AHREF = re.compile(r'<a\s+href="([^"]+)"', re.IGNORECASE)


def _strip_tags(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = html_module.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _anchor_bill_pairs(inner: str) -> list[tuple[str, str]]:
    """Each <a href=...>...</a> becomes one or more (url, label) from <em> text inside the anchor."""
    out: list[tuple[str, str]] = []
    for m in AHREF.finditer(inner):
        start = m.start()
        url = html_module.unescape(m.group(1).strip())
        end_a = inner.find("</a>", m.end())
        if end_a < 0:
            continue
        chunk = inner[start : end_a + 4]
        for em in re.finditer(r"<em>([^<]*)</em>", chunk, re.IGNORECASE):
            label = html_module.unescape(em.group(1)).strip()
            if label:
                out.append((url, label))
    return out


def _parse_active_row(inner: str) -> dict[str, Any] | None:
    if "<a " not in inner.lower():
        return None
    sm = STATE_LEAD.search(inner)
    if not sm:
        return None
    state = sm.group(1).strip()
    bills: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for url, label in _anchor_bill_pairs(inner):
        key = (url, label)
        if key in seen:
            continue
        seen.add(key)
        bills.append({"label": label, "url": url})
    if not bills:
        return None
    return {"state": state, "bills": bills}


def _parse_enacted_row(inner: str) -> str | None:
    if re.search(r"<a\s+", inner, re.IGNORECASE):
        return None
    text = _strip_tags(inner)
    if len(text) < 30:
        return None
    low = text.lower()
    if "active legislation" in low:
        return None
    if "approved" not in low and "implemented" not in low:
        return None
    return text


def parse_endjunkfees_html(page_html: str) -> dict[str, Any]:
    active: list[dict[str, Any]] = []
    enacted: list[str] = []
    for m in P_PARAGRAPH.finditer(page_html):
        inner = m.group(1).strip()
        if not inner or inner.startswith("<span"):
            continue
        if "Active Legislation" in _strip_tags(inner):
            continue
        ar = _parse_active_row(inner)
        if ar:
            active.append(ar)
            continue
        en = _parse_enacted_row(inner)
        if en:
            enacted.append(en)
    return {"active_legislation": active, "enacted_or_effective": enacted}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync End Junk Fees homepage into campaign_snapshot.json")
    ap.add_argument("--from-html", type=Path, help="Parse a saved HTML file instead of fetching")
    ap.add_argument("--stdout", action="store_true", help="Print JSON to stdout; do not write file")
    args = ap.parse_args()

    url = (os.environ.get("ENDJUNKFEES_URL") or DEFAULT_URL).strip() or DEFAULT_URL
    err: str | None = None
    page = ""
    try:
        if args.from_html:
            page = args.from_html.read_text(encoding="utf-8", errors="replace")
        else:
            page = fetch_html(url)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        err = str(e)

    if err or not page.strip():
        payload: dict[str, Any] = {
            "source_url": url,
            "fetched_at": None,
            "fetch_error": err or "empty response",
            "active_legislation": [],
            "enacted_or_effective": [],
        }
    else:
        parsed = parse_endjunkfees_html(page)
        payload = {
            "source_url": url,
            "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "fetch_error": None,
            **parsed,
        }

    text = json.dumps(payload, indent=2) + "\n"
    if args.stdout:
        sys.stdout.write(text)
        return

    dash_raw = os.environ.get("DASHBOARD_DIR", "").strip()
    dash_dir = Path(dash_raw).expanduser() if dash_raw else Path(__file__).resolve().parent / "dashboard"
    dash_dir.mkdir(parents=True, exist_ok=True)
    out = dash_dir / "campaign_snapshot.json"
    out.write_text(text, encoding="utf-8")
    print(f"Wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
