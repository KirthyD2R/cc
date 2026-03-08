"""
Step 2: Extract Invoice Details from PDFs

Reads downloaded invoice PDFs, extracts vendor name, amount, date,
currency, and invoice number using vendor-specific parsing patterns.
Skips Receipt PDFs (dedup — keeps Invoice, drops Receipt).
Outputs extracted_invoices.json.
"""

import os
import re
import json
import shutil
import pdfplumber
from email import policy
from email.parser import BytesParser
from datetime import datetime
from utils import PROJECT_ROOT, log_action, parse_date

INPUT_DIR = os.path.join(PROJECT_ROOT, "input_pdfs", "invoices")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")

# Company's own GSTIN — filtered out when extracting vendor GSTIN
_COMPANY_GSTIN = "33AAICD7217K1ZK"

# Lazy-loaded GSTIN lookup sources
_gstin_lookup_cache = None

def _resolve_vendor_by_gstin(gstin):
    """Resolve vendor name from GSTIN using Zoho vendor cache (primary) and gstin_map (fallback)."""
    global _gstin_lookup_cache
    if _gstin_lookup_cache is None:
        _gstin_lookup_cache = {}
        # Load gstin_map from vendor_mappings.json (fallback)
        vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
        if os.path.exists(vm_path):
            try:
                with open(vm_path, "r", encoding="utf-8") as f:
                    vm = json.load(f)
                for g, name in vm.get("gstin_map", {}).items():
                    _gstin_lookup_cache[g] = name
            except Exception:
                pass
        # Load from Zoho vendor cache (overrides gstin_map — source of truth)
        vc_path = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")
        if os.path.exists(vc_path):
            try:
                with open(vc_path, "r", encoding="utf-8") as f:
                    vendors = json.load(f)
                for v in vendors:
                    gst_no = (v.get("gst_no") or "").strip()
                    if gst_no:
                        _gstin_lookup_cache[gst_no] = v.get("contact_name", "")
            except Exception:
                pass

    vendor = _gstin_lookup_cache.get(gstin)
    if vendor:
        log_action(f"  Resolved vendor by GSTIN {gstin}: {vendor}")
    return vendor


def _update_gstin_map(invoices):
    """Auto-populate gstin_map in vendor_mappings.json from extracted invoices.

    Only adds GSTINs that have a single, consistent vendor name (skips ambiguous ones
    like the company's own GSTIN appearing on multiple vendors).
    """
    # Collect GSTIN → set of vendor names
    gstin_vendors = {}
    for inv in invoices:
        gstin = (inv.get("vendor_gstin") or "").strip()
        vendor = (inv.get("vendor_name") or "").strip()
        if gstin and vendor and gstin != _COMPANY_GSTIN:
            gstin_vendors.setdefault(gstin, set()).add(vendor)

    # Load existing vendor_mappings
    vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    if not os.path.exists(vm_path):
        return
    try:
        with open(vm_path, "r", encoding="utf-8") as f:
            vm = json.load(f)
    except Exception:
        return

    gstin_map = vm.get("gstin_map", {})
    added = 0
    for gstin, names in gstin_vendors.items():
        if gstin not in gstin_map and len(names) == 1:
            gstin_map[gstin] = next(iter(names))
            added += 1

    if added > 0:
        vm["gstin_map"] = gstin_map
        with open(vm_path, "w", encoding="utf-8") as f:
            json.dump(vm, f, indent=4, ensure_ascii=False)
        log_action(f"Added {added} new GSTIN mappings to vendor_mappings.json")
_GSTIN_RE = re.compile(r'\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d][A-Z\d]')


def _extract_vendor_gstin(text):
    """Extract the vendor's GSTIN from invoice text (not the company's own)."""
    if not text:
        return None
    # Standard Indian GSTIN format
    matches = _GSTIN_RE.findall(text)
    vendor_gstins = [g for g in matches if g != _COMPANY_GSTIN]
    if vendor_gstins:
        return vendor_gstins[0]
    # Fallback: labeled GSTIN (handles foreign registrations like Atlassian's 9917AUS29001OSF)
    m = re.search(r'GSTIN[:\s]+(\S+)', text, re.IGNORECASE)
    if m:
        gstin = m.group(1).rstrip('.,;')
        if gstin and gstin != _COMPANY_GSTIN:
            return gstin
    return None

# Invoice numbers that are too generic to use for dedup
GENERIC_NUMBERS = {"payment", "original", "invoice", "receipt", "bill", "tax", "none", "n/a", ""}


# --- Text Extraction ---

def extract_text(pdf_path):
    """Extract text from all pages of a PDF."""
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text += page_text + "\n"
    except Exception as e:
        log_action(f"pdfplumber failed for {pdf_path}: {e}", "WARNING")

    # OCR fallback if text is too short
    if len(text.strip()) < 50:
        try:
            import pytesseract
            from pdf2image import convert_from_path
            log_action(f"Using OCR fallback for {pdf_path}")
            images = convert_from_path(pdf_path)
            text = "\n".join(pytesseract.image_to_string(img) for img in images)
        except ImportError:
            log_action(
                "OCR fallback unavailable: install pytesseract and pdf2image "
                "(pip install pytesseract pdf2image) for scanned PDF support",
                "WARNING",
            )
        except Exception as e:
            log_action(f"OCR failed: {e}", "WARNING")

    # Some PDFs encode dashes as null bytes — normalize them
    text = text.replace('\x00', '-')
    return text


def _strip_html(html):
    """Strip HTML tags and entities, return clean text."""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&#?\w+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_text_from_eml(eml_path):
    """Extract text content from an EML (email) file.

    Parses MIME structure, collects all text parts.
    Returns the best text for invoice extraction — prefers text with
    receipt keywords (Order number, Total). Handles forwarded emails
    where receipt details may be in a nested HTML part.
    """
    try:
        with open(eml_path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception as e:
        log_action(f"Failed to parse EML {eml_path}: {e}", "WARNING")
        return ""

    # Collect all text parts
    plain_parts = []
    html_parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            content = payload.decode("utf-8", errors="replace") if payload else ""
        if not content:
            continue
        if ct == "text/plain":
            plain_parts.append(content)
        elif ct == "text/html":
            html_parts.append(content)

    _RECEIPT_KEYWORDS = ("order number", "total:")

    def _has_receipt_data(text):
        tl = text.lower()
        return any(kw in tl for kw in _RECEIPT_KEYWORDS)

    # 1. Try clean plain text parts (non-HTML)
    clean_plains = [pt for pt in plain_parts if "<html" not in pt.lower() and "<div" not in pt.lower()]
    for pt in clean_plains:
        if _has_receipt_data(pt):
            return pt

    # 2. Try HTML parts (strip tags) — forwarded emails often have receipt in HTML
    for ht in html_parts:
        stripped = _strip_html(ht)
        if _has_receipt_data(stripped):
            return stripped

    # 3. Try plain text parts that contain HTML markup (strip tags)
    html_plains = [pt for pt in plain_parts if "<html" in pt.lower() or "<div" in pt.lower()]
    for pt in html_plains:
        stripped = _strip_html(pt)
        if _has_receipt_data(stripped):
            return stripped

    # 4. Fallback: return whatever we have
    if clean_plains:
        return clean_plains[0]
    if html_parts:
        return _strip_html(html_parts[0])
    if plain_parts:
        return plain_parts[0]

    return ""


# --- Dedup: Skip Receipts ---

def is_receipt_file(filename):
    """Check if this is a receipt PDF (we prefer Invoice over Receipt).

    EML files are never skipped — they are the primary source for email-based invoices.
    """
    # EML receipts are primary source, don't skip
    if filename.lower().endswith('.eml'):
        return False
    name_lower = filename.lower()
    # Stripe-style receipts: "Receipt-XXXX-XXXX-XXXX.pdf"
    if name_lower.startswith("receipt-"):
        return True
    # GitHub receipts: "github-*-receipt-*.pdf"
    if "-receipt-" in name_lower:
        return True
    return False


# --- Vendor Detection ---

def detect_vendor(text):
    """Detect vendor from text content and return (vendor_name, vendor_key)."""
    text_upper = text[:2000].upper()

    # Amazon India marketplace invoices (check before AWS)
    if "TAX INVOICE/BILL OF SUPPLY" in text_upper and "AMAZON SELLER SERVICES" in text_upper:
        return "Amazon India"

    checks = [
        ("ATLASSIAN", "Atlassian"),
        ("AMAZON WEB SERVICES", "Amazon Web Services"),
        ("GITHUB, INC", "GitHub"),
        ("ANTHROPIC", "Anthropic"),
        ("VERCEL INC", "Vercel"),
        ("WISPR FLOW", "Wispr Flow"),
        ("GOOGLE PLAY", "Google Play"),
        ("GOOGLE INDIA PRIVATE LIMITED", "Google"),
        ("GOOGLE WORKSPACE", "Google"),
        ("GOOGLE CLOUD", "Google"),
        ("NEW RELIC", "New Relic"),
        ("ZOHO CORP", "Zoho"),
        ("INFO EDGE", "Info Edge (Naukri)"),
        ("NAUKRI", "Info Edge (Naukri)"),
        ("GAMMA", "Gamma"),
        ("MICROSOFT", "Microsoft"),
        ("NSTP", "NSTP"),
        ("GROQ", "Groq Inc"),
        ("S2 LABS", "S2 Labs Inc."),
        ("SUPABASE", "Supabase Pte. Ltd"),
        ("LINKEDIN", "LinkedIn"),
        ("HYPERBROWSER", "Hyperbrowser AI"),
        ("NETFLIX", "Netflix"),
        ("WINDSURF", "Windsurf"),
        ("CODEIUM", "Windsurf"),
        ("BLUE DART", "Blue Dart Express Ltd"),
        ("MEDIUM CORPORATION", "Medium"),
        ("FLIPKART", "Flipkart"),

    ]

    for keyword, vendor in checks:
        if keyword in text_upper:
            return vendor

    return None


def _is_stripe_format(text):
    """Detect Stripe-style invoices by their distinctive layout."""
    return bool(
        re.search(r"Invoice\s+number\s+[A-Z0-9]", text)
        and re.search(r"Date\s+of\s+issue\s+\w+\s+\d", text)
    )


# === Vendor-Specific Extractors ===

def extract_stripe_invoice(text):
    """Stripe-style invoices: Anthropic, Vercel, Wispr Flow, Gamma, etc.

    Pattern:
      Invoice number XXXXXXXX XXXX
      Date of issue Month DD, YYYY
      <Vendor Name>
      ...
      $XXX.XX USD due ...  OR  ₹X,XXX.XX due ...
      Amount due $XXX.XX USD
    """
    inv_number = None
    m = re.search(r"Invoice\s+number\s+([A-Z0-9]+[\s\-]+\d+)", text)
    if m:
        inv_number = m.group(1).replace(" ", "-")

    date = None
    m = re.search(r"Date\s+of\s+issue\s+(\w+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    # Amount: look for "Amount due $XXX.XX" or "$XXX.XX USD due" or "₹X,XXX.XX due"
    amount, currency = None, "USD"
    m = re.search(r"Amount\s+due\s+[\$₹]?\s*([\d,]+\.?\d*)\s*(USD)?", text)
    if m:
        amount = float(m.group(1).replace(",", ""))
        if "₹" in text[:3000]:
            currency = "INR"
    if not amount:
        m = re.search(r"[\$]([\d,]+\.?\d*)\s+USD\s+due", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"₹([\d,]+\.?\d*)\s+due", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
            currency = "INR"

    return inv_number, date, amount, currency


def extract_atlassian(text):
    """Atlassian invoices: Invoice number: IN-XXX, Invoice date: Mon DD, YYYY, Invoice Total: USD XX.XX"""
    inv_number = None
    m = re.search(r"Invoice\s+number:\s*(IN-[\d\-]+)", text)
    if m:
        inv_number = m.group(1)

    date = None
    m = re.search(r"Invoice\s+date:\s*(\w+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    amount = None
    m = re.search(r"(?:Invoice\s+Total|Total\s+billed\s+amount)\s*:?\s*USD\s*([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, "USD"


def extract_aws(text):
    """AWS statements: Statement Number, Statement Date, TOTAL AMOUNT DUE in INR.

    AWS India bills are payable in INR. The USD line is just the service charges total.
    The INR amount is what the CC is actually charged.
    Format: "TOTAL AMOUNT DUE BY <date> INR 300,039.59"
    """
    inv_number = None
    m = re.search(r"Statement\s+Number:\s*(\d+)", text)
    if m:
        inv_number = f"AWS-{m.group(1)}"

    date = None
    m = re.search(r"Statement\s+Date:\s*(\w+\s+\d{1,2}\s*,?\s*\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    # INR amount: "TOTAL AMOUNT DUE BY ... INR XX,XXX.XX" (actual payment amount)
    amount, currency = None, "INR"
    m = re.search(r"TOTAL\s+AMOUNT\s+DUE\s+BY\s+.*?INR\s+([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    # Fallback: INR total line "Total for this statement (1 USD = ... INR) ... INR XX,XXX.XX"
    if not amount:
        m = re.search(r"Total\s+for\s+this\s+statement\s*\(.*?INR\s*\)\s*\d*\s*INR\s+([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))

    # Fallback: USD if no INR found (non-India AWS billing)
    if not amount:
        m = re.search(r"Total\s+for\s+this\s+statement\s+in\s+USD\s+(?:USD\s+)?\$?([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
            currency = "USD"

    return inv_number, date, amount, currency


def extract_github_invoice(text):
    """GitHub invoices: Invoice # INVXXXXXXXXX, Invoice Date, INVOICE TOTAL"""
    inv_number = None
    m = re.search(r"Invoice\s*#\s*(INV\d+)", text)
    if m:
        inv_number = m.group(1)

    date = None
    m = re.search(r"Invoice\s+Date\s+(\w+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    amount = None
    m = re.search(r"INVOICE\s+TOTAL:\s*\$?([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, "USD"


def extract_github_receipt(text):
    """GitHub receipts: Date YYYY-MM-DD, Total $X.XX USD"""
    inv_number = None
    m = re.search(r"Transaction\s+ID\s+(\S+)", text)
    if m:
        inv_number = m.group(1)

    date = None
    m = re.search(r"Date\s+(\d{4}-\d{2}-\d{2})", text)
    if m:
        date = m.group(1)

    amount = None
    m = re.search(r"Total\s+\$?([\d,]+\.?\d*)\s*USD", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, "USD"


def extract_google(text):
    """Google invoices: Invoice number: XXXXXXXXXX, Invoice date DD Mon YYYY, Total in INR ₹X,XXX.XX"""
    inv_number = None
    m = re.search(r"Invoice\s+number:?\s*(\d{5,})", text)
    if m:
        inv_number = m.group(1)

    date = None
    m = re.search(r"Invoice\s+date\s+(\d{1,2}\s+\w+\s+\d{4})", text)
    if m:
        date = parse_date(m.group(1))
    if not date:
        # "Summary for 1 Jan 2026 - 31 Jan 2026" → use end date
        m = re.search(r"Summary\s+for\s+\d{1,2}\s+\w+\s+\d{4}\s*-\s*(\d{1,2}\s+\w+\s+\d{4})", text)
        if m:
            date = parse_date(m.group(1))

    # Issue #16: Try INR first, fall back to USD
    amount, currency = None, "INR"
    m = re.search(r"Total\s+in\s+INR\s+₹?([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"Total\s+in\s+USD\s+\$?([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
            currency = "USD"
    if not amount:
        m = re.search(r"Total\s+(?:amount\s+)?[\$₹]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, currency


def extract_google_play(text):
    """Google Play subscription receipts (from EML files).

    Pattern:
      Order number: SOP.3305-3916-7418-85612..11
      Order date: Dec 16, 2025 6:00:02 PM GMT+5:30
      Total: ₹1,950.00/month
    """
    inv_number = None
    m = re.search(r"Order\s+number:\s*(SOP[\.\d\-]+)", text)
    if m:
        inv_number = m.group(1).strip()

    date = None
    m = re.search(r"Order\s+date:\s*(\w+\s+\d{1,2},\s*\d{4})", text)
    if not m:
        # Forwarded email: "Date: Sat, Aug 16, 2025 at 6:00 PM"
        m = re.search(r"Date:\s*\w+,\s*(\w+\s+\d{1,2},\s*\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    amount, currency = None, "INR"
    # Total: ₹1,950.00/month or Total: ₹1,950.00 (handles extra spaces from HTML stripping)
    m = re.search(r"Total:\s*₹?\s*([\d,]+\.\d{2})", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, currency


def extract_new_relic(text):
    """New Relic: Invoice INV01XXXXXX, amount from Total Due"""
    inv_number = None
    m = re.search(r"(INV\d+)", text)
    if m:
        inv_number = m.group(1)

    date = None
    m = re.search(r"(?:Invoice\s+Date|Date)\s*:?\s*(\w+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        date = parse_date(m.group(1))
    if not date:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
        if m:
            date = parse_date(m.group(1))

    amount = None
    m = re.search(r"(?:Total\s+Due|Amount\s+Due|Total)\s*:?\s*\$?([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, "USD"


def extract_microsoft(text, filename):
    """Microsoft invoices: Payment Ref, Statement Date DD/MM/YYYY, Total Charges INR XX,XXX.XX"""
    # Invoice number = filename (which is the actual invoice number)
    inv_number = os.path.splitext(filename)[0]

    date = None
    m = re.search(r"Statement\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    amount, currency = None, "INR"
    m = re.search(r"Total\s+Charges\s+INR\s+([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"Total\s+Amount\s+INR\s+([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
    # Issue #16: Fall back to USD if no INR found
    if not amount:
        m = re.search(r"Total\s+(?:Charges|Amount)\s+USD\s+\$?([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
            currency = "USD"

    return inv_number, date, amount, currency


def extract_naukri(text, filename):
    """Naukri/Info Edge invoices: Document Date DD-Mon-YYYY, invoice number from filename."""
    # Invoice number from filename: NK09I1126006982.pdf → NK09I1126006982
    inv_number = os.path.splitext(filename)[0]

    date = None
    m = re.search(r"Document\s+Date\s+(\d{1,2}-\w{3}-\d{4})", text)
    if m:
        date = parse_date(m.group(1))

    amount, currency = None, "INR"
    m = re.search(r"Gross\s+Amount/Total\s+([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, currency


def extract_nstp(text, filename):
    """NSTP invoices: Invoice number from filename, date from Dt in filename."""
    inv_number = os.path.splitext(filename)[0]
    # Try to get invoice number from text
    m = re.search(r"NSTP/[\d\-]+/(\d+)", text)
    if m:
        inv_number = f"NSTP-{m.group(1)}"

    date = None
    # Try "Dt5-Jan-26" or similar from filename
    # Issue #18: Use %y format which handles 2-digit years properly
    m = re.search(r"Dt(\d{1,2})-(\w{3})-(\d{2})", filename)
    if m:
        date = parse_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", formats=["%d-%b-%y"])
    if not date:
        m = re.search(r"(?:Date|Dated?)\s*:?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", text)
        if m:
            date = parse_date(m.group(1))

    amount, currency = None, "INR"
    # "Total Invoice Value Including GST : ₹ 1,29,800.00"
    m = re.search(r"Total\s+Invoice\s+Value\s+Including\s+GST\s*[:\s]*(?:\(cid:\d+\))?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if m:
        amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"(?:Grand\s+Total|Total\s+Amount|Net\s+Amount)\s*[:\-]?\s*[\₹]?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(",", ""))

    return inv_number, date, amount, currency


def extract_amazon_india_page(page_text):
    """Extract invoice data from a single Amazon India invoice page.

    Amazon India PDFs may contain multiple invoices (one per page), each with
    a different seller, invoice number, and total. The correct total is the
    'Invoice Value' field (not the TOTAL row which shows tax totals).
    """
    inv_number = None
    m = re.search(r"Invoice\s+Number\s*:\s*([A-Za-z0-9\-]+)", page_text)
    if m:
        inv_number = m.group(1).strip()

    date = None
    m = re.search(r"Invoice\s+Date\s*:\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})", page_text)
    if m:
        date = parse_date(m.group(1))
    if not date:
        m = re.search(r"Order\s+Date\s*:\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})", page_text)
        if m:
            date = parse_date(m.group(1))

    # Invoice Value is the correct total (not the TOTAL row which is tax total)
    # pdfplumber may put "Invoice Value:" and the number on separate lines with junk between
    amount = None
    m = re.search(r"Invoice\s+Value\s*:?\s*[\n\s]*[\₹]?\s*([\d,]+\.?\d+)", page_text)
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            if val > 0:
                amount = val
        except ValueError:
            pass
    # Fallback: TOTAL row — last ₹ amount is grand total (first is tax total)
    # Format: "TOTAL: ₹73.68₹1,548.00" or "TOTAL: ₹20.44 ₹134.00"
    if not amount:
        amounts = re.findall(r"[\₹]([\d,]+\.?\d+)", page_text[page_text.find("TOTAL:"):] if "TOTAL:" in page_text else "")
        if len(amounts) >= 2:
            try:
                amount = float(amounts[-1].replace(",", ""))
            except ValueError:
                pass
        elif len(amounts) == 1:
            try:
                amount = float(amounts[0].replace(",", ""))
            except ValueError:
                pass

    # Vendor: "Sold By :" section — next line has the company name
    vendor = None
    m = re.search(r"Sold\s+By\s*:.*?\n(.+)", page_text)
    if m:
        # Line may contain both vendor + billing address separated by spaces
        name = m.group(1).strip()
        # Take text before buyer company name or "Billing" label
        for sep in ["D2R", "Billing"]:
            if sep in name:
                name = name[:name.index(sep)].strip()
        if name and "Amazon Seller Services" not in name:
            vendor = name
    # Fallback: "For <Company>:" signature line
    if not vendor:
        m = re.search(r"For\s+(.+?):", page_text)
        if m:
            name = m.group(1).strip()
            if name and "Authorized" not in name and "Recipient" not in name:
                vendor = name

    # Extract vendor GSTIN — find all GST Registration No entries and exclude company's own
    vendor_gstin = None
    gst_matches = re.findall(r"GST\s+Registration\s+No\s*:\s*(\d{2}[A-Z0-9]{13})", page_text)
    for g in gst_matches:
        if g != _COMPANY_GSTIN:
            vendor_gstin = g
            break

    # Extract Amazon seller entity codes (e.g., ASSPL, ARIPL) from footer disclaimer
    amazon_entities = {}
    entity_matches = re.findall(r"\*?([A-Z]{3,6})-([A-Za-z][A-Za-z .]+(?:Pvt|Private)[. ]+Ltd\.?)", page_text)
    for code, full_name in entity_matches:
        amazon_entities[code] = full_name.strip().rstrip(".,")

    # Extract fulfillment center from invoice number prefix (e.g., BLR7, MAA4, PNQ2)
    fc_code = None
    if inv_number:
        fc_m = re.match(r"^([A-Z]{2,4}\d?)-", inv_number)
        if fc_m:
            fc_code = fc_m.group(1)

    result = {
        "invoice_number": inv_number,
        "date": date,
        "amount": amount,
        "currency": "INR",
        "vendor_name": vendor,
        "vendor_gstin": vendor_gstin,
    }
    if amazon_entities:
        result["amazon_entities"] = amazon_entities
    if fc_code:
        result["amazon_fc_code"] = fc_code

    return result


def extract_amazon_india_multi(pdf_path, filename):
    """Extract multiple invoices from an Amazon India PDF (one per page)."""
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(page_text)
    except Exception as e:
        log_action(f"pdfplumber failed for {pdf_path}: {e}", "WARNING")
        return []

    if not pages:
        return []

    results = []
    for i, page_text in enumerate(pages):
        # Skip pages that aren't invoices (e.g. cover pages)
        if "Invoice Number" not in page_text and "Invoice Value" not in page_text:
            continue

        data = extract_amazon_india_page(page_text)
        inv_number = data["invoice_number"]
        # Use page-specific filename for multi-page PDFs
        if len(pages) > 1 and inv_number:
            display_file = f"{os.path.splitext(filename)[0]}_p{i+1}_{inv_number}.pdf"
        else:
            display_file = filename

        results.append({
            "file": display_file,
            "path": pdf_path,
            "vendor_name": data["vendor_name"],
            "invoice_number": inv_number,
            "date": data["date"] or datetime.now().strftime("%Y-%m-%d"),
            "amount": data["amount"],
            "currency": data["currency"],
            "raw_text_preview": page_text[:500],
            "vendor_gstin": data["vendor_gstin"],
        })

    return results

def extract_linkedin(text):
    """LinkedIn Singapore Pte Ltd invoices.

    Layout (pdfplumber extracts table headers and values on separate lines):
      Effective Date Transaction ID Invoice Number Purchaser Email
      6/9/2025 P441245346 511109459487 daniel.john@...
      Amount Transaction Date ...
      ₹8,400.00 6/8/2025 ...
    Footer: LinkedIn Singapore Pte Ltd, ... SG GST: 201109821G
    """
    inv_number = None
    # Invoice number is a long numeric string on the data row after headers
    m = re.search(r"Invoice\s+Number\s+Purchaser\s+Email\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})\s+\w+\s+(\d{6,})\s+", text)
    if m:
        inv_number = m.group(2)

    # Fallback: extract from filename LNKD_INVOICE_XXXXXXXXXXX
    if not inv_number:
        m2 = re.search(r"LNKD_INVOICE_(\d+)", text)
        if not m2:
            # Try from raw text — any 9+ digit number near "Invoice Number"
            m2 = re.search(r"Invoice\s+Number.*?(\d{9,})", text, re.DOTALL)
        if m2:
            inv_number = m2.group(1)

    # Date: "Effective Date" value (M/D/YYYY format)
    date = None
    m = re.search(r"Effective\s+Date.*?\n\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    if m:
        date = parse_date(m.group(1), formats=["%m/%d/%Y"])
    # Fallback: Transaction Date
    if not date:
        m = re.search(r"Transaction\s+Date.*?\n.*?(\d{1,2}/\d{1,2}/\d{4})", text)
        if not m:
            m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
        if m:
            date = parse_date(m.group(1), formats=["%m/%d/%Y"])

    # Amount: ₹X,XXX.XX or $X,XXX.XX
    amount, currency = None, "INR"
    m = re.search(r"Amount\s*\n\s*[\$₹]\s*([\d,]+\.?\d*)", text)
    if m:
        amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"₹\s*([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"\$\s*([\d,]+\.?\d*)", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
            currency = "USD"

    # Check for USD
    if re.search(r"\bUSD\b|\$", text[:500]):
        currency = "USD"

    return inv_number, date, amount, currency


def extract_medium(text):
    """Medium invoices: Invoice XXXX, Payment date: MM/DD/YY, Total paid $X.XX USD"""
    inv_number = None
    m = re.search(r"Invoice\s+([a-f0-9]{8,})", text)
    if m:
        inv_number = m.group(1)
 
    date = None
    m = re.search(r"Payment\s+date:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text)
    if m:
        date = parse_date(m.group(1), formats=["%m/%d/%y", "%m/%d/%Y"])
 
    amount = None
    m = re.search(r"Total\s+paid\s+\$?([\d,]+\.?\d*)\s*USD", text)
    if m:
        amount = float(m.group(1).replace(",", ""))
    if not amount:
        m = re.search(r"Total\s+\$?([\d,]+\.?\d*)\s*USD", text)
        if m:
            amount = float(m.group(1).replace(",", ""))
 
    return inv_number, date, amount, "USD"

def extract_generic(text):
    """Fallback: try common patterns."""
    inv_number = None
    for pat in [
        r"(?:Invoice|Inv|Bill)\s*(?:#|No|Number|Num)[\s.:]*([A-Za-z0-9\-/]+)",
        r"(?:Invoice|Inv)[\s:\-]+([A-Za-z0-9\-/]+)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            inv_number = m.group(1).strip()
            break

    date = None
    for pat in [
        r"(?:Invoice\s*date|Date\s+of\s+issue|Date)\s*[:\-]?\s*(\d{1,2}[/\.\-]\d{1,2}[/\.\-]\d{2,4})",
        r"(?:Invoice\s*date|Date\s+of\s+issue|Date)\s*[:\-]?\s*(\d{1,2}\s+\w{3,9}\s+\d{4})",
        r"(?:Invoice\s*date|Date\s+of\s+issue|Date)\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:Issue\s+Date)\s*[:\-]?\s*(\w+\s+\d{1,2},?\s+\d{4})",
        r"(\d{4}-\d{2}-\d{2})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            date = parse_date(m.group(1))
            if date:
                break

    amount, currency = None, "INR"
    if re.search(r"(?:USD|\$)", text):
        currency = "USD"
    # Issue #14: Prioritized amount patterns — most specific first, use FIRST match not last
    for pat in [
        r"Amount\s*Due\s*[:\-]?\s*[\$₹]?\s*([\d,]+\.?\d*)",
        r"Total\s*Due\s*[:\-]?\s*[\$₹]?\s*([\d,]+\.?\d*)",
        r"Grand\s*Total\s*[:\-]?\s*[\$₹]?\s*([\d,]+\.?\d*)",
        r"Total\s*(?:Amount|Payable)?\s*[:\-]?\s*[\$₹]?\s*([\d,]+\.?\d*)",
        r"[\$]([\d,]+\.?\d*)",
        r"₹\s*([\d,]+\.?\d*)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    amount = val
                    break
            except ValueError:
                continue

    return inv_number, date, amount, currency


# --- Main Extraction ---

def extract_invoice(pdf_path, filename):
    """Extract all fields from a single invoice PDF or EML using vendor-specific logic."""
    if filename.lower().endswith('.eml'):
        text = extract_text_from_eml(pdf_path)
    else:
        text = extract_text(pdf_path)
    if not text.strip():
        return None

    vendor = detect_vendor(text)

    # Skip Amazon order summaries (duplicates of tax invoices)
    if "Final Details for Order" in text and "Amazon.in order number" in text:
        log_action(f"  Skipping Amazon order summary: {filename} (use tax invoice instead)")
        return None

    # Amazon India: multi-page handling (returns list, not single dict)
    if vendor == "Amazon India":
        return extract_amazon_india_multi(pdf_path, filename)

    # Route to vendor-specific extractor
    if vendor == "Atlassian":
        inv_number, date, amount, currency = extract_atlassian(text)
    elif vendor == "Amazon Web Services":
        inv_number, date, amount, currency = extract_aws(text)
    elif vendor == "GitHub":
        if "Thanks for your purchase" in text:
            inv_number, date, amount, currency = extract_github_receipt(text)
        else:
            inv_number, date, amount, currency = extract_github_invoice(text)
    elif vendor == "Google Play":
        inv_number, date, amount, currency = extract_google_play(text)
    elif vendor == "Google":
        inv_number, date, amount, currency = extract_google(text)
    elif vendor == "New Relic":
        inv_number, date, amount, currency = extract_new_relic(text)
    elif _is_stripe_format(text):
        inv_number, date, amount, currency = extract_stripe_invoice(text)
    elif vendor == "Microsoft":
        inv_number, date, amount, currency = extract_microsoft(text, filename)
    elif vendor == "Info Edge (Naukri)":
        inv_number, date, amount, currency = extract_naukri(text, filename)
    elif vendor == "NSTP":
        inv_number, date, amount, currency = extract_nstp(text, filename)
    elif vendor == "LinkedIn":
        inv_number, date, amount, currency = extract_linkedin(text)
    elif vendor == "Medium":
        inv_number, date, amount, currency = extract_medium(text)
    else:
        inv_number, date, amount, currency = extract_generic(text)

    # Fallback vendor name — only if there's strong evidence (company suffix + address nearby)
    # Do NOT guess from random PDF lines — wrong vendors cause bad bills in Zoho
    if not vendor:
        vendor = _detect_vendor_fallback(text)

    vendor_gstin = _extract_vendor_gstin(text)

    # LinkedIn: extract SG GST from footer (e.g., "SG GST: 201109821G")
    if not vendor_gstin and vendor == "LinkedIn":
        m = re.search(r"SG\s+GST:\s*(\S+)", text)
        if m:
            vendor_gstin = m.group(1).rstrip(".,;")

    # GSTIN-based vendor resolution: match against gstin_map and Zoho vendor cache
    if not vendor and vendor_gstin:
        vendor = _resolve_vendor_by_gstin(vendor_gstin)

    return {
        "file": filename,
        "path": pdf_path,
        "vendor_name": vendor,
        "invoice_number": inv_number,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "amount": amount,
        "currency": currency,
        "raw_text_preview": text[:500],
        "vendor_gstin": vendor_gstin,
    }


def _detect_vendor_fallback(text):
    """Try to detect vendor name from unrecognized PDFs.

    Only returns a name if there's strong evidence it's a real company:
      - Line has a company suffix (Pvt Ltd, Inc, LLC, etc.)  AND
      - Nearby lines contain address/GSTIN indicators
    Returns None if unsure — Step 3 will use "Unidentified Vendor".
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Reject lines that are clearly not vendor names
    skip_words = {"invoice", "tax invoice", "bill", "receipt", "irn:", "date",
                  "page", "from:", "to:", "dear", "hi ", "hello", "subject:",
                  "order", "ref", "sr no", "sl no", "description", "item",
                  "qty", "amount", "total", "subtotal", "payment", "thank",
                  "inbox", "outlook", "gmail", "email", "sent", "received"}

    company_suffixes = (
        "pvt ltd", "pvt. ltd", "pvt. ltd.", "private limited",
        "limited", "ltd", "ltd.",
        "inc", "inc.", "llp", "llc", "corp", "corp.", "corporation",
        "technologies", "solutions", "services", "software", "systems",
        "enterprises", "labs", "consultants", "associates",
    )

    address_patterns = re.compile(
        r"\b(?:road|street|st\.|floor|plot|sector|phase|block|tower|"
        r"nagar|colony|lane|marg|avenue|ave|suite|building|"
        r"pin\s*code|\d{6}|india|bangalore|bengaluru|mumbai|delhi|chennai|"
        r"hyderabad|pune|kolkata|gurgaon|gurugram|noida|"
        r"gstin|gst\s*no|gst\s*:?\s*\d|pan\s*no|cin)\b",
        re.IGNORECASE,
    )

    # Labels that indicate the CUSTOMER/buyer section (not the seller/vendor)
    customer_labels = re.compile(
        r"^\s*(?:customer\s*address|bill\s*to|ship\s*to|consignee|buyer|"
        r"customer\s*name|deliver\s*to|shipped\s*to)\s*[:\-]?",
        re.IGNORECASE,
    )
    # Own company names — never detect self as vendor
    self_names = ("d2r ai", "d2b ai")

    in_customer_section = False

    for i, line in enumerate(lines[:15]):
        cleaned = line.strip().rstrip(".,")

        # Track customer-section context: lines after "Customer Address:" etc.
        # are about the buyer, not the vendor. Reset when a seller label appears.
        if customer_labels.match(cleaned):
            in_customer_section = True
        elif re.match(r"^\s*(?:billing\s*address|from|seller|supplier)\s*[:\-]?", cleaned, re.IGNORECASE):
            in_customer_section = False

        # Skip too short, too long, or numeric-only lines
        if len(cleaned) < 4 or len(cleaned) > 60:
            continue
        # Skip lines starting with date/time patterns (Outlook headers like "12/20/25, 5:16 PM")
        if re.match(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", cleaned):
            continue
        # Skip lines with known non-vendor words
        first_word = cleaned.lower().split(":")[0].strip().split()[0] if cleaned.split() else ""
        if any(cleaned.lower().startswith(s) for s in skip_words) or first_word in skip_words:
            continue
        # Skip garbled OCR text (repeated chars like "IImmppoorrtteerr")
        if re.search(r"(.)\1{2,}", cleaned):
            continue
        # Skip lines that look like "IRN" or pure numbers
        if cleaned.startswith("IRN") or re.match(r"^[\d\s\-/.,]+$", cleaned):
            continue
        # Skip lines containing "SAC:" (service classification codes, not vendor names)
        if "SAC:" in cleaned.upper() or "HSN:" in cleaned.upper():
            continue

        # MUST have a company suffix to be considered a vendor
        # Truncate at the company suffix to avoid grabbing customer names
        # e.g. "Groq, Inc. D2R AI LABS - Daniel John" → "Groq, Inc."
        truncated = None
        cleaned_lower = cleaned.lower()
        for s in company_suffixes:
            # Check "word Inc." or "word Pvt Ltd" etc.
            for pat_str in [f" {s}.", f" {s} ", f" {s}"]:
                pos = cleaned_lower.find(pat_str)
                if pos >= 0:
                    truncated = cleaned[:pos + len(pat_str)].strip().rstrip(".,")
                    break
            if truncated:
                break
            # Also check if line ends with suffix
            if cleaned_lower.endswith(s):
                truncated = cleaned
                break

        if not truncated:
            continue

        # Skip own company name — never detect self as vendor
        trunc_lower = truncated.lower()
        if any(s in trunc_lower for s in self_names):
            continue

        # Skip companies found in customer/buyer section
        if in_customer_section:
            continue

        # Strip label prefix if present (e.g. "Billing Address : Blue Dart Express Ltd")
        if ":" in truncated:
            after_colon = truncated.split(":", 1)[1].strip()
            if len(after_colon) >= 4:
                truncated = after_colon

        # MUST also have address/GSTIN evidence nearby
        nearby_text = " ".join(lines[i:i + 8]).lower()
        if address_patterns.search(nearby_text):
            return truncated

    return None


# --- Run (importable by run_loop.py) ---

def run(already_processed=None, force_all=False):
    """Extract invoice data from PDF files.

    In incremental mode (force_all=False), only processes PDFs not in
    already_processed set and not already in extracted_invoices.json.
    Appends new extractions to existing output.

    Args:
        already_processed: set of filenames already extracted. If None, checks
            existing extracted_invoices.json for already-extracted files.
        force_all: If True, re-extract all PDFs (overwrites output).

    Returns:
        dict: {
            "newly_processed": list[str],  # filenames processed this run
            "new_count": int,
            "total_count": int,
        }
    """
    log_action("=" * 50)
    log_action("Step 2: Extract Invoice Details from PDFs")
    log_action("=" * 50)

    if not os.path.isdir(INPUT_DIR):
        log_action(f"No input directory: {INPUT_DIR}", "ERROR")
        return {"newly_processed": [], "new_count": 0, "total_count": 0}

    all_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith((".pdf", ".eml"))]
    if not all_files:
        log_action("No invoice files found in input_pdfs/invoices/", "WARNING")
        return {"newly_processed": [], "new_count": 0, "total_count": 0}

    # Dedup: skip Receipt PDFs
    invoice_files = []
    skipped_receipts = 0
    for f in sorted(all_files):
        if is_receipt_file(f):
            skipped_receipts += 1
            log_action(f"  Skipping receipt: {f}")
        else:
            invoice_files.append(f)

    log_action(f"Found {len(all_files)} files (PDF+EML), skipped {skipped_receipts} receipts, processing {len(invoice_files)} invoices")

    # Incremental mode: load existing extractions and skip already-processed files
    existing_results = []
    existing_files = set()
    if not force_all and os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing_results = json.load(f)
        existing_files = {inv["file"] for inv in existing_results}

    if already_processed:
        existing_files.update(already_processed)

    results = []
    newly_processed = []
    for pdf_file in invoice_files:
        if not force_all and pdf_file in existing_files:
            log_action(f"  Skipping (already extracted): {pdf_file}")
            continue

        pdf_path = os.path.join(INPUT_DIR, pdf_file)
        log_action(f"  Extracting: {pdf_file}")

        invoice = extract_invoice(pdf_path, pdf_file)
        if invoice:
            # Amazon India returns a list of invoices (one per page)
            if isinstance(invoice, list):
                for inv in invoice:
                    results.append(inv)
                    log_action(f"    [{inv.get('invoice_number', '?')}] Vendor: {inv['vendor_name']}, Amount: {inv['amount']} {inv['currency']}, Date: {inv['date']}")
                newly_processed.append(pdf_file)
                log_action(f"    Amazon India: {len(invoice)} invoices from {pdf_file}")
            else:
                results.append(invoice)
                newly_processed.append(pdf_file)
                log_action(f"    Vendor: {invoice['vendor_name']}, Amount: {invoice['amount']} {invoice['currency']}, Date: {invoice['date']}")
        else:
            log_action(f"    Could not extract data from {pdf_file}", "WARNING")

    # Dedup by invoice_number: if same invoice number appears twice, keep first
    # Build seen_numbers from existing results first (for incremental dedup)
    seen_numbers = {}
    for inv in existing_results:
        num = inv.get("invoice_number")
        if num and num.lower().strip() not in GENERIC_NUMBERS:
            seen_numbers[num] = inv["file"]

    deduped_new = []
    for inv in results:
        num = inv.get("invoice_number")
        # Issue #23: Log when a generic number bypasses dedup
        if num and num.lower().strip() in GENERIC_NUMBERS:
            log_action(f"  Generic invoice number '{num}' for {inv['file']} — dedup bypassed", "WARNING")
        if num and num.lower().strip() not in GENERIC_NUMBERS and num in seen_numbers:
            log_action(f"  Dedup: skipping {inv['file']} (same invoice #{num} as {seen_numbers[num]})")
            continue
        if num and num.lower().strip() not in GENERIC_NUMBERS:
            seen_numbers[num] = inv["file"]
        deduped_new.append(inv)

    # Combine existing + new results
    combined = existing_results + deduped_new

    # Write output
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2, ensure_ascii=False)

    log_action(f"Done. {len(deduped_new)} new invoices extracted (total: {len(combined)}). Output: {OUTPUT_FILE}")

    # Auto-populate gstin_map in vendor_mappings.json with newly discovered GSTIN→vendor pairs
    _update_gstin_map(combined)

    # Organize PDFs into month-wise folders for easy browsing
    organized_count = organize_pdfs_by_month(combined)
    if organized_count > 0:
        log_action(f"Organized {organized_count} PDFs into month folders")
        # Re-save with organized_path fields added
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(combined, f, indent=2, ensure_ascii=False)

    return {
        "newly_processed": newly_processed,
        "new_count": len(deduped_new),
        "total_count": len(combined),
    }


def organize_pdfs_by_month(invoices):
    """Copy extracted invoice PDFs into organized_invoices/<Mon YYYY>/ folders.

    Copies (not moves) so the original path stays valid for Step 3 attachment.
    Skips files that already exist at destination (idempotent for re-runs).
    Deduplicates by invoice_number: if two files share the same invoice_number
    in the same month, only the first is copied (the (1) duplicate is skipped).

    Returns:
        int: number of files newly copied
    """
    organized_root = os.path.join(PROJECT_ROOT, "organized_invoices")
    copied = 0

    # Track (invoice_number, month_folder) -> first filename, to skip duplicates
    seen_inv_numbers = {}

    for inv in invoices:
        src_path = inv.get("path")
        date_str = inv.get("date")
        filename = inv.get("file")
        inv_number = inv.get("invoice_number", "")

        if not src_path or not os.path.exists(src_path):
            continue

        if not date_str:
            continue

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            month_folder = dt.strftime("%b %Y")  # e.g. "Apr 2025"
        except ValueError:
            continue

        # Dedup by invoice_number within the same month
        if inv_number and inv_number.lower().strip() not in GENERIC_NUMBERS:
            key = (inv_number, month_folder)
            if key in seen_inv_numbers:
                first_file = seen_inv_numbers[key]
                log_action(
                    f"  Organize dedup: skipping '{filename}' "
                    f"(same invoice #{inv_number} as '{first_file}' in {month_folder})"
                )
                continue
            seen_inv_numbers[key] = filename

        dest_dir = os.path.join(organized_root, month_folder)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)

        if os.path.exists(dest_path):
            inv["organized_path"] = dest_path
            continue

        try:
            shutil.copy2(src_path, dest_path)
            inv["organized_path"] = dest_path
            copied += 1
        except Exception as e:
            log_action(f"  Organize: failed to copy {filename}: {e}", "WARNING")

    return copied


# --- Main ---

def main():
    # Standalone mode: force re-extract all PDFs (original behavior)
    run(force_all=True)


if __name__ == "__main__":
    main()
