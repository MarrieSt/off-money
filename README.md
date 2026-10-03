# Money

Money is a standalone Django application for personal finance history and, in future phases, day-by-day spending analytics. It is independent of Culture and has its own PostgreSQL database, authentication model, migrations, and deployment configuration. Phase 1 provides the data foundation, Admin, and an authenticated server-rendered application shell. It does not import transactions or calculate analytics.

## Requirements

- Python 3.12+
- PostgreSQL 14+
- `pip`

The project uses Django 5.2, PostgreSQL, `psycopg`, WhiteNoise, and Gunicorn. No Google credentials or integration are required to run it.

## Local setup

Create a PostgreSQL database and local environment file:

```bash
createdb money
cp .env.example .env
```

Edit `.env` if your local PostgreSQL username, password, host, or port differs. Then create the virtual environment and start the application:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. The superuser can manage users and seed domain records at `/admin/`. There is no public signup flow.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Django secret; use a strong generated secret outside local development. |
| `DEBUG` | Set `False` in production. |
| `DATABASE_URL` | PostgreSQL connection URL. Local default is `postgresql://postgres:postgres@localhost:5432/money`. |
| `ALLOWED_HOSTS` | Comma-separated hostnames accepted by Django. |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated trusted origins, including `https://money.sokoloff.co.uk` in production. |

`DATABASE_URL` should be the Neon PostgreSQL URL in Railway. SSL is required automatically when `DEBUG=False`. Do not commit `.env` or production credentials.

## Useful commands

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py test
python manage.py collectstatic --noinput
```

The default database is PostgreSQL, including for tests. Start the local PostgreSQL service before running tests.

## Railway and Neon

1. Create a Neon project/database and copy its PostgreSQL connection URL (with SSL support).
2. Create a Railway service from this repository and set `DATABASE_URL`, `SECRET_KEY`, `DEBUG=False`, and `ALLOWED_HOSTS` to include the Railway hostname and `money.sokoloff.co.uk`.
3. Set `CSRF_TRUSTED_ORIGINS=https://money.sokoloff.co.uk` (plus the Railway HTTPS origin if using it directly).
4. Railway builds dependencies and collects static files using `railway.json`; Gunicorn serves `money.wsgi:application` and `/health/` is the health check.
5. Run `python manage.py migrate` as a Railway one-off command before opening the service. Create the initial staff user with `python manage.py createsuperuser` as a one-off command.
6. Attach `money.sokoloff.co.uk` to the Railway service and configure the DNS record Railway provides.

The health endpoint returns `{"status":"ok","app":"money"}` and intentionally does not require a database query.

## Data model and future phases

The first migration includes the independent `MoneyUser` model. Finance records are user-owned. `FinancialAccount` and `Transaction` carry a `source_system` identifier (initially `emma`) so external identities are scoped by user and source; transaction IDs may be absent for manually seeded records. Transaction amounts use decimal precision, and source fields plus `raw_data` preserve imported values. Spend rules, daily summaries, and reward records are defined but no evaluator, aggregation, streak calculation, or reward job runs in Phase 1.

Future Emma ingestion is intended to use a read-only Google service account shared on the Emma spreadsheet, an import service/management command, source-scoped upserts, and explicit import timestamps. It will update matching rows without deleting local history when source rows disappear. Google credentials and scheduling belong to that later phase; neither is needed for this application to boot.
