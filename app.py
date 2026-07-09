from flask import Flask, render_template, request, jsonify
import csv
import os
import sys

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/send-single', methods=['POST'])
def send_single():
    data = request.json
    phone = data.get('phone')
    name = data.get('name', 'there')
    template = data.get('template', 'Hi {first_name}, this is a message from your clinic. Reply STOP to unsubscribe.')

    temp_csv = os.path.join(BASE, 'single_send.csv')
    with open(temp_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['phone', 'first_name'])
        writer.writerow([phone, name])

    result = os.popen(
        f'cd {BASE} && "{sys.executable}" send_campaign.py --csv single_send.csv --limit 1 --yes --template "{template}" 2>&1'
    ).read()

    return jsonify({'output': result})

@app.route('/send-bulk', methods=['POST'])
def send_bulk():
    template = request.form.get('template', 'Hi {first_name}, this is a message from your clinic. Reply STOP to unsubscribe.')
    batch_size = request.form.get('batch_size', '300')
    file = request.files.get('csv_file')

    if not file:
        return jsonify({'error': 'No file uploaded'})

    csv_path = os.path.join(BASE, 'bulk_upload.csv')
    file.save(csv_path)

    result = os.popen(
        f'cd {BASE} && "{sys.executable}" send_campaign.py --csv bulk_upload.csv --batch-size {batch_size} --yes --template "{template}" 2>&1'
    ).read()

    return jsonify({'output': result})

if __name__ == '__main__':
    app.run(debug=True, port=5000)