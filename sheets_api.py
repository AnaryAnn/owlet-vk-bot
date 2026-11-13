"""Read-only Google Sheets adapter for the current-week website statistics."""
import json
import math
import os
import re
import time
import logging
import netrc  # Load stdlib before Google auth starts background refresh threads.
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_ID = os.environ.get("STATS_SPREADSHEET_ID", "1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60")
TIMEZONE = os.environ.get("STATS_TIMEZONE", "Europe/Moscow")


def bounds(a1):
    match = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^!]+))!([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?", a1)
    if not match:
        raise ValueError('Unsupported A1 range')
    quoted, plain, col, row, end_col, end_row = match.groups()
    def column(value):
        result = 0
        for char in value:
            result = result * 26 + ord(char) - ord('A') + 1
        return result - 1
    first_row = int(row) - 1 if row else 0
    return (quoted.replace("''", "'") if quoted else plain,
            first_row, column(col),
            int(end_row) if end_row else (None if end_col or not row else first_row + 1),
            column(end_col or col) + 1)


def grid_ranges(payload, ranges, formatted=False):
    # Google returns sheets in workbook order, not necessarily request order.
    sheets = {s['properties']['title']: s for s in payload.get('sheets', [])}
    output = []
    for a1 in ranges:
        title, r0, c0, r1, c1 = bounds(a1)
        if title not in sheets:
            raise ValueError('Requested sheet is missing from Google response')
        cells = {}
        for block in sheets[title].get('data', []):
            for row_offset, row in enumerate(block.get('rowData', [])):
                r = block.get('startRow', 0) + row_offset
                if r < r0 or (r1 is not None and r >= r1):
                    continue
                for col_offset, cell in enumerate(row.get('values', [])):
                    c = block.get('startColumn', 0) + col_offset
                    if not c0 <= c < c1:
                        continue
                    value = cell.get('userEnteredValue', {})
                    if 'formulaValue' in value:
                        raise ValueError('Formula found in an input-data range; refusing to replace it with an empty value')
                    if 'numberValue' in value:
                        value = value['numberValue']
                    elif 'stringValue' in value:
                        value = value['stringValue']
                    elif 'boolValue' in value:
                        value = value['boolValue']
                    else:
                        value = ''
                    if value != '':
                        cells[r, c] = value
        rows = []
        if cells:
            for r in range(r0, max(r for r, _ in cells) + 1):
                row = [cells.get((r, c), '') for c in range(c0, c1)]
                while row and row[-1] == '':
                    row.pop()
                rows.append(row)
        output.append({'range': a1, 'values': rows})
    return output

def read_response(session, url, params=None, timeout=25):
    """Повторяем только чтение; записи с неопределённым результатом не трогаем."""
    for attempt in range(2):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise
        except requests.HTTPError as exc:
            if attempt or exc.response is None or exc.response.status_code not in (429, 500, 502, 503, 504):
                raise
        logging.warning('Повтор чтения Google Sheets после временной ошибки; записи не повторяются')
        time.sleep(.3)


class SheetsSession(AuthorizedSession):
    """Explicit service-account auth; never consult ambient netrc credentials."""
    def __init__(self, credentials):
        self._owned_auth_session = requests.Session()
        self._owned_auth_session.trust_env = False
        super().__init__(credentials, auth_request=Request(self._owned_auth_session),
                         refresh_timeout=20)
        self.trust_env = False

    def close(self):
        try:
            super().close()
        finally:
            self._owned_auth_session.close()


def _session(write=False):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"] if write else SCOPES
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return SheetsSession(creds)


def read_value_ranges(session, book, ranges, formatted=False, timeout=25):
    base = f'https://sheets.googleapis.com/v4/spreadsheets/{book}'
    if book == SHEET_ID:
        response = read_response(session, base, params={
            'ranges': ranges, 'includeGridData': 'true',
            'fields': 'sheets(properties.title,data(startRow,startColumn,rowData.values.userEnteredValue))'
        }, timeout=timeout)
        return grid_ranges(response.json(), ranges, formatted)
    response = read_response(session, base + '/values:batchGet', params={
        'ranges': ranges, 'valueRenderOption': 'FORMATTED_VALUE' if formatted else 'UNFORMATTED_VALUE'
    }, timeout=timeout)
    return response.json().get('valueRanges', [])


def _number(value):
    if value is None or str(value).strip() == "":
        return 0
    text = "".join(str(value).split()).replace(",", ".")
    n = float(text)
    if not math.isfinite(n):
        raise ValueError("Statistics value must be finite")
    return int(n) if n.is_integer() else n


def current_week():
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    start = today - timedelta(days=(today.weekday() - 1) % 7)
    end = start + timedelta(days=6)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "startDisplay": start.strftime("%d.%m.%Y"), "endDisplay": end.strftime("%d.%m.%Y"),
            "sheetName": f'{start:%d.%m}-{end:%d.%m.%y}'}


def _quote(name):
    return "'" + name.replace("'", "''") + "'"


def read_current_stats(week=None):
    week = current_week() if week is None else week
    ranges = [f"{_quote('Участники')}!A2:C", f"{_quote(week['sheetName'])}!B2",
              f"{_quote(week['sheetName'])}!A8:D"]
    with _session() as session:
        data = read_value_ranges(session, SHEET_ID, ranges, formatted=True)
    if len(data) != 3:
        raise RuntimeError("Unexpected Google Sheets response")
    participant_rows = data[0].get("values", [])
    participants = []
    for r in participant_rows:
        name = str(r[0]).strip() if r else ""
        if name:
            status = str(r[2]).strip().lower() if len(r) > 2 else "активен"
            participants.append({"name": name, "vkId": str(r[1]).strip() if len(r) > 1 else "",
                                 "status": "выбыл" if status == "выбыл" else "активен"})
    goal_rows = data[1].get("values", [])
    goal_value = goal_rows[0][0] if goal_rows and goal_rows[0] else ""
    goal = None if str(goal_value).strip() == "" else _number(goal_value)
    records = []
    for r in data[2].get("values", []):
        if not r or not str(r[0]).strip():
            continue
        plan = r[1] if len(r) > 1 else ""
        fact = r[2] if len(r) > 2 else ""
        if str(plan).strip() == "" and str(fact).strip() == "":
            continue
        records.append({"weekStart": week["start"], "weekEnd": week["end"],
                        "name": str(r[0]).strip(), "plan": _number(plan), "fact": _number(fact),
                        "updated": str(r[3]) if len(r) > 3 else ""})
    return {"success": True, "currentWeek": week, "teamGoal": goal,
            "peopleCount": len(participants),
            "activePeopleCount": sum(p["status"] == "активен" for p in participants),
            "participants": participants, "currentRecords": records,
            "weeks": [{"sheetName": week["sheetName"], "weekStart": week["start"],
                       "weekEnd": week["end"], "goal": goal, "records": records}]}



def _history_week(title):
    """Распознаём только недельные листы, включая переход через Новый год."""
    if not re.fullmatch(r"\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}", title):
        return None
    try:
        start_text, end_text = title.split('-')
        end = datetime.strptime(end_text, '%d.%m.%y').date()
        start = datetime.strptime(start_text + f'.{end.year}', '%d.%m.%Y').date()
        if start > end:
            start = start.replace(year=end.year - 1)
        if (end - start).days != 6 or start.weekday() != 1:
            return None
        return dict(sheetName=title, weekStart=start.isoformat(), weekEnd=end.isoformat())
    except ValueError:
        return None


def read_stats_history():
    """Только чтение: список недель и их отчёты, пакетами по 40 листов."""
    with _session() as session:
        base = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
        response = read_response(session, base, params={'fields': 'sheets.properties.title'}, timeout=25)
        response.raise_for_status()
        weeks = [w for sheet in response.json().get('sheets', [])
                 if (w := _history_week(sheet.get('properties', {}).get('title', '')))]
        weeks = sorted((w for w in weeks if w['weekStart'] <= current_week()['start']),
                       key=lambda w: w['weekStart'])
        for offset in range(0, len(weeks), 40):
            batch = weeks[offset:offset + 40]
            ranges = [f"{_quote(w['sheetName'])}!{cells}" for w in batch for cells in ('B2', 'A8:D')]
            values = read_value_ranges(session, SHEET_ID, ranges, formatted=True)
            if len(values) != len(ranges):
                raise RuntimeError('Unexpected history response')
            for i, week in enumerate(batch):
                goal_rows = values[2*i].get('values', [])
                goal = goal_rows[0][0] if goal_rows and goal_rows[0] else ''
                week['goal'] = _number(goal) if str(goal).strip() else None
                records = []
                for row in values[2*i+1].get('values', []):
                    if not row or not str(row[0]).strip():
                        continue
                    plan = row[1] if len(row) > 1 else ''
                    fact = row[2] if len(row) > 2 else ''
                    if not str(plan).strip() and not str(fact).strip():
                        continue
                    records.append(dict(name=str(row[0]).strip(), plan=_number(plan), fact=_number(fact),
                        weekStart=week['weekStart'], weekEnd=week['weekEnd'],
                        updated=str(row[3]) if len(row) > 3 else ''))
                week['records'] = records
                week['totalMice'] = sum(r['fact'] for r in records)
                week['activeOwlets'] = len({r['name'] for r in records if r['fact'] > 0})
    return dict(success=True, weeks=weeks, currentWeek=current_week())
