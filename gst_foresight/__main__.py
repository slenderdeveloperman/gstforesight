"""
gst_foresight/__main__.py — CLI entry point.

Usage:
    python -m gst_foresight ingest --all
    python -m gst_foresight ingest --source cbic_circulars
    python -m gst_foresight predict
    python -m gst_foresight status
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def cmd_ingest(args):
    from scrapers.sources import (
        CBICCircularScraper,
        GSTCouncilScraper,
        AARRulingScraper,
        BudgetSpeechScraper,
    )
    from processors.tagger import TopicTagger
    from processors.chunker import Chunker
    from processors.embedder import Embedder

    SCRAPERS = {
        "cbic_circulars": CBICCircularScraper,
        "gst_council_minutes": GSTCouncilScraper,
        "aar_rulings": AARRulingScraper,
        "budget_speeches": BudgetSpeechScraper,
    }

    tagger = TopicTagger()
    chunker = Chunker()
    embedder = Embedder()
    to_run = list(SCRAPERS.keys()) if args.all else [args.source]

    for source_id in to_run:
        if source_id not in SCRAPERS:
            print(f"[ingest] unknown source: {source_id}")
            continue

        print(f"[ingest] scraping {source_id}...")
        scraper = SCRAPERS[source_id]()
        docs = scraper.scrape()
        new_count = scraper.save(docs)
        print(f"[ingest] {source_id}: {len(docs)} found, {new_count} new")

        # Tag new documents
        raw_dir = Path("data/raw") / source_id
        tagged = 0
        for path in raw_dir.glob("*.json"):
            processed_path = Path("data/processed") / path.name
            if not processed_path.exists():
                result = tagger.tag_and_save(path)
                if result:
                    tagged += 1
        print(f"[ingest] {source_id}: tagged {tagged} new documents")

        # Chunk + embed new processed documents
        chunked = 0
        embedded = 0
        for path in Path("data/processed").glob("*.json"):
            if chunker.chunks_exist(path):
                continue
            chunks = chunker.chunk_and_save(path)
            if chunks:
                chunked += 1
                new_vectors = embedder.embed_chunks(chunks)
                embedded += new_vectors
        if chunked:
            print(f"[ingest] {source_id}: chunked {chunked} docs → {embedded} new vectors")


def cmd_predict(args):
    from predictors.engine import PredictionEngine
    engine = PredictionEngine()
    predictions = engine.run()

    if not predictions:
        print("No predictions generated. Run ingest first.")
        return

    print(f"\n{'='*60}")
    print(f"GST FORESIGHT — {len(predictions)} active predictions")
    print(f"{'='*60}\n")

    for p in predictions[:10]:  # show top 10
        bar = "█" * int(p["probability"] / 10) + "░" * (10 - int(p["probability"] / 10))
        print(f"  {p['probability']:>4}% [{bar}] {p['topic_label']}")
        print(f"         Horizon: {p['horizon_label']}")
        print(f"         Signals: {', '.join(s['type'] for s in p['signals'])}")
        print()


def cmd_status(args):
    raw_dir = Path("data/raw")
    processed_dir = Path("data/processed")
    chunks_dir = Path("data/chunks")
    vectors_dir = Path("data/vectors")
    predictions_dir = Path("data/predictions")

    print("\ngst-foresight status\n")

    for source_dir in sorted(raw_dir.iterdir()) if raw_dir.exists() else []:
        count = len(list(source_dir.glob("*.json")))
        full_text = sum(
            1 for p in source_dir.glob("*.json")
            if json.loads(p.read_text()).get("metadata", {}).get("full_text_extracted")
        )
        print(f"  {source_dir.name:<30} {count:>4} docs  ({full_text} with full text)")

    processed = len(list(processed_dir.glob("*.json"))) if processed_dir.exists() else 0
    chunked = len(list(chunks_dir.glob("*.json"))) if chunks_dir.exists() else 0
    print(f"\n  processed (tagged):   {processed}")
    print(f"  chunked:              {chunked}")

    if vectors_dir.exists():
        try:
            from processors.embedder import Embedder
            stats = Embedder().stats()
            print(f"  vectors (ChromaDB):   {stats['total_chunks']} chunks indexed")
        except Exception:
            print(f"  vectors:              dir exists (run ingest to index)")
    else:
        print(f"  vectors:              none yet — run ingest")

    latest = predictions_dir / "latest.json"
    if latest.exists():
        data = json.loads(latest.read_text())
        print(f"\n  predictions:          {data['prediction_count']} active (generated {data['generated_at'][:10]})")
    else:
        print(f"\n  predictions:          none yet — run `python -m gst_foresight predict`")


def main():
    parser = argparse.ArgumentParser(prog="gst_foresight")
    sub = parser.add_subparsers(dest="command")

    ingest_parser = sub.add_parser("ingest", help="Scrape and process data sources")
    ingest_parser.add_argument("--all", action="store_true", help="Run all active scrapers")
    ingest_parser.add_argument("--source", help="Run a specific source scraper")

    sub.add_parser("predict", help="Generate predictions from processed data")
    sub.add_parser("status", help="Show data and prediction status")

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "predict":
        cmd_predict(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
