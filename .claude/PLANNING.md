# Session: GST Foresight — X Data Pipeline + Date Bug Audit
**Date:** 2026-05-16
**Branch / Project:** main + feature/x-social-signal-pipeline · GST FORESIGHT

## Goal
Two independent workstreams: (1) design and implement a resilient open-source X (Twitter) scraping pipeline as a 9th signal source, and (2) audit and fix all date/time parsing bugs across scrapers and the prediction engine that were causing incorrect recency calculations.

## Context & Background

**X pipeline architecture**: twscrape (primary, vladkens/twscrape) + self-hosted RSSHub on Railway (fallback). twscrape patches every 2-4 weeks when X changes internals — just `pip install --upgrade twscrape`. RSSHub has separate maintenance cadence, giving redundancy. Both return `[]` on failure, never blocking other scrapers.

**Target accounts (hardcoded allowlist)**: `@FinMinIndia`, `@CBIC_India`, `@PIBFinance`, `@nsitharamanoffc`, `@Anurag_Office`. Only government/regulatory accounts; `account_type: "government"` hardcoded in metadata.

**Signal weight**: `social_regulatory_signal: 0.18` — between PIB (0.20) and ICAI (0.10). Strength model: base 0.30 + recency decay + engagement boost (RT>500: +0.10) + imminent language (+0.10). Hard cap 0.72 — tweets never dominate CBIC/Council signals. Hard 60-day staleness cutoff (tweets are real-time, not archives).

**asyncio.run() bridge**: `scrape()` is synchronous (all scrapers are), but twscrape is fully async. `asyncio.run()` bridges them. Safe because nothing else in the pipeline uses an event loop — if that changes, use `nest_asyncio`.

**Date bugs found**: 3 root causes: (1) CBIC `_parse_date_from_url` returns year-only Jan 1 placeholders; (2) GST Council date range strings "05-Oct-2020 - 12-Oct-2020" passed whole to strptime → None; (3) AAR rulings `date=None` hardcoded. Engine used deprecated `datetime.utcnow()` and ad-hoc timezone stripping per evaluator.

**Python environment**: project uses `.venv/` — always use `.venv/bin/python`, not system `python3`.

## Decisions Made

- **No paid X API**: twscrape (free, no API key) + RSSHub (self-hosted, free). Official X API v2 is $100/month — ruled out.
- **twscrape over Nitter**: Nitter is effectively dead for public instances; self-hosting has same maintenance burden as twscrape with less upkeep activity.
- **Tweets = single chunk**: 280-char tweets don't benefit from 3000-char chunking. Added 400-char short-content guard in `Chunker.chunk()`.
- **60-day staleness cutoff**: tweets that produce no follow-up policy action within 60 days are dead signals. PIB evaluator doesn't hard-cutoff — tweets get one because X is a real-time channel, not a regulatory archive.
- **`_parse_doc_date()` shared helper**: replace per-evaluator ad-hoc timezone stripping with a single normaliser (`re.sub(r"Z$|[+-]\d{2}:\d{2}$", "", ...)`) used everywhere.
- **`_utcnow()` wrapper**: replaces deprecated `datetime.utcnow()` with `datetime.now(timezone.utc).replace(tzinfo=None)`. Keeps naive datetime behaviour, removes deprecation warning.
- **Retroactive raw data patch**: 12 existing raw JSON files corrected in-place (3 CBIC, 9 GST Council) + stale processed/chunks deleted so pipeline re-processes them on next ingest.

## What Was Built / Changed

**Feature branch `feature/x-social-signal-pipeline`** (pushed to remote, not merged to main):
| File | Change |
|------|--------|
| `scrapers/twitter.py` | **New** — `XScraper(BaseScraper)` with twscrape primary + RSSHub fallback + graceful degradation |
| `gst_foresight/__main__.py` | Added `XScraper` import + `"twitter_signal": XScraper` to SCRAPERS dict |
| `config/sources.yaml` | Added `twitter_signal` source entry + `social_regulatory_signal: 0.18` weight |
| `predictors/engine.py` | Added `evaluate_social_regulatory_signal()` + wired into evaluators + horizon branch |
| `processors/chunker.py` | Short-content guard: ≤400 chars → single chunk, skip loop |
| `requirements.txt` | Added `twscrape>=0.12.0` and `feedparser>=6.0.11` |
| `.github/workflows/ingest.yml` | Added `X_ACCOUNT_JSON` and `RSSHUB_URL` secrets |

**Main branch (commit `502288a`)**:
| File | Change |
|------|--------|
| `scrapers/sources.py` | `CBICCircularScraper._parse_date_from_url` → `_parse_date_from_content` (reads "Dated:" from PDF content, circular number month, URL year fallback); GST Council date range split fix; `AARRulingScraper._parse_aar_date()` method + wired in |
| `predictors/engine.py` | `_parse_doc_date()` helper + `_utcnow()` wrapper added; all `datetime.utcnow()` replaced; evaluators use shared helpers |
| `data/predictions/latest.json` | Regenerated with corrected dates (12 raw docs patched) |

**Retroactive data fixes** (run inline, not committed separately):
- CBIC: `2025-01-01 → 2025-06-24` (circular 250), `2025-01-01 → 2019-11-05` (249), `2024-01-01 → 2024-12-04` (239)
- GST Council: 9 meetings with date ranges now have correct start dates (2016–2020)

## Blockers & Open Questions

- [ ] **X pipeline activation**: feature branch pushed but NOT merged. Requires two GitHub Actions secrets before it works: `X_ACCOUNT_JSON` (burner X account credentials) and `RSSHUB_URL` (Railway-deployed RSSHub instance).
- [ ] **RSSHub Railway deployment**: one-time setup — deploy `DIYgod/RSSHub` template on railway.app, get URL, add as `RSSHUB_URL` secret. Not done yet.
- [ ] **Burner X account**: need a dedicated account (not personal) for twscrape credential pool. Not created yet.
- [ ] **AAR rulings corpus is empty** (0 docs): scraper hasn't successfully scraped yet. `_parse_aar_date` is ready but untested with real data.
- [ ] **ICAI 95 docs with None dates**: year-only Jan-1 dates. `evaluate_industry_ask_repeat` doesn't use dates so no prediction impact — lower priority.
- [ ] Carry-over from previous sessions: `onViewAlert` fix, single-click ↗ fix, rotate `EMBED_SECRET`, rename GitHub repo.

## Next Steps

1. **Merge `feature/x-social-signal-pipeline`** once X credentials are available (create burner account + deploy RSSHub on Railway).
2. **Deploy RSSHub on Railway**: railway.app → new project → `DIYgod/RSSHub` template → get URL → add `RSSHUB_URL` secret to GitHub repo.
3. **Create burner X account** → format credentials as `[{"username":"...","password":"...","email":"...","email_password":"..."}]` → add as `X_ACCOUNT_JSON` GitHub secret.
4. **Run ingest with `--source twitter_signal`** to confirm first batch of tweets flows through tagger → chunker → Supabase.
5. **Verify corrected CBIC dates in predictions**: run `python -m gst_foresight predict` after next ingest and check that CBIC signal descriptions show correct dates (June/Dec 2025, not Jan 2025).
6. Carry-over: rotate `EMBED_SECRET`, wire `onViewAlert`, rename GitHub repo `gstpulse → gstforesight`.

## Key Commands / Code Snippets

**Run ingest for X only (test the new scraper)**:
```bash
cd ~/Projects/GST\ FORESIGHT
X_ACCOUNT_JSON='[{"username":"...","password":"...","email":"...","email_password":"..."}]' \
RSSHUB_URL="https://your-rsshub.railway.app" \
.venv/bin/python -m gst_foresight ingest --source twitter_signal --skip-embed
```

**Verify date fixes in existing corpus**:
```bash
cd ~/Projects/GST\ FORESIGHT
.venv/bin/python -c "
import json
from pathlib import Path
for f in Path('data/raw/cbic_circulars').glob('*.json'):
    d = json.loads(f.read_text())
    print(d['date'][:10], f.name[:50])
"
```

**Test `_parse_doc_date` helper**:
```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from predictors.engine import _parse_doc_date, _utcnow
assert _parse_doc_date('2025-06-24T00:00:00+05:30').tzinfo is None
print('OK')
"
```

**RSSHub Railway deploy**: railway.app → New Project → Deploy from Template → search `rsshub` → deploy → copy URL

**twscrape credentials format** (for `X_ACCOUNT_JSON` secret):
```json
[{"username": "burner_account", "password": "...", "email": "...", "email_password": "..."}]
```

**`social_regulatory_signal` strength model**:
```
base 0.30
+ recency: ≤7d +0.20 | ≤14d +0.12 | ≤30d +0.05 | older 0
+ engagement: RT>500 +0.10
+ imminent language +0.10 (notifi*, circular, effective, shortly, gazette)
+ multi-tweet: +0.04/extra tweet (max +0.12)
hard cap: 0.72
60-day hard staleness cutoff
```

---

# Session: GST Foresight — RAG Pipeline Debugging & Query Eval
**Date:** 2026-05-15
**Branch / Project:** main · GST FORESIGHT

## Goal
Get the end-to-end query pipeline working on live Vercel — embed → vector search → Sarvam-M — and run the 5-query eval suite against the dashboard suggestion chips. Three distinct failure layers had to be diagnosed and fixed: corrupt env vars in Vercel headers, the Supabase JS client bypassing cleanEnv, and Sarvam-M's context window being exceeded.

## Context & Background

**Rate limit table**: `public.usage` in Supabase. Has columns `ip`, `query_count`, `reset_at`. Reset with `DELETE FROM usage;` via SQL editor or MCP tool. Project ID: `ayyeviobzkcqyvtvfbeu`.

**sarvam-m context window**: **7192 tokens total** (input + output). At `match_count=8` with gst_council_minutes chunks (~2800 chars each), prompt tokens hit ~6344 → exceeds window when `max_tokens=1024`. Fixed by dropping to `match_count=5`, `max_tokens=800`.

**Vercel env var corruption**: Pasting long secrets (JWT, base64 keys) into the Vercel dashboard can introduce `
` mid-value from soft-wrapping. `.trim()` only strips ends; `cleanEnv()` (`replace(/[^ -~]/g, '')`) strips all non-printable ASCII anywhere in the string.

**Supabase JS client bypassed cleanEnv**: `createClient(url, key)` builds its own internal headers from the raw key passed in, ignoring any wrapper. Removed `@supabase/supabase-js` entirely; all three Supabase calls (rate-limit, embed, match_chunks) now use raw `fetch()` with `supabaseHeaders` built from `cleanEnv()`.

**Match count vs. sources**: gst_council_minutes chunks (~2800 chars each) are denser/longer than budget_speeches chunks (~2600 chars). This caused `gst_council_minutes`-heavy queries to always exceed context, while the `budget_speeches` query (RCM digital platforms) always succeeded — the telltale sign that led to the diagnosis.

## Decisions Made

- **Remove Supabase JS client entirely**: raw `fetch()` to all Supabase REST endpoints. Cleaner, no hidden header logic, cleanEnv works uniformly.
- **match_count 8→5**: stays well under the 7192-token window for all query types. Top-5 chunks by cosine similarity are almost always more relevant than 6-8.
- **max_tokens 1024→800**: enough headroom for the structured answer format.
- **cleanEnv over .trim()**: handles mid-value corruption, not just trailing newlines. Applied to all four env vars (SUPABASE_URL, SUPABASE_ANON_KEY, EMBED_SECRET, SARVAM_API_KEY).

## What Was Built / Changed

| File | Change |
|------|--------|
| `api/query.js` | Removed `@supabase/supabase-js` import; all Supabase calls use raw fetch; `cleanEnv()` helper applied to all env vars; `match_count` 8→5; `max_tokens` 1024→800; `_debug` fields removed from final version |
| `tests/test_query_quality.js` | New file: 5-query eval against live endpoint, auto-grounding check, structured JSON log saved to `tests/query_eval_{ts}.json` |
| `.gitignore` | Added `tests/query_eval_*.json` |

**Commits this session**: `792fac3`, `8df0d76`, `b7045d4`, `95460fd`, `b08efad`, `f7c5f79`, `db6835a`, `d3aa64c`

**PIB / prediction engine changes** (earlier in session, before the Vercel debugging):
- `predictors/engine.py`: Added `evaluate_government_forward_signal()` evaluator for `pib_finance` source; `generated_at` now appends `"Z"` (fixes 5:30h time display drift)
- `scrapers/sources.py`: `PRID_STEP` 5→25, `LOOKBACK_PRIDS` 4500→900 (18-day → 90-day window, same runtime)
- `config/sources.yaml`: `pib_finance` added as active source, `government_forward_signal: 0.20` in signal weights
- `index.html`: NOAA `n` formula sign fixed (`+ lng/360`); body background `useEffect` added to fix dark-mode bleed in light mode

## Blockers & Open Questions

- [ ] **Eval still not fully run**: context fix deployed but rate limit was exhausted before re-running the full 5-query eval. Need to reset `usage` table and re-run `node tests/test_query_quality.js`.
- [ ] **Remove `_debug` from llm_error**: commit `db6835a` added it for diagnosis; the context fix `d3aa64c` didn't clean it up. Remove before treating as production-ready.
- [ ] `onViewAlert` still opens `predictions[0]` not active prediction.
- [ ] Single-click ↗ doesn't open `ScreenPredictionDetail` (double-click works).
- [ ] Rotate `EMBED_SECRET` — value appeared in prior session plain text.
- [ ] GitHub repo still named `gstpulse` (rename pending).
- [ ] Run `tests/test_security.js` against live Vercel URL.
- [ ] Confirm end-to-end ingest → `latest.json` → live predictions flow.

## Next Steps

1. **Reset usage table** (`DELETE FROM usage;`) and run `node tests/test_query_quality.js` to confirm all 5 queries return grounded answers.
2. **Remove `_debug` from `llm_error` path** in `api/query.js` (commit `db6835a` added it, not yet cleaned).
3. **Wire `onViewAlert`** to the active prediction.
4. **Wire single-click ↗ → `ScreenPredictionDetail`**.
5. **Rotate `EMBED_SECRET`**: `openssl rand -base64 32`, update Supabase Secrets + Vercel env + `.env`.
6. **Rename GitHub repo** `gstpulse` → `gstforesight`.
7. **Run `tests/test_security.js`** against live Vercel.

## Key Commands / Code Snippets

**Reset rate limit (Supabase MCP or SQL editor)**:
```sql
DELETE FROM usage;
```

**Run query eval**:
```bash
cd ~/Projects/GST\ FORESIGHT && node tests/test_query_quality.js
```

**cleanEnv helper** (in `api/query.js`):
```js
const cleanEnv = v => (v ?? '').replace(/[^ -~]/g, '');
```

**sarvam-m limits**:
- Context window: **7192 tokens** (input + output combined)
- Safe operating point: `match_count=5`, `max_tokens=800` → ~4000 input tokens, well within budget

**Supabase raw fetch pattern** (replaces JS client):
```js
const supabaseUrl = cleanEnv(process.env.SUPABASE_URL);
const supabaseKey = cleanEnv(process.env.SUPABASE_ANON_KEY);
const supabaseHeaders = {
  'Authorization': `Bearer ${supabaseKey}`,
  'apikey': supabaseKey,
  'Content-Type': 'application/json',
};
// Then: fetch(`${supabaseUrl}/rest/v1/rpc/match_chunks`, { method:'POST', headers: supabaseHeaders, body: ... })
```

**Diagnose Sarvam context overflow locally**:
```bash
# Build exact prompt + call Sarvam to get real error message
python3 - << 'EOF'
# (see session transcript for full script)
# Key: ERROR 422 means prompt_tokens + max_tokens > 7192
EOF
```

---

# Session: GST Foresight — Security Hardening, RAG Validation & UI Fixes
**Date:** 2026-05-15
**Branch / Project:** main · GST FORESIGHT

## Goal
Full security audit and hardening of the API layer, end-to-end validation of the RAG query chain (embed → match_chunks → Sarvam), and three UI fixes: black bottom gap on sub-screens, cluttered header, and dark mode triggering during daytime.

## Context & Background

**Architecture**: Single `index.html` (React 18 + Babel standalone, no build step) deployed to Vercel as a static site. Edge function at `api/query.js` (Vercel edge runtime). Supabase project `ayyeviobzkcqyvtvfbeu` handles vector search via `match_chunks` RPC and query-time embedding via `embed` edge function. Sarvam-M generates answer text.

**Environment variables** (all four now required):
- `SUPABASE_URL=https://ayyeviobzkcqyvtvfbeu.supabase.co`
- `SUPABASE_ANON_KEY` — public, used by browser and Vercel edge
- `SUPABASE_SERVICE_KEY` — local `.env` only, never on Vercel, used by Python ingest pipeline
- `SARVAM_API_KEY` — Vercel env only
- `EMBED_SECRET` — **new this session**, required on both Vercel and Supabase Function secrets

**Schema state**: `match_chunks` RPC updated with `match_threshold` param (default 0.3) and `SECURITY DEFINER`. `check_and_increment_usage` race condition fixed via `SELECT FOR UPDATE`. Chunks RLS tightened to block direct anon REST reads. All migrations applied to live Supabase via SQL editor.

**NOAA dark mode**: rise/set times and current time must ALL be compared in UTC (not local time) to avoid browser timezone / VPN mismatches. Fixed this session.

**Domain decided**: `gstforesight.in` / `gstforesight.vercel.app`. Repo still named `gstpulse` on GitHub — rename pending.

## Decisions Made

- **`EMBED_SECRET` required (not optional)**: embed function now returns 503 if unset, 401 if wrong. Python ingest pipeline also requires it at startup. Prevents anon key holders from calling embed directly and bypassing rate limits.
- **CORS origin allowlist over wildcard**: `api/query.js` now uses a `Set` of allowed origins instead of `*`. Vercel headers block also locks `/api/*` to `gstforesight.vercel.app`.
- **match_chunks `SECURITY DEFINER`**: lets anon call the RPC while blocking direct REST reads of the chunks table. Closes corpus extraction via `/rest/v1/chunks?select=*`.
- **Stats moved from header to ticker**: docs indexed, active predictions, backtest accuracy relocated to ticker bar left prefix. Header centre is now clean: date/time + "Foresight on demand".
- **UTC-only NOAA comparison**: `getUTCHours()` throughout isDayTime to eliminate timezone ambiguity.
- **`timeZone: 'Asia/Calcutta'` forced on updatedLabel**: time label always shows IST regardless of browser timezone.
- **`shell()` helper for sub-screens**: all sub-screen early returns now wrapped in `100vw/100vh` container via a one-liner helper — same fix applied to all 5 screens (query, prediction, source, alert, pricing).

## What Was Built / Changed

| File | Change |
|------|--------|
| `supabase/schema.sql` | TOCTOU race fix in `check_and_increment_usage` (SELECT FOR UPDATE); `match_chunks` gets `match_threshold` + `SECURITY DEFINER + search_path = public, extensions`; chunks RLS drops open anon-read policy |
| `api/query.js` | `sanitizeQuery()` strips control chars; `buildPrompt` uses XML delimiters + injection-resistance instruction; CORS origin allowlist; 8 KB body size guard; internal error detail stripped from 500; `EMBED_SECRET` always sent (not conditional) |
| `supabase/functions/embed/index.ts` | `EMBED_SECRET` now required (503 if unset, 401 if missing/wrong); internal error detail stripped from 502 |
| `index.html` | SRI integrity hashes on React 18, ReactDOM 18, Babel CDN scripts; `shell()` helper fixes black bottom gap on all sub-screens; header centre cleaned to date/time + tagline; stats moved to ticker; `getUTCHours()` in NOAA comparison; `timeZone:'Asia/Calcutta'` on updatedLabel |
| `vercel.json` | CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy headers; `/api/*` CORS locked to deployment origin |
| `processors/embedder.py` | `EMBED_SECRET` now required at startup (RuntimeError if missing) |
| `tests/test_security.js` | New: 20-test vulnerability suite covering body size, prompt injection, CORS, rate limit, sanitization, error leak |
| `.env` | `EMBED_SECRET` added |
| `README.md`, `PRODUCT_SPEC.md` | `gstpulse` → `gstforesight` rename; domain question closed |

**Commits this session**: `fa16a80`, `0eb982a`, `07f1726`, `0f406c0`, `e77891d`

## Blockers & Open Questions

- [ ] GitHub repo still named `gstpulse` — rename in GitHub Settings → then run: `git remote set-url origin https://github.com/slenderdeveloperman/gstforesight.git`
- [ ] **Rotate `EMBED_SECRET`** — the value was shared in plain text in this session. Generate a new one: `openssl rand -base64 32`, update Supabase Secrets + Vercel env + `.env`.
- [ ] `onViewAlert` always opens alert for `predictions[0]`, not the active prediction — known gap in `App`'s `goAlert` signature.
- [ ] Single-click on ↗ button doesn't open `ScreenPredictionDetail` — double-click works, single-click only sets `activeId` in signal panel.
- [ ] Alert screen and Pricing screen: content is mock, no real interactivity wired.
- [ ] Ingest pipeline not tested end-to-end with live Supabase — `data/predictions/latest.json` flow unverified.
- [ ] Run security test suite against live Vercel URL: `BASE_URL=https://gstforesight.vercel.app node tests/test_security.js`

## Next Steps

1. **Rotate `EMBED_SECRET`** — priority because old value appeared in chat.
2. **Rename GitHub repo** from `gstpulse` → `gstforesight` + update remote URL locally.
3. **Wire `onViewAlert`** to active prediction (not `predictions[0]`).
4. **Wire single-click ↗ → `ScreenPredictionDetail`**.
5. **Run ingest pipeline** locally to confirm `latest.json` is written and live predictions load on the deployed site.
6. **Run `tests/test_security.js`** against the live Vercel URL to confirm all security fixes are deployed correctly.
7. **Phase 2 remaining**: query UI is live but the "5 queries/month" counter in `localStorage` and the Pro paywall flow are not wired to the backend yet.

## Key Commands / Code Snippets

**Test full RAG chain locally (requires .env)**:
```bash
cd ~/Projects/GST\ FORESIGHT && python3 - << 'EOF'
import urllib.request, json
env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
# ... (see previous session for full script)
EOF
```

**Run security tests against live site**:
```bash
BASE_URL=https://gstforesight.vercel.app node tests/test_security.js
```

**Rotate EMBED_SECRET**:
```bash
openssl rand -base64 32
# Then update: Supabase → Functions → embed → Secrets
#              Vercel → Settings → Environment Variables
#              .env in project root
```

**match_chunks RPC signature (live on Supabase)**:
```sql
match_chunks(query_embedding vector(384), match_count int default 8, match_threshold float default 0.3)
-- SECURITY DEFINER, set search_path = public, extensions
```

**NOAA isDayTime — correct UTC approach**:
```js
const toUTC = jd => { const d = new Date((jd - 2440587.5) * 86400000); return d.getUTCHours() * 60 + d.getUTCMinutes(); };
// isDayTime: compare UTC minutes throughout — never getHours() (browser-timezone-dependent)
```

**updatedLabel with forced IST**:
```js
new Date(ts).toLocaleTimeString('en-IN', { hour:'2-digit', minute:'2-digit', timeZone:'Asia/Calcutta', hour12:false })
```

**Supabase infrastructure (confirmed live)**:
- RPC `check_and_increment_usage(client_ip, free_limit)` → `{ allowed, remaining, reset_at }`
- RPC `match_chunks(query_embedding, match_count, match_threshold)` → rows with similarity scores
- Edge function slug: `embed` — requires `X-Embed-Secret` header (now mandatory)
- Project ID: `ayyeviobzkcqyvtvfbeu`

---

# Session: GST Foresight — Design v2 Implementation + Vercel Fix
**Date:** 2026-05-14
**Branch / Project:** main · GST FORESIGHT

## Goal
Implement a second design bundle (Direction A "Refined Terminal" v2) fetched from the Claude Design handoff into the production `index.html`. Along the way, diagnose and fix all Vercel deployment errors that had been blocking the live site, and add two UX improvements: logo-to-home navigation and a sunrise/sunset auto dark mode.

## Context & Background

**Architecture**: Single self-contained `index.html` — React 18 + Babel standalone, all component JSX inline in a `<script type="text/babel">` block. No build step. Deployed to Vercel as a static site with one edge function (`api/query.js`).

**Backend**: Supabase project `ayyeviobzkcqyvtvfbeu`. Two active RPCs: `check_and_increment_usage` and `match_chunks`. Supabase edge function `embed` (slug) handles query-time embedding. Sarvam-M generates the answer text.

**Edge function format (critical)**: Vercel uses `export default async function handler(request)` — NOT the Cloudflare Workers `export default { fetch }` object pattern. This was the root cause of the 500 errors.

**Environment variables on Vercel** (only three needed):
- `SUPABASE_URL=https://ayyeviobzkcqyvtvfbeu.supabase.co`
- `SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...`
- `SARVAM_API_KEY=sk_t1anlfjj_dR3RVg1JnREybKBp1fUWPQ1w`
- `SUPABASE_SERVICE_KEY` — stays local only, never on Vercel

**Design source**: Fetched from Claude Design handoff bundle, extracted to `/tmp/gst_design2/`. Four files read: `direction-a.jsx`, `gst-screens.jsx`, `GST Foresight - Flows.html`, `chat1.md`.

**Target audience**: CAs in private practice — quick scannable foresight before client calls.

## Decisions Made

- **Layout change**: 3-column (320px left query | center predictions | 380px detail) → 2-column (`1fr 380px`) with query dock at the bottom. Rationale: central real estate should be predictions, not query input.
- **Chrome hierarchy**: Header + ticker fused into one `bg2` plate with `2px solid border2` bottom edge. Eliminates stacked 1px borders that made the top read as a striped slab.
- **`RuleLabel` over `ColHeader`**: Transparent background + accent dot + hairline only. Column hierarchy comes from position, not filled bars competing with chrome.
- **Text density**: `tx` object for predictions (~25% smaller), `sx` object for signal breakdown (~25% smaller) — more rows visible, less visual noise.
- **onAsk flow**: Query → navigate immediately to `ScreenQueryResponse` on API completion. No inline response in the home dock (no room). Button shows "Analysing…" during wait.
- **Auto dark mode**: NOAA sunrise equation (not a fixed 6am/6pm) + `navigator.geolocation` for accuracy. `manualOverride` ref prevents auto-updates after user manually toggles — so the button still works.
- **`@xenova/transformers` deleted from Vercel**: Local-Python-only; incompatible with V8 edge runtime. Supabase edge function `embed` already handles query-time embedding.
- **`vercel.json` `functions` block removed**: Edge runtime declared via `export const config = { runtime: 'edge' }` in the JS file, not in `vercel.json`. Version strings in `vercel.json` are only for community runtimes.

## What Was Built / Changed

| File | Change |
|------|--------|
| `index.html` | Full design v2 implementation (see below) |
| `api/query.js` | Rewritten: correct Vercel export format, `x-forwarded-for` for IP, top-level try/catch |
| `api/embed.js` | Deleted — dead code, `@xenova/transformers` incompatible with edge runtime |
| `package.json` | Removed `@xenova/transformers` |
| `vercel.json` | Removed invalid `functions` block |

**`index.html` changes** (design v2):
- Chrome plate: header (64px) + ticker (36px, 13px font) fused. Logo 34px box, FORESIGHT 16px, subtitle 11px.
- Header meta font: 9.5px → 12px. Padding: 18px → 22px. Header meta gap: 24 → 28.
- GitHub button removed. API button: `muted` prop (dashed border, 0.6 opacity, "API · soon").
- Layout: 2-column `1fr 380px`. No left query column.
- `RuleLabel` component: transparent bg, 4×4 accent square, `10.5px` mono.
- Prediction table grid: `56px→48px` widths. `tx` object text sizes: `id:8, topic:11, changeType:8, delta:8, probBig:18, probPct:8, horizon:8, colHead:8, spark:20`.
- Signal breakdown `sx` object: `probBig:33, probLab:7.5, topic:11, horizon:8, statLab:7, statVal:10, section:7.5, sigType:7.5, sigStr:8, sigDesc:9, docChip:7, watchBody:9, btn:8`.
- Query dock: bottom-centered, `max-width:920px`, single-line `<input>`, `›` prefix icon, centered "Try" suggestion chips, accent tick on top edge.
- `Pill` component: `muted` prop → dashed border, dimmed, " · soon" suffix.
- `Stat` component: `fs` prop (`{lab, val}`) for font size control.
- `SectionLabel` component: `fs` prop (default 9).
- All 5 sub-screens (`ScreenChrome`): 64px header, 34px logo, 16px FORESIGHT, 11px subtitle, 12px breadcrumb. GitHub removed, API greyed.
- Logo block on sub-screens: `onClick={onBack}`, `cursor:pointer`.
- Auto dark/light: `computeSunTimes(lat, lng)` via NOAA solar equation in plain `<script>`. `isDayTime()` with 20°N/78°E fallback. `manualOverride` ref gates auto-check.

## Blockers & Open Questions

- [ ] `EMBED_SECRET` — optional shared secret between `api/query.js` and Supabase `embed` function for extra security. Not set; low priority but worth adding later.
- [ ] `data/predictions/latest.json` — the ingest pipeline writes here; live data flow not tested end-to-end in this session.
- [ ] Alert screen and Pricing screen: content unchanged from v1; visual chrome updated. No interactivity wired beyond mock.
- [ ] The `onViewAlert` call from the signal breakdown panel always opens alert for `predictions[0]`, not the `active` prediction. This is a known gap (App's `goAlert` signature).

## Next Steps

1. **Test the live query flow end-to-end**: open the deployed Vercel URL, submit a query, confirm `ScreenQueryResponse` renders with real Sarvam answer + source chips.
2. **Wire `onViewAlert` to the active prediction**: in `App`, change `onViewAlert={() => goAlert(predictions[0])}` to pass the active prediction from `DirectionA` (requires adding a callback pattern or lifting state).
3. **Wire prediction row click → `ScreenPredictionDetail`**: double-click currently works but single-click only sets `activeId` in the signal panel. Consider making single-click on the ↗ button open the deep-dive screen.
4. **Add `EMBED_SECRET`** to Vercel env vars and `api/query.js` `Authorization` header for the Supabase embed call.
5. **Run ingest pipeline locally** and confirm `data/predictions/latest.json` is written and served correctly.

## Key Commands / Code Snippets

**Vercel edge function format** (must use this, not Cloudflare Workers pattern):
```js
export const config = { runtime: 'edge' };
export default async function handler(request) {
  // ...
}
```

**IP address in edge runtime** (no `@vercel/functions` needed):
```js
const ip = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ?? '0.0.0.0';
```

**NOAA sunrise equation (inline, no deps)**:
```js
function computeSunTimes(lat, lng) {
  const DEG = Math.PI / 180;
  const JD = Date.now() / 86400000 + 2440587.5;
  const n  = Math.round(JD - 2451545.0 - 0.0009 - lng / 360);
  const J_noon = 2451545.0 + 0.0009 + lng / 360 + n;
  const M_deg  = (357.5291 + 0.98560028 * (J_noon - 2451545)) % 360;
  const M = M_deg * DEG;
  const C = 1.9148*Math.sin(M) + 0.0200*Math.sin(2*M) + 0.0003*Math.sin(3*M);
  const lam = ((M_deg + C + 102.9372 + 180) % 360) * DEG;
  const J_trans = J_noon + 0.0053*Math.sin(M) - 0.0069*Math.sin(2*lam);
  const sin_d = Math.sin(lam) * Math.sin(23.4397*DEG);
  const cos_d = Math.cos(Math.asin(sin_d));
  const cos_ha = (Math.sin(-0.8333*DEG) - Math.sin(lat*DEG)*sin_d) / (Math.cos(lat*DEG)*cos_d);
  if (cos_ha < -1) return { rise: 0, set: 1440 };
  if (cos_ha >  1) return { rise: 720, set: 720 };
  const ha_deg = Math.acos(cos_ha) / DEG;
  const toLocal = jd => { const d = new Date((jd-2440587.5)*86400000); return d.getHours()*60+d.getMinutes(); };
  return { rise: toLocal(J_trans - ha_deg/360), set: toLocal(J_trans + ha_deg/360) };
}
```

**`vercel.json` (minimal — no `functions` block)**:
```json
{ "buildCommand": null, "outputDirectory": ".", "framework": null }
```

**Supabase infrastructure** (confirmed active):
- RPC `check_and_increment_usage(ip, max_per_day)` → `{ allowed: bool, remaining: int }`
- RPC `match_chunks(query_embedding, match_count, match_threshold)` → rows
- Edge function slug: `embed` (handles query-time embedding via Xenova locally / remote)

---

# Session: GST Foresight — P0/P2 Bug Fixes
**Date:** 2026-06-01
**Branch / Project:** main · GST FORESIGHT
**Commit:** `9027ec9`

## Goal
Fix all P0 production bugs and P2 data pipeline integrity issues identified in the post-Phase-3 codebase audit.

## What Was Fixed

| File | Fix | Category |
|------|-----|----------|
| `api/query.js:260` | Removed `_debug` field from `llm_error` response — was leaking internal Sarvam HTTP status to clients | P0 / OWASP info disclosure |
| `api/subscribe.js` | VALID_TOPIC_IDS had 6 of 12 IDs mismatched vs `data/predictions/latest.json`. Alerts for `rcm_expansion`, `gstr_compliance`, `valuation_rules`, `crypto_vda`, `composition_scheme`, `council_outcomes` would never fire | P0 / silent alert failure |
| `processors/chunker.py:25` | `text.replace('\x00', '')` before chunking — PDF extractors occasionally emit null bytes; Postgres rejects the text silently causing upsert failures | P0 / corpus gap |
| `scrapers/sources.py` (AARScraper) | CBIC retired `advance-ruling.html`. Scraper now tries 3 candidate URLs in order, surfaces a loud WARNING when all fail instead of silently returning `[]` | P2 / silent 0-doc failure |
| `scrapers/sources.py` (IndianKanoonScraper:468) | `full_text_extracted: bool(snippet)` → `False` — search snippets are not full text; marks docs for `reextract` correctly | P2 / incorrect metadata |
| `gst_foresight/__main__.py` (cmd_reextract) | Passes `max_bytes=50MB` when source is `gst_council_minutes` (default 10MB cap skips 20-30MB council PDFs) | P2 / truncated corpus |

## Canonical Topic IDs (from data/predictions/latest.json)
These are the ground truth IDs that all components must agree on:
```
ims_itc_flow, rate_rationalisation, itc_eligibility, classification_disputes,
msme_composition, place_of_supply, real_estate, rcm_coverage,
valuation, return_format, e_invoicing, gst_on_crypto_vda
```

## AAR Scraper Status
CBIC removed the advance rulings listing page (URL returns 404 regardless of User-Agent — not a bot-block, the page is genuinely gone). The scraper now tries:
1. `https://cbic-gst.gov.in/advance-rulings-list.html`
2. `https://cbic-gst.gov.in/advance-ruling-orders.html`
3. `https://cbic-gst.gov.in/advance-ruling.html` (old, 404s)

When the correct new URL is known, add it as the first candidate. The GST Council site (`gstcouncil.gov.in/advance-rulings`) only links to PDF lists of AAR authorities, not individual orders.

## Caveat: IndianKanoon Existing Raw Docs
Existing raw docs in `data/raw/court_judgments/` that were stored with `full_text_extracted: True` will NOT be re-fetched by `reextract` (it reads the stored flag). To force re-extraction of full judgment text, either:
- Delete the raw JSON files for court_judgments and re-scrape
- Or run: `python3 -c "import json,pathlib; [f.write_text(json.dumps({**json.loads(f.read_text()), 'metadata': {**json.loads(f.read_text()).get('metadata',{}), 'full_text_extracted': False}}, indent=2)) for f in pathlib.Path('data/raw/court_judgments').glob('*.json')]"`

## Open Items (not yet fixed — held off by user)
- [ ] P1: Add `SUPABASE_SERVICE_KEY`, `RESEND_API_KEY`, `ALERT_FROM_EMAIL`, `RAZORPAY_WEBHOOK_SECRET` to Vercel env
- [ ] P1: Enable Google OAuth in Supabase Auth dashboard → Providers
- [ ] P1: Create Razorpay plans `plan_pro_individual` / `plan_pro_firm` in dashboard
- [ ] P3: `onViewAlert` opens `predictions[0]` instead of active prediction (`index.html`)
- [ ] P3: Single-click ↗ doesn't open `ScreenPredictionDetail` (double-click works)
- [ ] P3: `ScreenSourceDoc` shows static mock data — not wired to real chunks
- [ ] P4: Rotate `EMBED_SECRET` (value appeared in plaintext in a prior session)
- [ ] P4: Add SRI hash to Supabase CDN `<script>` tag in `index.html`
- [ ] P4: Run `node tests/test_security.js` against live Vercel URL
- [ ] P4: Run `node tests/test_query_quality.js` (reset `DELETE FROM usage;` first)

---

# Session: GST Foresight — CI Reliability Fix + Architecture Doc
**Date:** 2026-06-24
**Branch / Project:** main · GST FORESIGHT
**Commits:** `6afc534` → rebased to `00f7df5`

## Goal
Fix the failing `ticker` step in the daily GitHub Actions ingest workflow, and produce a comprehensive architecture reference document.

## What Was Fixed

| File | Fix |
|------|-----|
| `scripts/scrape_ticker.py` | `sys.exit(1)` on missing `data/processed/` replaced with empty-ticker write + `sys.exit(0)` — CI no longer hard-fails when all scrapers return nothing (network issues, GOI sites down) |
| `gst_foresight/__main__.py` | `cmd_ingest` now proactively `mkdir` `data/processed/` and `data/predictions/` at startup, before any scraper runs — ensures downstream steps always find the dirs even on total scraper failure |

**Root cause**: `data/processed/` is gitignored. In CI after a fresh checkout the directory doesn't exist. `tagger.tag_and_save()` only creates it when new documents arrive — if all scrapers fail (GOI sites down, network timeout), the directory is never created, and the ticker step hard-exits with code 1, marking the entire CI run red.

**Resolution**: Double-defence — create the directory proactively at ingest startup (defence 1), and make the ticker write an empty `{"items": []}` instead of aborting if the directory is still somehow absent (defence 2).

## What Was Created

- **`ARCHITECTURE.md`** — comprehensive architecture + tech stack reference document covering: system diagram, all 8 scrapers, 5-stage ingest pipeline, RAG query flow, full Supabase schema, 14 named architectural decisions with rationale, security model, CI/CD workflows, local dev commands.

## Ticker refresh
`data/news/ticker.json` regenerated from latest corpus — 490 docs loaded, 40 items written. Now includes new PIB Finance releases, ICAI June newsletters, HC/SC rulings from June 2026, and CBIC Circulars 239/249/250.

## Push notes
Remote had a CI-bot commit on `main` (bot had also regenerated the ticker). Resolved via `git pull --rebase`; conflict in `ticker.json` only (both sides auto-generated it); took local version. Clean push to `00f7df5`.

## Open Items Carried Forward

**P0 / blockers:**
- [ ] Confirm live query works end-to-end on deployed Vercel site (been pending since 2026-05-15; multiple sessions have approached but not confirmed)
- [ ] Run `reextract` for 39 docs with `full_text_extracted: False` — URL encoding fix is in place, GST Council 50/53/54 need OCR pass
- [ ] Rebuild predictions on full-text corpus after reextract; validate against backtest cases

**P1 / important:**
- [ ] Run 20-query manual eval (`node tests/test_query_quality.js`); target ≥85% grounded responses — reset `DELETE FROM usage;` first
- [ ] Add Phase 3 Vercel env vars: `RESEND_API_KEY`, `ALERT_FROM_EMAIL`, `RAZORPAY_WEBHOOK_SECRET`
- [ ] Enable Google OAuth in Supabase Auth dashboard → Providers
- [ ] Create Razorpay plans `plan_pro_individual` / `plan_pro_firm`

**P2 / quality:**
- [ ] Sarvam semantic tagging pass for GST Council minutes (`scripts/semantic_tag_council.py` exists but not run on full corpus) — regex tagger misses "kept in abeyance", "further deliberation"
- [ ] Wire `ScreenSourceDoc` to real chunks from Supabase (currently shows static mock data)
- [ ] AAR scraper: all 3 candidate URLs still 404; find the new CBIC advance rulings URL and add as first candidate

**P3 / polish:**
- [ ] X/Twitter pipeline: feature branch `feature/x-social-signal-pipeline` is pushed; needs burner X account + RSSHub deploy on Railway before merge
- [ ] Deploy RSSHub on Railway: new project → DIYgod/RSSHub template → get URL → add `RSSHUB_URL` GitHub secret
- [ ] `ScreenQueryResponse` → `remaining_queries` counter display (backend returns it, UI may not render it)

**P4 / deferred:**
- [ ] Run `node tests/test_security.js` against live Vercel URL (last confirmed pass: 25/25 on 2026-05-22)
- [ ] Add SRI hash to Supabase CDN `<script>` tag in `index.html`
- [ ] Rotate `EMBED_SECRET` if not already done after 2026-05-22

## What's Next (ordered)

1. **`python -m gst_foresight reextract`** — clears the 39-doc backlog of title-only entries. Run locally, then `python -m gst_foresight predict` to rebuild predictions on full text.
2. **Live query confirmation** — open `https://gstforesight.vercel.app`, submit a real CA query, verify structured Sarvam response.
3. **20-query eval** — `DELETE FROM usage; node tests/test_query_quality.js`. Log failures for prompt tuning.
4. **Phase 3 auth decision** — pick Supabase Auth (already in schema) or Clerk. Schema is fully written; just need the provider wired up.
5. **Alert system end-to-end test** — `scripts/send_alerts.py` is written; needs `RESEND_API_KEY` + at least one row in `alert_subscriptions` to test.
6. **Merge X pipeline** — once burner account + RSSHub are ready.

## Key Commands

```bash
# Re-extract full text for 39 missing docs
cd ~/Projects/GST\ FORESIGHT && .venv/bin/python -m gst_foresight reextract

# Rebuild predictions after reextract
.venv/bin/python -m gst_foresight predict

# 20-query eval (reset usage first in Supabase SQL editor: DELETE FROM usage;)
node tests/test_query_quality.js

# Run semantic tagging pass on council minutes
.venv/bin/python scripts/semantic_tag_council.py

# Status check
.venv/bin/python -m gst_foresight status
```
