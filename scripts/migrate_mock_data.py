#!/usr/bin/env python3
"""
Migrate ALL mock data from the frontend TypeScript files into the PostgreSQL
database via the FastAPI REST API.

Usage:
    python scripts/migrate_mock_data.py

Requires the backend to be running at http://localhost:8000 with:
  - Admin user seeded: admin@criticomida.com / admin123
  - 15 categories seeded
"""

import sys
import uuid
from datetime import date

import requests

BASE_URL = "http://localhost:8000"

# ---------------------------------------------------------------------------
# Coordinates from app/data/restaurants.ts
# ---------------------------------------------------------------------------
COORDINATES = {
    "Trattoria del Ponte": {"lat": 45.4642, "lng": 9.1900},
    "El Rincon de Juan": {"lat": 36.7213, "lng": -4.4214},
    "Le Procope": {"lat": 48.8530, "lng": 2.3386},
    "Sukiyabashi Jiro": {"lat": 35.6696, "lng": 139.7672},
    "Pujol": {"lat": 19.4326, "lng": -99.1332},
    "Gaggan Anand": {"lat": 13.7563, "lng": 100.5018},
}

# ---------------------------------------------------------------------------
# Category slug mapping (mock variable name -> category slug in DB)
# ---------------------------------------------------------------------------
CATEGORY_SLUG_MAP = {
    "dessertMock": "dulces",
    "burgerMock": "burguers",
    "desayunoMock": "desayunos",
    "japanMock": "japan-food",
    "arabicMock": "arabic-food",
    "israelMock": "israelfood",
    "thaiMock": "thaifood",
    "koreanMock": "koreanfood",
    "chinaMock": "chinafood",
    "parrillaMock": "parrillas",
    "brazilMock": "brazilfood",
    "heladoMock": "helados",
    "peruMock": "peru-food",
    "mexicoMock": "mexico-food",
    "brunchMock": "brunchs",
}

# ---------------------------------------------------------------------------
# ALL mock data extracted from app/restaurants/[id]/page.tsx
# ---------------------------------------------------------------------------

MOCK_DATA = {
    "dessertMock": {
        "dulce-tentacion": {
            "name": "Dulce Tentacion",
            "location": "Palermo, CABA",
            "rating": 4.8,
            "reviewCount": 18,
            "description": "Pasteleria artesanal con los mejores postres y tortas.",
            "pros": ["Gran variedad de postres", "Ambiente acogedor", "Opciones veganas"],
            "cons": ["Precios algo altos", "A veces hay espera"],
            "diary": "Visitamos Dulce Tentacion en una tarde lluviosa. El aroma a chocolate y cafe nos recibio al entrar. Probamos varias tortas y la atencion fue excelente. Un lugar para volver!",
            "plates": [
                {"name": "Torta de Chocolate", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "pros": ["Super humeda", "Chocolate intenso"], "cons": ["Porcion pequena"], "note": "Una de las mejores tortas de chocolate que probamos. Ideal para fans del chocolate."},
                {"name": "Cheesecake de frutos rojos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "pros": ["Frutos frescos", "Base crocante"], "cons": ["Un poco dulce de mas"], "note": "Muy buena textura y sabor, aunque un poco empalagosa."},
                {"name": "Lemon Pie", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "pros": ["Sabor citrico", "Merengue perfecto"], "cons": [], "note": "El equilibrio justo entre acido y dulce."},
            ],
            "gallery": ["/img/food-fallback.jpg", "/img/food-fallback.jpg", "/img/food-fallback.jpg"],
        },
        "la-pasteleria": {
            "name": "La Pasteleria",
            "location": "Recoleta, CABA",
            "rating": 4.7,
            "reviewCount": 12,
            "description": "Tortas y tartas caseras.",
            "pros": ["Tartas frescas", "Ambiente familiar", "Cafe de calidad"],
            "cons": ["Pocas opciones sin TACC"],
            "diary": "Fuimos a La Pasteleria un domingo por la tarde. El lugar estaba lleno pero el servicio fue rapido. Probamos la tarta de manzana y la chocotorta, ambas deliciosas.",
            "plates": [
                {"name": "Tarta de Manzana", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "pros": ["Manzanas frescas", "Masa crocante"], "cons": ["Un poco seca en los bordes"], "note": "La tarta tiene un sabor clasico y reconfortante."},
                {"name": "Chocotorta", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "pros": ["Muy cremosa", "Sabor intenso a chocolate"], "cons": [], "note": "Perfecta para los amantes del chocolate."},
            ],
        },
        "chocolovers": {
            "name": "ChocoLovers",
            "location": "Belgrano, CABA",
            "rating": 4.9,
            "reviewCount": 22,
            "description": "Especialidad en postres de chocolate.",
            "pros": ["Chocolate de calidad", "Opciones sin azucar", "Ambiente moderno"],
            "cons": ["Dificil estacionar"],
            "diary": "ChocoLovers es el paraiso para los fanaticos del chocolate. Probamos el volcan y la mousse, ambos espectaculares. El local es pequeno pero muy calido.",
            "plates": [
                {"name": "Volcan de Chocolate", "date": "2024-06-08", "image": "/img/food-fallback.jpg", "pros": ["Relleno liquido", "Servido caliente"], "cons": ["Porcion pequena"], "note": "El volcan es un must, explosion de chocolate!"},
                {"name": "Mousse de Chocolate", "date": "2024-06-08", "image": "/img/food-fallback.jpg", "pros": ["Textura aireada", "No empalaga"], "cons": [], "note": "Ideal para terminar la comida."},
            ],
        },
        "tarta-co": {
            "name": "Tarta & Co.",
            "location": "Caballito, CABA",
            "rating": 4.6,
            "reviewCount": 10,
            "description": "Tartas dulces y saladas.",
            "pros": ["Variedad de tartas", "Precios accesibles"],
            "cons": ["Pocas mesas"],
            "diary": "Un lugar sencillo pero con tartas muy ricas. Probamos la de ricota y la de frutilla. Ideal para una merienda rapida.",
            "plates": [
                {"name": "Tarta de Ricota", "date": "2024-06-07", "image": "/img/food-fallback.jpg", "pros": ["Ricota suave", "Masa fina"], "cons": [], "note": "Muy buena relacion precio-calidad."},
                {"name": "Tarta de Frutilla", "date": "2024-06-07", "image": "/img/food-fallback.jpg", "pros": ["Frutillas frescas"], "cons": ["Poca crema"], "note": "Fresca y liviana."},
            ],
        },
        "dulzura-real": {
            "name": "Dulzura Real",
            "location": "Almagro, CABA",
            "rating": 4.5,
            "reviewCount": 15,
            "description": "Variedad de dulces y pasteleria.",
            "pros": ["Opciones sin TACC", "Porciones generosas"],
            "cons": ["Demora en la atencion"],
            "diary": "Dulzura Real tiene una gran variedad de dulces. Probamos el brownie y la carrot cake. El local es amplio y luminoso.",
            "plates": [
                {"name": "Brownie", "date": "2024-06-06", "image": "/img/food-fallback.jpg", "pros": ["Muy humedo", "Nueces frescas"], "cons": [], "note": "Ideal para acompanar con cafe."},
                {"name": "Carrot Cake", "date": "2024-06-06", "image": "/img/food-fallback.jpg", "pros": ["Sabor especiado", "Frosting suave"], "cons": ["Un poco denso"], "note": "Muy buena opcion para los que buscan algo distinto."},
            ],
        },
        "postre-express": {
            "name": "Postre Express",
            "location": "San Telmo, CABA",
            "rating": 4.3,
            "reviewCount": 8,
            "description": "Postres rapidos y deliciosos.",
            "pros": ["Servicio rapido", "Precios bajos"],
            "cons": ["Pocas opciones saludables"],
            "diary": "Ideal para una parada rapida. Probamos el flan y la chocotorta. Todo salio en menos de 10 minutos.",
            "plates": [
                {"name": "Flan Casero", "date": "2024-06-05", "image": "/img/food-fallback.jpg", "pros": ["Textura suave", "Mucho dulce de leche"], "cons": [], "note": "Clasico y bien hecho."},
                {"name": "Chocotorta", "date": "2024-06-05", "image": "/img/food-fallback.jpg", "pros": ["Porcion grande"], "cons": ["Un poco empalagosa"], "note": "Ideal para compartir."},
            ],
        },
        "la-dulceria": {
            "name": "La Dulceria",
            "location": "Flores, CABA",
            "rating": 4.4,
            "reviewCount": 11,
            "description": "Dulces tradicionales argentinos.",
            "pros": ["Recetas clasicas", "Ambiente familiar"],
            "cons": ["No aceptan tarjetas"],
            "diary": "Un viaje a la infancia con sabores tradicionales. Probamos el pastelito y la torta rogel.",
            "plates": [
                {"name": "Pastelito", "date": "2024-06-04", "image": "/img/food-fallback.jpg", "pros": ["Masa crocante", "Dulce de membrillo"], "cons": [], "note": "Perfecto para acompanar el mate."},
                {"name": "Torta Rogel", "date": "2024-06-04", "image": "/img/food-fallback.jpg", "pros": ["Mucho dulce de leche", "Merengue casero"], "cons": ["Porcion chica"], "note": "Un clasico bien logrado."},
            ],
        },
        "tentaciones": {
            "name": "Tentaciones",
            "location": "Villa Crespo, CABA",
            "rating": 4.2,
            "reviewCount": 9,
            "description": "Opciones sin gluten y veganas.",
            "pros": ["Sin TACC", "Opciones veganas", "Ambiente relajado"],
            "cons": ["Pocas mesas"],
            "diary": "Un lugar ideal para quienes buscan opciones saludables. Probamos la cookie vegana y el budin de limon.",
            "plates": [
                {"name": "Cookie Vegana", "date": "2024-06-03", "image": "/img/food-fallback.jpg", "pros": ["Sin azucar", "Textura crocante"], "cons": [], "note": "Muy rica y saludable."},
                {"name": "Budin de Limon", "date": "2024-06-03", "image": "/img/food-fallback.jpg", "pros": ["Sabor fresco"], "cons": ["Un poco seco"], "note": "Ideal para acompanar con te."},
            ],
        },
        "sugar-rush": {
            "name": "Sugar Rush",
            "location": "Chacarita, CABA",
            "rating": 4.7,
            "reviewCount": 14,
            "description": "Pasteleria moderna y creativa.",
            "pros": ["Presentacion original", "Sabores innovadores"],
            "cons": ["Precios elevados"],
            "diary": "Sugar Rush sorprende con sus combinaciones. Probamos el cupcake de matcha y la torta de maracuya.",
            "plates": [
                {"name": "Cupcake de Matcha", "date": "2024-06-02", "image": "/img/food-fallback.jpg", "pros": ["Sabor intenso", "Decoracion creativa"], "cons": [], "note": "Diferente y delicioso."},
                {"name": "Torta de Maracuya", "date": "2024-06-02", "image": "/img/food-fallback.jpg", "pros": ["Sabor fresco", "Textura suave"], "cons": ["Un poco acida"], "note": "Ideal para los que buscan algo distinto."},
            ],
        },
        "dulce-final": {
            "name": "Dulce Final",
            "location": "Retiro, CABA",
            "rating": 4.6,
            "reviewCount": 13,
            "description": "El mejor final para tu comida.",
            "pros": ["Postres variados", "Buena atencion"],
            "cons": ["Local pequeno"],
            "diary": "Un cierre perfecto para cualquier comida. Probamos la mousse de limon y el tiramisu.",
            "plates": [
                {"name": "Mousse de Limon", "date": "2024-06-01", "image": "/img/food-fallback.jpg", "pros": ["Muy liviana", "Sabor refrescante"], "cons": [], "note": "Ideal para el verano."},
                {"name": "Tiramisu", "date": "2024-06-01", "image": "/img/food-fallback.jpg", "pros": ["Cafe intenso", "Textura cremosa"], "cons": ["Un poco dulce"], "note": "Muy bien logrado."},
            ],
        },
    },
    "burgerMock": {
        "burger-bros": {
            "name": "Burger Bros",
            "location": "Palermo, CABA",
            "rating": 4.7,
            "reviewCount": 10,
            "description": "Hamburguesas artesanales y papas rusticas.",
            "pros": ["Pan casero", "Carne jugosa", "Papas rusticas"],
            "cons": ["A veces mucha gente", "Precios altos"],
            "diary": "Visitamos Burger Bros un viernes por la noche. El local estaba lleno, pero la atencion fue rapida. Las hamburguesas son grandes y el pan es realmente casero.",
            "plates": [
                {"name": "Cheese Bacon Burger", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Muy sabrosa, el bacon crocante y el queso derretido. La carne al punto justo.", "pros": ["Bacon crocante", "Queso abundante"], "cons": ["Un poco grasosa"]},
                {"name": "Papas Rusticas", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Papas cortadas a mano, bien doradas y crujientes.", "pros": ["Porcion generosa", "Bien condimentadas"], "cons": ["Un poco aceitosas"]},
            ],
        },
        "burger-house": {
            "name": "Burger House",
            "location": "Recoleta, CABA",
            "rating": 4.6,
            "reviewCount": 8,
            "description": "Hamburguesas clasicas.",
            "pros": ["Ambiente familiar", "Salsas caseras"],
            "cons": ["Pocas opciones vegetarianas"],
            "diary": "Ideal para ir en familia. Probamos la clasica y la veggie. Las salsas son un diferencial.",
            "plates": [
                {"name": "Hamburguesa Clasica", "date": "2024-06-08", "image": "/img/food-fallback.jpg", "note": "Simple, bien hecha, pan suave y carne sabrosa.", "pros": ["Sabor clasico", "Pan fresco"], "cons": ["Le faltaba un poco de sal"]},
                {"name": "Veggie Burger", "date": "2024-06-08", "image": "/img/food-fallback.jpg", "note": "Buena opcion vegetariana, aunque un poco seca.", "pros": ["Opcion saludable"], "cons": ["Un poco seca"]},
            ],
        },
        "burger-express": {
            "name": "Burger Express",
            "location": "Belgrano, CABA",
            "rating": 4.5,
            "reviewCount": 6,
            "description": "Hamburguesas rapidas.",
            "pros": ["Servicio rapido", "Buena relacion precio/calidad"],
            "cons": ["Local pequeno"],
            "diary": "Perfecto para una comida rapida. El local es chico pero la atencion es agil.",
            "plates": [
                {"name": "Express Burger", "date": "2024-06-05", "image": "/img/food-fallback.jpg", "note": "Ideal para llevar, carne bien cocida y pan firme.", "pros": ["Rapido", "Economico"], "cons": ["Poco espacio para sentarse"]},
                {"name": "Combo Express", "date": "2024-06-05", "image": "/img/food-fallback.jpg", "note": "Incluye bebida y papas, buena opcion para el mediodia.", "pros": ["Completo", "Buen precio"], "cons": ["Papas algo frias"]},
            ],
        },
        "burger-grill": {
            "name": "Burger Grill",
            "location": "San Telmo, CABA",
            "rating": 4.4,
            "reviewCount": 5,
            "description": "Grill de hamburguesas.",
            "pros": ["Carne a la parrilla", "Salsas originales"],
            "cons": ["Demora en la atencion"],
            "diary": "Las hamburguesas a la parrilla tienen un sabor ahumado unico. Las salsas son caseras y originales.",
            "plates": [
                {"name": "Grill Burger", "date": "2024-06-03", "image": "/img/food-fallback.jpg", "note": "Carne jugosa, pan tostado y salsa especial.", "pros": ["Sabor ahumado", "Salsa especial"], "cons": ["Demora en servir"]},
                {"name": "Papas Grill", "date": "2024-06-03", "image": "/img/food-fallback.jpg", "note": "Papas con especias, bien crocantes.", "pros": ["Bien condimentadas"], "cons": ["Porcion pequena"]},
            ],
        },
        "burger-veggie": {
            "name": "Burger Veggie",
            "location": "Caballito, CABA",
            "rating": 4.3,
            "reviewCount": 4,
            "description": "Opciones vegetarianas.",
            "pros": ["Variedad veggie", "Ingredientes frescos"],
            "cons": ["No hay opciones carnivoras"],
            "diary": "Un paraiso para vegetarianos. Probamos la burger de garbanzos y la de lentejas.",
            "plates": [
                {"name": "Burger de Garbanzos", "date": "2024-06-01", "image": "/img/food-fallback.jpg", "note": "Textura suave, sabor especiado.", "pros": ["Saludable", "Bien condimentada"], "cons": ["Un poco seca"]},
                {"name": "Burger de Lentejas", "date": "2024-06-01", "image": "/img/food-fallback.jpg", "note": "Buena opcion, acompanada de ensalada fresca.", "pros": ["Acompanamiento fresco"], "cons": ["Le faltaba sabor"]},
            ],
        },
        "burger-central": {
            "name": "Burger Central",
            "location": "Almagro, CABA",
            "rating": 4.2,
            "reviewCount": 3,
            "description": "Hamburguesas centricas.",
            "pros": ["Ubicacion conveniente", "Menu variado"],
            "cons": ["Ambiente ruidoso"],
            "diary": "Ideal para una comida rapida en el centro. El menu es variado y hay opciones para todos.",
            "plates": [
                {"name": "Central Burger", "date": "2024-05-29", "image": "/img/food-fallback.jpg", "note": "Buena relacion precio/calidad.", "pros": ["Economica"], "cons": ["Ambiente ruidoso"]},
                {"name": "Combo Central", "date": "2024-05-29", "image": "/img/food-fallback.jpg", "note": "Incluye bebida y papas, opcion completa.", "pros": ["Completo"], "cons": ["Papas poco crocantes"]},
            ],
        },
        "burger-fiesta": {
            "name": "Burger Fiesta",
            "location": "Flores, CABA",
            "rating": 4.7,
            "reviewCount": 10,
            "description": "Fiesta de hamburguesas.",
            "pros": ["Ambiente divertido", "Promos 2x1"],
            "cons": ["Musica alta"],
            "diary": "Ideal para ir con amigos. Las promos 2x1 son geniales y el ambiente es muy animado.",
            "plates": [
                {"name": "Fiesta Burger", "date": "2024-05-27", "image": "/img/food-fallback.jpg", "note": "Hamburguesa doble, mucho queso y salsa especial.", "pros": ["Doble carne", "Salsa especial"], "cons": ["Muy grande"]},
                {"name": "Papas Fiesta", "date": "2024-05-27", "image": "/img/food-fallback.jpg", "note": "Papas con cheddar y verdeo.", "pros": ["Cheddar abundante"], "cons": ["Muy caloricas"]},
            ],
        },
        "burger-friends": {
            "name": "Burger & Friends",
            "location": "Villa Crespo, CABA",
            "rating": 4.6,
            "reviewCount": 7,
            "description": "Ideal para grupos.",
            "pros": ["Mesas grandes", "Atencion rapida"],
            "cons": ["Poca variedad de bebidas"],
            "diary": "Fuimos en grupo y nos atendieron muy bien. Las hamburguesas salieron rapido.",
            "plates": [
                {"name": "Friends Burger", "date": "2024-05-25", "image": "/img/food-fallback.jpg", "note": "Hamburguesa con cebolla caramelizada y panceta.", "pros": ["Cebolla caramelizada", "Panceta crocante"], "cons": ["Un poco salada"]},
                {"name": "Combo Amigos", "date": "2024-05-25", "image": "/img/food-fallback.jpg", "note": "Combo para compartir, incluye papas y bebida.", "pros": ["Ideal para compartir"], "cons": ["Porcion justa"]},
            ],
        },
        "burger-final": {
            "name": "Burger Final",
            "location": "Chacarita, CABA",
            "rating": 4.5,
            "reviewCount": 8,
            "description": "El mejor final hamburguesero.",
            "pros": ["Cierre perfecto", "Postres ricos"],
            "cons": ["Pocas mesas"],
            "diary": "Perfecto para cerrar la noche. Probamos la burger final y un postre casero.",
            "plates": [
                {"name": "Final Burger", "date": "2024-05-23", "image": "/img/food-fallback.jpg", "note": "Hamburguesa con huevo y jamon, muy completa.", "pros": ["Huevo a la plancha", "Jamon cocido"], "cons": ["Muy contundente"]},
                {"name": "Brownie Final", "date": "2024-05-23", "image": "/img/food-fallback.jpg", "note": "Brownie casero, ideal para el postre.", "pros": ["Postre casero"], "cons": ["Muy dulce"]},
            ],
        },
        "burger-king": {
            "name": "Burger King",
            "location": "Retiro, CABA",
            "rating": 4.4,
            "reviewCount": 6,
            "description": "Hamburguesas de reyes.",
            "pros": ["Rapido", "Conocido"],
            "cons": ["Comida rapida estandar"],
            "diary": "Una opcion conocida y rapida. Ideal para salir del paso.",
            "plates": [
                {"name": "Whopper", "date": "2024-05-20", "image": "/img/food-fallback.jpg", "note": "La clasica de la cadena, igual que siempre.", "pros": ["Sabor conocido"], "cons": ["Nada especial"]},
                {"name": "Papas King", "date": "2024-05-20", "image": "/img/food-fallback.jpg", "note": "Papas clasicas de fast food.", "pros": ["Rapidas"], "cons": ["No son caseras"]},
            ],
        },
    },
    "desayunoMock": {
        "cafe-amanecer": {
            "name": "Cafe Amanecer",
            "location": "San Telmo, CABA",
            "rating": 4.4,
            "reviewCount": 7,
            "description": "Desayunos completos y cafe de especialidad.",
            "pros": ["Cafe de especialidad", "Desayuno abundante"],
            "cons": ["Pocas mesas"],
            "diary": "Probamos el desayuno completo y el cafe. El ambiente es tranquilo y la atencion muy buena.",
            "plates": [
                {"name": "Desayuno Completo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye medialunas, jugo y cafe.", "pros": ["Porcion generosa"], "cons": []},
                {"name": "Cafe de especialidad", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Aromatico y bien preparado.", "pros": ["Buen aroma"], "cons": []},
            ],
        },
        "desayuno-feliz": {
            "name": "Desayuno Feliz",
            "location": "Palermo, CABA",
            "rating": 4.7,
            "reviewCount": 10,
            "description": "Desayunos saludables y energeticos.",
            "pros": ["Opciones saludables", "Ambiente alegre"],
            "cons": ["Precios altos"],
            "diary": "El lugar es colorido y la carta muy variada. Probamos el bowl de frutas y el cafe con leche.",
            "plates": [
                {"name": "Bowl de Frutas", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "note": "Frutas frescas y yogur.", "pros": ["Muy fresco"], "cons": []},
                {"name": "Cafe con leche", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "note": "Leche espumosa, cafe suave.", "pros": ["Bien preparado"], "cons": []},
            ],
        },
        "morning-cafe": {
            "name": "Morning Cafe",
            "location": "Recoleta, CABA",
            "rating": 4.6,
            "reviewCount": 8,
            "description": "Cafe de especialidad y medialunas.",
            "pros": ["Medialunas frescas", "Cafe intenso"],
            "cons": ["Poco espacio"],
            "diary": "Probamos las medialunas y el cafe. El local es pequeno pero acogedor.",
            "plates": [
                {"name": "Medialunas", "date": "2024-06-08", "image": "/img/food-fallback.jpg", "note": "Recien horneadas, muy tiernas.", "pros": ["Muy frescas"], "cons": []},
                {"name": "Cafe", "date": "2024-06-08", "image": "/img/food-fallback.jpg", "note": "Intenso y aromatico.", "pros": ["Buen sabor"], "cons": []},
            ],
        },
        "desayuno-express": {
            "name": "Desayuno Express",
            "location": "Belgrano, CABA",
            "rating": 4.3,
            "reviewCount": 5,
            "description": "Rapido y delicioso.",
            "pros": ["Servicio rapido", "Opciones para llevar"],
            "cons": ["Pocas opciones saludables"],
            "diary": "Ideal para un desayuno rapido. Probamos el sandwich de jamon y queso y el jugo de naranja.",
            "plates": [
                {"name": "Sandwich de Jamon y Queso", "date": "2024-06-07", "image": "/img/food-fallback.jpg", "note": "Pan fresco, buen relleno.", "pros": ["Ideal para llevar"], "cons": []},
                {"name": "Jugo de Naranja", "date": "2024-06-07", "image": "/img/food-fallback.jpg", "note": "Natural, exprimido en el momento.", "pros": ["Muy fresco"], "cons": []},
            ],
        },
        "cafe-del-sol": {
            "name": "Cafe del Sol",
            "location": "Caballito, CABA",
            "rating": 4.5,
            "reviewCount": 9,
            "description": "Ambiente calido y menu variado.",
            "pros": ["Ambiente calido", "Menu variado"],
            "cons": ["Demora en la atencion"],
            "diary": "El ambiente es muy agradable. Probamos la tostada con mermelada y el cafe.",
            "plates": [
                {"name": "Tostada con mermelada", "date": "2024-06-06", "image": "/img/food-fallback.jpg", "note": "Pan artesanal, mermelada casera.", "pros": ["Muy rico"], "cons": []},
                {"name": "Cafe", "date": "2024-06-06", "image": "/img/food-fallback.jpg", "note": "Aromatico y suave.", "pros": ["Buen aroma"], "cons": []},
            ],
        },
        "desayuno-real": {
            "name": "Desayuno Real",
            "location": "Almagro, CABA",
            "rating": 4.4,
            "reviewCount": 7,
            "description": "Desayunos abundantes y frescos.",
            "pros": ["Porciones grandes", "Ingredientes frescos"],
            "cons": ["Pocas opciones veganas"],
            "diary": "Probamos el desayuno real y el jugo de pomelo. Todo muy fresco.",
            "plates": [
                {"name": "Desayuno Real", "date": "2024-06-05", "image": "/img/food-fallback.jpg", "note": "Incluye huevos, pan y jugo.", "pros": ["Muy completo"], "cons": []},
                {"name": "Jugo de Pomelo", "date": "2024-06-05", "image": "/img/food-fallback.jpg", "note": "Natural, un poco acido.", "pros": ["Refrescante"], "cons": ["Un poco acido"]},
            ],
        },
        "cafe-pan": {
            "name": "Cafe & Pan",
            "location": "Flores, CABA",
            "rating": 4.6,
            "reviewCount": 10,
            "description": "Panaderia artesanal y cafe.",
            "pros": ["Pan artesanal", "Cafe intenso"],
            "cons": ["Pocas mesas"],
            "diary": "Probamos el pan de campo y el cafe. Todo muy fresco.",
            "plates": [
                {"name": "Pan de Campo", "date": "2024-06-04", "image": "/img/food-fallback.jpg", "note": "Corteza crocante, miga suave.", "pros": ["Muy fresco"], "cons": []},
                {"name": "Cafe", "date": "2024-06-04", "image": "/img/food-fallback.jpg", "note": "Intenso y aromatico.", "pros": ["Buen sabor"], "cons": []},
            ],
        },
        "desayuno-final": {
            "name": "Desayuno Final",
            "location": "Villa Crespo, CABA",
            "rating": 4.2,
            "reviewCount": 6,
            "description": "El mejor desayuno para empezar el dia.",
            "pros": ["Ideal para empezar el dia", "Opciones variadas"],
            "cons": ["Cierra temprano"],
            "diary": "Probamos la tostada francesa y el jugo de naranja. Muy buen desayuno.",
            "plates": [
                {"name": "Tostada Francesa", "date": "2024-06-03", "image": "/img/food-fallback.jpg", "note": "Dulce, bien dorada.", "pros": ["Muy rica"], "cons": []},
                {"name": "Jugo de Naranja", "date": "2024-06-03", "image": "/img/food-fallback.jpg", "note": "Natural, exprimido en el momento.", "pros": ["Muy fresco"], "cons": []},
            ],
        },
        "cafe-central": {
            "name": "Cafe Central",
            "location": "Chacarita, CABA",
            "rating": 4.7,
            "reviewCount": 12,
            "description": "Cafe clasico y ambiente retro.",
            "pros": ["Ambiente retro", "Cafe clasico"],
            "cons": ["Puede estar lleno"],
            "diary": "El ambiente es muy retro y el cafe excelente. Probamos el cafe y la medialuna.",
            "plates": [
                {"name": "Cafe", "date": "2024-06-02", "image": "/img/food-fallback.jpg", "note": "Clasico, bien preparado.", "pros": ["Buen sabor"], "cons": []},
                {"name": "Medialuna", "date": "2024-06-02", "image": "/img/food-fallback.jpg", "note": "Tierna y dulce.", "pros": ["Muy fresca"], "cons": []},
            ],
        },
        "desayuno-co": {
            "name": "Desayuno & Co.",
            "location": "Retiro, CABA",
            "rating": 4.5,
            "reviewCount": 8,
            "description": "Desayunos internacionales.",
            "pros": ["Opciones internacionales", "Ambiente moderno"],
            "cons": ["Precios altos"],
            "diary": "Probamos el desayuno americano y el jugo de manzana. Muy buena experiencia.",
            "plates": [
                {"name": "Desayuno Americano", "date": "2024-06-01", "image": "/img/food-fallback.jpg", "note": "Huevos, bacon y tostadas.", "pros": ["Muy completo"], "cons": []},
                {"name": "Jugo de Manzana", "date": "2024-06-01", "image": "/img/food-fallback.jpg", "note": "Natural, sin azucar.", "pros": ["Muy fresco"], "cons": []},
            ],
        },
        "la-pasteleria": {
            "name": "La Pasteleria",
            "location": "Recoleta, CABA",
            "rating": 4.7,
            "reviewCount": 12,
            "description": "Tortas y tartas caseras.",
            "pros": ["Tartas frescas", "Ambiente familiar", "Cafe de calidad"],
            "cons": ["Pocas opciones sin TACC"],
            "diary": "Fuimos a La Pasteleria un domingo por la tarde. El lugar estaba lleno pero el servicio fue rapido. Probamos la tarta de manzana y la chocotorta, ambas deliciosas.",
            "plates": [
                {"name": "Tarta de Manzana", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "note": "La tarta tiene un sabor clasico y reconfortante.", "pros": ["Manzanas frescas", "Masa crocante"], "cons": ["Un poco seca en los bordes"]},
                {"name": "Chocotorta", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "note": "Perfecta para los amantes del chocolate.", "pros": ["Muy cremosa", "Sabor intenso a chocolate"], "cons": []},
            ],
        },
    },
    "japanMock": {
        "sushi-house": {"name": "Sushi House", "location": "Belgrano, CABA", "rating": 4.9, "reviewCount": 15, "description": "Sushi fresco y ramen tradicional japones.", "pros": ["Sushi fresco", "Ramen autentico"], "cons": ["Precios altos"], "diary": "Probamos el sushi y el ramen. Todo muy fresco y bien presentado.", "plates": [{"name": "Sushi Variado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Piezas frescas, arroz en su punto.", "pros": ["Muy fresco"], "cons": []}, {"name": "Ramen", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Caldo sabroso, fideos al dente.", "pros": ["Caldo intenso"], "cons": []}]},
        "tokyo-bites": {"name": "Tokyo Bites", "location": "Palermo, CABA", "rating": 4.8, "reviewCount": 12, "description": "Comida callejera japonesa.", "pros": ["Street food autentica", "Ambiente moderno"], "cons": ["Poco espacio"], "diary": "Probamos el takoyaki y el yakitori. Muy buena experiencia.", "plates": [{"name": "Takoyaki", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "note": "Bolas de pulpo, bien hechas.", "pros": ["Sabor original"], "cons": []}, {"name": "Yakitori", "date": "2024-06-09", "image": "/img/food-fallback.jpg", "note": "Brochetas de pollo, jugosas.", "pros": ["Bien cocidas"], "cons": []}]},
        "ramen-bar": {"name": "Ramen Bar", "location": "Recoleta, CABA", "rating": 4.7, "reviewCount": 9, "description": "Ramen y gyozas.", "pros": ["Ramen casero", "Gyozas frescas"], "cons": ["Pocas mesas"], "diary": "Probamos el ramen de cerdo y las gyozas. El caldo es muy sabroso y las gyozas bien rellenas.", "plates": [{"name": "Ramen de Cerdo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Caldo intenso, fideos al dente.", "pros": ["Caldo sabroso"], "cons": []}, {"name": "Gyozas", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Relleno jugoso, masa fina.", "pros": ["Bien rellenas"], "cons": []}]},
        "sakura-sushi": {"name": "Sakura Sushi", "location": "San Telmo, CABA", "rating": 4.6, "reviewCount": 8, "description": "Sushi rolls y sake.", "pros": ["Sushi variado", "Sake importado"], "cons": ["Precios altos"], "diary": "Probamos los rolls de salmon y el sake frio. Muy buena calidad y atencion.", "plates": [{"name": "Rolls de Salmon", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salmon fresco, arroz bien preparado.", "pros": ["Salmon fresco"], "cons": []}, {"name": "Sake", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sake frio, suave.", "pros": ["Muy suave"], "cons": []}]},
        "nippon-grill": {"name": "Nippon Grill", "location": "Caballito, CABA", "rating": 4.5, "reviewCount": 7, "description": "Parrilla japonesa y tempura.", "pros": ["Parrilla japonesa", "Tempura crocante"], "cons": ["Demora en la atencion"], "diary": "Probamos la carne a la parrilla y el tempura de langostinos. Sabores autenticos y porciones generosas.", "plates": [{"name": "Carne a la Parrilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne jugosa, bien cocida.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Tempura de Langostinos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Rebozado crocante, langostinos frescos.", "pros": ["Muy crocante"], "cons": []}]},
        "osaka-express": {"name": "Osaka Express", "location": "Almagro, CABA", "rating": 4.4, "reviewCount": 6, "description": "Comida rapida japonesa.", "pros": ["Rapido", "Economico"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el donburi y el yakisoba. Todo salio rapido y estaba bien preparado.", "plates": [{"name": "Donburi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz con pollo y verduras.", "pros": ["Rapido"], "cons": []}, {"name": "Yakisoba", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos salteados, bien condimentados.", "pros": ["Bien condimentado"], "cons": []}]},
        "kyoto-cafe": {"name": "Kyoto Cafe", "location": "Flores, CABA", "rating": 4.3, "reviewCount": 5, "description": "Cafe japones y postres.", "pros": ["Cafe japones", "Postres originales"], "cons": ["Pocas mesas"], "diary": "Probamos el matcha latte y el mochi. El ambiente es tranquilo y los postres muy ricos.", "plates": [{"name": "Matcha Latte", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor intenso, bien preparado.", "pros": ["Muy rico"], "cons": []}, {"name": "Mochi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce japones, textura suave.", "pros": ["Muy suave"], "cons": []}]},
        "samurai-sushi": {"name": "Samurai Sushi", "location": "Villa Crespo, CABA", "rating": 4.2, "reviewCount": 4, "description": "Sushi y platos calientes.", "pros": ["Sushi variado", "Platos calientes"], "cons": ["Pocas opciones vegetarianas"], "diary": "Probamos el sushi caliente y el yakimeshi. Muy buena atencion y sabor.", "plates": [{"name": "Sushi Caliente", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sushi tempurizado, relleno de salmon.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Yakimeshi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz salteado con verduras.", "pros": ["Bien preparado"], "cons": []}]},
        "zen-ramen": {"name": "Zen Ramen", "location": "Chacarita, CABA", "rating": 4.7, "reviewCount": 10, "description": "Ramen vegetariano.", "pros": ["Ramen vegetariano", "Caldo intenso"], "cons": ["Pocas opciones con carne"], "diary": "Probamos el ramen vegetariano y el tofu grillado. El caldo es muy sabroso y el tofu bien preparado.", "plates": [{"name": "Ramen Vegetariano", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Caldo de verduras, fideos al dente.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Tofu Grillado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tofu dorado, bien condimentado.", "pros": ["Bien preparado"], "cons": []}]},
        "tokyo-final": {"name": "Tokyo Final", "location": "Retiro, CABA", "rating": 4.6, "reviewCount": 9, "description": "El mejor final japones.", "pros": ["Postres japoneses", "Ambiente tranquilo"], "cons": ["Pocas mesas"], "diary": "Probamos el dorayaki y el te verde. Muy buen cierre para una comida japonesa.", "plates": [{"name": "Dorayaki", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Panqueque japones relleno de anko.", "pros": ["Muy rico"], "cons": []}, {"name": "Te Verde", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Te suave, bien preparado.", "pros": ["Muy suave"], "cons": []}]},
    },
    "arabicMock": {
        "sabores-de-oriente": {"name": "Sabores de Oriente", "location": "Almagro, CABA", "rating": 4.5, "reviewCount": 7, "description": "Comida arabe autentica: shawarma, falafel y mas.", "pros": ["Shawarma autentico", "Falafel casero"], "cons": ["Demora en la atencion"], "diary": "Probamos el shawarma y el falafel. Muy sabrosos y bien servidos.", "plates": [{"name": "Shawarma", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne bien condimentada.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Falafel", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Crujiente por fuera, suave por dentro.", "pros": ["Bien hecho"], "cons": []}]},
        "el-oasis": {"name": "El Oasis", "location": "Palermo, CABA", "rating": 4.6, "reviewCount": 8, "description": "Ambiente arabe y tes.", "pros": ["Te arabe", "Ambiente autentico"], "cons": ["Puede estar lleno"], "diary": "Probamos el te arabe y el baklava. Muy buena experiencia.", "plates": [{"name": "Te Arabe", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Te especiado, bien servido.", "pros": ["Muy aromatico"], "cons": []}, {"name": "Baklava", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce de nuez y miel.", "pros": ["Muy dulce"], "cons": []}]},
        "damasco-grill": {"name": "Damasco Grill", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 10, "description": "Carnes y especias.", "pros": ["Carnes especiadas", "Ambiente familiar"], "cons": ["Demora en la atencion"], "diary": "Probamos el kebab y el tabule. Muy buena atencion.", "plates": [{"name": "Kebab", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne especiada, bien cocida.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Tabule", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Ensalada fresca de trigo.", "pros": ["Muy fresca"], "cons": []}]},
        "falafel-house": {"name": "Falafel House", "location": "Recoleta, CABA", "rating": 4.8, "reviewCount": 12, "description": "Falafel y hummus.", "pros": ["Falafel casero", "Hummus cremoso"], "cons": ["Pocas mesas"], "diary": "Probamos el falafel y el hummus. Muy buena experiencia.", "plates": [{"name": "Falafel", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Croquetas de garbanzo, bien fritas.", "pros": ["Muy crocante"], "cons": []}, {"name": "Hummus", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso y bien condimentado.", "pros": ["Muy cremoso"], "cons": []}]},
        "shawarma-express": {"name": "Shawarma Express", "location": "San Telmo, CABA", "rating": 4.4, "reviewCount": 5, "description": "Shawarma rapido.", "pros": ["Servicio rapido", "Shawarma sabroso"], "cons": ["Pocas opciones vegetarianas"], "diary": "Probamos el shawarma y el tabule. Todo salio rapido y estaba muy rico.", "plates": [{"name": "Shawarma", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne bien condimentada.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Tabule", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Ensalada fresca de trigo.", "pros": ["Muy fresca"], "cons": []}]},
        "cafe-arabe": {"name": "Cafe Arabe", "location": "Caballito, CABA", "rating": 4.3, "reviewCount": 4, "description": "Cafe y dulces arabes.", "pros": ["Cafe intenso", "Dulces tipicos"], "cons": ["Pocas opciones saladas"], "diary": "Probamos el cafe arabe y el mamul. Muy buena experiencia.", "plates": [{"name": "Cafe Arabe", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cafe fuerte, especiado.", "pros": ["Muy intenso"], "cons": []}, {"name": "Mamul", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce de datiles y nuez.", "pros": ["Muy dulce"], "cons": []}]},
        "el-desierto": {"name": "El Desierto", "location": "Almagro, CABA", "rating": 4.2, "reviewCount": 3, "description": "Comida del desierto.", "pros": ["Platos originales", "Ambiente tematico"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el cordero al horno y el couscous. Muy buena atencion.", "plates": [{"name": "Cordero al Horno", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cordero tierno, bien condimentado.", "pros": ["Muy tierno"], "cons": []}, {"name": "Couscous", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Semola con verduras.", "pros": ["Muy suave"], "cons": []}]},
        "sahara-grill": {"name": "Sahara Grill", "location": "Flores, CABA", "rating": 4.7, "reviewCount": 10, "description": "Grill arabe.", "pros": ["Carnes a la parrilla", "Ambiente animado"], "cons": ["Puede estar lleno"], "diary": "Probamos la carne a la parrilla y el tabule. Muy buena atencion.", "plates": [{"name": "Carne a la Parrilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte jugoso, bien cocido.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Tabule", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Ensalada fresca de trigo.", "pros": ["Muy fresca"], "cons": []}]},
        "beduino": {"name": "Beduino", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 7, "description": "Comida beduina.", "pros": ["Platos originales", "Opciones vegetarianas"], "cons": ["Pocas opciones dulces"], "diary": "Probamos el mansaf y la ensalada arabe. Muy buena experiencia.", "plates": [{"name": "Mansaf", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cordero con yogur y arroz.", "pros": ["Muy original"], "cons": []}, {"name": "Ensalada Arabe", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tomate, pepino y perejil.", "pros": ["Muy fresca"], "cons": []}]},
        "oriente-final": {"name": "Oriente Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 8, "description": "El mejor final arabe.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Puede estar lleno"], "diary": "Probamos el final arabe y el baklava. Muy buen cierre para una comida.", "plates": [{"name": "Final Arabe", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Baklava", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce de nuez y miel.", "pros": ["Muy dulce"], "cons": []}]},
    },
    "israelMock": {
        "tel-aviv-bistro": {"name": "Tel Aviv Bistro", "location": "Colegiales, CABA", "rating": 4.3, "reviewCount": 5, "description": "Sabores israelies modernos y tradicionales.", "pros": ["Platos tipicos", "Opciones vegetarianas"], "cons": ["Pocas mesas"], "diary": "Probamos el shakshuka y el hummus. Muy buena experiencia.", "plates": [{"name": "Shakshuka", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa de tomate especiada.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Hummus", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso y bien condimentado.", "pros": ["Muy cremoso"], "cons": []}]},
        "jerusalen-cafe": {"name": "Jerusalen Cafe", "location": "Palermo, CABA", "rating": 4.6, "reviewCount": 8, "description": "Cafe y platos israelies.", "pros": ["Cafe intenso", "Platos tipicos"], "cons": ["Puede estar lleno"], "diary": "Probamos el cafe y el sabich. Muy buena atencion.", "plates": [{"name": "Cafe Israeli", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cafe fuerte, especiado.", "pros": ["Muy intenso"], "cons": []}, {"name": "Sabich", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Berenjena, huevo y ensalada.", "pros": ["Muy sabroso"], "cons": []}]},
        "sabra-grill": {"name": "Sabra Grill", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 10, "description": "Grill israeli.", "pros": ["Carnes a la parrilla", "Opciones vegetarianas"], "cons": ["Demora en la atencion"], "diary": "Probamos el kebab y la ensalada israeli. Muy buena experiencia.", "plates": [{"name": "Kebab", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne especiada, bien cocida.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Ensalada Israeli", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tomate, pepino y cebolla.", "pros": ["Muy fresca"], "cons": []}]},
        "kibutz-house": {"name": "Kibutz House", "location": "Recoleta, CABA", "rating": 4.8, "reviewCount": 12, "description": "Comida de kibutz.", "pros": ["Platos tipicos", "Ambiente familiar"], "cons": ["Pocas opciones gourmet"], "diary": "Probamos el falafel y el couscous. Muy buena opcion para familias.", "plates": [{"name": "Falafel", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Croquetas de garbanzo, bien fritas.", "pros": ["Muy crocante"], "cons": []}, {"name": "Couscous", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Semola con verduras.", "pros": ["Muy suave"], "cons": []}]},
        "falafel-israel": {"name": "Falafel Israel", "location": "San Telmo, CABA", "rating": 4.4, "reviewCount": 6, "description": "Falafel y hummus.", "pros": ["Falafel casero", "Hummus cremoso"], "cons": ["Pocas mesas"], "diary": "Probamos el falafel y el hummus. Muy buena experiencia.", "plates": [{"name": "Falafel", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Croquetas de garbanzo, bien fritas.", "pros": ["Muy crocante"], "cons": []}, {"name": "Hummus", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso y bien condimentado.", "pros": ["Muy cremoso"], "cons": []}]},
        "cafe-sabich": {"name": "Cafe Sabich", "location": "Caballito, CABA", "rating": 4.3, "reviewCount": 5, "description": "Cafe y sabich.", "pros": ["Cafe intenso", "Sabich tipico"], "cons": ["Pocas opciones dulces"], "diary": "Probamos el cafe y el sabich. Muy buena atencion.", "plates": [{"name": "Cafe Israeli", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cafe fuerte, especiado.", "pros": ["Muy intenso"], "cons": []}, {"name": "Sabich", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Berenjena, huevo y ensalada.", "pros": ["Muy sabroso"], "cons": []}]},
        "shakshuka-bar": {"name": "Shakshuka Bar", "location": "Almagro, CABA", "rating": 4.2, "reviewCount": 4, "description": "Shakshuka y mas.", "pros": ["Shakshuka casera", "Opciones vegetarianas"], "cons": ["Pocas opciones carnivoras"], "diary": "Probamos la shakshuka y la ensalada israeli. Muy buena opcion veggie.", "plates": [{"name": "Shakshuka", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa de tomate especiada.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Ensalada Israeli", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tomate, pepino y cebolla.", "pros": ["Muy fresca"], "cons": []}]},
        "kosher-express": {"name": "Kosher Express", "location": "Flores, CABA", "rating": 4.7, "reviewCount": 10, "description": "Comida kosher rapida.", "pros": ["Kosher rapido", "Opciones variadas"], "cons": ["Pocas opciones gourmet"], "diary": "Probamos el shawarma y el hummus. Muy buena opcion rapida.", "plates": [{"name": "Shawarma", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne bien condimentada.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Hummus", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso y bien condimentado.", "pros": ["Muy cremoso"], "cons": []}]},
        "sabores-de-israel": {"name": "Sabores de Israel", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 7, "description": "Platos tipicos israelies.", "pros": ["Platos tipicos", "Opciones vegetarianas"], "cons": ["Pocas opciones dulces"], "diary": "Probamos el malawach y la ensalada israeli. Muy buena experiencia.", "plates": [{"name": "Malawach", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pan hojaldrado tipico.", "pros": ["Muy original"], "cons": []}, {"name": "Ensalada Israeli", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tomate, pepino y cebolla.", "pros": ["Muy fresca"], "cons": []}]},
        "israel-final": {"name": "Israel Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 8, "description": "El mejor final israeli.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Puede estar lleno"], "diary": "Probamos el final israeli y el rugelach. Muy buen cierre para una comida.", "plates": [{"name": "Final Israeli", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Rugelach", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce tipico, muy sabroso.", "pros": ["Muy dulce"], "cons": []}]},
    },
    "thaiMock": {
        "bangkok-express": {"name": "Bangkok Express", "location": "Palermo, CABA", "rating": 4.6, "reviewCount": 9, "description": "Pad thai, currys y street food tailandes.", "pros": ["Pad thai autentico", "Curry sabroso"], "cons": ["Picante fuerte"], "diary": "Probamos el pad thai y el curry verde. Muy buena experiencia.", "plates": [{"name": "Pad Thai", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos bien salteados.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Curry Verde", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y aromatico.", "pros": ["Buen aroma"], "cons": ["Muy picante"]}]},
        "thai-house": {"name": "Thai House", "location": "Recoleta, CABA", "rating": 4.7, "reviewCount": 10, "description": "Comida tailandesa tradicional.", "pros": ["Comida tradicional", "Ambiente acogedor"], "cons": ["Puede estar lleno"], "diary": "Probamos el curry rojo y el pad thai. Muy buena atencion.", "plates": [{"name": "Curry Rojo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y sabroso.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}, {"name": "Pad Thai", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos de arroz, bien salteados.", "pros": ["Muy sabroso"], "cons": []}]},
        "siam-grill": {"name": "Siam Grill", "location": "Belgrano, CABA", "rating": 4.8, "reviewCount": 12, "description": "Grill tailandes.", "pros": ["Carnes a la parrilla", "Opciones vegetarianas"], "cons": ["Demora en la atencion"], "diary": "Probamos la carne a la parrilla y el curry amarillo. Muy buena experiencia.", "plates": [{"name": "Carne a la Parrilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte jugoso, bien cocido.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Curry Amarillo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y aromatico.", "pros": ["Buen aroma"], "cons": ["Muy picante"]}]},
        "pad-thai-bar": {"name": "Pad Thai Bar", "location": "San Telmo, CABA", "rating": 4.5, "reviewCount": 6, "description": "Especialidad en pad thai.", "pros": ["Pad thai autentico", "Opciones vegetarianas"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el pad thai y el curry verde. Muy buena opcion veggie.", "plates": [{"name": "Pad Thai", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos de arroz, bien salteados.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Curry Verde", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y aromatico.", "pros": ["Buen aroma"], "cons": ["Muy picante"]}]},
        "curry-express": {"name": "Curry Express", "location": "Caballito, CABA", "rating": 4.4, "reviewCount": 5, "description": "Currys rapidos.", "pros": ["Servicio rapido", "Curry casero"], "cons": ["Pocas opciones"], "diary": "Probamos el curry rojo y el arroz jazmin. Todo salio rapido y estaba muy rico.", "plates": [{"name": "Curry Rojo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y sabroso.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}, {"name": "Arroz Jazmin", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Aromatico y bien cocido.", "pros": ["Muy aromatico"], "cons": []}]},
        "bangkok-cafe": {"name": "Bangkok Cafe", "location": "Almagro, CABA", "rating": 4.3, "reviewCount": 4, "description": "Cafe tailandes.", "pros": ["Cafe intenso", "Opciones dulces"], "cons": ["Pocas opciones saladas"], "diary": "Probamos el cafe tailandes y el pastel de coco. Muy buena experiencia.", "plates": [{"name": "Cafe Tailandes", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cafe fuerte, bien preparado.", "pros": ["Muy intenso"], "cons": []}, {"name": "Pastel de Coco", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce tipico, muy sabroso.", "pros": ["Muy dulce"], "cons": []}]},
        "thai-veggie": {"name": "Thai Veggie", "location": "Flores, CABA", "rating": 4.2, "reviewCount": 3, "description": "Opciones vegetarianas.", "pros": ["Opciones vegetarianas", "Sabores originales"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el curry veggie y la ensalada tailandesa. Muy buena opcion veggie.", "plates": [{"name": "Curry Veggie", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Curry de verduras, bien especiado.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Ensalada Tailandesa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Verduras frescas y sesamo.", "pros": ["Muy fresca"], "cons": []}]},
        "sabor-thai": {"name": "Sabor Thai", "location": "Villa Crespo, CABA", "rating": 4.7, "reviewCount": 10, "description": "Sabores tailandeses.", "pros": ["Platos tipicos", "Opciones vegetarianas"], "cons": ["Pocas opciones dulces"], "diary": "Probamos el pad thai y el curry amarillo. Muy buena experiencia.", "plates": [{"name": "Pad Thai", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos de arroz, bien salteados.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Curry Amarillo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y aromatico.", "pros": ["Buen aroma"], "cons": ["Muy picante"]}]},
        "thai-fiesta": {"name": "Thai Fiesta", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 7, "description": "Fiesta tailandesa.", "pros": ["Ambiente festivo", "Platos variados"], "cons": ["Musica alta"], "diary": "Fuimos en grupo y probamos varios platos tailandeses. Muy divertido.", "plates": [{"name": "Pad Thai", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos de arroz, bien salteados.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Curry Verde", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y aromatico.", "pros": ["Buen aroma"], "cons": ["Muy picante"]}]},
        "thai-final": {"name": "Thai Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 8, "description": "El mejor final tailandes.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Puede estar lleno"], "diary": "Probamos el final tailandes y el pastel de mango. Muy buen cierre para una comida.", "plates": [{"name": "Final Tailandes", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Pastel de Mango", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce tipico, muy sabroso.", "pros": ["Muy dulce"], "cons": []}]},
    },
    "koreanMock": {
        "kimchi-house": {"name": "Kimchi House", "location": "Flores, CABA", "rating": 4.4, "reviewCount": 6, "description": "Barbacoa coreana y platos picantes.", "pros": ["Kimchi casero", "Barbacoa autentica"], "cons": ["Picante fuerte"], "diary": "Probamos el kimchi y la barbacoa. Muy buena experiencia.", "plates": [{"name": "Kimchi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y bien fermentado.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}, {"name": "Barbacoa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne jugosa y bien marinada.", "pros": ["Bien marinada"], "cons": []}]},
        "seul-grill": {"name": "Seul Grill", "location": "Palermo, CABA", "rating": 4.6, "reviewCount": 8, "description": "Grill coreano.", "pros": ["Carnes a la parrilla", "Ambiente animado"], "cons": ["Puede estar lleno"], "diary": "Probamos la carne a la parrilla y el bibimbap. Muy buena atencion.", "plates": [{"name": "Carne a la Parrilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte jugoso, bien cocido.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Bibimbap", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz, verduras y huevo.", "pros": ["Muy completo"], "cons": []}]},
        "bibimbap-bar": {"name": "Bibimbap Bar", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 10, "description": "Bibimbap y mas.", "pros": ["Bibimbap autentico", "Opciones vegetarianas"], "cons": ["Demora en la atencion"], "diary": "Probamos el bibimbap y el kimchi. Muy buena experiencia.", "plates": [{"name": "Bibimbap", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz, verduras y carne.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Kimchi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y bien fermentado.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}]},
        "k-pop-cafe": {"name": "K-Pop Cafe", "location": "Recoleta, CABA", "rating": 4.8, "reviewCount": 12, "description": "Cafe y musica coreana.", "pros": ["Cafe intenso", "Ambiente musical"], "cons": ["Puede ser ruidoso"], "diary": "Probamos el cafe y el pastel coreano. Muy buena experiencia.", "plates": [{"name": "Cafe Coreano", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cafe fuerte, bien preparado.", "pros": ["Muy intenso"], "cons": []}, {"name": "Pastel Coreano", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce tipico, muy sabroso.", "pros": ["Muy dulce"], "cons": []}]},
        "kimchi-express": {"name": "Kimchi Express", "location": "San Telmo, CABA", "rating": 4.4, "reviewCount": 5, "description": "Kimchi rapido.", "pros": ["Servicio rapido", "Kimchi casero"], "cons": ["Pocas opciones"], "diary": "Probamos el kimchi y el bulgogi. Todo salio rapido y estaba muy rico.", "plates": [{"name": "Kimchi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y bien fermentado.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}, {"name": "Bulgogi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne marinada, bien cocida.", "pros": ["Muy sabroso"], "cons": []}]},
        "corea-del-sur": {"name": "Corea del Sur", "location": "Caballito, CABA", "rating": 4.3, "reviewCount": 4, "description": "Platos tipicos coreanos.", "pros": ["Platos tradicionales", "Opciones variadas"], "cons": ["Ambiente ruidoso"], "diary": "Probamos el japchae y el kimchi. Muy buena atencion.", "plates": [{"name": "Japchae", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos de batata, verduras.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Kimchi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y bien fermentado.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}]},
        "seul-veggie": {"name": "Seul Veggie", "location": "Almagro, CABA", "rating": 4.2, "reviewCount": 3, "description": "Opciones vegetarianas.", "pros": ["Opciones vegetarianas", "Sabores originales"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el bibimbap veggie y la ensalada coreana. Muy buena opcion veggie.", "plates": [{"name": "Bibimbap Veggie", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz, verduras y tofu.", "pros": ["Muy saludable"], "cons": []}, {"name": "Ensalada Coreana", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Verduras frescas y sesamo.", "pros": ["Muy fresca"], "cons": []}]},
        "korean-bbq": {"name": "Korean BBQ", "location": "Villa Crespo, CABA", "rating": 4.7, "reviewCount": 10, "description": "Barbacoa coreana.", "pros": ["Barbacoa autentica", "Carnes jugosas"], "cons": ["Puede estar lleno"], "diary": "Probamos la barbacoa y el kimchi. Muy buena experiencia.", "plates": [{"name": "Barbacoa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne jugosa, bien marinada.", "pros": ["Muy jugosa"], "cons": []}, {"name": "Kimchi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y bien fermentado.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}]},
        "kimchi-fiesta": {"name": "Kimchi Fiesta", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 7, "description": "Fiesta coreana.", "pros": ["Ambiente festivo", "Platos variados"], "cons": ["Musica alta"], "diary": "Fuimos en grupo y probamos varios platos coreanos. Muy divertido.", "plates": [{"name": "Japchae", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos de batata, verduras.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Kimchi", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante y bien fermentado.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}]},
        "corea-final": {"name": "Corea Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 8, "description": "El mejor final coreano.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Puede estar lleno"], "diary": "Probamos el final coreano y el pastel de arroz. Muy buen cierre para una comida.", "plates": [{"name": "Final Coreano", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Pastel de Arroz", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce tipico, muy sabroso.", "pros": ["Muy dulce"], "cons": []}]},
    },
    "chinaMock": {
        "gran-dragon": {"name": "Gran Dragon", "location": "Belgrano, CABA", "rating": 4.5, "reviewCount": 9, "description": "Clasico restaurante chino con autenticos dim sum y pato laqueado.", "pros": ["Dim sum casero", "Pato laqueado"], "cons": ["Demora en la atencion"], "diary": "Probamos el dim sum y el pato laqueado. Muy buena experiencia.", "plates": [{"name": "Dim Sum", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Relleno jugoso.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Pato Laqueado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Piel crocante, carne tierna.", "pros": ["Bien hecho"], "cons": []}]},
        "palacio-oriental": {"name": "Palacio Oriental", "location": "Microcentro, CABA", "rating": 4.2, "reviewCount": 5, "description": "Especialidad en fideos caseros y platos tradicionales.", "pros": ["Fideos caseros", "Platos abundantes"], "cons": ["Ambiente ruidoso"], "diary": "Probamos los fideos caseros y el pollo agridulce. Platos generosos y sabrosos.", "plates": [{"name": "Fideos Caseros", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos gruesos, bien cocidos.", "pros": ["Muy abundante"], "cons": []}, {"name": "Pollo Agridulce", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa equilibrada, pollo tierno.", "pros": ["Salsa rica"], "cons": []}]},
        "casa-de-te-de-jade": {"name": "Casa de Te de Jade", "location": "Barrio Chino, CABA", "rating": 4.7, "reviewCount": 10, "description": "Experiencia de te y pasteleria china en un ambiente moderno.", "pros": ["Variedad de tes", "Pasteleria fresca"], "cons": ["Precios altos"], "diary": "Probamos el te verde y los pastelitos de loto. El ambiente es moderno y relajante.", "plates": [{"name": "Te Verde", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Aromatico y suave.", "pros": ["Muy aromatico"], "cons": []}, {"name": "Pastelito de Loto", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce tradicional, relleno suave.", "pros": ["Muy fresco"], "cons": []}]},
        "sabor-de-pekin": {"name": "Sabor de Pekin", "location": "Recoleta, CABA", "rating": 4.3, "reviewCount": 6, "description": "Sabores autenticos de Pekin con menu degustacion.", "pros": ["Menu degustacion", "Sabores autenticos"], "cons": ["Porciones pequenas"], "diary": "Probamos el menu degustacion y el pato pekincs. Sabores autenticos y bien presentados.", "plates": [{"name": "Menu Degustacion", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye varios platos tipicos.", "pros": ["Muy variado"], "cons": []}, {"name": "Pato Pekincs", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Piel crocante, carne jugosa.", "pros": ["Muy sabroso"], "cons": []}]},
        "dragon-dorado": {"name": "Dragon Dorado", "location": "Caballito, CABA", "rating": 4.1, "reviewCount": 4, "description": "Comida china tradicional y ambiente familiar.", "pros": ["Ambiente familiar", "Comida tradicional"], "cons": ["Pocas opciones vegetarianas"], "diary": "Fuimos en familia y probamos el arroz frito y el cerdo agridulce. Muy buena atencion.", "plates": [{"name": "Arroz Frito", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz suelto, bien condimentado.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Cerdo Agridulce", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa equilibrada, carne tierna.", "pros": ["Muy rico"], "cons": []}]},
        "mandarin-express": {"name": "Mandarin Express", "location": "Almagro, CABA", "rating": 4.0, "reviewCount": 3, "description": "Rapido, sabroso y economico.", "pros": ["Servicio rapido", "Precios bajos"], "cons": ["Pocas mesas"], "diary": "Ideal para una comida rapida. Probamos el chow mein y el pollo al curry.", "plates": [{"name": "Chow Mein", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos salteados, bien condimentados.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Pollo al Curry", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Curry suave, pollo tierno.", "pros": ["Muy rico"], "cons": []}]},
        "panda-feliz": {"name": "Panda Feliz", "location": "Villa Urquiza, CABA", "rating": 4.4, "reviewCount": 7, "description": "Ideal para familias y grupos grandes.", "pros": ["Ideal para grupos", "Porciones grandes"], "cons": ["Ambiente ruidoso"], "diary": "Fuimos en grupo y probamos el pollo con almendras y el arroz primavera. Porciones abundantes.", "plates": [{"name": "Pollo con Almendras", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pollo tierno, almendras crocantes.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Arroz Primavera", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz con verduras frescas.", "pros": ["Muy fresco"], "cons": []}]},
        "jardin-de-bambu": {"name": "Jardin de Bambu", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 9, "description": "Decoracion tematica y platos vegetarianos.", "pros": ["Decoracion tematica", "Opciones vegetarianas"], "cons": ["Pocas opciones con carne"], "diary": "Probamos el tofu salteado y los fideos de arroz. Ambiente muy agradable.", "plates": [{"name": "Tofu Salteado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tofu suave, bien condimentado.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Fideos de Arroz", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Fideos suaves, verduras frescas.", "pros": ["Muy fresco"], "cons": []}]},
        "sabores-de-shanghai": {"name": "Sabores de Shanghai", "location": "Belgrano, CABA", "rating": 4.3, "reviewCount": 6, "description": "Especialidad en platos de Shanghai.", "pros": ["Platos tipicos", "Sabores originales"], "cons": ["Porciones chicas"], "diary": "Probamos el xiaolongbao y el arroz frito. Sabores originales y bien preparados.", "plates": [{"name": "Xiaolongbao", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dumplings rellenos de caldo.", "pros": ["Muy original"], "cons": []}, {"name": "Arroz Frito", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz suelto, bien condimentado.", "pros": ["Muy sabroso"], "cons": []}]},
        "fenix-rojo": {"name": "Fenix Rojo", "location": "San Telmo, CABA", "rating": 4.2, "reviewCount": 5, "description": "Nuevo en la ciudad, menu degustacion.", "pros": ["Menu degustacion", "Ambiente moderno"], "cons": ["Pocas mesas"], "diary": "Probamos el menu degustacion y el cerdo agridulce. Platos bien presentados y sabrosos.", "plates": [{"name": "Menu Degustacion", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye varios platos tipicos.", "pros": ["Muy variado"], "cons": []}, {"name": "Cerdo Agridulce", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa equilibrada, carne tierna.", "pros": ["Muy rico"], "cons": []}]},
    },
    "parrillaMock": {
        "don-asado": {"name": "Don Asado", "location": "San Nicolas, CABA", "rating": 4.8, "reviewCount": 12, "description": "Parrilla argentina con cortes premium y ambiente familiar.", "pros": ["Cortes premium", "Ambiente familiar"], "cons": ["Precios altos"], "diary": "Probamos el asado y la provoleta. Muy buena experiencia.", "plates": [{"name": "Asado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne jugosa, bien cocida.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Provoleta", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Queso derretido, crocante.", "pros": ["Bien hecha"], "cons": []}]},
        "la-parrilla": {"name": "La Parrilla", "location": "Palermo, CABA", "rating": 4.7, "reviewCount": 10, "description": "Parrilla tradicional argentina.", "pros": ["Ambiente tradicional", "Cortes clasicos"], "cons": ["Puede estar lleno"], "diary": "Fuimos a La Parrilla y probamos el bife de chorizo y la ensalada criolla. Muy buena atencion.", "plates": [{"name": "Bife de Chorizo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte grueso, jugoso.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Ensalada Criolla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tomate, cebolla y morron.", "pros": ["Muy fresca"], "cons": []}]},
        "asado-express": {"name": "Asado Express", "location": "Belgrano, CABA", "rating": 4.6, "reviewCount": 7, "description": "Asado rapido y sabroso.", "pros": ["Servicio rapido", "Buena relacion precio/calidad"], "cons": ["Pocas mesas"], "diary": "Ideal para una comida rapida. Probamos el vacio y las papas fritas.", "plates": [{"name": "Vacio", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tierno y bien cocido.", "pros": ["Muy tierno"], "cons": []}, {"name": "Papas Fritas", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Crocantes y doradas.", "pros": ["Muy crocantes"], "cons": []}]},
        "parrilla-del-sol": {"name": "Parrilla del Sol", "location": "Recoleta, CABA", "rating": 4.5, "reviewCount": 5, "description": "Ambiente soleado y cortes premium.", "pros": ["Ambiente soleado", "Cortes premium"], "cons": ["Precios elevados"], "diary": "Probamos el ojo de bife y la ensalada mixta. Muy buena experiencia.", "plates": [{"name": "Ojo de Bife", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte premium, bien jugoso.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Ensalada Mixta", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Lechuga, tomate y cebolla.", "pros": ["Muy fresca"], "cons": []}]},
        "el-quincho": {"name": "El Quincho", "location": "San Telmo, CABA", "rating": 4.4, "reviewCount": 6, "description": "Quincho familiar y carnes.", "pros": ["Ambiente familiar", "Carnes variadas"], "cons": ["Puede ser ruidoso"], "diary": "Fuimos en familia y probamos la tira de asado y el chorizo. Muy buena atencion.", "plates": [{"name": "Tira de Asado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte clasico, bien cocido.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Chorizo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Bien condimentado.", "pros": ["Muy rico"], "cons": []}]},
        "parrilla-real": {"name": "Parrilla Real", "location": "Caballito, CABA", "rating": 4.3, "reviewCount": 5, "description": "Parrilla abundante y variada.", "pros": ["Porciones abundantes", "Variedad de cortes"], "cons": ["Demora en la atencion"], "diary": "Probamos el matambre a la pizza y la morcilla. Todo muy abundante.", "plates": [{"name": "Matambre a la Pizza", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Matambre tierno, con salsa y queso.", "pros": ["Muy tierno"], "cons": []}, {"name": "Morcilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Clasica, bien cocida.", "pros": ["Muy sabrosa"], "cons": []}]},
        "asado-co": {"name": "Asado & Co.", "location": "Almagro, CABA", "rating": 4.2, "reviewCount": 4, "description": "Asado para compartir.", "pros": ["Ideal para grupos", "Porciones grandes"], "cons": ["Pocas opciones vegetarianas"], "diary": "Fuimos en grupo y probamos la parrillada completa y la ensalada rusa. Muy buena opcion para compartir.", "plates": [{"name": "Parrillada Completa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye varios cortes y achuras.", "pros": ["Muy completa"], "cons": []}, {"name": "Ensalada Rusa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Papa, zanahoria y arvejas.", "pros": ["Muy fresca"], "cons": []}]},
        "parrilla-central": {"name": "Parrilla Central", "location": "Flores, CABA", "rating": 4.7, "reviewCount": 10, "description": "Parrilla centrica y moderna.", "pros": ["Ambiente moderno", "Ubicacion centrica"], "cons": ["Puede estar lleno"], "diary": "Probamos el lomo y la ensalada caprese. Muy buena atencion y ambiente.", "plates": [{"name": "Lomo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte tierno, bien cocido.", "pros": ["Muy tierno"], "cons": []}, {"name": "Ensalada Caprese", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Tomate, mozzarella y albahaca.", "pros": ["Muy fresca"], "cons": []}]},
        "el-fogon": {"name": "El Fogon", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 7, "description": "Fogon tradicional.", "pros": ["Sabor ahumado", "Cortes clasicos"], "cons": ["Pocas mesas"], "diary": "Probamos el costillar y la papa al plomo. Sabor ahumado y tradicional.", "plates": [{"name": "Costillar", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne ahumada, bien cocida.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Papa al Plomo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Papa asada, muy tierna.", "pros": ["Muy tierna"], "cons": []}]},
        "parrilla-final": {"name": "Parrilla Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 8, "description": "El mejor final parrillero.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Puede estar lleno"], "diary": "Probamos el final parrillero y el flan casero. Muy buen cierre para una comida.", "plates": [{"name": "Final Parrillero", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Flan Casero", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Clasico, con dulce de leche.", "pros": ["Muy rico"], "cons": []}]},
    },
    "brazilMock": {
        "sabor-brasil": {"name": "Sabor Brasil", "location": "Centro, CABA", "rating": 4.5, "reviewCount": 7, "description": "Feijoada, caipirinhas y autentica comida brasilena.", "pros": ["Feijoada autentica", "Caipirinhas frescas"], "cons": ["Demora en la atencion"], "diary": "Probamos la feijoada y la caipirinha. Muy buena experiencia.", "plates": [{"name": "Feijoada", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor intenso, bien servida.", "pros": ["Muy sabrosa"], "cons": []}, {"name": "Caipirinha", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Refrescante, bien preparada.", "pros": ["Muy fresca"], "cons": []}]},
        "rio-grill": {"name": "Rio Grill", "location": "Palermo, CABA", "rating": 4.6, "reviewCount": 9, "description": "Grill brasileno.", "pros": ["Carnes a la parrilla", "Ambiente animado"], "cons": ["Puede estar lleno"], "diary": "Fuimos a Rio Grill y probamos la picanha y el arroz carreteiro. Muy buena atencion.", "plates": [{"name": "Picanha", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte tipico, jugoso.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Arroz Carreteiro", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz con carne y verduras.", "pros": ["Muy sabroso"], "cons": []}]},
        "bahia-cafe": {"name": "Bahia Cafe", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 10, "description": "Cafe y postres brasileneos.", "pros": ["Postres caseros", "Cafe intenso"], "cons": ["Pocas mesas"], "diary": "Probamos el brigadeiro y el cafe brasileno. Todo muy rico.", "plates": [{"name": "Brigadeiro", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce de chocolate tipico.", "pros": ["Muy dulce"], "cons": []}, {"name": "Cafe Brasileno", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Intenso y aromatico.", "pros": ["Muy intenso"], "cons": []}]},
        "samba-house": {"name": "Samba House", "location": "Recoleta, CABA", "rating": 4.8, "reviewCount": 12, "description": "Samba y comida tipica.", "pros": ["Musica en vivo", "Comida tipica"], "cons": ["Ruidoso"], "diary": "Fuimos a Samba House y disfrutamos de la musica y la feijoada. Muy buen ambiente.", "plates": [{"name": "Feijoada", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Clasica, bien servida.", "pros": ["Muy sabrosa"], "cons": []}, {"name": "Coxinha", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Croqueta de pollo tipica.", "pros": ["Muy crocante"], "cons": []}]},
        "feijoada-express": {"name": "Feijoada Express", "location": "San Telmo, CABA", "rating": 4.4, "reviewCount": 5, "description": "Feijoada rapida.", "pros": ["Servicio rapido", "Buena relacion precio/calidad"], "cons": ["Pocas opciones"], "diary": "Probamos la feijoada y el pastel de queijo. Todo salio rapido y estaba muy rico.", "plates": [{"name": "Feijoada", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Rapida pero sabrosa.", "pros": ["Muy sabrosa"], "cons": []}, {"name": "Pastel de Queijo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Empanada de queso tipica.", "pros": ["Muy rico"], "cons": []}]},
        "brasil-veggie": {"name": "Brasil Veggie", "location": "Caballito, CABA", "rating": 4.3, "reviewCount": 4, "description": "Opciones vegetarianas.", "pros": ["Opciones vegetarianas", "Sabores originales"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos la moqueca de banana y la ensalada tropical. Muy buena opcion veggie.", "plates": [{"name": "Moqueca de Banana", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Guiso de banana tipico.", "pros": ["Muy original"], "cons": []}, {"name": "Ensalada Tropical", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Frutas frescas y verdes.", "pros": ["Muy fresca"], "cons": []}]},
        "churrasco-bar": {"name": "Churrasco Bar", "location": "Almagro, CABA", "rating": 4.2, "reviewCount": 3, "description": "Churrasco y caipirinhas.", "pros": ["Churrasco a la lena", "Caipirinhas"], "cons": ["Puede ser ruidoso"], "diary": "Probamos el churrasco y la caipirinha. Muy buena experiencia.", "plates": [{"name": "Churrasco", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne a la lena, bien cocida.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Caipirinha", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Refrescante y bien preparada.", "pros": ["Muy fresca"], "cons": []}]},
        "sabor-carioca": {"name": "Sabor Carioca", "location": "Flores, CABA", "rating": 4.7, "reviewCount": 10, "description": "Sabores cariocas.", "pros": ["Platos tipicos", "Ambiente alegre"], "cons": ["Pocas mesas"], "diary": "Probamos la feijoada y el pudim de leite. Todo muy sabroso.", "plates": [{"name": "Feijoada", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Clasica, bien servida.", "pros": ["Muy sabrosa"], "cons": []}, {"name": "Pudim de Leite", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Flan de leche tipico.", "pros": ["Muy dulce"], "cons": []}]},
        "brasil-fiesta": {"name": "Brasil Fiesta", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 7, "description": "Fiesta brasilena.", "pros": ["Ambiente festivo", "Musica en vivo"], "cons": ["Ruidoso"], "diary": "Fuimos en grupo y disfrutamos de la musica y la comida tipica. Probamos la farofa y la caipirinha.", "plates": [{"name": "Farofa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Harina de mandioca tostada.", "pros": ["Muy original"], "cons": []}, {"name": "Caipirinha", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Refrescante y bien preparada.", "pros": ["Muy fresca"], "cons": []}]},
        "brasil-final": {"name": "Brasil Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 8, "description": "El mejor final brasileno.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Puede estar lleno"], "diary": "Probamos el final brasileno y el brigadeiro. Muy buen cierre para una comida.", "plates": [{"name": "Final Brasileno", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Corte especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Brigadeiro", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce de chocolate tipico.", "pros": ["Muy dulce"], "cons": []}]},
    },
    "heladoMock": {
        "heladeria-italia": {"name": "Heladeria Italia", "location": "Caballito, CABA", "rating": 4.9, "reviewCount": 12, "description": "Helados artesanales con sabores unicos.", "pros": ["Helado artesanal", "Sabores originales"], "cons": ["Colas largas"], "diary": "Probamos el helado de pistacho y el de dulce de leche. Muy buena experiencia.", "plates": [{"name": "Helado de Pistacho", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor intenso, cremoso.", "pros": ["Muy cremoso"], "cons": []}, {"name": "Helado de Dulce de Leche", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Clasico, bien logrado.", "pros": ["Muy rico"], "cons": []}]},
        "helado-feliz": {"name": "Helado Feliz", "location": "Palermo, CABA", "rating": 4.8, "reviewCount": 10, "description": "Helados felices.", "pros": ["Ambiente alegre", "Helados cremosos"], "cons": ["Pocas mesas"], "diary": "Fuimos a Helado Feliz y probamos el helado de frutilla y el de chocolate. El local es pequeno pero muy colorido.", "plates": [{"name": "Helado de Frutilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Frutilla natural, muy refrescante.", "pros": ["Muy fresco"], "cons": []}, {"name": "Helado de Chocolate", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Chocolate intenso, cremoso.", "pros": ["Sabor intenso"], "cons": []}]},
        "helado-express": {"name": "Helado Express", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 8, "description": "Helados rapidos.", "pros": ["Servicio rapido", "Precios accesibles"], "cons": ["Pocas opciones gourmet"], "diary": "Ideal para una parada rapida. Probamos el helado de vainilla y el de limon.", "plates": [{"name": "Helado de Vainilla", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Clasico, suave y cremoso.", "pros": ["Muy suave"], "cons": []}, {"name": "Helado de Limon", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Refrescante, ideal para el verano.", "pros": ["Muy refrescante"], "cons": []}]},
        "helado-house": {"name": "Helado House", "location": "San Telmo, CABA", "rating": 4.6, "reviewCount": 7, "description": "Casa de helados.", "pros": ["Variedad de sabores", "Ambiente familiar"], "cons": ["Puede estar lleno"], "diary": "Visitamos Helado House y probamos el helado de crema americana y el de dulce de leche granizado.", "plates": [{"name": "Helado de Crema Americana", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Muy cremoso, sabor clasico.", "pros": ["Muy cremoso"], "cons": []}, {"name": "Helado de Dulce de Leche Granizado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce de leche con trozos de chocolate.", "pros": ["Trozos de chocolate"], "cons": []}]},
        "helado-veggie": {"name": "Helado Veggie", "location": "Caballito, CABA", "rating": 4.5, "reviewCount": 6, "description": "Opciones veganas.", "pros": ["Opciones veganas", "Sabores originales"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el helado de coco vegano y el de frutos rojos. Muy buena opcion para veganos.", "plates": [{"name": "Helado de Coco Vegano", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso, sabor a coco natural.", "pros": ["Muy cremoso"], "cons": []}, {"name": "Helado de Frutos Rojos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Frutos rojos frescos, sin lacteos.", "pros": ["Sin lacteos"], "cons": []}]},
        "helado-central": {"name": "Helado Central", "location": "Almagro, CABA", "rating": 4.4, "reviewCount": 5, "description": "Helados centricos.", "pros": ["Ubicacion centrica", "Atencion rapida"], "cons": ["Ambiente ruidoso"], "diary": "Fuimos a Helado Central y probamos el helado de menta granizada y el de crema del cielo.", "plates": [{"name": "Helado de Menta Granizada", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Menta fresca, trozos de chocolate.", "pros": ["Muy refrescante"], "cons": []}, {"name": "Helado de Crema del Cielo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Color llamativo, sabor suave.", "pros": ["Muy suave"], "cons": []}]},
        "helado-fiesta": {"name": "Helado Fiesta", "location": "Flores, CABA", "rating": 4.3, "reviewCount": 4, "description": "Fiesta de helados.", "pros": ["Ambiente festivo", "Promos 2x1"], "cons": ["Musica alta"], "diary": "Fuimos en grupo y aprovechamos la promo 2x1. Probamos el helado de banana split y el de chocolate blanco.", "plates": [{"name": "Helado de Banana Split", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Banana, chocolate y crema.", "pros": ["Muy divertido"], "cons": []}, {"name": "Helado de Chocolate Blanco", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso, sabor suave.", "pros": ["Muy cremoso"], "cons": []}]},
        "helado-friends": {"name": "Helado & Friends", "location": "Villa Crespo, CABA", "rating": 4.2, "reviewCount": 3, "description": "Ideal para grupos.", "pros": ["Mesas grandes", "Atencion rapida"], "cons": ["Poca variedad de sabores"], "diary": "Fuimos en grupo y probamos varios sabores. El local es ideal para compartir.", "plates": [{"name": "Helado de Sambayon", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor clasico, bien logrado.", "pros": ["Muy clasico"], "cons": []}, {"name": "Helado de Tiramisu", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor a cafe y cacao.", "pros": ["Muy original"], "cons": []}]},
        "helado-final": {"name": "Helado Final", "location": "Chacarita, CABA", "rating": 4.1, "reviewCount": 4, "description": "El mejor final heladero.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Pocas mesas"], "diary": "Probamos el helado final y el postre especial. Muy buen cierre para una salida.", "plates": [{"name": "Helado Final", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor especial de la casa.", "pros": ["Muy especial"], "cons": []}, {"name": "Postre Especial", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Helado con salsa de frutos rojos.", "pros": ["Muy rico"], "cons": []}]},
        "helado-rey": {"name": "Helado Rey", "location": "Retiro, CABA", "rating": 4.0, "reviewCount": 3, "description": "Helados de reyes.", "pros": ["Helados premium", "Atencion cordial"], "cons": ["Precios altos"], "diary": "Probamos el helado de crema rusa y el de chocolate amargo. Muy buena calidad.", "plates": [{"name": "Helado de Crema Rusa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Cremoso, sabor delicado.", "pros": ["Muy cremoso"], "cons": []}, {"name": "Helado de Chocolate Amargo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Chocolate intenso, poco dulce.", "pros": ["Sabor intenso"], "cons": []}]},
    },
    "peruMock": {
        "ceviche-lima": {"name": "Ceviche Lima", "location": "Retiro, CABA", "rating": 4.9, "reviewCount": 15, "description": "Ceviches frescos y deliciosos.", "pros": ["Ceviches frescos", "Ambiente acogedor"], "cons": ["Puede estar lleno"], "diary": "Probamos el ceviche mixto y el ceviche de pescado. Muy buena experiencia.", "plates": [{"name": "Ceviche Mixto", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pescado, camarones y calamar.", "pros": ["Muy fresco"], "cons": []}, {"name": "Ceviche de Pescado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pescado fresco, limon y aji.", "pros": ["Muy sabroso"], "cons": []}]},
        "peru-central": {"name": "Peru Central", "location": "Palermo, CABA", "rating": 4.8, "reviewCount": 12, "description": "Comida peruana autentica.", "pros": ["Platos tipicos", "Ambiente animado"], "cons": ["Puede ser ruidoso"], "diary": "Probamos el lomo saltado y el arroz con pollo. Muy buena atencion.", "plates": [{"name": "Lomo Saltado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne de res, verduras y fideos.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Arroz con Pollo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pollo, arroz y verduras.", "pros": ["Muy completo"], "cons": []}]},
        "peru-express": {"name": "Peru Express", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 10, "description": "Comida peruana rapida y deliciosa.", "pros": ["Servicio rapido", "Buena relacion precio/calidad"], "cons": ["Pocas mesas"], "diary": "Ideal para una comida rapida. Probamos el pollo a la brasa y el arroz chaufa.", "plates": [{"name": "Pollo a la Brasa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pollo jugoso y bien cocido.", "pros": ["Muy jugoso"], "cons": []}, {"name": "Arroz Chaufa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz frito con verduras y carne.", "pros": ["Muy sabroso"], "cons": []}]},
        "peru-house": {"name": "Peru House", "location": "San Telmo, CABA", "rating": 4.6, "reviewCount": 8, "description": "Casa de comida peruana.", "pros": ["Variedad de platos", "Ambiente familiar"], "cons": ["Puede estar lleno"], "diary": "Visitamos Peru House y probamos el ceviche de pescado y el lomo saltado. Muy buena opcion.", "plates": [{"name": "Ceviche de Pescado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pescado fresco, limon y aji.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Lomo Saltado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne de res, verduras y fideos.", "pros": ["Muy sabroso"], "cons": []}]},
        "peru-veggie": {"name": "Peru Veggie", "location": "Caballito, CABA", "rating": 4.5, "reviewCount": 6, "description": "Opciones vegetarianas y veganas.", "pros": ["Opciones vegetarianas", "Sabores originales"], "cons": ["Pocas opciones tradicionales"], "diary": "Probamos el arroz con verduras y el aji de gallina vegano. Muy buena opcion veggie.", "plates": [{"name": "Arroz con Verduras", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz con una variedad de verduras.", "pros": ["Muy completo"], "cons": []}, {"name": "Aji de Gallina Vegano", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa de aji amarillo y papa.", "pros": ["Muy sabroso"], "cons": []}]},
        "peru-central-soho": {"name": "Peru Central", "location": "Almagro, CABA", "rating": 4.4, "reviewCount": 5, "description": "Comida peruana centrica.", "pros": ["Ubicacion centrica", "Atencion rapida"], "cons": ["Ambiente ruidoso"], "diary": "Fuimos a Peru Central y probamos el ceviche de camarones y el arroz con pollo.", "plates": [{"name": "Ceviche de Camarones", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Camarones frescos, limon y aji.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Arroz con Pollo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pollo, arroz y verduras.", "pros": ["Muy completo"], "cons": []}]},
        "peru-fiesta": {"name": "Peru Fiesta", "location": "Flores, CABA", "rating": 4.3, "reviewCount": 4, "description": "Fiesta peruana.", "pros": ["Ambiente festivo", "Musica en vivo"], "cons": ["Ruidoso"], "diary": "Fuimos en grupo y disfrutamos de la musica y la comida peruana. Probamos el lomo saltado y el arroz chaufa.", "plates": [{"name": "Lomo Saltado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne de res, verduras y fideos.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Arroz Chaufa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz frito con verduras y carne.", "pros": ["Muy sabroso"], "cons": []}]},
        "peru-friends": {"name": "Peru & Friends", "location": "Villa Crespo, CABA", "rating": 4.2, "reviewCount": 3, "description": "Ideal para grupos.", "pros": ["Mesas grandes", "Atencion rapida"], "cons": ["Poca variedad de sabores"], "diary": "Fuimos en grupo y probamos varios platos. El local es ideal para compartir.", "plates": [{"name": "Ceviche Mixto", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pescado, camarones y calamar.", "pros": ["Muy fresco"], "cons": []}, {"name": "Lomo Saltado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne de res, verduras y fideos.", "pros": ["Muy sabroso"], "cons": []}]},
        "peru-final": {"name": "Peru Final", "location": "Chacarita, CABA", "rating": 4.1, "reviewCount": 4, "description": "El mejor final peruano.", "pros": ["Cierre perfecto", "Postres ricos"], "cons": ["Pocas mesas"], "diary": "Probamos el postre de mazamorra y el arroz con leche. Muy buen cierre para una comida.", "plates": [{"name": "Postre de Mazamorra", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Postre de maiz dulce y leche.", "pros": ["Muy dulce"], "cons": []}, {"name": "Arroz con Leche", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Arroz cremoso con leche y canela.", "pros": ["Muy rico"], "cons": []}]},
        "peru-rey": {"name": "Peru Rey", "location": "Retiro, CABA", "rating": 4.0, "reviewCount": 3, "description": "Comida peruana de reyes.", "pros": ["Platos premium", "Atencion cordial"], "cons": ["Precios altos"], "diary": "Probamos el ceviche de pescado y el arroz con pollo. Muy buena calidad.", "plates": [{"name": "Ceviche de Pescado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pescado fresco, limon y aji.", "pros": ["Muy sabroso"], "cons": []}, {"name": "Arroz con Pollo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pollo, arroz y verduras.", "pros": ["Muy completo"], "cons": []}]},
    },
    "mexicoMock": {
        "la-lupita": {"name": "La Lupita", "location": "Villa Crespo, CABA", "rating": 4.7, "reviewCount": 11, "description": "Tacos, burritos y margaritas en un ambiente colorido.", "pros": ["Tacos autenticos", "Margaritas frescas"], "cons": ["Puede estar lleno"], "diary": "Probamos los tacos al pastor y las margaritas. El ambiente es muy colorido y animado.", "plates": [{"name": "Tacos al Pastor", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne jugosa, pina fresca.", "pros": ["Muy sabrosos"], "cons": []}, {"name": "Margarita", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Refrescante, bien preparada.", "pros": ["Muy fresca"], "cons": []}]},
        "el-mariachi": {"name": "El Mariachi", "location": "Palermo, CABA", "rating": 4.6, "reviewCount": 9, "description": "Comida mexicana tradicional y musica en vivo.", "pros": ["Musica en vivo", "Comida tradicional"], "cons": ["Ruidoso"], "diary": "Fuimos a cenar y disfrutamos de la musica en vivo. Probamos el guacamole y los nachos.", "plates": [{"name": "Guacamole", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Aguacate fresco, bien condimentado.", "pros": ["Muy fresco"], "cons": []}, {"name": "Nachos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Crujientes, con mucho queso.", "pros": ["Bien servidos"], "cons": []}]},
        "azteca-grill": {"name": "Azteca Grill", "location": "Belgrano, CABA", "rating": 4.8, "reviewCount": 13, "description": "Carnes y salsas picantes.", "pros": ["Carnes jugosas", "Salsas picantes"], "cons": ["Picante fuerte"], "diary": "Probamos el burrito de carne y la salsa roja. Muy sabroso pero picante.", "plates": [{"name": "Burrito de Carne", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Carne tierna, salsa picante.", "pros": ["Muy sabroso"], "cons": ["Muy picante"]}, {"name": "Salsa Roja", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Picante, pero deliciosa.", "pros": ["Buen sabor"], "cons": ["Muy picante"]}]},
        "taco-loco": {"name": "Taco Loco", "location": "Recoleta, CABA", "rating": 4.5, "reviewCount": 7, "description": "Tacos y nachos para compartir.", "pros": ["Tacos variados", "Nachos crujientes"], "cons": ["Porciones chicas"], "diary": "Ideal para compartir con amigos. Probamos los tacos de pollo y los nachos con queso.", "plates": [{"name": "Tacos de Pollo", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pollo tierno, tortillas suaves.", "pros": ["Bien servidos"], "cons": []}, {"name": "Nachos con Queso", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Queso derretido, nachos crujientes.", "pros": ["Muy ricos"], "cons": []}]},
        "cantina-frida": {"name": "Cantina Frida", "location": "San Telmo, CABA", "rating": 4.4, "reviewCount": 8, "description": "Ambiente artistico y margaritas.", "pros": ["Decoracion artistica", "Margaritas"], "cons": ["Pocas mesas"], "diary": "El ambiente es muy original. Probamos la margarita y los tacos vegetarianos.", "plates": [{"name": "Margarita", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Bien preparada, refrescante.", "pros": ["Muy fresca"], "cons": []}, {"name": "Tacos Vegetarianos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Relleno sabroso, tortillas suaves.", "pros": ["Opcion veggie"], "cons": []}]},
        "chili-house": {"name": "Chili House", "location": "Caballito, CABA", "rating": 4.3, "reviewCount": 6, "description": "Chili con carne y cervezas artesanales.", "pros": ["Chili casero", "Buena cerveza"], "cons": ["Picante fuerte"], "diary": "Probamos el chili con carne y la cerveza artesanal. Muy picante pero sabroso.", "plates": [{"name": "Chili con Carne", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Muy picante, carne tierna.", "pros": ["Buen sabor"], "cons": ["Muy picante"]}, {"name": "Cerveza Artesanal", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Rubia, bien fria.", "pros": ["Muy fresca"], "cons": []}]},
        "guacamole-bar": {"name": "Guacamole Bar", "location": "Almagro, CABA", "rating": 4.2, "reviewCount": 5, "description": "Guacamole fresco y tacos.", "pros": ["Guacamole fresco", "Tacos variados"], "cons": ["Pocas opciones de postre"], "diary": "Probamos el guacamole y los tacos de pescado. Todo muy fresco.", "plates": [{"name": "Guacamole", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Aguacate fresco, bien condimentado.", "pros": ["Muy fresco"], "cons": []}, {"name": "Tacos de Pescado", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pescado fresco, tortillas suaves.", "pros": ["Muy sabrosos"], "cons": []}]},
        "sabor-azteca": {"name": "Sabor Azteca", "location": "Flores, CABA", "rating": 4.7, "reviewCount": 10, "description": "Especialidad en enchiladas.", "pros": ["Enchiladas caseras", "Salsas variadas"], "cons": ["Picante fuerte"], "diary": "Probamos las enchiladas verdes y la salsa de mole. Muy sabrosas.", "plates": [{"name": "Enchiladas Verdes", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salsa verde, relleno de pollo.", "pros": ["Muy sabrosas"], "cons": ["Muy picante"]}, {"name": "Salsa de Mole", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Sabor intenso, especiado.", "pros": ["Muy original"], "cons": []}]},
        "fiesta-mex": {"name": "Fiesta Mex", "location": "Chacarita, CABA", "rating": 4.6, "reviewCount": 9, "description": "Fiesta tematica y menu degustacion.", "pros": ["Menu degustacion", "Ambiente festivo"], "cons": ["Ruidoso"], "diary": "Fuimos en grupo y probamos el menu degustacion. Muy divertido.", "plates": [{"name": "Menu Degustacion", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye varios platos tipicos.", "pros": ["Ideal para grupos"], "cons": []}, {"name": "Margarita", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Bien preparada, refrescante.", "pros": ["Muy fresca"], "cons": []}]},
        "puebla-picante": {"name": "Puebla Picante", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 7, "description": "Platos picantes y postres.", "pros": ["Platos picantes", "Postres ricos"], "cons": ["Muy picante"], "diary": "Probamos el chile relleno y el pastel de tres leches. Muy sabroso.", "plates": [{"name": "Chile Relleno", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Muy picante, relleno de queso.", "pros": ["Buen sabor"], "cons": ["Muy picante"]}, {"name": "Pastel de Tres Leches", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce, bien humedo.", "pros": ["Muy rico"], "cons": []}]},
    },
    "brunchMock": {
        "brunch-co": {"name": "Brunch & Co.", "location": "Recoleta, CABA", "rating": 4.6, "reviewCount": 10, "description": "El mejor brunch de la ciudad con opciones veganas.", "pros": ["Opciones veganas", "Ambiente moderno"], "cons": ["Puede estar lleno"], "diary": "Fuimos a Brunch & Co. un domingo soleado. Probamos el avocado toast y los pancakes. El ambiente es moderno y la atencion muy buena.", "plates": [{"name": "Avocado Toast", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pan artesanal, palta fresca y semillas.", "pros": ["Muy fresco", "Bien presentado"], "cons": []}, {"name": "Pancakes Veganos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Esponjosos y dulces, acompanados de frutas.", "pros": ["Opcion saludable"], "cons": []}]},
        "sunny-brunch": {"name": "Sunny Brunch", "location": "Palermo, CABA", "rating": 4.8, "reviewCount": 12, "description": "Ambiente luminoso y menu variado.", "pros": ["Ambiente luminoso", "Menu variado"], "cons": ["Puede haber espera"], "diary": "Visitamos Sunny Brunch en una manana de sabado. Probamos los huevos benedictinos y el jugo natural. Todo muy rico y fresco.", "plates": [{"name": "Huevos Benedictinos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Huevos en su punto, salsa holandesa suave.", "pros": ["Salsa deliciosa"], "cons": []}]},
        "bruncheria": {"name": "Bruncheria", "location": "Belgrano, CABA", "rating": 4.7, "reviewCount": 15, "description": "Brunchs clasicos y modernos.", "pros": ["Clasico y moderno", "Opciones variadas"], "cons": ["Precios algo altos"], "diary": "Probamos el bagel de salmon y la limonada. El local es muy lindo y la atencion excelente.", "plates": [{"name": "Bagel de Salmon", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Salmon ahumado, queso crema y alcaparras.", "pros": ["Salmon fresco"], "cons": []}]},
        "eggs-more": {"name": "Eggs & More", "location": "Caballito, CABA", "rating": 4.5, "reviewCount": 8, "description": "Especialidad en huevos y pancakes.", "pros": ["Especialidad en huevos", "Pancakes ricos"], "cons": ["Pocas mesas"], "diary": "Desayunamos huevos revueltos y pancakes. Todo salio rapido y estaba muy bien preparado.", "plates": [{"name": "Huevos Revueltos", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Huevos suaves, bien condimentados.", "pros": ["Bien cocidos"], "cons": []}]},
        "brunch-express": {"name": "Brunch Express", "location": "San Telmo, CABA", "rating": 4.3, "reviewCount": 6, "description": "Rapido y delicioso.", "pros": ["Servicio rapido", "Opciones para llevar"], "cons": ["Pocas opciones saludables"], "diary": "Ideal para un brunch rapido. Probamos el sandwich de jamon y queso y el cafe.", "plates": [{"name": "Sandwich de Jamon y Queso", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Pan fresco, buen relleno.", "pros": ["Ideal para llevar"], "cons": []}]},
        "morning-glory": {"name": "Morning Glory", "location": "Almagro, CABA", "rating": 4.4, "reviewCount": 9, "description": "Opciones saludables y jugos naturales.", "pros": ["Opciones saludables", "Jugos naturales"], "cons": ["Precios altos"], "diary": "Probamos el bowl de frutas y el jugo detox. Todo muy fresco y bien presentado.", "plates": [{"name": "Bowl de Frutas", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Frutas frescas, yogur y granola.", "pros": ["Muy fresco"], "cons": []}]},
        "brunch-house": {"name": "Brunch House", "location": "Flores, CABA", "rating": 4.6, "reviewCount": 11, "description": "Ambiente familiar y menu kids.", "pros": ["Ambiente familiar", "Menu para ninos"], "cons": ["Puede estar lleno"], "diary": "Fuimos en familia y probamos el menu kids y el brunch clasico. Muy buena atencion.", "plates": [{"name": "Brunch Clasico", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Huevos, panceta, pan y jugo.", "pros": ["Porcion generosa"], "cons": []}]},
        "brunch-time": {"name": "Brunch Time", "location": "Villa Crespo, CABA", "rating": 4.2, "reviewCount": 7, "description": "Brunch todo el dia.", "pros": ["Brunch todo el dia", "Opciones variadas"], "cons": ["Cierra temprano"], "diary": "Probamos la tostada francesa y el jugo de naranja. Muy buen brunch.", "plates": [{"name": "Tostada Francesa", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Dulce, bien dorada.", "pros": ["Muy rica"], "cons": []}]},
        "brunch-friends": {"name": "Brunch & Friends", "location": "Chacarita, CABA", "rating": 4.7, "reviewCount": 13, "description": "Ideal para grupos grandes.", "pros": ["Ideal para grupos", "Mesas grandes"], "cons": ["Puede ser ruidoso"], "diary": "Fuimos en grupo y probamos el brunch para compartir. Todo salio rapido y estaba muy rico.", "plates": [{"name": "Brunch para Compartir", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye variedad de platos dulces y salados.", "pros": ["Ideal para compartir"], "cons": []}]},
        "brunch-final": {"name": "Brunch Final", "location": "Retiro, CABA", "rating": 4.5, "reviewCount": 10, "description": "El brunch perfecto para cerrar la semana.", "pros": ["Cierre de semana", "Opciones variadas"], "cons": ["Puede estar lleno"], "diary": "Probamos el brunch final y el cafe especial. Muy buen cierre de semana.", "plates": [{"name": "Brunch Final", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Incluye huevos, pan, frutas y cafe.", "pros": ["Muy completo"], "cons": []}]},
        "tarta-co": {"name": "Tarta & Co.", "location": "Caballito, CABA", "rating": 4.6, "reviewCount": 10, "description": "Tartas dulces y saladas.", "pros": ["Variedad de tartas", "Precios accesibles"], "cons": ["Pocas mesas"], "diary": "Un lugar sencillo pero con tartas muy ricas. Probamos la de ricota y la de frutilla. Ideal para una merienda rapida.", "plates": [{"name": "Tarta de Ricota", "date": "2024-06-10", "image": "/img/food-fallback.jpg", "note": "Ricota suave, masa fina.", "pros": ["Muy buena relacion precio-calidad"], "cons": []}]},
    },
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def login_session(email: str, password: str) -> requests.Session:
    """Login and return a session with auth cookies set."""
    session = requests.Session()
    resp = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
    )
    resp.raise_for_status()
    return session


def get_categories(session: requests.Session) -> dict[str, int]:
    """Return a mapping of category slug -> category id."""
    resp = session.get(f"{BASE_URL}/api/categories")
    resp.raise_for_status()
    return {cat["slug"]: cat["id"] for cat in resp.json()}


def create_restaurant(
    session: requests.Session,
    slug: str,
    name: str,
    description: str,
    location_name: str,
    category_id: int,
    latitude=None,
    longitude=None,
    cover_image_url=None,
):
    """Create a restaurant via the API. Returns the response JSON or None on conflict."""
    payload = {
        "slug": slug,
        "name": name,
        "description": description,
        "location_name": location_name,
        "category_id": category_id,
        "cover_image_url": cover_image_url,
    }
    if latitude is not None:
        payload["latitude"] = latitude
    if longitude is not None:
        payload["longitude"] = longitude

    resp = session.post(
        f"{BASE_URL}/api/restaurants",
        json=payload,
    )
    if resp.status_code == 201:
        return resp.json()
    elif resp.status_code == 409:
        print(f"    [SKIP] Restaurant '{name}' already exists")
        return None
    else:
        print(f"    [ERROR] Creating restaurant '{name}': {resp.status_code} {resp.text}")
        return None


def create_dish(
    session: requests.Session,
    restaurant_slug: str,
    restaurant_id: str,
    dish_name: str,
    cover_image_url=None,
):
    """Create a dish. Returns the response JSON or None."""
    payload = {
        "restaurant_id": restaurant_id,
        "name": dish_name,
        "cover_image_url": cover_image_url,
    }
    resp = session.post(
        f"{BASE_URL}/api/restaurants/{restaurant_slug}/dishes",
        json=payload,
    )
    if resp.status_code == 201:
        return resp.json()
    else:
        print(f"      [ERROR] Creating dish '{dish_name}': {resp.status_code} {resp.text}")
        return None


def create_dish_review(session: requests.Session, dish_id: str, plate_data: dict):
    """Create a dish review with pros/cons."""
    pros_cons = []
    for pro in plate_data.get("pros", []):
        pros_cons.append({"type": "pro", "text": pro})
    for con in plate_data.get("cons", []):
        pros_cons.append({"type": "con", "text": con})

    # Parse the date
    date_str = plate_data.get("date", "2024-06-10")

    payload = {
        "dish_id": dish_id,
        "date_tasted": date_str,
        "note": plate_data.get("note", ""),
        "rating": plate_data.get("rating", 5),
        "pros_cons": pros_cons,
        "tags": [],
        "images": [],
    }

    resp = session.post(
        f"{BASE_URL}/api/dishes/{dish_id}/reviews",
        json=payload,
    )
    if resp.status_code == 201:
        return resp.json()
    elif resp.status_code == 409:
        print(f"        [SKIP] Review for dish already exists")
        return None
    else:
        print(f"        [ERROR] Creating review: {resp.status_code} {resp.text}")
        return None


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Mock Data Migration Script")
    print("=" * 60)

    # Step 1: Login
    print("\n[1] Logging in as admin...")
    try:
        session = login_session("admin@criticomida.com", "admin123")
        print("    Logged in successfully.")
    except Exception as e:
        print(f"    FAILED to login: {e}")
        sys.exit(1)

    # Step 2: Get categories
    print("\n[2] Fetching categories...")
    try:
        categories = get_categories(session)
        print(f"    Found {len(categories)} categories: {list(categories.keys())}")
    except Exception as e:
        print(f"    FAILED to fetch categories: {e}")
        sys.exit(1)

    # Step 3: Iterate over all mock data
    total_restaurants = 0
    total_dishes = 0
    total_reviews = 0
    skipped = 0

    for mock_name, restaurants in MOCK_DATA.items():
        category_slug = CATEGORY_SLUG_MAP.get(mock_name)
        if not category_slug:
            print(f"\n[WARN] No category mapping for '{mock_name}', skipping.")
            continue

        category_id = categories.get(category_slug)
        if category_id is None:
            print(f"\n[WARN] Category '{category_slug}' not found in DB, skipping.")
            continue

        print(f"\n{'=' * 40}")
        print(f"Category: {mock_name} -> {category_slug} (id={category_id})")
        print(f"{'=' * 40}")

        for rest_slug, rest_data in restaurants.items():
            name = rest_data["name"]
            location = rest_data["location"]
            description = rest_data.get("description", "")

            # Check for coordinates
            lat = None
            lng = None
            for coord_name, coords in COORDINATES.items():
                if coord_name.lower().replace("'", "") in name.lower().replace("'", ""):
                    lat = coords["lat"]
                    lng = coords["lng"]
                    break

            print(f"\n  Restaurant: {name} ({rest_slug})")

            restaurant_resp = create_restaurant(
                session=session,
                slug=rest_slug,
                name=name,
                description=description,
                location_name=location,
                category_id=category_id,
                latitude=lat,
                longitude=lng,
                cover_image_url="/img/food-fallback.jpg",
            )

            if restaurant_resp is None:
                skipped += 1
                continue

            total_restaurants += 1
            restaurant_id = restaurant_resp["id"]
            restaurant_slug_actual = restaurant_resp["slug"]

            # Create dishes and reviews
            for plate in rest_data.get("plates", []):
                dish_resp = create_dish(
                    session=session,
                    restaurant_slug=restaurant_slug_actual,
                    restaurant_id=restaurant_id,
                    dish_name=plate["name"],
                    cover_image_url=plate.get("image", "/img/food-fallback.jpg"),
                )
                if dish_resp is None:
                    continue
                total_dishes += 1
                dish_id = dish_resp["id"]

                # Create review for the dish
                review_resp = create_dish_review(
                    session=session,
                    dish_id=dish_id,
                    plate_data=plate,
                )
                if review_resp is not None:
                    total_reviews += 1

    # Summary
    print("\n" + "=" * 60)
    print("MIGRATION COMPLETE")
    print("=" * 60)
    print(f"  Restaurants created: {total_restaurants}")
    print(f"  Dishes created:      {total_dishes}")
    print(f"  Reviews created:     {total_reviews}")
    print(f"  Skipped (existing):  {skipped}")
    print("=" * 60)


if __name__ == "__main__":
    main()
