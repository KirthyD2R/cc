# VS Code Setup Guide — CC Statement Automation (Zoho Books)

## Complete Step-by-Step Setup

---

## STEP 1: Install Prerequisites

### 1.1 Install Python (if not already installed)
- Download Python 3.10+ from [python.org](https://www.python.org/downloads/)
- During installation, **CHECK** ✅ "Add Python to PATH"
- Verify in terminal:
```bash
python --version
# Expected: Python 3.10.x or higher
```

### 1.2 Install Java (required for tabula-py PDF extraction)
- Download Java JDK 11+ from [adoptium.net](https://adoptium.net/)
- Install and verify:
```bash
java -version
# Expected: openjdk version "11.x.x" or higher
```

### 1.3 Install VS Code Extensions
Open VS Code → Extensions (Ctrl+Shift+X) → Install:
- **Python** (by Microsoft)
- **Pylance** (by Microsoft)
- **Python Environment Manager** (optional but helpful)

---

## STEP 2: Create Project Folder

### 2.1 Open Terminal in VS Code
- Press **Ctrl + `** (backtick) to open terminal

### 2.2 Create and Navigate to Project
```bash
# Windows
mkdir C:\Projects\cc-statement-automation
cd C:\Projects\cc-statement-automation

# Mac/Linux
mkdir -p ~/Projects/cc-statement-automation
cd ~/Projects/cc-statement-automation
```

### 2.3 Open in VS Code
```bash
code .
```

---

## STEP 3: Extract the Project ZIP

After downloading the ZIP file provided:

```bash
# If you downloaded the zip, extract it into your project folder
# The folder structure should look like:

cc-statement-automation/
├── README.md
├── requirements.txt
├── .gitignore
├── .env                          ← You will create this
├── config/
│   ├── zoho_config.json
│   └── vendor_mappings.json
├── scripts/
│   ├── utils.py
│   ├── 01_pdf_to_excel.py
│   ├── 02_zoho_import.py
│   ├── 03_match_transactions.py
│   ├── 04_create_vendors_bills.py
│   └── 05_reconcile.py
├── input_pdfs/                   ← Place your 3 PDFs here
├── output/                       ← Generated files appear here
└── docs/
    ├── SETUP_GUIDE.md
    ├── ZOHO_API_SETUP.md
    ├── MANUAL_STEPS.md
    └── WORKFLOW_CHECKLIST.md
```

---

## STEP 4: Set Up Python Virtual Environment

### 4.1 Create Virtual Environment
```bash
# In VS Code terminal (Ctrl + `)
python -m venv venv
```

### 4.2 Activate Virtual Environment

```bash
# Windows (Command Prompt)
venv\Scripts\activate

# Windows (PowerShell)
venv\Scripts\Activate.ps1

# If PowerShell gives execution policy error, run:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
# Then try again:
venv\Scripts\Activate.ps1

# Mac/Linux
source venv/bin/activate
```

You should see `(venv)` at the start of your terminal prompt.

### 4.3 Select Python Interpreter in VS Code
- Press **Ctrl + Shift + P**
- Type: `Python: Select Interpreter`
- Choose the one with `venv` in its path:
  `./venv/Scripts/python.exe` (Windows) or `./venv/bin/python` (Mac/Linux)

---

## STEP 5: Install Dependencies

### 5.1 Install All Required Packages
```bash
# Make sure (venv) is active in terminal
pip install -r requirements.txt
```

### 5.2 Verify Installation
```bash
pip list
```

You should see:
```
tabula-py
pdfplumber
openpyxl
pandas
requests
python-dotenv
fuzzywuzzy
python-Levenshtein
```

### 5.3 If Any Package Fails
```bash
# Install individually
pip install tabula-py
pip install pdfplumber
pip install openpyxl
pip install pandas
pip install requests
pip install python-dotenv
pip install fuzzywuzzy
pip install python-Levenshtein
```

---

## STEP 6: Configure the Project

### 6.1 Create .env File (for sensitive credentials)
Create a new file `.env` in the project root:

```env
# Zoho Books API Credentials
ZOHO_ORG_ID=your_organization_id
ZOHO_CLIENT_ID=your_client_id
ZOHO_CLIENT_SECRET=your_client_secret
ZOHO_REFRESH_TOKEN=your_refresh_token

# Credit Card Account IDs in Zoho Books
CC1_ACCOUNT_ID=account_id_for_card_1
CC2_ACCOUNT_ID=account_id_for_card_2
CC3_ACCOUNT_ID=account_id_for_card_3

# Invoice Email
INVOICE_EMAIL=Invoice@d2railabs.com
```

### 6.2 Update zoho_config.json
Edit `config/zoho_config.json` and fill in your actual values:
- `organization_id` → From Zoho Books → Settings → Organization Profile
- `client_id` & `client_secret` → From Zoho API Console
- `refresh_token` → Generated via OAuth (see docs/ZOHO_API_SETUP.md)
- `zoho_account_id` for each card → From Zoho Books Banking URL

### 6.3 Reset Invoice Email Password
1. Go to email provider for `Invoice@d2railabs.com`
2. Click **"Forgot Password"**
3. Complete the reset process
4. Log in and check for existing vendor invoices

---

## STEP 7: Place Your Credit Card PDFs

Copy your 3 credit card statement PDFs into the `input_pdfs/` folder:

```bash
# Example
cp ~/Downloads/hdfc_statement.pdf input_pdfs/
cp ~/Downloads/icici_statement.pdf input_pdfs/
cp ~/Downloads/sbi_statement.pdf input_pdfs/
```

Or just drag-and-drop them into the `input_pdfs/` folder in VS Code's file explorer.

---

## STEP 8: Run the Scripts (in order)

### ▶️ Script 1: Convert PDFs to Excel
```bash
python scripts/01_pdf_to_excel.py
```
**Expected Output:**
```
[INFO] Found 3 PDF(s) to process
[INFO] Processing: input_pdfs/hdfc_statement.pdf
[INFO] Detected bank: hdfc
[INFO] Extracted 45 transactions from hdfc_statement.pdf
[INFO] Created Zoho Excel: output/hdfc_statement_zoho_20260210_143022.xlsx

✅ Processed 3/3 PDFs successfully
   → output/hdfc_statement_zoho_20260210_143022.xlsx
   → output/icici_statement_zoho_20260210_143024.xlsx
   → output/sbi_statement_zoho_20260210_143025.xlsx
```

**⚠️ VERIFY NOW:** Open Excel files in `output/` and check transactions are correct.

---

### ▶️ Script 2: Import to Zoho Books
```bash
# If Zoho API is configured:
python scripts/02_zoho_import.py

# If API is NOT configured yet, it will show manual steps:
# → Follow the on-screen instructions to import manually in Zoho Books UI
```
**Expected Output (API):**
```
[INFO] Importing hdfc_statement_zoho_20260210.xlsx → Credit Card 1
  ✅ Credit Card 1: Import successful
  ✅ Credit Card 2: Import successful
  ✅ Credit Card 3: Import successful
```

---

### ▶️ Script 3: Match Transactions with Invoices
```bash
python scripts/03_match_transactions.py
```
**Expected Output:**
```
==================================================
Processing: Credit Card 1
==================================================
[INFO] Found 45 uncategorized transactions
[INFO] Processing: 2026-01-05 | AMAZON PAY | ₹2500.0
[INFO]   Vendor match: Amazon India (confidence: 95%)
[INFO]   ✅ Matched with bill: BILL-00142

==================================================
  MATCHING SUMMARY
==================================================
  ✅ Auto-matched:    32
  ⚠️  Needs action:   13
  📊 Report saved:    output/match_report.xlsx
```

**⚠️ REVIEW:** Open `output/match_report.xlsx` and check matches.

---

### ▶️ Script 4: Create Vendors & Bills

```bash
# ALWAYS do a dry run first:
python scripts/04_create_vendors_bills.py --dry-run

# Review the output, then execute:
python scripts/04_create_vendors_bills.py
```
**Expected Output:**
```
==================================================
  VENDOR & BILL CREATION SUMMARY
==================================================
  👤 Vendors created:  5
  📄 Bills created:    13
```

---

### ▶️ Script 5: Reconcile Everything
```bash
python scripts/05_reconcile.py
```
**Expected Output:**
```
==================================================
  RECONCILIATION COMPLETE
==================================================
  ✅ Successfully matched:  45
  ❌ Failed:                0
  📊 Total processed:       45
  📈 Success rate:          100.0%

🎉 Done! Verify in Zoho Books: Banking → Credit Cards → Categorized
```

---

## STEP 9: Verify in Zoho Books

1. Log into [Zoho Books](https://books.zoho.in)
2. Go to **Banking → Credit Cards**
3. Click each card → **Categorized** tab → all matched transactions
4. Check **Purchases → Bills** → new bills created
5. Cross-check with invoices from `Invoice@d2railabs.com`

---

## Run All Scripts at Once (after first-time verification)

Create a runner script or use:

```bash
# Windows
python scripts/01_pdf_to_excel.py && python scripts/02_zoho_import.py && python scripts/03_match_transactions.py && python scripts/04_create_vendors_bills.py && python scripts/05_reconcile.py

# Mac/Linux
python scripts/01_pdf_to_excel.py && \
python scripts/02_zoho_import.py && \
python scripts/03_match_transactions.py && \
python scripts/04_create_vendors_bills.py && \
python scripts/05_reconcile.py
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `python not found` | Use `python3` instead, or reinstall Python with PATH checked |
| `venv activation fails` (PowerShell) | Run: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| `java not found` | Install Java JDK and restart VS Code |
| `tabula-py error` | Ensure Java is installed and in PATH |
| `No transactions extracted` | PDF format not recognized — open PDF, check if it's image-based (needs OCR) |
| `Zoho 401 Unauthorized` | Refresh token expired — regenerate via API Console |
| `Module not found` | Ensure `(venv)` is active, then `pip install -r requirements.txt` |
| `Permission denied` (Linux/Mac) | Use `chmod +x scripts/*.py` |

---

## VS Code Tips

- **Run current file:** Right-click in editor → "Run Python File in Terminal"
- **Debug:** Set breakpoints (click left of line numbers) → F5 to debug
- **Terminal:** Ctrl + ` to toggle terminal
- **Multiple terminals:** Click the + icon in terminal panel
- **File explorer:** Ctrl + Shift + E to view project files
