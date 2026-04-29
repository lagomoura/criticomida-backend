"""Helpers para distancia geográfica con SQLAlchemy.

Implementación Haversine en SQL puro (sin PostGIS). Devuelve una expresión
SQLAlchemy que puede ir en SELECT, WHERE/HAVING u ORDER BY.

TODO(geo): cuando el universo de restaurantes pase ~50k filas, migrar a
`cube + earthdistance` con índice GIST.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Float, cast, func


EARTH_RADIUS_KM = 6371.0


def haversine_km_expr(
    lat_col: ColumnElement,
    lng_col: ColumnElement,
    *,
    lat: float,
    lng: float,
) -> ColumnElement:
    """Distancia Haversine en km entre las columnas (lat_col, lng_col) y (lat, lng)."""
    delta_lat = func.radians(cast(lat_col, Float) - lat) / 2.0
    delta_lng = func.radians(cast(lng_col, Float) - lng) / 2.0
    a = (
        func.power(func.sin(delta_lat), 2)
        + func.cos(func.radians(lat))
        * func.cos(func.radians(cast(lat_col, Float)))
        * func.power(func.sin(delta_lng), 2)
    )
    return EARTH_RADIUS_KM * 2.0 * func.asin(func.sqrt(a))
