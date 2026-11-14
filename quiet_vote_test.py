"""Owner-triggered transport test, isolated from real quest votes."""
import hashlib
import hmac
import json
import logging
import time

PEER_ID = 2000000001
COMMAND = 'тест тихого голосования'


def signature(token, expires):
    return hmac.new(token.encode(), f'quiet-vote-test:{PEER_ID}:{expires}'.encode(),
                    hashlib.sha256).hexdigest()


def publish(send, token, message_id):
    expires = int(time.time()) + 86400
    payload = dict(kind='quiet_vote_test', expires=expires, signature=signature(token, expires))
    buttons = []
    for choice, label in enumerate(('Ух!', 'Ух-хух!'), 1):
        buttons.append([{'action': {'type': 'callback', 'label': label,
            'payload': json.dumps(dict(payload, choice=choice), separators=(',', ':'))}, 'color': 'primary'}])
    send(PEER_ID, '🦉 Проверка тихого голосования. Нажми кнопку: сообщение от твоего имени '
         'в чате появляться не должно. Подтверждение придёт возле кнопки и в ЛС '
         '(если сообщения сообщества разрешены). Это тест, на квесты и награды он не влияет.',
         keyboard=dict(inline=True, one_time=False, buttons=buttons),
         random_id=f'quiet-vote-test:{message_id}')
    logging.warning('QUIET_VOTE_TEST published peer_id=%s', PEER_ID)


def handle(event, token, feedback):
    payload = event.get('payload') or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return False
    if not isinstance(payload, dict) or payload.get('kind') != 'quiet_vote_test':
        return False
    try:
        expires = int(payload.get('expires') or 0)
        user, peer = int(event.get('user_id') or 0), int(event.get('peer_id') or 0)
        choice = int(payload.get('choice') or 0)
        event_id = event.get('event_id')
        valid = (bool(token) and peer == PEER_ID and user > 0 and choice in (1, 2)
                 and isinstance(event_id, str) and bool(event_id)
                 and time.time() < expires <= time.time() + 86460
                 and hmac.compare_digest(str(payload.get('signature') or ''), signature(token, expires)))
    except (ValueError, TypeError):
        valid = False
    if valid:
        logging.warning('QUIET_VOTE_TEST received user_id=%s choice=%s', user, choice)
        feedback(user, peer, event_id, f'🗳 Тестовый голос принят: вариант {choice}.')
    return True
