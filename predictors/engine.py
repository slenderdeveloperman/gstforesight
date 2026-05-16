"""
predictors/engine.py — Core prediction logic.

Takes processed, tagged documents and computes probability-weighted
predictions for upcoming GST rule changes.

The model is intentionally transparent:
- Every prediction links to the signals that generated it
- Probabilities are derived from signal weights in config
- Backtest accuracy is tracked per signal type

How to add a new signal type:
1. Add it to SIGNAL_EVALUATORS below
2. Add its weight to config/sources.yaml signal_weights
3. Add backtest cases to tests/backtest_cases.json
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import yaml


CONFIG_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"
PREDICTIONS_DIR = Path(__file__).parent.parent / "data" / "predictions"


class Signal:
    """A single piece of evidence contributing to a prediction."""

    def __init__(
        self,
        signal_type: str,
        topic_id: str,
        strength: float,       # 0–1
        description: str,
        source_docs: list[str],
        horizon_days: int,
    ):
        self.signal_type = signal_type
        self.topic_id = topic_id
        self.strength = strength
        self.description = description
        self.source_docs = source_docs
        self.horizon_days = horizon_days


class Prediction:
    """A probability-weighted prediction for a topic changing."""

    def __init__(self, topic_id: str, topic_label: str):
        self.topic_id = topic_id
        self.topic_label = topic_label
        self.signals: list[Signal] = []
        self.probability: float = 0.0
        self.horizon_label: str = ""
        self.horizon_days: int = 0
        self.generated_at: str = datetime.utcnow().isoformat() + "Z"

    def add_signal(self, signal: Signal):
        self.signals.append(signal)

    def compute_probability(self, weights: dict) -> float:
        """
        Combine signals into a final probability.
        Uses weighted average with diminishing returns on additional signals
        (the 3rd signal of the same type adds less than the 1st).
        """
        if not self.signals:
            return 0.0

        # Group by signal type for diminishing returns
        by_type = defaultdict(list)
        for s in self.signals:
            by_type[s.signal_type].append(s)

        total_weight = 0.0
        weighted_sum = 0.0

        for sig_type, sigs in by_type.items():
            base_weight = weights.get(sig_type, 0.10)
            for i, sig in enumerate(sigs):
                # Diminishing returns: 1st signal full weight, 2nd 60%, 3rd 40%
                decay = 1.0 / (1 + i * 0.6)
                w = base_weight * decay
                weighted_sum += sig.strength * w
                total_weight += w

        raw_prob = weighted_sum / total_weight if total_weight > 0 else 0
        # Cap at 95% — we never predict certainty
        self.probability = round(min(raw_prob * 100, 95), 1)
        return self.probability

    def compute_horizon(self) -> str:
        """Derive expected timeframe from signal types present."""
        sig_types = {s.signal_type for s in self.signals}

        if "social_regulatory_signal" in sig_types:
            min_horizon = min(
                s.horizon_days for s in self.signals
                if s.signal_type == "social_regulatory_signal"
            )
            self.horizon_days = min_horizon
            self.horizon_label = (
                "Imminent — within 30 days"
                if min_horizon <= 30
                else "Next GST Council meeting"
            )
        elif "council_deferred_item" in sig_types:
            self.horizon_days = 90
            self.horizon_label = "Next GST Council meeting"
        elif "government_forward_signal" in sig_types:
            # PIB signals are more proximate than budget language
            min_horizon = min(
                s.horizon_days for s in self.signals
                if s.signal_type == "government_forward_signal"
            )
            self.horizon_days = min_horizon
            self.horizon_label = (
                "Next GST Council meeting"
                if min_horizon <= 90
                else "Next Union Budget / 2 Council meetings"
            )
        elif "budget_speech_phrase" in sig_types:
            self.horizon_days = 180
            self.horizon_label = "Next Union Budget / 2 Council meetings"
        elif "repeated_circular_topic" in sig_types:
            self.horizon_days = 180
            self.horizon_label = "2–3 quarters"
        else:
            self.horizon_days = 270
            self.horizon_label = "Next FY"

        return self.horizon_label

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "topic_label": self.topic_label,
            "probability": self.probability,
            "horizon_label": self.horizon_label,
            "horizon_days": self.horizon_days,
            "signal_count": len(self.signals),
            "signals": [
                {
                    "type": s.signal_type,
                    "strength": s.strength,
                    "description": s.description,
                    "source_docs": s.source_docs[:5],  # cap for readability
                }
                for s in self.signals
            ],
            "generated_at": self.generated_at,
        }


class PredictionEngine:
    """
    Main engine. Reads processed documents, evaluates signals, generates predictions.
    """

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self.config = yaml.safe_load(f)
        self.signal_weights = self.config["prediction"]["signal_weights"]
        self.min_prob = self.config["prediction"]["min_probability_to_surface"]
        self.min_signals = self.config["prediction"]["min_signals_to_predict"]
        self.topics = {t["id"]: t["label"] for t in self.config["topics"]}
        PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)

    def load_processed_docs(self) -> list[dict]:
        docs = []
        for path in PROCESSED_DIR.glob("*.json"):
            try:
                docs.append(json.loads(path.read_text()))
            except Exception:
                continue
        return docs

    def evaluate_repeated_circular_topic(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: same topic clarified 2+ times in CBIC circulars.
        Implies ongoing ambiguity — more clarification likely.
        """
        relevant = [
            d for d in docs
            if d.get("source_id") == "cbic_circulars"
            and topic_id in d.get("topic_tags", [])
        ]
        if len(relevant) < 2:
            return None

        # More circulars on same topic = stronger signal
        count = len(relevant)
        strength = min(0.3 + (count - 2) * 0.15, 0.9)

        return Signal(
            signal_type="repeated_circular_topic",
            topic_id=topic_id,
            strength=strength,
            description=f"{count} CBIC circulars on this topic — ongoing ambiguity suggests further clarification likely",
            source_docs=[d["doc_id"] for d in relevant[-5:]],
            horizon_days=180,
        )

    def evaluate_council_deferred_item(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: topic appears in GST Council minutes as deferred.
        Strength scales with deferral count — a third deferral is statistically
        rare and implies imminent resolution.
        """
        council_docs = [
            d for d in docs
            if d.get("source_id") == "gst_council_minutes"
            and topic_id in d.get("topic_tags", [])
        ]
        deferred = [
            d for d in council_docs
            if any(
                kw in (d.get("content") or "").lower()
                for kw in ["defer", "next meeting", "further deliberation", "examine further",
                           "kept in abeyance", "referred back", "pending decision"]
            )
        ]
        if not deferred:
            return None

        count = len(deferred)
        # 1 deferral → 0.65, 2 → 0.80, 3+ → 0.92
        strength = min(0.55 + count * 0.15, 0.92)

        return Signal(
            signal_type="council_deferred_item",
            topic_id=topic_id,
            strength=round(strength, 2),
            description=f"Item deferred in {count} GST Council meeting(s) — {'repeated deferrals indicate imminent resolution' if count >= 2 else 'deferred items typically resurface within 1–2 meetings'}",
            source_docs=[d["doc_id"] for d in deferred],
            horizon_days=90,
        )

    def evaluate_aar_ruling_frequency(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: 3+ AARs on same topic in rolling 12 months.
        High AAR frequency on a topic = disputed area = CBIC will clarify.
        """
        cutoff = datetime.utcnow() - timedelta(days=365)
        relevant = []
        for d in docs:
            if d.get("source_id") != "aar_rulings":
                continue
            if topic_id not in d.get("topic_tags", []):
                continue
            date_str = d.get("date")
            if date_str:
                try:
                    if datetime.fromisoformat(date_str) < cutoff:
                        continue
                except Exception:
                    pass
            relevant.append(d)

        if len(relevant) < 3:
            return None

        strength = min(0.2 + len(relevant) * 0.08, 0.75)
        return Signal(
            signal_type="aar_ruling_frequency",
            topic_id=topic_id,
            strength=strength,
            description=f"{len(relevant)} AAR rulings on this topic in last 12 months — judicial pressure likely to trigger CBIC clarification",
            source_docs=[d["doc_id"] for d in relevant[-5:]],
            horizon_days=180,
        )

    def evaluate_budget_speech_phrase(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: topic mentioned with action language in budget speech.
        Strength decays with age — a 2025 mention is far more predictive
        than a 2022 mention that went unacted on.
        """
        budget_docs = [
            d for d in docs
            if d.get("source_id") == "budget_speeches"
            and topic_id in d.get("topic_tags", [])
        ]
        action_phrases = [
            "rationalise", "simplify", "review", "examine", "bring within",
            "relief", "reduce", "exempt", "clarif", "expand", "mandate", "extend"
        ]
        actionable = [
            d for d in budget_docs
            if any(p in (d.get("content") or "").lower() for p in action_phrases)
        ]
        if not actionable:
            return None

        current_year = datetime.utcnow().year
        # Score by recency: most recent mention drives the strength
        strength = 0.30
        years = []
        for d in actionable:
            year = d.get("metadata", {}).get("year") or d.get("date", "")[:4]
            years.append(str(year))
            try:
                age = current_year - int(year)
                # 0 yrs ago → 0.82, 1 yr → 0.68, 2 yrs → 0.54, 3+ → 0.40
                doc_strength = max(0.82 - age * 0.14, 0.30)
                strength = max(strength, doc_strength)
            except (ValueError, TypeError):
                pass

        return Signal(
            signal_type="budget_speech_phrase",
            topic_id=topic_id,
            strength=round(strength, 2),
            description=f"Budget speech ({', '.join(sorted(set(years), reverse=True))}) contained action language — {'recent signal, high predictive weight' if strength >= 0.68 else 'older signal, moderate predictive weight'}",
            source_docs=[d["doc_id"] for d in actionable],
            horizon_days=180,
        )

    def evaluate_judicial_split(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: rulings on the same topic from multiple distinct courts/tribunals.
        Multiple jurisdictions ruling on the same GST question creates interpretive
        divergence that CBIC almost always resolves with a clarificatory circular
        within 6–12 months. Detected structurally (distinct courts, same topic)
        rather than by keyword — works on short snippets, not just full texts.
        """
        relevant = [
            d for d in docs
            if d.get("source_id") in ["aar_rulings", "court_judgments", "high_court_orders"]
            and topic_id in (d.get("topic_tags") or [])
        ]
        if len(relevant) < 2:
            return None

        # Count distinct courts — a split requires at least 2 different forums
        courts = set()
        for d in relevant:
            court = (d.get("metadata") or {}).get("court", "").strip()
            if court:
                courts.add(court)

        if len(courts) < 2:
            return None

        # More rulings + more distinct courts = stronger signal
        ruling_factor = min(len(relevant) * 0.07, 0.30)
        court_factor  = min((len(courts) - 1) * 0.12, 0.30)
        strength = round(min(0.38 + ruling_factor + court_factor, 0.80), 2)

        court_list = ", ".join(sorted(courts)[:4])
        return Signal(
            signal_type="judicial_split",
            topic_id=topic_id,
            strength=strength,
            description=f"{len(relevant)} rulings across {len(courts)} courts/tribunals ({court_list}) — multi-forum litigation on the same GST issue reliably precedes CBIC clarification",
            source_docs=[d["doc_id"] for d in relevant[-5:]],
            horizon_days=180,
        )

    def evaluate_government_forward_signal(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: Finance Ministry (PIB) press release touches a GST topic
        with forward-looking action language.

        PIB releases are immediate ministry communications — a mention of
        "rationalise", "review", or "exempt" here is more proximate to
        policy action than the same phrase buried in a budget speech.
        Strength decays with age: a 30-day-old PIB mention is far more
        predictive than a 90-day-old one.
        """
        relevant = [
            d for d in docs
            if d.get("source_id") == "pib_finance"
            and topic_id in (d.get("topic_tags") or [])
        ]
        if not relevant:
            return None

        action_phrases = [
            "rationalise", "rationalize", "streamline", "simplify",
            "review", "examine", "consider", "study",
            "exempt", "relief", "reduce", "waive",
            "mandate", "expand", "extend", "include",
            "clarif", "address", "resolve",
            "to be notified", "will be issued", "shortly", "proposed",
            "working group", "committee", "task force",
        ]
        imminent_phrases = [
            "to be notified", "will be issued", "shortly", "proposed",
            "with effect from", "effective from",
        ]

        # Score each doc by recency and action language presence
        best_strength = 0.0
        best_description = ""
        actionable_docs = []
        today = datetime.utcnow()

        for d in relevant:
            content = ((d.get("title") or "") + " " + (d.get("content") or "")).lower()
            has_action = any(p in content for p in action_phrases)
            if not has_action:
                continue

            # Recency decay: ≤30 days → full weight, ≤60 → 0.75, ≤90 → 0.55, older → 0.40
            age_days = 999
            date_str = d.get("date")
            if date_str:
                try:
                    doc_date = datetime.fromisoformat(date_str.replace("Z", "+00:00").split("+")[0])
                    age_days = (today - doc_date).days
                except (ValueError, TypeError):
                    pass

            if age_days <= 30:
                base = 0.65
            elif age_days <= 60:
                base = 0.52
            elif age_days <= 90:
                base = 0.42
            else:
                base = 0.32

            # Imminent language bumps strength
            has_imminent = any(p in content for p in imminent_phrases)
            strength = min(base + (0.15 if has_imminent else 0.0), 0.88)

            if strength > best_strength:
                best_strength = strength
                recency_tag = f"{age_days}d ago" if age_days < 999 else "unknown date"
                imminent_note = " with imminent language" if has_imminent else ""
                best_description = (
                    f"PIB Finance Ministry release ({recency_tag}){imminent_note} "
                    f"on this topic — ministry-level attention typically precedes "
                    f"Council action within 1–2 meetings"
                )

            actionable_docs.append(d)

        if not actionable_docs:
            return None

        # Multiple mentions bump strength slightly (diminishing, capped)
        if len(actionable_docs) > 1:
            best_strength = min(best_strength + (len(actionable_docs) - 1) * 0.04, 0.88)
            best_description = (
                f"{len(actionable_docs)} PIB Finance releases on this topic — "
                + best_description.split("—", 1)[-1].strip()
            )

        # Horizon: imminent language → next meeting (90d), else 2 meetings (180d)
        horizon = 90 if "imminent language" in best_description else 180

        return Signal(
            signal_type="government_forward_signal",
            topic_id=topic_id,
            strength=round(best_strength, 2),
            description=best_description,
            source_docs=[d["doc_id"] for d in actionable_docs[-5:]],
            horizon_days=horizon,
        )

    def evaluate_industry_ask_repeat(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: same ask in 2+ consecutive industry memoranda.
        Persistent industry demand eventually gets addressed.
        """
        memo_docs = [
            d for d in docs
            if d.get("source_id") in ["icai_memoranda", "icai_representations", "ficci_submissions"]
            and topic_id in d.get("topic_tags", [])
        ]
        if len(memo_docs) < 2:
            return None

        return Signal(
            signal_type="industry_ask_repeat",
            topic_id=topic_id,
            strength=0.45,
            description=f"Repeated ask across {len(memo_docs)} industry submissions — government typically addresses persistent asks within 2–3 budget cycles",
            source_docs=[d["doc_id"] for d in memo_docs],
            horizon_days=365,
        )

    def evaluate_social_regulatory_signal(
        self, docs: list[dict], topic_id: str
    ) -> Optional[Signal]:
        """
        Signal: recent tweet from a verified government GST account touches
        a GST topic.

        Unlike PIB releases (government_forward_signal), tweets are:
          - Faster: posted minutes after an event, not hours/days later
          - Noisier: informal, sometimes aspirational, not always enacted
          - Stale quickly: a 60-day-old tweet with no follow-up is a dead signal

        Strength model:
          base                     = 0.30
          recency: ≤7d            += 0.20  |  ≤14d → +0.12  |  ≤30d → +0.05
          engagement: RT > 500    += 0.10  (viral government signal)
          imminent language       += 0.10  (notifi*, circular, effective, shortly)
          multi-tweet bonus       += 0.04 per extra tweet, capped at +0.12
          hard cap                 = 0.72  (tweets never dominate institutional signals)

        Tweets older than 60 days are dropped — X is a real-time channel,
        not an archive. A tweet that produced no follow-up policy action
        within 60 days is unlikely to do so.
        """
        IMMINENT_PHRASES = [
            "notifi", "circular", "effective from", "effective date",
            "shortly", "will be issued", "to be issued", "gazette",
            "gst council", "amendment",
        ]

        relevant = [
            d for d in docs
            if d.get("source_id") == "twitter_signal"
            and topic_id in (d.get("topic_tags") or [])
            and (d.get("metadata") or {}).get("account_type") == "government"
        ]
        if not relevant:
            return None

        today = datetime.utcnow()
        best_strength = 0.0
        has_imminent = False
        actionable: list[dict] = []

        for d in relevant:
            # Hard staleness cutoff — tweets older than 60 days are ignored
            age_days = 999
            date_str = d.get("date")
            if date_str:
                try:
                    doc_date = datetime.fromisoformat(
                        date_str.replace("Z", "+00:00").split("+")[0]
                    )
                    age_days = (today - doc_date).days
                except (ValueError, TypeError):
                    pass

            if age_days > 60:
                continue

            content = ((d.get("title") or "") + " " + (d.get("content") or "")).lower()

            recency_bonus = (
                0.20 if age_days <= 7
                else 0.12 if age_days <= 14
                else 0.05 if age_days <= 30
                else 0.0
            )

            rt_count = int((d.get("metadata") or {}).get("retweet_count", 0) or 0)
            engagement_bonus = 0.10 if rt_count > 500 else 0.0

            doc_imminent = any(p in content for p in IMMINENT_PHRASES)
            if doc_imminent:
                has_imminent = True
            imminent_bonus = 0.10 if doc_imminent else 0.0

            tweet_strength = min(0.30 + recency_bonus + engagement_bonus + imminent_bonus, 0.72)
            best_strength = max(best_strength, tweet_strength)
            actionable.append(d)

        if not actionable:
            return None

        # Diminishing multi-tweet boost (each extra matching tweet adds a little)
        extra_boost = min((len(actionable) - 1) * 0.04, 0.12)
        final_strength = min(best_strength + extra_boost, 0.72)

        usernames = sorted({
            (d.get("metadata") or {}).get("username", "unknown") for d in actionable
        })
        recency_label = f"{len(actionable)} tweet(s) in last 60 days"
        imminent_note = " — imminent regulatory language detected" if has_imminent else ""

        return Signal(
            signal_type="social_regulatory_signal",
            topic_id=topic_id,
            strength=round(final_strength, 2),
            description=(
                f"Government X accounts ({', '.join(f'@{u}' for u in usernames[:3])}) "
                f"posted on this topic — {recency_label}{imminent_note}"
            ),
            source_docs=[d["doc_id"] for d in actionable[-5:]],
            horizon_days=30 if has_imminent else 90,
        )

    def run(self) -> list[dict]:
        """Full prediction run. Returns list of prediction dicts."""
        docs = self.load_processed_docs()
        if not docs:
            print("[engine] no processed docs found — run ingest first")
            return []

        print(f"[engine] loaded {len(docs)} processed documents")

        evaluators = [
            self.evaluate_repeated_circular_topic,
            self.evaluate_council_deferred_item,
            self.evaluate_aar_ruling_frequency,
            self.evaluate_budget_speech_phrase,
            self.evaluate_government_forward_signal,
            self.evaluate_industry_ask_repeat,
            self.evaluate_judicial_split,
            self.evaluate_social_regulatory_signal,
        ]

        predictions = []

        for topic_id, topic_label in self.topics.items():
            pred = Prediction(topic_id, topic_label)

            for evaluator in evaluators:
                signal = evaluator(docs, topic_id)
                if signal:
                    pred.add_signal(signal)

            if len(pred.signals) < self.min_signals:
                continue

            pred.compute_probability(self.signal_weights)
            pred.compute_horizon()

            if pred.probability >= self.min_prob:
                predictions.append(pred)

        # Sort by probability descending
        predictions.sort(key=lambda p: -p.probability)

        output = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "doc_count": len(docs),
            "prediction_count": len(predictions),
            "predictions": [p.to_dict() for p in predictions],
        }

        # Save
        out_path = PREDICTIONS_DIR / "latest.json"
        out_path.write_text(json.dumps(output, indent=2))

        # Also save a timestamped snapshot
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
        snapshot_path = PREDICTIONS_DIR / f"snapshot_{ts}.json"
        snapshot_path.write_text(json.dumps(output, indent=2))

        print(f"[engine] generated {len(predictions)} predictions → {out_path}")
        return output["predictions"]
