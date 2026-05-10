"""Schemas for the social primitives (follows, likes)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class FollowActionResponse(BaseModel):
    """Response for POST/DELETE /api/users/{id}/follow."""

    follower_id: uuid.UUID
    following_id: uuid.UUID
    following: bool
    # Denormalized count — useful for the caller to update its UI without a
    # round-trip to the public profile endpoint.
    followers_count: int


class FollowerSummary(BaseModel):
    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FollowersPage(BaseModel):
    items: list[FollowerSummary]
    next_cursor: str | None = None


class UserSuggestion(BaseModel):
    """One candidate row from people-you-may-know.

    ``shared_followers`` cuenta a cuántos del grafo de seguidores del viewer
    sigue este candidato (friends-of-friends).
    ``shared_restaurants`` cuenta restaurantes donde tanto el viewer como
    el candidato reseñaron platos (señal de afinidad gastronómica).
    """

    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    shared_followers: int = 0
    shared_restaurants: int = 0


class UserSuggestionsPage(BaseModel):
    items: list[UserSuggestion]


class LikeActionResponse(BaseModel):
    """Response for POST/DELETE /api/reviews/{id}/like."""

    review_id: uuid.UUID
    liked: bool
    likes_count: int


class CommentLikeActionResponse(BaseModel):
    """Response for POST/DELETE /api/comments/{id}/like."""

    comment_id: uuid.UUID
    liked: bool
    likes_count: int
