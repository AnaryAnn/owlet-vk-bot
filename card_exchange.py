"""Private duplicate-card exchanges and VK keyboards."""
import hashlib
import hmac
import json
import logging
import random
import re
import time
import threading
from uuid import uuid4

from native_treasury import PREFIX, count, read, valid_id
from sheets_api import _session
from website_store import ARTIFACTS_ID, RARITIES, Busy, Conflict, Sheets, Uncertain, append, stamp, update

RARITY_ORDER = [rarity for rarity, _, _ in RARITIES]
PAGE_SIZE = 6
_PENDING = set()
_PENDING_LOCK = threading.Lock()


def rarity_key(value):
    return str(value).strip().casefold().replace('ё', 'е')


def target_rarity(rarity, cost):
    if rarity not in RARITY_ORDER or cost not in (1, 2):
        return None
    target = RARITY_ORDER.index(rarity) + cost - 1
    return RARITY_ORDER[target] if target < len(RARITY_ORDER) else None


def exchange(peer_id, card_id, cost, request_id, rng=None):
    owner = str(peer_id)
    rng = rng or random.SystemRandom()
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(ARTIFACTS_ID):
            journal_id = api.ensure_tab(ARTIFACTS_ID, '_ArtifactExchanges', ['id', 'owner', 'result'])
            tabs = api.tabs(ARTIFACTS_ID)
            journal, inventory, catalog = api.values(ARTIFACTS_ID, [
                "'_ArtifactExchanges'!A2:C", "'Инвентарь'!A2:H", "'Артефакты'!A2:F"])
            for row in journal:
                if row and row[0] == request_id:
                    if str(row[1]) != owner:
                        raise Conflict('Не удалось подтвердить владельца обмена.')
                    return json.loads(row[2])

            cards = {}
            for row in catalog:
                ident = valid_id(row[0]) if row else None
                if ident and len(row) >= 3 and str(row[2]).strip():
                    if ident in cards:
                        raise Conflict('В каталоге есть повторяющиеся номера карточек.')
                    cards[ident] = list(row) + [''] * max(0, 6 - len(row))
            owned = {}
            for index, row in enumerate(inventory, 1):
                if len(row) < 3 or str(row[0]).strip() != owner:
                    continue
                ident = valid_id(row[2])
                if ident is None:
                    continue
                owned.setdefault(ident, []).append((index, list(row) + [''] * max(0, 8 - len(row))))

            def quantity_of(rows):
                quantities = [count(row[5]) for _, row in rows]
                if any(value != int(value) for value in quantities):
                    raise Conflict('В коллекции есть некорректное число копий.')
                return sum(int(value) for value in quantities)

            source = cards.get(card_id)
            source_rows = owned.get(card_id, [])
            if not source or not source_rows:
                raise ValueError('Этой карточки уже нет в коллекции.')
            quantity = quantity_of(source_rows)
            target = target_rarity(rarity_key(source[1]), cost)
            if not target or quantity < cost + 1:
                raise ValueError('Этот вариант обмена уже недоступен. Открой меню обмена заново.')
            candidates = [ident for ident, card in cards.items() if rarity_key(card[1]) == target]
            if not candidates:
                raise ValueError('В каталоге нет карточек нужной редкости.')
            missing = [ident for ident in candidates if not any(
                count(row[5]) for _, row in owned.get(ident, []))]
            reward_id = rng.choice(missing or candidates)
            reward = cards[reward_id]
            inventory_id = tabs['Инвентарь']['sheetId']
            now = stamp()
            writes = []
            remaining = cost - (reward_id == card_id)
            for index, row in reversed(source_rows):
                if not remaining:
                    break
                copies = int(count(row[5]))
                spent = min(copies, remaining)
                if spent:
                    writes.append(update(inventory_id, index, 5, [copies - spent, row[6], now]))
                    remaining -= spent
            if reward_id != card_id:
                reward_rows = owned.get(reward_id, [])
                if reward_rows:
                    quantity_of(reward_rows)
                    index, row = reward_rows[0]
                    writes.append(update(inventory_id, index, 5, [int(count(row[5])) + 1, row[6], now]))
                else:
                    writes.append(append(inventory_id, [owner, source_rows[0][1][1], str(reward_id),
                                                        str(reward[2]), str(reward[1]), 1, now, now]))
            photo = str(reward[4]).strip()
            image = str(reward[5]).strip()
            result = dict(id=reward_id, name=str(reward[2]), rarity=target,
                          photo=photo if re.fullmatch(r'photo-?\d+_\d+(?:_[\w-]+)?', photo) else '',
                          image=image if image.startswith(PREFIX) else '')
            writes.append(append(journal_id, [request_id, owner, json.dumps(result, ensure_ascii=False)]))
            api.batch(ARTIFACTS_ID, writes)
            return result


def button(label, **payload):
    return {'action': {'type': 'text', 'label': label,
                       'payload': json.dumps(payload, ensure_ascii=False)}, 'color': 'secondary'}


def menu():
    return {'one_time': False, 'buttons': [[button('Моя коллекция'), button('Обмен')], [button('Внести данные'), button('Квест')], [button('Мой прогресс')], [button('Подарить повтор'), button('У меня есть промокод')]]}


def signature(secret, peer_id, card_id, cost, nonce, expiry):
    data = f'{peer_id}:{card_id}:{cost}:{nonce}:{expiry}'.encode()
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()[:24]


def complete_exchange(peer, card_id, cost, nonce, send):
    with _PENDING_LOCK:
        if nonce in _PENDING:
            return
        _PENDING.add(nonce)
    try:
        deadline = time.monotonic() + 300
        notified = False
        while True:
            try:
                reward = exchange(peer, card_id, cost, nonce)
                break
            except (Busy, Uncertain) as exc:
                logging.warning('CARD_EXCHANGE_RETRY request_id=%s error=%s', nonce, type(exc).__name__)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    send(peer, 'Таблица всё ещё занята. Нажми ту же кнопку, чтобы проверить результат.', keyboard=menu())
                    return
                if not notified:
                    try:
                        send(peer, '🔄 Обмен подтверждён. Таблица занята, повторю попытку автоматически.',
                             random_id=f'card-exchange-pending:{peer}:{nonce}')
                    except Exception:
                        logging.warning('CARD_EXCHANGE_PROGRESS_SEND_FAILED request_id=%s', nonce)
                    notified = True
                time.sleep(min(remaining, 6))
        summary = f"🎁 Обмен завершён. Получена карточка №{reward['id']} «{reward['name']}» · {reward['rarity']}"
        if not reward['photo'] and reward['image']:
            summary += '\n' + reward['image']
        send(peer, summary, attachment=reward['photo'] or None,
             keyboard=menu(), random_id=f'card-exchange:{peer}:{nonce}')
        logging.warning('CARD_EXCHANGE_DONE request_id=%s', nonce)
    finally:
        with _PENDING_LOCK:
            _PENDING.discard(nonce)


def handle_message(message, send, secret):
    peer = int(message['peer_id'])
    text = ' '.join(str(message.get('text') or '').casefold().split())
    payload = message.get('payload')
    try:
        payload = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    action = payload.get('card_exchange')
    if text in ('начать', 'меню') or payload.get('command') == 'start':
        send(peer, '🦉 Выбери, что открыть.', keyboard=menu())
        return True
    if text != 'обмен' and action not in ('page', 'card', 'quote', 'confirm'):
        return False

    def inline(text, rows):
        send(peer, text, keyboard={'inline': True, 'buttons': rows})

    try:
        if action == 'confirm':
            card_id, cost = payload.get('id'), payload.get('cost')
            nonce, expiry = payload.get('nonce'), payload.get('expiry')
            if (type(card_id) is not int or valid_id(card_id) is None
                    or type(cost) is not int or cost not in (1, 2)
                    or not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{32}', nonce)
                    or type(expiry) is not int or expiry < time.time()
                    or not hmac.compare_digest(str(payload.get('sig', '')),
                                               signature(secret, peer, card_id, cost, nonce, expiry))):
                raise ValueError('Подтверждение устарело. Открой «Обмен» заново.')
            complete_exchange(peer, card_id, cost, nonce, send)
            return True

        cards = read('collection', str(peer))['personal']['cards']
        duplicates = [card for card in cards if card['count'] >= 2 and
                      target_rarity(rarity_key(card['rarity']), 1)]
        if action in ('card', 'quote'):
            card_id = payload.get('id')
            source = next((card for card in duplicates if card['id'] == card_id), None)
            if source is None:
                raise ValueError('Эту карточку сейчас нельзя обменять.')
            rarity = rarity_key(source['rarity'])
            options = {cost: target_rarity(rarity, cost) for cost in (1, 2)
                       if source['count'] >= cost + 1 and target_rarity(rarity, cost)}
            if action == 'card':
                inline(f"№{card_id} «{source['name']}» ×{source['count']:g}. Выбери вариант обмена.", [
                    [button('1 копия · та же редкость' if cost == 1 else '2 копии · редкость выше',
                            card_exchange='quote', id=card_id, cost=cost)] for cost in options])
            else:
                cost = payload.get('cost')
                if type(cost) is not int or cost not in options:
                    raise ValueError('Этот вариант обмена сейчас недоступен.')
                nonce = uuid4().hex
                expiry = int(time.time()) + 900
                inline(f"Спишется {cost} из {source['count']:g} копий карточки «{source['name']}». "
                       f"Редкость случайной награды: {options[cost]}.", [
                    [button('Подтвердить обмен', card_exchange='confirm', id=card_id, cost=cost,
                            nonce=nonce, expiry=expiry,
                            sig=signature(secret, peer, card_id, cost, nonce, expiry))],
                    [button('Назад', card_exchange='card', id=card_id)]])
            return True

        if text == 'обмен' and action is None:
            send(peer, '🔄 Обмен повторных карточек', keyboard=menu())
        if not duplicates:
            send(peer, 'Пока нет повторных карточек для обмена.', keyboard=menu())
            return True
        page = payload.get('page', 0)
        if type(page) is not int:
            page = 0
        last_page = (len(duplicates) - 1) // PAGE_SIZE
        page = min(last_page, max(0, page))
        visible = duplicates[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        rows = [[button(f"№{card['id']} ×{card['count']:g}", card_exchange='card', id=card['id'])]
                for card in visible]
        navigation = []
        if page > 0:
            navigation.append(button('← Назад', card_exchange='page', page=page - 1))
        if page < last_page:
            navigation.append(button('Дальше →', card_exchange='page', page=page + 1))
        if navigation:
            rows.append(navigation)
        inline('Выбери карточку для обмена:\n' + '\n'.join(
            f"№{card['id']} · {card['name']} · {card['rarity']} ×{card['count']:g}" for card in visible), rows)
    except (Conflict, ValueError) as exc:
        send(peer, str(exc), keyboard=menu())
    except (Busy, Uncertain):
        send(peer, 'Таблица сейчас занята. Нажми ту же кнопку подтверждения ещё раз.', keyboard=menu())
    return True
