# GST Foresight — Product Specification
**Version**: 0.6
**Last updated**: 2026-06-29
**Status**: Phase 1 complete; Phase 2 infrastructure complete, live validation pending; Phase 3 schema written, not yet wired; Phase 4 (Personalization) shipped 2026-06-29; Track Record system shipped 2026-06-29 (public scorecard, Brier scoring, resolution engine, SHA256 integrity)

---

## 1. Vision

GST rules change constantly. Circulars drop with little warning. Rate changes get announced at council meetings with minimal lead time. CAs and businesses are permanently reactive.

GST Foresight flips that. It reads the public regulatory record — CBIC circulars, GST Council minutes, AAR rulings, budget speeches, industry memoranda — and surfaces probability-weighted predictions of what's likely to change next, and when.

The query model extends this: instead of reading a dashboard, a CA types *"what's the outlook for ITC on marketing expenses?"* and gets a structured foresight response grounded in the actual regulatory corpus.

**One-line positioning**: The Bloomberg Terminal for Indian GST foresight — minus the price tag and jargon.

---

## 2. Target Users

### Primary — Chartered Accountants (CAs)
- 400,000+ ICAI members in India
- Pain: reactive to GST changes, clients expect proactive advisory
- Use case: check predictions before client meetings, cite signals in advice letters
- Acquisition: ICAI forums, CA WhatsApp groups, word-of-mouth
- Willingness to pay: moderate (Rs 500–2,000/month individually; higher via firm licenses)

### Secondary — Tax & Compliance Professionals
- In-house tax heads at SMEs and mid-market companies
- Pain: missed circular deadlines, rate change surprises affecting pricing
- Use case: subscribe to alerts on specific topics (e.g. real estate, e-invoicing)
- Willingness to pay: higher (Rs 2,000–5,000/month, expensed)

### Tertiary — Fintech / ERP Developers
- Teams building GST-adjacent products (invoicing, payroll, accounting software)
- Pain: need to anticipate API/format changes before they ship
- Use case: API access to predictions feed
- Willingness to pay: API pricing per query or monthly flat rate

### Not targeted (v1)
- Large enterprises with existing Bloomberg Tax / Refinitiv contracts
- Individual taxpayers (not the audience for regulatory foresight)

---

## 3. Product Surfaces

### 3.1 Predictions Dashboard (exists — needs upgrade)
Public, no login required. Shows ranked predictions with probability bars, signal breakdowns, and horizon estimates.

**Current state**: Built and deployed. Operating on index-level data (subject lines only).
**Required upgrade**: Rebuild on full-text corpus once PDF pipeline is complete.

### 3.2 Query Interface (to build — Phase 2)
A text input on the dashboard. User types a free-form question about a GST topic and receives a structured foresight response.

**Example queries**:
- "Will GST on co-working spaces change in the next two quarters?"
- "What's the risk of ITC reversal rules tightening further?"
- "Is the e-invoicing threshold likely to drop to ₹1 crore?"

**Response format**:
```
Topic: E-Invoicing Threshold
Probability of change: 62%
Horizon: Next Union Budget / 2 Council meetings

Signals driving this:
• Council deferral — threshold reduction deferred at 53rd meeting
• Budget signal — Budget 2025 contained action language on e-invoicing expansion

What to watch:
• Next GST Council press release for threshold announcement
• MoF notifications in Q3 FY26

Confidence note: Based on 847 indexed documents. Last updated: 09 May 2026.
```

### 3.3 Track Record (live — public, no login)
Public scorecard at `/track-record.html` that shows all live predictions alongside their resolution outcomes, scored by Brier score and binary accuracy with Wilson 95% CI.

**Current state**: Shipped 2026-06-29. Resolution engine runs on 4-day CI cycle.

**Key design properties**:
- Every prediction row is anchored to a specific git commit SHA (`source_commit` + `source_committed_at`) — the SHA is recorded before any outcome is known, making post-hoc manipulation detectable
- Multi-tag containment resolution: a CBIC circular resolves a prediction only if the prediction's `topic_id` is in the circular's `doc_tags[]` — exact match too brittle for multi-topic circulars
- UTC-aware expiry throughout — predictions that pass their `deadline` without a resolution document become `expired_no_match`
- Topic-level cooldown after `expired_no_match`: new pending row blocked for `horizon_days × 0.5` days to prevent gaming the system with rapid re-predictions
- SHA256 integrity sidecar (`data/track-record.sha256`) — resolver validates sidecar on startup, rewrites on exit
- Accuracy display suppressed until 10 resolved rows (`min_resolved_for_accuracy`) — prevents misleading 100% accuracy claims on small samples
- Resolution document types: CBIC circulars, GST Council minutes (decision language gated), Finance Act, CBIC notifications. AARs and court judgments are signals only, not resolution triggers.

**Resolution scoring**:
- `materialised` = a qualifying resolution doc with the matching topic tag appeared before deadline
- `expired_no_match` = deadline passed, no qualifying doc found
- `pending` = still within horizon window

### 3.4 Topic Alerts (Phase 3 — requires auth)
Users subscribe to specific topics. When a prediction probability moves by ≥10 points, or a new signal fires, they receive an email alert.

**Example alert**: *"IMS / ITC Flow Mechanism probability moved from 62% → 78%. New signal: council deferral at 54th meeting. View breakdown →"*

### 3.4 Saved Query History (Phase 3 — requires auth)
Logged-in users see their past queries and responses. Useful for CAs who want to reference advice they gave clients.

### 3.5 API Access (Phase 4)
Programmatic access to the predictions feed and query endpoint. Targeted at fintech/ERP developers. Priced per query or monthly flat.

---

## 4. Access Tiers

| Feature | Free | Pro (paid) | API |
|---|---|---|---|
| Predictions dashboard | ✓ Full access | ✓ | ✓ |
| Signal breakdowns | ✓ | ✓ | ✓ |
| Query model | 5 queries/month | Unlimited | Per-query billing |
| Topic alerts | — | ✓ | — |
| Query history | — | ✓ | — |
| Downloadable reports | — | ✓ | — |
| API access | — | — | ✓ |
| Login required | No | Yes | Yes (API key) |

**Pro pricing (indicative)**: Rs 799/month individual, Rs 2,499/month firm (up to 5 seats).
**API pricing (indicative)**: Rs 2/query, Rs 999/month for 1,000 queries/month flat.

**Gate decision**: No login wall on the dashboard or first 5 queries. Auth only introduced when saving history or alerts — features that intrinsically require identity.

---

## 5. Technical Architecture

### 5.1 Full Stack Overview

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions (cron: daily 06:00 IST)                     │
│  └── Ingest pipeline                                         │
│      ├── Scrapers (httpx + BeautifulSoup)                   │
│      ├── PDF downloader + pdfplumber extractor              │
│      ├── Chunker (800 token chunks, 100 token overlap)      │
│      ├── Tagger (regex first pass)                          │
│      ├── Embedder (sentence-transformers all-MiniLM-L6-v2) │
│      ├── Vector store writer (Supabase pgvector)            │
│      └── Prediction engine → latest.json                   │
│          └── git commit + push → Vercel deploys             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Vercel (static hosting + edge function)                    │
│  ├── index.html (dashboard + query UI)                      │
│  ├── data/predictions/latest.json                           │
│  └── api/query.js (Vercel edge runtime)                     │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Supabase (vector search + embedding + rate limiting)       │
│  ├── pgvector — chunks table with 384-dim embeddings        │
│  ├── match_chunks RPC — similarity search (SECURITY DEFINER)│
│  ├── embed edge function — query-time embedding             │
│  └── check_and_increment_usage RPC — per-IP rate limiting   │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 Data Pipeline (detailed)

**Stage 1 — Scrape**
- Scrapers fetch index pages from each source (existing)
- New: follow PDF links and download full documents
- PDF text extracted via `pdfplumber` (handles most GOI PDFs)
- Fallback: `pymupdf` (fitz) for scanned/image PDFs with OCR

**Stage 2 — Chunk**
- Documents split into 800-token chunks with 100-token overlap
- Overlap prevents signal loss at chunk boundaries
- Metadata attached to each chunk: source_id, doc_id, date, page_number

**Stage 3 — Tag**
- Existing regex tagger runs on each chunk (fast, free)
- Sarvam semantic pass on GST Council minutes and Hindi-language documents only
- Output: topic_tags + topic_scores per chunk

**Stage 4 — Embed**
- Each chunk embedded using sentence-transformers `all-MiniLM-L6-v2` (free, local, 384 dimensions)
- Sarvam embed-v1 as upgrade path for Hindi content
- Embeddings stored in ChromaDB persistent client

**Stage 5 — Predict**
- Existing prediction engine runs over tagged chunks (unchanged logic)
- Generates latest.json with updated probabilities

**Stage 6 — Commit**
- GitHub Actions commits updated latest.json and ChromaDB export
- GitHub Pages auto-deploys on push

### 5.3 Query Flow (Vercel edge function)

```
User types query in dashboard
    ↓
fetch() POST to api/query.js (Vercel edge runtime)
    ↓
Edge function: sanitize query, check IP rate limit via Supabase RPC
    ↓
Edge function: call Supabase embed function → 384-dim vector
    ↓
Edge function: call match_chunks RPC → top 8 chunks by cosine similarity
    ↓
Edge function: build prompt with XML delimiters + retrieved chunks
    ↓
Sarvam-M: generate structured foresight response
    ↓
Edge function: return JSON response to dashboard
    ↓
Dashboard: render response in ScreenQueryResponse
```

### 5.4 Rate Limiting (free tier enforcement)
- 5 free queries/month tracked via localStorage (soft limit, no backend needed)
- Hard limit enforced at Supabase via `check_and_increment_usage(ip, max_per_day)` RPC
  — uses `SELECT FOR UPDATE` to prevent TOCTOU race condition under concurrent requests
- No auth required for free tier — friction-free

### 5.5 Vector Store Strategy
- Supabase pgvector — `chunks` table with `embedding vector(384)` column
- `match_chunks` RPC uses `SECURITY DEFINER` so anon key can call it but cannot
  read the table directly via REST (blocks corpus extraction via `/rest/v1/chunks?select=*`)
- Ingest pipeline writes via `SUPABASE_SERVICE_KEY` (local `.env` only, never on Vercel)
- Query-time embedding via Supabase `embed` edge function (requires `X-Embed-Secret` header)

---

## 6. Data Sources

### Active (scraper exists)
| Source | Signal type | Full-text? | Update schedule |
|---|---|---|---|
| CBIC Circulars | Historical pattern | PDF — to add | Weekly |
| GST Council Minutes | Agenda / deferral | PDF — to add | Per meeting (~6/year) |
| AAR Rulings | Judicial pressure | PDF — to add | Weekly |
| Budget Speeches | Political signal | PDF — to add | Annual |

### Planned (scraper to build)
| Source | Signal type | Priority |
|---|---|---|
| ICAI Pre-Budget Memoranda | Industry demand | High |
| High Court GST Orders | Judicial split | High |
| FICCI/CII GST Submissions | Industry demand | Medium |
| Parliament GST Questions | Political attention | Medium |
| State Election Calendar | Political economy | Low |
| Ministry Press Releases | Pre-announcement | Medium |

---

## 7. Topic Taxonomy

12 topics currently tracked (see config/sources.yaml):
`itc_eligibility`, `rcm_coverage`, `rate_rationalisation`, `return_format`,
`ims_itc_flow`, `e_invoicing`, `classification_disputes`, `valuation`,
`place_of_supply`, `gst_on_crypto_vda`, `msme_composition`, `real_estate`

**Taxonomy governance**: Topics added via PR to config/sources.yaml with a rationale note.
New topics require at least 2 active sources with documented keyword patterns before activation.

---

## 8. Sarvam Integration Points

Sarvam is used selectively — not as a replacement for the existing pipeline but as an upgrade layer at specific bottlenecks:

| Use case | Why Sarvam specifically |
|---|---|
| GST Council minutes semantic tagging | Indirect deferral language ("kept in abeyance") not caught by regex |
| Hindi-language CBIC documents | Sarvam trained on Indian legal Hindi |
| Budget speech full-text signal extraction | Action phrases buried in 40+ pages of text |
| Query response generation | Domain-appropriate responses for Indian regulatory context |

**Sarvam models targeted**:
- `sarvam-2b-v0.5` for classification/tagging tasks (cost-efficient)
- `sarvam-1` for query response generation (higher quality)
- Sarvam embed-v1 for Hindi document embeddings

---

## 9. Build Phases

### Phase 1 — Full-text pipeline (immediate priority)
**Goal**: Replace index-level scraping with full document text.

Tasks:
- [x] Add `pdfplumber` and `pymupdf` to requirements.txt
- [x] Add `fetch_pdf_text(url)` method to `BaseScraper` (3-tier: pdfplumber → pymupdf → Docling+RapidOCR; URL-encoding fix for space-in-filename PDFs added 2026-05-22)
- [x] Update all scrapers to follow PDF links and extract full text (8 active scrapers)
- [x] Add chunker module (`processors/chunker.py`)
- [x] Add embedder module (`processors/embedder.py`) — uses Supabase pgvector (chromadb dep removed 2026-05-22)
- [x] Vector store: Supabase pgvector (`chunks` table, `match_chunks` RPC, SECURITY DEFINER)
- [x] Update ingest CLI to run chunk → embed pipeline after tagging
- [ ] **Run `python -m gst_foresight reextract`** — 39 docs missing full text; URL encoding fix applied 2026-05-22; GST Council 50/53/54 need OCR pass; still pending as of 2026-06-24
- [ ] Rebuild predictions on full-text corpus after reextract, validate against backtest cases

**Exit criteria**: Prediction engine running on full document text. Backtest accuracy unchanged or improved.

### Phase 2 — Query interface (after Phase 1)
**Goal**: Users can ask specific questions and get grounded foresight responses.

#### Built (infrastructure complete)
- [x] Vercel edge function (`api/query.js`) — sanitization, CORS allowlist, 8KB body guard
- [x] Sarvam-M API for answer generation (key in Vercel env)
- [x] Supabase `match_chunks` RPC — pgvector similarity search, SECURITY DEFINER
- [x] Supabase `embed` edge function — query-time embedding, `X-Embed-Secret` required
- [x] Supabase `check_and_increment_usage` RPC — per-IP rate limiting, TOCTOU-safe
- [x] Query UI panel in `index.html` (`ScreenQueryResponse` component)
- [x] localStorage-based free query counter (5/month soft limit)

#### Action list (ordered by priority)

**P1 — Security (do before any sharing)**
- [x] Rotate EMBED_SECRET — done 2026-05-22
- [x] Run `tests/test_security.js` against live Vercel URL — 25/25 passed 2026-05-22

**P2 — Correctness (query flow must work end-to-end)**
- [ ] Confirm live query works: open deployed site, type a query, verify Sarvam returns a structured foresight response (not an error or fallback)
- [ ] Confirm ingest → `latest.json` → live predictions are in sync (predictions may be stale after reextract)
- [ ] Test with 20 representative CA queries — manual eval: grounded signal citations? Accurate probability estimates? Target: 85% pass rate. Log failures for prompt tuning.

**P2.5 — CI reliability (infrastructure)**
- [x] Fix ticker CI failure: `scrape_ticker.py` hard-exited with code 1 when `data/processed/` absent — fixed 2026-06-24 (empty ticker write + exit 0)
- [x] Ingest CLI now proactively creates `data/processed/` + `data/predictions/` at startup — fixed 2026-06-24
- [x] Created `ARCHITECTURE.md` — full architecture + tech stack reference — 2026-06-24

**P3 — UI fixes (polish before sharing)**
- [x] Fix `onViewAlert` — was always opening `predictions[0]`; root cause was row `onClick` immediately navigating away before `active` could be changed; fixed 2026-05-22
- [x] Fix single-click ↗ → `ScreenPredictionDetail` — row click now only sets `activeId` (select); navigation via ↗ button or double-click on row; fixed 2026-05-22
- [ ] Wire `ScreenSourceDoc` to real chunks from Supabase (currently shows static mock data)

**P4 — Signal quality (improves answer accuracy)**
- [ ] Sarvam semantic tagging pass for GST Council minutes — regex tagger misses deferral language ("kept in abeyance", "further deliberation"); `scripts/semantic_tag_council.py` exists, not yet run on full corpus
- [ ] AAR scraper: all 3 candidate URLs returning 404 — find new CBIC advance rulings URL

**Exit criteria**: Query model returns grounded responses on 85%+ of 20 test queries. Security hardening confirmed via test suite. No known UI bugs.

### Phase 3 — Auth + alerts (after validated query model)
**Goal**: Give users a reason to create an account.

Tasks:
- [ ] Evaluate auth provider (Clerk, Supabase Auth, or Firebase Auth)
- [ ] Implement email/Google login
- [ ] Build saved query history (Supabase or PlanetScale free tier)
- [ ] Build topic alert system (GitHub Actions checks prediction deltas, sends emails via Resend or Postmark)
- [ ] Implement Pro paywall (Razorpay or Stripe for Indian market)
- [ ] Build firm/team accounts (up to 5 seats)

**Exit criteria**: End-to-end paid subscription flow working. First 10 paying users.

### Phase 4 — API (after revenue validation)
- [ ] Design REST API spec
- [ ] Rate limiting + API key management
- [ ] Developer documentation
- [ ] Pricing page

### Phase — Track Record (shipped 2026-06-29)
**Goal**: Give CAs and external observers a cryptographically-grounded, auditable scorecard of live predictions vs. actual outcomes. Builds credibility without requiring a paid subscription.

#### What was built

| File | Purpose |
|------|---------|
| `predictors/resolve_track_record.py` | Resolution engine: register pending rows, resolve against CBIC/Council docs, expire on deadline, Brier score, Wilson CI, SHA256 integrity |
| `data/track-record.json` (schema v2) | Migrated: full field set — `topic_id`, `predicted_at`, `source_committed_at`, `probability_at_resolution`, `resolved_at`, `outcome_doc_id`, `outcome_summary`, `days_to_resolution`, `horizon_days`; scorecard with `materialised`/`expired_no_match`/`brier_score`/`accuracy_ci_low`/`accuracy_ci_high` |
| `data/track-record.sha256` | SHA256 sidecar — resolver validates on startup, rewrites on exit; tamper-evident |
| `track-record.html` | Public scorecard page: 5-cell scorecard (calls / materialised / expired-miss / accuracy+CI / Brier score); accuracy suppressed until N≥10; `badge()` handles new status names + legacy |
| `data/predictions/history/` | Append-only daily snapshots of `latest.json`; immutable once written (no-overwrite guarantee) |
| `gst_foresight/__main__.py` | `python -m gst_foresight snapshot` (daily copy → `history/<today>.json`) and `python -m gst_foresight resolve [--force]` commands |
| `scripts/backfill_track_record.py` | Seeds `track-record.json` from git history of `latest.json`; idempotent; `--dry-run` supported; does NOT auto-resolve (corpus not in git) |
| `tests/test_track_record.py` | 37 tests — §9.1 registration/resolution/signal-vs-resolution; §9.2 expiry/cooldown; §9.3 snapshot; §9.4 scoring; §9.5 integrity; §9.6 datetime parsing; §integration full lifecycle |
| `.github/workflows/ingest_daily.yml` | `snapshot` step runs daily; `resolve` step runs daily (resolver self-gates on 4-day interval); git add extended to include `history/`, `track-record.json`, `track-record.sha256` |

#### Critical review decisions applied (before implementation)

1. **Binary accuracy → Brier score + Wilson CI**: raw hit rate on small N is misleading — Brier penalises confident wrong predictions; Wilson CI shows uncertainty range
2. **Git timestamp trust model softened**: methodology banner says "best-effort practical evidence" rather than cryptographic proof (clocks can be set; commit timestamps are not signed by CBIC)
3. **Exact `topic_id` match → multi-tag containment**: `topic_id in doc_tags[]` because a CBIC circular typically covers multiple topics; exact match silently blocked many valid resolutions
4. **Perpetual re-opening prevention**: 45-day cooldown (= `horizon_days × 0.5`) after `expired_no_match` before a new pending row can open on the same topic
5. **Finance Act added to `RESOLUTION_SOURCE_IDS`**: budget-enacted changes (Finance Act) are the highest-authority resolution document type
6. **Invisible probability drift captured**: `probability_at_resolution` field records the model's confidence *at resolution time*, not just at prediction time
7. **Day-of-year scheduling replaced**: `last_resolution_run` in JSON + interval check (4 days) — resilient to missed CI days; `DAY_OF_YEAR % 4` would drift if the job skips
8. **Small-N accuracy suppressed**: `min_resolved_for_accuracy: 10` — accuracy and CI are null until this threshold, with "Need N resolved (have X)" UI message
9. **Rebase-broken SHAs caught**: `git cat-file -t <sha>` validation before recording `source_commit` — invalid SHAs become `null` rather than broken GitHub links
10. **No tamper detection → SHA256 sidecar**: `data/track-record.sha256` written alongside JSON; resolver raises `RuntimeError` on mismatch at startup

#### GST Council resolution gating
GST Council minutes are resolution documents only when the minute text contains a decision keyword (`"approved"`, `"decided"`, `"resolved"`, `"ratified"`, `"recommended"`, `"notified"`, `"effective"`, `"waived"`, `"reduced"`, `"exempted"`). A plain discussion or deferral in council minutes does not count as resolution.

#### Exit criteria
- [x] 37 tests green
- [x] SHA256 sidecar initialised and integrity-verified
- [x] `last_resolution_run` set; CI self-gate operational
- [x] All 12 existing prediction rows migrated to schema v2
- [x] Pushed to `main` (commit `7ed10a9`)

---

## 10. Non-Goals (v1)

- **No scraping the GST portal** — requires GSP license, out of scope
- **No ITR or GST filing functionality** — this is intelligence, not compliance software
- **No mobile app** — web-first
- **No real-time data** — daily ingest cadence is sufficient for regulatory signals
- **No enforcement / demand notice tracking** — different product category

---

## 11. Open Questions

| Question | Decision needed by | Owner | Status |
|---|---|---|---|
| Sarvam API key — personal account or org account? | Before Phase 2 | Yashu | Using personal key; upgrade to org before launch |
| ChromaDB vs FAISS for vector store | Before Phase 1 | Engineering | ✓ Closed — Supabase pgvector chosen (2026-05-22) |
| Vercel edge function + Supabase free tier limits sufficient? | Before Phase 3 | Engineering | Open — test under load before Phase 3 launch |
| Razorpay vs Stripe for payments? | Before Phase 3 | Yashu | Open — Razorpay preferred (Indian market); plans not yet created |
| Hindi-language sources — priority vs. English-only first? | Before Phase 1 | Yashu | Deferred — English-only first; Sarvam tagging pass handles Hindi council docs |
| Domain name — gstforesight.in ✓ decided | Closed | Yashu | ✓ Closed |
| Auth provider — Clerk vs Supabase Auth vs Firebase? | Before Phase 3 | Yashu | Open — Supabase Auth is already in schema; Clerk is easier to wire but adds a dependency |

---

## 12. Success Metrics

| Phase | Metric | Target |
|---|---|---|
| Phase 1 | Backtest accuracy on full-text corpus | ≥ current (≥70% on 50-70% bucket) |
| Phase 2 | Query response accuracy (manual eval) | ≥85% grounded responses |
| Phase 2 | Monthly active users | 500 within 60 days of launch |
| Phase 3 | Free → Pro conversion rate | ≥5% |
| Phase 3 | Monthly recurring revenue | Rs 50,000 within 90 days |
| Phase 4 | API customers | 10 within 6 months of launch |
| Personalization | False-continuity rate (unrelated-query eval case) | 0% — model must not reference recent context when irrelevant |
| Personalization | Returning users (2nd+ query) seeing `personalization.applied: true` | ≥80% logged-in · ≥60% anon (session-only) |
| Personalization | Manual read: continuity framing feels natural, not forced (sample 10 multi-turn sessions) | Pass/fail judgment call, logged in eval notes |
| Track Record | Brier score at 20 resolved predictions | < 0.20 (random 50%-guesser baseline = 0.25) |
| Track Record | SHA256 integrity check: no tamper detected across CI runs | 0 integrity failures |
| Track Record | Resolver correctly skips runs within the 4-day interval | Verified by `last_resolution_run` timestamp |
| Track Record | Resolution classification: no AARs / court judgments counted as resolution docs | Confirmed by `TestResolutionDocType` test suite |
