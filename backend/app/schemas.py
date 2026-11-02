"""All request/response contracts. The LLM may only produce objects that pass these models."""
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, StringConstraints, field_validator

Scalar = Union[Annotated[str, StringConstraints(max_length=200)], int, float, bool]


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid email")
        return v


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class DatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class Filter(BaseModel):
    column: str = Field(max_length=200)
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in"]
    value: Union[Scalar, list[Scalar]]


class MetricRequest(BaseModel):
    """A structured metric request. The semantic layer compiles it to SQL; the LLM never
    writes SQL."""
    metric: str = Field(max_length=200)  # column name, or "*" with agg=count
    agg: Literal["sum", "avg", "min", "max", "count", "count_distinct"]
    dimensions: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=2)
    time_column: str | None = Field(default=None, max_length=200)
    time_grain: Literal["day", "month", "year"] | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    order: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=20, ge=1, le=50)


class Plan(BaseModel):
    requests: list[MetricRequest] = Field(default_factory=list, max_length=4)
    clarification: str | None = Field(default=None, max_length=300)
    options: list[str] = Field(default_factory=list, max_length=8)


class AskIn(BaseModel):
    dataset_id: str = Field(max_length=64)
    question: str = Field(min_length=3, max_length=1000)


class Layout(BaseModel):
    x: int = Field(default=0, ge=0, le=11)
    y: int = Field(default=0, ge=0, le=500)
    w: int = Field(default=6, ge=1, le=12)
    h: int = Field(default=4, ge=1, le=12)


class Component(BaseModel):
    id: str = Field(max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["kpi", "bar", "line", "pie", "table"]
    title: str = Field(max_length=160)
    run_id: str = Field(max_length=64)
    fact_id: str = Field(max_length=20)  # evidence binding
    layout: Layout = Field(default_factory=Layout)


class DashboardSpec(BaseModel):
    schema_version: Literal[1] = 1
    components: list[Component] = Field(default_factory=list, max_length=30)

    @field_validator("components")
    @classmethod
    def _unique_ids(cls, v: list[Component]) -> list[Component]:
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("component ids must be unique")
        return v


class DashboardIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dataset_id: str = Field(max_length=64)
    spec: DashboardSpec = Field(default_factory=DashboardSpec)


class DashboardUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    spec: DashboardSpec
    base_version: int = Field(ge=1)  # optimistic concurrency


class DashboardFromRun(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class RestoreIn(BaseModel):
    version: int = Field(ge=1)
