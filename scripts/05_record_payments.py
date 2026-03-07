"""
Step 5: Record Vendor Payments (Close Bills as Paid)

For each created bill:
  1. Look up actual INR amount from CC statement (parsed in Step 4)
  2. For USD bills: calculate exact exchange rate from CC INR / bill USD
  3. Record vendor payment linking bill to the correct CC account
  4. Bills become PAID
"""

import os
import re
import json
import time
from datetime import datetime, timedelta
from utils import (
    PROJECT_ROOT, load_config, load_vendor_mappings,
    ZohoBooksAPI, log_action, resolve_account_ids,
)

BILLS_FILE = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
CC_TXNS_FILE = os.path.join(PROJECT_ROOT, "output", "cc_transactions.json")
PAYMENTS_FILE = os.path.join(PROJECT_ROOT, "output", "recorded_payments.json")


def load_created_bills():
    with open(BILLS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cc_transactions():
    if not os.path.exists(CC_TXNS_FILE):
        log_action("CC transactions JSON not found. Run Step 4 first.", "WARNING")
        return []
    with open(CC_TXNS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def build_vendor_to_merchants(vendor_mappings):
    """Reverse vendor_mappings: vendor_name -> list of CC merchant keywords."""
    reverse = {}
    mappings = vendor_mappings.get("mappings", {})
    for merchant, vendor in mappings.items():
        reverse.setdefault(vendor.lower(), []).append(merchant.lower())

    # Add vendor aliases so "Anthropic" also picks up "Anthropic (Claude AI)" keywords
    aliases = {
        "anthropic": ["anthropic (claude ai)"],
        "anthropic (claude ai)": ["anthropic"],
    }
    for name, alias_list in aliases.items():
        for alias in alias_list:
            if alias in reverse:
                reverse.setdefault(name, []).extend(reverse[alias])

    return reverse


def _normalize(s):
    """Strip spaces, dots, special chars for fuzzy comparison."""
    return re.sub(r'[\s.\-,*()]+', '', s.lower())


def _extract_forex_from_desc(description, currency):
    """Extract foreign currency amount from CC description like 'ATLASSIAN USD 223.07' or 'VENDOR QAR 22.00'."""
    m = re.search(rf'{re.escape(currency)}\s+([\d,]+\.?\d*)', description, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _extract_forex_from_zoho_desc(description):
    """Extract forex from '[USD 359.90]' appended to Zoho banking descriptions."""
    m = re.search(r'\[([A-Z]{3})\s+([\d,.]+)\]', description or "")
    if m:
        try:
            return float(m.group(2).replace(',', '')), m.group(1)
        except ValueError:
            pass
    return None, None


def fetch_cc_transactions_from_zoho(api, cards):
    """Fetch all CC banking transactions from Zoho (all pages, all cards)."""
    all_txns = []
    for card in cards:
        account_id = card.get("zoho_account_id")
        if not account_id:
            continue
        page = 1
        while True:
            result = api.list_uncategorized(account_id, page=page)
            for t in result.get("banktransactions", []):
                # Zoho Banking API returns 'amount' (positive = debit/charge, negative = credit/refund)
                amount = float(t.get("amount") or 0)
                desc = t.get("description", "") or t.get("payee", "")
                forex_amt, forex_cur = _extract_forex_from_zoho_desc(desc)
                entry = {
                    "transaction_id": t.get("transaction_id", ""),
                    "date": t.get("date", ""),
                    "description": desc,
                    "amount": amount,
                    "card_name": card["name"],
                    "zoho_account_id": account_id,
                }
                if forex_amt:
                    entry["forex_amount"] = forex_amt
                    entry["forex_currency"] = forex_cur
                    log_action(f"  [forex] {t.get('date','')} | {desc[:50]} | INR {amount} | {forex_cur} {forex_amt}")
                all_txns.append(entry)
            if not result.get("page_context", {}).get("has_more_page", False):
                break
            page += 1
    forex_count = sum(1 for t in all_txns if t.get("forex_amount"))
    log_action(f"Fetched {len(all_txns)} CC transactions from Zoho Banking ({forex_count} with forex)")
    return all_txns


def fetch_unpaid_bills_from_zoho(api):
    """Fetch all unpaid and overdue bills from Zoho (paginated), deduplicated."""
    all_bills = []
    for status in ("unpaid", "overdue"):
        page = 1
        while True:
            result = api.list_bills(status=status, page=page)
            bills = result.get("bills", [])
            all_bills.extend(bills)
            if not result.get("page_context", {}).get("has_more_page", False):
                break
            page += 1
    # Deduplicate by bill_id
    seen = set()
    unique = []
    for b in all_bills:
        bid = b.get("bill_id")
        if bid and bid not in seen:
            seen.add(bid)
            unique.append(b)
    log_action(f"Fetched {len(unique)} unpaid/overdue bills from Zoho")
    return unique


def _match_vendor_keywords(desc_lower, desc_norm, merchant_keywords):
    """Check if any merchant keyword matches the CC description.

    Uses both normal substring match and spaceless/normalized match
    to handle CC descriptions like 'AMAZONWEBSERVICESC' or 'CLAUDE.AISUBSCRIPTION'.
    """
    for keyword in merchant_keywords:
        # Normal substring match
        if keyword in desc_lower:
            return True
        # Starts-with match (first 10 chars)
        if desc_lower.startswith(keyword[:10]):
            return True
        # Normalized (spaceless) match for concatenated CC descriptions
        kw_norm = _normalize(keyword)
        if len(kw_norm) >= 6 and kw_norm in desc_norm:
            return True
    return False


# Reasonable INR/USD exchange rate range for sanity checks
_MIN_RATE = 75.0
_MAX_RATE = 100.0
_MID_RATE = 86.0  # Approximate mid-point for estimation


def find_cc_transaction(vendor_name, bill_amount, bill_date, currency, cc_transactions, vendor_to_merchants, used_indices):
    """Find the matching CC transaction for a bill.

    For USD bills: matches by vendor name + USD amount in CC description,
    or by estimated INR amount (USD × ~86) with exchange rate sanity check.
    For INR bills: matches by vendor name + amount (3% tolerance).
    used_indices: set of already-matched CC transaction indices.
    Returns (match_dict, txn_index) or (None, None).
    """
    if not vendor_name:
        return None, None

    vname_lower = vendor_name.lower()

    # Get known merchant keywords for this vendor
    merchant_keywords = list(vendor_to_merchants.get(vname_lower, []))
    merchant_keywords.append(vname_lower)
    # Also add the vendor name without spaces for normalized matching
    merchant_keywords.append(re.sub(r'\s+', '', vname_lower))

    # Parse bill date for proximity check
    try:
        b_date = datetime.strptime(bill_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        b_date = None

    best_match = None
    best_score = 0
    best_idx = None

    for idx, txn in enumerate(cc_transactions):
        if idx in used_indices:
            continue

        desc_lower = txn["description"].lower()
        desc_norm = _normalize(txn["description"])
        txn_amount = txn["amount"]

        # Skip credits
        if txn_amount <= 0:
            continue

        # Check vendor keyword match
        if not _match_vendor_keywords(desc_lower, desc_norm, merchant_keywords):
            continue

        log_action(f"  [diag] Keyword hit: '{txn['description'][:60]}' INR {txn_amount} date {txn['date']}")
        score = 0

        if currency == "INR":
            # INR matching: 3% tolerance (was 0.5% — too tight for tax differences)
            diff = abs(txn_amount - bill_amount)
            tolerance = max(10.0, bill_amount * 0.03)
            if diff > tolerance:
                log_action(f"  [diag]   INR amount mismatch: txn {txn_amount} vs bill {bill_amount} (diff {diff:.2f} > tol {tolerance:.2f})")
                continue
            # Score higher for closer amount match
            pct_diff = diff / bill_amount if bill_amount else 1.0
            score = 150 - int(pct_diff * 1000)  # 150 for exact, lower for further
            log_action(f"  [diag]   Strategy INR: diff={diff:.2f} score={score}")
        else:
            # Foreign currency matching: try multiple strategies
            # Strategy 0: Direct forex match from parsed CC data (highest confidence)
            cc_forex = txn.get("forex_amount")
            cc_forex_cur = txn.get("forex_currency")
            if cc_forex and cc_forex_cur and cc_forex_cur.upper() == currency.upper():
                if abs(cc_forex - bill_amount) < 0.05:
                    score = 400  # Exact currency match — highest confidence
                    log_action(f"  [diag]   Strategy 0 (forex): {cc_forex_cur} {cc_forex} == bill {bill_amount} -> score {score}")
                else:
                    # Same currency but wrong amount — skip
                    log_action(f"  [diag]   Strategy 0 fail: {cc_forex_cur} {cc_forex} != bill {bill_amount}")
                    continue
            else:
                # Strategy 1: currency code amount in CC description (e.g. 'USD 223.07', 'QAR 22.00')
                cc_fx = _extract_forex_from_desc(txn["description"], currency)

                if cc_fx is not None:
                    if abs(cc_fx - bill_amount) < 0.02:
                        score = 300
                        log_action(f"  [diag]   Strategy 1 ({currency} in desc): {cc_fx} == bill {bill_amount} -> score {score}")
                    else:
                        log_action(f"  [diag]   Strategy 1 fail: {currency} in desc {cc_fx} != bill {bill_amount}")
                        continue
                elif currency == "USD":
                    # Strategy 2: Estimate INR from USD amount using known INR/USD range (75-100)
                    # Only applies to USD — other currencies have different rates; S0/S1 are preferred
                    estimated_inr = bill_amount * _MID_RATE
                    min_inr = bill_amount * _MIN_RATE
                    max_inr = bill_amount * _MAX_RATE

                    if min_inr <= txn_amount <= max_inr:
                        closeness = 1.0 - abs(txn_amount - estimated_inr) / estimated_inr
                        score = 100 + int(closeness * 50)
                        log_action(f"  [diag]   Strategy 2 (USD INR range): txn {txn_amount} in [{min_inr:.0f},{max_inr:.0f}] -> score {score}")
                    else:
                        log_action(f"  [diag]   Strategy 2 fail: txn {txn_amount} out of USD INR range [{min_inr:.0f},{max_inr:.0f}]")
                        continue
                else:
                    # Non-USD foreign currency with no forex tag and no amount in description → no match
                    log_action(f"  [diag]   No strategy for {currency} bill without forex tag or {currency} in desc -> skip")
                    continue

        # Date filter: ±10 days — handles legitimate payment delays while keeping
        # monthly same-amount subscriptions (30-day gap) from matching the wrong month
        if b_date:
            try:
                t_date = datetime.strptime(txn["date"], "%Y-%m-%d")
                day_diff = abs((t_date - b_date).days)
                if day_diff > 10:
                    log_action(f"  [diag]   Date filter: txn {txn['date']} vs bill {bill_date} diff={day_diff}d > 10 -> skip")
                    continue
                # Closer date = higher score bonus
                score += (10 - day_diff) * 5  # up to +50
                log_action(f"  [diag]   Date ok: diff={day_diff}d score={score}")
            except (ValueError, TypeError):
                pass

        if score > best_score:
            best_score = score
            best_match = txn
            best_idx = idx

    if best_match and best_score >= 50:
        log_action(f"  [diag] Best match: '{best_match['description'][:60]}' score={best_score}")
        result = {
            "inr_amount": best_match["amount"],
            "card_name": best_match.get("card_name"),
            "zoho_account_id": best_match.get("zoho_account_id"),
            "txn_date": best_match["date"],
            "description": best_match["description"],
        }
        if best_match.get("forex_amount"):
            result["forex_amount"] = best_match["forex_amount"]
            result["forex_currency"] = best_match["forex_currency"]
        return result, best_idx

    log_action(f"  [diag] No match found for {vendor_name} ({currency} {bill_amount}) bill_date={bill_date}")
    return None, None


def record_payment(api, bill_entry, config, cc_transactions, vendor_to_merchants, used_indices, currency_map):
    """Record a vendor payment for a single bill."""
    bill_id = bill_entry["bill_id"]
    vendor_id = bill_entry["vendor_id"]
    amount = bill_entry["amount"]
    currency = bill_entry.get("currency", "INR")
    vendor_name = bill_entry.get("vendor_name", "")

    # Fetch full bill to get date and total
    try:
        bill_data = api.get_bill(bill_id)
        bill = bill_data.get("bill", {})
        bill_date = bill.get("date", datetime.now().strftime("%Y-%m-%d"))
        bill_total = bill.get("total", amount)
    except Exception as e:
        log_action(f"  Could not fetch bill {bill_id}: {e}", "WARNING")
        bill_date = datetime.now().strftime("%Y-%m-%d")
        bill_total = amount

    # Try to find matching CC transaction
    cc_match, match_idx = find_cc_transaction(
        vendor_name, bill_total, bill_date, currency,
        cc_transactions, vendor_to_merchants, used_indices,
    )
    if match_idx is not None:
        used_indices.add(match_idx)

    # Only record payment if there's a matching CC transaction
    if not cc_match:
        log_action(f"  SKIP: No CC transaction found for {vendor_name} ({currency} {bill_total}) - bill stays open", "WARNING")
        return None, None

    account_id = cc_match["zoho_account_id"]
    card_name = cc_match["card_name"]
    log_action(f"  CC match: '{cc_match['description']}' INR {cc_match['inr_amount']} on {cc_match['txn_date']}")

    # Use CC transaction date as payment date (actual charge date, may differ ±5 days from bill date)
    payment_date = cc_match["txn_date"] or bill_date
    if payment_date != bill_date:
        log_action(f"  Date adjusted: bill {bill_date} -> CC payment {payment_date}")

    payment_data = {
        "vendor_id": vendor_id,
        "payment_mode": "Credit Card",
        "date": payment_date,
        "amount": bill_total,
        "paid_through_account_id": account_id,
        "bills": [
            {
                "bill_id": bill_id,
                "amount_applied": bill_total,
            }
        ],
    }

    # Handle foreign currency bills: calculate exchange rate from CC INR amount
    # Use enough precision so rate × bill_total = exact INR (avoids 1-3 Rs mismatch in banking)
    # Applies to ALL non-INR currencies (USD, QAR, EUR, GBP, etc.)
    if currency != "INR":
        actual_inr = cc_match["inr_amount"]
        if bill_total:
            # Try increasing precision until rate × bill_total rounds to actual_inr
            exact_rate = actual_inr / bill_total
            for decimals in range(6, 12):
                test_rate = round(exact_rate, decimals)
                if round(test_rate * bill_total, 2) == round(actual_inr, 2):
                    exact_rate = test_rate
                    break
            else:
                exact_rate = round(exact_rate, 10)
        else:
            exact_rate = 0
        payment_data["currency_id"] = currency_map.get(currency)
        payment_data["exchange_rate"] = exact_rate
        log_action(f"  {currency} {bill_total} -> INR {actual_inr} (rate: {exact_rate})")

    try:
        result = api.record_vendor_payment(payment_data)
        payment = result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")
        if payment_id:
            log_action(f"  Payment recorded: {payment_id} via {card_name} ({currency} {bill_total})")
            return payment_id, cc_match
    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            log_action(f"  Bill {bill_id} already paid, skipping")
            return "already_paid", cc_match
        log_action(f"  Payment failed for bill {bill_id}: {e}", "ERROR")

    return None, cc_match


# --- Run (importable by run_loop.py) ---

def run():
    """Record vendor payments for unpaid bills with matching CC transactions.

    Bills with no CC match get status "unmatched" (not "failed") so they are
    retried on the next loop run when new CC transactions may be available.

    Returns:
        dict: {
            "paid_count": int,
            "matched_bill_ids": list[str],
            "still_unmatched_bill_ids": list[str],
            "already_paid_count": int,
        }
    """
    log_action("=" * 50)
    log_action("Step 5: Record Vendor Payments")
    log_action("=" * 50)

    config = load_config()
    vendor_mappings = load_vendor_mappings()
    api = ZohoBooksAPI(config)

    cards = config.get("credit_cards", [])
    resolve_account_ids(api, cards)

    currency_map = api.list_currencies()
    log_action(f"Loaded {len(currency_map)} currencies from Zoho")

    # Fetch all unpaid/overdue bills directly from Zoho (source of truth)
    zoho_bills = fetch_unpaid_bills_from_zoho(api)
    bills = [
        {
            "bill_id": b.get("bill_id", ""),
            "vendor_id": b.get("vendor_id", ""),
            "vendor_name": b.get("vendor_name", ""),
            "amount": float(b.get("total", 0)),
            "currency": b.get("currency_code", "INR"),
            "file": b.get("bill_number", b.get("bill_id", "")),
            "date": b.get("date", ""),
        }
        for b in zoho_bills
        if b.get("bill_id")
    ]
    log_action(f"Found {len(bills)} unpaid bills to process")

    # Fetch CC transactions from Zoho Banking (source of truth, not local JSON)
    cc_transactions = fetch_cc_transactions_from_zoho(api, cards)
    if not cc_transactions:
        log_action("No CC transactions found in Zoho Banking", "WARNING")
        return {
            "paid_count": 0,
            "matched_bill_ids": [],
            "still_unmatched_bill_ids": [b["bill_id"] for b in bills],
            "already_paid_count": 0,
        }
    log_action(f"Loaded {len(cc_transactions)} CC transactions from Zoho for matching")

    # Build reverse mapping: vendor name -> merchant keywords
    vendor_to_merchants = build_vendor_to_merchants(vendor_mappings)

    # Load existing payments to skip re-processing within this session
    # (Zoho already filters out paid bills above, this guards against same-session duplicates)
    paid = {}
    if os.path.exists(PAYMENTS_FILE):
        with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
            for entry in json.load(f):
                if entry.get("bill_id") and entry.get("status") == "paid":
                    paid[entry["bill_id"]] = entry

    results = list(paid.values())
    unmatched_usd = []
    matched_bill_ids = []
    still_unmatched_bill_ids = []
    already_paid_count = 0
    used_indices = set()  # Track which CC transactions have been matched (prevents double-use)

    for bill_entry in bills:
        bill_id = bill_entry["bill_id"]
        if bill_id in paid:
            log_action(f"Skipping (already paid this session): {bill_entry['file']}")
            already_paid_count += 1
            continue

        log_action(f"Recording payment for: {bill_entry['file']}")
        payment_id, cc_match = record_payment(
            api, bill_entry, config, cc_transactions, vendor_to_merchants, used_indices, currency_map,
        )

        # "unmatched" (no CC transaction found) allows retry on next loop run
        # "paid" (payment recorded) is final
        # "already_paid" (Zoho says already paid) is final
        if payment_id and payment_id != "already_paid":
            status = "paid"
            matched_bill_ids.append(bill_id)
        elif payment_id == "already_paid":
            status = "paid"
            already_paid_count += 1
        else:
            status = "unmatched"
            still_unmatched_bill_ids.append(bill_id)

        entry = {
            "file": bill_entry["file"],
            "bill_id": bill_id,
            "vendor_name": bill_entry.get("vendor_name"),
            "amount": bill_entry.get("amount"),
            "currency": bill_entry.get("currency"),
            "payment_id": payment_id,
            "status": status,
        }
        if cc_match:
            entry["cc_inr_amount"] = cc_match["inr_amount"]
            entry["cc_card"] = cc_match["card_name"]

        results.append(entry)

        if bill_entry.get("currency") == "USD" and not payment_id:
            unmatched_usd.append(bill_entry["file"])

        # Pace bulk API calls to stay under Zoho rate limits
        time.sleep(0.3)

    # Save results
    os.makedirs(os.path.dirname(PAYMENTS_FILE), exist_ok=True)
    with open(PAYMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    paid_count = sum(1 for r in results if r.get("status") == "paid")
    log_action(f"Done. {paid_count}/{len(bills)} payments recorded. Results: {PAYMENTS_FILE}")

    if unmatched_usd:
        log_action(f"WARNING: {len(unmatched_usd)} USD bills had no CC match (skipped):", "WARNING")
        for fname in unmatched_usd:
            log_action(f"  - {fname}", "WARNING")

    if still_unmatched_bill_ids:
        log_action(f"{len(still_unmatched_bill_ids)} bills unmatched (will retry on next run)")

    return {
        "paid_count": len(matched_bill_ids),
        "matched_bill_ids": matched_bill_ids,
        "still_unmatched_bill_ids": still_unmatched_bill_ids,
        "already_paid_count": already_paid_count,
    }


# --- Main ---

def main():
    run()


if __name__ == "__main__":
    main()
