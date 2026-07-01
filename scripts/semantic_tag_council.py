"""
scripts/semantic_tag_council.py — Phase 2 P4: improve topic tags on GST Council minutes.

Two-pass approach:
  Pass 1 — Windowed regex: run the existing tagger on every 50K window of
            each meeting doc and union the results. Free, fast, fixes the
            50K truncation bug.
  Pass 2 — Sarvam semantic: for meetings where ≥2 of the 12 topics are still
            missing after Pass 1, send agenda-section excerpts to sarvam-30b
            for classification. Catches indirect deferral language.
  Pass 3 — Per-chunk regex: re-tag each chunk individually (vs. inheriting
            doc-level tags). Improves vector search source attribution.
  Pass 4 — Supabase upsert: push updated chunk topic_tags to Supabase.
  Pass 5 — Rebuild predictions from updated processed docs.

Usage:
    HF_HUB_OFFLINE=1 .venv/bin/python scripts/semantic_tag_council.py
    HF_HUB_OFFLINE=1 .venv/bin/python scripts/semantic_tag_council.py --recent-only
"""

import gc
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from processors.tagger import TOPIC_KEYWORDS

RAW_DIR       = Path("data/raw/gst_council_minutes")
PROCESSED_DIR = Path("data/processed")
CHUNKS_DIR    = Path("data/chunks")

WINDOW_SIZE   = 50_000
WINDOW_OVERLAP = 5_000

ALL_TOPICS = list(TOPIC_KEYWORDS.keys())

TOPIC_LABELS = {
    "itc_eligibility":        "ITC eligibility, Section 16/17, Rule 37A, blocked credit",
    "rcm_coverage":           "Reverse charge mechanism, RCM, Section 9(3)/(4)",
    "rate_rationalisation":   "GST rate changes, rate rationalisation, fitment committee, nil/5/12/18/28%",
    "return_format":          "GSTR-1, GSTR-3B, GSTR-9, return filing, QRMP",
    "ims_itc_flow":           "Invoice Management System, IMS, GSTR-2B, Rule 60B",
    "e_invoicing":            "E-invoicing, IRN, electronic invoice, Rule 48, threshold",
    "classification_disputes":"HSN classification, composite/mixed supply, works contract, AAR",
    "valuation":              "GST valuation, transaction value, related party, discount, Rule 27-35",
    "place_of_supply":        "Place of supply, OIDAR, intermediary, cross-border, Section 12/13",
    "gst_on_crypto_vda":      "Virtual digital assets, VDA, cryptocurrency, NFT, blockchain",
    "msme_composition":       "Composition scheme, threshold limit, MSME, aggregate turnover, Section 10",
    "real_estate":            "Real estate, affordable housing, under-construction, works contract, Section 17(5)",
}


# ── Pass 1: windowed regex ─────────────────────────────────────────────────────

def regex_tag_windowed(text: str) -> list[str]:
    """Run regex tagger on every 50K window of text; return union of matched topics."""
    all_tags: set[str] = set()
    step = WINDOW_SIZE - WINDOW_OVERLAP
    for start in range(0, max(len(text), 1), step):
        window = text[start: start + WINDOW_SIZE].lower()
        for topic_id, patterns in TOPIC_KEYWORDS.items():
            for pattern in patterns:
                try:
                    if re.search(pattern, window, re.IGNORECASE):
                        all_tags.add(topic_id)
                        break
                except re.error:
                    pass
    return sorted(all_tags)


def per_chunk_regex_tag(text: str) -> list[str]:
    """Single-window regex tag for a chunk (chunks are ≤800 tokens, well under 50K)."""
    tags: set[str] = set()
    lower = text.lower()
    for topic_id, patterns in TOPIC_KEYWORDS.items():
        for pattern in patterns:
            try:
                if re.search(pattern, lower, re.IGNORECASE):
                    tags.add(topic_id)
                    break
            except re.error:
                pass
    return sorted(tags)


# ── Pass 2: Sarvam semantic gap-fill ──────────────────────────────────────────

SARVAM_URL = "https://api.sarvam.ai/v1/chat/completions"

CLASSIFY_PROMPT = """\
You are classifying a GST Council meeting minutes excerpt.

Your task: identify which of these 12 GST topic IDs are discussed — directly OR indirectly
(e.g. via deferral language like "kept in abeyance", GoM names, fitment committee references,
vague references like "the matter" when the prior context names a topic).

TOPICS:
{topic_list}

EXCERPT:
{excerpt}

Respond with ONLY a comma-separated list of matching topic IDs from the list above.
If none match, respond with: none
Example: itc_eligibility,rate_rationalisation,e_invoicing"""


def sarvam_classify(excerpt: str, api_key: str) -> list[str]:
    """Call sarvam-30b to classify which topics are in this excerpt."""
    topic_list = "\n".join(f"  {tid}: {desc}" for tid, desc in TOPIC_LABELS.items())
    prompt = CLASSIFY_PROMPT.format(topic_list=topic_list, excerpt=excerpt[:3000])

    payload = json.dumps({
        "model": "sarvam-30b",
        "messages": [
            {"role": "system", "content": "You are a GST domain classifier. Output only the requested format."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        # sarvam-30b is a CoT model — reasoning tokens are billed against
        # max_tokens before the final answer is emitted in
        # choices[0].message.content, so a low cap regularly finishes with
        # finish_reason="length" and content=None (silently dropping the
        # classification for that excerpt). 3000 was well below the
        # documented safe minimum of ~4000. 4096 is the hard ceiling for the
        # "starter" Sarvam subscription tier (verified via live 400 response:
        # "max_tokens (4500) exceeds the maximum allowed ... (starter): 4096")
        # — this is the most headroom available without a plan upgrade.
        "max_tokens": 4096,
    }).encode()

    req = urllib.request.Request(
        SARVAM_URL, data=payload,
        headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            answer = (data["choices"][0]["message"].get("content") or "").strip().lower()
            if answer == "none" or not answer:
                return []
            found = []
            for tid in ALL_TOPICS:
                if tid in answer:
                    found.append(tid)
            return found
    except Exception as e:
        print(f"    [sarvam] error: {e}")
        return []


def extract_agenda_excerpts(text: str, n: int = 6) -> list[str]:
    """
    Extract up to n agenda section excerpts from meeting minutes.
    GST Council minutes use numbered agenda items — split on those boundaries.
    """
    # Match agenda item headers like "Agenda Item 3", "3.", "Item No. 5" etc.
    pattern = re.compile(
        r'(?:agenda\s+item\s+\d+|item\s+no\.?\s*\d+|\n\s*\d{1,2}\s*[.)]\s+[A-Z])',
        re.IGNORECASE
    )
    splits = [m.start() for m in pattern.finditer(text)]

    if len(splits) < 2:
        # Fallback: take evenly-spaced 3K-char windows from across the document
        step = max(len(text) // (n + 1), 3000)
        return [text[i * step: i * step + 3000] for i in range(1, n + 1)]

    excerpts = []
    for i, start in enumerate(splits[:n]):
        end = splits[i + 1] if i + 1 < len(splits) else start + 4000
        excerpts.append(text[start: min(end, start + 4000)])

    return excerpts


# ── Supabase upsert ───────────────────────────────────────────────────────────

def upsert_chunks_to_supabase(chunk_rows: list[dict], supabase_url: str, service_key: str):
    """Upsert chunk rows (with updated topic_tags) to Supabase via REST."""
    if not chunk_rows:
        return 0

    rest_url = f"{supabase_url}/rest/v1/chunks"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    upserted = 0
    for i in range(0, len(chunk_rows), 100):
        batch = chunk_rows[i: i + 100]
        payload = json.dumps(batch).encode()
        req = urllib.request.Request(
            rest_url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status in (200, 201):
                    upserted += len(batch)
        except Exception as e:
            print(f"    [supabase] upsert error: {e}", flush=True)
    return upserted


# ── Main ───────────────────────────────────────────────────────────────────────

def load_env() -> dict:
    env: dict[str, str] = {}
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().replace("\r", "").replace("\n", "")
    return env


def run(recent_only: bool = False):
    cfg = load_env()
    sarvam_key = cfg.get("SARVAM_API_KEY", "")
    supabase_url = cfg.get("SUPABASE_URL", "")
    service_key = cfg.get("SUPABASE_SERVICE_KEY", "")

    if not sarvam_key:
        print("[semantic-tag] SARVAM_API_KEY missing in .env")
        sys.exit(1)
    if not supabase_url or not service_key:
        print("[semantic-tag] SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env")
        sys.exit(1)

    council_processed = sorted(PROCESSED_DIR.glob("gst_council_*.json"))
    if recent_only:
        # Only meetings 50 onwards (recent, highest signal value)
        council_processed = [
            p for p in council_processed
            if int(re.search(r"_(\d+)", p.stem).group(1)) >= 50
        ]

    print(f"\n[semantic-tag] processing {len(council_processed)} council meeting docs", flush=True)
    print(f"  Pass 1 — windowed regex re-tag", flush=True)
    print(f"  Pass 2 — Sarvam semantic gap-fill (meetings with ≥2 missing topics)", flush=True)
    print(f"  Pass 3 — per-chunk regex tagging", flush=True)
    print(f"  Pass 4 — Supabase upsert\n", flush=True)

    docs_updated = 0
    chunks_updated = 0
    all_chunk_rows: list[dict] = []

    for doc_idx, proc_path in enumerate(council_processed, 1):
        t_doc = time.time()
        doc = json.loads(proc_path.read_text())
        meeting_id = proc_path.stem  # e.g. "gst_council_50"
        content = doc.get("content") or ""
        print(f"\n[{doc_idx:02d}/{len(council_processed)}] {meeting_id} — {len(content):,} chars", flush=True)

        if not content:
            print(f"  skip (no content)", flush=True)
            continue

        # ── Pass 1: windowed regex ────────────────────────────────────────────
        t0 = time.time()
        print(f"  pass1: windowed regex...", end=" ", flush=True)
        old_tags = set(doc.get("topic_tags") or [])
        new_tags = set(regex_tag_windowed(content))
        gained_pass1 = new_tags - old_tags
        missing_after_p1 = set(ALL_TOPICS) - new_tags
        print(f"{int((time.time()-t0)*1000)}ms — {len(new_tags)} topics found, +{len(gained_pass1)} new", flush=True)
        if gained_pass1:
            print(f"    +regex: {sorted(gained_pass1)}", flush=True)

        # ── Pass 2: Sarvam for remaining gaps (≥2 topics still missing) ───────
        # Only run on meeting 30+ (Oct 2018 onwards). Earlier meetings predate
        # IMS, e-invoicing, and VDA — those topics didn't exist, so Sarvam will
        # always return "none" for them, wasting ~2-4 min per meeting.
        meeting_num = int(re.search(r"_(\d+)", meeting_id).group(1))
        sarvam_added: set[str] = set()
        if len(missing_after_p1) >= 2 and meeting_num >= 30:
            excerpts = extract_agenda_excerpts(content, n=8)
            print(f"  pass2: sarvam on {len(excerpts)} excerpts for missing={sorted(missing_after_p1)}", flush=True)
            for ei, exc in enumerate(excerpts, 1):
                t0 = time.time()
                print(f"    excerpt {ei}/{len(excerpts)}...", end=" ", flush=True)
                found = sarvam_classify(exc, sarvam_key)
                new_from_sarvam = set(found) & missing_after_p1
                elapsed = int((time.time()-t0)*1000)
                print(f"{elapsed}ms — found: {sorted(new_from_sarvam) or 'none'}", flush=True)
                if new_from_sarvam:
                    sarvam_added |= new_from_sarvam
                    new_tags |= new_from_sarvam
                    missing_after_p1 -= new_from_sarvam
                    if not missing_after_p1:
                        print(f"    all missing topics found — skipping remaining excerpts", flush=True)
                        break
                time.sleep(0.5)
            if sarvam_added:
                print(f"    +sarvam: {sorted(sarvam_added)}", flush=True)

        # Update processed doc if tags changed
        if new_tags != old_tags:
            doc["topic_tags"] = sorted(new_tags)
            proc_path.write_text(json.dumps(doc, indent=2))
            docs_updated += 1

        final_tags = sorted(new_tags)
        print(f"  final tags ({len(final_tags)}): {final_tags}", flush=True)

        # ── Pass 3: per-chunk regex tagging ──────────────────────────────────
        chunk_path = CHUNKS_DIR / proc_path.name
        if not chunk_path.exists():
            print(f"  pass3: no chunk file — skip", flush=True)
            continue

        t0 = time.time()
        print(f"  pass3: per-chunk regex...", end=" ", flush=True)
        chunks = json.loads(chunk_path.read_text())
        changed_chunks = []

        for chunk in chunks:
            old_chunk_tags = chunk.get("topic_tags") or []
            per_chunk_tags = per_chunk_regex_tag(chunk.get("text", ""))

            # If chunk regex found nothing, fall back to the updated doc-level tags
            # (some chunks are procedural boilerplate with no keyword signal)
            new_chunk_tags = per_chunk_tags if per_chunk_tags else final_tags

            if set(new_chunk_tags) != set(old_chunk_tags):
                chunk["topic_tags"] = new_chunk_tags
                changed_chunks.append(chunk)

        print(f"{int((time.time()-t0)*1000)}ms — {len(changed_chunks)}/{len(chunks)} chunks changed", flush=True)

        if changed_chunks:
            chunk_path.write_text(json.dumps(chunks, indent=2))
            chunks_updated += len(changed_chunks)
            print(f"    saved {len(changed_chunks)} chunks to disk", flush=True)

            # Build Supabase rows for upsert (only fields that change)
            for chunk in changed_chunks:
                all_chunk_rows.append({
                    "id": chunk["chunk_id"],
                    "doc_id": chunk["doc_id"],
                    "source_id": chunk["source_id"],
                    "date": chunk.get("date") or None,
                    "topic_tags": ",".join(chunk["topic_tags"]),
                    "chunk_index": chunk["chunk_index"],
                    "content": chunk["text"],
                })

        print()

    # ── Pass 4: Supabase upsert ───────────────────────────────────────────────
    print(f"\n[pass4] upserting {len(all_chunk_rows)} changed chunks to Supabase...", flush=True)
    if all_chunk_rows:
        t0 = time.time()
        pushed = upsert_chunks_to_supabase(all_chunk_rows, supabase_url, service_key)
        print(f"  upserted: {pushed} rows in {int((time.time()-t0)*1000)}ms", flush=True)
    else:
        print("  no chunk changes to push", flush=True)

    print(f"\n[semantic-tag] done", flush=True)
    print(f"  processed docs updated : {docs_updated}", flush=True)
    print(f"  chunks re-tagged       : {chunks_updated}", flush=True)
    print(f"  supabase rows pushed   : {len(all_chunk_rows)}", flush=True)

    # ── Pass 5: rebuild predictions ───────────────────────────────────────────
    if docs_updated:
        print("\n[pass5] rebuilding predictions...", flush=True)
        t0 = time.time()
        from predictors.engine import PredictionEngine
        engine = PredictionEngine()
        predictions = engine.run()
        print(f"  {len(predictions)} predictions in {int((time.time()-t0)*1000)}ms", flush=True)

        print("\n  Top 5:", flush=True)
        for p in predictions[:5]:
            print(f"    {p['probability']:>5}% {p['topic_label']}", flush=True)


if __name__ == "__main__":
    recent_only = "--recent-only" in sys.argv
    run(recent_only=recent_only)
