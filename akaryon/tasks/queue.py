from collections import deque

from akaryon.tasks.models import Task


class TaskQueue:
    def __init__(self) -> None:
        self._items: deque[Task] = deque()

    def put(self, task: Task) -> None:
        self._items.append(task)

    def get(self) -> Task | None:
        return self._items.popleft() if self._items else None
