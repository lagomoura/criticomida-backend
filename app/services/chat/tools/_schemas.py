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


class UserPreferenceLanguage(str, Enum):
    """Idioma persistente preferido por el comensal (B2C). Idéntico
    al del owner pero distinto enum para mantener los dos productos
    desacoplados."""

    es = "es"
    en = "en"
    pt = "pt"


class UserResponseStyle(str, Enum):
    """Tipo de respuesta editorial que el comensal pidió fijar.

    ``editorial`` es el default del prompt — 2-3 frases que enmarcan
    los resultados. ``concise`` colapsa a una frase + cards (para
    comensales que prefieren ir al grano). ``warm`` agrega más color
    conversacional.
    """

    editorial = "editorial"
    concise = "concise"
    warm = "warm"


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


class UpdateUserChatPreferencesInput(BaseModel):
    """Inputs para persistir preferencias del comensal (Sommelier).

    Espejo B2C de ``UpdateOwnerPreferencesInput``. Cada campo es
    opcional: el comensal suele pedir cambiar UNA cosa por turno.
    Pasá solo lo que mencionó. Para limpiar una preferencia ("no
    fijes idioma, adaptate al que use") pasá la cadena vacía ``""``.

    Sin fila previa → upsert. Las preferencias aplican desde la
    PRÓXIMA sesión: en el turno actual el agente sigue con lo que
    tenía al arrancar (mismo contrato que el Business).
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("language", "response_style", mode="before")
    @classmethod
    def _lowercase(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    language: UserPreferenceLanguage | None = Field(
        default=None,
        description=(
            "Idioma fijo preferido para las respuestas del Sommelier: "
            "'es', 'en', 'pt'. Solo pasalo si el comensal lo dijo "
            "explícito ('siempre respondé en inglés'). NO lo derives "
            "de en qué idioma vino el mensaje actual."
        ),
    )
    response_style: UserResponseStyle | None = Field(
        default=None,
        description=(
            "Estilo de respuesta editorial: 'editorial' (default — "
            "2-3 frases enmarcando), 'concise' (una frase + cards, "
            "sin rodeos), 'warm' (más conversacional). Solo pasalo "
            "si el comensal lo dijo explícito ('respondeme corto', "
            "'siempre al grano', 'andá al hueso')."
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
#   Sommelier (B2C) tool inputs
# ──────────────────────────────────────────────────────────────────────────


class PriceTierFilter(str, Enum):
    """Cap for the ``max_price_tier`` filter in ``search_dishes``.

    Mirrors ``app.models.dish.PriceTier`` but namespaced here so the
    chat schema layer stays free of model imports. The three buckets
    are the only ones the catalog tracks — there's no '$$$$'.
    """

    cheap = "$"
    mid = "$$"
    high = "$$$"


class BboxFilter(BaseModel):
    """Geographic bounding box (south/west/north/east in WGS84)."""

    model_config = ConfigDict(extra="forbid")

    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)


class CenterPoint(BaseModel):
    """Map center + optional zoom for ``open_in_map``."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    zoom: int | None = Field(default=None, ge=8, le=18)


class SearchDishesInput(BaseModel):
    """Inputs del tool ``search_dishes`` (todos los agentes).

    Filtros estructurados se componen como AND y NUNCA se violan
    (eso es la garantía de que el LLM no te miente con "te traje
    cualquier cosa porque tu pedido era estricto"). El
    ``semantic_query`` re-rankea por similitud dentro del subset si
    hay embeddings disponibles; si no, ordena por rating.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("max_price_tier", mode="before")
    @classmethod
    def _normalize_price_tier(cls, value: Any) -> Any:
        # Models occasionally emit synonyms ('low'/'mid'/'high',
        # 'barato', '$$$$') — fold them onto the canonical tokens
        # before the enum check rejects them.
        if isinstance(value, str):
            v = value.strip()
            if v in ("$", "$$", "$$$"):
                return v
            mapping = {
                "$$$$": "$$$",
                "low": "$",
                "cheap": "$",
                "barato": "$",
                "mid": "$$",
                "medio": "$$",
                "high": "$$$",
                "alto": "$$$",
                "caro": "$$$",
            }
            return mapping.get(v.lower(), v)
        return value

    neighborhood: str | None = Field(
        default=None,
        validation_alias=AliasChoices("neighborhood", "barrio", "zona"),
        description=(
            "Substring del location_name del restaurante (case-"
            "insensitive). Ejemplos: 'Palermo', 'Centro', 'Belgrano'."
        ),
    )
    city: str | None = Field(
        default=None,
        validation_alias=AliasChoices("city", "ciudad", "cidade"),
        description="Ciudad exacta. Ejemplo: 'Buenos Aires'.",
    )
    bbox: BboxFilter | None = Field(
        default=None,
        description=(
            "Bounding box geográfico. Usar cuando el comensal "
            "menciona o toca un área visible en el mapa."
        ),
    )
    min_value_prop: int | None = Field(
        default=None,
        ge=1,
        le=3,
        description=(
            "Pilar de costo/beneficio mínimo (1-3). 3 = ganga. "
            "Pasalo cuando el comensal pide 'barato pero rico'."
        ),
    )
    min_presentation: int | None = Field(
        default=None,
        ge=1,
        le=3,
        description=(
            "Pilar de presentación mínimo (1-3). 3 = visualmente "
            "destacado. Pasalo en pedidos con mood ('cita', 'foto')."
        ),
    )
    min_execution: int | None = Field(
        default=None,
        ge=1,
        le=3,
        description=(
            "Pilar de ejecución técnica mínimo (1-3). 3 = oficio "
            "destacado. Pasalo cuando piden 'comida bien hecha'."
        ),
    )
    min_rating: float | None = Field(
        default=None,
        ge=0,
        le=5,
        validation_alias=AliasChoices(
            "min_rating", "rating_min", "rating_minimo"
        ),
        description="Rating agregado mínimo del plato, escala 0-5.",
    )
    max_price_tier: PriceTierFilter | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "max_price_tier", "precio_max", "max_price"
        ),
        description=(
            "Tope de bucket de precio: '$' = barato, '$$' = medio, "
            "'$$$' = caro. Solo existen tres niveles."
        ),
    )
    category_slug: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "category_slug", "categoria", "category"
        ),
        description=(
            "Slug de categoría del restaurante (ej: 'italiana', "
            "'japonesa', 'parrilla'). Lo determina el catálogo — si "
            "no estás seguro de un slug, llamá search_dishes sin él."
        ),
    )
    semantic_query: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "semantic_query", "query", "vibe", "mood"
        ),
        description=(
            "Texto libre para re-ranking semántico ('cita romántica', "
            "'comida confort'). Pasalo cuando el pedido tiene un "
            "'mood' distinto del filtrado estructurado."
        ),
    )
    name_contains: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "name_contains", "nombre", "plato", "dish_name"
        ),
        description=(
            "Substring acento-insensible que TIENE que aparecer en el "
            "nombre del plato. Filtro duro AND — el match es a nivel "
            "SQL contra la columna normalizada (lower + unaccent). Usalo "
            "siempre que el comensal pida un plato/bebida concreto por "
            "nombre ('ceviche', 'ramen', 'milanesa', 'café', 'risotto') "
            "para garantizar que el resultado contiene ese nombre — el "
            "``semantic_query`` solo no alcanza porque embeddings con "
            "ruido pueden devolver top-K que no incluye el plato real. "
            "Acentos y mayúsculas no importan; ``ceviche`` matchea "
            "'Ceviche de pescado' y 'Cevíches mixtos'. NO lo uses para "
            "'moods' o categorías ('algo rico', 'comida confort') — "
            "para eso está ``semantic_query``."
        ),
    )
    limit: int = Field(
        default=6,
        ge=1,
        le=12,
        description=(
            "Cantidad máxima de platos a devolver. Default 6 — más "
            "satura la grid visual."
        ),
    )


class GetDishDetailInput(BaseModel):
    """Inputs del tool ``get_dish_detail``.

    Acepta UUID o nombre libre — el tool resuelve nombres internamente.
    NUNCA le pidas al humano un dish_id ni 'el nombre exacto'.
    """

    model_config = ConfigDict(extra="forbid")

    dish_id: str | None = Field(
        default=None,
        description=(
            "UUID del plato. Pasalo cuando ya viene de un "
            "search_dishes previo en la misma conversación."
        ),
    )
    dish_name: str | None = Field(
        default=None,
        description=(
            "Nombre libre del plato como lo dijo el humano "
            "('el risotto', 'la pizza margherita')."
        ),
    )


class AddToWishlistInput(BaseModel):
    """Inputs del tool ``add_to_wishlist``.

    Mismo contrato amistoso de get_dish_detail: UUID o nombre libre.
    """

    model_config = ConfigDict(extra="forbid")

    dish_id: str | None = Field(default=None, description="UUID del plato.")
    dish_name: str | None = Field(
        default=None,
        description=(
            "Nombre libre del plato como lo dijo el comensal."
        ),
    )


class OpenInMapInput(BaseModel):
    """Inputs del tool ``open_in_map``.

    Al menos uno de bbox / center / dish_ids tiene que venir; el handler
    valida en runtime y devuelve un payload navegacional para el FE.
    """

    model_config = ConfigDict(extra="forbid")

    bbox: BboxFilter | None = Field(
        default=None,
        description=(
            "Área visible para enmarcar. Usar cuando el comensal "
            "quiere ver un barrio o zona entera."
        ),
    )
    center: CenterPoint | None = Field(
        default=None,
        description=(
            "Coordenadas + zoom opcional. Anclá el mapa en un punto "
            "concreto (ej: la dirección de un restaurante)."
        ),
    )
    dish_ids: list[str] | None = Field(
        default=None,
        max_length=20,
        description=(
            "UUIDs de platos a pin-ear en el mapa. Usar para mostrar "
            "los resultados de un search_dishes geográficamente."
        ),
    )


class CreateDishRouteInput(BaseModel):
    """Inputs del tool ``create_dish_route``.

    Crea una ruta compartible (``dish_lists`` + ``dish_list_items``).
    El usuario logueado queda como owner; sin login el tool falla
    explícito. Default ``is_public=true`` — la persona suele querer
    compartirla; pasá ``False`` solo si lo pide explícito.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=3,
        max_length=160,
        description=(
            "Título corto de la ruta. Ejemplo: 'Ruta de pastas en "
            "Belgrano' o 'Domingo en el Centro'."
        ),
    )
    description: str | None = Field(
        default=None,
        max_length=600,
        description=(
            "Frase editorial que enmarca la ruta. Opcional pero ayuda "
            "al comensal cuando la comparte."
        ),
    )
    dish_ids: list[str] = Field(
        min_length=2,
        max_length=10,
        description=(
            "UUIDs de los platos en el orden en que aparecerán "
            "(primer plato = primera parada)."
        ),
    )
    is_public: bool = Field(
        default=True,
        description=(
            "True (default) genera URL pública compartible. False = "
            "lista privada solo visible al comensal."
        ),
    )


class CompareDishesInput(BaseModel):
    """Inputs del tool ``compare_dishes`` — comparativa lado a lado.

    El comensal pide "¿cuál es mejor, X o Y?", "compará A vs B", "qué
    me conviene". El tool toma 2-4 dishes (por uuid o por nombre) y
    devuelve una estructura comparativa: rating + breakdown por pilar
    + pros/cons agregados de las top reviews. La UI rendereea esto
    como una grilla side-by-side (`ComparisonCard`), distinta del
    listado vertical de `DishCard`.

    Acepta ``dish_ids`` (uuids del output de un search_dishes previo)
    o ``dish_names`` (nombres libres como los dijo el comensal); el
    tool resuelve nombres internamente. NUNCA pidas al humano un uuid.

    Mínimo 2 — comparar uno solo no es comparar. Máximo 4 — más de
    eso satura visualmente la grilla incluso en desktop.
    """

    model_config = ConfigDict(extra="forbid")

    dish_ids: list[str] | None = Field(
        default=None,
        min_length=2,
        max_length=4,
        description=(
            "UUIDs de los platos a comparar. Vienen del output de un "
            "search_dishes previo. NUNCA inventes uuids."
        ),
    )
    dish_names: list[str] | None = Field(
        default=None,
        min_length=2,
        max_length=4,
        description=(
            "Nombres libres de los platos como los dijo el comensal "
            "('el risotto', 'la pasta carbonara'). El tool los "
            "resuelve internamente. Si hay ambigüedad en alguno, el "
            "tool devuelve un payload que te pide elegir."
        ),
    )


class SurpriseMeInput(BaseModel):
    """Inputs del tool ``surprise_me`` — serendipity en lugar de
    búsqueda dirigida.

    El comensal pide algo distinto sin saber qué; el tool elige UN
    plato que esté **fuera del histórico** del comensal (categoría
    o barrio que no frecuenta), respeta sus alergias declaradas, y
    devuelve un ``serendipity_reason`` legible que el agente cita
    en su texto editorial. La selección es estable durante el día
    para el mismo usuario — un "sorprendeme" repetido en la misma
    sesión no rota infinitamente.
    """

    model_config = ConfigDict(extra="forbid")

    neighborhood: str | None = Field(
        default=None,
        validation_alias=AliasChoices("neighborhood", "barrio", "zona"),
        description=(
            "Barrio donde buscar. Si se omite, el tool usa el barrio "
            "más popular de los que el comensal NO frecuenta — eso "
            "es lo que hace 'sorprender'. Pasalo cuando el comensal "
            "explícitamente acota geográficamente ('sorprendeme algo "
            "en Palermo')."
        ),
    )


class RecommendDishesInput(BaseModel):
    """Inputs del tool ``recommend_dishes`` (Sommelier curated grid).

    El agente llama este tool para PRESENTAR al comensal el subset
    de platos que decidió recomendar, después de haber filtrado los
    resultados crudos de ``search_dishes``. La regla de oro: la grid
    visible al comensal debe coincidir 1:1 con lo que el agente
    menciona en su texto editorial. Si el agente solo va a hablar de
    "Café Turco", solo pasa ese dish_id — los otros dishes de la
    búsqueda no se muestran.

    Validación de cantidad: 1-6 dishes. Más de 6 satura la grid
    visualmente; menos de 1 no tiene sentido (si el agente no quiere
    recomendar nada, no llama el tool — preguntá o decí que no
    encontraste).
    """

    model_config = ConfigDict(extra="forbid")

    dish_ids: list[str] = Field(
        min_length=1,
        max_length=6,
        description=(
            "UUIDs de los platos a presentar al comensal como cards. "
            "Vienen siempre del output de un search_dishes previo en "
            "el mismo turno. NUNCA inventes UUIDs. NUNCA pidas al "
            "comensal un dish_id."
        ),
    )


class RequestReservationInput(BaseModel):
    """Inputs del tool ``request_reservation``.

    Pide una mesa en un restaurante concreto. Si el restaurante tiene
    owner verificado, el owner recibe la solicitud por email + push.
    Si no, el tool devuelve un deeplink al partner externo.
    """

    model_config = ConfigDict(extra="forbid")

    restaurant_id: str = Field(
        description=(
            "UUID del restaurante. Viene de search_dishes / "
            "get_dish_detail. NUNCA se lo pedís al comensal."
        ),
    )
    party_size: int = Field(
        ge=1,
        le=30,
        description="Cantidad de comensales en la mesa.",
    )
    requested_for: str = Field(
        description=(
            "ISO 8601 datetime con timezone offset. Ejemplo: "
            "'2026-05-10T21:00:00-03:00'. Si el comensal no menciona "
            "tz, usá la del restaurante (Argentina = -03:00)."
        ),
    )
    message: str | None = Field(
        default=None,
        max_length=600,
        description=(
            "Nota opcional del comensal (alergias, ocasión). El owner "
            "la lee literal."
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
#   JSONSchema serialisation
# ──────────────────────────────────────────────────────────────────────────


def pydantic_to_anthropic_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Render a Pydantic model as an inlined JSONSchema for tool specs.

    Pydantic emits ``$defs`` plus ``$ref`` for nested enums. Inlining
    them keeps the wire payload smaller and avoids surprises with
    consumers that don't fully resolve ``$ref``. Sibling keywords on
    the ``$ref`` node (e.g. ``description``, ``default``) are preserved
    so per-field documentation survives the inline pass.
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
