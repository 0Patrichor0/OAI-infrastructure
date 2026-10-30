"""Bounded, coalesced HTTP reads. Never used for write verification or snapshots."""
import copy
import threading
import time

from .actions import IntentError


class ReadBroker:
    def __init__(self, ttl=2.0, wait=57.0):
        self.ttl, self.wait = ttl, wait
        self.condition = threading.Condition()
        self.entries = {}
        self.slots = threading.BoundedSemaphore(2)
        self.generation = 0
        self.stats = dict(queries=0, shared=0, failures=0, last_error=None)

    def invalidate(self):
        with self.condition:
            self.generation += 1
            self.entries.clear()
            self.condition.notify_all()

    def read(self, key, loader):
        deadline = time.monotonic() + self.wait
        with self.condition:
            entry = self.entries.get(key)
            if entry and entry['generation'] == self.generation:
                self.stats['shared'] += 1
                while entry['loading']:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise IntentError('E2 读取等待超时，请稍后重试')
                    self.condition.wait(remaining)
                if entry.get('error'):
                    raise IntentError(entry['error'])
                if entry['generation'] != self.generation:
                    raise IntentError('读取期间发生控制操作，请重新读取')
                if time.monotonic() - entry['finished'] <= self.ttl:
                    return self.response(entry, True)
            if not self.slots.acquire(blocking=False):
                raise IntentError('E2 正在读取其他对象，请稍后重试；请求未加入长队列')
            if len(self.entries) > 64:
                self.entries = {k:v for k,v in self.entries.items() if v['loading']}
            entry = dict(loading=True, generation=self.generation)
            self.entries[key] = entry
            self.stats['queries'] += 1
        started = time.monotonic()
        try:
            data = loader()
            with self.condition:
                if entry['generation'] != self.generation:
                    raise IntentError('读取期间发生控制操作，请重新读取')
                entry.update(data=data, finished=time.monotonic(), sampled_at=time.time(),
                             duration_s=round(time.monotonic()-started, 3))
            return self.response(entry, False)
        except Exception as exc:
            with self.condition:
                entry['error'] = str(exc)
                self.stats['failures'] += 1
                self.stats['last_error'] = dict(at=time.time(), key=str(key), error=str(exc))
                if self.entries.get(key) is entry:
                    self.entries.pop(key, None)
            raise
        finally:
            with self.condition:
                entry['loading'] = False
                self.slots.release()
                self.condition.notify_all()

    @staticmethod
    def response(entry, shared):
        timestamp = (entry['data'].get('state') or {}).get('timestamp_us')
        sampled_at = timestamp/1e6 if isinstance(timestamp,(int,float)) and timestamp>0 else entry['sampled_at']
        return {**copy.deepcopy(entry['data']), 'read': {
            'sampled_at': sampled_at, 'fetched_at':entry['sampled_at'], 'shared': shared,
            'age_s': round(max(0,time.time()-sampled_at), 3),
            'cache_age_s':round(time.monotonic()-entry['finished'], 3),
            'duration_s': entry['duration_s']}}

    def diagnostics(self):
        with self.condition:
            return {**self.stats, 'in_flight':sum(v['loading'] for v in self.entries.values()),
                    'cache_ttl_s':self.ttl, 'max_distinct_reads':2}


broker = ReadBroker()
