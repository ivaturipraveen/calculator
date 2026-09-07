"""Request and response models.

The calculate request is one shape for all eight renderers. A client sends the
parts its calculator has -- `inputs` for a formula, `selections` for a score,
`answers` for a tree -- and the server ignores the rest. That keeps one endpoint
and one client method instead of five.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CalculateRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    units: dict[str, str] = Field(
        default_factory=dict,
        description="Per-field unit code the value is expressed in; omitted means the base unit.",
    )
    selections: dict[str, Any] = Field(
        default_factory=dict, description="Score criteria: group key -> option key(s)."
    )
    answers: list[str] = Field(
        default_factory=list, description="Decision tree: the yes/no answers so far."
    )
    pair_index: Optional[int] = Field(
        default=None, description="Unit converter: which printed pair to apply."
    )
    value: Optional[float] = Field(default=None, description="Unit converter: the input value.")
    reverse: bool = Field(default=False, description="Unit converter: apply the pair backwards.")


class FieldIssue(BaseModel):
    field: str
    message: str


class OutputValue(BaseModel):
    key: str
    label: str
    value: Any
    unit: Optional[str] = None
    decimals: Optional[int] = None
    kind: str = "number"
    finite: bool = True


class CalculateResponse(BaseModel):
    slug: str
    renderer: str
    ok: bool
    errors: list[FieldIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    outputs: list[OutputValue] = Field(default_factory=list)
    lookups: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Which row of each reference table this calculation read.",
    )
    score: Optional[dict[str, Any]] = None
    table: Optional[dict[str, Any]] = None
    drugs: Optional[dict[str, Any]] = None
    conversion: Optional[dict[str, Any]] = None
    tree: Optional[dict[str, Any]] = None
