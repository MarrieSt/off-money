# Money

Money is a standalone Django application for personal finance history and, in future phases, day-by-day spending analytics. It is independent of Culture and has its own PostgreSQL database, authentication model, migrations, and deployment configuration. Phase 1 provides the data foundation and authenticated application shell. Phase 2 adds Emma Google Sheets ingestion, raw-row traceability, import history, and operational tooling. Analytics calculations remain out of scope.

## Requirements

- Python 3.12+
- PostgreSQL 14+
- `pip`

The project uses Django 5.2, PostgreSQL, `psycopg`, WhiteNoise, Gunicorn, `google-auth`, and `gspread`. Google credentials are only required to run an import, not to start the application.

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
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | The service-account JSON document, stored as a secret environment variable. |
| `EMMA_SPREADSHEET_ID` | ID from the Emma-export Google Sheets URL. |
| `EMMA_WORKSHEET_NAME` | Worksheet tab name, normally `Primary`. |
| `MONEY_DEFAULT_CURRENCY` | Fallback currency for rows without a currency value; defaults to `GBP`. |
| `MONEY_IMPORT_USERNAME` | Existing Money username that owns imported records. |

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

The first migration includes the independent `MoneyUser` model. Finance records are user-owned. `FinancialAccount` and `Transaction` carry a `source_system` identifier so external identities are scoped by user and source; transaction IDs may be absent for manually seeded records. Transaction amounts use decimal precision, and source fields plus `raw_data` preserve imported values. Spend rules, daily summaries, and reward records are defined, but no evaluator, aggregation, streak calculation, or reward job runs yet.

## Emma Google Sheets import

1. Enable the Google Sheets API in a Google Cloud project and create a service account.
2. Share the Emma spreadsheet with the service-account email as a Viewer. The importer requests only `https://www.googleapis.com/auth/spreadsheets.readonly` and never writes to Sheets.
3. Set `GOOGLE_SHEETS_CREDENTIALS_JSON`, `EMMA_SPREADSHEET_ID`, `EMMA_WORKSHEET_NAME=Primary`, and `MONEY_IMPORT_USERNAME` in Railway. Store the complete service-account JSON in the Railway variable; do not commit a credential file.
4. The importer automatically creates any missing `FinancialAccount` from each row's Account and Bank values for `MONEY_IMPORT_USERNAME`. New account currency defaults to `MONEY_DEFAULT_CURRENCY`; each canonical transaction stores the row's own Currency value. No manual account or bank setup is required.
5. Run `python manage.py import_emma`, or sign in as staff and use **Data → Emma imports → Run Emma import**. On Railway's Nixpacks shell, use `/opt/venv/bin/python manage.py import_emma` if the shell's default `python` does not resolve to the app environment.

Each non-empty sheet row is kept as the latest `EmmaRawTransaction` snapshot, keyed by user, source, and Emma ID. Emma dates use the US `MM/DD/YYYY` convention (for example, `9/1/2022` is September 1); ISO dates are also accepted. A normalized SHA256 detects source changes; the Emma ID remains the canonical transaction identity. Repeated unchanged rows are skipped, changed rows update the existing `Transaction`, and row-level failures do not stop other rows. A run is marked partial if any rows fail. Import history and raw rows are available in Admin. No scheduled Railway job is configured in this phase.
