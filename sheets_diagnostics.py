"""Temporary production verification; logs statuses and counts only."""
import base64
import logging
import os
import re
import uuid
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
            key = 'owlet_diagnostic_' + uuid.uuid4().hex
            response = session.post('https://sheets.googleapis.com/v4/spreadsheets/' + SHEET_ID + ':batchUpdate',
                params={'fields': 'spreadsheetId,replies'}, json={'requests': [
                {'createDeveloperMetadata': {'developerMetadata': {
                    'metadataKey': key, 'metadataValue': 'temporary',
                    'location': {'spreadsheet': True}, 'visibility': 'DOCUMENT'}}},
                {'deleteDeveloperMetadata': {'dataFilter': {'developerMetadataLookup': {'metadataKey': key}}}}
            ]}, timeout=(5, 185))
            response.raise_for_status()
            logging.warning('SHEETS_VERIFY metadata_roundtrip=ok seconds=%.2f', time.monotonic()-started)
    except Exception as exc:
        logging.warning('SHEETS_VERIFY write_error=%s', type(exc).__name__)
    try:
        with _session() as session:
            data = Sheets(session).get(SHEET_ID, fields='sheets(properties.title,data.rowData.values.userEnteredValue)')
            formulas = [c['userEnteredValue']['formulaValue'] for sh in data.get('sheets', [])
                for block in sh.get('data', []) for row in block.get('rowData', [])
                for c in row.get('values', []) if 'formulaValue' in c.get('userEnteredValue', {})]
            functions = sorted({f for formula in formulas for f in re.findall(r'([A-Z_]+)\s*\(', formula.upper())})
            logging.warning('SHEETS_VERIFY formula_count=%s functions=%s', len(formulas), functions)
    except Exception as exc:
        logging.warning('SHEETS_VERIFY formula_scan_error=%s', type(exc).__name__)
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
