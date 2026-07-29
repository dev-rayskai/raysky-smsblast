from flask import Flask, render_template, request, jsonify
import csv
import os
import io
import subprocess
import sys
from datetime import datetime
import pytz

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/parse-csv', methods=['POST'])
def parse_csv():
    file = request.files.get('csv_file')
    if not file:
        return jsonify({'error': 'No file uploaded'})

    content = file.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content))
    
    if not reader.fieldnames:
        return jsonify({'error': 'CSV file is empty'})

    fieldnames = [f.lower().strip() for f in reader.fieldnames]
    
    total = 0
    valid = 0
    flagged = 0
    flagged_rows = []
    sample_recipient = None

    import re
    def normalize_phone(raw):
        digits = re.sub(r'\D', '', raw or '')
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        if len(digits) != 10:
            return None
        if digits[0] in '01' or digits[3] in '01':
            return None
        return f'+1{digits}'

    phone_cols = ('phone', 'phone_number', 'mobile', 'cell', 'number')
    name_cols = ('first_name', 'firstname', 'given_name', 'name')
    last_name_cols = ('last_name', 'lastname', 'surname', 'family_name')

    def pick_col(fieldnames, candidates):
        for c in candidates:
            if c in fieldnames:
                return c
        return None

    phone_col = pick_col(fieldnames, phone_cols)
    name_col = pick_col(fieldnames, name_cols)
    last_name_col = pick_col(fieldnames, last_name_cols)

    if not phone_col:
        return jsonify({'error': f'No phone column found. Got: {reader.fieldnames}'})

    seen = set()
    
    for i, row in enumerate(reader, start=1):
        total += 1
        lower_row = {k.lower().strip(): v for k, v in row.items()}
        phone = normalize_phone(lower_row.get(phone_col, ''))
        first_name = lower_row.get(name_col, '') if name_col else ''
        last_name = lower_row.get(last_name_col, '') if last_name_col else ''

        if not phone:
            flagged += 1
            flagged_rows.append({'row': i, 'reason': 'Invalid phone', 'data': lower_row.get(phone_col, '')})
            continue
        if phone in seen:
            flagged += 1
            flagged_rows.append({'row': i, 'reason': 'Duplicate', 'data': phone})
            continue
        seen.add(phone)
        valid += 1

        if valid == 1:
            sample_recipient = {
                'first_name': first_name or 'Patient',
                'last_name': last_name or '',
                'phone': phone
            }

    detected_cols = []
    if name_col: detected_cols.append(name_col)
    if last_name_col: detected_cols.append(last_name_col)
    if phone_col: detected_cols.append(phone_col)

    return jsonify({
        'total': total,
        'valid': valid,
        'flagged': flagged,
        'flagged_rows': flagged_rows[:50],
        'columns_detected': ', '.join(detected_cols),
        'sample_recipient': sample_recipient
    })


@app.route('/send-single', methods=['POST'])
def send_single():
    data = request.json
    phone = data.get('phone')
    name = data.get('name', 'there')
    last_name = data.get('last_name', '')
    template = data.get('template', 'Hi {first_name}, this is a message from your clinic. Reply STOP to unsubscribe.')

    temp_csv = os.path.join(BASE, 'single_send.csv')
    with open(temp_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['phone', 'first_name', 'last_name'])
        writer.writerow([phone, name, last_name])

    proc = subprocess.run(
        [sys.executable, 'send_campaign.py',
         '--csv', 'single_send.csv', '--limit', '1', '--yes',
         '--template', template],
        cwd=BASE, capture_output=True, text=True,
    )
    result = proc.stdout + proc.stderr

    return jsonify({'output': result})


@app.route('/send-bulk', methods=['POST'])
def send_bulk():
    template = request.form.get('template', 'Hi {first_name}, this is a message from your clinic. Reply STOP to unsubscribe.')
    batch_size = request.form.get('batch_size', '300')
    batch_pause = request.form.get('batch_pause', '45')
    rate = request.form.get('rate', '10')
    consent = request.form.get('consent', 'false')
    file = request.files.get('csv_file')

    if not file:
        return jsonify({'error': 'No file uploaded'})

    if consent != 'true':
        return jsonify({'error': 'You must confirm CASL consent before sending.'})

    toronto = pytz.timezone('America/Toronto')
    now = datetime.now(toronto)
    if now.hour < 9 or now.hour >= 20:
        return jsonify({'error': f'Outside sending window (09:00-20:00 Toronto). Current time: {now.strftime("%H:%M")} ET. Campaign will queue and resume inside the window.'})

    csv_path = os.path.join(BASE, 'bulk_upload.csv')
    file.save(csv_path)

    proc = subprocess.run(
        [sys.executable, 'send_campaign.py',
         '--csv', 'bulk_upload.csv',
         '--batch-size', batch_size,
         '--batch-pause', batch_pause,
         '--rate', rate,
         '--yes',
         '--template', template],
        cwd=BASE, capture_output=True, text=True,
    )
    result = proc.stdout + proc.stderr

    return jsonify({'output': result})


@app.route('/results')
def results():
    results_path = os.path.join(BASE, 'results.csv')
    if not os.path.exists(results_path):
        return jsonify({'rows': []})

    rows = []
    with open(results_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row.pop(None, None)
            rows.append(row)

    return jsonify({'rows': rows})


if __name__ == '__main__':
    app.run(debug=True, port=5000)