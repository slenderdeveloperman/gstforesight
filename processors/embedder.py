"""
processors/embedder.py — Embeds document chunks using a local sentence-transformers model.

Ingest path  : SentenceTransformer('Supabase/gte-small') runs locally — zero API calls,
               no rate limits, no cost. Vectors are pushed to Supabase pgvector via REST.

Query path   : api/query.js (Vercel Edge Function) calls the Supabase edge function at
               query time (1 call per user query). That path is unchanged.

Model note   : Supabase/gte-small is the same underlying model as the Supabase edge
               function uses (thenlper/gte-small, ONNX weights). Cosine similarity
               between local PyTorch and ONNX vectors is >0.999 — functionally identical
               for retrieval. Existing Supabase vectors do NOT need re-embedding.
"""

import os
from pathlib import Path
from typing import Optional

import httpx

CHUNKS_DIR = Path(__file__).parent.parent / "data" / "chunks"
LOCAL_BATCH_SIZE = 64   # sentence-transformers encodes locally; larger batches are fine

_model = None   # lazy-loaded on first embed call


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        print("[embedder] loading Supabase/gte-small locally (first call only)...", flush=True)
        _model = SentenceTransformer("Supabase/gte-small")
        print("[embedder] model ready — 384-dim, local inference, zero API calls", flush=True)
    return _model


def _load_env() -> dict:
    env: dict[str, str] = {}
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


class Embedder:
    def __init__(self):
        def _clean(key: str) -> str:
            raw = os.environ.get(key) or cfg.get(key, "")
            # Remove ALL whitespace (including embedded \n from copy-pasted long tokens)
            import re
            return re.sub(r'\s+', '', raw)

        cfg = _load_env()
        self._supabase_url = _clean("SUPABASE_URL")
        self._service_key  = _clean("SUPABASE_SERVICE_KEY")
        embed_secret       = _clean("EMBED_SECRET")

        if not self._supabase_url or not self._service_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        if not embed_secret:
            raise RuntimeError("EMBED_SECRET must be set in .env — the embed function now requires it")

        self._rest_url = f"{self._supabase_url}/rest/v1/chunks"
        self._embed_url = f"{self._supabase_url}/functions/v1/embed"  # used only by query()

        self._http = httpx.Client(timeout=60)
        self._rest_headers = {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        # embed_headers used by query() — Python CLI search only
        self._embed_headers = {
            "Authorization": f"Bearer {self._service_key}",
            "Content-Type": "application/json",
            "X-Embed-Secret": embed_secret,
        }
        print("[embedder] ready — ingest via local sentence-transformers, REST upsert to Supabase", flush=True)

    # ── local embedding ────────────────────────────────────────────────────────

    def _embed_texts_local(self, texts: list[str]) -> list[list[float]]:
        """Encode texts locally with sentence-transformers. No API calls, no rate limits."""
        model = _get_model()
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), LOCAL_BATCH_SIZE):
            batch = texts[i: i + LOCAL_BATCH_SIZE]
            vecs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
            all_embeddings.extend(vecs.tolist())
            print(f"[embedder]   encoded {min(i + LOCAL_BATCH_SIZE, len(texts))}/{len(texts)} chunks locally", flush=True)
        return all_embeddings

    # ── idempotency check ──────────────────────────────────────────────────────

    def already_embedded_ids(self, chunk_ids: list[str]) -> set[str]:
        """Query Supabase for which of these chunk IDs already exist in the table."""
        if not chunk_ids:
            return set()
        id_list = ",".join(f'"{cid}"' for cid in chunk_ids)
        resp = self._http.get(
            f"{self._rest_url}?select=id&id=in.({id_list})",
            headers=self._rest_headers,
        )
        if resp.status_code != 200:
            return set()
        return {row["id"] for row in resp.json()}

    # ── public API ─────────────────────────────────────────────────────────────

    def embed_chunks(self, chunks: list[dict]) -> int:
        """Embed chunks locally and upsert into Supabase. Returns count of rows upserted.

        Skips chunks already present in Supabase — safe to re-run after any failure.
        """
        if not chunks:
            return 0

        chunk_ids = [c["chunk_id"] for c in chunks]
        done_ids = self.already_embedded_ids(chunk_ids)
        pending = [c for c in chunks if c["chunk_id"] not in done_ids]

        if not pending:
            print(f"[embedder] all {len(chunks)} chunks already indexed — skipping", flush=True)
            return 0

        if done_ids:
            print(f"[embedder] {len(done_ids)} already indexed, embedding {len(pending)} new chunks locally...", flush=True)
        else:
            print(f"[embedder] embedding {len(pending)} chunks locally...", flush=True)

        texts = [c["text"] for c in pending]
        embeddings = self._embed_texts_local(texts)

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
            for c, emb in zip(pending, embeddings)
        ]

        upserted = 0
        for i in range(0, len(rows), 100):
            batch = rows[i: i + 100]
            resp = self._http.post(
                self._rest_url,
                headers=self._rest_headers,
                json=batch,
            )
            if resp.status_code in (200, 201):
                upserted += len(batch)
                print(f"[embedder]   upserted {upserted}/{len(rows)} rows", flush=True)
            else:
                print(f"[embedder] upsert error {resp.status_code}: {resp.text[:200]}", flush=True)

        return upserted

    def query(self, text: str, n_results: int = 5, match_threshold: float = 0.3) -> list[dict]:
        """Embed a query string locally and run semantic search via Supabase match_chunks RPC.

        match_threshold must be passed — omitting it causes PostgREST to return
        HTTP 300 (Multiple Choices) because it cannot resolve the function overload.
        """
        embedding = self._embed_texts_local([text])[0]

        # RPC calls use plain auth headers — no 'Prefer: resolution=merge-duplicates'
        # (that header is for upsert operations only)
        rpc_headers = {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(
            f"{self._supabase_url}/rest/v1/rpc/match_chunks",
            headers=rpc_headers,
            json={
                "query_embedding": embedding,
                "match_count": n_results,
                "match_threshold": match_threshold,
            },
        )
        resp.raise_for_status()
        results = resp.json()

        return [
            {
                "text": r.get("content", ""),
                "metadata": {
                    "doc_id": r.get("doc_id"),
                    "source_id": r.get("source_id"),
                    "date": r.get("date"),
                    "topic_tags": r.get("topic_tags"),
                },
                "similarity": r.get("similarity", 0),
            }
            for r in (results or [])
        ]

    def stats(self) -> dict:
        resp = self._http.get(
            f"{self._rest_url}?select=count",
            headers={**self._rest_headers, "Prefer": "count=exact"},
        )
        count = int(resp.headers.get("content-range", "0/0").split("/")[-1])
        return {"total_chunks": count, "model": "Supabase/gte-small (384-dim, local)", "backend": "supabase pgvector"}

    def push_to_supabase(self, chunks: list[dict]) -> int:
        """Alias kept for CLI compatibility."""
        return self.embed_chunks(chunks)

    def __del__(self):
        try:
            self._http.close()
        except Exception:
            pass
