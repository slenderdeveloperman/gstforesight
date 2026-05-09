/**
 * api/embed.js — lightweight embedding endpoint (Vercel Edge Function)
 *
 * Accepts { text: string }, returns { embedding: number[] } using
 * Transformers.js running in the V8 edge runtime via WASM.
 * Uses all-MiniLM-L6-v2 — same model as the Python ingest pipeline.
 *
 * Called internally by api/query.js. Not exposed publicly.
 *
 * Env vars: none (model loaded from CDN on first call, cached by Vercel)
 */

import { pipeline } from '@xenova/transformers';

export const config = { runtime: 'edge' };

// Module-level singleton — warm across requests in the same isolate
let extractor = null;

async function getExtractor() {
  if (!extractor) {
    extractor = await pipeline('feature-extraction', 'Xenova/all-MiniLM-L6-v2');
  }
  return extractor;
}

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
  'Content-Type': 'application/json',
};

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'method_not_allowed' }), { status: 405, headers: CORS });
    }

    let text;
    try {
      ({ text } = await request.json());
    } catch {
      return new Response(JSON.stringify({ error: 'invalid_json' }), { status: 400, headers: CORS });
    }
    if (!text || typeof text !== 'string') {
      return new Response(JSON.stringify({ error: 'missing_text' }), { status: 400, headers: CORS });
    }

    try {
      const embed = await getExtractor();
      const output = await embed(text.slice(0, 512), { pooling: 'mean', normalize: true });
      const embedding = Array.from(output.data);
      return new Response(JSON.stringify({ embedding }), { status: 200, headers: CORS });
    } catch (e) {
      console.error('[embed] error:', e);
      return new Response(JSON.stringify({ error: 'embed_failed' }), { status: 502, headers: CORS });
    }
  },
};
