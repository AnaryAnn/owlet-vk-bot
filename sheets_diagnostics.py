"""Temporary non-cell write verification; no participant data is modified."""
import logging
import uuid
import requests
from sheets_api import SHEET_ID, _session
from website_store import Sheets


def probe(app):
    key = 'owlet_diagnostic_' + uuid.uuid4().hex
    with _session(write=True) as session:
        api = Sheets(session)
        def exists():
            data = api.get(SHEET_ID, fields='developerMetadata')
            return any(m.get('metadataKey') == key for m in data.get('developerMetadata', []))
        for operation in ['create', 'delete']:
            body = ({'createDeveloperMetadata': {'developerMetadata': {
                'metadataKey': key, 'metadataValue': 'temporary',
                'location': {'spreadsheet': True}, 'visibility': 'DOCUMENT'}}} if operation == 'create' else
                {'deleteDeveloperMetadata': {'dataFilter': {'developerMetadataLookup': {'metadataKey': key}}}})
            try:
                response = session.post('https://sheets.googleapis.com/v4/spreadsheets/' + SHEET_ID + ':batchUpdate',
                    json={'requests': [body]}, timeout=(5, 5))
                logging.warning('SHEETS_COMMIT operation=%s status=%s', operation, response.status_code)
            except requests.RequestException as exc:
                logging.warning('SHEETS_COMMIT operation=%s error=%s', operation, type(exc).__name__)
            try:
                logging.warning('SHEETS_COMMIT after=%s exists=%s', operation, exists())
            except Exception as exc:
                logging.warning('SHEETS_COMMIT verification_error=%s', type(exc).__name__)
