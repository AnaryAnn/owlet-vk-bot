"""Проверка доступа и схемы трёх книг. Не записывает данные и не отправляет VK."""
import json
import os

from sheets_api import _session, SHEET_ID
from website_store import ARTIFACTS_ID, Sheets, LOCK_ID


def check():
    quests = os.environ.get('QUEST_SPREADSHEET_ID', '').strip()
    if not quests:
        return {'success': False, 'missing': ['QUEST_SPREADSHEET_ID']}
    expected = [(SHEET_ID, ['Участники']),
        (ARTIFACTS_ID, ['Инвентарь', 'Артефакты']),
        (quests, ['Квесты', 'Этапы', 'Журнал', 'Исходящие', 'Процентные рубежи'])]
    result = []
    with _session(write=True) as session:
        api = Sheets(session)
        for book, names in expected:
            data = api.get(book, fields='sheets.properties,developerMetadata')
            tabs = {t['properties']['title'] for t in data.get('sheets', [])}
            missing = [n for n in names if n not in tabs]
            locked = any(m.get('metadataId') == LOCK_ID for m in data.get('developerMetadata', []))
            result.append(dict(missingSheets=missing, locked=locked, success=not missing and not locked))
    return dict(success=all(r['success'] for r in result), books=result,
        note='Проверено чтение и структура; право записи проверяется отдельно на согласованном запросе')


if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False))
