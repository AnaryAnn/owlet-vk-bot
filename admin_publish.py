"""Owner-only DM composer. Drafts and send identities survive process restarts."""
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager

import requests
from sheets_api import _session, SHEET_ID
from website_store import Sheets, Busy, append, update

PHRASE = 'Сыч, неси депешу'
TARGET = 2000000002
TTL = 600
TAB = '_AdminBroadcasts'
LOCK_ID = 1937152403
LOCK_KEY = 'owlet_admin_broadcast_v1'
_owner = None
_owner_guard = threading.Lock()


def normalized(text):
    return ' '.join(re.findall(r'[а-яa-z0-9]+', str(text).casefold().replace('ё', 'е')))


def resolve_owner():
    global _owner
    with _owner_guard:
        if _owner is None:
            with _session() as session:
                rows = Sheets(session).values(SHEET_ID, ["'Участники'!A2:C"])[0]
            matches = [r for r in rows if r and sorted(normalized(r[0]).split()) == ['анна', 'платонова']]
            if len(matches) != 1 or len(matches[0]) < 2 or not str(matches[0][1]).isdigit():
                raise ValueError('Не найден единственный VK ID Анны Платоновой в участниках')
            if len(matches[0]) > 2 and str(matches[0][2]).casefold().strip() == 'выбыл':
                raise ValueError('Организатор отмечен как выбывший')
            _owner = int(matches[0][1])
            if _owner <= 0:
                raise ValueError('Некорректный VK ID организатора')
            logging.info('ADMIN_PUBLISH_OWNER verified vk_id=%s', _owner)
        return _owner


class GoogleDraftStore:
    def __init__(self, owner):
        self.owner = owner

    @contextmanager
    def locked(self):
        book = os.environ.get('QUEST_SPREADSHEET_ID', '').strip()
        if not book or book == SHEET_ID:
            raise ValueError('Для черновиков нужна отдельная книга квестов')
        with _session(write=True) as session:
            api = Sheets(session)
            owner = uuid.uuid4().hex
            metadata = dict(metadataId=LOCK_ID, metadataKey=LOCK_KEY, metadataValue=owner,
                            location={'spreadsheet': True}, visibility='DOCUMENT')
            try:
                api.batch(book, [{'createDeveloperMetadata': {'developerMetadata': metadata}}])
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 400:
                    current = api.get(book, fields='developerMetadata').get('developerMetadata', [])
                    if any(m.get('metadataId') == LOCK_ID for m in current):
                        raise Busy('Черновик занят, повтори действие через несколько секунд') from exc
                raise
            try:
                sid = api.ensure_tab(book, TAB, ['owner_vk_id', 'state_json'])
                rows = api.values(book, [f"'{TAB}'!A2:B"])[0]
                found = [(i+1, r) for i, r in enumerate(rows) if r and str(r[0]) == str(self.owner)]
                if len(found) > 1:
                    raise ValueError('Повторные состояния организатора')
                index, row = found[0] if found else (None, [])
                state = json.loads(row[1]) if len(row) > 1 else {'phase': 'closed', 'last_cid': 0}
                migrated = state.get('ordering_version') != 2
                if migrated:
                    logging.warning('ADMIN_PUBLISH order migration previous=%s phase=%s',
                                    state.get('last_cid'), state.get('phase'))
                    state['last_cid'] = 0
                    state['ordering_version'] = 2
                class Transaction:
                    def __init__(self):
                        self.state = state
                        self.exists = bool(found)
                        self.migrated = migrated
                    def save(tx, value):
                        nonlocal index
                        value = dict(value, ordering_version=2)
                        record = [str(self.owner), json.dumps(value, ensure_ascii=False)]
                        api.batch(book, [update(sid, index, 0, record) if index is not None else append(sid, record)])
                        if index is None:
                            index = len(rows) + 1
                        tx.state = value
                yield Transaction()
            finally:
                if not api.uncertain:
                    api.batch(book, [{'deleteDeveloperMetadata': {'dataFilter': {'developerMetadataLookup': {
                        'metadataId': LOCK_ID, 'metadataKey': LOCK_KEY, 'metadataValue': owner}}}}])


def keyboard(nonce, retry=False):
    actions = [('send', 'Проверить отправку' if retry else 'Отправить', 'positive')]
    if not retry:
        actions.append(('cancel', 'Отмена', 'negative'))
    return {'inline': True, 'buttons': [[{'action': {'type': 'text', 'label': label + ' ' + nonce[:8].upper(),
        'payload': json.dumps({'kind': 'admin_publish', 'nonce': nonce, 'action': action})}, 'color': color}
        for action, label, color in actions]]}


def content(message):
    text = str(message.get('text') or '').strip()
    photos = []
    if message.get('fwd_messages'):
        raise ValueError('Пришли текст и фотографии напрямую, без пересылки сообщений.')
    for attachment in message.get('attachments') or []:
        if attachment.get('type') == 'link':
            continue  # The URL remains in the literal message text.
        if attachment.get('type') != 'photo':
            raise ValueError('Поддерживаются текст и фотографии. Пришли картинку как фото, а не файл.')
        photo = attachment.get('photo') or {}
        owner, ident = photo.get('owner_id'), photo.get('id')
        if not isinstance(owner, int) or not owner or not isinstance(ident, int) or ident <= 0:
            raise ValueError('Не удалось прочитать фотографию.')
        key = photo.get('access_key') or ''
        if key and not re.fullmatch(r'[A-Za-z0-9_-]+', str(key)):
            raise ValueError('Некорректный ключ фотографии.')
        photos.append(f'photo{owner}_{ident}' + (f'_{key}' if key else ''))
    if not text and not photos:
        raise ValueError('Пришли текст, фотографию или текст с фотографиями одним сообщением.')
    if len(text) > 3900 or len(photos) > 10:
        raise ValueError('В одном сообщении можно до 3900 символов и до 10 фотографий.')
    return text, ','.join(photos)


class Publisher:
    def __init__(self, owner, store, send, now=time.time):
        self.owner, self.store, self.send, self.now = owner, store, send, now

    def reply(self, text, key, **kwargs):
        self.send(self.owner, text, random_id='admin-dm:' + str(key), **kwargs)

    def message(self, message):
        if int(message.get('from_id') or 0) != self.owner or int(message.get('peer_id') or 0) != self.owner:
            return False
        cid = int(message.get('conversation_message_id') or 0)
        if cid <= 0:
            return False
        command = re.fullmatch(r'(отправить|проверить отправку|отмена) ([a-f0-9]{8})',
                               normalized(message.get('text', '')))
        if command:
            with self.store.locked() as tx:
                nonce = tx.state.get('nonce', '')
                if not nonce or command[2] != nonce[:8]:
                    self.reply('Это кнопка другого черновика. Используй кнопки под последним предпросмотром.',
                               f'stale-button:{cid}')
                    return True
            return self.callback({'user_id': self.owner, 'peer_id': self.owner,
                'payload': {'kind': 'admin_publish', 'nonce': nonce,
                            'action': 'cancel' if command[1] == 'отмена' else 'send'}})
        with self.store.locked() as tx:
            state = dict(tx.state)
            logging.warning('ADMIN_PUBLISH_MESSAGE order=%s previous=%s phase=%s',
                            cid, state.get('last_cid', 0), state.get('phase'))
            if cid <= state.get('last_cid', 0):
                return True
            phase = state.get('phase', 'closed')
            expired = False
            received = min(self.now(), float(message.get('date') or self.now()))
            if phase in ('awaiting', 'preview') and received > state.get('expires', 0):
                state = {'phase': 'closed', 'last_cid': state.get('last_cid', 0)}
                phase = 'closed'
                expired = True
            text = normalized(message.get('text', ''))
            if phase == 'closed' and text != normalized(PHRASE):
                if expired:
                    tx.save({'phase': 'closed', 'last_cid': cid})
                    self.reply('Время черновика истекло. Начни снова с кодовой фразы.', f'expired-message:{cid}')
                    return True
                return False
            state['last_cid'] = cid
            if phase == 'sending':
                tx.save(state)
                self.reply('Предыдущая отправка ещё не подтверждена. Нажми кнопку ещё раз: дубля не будет.',
                           f'pending:{cid}', keyboard=keyboard(state['nonce'], retry=True))
                return True
            if text == normalized(PHRASE):
                state = {'phase': 'awaiting', 'last_cid': cid, 'expires': self.now() + TTL}
                tx.save(state)
                self.reply('🦉 Режим депеши открыт на 10 минут. Пришли текст и/или до 10 фото одним сообщением. '
                           'Новое сообщение заменяет черновик. Для выхода напиши «Отмена».', f'open:{cid}')
                return True
            if text == 'отмена':
                tx.save({'phase': 'closed', 'last_cid': cid})
                self.reply('Депеша отменена. Режим закрыт.', f'cancel:{cid}')
                return True
            try:
                body, photos = content(message)
            except ValueError as exc:
                tx.save(state)
                self.reply(str(exc), f'invalid:{cid}')
                return True
            state.update(phase='preview', nonce=uuid.uuid4().hex, text=body,
                         attachment=photos, expires=self.now() + TTL)
            tx.save(state)
            self.reply(body, f'preview:{state["nonce"]}', attachment=photos or None)
            self.reply('Так будет выглядеть сообщение в «Сычиной охоте». Отправить от лица Робосычика?',
                       f'controls:{state["nonce"]}', keyboard=keyboard(state['nonce']))
            logging.warning('ADMIN_PUBLISH_PREVIEW sent order=%s photos=%s', cid, bool(photos))
        return True

    def callback(self, event):
        payload = event.get('payload') or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return False
        if not isinstance(payload, dict) or payload.get('kind') != 'admin_publish':
            return False
        if int(event.get('user_id') or 0) != self.owner or int(event.get('peer_id') or 0) != self.owner:
            return True
        with self.store.locked() as tx:
            state = dict(tx.state)
            nonce, action = payload.get('nonce'), payload.get('action')
            if not nonce or nonce != state.get('nonce') or action not in ('send', 'cancel'):
                return True
            phase = state.get('phase')
            logging.warning('ADMIN_PUBLISH_CONFIRM action=%s phase=%s', action, phase)
            if phase == 'closed':
                self.reply('Эта депеша уже отправлена или отменена. Режим закрыт.', 'closed:' + nonce)
                return True
            if phase == 'preview' and self.now() > state.get('expires', 0):
                tx.save({'phase': 'closed', 'last_cid': state.get('last_cid', 0)})
                self.reply('Время черновика истекло. Начни снова с кодовой фразы.', 'expired:' + nonce)
                return True
            if phase not in ('preview', 'sending'):
                return True
            if action == 'cancel':
                if phase == 'sending':
                    self.reply('Отправка уже началась. Проверь её той же кнопкой.', 'uncertain:' + nonce,
                               keyboard=keyboard(nonce, retry=True))
                    return True
                tx.save({'phase': 'closed', 'nonce': nonce, 'last_cid': state.get('last_cid', 0)})
                self.reply('Депеша отменена. Режим закрыт.', 'cancelled:' + nonce)
                return True
            state['phase'] = 'sending'
            tx.save(state)
            try:
                delivery = self.send(TARGET, state['text'], attachment=state['attachment'] or None,
                                     random_id='admin-publish:' + nonce)
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc).startswith('VK messages.send error:'):
                    state['phase'] = 'preview'
                    tx.save(state)
                    self.reply('VK отклонил сообщение. Можно прислать исправленный черновик или отменить депешу.',
                               'rejected:' + nonce, keyboard=keyboard(nonce))
                    return True
                logging.warning('ADMIN_PUBLISH delivery not confirmed; draft retained')
                self.reply('VK пока не подтвердил отправку. Нажми «Проверить отправку»: повтор использует тот же номер депеши.',
                           'retry:' + nonce, keyboard=keyboard(nonce, retry=True))
                return True
            tx.save({'phase': 'closed', 'nonce': nonce, 'last_cid': state.get('last_cid', 0)})
            logging.warning('ADMIN_PUBLISH_SENT response=%s', delivery.get('response') if isinstance(delivery, dict) else bool(delivery))
            self.reply('🦉 Депеша отправлена в «Сычиную охоту». Режим закрыт.', 'sent:' + nonce)
        return True


def verified_message(message, token, owner, max_age=TTL):
    """The public callback body cannot authorize publishing by claiming a VK ID.

    Fetch incoming DM content independently from VK. The durable high-water CID
    rejects replays. Confirmation buttons carry a fresh private 128-bit nonce.
    """
    if int(message.get('peer_id') or 0) != owner:
        return None
    mid = int(message.get('id') or 0)
    cid = int(message.get('conversation_message_id') or 0)
    if mid <= 0 and cid <= 0:
        logging.warning('ADMIN_PUBLISH verification has no message identifier')
        return None
    method = 'messages.getById' if mid > 0 else 'messages.getByConversationMessageId'
    params = {'access_token': token, 'v': '5.199'}
    params.update({'message_ids': mid} if mid > 0 else {'peer_id': owner, 'conversation_message_ids': cid})
    group = os.environ.get('VK_GROUP_ID', '').strip()
    if group.isdigit():
        params['group_id'] = int(group)
    response = requests.post('https://api.vk.com/method/' + method, data=params, timeout=10)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        logging.warning('ADMIN_PUBLISH verification failed method=%s vk_code=%s', method, result['error'].get('error_code'))
        return None
    items = result.get('response', {}).get('items', [])
    matches = [m for m in items if (int(m.get('id') or 0) == mid if mid > 0 else
                                  int(m.get('conversation_message_id') or 0) == cid)
               and int(m.get('from_id') or 0) == owner and int(m.get('peer_id') or 0) == owner
               and not m.get('out') and (max_age is None or time.time() - int(m.get('date') or 0) <= max_age)]
    if len(matches) != 1:
        logging.warning('ADMIN_PUBLISH verification rejected method=%s items=%s', method, len(items))
        return None
    actual = dict(matches[0])
    # Private callbacks can omit conversation_message_id; VK's verified global
    # message ID is still monotonic for this private dialog.
    actual['conversation_message_id'] = int(actual.get('id') or actual.get('conversation_message_id') or 0)
    return actual


def handle_message(message, send, token):
    owner = resolve_owner()
    if int(message.get('peer_id') or 0) != owner:
        return False
    logging.warning('ADMIN_PUBLISH_CALLBACK mid=%s cid=%s author_present=%s',
                    message.get('id'), message.get('conversation_message_id'), bool(message.get('from_id')))
    actual = verified_message(message, token, owner)
    if not actual:
        return False
    try:
        return Publisher(owner, GoogleDraftStore(owner), send).message(actual)
    except Exception:
        logging.exception('ADMIN_PUBLISH draft operation failed')
        send(owner, 'Не удалось сохранить черновик. Повтори сообщение чуть позже.',
             random_id=f'admin-draft-error:{actual["conversation_message_id"]}')
        return True


def handle_callback(event, send, token):
    owner = resolve_owner()
    try:
        handled = Publisher(owner, GoogleDraftStore(owner), send).callback(event)
    except Exception:
        logging.exception('ADMIN_PUBLISH confirmation failed')
        # Keep the same draft/send identity when the storage outcome is uncertain.
        if int(event.get('user_id') or 0) == owner and int(event.get('peer_id') or 0) == owner:
            send(owner, 'Не удалось подтвердить действие. Нажми ту же кнопку ещё раз чуть позже.',
                 random_id='admin-action-error:' + str(event.get('event_id', '')))
        handled = True
    if (handled and int(event.get('user_id') or 0) == owner
            and int(event.get('peer_id') or 0) == owner and event.get('event_id')):
        try:
            response = requests.post('https://api.vk.com/method/messages.sendMessageEventAnswer',
                data={'access_token': token, 'v': '5.199', 'event_id': event['event_id'],
                      'user_id': owner, 'peer_id': owner, 'event_data': json.dumps({
                          'type': 'show_snackbar', 'text': 'Ответ — в личных сообщениях Робосычика.'}, ensure_ascii=False)}, timeout=10)
            response.raise_for_status()
        except Exception:
            logging.warning('ADMIN_PUBLISH callback acknowledgement not confirmed')
    return handled


def prepare(send=None, token=None):
    """Initialize storage and recover only a recent, verified owner command."""
    try:
        owner = resolve_owner()
        with GoogleDraftStore(owner).locked() as tx:
            if not tx.state:
                raise ValueError('Invalid draft store')
            if not tx.exists or tx.migrated:
                tx.save(tx.state)
        logging.warning('ADMIN_PUBLISH_READY owner_vk_id=%s version=4', owner)
        if send and token:
            recover_confirmed_publication(owner, send, token)
            recover_command(owner, send, token)
    except Exception:
        logging.exception('ADMIN_PUBLISH initialization failed')


def recover_confirmed_publication(owner, send, token):
    """One-off recovery of message 20, explicitly confirmed by its owner.

    The owner's reported click is recorded at 2026-11-12 19:04:30 UTC. Verify the
    exact original VK content, preserve the send identity, and skip any edited,
    cancelled or already closed draft. No other message can be published here.
    """
    from datetime import datetime, timezone
    approved_at = datetime(2026, 11, 12, 19, 4, 30, tzinfo=timezone.utc).timestamp()
    if owner != 54341305 or time.time() > approved_at + 3600:
        return
    store = GoogleDraftStore(owner)
    with store.locked() as tx:
        state = dict(tx.state)
        if state.get('last_cid') != 20 or state.get('phase') not in ('preview', 'sending'):
            logging.warning('ADMIN_PUBLISH_APPROVED_RECOVERY skipped phase=%s', state.get('phase'))
            return
        actual = verified_message({'id': 20, 'peer_id': owner}, token, owner, max_age=None)
        if (not actual or content(actual) != (state.get('text'), state.get('attachment'))
                or approved_at > state.get('expires', 0)):
            logging.warning('ADMIN_PUBLISH_APPROVED_RECOVERY snapshot mismatch')
            return
        nonce = state['nonce']
    Publisher(owner, store, send, now=lambda: approved_at).callback({
        'user_id': owner, 'peer_id': owner,
        'payload': {'kind': 'admin_publish', 'nonce': nonce, 'action': 'send'}})


def recover_command(owner, send, token):
    """Recover a recent activation or unprocessed draft; never publish to chat."""
    params = {'access_token': token, 'v': '5.199', 'peer_id': owner, 'count': 5}
    group = os.environ.get('VK_GROUP_ID', '').strip()
    if group.isdigit():
        params['group_id'] = int(group)
    response = requests.post('https://api.vk.com/method/messages.getHistory', data=params, timeout=10)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        logging.warning('ADMIN_PUBLISH recovery history unavailable vk_code=%s', result['error'].get('error_code'))
        return
    incoming = next((m for m in result.get('response', {}).get('items', [])
                     if int(m.get('from_id') or 0) == owner and not m.get('out')), None)
    if not incoming or time.time() - int(incoming.get('date') or 0) > TTL:
        return
    if normalized(incoming.get('text', '')) != normalized(PHRASE):
        with GoogleDraftStore(owner).locked() as tx:
            if tx.state.get('phase') not in ('awaiting', 'preview'):
                return
    actual = verified_message(incoming, token, owner)
    if actual:
        handled = Publisher(owner, GoogleDraftStore(owner), send).message(actual)
        logging.warning('ADMIN_PUBLISH_RECOVERY handled=%s mid=%s', handled, incoming.get('id'))
