"""Встроенные опросы VK: состояние в книге, API-токены только в окружении."""
import hashlib
import json
import os
from datetime import datetime, timezone
import requests
from website_store import append, update
from quest_votes import options, deadline

HEADERS = ['quest_id', 'stage_id', 'fingerprint', 'status', 'owner_id', 'poll_id',
           'answers_JSON', 'votes_JSON', 'closed', 'message_id']
TAB = '_QuestPolls'


def fingerprint(quest, stage, choices):
    body = [quest['peer_id'], stage['текст'], choices, int(deadline(stage).timestamp())]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def state(api, book, quest, stage, choices):
    from native_quests import table
    if TAB not in api.tabs(book):
        return None
    _, rows = table(api, book, TAB)
    row = next((r for r in rows if r['quest_id'] == quest['quest_id'] and r['stage_id'] == stage['stage_id']), None)
    if row and row['fingerprint'] != fingerprint(quest, stage, choices):
        raise ValueError('Настройки опубликованного опроса изменены; используйте новый stage_id')
    return row


def storage(api, book, action, data):
    from native_quests import table
    _, quests = table(api, book, 'Квесты')
    _, stages = table(api, book, 'Этапы')
    if action == 'pollTasks':
        tasks = []
        for quest in quests:
            if quest.get('статус') != 'running' or str(quest.get('peer_id')) != str(data.get('peerId')):
                continue
            stage = next((s for s in stages if s['quest_id'] == quest['quest_id'] and s['stage_id'] == quest['текущий_этап']), None)
            if not stage or stage.get('тип') != 'vote' or datetime.now(timezone.utc) < deadline(stage):
                continue
            choices = options(stage, stages)
            row = state(api, book, quest, stage, choices)
            if row and row['status'] == 'published' and str(row.get('closed')) != '1':
                tasks.append(dict(questId=quest['quest_id'], stageId=stage['stage_id'],
                    ownerId=int(row['owner_id']), pollId=int(row['poll_id']),
                    answerIds=json.loads(row['answers_JSON']), endDate=int(deadline(stage).timestamp()),
                    question=stage['текст'], answerTexts=[c['текст'] for c in choices]))
        return dict(success=True, tasks=tasks)
    quest = next(q for q in quests if q['quest_id'] == data['questId'])
    stage = next(s for s in stages if s['quest_id'] == data['questId'] and s['stage_id'] == data['stageId'])
    choices = options(stage, stages)
    row = state(api, book, quest, stage, choices)
    if action == 'pollConfig':
        if not row or row['status'] != 'creating':
            raise ValueError('Нет незавершённого создания опроса для восстановления')
        return dict(success=True, question=stage['текст'], answers=[c['текст'] for c in choices],
                    endDate=int(deadline(stage).timestamp()))
    sid = api.ensure_tab(book, TAB, HEADERS)
    if action == 'pollClaim':
        _, outbox = table(api, book, 'Исходящие')
        if not any(r['event_id'] == data['eventId'] and r['quest_id'] == quest['quest_id']
                   and r['stage_id'] == stage['stage_id'] and r['статус'] == 'pending' for r in outbox):
            raise ValueError('Нет ожидающего объявления этого опроса')
        if row and row['status'] != 'retry':
            if row['status'] == 'creating':
                raise RuntimeError('Создание опроса не подтверждено. Не повторяем polls.create: проверьте VK и привяжите существующий poll_id')
            return dict(success=True, poll=dict(ownerId=int(row['owner_id']), pollId=int(row['poll_id'])))
        if datetime.now(timezone.utc) >= deadline(stage):
            raise ValueError('Дедлайн прошёл до создания опроса. Нужен новый этап с будущим дедлайном')
        if row:
            api.batch(book, [update(sid, row['_row'], 3, ['creating'])])
        else:
            api.batch(book, [append(sid, [quest['quest_id'], stage['stage_id'], fingerprint(quest, stage, choices),
                'creating', '', '', '', '', '', ''])])
        return dict(success=True, create=True, question=stage['текст'],
                    answers=[c['текст'] for c in choices], endDate=int(deadline(stage).timestamp()))
    if not row:
        raise ValueError('Опрос не зарегистрирован')
    def save(key, value):
        api.batch(book, [update(sid, row['_row'], HEADERS.index(key), [value])])
    if action == 'pollCreateFailed':
        if row['status'] != 'creating':
            raise ValueError('Нет попытки создания')
        save('status', 'retry')
    elif action == 'pollBind':
        if row['status'] != 'creating':
            raise ValueError('Опрос уже привязан')
        ids = data['answerIds']
        if len(ids) != len(choices) or len(set(ids)) != len(ids) or any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError('Ответы VK не соответствуют вариантам этапа')
        if not int(data['ownerId']) or int(data['pollId']) <= 0:
            raise ValueError('Некорректный ID опроса')
        values = ['ready', int(data['ownerId']), int(data['pollId']), json.dumps(ids), '', '', '']
        api.batch(book, [update(sid, row['_row'], 3, values)])
    elif action == 'pollPublished':
        if row['status'] not in ('ready', 'published') or int(data['messageId']) <= 0:
            raise ValueError('Опрос ещё не готов к публикации')
        api.batch(book, [update(sid, row['_row'], 3, ['published']),
                         update(sid, row['_row'], 9, [int(data['messageId'])])])
    elif action == 'pollSnapshot':
        if row['status'] != 'published' or datetime.now(timezone.utc) < deadline(stage):
            raise ValueError('Рано подводить итоги')
        counts = data['counts']
        if len(counts) != len(choices) or any(type(n) is not int or n < 0 for n in counts) or data.get('closed') is not True:
            raise ValueError('Нужен подтверждённый результат закрытого опроса')
        api.batch(book, [update(sid, row['_row'], 7, [json.dumps(counts), '1'])])
    else:
        raise ValueError('Неизвестное действие опроса')
    return dict(success=True)


class VKRejected(RuntimeError):
    pass


def vk_call(method, token, **params):
    if not token:
        raise RuntimeError('Не задан VK_POLL_USER_TOKEN' if method.startswith('polls.') else 'Не задан VK_TOKEN')
    # Не включаем текст сетевой ошибки, тело ответа или URL с токеном в журнал.
    try:
        response = requests.post('https://api.vk.com/method/' + method,
            data=dict(params, access_token=token, v='5.199'), timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f'VK {method}: HTTP {response.status_code}')
        body = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError('VK ' + method + ': не удалось подтвердить ответ API') from None
    if body.get('error'):
        code = body['error'].get('error_code', '?')
        raise VKRejected(f'VK {method}: ошибка {code}. Проверьте права токена и доступ к опросу')
    if 'response' not in body:
        raise RuntimeError('VK ' + method + ': ответ отсутствует')
    return body['response']


def user_token():
    return os.environ.get('VK_POLL_USER_TOKEN', '').strip()


def validate_poll(poll, config):
    if (poll.get('multiple') is not False or int(poll.get('end_date', 0)) != config['endDate']
            or poll.get('question') != config['question']
            or [a.get('text') for a in poll.get('answers', [])] != config['answers']):
        raise ValueError('VK вернул опрос с другими параметрами')


def publish(item, api_call):
    # Проверяем токен до claim: отсутствие настройки не блокирует создание навсегда.
    token = user_token()
    if not token:
        raise RuntimeError('Для встроенных опросов добавьте VK_POLL_USER_TOKEN в Render')
    group_token = os.environ.get('VK_TOKEN', '').strip()
    if not group_token:
        raise RuntimeError('Не задан VK_TOKEN сообщества')
    cfg = item['poll']
    key = dict(questId=cfg['questId'], stageId=cfg['stageId'])
    claim = api_call('pollClaim', **key, eventId=item['eventId'])
    if claim.get('create'):
        try:
            poll = vk_call('polls.create', token, question=claim['question'],
                add_answers=json.dumps(claim['answers'], ensure_ascii=False), is_anonymous=1,
                is_multiple=0, disable_unvote=0, end_date=claim['endDate'])
        except VKRejected:
            # Явный отказ VK означает, что объект не был создан. Сетевой таймаут — нет.
            api_call('pollCreateFailed', **key)
            raise
        validate_poll(poll, claim)
        api_call('pollBind', **key, ownerId=int(poll['owner_id']), pollId=int(poll['id']),
            answerIds=[int(a['id']) for a in poll['answers']])
        saved = dict(ownerId=int(poll['owner_id']), pollId=int(poll['id']))
    else:
        saved = claim['poll']
    group_token = os.environ.get('VK_TOKEN', '').strip()
    stable = int.from_bytes(hashlib.sha256(item['eventId'].encode()).digest()[:4], 'big') & 0x7fffffff
    message_id = vk_call('messages.send', group_token, peer_id=int(item['peerId']),
        message=item['text'], attachment=f"poll{saved['ownerId']}_{saved['pollId']}", random_id=stable or 1)
    messages = vk_call('messages.getById', group_token, message_ids=message_id)
    found = next((m for m in messages.get('items', []) if int(m.get('id', 0)) == int(message_id)
                  and str(m.get('peer_id')) == str(item['peerId'])), None)
    if not found or not any(a.get('type') == 'poll' and int(a.get('poll', {}).get('id', 0)) == saved['pollId']
            and int(a.get('poll', {}).get('owner_id', 0)) == saved['ownerId'] for a in found.get('attachments', [])):
        raise RuntimeError('VK не подтвердил вложение опроса в беседу. Объявление не отмечено отправленным')
    api_call('pollPublished', **key, messageId=int(message_id))


def refresh(peer_id, api_call):
    tasks = api_call('pollTasks', peerId=str(peer_id))
    for task in tasks['tasks']:
        poll = vk_call('polls.getById', user_token(), owner_id=task['ownerId'], poll_id=task['pollId'])
        if (int(poll.get('id', 0)) != task['pollId'] or int(poll.get('owner_id', 0)) != task['ownerId']
                or poll.get('multiple') is not False or int(poll.get('end_date', 0)) != task['endDate']):
            raise ValueError('Настройки VK-опроса изменены. Автоматический переход остановлен')
        if poll.get('closed') is not True:
            raise RuntimeError('VK ещё не подтвердил закрытие опроса; повторите проверку позже')
        answers = {int(a['id']): a for a in poll.get('answers', [])}
        if set(answers) != set(task['answerIds']):
            raise ValueError('Варианты VK-опроса изменены')
        if (poll.get('question') != task['question'] or
                [answers[i].get('text') for i in task['answerIds']] != task['answerTexts']):
            raise ValueError('Вопрос или текст вариантов VK-опроса изменён')
        counts = [answers[i]['votes'] for i in task['answerIds']]
        api_call('pollSnapshot', questId=task['questId'], stageId=task['stageId'], counts=counts, closed=True)


def recover(data, api_call):
    key = dict(questId=str(data['questId']), stageId=str(data['stageId']))
    config = api_call('pollConfig', **key)
    owner, poll_id = int(data['ownerId']), int(data['pollId'])
    poll = vk_call('polls.getById', user_token(), owner_id=owner, poll_id=poll_id)
    if int(poll['owner_id']) != owner or int(poll['id']) != poll_id:
        raise ValueError('VK вернул другой опрос')
    validate_poll(poll, config)
    return api_call('pollBind', **key, ownerId=owner, pollId=poll_id,
                    answerIds=[int(a['id']) for a in poll['answers']])
