"""Коллективные квесты. Состояние хранится через Google Sheets API."""
import os
import requests



def quest_api(action, **kwargs):
    from native_quests import dispatch
    return dispatch(action, **kwargs)


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
    if not dry_run:
        from quest_polls import refresh
        refresh(peer_id, quest_api)
    payload = quest_api('tick', fact=total_fact, percent=percent, dryRun=bool(dry_run))
    if dry_run:
        return payload
    sent = []
    for item in payload.get('outbox', []):
        if item.get('peerId') and str(item['peerId']) != str(peer_id):
            continue
        if item.get('poll'):
            from quest_polls import publish
            publish(item, quest_api)
        else:
            vk_sender(peer_id, item['text'])
        quest_api('ack', eventId=item['eventId'])
        sent.append(item['eventId'])
    return {'success': True, 'fact': total_fact, 'percent': percent, 'sent': sent, 'pending': len(payload.get('outbox', []))}

