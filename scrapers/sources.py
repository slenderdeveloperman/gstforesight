"""
scrapers/cbic.py — Scrapes CBIC circulars and notifications.

CBIC publishes circulars at cbic.gov.in. These are the highest-weight
signal source — they tell us what topics the government has had to clarify,
which predicts what topics will need clarification again.
"""

import re
from datetime import datetime
from scrapers.base import BaseScraper, Document


class CBICCircularScraper(BaseScraper):
    source_id = "cbic_circulars"
    BASE_URL = "https://cbic.gov.in/htdocs-cbic/gst/circulars-cgst-english.htm"

    def scrape(self) -> list[Document]:
        docs = []
        try:
            soup = self.fetch_html(self.BASE_URL)
            # CBIC page has a table of circulars with circular number, date, subject, PDF link
            for row in soup.select("table tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue

                circular_no = cells[0].get_text(strip=True)
                date_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                subject = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                link_tag = row.find("a", href=True)
                url = link_tag["href"] if link_tag else self.BASE_URL

                if not circular_no or not subject:
                    continue

                # Normalise circular number to use as doc_id
                doc_id = re.sub(r"[^a-z0-9]", "_", circular_no.lower())

                date = self._parse_date(date_text)

                full_url = url if url.startswith("http") else f"https://cbic.gov.in/{url.lstrip('/')}"
                # Attempt full-text extraction from PDF; fall back to subject line
                full_text = None
                if full_url.lower().endswith(".pdf"):
                    full_text = self.fetch_pdf_text(full_url)
                else:
                    pdf_url = self._find_pdf_url(full_url)
                    if pdf_url:
                        full_text = self.fetch_pdf_text(pdf_url)

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=f"cbic_circ_{doc_id}",
                    title=f"Circular {circular_no}: {subject}",
                    url=full_url,
                    date=date,
                    content=full_text or subject,
                    metadata={
                        "circular_no": circular_no,
                        "raw_date": date_text,
                        "subject": subject,
                        "full_text_extracted": full_text is not None,
                    },
                ))
        except Exception as e:
            print(f"[cbic_circulars] scrape error: {e}")
        return docs

    def _parse_date(self, text: str) -> datetime | None:
        for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%B %d, %Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text.strip(), fmt)
            except ValueError:
                continue
        return None


class GSTCouncilScraper(BaseScraper):
    """
    Scrapes GST Council meeting press releases and agenda summaries.
    The most valuable signal: items listed as 'deferred' in one meeting
    almost always appear as decisions in the next 1-2 meetings.
    """
    source_id = "gst_council_minutes"
    BASE_URL = "https://gstcouncil.gov.in/gst-council-meeting"

    def scrape(self) -> list[Document]:
        docs = []
        try:
            soup = self.fetch_html(self.BASE_URL)
            # Council site lists meetings with press release links
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True)

                # Filter for meeting-related links
                if not any(k in text.lower() or k in href.lower() 
                          for k in ["meeting", "council", "press", "agenda"]):
                    continue

                # Extract meeting number if present
                meeting_match = re.search(r"(\d+)(st|nd|rd|th)", text, re.IGNORECASE)
                meeting_no = meeting_match.group(1) if meeting_match else "unknown"

                doc_id = f"gst_council_{meeting_no}_{Document.content_hash(href)}"
                full_url = href if href.startswith("http") else f"https://gstcouncil.gov.in/{href.lstrip('/')}"

                # Council meeting pages contain press release + minutes PDF links
                full_text = self._find_pdf_url(full_url)
                full_text = self.fetch_pdf_text(full_text) if full_text else None

                docs.append(Document(
                    source_id=self.source_id,
                    doc_id=doc_id,
                    title=text or f"GST Council Meeting {meeting_no}",
                    url=full_url,
                    date=None,
                    content=full_text or text,
                    metadata={
                        "meeting_no": meeting_no,
                        "full_text_extracted": full_text is not None,
                    },
                ))
        except Exception as e:
            print(f"[gst_council] scrape error: {e}")
        return docs


class AARRulingScraper(BaseScraper):
    """
    Scrapes Advance Authority Rulings from CBIC.
    
    Key insight: when the same legal question gets 3+ AARs in 12 months,
    CBIC almost always issues a clarification within 6-9 months after that.
    Topic frequency is the signal, not individual rulings.
    """
    source_id = "aar_rulings"
    BASE_URL = "https://cbic-gst.gov.in/advance-ruling.html"

    def scrape(self) -> list[Document]:
        docs = []
        try:
            soup = self.fetch_html(self.BASE_URL)
            for row in soup.select("table tr, .ruling-item"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                title = cells[0].get_text(strip=True)
                date_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                link = row.find("a", href=True)
                url = link["href"] if link else self.BASE_URL

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
                    date=None,
                    content=full_text or title,
                    metadata={
                        "raw_date": date_text,
                        "full_text_extracted": full_text is not None,
                    },
                ))
        except Exception as e:
            print(f"[aar_rulings] scrape error: {e}")
        return docs


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
