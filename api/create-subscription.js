export const config = { runtime: 'edge' };

const cleanEnv = v => (v ?? '').replace(/[^\x20-\x7E]/g, '').trim();

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
    console.error('[create-subscription] missing Razorpay env vars', {
      hasKeyId: !!rzpKeyId, hasSecret: !!rzpSecret, hasPlanId: !!planId,
    });
    return r({ error: 'config_error' }, 500);
  }

  // Log partial values to confirm env vars are correct without leaking secrets
  const authToken = btoa(`${rzpKeyId}:${rzpSecret}`);
  console.log('[create-subscription] env check', {
    keyId: rzpKeyId.slice(0, 12) + '…' + rzpKeyId.slice(-4),
    secretLen: rzpSecret.length,
    secretPrefix: rzpSecret.slice(0, 8),
    planId,
    authPrefix: authToken.slice(0, 16),  // first 16 chars of base64 to verify btoa
  });

  const userInfo = await getUserInfo(request, supabaseUrl, supabaseKey);
  if (!userInfo) return r({ error: 'unauthorized', message: 'Sign in to subscribe.' }, 401);

  // Verify plan exists before attempting subscription creation
  let planCheckRes;
  try {
    planCheckRes = await fetch(`https://api.razorpay.com/v1/plans/${planId}`, {
      headers: {
        'Authorization': `Basic ${authToken}`,
        'Accept': 'application/json',
      },
    });
  } catch (e) {
    console.error('[create-subscription] plan check fetch error', e.message);
    return r({ error: 'razorpay_unavailable' }, 502);
  }

  if (!planCheckRes.ok) {
    const planErrText = await planCheckRes.text();
    console.error('[create-subscription] plan not found', planCheckRes.status, planErrText);
    let planErrMsg = 'Plan not found.';
    try { planErrMsg = JSON.parse(planErrText)?.error?.description ?? planErrMsg; } catch {}
    return r({ error: 'plan_not_found', message: planErrMsg, debug: { status: planCheckRes.status, plan_id: planId } }, 502);
  }

  const planData = await planCheckRes.json();
  console.log('[create-subscription] plan verified:', planData.id, planData.interval, planData.item?.amount);

  let subRes;
  try {
    subRes = await fetch('https://api.razorpay.com/v1/subscriptions', {
      method: 'POST',
      headers: {
        'Authorization': `Basic ${authToken}`,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
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
    console.error('[create-subscription] subscription error', subRes.status, errText);
    let rzpMsg = 'Failed to create subscription.';
    try { rzpMsg = JSON.parse(errText)?.error?.description ?? rzpMsg; } catch {}
    return r({ error: 'razorpay_error', message: rzpMsg, debug: { status: subRes.status, plan_id: planId } }, 502);
  }

  const sub = await subRes.json();
  // Return key_id so the frontend always uses the correct mode (test vs live)
  return r({ subscription_id: sub.id, key_id: rzpKeyId });
}
