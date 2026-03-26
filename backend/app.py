"""
app.py — Flask API for Bangalore Primary Schools Fee Search & NutriCheck India
Endpoints:
  GET  /api/schools           → filtered + sorted school list
  GET  /api/status            → data freshness info
  POST /api/scrape            → trigger background web scrape
  GET  /api/scrape/status     → check ongoing scrape progress
  POST /api/score             → score ingredient list for health impact
  GET  /api/product/<barcode> → lookup product by barcode (Open Food Facts)
  GET  /api/conditions        → list supported health conditions
  GET  /                      → serves schools.html
  GET  /<path>                → serves other static assets
"""

from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
import json
import os
import re
import threading
import logging
import time
from datetime import datetime, timezone
import requests as http_requests

# ── App setup ─────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR      = os.path.join(BASE_DIR, '..')          # project root (schools.html lives here)
DATA_FILE       = os.path.join(BASE_DIR, 'data', 'schools.json')
LOCK_FILE       = os.path.join(BASE_DIR, 'data', '.scrape.lock')
INGREDIENTS_DB  = os.path.join(BASE_DIR, 'data', 'ingredients_db.json')

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='')
CORS(app, resources={r'/api/*': {'origins': '*'}})

logging.basicConfig(level=logging.INFO, format='%(asctime)s [flask] %(message)s')
log = logging.getLogger(__name__)

# ── Reddit review cache (in-memory, TTL 1 hour) ───────────────────────────────
_reddit_cache: dict = {}   # key → {"ts": float, "data": list}
_REDDIT_TTL   = 3600       # seconds


# ── Scrape state (in-memory) ──────────────────────────────────────────────────
_scrape_state = {
    'running':   False,
    'progress':  '',
    'error':     None,
    'completed': None,   # ISO timestamp of last successful scrape
}
_scrape_lock = threading.Lock()


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_data(data: dict) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)     # atomic write


def merge_schools(curated: list[dict], scraped: list[dict]) -> list[dict]:
    """
    Merge scraped results into the curated list.
    - Curated entries are kept as-is (they have richer fee breakdowns).
    - Scraped entries not in curated are appended.
    - Matching is done on normalised school name.
    """
    def norm(name: str) -> str:
        return (name or '').lower().strip()

    existing = {norm(s['name']) for s in curated}
    new_entries = [s for s in scraped if norm(s.get('name', '')) not in existing]
    log.info('Merge: %d curated + %d new scraped entries', len(curated), len(new_entries))
    return curated + new_entries


# ── Filtering helpers ─────────────────────────────────────────────────────────

def apply_filters(schools: list[dict], args) -> list[dict]:
    query  = (args.get('q') or '').lower().strip()
    board  = args.get('board', '')
    stype  = args.get('type', '')
    budget = args.get('budget', '')

    def matches(s):
        if query and (query not in s.get('name', '').lower() and
                      query not in s.get('area', '').lower()):
            return False
        if board and board.upper() not in (s.get('board') or '').upper():
            return False
        if stype and s.get('type', '') != stype:
            return False
        fee = s.get('annualFee', 0) or 0
        if budget == 'high' and fee < 200_000:
            return False
        if budget == 'mid'  and not (50_000 <= fee < 200_000):
            return False
        if budget == 'low'  and fee >= 50_000:
            return False
        return True

    return [s for s in schools if matches(s)]


def apply_sort(schools: list[dict], sort_dir: str) -> list[dict]:
    reverse = (sort_dir != 'asc')
    return sorted(schools, key=lambda s: s.get('annualFee') or 0, reverse=reverse)


# ── Background scrape ─────────────────────────────────────────────────────────

def _run_scrape():
    global _scrape_state
    log.info('Background scrape started')

    try:
        _scrape_state['progress'] = 'Fetching school listings from SchoolDekho…'
        from scraper import scrape_schools   # lazy import keeps startup fast

        scraped = scrape_schools(max_pages=5, detail_limit=20)
        _scrape_state['progress'] = f'Fetched {len(scraped)} schools — merging with curated data…'

        data = load_data()
        merged = merge_schools(data.get('schools', []), scraped)
        data['schools']     = merged
        data['lastUpdated'] = datetime.now(timezone.utc).isoformat()
        data['source']      = 'curated + scraped'
        save_data(data)

        _scrape_state['completed'] = data['lastUpdated']
        _scrape_state['progress']  = f'Done — {len(merged)} schools in database'
        log.info('Scrape finished. Total schools: %d', len(merged))

    except Exception as exc:
        log.exception('Scrape failed: %s', exc)
        _scrape_state['error']    = str(exc)
        _scrape_state['progress'] = 'Scrape failed — see server logs'

    finally:
        with _scrape_lock:
            _scrape_state['running'] = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/api/schools', methods=['GET'])
def get_schools():
    """
    Query params:
      q        – search term (name / area)
      board    – CBSE | ICSE | IB | IGCSE | State
      type     – International | Private | Government
      budget   – high (>₹2L) | mid (₹50K–₹2L) | low (<₹50K)
      sort     – asc | desc (default: desc = highest fees first)
      page     – 1-based page number (default: 1)
      per_page – results per page (default: 50, max: 200)
    """
    try:
        data    = load_data()
        schools = data.get('schools', [])

        # Filter
        filtered = apply_filters(schools, request.args)

        # Sort
        sort_dir = request.args.get('sort', 'desc').lower()
        sorted_  = apply_sort(filtered, sort_dir)

        # Pagination
        per_page = min(int(request.args.get('per_page', 50)), 200)
        page     = max(int(request.args.get('page', 1)), 1)
        total    = len(sorted_)
        start    = (page - 1) * per_page
        end      = start + per_page
        paginated = sorted_[start:end]

        return jsonify({
            'schools':     paginated,
            'total':       total,
            'page':        page,
            'per_page':    per_page,
            'total_pages': max(1, -(-total // per_page)),  # ceiling division
            'lastUpdated': data.get('lastUpdated'),
            'source':      data.get('source', 'curated'),
        })

    except Exception as exc:
        log.exception('Error in /api/schools: %s', exc)
        return jsonify({'error': 'Internal server error', 'detail': str(exc)}), 500


@app.route('/api/status', methods=['GET'])
def get_status():
    """Return metadata about the current dataset."""
    try:
        data = load_data()
        schools = data.get('schools', [])
        boards  = sorted({s.get('board', 'Unknown') for s in schools})
        types   = sorted({s.get('type',  'Unknown') for s in schools})
        fees    = [s.get('annualFee', 0) or 0 for s in schools]

        return jsonify({
            'total':       len(schools),
            'lastUpdated': data.get('lastUpdated'),
            'source':      data.get('source', 'curated'),
            'boards':      boards,
            'types':       types,
            'feeRange': {
                'min': min(fees) if fees else 0,
                'max': max(fees) if fees else 0,
            },
            'scrapeRunning': _scrape_state['running'],
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/scrape', methods=['POST'])
def trigger_scrape():
    """
    Start a background scrape job (non-blocking).
    Returns 409 if a scrape is already running.
    """
    with _scrape_lock:
        if _scrape_state['running']:
            return jsonify({
                'status':   'already_running',
                'progress': _scrape_state['progress'],
            }), 409

        _scrape_state['running'] = True
        _scrape_state['error']   = None
        _scrape_state['progress'] = 'Starting…'

    thread = threading.Thread(target=_run_scrape, daemon=True)
    thread.start()

    return jsonify({'status': 'started', 'message': 'Scrape started in background'}), 202


@app.route('/api/scrape/status', methods=['GET'])
def scrape_status():
    """Poll the status of an ongoing / last scrape."""
    return jsonify({
        'running':   _scrape_state['running'],
        'progress':  _scrape_state['progress'],
        'error':     _scrape_state['error'],
        'completed': _scrape_state['completed'],
    })


# ── NutriCheck: Ingredient Scoring Engine ─────────────────────────────────────

def load_ingredients_db() -> dict:
    with open(INGREDIENTS_DB, 'r', encoding='utf-8') as f:
        return json.load(f)


def parse_ingredients(text: str) -> list[str]:
    """Parse an ingredient list string into individual ingredient names."""
    # Remove common prefixes
    text = re.sub(r'^(ingredients?\s*:?\s*)', '', text, flags=re.IGNORECASE)
    # Remove percentages like (23%) or 23%
    text = re.sub(r'\(?\d+\.?\d*\s*%\)?\s*', '', text)

    # Preserve E-number / INS parentheticals: "Tartrazine (E102)" → "Tartrazine E102"
    # Also preserve INS sub-numbers: "503(ii)" → "503ii"
    text = re.sub(r'\(\s*(E\d{3,4}[a-z]?)\s*\)', r' \1', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d{3,4})\(([iv]+)\)', r'\1\2', text, flags=re.IGNORECASE)

    # Expand bracketed sub-ingredients but keep parent too
    # e.g., "Edible Vegetable Oil (Palm Oil, Soybean Oil)" → "Edible Vegetable Oil, Palm Oil, Soybean Oil"
    text = text.replace('(', ',').replace(')', ',')
    text = text.replace('[', ',').replace(']', ',')

    # Split by commas, semicolons, or " and " (with spaces to avoid splitting "Ferrous Fumarate And Zinc")
    parts = re.split(r'[,;]+', text)

    # Clean each ingredient
    ingredients = []
    for part in parts:
        cleaned = part.strip().strip('.').strip()
        # Remove "contains" prefix
        cleaned = re.sub(r'^contains?\s*:?\s*', '', cleaned, flags=re.IGNORECASE)
        # Remove lone roman numerals left over from INS parsing
        if re.match(r'^[iv]+$', cleaned, re.IGNORECASE):
            continue
        if cleaned and len(cleaned) > 1:
            ingredients.append(cleaned)
    return ingredients


def match_ingredient(name: str, db: dict) -> dict | None:
    """Try to match an ingredient name against the database."""
    lower = name.lower().strip()

    # Direct match in harmful
    if lower in db['harmful']:
        return {**db['harmful'][lower], 'type': 'harmful'}

    # Direct match in positive
    if lower in db['positive']:
        return {**db['positive'][lower], 'type': 'positive'}

    # E-number lookup (E102, E211, etc.)
    upper = name.upper().strip()
    if upper in db.get('e_numbers', {}):
        mapped = db['e_numbers'][upper]
        if mapped in db['harmful']:
            return {**db['harmful'][mapped], 'type': 'harmful'}

    # INS number lookup — Indian labels use INS format like "621", "102"
    # Map bare numbers to E-numbers
    ins_match = re.match(r'^(\d{3,4})(?:\([iv]+\))?$', lower.strip())
    if ins_match:
        ins_num = ins_match.group(1)
        e_key = f'E{ins_num}'
        if e_key in db.get('e_numbers', {}):
            mapped = db['e_numbers'][e_key]
            if mapped in db['harmful']:
                return {**db['harmful'][mapped], 'type': 'harmful'}

    # Word-boundary fuzzy match — only match if the DB key appears as a
    # complete word/phrase in the ingredient name (not a substring of
    # another word). Require the key to be at least 4 chars to avoid
    # false positives with short strings like "oil" or "fat".
    #
    # Check POSITIVE first so "organic whole wheat flour" matches
    # "whole wheat" (positive) rather than "wheat flour" (harmful).
    for key, entry in db['positive'].items():
        if len(key) < 4:
            continue
        pattern = r'\b' + re.escape(key) + r'\b'
        if re.search(pattern, lower):
            return {**entry, 'type': 'positive'}

    for key, entry in db['harmful'].items():
        if len(key) < 4:
            continue
        pattern = r'\b' + re.escape(key) + r'\b'
        if re.search(pattern, lower):
            return {**entry, 'type': 'harmful'}

    return None


def score_ingredients(ingredient_text: str, product_type: str = 'food',
                      conditions: list[str] = None) -> dict:
    """Score a list of ingredients and return detailed results."""
    db = load_ingredients_db()
    parsed = parse_ingredients(ingredient_text)
    conditions = conditions or []

    score = 100
    ingredients_result = []
    fssai_alerts = []
    family_alerts = []
    matched_count = 0

    for name in parsed:
        match = match_ingredient(name, db)

        if match:
            matched_count += 1
            entry = {
                'name': name,
                'matched': True,
                'score': match.get('score', 0),
                'severity': match.get('severity', 'moderate'),
                'label': match.get('label', name),
                'description': match.get('description', ''),
                'category': match.get('category', ''),
                'type': match.get('type', 'harmful'),
            }

            # Apply score
            score += match.get('score', 0)

            # FSSAI check
            fssai_status = match.get('fssai_status')
            if fssai_status:
                entry['fssai_status'] = fssai_status
                if fssai_status == 'banned':
                    fssai_alerts.append(f"{match.get('label', name)} is BANNED by FSSAI")
                elif fssai_status == 'restricted':
                    fssai_alerts.append(f"{match.get('label', name)} is restricted by FSSAI")

            # Also check fssai_banned list
            if name.lower() in [b.lower() for b in db.get('fssai_banned', [])]:
                if not fssai_status:
                    entry['fssai_status'] = 'banned'
                    fssai_alerts.append(f"{name} is BANNED by FSSAI")

            # Condition-specific alerts
            condition_alerts = []
            ingredient_conditions = match.get('conditions', [])
            condition_warnings = match.get('condition_warnings', {})

            for cond in conditions:
                if cond in ingredient_conditions:
                    warning = condition_warnings.get(cond, f"May be concerning for {cond}")
                    condition_alerts.append({
                        'condition': cond,
                        'warning': warning
                    })
                    family_alerts.append({
                        'condition': cond,
                        'ingredient': match.get('label', name),
                        'warning': warning
                    })
                    score -= 5  # Extra deduction for condition-relevant ingredients

            if condition_alerts:
                entry['condition_alerts'] = condition_alerts

            ingredients_result.append(entry)
        else:
            ingredients_result.append({
                'name': name,
                'matched': False,
                'severity': 'neutral',
                'label': name.title(),
                'description': '',
                'type': 'unknown',
            })

    # Deduplicate alerts
    fssai_alerts = list(dict.fromkeys(fssai_alerts))
    seen_family = set()
    unique_family = []
    for fa in family_alerts:
        key = (fa['condition'], fa['ingredient'])
        if key not in seen_family:
            seen_family.add(key)
            unique_family.append(fa)
    family_alerts = unique_family

    # Clamp score
    score = max(0, min(100, score))

    # Determine category
    if score >= 75:
        category = 'Excellent'
        color = '#4CAF50'
    elif score >= 50:
        category = 'Good'
        color = '#8BC34A'
    elif score >= 25:
        category = 'Mediocre'
        color = '#FF9800'
    else:
        category = 'Poor'
        color = '#F44336'

    return {
        'score': score,
        'category': category,
        'color': color,
        'ingredients': ingredients_result,
        'fssai_alerts': fssai_alerts,
        'family_alerts': family_alerts,
        'total_ingredients': len(parsed),
        'matched_ingredients': matched_count,
        'unmatched_ingredients': len(parsed) - matched_count,
    }


# ── NutriCheck API Routes ────────────────────────────────────────────────────

@app.route('/api/score', methods=['POST'])
def api_score():
    """Score ingredient list for health impact."""
    try:
        data = request.get_json(force=True)
        ingredient_text = data.get('ingredients', '')
        product_type = data.get('product_type', 'food')
        conditions = data.get('conditions', [])

        if not ingredient_text.strip():
            return jsonify({'error': 'No ingredients provided'}), 400

        result = score_ingredients(ingredient_text, product_type, conditions)
        return jsonify(result)

    except Exception as exc:
        log.exception('Error in /api/score: %s', exc)
        return jsonify({'error': 'Internal server error', 'detail': str(exc)}), 500


@app.route('/api/product/<barcode>', methods=['GET'])
def api_product(barcode):
    """Lookup product by barcode via Open Food Facts API."""
    try:
        # Sanitize barcode
        barcode = re.sub(r'[^0-9]', '', barcode)
        if not barcode:
            return jsonify({'error': 'Invalid barcode'}), 400

        conditions = request.args.get('conditions', '')
        conditions = [c.strip() for c in conditions.split(',') if c.strip()] if conditions else []

        # Fetch from Open Food Facts
        url = f'https://world.openfoodfacts.org/api/v2/product/{barcode}.json'
        resp = http_requests.get(url, timeout=10, headers={
            'User-Agent': 'NutriCheckIndia/1.0 (contact@nutricheck.in)'
        })

        if resp.status_code != 200:
            return jsonify({'found': False, 'error': 'Product not found in database'}), 404

        off_data = resp.json()
        if off_data.get('status') != 1:
            return jsonify({
                'found': False,
                'error': 'Product not found. Try entering ingredients manually.',
            }), 404

        product = off_data.get('product', {})
        product_name = product.get('product_name', '') or product.get('product_name_en', '')
        brand = product.get('brands', '')
        image_url = product.get('image_url', '') or product.get('image_front_url', '')
        ingredients_text = product.get('ingredients_text', '') or product.get('ingredients_text_en', '')
        nutriscore = product.get('nutriscore_grade', '')
        nova_group = product.get('nova_group', '')

        result = {
            'found': True,
            'product': {
                'name': product_name,
                'brand': brand,
                'image_url': image_url,
                'nutriscore': nutriscore,
                'nova_group': nova_group,
                'ingredients_text': ingredients_text,
            }
        }

        # Score ingredients if available
        if ingredients_text:
            score_result = score_ingredients(ingredients_text, 'food', conditions)
            result.update(score_result)
        else:
            result['score'] = None
            result['ingredients_available'] = False
            result['message'] = 'Ingredients not available for this product. Try entering them manually.'

        return jsonify(result)

    except http_requests.Timeout:
        return jsonify({'found': False, 'error': 'Request timed out. Please try again.'}), 504
    except Exception as exc:
        log.exception('Error in /api/product: %s', exc)
        return jsonify({'error': 'Internal server error', 'detail': str(exc)}), 500


@app.route('/api/conditions', methods=['GET'])
def api_conditions():
    """Return supported health conditions for family profile setup."""
    try:
        db = load_ingredients_db()
        return jsonify(db.get('conditions', {}))
    except Exception as exc:
        log.exception('Error in /api/conditions: %s', exc)
        return jsonify({'error': str(exc)}), 500


# ── Reddit Reviews ────────────────────────────────────────────────────────────

@app.route('/api/reviews', methods=['GET'])
def get_reviews():
    """
    Fetch Reddit r/bangalore mentions for a school.
    Query params:
      school – school name (required)
      area   – area hint (optional, improves search quality)
    Returns up to 5 posts; 'not_enough_info' flag if fewer than 2 found.
    Caches results for 1 hour to avoid hammering Reddit.
    """
    school = (request.args.get('school') or '').strip()
    if not school:
        return jsonify({'error': 'school param required'}), 400

    cache_key = school.lower()
    now = time.time()

    # Return cached result if fresh
    cached = _reddit_cache.get(cache_key)
    if cached and (now - cached['ts']) < _REDDIT_TTL:
        return jsonify(cached['data'])

    try:
        # Two token sets:
        # phrase_tokens — used for the Reddit search query (includes "school"
        #   so "Canadian International School" doesn't match Singapore results)
        # filter_tokens — used for post-fetch filtering (strips more generics
        #   so we don't create overly strict patterns)
        phrase_stop = {
            'the', 'a', 'an', 'and', 'of', 'for', 'in', 'at', 'by',
            'high', 'boys', 'girls', 'primary', 'secondary',
            'sr', 'jr', 'india', 'bangalore', 'bengaluru', 'home',
        }
        filter_stop = phrase_stop | {'school', 'academy', 'vidyalaya'}

        raw_tokens = re.split(r'\W+', school)
        phrase_tokens = [w for w in raw_tokens if w and w.lower() not in phrase_stop and len(w) > 1]
        filter_tokens = [w for w in raw_tokens if w and w.lower() not in filter_stop and len(w) > 1]

        # Build the quoted key phrase: use up to 3 phrase tokens so
        # "Canadian International School" → "Canadian International School"
        # rather than just "Canadian International" which matches Singapore
        key_phrase = ' '.join(phrase_tokens[:3]) if phrase_tokens else school
        query = f'"{key_phrase}"'

        headers = {'User-Agent': 'BangaloreSchoolFinder/1.0 (educational tool)'}

        # Context terms: post must mention school/education or Bangalore
        context_re = re.compile(
            r'\b(school|college|admission|fees?|education|students?|bangalore|bengaluru|karnataka)\b',
            re.IGNORECASE
        )

        # Filter patterns built from filter_tokens (word-boundary, case-insensitive)
        tok_patterns = [re.compile(r'\b' + re.escape(t) + r'\b', re.IGNORECASE)
                        for t in filter_tokens if len(t) > 2]

        blr_re  = re.compile(r'\b(bangalore|bengaluru|karnataka|blr)\b', re.IGNORECASE)
        # Exclude real-estate / property-listing spam that mentions schools
        # as "nearby amenities" — these posts are not school reviews
        spam_re = re.compile(
            r'\b(bhk|sq\.?\s*ft|sqft|pre.?launching|possession|plots?\s+for\s+sale'
            r'|villa\s+project|purva\s+land|sumadhura|prestige\s+white'
            r'|ready.to.build|rate\s+per\s+sqft|property\s+for\s+sale'
            r'|flat\s+for\s+(rent|sale)|for\s+living.*working.*invest'
            r'|gateway\s+to\s+dream|dream\s+living|launch(ing)?\s+code\s+name'
            r'|plots?\s+in\s+bangalore|apartments?\s+in\s+bangalore'
            r'|available\s+for\s+rent|lakh\s+per\s+month)\b',
            re.IGNORECASE
        )

        def _matches(title: str, selftext: str) -> bool:
            combined = title + ' ' + selftext
            # Must match ALL distinctive tokens at word boundaries
            if tok_patterns and not all(p.search(combined) for p in tok_patterns):
                return False
            # Must have school/education context
            if not context_re.search(combined):
                return False
            # Exclude real-estate spam posts (list schools as nearby amenities)
            if spam_re.search(combined):
                return False
            return True

        # Schools whose names start with a nationality/country word are globally
        # ambiguous — "Canadian International School" exists in 20 countries.
        # For those, append "bangalore" to the wider Reddit query.
        # India-specific chains (Ryan, DPS, NPS, KV) must NOT get "bangalore"
        # appended or their national reputation posts disappear.
        globally_ambiguous_prefixes = {
            'canadian', 'american', 'british', 'french', 'german', 'japanese',
            'korean', 'australian', 'irish', 'scottish', 'swiss', 'italian',
            'spanish', 'chinese', 'singapore', 'stonehill', 'gear',
        }
        first_token = (phrase_tokens[0].lower() if phrase_tokens else '')
        needs_blr_in_query = first_token in globally_ambiguous_prefixes

        def _fetch(restrict: bool) -> list:
            """Fetch and filter Reddit posts. restrict=True limits to r/bangalore."""
            q = query if (restrict or not needs_blr_in_query) else f'{query} bangalore'
            params = {
                'q':           q,
                'restrict_sr': '1' if restrict else '0',
                'sort':        'relevance',
                't':           'all',
                'limit':       '25',
            }
            base = 'https://www.reddit.com/r/bangalore/search.json' if restrict \
                   else 'https://www.reddit.com/search.json'
            resp = http_requests.get(base, params=params, headers=headers, timeout=8)
            if resp.status_code != 200:
                return []

            results = []
            for child in resp.json().get('data', {}).get('children', []):
                d        = child.get('data', {})
                title    = d.get('title', '')
                selftext = (d.get('selftext') or '').strip()

                if not _matches(title, selftext):
                    continue

                snippet = selftext[:200].strip()
                if len(selftext) > 200:
                    snippet += '…'
                results.append({
                    'title':    title,
                    'snippet':  snippet or None,
                    'url':      f"https://reddit.com{d.get('permalink', '')}",
                    'upvotes':  d.get('score', 0),
                    'comments': d.get('num_comments', 0),
                    'created':  d.get('created_utc'),
                })
                if len(results) == 5:
                    break
            return results

        # Try r/bangalore first; fall back to all of Reddit if sparse
        posts  = _fetch(restrict=True)
        source = 'r/bangalore'
        if len(posts) < 2:
            wider  = _fetch(restrict=False)
            if len(wider) > len(posts):
                posts  = wider
                source = 'Reddit'

        result = {
            'school':           school,
            'posts':            posts,
            'not_enough_info':  len(posts) < 2,
            'source':           source,
            'cached_at':        datetime.utcnow().isoformat(),
        }
        _reddit_cache[cache_key] = {'ts': now, 'data': result}
        return jsonify(result)

    except http_requests.Timeout:
        return jsonify({'error': 'Reddit request timed out', 'not_enough_info': True}), 504
    except Exception as exc:
        log.exception('Error fetching Reddit reviews: %s', exc)
        return jsonify({'error': str(exc), 'not_enough_info': True}), 500


# ── Static file serving ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'schools.html')


@app.route('/ingredients')
def ingredients_page():
    return send_from_directory(STATIC_DIR, 'ingredients.html')


@app.route('/<path:filename>')
def static_files(filename):
    safe = os.path.realpath(os.path.join(STATIC_DIR, filename))
    root = os.path.realpath(STATIC_DIR)
    if not safe.startswith(root):   # path traversal guard
        abort(403)
    return send_from_directory(STATIC_DIR, filename)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    log.info('Starting Flask server → http://localhost:5000')
    log.info('Schools data file: %s', DATA_FILE)
    app.run(host='0.0.0.0', port=5000, debug=True)
