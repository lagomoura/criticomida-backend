from typing import Literal

from pydantic import BaseModel, Field


class OwnerNotificationPreferenceRead(BaseModel):
    notify_on_review: bool


class OwnerNotificationPreferenceUpdate(BaseModel):
    notify_on_review: bool


# Mirror the chat tool's enum catalogue so the settings panel and the
# in-chat tool stay in sync. Adding a new tone or language requires
# updating both this Literal and ``chat/tools/_schemas.py``.
ChatTone = Literal["warm", "professional", "concise", "match_brand"]
ChatLanguage = Literal["es", "en", "pt"]


class OwnerChatPreferenceRead(BaseModel):
    """State of the per-restaurant chat preferences for the owner.

    All three fields are nullable: ``None`` means "no preference set"
    and the agent falls back to prompt defaults (professional tone,
    language adapted to input, no KPI focus).
    """

    tone_preference: ChatTone | None = None
    kpi_focus: list[str] | None = None
    language_preference: ChatLanguage | None = None


class OwnerChatPreferenceUpdate(BaseModel):
    """Full-state replace. ``None`` clears the preference; the settings
    panel always submits the complete form so partial updates are not
    needed at this layer (the in-chat tool handles partial intents)."""

    tone_preference: ChatTone | None = Field(default=None)
    kpi_focus: list[str] | None = Field(default=None)
    language_preference: ChatLanguage | None = Field(default=None)
