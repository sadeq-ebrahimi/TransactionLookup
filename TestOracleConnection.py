
import json
from pathlib import Path
import oracledb


oracledb.init_oracle_client(
    lib_dir=r"C:\Oracle\Client19\x64\product\19.0.0\client_1\bin",
    config_dir=r"C:\Users\GBS13937\OneDrive - Sella\Documents\MOTObatch\TransactionLookup"
)

ROOT = Path(__file__).resolve().parent
config_path = ROOT / "config.json"

if not config_path.exists():
    raise SystemExit("config.json not found.")

config = json.loads(config_path.read_text(encoding="utf-8"))
oracle = config["oracle"]

tns_admin = Path(oracle.get("tns_admin", "."))
if not tns_admin.is_absolute():
    tns_admin = (ROOT / tns_admin).resolve()

oracledb.defaults.config_dir = str(tns_admin)
alias = oracle.get("tns_alias", "oraboh")

print("Oracle driver:", oracledb.__version__)
print("TNS alias:", alias)
print("TNS directory:", tns_admin)

try:
    with oracledb.connect(
        user=oracle["username"],
        password=oracle["password"],
        dsn=alias
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM dual")
            print("SUCCESS: Oracle connection established.")
            print("Result:", cursor.fetchone()[0])

except Exception as exc:
    print("ORACLE CONNECTION FAILED")
    print(type(exc).__name__, exc)
    raise SystemExit(1)
