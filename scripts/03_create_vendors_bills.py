"""
Step 3: Create Vendors & Bills in Zoho Books

For each extracted invoice:
  1. Check if vendor exists in Zoho Books → create if not
  2. Create a bill with line items, dates, and expense account
  3. Attach the original PDF to the bill
"""

import os
import json
import re
import time
from utils import (
    PROJECT_ROOT, load_config, load_vendor_mappings,
    ZohoBooksAPI, VendorCategorizer, fuzzy_match_vendor, log_action,
)

INVOICES_FILE = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
COMPARE_INVOICES_FILE = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")
RESULTS_FILE = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
BILLS_CACHE = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
VENDORS_CACHE = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")

# Company GSTIN and state code (first 2 digits) for IGST vs CGST+SGST determination
_COMPANY_GSTIN = "33AAICD7217K1ZK"
_COMPANY_STATE_CODE = _COMPANY_GSTIN[:2]  # "33" = Tamil Nadu

# GSTIN state code → Zoho place_of_supply code mapping
_STATE_CODE_MAP = {
    "01": "JK", "02": "HP", "03": "PB", "04": "CH", "05": "UT", "06": "HR",
    "07": "DL", "08": "RJ", "09": "UP", "10": "BR", "11": "SK", "12": "AR",
    "13": "NL", "14": "MN", "15": "MZ", "16": "TR", "17": "ML", "18": "AS",
    "19": "WB", "20": "JH", "21": "OR", "22": "CT", "23": "MP", "24": "GJ",
    "25": "DD", "26": "DN", "27": "MH", "28": "AP", "29": "KA", "30": "GA",
    "31": "LD", "32": "KL", "33": "TN", "34": "PY", "35": "AN", "36": "TS",
    "37": "AP", "38": "LA",
}


def _normalize_bill_number(num):
    """Normalize bill number for fuzzy matching: strip INV- prefix, lowercase, remove non-alphanumeric."""
    if not num:
        return ""
    s = num.strip()
    s = re.sub(r'^(INV[-_]?)', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[^a-z0-9]', '', s.lower())
    return s


def load_invoices():
    # Prefer compare_invoices.json (matches what the UI bill picker uses)
    path = COMPARE_INVOICES_FILE if os.path.exists(COMPARE_INVOICES_FILE) else INVOICES_FILE
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _sanitize_vendor_name(name):
    """Clean vendor name for Zoho API compatibility."""
    import re
    # Remove characters Zoho rejects (keep letters, numbers, spaces, hyphens, dots, &, parentheses)
    name = re.sub(r'[^\w\s\-\.&()\',]', ' ', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name if name else None


def ensure_vendor(api, vendor_name, invoice, vendor_mappings, currency_map):
    """Find or create vendor in Zoho Books. Returns (vendor_id, clean_name) or (None, vendor_name)."""
    if not vendor_name:
        return None, vendor_name

    # Sanitize vendor name to avoid Zoho API errors
    vendor_name = _sanitize_vendor_name(vendor_name)
    if not vendor_name:
        return None, vendor_name

    # Try exact match first
    try:
        vendor_id = api.find_vendor(vendor_name)
    except Exception as e:
        log_action(f"  Vendor lookup failed for '{vendor_name}': {e}", "WARNING")
        vendor_id = None
    if vendor_id:
        log_action(f"  Found vendor: {vendor_name} ({vendor_id})")
        return vendor_id, vendor_name

    # Try fuzzy match from mappings
    mapped, score = fuzzy_match_vendor(vendor_name, vendor_mappings)
    if mapped:
        log_action(f"  Fuzzy matched '{vendor_name}' -> '{mapped}' (score: {score})")
        try:
            vendor_id = api.find_vendor(mapped)
            if vendor_id:
                return vendor_id, mapped
        except Exception as e:
            log_action(f"  Vendor lookup failed for '{mapped}': {e}", "WARNING")
        vendor_name = mapped  # Use the mapped name for creation

    # Try GSTIN match before creating — avoids duplicates when name differs
    vendor_gstin = invoice.get("vendor_gstin", "")
    if vendor_gstin:
        # Search Zoho directly by GSTIN (source of truth)
        try:
            gstin_vid, gstin_vname = api.find_vendor_by_gstin(vendor_gstin)
            if gstin_vid:
                log_action(f"  Found vendor by GSTIN {vendor_gstin}: {gstin_vname} ({gstin_vid})")
                return gstin_vid, gstin_vname or vendor_name
        except Exception as e:
            log_action(f"  GSTIN lookup failed: {e}", "WARNING")

    # Create new vendor with details from vendor_details config
    currency = invoice.get("currency", "INR")
    vendor_details = vendor_mappings.get("vendor_details", {}).get(vendor_name, {})

    vendor_data = {
        "contact_name": vendor_name,
        "contact_type": "vendor",
        "currency_id": currency_map.get(
            vendor_details.get("currency_code", currency),
            currency_map.get("INR"),
        ),
    }

    if vendor_details.get("company_name"):
        vendor_data["company_name"] = vendor_details["company_name"]
    if vendor_details.get("website"):
        vendor_data["website"] = vendor_details["website"]
    if vendor_details.get("billing_address"):
        vendor_data["billing_address"] = vendor_details["billing_address"]
    if vendor_details.get("shipping_address"):
        vendor_data["shipping_address"] = vendor_details["shipping_address"]
    # GST treatment: use vendor_details config, or infer from invoice data
    if vendor_details.get("gst_treatment"):
        vendor_data["gst_treatment"] = vendor_details["gst_treatment"]
    elif invoice.get("vendor_gstin"):
        vendor_data["gst_treatment"] = "business_gst"
        vendor_data["gst_no"] = invoice["vendor_gstin"]
    elif currency and currency != "INR":
        vendor_data["gst_treatment"] = "overseas"
    else:
        vendor_data["gst_treatment"] = "business_none"
    if vendor_details.get("gst_no"):
        vendor_data["gst_no"] = vendor_details["gst_no"]

    try:
        result = api.create_vendor(vendor_data)
        new_id = result.get("contact", {}).get("contact_id")
        if new_id:
            log_action(f"  Created vendor: {vendor_name} ({new_id})")
            return new_id, vendor_name
    except Exception as e:
        # If Zoho rejects GST fields (invalid value/element), retry without them
        err_lower = str(e).lower()
        has_gst_fields = "gst_treatment" in vendor_data or "gst_no" in vendor_data
        if has_gst_fields and ("gst" in err_lower or "invalid" in err_lower):
            log_action(f"  Zoho rejected GST fields, retrying without them: {e}", "WARNING")
            vendor_data.pop("gst_treatment", None)
            vendor_data.pop("gst_no", None)
            try:
                result = api.create_vendor(vendor_data)
                new_id = result.get("contact", {}).get("contact_id")
                if new_id:
                    log_action(f"  Created vendor (without GST): {vendor_name} ({new_id})")
                    return new_id, vendor_name
            except Exception as e2:
                log_action(f"  Failed to create vendor (retry): {e2}", "ERROR")
                return None, vendor_name
        else:
            log_action(f"  Failed to create vendor: {e}", "ERROR")
            return None, vendor_name

    log_action(f"  Failed to create vendor: {vendor_name}", "ERROR")
    return None, vendor_name


def create_bill_for_invoice(api, invoice, vendor_id, expense_accounts, default_expense, currency_map,
                            categorizer=None, vendor_name=None,
                            existing_bills=None, existing_bills_norm=None,
                            igst_tax_id=None, intrastate_tax_id=None, default_exemption_id=None):
    """Create a bill in Zoho Books for the given invoice."""
    currency = invoice.get("currency", "INR")
    amount = invoice.get("amount", 0)

    if not amount or amount <= 0:
        log_action(f"  Invalid amount for {invoice['file']}: {amount}", "WARNING")
        return None

    # Determine expense account — use categorizer if available
    account_id = None
    if categorizer and vendor_name:
        try:
            cat_account_id, cat_account_name = categorizer.get_account_for_vendor(
                vendor_name, invoice_data=invoice,
            )
            if cat_account_id:
                account_id = cat_account_id
        except Exception as e:
            log_action(f"  Categorization failed for '{vendor_name}': {e}", "WARNING")

    if not account_id:
        account_id = expense_accounts.get(default_expense)
    if not account_id:
        # Use first available expense account
        account_id = next(iter(expense_accounts.values()), None)

    # Build unique bill number
    inv_number = invoice.get("invoice_number") or re.sub(r'\.(pdf|eml)$', '', invoice["file"], flags=re.IGNORECASE)
    bill_number = inv_number

    # Determine GST treatment for bills (valid: business_gst, business_none, overseas, consumer)
    vendor_gstin = invoice.get("vendor_gstin", "")
    # Ignore company's own GSTIN if it was mistakenly extracted as vendor's
    if vendor_gstin == _COMPANY_GSTIN:
        vendor_gstin = ""
    if vendor_gstin:
        gst_treatment = "business_gst"
    elif currency and currency != "INR":
        gst_treatment = "overseas"
    else:
        gst_treatment = "business_none"

    # Determine IGST vs intrastate GST based on vendor state vs company state
    is_interstate = False
    vendor_state_code = ""
    if vendor_gstin and len(vendor_gstin) >= 2:
        vendor_state_code = vendor_gstin[:2]
        is_interstate = vendor_state_code != _COMPANY_STATE_CODE

    line_item = {
        "account_id": account_id,
        "description": f"Invoice: {invoice['file']}",
        "rate": amount,
        "quantity": 1,
    }

    # Apply correct tax based on interstate/intrastate, or tax exemption
    if gst_treatment == "business_gst":
        if is_interstate and igst_tax_id:
            line_item["tax_id"] = igst_tax_id
        elif not is_interstate and intrastate_tax_id:
            line_item["tax_id"] = intrastate_tax_id
        elif igst_tax_id:
            line_item["tax_id"] = igst_tax_id
        elif intrastate_tax_id:
            line_item["tax_id"] = intrastate_tax_id
    elif gst_treatment == "overseas" and default_exemption_id:
        line_item["tax_exemption_id"] = default_exemption_id
        line_item["tax_exemption_code"] = "non_taxable"
    # business_none: no tax, no exemption (unregistered Indian vendor)

    bill_data = {
        "vendor_id": vendor_id,
        "bill_number": bill_number,
        "date": invoice.get("date"),
        "due_date": invoice.get("date"),
        "currency_id": currency_map.get(currency, currency_map.get("INR")),
        "gst_treatment": gst_treatment,
        "is_inclusive_tax": gst_treatment == "business_gst",
        "line_items": [line_item],
    }
    if vendor_gstin:
        bill_data["gst_no"] = vendor_gstin

    try:
        result = api.create_bill(bill_data)
        bill = result.get("bill", {})
        bill_id = bill.get("bill_id")
        if bill_id:
            log_action(f"  Created bill {bill_number}: {bill_id} ({currency} {amount})")
            return bill_id, True
    except Exception as e:
        err_lower = str(e).lower()
        if "already been used" in err_lower or "already been created" in err_lower:
            # Bill exists in Zoho — try to find its ID so we can still attach PDF
            log_action(f"  Bill {bill_number} already exists in Zoho", "WARNING")
            # Look up from existing_bills maps passed by caller
            eid = None
            if existing_bills and bill_number in existing_bills:
                eid = existing_bills[bill_number]
            if not eid and existing_bills_norm:
                norm = _normalize_bill_number(bill_number)
                if norm:
                    eid = existing_bills_norm.get(norm)
            if not eid:
                # Fetch from Zoho by bill number
                try:
                    search = api.list_bills(bill_number=bill_number)
                    for b in search.get("bills", []):
                        if b.get("bill_number") == bill_number:
                            eid = b.get("bill_id")
                            break
                except Exception:
                    pass
            if eid:
                log_action(f"  Found existing bill ID: {eid} — will attach PDF")
                return eid, False
            return None, False
        else:
            log_action(f"  Failed to create bill: {e}", "ERROR")

    return None, False


def attach_pdf(api, bill_id, pdf_path):
    """Attach the original invoice PDF to the bill."""
    if not os.path.exists(pdf_path):
        log_action(f"  PDF not found for attachment: {pdf_path}", "WARNING")
        return False
    try:
        api.attach_to_bill(bill_id, pdf_path)
        log_action(f"  Attached PDF to bill {bill_id}")
        return True
    except Exception as e:
        log_action(f"  Failed to attach PDF: {e}", "WARNING")
        return False


# --- Run (importable by run_loop.py) ---

def run(selected_files=None):
    """Create vendors and bills in Zoho for unprocessed invoices.

    Args:
        selected_files: Optional list of filenames to process. If None, processes all.

    Returns:
        dict: {
            "created_count": int,
            "skipped_count": int,
            "new_bill_ids": list[str],
            "failed_files": list[str],
        }
    """
    log_action("=" * 50)
    log_action("Step 3: Create Vendors & Bills in Zoho Books")
    log_action("=" * 50)

    config = load_config()
    vendor_mappings = load_vendor_mappings()
    api = ZohoBooksAPI(config)

    default_expense = vendor_mappings.get("default_expense_account", "Credit Card Charges")
    expense_accounts = api.get_expense_accounts()
    log_action(f"Loaded {len(expense_accounts)} expense accounts")

    # Initialize vendor categorizer for intelligent account assignment
    categorizer = VendorCategorizer(api)
    log_action("Initialized VendorCategorizer for account assignment")

    currency_map = api.list_currencies()
    log_action(f"Loaded {len(currency_map)} currencies from Zoho")

    # Fetch taxes and tax exemptions for bill line items
    tax_map = {}  # tax_name_lower -> tax_id
    igst_tax_id = None       # For interstate transactions (IGST)
    intrastate_tax_id = None  # For intrastate transactions (CGST+SGST / GST group)
    try:
        taxes = api.list_taxes()
        for t in taxes:
            name = t.get("tax_name", "").lower()
            pct = t.get("tax_percentage", 0)
            tax_map[name] = t.get("tax_id")
            # Identify IGST (interstate) vs intrastate GST
            if pct == 18:
                if "igst" in name:
                    igst_tax_id = t.get("tax_id")
                elif "gst" in name and "igst" not in name:
                    intrastate_tax_id = t.get("tax_id")
        # Fallback: if only one type found, try any 18% GST
        if not igst_tax_id and not intrastate_tax_id:
            for t in taxes:
                if "gst" in t.get("tax_name", "").lower() and t.get("tax_percentage", 0) == 18:
                    intrastate_tax_id = t.get("tax_id")
                    break
        log_action(f"Loaded {len(taxes)} taxes (IGST: {igst_tax_id}, Intrastate GST: {intrastate_tax_id})")
    except Exception as e:
        log_action(f"Could not load taxes: {e}", "WARNING")

    default_exemption_id = None
    try:
        exemptions = api.list_tax_exemptions()
        if exemptions:
            default_exemption_id = exemptions[0].get("tax_exemption_id")
        log_action(f"Loaded {len(exemptions)} tax exemptions (default: {default_exemption_id})")
    except Exception as e:
        log_action(f"Could not load tax exemptions: {e}", "WARNING")

    invoices = load_invoices()
    if selected_files:
        selected_set = set(selected_files)
        extracted_files = {inv["file"] for inv in invoices}
        # Log unmatched files for debugging
        unmatched = selected_set - extracted_files
        if unmatched:
            log_action(f"Selected files NOT found in extracted_invoices.json: {unmatched}", "WARNING")
        invoices = [inv for inv in invoices if inv["file"] in selected_set]
        log_action(f"Filtered to {len(invoices)} selected invoices (of {len(selected_files)} requested)")
    log_action(f"Processing {len(invoices)} invoices")

    # Load existing bills from cache (fast) or fallback to live Zoho fetch
    existing_bills = {}       # bill_number -> bill_id (exact)
    existing_bills_norm = {}  # normalized_bill_number -> bill_id
    existing_bill_ids = set()
    existing_vendor_date = set()  # (vendor_name_lower, date) pairs for secondary dedup

    # Also build vendor name -> vendor_id from cache
    cached_vendor_map = {}    # vendor_name_lower -> vendor_id

    try:
        if os.path.exists(BILLS_CACHE):
            with open(BILLS_CACHE, "r", encoding="utf-8") as f:
                cached_bills = json.load(f)
            for b in cached_bills:
                bn = b.get("bill_number", "")
                bid = b.get("bill_id", "")
                existing_bills[bn] = bid
                existing_bill_ids.add(bid)
                norm = _normalize_bill_number(bn)
                if norm:
                    existing_bills_norm[norm] = bid
                b_vendor = (b.get("vendor_name") or "").strip().lower()
                b_date = b.get("date", "")
                if b_vendor and b_date:
                    existing_vendor_date.add((b_vendor, b_date))
            log_action(f"Loaded {len(existing_bills)} bills from cache ({len(existing_vendor_date)} vendor+date pairs)")
        else:
            # Fallback: fetch from Zoho live
            log_action("No bills cache found — fetching from Zoho live...")
            page = 1
            while True:
                result = api.list_bills(page=page)
                bills_page = result.get("bills", [])
                if not bills_page:
                    break
                for b in bills_page:
                    bn = b.get("bill_number", "")
                    bid = b.get("bill_id", "")
                    existing_bills[bn] = bid
                    existing_bill_ids.add(bid)
                    norm = _normalize_bill_number(bn)
                    if norm:
                        existing_bills_norm[norm] = bid
                    b_vendor = (b.get("vendor_name") or "").strip().lower()
                    b_date = b.get("date", "")
                    if b_vendor and b_date:
                        existing_vendor_date.add((b_vendor, b_date))
                has_more = result.get("page_context", {}).get("has_more_page", False)
                if not has_more:
                    break
                page += 1
            log_action(f"Fetched {len(existing_bills)} bills from Zoho for dedup")

        # Load vendor cache
        cached_gstin_map = {}  # gstin -> (contact_id, contact_name)
        if os.path.exists(VENDORS_CACHE):
            with open(VENDORS_CACHE, "r", encoding="utf-8") as f:
                cached_vendors = json.load(f)
            for v in cached_vendors:
                cn = (v.get("contact_name") or "").strip().lower()
                vid = v.get("contact_id", "")
                if cn and vid:
                    cached_vendor_map[cn] = vid
                gst_no = (v.get("gst_no") or "").strip()
                if gst_no and vid:
                    cached_gstin_map[gst_no] = (vid, v.get("contact_name", ""))
            log_action(f"Loaded {len(cached_vendor_map)} vendors from cache")
    except Exception as e:
        log_action(f"Could not load bill/vendor data for dedup: {e}", "WARNING")

    # Load local results — trust "created" entries (Zoho bill-number dedup prevents duplicates)
    processed = {}
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            for entry in json.load(f):
                status = entry.get("status", "")
                if status == "created" and entry.get("bill_id"):
                    processed[entry["file"]] = entry
                # Discard stale "skipped"/"failed" entries — re-evaluate against current Zoho state

    results = list(processed.values())

    new_bill_ids = []
    skipped_count = 0
    failed_files = []

    for invoice in invoices:
        fname = invoice["file"]
        if fname in processed:
            log_action(f"Skipping (already processed): {fname}")
            skipped_count += 1
            continue

        # Dedup by bill number against Zoho (primary — uses extracted invoice_number)
        _GENERIC_NUMBERS = {"payment", "original", "invoice", "bill", "tax", "none", "n/a", ""}
        raw_inv_number = invoice.get("invoice_number", "")
        has_reliable_inv_number = bool(raw_inv_number and raw_inv_number.lower().strip() not in _GENERIC_NUMBERS)
        # GitHub receipt files use generic receipt numbers — fall back to filename
        if has_reliable_inv_number and fname.lower().startswith("github") and "receipt" in fname.lower():
            has_reliable_inv_number = False
        inv_number = raw_inv_number if has_reliable_inv_number else re.sub(r'\.(pdf|eml)$', '', fname, flags=re.IGNORECASE)
        bill_number = inv_number

        # 1. Exact bill number match
        if bill_number in existing_bills:
            log_action(f"Skipping (bill {bill_number} exists in Zoho): {fname}")
            results.append({"file": fname, "status": "skipped", "reason": f"bill {bill_number} exists"})
            skipped_count += 1
            continue

        # 2. Normalized bill number match (handles prefix/hyphen differences)
        if has_reliable_inv_number:
            norm = _normalize_bill_number(inv_number)
            if norm and norm in existing_bills_norm:
                log_action(f"Skipping (normalized match for {inv_number}): {fname}")
                results.append({"file": fname, "status": "skipped", "reason": f"normalized match: {inv_number}"})
                skipped_count += 1
                continue

        # Dedup by vendor name + bill date against Zoho (secondary fallback)
        # Only applies when invoice_number is not reliable (filename used as bill_number fallback).
        # If the invoice HAS a real invoice_number, two bills with the same vendor+date but
        # different invoice IDs are LEGITIMATE different bills — do not skip them.
        vendor_name = invoice.get("vendor_name")
        inv_date = invoice.get("date", "")
        if not has_reliable_inv_number and vendor_name and inv_date:
            # Check both raw vendor name and fuzzy-mapped name (e.g. "CLAUDE.AI SUBSCRIPTION" -> "Anthropic")
            names_to_check = {vendor_name.strip().lower()}
            mapped, _ = fuzzy_match_vendor(vendor_name, vendor_mappings)
            if mapped:
                names_to_check.add(mapped.strip().lower())
            vendor_date_dup = any((n, inv_date) in existing_vendor_date for n in names_to_check)
            if vendor_date_dup:
                matched = next(n for n in names_to_check if (n, inv_date) in existing_vendor_date)
                log_action(f"Skipping (vendor '{matched}' + date {inv_date} already has a bill in Zoho, no invoice# to distinguish): {fname}")
                results.append({"file": fname, "status": "skipped", "reason": "vendor+date match exists"})
                skipped_count += 1
                continue

        log_action(f"Processing: {fname}")

        # Step 1: Ensure vendor exists — try cache (GSTIN first, then name), then API
        inv_gstin = (invoice.get("vendor_gstin") or "").strip()
        # Ignore company's own GSTIN if mistakenly extracted as vendor's
        # Re-extract the real vendor GSTIN from invoice text
        if inv_gstin == _COMPANY_GSTIN:
            inv_gstin = ""
            raw_text = invoice.get("raw_text_preview", "")
            if raw_text:
                _gstin_re = re.compile(r'\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d][A-Z\d]')
                all_gstins = _gstin_re.findall(raw_text)
                real_vendor_gstins = [g for g in all_gstins if g != _COMPANY_GSTIN]
                if real_vendor_gstins:
                    inv_gstin = real_vendor_gstins[0]
                    log_action(f"  Re-extracted vendor GSTIN from invoice text: {inv_gstin}")
            invoice["vendor_gstin"] = inv_gstin
        cached_vid = None
        mapped_name = None

        # Priority 1: Match by GSTIN in Zoho vendor cache (most reliable)
        if inv_gstin and inv_gstin in cached_gstin_map:
            cached_vid, gstin_vname = cached_gstin_map[inv_gstin]
            log_action(f"  Vendor from cache (GSTIN {inv_gstin}): {gstin_vname} ({cached_vid})")
            vendor_name = gstin_vname

        # Priority 2: Match by name in Zoho vendor cache
        if not cached_vid and vendor_name:
            vn_lower = vendor_name.strip().lower()
            cached_vid = cached_vendor_map.get(vn_lower)
            if not cached_vid:
                mapped_name, _ = fuzzy_match_vendor(vendor_name, vendor_mappings)
                if mapped_name:
                    cached_vid = cached_vendor_map.get(mapped_name.strip().lower())

        if cached_vid:
            if not inv_gstin or inv_gstin not in cached_gstin_map:
                log_action(f"  Vendor from cache: {vendor_name} ({cached_vid})")
            vendor_id = cached_vid
            if mapped_name and not inv_gstin:
                vendor_name = mapped_name
            # Enrich invoice with vendor's real GSTIN from cache (for correct IGST/intrastate)
            if not inv_gstin:
                for gstin, (vid, _) in cached_gstin_map.items():
                    if vid == cached_vid:
                        invoice["vendor_gstin"] = gstin
                        log_action(f"  Enriched vendor GSTIN from cache: {gstin}")
                        break
        elif vendor_name:
            vendor_id, vendor_name = ensure_vendor(api, vendor_name, invoice, vendor_mappings, currency_map)
        else:
            vendor_id = None

        if not vendor_id:
            log_action(f"  No vendor found — skipping bill creation for: {fname}", "WARNING")
            results.append({"file": fname, "status": "skipped", "reason": "no vendor"})
            skipped_count += 1
            continue

        # Step 2: Create bill (with categorized expense account)
        bill_id, is_new = create_bill_for_invoice(
            api, invoice, vendor_id, expense_accounts, default_expense, currency_map,
            categorizer=categorizer, vendor_name=vendor_name,
            existing_bills=existing_bills, existing_bills_norm=existing_bills_norm,
            igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
            default_exemption_id=default_exemption_id,
        )
        if not bill_id:
            results.append({"file": fname, "status": "failed", "reason": "bill creation failed"})
            failed_files.append(fname)
            continue

        # Step 3: Attach PDF (prefer organized_path if available)
        pdf_path = invoice.get("organized_path") or invoice.get("path", os.path.join(PROJECT_ROOT, "input_pdfs", "invoices", fname))
        # Rebase stale absolute paths from a different project copy to current PROJECT_ROOT
        if not os.path.exists(pdf_path):
            for anchor in ("organized_invoices", "input_pdfs"):
                idx = pdf_path.replace("\\", "/").find(anchor)
                if idx != -1:
                    rebased = os.path.join(PROJECT_ROOT, pdf_path[idx:])
                    if os.path.exists(rebased):
                        pdf_path = rebased
                    break
        attached = attach_pdf(api, bill_id, pdf_path)

        bill_status = "created" if is_new else "attached"
        results.append({
            "file": fname,
            "status": bill_status,
            "vendor_name": vendor_name or f"[{fname}]",
            "vendor_id": vendor_id,
            "bill_id": bill_id,
            "amount": invoice.get("amount"),
            "currency": invoice.get("currency"),
            "attached": attached,
        })
        if is_new:
            new_bill_ids.append(bill_id)

        # Pace bulk API calls to stay under Zoho rate limits
        time.sleep(0.3)

    # Save results
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    newly_created = len(new_bill_ids)
    previously_created = sum(1 for r in results if r.get("status") == "created") - newly_created
    attached_only = sum(1 for r in results if r.get("status") == "attached")
    summary_parts = [f"{newly_created} newly created"]
    if previously_created > 0:
        summary_parts.append(f"{previously_created} previously created")
    if attached_only:
        summary_parts.append(f"{attached_only} existing (PDF attached)")
    if skipped_count:
        summary_parts.append(f"{skipped_count} skipped")
    log_action(f"Done. {', '.join(summary_parts)} of {len(invoices)} invoices. Results: {RESULTS_FILE}")

    return {
        "created_count": len(new_bill_ids),
        "skipped_count": skipped_count,
        "new_bill_ids": new_bill_ids,
        "failed_files": failed_files,
    }


# --- Main ---

def main():
    run()


if __name__ == "__main__":
    main()
