"""
processors/embedder.py — Embeds document chunks and stores in ChromaDB.

Uses sentence-transformers all-MiniLM-L6-v2 (384 dims, ~80MB, runs fully
locally — no API key needed). ChromaDB persists to data/vectors/ as flat
files that can be committed to the repo for the query Worker in Phase 2.
"""

from pathlib import Path
from typing import Optional

VECTORS_DIR = Path(__file__).parent.parent / "data" / "vectors"
COLLECTION_NAME = "gst_corpus"
MODEL_NAME = "all-MiniLM-L6-v2"
BATCH_SIZE = 64  # embed in batches to avoid OOM on large corpora


class Embedder:
    def __init__(self):
        VECTORS_DIR.mkdir(parents=True, exist_ok=True)

        # Lazy imports — only required if embedder is actually used
        import chromadb
        from sentence_transformers import SentenceTransformer

        self.client = chromadb.PersistentClient(path=str(VECTORS_DIR))
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        print(f"[embedder] loading model {MODEL_NAME}...")
        self.model = SentenceTransformer(MODEL_NAME)
        print(f"[embedder] model ready — {self.collection.count()} chunks already indexed")

    def embed_chunks(self, chunks: list[dict]) -> int:
        """Embed and store chunks. Skips already-indexed chunk IDs. Returns new count."""
        if not chunks:
            return 0

        chunk_ids = [c["chunk_id"] for c in chunks]

        # Check which IDs are already in the collection
        existing = set(self.collection.get(ids=chunk_ids)["ids"])
        new_chunks = [c for c in chunks if c["chunk_id"] not in existing]

        if not new_chunks:
            return 0

        new_count = 0
        for i in range(0, len(new_chunks), BATCH_SIZE):
            batch = new_chunks[i: i + BATCH_SIZE]
            texts = [c["text"] for c in batch]

            embeddings = self.model.encode(
                texts,
                show_progress_bar=False,
                convert_to_numpy=True,
            ).tolist()

            self.collection.add(
                ids=[c["chunk_id"] for c in batch],
                embeddings=embeddings,
                documents=texts,
                metadatas=[
                    {
                        "doc_id": c["doc_id"],
                        "source_id": c["source_id"],
                        "date": c.get("date") or "",
                        "topic_tags": ",".join(c.get("topic_tags", [])),
                        "chunk_index": c["chunk_index"],
                    }
                    for c in batch
                ],
            )
            new_count += len(batch)

        return new_count

    def query(
        self,
        text: str,
        n_results: int = 8,
        source_filter: Optional[str] = None,
        topic_filter: Optional[str] = None,
    ) -> list[dict]:
        """
        Semantic search over the corpus.
        Returns list of {text, metadata, distance} dicts, closest first.
        """
        embedding = self.model.encode([text]).tolist()

        where = None
        if source_filter:
            where = {"source_id": source_filter}
        elif topic_filter:
            where = {"topic_tags": {"$contains": topic_filter}}

        results = self.collection.query(
            query_embeddings=embedding,
            n_results=min(n_results, self.collection.count()),
            where=where,
        )

        out = []
        for i, doc_text in enumerate(results["documents"][0]):
            out.append({
                "text": doc_text,
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i],
            })
        return out

    def push_to_supabase(self, chunks: list[dict]) -> int:
        """
        Upsert chunks + pre-computed embeddings into Supabase (pgvector).
        Called by the ingest pipeline after embed_chunks().

        Requires env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY
        No-ops silently if env vars are absent (safe for local dev).
        """
        import os
        import httpx as _httpx

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            return 0

        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",  # upsert on conflict
        }

        # Compute embeddings for this batch
        texts = [c["text"] for c in chunks]
        embeddings = self.model.encode(
            texts, show_progress_bar=False, convert_to_numpy=True
        ).tolist()

        rows = [
            {
                "id": c["chunk_id"],
                "doc_id": c["doc_id"],
                "source_id": c["source_id"],
                "date": c.get("date") or None,
                "topic_tags": ",".join(c.get("topic_tags", [])),
                "chunk_index": c["chunk_index"],
                "content": c["text"],
                "embedding": emb,
            }
            for c, emb in zip(chunks, embeddings)
        ]

        upserted = 0
        for i in range(0, len(rows), 100):
            batch = rows[i : i + 100]
            try:
                res = _httpx.post(
                    f"{url}/rest/v1/chunks",
                    headers=headers,
                    json=batch,
                    timeout=60,
                )
                if res.status_code in (200, 201):
                    upserted += len(batch)
                else:
                    print(f"[supabase] upsert error: {res.status_code} {res.text[:200]}")
            except Exception as e:
                print(f"[supabase] request failed: {e}")

        return upserted

    def stats(self) -> dict:
        return {
            "total_chunks": self.collection.count(),
            "model": MODEL_NAME,
            "vectors_dir": str(VECTORS_DIR),
        }
