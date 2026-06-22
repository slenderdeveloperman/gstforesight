"""
scripts/scrape_ticker.py — Build the live site ticker from the processed corpus.

Reads data/processed/*.json (our ingested docs), sorts by date desc, and writes
data/news/ticker.json with the 40 most recent items across all source types.

This runs after every ingest so the ticker always reflects the latest corpus state.
No external HTTP calls — zero scraping failures.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"
OUT_PATH      = Path(__file__).parent.parent / "data" / "news" / "ticker.json"
IST           = timezone(timedelta(hours=5, minutes=30))
MAX_ITEMS     = 40

SOURCE_LABELS = {
    "cbic_circulars":          "CBIC CIRC",
    "cbic_notifications":      "CBIC NOTIF",
    "gst_council_minutes":     "COUNCIL",
    "budget_speeches":         "BUDGET",
    "icai_representations":    "ICAI",
    "pib_press_releases":      "PIB",
    "aar_rulings":             "AAR",
    "court_judgments":         "HC/SC",
}


def fmt_date(date_str: str) -> str:
    """Convert ISO date string to ticker label like '18 JUN' or 'YDA' / '2D'."""
    if not date_str:
        return "RECENT"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(IST)
        today = datetime.now(IST).date()
        delta = (today - dt.date()).days
        if delta == 0:
            return dt.strftime("%-H:%M IST")
        if delta == 1:
            return "YDA"
        if delta <= 7:
            return f"{delta}D"
        return dt.strftime("%-d %b").upper()
    except Exception:
        return date_str[:10] if date_str else "RECENT"


def parse_date(date_str: str) -> datetime:
    if not date_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


JUNK_TITLES = re.compile(
    r'^(icai|cbic|representation|suggestions?|feedback|announcement|'
    r'list of|follow |invitation|e-learning|certificate course|workshop|seminar|'
    r'webinar|one day|two day|national conference|handbook on|draft\b|unique\b|'
    r'feedback form|click here|read more|download|view more)$',
    re.IGNORECASE
)


CIRC_NUM_RE  = re.compile(r'Circular No\.\s*([\d/A-Z\-]+)', re.IGNORECASE)
NOTIF_NUM_RE = re.compile(r'Notification No\.\s*([\d/A-Z\-]+)', re.IGNORECASE)
HEX_HASH_RE  = re.compile(r'\b[0-9a-f]{8,}\b', re.IGNORECASE)


def clean_title(title: str, content: str = "") -> str:
    """Collapse whitespace, strip doc-ID hashes, return best headline."""
    def normalise(t: str) -> str:
        t = re.sub(r'[\s\t\n\r]+', ' ', t).strip()
        t = HEX_HASH_RE.sub('', t)           # remove embedded doc-ID hashes
        t = re.sub(r':\s*', ': ', t)         # normalise colon spacing
        t = re.sub(r'\s+', ' ', t).strip()
        t = t.strip(': ')
        return t

    # Try to prepend real circular/notification number from content
    prefix = ""
    if content:
        m = CIRC_NUM_RE.search(content[:500])
        if m:
            prefix = f"Circular {m.group(1)} — "
        else:
            m = NOTIF_NUM_RE.search(content[:500])
            if m:
                prefix = f"Notification {m.group(1)} — "

    if title:
        t = normalise(title)
        # Strip "CBIC Circular :" prefix that's now empty after hash removal
        t = re.sub(r'^(CBIC\s+)?(Circular|Notification)\s*:\s*', '', t, flags=re.IGNORECASE).strip()
        if len(t) >= 20 and not JUNK_TITLES.match(t):
            full = (prefix + t)[:120]
            return full + "…" if len(prefix + t) > 120 else full

    # Fall back to first substantive sentence of content
    if content:
        clean = normalise(content)
        sentences = re.split(r'(?<=[.!?])\s+', clean)
        for sent in sentences:
            sent = sent.strip()
            if len(sent) >= 30 and not JUNK_TITLES.match(sent):
                full = (prefix + sent)[:120]
                return full + "…" if len(prefix + sent) > 120 else full

    return ""


def main():
    now_ist = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    print(f"\n[ticker] building from corpus — {now_ist}\n", flush=True)

    if not PROCESSED_DIR.exists():
        print("[ticker] data/processed/ not found — aborting", flush=True)
        sys.exit(1)

    docs = []
    skipped = 0
    for path in PROCESSED_DIR.glob("*.json"):
        try:
            doc = json.loads(path.read_text())
        except Exception:
            skipped += 1
            continue

        date_str  = doc.get("date") or doc.get("published_date") or doc.get("scraped_at") or ""
        title     = doc.get("title") or doc.get("heading") or ""
        content   = doc.get("content") or ""
        source_id = doc.get("source_id") or path.stem.rsplit("_", 1)[0]
        doc_id    = doc.get("doc_id") or path.stem

        headline = clean_title(title, content)
        if not headline:
            skipped += 1
            continue

        label = SOURCE_LABELS.get(source_id, source_id.upper().replace("_", " "))

        docs.append({
            "_date":    parse_date(date_str),
            "time":     fmt_date(date_str),
            "text":     headline,
            "source":   label,
            "doc_id":   doc_id,
            "url":      doc.get("source_url") or "",
        })

    print(f"  loaded {len(docs)} docs, skipped {skipped}", flush=True)

    # Sort newest first, then deduplicate and cap per-source for diversity
    docs.sort(key=lambda d: d["_date"], reverse=True)

    seen: set[str] = set()
    source_counts: dict[str, int] = {}
    MAX_PER_SOURCE = 8
    items = []
    for d in docs:
        key = d["text"][:50].lower()
        src = d["source"]
        if key in seen:
            continue
        if source_counts.get(src, 0) >= MAX_PER_SOURCE:
            continue
        seen.add(key)
        source_counts[src] = source_counts.get(src, 0) + 1
        items.append({k: v for k, v in d.items() if k != "_date"})
        if len(items) >= MAX_ITEMS:
            break

    if not items:
        print("[ticker] nothing to write", flush=True)
        sys.exit(0)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    print(f"\n[ticker] wrote {len(items)} items → {OUT_PATH}", flush=True)
    print(f"\n  Top 5:", flush=True)
    for it in items[:5]:
        print(f"    {it['time']:>8} [{it['source']:>12}] {it['text'][:65]}", flush=True)


if __name__ == "__main__":
    main()
