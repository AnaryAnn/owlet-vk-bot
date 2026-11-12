"""Команды для внешнего cron (Render/GitHub/свой сервер), без Google-триггеров.

Запуск: python native_jobs.py morning --dry-run
Реальный запуск требует NATIVE_STORAGE_ENABLED=true и одобренного расписания.
"""
import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import os

from sheets_api import _session, SHEET_ID, TIMEZONE
from website_store import Sheets, append, update, stamp
from native_storage import enabled

JOBS = ('morning', 'evening', 'statistics', 'sync', 'quests', 'cleanup', 'daily')


def configured_schedule():
    override = os.environ.get('NATIVE_SCHEDULES_JSON')
    if override is not None:
        return json.loads(override)
    return json.loads(Path(__file__).with_name('native_schedule.json').read_text())


def due_jobs(schedule, now=None):
    now = now or datetime.now(ZoneInfo(TIMEZONE))
    minute = now.hour * 60 + now.minute
    result = []
    if not isinstance(schedule, list) or len(schedule) > 20: raise ValueError('Некорректное расписание')
    for item in schedule:
        if not isinstance(item, dict) or item.get('job') not in JOBS: raise ValueError('Неизвестное задание')
        if 'everyMinutes' in item:
            interval = item['everyMinutes']
            if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 1440:
                raise ValueError('Интервал должен быть от 5 до 1440 минут')
            due = minute % interval < 5
        else:
            hour, at = item.get('hour'), item.get('minute', 0)
            days = item.get('weekdays', list(range(7)))
            if (not isinstance(hour, int) or not 0 <= hour <= 23 or not isinstance(at, int)
                    or not 0 <= at <= 59 or not isinstance(days, list)
                    or any(not isinstance(d, int) or not 0 <= d <= 6 for d in days)):
                raise ValueError('Некорректное время задания')
            due = now.weekday() in days and 0 <= minute - hour * 60 - at < 30
        if due and item['job'] not in result: result.append(item['job'])
    return result


def run(job, dry_run=False):
    if job not in JOBS: raise ValueError('Неизвестное задание')
    now = datetime.now(ZoneInfo(TIMEZONE))
    slot = now.strftime('%Y-%m-%d') if job in ('morning', 'evening', 'statistics') else str(int(now.timestamp()) // 300)
    key = f'{job}:{slot}'
    if dry_run: return dict(success=True, dryRun=True, job=job, key=key, timezone=TIMEZONE)
    enabled()
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(SHEET_ID):
            sid = api.ensure_tab(SHEET_ID, '_NativeJobs', ['job_key', 'status', 'started', 'finished'])
            rows = api.values(SHEET_ID, ["'_NativeJobs'!A2:D"])[0]
            if any(r and r[0] == key for r in rows):
                return dict(success=True, skipped=True, key=key,
                    reason='Задание уже запускалось; неполное выполнение нужно проверить вручную')
            index = len(rows) + 1
            started = stamp()
            api.batch(SHEET_ID, [append(sid, [key, 'started', started, ''])])
    import app
    if job in ('morning', 'evening'): result = app.generate_scheduled_digest(job)
    elif job == 'statistics': result = app.generate_scheduled_stats()
    elif job == 'sync': result = app.sync_chat_participants(app.STATS_PEER_ID)
    elif job == 'quests': result = app.quest_tick(app.google_get, app.vk_send, app.STATS_PEER_ID)
    elif job == 'daily':
        from daily_tasks import sweep
        result = sweep()
    else: result = app.digest_post({'action': 'cleanup', 'hours': 24})
    if isinstance(result, dict) and result.get('success') is False:
        raise RuntimeError('Задание не подтвердило успех')
    with _session(write=True) as session:
        api = Sheets(session)
        with api.locked(SHEET_ID):
            api.batch(SHEET_ID, [update(sid, index, 1, ['done', started, stamp()])])
    return dict(success=True, job=job, key=key, result=result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('job', choices=JOBS)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.job, args.dry_run), ensure_ascii=False))
