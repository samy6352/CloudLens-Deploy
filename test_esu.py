"""Prove the end-of-support tab is right, including on data this estate does not have.

Nothing here is out of support, so the live tab correctly reports zero — which proves nothing
about whether it would report the right thing when a customer runs Windows Server 2012. So the
inventory queries are stubbed with machines of each kind and the report is checked against what
the ESU rules actually say:

  * in Azure, ESU is included, so an out-of-support Azure VM costs nothing extra
  * outside Azure it is billed per core through Arc, so an uncovered machine has a real number
  * an Arc machine with a licence assigned is covered and costs nothing further
  * supported versions, Linux and appliance images are not findings at all
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import esu as esu_mod
from app import waste
from app.esu import LIFECYCLE, SQL_LIFECYCLE, classify, esu_prices, esu_report

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


TODAY = date(2026, 8, 22)

print("VERSION CLASSIFICATION")
cases = [
    ("WindowsServer 2012-R2-Datacenter", "Windows Server 2012 R2", "out of support"),
    ("WindowsServer 2012-Datacenter", "Windows Server 2012", "out of support"),
    ("2008-R2-SP1", "Windows Server 2008 R2", "out of support"),
    ("WindowsServer 2016-Datacenter", "Windows Server 2016", "ending soon"),
    ("WindowsServer 2019-datacenter", "Windows Server 2019", "supported"),
    ("2022-datacenter-azure-edition", "Windows Server 2022", "supported"),
]
for text, product, status in cases:
    got = classify(text, today=TODAY)
    check(f"{text} -> {product}, {status}",
          got is not None and got["product"] == product and got["status"] == status,
          str(got))

for text in ("ubuntu-24_04-lts", "win11-22h2-pro", "check-point-cg-r8120", ""):
    check(f"{text or '(empty)'} is not reported as a Windows Server",
          classify(text, today=TODAY) is None)

# R2 must win over the bare year, or every 2012 R2 box is labelled plain 2012.
check("2012 R2 is distinguished from 2012",
      classify("2012-R2-Datacenter", today=TODAY)["product"] == "Windows Server 2012 R2")

print("\nSQL CLASSIFICATION")
check("SQL2012 is out of support",
      classify("SQL2012-WS2012", SQL_LIFECYCLE, today=TODAY)["product"] == "SQL Server 2012")
# The trap: the image offer carries both a SQL year and a Windows year.
check("SQL2019 on WS2016 is read as SQL 2019, not SQL 2016",
      classify("SQL2019-WS2016 Standard", SQL_LIFECYCLE, today=TODAY)["product"]
      == "SQL Server 2019",
      str(classify("SQL2019-WS2016 Standard", SQL_LIFECYCLE, today=TODAY)))
check("and it is therefore still supported",
      classify("SQL2019-WS2016 Standard", SQL_LIFECYCLE, today=TODAY)["status"] == "supported")

print("\nLIVE PRICE LIST")
rates = asyncio.run(esu_prices())
check("rates come back from the retail catalogue", bool(rates), str(list(rates)[:3]))
if rates:
    std = rates.get("Windows Server 2012|Standard")
    dc = rates.get("Windows Server 2012|Datacenter")
    check("Windows Server 2012 Standard is priced", std is not None and std > 0, str(std))
    check("Datacenter costs more per core than Standard", dc and std and dc > std, f"{dc} vs {std}")
    check("a per-core hourly rate is a plausible size", 0 < (std or 0) < 1, str(std))


# ---------------------------------------------------------------- the report
print("\nREPORT, WITH MACHINES THIS ESTATE DOES NOT HAVE")

FAKE = {
    "microsoft.compute/virtualmachines": [
        {"name": "legacy-app01", "id": "/vm/1", "resourceGroup": "rg-legacy",
         "location": "westus", "publisher": "MicrosoftWindowsServer", "offer": "WindowsServer",
         "sku": "2012-R2-Datacenter", "vmSize": "Standard_D2s_v3"},
        {"name": "modern01", "id": "/vm/2", "resourceGroup": "rg-app", "location": "westus",
         "publisher": "MicrosoftWindowsServer", "offer": "WindowsServer",
         "sku": "2022-datacenter", "vmSize": "Standard_D2s_v3"},
        {"name": "linux01", "id": "/vm/3", "resourceGroup": "rg-app", "location": "westus",
         "publisher": "canonical", "offer": "ubuntu-24_04-lts", "sku": "server",
         "vmSize": "Standard_D2s_v3"},
    ],
    "microsoft.hybridcompute/machines": [
        {"name": "onprem-dc01", "id": "/arc/1", "resourceGroup": "rg-arc", "location": "eastus",
         "osName": "windows", "osSku": "Windows Server 2012 R2 Standard", "status": "Connected",
         "cores": "16", "esuState": "NotAssigned", "esuEligibility": "Eligible"},
        {"name": "onprem-file01", "id": "/arc/2", "resourceGroup": "rg-arc", "location": "eastus",
         "osName": "windows", "osSku": "Windows Server 2012 Standard", "status": "Connected",
         "cores": "4", "esuState": "Assigned", "esuEligibility": "Eligible"},
        {"name": "onprem-new01", "id": "/arc/3", "resourceGroup": "rg-arc", "location": "eastus",
         "osName": "windows", "osSku": "Windows Server 2022 Datacenter", "status": "Connected",
         "cores": "8", "esuState": "NotAssigned", "esuEligibility": "Ineligible"},
    ],
    "microsoft.sqlvirtualmachine/sqlvirtualmachines": [
        {"name": "sql-old01", "id": "/sql/1", "resourceGroup": "rg-data", "location": "westus",
         "image": "SQL2012-WS2012", "edition": "Standard"},
    ],
}


async def fake_arg(query: str, subscriptions: list[str], top: int = 500) -> list[dict]:
    for resource_type, rows in FAKE.items():
        if resource_type in query.lower():
            return rows
    return []


waste._arg = fake_arg  # the report imports this at call time
report = asyncio.run(esu_report(subscription_ids=["sub-a"], days=30))
by_name = {m["name"]: m for m in report["machines"]}

check("only machines past or near end of support are reported",
      set(by_name) == {"legacy-app01", "onprem-dc01", "onprem-file01", "sql-old01"},
      str(sorted(by_name)))
check("a supported Windows Server is not a finding", "modern01" not in by_name)
check("a Linux VM is not a finding", "linux01" not in by_name)
check("a supported Arc server is not a finding", "onprem-new01" not in by_name)

vm = by_name["legacy-app01"]
check("an out-of-support Azure VM is reported", vm["status"] == "out of support")
check("...and is marked covered, because Azure includes ESU", vm["covered"] is True)
check("...and therefore costs nothing extra", vm["monthly_esu_cost"] == 0.0)
check("...with wording that says why", "free for Azure VMs" in vm["coverage"], vm["coverage"])

arc = by_name["onprem-dc01"]
check("an uncovered Arc server is reported as uncovered", arc["covered"] is False)
check("...and carries a real monthly estimate",
      (arc["monthly_esu_cost"] or 0) > 0 if rates else arc["monthly_esu_cost"] is None,
      str(arc["monthly_esu_cost"]))
if rates:
    base = esu_mod._rate_for("Windows Server 2012 R2", rates)
    check("...priced through the WS2012 meter, which also covers R2", base is not None, str(base))
    check("...priced on its actual core count, not the 8-core floor",
          abs(arc["monthly_esu_cost"] - round(base * 16 * 730, 2)) < 0.01,
          f"{arc['monthly_esu_cost']} vs {round((base or 0) * 16 * 730, 2)} for 16 cores")

covered = by_name["onprem-file01"]
check("an Arc server with a licence assigned is covered", covered["covered"] is True)
check("...and adds nothing to the estimate", covered["monthly_esu_cost"] == 0.0)

sql = by_name["sql-old01"]
check("an out-of-support SQL Server on an Azure VM is reported",
      sql["product"] == "SQL Server 2012")
check("...and is covered, because Azure includes SQL ESU too", sql["covered"] is True)

check("the totals count only uncovered machines", report["exposed"] == 1, str(report["exposed"]))
check("the monthly estimate matches the uncovered machine",
      abs(report["estimated_monthly_cost"] - (arc["monthly_esu_cost"] or 0)) < 0.01,
      f"{report['estimated_monthly_cost']} vs {arc['monthly_esu_cost']}")
check("out-of-support and ending-soon are counted separately",
      report["out_of_support"] >= 3 and isinstance(report["ending_soon"], int),
      f"{report['out_of_support']} / {report['ending_soon']}")
check("the scan is reported so an empty result is explainable",
      report["scanned"]["vms"] == 3 and report["scanned"]["arc"] == 3,
      str(report["scanned"]))
check("the note explains the Azure-versus-Arc rule",
      "Azure VMs" in report["note"] and "Arc" in report["note"])

print(f"\n  {'FAILED: ' + ', '.join(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
