"""
Utility: Delete All Banking Transactions (for re-runs/testing)

Deletes all imported bank transactions from Zoho Books CC accounts.
Handles categorized/matched transactions by uncategorizing them first.
Usage: python scripts/delete_banking_transactions.py [--dry-run]
"""

import argparse
from utils import load_config, ZohoBooksAPI, log_action, resolve_account_ids


def fetch_all_transactions(api, account_id):
    """Fetch all bank transactions across all pages."""
    all_txns = []
    page = 1
    while True:
        result = api.list_bank_transactions(account_id, page=page)
        txns = result.get("banktransactions", [])
        if not txns:
            break
        all_txns.extend(txns)
        page_context = result.get("page_context", {})
        if not page_context.get("has_more_page", False):
            break
        page += 1
    return all_txns


def main():
    parser = argparse.ArgumentParser(description="Delete banking transactions from Zoho Books")
    parser.add_argument("--dry-run", action="store_true", help="List transactions without deleting")
    parser.add_argument("--account", help="Only delete for a specific CC account name")
    args = parser.parse_args()

    config = load_config()
    api = ZohoBooksAPI(config)
    cards = config.get("credit_cards", [])

    if not cards:
        log_action("No credit cards configured", "ERROR")
        return

    resolve_account_ids(api, cards)

    total_deleted = 0
    total_found = 0

    for card in cards:
        name = card["name"]
        account_id = card["zoho_account_id"]

        if args.account and args.account.lower() not in name.lower():
            continue

        log_action(f"Fetching transactions for {name} (account {account_id})...")
        txns = fetch_all_transactions(api, account_id)
        log_action(f"  Found {len(txns)} transactions")
        total_found += len(txns)

        if not txns:
            continue

        if args.dry_run:
            for txn in txns:
                log_action(
                    f"  [DRY RUN] {txn.get('date', 'N/A')} | "
                    f"{txn.get('reference_number', 'N/A')} | "
                    f"{txn.get('payee', txn.get('description', 'N/A'))} | "
                    f"{txn.get('currency_code', '')} {txn.get('amount', 0)} | "
                    f"{txn.get('status', 'N/A')}"
                )
            continue

        deleted = 0
        for txn in txns:
            txn_id = txn["transaction_id"]
            status = txn.get("status", "").lower()
            desc = txn.get("payee", txn.get("description", txn_id))

            try:
                # Matched/categorized transactions must be uncategorized first
                if status in ("matched", "categorized"):
                    try:
                        api.uncategorize_transaction(txn_id)
                        log_action(f"  Uncategorized: {desc}")
                    except Exception:
                        pass  # May already be uncategorized or API differs

                api.delete_bank_transaction(txn_id)
                deleted += 1
                log_action(f"  Deleted: {txn.get('date', '')} | {desc} | {status}")
            except Exception as e:
                log_action(f"  Failed to delete {desc}: {e}", "ERROR")

        total_deleted += deleted
        log_action(f"  Deleted {deleted}/{len(txns)} transactions for {name}")

    if args.dry_run:
        log_action(f"Dry run complete. {total_found} transactions would be deleted.")
    else:
        log_action(f"Done. Deleted {total_deleted}/{total_found} banking transactions.")


if __name__ == "__main__":
    main()
