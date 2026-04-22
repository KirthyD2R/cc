"""
Step 4: Parse CC Statement PDFs → CSV + JSON

Parses credit card statement PDFs (HDFC, Kotak, IDFC FIRST/Mayura)
and exports transactions as CSV files for Zoho Banking import.

Each card's PDF is parsed using bank-specific logic.
Output: output/<card_name>_transactions.csv
"""

import os
import re
import csv
import json
import hashlib
import pdfplumber
from datetime import datetime
from utils import PROJECT_ROOT, load_config, ZohoBooksAPI, log_action, parse_date, format_amount, resolve_account_ids

INPUT_DIR = os.path.join(PROJECT_ROOT, "input_pdfs", "cc_statements")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")


def _file_md5(path):
    """Compute MD5 hash of a file for change detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_pdf(pdf_path, passwords=None):
    """Open a PDF, trying passwords if it's encrypted.

    Args:
        pdf_path: Path to the PDF file.
        passwords: list of passwords to try. Common CC statement passwords
            are the last 4 digits, DOB (DDMMYYYY), etc.

    Returns:
        pdfplumber PDF object (caller must close it).

    Raises:
        Exception if PDF cannot be opened with any password.
    """
    # Try without password first
    try:
        return pdfplumber.open(pdf_path)
    except Exception as e:
        # Check both the message and the exception class name for password errors
        err_text = (str(e) + type(e).__name__).lower()
        if "password" not in err_text:
            raise

    # PDF is password-protected — try each password
    attempts = list(passwords or [])
    for pw in attempts:
        pw_str = str(pw)
        try:
            return pdfplumber.open(pdf_path, password=pw_str)
        except Exception:
            continue

    raise RuntimeError(
        f"PDF is password-protected: {os.path.basename(pdf_path)}. "
        f"Tried {len(attempts)} password(s). "
        f"Add 'pdf_password' to the card config in zoho_config.json."
    )


# Supported forex currencies for extraction
_FOREX_CURRENCIES = r'(?:USD|EUR|GBP|AED|QAR|SGD|CAD|AUD|JPY|CHF|SAR|KWD|BHD|OMR)'


def _extract_forex(text):
    """Extract forex amount and currency from text.

    Handles formats like: (12.27 USD), USD 223.07, QAR 30.00
    Returns (amount_float, currency_str) or (None, None).
    """
    if not text:
        return None, None
    # Pattern 1: AMOUNT CURRENCY  e.g. "12.27 USD" or "(12.27 USD)"
    m = re.search(r'\(?\s*([\d,]+\.?\d*)\s+' + _FOREX_CURRENCIES + r'\s*\)?', text, re.IGNORECASE)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
            currency = re.search(_FOREX_CURRENCIES, text[m.start():], re.IGNORECASE).group(0).upper()
            return amount, currency
        except (ValueError, AttributeError):
            pass
    # Pattern 2: CURRENCY AMOUNT  e.g. "USD 223.07"
    m = re.search(_FOREX_CURRENCIES + r'\s+([\d,]+\.?\d*)', text, re.IGNORECASE)
    if m:
        try:
            currency = m.group(0).split()[0].upper()
            amount = float(m.group(1).replace(",", ""))
            return amount, currency
        except (ValueError, AttributeError):
            pass
    return None, None


# === HDFC CC Parser ===

def parse_hdfc(pdf_path, passwords=None):
    """Parse HDFC credit card statement.

    Format: DD/MM/YYYY| HH:MM DESCRIPTION [+] C AMOUNT l
    'C' = rupee symbol, '+' before C = credit, 'l' = PI indicator
    International lines include 'USD XX.XX' in description.
    """
    transactions = []
    with _open_pdf(pdf_path, passwords) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue

                # Issue #8: Time is optional — some rows (opening balance, rewards) omit HH:MM
                m = re.match(
                    r"(\d{2}/\d{2}/\d{4})\s*(?:\|\s*\d{2}:\d{2})?\s+(.+?)\s+(\+\s+)?C\s+([\d,]+\.\d{2})\s*",
                    line,
                )
                if m:
                    date_str, desc, is_credit, amount_str = m.groups()
                    date = parse_date(date_str)
                    if not date:
                        continue
                    amount = format_amount(amount_str)
                    if is_credit:
                        amount = -amount
                    # Clean up description: remove (Ref# ...) suffix
                    desc = re.sub(r'\s*\(Ref#[^)]*\)', '', desc).strip()
                    # Extract forex from description (e.g. "ATLASSIAN AMSTERDAM USD 223.07")
                    forex_amt, forex_cur = _extract_forex(desc)
                    txn = {"date": date, "description": desc, "amount": amount}
                    if forex_amt:
                        txn["forex_amount"] = forex_amt
                        txn["forex_currency"] = forex_cur
                    transactions.append(txn)

    return transactions


# === Kotak CC Parser ===

def parse_kotak(pdf_path, passwords=None):
    """Parse Kotak credit card statement."""
    transactions = []
    with _open_pdf(pdf_path, passwords) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                i += 1
                if not line:
                    continue

                # Pattern: DD MMM YYYY or DD-MM-YYYY Description Amount
                # Kotak uses: DD Mon YYYY format
                m = re.match(
                    r"(\d{1,2}\s+\w{3}\s+\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr)?$",
                    line, re.IGNORECASE,
                )
                if not m:
                    # Alternative: DD/MM/YYYY format
                    m = re.match(
                        r"(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr)?$",
                        line, re.IGNORECASE,
                    )
                if m:
                    date_str, desc, amount_str, cr_dr = m.groups()
                    date = parse_date(date_str)
                    if not date:
                        continue
                    amount = format_amount(amount_str)
                    if cr_dr and cr_dr.upper() == "CR":
                        amount = -amount
                    txn = {"date": date, "description": desc.strip(), "amount": amount}
                    # Peek at next line for forex amount like "(12.27 USD)"
                    if i < len(lines):
                        next_line = lines[i].strip()
                        forex_amt, forex_cur = _extract_forex(next_line)
                        if forex_amt:
                            txn["forex_amount"] = forex_amt
                            txn["forex_currency"] = forex_cur
                            i += 1  # Skip the forex line
                    transactions.append(txn)

    return transactions


# === IDFC FIRST / Mayura CC Parser ===

IDFC_SKIP_KEYWORDS = [
    "Statement Date", "Your Transactions", "Date Details",
    "Card Number:", "Page ", "Payment Modes", "Your Card Information",
    "Reward Points", "Important Information", "Insurance Details",
    "Schedule of Charges", "Grievance", "Discover This",
]

def parse_idfc_first(pdf_path, passwords=None):
    """Parse IDFC FIRST (Mayura) credit card statement.

    Format: DD/MM/YYYY DESCRIPTION [Convert] [USD XX.XX] AMOUNT DR|CR
    Some transactions have multi-line descriptions where the merchant name
    wraps across lines before/after the date+amount line.
    """
    transactions = []
    with _open_pdf(pdf_path, passwords) as pdf:
        all_lines = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.split("\n"))

        i = 0
        desc_before = []  # buffered description lines before a date line

        while i < len(all_lines):
            line = all_lines[i].strip()
            i += 1

            if not line:
                desc_before = []
                continue

            # Skip header/footer lines
            if any(kw in line for kw in IDFC_SKIP_KEYWORDS):
                desc_before = []
                continue

            # Try to match: DD/MM/YYYY [text] AMOUNT DR|CR
            m = re.match(
                r"(\d{2}/\d{2}/\d{4})\s+(.*?)\s*([\d,]+\.\d{2})\s+(DR|CR)\s*$",
                line, re.IGNORECASE,
            )

            if m:
                date_str = m.group(1)
                mid_text = m.group(2).strip()
                amount_str = m.group(3)
                dr_cr = m.group(4)

                # Build description from: lines before + middle text
                parts = list(desc_before)
                if mid_text:
                    parts.append(mid_text)

                # Only check for continuation line if this is a multi-line
                # transaction (has desc_before). Single-line transactions
                # (date + description + amount all on one line) don't wrap.
                if desc_before and i < len(all_lines):
                    next_line = all_lines[i].strip()
                    if (next_line
                            and not re.match(r"\d{2}/\d{2}/\d{4}", next_line)
                            and not any(kw in next_line for kw in IDFC_SKIP_KEYWORDS)):
                        parts.append(next_line)
                        i += 1

                desc = " ".join(parts).strip()
                # Extract forex BEFORE cleaning description
                forex_amt, forex_cur = _extract_forex(desc)
                # Clean: remove "Convert" tag and forex amounts from description
                desc = re.sub(r'\bConvert\b', '', desc)
                desc = re.sub(r'\b' + _FOREX_CURRENCIES + r'\s+[\d,.]+', '', desc)
                desc = re.sub(r'\s+', ' ', desc).strip()

                desc_before = []

                date = parse_date(date_str)
                if not date or not desc:
                    continue

                amount = format_amount(amount_str)
                if dr_cr.upper() == "CR":
                    amount = -amount

                txn = {"date": date, "description": desc, "amount": amount}
                if forex_amt:
                    txn["forex_amount"] = forex_amt
                    txn["forex_currency"] = forex_cur
                transactions.append(txn)
            else:
                # Not a date line - buffer as potential description for next transaction
                desc_before.append(line)

    return transactions


# === Amex CC Parser ===

_AMEX_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

_AMEX_SKIP = [
    "PAYMENT RECEIVED", "Total of new", "Total of other",
    "TOTAL OVERSEAS", "New domestic transactions", "New overseas transactions",
    "Card Number", "OTHER ACCOUNT TRANSACTIONS", "Statement of Account",
    "Prepared for", "Page ", "Payment Information", "Payment Methods",
    "Cardmember", "National Electronic", "Payment Advice", "Make a crossed",
    "amount due", "Mail payment to", "AMERICAN EXPRESS", "CYBER CITY",
    "SECTOR-", "GURGAON", "Incorporated", "Registered Trademark",
    "americanexpress", "Please pay by",
    "Statement includes", "Thank you for using", "Previous Balance",
    "Details Foreign Spending", "Drop Boxes", "Discover This",
    "UPI (Unified", "AEBC3", "direct debit", "your Bank account",
    "total amount", "RAMESH MAHESH", "R K MUTT", "R A PURAM",
    "CHENNAI TN", "DLF", "For any queries",
]

# Transaction line: Month Day DESCRIPTION [FOREX_AMT] INR_AMT [CR]
_AMEX_TXN_RE = re.compile(
    r'^(' + '|'.join(_AMEX_MONTHS.keys()) + r')\s+(\d{1,2})\s+'
    r'(.+?)\s+'
    r'([\d,]*\.\d{2})\s*'         # amount1 (INR for domestic, forex for overseas; handles .64)
    r'(?:([\d,]+\.\d{2})\s*)?'    # amount2 (INR for overseas, absent for domestic)
    r'(CR)?\s*$',
    re.IGNORECASE,
)

# Overseas currency line (follows overseas transaction)
_AMEX_CURRENCY_RE = re.compile(
    r'^\s*(CR)?\s*(' + _FOREX_CURRENCIES + r'|UNITED STATES DOLLAR|EURO|BRITISH POUND'
    r'|UAE DIRHAM|SINGAPORE DOLLAR|CANADIAN DOLLAR|AUSTRALIAN DOLLAR'
    r'|JAPANESE YEN|SWISS FRANC|SAUDI RIYAL|QATARI RIYAL)\s*$',
    re.IGNORECASE,
)

_AMEX_CURRENCY_MAP = {
    'UNITED STATES DOLLAR': 'USD', 'EURO': 'EUR', 'BRITISH POUND': 'GBP',
    'UAE DIRHAM': 'AED', 'SINGAPORE DOLLAR': 'SGD', 'CANADIAN DOLLAR': 'CAD',
    'AUSTRALIAN DOLLAR': 'AUD', 'JAPANESE YEN': 'JPY', 'SWISS FRANC': 'CHF',
    'SAUDI RIYAL': 'SAR', 'QATARI RIYAL': 'QAR',
}


def parse_amex(pdf_path, passwords=None):
    """Parse American Express credit card statement.

    Format:
      Domestic: Month Day DESCRIPTION AMOUNT [CR]
      Overseas: Month Day DESCRIPTION FOREX_AMT INR_AMT [CR]
                CURRENCY_NAME (next line)
    Statement date on page 1 gives year context for transaction dates.
    """
    transactions = []
    stmt_month = None
    stmt_year = None

    with _open_pdf(pdf_path, passwords) as pdf:
        all_lines = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.split("\n"))

        # Extract statement date from header: "XXXX-XXXXXX-NNNNN DD/MM/YYYY"
        for line in all_lines[:30]:
            m = re.search(r'XXXX-XXXXXX-\d+\s+(\d{2})/(\d{2})/(\d{4})', line)
            if m:
                stmt_month = int(m.group(2))
                stmt_year = int(m.group(3))
                break

        if not stmt_year:
            # Fallback: try to find date from "Prepared for ... Date" line
            stmt_year = datetime.now().year
            stmt_month = datetime.now().month

        def _resolve_year(month_num):
            """Determine year: if txn month > statement month, it's from previous year."""
            if month_num > stmt_month:
                return stmt_year - 1
            return stmt_year

        i = 0
        while i < len(all_lines):
            line = all_lines[i].strip()
            i += 1

            if not line:
                continue

            # Skip header/footer/summary lines
            if any(kw in line for kw in _AMEX_SKIP):
                continue

            # Skip standalone CR line (handled by previous txn or next check)
            if line.strip() == 'CR':
                continue

            # Try transaction match
            m = _AMEX_TXN_RE.match(line)
            if not m:
                continue

            month_name = m.group(1).lower()
            day = int(m.group(2))
            desc = m.group(3).strip()
            amount1_str = m.group(4)
            amount2_str = m.group(5)  # None for domestic
            is_credit = bool(m.group(6))

            month_num = _AMEX_MONTHS[month_name]
            year = _resolve_year(month_num)
            date_str = f"{year}-{month_num:02d}-{day:02d}"

            amount1 = float(amount1_str.replace(",", ""))
            amount2 = float(amount2_str.replace(",", "")) if amount2_str else None

            # Check next line for CR or currency
            next_is_cr = False
            forex_amt = None
            forex_cur = None

            if i < len(all_lines):
                next_line = all_lines[i].strip()

                # Check for standalone CR on next line
                if next_line == 'CR':
                    is_credit = True
                    i += 1
                    # Check line after CR for currency
                    if i < len(all_lines):
                        next_line = all_lines[i].strip()

                # Check for currency line (overseas transaction)
                cur_m = _AMEX_CURRENCY_RE.match(next_line)
                if cur_m:
                    if cur_m.group(1):  # CR before currency name
                        is_credit = True
                    cur_name = cur_m.group(2).upper()
                    forex_cur = _AMEX_CURRENCY_MAP.get(cur_name, cur_name[:3])
                    i += 1

            if forex_cur and amount2 is not None:
                # Overseas: amount1=forex, amount2=INR
                forex_amt = amount1
                inr_amount = amount2
            elif amount2 is not None:
                # Two amounts but no currency detected — treat amount2 as INR
                forex_amt = amount1
                inr_amount = amount2
            else:
                # Domestic: amount1=INR
                inr_amount = amount1

            if is_credit:
                inr_amount = -inr_amount

            txn = {"date": date_str, "description": desc, "amount": inr_amount}
            if forex_amt and forex_cur:
                txn["forex_amount"] = forex_amt
                txn["forex_currency"] = forex_cur
            transactions.append(txn)

    return transactions


# === Table-based Extraction Fallback ===

def parse_tables(pdf_path, passwords=None):
    """Fallback: try extracting tabular data from PDF."""
    transactions = []
    with _open_pdf(pdf_path, passwords) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 3:
                        continue
                    # Try to identify date, description, amount columns
                    cells = [str(c).strip() if c else "" for c in row]
                    date = None
                    for cell in cells:
                        date = parse_date(cell)
                        if date:
                            break
                    if not date:
                        continue

                    # Find amount (last numeric cell)
                    amount = None
                    for cell in reversed(cells):
                        try:
                            amount = format_amount(cell)
                            break
                        except (ValueError, AttributeError):
                            continue

                    if amount is None:
                        continue

                    # Description: middle cells joined
                    desc_parts = [c for c in cells if c and parse_date(c) is None]
                    try:
                        desc_parts = [c for c in desc_parts if format_amount(c) != amount]
                    except (ValueError, AttributeError):
                        pass
                    desc = " ".join(desc_parts).strip()

                    if desc:
                        transactions.append({
                            "date": date,
                            "description": desc,
                            "amount": amount,
                        })

    return transactions


# === CSV Export ===

def write_csv(transactions, output_path):
    """Write transactions to CSV. All amounts written as positive (absolute
    value); forex lives in its own column, never duplicated in the description.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "description", "amount", "forex_ref"])
        writer.writeheader()
        for t in transactions:
            forex_ref = ""
            if t.get("forex_amount"):
                forex_ref = f'{t["forex_currency"]} {t["forex_amount"]:.2f}'

            # Strip any "USD 200.00" / "(12.27 USD)" tail the parser left in the
            # raw description — forex_ref column already carries that info.
            desc = t["description"]
            if forex_ref:
                desc = re.sub(
                    r'\s*\(?\s*' + _FOREX_CURRENCIES + r'\s+[\d,]+\.?\d*\s*\)?',
                    '',
                    desc,
                    flags=re.IGNORECASE,
                ).strip()
                desc = re.sub(
                    r'\s*\(?\s*[\d,]+\.?\d*\s+' + _FOREX_CURRENCIES + r'\s*\)?',
                    '',
                    desc,
                    flags=re.IGNORECASE,
                ).strip()

            writer.writerow({
                "date": t["date"],
                "description": desc,
                "amount": abs(t["amount"]),
                "forex_ref": forex_ref,
            })
    log_action(f"  Written {len(transactions)} transactions to {output_path}")


# === Main ===

PARSERS = {
    "HDFC": parse_hdfc,
    "Kotak": parse_kotak,
    "IDFC FIRST": parse_idfc_first,
    "Amex": parse_amex,
}


def run(known_hashes=None, selected_files=None, pdf_password=None):
    """Parse CC statement PDFs into CSV + JSON.

    Args:
        known_hashes: dict of {card_name: md5_hex}. If a card's PDF hash
            matches, that card is skipped (no re-parsing needed).
        selected_files: list of filenames (basenames) to restrict parsing to.
            When provided, only PDFs matching these filenames are parsed.
            When None, all discovered PDFs are parsed (full scan).
        pdf_password: optional password for encrypted PDFs (from UI input).

    Returns:
        dict: {
            "has_new_data": bool,
            "new_hashes": dict,            # {card_name: md5_hex}
            "total_transactions": int,
            "cards_parsed": list[str],
        }
    """
    log_action("=" * 50)
    log_action("Step 4: Parse CC Statement PDFs -> CSV + JSON")
    if selected_files:
        log_action(f"  Selected files mode: parsing only {len(selected_files)} file(s)")
        for sf in selected_files:
            log_action(f"    - {sf}")
    log_action("=" * 50)

    config = load_config()
    api = ZohoBooksAPI(config)
    cards = config.get("credit_cards", [])

    if not cards:
        log_action("No credit cards configured in zoho_config.json", "ERROR")
        return {"has_new_data": False, "new_hashes": known_hashes or {}, "total_transactions": 0, "cards_parsed": []}

    resolve_account_ids(api, cards)

    if known_hashes is None:
        known_hashes = {}

    all_transactions = []  # Combined for JSON output
    new_hashes = dict(known_hashes)
    cards_parsed = []
    has_new_data = False
    password_failed_files = []  # Track PDFs that failed due to password

    for card in cards:
        name = card["name"]
        bank = card["bank"]
        if not name and not bank:
            continue
        pdf_file = card.get("pdf_file")
        account_id = card.get("zoho_account_id")

        # Discover ALL PDFs for this card (configured + pattern matches)
        import glob
        # Support multiple glob patterns via pdf_patterns list (falls back to pdf_pattern or bank*)
        patterns = card.get("pdf_patterns", [])
        if not patterns:
            patterns = [card.get("pdf_pattern", f"{bank}*")]
        pattern_matches = []
        for pattern in patterns:
            if not pattern.lower().endswith(".pdf"):
                pattern += ".pdf"
            pattern_matches.extend(glob.glob(os.path.join(INPUT_DIR, pattern)))
        # Also try broader matching: card name variations and fuzzy match
        # to catch typos in filenames (e.g. "Kotack" instead of "Kotak")
        bank_lower = bank.lower()
        # Extract first word of card name as alias (e.g. "Mayura" from "Mayura CC 9677")
        card_name_parts = name.lower().split()
        card_alias = card_name_parts[0] if card_name_parts else ""
        all_pdfs_in_dir = glob.glob(os.path.join(INPUT_DIR, "*.pdf"))
        for p in all_pdfs_in_dir:
            if p in pattern_matches:
                continue
            bname = os.path.basename(p).lower()
            # Exact substring match on bank name
            if bank_lower in bname:
                pattern_matches.append(p)
                continue
            # Exact substring match on card alias (e.g. "mayura" in "mayura_july 2025.pdf")
            if card_alias and len(card_alias) >= 3 and card_alias in bname:
                pattern_matches.append(p)
                continue
            # Fuzzy match: allow 1-2 char typos (e.g. "kotack" ≈ "kotak")
            # Check if filename starts with something close to bank name or card alias
            for check_name in [bank_lower, card_alias]:
                if len(check_name) < 3:
                    continue
                prefix = bname[:len(check_name) + 2]  # allow up to 2 extra chars
                common = sum(1 for a, b in zip(check_name, prefix) if a == b)
                if common >= len(check_name) - 1:
                    pattern_matches.append(p)
                    break

        # Add the configured pdf_file if it exists and isn't already matched
        if pdf_file:
            configured_path = os.path.join(INPUT_DIR, pdf_file)
            if os.path.exists(configured_path) and configured_path not in pattern_matches:
                pattern_matches.append(configured_path)

        # Disambiguate by last_four_digits when multiple cards share the same
        # bank / pdf_patterns. Without this, one PDF matches every card of the
        # same bank, so txns from one cardholder leak onto another card.
        my_last_four = card.get("last_four_digits")
        other_last_fours = {
            c.get("last_four_digits") for c in cards
            if c.get("last_four_digits") and c.get("last_four_digits") != my_last_four
        }
        if other_last_fours:
            disambiguated = []
            for p in pattern_matches:
                bname = os.path.basename(p)
                if my_last_four and my_last_four in bname:
                    disambiguated.append(p)  # explicit match for this card
                    continue
                if any(lf in bname for lf in other_last_fours):
                    continue  # filename belongs to a different card
                disambiguated.append(p)  # ambiguous — keep (legacy fallback)
            pattern_matches = disambiguated

        # Sort by filename for consistent ordering
        pdf_paths = sorted(set(pattern_matches))

        # Filter to selected_files if provided
        if selected_files is not None:
            selected_set = {s.lower() for s in selected_files}
            pdf_paths = [p for p in pdf_paths if os.path.basename(p).lower() in selected_set]
            if not pdf_paths:
                # No selected files match this card — skip silently
                continue

        if not pdf_paths:
            log_action(f"No PDF files found for {name} (pattern: {pattern})", "WARNING")
            continue

        log_action(f"Found {len(pdf_paths)} PDF(s) for {name}")

        # Compute combined hash of all PDFs for change detection
        combined_hash = hashlib.md5()
        for p in pdf_paths:
            combined_hash.update(_file_md5(p).encode())
        file_hash = combined_hash.hexdigest()
        new_hashes[name] = file_hash

        if known_hashes.get(name) == file_hash and selected_files is None:
            log_action(f"  Skipping {name} — all PDFs unchanged since last run")
            continue

        has_new_data = True

        # Select parser by bank
        parser = PARSERS.get(bank)
        if not parser:
            log_action(f"  No parser registered for bank '{bank}'. Add one to PARSERS dict.", "ERROR")

        # Build password list for encrypted PDFs
        passwords = []
        if pdf_password:
            passwords.append(pdf_password)  # UI-provided password first
        if card.get("pdf_password"):
            passwords.append(card["pdf_password"])
        if card.get("last_four_digits"):
            passwords.append(card["last_four_digits"])
        # Common patterns: card name, bank name
        passwords.extend([name, bank, ""])

        # Parse ALL PDFs for this card
        card_transactions = []
        for pdf_path in pdf_paths:
            pdf_basename = os.path.basename(pdf_path)
            log_action(f"  Parsing: {pdf_basename}")

            transactions = []
            pw_fail = False
            if parser:
                try:
                    transactions = parser(pdf_path, passwords=passwords)
                except RuntimeError as e:
                    if "password-protected" in str(e):
                        pw_fail = True
                    log_action(f"    {e}", "ERROR")
                if not transactions and not pw_fail:
                    if parser:
                        log_action(f"    {bank} parser returned 0 transactions — trying table fallback", "WARNING")

            # Fallback to table extraction if regex found nothing (skip if password issue)
            if not transactions and not pw_fail:
                try:
                    transactions = parse_tables(pdf_path, passwords=passwords)
                except RuntimeError as e:
                    if "password-protected" in str(e):
                        pw_fail = True
                    log_action(f"    {e}", "ERROR")

            if pw_fail:
                password_failed_files.append(pdf_basename)
                continue

            if not transactions:
                log_action(f"    No transactions found in {pdf_basename}", "WARNING")
                continue

            log_action(f"    Found {len(transactions)} transactions")
            card_transactions.extend(transactions)

        if not card_transactions:
            log_action(f"  No transactions found across all PDFs for {name}", "ERROR")
            continue

        # Deduplicate across all PDFs
        seen = set()
        unique = []
        for t in card_transactions:
            key = (t["date"], t["description"], t["amount"])
            if key not in seen:
                seen.add(key)
                unique.append(t)

        log_action(f"  Total: {len(unique)} unique transactions from {len(pdf_paths)} PDF(s)")
        cards_parsed.append(name)

        # Add card info to each transaction for JSON. Keep credits so the JSON
        # is a faithful record of the statement; the payment matcher filters
        # them out itself (see scripts/05_record_payments.py).
        for t in unique:
            entry = {
                "date": t["date"],
                "description": t["description"],
                "amount": t["amount"],
                "card_name": name,
                "zoho_account_id": account_id,
            }
            if t.get("forex_amount"):
                entry["forex_amount"] = t["forex_amount"]
                entry["forex_currency"] = t["forex_currency"]
            all_transactions.append(entry)

        # Export CSV
        safe_name = name.replace(" ", "_")
        csv_path = os.path.join(OUTPUT_DIR, f"{safe_name}_transactions.csv")
        write_csv(unique, csv_path)

    # Save combined JSON for payment script to use
    # When parsing selected files (single card), merge with existing data
    # instead of overwriting — so other cards' transactions are preserved
    json_path = os.path.join(OUTPUT_DIR, "cc_transactions.json")
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    if selected_files and os.path.exists(json_path) and os.path.getsize(json_path) > 0:
        # Merge mode: keep transactions from cards we didn't re-parse
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []
        # Remove old entries for cards we just re-parsed
        reparsed_cards = set(cards_parsed)
        kept = [t for t in existing if t.get("card_name") not in reparsed_cards]
        merged = kept + all_transactions
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        log_action(f"Merged: kept {len(kept)} from other cards + {len(all_transactions)} new = {len(merged)} total in {json_path}")
    else:
        # Full scan mode: overwrite entirely
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(all_transactions, f, indent=2, ensure_ascii=False)
        log_action(f"Saved {len(all_transactions)} transactions to {json_path}")

    log_action("Done. CC statement CSVs + JSON ready.")

    result = {
        "has_new_data": has_new_data,
        "new_hashes": new_hashes,
        "total_transactions": len(all_transactions),
        "cards_parsed": cards_parsed,
    }
    if password_failed_files:
        result["password_failed"] = True
        result["password_failed_files"] = password_failed_files
    return result


# --- Main ---

def main():
    run(known_hashes=None)


if __name__ == "__main__":
    main()
