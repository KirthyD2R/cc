# Workflow Checklist — CC Statement Automation

Use this checklist for each billing cycle.

---

## Phase 0: One-Time Setup
- [ ] Reset password for Invoice@d2railabs.com (Forgot Password)
- [ ] Set up Zoho API credentials in `config/zoho_config.json`
- [ ] Install Python dependencies: `pip install -r requirements.txt`
- [ ] Install Java (needed for tabula-py): `sudo apt install default-jre`
- [ ] Get CC Account IDs from Zoho Books and add to config

---

## Phase 1: PDF → Excel Conversion
- [ ] Collect all 3 CC statement PDFs
- [ ] Place PDFs in `input_pdfs/` folder
- [ ] Run: `python scripts/01_pdf_to_excel.py`
- [ ] **VERIFY:** Open generated Excel files in `output/` and check:
  - [ ] All transactions are captured
  - [ ] Dates are correct (YYYY-MM-DD format)
  - [ ] Amounts are correct (debits vs credits)
  - [ ] Descriptions/merchant names are readable

---

## Phase 2: Import to Zoho Books
- [ ] **API Method:** Run `python scripts/02_zoho_import.py`
- [ ] **OR Manual Method:**
  - [ ] Zoho Books → Banking → Credit Cards
  - [ ] Select Card 1 → Import Statement → Upload Excel
  - [ ] Map columns → Import
  - [ ] Repeat for Card 2 and Card 3
- [ ] **VERIFY:** Check uncategorized transactions appear in Zoho

---

## Phase 3: Match Transactions
- [ ] Run: `python scripts/03_match_transactions.py`
- [ ] Review `output/match_report.xlsx`:
  - [ ] **Matched sheet:** Verify auto-matches are correct
  - [ ] **Unmatched sheet:** Review what needs manual action
  - [ ] **Summary sheet:** Check match rate

---

## Phase 4: Create Vendors & Bills
- [ ] **Preview first:** `python scripts/04_create_vendors_bills.py --dry-run`
- [ ] Review the dry-run output
- [ ] **Execute:** `python scripts/04_create_vendors_bills.py`
- [ ] Check `config/vendor_mappings.json` for new vendor entries
- [ ] **VERIFY in Zoho:** Purchases → Bills — new bills created

---

## Phase 5: Reconciliation
- [ ] Run: `python scripts/05_reconcile.py`
- [ ] **VERIFY in Zoho Books:**
  - [ ] Banking → Credit Cards → Categorized tab
  - [ ] All transactions should be matched
  - [ ] Handle any remaining in Uncategorized tab manually

---

## Phase 6: Invoice Cross-Check
- [ ] Log into Invoice@d2railabs.com
- [ ] Cross-reference invoices from vendors with the bills created
- [ ] Attach actual vendor invoices to bills in Zoho:
  - [ ] Purchases → Bills → Select Bill → Attach file
- [ ] Flag any discrepancies for review

---

## Troubleshooting

| Issue | Solution |
|-------|---------|
| PDF extraction fails | Try different bank parser or use manual Excel entry |
| Zoho API 401 error | Refresh token may have expired — regenerate |
| Vendor not matching | Add mapping to `config/vendor_mappings.json` |
| Amount mismatch | Check for GST/tax differences between CC and invoice |
| Duplicate transactions | Check if statement was already imported in Zoho |
