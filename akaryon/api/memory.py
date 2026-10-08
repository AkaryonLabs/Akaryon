import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from akaryon.memory.security import MemorySecurityError

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("")
def list_memory(request: Request, offset: int = Query(default=0, ge=0),
                limit: int = Query(default=50, ge=1, le=200)) -> dict:
    items, total = request.app.state.memory.list_conversations(offset, limit)
    return {"items": items, "offset": offset, "limit": limit, "total": total}


class MessageOutput(BaseModel):
    role: str
    content: str


class MemoryEntryInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50000)
    scope: Literal["long_term", "project", "document"] = "long_term"
    project_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class MemoryEntryUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50000)


@router.post("/entries")
def store_memory_entry(body: MemoryEntryInput, request: Request) -> dict:
    if body.scope == "project" and not body.project_id:
        raise HTTPException(status_code=422, detail="project_id is required for project memory")
    try:
        entry = request.app.state.memory.store_entry(body.title, body.content, body.scope,
                                                     body.project_id, body.metadata)
    except MemorySecurityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.memory._entry_dict(entry)


@router.get("/entries")
def search_memory_entries(request: Request, q: str = Query(default="", max_length=500),
                          scope: Literal["long_term", "project", "document"] | None = None,
                          project_id: str | None = None,
                          offset: int = Query(default=0, ge=0),
                          limit: int = Query(default=20, ge=1, le=200)) -> dict:
    items, total = request.app.state.memory.search_entries(q, scope, project_id, offset, limit)
    return {"items": items, "offset": offset, "limit": limit, "total": total}


@router.get("/entries/export")
def export_memory_entries(request: Request,
                          scope: Literal["long_term", "project", "document"] | None = None,
                          project_id: str | None = None) -> StreamingResponse:
    memory = request.app.state.memory

    def serialize(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Cannot export value of type {type(value).__name__}")

    def generate():
        yield '{"items":['
        offset, written, first = 0, 0, True
        while True:
            items, total = memory.search_entries("", scope, project_id, offset, 200)
            if not items:
                break
            for item in items:
                if not first:
                    yield ","
                yield json.dumps(item, ensure_ascii=False, default=serialize)
                first = False
                written += 1
            offset += len(items)
            if offset >= total:
                break
        yield f'],"count":{written}}}'

    return StreamingResponse(generate(), media_type="application/json",
                             headers={"Content-Disposition": "attachment; filename=akaryon-memory.json",
                                      "Cache-Control": "no-store"})


@router.patch("/entries/{memory_id}")
def update_memory_entry(memory_id: str, body: MemoryEntryUpdate, request: Request) -> dict:
    try:
        entry = request.app.state.memory.update_entry(memory_id, body.title.strip(), body.content.strip())
    except MemorySecurityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return entry


@router.delete("/entries/{memory_id}")
def delete_memory_entry(memory_id: str, request: Request) -> dict[str, bool]:
    if not request.app.state.memory.delete_entry(memory_id):
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"deleted": True}


class ConversationOutput(BaseModel):
    id: str
    project_id: str | None = None
    created_at: datetime
    message_count: int
    messages: list[MessageOutput]


@router.get("/{conversation_id}", response_model=ConversationOutput)
def get_conversation(conversation_id: str, request: Request,
                     offset: int = Query(default=0, ge=0),
                     limit: int = Query(default=50, ge=1, le=200)) -> dict:
    conversation = request.app.state.memory.get_conversation(conversation_id, offset, limit)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation
