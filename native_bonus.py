"""Внеочередная выдача артефактов: перенос ключей кампании из колонки I."""
import random
import re

from sheets_api import _session
from website_store import Sheets, ARTIFACTS_ID, stamp, append, update, RARITIES


def award(candidates, threshold, rarity, campaign, preview=True):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', campaign):
        raise ValueError('Некорректное имя кампании')
    if rarity not in {r[0] for r in RARITIES}:
        raise ValueError('Некорректная редкость')
    if candidates.get('unresolved') and not preview:
        raise ValueError('Есть участники без однозначного VK ID')
    winners = candidates.get('eligible', [])
    week = str(candidates['weekKey'])
    ids = [str(p['vkId']) for p in winners]
    if len(ids) != len(set(ids)): raise ValueError('Повтор VK ID среди получателей')
    with _session(write=not preview) as session:
        api = Sheets(session)
        # Read-only preview не создаёт ни листов, ни метаданных.
        if preview:
            rows = api.values(ARTIFACTS_ID, ["'Инвентарь'!A2:I"])[0]
            seen = {key.strip() for r in rows if len(r) > 8 for key in str(r[8]).splitlines() if key.strip()}
            return dict(success=True, mode='preview', week=week, qualified=len(winners),
                alreadyAwarded=sum(f'bonus::{week}::{campaign}::{i}' in seen for i in ids),
                unresolved=candidates.get('unresolved', []), winners=winners)
        with api.locked(ARTIFACTS_ID):
            inventory, catalog = api.values(ARTIFACTS_ID, ["'Инвентарь'!A2:I", "'Артефакты'!A2:E"])
            choices = [r for r in catalog if len(r) > 2 and str(r[1]).strip().lower().replace('ё', 'е') == rarity]
            if not choices: raise ValueError('Нет артефактов выбранной редкости')
            rows = [list(r) + [''] * max(0, 9 - len(r)) for r in inventory]
            seen = {key.strip() for r in rows for key in str(r[8]).splitlines() if key.strip()}
            sid = api.tabs(ARTIFACTS_ID)['Инвентарь']['sheetId']
            result, writes, duplicates = [], [], 0
            for p in winners:
                vk = str(p['vkId']); key = f'bonus::{week}::{campaign}::{vk}'
                if key in seen: duplicates += 1; continue
                art = random.SystemRandom().choice(choices)
                matches = [(i, r) for i, r in enumerate(rows) if str(r[0]) == vk and str(r[2]) == str(art[0])]
                if len(matches) > 1: raise ValueError('Неоднозначная строка инвентаря')
                if matches:
                    index, row = matches[0]
                    row[5] = float(row[5] or 0) + 1; row[7] = stamp()
                    row[8] = '\n'.join(filter(None, [str(row[8]).strip(), key]))
                    writes.append(update(sid, index + 1, 5, row[5:9]))
                else:
                    row = [vk, str(p.get('name', '')), str(art[0]), str(art[2]), rarity, 1, stamp(), stamp(), key]
                    rows.append(row); writes.append(append(sid, row))
                seen.add(key)
                result.append(dict(vkId=vk, name=p.get('name', ''), fact=p.get('fact'),
                    artifact=str(art[2]), artifactId=str(art[0]), rarity=rarity))
            if writes: api.batch(ARTIFACTS_ID, [update(sid, 0, 8, ['Бонусные выдачи'])] + writes)
            return dict(success=True, mode='award', week=week, x=threshold, rarity=rarity,
                campaign=campaign, qualified=len(winners), awardedCount=len(result),
                duplicateCount=duplicates, awarded=result)
