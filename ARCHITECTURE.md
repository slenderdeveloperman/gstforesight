# GST Foresight — Architecture & Tech Stack Reference

**Version**: Based on codebase state as of June 2026  
**Product Version**: 0.3 (PRODUCT_SPEC.md)  
**Status**: Phase 1 complete, Phase 2 infrastructure complete (query model), Phase 3 scoped (auth + alerts)

---

## 1. What This System Does

GST Foresight watches the Indian public regulatory corpus — CBIC circulars, GST Council minutes, AAR rulings, budget speeches, PIB press releases, parliamentary questions, court judgments, and ICAI representations — and produces probability-weighted predictions of which GST provisions are likely to change, and when.

The system has two output modes:
1. **Dashboard** — ranked predictions with probability bars, signal breakdowns, and horizon estimates. Static, no login required.
2. **Query model** — free-form natural language queries answered with grounded citations from the indexed corpus.

---

## 2. High-Level Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  GitHub Actions (cron: 03:00 UTC daily)                        │
│  └── Ingest pipeline (Python, runs on ubuntu-latest)           │
│      ├── 8 scrapers  → data/raw/{source_id}/*.json             │
│      ├── TopicTagger → data/processed/*.json                   │
│      ├── Chunker     → data/chunks/*.json                      │
│      ├── Embedder    → Supabase pgvector (chunks table)        │
│      └── PredictionEngine → data/predictions/latest.json       │
│          └── git push → Vercel redeploys                       │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│  Vercel (static hosting + edge functions, no build step)       │
│  ├── index.html           — SPA dashboard + query UI          │
│  ├── data/predictions/latest.json  — served statically        │
│  ├── api/query.js         — edge function (query RAG flow)    │
│  ├── api/subscribe.js     — email subscription                │
│  ├── api/create-subscription.js — Razorpay payment handler    │
│  └── api/activate.js      — Pro tier activation               │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│  Supabase (managed PostgreSQL + pgvector + edge functions)     │
│  ├── chunks table         — 384-dim embeddings (pgvector)     │
│  ├── usage table          — IP-based rate limiting            │
│  ├── profiles table       — user accounts (Phase 3)           │
│  ├── subscriptions table  — Pro payment records (Phase 3)     │
│  ├── teams table          — firm accounts (Phase 3)           │
│  ├── team_members table   — many-to-many (Phase 3)            │
│  ├── query_history table  — saved query records (Phase 3)     │
│  ├── alert_subscriptions  — topic alert preferences (Phase 3) │
│  ├── match_chunks RPC     — cosine similarity search          │
│  ├── check_and_increment_usage RPC — TOCTOU-safe rate limit   │
│  ├── is_pro RPC           — subscription status check        │
│  ├── save_query RPC       — query history persistence         │
│  ├── get_history RPC      — history retrieval (auth-gated)    │
│  └── embed edge function  — query-time vector embedding       │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. Tech Stack

### 3.1 Python Backend (Ingest Pipeline)

| Library | Version | Role |
|---------|---------|------|
| `httpx` | >=0.27.0 | Async-capable HTTP client for scraping |
| `beautifulsoup4` | >=4.12.0 | HTML parsing |
| `lxml` | >=5.0.0 | Fast HTML/XML parser backend for BS4 |
| `pyyaml` | >=6.0.1 | Loads `config/sources.yaml` |
| `python-dateutil` | >=2.9.0 | Date string parsing across many formats |
| `rich` | >=13.0.0 | Terminal output formatting |
| `pytest` | >=8.0.0 | Testing framework |
| `pdfplumber` | >=0.11.0 | PDF text extraction (primary; handles text-layer PDFs) |
| `pymupdf` (fitz) | >=1.24.0 | PDF extraction fallback; batch-page splitting for Docling |
| `docling` | >=2.0.0 | OCR pipeline orchestrator for scanned/image PDFs |
| `rapidocr-onnxruntime` | >=1.2.0 | OCR engine used by Docling (no GPU required) |
| `sentence-transformers` | >=3.0.0 | Local embedding inference (Supabase/gte-small model) |

**Python version**: 3.11 (pinned in GitHub Actions)

### 3.2 JavaScript Frontend + Edge Functions

| Library / Platform | Role |
|---|---|
| `@supabase/supabase-js` ^2.45.0 | Supabase client (auth, RPC calls) |
| `@vercel/functions` ^1.0.0 | Vercel edge runtime types |
| Vanilla JS + HTML5 | No framework — single-file SPA (`index.html`) |
| Vercel Edge Runtime | `api/query.js` runs at the edge (`export const config = { runtime: 'edge' }`) |

**Node.js**: ESM (`"type": "module"` in package.json)

### 3.3 Infrastructure

| Service | Role | Key details |
|---|---|---|
| **Vercel** | Static hosting + edge functions | No build step; `outputDirectory: "."` in vercel.json |
| **Supabase** | PostgreSQL + pgvector + edge functions + auth | pgvector ext, HNSW index (m=16, ef_construction=64) |
| **GitHub Actions** | CI/CD, scheduled ingest, alert delivery | ubuntu-latest, pip caching, HuggingFace model caching |
| **Razorpay** | Indian payment gateway (Phase 3) | Checkout.js embedded; webhook-based activation |
| **Resend** | Email delivery for alerts (Phase 3) | Used in `scripts/send_alerts.py` |
| **Sarvam AI** | Query response generation | `sarvam-30b` model via `https://api.sarvam.ai/v1/chat/completions` |

---

## 4. Data Pipeline (Detailed)

### Stage 1 — Scrape

Eight scraper classes, all extending `BaseScraper`:

| Scraper class | `source_id` | Signal type | Notes |
|---|---|---|---|
| `CBICCircularScraper` | `cbic_circulars` | Historical pattern | Scrapes `cbic-gst.gov.in` (legacy static mirror); Angular SPA on main site blocks scraping |
| `GSTCouncilScraper` | `gst_council_minutes` | Council agenda / deferral | Single table on `gstcouncil.gov.in`; PDFs capped at 50 MB |
| `AARRulingScraper` | `aar_rulings` | Judicial pressure | Tries 3 candidate URLs in sequence (CBIC moves pages) |
| `BudgetSpeechScraper` | `budget_speeches` | Political signal | Full text from `indiabudget.gov.in` PDFs; 2017–present |
| `IndianKanoonScraper` | `court_judgments` | Judicial split | 4 search queries, up to 5 pages each (~100 judgments/run) |
| `ICAIRepresentationScraper` | `icai_representations` | Industry demand | 3 source pages; deduplication across pages via seen URL set |
| `PIBFinanceScraper` | `pib_finance` | Government forward signal | PRID enumeration; RSS anchor + step-25 backwards walk (900 PRIDs = 90-day window) |
| `ParliamentaryQuestionsScraper` | `parliamentary_questions` | Political signal | Lok Sabha + Rajya Sabha; GST keyword filter |

**BaseScraper provides**:
- `fetch_html(url)` — validated, capped at 10 MB
- `fetch_pdf_text(url)` — 3-tier extraction chain (pdfplumber → pymupdf → Docling+RapidOCR)
- `doc_cached(doc_id)` — filesystem-based deduplication
- `save(docs)` — skips already-seen content
- `_validate_url(url)` — checks against ALLOWED_DOMAINS allowlist (SSRF prevention)
- `_normalize_url(url)` — percent-encodes spaces in GOI PDF filenames (common source of 400/404)
- `verify=False` on httpx client — pragmatic workaround for GOI TLS cert issues; safe because all URLs are domain-allowlisted

**PDF extraction chain** (`fetch_pdf_text`):
1. `pdfplumber` — fast; handles most text-layer GOI PDFs
2. `pymupdf (fitz)` — catches layouts pdfplumber misses
3. `Docling + RapidOCR` — for scanned/image PDFs; processed in 5-page batches with explicit `gc.collect()` between batches to keep peak RAM under ~400 MB on CPU

**Docling singleton**: `_docling_converter` is a module-level lazy-initialised singleton. Loading RapidOCR models is expensive; the singleton is initialised once and reused. `release_docling()` is called between sources in the ingest loop to free GPU/CPU memory.

### Stage 2 — Tag

`TopicTagger` applies regex keyword patterns to each raw document.

- Text is truncated to 50,000 chars before regex to prevent ReDoS
- 12 topic IDs (from `config/sources.yaml`): `itc_eligibility`, `rcm_coverage`, `rate_rationalisation`, `return_format`, `ims_itc_flow`, `e_invoicing`, `classification_disputes`, `valuation`, `place_of_supply`, `gst_on_crypto_vda`, `msme_composition`, `real_estate`
- Outputs: `topic_tags` (sorted by match count desc), `topic_scores` (count per topic), `tagged_at`
- New patterns added at runtime via `tagger.add_keywords(topic_id, patterns)`
- Processed documents written to `data/processed/{doc_id}.json`

### Stage 3 — Chunk

`Chunker` splits processed documents into overlapping chunks.

- Chunk size: 3000 chars (~750 tokens for English legal text)
- Overlap: 400 chars (~100 tokens) — prevents signal loss at chunk boundaries
- Sentence-boundary awareness: breaks at last `. ` within first 50% of chunk
- Chunk metadata: `chunk_id`, `doc_id`, `source_id`, `date`, `topic_tags`, `topic_scores`, `chunk_index`, `char_start`
- Written to `data/chunks/{doc_id}.json`

### Stage 4 — Embed

`Embedder` runs local inference then upserts to Supabase.

- Model: `Supabase/gte-small` (same underlying model as Supabase's edge function; `thenlper/gte-small`, ONNX weights)
- Dimensions: 384
- Inference: local via `sentence-transformers`; batch size 64; `normalize_embeddings=True`
- Idempotent: checks which chunk IDs already exist in Supabase before embedding
- Upsert: REST POST to `/rest/v1/chunks` with `Prefer: resolution=merge-duplicates`; batched in groups of 100
- Key whitespace stripping on env vars: `re.sub(r'\s+', '', raw)` — handles embedded `\n` from copy-pasted long tokens in Vercel dashboard

### Stage 5 — Predict

`PredictionEngine` runs 7 signal evaluators over all tagged documents for each of the 12 topics.

**Signal evaluators and their logic**:

| Evaluator | Signal type | Trigger | Strength formula |
|---|---|---|---|
| `evaluate_repeated_circular_topic` | `repeated_circular_topic` | 2+ CBIC circulars on same topic | `min(0.3 + (count-2)*0.15, 0.9)` |
| `evaluate_council_deferred_item` | `council_deferred_item` | Deferral language in council minutes | `min(0.55 + count*0.15, 0.92)` (1 deferral → 0.65, 3+ → 0.92) |
| `evaluate_aar_ruling_frequency` | `aar_ruling_frequency` | 3+ AARs on same topic in 12 months | `min(0.2 + count*0.08, 0.75)` |
| `evaluate_budget_speech_phrase` | `budget_speech_phrase` | Action language in budget speech | Recency decay: `max(0.82 - age_years*0.14, 0.30)` |
| `evaluate_government_forward_signal` | `government_forward_signal` | PIB Finance release with action language | Recency decay by age: ≤30d→0.65, ≤60d→0.52, ≤90d→0.42, older→0.32; imminent language +0.15 |
| `evaluate_industry_ask_repeat` | `industry_ask_repeat` | Same topic in 2+ ICAI/FICCI memoranda | Fixed 0.45 |
| `evaluate_judicial_split` | `judicial_split` | Same topic ruled on by 2+ distinct courts | `min(0.38 + ruling_factor + court_factor, 0.80)` |

**Probability combination**: weighted average with diminishing returns per signal type (2nd signal of same type: 60% of base weight; 3rd: 40%). Capped at 95%.

**Signal weights** (from `config/sources.yaml`):
```
repeated_circular_topic:  0.25
council_deferred_item:    0.30  (highest)
aar_ruling_frequency:     0.20
budget_speech_phrase:     0.35  (highest)
industry_ask_repeat:      0.15
judicial_split:           0.25
government_forward_signal: 0.20
```

**Horizon derivation**: determined by highest-priority signal type present:
- `council_deferred_item` → "Next GST Council meeting" (90 days)
- `government_forward_signal` → "Next GST Council meeting" if horizon_days ≤90, else "2 Council meetings"
- `budget_speech_phrase` → "Next Union Budget / 2 Council meetings" (180 days)
- `repeated_circular_topic` → "2–3 quarters" (180 days)
- Default → "Next FY" (270 days)

**Surfacing threshold**: minimum 2 signals AND probability ≥ 30%.

**Output**: `data/predictions/latest.json` + timestamped snapshot. GitHub Actions commits this file; Vercel serves it statically.

### Stage 6 — Alert Delivery (Phase 3)

Triggered by `alerts.yml` when `data/predictions/latest.json` changes on main:
1. `send_alerts.py` diffs current `latest.json` against `previous.json`
2. Queries Supabase `alert_subscriptions` for users watching topics with delta ≥ threshold
3. Sends emails via Resend API
4. Rotates `previous.json` ← `latest.json` and commits

---

## 5. Query Flow (RAG Pipeline)

```
User types query
    ↓
POST /api/query (Vercel edge function — api/query.js)
    ↓
Sanitize: strip control chars, collapse whitespace, cap at 500 chars
    ↓
Auth: getUserId() — validates JWT via Supabase /auth/v1/user (server-side)
    ↓
is_pro RPC — checks individual subscription OR team membership
    ↓
[If not Pro] check_and_increment_usage RPC — atomic rate limit (SELECT FOR UPDATE)
    ↓
Supabase embed edge function — embeds query to 384-dim vector
    (protected by X-Embed-Secret header)
    ↓
match_chunks RPC — cosine similarity search (top 5, threshold 0.3)
    (SECURITY DEFINER — anon key can call but cannot read chunks table directly)
    ↓
buildPrompt() — injects top chunks into XML-delimited prompt with injection guard
    ↓
Sarvam AI sarvam-30b — structured foresight response (temp 0.2, max 4000 tokens)
    ↓
[If logged in] save_query RPC — fire-and-forget history persistence
    ↓
Return: { answer, sources[4], remaining_queries, is_pro }
```

**Prompt injection protection**: The system prompt explicitly instructs the model to treat `<user_query>` as a question only, not as instructions. Phrases like "ignore previous instructions" are called out by name.

---

## 6. Database Schema (Supabase / PostgreSQL)

### Phase 1–2 Tables

**`chunks`** — indexed document corpus
```
id           text PK          -- chunk_id from chunker.py
doc_id       text NOT NULL
source_id    text NOT NULL
date         text
topic_tags   text             -- comma-separated topic IDs
chunk_index  int
content      text NOT NULL
embedding    vector(384)      -- HNSW index (m=16, ef_construction=64)
inserted_at  timestamptz
```

**`usage`** — IP-level rate limiting
```
ip           text PK
query_count  int DEFAULT 0
reset_at     timestamptz      -- rolling 30-day window
```

### Phase 3 Tables (schema in schema.sql, not yet wired to UI)

**`profiles`** — auto-created on signup via trigger  
**`subscriptions`** — Pro payment records (Razorpay); `razorpay_payment_id` UNIQUE prevents double-activation  
**`teams`** — firm accounts; `max_seats` enforced by `check_seat_limit` trigger  
**`team_members`** — many-to-many users ↔ teams  
**`query_history`** — append-only; no user-facing delete  
**`alert_subscriptions`** — topic + threshold per user; UNIQUE (user_id, topic_id)

### RPCs (all `SECURITY DEFINER`)

| RPC | Called by | Purpose |
|---|---|---|
| `match_chunks` | `api/query.js` | Cosine similarity search |
| `check_and_increment_usage` | `api/query.js` | Atomic rate limit (TOCTOU-safe via `SELECT FOR UPDATE`) |
| `is_pro` | `api/query.js` | Subscription status check |
| `save_query` | `api/query.js` | History persistence |
| `get_history` | Frontend (Phase 3) | User history retrieval; enforces `auth.uid() == p_user_id` |

---

## 7. File / Data Structure

```
GST FORESIGHT/
├── gst_foresight/
│   └── __main__.py          CLI: ingest / predict / status / reextract / embed
├── scrapers/
│   ├── base.py              BaseScraper, Document, ALLOWED_DOMAINS
│   └── sources.py           All 8 scraper classes
├── processors/
│   ├── tagger.py            TopicTagger, TOPIC_KEYWORDS
│   ├── chunker.py           Chunker (3000-char, 400-char overlap)
│   └── embedder.py          Embedder (local gte-small → Supabase pgvector)
├── predictors/
│   ├── engine.py            PredictionEngine, Signal, Prediction, 7 evaluators
│   └── backtest.py          Backtest runner
├── api/
│   ├── query.js             Edge function: sanitize → auth → rate-limit → embed → search → Sarvam → respond
│   ├── subscribe.js         Email subscription
│   ├── create-subscription.js  Razorpay payment init
│   └── activate.js          Pro tier activation post-payment
├── supabase/
│   ├── schema.sql           All tables, RPCs, RLS policies
│   └── functions/
│       ├── embed/index.ts   Query-time embedding (protected by X-Embed-Secret)
│       └── smooth-worker/index.ts
├── scripts/
│   ├── scrape_ticker.py     News ticker updater (runs in daily CI)
│   ├── send_alerts.py       Alert diff + Resend delivery
│   ├── eval_20_queries.py   20-query manual evaluation harness
│   ├── semantic_tag_council.py  Sarvam semantic tagging pass
│   └── embed_council_chunks.py  One-off re-embed script
├── config/
│   └── sources.yaml         Topic taxonomy, source metadata, signal weights, prediction config
├── data/
│   ├── raw/{source_id}/     Scraped Document JSON files
│   ├── processed/           Tagged documents (topic_tags, topic_scores)
│   ├── chunks/              Chunked documents
│   ├── predictions/
│   │   ├── latest.json      Current predictions (served by Vercel, committed by CI)
│   │   └── previous.json    Previous run snapshot (for alert delta)
│   └── news/
│       └── ticker.json      News ticker data
├── tests/
│   ├── backtest_cases.json  Ground-truth cases for backtesting
│   ├── test_query_quality.js  20-query eval harness
│   ├── test_security.js     25 security tests (CORS, injection, oversized body, etc.)
│   └── query_eval_*.json   Historical eval run results
├── .github/workflows/
│   ├── ingest_daily.yml     Daily 03:00 UTC: ingest → predict → commit
│   ├── alerts.yml           Triggered on latest.json push: diff → alert → rotate
│   └── ticker.yml           (ticker refresh)
├── index.html               SPA dashboard (vanilla JS, no build step)
├── vercel.json              Headers (CSP, CORS, Cache-Control), no build config
├── requirements.txt         Python dependencies (by pipeline phase)
├── package.json             JS dependencies (@supabase/supabase-js, @vercel/functions)
└── PRODUCT_SPEC.md          Full product spec with phases, access tiers, success metrics
```

---

## 8. Key Architectural Decisions

### 8.1 No framework on the frontend
The entire UI is a single `index.html` with vanilla JavaScript. No React, no Vite, no build step. `vercel.json` sets `outputDirectory: "."` — Vercel serves the directory as-is. **Why**: eliminates build pipeline complexity, keeps CI fast, and the frontend is simple enough that a framework adds more overhead than it saves.

### 8.2 Static predictions file as the primary data contract
Predictions are written to `data/predictions/latest.json` by Python and served as a static file by Vercel. The frontend fetches it with a plain `fetch()`. **Why**: decouples the prediction pipeline from the serving layer entirely; zero infrastructure needed to serve the dashboard; cache-busting handled by Vercel's `no-store` header on `/data/*.json`.

### 8.3 Local embedding, remote vector store
Embeddings are generated locally during ingest using `sentence-transformers` (no API cost, no rate limits), then pushed to Supabase pgvector via REST. At query time, the Supabase `embed` edge function re-embeds the user's query (using the same underlying model, ONNX weights). **Why**: avoids per-document API billing during bulk ingest; the query-time path stays serverless-friendly (edge function, no Python runtime needed).

### 8.4 SECURITY DEFINER pattern on all Supabase RPCs
Every RPC that reads sensitive tables (`chunks`, `usage`, `query_history`) is `SECURITY DEFINER`. This means the function runs with the owner's privileges (service_role) regardless of who calls it. **Why**: the anon key can call these RPCs via the Vercel edge function, but cannot read the underlying tables via REST (`GET /rest/v1/chunks?select=*` is blocked by RLS). This prevents corpus extraction and rate-limit bypass without requiring server-side key management.

### 8.5 TOCTOU-safe rate limiting via SELECT FOR UPDATE
`check_and_increment_usage` uses `INSERT ... ON CONFLICT DO NOTHING` followed by `SELECT ... FOR UPDATE`. The `FOR UPDATE` row-level lock means two concurrent requests for the same IP serialize at the database level. **Why**: without this, two simultaneous requests arriving before either increments the counter would both be allowed through under the same quota.

### 8.6 3-tier PDF extraction chain
PDF text extraction tries pdfplumber first (fast, handles text-layer PDFs), then pymupdf (better on some GOI layouts), then Docling + RapidOCR (for scanned/image PDFs). The Docling converter is a lazy-initialised module-level singleton, and Docling processes PDFs in 5-page batches with explicit `gc.collect()` between batches. **Why**: GOI PDFs are heterogeneous — some are text-layer PDFs, some are scanned images, and some are hybrids. The fallback chain handles all three without requiring manual triage. The batch processing keeps peak RAM flat on long documents.

### 8.7 Intentionally simple, explainable prediction model
The prediction engine uses weighted signal combination with diminishing returns — no neural networks, no black-box ML. Every prediction links to its source documents. **Why**: the product's credibility with CAs depends on being able to explain *why* a prediction was made. "Council deferred this item in 2 meetings" is persuasive; an opaque probability from a neural network is not.

### 8.8 ALLOWED_DOMAINS allowlist on all HTTP fetches
`BaseScraper._validate_url()` checks every URL against a hardcoded allowlist before any HTTP request. The allowlist includes known GOI domains plus a CloudFront CDN domain used by ICAI. **Why**: the scrapers follow links found in scraped pages; without a domain allowlist, a malicious redirect could cause the scraper to make requests to arbitrary hosts (SSRF).

### 8.9 Config-driven taxonomy governance
Topic taxonomy and source metadata live in `config/sources.yaml`. Signal weights are also in this file. New topics require a PR with a rationale note and at least 2 active sources with documented keyword patterns. **Why**: keeps topic definitions and signal weights auditable and version-controlled rather than scattered across code.

### 8.10 Alert workflow triggered by file change, not by schedule
`alerts.yml` triggers on `push` to `main` when `data/predictions/latest.json` changes, not on a separate cron. **Why**: ensures alerts are always sent based on the actual latest prediction state, and the alert workflow is only invoked when there is actually new data to diff. No wasted runs.

### 8.11 GitHub Actions as the ingest runtime
The daily ingest pipeline runs as a GitHub Actions job, not on a persistent server. The HuggingFace model cache is persisted between runs via `actions/cache`. **Why**: zero server cost for a batch pipeline that runs once daily. The 90-minute job timeout is sufficient for all 8 sources. Eliminates the need to maintain and monitor a cron server.

### 8.12 Sarvam AI for query responses (not Claude or GPT)
The query response generation uses `sarvam-30b` (Sarvam AI). The system prompt positions the model as a GST regulatory analyst. **Why**: Sarvam is trained on Indian legal and regulatory text, including Hindi-language documents. This gives better handling of Indian GST terminology and references compared to a general-purpose Western LLM. The same API interface (`/v1/chat/completions`, OpenAI-compatible) means it's easy to swap models.

### 8.13 Pro tier bypasses rate limiting, not corpus access
Pro users skip IP-based rate limiting (`check_and_increment_usage` is not called). Both free and Pro users get the same vector search and the same Sarvam response. **Why**: the corpus is the same for everyone — the gate is on usage frequency, not on data quality. This keeps the free experience genuinely useful (first 5 queries are full-quality).

### 8.14 localStorage for soft query limit, Supabase for hard limit
The frontend tracks query count in `localStorage` (5/month soft limit — no network call). The edge function enforces the hard limit via Supabase. **Why**: the soft limit gives instant UI feedback without a round-trip; the hard limit prevents bypass via `localStorage` clearing. A user who clears `localStorage` still hits the hard cap.

---

## 9. CI/CD Workflows

### `ingest_daily.yml` (triggers: daily cron 03:00 UTC + manual dispatch)
1. Checkout with write token
2. Set up Python 3.11 with pip + HuggingFace model caching
3. Validate all required secrets are present
4. `python3 -m gst_foresight ingest --all`
5. `python3 -m gst_foresight predict`
6. `python3 scripts/scrape_ticker.py`
7. Commit `data/predictions/latest.json` + `data/news/ticker.json` (if changed)
8. `python3 -m gst_foresight status`

### `alerts.yml` (triggers: push to main touching `data/predictions/latest.json`)
1. Checkout with `fetch-depth: 2` (needs HEAD and HEAD~1 for diff)
2. Install only `httpx` (no sentence-transformers — keeps this job fast)
3. `python3 scripts/send_alerts.py` (diff + Resend delivery)
4. Commit `data/predictions/previous.json` (rotated snapshot)

**Required GitHub Secrets**: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SARVAM_API_KEY`, `EMBED_SECRET`, `RESEND_API_KEY`, `ALERT_FROM_EMAIL`

---

## 10. Security Model

| Layer | Mechanism |
|---|---|
| HTTP fetches | ALLOWED_DOMAINS allowlist prevents SSRF |
| Response size | 10 MB cap on all responses (50 MB override for large council PDFs) |
| Edge function body | 8 KB max body; `content-length` checked before reading |
| Query sanitization | Strip control chars, collapse whitespace, cap 500 chars |
| Prompt injection | XML delimiter + explicit instruction to ignore meta-commands in `<user_query>` |
| CORS | Origin allowlist in `api/query.js`; locked to production origin in `vercel.json` headers |
| Corpus protection | RLS on `chunks` blocks direct REST reads; only `match_chunks` RPC (SECURITY DEFINER) readable by anon |
| Rate limit race condition | `SELECT FOR UPDATE` in `check_and_increment_usage` prevents TOCTOU |
| Service key isolation | `SUPABASE_SERVICE_KEY` only in local `.env` and GitHub Secrets — never in Vercel env |
| Payment integrity | `razorpay_payment_id` UNIQUE constraint prevents double-activation |
| History access | `get_history` RPC enforces `auth.uid() == p_user_id` — no cross-user reads |
| Security headers | CSP, X-Content-Type-Options, X-Frame-Options: DENY, Referrer-Policy in vercel.json |
| Embed secret | `X-Embed-Secret` header required on Supabase embed function; rotated after prior exposure |

Security test suite: `tests/test_security.js` — 25 tests covering the above, run against the live Vercel URL. Last run: 25/25 passed (2026-05-22).

---

## 11. Source Signal Weights (from config/sources.yaml)

| Source | Weight | Signal type |
|---|---|---|
| CBIC Circulars | 0.35 | historical_pattern |
| GST Council Minutes | 0.30 | council_agenda |
| AAR Rulings | 0.20 | judicial_pressure |
| Budget Speeches | 0.15 | political_signal |
| ICAI Memoranda | 0.10 | industry_demand |
| PIB Finance Releases | 0.20 | government_forward_signal |
| High Court Orders | 0.15 (planned) | judicial_pressure |
| FICCI Submissions | 0.08 (planned) | industry_demand |
| Parliament Questions | 0.08 (planned) | political_signal |
| Election Calendar | 0.12 (planned) | political_economy |

---

## 12. Current Build Phase Status

| Phase | Status | Exit criteria |
|---|---|---|
| Phase 1 — Full-text pipeline | Mostly complete | `reextract` run still pending for 39 docs; GST Council 50/53/54 need OCR pass |
| Phase 2 — Query interface | Infrastructure complete | Live query end-to-end confirmation pending; 20-query eval target: 85% pass rate |
| Phase 3 — Auth + alerts | Scoped, schema written | Pending auth provider choice (Clerk / Supabase Auth / Firebase), payment flow |
| Phase 4 — API | Scoped | After revenue validation from Phase 3 |

---

## 13. Local Development

```bash
# Install Python deps
pip install -r requirements.txt

# Copy env file
cp .env.example .env   # fill in SUPABASE_URL, SUPABASE_SERVICE_KEY, EMBED_SECRET

# Ingest all sources
python -m gst_foresight ingest --all

# Skip OCR on memory-constrained machines
python -m gst_foresight ingest --all --no-ocr

# Re-fetch PDFs for docs where full text was missed
python -m gst_foresight reextract

# Embed any un-indexed chunks (run separately if ingest --skip-embed was used)
python -m gst_foresight embed

# Generate predictions
python -m gst_foresight predict

# Check status
python -m gst_foresight status

# Serve dashboard (open index.html in browser directly — no server needed)
```

**Required env vars**: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `EMBED_SECRET`, `SARVAM_API_KEY`
