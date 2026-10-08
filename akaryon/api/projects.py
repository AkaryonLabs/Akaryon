from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="A human-readable project name.")
    path: str | None = Field(
        default=None,
        description="Optional informational path. Setting this does not grant filesystem access.",
    )


class ProjectOutput(BaseModel):
    id: str = Field(description="Use this ID as project_id in chat requests.")
    name: str
    path: str | None = None


@router.get("", response_model=list[ProjectOutput])
def list_projects(request: Request) -> list[dict]:
    return request.app.state.memory.list_projects()


@router.post("", response_model=ProjectOutput)
def create_project(body: ProjectInput, request: Request) -> dict:
    return request.app.state.memory.create_project(body.name, body.path)
