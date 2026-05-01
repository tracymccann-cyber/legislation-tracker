#!/usr/bin/env python3
"""
Track state bills (junk-fee / pricing transparency style) via Open States API v3.

Alerts when:
  1) A matching bill is newly discovered by this tracker (search hit you have not stored yet).
  2) A bill you already track gets a new introduction or filing action (Open States classifications only;
     not emitted on first ingest of that bill, so you are not spammed with stale filing dates).
  3) A bill receives a new chamber passage action (upper or lower), per Open States action classification.
  4) A bill receives a new executive-signature or became-law action.

The first run for a given bill records its existing action history quietly for milestones (2–4); use
--bootstrap to silence the "new bill" lines as well while you seed the database.

Search hits are narrowed by default: a bill is kept only if its title contains a phrase from
BILL_TOPIC_KEYWORDS (see env.example). That avoids unrelated bills whose text matched the search API.

Optional: set DASHBOARD_ONLY_WITH_MILESTONES=1 so data.json lists only bills with at least one milestone
date (introduced, chamber passage, or signed/law) derived from Open States action classifications.

endjunkfees.com has no public API; this tool uses Open States for bill tracking. For the campaign’s
state-by-state list (active 2026 bills plus enacted lines), run `python sync_endjunkfees_snapshot.py`
to refresh `dashboard/campaign_snapshot.json` (run locally when you want the campaign panel updated).

Usage:
  export OPENSTATES_API_KEY=...
  # If you load vars with `set -a && . ./.env && set +a`, quote any value that contains spaces
  # or shell metacharacters (see env.example: SEARCH_QUERIES='...').
  python tracker.py              # one poll; use cron for periodic checks
  python tracker.py --bootstrap  # seed state, no alerts

After each run, by default, dashboard/data.json is refreshed for the static UI in dashboard/
(open index.html via a local web server, publish that folder to GitHub Pages, or deploy the
dashboard/ folder on Vercel with Root Directory set to dashboard).
Set DASHBOARD_DIR or pass --no-dashboard to skip.

Hosting: run this script locally (see QUICKSTART.txt). GitHub Actions only deploys the static
dashboard/ folder — see .github/workflows/deploy-dashboard.yml (no Open States API in CI).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode

API = "https://v3.openstates.org"
# Fewer default phrases = fewer API calls (helps avoid 429). Add more in SEARCH_QUERIES if needed.
DEFAULT_QUERIES = ("junk fee", "drip pricing", "deceptive pricing")
STATE_DB = Path(__file__).resolve().parent / "tracker_state.db"


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    body: str
    url: str | None


def _env_queries() -> tuple[str, ...]:
    raw = os.environ.get("SEARCH_QUERIES", "").strip()
    if not raw:
        return DEFAULT_QUERIES
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return DEFAULT_QUERIES
    # Preserve order, drop duplicates (fewer API round-trips).
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def _env_jurisdictions() -> tuple[str, ...] | None:
    raw = os.environ.get("JURISDICTIONS", "").strip()
    if not raw:
        return None
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _env_watch_bills() -> tuple[tuple[str, str, str], ...]:
    raw = os.environ.get("WATCH_BILLS", "").strip()
    if not raw:
        return ()
    out: list[tuple[str, str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3:
            raise SystemExit(f"Invalid WATCH_BILLS entry {part!r}; use jurisdiction:session:identifier")
        j, sess, ident = bits[0].strip().lower(), bits[1].strip(), bits[2].strip()
        out.append((j, sess, ident))
    return tuple(out)


DEFAULT_TOPIC_TITLE_NEEDLES = (
    "junk fee",
    "junk fees",
    "junk-fee",
    "junkfees",
    "drip pricing",
    "deceptive pricing",
    "hidden fee",
    "surprise fee",
    "pricing transparency",
)


def _env_topic_title_needles() -> tuple[str, ...]:
    raw = os.environ.get("BILL_TOPIC_KEYWORDS", "").strip()
    if not raw:
        return DEFAULT_TOPIC_TITLE_NEEDLES
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else DEFAULT_TOPIC_TITLE_NEEDLES


def _env_title_topic_filter_enabled() -> bool:
    raw = (os.environ.get("BILL_TOPIC_TITLE_FILTER") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def bill_title_matches_topic(bill: dict[str, Any]) -> bool:
    """True if the bill title contains at least one configured topic substring (case-insensitive)."""
    if not _env_title_topic_filter_enabled():
        return True
    title = (bill.get("title") or "").lower()
    if not title:
        return False
    return any(needle in title for needle in _env_topic_title_needles())


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bills (
          bill_id TEXT PRIMARY KEY,
          identifier TEXT NOT NULL,
          title TEXT NOT NULL,
          jurisdiction TEXT NOT NULL,
          session TEXT NOT NULL,
          url TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_actions (
          bill_id TEXT NOT NULL,
          action_id TEXT NOT NULL,
          PRIMARY KEY (bill_id, action_id)
        );
        """
    )
    conn.commit()


USER_AGENT = "junk-fee-legislation-tracker/1.0 (+https://openstates.org/)"

# Serialized pacing for every Open States HTTP GET (search + bill detail).
_last_openstates_monotonic: float = 0.0
# Total 429 responses since last successful Open States GET (fail fast instead of retrying for many minutes).
_openstates_429_since_ok: int = 0


def _openstates_abort_429_threshold() -> int:
    raw = (os.environ.get("OPENSTATES_ABORT_AFTER_429_COUNT") or "22").strip() or "22"
    try:
        n = int(raw)
    except ValueError:
        n = 22
    return max(8, min(n, 200))


def _openstates_note_success() -> None:
    global _openstates_429_since_ok
    _openstates_429_since_ok = 0


def _openstates_note_429_and_maybe_abort() -> None:
    global _openstates_429_since_ok
    _openstates_429_since_ok += 1
    cap = _openstates_abort_429_threshold()
    if _openstates_429_since_ok >= cap:
        raise SystemExit(
            f"Open States returned HTTP 429 (rate limited) {cap} times in a row without a successful "
            "response. Stop the job, wait 15–30 minutes, then try again with fewer SEARCH_QUERIES phrases, "
            "set JURISDICTIONS to narrow scope, raise OPENSTATES_MIN_INTERVAL_SEC (e.g. 2), and avoid "
            "running the tracker locally at the same time as GitHub Actions (same API key shares quota). "
            "See env.example."
        )


def _openstates_throttle() -> None:
    """Minimum gap between Open States requests to reduce 429s (set OPENSTATES_MIN_INTERVAL_SEC=0 to disable)."""
    raw = (os.environ.get("OPENSTATES_MIN_INTERVAL_SEC") or "1.0").strip() or "1.0"
    try:
        gap = float(raw)
    except ValueError:
        gap = 1.0
    if gap <= 0:
        return
    global _last_openstates_monotonic
    now = time.monotonic()
    wait = gap - (now - _last_openstates_monotonic)
    if wait > 0:
        time.sleep(wait)
    _last_openstates_monotonic = time.monotonic()


def _retry_wait_seconds(attempt: int, retry_after_header: str | None) -> float:
    raw_cap = (os.environ.get("OPENSTATES_429_WAIT_CAP_SEC") or "600").strip() or "600"
    try:
        cap = float(raw_cap)
    except ValueError:
        cap = 600.0
    cap = max(30.0, min(cap, 3600.0))
    if retry_after_header:
        try:
            return min(float(retry_after_header), cap)
        except ValueError:
            pass
    return min(2.0 * (2**attempt), cap)


def _openstates_max_retries() -> int:
    raw = (os.environ.get("OPENSTATES_MAX_RETRIES") or "14").strip() or "14"
    try:
        n = int(raw)
    except ValueError:
        n = 14
    return max(3, min(n, 40))


def http_get_json(url: str, max_retries: int | None = None) -> dict[str, Any]:
    if max_retries is None:
        max_retries = _openstates_max_retries()
    is_os = "openstates.org" in url
    if is_os:
        _openstates_throttle()
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if is_os:
                    _openstates_note_success()
                return data
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt >= max_retries:
                raise
            if is_os:
                _openstates_note_429_and_maybe_abort()
            wait = _retry_wait_seconds(attempt, e.headers.get("Retry-After"))
            print(f"Open States rate limit (429); waiting {wait:.0f}s then retrying...", file=sys.stderr)
            time.sleep(wait)
            if is_os:
                _openstates_throttle()


def http_post_json(url: str, payload: dict[str, Any], max_retries: int = 6) -> None:
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt >= max_retries:
                raise
            wait = _retry_wait_seconds(attempt, e.headers.get("Retry-After"))
            print(f"Slack webhook rate limited (429); waiting {wait:.0f}s...", file=sys.stderr)
            time.sleep(wait)


def search_bills(
    api_key: str,
    query: str,
    jurisdictions: tuple[str, ...] | None,
    page: int,
) -> dict[str, Any]:
    params: dict[str, str] = {
        "apikey": api_key,
        "q": query,
        "include": "actions",
        "per_page": "20",
        "page": str(page),
        "sort": "updated_desc",
    }
    if jurisdictions:
        pairs: list[tuple[str, str]] = [(k, v) for k, v in params.items()]
        for j in jurisdictions:
            pairs.append(("jurisdiction", j))
        qs = urlencode(pairs)
    else:
        qs = urlencode(params)
    return http_get_json(f"{API}/bills?{qs}")


def fetch_bill_detail(api_key: str, jurisdiction: str, session: str, identifier: str) -> dict[str, Any]:
    j = quote(jurisdiction, safe="")
    s = quote(session, safe="")
    i = quote(identifier, safe="")
    qs = urlencode({"apikey": api_key, "include": "actions"})
    return http_get_json(f"{API}/bills/{j}/{s}/{i}?{qs}")


def org_chamber(classification: str | None) -> str | None:
    if classification in ("upper", "lower"):
        return classification
    return None


def classify_action_alert(action: dict[str, Any]) -> str | None:
    classes = set(action.get("classification") or ())
    org = action.get("organization") or {}
    org_cls = org.get("classification")

    if "executive-signature" in classes or "became-law" in classes:
        return "signed_or_law"
    if "passage" in classes and org_chamber(org_cls):
        return "chamber_passage"
    if "introduction" in classes or "filing" in classes:
        return "introduction"
    return None


def send_slack(webhook: str, text: str) -> None:
    http_post_json(webhook, {"text": text})


def format_alert(a: Alert) -> str:
    lines = [f"[{a.kind}] {a.title}", a.body]
    if a.url:
        lines.append(a.url)
    return "\n".join(lines)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _max_date(prev: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return prev
    if not prev:
        return candidate
    return candidate if candidate >= prev else prev


def derive_milestones(actions: Iterable[dict[str, Any]]) -> dict[str, str | None]:
    ordered = sorted(
        (a for a in actions if a.get("date")),
        key=lambda a: (a.get("date") or "", a.get("order") or 0),
    )
    intro: str | None = None
    lower: str | None = None
    upper: str | None = None
    signed: str | None = None
    for a in ordered:
        d = a.get("date")
        if not d:
            continue
        classes = set(a.get("classification") or ())
        org = (a.get("organization") or {}).get("classification")
        if "introduction" in classes or "filing" in classes:
            intro = intro or d
        if "passage" in classes and org == "lower":
            lower = _max_date(lower, str(d))
        if "passage" in classes and org == "upper":
            upper = _max_date(upper, str(d))
        if "executive-signature" in classes or "became-law" in classes:
            signed = _max_date(signed, str(d))
    return {
        "introduction": intro,
        "lower_passage": lower,
        "upper_passage": upper,
        "signed_or_law": signed,
    }


def stage_from_milestones(m: dict[str, str | None]) -> str:
    if m.get("signed_or_law"):
        return "law"
    if m.get("lower_passage") or m.get("upper_passage"):
        return "chamber"
    if m.get("introduction"):
        return "introduced"
    return "unknown"


def _env_dashboard_only_with_milestones() -> bool:
    raw = (os.environ.get("DASHBOARD_ONLY_WITH_MILESTONES") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def build_dashboard_payload(
    conn: sqlite3.Connection,
    bills: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not bills:
        return {"generated_at": _utc_now_iso(), "source": "Open States API v3", "bills": []}

    ids = tuple(bills.keys())
    placeholders = ",".join("?" * len(ids))
    created: dict[str, str] = {}
    cur = conn.execute(f"SELECT bill_id, created_at FROM bills WHERE bill_id IN ({placeholders})", ids)
    for bid, ts in cur.fetchall():
        created[str(bid)] = str(ts)

    rows: list[dict[str, Any]] = []
    skipped_no_progress = 0
    only_ms = _env_dashboard_only_with_milestones()
    for bill_id, b in bills.items():
        ident = b.get("identifier") or ""
        title = b.get("title") or ""
        jur = (b.get("jurisdiction") or {}).get("name") or ""
        sess = b.get("session") or ""
        url = b.get("openstates_url")
        actions = list(b.get("actions") or [])
        milestones = derive_milestones(actions)
        if only_ms and not any(
            milestones.get(k) for k in ("introduction", "lower_passage", "upper_passage", "signed_or_law")
        ):
            skipped_no_progress += 1
            continue
        rows.append(
            {
                "bill_id": bill_id,
                "identifier": ident,
                "title": title,
                "jurisdiction": jur,
                "session": sess,
                "url": url,
                "tracked_since": created.get(bill_id),
                "updated_at": b.get("updated_at"),
                "stage": stage_from_milestones(milestones),
                "milestones": milestones,
            }
        )
    if only_ms and skipped_no_progress:
        print(
            f"Dashboard: omitted {skipped_no_progress} bill(s) with no dated milestones "
            "(DASHBOARD_ONLY_WITH_MILESTONES=1).",
            file=sys.stderr,
        )
    rows.sort(key=lambda r: (r["jurisdiction"].lower(), (r["identifier"] or "").lower()))
    return {"generated_at": _utc_now_iso(), "source": "Open States API v3", "bills": rows}


def write_dashboard(out_dir: Path, payload: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "data.json"
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def _request_pacing() -> tuple[float, float]:
    """(delay between paginated bill search requests, gap before starting a new query phrase)."""

    def _f(name: str, default: str) -> float:
        raw = (os.environ.get(name) or default).strip() or default
        try:
            return max(0.0, float(raw))
        except ValueError:
            return max(0.0, float(default))

    return (_f("OPENSTATES_PAGE_DELAY_SEC", "2.5"), _f("OPENSTATES_QUERY_GAP_SEC", "6.0"))


def collect_unique_bills(
    api_key: str,
    queries: tuple[str, ...],
    jurisdictions: tuple[str, ...] | None,
) -> dict[str, dict[str, Any]]:
    page_delay, query_gap = _request_pacing()
    by_id: dict[str, dict[str, Any]] = {}
    scope = "all jurisdictions" if not jurisdictions else ", ".join(jurisdictions)
    print(
        f"Open States: {len(queries)} search phrase(s), {scope}. "
        f"There are intentional pauses (~{query_gap:.0f}s between phrases, ~{page_delay:.0f}s between pages); "
        "this is normal.",
        file=sys.stderr,
    )
    if _env_title_topic_filter_enabled():
        needles = _env_topic_title_needles()
        print(
            f"Title topic filter ON ({len(needles)} keyword(s)): bills whose titles match none of these "
            f"are dropped after search. Set BILL_TOPIC_TITLE_FILTER=0 to disable.",
            file=sys.stderr,
        )
    for i, q in enumerate(queries):
        if i:
            print(f"Waiting {query_gap:.0f}s before next phrase (rate pacing)…", file=sys.stderr)
            time.sleep(query_gap)
        page = 1
        while True:
            data = search_bills(api_key, q, jurisdictions, page)
            results = data.get("results") or []
            pag = data.get("pagination") or {}
            max_page = int(pag.get("max_page") or 1)
            print(
                f"  phrase {i + 1}/{len(queries)} {q!r} — page {page}/{max_page}, {len(results)} result(s), "
                f"{len(by_id)} unique bill(s) so far",
                file=sys.stderr,
            )
            skipped_title = 0
            for b in results:
                bid = b.get("id")
                if not bid:
                    continue
                if not bill_title_matches_topic(b):
                    skipped_title += 1
                    continue
                by_id[bid] = b
            if skipped_title:
                print(
                    f"  dropped {skipped_title} search hit(s) (title did not match topic keywords)",
                    file=sys.stderr,
                )
            if page >= max_page:
                break
            page += 1
            if page_delay > 0:
                print(f"  waiting {page_delay:.0f}s before next results page…", file=sys.stderr)
            time.sleep(page_delay)
    return by_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll Open States for junk-fee-related bills and emit alerts.")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Record all current bills/actions as seen without emitting alerts.",
    )
    parser.add_argument("--db", type=Path, default=STATE_DB, help="SQLite path for dedupe state.")
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Do not write dashboard/data.json (static site still in dashboard/index.html).",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENSTATES_API_KEY", "").strip()
    if not api_key:
        print("Set OPENSTATES_API_KEY (see env.example).", file=sys.stderr)
        sys.exit(1)

    bootstrap = args.bootstrap or os.environ.get("BOOTSTRAP_SILENT", "").strip() in ("1", "true", "yes")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    queries = _env_queries()
    jurisdictions = _env_jurisdictions()
    watch = _env_watch_bills()

    conn = sqlite3.connect(args.db)
    init_db(conn)

    alerts: list[Alert] = []

    bills = collect_unique_bills(api_key, queries, jurisdictions)

    page_delay, _query_gap = _request_pacing()
    for idx, (j, sess, ident) in enumerate(watch):
        if idx:
            time.sleep(page_delay)
        try:
            detail = fetch_bill_detail(api_key, j, sess, ident)
            bid = detail.get("id")
            if bid:
                if bill_title_matches_topic(detail):
                    bills[bid] = detail
                else:
                    tit = (detail.get("title") or "")[:100]
                    print(
                        f"Skipping WATCH_BILLS {j}:{sess}:{ident} — title does not match BILL_TOPIC_KEYWORDS: {tit!r}",
                        file=sys.stderr,
                    )
        except urllib.error.HTTPError as e:
            print(f"Warning: could not load WATCH_BILLS {j}:{sess}:{ident}: {e}", file=sys.stderr)

    print(f"Processing {len(bills)} bill(s) for database / dashboard…", file=sys.stderr)
    for bill_id, b in bills.items():
        ident = b.get("identifier") or ""
        title = b.get("title") or ""
        jur = (b.get("jurisdiction") or {}).get("name") or ""
        sess = b.get("session") or ""
        url = b.get("openstates_url")

        cur = conn.execute("SELECT 1 FROM bills WHERE bill_id = ?", (bill_id,))
        is_new_bill = cur.fetchone() is None

        if is_new_bill:
            conn.execute(
                "INSERT INTO bills (bill_id, identifier, title, jurisdiction, session, url, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                (bill_id, ident, title, jur, sess, url),
            )
            if not bootstrap:
                alerts.append(
                    Alert(
                        kind="new_bill",
                        title=f"{jur} {ident}: {title}",
                        body="New bill matched your search (first time seen by this tracker).",
                        url=url,
                    )
                )

        actions: Iterable[dict[str, Any]] = b.get("actions") or []
        for action in sorted(actions, key=lambda a: (a.get("date") or "", a.get("order") or 0)):
            aid = action.get("id")
            if not aid:
                continue
            cur = conn.execute(
                "SELECT 1 FROM seen_actions WHERE bill_id = ? AND action_id = ?",
                (bill_id, str(aid)),
            )
            if cur.fetchone() is not None:
                continue

            kind = classify_action_alert(action)
            desc = action.get("description") or ""
            adate = action.get("date") or ""

            # Skip stale milestones the first time we ingest a bill (only alert on deltas).
            if not bootstrap and not is_new_bill and kind == "introduction":
                alerts.append(
                    Alert(
                        kind="introduction",
                        title=f"{jur} {ident}: introduced / filed",
                        body=f"{adate} — {desc}",
                        url=url,
                    )
                )
            elif not bootstrap and not is_new_bill and kind == "chamber_passage":
                alerts.append(
                    Alert(
                        kind="chamber_passage",
                        title=f"{jur} {ident}: chamber passage",
                        body=f"{adate} — {desc}",
                        url=url,
                    )
                )
            elif not bootstrap and not is_new_bill and kind == "signed_or_law":
                alerts.append(
                    Alert(
                        kind="signed_or_law",
                        title=f"{jur} {ident}: signed / became law",
                        body=f"{adate} — {desc}",
                        url=url,
                    )
                )

            conn.execute(
                "INSERT OR IGNORE INTO seen_actions (bill_id, action_id) VALUES (?,?)",
                (bill_id, str(aid)),
            )

        conn.commit()

    if not args.no_dashboard:
        dash_raw = os.environ.get("DASHBOARD_DIR", "").strip()
        dash_dir = Path(dash_raw).expanduser() if dash_raw else Path(__file__).resolve().parent / "dashboard"
        payload = build_dashboard_payload(conn, bills)
        out_json = write_dashboard(dash_dir, payload)
        print(f"Dashboard data written to {out_json}", file=sys.stderr)

    for a in alerts:
        msg = format_alert(a)
        print(msg)
        print("---")
        if slack_url and not bootstrap:
            try:
                send_slack(slack_url, msg)
            except Exception as e:
                print(f"Slack notify failed: {e}", file=sys.stderr)

    if bootstrap:
        print(f"Bootstrap complete ({len(bills)} bills). Next run will emit alerts for new changes.")


if __name__ == "__main__":
    main()
