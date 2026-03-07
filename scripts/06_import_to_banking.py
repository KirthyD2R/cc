"""
Step 6: Import CC Transaction CSVs to Zoho Banking

Reads parsed CC statement CSVs and imports them as JSON transactions
into Zoho Books Banking module via the bankstatements API.
Transactions appear as "Uncategorized" in the CC account.
"""

import os
import csv
import json
import hashlib
from utils import PROJECT_ROOT, load_config, ZohoBooksAPI, log_action, resolve_account_ids

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
TRACKING_FILE = os.path.join(OUTPUT_DIR, "imported_statements.json")


def _file_md5(path):
    """Compute MD5 hash of a file for change detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_transactions_from_csv(csv_path):
    """Read CSV and convert to Zoho bankstatements transaction format.

    CSV sign convention: negative = charge (debit), positive = refund (credit).
    """
    transactions = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            amount = float(row["amount"])
            transactions.append({
                "date": row["date"],
                "debit_or_credit": "debit" if amount < 0 else "credit",
                "amount": abs(amount),
                "description": row["description"],
            })
    return transactions


def run(selected_cards=None):
    """Import CC transaction CSVs to Zoho Banking.

    Args:
        selected_cards: list of card names to import. When provided, only
            matching cards are imported. When None, all cards are imported.

    Returns:
        dict: {"imported_count": int, "skipped_count": int, "cards_imported": list[str]}
    """
    log_action("=" * 50)
    log_action("Step 6: Import CC Statements to Zoho Banking")
    if selected_cards:
        log_action(f"  Selected cards: {', '.join(selected_cards)}")
    log_action("=" * 50)

    config = load_config()
    api = ZohoBooksAPI(config)
    cards = config.get("credit_cards", [])

    if not cards:
        log_action("No credit cards configured", "ERROR")
        return {"imported_count": 0, "skipped_count": 0, "cards_imported": []}

    resolve_account_ids(api, cards)

    # Issue #19: Track imported statements to prevent duplicates on re-run
    imported = {}
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            imported = json.load(f)

    imported_count = 0
    skipped_count = 0
    cards_imported = []

    for card in cards:
        name = card["name"]
        # Skip cards not in selection
        if selected_cards is not None and name not in selected_cards:
            continue
        account_id = card["zoho_account_id"]
        safe_name = name.replace(" ", "_")
        csv_path = os.path.join(OUTPUT_DIR, f"{safe_name}_transactions.csv")

        if not os.path.exists(csv_path):
            log_action(f"No CSV found for {name}: {csv_path}", "WARNING")
            continue

        # Check if already imported — compare CSV hash and account ID
        csv_hash = _file_md5(csv_path)
        prev = imported.get(safe_name)
        account_changed = prev and prev.get("account_id") and prev["account_id"] != account_id
        if prev and prev.get("csv_hash") == csv_hash and not account_changed:
            log_action(f"Already imported for {name} (CSV unchanged), skipping")
            skipped_count += 1
            continue

        if account_changed:
            log_action(f"Zoho account changed for {name} (old: {prev['account_id']} -> new: {account_id}), re-importing")
        elif prev:
            log_action(f"CSV changed for {name} (new statement data detected), re-importing")

        transactions = load_transactions_from_csv(csv_path)
        if not transactions:
            log_action(f"CSV is empty for {name}, skipping", "WARNING")
            continue

        log_action(f"Importing {len(transactions)} transactions for {name} -> account {account_id}")

        dates = [t["date"] for t in transactions]
        payload = {
            "account_id": account_id,
            "start_date": min(dates),
            "end_date": max(dates),
            "transactions": transactions,
        }

        try:
            result = api._request("POST", "bankstatements", json=payload)
            log_action(f"  {result.get('message', 'Imported')}")
            imported[safe_name] = {
                "card_name": name,
                "account_id": account_id,
                "transaction_count": len(transactions),
                "date_range": f"{min(dates)} to {max(dates)}",
                "csv_hash": csv_hash,
            }
            imported_count += 1
            cards_imported.append(name)
        except Exception as e:
            error_msg = str(e)
            if "already" in error_msg.lower():
                log_action(f"  Transactions may already be imported for {name}", "WARNING")
                imported[safe_name] = {"card_name": name, "csv_hash": csv_hash, "note": "already existed in Zoho"}
                skipped_count += 1
            else:
                log_action(f"  Import failed for {name}: {e}", "ERROR")

    # Save tracking file
    os.makedirs(os.path.dirname(TRACKING_FILE), exist_ok=True)
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(imported, f, indent=2)

    log_action("Done. Check Zoho Books -> Banking -> CC accounts -> Uncategorized tab.")

    return {"imported_count": imported_count, "skipped_count": skipped_count, "cards_imported": cards_imported}


# --- Main ---

def main():
    run()


if __name__ == "__main__":
    main()
