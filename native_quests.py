"""Правила «Книги приключений», перенесённые из квесты.gs без Apps Script."""
import math
import os
from datetime import datetime, timezone

from sheets_api import _session, _quote
from website_store import Sheets, append, update


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def number(value):
    result = float(str(value or '0').replace(',', '.'))
    if not math.isfinite(result):
        raise ValueError('Некорректное число')
    return result


def table(api, book, name):
    rows = api.values(book, [f'{_quote(name)}!A:Z'], formatted=True)[0]
    if not rows:
        raise ValueError('Отсутствуют заголовки листа «' + name + '»')
    headers = rows[0]
    return headers, [dict(zip(headers, row + [''] * max(0, len(headers) - len(row))), _row=i)
                     for i, row in enumerate(rows[1:], 1) if row and row[0] != '']


def quest_action(api, book, action, data):
    """Вызывается под одной блокировкой книги; переход и исходящие — один batch."""
    tabs = api.tabs(book)
    writes = []
    timestamp = now_iso()
    if action in ('milestoneClaim', 'milestoneAck'):
        headers, rows = table(api, book, 'Процентные рубежи')
        sid = tabs['Процентные рубежи']['sheetId']
        def field(row, key, value):
            if key not in headers:
                raise ValueError('Нет колонки ' + key)
            writes.append(update(sid, row['_row'], headers.index(key), [value]))
        week = str(data.get('weekKey') or '').strip()
        if not week:
            raise ValueError('Нужен weekKey')
        if action == 'milestoneAck':
            selected = next((r for r in rows if r.get('event_id') == str(data.get('eventId') or '')
                             and r.get('неделя_ключ') == week), None)
            if not selected:
                raise ValueError('Рубеж не найден')
            if selected.get('статус') == 'sent':
                return {'success': True}
            if selected.get('статус') != 'sending':
                raise ValueError('Рубеж не в состоянии sending')
            field(selected, 'статус', 'sent')
            # Сохраняем позиционную схему исходного скрипта для даты и ошибки.
            writes += [update(sid, selected['_row'], 7, [timestamp]),
                       update(sid, selected['_row'], 9, [''])]
            api.batch(book, writes)
            return {'success': True}
        if data.get('percent') is None:
            raise ValueError('Нужен percent')
        percent = number(data['percent'])
        if percent < 0:
            raise ValueError('Нужен неотрицательный percent')
        pending = [r for r in rows if r.get('статус') == 'sending' and r.get('неделя_ключ') == week]
        eligible = sorted((r for r in rows if r.get('статус') == 'active'
            and percent >= number(r.get('порог_%')) and r.get('event_id')
            and r.get('текст') and r.get('peer_id')), key=lambda r: number(r.get('порог_%')))
        selected = (pending or eligible or [None])[0]
        if not selected:
            return {'success': True, 'event': None}
        if selected['статус'] == 'active':
            field(selected, 'статус', 'sending'); field(selected, 'неделя_ключ', week)
            api.batch(book, writes)
        return {'success': True, 'event': {'eventId': selected['event_id'],
            'weekKey': week, 'peerId': selected['peer_id'], 'text': selected['текст'],
            'photo': selected.get('vk_photo') or ''}}

    _, outbox = table(api, book, 'Исходящие')
    if action == 'ack':
        event_id = str(data.get('eventId') or '')
        selected = next((r for r in outbox if r.get('event_id') == event_id), None)
        if not event_id or not selected:
            raise ValueError('Событие не найдено')
        if selected.get('статус') != 'sent':
            api.batch(book, [update(tabs['Исходящие']['sheetId'], selected['_row'], 5,
                ['sent', selected.get('создано_ISO') or '', timestamp])])
        return {'success': True}
    if action != 'tick':
        raise ValueError('Неизвестное действие квестов')
    if data.get('fact') is None:
        raise ValueError('Нужен fact')
    fact = number(data['fact'])
    percent = None if data.get('percent') is None else number(data['percent'])
    if fact < 0:
        raise ValueError('Некорректный факт')
    _, quests = table(api, book, 'Квесты')
    _, stages = table(api, book, 'Этапы')
    dry = bool(data.get('dryRun'))
    transitions = []
    emitted = {r.get('event_id') for r in outbox}
    new_outbox = []
    def event(quest, stage_id, kind, text):
        event_id = ':'.join([str(quest['quest_id']), str(stage_id), kind])
        if event_id in emitted:
            return
        emitted.add(event_id)
        writes.extend([
            append(tabs['Исходящие']['sheetId'], [event_id, quest['quest_id'], stage_id,
                quest['peer_id'], text, 'pending', timestamp, '']),
            append(tabs['Журнал']['sheetId'], [event_id, quest['quest_id'], stage_id, kind, timestamp, ''])])
        new_outbox.append({'event_id': event_id, 'peer_id': quest['peer_id'],
                          'текст': text, 'статус': 'pending'})
    for quest in quests:
        if quest.get('статус') not in ('active', 'running'):
            continue
        stage = next((s for s in stages if s.get('quest_id') == quest.get('quest_id')
                      and s.get('stage_id') == quest.get('текущий_этап')), None)
        if not stage:
            raise ValueError('Этап квеста отсутствует')
        if quest['статус'] == 'active':
            if stage.get('тип') == 'start':
                transitions.append({'quest': quest['quest_id'], 'kind': 'start'})
                if not dry:
                    event(quest, stage['stage_id'], 'start', stage['текст'])
                    writes.append(update(tabs['Квесты']['sheetId'], quest['_row'], 2, ['running']))
            continue  # Первый tick не завершает только что начатый квест.
        deadline = datetime.fromisoformat(str(stage.get('дедлайн_ISO') or '').replace('Z', '+00:00'))
        if deadline.tzinfo is None:
            raise ValueError('Дедлайн должен содержать часовой пояс')
        has_fact = stage.get('порог_мышей') not in ('', None)
        has_percent = stage.get('порог_процентов') not in ('', None)
        if not has_fact and not has_percent:
            raise ValueError('Нужен хотя бы один порог')
        success = (not has_fact or fact >= number(stage['порог_мышей'])) and (
            not has_percent or (percent is not None and percent >= number(stage['порог_процентов'])))
        if not success and datetime.now(timezone.utc) < deadline:
            continue
        target_id = stage.get('успех_этап') if success else stage.get('провал_этап')
        target = next((s for s in stages if s.get('quest_id') == quest['quest_id']
                       and s.get('stage_id') == target_id), None)
        if not target:
            raise ValueError('Ветка квеста отсутствует')
        transitions.append({'quest': quest['quest_id'], 'from': stage['stage_id'],
                            'to': target_id, 'success': success})
        if not dry:
            event(quest, target_id, 'success' if success else 'failure', target['текст'])
            writes.append(update(tabs['Квесты']['sheetId'], quest['_row'], 2, ['finished', target_id]))
    if writes:
        api.batch(book, writes)
    return {'success': True, 'transitions': transitions, 'outbox': [
        {'eventId': r['event_id'], 'peerId': r['peer_id'], 'text': r['текст']}
        for r in outbox + new_outbox if r.get('статус') == 'pending']}


def dispatch(action, **data):
    if os.environ.get('NATIVE_STORAGE_ENABLED', '').lower() != 'true':
        raise RuntimeError('Нативное хранилище ещё не включено')
    book = os.environ.get('QUEST_SPREADSHEET_ID', '').strip()
    if not book:
        raise RuntimeError('Не задан QUEST_SPREADSHEET_ID')
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(book):
            return quest_action(api, book, action, data)
