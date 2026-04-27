# CritiComida — Backend

FastAPI + Postgres. Submódulo del monorepo del front [criticomida-nextjs](https://github.com/lagomoura/criticomida_production).

## Quickstart

```bash
cp .env.example .env                      # ajustar secretos si rotaste keys
docker compose up -d db                   # Postgres en :5433 (host)
./scripts/restore_dev_db.sh               # carga snapshot prod → dev
docker compose up api                     # alembic upgrade head + uvicorn :8002
```

API queda en `http://localhost:8002`. Docs OpenAPI en `/docs`.

Si es la primera vez y no tenés snapshot, mirá [docs/ENVIRONMENTS.md → Snapshot](../docs/ENVIRONMENTS.md#snapshot-prod--dev).

## Modos locales

- **Full-docker (default)**: api + db en compose. `DATABASE_URL=...@db:5432/...`.
- **Hybrid**: db en compose, uvicorn en host. `DATABASE_URL=...@localhost:5433/...`.

`docker-compose.yml` mapea host `:8002 → container :8000`. El `:8000` del host queda libre para otros proyectos.

## Migraciones

Alembic. Corren automáticamente en el `entrypoint.sh` antes de uvicorn (igual en dev y prod).

```bash
# crear una migración nueva (autogenerate contra DB con versión anterior aplicada)
alembic revision --autogenerate -m "agregar columna foo"

# aplicar manualmente sin reiniciar el contenedor
docker compose exec api alembic upgrade head

# rollback
docker compose exec api alembic downgrade -1
```

## Tests

```bash
docker compose exec api pytest                 # corre todo
docker compose exec api pytest tests/test_x    # un archivo
# o desde el host con la DB en compose:
pytest
```

## Scripts útiles (`scripts/`)

| Script                         | Para qué                                                       |
|--------------------------------|----------------------------------------------------------------|
| `restore_dev_db.sh`            | Carga `seeds/dev_baseline.dump` en la DB local.                |
| `migrate_mock_data.py`         | Importa data de los mocks del front a la DB (legacy seed).     |
| `import_dishes.py`             | Importa platos de un CSV/JSON.                                 |
| `import_google_maps.py`        | Importa reviews de un Takeout de Google Maps.                  |
| `backfill_dish_reviews.py`     | Recalcula agregados de reseñas.                                |
| `backfill_google_places.py`    | Enriquece restaurants con datos de Google Places.              |
| `assign_restaurant_categories.py` | Asigna categoría a restaurants sin categoría.               |

`scripts/seeds/` contiene los dumps de prod (gitignored).

## Deploy (Railway)

- Trigger: push a `main` de este repo.
- Image: el `Dockerfile` de la raíz.
- Entrypoint: `./entrypoint.sh` corre `alembic upgrade head` antes de uvicorn. Si falla, el contenedor no arranca.
- Variables: setadas en el dashboard de Railway, ver [docs/ENVIRONMENTS.md](../docs/ENVIRONMENTS.md#variables-1) en el monorepo del front.
- `DATABASE_URL` la inyecta automáticamente el servicio Postgres de Railway.

## Más detalle

- **[../docs/ENVIRONMENTS.md](../docs/ENVIRONMENTS.md)** — referencia completa: variables, credenciales, snapshot, deploy, troubleshooting.
- **[../CLAUDE.md](../CLAUDE.md)** — contexto del proyecto y arquitectura.
