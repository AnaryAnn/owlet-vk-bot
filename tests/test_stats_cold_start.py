import os
from pathlib import Path
import subprocess
import sys
import unittest


class ColdStartTests(unittest.TestCase):
    def test_concurrent_reads_and_writes_ignore_partially_initialized_compatibility_module(self):
        script = r'''
import sys, types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import app
import sheets_api
import website_store

# Reproduce the exact production failure condition without relying on timing.
partial = types.ModuleType('sheets_grid')
partial.__spec__ = types.SimpleNamespace(_initializing=True)
sys.modules['sheets_grid'] = partial

def check(_):
    session = Mock()
    response = Mock(status_code=200)
    response.json.return_value = {'sheets': [{'properties': {'title': 'Участники'},
        'data': [{'startRow': 1, 'rowData': [{'values': [
            {'userEnteredValue': {'stringValue': 'Тест'}}]}]}]}]}
    session.get.return_value = response
    rows = sheets_api.read_value_ranges(session, sheets_api.SHEET_ID, ["'Участники'!A2:C"])
    assert rows[0]['values'] == [['Тест']]
    api = website_store.Sheets(session)
    api.tabs = Mock(return_value={'week': {'sheetId': 1}, 'journal': {'sheetId': 2}})
    api.values = Mock(return_value=[[['header'], ['old']]])
    api.get = Mock(return_value={'sheets': [
        {'properties': {'sheetId': sid, 'gridProperties': {'rowCount': 1000, 'columnCount': 8}}}
        for sid in (1, 2)]})
    committed = Mock(status_code=200)
    committed.json.return_value = {}
    session.post.return_value = committed
    api.batch(sheets_api.SHEET_ID, [website_store.update(1, 7, 1, [20, 10, 'date']),
        website_store.append(2, ['id', 'fingerprint', '{}'])])
    assert session.post.call_count == 1

with ThreadPoolExecutor(max_workers=16) as pool:
    list(pool.map(check, range(64)))
'''
        env = dict(os.environ)
        env.pop('RENDER', None)
        completed = subprocess.run([sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
