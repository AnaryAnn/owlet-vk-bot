"""Личные задания на 24 часа. Состояние и очереди — в книге статистики."""
import json
import logging
import random
import time
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo

from sheets_api import _session, _quote, _number, current_week, SHEET_ID
from website_store import Sheets, append, update, Busy, artifact_drop

LEVELS = {
    'easy': ('🟢 Лёгкое', 1, 3, 2),
    'normal': ('🟡 Обычное', 4, 7, 3),
    'heroic': ('🔴 Героическое', 8, 15, 6),
}
SCHEMA = {
    '_DailyTasks': ['vk_id', 'name', 'earned_points', 'task_json', 'offer_id'],
    '_DailyHistory': ['event_id', 'vk_id', 'task_json'],
    '_DailyRewards': ['reward_id', 'vk_id', 'name', 'status', 'result_json'],
    '_DailyNotices': ['notice_id', 'vk_id', 'text', 'photo', 'status'],
}


def rows(api, tab):
    return api.values(SHEET_ID, [f"{_quote(tab)}!A2:Z"])[0]


def tables(api):
    return {t: api.ensure_tab(SHEET_ID, t, h) for t, h in SCHEMA.items()}


def find_user(api, vk_id):
    members = api.values(SHEET_ID, ["'Участники'!A2:C"])[0]
    found = [r for r in members if len(r) > 1 and str(r[1]).strip() == str(vk_id)]
    if len(found) != 1 or (len(found[0]) > 2 and str(found[0][2]).lower().strip() == 'выбыл'):
        raise ValueError('Задания доступны участникам с привязанным VK ID. Сначала сдай обычный отчёт в беседе.')
    return str(found[0][0]).strip()


def state(api, vk_id):
    matches = [(i, list(r) + [''] * max(0, 5-len(r))) for i, r in enumerate(rows(api, '_DailyTasks'), 1)
               if r and str(r[0]) == str(vk_id)]
    if len(matches) > 1:
        raise ValueError('Найдены повторные записи личных заданий; нужна проверка организатором')
    return matches[0] if matches else (None, [str(vk_id), '', 0, '', ''])


def task_of(row):
    return json.loads(row[3]) if row[3] else None


def state_write(sid, index, row):
    return update(sid, index, 0, row) if index else append(sid, row)


def history(sids, vk_id, task):
    return append(sids['_DailyHistory'], [task['id'] + ':' + task['status'], str(vk_id), json.dumps(task, ensure_ascii=False)])


def notice(sids, key, vk_id, text, photo=''):
    return append(sids['_DailyNotices'], [key, str(vk_id), text, photo, 'pending'])


def describe(task, points):
    title = LEVELS[task['level']][0]
    deadline = datetime.fromtimestamp(task['deadline'], ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    return (f"{title} задание: +{task['target']} мышей ({task['target'] * 100} крестиков).\n"
        f"Прогресс: {task['progress']}/{task['target']} мышей.\nДо: {deadline}.\n"
        f"За выполнение: {LEVELS[task['level']][3]} очка. К артефакту: {int(points) % 6}/6.\n"
        'Сдавай обычный отчёт «план, факт». Считается прирост факта после выдачи.\n'
        'Отменить или сменить задание нельзя.')


def menu(offer):
    return dict(inline=True, one_time=False, buttons=[[dict(action=dict(type='callback', label=title,
        payload=json.dumps(dict(kind='daily_task', level=key, offer=offer), separators=(',', ':'))), color=color)]
        for (key, (title, _, _, _)), color in zip(LEVELS.items(), ['positive', 'primary', 'negative'])])


def snapshot(api, name):
    from native_storage import ensure_week
    week = current_week()
    ensure_week(api)
    matches = [r for r in api.values(SHEET_ID, [f"{_quote(week['sheetName'])}!A8:D"])[0]
               if r and str(r[0]).strip() == name]
    if len(matches) != 1:
        raise ValueError('Не найдена однозначная строка недельного отчёта. Нужна синхронизация участников.')
    return week['start'], _number(matches[0][2]) if len(matches[0]) > 2 else 0


def request(api, vk_id, level=None, offer=None, now=None, rng=None):
    now = time.time() if now is None else now
    rng = rng or random.SystemRandom()
    name = find_user(api, vk_id)
    sids = tables(api)
    index, row = state(api, vk_id)
    task = task_of(row)
    writes = []
    if task and task['status'] == 'active' and now < task['deadline']:
        return dict(text=describe(task, row[2]), task=task)
    expired = task and task['status'] == 'active'
    if expired:
        task['status'] = 'expired'
        writes.append(history(sids, vk_id, task))
        row[3] = json.dumps(task, ensure_ascii=False)
    if level is None:
        row[1], row[4] = name, row[4] or uuid4().hex
        api.batch(SHEET_ID, writes + [state_write(sids['_DailyTasks'], index, row)])
        prefix = 'Срок предыдущего задания истёк. Очки сохранены.\n\n' if expired else ''
        return dict(text=prefix + 'Выбери задание на 24 часа:\n🟢 1–3 мыши · +2 очка\n🟡 4–7 мышей · +3 очка\n🔴 8–15 мышей · +6 очков\n'
            f'Каждые 6 очков — гарантированный артефакт. Твой прогресс: {int(row[2]) % 6}/6.', keyboard=menu(row[4]))
    if level not in LEVELS or not isinstance(offer, str) or not row[4] or row[4] != offer or expired:
        raise ValueError('Эта кнопка устарела. Напиши «Задание» в ЛС Робосычика.')
    week, fact = snapshot(api, name)
    title, low, high, points = LEVELS[level]
    task = dict(id=uuid4().hex, level=level, target=rng.randint(low, high), issued=now, deadline=now+86400,
        status='active', baseline_fact=fact, week=week, week_baseline=fact, carry=0, progress=0)
    row[1], row[3], row[4] = name, json.dumps(task, ensure_ascii=False), ''
    api.batch(SHEET_ID, writes + [state_write(sids['_DailyTasks'], index, row), history(sids, vk_id, task)])
    return dict(text='📜 Задание получено!\n\n' + describe(task, row[2]), task=task)


def report_update(api, name, vk_id, week, fact, now=None, tabs=None):
    """Вызывается внутри save_report под замком. Возвращает записи для того же batch."""
    now = time.time() if now is None else now
    if '_DailyTasks' not in (api.tabs(SHEET_ID) if tabs is None else tabs):
        return [], None
    index, row = state(api, vk_id)
    task = task_of(row)
    if not task or task['status'] != 'active':
        return [], None
    sids = tables(api)
    writes = []
    earned = int(row[2] or 0)
    if now >= task['deadline']:
        task['status'] = 'expired'
        writes += [history(sids, vk_id, task), notice(sids, task['id']+':expired', vk_id,
            '⌛ Срок задания истёк. Накопленные очки сохранены. Напиши «Задание», чтобы взять новое.')]
    else:
        if task['week'] != week['start']:
            task['carry'], task['week_baseline'], task['week'] = task['progress'], 0, week['start']
        task['progress'] = task['carry'] + max(0, fact - task['week_baseline'])
        if task['progress'] >= task['target']:
            task['status'], task['completed'] = 'completed', now
            before = earned
            earned += LEVELS[task['level']][3]
            row[2] = earned
            writes += [history(sids, vk_id, task), notice(sids, task['id']+':completed', vk_id,
                f"✅ Задание выполнено! +{LEVELS[task['level']][3]} очка. Прогресс к следующему артефакту: {earned % 6}/6.\n"
                'Можно сразу взять новое — напиши «Задание».')]
            for n in range(before // 6 + 1, earned // 6 + 1):
                writes.append(append(sids['_DailyRewards'], [f'daily:{vk_id}:{n}', str(vk_id), name, 'pending', '']))
    row[3] = json.dumps(task, ensure_ascii=False)
    writes.append(state_write(sids['_DailyTasks'], index, row))
    return writes, dict(status=task['status'], progress=task['progress'], target=task['target'], points=earned % 6)


def transact(action, **kwargs):
    import os
    if os.environ.get('NATIVE_STORAGE_ENABLED', '').lower() != 'true':
        raise ValueError('Личные задания ещё не включены')
    for attempt in range(5):
        try:
            with _session(write=True) as session:
                api = Sheets(session)
                with api.locked(SHEET_ID):
                    return action(api, **kwargs)
        except Busy:
            if attempt == 4: raise
            time.sleep(.3 * 2**attempt)


def process_pending(vk_id=None, limit=10, sender=None):
    if sender is None:
        from app import vk_send
        sender = vk_send
    def load(api, tab):
        if tab not in api.tabs(SHEET_ID): return []
        return [(i, r) for i, r in enumerate(rows(api, tab), 1)
                if len(r) >= 5 and r[4 if tab == '_DailyNotices' else 3] == 'pending'
                and (vk_id is None or str(r[1]) == str(vk_id))][:limit]
    rewards = transact(lambda api: load(api, '_DailyRewards'))
    for index, r in rewards:
        try:
            with _session(write=True) as session:
                award = artifact_drop(Sheets(session), dict(requestId=r[0], vkId=str(r[1]), name=r[2], plan=0, fact=0), guaranteed=True)
            if not award.get('dropped'):
                raise ValueError('Гарантированный артефакт ещё не выдан')
            def finish(api):
                latest = rows(api, '_DailyRewards')[index-1]
                if latest[3] == 'done': return
                sids = tables(api); art = award['artifact']
                api.batch(SHEET_ID, [update(sids['_DailyRewards'], index, 3, ['done', json.dumps(award, ensure_ascii=False)]),
                    notice(sids, r[0]+':reward', r[1], f"🎁 Награда за задания: {art['emoji']} {art['name']}!\n" + art.get('description', ''), art.get('vkPhoto', ''))])
            transact(finish)
        except Exception:
            logging.exception('Отложена гарантированная награда личного задания')
    notices = transact(lambda api: load(api, '_DailyNotices'))
    for index, r in notices:
        try:
            sender(int(r[1]), r[2], attachment=r[3] or None, random_id='daily-notice:'+r[0])
            def mark(api):
                sid = api.tabs(SHEET_ID)['_DailyNotices']['sheetId']
                api.batch(SHEET_ID, [update(sid, index, 4, ['sent'])])
            transact(mark)
        except Exception:
            logging.exception('Отложено личное уведомление задания')
    return dict(success=True, rewards=len(rewards), notices=len(notices))


def safe_pending(vk_id):
    try:
        process_pending(str(vk_id))
    except Exception:
        logging.exception('Отложена обработка наград задания')


def sweep():
    def expire(api):
        if '_DailyTasks' not in api.tabs(SHEET_ID): return
        sids = tables(api); writes = []; now = time.time()
        for index, r in enumerate(rows(api, '_DailyTasks'), 1):
            task = task_of(r)
            if task and task['status'] == 'active' and now >= task['deadline']:
                task['status'] = 'expired'
                writes += [update(sids['_DailyTasks'], index, 3, [json.dumps(task, ensure_ascii=False)]),
                    history(sids, r[0], task), notice(sids, task['id']+':expired', r[0],
                        '⌛ Срок задания истёк. Очки сохранены. Напиши «Задание», чтобы взять новое.')]
        if writes: api.batch(SHEET_ID, writes)
    transact(expire)
    return process_pending()
