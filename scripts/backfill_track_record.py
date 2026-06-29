"""
scripts/backfill_track_record.py — Seed track-record.json from git history.

Walks every historical commit of data/predictions/latest.json in chronological
order and registers predictions that predate the current track-record.json entries.
Does NOT auto-resolve historical predictions (the corpus was not committed to git,
so we can't know what resolution docs existed at each point in time). Resolution
runs forward from today via `python -m gst_foresight resolve`.

Usage:
    python3 scripts/backfill_track_record.py [--dry-run]

Safe to re-run — idempotent. Already-registered predictions are skipped.
After running, the SHA256 sidecar is updated automatically.
"""

import argparse
import json
import subprocess
import sys
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

TRACK_RECORD_PATH = ROOT / "data" / "track-record.json"
HISTORY_DIR = ROOT / "data" / "predictions" / "history"


def _run_git(*args, check=True) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_sha_valid(sha: str) -> bool:
    if not sha or len(sha) < 7:
        return False
    result = subprocess.run(
        ["git", "cat-file", "-t", sha],
        capture_output=True, text=True, cwd=ROOT, timeout=10
    )
    return result.returncode == 0 and result.stdout.strip() == "commit"


def _committed_at_utc(sha: str) -> str:
    raw = _run_git("log", "-1", "--format=%cI", sha)
    # Convert to UTC ISO string
    from predictors.resolve_track_record import _parse_utc
    dt = _parse_utc(raw)
    if dt:
        return dt.astimezone(timezone.utc).isoformat()
    return ""


def _show_latest(sha: str) -> dict:
    """Return parsed latest.json at a given commit SHA, or {}."""
    result = subprocess.run(
        ["git", "show", f"{sha}:data/predictions/latest.json"],
        capture_output=True, text=True, cwd=ROOT, timeout=15
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _build_row_id(topic_id: str, predicted_at: str) -> str:
    date_part = predicted_at[:10].replace("-", "") if predicted_at else "unknown"
    return f"{topic_id}__{date_part}"


def main(dry_run: bool = False) -> None:
    from predictors.resolve_track_record import write_with_integrity

    if not TRACK_RECORD_PATH.exists():
        print("[backfill] track-record.json not found — run the resolver first to initialise it")
        sys.exit(1)

    data = json.loads(TRACK_RECORD_PATH.read_text())
    predictions = data.get("predictions", [])

    # Keys already registered (by id)
    registered_ids = {r["id"] for r in predictions}
    # Topic IDs that currently have an open pending row
    open_topics = {r["topic_id"] for r in predictions if r.get("status") == "pending"}

    # Get all commits that touched latest.json, in chronological order (oldest first)
    raw_log = _run_git(
        "log", "--follow", "--format=%H %cI", "--", "data/predictions/latest.json"
    )
    commits = [line.split(" ", 1) for line in raw_log.splitlines() if line.strip()]
    commits.reverse()  # oldest first

    print(f"[backfill] found {len(commits)} commits touching data/predictions/latest.json")

    added = 0
    for sha, committed_at_raw in commits:
        # Validate the SHA still exists (rebases can orphan commits)
        if not _git_sha_valid(sha):
            print(f"  [backfill] WARNING: {sha[:8]} not found in git history — skipping")
            continue

        from predictors.resolve_track_record import _parse_utc
        dt = _parse_utc(committed_at_raw)
        committed_at = dt.astimezone(timezone.utc).isoformat() if dt else None
        called_on = committed_at[:10] if committed_at else sha[:8]

        latest = _show_latest(sha)
        if not latest:
            continue

        for pred in latest.get("predictions", []):
            topic_id = pred.get("topic_id")
            if not topic_id:
                continue

            predicted_at = pred.get("generated_at") or committed_at or ""
            row_id = _build_row_id(topic_id, predicted_at)

            if row_id in registered_ids:
                continue  # already registered — idempotent

            # Only register if not already pending (don't duplicate open rows)
            if topic_id in open_topics:
                continue

            horizon_days = pred.get("horizon_days", 90)
            from datetime import timedelta
            predicted_dt = _parse_utc(predicted_at)
            deadline = (
                (predicted_dt + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
                if predicted_dt else None
            )

            row = {
                "id": row_id,
                "topic_id": topic_id,
                "topic_label": pred.get("topic_label", topic_id),
                "topic_short": pred.get("topic_label", topic_id).split("/")[0].strip(),
                "probability": pred.get("probability", 0.0),
                "probability_at_resolution": None,
                "called_on": called_on,
                "predicted_at": predicted_at if predicted_at.endswith("Z") else predicted_at + "Z",
                "git_commit": sha,
                "source_committed_at": committed_at,
                "horizon_label": pred.get("horizon_label", ""),
                "horizon_days": horizon_days,
                "deadline": deadline,
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

            registered_ids.add(row_id)
            open_topics.add(topic_id)

            if dry_run:
                print(f"  [backfill] DRY-RUN: would register {row_id} (p={row['probability']}%)")
            else:
                predictions.append(row)
                print(f"  [backfill] registered {row_id} (p={row['probability']}%)")
                added += 1

    if not dry_run:
        from predictors.resolve_track_record import _compute_scorecard
        data["predictions"] = predictions
        data["scorecard"] = _compute_scorecard(predictions)
        write_with_integrity(data)
        print(f"[backfill] done — {added} new rows registered, SHA256 updated")
    else:
        print(f"[backfill] dry-run complete — {added} rows would be added")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill track-record.json from git history")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be added without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
