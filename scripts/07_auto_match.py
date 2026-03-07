"""
Step 7: Auto-Match Uncategorized Banking Transactions

For each CC account:
  1. List uncategorized transactions (with pagination)
  2. For each, fetch Zoho's suggested matches
  3. Rank candidates by amount closeness and try to match
  4. Handle amount mismatches gracefully (try next candidate)
  5. Rate-limit aware with inter-request delays
"""

import time
from utils import load_config, ZohoBooksAPI, log_action, resolve_account_ids


def _fetch_all_uncategorized(api, account_id):
    """Fetch all uncategorized transactions with pagination."""
    all_txns = []
    page = 1
    while True:
        try:
            result = api.list_uncategorized(account_id, page=page)
        except Exception as e:
            log_action(f"  Failed to list uncategorized (page {page}): {e}", "ERROR")
            break

        txns = result.get("banktransactions", [])
        all_txns.extend(txns)

        page_context = result.get("page_context", {})
        if page_context.get("has_more_page", False):
            page += 1
        else:
            break

    return all_txns


def _get_comparable_amount(candidate, txn_amount_abs):
    """Get the candidate amount in the same currency as the banking transaction.

    Zoho candidates may return amount in original currency (e.g. USD) while
    the banking transaction is in INR. Use bcy_amount (base currency amount)
    when available, or detect cross-currency by exchange rate heuristic.
    """
    # Prefer bcy_amount (base currency = INR for Indian org)
    bcy = candidate.get("bcy_amount")
    if bcy is not None:
        return abs(float(bcy))

    # Fallback: debit_amount / credit_amount in base currency
    for field in ("bcy_debit", "bcy_credit", "debit_amount_bcy", "credit_amount_bcy"):
        val = candidate.get(field)
        if val is not None and float(val) > 0:
            return abs(float(val))

    # Check if candidate is in a foreign currency
    raw_amount = abs(float(candidate.get("amount", 0)))
    currency = candidate.get("currency_code", "INR")

    if currency != "INR" and txn_amount_abs > 0 and raw_amount > 0:
        # Cross-currency: check if ratio looks like an exchange rate (60-100 for USD/INR)
        ratio = txn_amount_abs / raw_amount
        if 60 <= ratio <= 110:
            # This candidate is likely in USD, banking is in INR
            # Use the raw amount * estimated rate = txn amount (let Zoho handle actual matching)
            # Return txn_amount_abs so it passes ranking — Zoho API will validate the actual match
            log_action(
                f"    [diag] Cross-currency candidate: {currency} {raw_amount} "
                f"-> INR {txn_amount_abs} (ratio {ratio:.2f})"
            )
            return txn_amount_abs  # Treat as compatible — let Zoho API decide

    return raw_amount


def _rank_candidates(candidates, txn_amount_abs, txn_date=None):
    """Rank candidates by amount closeness and filter by confidence.

    Strategy:
    - 1 candidate: trust Zoho's suggestion, try it directly
    - 2-5 candidates: try up to 2 closest within 10%
    - 6+ candidates: try closest within 5%, prefer date-matched

    Handles cross-currency: uses bcy_amount (INR) when candidates are in USD.
    """
    if not candidates:
        return []

    # Log first candidate's fields once for diagnostics
    if candidates:
        first = candidates[0]
        diag_fields = {k: v for k, v in first.items() if v not in (None, "", 0, "0.00")}
        log_action(f"    [diag] Candidate fields: {list(diag_fields.keys())}")
        log_action(
            f"    [diag] Sample: amount={first.get('amount')} "
            f"bcy_amount={first.get('bcy_amount')} "
            f"currency={first.get('currency_code')} "
            f"type={first.get('transaction_type')}"
        )

    scored = []
    for c in candidates:
        c_amount = _get_comparable_amount(c, txn_amount_abs)
        diff = abs(c_amount - txn_amount_abs)
        pct = diff / txn_amount_abs if txn_amount_abs > 0 else float("inf")

        # Date bonus: if candidate date matches transaction date, prioritize it
        date_penalty = 0
        if txn_date and c.get("date"):
            if c["date"] == txn_date:
                date_penalty = -1  # Exact date match gets priority
            elif abs(_days_diff(txn_date, c["date"])) <= 5:
                date_penalty = 0
            else:
                date_penalty = 1  # Far date gets deprioritized

        scored.append((date_penalty, pct, diff, c))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))

    n = len(candidates)

    if n == 1:
        # Single candidate — trust Zoho's suggestion
        return [scored[0][3]]
    elif n <= 5:
        # 2-5 candidates — try up to 2 closest within 10%
        result = [s[3] for s in scored[:2] if s[1] < 0.10]
        if not result:
            # Fallback: try the best candidate if within 20% (cross-currency rounding)
            if scored[0][1] < 0.20:
                result = [scored[0][3]]
        return result
    else:
        # 6+ candidates — try closest within 5%, up to 3 attempts
        result = [s[3] for s in scored[:3] if s[1] < 0.05]
        if not result:
            # Fallback: try best 2 within 10%
            result = [s[3] for s in scored[:2] if s[1] < 0.10]
        return result


def _days_diff(date1, date2):
    """Return difference in days between two YYYY-MM-DD date strings."""
    try:
        from datetime import datetime
        d1 = datetime.strptime(date1, "%Y-%m-%d")
        d2 = datetime.strptime(date2, "%Y-%m-%d")
        return (d1 - d2).days
    except (ValueError, TypeError):
        return 999


def _try_match(api, txn_id, txn_amount, txn_date, ranked_candidates):
    """Try to match a banking transaction against ranked candidates.

    Strategy:
    1. Try direct match API (works when amounts are exact)
    2. On 'total amount does not match', fall back to categorize-as-vendor-payment
       (Zoho's '+ Create New Transaction' flow) for bills with small difference

    Returns (matched: bool, candidate_used: dict or None).
    """
    for candidate in ranked_candidates:
        match_data = [
            {
                "transaction_id": candidate.get("transaction_id"),
                "transaction_type": candidate.get("transaction_type", "vendor_payment"),
            }
        ]

        try:
            api.match_transaction(txn_id, match_data)
            return True, candidate
        except Exception as e:
            error_msg = str(e).lower()
            if "total amount does not match" in error_msg:
                c_amount = abs(float(candidate.get("amount", 0)))
                txn_abs = abs(float(txn_amount))
                diff = abs(c_amount - txn_abs)
                pct = (diff / txn_abs * 100) if txn_abs else 0

                log_action(
                    f"    Match amount mismatch: bill={c_amount} banking={txn_abs} "
                    f"diff={diff:.2f} ({pct:.1f}%)"
                )

                # Fallback: categorize as vendor payment if candidate is a bill
                # and difference is within 5% (handles exchange rate / tax rounding)
                if candidate.get("transaction_type") == "bill" and pct < 5:
                    vendor_id = candidate.get("contact_id") or candidate.get("vendor_id")
                    bill_id = candidate.get("transaction_id")
                    if vendor_id and bill_id:
                        try:
                            log_action(f"    Trying categorize-as-payment fallback...")
                            api.categorize_as_vendor_payment(
                                txn_id, vendor_id, bill_id, txn_abs, txn_date
                            )
                            log_action(f"    Categorized as vendor payment (applied {txn_abs})")
                            return True, candidate
                        except Exception as cat_err:
                            log_action(f"    Categorize fallback failed: {cat_err}", "WARNING")

                continue
            elif "already" in error_msg:
                # Already matched/categorized
                return True, candidate
            else:
                # Unexpected error — stop trying for this transaction
                log_action(f"    Match error: {e}", "WARNING")
                return False, None

    return False, None


def auto_match_account(api, account_id, card_name):
    """Process all uncategorized transactions for one CC account."""
    log_action(f"Processing: {card_name} (account: {account_id})")

    all_transactions = _fetch_all_uncategorized(api, account_id)

    if not all_transactions:
        log_action(f"  No uncategorized transactions")
        return 0, 0

    log_action(f"  Found {len(all_transactions)} uncategorized transactions")

    matched_count = 0
    skipped_count = 0

    for txn in all_transactions:
        txn_id = txn.get("transaction_id")
        txn_date = txn.get("date", "")
        txn_amount = txn.get("amount", 0)
        txn_desc = txn.get("description", txn.get("payee", ""))

        log_action(f"  Transaction: {txn_date} | {txn_desc} | {txn_amount}")

        # Fetch possible matches from Zoho
        try:
            match_result = api.get_matching_transactions(txn_id)
        except Exception as e:
            log_action(f"    Failed to get matches: {e}", "WARNING")
            skipped_count += 1
            continue

        possible = match_result.get("matching_transactions", [])
        if not possible:
            log_action(f"    No possible matches found")
            skipped_count += 1
            continue

        # Rank candidates by amount closeness + date proximity
        txn_amount_abs = abs(float(txn_amount))
        ranked = _rank_candidates(possible, txn_amount_abs, txn_date=txn_date)

        if not ranked:
            log_action(f"    {len(possible)} candidates but no confident match")
            skipped_count += 1
            continue

        # Try matching against ranked candidates
        matched, candidate = _try_match(api, txn_id, txn_amount, txn_date, ranked)

        if matched and candidate:
            matched_count += 1
            log_action(
                f"    Matched -> {candidate.get('transaction_type')}: "
                f"{candidate.get('payee', candidate.get('vendor_name', 'N/A'))} "
                f"({candidate.get('amount')}) date: {candidate.get('date')}"
            )
        else:
            if ranked:
                # Show what was tried for debugging
                for r in ranked[:2]:
                    comparable = _get_comparable_amount(r, txn_amount_abs)
                    log_action(
                        f"    Tried: {r.get('payee', r.get('vendor_name', '?'))} "
                        f"amount={r.get('amount')} bcy={r.get('bcy_amount')} "
                        f"currency={r.get('currency_code', '?')} "
                        f"date={r.get('date')} "
                        f"(comparable={comparable:.2f} diff={comparable - txn_amount_abs:.2f})"
                    )
                log_action(f"    {len(possible)} candidates, amount mismatch with Zoho")
            skipped_count += 1

        # Small delay between transactions to stay under rate limits
        time.sleep(0.3)

    return matched_count, skipped_count


# --- Run (importable by run_loop.py) ---

def run():
    """Auto-match uncategorized banking transactions to vendor payments.

    Returns:
        dict: {"matched_count": int, "skipped_count": int}
    """
    log_action("=" * 50)
    log_action("Step 7: Auto-Match Banking Transactions")
    log_action("=" * 50)

    config = load_config()
    api = ZohoBooksAPI(config)
    cards = config.get("credit_cards", [])

    if not cards:
        log_action("No credit cards configured", "ERROR")
        return {"matched_count": 0, "skipped_count": 0}

    resolve_account_ids(api, cards)

    total_matched = 0
    total_skipped = 0

    for card in cards:
        matched, skipped = auto_match_account(
            api, card["zoho_account_id"], card["name"]
        )
        total_matched += matched
        total_skipped += skipped

    log_action(f"Done. Matched: {total_matched}, Skipped: {total_skipped}")
    log_action("Verify: Zoho Books -> Banking -> CC accounts -> Categorized tab")

    return {"matched_count": total_matched, "skipped_count": total_skipped}


# --- Main ---

def main():
    run()


if __name__ == "__main__":
    main()
