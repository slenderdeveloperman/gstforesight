"""
scripts/backfill_query_topic_tags.py

One-off backfill: tag existing query_history rows that predate the topic_tags column.
Reads rows where topic_tags IS NULL, runs the regex tagger against the query text,
writes back via Supabase REST API using the service_role key.

Run once after applying the Phase 4 schema migration:
  .venv/bin/python scripts/backfill_query_topic_tags.py

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in environment (or .env file).
Does NOT require SUPABASE_ANON_KEY — service_role bypasses RLS.
"""

import os
import sys
import time
import json
import requests
from pathlib import Path

# Load .env if present
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print('[backfill] ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY required.')
    sys.exit(1)

headers = {
    'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
    'apikey': SUPABASE_SERVICE_KEY,
    'Content-Type': 'application/json',
    'Prefer': 'return=minimal',
}

# Regex-based tagger — same patterns as processors/tagger.py and api/query.js.
# Update all three if the taxonomy changes.
import re

TOPIC_KEYWORDS = {
    'itc_eligibility':        [r'input tax credit', r'\bitc\b', r'section 16', r'section 17', r'rule 36', r'rule 37a', r'blocked credit', r'eligib\w+ for credit', r'reversal of credit', r'gstr-2b', r'invoice management'],
    'rcm_coverage':           [r'reverse charge', r'\brcm\b', r'section 9\(3\)', r'section 9\(4\)', r'unregistered'],
    'rate_rationalisation':   [r'rate\w* of tax', r'rate\w* rationaliz', r'rate\w* change', r'gst rate', r'tax rate', r'exempt\w+', r'nil rat', r'5%|12%|18%|28%', r'cess'],
    'return_format':          [r'gstr-1\b', r'gstr-3b', r'gstr-9\b', r'return format', r'annual return', r'filing process', r'qrmp', r'rule 61\b', r'rule 80\b'],
    'ims_itc_flow':           [r'invoice management system', r'\bims\b', r'gstr-2b', r'rule 60b', r'accept.*invoice', r'reject.*invoice', r'deemed accept'],
    'e_invoicing':            [r'e.?invoic\w+', r'electronic invoic\w+', r'\birn\b', r'invoice registration', r'rule 48\b', r'e.?invoice threshold'],
    'classification_disputes':[r'hsn code', r'classif\w+', r'composite supply', r'mixed supply', r'works contract', r'advance ruling', r'\baar\b', r'tariff heading'],
    'valuation':              [r'valuation', r'transaction value', r'related party', r'rule 2[7-9]|rule 3[0-5]', r'open market value'],
    'place_of_supply':        [r'place of supply', r'oidar', r'intermediary', r'cross.border', r'section 12|section 13', r'export of service'],
    'gst_on_crypto_vda':      [r'virtual digital asset', r'\bvda\b', r'crypto\w*', r'\bnft\b', r'digital asset', r'blockchain'],
    'msme_composition':       [r'composition scheme', r'threshold limit', r'aggregate turnover', r'small taxpayer', r'\bmsme\b', r'section 10\b'],
    'real_estate':            [r'real estate', r'construction service', r'affordable housing', r'works contract', r'flat\b|apartment', r'notification 11/2017', r'section 17\(5\)'],
}

def tag_text(text: str) -> str:
    t = text.lower()[:5000]
    matched = []
    for topic_id, patterns in TOPIC_KEYWORDS.items():
        if any(re.search(p, t, re.IGNORECASE) for p in patterns):
            matched.append(topic_id)
    return ','.join(matched)


def fetch_untagged(offset: int, limit: int = 100) -> list[dict]:
    res = requests.get(
        f'{SUPABASE_URL}/rest/v1/query_history',
        headers=headers,
        params={
            'select': 'id,query',
            'topic_tags': 'is.null',
            'order': 'created_at.asc',
            'offset': offset,
            'limit': limit,
        },
    )
    res.raise_for_status()
    return res.json()


def update_row(row_id: str, topic_tags: str):
    res = requests.patch(
        f'{SUPABASE_URL}/rest/v1/query_history',
        headers=headers,
        params={'id': f'eq.{row_id}'},
        json={'topic_tags': topic_tags},
    )
    res.raise_for_status()


def main():
    print('[backfill] starting — fetching untagged query_history rows')
    offset = 0
    total_updated = 0

    while True:
        rows = fetch_untagged(offset)
        if not rows:
            break

        for row in rows:
            tags = tag_text(row.get('query', ''))
            if tags:
                update_row(row['id'], tags)
                total_updated += 1
            time.sleep(0.05)  # 50ms pause — avoid hammering the REST API

        print(f'[backfill] processed {offset + len(rows)} rows, {total_updated} tagged so far')
        offset += len(rows)
        if len(rows) < 100:
            break

    print(f'[backfill] done — {total_updated} rows updated')


if __name__ == '__main__':
    main()
