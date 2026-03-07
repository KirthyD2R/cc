# Manual Steps Reference

These steps require human intervention and cannot be fully automated.

---

## 1. Email Password Reset (One-time)
**Email:** Invoice@d2railabs.com

1. Go to the email provider's login page
2. Click **"Forgot Password"**
3. Follow the reset flow (may need lead's help if recovery email/phone is different)
4. Set a new password and log in
5. Check for existing vendor invoices that need to be matched

---

## 2. Verifying PDF Extraction (Every cycle)

After running `01_pdf_to_excel.py`, manually verify:

- Open each generated Excel file in `output/`
- Check for:
  - **Missing transactions** — compare row count with PDF statement summary
  - **Garbled text** — PDF parsing sometimes corrupts merchant names
  - **Wrong amounts** — especially for international transactions with conversion
  - **Date errors** — especially year rollover (Dec → Jan)
- Fix any issues directly in the Excel file before importing

---

## 3. Vendor Matching Review (Every cycle)

After running `03_match_transactions.py`:

- Open `output/match_report.xlsx`
- **Matched sheet:** Spot-check 5-10 entries for correctness
- **Unmatched sheet:** For each entry, decide:
  - Is this a known vendor with a different name? → Add to `vendor_mappings.json`
  - Is this a new vendor? → Script 04 will create it
  - Is this a personal expense? → Handle separately

---

## 4. Invoice Cross-Referencing

For each CC transaction, you should ideally:

1. Find the actual invoice in Invoice@d2railabs.com inbox
2. Verify the invoice amount matches the CC charge
3. Attach the invoice to the bill in Zoho Books:
   - Purchases → Bills → Select the bill → Attach Files
4. Note any discrepancies (GST differences, partial payments, etc.)

---

## 5. Zoho Books UI Steps (if not using API)

### Import Statement Manually
1. Banking → Credit Cards → Select card
2. Click **"Import Statement"** (top right)
3. Choose file → Upload Excel from `output/`
4. Column mapping:
   | Excel Column | Zoho Field |
   |---|---|
   | Transaction Date | Date |
   | Description | Description |
   | Withdrawals | Debit Amount |
   | Deposits | Credit Amount |
   | Payee | Payee |
5. Click **Import**

### Match Transaction to Bill Manually
1. Banking → Credit Cards → Select card → **Uncategorized** tab
2. Click on a transaction
3. Click **"Match"** tab
4. Search for the corresponding bill
5. Select and confirm the match

### Create Vendor Manually
1. Contacts → Vendors → **+ New Vendor**
2. Fill in vendor name and details
3. Save

### Create Bill Manually
1. Purchases → Bills → **+ New Bill**
2. Select vendor
3. Add line items matching the invoice
4. Save
