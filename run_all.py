"""
Run All Scripts — CC Statement Automation

Pipeline (7 scripts, 2 phases):

  Phase 1: Invoice → Bills (Steps 1-3)
    01. Fetch invoice PDFs from Outlook inbox
    02. Extract vendor/amount/date from PDFs
    03. Create vendors & bills in Zoho Books

  Phase 2: CC Statements → Payments → Banking → Match (Steps 4-7)
    04. Parse CC statement PDFs → CSV + JSON
    05. Record payments (actual INR from CC, zero forex diff)
    06. Import CSVs to Zoho Banking (uncategorized)
    07. Auto-match uncategorized transactions to paid bills

Usage:
    python run_all.py                  # Interactive (pauses between steps)
    python run_all.py --auto           # No pauses
    python run_all.py --phase 1        # Run only Phase 1
    python run_all.py --phase 2        # Run only Phase 2
    python run_all.py --from 5         # Start from step 5
"""

import subprocess
import sys
import os
import argparse


def run_script(script_name):
    """Run a Python script and return success status."""
    print(f"\n{'='*60}")
    print(f"  RUNNING: {script_name}")
    print(f"{'='*60}\n")
    result = subprocess.run(
        [sys.executable, script_name],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    return result.returncode == 0


def pause(message="Press Enter to continue (or 'q' to quit)..."):
    response = input(f"\n  {message} ")
    if response.strip().lower() == "q":
        print("\n  Stopped by user.")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Run CC statement automation pipeline")
    parser.add_argument("--auto", action="store_true", help="Skip verification pauses")
    parser.add_argument("--phase", type=int, choices=[1, 2], help="Run only Phase 1 or 2")
    parser.add_argument("--from", dest="from_step", type=int, help="Start from step N")
    parser.add_argument("--fail-fast", action="store_true", help="Stop pipeline on first step failure")
    args = parser.parse_args()

    print("""
    ============================================================
      CC STATEMENT AUTOMATION - ZOHO BOOKS
      Pipeline: 2 Phases, 7 Scripts
    ============================================================
    """)

    all_scripts = [
        # Phase 1: Invoice → Bills
        {
            "num": 1, "phase": 1,
            "file": "scripts/01_fetch_invoices.py",
            "name": "Fetch invoice PDFs from Outlook",
            "verify": "Check input_pdfs/invoices/ for downloaded PDFs",
        },
        {
            "num": 2, "phase": 1,
            "file": "scripts/02_extract_invoices.py",
            "name": "Extract invoice details from PDFs",
            "verify": "Check output/extracted_invoices.json",
        },
        {
            "num": 3, "phase": 1,
            "file": "scripts/03_create_vendors_bills.py",
            "name": "Create vendors & bills in Zoho",
            "verify": "Zoho -> Purchases -> Bills (check new entries)",
        },
        # Phase 2: CC Statements → Payments → Banking → Match
        {
            "num": 4, "phase": 2,
            "file": "scripts/04_parse_cc_statements.py",
            "name": "Parse CC statement PDFs -> CSV + JSON",
            "verify": "Check output/ for *_transactions.csv and cc_transactions.json",
        },
        {
            "num": 5, "phase": 2,
            "file": "scripts/05_record_payments.py",
            "name": "Record payments (actual INR from CC)",
            "verify": "Zoho -> Purchases -> Bills should show PAID status",
        },
        {
            "num": 6, "phase": 2,
            "file": "scripts/06_import_to_banking.py",
            "name": "Import to Zoho Banking (uncategorized)",
            "verify": "Zoho -> Banking -> CC accounts -> Uncategorized tab",
        },
        {
            "num": 7, "phase": 2,
            "file": "scripts/07_auto_match.py",
            "name": "Auto-match transactions to paid bills",
            "verify": "Zoho -> Banking -> CC accounts -> Categorized tab",
        },
    ]

    # Filter
    scripts = all_scripts
    if args.phase:
        scripts = [s for s in all_scripts if s["phase"] == args.phase]
    if args.from_step:
        scripts = [s for s in scripts if s["num"] >= args.from_step]

    phase_names = {1: "Invoice -> Bills", 2: "CC Statements -> Payments -> Banking -> Match"}
    current_phase = None

    for i, script in enumerate(scripts):
        if script["phase"] != current_phase:
            current_phase = script["phase"]
            print(f"\n{'#'*60}")
            print(f"  PHASE {current_phase}: {phase_names[current_phase]}")
            print(f"{'#'*60}")

        print(f"\n  Step {script['num']}: {script['name']}")
        success = run_script(script["file"])

        if not success:
            print(f"\n  Step {script['num']} FAILED!")
            print(f"  Check output/automation.log for details.")
            # Issue #33: --fail-fast stops pipeline on first failure
            if args.fail_fast:
                print("  --fail-fast: Stopping pipeline.")
                sys.exit(1)
            if not args.auto:
                pause("Press Enter to continue anyway, or 'q' to quit...")
            else:
                print("  Continuing in auto mode...")

        if not args.auto and i < len(scripts) - 1:
            print(f"\n  Step {script['num']} completed.")
            print(f"  VERIFY: {script['verify']}")
            pause()

    print(f"""
    ============================================================
      ALL DONE!
    ============================================================

      Final Verification:
      1. Zoho Books → Banking → CC accounts → Categorized
      2. Zoho Books → Purchases → Bills (check PAID status)
      3. Run: python scripts/list_bills.py
    ============================================================
    """)


if __name__ == "__main__":
    main()
