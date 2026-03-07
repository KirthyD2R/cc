# Setup Guide — CC Statement Automation

## Pre-requisites Checklist

### 1. Email Access
- [ ] Go to **Invoice@d2railabs.com** mail provider
- [ ] Click **"Forgot Password"** and reset the password
- [ ] Log in and verify access to invoice emails
- [ ] Note: This inbox contains vendor invoices that need to be matched with CC transactions

### 2. Zoho Books Access
- [ ] Verify you have admin/accountant access to Zoho Books
- [ ] Navigate to **Banking → Credit Cards** and confirm all 3 credit cards are listed
- [ ] Note the **Account IDs** for each credit card (found in the URL when you click on a card)

### 3. Zoho API Setup (for automation)

#### Option A: Use Zoho's API Console (Recommended)
1. Go to [Zoho API Console](https://api-console.zoho.in/)
2. Create a **Self Client** application
3. Generate tokens with these scopes:
   ```
   ZohoBooks.fullaccess.all
   ```
4. Note down:
   - **Client ID**
   - **Client Secret**
   - **Refresh Token**
5. Add these to `config/zoho_config.json`

#### Option B: Manual Process (No API)
If API setup is not feasible, you can:
1. Run Script 01 to convert PDFs → Excel
2. Manually import Excel files into Zoho Books
3. Manually match transactions in the UI

### 4. Python Environment
```bash
# Verify Python 3.8+
python3 --version

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# OR
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# For tabula-py, you also need Java
java -version  # If missing: sudo apt install default-jre
```

### 5. Credit Card PDFs
- [ ] Obtain all 3 credit card statement PDFs
- [ ] Place them in the `input_pdfs/` folder
- [ ] Name them descriptively (e.g., `hdfc_jan2026.pdf`, `icici_jan2026.pdf`)

---

## Configuration

### config/zoho_config.json
Fill in ALL fields:

| Field | Where to find it |
|-------|-----------------|
| `organization_id` | Zoho Books → Settings → Organization Profile |
| `client_id` | Zoho API Console → Self Client |
| `client_secret` | Zoho API Console → Self Client |
| `refresh_token` | Generated via OAuth flow |
| `zoho_account_id` (per card) | Banking → Credit Cards → click card → ID in URL |

### config/vendor_mappings.json
- Pre-populated with common vendors
- Add your specific vendor mappings as you process statements
- Format: `"CC_STATEMENT_MERCHANT_NAME": "Zoho_Vendor_Name"`

---

## Execution Order

```
Step 1: python scripts/01_pdf_to_excel.py      # Convert PDFs
Step 2: python scripts/02_zoho_import.py        # Import to Zoho
Step 3: python scripts/03_match_transactions.py  # Auto-match
Step 4: python scripts/04_create_vendors_bills.py --dry-run  # Preview
Step 4b: python scripts/04_create_vendors_bills.py            # Execute
Step 5: python scripts/05_reconcile.py           # Final reconciliation
```

### After Automation
1. Log into Zoho Books
2. Go to **Banking → Credit Cards**
3. Check the **Categorized** tab — all matched transactions should be here
4. Check **Uncategorized** tab — handle any remaining items manually
5. Verify bills under **Purchases → Bills**
