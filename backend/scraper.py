"""
scraper.py — Bangalore Primary School Scraper
Targets SchoolDekho.com for Bangalore school listings.
Falls back gracefully to the curated seed dataset on any failure.
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [scraper] %(message)s')
log = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/121.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── Fee normalisation helpers ─────────────────────────────────────────────────

def parse_fee_text(raw: str) -> int | None:
    """
    Convert a raw fee string like '₹1.2 Lakh', '12,000/year', '2L–3L' → int (INR).
    Returns None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip().lower()
    if any(w in raw for w in ('free', 'nil', 'no fee', 'n/a', '—')):
        return 0

    # Prefer the first number block
    # Remove currency symbols, commas
    clean = re.sub(r'[₹$,]', '', raw)

    # Handle lakh variants: 1.2 lakh / 1.2l / 1,20,000
    lakh_match = re.search(r'([\d.]+)\s*(?:lakh|l\b)', clean)
    if lakh_match:
        try:
            return int(float(lakh_match.group(1)) * 100_000)
        except ValueError:
            pass

    # Plain number (could be monthly — multiply by 12 if 'month' present)
    num_match = re.search(r'[\d,]+', clean)
    if num_match:
        try:
            val = int(num_match.group().replace(',', ''))
            if 'month' in raw:
                val *= 12
            return val
        except ValueError:
            pass

    return None


def fmt_inr(amount: int) -> str:
    """Format an integer INR amount for display."""
    if amount == 0:
        return 'Free'
    if amount >= 100_000:
        lakhs = amount / 100_000
        label = f'₹{lakhs:.1f} Lakhs' if lakhs != int(lakhs) else f'₹{int(lakhs)} Lakhs'
        return label
    if amount >= 1_000:
        return f'₹{amount:,}'
    return f'₹{amount}'


# ── SchoolDekho scraper ───────────────────────────────────────────────────────

BASE_URL  = 'https://www.schooldekho.com'
LIST_URL  = (
    'https://www.schooldekho.com/school/list/'
    '?city=bangalore&type=primary&page={page}'
)


def _get_soup(url: str, retries: int = 2) -> BeautifulSoup | None:
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, timeout=12)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, 'html.parser')
            log.warning('HTTP %s for %s', resp.status_code, url)
        except requests.RequestException as exc:
            log.warning('Request error (attempt %d): %s', attempt + 1, exc)
        if attempt < retries:
            time.sleep(2 ** attempt)  # exponential back-off
    return None


def _scrape_list_page(page: int) -> list[dict]:
    """Scrape one listing page; return list of partial school dicts."""
    soup = _get_soup(LIST_URL.format(page=page))
    if soup is None:
        return []

    schools = []
    # SchoolDekho uses several possible card class names — try the common ones
    cards = (
        soup.select('.school-card')
        or soup.select('.school-item')
        or soup.select('[class*="school-card"]')
        or soup.select('[class*="school-item"]')
        or soup.select('article')
    )

    if not cards:
        # Fallback: look for any block with an anchor that likely leads to a school page
        cards = soup.select('a[href*="/school/"]')

    log.info('Page %d → found %d candidate elements', page, len(cards))

    for card in cards:
        try:
            school = _parse_card(card)
            if school and school.get('name'):
                schools.append(school)
        except Exception as exc:
            log.debug('Card parse error: %s', exc)

    return schools


def _parse_card(card: BeautifulSoup) -> dict | None:
    """Extract school info from a single result card."""
    text = card.get_text(' ', strip=True)

    # Name — prefer explicit heading, else first meaningful text
    name = None
    for sel in ('h2', 'h3', 'h4', '.school-name', '[class*="name"]'):
        el = card.select_one(sel)
        if el and len(el.get_text(strip=True)) > 3:
            name = el.get_text(strip=True)
            break
    if not name:
        name = text[:80].split('\n')[0].strip() or None

    # Area / location
    area = None
    for sel in ('.location', '.address', '[class*="location"]', '[class*="area"]'):
        el = card.select_one(sel)
        if el:
            area = el.get_text(strip=True)
            break
    if not area:
        # Try to extract after common location markers
        loc_match = re.search(r'(?:location|area|address)[:\s]+([^\n,|]+)', text, re.I)
        if loc_match:
            area = loc_match.group(1).strip()

    # Board affiliation
    board = 'Unknown'
    board_match = re.search(r'\b(CBSE|ICSE|IB|IGCSE|State Board|State|Karnataka)\b', text, re.I)
    if board_match:
        board = board_match.group(1).upper()
        if board == 'KARNATAKA':
            board = 'State'

    # Type
    school_type = 'Private'
    if re.search(r'\bgovernment\b|\bgovt\b|\bkv\b|\bkendriya\b', text, re.I):
        school_type = 'Government'
    elif re.search(r'\binternational\b', text, re.I):
        school_type = 'International'

    # Fee — look for explicit fee elements first, then regex the whole text
    raw_fee = None
    for sel in ('.fee', '[class*="fee"]', '.fees', '[class*="fees"]'):
        el = card.select_one(sel)
        if el:
            raw_fee = el.get_text(strip=True)
            break
    if not raw_fee:
        fee_match = re.search(
            r'(?:fee|fees|annual|tuition)[^₹\d]*([₹\d][^\n<]{1,40})',
            text, re.I
        )
        if fee_match:
            raw_fee = fee_match.group(1)

    annual_fee = parse_fee_text(raw_fee) if raw_fee else None

    # Detail page URL
    detail_url = None
    link = card.select_one('a[href*="/school/"]') or card.find('a', href=re.compile(r'/school/'))
    if link:
        href = link.get('href', '')
        detail_url = href if href.startswith('http') else BASE_URL + href

    # Phone
    phone = 'N/A'
    phone_match = re.search(r'(\+?[\d][\d\s\-]{8,13}\d)', text)
    if phone_match:
        phone = phone_match.group(1).strip()

    return {
        'name':        name,
        'area':        area or 'Bengaluru',
        'board':       board,
        'type':        school_type,
        'annualFee':   annual_fee,
        'rawFeeText':  raw_fee,
        'detailUrl':   detail_url,
        'phone':       phone,
        'source':      'scraped',
    }


def _scrape_detail(school: dict) -> dict:
    """
    Visit the school's detail page to fill in missing fee data.
    Returns the enriched school dict.
    """
    url = school.get('detailUrl')
    if not url or school.get('annualFee') is not None:
        return school

    soup = _get_soup(url)
    if not soup:
        return school

    page_text = soup.get_text(' ', strip=True)

    # Try fee table rows
    fee_cells = soup.select('td, .fee-value, [class*="fee"]')
    for cell in fee_cells:
        raw = cell.get_text(strip=True)
        fee = parse_fee_text(raw)
        if fee is not None:
            school['annualFee'] = fee
            school['rawFeeText'] = raw
            break

    # Fallback regex on full page text
    if school.get('annualFee') is None:
        fee_match = re.search(
            r'(?:annual fee|tuition fee|total fee)[^₹\d]*([₹\d][^\n<]{1,40})',
            page_text, re.I
        )
        if fee_match:
            raw = fee_match.group(1)
            school['annualFee'] = parse_fee_text(raw)
            school['rawFeeText'] = raw

    # Phone if still missing
    if school.get('phone') in (None, 'N/A'):
        phone_match = re.search(r'(\+?[\d][\d\s\-]{8,13}\d)', page_text)
        if phone_match:
            school['phone'] = phone_match.group(1).strip()

    return school


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_schools(max_pages: int = 5, detail_limit: int = 20) -> list[dict]:
    """
    Scrape SchoolDekho for Bangalore primary school listings.

    Args:
        max_pages:    How many listing pages to scrape (each ~10 schools).
        detail_limit: Max detail-page visits to enrich fee data (rate-limit friendly).

    Returns:
        List of school dicts with at least: name, area, board, type, annualFee.
    """
    log.info('Starting scrape — up to %d listing pages', max_pages)
    all_schools: list[dict] = []
    seen_names: set[str] = set()

    for page in range(1, max_pages + 1):
        batch = _scrape_list_page(page)
        if not batch:
            log.info('Page %d returned no schools — stopping early', page)
            break

        for s in batch:
            key = (s.get('name') or '').lower().strip()
            if key and key not in seen_names:
                seen_names.add(key)
                all_schools.append(s)

        log.info('After page %d: %d unique schools', page, len(all_schools))
        time.sleep(1.2)  # polite crawl delay

    # Enrich top schools with detail-page fee data
    needs_detail = [s for s in all_schools if s.get('annualFee') is None]
    log.info('Visiting detail pages for %d schools (limit %d)', len(needs_detail), detail_limit)
    for s in needs_detail[:detail_limit]:
        _scrape_detail(s)
        time.sleep(0.8)

    # Finalise display fields for any school that has a numeric fee
    results = []
    for s in all_schools:
        fee = s.get('annualFee')
        if fee is None:
            fee = 0  # unknown → treat as free / unknown

        s['annualFee']  = fee
        s['feeDisplay'] = fmt_inr(fee)
        s['feePeriod']  = 'per year' if fee > 0 else '(unavailable / free)'
        s.setdefault('breakdown', {
            'tuition':   s.get('rawFeeText') or 'Contact school',
            'admission': 'Contact school',
            'transport': 'Contact school',
        })
        s.setdefault('affiliation', s.get('board', 'N/A'))
        results.append(s)

    log.info('Scrape complete — %d schools returned', len(results))
    return results
