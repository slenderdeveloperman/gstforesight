export const config = { runtime: 'edge' };

const cleanEnv = v => (v ?? '').replace(/[^\x20-\x7E]/g, '');

const ALLOWED_ORIGINS = new Set([
  'https://gstforesight.vercel.app',
  'http://localhost:3000',
  'http://localhost:5500',
  'http://127.0.0.1:5500',
]);

function corsHeaders(request) {
  const origin = request.headers.get('origin') ?? '';
  const allowed = ALLOWED_ORIGINS.has(origin) ? origin : 'https://gstforesight.vercel.app';
  return {
    'Access-Control-Allow-Origin': allowed,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
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
    return user?.id ? { id: user.id, email: user.email ?? '' } : null;
  } catch {
    return null;
  }
}

export default async function handler(request) {
  const r = (body, status = 200) => json(body, status, request);

  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders(request) });
  }
  if (request.method !== 'POST') return r({ error: 'method_not_allowed' }, 405);

  const supabaseUrl = cleanEnv(process.env.SUPABASE_URL);
  const supabaseKey = cleanEnv(process.env.SUPABASE_ANON_KEY);
  const rzpKeyId    = cleanEnv(process.env.RAZORPAY_KEY_ID);
  const rzpSecret   = cleanEnv(process.env.RAZORPAY_KEY_SECRET);
  const planId      = cleanEnv(process.env.RAZORPAY_PLAN_ID);

  if (!rzpKeyId || !rzpSecret || !planId) {
    console.error('[create-subscription] missing Razorpay env vars');
    return r({ error: 'config_error' }, 500);
  }

  const userInfo = await getUserInfo(request, supabaseUrl, supabaseKey);
  if (!userInfo) return r({ error: 'unauthorized', message: 'Sign in to subscribe.' }, 401);

  let subRes;
  try {
    subRes = await fetch('https://api.razorpay.com/v1/subscriptions', {
      method: 'POST',
      headers: {
        'Authorization': `Basic ${btoa(`${rzpKeyId}:${rzpSecret}`)}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        plan_id: planId,
        total_count: 12,
        quantity: 1,
        customer_notify: 1,
        notes: { user_id: userInfo.id, email: userInfo.email },
      }),
    });
  } catch (e) {
    console.error('[create-subscription] Razorpay fetch error', e.message);
    return r({ error: 'razorpay_unavailable' }, 502);
  }

  if (!subRes.ok) {
    const errText = await subRes.text();
    console.error('[create-subscription] Razorpay error', subRes.status, errText);
    return r({ error: 'razorpay_error', message: 'Failed to create subscription.' }, 502);
  }

  const sub = await subRes.json();
  return r({ subscription_id: sub.id });
}
