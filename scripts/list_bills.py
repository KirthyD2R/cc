"""
Utility: List All Bills

Shows all bills in Zoho Books with vendor, amount, status.
Usage: python scripts/list_bills.py [--status open|paid|all]
"""

import argparse
from utils import load_config, ZohoBooksAPI, log_action


def main():
    parser = argparse.ArgumentParser(description="List bills from Zoho Books")
    parser.add_argument("--status", default="all", choices=["open", "paid", "all"],
                        help="Filter by status (default: all)")
    args = parser.parse_args()

    config = load_config()
    api = ZohoBooksAPI(config)

    if args.status == "all":
        result = api.list_bills()
    else:
        result = api.list_bills(status=args.status)

    bills = result.get("bills", [])

    print(f"\n{'='*80}")
    print(f"  Bills in Zoho Books ({len(bills)} total, filter: {args.status})")
    print(f"{'='*80}")
    print(f"  {'Bill #':<20} {'Vendor':<25} {'Amount':>12} {'Status':<10} {'Date'}")
    print(f"  {'-'*20} {'-'*25} {'-'*12} {'-'*10} {'-'*10}")

    for bill in bills:
        print(
            f"  {bill.get('bill_number', 'N/A'):<20} "
            f"{bill.get('vendor_name', 'N/A')[:25]:<25} "
            f"{bill.get('currency_code', '')} {bill.get('total', 0):>8.2f} "
            f"{bill.get('status', 'N/A'):<10} "
            f"{bill.get('date', 'N/A')}"
        )

    print(f"\n  Total: {len(bills)} bills")


if __name__ == "__main__":
    main()
