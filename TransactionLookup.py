#!/usr/bin/env python3
"""
TransactionLookup
1) Reads SHOP_LOGIN + BANK_TRX_ID rows from transactions.txt
2) Looks them up in Oracle
3) Calls Fabrick Orchestra GET /merchants/{merchantId}/payments
4) Writes one consolidated CSV report

Uses only the Python standard library plus python-oracledb.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    import oracledb
except ImportError:
    print("Missing dependency: python-oracledb")
    print("Install it in this Python environment with: python -m pip install oracledb")
    sys.exit(2)


# ---------------------------------------------------------
# ORACLE CLIENT INITIALIZATION
# ---------------------------------------------------------

oracledb.init_oracle_client(
    lib_dir=r"C:\Oracle\Client19\x64\product\19.0.0\client_1\bin",
    config_dir=r"C:\Users\GBS13937\OneDrive - Sella\Documents\MOTObatch\TransactionLookup"
)


# ---------------------------------------------------------
# PATHS AND CONFIGURATION
# ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent

CONFIG_PATH = ROOT / "config.json"
INPUT_PATH = ROOT / "transactions.txt"
RESULTS_DIR = ROOT / "Results"
LOG_PATH = RESULTS_DIR / "errors.log"


# ---------------------------------------------------------
# CSV OUTPUT COLUMNS
# ---------------------------------------------------------

OUTPUT_FIELDS = [
    "INPUT_ROW",
    "SHOP_LOGIN",
    "BANK_TRX_ID",
    "TIPO_OP",
    "CLIENT_TRANSACTION_ID",
    "MERCHANT_ID",
    "ORACLE_STATUS",
    "HTTP_STATUS",
    "API_STATUS",
    "TRACE_ID",
    "TRANSACTION_ID",
    "TRANSACTION_STATUS",
    "API_CLIENT_TRANSACTION_ID",
    "PAYMENT_METHOD",
    "AMOUNT",
    "CURRENCY",
    "MERCHANT_NAME",
    "ACQUIRER_NAME",
    "OPERATION_STATUS",
    "OPERATION_RESULT",
    "OPERATION_ID",
    "RECONCILIATION_KEY",
    "API_ERRORS",
    "ERROR_MESSAGE",
    "RAW_RESPONSE"
]


# ---------------------------------------------------------
# INPUT HEADER ALIASES
# ---------------------------------------------------------

HEADER_ALIASES = {
    "shoplogin": "SHOP_LOGIN",
    "shopid": "SHOP_LOGIN",
    "banktrxid": "BANK_TRX_ID",
    "banktransactionid": "BANK_TRX_ID",
    "banktransaction": "BANK_TRX_ID",
    "tipop": "TIPO_OP",
    "tipoop": "TIPO_OP",
}


# ---------------------------------------------------------
# PROGRESS DISPLAY
# ---------------------------------------------------------

def progress(message: str) -> None:
    """Print a timestamped progress message immediately."""

    timestamp = datetime.now().strftime("%H:%M:%S")

    print(
        f"[{timestamp}] {message}",
        flush=True
    )


# ---------------------------------------------------------
# INPUT UTILITIES
# ---------------------------------------------------------

def normalize_header(value: str) -> str:

    return re.sub(
        r"[^a-z0-9]",
        "",
        (value or "").strip().lower()
    )


def load_config() -> dict[str, Any]:

    if not CONFIG_PATH.exists():

        raise FileNotFoundError(
            f"Configuration file not found: {CONFIG_PATH}\n"
            "Copy config.example.json to config.json and fill in your connection details."
        )

    with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:

        return json.load(f)



def read_input(path: Path) -> list[dict[str, str]]:

    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    content = path.read_text(encoding="utf-8-sig")

    if not content.strip():
        raise ValueError("transactions.txt is empty.")

    lines = [
        line for line in content.splitlines()
        if line.strip()
    ]

    # Split on one or more consecutive tab characters
    headers = re.split(r"\t+", lines[0].strip())

    mapped = {}

    for index, header in enumerate(headers):
        canonical = HEADER_ALIASES.get(normalize_header(header))

        if canonical:
            mapped[canonical] = index

    if "SHOP_LOGIN" not in mapped or "BANK_TRX_ID" not in mapped:
        raise ValueError(
            "Input must contain SHOP LOGIN and BANK TRX ID headers."
        )

    rows = []

    for row_number, line in enumerate(lines[1:], start=2):

        values = re.split(r"\t+", line.strip())

        shop = (
            values[mapped["SHOP_LOGIN"]].strip()
            if len(values) > mapped["SHOP_LOGIN"]
            else ""
        )

        bank = (
            values[mapped["BANK_TRX_ID"]].strip()
            if len(values) > mapped["BANK_TRX_ID"]
            else ""
        )

        tipo = (
            values[mapped["TIPO_OP"]].strip()
            if "TIPO_OP" in mapped and len(values) > mapped["TIPO_OP"]
            else ""
        )

        if not shop and not bank:
            continue

        if not shop or not bank:
            logging.warning(
                "Input row %s skipped: missing SHOP_LOGIN or BANK_TRX_ID.",
                row_number
            )
            continue

        rows.append({
            "INPUT_ROW": str(row_number),
            "SHOP_LOGIN": shop,
            "BANK_TRX_ID": bank,
            "TIPO_OP": tipo,
        })

    if not rows:
        raise ValueError("No valid transactions found.")

    return rows


# ---------------------------------------------------------
# ORACLE LOOKUP
# ---------------------------------------------------------

def oracle_lookup(
    input_rows: list[dict[str, str]],
    cfg: dict[str, Any]
) -> list[dict[str, Any]]:

    oracle_cfg = cfg["oracle"]

    tns_admin = oracle_cfg.get("tns_admin")

    if tns_admin:

        # Resolve relative TNS_ADMIN paths from this project folder.

        tns_path = Path(
            tns_admin
        ).expanduser()

        if not tns_path.is_absolute():

            tns_path = ROOT / tns_path

        oracledb.defaults.config_dir = str(
            tns_path.resolve()
        )

    dsn = (
        oracle_cfg.get("tns_alias")
        or oracle_cfg.get("dsn")
    )

    if not dsn:

        raise ValueError(
            "Set oracle.tns_alias or oracle.dsn in config.json."
        )

    results: list[dict[str, Any]] = []

    batch_size = int(
        oracle_cfg.get("query_batch_size", 400)
    )

    sql_prefix = """
        WITH input_transactions AS (
    """

    sql_suffix = """
        )
        SELECT
            i.shop_login,
            i.bank_transaction_id,
            rept.SHOPTRANSACTIONID || '_' || rept.BANKTRANSACTIONID
                AS client_transaction_id,
            AM.merchantidentifier AS merchant_id
        FROM input_transactions i
        LEFT JOIN ANAG_SHOP s
            ON s.SHOP_LOGIN = i.shop_login
        LEFT JOIN REP_TRANSACTION rept
            ON rept.IDSHOP = s.IDSHOP
                AND rept.BANKTRANSACTIONID = i.bank_transaction_id
            AND rept.BANKTRANSACTIONID = i.bank_transaction_id
        LEFT JOIN ECOMM_DBA.GPY_ACCEPTANCE_MERCHANTS AM
            ON rept.IDSHOP = AM.IDSHOP
    """

    # -----------------------------------------------------
    # CONNECT TO ORACLE
    # -----------------------------------------------------

    progress(
        f"Oracle: connecting to {dsn}..."
    )

    connect_start = time.perf_counter()

    connection = oracledb.connect(
        user=oracle_cfg["username"],
        password=oracle_cfg["password"],
        dsn=dsn,
    )

    progress(
        f"Oracle: connected in "
        f"{time.perf_counter() - connect_start:.2f}s."
    )

    try:

        with connection.cursor() as cursor:

            total_batches = (
                len(input_rows) + batch_size - 1
            ) // batch_size

            for batch_number, start in enumerate(
                range(0, len(input_rows), batch_size),
                start=1
            ):

                batch = input_rows[
                    start:start + batch_size
                ]

                progress(
                    f"Oracle: preparing batch "
                    f"{batch_number}/{total_batches} "
                    f"({len(batch)} input rows)..."
                )

                selects = []

                binds: dict[str, str] = {}

                for idx, row in enumerate(batch):

                    shop_key = f"shop_{idx}"

                    bank_key = f"bank_{idx}"

                    selects.append(
                        f"SELECT :{shop_key} AS shop_login, "
                        f":{bank_key} AS bank_transaction_id FROM dual"
                    )

                    binds[shop_key] = row["SHOP_LOGIN"]

                    binds[bank_key] = row["BANK_TRX_ID"]

                sql = (
                    sql_prefix
                    + "\nUNION ALL\n".join(selects)
                    + sql_suffix
                )

                # -------------------------------------------------
                # EXECUTE ORACLE QUERY
                # -------------------------------------------------

                query_start = time.perf_counter()

                progress(
                    f"Oracle: executing batch "
                    f"{batch_number}/{total_batches}..."
                )

                cursor.execute(
                    sql,
                    binds
                )

                query_elapsed = (
                    time.perf_counter() - query_start
                )

                progress(
                    f"Oracle: batch {batch_number}/{total_batches} "
                    f"query completed in {query_elapsed:.2f}s; "
                    f"fetching rows..."
                )

                columns = [
                    d[0].lower()
                    for d in cursor.description
                ]

                # -------------------------------------------------
                # FETCH RESULTS
                # -------------------------------------------------

                fetch_start = time.perf_counter()

                records = cursor.fetchall()

                fetch_elapsed = (
                    time.perf_counter() - fetch_start
                )

                for record in records:

                    results.append(
                        dict(zip(columns, record))
                    )

                progress(
                    f"Oracle: batch {batch_number}/{total_batches} "
                    f"fetched {len(records)} rows "
                    f"in {fetch_elapsed:.2f}s."
                )

    finally:

        connection.close()

        progress(
            "Oracle: connection closed."
        )

    return results


# ---------------------------------------------------------
# API RESPONSE UTILITIES
# ---------------------------------------------------------

def first_item(value: Any) -> dict[str, Any]:

    if (
        isinstance(value, list)
        and value
        and isinstance(value[0], dict)
    ):

        return value[0]

    return {}


def compact_json(value: Any) -> str:

    if value is None:

        return ""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":")
    )


# ---------------------------------------------------------
# FABRICK ORCHESTRA API CALL
# ---------------------------------------------------------

def call_orchestra(
    merchant_id: str,
    client_transaction_id: str,
    cfg: dict[str, Any]
) -> dict[str, Any]:

    api_cfg = cfg["orchestra"]

    base_url = api_cfg["base_url"].rstrip("/")

    url = (
        f"{base_url}/{quote(str(merchant_id), safe='')}/payments?"
        + urlencode({
            "clientTransactionId": str(client_transaction_id)
        })
    )

    headers = {

        api_cfg.get("auth_header_name", "Auth-Schema"):
            api_cfg.get("auth_header_value", "S2S"),

        api_cfg.get("api_key_header_name", "Api-Key"):
            api_cfg["api_key"],

        "Content-Type": api_cfg.get(
            "content_type",
            "application/json;charset=UTF8"
        ),

        "Accept": "application/json",
    }

    timeout = int(
        api_cfg.get("timeout_seconds", 30)
    )

    retries = int(
        api_cfg.get("max_retries", 2)
    )

    delay = float(
        api_cfg.get("delay_between_requests_seconds", 0.2)
    )

    last_error = ""

    # -----------------------------------------------------
    # REQUEST / RETRY LOOP
    # -----------------------------------------------------

    for attempt in range(retries + 1):

        request = Request(
            url,
            headers=headers,
            method="GET"
        )

        progress(
            f"Orchestra: request attempt "
            f"{attempt + 1}/{retries + 1} "
            f"(timeout {timeout}s) "
            f"for {client_transaction_id}..."
        )

        attempt_start = time.perf_counter()

        try:

            with urlopen(
                request,
                timeout=timeout
            ) as response:

                http_status = response.status

                raw_text = response.read().decode(
                    "utf-8",
                    errors="replace"
                )

            try:

                body = (
                    json.loads(raw_text)
                    if raw_text
                    else {}
                )

            except json.JSONDecodeError:

                body = {}

            payload = body.get("payload") or {}

            content = payload.get("content") or {}

            routes = first_item(
                content.get("paymentRoutes")
            )

            operations = first_item(
                content.get("operations")
            )

            provider_data = (
                content.get("providerData") or {}
            )

            if not provider_data:

                provider_data = (
                    operations.get("providerData") or {}
                )

            progress(
                f"Orchestra: HTTP {http_status} received in "
                f"{time.perf_counter() - attempt_start:.2f}s "
                f"for {client_transaction_id}."
            )

            return {

                "HTTP_STATUS": http_status,

                "API_STATUS": body.get(
                    "status",
                    ""
                ),

                "TRACE_ID": payload.get(
                    "traceId",
                    ""
                ),

                "TRANSACTION_ID": payload.get(
                    "transactionId",
                    ""
                ),

                "TRANSACTION_STATUS": payload.get(
                    "transactionStatus",
                    ""
                ),

                "API_CLIENT_TRANSACTION_ID": content.get(
                    "clientTransactionId",
                    ""
                ),

                "PAYMENT_METHOD": content.get(
                    "paymentMethod",
                    ""
                ),

                "AMOUNT": content.get(
                    "amount",
                    ""
                ),

                "CURRENCY": content.get(
                    "currency",
                    ""
                ),

                "MERCHANT_NAME": (
                    content.get("merchant") or {}
                ).get(
                    "displayName",
                    ""
                ),

                "ACQUIRER_NAME": routes.get(
                    "acquirerName",
                    ""
                ),

                "OPERATION_STATUS": operations.get(
                    "status",
                    ""
                ),

                "OPERATION_RESULT": operations.get(
                    "result",
                    ""
                ),

                "OPERATION_ID": operations.get(
                    "operationId",
                    ""
                ),

                "RECONCILIATION_KEY": provider_data.get(
                    "reconciliationKey",
                    ""
                ),

                "API_ERRORS": compact_json(
                    body.get("errors")
                ),

                "ERROR_MESSAGE": "",

                "RAW_RESPONSE": raw_text,
            }

        # -------------------------------------------------
        # HTTP ERRORS
        # -------------------------------------------------

        except HTTPError as exc:

            raw_text = exc.read().decode(
                "utf-8",
                errors="replace"
            )

            retryable = (
                exc.code == 429
                or 500 <= exc.code <= 599
            )

            last_error = (
                f"HTTP {exc.code}: {raw_text[:1000]}"
            )

            progress(
                f"Orchestra: HTTP {exc.code} after "
                f"{time.perf_counter() - attempt_start:.2f}s: "
                f"{last_error[:250]}"
            )

            if retryable and attempt < retries:

                wait_seconds = delay * (attempt + 1)

                progress(
                    f"Orchestra: retrying in "
                    f"{wait_seconds:.1f}s..."
                )

                time.sleep(wait_seconds)

                continue

            try:

                body = (
                    json.loads(raw_text)
                    if raw_text
                    else {}
                )

            except json.JSONDecodeError:

                body = {}

            return {

                "HTTP_STATUS": exc.code,

                "API_STATUS": body.get(
                    "status",
                    "HTTP_ERROR"
                ),

                "API_ERRORS": compact_json(
                    body.get("errors")
                ),

                "ERROR_MESSAGE": last_error,

                "RAW_RESPONSE": raw_text,
            }

        # -------------------------------------------------
        # CONNECTION / TIMEOUT ERRORS
        # -------------------------------------------------

        except (
            URLError,
            TimeoutError,
            OSError
        ) as exc:

            last_error = str(exc)

            progress(
                f"Orchestra: request failed after "
                f"{time.perf_counter() - attempt_start:.2f}s: "
                f"{last_error[:250]}"
            )

            if attempt < retries:

                wait_seconds = delay * (attempt + 1)

                progress(
                    f"Orchestra: retrying in "
                    f"{wait_seconds:.1f}s..."
                )

                time.sleep(wait_seconds)

                continue

            break

    return {

        "HTTP_STATUS": "",

        "API_STATUS": "REQUEST_FAILED",

        "API_ERRORS": "",

        "ERROR_MESSAGE": last_error,

        "RAW_RESPONSE": "",
    }


# ---------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------

def main() -> int:

    RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )

    try:

        # -------------------------------------------------
        # LOAD CONFIGURATION AND INPUT
        # -------------------------------------------------

        progress(
            "Starting TransactionLookup."
        )

        progress(
            "Loading configuration..."
        )

        cfg = load_config()

        progress(
            "Reading transactions.txt..."
        )

        input_rows = read_input(
            INPUT_PATH
        )

        progress(
            f"Loaded {len(input_rows)} input transactions."
        )

        # -------------------------------------------------
        # ORACLE LOOKUP
        # -------------------------------------------------

        progress(
            "Starting Oracle lookup..."
        )

        oracle_rows = oracle_lookup(
            input_rows,
            cfg
        )

        progress(
            f"Oracle returned {len(oracle_rows)} matching/input rows."
        )

        # -------------------------------------------------
        # INDEX ORACLE RESULTS
        # -------------------------------------------------

        # Keep every original input row, including rows with no Oracle match.

        lookup: dict[
            tuple[str, str],
            list[dict[str, Any]]
        ] = {}

        for result in oracle_rows:

            key = (

                str(
                    result.get("shop_login") or ""
                ).strip(),

                str(
                    result.get("bank_transaction_id") or ""
                ).strip()

            )

            lookup.setdefault(
                key,
                []
            ).append(result)

        # -------------------------------------------------
        # PROCESS TRANSACTIONS
        # -------------------------------------------------

        output_rows: list[dict[str, Any]] = []

        total = len(input_rows)

        for number, original in enumerate(
            input_rows,
            start=1
        ):

            key = (
                original["SHOP_LOGIN"],
                original["BANK_TRX_ID"]
            )

            matches = lookup.get(
                key,
                []
            )

            # -------------------------------------------------
            # NO ORACLE MATCH
            # -------------------------------------------------

            if not matches:

                output_rows.append({

                    **original,

                    "ORACLE_STATUS": "NO_MATCH",

                    "API_STATUS": "SKIPPED",

                    "ERROR_MESSAGE":
                        "No matching transaction found in Oracle.",
                })

                progress(
                    f"[{number}/{total}] "
                    f"{key[0]} / {key[1]} - no Oracle match."
                )

                continue

            # -------------------------------------------------
            # PROCESS MATCHES
            # -------------------------------------------------

            for match in matches:

                client_id = match.get(
                    "client_transaction_id"
                )

                merchant_id = match.get(
                    "merchant_id"
                )

                base = {

                    **original,

                    "CLIENT_TRANSACTION_ID":
                        client_id or "",

                    "MERCHANT_ID":
                        merchant_id or "",

                    "ORACLE_STATUS":
                        "FOUND",
                }

                # -------------------------------------------------
                # CHECK REQUIRED ORACLE VALUES
                # -------------------------------------------------

                if not client_id or not merchant_id:

                    missing = []

                    if not client_id:

                        missing.append(
                            "client transaction ID"
                        )

                    if not merchant_id:

                        missing.append(
                            "merchant ID"
                        )

                    output_rows.append({

                        **base,

                        "API_STATUS": "SKIPPED",

                        "ERROR_MESSAGE":
                            "Oracle result missing "
                            + " and ".join(missing)
                            + ".",
                    })

                    continue

                # -------------------------------------------------
                # ORCHESTRA API REQUEST
                # -------------------------------------------------

                progress(
                    f"[{number}/{total}] Orchestra started: "
                    f"clientTransactionId={client_id}, "
                    f"merchantId={merchant_id}."
                )

                api_start = time.perf_counter()

                api_result = call_orchestra(
                    str(merchant_id),
                    str(client_id),
                    cfg
                )

                api_elapsed = (
                    time.perf_counter() - api_start
                )

                progress(
                    f"[{number}/{total}] Orchestra finished "
                    f"in {api_elapsed:.2f}s | "
                    f"HTTP={api_result.get('HTTP_STATUS', '')} | "
                    f"API_STATUS={api_result.get('API_STATUS', '')}."
                )

                output_rows.append({

                    **base,

                    **api_result
                })

                # -------------------------------------------------
                # LOG ERRORS
                # -------------------------------------------------

                if api_result.get("ERROR_MESSAGE"):

                    logging.error(
                        "Input row %s, clientTransactionId=%s: %s",
                        original["INPUT_ROW"],
                        client_id,
                        api_result["ERROR_MESSAGE"]
                    )

                # -------------------------------------------------
                # DELAY BETWEEN REQUESTS
                # -------------------------------------------------

                time.sleep(
                    float(
                        cfg["orchestra"].get(
                            "delay_between_requests_seconds",
                            0.2
                        )
                    )
                )

        # -------------------------------------------------
        # EXPORT CSV
        # -------------------------------------------------

        progress(
            "All transactions processed. Writing CSV report..."
        )

        timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        output_path = (
            RESULTS_DIR
            / f"Results_{timestamp}.csv"
        )

        with output_path.open(
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=OUTPUT_FIELDS,
                extrasaction="ignore"
            )

            writer.writeheader()

            writer.writerows(
                output_rows
            )

        # -------------------------------------------------
        # FINISH
        # -------------------------------------------------

        progress(
            f"Finished. Report: {output_path}"
        )

        progress(
            f"Rows written: {len(output_rows)}"
        )

        progress(
            f"Log file: {LOG_PATH}"
        )

        return 0

    # -----------------------------------------------------
    # GLOBAL ERROR HANDLING
    # -----------------------------------------------------

    except Exception as exc:

        logging.exception(
            "Fatal error"
        )

        progress(
            f"ERROR: {exc}"
        )

        progress(
            f"See log: {LOG_PATH}"
        )

        return 1


# ---------------------------------------------------------
# SCRIPT ENTRY POINT
# ---------------------------------------------------------

if __name__ == "__main__":

    raise SystemExit(
        main()
    )