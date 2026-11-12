"""One-off, read-only production probes. Never log cell contents or credentials."""
import logging
import time
from urllib.parse import quote

from sheets_api import SHEET_ID, _session, current_week


def probe():
    base = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
    week = current_week()['sheetName']
    checks = [
        ('metadata', base, {'fields': 'sheets.properties'}),
        ('participants_single', base + '/values/' + quote("'Участники'!A2:C3", safe=''), {}),
        ('participants_batch', base + '/values:batchGet', {'ranges': "'Участники'!A2:C3"}),
        ('participants_grid', base, {'ranges': "'Участники'!A2:C3", 'includeGridData': 'true',
                                   'fields': 'sheets.data.rowData.values.userEnteredValue'}),
        ('week_single', base + '/values/' + quote(f"'{week}'!A8:D9", safe=''), {}),
    ]
    with _session() as session:
        for name, url, params in checks:
            started = time.monotonic()
            try:
                response = session.get(url, params=params, timeout=(5, 8))
                response.raise_for_status()
                data = response.json()
                extra = {}
                if name == 'metadata':
                    props = [s['properties'] for s in data.get('sheets', [])]
                    extra = {'tabs': len(props), 'cells': sum(
                        p.get('gridProperties', {}).get('rowCount', 0) *
                        p.get('gridProperties', {}).get('columnCount', 0) for p in props)}
                logging.warning('SHEETS_PROBE name=%s status=%s seconds=%.2f details=%s',
                                name, response.status_code, time.monotonic()-started, extra)
            except Exception as exc:
                logging.warning('SHEETS_PROBE name=%s error=%s seconds=%.2f',
                                name, type(exc).__name__, time.monotonic()-started)
