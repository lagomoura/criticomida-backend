"""Data-only: invalida los email_token en plano en restaurant_claims.

Audit-driven: MEDIO #7 del audit DB de 2026-05-08. La key
``verification_payload['email_token']`` se guardaba en plano. El router
ahora persiste solo el SHA-256 del token y devuelve el plano una sola
vez en la response del POST /claims.

Esta migración elimina los tokens en plano que quedaron en la base.
Decisión: invalidar todos los activos (los users con un claim pending
tendrán que reabrir el claim para recibir un nuevo token).

Idempotente: el operador JSONB ``-`` es no-op cuando la key no existe.

Revision ID: 052
Revises: 051
Create Date: 2026-05-08
"""

from typing import Sequence, Union

from alembic import op


revision: str = "052"
down_revision: Union[str, None] = "051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE restaurant_claims
           SET verification_payload =
               verification_payload - 'email_token'
         WHERE verification_payload ? 'email_token';
        """
    )


def downgrade() -> None:
    # No hay nada que restaurar — los tokens en plano se perdieron a
    # propósito. El downgrade es no-op.
    pass
