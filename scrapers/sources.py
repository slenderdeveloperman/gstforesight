"""
scrapers/cbic.py — Scrapes CBIC circulars and notifications.

CBIC publishes circulars at cbic.gov.in. These are the highest-weight
signal source — they tell us what topics the government has had to clarify,
which predicts what topics will need clarification again.
"""

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Document


class CBICCircularScraper(BaseScraper):
    """
    Scrapes CBIC GST circulars from cbic-gst.gov.in (static site).

    cbic.gov.in/htdocs-cbic/gst/circulars-cgst-english.htm is an Angular SPA
    that blocks JS bundle access and returns the shell HTML for every route —
    zero DOM content is available server-side. cbic-gst.gov.in is the legacy
    static mirror that still exposes PDF links directly in HTML.

    Strategy:
    1. Homepage — yields the ~6 most-recent circulars with direct PDF hrefs
    2. Communication page — additional taxpayer circulars and trade notices
    Both pages use relative PDF hrefs; we resolve and extract full text.
    """
    source_id = "cbic_circulars"
    BASE_URL = "https://cbic-gst.gov.in/"
    COMM_URL = "https://cbic-gst.gov.in/communication-tax-payers.html"

    def scrape(self) -> list[Document]:
        docs = []
        seen_urls: set[str] = set()

        for page_url in (self.BASE_URL, self.COMM_URL):
            try:
                soup = self.fetch_html(page_url)
            except Exception as e:
                print(f"[cbic_circulars] fetch error {page_url}: {e}")
                continue

            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True)

                # Only follow PDF links with "circular" in path or text
                is_circular = (
                    ".pdf" in href.lower()
                    and ("circular" in href.lower() or "circular" in text.lower())
                )
                if not is_circular:
                    continue

                full_url = href if href.startswith("http") else f"https://cbic-gst.gov.in/{href.lstrip('/')}"
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # Extract circular number from URL pattern: Circular-No-250-2025.pdf
                # or circular-239-33-2024-GST.pdf
                num_match = re.search(r"[Cc]ircular[^/]*?[Nn]o[^/]*?(\d+)", href)
                if not num_match:
                    num_match = re.search(r"[Cc]ircular[^/]*?-(\d{2,})-", href)
                circular_no = num_match.group(1) if num_match else Document.content_hash(href)

                doc_id = f"cbic_circ_{re.sub(r'[^a-z0-9]', '_', href.lower()[-40:])}"

                full_text = self.fetch_pdf_text(full_url)

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=doc_id,
                    title=f"CBIC Circular {circular_no}: {text[:120]}" if text else f"CBIC Circular {circular_no}",
                    url=full_url,
                    date=self._parse_date_from_content(full_text or "", href),
                    content=full_text or text or circular_no,
                    metadata={
                        "circular_no": circular_no,
                        "source_page": page_url,
                        "full_text_extracted": full_text is not None,
                    },
                ))
                print(f"[cbic_circulars] circular {circular_no}: {'full text' if full_text else 'title only'}")

        print(f"[cbic_circulars] total: {len(docs)} circulars")
        return docs

    def _parse_date_from_content(self, content: str, href: str) -> datetime | None:
        """Extract publication date from circular content, falling back to URL year.

        Priority order:
          1. "Dated: 24th June, 2025" pattern in PDF content
          2. Circular number month/year: "Circular No. 250/07/2025" → 2025-07-01
          3. Year from URL (Jan 1 placeholder — least accurate)
        """
        if content:
            # "Dated the 24th June, 2025" / "Dated: 24th June, 2025" etc.
            m = re.search(
                r"[Dd]ated[:\s,]*(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+\n?(\w+)[,\s]+(\d{4})",
                content,
            )
            if m:
                day, month, year = m.group(1), m.group(2), m.group(3)
                for fmt in ("%d %B %Y", "%d %b %Y"):
                    try:
                        return datetime.strptime(f"{day} {month} {year}", fmt)
                    except ValueError:
                        continue

            # Circular number encodes month: "Circular No. 250/07/2025-GST"
            m2 = re.search(r"[Cc]ircular\s+No\.?\s*\d+/(\d{2})/(\d{4})", content)
            if m2:
                try:
                    return datetime(int(m2.group(2)), int(m2.group(1)), 1)
                except ValueError:
                    pass

        # Fallback: year only from URL → Jan 1 placeholder
        year_match = re.search(r"20(\d{2})", href)
        if year_match:
            try:
                return datetime(int("20" + year_match.group(1)), 1, 1)
            except ValueError:
                pass
        return None


class GSTCouncilScraper(BaseScraper):
    """
    Scrapes GST Council meeting minutes from gstcouncil.gov.in.

    The page has a single table: each row is one meeting with columns
    Meeting Name | Date | Venue | Agenda (PDF) | Minutes (PDF).
    Minutes PDFs are linked directly — no sub-page navigation needed.

    Minutes PDFs run 15–30 MB each; we pass max_bytes=50MB to fetch_pdf_text
    so large documents aren't silently dropped by the 10 MB default cap.

    Key signal: deferral language in minutes ("defer", "kept in abeyance",
    "further deliberation") — items deferred at one meeting resurface at the
    next 1–2 meetings with very high probability.
    """
    source_id = "gst_council_minutes"
    BASE_URL = "https://gstcouncil.gov.in/gst-council-meeting"
    DOMAIN   = "https://gstcouncil.gov.in"
    # Council minutes are large official documents — raise the cap to 50 MB
    MAX_PDF_BYTES = 50 * 1024 * 1024

    def scrape(self) -> list[Document]:
        docs = []
        try:
            soup = self.fetch_html(self.BASE_URL)

            # The page has one <table>; each <tr> (except the header) is a meeting
            table = soup.find("table")
            if not table:
                print("[gst_council] ERROR — could not find meetings table on page", flush=True)
                return docs

            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue  # header row or malformed

                meeting_name = cells[0].get_text(strip=True)
                date_text    = cells[1].get_text(strip=True)
                venue        = cells[2].get_text(strip=True)

                # Meeting number from name e.g. "55th GST Council Meeting" → 55
                num_match = re.search(r"(\d+)", meeting_name)
                meeting_no = num_match.group(1) if num_match else Document.content_hash(meeting_name)

                # Column 5 (index 4) is the Minutes PDF link
                minutes_link = cells[4].find("a", href=True)
                if not minutes_link:
                    print(f"[gst_council] meeting {meeting_no}: no minutes link found", flush=True)
                    continue

                minutes_href = minutes_link["href"]
                minutes_url  = (
                    minutes_href if minutes_href.startswith("http")
                    else f"{self.DOMAIN}{minutes_href}"
                )

                doc_id = f"gst_council_{meeting_no}"
                if self.doc_cached(doc_id):
                    print(f"[gst_council] meeting {meeting_no}: cached, skipping", flush=True)
                    continue

                # Parse date e.g. "21-Dec-2024" or range "05-Oct-2020 - 12-Oct-2020"
                # Multi-day meetings use a range — extract only the start date.
                date_single = re.split(r"\s+-\s+", date_text)[0].strip()
                date = None
                for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y"):
                    try:
                        from datetime import datetime
                        date = datetime.strptime(date_single, fmt)
                        break
                    except ValueError:
                        continue

                print(f"[gst_council] meeting {meeting_no} ({date_text}, {venue}): downloading minutes...", flush=True)
                full_text = self.fetch_pdf_text(minutes_url, max_bytes=self.MAX_PDF_BYTES)

                if full_text:
                    print(f"[gst_council] meeting {meeting_no}: {len(full_text):,} chars extracted", flush=True)
                else:
                    print(f"[gst_council] meeting {meeting_no}: text extraction failed (likely scanned PDF)", flush=True)

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=doc_id,
                    title=f"GST Council Meeting {meeting_no} Minutes ({date_text}, {venue})",
                    url=minutes_url,
                    date=date,
                    content=full_text or meeting_name,
                    metadata={
                        "meeting_no": meeting_no,
                        "date_text": date_text,
                        "venue": venue,
                        "full_text_extracted": full_text is not None,
                    },
                ))

        except Exception as e:
            print(f"[gst_council] scrape error: {e}", flush=True)

        print(f"[gst_council] total: {len(docs)} meetings scraped", flush=True)
        return docs


class AARRulingScraper(BaseScraper):
    """
    Scrapes Advance Authority Rulings from CBIC.
    
    Key insight: when the same legal question gets 3+ AARs in 12 months,
    CBIC almost always issues a clarification within 6-9 months after that.
    Topic frequency is the signal, not individual rulings.
    """
    source_id = "aar_rulings"
    # CBIC retired advance-ruling.html. Candidates to try in order.
    CANDIDATE_URLS = [
        "https://cbic-gst.gov.in/advance-rulings-list.html",
        "https://cbic-gst.gov.in/advance-ruling-orders.html",
        "https://cbic-gst.gov.in/advance-ruling.html",
    ]

    def scrape(self) -> list[Document]:
        docs = []
        soup = None
        working_url = None
        for candidate in self.CANDIDATE_URLS:
            try:
                soup = self.fetch_html(candidate)
                if soup.select("table tr, .ruling-item"):
                    working_url = candidate
                    break
            except Exception as e:
                print(f"[aar_rulings] {candidate} — {e}", flush=True)

        if soup is None or working_url is None:
            print(
                "[aar_rulings] WARNING: all candidate URLs failed or returned no rows. "
                "CBIC may have moved the advance rulings listing — update CANDIDATE_URLS.",
                flush=True,
            )
            return []

        try:
            for row in soup.select("table tr, .ruling-item"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                title = cells[0].get_text(strip=True)
                date_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                link = row.find("a", href=True)
                url = link["href"] if link else working_url

                if not title:
                    continue

                doc_id = f"aar_{Document.content_hash(title + date_text)}"
                full_url = url if url.startswith("http") else f"https://cbic-gst.gov.in/{url.lstrip('/')}"

                full_text = None
                if full_url.lower().endswith(".pdf"):
                    full_text = self.fetch_pdf_text(full_url)
                else:
                    pdf_url = self._find_pdf_url(full_url)
                    if pdf_url:
                        full_text = self.fetch_pdf_text(pdf_url)

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=doc_id,
                    title=title,
                    url=full_url,
                    date=self._parse_aar_date(date_text),
                    content=full_text or title,
                    metadata={
                        "raw_date": date_text,
                        "full_text_extracted": full_text is not None,
                    },
                ))
        except Exception as e:
            print(f"[aar_rulings] parse error: {e}", flush=True)

        if not docs:
            print(
                f"[aar_rulings] WARNING: fetched {working_url} but parsed 0 rulings — "
                "page structure may have changed, update selectors.",
                flush=True,
            )
        return docs

    def _parse_aar_date(self, text: str) -> datetime | None:
        """Parse date strings from CBIC AAR listing tables."""
        text = text.strip()
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                    "%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        # Year-only fallback
        m = re.search(r"20\d{2}", text)
        if m:
            try:
                return datetime(int(m.group(0)), 1, 1)
            except ValueError:
                pass
        return None


class BudgetSpeechScraper(BaseScraper):
    """
    Scrapes Union Budget speeches from indiabudget.gov.in.
    
    Budget speech language analysis: certain phrases reliably precede
    GST council action within 1-2 meetings. Model is trained on 2017-2025 corpus.
    """
    source_id = "budget_speeches"
    BASE_URL = "https://indiabudget.gov.in/bspeech.php"

    # Phrases from past budget speeches that preceded GST changes
    # Format: phrase → what actually changed → lag in days
    KNOWN_PREDICTIVE_PHRASES = [
        "rationalise gst rates",
        "simplify gst returns",
        "review gst on",
        "gst council will examine",
        "bring within gst",
        "compliance burden",
        "msme relief",
        "inverted duty structure",
    ]

    def scrape(self) -> list[Document]:
        docs = []
        try:
            soup = self.fetch_html(self.BASE_URL)
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True)

                # Budget speech PDFs usually labelled by year
                year_match = re.search(r"20\d{2}", text + href)
                if not year_match:
                    continue

                year = year_match.group(0)
                doc_id = f"budget_speech_{year}"
                full_url = href if href.startswith("http") else f"https://indiabudget.gov.in/{href.lstrip('/')}"

                # Budget speeches are PDFs — extract full text for signal detection
                full_text = None
                if full_url.lower().endswith(".pdf"):
                    full_text = self.fetch_pdf_text(full_url)
                else:
                    pdf_url = self._find_pdf_url(full_url)
                    if pdf_url:
                        full_text = self.fetch_pdf_text(pdf_url)

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=doc_id,
                    title=f"Union Budget Speech {year}",
                    url=full_url,
                    date=datetime(int(year), 2, 1),
                    content=full_text or text,
                    metadata={
                        "year": year,
                        "predictive_phrases": self.KNOWN_PREDICTIVE_PHRASES,
                        "full_text_extracted": full_text is not None,
                    },
                ))
        except Exception as e:
            print(f"[budget_speeches] scrape error: {e}")
        return docs


class IndianKanoonScraper(BaseScraper):
    """
    Scrapes GST-related High Court and Supreme Court judgments from Indian Kanoon.

    Signal: Litigation clustering predicts CBIC clarifications.
    When courts across jurisdictions rule inconsistently on the same GST issue,
    CBIC almost always issues a clarificatory circular within 6-9 months to
    settle the split. Topic frequency across judgments is the leading indicator.
    """
    source_id = "court_judgments"
    BASE_URL = "https://indiankanoon.org"
    MAX_PAGES = 5  # ~100 judgments per run — enough to detect clustering

    # Separate queries capture different litigation clusters
    SEARCH_QUERIES = [
        "GST input tax credit doctypes:judgments",
        "GST rate classification HSN doctypes:judgments",
        "GST refund zero rated supply doctypes:judgments",
        "CGST SGST IGST writ petition doctypes:judgments",
    ]

    def scrape(self) -> list[Document]:
        docs = []
        seen_ids = set()
        for query in self.SEARCH_QUERIES:
            for page in range(self.MAX_PAGES):
                try:
                    encoded = query.replace(" ", "+").replace(":", "%3A")
                    url = f"{self.BASE_URL}/search/?formInput={encoded}+sortby%3Amostrecent&pagenum={page}"
                    soup = self.fetch_html(url)

                    results = soup.select(".result")
                    if not results:
                        break

                    for result in results:
                        # Title is in .result_title a (href = /docfragment/{id}/)
                        # Canonical URL is in the "Full Document" cite_tag (href = /doc/{id}/)
                        title_tag = result.select_one(".result_title a, h4 a")
                        if not title_tag:
                            continue

                        title = title_tag.get_text(strip=True)

                        # Prefer the /doc/ href; fall back to extracting id from /docfragment/
                        full_doc_link = result.select_one("a[href^='/doc/']")
                        if full_doc_link:
                            href = full_doc_link.get("href", "")
                        else:
                            frag = title_tag.get("href", "")
                            import re as _re
                            m = _re.search(r"/docfragment/(\d+)/", frag)
                            href = f"/doc/{m.group(1)}/" if m else ""

                        if not href:
                            continue

                        doc_id = f"judgment_{Document.content_hash(href)}"
                        if doc_id in seen_ids:
                            continue
                        seen_ids.add(doc_id)

                        doc_url = f"{self.BASE_URL}{href}"

                        # Court lives in .docsource (not .docsource_main)
                        meta_tag = result.select_one(".docsource")
                        court = meta_tag.get_text(strip=True) if meta_tag else ""

                        # Date is embedded in the title "Case Name on DD Mon, YYYY"
                        import re as _re2
                        date_text = ""
                        dm = _re2.search(r" on (\d{1,2} \w+,? \d{4})$", title)
                        if dm:
                            date_text = dm.group(1).replace(",", "").strip()

                        # Snippet for content
                        snippet_tag = result.select_one(".headline, .snippet_part")
                        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

                        date = self._parse_ik_date(date_text)

                        docs.append(Document(
                            source_id=self.source_id,
                            doc_id=doc_id,
                            title=title,
                            url=doc_url,
                            date=date,
                            content=snippet or title,
                            metadata={
                                "court": court,
                                "raw_date": date_text,
                                "search_query": query,
                                "full_text_extracted": False,
                            },
                        ))

                except Exception as e:
                    print(f"[court_judgments] page {page} query '{query[:30]}': {e}")
                    break  # move to next query on fetch error

        print(f"[court_judgments] collected {len(docs)} unique judgments")
        return docs

    def _parse_ik_date(self, text: str) -> datetime | None:
        text = text.strip()
        for fmt in ("%d %B, %Y", "%d %b, %Y", "%B %d, %Y", "%b %d, %Y",
                    "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None


class ICAIRepresentationScraper(BaseScraper):
    """
    Scrapes ICAI IDTC pre-budget memoranda and GST Council representations.

    Signal: ICAI represents 400k+ CAs. Their formal representations to the
    Finance Ministry and GST Council have a ~30% conversion rate to policy
    within 2 years. This corpus captures what the profession will lobby for
    months before the Budget or a Council meeting — the earliest possible
    demand signal before it reaches the legislative agenda.
    """
    source_id = "icai_representations"
    SOURCES = [
        ("https://idtc.icai.org/budget-memorandum.html", "budget_memo"),
        ("https://idtc.icai.org/representation.html", "representation"),
        ("https://idtc.icai.org/", "idtc_home"),
    ]
    # Only follow homepage links that look like signal docs.
    # Filters out certificate courses, batch registrations, event pages, etc.
    SIGNAL_KEYWORDS = {
        "budget", "memorandum", "representation", "suggestion", "circular",
        "gst", "indirect tax", "idtc", "submission", "pre-budget",
    }

    def scrape(self) -> list[Document]:
        docs = []
        seen_ids = set()
        seen_urls: set[str] = set()  # dedup across source pages — same PDF listed on multiple pages

        for page_url, doc_type in self.SOURCES:
            print(f"[icai] fetching source page: {page_url}", flush=True)
            try:
                soup = self.fetch_html(page_url)
            except Exception as e:
                print(f"[icai] {page_url}: {e}", flush=True)
                continue

            links = soup.find_all("a", href=True)
            print(f"[icai] {page_url}: {len(links)} links found", flush=True)

            for link in links:
                href = link["href"]
                text = link.get_text(strip=True)

                if not text or len(text) < 12:
                    continue

                is_pdf = href.lower().endswith(".pdf")
                is_internal = "icai.org" in href or href.startswith("/")
                if not (is_pdf or is_internal):
                    continue

                full_url = href if href.startswith("http") else urljoin(page_url, href)

                try:
                    self._validate_url(full_url)
                except ValueError as e:
                    print(f"[icai] blocked: {full_url[:80]} — {e}", flush=True)
                    continue

                # Dedup on normalised URL across all source pages — same PDF
                # can appear on both budget-memorandum.html and representation.html
                if full_url in seen_urls:
                    print(f"[icai] duplicate URL, skipping: {full_url[:80]}", flush=True)
                    continue
                seen_urls.add(full_url)

                doc_id = f"icai_{doc_type}_{Document.content_hash(href)}"
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                if self.doc_cached(doc_id):
                    print(f"[icai] cached, skipping: {text[:50]}", flush=True)
                    continue

                year_match = re.search(r"20\d{2}", text + href)
                year = year_match.group(0) if year_match else "unknown"

                full_text = None
                if is_pdf:
                    print(f"[icai] downloading PDF: {full_url[:90]}", flush=True)
                    full_text = self.fetch_pdf_text(full_url)
                else:
                    # On the homepage, skip non-PDF links that don't look like
                    # signal docs (filters out course pages, event registrations, etc.)
                    if doc_type == "idtc_home":
                        combined = (text + " " + href).lower()
                        if not any(kw in combined for kw in self.SIGNAL_KEYWORDS):
                            continue
                    print(f"[icai] probing HTML for PDF: {full_url[:90]}", flush=True)
                    pdf_url = self._find_pdf_url(full_url)
                    if pdf_url:
                        print(f"[icai]   → found PDF: {pdf_url[:90]}", flush=True)
                        full_text = self.fetch_pdf_text(pdf_url)
                    else:
                        print(f"[icai]   → no PDF found, skipping", flush=True)
                        continue

                chars = len(full_text) if full_text else 0
                print(f"[icai] {'OK' if full_text else 'no text'} — {chars:,} chars — {text[:50]}", flush=True)

                date = datetime(int(year), 1, 1) if year != "unknown" else None

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=doc_id,
                    title=text,
                    url=full_url,
                    date=date,
                    content=full_text or text,
                    metadata={
                        "doc_type": doc_type,
                        "year": year,
                        "source_page": page_url,
                        "full_text_extracted": full_text is not None,
                    },
                ))

        print(f"[icai_representations] collected {len(docs)} documents", flush=True)
        return docs


class PIBFinanceScraper(BaseScraper):
    """
    Scrapes Finance Ministry press releases from PIB (Press Information Bureau).

    Signal: PIB releases carry government forward-signalling language — phrases
    like 'under consideration', 'committee constituted', 'matter being examined'
    consistently precede formal CBIC circulars by 30-90 days.

    PIB's listing pages are JavaScript-rendered (ASP.NET UpdatePanel). Static
    HTML fetches only return navigation shell. Strategy: use RSS seeds for
    latest PRID anchor, then enumerate backwards with step-50 sampling over the
    last 90 days. Finance GST releases appear roughly 1-2 per 250 PRIDs.
    """
    source_id = "pib_finance"

    RSS_URL = "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=3&Regid=3"
    PR_BASE = "https://pib.gov.in/PressReleasePage.aspx?PRID={prid}"

    # Known Finance/GST PRIDs — add more as you find them manually.
    # These seed the enumerator so we always cover key council meeting dates.
    SEED_PRIDS: list[int] = [
        2086770,   # Dec 21, 2024 — Finance Ministry (NPS; same-day GST council)
        2093698,   # Jan 17, 2025 — finance-adjacent (budget/economy)
    ]

    # How many PRIDs to enumerate backwards from current anchor.
    # 90 days × ~250 PRIDs/day = ~22500 PRIDs of range.
    # With PRID_STEP=25: 22500/25 = 900 candidates → exactly fills MAX_REQUESTS.
    LOOKBACK_PRIDS = 900    # steps to walk back (× PRID_STEP = PRID range covered)

    # TRADEOFF — step size vs. coverage:
    #   step=5  → dense sampling, hits ~every Finance release, but MAX_REQUESTS=900
    #             only covers 900×5=4500 PRIDs ≈ 18 days. Good for recency; bad for
    #             historical signal breadth needed by the evaluator.
    #   step=25 → coarse sampling, hits ~1-in-3 Finance releases per day, but
    #             900×25=22500 PRIDs ≈ 90 days. Enough temporal reach for the
    #             government_forward_signal evaluator without increasing runtime.
    #   step=5 + MAX_REQUESTS=4500 → full 90-day dense coverage, but adds ~60 min
    #             to CI runtime. Use this for one-off historical backfills.
    PRID_STEP = 25  # coarse/wide — default for daily CI runs

    # Max requests per run — prevents runaway scraping
    MAX_REQUESTS = 900

    RELEVANT_TERMS = {
        "gst", "goods and services tax", "cbic", "indirect tax",
        "input tax credit", "itc", "gst council", "finance minister",
        "tax rate", "exemption", "e-invoice", "e-way bill", "refund",
        "goods and service", "department of revenue",
    }

    MINISTRY_TERMS = {
        "ministry of finance", "department of revenue", "cbic",
        "central board of indirect", "department of financial services",
    }

    def scrape(self) -> list[Document]:
        docs = []
        seen_prids: set[int] = set()

        # ── Step 1: Get anchor PRID from RSS ────────────────────────────────
        print("[pib_finance] fetching RSS to get latest PRID anchor...", flush=True)
        latest_prid = self._get_latest_prid_from_rss()
        if latest_prid:
            print(f"[pib_finance] RSS anchor PRID: {latest_prid}", flush=True)
        else:
            # Fallback: use a known recent PRID
            latest_prid = max(self.SEED_PRIDS) + 170_000  # rough 2-year offset
            print(f"[pib_finance] RSS failed — using fallback anchor PRID: {latest_prid}", flush=True)

        # ── Step 2: Build PRID candidate list ───────────────────────────────
        # (a) Seed PRIDs first (always check these)
        candidates = list(self.SEED_PRIDS)

        # (b) Enumerate backwards from anchor with step
        start = latest_prid
        end = latest_prid - self.LOOKBACK_PRIDS * self.PRID_STEP
        step_candidates = list(range(start, max(end, 0), -self.PRID_STEP))
        candidates.extend(step_candidates)

        # Deduplicate
        candidates = sorted(set(candidates), reverse=True)[:self.MAX_REQUESTS]
        print(f"[pib_finance] will check {len(candidates)} PRIDs "
              f"(from {candidates[0]} to {candidates[-1]})", flush=True)

        # ── Step 3: Fetch and filter each candidate ─────────────────────────
        checked = 0
        for prid in candidates:
            if prid in seen_prids:
                continue
            seen_prids.add(prid)

            doc_id = f"pib_{prid}"
            if self.doc_cached(doc_id):
                print(f"[pib_finance] PRID {prid}: cached, skip", flush=True)
                checked += 1
                continue

            url = self.PR_BASE.format(prid=prid)
            try:
                self._validate_url(url)
            except ValueError:
                continue

            checked += 1
            if checked % 100 == 0:
                print(f"[pib_finance] progress: {checked}/{len(candidates)} checked, "
                      f"{len(docs)} collected so far", flush=True)

            text, date, title = self._fetch_pr(url)
            if not text:
                continue

            # Must be English Finance/GST content
            if not self._is_finance(text):
                continue
            if not self._is_relevant(text):
                continue

            print(f"[pib_finance] ✓ PRID {prid} ({date}): {title[:70]}", flush=True)
            docs.append(Document(
                source_id=self.source_id,
                doc_id=doc_id,
                title=title or f"PIB Finance Release {prid}",
                url=url,
                date=date,
                content=text,
                metadata={"prid": prid, "full_text_extracted": True},
            ))

        print(f"[pib_finance] done — checked {checked} PRIDs, "
              f"collected {len(docs)} Finance/GST releases", flush=True)
        return docs

    def _get_latest_prid_from_rss(self) -> Optional[int]:
        """Extract the highest PRID from the PIB RSS feed."""
        try:
            from xml.etree import ElementTree as ET
            r = self.client.get(self.RSS_URL, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            prids = []
            for item in root.findall(".//item"):
                link_el = item.find("link")
                if link_el is not None and link_el.text:
                    m = re.search(r"PRID=(\d+)", link_el.text)
                    if m:
                        prids.append(int(m.group(1)))
            return max(prids) if prids else None
        except Exception as e:
            print(f"[pib_finance] RSS fetch failed: {e}", flush=True)
            return None

    def _fetch_pr(self, url: str) -> tuple[str, Optional[datetime], str]:
        """Fetch a press release and return (content, date, title)."""
        try:
            r = self.client.get(url, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            el = soup.select_one(".innner-page-main-about-us-content-right-part, [class*='content']")
            if not el:
                return "", None, ""
            text = el.get_text(strip=True)
            if len(text) < 100:
                return "", None, ""

            # Extract date from "Posted On: DD MON YYYY" embedded in content
            date = None
            dm = re.search(r"Posted On:\s*(\d{1,2} \w+ \d{4})", text)
            if dm:
                date = self._parse_pib_date(dm.group(1))

            # Extract title: first line before "Posted On"
            title = text.split("Posted On")[0].strip()[:200] if "Posted On" in text else text[:100]
            return text, date, title
        except Exception:
            return "", None, ""

    def _is_finance(self, text: str) -> bool:
        """True if the release is from Finance Ministry / CBIC (English)."""
        lower = text.lower()
        # Must have Finance ministry markers AND be in English (ASCII-heavy)
        ascii_ratio = sum(1 for c in text[:100] if ord(c) < 128) / max(len(text[:100]), 1)
        return ascii_ratio > 0.7 and any(term in lower for term in self.MINISTRY_TERMS)

    def _is_relevant(self, text: str) -> bool:
        lower = text.lower()
        return any(term in lower for term in self.RELEVANT_TERMS)

    def _parse_pib_date(self, text: str) -> Optional[datetime]:
        text = text.strip()
        for fmt in ("%d %B %Y", "%d %b %Y", "%d %B, %Y", "%B %d, %Y",
                    "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
        if m:
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    return datetime.strptime(m.group(0), fmt)
                except ValueError:
                    pass
        return None


class ParliamentaryQuestionsScraper(BaseScraper):
    """
    Scrapes GST-related starred and unstarred questions from Lok Sabha and Rajya Sabha.

    Signal: Parliamentary questions are filed by MPs on behalf of constituents
    and industry bodies. When 10+ MPs from different parties file questions on
    the same GST topic in a single session, it reflects broad stakeholder
    pressure — a reliable 6-12 month leading indicator of policy review.
    Questions also reveal which sectors are experiencing compliance pain before
    that pain surfaces as formal industry representations or court filings.
    """
    source_id = "parliamentary_questions"

    # Lok Sabha questions search — 18th Lok Sabha (2024-present)
    LS_SEARCH_URL = "https://loksabha.nic.in/Questions/Qtextsearch.aspx"
    # Rajya Sabha questions are listed per session
    RS_BASE_URL = "https://rajyasabha.nic.in/rsnew/questions/questions_home.aspx"
    # Sansad portal — newer unified access
    SANSAD_URL = "https://sansad.in/ls/questions"

    GST_KEYWORDS = ["GST", "Goods and Services Tax", "CGST", "IGST", "input tax credit"]

    def scrape(self) -> list[Document]:
        docs = []
        # Try Lok Sabha question search first, then Rajya Sabha listing
        for fetch_fn in [self._scrape_loksabha, self._scrape_rajyasabha]:
            try:
                results = fetch_fn()
                docs.extend(results)
            except Exception as e:
                print(f"[parliamentary_questions] source error: {e}")
        print(f"[parliamentary_questions] collected {len(docs)} questions")
        return docs

    def _scrape_loksabha(self) -> list[Document]:
        docs = []
        for keyword in self.GST_KEYWORDS[:2]:  # Top 2 keywords to avoid hammering
            try:
                # Lok Sabha text search — GET with qtitle param
                url = f"{self.LS_SEARCH_URL}?qtitle={keyword.replace(' ', '+')}&qtype=All"
                soup = self.fetch_html(url)

                # Results are typically in a table
                for row in soup.select("table tr, .question-item"):
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue

                    q_no = cells[0].get_text(strip=True)
                    title = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                    date_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    member = cells[3].get_text(strip=True) if len(cells) > 3 else ""

                    if not title or len(title) < 8:
                        continue

                    link = row.find("a", href=True)
                    href = link["href"] if link else ""
                    full_url = (
                        href if href.startswith("http")
                        else urljoin(self.LS_SEARCH_URL, href)
                        if href else self.LS_SEARCH_URL
                    )

                    doc_id = f"ls_q_{Document.content_hash(q_no + title)}"
                    date = self._parse_date(date_text)

                    # Fetch full question text if available
                    full_text = None
                    if href and href != self.LS_SEARCH_URL:
                        try:
                            self._validate_url(full_url)
                            q_soup = self.fetch_html(full_url)
                            body = q_soup.select_one(".question-body, .qtextcontent, #qtext")
                            if body:
                                full_text = body.get_text(separator="\n", strip=True)
                        except Exception:
                            pass

                    docs.append(Document(
                        source_id=self.source_id,
                        doc_id=doc_id,
                        title=f"LS Q{q_no}: {title}",
                        url=full_url,
                        date=date,
                        content=full_text or title,
                        metadata={
                            "house": "Lok Sabha",
                            "question_no": q_no,
                            "member": member,
                            "keyword": keyword,
                            "full_text_extracted": full_text is not None,
                        },
                    ))

            except Exception as e:
                print(f"[parliamentary_questions] Lok Sabha '{keyword}': {e}")

        return docs

    def _scrape_rajyasabha(self) -> list[Document]:
        docs = []
        try:
            soup = self.fetch_html(self.RS_BASE_URL)
            # Rajya Sabha questions page links to session-wise question lists
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True)

                if not any(k.lower() in text.lower() for k in ["starred", "unstarred", "question"]):
                    continue

                full_url = href if href.startswith("http") else urljoin(self.RS_BASE_URL, href)
                try:
                    self._validate_url(full_url)
                except ValueError:
                    continue

                try:
                    q_soup = self.fetch_html(full_url)
                    for row in q_soup.select("table tr"):
                        cells = row.find_all("td")
                        if len(cells) < 2:
                            continue

                        q_no = cells[0].get_text(strip=True)
                        title = cells[1].get_text(strip=True) if len(cells) > 1 else ""

                        if not title or not any(
                            k.lower() in title.lower() for k in self.GST_KEYWORDS
                        ):
                            continue

                        doc_id = f"rs_q_{Document.content_hash(q_no + title + href)}"
                        docs.append(Document(
                            source_id=self.source_id,
                            doc_id=doc_id,
                            title=f"RS Q{q_no}: {title}",
                            url=full_url,
                            date=None,
                            content=title,
                            metadata={
                                "house": "Rajya Sabha",
                                "question_no": q_no,
                                "source_page": text,
                                "full_text_extracted": False,
                            },
                        ))
                except Exception:
                    continue

        except Exception as e:
            print(f"[parliamentary_questions] Rajya Sabha: {e}")

        return docs

    def _parse_date(self, text: str) -> datetime | None:
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(text.strip(), fmt)
            except ValueError:
                continue
        return None
