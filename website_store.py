"""Прямая запись отчётов в Sheets после полного отключения Apps Script.

Замок не имеет автоматического протухания: при неопределённом результате
записи он сохраняется до проверки оператором, чтобы поздний ответ Google
не повредил более новую запись. См. WEBSITE_MIGRATION.md.
"""
import hashlib
import json
import os
import random
import re
import time
import uuid
from urllib.parse import quote
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from report_comments import append_overplan_comment
from sheets_api import bounds, _session, _quote, _number, current_week, SHEET_ID, TIMEZONE, read_response

ARTIFACTS_ID = os.environ.get('ARTIFACTS_SPREADSHEET_ID',
    '1xpcRGO2i2nw6TAik3NskITxwAM6e7831LaSigDR3sRk')
LOCK_ID = 1937152401
LOCK_KEY = 'owlet_shared_write_v1'
STATS_LOCK_ID = 1937152402
STATS_LOCK_KEY = 'owlet_stats_write_v2'
STATS_LOCK_BOOK = os.environ.get('STATS_LOCK_SPREADSHEET_ID') or os.environ.get('QUEST_SPREADSHEET_ID') or ARTIFACTS_ID
BASE = 'https://sheets.googleapis.com/v4/spreadsheets/'


class Busy(Exception):
    pass


class Conflict(Exception):
    pass


class Uncertain(Exception):
    pass


def payload_values(data):
    if not isinstance(data, dict):
        raise ValueError('Ожидается объект JSON')
    name = data.get('name')
    report_id = data.get('requestId')
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError('Некорректный участник')
    if not isinstance(report_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,100}', report_id):
        raise ValueError('Некорректный requestId')
    numbers = []
    for key in ('plan', 'fact'):
        n = data.get(key)
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not 0 <= n <= 1_000_000_000:
            raise ValueError('План и факт должны быть целыми неотрицательными числами')
        if int(n) != n:
            raise ValueError('План и факт должны быть целыми числами')
        numbers.append(int(n))
    return name.strip(), *numbers, 'website-' + report_id


def fingerprint(name, plan, fact):
    raw = json.dumps([name, plan, fact], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def cell(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {'userEnteredValue': {'numberValue': value}}
    return {'userEnteredValue': {'stringValue': str(value)}}


def update(sheet_id, row, column, values):
    return {'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': row,
                                     'columnIndex': column},
        'rows': [{'values': [cell(v) for v in values]}], 'fields': 'userEnteredValue'}}


def append(sheet_id, values):
    return {'appendCells': {'sheetId': sheet_id,
        'rows': [{'values': [cell(v) for v in values]}], 'fields': 'userEnteredValue'}}


class Sheets:
    def __init__(self, session):
        self.session = session
        self.uncertain = False
        self._tabs_cache = {}

    def get(self, book, suffix='', **params):
        r = read_response(self.session, BASE + book + suffix, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def batch(self, book, requests_list):
        if book == SHEET_ID and requests_list and all(
                set(w) <= {'updateCells', 'appendCells'} and len(w) == 1 for w in requests_list):
            return self._literal_batch(book, requests_list)
        if any(k in w for w in requests_list for k in ('addSheet', 'deleteSheet', 'updateSheetProperties')):
            self._tabs_cache.pop(book, None)
        return self._post(book, ':batchUpdate', {'requests': requests_list})

    def _post(self, book, suffix, payload, params=None):
        try:
            kwargs = {'params': params} if params else {}
            r = self.session.post(BASE + book + suffix, json=payload, timeout=20, **kwargs)
        except requests.RequestException as exc:
            self.uncertain = True
            raise Uncertain('Результат записи требует проверки') from exc
        if r.status_code >= 500:
            self.uncertain = True
            raise Uncertain('Результат записи требует проверки')
        r.raise_for_status()
        return r.json()

    def _literal_batch(self, book, requests_list):
        """Reserve append capacity, then commit data and its journal atomically.

        A reservation contains no report ID or success result. A failed commit
        therefore cannot be mistaken for a saved report on a retry.
        """
        def column(index):
            result = ''
            index += 1
            while index:
                index, remainder = divmod(index - 1, 26)
                result = chr(65 + remainder) + result
            return result
        prepared = []
        has_formula = False
        for request in requests_list:
            operation = request.get('updateCells') or request.get('appendCells')
            if operation.get('fields') != 'userEnteredValue' or not operation.get('rows'):
                return self._post(book, ':batchUpdate', {'requests': requests_list})
            rows = []
            for row in operation['rows']:
                cells = []
                for cell_data in row.get('values', []):
                    value = cell_data.get('userEnteredValue', {})
                    if set(value) - {'stringValue', 'numberValue', 'boolValue', 'formulaValue'}:
                        raise ValueError('Unsupported cell value')
                    has_formula |= 'formulaValue' in value
                    cells.append(value)
                rows.append(cells)
            prepared.append((operation, rows, 'appendCells' in request))
        tabs = {p['sheetId']: title for title, p in self.tabs(book).items()}
        data = []
        for operation, rows, is_append in prepared:
            start = operation if is_append else operation['start']
            title = tabs[start['sheetId']]
            width = max(len(row) for row in rows)
            if not width:
                raise ValueError('Empty update')
            if is_append:
                # The last occupied row anchors append below all existing data.
                existing = self.values(book, [f'{_quote(title)}!A:{column(width - 1)}'])[0]
                anchor = max(1, len(existing))
                reserved = self._post(book,
                    '/values/' + quote(f'{_quote(title)}!A{anchor}:{column(width-1)}{anchor}', safe='') + ':append',
                    {'values': [['reserved-' + uuid.uuid4().hex] for _ in rows]},
                    {'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'})
                _, row_index, _, _, _ = bounds(reserved['updates']['updatedRange'])
                if row_index != len(existing):
                    self.uncertain = True
                    raise Uncertain('Не подтверждено положение служебной строки')
                col_index = 0
            else:
                row_index, col_index = start.get('rowIndex', 0), start.get('columnIndex', 0)
            values = []
            for row in rows:
                converted = []
                for cell_value in row:
                    value = next(iter(cell_value.values()), '')
                    if has_formula and isinstance(value, str) and 'formulaValue' not in cell_value:
                        value = "'" + value  # Preserve literal strings in USER_ENTERED mode.
                    converted.append(value)
                values.append(converted)
            data.append({'range': f'{_quote(title)}!{column(col_index)}{row_index+1}:'
                        f'{column(col_index+width-1)}{row_index+len(rows)}', 'values': values})
        return self._post(book, '/values:batchUpdate',
            {'valueInputOption': 'USER_ENTERED' if has_formula else 'RAW', 'data': data})

    def values(self, book, ranges, formatted=False):
        if book == SHEET_ID:
            from sheets_api import read_value_ranges
            return [v.get('values', []) for v in read_value_ranges(
                self.session, book, ranges, formatted=formatted, timeout=20)]
        return [v.get('values', []) for v in self.get(book, '/values:batchGet',
            ranges=ranges, valueRenderOption='FORMATTED_VALUE' if formatted else 'UNFORMATTED_VALUE').get('valueRanges', [])]

    def tabs(self, book):
        if book not in self._tabs_cache:
            self._tabs_cache[book] = {s['properties']['title']: s['properties'] for s in self.get(book,
                fields='sheets(properties(sheetId,title))').get('sheets', [])}
        return self._tabs_cache[book]

    def ensure_tab(self, book, title, headers, hidden=True):
        tabs = self.tabs(book)
        if title in tabs:
            return tabs[title]['sheetId']
        used = {s['sheetId'] for s in tabs.values()}
        new_id = random.SystemRandom().randrange(1, 2**30)
        while new_id in used:
            new_id = random.SystemRandom().randrange(1, 2**30)
        self.batch(book, [{'addSheet': {'properties': {
            'sheetId': new_id, 'title': title, 'hidden': hidden}}},
            update(new_id, 0, 0, headers)])
        return new_id

    @contextmanager
    def locked(self, book):
        lock_id, lock_key = LOCK_ID, LOCK_KEY
        if book == SHEET_ID:
            book = STATS_LOCK_BOOK
            lock_id, lock_key = STATS_LOCK_ID, STATS_LOCK_KEY
            if book == SHEET_ID:
                raise ValueError('The statistics lock must use a separate workbook')
        owner = str(uuid.uuid4())
        metadata = {'metadataId': lock_id, 'metadataKey': lock_key,
            'metadataValue': owner, 'location': {'spreadsheet': True}, 'visibility': 'DOCUMENT'}
        try:
            self.batch(book, [{'createDeveloperMetadata': {'developerMetadata': metadata}}])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                try:
                    message = str(exc.response.json().get('error', {}).get('message', '')).lower()
                except (ValueError, TypeError, AttributeError):
                    message = ''
                # Точный отказ в создании уже существующего metadataId остаётся
                # конфликтом, даже если владелец успел отпустить замок до чтения.
                duplicate = (str(lock_id) in message and 'metadata' in message
                             and ('already exists' in message or 'already in use' in message))
                if not duplicate:
                    current = self.get(book, fields='developerMetadata').get('developerMetadata', [])
                    duplicate = any(m.get('metadataId') == lock_id and m.get('metadataKey') == lock_key
                                    for m in current)
                if duplicate:
                    raise Busy('Таблица занята. Повторите запрос с тем же идентификатором') from exc
            raise
        try:
            yield
        finally:
            # При потере ответа Google замок остаётся; нельзя пропускать новую запись.
            if not self.uncertain:
                self.batch(book, [{'deleteDeveloperMetadata': {'dataFilter': {
                    'developerMetadataLookup': {'metadataId': lock_id,
                        'metadataKey': lock_key, 'metadataValue': owner}}}}])


def stamp():
    return datetime.now(ZoneInfo(TIMEZONE)).strftime('%d.%m.%Y %H:%M:%S')


def _journal(api):
    tab = api.ensure_tab(SHEET_ID, '_WebsiteReports', ['request_id', 'fingerprint', 'result_json'])
    rows = api.values(SHEET_ID, ["'_WebsiteReports'!A2:B"])[0]
    return tab, rows


def save_report(api, name, plan, fact, report_id):
    with api.locked(SHEET_ID):
        journal_id, journal = _journal(api)
        digest = fingerprint(name, plan, fact)
        for i, row in enumerate(journal):
            if row and row[0] == report_id:
                if len(row) < 2 or row[1] != digest:
                    raise Conflict('requestId уже использован для других данных')
                saved = api.values(SHEET_ID, [f"'_WebsiteReports'!C{i + 2}"])[0]
                return json.loads(saved[0][0]), journal_id, i + 1
        week = current_week()
        tabs = api.tabs(SHEET_ID)
        participants = api.values(SHEET_ID, ["'Участники'!A2:C"])[0]
        found = [r for r in participants if r and str(r[0]).strip() == name]
        if len(found) != 1:
            raise ValueError('Участник не найден или имя неоднозначно')
        person = found[0]
        vk_id = str(person[1]).strip() if len(person) > 1 else ''
        if not vk_id.isdigit():
            raise ValueError('Участник должен иметь привязанный VK ID')
        if len(person) > 2 and str(person[2]).strip().lower() == 'выбыл':
            raise ValueError('Участник выбыл из беседы')
        title = week['sheetName']
        if title not in tabs:
            from native_storage import ensure_week
            ensure_week(api, participants)
            tabs = api.tabs(SHEET_ID)
        rows = api.values(SHEET_ID, [f'{_quote(title)}!A8:D'])[0]
        matches = [(i + 7, r) for i, r in enumerate(rows) if r and str(r[0]).strip() == name]
        if len(matches) != 1:
            raise Busy('Строка участника отсутствует или неоднозначна. Нужна синхронизация')
        index, old = matches[0]
        previous_fact = _number(old[2]) if len(old) > 2 else 0
        previous_plan = _number(old[1]) if len(old) > 1 else 0
        changed = len(old) < 4 or not old[3] or previous_plan != plan or previous_fact != fact
        result = {'success': True, 'requestId': report_id, 'name': name, 'vkId': vk_id,
            'plan': plan, 'fact': fact, 'week': week, 'previousFact': previous_fact,
            'artifactEligible': fact > previous_fact, 'changed': bool(changed),
            'artifactStatus': 'pending' if fact > previous_fact else 'skipped',
            'notificationStatus': 'pending' if changed else 'skipped'}
        journal_row = len(journal) + 1
        # Строка отчёта и защита от повтора — одна атомарная запись Google.
        api.batch(SHEET_ID, [update(tabs[title]['sheetId'], index, 1, [plan, fact, stamp() if changed else old[3]]),
            append(journal_id, [report_id, digest, json.dumps(result, ensure_ascii=False)])])
        return result, journal_id, journal_row


RARITIES = [('обычный', 65, '⚪'), ('необычный', 22, '🟢'),
            ('редкий', 9, '🔵'), ('эпический', 3, '🟣'), ('легендарный', 1, '🟡')]


def artifact_drop(api, report, rng=None):
    rng = rng or random.SystemRandom()
    with api.locked(ARTIFACTS_ID):
        log_id = api.ensure_tab(ARTIFACTS_ID, '_ArtifactDrops',
            ['report_id', 'timestamp', 'vk_id', 'name', 'plan', 'fact', 'chance', 'result_json'])
        logs = api.values(ARTIFACTS_ID, ["'_ArtifactDrops'!A2:H"])[0]
        for row in logs:
            if row and row[0] == report['requestId']:
                return json.loads(row[7])
        chance = .20 if report['fact'] >= report['plan'] else .10
        result = {'success': True, 'attempted': True, 'dropped': False, 'chance': chance}
        writes = []
        if rng.random() < chance:
            roll = rng.random() * 100
            selected = RARITIES[-1]
            for item in RARITIES:
                roll -= item[1]
                if roll < 0:
                    selected = item
                    break
            catalog = api.values(ARTIFACTS_ID, ["'Артефакты'!A2:E"])[0]
            choices = [r for r in catalog if len(r) >= 3 and str(r[0]).strip() and str(r[2]).strip()
                and str(r[1]).strip().lower().replace('ё', 'е') == selected[0]]
            if not choices:
                result['error'] = 'Нет артефактов выбранной редкости'
                api.batch(ARTIFACTS_ID, [append(log_id, [report['requestId'], stamp(),
                    report['vkId'], report['name'], report['plan'], report['fact'], chance,
                    json.dumps(result, ensure_ascii=False)])])
                return result
            a = rng.choice(choices)
            inventory = api.tabs(ARTIFACTS_ID)
            if 'Инвентарь' not in inventory:
                raise Busy('Лист «Инвентарь» ещё не создан')
            inventory_id = inventory['Инвентарь']['sheetId']
            rows = api.values(ARTIFACTS_ID, ["'Инвентарь'!A2:H"])[0]
            found = [(i + 1, r) for i, r in enumerate(rows) if len(r) >= 3
                and str(r[0]).strip() == report['vkId'] and str(r[2]).strip() == str(a[0]).strip()]
            if len(found) > 1:
                raise Conflict('В инвентаре найдены неоднозначные строки')
            count = 1
            if found:
                index, old = found[0]
                count = int(_number(old[5])) + 1
                writes.append(update(inventory_id, index, 5, [count, old[6], stamp()]))
            else:
                writes.append(append(inventory_id, [report['vkId'], report['name'], str(a[0]),
                    str(a[2]), str(a[1]), 1, stamp(), stamp()]))
            result.update(dropped=True, artifact={'id': str(a[0]), 'name': str(a[2]),
                'rarity': str(a[1]), 'description': str(a[3]) if len(a) > 3 else '',
                'vkPhoto': str(a[4]) if len(a) > 4 else '', 'quantity': count,
                'emoji': selected[2], 'duplicate': bool(found)})
        writes.append(append(log_id, [report['requestId'], stamp(), report['vkId'],
            report['name'], report['plan'], report['fact'], chance, json.dumps(result, ensure_ascii=False)]))
        api.batch(ARTIFACTS_ID, writes)
        return result


def send_notification(report):
    message = f"🦉 Данные внесены через сайт!\n{report['name']}: план {report['plan']} 🐭, факт {report['fact']} 🐭"
    artifact = report.get('artifact', {}).get('artifact') or {}
    if artifact:
        message += f"\n\n{artifact.get('emoji', '✨')} Выпал артефакт: {artifact['name']}"
        if artifact.get('description'):
            message += '\n' + artifact['description']
    message = append_overplan_comment(message, report['plan'], report['fact'], report['requestId'])
    random_id = int.from_bytes(hashlib.sha256(report['requestId'].encode()).digest()[:4], 'big') & 0x7fffffff
    payload = {'access_token': os.environ.get('VK_TOKEN', ''), 'v': '5.199',
        'peer_id': 2000000002, 'random_id': random_id or 1, 'message': message}
    photo = artifact.get('vkPhoto', '')
    if re.fullmatch(r'photo-?\d+_\d+', photo):
        payload['attachment'] = photo
    r = requests.post('https://api.vk.com/method/messages.send', data=payload, timeout=12)
    r.raise_for_status()
    if 'error' in r.json():
        raise Busy('Не удалось отправить уведомление VK')


def submit_report(data):
    if os.environ.get('NATIVE_STORAGE_ENABLED', '').lower() != 'true':
        raise Busy('Полный переход на Python ещё не завершён')
    name, plan, fact, report_id = payload_values(data)
    with _session(write=True) as session:
        api = Sheets(session)
        report, journal_id, journal_row = save_report(api, name, plan, fact, report_id)
        try:
            # Не держим замок статистики при обращении к другой таблице.
            artifact = artifact_drop(api, report) if report['artifactStatus'] == 'pending' else None
            with api.locked(SHEET_ID):
                latest = api.values(SHEET_ID, [f"'_WebsiteReports'!C{journal_row + 1}"])[0]
                report = json.loads(latest[0][0])
                if report['artifactStatus'] == 'pending' and artifact is not None:
                    report['artifact'] = artifact
                    report['artifactStatus'] = 'done'
                    api.batch(SHEET_ID, [update(journal_id, journal_row, 2,
                        [json.dumps(report, ensure_ascii=False)])])
                if report['notificationStatus'] == 'pending':
                    send_notification(report)
                    report['notificationStatus'] = 'sent'
                    api.batch(SHEET_ID, [update(journal_id, journal_row, 2,
                        [json.dumps(report, ensure_ascii=False)])])
        except (Busy, Conflict, Uncertain, requests.RequestException, ValueError, KeyError):
            # Запись уже сохранена. Клиент может повторить тот же requestId.
            report['warning'] = 'Данные сохранены. Артефакт или уведомление ещё не подтверждены; повторите запрос.'
        return report
