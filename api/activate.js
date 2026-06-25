
export const config = { runtime: 'edge' };

// Razorpay webhook handler — activates Pro subscriptions after confirmed payment.
// This endpoint is NOT called by the browser; it's called by Razorpay's servers.
// Razorpay signs each webhook body with HMAC-SHA256 using the webhook secret,
// so we verify the signature before trusting any payload data.

const cleanEnv = v => (v ?? '').replace(/[^\x20-\x7E]/g, '');

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Razorpay sends X-Razorpay-Signature: hex(HMAC-SHA256(body, webhook_secret))
async function verifyRazorpaySignature(body, signature, secret) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const mac = await crypto.subtle.sign('HMAC', key, encoder.encode(body));
  const hex = Array.from(new Uint8Array(mac)).map(b => b.toString(16).padStart(2, '0')).join('');
  return hex === signature;
}

// Maps Razorpay plan IDs → DB plan names. Add new plans here.
const PLAN_MAP = {
  'plan_T4z866Cblz5TIl': { plan: 'pro_individual', days: 30 },
};

export default async function handler(request) {
  if (request.method !== 'POST') {
    return json({ error: 'method_not_allowed' }, 405);
  }

  const supabaseUrl   = cleanEnv(process.env.SUPABASE_URL);
  const serviceKey    = cleanEnv(process.env.SUPABASE_SERVICE_KEY);
  const webhookSecret = cleanEnv(process.env.RAZORPAY_WEBHOOK_SECRET);

  if (!supabaseUrl || !serviceKey || !webhookSecret) {
    console.error('[activate] missing env vars');
    return json({ error: 'config_error' }, 500);
  }

  const rawBody  = await request.text();
  const signature = request.headers.get('x-razorpay-signature') ?? '';

  const valid = await verifyRazorpaySignature(rawBody, signature, webhookSecret);
  if (!valid) {
    console.error('[activate] signature mismatch');
    return json({ error: 'invalid_signature' }, 401);
  }

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return json({ error: 'invalid_json' }, 400);
  }

  const event = payload?.event;

  // Extract userId, paymentId, planId from either subscription or one-time payment events.
  let userId, paymentId, planId;

  // Payment link flow — look up user by email since notes.user_id is unavailable.
  if (event === 'payment_link.paid') {
    const payment = payload?.payload?.payment?.entity;
    const email   = payment?.email;
    paymentId     = payment?.id;
    if (!email) {
      console.error('[activate] payment_link.paid missing email', payload);
      return json({ error: 'missing_email' }, 400);
    }
    const lookupRes = await fetch(
      `${cleanEnv(process.env.SUPABASE_URL)}/auth/v1/admin/users?email=${encodeURIComponent(email)}`,
      { headers: { 'Authorization': `Bearer ${cleanEnv(process.env.SUPABASE_SERVICE_KEY)}`, 'apikey': cleanEnv(process.env.SUPABASE_SERVICE_KEY) } },
    );
    if (!lookupRes.ok) {
      console.error('[activate] user lookup failed', await lookupRes.text());
      return json({ error: 'user_lookup_failed' }, 502);
    }
    const { users } = await lookupRes.json();
    if (!users?.length) {
      console.error('[activate] no user found for email', email);
      return json({ error: 'user_not_found' }, 404);
    }
    userId = users[0].id;
    planId = 'plan_T4z866Cblz5TIl'; // payment link maps to pro_individual
  } else if (event === 'subscription.activated' || event === 'subscription.charged') {
    const sub     = payload?.payload?.subscription?.entity;
    const payment = payload?.payload?.payment?.entity;
    userId    = sub?.notes?.user_id;
    paymentId = payment?.id;
    planId    = sub?.plan_id;
  } else if (event === 'payment.captured') {
    const payment = payload?.payload?.payment?.entity;
    const notes   = payment?.notes ?? {};
    userId    = notes.user_id;
    paymentId = payment?.id;
    planId    = notes.plan_id;
  } else {
    return json({ ok: true, skipped: event });
  }

  if (!userId || !planId) {
    console.error('[activate] missing user_id or plan_id', { userId, planId, event });
    return json({ error: 'missing_notes' }, 400);
  }

  const planConfig = PLAN_MAP[planId];
  if (!planConfig) {
    console.error('[activate] unknown plan_id', planId);
    return json({ error: 'unknown_plan' }, 400);
  }

  const validUntil = new Date(Date.now() + planConfig.days * 86_400_000).toISOString();

  // razorpay_payment_id is UNIQUE — Razorpay retries webhooks, so duplicate inserts
  // for the same payment are silently ignored via ON CONFLICT.
  const supabaseHeaders = {
    'Authorization': `Bearer ${serviceKey}`,
    'apikey': serviceKey,
    'Content-Type': 'application/json',
    'Prefer': 'resolution=ignore-duplicates',
  };

  const insertRes = await fetch(`${supabaseUrl}/rest/v1/subscriptions`, {
    method: 'POST',
    headers: supabaseHeaders,
    body: JSON.stringify({
      user_id: userId,
      plan: planConfig.plan,
      valid_until: validUntil,
      razorpay_payment_id: paymentId,
    }),
  });

  if (!insertRes.ok) {
    const errText = await insertRes.text();
    console.error('[activate] subscription insert failed', insertRes.status, errText);
    return json({ error: 'db_error' }, 502);
  }

  console.log(`[activate] Pro activated: user=${userId} plan=${planConfig.plan} until=${validUntil} event=${event}`);
  return json({ ok: true, plan: planConfig.plan, valid_until: validUntil });
}
