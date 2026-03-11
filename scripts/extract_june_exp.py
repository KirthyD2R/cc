"""One-time script: Extract 11 invoices from june exp.pdf into both JSONs."""
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
base_file = "june exp.pdf"
base_path = os.path.join(PROJECT_ROOT, "new image invoices", base_file)

invoices = [
    {
        "file": "june exp.pdf - Page 1",
        "path": base_path,
        "vendor_name": "Residence Inn by Marriott",
        "invoice_number": "Folio-90452",
        "date": "2025-06-14",
        "amount": 890.12,
        "currency": "USD",
        "raw_text_preview": "Residence Inn Beaumont, 5380 Clearwater Ct, Beaumont TX 77705. Guest: Sylvester Daniel, Coburns Supply. Room 106, Rate $110/night. Arrive 14Jun25, Depart 21Jun25 (7 nights). Total Charges $890.12. Mastercard ****9677.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "Room Charge (7 nights @ $110)", "quantity": 7, "unit_price": 110.00, "amount": 770.00},
            {"description": "State Occupancy Tax", "quantity": 7, "unit_price": 6.60, "amount": 46.20},
            {"description": "City Tax", "quantity": 7, "unit_price": 9.90, "amount": 69.30},
            {"description": "State Cost - Recovery Fee", "quantity": 7, "unit_price": 0.66, "amount": 4.62}
        ],
        "organized_path": None,
        "note": "US hotel stay, paid via Mastercard ****9677"
    },
    {
        "file": "june exp.pdf - Page 2",
        "path": base_path,
        "vendor_name": "Munveedu",
        "invoice_number": "4940",
        "date": "2025-06-05",
        "amount": 512.00,
        "currency": "INR",
        "raw_text_preview": "Munveedu, 34/72 A, B. Ramachandra Adithanar Street, Gandhi Nagar, Adyar, Chennai-600020. GSTIN: 33AFVPN5438F1Z4. Date: 05/06/25. Bill No: 4940. Grand Total Rs.512.00.",
        "vendor_gstin": "33AFVPN5438F1Z4",
        "line_items": [
            {"description": "Mutton Bone Soup", "quantity": 1, "unit_price": 139.00, "amount": 139.00},
            {"description": "Mutton Sukka", "quantity": 1, "unit_price": 349.00, "amount": 349.00},
            {"description": "SGST 2.5%", "quantity": 1, "unit_price": None, "amount": 12.20},
            {"description": "CGST 2.5%", "quantity": 1, "unit_price": None, "amount": 12.20}
        ],
        "organized_path": None
    },
    {
        "file": "june exp.pdf - Page 3",
        "path": base_path,
        "vendor_name": "Al-Arab Family Restaurant",
        "invoice_number": "41",
        "date": "2025-05-30",
        "amount": 390.00,
        "currency": "INR",
        "raw_text_preview": "Al-Arab Family Restaurant, MM Complex Opp HP Petrol Bunk, Lalgudi, Trichy. Date: 30-05-2025. Bill No 41. Grand Total Rs.390.00.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "Chicken Briyani", "quantity": 2, "unit_price": 120.00, "amount": 240.00},
            {"description": "Chicken Lollipop", "quantity": 1, "unit_price": 150.00, "amount": 150.00}
        ],
        "organized_path": None
    },
    {
        "file": "june exp.pdf - Page 4",
        "path": base_path,
        "vendor_name": "Star Briyani",
        "invoice_number": "22932",
        "date": "2025-05-28",
        "amount": 961.00,
        "currency": "INR",
        "raw_text_preview": "Star Briyani, Chengalpattu-603101. GSTIN: 33ABMFA5215A1Z2. Date: 28/05/25. Bill No 22932. Grand Total Rs.961.00.",
        "vendor_gstin": "33ABMFA5215A1Z2",
        "line_items": [
            {"description": "Chicken Briyani", "quantity": 1, "unit_price": 275.00, "amount": 275.00},
            {"description": "Pepper Chicken Dry (boneless)", "quantity": 1, "unit_price": 280.00, "amount": 280.00},
            {"description": "Mutton Chukka", "quantity": 1, "unit_price": 360.00, "amount": 360.00},
            {"description": "SGST 2.5%", "quantity": 1, "unit_price": None, "amount": 22.88},
            {"description": "CGST 2.5%", "quantity": 1, "unit_price": None, "amount": 22.88}
        ],
        "organized_path": None
    },
    {
        "file": "june exp.pdf - Page 5",
        "path": base_path,
        "vendor_name": "McDonald's",
        "invoice_number": "MCD-4237-045",
        "date": "2025-06-15",
        "amount": 13.17,
        "currency": "USD",
        "raw_text_preview": "McDonald's Restaurant #4237, 4515 Dowlen Rd, Beaumont TX 77706. Date: 06/15/2025. Subtotal $12.17, Tax $1.00, Total $13.17. Mastercard ****9677.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "20 McNuggets", "quantity": 1, "unit_price": None, "amount": 6.99},
            {"description": "French Fries", "quantity": 1, "unit_price": None, "amount": 3.69},
            {"description": "Coke", "quantity": 1, "unit_price": None, "amount": 1.49},
            {"description": "Sales Tax", "quantity": 1, "unit_price": None, "amount": 1.00}
        ],
        "organized_path": None,
        "note": "US McDonald's, paid via Mastercard ****9677"
    },
    {
        "file": "june exp.pdf - Page 6",
        "path": base_path,
        "vendor_name": "Best Buy",
        "invoice_number": "BB-238-061525",
        "date": "2025-06-15",
        "amount": 21.64,
        "currency": "USD",
        "raw_text_preview": "Best Buy #238, 5885 Eastex Fwy, Beaumont TX 77706. Date: 06/15/25. Insignia All-In-One Travel Adapter $24.99, Discount -$5.00, Tax $1.65, Total $21.64. Mastercard ****9677.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "Insignia All-In-One Travel Adapter", "quantity": 1, "unit_price": 24.99, "amount": 24.99},
            {"description": "Sale Discount", "quantity": 1, "unit_price": None, "amount": -5.00},
            {"description": "Sales Tax", "quantity": 1, "unit_price": None, "amount": 1.65}
        ],
        "organized_path": None,
        "note": "US Best Buy, paid via Mastercard ****9677"
    },
    {
        "file": "june exp.pdf - Page 7",
        "path": base_path,
        "vendor_name": "Charley's Philly Steaks",
        "invoice_number": "CPS-02249-007614",
        "date": "2025-06-21",
        "amount": 8.22,
        "currency": "USD",
        "raw_text_preview": "Charley's Philly Steaks #02249, 111 Yale St, Houston TX 77007. Date: 6/21/2025. Sub Total $7.59, Sales Tax $0.63, Order Total $8.22. MasterCard ****9677.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "6pc Boneless & fry", "quantity": 1, "unit_price": None, "amount": 6.99},
            {"description": "Ranch (Side)", "quantity": 1, "unit_price": None, "amount": 0.60},
            {"description": "Sales Tax", "quantity": 1, "unit_price": None, "amount": 0.63}
        ],
        "organized_path": None,
        "note": "US restaurant, paid via MasterCard ****9677"
    },
    {
        "file": "june exp.pdf - Page 8",
        "path": base_path,
        "vendor_name": "Walmart",
        "invoice_number": "WM-5959-02837",
        "date": "2025-06-21",
        "amount": 173.03,
        "currency": "USD",
        "raw_text_preview": "Walmart Supercenter, 111 Yale St, Houston TX 77007. Date: 06/21/25. 11 items. Subtotal $159.84, Tax 8.25% $13.19, Total $173.03. Mastercard 9677.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "Food Bag", "quantity": 1, "unit_price": None, "amount": 3.43},
            {"description": "Protg", "quantity": 1, "unit_price": None, "amount": 24.73},
            {"description": "PG Intl Adpt", "quantity": 1, "unit_price": None, "amount": 31.84},
            {"description": "Fiorelli", "quantity": 1, "unit_price": None, "amount": 14.00},
            {"description": "Wallets", "quantity": 1, "unit_price": None, "amount": 17.94},
            {"description": "Fiorelli", "quantity": 1, "unit_price": None, "amount": 18.96},
            {"description": "Nerds Candy", "quantity": 1, "unit_price": None, "amount": 3.47},
            {"description": "Nugget Asst", "quantity": 1, "unit_price": None, "amount": 14.84},
            {"description": "WO Hard Club", "quantity": 1, "unit_price": None, "amount": 10.17},
            {"description": "MXD Vrty 30", "quantity": 1, "unit_price": None, "amount": 14.96},
            {"description": "Choc", "quantity": 1, "unit_price": None, "amount": 5.48},
            {"description": "Tax 8.25%", "quantity": 1, "unit_price": None, "amount": 13.19}
        ],
        "organized_path": None,
        "note": "US Walmart, paid via Mastercard 9677"
    },
    {
        "file": "june exp.pdf - Page 9",
        "path": base_path,
        "vendor_name": "Popeyes",
        "invoice_number": "POP-828",
        "date": "2025-06-22",
        "amount": 8.62,
        "currency": "USD",
        "raw_text_preview": "Popeyes, 3500 N Terminal Rd, Houston TX 77032 (airport). Date: 6/22/25. 6Pc Wings $6.99, Tax $0.58, Tip $1.05, Total $8.62. Mastercard ****9677.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "6Pc Wings Only - Spicy Bone-In", "quantity": 1, "unit_price": None, "amount": 6.99},
            {"description": "Tax", "quantity": 1, "unit_price": None, "amount": 0.58},
            {"description": "Tip", "quantity": 1, "unit_price": None, "amount": 1.05}
        ],
        "organized_path": None,
        "note": "US Popeyes (Houston airport), paid via Mastercard ****9677"
    },
    {
        "file": "june exp.pdf - Page 10",
        "path": base_path,
        "vendor_name": "QDF F and B (HIA Airport)",
        "invoice_number": "014387",
        "date": "2025-06-23",
        "amount": 29.00,
        "currency": "QAR",
        "raw_text_preview": "QNB POS, QDF F AND B, HIA Airport, Doha Qatar. Date: Jun 23, 25. Mastercard ****9677. Contactless. Invoice 014387. Total QAR 29.00.",
        "vendor_gstin": None,
        "line_items": [
            {"description": "Food & Beverage - Doha Airport", "quantity": 1, "unit_price": None, "amount": 29.00}
        ],
        "organized_path": None,
        "note": "Qatar Doha airport purchase, paid via Mastercard ****9677"
    },
    {
        "file": "june exp.pdf - Page 11",
        "path": base_path,
        "vendor_name": "Sanmith Pure Veg Restaurant",
        "invoice_number": "21713",
        "date": "2025-06-29",
        "amount": 620.00,
        "currency": "INR",
        "raw_text_preview": "Sanmith Pure Veg Restaurant, Besant Nagar, Chennai. GSTIN: 33ANUPP0505C2Z9. Date: 29/06/25. Bill No 21713. Grand Total Rs.620.00. Paid via GPay.",
        "vendor_gstin": "33ANUPP0505C2Z9",
        "line_items": [
            {"description": "Kothu Combo", "quantity": 1, "unit_price": 295.00, "amount": 295.00},
            {"description": "Steamed Rice Combo", "quantity": 1, "unit_price": 295.00, "amount": 295.00},
            {"description": "CGST 2.5%", "quantity": 1, "unit_price": None, "amount": 14.75},
            {"description": "SGST 2.5%", "quantity": 1, "unit_price": None, "amount": 14.75}
        ],
        "organized_path": None,
        "note": "Paid via GPay"
    },
]

for fname in ["output/extracted_invoices.json", "output/compare_invoices.json"]:
    fpath = os.path.join(PROJECT_ROOT, fname)
    with open(fpath, "r", encoding="utf-8") as f:
        existing = json.load(f)
    existing_files = {inv.get("file") for inv in existing}
    new = [inv for inv in invoices if inv["file"] not in existing_files]
    if new:
        existing.extend(new)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"{fname}: added {len(new)}, total {len(existing)}")

print("Done!")
