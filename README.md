# CC Statement Automation — Zoho Books

Automated pipeline to extract credit card transactions, fetch invoices from Outlook, create bills in Zoho Books, record payments, and auto-match banking transactions.

## Quick Setup

```bash
# 1. Clone/extract project, open in VS Code
cd C:\Projects\cc-statement-automation
code .

# 2. Create & activate virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
#    - config/zoho_config.json     → Zoho Books API credentials
#    - config/outlook_config.json  → Outlook OAuth (Azure app) credentials

# 5. Place CC statement PDFs in input_pdfs/

# 6. Run the full pipeline
python run_all.py              # Interactive (pauses for verification)
python run_all.py --auto       # No pauses
```

## Pipeline Overview (3 Phases, 12 Scripts)

### Phase 1: CC Statement → Zoho Banking (Scripts 01-06)
```
CC PDFs → Parse → Excel/CSV → Import to Zoho → Match → Create Bills → Categorize
```

### Phase 2: Invoice Emails → Zoho Bills (Scripts 07-10)
```
Outlook Inbox → Fetch PDFs → Extract Details → Create Bills with Attachments
```

### Phase 3: Payments & Reconciliation (Scripts 11-12)
```
Scan Receipts for Card Info → Record Payments → Auto-Match Banking Transactions
```

## Step-by-Step

### Phase 1: CC Statements
```bash
python scripts/01_pdf_to_excel.py              # Parse CC PDFs → Excel/CSV
python scripts/02_zoho_import.py               # Import to Zoho Banking
python scripts/03_match_transactions.py        # Match with existing data
python scripts/04_create_vendors_bills.py      # Create vendors & bills
python scripts/05_reconcile.py                 # Reconcile transactions
python scripts/06_categorize_expenses.py       # Categorize expenses
```

### Phase 2: Invoice Extraction
```bash
python scripts/07_outlook_invoices.py          # Fetch invoice PDFs from Outlook
python scripts/08_delete_bills.py --dry-run    # (Optional) Delete bills for re-run
python scripts/09_extract_invoices.py          # Extract vendor/amount/date from PDFs
python scripts/10_create_from_invoices.py      # Create bills in Zoho with PDF attached
python scripts/list_bills.py                   # View all created bills
```

### Phase 3: Payments & Match
```bash
python scripts/11_record_payments.py --scan    # Scan for card last-4 digits
python scripts/11_record_payments.py --dry-run # Preview payments
python scripts/11_record_payments.py           # Record vendor payments
python scripts/12_auto_match.py --dry-run      # Preview banking matches
python scripts/12_auto_match.py                # Auto-match transactions
```

## Credit Card Accounts

| Card | Last 4 | Zoho Account ID |
|------|--------|-----------------|
| HDFC CC | 8948 | 3369633000000043025 |
| Kotak CC | 9157 | 3369633000000043033 |
| Mayura CC (IDFC) | 9677 | 3369633000000043041 |

## Notes

- **Currency**: Bills created in PDF currency. USD bills + INR CC → vendor payment with exchange rate
- **Dates**: DD/MM preferred for Indian invoices
- **Dedup**: Script 10 skips duplicates by (vendor, amount, date)
- **AWS**: Extracts currency from "Total" line (INR if both USD/INR present)
- **NK09/NON_STP**: Require manual vendor identification