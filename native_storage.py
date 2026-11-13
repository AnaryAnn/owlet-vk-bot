"""Нативные адаптеры статистики, синхронизации и ChatLog. Без Apps Script."""
import hashlib
import json
import math
import os
import random
import time
import logging
from datetime import date, timedelta

from sheets_api import _session, _quote, _number, current_week, SHEET_ID
from website_store import Sheets, ARTIFACTS_ID, append, update, save_report, artifact_drop, stamp


def enabled():
    if os.environ.get('NATIVE_STORAGE_ENABLED', '').lower() != 'true':
        raise RuntimeError('Полный переход на Python ещё не включён')


def normalize(value):
    return ' '.join(str(value).strip().lower().replace('ё', 'е').split())


def write_rows(sid, row, matrix):
    from website_store import cell
    return {'updateCells': {'start': {'sheetId': sid, 'rowIndex': row, 'columnIndex': 0},
        'rows': [{'values': [cell(v) for v in r]} for r in matrix], 'fields': 'userEnteredValue'}}


def formulas(sid, active):
    values = ['=SUM(B8:B)', '=SUM(C8:C)', '=IF(B2="","",MAX(0,B2-B4))',
        f'=IF(B2="","",MAX(0,ROUNDUP((B2-B4)/{max(1, active)},0)))' if active else '=IF(B2="","",0)']
    return {'updateCells': {'start': {'sheetId': sid, 'rowIndex': 2, 'columnIndex': 1},
        'rows': [{'values': [{'userEnteredValue': {'formulaValue': v}}]} for v in values],
        'fields': 'userEnteredValue'}}


def participant_rows(api):
    # Читаем конкретный диапазон напрямую, без запроса всех листов книги.
    return api.values(SHEET_ID, ["'Участники'!A2:C"])[0]


def people(rows):
    return [{'name': str(r[0]).strip(), 'vkId': str(r[1]).strip() if len(r) > 1 else '',
        'status': 'выбыл' if len(r) > 2 and str(r[2]).strip().lower() == 'выбыл' else 'активен'}
        for r in rows if r and str(r[0]).strip()]


def ensure_week(api, participants=None):
    """Вызывать под блокировкой статистики. Недельный лист создаётся из живого состава."""
    week = current_week(); title = week['sheetName']
    tabs = api.tabs(SHEET_ID)
    if title in tabs:
        return tabs[title]['sheetId']
    rows = participants if participants is not None else participant_rows(api)
    members = sorted(people(rows), key=lambda p: p['name'])
    used = {p['sheetId'] for p in tabs.values()}
    sid = random.SystemRandom().randrange(1, 2**30)
    while sid in used: sid = random.SystemRandom().randrange(1, 2**30)
    matrix = [['Цель команды', ''], ['Суммарный план', ''], ['Суммарный факт', ''],
        ['Не хватает до цели', ''], ['Добавить к плану на человека', ''],
        ['Имя', 'План', 'Факт', 'Последнее изменение']]
    matrix += [[p['name'], '', '', ''] for p in members]
    meta = api.ensure_tab(SHEET_ID, '_Служебное', ['Лист', 'Неделя с', 'Неделя по'])
    known = api.values(SHEET_ID, ["'_Служебное'!A2:C"])[0]
    writes = [{'addSheet': {'properties': {'sheetId': sid, 'title': title,
        'gridProperties': {'rowCount': max(1000, len(members) + 7), 'columnCount': 4,
                           'frozenRowCount': 7}}}},
        update(sid, 0, 0, ['Неделя ' + week['startDisplay'] + ' - ' + week['endDisplay']]),
        write_rows(sid, 1, matrix), formulas(sid, sum(p['status'] == 'активен' for p in members))]
    if not any(r and r[0] == title for r in known):
        writes.append(append(meta, [title, week['start'], week['end']]))
    api.batch(SHEET_ID, writes)
    return sid


def records(rows, week):
    result = []
    for r in rows:
        if not r or not str(r[0]).strip(): continue
        r = list(r) + [''] * max(0, 4 - len(r))
        if r[1] == '' and r[2] == '': continue
        result.append(dict(name=str(r[0]).strip(), plan=_number(r[1]), fact=_number(r[2]),
            updated=str(r[3]), weekStart=week['start'], weekEnd=week['end']))
    return result


def stats_get(data=None):
    enabled(); data = data or {}; action = data.get('action', 'stats')
    with _session(write=True) as session:
        api = Sheets(session)
        members = people(participant_rows(api))
        basic = dict(success=True, people=sorted(p['name'] for p in members),
            peopleCount=len(members), activePeopleCount=sum(p['status'] == 'активен' for p in members), participants=members)
        if action == 'participants': return basic
        if action == 'vkUser':
            names = [p['name'] for p in members if p['vkId'] == str(data.get('vkId', ''))]
            return dict(success=True, vkId=str(data.get('vkId', '')), name=names[0] if len(names) == 1 else None)
        week = current_week()
        if data.get('week') == 'previous':
            start = date.fromisoformat(week['start']) - timedelta(days=7); end = start + timedelta(days=6)
            week = dict(start=start.isoformat(), end=end.isoformat(), startDisplay=start.strftime('%d.%m.%Y'),
                endDisplay=end.strftime('%d.%m.%Y'), sheetName=f'{start:%d.%m}-{end:%d.%m.%y}')
        elif data.get('week', 'current') != 'current':
            raise ValueError('Неделя должна быть current или previous')
        if week['sheetName'] not in api.tabs(SHEET_ID):
            if data.get('week') == 'previous': raise ValueError('Лист прошлой недели не найден')
            with api.locked(SHEET_ID): ensure_week(api)
        goal_rows, rows = api.values(SHEET_ID, [f"{_quote(week['sheetName'])}!B2", f"{_quote(week['sheetName'])}!A8:D"])
        goal = _number(goal_rows[0][0]) if goal_rows and goal_rows[0] and goal_rows[0][0] != '' else None
        current = records(rows, week)
        if action == 'current':
            name = str(data.get('name', '')); match = [r for r in current if r['name'] == name]
            if name not in basic['people'] or len(match) > 1: raise ValueError('Участник не найден или неоднозначен')
            return dict(success=True, week=week, record=match[0] if match else None)
        if action == 'settings': return dict(success=True, week=week, teamGoal=goal)
        if action not in ('stats', 'bonusWeekStats'): raise ValueError('Неизвестное чтение статистики')
        return dict(basic, currentWeek=week, teamGoal=goal, currentRecords=current,
            selectedWeek=data.get('week', 'current'), weeks=[dict(sheetName=week['sheetName'],
            weekStart=week['start'], weekEnd=week['end'], goal=goal, records=current)])


def sync_members(api, incoming, sync_id):
    rows = participant_rows(api); old = people(rows)
    by_id = {}
    for p in incoming:
        vk_id, name = str(p.get('vkId', '')).strip(), str(p.get('name', '')).strip()
        if not vk_id.isdigit() or not name: raise ValueError('Некорректный участник VK')
        by_id[vk_id] = name
    known = {p['vkId'] for p in old if p['vkId']}
    added = [dict(name=name, vkId=i, status='активен') for i, name in by_id.items() if i not in known]
    removed = [p for p in old if p['vkId'] and p['vkId'] not in by_id]
    keep = [p for p in old if not p['vkId'] or p['vkId'] in by_id] + added
    sid = ensure_week(api, [[p['name'], p['vkId'], p['status']] for p in keep])
    weekly = api.values(SHEET_ID, [f"{_quote(current_week()['sheetName'])}!A8:D"])[0]
    left_names = {normalize(p['name']) for p in removed}; kept_reported = []
    new_weekly = []
    for r in weekly:
        if not r or not str(r[0]).strip(): continue
        r = list(r) + [''] * max(0, 4 - len(r))
        if normalize(r[0]) in left_names:
            if all(v == '' for v in r[1:4]): continue
            kept_reported.extend(dict(name=p['name'], vkId=p['vkId']) for p in removed if normalize(p['name']) == normalize(r[0]))
        new_weekly.append(r[:4])
    present = {normalize(r[0]) for r in new_weekly}
    for p in keep:
        if normalize(p['name']) not in present:
            new_weekly.append([p['name'], '', '', '']); present.add(normalize(p['name']))
    members_matrix = [[p['name'], p['vkId'], p['status']] for p in keep]
    members_matrix += [['', '', '']] * max(0, len(rows) - len(members_matrix))
    new_weekly += [['', '', '', '']] * max(0, len(weekly) - len(new_weekly))
    writes = [formulas(sid, sum(p['status'] == 'активен' for p in keep))]
    if members_matrix: writes.append(write_rows(api.tabs(SHEET_ID)['Участники']['sheetId'], 1, members_matrix))
    if new_weekly: writes.append(write_rows(sid, 7, new_weekly))
    result = dict(success=True, vkCount=len(by_id), participantsCount=len(keep),
        added=[dict(name=p['name'], vkId=p['vkId']) for p in added],
        removed=[dict(name=p['name'], vkId=p['vkId']) for p in removed], keptReported=kept_reported)
    return result, writes


def stats_post(data):
    enabled(); action = data.get('action')
    if action == 'checkEvent':
        # A read-only duplicate check must not compete with saving reports.
        with _session() as session:
            api = Sheets(session)
            if 'EventLog' not in api.tabs(SHEET_ID):
                return dict(success=True, exists=False)
            rows = api.values(SHEET_ID, ["'EventLog'!A2:D"])[0]
            return dict(success=True, exists=any(r and str(r[0]) == str(data.get('eventId') or '') for r in rows))
    if action == 'vkSave':
        logging.warning('REPORT_NATIVE report_id=%s stage=person_start', data.get('reportId'))
        person = stats_get({'action': 'vkUser', 'vkId': str(data.get('vkId', ''))})
        logging.warning('REPORT_NATIVE report_id=%s stage=person_done found=%s', data.get('reportId'), bool(person.get('name')))
        if not person.get('name'): return dict(success=False, unknownVkUser=True, vkId=str(data.get('vkId', '')))
        from website_store import payload_values
        values = payload_values(dict(name=person['name'], plan=data.get('plan'), fact=data.get('fact'),
            requestId=hashlib.sha256(str(data.get('reportId', '')).encode()).hexdigest()))
        if not data.get('reportId'): raise ValueError('Нужен reportId')
        with _session(write=True) as session:
            logging.warning('REPORT_NATIVE report_id=%s stage=write_session_ready', data.get('reportId'))
            api = Sheets(session)
            result, _, _ = save_report(api, values[0], values[1], values[2], str(data['reportId']))
        logging.warning('REPORT_NATIVE report_id=%s stage=committed', data.get('reportId'))
        return dict(result, reportId=str(data['reportId']))
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(SHEET_ID):
            if action == 'markEventProcessed':
                event = str(data.get('eventId') or '')
                log = api.ensure_tab(SHEET_ID, 'EventLog', ['event_id', 'timestamp', 'vk_id', 'peer_id'])
                rows = api.values(SHEET_ID, ["'EventLog'!A2:D"])[0]
                exists = any(r and str(r[0]) == event for r in rows)
                if action == 'markEventProcessed' and event and not exists:
                    api.batch(SHEET_ID, [append(log, [event, stamp(), str(data.get('vkId', '')), str(data.get('peerId', ''))])])
                return dict(success=True, exists=exists)
            if action == 'saveGoal':
                goal = data.get('goal')
                if isinstance(goal, bool) or not isinstance(goal, (int, float)) or not math.isfinite(goal) or goal < 0:
                    raise ValueError('Некорректная цель')
                sid = ensure_week(api)
                api.batch(SHEET_ID, [update(sid, 1, 1, [goal])]); return dict(success=True, teamGoal=goal)
            if action == 'vkAutoBind':
                rows = participant_rows(api); members = people(rows); vk_id = str(data.get('vkId', '')).strip()
                if not vk_id.isdigit(): raise ValueError('Некорректный VK ID')
                existing = [p for p in members if p['vkId'] == vk_id]
                if len(existing) == 1: return dict(success=True, name=existing[0]['name'], alreadyBound=True)
                if len(existing) > 1: return dict(success=False, ambiguous=True)
                name = (str(data.get('firstName', '')) + ' ' + str(data.get('lastName', ''))).strip()
                if not name: raise ValueError('Пустое имя VK')
                matches = [(i, r) for i, r in enumerate(rows, 1) if r and normalize(r[0]) == normalize(name)]
                if len(matches) > 1: return dict(success=False, ambiguous=True)
                pid = api.tabs(SHEET_ID)['Участники']['sheetId']
                if matches:
                    index, r = matches[0]
                    if len(r) > 1 and str(r[1]).strip() not in ('', vk_id): return dict(success=False, ambiguous=True)
                    api.batch(SHEET_ID, [update(pid, index, 1, [vk_id])]); name = str(r[0]).strip()
                else:
                    api.batch(SHEET_ID, [append(pid, [name, vk_id, 'активен'])])
                sid = ensure_week(api)
                current = api.values(SHEET_ID, [f"{_quote(current_week()['sheetName'])}!A8:D"])[0]
                if not any(r and normalize(r[0]) == normalize(name) for r in current):
                    api.batch(SHEET_ID, [append(sid, [name, '', '', ''])])
                return dict(success=True, name=name, autoBound=True, newParticipant=not matches)
            if action == 'syncParticipants':
                sync_id = str(data.get('syncId') or '')
                if not sync_id: raise ValueError('Нужен syncId')
                journal = api.ensure_tab(SHEET_ID, '_NativeSync', ['sync_id', 'payload_hash', 'result_json'])
                saved = api.values(SHEET_ID, ["'_NativeSync'!A2:C"])[0]
                digest = hashlib.sha256(json.dumps(data.get('members', []), sort_keys=True).encode()).hexdigest()
                for r in saved:
                    if r and r[0] == sync_id:
                        if r[1] != digest: raise ValueError('syncId повторён с другими участниками')
                        return json.loads(r[2])
                result, writes = sync_members(api, data.get('members', []), sync_id)
                api.batch(SHEET_ID, writes + [append(journal, [sync_id, digest, json.dumps(result, ensure_ascii=False)])])
                return result
            raise ValueError('Неизвестная запись статистики')


def digest_post(data):
    enabled(); action = data.get('action')
    with _session(write=True) as session:
        api = Sheets(session)
        if action == 'rollArtifact':
            return artifact_drop(api, dict(requestId=str(data['reportId']), vkId=str(data['vkId']),
                name=str(data['name']), plan=data['plan'], fact=data['fact']))
        with api.locked(ARTIFACTS_ID):
            sid = api.ensure_tab(ARTIFACTS_ID, 'ChatLog', ['timestamp', 'peer_id', 'from_id', 'name', 'text', 'event_id'])
            rows = api.values(ARTIFACTS_ID, ["'ChatLog'!A2:F"])[0]
            if action in ('getEditionMessages', 'completeEdition'):
                peer = str(data.get('peerId', ''))
                until = float(data['until'])
                state_sid = api.ensure_tab(ARTIFACTS_ID, '_DigestState', ['peer_id', 'until'])
                states = api.values(ARTIFACTS_ID, ["'_DigestState'!A2:B"])[0]
                match = next(((i, r) for i, r in enumerate(states, 1) if r and str(r[0]) == peer), None)
                previous = float(match[1][1]) if match else until - 86400
                if action == 'getEditionMessages':
                    messages = [dict(timestamp=float(r[0]), peerId=str(r[1]), fromId=str(r[2]),
                        name=str(r[3]), text=str(r[4])) for r in rows if len(r) > 4
                        and str(r[1]) == peer and previous < float(r[0]) <= until]
                    return dict(success=True, alreadySent=bool(match and previous >= until),
                        messages=sorted(messages, key=lambda m: m['timestamp']))
                if match and previous >= until:
                    return dict(success=True, duplicate=True)
                keep = [r for r in rows if r and (str(r[1]) != peer or float(r[0]) > until)]
                writes = [update(state_sid, match[0], 0, [peer, until]) if match else append(state_sid, [peer, until])]
                if rows:
                    writes.append(write_rows(sid, 1, keep + [[''] * 6] * (len(rows) - len(keep))))
                api.batch(ARTIFACTS_ID, writes)
                return dict(success=True, deleted=len(rows) - len(keep))
            if action == 'saveMessage':
                event = str(data.get('eventId') or '')
                if event and any(len(r) > 5 and str(r[5]) == event for r in rows): return dict(success=True, duplicate=True)
                if not data.get('peerId') or not data.get('fromId') or not data.get('timestamp'): raise ValueError('Неполное сообщение')
                api.batch(ARTIFACTS_ID, [append(sid, [data['timestamp'], str(data['peerId']), str(data['fromId']),
                    str(data.get('name', '')), str(data.get('text', '')), event])]); return dict(success=True)
            if action == 'getMessages':
                hours = max(1, min(float(data.get('hours') or 12), 48)); cutoff = time.time() - hours * 3600
                messages = [dict(timestamp=float(r[0]), peerId=str(r[1]), fromId=str(r[2]),
                    name=str(r[3]), text=str(r[4])) for r in rows if len(r) > 4 and float(r[0]) >= cutoff
                    and str(r[1]) == str(data.get('peerId', ''))]
                return dict(success=True, count=len(messages), messages=sorted(messages, key=lambda m: m['timestamp']))
            if action == 'cleanup':
                cutoff = time.time() - max(12, min(float(data.get('hours') or 24), 168)) * 3600
                keep = [r for r in rows if r and float(r[0]) >= cutoff]
                if rows: api.batch(ARTIFACTS_ID, [write_rows(sid, 1, keep + [[''] * 6] * (len(rows) - len(keep)))])
                return dict(success=True, deleted=len(rows) - len(keep))
            raise ValueError('Неизвестное действие дайджестов')
