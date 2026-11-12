"""Валидация и выбор ветки встроенного опроса VK."""
import json
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timezone


def deadline(stage):
    value = datetime.fromisoformat(str(stage.get('дедлайн_ISO') or '').replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Дедлайн голосования должен содержать часовой пояс')
    return value


def options(stage, stages):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', str(stage.get('quest_id', ''))):
        raise ValueError('ID квеста: 1–40 латинских букв, цифр, дефисов или подчёркиваний')
    choices = json.loads(stage.get('варианты_JSON') or '[]')
    if not isinstance(choices, list) or not 2 <= len(choices) <= 10:
        raise ValueError('Голосованию нужны 2–10 вариантов в варианты_JSON')
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
    if len(set(c['текст'] for c in choices)) != len(choices):
        raise ValueError('Варианты опроса должны отличаться')
    if not 1 <= len(str(stage.get('текст', '')).strip()) <= 255:
        raise ValueError('Вопрос опроса: от 1 до 255 символов')
    deadline(stage)
    return choices


def announcement(quest, stage, choices):
    lines = [stage['текст'], '', '🗳 Голосование:']
    lines += [f"{i}. {c['текст']}" for i, c in enumerate(choices, 1)]
    lines += ['', 'Выбери вариант в прикреплённом опросе ВК. Голос можно изменить.',
              'Приём до ' + deadline(stage).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК') + '.']
    lines += ['При ничьей — отдельная ветка сюжета.' if stage.get('ничья_этап') else
              'При ничьей победит первый из лидирующих вариантов в списке.']
    return '\n'.join(lines)


def summarize(stage, choices, counts):
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
