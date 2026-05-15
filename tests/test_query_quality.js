/**
 * tests/test_query_quality.js
 *
 * Query quality evaluation for GST Foresight.
 * Submits the 5 dashboard suggestion queries against the Vercel endpoint
 * and produces a structured log for manual grounding assessment.
 *
 * Usage:
 *   node tests/test_query_quality.js                          ← live Vercel
 *   BASE_URL=http://localhost:3000 node tests/test_query_quality.js  ← local dev
 *
 * ⚠ Rate limit: each run consumes up to 5 free queries from the calling IP's
 *   monthly quota. Use localhost for repeated test runs.
 *
 * Output:
 *   Console  — per-query status, sources, answer preview
 *   File     — tests/query_eval_{timestamp}.json (full structured log)
 *              Fill in manual_verdict per result after reviewing:
 *              "grounded" | "plausible" | "off-target"
 */

const BASE_URL = process.env.BASE_URL ?? 'https://gstforesight.vercel.app';
const QUERY_URL = `${BASE_URL}/api/query`;

// Mirror of the suggestion chips displayed under the query box in index.html
const EVAL_QUERIES = [
  "What's the outlook for ITC on marketing expenses?",
  "Will GST on co-working spaces change soon?",
  "Risk of RCM expansion to digital platforms?",
  "Is Section 16(4) likely to be liberalised?",
  "Is e-invoicing threshold dropping to ₹1 cr?",
];

// Auto-grounding: does the answer cite a known source ID or regulatory reference?
const GROUNDING_SIGNALS = [
  /cbic_circ/i, /gst_council/i, /aar_/i, /budget_/i, /icai_/i,
  /section \d+/i, /rule \d+/i,
  /\bCBIC\b/, /\bGST Council\b/, /\bAAR\b/,
  /circular/i, /notification/i, /council meeting/i,
];

function autoGroundingCheck(answer) {
  if (!answer) return false;
  return GROUNDING_SIGNALS.some(re => re.test(answer));
}

async function runQuery(query) {
  const start = Date.now();
  try {
    const res = await fetch(QUERY_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    const latencyMs = Date.now() - start;
    const data = await res.json();
    const sources = (data.sources ?? []).map(s => s.source_id);

    return {
      query,
      timestamp: new Date().toISOString(),
      status: res.status,
      latency_ms: latencyMs,
      ok: res.status === 200 && !!data.answer,
      answer: data.answer ?? null,
      answer_preview: data.answer ? data.answer.replace(/\n+/g, ' ').slice(0, 220) : null,
      sources,
      chunks_returned: data.sources?.length ?? 0,
      remaining_queries: data.remaining_queries ?? null,
      auto_grounded: autoGroundingCheck(data.answer),
      // Fill this in after reviewing the full answer in the JSON log:
      manual_verdict: null,   // "grounded" | "plausible" | "off-target"
      manual_notes: null,     // optional free-text annotation
      error: data.error ?? null,
    };
  } catch (e) {
    return {
      query,
      timestamp: new Date().toISOString(),
      status: null,
      latency_ms: Date.now() - start,
      ok: false,
      answer: null,
      answer_preview: null,
      sources: [],
      chunks_returned: 0,
      remaining_queries: null,
      auto_grounded: false,
      manual_verdict: null,
      manual_notes: null,
      error: e.message,
    };
  }
}

async function main() {
  const { writeFileSync } = await import('fs');
  const { join } = await import('path');

  const BAR = '═'.repeat(58);
  console.log(`\n${BAR}`);
  console.log('  GST Foresight — Query Quality Evaluation');
  console.log(`  Endpoint : ${QUERY_URL}`);
  console.log(`  Queries  : ${EVAL_QUERIES.length}`);
  console.log(`${BAR}\n`);

  const results = [];

  for (const [i, query] of EVAL_QUERIES.entries()) {
    process.stdout.write(`  [${i + 1}/${EVAL_QUERIES.length}] ${query.slice(0, 52).padEnd(52)} `);
    const result = await runQuery(query);
    results.push(result);

    if (result.ok) {
      const grounding = result.auto_grounded ? '✓ grounded' : '? review';
      console.log(`${result.status} · ${result.latency_ms}ms · ${result.chunks_returned} chunks · ${grounding}`);
      if (result.sources.length) {
        console.log(`         Sources  : ${result.sources.join(' · ')}`);
      }
      console.log(`         Preview  : ${result.answer_preview}…`);
    } else {
      console.log(`FAIL · ${result.status ?? 'network'} · ${result.error}`);
    }
    console.log();

    // Pace requests — avoid hammering the endpoint
    if (i < EVAL_QUERIES.length - 1) await new Promise(r => setTimeout(r, 900));
  }

  const answered = results.filter(r => r.ok).length;
  const grounded = results.filter(r => r.auto_grounded).length;
  const remainingIp = results.find(r => r.remaining_queries != null)?.remaining_queries ?? 'unknown';

  console.log(BAR);
  console.log(`  Answered     : ${answered}/${results.length}`);
  console.log(`  Auto-grounded: ${grounded}/${answered} (references known source IDs or regulatory terms)`);
  console.log(`  IP remaining : ${remainingIp} free queries left this month`);
  console.log(BAR);

  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const outPath = join(process.cwd(), 'tests', `query_eval_${ts}.json`);
  writeFileSync(outPath, JSON.stringify({
    run_at: new Date().toISOString(),
    endpoint: QUERY_URL,
    summary: {
      total: results.length,
      answered,
      auto_grounded: grounded,
      failed: results.length - answered,
    },
    results,
  }, null, 2));

  console.log(`\n  Log saved → ${outPath}`);
  console.log('  Next: open the log, review each answer, set manual_verdict per result.\n');

  if (answered < results.length) process.exit(1);
}

main().catch(e => { console.error(e); process.exit(1); });
