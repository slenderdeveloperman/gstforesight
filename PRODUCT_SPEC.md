# GST Foresight — Product Specification
**Version**: 0.2 (pre-build)
**Last updated**: 2026-05-09
**Status**: Approved for implementation

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

### 3.3 Topic Alerts (Phase 3 — requires auth)
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
│      ├── Embedder (Sarvam embed-v1 or sentence-transformers)│
│      ├── Vector store writer (ChromaDB flat file)           │
│      └── Prediction engine → latest.json                   │
│          └── git commit + push → GitHub Pages deploys       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  GitHub Pages (static hosting)                              │
│  ├── index.html (dashboard + query UI)                      │
│  └── data/predictions/latest.json                           │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Cloudflare Worker (query API endpoint)                     │
│  ├── Receives query from dashboard fetch()                  │
│  ├── Embeds query → vector search against ChromaDB export  │
│  ├── Retrieves top-k chunks                                 │
│  └── Calls Sarvam API → returns structured response        │
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

### 5.3 Query Flow (Cloudflare Worker)

```
User types query in dashboard
    ↓
fetch() POST to Cloudflare Worker endpoint
    ↓
Worker: embed query using same model as corpus
    ↓
Worker: similarity search against ChromaDB export (loaded into Worker KV)
    ↓
Worker: retrieve top 8 chunks + their metadata
    ↓
Worker: build prompt for Sarvam API
    ↓
Sarvam: generate structured foresight response
    ↓
Worker: return JSON response to dashboard
    ↓
Dashboard: render response in query panel
```

### 5.4 Rate Limiting (free tier enforcement)
- 5 free queries tracked via localStorage (no backend needed for soft limit)
- Hard limit enforced at Cloudflare Worker level via IP-based rate limiting (Workers built-in)
- No auth required for free tier — friction-free

### 5.5 Vector Store Strategy
- ChromaDB persistent client writes to `data/vectors/` directory
- Total estimated size: ~30 MB for full corpus
- Committed to GitHub repo (under the 100MB file limit)
- Cloudflare Worker loads vector export from KV store (updated on each GitHub Actions run via Cloudflare API)

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
- [ ] Add `pdfplumber` and `pymupdf` to requirements.txt
- [ ] Add `fetch_pdf_text(url)` method to `BaseScraper`
- [ ] Update all 4 active scrapers to follow PDF links and extract full text
- [ ] Add chunker module (`processors/chunker.py`)
- [ ] Add embedder module (`processors/embedder.py`) using sentence-transformers
- [ ] Set up ChromaDB persistent client in `data/vectors/`
- [ ] Update ingest CLI to run chunk → embed pipeline after tagging
- [ ] Rebuild predictions on full-text corpus, validate against backtest cases

**Exit criteria**: Prediction engine running on full document text. Backtest accuracy unchanged or improved.

### Phase 2 — Query interface (after Phase 1)
**Goal**: Users can ask specific questions and get grounded foresight responses.

Tasks:
- [ ] Build Cloudflare Worker (`workers/query.js`)
- [ ] Integrate Sarvam API into Worker (key stored in Worker secrets)
- [ ] Add query UI panel to `index.html`
- [ ] Implement localStorage-based free query counter (5/month)
- [ ] Add Sarvam semantic tagging pass for council minutes and Hindi docs
- [ ] Test with 20 representative CA queries
- [ ] Deploy Worker, wire up dashboard fetch()

**Exit criteria**: Query model returns accurate, grounded responses on 85%+ of test queries.

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

---

## 10. Non-Goals (v1)

- **No scraping the GST portal** — requires GSP license, out of scope
- **No ITR or GST filing functionality** — this is intelligence, not compliance software
- **No mobile app** — web-first
- **No real-time data** — daily ingest cadence is sufficient for regulatory signals
- **No enforcement / demand notice tracking** — different product category

---

## 11. Open Questions

| Question | Decision needed by | Owner |
|---|---|---|
| Sarvam API key — personal account or org account? | Before Phase 2 | Yashu |
| ChromaDB vs FAISS for vector store | Before Phase 1 | Engineering |
| Cloudflare Workers free tier sufficient? (100K req/day) | Before Phase 2 | Engineering |
| Razorpay vs Stripe for payments? | Before Phase 3 | Yashu |
| Hindi-language sources — priority vs. English-only first? | Before Phase 1 | Yashu |
| Domain name — gstforesight.in ✓ decided | Closed | Yashu |

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
