"""
Web UI Dashboard — CC Statement Automation

Single-file Flask app with embedded HTML/CSS/JS.
Provides a browser-based dashboard to run pipeline steps, view live logs, and track status.

Usage:
    python app.py              # Starts server and opens browser at http://localhost:5000
    python app.py --port 8080  # Custom port
    python app.py --no-open    # Don't auto-open browser
"""

import os
import sys
import json
import re
import glob
import queue
import threading
import time
import importlib.util
import argparse
import webbrowser
import traceback
from datetime import datetime

from flask import Flask, Response, jsonify, request, render_template_string

# --- Setup paths ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from utils import PROJECT_ROOT as UTILS_ROOT, log_action, _log_subscribers

app = Flask(__name__)

# --- Script registry ---
STEPS = {
    "1": {
        "script": "01_fetch_invoices.py",
        "name": "Fetch Invoices from Inbox",
        "desc": "Download invoice PDFs from Outlook",
        "phase": 1,
        "run_kwargs": {"headless": False},
    },
    "2": {
        "script": "02_extract_invoices.py",
        "name": "Extract Data",
        "desc": "Extract vendor/amount/date from PDFs",
        "phase": 1,
        "run_kwargs": {},
    },
    "3": {
        "script": "03_create_vendors_bills.py",
        "name": "Create Bills and Vendors",
        "desc": "Create vendors & bills in Zoho Books",
        "phase": 1,
        "run_kwargs": {},
    },
    "4": {
        "script": "04_parse_cc_statements.py",
        "name": "Parse CC",
        "desc": "Parse CC statement PDFs to CSV + JSON",
        "phase": 2,
        "run_kwargs": {},
    },
    "5": {
        "script": "06_import_to_banking.py",
        "name": "Import Banking",
        "desc": "Import CC transactions to Zoho Banking",
        "phase": 2,
        "run_kwargs": {},
    },
    "6": {
        "script": "05_record_payments.py",
        "name": "Payments",
        "desc": "Record payments (match bills to CC)",
        "phase": 2,
        "run_kwargs": {},
    },
    "7": {
        "script": "07_auto_match.py",
        "name": "Auto-Match",
        "desc": "Auto-match transactions to paid bills",
        "phase": 2,
        "run_kwargs": {},
    },
    "review": {
        "script": None,
        "name": "Review Accounts",
        "desc": "Review and fix expense account assignments on bills",
        "phase": 1,
        "run_kwargs": {},
        "interactive": True,
        "skip_in_run_all": True,
    },
}

# --- In-memory state ---
_state_lock = threading.Lock()
_state = {
    "running": False,
    "current_step": None,
    "step_results": {},  # step_id -> {status, message, timestamp, result}
    "last_run_all": None,
}


def _import_script(filename):
    """Import a script from scripts/ folder by filename."""
    module_name = filename.replace(".py", "")
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(SCRIPTS_DIR, filename)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_step(step_id, extra_kwargs=None):
    """Execute a single pipeline step. Called from background thread."""
    step = STEPS[step_id]

    # Guard: interactive steps cannot be run as scripts
    if step.get("interactive"):
        log_action(f"Step '{step_id}' ({step['name']}) is interactive — use the UI panel instead.", "WARNING")
        with _state_lock:
            _state["step_results"][step_id] = {
                "status": "success",
                "message": "Interactive step — use UI panel",
                "timestamp": datetime.now().isoformat(),
            }
        return

    with _state_lock:
        _state["current_step"] = step_id
        _state["step_results"][step_id] = {
            "status": "running",
            "message": f"Running {step['name']}...",
            "timestamp": datetime.now().isoformat(),
        }

    log_action(f"=== Step {step_id}: {step['name']} ===")
    try:
        mod = _import_script(step["script"])
        kwargs = dict(step["run_kwargs"])
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        result = mod.run(**kwargs)
        result_dict = result if isinstance(result, dict) else {}

        with _state_lock:
            _state["step_results"][step_id] = {
                "status": "success",
                "message": _summarize_result(step_id, result_dict),
                "timestamp": datetime.now().isoformat(),
                "result": _safe_serialize(result_dict),
            }
        log_action(f"=== Step {step_id} DONE: {_state['step_results'][step_id]['message']} ===")
    except Exception as e:
        tb = traceback.format_exc()
        log_action(f"Step {step_id} FAILED: {e}", "ERROR")
        log_action(tb, "ERROR")
        with _state_lock:
            _state["step_results"][step_id] = {
                "status": "error",
                "message": str(e)[:200],
                "timestamp": datetime.now().isoformat(),
            }


def _safe_serialize(obj):
    """Make a result dict JSON-safe (convert sets, etc.)."""
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(v) for v in obj]
    if isinstance(obj, set):
        return list(obj)
    return obj


def _summarize_result(step_id, result):
    """Create a human-readable summary from step result dict."""
    if not result:
        return "Done"
    parts = []
    for key in ["downloaded_count", "new_count", "created_count", "total_transactions",
                 "paid_count", "imported_count", "matched_count"]:
        if key in result:
            label = key.replace("_", " ").replace("count", "").strip()
            parts.append(f"{label}: {result[key]}")
    for key in ["skipped_count", "total_count", "cards_parsed"]:
        if key in result:
            label = key.replace("_", " ").replace("count", "").strip()
            parts.append(f"{label}: {result[key]}")
    return " | ".join(parts) if parts else "Done"


def _run_step_thread(step_id, extra_kwargs=None):
    """Run a step in a background thread with lock management."""
    try:
        _run_step(step_id, extra_kwargs=extra_kwargs)
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


def _run_all_thread():
    """Run all steps sequentially in a background thread."""
    log_action("=" * 60)
    log_action("RUN ALL: Starting full pipeline (Steps 1-6)")
    log_action("=" * 60)
    with _state_lock:
        _state["last_run_all"] = datetime.now().isoformat()

    for step_id in ["1", "2", "3", "review", "4", "5", "6"]:
        step = STEPS[step_id]
        # Skip interactive steps in Run All
        if step.get("skip_in_run_all") or step.get("interactive"):
            log_action(f"Skipping '{step['name']}' (interactive step — use UI panel)")
            continue
        # Check if still supposed to be running (could be cancelled)
        with _state_lock:
            if not _state["running"]:
                log_action("Run All cancelled.", "WARNING")
                return
        _run_step(step_id)
        # Stop on error
        with _state_lock:
            if _state["step_results"].get(step_id, {}).get("status") == "error":
                log_action(f"Run All stopped: Step {step_id} failed.", "ERROR")
                break

    log_action("=" * 60)
    log_action("RUN ALL: Complete")
    log_action("=" * 60)

    with _state_lock:
        _state["running"] = False
        _state["current_step"] = None


def _run_cleanup_thread():
    """Run cleanup in a background thread."""
    log_action("=" * 60)
    log_action("CLEANUP: Starting complete cleanup")
    log_action("=" * 60)

    with _state_lock:
        _state["current_step"] = "cleanup"

    try:
        cleanup_mod = _import_script("cleanup_all.py")
        from utils import load_config, ZohoBooksAPI, resolve_account_ids

        config = load_config()
        api_obj = ZohoBooksAPI(config)
        resolve_account_ids(api_obj, config.get("credit_cards", []))

        cleanup_mod.cleanup_banking(api_obj, config)
        cleanup_mod.cleanup_vendor_payments(api_obj)
        cleanup_mod.cleanup_bills(api_obj)
        cleanup_mod.cleanup_vendors(api_obj)
        cleanup_mod.cleanup_remaining_bank_txns(api_obj, config)
        cleanup_mod.cleanup_local_files()

        log_action("CLEANUP: Complete")
        with _state_lock:
            _state["step_results"]["cleanup"] = {
                "status": "success",
                "message": "All Zoho data + local files cleaned",
                "timestamp": datetime.now().isoformat(),
            }
    except Exception as e:
        log_action(f"CLEANUP FAILED: {e}", "ERROR")
        log_action(traceback.format_exc(), "ERROR")
        with _state_lock:
            _state["step_results"]["cleanup"] = {
                "status": "error",
                "message": str(e)[:200],
                "timestamp": datetime.now().isoformat(),
            }
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


# --- Flask Routes ---

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/extract-zips", methods=["POST"])
def api_extract_zips():
    """Extract PDFs from ZIP files in 'all zips' folder, then run Step 2 to organize month-wise."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True

    def _extract_zips_thread():
        try:
            with _state_lock:
                _state["current_step"] = "extract-zips"
                _state["step_results"]["extract-zips"] = {
                    "status": "running",
                    "message": "Extracting ZIPs...",
                    "timestamp": datetime.now().isoformat(),
                }

            log_action("=== Extract ZIPs ===")

            # Extract PDFs from ZIPs into input_pdfs/invoices/
            mod_zip = _import_script("extract_zips.py")
            zip_results = mod_zip.extract_pdfs_all()

            msg = f"Extracted {zip_results['copied']} new PDFs from ZIPs into input_pdfs/invoices/"
            with _state_lock:
                _state["step_results"]["extract-zips"] = {
                    "status": "success",
                    "message": msg,
                    "timestamp": datetime.now().isoformat(),
                }
            log_action(f"=== Extract ZIPs DONE: {msg} — Run Extract Data next ===")
        except Exception as e:
            tb = traceback.format_exc()
            log_action(f"Extract ZIPs FAILED: {e}", "ERROR")
            log_action(tb, "ERROR")
            with _state_lock:
                _state["step_results"]["extract-zips"] = {
                    "status": "error",
                    "message": str(e)[:200],
                    "timestamp": datetime.now().isoformat(),
                }
        finally:
            with _state_lock:
                _state["running"] = False
                _state["current_step"] = None

    t = threading.Thread(target=_extract_zips_thread, daemon=True)
    t.start()
    return jsonify({"ok": True, "step": "extract-zips"})


@app.route("/api/run/<step>", methods=["POST"])
def api_run(step):
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True

    if step == "all":
        t = threading.Thread(target=_run_all_thread, daemon=True)
    elif step == "cleanup":
        t = threading.Thread(target=_run_cleanup_thread, daemon=True)
    elif step in STEPS:
        # Guard interactive steps from being "run"
        if STEPS[step].get("interactive"):
            with _state_lock:
                _state["running"] = False
            return jsonify({"error": f"'{STEPS[step]['name']}' is interactive — use the review panel UI"}), 400

        # Accept optional run_kwargs from JSON body
        extra_kwargs = None
        if request.is_json and request.json:
            extra_kwargs = request.json.get("run_kwargs")

        t = threading.Thread(target=_run_step_thread, args=(step, extra_kwargs), daemon=True)
    else:
        with _state_lock:
            _state["running"] = False
        return jsonify({"error": f"Unknown step: {step}"}), 400

    t.start()
    return jsonify({"ok": True, "step": step})


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify({
            "running": _state["running"],
            "current_step": _state["current_step"],
            "step_results": _state["step_results"],
            "last_run_all": _state["last_run_all"],
            "summary": _get_summary(),
        })


@app.route("/api/logs")
def api_logs_sse():
    """SSE endpoint for live log streaming."""
    q = queue.Queue(maxsize=500)
    _log_subscribers.append(q)

    def stream():
        try:
            while True:
                try:
                    line = q.get(timeout=30)
                    yield f"data: {json.dumps({'line': line})}\n\n"
                except queue.Empty:
                    # Send keepalive
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                _log_subscribers.remove(q)
            except ValueError:
                pass

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/logs/history")
def api_logs_history():
    """Return last N lines from automation.log."""
    log_path = os.path.join(PROJECT_ROOT, "output", "automation.log")
    n = request.args.get("n", 200, type=int)
    lines = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                lines = [l.rstrip() for l in all_lines[-n:]]
        except Exception:
            pass
    return jsonify({"lines": lines})


def _update_vendor_account_mapping(vendor_name, account_id, account_name):
    """Update vendor_mappings.json account_mappings so future bills auto-use the corrected account."""
    mappings_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    try:
        with open(mappings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "account_mappings" not in data:
            data["account_mappings"] = {}
        data["account_mappings"][vendor_name] = {
            "account_name": account_name,
            "account_id": account_id,
        }
        with open(mappings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        log_action(f"Updated vendor mapping: {vendor_name} -> {account_name}")
    except Exception as e:
        log_action(f"Failed to update vendor mappings: {e}", "WARNING")


@app.route("/api/review/bills")
def api_review_bills():
    """Load created_bills.json and resolve account names from local mappings (no per-bill API calls)."""
    bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
    if not os.path.exists(bills_path):
        return jsonify({"error": "No created_bills.json found. Run Step 3 first."}), 404

    try:
        with open(bills_path, "r", encoding="utf-8") as f:
            local_bills = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read bills file: {e}"}), 500

    # Load account mappings from vendor_mappings.json (same source Step 3 used)
    from utils import load_vendor_mappings
    vendor_mappings = load_vendor_mappings()
    account_mappings = vendor_mappings.get("account_mappings", {})
    default_account = vendor_mappings.get("default_expense_account", "Credit Card Charges")

    result = []
    for entry in local_bills:
        if entry.get("status") != "created" or not entry.get("bill_id"):
            continue

        vendor_name = entry.get("vendor_name", "Unknown")
        mapping = account_mappings.get(vendor_name, {})
        account_name = mapping.get("account_name", default_account)
        account_id = mapping.get("account_id", "")

        result.append({
            "bill_id": entry["bill_id"],
            "vendor_name": vendor_name,
            "amount": entry.get("amount"),
            "currency": entry.get("currency", "INR"),
            "account_id": account_id,
            "account_name": account_name,
        })

    return jsonify({"bills": result})


@app.route("/api/review/accounts")
def api_review_accounts():
    """Return sorted list of expense accounts for dropdowns."""
    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)
    try:
        accounts = api.get_expense_accounts()
        sorted_accounts = sorted(
            [{"account_id": aid, "account_name": aname} for aname, aid in accounts.items()],
            key=lambda x: x["account_name"],
        )
        return jsonify({"accounts": sorted_accounts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/review/update-account", methods=["POST"])
def api_review_update_account():
    """Update a bill's expense account in Zoho and cache in vendor_mappings."""
    data = request.json
    bill_id = data.get("bill_id")
    account_id = data.get("account_id")
    account_name = data.get("account_name", "")
    vendor_name = data.get("vendor_name", "")

    if not bill_id or not account_id:
        return jsonify({"error": "bill_id and account_id are required"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    try:
        # Fetch current bill to get full line_items
        bill_resp = api.get_bill(bill_id)
        bill = bill_resp.get("bill", {})
        line_items = bill.get("line_items", [])
        if not line_items:
            return jsonify({"error": "Bill has no line items"}), 400

        # Build clean line items with only writable fields for Zoho PUT
        clean_items = []
        for i, li in enumerate(line_items):
            clean = {
                "line_item_id": li.get("line_item_id"),
                "account_id": account_id if i == 0 else li.get("account_id"),
                "description": li.get("description", ""),
                "rate": li.get("rate", 0),
                "quantity": li.get("quantity", 1),
            }
            if li.get("tax_id"):
                clean["tax_id"] = li["tax_id"]
            if li.get("item_id"):
                clean["item_id"] = li["item_id"]
            clean_items.append(clean)

        result = api.update_bill(bill_id, {"line_items": clean_items})
        updated_bill = result.get("bill", {})
        updated_items = updated_bill.get("line_items", [])
        actual_account = updated_items[0].get("account_name", "?") if updated_items else "?"
        log_action(f"Updated bill {bill_id} account to {account_name} ({account_id}) — Zoho confirmed: {actual_account}")

        # Also update vendor_mappings.json for future auto-assignment
        if vendor_name and vendor_name != "Unknown":
            _update_vendor_account_mapping(vendor_name, account_id, account_name)

        return jsonify({"ok": True})
    except Exception as e:
        log_action(f"Failed to update bill account: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/review/bulk-update-account", methods=["POST"])
def api_review_bulk_update_account():
    """Update expense account for all bills of a vendor in one go."""
    data = request.json
    bill_ids = data.get("bill_ids", [])
    account_id = data.get("account_id")
    account_name = data.get("account_name", "")
    vendor_name = data.get("vendor_name", "")

    if not bill_ids or not account_id:
        return jsonify({"error": "bill_ids and account_id are required"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    succeeded = []
    failed = []

    for bill_id in bill_ids:
        try:
            bill_resp = api.get_bill(bill_id)
            bill = bill_resp.get("bill", {})
            line_items = bill.get("line_items", [])
            if not line_items:
                failed.append({"bill_id": bill_id, "error": "No line items"})
                continue

            # Build clean line items with only writable fields
            clean_items = []
            for i, li in enumerate(line_items):
                clean = {
                    "line_item_id": li.get("line_item_id"),
                    "account_id": account_id if i == 0 else li.get("account_id"),
                    "description": li.get("description", ""),
                    "rate": li.get("rate", 0),
                    "quantity": li.get("quantity", 1),
                }
                if li.get("tax_id"):
                    clean["tax_id"] = li["tax_id"]
                if li.get("item_id"):
                    clean["item_id"] = li["item_id"]
                clean_items.append(clean)

            api.update_bill(bill_id, {"line_items": clean_items})
            succeeded.append(bill_id)
            time.sleep(0.3)  # Rate limit
        except Exception as e:
            failed.append({"bill_id": bill_id, "error": str(e)})
            log_action(f"Bulk update failed for bill {bill_id}: {e}", "ERROR")

    # Update vendor mapping once for all bills
    if vendor_name and vendor_name != "Unknown" and succeeded:
        _update_vendor_account_mapping(vendor_name, account_id, account_name)

    log_action(f"Bulk updated {len(succeeded)}/{len(bill_ids)} bills for {vendor_name} -> {account_name}")
    return jsonify({"ok": True, "succeeded": succeeded, "failed": failed})


@app.route("/api/review/create-account", methods=["POST"])
def api_review_create_account():
    """Create a new expense account in Zoho COA."""
    data = request.json
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name:
        return jsonify({"error": "Account name is required"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    try:
        result = api.create_expense_account(name, description)
        account = result.get("account", {})
        return jsonify({
            "ok": True,
            "account_id": account.get("account_id"),
            "account_name": account.get("account_name", name),
        })
    except Exception as e:
        log_action(f"Failed to create expense account: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/review/available-csvs")
def api_available_csvs():
    """List parsed CC transaction CSVs available for import."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    config_path = os.path.join(PROJECT_ROOT, "config", "zoho_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}

    cards = config.get("credit_cards", [])
    available = []
    for card in cards:
        name = card["name"]
        safe_name = name.replace(" ", "_")
        csv_path = os.path.join(output_dir, f"{safe_name}_transactions.csv")
        if os.path.exists(csv_path):
            # Count rows
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    row_count = sum(1 for _ in f) - 1  # minus header
            except Exception:
                row_count = 0
            available.append({"card_name": name, "csv_file": f"{safe_name}_transactions.csv", "rows": row_count})
    return jsonify({"cards": available})


@app.route("/api/payments/preview")
def api_payments_preview():
    """Preview bill-to-CC-transaction matches.

    Fetches live from Zoho: unpaid bills + CC transactions from 4 configured cards.
    Matching priority: amount (INR or USD→INR) > forex > closest date > fuzzy vendor.
    """
    try:
        mod_05 = _import_script("05_record_payments.py")
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action
        from datetime import datetime as _dt

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # 1. Fetch unpaid bills from Zoho (live)
        zoho_bills = mod_05.fetch_unpaid_bills_from_zoho(api)
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
            for b in zoho_bills if b.get("bill_id")
        ]

        # 2. Fetch CC transactions from Zoho Banking (live, all 4 cards)
        cc_transactions = mod_05.fetch_cc_transactions_from_zoho(api, cards)

        # Build CC list (debits only)
        cc_list = []
        for t in cc_transactions:
            if float(t.get("amount", 0)) <= 0:
                continue
            cc_entry = {
                "transaction_id": t.get("transaction_id", ""),
                "description": t.get("description", ""),
                "amount": float(t.get("amount", 0)),
                "date": t.get("date", ""),
                "card_name": t.get("card_name", ""),
                "zoho_account_id": t.get("zoho_account_id", ""),
            }
            if t.get("forex_amount"):
                cc_entry["forex_amount"] = t["forex_amount"]
                cc_entry["forex_currency"] = t["forex_currency"]
            cc_list.append(cc_entry)

        # 3. Build vendor_mappings resolver for fuzzy vendor bonus
        vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
        vendor_map = {}
        vendor_map_norm = {}
        def _norm(s):
            return re.sub(r'[\s.\-,*()]+', '', s.lower())
        try:
            with open(vm_path, "r", encoding="utf-8") as f:
                vm = json.load(f)
            for k, v in vm.get("mappings", {}).items():
                vendor_map[k.lower()] = v
                vendor_map_norm[_norm(k)] = v
        except Exception:
            pass
        _sorted_keys = sorted(vendor_map.keys(), key=len, reverse=True)
        _sorted_norm_keys = sorted(vendor_map_norm.keys(), key=len, reverse=True)

        def _resolve_vendor(desc):
            if not desc:
                return None
            dl = desc.lower()
            dn = _norm(desc)
            if dl in vendor_map:
                return vendor_map[dl]
            if dn in vendor_map_norm:
                return vendor_map_norm[dn]
            for key in _sorted_keys:
                if key and len(key) >= 4 and key in dl:
                    return vendor_map[key]
            for key in _sorted_norm_keys:
                if key and len(key) >= 4 and key in dn:
                    return vendor_map_norm[key]
            return None

        def _vendor_match(bill_vendor, cc_desc):
            """Fuzzy vendor match: resolved CC vendor vs bill vendor name."""
            resolved = _resolve_vendor(cc_desc)
            if not resolved:
                return False
            rv = _norm(resolved)
            bv = _norm(bill_vendor)
            # Exact normalized match
            if rv == bv:
                return True
            # Substring match (either direction)
            if len(rv) >= 4 and (rv in bv or bv in rv):
                return True
            # First-word match (e.g. "microsoft" in "microsoftcorporationindiapvtltd")
            rv_first = _norm(resolved.split()[0]) if resolved.split() else ""
            if rv_first and len(rv_first) >= 4 and rv_first in bv:
                return True
            return False

        # 4. Two-phase matching (same logic as Monthly Compare categorize step):
        #    Phase 1: Vendor-first — group by resolved vendor, match by amount within vendor
        #    Phase 2: Amount fallback — unmatched bills try amount+date across all CC (tight tolerance)
        used_cc = set()  # indices of CC transactions already matched
        matches = []
        matched_count = 0
        unmatched_count = 0

        # --- Helper: amount-match a bill against a CC transaction ---
        def _amount_diff(bill, cc):
            """Return (diff, match_type) or (None, None) if not comparable."""
            bill_amt = bill["amount"]
            bill_cur = bill["currency"]
            cc_inr = cc["amount"]
            fx = cc.get("forex_amount")
            fx_cur = (cc.get("forex_currency") or "").upper()

            # Case 1: Same forex currency (e.g. USD bill, USD forex on CC)
            if fx and fx_cur and bill_cur.upper() == fx_cur:
                return abs(fx - bill_amt), f"{fx_cur} → {bill_cur}"
            # Case 2: INR bill, INR CC (no forex)
            if bill_cur == "INR" and not fx:
                return abs(cc_inr - bill_amt), "INR → INR"
            # Case 3: INR bill, CC has forex (compare INR amounts)
            if bill_cur == "INR" and fx:
                return abs(cc_inr - bill_amt), f"{fx_cur} → INR (forex)"
            # Case 4: USD bill, no forex tag — estimate INR range (75-100)
            if bill_cur == "USD" and not fx:
                est_min = bill_amt * 75
                est_max = bill_amt * 100
                if est_min <= cc_inr <= est_max:
                    return abs(cc_inr - bill_amt * 86), "USD → INR (est)"
            return None, None

        # --- Confidence scoring helper ---
        def _compute_confidence(bill, cc, cc_resolved_vendor):
            """Compute vendor/amount/date confidence (0-100 each) for a bill-CC match."""
            # Vendor confidence
            vendor_conf = 0
            if cc_resolved_vendor:
                rv = _norm(cc_resolved_vendor)
                bv = _norm(bill["vendor_name"])
                if rv == bv:
                    vendor_conf = 100
                elif len(rv) >= 4 and (rv in bv or bv in rv):
                    vendor_conf = 80
                else:
                    rv_first = _norm(cc_resolved_vendor.split()[0]) if cc_resolved_vendor.split() else ""
                    if rv_first and len(rv_first) >= 4 and rv_first in bv:
                        vendor_conf = 60

            # Amount confidence
            amount_conf = 0
            diff, mtype = _amount_diff(bill, cc)
            if diff is not None:
                bill_amt = bill["amount"] if bill["amount"] else 1
                pct_diff = diff / bill_amt
                if pct_diff < 0.001:
                    amount_conf = 100
                elif pct_diff < 0.005:
                    amount_conf = 95
                elif pct_diff < 0.01:
                    amount_conf = 90
                elif pct_diff < 0.03:
                    amount_conf = 75
                elif pct_diff < 0.05:
                    amount_conf = 60
                else:
                    amount_conf = 40
                # Forex exact match gets bonus
                if mtype and "est" in mtype:
                    amount_conf = min(amount_conf, 70)

            # Date confidence
            date_conf = 0
            try:
                bd = _dt.strptime(bill["date"], "%Y-%m-%d")
                cd = _dt.strptime(cc["date"], "%Y-%m-%d")
                day_diff = abs((bd - cd).days)
                if day_diff == 0:
                    date_conf = 100
                elif day_diff <= 2:
                    date_conf = 90
                elif day_diff <= 5:
                    date_conf = 75
                elif day_diff <= 10:
                    date_conf = 50
                elif day_diff <= 30:
                    date_conf = 25
            except Exception:
                pass

            overall = int(vendor_conf * 0.4 + amount_conf * 0.4 + date_conf * 0.2)
            return {
                "vendor": vendor_conf,
                "amount": amount_conf,
                "date": date_conf,
                "overall": overall,
            }

        # --- Resolve CC vendor names for grouping ---
        cc_vendors = []  # parallel to cc_list: resolved vendor name or None
        for cc in cc_list:
            cc_vendors.append(_resolve_vendor(cc.get("description", "")))

        # --- Phase 1: Vendor-first matching ---
        # Group bills by normalized vendor name
        from collections import defaultdict
        bill_vendor_groups = defaultdict(list)  # norm_vendor -> [bill_index]
        for bi, bill in enumerate(bills):
            bv = _norm(bill["vendor_name"])
            bill_vendor_groups[bv].append(bi)

        # Also map via vendor_mappings: resolved CC vendor → normalized
        bill_matched = [False] * len(bills)

        for ci, cc in enumerate(cc_list):
            if ci in used_cc:
                continue
            rv = cc_vendors[ci]
            if not rv:
                continue
            rv_norm = _norm(rv)

            # Find bills whose vendor matches this CC's resolved vendor
            candidate_bills = []
            for bv_norm, bill_idxs in bill_vendor_groups.items():
                if rv_norm == bv_norm:
                    candidate_bills.extend(bill_idxs)
                elif len(rv_norm) >= 4 and (rv_norm in bv_norm or bv_norm in rv_norm):
                    candidate_bills.extend(bill_idxs)
                else:
                    # First-word match
                    rv_first = _norm(rv.split()[0]) if rv.split() else ""
                    if rv_first and len(rv_first) >= 4 and rv_first in bv_norm:
                        candidate_bills.extend(bill_idxs)

            # Among vendor-matched bills, find best amount match (1% tolerance)
            # When amounts tie, prefer closest date
            best_bi = None
            best_diff = float("inf")
            best_date_diff = float("inf")
            for bi in candidate_bills:
                if bill_matched[bi]:
                    continue
                diff, mtype = _amount_diff(bills[bi], cc)
                if diff is None:
                    continue
                threshold = max(1.0, bills[bi]["amount"] * 0.01)
                if diff > threshold:
                    continue
                # Date proximity as tiebreaker
                try:
                    bd = _dt.strptime(bills[bi]["date"], "%Y-%m-%d")
                    cd = _dt.strptime(cc["date"], "%Y-%m-%d")
                    dd = abs((bd - cd).days)
                except Exception:
                    dd = 9999
                if diff < best_diff or (diff == best_diff and dd < best_date_diff):
                    best_diff = diff
                    best_date_diff = dd
                    best_bi = bi

            if best_bi is not None:
                bill = bills[best_bi]
                bill_matched[best_bi] = True
                used_cc.add(ci)
                conf = _compute_confidence(bill, cc, rv)
                entry = {
                    "bill_id": bill["bill_id"],
                    "vendor_id": bill["vendor_id"],
                    "vendor_name": bill["vendor_name"],
                    "bill_amount": bill["amount"],
                    "bill_currency": bill["currency"],
                    "bill_date": bill["date"],
                    "bill_number": bill["file"],
                    "status": "matched",
                    "match_score": 300,
                    "confidence": conf,
                    "cc_transaction_id": cc.get("transaction_id", ""),
                    "cc_description": cc.get("description", ""),
                    "cc_inr_amount": cc.get("amount", 0),
                    "cc_date": cc.get("date", ""),
                    "cc_card": cc.get("card_name", ""),
                }
                if cc.get("forex_amount"):
                    entry["cc_forex_amount"] = cc["forex_amount"]
                    entry["cc_forex_currency"] = cc["forex_currency"]
                matched_count += 1
                matches.append(entry)

        # --- Phase 2: Amount+date fallback for unmatched bills ---
        for bi, bill in enumerate(bills):
            if bill_matched[bi]:
                continue

            entry = {
                "bill_id": bill["bill_id"],
                "vendor_id": bill["vendor_id"],
                "vendor_name": bill["vendor_name"],
                "bill_amount": bill["amount"],
                "bill_currency": bill["currency"],
                "bill_date": bill["date"],
                "bill_number": bill["file"],
            }

            best = None
            best_diff = float("inf")
            best_idx = None

            for ci, cc in enumerate(cc_list):
                if ci in used_cc:
                    continue

                # Date limit: within 30 days
                try:
                    bd = _dt.strptime(bill["date"], "%Y-%m-%d")
                    cd = _dt.strptime(cc["date"], "%Y-%m-%d")
                    if abs((bd - cd).days) > 30:
                        continue
                except Exception:
                    continue

                diff, mtype = _amount_diff(bill, cc)
                if diff is None:
                    continue
                threshold = max(1.0, bill["amount"] * 0.01)
                if diff <= threshold and diff < best_diff:
                    best_diff = diff
                    best = cc
                    best_idx = ci

            if best is not None:
                used_cc.add(best_idx)
                bill_matched[bi] = True
                best_resolved = cc_vendors[best_idx] if best_idx < len(cc_vendors) else None
                conf = _compute_confidence(bill, best, best_resolved)
                entry["status"] = "matched"
                entry["match_score"] = 200
                entry["confidence"] = conf
                entry["cc_transaction_id"] = best.get("transaction_id", "")
                entry["cc_description"] = best.get("description", "")
                entry["cc_inr_amount"] = best.get("amount", 0)
                entry["cc_date"] = best.get("date", "")
                entry["cc_card"] = best.get("card_name", "")
                if best.get("forex_amount"):
                    entry["cc_forex_amount"] = best["forex_amount"]
                    entry["cc_forex_currency"] = best["forex_currency"]
                matched_count += 1
            else:
                entry["status"] = "unmatched"
                unmatched_count += 1

            matches.append(entry)

        # Collect unmatched CC transactions
        unmatched_cc = [cc_list[i] for i in range(len(cc_list)) if i not in used_cc]

        # Collect unique card names from config for filter dropdown
        card_names = [c.get("name", "") for c in cards if c.get("name")]

        # Per-card counts: total CC transactions and uncategorized (unmatched to bills)
        card_cc_total = {}
        for cc in cc_list:
            cn = cc.get("card_name", "")
            card_cc_total[cn] = card_cc_total.get(cn, 0) + 1
        card_cc_unmatched = {}
        for cc in unmatched_cc:
            cn = cc.get("card_name", "")
            card_cc_unmatched[cn] = card_cc_unmatched.get(cn, 0) + 1

        return jsonify({
            "matches": matches,
            "unmatched_cc": unmatched_cc,
            "card_names": card_names,
            "card_cc_total": card_cc_total,
            "card_cc_unmatched": card_cc_unmatched,
            "summary": {
                "total_bills": len(matches),
                "matched": matched_count,
                "unmatched": unmatched_count,
                "unmatched_cc_count": len(unmatched_cc),
            },
        })
    except Exception as e:
        from scripts.utils import log_action
        log_action(f"payments/preview error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _auto_match_banking_txn(api, cc_txn_id, payment_id, log_action,
                            account_id=None, cc_amount=None, cc_date=None):
    """After recording a vendor payment, auto-match the CC banking transaction.

    Fetches Zoho's suggested matches for the banking txn, finds the one
    matching our payment_id, and calls match_transaction to categorize it.
    If cc_txn_id is not provided, searches uncategorized transactions by amount+date.
    """
    # If no cc_txn_id, try to find it from uncategorized banking transactions
    if not cc_txn_id and account_id and cc_amount:
        try:
            from scripts.utils import log_action as _la
            result = api.list_uncategorized(account_id)
            txns = result.get("banktransactions", [])
            target_amount = round(float(cc_amount), 2)
            for t in txns:
                t_amount = abs(round(float(t.get("amount", 0)), 2))
                t_date = t.get("date", "")
                if t_amount == target_amount:
                    if not cc_date or t_date == cc_date:
                        cc_txn_id = t.get("transaction_id")
                        log_action(f"  Auto-match: found banking txn {cc_txn_id} by amount {target_amount}" +
                                   (f" on {cc_date}" if cc_date else ""))
                        break
        except Exception as e:
            log_action(f"  Auto-match: failed to search uncategorized: {e}", "WARNING")

    if not cc_txn_id:
        log_action("  Auto-match skipped: no cc_transaction_id found")
        return False
    try:
        import time
        # Small delay for Zoho to register the payment before matching
        time.sleep(0.5)

        match_result = api.get_matching_transactions(cc_txn_id)
        candidates = match_result.get("matching_transactions", [])

        if not candidates:
            log_action(f"  Auto-match: no candidates found for banking txn {cc_txn_id}")
            return False

        # Find the vendor_payment we just created
        target = None
        for c in candidates:
            if c.get("transaction_id") == payment_id:
                target = c
                break
            # Also match by transaction_type = vendor_payment (if payment_id matches reference)
            if c.get("transaction_type") == "vendor_payment" and c.get("payment_id") == payment_id:
                target = c
                break

        if not target:
            # Fallback: try the first vendor_payment candidate (likely the one we just created)
            for c in candidates:
                if c.get("transaction_type") == "vendor_payment":
                    target = c
                    log_action(f"  Auto-match: using first vendor_payment candidate")
                    break

        if target:
            match_data = [{
                "transaction_id": target.get("transaction_id"),
                "transaction_type": target.get("transaction_type", "vendor_payment"),
            }]
            api.match_transaction(cc_txn_id, match_data)
            log_action(f"  Auto-match: banking txn {cc_txn_id} -> categorized")
            return True
        else:
            log_action(f"  Auto-match: payment {payment_id} not found in {len(candidates)} candidates")
            return False

    except Exception as e:
        error_msg = str(e).lower()
        if "already" in error_msg:
            log_action(f"  Auto-match: already categorized")
            return True
        log_action(f"  Auto-match failed: {e}", "WARNING")
        return False


@app.route("/api/payments/record-one", methods=["POST"])
def api_payments_record_one():
    """Record payment for a single bill using CC transaction from preview."""
    data = request.json or {}
    bill_id = data.get("bill_id")
    if not bill_id:
        return jsonify({"error": "bill_id required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        currency_map = api.list_currencies()

        # Fetch the bill from Zoho
        bill_data = api.get_bill(bill_id)
        bill = bill_data.get("bill", {})
        bill_total = float(bill.get("total", 0))
        bill_currency = bill.get("currency_code", "INR")
        vendor_id = bill.get("vendor_id", "")

        # Use CC match from preview (passed from frontend)
        cc_inr = data.get("cc_inr_amount")
        cc_date = data.get("cc_date")
        cc_card = data.get("cc_card")

        if not cc_inr or not cc_date:
            return jsonify({"status": "unmatched", "bill_id": bill_id, "message": "No CC match data provided"})

        # Resolve CC card -> zoho_account_id
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found in config"}), 400

        # Build payment
        payment_date = cc_date or bill.get("date", "")
        payment_data = {
            "vendor_id": vendor_id,
            "payment_mode": "Credit Card",
            "date": payment_date,
            "amount": bill_total,
            "paid_through_account_id": account_id,
            "bills": [{"bill_id": bill_id, "amount_applied": bill_total}],
        }

        # Handle foreign currency: calculate exchange rate from CC INR amount
        if bill_currency != "INR":
            actual_inr = float(cc_inr)
            if bill_total:
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
            payment_data["currency_id"] = currency_map.get(bill_currency)
            payment_data["exchange_rate"] = exact_rate
            log_action(f"  {bill_currency} {bill_total} -> INR {actual_inr} (rate: {exact_rate})")

        log_action(f"Recording payment: bill {bill_id} via {cc_card} on {payment_date} ({bill_currency} {bill_total})")

        result = api.record_vendor_payment(payment_data)
        payment = result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")

        if payment_id:
            log_action(f"  Payment recorded: {payment_id}")

            # Auto-match: categorize the CC banking transaction
            cc_txn_id = data.get("cc_transaction_id")
            matched_banking = _auto_match_banking_txn(
                api, cc_txn_id, payment_id, log_action,
                account_id=account_id, cc_amount=cc_inr, cc_date=cc_date,
            )

            return jsonify({"status": "paid", "payment_id": payment_id, "bill_id": bill_id, "banking_matched": matched_banking})
        else:
            return jsonify({"status": "failed", "bill_id": bill_id, "message": "No payment_id returned"})

    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            log_action(f"  Bill {bill_id} already paid")
            return jsonify({"status": "already_paid", "bill_id": bill_id})
        from scripts.utils import log_action
        log_action(f"record-one error for {bill_id}: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/payments/record-selected", methods=["POST"])
def api_payments_record_selected():
    """Record payments for multiple bills using CC match data from preview."""
    data = request.json or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action
        import time

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)
        currency_map = api.list_currencies()

        # Build card name -> account_id map
        card_map = {c.get("name"): c.get("zoho_account_id") for c in cards}

        results = []
        for item in items:
            bill_id = item.get("bill_id")
            if not bill_id:
                continue
            try:
                bill_data = api.get_bill(bill_id)
                bill = bill_data.get("bill", {})
                bill_total = float(bill.get("total", 0))
                bill_currency = bill.get("currency_code", "INR")
                vendor_id = bill.get("vendor_id", "")

                cc_card = item.get("cc_card", "")
                cc_inr = item.get("cc_inr_amount")
                cc_date = item.get("cc_date")
                account_id = card_map.get(cc_card)

                if not cc_inr or not cc_date or not account_id:
                    results.append({"bill_id": bill_id, "status": "unmatched"})
                    continue

                payment_date = cc_date
                payment_data = {
                    "vendor_id": vendor_id,
                    "payment_mode": "Credit Card",
                    "date": payment_date,
                    "amount": bill_total,
                    "paid_through_account_id": account_id,
                    "bills": [{"bill_id": bill_id, "amount_applied": bill_total}],
                }

                if bill_currency != "INR":
                    actual_inr = float(cc_inr)
                    if bill_total:
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
                    payment_data["currency_id"] = currency_map.get(bill_currency)
                    payment_data["exchange_rate"] = exact_rate

                log_action(f"Recording payment: bill {bill_id} via {cc_card} ({bill_currency} {bill_total})")
                result = api.record_vendor_payment(payment_data)
                payment_id = result.get("vendorpayment", {}).get("payment_id")

                if payment_id:
                    log_action(f"  Payment recorded: {payment_id}")

                    # Auto-match: categorize the CC banking transaction
                    cc_txn_id = item.get("cc_transaction_id")
                    _auto_match_banking_txn(api, cc_txn_id, payment_id, log_action)

                    results.append({"bill_id": bill_id, "status": "paid", "payment_id": payment_id})
                else:
                    results.append({"bill_id": bill_id, "status": "failed"})

            except Exception as e:
                error_msg = str(e).lower()
                if "already been paid" in error_msg or "already paid" in error_msg:
                    results.append({"bill_id": bill_id, "status": "already_paid"})
                else:
                    log_action(f"record-selected error for {bill_id}: {e}", "ERROR")
                    results.append({"bill_id": bill_id, "status": "error", "message": str(e)})

            time.sleep(0.3)

        paid = sum(1 for r in results if r["status"] == "paid")
        return jsonify({"results": results, "paid_count": paid, "total": len(results)})
    except Exception as e:
        from scripts.utils import log_action
        log_action(f"record-selected error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _update_payment_tracking(bill_entry, payment_id, cc_match):
    """Append to recorded_payments.json tracking file."""
    payments_file = os.path.join(PROJECT_ROOT, "output", "recorded_payments.json")
    payments = []
    if os.path.exists(payments_file):
        with open(payments_file, "r", encoding="utf-8") as f:
            payments = json.load(f)

    entry = {
        "file": bill_entry.get("file", ""),
        "bill_id": bill_entry["bill_id"],
        "vendor_name": bill_entry.get("vendor_name"),
        "amount": bill_entry.get("amount"),
        "currency": bill_entry.get("currency"),
        "payment_id": payment_id,
        "status": "paid",
    }
    if cc_match:
        entry["cc_inr_amount"] = cc_match.get("inr_amount")
        entry["cc_card"] = cc_match.get("card_name")

    # Remove any existing entry for this bill_id
    payments = [p for p in payments if p.get("bill_id") != bill_entry["bill_id"]]
    payments.append(entry)

    os.makedirs(os.path.dirname(payments_file), exist_ok=True)
    with open(payments_file, "w", encoding="utf-8") as f:
        json.dump(payments, f, indent=2, ensure_ascii=False)


@app.route("/api/bills/create-one", methods=["POST"])
def api_bills_create_one():
    """Create a single bill in Zoho from invoice data (used by Monthly Compare exact match rows)."""
    data = request.json or {}
    vendor_name = data.get("vendor_name", "")
    amount = data.get("amount")
    currency = data.get("currency", "INR")
    date = data.get("date")
    invoice_number = data.get("invoice_number", "")
    vendor_gstin = data.get("vendor_gstin", "")

    if not vendor_name or not amount or not date:
        return jsonify({"error": "vendor_name, amount, and date are required"}), 400

    try:
        mod_03 = _import_script("03_create_vendors_bills.py")
        from scripts.utils import load_config, load_vendor_mappings, ZohoBooksAPI, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        vendor_mappings = load_vendor_mappings()
        currency_map = api.list_currencies()

        # Resolve vendor (find or create)
        invoice = {
            "file": invoice_number or vendor_name,
            "vendor_name": vendor_name,
            "vendor_gstin": vendor_gstin,
            "amount": float(amount),
            "currency": currency,
            "date": date,
            "invoice_number": invoice_number,
        }

        vendor_id, resolved_name = mod_03.ensure_vendor(
            api, vendor_name, invoice, vendor_mappings, currency_map,
        )
        if not vendor_id:
            return jsonify({"error": f"Could not find or create vendor '{vendor_name}'"}), 500

        # Get expense accounts
        expense_accounts = api.get_expense_accounts()

        # Get tax IDs
        igst_tax_id = None
        intrastate_tax_id = None
        default_exemption_id = None
        try:
            taxes = api.list_taxes()
            for t in taxes:
                tname = t.get("tax_name", "").lower()
                if "igst" in tname and t.get("tax_percentage") == 18:
                    igst_tax_id = t.get("tax_id")
                if ("cgst" in tname or "sgst" in tname) and t.get("tax_percentage") == 9:
                    intrastate_tax_id = intrastate_tax_id or t.get("tax_id")
            exemptions = api.list_tax_exemptions()
            for ex in exemptions:
                if "non" in ex.get("exemption_reason", "").lower():
                    default_exemption_id = ex.get("tax_exemption_id")
                    break
        except Exception:
            pass

        default_expense = config.get("default_expense_account", "Credit Card Charges")
        result = mod_03.create_bill_for_invoice(
            api, invoice, vendor_id, expense_accounts, default_expense, currency_map,
            vendor_name=resolved_name,
            igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
            default_exemption_id=default_exemption_id,
        )

        if result and result[0]:
            bill_id = result[0]
            is_new = result[1]
            status = "created" if is_new else "exists"
            log_action(f"Bill {status}: {invoice_number or vendor_name} -> {bill_id}")

            # Append to created_bills.json so Review Accounts panel can see it
            bills_file = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
            try:
                existing = []
                if os.path.exists(bills_file):
                    with open(bills_file, "r") as f:
                        existing = json.load(f)
                existing.append({
                    "file": invoice_number or vendor_name,
                    "status": status,
                    "vendor_name": resolved_name or vendor_name,
                    "vendor_id": vendor_id,
                    "bill_id": bill_id,
                    "amount": float(amount),
                    "currency": currency,
                    "attached": False,
                })
                with open(bills_file, "w") as f:
                    json.dump(existing, f, indent=2)
            except Exception as ex:
                log_action(f"Warning: could not update created_bills.json: {ex}", "WARN")

            return jsonify({"status": status, "bill_id": bill_id, "bill_number": invoice_number})
        else:
            return jsonify({"error": "Failed to create bill"}), 500

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"create-one bill error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bills/create-and-record", methods=["POST"])
def api_bills_create_and_record():
    """Create bill + record payment + auto-match banking transaction in one step."""
    data = request.json or {}
    inv = data.get("invoice", {})
    cc = data.get("cc", {})

    vendor_name = inv.get("vendor_name", "")
    amount = inv.get("amount")
    currency = inv.get("currency", "INR")
    date = inv.get("date")
    invoice_number = inv.get("invoice_number", "")
    vendor_gstin = inv.get("vendor_gstin", "")

    cc_inr = cc.get("amount")
    cc_date = cc.get("date")
    cc_card = cc.get("card_name")
    cc_txn_id = cc.get("transaction_id", "")

    # For foreign currency invoices, prefer the CC-mapped vendor name if available
    # (e.g., CC maps "CLAUDE.AI SUBSCRIPTION" -> "Anthropic USD" while invoice says "Anthropic")
    cc_vendor = cc.get("vendor_name", "")
    if currency != "INR" and cc_vendor and cc_vendor != vendor_name:
        vendor_name = cc_vendor

    if not vendor_name or not amount or not date:
        return jsonify({"error": "invoice vendor_name, amount, date required"}), 400
    if not cc_inr or not cc_date or not cc_card:
        return jsonify({"error": "cc amount, date, card_name required"}), 400

    try:
        mod_03 = _import_script("03_create_vendors_bills.py")
        from scripts.utils import load_config, load_vendor_mappings, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        vendor_mappings = load_vendor_mappings()
        currency_map = api.list_currencies()
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # --- Step 1: Create bill ---
        invoice = {
            "file": invoice_number or vendor_name,
            "vendor_name": vendor_name,
            "vendor_gstin": vendor_gstin,
            "amount": float(amount),
            "currency": currency,
            "date": date,
            "invoice_number": invoice_number,
        }

        vendor_id, resolved_name = mod_03.ensure_vendor(
            api, vendor_name, invoice, vendor_mappings, currency_map,
        )
        if not vendor_id:
            return jsonify({"error": f"Could not find or create vendor '{vendor_name}'"}), 500

        expense_accounts = api.get_expense_accounts()
        igst_tax_id = None
        intrastate_tax_id = None
        default_exemption_id = None
        try:
            taxes = api.list_taxes()
            for t in taxes:
                tname = t.get("tax_name", "").lower()
                if "igst" in tname and t.get("tax_percentage") == 18:
                    igst_tax_id = t.get("tax_id")
                if ("cgst" in tname or "sgst" in tname) and t.get("tax_percentage") == 9:
                    intrastate_tax_id = intrastate_tax_id or t.get("tax_id")
            exemptions = api.list_tax_exemptions()
            for ex in exemptions:
                if "non" in ex.get("exemption_reason", "").lower():
                    default_exemption_id = ex.get("tax_exemption_id")
                    break
        except Exception:
            pass

        default_expense = config.get("default_expense_account", "Credit Card Charges")
        result = mod_03.create_bill_for_invoice(
            api, invoice, vendor_id, expense_accounts, default_expense, currency_map,
            vendor_name=resolved_name,
            igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
            default_exemption_id=default_exemption_id,
        )

        if not result or not result[0]:
            return jsonify({"error": "Failed to create bill"}), 500

        bill_id = result[0]
        is_new = result[1]
        bill_status = "created" if is_new else "exists"
        log_action(f"Bill {bill_status}: {invoice_number or vendor_name} -> {bill_id}")

        # Save to created_bills.json
        bills_file = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
        try:
            existing = []
            if os.path.exists(bills_file):
                with open(bills_file, "r") as f:
                    existing = json.load(f)
            existing.append({
                "file": invoice_number or vendor_name,
                "status": bill_status,
                "vendor_name": resolved_name or vendor_name,
                "vendor_id": vendor_id,
                "bill_id": bill_id,
                "amount": float(amount),
                "currency": currency,
                "attached": False,
            })
            with open(bills_file, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass

        # --- Step 2: Record payment ---
        bill_data = api.get_bill(bill_id)
        bill = bill_data.get("bill", {})
        bill_total = float(bill.get("total", 0))
        bill_currency = bill.get("currency_code", "INR")

        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"status": "bill_created", "bill_id": bill_id, "error": f"CC card '{cc_card}' not found"})

        payment_date = cc_date or bill.get("date", "")
        payment_data = {
            "vendor_id": vendor_id,
            "payment_mode": "Credit Card",
            "date": payment_date,
            "amount": bill_total,
            "paid_through_account_id": account_id,
            "bills": [{"bill_id": bill_id, "amount_applied": bill_total}],
        }

        if bill_currency != "INR":
            actual_inr = float(cc_inr)
            if bill_total:
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
            payment_data["currency_id"] = currency_map.get(bill_currency)
            payment_data["exchange_rate"] = exact_rate
            log_action(f"  {bill_currency} {bill_total} -> INR {actual_inr} (rate: {exact_rate})")

        log_action(f"Recording payment: bill {bill_id} via {cc_card} on {payment_date}")
        pay_result = api.record_vendor_payment(payment_data)
        payment = pay_result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")

        if not payment_id:
            return jsonify({"status": "bill_created", "bill_id": bill_id, "error": "Payment failed - no payment_id"})

        log_action(f"  Payment recorded: {payment_id}")

        # --- Step 3: Auto-match banking transaction ---
        matched_banking = _auto_match_banking_txn(
            api, cc_txn_id, payment_id, log_action,
            account_id=account_id, cc_amount=cc_inr, cc_date=cc_date,
        )

        return jsonify({
            "status": "paid",
            "bill_id": bill_id,
            "payment_id": payment_id,
            "banking_matched": matched_banking,
        })

    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            return jsonify({"status": "already_paid", "bill_id": data.get("bill_id", "")})
        from scripts.utils import log_action
        log_action(f"create-and-record error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bills/create-and-record-bulk", methods=["POST"])
def api_bills_create_and_record_bulk():
    """Create bills 1-by-1, record payment for each, then auto-match all payments with CC banking txn.
    Used for grouped invoices (multiple invoices matched to 1 CC charge).
    """
    data = request.json or {}
    invoices = data.get("invoices", [])
    cc = data.get("cc", {})

    cc_inr = cc.get("amount")
    cc_date = cc.get("date")
    cc_card = cc.get("card_name")
    cc_txn_id = cc.get("transaction_id", "")
    cc_vendor = cc.get("vendor_name", "")

    if not invoices:
        return jsonify({"error": "invoices array required"}), 400
    if not cc_inr or not cc_date or not cc_card:
        return jsonify({"error": "cc amount, date, card_name required"}), 400

    try:
        mod_03 = _import_script("03_create_vendors_bills.py")
        from scripts.utils import load_config, load_vendor_mappings, ZohoBooksAPI, resolve_account_ids, log_action
        import time

        config = load_config()
        api = ZohoBooksAPI(config)
        vendor_mappings = load_vendor_mappings()
        currency_map = api.list_currencies()
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # Find CC card account
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found"}), 400

        # Load tax info once
        expense_accounts = api.get_expense_accounts()
        igst_tax_id = None
        intrastate_tax_id = None
        default_exemption_id = None
        try:
            taxes = api.list_taxes()
            for t in taxes:
                tname = t.get("tax_name", "").lower()
                if "igst" in tname and t.get("tax_percentage") == 18:
                    igst_tax_id = t.get("tax_id")
                if ("cgst" in tname or "sgst" in tname) and t.get("tax_percentage") == 9:
                    intrastate_tax_id = intrastate_tax_id or t.get("tax_id")
            exemptions = api.list_tax_exemptions()
            for ex in exemptions:
                if "non" in ex.get("exemption_reason", "").lower():
                    default_exemption_id = ex.get("tax_exemption_id")
                    break
        except Exception:
            pass

        default_expense = config.get("default_expense_account", "Credit Card Charges")
        results = []
        payment_ids = []

        log_action(f"[Bulk] Creating {len(invoices)} bills for CC charge INR {cc_inr} on {cc_date}")

        for idx, inv in enumerate(invoices):
            vendor_name = inv.get("vendor_name", "")
            amount = inv.get("amount")
            currency = inv.get("currency", "INR")
            inv_date = inv.get("date")
            invoice_number = inv.get("invoice_number", "")
            vendor_gstin = inv.get("vendor_gstin", "")

            # For foreign currency, prefer CC-mapped vendor name
            if currency != "INR" and cc_vendor and cc_vendor != vendor_name:
                vendor_name = cc_vendor

            log_action(f"  [{idx+1}/{len(invoices)}] Creating bill: {invoice_number or vendor_name} ({currency} {amount})")

            # --- Step 1: Create bill ---
            invoice_obj = {
                "file": invoice_number or vendor_name,
                "vendor_name": vendor_name,
                "vendor_gstin": vendor_gstin,
                "amount": float(amount),
                "currency": currency,
                "date": inv_date,
                "invoice_number": invoice_number,
            }

            try:
                vendor_id, resolved_name = mod_03.ensure_vendor(
                    api, vendor_name, invoice_obj, vendor_mappings, currency_map,
                )
                if not vendor_id:
                    log_action(f"  [{idx+1}] Failed: could not find/create vendor '{vendor_name}'", "ERROR")
                    results.append({"invoice_number": invoice_number, "status": "error", "error": f"Vendor not found: {vendor_name}"})
                    continue

                result = mod_03.create_bill_for_invoice(
                    api, invoice_obj, vendor_id, expense_accounts, default_expense, currency_map,
                    vendor_name=resolved_name,
                    igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
                    default_exemption_id=default_exemption_id,
                )

                if not result or not result[0]:
                    log_action(f"  [{idx+1}] Failed: could not create bill", "ERROR")
                    results.append({"invoice_number": invoice_number, "status": "error", "error": "Failed to create bill"})
                    continue

                bill_id = result[0]
                is_new = result[1]
                log_action(f"  [{idx+1}] Bill {'created' if is_new else 'exists'}: {bill_id}")

                # --- Step 2: Record payment for this bill ---
                bill_data = api.get_bill(bill_id)
                bill = bill_data.get("bill", {})
                bill_status = bill.get("status", "")
                bill_total = float(bill.get("total", 0))
                bill_currency = bill.get("currency_code", "INR")
                balance = float(bill.get("balance", bill_total))

                if bill_status == "paid" or balance <= 0:
                    log_action(f"  [{idx+1}] Already paid, skipping payment")
                    results.append({"invoice_number": invoice_number, "status": "already_paid", "bill_id": bill_id})
                    continue

                payment_date = cc_date
                payment_data = {
                    "vendor_id": vendor_id,
                    "payment_mode": "Credit Card",
                    "date": payment_date,
                    "amount": balance,
                    "paid_through_account_id": account_id,
                    "bills": [{"bill_id": bill_id, "amount_applied": balance}],
                }

                if bill_currency != "INR":
                    actual_inr_portion = float(cc_inr) * (balance / float(inv.get("amount", balance)))
                    if balance:
                        exact_rate = actual_inr_portion / balance
                        for decimals in range(6, 12):
                            test_rate = round(exact_rate, decimals)
                            if round(test_rate * balance, 2) == round(actual_inr_portion, 2):
                                exact_rate = test_rate
                                break
                        else:
                            exact_rate = round(exact_rate, 10)
                    else:
                        exact_rate = 0
                    payment_data["currency_id"] = currency_map.get(bill_currency)
                    payment_data["exchange_rate"] = exact_rate

                pay_result = api.record_vendor_payment(payment_data)
                payment = pay_result.get("vendorpayment", {})
                payment_id = payment.get("payment_id")

                if payment_id:
                    log_action(f"  [{idx+1}] Payment recorded: {payment_id}")
                    payment_ids.append(payment_id)
                    results.append({"invoice_number": invoice_number, "status": "paid", "bill_id": bill_id, "payment_id": payment_id})
                else:
                    log_action(f"  [{idx+1}] Payment failed", "ERROR")
                    results.append({"invoice_number": invoice_number, "status": "bill_created", "bill_id": bill_id, "error": "Payment failed"})

            except Exception as ex:
                error_msg = str(ex).lower()
                if "already" in error_msg and "paid" in error_msg:
                    results.append({"invoice_number": invoice_number, "status": "already_paid"})
                    log_action(f"  [{idx+1}] Already paid")
                    continue
                log_action(f"  [{idx+1}] Error: {ex}", "ERROR")
                results.append({"invoice_number": invoice_number, "status": "error", "error": str(ex)})

        # --- Step 3: Auto-match all payments with the CC banking transaction ---
        matched_banking = False
        if payment_ids:
            log_action(f"[Bulk] Auto-matching {len(payment_ids)} payments to CC banking txn")
            time.sleep(1)  # Wait for Zoho to register all payments
            matched_banking = _auto_match_banking_txn_multi(
                api, cc_txn_id, payment_ids, log_action,
                account_id=account_id, cc_amount=cc_inr, cc_date=cc_date,
            )

        created_count = sum(1 for r in results if r.get("status") in ("paid", "bill_created"))
        paid_count = sum(1 for r in results if r.get("status") == "paid")
        already_paid_count = sum(1 for r in results if r.get("status") == "already_paid")
        bill_created_only = sum(1 for r in results if r.get("status") == "bill_created")
        error_count = sum(1 for r in results if r.get("status") == "error")
        log_action(f"[Bulk] Done: {created_count} created, {paid_count} recorded, {already_paid_count} skipped (already paid), {error_count} errors, banking_matched={matched_banking}")

        overall_status = "paid" if paid_count > 0 and error_count == 0 else ("partial" if paid_count > 0 else "error")
        return jsonify({
            "status": overall_status,
            "results": results,
            "banking_matched": matched_banking,
            "total": len(invoices),
            "created_count": created_count,
            "paid_count": paid_count,
            "already_paid_count": already_paid_count,
            "bill_created_only": bill_created_only,
            "error_count": error_count,
        })

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"create-and-record-bulk error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _auto_match_banking_txn_multi(api, cc_txn_id, payment_ids, log_action,
                                   account_id=None, cc_amount=None, cc_date=None):
    """Auto-match a CC banking transaction against multiple vendor payments."""
    import time

    # If no cc_txn_id, try to find it from uncategorized banking transactions
    if not cc_txn_id and account_id and cc_amount:
        try:
            result = api.list_uncategorized(account_id)
            txns = result.get("banktransactions", [])
            target_amount = round(float(cc_amount), 2)
            for t in txns:
                t_amount = abs(round(float(t.get("amount", 0)), 2))
                t_date = t.get("date", "")
                if t_amount == target_amount:
                    if not cc_date or t_date == cc_date:
                        cc_txn_id = t.get("transaction_id")
                        log_action(f"  Auto-match: found banking txn {cc_txn_id} by amount {target_amount}")
                        break
        except Exception as e:
            log_action(f"  Auto-match: failed to search uncategorized: {e}", "WARNING")

    if not cc_txn_id:
        log_action("  Auto-match skipped: no cc_transaction_id found")
        return False

    try:
        time.sleep(0.5)
        match_result = api.get_matching_transactions(cc_txn_id)
        candidates = match_result.get("matching_transactions", [])

        if not candidates:
            log_action(f"  Auto-match: no candidates found for banking txn {cc_txn_id}")
            return False

        # Find ALL vendor payments that match our payment_ids
        match_data = []
        for c in candidates:
            cid = c.get("transaction_id") or c.get("payment_id", "")
            if cid in payment_ids or c.get("payment_id", "") in payment_ids:
                match_data.append({
                    "transaction_id": c.get("transaction_id"),
                    "transaction_type": c.get("transaction_type", "vendor_payment"),
                })

        # Fallback: if we couldn't match by ID, grab all vendor_payment candidates
        if not match_data:
            for c in candidates:
                if c.get("transaction_type") == "vendor_payment":
                    match_data.append({
                        "transaction_id": c.get("transaction_id"),
                        "transaction_type": "vendor_payment",
                    })
            if match_data:
                log_action(f"  Auto-match: using {len(match_data)} vendor_payment candidates (fallback)")

        if match_data:
            api.match_transaction(cc_txn_id, match_data)
            log_action(f"  Auto-match: banking txn {cc_txn_id} -> categorized with {len(match_data)} payments")
            return True
        else:
            log_action(f"  Auto-match: payments not found in {len(candidates)} candidates")
            return False

    except Exception as e:
        error_msg = str(e).lower()
        if "already" in error_msg:
            log_action(f"  Auto-match: already categorized")
            return True
        log_action(f"  Auto-match failed: {e}", "WARNING")
        return False


@app.route("/api/bills/record-only", methods=["POST"])
def api_bills_record_only():
    """Record payment + auto-match banking for existing bill(s) (skip bill creation).
    Supports multiple bill_ids for grouped invoices (e.g., 2 bills matched to 1 CC charge).
    """
    data = request.json or {}
    bill_id = data.get("bill_id", "")
    bill_ids = data.get("bill_ids", [])
    cc = data.get("cc", {})
    cc_inr = cc.get("amount")
    cc_date = cc.get("date")
    cc_card = cc.get("card_name")
    cc_txn_id = cc.get("transaction_id", "")

    # Build list of bill IDs to process
    all_bill_ids = list(bill_ids) if bill_ids else []
    if bill_id and bill_id not in all_bill_ids:
        all_bill_ids.insert(0, bill_id)
    all_bill_ids = [b for b in all_bill_ids if b]

    if not all_bill_ids:
        return jsonify({"error": "bill_id or bill_ids required"}), 400
    if not cc_inr or not cc_date or not cc_card:
        return jsonify({"error": "cc amount, date, card_name required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)
        currency_map = api.list_currencies()

        # Find CC card account
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found"}), 400

        # Fetch all bill details and build payment
        bills_to_pay = []
        total_amount = 0
        bill_currency = "INR"
        vendor_id = None
        skipped = []

        for bid in all_bill_ids:
            try:
                bill_data = api.get_bill(bid)
                bill = bill_data.get("bill", {})
                status = bill.get("status", "")
                if status == "paid":
                    log_action(f"  Bill {bid} already paid, skipping")
                    skipped.append(bid)
                    continue
                bt = float(bill.get("total", 0))
                # Use balance_due if available (handles partial payments)
                balance = float(bill.get("balance", bt))
                if balance <= 0:
                    log_action(f"  Bill {bid} has zero balance, skipping")
                    skipped.append(bid)
                    continue
                bills_to_pay.append({"bill_id": bid, "amount_applied": balance})
                total_amount += balance
                bill_currency = bill.get("currency_code", "INR")
                if not vendor_id:
                    vendor_id = bill.get("vendor_id", "")
            except Exception as ex:
                error_msg = str(ex).lower()
                if "already" in error_msg and "paid" in error_msg:
                    skipped.append(bid)
                    continue
                raise

        if not bills_to_pay:
            if skipped:
                return jsonify({"status": "already_paid", "bill_id": all_bill_ids[0], "skipped": skipped})
            return jsonify({"error": "No bills to pay"}), 400

        if not vendor_id:
            return jsonify({"error": "Bill has no vendor_id"}), 500

        # Record single payment covering all bills
        payment_date = cc_date
        payment_data = {
            "vendor_id": vendor_id,
            "payment_mode": "Credit Card",
            "date": payment_date,
            "amount": round(total_amount, 2),
            "paid_through_account_id": account_id,
            "bills": bills_to_pay,
        }

        if bill_currency != "INR":
            actual_inr = float(cc_inr)
            if total_amount:
                exact_rate = actual_inr / total_amount
                for decimals in range(6, 12):
                    test_rate = round(exact_rate, decimals)
                    if round(test_rate * total_amount, 2) == round(actual_inr, 2):
                        exact_rate = test_rate
                        break
                else:
                    exact_rate = round(exact_rate, 10)
            else:
                exact_rate = 0
            payment_data["currency_id"] = currency_map.get(bill_currency)
            payment_data["exchange_rate"] = exact_rate
            log_action(f"  {bill_currency} {total_amount} -> INR {actual_inr} (rate: {exact_rate})")

        bill_count = len(bills_to_pay)
        log_action(f"Recording payment ({bill_count} bill{'s' if bill_count > 1 else ''}): {', '.join(b['bill_id'] for b in bills_to_pay)} via {cc_card} on {payment_date}")
        pay_result = api.record_vendor_payment(payment_data)
        payment = pay_result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")

        if not payment_id:
            return jsonify({"error": "Payment failed - no payment_id"}), 500

        log_action(f"  Payment recorded: {payment_id} (total: {round(total_amount, 2)})")

        # Auto-match banking transaction
        matched_banking = _auto_match_banking_txn(
            api, cc_txn_id, payment_id, log_action,
            account_id=account_id, cc_amount=cc_inr, cc_date=cc_date,
        )

        return jsonify({
            "status": "paid",
            "bill_id": all_bill_ids[0],
            "bill_ids": [b["bill_id"] for b in bills_to_pay],
            "skipped": skipped,
            "payment_id": payment_id,
            "banking_matched": matched_banking,
        })

    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            return jsonify({"status": "already_paid", "bill_id": all_bill_ids[0]})
        from scripts.utils import log_action
        log_action(f"record-only error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/payments/clear-cache", methods=["POST"])
def api_payments_clear_cache():
    """Clear recorded_payments.json so Step 6 re-tries all bills."""
    payments_file = os.path.join(PROJECT_ROOT, "output", "recorded_payments.json")
    if os.path.exists(payments_file):
        os.remove(payments_file)
        return jsonify({"status": "ok", "message": "Payments cache cleared — Step 6 will retry all bills"})
    return jsonify({"status": "ok", "message": "No payments cache to clear"})


@app.route("/api/cc/clear-parsed", methods=["POST"])
def api_cc_clear_parsed():
    """Delete cc_transactions.json and all CC CSV files so Step 4 re-parses fresh."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    removed = []
    # Remove combined JSON
    json_path = os.path.join(output_dir, "cc_transactions.json")
    if os.path.exists(json_path):
        os.remove(json_path)
        removed.append("cc_transactions.json")
    # Remove all card CSV files
    import glob
    for csv_path in glob.glob(os.path.join(output_dir, "*_transactions.csv")):
        os.remove(csv_path)
        removed.append(os.path.basename(csv_path))
    if removed:
        return jsonify({"status": "ok", "message": f"Cleared: {', '.join(removed)}"})
    return jsonify({"status": "ok", "message": "No parsed data to clear"})


@app.route("/api/banking/clear-cache", methods=["POST"])
def api_banking_clear_cache():
    """Clear the imported_statements.json tracking file so Step 5 re-imports."""
    tracking_file = os.path.join(PROJECT_ROOT, "output", "imported_statements.json")
    if os.path.exists(tracking_file):
        os.remove(tracking_file)
        return jsonify({"status": "ok", "message": "Import cache cleared"})
    return jsonify({"status": "ok", "message": "No cache to clear"})


@app.route("/api/banking/delete-transactions", methods=["POST"])
def api_banking_delete_transactions():
    """Delete all banking transactions for a specific CC card from Zoho, then clear cache."""
    data = request.json or {}
    card_name = data.get("card_name")
    if not card_name:
        return jsonify({"status": "error", "message": "card_name required"}), 400

    try:
        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        from scripts.utils import resolve_account_ids
        resolve_account_ids(api, cards)

        card = next((c for c in cards if c["name"] == card_name), None)
        if not card:
            return jsonify({"status": "error", "message": f"Card '{card_name}' not found"}), 404

        account_id = card["zoho_account_id"]

        # Fetch all transactions for this account
        all_txns = []
        page = 1
        while True:
            result = api.list_bank_transactions(account_id, page=page)
            txns = result.get("banktransactions", [])
            if not txns:
                break
            all_txns.extend(txns)
            if not result.get("page_context", {}).get("has_more_page", False):
                break
            page += 1

        deleted = 0
        for txn in all_txns:
            txn_id = txn["transaction_id"]
            status = txn.get("status", "").lower()
            try:
                if status in ("matched", "categorized"):
                    try:
                        api.uncategorize_transaction(txn_id)
                    except Exception:
                        pass
                api.delete_bank_transaction(txn_id)
                deleted += 1
            except Exception:
                pass

        # Clear import cache so Step 5 re-imports fresh
        tracking_file = os.path.join(PROJECT_ROOT, "output", "imported_statements.json")
        if os.path.exists(tracking_file):
            os.remove(tracking_file)

        return jsonify({"status": "ok", "message": f"Deleted {deleted} transactions for {card_name}", "deleted": deleted})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/invoices/list")
def api_invoices_list():
    """List extracted invoices grouped by month, with bill creation status."""
    invoices_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
    bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")

    if not os.path.exists(invoices_path):
        return jsonify({"error": "No extracted_invoices.json found. Run Step 2 first."}), 404

    try:
        with open(invoices_path, "r", encoding="utf-8") as f:
            invoices = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read invoices: {e}"}), 500

    # Build set of already-created filenames — verify against Zoho (not just local file)
    created_files = set()
    if os.path.exists(bills_path):
        try:
            with open(bills_path, "r", encoding="utf-8") as f:
                local_bills = json.load(f)

            # Fetch actual bill IDs from Zoho to verify local tracking is accurate
            from scripts.utils import load_config, ZohoBooksAPI
            zoho_bill_ids = set()
            try:
                config = load_config()
                api = ZohoBooksAPI(config)
                page = 1
                while True:
                    result = api.list_bills() if page == 1 else api._request("GET", "bills", params={"page": page})
                    for b in result.get("bills", []):
                        zoho_bill_ids.add(b.get("bill_id"))
                    if not result.get("page_context", {}).get("has_more_page", False):
                        break
                    page += 1
            except Exception as e:
                from scripts.utils import log_action
                log_action(f"Zoho bill verification failed: {e}", "WARNING")
                zoho_bill_ids = None  # Zoho unavailable — fall back to local data

            cleaned_bills = []
            for entry in local_bills:
                if entry.get("status") == "created" and entry.get("bill_id"):
                    # Only trust "created" if bill still exists in Zoho (or Zoho check failed)
                    if zoho_bill_ids is None or entry["bill_id"] in zoho_bill_ids:
                        created_files.add(entry.get("file", ""))
                        cleaned_bills.append(entry)
                    else:
                        # Bill was deleted in Zoho — remove from local tracking
                        cleaned_bills.append({**entry, "status": "deleted_in_zoho"})
                else:
                    cleaned_bills.append(entry)

            # Update local file if any bills were removed from Zoho
            if zoho_bill_ids is not None and len(cleaned_bills) != len(local_bills):
                try:
                    with open(bills_path, "w", encoding="utf-8") as f:
                        json.dump(cleaned_bills, f, indent=2)
                except Exception:
                    pass
        except Exception:
            pass

    # Group invoices by month
    from collections import defaultdict
    month_groups = defaultdict(list)
    no_date = []
    for inv in invoices:
        date_str = inv.get("date", "")
        file_name = inv.get("file", "")
        status = "created" if file_name in created_files else "pending"
        entry = {
            "file": file_name,
            "vendor_name": inv.get("vendor_name", "Unknown"),
            "amount": inv.get("amount", 0),
            "currency": inv.get("currency", "INR"),
            "date": date_str,
            "status": status,
        }
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                month_key = dt.strftime("%b %Y")
                month_groups[month_key].append(entry)
                continue
            except ValueError:
                pass
        no_date.append(entry)

    # Sort months chronologically (newest first)
    def _month_sort_key(m):
        try:
            return datetime.strptime(m, "%b %Y")
        except ValueError:
            return datetime.min
    sorted_months = sorted(month_groups.keys(), key=_month_sort_key, reverse=True)

    months = []
    for m in sorted_months:
        months.append({"month": m, "invoices": month_groups[m]})
    if no_date:
        months.append({"month": "No Date", "invoices": no_date})

    total = len(invoices)
    pending = total - len(created_files & {inv.get("file", "") for inv in invoices})
    created = total - pending

    return jsonify({
        "months": months,
        "summary": {"total": total, "pending": pending, "created": created},
    })


@app.route("/api/upload/cc", methods=["POST"])
def api_upload_cc():
    """Receive multipart PDF files, save to input_pdfs/cc_statements/."""
    cc_dir = os.path.join(PROJECT_ROOT, "input_pdfs", "cc_statements")
    os.makedirs(cc_dir, exist_ok=True)

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    saved = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            continue
        # Secure the filename
        safe_name = os.path.basename(f.filename)
        dest = os.path.join(cc_dir, safe_name)
        f.save(dest)
        saved.append(safe_name)
        log_action(f"Uploaded CC statement: {safe_name}")

    if not saved:
        return jsonify({"error": "No valid PDF files in upload"}), 400

    return jsonify({"ok": True, "files": saved})


def _get_summary():
    """Build a summary from output JSON files if they exist."""
    summary = {}
    output_dir = os.path.join(PROJECT_ROOT, "output")

    # Extracted invoices count
    invoices_path = os.path.join(output_dir, "extracted_invoices.json")
    if os.path.exists(invoices_path):
        try:
            with open(invoices_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            summary["invoices"] = len(data) if isinstance(data, list) else 0
        except Exception:
            pass

    # CC transactions count
    cc_path = os.path.join(output_dir, "cc_transactions.json")
    if os.path.exists(cc_path):
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                summary["cc_transactions"] = len(data)
            elif isinstance(data, dict):
                total = 0
                for card_txns in data.values():
                    if isinstance(card_txns, list):
                        total += len(card_txns)
                summary["cc_transactions"] = total
        except Exception:
            pass

    # Bills created
    bills_path = os.path.join(output_dir, "created_bills.json")
    if os.path.exists(bills_path):
        try:
            with open(bills_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            summary["bills"] = len(data) if isinstance(data, list) else 0
        except Exception:
            pass

    return summary


def _determine_unmatched_reason(payment, cc_transactions, reverse_vendor_map):
    """Analyze why a bill didn't match any CC transaction."""
    vendor_name = payment.get("vendor_name", "")
    if not vendor_name:
        return "No vendor name on bill"

    bill_amount = payment.get("amount", 0)
    currency = payment.get("currency", "INR")

    keywords = reverse_vendor_map.get(vendor_name.lower(), [vendor_name.lower()])

    # Search CC transactions for vendor keyword matches
    vendor_txns = []
    for txn in cc_transactions:
        desc_lower = txn.get("description", "").lower()
        for kw in keywords:
            if kw in desc_lower or desc_lower.startswith(kw[:10]):
                vendor_txns.append(txn)
                break

    if not vendor_txns:
        return "No CC transaction found for this vendor"

    if currency == "USD":
        return (f"Vendor found in CC ({len(vendor_txns)} txn) but "
                f"USD ${bill_amount} not matched (exchange rate / date mismatch)")

    amounts = [t["amount"] for t in vendor_txns]
    closest = min(amounts, key=lambda a: abs(a - bill_amount))
    diff = abs(closest - bill_amount)
    pct = (diff / bill_amount * 100) if bill_amount else 0
    return (f"Vendor in CC ({len(vendor_txns)} txn) - amount mismatch "
            f"(bill: {bill_amount:,.2f}, closest CC: {closest:,.2f}, diff: {pct:.1f}%)")


@app.route("/api/match-status")
def api_match_status():
    """Return match status data for the Match Status dashboard panel."""
    output_dir = os.path.join(PROJECT_ROOT, "output")

    payments_path = os.path.join(output_dir, "recorded_payments.json")
    payments = []
    if os.path.exists(payments_path):
        try:
            with open(payments_path, "r", encoding="utf-8") as f:
                payments = json.load(f)
        except Exception:
            return jsonify({"error": "Failed to read recorded_payments.json"}), 500

    if not payments:
        return jsonify({"error": "No payment data found. Run Step 6 (Payments) first."}), 404

    # Load CC transactions for unmatched reason analysis
    cc_path = os.path.join(output_dir, "cc_transactions.json")
    cc_transactions = []
    if os.path.exists(cc_path):
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                cc_transactions = json.load(f)
        except Exception:
            pass

    # Build reverse vendor map: vendor_name -> merchant keywords
    vendor_mappings_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    reverse_map = {}
    try:
        with open(vendor_mappings_path, "r", encoding="utf-8") as f:
            vm = json.load(f)
        for merchant, vendor in vm.get("mappings", {}).items():
            reverse_map.setdefault(vendor.lower(), []).append(merchant.lower())
    except Exception:
        pass

    matched = []
    unmatched = []

    for p in payments:
        row = {
            "file": p.get("file"),
            "vendor_name": p.get("vendor_name"),
            "amount": p.get("amount"),
            "currency": p.get("currency", "INR"),
            "cc_inr_amount": p.get("cc_inr_amount"),
            "cc_card": p.get("cc_card"),
            "status": p.get("status"),
        }

        if p.get("status") == "paid":
            matched.append(row)
        else:
            row["reason"] = _determine_unmatched_reason(
                p, cc_transactions, reverse_map
            )
            unmatched.append(row)

    return jsonify({
        "matched": matched,
        "unmatched": unmatched,
        "summary": {
            "matched_count": len(matched),
            "unmatched_count": len(unmatched),
            "total_count": len(payments),
        },
    })


@app.route("/api/check-cc-match")
def api_check_cc_match():
    """Compare cached unpaid bills with cached CC transactions for side-by-side review (no live Zoho calls)."""
    try:
        mod_05 = _import_script("05_record_payments.py")
        from scripts.utils import load_vendor_mappings

        # Load cached data
        bills_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
        cc_txns_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_cc_transactions_cache.json")

        if not os.path.exists(bills_cache_path) or not os.path.exists(cc_txns_cache_path):
            return jsonify({"error": "Cache not found. Click Sync Zoho first."}), 404

        with open(bills_cache_path, "r", encoding="utf-8") as f:
            all_bills = json.load(f)
        with open(cc_txns_cache_path, "r", encoding="utf-8") as f:
            cc_raw = json.load(f)

        # Filter to unpaid/overdue bills only
        bills = [
            {
                "vendor_name": b.get("vendor_name", ""),
                "amount": float(b.get("total", 0)),
                "currency": b.get("currency", b.get("currency_code", "INR")),
                "date": b.get("date", ""),
            }
            for b in all_bills
            if b.get("status") in ("open", "unpaid", "overdue") and b.get("vendor_name")
        ]

        # Build keyword -> vendor_name reverse map for grouping
        vendor_mappings = load_vendor_mappings()
        vendor_to_merchants = mod_05.build_vendor_to_merchants(vendor_mappings)
        keyword_to_vendor = {}
        for bill in bills:
            vname = bill["vendor_name"]
            vkey = vname.lower()
            keyword_to_vendor[vkey] = vname
            keyword_to_vendor[vkey.replace(" ", "")] = vname
            for kw in vendor_to_merchants.get(vkey, []):
                keyword_to_vendor[kw.lower()] = vname

        def _get_vendor_for_cc(desc):
            desc_lower = desc.lower()
            desc_norm = mod_05._normalize(desc)
            for kw, vname in keyword_to_vendor.items():
                if not kw:
                    continue
                if kw in desc_lower:
                    return vname
                if desc_lower.startswith(kw[:10]):
                    return vname
                kw_norm = mod_05._normalize(kw)
                if len(kw_norm) >= 6 and kw_norm in desc_norm:
                    return vname
            return None

        # Group bills by vendor
        from collections import defaultdict
        bill_groups = defaultdict(list)
        for b in bills:
            bill_groups[b["vendor_name"]].append(b)

        # Group CC transactions by matched vendor
        cc_groups = defaultdict(list)
        unmatched_cc = []
        for t in cc_raw:
            if float(t.get("amount", 0)) <= 0:
                continue
            vname = _get_vendor_for_cc(t.get("description", ""))
            entry = {
                "description": t.get("description", ""),
                "amount": t.get("amount", 0),
                "date": t.get("date", ""),
                "card_name": t.get("card_name", ""),
            }
            if vname:
                cc_groups[vname].append(entry)
            else:
                unmatched_cc.append(entry)

        # Build grouped list sorted by vendor name
        all_vendors = sorted(set(list(bill_groups.keys()) + list(cc_groups.keys())))
        grouped = [
            {
                "vendor": v,
                "bills": bill_groups.get(v, []),
                "cc_transactions": cc_groups.get(v, []),
            }
            for v in all_vendors
        ]

        total_bills = sum(len(g["bills"]) for g in grouped)
        total_cc = sum(len(g["cc_transactions"]) for g in grouped)

        return jsonify({
            "grouped": grouped,
            "unmatched_cc": unmatched_cc,
            "summary": {
                "vendors_count": len(grouped),
                "bills_count": total_bills,
                "cc_transactions_count": total_cc,
                "unmatched_cc_count": len(unmatched_cc),
            },
        })
    except Exception as e:
        log_action(f"check-cc-match error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _parse_month_key(month_str):
    """Parse 'Feb 2026' -> sortable tuple (2026, 2). Returns (0, 0) on failure."""
    try:
        dt = datetime.strptime(month_str, "%b %Y")
        return (dt.year, dt.month)
    except Exception:
        return (0, 0)


@app.route("/api/compare/monthly")
def api_compare_monthly():
    """Compare CC transactions vs organized invoices, grouped by month."""
    from collections import defaultdict

    output_dir = os.path.join(PROJECT_ROOT, "output")

    # --- Load CC transactions ---
    cc_path = os.path.join(output_dir, "cc_transactions.json")
    cc_transactions = []
    if os.path.exists(cc_path):
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                cc_transactions = json.load(f)
        except Exception:
            pass

    # --- Load invoices (prefer compare_invoices.json from org_inv, fallback to extracted_invoices.json) ---
    inv_path = os.path.join(output_dir, "compare_invoices.json")
    if not os.path.exists(inv_path):
        inv_path = os.path.join(output_dir, "extracted_invoices.json")
    invoices = []
    if os.path.exists(inv_path):
        try:
            with open(inv_path, "r", encoding="utf-8") as f:
                invoices = json.load(f)
        except Exception:
            pass

    if not cc_transactions and not invoices:
        return jsonify({"error": "No data found. Click 'Parse All Invoices' and 'Parse All CC' first."}), 404

    # --- Month helpers ---
    month_from_path_re = re.compile(r'organized_invoices[/\\](\w+ \d{4})[/\\]')

    def get_invoice_month(inv):
        # Prefer the actual extracted date (most reliable)
        d = inv.get("date", "")
        if d and len(d) >= 7:
            try:
                dt = datetime.strptime(d[:10], "%Y-%m-%d")
                return dt.strftime("%b %Y")
            except Exception:
                pass
        # Fallback: organized_month from folder name
        om = inv.get("organized_month", "")
        if om:
            return om
        # Fallback: parse from organized_path
        op = inv.get("organized_path", "")
        m = month_from_path_re.search(op)
        if m:
            return m.group(1)
        return "Unknown"

    def get_cc_month(txn):
        d = txn.get("date", "")
        if d and len(d) >= 7:
            try:
                dt = datetime.strptime(d[:10], "%Y-%m-%d")
                return dt.strftime("%b %Y")
            except Exception:
                pass
        return "Unknown"

    # --- Vendor name resolution from vendor_mappings ---
    vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    vendor_map = {}        # lowercased keys
    vendor_map_norm = {}   # normalized keys (no punctuation/spaces)
    def _norm(s):
        return re.sub(r'[\s.\-,*()]+', '', s.lower())
    try:
        with open(vm_path, "r", encoding="utf-8") as f:
            vm = json.load(f)
        for k, v in vm.get("mappings", {}).items():
            vendor_map[k.lower()] = v
            vendor_map_norm[_norm(k)] = v
    except Exception:
        pass
    _sorted_keys = sorted(vendor_map.keys(), key=len, reverse=True)
    _sorted_norm_keys = sorted(vendor_map_norm.keys(), key=len, reverse=True)

    def _resolve_vendor(desc):
        if not desc:
            return None
        dl = desc.lower()
        dn = _norm(desc)
        # 1. Exact lowercase match
        if dl in vendor_map:
            return vendor_map[dl]
        # 2. Exact normalized match
        if dn in vendor_map_norm:
            return vendor_map_norm[dn]
        # 3. Substring match on lowercase (longest key first)
        for key in _sorted_keys:
            if key and len(key) >= 4 and key in dl:
                return vendor_map[key]
        # 4. Substring match on normalized (handles commas, dots, etc.)
        for key in _sorted_norm_keys:
            if key and len(key) >= 4 and key in dn:
                return vendor_map_norm[key]
        return None

    # --- Load Zoho uncategorized CC transactions (use as CC source if available) ---
    zoho_cc_path = os.path.join(output_dir, "zoho_cc_transactions_cache.json")
    zoho_cc_txns = []
    if os.path.exists(zoho_cc_path):
        try:
            with open(zoho_cc_path, "r", encoding="utf-8") as f:
                zoho_cc_txns = json.load(f)
        except Exception:
            pass

    # Use Zoho uncategorized CC transactions if available, otherwise fall back to parsed
    cc_source = zoho_cc_txns if zoho_cc_txns else cc_transactions

    # --- Group by month (debits only) ---
    cc_by_month = defaultdict(list)
    for t in cc_source:
        if float(t.get("amount", 0)) <= 0:
            continue  # Skip credits (refunds, payments, waivers)
        cc_by_month[get_cc_month(t)].append({
            "transaction_id": t.get("transaction_id", ""),
            "date": t.get("date", ""),
            "description": t.get("description", ""),
            "amount": t.get("amount", 0),
            "card_name": t.get("card_name", ""),
            "forex_amount": t.get("forex_amount"),
            "forex_currency": t.get("forex_currency"),
            "vendor_name": _resolve_vendor(t.get("description", "")),
        })

    # --- Load Zoho bills cache + created_bills for "In Zoho" check ---
    bills_cache_path = os.path.join(output_dir, "zoho_bills_cache.json")
    zoho_bill_numbers = {}   # bill_number -> (bill_id, status)
    zoho_bill_numbers_norm = {}  # normalized -> (bill_id, status)
    zoho_bill_ids_set = set()  # all bill_ids from cache
    zoho_vendor_date_amount = {}  # (vendor_lower, date, amount) -> (bill_id, status)
    if os.path.exists(bills_cache_path):
        try:
            with open(bills_cache_path, "r", encoding="utf-8") as f:
                for b in json.load(f):
                    bn = b.get("bill_number", "")
                    bid = b.get("bill_id", "")
                    bst = b.get("status", "")
                    bill_info = (bid, bst)
                    if bid:
                        zoho_bill_ids_set.add(bid)
                    if bn:
                        zoho_bill_numbers[bn] = bill_info
                        norm = _normalize_bill_number(bn)
                        if norm:
                            zoho_bill_numbers_norm[norm] = bill_info
                        # Index individual invoice numbers from combined bills
                        # e.g. "INV-001 + INV-002 + INV-003" -> index each part
                        if " + " in bn:
                            for part in bn.split(" + "):
                                part = part.strip()
                                if part:
                                    zoho_bill_numbers[part] = bill_info
                                    pnorm = _normalize_bill_number(part)
                                    if pnorm:
                                        zoho_bill_numbers_norm[pnorm] = bill_info
                    # Index by vendor+date+amount for fallback matching
                    bv = (b.get("vendor_name") or "").strip().lower()
                    bd = b.get("date", "")
                    ba = round(float(b.get("total", 0)), 2)
                    if bv and bd and ba and bid:
                        zoho_vendor_date_amount[(bv, bd, ba)] = bill_info
        except Exception:
            pass

    # Build vendor name aliases from vendors cache (contact_name ↔ company_name)
    _vendor_aliases = defaultdict(set)  # name_lower -> set of alias names
    vendors_cache_path = os.path.join(output_dir, "zoho_vendors_cache.json")
    if os.path.exists(vendors_cache_path):
        try:
            with open(vendors_cache_path, "r", encoding="utf-8") as f:
                for v in json.load(f):
                    cn = (v.get("contact_name") or "").strip().lower()
                    comp = (v.get("company_name") or "").strip().lower()
                    if cn and comp and cn != comp:
                        _vendor_aliases[cn].add(comp)
                        _vendor_aliases[comp].add(cn)
        except Exception:
            pass

    created_bills_set = {}  # file -> bill_id
    deleted_bills_set = set()  # files intentionally deleted
    created_bills_path = os.path.join(output_dir, "created_bills.json")
    if os.path.exists(created_bills_path):
        try:
            with open(created_bills_path, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    if entry.get("status") == "created" and entry.get("bill_id"):
                        # Only trust if bill_id still exists in Zoho cache
                        if not zoho_bill_ids_set or entry["bill_id"] in zoho_bill_ids_set:
                            created_bills_set[entry.get("file", "")] = entry["bill_id"]
                    elif entry.get("status") == "deleted_in_zoho":
                        deleted_bills_set.add(entry.get("file", ""))
        except Exception:
            pass

    _GENERIC_INV = {"payment", "original", "invoice", "bill", "tax", "none", "n/a", ""}

    def _check_in_zoho(inv):
        """Check if invoice already has a bill in Zoho.
        Returns (bill_id, status) tuple or None."""
        inv_num = (inv.get("invoice_number") or "").strip()
        fname = inv.get("file", "")
        # Check created_bills.json first (includes deleted_in_zoho)
        if fname in created_bills_set:
            return (created_bills_set[fname], "")
        if fname in deleted_bills_set:
            return ("deleted", "")
        # Check by invoice number
        has_reliable = bool(inv_num and inv_num.lower().strip() not in _GENERIC_INV)
        if has_reliable:
            if inv_num in zoho_bill_numbers:
                return zoho_bill_numbers[inv_num]
            norm = _normalize_bill_number(inv_num)
            if norm and norm in zoho_bill_numbers_norm:
                return zoho_bill_numbers_norm[norm]
            # Also check with INV- prefix
            legacy = f"INV-{inv_num}"
            if legacy in zoho_bill_numbers:
                return zoho_bill_numbers[legacy]
        else:
            # Try filename without extension as bill number
            bn = re.sub(r'\.(pdf|eml)$', '', fname, flags=re.IGNORECASE)
            if bn in zoho_bill_numbers:
                return zoho_bill_numbers[bn]
            norm = _normalize_bill_number(bn)
            if norm and norm in zoho_bill_numbers_norm:
                return zoho_bill_numbers_norm[norm]
        # Fallback: match by vendor + date + amount (for bills created manually in Zoho)
        inv_vendor = (inv.get("vendor_name") or "").strip().lower()
        inv_date = inv.get("date", "")
        inv_amount = round(float(inv.get("amount") or 0), 2)
        if inv_vendor and inv_date and inv_amount:
            # Build set of vendor name variants to check
            names_to_check = {inv_vendor}
            resolved = _resolve_vendor(inv_vendor) if inv_vendor else None
            if resolved:
                names_to_check.add(resolved.strip().lower())
            # Also check vendor alias names (contact_name ↔ company_name from vendors cache)
            if inv_vendor in _vendor_aliases:
                names_to_check.update(_vendor_aliases[inv_vendor])
            for vn in names_to_check:
                key = (vn, inv_date, inv_amount)
                if key in zoho_vendor_date_amount:
                    return zoho_vendor_date_amount[key]
        return None

    inv_by_month = defaultdict(list)
    for inv in invoices:
        zoho_result = _check_in_zoho(inv)
        zoho_bid = zoho_result[0] if zoho_result else ""
        zoho_status = zoho_result[1] if zoho_result else ""
        inv_by_month[get_invoice_month(inv)].append({
            "vendor_name": inv.get("vendor_name", "") or "",
            "vendor_gstin": inv.get("vendor_gstin"),
            "amount": inv.get("amount"),
            "currency": inv.get("currency", "INR"),
            "date": inv.get("date", ""),
            "invoice_number": inv.get("invoice_number", ""),
            "in_zoho": bool(zoho_bid),
            "zoho_bill_id": zoho_bid,
            "zoho_bill_status": zoho_status,
        })

    # --- Build sorted month list (newest first) ---
    all_months = sorted(
        set(list(cc_by_month.keys()) + list(inv_by_month.keys())),
        key=lambda m: _parse_month_key(m),
        reverse=True,
    )

    months = []
    for mk in all_months:
        cc_list = sorted(cc_by_month.get(mk, []), key=lambda x: x.get("date", ""))
        inv_list = sorted(inv_by_month.get(mk, []), key=lambda x: x.get("date", ""))
        cc_total = sum(t["amount"] for t in cc_list if t.get("amount") and t["amount"] > 0)
        inv_total = sum(i["amount"] for i in inv_list if i.get("amount"))
        months.append({
            "month": mk,
            "cc_transactions": cc_list,
            "invoices": inv_list,
            "cc_count": len(cc_list),
            "inv_count": len(inv_list),
            "cc_total": round(cc_total, 2),
            "inv_total": round(inv_total, 2),
        })

    # Load Zoho vendor names for filter dropdown
    zoho_vendor_names = []
    vendors_cache_path = os.path.join(output_dir, "zoho_vendors_cache.json")
    if os.path.exists(vendors_cache_path):
        try:
            with open(vendors_cache_path, "r", encoding="utf-8") as f:
                zoho_vendor_names = sorted(set(
                    (v.get("contact_name") or "").strip()
                    for v in json.load(f)
                    if (v.get("contact_name") or "").strip()
                ), key=str.casefold)
        except Exception:
            pass

    return jsonify({
        "months": months,
        "summary": {
            "total_months": len(months),
            "total_cc": sum(m["cc_count"] for m in months),
            "total_invoices": sum(m["inv_count"] for m in months),
        },
        "zoho_vendors": zoho_vendor_names,
    })


def _auto_update_vendor_mappings(invoices_list, log_action):
    """Auto-discover new vendor mappings from parsed invoices + unmapped CC descriptions."""
    try:
        vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
        with open(vm_path, "r", encoding="utf-8") as f:
            vm_data = json.load(f)
        mappings = vm_data.get("mappings", {})

        def _nrm(s):
            return re.sub(r'[\s.\-,*()]+', '', s.lower())

        existing_values = set(mappings.values())  # clean vendor names
        existing_keys_norm = {_nrm(k) for k in mappings}

        # --- Step 1: Collect invoice vendor names, resolve canonical target ---
        inv_vendor_canonical = {}  # invoice vendor_name → canonical mapping target
        for inv in invoices_list:
            vn = (inv.get("vendor_name") or "").strip()
            if not vn or vn.lower() in {"unknown", "n/a", ""}:
                continue
            if vn in inv_vendor_canonical:
                continue
            if vn in existing_values:
                inv_vendor_canonical[vn] = vn
                continue
            # Check if loosely matches an existing mapping value
            vn_n = _nrm(vn)
            matched = None
            for ev in existing_values:
                ev_n = _nrm(ev)
                if ev_n and vn_n and (ev_n in vn_n or vn_n in ev_n):
                    matched = ev
                    break
            inv_vendor_canonical[vn] = matched or vn

        # --- Step 2: Build token lookup (vendor name fragments → canonical) ---
        # Combine existing mapping values + invoice vendor names
        all_canonical = set(existing_values) | set(inv_vendor_canonical.values())
        token_map = {}  # normalized token → canonical name

        # Full normalized vendor names
        for canon in all_canonical:
            cn = _nrm(canon)
            if cn and len(cn) >= 4:
                token_map[cn] = canon

        # First-word tokens — only add if UNIQUE to one vendor (skip ambiguous)
        first_word_vendors = {}  # first_word → set of vendor names
        for canon in all_canonical:
            words = [w for w in re.split(r'[\s,.\-*()]+', canon) if w]
            if words:
                fw = _nrm(words[0])
                if fw and len(fw) >= 5:
                    first_word_vendors.setdefault(fw, set()).add(canon)
        for fw, vendors in first_word_vendors.items():
            if len(vendors) == 1 and fw not in token_map:
                token_map[fw] = next(iter(vendors))

        # Existing mapping keys as tokens (these are manually curated, safe)
        for k, v in mappings.items():
            kn = _nrm(k)
            if kn and len(kn) >= 4:
                token_map[kn] = v

        sorted_tokens = sorted(token_map.keys(), key=len, reverse=True)

        # --- Step 3: Find unmapped CC descriptions & auto-map ---
        new_mappings = {}
        cc_path = os.path.join(PROJECT_ROOT, "output", "cc_transactions.json")
        if os.path.exists(cc_path):
            with open(cc_path, "r", encoding="utf-8") as f:
                cc_txns = json.load(f)

            map_lower = {k.lower(): v for k, v in mappings.items()}
            map_norm = {_nrm(k): v for k, v in mappings.items()}
            sk_low = sorted(map_lower.keys(), key=len, reverse=True)
            sk_nrm = sorted(map_norm.keys(), key=len, reverse=True)

            for t in cc_txns:
                desc = t.get("description", "").strip()
                if not desc or float(t.get("amount", 0)) <= 0:
                    continue
                dl = desc.lower()
                dn = _nrm(desc)

                # Already mapped? (replicate _resolve_vendor logic)
                if dl in map_lower or dn in map_norm:
                    continue
                found = False
                for key in sk_low:
                    if key and len(key) >= 4 and key in dl:
                        found = True
                        break
                if not found:
                    for key in sk_nrm:
                        if key and len(key) >= 4 and key in dn:
                            found = True
                            break
                if found:
                    continue

                # Try token matching (longest first for precision)
                for tok in sorted_tokens:
                    if tok in dn:
                        target = token_map[tok]
                        new_mappings[desc] = target
                        # Update local lookup so we don't double-add variants
                        map_lower[dl] = target
                        map_norm[dn] = target
                        break

        if new_mappings:
            mappings.update(new_mappings)
            vm_data["mappings"] = mappings
            with open(vm_path, "w", encoding="utf-8") as f:
                json.dump(vm_data, f, indent=4, ensure_ascii=False)
            log_action(f"Auto-mapping: {len(new_mappings)} new vendor mappings added:")
            for desc, target in sorted(new_mappings.items(), key=lambda x: x[1]):
                log_action(f"    '{desc}' → '{target}'")
        else:
            log_action("Auto-mapping: no new vendor mappings needed")
    except Exception as e:
        log_action(f"Auto-mapping failed: {e}", "WARNING")


def _parse_org_invoices_thread():
    """Background thread: extract invoice data from organized_invoices/."""
    from scripts.utils import log_action
    try:
        mod_02 = _import_script("02_extract_invoices.py")
        org_root = os.path.join(PROJECT_ROOT, "organized_invoices")
        output_path = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")

        log_action("=" * 50)
        log_action("Parse All Invoices from organized_invoices/")
        log_action("=" * 50)

        results = []
        total = 0
        for month_folder in sorted(os.listdir(org_root)):
            month_dir = os.path.join(org_root, month_folder)
            if not os.path.isdir(month_dir):
                continue
            pdfs = [f for f in os.listdir(month_dir) if f.lower().endswith((".pdf", ".eml"))]
            log_action(f"  {month_folder}: {len(pdfs)} files")
            for pdf_file in sorted(pdfs):
                pdf_path = os.path.join(month_dir, pdf_file)
                try:
                    inv = mod_02.extract_invoice(pdf_path, pdf_file)
                    if inv:
                        # Amazon India returns a list of invoices (one per page)
                        inv_list = inv if isinstance(inv, list) else [inv]
                        for item in inv_list:
                            item["organized_month"] = month_folder
                            item["organized_path"] = pdf_path
                            results.append(item)
                            total += 1
                            log_action(f"    {item.get('file', pdf_file)} -> {item.get('vendor_name', '?')}, {item.get('amount', '?')} {item.get('currency', '?')}")
                    else:
                        log_action(f"    {pdf_file} -> no data", "WARNING")
                except Exception as e:
                    log_action(f"    {pdf_file} -> FAILED: {e}", "WARNING")

        # Dedup by invoice_number (keep first occurrence)
        seen = {}
        deduped = []
        generic = {"payment", "original", "invoice", "receipt", "bill", "tax", "none", "n/a", ""}
        for inv in results:
            num = inv.get("invoice_number", "")
            if num and num.lower().strip() not in generic and num in seen:
                log_action(f"  Dedup: skipping {inv['file']} (same #{num} as {seen[num]})")
                continue
            if num and num.lower().strip() not in generic:
                seen[num] = inv["file"]
            deduped.append(inv)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(deduped, f, indent=2, ensure_ascii=False)

        log_action(f"Done: {len(deduped)} invoices extracted ({total} total, {total - len(deduped)} deduped)")

        # --- Auto-discover & update vendor mappings ---
        _auto_update_vendor_mappings(deduped, log_action)

    except Exception as e:
        log_action(f"Parse org invoices failed: {e}", "ERROR")
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


@app.route("/api/compare/parse-org-invoices", methods=["POST"])
def api_parse_org_invoices():
    """Start background extraction from organized_invoices/ folder."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True
        _state["current_step"] = "Parse Org Invoices"

    org_root = os.path.join(PROJECT_ROOT, "organized_invoices")
    if not os.path.isdir(org_root):
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None
        return jsonify({"error": "organized_invoices/ folder not found"}), 404

    t = threading.Thread(target=_parse_org_invoices_thread, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# --- Sync Zoho (bills + vendors cache) ---

def _sync_zoho_thread():
    """Background thread: fetch all bills + vendors from Zoho and save as local cache."""
    from scripts.utils import log_action, load_config, ZohoBooksAPI
    try:
        log_action("=" * 50)
        log_action("Sync Zoho: Fetching bills, vendors & CC accounts")
        log_action("=" * 50)

        config = load_config()
        api = ZohoBooksAPI(config)

        # --- Fetch ALL bills (paginated) ---
        all_bills = []
        page = 1
        while True:
            result = api.list_bills(page=page)
            bills_page = result.get("bills", [])
            if not bills_page:
                break
            for b in bills_page:
                all_bills.append({
                    "bill_id": b.get("bill_id"),
                    "bill_number": b.get("bill_number", ""),
                    "vendor_name": b.get("vendor_name", ""),
                    "vendor_id": b.get("vendor_id", ""),
                    "date": b.get("date", ""),
                    "total": b.get("total", 0),
                    "status": b.get("status", ""),
                })
            has_more = result.get("page_context", {}).get("has_more_page", False)
            if not has_more:
                break
            page += 1
            log_action(f"  Bills page {page}...")

        log_action(f"Fetched {len(all_bills)} bills from Zoho")

        # --- Fetch ALL vendors (paginated) ---
        all_vendors = []
        page = 1
        while True:
            result = api.list_all_vendors(page=page)
            contacts = result.get("contacts", [])
            if not contacts:
                break
            for v in contacts:
                all_vendors.append({
                    "contact_id": v.get("contact_id"),
                    "contact_name": v.get("contact_name", ""),
                    "company_name": v.get("company_name", ""),
                    "gst_no": v.get("gst_no", ""),
                    "gst_treatment": v.get("gst_treatment", ""),
                    "currency_code": v.get("currency_code", "INR"),
                })
            has_more = result.get("page_context", {}).get("has_more_page", False)
            if not has_more:
                break
            page += 1
            log_action(f"  Vendors page {page}...")

        log_action(f"Fetched {len(all_vendors)} vendors from Zoho")

        # --- Fetch CC bank accounts ---
        bank_accounts = api.list_bank_accounts()
        cc_accounts = [a for a in bank_accounts if a.get("account_type") == "credit_card"]
        log_action(f"Fetched {len(cc_accounts)} CC accounts from Zoho Banking")

        # --- Save caches ---
        os.makedirs(os.path.join(PROJECT_ROOT, "output"), exist_ok=True)
        bills_cache = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
        vendors_cache = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")

        with open(bills_cache, "w", encoding="utf-8") as f:
            json.dump(all_bills, f, indent=2, ensure_ascii=False)
        with open(vendors_cache, "w", encoding="utf-8") as f:
            json.dump(all_vendors, f, indent=2, ensure_ascii=False)

        cc_cache = os.path.join(PROJECT_ROOT, "output", "zoho_cc_accounts_cache.json")
        with open(cc_cache, "w", encoding="utf-8") as f:
            json.dump(cc_accounts, f, indent=2, ensure_ascii=False)

        # --- Fetch CC transactions (uncategorized) from each configured card ---
        cards = config.get("credit_cards", [])
        all_cc_txns = []
        for card in cards:
            account_id = card.get("zoho_account_id")
            if not account_id:
                log_action(f"  Skipping card '{card.get('name')}' - no zoho_account_id")
                continue
            card_name = card.get("name", "")
            page = 1
            card_txns = 0
            while True:
                result = api.list_uncategorized(account_id, page=page)
                for t in result.get("banktransactions", []):
                    amount = float(t.get("amount") or 0)
                    desc = t.get("description", "") or t.get("payee", "")
                    entry = {
                        "date": t.get("date", ""),
                        "description": desc,
                        "amount": amount,
                        "card_name": card_name,
                        "zoho_account_id": account_id,
                        "transaction_id": t.get("transaction_id", ""),
                    }
                    # Parse forex from '[USD 359.90]' in description
                    import re as _re
                    fx_m = _re.search(r'\[([A-Z]{3})\s+([\d,.]+)\]', desc)
                    if fx_m:
                        try:
                            entry["forex_amount"] = float(fx_m.group(2).replace(',', ''))
                            entry["forex_currency"] = fx_m.group(1)
                        except ValueError:
                            pass
                    all_cc_txns.append(entry)
                    card_txns += 1
                if not result.get("page_context", {}).get("has_more_page", False):
                    break
                page += 1
            log_action(f"  {card_name}: {card_txns} uncategorized transactions")

        cc_txns_cache = os.path.join(PROJECT_ROOT, "output", "zoho_cc_transactions_cache.json")
        with open(cc_txns_cache, "w", encoding="utf-8") as f:
            json.dump(all_cc_txns, f, indent=2, ensure_ascii=False)

        log_action(f"Saved: {len(all_bills)} bills -> zoho_bills_cache.json")
        log_action(f"Saved: {len(all_vendors)} vendors -> zoho_vendors_cache.json")
        log_action(f"Saved: {len(cc_accounts)} CC accounts -> zoho_cc_accounts_cache.json")
        log_action(f"Saved: {len(all_cc_txns)} CC transactions -> zoho_cc_transactions_cache.json")

        # Resolve/update CC account IDs in config
        from scripts.utils import resolve_account_ids
        cards = config.get("credit_cards", [])
        if cards:
            resolve_account_ids(api, cards)

        log_action("Sync Zoho complete!")

    except Exception as e:
        log_action(f"Sync Zoho failed: {e}", "ERROR")
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


@app.route("/api/zoho/sync", methods=["POST"])
def api_zoho_sync():
    """Start background sync of all bills + vendors from Zoho."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True
        _state["current_step"] = "Sync Zoho"

    t = threading.Thread(target=_sync_zoho_thread, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# --- Match Preview (bill dedup analysis) ---

def _normalize_bill_number(num):
    """Normalize bill number for fuzzy matching: strip INV- prefix, lowercase, remove non-alphanumeric."""
    import re
    if not num:
        return ""
    s = num.strip()
    # Strip common prefixes
    s = re.sub(r'^(INV[-_]?)', '', s, flags=re.IGNORECASE)
    # Lowercase and remove non-alphanumeric
    s = re.sub(r'[^a-z0-9]', '', s.lower())
    return s


@app.route("/api/zoho-vendors")
def api_zoho_vendors():
    """Return Zoho vendor list from cache for the UI dropdown."""
    cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")
    if not os.path.exists(cache_path):
        return jsonify([])
    with open(cache_path, "r", encoding="utf-8") as f:
        vendors = json.load(f)
    return jsonify([{"contact_id": v.get("contact_id", ""), "contact_name": v.get("contact_name", ""), "currency_code": v.get("currency_code", "INR")} for v in vendors])


@app.route("/api/vendor-overrides")
def api_vendor_overrides_get():
    """Return saved vendor overrides."""
    path = os.path.join(PROJECT_ROOT, "output", "vendor_overrides.json")
    if not os.path.exists(path):
        return jsonify({})
    with open(path, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/vendor-overrides", methods=["POST"])
def api_vendor_overrides_post():
    """Merge and save vendor overrides."""
    path = os.path.join(PROJECT_ROOT, "output", "vendor_overrides.json")
    existing = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    new_overrides = request.json.get("overrides", {})
    existing.update(new_overrides)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    return jsonify({"ok": True, "count": len(existing)})


@app.route("/api/bills/match-preview", methods=["POST"])
def api_bills_match_preview():
    """Classify each extracted invoice as skip/new_bill/new_vendor_bill against Zoho cache."""
    from scripts.utils import log_action

    # Load extracted invoices (prefer compare_invoices, fallback to extracted_invoices)
    compare_path = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")
    extract_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
    invoices_path = compare_path if os.path.exists(compare_path) else extract_path
    if not os.path.exists(invoices_path):
        return jsonify({"error": "No extracted invoices found. Run Extract Data first."}), 404

    with open(invoices_path, "r", encoding="utf-8") as f:
        invoices = json.load(f)

    # Load Zoho caches
    bills_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
    vendors_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")
    if not os.path.exists(bills_cache_path) or not os.path.exists(vendors_cache_path):
        return jsonify({"error": "Zoho cache not found. Click Sync Zoho first."}), 404

    with open(bills_cache_path, "r", encoding="utf-8") as f:
        zoho_bills = json.load(f)
    with open(vendors_cache_path, "r", encoding="utf-8") as f:
        zoho_vendors = json.load(f)

    # Build indices
    # Exact bill number -> bill info
    bills_exact = {}
    # Normalized bill number -> bill info
    bills_norm = {}
    # (vendor_name_lower, date) -> bill info
    bills_vendor_date = {}
    for b in zoho_bills:
        bn = b.get("bill_number", "")
        bills_exact[bn] = b
        norm = _normalize_bill_number(bn)
        if norm:
            bills_norm[norm] = b

        vn = (b.get("vendor_name") or "").strip().lower()
        bd = b.get("date", "")
        if vn and bd:
            bills_vendor_date[(vn, bd)] = b

    # Vendor name -> vendor info (lowercase)
    vendor_name_map = {}
    import re
    _norm_v = lambda s: re.sub(r'[\s.\-,*()]+', '', s.lower()) if s else ""
    vendor_norm_map = {}
    for v in zoho_vendors:
        cn = (v.get("contact_name") or "").strip()
        if cn:
            vendor_name_map[cn.lower()] = v
            vendor_norm_map[_norm_v(cn)] = v
        comp = (v.get("company_name") or "").strip()
        if comp:
            vendor_name_map[comp.lower()] = v
            vendor_norm_map[_norm_v(comp)] = v

    # GSTIN -> vendor info
    vendor_gstin_map = {}
    for v in zoho_vendors:
        gst_no = (v.get("gst_no") or "").strip()
        if gst_no:
            vendor_gstin_map[gst_no] = v

    # Also load vendor_mappings for resolving names
    vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    vendor_mappings_data = {}
    if os.path.exists(vm_path):
        with open(vm_path, "r", encoding="utf-8") as f:
            vendor_mappings_data = json.load(f).get("mappings", {})

    _GENERIC_NUMBERS = {"payment", "original", "invoice", "bill", "tax", "none", "n/a", ""}

    # Load created_bills.json to mark already-created bills
    created_bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
    created_bills_map = {}  # file -> entry
    if os.path.exists(created_bills_path):
        try:
            with open(created_bills_path, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    if entry.get("status") == "created" and entry.get("bill_id"):
                        created_bills_map[entry["file"]] = entry
        except Exception:
            pass

    # Classify each invoice
    preview = []
    for inv in invoices:
        raw_inv_number = inv.get("invoice_number", "")
        vendor_name = inv.get("vendor_name", "")
        amount = inv.get("amount", 0)
        inv_date = inv.get("date", "")
        has_reliable = bool(raw_inv_number and raw_inv_number.lower().strip() not in _GENERIC_NUMBERS)
        # GitHub receipt files use generic receipt numbers — fall back to filename
        fname = inv.get("file", "")
        if has_reliable and fname.lower().startswith("github") and "receipt" in fname.lower():
            has_reliable = False

        inv_number = raw_inv_number if has_reliable else re.sub(r'\.(pdf|eml)$', '', fname, flags=re.IGNORECASE)
        bill_number = inv_number
        bill_number_legacy = f"INV-{inv_number}"  # for matching old bills

        entry = {
            "file": inv.get("file", ""),
            "invoice_number": raw_inv_number,
            "vendor_name": vendor_name,
            "amount": amount,
            "currency": inv.get("currency", "INR"),
            "date": inv_date,
            "organized_month": inv.get("organized_month", ""),
        }

        # --- Check if already created via Step 3 ---
        fname = inv.get("file", "")
        if fname in created_bills_map:
            cb = created_bills_map[fname]
            entry["action"] = "skip"
            entry["matched_bill_id"] = cb.get("bill_id", "")
            entry["match_type"] = "created_bills"
            entry["matched_bill"] = f"Created: {cb.get('vendor_name', '')}"
            preview.append(entry)
            continue

        # --- Check if bill already exists ---
        matched_bill = None
        match_type = None

        # 1. Exact bill number match (check both new format and legacy INV- prefix)
        if bill_number in bills_exact:
            matched_bill = bills_exact[bill_number]
            match_type = "exact"
        elif bill_number_legacy in bills_exact:
            matched_bill = bills_exact[bill_number_legacy]
            match_type = "exact"
        # 2. Normalized match (e.g., INV-02148314-0004 vs 02148314-0004)
        elif has_reliable:
            norm = _normalize_bill_number(inv_number)
            if norm and norm in bills_norm:
                matched_bill = bills_norm[norm]
                match_type = "normalized"
        # 3. Vendor+date fallback (only if no reliable invoice number)
        if not matched_bill and not has_reliable and vendor_name and inv_date:
            vn_lower = vendor_name.strip().lower()
            # Also try mapped vendor name
            names_to_check = {vn_lower}
            mapped_name = vendor_mappings_data.get(vendor_name) or vendor_mappings_data.get(vn_lower)
            if mapped_name:
                names_to_check.add(mapped_name.strip().lower())
            for n in names_to_check:
                if (n, inv_date) in bills_vendor_date:
                    matched_bill = bills_vendor_date[(n, inv_date)]
                    match_type = "vendor_date"
                    break

        if matched_bill:
            entry["action"] = "skip"
            entry["matched_bill"] = matched_bill.get("bill_number", "")
            entry["matched_bill_id"] = matched_bill.get("bill_id", "")
            entry["match_type"] = match_type
            preview.append(entry)
            continue

        # --- Check if vendor exists ---
        vendor_found = None
        vendor_match_method = None
        inv_gstin = (inv.get("vendor_gstin") or "").strip()

        # Priority 1: GSTIN match (most reliable)
        if inv_gstin and inv_gstin in vendor_gstin_map:
            vendor_found = vendor_gstin_map[inv_gstin]
            vendor_match_method = "gstin"

        # Priority 2: Name-based matching
        if not vendor_found and vendor_name:
            vn_lower = vendor_name.strip().lower()
            vn_norm = _norm_v(vendor_name)
            # Direct vendor match
            if vn_lower in vendor_name_map:
                vendor_found = vendor_name_map[vn_lower]
                vendor_match_method = "name"
            elif vn_norm in vendor_norm_map:
                vendor_found = vendor_norm_map[vn_norm]
                vendor_match_method = "name"
            else:
                # Try via vendor_mappings
                mapped_name = vendor_mappings_data.get(vendor_name) or vendor_mappings_data.get(vn_lower)
                if mapped_name:
                    mn_lower = mapped_name.strip().lower()
                    mn_norm = _norm_v(mapped_name)
                    if mn_lower in vendor_name_map:
                        vendor_found = vendor_name_map[mn_lower]
                        vendor_match_method = "name"
                    elif mn_norm in vendor_norm_map:
                        vendor_found = vendor_norm_map[mn_norm]
                        vendor_match_method = "name"
            # Fuzzy match against Zoho vendor names (catches "Microsoft" ≈ "Microsoft Pvt Ltd")
            if not vendor_found:
                from thefuzz import fuzz
                best_score, best_vendor = 0, None
                for vkey, vinfo in vendor_name_map.items():
                    score = fuzz.token_set_ratio(vn_lower, vkey)
                    if score > best_score:
                        best_score, best_vendor = score, vinfo
                if best_score >= 85:
                    vendor_found = best_vendor
                    vendor_match_method = "fuzzy"

        if vendor_found:
            entry["action"] = "new_bill"
            entry["matched_vendor_id"] = vendor_found.get("contact_id", "")
            entry["matched_vendor_name"] = vendor_found.get("contact_name", "")
            entry["vendor_match_method"] = vendor_match_method
            # Flag if vendor matched but GSTIN missing in Zoho
            if vendor_match_method != "gstin" and inv_gstin:
                zoho_gst = (vendor_found.get("gst_no") or "").strip()
                if not zoho_gst:
                    entry["gstin_missing"] = True
        else:
            entry["action"] = "new_vendor_bill"

        preview.append(entry)

    # Save preview for reference
    preview_path = os.path.join(PROJECT_ROOT, "output", "bill_match_preview.json")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    with open(preview_path, "w", encoding="utf-8") as f:
        json.dump(preview, f, indent=2, ensure_ascii=False)

    # Summary counts
    skip_count = sum(1 for p in preview if p["action"] == "skip")
    new_bill_count = sum(1 for p in preview if p["action"] == "new_bill")
    new_vendor_bill_count = sum(1 for p in preview if p["action"] == "new_vendor_bill")

    log_action(f"Match preview: {skip_count} skip, {new_bill_count} new bills, {new_vendor_bill_count} new vendor+bill")

    return jsonify({
        "preview": preview,
        "summary": {
            "total": len(preview),
            "skip": skip_count,
            "new_bill": new_bill_count,
            "new_vendor_bill": new_vendor_bill_count,
        },
    })


@app.route("/api/compare/save-categorize", methods=["POST"])
def api_save_categorize():
    """Save categorize check results to output/categorize_<month>.json."""
    data = request.json or {}
    month = data.get("month", "unknown")
    rows = data.get("rows", [])
    summary = data.get("summary", {})

    # "Jan 2026" -> "categorize_jan_2026.json"
    safe_name = month.lower().replace(" ", "_")
    filename = f"categorize_{safe_name}.json"
    output_path = os.path.join(PROJECT_ROOT, "output", filename)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "month": month,
            "rows": rows,
            "summary": summary,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, f, indent=2, ensure_ascii=False)

    return jsonify({"status": "ok", "month": month, "path": output_path})


@app.route("/api/compare/categorize-overall")
def api_categorize_overall():
    """Aggregate counts from all saved categorize_*.json files."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    totals = {"exact": 0, "close": 0, "cross_exact": 0, "cross_close": 0,
              "no_invoice": 0, "unmapped": 0, "no_cc": 0, "total": 0, "months_done": 0}
    months_detail = []
    for fn in sorted(glob.glob(os.path.join(output_dir, "categorize_*.json"))):
        try:
            with open(fn, encoding="utf-8") as f:
                data = json.load(f)
            counts = {"exact": 0, "close": 0, "cross_exact": 0, "cross_close": 0,
                       "no_invoice": 0, "unmapped": 0, "no_cc": 0}
            for row in data.get("rows", []):
                s = row.get("status", "")
                if s in counts:
                    counts[s] += 1
            month_total = sum(counts.values())
            for k in counts:
                totals[k] += counts[k]
            totals["total"] += month_total
            totals["months_done"] += 1
            months_detail.append({"month": data.get("month", ""), "counts": counts, "total": month_total})
        except Exception:
            continue
    return jsonify({"totals": totals, "months": months_detail})


# --- HTML Dashboard (embedded) ---

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CC Statement Automation</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #232733;
    --border: #2e3345;
    --text: #e1e4ed;
    --text-dim: #8b90a0;
    --accent: #6c8cff;
    --accent-hover: #8ba4ff;
    --green: #4ade80;
    --red: #f87171;
    --orange: #fb923c;
    --yellow: #facc15;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    overflow: hidden;
  }
  .container { display: flex; flex-direction: column; height: 100vh; padding: 16px 20px; }

  /* Header */
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .header h1 {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.5px;
  }
  .header h1 span { color: var(--accent); }
  .status-badge {
    padding: 5px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
  }
  .status-idle { background: var(--surface2); color: var(--text-dim); }
  .status-running { background: rgba(108,140,255,0.15); color: var(--accent); animation: pulse 2s infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }

  /* Main two-column layout */
  .main-layout {
    display: flex;
    gap: 16px;
    flex: 1;
    min-height: 0;
  }

  /* Left panel — phases (20%) */
  .left-panel {
    width: 20%;
    min-width: 200px;
    display: flex;
    flex-direction: column;
    gap: 0;
    overflow: visible;
    flex-shrink: 0;
  }
  .left-panel-scroll {
    flex: 1;
    overflow-y: auto;
    overflow-x: visible;
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-height: calc(100vh - 60px);
    padding-right: 4px;
  }

  /* Right panel — logs (80%) */
  .right-panel {
    width: 80%;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  /* Phase sections */
  .phase {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
  }
  .phase-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .step-grid {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* Step buttons */
  .step-btn {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
    width: 100%;
  }
  .step-btn:hover:not(:disabled) {
    border-color: var(--accent);
    background: rgba(108,140,255,0.08);
  }
  .step-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .step-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 5px;
    background: var(--bg);
    font-size: 11px;
    font-weight: 700;
    flex-shrink: 0;
  }
  .step-indicator {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-left: auto;
    flex-shrink: 0;
  }
  .ind-idle { background: var(--border); }
  .ind-running { background: var(--accent); animation: pulse 1s infinite; }
  .ind-success { background: var(--green); }
  .ind-error { background: var(--red); }

  /* Step result tooltip */
  .step-btn .step-msg {
    display: none;
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 11px;
    white-space: nowrap;
    z-index: 10;
    color: var(--text-dim);
    pointer-events: none;
  }
  .step-btn { position: relative; }
  .step-btn:hover .step-msg { display: block; }

  /* Info icon + tooltip */
  .info-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--border);
    color: var(--text-dim);
    font-size: 10px;
    font-weight: 700;
    font-style: italic;
    cursor: help;
    flex-shrink: 0;
    position: relative;
    margin-left: auto;
  }
  .info-btn:hover { background: var(--accent); color: #fff; }
  .info-tooltip {
    display: none;
    position: fixed;
    background: var(--surface2);
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 11.5px;
    font-style: normal;
    font-weight: 400;
    line-height: 1.5;
    white-space: normal;
    width: 280px;
    z-index: 9999;
    color: var(--text);
    pointer-events: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  }

  /* Action row */
  .action-row {
    display: flex;
    gap: 6px;
  }
  .btn-primary {
    padding: 8px 14px;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    flex: 1;
  }
  .btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-danger {
    padding: 8px 14px;
    background: transparent;
    color: var(--red);
    border: 1px solid rgba(248,113,113,0.3);
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    flex: 1;
  }
  .btn-danger:hover:not(:disabled) { background: rgba(248,113,113,0.1); }
  .btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Summary bar */
  .summary-bar {
    display: flex;
    gap: 12px;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    flex-wrap: wrap;
  }
  .summary-item {
    font-size: 11px;
    color: var(--text-dim);
  }
  .summary-item strong {
    color: var(--text);
    font-size: 15px;
    font-weight: 600;
    margin-right: 2px;
  }

  /* Log panel */
  .log-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .log-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
    flex-shrink: 0;
  }
  .log-header button {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .log-header button:hover { color: var(--text); }
  #logBox {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 12.5px;
    line-height: 1.7;
  }
  #logBox::-webkit-scrollbar { width: 6px; }
  #logBox::-webkit-scrollbar-track { background: transparent; }
  #logBox::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-line-INFO { color: var(--text-dim); }
  .log-line-WARNING { color: var(--orange); }
  .log-line-ERROR { color: var(--red); }

  /* Confirmation modal */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    backdrop-filter: blur(4px);
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .modal-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 28px 32px;
    min-width: 360px;
    max-width: 440px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  }
  .modal-title {
    font-size: 17px;
    font-weight: 700;
    margin-bottom: 10px;
  }
  .modal-msg {
    font-size: 13px;
    color: var(--text-dim);
    line-height: 1.6;
    margin-bottom: 24px;
  }
  .modal-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
  .modal-btn {
    padding: 9px 20px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    transition: all 0.15s;
  }
  .modal-btn-cancel {
    background: var(--surface2);
    color: var(--text-dim);
    border: 1px solid var(--border);
  }
  .modal-btn-cancel:hover { color: var(--text); background: var(--bg); }
  .modal-btn-confirm {
    background: var(--accent);
    color: #fff;
  }
  .modal-btn-confirm:hover { background: var(--accent-hover); }
  .modal-btn-confirm.danger {
    background: var(--red);
  }
  .modal-btn-confirm.danger:hover { background: #ef4444; }

  /* Bill Picker — Filter Bar */
  .bill-filter-bar {
    display: flex; flex-wrap: wrap; gap: 8px; padding: 8px 0 10px; align-items: flex-end;
  }
  .bill-filter-group { display: flex; flex-direction: column; gap: 2px; }
  .bill-filter-group label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .bill-filter-group select,
  .bill-filter-group input {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 12px; padding: 4px 8px; min-width: 90px;
  }
  .bill-filter-group input[type="number"] { width: 80px; }

  /* Checkbox Dropdown */
  .cb-dropdown { position: relative; display: inline-block; }
  .cb-dropdown-btn {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 12px; padding: 4px 10px; cursor: pointer;
    display: flex; align-items: center; gap: 6px; white-space: nowrap; min-width: 90px;
  }
  .cb-dropdown-btn:hover { border-color: var(--text-dim); }
  .cb-dropdown-btn .cb-badge {
    background: var(--accent); color: #fff; font-size: 10px; font-weight: 700;
    border-radius: 8px; padding: 0 5px; min-width: 16px; text-align: center; line-height: 16px;
  }
  .cb-dropdown-panel {
    display: none; position: absolute; top: 100%; left: 0; z-index: 20;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3); min-width: 180px; max-height: 260px;
    flex-direction: column; margin-top: 4px;
  }
  .cb-dropdown-panel.open { display: flex; }
  .cb-dropdown-actions {
    display: flex; gap: 8px; padding: 6px 10px; border-bottom: 1px solid var(--border);
    font-size: 11px;
  }
  .cb-dropdown-actions a {
    color: var(--accent); cursor: pointer; text-decoration: none;
  }
  .cb-dropdown-actions a:hover { text-decoration: underline; }
  .cb-dropdown-list { overflow-y: auto; padding: 4px 0; flex: 1; }
  .cb-dropdown-list label {
    display: flex; align-items: center; gap: 6px; padding: 3px 10px; font-size: 12px;
    cursor: pointer; white-space: nowrap;
  }
  .cb-dropdown-list label:hover { background: var(--surface2); }
  .cb-dropdown-list input[type="checkbox"] { accent-color: var(--accent); }

  /* Mapping Bar */
  .bill-mapping-bar {
    display: flex; align-items: center; gap: 10px; padding: 8px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 8px; font-size: 13px;
  }
  .bill-mapping-bar label { color: var(--text-dim); white-space: nowrap; font-size: 12px; }
  .bill-mapping-bar .modal-btn { padding: 5px 14px; font-size: 12px; }

  /* Searchable Dropdown */
  .search-dropdown { position: relative; flex: 1; max-width: 300px; }
  .search-dropdown input {
    width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 12px; padding: 5px 10px; box-sizing: border-box;
  }
  .search-dropdown-list {
    display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 20;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3); max-height: 200px; overflow-y: auto;
    margin-top: 4px;
  }
  .search-dropdown-list.open { display: block; }
  .sd-item {
    padding: 5px 10px; font-size: 12px; cursor: pointer; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .sd-item:hover { background: var(--surface2); }
  .sd-item.selected { background: var(--accent); color: #fff; }
  .bill-filter-clear {
    padding: 4px 12px; border-radius: 6px; font-size: 11px; font-weight: 600;
    border: 1px solid var(--border); background: transparent; color: var(--text-dim);
    cursor: pointer; align-self: flex-end;
  }
  .bill-filter-clear:hover { color: var(--text); border-color: var(--text-dim); }

  /* Bill Picker — Table */
  .bill-table-wrap { flex: 1; overflow-y: auto; min-height: 0; }
  .bill-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .bill-table thead { position: sticky; top: 0; z-index: 2; background: var(--surface); }
  .bill-table th {
    padding: 6px 8px; text-align: left; font-size: 11px; font-weight: 600;
    color: var(--text-dim); border-bottom: 1px solid var(--border); cursor: pointer;
    user-select: none; white-space: nowrap;
  }
  .bill-table th .sort-arrow { font-size: 10px; margin-left: 2px; opacity: 0.3; }
  .bill-table th.sorted .sort-arrow { opacity: 1; color: var(--accent); }
  .bill-table td { padding: 5px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }
  .bill-table tr.row-skip { opacity: 0.45; }
  .bill-table .col-checkbox { width: 32px; text-align: center; }
  .bill-table .col-amount { text-align: right; font-family: monospace; font-size: 11px; white-space: nowrap; }
  .bill-table .col-action { width: 70px; text-align: center; }
  .bill-table .vendor-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Bill Picker — Status & Action */
  .bill-status-badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 10px; font-weight: 600; text-transform: uppercase;
  }
  .bill-status-badge.pending { background: rgba(251,191,36,0.15); color: #fbbf24; }
  .bill-status-badge.created { background: rgba(52,211,153,0.15); color: #34d399; }
  .bill-create-btn {
    padding: 3px 10px; border-radius: 6px; font-size: 11px; font-weight: 600;
    border: 1px solid var(--accent); background: transparent; color: var(--accent);
    cursor: pointer; white-space: nowrap;
  }
  .bill-create-btn:hover { background: var(--accent); color: #fff; }

  /* Bill Picker — Vertical Layout */
  .bill-picker-layout { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  .bill-picker-left { display: flex; flex-direction: column; min-height: 0; flex: 1; }
  .bill-picker-right {
    display: flex; flex-direction: row; flex-wrap: wrap; align-items: center; gap: 12px;
    padding: 10px 0; border-top: 1px solid var(--border); margin-top: 8px;
  }
  .bill-selected-count { font-size: 13px; font-weight: 600; padding: 6px 0; color: var(--accent); }

  /* Bill Picker — Bottom Bar Summary */
  .bill-summary-stat {
    display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-dim);
  }
  .bill-summary-stat .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .bill-summary-stat .count { font-weight: 700; font-family: monospace; color: var(--text); }
  .bill-summary-actions { margin-left: auto; display: flex; gap: 8px; }

  /* Zoho Vendor Column */
  .col-zoho-vendor { max-width: 220px; white-space: nowrap; position: relative; }
  .zoho-vendor-display { display: flex; align-items: center; gap: 4px; }
  .zoho-vendor-display .vendor-text { overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }
  .zoho-vendor-edit-btn {
    flex-shrink: 0; border: none; background: none; color: var(--text-dim); cursor: pointer;
    font-size: 11px; padding: 1px 4px; border-radius: 4px; opacity: 0.5; line-height: 1;
  }
  .zoho-vendor-edit-btn:hover { opacity: 1; background: var(--surface2); color: var(--accent); }
  .row-vendor-dropdown {
    position: absolute; top: 100%; left: 0; z-index: 30; width: 220px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4); display: none;
  }
  .row-vendor-dropdown.open { display: block; }
  .row-vendor-dropdown input {
    width: 100%; background: var(--bg); border: none; border-bottom: 1px solid var(--border);
    color: var(--text); font-size: 12px; padding: 6px 8px; box-sizing: border-box;
    border-radius: 8px 8px 0 0;
  }
  .row-vendor-dropdown .rvd-list { max-height: 180px; overflow-y: auto; }

  /* Review badge */
  .review-badge {
    background: var(--accent) !important;
    color: #fff !important;
    font-size: 10px !important;
  }
  .review-btn {
    border-style: dashed !important;
    border-color: var(--accent) !important;
    opacity: 0.85;
  }
  .review-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }

  /* Review panel */
  .review-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .review-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
    flex-shrink: 0;
  }
  .review-close-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .review-close-btn:hover { color: var(--red); border-color: var(--red); }
  .review-create-btn {
    background: rgba(108,140,255,0.1);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .review-create-btn:hover { background: rgba(108,140,255,0.2); }
  .review-body {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
  }
  .review-loading {
    text-align: center;
    color: var(--text-dim);
    padding: 40px 0;
    font-size: 13px;
  }
  .review-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .review-table th {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-dim);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .review-table td {
    padding: 8px 10px;
    border-bottom: 1px solid rgba(46,51,69,0.5);
    vertical-align: middle;
  }
  .review-table select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 5px 8px;
    font-size: 12px;
    width: 100%;
    max-width: 220px;
  }
  .review-table select:focus { border-color: var(--accent); outline: none; }
  .review-save-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
  }
  .review-save-btn:hover { border-color: var(--accent); color: var(--accent); }
  .review-save-btn.saved {
    border-color: var(--green);
    color: var(--green);
  }
  .review-save-btn.save-error {
    border-color: var(--red);
    color: var(--red);
  }
  .review-save-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .vendor-group-header td {
    background: var(--surface2);
    padding: 10px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 13px;
    color: var(--accent);
  }
  .vendor-group-header .vendor-bulk-row {
    display: flex;
    align-items: center;
    gap: 10px;
    justify-content: space-between;
  }
  .vendor-group-header .vendor-name-label {
    flex-shrink: 0;
    min-width: 120px;
  }
  .vendor-group-header .vendor-bill-count {
    font-size: 11px;
    color: var(--text-dim);
    font-weight: 400;
  }
  .vendor-bulk-select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 5px 8px;
    font-size: 12px;
    min-width: 180px;
  }
  .vendor-bulk-select:focus { border-color: var(--accent); outline: none; }
  .apply-all-btn {
    background: rgba(108,140,255,0.1);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 5px 14px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
    white-space: nowrap;
    font-weight: 600;
  }
  .apply-all-btn:hover { background: rgba(108,140,255,0.25); }
  .apply-all-btn.saved { border-color: var(--green); color: var(--green); }
  .apply-all-btn.save-error { border-color: var(--red); color: var(--red); }
  .apply-all-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Match Status panel */
  .match-summary {
    display: flex;
    gap: 16px;
    margin-bottom: 16px;
  }
  .match-stat {
    padding: 12px 20px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    text-align: center;
    flex: 1;
  }
  .match-stat .stat-value {
    font-size: 28px;
    font-weight: 700;
  }
  .match-stat .stat-label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 2px;
  }
  .stat-matched .stat-value { color: var(--green); }
  .stat-unmatched .stat-value { color: var(--orange); }
  .stat-total .stat-value { color: var(--accent); }
  .match-tabs {
    display: flex;
    gap: 0;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .match-tab {
    padding: 8px 20px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--text-dim);
    border-bottom: 2px solid transparent;
    transition: all 0.15s;
  }
  .match-tab:hover { color: var(--text); }
  .match-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .match-tab.tab-matched.active { color: var(--green); border-bottom-color: var(--green); }
  .match-tab.tab-unmatched.active { color: var(--orange); border-bottom-color: var(--orange); }
  .match-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .match-table th {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-dim);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
    background: var(--surface2);
    z-index: 1;
  }
  .match-table td {
    padding: 8px 10px;
    border-bottom: 1px solid rgba(46,51,69,0.5);
    vertical-align: middle;
  }
  .match-table .status-paid { color: var(--green); font-weight: 600; }
  .match-table .status-unmatched { color: var(--orange); font-weight: 600; }
  .match-table .reason-cell { font-size: 11px; color: var(--red); max-width: 300px; }
  .cat-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    border: 1px solid var(--border);
  }
  .cat-table th {
    text-align: left;
    padding: 8px 10px;
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
    background: var(--surface2);
    z-index: 1;
  }
  .cat-table td {
    padding: 8px 10px;
    border: 1px solid var(--border);
    vertical-align: middle;
  }
  .match-btn {
    border-style: dashed !important;
    border-color: var(--orange) !important;
    opacity: 0.85;
  }
  .match-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }
  .check-btn {
    border-style: dashed !important;
    border-color: var(--yellow) !important;
    opacity: 0.85;
  }
  .check-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }
  .compare-btn {
    border-style: dashed !important;
    border-color: var(--green, #4ade80) !important;
    opacity: 0.85;
  }
  .compare-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }

  /* Step with upload */
  .step-with-upload {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .upload-row {
    display: flex;
  }
  .upload-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    width: 100%;
    padding: 5px 10px;
    background: transparent;
    border: 1px dashed var(--border);
    border-radius: 6px;
    color: var(--text-dim);
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .upload-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(108,140,255,0.05);
  }
  .upload-step-btn {
    cursor: pointer;
    border-style: dashed;
  }
  .upload-step-btn:hover {
    border-color: var(--accent);
    background: rgba(108,140,255,0.05);
  }
  }
</style>
</head>
<body>
<div class="container">
  <!-- Header -->
  <div class="header">
    <h1><span>CC</span> Statement Automation</h1>
    <div id="globalStatus" class="status-badge status-idle">Idle</div>
  </div>

  <!-- Two-column layout -->
  <div class="main-layout">
    <!-- Left panel: Phases -->
    <div class="left-panel">
    <div class="left-panel-scroll">
      <!-- Box 1: Invoices → Compare -->
      <div class="phase">
        <div class="phase-label">Invoices &rarr; Compare</div>
        <div class="step-grid">
          <button class="step-btn" data-step="1" onclick="runStep('1')">
            <span class="step-num">1</span> Fetch Invoices
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Connects to Outlook via Microsoft Graph API, searches inbox for invoice/receipt emails, and downloads PDF attachments to input_pdfs/invoices/</span>
            </span>
            <span class="step-indicator ind-idle" id="ind-1"></span>
            <span class="step-msg" id="msg-1"></span>
          </button>
          <button class="step-btn" onclick="runExtractZips()" id="btn-extract-zips" style="border:1.5px dashed var(--accent);background:rgba(108,140,255,0.05)">
            <span class="step-num" style="background:var(--accent);color:#fff;font-size:10px">Z</span> Extract ZIPs
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Extract PDFs from ZIP files (and loose PDFs) in 'all zips' folder, then parse and organize into month-wise folders.</span>
            </span>
            <span class="step-indicator ind-idle" id="ind-extract-zips"></span>
            <span class="step-msg" id="msg-extract-zips"></span>
          </button>
          <button class="step-btn" data-step="2" onclick="runStep('2')">
            <span class="step-num">2</span> Extract Data
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Reads each invoice PDF using pdfplumber + OCR fallback. Extracts vendor name, amount, date, currency, and invoice number.</span>
            </span>
            <span class="step-indicator ind-idle" id="ind-2"></span>
            <span class="step-msg" id="msg-2"></span>
          </button>
          <button class="step-btn compare-btn" onclick="openComparePanel()">
            <span class="step-num" style="background:var(--green, #4ade80);color:#000;font-size:10px">$</span> Monthly Compare
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Compare CC statement transactions vs organized invoices side-by-side, grouped by month. Shows vendor GSTIN and forex details.</span>
            </span>
          </button>
        </div>
      </div>

      <!-- Box 2: Bills -->
      <div class="phase">
        <div class="phase-label">Bills</div>
        <div class="step-grid">
          <button class="step-btn" onclick="syncZoho()" style="border:1.5px dashed var(--accent);background:rgba(108,140,255,0.05)">
            <span class="step-num" style="background:var(--accent);color:#fff;font-size:10px">S</span> Sync Zoho
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Pull all existing bills, vendors &amp; CC bank accounts from Zoho Books into local cache. Run this before creating bills to enable smart dedup.</span>
            </span>
            <span class="step-indicator ind-idle" id="ind-sync"></span>
            <span class="step-msg" id="msg-sync"></span>
          </button>
          <div class="step-with-upload">
            <div class="upload-row">
              <div class="step-btn" data-step="3" style="cursor:default">
                <span class="step-num">3</span> Create Bills
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Use Upload 1 to pick invoices with match preview, or Upload All to create bills for all new invoices (skips existing).</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-3"></span>
                <span class="step-msg" id="msg-3"></span>
              </div>
            </div>
            <div style="display:flex;gap:6px;margin-top:4px">
              <button class="upload-btn" onclick="openBillPicker()" style="flex:1">Upload 1</button>
              <button class="upload-btn" onclick="openBillPicker()" style="flex:1">Upload All</button>
            </div>
          </div>
          <button class="step-btn review-btn" onclick="openReviewPanel()">
            <span class="step-num review-badge">R</span> Review Accounts
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Review and fix expense account assignments on bills created in Step 3. You can change accounts and create new ones. This is optional &mdash; skipped during Run All.</span>
            </span>
          </button>
        </div>
      </div>

      <!-- Box 3: Payments -->
      <div class="phase">
        <div class="phase-label">Payments</div>
        <div class="step-grid">
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" data-step="6" onclick="openPaymentPreview()" style="width:100%">
                <span class="step-num">6</span> RecordPayment
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Preview bill-to-CC matches, then record payments individually or in bulk. Automatically categorizes the CC banking transaction in Zoho after recording (no separate auto-match needed).</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-6"></span>
                <span class="step-msg" id="msg-6"></span>
              </button>
            </div>
            <div style="margin-top:4px">
              <button class="upload-btn" onclick="clearPaymentsCache()" style="width:100%">Clear Cache</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Box 4: Banking -->
      <div class="phase">
        <div class="phase-label">Banking</div>
        <div class="step-grid">
          <div class="step-with-upload">
            <div class="upload-row">
              <input type="file" id="ccUploadInput" accept=".pdf" multiple style="display:none" onchange="handleCCUpload(this)">
              <label for="ccUploadInput" class="step-btn upload-step-btn">
                <span class="step-num">4</span> Upload &amp; Parse CC
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Upload CC statement PDFs (HDFC, Kotak, Mayura) to parse into transactions. Only uploaded files are parsed.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-4"></span>
                <span class="step-msg" id="msg-4"></span>
              </label>
            </div>
            <div style="margin-top:4px">
              <button class="upload-btn" onclick="clearParsedCC()" style="width:100%">Clear Parsed Data</button>
            </div>
          </div>
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" data-step="5" onclick="openImportPicker()" style="width:100%">
                <span class="step-num">5</span> Import Banking
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Choose which CC card's parsed transactions to import into Zoho Books Banking. Transactions appear as 'Uncategorized' in each CC account.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-5"></span>
                <span class="step-msg" id="msg-5"></span>
              </button>
            </div>
            <div style="margin-top:4px">
              <button class="upload-btn" onclick="clearImportCache()" style="width:100%">Clear Cache</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Action buttons (hidden — backend logic retained) -->
      <div class="action-row" style="display:none;">
        <button class="btn-primary" id="btnRunAll" onclick="confirmRunAll()">Run All</button>
        <button class="btn-danger" id="btnCleanup" onclick="confirmCleanup()">Cleanup</button>
      </div>

      <!-- Confirmation modal -->
      <div id="confirmModal" class="modal-overlay" style="display:none;z-index:10000">
        <div class="modal-box">
          <div class="modal-title" id="modalTitle"></div>
          <div class="modal-msg" id="modalMsg"></div>
          <div class="modal-actions">
            <button class="modal-btn modal-btn-cancel" onclick="closeModal()">No, Cancel</button>
            <button class="modal-btn modal-btn-confirm" id="modalConfirmBtn">Yes, Proceed</button>
          </div>
        </div>
      </div>

      <!-- Summary -->
      <div class="summary-bar" id="summaryBar">
        <div class="summary-item"><strong id="sumInvoices">-</strong> Invoices</div>
        <div class="summary-item"><strong id="sumBills">-</strong> Bills</div>
        <div class="summary-item"><strong id="sumCC">-</strong> CC Txns</div>
      </div>
    </div><!-- end left-panel-scroll -->
    </div><!-- end left-panel -->

    <!-- Right panel: Logs + Review -->
    <div class="right-panel">
      <div class="log-panel" id="logPanel">
        <div class="log-header">
          <span>Live Logs</span>
          <button onclick="clearLogs()">Clear</button>
        </div>
        <div id="logBox"></div>
      </div>

      <!-- Review panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="reviewPanel" style="display:none">
        <div class="review-header">
          <span>Review Expense Accounts</span>
          <div style="display:flex;gap:8px;align-items:center">
            <button class="review-create-btn" onclick="openCreateAccountModal()">+ New Account</button>
            <button class="review-close-btn" onclick="closeReviewPanel()">&#10005; Close</button>
          </div>
        </div>
        <div class="review-body" id="reviewBody">
          <div class="review-loading" id="reviewLoading">Loading bills...</div>
          <table class="review-table" id="reviewTable" style="display:none">
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Amount</th>
                <th>Currency</th>
                <th>Current Account</th>
                <th>Change To</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="reviewTableBody"></tbody>
          </table>
        </div>
      </div>

      <!-- Match Status panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="matchPanel" style="display:none">
        <div class="review-header">
          <span>Match Status</span>
          <button class="review-close-btn" onclick="closeMatchPanel()">&#10005; Close</button>
        </div>
        <div class="review-body" id="matchBody">
          <div class="review-loading" id="matchLoading">Loading match data...</div>
          <div id="matchContent" style="display:none">
            <div class="match-summary">
              <div class="match-stat stat-matched">
                <div class="stat-value" id="matchedCount">0</div>
                <div class="stat-label">Matched</div>
              </div>
              <div class="match-stat stat-unmatched">
                <div class="stat-value" id="unmatchedCount">0</div>
                <div class="stat-label">Unmatched</div>
              </div>
              <div class="match-stat stat-total">
                <div class="stat-value" id="totalCount">0</div>
                <div class="stat-label">Total</div>
              </div>
            </div>
            <div class="match-tabs">
              <button class="match-tab tab-matched active" onclick="switchMatchTab('matched')">Matched</button>
              <button class="match-tab tab-unmatched" onclick="switchMatchTab('unmatched')">Unmatched</button>
            </div>
            <table class="match-table">
              <thead><tr id="matchTableHead"></tr></thead>
              <tbody id="matchTableBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Check CC Match panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="checkPanel" style="display:none">
        <div class="review-header">
          <span>Check CC Match (cached)</span>
          <div style="display:flex;gap:16px;align-items:center">
            <span id="checkSummaryText" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeCheckPanel()">&#10005; Close</button>
          </div>
        </div>
        <div id="checkBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="checkLoading" style="align-self:center;width:100%;text-align:center">Fetching from Zoho...</div>
          <div id="checkContent" style="display:none;flex:1;overflow-y:auto;flex-direction:column"></div>
        </div>
      </div>

      <!-- Payment Preview panel -->
      <div class="review-panel" id="paymentPanel" style="display:none">
        <div class="review-header">
          <span>Record Payments &mdash; CC &harr; Bill Match</span>
          <div style="display:flex;gap:12px;align-items:center">
            <select id="paymentCardFilter" onchange="filterPaymentsByCard(this.value)" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px;display:none">
              <option value="">All Cards</option>
            </select>
            <span id="paymentSummaryText" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button id="recordSelectedBtn" onclick="confirmRecordSelected()" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;display:none">Record Selected (0)</button>
            <button class="review-close-btn" onclick="closePaymentPanel()">&#10005; Close</button>
          </div>
        </div>
        <div id="paymentBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="paymentLoading" style="align-self:center;width:100%;text-align:center">Fetching bills &amp; CC transactions...</div>
          <div id="paymentContent" style="display:none;flex:1;overflow-y:auto"></div>
        </div>
      </div>

      <!-- Monthly Compare panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="comparePanel" style="display:none">
        <div class="review-header">
          <span>Monthly Compare &mdash; CC vs Invoices</span>
          <div style="display:flex;gap:12px;align-items:center">
            <button id="parseAllBillsBtn" onclick="parseOrgInvoices()" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer">Parse All Invoices</button>
            <button id="parseAllCCBtn" onclick="parseAllForCompare('4','parseAllCCBtn','CC')" style="background:var(--yellow);color:#000;border:none;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer">Parse All CC</button>
            <select id="compareMonthSelect" onchange="renderCompareMonth(this.value)" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px"></select>
            <span id="compareSummaryText" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeComparePanel()">&#10005; Close</button>
          </div>
        </div>
        <div id="compareFilterBar" style="display:none;padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02);gap:12px;align-items:center;flex-shrink:0;flex-wrap:wrap;font-size:12px">
          <label style="color:var(--text-dim)">Vendor:</label>
          <select id="compareVendorFilter" onchange="applyCompareFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px;max-width:200px"></select>
          <label style="color:var(--text-dim);margin-left:12px">From:</label>
          <input type="date" id="compareDateFrom" onchange="applyCompareFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <label style="color:var(--text-dim)">To:</label>
          <input type="date" id="compareDateTo" onchange="applyCompareFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <button onclick="clearCompareFilters()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer">Clear</button>
        </div>
        <div id="compareBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="compareLoading" style="align-self:center;width:100%;text-align:center">Loading data...</div>
          <div id="compareContent" style="display:none;flex:1;overflow-y:auto;flex-direction:column"></div>
        </div>
      </div>

      <!-- Import Picker modal -->
      <div id="importPickerModal" class="modal-overlay" style="display:none">
        <div class="modal-box">
          <div class="modal-title">Select Cards to Import</div>
          <div id="importPickerBody" style="margin-bottom:16px">
            <div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading available CSVs...</div>
          </div>
          <div class="modal-actions">
            <button class="modal-btn modal-btn-cancel" onclick="closeImportPicker()">Cancel</button>
            <button class="modal-btn modal-btn-confirm" onclick="importSelectedCards()">Import Selected</button>
          </div>
        </div>
      </div>

      <!-- Bill Picker modal -->
      <div id="billPickerModal" class="modal-overlay" style="display:none">
        <div class="modal-box" style="max-width:1100px;max-height:85vh;width:95vw;display:flex;flex-direction:column">
          <div class="modal-title">Select Invoices to Create Bills</div>
          <div class="bill-picker-layout">
            <div class="bill-picker-left" id="billPickerBody">
              <div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading invoices...</div>
            </div>
            <div class="bill-picker-right" id="billPickerSummary"></div>
          </div>
        </div>
      </div>

      <!-- Create Account modal -->
      <div id="createAccountModal" class="modal-overlay" style="display:none">
        <div class="modal-box">
          <div class="modal-title">Create New Expense Account</div>
          <div style="margin-bottom:16px">
            <label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px">Account Name</label>
            <input type="text" id="newAccountName" placeholder="e.g. Cloud Infrastructure" style="width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
          </div>
          <div style="margin-bottom:20px">
            <label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px">Description (optional)</label>
            <input type="text" id="newAccountDesc" placeholder="e.g. AWS, GCP hosting costs" style="width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
          </div>
          <div class="modal-actions">
            <button class="modal-btn modal-btn-cancel" onclick="closeCreateAccountModal()">Cancel</button>
            <button class="modal-btn modal-btn-confirm" id="createAccountBtn" onclick="createNewAccount()">Create</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const logBox = document.getElementById('logBox');
let autoScroll = true;

// Track scroll position — disable auto-scroll when user scrolls up
logBox.addEventListener('scroll', () => {
  const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
  autoScroll = atBottom;
});

function addLogLine(text) {
  const div = document.createElement('div');
  div.className = 'log-line';
  // Color based on level
  if (text.includes('[ERROR]')) div.className += ' log-line-ERROR';
  else if (text.includes('[WARNING]')) div.className += ' log-line-WARNING';
  else div.className += ' log-line-INFO';
  div.textContent = text;
  logBox.appendChild(div);
  // Limit to 1000 lines
  while (logBox.children.length > 1000) logBox.removeChild(logBox.firstChild);
  if (autoScroll) logBox.scrollTop = logBox.scrollHeight;
}

function clearLogs() {
  logBox.innerHTML = '';
}

// --- SSE for live logs ---
let evtSource = null;
function connectSSE() {
  evtSource = new EventSource('/api/logs');
  evtSource.onmessage = function(e) {
    try {
      const data = JSON.parse(e.data);
      if (data.line) addLogLine(data.line);
    } catch(err) {}
  };
  evtSource.onerror = function() {
    evtSource.close();
    setTimeout(connectSSE, 3000);
  };
}
connectSSE();

// --- Load log history on page load ---
fetch('/api/logs/history?n=100')
  .then(r => r.json())
  .then(data => {
    (data.lines || []).forEach(l => addLogLine(l));
  });

// --- Run step ---
function runStep(step) {
  fetch('/api/run/' + step, {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[UI] ' + data.error);
      }
      pollStatus();
    })
    .catch(err => addLogLine('[UI] Request failed: ' + err));
}

function runExtractZips() {
  fetch('/api/extract-zips', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[UI] ' + data.error);
      }
      pollStatus();
    })
    .catch(err => addLogLine('[UI] Request failed: ' + err));
}

// --- Confirmation modals ---
function showModal(title, msg, onConfirm, isDanger, confirmText) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalMsg').innerHTML = msg;
  const btn = document.getElementById('modalConfirmBtn');
  btn.className = 'modal-btn modal-btn-confirm' + (isDanger ? ' danger' : '');
  btn.textContent = confirmText || (isDanger ? 'Yes, Delete' : 'Yes, Proceed');
  btn.onclick = function() { closeModal(); onConfirm(); };
  document.getElementById('confirmModal').style.display = 'flex';
}

function closeModal() {
  document.getElementById('confirmModal').style.display = 'none';
}

function confirmRunAll() {
  showModal(
    'Run All Steps (1-6)?',
    'This will execute the full pipeline sequentially: Fetch Invoices, Extract Data, Create Bills, Parse CC, Record Payments, and Import Banking.',
    function() { runStep('all'); },
    false
  );
}

function confirmCleanup() {
  showModal(
    'Delete ALL Zoho Data?',
    'This will permanently DELETE all vendors, bills, payments, and bank transactions from Zoho Books, plus local output files. This cannot be undone.',
    function() { runStep('cleanup'); },
    true
  );
}

// --- Poll status ---
function pollStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(updateUI)
    .catch(() => {});
}

function updateUI(data) {
  // Global status badge
  const badge = document.getElementById('globalStatus');
  if (data.running) {
    badge.className = 'status-badge status-running';
    let label = 'Running';
    if (data.current_step && data.current_step !== 'cleanup') {
      label = 'Running Step ' + data.current_step;
    } else if (data.current_step === 'cleanup') {
      label = 'Cleaning Up';
    }
    badge.textContent = label;
  } else {
    badge.className = 'status-badge status-idle';
    badge.textContent = 'Idle';
  }

  // Disable/enable buttons
  const btns = document.querySelectorAll('.step-btn, .btn-primary, .btn-danger');
  btns.forEach(b => b.disabled = data.running);

  // Step indicators + tooltips
  for (let i = 1; i <= 7; i++) {
    const ind = document.getElementById('ind-' + i);
    const msg = document.getElementById('msg-' + i);
    const res = data.step_results[String(i)];
    if (!res) {
      ind.className = 'step-indicator ind-idle';
      msg.textContent = '';
      continue;
    }
    ind.className = 'step-indicator ind-' + res.status;
    msg.textContent = res.message || '';
    msg.style.display = '';
  }

  // Extract ZIPs indicator
  const zipInd = document.getElementById('ind-extract-zips');
  const zipMsgEl = document.getElementById('msg-extract-zips');
  const zipRes = data.step_results['extract-zips'];
  if (zipRes) {
    zipInd.className = 'step-indicator ind-' + zipRes.status;
    zipMsgEl.textContent = zipRes.message || '';
  } else if (data.current_step === 'extract-zips' && data.running) {
    zipInd.className = 'step-indicator ind-running';
    zipMsgEl.textContent = 'Extracting...';
  }

  // Sync Zoho indicator
  const syncInd = document.getElementById('ind-sync');
  const syncMsg = document.getElementById('msg-sync');
  if (data.current_step === 'Sync Zoho' && data.running) {
    syncInd.className = 'step-indicator ind-running';
    syncMsg.textContent = 'Syncing...';
  } else if (syncInd.className.includes('ind-running') && !data.running) {
    syncInd.className = 'step-indicator ind-success';
    syncMsg.textContent = 'Done';
  }

  // Summary
  const s = data.summary || {};
  document.getElementById('sumInvoices').textContent = s.invoices != null ? s.invoices : '-';
  document.getElementById('sumBills').textContent = s.bills != null ? s.bills : '-';
  document.getElementById('sumCC').textContent = s.cc_transactions != null ? s.cc_transactions : '-';
}

// Poll every 2 seconds
setInterval(pollStatus, 2000);
pollStatus();

// --- Review Panel ---
let _reviewAccounts = []; // cached accounts list

function openReviewPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'flex';
  document.getElementById('reviewLoading').style.display = 'block';
  document.getElementById('reviewTable').style.display = 'none';

  // Fetch bills + accounts in parallel
  Promise.all([
    fetch('/api/review/bills').then(r => r.json()),
    fetch('/api/review/accounts').then(r => r.json()),
  ]).then(([billsData, accountsData]) => {
    if (billsData.error) {
      document.getElementById('reviewLoading').textContent = billsData.error;
      return;
    }
    _reviewAccounts = accountsData.accounts || [];
    renderReviewTable(billsData.bills || []);
  }).catch(err => {
    document.getElementById('reviewLoading').textContent = 'Failed to load: ' + err;
  });
}

function closeReviewPanel() {
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderReviewTable(bills) {
  const tbody = document.getElementById('reviewTableBody');
  tbody.innerHTML = '';

  if (!bills.length) {
    const el = document.getElementById('reviewLoading');
    el.textContent = 'No bills found. Run Step 3 first.';
    el.style.display = 'block';
    document.getElementById('reviewTable').style.display = 'none';
    return;
  }

  // Group bills by vendor name
  const vendorGroups = {};
  bills.forEach((bill, idx) => {
    const vn = bill.vendor_name || 'Unknown';
    if (!vendorGroups[vn]) vendorGroups[vn] = [];
    vendorGroups[vn].push({...bill, _origIdx: idx});
  });

  // Sort vendor names alphabetically
  const sortedVendors = Object.keys(vendorGroups).sort();

  let globalIdx = 0;
  sortedVendors.forEach(vendorName => {
    const group = vendorGroups[vendorName];

    // --- Vendor group header row with Apply All ---
    const headerTr = document.createElement('tr');
    headerTr.className = 'vendor-group-header';
    const headerTd = document.createElement('td');
    headerTd.colSpan = 6;

    const bulkRow = document.createElement('div');
    bulkRow.className = 'vendor-bulk-row';

    const nameLabel = document.createElement('span');
    nameLabel.className = 'vendor-name-label';
    nameLabel.innerHTML = vendorName + ' <span class="vendor-bill-count">(' + group.length + ' bill' + (group.length > 1 ? 's' : '') + ')</span>';
    bulkRow.appendChild(nameLabel);

    // Only show Apply All controls if vendor has multiple bills
    if (group.length > 1) {
      const bulkSelect = document.createElement('select');
      bulkSelect.className = 'vendor-bulk-select';
      bulkSelect.id = 'bulkSelect-' + vendorName.replace(/[^a-zA-Z0-9]/g, '_');
      const defOpt = document.createElement('option');
      defOpt.value = '';
      defOpt.textContent = '-- select account --';
      bulkSelect.appendChild(defOpt);
      _reviewAccounts.forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.account_id;
        opt.textContent = a.account_name;
        opt.setAttribute('data-name', a.account_name);
        bulkSelect.appendChild(opt);
      });
      bulkRow.appendChild(bulkSelect);

      const applyBtn = document.createElement('button');
      applyBtn.className = 'apply-all-btn';
      applyBtn.textContent = 'Apply All';
      applyBtn.id = 'bulkBtn-' + vendorName.replace(/[^a-zA-Z0-9]/g, '_');
      applyBtn.onclick = function() { bulkSaveVendorAccount(vendorName, group); };
      bulkRow.appendChild(applyBtn);
    }

    headerTd.appendChild(bulkRow);
    headerTr.appendChild(headerTd);
    tbody.appendChild(headerTr);

    // --- Individual bill rows ---
    group.forEach(bill => {
      const idx = globalIdx++;
      const tr = document.createElement('tr');
      tr.setAttribute('data-vendor', vendorName);

      // Vendor (dimmed since header shows it)
      const tdVendor = document.createElement('td');
      tdVendor.textContent = bill.vendor_name;
      tdVendor.style.color = 'var(--text-dim)';
      tdVendor.style.paddingLeft = '20px';
      tr.appendChild(tdVendor);

      // Amount
      const tdAmount = document.createElement('td');
      tdAmount.textContent = bill.amount != null ? Number(bill.amount).toLocaleString() : '-';
      tr.appendChild(tdAmount);

      // Currency
      const tdCurrency = document.createElement('td');
      tdCurrency.textContent = bill.currency || 'INR';
      tr.appendChild(tdCurrency);

      // Current Account
      const tdCurrent = document.createElement('td');
      tdCurrent.textContent = bill.account_name || '-';
      tdCurrent.style.color = 'var(--text-dim)';
      tdCurrent.id = 'currentAcct-' + idx;
      tr.appendChild(tdCurrent);

      // Change To (dropdown)
      const tdChange = document.createElement('td');
      const select = document.createElement('select');
      select.id = 'selectAcct-' + idx;
      const defaultOpt = document.createElement('option');
      defaultOpt.value = '';
      defaultOpt.textContent = '-- keep current --';
      select.appendChild(defaultOpt);
      _reviewAccounts.forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.account_id;
        opt.textContent = a.account_name;
        opt.setAttribute('data-name', a.account_name);
        select.appendChild(opt);
      });
      tdChange.appendChild(select);
      tr.appendChild(tdChange);

      // Action (Save button)
      const tdAction = document.createElement('td');
      const btn = document.createElement('button');
      btn.className = 'review-save-btn';
      btn.textContent = 'Save';
      btn.id = 'saveBtn-' + idx;
      btn.setAttribute('data-bill-id', bill.bill_id);
      btn.onclick = function() { saveAccountChange(bill.bill_id, idx, bill.vendor_name); };
      tdAction.appendChild(btn);
      tr.appendChild(tdAction);

      tbody.appendChild(tr);
    });
  });

  // Store bills globally for bulk operations
  window._reviewBills = bills;
  window._reviewVendorGroups = vendorGroups;

  document.getElementById('reviewLoading').style.display = 'none';
  document.getElementById('reviewTable').style.display = 'table';
}

function saveAccountChange(billId, idx, vendorName) {
  const select = document.getElementById('selectAcct-' + idx);
  const btn = document.getElementById('saveBtn-' + idx);
  const accountId = select.value;
  if (!accountId) {
    addLogLine('[Review] No account selected for row ' + idx);
    return;
  }
  const accountName = select.options[select.selectedIndex].getAttribute('data-name') || '';

  btn.disabled = true;
  btn.textContent = 'Saving...';

  fetch('/api/review/update-account', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_id: billId, account_id: accountId, account_name: accountName, vendor_name: vendorName}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      btn.textContent = 'Saved';
      btn.className = 'review-save-btn saved';
      document.getElementById('currentAcct-' + idx).textContent = accountName;
    } else {
      btn.textContent = 'Error';
      btn.className = 'review-save-btn save-error';
      addLogLine('[Review] Error: ' + (data.error || 'Unknown'));
    }
    btn.disabled = false;
  })
  .catch(err => {
    btn.textContent = 'Error';
    btn.className = 'review-save-btn save-error';
    btn.disabled = false;
    addLogLine('[Review] Request failed: ' + err);
  });
}

function bulkSaveVendorAccount(vendorName, group) {
  const safeVendor = vendorName.replace(/[^a-zA-Z0-9]/g, '_');
  const select = document.getElementById('bulkSelect-' + safeVendor);
  const btn = document.getElementById('bulkBtn-' + safeVendor);
  const accountId = select.value;
  if (!accountId) {
    addLogLine('[Review] No account selected for ' + vendorName);
    return;
  }
  const accountName = select.options[select.selectedIndex].getAttribute('data-name') || '';
  const billIds = group.map(b => b.bill_id);

  btn.disabled = true;
  btn.textContent = 'Applying...';

  fetch('/api/review/bulk-update-account', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_ids: billIds, account_id: accountId, account_name: accountName, vendor_name: vendorName}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      const okCount = (data.succeeded || []).length;
      const failCount = (data.failed || []).length;
      btn.textContent = okCount + '/' + billIds.length + ' Done';
      btn.className = failCount ? 'apply-all-btn save-error' : 'apply-all-btn saved';
      addLogLine('[Review] ' + vendorName + ': ' + okCount + ' bills updated to ' + accountName);

      // Update individual row UI for succeeded bills
      document.querySelectorAll('tr[data-vendor="' + vendorName + '"]').forEach(row => {
        const saveBtn = row.querySelector('.review-save-btn');
        if (!saveBtn) return;
        const billId = saveBtn.getAttribute('data-bill-id');
        if ((data.succeeded || []).includes(billId)) {
          saveBtn.textContent = 'Saved';
          saveBtn.className = 'review-save-btn saved';
          // Update current account label
          const idx = saveBtn.id.replace('saveBtn-', '');
          const currentEl = document.getElementById('currentAcct-' + idx);
          if (currentEl) currentEl.textContent = accountName;
        }
      });
    } else {
      btn.textContent = 'Error';
      btn.className = 'apply-all-btn save-error';
      addLogLine('[Review] Bulk error for ' + vendorName + ': ' + (data.error || 'Unknown'));
    }
    btn.disabled = false;
  })
  .catch(err => {
    btn.textContent = 'Error';
    btn.className = 'apply-all-btn save-error';
    btn.disabled = false;
    addLogLine('[Review] Bulk request failed: ' + err);
  });
}

// --- Create Account Modal ---
function openCreateAccountModal() {
  document.getElementById('newAccountName').value = '';
  document.getElementById('newAccountDesc').value = '';
  document.getElementById('createAccountModal').style.display = 'flex';
}

function closeCreateAccountModal() {
  document.getElementById('createAccountModal').style.display = 'none';
}

function createNewAccount() {
  const name = document.getElementById('newAccountName').value.trim();
  const desc = document.getElementById('newAccountDesc').value.trim();
  if (!name) return;

  const btn = document.getElementById('createAccountBtn');
  btn.disabled = true;
  btn.textContent = 'Creating...';

  fetch('/api/review/create-account', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name, description: desc}),
  })
  .then(r => r.json())
  .then(data => {
    btn.disabled = false;
    btn.textContent = 'Create';
    if (data.ok) {
      // Add to cached accounts list and all dropdowns
      const newAcct = {account_id: data.account_id, account_name: data.account_name};
      _reviewAccounts.push(newAcct);
      _reviewAccounts.sort((a, b) => a.account_name.localeCompare(b.account_name));

      // Update all select dropdowns
      document.querySelectorAll('[id^="selectAcct-"]').forEach(select => {
        const opt = document.createElement('option');
        opt.value = data.account_id;
        opt.textContent = data.account_name;
        opt.setAttribute('data-name', data.account_name);
        // Insert sorted
        let inserted = false;
        for (let i = 1; i < select.options.length; i++) {
          if (select.options[i].textContent > data.account_name) {
            select.insertBefore(opt, select.options[i]);
            inserted = true;
            break;
          }
        }
        if (!inserted) select.appendChild(opt);
      });

      closeCreateAccountModal();
      addLogLine('[Review] Created account: ' + data.account_name);
    } else {
      addLogLine('[Review] Error creating account: ' + (data.error || 'Unknown'));
    }
  })
  .catch(err => {
    btn.disabled = false;
    btn.textContent = 'Create';
    addLogLine('[Review] Request failed: ' + err);
  });
}

// --- CC Upload ---
let _lastUploadedFiles = [];  // track uploaded files for import filtering

function handleCCUpload(input) {
  const files = input.files;
  if (!files || !files.length) return;

  const formData = new FormData();
  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }

  addLogLine('[Upload] Uploading ' + files.length + ' CC statement(s)...');

  fetch('/api/upload/cc', {method: 'POST', body: formData})
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        _lastUploadedFiles = data.files;
        addLogLine('[Upload] Saved: ' + data.files.join(', '));
        runStepWithKwargs('4', {selected_files: data.files});
      } else {
        addLogLine('[Upload] Error: ' + (data.error || 'Unknown'));
      }
    })
    .catch(err => addLogLine('[Upload] Request failed: ' + err));

  input.value = '';
}

// --- Import Picker ---
function clearPaymentsCache() {
  fetch('/api/payments/clear-cache', {method: 'POST'})
    .then(r => r.json())
    .then(d => appendLog('[INFO] ' + d.message))
    .catch(e => appendLog('[ERROR] ' + e));
}

function clearParsedCC() {
  showModal(
    'Clear Parsed CC Data',
    'This will delete cc_transactions.json and all CSV files so Step 4 re-parses fresh on next upload.',
    () => {
      fetch('/api/cc/clear-parsed', {method: 'POST'})
        .then(r => r.json())
        .then(d => appendLog('[INFO] ' + d.message))
        .catch(e => appendLog('[ERROR] ' + e));
    }, true, 'Yes, Clear'
  );
}

function clearImportCache() {
  fetch('/api/banking/clear-cache', {method: 'POST'})
    .then(r => r.json())
    .then(d => appendLog('[INFO] ' + d.message))
    .catch(e => appendLog('[ERROR] ' + e));
}

function openImportPicker() {
  document.getElementById('importPickerModal').style.display = 'flex';
  const body = document.getElementById('importPickerBody');
  body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading...</div>';

  // Get last parsed cards from step 4 result
  fetch('/api/status').then(r => r.json()).then(statusData => {
    const parsed = (statusData.step_results && statusData.step_results['4'] && statusData.step_results['4'].result)
      ? statusData.step_results['4'].result.cards_parsed || []
      : [];

    return fetch('/api/review/available-csvs').then(r => r.json()).then(data => {
      let cards = data.cards || [];

      // Only show cards parsed in last Step 4 run — never show all
      cards = cards.filter(c => parsed.includes(c.card_name));

      if (!cards.length) {
        body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">No parsed CSVs found. Upload & Parse CC statements first (Step 4).</div>';
        return;
      }
      let html = '';
      cards.forEach((c, i) => {
        html += '<label style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;cursor:pointer">'
          + '<input type="checkbox" class="import-card-cb" value="' + c.card_name.replace(/"/g,'&quot;') + '" checked>'
          + '<span>' + c.card_name + '</span>'
          + '<span style="color:var(--text-dim);margin-left:auto;font-size:11px">' + c.rows + ' txns</span>'
          + '</label>';
      });
      body.innerHTML = html;
    });
  }).catch(err => {
    body.innerHTML = '<div style="color:var(--red);font-size:13px;padding:12px 0">Error: ' + err + '</div>';
  });
}

function closeImportPicker() {
  document.getElementById('importPickerModal').style.display = 'none';
}

function importSelectedCards() {
  const checkboxes = document.querySelectorAll('.import-card-cb:checked');
  const selected = Array.from(checkboxes).map(cb => cb.value);
  if (!selected.length) {
    addLogLine('[Import] No cards selected');
    return;
  }
  closeImportPicker();
  addLogLine('[Import] Importing: ' + selected.join(', '));
  runStepWithKwargs('5', {selected_cards: selected});
}

// --- Sync Zoho ---
function syncZoho() {
  addLogLine('[Sync] Starting Zoho sync (bills, vendors & CC accounts)...');
  fetch('/api/zoho/sync', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[Sync] ' + data.error);
      } else {
        addLogLine('[Sync] Sync started...');
        pollStatus();
      }
    })
    .catch(err => addLogLine('[Sync] Request failed: ' + err));
}

// --- Bill Picker with Match Preview ---
let _billPickerData = null;
let _matchPreviewData = null;
let _billFilteredRows = [];
let _billSortCol = 'vendor';
let _billSortAsc = true;
var _billSelectedFiles = new Set();

function _getStatusLabel(inv) {
  if (inv.action === 'skip') return 'In Zoho';
  if (inv.action === 'new_bill') return 'New Bill + Existing Vendor';
  return 'New Bill + New Vendor';
}
function _getStatusKey(inv) {
  if (inv.action === 'skip') return 'skip';
  if (inv.action === 'new_bill') return 'new_bill';
  return 'new_vendor';
}
function _getMatchTypeLabel(inv) {
  var m = inv.vendor_match_method || 'name';
  if (m === 'manual') return 'Manual';
  return m === 'gstin' ? 'GSTIN' : m === 'fuzzy' ? 'Fuzzy' : 'Name';
}
function _getMatchTypeKey(inv) {
  return inv.vendor_match_method || 'name';
}

/* --- Checkbox Dropdown Component --- */
function _buildCheckboxDropdown(id, label, options) {
  var html = '<div class="cb-dropdown" id="cbd_' + id + '">'
    + '<button type="button" class="cb-dropdown-btn" onclick="_toggleCbDropdown(\'' + id + '\')">'
    + label + ' <span class="cb-badge" id="cbd_badge_' + id + '"></span></button>'
    + '<div class="cb-dropdown-panel" id="cbd_panel_' + id + '">'
    + '<div class="cb-dropdown-actions">'
    + '<a onclick="_cbSelectAll(\'' + id + '\')">Select All</a>'
    + '<a onclick="_cbClearAll(\'' + id + '\')">Clear</a></div>'
    + '<div class="cb-dropdown-list">';
  options.forEach(function(o) {
    html += '<label><input type="checkbox" checked value="' + o.value + '" onchange="_onCbChange(\'' + id + '\')">' + o.text + '</label>';
  });
  html += '</div></div></div>';
  return html;
}
function _toggleCbDropdown(id) {
  var panel = document.getElementById('cbd_panel_' + id);
  var isOpen = panel.classList.contains('open');
  document.querySelectorAll('.cb-dropdown-panel.open').forEach(function(p) { p.classList.remove('open'); });
  if (!isOpen) panel.classList.add('open');
}
function _onCbChange(id) {
  _updateCbBadge(id);
  applyBillFilters();
}
function _cbSelectAll(id) {
  document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]').forEach(function(cb) { cb.checked = true; });
  _updateCbBadge(id);
  applyBillFilters();
}
function _cbClearAll(id) {
  document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]').forEach(function(cb) { cb.checked = false; });
  _updateCbBadge(id);
  applyBillFilters();
}
function _updateCbBadge(id) {
  var all = document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]');
  var checked = document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]:checked');
  var badge = document.getElementById('cbd_badge_' + id);
  if (badge) badge.textContent = checked.length < all.length ? checked.length : '';
}
function _getCbValues(id) {
  var vals = [];
  document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]:checked').forEach(function(cb) { vals.push(cb.value); });
  return vals;
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('.cb-dropdown')) {
    document.querySelectorAll('.cb-dropdown-panel.open').forEach(function(p) { p.classList.remove('open'); });
  }
});

/* --- Searchable Zoho Vendor Dropdown --- */
var _zohoVendors = [];
var _selectedZohoVendor = null;

function _loadZohoVendors() {
  return fetch('/api/zoho-vendors').then(function(r) { return r.json(); }).then(function(data) {
    _zohoVendors = data;
  });
}
function _buildSearchDropdown() {
  return '<div class="search-dropdown" id="zohoVendorSearch">'
    + '<input type="text" placeholder="Search Zoho vendor..." onfocus="_openZohoDropdown()" oninput="_filterZohoDropdown()">'
    + '<div class="search-dropdown-list" id="zohoVendorList"></div></div>';
}
function _openZohoDropdown() {
  _filterZohoDropdown();
  document.getElementById('zohoVendorList').classList.add('open');
}
function _currencyBadge(code) {
  code = code || 'INR';
  var color = code === 'INR' ? '#a3a3a3' : code === 'USD' ? '#60a5fa' : '#c084fc';
  return ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:rgba(255,255,255,0.08);color:'+color+';font-weight:600">'+code+'</span>';
}
function _filterZohoDropdown() {
  var input = document.querySelector('#zohoVendorSearch input');
  var q = (input ? input.value : '').toLowerCase();
  var list = document.getElementById('zohoVendorList');
  var items = '';
  var count = 0;
  for (var i = 0; i < _zohoVendors.length && count < 50; i++) {
    var v = _zohoVendors[i];
    if (q && v.contact_name.toLowerCase().indexOf(q) < 0) continue;
    var sel = _selectedZohoVendor && _selectedZohoVendor.contact_id === v.contact_id ? ' selected' : '';
    items += '<div class="sd-item' + sel + '" onclick="_selectZohoVendor(this)" data-id="' + v.contact_id + '" data-name="' + v.contact_name.replace(/"/g, '&quot;') + '" data-currency="' + (v.currency_code||'INR') + '">' + v.contact_name + _currencyBadge(v.currency_code) + '</div>';
    count++;
  }
  if (!items) items = '<div class="sd-item" style="color:var(--text-dim)">No matches</div>';
  // Note: vendor names come from local zoho_vendors_cache.json, not untrusted input
  list.innerHTML = items;
}
function _selectZohoVendor(el) {
  _selectedZohoVendor = { contact_id: el.getAttribute('data-id'), contact_name: el.getAttribute('data-name') };
  var input = document.querySelector('#zohoVendorSearch input');
  if (input) input.value = _selectedZohoVendor.contact_name;
  document.getElementById('zohoVendorList').classList.remove('open');
}
/* --- Per-row vendor edit --- */
var _activeRowVendorDropdown = null;
function _openRowVendorEdit(fileKey, event) {
  event.stopPropagation();
  _closeRowVendorDropdown();
  var td = event.target.closest('.col-zoho-vendor');
  if (!td) return;
  var dd = document.createElement('div');
  dd.className = 'row-vendor-dropdown open';
  dd.setAttribute('data-file', fileKey);
  dd.innerHTML = '<input type="text" placeholder="Search vendor..." oninput="_filterRowVendorDropdown(this)" autofocus>'
    + '<div class="rvd-list"></div>';
  td.appendChild(dd);
  _activeRowVendorDropdown = dd;
  var input = dd.querySelector('input');
  input.focus();
  _filterRowVendorDropdown(input);
}
function _filterRowVendorDropdown(input) {
  var q = (input ? input.value : '').toLowerCase();
  var list = input.closest('.row-vendor-dropdown').querySelector('.rvd-list');
  var fileKey = input.closest('.row-vendor-dropdown').getAttribute('data-file');
  var items = '';
  var count = 0;
  for (var i = 0; i < _zohoVendors.length && count < 50; i++) {
    var v = _zohoVendors[i];
    if (q && v.contact_name.toLowerCase().indexOf(q) < 0) continue;
    items += '<div class="sd-item" onclick="_selectRowVendor(this,\'' + fileKey.replace(/'/g, "\\'") + '\')" data-id="' + v.contact_id + '" data-name="' + v.contact_name.replace(/"/g, '&quot;') + '" data-currency="' + (v.currency_code||'INR') + '">' + v.contact_name + _currencyBadge(v.currency_code) + '</div>';
    count++;
  }
  if (!items) items = '<div class="sd-item" style="color:var(--text-dim)">No matches</div>';
  list.innerHTML = items;
}
function _selectRowVendor(el, fileKey) {
  var vendorId = el.getAttribute('data-id');
  var vendorName = el.getAttribute('data-name');
  // Find the invoice in preview data
  var inv = null;
  _matchPreviewData.preview.forEach(function(item) {
    if (item.file === fileKey) inv = item;
  });
  if (!inv) return;
  var vname = inv.vendor_name || inv.file;
  var overrides = {};
  overrides[vname] = { contact_id: vendorId, contact_name: vendorName };
  fetch('/api/vendor-overrides', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ overrides: overrides })
  }).then(function(r) { return r.json(); }).then(function() {
    Object.assign(_vendorOverrides, overrides);
    if (inv.action === 'new_vendor') inv.action = 'new_bill';
    inv.matched_vendor_id = vendorId;
    inv.matched_vendor_name = vendorName;
    inv.vendor_match_method = 'manual';
    _closeRowVendorDropdown();
    _sortFilteredRows();
    _renderTableRows();
    // Update summary
    var s = { total: 0, skip: 0, new_bill: 0, new_vendor_bill: 0 };
    _matchPreviewData.preview.forEach(function(item) {
      s.total++;
      if (item.action === 'skip') s.skip++;
      else if (item.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
    var totalNew = s.new_bill + s.new_vendor_bill;
    var summary = document.getElementById('billPickerSummary');
    if (summary) _renderSummaryPanel(summary, s, totalNew);
    showToast('Vendor changed to ' + vendorName, 'success');
  });
}
function _closeRowVendorDropdown() {
  if (_activeRowVendorDropdown) {
    _activeRowVendorDropdown.remove();
    _activeRowVendorDropdown = null;
  }
}

document.addEventListener('click', function(e) {
  if (!e.target.closest('.search-dropdown')) {
    var list = document.getElementById('zohoVendorList');
    if (list) list.classList.remove('open');
  }
  if (!e.target.closest('.row-vendor-dropdown') && !e.target.closest('.zoho-vendor-edit-btn')) {
    _closeRowVendorDropdown();
  }
});

/* --- Vendor Overrides --- */
var _vendorOverrides = {};

function _loadVendorOverrides() {
  return fetch('/api/vendor-overrides').then(function(r) { return r.json(); }).then(function(data) {
    _vendorOverrides = data;
  });
}
function _applyOverridesToPreview() {
  if (!_matchPreviewData || !_matchPreviewData.preview) return;
  _matchPreviewData.preview.forEach(function(inv) {
    var vname = inv.vendor_name || '';
    if (_vendorOverrides[vname] && inv.action === 'new_vendor_bill') {
      inv.action = 'new_bill';
      inv.matched_vendor_id = _vendorOverrides[vname].contact_id;
      inv.matched_vendor_name = _vendorOverrides[vname].contact_name;
      inv.vendor_match_method = 'manual';
    }
  });
}
function applyZohoVendorMapping() {
  if (!_selectedZohoVendor) { showToast('Select a Zoho vendor first', 'warning'); return; }
  // Collect target rows: selected (checked) rows, or all filtered non-skip rows
  var targetRows = [];
  var useSelected = false;
  _matchPreviewData.preview.forEach(function(inv) {
    if (_billSelectedFiles.has(inv.file) && inv.action !== 'skip') targetRows.push(inv);
  });
  if (targetRows.length > 0) {
    useSelected = true;
  } else {
    // Fall back to all filtered (visible) non-skip rows
    _billFilteredRows.forEach(function(inv) {
      if (inv.action !== 'skip') targetRows.push(inv);
    });
  }
  if (!targetRows.length) { showToast('No rows to map — filter or select invoices first', 'warning'); return; }
  var label = useSelected ? 'selected' : 'filtered';
  var msg = 'Change vendor for ' + targetRows.length + ' ' + label + ' invoice(s) to "' + _selectedZohoVendor.contact_name + '"?\n\nThis will override any existing vendor match.';
  if (!confirm(msg)) return;
  var overrides = {};
  targetRows.forEach(function(inv) {
    var vname = inv.vendor_name || inv.file;
    overrides[vname] = { contact_id: _selectedZohoVendor.contact_id, contact_name: _selectedZohoVendor.contact_name };
  });
  fetch('/api/vendor-overrides', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ overrides: overrides })
  }).then(function(r) { return r.json(); }).then(function() {
    Object.assign(_vendorOverrides, overrides);
    targetRows.forEach(function(inv) {
      if (inv.action === 'new_vendor') inv.action = 'new_bill';
      inv.matched_vendor_id = _selectedZohoVendor.contact_id;
      inv.matched_vendor_name = _selectedZohoVendor.contact_name;
      inv.vendor_match_method = 'manual';
    });
    _sortFilteredRows();
    _renderTableRows();
    _updateSelectionUI();
    var s = { total: 0, skip: 0, new_bill: 0, new_vendor_bill: 0 };
    _matchPreviewData.preview.forEach(function(inv) {
      s.total++;
      if (inv.action === 'skip') s.skip++;
      else if (inv.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
    var totalNew = s.new_bill + s.new_vendor_bill;
    var summary = document.getElementById('billPickerSummary');
    if (summary) _renderSummaryPanel(summary, s, totalNew);
    showToast('Mapped ' + targetRows.length + ' ' + label + ' invoice(s) to ' + _selectedZohoVendor.contact_name, 'success');
  });
}

function _renderSummaryPanel(summary, s, totalNew) {
  // Note: summary stats are computed from local match-preview data, not untrusted input
  summary.innerHTML = ''
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--text-dim)"></span> Total <span class="count" id="bpTotal">' + s.total + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--green)"></span> In Zoho <span class="count" id="bpSkip">' + s.skip + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--accent)"></span> Existing Vendor <span class="count" id="bpNewBill">' + s.new_bill + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--yellow)"></span> New Vendor <span class="count" id="bpNewVendor">' + s.new_vendor_bill + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--accent)"></span> Upload <span class="count" id="bpWillUpload">' + totalNew + '</span></div>'
    + '<div class="bill-summary-stat" id="bpSelectedCount" style="font-weight:600;color:var(--accent)">Selected: 0</div>'
    + '<div class="bill-summary-actions">'
    + '<button class="modal-btn modal-btn-confirm" id="createSelectedBillsBtn" onclick="createSelectedBills()" disabled>Create Selected (0)</button>'
    + '<button class="modal-btn modal-btn-cancel" onclick="closeBillPicker()">Cancel</button>'
    + '</div>';
}

function _buildFilterBar(preview) {
  var months = [];
  var vendors = [];
  var seen = {};
  preview.forEach(function(p) {
    var m = p.organized_month || 'Unknown';
    if (!seen['m_'+m]) { months.push(m); seen['m_'+m] = 1; }
    var v = p.vendor_name || 'Unknown';
    if (!seen['v_'+v]) { vendors.push(v); seen['v_'+v] = 1; }
  });
  months.sort();
  vendors.sort(function(a,b){ return a.localeCompare(b); });

  var html = '<div class="bill-filter-bar">';
  html += '<div class="bill-filter-group"><label>From</label><select id="bfFrom"><option value="">All</option>';
  months.forEach(function(m){ html += '<option value="'+m+'">'+m+'</option>'; });
  html += '</select></div>';
  html += '<div class="bill-filter-group"><label>To</label><select id="bfTo"><option value="">All</option>';
  months.forEach(function(m){ html += '<option value="'+m+'">'+m+'</option>'; });
  html += '</select></div>';
  var vendorOpts = vendors.map(function(v){ return {value: v, text: v}; });
  html += '<div class="bill-filter-group"><label>Vendor</label>' + _buildCheckboxDropdown('vendor', 'Vendor', vendorOpts) + '</div>';
  html += '<div class="bill-filter-group"><label>Min Amt</label><input type="number" id="bfMinAmt" placeholder="0" step="any"></div>';
  html += '<div class="bill-filter-group"><label>Max Amt</label><input type="number" id="bfMaxAmt" placeholder="any" step="any"></div>';
  var statusOpts = [{value:'skip',text:'In Zoho'},{value:'new_bill',text:'New Bill + Existing Vendor'},{value:'new_vendor',text:'New Bill + New Vendor'}];
  html += '<div class="bill-filter-group"><label>Status</label>' + _buildCheckboxDropdown('status', 'Status', statusOpts) + '</div>';
  var matchOpts = [{value:'gstin',text:'GSTIN'},{value:'name',text:'Name'},{value:'fuzzy',text:'Fuzzy'},{value:'manual',text:'Manual'}];
  html += '<div class="bill-filter-group"><label>Match Type</label>' + _buildCheckboxDropdown('matchtype', 'Match Type', matchOpts) + '</div>';
  html += '<button class="bill-filter-clear" onclick="clearBillFilters()">Clear</button>';
  html += '</div>';
  return html;
}

function _buildTable() {
  var cols = [
    {key:'check', label:'<input type="checkbox" id="bpSelectAll" onchange="toggleBillSelectAll(this)" style="cursor:pointer;accent-color:var(--accent)">', sort:false, cls:'col-checkbox'},
    {key:'vendor', label:'Vendor', sort:true},
    {key:'date', label:'Date', sort:true},
    {key:'amount', label:'Amount', sort:true, cls:'col-amount'},
    {key:'status', label:'Status', sort:true},
    {key:'match', label:'Match', sort:true},
    {key:'zoho_vendor', label:'Zoho Vendor', sort:true, cls:'col-zoho-vendor'},
    {key:'action', label:'', sort:false, cls:'col-action'}
  ];
  var html = '<div class="bill-table-wrap"><table class="bill-table"><thead><tr>';
  cols.forEach(function(c) {
    var cls = (c.cls ? ' class="'+c.cls+'"' : '');
    if (c.sort) {
      var sorted = (_billSortCol === c.key) ? ' sorted' : '';
      var arrow = (_billSortCol === c.key) ? (_billSortAsc ? '\u25B2' : '\u25BC') : '\u25B2';
      html += '<th'+cls+' class="'+(c.cls||'')+sorted+'" onclick="sortBillTable(\''+c.key+'\')" style="cursor:pointer">' + c.label + ' <span class="sort-arrow">'+arrow+'</span></th>';
    } else {
      html += '<th'+cls+'>' + c.label + '</th>';
    }
  });
  html += '</tr></thead><tbody id="bpTbody"></tbody></table></div>';
  return html;
}

function _renderTableRows() {
  var tbody = document.getElementById('bpTbody');
  if (!tbody) return;
  var html = '';
  _billFilteredRows.forEach(function(inv) {
    var isSkip = inv.action === 'skip';
    var rowCls = isSkip ? ' class="row-skip"' : '';
    var amt = inv.amount ? Number(inv.amount).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '0.00';
    var fileEsc = (inv.file || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
    var vendorEsc = (inv.vendor_name || 'Unknown').replace(/"/g, '&quot;');

    // Checkbox
    var cb = '';
    if (!isSkip) {
      var checked = _billSelectedFiles.has(inv.file) ? ' checked' : '';
      cb = '<input type="checkbox" onchange="onBillCheckChange(this)" data-file="'+fileEsc+'" style="cursor:pointer;accent-color:var(--accent)"'+checked+'>';
    }

    // Status badge
    var statusBadge = '';
    if (isSkip) {
      statusBadge = '<span class="bill-status-badge created">In Zoho</span>';
    } else if (inv.action === 'new_bill') {
      var vmMethod = inv.vendor_match_method || 'name';
      var vmColor = vmMethod === 'gstin' ? 'var(--green)' : 'var(--accent)';
      var vmBg = vmMethod === 'gstin' ? 'rgba(34,197,94,0.15)' : 'rgba(108,140,255,0.15)';
      statusBadge = '<span class="bill-status-badge" style="background:'+vmBg+';color:'+vmColor+'">New Bill</span>';
    } else {
      statusBadge = '<span class="bill-status-badge" style="background:rgba(250,204,21,0.15);color:var(--yellow)">New Vendor</span>';
    }

    // Match type badge (only for new_bill)
    var matchBadge = '';
    if (inv.action === 'new_bill') {
      matchBadge = '<span class="bill-status-badge" style="background:rgba(108,140,255,0.1);color:var(--text-dim)">' + _getMatchTypeLabel(inv) + '</span>';
    }

    // Action button
    var actionBtn = '';
    if (!isSkip) {
      actionBtn = '<button class="bill-create-btn" onclick="createOneBillConfirm(\''+fileEsc+'\',\''+vendorEsc+'\',\''+amt+'\')">Create</button>';
    }

    // Zoho vendor column
    var zohoVendor = '';
    if (isSkip) {
      zohoVendor = inv.matched_vendor_name || (inv.matched_bill ? inv.matched_bill.vendor_name || '' : '');
    } else if (inv.action === 'new_bill') {
      zohoVendor = inv.matched_vendor_name || '';
    }
    var zohoVendorEsc = zohoVendor.replace(/"/g,'&quot;');
    var zohoVendorCell = '';
    if (isSkip) {
      zohoVendorCell = '<span title="'+zohoVendorEsc+'">'+zohoVendor+'</span>';
    } else {
      var editFileKey = inv.file.replace(/'/g, "\\'").replace(/"/g,'&quot;');
      zohoVendorCell = '<div class="zoho-vendor-display">'
        + '<span class="vendor-text" title="'+zohoVendorEsc+'">'+(zohoVendor || '<span style="color:var(--text-dim);font-style:italic">—</span>')+'</span>'
        + '<button class="zoho-vendor-edit-btn" onclick="_openRowVendorEdit(\''+editFileKey+'\',event)" title="Change vendor">&#9998;</button>'
        + '</div>';
    }

    html += '<tr'+rowCls+'>'
      + '<td class="col-checkbox">'+cb+'</td>'
      + '<td class="vendor-cell" title="'+vendorEsc+'">'+vendorEsc+'</td>'
      + '<td>'+(inv.date || '')+'</td>'
      + '<td class="col-amount">'+amt+' '+(inv.currency || 'INR')+'</td>'
      + '<td>'+statusBadge+'</td>'
      + '<td>'+matchBadge+'</td>'
      + '<td class="col-zoho-vendor">'+zohoVendorCell+'</td>'
      + '<td class="col-action">'+actionBtn+'</td>'
      + '</tr>';
  });
  tbody.innerHTML = html;
  _updateSelectAllCheckbox();
}

function applyBillFilters() {
  if (!_matchPreviewData) return;
  var preview = _matchPreviewData.preview || [];
  var fromVal = document.getElementById('bfFrom') ? document.getElementById('bfFrom').value : '';
  var toVal = document.getElementById('bfTo') ? document.getElementById('bfTo').value : '';
  var vendors = _getCbValues('vendor');
  var minAmt = document.getElementById('bfMinAmt') ? parseFloat(document.getElementById('bfMinAmt').value) : NaN;
  var maxAmt = document.getElementById('bfMaxAmt') ? parseFloat(document.getElementById('bfMaxAmt').value) : NaN;
  var statuses = _getCbValues('status');
  var matchTypes = _getCbValues('matchtype');

  // Build sorted month list for range filtering
  var allMonths = [];
  var monthSet = {};
  preview.forEach(function(p) {
    var m = p.organized_month || 'Unknown';
    if (!monthSet[m]) { allMonths.push(m); monthSet[m] = 1; }
  });
  allMonths.sort();

  var fromIdx = fromVal ? allMonths.indexOf(fromVal) : 0;
  var toIdx = toVal ? allMonths.indexOf(toVal) : allMonths.length - 1;
  if (fromIdx < 0) fromIdx = 0;
  if (toIdx < 0) toIdx = allMonths.length - 1;
  var validMonths = {};
  for (var i = fromIdx; i <= toIdx; i++) validMonths[allMonths[i]] = 1;

  _billFilteredRows = preview.filter(function(inv) {
    var m = inv.organized_month || 'Unknown';
    if (fromVal || toVal) { if (!validMonths[m]) return false; }
    if (vendors.length && vendors.indexOf(inv.vendor_name || 'Unknown') < 0) return false;
    var amt = parseFloat(inv.amount) || 0;
    if (!isNaN(minAmt) && amt < minAmt) return false;
    if (!isNaN(maxAmt) && amt > maxAmt) return false;
    if (statuses.length && statuses.indexOf(_getStatusKey(inv)) < 0) return false;
    if (matchTypes.length) {
      if (inv.action !== 'new_bill') return false;
      if (matchTypes.indexOf(_getMatchTypeKey(inv)) < 0) return false;
    }
    return true;
  });
  _sortFilteredRows();
  _renderTableRows();
  _updateSelectionUI();
  _updateFilteredSummary();
}

function _sortFilteredRows() {
  var col = _billSortCol;
  var asc = _billSortAsc ? 1 : -1;
  _billFilteredRows.sort(function(a, b) {
    var va, vb;
    if (col === 'vendor') { va = (a.vendor_name||'').toLowerCase(); vb = (b.vendor_name||'').toLowerCase(); }
    else if (col === 'date') { va = a.date || ''; vb = b.date || ''; }
    else if (col === 'amount') { va = parseFloat(a.amount)||0; vb = parseFloat(b.amount)||0; }
    else if (col === 'status') { va = _getStatusKey(a); vb = _getStatusKey(b); }
    else if (col === 'match') { va = a.action==='new_bill' ? _getMatchTypeKey(a) : 'zzz'; vb = b.action==='new_bill' ? _getMatchTypeKey(b) : 'zzz'; }
    else if (col === 'zoho_vendor') { va = (a.matched_vendor_name||'').toLowerCase(); vb = (b.matched_vendor_name||'').toLowerCase(); }
    else { va = ''; vb = ''; }
    if (va < vb) return -1 * asc;
    if (va > vb) return 1 * asc;
    return 0;
  });
}

function sortBillTable(col) {
  if (_billSortCol === col) { _billSortAsc = !_billSortAsc; }
  else { _billSortCol = col; _billSortAsc = true; }
  // Update header arrows
  document.querySelectorAll('.bill-table th').forEach(function(th) { th.classList.remove('sorted'); });
  var idx = {vendor:1,date:2,amount:3,status:4,match:5,zoho_vendor:6}[col];
  if (idx !== undefined) {
    var ths = document.querySelectorAll('.bill-table th');
    if (ths[idx]) {
      ths[idx].classList.add('sorted');
      var arrow = ths[idx].querySelector('.sort-arrow');
      if (arrow) arrow.textContent = _billSortAsc ? '\u25B2' : '\u25BC';
    }
  }
  _sortFilteredRows();
  _renderTableRows();
}

function clearBillFilters() {
  ['bfFrom','bfTo','bfMinAmt','bfMaxAmt'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
  ['vendor','status','matchtype'].forEach(function(id) { _cbSelectAll(id); });
  applyBillFilters();
}

function onBillCheckChange(cb) {
  var file = cb.getAttribute('data-file');
  if (cb.checked) { _billSelectedFiles.add(file); } else { _billSelectedFiles.delete(file); }
  _updateSelectAllCheckbox();
  _updateSelectionUI();
}

function toggleBillSelectAll(cb) {
  var checkboxes = document.querySelectorAll('#bpTbody input[type="checkbox"]');
  checkboxes.forEach(function(c) {
    c.checked = cb.checked;
    var file = c.getAttribute('data-file');
    if (cb.checked) { _billSelectedFiles.add(file); } else { _billSelectedFiles.delete(file); }
  });
  _updateSelectionUI();
}

function _updateSelectAllCheckbox() {
  var sa = document.getElementById('bpSelectAll');
  if (!sa) return;
  var checkboxes = document.querySelectorAll('#bpTbody input[type="checkbox"]');
  if (!checkboxes.length) { sa.checked = false; sa.indeterminate = false; return; }
  var total = checkboxes.length;
  var checked = Array.from(checkboxes).filter(function(c){return c.checked}).length;
  sa.checked = checked === total;
  sa.indeterminate = checked > 0 && checked < total;
}

function _updateSelectionUI() {
  var n = _billSelectedFiles.size;
  var countEl = document.getElementById('bpSelectedCount');
  if (countEl) countEl.textContent = 'Selected: ' + n;
  var btn = document.getElementById('createSelectedBillsBtn');
  if (btn) {
    btn.textContent = 'Create Selected (' + n + ')';
    btn.disabled = n === 0;
  }
}

function _updateFilteredSummary() {
  var skip = 0, newBill = 0, newVendor = 0;
  _billFilteredRows.forEach(function(inv) {
    if (inv.action === 'skip') skip++;
    else if (inv.action === 'new_bill') newBill++;
    else newVendor++;
  });
  var total = _billFilteredRows.length;
  var willUpload = newBill + newVendor;
  var el;
  el = document.getElementById('bpTotal'); if (el) el.textContent = total;
  el = document.getElementById('bpSkip'); if (el) el.textContent = skip;
  el = document.getElementById('bpNewBill'); if (el) el.textContent = newBill;
  el = document.getElementById('bpNewVendor'); if (el) el.textContent = newVendor;
  el = document.getElementById('bpWillUpload'); if (el) el.textContent = willUpload;
}

function createSelectedBills() {
  if (!_billSelectedFiles.size) return;
  var n = _billSelectedFiles.size;
  var files = Array.from(_billSelectedFiles);
  var overridesDict = {};
  if (_matchPreviewData && _matchPreviewData.preview) {
    _matchPreviewData.preview.forEach(function(inv) {
      if (_billSelectedFiles.has(inv.file) && inv.matched_vendor_id) {
        overridesDict[inv.file] = { contact_id: inv.matched_vendor_id, contact_name: inv.matched_vendor_name || '' };
      }
    });
  }
  showModal(
    'Create ' + n + ' Bills?',
    n + ' bills will be created in Zoho Books.',
    function() {
      closeBillPicker();
      addLogLine('[Bills] Creating ' + n + ' selected bills...');
      runStepWithKwargs('3', {selected_files: files, vendor_overrides: overridesDict});
    },
    false,
    'Create ' + n + ' Bills'
  );
}

function createOneBillConfirm(filename, vendorName, amount) {
  showModal(
    'Create Bill?',
    'Create bill for <b>' + vendorName + '</b> (' + amount + ')?',
    function() {
      closeBillPicker();
      addLogLine('[Bills] Creating bill for: ' + filename);
      runStepWithKwargs('3', {selected_files: [filename]});
    },
    false,
    'Create Bill'
  );
}

function openBillPicker() {
  _billSelectedFiles.clear();
  _billSortCol = 'vendor';
  _billSortAsc = true;
  _selectedZohoVendor = null;
  document.getElementById('billPickerModal').style.display = 'flex';
  var body = document.getElementById('billPickerBody');
  var summary = document.getElementById('billPickerSummary');
  body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading match preview...</div>';
  summary.innerHTML = '';

  Promise.all([
    fetch('/api/bills/match-preview', {method: 'POST'}).then(function(r){return r.json()}),
    _loadVendorOverrides(),
    _loadZohoVendors()
  ]).then(function(results) {
    var data = results[0];
    if (data.error) {
      addLogLine('[Bills] ' + data.error + ' — falling back to basic list');
      _loadBasicBillPicker(body, summary);
      return;
    }
    _matchPreviewData = data;
    _applyOverridesToPreview();

    // Recount after overrides
    var s = { total: 0, skip: 0, new_bill: 0, new_vendor_bill: 0 };
    (data.preview || []).forEach(function(inv) {
      s.total++;
      if (inv.action === 'skip') s.skip++;
      else if (inv.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
    var totalNew = s.new_bill + s.new_vendor_bill;
    _renderSummaryPanel(summary, s, totalNew);

    var preview = data.preview || [];
    if (!preview.length) {
      body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">No invoices found. Run Extract Data first.</div>';
      return;
    }

    // Build: filter bar + mapping bar + table
    var mappingBar = '<div class="bill-mapping-bar">'
      + '<label>Bulk change Zoho Vendor:</label>'
      + _buildSearchDropdown()
      + '<button class="modal-btn modal-btn-confirm" onclick="applyZohoVendorMapping()">Apply</button>'
      + '</div>';
    body.innerHTML = _buildFilterBar(preview) + mappingBar + _buildTable();

    // Attach filter listeners for From/To and amount inputs
    ['bfFrom','bfTo','bfMinAmt','bfMaxAmt'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('change', applyBillFilters);
    });

    _billFilteredRows = preview.slice();
    _sortFilteredRows();
    _renderTableRows();
    _updateSelectionUI();
  }).catch(function(err) {
    addLogLine('[Bills] Match preview failed: ' + err + ' — falling back');
    _loadBasicBillPicker(body, summary);
  });
}

function _loadBasicBillPicker(body, summary) {
  // Fallback: flat table without match preview (no Zoho sync cache)
  fetch('/api/invoices/list').then(function(r){return r.json()}).then(function(data) {
    if (data.error) {
      body.innerHTML = '<div style="color:var(--red);font-size:13px;padding:12px 0">' + data.error + '</div>';
      return;
    }
    _billPickerData = data;
    _matchPreviewData = null;
    var s = data.summary || {};
    var totalPending = s.pending || 0;

    summary.innerHTML = ''
      + '<div class="bill-summary-total"><span>Total Invoices</span><span class="count">' + (s.total || 0) + '</span></div>'
      + '<div class="bill-summary-card"><span class="label"><span class="dot" style="background:var(--green)"></span> Created</span><span class="count" style="color:var(--green)">' + (s.created || 0) + '</span></div>'
      + '<div class="bill-summary-card"><span class="label"><span class="dot" style="background:var(--yellow)"></span> Pending</span><span class="count" style="color:var(--yellow)">' + totalPending + '</span></div>'
      + '<div class="bill-summary-divider"></div>'
      + '<div style="font-size:11px;color:var(--text-dim);padding:4px 0">Sync Zoho first for smart dedup</div>'
      + '<div class="bill-summary-upload-section">'
      + '<button class="modal-btn modal-btn-cancel" onclick="closeBillPicker()" style="width:100%">Cancel</button>'
      + '</div>';

    var months = data.months || [];
    if (!months.length) {
      body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">No invoices found. Run Step 2 (Extract Data) first.</div>';
      return;
    }

    var html = '<div class="bill-table-wrap"><table class="bill-table"><thead><tr>'
      + '<th>Vendor</th><th>Month</th><th class="col-amount">Amount</th><th>Status</th><th class="col-action"></th>'
      + '</tr></thead><tbody>';
    months.forEach(function(group) {
      group.invoices.forEach(function(inv) {
        var isCreated = inv.status === 'created';
        var amt = inv.amount ? Number(inv.amount).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '0.00';
        var vendorEsc = (inv.vendor_name || 'Unknown').replace(/"/g, '&quot;');
        var rowCls = isCreated ? ' class="row-skip"' : '';
        html += '<tr'+rowCls+'>'
          + '<td class="vendor-cell" title="'+vendorEsc+'">'+vendorEsc+'</td>'
          + '<td>'+group.month+'</td>'
          + '<td class="col-amount">'+amt+' '+(inv.currency || 'INR')+'</td>'
          + '<td><span class="bill-status-badge '+inv.status+'">'+inv.status+'</span></td>'
          + '<td class="col-action">';
        if (!isCreated) {
          html += '<button class="bill-create-btn" onclick="createOneBill(\''+inv.file.replace(/'/g, "\\'")+'\')">Create</button>';
        }
        html += '</td></tr>';
      });
    });
    html += '</tbody></table></div>';
    body.innerHTML = html;
  }).catch(function(err) {
    body.innerHTML = '<div style="color:var(--red);font-size:13px;padding:12px 0">Error: ' + err + '</div>';
  });
}

function closeBillPicker() {
  document.getElementById('billPickerModal').style.display = 'none';
}

function createOneBill(filename) {
  closeBillPicker();
  addLogLine('[Bills] Creating bill for: ' + filename);
  runStepWithKwargs('3', {selected_files: [filename]});
}

function runStepWithKwargs(step, kwargs) {
  fetch('/api/run/' + step, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({run_kwargs: kwargs}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      addLogLine('[UI] ' + data.error);
    }
    pollStatus();
  })
  .catch(err => addLogLine('[UI] Request failed: ' + err));
}

// --- Match Status Panel ---
let _matchData = null;

function openMatchPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'flex';
  document.getElementById('matchLoading').style.display = 'block';
  document.getElementById('matchContent').style.display = 'none';

  fetch('/api/match-status')
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('matchLoading').textContent = data.error;
        return;
      }
      _matchData = data;
      renderMatchPanel(data);
    })
    .catch(err => {
      document.getElementById('matchLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeMatchPanel() {
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderMatchPanel(data) {
  document.getElementById('matchedCount').textContent = data.summary.matched_count;
  document.getElementById('unmatchedCount').textContent = data.summary.unmatched_count;
  document.getElementById('totalCount').textContent = data.summary.total_count;
  document.getElementById('matchLoading').style.display = 'none';
  document.getElementById('matchContent').style.display = 'block';
  switchMatchTab('matched');
}

function switchMatchTab(tab) {
  document.querySelectorAll('.match-tab').forEach(t => t.classList.remove('active'));
  document.querySelector('.tab-' + tab).classList.add('active');

  const tbody = document.getElementById('matchTableBody');
  const thead = document.getElementById('matchTableHead');
  tbody.innerHTML = '';

  const items = tab === 'matched' ? _matchData.matched : _matchData.unmatched;

  if (tab === 'unmatched') {
    thead.innerHTML = '<th>Vendor</th><th>Invoice File</th><th>Bill Amount</th>'
      + '<th>Currency</th><th>CC Card</th><th>CC INR Amount</th><th>Status</th><th>Reason</th>';
  } else {
    thead.innerHTML = '<th>Vendor</th><th>Invoice File</th><th>Bill Amount</th>'
      + '<th>Currency</th><th>CC Card</th><th>CC INR Amount</th><th>Status</th>';
  }

  if (!items || !items.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = tab === 'unmatched' ? 8 : 7;
    td.style.textAlign = 'center';
    td.style.color = 'var(--text-dim)';
    td.style.padding = '30px';
    td.textContent = tab === 'matched' ? 'No matched payments yet' : 'All bills matched!';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  items.forEach(item => {
    const tr = document.createElement('tr');
    const addTd = (text, opts) => {
      const td = document.createElement('td');
      td.textContent = text || '-';
      if (opts) Object.assign(td.style, opts);
      tr.appendChild(td);
      return td;
    };

    addTd(item.vendor_name);
    const fileTd = addTd(item.file, {fontSize:'11px',maxWidth:'200px',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'});
    fileTd.title = item.file || '';
    addTd(item.amount != null ? Number(item.amount).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-');
    addTd(item.currency);
    addTd(item.cc_card);
    addTd(item.cc_inr_amount != null ? Number(item.cc_inr_amount).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-');

    const statusTd = addTd(item.status);
    statusTd.className = item.status === 'paid' ? 'status-paid' : 'status-unmatched';

    if (tab === 'unmatched') {
      const reasonTd = addTd(item.reason);
      reasonTd.className = 'reason-cell';
      reasonTd.title = item.reason || '';
    }

    tbody.appendChild(tr);
  });
}

// --- Check Bills & CC Panel ---
function openCheckPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'flex';
  document.getElementById('checkLoading').style.display = 'block';
  document.getElementById('checkContent').style.display = 'none';

  fetch('/api/check-cc-match')
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('checkLoading').textContent = data.error;
        return;
      }
      renderCheckPanel(data);
    })
    .catch(err => {
      document.getElementById('checkLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeCheckPanel() {
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

// --- Payment Preview Panel ---
var _paymentPreviewData = null;

function openPaymentPreview() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'flex';
  document.getElementById('paymentLoading').style.display = 'block';
  document.getElementById('paymentContent').style.display = 'none';
  document.getElementById('recordSelectedBtn').style.display = 'none';

  fetch('/api/payments/preview')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('paymentLoading').textContent = data.error;
        return;
      }
      _paymentPreviewData = data;
      renderPaymentPreview(data);
    })
    .catch(function(err) {
      document.getElementById('paymentLoading').textContent = 'Failed: ' + err;
    });
}

function closePaymentPanel() {
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function filterPaymentsByCard(cardName) {
  if (!_paymentPreviewData) return;
  var matches = _paymentPreviewData.matches || [];
  var content = document.getElementById('paymentContent');
  // Data rows have data-status attribute; separator rows have pay-section-sep class
  var dataRows = content.querySelectorAll('tbody tr[data-status]');

  var visibleMatched = 0, visibleUnmatched = 0, visiblePaid = 0, visibleCcOnly = 0;

  dataRows.forEach(function(row, i) {
    var m = matches[i];
    if (!m) return;
    var rowCard = row.getAttribute('data-card') || '';
    var status = row.getAttribute('data-status') || '';
    var show = !cardName || rowCard === cardName;
    // For unmatched bills (no card on CC side), show only when "All Cards"
    if (cardName && status === 'unmatched' && !rowCard) show = false;

    row.style.display = show ? '' : 'none';
    if (show) {
      if (status === 'matched') visibleMatched++;
      else if (status === 'unmatched') visibleUnmatched++;
      else if (status === 'already_paid') visiblePaid++;
      else if (status === 'cc_only') visibleCcOnly++;
    }
  });

  // Show/hide section separators based on whether any rows in that section are visible
  var seps = {matched: visibleMatched, cc_only: visibleCcOnly, unmatched: visibleUnmatched, other: visiblePaid};
  ['matched','cc_only','unmatched','other'].forEach(function(sec) {
    var sep = content.querySelector('.pay-sep-' + sec);
    if (sep) sep.style.display = seps[sec] > 0 ? '' : 'none';
  });

  // Update summary text with filtered counts
  var billCount = visibleMatched + visibleUnmatched + visiblePaid;
  document.getElementById('paymentSummaryText').textContent =
    billCount + ' bills \u00B7 ' + visibleMatched + ' matched \u00B7 ' + visibleUnmatched + ' no CC \u00B7 ' + visiblePaid + ' already paid \u00B7 ' + visibleCcOnly + ' no invoice';

  // Reset checkboxes on filter change
  _paySelectedBills.clear();
  var selAll = document.getElementById('paySelectAll');
  if (selAll) selAll.checked = false;
  document.querySelectorAll('.pay-cb').forEach(function(c) { c.checked = false; });
  _updatePaySelectedBtn();
}

function renderPaymentPreview(data) {
  var matches = data.matches || [];
  var s = data.summary || {};
  var fmt = function(n) { return n != null ? Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // Populate card filter dropdown with uncategorized counts
  var cardFilter = document.getElementById('paymentCardFilter');
  var ccTotal = data.card_cc_total || {};
  var ccUnmatched = data.card_cc_unmatched || {};
  var totalUncatCount = (data.unmatched_cc || []).length;
  cardFilter.innerHTML = '<option value="">All Cards (' + totalUncatCount + ' uncategorized)</option>';
  var cardNames = data.card_names || [];
  if (cardNames.length > 0) {
    cardNames.forEach(function(name) {
      var opt = document.createElement('option');
      opt.value = name;
      var total = ccTotal[name] || 0;
      var uncat = ccUnmatched[name] || 0;
      opt.textContent = name + ' (' + uncat + '/' + total + ' uncategorized)';
      cardFilter.appendChild(opt);
    });
    cardFilter.style.display = 'inline-block';
  } else {
    cardFilter.style.display = 'none';
  }

  document.getElementById('paymentSummaryText').textContent =
    s.total_bills + ' bills \u00B7 ' + s.matched + ' matched \u00B7 ' + s.unmatched + ' no CC \u00B7 ' + (s.already_paid || 0) + ' already paid \u00B7 ' + totalUncatCount + ' no invoice';

  document.getElementById('paymentLoading').style.display = 'none';
  var content = document.getElementById('paymentContent');
  content.style.display = 'block';
  content.innerHTML = '';

  // Merge unmatched CC transactions into matches array for unified display
  var unmatchedCc = data.unmatched_cc || [];
  unmatchedCc.forEach(function(cc) {
    matches.push({
      status: 'cc_only',
      cc_transaction_id: cc.transaction_id || '',
      cc_description: cc.description || '',
      cc_inr_amount: cc.amount || 0,
      cc_date: cc.date || '',
      cc_card: cc.card_name || '',
      cc_forex_amount: cc.forex_amount || null,
      cc_forex_currency: cc.forex_currency || null,
    });
  });

  if (!matches.length) {
    content.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:40px">No unpaid bills or CC transactions found</div>';
    return;
  }

  // Sort: matched first (by confidence desc), then no CC, then no invoice, then already_paid
  var order = {matched: 0, unmatched: 1, cc_only: 2, already_paid: 3};
  matches.sort(function(a, b) {
    var oa = order.hasOwnProperty(a.status) ? order[a.status] : 9;
    var ob = order.hasOwnProperty(b.status) ? order[b.status] : 9;
    if (oa !== ob) return oa - ob;
    // Within matched, sort by confidence descending
    if (a.status === 'matched' && b.status === 'matched') {
      var ca = (a.confidence && a.confidence.overall) || 0;
      var cb = (b.confidence && b.confidence.overall) || 0;
      return cb - ca;
    }
    return 0;
  });

  // Reset selected checkboxes
  _paySelectedBills.clear();
  document.getElementById('recordSelectedBtn').style.display = 'none';

  // Build table — CC columns LEFT, Bill columns RIGHT
  var tbl = document.createElement('table');
  tbl.className = 'match-table';
  tbl.style.cssText = 'width:100%;font-size:11px';
  tbl.innerHTML = '<thead><tr>'
    + '<th style="padding:6px 4px;text-align:center;width:28px"><input type="checkbox" id="paySelectAll" onchange="togglePaySelectAll(this)" title="Select all matched"></th>'
    + '<th style="text-align:left;padding:6px 8px">CC Description</th>'
    + '<th style="text-align:right;padding:6px 8px">CC INR</th>'
    + '<th style="padding:6px 8px">CC Date</th>'
    + '<th style="padding:6px 8px">Card</th>'
    + '<th style="text-align:left;padding:6px 8px;border-left:2px solid var(--border)">Vendor / Invoice</th>'
    + '<th style="text-align:right;padding:6px 8px">Bill Amt</th>'
    + '<th style="padding:6px 4px">Cur</th>'
    + '<th style="padding:6px 8px">Bill Date</th>'
    + '<th style="padding:6px 8px;text-align:center">Confidence</th>'
    + '<th style="padding:6px 8px">Action</th>'
    + '</tr></thead>';

  var tbody = document.createElement('tbody');
  var _lastSection = '';

  function _confDot(val) {
    var col = val >= 90 ? 'var(--green)' : val >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
    return '<span style="color:' + col + ';font-weight:700" title="' + val + '%">' + val + '</span>';
  }

  matches.forEach(function(m, idx) {
    // Add section separator rows
    var section = m.status === 'matched' ? 'matched' : (m.status === 'cc_only' ? 'cc_only' : (m.status === 'unmatched' ? 'unmatched' : 'other'));
    if (section !== _lastSection) {
      _lastSection = section;
      var sepTr = document.createElement('tr');
      sepTr.className = 'pay-section-sep pay-sep-' + section;
      var sepLabel = '', sepColor = '', sepBg = '';
      if (section === 'matched') {
        var mCount = matches.filter(function(x){return x.status==='matched'}).length;
        sepLabel = '\u2714 Matched (' + mCount + ')';
        sepColor = 'var(--green)'; sepBg = 'rgba(80,200,120,0.08)';
      } else if (section === 'cc_only') {
        var ccCount = matches.filter(function(x){return x.status==='cc_only'}).length;
        sepLabel = '\u26A0 No Invoice \u2014 CC Only (' + ccCount + ')';
        sepColor = 'var(--accent)'; sepBg = 'rgba(100,150,255,0.06)';
      } else if (section === 'unmatched') {
        var umCount = matches.filter(function(x){return x.status==='unmatched'}).length;
        sepLabel = '\u26A0 No CC Match \u2014 Bills Only (' + umCount + ')';
        sepColor = 'var(--yellow)'; sepBg = 'rgba(255,200,50,0.06)';
      } else {
        var apCount = matches.filter(function(x){return x.status==='already_paid'}).length;
        sepLabel = '\u2713 Already Paid (' + apCount + ')';
        sepColor = 'var(--text-dim)'; sepBg = 'rgba(150,150,150,0.06)';
      }
      sepTr.innerHTML = '<td colspan="11" style="padding:7px 10px;font-size:11px;font-weight:700;color:' + sepColor + ';border-top:2px solid var(--border);background:' + sepBg + '">' + sepLabel + '</td>';
      tbody.appendChild(sepTr);
    }

    var tr = document.createElement('tr');
    tr.id = 'pay-row-' + (m.bill_id || 'cc-' + idx);
    tr.setAttribute('data-card', m.cc_card || '');
    tr.setAttribute('data-status', m.status || '');

    var bgColor = 'transparent';
    if (m.status === 'matched') bgColor = 'rgba(80,200,120,0.04)';
    else if (m.status === 'cc_only') bgColor = 'rgba(100,150,255,0.04)';
    else if (m.status === 'unmatched') bgColor = 'rgba(255,200,50,0.04)';
    else if (m.status === 'already_paid') bgColor = 'rgba(150,150,150,0.04)';
    tr.style.background = bgColor;

    var confCell = '';
    var actionBtn = '';
    if (m.status === 'matched') {
      actionBtn = '<button class="bill-create-btn" id="pay-btn-' + m.bill_id + '" onclick="confirmRecordOne(\'' + m.bill_id + '\')">Record</button>';
      // Confidence breakdown: V=vendor, A=amount, D=date
      var c = m.confidence || {};
      var ov = c.overall || 0;
      var ovColor = ov >= 85 ? 'var(--green)' : ov >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      confCell = '<div style="text-align:center;line-height:1.4">'
        + '<div style="font-size:13px;font-weight:700;color:' + ovColor + '">' + ov + '%</div>'
        + '<div style="font-size:9px;color:var(--text-dim)">Vendor:' + _confDot(c.vendor||0) + ' Amt:' + _confDot(c.amount||0) + ' Date:' + _confDot(c.date||0) + '</div>'
        + '</div>';
    } else if (m.status === 'cc_only') {
      confCell = '<span style="color:var(--accent);font-size:10px">No Invoice</span>';
    } else if (m.status === 'unmatched') {
      confCell = '<span style="color:var(--yellow);font-size:10px">No CC</span>';
    } else if (m.status === 'already_paid') {
      confCell = '<span style="color:var(--text-dim);font-size:10px">\u2713 Paid</span>';
    }

    // CC columns (left side) — empty for unmatched/already_paid bills
    var hasCc = m.status === 'matched' || m.status === 'cc_only';
    var ccDesc = hasCc ? (m.cc_description || '-') : '';
    var ccDescFull = ccDesc;
    if (ccDesc.length > 40) ccDesc = ccDesc.substring(0, 40) + '\u2026';
    var forexNote = '';
    if (hasCc && m.cc_forex_amount) forexNote = ' (' + m.cc_forex_currency + ' ' + fmt(m.cc_forex_amount) + ')';
    var dimStyle = 'color:var(--text-dim);';

    // Bill columns (right side) — empty for cc_only
    var hasBill = m.status !== 'cc_only';

    // Checkbox cell — only for matched rows
    var cbCell = '';
    if (m.status === 'matched') {
      cbCell = '<td style="text-align:center;padding:5px 4px"><input type="checkbox" class="pay-cb" data-billid="' + m.bill_id + '" onchange="togglePayCheckbox(this)"></td>';
    } else {
      cbCell = '<td style="padding:5px 4px"></td>';
    }

    tr.innerHTML = cbCell
      // --- CC LEFT ---
      + '<td style="text-align:left;padding:5px 8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' + (hasCc?'':''+dimStyle) + '" title="' + ccDescFull.replace(/"/g,'&quot;') + '">' + (hasCc ? ccDesc + forexNote : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="text-align:right;padding:5px 8px;font-family:monospace;' + (hasCc?'':''+dimStyle) + '">' + (hasCc && m.cc_inr_amount ? fmt(m.cc_inr_amount) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px;' + (hasCc?'':''+dimStyle) + '">' + (hasCc ? (m.cc_date||'-') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px;font-size:10px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (hasCc ? (m.cc_card||'-') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      // --- BILL RIGHT ---
      + '<td style="text-align:left;padding:5px 8px;border-left:2px solid var(--border);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (m.vendor_name||'').replace(/"/g,'&quot;') + '">' + (hasBill ? (m.vendor_name||'-') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="text-align:right;padding:5px 8px;font-family:monospace">' + (hasBill ? fmt(m.bill_amount) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 4px;text-align:center">' + (hasBill ? (m.bill_currency||'INR') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px">' + (hasBill ? (m.bill_date||'-') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px">' + confCell + '</td>'
      + '<td style="padding:5px 8px">' + actionBtn + '</td>';

    tbody.appendChild(tr);
  });

  tbl.appendChild(tbody);
  content.appendChild(tbl);
}

function confirmRecordOne(billId) {
  var m = _paymentPreviewData && _paymentPreviewData.matches
    ? _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId && x.status === 'matched'; })
    : null;
  var desc = m ? (m.vendor_name || '') + ' — ' + (m.bill_currency||'INR') + ' ' + Number(m.bill_amount).toLocaleString() + ' via ' + (m.cc_card||'CC') : billId;
  showModal('Record Payment?', 'This will mark the bill as PAID in Zoho Books: ' + desc, function() {
    recordOnePayment(billId, _paymentPreviewData);
  }, false, 'Record');
}

function recordOnePayment(billId, previewData) {
  var btn = document.getElementById('pay-btn-' + billId);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  // Find the matched CC info from preview data
  var payload = {bill_id: billId};
  if (previewData && previewData.matches) {
    var m = previewData.matches.find(function(x) { return x.bill_id === billId && x.status === 'matched'; });
    if (m) {
      payload.cc_transaction_id = m.cc_transaction_id;
      payload.cc_description = m.cc_description;
      payload.cc_inr_amount = m.cc_inr_amount;
      payload.cc_date = m.cc_date;
      payload.cc_card = m.cc_card;
      if (m.cc_forex_amount) {
        payload.cc_forex_amount = m.cc_forex_amount;
        payload.cc_forex_currency = m.cc_forex_currency;
      }
    }
  }

  fetch('/api/payments/record-one', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var row = document.getElementById('pay-row-' + billId);
    if (data.status === 'paid') {
      if (row) row.style.background = 'rgba(80,200,120,0.15)';
      if (btn) { btn.textContent = '\u2713 Paid'; btn.style.color = 'var(--green)'; }
      addLogLine('[Payment] Recorded: ' + billId);
    } else if (data.status === 'already_paid') {
      if (btn) { btn.textContent = 'Already Paid'; btn.style.color = 'var(--text-dim)'; }
    } else {
      if (btn) { btn.textContent = 'Failed'; btn.disabled = false; btn.style.color = 'var(--yellow)'; }
      addLogLine('[Payment] No match for ' + billId);
    }
  })
  .catch(function(err) {
    if (btn) { btn.textContent = 'Error'; btn.disabled = false; }
    addLogLine('[Payment] Error: ' + err);
  });
}

// --- Payment checkbox selection ---
var _paySelectedBills = new Set();

function togglePayCheckbox(cb) {
  var billId = cb.getAttribute('data-billid');
  if (cb.checked) _paySelectedBills.add(billId);
  else _paySelectedBills.delete(billId);
  _updatePaySelectedBtn();
}

function togglePaySelectAll(cb) {
  var checkboxes = document.querySelectorAll('.pay-cb');
  checkboxes.forEach(function(c) {
    // Only toggle visible (not filtered out) rows
    var row = c.closest('tr');
    if (row && row.style.display === 'none') return;
    c.checked = cb.checked;
    var billId = c.getAttribute('data-billid');
    if (cb.checked) _paySelectedBills.add(billId);
    else _paySelectedBills.delete(billId);
  });
  _updatePaySelectedBtn();
}

function _updatePaySelectedBtn() {
  var btn = document.getElementById('recordSelectedBtn');
  var count = _paySelectedBills.size;
  if (count > 0) {
    btn.style.display = 'inline-block';
    btn.textContent = 'Record Selected (' + count + ')';
    btn.disabled = false;
  } else {
    btn.style.display = 'none';
  }
}

function confirmRecordSelected() {
  var count = _paySelectedBills.size;
  if (!count) return;
  showModal('Record ' + count + ' Selected Payments?', 'This will mark ' + count + ' selected bills as PAID in Zoho Books.', function() {
    recordSelectedPayments();
  }, true, 'Record Selected');
}

function recordSelectedPayments() {
  if (!_paymentPreviewData || !_paySelectedBills.size) return;
  var selectedItems = _paymentPreviewData.matches
    .filter(function(m) { return m.status === 'matched' && _paySelectedBills.has(m.bill_id); })
    .map(function(m) {
      var item = {bill_id: m.bill_id, cc_transaction_id: m.cc_transaction_id, cc_inr_amount: m.cc_inr_amount, cc_date: m.cc_date, cc_card: m.cc_card};
      if (m.cc_forex_amount) { item.cc_forex_amount = m.cc_forex_amount; item.cc_forex_currency = m.cc_forex_currency; }
      return item;
    });

  if (!selectedItems.length) return;

  var selBtn = document.getElementById('recordSelectedBtn');
  selBtn.disabled = true;
  selBtn.textContent = 'Recording ' + selectedItems.length + '...';

  selectedItems.forEach(function(item) {
    var btn = document.getElementById('pay-btn-' + item.bill_id);
    if (btn) { btn.disabled = true; btn.textContent = '...'; }
  });

  fetch('/api/payments/record-selected', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({items: selectedItems}),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var results = data.results || [];
    var paidCount = 0;
    results.forEach(function(r) {
      var row = document.getElementById('pay-row-' + r.bill_id);
      var btn = document.getElementById('pay-btn-' + r.bill_id);
      var cb = document.querySelector('.pay-cb[data-billid="' + r.bill_id + '"]');
      if (r.status === 'paid') {
        if (row) row.style.background = 'rgba(80,200,120,0.15)';
        if (btn) { btn.textContent = '\u2713 Paid'; btn.style.color = 'var(--green)'; }
        if (cb) { cb.checked = false; cb.disabled = true; }
        _paySelectedBills.delete(r.bill_id);
        paidCount++;
      } else {
        if (btn) { btn.textContent = r.status; btn.disabled = false; }
      }
    });
    selBtn.textContent = paidCount + '/' + selectedItems.length + ' Recorded';
    _updatePaySelectedBtn();
    addLogLine('[Payment] Selected record: ' + paidCount + '/' + selectedItems.length + ' paid');
  })
  .catch(function(err) {
    selBtn.textContent = 'Error';
    selBtn.disabled = false;
    addLogLine('[Payment] Selected error: ' + err);
  });
}

function renderCheckPanel(data) {
  const grouped = data.grouped || [];
  const summary = data.summary || {};
  const fmt = n => n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-';

  document.getElementById('checkSummaryText').textContent =
    (summary.vendors_count || 0) + ' vendors · ' + (summary.bills_count || 0) + ' bills · ' + (summary.cc_transactions_count || 0) + ' CC matched · ' + (summary.unmatched_cc_count || 0) + ' CC unmatched';

  const content = document.getElementById('checkContent');
  content.innerHTML = '';

  if (!grouped.length) {
    content.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:40px">No data found</div>';
  } else {
    // Sticky column headers
    const colHeader = document.createElement('div');
    colHeader.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;position:sticky;top:0;z-index:2;background:var(--bg-panel);border-bottom:2px solid var(--border);flex-shrink:0';
    colHeader.innerHTML =
      '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--accent)">Bills</div>' +
      '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--yellow);border-left:1px solid var(--border)">CC Transactions</div>';
    content.appendChild(colHeader);

    grouped.forEach(function(group) {
      const billsLen = group.bills.length;
      const ccLen = group.cc_transactions.length;
      const hasMatch = billsLen > 0 && ccLen > 0;

      // Vendor header row
      const vendorHeader = document.createElement('div');
      vendorHeader.style.cssText = 'padding:5px 12px;font-size:12px;font-weight:600;background:rgba(255,255,255,0.04);display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);border-top:1px solid var(--border)';
      vendorHeader.innerHTML =
        '<span style="color:' + (hasMatch ? '#4ade80' : 'var(--text-dim)') + ';font-size:8px">●</span>' +
        '<span>' + group.vendor + '</span>' +
        '<span style="font-size:10px;color:var(--text-dim);font-weight:400">' + billsLen + ' bill' + (billsLen!==1?'s':'') + ' · ' + ccLen + ' CC txn' + (ccLen!==1?'s':'') + '</span>' +
        (!hasMatch ? '<span style="font-size:10px;color:#f59e0b;margin-left:auto">⚠ no match</span>' : '');

      // Two-column body
      const cols = document.createElement('div');
      cols.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--border)';

      // Bills column
      const billsCol = document.createElement('div');
      billsCol.style.cssText = 'border-right:1px solid var(--border);min-width:0';
      if (!billsLen) {
        billsCol.innerHTML = '<div style="padding:6px 12px;font-size:11px;color:var(--text-dim)">— no bills</div>';
      } else {
        const tbl = document.createElement('table');
        tbl.className = 'match-table';
        tbl.style.width = '100%';
        const thead = '<thead><tr><th>Amount</th><th>Cur</th><th>Date</th></tr></thead>';
        const tbody = document.createElement('tbody');
        group.bills.forEach(function(b) {
          const tr = document.createElement('tr');
          tr.innerHTML = '<td style="font-family:monospace;font-size:11px">' + fmt(b.amount) + '</td><td>' + (b.currency||'INR') + '</td><td>' + (b.date||'-') + '</td>';
          tbody.appendChild(tr);
        });
        tbl.innerHTML = thead;
        tbl.appendChild(tbody);
        billsCol.appendChild(tbl);
      }

      // CC column
      const ccCol = document.createElement('div');
      ccCol.style.cssText = 'min-width:0';
      if (!ccLen) {
        ccCol.innerHTML = '<div style="padding:6px 12px;font-size:11px;color:var(--text-dim)">— no CC match</div>';
      } else {
        const tbl = document.createElement('table');
        tbl.className = 'match-table';
        tbl.style.width = '100%';
        const thead = '<thead><tr><th>Description</th><th>INR Amt</th><th>Date</th><th>Card</th></tr></thead>';
        const tbody = document.createElement('tbody');
        group.cc_transactions.forEach(function(t) {
          const tr = document.createElement('tr');
          const cardShort = (t.card_name||'-').replace('CC ','');
          tr.innerHTML =
            '<td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px" title="' + t.description + '">' + (t.description||'-') + '</td>' +
            '<td style="font-family:monospace;font-size:11px">' + fmt(t.amount) + '</td>' +
            '<td>' + (t.date||'-') + '</td>' +
            '<td style="font-size:10px;color:var(--text-dim)">' + cardShort + '</td>';
          tbody.appendChild(tr);
        });
        tbl.innerHTML = thead;
        tbl.appendChild(tbody);
        ccCol.appendChild(tbl);
      }

      cols.appendChild(billsCol);
      cols.appendChild(ccCol);

      const section = document.createElement('div');
      section.appendChild(vendorHeader);
      section.appendChild(cols);
      content.appendChild(section);
    });
  }

  document.getElementById('checkLoading').style.display = 'none';
  document.getElementById('checkContent').style.display = 'block';
}

// --- Monthly Compare Panel ---
var _compareData = null;

function openComparePanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'flex';
  document.getElementById('compareLoading').style.display = 'block';
  document.getElementById('compareContent').style.display = 'none';

  fetch('/api/compare/monthly')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('compareLoading').textContent = data.error;
        return;
      }
      _compareData = data;
      renderComparePanel(data);
    })
    .catch(function(err) {
      document.getElementById('compareLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeComparePanel() {
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function parseAllForCompare(step, btnId, label) {
  var btn = document.getElementById(btnId);
  btn.disabled = true;
  btn.textContent = 'Parsing ' + label + '...';
  fetch('/api/run/' + step, { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      btn.textContent = 'Done! Refreshing...';
      // Refresh compare data
      return fetch('/api/compare/monthly').then(function(r) { return r.json(); });
    })
    .then(function(data) {
      if (data && !data.error) {
        _compareData = data;
        renderComparePanel(data);
      }
      btn.disabled = false;
      btn.textContent = label === 'CC' ? 'Parse All CC' : 'Parse All Invoices';
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = label === 'CC' ? 'Parse All CC' : 'Parse All Invoices';
      alert('Parse failed: ' + err);
    });
}

function parseOrgInvoices() {
  var btn = document.getElementById('parseAllBillsBtn');
  btn.disabled = true;
  btn.textContent = 'Parsing Invoices...';
  fetch('/api/compare/parse-org-invoices', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); btn.disabled = false; btn.textContent = 'Parse All Invoices'; return; }
      // Poll until done
      var poll = setInterval(function() {
        fetch('/api/status').then(function(r) { return r.json(); }).then(function(st) {
          if (!st.running) {
            clearInterval(poll);
            btn.textContent = 'Done! Refreshing...';
            fetch('/api/compare/monthly').then(function(r) { return r.json(); }).then(function(d) {
              if (d && !d.error) { _compareData = d; renderComparePanel(d); }
              btn.disabled = false;
              btn.textContent = 'Parse All Invoices';
            });
          }
        });
      }, 2000);
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = 'Parse All Invoices';
      alert('Parse failed: ' + err);
    });
}

function renderComparePanel(data) {
  var months = data.months || [];
  var summary = data.summary || {};

  document.getElementById('compareSummaryText').textContent =
    (summary.total_months || 0) + ' months · ' +
    (summary.total_cc || 0) + ' CC txns · ' +
    (summary.total_invoices || 0) + ' invoices';

  // Populate month dropdown
  var select = document.getElementById('compareMonthSelect');
  select.innerHTML = '';
  // "All Months" option
  var allOpt = document.createElement('option');
  allOpt.value = 'all';
  var totalCC = months.reduce(function(s, m) { return s + (m.cc_count || 0); }, 0);
  var totalInv = months.reduce(function(s, m) { return s + (m.inv_count || 0); }, 0);
  allOpt.textContent = 'All Months (' + totalCC + ' CC / ' + totalInv + ' inv)';
  select.appendChild(allOpt);
  months.forEach(function(m, idx) {
    var opt = document.createElement('option');
    opt.value = idx;
    opt.textContent = m.month + ' (' + m.cc_count + ' CC / ' + m.inv_count + ' inv)';
    select.appendChild(opt);
  });

  document.getElementById('compareLoading').style.display = 'none';
  document.getElementById('compareContent').style.display = 'block';

  if (months.length > 0) {
    renderCompareMonth('all');
  } else {
    document.getElementById('compareContent').innerHTML =
      '<div style="text-align:center;color:var(--text-dim);padding:40px">No data found</div>';
  }
}

function renderCompareMonth(idx) {
  var m;
  if (idx === 'all' || idx === 'NaN') {
    // Merge all months
    var allCC = [], allInv = [];
    var ccTot = 0, invTot = 0;
    _compareData.months.forEach(function(mo) {
      allCC = allCC.concat(mo.cc_transactions || []);
      allInv = allInv.concat(mo.invoices || []);
      ccTot += (mo.cc_total || 0);
      invTot += (mo.inv_total || 0);
    });
    m = {
      month: 'All Months',
      cc_transactions: allCC, invoices: allInv,
      cc_count: allCC.length, inv_count: allInv.length,
      cc_total: ccTot, inv_total: invTot
    };
  } else {
    idx = parseInt(idx);
    m = _compareData.months[idx];
  }
  var content = document.getElementById('compareContent');
  content.innerHTML = '';
  var fmt = function(n) { return n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };
  var fmtDate = function(d) { if (!d || d.length < 10) return d || '-'; var p = d.split('-'); return p[2] + '-' + p[1] + '-' + p[0]; };

  // Month summary bar
  var summaryBar = document.createElement('div');
  summaryBar.style.cssText = 'display:flex;gap:24px;padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);flex-shrink:0;background:rgba(255,255,255,0.03)';
  summaryBar.innerHTML =
    '<span><strong>' + m.cc_count + '</strong> CC transactions &middot; Total: <strong style="font-family:monospace">' + fmt(m.cc_total) + '</strong> INR</span>' +
    '<span style="margin-left:auto"><strong>' + m.inv_count + '</strong> invoices &middot; Total: <strong style="font-family:monospace">' + fmt(m.inv_total) + '</strong></span>';
  content.appendChild(summaryBar);

  // Sticky two-column header
  var colHeader = document.createElement('div');
  colHeader.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;position:sticky;top:0;z-index:2;background:var(--bg-panel);border-bottom:2px solid var(--border);flex-shrink:0';
  colHeader.innerHTML =
    '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--yellow)">CC Statements (' + m.cc_count + ')</div>' +
    '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--accent);border-left:1px solid var(--border)">Invoices (' + m.inv_count + ')</div>';
  content.appendChild(colHeader);

  // Two-column body
  var cols = document.createElement('div');
  cols.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;flex:1;min-height:0';

  // --- CC column ---
  var ccCol = document.createElement('div');
  ccCol.style.cssText = 'border-right:1px solid var(--border);min-width:0;overflow-y:auto';
  if (!m.cc_transactions.length) {
    ccCol.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-dim)">No CC transactions this month</div>';
  } else {
    var tbl = document.createElement('table');
    tbl.className = 'match-table';
    tbl.style.width = '100%';
    tbl.innerHTML = '<thead><tr><th>Date</th><th>Description</th><th>Amount (INR)</th><th>Forex</th><th>Card</th></tr></thead>';
    var tbody = document.createElement('tbody');
    m.cc_transactions.forEach(function(t) {
      var tr = document.createElement('tr');
      tr.setAttribute('data-vendor', (t.vendor_name || t.description || '').toLowerCase());
      tr.setAttribute('data-date', t.date || '');
      var forexText = t.forex_currency && t.forex_amount
        ? t.forex_currency + ' ' + fmt(t.forex_amount) : '-';
      var cardShort = (t.card_name || '-').replace('CC ', '');
      tr.innerHTML =
        '<td style="white-space:nowrap;font-size:11px">' + fmtDate(t.date) + '</td>' +
        '<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px" title="' + (t.description || '') + '">' + (t.description || '-') + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + fmt(t.amount) + '</td>' +
        '<td style="font-size:10px;color:var(--yellow)">' + forexText + '</td>' +
        '<td style="font-size:10px;color:var(--text-dim)">' + cardShort + '</td>';
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    ccCol.appendChild(tbl);
  }

  // --- Invoice column ---
  var invCol = document.createElement('div');
  invCol.style.cssText = 'min-width:0;overflow-y:auto';
  if (!m.invoices.length) {
    invCol.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-dim)">No invoices this month</div>';
  } else {
    var tbl2 = document.createElement('table');
    tbl2.className = 'match-table';
    tbl2.style.width = '100%';
    tbl2.innerHTML = '<thead><tr><th>Vendor</th><th>GSTIN</th><th>Amount</th><th>Date</th><th>Invoice #</th></tr></thead>';
    var tbody2 = document.createElement('tbody');
    m.invoices.forEach(function(inv) {
      var tr = document.createElement('tr');
      tr.setAttribute('data-vendor', (inv.vendor_name || '').toLowerCase());
      tr.setAttribute('data-date', inv.date || '');
      var gstinHtml = inv.vendor_gstin
        ? '<span title="' + inv.vendor_gstin + '">' + inv.vendor_gstin + '</span>'
        : '<span style="color:var(--text-dim)">-</span>';
      tr.innerHTML =
        '<td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="' + (inv.vendor_name || '') + '">' + (inv.vendor_name || '-') + '</td>' +
        '<td style="font-size:10px;font-family:monospace">' + gstinHtml + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + fmt(inv.amount) + ' <span style="font-size:9px;color:var(--text-dim)">' + (inv.currency || 'INR') + '</span></td>' +
        '<td style="white-space:nowrap;font-size:11px">' + fmtDate(inv.date) + '</td>' +
        '<td style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (inv.invoice_number || '') + '">' + (inv.invoice_number || '-') + '</td>';
      tbody2.appendChild(tr);
    });
    tbl2.appendChild(tbody2);
    invCol.appendChild(tbl2);
  }

  cols.appendChild(ccCol);
  cols.appendChild(invCol);
  content.appendChild(cols);

  // --- Categorize Check button ---
  var catBar = document.createElement('div');
  catBar.style.cssText = 'padding:10px 12px;border-top:1px solid var(--border);text-align:center;flex-shrink:0';

  var catBtn = document.createElement('button');
  catBtn.textContent = 'Categorize Check';
  catBtn.style.cssText = 'background:transparent;color:var(--accent);border:1px dashed var(--accent);padding:6px 18px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600';
  catBtn.onclick = function() { runCategorizeCheck(idx, 'cc'); };
  catBar.appendChild(catBtn);

  content.appendChild(catBar);

  // Container for categorize results
  var catResults = document.createElement('div');
  catResults.id = 'categorizeResults';
  content.appendChild(catResults);

  // Populate vendor filter dropdown from Zoho vendors
  var zohoVendors = (_compareData && _compareData.zoho_vendors) || [];
  var vSelect = document.getElementById('compareVendorFilter');
  vSelect.innerHTML = '<option value="">All Vendors</option>';
  zohoVendors.forEach(function(v) {
    var opt = document.createElement('option');
    opt.value = v.toLowerCase();
    opt.textContent = v.length > 30 ? v.substring(0, 28) + '...' : v;
    opt.title = v;
    vSelect.appendChild(opt);
  });

  // Set date range defaults from month data
  var dateFrom = document.getElementById('compareDateFrom');
  var dateTo = document.getElementById('compareDateTo');
  dateFrom.value = '';
  dateTo.value = '';

  // Show filter bar
  document.getElementById('compareFilterBar').style.display = 'flex';
}

function _vendorMatch(rowVendor, filterVal) {
  // Bidirectional: "github" matches "github, inc." and vice versa
  return rowVendor.indexOf(filterVal) !== -1 || filterVal.indexOf(rowVendor) !== -1;
}

function applyCompareFilters() {
  var vendorVal = (document.getElementById('compareVendorFilter').value || '').toLowerCase();
  var dateFrom = document.getElementById('compareDateFrom').value || '';
  var dateTo = document.getElementById('compareDateTo').value || '';

  function _filterRows(tbody) {
    if (!tbody) return;
    Array.prototype.forEach.call(tbody.rows, function(tr) {
      var rv = tr.getAttribute('data-vendor') || '';
      var rd = tr.getAttribute('data-date') || '';
      var show = true;
      if (vendorVal && rv && !_vendorMatch(rv, vendorVal)) show = false;
      if (dateFrom && rd && rd < dateFrom) show = false;
      if (dateTo && rd && rd > dateTo) show = false;
      tr.style.display = show ? '' : 'none';
    });
  }

  var tables = document.querySelectorAll('#compareContent table.match-table');
  if (tables.length >= 1) _filterRows(tables[0].querySelector('tbody'));
  if (tables.length >= 2) _filterRows(tables[1].querySelector('tbody'));

  // Also filter Categorize Check rows (vendor + date + status)
  var catStatusVal = '';
  var catStatusEl = document.getElementById('catStatusFilter');
  if (catStatusEl) catStatusVal = catStatusEl.value || '';
  var catRows = document.querySelectorAll('#categorizeResults .cat-row');
  Array.prototype.forEach.call(catRows, function(tr) {
    var rv = tr.getAttribute('data-vendor') || '';
    var rd = tr.getAttribute('data-date') || '';
    var rs = tr.getAttribute('data-status') || '';
    var show = true;
    if (vendorVal && rv && !_vendorMatch(rv, vendorVal)) show = false;
    if (dateFrom && rd && rd < dateFrom) show = false;
    if (dateTo && rd && rd > dateTo) show = false;
    if (catStatusVal && rs !== catStatusVal) show = false;
    tr.style.display = show ? '' : 'none';
  });
}

function applyCatStatusFilter() {
  applyCompareFilters();
}

function clearCompareFilters() {
  document.getElementById('compareVendorFilter').value = '';
  document.getElementById('compareDateFrom').value = '';
  document.getElementById('compareDateTo').value = '';
  var catStatusEl = document.getElementById('catStatusFilter');
  if (catStatusEl) catStatusEl.value = '';
  applyCompareFilters();
}

var _catRows = [];
var _catMonth = '';

function runCategorizeCheck(idx, mode) {
  var m;
  if (idx === 'all' || isNaN(parseInt(idx))) {
    var allCC = [], allInv = [];
    _compareData.months.forEach(function(mo) {
      allCC = allCC.concat(mo.cc_transactions || []);
      allInv = allInv.concat(mo.invoices || []);
    });
    m = { month: 'All Months', cc_transactions: allCC, invoices: allInv };
  } else {
    idx = parseInt(idx);
    m = _compareData.months[idx];
  }
  var ccList = (m.cc_transactions || []).slice();
  var invList = (m.invoices || []).slice();

  // Apply active vendor/date filters
  var vendorVal = (document.getElementById('compareVendorFilter').value || '').toLowerCase();
  var dateFrom = document.getElementById('compareDateFrom').value || '';
  var dateTo = document.getElementById('compareDateTo').value || '';
  if (vendorVal || dateFrom || dateTo) {
    ccList = ccList.filter(function(t) {
      var v = (t.vendor_name || t.description || '').toLowerCase();
      var d = t.date || '';
      if (vendorVal && v.indexOf(vendorVal) === -1) return false;
      if (dateFrom && d && d < dateFrom) return false;
      if (dateTo && d && d > dateTo) return false;
      return true;
    });
    invList = invList.filter(function(inv) {
      var v = (inv.vendor_name || '').toLowerCase();
      var d = inv.date || '';
      if (vendorVal && v.indexOf(vendorVal) === -1) return false;
      if (dateFrom && d && d < dateFrom) return false;
      if (dateTo && d && d > dateTo) return false;
      return true;
    });
  }

  var container = document.getElementById('categorizeResults');
  container.innerHTML = '';
  var fmt = function(n) { return n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // Group CC by vendor
  var ccByVendor = {};
  var unmappedCC = [];
  ccList.forEach(function(t) {
    var v = t.vendor_name;
    if (!v) { unmappedCC.push(t); return; }
    if (!ccByVendor[v]) ccByVendor[v] = [];
    ccByVendor[v].push(Object.assign({_matched: false}, t));
  });

  // Group invoices by vendor
  var invByVendor = {};
  invList.forEach(function(inv) {
    var v = inv.vendor_name || '';
    if (!v) return;
    if (!invByVendor[v]) invByVendor[v] = [];
    invByVendor[v].push(Object.assign({_matched: false}, inv));
  });

  // All vendors from both sides
  var allVendors = {};
  Object.keys(ccByVendor).forEach(function(v) { allVendors[v] = true; });
  Object.keys(invByVendor).forEach(function(v) { allVendors[v] = true; });
  var vendorList = Object.keys(allVendors).sort();

  var rows = []; // {vendor, cc, inv, matchType, status}

  vendorList.forEach(function(vendor) {
    var ccs = (ccByVendor[vendor] || []).slice();
    var invs = (invByVendor[vendor] || []).slice();

    // Sort both by date so greedy matching pairs chronologically
    ccs.sort(function(a, b) { return (a.date || '').localeCompare(b.date || ''); });
    invs.sort(function(a, b) { return (a.date || '').localeCompare(b.date || ''); });

    // Try to match each CC to an invoice (prefer closest date when amounts tie)
    ccs.forEach(function(cc) {
      var bestMatch = null;
      var bestType = '';
      var bestDiff = Infinity;
      var bestDateDiff = Infinity;
      var _ccDate = cc.date ? new Date(cc.date) : null;
      var ccForex = cc.forex_amount ? parseFloat(cc.forex_amount) : null;
      var ccCur = cc.forex_currency || null;
      var ccInr = parseFloat(cc.amount) || 0;

      invs.forEach(function(inv) {
        if (inv._matched) return;
        var invAmt = parseFloat(inv.amount) || 0;
        var invCur = (inv.currency || 'INR').toUpperCase();

        var diff = null;
        var mtype = '';

        // Case 1: USD -> USD (or same forex currency)
        if (ccCur && ccForex && invCur === ccCur) {
          diff = Math.abs(ccForex - invAmt);
          mtype = ccCur + ' \u2192 ' + invCur;
        }
        // Case 2: Forex -> INR (CC has forex, invoice is INR)
        else if (ccCur && ccForex && invCur === 'INR') {
          diff = Math.abs(ccInr - invAmt);
          mtype = ccCur + ' \u2192 INR (forex)';
        }
        // Case 3: INR -> INR (no forex)
        else if (!ccCur && invCur === 'INR') {
          diff = Math.abs(ccInr - invAmt);
          mtype = 'INR \u2192 INR';
        }
        // Case 4: Any currency -> INR with forex
        else if (ccCur && ccForex && invCur !== ccCur && invCur !== 'INR') {
          // Cross-currency — can't reliably match
          return;
        }
        // Fallback: just compare INR amounts
        else {
          diff = Math.abs(ccInr - invAmt);
          mtype = 'INR \u2192 ' + invCur;
        }

        // Date proximity as tiebreaker when amounts are equal
        var dateDiff = 9999;
        if (_ccDate && inv.date) {
          var invD = new Date(inv.date);
          dateDiff = Math.abs((_ccDate - invD) / 86400000);
        }

        if (diff !== null && (diff < bestDiff || (diff === bestDiff && dateDiff < bestDateDiff))) {
          bestDiff = diff;
          bestDateDiff = dateDiff;
          bestMatch = inv;
          bestType = mtype;
        }
      });

      // Tolerance: exact or within 1% of compared amount, AND date within ±10 days
      var _compAmt = (ccForex && ccCur) ? ccForex : ccInr;
      var threshold = Math.max(_compAmt < 100 ? 0.5 : 1, _compAmt * 0.01);
      if (bestMatch && bestDiff <= threshold && bestDateDiff <= 10) {
        bestMatch._matched = true;
        cc._matched = true;
        rows.push({
          vendor: vendor,
          cc: cc,
          inv: bestMatch,
          matchType: bestType,
          diff: bestDiff,
          status: bestDiff < 0.01 ? 'exact' : 'close'
        });
      } else {
        rows.push({
          vendor: vendor,
          cc: cc,
          inv: null,
          matchType: '',
          diff: null,
          status: 'no_invoice'
        });
      }
    });

    // Unmatched invoices
    invs.forEach(function(inv) {
      if (inv._matched) return;
      rows.push({
        vendor: vendor,
        cc: null,
        inv: inv,
        matchType: '',
        diff: null,
        status: 'no_cc'
      });
    });
  });

  // Unmapped CC (no vendor resolved)
  unmappedCC.forEach(function(cc) {
    rows.push({
      vendor: '(unmapped)',
      cc: cc,
      inv: null,
      matchType: '',
      diff: null,
      status: 'unmapped'
    });
  });

  // --- Multi-invoice sum matching (GSTIN-grouped) --- runs BEFORE vendor-agnostic 1:1
  // Logic: 1) Date: find invoices within ±5 days of CC
  //        2) GSTIN: group by same GSTIN (same supplier — vendor name may differ)
  //        3) Sum: try full GSTIN group first, then combinations within group
  //        No mixed-GSTIN fallback — GSTIN is the identity
  var _pd = function(s) { if (!s) return null; var d = new Date(s); return isNaN(d.getTime()) ? null : d; };
  var _daysDiff = function(a, b) { if (!a || !b) return 9999; return Math.abs((a - b) / 86400000); };

  (function() {
    var sumUnmatchedCC = [];
    var sumUnmatchedInv = [];
    rows.forEach(function(r, ri) {
      if ((r.status === 'no_invoice' || r.status === 'unmapped') && r.cc) sumUnmatchedCC.push(ri);
      if (r.status === 'no_cc' && r.inv) sumUnmatchedInv.push(ri);
    });

    var sumInvUsed = {};

    function sumCombinations(arr, minSize, maxSize) {
      var results = [];
      function sc(start, cur) {
        if (cur.length >= minSize) results.push(cur.slice());
        if (cur.length >= maxSize) return;
        for (var i = start; i < arr.length; i++) {
          cur.push(arr[i]);
          sc(i + 1, cur);
          cur.pop();
        }
      }
      sc(0, []);
      return results;
    }

    sumUnmatchedCC.forEach(function(ccIdx) {
      var r = rows[ccIdx];
      if (!r || (r.status !== 'no_invoice' && r.status !== 'unmapped')) return;
      var cc5 = r.cc;
      var ccInr5 = parseFloat(cc5.amount) || 0;
      var ccForex5 = cc5.forex_amount ? parseFloat(cc5.forex_amount) : null;
      var ccCur5 = cc5.forex_currency || null;
      var ccDate5 = _pd(cc5.date);

      // Step 1: Find nearby unmatched invoices (±5 days), must have GSTIN
      var nearby5 = [];
      sumUnmatchedInv.forEach(function(invIdx) {
        if (sumInvUsed[invIdx]) return;
        var inv5 = rows[invIdx].inv;
        if (!inv5) return;
        var invDate5 = _pd(inv5.date);
        if (_daysDiff(ccDate5, invDate5) > 5) return;
        if (!inv5.vendor_gstin) return;

        var invCur5 = (inv5.currency || 'INR').toUpperCase();
        var compatible = false;
        if (ccCur5 && ccForex5 && invCur5 === ccCur5) compatible = true;
        else if (!ccCur5 && invCur5 === 'INR') compatible = true;
        else if (ccCur5 && invCur5 === 'INR') compatible = true;
        if (!compatible) return;

        nearby5.push({invIdx: invIdx, inv: inv5});
      });

      if (nearby5.length < 2) return;

      // Step 2: Group by GSTIN
      var byGstin = {};
      nearby5.forEach(function(item) {
        var gstin = item.inv.vendor_gstin;
        if (!byGstin[gstin]) byGstin[gstin] = [];
        byGstin[gstin].push(item);
      });

      // Determine target amount
      var targetAmt5 = ccForex5 && ccCur5 ? ccForex5 : ccInr5;
      var useInr = !ccCur5 || (ccCur5 && nearby5[0] && (nearby5[0].inv.currency || 'INR').toUpperCase() === 'INR');
      if (useInr) targetAmt5 = ccInr5;

      var found5 = null;
      var thresh5 = Math.max(1, targetAmt5 * 0.005);

      // Step 3: Try full GSTIN group sum first, then combinations
      Object.keys(byGstin).forEach(function(gstin) {
        if (found5) return;
        var group = byGstin[gstin];
        if (group.length < 2) return;

        var fullSum = group.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
        if (Math.abs(fullSum - targetAmt5) <= thresh5) {
          found5 = group;
          return;
        }

        var combos = sumCombinations(group, 2, Math.min(6, group.length));
        for (var ci = 0; ci < combos.length; ci++) {
          var combo = combos[ci];
          var sum = combo.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
          if (Math.abs(sum - targetAmt5) <= thresh5) {
            found5 = combo;
            break;
          }
        }
      });

      if (found5) {
        found5.forEach(function(item) {
          sumInvUsed[item.invIdx] = true;
          if (rows[item.invIdx]) rows[item.invIdx] = null;
        });

        var totalSum = found5.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
        var totalDiff5 = Math.abs(totalSum - targetAmt5);
        var invNumbers = found5.map(function(item) { return item.inv.invoice_number || ''; }).filter(Boolean).join(' + ');
        var gstin5 = found5[0].inv.vendor_gstin || '';
        var mtype5 = (ccCur5 || 'INR') + ' \u2192 ' + (found5[0].inv.currency || 'INR') + ' (GSTIN:' + gstin5.substring(0, 10) + '.. x' + found5.length + ')';

        var anyInZoho = found5.some(function(item) { return item.inv.in_zoho; });
        var zohoBillIds = [];
        var zohoBillId = '';
        var zohoBillStatus = '';
        found5.forEach(function(item) {
          if (item.inv.zoho_bill_id && zohoBillIds.indexOf(item.inv.zoho_bill_id) === -1) {
            zohoBillIds.push(item.inv.zoho_bill_id);
          }
          if (!zohoBillId && item.inv.zoho_bill_id) {
            zohoBillId = item.inv.zoho_bill_id;
            zohoBillStatus = item.inv.zoho_bill_status || '';
          }
        });
        rows[ccIdx] = {
          vendor: found5[0].inv.vendor_name || r.vendor,
          cc: cc5,
          inv: {
            vendor_name: found5[0].inv.vendor_name || r.vendor,
            amount: totalSum,
            currency: found5[0].inv.currency || 'INR',
            date: found5[0].inv.date,
            invoice_number: invNumbers,
            vendor_gstin: gstin5,
            in_zoho: anyInZoho,
            zoho_bill_id: zohoBillId,
            zoho_bill_ids: zohoBillIds,
            zoho_bill_status: zohoBillStatus,
            _grouped_invoices: found5.map(function(item) { return item.inv; })
          },
          matchType: mtype5,
          diff: totalDiff5,
          status: totalDiff5 < 0.01 ? 'exact' : 'close'
        };
      }
    });

    // Remove nulled rows from sum matching
    for (var ri3 = rows.length - 1; ri3 >= 0; ri3--) {
      if (rows[ri3] === null) rows.splice(ri3, 1);
    }
  })();

  // --- Amount+date matching for unmatched items (vendor-agnostic) ---
  // If vendor names differ but amount/forex match and dates are close, treat as match

  var unmatchedCCIdxs = [];
  var unmatchedInvIdxs = [];
  rows.forEach(function(r, ri) {
    if ((r.status === 'no_invoice' || r.status === 'unmapped') && r.cc) unmatchedCCIdxs.push(ri);
    if (r.status === 'no_cc' && r.inv) unmatchedInvIdxs.push(ri);
  });

  var _invUsed = {};
  unmatchedCCIdxs.forEach(function(ccIdx) {
    var r = rows[ccIdx];
    var cc2 = r.cc;
    var ccInr2 = parseFloat(cc2.amount) || 0;
    var ccForex2 = cc2.forex_amount ? parseFloat(cc2.forex_amount) : null;
    var ccCur2 = cc2.forex_currency || null;
    var ccDate2 = _pd(cc2.date);
    var best2 = null, bestDiff2 = Infinity, bestInvIdx2 = -1, bestMtype2 = '';

    unmatchedInvIdxs.forEach(function(invIdx) {
      if (_invUsed[invIdx]) return;
      var inv2 = rows[invIdx].inv;
      var invAmt2 = parseFloat(inv2.amount) || 0;
      var invCur2 = (inv2.currency || 'INR').toUpperCase();
      var invDate2 = _pd(inv2.date);
      if (_daysDiff(ccDate2, invDate2) > 30) return;

      var diff2 = null, mtype2 = '', compAmt2 = ccInr2;
      if (ccCur2 && ccForex2 && invCur2 === ccCur2) {
        diff2 = Math.abs(ccForex2 - invAmt2); mtype2 = ccCur2 + ' \u2192 ' + invCur2;
        compAmt2 = ccForex2;
      } else if (ccCur2 && ccForex2 && invCur2 === 'INR') {
        diff2 = Math.abs(ccInr2 - invAmt2); mtype2 = ccCur2 + ' \u2192 INR (forex)';
      } else if (!ccCur2 && invCur2 === 'INR') {
        diff2 = Math.abs(ccInr2 - invAmt2); mtype2 = 'INR \u2192 INR';
      } else {
        // Cross-currency mismatch (e.g. INR CC vs USD invoice) — skip
        return;
      }

      var threshold2 = Math.max(compAmt2 < 100 ? 0.5 : 1, compAmt2 * 0.01);
      if (diff2 !== null && diff2 <= threshold2 && diff2 < bestDiff2) {
        bestDiff2 = diff2; best2 = inv2; bestInvIdx2 = invIdx; bestMtype2 = mtype2;
      }
    });

    if (best2 && bestInvIdx2 >= 0) {
      _invUsed[bestInvIdx2] = true;
      rows[ccIdx] = {
        vendor: (best2.vendor_name || r.vendor),
        cc: cc2, inv: best2, matchType: bestMtype2, diff: bestDiff2,
        status: bestDiff2 < 0.01 ? 'exact' : 'close'
      };
      rows[bestInvIdx2] = null; // mark for removal
    }
  });

  // Remove nulled rows
  for (var _ri = rows.length - 1; _ri >= 0; _ri--) {
    if (rows[_ri] === null) rows.splice(_ri, 1);
  }

  // --- Cross-month search for unmatched CC (limited to +/- 1 month) --- skip when viewing all months
  if (idx === 'all' || isNaN(parseInt(idx))) { /* skip cross-month for all-months view */ } else {
  var _monthNames = {Jan:0, Feb:1, Mar:2, Apr:3, May:4, Jun:5, Jul:6, Aug:7, Sep:8, Oct:9, Nov:10, Dec:11};
  var _parseMonthDate = function(mk) {
    var parts = (mk || '').split(' ');
    if (parts.length !== 2) return null;
    var mon = _monthNames[parts[0]];
    var yr = parseInt(parts[1]);
    if (mon == null || isNaN(yr)) return null;
    return new Date(yr, mon, 15);
  };
  var _curMonthDate = _parseMonthDate(m.month);

  var otherMonthInvs = [];
  _compareData.months.forEach(function(om, omIdx) {
    if (omIdx === idx) return;
    // Only include months within ~45 days (roughly 1 month gap)
    var omDate = _parseMonthDate(om.month);
    if (_curMonthDate && omDate && Math.abs(_curMonthDate - omDate) > 45 * 86400000) return;
    (om.invoices || []).forEach(function(inv) {
      otherMonthInvs.push(Object.assign({_src_month: om.month}, inv));
    });
  });

  rows.forEach(function(r, ri) {
    if (r.status !== 'no_invoice' && r.status !== 'unmapped') return;
    if (!r.cc) return;
    var cc3 = r.cc;
    var vendor3 = r.vendor;
    var ccInr3 = parseFloat(cc3.amount) || 0;
    var ccForex3 = cc3.forex_amount ? parseFloat(cc3.forex_amount) : null;
    var ccCur3 = cc3.forex_currency || null;
    var ccDate3 = _pd(cc3.date);
    var bestMatch3 = null;
    var bestType3 = '';
    var bestDiff3 = Infinity;
    var bestMonth3 = '';

    otherMonthInvs.forEach(function(inv) {
      var vendorMatch3 = (inv.vendor_name || '') === vendor3;
      var invAmt3 = parseFloat(inv.amount) || 0;
      var invCur3 = (inv.currency || 'INR').toUpperCase();
      var invDate3 = _pd(inv.date);

      // For non-vendor matches, also require date proximity (45 days)
      if (!vendorMatch3 && _daysDiff(ccDate3, invDate3) > 45) return;

      var diff3 = null;
      var mtype3 = '';
      if (ccCur3 && ccForex3 && invCur3 === ccCur3) {
        diff3 = Math.abs(ccForex3 - invAmt3);
        mtype3 = ccCur3 + ' \u2192 ' + invCur3;
      } else if (ccCur3 && ccForex3 && invCur3 === 'INR') {
        diff3 = Math.abs(ccInr3 - invAmt3);
        mtype3 = ccCur3 + ' \u2192 INR (forex)';
      } else if (!ccCur3 && invCur3 === 'INR') {
        diff3 = Math.abs(ccInr3 - invAmt3);
        mtype3 = 'INR \u2192 INR';
      } else {
        // Cross-currency mismatch — skip
        return;
      }

      var _compAmt3 = (ccCur3 && ccForex3 && invCur3 === ccCur3) ? ccForex3 : ccInr3;
      var _thresh3 = Math.max(_compAmt3 < 100 ? 0.5 : 1, _compAmt3 * 0.01);
      if (diff3 !== null && diff3 <= _thresh3 && diff3 < bestDiff3) {
        bestDiff3 = diff3;
        bestMatch3 = inv;
        bestType3 = mtype3;
        bestMonth3 = inv._src_month;
      }
    });

    if (bestMatch3 && bestDiff3 < Infinity) {
      rows[ri] = {
        vendor: (bestMatch3.vendor_name || vendor3),
        cc: cc3,
        inv: bestMatch3,
        matchType: bestType3 + ' [' + bestMonth3 + ']',
        diff: bestDiff3,
        status: bestDiff3 < 0.01 ? 'cross_exact' : 'cross_close'
      };
    }
  });
  } // end cross-month else block

  // Sort rows: Create Bill first, then In Zoho, then rest
  var statusOrder = {exact: 0, close: 0, cross_exact: 1, cross_close: 1, no_cc: 2, no_invoice: 3, unmapped: 4};
  rows.sort(function(a, b) {
    var oa = statusOrder[a.status] != null ? statusOrder[a.status] : 4;
    var ob = statusOrder[b.status] != null ? statusOrder[b.status] : 4;
    if (oa !== ob) return oa - ob;
    // Within matched: Create Bill (not in_zoho) before In Zoho
    var aZoho = (a.inv && a.inv.in_zoho) ? 1 : 0;
    var bZoho = (b.inv && b.inv.in_zoho) ? 1 : 0;
    if (aZoho !== bZoho) return aZoho - bZoho;
    return (a.vendor || '').localeCompare(b.vendor || '');
  });

  // Store globally and save
  _catRows = rows;
  _catMonth = m.month;

  var matched = rows.filter(function(r) { return r.status === 'exact' || r.status === 'close'; }).length;
  var crossMatched = rows.filter(function(r) { return r.status === 'cross_exact' || r.status === 'cross_close'; }).length;
  var noInv = rows.filter(function(r) { return r.status === 'no_invoice'; }).length;
  var noCc = rows.filter(function(r) { return r.status === 'no_cc'; }).length;
  var unmapped = rows.filter(function(r) { return r.status === 'unmapped'; }).length;

  // Save to file
  var saveRows = rows.map(function(r) {
    return {
      vendor: r.vendor,
      cc_amount_inr: r.cc ? r.cc.amount : null,
      cc_forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
      cc_forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null,
      cc_date: r.cc ? r.cc.date : null,
      cc_description: r.cc ? r.cc.description : null,
      inv_amount: r.inv ? r.inv.amount : null,
      inv_currency: r.inv ? r.inv.currency : null,
      inv_date: r.inv ? r.inv.date : null,
      inv_invoice_number: r.inv ? r.inv.invoice_number : null,
      inv_vendor_gstin: r.inv ? r.inv.vendor_gstin : null,
      match_type: r.matchType || null,
      diff: r.diff != null ? r.diff : null,
      status: r.status
    };
  });
  fetch('/api/compare/save-categorize', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      month: m.month, rows: saveRows,
      summary: {matched: matched, cross_matched: crossMatched, no_invoice: noInv, no_cc: noCc, unmapped: unmapped}
    })
  }).then(function() {
    // Refresh overall summary after save completes
    _fetchOverallCategorize();
  });

  // Render CC-based view
  renderCatView('cc');
}

function _fetchOverallCategorize() {
  fetch('/api/compare/categorize-overall').then(function(r) { return r.json(); }).then(function(data) {
    var el = document.getElementById('catOverallSummary');
    if (!el) return;
    var t = data.totals || {};
    var total = t.total || 0;
    var matched = (t.exact || 0) + (t.close || 0);
    var cross = (t.cross_exact || 0) + (t.cross_close || 0);
    var resolved = matched + cross;
    var pct = total ? Math.round(resolved / total * 100) : 0;
    el.innerHTML =
      '<strong style="font-size:13px;min-width:120px">\uD83D\uDCCA All Months (' + (t.months_done || 0) + ' done, ' + total + ' txns)</strong>' +
      '<span style="color:var(--green)">\u2713 Exact: ' + (t.exact || 0) + '</span>' +
      '<span style="color:var(--green)">\u2248 Close: ' + (t.close || 0) + '</span>' +
      '<span style="color:var(--accent)">\u2194 Other Month: ' + cross +
        (cross ? ' <span style="font-size:10px;opacity:0.7">(E:' + (t.cross_exact||0) + ' C:' + (t.cross_close||0) + ')</span>' : '') + '</span>' +
      '<span style="color:var(--yellow)">\u26A0 No Invoice: ' + (t.no_invoice || 0) + '</span>' +
      '<span style="color:var(--text-dim)">\u2753 Unmapped: ' + (t.unmapped || 0) + '</span>' +
      '<span style="color:var(--green);margin-left:auto;font-weight:bold">\u2714 Resolved: ' + resolved + '/' + total + ' (' + pct + '%)</span>';
  }).catch(function() {
    var el = document.getElementById('catOverallSummary');
    if (el) el.innerHTML = '<span style="color:var(--text-dim)">Run Categorize Check on each month to build overall summary</span>';
  });
}

function renderCatView() {
  var container = document.getElementById('categorizeResults');
  container.innerHTML = '';
  var rows = _catRows;
  if (!rows.length) return;
  var fmt = function(n) { return n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // CC-based: only rows that have a CC entry, exclude paid bills
  var filtered = rows.filter(function(r) {
    if (!r.cc) return false;
    if (r.inv && r.inv.zoho_bill_status && r.inv.zoho_bill_status.toLowerCase() === 'paid') return false;
    return true;
  });

  var exactCount = filtered.filter(function(r) { return r.status === 'exact'; }).length;
  var closeCount = filtered.filter(function(r) { return r.status === 'close'; }).length;
  var matched = exactCount + closeCount;
  var crossExact = filtered.filter(function(r) { return r.status === 'cross_exact'; }).length;
  var crossClose = filtered.filter(function(r) { return r.status === 'cross_close'; }).length;
  var crossMatched = crossExact + crossClose;
  var noInv = filtered.filter(function(r) { return r.status === 'no_invoice'; }).length;
  var unmapped = filtered.filter(function(r) { return r.status === 'unmapped'; }).length;

  // Overall summary row (all months aggregate) - placeholder, filled async
  var overallDiv = document.createElement('div');
  overallDiv.id = 'catOverallSummary';
  overallDiv.style.cssText = 'padding:10px 12px;font-size:12px;border-bottom:2px solid var(--accent);background:rgba(108,140,255,0.08);display:flex;gap:12px;flex-wrap:wrap;align-items:center';
  overallDiv.innerHTML = '<span style="color:var(--text-dim)">Loading overall summary...</span>';
  container.appendChild(overallDiv);
  _fetchOverallCategorize();

  // Header
  var hdr = document.createElement('div');
  hdr.style.cssText = 'padding:10px 12px;font-size:12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.03);display:flex;gap:16px;flex-wrap:wrap;align-items:center';
  hdr.innerHTML =
    '<strong style="font-size:13px">CC Based (' + filtered.length + ')</strong>' +
    '<span style="color:var(--green)">\u2713 Matched: ' + matched + '</span>' +
    (crossMatched ? '<span style="color:var(--accent)">\u2194 Other Month: ' + crossMatched + '</span>' : '') +
    '<span style="color:var(--yellow)">\u26A0 No Invoice: ' + noInv + '</span>' +
    (unmapped ? '<span style="color:var(--text-dim)">\u2753 Unmapped: ' + unmapped + '</span>' : '') +
    '<button id="catCreateSelectedBtn" onclick="confirmCatCreateSelected()" style="display:none;background:var(--accent);color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600;margin-left:auto">Create & Record (0)</button>';
  container.appendChild(hdr);

  // Status filter bar
  var catFilter = document.createElement('div');
  catFilter.style.cssText = 'padding:6px 12px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;font-size:11px';
  catFilter.innerHTML = '<label style="color:var(--text-dim);font-weight:600">Status:</label>' +
    '<select id="catStatusFilter" onchange="applyCatStatusFilter()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px">' +
    '<option value="">All</option>' +
    '<option value="exact">Exact</option>' +
    '<option value="close">Close</option>' +
    '<option value="cross_exact">Other Month (Exact)</option>' +
    '<option value="cross_close">Other Month (Close)</option>' +
    '<option value="no_invoice">No Invoice</option>' +
    '<option value="unmapped">Unmapped</option>' +
    '</select>';
  container.appendChild(catFilter);

  // Reset selection tracking
  _catSelectedInvs = {};

  // Table
  var tbl = document.createElement('table');
  tbl.className = 'cat-table';
  tbl.innerHTML = '<thead><tr><th style="width:28px;text-align:center"><input type="checkbox" id="catSelectAll" onchange="toggleCatSelectAll(this)" title="Select all Create Bill rows"></th><th>Vendor</th><th>CC Description</th><th>CC Forex</th><th>CC Amount (INR)</th><th>CC Date</th><th>Card</th><th>Inv Amount</th><th>Inv Date</th><th>Match Type</th><th>Diff</th><th>Confidence</th><th>Status</th><th>Action</th></tr></thead>';
  var tbody = document.createElement('tbody');

  filtered.forEach(function(r) {
    var tr = document.createElement('tr');
    tr.className = 'cat-row';
    tr.setAttribute('data-vendor', (r.vendor || '').toLowerCase());
    tr.setAttribute('data-date', (r.cc && r.cc.date) || '');
    var statusHtml = '';
    var rowBg = '';
    if (r.status === 'exact') {
      statusHtml = '<span style="color:var(--green)">\u2713 Exact</span>';
      rowBg = 'rgba(80,200,120,0.06)';
    } else if (r.status === 'close') {
      statusHtml = '<span style="color:var(--green)">\u2248 Close</span>';
      rowBg = 'rgba(80,200,120,0.04)';
    } else if (r.status === 'cross_exact') {
      statusHtml = '<span style="color:var(--accent)">\u2194 Other Month (Exact)</span>';
      rowBg = 'rgba(96,165,250,0.08)';
    } else if (r.status === 'cross_close') {
      statusHtml = '<span style="color:var(--accent)">\u2194 Other Month (Close)</span>';
      rowBg = 'rgba(96,165,250,0.05)';
    } else if (r.status === 'no_invoice') {
      statusHtml = '<span style="color:var(--yellow)">\u26A0 No Invoice</span>';
      rowBg = 'rgba(255,200,50,0.06)';
    } else if (r.status === 'unmapped') {
      statusHtml = '<span style="color:var(--text-dim)">\u2753 Unmapped</span>';
      rowBg = 'rgba(150,150,150,0.06)';
    }
    if (rowBg) tr.style.background = rowBg;

    var ccDesc = r.cc ? (r.cc.description || '-') : '-';
    var ccAmt = r.cc ? fmt(r.cc.amount) : '-';
    var ccForex = r.cc && r.cc.forex_currency && r.cc.forex_amount
      ? r.cc.forex_currency + ' ' + fmt(r.cc.forex_amount) : '-';
    var ccDate = r.cc && r.cc.date ? r.cc.date : '-';
    var invAmt = r.inv ? fmt(r.inv.amount) + ' <span style="font-size:9px;color:var(--text-dim)">' + (r.inv.currency || 'INR') + '</span>' : '-';
    var invDate = r.inv && r.inv.date ? r.inv.date : '-';
    var diffText = r.diff != null ? fmt(r.diff) : '-';

    // Create Bill & Record button or Paid/In Zoho badge
    var actionHtml = '';
    if ((r.status === 'exact' || r.status === 'close') && r.inv) {
      if (r.inv.in_zoho) {
        var recPayload = JSON.stringify({
          bill_id: r.inv.zoho_bill_id || '',
          bill_ids: r.inv.zoho_bill_ids || [],
          vendor_name: r.inv.vendor_name || r.vendor,
          amount: r.inv.amount,
          currency: r.inv.currency || 'INR',
          date: r.inv.date,
          cc: {
            transaction_id: r.cc ? r.cc.transaction_id || '' : '',
            amount: r.cc ? r.cc.amount : 0,
            date: r.cc ? r.cc.date : '',
            card_name: r.cc ? r.cc.card_name || '' : '',
            forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
            forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null
          }
        }).replace(/'/g, "\\'").replace(/"/g, '&quot;');
        var billStatus = (r.inv.zoho_bill_status || '').toLowerCase();
        if (billStatus === 'paid') {
          actionHtml = '<span style="font-size:10px;padding:2px 8px;color:var(--green);font-weight:600">\u2705 Paid</span>';
        } else {
          var recLabel = billStatus === 'overdue' ? 'Record (Overdue)' : 'Record';
          actionHtml = '<button class="bill-create-btn" onclick="recordPaymentOnly(this, \'' + recPayload + '\')" style="font-size:10px;padding:2px 8px;background:rgba(34,197,94,0.15);color:var(--green);border:1px solid var(--green)">' + recLabel + '</button>';
        }
      } else if (r.inv._grouped_invoices && r.inv._grouped_invoices.length > 1) {
        // Bulk: multiple invoices grouped to 1 CC charge
        var ccVendor = r.cc && r.cc.vendor_name ? r.cc.vendor_name : '';
        var bulkPayload = JSON.stringify({
          invoices: r.inv._grouped_invoices.map(function(gi) {
            return {
              vendor_name: gi.vendor_name || r.vendor,
              amount: gi.amount,
              currency: gi.currency || 'INR',
              date: gi.date,
              invoice_number: gi.invoice_number || '',
              vendor_gstin: gi.vendor_gstin || ''
            };
          }),
          cc: {
            transaction_id: r.cc ? r.cc.transaction_id || '' : '',
            amount: r.cc ? r.cc.amount : 0,
            date: r.cc ? r.cc.date : '',
            card_name: r.cc ? r.cc.card_name || '' : '',
            forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
            forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null,
            vendor_name: ccVendor
          }
        }).replace(/'/g, "\\'").replace(/"/g, '&quot;');
        actionHtml = '<button class="bill-create-btn" onclick="createBillAndRecordBulk(this, \'' + bulkPayload + '\')" style="font-size:10px;padding:2px 8px">Create & Record (' + r.inv._grouped_invoices.length + ')</button>';
      } else {
        // Single invoice
        var ccVendor = r.cc && r.cc.vendor_name ? r.cc.vendor_name : '';
        var payload = JSON.stringify({
          invoice: {
            vendor_name: r.inv.vendor_name || r.vendor,
            amount: r.inv.amount,
            currency: r.inv.currency || 'INR',
            date: r.inv.date,
            invoice_number: r.inv.invoice_number || '',
            vendor_gstin: r.inv.vendor_gstin || ''
          },
          cc: {
            transaction_id: r.cc ? r.cc.transaction_id || '' : '',
            amount: r.cc ? r.cc.amount : 0,
            date: r.cc ? r.cc.date : '',
            card_name: r.cc ? r.cc.card_name || '' : '',
            forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
            forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null,
            vendor_name: ccVendor
          }
        }).replace(/'/g, "\\'").replace(/"/g, '&quot;');
        actionHtml = '<button class="bill-create-btn" onclick="createBillAndRecord(this, \'' + payload + '\')" style="font-size:10px;padding:2px 8px">Create & Record</button>';
      }
    }

    // Compute confidence for matched rows
    var confHtml = '-';
    if (r.cc && r.inv && (r.status === 'exact' || r.status === 'close' || r.status === 'cross_exact' || r.status === 'cross_close')) {
      // Vendor confidence
      var ccVendor = (r.cc.vendor_name || r.cc.description || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      var invVendor = (r.inv.vendor_name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      var vConf = 0;
      if (ccVendor && invVendor) {
        if (ccVendor === invVendor || ccVendor.indexOf(invVendor) !== -1 || invVendor.indexOf(ccVendor) !== -1) vConf = 100;
        else {
          var ccW = ccVendor.substring(0, Math.min(4, ccVendor.length));
          var invW = invVendor.substring(0, Math.min(4, invVendor.length));
          if (ccW === invW) vConf = 80;
        }
      }
      // Amount confidence — use same currency comparison as the match logic
      var ccForexAm = r.cc.forex_amount ? parseFloat(r.cc.forex_amount) : null;
      var ccForexCur = r.cc.forex_currency || null;
      var ccInrAm = parseFloat(r.cc.amount) || 0;
      var invAm = parseFloat(r.inv.amount) || 0;
      var invCur = (r.inv.currency || 'INR').toUpperCase();
      var ccAm, compareAm;
      if (ccForexCur && ccForexAm && invCur === ccForexCur) {
        // Same forex currency (e.g. USD vs USD)
        ccAm = ccForexAm; compareAm = invAm;
      } else if (ccForexCur && ccForexAm && invCur === 'INR') {
        // Forex CC vs INR invoice — compare INR
        ccAm = ccInrAm; compareAm = invAm;
      } else {
        ccAm = ccInrAm; compareAm = invAm;
      }
      var amDiff = Math.abs(ccAm - compareAm);
      var amPct = ccAm > 0 ? (amDiff / ccAm) : 1;
      var aConf = amPct < 0.001 ? 100 : amPct < 0.01 ? 95 : amPct < 0.05 ? 80 : amPct < 0.1 ? 60 : 40;
      // Date confidence
      var dConf = 0;
      if (r.cc.date && r.inv.date) {
        var cd = new Date(r.cc.date), id = new Date(r.inv.date);
        var daysDiff = Math.abs((cd - id) / 86400000);
        dConf = daysDiff <= 1 ? 100 : daysDiff <= 5 ? 90 : daysDiff <= 15 ? 50 : daysDiff <= 30 ? 25 : 0;
      }
      var overall = Math.round(vConf * 0.4 + aConf * 0.4 + dConf * 0.2);
      var ovColor = overall >= 85 ? 'var(--green)' : overall >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      var _cd = function(v) { var c = v >= 90 ? 'var(--green)' : v >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)'; return '<span style="color:'+c+';font-weight:700">'+v+'</span>'; };
      confHtml = '<div style="text-align:center;line-height:1.4">'
        + '<div style="font-size:12px;font-weight:700;color:' + ovColor + '">' + overall + '%</div>'
        + '<div style="font-size:8px;color:var(--text-dim)">Vendor:' + _cd(vConf) + ' Amt:' + _cd(aConf) + ' Date:' + _cd(dConf) + '</div></div>';
    }

    // Checkbox for Create Bill rows
    var canCreate = (r.status === 'exact' || r.status === 'close') && r.inv && !r.inv.in_zoho;
    var cbCell = canCreate
      ? '<td style="text-align:center;padding:3px 4px"><input type="checkbox" class="cat-cb" data-catidx="' + filtered.indexOf(r) + '" onchange="toggleCatCheckbox(this)"></td>'
      : '<td style="padding:3px 4px"></td>';

      tr.innerHTML = cbCell +
        '<td style="font-size:11px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + r.vendor + '">' + r.vendor + '</td>' +
        '<td style="font-size:10px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + ccDesc + '">' + ccDesc + '</td>' +
        '<td style="font-size:10px;color:var(--yellow)">' + ccForex + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + ccAmt + '</td>' +
        '<td style="font-size:10px">' + ccDate + '</td>' +
        '<td style="font-size:10px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (r.cc && r.cc.card_name ? r.cc.card_name : '-') + '">' + (r.cc && r.cc.card_name ? r.cc.card_name : '-') + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + invAmt + '</td>' +
        '<td style="font-size:10px">' + invDate + '</td>' +
        '<td style="font-size:10px">' + (r.matchType || '-') + '</td>' +
        '<td style="font-family:monospace;font-size:10px;text-align:right">' + diffText + '</td>' +
        '<td style="padding:3px 4px">' + confHtml + '</td>' +
        '<td style="font-size:10px;white-space:nowrap">' + statusHtml + '</td>' +
        '<td style="padding:3px 6px">' + actionHtml + '</td>';
      tbody.appendChild(tr);

      // Render individual sub-rows for grouped invoices
      if (r.inv && r.inv._grouped_invoices && r.inv._grouped_invoices.length > 1) {
        var catRowIdx = _catRows.indexOf(r);
        r.inv._grouped_invoices.forEach(function(gi, giIdx) {
          var subTr = document.createElement('tr');
          subTr.className = 'cat-row cat-sub-row';
          subTr.setAttribute('data-vendor', (r.vendor || '').toLowerCase());
          subTr.setAttribute('data-date', (r.cc && r.cc.date) || '');
          subTr.setAttribute('data-status', r.status || '');
          subTr.style.background = 'rgba(108,140,255,0.04)';
          var giAmt = fmt(gi.amount) + ' <span style="font-size:9px;color:var(--text-dim)">' + (gi.currency || 'INR') + '</span>';
          var giStatus = (gi.zoho_bill_status || '').toLowerCase();
          var giStatusHtml = '';
          if (giStatus === 'paid') giStatusHtml = '<span style="color:var(--green);font-size:9px">\u2705 Paid</span>';
          else if (gi.in_zoho) giStatusHtml = '<span style="color:var(--yellow);font-size:9px">\u25CF In Zoho</span>';
          var removeBtn = '<button onclick="removeGroupedInvoice(' + catRowIdx + ',' + giIdx + ')" style="font-size:9px;padding:1px 6px;background:rgba(239,68,68,0.15);color:var(--red,#ef4444);border:1px solid rgba(239,68,68,0.3);border-radius:4px;cursor:pointer" title="Remove this invoice from group">\u2715</button>';
          subTr.innerHTML =
            '<td style="text-align:center">' + removeBtn + '</td>' +
            '<td colspan="5" style="font-size:10px;padding-left:24px;color:var(--text-dim)">' +
              '<span style="color:var(--accent);margin-right:4px">\u2514</span> ' +
              'Bill ' + (giIdx + 1) + ': <span style="color:var(--text)">' + (gi.invoice_number || 'N/A') + '</span>' +
            '</td>' +
            '<td style="font-family:monospace;font-size:10px;text-align:right">' + giAmt + '</td>' +
            '<td style="font-size:10px">' + (gi.date || '-') + '</td>' +
            '<td colspan="3" style="font-size:9px;color:var(--text-dim)">' + (gi.zoho_bill_id || '') + '</td>' +
            '<td style="font-size:10px">' + giStatusHtml + '</td>' +
            '<td></td>';
          tbody.appendChild(subTr);
        });
      }
  });

  tbl.appendChild(tbody);
  container.appendChild(tbl);
}

// --- Remove an invoice from a grouped row ---
function removeGroupedInvoice(catRowIdx, giIdx) {
  var r = _catRows[catRowIdx];
  if (!r || !r.inv || !r.inv._grouped_invoices) return;
  var gis = r.inv._grouped_invoices;
  if (gis.length <= 1) return; // Can't remove the last one

  var removed = gis.splice(giIdx, 1)[0];
  addLogLine('[Group] Removed: ' + (removed.invoice_number || 'N/A') + ' (' + (removed.amount || 0) + ' ' + (removed.currency || 'INR') + ')');

  // Recalculate parent row totals
  var newTotal = gis.reduce(function(s, g) { return s + (parseFloat(g.amount) || 0); }, 0);
  r.inv.amount = newTotal;
  r.inv.invoice_number = gis.map(function(g) { return g.invoice_number || ''; }).filter(Boolean).join(' + ');
  r.inv.vendor_gstin = gis[0].vendor_gstin || '';
  r.inv.date = gis[0].date;

  // Recalculate zoho status
  var anyInZoho = gis.some(function(g) { return g.in_zoho; });
  var zohoBillIds = [];
  var zohoBillId = '';
  var zohoBillStatus = '';
  gis.forEach(function(g) {
    if (g.zoho_bill_id && zohoBillIds.indexOf(g.zoho_bill_id) === -1) zohoBillIds.push(g.zoho_bill_id);
    if (!zohoBillId && g.zoho_bill_id) { zohoBillId = g.zoho_bill_id; zohoBillStatus = g.zoho_bill_status || ''; }
  });
  r.inv.in_zoho = anyInZoho;
  r.inv.zoho_bill_id = zohoBillId;
  r.inv.zoho_bill_ids = zohoBillIds;
  r.inv.zoho_bill_status = zohoBillStatus;

  // Recalculate diff
  var ccAmt = r.cc ? (r.cc.forex_amount && r.cc.forex_currency ? parseFloat(r.cc.forex_amount) : parseFloat(r.cc.amount)) : 0;
  r.diff = Math.abs(newTotal - ccAmt);
  r.status = r.diff < 0.01 ? 'exact' : 'close';

  // Update match type
  var cur = gis[0].currency || 'INR';
  var ccCur = r.cc && r.cc.forex_currency ? r.cc.forex_currency : 'INR';
  r.matchType = ccCur + ' \u2192 ' + cur + ' (GSTIN:' + (r.inv.vendor_gstin || '').substring(0, 10) + '.. x' + gis.length + ')';

  // If only 1 left, simplify back to single invoice row
  if (gis.length === 1) {
    var solo = gis[0];
    r.inv = solo;
    r.inv._grouped_invoices = undefined;
    r.matchType = (ccCur || 'INR') + ' \u2192 ' + (solo.currency || 'INR');
  }

  renderCatView();
}

// --- Create Bill & Record Payment from Monthly Compare ---
function createBillAndRecord(btn, payloadStr) {
  var payload = JSON.parse(payloadStr.replace(/&quot;/g, '"'));
  var inv = payload.invoice || {};
  var desc = (inv.vendor_name || '') + ' — ' + (inv.currency || 'INR') + ' ' + Number(inv.amount).toLocaleString() + (inv.invoice_number ? ' (' + inv.invoice_number + ')' : '');
  showModal('Create Bill & Record Payment?', 'This will create a bill and record payment in Zoho Books: ' + desc, function() {
    btn.disabled = true;
    btn.textContent = 'Creating...';
    fetch('/api/bills/create-and-record', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'paid') {
        var badge = document.createElement('span');
        badge.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600';
        badge.textContent = '\u2713 Paid';
        // Get row reference before replacing btn (which removes it from DOM)
        var row = btn.closest('tr');
        btn.parentNode.replaceChild(badge, btn);
        // Disable checkbox if present
        var cb = row ? row.querySelector('.cat-cb') : null;
        if (cb) { cb.checked = false; cb.disabled = true; }
        addLogLine('[Bill+Pay] Paid: ' + (inv.invoice_number || inv.vendor_name) + ' -> ' + (data.bill_id || '') + ' / ' + (data.payment_id || ''));
      } else if (data.status === 'bill_created') {
        btn.textContent = 'Bill OK, Pay Failed';
        btn.style.color = 'var(--yellow)';
        btn.disabled = false;
        addLogLine('[Bill+Pay] Bill created but payment failed: ' + (data.error || 'unknown'));
      } else if (data.status === 'already_paid') {
        var badge2 = document.createElement('span');
        badge2.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600';
        badge2.textContent = '\u2713 Paid';
        btn.parentNode.replaceChild(badge2, btn);
      } else {
        btn.textContent = 'Failed';
        btn.disabled = false;
        addLogLine('[Bill+Pay] Error: ' + (data.error || 'unknown'));
      }
    })
    .catch(function(err) {
      btn.textContent = 'Error';
      btn.disabled = false;
      addLogLine('[Bill+Pay] Error: ' + err);
    });
  }, false, 'Create & Record');
}

// --- Bulk Create Bills & Record Payments (grouped invoices -> 1 CC charge) ---
function createBillAndRecordBulk(btn, payloadStr) {
  var payload = JSON.parse(payloadStr.replace(/&quot;/g, '"'));
  var invoices = payload.invoices || [];
  var count = invoices.length;
  var totalAmt = invoices.reduce(function(s, i) { return s + (parseFloat(i.amount) || 0); }, 0);
  var detailList = invoices.map(function(i, idx) {
    return (idx + 1) + '. ' + (i.invoice_number || i.vendor_name) + ' (' + (i.currency || 'INR') + ' ' + Number(i.amount).toLocaleString() + ')';
  }).join('<br>');
  showModal('Create ' + count + ' Bills & Record?',
    'This will:<br>1. Create each bill one by one<br>2. Record payment for each bill<br>3. Club all ' + count + ' payments and auto-match with CC<br><br>' + detailList,
    function() {
      btn.disabled = true;
      btn.textContent = 'Processing 0/' + count + '...';
      addLogLine('[Bulk] Starting: ' + count + ' bills, total ' + totalAmt.toLocaleString(undefined, {minimumFractionDigits:2}));

      // Progress animation
      var progress = 0;
      var progressInterval = setInterval(function() {
        if (progress < count) {
          progress++;
          btn.textContent = 'Processing ' + progress + '/' + count + '...';
        }
      }, 2000);

      fetch('/api/bills/create-and-record-bulk', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        clearInterval(progressInterval);
        var row = btn.closest('tr');

        // Build summary report
        var total = data.total || count;
        var created = data.created_count || 0;
        var paid = data.paid_count || 0;
        var skipped = data.already_paid_count || 0;
        var billOnly = data.bill_created_only || 0;
        var errors = data.error_count || 0;
        var matched = data.banking_matched;

        // Log summary header
        addLogLine('');
        addLogLine('========== BULK REPORT ==========');
        addLogLine('Total bills: ' + total);
        addLogLine('Created:     ' + created);
        addLogLine('Recorded:    ' + paid);
        if (skipped > 0) addLogLine('Skipped:     ' + skipped + ' (already paid)');
        if (billOnly > 0) addLogLine('Bill only:   ' + billOnly + ' (payment failed)');
        if (errors > 0) addLogLine('Errors:      ' + errors);
        addLogLine('Auto-match:  ' + (matched ? 'YES - ' + paid + ' payments matched to 1 CC' : 'NO'));
        addLogLine('=================================');

        // Log individual results
        if (data.results) {
          data.results.forEach(function(res, ri) {
            var icon = res.status === 'paid' ? '[OK]' : res.status === 'already_paid' ? '[SKIP]' : '[FAIL]';
            addLogLine('  ' + icon + ' ' + (res.invoice_number || '?') + ': ' + res.status +
              (res.bill_id ? ' -> ' + res.bill_id : '') +
              (res.payment_id ? ' / pay:' + res.payment_id : '') +
              (res.error ? ' - ' + res.error : ''));
          });
        }

        if (data.status === 'paid' || data.status === 'partial') {
          // Replace button with summary badge
          var badgeHtml = document.createElement('div');
          badgeHtml.style.cssText = 'font-size:9px;line-height:1.4';
          badgeHtml.innerHTML =
            '<span style="color:var(--green);font-weight:600">' + paid + '/' + total + ' Paid</span>' +
            (skipped > 0 ? '<br><span style="color:var(--yellow)">' + skipped + ' skipped</span>' : '') +
            (errors > 0 ? '<br><span style="color:var(--red,#ef4444)">' + errors + ' errors</span>' : '') +
            '<br><span style="color:' + (matched ? 'var(--green)' : 'var(--yellow)') + '">' +
              (matched ? 'CC matched' : 'CC not matched') + '</span>';
          btn.parentNode.replaceChild(badgeHtml, btn);
          var cb = row ? row.querySelector('.cat-cb') : null;
          if (cb) { cb.checked = false; cb.disabled = true; }
        } else {
          btn.textContent = 'Failed (' + errors + ' errors)';
          btn.style.color = 'var(--red,#ef4444)';
          btn.disabled = false;
          addLogLine('[Bulk] Error: ' + (data.error || 'see details above'));
        }
      })
      .catch(function(err) {
        clearInterval(progressInterval);
        btn.textContent = 'Error';
        btn.disabled = false;
        addLogLine('[Bulk] Error: ' + err);
      });
    }, false, 'Create ' + count + ' Bills');
}

// --- Record Payment Only (bill already in Zoho) ---
function recordPaymentOnly(btn, payloadStr) {
  var payload = JSON.parse(payloadStr.replace(/&quot;/g, '"'));
  var desc = (payload.vendor_name || '') + ' — ' + (payload.currency || 'INR') + ' ' + Number(payload.amount).toLocaleString();
  showModal('Record Payment?', 'Bill already exists in Zoho. This will record payment and auto-match the CC banking transaction: ' + desc, function() {
    btn.disabled = true;
    btn.textContent = 'Recording...';
    fetch('/api/bills/record-only', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'paid') {
        var badge = document.createElement('span');
        badge.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600';
        badge.textContent = '\u2713 Paid';
        btn.parentNode.replaceChild(badge, btn);
        addLogLine('[Record] Paid: ' + (payload.vendor_name || '') + ' -> payment_id=' + (data.payment_id || ''));
      } else {
        btn.textContent = 'Failed';
        btn.disabled = false;
        addLogLine('[Record] Error: ' + (data.error || 'unknown'));
      }
    })
    .catch(function(err) {
      btn.textContent = 'Error';
      btn.disabled = false;
      addLogLine('[Record] Error: ' + err);
    });
  }, false, 'Record Payment');
}

// --- Categorize Check: bulk Create & Record selection ---
var _catSelectedItems = {}; // idx -> {invoice, cc} payload

function toggleCatCheckbox(cb) {
  var idx = cb.getAttribute('data-catidx');
  if (cb.checked) {
    var filtered = _catRows.filter(function(r) { return r.cc != null; });
    var r = filtered[parseInt(idx)];
    if (r && r.inv && r.cc) {
      _catSelectedItems[idx] = {
        invoice: {
          vendor_name: r.inv.vendor_name || r.vendor,
          amount: r.inv.amount,
          currency: r.inv.currency || 'INR',
          date: r.inv.date,
          invoice_number: r.inv.invoice_number || '',
          vendor_gstin: r.inv.vendor_gstin || ''
        },
        cc: {
          transaction_id: r.cc.transaction_id || '',
          amount: r.cc.amount || 0,
          date: r.cc.date || '',
          card_name: r.cc.card_name || '',
          forex_amount: r.cc.forex_amount || null,
          forex_currency: r.cc.forex_currency || null
        }
      };
    }
  } else {
    delete _catSelectedItems[idx];
  }
  _updateCatSelectedBtn();
}

function toggleCatSelectAll(cb) {
  document.querySelectorAll('.cat-cb').forEach(function(c) {
    var row = c.closest('tr');
    if (row && row.style.display === 'none') return;
    c.checked = cb.checked;
    toggleCatCheckbox(c);
  });
}

function _updateCatSelectedBtn() {
  var btn = document.getElementById('catCreateSelectedBtn');
  var count = Object.keys(_catSelectedItems).length;
  if (count > 0) {
    btn.style.display = 'inline-block';
    btn.textContent = 'Create & Record (' + count + ')';
    btn.disabled = false;
  } else {
    btn.style.display = 'none';
  }
}

function confirmCatCreateSelected() {
  var count = Object.keys(_catSelectedItems).length;
  if (!count) return;
  showModal('Create Bills & Record Payments?', 'This will create ' + count + ' bills, record payments, and auto-match banking transactions in Zoho Books.', function() {
    catCreateAndRecordSelected();
  }, true, 'Create & Record ' + count);
}

function catCreateAndRecordSelected() {
  var keys = Object.keys(_catSelectedItems);
  if (!keys.length) return;
  var btn = document.getElementById('catCreateSelectedBtn');
  btn.disabled = true;
  btn.textContent = 'Processing ' + keys.length + '...';
  var total = keys.length, done = 0, success = 0;

  // Sequential to avoid rate limits
  function processNext(i) {
    if (i >= keys.length) {
      btn.textContent = success + '/' + total + ' Paid';
      _updateCatSelectedBtn();
      addLogLine('[Bill+Pay] Bulk: ' + success + '/' + total + ' paid');
      return;
    }
    var idx = keys[i];
    var payload = _catSelectedItems[idx];
    var inv = payload.invoice;
    var cb = document.querySelector('.cat-cb[data-catidx="' + idx + '"]');
    btn.textContent = 'Processing ' + (i + 1) + '/' + total + '...';

    fetch('/api/bills/create-and-record', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      done++;
      if (data.status === 'paid' || data.status === 'already_paid') {
        success++;
        if (cb) {
          var actionTd = cb.closest('tr').querySelector('td:last-child');
          if (actionTd) {
            actionTd.innerHTML = '<span style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600">\u2713 Paid</span>';
          }
          cb.checked = false; cb.disabled = true;
        }
        delete _catSelectedItems[idx];
        addLogLine('[Bill+Pay] Paid: ' + (inv.invoice_number || inv.vendor_name));
      } else {
        addLogLine('[Bill+Pay] Failed: ' + (inv.invoice_number || inv.vendor_name) + ' - ' + (data.error || 'unknown'));
      }
      setTimeout(function() { processNext(i + 1); }, 500);
    })
    .catch(function(err) {
      done++;
      addLogLine('[Bill+Pay] Error: ' + (inv.invoice_number || inv.vendor_name) + ' - ' + err);
      setTimeout(function() { processNext(i + 1); }, 500);
    });
  }
  processNext(0);
}

// --- Info tooltip positioning (fixed, not clipped by scroll) ---
document.querySelectorAll('.info-btn').forEach(function(btn) {
  var tip = btn.querySelector('.info-tooltip');
  if (!tip) return;
  btn.addEventListener('mouseenter', function() {
    var rect = btn.getBoundingClientRect();
    tip.style.display = 'block';
    // Position to the right of the button
    var left = rect.right + 10;
    var top = rect.top + rect.height / 2 - tip.offsetHeight / 2;
    // Keep within viewport
    if (left + 290 > window.innerWidth) left = rect.left - 290;
    if (top < 4) top = 4;
    if (top + tip.offsetHeight > window.innerHeight - 4) top = window.innerHeight - 4 - tip.offsetHeight;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  });
  btn.addEventListener('mouseleave', function() {
    tip.style.display = 'none';
  });
});
</script>
</body>
</html>
"""

# --- Main ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CC Statement Automation Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Port to run on (default: 5000)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Ensure output dir exists
    os.makedirs(os.path.join(PROJECT_ROOT, "output"), exist_ok=True)

    log_action("Dashboard starting on http://localhost:{0}".format(args.port))

    if not args.no_open and not os.environ.get("WERKZEUG_RUN_MAIN"):
        # Only open browser on initial launch, not on reloader restarts
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    # TEMPLATES_AUTO_RELOAD: pick up HTML/CSS/JS changes on browser refresh
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True, use_reloader=True)
