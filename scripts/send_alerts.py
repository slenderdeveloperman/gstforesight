"""
scripts/send_alerts.py — Diff latest vs previous predictions, query alert_subscriptions,
send email via Resend.

Called by .github/workflows/alerts.yml immediately after a predictions update is
pushed to main. Reads data/predictions/previous.json and latest.json from disk.

Required env vars:
  SUPABASE_URL, SUPABASE_SERVICE_KEY — for querying alert_subscriptions
  RESEND_API_KEY                      — Resend REST API key
  ALERT_FROM_EMAIL                    — verified sender address in Resend
"""

import json
import os
import sys
from pathlib import Path

import httpx

PREDICTIONS_DIR = Path(__file__).parent.parent / "data" / "predictions"
LATEST = PREDICTIONS_DIR / "latest.json"
PREVIOUS = PREDICTIONS_DIR / "previous.json"


def load_predictions(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {p["topic_id"]: p for p in data.get("predictions", [])}
    except Exception as e:
        print(f"[alerts] could not load {path.name}: {e}", flush=True)
        return {}


def find_moved_topics(prev: dict, curr: dict, topic_alert_map: dict) -> list[dict]:
    """
    For each active alert subscription, check if the topic's max probability
    shifted by >= threshold_delta between prev and curr.

    topic_alert_map: {topic_id: [{"user_id", "email", "threshold_delta"}, ...]}
    Returns list of {"email", "topic_id", "delta", "prev_prob", "curr_prob", "topic_label", "prediction_id"}
    """
    # Build topic_id → max probability from prediction list
    def max_prob_by_topic(preds: dict) -> dict[str, tuple[float, dict]]:
        out: dict[str, tuple[float, dict]] = {}
        for p in preds.values():
            tid = p.get("topic_id", "")
            prob = p.get("probability", 0)
            if tid not in out or prob > out[tid][0]:
                out[tid] = (prob, p)
        return out

    prev_by_topic = max_prob_by_topic(prev)
    curr_by_topic = max_prob_by_topic(curr)

    alerts_to_send = []
    for topic_id, subscribers in topic_alert_map.items():
        prev_prob, _ = prev_by_topic.get(topic_id, (None, {}))
        curr_prob, curr_pred = curr_by_topic.get(topic_id, (None, {}))
        if prev_prob is None or curr_prob is None:
            continue

        delta = abs(curr_prob - prev_prob)
        for sub in subscribers:
            if delta >= sub["threshold_delta"]:
                alerts_to_send.append({
                    "email": sub["email"],
                    "topic_id": topic_id,
                    "delta": round(curr_prob - prev_prob, 1),
                    "prev_prob": prev_prob,
                    "curr_prob": curr_prob,
                    "topic_label": curr_pred.get("topic_label", topic_id),
                    "prediction_id": curr_pred.get("topic_id", ""),
                    "horizon_label": curr_pred.get("horizon_label", ""),
                    "threshold_delta": sub["threshold_delta"],
                })

    return alerts_to_send


def fetch_active_subscriptions(client: httpx.Client, supabase_url: str, service_key: str) -> dict:
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
    }
    resp = client.get(
        f"{supabase_url}/rest/v1/alert_subscriptions?active=eq.true&select=user_id,topic_id,threshold_delta,email",
        headers=headers,
    )
    if resp.status_code != 200:
        print(f"[alerts] could not fetch subscriptions: {resp.status_code} {resp.text[:200]}", flush=True)
        return {}

    rows = resp.json()
    topic_map: dict[str, list] = {}
    for row in rows:
        tid = row["topic_id"]
        topic_map.setdefault(tid, []).append({
            "user_id": row["user_id"],
            "email": row["email"],
            "threshold_delta": row["threshold_delta"],
        })
    return topic_map


def build_email_html(alert: dict) -> str:
    direction = "▲" if alert["delta"] > 0 else "▼"
    color = "#c85a3a" if alert["delta"] > 0 else "#6a9a5a"
    return f"""
<div style="font-family:'IBM Plex Mono',monospace;max-width:560px;margin:0 auto;background:#0a0a08;padding:32px;border:1px solid #2a2a25">
  <div style="font-size:11px;color:#9a9888;letter-spacing:0.12em;margin-bottom:8px">GST FORESIGHT · ALERT</div>
  <div style="font-size:22px;color:#e8e6de;margin-bottom:4px">{alert['topic_label']}</div>
  <div style="font-size:28px;font-weight:600;color:{color};margin-bottom:24px">
    {direction} {abs(alert['delta']):.0f} pts — {alert['curr_prob']:.0f}% probability
  </div>
  <div style="font-size:12px;color:#9a9888;line-height:1.8;margin-bottom:24px">
    <div><strong style="color:#e8e6de">Prediction</strong> {alert['prediction_id']}</div>
    <div><strong style="color:#e8e6de">Previous</strong>  {alert['prev_prob']:.0f}%</div>
    <div><strong style="color:#e8e6de">Current</strong>   {alert['curr_prob']:.0f}%</div>
    <div><strong style="color:#e8e6de">Horizon</strong>   {alert['horizon_label']}</div>
  </div>
  <a href="https://gstforesight.vercel.app" style="display:inline-block;font-size:10px;letter-spacing:0.12em;color:#0a0a08;background:#c8b86a;padding:10px 18px;text-decoration:none">VIEW ON FORESIGHT →</a>
  <div style="margin-top:32px;font-size:10px;color:#5a5848;line-height:1.6">
    You are receiving this because you set up a GST Foresight alert for <strong>{alert['topic_id']}</strong>
    with a ≥{alert.get('threshold_delta',10)}-point trigger.<br>
    <a href="https://gstforesight.vercel.app" style="color:#5a5848">Manage alerts</a>
  </div>
</div>
"""


def send_email(client: httpx.Client, resend_key: str, from_email: str, alert: dict) -> bool:
    subject = f"GST Foresight Alert: {alert['topic_label']} moved {'+' if alert['delta']>0 else ''}{alert['delta']:.0f} pts"
    resp = client.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
        json={
            "from": from_email,
            "to": [alert["email"]],
            "subject": subject,
            "html": build_email_html(alert),
        },
    )
    if resp.status_code in (200, 201):
        print(f"[alerts] sent → {alert['email']} ({alert['topic_id']} {alert['delta']:+.0f}pt)", flush=True)
        return True
    print(f"[alerts] send failed for {alert['email']}: {resp.status_code} {resp.text[:200]}", flush=True)
    return False


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "")
    service_key  = os.environ.get("SUPABASE_SERVICE_KEY", "")
    resend_key   = os.environ.get("RESEND_API_KEY", "")
    from_email   = os.environ.get("ALERT_FROM_EMAIL", "alerts@gstforesight.in")

    if not supabase_url or not service_key:
        print("[alerts] SUPABASE_URL and SUPABASE_SERVICE_KEY required", flush=True)
        sys.exit(1)
    if not resend_key:
        print("[alerts] RESEND_API_KEY required", flush=True)
        sys.exit(1)

    prev = load_predictions(PREVIOUS)
    curr = load_predictions(LATEST)

    if not prev:
        print("[alerts] no previous snapshot — skipping diff (first run)", flush=True)
        return
    if not curr:
        print("[alerts] latest.json missing or empty — aborting", flush=True)
        sys.exit(1)

    print(f"[alerts] diffing {len(prev)} prev predictions vs {len(curr)} curr", flush=True)

    with httpx.Client(timeout=30) as client:
        topic_map = fetch_active_subscriptions(client, supabase_url, service_key)
        if not topic_map:
            print("[alerts] no active subscriptions — done", flush=True)
            return

        print(f"[alerts] {sum(len(v) for v in topic_map.values())} active subscriptions across {len(topic_map)} topics", flush=True)

        alerts = find_moved_topics(prev, curr, topic_map)
        if not alerts:
            print("[alerts] no thresholds crossed — no emails to send", flush=True)
            return

        print(f"[alerts] {len(alerts)} alert(s) to send", flush=True)
        sent = sum(send_email(client, resend_key, from_email, a) for a in alerts)
        print(f"[alerts] sent {sent}/{len(alerts)}", flush=True)


if __name__ == "__main__":
    main()
