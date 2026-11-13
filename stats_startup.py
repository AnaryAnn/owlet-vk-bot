"""Read-only cold-start check; never alters reports, locks or rewards."""
import logging
from sheets_api import read_current_stats, read_stats_history


def check():
    try:
        stats = read_current_stats()
        history = read_stats_history()
        logging.warning('STATS_STARTUP_OK records=%s weeks=%s',
                        len(stats['currentRecords']), len(history['weeks']))
    except Exception:
        logging.exception('STATS_STARTUP_FAILED')
