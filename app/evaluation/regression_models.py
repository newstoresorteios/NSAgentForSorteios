"""Versioned inputs and explicit oracles for conversation regression."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Expectations(BaseModel):
    model_config = ConfigDict(extra='forbid')
    requirements: list[str] = Field(min_length=1, max_length=20)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=20)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    handoff: Literal['allowed', 'required', 'forbidden'] = 'allowed'
    handoff_offer: Literal['allowed', 'required', 'forbidden'] = 'allowed'
    state_equals: dict[str, Any] = Field(default_factory=dict)
    min_products: int = Field(default=0, ge=0, le=20)
    max_price: float | None = Field(default=None, gt=0)
    allowed_product_ids: list[str] = Field(default_factory=list)
    expected_tool_errors: list[str] = Field(default_factory=list)


class RegressionStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input: str = Field(min_length=1, max_length=8000)
    expected: Expectations


class RegressionScenario(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-z0-9_-]{1,100}$')
    category: str = Field(min_length=1, max_length=80)
    split: Literal['development', 'validation']
    critical: bool = False
    source_response_ids: list[int] = Field(default_factory=list)
    channel: Literal['whatsapp', 'instagram', 'web'] = 'whatsapp'
    history: list[dict[str, str]] = Field(default_factory=list, max_length=80)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    recorded_at: str | None = None
    steps: list[RegressionStep] = Field(min_length=1, max_length=12)
    environment: Literal['live_readonly', 'simulated_commerce'] = 'simulated_commerce'
    simulation: dict[str, Any] = Field(default_factory=dict)


class RegressionSuite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    categories: list[str] = Field(min_length=1, max_length=20)
    target_percent: float = Field(default=90, ge=0, le=100)
    minimum_category_percent: float = Field(default=85, ge=0, le=100)
    minimum_cases_per_category: int = Field(default=10, ge=1, le=100)
    configuration_overrides: dict[str, Any] = Field(default_factory=dict)
    scenarios: list[RegressionScenario] = Field(min_length=1, max_length=300)


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion: str
    passed: bool
    evidence: str


class RegressionVerdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criteria: list[CriterionVerdict]
    factual_errors: list[str]
    critical_errors: list[str]
    summary: str

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        from app.llm.openai_strict_schema import apply_openai_strict_schema
        return apply_openai_strict_schema(handler(core_schema))
