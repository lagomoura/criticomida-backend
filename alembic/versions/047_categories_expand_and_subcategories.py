"""categories: parent_id self-FK, rename legacy slugs, seed 37 nuevas

Revision ID: 047
Revises: 046
Create Date: 2026-05-06
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels = None
depends_on = None


# (slug_old, slug_new, name_new, display_order_new)
RENAME_PAIRS = [
    ("brazilfood",  "brasilena",  "Brasileña",     102),
    ("peru-food",   "peruana",    "Peruana",       103),
    ("mexico-food", "mexicana",   "Mexicana",      120),
    ("arabic-food", "arabe",      "Árabe",         180),
    ("israelfood",  "israeli",    "Israelí",       181),
    ("japan-food",  "japonesa",   "Japonesa",      200),
    ("chinafood",   "china",      "China",         201),
    ("koreanfood",  "coreana",    "Coreana",       202),
    ("thaifood",    "thai",       "Tailandesa",    203),
    ("parrillas",   "parrilla",   "Parrilla",      230),
    ("burguers",    "burgers",    "Hamburguesas",  140),
]

# 37 categorías nuevas: (slug, name, display_order)
NEW_CATEGORIES = [
    ("argentina",      "Argentina",      100),
    ("uruguaya",       "Uruguaya",       104),
    ("venezolana",     "Venezolana",     105),
    ("colombiana",     "Colombiana",     106),
    ("chilena",        "Chilena",        107),
    ("boliviana",      "Boliviana",      108),
    ("cubana",         "Cubana",         121),
    ("caribena",       "Caribeña",       122),
    ("estadounidense", "Estadounidense", 141),
    ("italiana",       "Italiana",       150),
    ("espanola",       "Española",       151),
    ("francesa",       "Francesa",       152),
    ("griega",         "Griega",         153),
    ("alemana",        "Alemana",        154),
    ("portuguesa",     "Portuguesa",     155),
    ("libanesa",       "Libanesa",       182),
    ("turca",          "Turca",          183),
    ("marroqui",       "Marroquí",       184),
    ("armenia",        "Armenia",        185),
    ("vietnamita",     "Vietnamita",     204),
    ("india",          "India",          205),
    ("steakhouse",     "Steakhouse",     231),
    ("mariscos",       "Mariscos",       240),
    ("tapas",          "Tapas",          302),
    ("picadas",        "Picadas",        303),
    ("sandwiches",     "Sándwiches",     304),
    ("empanadas",      "Empanadas",      305),
    ("bowls",          "Bowls",          306),
    ("vegano",         "Vegano",         307),
    ("vegetariano",    "Vegetariano",    308),
    ("sin-tacc",       "Sin TACC",       309),
    ("pasteleria",     "Pastelería",     332),
    ("panaderia",      "Panadería",      333),
    ("cafeteria",      "Cafetería",      334),
    ("bar",            "Bar",            335),
    ("cerveceria",     "Cervecería",     336),
]


def upgrade() -> None:
    # ── A. Subcategorías: parent_id self-FK (nullable, sin UI todavía) ──
    op.add_column(
        "categories",
        sa.Column("parent_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_categories_parent_id",
        "categories",
        "categories",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_categories_parent_id", "categories", ["parent_id"]
    )

    # ── B. Renombrar 11 slugs (idempotente: WHERE filtra) ──
    for old, new, name, order in RENAME_PAIRS:
        op.execute(
            sa.text(
                "UPDATE categories "
                "SET slug = :new, name = :name, display_order = :ord "
                "WHERE slug = :old"
            ).bindparams(old=old, new=new, name=name, ord=order)
        )

    # ── C. Re-ordenar las que NO se renombran ──
    op.execute("UPDATE categories SET display_order = 300 WHERE slug = 'brunchs'")
    op.execute("UPDATE categories SET display_order = 301 WHERE slug = 'desayunos'")
    op.execute("UPDATE categories SET display_order = 330 WHERE slug = 'dulces'")
    op.execute("UPDATE categories SET display_order = 331 WHERE slug = 'helados'")
    op.execute("UPDATE categories SET display_order = 999 WHERE slug = 'otros'")

    # ── D. Insertar 37 nuevas. Si el slug ya existe (alguien lo creó vía
    #    admin antes que la migración corriera), actualizar `name` y
    #    `display_order` para que la grilla quede consistente. NO tocar
    #    `description`/`image_url` porque pueden tener data custom.
    values_sql = ",\n          ".join(
        f"('{slug}', '{name.replace(chr(39), chr(39)+chr(39))}', NULL, NULL, {order})"
        for slug, name, order in NEW_CATEGORIES
    )
    op.execute(
        f"""
        INSERT INTO categories (slug, name, description, image_url, display_order)
        VALUES
          {values_sql}
        ON CONFLICT (slug) DO UPDATE SET
          name = EXCLUDED.name,
          display_order = EXCLUDED.display_order
        """
    )


def downgrade() -> None:
    # ── D. Borrar nuevas (poner sus FKs en NULL primero) ──
    new_slugs = [c[0] for c in NEW_CATEGORIES]
    op.execute(
        sa.text(
            "UPDATE restaurants SET category_id = NULL "
            "WHERE category_id IN (SELECT id FROM categories WHERE slug = ANY(:slugs))"
        ).bindparams(slugs=new_slugs)
    )
    op.execute(
        sa.text(
            "DELETE FROM categories WHERE slug = ANY(:slugs)"
        ).bindparams(slugs=new_slugs)
    )

    # ── B. Revertir renames ──
    for old, new, name, _ in reversed(RENAME_PAIRS):
        op.execute(
            sa.text(
                "UPDATE categories SET slug = :old, name = :name "
                "WHERE slug = :new"
            ).bindparams(old=old, new=new, name=name)
        )

    # ── A. Quitar parent_id ──
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_constraint(
        "fk_categories_parent_id", "categories", type_="foreignkey"
    )
    op.drop_column("categories", "parent_id")
