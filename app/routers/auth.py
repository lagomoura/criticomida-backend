import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import (
    attach_auth_cookies,
    clear_auth_cookies,
    create_access_token,
    create_refresh_token_string,
    decode_jwt_strict,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import (
    TokenRefresh,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def revoke_all_refresh_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


async def rotate_refresh_session(
    db: AsyncSession,
    refresh_jwt: str,
) -> tuple[str, str, User]:
    payload = decode_token(refresh_jwt)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )
    jti = payload.get("jti")
    sub = payload.get("sub")
    if not jti or not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    result = await db.execute(
        select(RefreshToken).where(RefreshToken.jti == jti)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    now = datetime.now(timezone.utc)
    if row.revoked_at is not None:
        await revoke_all_refresh_for_user(db, row.user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token reuse detected",
        )
    if row.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )
    if str(row.user_id) != sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_result = await db.execute(select(User).where(User.id == row.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    row.revoked_at = now
    new_jti = str(uuid.uuid4())
    new_exp = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(
        RefreshToken(
            jti=new_jti,
            user_id=user.id,
            expires_at=new_exp,
        )
    )
    access = create_access_token(subject=str(user.id))
    refresh = create_refresh_token_string(subject=str(user.id), jti=new_jti)
    return access, refresh, user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(
        email=user_data.email,
        password_hash=hash_password(user_data.password),
        display_name=user_data.display_name,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: UserLogin,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    result = await db.execute(
        select(User).where(User.email == credentials.email)
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(
        credentials.password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    await revoke_all_refresh_for_user(db, user.id)

    new_jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    new_exp = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(
        RefreshToken(
            jti=new_jti,
            user_id=user.id,
            expires_at=new_exp,
        )
    )

    access = create_access_token(subject=str(user.id))
    refresh = create_refresh_token_string(subject=str(user.id), jti=new_jti)
    attach_auth_cookies(response, access, refresh)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: TokenRefresh,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    raw = request.cookies.get("refresh_token")
    if not raw and body.refresh_token:
        raw = body.refresh_token.strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )
    access, refresh, _user = await rotate_refresh_session(db, raw)
    attach_auth_cookies(response, access, refresh)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_user(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    raw_refresh = request.cookies.get("refresh_token")
    clear_auth_cookies(response)
    if not raw_refresh:
        return None
    try:
        payload = decode_jwt_strict(raw_refresh)
    except JWTError:
        return None
    if payload.get("type") != "refresh":
        return None
    jti = payload.get("jti")
    if not jti:
        return None
    now = datetime.now(timezone.utc)
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.jti == jti,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return None


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    return current_user
