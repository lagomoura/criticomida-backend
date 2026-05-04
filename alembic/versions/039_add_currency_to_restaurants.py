"""Agrega currency_code (ISO 4217) opcional a restaurants.

Soporta el campo de precio numérico por reseña: la UI infiere el símbolo
de moneda del restaurante para evitar pedírselo al crítico cada vez. La
columna queda nullable y la UI muestra ``$`` genérico cuando falta. El
backfill cubre los hubs conocidos por ``city`` (case-insensitive); el resto
queda NULL hasta que el flujo de creación de restaurante lo complete o un
admin lo edite.

No hay CHECK estricto sobre el formato — la validación regex
``^[A-Z]{3}$`` se aplica en el schema Pydantic de entrada.

Revision ID: 039
Revises: 038
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mapeo conservador city → currency_code. ILIKE con prefijo cubre variantes
# ("Buenos Aires", "Buenos Aires, Argentina", "Ciudad de Buenos Aires").
_CITY_TO_CURRENCY = (
    ("ARS", ("Buenos Aires", "Mendoza", "Córdoba", "Cordoba", "Rosario", "La Plata")),
    ("BRL", ("São Paulo", "Sao Paulo", "Rio de Janeiro", "Curitiba", "Belo Horizonte", "Porto Alegre")),
    ("USD", ("Miami", "New York", "Los Angeles", "San Francisco", "Chicago")),
)


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("currency_code", sa.String(length=3), nullable=True),
    )
    for currency, cities in _CITY_TO_CURRENCY:
        for city in cities:
            op.execute(
                sa.text(
                    "UPDATE restaurants SET currency_code = :currency "
                    "WHERE currency_code IS NULL AND city ILIKE :city_pattern"
                ).bindparams(currency=currency, city_pattern=f"{city}%")
            )


def downgrade() -> None:
    op.drop_column("restaurants", "currency_code")
