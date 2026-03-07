"""
Scheduled Loop Orchestrator — CC Statement Automation

Designed to run via Windows Task Scheduler every 10-15 minutes.
Each invocation checks for new work, processes incrementally, and exits.

Phases:
  A. Fetch new invoice emails from Outlook
  B. Extract data from new PDFs
  C. Create vendors/bills in Zoho for new invoices
  D. Parse CC statement PDFs (if changed)
  E. Match unmatched bills to CC transactions (retry logic)
  F. Import CC CSVs to Zoho Banking
  G. Auto-match banking transactions to payments

Usage:
    python run_loop.py                  # Run all phases
    python run_loop.py --phase invoices # Phase A-C only
    python run_loop.py --phase cc       # Phase D-G only
    python run_loop.py --dry-run        # Load state, log plan, no API calls
"""

import sys
import os
import json
import time
import argparse
import traceback
import importlib.util
from datetime import datetime

# Ensure scripts/ is importable (for utils.py)
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from utils import PROJECT_ROOT, log_action


def _import_script(filename):
    """Import a script from scripts/ folder by filename (handles numeric prefixes)."""
    module_name = filename.replace(".py", "")
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(SCRIPTS_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

STATE_FILE = os.path.join(PROJECT_ROOT, "output", "loop_state.json")
LOCK_FILE = os.path.join(PROJECT_ROOT, "output", ".loop_lock")


# --- State Management ---

def load_state():
    """Load persistent state from previous runs."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "version": 1,
        "last_run": None,
        "last_email_check": None,
        "processed_email_ids": [],
        "processed_pdf_files": [],
        "cc_statements_hash": {},
        "consecutive_failures": 0,
        "run_history": [],
    }


def save_state(state):
    """Persist state for next run."""
    state["last_run"] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# --- Lock ---

def acquire_lock():
    """Prevent concurrent runs. Returns True if lock acquired."""
    if os.path.exists(LOCK_FILE):
        try:
            age = time.time() - os.path.getmtime(LOCK_FILE)
            if age < 600:  # 10 minutes
                return False
            log_action("Stale lock detected (>10 min old), overriding", "WARNING")
        except OSError:
            pass
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    """Remove the lock file."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Scheduled loop orchestrator")
    parser.add_argument("--phase", choices=["invoices", "cc", "all"], default="all",
                        help="Which phases to run (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load state and log plan without making API calls")
    args = parser.parse_args()

    if not acquire_lock():
        log_action("Another loop run is in progress, exiting", "WARNING")
        sys.exit(0)

    try:
        _run_loop(args)
    finally:
        release_lock()


def _run_loop(args):
    log_action("=" * 60)
    log_action("LOOP RUN START")
    log_action(f"Phase: {args.phase} | Dry run: {args.dry_run}")
    log_action("=" * 60)

    state = load_state()
    errors = []
    phases_run = []

    if args.dry_run:
        log_action(f"State loaded: last_run={state.get('last_run')}")
        log_action(f"  Processed emails: {len(state.get('processed_email_ids', []))}")
        log_action(f"  Processed PDFs: {len(state.get('processed_pdf_files', []))}")
        log_action(f"  CC hashes: {list(state.get('cc_statements_hash', {}).keys())}")
        log_action(f"  Consecutive failures: {state.get('consecutive_failures', 0)}")
        log_action("Dry run — no changes made.")
        save_state(state)
        return

    # === Phase A: Fetch new invoice emails ===
    if args.phase in ("invoices", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase A: Fetch invoice emails")
            fetch_run = _import_script("01_fetch_invoices.py").run
            result = fetch_run(
                since_timestamp=state.get("last_email_check"),
                known_email_ids=set(state.get("processed_email_ids", [])),
                headless=True,
            )
            state["last_email_check"] = result["check_timestamp"]
            state["processed_email_ids"].extend(result["new_email_ids"])
            # Cap at 5000 to prevent unbounded growth
            state["processed_email_ids"] = state["processed_email_ids"][-5000:]
            phases_run.append("fetch_invoices")
            log_action(f"Phase A done: {result['downloaded_count']} new PDFs, {result['skipped_count']} skipped")
        except RuntimeError as e:
            if "token expired" in str(e).lower() or "re-authenticate" in str(e).lower():
                log_action(f"Phase A skipped: {e}", "WARNING")
                log_action("  Run 'python scripts/01_fetch_invoices.py' manually to re-authenticate.", "WARNING")
                # Don't count token expiry as a pipeline error — continue with existing PDFs
            else:
                errors.append(("fetch_invoices", str(e)))
                log_action(f"Phase A FAILED: {e}", "ERROR")
                log_action(traceback.format_exc(), "ERROR")
        except Exception as e:
            errors.append(("fetch_invoices", str(e)))
            log_action(f"Phase A FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # === Phase B: Extract new PDFs ===
    if args.phase in ("invoices", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase B: Extract invoice PDFs")
            extract_run = _import_script("02_extract_invoices.py").run
            result = extract_run(
                already_processed=set(state.get("processed_pdf_files", [])),
            )
            state["processed_pdf_files"].extend(result["newly_processed"])
            # Cap at 5000
            state["processed_pdf_files"] = state["processed_pdf_files"][-5000:]
            phases_run.append("extract_invoices")
            log_action(f"Phase B done: {result['new_count']} new, {result['total_count']} total")
        except Exception as e:
            errors.append(("extract_invoices", str(e)))
            log_action(f"Phase B FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # === Phase C: Create vendors/bills ===
    if args.phase in ("invoices", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase C: Create vendors & bills")
            create_run = _import_script("03_create_vendors_bills.py").run
            result = create_run()
            phases_run.append("create_vendors_bills")
            log_action(f"Phase C done: {result['created_count']} created, {result['skipped_count']} skipped")
        except Exception as e:
            errors.append(("create_vendors_bills", str(e)))
            log_action(f"Phase C FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # === Phase D: Parse CC statements ===
    if args.phase in ("cc", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase D: Parse CC statement PDFs")
            parse_run = _import_script("04_parse_cc_statements.py").run
            result = parse_run(
                known_hashes=state.get("cc_statements_hash", {}),
            )
            state["cc_statements_hash"] = result["new_hashes"]
            phases_run.append("parse_cc_statements")
            if result["has_new_data"]:
                log_action(f"Phase D done: {result['total_transactions']} transactions from {result['cards_parsed']}")
            else:
                log_action("Phase D done: no new CC statement PDFs")
        except Exception as e:
            errors.append(("parse_cc_statements", str(e)))
            log_action(f"Phase D FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # === Phase E: Match bills to CC transactions ===
    if args.phase in ("cc", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase E: Record payments (match bills to CC)")
            payments_run = _import_script("05_record_payments.py").run
            result = payments_run()
            phases_run.append("record_payments")
            log_action(
                f"Phase E done: {result['paid_count']} paid, "
                f"{len(result['still_unmatched_bill_ids'])} unmatched (will retry)"
            )
        except Exception as e:
            errors.append(("record_payments", str(e)))
            log_action(f"Phase E FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # === Phase F: Import CSVs to banking ===
    if args.phase in ("cc", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase F: Import to Zoho Banking")
            import_run = _import_script("06_import_to_banking.py").run
            result = import_run()
            phases_run.append("import_to_banking")
            log_action(f"Phase F done: {result['imported_count']} imported, {result['skipped_count']} skipped")
        except Exception as e:
            errors.append(("import_to_banking", str(e)))
            log_action(f"Phase F FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # === Phase G: Auto-match banking transactions ===
    if args.phase in ("cc", "all"):
        try:
            log_action("-" * 40)
            log_action("Phase G: Auto-match banking transactions")
            match_run = _import_script("07_auto_match.py").run
            result = match_run()
            phases_run.append("auto_match")
            log_action(f"Phase G done: {result['matched_count']} matched, {result['skipped_count']} skipped")
        except Exception as e:
            errors.append(("auto_match", str(e)))
            log_action(f"Phase G FAILED: {e}", "ERROR")
            log_action(traceback.format_exc(), "ERROR")

    # --- Update state ---
    if errors:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    else:
        state["consecutive_failures"] = 0

    state.setdefault("run_history", []).append({
        "timestamp": datetime.now().isoformat(),
        "phases_run": phases_run,
        "errors": [{"phase": p, "error": e} for p, e in errors],
    })
    # Keep last 100 runs
    state["run_history"] = state["run_history"][-100:]

    save_state(state)

    log_action("=" * 60)
    log_action(f"LOOP RUN COMPLETE: {len(phases_run)} phases, {len(errors)} errors")
    if state["consecutive_failures"] > 3:
        log_action(f"WARNING: {state['consecutive_failures']} consecutive failures!", "WARNING")
    log_action("=" * 60)

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
