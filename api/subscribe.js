
export const config = { runtime: 'edge' };

const ALLOWED_ORIGINS = new Set([
  'https://gstforesight.vercel.app',
  'http://localhost:3000',
  'http://localhost:5500',
  'http://127.0.0.1:5500',
]);

function corsHeaders(request) {
  const origin = request.headers.get('origin') ?? '';
  const allowedOrigin = ALLOWED_ORIGINS.has(origin) ? origin : 'https://gstforesight.vercel.app';
  return {
    'Access-Control-Allow-Origin': allowedOrigin,
    'Access-Control-Allow-Methods': 'POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    'Content-Type': 'application/json',
    'Vary': 'Origin',
  };
}

function json(body, status = 200, request = null) {
  return new Response(JSON.stringify(body), {
    status,
    headers: request ? corsHeaders(request) : { 'Content-Type': 'application/json' },
  });
}

const cleanEnv = v => (v ?? '').replace(/[^\x20-\x7E]/g, '');

// Valid topic IDs from the 12-topic prediction taxonomy
const VALID_TOPIC_IDS = new Set([
  'ims_itc_flow', 'rate_rationalisation', 'itc_eligibility', 'classification_disputes',
  'msme_composition', 'place_of_supply', 'real_estate', 'rcm_coverage',
  'valuation', 'return_format', 'e_invoicing', 'gst_on_crypto_vda',
]);

// Returns { id, email } for authenticated requests, null otherwise.
async function getUserInfo(request, supabaseUrl, supabaseKey) {
  const authHeader = request.headers.get('authorization') ?? '';
  if (!authHeader.startsWith('Bearer ')) return null;
  const token = authHeader.slice(7).trim();
  if (!token) return null;
  try {
    const res = await fetch(`${supabaseUrl}/auth/v1/user`, {
      headers: { 'Authorization': `Bearer ${token}`, 'apikey': supabaseKey },
    });
    if (!res.ok) return null;
    const user = await res.json();
    return user?.id ? { id: user.id, email: user.email ?? null } : null;
  } catch {
    return null;
  }
}

export default async function handler(request) {
  const r = (body, status = 200) => json(body, status, request);

  try {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }

    const supabaseUrl = cleanEnv(process.env.SUPABASE_URL);
    const supabaseKey = cleanEnv(process.env.SUPABASE_ANON_KEY);
    const supabaseHeaders = {
      'Authorization': `Bearer ${supabaseKey}`,
      'apikey': supabaseKey,
      'Content-Type': 'application/json',
    };

    // All subscribe endpoints require authentication
    const userInfo = await getUserInfo(request, supabaseUrl, supabaseKey);
    if (!userInfo?.id) {
      return r({ error: 'unauthorized', message: 'Sign in to manage alert subscriptions.' }, 401);
    }
    const { id: userId, email } = userInfo;

    // ── POST /api/subscribe — upsert alert subscription ────────────────────────
    if (request.method === 'POST') {
      let body;
      try {
        body = await request.json();
      } catch {
        return r({ error: 'invalid_json' }, 400);
      }

      const { topic_id, threshold_delta = 10 } = body ?? {};

      if (!topic_id || !VALID_TOPIC_IDS.has(topic_id)) {
        return r({ error: 'invalid_topic', message: `topic_id must be one of: ${[...VALID_TOPIC_IDS].join(', ')}` }, 400);
      }
      if (typeof threshold_delta !== 'number' || threshold_delta < 1 || threshold_delta > 100) {
        return r({ error: 'invalid_threshold', message: 'threshold_delta must be an integer between 1 and 100.' }, 400);
      }

      // Upsert: unique(user_id, topic_id) — second call updates threshold only.
      // Uses the service key via RLS (authenticated role can write own rows).
      const upsertRes = await fetch(`${supabaseUrl}/rest/v1/alert_subscriptions`, {
        method: 'POST',
        headers: { ...supabaseHeaders, 'Authorization': `Bearer ${request.headers.get('authorization')?.slice(7)}`, 'Prefer': 'resolution=merge-duplicates,return=representation' },
        body: JSON.stringify({ user_id: userId, topic_id, threshold_delta, email, active: true }),
      });

      if (!upsertRes.ok) {
        const errText = await upsertRes.text();
        console.error('[subscribe upsert]', upsertRes.status, errText);
        return r({ error: 'db_error', message: 'Failed to save subscription.' }, 502);
      }

      const rows = await upsertRes.json();
      return r({ ok: true, subscription: rows[0] ?? null }, 200);
    }

    // ── DELETE /api/subscribe — deactivate subscription ────────────────────────
    if (request.method === 'DELETE') {
      let body;
      try {
        body = await request.json();
      } catch {
        return r({ error: 'invalid_json' }, 400);
      }

      const { topic_id } = body ?? {};
      if (!topic_id) return r({ error: 'missing_topic_id' }, 400);

      // Soft-delete: set active=false so the alerts workflow ignores it.
      // The unique constraint stays intact; re-subscribing updates threshold back.
      const patchRes = await fetch(
        `${supabaseUrl}/rest/v1/alert_subscriptions?user_id=eq.${userId}&topic_id=eq.${encodeURIComponent(topic_id)}`,
        {
          method: 'PATCH',
          headers: { ...supabaseHeaders, 'Authorization': `Bearer ${request.headers.get('authorization')?.slice(7)}` },
          body: JSON.stringify({ active: false }),
        },
      );

      if (!patchRes.ok) {
        const errText = await patchRes.text();
        console.error('[subscribe delete]', patchRes.status, errText);
        return r({ error: 'db_error', message: 'Failed to deactivate subscription.' }, 502);
      }

      return r({ ok: true });
    }

    return r({ error: 'method_not_allowed' }, 405);

  } catch (e) {
    console.error('[subscribe unhandled]', e.message, e.stack);
    return r({ error: 'internal', message: 'An unexpected error occurred.' }, 500);
  }
}
