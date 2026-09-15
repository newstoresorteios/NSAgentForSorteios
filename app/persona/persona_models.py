from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


PersonaStatus = Literal["draft", "active", "archived"]
PersonaSource = Literal["user", "migration", "system"]


class PersonaVersion(BaseModel):
    id: int | None = None
    tenant_id: str
    persona_key: str
    workspace_id: str | None = None
    version: int
    name: str
    source: PersonaSource = "user"
    instructions: str
    instructions_hash: str
    status: PersonaStatus = "draft"
    created_by: str | None = None
    activated_by: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_id", mode="before")
    @classmethod
    def normalize_workspace_id(cls, value: Any) -> Any:
        # psycopg returns UUID objects; HTTP/JSON repositories return strings.
        return str(value) if isinstance(value, UUID) else value


class PersonaVersionCreate(BaseModel):
    name: str = "NewStore Commercial"
    instructions: str
    created_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
