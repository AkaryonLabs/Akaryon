from collections.abc import Callable

from akaryon.core.events import EventBus
from akaryon.core.exceptions import TaskCancelled
from akaryon.tasks.models import Task, TaskStatus


class TaskExecutor:
    def __init__(self, events: EventBus, save_task=None) -> None:
        self.events = events
        self.save_task = save_task

    def execute(self, task: Task, action: Callable[[], str]) -> Task:
        from datetime import datetime, timezone
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        if self.save_task:
            self.save_task(task)
        self.events.publish("TaskStarted", task_id=task.id)
        try:
            task.result = action()
            if task.status is TaskStatus.CANCELLED:
                pass
            elif task.status is TaskStatus.WAITING_FOR_APPROVAL:
                self.events.publish("ApprovalRequired", task_id=task.id)
            else:
                task.status = TaskStatus.COMPLETED
                self.events.publish("TaskCompleted", task_id=task.id)
        except Exception as exc:
            if isinstance(exc, TaskCancelled) or task.status is TaskStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
                task.error = None
            else:
                task.status, task.error = TaskStatus.FAILED, str(exc)
                self.events.publish("TaskFailed", task_id=task.id, error=str(exc))
        if task.status is not TaskStatus.WAITING_FOR_APPROVAL:
            task.completed_at = datetime.now(timezone.utc)
        if self.save_task:
            self.save_task(task)
        return task
