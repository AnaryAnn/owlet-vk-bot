"""Голосование inline-кнопками VK. Состояние хранится под замком книги."""
import json
import hashlib
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from website_store import append, update

HEADERS = ['quest_id', 'stage_id', 'peer_id', 'vk_id', 'choice', 'message_id', 'updated_ISO', 'revision']


def deadline(stage):
    value = datetime.fromisoformat(str(stage.get('дедлайн_ISO') or '').replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Дедлайн голосования должен содержать часовой пояс')
    return value


def options(stage, stages):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', str(stage.get('quest_id', ''))):
        raise ValueError('ID квеста: 1–40 латинских букв, цифр, дефисов или подчёркиваний')
    choices = json.loads(stage.get('варианты_JSON') or '[]')
    if not isinstance(choices, list) or not 2 <= len(choices) <= 6:
        raise ValueError('Голосованию нужны 2–6 вариантов в варианты_JSON')
    targets = {s['stage_id'] for s in stages if s.get('quest_id') == stage['quest_id']}
    for item in choices:
        if (not isinstance(item, dict) or not isinstance(item.get('текст'), str)
                or not item['текст'].strip() or len(item['текст']) > 100
                or item.get('этап') not in targets or item['этап'] == stage['stage_id']):
            raise ValueError('Некорректный вариант голосования или целевой этап')
    for field in ('провал_этап', 'ничья_этап'):
        target = stage.get(field)
        if (field == 'провал_этап' or target) and (target not in targets or target == stage['stage_id']):
            raise ValueError('Нужна корректная ветка ' + field)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', str(stage.get('stage_id', ''))):
        raise ValueError('ID этапа: 1–40 латинских букв, цифр, дефисов или подчёркиваний')
    deadline(stage)
    return choices


def announcement(quest, stage, choices):
    lines = [stage['текст']]
    lines += ['', 'Нажми кнопку с выбранным вариантом под этим сообщением.',
              'Один голос на участника. До дедлайна выбор можно изменить.',
              'Приём до ' + deadline(stage).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК') + '.']
    lines += ['При ничьей — отдельная ветка сюжета.' if stage.get('ничья_этап') else
              'При ничьей победит первый из лидирующих вариантов в списке.']
    return '\n'.join(lines)


def vote(api, book, data):
    from native_quests import table, now_iso
    _, quests = table(api, book, 'Квесты')
    _, stages = table(api, book, 'Этапы')
    quest = next((q for q in quests if q['quest_id'] == str(data.get('questId', ''))
                  and str(q.get('peer_id')) == str(data.get('peerId'))), None)
    if not quest or quest.get('статус') != 'running':
        raise ValueError('В этом чате нет такого открытого голосования')
    stage = next((s for s in stages if s['quest_id'] == quest['quest_id']
                  and s['stage_id'] == quest['текущий_этап']), None)
    if not stage or stage.get('тип') != 'vote':
        raise ValueError('Сейчас в этом квесте нет голосования')
    choices = options(stage, stages)
    revision = version(quest, stage, choices)
    if data.get('stageId') != stage['stage_id'] or data.get('revision') != revision:
        raise ValueError('Эта кнопка устарела. Выбери вариант в текущем объявлении квеста')
    if datetime.now(timezone.utc) >= deadline(stage):
        raise ValueError('Приём голосов завершён')
    choice, user = int(data['choice']), str(data['vkId'])
    callback = data.get('source') == 'callback'
    event_id = data.get('eventId') if callback else None
    if callback and (not isinstance(event_id, str) or not 1 <= len(event_id) <= 256):
        raise ValueError('Неверный идентификатор нажатия')
    message = 1 if callback else int(data['messageId'])
    if not user.isdigit() or int(user) <= 0 or message <= 0 or not 1 <= choice <= len(choices):
        raise ValueError('Неверный участник или номер варианта')
    sid = api.ensure_tab(book, 'Голоса', HEADERS)
    headers, rows = table(api, book, 'Голоса')
    if headers != HEADERS:
        raise ValueError('Неверные колонки листа Голоса; см. QUEST_VOTING.md')
    old = next((r for r in rows if r['quest_id'] == quest['quest_id'] and r['stage_id'] == stage['stage_id']
                and str(r['peer_id']) == str(data['peerId']) and str(r['vk_id']) == user), None)
    event_write = None
    if callback:
        event_headers = ['event_key', 'quest_id', 'stage_id', 'peer_id', 'vk_id', 'created_ISO']
        event_sid = api.ensure_tab(book, '_QuestVoteEvents', event_headers)
        actual_headers, events = table(api, book, '_QuestVoteEvents')
        if actual_headers != event_headers:
            raise ValueError('Неверные колонки журнала нажатий')
        event_key = hashlib.sha256(json.dumps([str(data['peerId']), user, event_id]).encode()).hexdigest()
        if any(e['event_key'] == event_key for e in events):
            return dict(success=True, duplicate=True, choice=int(old['choice']) if old else choice)
        # Callback API не передаёт порядковый номер клика. Назначаем порядок
        # обработки под замком; повтор уже принятого event_id ничего не меняет.
        message = int(old['message_id']) + 1 if old else 1
        event_write = append(event_sid, [event_key, quest['quest_id'], stage['stage_id'],
            str(data['peerId']), user, now_iso()])
    if not callback and old and int(old['message_id']) >= message:
        return dict(success=True, duplicate=True, choice=int(old['choice']))
    if old and old.get('revision') != revision:
        raise ValueError('Нельзя менять настройки уже начатого голосования. Используйте новый stage_id')
    values = [quest['quest_id'], stage['stage_id'], str(data['peerId']), user, choice, message, now_iso(), revision]
    writes = [update(sid, old['_row'], 0, values) if old else append(sid, values)]
    if event_write:
        writes.append(event_write)
    api.batch(book, writes)
    return dict(success=True, changed=bool(old), choice=choice)


def tally(api, book, quest, stage, choices):
    from native_quests import table
    counts = [0] * len(choices)
    if 'Голоса' in api.tabs(book):
        _, rows = table(api, book, 'Голоса')
        latest = {}
        for r in rows:
            if (r['quest_id'] == quest['quest_id'] and r['stage_id'] == stage['stage_id']
                    and str(r['peer_id']) == str(quest['peer_id'])):
                if r.get('revision') != version(quest, stage, choices):
                    raise ValueError('Настройки голосования изменены. Автоматический переход остановлен')
                old = latest.get(str(r['vk_id']))
                if old is None or int(r['message_id']) > int(old['message_id']):
                    latest[str(r['vk_id'])] = r
        for r in latest.values():
            n = int(r['choice'])
            if 1 <= n <= len(counts):
                counts[n - 1] += 1
    total = sum(counts)
    leaders = [i for i, n in enumerate(counts) if n == max(counts)]
    if not total:
        target, reason = stage['провал_этап'], 'Голосов нет — выбрана резервная ветка.'
    elif len(leaders) > 1 and stage.get('ничья_этап'):
        target, reason = stage['ничья_этап'], 'Ничья — выбрана ветка для равного числа голосов.'
    else:
        winner = leaders[0]
        target = choices[winner]['этап']
        reason = 'Выбран вариант: ' + choices[winner]['текст'] + ('. При ничьей — первый из лидеров.' if len(leaders) > 1 else '.')
    summary = '🗳 Итоги голосования\n' + '\n'.join(f"{i}. {c['текст']}: {counts[i-1]}" for i, c in enumerate(choices, 1))
    return target, summary + '\n' + reason


def version(quest, stage, choices):
    body = [str(quest['peer_id']), stage['текст'], choices, int(deadline(stage).timestamp()),
            stage.get('провал_этап'), stage.get('ничья_этап')]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def keyboard(quest, stage, choices):
    revision = version(quest, stage, choices)
    buttons = []
    for i, choice in enumerate(choices, 1):
        payload = dict(kind='quest_vote', questId=quest['quest_id'], stageId=stage['stage_id'],
                       choice=i, revision=revision)
        label = f"{i}. {choice['текст']}"
        if len(label) > 40:
            label = label[:39] + '…'
        buttons.append([dict(action=dict(type='callback', label=label,
            payload=json.dumps(payload, ensure_ascii=False, separators=(',', ':'))), color='primary')])
    return dict(inline=True, one_time=False, buttons=buttons)


def parse_payload(value):
    if isinstance(value, str):
        if len(value) > 1024:
            return None
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get('kind') != 'quest_vote':
        return None
    if (not all(isinstance(value.get(k), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', value[k])
                for k in ('questId', 'stageId'))
            or type(value.get('choice')) is not int or not 1 <= value['choice'] <= 6
            or not isinstance(value.get('revision'), str) or not re.fullmatch(r'[a-f0-9]{16}', value['revision'])):
        raise ValueError('Некорректные данные кнопки голосования')
    return {k: value[k] for k in ('questId', 'stageId', 'choice', 'revision')}
