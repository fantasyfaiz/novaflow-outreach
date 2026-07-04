import os
import csv
import io
import anthropic
from flask import Flask, render_template, jsonify, request

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOG_CSV_PATH = os.path.join(BASE_DIR, 'data', 'fog_booth_only.csv')

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


def generate_paragraph(first_name, company, job_title):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    prompt = f"""Write exactly ONE paragraph (4-5 sentences) for a biotech/genomics software outreach email.

Recipient:
- Name: {first_name}
- Company: {company}
- Job Title: {job_title}

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


# ── Routes ────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


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
        )
        return jsonify({'para': para})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001)
