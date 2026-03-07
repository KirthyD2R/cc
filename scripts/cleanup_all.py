"""
Utility: Complete Cleanup — Delete ALL data from Zoho Books

Deletes in the correct dependency order:
  1. Unmatch & uncategorize banking transactions
  2. Delete vendor payments (now unlinked)
  3. Void & delete bills
  4. Delete vendors
  5. Delete remaining bank transactions
  6. Clean local output files

Usage: python scripts/cleanup_all.py
"""

import os
import sys
import argparse
import requests
from utils import PROJECT_ROOT, load_config, ZohoBooksAPI, log_action, resolve_account_ids


def raw_api(api, method, endpoint, **kwargs):
    """Raw API call with retry on 429 rate-limit. Returns (ok, response_json_or_text)."""
    import time as _time
    url = f"{api.base_url}/{endpoint}"
    params = kwargs.pop("params", {})
    params["organization_id"] = api.org_id
    for attempt in range(4):
        try:
            resp = requests.request(method, url, headers=api.auth.get_headers(), params=params, **kwargs)
            if resp.ok:
                return True, resp.json()
            if resp.status_code == 429 and attempt < 3:
                wait = 60 * (attempt + 1)
                log_action(f"  Rate limited (429), waiting {wait}s before retry...", "WARNING")
                _time.sleep(wait)
                continue
            return False, resp.text[:300]
        except Exception as e:
            return False, str(e)
    return False, "Max retries exceeded (429)"


def paginated_fetch(api, endpoint, list_key, extra_params=None):
    """Fetch all items across pages."""
    all_items = []
    page = 1
    while True:
        params = {"page": page}
        if extra_params:
            params.update(extra_params)
        ok, result = raw_api(api, "GET", endpoint, params=params)
        if not ok:
            break
        items = result.get(list_key, [])
        if not items:
            break
        all_items.extend(items)
        ctx = result.get("page_context", {})
        if not ctx.get("has_more_page", False):
            break
        page += 1
    return all_items


# --- Step 1: Unmatch & Uncategorize Banking Transactions ---

def cleanup_banking(api, config):
    log_action("=" * 50)
    log_action("Step 1: Unmatch & Uncategorize Banking Transactions")
    log_action("=" * 50)

    cards = config.get("credit_cards", [])

    for card in cards:
        name = card["name"]
        account_id = card["zoho_account_id"]
        log_action(f"Processing {name}...")

        txns = paginated_fetch(
            api, "banktransactions", "banktransactions",
            extra_params={"account_id": account_id},
        )
        log_action(f"  Found {len(txns)} transactions")

        for txn in txns:
            status = txn.get("status", "").lower()
            imported_id = txn.get("imported_transaction_id", "")
            desc = txn.get("payee", txn.get("description", ""))[:40]

            if status == "matched" and imported_id:
                ok, msg = raw_api(
                    api, "POST", f"banktransactions/{imported_id}/unmatch",
                    params={"account_id": account_id},
                )
                log_action(f"  Unmatched: {desc}" if ok else f"  Unmatch failed ({desc}): {msg}")

            elif status == "categorized" and imported_id:
                ok, msg = raw_api(api, "POST", f"banktransactions/{imported_id}/uncategorize")
                log_action(f"  Uncategorized: {desc}" if ok else f"  Uncategorize failed ({desc}): {msg}")


# --- Step 2: Delete Vendor Payments ---

def cleanup_vendor_payments(api):
    log_action("=" * 50)
    log_action("Step 2: Delete Vendor Payments")
    log_action("=" * 50)

    payments = paginated_fetch(api, "vendorpayments", "vendorpayments")
    log_action(f"  Found {len(payments)} vendor payments")
    deleted = 0

    for p in payments:
        desc = f"{p.get('vendor_name', '')} {p.get('currency_code', '')} {p.get('amount', '')}"
        ok, msg = raw_api(api, "DELETE", f"vendorpayments/{p['payment_id']}")
        if ok:
            deleted += 1
            log_action(f"  Deleted payment: {desc}")
        else:
            log_action(f"  Failed ({desc}): {msg}", "WARNING")

    log_action(f"Vendor payments: {deleted}/{len(payments)} deleted")


# --- Step 3: Void & Delete Bills ---

def cleanup_bills(api):
    log_action("=" * 50)
    log_action("Step 3: Void & Delete Bills")
    log_action("=" * 50)

    bills = paginated_fetch(api, "bills", "bills")
    log_action(f"  Found {len(bills)} bills")
    deleted = 0

    for bill in bills:
        bid = bill["bill_id"]
        desc = f"{bill.get('bill_number', '')} ({bill.get('vendor_name', '')})"

        # Try direct delete first
        ok, msg = raw_api(api, "DELETE", f"bills/{bid}")
        if ok:
            deleted += 1
            log_action(f"  Deleted bill: {desc}")
            continue

        # If delete fails, void first then delete
        raw_api(api, "POST", f"bills/{bid}/status/void")
        ok, msg = raw_api(api, "DELETE", f"bills/{bid}")
        if ok:
            deleted += 1
            log_action(f"  Voided & deleted bill: {desc}")
        else:
            log_action(f"  Failed ({desc}): {msg}", "WARNING")

    log_action(f"Bills: {deleted}/{len(bills)} deleted")


# --- Step 4: Delete Vendors ---

def cleanup_vendors(api):
    log_action("=" * 50)
    log_action("Step 4: Delete Vendors")
    log_action("=" * 50)

    vendors = paginated_fetch(api, "contacts", "contacts", extra_params={"contact_type": "vendor"})
    log_action(f"  Found {len(vendors)} vendors")
    deleted = 0

    for v in vendors:
        name = v.get("contact_name", v["contact_id"])
        ok, msg = raw_api(api, "DELETE", f"contacts/{v['contact_id']}")
        if ok:
            deleted += 1
            log_action(f"  Deleted vendor: {name}")
        else:
            log_action(f"  Failed ({name}): {msg}", "WARNING")

    log_action(f"Vendors: {deleted}/{len(vendors)} deleted")


# --- Step 5: Delete Remaining Bank Transactions ---

def cleanup_remaining_bank_txns(api, config):
    log_action("=" * 50)
    log_action("Step 5: Delete Remaining Bank Transactions")
    log_action("=" * 50)

    cards = config.get("credit_cards", [])
    total = 0

    for card in cards:
        name = card["name"]
        account_id = card["zoho_account_id"]
        txns = paginated_fetch(
            api, "banktransactions", "banktransactions",
            extra_params={"account_id": account_id},
        )
        if not txns:
            continue

        log_action(f"  {name}: {len(txns)} remaining")
        for txn in txns:
            ok, _ = raw_api(api, "DELETE", f"banktransactions/{txn['transaction_id']}")
            if ok:
                total += 1

    log_action(f"Bank transactions: {total} deleted")


# --- Step 6: Clean Local Output Files ---

def cleanup_local_files():
    log_action("=" * 50)
    log_action("Step 6: Clean Local Output Files")
    log_action("=" * 50)

    output_dir = os.path.join(PROJECT_ROOT, "output")
    if not os.path.isdir(output_dir):
        log_action("  No output directory found")
        return

    cleaned = 0
    for f in os.listdir(output_dir):
        if f == "automation.log":
            continue
        fpath = os.path.join(output_dir, f)
        if os.path.isfile(fpath):
            os.remove(fpath)
            cleaned += 1
            log_action(f"  Removed: {f}")

    log_action(f"Local files: {cleaned} removed")


# --- Main ---

def main():
    # Issue #22: Add --dry-run flag and confirmation prompt
    parser = argparse.ArgumentParser(description="Complete cleanup - delete ALL data from Zoho Books")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    log_action("=" * 50)
    log_action("COMPLETE CLEANUP - Resetting Zoho Books")
    if args.dry_run:
        log_action("*** DRY RUN MODE — no deletions will be performed ***")
    log_action("=" * 50)

    if not args.yes and not args.dry_run:
        print("\n  WARNING: This will delete ALL vendors, bills, payments, and bank transactions!")
        print("  Use --dry-run to preview. Use --yes to skip this prompt.\n")
        confirm = input("  Type 'DELETE' to confirm: ")
        if confirm.strip() != "DELETE":
            print("  Aborted.")
            sys.exit(0)

    config = load_config()
    api = ZohoBooksAPI(config)
    try:
        resolve_account_ids(api, config.get("credit_cards", []))
    except Exception as e:
        log_action(f"resolve_account_ids failed ({e}), using config IDs as fallback", "WARNING")

    if args.dry_run:
        # Show counts only
        cards = config.get("credit_cards", [])
        for card in cards:
            txns = paginated_fetch(api, "banktransactions", "banktransactions",
                                   extra_params={"account_id": card["zoho_account_id"]})
            log_action(f"  Would process {len(txns)} bank transactions for {card['name']}")
        payments = paginated_fetch(api, "vendorpayments", "vendorpayments")
        log_action(f"  Would delete {len(payments)} vendor payments")
        bills = paginated_fetch(api, "bills", "bills")
        log_action(f"  Would delete {len(bills)} bills")
        vendors = paginated_fetch(api, "contacts", "contacts", extra_params={"contact_type": "vendor"})
        log_action(f"  Would delete {len(vendors)} vendors")
        log_action("Dry run complete. No changes made.")
        return

    cleanup_banking(api, config)
    cleanup_vendor_payments(api)
    cleanup_bills(api)
    cleanup_vendors(api)
    cleanup_remaining_bank_txns(api, config)
    cleanup_local_files()

    log_action("=" * 50)
    log_action("ALL DONE - Zoho Books is clean. Ready for fresh run.")
    log_action("=" * 50)


if __name__ == "__main__":
    main()
