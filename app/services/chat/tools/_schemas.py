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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


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


class SummaryDimension(str, Enum):
    """Aspectos que ``summarize_reviews_period`` puede calcular."""

    sentiment = "sentiment"
    rating = "rating"
    responded = "responded"


class ResponseTone(str, Enum):
    """Tono solicitado para la redacción de la respuesta a una reseña."""

    warm = "warm"
    professional = "professional"
    apologetic = "apologetic"
    match_brand = "match_brand"


class BaselineMetric(str, Enum):
    """Métrica para ``compare_to_baseline``."""

    rating = "rating"
    review_count = "review_count"
    sentiment_score = "sentiment_score"
    response_rate = "response_rate"


class BaselineKind(str, Enum):
    """Contra qué baseline se compara la métrica."""

    prior_period = "prior_period"
    all_time = "all_time"
    competition = "competition"


class OwnerPreferenceTone(str, Enum):
    """Tono que el owner pidió persistir para el chat Business."""

    warm = "warm"
    professional = "professional"
    concise = "concise"
    match_brand = "match_brand"


class OwnerPreferenceLanguage(str, Enum):
    """Idioma persistente preferido por el owner para las respuestas
    del agente. ``None`` (no persistir) significa adaptar al input."""

    es = "es"
    en = "en"
    pt = "pt"


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


class SuggestReviewResponseInput(BaseModel):
    """Inputs del tool ``suggest_review_response`` (agente Business).

    El tool devuelve **contexto estructurado** (reseña + plato + tono +
    guía y constraints), no un draft pre-escrito. El agente redacta la
    respuesta final en su turno siguiente apoyándose en ese payload.
    Esto mantiene el tono consistente con el resto de la conversación
    sin un segundo LLM dedicado.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("tone", mode="before")
    @classmethod
    def _lowercase_tone(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    review_id: str = Field(
        description=(
            "UUID de la reseña a responder. SIEMPRE viene del output "
            "de un list_reviews previo en la misma conversación; nunca "
            "se le pide al owner."
        ),
    )
    tone: ResponseTone | None = Field(
        default=None,
        description=(
            "Tono solicitado para la respuesta. Pasalo cuando el owner "
            "lo especifica: 'warm' = cálido / agradecido, "
            "'professional' = cortés y neutro, 'apologetic' = "
            "reconociendo el problema, 'match_brand' = perfil del "
            "owner (roadmap F5). Si lo omitís, el tool lo infiere del "
            "sentimiento de la reseña: negative → apologetic, "
            "positive → warm, neutral → professional."
        ),
    )


class UpdateOwnerPreferencesInput(BaseModel):
    """Inputs para actualizar las preferencias persistentes del owner.

    Cada campo es opcional: el owner puede pedir cambiar UNA cosa
    sola. Pasá solo los campos que el owner mencionó explícitamente
    en su mensaje. Para limpiar una preferencia (ej. "no fijes idioma,
    adaptate al que use") pasá la cadena vacía ``""``.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("tone", "language", mode="before")
    @classmethod
    def _lowercase(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    tone: OwnerPreferenceTone | None = Field(
        default=None,
        description=(
            "Tono que el owner quiere por default en TODAS las "
            "interacciones futuras: 'warm', 'professional', 'concise', "
            "'match_brand'. Solo pasalo si el owner lo dijo explícito "
            "('siempre tono formal', 'respondé corto')."
        ),
    )
    language: OwnerPreferenceLanguage | None = Field(
        default=None,
        description=(
            "Idioma fijo preferido para las respuestas del agente: "
            "'es', 'en', 'pt'. Solo pasalo si el owner lo dijo explícito "
            "('respondé siempre en portugués'). NO lo derives de en qué "
            "idioma vino el mensaje actual."
        ),
    )
    kpi_focus: list[str] | None = Field(
        default=None,
        description=(
            "Lista corta de KPIs que el owner quiere ver siempre en "
            "saludos / resúmenes ('rating_avg', 'response_rate', "
            "'review_count_30d', etc.). Lista vacía [] = limpiar."
        ),
    )


class CompareToBaselineInput(BaseModel):
    """Inputs del tool ``compare_to_baseline`` (agente Business).

    Devuelve current vs baseline (con delta absoluto + delta %),
    eligiendo entre tres tipos de baseline: período anterior, historia
    completa del restaurante, o competidores geográficos. Una sola
    llamada reemplaza dos o tres calls que el LLM tendría que componer
    a mano para responder "¿estoy mejor o peor que…?".
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("metric", "vs", mode="before")
    @classmethod
    def _lowercase(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    metric: BaselineMetric = Field(
        description=(
            "Qué medir: 'rating' (promedio de estrellas, 1-5), "
            "'review_count' (cantidad de reseñas en el período), "
            "'sentiment_score' (promedio del score de sentimiento, "
            "-1 a +1), 'response_rate' (% de reseñas con respuesta "
            "del owner)."
        ),
    )
    vs: BaselineKind = Field(
        validation_alias=AliasChoices(
            "vs", "baseline", "target_baseline", "compared_to"
        ),
        description=(
            "Contra qué se compara: 'prior_period' = mismo length "
            "inmediatamente antes, 'all_time' = historia completa del "
            "restaurante, 'competition' = restaurantes en un radio "
            "geográfico (percentil + cohort_size). 'competition' no "
            "soporta sentiment_score (la mayoría de competidores no "
            "tienen sentimiento analizado)."
        ),
    )
    from_date: date | None = Field(
        default=None,
        alias="from",
        validation_alias=AliasChoices("from_date", "from"),
        description=(
            "Inicio del período a evaluar, ISO YYYY-MM-DD. Si lo "
            "omitís, el handler usa los últimos 30 días — útil cuando "
            "el owner pregunta sin especificar fecha."
        ),
    )
    to_date: date | None = Field(
        default=None,
        alias="to",
        validation_alias=AliasChoices("to_date", "to"),
        description=(
            "Fin del período a evaluar, ISO YYYY-MM-DD. Si lo omitís, "
            "el handler usa la fecha de hoy."
        ),
    )
    radius_km: float = Field(
        default=2.0,
        ge=0.5,
        le=20.0,
        description=(
            "Radio en km para buscar competidores. Solo se usa con "
            "vs='competition'. Default 2.0 — barrio cercano."
        ),
    )


class SummarizeReviewsInput(BaseModel):
    """Inputs del tool ``summarize_reviews_period`` (agente Business).

    Devuelve agregados pre-calculados sobre las reseñas del restaurante
    en el período pedido, con delta automático contra el período
    inmediatamente anterior de la misma duración.
    """

    # ``populate_by_name=True`` lets the LLM pass either ``from_date``
    # (canonical) or ``from`` (idiomatic JSON-Schema range shorthand
    # most models reach for first). Same for to. The handler always
    # sees ``from_date`` / ``to_date`` once Pydantic normalises.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("dimensions", mode="before")
    @classmethod
    def _lowercase_dimensions(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [v.lower() if isinstance(v, str) else v for v in value]
        return value

    from_date: date = Field(
        alias="from",
        validation_alias=AliasChoices("from_date", "from"),
        description="Inicio del período inclusive, ISO YYYY-MM-DD.",
    )
    to_date: date = Field(
        alias="to",
        validation_alias=AliasChoices("to_date", "to"),
        description="Fin del período inclusive, ISO YYYY-MM-DD.",
    )
    dimensions: list[SummaryDimension] = Field(
        default_factory=lambda: [
            SummaryDimension.sentiment,
            SummaryDimension.rating,
            SummaryDimension.responded,
        ],
        description=(
            "Aspectos a calcular. Default: los tres "
            "(sentiment, rating, responded)."
        ),
        min_length=1,
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
