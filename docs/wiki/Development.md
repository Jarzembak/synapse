# Development

## Backend tests

The backend test suite runs without Docker:

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests -q
```

## Frontend checks

```bash
cd frontend
npm ci
npm run typecheck
npm run build
```

`npm run typecheck` performs TypeScript validation without creating output.
`npm run build` validates types and produces the production bundle.

## Hot-reload development

Start backend services with the development overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up api redis ollama worker beat
```

Then start Vite in another terminal:

```bash
cd frontend
npm ci
npm run dev
```

The frontend development server proxies `/api` to `localhost:8000`. It also
adds the same trusted-origin assertion as production nginx. The canonical
development origin defaults to `http://localhost:5173`; set
`SYNAPSE_DEV_PUBLIC_ORIGIN` only when Vite is opened at a different origin.
The development Compose overlay configures the API with the matching value.

## Rebuild application containers

After backend or frontend source changes:

```bash
docker compose up --build
```

Compose recreates only services whose image or configuration changed.

## Dependency locks

Backend requirement inputs remain portable for local development, while
container and CI installation use generated Linux constraints. After editing
`requirements.txt` or `requirements-dev.txt`, run from `backend/`:

```bash
python -m pip install pip-tools==7.5.3
pip-compile --allow-unsafe --no-emit-index-url --no-emit-trusted-host --strip-extras --output-file=constraints.txt requirements.txt
pip-compile --allow-unsafe --no-emit-index-url --no-emit-trusted-host --strip-extras --output-file=constraints-dev.txt requirements-dev.txt
```

Commit both the input requirements and generated constraints, then rerun the
backend tests.

## Compose validation

CI validates the default, development, and GPU Compose configurations. To
inspect a configuration locally:

```bash
docker compose config
docker compose -f docker-compose.yml -f docker-compose.dev.yml config
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config
```

## Persistent data

`docker compose down -v` removes named Docker volumes such as model and Redis
caches. It does not remove bind-mounted application state under `./data`.

Deleting `./data` removes the library, database, media, logs, backups, and
credentials stored there. Make and verify a backup before intentionally
starting with an empty installation.

## Logs and diagnostics

Use the in-app Logs and System pages first. CLI alternatives include:

```bash
docker compose ps
docker compose logs --tail 200 api
docker compose logs --tail 200 worker
docker compose logs --tail 200 paper-worker
```

See
[Operations and Troubleshooting](https://github.com/Jarzembak/synapse/wiki/Operations-and-Troubleshooting).

## Continuous integration

Pull requests run:

- backend pytest;
- frontend TypeScript validation and production build; and
- default, development, and GPU Compose validation.
