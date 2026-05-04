"""Pydantic input schemas for chat tools.

Tools define their contract with Pydantic models instead of hand-rolled
JSONSchema dicts. Two payoffs:

- **Strict enum validation server-side at the LLM provider.** Invalid
  values never reach the handler. The polyglot mapping (``"todavía no"``,
  ``"ainda não"``, ``"yet to reply"`` → ``pending``) stays where it
  belongs: inside the LLM. We don't maintain synonym tables per language.
- **Fail-loud recovery.** If a payload still gets past the provider's
  enum check, ``model_validate`` raises ``ValidationError``; the agent
  loop surfaces that as ``{"error": ...}`` so the model can retry with a
  corrected payload on the next iteration.

Helper :func:`pydantic_to_anthropic_schema` converts a Pydantic model into
an inlined JSON Schema suitable for ``ToolSpec.input_schema``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ──────────────────────────────────────────────────────────────────────────
#   Enums — semantic vocabularies the LLM maps natural language onto
# ──────────────────────────────────────────────────────────────────────────


class RespondedStatus(str, Enum):
    """Filtro por respuesta del owner a una reseña."""

    any = "any"
    pending = "pending"
    responded = "responded"


class Sentiment(str, Enum):
    """Sentimiento detectado en el texto de la reseña."""

    any = "any"
    positive = "positive"
    neutral = "neutral"
    negative = "negative"


class ReviewSort(str, Enum):
    """Orden del listado de reseñas."""

    recent = "recent"
    oldest = "oldest"
    rating_high = "rating_high"
    rating_low = "rating_low"
    most_negative = "most_negative"
    most_positive = "most_positive"


# ──────────────────────────────────────────────────────────────────────────
#   Tool input models
# ──────────────────────────────────────────────────────────────────────────


class ListReviewsInput(BaseModel):
    """Inputs del tool ``list_reviews`` (agente Business).

    Las descripciones están en español neutro y describen *semántica*,
    no vocabulario. El LLM resuelve la traducción NL → enum.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator(
        "responded_status", "sentiment", "sort", mode="before"
    )
    @classmethod
    def _lowercase_enum(cls, value: Any) -> Any:
        # Models occasionally emit uppercase enum values ('NEUTRAL',
        # 'PENDING'). Lowercasing before the enum check keeps the
        # contract semantic (one canonical form) without forcing the
        # LLM to mind casing — which is just visual noise, not vocabulary.
        if isinstance(value, str):
            return value.lower()
        return value

    responded_status: RespondedStatus = Field(
        default=RespondedStatus.any,
        description=(
            "Filtra por respuesta del owner. 'pending' = el owner todavía "
            "no respondió la reseña; 'responded' = ya respondió; 'any' = "
            "ambas."
        ),
    )
    sentiment: Sentiment = Field(
        default=Sentiment.any,
        description=(
            "Filtra por sentimiento detectado del texto. Reseñas sin "
            "sentimiento analizado se excluyen cuando el filtro es "
            "distinto de 'any'."
        ),
    )
    sort: ReviewSort = Field(
        default=ReviewSort.recent,
        description="Orden del resultado. 'recent' es el default.",
    )
    dish_name_contains: str | None = Field(
        default=None,
        description=(
            "Substring acento-insensible que tiene que aparecer en el "
            "nombre del plato. Útil para 'reseñas de mi hamburguesa'."
        ),
    )
    min_rating: float | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Rating mínimo del cliente, escala 1-5.",
    )
    max_rating: float | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Rating máximo del cliente, escala 1-5.",
    )
    date_from: date | None = Field(
        default=None,
        description="Fecha mínima inclusive, ISO YYYY-MM-DD.",
    )
    date_to: date | None = Field(
        default=None,
        description="Fecha máxima inclusive, ISO YYYY-MM-DD.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Cantidad máxima de reseñas a devolver (1-50).",
    )


# ──────────────────────────────────────────────────────────────────────────
#   JSONSchema serialisation
# ──────────────────────────────────────────────────────────────────────────


def pydantic_to_anthropic_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Render a Pydantic model as an inlined JSONSchema for tool specs.

    Pydantic emits ``$defs`` plus ``$ref`` for nested enums. Anthropic
    accepts that shape, but inlining keeps the wire payload smaller and
    portable across whichever provider litellm is proxying. Sibling
    keywords on the ``$ref`` node (e.g. ``description``, ``default``)
    are preserved so per-field documentation survives the inline pass.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def _inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                merged: dict[str, Any] = deepcopy(target)
                for key, value in node.items():
                    if key != "$ref":
                        merged[key] = value
                return _inline(merged)
            return {key: _inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_inline(item) for item in node]
        return node

    schema = _inline(raw)
    schema.pop("title", None)
    return schema
