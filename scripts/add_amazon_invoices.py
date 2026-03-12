"""Add 10 new Amazon invoices to extracted_invoices.json and compare_invoices.json"""
import json

new_invoices = [
    {
        "file": "invoice.pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice.pdf",
        "vendor_name": "Shivam Enterprises",
        "invoice_number": "IN-4132",
        "date": "2025-06-26",
        "amount": 149.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - Eclet 5 A4 Document File Folder Bag - Order 408-1659780-0166741",
        "vendor_gstin": "07AXCPK1601J2ZT",
        "line_items": [{"description": "Eclet 5 A4 Document File Folder Bag, Transparent Envelope Holder Storage Case", "quantity": 1, "unit_price": 133.04, "amount": 149.0}],
        "organized_path": None
    },
    {
        "file": "invoice (1).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (1).pdf",
        "vendor_name": "Accuprints",
        "invoice_number": "VLMN-7560",
        "date": "2025-06-26",
        "amount": 197.01,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - AccuPrints Branded Premium Envelope White Cheque Size - Order 408-1746287-0702758",
        "vendor_gstin": "06AURPS1867J1ZV",
        "line_items": [{"description": "AccuPrints Branded Premium Envelope White Cheque Size 100gsm 4.5x10inch (50)", "quantity": 1, "unit_price": 166.96, "amount": 197.01}],
        "organized_path": None
    },
    {
        "file": "invoice (2).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (2).pdf",
        "vendor_name": "R K WorldInfocom Pvt Ltd",
        "invoice_number": "MAA4-2216157",
        "date": "2025-06-26",
        "amount": 258.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - Harpic Powerplus Disinfectant Toilet Cleaner 500ml Pack of 3 - Order 408-1746287-0702758",
        "vendor_gstin": "33AAECR0564M1ZA",
        "line_items": [{"description": "Harpic Powerplus Disinfectant Toilet Cleaner Liquid, Original - 500 ml (Pack of 3)", "quantity": 1, "unit_price": 218.64, "amount": 258.0}],
        "organized_path": None
    },
    {
        "file": "invoice (3).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (3).pdf",
        "vendor_name": "R K WorldInfocom Pvt Ltd",
        "invoice_number": "MAA4-2213948",
        "date": "2025-06-26",
        "amount": 452.20,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - Beco Natural Dishwash Liquid 2L Refill Pack x2 - Order 408-4770466-4407501",
        "vendor_gstin": "33AAECR0564M1ZA",
        "line_items": [{"description": "Beco Natural Dishwash Liquid-2Litres Refill Pack Orange Citrus Freshness", "quantity": 2, "unit_price": 201.70, "amount": 452.20}],
        "organized_path": None
    },
    {
        "file": "invoice (4).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (4).pdf",
        "vendor_name": "R K WorldInfocom Pvt Ltd",
        "invoice_number": "FMAB-182420",
        "date": "2025-06-26",
        "amount": 336.30,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - Dettol Germ Protection Wet Wipes 40 Count Pack of 3 - Order 408-4770466-4407501",
        "vendor_gstin": "33AAECR0564M1ZA",
        "line_items": [{"description": "Dettol Germ Protection Wet Wipes for Skin & Surfaces, Original - 40 Count (Pack of 3)", "quantity": 1, "unit_price": 300.0, "amount": 336.30}],
        "organized_path": None
    },
    {
        "file": "invoice (5).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (5).pdf",
        "vendor_name": "ETRADE MARKETING PRIVATE LIMITED",
        "invoice_number": "BLR7-1062095",
        "date": "2025-06-26",
        "amount": 198.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - Kuber Industries 3-Compartment Pen Holder Sandy Brown - Order 408-5469361-8046746",
        "vendor_gstin": "29AADCV4254H1Z4",
        "line_items": [{"description": "Kuber Industries 3-Compartment Pen Holder For Study Table, Marker/Pencil Stand & Desk Organizer", "quantity": 1, "unit_price": 167.80, "amount": 198.0}],
        "organized_path": None
    },
    {
        "file": "invoice (6).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (6).pdf",
        "vendor_name": "CLICKTECH RETAIL PRIVATE LIMITED",
        "invoice_number": "MAA4-1088537",
        "date": "2025-06-26",
        "amount": 225.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - CHAND SURAJ Stella Natural Grass Broom Stick - Order 408-7259262-9459548",
        "vendor_gstin": "33AAJCC9783E1ZE",
        "line_items": [{"description": "CHAND SURAJ Stella Natural Grass Broom Stick for Home Cleaning with Stainless Steel Handle", "quantity": 1, "unit_price": 190.68, "amount": 225.0}],
        "organized_path": None
    },
    {
        "file": "invoice (7).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (7).pdf",
        "vendor_name": "CLICKTECH RETAIL PRIVATE LIMITED",
        "invoice_number": "MAA4-1088415",
        "date": "2025-06-26",
        "amount": 294.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - AIPL BOPP Packaging Clear Tape 48MM x 50M Pack of 2 x3 - Order 408-7618554-1326721",
        "vendor_gstin": "33AAJCC9783E1ZE",
        "line_items": [{"description": "AIPL BOPP Packaging Clear Tape - 48MM x 50 Meter, Pack of 2", "quantity": 3, "unit_price": 83.06, "amount": 294.0}],
        "organized_path": None
    },
    {
        "file": "invoice (8).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (8).pdf",
        "vendor_name": "GLOBAL BRANDS",
        "invoice_number": "MAA4-12223",
        "date": "2025-06-26",
        "amount": 327.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - Mr Muscle Platinum Kitchen Cleaner 750Ml - Order 408-7618554-1326721",
        "vendor_gstin": "33AYWPA0131C1Z3",
        "line_items": [{"description": "Mr Muscle Mr. Muscle Platinum Kitchen Cleaner, 750Ml", "quantity": 1, "unit_price": 277.12, "amount": 327.0}],
        "organized_path": None
    },
    {
        "file": "invoice (9).pdf",
        "path": "D:\\cc new\\cc\\new image invoices\\amazon new\\invoice (9).pdf",
        "vendor_name": "Aggarwal Enterprises",
        "invoice_number": "IN-1362",
        "date": "2025-06-26",
        "amount": 319.0,
        "currency": "INR",
        "raw_text_preview": "Amazon.in Tax Invoice - SS Spoon Stand/Cutlery Holder Kitchen Shelves Combo Pack Of 2 - Order 408-9085880-4297166",
        "vendor_gstin": "07BNEPA0491F1ZG",
        "line_items": [
            {"description": "zero to infinity store Stainless Steel Spoon Stand/Cutlery Holder Kitchen Shelves (Combo Pack Of 2)", "quantity": 1, "unit_price": 168.75, "amount": 189.0},
            {"description": "Shipping Charges", "quantity": 1, "unit_price": 116.07, "amount": 130.0}
        ],
        "organized_path": None
    },
]

# Add to extracted_invoices.json
with open("output/extracted_invoices.json", "r", encoding="utf-8") as f:
    extracted = json.load(f)

existing_inv_nums = {inv.get("invoice_number") for inv in extracted}
added_ext = 0
for inv in new_invoices:
    if inv["invoice_number"] not in existing_inv_nums:
        extracted.append(inv)
        added_ext += 1

with open("output/extracted_invoices.json", "w", encoding="utf-8") as f:
    json.dump(extracted, f, indent=2, ensure_ascii=False)

print(f"extracted_invoices.json: added {added_ext} new invoices (total: {len(extracted)})")

# Add to compare_invoices.json
with open("output/compare_invoices.json", "r", encoding="utf-8") as f:
    compare = json.load(f)

existing_cmp_nums = {inv.get("invoice_number") for inv in compare}
added_cmp = 0
for inv in new_invoices:
    if inv["invoice_number"] not in existing_cmp_nums:
        compare.append(inv)
        added_cmp += 1

with open("output/compare_invoices.json", "w", encoding="utf-8") as f:
    json.dump(compare, f, indent=2, ensure_ascii=False)

print(f"compare_invoices.json: added {added_cmp} new invoices (total: {len(compare)})")

# Summary
print()
print("10 NEW AMAZON INVOICES (all dated 2025-06-26):")
print("-" * 90)
total = 0
for inv in new_invoices:
    total += inv["amount"]
    print(f"  {inv['invoice_number']:16s} | Rs{inv['amount']:>8.2f} | {inv['vendor_name'][:40]}")
print(f"  {'TOTAL':16s} | Rs{total:>8.2f}")
print()

# Order cross-check
print("ORDER CROSS-CHECK (invoice totals vs order totals):")
order_map = {}
order_names = {
    "408-1659780-0166741": 149,
    "408-1746287-0702758": 455.01,
    "408-4770466-4407501": 788.50,
    "408-5469361-8046746": 198,
    "408-7259262-9459548": 225,
    "408-7618554-1326721": 621,
    "408-9085880-4297166": 319,
}
for inv in new_invoices:
    for oid in order_names:
        if oid in inv["raw_text_preview"]:
            order_map.setdefault(oid, []).append(inv["amount"])

for oid, expected in order_names.items():
    got = sum(order_map.get(oid, []))
    status = "OK" if abs(got - expected) < 0.02 else "MISMATCH"
    print(f"  {oid}: expected Rs{expected}, invoices Rs{got} [{status}]")
