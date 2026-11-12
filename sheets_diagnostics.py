"""Temporary checks confined to a diagnostic journal cell and metadata."""
import logging
import os
import uuid
import requests
from sheets_api import SHEET_ID, _session, read_value_ranges


def probe(app):
    with _session(write=True) as session:
        base = 'https://sheets.googleapis.com/v4/spreadsheets/'
        meta = session.get(base + SHEET_ID, params={'fields': 'sheets.properties'}, timeout=10).json()
        sid = next(s['properties']['sheetId'] for s in meta['sheets'] if s['properties']['title'] == '_WebsiteReports')
        rows = read_value_ranges(session, SHEET_ID, ["'_WebsiteReports'!A2:C"])[0]['values']
        index, row = next((i + 1, r) for i, r in reversed(list(enumerate(rows))) if r and str(r[0]).startswith('diagnostic-'))
        checks = [('cells', base + SHEET_ID + ':batchUpdate', {'requests': [{'updateCells': {
            'start': {'sheetId': sid, 'rowIndex': index, 'columnIndex': 2},
            'rows': [{'values': [{'userEnteredValue': {'stringValue': row[2]}}]}], 'fields': 'userEnteredValue'}}]}),
            ('values_batch', base + SHEET_ID + '/values:batchUpdate', {'valueInputOption': 'RAW',
             'data': [{'range': "'_WebsiteReports'!C" + str(index + 1), 'values': [[row[2]]]}]})]
        book = os.environ.get('QUEST_SPREADSHEET_ID')
        if book:
            key = 'owlet_probe_' + uuid.uuid4().hex
            checks.append(('other_book_lock', base + book + ':batchUpdate', {'requests': [
                {'createDeveloperMetadata': {'developerMetadata': {'metadataKey': key, 'metadataValue': 'probe',
                    'location': {'spreadsheet': True}, 'visibility': 'DOCUMENT'}}},
                {'deleteDeveloperMetadata': {'dataFilter': {'developerMetadataLookup': {'metadataKey': key}}}}]}))
        for name, url, data in checks:
            try:
                response = session.post(url, json=data, timeout=(5, 8))
                logging.warning('SHEETS_WRITE_PATH name=%s status=%s', name, response.status_code)
            except requests.RequestException as exc:
                logging.warning('SHEETS_WRITE_PATH name=%s error=%s', name, type(exc).__name__)
