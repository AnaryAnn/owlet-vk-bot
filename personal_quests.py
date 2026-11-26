"""Weekly personal quests and their idempotent card rewards."""
import json
import logging
import math
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from card_exchange import button, menu, rarity_key
from native_storage import stats_get
from native_treasury import PREFIX, count, valid_id
from sheets_api import _session, current_week, read_stats_history
from website_store import ARTIFACTS_ID, Busy, Sheets, Uncertain, append, stamp, update

TAB = '_PersonalQuests'
RANGE = f"'{TAB}'!A2:B"
LEVELS = {'E': ('Лёгкий', 2, 'обычный'), 'M': ('Средний', 5, 'необычный'),
          'H': ('Сложный', 10, 'редкий')}
CATALOG = json.loads(Path(__file__).with_name('personal_quest_catalog.json').read_text(encoding='utf-8'))
HONESTY = ('ПРОТОКОЛ ЧЕСТНОГО ПРИКЛЮЧЕНИЯ! Перед принятием внеси в отчёт всех уже вышитых мышек. '
           'Квест предназначен для новых подвигов. Я тебе доверяю!')


def fact_for(week, name):
    facts = [row['fact'] for row in week.get('records', [])
             if row.get('factEntered') and row['name'].casefold() == name.casefold()]
    if len(facts) > 1:
        raise ValueError('В статистике несколько отчётов с твоим именем. Нужна проверка организатора.')
    return facts[0] if facts else None


def calculate(snapshot, name):
    current = date.fromisoformat(snapshot['currentWeek']['start'])
    cutoff = (current - timedelta(weeks=6)).isoformat()
    history = [fact_for(w, name) for w in snapshot['weeks'] if cutoff <= w['weekStart'] < current.isoformat()]
    history = [fact for fact in history if fact is not None]
    current_record = next((w for w in snapshot['weeks'] if w['weekStart'] == current.isoformat()), {})
    fact = fact_for(current_record, name) or 0
    median_fact = math.ceil(median(history)) if len(history) >= 2 else None
    return median_fact, fact


def rows(api):
    if TAB not in api.tabs(ARTIFACTS_ID):
        return []
    return [(index, row[0], json.loads(row[1])) for index, row in
            enumerate(api.values(ARTIFACTS_ID, [RANGE])[0], 1) if len(row) >= 2 and row[0]]


def encoded(quest):
    return json.dumps(quest, ensure_ascii=False)


def choose_story(level, recent):
    choices = [key for key in CATALOG if key.startswith(level)]
    return random.choice([key for key in choices if key not in recent] or choices)


def save_choice(peer, week, payload, name, median_fact, fact):
    key = f"{peer}:{week['start']}"
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(ARTIFACTS_ID):
            if current_week()['start'] != week['start']:
                raise ValueError('Игровая неделя уже сменилась. Открой «Квест» заново.')
            records = rows(api)
            found = next(((index, q) for index, ident, q in records if ident == key), None)
            if payload['pq'] == 'cancel':
                if not found:
                    raise ValueError('Активного квеста нет.')
                index, quest = found
                if quest['status'] == 'active':
                    quest['status'] = 'cancelled'
                    quest['finished_at'] = stamp()
                    tab = api.tabs(ARTIFACTS_ID)[TAB]['sheetId']
                    api.batch(ARTIFACTS_ID, [update(tab, index, 1, [encoded(quest)])])
                return quest
            if found:
                return found[1]
            level = payload.get('level')
            if level not in LEVELS or median_fact is None:
                raise ValueError('Квест недоступен. Открой меню заново.')
            _, increment, _ = LEVELS[level]
            target = max(median_fact, fact) + increment
            if payload.get('target') != target:
                return None
            recent = [q['story'] for _, _, q in records if q['peer'] == peer][-3:]
            quest = dict(peer=peer, name=name, week=week['start'], end=week['end'],
                         level=level, story=choose_story(level, recent), median_fact=median_fact,
                         accepted_fact=fact, target=target, status='active', accepted_at=stamp())
            tab = api.ensure_tab(ARTIFACTS_ID, TAB, ['quest_key', 'quest_json'])
            api.batch(ARTIFACTS_ID, [append(tab, [key, encoded(quest)])])
            return quest


def award(api, quest):
    inventory, catalog = api.values(ARTIFACTS_ID, ["'Инвентарь'!A2:H", "'Артефакты'!A2:F"])
    rarity = LEVELS[quest['level']][2]
    candidates = {valid_id(row[0]): row for row in catalog if len(row) >= 3
                  and valid_id(row[0]) and str(row[2]).strip() and rarity_key(row[1]) == rarity}
    if not candidates:
        raise ValueError(f'В каталоге нет карточек редкости «{rarity}».')
    owner = str(quest['peer'])
    owned = {}
    for index, row in enumerate(inventory, 1):
        if len(row) >= 6 and str(row[0]).strip() == owner and valid_id(row[2]):
            owned.setdefault(valid_id(row[2]), []).append((index, row))
    options = [ident for ident in candidates if not any(count(row[5]) for _, row in owned.get(ident, []))]
    ident = random.choice(options or list(candidates))
    row = list(candidates[ident]) + [''] * max(0, 6 - len(candidates[ident]))
    tab = api.tabs(ARTIFACTS_ID)['Инвентарь']['sheetId']
    now = stamp()
    if ident in owned:
        index, existing = owned[ident][0]
        quantity = count(existing[5])
        if quantity != int(quantity):
            raise ValueError('Некорректное число копий в инвентаре.')
        first = existing[6] if len(existing) > 6 else now
        write = update(tab, index, 5, [int(quantity) + 1, first, now])
    else:
        write = append(tab, [owner, quest['name'], str(ident), str(row[2]), rarity, 1, now, now])
    photo, image = str(row[4]).strip(), str(row[5]).strip()
    return write, dict(id=ident, name=str(row[2]), rarity=rarity,
                       photo=photo if re.fullmatch(r'photo-?\d+_\d+(?:_[\w-]+)?', photo) else '',
                       image=image if image.startswith(PREFIX) else '')


def finalize(key, historical_week):
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(ARTIFACTS_ID):
            found = next(((index, q) for index, ident, q in rows(api) if ident == key), None)
            if not found:
                return None
            index, quest = found
            if quest['status'] != 'active':
                return quest
            fact = fact_for(historical_week, quest['name']) or 0
            quest.update(status='success' if fact >= quest['target'] else 'failed',
                         final_fact=fact, finished_at=stamp(), delivered=False)
            writes = []
            if quest['status'] == 'success':
                write, quest['reward'] = award(api, quest)
                writes.append(write)
            tab = api.tabs(ARTIFACTS_ID)[TAB]['sheetId']
            writes.append(update(tab, index, 1, [encoded(quest)]))
            api.batch(ARTIFACTS_ID, writes)
            return quest


def mark_delivered(key):
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(ARTIFACTS_ID):
            for index, ident, quest in rows(api):
                if ident == key and not quest.get('delivered'):
                    quest['delivered'] = True
                    tab = api.tabs(ARTIFACTS_ID)[TAB]['sheetId']
                    api.batch(ARTIFACTS_ID, [update(tab, index, 1, [encoded(quest)])])
                    return


def finish_message(quest):
    story = CATALOG[quest['story']]
    title = 'Квест выполнен!' if quest['status'] == 'success' else 'Квест завершён'
    message = f"🦉 {title} «{story['title']}»\n{story[quest['status']]}\n\n"
    message += f"Итог: {quest['final_fact']:g} из {quest['target']:g} мышей."
    reward = quest.get('reward')
    if reward:
        message += f"\n🎁 Карточка №{reward['id']} «{reward['name']}» · {reward['rarity']}."
        if reward['image'] and not reward['photo']:
            message += '\n' + reward['image']
    return message, (reward or {}).get('photo') or None


def process_closed(send):
    current = current_week()['start']
    with _session() as session:
        pending = [(key, q) for _, key, q in rows(Sheets(session))
                   if q['week'] < current and (q['status'] == 'active' or
                      q['status'] in ('success', 'failed') and not q.get('delivered'))]
    if not pending:
        return
    history = {w['weekStart']: w for w in read_stats_history()['weeks']} if any(
        q['status'] == 'active' for _, q in pending) else {}
    for key, quest in pending:
        try:
            if quest['status'] == 'active':
                week = history.get(quest['week'])
                if week is None:
                    continue
                quest = finalize(key, week)
            if quest and quest['status'] in ('success', 'failed') and not quest.get('delivered'):
                message, photo = finish_message(quest)
                send(quest['peer'], message, attachment=photo, random_id=f'personal-quest-finish:{key}')
                mark_delivered(key)
                logging.warning('PERSONAL_QUEST_FINALIZED key=%s status=%s', key, quest['status'])
        except Exception:
            logging.exception('PERSONAL_QUEST_FINISH_RETRY key=%s', key)


def worker(send):
    while True:
        try:
            process_closed(send)
        except Exception:
            logging.exception('PERSONAL_QUEST_WORKER_RETRY')
        time.sleep(60)


def handle_message(message, send):
    peer = int(message['peer_id'])
    payload = message.get('payload')
    try:
        payload = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    action = payload.get('pq')
    text = ' '.join(str(message.get('text') or '').casefold().split())
    if text != 'квест' and action not in ('offer', 'accept', 'cancel_offer', 'cancel'):
        return False

    def inline(text, buttons):
        send(peer, text, keyboard={'inline': True, 'buttons': [[item] for item in buttons]})

    try:
        week = current_week()
        if action and payload.get('week') != week['start']:
            raise ValueError('Эта кнопка относится к другой неделе. Открой «Квест» заново.')
        key = f"{peer}:{week['start']}"
        with _session() as session:
            quest = next((q for _, ident, q in rows(Sheets(session)) if ident == key), None)
        if quest and quest['status'] == 'cancelled':
            send(peer, '🦉 Квест этой недели отменён. Следующий можно взять на новой неделе.', keyboard=menu())
            return True
        if quest and action == 'cancel_offer':
            inline('Отменить квест? Другой в этой неделе взять не получится.',
                   [button('Да, отменить квест', pq='cancel', week=week['start'])])
            return True

        if quest:
            if action == 'cancel':
                save_choice(peer, week, payload, quest['name'], None, 0)
                send(peer, '🦉 Квест отменён. Я буду ждать тебя на следующей неделе.', keyboard=menu())
                return True
            snapshot = read_stats_history()
            current_data = next((w for w in snapshot['weeks'] if w['weekStart'] == week['start']), {})
            fact = fact_for(current_data, quest['name']) or 0
        else:
            participants = stats_get({'action': 'participants'})['participants']
            people = [p['name'] for p in participants if str(p['vkId']) == str(peer)
                      and str(p['status']).strip().casefold() != 'выбыл']
            if len(people) != 1:
                raise ValueError('Квест доступен участникам охоты с привязанным профилем VK.')
            snapshot = read_stats_history()
            if snapshot['currentWeek']['start'] != week['start']:
                raise ValueError('Игровая неделя сменилась. Открой «Квест» заново.')
            median_fact, fact = calculate(snapshot, people[0])
            if median_fact is None:
                raise ValueError('Чтобы взять квест, нужны отчёты хотя бы за две из шести завершённых недель.')
            if action in ('offer', 'accept'):
                level = payload.get('level')
                if level not in LEVELS:
                    raise ValueError('Выбери сложность в меню «Квест».')
                label, increment, rarity = LEVELS[level]
                target = max(median_fact, fact) + increment
                if action == 'accept':
                    quest = save_choice(peer, week, payload, people[0], median_fact, fact)
                if not quest:
                    changed = 'Цель обновилась. Проверь её и подтверди снова.\n\n' if action == 'accept' else ''
                    inline(f'{changed}{label} квест · {target:g} мышей до {week["endDisplay"]}\n'
                           f'Награда за успех: карточка · {rarity}.\n\n{HONESTY}',
                           [button('Принять квест', pq='accept', week=week['start'], level=level, target=target)])
                    return True
            else:
                base = max(median_fact, fact)
                inline(f'🦉 Выбери личный квест до {week["endDisplay"]}.\nМедианный факт: {median_fact:g} · текущий: {fact:g}.',
                       [button(f'{label} · {base + increment:g} мышей', pq='offer', week=week['start'], level=level)
                        for level, (label, increment, _) in LEVELS.items()])
                return True

        story = CATALOG[quest['story']]
        progress = math.floor(100 * fact / quest['target'])
        inline(f"🦉 «{story['title']}»\n{story['intro']}\n\n"
               f"Твоя цель до конца недели: {quest['target']:g} мышей.\n"
               f"Прогресс: {fact:g} из {quest['target']:g} ({progress}%).",
               [button('Отменить квест', pq='cancel_offer', week=week['start'])])
    except ValueError as exc:
        send(peer, f'🦉 {exc}', keyboard=menu())
    except (Busy, Uncertain):
        send(peer, '🦉 Таблица сейчас занята. Нажми ту же кнопку чуть позже.', keyboard=menu())
    except Exception:
        logging.exception('PERSONAL_QUEST_COMMAND_FAILED')
        send(peer, '🦉 Сейчас квесты не открылись. Попробуй чуть позже.', keyboard=menu())
    return True
