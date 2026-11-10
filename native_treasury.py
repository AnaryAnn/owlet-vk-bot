"""Контракт Сокровищница.gs: показывать только уже найденные карточки."""
import re

from sheets_api import _session
from website_store import ARTIFACTS_ID, Sheets

PREFIX = 'https://res.cloudinary.com/jlcfrihy/image/upload/'


def valid_id(value):
    try:
        n = float(value)
        return int(n) if n.is_integer() and 1 <= n <= 200 else None
    except (TypeError, ValueError, OverflowError):
        return None


def count(value):
    try:
        n = float(value)
        return n if 0 < n < float('inf') else 0
    except (TypeError, ValueError):
        return 0


def build(action, vk_id, inventory, catalog):
    if action == 'participants':
        people = {}
        for row in inventory:
            row = list(row) + [''] * max(0, 6 - len(row))
            owner, name = str(row[0]).strip(), str(row[1]).strip()
            if not re.fullmatch(r'\d{1,20}', owner) or not valid_id(row[2]) or not count(row[5]):
                continue
            if owner not in people or (name and people[owner]['name'].startswith('Сычик ')):
                people[owner] = {'vkId': owner, 'name': name or 'Сычик ' + owner}
        return {'ok': True, 'participants': sorted(people.values(), key=lambda p: p['name'])}
    if action != 'collection' or not re.fullmatch(r'\d{1,20}', str(vk_id)):
        raise ValueError('Некорректный запрос сокровищницы')
    cards, mine, all_ids = {}, {}, set()
    for row in catalog:
        row = list(row) + [''] * max(0, 6 - len(row))
        ident = valid_id(row[0])
        if ident:
            image = str(row[5]).strip()
            image = image if image.startswith(PREFIX) else ''
            cards[ident] = dict(id=ident, rarity=str(row[1]), name=str(row[2]),
                description=str(row[3]), hasImage=bool(image), imageUrl=image)
    for row in inventory:
        row = list(row) + [''] * max(0, 6 - len(row))
        ident, quantity = valid_id(row[2]), count(row[5])
        if ident not in cards or not quantity:
            continue
        all_ids.add(ident)
        if str(row[0]).strip() == str(vk_id):
            mine[ident] = mine.get(ident, 0) + quantity
    personal = [dict(cards[i], count=mine[i]) for i in sorted(mine)]
    common = [cards[i] for i in sorted(all_ids)]
    return {'ok': True, 'total': 200,
        'personal': {'unique': len(personal), 'cards': personal},
        'common': {'unique': len(common), 'cards': common}}


def read(action, vk_id='0'):
    with _session() as session:
        inventory, catalog = Sheets(session).values(ARTIFACTS_ID,
            ["'Инвентарь'!A2:H", "'Артефакты'!A2:F"])
    return build(action, vk_id, inventory, catalog)
