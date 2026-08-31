"""Prove the export loader handles real FOCUS and Azure schemas, in CSV and Parquet.

We cannot read a customer's export from this workstation (their storage is correctly locked
down), so this builds representative files with the *real* column names from both schema
families and runs them through the same code path the blob reader uses.
"""
import gzip, io, csv, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import exports
from app.warehouse import COLUMNS

# Genuine FOCUS 1.0 column names, as emitted by a FocusCost export.
FOCUS_HEADER = [
    "BilledCost", "BillingCurrency", "ChargePeriodStart", "ChargePeriodEnd",
    "SubAccountId", "SubAccountName", "ResourceId", "ResourceName",
    "ServiceName", "ServiceCategory", "RegionName", "PricingQuantity",
    "PricingUnit", "ChargeCategory", "CommitmentDiscountName", "Tags",
    "x_ResourceGroupName", "x_SkuDescription", "ListUnitPrice",
]
FOCUS_ROWS = [
    ["12.34", "USD", "2026-08-01", "2026-08-02",
     "sub-aaa", "Prod", "/subscriptions/sub-aaa/resourceGroups/rg1/providers/Microsoft.Compute/disks/d1",
     "d1", "Storage", "Storage", "eastus", "1", "1 GB/Month", "Usage", "", '{"env":"prod"}',
     "rg1", "P10 Disk", "0.05"],
    ["7.66", "USD", "2026-08-02", "2026-08-03",
     "sub-aaa", "Prod", "/subscriptions/sub-aaa/resourceGroups/rg1/providers/Microsoft.Web/serverfarms/asp1",
     "asp1", "Azure App Service", "Compute", "westeurope", "24", "1 Hour", "Usage",
     "SavingsPlan-1yr", "", "rg1", "P1v3", "0.32"],
]

# Azure's own ActualCost/AmortizedCost export names (different family, same target schema).
AZURE_HEADER = [
    "date", "costInBillingCurrency", "billingCurrency", "SubscriptionId", "subscriptionName",
    "resourceGroupName", "ResourceId", "meterCategory", "meterName", "resourceLocation",
    "quantity", "unitOfMeasure", "chargeType", "pricingModel", "benefitName", "tags",
]
AZURE_ROWS = [
    ["08/03/2026", "3.21", "USD", "sub-bbb", "Dev", "rg2",
     "/subscriptions/sub-bbb/resourceGroups/rg2/providers/Microsoft.Compute/virtualMachines/vm1",
     "Virtual Machines", "D2s v3", "centralindia", "24", "1 Hour", "Usage", "OnDemand", "", ""],
]


def as_csv(header, rows, gz=False):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    raw = buf.getvalue().encode()
    return gzip.compress(raw) if gz else raw


def as_parquet(header, rows):
    import duckdb
    con = duckdb.connect()
    cols = ", ".join(f'? AS "{h}"' for h in header)
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES " +
                ",".join("(" + ",".join("?" for _ in header) + ")" for _ in rows) +
                f") AS v({', '.join(chr(34) + h + chr(34) for h in header)})",
                [v for row in rows for v in row])
    path = Path(tempfile.gettempdir()) / "focus_test.parquet"
    con.execute(f"COPY t TO '{str(path).replace(chr(92), '/')}' (FORMAT PARQUET)")
    con.close()
    return path.read_bytes()


CASES = [
    ("FOCUS csv",        "part_0.csv",         as_csv(FOCUS_HEADER, FOCUS_ROWS)),
    ("FOCUS csv.gz",     "part_0.csv.gz",      as_csv(FOCUS_HEADER, FOCUS_ROWS, gz=True)),
    ("FOCUS parquet",    "part_0.parquet",     as_parquet(FOCUS_HEADER, FOCUS_ROWS)),
    ("Azure actual csv", "actual.csv",         as_csv(AZURE_HEADER, AZURE_ROWS)),
]

keys = list(COLUMNS)
ok = fail = 0

for label, name, raw in CASES:
    try:
        header, rows = exports._read_any(name, raw)
        recs, skipped = exports._load_rows(header, rows, "fallback-sub")
        if not recs:
            print(f"  FAIL {label:<18} produced no rows (skipped {skipped})")
            fail += 1
            continue
        r = dict(zip(keys, recs[0]))
        print(f"  OK   {label:<18} {len(recs)} row(s), skipped {skipped}")
        print(f"         date={r['ChargePeriodStart']}  cost={r['BilledCost']}  "
              f"cur={r['BillingCurrency']}  sub={r['SubAccountId']}")
        print(f"         svc={r['ServiceName']}  rg={r['ResourceGroup']}  res={r['ResourceName']}")
        print(f"         qty={r['PricingQuantity']}  charge={r['ChargeCategory']}  "
              f"benefit={r['BenefitName']}")
        # The mapping must actually populate the fields the app relies on.
        assert r["ChargePeriodStart"], "date not mapped"
        assert r["BilledCost"], "cost not mapped"
        assert r["SubAccountId"], "subscription not mapped"
        assert r["ServiceName"], "service not mapped"
        assert r["ResourceGroup"], "resource group not mapped"
        ok += 1
    except Exception as exc:
        print(f"  FAIL {label:<18} {str(exc)[:150]}")
        fail += 1

print(f"\n  {ok} passed, {fail} failed")

# The fallback subscription must fill in when the file omits it, and never override a real value.
header, rows = exports._read_any("x.csv", as_csv(FOCUS_HEADER, FOCUS_ROWS))
recs, _ = exports._load_rows(header, rows, "should-not-win")
assert dict(zip(keys, recs[0]))["SubAccountId"] == "sub-aaa", "fallback overwrote a real value"
print("  OK   fallback subscription does not override the file's own value")

sys.exit(1 if fail else 0)
