"""只增不改的事件存储。

每条事件携带全局递增序号（seq）与提交时间戳（ts）。
线程安全；可选 JSONL 持久化，重启后序号与历史完整保留。
"""

import json
import os
import threading


class EventStore:
    def __init__(self, path=None, clock=None):
        self._lock = threading.RLock()
        self._events = []
        self._path = path
        self._clock = clock or (lambda: 0)
        self._file = None
        if path:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if line:
                            self._events.append(json.loads(line))
            self._file = open(path, "a", encoding="utf-8")

    @property
    def seq(self):
        with self._lock:
            return len(self._events)

    def append(self, event_type, data, actor_id):
        record = {
            "seq": len(self._events) + 1,
            "ts": self._clock(),
            "type": event_type,
            "actor": actor_id,
            "data": data,
        }
        with self._lock:
            record["seq"] = len(self._events) + 1
            if self._file:
                self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._file.flush()
            self._events.append(record)
        return dict(record)

    def read(self, after_seq=0):
        with self._lock:
            return [dict(event) for event in self._events[after_seq:]]

    def all(self):
        return self.read(0)

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
