"""
processors/tagger.py — Tags documents with topic labels from the taxonomy.

This is the core NLP step. Documents come in as raw text; they go out
tagged with one or more topic IDs from config/sources.yaml.

Approach: keyword matching first (fast, transparent, tunable),
with optional LLM-based tagging for ambiguous documents.
"""

import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import yaml

# Cap text passed to regex — prevents ReDoS on unexpectedly large scraped documents.
_MAX_TAG_CHARS = 50_000


CONFIG_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"
PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"


# Topic keyword maps — the vocabulary that maps document text to topic IDs.
# Tuned manually. Add terms when you see missed tags. Remove false positives.
# Each entry: topic_id → list of keyword patterns (regex-compatible)

TOPIC_KEYWORDS = {
    "itc_eligibility": [
        r"input tax credit",
        r"\bitc\b",
        r"section 16",
        r"section 17",
        r"rule 36",
        r"rule 37a",
        r"blocked credit",
        r"eligib\w+ for credit",
        r"reversal of credit",
        r"gstr-2b",
        r"ims",
        r"invoice management",
    ],
    "rcm_coverage": [
        r"reverse charge",
        r"\brcm\b",
        r"section 9\(3\)",
        r"section 9\(4\)",
        r"unregistered",
    ],
    "rate_rationalisation": [
        r"rate\w* of tax",
        r"rate\w* rationaliz",
        r"rate\w* change",
        r"gst rate",
        r"tax rate",
        r"exempt\w+",
        r"nil rat",
        r"5%|12%|18%|28%",
        r"cess",
    ],
    "return_format": [
        r"gstr-1\b",
        r"gstr-3b",
        r"gstr-9",
        r"return format",
        r"annual return",
        r"filing process",
        r"qrmp",
        r"rule 61",
        r"rule 80",
    ],
    "ims_itc_flow": [
        r"invoice management system",
        r"\bims\b",
        r"gstr-2b",
        r"rule 60b",
        r"accept.*invoice",
        r"reject.*invoice",
        r"deemed accept",
    ],
    "e_invoicing": [
        r"e.?invoic\w+",
        r"electronic invoic\w+",
        r"irn\b",
        r"invoice registration",
        r"rule 48",
        r"e.?invoice threshold",
    ],
    "classification_disputes": [
        r"hsn code",
        r"classif\w+",
        r"composite supply",
        r"mixed supply",
        r"works contract",
        r"advance ruling",
        r"\baar\b",
        r"tariff heading",
    ],
    "valuation": [
        r"valuation",
        r"transaction value",
        r"related party",
        r"discount",
        r"rule 2[7-9]|rule 3[0-5]",
        r"open market value",
    ],
    "place_of_supply": [
        r"place of supply",
        r"oidar",
        r"intermediary",
        r"cross.border",
        r"section 12|section 13",
        r"export of service",
    ],
    "gst_on_crypto_vda": [
        r"virtual digital asset",
        r"\bvda\b",
        r"crypto\w*",
        r"nft\b",
        r"digital asset",
        r"blockchain",
    ],
    "msme_composition": [
        r"composition scheme",
        r"threshold limit",
        r"aggregate turnover",
        r"small taxpayer",
        r"\bmsme\b",
        r"section 10\b",
    ],
    "real_estate": [
        r"real estate",
        r"construction service",
        r"affordable housing",
        r"works contract",
        r"flat|apartment",
        r"notification 11/2017",
        r"section 17\(5\)",
    ],
}


class TopicTagger:
    """Tags a document with matching topic IDs."""

    def __init__(self):
        with open(CONFIG_PATH) as f:
            self.config = yaml.safe_load(f)
        self.topic_ids = [t["id"] for t in self.config.get("topics", [])]
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    def tag(self, doc_dict: dict) -> dict:
        """Add topic_tags list to a document dict. Returns enriched dict."""
        raw_text = (
            (doc_dict.get("title") or "") + " " +
            (doc_dict.get("content") or "")
        )
        text = raw_text.lower()

        topic_scores = {}

        # Scan in bounded windows rather than truncating to the first
        # _MAX_TAG_CHARS chars — GST Council minutes and other large PDFs
        # routinely run 300K+ chars, so a flat truncation silently ignores
        # everything past the first ~50K chars (roughly the first 14% of a
        # council minutes doc). Each regex call still only ever sees a
        # bounded _MAX_TAG_CHARS-sized window, so the ReDoS protection this
        # cap exists for is preserved.
        for window_start in range(0, max(len(text), 1), _MAX_TAG_CHARS):
            window = text[window_start:window_start + _MAX_TAG_CHARS]
            for topic_id, patterns in TOPIC_KEYWORDS.items():
                score = 0
                for pattern in patterns:
                    try:
                        matches = re.findall(pattern, window, re.IGNORECASE)
                        score += len(matches)
                    except re.error:
                        # Malformed pattern — skip rather than crash
                        pass
                if score > 0:
                    topic_scores[topic_id] = topic_scores.get(topic_id, 0) + score

        matched_topics = [
            t for t, s in sorted(topic_scores.items(), key=lambda x: -x[1])
            if s >= 1
        ]

        doc_dict["topic_tags"] = matched_topics
        doc_dict["topic_scores"] = topic_scores
        doc_dict["tagged_at"] = datetime.now(timezone.utc).isoformat()
        return doc_dict

    def tag_and_save(self, raw_path: Path) -> Optional[dict]:
        """Read a raw doc, tag it, save to processed/. Returns tagged doc."""
        try:
            doc = json.loads(raw_path.read_text())
            tagged = self.tag(doc)
            out_path = PROCESSED_DIR / raw_path.name
            out_path.write_text(json.dumps(tagged, indent=2))
            return tagged
        except Exception as e:
            print(f"[tagger] error on {raw_path.name}: {e}")
            return None

    def add_keywords(self, topic_id: str, patterns: list[str]):
        """
        Add keyword patterns to a topic at runtime.
        Call this when you notice a topic is being missed.
        
        Example:
            tagger.add_keywords("rcm_coverage", [r"uber|swiggy|zomato"])
        """
        if topic_id in TOPIC_KEYWORDS:
            TOPIC_KEYWORDS[topic_id].extend(patterns)
        else:
            TOPIC_KEYWORDS[topic_id] = patterns
        print(f"[tagger] added {len(patterns)} patterns to '{topic_id}'")
