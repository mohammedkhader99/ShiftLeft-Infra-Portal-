import os
from collections.abc import Iterator

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CostCentre, Environment, Project, Technology
from db.session import SessionLocal

load_dotenv()

app = FastAPI(title="Infra Portal API")


def is_mock_mode() -> bool:
    return os.getenv("USE_MOCK", "true").strip().lower() == "true"


def get_session() -> Iterator[Session]:
    """Hand out a database session for the life of one request (read-only use here)."""
    with SessionLocal() as session:
        yield session


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mock": is_mock_mode()}


# --- Lookups (increment 1.2) -------------------------------------------------
# Typed response shapes so the JSON is clean and stable for the portal to render.


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class CostCentreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str


class TechnologyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    lifecycle_state: str


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    environment_class: str


class LookupsResponse(BaseModel):
    projects: list[ProjectOut]
    cost_centres: list[CostCentreOut]
    technologies: list[TechnologyOut]
    environments: list[EnvironmentOut]


@app.get("/api/lookups", response_model=LookupsResponse)
def lookups(session: Session = Depends(get_session)) -> LookupsResponse:
    """Read-only reference data for the guided-request form's dropdowns."""
    return LookupsResponse(
        projects=session.scalars(select(Project).order_by(Project.name)).all(),
        cost_centres=session.scalars(select(CostCentre).order_by(CostCentre.name)).all(),
        technologies=session.scalars(select(Technology).order_by(Technology.name)).all(),
        environments=session.scalars(select(Environment).order_by(Environment.name)).all(),
    )
