"""dish_root_family lookup: mapea raíz → familia de plato

Revision ID: 060
Revises: 059
Create Date: 2026-05-11

Why:
    Un cheeseburger y un sándwich de bondiola son del mismo "tipo" para el
    duelo, pero sus `dish_root` son distintos ("cheeseburger" vs "sandwich").
    Para que peleen entre sí necesitamos un eje semántico extra: la familia.

    De la misma manera, "ravioles de salmón" vs "ravioles de ricota" YA se
    enfrentan por root exacto, pero un "raviol" vs un "sorrentino" no — y son
    del mismo "tipo" (pasta rellena), así que también deberían pelear.

What:
    1. Tabla `dish_root_family` (`dish_root` PK → `family`) seed con ~80
       entries curados — los platos más comunes del catálogo argentino +
       internacionales habituales. Listo para ampliarse desde un admin tool
       a futuro (UPSERT).
    2. Sin generated column en `dishes`: la familia se resuelve via JOIN al
       lookup en el endpoint, así una corrección en la familia (renombrar
       "burger" → "burgers", merge de "sandwich" y "lomito") no requiere
       recalcular columnas materializadas.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "060"
down_revision: Union[str, None] = "059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Seed inicial. Está curado a mano para minimizar ruido (no quiero que
# "pollo" caiga en "asado" porque eso lo enfrenta con un bife de chorizo).
# Las familias usan slugs internos (lower, sin acentos, kebab-case); el FE
# tiene un i18n dict que las traduce al idioma del viewer.
_SEED_ROWS: list[tuple[str, str]] = [
    # Hamburguesas + smashburgers
    ("hamburguesa", "burger"),
    ("hamburguesas", "burger"),
    ("burger", "burger"),
    ("burgers", "burger"),
    ("cheeseburger", "burger"),
    ("smashburger", "burger"),
    ("smash", "burger"),
    # Sándwiches argentinos
    ("sandwich", "sandwich"),
    ("sandwiches", "sandwich"),
    ("lomito", "sandwich"),
    ("lomitos", "sandwich"),
    ("choripan", "sandwich"),
    ("choripán", "sandwich"),
    ("bondiola", "sandwich"),
    ("milanesa", "milanesa"),
    ("milanesas", "milanesa"),
    # Pizzas
    ("pizza", "pizza"),
    ("pizzas", "pizza"),
    ("muzza", "pizza"),
    ("muzzarella", "pizza"),
    ("mozzarella", "pizza"),
    ("fugazza", "pizza"),
    ("fugazzeta", "pizza"),
    ("calzone", "pizza"),
    ("focaccia", "pizza"),
    # Pasta rellena
    ("ravioles", "pasta-rellena"),
    ("sorrentinos", "pasta-rellena"),
    ("canelones", "pasta-rellena"),
    ("lasagna", "pasta-rellena"),
    ("lasaña", "pasta-rellena"),
    ("agnolotti", "pasta-rellena"),
    # Pasta no rellena
    ("fideos", "pasta"),
    ("spaghetti", "pasta"),
    ("tallarines", "pasta"),
    ("fettuccine", "pasta"),
    ("noquis", "pasta"),
    ("ñoquis", "pasta"),
    ("gnocchi", "pasta"),
    ("penne", "pasta"),
    ("rigatoni", "pasta"),
    ("linguine", "pasta"),
    # Empanadas + típicos
    ("empanada", "empanadas"),
    ("empanadas", "empanadas"),
    ("humita", "empanadas"),
    # Parrilla
    ("asado", "parrilla"),
    ("bife", "parrilla"),
    ("ojo", "parrilla"),
    ("vacio", "parrilla"),
    ("vacío", "parrilla"),
    ("entraña", "parrilla"),
    ("chorizo", "parrilla"),
    ("parrilla", "parrilla"),
    ("matambre", "parrilla"),
    ("costilla", "parrilla"),
    # Sushi
    ("sushi", "sushi"),
    ("roll", "sushi"),
    ("rolls", "sushi"),
    ("nigiri", "sushi"),
    ("sashimi", "sushi"),
    ("temaki", "sushi"),
    ("uramaki", "sushi"),
    # Asian bowls
    ("ramen", "ramen"),
    ("udon", "ramen"),
    ("pho", "ramen"),
    # Mexicano
    ("tacos", "mexicano"),
    ("taco", "mexicano"),
    ("quesadilla", "mexicano"),
    ("quesadillas", "mexicano"),
    ("burrito", "mexicano"),
    ("burritos", "mexicano"),
    ("nachos", "mexicano"),
    ("guacamole", "mexicano"),
    # Mediterráneo
    ("hummus", "mediterraneo"),
    ("falafel", "mediterraneo"),
    ("kebab", "mediterraneo"),
    ("shawarma", "mediterraneo"),
    # Postres
    ("tiramisu", "postres"),
    ("tiramisú", "postres"),
    ("flan", "postres"),
    ("helado", "postres"),
    ("cheesecake", "postres"),
    ("panqueque", "postres"),
    ("volcan", "postres"),
    ("brownie", "postres"),
    ("chocotorta", "postres"),
    ("tarta", "postres"),
    ("torta", "postres"),
    # Ensaladas
    ("ensalada", "ensaladas"),
    ("ensaladas", "ensaladas"),
    ("cesar", "ensaladas"),
    ("caesar", "ensaladas"),
    # Cervezas (la DB ya tiene "ipa" cargado)
    ("ipa", "cerveza"),
    ("lager", "cerveza"),
    ("stout", "cerveza"),
    ("porter", "cerveza"),
    ("apa", "cerveza"),
    ("weizze", "cerveza"),
    ("weisse", "cerveza"),
    # Café
    ("cafe", "cafe"),
    ("café", "cafe"),
    ("cappuccino", "cafe"),
    ("latte", "cafe"),
    # Otros
    ("pollo", "pollo"),
    ("risotto", "risotto"),
    ("paella", "paella"),
    ("salmon", "pescado"),
    ("salmón", "pescado"),
    ("merluza", "pescado"),
]


def upgrade() -> None:
    op.create_table(
        "dish_root_family",
        sa.Column("dish_root", sa.Text(), primary_key=True),
        sa.Column("family", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_dish_root_family_family",
        "dish_root_family",
        ["family"],
    )

    # Las claves vienen como las escribiría el usuario (con acentos), pero
    # `dish_root_extract` ya las normaliza (lower + unaccent). Aplicamos la
    # misma normalización aquí para que el JOIN sea por igualdad estricta.
    # Hacemos esto en SQL via f_unaccent (definida en migración 020) para no
    # depender de unicodedata de Python en runtime de migración.
    bind = op.get_bind()
    for root, family in _SEED_ROWS:
        bind.execute(
            sa.text(
                """
                INSERT INTO dish_root_family (dish_root, family)
                VALUES (lower(public.f_unaccent(:root)), :family)
                ON CONFLICT (dish_root) DO UPDATE SET family = EXCLUDED.family
                """
            ),
            {"root": root, "family": family},
        )


def downgrade() -> None:
    op.drop_index("ix_dish_root_family_family", table_name="dish_root_family")
    op.drop_table("dish_root_family")
