"""Match uncategorized CC transactions to extracted invoices by amount + date."""
import json
from datetime import datetime

with open("output/banking_summary_cache.json", encoding="utf-8") as f:
    banking = json.load(f)
with open("output/extracted_invoices.json", encoding="utf-8") as f:
    invoices = json.load(f)

# Get all uncategorized transactions
uncat = [t for t in banking["transactions"] if t["status"] == "uncategorized"]
print(f"Total uncategorized CC txns: {len(uncat)}")

# Build invoice lookup by amount (rounded)
inv_by_amt = {}
for inv in invoices:
    amt = inv.get("amount", 0)
    if amt and amt > 0:
        inv_by_amt.setdefault(round(amt, 2), []).append(inv)

# Match
matches = []
matched_txn_ids = set()
for txn in uncat:
    txn_amt = round(txn["amount"], 2)
    txn_date = datetime.strptime(txn["date"], "%Y-%m-%d")

    if txn_amt in inv_by_amt:
        for inv in inv_by_amt[txn_amt]:
            inv_date_str = inv.get("date", "")
            if not inv_date_str:
                continue
            try:
                inv_date = datetime.strptime(inv_date_str, "%Y-%m-%d")
            except:
                continue
            day_diff = abs((inv_date - txn_date).days)
            if day_diff <= 15:
                matches.append((txn, inv, day_diff))
                matched_txn_ids.add(txn["transaction_id"])

# Deduplicate
seen = set()
unique_matches = []
for txn, inv, days in sorted(matches, key=lambda x: (x[0]["card"], x[0]["date"])):
    key = (txn["transaction_id"], inv.get("invoice_number"))
    if key in seen:
        continue
    seen.add(key)
    unique_matches.append((txn, inv, days))

print(f"Matched txns: {len(matched_txn_ids)} unique CC txns matched to invoices")
print()
print("MATCHES (excluding already-categorized):")
print("=" * 120)

for txn, inv, days in unique_matches:
    desc = txn["description"][:50]
    vendor = inv.get("vendor_name", "")[:35]
    inv_num = inv.get("invoice_number", "")
    print(f"  {txn['card']:22s} | CC: {txn['date']} Rs{txn['amount']:>10,.2f} | {desc}")
    print(f"  {'':22s} | Inv: {inv['date']} Rs{inv['amount']:>10,.2f} | {vendor} | {inv_num} | {days}d gap")
    print()
