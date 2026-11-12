"""Temporary audit-only write probe; no participant records are modified."""
import logging
import uuid
import requests
from urllib.parse import quote
from sheets_api import SHEET_ID, _session, read_value_ranges


def probe(app):
    key = 'diagnostic-' + uuid.uuid4().hex
    with _session(write=True) as session:
        base = 'https://sheets.googleapis.com/v4/spreadsheets/' + SHEET_ID
        try:
            response = session.post(base + '/values/' + quote("'_WebsiteReports'!A:C", safe='') + ':append',
                params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'},
                json={'values': [[key, 'diagnostic', '{"success":true,"diagnostic":true}']]}, timeout=(5, 8))
            logging.warning('SHEETS_VALUES_WRITE status=%s', response.status_code)
        except requests.RequestException as exc:
            logging.warning('SHEETS_VALUES_WRITE error=%s', type(exc).__name__)
        try:
            rows = read_value_ranges(session, SHEET_ID, ["'_WebsiteReports'!A2:A"])[0]['values']
            logging.warning('SHEETS_VALUES_WRITE committed=%s', any(r and r[0] == key for r in rows))
        except Exception as exc:
            logging.warning('SHEETS_VALUES_WRITE verification_error=%s', type(exc).__name__)
