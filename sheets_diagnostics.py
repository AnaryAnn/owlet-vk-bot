"""Temporary production verification; logs statuses and counts only."""
import base64
import logging
import os
import time
from sheets_api import SHEET_ID, _session
from website_store import Sheets


def probe(app):
    try:
        with _session(write=True) as session:
            api = Sheets(session)
            metadata = api.get(SHEET_ID, fields='developerMetadata')
            logging.warning('SHEETS_VERIFY shared_lock=%s', any(
                m.get('metadataId') == 1937152401 for m in metadata.get('developerMetadata', [])))
            # Empty atomic batch verifies the write endpoint without changing cells.
            started = time.monotonic()
            api.batch(SHEET_ID, [])
            logging.warning('SHEETS_VERIFY empty_write=ok seconds=%.2f', time.monotonic()-started)
    except Exception as exc:
        logging.warning('SHEETS_VERIFY write_error=%s', type(exc).__name__)
    password = os.environ.get('WEBSITE_SITE_PASSWORD') or os.environ.get('WEBSITE_ADMIN_PASSWORD', '')
    headers = {'X-Website-Password': base64.b64encode(password.encode()).decode()}
    with app.test_client() as client:
        for path in ['/api/website/stats', '/api/website/history', '/api/website/participants']:
            started = time.monotonic()
            response = client.get(path, headers=headers)
            data = response.get_json() or {}
            logging.warning('SHEETS_VERIFY path=%s status=%s success=%s records=%s weeks=%s seconds=%.2f',
                path, response.status_code, data.get('success'), len(data.get('currentRecords', [])),
                len(data.get('weeks', [])), time.monotonic()-started)
