"""Read-only Google Sheets adapter for the current-week website statistics."""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_ID = os.environ.get("STATS_SPREADSHEET_ID", "1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60")
TIMEZONE = os.environ.get("STATS_TIMEZONE", "Europe/Moscow")


def _session():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


def _number(value):
    if value is None or str(value).strip() == "":
        return 0
    text = str(value).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    n = float(text)
    return int(n) if n.is_integer() else n


def _week():
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    start = today - timedelta(days=(today.weekday() - 1) % 7)
    end = start + timedelta(days=6)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "startDisplay": start.strftime("%d.%m.%Y"), "endDisplay": end.strftime("%d.%m.%Y"),
            "sheetName": f'{start:%d.%m}-{end:%d.%m.%y}'}


def _quote(name):
    return "'" + name.replace("'", "''") + "'"


def read_current_stats():
    week = _week()
    ranges = [f"{_quote('Участники')}!A2:C", f"{_quote(week['sheetName'])}!B2",
              f"{_quote(week['sheetName'])}!A8:D"]
    response = _session().get(
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
