"""Bounded callback diagnostics: stages and thread stacks, never message content."""
import faulthandler
import sys
import time
from threading import Thread, Event, Lock

_guard = Lock()
_last_dump = 0


class ReportTrace:
    def __init__(self, event_id):
        self.event_id = event_id
        self.started = time.monotonic()
        self.phase = 'received'
        self.stop = Event()
        self.step('received')
        Thread(target=self.watch, daemon=True, name='report-watchdog').start()

    def step(self, phase):
        self.phase = phase
        print(f'REPORT_FLOW event={self.event_id} phase={phase} elapsed={time.monotonic()-self.started:.2f}', flush=True)

    def watch(self):
        global _last_dump
        if self.stop.wait(20):
            return
        self.step('stalled:' + self.phase)
        with _guard:
            if time.monotonic() - _last_dump > 60:
                _last_dump = time.monotonic()
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

    def close(self):
        self.stop.set()
