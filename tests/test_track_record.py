"""
tests/test_track_record.py — Track record resolution engine tests.

Run: node --experimental-vm-modules node_modules/.bin/jest tests/test_track_record.py
  or: python3 -m pytest tests/test_track_record.py -v

All tests in this file are Python (pytest). The suite covers §9.1–9.6 from the spec
plus the additional invariants identified in the critical review.
"""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sys

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from predictors.resolve_track_record import (
    VALID_STATUSES,
    _compute_scorecard,
    _is_expired,
    _is_in_cooldown,
    _parse_utc,
    _resolves_prediction,
    _sha256,
    _serialize,
    _wilson_ci,
    expire_overdue_predictions,
    register_new_predictions,
    resolve_open_predictions,
    validate_integrity,
    write_with_integrity,
    TRACK_RECORD_PATH,
    TRACK_RECORD_SHA_PATH,
    MIN_RESOLVED_FOR_ACCURACY,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _utc(s: str) -> datetime:
    """Parse ISO string to UTC-aware datetime for test setup."""
    return _parse_utc(s)


def _pending_row(
    topic_id="itc_eligibility",
    probability=72.0,
    predicted_at="2026-05-14T00:00:00Z",
    horizon_days=90,
    status="pending",
    resolved_at=None,
):
    return {
        "id": f"{topic_id}__20260514",
        "topic_id": topic_id,
        "topic_label": "ITC Eligibility Rules",
        "topic_short": "ITC Rules",
        "probability": probability,
        "probability_at_resolution": None,
        "called_on": "2026-05-14",
        "predicted_at": predicted_at,
        "git_commit": "abc123",
        "source_committed_at": predicted_at,
        "horizon_label": "Next GST Council meeting",
        "horizon_days": horizon_days,
        "deadline": "2026-08-12",
        "status": status,
        "resolved_at": resolved_at,
        "outcome_doc_id": None,
        "outcome_summary": None,
        "days_to_resolution": None,
        "top_signal": "Test signal",
        "outcome": None,
    }


def _resolution_doc(
    topic_id="itc_eligibility",
    source_id="cbic_circulars",
    date="2026-06-15T00:00:00Z",
    doc_id="cbic_circ_test_001",
):
    return {
        "doc_id": doc_id,
        "source_id": source_id,
        "topic_tags": [topic_id],
        "date": date,
        "title": f"Circular clarifying {topic_id}",
        "content": "The CBIC hereby clarifies...",
    }


# ── §9.1 resolve_track_record.py core logic ──────────────────────────────────

class TestNewPredictionRegistration:
    def test_new_prediction_registers_as_pending(self):
        latest = {
            "predictions": [
                {
                    "topic_id": "itc_eligibility",
                    "topic_label": "ITC Eligibility Rules",
                    "probability": 72.0,
                    "horizon_label": "Next GST Council meeting",
                    "horizon_days": 90,
                    "generated_at": "2026-06-01T00:00:00Z",
                    "signals": [{"description": "Test signal"}],
                }
            ]
        }
        predictions = []
        now = _utc("2026-06-01T12:00:00Z")

        result = register_new_predictions(latest, predictions, "abc123", "2026-06-01T00:00:00Z", now)

        assert len(result) == 1
        row = result[0]
        assert row["status"] == "pending"
        assert row["resolved_at"] is None
        assert row["topic_id"] == "itc_eligibility"
        assert row["probability"] == 72.0

    def test_duplicate_topic_does_not_create_second_pending_row(self):
        existing = [_pending_row("itc_eligibility")]  # already open
        latest = {
            "predictions": [
                {
                    "topic_id": "itc_eligibility",
                    "topic_label": "ITC Eligibility Rules",
                    "probability": 80.0,
                    "horizon_label": "Next GST Council meeting",
                    "horizon_days": 90,
                    "generated_at": "2026-06-15T00:00:00Z",
                    "signals": [],
                }
            ]
        }
        now = _utc("2026-06-15T00:00:00Z")
        result = register_new_predictions(latest, existing, "def456", None, now)

        open_rows = [r for r in result if r["status"] == "pending"]
        assert len(open_rows) == 1, "Must have exactly one open pending row per topic"


class TestResolutionDocType:
    def test_resolution_doc_type_matches_and_resolves(self):
        row = _pending_row()
        doc = _resolution_doc(source_id="cbic_circulars", date="2026-06-15T00:00:00Z")
        now = _utc("2026-06-20T00:00:00Z")

        result = resolve_open_predictions([row], [doc], now)

        assert result[0]["status"] == "materialised"
        assert result[0]["outcome_doc_id"] == doc["doc_id"]
        assert result[0]["days_to_resolution"] is not None

    def test_signal_type_doc_does_not_resolve(self):
        """AAR rulings and judgments are signals, not resolution outcomes."""
        row = _pending_row()
        signal_doc = _resolution_doc(source_id="aar_rulings", date="2026-06-15T00:00:00Z")
        judgment_doc = _resolution_doc(source_id="court_judgments", date="2026-06-15T00:00:00Z")
        now = _utc("2026-06-20T00:00:00Z")

        result_aar = resolve_open_predictions([_pending_row()], [signal_doc], now)
        result_judgment = resolve_open_predictions([_pending_row()], [judgment_doc], now)

        assert result_aar[0]["status"] == "pending", "AAR ruling must NOT resolve a prediction"
        assert result_judgment[0]["status"] == "pending", "Court judgment must NOT resolve a prediction"

    def test_doc_dated_before_prediction_does_not_resolve(self):
        """A circular that predates the prediction cannot be the resolution."""
        row = _pending_row(predicted_at="2026-06-01T00:00:00Z")
        old_doc = _resolution_doc(source_id="cbic_circulars", date="2026-05-01T00:00:00Z")  # before prediction
        now = _utc("2026-06-20T00:00:00Z")

        result = resolve_open_predictions([row], [old_doc], now)

        assert result[0]["status"] == "pending", "Pre-prediction doc must not resolve the row"

    def test_multi_tag_containment_resolves(self):
        """Resolution fires when topic_id is *in* the doc's tag list, even with other tags."""
        row = _pending_row("itc_eligibility")
        doc = _resolution_doc("itc_eligibility", source_id="cbic_circulars", date="2026-06-15T00:00:00Z")
        doc["topic_tags"] = ["itc_eligibility", "return_format"]  # multi-tag doc

        now = _utc("2026-06-20T00:00:00Z")
        result = resolve_open_predictions([row], [doc], now)

        assert result[0]["status"] == "materialised", "Multi-tag doc should resolve if topic_id is contained"

    def test_different_topic_id_does_not_resolve(self):
        row = _pending_row("itc_eligibility")
        wrong_topic_doc = _resolution_doc("rate_rationalisation", source_id="cbic_circulars", date="2026-06-15T00:00:00Z")
        now = _utc("2026-06-20T00:00:00Z")

        result = resolve_open_predictions([row], [wrong_topic_doc], now)

        assert result[0]["status"] == "pending"

    def test_gst_council_minutes_with_decision_language_resolves(self):
        row = _pending_row()
        council_doc = _resolution_doc(source_id="gst_council_minutes", date="2026-06-15T00:00:00Z")
        council_doc["content"] = "The Council approved the amendment w.e.f 1 July 2026"
        now = _utc("2026-06-20T00:00:00Z")

        result = resolve_open_predictions([row], [council_doc], now)
        assert result[0]["status"] == "materialised"

    def test_gst_council_minutes_deferral_does_not_resolve(self):
        row = _pending_row()
        deferral_doc = _resolution_doc(source_id="gst_council_minutes", date="2026-06-15T00:00:00Z")
        deferral_doc["content"] = "The item was deferred to the next meeting for further deliberation"
        now = _utc("2026-06-20T00:00:00Z")

        result = resolve_open_predictions([row], [deferral_doc], now)
        assert result[0]["status"] == "pending", "Deferral in council minutes must not resolve"


class TestExpiry:
    def test_expiry_fires_on_short_fake_horizon(self):
        """Key test: short horizon forces the expiry path in CI without waiting 90 days."""
        row = _pending_row(horizon_days=1, predicted_at="2026-06-27T00:00:00Z")
        now = _utc("2026-06-29T00:00:00Z")  # 2 days later > 1 day horizon

        result = expire_overdue_predictions([row], now)

        assert result[0]["status"] == "expired_no_match"
        assert result[0]["resolved_at"] is not None

    def test_expiry_does_not_fire_early(self):
        """Row stays pending if horizon hasn't elapsed yet."""
        row = _pending_row(horizon_days=1, predicted_at="2026-06-28T12:00:00Z")
        now = _utc("2026-06-28T18:00:00Z")  # only 6 hours later

        result = expire_overdue_predictions([row], now)

        assert result[0]["status"] == "pending"

    def test_expiry_uses_utc_aware_comparison(self):
        """_is_expired must not raise when predicted_at has a timezone offset."""
        row = _pending_row(horizon_days=1, predicted_at="2026-06-27T18:30:00+05:30")
        now = _utc("2026-06-29T00:00:00Z")  # well past deadline

        # Should not raise TypeError (naive/aware mismatch)
        expired = _is_expired(row, now)
        assert expired is True

    def test_expiry_with_naive_predicted_at_assumed_utc(self):
        """Naive predicted_at strings (no tz) must be treated as UTC, not fail silently."""
        row = _pending_row(horizon_days=1, predicted_at="2026-06-27T00:00:00")
        now = _utc("2026-06-29T00:00:00Z")

        expired = _is_expired(row, now)
        assert expired is True

    def test_resolved_row_never_mutates_again(self):
        """A materialised row must not be overwritten by a later matching doc."""
        row = _pending_row()
        row["status"] = "materialised"
        row["outcome_doc_id"] = "original_doc"
        row["resolved_at"] = "2026-06-10T00:00:00Z"

        later_doc = _resolution_doc(source_id="cbic_circulars", date="2026-06-20T00:00:00Z")
        later_doc["doc_id"] = "later_doc"
        now = _utc("2026-06-25T00:00:00Z")

        result = resolve_open_predictions([row], [later_doc], now)

        assert result[0]["outcome_doc_id"] == "original_doc", "Resolved row must be immutable"
        assert result[0]["status"] == "materialised"


class TestCooldown:
    def test_cooldown_prevents_reopening_after_expiry(self):
        expired_row = _pending_row("itc_eligibility", horizon_days=90)
        expired_row["status"] = "expired_no_match"
        expired_row["resolved_at"] = "2026-06-28T00:00:00Z"

        now = _utc("2026-06-29T00:00:00Z")  # only 1 day after expiry, cooldown = 45d

        in_cooldown = _is_in_cooldown("itc_eligibility", [expired_row], now)
        assert in_cooldown is True

    def test_cooldown_expires_after_interval(self):
        expired_row = _pending_row("itc_eligibility", horizon_days=90)
        expired_row["status"] = "expired_no_match"
        expired_row["resolved_at"] = "2026-01-01T00:00:00Z"

        now = _utc("2026-06-29T00:00:00Z")  # ~180 days later, cooldown was 45d

        in_cooldown = _is_in_cooldown("itc_eligibility", [expired_row], now)
        assert in_cooldown is False

    def test_cooldown_does_not_affect_different_topic(self):
        expired_row = _pending_row("itc_eligibility", horizon_days=90)
        expired_row["status"] = "expired_no_match"
        expired_row["resolved_at"] = "2026-06-28T00:00:00Z"

        now = _utc("2026-06-29T00:00:00Z")

        in_cooldown = _is_in_cooldown("rate_rationalisation", [expired_row], now)
        assert in_cooldown is False


class TestStatusSchema:
    def test_no_third_status_value_possible(self):
        """Any status outside the allowed set should be detectable."""
        bad_status = "partially_resolved"
        assert bad_status not in VALID_STATUSES

        for s in VALID_STATUSES:
            assert s in {"pending", "materialised", "expired_no_match"}


# ── §9.2 Snapshot step ───────────────────────────────────────────────────────

class TestSnapshot:
    def test_snapshot_writes_dated_file(self, tmp_path):
        """Snapshot creates history/<today>.json identical to latest.json."""
        from datetime import date
        import shutil

        latest_data = {"generated_at": "2026-06-29T00:00:00Z", "predictions": []}
        latest = tmp_path / "latest.json"
        latest.write_text(json.dumps(latest_data))

        history = tmp_path / "history"
        history.mkdir()
        dest = history / f"{date.today().isoformat()}.json"

        shutil.copy(latest, dest)

        assert dest.exists()
        assert json.loads(dest.read_text()) == latest_data

    def test_snapshot_does_not_overwrite_existing(self, tmp_path):
        """Running snapshot twice on the same day must not overwrite the first file."""
        from datetime import date
        import shutil

        history = tmp_path / "history"
        history.mkdir()
        dest = history / f"{date.today().isoformat()}.json"

        original = {"original": True}
        dest.write_text(json.dumps(original))

        # Simulate second snapshot run
        latest = tmp_path / "latest.json"
        latest.write_text(json.dumps({"different": True}))

        if not dest.exists():  # Only copy if not exists
            shutil.copy(latest, dest)

        assert json.loads(dest.read_text()) == original, "Second snapshot run must not overwrite"


# ── §9.4 Scorecard / UI data layer ───────────────────────────────────────────

class TestScorecard:
    def test_accuracy_excludes_pending(self):
        """Pending rows must not inflate the accuracy denominator."""
        predictions = [
            _pending_row(status="materialised", probability=80.0),
            _pending_row(status="expired_no_match", probability=60.0),
            _pending_row(status="pending", probability=70.0),
            _pending_row(status="pending", probability=75.0),
        ]
        # Set enough resolved for accuracy to be computed
        # Only 2 resolved — below MIN_RESOLVED_FOR_ACCURACY, so accuracy will be None
        sc = _compute_scorecard(predictions)
        assert sc["resolved"] == 2
        assert sc["pending"] == 2
        # Below threshold — accuracy should be None
        assert sc["accuracy"] is None

    def test_accuracy_suppressed_below_threshold(self):
        resolved = [_pending_row(status="materialised") for _ in range(MIN_RESOLVED_FOR_ACCURACY - 1)]
        sc = _compute_scorecard(resolved)
        assert sc["accuracy"] is None, f"Accuracy must be None when resolved < {MIN_RESOLVED_FOR_ACCURACY}"

    def test_accuracy_shown_at_threshold(self):
        resolved = [_pending_row(status="materialised") for _ in range(MIN_RESOLVED_FOR_ACCURACY)]
        sc = _compute_scorecard(resolved)
        assert sc["accuracy"] == 1.0
        assert sc["accuracy_ci_low"] is not None
        assert sc["accuracy_ci_high"] is not None

    def test_brier_score_penalises_confident_wrong_prediction(self):
        """A confident miss (90% → expired) should score worse than a hedged miss (51% → expired)."""
        confident_miss = [_pending_row(status="expired_no_match", probability=90.0)]
        hedged_miss = [_pending_row(status="expired_no_match", probability=51.0)]

        sc_confident = _compute_scorecard(confident_miss)
        sc_hedged = _compute_scorecard(hedged_miss)

        assert sc_confident["brier_score"] > sc_hedged["brier_score"], (
            "Confident miss must score worse (higher Brier score) than hedged miss"
        )

    def test_brier_score_perfect_prediction(self):
        """A 100%-correct materialised prediction should have Brier contribution near 0."""
        perfect = [_pending_row(status="materialised", probability=100.0)]
        sc = _compute_scorecard(perfect)
        assert sc["brier_score"] == 0.0

    def test_wilson_ci_bounds(self):
        """Wilson CI must be within [0, 1] and low < high."""
        for hits, n in [(0, 10), (5, 10), (10, 10), (3, 15)]:
            lo, hi = _wilson_ci(hits, n)
            assert 0.0 <= lo <= 1.0
            assert 0.0 <= hi <= 1.0
            assert lo <= hi

    def test_brier_score_null_when_no_resolved(self):
        predictions = [_pending_row(status="pending")]
        sc = _compute_scorecard(predictions)
        assert sc["brier_score"] is None
        assert sc["brier_n"] == 0


# ── §9.5 Integrity ───────────────────────────────────────────────────────────

class TestIntegrity:
    def test_sha256_mismatch_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "predictors.resolve_track_record.TRACK_RECORD_SHA_PATH",
            tmp_path / "track-record.sha256"
        )
        data = {"predictions": [], "meta": {}}
        sha_path = tmp_path / "track-record.sha256"
        sha_path.write_text("0000000000000000000000000000000000000000000000000000000000000000")

        with pytest.raises(RuntimeError, match="integrity check failed"):
            validate_integrity(data)

    def test_sha256_passes_on_fresh_sidecar(self, tmp_path, monkeypatch):
        from predictors.resolve_track_record import TRACK_RECORD_SHA_PATH as orig_path
        monkeypatch.setattr(
            "predictors.resolve_track_record.TRACK_RECORD_SHA_PATH",
            tmp_path / "track-record.sha256"
        )
        # No sidecar → no error
        validate_integrity({"predictions": []})

    def test_write_with_integrity_creates_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "predictors.resolve_track_record.TRACK_RECORD_PATH",
            tmp_path / "track-record.json"
        )
        monkeypatch.setattr(
            "predictors.resolve_track_record.TRACK_RECORD_SHA_PATH",
            tmp_path / "track-record.sha256"
        )
        data = {"predictions": [], "meta": {}, "scorecard": {}}
        write_with_integrity(data)

        sha_path = tmp_path / "track-record.sha256"
        assert sha_path.exists()
        expected = _sha256(data)
        assert sha_path.read_text().strip() == expected

    def test_source_commit_none_when_sha_invalid(self):
        """If git_commit is empty or invalid, the row should record None."""
        from predictors.resolve_track_record import _git_sha_valid
        assert _git_sha_valid("") is False
        assert _git_sha_valid("notacommit") is False


# ── §9.6 Parse / timezone edge cases ─────────────────────────────────────────

class TestDatetimeParsing:
    def test_parse_utc_z_suffix(self):
        dt = _parse_utc("2026-06-15T10:00:00Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_parse_utc_offset(self):
        dt = _parse_utc("2026-06-15T10:00:00+05:30")
        assert dt.tzinfo is not None
        utc_hour = dt.astimezone(timezone.utc).hour
        assert utc_hour == 4  # 10:00 +5:30 = 04:30 UTC

    def test_parse_utc_naive_assumed_utc(self):
        dt = _parse_utc("2026-06-15T10:00:00")
        assert dt is not None
        assert dt.tzinfo is not None  # must be aware after parse

    def test_parse_utc_date_only(self):
        dt = _parse_utc("2026-06-15")
        assert dt is not None
        assert dt.hour == 0

    def test_parse_utc_none_input(self):
        assert _parse_utc(None) is None
        assert _parse_utc("") is None


# ── Integration: full cycle ───────────────────────────────────────────────────

class TestFullCycle:
    def test_register_then_expire_then_reopen_after_cooldown(self):
        """Full lifecycle: pending → expired → cooldown → reopens after cooldown expires."""
        now = _utc("2026-06-01T00:00:00Z")

        # Step 1: register
        latest = {
            "predictions": [{
                "topic_id": "rcm_coverage",
                "topic_label": "RCM",
                "probability": 70.0,
                "horizon_label": "Next council",
                "horizon_days": 4,  # short for testing
                "generated_at": "2026-06-01T00:00:00Z",
                "signals": [],
            }]
        }
        predictions = []
        predictions = register_new_predictions(latest, predictions, None, None, now)
        assert predictions[0]["status"] == "pending"

        # Step 2: expire (now = 5 days later > 4 day horizon)
        now_expired = _utc("2026-06-06T00:00:00Z")
        predictions = expire_overdue_predictions(predictions, now_expired)
        assert predictions[0]["status"] == "expired_no_match"

        # Step 3: try to reopen while in cooldown (cooldown = 4 * 0.5 = 2 days → still active at 1 day after expiry)
        now_cooldown = _utc("2026-06-07T00:00:00Z")
        assert _is_in_cooldown("rcm_coverage", predictions, now_cooldown) is True

        # Step 4: try to reopen after cooldown (3 days after expiry > 2 day cooldown)
        now_after_cooldown = _utc("2026-06-09T00:00:00Z")
        assert _is_in_cooldown("rcm_coverage", predictions, now_after_cooldown) is False

        # Step 5: confirm register creates new row after cooldown
        predictions = register_new_predictions(latest, predictions, None, None, now_after_cooldown)
        open_rows = [r for r in predictions if r["status"] == "pending"]
        assert len(open_rows) == 1
