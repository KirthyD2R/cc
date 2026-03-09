"""
Categorize extracted invoices into expense types based on line item descriptions.

Reads extracted_invoices.json, classifies each invoice into an expense category
using keyword-based rules, and outputs expense_categories.json.
"""

import os
import re
import json
from utils import PROJECT_ROOT, log_action

INPUT_FILE = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "output", "expense_categories.json")

# --- Category Rules ---
# Each rule: (category, subcategory, keywords_in_description, keywords_in_vendor)
# First match wins — order matters (more specific rules first)

RULES = [
    # --- Software & SaaS ---
    ("Software & SaaS", "HR & Payroll Software", ["zoho payroll", "zoho people"], []),
    ("Software & SaaS", "Accounting Software", ["zoho books", "zoho invoice"], []),
    ("Software & SaaS", "CRM Software", ["zoho crm"], []),
    ("Software & SaaS", "Zoho Suite", ["zoho"], ["zoho"]),
    ("Software & SaaS", "Version Control & DevOps", ["github team", "github actions", "github copilot"], ["github"]),
    ("Software & SaaS", "AI Tools", ["claude pro", "max plan", "chatgpt"], ["anthropic", "openai"]),
    ("Software & SaaS", "AI API Usage", ["deepseek", "llama", "gpt-oss", "meta-llama", "bedrock"], ["groq"]),
    ("Software & SaaS", "Database & Backend", ["pro plan", "supabase", "startup", "credits"], ["supabase", "s2 labs"]),
    ("Software & SaaS", "Recruitment Software", ["recruiter lite", "premium business"], ["linkedin"]),
    ("Software & SaaS", "Job Portal", [], ["naukri", "info edge"]),
    ("Software & SaaS", "Publishing Platform", [], ["medium"]),
    ("Software & SaaS", "Google Workspace", ["google workspace"], []),
    ("Software & SaaS", "Google Cloud / Workspace", [], ["google"]),
    ("Software & SaaS", "App Subscriptions", ["google play"], ["google play"]),

    # --- Cloud & Hosting ---
    ("Cloud & Hosting", "Cloud Compute (AWS)", ["amazon elastic compute", "amazon ec2", "aws app runner",
                                                  "amazon ecs", "elastic container", "amazon sagemaker"], []),
    ("Cloud & Hosting", "Cloud Database (AWS)", ["amazon relational database", "amazon dynamodb",
                                                  "amazon elasticache", "amazon opensearch"], []),
    ("Cloud & Hosting", "Cloud Storage (AWS)", ["amazon simple storage", "amazon s3"], []),
    ("Cloud & Hosting", "Cloud Networking (AWS)", ["amazon route 53", "aws data transfer",
                                                    "amazon virtual private", "aws waf",
                                                    "amazon cloudfront"], []),
    ("Cloud & Hosting", "Cloud Security (AWS)", ["aws secrets manager", "aws key management",
                                                  "amazon cognito", "aws identity"], []),
    ("Cloud & Hosting", "Cloud AI (AWS)", ["amazon bedrock"], []),
    ("Cloud & Hosting", "Cloud Messaging (AWS)", ["aws end user messaging", "amazon simple notification",
                                                   "amazon simple email"], []),
    ("Cloud & Hosting", "Cloud Other (AWS)", ["aws", "amazon web services"], ["amazon web services"]),
    ("Cloud & Hosting", "Cloud Compute (Azure)", ["microsoft 365", "azure", "997331", "microsoft"], ["microsoft"]),
    ("Cloud & Hosting", "Cloud Hosting (Fly.io)", [], ["fly.io"]),
    ("Cloud & Hosting", "Cloud Hosting (Fly.io)", ["pay-as-you-go", "bandwidth"], ["fly.io"]),

    # --- Hardware & Equipment ---
    ("Hardware & Equipment", "Laptops & Computers", ["macbook", "laptop", "notebook"], ["aptronix"]),
    ("Hardware & Equipment", "Computer Accessories", ["monitor stand", "keyboard", "mouse", "dell ms116",
                                                       "logitech", "usb hub", "webcam", "headset",
                                                       "cable protector", "charging cable", "usb c",
                                                       "type-c", "spike guard", "extension board",
                                                       "multi plug", "adaptor", "adapter", "smart plug",
                                                       "power inverter", "charger", "wireless carplay",
                                                       "hardwire kit"], []),
    ("Hardware & Equipment", "Printers & Supplies", ["printer", "canon pixma", "ink", "toner",
                                                      "printer cover", "copier paper", "a4 size",
                                                      "a4 document"], []),
    ("Hardware & Equipment", "Phone Accessories", ["iphone", "phone case", "magsafe wallet",
                                                    "phone mount", "car phone", "screen protector"], []),
    ("Hardware & Equipment", "Kitchen Appliances", ["cooking robot", "nosh ai", "glass carafe",
                                                     "tea pot", "kettle", "water container",
                                                     "storage jar"], []),
    ("Hardware & Equipment", "Dash Cam & Car Accessories", ["dash cam", "wolfbox", "car seat belt",
                                                             "sun visor", "car trunk organizer",
                                                             "seat belt cutter", "pressure washer",
                                                             "mesh car", "car organizer"], []),
    ("Hardware & Equipment", "Electronics & Gadgets", ["air blower", "turbo jet", "portronics",
                                                        "snapcase"], []),
    ("Hardware & Equipment", "Laptops & Computers", ["imei", "serial"], ["aptronix"]),

    # --- Office Supplies ---
    ("Office Supplies", "Stationery", ["pen holder", "pencil", "marker", "ball pen", "stamp pad",
                                        "envelope", "file folder", "document holder"], []),
    ("Office Supplies", "Paper Products", ["paper roll", "tissue", "toilet paper", "kitchen tissue",
                                            "kitchen towel", "paper a4", "bond paper", "jk paper",
                                            "jk easy copier"], []),
    ("Office Supplies", "Packaging", ["packaging", "tape", "bopp", "garbage bag", "dustbin",
                                       "waste plastic"], []),

    # --- Housekeeping & Maintenance ---
    ("Housekeeping", "Cleaning Supplies", ["disinfectant", "surface cleaner", "floor cleaner",
                                            "toilet cleaner", "bathroom cleaner", "glass cleaner",
                                            "household cleaner", "harpic", "lizol", "mr muscle",
                                            "kitchen cleaner", "dishwash", "dish wash",
                                            "cleaning cloth", "microfiber", "wipes", "dettol",
                                            "broom", "grass broom", "jhadu",
                                            "air wick", "freshmatic"], []),
    ("Housekeeping", "Sanitary Supplies", ["sanitary pad", "stayfree"], []),

    # --- Pantry & Kitchen ---
    ("Pantry & Kitchen", "Beverages", ["coffee", "tea", "kahwa", "filter coffee", "levista"], []),
    ("Pantry & Kitchen", "Food & Groceries", ["sugar", "honey", "saffola", "amrit brown sugar"], []),

    # --- Insurance & Warranty ---
    ("Insurance & Warranty", "Product Warranty", ["protect promise", "extended warranty",
                                                   "protection plan"], []),

    # --- Medical & Health ---
    ("Medical & Health", "Pharmacy", [], ["apollo pharmacies", "apollo pharmacy"]),

    # --- Advertising & Marketing ---
    ("Advertising & Marketing", "Digital Ads", ["google ads"], []),

    # --- Vendor-only fallbacks (for invoices without line items) ---
    ("Office Supplies", "General Purchase", [], ["amazon retail india", "a2z sales",
                                                  "aggarwal enterprises"]),
]


def categorize_description(description, vendor_name):
    """Match description + vendor against rules. Returns (category, subcategory)."""
    desc_lower = (description or "").lower()
    vendor_lower = (vendor_name or "").lower()

    for category, subcategory, desc_keywords, vendor_keywords in RULES:
        desc_match = desc_keywords and any(kw in desc_lower for kw in desc_keywords)
        vendor_match = vendor_keywords and any(kw in vendor_lower for kw in vendor_keywords)
        if desc_match or (vendor_match and not desc_keywords):
            return category, subcategory

    return "Uncategorized", "Other"


def categorize_invoice(inv):
    """Categorize a single invoice entry. Returns enriched dict."""
    vendor = inv.get("vendor_name", "")
    amount = inv.get("amount")
    currency = inv.get("currency", "INR")

    # Use line items if available, otherwise use vendor name as description
    if "line_items" in inv and inv["line_items"]:
        items = inv["line_items"]
        # Categorize based on first (or most expensive) line item
        primary = max(items, key=lambda x: x.get("amount") or 0)
        desc = primary.get("description", "")
        # Also check all descriptions for better matching
        all_descs = " | ".join(it.get("description", "") for it in items)
    else:
        desc = vendor or ""
        all_descs = desc

    category, subcategory = categorize_description(all_descs, vendor)

    # If primary didn't match, try vendor-only match
    if category == "Uncategorized":
        category, subcategory = categorize_description(desc, vendor)

    result = {
        "file": inv.get("file"),
        "vendor_name": vendor,
        "invoice_number": inv.get("invoice_number"),
        "date": inv.get("date"),
        "amount": amount,
        "currency": currency,
        "expense_category": category,
        "expense_subcategory": subcategory,
        "description": desc[:200] if desc else None,
    }

    # Include line items summary
    if "line_items" in inv:
        result["line_items_count"] = len(inv["line_items"])
        result["line_items"] = [
            {
                "description": it.get("description", "")[:150],
                "amount": it.get("amount"),
                "quantity": it.get("quantity", 1),
            }
            for it in inv["line_items"]
        ]

    return result


def run():
    log_action("=" * 50)
    log_action("Expense Categorization")
    log_action("=" * 50)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        invoices = json.load(f)

    log_action(f"Loaded {len(invoices)} invoices")

    results = []
    category_summary = {}
    for inv in invoices:
        categorized = categorize_invoice(inv)
        results.append(categorized)

        cat = categorized["expense_category"]
        sub = categorized["expense_subcategory"]
        key = f"{cat} > {sub}"
        if key not in category_summary:
            category_summary[key] = {"count": 0, "total_inr": 0.0, "total_usd": 0.0}
        category_summary[key]["count"] += 1
        amt = categorized.get("amount") or 0
        if categorized.get("currency") == "USD":
            category_summary[key]["total_usd"] += amt
        else:
            category_summary[key]["total_inr"] += amt

    # Sort results by date
    results.sort(key=lambda x: x.get("date") or "")

    # Build output with summary
    output = {
        "generated": __import__("datetime").datetime.now().isoformat(),
        "total_invoices": len(results),
        "category_summary": dict(sorted(category_summary.items())),
        "invoices": results,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log_action(f"\nExpense Category Summary:")
    log_action(f"{'Category':<50s} {'Count':>5s} {'INR':>12s} {'USD':>10s}")
    log_action("-" * 80)
    for key in sorted(category_summary.keys()):
        s = category_summary[key]
        inr = f"₹{s['total_inr']:,.2f}" if s['total_inr'] else ""
        usd = f"${s['total_usd']:,.2f}" if s['total_usd'] else ""
        log_action(f"{key:<50s} {s['count']:>5d} {inr:>12s} {usd:>10s}")

    uncategorized = sum(1 for r in results if r["expense_category"] == "Uncategorized")
    log_action(f"\nCategorized: {len(results) - uncategorized}/{len(results)} ({(len(results)-uncategorized)*100//len(results)}%)")
    log_action(f"Output: {OUTPUT_FILE}")

    return results


def main():
    run()


if __name__ == "__main__":
    main()
