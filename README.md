# TransactionLookup

Automates:

1. Read `SHOP\\\_LOGIN` and `BANK\\\_TRX\\\_ID` from `transactions.txt`.
2. Query Oracle using the supplied lookup logic.
3. Call Fabrick Orchestra `GET /merchants/{merchantId}/payments?clientTransactionId=...`.
4. Export a consolidated CSV report in `Results`.

## Setup

1. Copy `config.example.json` to `config.json`.
2. Fill in your Oracle username/password and Orchestra API key. The supplied `tnsnames.ora` is already configured in `config.example.json` with alias `oraboh` and `tns\\\_admin` set to the project folder (`.`).
3. Copy `transactions.example.txt` to `transactions.txt` and replace the example rows.
4. Install `python-oracledb` into the portable Python environment: `python -m pip install oracledb`
5. Run: `python TransactionLookup.py`

## Oracle connection

This project uses `python-oracledb` Thin mode. It can use a TNS alias when `tns\\\_admin`
points to the directory containing `tnsnames.ora`. If your company Oracle configuration
requires Thick mode, Oracle Client libraries or a wallet, the connection configuration
will need a small adjustment.

## Input format

Tab-separated, comma-separated, or semicolon-separated files are supported. Required
headers are `SHOP LOGIN` and `BANK TRX ID`. `TIPO OP.` is optional and carried into output.

## Output

A timestamped CSV is created in `Results`. It includes Oracle lookup values, HTTP/API
status, transaction status, amount/currency, operation details, reconciliation key,
errors, and the raw API response. Rows without an Oracle match or required IDs are
kept and marked as skipped.

## Security

* Do not commit or share `config.json`.
* Keep API keys and Oracle credentials out of the input TXT and output CSV.
* The raw API response may contain personal or payment-related data. Store and share
result files according to your company's data-handling rules.
* Test with a small, approved set of transactions before running a large batch.
* \& ".\\python\\python.exe" ".\\TransactionLookup.py"

