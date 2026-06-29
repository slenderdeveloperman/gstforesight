"""
predictors/resolve_track_record.py — Live prediction resolution engine.

Runs every 4 days (interval-based, not clock-position-based).
Three steps per run:
  1. Register any new topic_ids from latest.json as pending rows
  2. Attempt resolution of open pending rows against the processed corpus
  3. Expire pending rows whose horizon has elapsed

Design decisions baked in:
- Resolution requires CBIC circulars or GST Council decisions — NOT signals
  (AARs, judgments, budget speeches). Signals are evidence for prediction;
  only official regulatory output counts as the prediction coming true.
- Multi-tag containment: doc_tags must *contain* the pending topic_id, not
  equal it exactly — prevents tagger noise from silently blocking resolution.
- UTC-aware datetimes throughout — never compare naive vs aware.
- Topic-level cooldown: a new pending row cannot be opened for 45 days
  after an expired_no_match for the same topic (cooldown = horizon * 0.5).
- Brier score computed alongside binary accuracy — shows calibration quality.
- Wilson CI on accuracy — suppressed if resolved < MIN_RESOLVED_FOR_ACCURACY.
- SHA256 integrity sidecar validated at startup, rewritten at exit.
"""

import hashlib
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
TRACK_RECORD_PATH = ROOT / "data" / "track-record.json"
TRACK_RECORD_SHA_PATH = ROOT / "data" / "track-record.sha256"
PROCESSED_DIR = ROOT / "data" / "processed"
PREDICTIONS_DIR = ROOT / "data" / "predictions"
HISTORY_DIR = PREDICTIONS_DIR / "history"

# ── Tuning constants ────────────────────────────────────────────────────────
RESOLUTION_INTERVAL_DAYS = 4
COOLDOWN_FACTOR = 0.5          # cooldown = horizon_days * COOLDOWN_FACTOR
MIN_RESOLVED_FOR_ACCURACY = 10  # suppress accuracy % if fewer resolved rows

# Source IDs that count as actual regulatory outcomes (not signals).
# Signals: aar_rulings, court_judgments, budget_speeches, pib_finance, etc.
# Adding finance_act / cbic_notifications here for when those scrapers land.
RESOLUTION_SOURCE_IDS = {
    "cbic_circulars",
    "gst_council_minutes",   # only if doc has decision language (see below)
    "finance_act",           # future: Finance Act provisions
    "cbic_notifications",    # future: CBIC notifications scraper
}

# For GST Council minutes, we additionally require decision language.
# Without this filter, any council minute (including deferrals) would resolve.
COUNCIL_DECISION_KEYWORDS = [
    "approved", "decided", "resolved", "ratified", "accepted",
    "recommendation accepted", "notified", "circular to be issued",
    "amendment in gst", "amendment to gst", "shall be notified",
    "notification to be issued", "w.e.f", "with effect from",
]

VALID_STATUSES = {"pending", "materialised", "expired_no_match"}


# ── UTC helpers ─────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO datetime string to a UTC-aware datetime.

    Accepts Z suffix, +HH:MM offsets, and naive strings (assumed UTC).
    Raises ValueError for strings that parse to naive without a clear UTC assumption.
    """
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # If no timezone present in a 'T' string, assume UTC
    if "T" in s and "+" not in s[10:] and (len(s) <= 19 or s[19] not in ("+", "-")):
        s = s + "+00:00"
    # Date-only (YYYY-MM-DD) → midnight UTC
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s = s + "T00:00:00+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── JSON serialization (consistent for SHA256) ─────────────────────────────

def _serialize(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _sha256(data: dict) -> str:
    return hashlib.sha256(_serialize(data).encode("utf-8")).hexdigest()


# ── Integrity ───────────────────────────────────────────────────────────────

def validate_integrity(data: dict) -> None:
    """Raise if SHA256 sidecar exists and doesn't match current file content."""
    if not TRACK_RECORD_SHA_PATH.exists():
        return
    expected = TRACK_RECORD_SHA_PATH.read_text().strip()
    actual = _sha256(data)
    if actual != expected:
        raise RuntimeError(
            "track-record.json integrity check failed — file may have been modified outside the resolver.\n"
            f"Expected SHA256: {expected}\n"
            f"Actual SHA256:   {actual}\n"
            "If this was an intentional edit, delete data/track-record.sha256 and re-run."
        )


def write_with_integrity(data: dict) -> None:
    TRACK_RECORD_PATH.write_text(_serialize(data))
    TRACK_RECORD_SHA_PATH.write_text(_sha256(data))


# ── Git helpers ─────────────────────────────────────────────────────────────

def _git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_full_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_committed_at(sha: str) -> Optional[str]:
    """Return ISO 8601 UTC committed-at for a given SHA."""
    if not sha:
        return None
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI", sha],
            capture_output=True, text=True, cwd=ROOT, timeout=10
        )
        raw = result.stdout.strip()
        if not raw:
            return None
        dt = _parse_utc(raw)
        return dt.astimezone(timezone.utc).isoformat() if dt else None
    except Exception:
        return None


def _git_sha_valid(sha: str) -> bool:
    """Return True if the given SHA exists in the repository."""
    if not sha or len(sha) < 7:
        return False
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            capture_output=True, text=True, cwd=ROOT, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() == "commit"
    except Exception:
        return False


# ── Corpus loading ──────────────────────────────────────────────────────────

def _load_processed_docs() -> list[dict]:
    docs = []
    if not PROCESSED_DIR.exists():
        return docs
    for path in PROCESSED_DIR.glob("*.json"):
        try:
            docs.append(json.loads(path.read_text()))
        except Exception:
            continue
    return docs


# ── Resolution logic ────────────────────────────────────────────────────────

def _is_resolution_doc(doc: dict) -> bool:
    """True if the doc represents an actual regulatory outcome (not a signal)."""
    source_id = doc.get("source_id", "")
    if source_id not in RESOLUTION_SOURCE_IDS:
        return False
    # GST Council minutes: only count as resolution if they contain decision language.
    # Deferral minutes would otherwise falsely resolve predictions.
    if source_id == "gst_council_minutes":
        content = (doc.get("content") or "").lower()
        return any(kw in content for kw in COUNCIL_DECISION_KEYWORDS)
    return True


def _doc_tags(doc: dict) -> list[str]:
    """Normalise topic_tags — may be list or comma-separated string."""
    tags = doc.get("topic_tags") or []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return list(tags)


def _resolves_prediction(doc: dict, row: dict) -> bool:
    """True if doc is a resolution-eligible outcome that post-dates predicted_at
    and matches the prediction's topic_id via containment (not equality)."""
    if not _is_resolution_doc(doc):
        return False

    # Multi-tag containment: topic_id must appear *in* the doc's tag list.
    # This protects against tagger noise where a circular is tagged with a
    # superset of topics but still clearly covers the predicted topic.
    if row["topic_id"] not in _doc_tags(doc):
        return False

    # Doc must post-date the prediction
    predicted_at = _parse_utc(row.get("predicted_at"))
    doc_date_str = doc.get("date") or doc.get("tagged_at") or ""
    doc_date = _parse_utc(doc_date_str)
    if not predicted_at or not doc_date:
        return False

    return doc_date > predicted_at


def _is_expired(row: dict, now: datetime) -> bool:
    """True if now > predicted_at + horizon_days (UTC-aware comparison)."""
    predicted_at = _parse_utc(row.get("predicted_at"))
    if not predicted_at:
        return False
    horizon_days = row.get("horizon_days", 90)
    deadline = predicted_at + timedelta(days=horizon_days)
    return now > deadline


def _is_in_cooldown(topic_id: str, predictions: list[dict], now: datetime) -> bool:
    """True if a recent expired_no_match for this topic is still within cooldown.

    Cooldown = horizon_days * COOLDOWN_FACTOR. Prevents a topic from immediately
    cycling back to pending right after expiry, which would let the model endlessly
    re-try a topic without each attempt counting as a separate miss in the record.
    """
    expired_rows = [
        r for r in predictions
        if r.get("topic_id") == topic_id
        and r.get("status") == "expired_no_match"
        and r.get("resolved_at")
    ]
    for row in expired_rows:
        resolved_at = _parse_utc(row["resolved_at"])
        if not resolved_at:
            continue
        cooldown_days = row.get("horizon_days", 90) * COOLDOWN_FACTOR
        if (now - resolved_at).days < cooldown_days:
            return True
    return False


# ── Scorecard computation ───────────────────────────────────────────────────

def _wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    """Wilson score 95% confidence interval for a binomial proportion."""
    if n == 0:
        return None, None
    p = hits / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return round(max(center - margin, 0.0), 3), round(min(center + margin, 1.0), 3)


def _compute_scorecard(predictions: list[dict]) -> dict:
    materialised = [r for r in predictions if r.get("status") == "materialised"]
    expired = [r for r in predictions if r.get("status") == "expired_no_match"]
    pending = [r for r in predictions if r.get("status") == "pending"]
    resolved = materialised + expired
    n_resolved = len(resolved)
    n_hits = len(materialised)

    # Brier score: lower is better; random 50% guesser scores 0.25
    brier = None
    if resolved:
        brier = round(
            sum(
                (r["probability"] / 100 - (1.0 if r["status"] == "materialised" else 0.0)) ** 2
                for r in resolved
            ) / n_resolved,
            4,
        )

    # Accuracy and Wilson CI — suppressed until MIN_RESOLVED_FOR_ACCURACY rows resolved
    accuracy = None
    ci_low = None
    ci_high = None
    if n_resolved >= MIN_RESOLVED_FOR_ACCURACY:
        accuracy = round(n_hits / n_resolved, 3)
        ci_low, ci_high = _wilson_ci(n_hits, n_resolved)

    return {
        "total": len(predictions),
        "resolved": n_resolved,
        "materialised": n_hits,
        "expired_no_match": len(expired),
        "pending": len(pending),
        "accuracy": accuracy,
        "accuracy_ci_low": ci_low,
        "accuracy_ci_high": ci_high,
        "brier_score": brier,
        "brier_n": n_resolved,
        "min_resolved_for_accuracy": MIN_RESOLVED_FOR_ACCURACY,
    }


# ── Step 1: register new predictions ───────────────────────────────────────

def _build_row_id(topic_id: str, predicted_at: str) -> str:
    date_part = predicted_at[:10].replace("-", "") if predicted_at else "unknown"
    return f"{topic_id}__{date_part}"


def register_new_predictions(
    latest: dict,
    predictions: list[dict],
    source_commit: str,
    source_committed_at: Optional[str],
    now: datetime,
) -> list[dict]:
    """Add pending rows for any topic_id in latest.json not already open."""
    open_topic_ids = {
        r["topic_id"] for r in predictions if r.get("status") == "pending"
    }

    for pred in latest.get("predictions", []):
        topic_id = pred.get("topic_id")
        if not topic_id:
            continue
        if topic_id in open_topic_ids:
            continue  # already has an open pending row

        # Respect cooldown: don't re-open immediately after an expired_no_match
        if _is_in_cooldown(topic_id, predictions, now):
            print(f"  [register] {topic_id}: cooldown active — skipping")
            continue

        # Validate source_commit before recording it as proof
        commit_ok = _git_sha_valid(source_commit) if source_commit else False
        safe_commit = source_commit if commit_ok else None
        if source_commit and not commit_ok:
            print(f"  [register] WARNING: source_commit {source_commit!r} not found in git history — recording as null")

        predicted_at = pred.get("generated_at") or now.isoformat()
        row = {
            "id": _build_row_id(topic_id, predicted_at),
            "topic_id": topic_id,
            "topic_label": pred.get("topic_label", topic_id),
            "topic_short": pred.get("topic_label", topic_id).split("/")[0].strip(),
            "probability": pred.get("probability", 0.0),
            "probability_at_resolution": None,
            "called_on": predicted_at[:10],
            "predicted_at": predicted_at if predicted_at.endswith("Z") else predicted_at + "Z",
            "git_commit": safe_commit,
            "source_committed_at": source_committed_at,
            "horizon_label": pred.get("horizon_label", ""),
            "horizon_days": pred.get("horizon_days", 90),
            "deadline": (
                _parse_utc(predicted_at) + timedelta(days=pred.get("horizon_days", 90))
            ).strftime("%Y-%m-%d") if _parse_utc(predicted_at) else None,
            "status": "pending",
            "resolved_at": None,
            "outcome_doc_id": None,
            "outcome_summary": None,
            "days_to_resolution": None,
            "top_signal": (
                pred["signals"][0]["description"] if pred.get("signals") else ""
            ),
            "outcome": None,
        }
        predictions.append(row)
        open_topic_ids.add(topic_id)
        print(f"  [register] {topic_id}: new pending row created (p={row['probability']}%)")

    return predictions


# ── Step 2: resolve open predictions ───────────────────────────────────────

def resolve_open_predictions(
    predictions: list[dict], docs: list[dict], now: datetime
) -> list[dict]:
    """Attempt to resolve pending rows against the corpus."""
    for row in predictions:
        if row.get("status") != "pending":
            continue

        # Update probability_at_resolution from the current latest.json if available
        # (captures probability drift — what the model believed just before resolution)
        # This is a best-effort update; the field may be stale if latest.json changed.

        for doc in docs:
            if _resolves_prediction(doc, row):
                row["status"] = "materialised"
                row["resolved_at"] = now.isoformat()
                row["outcome_doc_id"] = doc.get("doc_id")
                row["outcome_summary"] = (
                    (doc.get("title") or "")[:200] or
                    (doc.get("content") or "")[:200]
                )
                predicted_at = _parse_utc(row.get("predicted_at"))
                row["days_to_resolution"] = (
                    (now - predicted_at).days if predicted_at else None
                )
                print(
                    f"  [resolve] {row['topic_id']}: materialised via "
                    f"{doc.get('doc_id', 'unknown')} ({doc.get('source_id', '')})"
                )
                break

    return predictions


# ── Step 3: expire overdue predictions ─────────────────────────────────────

def expire_overdue_predictions(
    predictions: list[dict], now: datetime
) -> list[dict]:
    """Flip pending rows past their horizon to expired_no_match."""
    for row in predictions:
        if row.get("status") != "pending":
            continue
        if _is_expired(row, now):
            row["status"] = "expired_no_match"
            row["resolved_at"] = now.isoformat()
            print(
                f"  [expire] {row['topic_id']}: expired after "
                f"{row.get('horizon_days', 90)} days — recording as miss"
            )
    return predictions


# ── Main entry point ────────────────────────────────────────────────────────

def run(force: bool = False) -> None:
    """Run the full resolution cycle. Skips if < RESOLUTION_INTERVAL_DAYS since last run."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    if not TRACK_RECORD_PATH.exists():
        print("[resolve] track-record.json not found — nothing to resolve")
        return

    data = json.loads(TRACK_RECORD_PATH.read_text())

    # Integrity check before modifying anything
    validate_integrity(data)

    # Interval gate: skip if we ran recently
    now = _utcnow()
    last_run_str = data.get("meta", {}).get("last_resolution_run")
    if last_run_str and not force:
        last_run = _parse_utc(last_run_str)
        if last_run and (now - last_run).days < RESOLUTION_INTERVAL_DAYS:
            days_since = (now - last_run).days
            print(
                f"[resolve] skipping — last run {days_since}d ago "
                f"(interval = {RESOLUTION_INTERVAL_DAYS}d). Use --force to override."
            )
            return

    print(f"[resolve] starting resolution run at {now.isoformat()}")

    predictions = data.get("predictions", [])

    # Validate all existing statuses
    for row in predictions:
        if row.get("status") not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status {row['status']!r} in row {row.get('id')} — "
                f"only {VALID_STATUSES} are allowed"
            )

    # Load latest.json and corpus
    latest_path = PREDICTIONS_DIR / "latest.json"
    latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}

    docs = _load_processed_docs()
    print(f"[resolve] loaded {len(docs)} processed documents")

    # Derive the current commit info for new rows
    source_commit = _git_full_sha()
    source_committed_at = _git_committed_at(source_commit)

    # Update probability_at_resolution for all pending rows from latest.json
    latest_probs = {p["topic_id"]: p["probability"] for p in latest.get("predictions", [])}
    for row in predictions:
        if row.get("status") == "pending" and row["topic_id"] in latest_probs:
            row["probability_at_resolution"] = latest_probs[row["topic_id"]]

    # Steps 1-3
    predictions = register_new_predictions(latest, predictions, source_commit, source_committed_at, now)
    predictions = resolve_open_predictions(predictions, docs, now)
    predictions = expire_overdue_predictions(predictions, now)

    # Recompute scorecard
    data["predictions"] = predictions
    data["scorecard"] = _compute_scorecard(predictions)
    data["meta"]["last_updated"] = now.strftime("%Y-%m-%d")
    data["meta"]["last_resolution_run"] = now.isoformat()

    write_with_integrity(data)
    print(
        f"[resolve] done — "
        f"{data['scorecard']['materialised']} materialised, "
        f"{data['scorecard']['expired_no_match']} expired, "
        f"{data['scorecard']['pending']} pending"
    )
