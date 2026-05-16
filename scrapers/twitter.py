"""
scrapers/twitter.py — Scrapes official Indian GST regulatory accounts on X.

Primary   : twscrape (vladkens/twscrape) — X internal GraphQL API, no paid API needed.
Fallback  : Self-hosted RSSHub RSS feeds parsed via feedparser.
Degrade   : Both fail → return [] with warning. Never blocks the main pipeline.

Target accounts (hardcoded allowlist — government/regulatory only):
  @FinMinIndia, @CBIC_India, @PIBFinance, @nsitharamanoffc, @Anurag_Office

Credentials:
  X_ACCOUNT_JSON  — GitHub Actions secret, JSON array of account dicts:
                    [{"username": "...", "password": "...", "email": "...", "email_password": "..."}]
  RSSHUB_URL      — GitHub Actions secret, self-hosted RSSHub base URL:
                    https://your-rsshub.railway.app

Maintenance:
  twscrape patches every 2-4 weeks when X changes internals. Just run:
    pip install --upgrade twscrape
  RSSHub has a separate maintenance cadence — if twscrape breaks, RSSHub
  likely still works. Check github.com/DIYgod/RSSHub for Twitter route status.
"""

import asyncio
import json as _json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from scrapers.base import BaseScraper, Document

logger = logging.getLogger(__name__)

TARGET_ACCOUNTS = [
    "FinMinIndia",
    "CBIC_India",
    "PIBFinance",
    "nsitharamanoffc",
    "Anurag_Office",
]

MAX_TWEETS_PER_ACCOUNT = 20

# Set via GitHub Actions secrets
_X_ACCOUNT_JSON = os.environ.get("X_ACCOUNT_JSON", "")
_RSSHUB_BASE_URL = os.environ.get("RSSHUB_URL", "").rstrip("/")


class XScraper(BaseScraper):
    """
    Scrapes official Indian GST regulatory accounts on X (Twitter).

    Data flow: XScraper.scrape() → list[Document]
      → BaseScraper.save()         → data/raw/twitter_signal/*.json
      → TopicTagger.tag_and_save() → data/processed/*.json
      → Chunker.chunk_and_save()   → data/chunks/*.json (single chunk per tweet)
      → Embedder.embed_chunks()    → Supabase chunks table
    """

    source_id = "twitter_signal"

    # ── Primary: twscrape ──────────────────────────────────────────────────────

    async def _scrape_via_twscrape(self) -> list[Document]:
        """Fetch X timelines via twscrape's async GraphQL client."""
        try:
            from twscrape import API
        except ImportError:
            logger.warning("[twitter] twscrape not installed — skipping primary path")
            return []

        if not _X_ACCOUNT_JSON:
            logger.warning("[twitter] X_ACCOUNT_JSON not set — skipping twscrape")
            return []

        try:
            accounts = _json.loads(_X_ACCOUNT_JSON)
        except Exception as e:
            logger.warning(f"[twitter] X_ACCOUNT_JSON parse error: {e}")
            return []

        docs = []
        api = API()  # in-memory SQLite by default — no file persistence needed in CI

        for acct in accounts:
            try:
                await api.pool.add_account(
                    username=acct["username"],
                    password=acct["password"],
                    email=acct["email"],
                    email_password=acct.get("email_password", acct.get("password", "")),
                )
            except Exception as e:
                logger.warning(f"[twitter/twscrape] add account {acct.get('username')!r}: {e}")

        try:
            await api.pool.login_all()
        except Exception as e:
            logger.warning(f"[twitter/twscrape] login_all failed: {e}")
            return []

        for username in TARGET_ACCOUNTS:
            try:
                user = await api.user_by_login(username)
                if not user:
                    logger.warning(f"[twitter/twscrape] @{username}: user not found")
                    continue

                count = 0
                async for tweet in api.user_tweets(user.id, limit=MAX_TWEETS_PER_ACCOUNT):
                    doc = self._build_document(
                        tweet_id=str(tweet.id),
                        username=username,
                        content=tweet.rawContent or "",
                        date=tweet.date,
                        like_count=tweet.likeCount or 0,
                        retweet_count=tweet.retweetCount or 0,
                        reply_count=tweet.replyCount or 0,
                        source_method="twscrape",
                    )
                    if doc:
                        docs.append(doc)
                        count += 1

                print(f"[twitter/twscrape] @{username}: {count} tweets", flush=True)

            except Exception as e:
                logger.warning(f"[twitter/twscrape] @{username}: {e}")
                continue

        return docs

    # ── Fallback: RSSHub ───────────────────────────────────────────────────────

    def _scrape_via_rsshub(self) -> list[Document]:
        """
        Fetch X timelines via self-hosted RSSHub RSS feeds.

        RSSHub exposes: GET {RSSHUB_URL}/twitter/user/{username}
        Parsed by feedparser — no httpx, no ALLOWED_DOMAINS check needed
        (RSSHub URL is operator-controlled via secret, not scraped content).

        Railway free-tier note: containers sleep after 15 min idle. First
        request per CI run may take ~10s to wake. feedparser has no default
        timeout — this is acceptable within GitHub Actions' 45-min job limit.
        """
        try:
            import feedparser
        except ImportError:
            logger.warning("[twitter] feedparser not installed — RSSHub fallback unavailable")
            return []

        if not _RSSHUB_BASE_URL:
            logger.warning("[twitter] RSSHUB_URL not set — skipping RSSHub fallback")
            return []

        docs = []

        for username in TARGET_ACCOUNTS:
            feed_url = f"{_RSSHUB_BASE_URL}/twitter/user/{username}"
            try:
                feed = feedparser.parse(feed_url)

                if feed.bozo and not feed.entries:
                    logger.warning(
                        f"[twitter/rsshub] @{username}: parse error: {feed.bozo_exception}"
                    )
                    continue

                count = 0
                for entry in feed.entries[:MAX_TWEETS_PER_ACCOUNT]:
                    content = _strip_html(entry.get("summary") or entry.get("title") or "")
                    if not content:
                        continue

                    tweet_id = _extract_tweet_id(entry.get("link") or "")
                    if not tweet_id:
                        tweet_id = Document.content_hash(username + content)

                    date: Optional[datetime] = None
                    published = entry.get("published_parsed")
                    if published:
                        try:
                            date = datetime(*published[:6], tzinfo=timezone.utc)
                        except Exception:
                            pass

                    doc = self._build_document(
                        tweet_id=tweet_id,
                        username=username,
                        content=content,
                        date=date,
                        like_count=0,       # RSSHub does not expose engagement counts
                        retweet_count=0,
                        reply_count=0,
                        source_method="rsshub",
                    )
                    if doc:
                        docs.append(doc)
                        count += 1

                print(f"[twitter/rsshub] @{username}: {count} tweets", flush=True)

            except Exception as e:
                logger.warning(f"[twitter/rsshub] @{username}: {e}")
                continue

        return docs

    # ── Shared document builder ────────────────────────────────────────────────

    def _build_document(
        self,
        tweet_id: str,
        username: str,
        content: str,
        date: Optional[datetime],
        like_count: int,
        retweet_count: int,
        reply_count: int,
        source_method: str,
    ) -> Optional[Document]:
        """
        Convert raw tweet fields into a Document, or return None if:
          - content is empty / whitespace-only
          - this tweet is already cached on disk (dedup via tweet_id)
        """
        content = (content or "").strip()
        if not content:
            return None

        doc_id = f"twitter_signal_{tweet_id}"
        if self.doc_cached(doc_id):
            return None

        title = f"@{username}: {content[:80]}{'...' if len(content) > 80 else ''}"

        return Document(
            source_id=self.source_id,
            doc_id=doc_id,
            title=title,
            url=f"https://x.com/{username}/status/{tweet_id}",
            date=date,
            content=content,
            metadata={
                "tweet_id": tweet_id,
                "username": username,
                "account_type": "government",   # all TARGET_ACCOUNTS are government
                "like_count": like_count,
                "retweet_count": retweet_count,
                "reply_count": reply_count,
                "source_method": source_method,
                "full_text_extracted": True,    # tweet IS the full content
            },
        )

    # ── Public scrape() entry point ────────────────────────────────────────────

    def scrape(self) -> list[Document]:
        """
        Scrape official Indian GST regulatory X accounts.

        Resilience chain:
          1. twscrape (primary)  — rich metadata including engagement counts
          2. RSSHub RSS (fallback) — less metadata, separate maintenance cadence
          3. [] with warning      — never raises, never blocks sibling scrapers
        """
        # Primary: twscrape (async — bridged via asyncio.run)
        docs: list[Document] = []
        try:
            docs = asyncio.run(self._scrape_via_twscrape())
            if docs:
                print(f"[twitter] twscrape: {len(docs)} tweets collected", flush=True)
        except Exception as e:
            logger.warning(f"[twitter] twscrape failed: {e}")
            docs = []

        # Fallback: RSSHub
        if not docs:
            try:
                docs = self._scrape_via_rsshub()
                if docs:
                    print(f"[twitter] rsshub fallback: {len(docs)} tweets collected", flush=True)
            except Exception as e:
                logger.warning(f"[twitter] RSSHub fallback failed: {e}")
                docs = []

        if not docs:
            print(
                "[twitter] WARNING: no tweets collected (check X_ACCOUNT_JSON / RSSHUB_URL) "
                "— pipeline continues normally",
                flush=True,
            )

        print(f"[twitter] total: {len(docs)} new documents", flush=True)
        return docs


# ── Module-level helpers ───────────────────────────────────────────────────────

def _extract_tweet_id(url: str) -> Optional[str]:
    """Extract numeric tweet ID from https://x.com/user/status/{id} URLs."""
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def _strip_html(text: str) -> str:
    """Remove HTML tags that RSSHub sometimes includes in entry summaries."""
    return re.sub(r"<[^>]+>", " ", text).strip()
