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

        # Short-form content (tweets ≤280 chars): emit as a single chunk.
        # Avoids the advance≤0 edge case and makes intent explicit.
        if len(text) <= 400:
            return [{
                "chunk_id": f"{doc['doc_id']}_chunk_0",
                "doc_id": doc["doc_id"],
                "source_id": doc.get("source_id", ""),
                "date": doc.get("date"),
                "topic_tags": doc.get("topic_tags", []),
                "topic_scores": doc.get("topic_scores", {}),
                "text": text,
                "chunk_index": 0,
                "char_start": 0,
            }]

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

            advance = len(chunk_text) - self.overlap
            if advance <= 0:
                # Remaining text is shorter than overlap — we're at the end
                break
            start = start + advance
            idx += 1

        return chunks

    def chunk_and_save(self, processed_path: Path) -> list[dict]:
        """Read a processed doc, chunk it, save chunks. Returns chunk list."""
        name = processed_path.name
        try:
            print(f"[chunker] reading {name}...", flush=True)
            raw = processed_path.read_text()
            print(f"[chunker] parsing JSON ({len(raw):,} bytes)...", flush=True)
            doc = json.loads(raw)

            content_len = len((doc.get("content") or ""))
            print(f"[chunker] chunking {name} ({content_len:,} chars)...", flush=True)
            chunks = self.chunk(doc)

            if not chunks:
                print(f"[chunker] SKIP {name} — no content", flush=True)
                return []

            out_path = CHUNKS_DIR / name
            print(f"[chunker] writing {len(chunks)} chunks → {out_path.name}...", flush=True)
            out_path.write_text(json.dumps(chunks, indent=2, default=str))
            print(f"[chunker] done {name} → {len(chunks)} chunks", flush=True)
            return chunks

        except json.JSONDecodeError as e:
            print(f"[chunker] JSON error on {name}: {e}", flush=True)
            return []
        except Exception as e:
            print(f"[chunker] ERROR on {name}: {type(e).__name__}: {e}", flush=True)
            return []

    def chunks_exist(self, processed_path: Path) -> bool:
        return (CHUNKS_DIR / processed_path.name).exists()
