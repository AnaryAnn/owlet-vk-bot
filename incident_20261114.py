"""One-time statistics unlock explicitly requested by the owner on 2026-11-14.

No report replay and no deletion of rows, journals, votes or rewards.
The persistent marker prevents this incident repair from running twice.
"""
import json
import logging
import time
from datetime import datetime, timezone

from sheets_api import _session, SHEET_ID
from website_store import Sheets, STATS_LOCK_BOOK, STATS_LOCK_ID, STATS_LOCK_KEY

MARKER_ID = 1937152499
MARKER_KEY = 'owlet_incident_20261114_stats_unlock'


def repair(api):
    if SHEET_ID != '1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60':
        raise RuntimeError('Unexpected statistics workbook; incident repair refused')
    if STATS_LOCK_BOOK != '1k2cIfXXyCGIrTzUnodYR9STXNkR00LhDzI60mpCVaXw':
        raise RuntimeError('Unexpected lock workbook; incident repair refused')
    metadata = api.get(STATS_LOCK_BOOK, fields='developerMetadata').get('developerMetadata', [])
    if any(m.get('metadataId') == MARKER_ID for m in metadata):
        return {'success': True, 'alreadyRepaired': True}
    lock = next((m for m in metadata if m.get('metadataId') == STATS_LOCK_ID
                 and m.get('metadataKey') == STATS_LOCK_KEY), None)
    # Read-only evidence is retained in the marker, not personal rows in logs.
    tabs = api.tabs(SHEET_ID)
    rows = api.values(SHEET_ID, ["'Участники'!A:C"])[0]
    reserved = [i + 1 for i, r in enumerate(rows) if r and str(r[0]).startswith('reserved-')]
    reports = api.values(SHEET_ID, ["'_WebsiteReports'!A:B"])[0] if '_WebsiteReports' in tabs else []
    affected = ['vk-7962df603f2532ca3728732558902ec989d024ca',
                'vk-139f4f56dfb320d3ace91966df6d4b116f2adb86',
                'vk-7c7dd0759cfe4067ff02eaf3d79f87d6c89fee37']
    audit = {'incident': '20261114-145439', 'authorized': True,
             'at': datetime.now(timezone.utc).isoformat(), 'reservedParticipantRows': reserved,
             'affectedReportsPresent': [rid for rid in affected if any(r and r[0] == rid for r in reports)],
             'lockPresent': bool(lock)}
    current = api.get(STATS_LOCK_BOOK, fields='developerMetadata').get('developerMetadata', [])
    current_lock = next((m for m in current if m.get('metadataId') == STATS_LOCK_ID), None)
    if current_lock != lock:
        raise RuntimeError('Lock changed during incident inspection; nothing removed')
    writes = []
    if lock:
        writes.append({'deleteDeveloperMetadata': {'dataFilter': {'developerMetadataLookup': {
            'metadataId': STATS_LOCK_ID, 'metadataKey': STATS_LOCK_KEY,
            'metadataValue': lock['metadataValue']}}}})
    writes.append({'createDeveloperMetadata': {'developerMetadata': {
        'metadataId': MARKER_ID, 'metadataKey': MARKER_KEY,
        'metadataValue': json.dumps(audit, sort_keys=True),
        'location': {'spreadsheet': True}, 'visibility': 'DOCUMENT'}}})
    api.batch(STATS_LOCK_BOOK, writes)
    remaining = api.get(STATS_LOCK_BOOK, fields='developerMetadata').get('developerMetadata', [])
    if lock and any(m.get('metadataId') == STATS_LOCK_ID and
                    m.get('metadataValue') == lock['metadataValue'] for m in remaining):
        raise RuntimeError('Incident lock removal not confirmed')
    # Confirm new writes can acquire and release their own lock.
    with api.locked(SHEET_ID):
        pass
    logging.warning('INCIDENT_20261114_REPAIRED %s', json.dumps(audit, sort_keys=True))
    return dict(success=True, audit=audit)


def run():
    # The old Gunicorn worker must finish draining before the stale lock is removed.
    time.sleep(45)
    if datetime.now(timezone.utc) >= datetime(2026, 11, 15, tzinfo=timezone.utc):
        return
    try:
        with _session(write=True) as session:
            repair(Sheets(session))
    except Exception:
        logging.exception('INCIDENT_20261114_REPAIR_FAILED')
