"""Голосования по сообщениям VK. Все операции выполняются под замком книги."""
import json
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from website_store import append, update

HEADERS = ['quest_id', 'stage_id', 'peer_id', 'vk_id', 'choice', 'message_id', 'updated_ISO']


def deadline(stage):
    value = datetime.fromisoformat(str(stage.get('дедлайн_ISO') or '').replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Дедлайн голосования должен содержать часовой пояс')
    return value


def options(stage, stages):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', str(stage.get('quest_id', ''))):
        raise ValueError('ID квеста: 1–40 латинских букв, цифр, дефисов или подчёркиваний')
    choices = json.loads(stage.get('варианты_JSON') or '[]')
    if not isinstance(choices, list) or not 2 <= len(choices) <= 8:
        raise ValueError('Голосованию нужны 2–8 вариантов в варианты_JSON')
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
    deadline(stage)
    return choices


def announcement(quest, stage, choices):
    lines = [stage['текст'], '', '🗳 Голосование:']
    lines += [f"{i}. {c['текст']}" for i, c in enumerate(choices, 1)]
    lines += ['', f"Напиши: голос {quest['quest_id']} 1", 'Один голос на участника. До дедлайна выбор можно изменить.',
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
    if datetime.now(timezone.utc) >= deadline(stage):
        raise ValueError('Приём голосов завершён')
    choice, user, message = int(data['choice']), str(data['vkId']), int(data['messageId'])
    if not user.isdigit() or int(user) <= 0 or message <= 0 or not 1 <= choice <= len(choices):
        raise ValueError('Неверный участник или номер варианта')
    sid = api.ensure_tab(book, 'Голоса', HEADERS)
    _, rows = table(api, book, 'Голоса')
    old = next((r for r in rows if r['quest_id'] == quest['quest_id'] and r['stage_id'] == stage['stage_id']
                and str(r['peer_id']) == str(data['peerId']) and str(r['vk_id']) == user), None)
    if old and int(old['message_id']) >= message:
        return dict(success=True, duplicate=True, choice=int(old['choice']))
    values = [quest['quest_id'], stage['stage_id'], str(data['peerId']), user, choice, message, now_iso()]
    api.batch(book, [update(sid, old['_row'], 0, values) if old else append(sid, values)])
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
