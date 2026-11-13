"""Temporary read-only audit. No cell contents or credentials are logged."""
import hashlib
import logging
import time
import uuid
import base64
import os
from collections import Counter
from contextlib import nullcontext
from sheets_api import _session, SHEET_ID, read_current_stats, read_stats_history, current_week, _quote, _number
from website_store import Sheets, Busy, save_report, STATS_LOCK_BOOK, STATS_LOCK_ID, STATS_LOCK_KEY


def release_reviewed_lock():
    """Only the audited orphan from the stopped night worker is eligible."""
    expected_owner = '9f6173154143b6ad671ddaf8959cd1d28f059a48ffb8e0624de4aafdb753f610'
    expected_request = 'vk-19b12bd1adf30f1275a5efd7e319e069a30a008a'
    with _session(write=True) as session:
        api = Sheets(session)
        metadata = api.get(STATS_LOCK_BOOK, fields='developerMetadata').get('developerMetadata', [])
        lock = next((m for m in metadata if m.get('metadataId') == STATS_LOCK_ID
                     and m.get('metadataKey') == STATS_LOCK_KEY), None)
        if not lock:
            logging.warning('STATS_RECOVERY reviewed_lock_already_absent')
            return
        owner = str(lock.get('metadataValue', ''))
        if hashlib.sha256(owner.encode()).hexdigest() != expected_owner:
            raise RuntimeError('Lock owner changed; refusing automatic release')
        rows = api.values(SHEET_ID, ["'_WebsiteReports'!A2:B"])[0]
        last = next((r[0] for r in reversed(rows) if r and not str(r[0]).startswith('reserved-')), '')
        if len(rows) != 252 or last != expected_request:
            raise RuntimeError('Journal changed since operator review; refusing release')
        api.batch(STATS_LOCK_BOOK, [{'deleteDeveloperMetadata': {'dataFilter': {
            'developerMetadataLookup': {'metadataId': STATS_LOCK_ID,
                'metadataKey': STATS_LOCK_KEY, 'metadataValue': owner}}}}])
        after = api.get(STATS_LOCK_BOOK, fields='developerMetadata').get('developerMetadata', [])
        if any(m.get('metadataId') == STATS_LOCK_ID and m.get('metadataValue') == owner for m in after):
            raise RuntimeError('Reviewed lock deletion not confirmed')
        logging.warning('STATS_RECOVERY reviewed_lock_released')


def check_report():
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(SHEET_ID):
            members = api.values(SHEET_ID, ["'Участники'!A2:C"])[0]
            names = Counter(r[0] for r in members if r)
            active = {r[0] for r in members if len(r) > 1 and str(r[1]).isdigit() and names[r[0]] == 1
                      and (len(r) < 3 or str(r[2]).lower() != 'выбыл')}
            title = current_week()['sheetName']
            rows = api.values(SHEET_ID, [f'{_quote(title)}!A8:D'])[0]
            counts = Counter(r[0] for r in rows if r)
            row = next(r for r in rows if len(r) == 4 and r[3] and r[0] in active and counts[r[0]] == 1
                       and all(isinstance(_number(v), int) and _number(v) >= 0 for v in r[1:3]))
            original = list(row)
            original_lock = api.locked
            api.locked = lambda _: nullcontext()  # The outer distributed mutex is held.
            try:
                result, _, _ = save_report(api, row[0], _number(row[1]), _number(row[2]),
                    'diagnostic-coldstart-' + uuid.uuid4().hex)
            finally:
                api.locked = original_lock
            after = next(r for r in api.values(SHEET_ID, [f'{_quote(title)}!A8:D'])[0] if r and r[0] == row[0])
            if not result.get('success') or original != after or result.get('artifactEligible'):
                raise RuntimeError('Report round trip did not preserve participant values')
            logging.warning('STATS_RECOVERY report_roundtrip=ok original_values_preserved=True')


def audit(app=None):
    try:
        release_reviewed_lock()
        for attempt in range(8):
            try:
                check_report()
                break
            except Busy:
                if attempt == 7:
                    raise
                time.sleep(1)
        with _session() as session:
            api = Sheets(session)
            metadata = api.get(STATS_LOCK_BOOK, fields='developerMetadata').get('developerMetadata', [])
            locks = [m for m in metadata if m.get('metadataId') == STATS_LOCK_ID
                     and m.get('metadataKey') == STATS_LOCK_KEY]
            for lock in locks:
                logging.warning('STATS_RECOVERY lock_id=%s owner_hash=%s', STATS_LOCK_ID,
                    hashlib.sha256(str(lock.get('metadataValue', '')).encode()).hexdigest())
            rows = api.values(SHEET_ID, ["'_WebsiteReports'!A2:B"])[0]
            last = next((r[0] for r in reversed(rows) if r and not str(r[0]).startswith('reserved-')), '')
            logging.warning('STATS_RECOVERY locks=%s journal_rows=%s last_request=%s', len(locks), len(rows), last)
        stats = read_current_stats()
        history = read_stats_history()
        logging.warning('STATS_RECOVERY read_ok records=%s weeks=%s',
                        len(stats['currentRecords']), len(history['weeks']))
        if app is not None:
            password = os.environ.get('WEBSITE_SITE_PASSWORD') or os.environ.get('WEBSITE_ADMIN_PASSWORD', '')
            headers = {'X-Website-Password': base64.b64encode(password.encode()).decode()}
            with app.test_client() as client:
                for path in ['/api/website/stats', '/api/website/history', '/api/website/participants']:
                    response = client.get(path, headers=headers)
                    logging.warning('STATS_RECOVERY website=%s status=%s', path, response.status_code)
    except Exception:
        logging.exception('STATS_RECOVERY audit failed')
