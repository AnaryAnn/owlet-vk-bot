"""Read-only Google Sheets adapter for the current-week website statistics."""
import json
import math
import os
import re
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_ID = os.environ.get("STATS_SPREADSHEET_ID", "1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60")
TIMEZONE = os.environ.get("STATS_TIMEZONE", "Europe/Moscow")


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


def _session(write=False):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"] if write else SCOPES
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return AuthorizedSession(creds)


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
        response = read_response(session,
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchGet",
            params={"ranges": ranges, "valueRenderOption": "FORMATTED_VALUE"}, timeout=25)
        response.raise_for_status()
        data = response.json().get("valueRanges", [])
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
            response = read_response(session, base + '/values:batchGet', params={
                'ranges': ranges, 'valueRenderOption': 'FORMATTED_VALUE'}, timeout=25)
            response.raise_for_status()
            values = response.json().get('valueRanges', [])
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
