# -*- coding: utf-8 -*-
"""Match uncategorized CC transactions to extracted invoices by vendor."""
import json
from datetime import datetime
from collections import defaultdict

with open("output/banking_summary_cache.json", encoding="utf-8") as f:
    banking = json.load(f)
with open("output/extracted_invoices.json", encoding="utf-8") as f:
    invoices = json.load(f)

uncat = [t for t in banking["transactions"] if t["status"] == "uncategorized"]

# Group CC txns by vendor
vendor_groups = defaultdict(list)
for t in uncat:
    desc = t["description"].upper()
    if "APPLE" in desc:
        vendor_groups["Apple"].append(t)
    elif "AMAZON" in desc:
        vendor_groups["Amazon"].append(t)
    elif "MICROSOFT" in desc or "MSFT" in desc:
        vendor_groups["Microsoft"].append(t)
    elif "LINKEDIN" in desc:
        vendor_groups["LinkedIn"].append(t)
    elif "GOOGLE" in desc:
        vendor_groups["Google"].append(t)
    elif "ZOHO" in desc:
        vendor_groups["Zoho"].append(t)
    elif "NEW RELIC" in desc or "NEWRELIC" in desc:
        vendor_groups["New Relic"].append(t)
    elif "ATLASSIAN" in desc:
        vendor_groups["Atlassian"].append(t)
    elif "GROQ" in desc:
        vendor_groups["Groq"].append(t)
    elif "OPENAI" in desc:
        vendor_groups["OpenAI"].append(t)

# Group invoices by vendor
inv_groups = defaultdict(list)
for inv in invoices:
    vn = (inv.get("vendor_name") or "").upper()
    preview = (inv.get("raw_text_preview") or "").upper()
    combined = vn + " " + preview
    if "APPLE" in combined:
        inv_groups["Apple"].append(inv)
    elif "AMAZON" in combined and "WEB SERVICE" not in combined:
        inv_groups["Amazon"].append(inv)
    elif "MICROSOFT" in combined or "MSFT" in combined:
        inv_groups["Microsoft"].append(inv)
    elif "LINKEDIN" in combined:
        inv_groups["LinkedIn"].append(inv)
    elif "GOOGLE" in combined and "PLAY" in combined:
        inv_groups["Google"].append(inv)
    elif "ZOHO" in combined:
        inv_groups["Zoho"].append(inv)
    elif "NEW RELIC" in combined:
        inv_groups["New Relic"].append(inv)
    elif "ATLASSIAN" in combined:
        inv_groups["Atlassian"].append(inv)
    elif "GROQ" in combined:
        inv_groups["Groq"].append(inv)
    elif "OPENAI" in combined:
        inv_groups["OpenAI"].append(inv)

for vendor in ["Apple", "Microsoft", "Amazon", "LinkedIn", "Google", "Zoho", "New Relic", "Atlassian", "Groq", "OpenAI"]:
    txns = vendor_groups.get(vendor, [])
    invs = inv_groups.get(vendor, [])
    if not txns:
        continue

    total_amt = sum(t["amount"] for t in txns)
    print(f"========== {vendor} ==========")
    print(f"  Uncategorized CC txns: {len(txns)}, Total: Rs{total_amt:,.2f}")
    print(f"  Extracted invoices: {len(invs)}")
    print()

    # Build invoice amount lookup
    inv_by_amt = defaultdict(list)
    for inv in invs:
        amt = inv.get("amount", 0)
        if amt and amt > 0:
            inv_by_amt[round(amt, 2)].append(inv)

    matched = []
    unmatched = []
    used_inv = set()

    for t in sorted(txns, key=lambda x: x["date"]):
        t_amt = round(t["amount"], 2)
        t_date = datetime.strptime(t["date"], "%Y-%m-%d")

        best = None
        best_idx = None
        if t_amt in inv_by_amt:
            for i, inv in enumerate(inv_by_amt[t_amt]):
                inv_id = inv.get("invoice_number", "") + inv.get("date", "")
                if inv_id in used_inv:
                    continue
                try:
                    inv_date = datetime.strptime(inv["date"], "%Y-%m-%d")
                except:
                    continue
                gap = abs((inv_date - t_date).days)
                if gap <= 15:
                    if best is None or gap < best[1]:
                        best = (inv, gap)
                        best_idx = inv_id

        if best:
            matched.append((t, best[0], best[1]))
            if best_idx:
                used_inv.add(best_idx)
        else:
            unmatched.append(t)

    if matched:
        print(f"  MATCHED ({len(matched)}):")
        for t, inv, gap in matched:
            card = t["card"][:20]
            inv_num = inv.get("invoice_number", "")[:20]
            print(f"    {card:20s} | CC:{t['date']} Rs{t['amount']:>10,.2f} | Inv:{inv['date']} Rs{inv['amount']:>10,.2f} | #{inv_num} | {gap}d")

    if unmatched:
        print(f"  UNMATCHED ({len(unmatched)}):")
        for t in unmatched:
            card = t["card"][:20]
            desc = t["description"][:55]
            # Replace problematic chars
            desc = desc.encode("ascii", "replace").decode("ascii")
            print(f"    {card:20s} | {t['date']} Rs{t['amount']:>10,.2f} | {desc}")
    print()
