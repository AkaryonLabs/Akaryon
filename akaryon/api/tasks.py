from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20000)


@router.post("")
def create_task(body: TaskInput, request: Request) -> dict:
    task = request.app.state.tasks.create(body.name, body.description)
    return _serialize(task)


@router.get("")
def list_tasks(request: Request) -> list[dict]:
    tasks = request.app.state.tasks.list()
    session_ids = request.app.state.agent_sessions.latest_ids_for_tasks([task.id for task in tasks])
    return [_serialize(task, session_ids.get(task.id)) for task in tasks]


@router.get("/{task_id}")
def get_task(task_id: str, request: Request) -> dict:
    task = request.app.state.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _serialize(task, _session_id(request, task.id))


@router.post("/{task_id}/run")
def run_task(task_id: str, request: Request) -> dict:
    task = request.app.state.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        request.app.state.manager.run_existing(task)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError:
        # TaskExecutor already persists the failure on the task; return that lifecycle state.
        pass
    task = request.app.state.tasks.get(task_id)
    return _serialize(task, _session_id(request, task.id))


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, request: Request) -> dict:
    try:
        task = request.app.state.tasks.cancel(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    request.app.state.codex_backend.cancel_task(task_id)
    cancelled_approvals = request.app.state.approvals.cancel_for_task(task_id)
    session = request.app.state.agent_sessions.get_for_task(task_id)
    if session:
        session.status = "cancelled"
        request.app.state.agent_sessions.save(session)
    request.app.state.events.publish("TaskApprovalsCancelled", task_id=task_id, count=cancelled_approvals)
    return _serialize(task, _session_id(request, task.id))


def _session_id(request: Request, task_id: str) -> str | None:
    return request.app.state.agent_sessions.latest_ids_for_tasks([task_id]).get(task_id)


def _serialize(task, session_id: str | None = None) -> dict:
    return {"id": task.id, "name": task.name, "description": task.description,
            "status": task.status.value, "created_at": task.created_at,
            "started_at": task.started_at, "completed_at": task.completed_at,
            "agent": task.agent, "parent_task": task.parent_task,
            "result": task.result, "error": task.error, "session_id": session_id}
