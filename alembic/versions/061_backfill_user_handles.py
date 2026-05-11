"""Backfill user handles from display_name for legacy accounts.

Revision ID: 061
Revises: 060
Create Date: 2026-05-11

Why:
    Antes de la migración 009 el handle no existía; usuarios viejos quedaron
    con handle=NULL. Desde 009 el signup lo pide obligatorio. Esta migración
    cierra el gap derivando un handle desde el display_name de cada usuario
    sin handle, para que el lookup por @handle, las menciones y los perfiles
    públicos por handle funcionen para todos los usuarios pre-009.

What:
    Para cada user con handle IS NULL (ordenado por created_at ASC, id ASC):
      1. norm = lower(f_unaccent(display_name))  -- SQL, función ya existente
      2. base = regex_replace(norm, '[^a-z0-9]+', '_'), strip '_' de los bordes
      3. truncar a 30 chars
      4. si len(base) < 3 → dejar NULL
      5. resolver colisiones con sufijo numérico ("aluskies2", "aluskies3"…)
         respetando max 30 chars totales y unicidad case-insensitive (CITEXT)

    El check constraint `^[a-z0-9_]{3,30}$` y el unique partial index
    `ux_users_handle WHERE handle IS NOT NULL` (ambos de la migración 009)
    quedan respetados por construcción.
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "061"
down_revision: Union[str, None] = "060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HANDLE_INVALID_RUN = re.compile(r"[^a-z0-9]+")


def upgrade() -> None:
    bind = op.get_bind()

    # Defensa contra concurrent runs (multi-replica deploys en Railway).
    # Alembic ya usa lock interno para alembic_version, pero un advisory
    # lock extra cuesta nada y protege el cuerpo de esta migración.
    bind.execute(sa.text("SELECT pg_advisory_xact_lock(8061)"))

    # 1. Cargar handles ya tomados. CITEXT es case-insensitive, pero
    # forzamos .lower() defensivamente para el match en memoria.
    taken: set[str] = {
        row[0].lower()
        for row in bind.execute(
            sa.text("SELECT handle FROM users WHERE handle IS NOT NULL")
        )
    }

    # 2. Usuarios pendientes, en orden determinista.
    rows = bind.execute(
        sa.text(
            """
            SELECT id, lower(public.f_unaccent(display_name)) AS norm
              FROM users
             WHERE handle IS NULL
               AND display_name IS NOT NULL
             ORDER BY created_at ASC, id ASC
            """
        )
    ).fetchall()

    print(f"[061] candidates to backfill: {len(rows)}")

    assigned = 0
    skipped_invalid = 0
    skipped_collision = 0

    for user_id, norm in rows:
        base = _HANDLE_INVALID_RUN.sub("_", norm or "").strip("_")
        if len(base) < 3:
            skipped_invalid += 1
            continue
        base = base[:30]
        if len(base) < 3:
            skipped_invalid += 1
            continue

        candidate: str | None = base
        n = 2
        while candidate in taken:
            suffix = str(n)
            candidate = f"{base[: 30 - len(suffix)]}{suffix}"
            n += 1
            if n > 9999:
                print(
                    f"[061] WARNING: collision limit exceeded for "
                    f"base={base!r} user_id={user_id}"
                )
                candidate = None
                break

        if candidate is None or candidate in taken:
            skipped_collision += 1
            continue

        taken.add(candidate)
        bind.execute(
            sa.text("UPDATE users SET handle = :h WHERE id = :id"),
            {"h": candidate, "id": user_id},
        )
        assigned += 1

    print(
        f"[061] done. assigned={assigned} "
        f"skipped_invalid={skipped_invalid} "
        f"skipped_collision={skipped_collision}"
    )


def downgrade() -> None:
    # Data-only migration. No hay forma confiable de saber qué handles fueron
    # asignados por este backfill vs. seteados manualmente por el usuario.
    # Fail-loud antes que revertir silenciosamente nada o destruir datos.
    raise NotImplementedError(
        "061 is a data-only backfill and has no reversible downgrade. "
        "If a manual rollback is required, identify the target users from "
        "git history of the backfill criteria and reset their handle to "
        "NULL via a one-off SQL script."
    )
