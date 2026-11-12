"""Temporary end-to-end verification without changing participant values."""
import base64
import logging
import os
import time
import uuid
from contextlib import nullcontext
from collections import Counter
from sheets_api import SHEET_ID, _session, current_week, _quote, _number
from website_store import Sheets, save_report


def probe(app):
    started = time.monotonic()
    try:
        with _session(write=True) as session:
            api = Sheets(session)
            with api.locked(SHEET_ID):
                members = api.values(SHEET_ID, ["'Участники'!A2:C"])[0]
                active = {r[0] for r in members if len(r) > 1 and str(r[1]).isdigit()
                    and (len(r) < 3 or str(r[2]).lower() != 'выбыл')}
                title = current_week()['sheetName']
                rows = api.values(SHEET_ID, [f'{_quote(title)}!A8:D'])[0]
                counts = Counter(r[0] for r in rows if r)
                row = next(r for r in rows if len(r) == 4 and r[3] and r[0] in active
                    and counts[r[0]] == 1 and all(isinstance(_number(v), int) for v in r[1:3]))
                original = list(row)
                lock_method = api.locked
                api.locked = lambda _: nullcontext()  # The outer distributed lock is already held.
                try:
                    result, _, _ = save_report(api, row[0], _number(row[1]), _number(row[2]),
                        'diagnostic-roundtrip-' + uuid.uuid4().hex)
                finally:
                    api.locked = lock_method
                after = next(r for r in api.values(SHEET_ID, [f'{_quote(title)}!A8:D'])[0] if r and r[0] == row[0])
                logging.warning('REPORT_ROUNDTRIP success=%s unchanged=%s artifactEligible=%s seconds=%.2f',
                    result['success'], original == after, result['artifactEligible'], time.monotonic()-started)
    except Exception:
        logging.exception('REPORT_ROUNDTRIP failed')
    password = os.environ.get('WEBSITE_SITE_PASSWORD') or os.environ.get('WEBSITE_ADMIN_PASSWORD', '')
    headers = {'X-Website-Password': base64.b64encode(password.encode()).decode()}
    with app.test_client() as client:
        for path in ['/api/website/stats', '/api/website/history', '/api/website/participants']:
            response = client.get(path, headers=headers)
            data = response.get_json() or {}
            logging.warning('WEBSITE_ROUNDTRIP path=%s status=%s success=%s records=%s weeks=%s',
                path, response.status_code, data.get('success'), len(data.get('currentRecords', [])), len(data.get('weeks', [])))
