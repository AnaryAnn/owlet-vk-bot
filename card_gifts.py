"""One-time duplicate-card gifts redeemed in private VK messages."""
import hashlib
import hmac
import json
import logging
import re

from card_exchange import button, menu
from native_storage import stats_get
from native_treasury import count, read, valid_id
from sheets_api import _session
from website_store import ARTIFACTS_ID, Busy, Sheets, Uncertain, append, stamp, update

TAB = '_CardGifts'
CODE_RE = re.compile(r'(?:подарок\s+)?(СЫЧ-[A-F0-9]{20})', re.IGNORECASE)


def code_hash(code):
    return hashlib.sha256(code.upper().encode()).hexdigest()


def gift_code(secret, peer, event_id):
    key = f'card-gift:{peer}:{event_id}'.encode()
    return 'СЫЧ-' + hmac.new(secret.encode(), key, hashlib.sha256).hexdigest()[:20].upper()


def inventory_rows(inventory, peer, card):
    return [(i, list(row) + [''] * max(0, 8 - len(row)))
            for i, row in enumerate(inventory, 1) if len(row) >= 3
            and str(row[0]).strip() == str(peer) and valid_id(row[2]) == card]


def quantities(rows):
    result = [count(row[5]) for _, row in rows]
    if any(value != int(value) for value in result):
        raise ValueError('В инвентаре есть некорректное количество копий.')
    return [int(value) for value in result]


def journal(api):
    if TAB not in api.tabs(ARTIFACTS_ID):
        return []
    return api.values(ARTIFACTS_ID, [f"'{TAB}'!A2:B"])[0]


def locate(rows, digest):
    return next(((i, json.loads(row[1])) for i, row in enumerate(rows, 1)
                 if len(row) >= 2 and row[0] == digest), None)


def make_gift(owner, card, code):
    digest = code_hash(code)
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(ARTIFACTS_ID):
            rows = journal(api)
            found = locate(rows, digest)
            if found:
                if found[1]['owner'] != owner or found[1]['card'] != card:
                    raise ValueError('Этот запрос относится к другому подарку.')
                return found[1]
            inventory, catalog = api.values(ARTIFACTS_ID, ["'Инвентарь'!A2:H", "'Артефакты'!A2:F"])
            source = inventory_rows(inventory, owner, card)
            copies = quantities(source)
            if sum(copies) < 2:
                raise ValueError('Для подарка нужны хотя бы две копии карточки.')
            row = next((r for r in catalog if len(r) >= 3 and valid_id(r[0]) == card), None)
            if row is None:
                raise ValueError('Карточка не найдена в каталоге.')
            photo = str(row[4]).strip() if len(row) > 4 else ''
            now = stamp()
            gift = dict(owner=owner, card=card, name=str(row[2]), rarity=str(row[1]), issued_at=now,
                        photo=photo if re.fullmatch(r'photo-?\d+_\d+(?:_[\w-]+)?', photo) else '')
            index = next(i for i in range(len(source) - 1, -1, -1) if copies[i])
            source_index, old = source[index]
            inventory_id = api.tabs(ARTIFACTS_ID)['Инвентарь']['sheetId']
            tab = api.ensure_tab(ARTIFACTS_ID, TAB, ['code_hash', 'gift_json'])
            api.batch(ARTIFACTS_ID, [
                update(inventory_id, source_index, 5, [copies[index] - 1, old[6], now]),
                append(tab, [digest, json.dumps(gift, ensure_ascii=False)]),
            ])
            return gift


def check_gift(found, peer):
    if not found:
        raise ValueError('Промокод не найден.')
    _, gift = found
    if gift['owner'] == peer:
        raise ValueError('Свой подарок забрать нельзя.')
    recipient = gift.get('recipient')
    if recipient is not None:
        if recipient == peer:
            raise ValueError('Ты уже использовал этот промокод. Карточка в твоей коллекции.')
        raise ValueError('Промокод уже использован.')
    return gift


def redeem(peer, name, digest):
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(ARTIFACTS_ID):
            found = locate(journal(api), digest)
            gift = check_gift(found, peer)
            if not gift.get('issued_at'):
                raise ValueError('Для этого промокода списание не подтверждено.')
            inventory = api.values(ARTIFACTS_ID, ["'Инвентарь'!A2:H"])[0]
            card = gift['card']
            recipient = inventory_rows(inventory, peer, card)
            recipient_quantities = quantities(recipient)
            tabs = api.tabs(ARTIFACTS_ID)
            tab = tabs['Инвентарь']['sheetId']
            now = stamp()
            writes = []
            if recipient:
                target_index, old = recipient[0]
                writes.append(update(tab, target_index, 5, [recipient_quantities[0] + 1, old[6], now]))
            else:
                writes.append(append(tab, [str(peer), name, str(card), gift['name'], gift['rarity'], 1, now, now]))
            gift.update(recipient=peer, redeemed_at=now)
            writes.append(update(tabs[TAB]['sheetId'], found[0], 1, [json.dumps(gift, ensure_ascii=False)]))
            api.batch(ARTIFACTS_ID, writes)
            return gift


def handle_message(message, event_id, send, secret):
    text = ' '.join(str(message.get('text') or '').casefold().split())
    peer = int(message['peer_id'])
    payload = message.get('payload') or {}
    try:
        payload = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        payload = {}
    action = payload.get('gift') if isinstance(payload, dict) else None
    match = CODE_RE.fullmatch(text)
    looks_like_code = bool(re.match(r'^(?:подарок(?:\s|$)|сыч[-–—\s])', text))
    offer = re.fullmatch(r'подарить\s+(\d{1,3})', text)
    if text not in ('подарить повтор', 'у меня есть промокод') and not match and not looks_like_code and not offer and action != 'redeem':
        return False

    try:
        if text == 'у меня есть промокод':
            send(peer, '🎁 Пришли подарочный промокод целиком.', keyboard=menu())
        elif text == 'подарить повтор':
            cards = read('collection', str(peer))['personal']['cards']
            duplicates = [card for card in cards if card['count'] >= 2]
            lines = [f"№{card['id']} · {card['name']} ×{card['count']:g}" for card in duplicates]
            send(peer, ('Напиши «Подарить» и номер карточки. Например: Подарить 64.\n\n' +
                        '\n'.join(lines)) if lines else 'Пока нет повторных карточек для подарка.', keyboard=menu())
        elif offer:
            card = valid_id(offer[1])
            ident = event_id or message.get('conversation_message_id') or message.get('id')
            if not card or not ident:
                raise ValueError('Не удалось определить номер карточки или сообщения.')
            code = gift_code(secret, peer, ident)
            gift = make_gift(peer, card, code)
            if gift.get('recipient') is not None:
                raise ValueError('Этот промокод уже закрыт. Создай новый подарок.')
            send(peer, f'🎁 Одноразовый промокод для карточки №{card}:\n{code}\n\n'
                       'Копия списана из твоей коллекции. Перешли код получателю, чтобы он забрал карточку.',
                 keyboard=menu(), random_id=f'gift-create:{peer}:{ident}')
        elif action == 'redeem':
            digest = payload.get('code_hash')
            if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise ValueError('Некорректный промокод.')
            people = stats_get({'action': 'participants'})['participants']
            names = {p['name'] for p in people if str(p.get('vkId')) == str(peer)
                     and str(p.get('status')).strip().casefold() != 'выбыл'}
            if len(names) != 1:
                raise ValueError('Подарок могут получить участники охоты с привязанным VK-профилем.')
            gift = redeem(peer, names.pop(), digest)
            send(peer, f"🎁 Подарок принят: карточка №{gift['card']} «{gift['name']}».",
                 attachment=gift.get('photo') or None, keyboard=menu(), random_id=f'gift-redeem:{digest}')
        elif match:
            digest = code_hash(match[1])
            with _session() as session:
                gift = check_gift(locate(journal(Sheets(session)), digest), peer)
            send(peer, f"🎁 Принять карточку №{gift['card']}?",
                 keyboard={'inline': True, 'buttons': [[button('Принять подарок', gift='redeem', code_hash=digest)]]})
        else:
            raise ValueError('Неверный формат промокода. Скопируй код целиком, начиная с «СЫЧ-».')
    except ValueError as exc:
        send(peer, f'🦉 {exc}', keyboard=menu())
    except (Busy, Uncertain):
        send(peer, '🦉 Таблица сейчас занята. Повтори запрос или нажми ту же кнопку чуть позже.', keyboard=menu())
    except Exception:
        logging.exception('CARD_GIFT_FAILED')
        send(peer, '🦉 Сейчас подарки недоступны. Попробуй чуть позже.', keyboard=menu())
    return True
