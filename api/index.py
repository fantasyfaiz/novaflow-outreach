import os
import csv
import io
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
import anthropic
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify, request

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOG_CSV_PATH = os.path.join(BASE_DIR, 'data', 'fog_booth_only.csv')

SUPABASE_URL   = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY   = os.environ.get('SUPABASE_KEY', '')
FOLLOW_UP_DAYS = int(os.environ.get('FOLLOW_UP_DAYS', '7'))

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'public'),
            static_url_path='')

REQUIRED_COLS = {'First Name', 'Last Name', 'Email', 'Company', 'Job Title'}
CANONICAL     = {col.lower(): col for col in REQUIRED_COLS | {'Grade'}}


def parse_csv_stream(stream):
    """Returns (contacts, filter_note) or raises ValueError."""
    raw = stream.read()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('latin-1')

    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [CANONICAL.get(f.lower(), f) for f in (reader.fieldnames or [])]
    fieldnames = set(reader.fieldnames)
    missing   = REQUIRED_COLS - fieldnames
    if missing:
        raise ValueError(f'Missing required columns: {", ".join(sorted(missing))}')

    has_grade = 'Grade' in fieldnames
    contacts  = []
    for row in reader:
        if has_grade and row.get('Grade', '').strip() != 'Neutral':
            continue
        email = row.get('Email', '').strip()
        if email:
            contacts.append({
                'first_name': row.get('First Name', '').strip(),
                'last_name':  row.get('Last Name',  '').strip(),
                'email':      email,
                'company':    row.get('Company',    '').strip(),
                'job_title':  row.get('Job Title',  '').strip(),
            })

    note = 'filtered to Neutral grade' if has_grade else 'all contacts loaded'
    return contacts, note


def generate_followup_paragraph(first_name, company, job_title):
    client = anthropic.Anthropic()
    prompt = f"""Write exactly ONE short paragraph (2-3 sentences) for a follow-up outreach email.

Context: This is a follow-up to an earlier cold outreach email about Novaflow (a bioinformatics/genomics analysis platform, YC-backed) sent about a week ago with no response.

Recipient:
- Name: {first_name}
- Company: {company}
- Job Title: {job_title}

About Novaflow:
- Bioinformatics analysis platform that takes raw genomic/sequencing data and produces results fast
- YC-backed, used at Harvard and Johns Hopkins
- Offering to analyze the recipient's own dataset for free

Requirements:
- Briefly acknowledge this is a follow-up, without being pushy or apologetic
- Keep the free dataset offer visible but light
- Sound human and genuine
- Do not start with "I"
- Do not use em dashes
- Do not include any greeting or sign-off
- Output only the paragraph text"""

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=200,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}]
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


def generate_paragraph(first_name, company, job_title, conference_name='', conference_location=''):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    conference_line = ''
    if conference_name:
        loc = f' in {conference_location}' if conference_location else ''
        conference_line = f'\n- The recipient connected with Novaflow at {conference_name}{loc}'
    prompt = f"""Write exactly ONE paragraph (4-5 sentences) for a biotech/genomics software outreach email.

Recipient:
- Name: {first_name}
- Company: {company}
- Job Title: {job_title}{conference_line}

About Novaflow:
- Bioinformatics analysis platform that takes raw genomic/sequencing data and produces results fast
- YC-backed startup built by researchers for researchers
- Labs at Harvard and Johns Hopkins are already using it
- Offering to analyze the recipient's own dataset for free, no commitment, no strings attached

Requirements:
- Connect their specific role and company to what Novaflow does
- Naturally weave in the free dataset offer (mention it once)
- Naturally weave in the YC backing and Harvard/Johns Hopkins social proof (mention once)
- Sound genuine and professional, not salesy
- Do not start with "I"
- Do not use any em dashes
- Do not include any greeting or sign-off
- Output only the paragraph text"""

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=300,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}]
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


def save_contact_to_supabase(contact, mode):
    """Upsert one contact into the Supabase `contacts` table.
    Returns the inserted/updated row dict. Raises RuntimeError on failure."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError('Supabase not configured (set SUPABASE_URL and SUPABASE_KEY)')

    now      = datetime.now(timezone.utc)
    due_date = (now + timedelta(days=FOLLOW_UP_DAYS)).date()
    payload  = {
        'first_name':     contact.get('first_name', '').strip(),
        'last_name':      contact.get('last_name',  '').strip(),
        'email':          contact.get('email',      '').strip().lower(),
        'company':        contact.get('company',    '').strip(),
        'job_title':      contact.get('job_title',  '').strip(),
        'date_contacted': now.isoformat(),
        'email_status':   'sent',
        'mode':           'conference' if mode == 'conference' else 'researcher',
        'follow_up_due':  due_date.isoformat(),
    }

    # on_conflict on the lower(email) unique index — re-sends update the row.
    url  = f'{SUPABASE_URL}/rest/v1/contacts?on_conflict=email'
    body = json.dumps([payload]).encode('utf-8')
    req  = urllib.request.Request(url, data=body, method='POST', headers={
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'resolution=merge-duplicates,return=representation',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode('utf-8') or '[]')
            return rows[0] if rows else payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')
        raise RuntimeError(f'Supabase {e.code}: {detail}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Supabase unreachable: {e.reason}')


# ── Routes ────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/save-contact', methods=['POST'])
def save_contact():
    data    = request.json or {}
    contact = data.get('contact')
    if not contact or not contact.get('email', '').strip():
        return jsonify({'error': 'contact with email is required'}), 400
    try:
        row = save_contact_to_supabase(contact, data.get('mode', 'researcher'))
        return jsonify({'saved': True, 'contact': row})
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/upload', methods=['POST'])
def upload_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.csv'):
        return jsonify({'error': 'Please upload a .csv file'}), 400
    try:
        contacts, note = parse_csv_stream(f.stream)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Parse error: {e}'}), 500
    if not contacts:
        return jsonify({'error': 'No valid contacts found in file'}), 400
    return jsonify({'contacts': contacts, 'note': note})


@app.route('/api/preset/fog-faiz')
def preset_fog_faiz():
    try:
        with open(FOG_CSV_PATH, 'rb') as f:
            contacts, note = parse_csv_stream(f)
    except FileNotFoundError:
        return jsonify({'error': 'Preset file not found'}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'contacts': contacts, 'note': note})


@app.route('/api/generate', methods=['POST'])
def generate_email():
    data    = request.json or {}
    contact = data.get('contact')
    if not contact:
        return jsonify({'error': 'contact is required'}), 400
    try:
        para = generate_paragraph(
            contact.get('first_name', ''),
            contact.get('company',    ''),
            contact.get('job_title',  ''),
            data.get('conference_name', ''),
            data.get('conference_location', ''),
        )
        return jsonify({'para': para})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate-followup', methods=['POST'])
def generate_followup_email():
    data    = request.json or {}
    contact = data.get('contact')
    if not contact:
        return jsonify({'error': 'contact is required'}), 400
    try:
        para = generate_followup_paragraph(
            contact.get('first_name', ''),
            contact.get('company',    ''),
            contact.get('job_title',  ''),
        )
        return jsonify({'para': para})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/scrape', methods=['POST'])
def scrape_contacts():
    data  = request.json or {}
    url   = data.get('url', '').strip()
    query = data.get('query', '').strip()

    if not url and not query:
        return jsonify({'error': 'Provide a URL or institution name'}), 400

    # If institution name given, try common people-page patterns
    if not url and query:
        slug = re.sub(r'[^a-z0-9]', '', query.lower())
        candidates = [
            f'https://www.{slug}.edu/people',
            f'https://www.{slug}.org/team',
            f'https://www.{slug}.com/team',
        ]
        for candidate in candidates:
            try:
                r = requests.get(candidate, timeout=8,
                                 headers={'User-Agent': 'Mozilla/5.0 (compatible; research-scraper/1.0)'})
                if r.status_code == 200:
                    url = candidate
                    break
            except Exception:
                continue
        if not url:
            return jsonify({'error': f'Could not find a people page for "{query}". Try pasting the URL directly.'}), 400

    # Normalise URL
    if not url.startswith('http'):
        url = 'https://' + url

    # Fetch page
    try:
        resp = requests.get(
            url, timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Page took too long to load (>15s)'}), 400
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'Page returned {e.response.status_code}'}), 400
    except Exception as e:
        return jsonify({'error': f'Could not fetch page: {str(e)}'}), 400

    # Clean HTML — strip noise, keep readable text
    soup = BeautifulSoup(resp.text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'iframe']):
        tag.decompose()
    page_text = soup.get_text(separator='\n', strip=True)
    page_text = re.sub(r'\n{3,}', '\n\n', page_text)[:14000]  # ~3500 tokens

    if len(page_text) < 100:
        return jsonify({'error': 'Page appears to be empty or JavaScript-rendered — try a static people-page URL'}), 400

    # Extract contacts with Claude
    client = anthropic.Anthropic()
    extraction_prompt = f"""Extract all researcher or team member contact details from this webpage text.

Return ONLY a valid JSON array. Each element must have exactly these fields:
  "first_name"  – string (required)
  "last_name"   – string (required)
  "email"       – string (empty string "" if not found)
  "company"     – string (institution or lab name, infer from context)
  "job_title"   – string (their role, e.g. "PhD Student", "Principal Investigator")

Rules:
- Only include real named people with at least a name and title
- Ignore nav links, generic department names, and non-person entries
- If the page has no clear people listings return []
- Output the JSON array only — no markdown, no explanation

Page URL: {url}

Page text:
{page_text}"""

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': extraction_prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        contacts = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({'error': 'Could not parse contacts from page — try a more structured people page'}), 500
    except Exception as e:
        return jsonify({'error': f'Extraction error: {str(e)}'}), 500

    if not isinstance(contacts, list):
        contacts = []

    # Normalise fields
    clean = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        first = str(c.get('first_name', '')).strip()
        last  = str(c.get('last_name',  '')).strip()
        if not first and not last:
            continue
        clean.append({
            'first_name': first,
            'last_name':  last,
            'email':      str(c.get('email',     '')).strip(),
            'company':    str(c.get('company',   '')).strip(),
            'job_title':  str(c.get('job_title', '')).strip(),
        })

    if not clean:
        return jsonify({'error': 'No researchers found on that page — try a lab people/team page'}), 400

    return jsonify({
        'contacts': clean,
        'note': f'{len(clean)} researchers scraped from {url}',
        'source_url': url,
    })


if __name__ == '__main__':
    app.run(debug=True, port=5001)
