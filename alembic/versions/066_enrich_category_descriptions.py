"""categories: enrich `description` with example dishes per category

El servicio `category_inference_service` arma el prompt de Gemini con la
tupla (slug, name, description) de cada categoría existente. Una
description rica en platos típicos funciona como few-shot — el modelo
queda anclado al sentido canónico de cada slug y deja de confundir
'pizzeria' con 'italiana' o 'sushi-bar' con 'japonesa'.

Update idempotente: si la categoría ya tiene una description distinta,
**no la pisa** (preserva data custom cargada por admin). Solo llena la
columna cuando estaba NULL o vacía.

Revision ID: 066
Revises: 065
Create Date: 2026-05-13
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "066"
down_revision: Union[str, None] = "065"
branch_labels = None
depends_on = None


# (slug, description) — description ≤ 500 chars. Formato: una línea con
# el rasgo distintivo + 4-6 platos típicos. El idioma del corpus es es-AR.
CATEGORY_DESCRIPTIONS: list[tuple[str, str]] = [
    # Latinoamericanas
    ("argentina", "Cocina argentina: asado, empanadas, milanesas, locro, choripán, dulce de leche"),
    ("brasilena", "Cocina brasileña: feijoada, pão de queijo, moqueca, coxinha, brigadeiro, açaí"),
    ("peruana", "Cocina peruana: ceviche, lomo saltado, ají de gallina, anticuchos, causa, tiradito"),
    ("uruguaya", "Cocina uruguaya: chivito, asado, milanesa, choripán, tortas fritas"),
    ("venezolana", "Cocina venezolana: arepas, cachapas, pabellón criollo, tequeños, empanadas"),
    ("colombiana", "Cocina colombiana: bandeja paisa, arepas, ajiaco, empanadas, sancocho, lechona"),
    ("chilena", "Cocina chilena: empanadas de pino, pastel de choclo, cazuela, completo, chorrillana"),
    ("boliviana", "Cocina boliviana: salteñas, anticuchos, silpancho, pique macho, sopa de maní"),
    ("mexicana", "Cocina mexicana: tacos, quesadillas, enchiladas, guacamole, mole, tamales, fajitas"),
    ("cubana", "Cocina cubana: ropa vieja, moros y cristianos, sandwich cubano, mojito, lechón asado"),
    ("caribena", "Cocina caribeña: arroz con gandules, jerk chicken, mofongo, pescado frito, plátanos"),
    ("estadounidense", "Cocina estadounidense: burgers, BBQ, mac and cheese, hot dogs, cheesecake, buffalo wings"),
    # Europeas
    ("italiana", "Cocina italiana: pizza napoletana, pasta, lasaña, risotto, gnocchi, tiramisú, focaccia"),
    ("espanola", "Cocina española: paella, tortilla, tapas, jamón, gazpacho, pulpo, croquetas"),
    ("francesa", "Cocina francesa: croissant, ratatouille, coq au vin, quiche, crème brûlée, escargot"),
    ("griega", "Cocina griega: souvlaki, moussaka, tzatziki, gyros, dolmas, baklava, ensalada griega"),
    ("alemana", "Cocina alemana: bratwurst, schnitzel, pretzel, chucrut, spätzle, cerveza artesanal"),
    ("portuguesa", "Cocina portuguesa: bacalao, pastel de nata, francesinha, sardinas, caldo verde"),
    # Medio Oriente
    ("arabe", "Cocina árabe: shawarma, hummus, falafel, kibbe, tabule, fatay, baklava"),
    ("israeli", "Cocina israelí: hummus, sabich, shakshuka, falafel, malabi, jalá"),
    ("libanesa", "Cocina libanesa: hummus, kibbe, tabule, fatay, manaqish, shawarma, baklava"),
    ("turca", "Cocina turca: kebab, baklava, börek, dolma, pide, künefe, café turco"),
    ("marroqui", "Cocina marroquí: tagine, cuscús, harira, pastilla, mechoui, té de menta"),
    ("armenia", "Cocina armenia: lahmajun, dolma, manti, kufta, sou beureg, baklava"),
    # Asiáticas
    ("japonesa", "Cocina japonesa: sushi, sashimi, ramen, tempura, gyozas, donburi, udon, takoyaki"),
    ("china", "Cocina china: chow mein, arroz frito, dumplings, pato pekinés, dim sum, mapo tofu"),
    ("coreana", "Cocina coreana: bibimbap, kimchi, bulgogi, samgyeopsal, tteokbokki, korean fried chicken"),
    ("thai", "Cocina tailandesa: pad thai, curry, tom yum, satay, papaya salad, mango sticky rice"),
    ("vietnamita", "Cocina vietnamita: pho, banh mi, rollitos primavera, bún chá, café vietnamita"),
    ("india", "Cocina india: curry, biryani, naan, tandoori, samosas, dal, masala, butter chicken"),
    # Funcionales / formato
    ("parrilla", "Parrilla y asado: bife de chorizo, vacío, mollejas, chorizo, morcilla, achuras"),
    ("burgers", "Hamburguesas: smash burger, cheeseburger, doble bacon, vegana, papas fritas"),
    ("steakhouse", "Steakhouse: cortes premium, ribeye, T-bone, picaña, dry aged, sides clásicos"),
    ("mariscos", "Mariscos y pescados: ceviche, pulpo, calamares, paella, langostinos, ostras"),
    ("tapas", "Tapas y bocados: croquetas, jamón, tortilla, pulpo, pinchos, vermut"),
    ("picadas", "Picadas: tablas de fiambres, quesos, escabeches, conservas, aceitunas, pan"),
    ("sandwiches", "Sándwiches: tostados, lomito, choripán, milanesa, focaccia, bagels"),
    ("empanadas", "Empanadas: carne, pollo, jamón y queso, humita, caprese, árabes, horneadas o fritas"),
    ("bowls", "Bowls saludables: poké, buddha bowl, ramen bowl, açaí, bowls veggie"),
    ("vegano", "Cocina vegana: bowls, falafel, hamburguesas vegetales, tofu, hummus, ensaladas"),
    ("vegetariano", "Cocina vegetariana: pastas, pizzas veggie, ensaladas, omelettes, quesadillas"),
    ("sin-tacc", "Apto celíaco / sin gluten: opciones libres de trigo, cebada y centeno"),
    # Horarios y momentos
    ("brunchs", "Brunch: huevos benedictinos, pancakes, waffles, avocado toast, mimosas, bowls"),
    ("desayunos", "Desayunos: medialunas, tostadas, huevos revueltos, jugos, café, yogur con granola"),
    ("dulces", "Postres y dulces: tortas, alfajores, helados, brownies, cheesecake, churros"),
    ("helados", "Heladerías: helado artesanal, sorbetes, paletas, sundaes, milkshakes"),
    ("pasteleria", "Pastelería: tortas, masas finas, macarons, choux, tartas, petit fours"),
    ("panaderia", "Panadería: pan artesanal, baguettes, focaccia, sourdough, facturas, scones"),
    ("cafeteria", "Cafetería: café de especialidad, capuccino, flat white, tortas, sándwiches, brunch"),
    ("bar", "Bar y coctelería: cócteles clásicos y de autor, vinos, picadas, tapas, after office"),
    ("cerveceria", "Cervecería: cervezas artesanales, IPA, stout, lager, papas, hamburguesas, alitas"),
    # Fallback
    ("otros", "Otros: cocinas o formatos sin categoría específica todavía"),
]


def upgrade() -> None:
    # Solo llena cuando description está NULL o vacía — preserva data
    # custom que el admin haya cargado vía UI antes de esta migration.
    for slug, description in CATEGORY_DESCRIPTIONS:
        op.execute(
            sa.text(
                "UPDATE categories "
                "SET description = :desc "
                "WHERE slug = :slug AND (description IS NULL OR description = '')"
            ).bindparams(desc=description, slug=slug)
        )


def downgrade() -> None:
    # Revertir solo las filas que coinciden exactamente con lo que esta
    # migration escribió: si el admin las editó después, no las tocamos.
    for slug, description in CATEGORY_DESCRIPTIONS:
        op.execute(
            sa.text(
                "UPDATE categories SET description = NULL "
                "WHERE slug = :slug AND description = :desc"
            ).bindparams(slug=slug, desc=description)
        )
