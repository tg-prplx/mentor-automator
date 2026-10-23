import asyncio
import copy
from collections import deque
from datetime import datetime

from .privacy import clean


class Events:
    def __init__(self):
        self.snapshot = {
            "state": "IDLE",
            "run_id": "",
            "question": "",
            "filled": 0,
            "skipped": 0,
            "answer": "",
            "confidence": None,
            "explanation": "",
            "timer": "Не указан",
            "monitor": "Ожидание",
            "actions": [],
        }
        self.actions = deque(maxlen=30)
        self.subscribers = set()

    def publish(self, action=None, **fields):
        if action:
            self.actions.append(f"{datetime.now():%H:%M:%S} {clean(action, 500)}")
        self.snapshot.update(fields)
        self.snapshot["actions"] = list(self.actions)
        for queue in list(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(copy.deepcopy(self.snapshot))

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        self.subscribers.add(queue)
        queue.put_nowait(copy.deepcopy(self.snapshot))
        return queue
