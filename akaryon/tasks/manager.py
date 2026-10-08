from akaryon.core.events import EventBus
from akaryon.tasks.executor import TaskExecutor
from akaryon.tasks.models import Task
from akaryon.database.models import TaskRecord
from akaryon.database.session import session_scope
from akaryon.tasks.models import TaskStatus
from datetime import datetime, timezone
from threading import Event


class TaskManager:
    def __init__(self, events: EventBus, session_factory=None) -> None:
        self.session_factory = session_factory
        self.tasks: dict[str, Task] = {}
        self._cancel_events: dict[str, Event] = {}
        self.executor = TaskExecutor(events, self._save)
        self.events = events

    def create(self, name: str, description: str) -> Task:
        task = Task(name=name, description=description)
        self.tasks[task.id] = task
        self._cancel_events[task.id] = Event()
        self._save(task)
        self.events.publish("TaskCreated", task_id=task.id)
        return task

    def list(self) -> list[Task]:
        if self.session_factory:
            from sqlalchemy import select
            with session_scope(self.session_factory) as session:
                return [self._from_record(record) for record in session.scalars(select(TaskRecord).order_by(TaskRecord.created_at)).all()]
        return list(self.tasks.values())

    def get(self, task_id: str) -> Task | None:
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                record = session.get(TaskRecord, task_id)
                return self._from_record(record) if record else None
        return self.tasks.get(task_id)

    def cancel(self, task_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        if task.status is TaskStatus.RUNNING:
            self._cancel_events.setdefault(task_id, Event()).set()
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now(timezone.utc)
            task.result = "Task cancellation requested."
            self._save(task)
            self.events.publish("TaskCancelled", task_id=task.id, cooperative=True)
            return task
        if task.status not in {TaskStatus.PENDING, TaskStatus.WAITING_FOR_APPROVAL}:
            raise ValueError(f"Cannot cancel a task in {task.status.value} state")
        task.status = TaskStatus.CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        task.result = "Task cancelled by request."
        self._save(task)
        self.events.publish("TaskCancelled", task_id=task.id)
        return task

    def is_cancel_requested(self, task_id: str) -> bool:
        return self._cancel_events.setdefault(task_id, Event()).is_set()

    def _save(self, task: Task) -> None:
        if not self.session_factory:
            return
        with session_scope(self.session_factory) as session:
            record = session.get(TaskRecord, task.id)
            values = {"name": task.name, "description": task.description, "status": task.status.value,
                      "created_at": task.created_at, "started_at": task.started_at,
                      "completed_at": task.completed_at, "agent": task.agent,
                      "parent_task": task.parent_task, "result": task.result, "error": task.error}
            if record is None:
                session.add(TaskRecord(id=task.id, **values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)

    @staticmethod
    def _from_record(record: TaskRecord) -> Task:
        from akaryon.tasks.models import TaskStatus
        return Task(id=record.id, name=record.name, description=record.description,
                    status=TaskStatus(record.status), created_at=record.created_at,
                    started_at=record.started_at, completed_at=record.completed_at,
                    agent=record.agent, parent_task=record.parent_task,
                    result=record.result, error=record.error)
