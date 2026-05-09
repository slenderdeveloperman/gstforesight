"""
processors/chunker.py — Splits full-text documents into overlapping chunks.

Character-based splitting (~3000 chars ≈ 750 tokens) with sentence-boundary
awareness and overlap to prevent signal loss at chunk edges.
"""

from pathlib import Path
import json

CHUNK_CHARS = 3000    # ~750 tokens for English legal text
OVERLAP_CHARS = 400   # ~100 tokens — preserves context across boundaries

CHUNKS_DIR = Path(__file__).parent.parent / "data" / "chunks"


class Chunker:
    def __init__(self, chunk_size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS):
        self.chunk_size = chunk_size
        self.overlap = overlap
        CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

    def chunk(self, doc: dict) -> list[dict]:
        """Split a processed document dict into overlapping chunk dicts."""
        text = (doc.get("content") or "").strip()
        if not text:
            return []

        chunks = []
        start = 0
        idx = 0

        while start < len(text):
            end = start + self.chunk_size
            chunk_text = text[start:end]

            # Break at nearest sentence boundary to avoid mid-clause cuts
            if end < len(text):
                last_period = chunk_text.rfind(". ")
                if last_period > self.chunk_size * 0.5:
                    chunk_text = chunk_text[: last_period + 1]

            chunk_text = chunk_text.strip()
            if not chunk_text:
                break

            chunks.append({
                "chunk_id": f"{doc['doc_id']}_chunk_{idx}",
                "doc_id": doc["doc_id"],
                "source_id": doc.get("source_id", ""),
                "date": doc.get("date"),
                "topic_tags": doc.get("topic_tags", []),
                "topic_scores": doc.get("topic_scores", {}),
                "text": chunk_text,
                "chunk_index": idx,
                "char_start": start,
            })

            start = start + len(chunk_text) - self.overlap
            idx += 1

        return chunks

    def chunk_and_save(self, processed_path: Path) -> list[dict]:
        """Read a processed doc, chunk it, save chunks. Returns chunk list."""
        try:
            doc = json.loads(processed_path.read_text())
            chunks = self.chunk(doc)
            if chunks:
                out_path = CHUNKS_DIR / processed_path.name
                out_path.write_text(json.dumps(chunks, indent=2, default=str))
            return chunks
        except Exception as e:
            print(f"[chunker] error on {processed_path.name}: {e}")
            return []

    def chunks_exist(self, processed_path: Path) -> bool:
        return (CHUNKS_DIR / processed_path.name).exists()
