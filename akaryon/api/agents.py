from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
def list_agents(request: Request) -> list[dict[str, str]]:
    return [{"id": "manager", "name": "ManagerAgent", "description": "Initial Akaryon system agent"}]


@router.get("/{agent_id}/sessions")
def list_agent_sessions(agent_id: str, request: Request,
                        offset: int = Query(default=0, ge=0),
                        limit: int = Query(default=50, ge=1, le=200)) -> dict:
    if agent_id != "manager":
        raise HTTPException(status_code=404, detail="Agent not found")
    sessions, total = request.app.state.agent_sessions.list_for_agent(agent_id, offset, limit)
    return {"items": [_session_dict(session) for session in sessions],
            "offset": offset, "limit": limit, "total": total}


@router.get("/{agent_id}/sessions/{session_id}")
def get_agent_session(agent_id: str, session_id: str, request: Request) -> dict:
    if agent_id != "manager":
        raise HTTPException(status_code=404, detail="Agent not found")
    session = request.app.state.agent_sessions.get(session_id)
    if session is None or session.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return _session_dict(session)


@router.get("/{agent_id}")
def get_agent(agent_id: str, request: Request) -> dict[str, str]:
    if agent_id != "manager":
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = request.app.state.manager.agent
    return {"id": agent.id, "name": agent.name, "description": agent.description, "state": agent.state}


def _session_dict(session) -> dict:
    return {"id": session.id, "agent_id": session.agent_id, "task_id": session.task_id,
            "conversation_id": session.conversation_id, "status": session.status,
            "model": session.model, "provider": session.provider, "result": session.result,
            "error": session.error, "state": session.state,
            "created_at": session.created_at, "updated_at": session.updated_at}
