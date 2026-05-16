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
    import gc
    from scrapers.sources import (
        CBICCircularScraper,
        GSTCouncilScraper,
        AARRulingScraper,
        BudgetSpeechScraper,
        IndianKanoonScraper,
        ICAIRepresentationScraper,
        PIBFinanceScraper,
        ParliamentaryQuestionsScraper,
    )
    from scrapers.twitter import XScraper
    from scrapers.base import release_docling, disable_ocr
    from processors.tagger import TopicTagger
    from processors.chunker import Chunker
    from processors.embedder import Embedder

    if getattr(args, "no_ocr", False):
        disable_ocr()

    SCRAPERS = {
        # Original signal sources
        "cbic_circulars": CBICCircularScraper,
        "gst_council_minutes": GSTCouncilScraper,
        "aar_rulings": AARRulingScraper,
        "budget_speeches": BudgetSpeechScraper,
        # Phase 2 proprietary corpus — earlier-stage signals
        "court_judgments": IndianKanoonScraper,
        "icai_representations": ICAIRepresentationScraper,
        "pib_finance": PIBFinanceScraper,
        "parliamentary_questions": ParliamentaryQuestionsScraper,
        # Phase 3 — real-time social signal
        "twitter_signal": XScraper,
    }

    tagger = TopicTagger()
    chunker = Chunker()
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
        del docs, scraper
        # Release Docling models between sources — they hold GB-scale ML weights
        release_docling()
        gc.collect()

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

        # Chunk (always) + embed (skipped when --skip-embed is set).
        # Embedder is created inside the loop and released after each source
        # so the ML model doesn't accumulate across all 8 sources.
        chunked = 0
        embedded = 0
        paths_to_chunk = [
            p for p in Path("data/processed").glob("*.json")
            if not chunker.chunks_exist(p)
        ]
        for path in paths_to_chunk:
            chunks = chunker.chunk_and_save(path)
            if chunks:
                chunked += 1

        if chunked:
            print(f"[ingest] {source_id}: chunked {chunked} docs")

        if not getattr(args, "skip_embed", False) and paths_to_chunk:
            embedder = Embedder()
            for path in paths_to_chunk:
                chunk_path = Path("data/chunks") / path.name
                if not chunk_path.exists():
                    continue
                chunks = json.loads(chunk_path.read_text())
                if chunks:
                    embedded += embedder.embed_chunks(chunks)
            del embedder
            gc.collect()
            if embedded:
                print(f"[ingest] {source_id}: {embedded} new vectors indexed to Supabase")


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


def cmd_reextract(args):
    """Re-fetch PDFs for raw docs where full_text_extracted=False.

    Overwrites the raw doc with full text, then deletes the corresponding
    processed/ and chunks/ files so the tagger + chunker re-process them.
    """
    import gc
    from scrapers.base import BaseScraper, Document

    # Minimal scraper just for fetch_pdf_text access
    class _Fetcher(BaseScraper):
        source_id = "_reextract"
        def scrape(self): return []

    raw_dir = Path("data/raw")
    processed_dir = Path("data/processed")
    chunks_dir = Path("data/chunks")

    if not raw_dir.exists():
        print("data/raw not found — run ingest first.")
        return

    fetcher = _Fetcher()
    updated = skipped = failed = 0

    for source_dir in sorted(raw_dir.iterdir()):
        if not source_dir.is_dir():
            continue
        for raw_path in sorted(source_dir.glob("*.json")):
            doc = json.loads(raw_path.read_text())
            if doc.get("metadata", {}).get("full_text_extracted"):
                skipped += 1
                continue
            url = doc.get("url", "")
            if not url:
                skipped += 1
                continue

            print(f"[reextract] {raw_path.name} — fetching {url[:70]}...")
            try:
                if url.lower().endswith(".pdf"):
                    text = fetcher.fetch_pdf_text(url)
                else:
                    # HTML page — extract visible text
                    soup = fetcher.fetch_html(url)
                    # Remove script/style noise
                    for tag in soup(["script", "style", "nav", "header", "footer"]):
                        tag.decompose()
                    text = soup.get_text(separator="\n", strip=True) or None
            except Exception as e:
                print(f"  ERROR: {e}")
                failed += 1
                continue

            if not text:
                print(f"  SKIP — no text extracted")
                failed += 1
                continue

            doc["content"] = text
            doc["metadata"]["full_text_extracted"] = True
            raw_path.write_text(json.dumps(doc, indent=2, default=str))

            # Cascade-delete processed/chunks so the pipeline re-generates them
            for stale_dir in (processed_dir, chunks_dir):
                stale = stale_dir / raw_path.name
                if stale.exists():
                    stale.unlink()

            print(f"  OK — {len(text):,} chars extracted")
            updated += 1

    fetcher.client.close()
    gc.collect()
    print(f"\n[reextract] done — {updated} updated, {skipped} skipped (already extracted), {failed} failed")


def cmd_embed(_args):
    """Embed all un-indexed chunks. Run separately when memory allows."""
    import gc, json as _json
    from processors.embedder import Embedder

    chunks_dir = Path("data/chunks")
    if not chunks_dir.exists():
        print("No chunks found — run ingest first.")
        return

    embedder = Embedder()
    total_embedded = 0
    total_pushed = 0

    for chunk_path in sorted(chunks_dir.glob("*.json")):
        chunks = _json.loads(chunk_path.read_text())
        if not chunks:
            continue
        new_vecs = embedder.embed_chunks(chunks)
        if new_vecs:
            total_embedded += new_vecs

    del embedder
    gc.collect()
    print(f"[embed] done — {total_embedded} new vectors indexed to Supabase")


def main():
    parser = argparse.ArgumentParser(prog="gst_foresight")
    sub = parser.add_subparsers(dest="command")

    ingest_parser = sub.add_parser("ingest", help="Scrape and process data sources")
    ingest_parser.add_argument("--all", action="store_true", help="Run all active scrapers")
    ingest_parser.add_argument("--source", help="Run a specific source scraper")
    ingest_parser.add_argument(
        "--skip-embed", action="store_true",
        help="Scrape and chunk only — skip embedding (use on memory-constrained machines)"
    )
    ingest_parser.add_argument(
        "--no-ocr", action="store_true",
        help="Skip Docling/RapidOCR entirely — avoids loading large ML models (use when memory is constrained)"
    )

    sub.add_parser("embed", help="Embed all chunked docs into ChromaDB (run separately if ingest --skip-embed was used)")
    sub.add_parser("reextract", help="Re-fetch PDFs for raw docs where full_text_extracted=False")
    sub.add_parser("predict", help="Generate predictions from processed data")
    sub.add_parser("status", help="Show data and prediction status")

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "embed":
        cmd_embed(args)
    elif args.command == "reextract":
        cmd_reextract(args)
    elif args.command == "predict":
        cmd_predict(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
