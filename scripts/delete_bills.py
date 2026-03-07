"""
Utility: Delete All Bills (for re-runs/testing)

Deletes all bills from Zoho Books. Use with caution.
Usage: python scripts/delete_bills.py [--status open|paid|all]
"""

import argparse
from utils import load_config, ZohoBooksAPI, log_action


def main():
    parser = argparse.ArgumentParser(description="Delete bills from Zoho Books")
    parser.add_argument("--status", default="all", choices=["open", "paid", "all"],
                        help="Delete only bills with this status (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="List bills without deleting")
    args = parser.parse_args()

    config = load_config()
    api = ZohoBooksAPI(config)

    log_action(f"Fetching bills (status={args.status})...")

    if args.status == "all":
        result = api.list_bills()
    else:
        result = api.list_bills(status=args.status)

    bills = result.get("bills", [])
    log_action(f"Found {len(bills)} bills")

    if not bills:
        return

    if args.dry_run:
        for bill in bills:
            log_action(
                f"  [DRY RUN] {bill['bill_number']} | {bill['vendor_name']} | "
                f"{bill['currency_code']} {bill['total']} | {bill['status']}"
            )
        log_action(f"Dry run complete. {len(bills)} bills would be deleted.")
        return

    deleted = 0
    for bill in bills:
        try:
            api.delete_bill(bill["bill_id"])
            deleted += 1
            log_action(f"  Deleted: {bill['bill_number']} ({bill['vendor_name']})")
        except Exception as e:
            log_action(f"  Failed to delete {bill['bill_number']}: {e}", "ERROR")

    log_action(f"Done. Deleted {deleted}/{len(bills)} bills.")


if __name__ == "__main__":
    main()
