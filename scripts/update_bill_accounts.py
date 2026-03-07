"""
Utility: Update Bill Accounts (Recategorize Existing Bills)

Recategorizes existing bills in Zoho Books by assigning the correct
COA expense account based on vendor type, instead of the default
"Credit Card Charges" account.

Usage:
    python scripts/update_bill_accounts.py                 # Update all bills
    python scripts/update_bill_accounts.py --dry-run       # Preview changes
    python scripts/update_bill_accounts.py --vendor "AWS"  # Filter by vendor name
"""

import argparse
import json
import os
from utils import (
    PROJECT_ROOT, load_config, ZohoBooksAPI, VendorCategorizer, log_action,
)

RESULTS_FILE = os.path.join(PROJECT_ROOT, "output", "updated_bills.json")


def main():
    parser = argparse.ArgumentParser(description="Recategorize bill expense accounts in Zoho Books")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without updating")
    parser.add_argument("--vendor", type=str, default=None, help="Filter by vendor name (substring match)")
    args = parser.parse_args()

    config = load_config()
    api = ZohoBooksAPI(config)
    categorizer = VendorCategorizer(api)

    log_action("=" * 60)
    log_action("Update Bill Accounts — Recategorize Existing Bills")
    if args.dry_run:
        log_action("  Mode: DRY RUN (no changes will be made)")
    log_action("=" * 60)

    # Fetch all bills
    result = api.list_bills()
    bills = result.get("bills", [])
    log_action(f"Found {len(bills)} bills in Zoho Books")

    # Filter by vendor if specified
    if args.vendor:
        vendor_filter = args.vendor.lower()
        bills = [b for b in bills if vendor_filter in b.get("vendor_name", "").lower()]
        log_action(f"Filtered to {len(bills)} bills matching vendor '{args.vendor}'")

    if not bills:
        log_action("No bills to process.")
        return

    results = []
    updated_count = 0
    skipped_count = 0
    failed_count = 0

    for bill_summary in bills:
        bill_id = bill_summary["bill_id"]
        vendor_name = bill_summary.get("vendor_name", "")
        bill_number = bill_summary.get("bill_number", "N/A")

        log_action(f"Processing: {bill_number} ({vendor_name})")

        # Get full bill detail (need line items with current account_id)
        try:
            bill_detail = api.get_bill(bill_id).get("bill", {})
        except Exception as e:
            log_action(f"  Failed to fetch bill detail: {e}", "ERROR")
            results.append({"bill_id": bill_id, "bill_number": bill_number, "status": "error", "reason": str(e)})
            failed_count += 1
            continue

        # Get categorized account for this vendor
        try:
            new_account_id, new_account_name = categorizer.get_account_for_vendor(vendor_name)
        except Exception as e:
            log_action(f"  Categorization failed: {e}", "WARNING")
            results.append({"bill_id": bill_id, "bill_number": bill_number, "status": "error", "reason": str(e)})
            failed_count += 1
            continue

        if not new_account_id:
            log_action(f"  No account resolved for '{vendor_name}', skipping")
            results.append({"bill_id": bill_id, "bill_number": bill_number, "status": "skipped", "reason": "no account"})
            skipped_count += 1
            continue

        # Check if any line item needs updating
        line_items = bill_detail.get("line_items", [])
        needs_update = False
        for item in line_items:
            if item.get("account_id") != new_account_id:
                needs_update = True
                break

        if not needs_update:
            log_action(f"  Already categorized: {new_account_name}")
            results.append({
                "bill_id": bill_id,
                "bill_number": bill_number,
                "vendor_name": vendor_name,
                "status": "already_correct",
                "account_name": new_account_name,
            })
            skipped_count += 1
            continue

        # Build updated line items
        old_account_name = line_items[0].get("account_name", "unknown") if line_items else "unknown"
        updated_items = []
        for item in line_items:
            updated_items.append({
                "line_item_id": item.get("line_item_id"),
                "account_id": new_account_id,
                "description": item.get("description", ""),
                "rate": item.get("rate", 0),
                "quantity": item.get("quantity", 1),
            })

        if args.dry_run:
            log_action(
                f"  [DRY RUN] Would update: {old_account_name} -> {new_account_name}"
            )
            results.append({
                "bill_id": bill_id,
                "bill_number": bill_number,
                "vendor_name": vendor_name,
                "status": "would_update",
                "old_account": old_account_name,
                "new_account": new_account_name,
            })
            updated_count += 1
            continue

        # Update the bill
        try:
            api.update_bill(bill_id, {"line_items": updated_items})
            log_action(f"  Updated: {old_account_name} -> {new_account_name}")
            results.append({
                "bill_id": bill_id,
                "bill_number": bill_number,
                "vendor_name": vendor_name,
                "status": "updated",
                "old_account": old_account_name,
                "new_account": new_account_name,
            })
            updated_count += 1
        except Exception as e:
            log_action(f"  Failed to update bill: {e}", "ERROR")
            results.append({
                "bill_id": bill_id,
                "bill_number": bill_number,
                "vendor_name": vendor_name,
                "status": "error",
                "reason": str(e),
            })
            failed_count += 1

    # Save results
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Summary
    action = "would update" if args.dry_run else "updated"
    log_action(f"\nDone. {action}: {updated_count}, skipped: {skipped_count}, failed: {failed_count}")
    log_action(f"Results saved to: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
