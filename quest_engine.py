"""Phase 1: collective quests. Separate Google Apps Script storage."""
import os
import requests

QUEST_URL = os.environ.get('QUEST_SCRIPT_URL', '').strip()
QUEST_PASSWORD = os.environ.get('QUEST_SCRIPT_PASSWORD', '').strip()


def quest_api(action, **kwargs):
    if not QUEST_URL or not QUEST_PASSWORD:
        raise RuntimeError('QUEST_SCRIPT_URL / QUEST_SCRIPT_PASSWORD not configured')
    response = requests.post(QUEST_URL, json={'action': action, 'password': QUEST_PASSWORD, **kwargs}, timeout=(5, 30))
    response.raise_for_status()
    data = response.json()
    if not data.get('success'):
        raise RuntimeError(data.get('error', 'Quest API failed'))
    return data


def tick(stats_loader, vk_sender, peer_id, dry_run=False):
    """Advance collective quests; never use a local in-memory state as source of truth.

    Pending announcements remain in the outbox until acknowledged. A network failure
    after VK accepted a message can still duplicate it; see README for production notes.
    """
    stats = stats_loader({'action': 'stats'})
    if not stats.get('success'):
        raise RuntimeError(stats.get('error', 'Stats API error'))
    records = stats.get('currentRecords') or []
    total_fact = sum(float(r.get('fact') or 0) for r in records)
    goal = stats.get('teamGoal')
    percent = 100 * total_fact / float(goal) if goal not in (None, '', 0, '0') else None
    payload = quest_api('tick', fact=total_fact, percent=percent, dryRun=bool(dry_run))
    if dry_run:
        return payload
    sent = []
    for item in payload.get('outbox', []):
        if item.get('peerId') and str(item['peerId']) != str(peer_id):
            continue
        vk_sender(peer_id, item['text'])
        quest_api('ack', eventId=item['eventId'])
        sent.append(item['eventId'])
    return {'success': True, 'fact': total_fact, 'percent': percent, 'sent': sent, 'pending': len(payload.get('outbox', []))}
