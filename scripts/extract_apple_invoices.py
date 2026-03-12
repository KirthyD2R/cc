# -*- coding: utf-8 -*-
"""Extract Apple invoice data from .eml files and add to JSON files."""
import email
import re
import html
import json
import os
import sys
import glob

def extract_apple_eml(path):
    """Extract invoice data from an Apple .eml file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except:
        return None

    msg = email.message_from_string(content)

    # Get body text
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except:
                    pass
                break
            elif ct == "text/plain" and not body:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except:
            body = str(msg.get_payload())

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", body)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    # Extract invoice number
    inv_match = re.search(r"Invoice\s*(?:Number|#|:)\s*[:\s]*([A-Z0-9][\w-]+)", text, re.IGNORECASE)
    if not inv_match:
        inv_match = re.search(r"(MLC[A-Z0-9]+)", text)
    inv_number = inv_match.group(1) if inv_match else None

    # Extract date - look for patterns like "Jan 16, 2025" or "16 Jan 2025" or "2025-01-16"
    date_match = re.search(r"(\w{3,9}\s+\d{1,2},?\s+\d{4})", text)
    date_str = None
    if date_match:
        from datetime import datetime
        raw = date_match.group(1).replace(",", "")
        for fmt in ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"]:
            try:
                dt = datetime.strptime(raw, fmt)
                date_str = dt.strftime("%Y-%m-%d")
                break
            except:
                continue

    # Extract amount - look for ₹ or INR or Rs amounts
    amt_match = re.search(r"(?:Total|Amount|Billed)\s*[:\s]*[\u20b9₹]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if not amt_match:
        amt_match = re.search(r"[\u20b9₹]\s*([\d,]+\.?\d*)", text)
    if not amt_match:
        amt_match = re.search(r"INR\s*([\d,]+\.?\d*)", text)

    amount = 0.0
    if amt_match:
        try:
            amount = float(amt_match.group(1).replace(",", ""))
        except:
            pass

    # Extract description
    desc_match = re.search(r"(?:iCloud|Apple\s+(?:Music|TV|One|Arcade|Store)|App\s+Store)[^.]*", text, re.IGNORECASE)
    description = desc_match.group(0).strip()[:100] if desc_match else "Apple subscription"

    return {
        "file": os.path.basename(path),
        "path": os.path.abspath(path),
        "vendor_name": "Apple",
        "invoice_number": inv_number,
        "date": date_str,
        "amount": amount,
        "currency": "INR",
        "raw_text_preview": text[:300],
        "vendor_gstin": None,
        "line_items": [{"description": description, "quantity": 1, "unit_price": None, "amount": amount}],
        "organized_path": None,
    }


# Find all Apple eml files
eml_patterns = [
    "new image invoices/Fw_ Apple invoice/*.eml",
    "new image invoices/Fw_ Apple invoice 25/*.eml",
    "new image invoices/*invoice from Apple*.eml",
]

all_files = []
for pattern in eml_patterns:
    all_files.extend(glob.glob(pattern))

all_files = sorted(set(all_files))
print(f"Found {len(all_files)} Apple .eml files")

# Extract all
extracted = []
errors = []
for path in all_files:
    result = extract_apple_eml(path)
    if result and result["date"]:
        extracted.append(result)
    else:
        errors.append(path)

print(f"Successfully extracted: {len(extracted)}")
print(f"Failed/no date: {len(errors)}")

# Show summary
print()
print("EXTRACTED APPLE INVOICES:")
print("-" * 100)
total = 0
for inv in sorted(extracted, key=lambda x: x["date"] or ""):
    total += inv["amount"]
    inv_num = (inv["invoice_number"] or "N/A")[:20]
    desc = inv["line_items"][0]["description"][:40]
    print(f"  {inv['date']} | Rs{inv['amount']:>8,.2f} | #{inv_num:20s} | {desc}")

print(f"\nTotal: {len(extracted)} invoices, Rs{total:,.2f}")

# Add to extracted_invoices.json
with open("output/extracted_invoices.json", "r", encoding="utf-8") as f:
    existing = json.load(f)

existing_inv_nums = {inv.get("invoice_number") for inv in existing if inv.get("invoice_number")}
added = 0
for inv in extracted:
    if inv["invoice_number"] and inv["invoice_number"] not in existing_inv_nums:
        existing.append(inv)
        added += 1
    elif not inv["invoice_number"]:
        # Check by file+date to avoid dups
        key = inv["file"] + (inv["date"] or "")
        existing_keys = {e["file"] + (e.get("date") or "") for e in existing}
        if key not in existing_keys:
            existing.append(inv)
            added += 1

with open("output/extracted_invoices.json", "w", encoding="utf-8") as f:
    json.dump(existing, f, indent=2, ensure_ascii=False)

print(f"\nextracted_invoices.json: added {added} (total: {len(existing)})")

# Add to compare_invoices.json
with open("output/compare_invoices.json", "r", encoding="utf-8") as f:
    compare = json.load(f)

compare_inv_nums = {inv.get("invoice_number") for inv in compare if inv.get("invoice_number")}
compare_keys = {e["file"] + (e.get("date") or "") for e in compare}
added_cmp = 0
for inv in extracted:
    if inv["invoice_number"] and inv["invoice_number"] not in compare_inv_nums:
        compare.append(inv)
        added_cmp += 1
    elif not inv["invoice_number"]:
        key = inv["file"] + (inv["date"] or "")
        if key not in compare_keys:
            compare.append(inv)
            added_cmp += 1

with open("output/compare_invoices.json", "w", encoding="utf-8") as f:
    json.dump(compare, f, indent=2, ensure_ascii=False)

print(f"compare_invoices.json: added {added_cmp} (total: {len(compare)})")

if errors:
    print(f"\nFailed files:")
    for e in errors[:5]:
        print(f"  {e}")
