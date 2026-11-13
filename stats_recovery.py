"""Temporary read-only audit. No cell contents or credentials are logged."""
import hashlib
import logging
from sheets_api import _session, SHEET_ID, read_current_stats, read_stats_history
from website_store import Sheets, STATS_LOCK_BOOK, STATS_LOCK_ID, STATS_LOCK_KEY


def audit():
    try:
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
    except Exception:
        logging.exception('STATS_RECOVERY audit failed')
