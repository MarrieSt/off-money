import json
import logging
from collections import Counter

import gspread
from django.conf import settings


logger = logging.getLogger(__name__)
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class GoogleSheetsConfigurationError(ValueError):
    pass


def fetch_worksheet_rows():
    """Read the configured worksheet and return source rows with sheet row numbers."""
    missing = [
        name
        for name, value in (
            ("GOOGLE_SHEETS_CREDENTIALS_JSON", settings.GOOGLE_SHEETS_CREDENTIALS_JSON),
            ("EMMA_SPREADSHEET_ID", settings.EMMA_SPREADSHEET_ID),
            ("EMMA_WORKSHEET_NAME", settings.EMMA_WORKSHEET_NAME),
        )
        if not value
    ]
    if missing:
        raise GoogleSheetsConfigurationError(
            "Missing required import configuration: " + ", ".join(missing)
        )

    try:
        credentials_info = json.loads(settings.GOOGLE_SHEETS_CREDENTIALS_JSON)
    except json.JSONDecodeError as exc:
        raise GoogleSheetsConfigurationError(
            "GOOGLE_SHEETS_CREDENTIALS_JSON must contain valid JSON."
        ) from exc

    logger.info("Opening configured Emma spreadsheet")
    client = gspread.service_account_from_dict(
        credentials_info,
        scopes=[READ_ONLY_SCOPE],
    )
    spreadsheet = client.open_by_key(settings.EMMA_SPREADSHEET_ID)
    logger.info("Opening Emma worksheet %s", settings.EMMA_WORKSHEET_NAME)
    worksheet = spreadsheet.worksheet(settings.EMMA_WORKSHEET_NAME)
    values = worksheet.get_all_values()

    if not values:
        logger.info("Emma worksheet contains no rows")
        return []

    headers = [str(value).strip() for value in values[0]]
    if not headers or any(not header for header in headers):
        raise ValueError("Emma worksheet has a blank column header.")
    duplicate_headers = [header for header, count in Counter(h.casefold() for h in headers).items() if count > 1]
    if duplicate_headers:
        raise ValueError("Emma worksheet has duplicate column headers.")

    rows = []
    for source_row_number, values_row in enumerate(values[1:], start=2):
        if not any(str(value).strip() for value in values_row):
            continue
        if len(values_row) > len(headers):
            raise ValueError(f"Emma worksheet row {source_row_number} contains values without headers.")
        padded_values = list(values_row) + [""] * (len(headers) - len(values_row))
        rows.append(
            {
                "source_row_number": source_row_number,
                "raw_data": dict(zip(headers, padded_values)),
            }
        )

    logger.info("Fetched %s non-empty rows from Emma worksheet", len(rows))
    return rows
