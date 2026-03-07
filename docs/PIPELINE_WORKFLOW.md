# CC Statement Automation — Pipeline Workflow

End-to-end automation for processing vendor invoices and credit card statements into Zoho Books. The pipeline fetches invoices from Outlook, extracts data from PDFs, creates bills, records payments, imports banking transactions, and auto-matches everything in Zoho Books.

---

## Architecture Overview

```
                         ┌─────────────────────────────────┐
                         │         PHASE 1                  │
                         │     Invoice → Bills              │
                         │                                  │
  Outlook Inbox ──────►  │  Step 1: Fetch Invoice PDFs      │
                         │           │                      │
  input_pdfs/invoices/ ◄─┤           ▼                      │
                         │  Step 2: Extract Invoice Data     │
                         │           │                      │
  extracted_invoices.json◄┤           ▼                      │
                         │  Step 3: Create Vendors & Bills   │──► Zoho Books
                         │                                  │    (Bills)
                         └─────────────────────────────────┘

                         ┌─────────────────────────────────┐
                         │         PHASE 2                  │
                         │  CC Statements → Payments →      │
                         │  Banking → Match                 │
                         │                                  │
  input_pdfs/cc_statements/►Step 4: Parse CC Statements     │
                         │           │                      │
  *_transactions.csv  ◄──┤           ▼                      │
  cc_transactions.json◄──┤  Step 5: Import to Banking       │──► Zoho Books
                         │           │                      │    (Banking)
                         │           ▼                      │
                         │  Step 6: Record Payments ◄──Zoho │──► Zoho Books
                         │  (bills+banking direct from Zoho)│    (Payments)
                         │           ▼                      │
                         │  Step 7: Auto-Match Transactions │──► Zoho Books
                         │                                  │    (Categorized)
                         └─────────────────────────────────┘
```

---

## Data Flow

```
Outlook Inbox
    │
    ▼
input_pdfs/invoices/*.pdf ────► output/extracted_invoices.json ────► Zoho Books Bills
                                                                        │
input_pdfs/cc_statements/*.pdf ──► output/*_transactions.csv ──────►  Banking (Uncategorized)
                                   output/cc_transactions.json (ref)          │
                                                                               │
                         Zoho Books (unpaid bills)  ──────────────► Payments (bills → PAID)
                         Zoho Banking (uncategorized CC txns) ─────►           │
                                                                               │
                                                                    Auto-Match (Categorized)
```

---

## Step-by-Step Reference

---

### Step 1: Fetch Invoice PDFs from Outlook

**Script:** `scripts/01_fetch_invoices.py`

**Purpose:** Download PDF invoice attachments from the Outlook inbox using Microsoft Graph API.

**Internal step-by-step logic:**

1. **Load config** — reads `config/outlook_config.json` for Azure AD credentials and search period
2. **Authenticate to Microsoft Graph**
   - Check `config/outlook_token.json` for a cached access token
   - If token exists and not expired (with 5-minute buffer): use it directly
   - If token expired but has a refresh token: `POST` to Azure token endpoint with `grant_type=refresh_token` to get a new access token silently
   - If refresh fails or no token exists:
     - **Interactive mode:** build OAuth2 authorization URL, start a local HTTP server on `localhost:8080`, open browser for Microsoft login, capture the authorization code from the redirect callback, exchange code for tokens via `POST /oauth2/v2.0/token`
     - **Headless mode** (scheduled runs): raise `RuntimeError` — manual re-auth needed
   - Cache the new token (access + refresh) to `config/outlook_token.json` with timestamp
3. **Query inbox for emails with attachments**
   - Call `GET /me/mailFolders/Inbox/messages` with OData filter:
     - `hasAttachments eq true`
     - `receivedDateTime ge {from}` and `le {to}` (default: last 60 days)
   - Select fields: `id, subject, receivedDateTime, from`
   - Fetch up to 100 per page, follow `@odata.nextLink` for pagination
4. **Filter out already-processed emails** — skip any message ID already in `known_email_ids` set (from loop state)
5. **Download attachments from each email**
   - Call `GET /me/messages/{id}/attachments`
   - For each attachment:
     - **PDF files:** base64-decode `contentBytes`, sanitize filename (replace `/` and `\`), skip if file already exists on disk, write to `input_pdfs/invoices/`
     - **ZIP files:** base64-decode to temp file, open with `zipfile`, extract any `.pdf` entries inside, skip existing files, write extracted PDFs to `input_pdfs/invoices/`
     - Skip non-PDF/non-ZIP attachments
6. **Return results** — `check_timestamp`, list of new email IDs, download count, skip count

**Authentication flow:**
```
Cached token valid?  ──yes──►  Use directly
       │ no
       ▼
Refresh token available?  ──yes──►  POST /token (refresh_token grant)
       │ no                              │ success ──►  Cache & use
       ▼                                 │ fail
Interactive mode?  ──no──►  RAISE ERROR (manual re-auth needed)
       │ yes
       ▼
Open browser ──►  User logs in ──►  Redirect to localhost:8080
       │
       ▼
Capture auth code ──►  POST /token (authorization_code grant)
       │
       ▼
Cache tokens ──►  Return access_token
```

**Config:** `config/outlook_config.json`
- `tenant_id`, `client_id`, `client_secret` — Azure AD app registration
- `redirect_uri` — local callback URL (default: `http://localhost:8080/callback`)
- `scopes` — Microsoft Graph permissions (`Mail.Read`, `User.Read`)
- `search_period.from` / `search_period.to` — date range filter

**Input:** Outlook inbox emails with PDF/ZIP attachments

**Output:** `input_pdfs/invoices/*.pdf`

---

### Step 2: Extract Invoice Details from PDFs

**Script:** `scripts/02_extract_invoices.py`

**Purpose:** Parse downloaded invoice PDFs and extract structured data (vendor, amount, date, currency, invoice number).

**Internal step-by-step logic:**

1. **Scan input directory** — list all `.pdf` files in `input_pdfs/invoices/`
2. **Filter out receipts** — skip filenames starting with `Receipt-` or containing `-receipt-` (dedup: prefer Invoice over Receipt for the same vendor/amount)
3. **Load existing extractions** (incremental mode)
   - Read `output/extracted_invoices.json` if it exists
   - Build a set of already-extracted filenames to skip
4. **For each new PDF file:**
   - **a. Extract text** — open PDF with `pdfplumber`, extract text from every page, concatenate
   - **b. OCR fallback** — if extracted text is < 50 characters (scanned PDF), try `pytesseract` + `pdf2image` to OCR all pages (optional dependency)
   - **c. Detect vendor** — scan the first 2000 chars (uppercased) for vendor keywords:
     ```
     "ATLASSIAN" → Atlassian
     "AMAZON WEB SERVICES" → Amazon Web Services
     "GITHUB, INC" → GitHub
     "ANTHROPIC" → Anthropic
     "GOOGLE INDIA PRIVATE LIMITED" → Google
     ... (20+ vendor keywords)
     ```
   - **d. Route to vendor-specific extractor** — each extractor uses regex patterns tailored to that vendor's invoice layout:

     | Vendor | Invoice # Pattern | Date Pattern | Amount Pattern |
     |--------|-------------------|--------------|----------------|
     | Atlassian | `Invoice number: IN-XXX-XXX` | `Invoice date: Mon DD, YYYY` | `Invoice Total: USD XX.XX` |
     | AWS | `Statement Number: XXXXXXX` | `Statement Date: Mon DD, YYYY` | `TOTAL AMOUNT DUE BY ... INR XX,XXX.XX` |
     | GitHub (Invoice) | `Invoice # INVXXXXXXXXX` | `Invoice Date Mon DD, YYYY` | `INVOICE TOTAL: $XX.XX` |
     | GitHub (Receipt) | `Transaction ID XXXXX` | `Date YYYY-MM-DD` | `Total $X.XX USD` |
     | Google | `Invoice number: XXXXXXXXXX` | `Invoice date DD Mon YYYY` | `Total in INR ₹X,XXX.XX` or `Total in USD` |
     | New Relic | `INV01XXXXXX` | `Invoice Date: Mon DD, YYYY` | `Total Due: $XX.XX` |
     | Stripe-based (Anthropic, Vercel, Wispr Flow, Gamma) | `Invoice number XXXXXXXX XXXX` | `Date of issue Mon DD, YYYY` | `Amount due $XX.XX USD` or `₹X,XXX.XX` |
     | Microsoft | Filename as invoice number | `Statement Date: DD/MM/YYYY` | `Total Charges INR XX,XXX.XX` |
     | Info Edge (Naukri) | Filename as invoice number | `Document Date DD-Mon-YYYY` | `Gross Amount/Total XX,XXX.XX` |
     | NSTP | `NSTP/XX-XX/XXXXX` from text | `DtDD-Mon-YY` from filename | `Total Invoice Value Including GST: X,XX,XXX.XX` |

   - **e. Generic fallback** — if no vendor detected, try common regex patterns: `Invoice #`, `Date of issue`, `Amount Due`, `Grand Total`, `$XX.XX`, `₹XX.XX`
   - **f. Vendor fallback detection** — for unrecognized PDFs, scan the first 15 lines for company names with suffixes (Pvt Ltd, Inc, LLC, etc.) that also have address/GSTIN evidence within 8 lines nearby. Rejects OCR garbage, date lines, and generic header text
   - **g. Build result object** — `{file, vendor_name, invoice_number, date, amount, currency, raw_text_preview}`
5. **Deduplicate by invoice number** — if two PDFs have the same non-generic invoice number, keep only the first. Generic numbers like "payment", "original", "invoice" bypass this check
6. **Merge with existing** — append new extractions to existing `extracted_invoices.json`
7. **Write output** — save combined results to `output/extracted_invoices.json`
8. **Organize PDFs by month** — copy invoices to `organized_invoices/<Mon YYYY>/` folders
   - Deduplicates by invoice_number within each month: if two files share the same invoice_number (e.g., `Invoice-PZLFLIEU-0002.pdf` and `Invoice-PZLFLIEU-0002 (1).pdf`), only the first is copied to the organized folder — the OS-duplicate `(1)` file is skipped

**Output schema:**
```json
{
  "file": "Invoice-AEEHRQ-00001.pdf",
  "path": "d:/.../input_pdfs/invoices/Invoice-AEEHRQ-00001.pdf",
  "vendor_name": "Anthropic",
  "invoice_number": "AEEHRQ0001",
  "date": "2025-06-01",
  "amount": 200.0,
  "currency": "USD",
  "raw_text_preview": "Invoice number AEEHRQ 0001..."
}
```

**Input:** `input_pdfs/invoices/*.pdf`

**Output:** `output/extracted_invoices.json`

---

### Step 3: Create Vendors & Bills in Zoho Books

**Script:** `scripts/03_create_vendors_bills.py`

**Purpose:** For each extracted invoice, find or create the vendor in Zoho Books and create a corresponding bill with the invoice PDF attached.

**Internal step-by-step logic:**

1. **Initialize**
   - Load `config/zoho_config.json` and `config/vendor_mappings.json`
   - Create `ZohoBooksAPI` client (handles OAuth2 token refresh automatically)
   - Fetch expense accounts from Zoho → build name-to-ID map
   - Initialize `VendorCategorizer` for intelligent account assignment
   - Fetch currency map from Zoho (currency_code → currency_id)
   - Load extracted invoices from `output/extracted_invoices.json`
2. **Build dedup index from Zoho**
   - Paginate through ALL existing bills in Zoho via `GET /bills`
   - Build three dedup structures:
     - `existing_bills`: bill_number → bill_id map
     - `existing_bill_ids`: set of all bill IDs (to verify local results still exist)
     - `existing_vendor_date`: set of `(vendor_name, date)` pairs
3. **Load local results** — read `output/created_bills.json` if exists, but only trust entries whose `bill_id` still exists in Zoho (re-creates deleted bills)
4. **For each extracted invoice:**
   - **a. Skip if already processed** — check local results by filename
   - **b. Dedup by bill number** — check if `INV-{invoice_number}` exists in Zoho
   - **c. Dedup by vendor+date** (fallback only) — only applied when the invoice has **no reliable invoice_number** (filename used as bill_number fallback). If vendor+date pair already exists in Zoho AND the invoice has no unique invoice_number, skip it. When the invoice has a real invoice_number, two bills from the same vendor on the same date with different invoice IDs are treated as legitimate different bills and are not skipped. Also checks the fuzzy-mapped vendor name (e.g., "CLAUDE.AI SUBSCRIPTION" → "Anthropic")
   - **d. Resolve vendor** (`ensure_vendor`):
     1. Sanitize vendor name (strip special characters Zoho rejects)
     2. Exact match: `GET /contacts?contact_name={name}`
     3. Fuzzy match: compare against `vendor_mappings.json` using `fuzzywuzzy` scoring
     4. Create new vendor: `POST /contacts` with company name, GST treatment, currency, billing address from `vendor_details` config
     5. If Zoho rejects GST fields → retry without them
   - **e. Determine expense account** (`VendorCategorizer`):
     1. Check `account_mappings` in vendor_mappings.json for vendor-specific account
     2. Fall back to `default_expense_account` ("Credit Card Charges")
     3. Fall back to first available expense account
   - **f. Create bill** — `POST /bills` with:
     ```json
     {
       "vendor_id": "<resolved>",
       "bill_number": "INV-<invoice_number>",
       "date": "<invoice_date>",
       "due_date": "<invoice_date>",
       "currency_id": "<from_currency_map>",
       "line_items": [{
         "account_id": "<expense_account>",
         "description": "Invoice: <filename>",
         "rate": "<amount>",
         "quantity": 1
       }]
     }
     ```
     If Zoho returns "already been used" → skip (idempotent)
   - **g. Attach PDF** — `POST /bills/{bill_id}/attachment` with the original invoice PDF file
   - **h. Rate limit** — `sleep(0.3)` between API calls
5. **Save results** — write all results (created, skipped, failed) to `output/created_bills.json`

**Output schema** (`created_bills.json` entry):
```json
{
  "file": "Invoice-AEEHRQ-00001.pdf",
  "status": "created",
  "vendor_name": "Anthropic",
  "vendor_id": "356883300000XXXXX",
  "bill_id": "356883300000XXXXX",
  "amount": 200.0,
  "currency": "USD",
  "attached": true
}
```

**Input:** `output/extracted_invoices.json`

**Output:**
- `output/created_bills.json`
- Zoho Books: new vendors and bills created with PDF attachments

---

### Step 4: Parse CC Statement PDFs

**Script:** `scripts/04_parse_cc_statements.py`

**Purpose:** Parse credit card statement PDFs from multiple banks and export transactions as CSV (for Zoho Banking import) and JSON (for payment matching).

**Internal step-by-step logic:**

1. **Load card config** — read `credit_cards` array from `zoho_config.json`
2. **Resolve Zoho account IDs** — if any card has account name instead of ID, look it up via Zoho API
3. **For each credit card:**
   - **a. Discover PDFs** — find all statement PDFs for this card:
     1. Glob `input_pdfs/cc_statements/{pdf_pattern}.pdf`
     2. Scan all PDFs in the directory for bank name substring (e.g., "Kotak" in filename)
     3. Scan for card alias substring (e.g., "Mayura" from card name "Mayura CC 9677")
     4. Fuzzy match: compare first N chars of filename to bank/alias name, allow 1-2 character typos (catches "Kotack" for "Kotak")
     5. Add the explicitly configured `pdf_file` if not already matched
     6. Sort and deduplicate paths
   - **b. Filter by selection** — if `selected_files` provided, keep only matching PDFs
   - **c. Change detection** — compute combined MD5 hash of all matched PDFs. If hash matches `known_hashes[card_name]` from previous run, skip this card entirely
   - **d. Build password list** — ordered: UI-provided password → config `pdf_password` → `last_four_digits` → card name → bank name → empty string
   - **e. Parse each PDF** using the bank-specific parser:

     **HDFC parser** (`parse_hdfc`):
     - Open PDF (try passwords if encrypted)
     - For each page, extract text, scan each line
     - Match regex: `DD/MM/YYYY [| HH:MM] DESCRIPTION [+] C AMOUNT`
       - `C` = rupee symbol (appears in PDF as literal "C")
       - `+` before `C` = credit (refund) → negate amount
       - Time `HH:MM` is optional (some rows like opening balance omit it)
     - Clean description: remove `(Ref# ...)` suffix

     **Kotak parser** (`parse_kotak`):
     - Open PDF, extract text per page
     - Try regex: `DD Mon YYYY DESCRIPTION AMOUNT [CR|Dr]`
     - Fallback regex: `DD/MM/YYYY DESCRIPTION AMOUNT [CR|Dr]`
     - `CR` suffix → negate amount (credit/refund)

     **IDFC FIRST / Mayura parser** (`parse_idfc_first`):
     - Open PDF, collect ALL lines across all pages
     - Process lines sequentially with a state machine:
       - Buffer non-date lines as potential description prefixes (`desc_before`)
       - When a date line is found: `DD/MM/YYYY [text] AMOUNT DR|CR`
         - Build description from: buffered lines + middle text + next continuation line (if multi-line transaction)
         - Clean description: strip "Convert" tag and "USD XX.XX" forex amounts
       - `CR` → negate amount, `DR` → positive amount
     - Skip header/footer lines (Statement Date, Page, Reward Points, etc.)

   - **f. Table fallback** — if bank parser returns 0 transactions, try `parse_tables()`:
     - Extract tabular data from PDF using `pdfplumber.extract_tables()`
     - For each row: identify date column, description columns, and amount column (last numeric cell)
     - Build transactions from tabular data
   - **g. Handle password failures** — if PDF is password-protected and all passwords fail, log error and track in `password_failed_files`
   - **h. Deduplicate** — across all PDFs for the same card, remove transactions with identical `(date, description, amount)` tuples
4. **Add card metadata** — annotate each transaction with `card_name` and `zoho_account_id`
5. **Export CSV** — per card: `output/<CardName>_transactions.csv`
   - Fields: `date, description, amount`
   - Amounts negated: charges become negative (money out), credits become positive (money back)
6. **Export JSON** — combined: `output/cc_transactions.json` with card metadata attached

**Config:** `zoho_config.json` → `credit_cards` array:
```json
{
  "name": "HDFC CC 8948",
  "bank": "HDFC",
  "pdf_file": "HDFC-CC-Jan2026.pdf",
  "pdf_pattern": "HDFC*",
  "pdf_password": "1234",
  "last_four_digits": "8948",
  "zoho_account_id": "356883300000XXXXX"
}
```

**Input:** `input_pdfs/cc_statements/*.pdf`

**Output:**
- `output/<CardName>_transactions.csv` — per-card CSV for Zoho Banking import
- `output/cc_transactions.json` — combined JSON with card metadata for Step 6

---

### Step 5: Import CC Transactions to Zoho Banking

**Script:** `scripts/06_import_to_banking.py`

**Purpose:** Import credit card transaction CSVs into Zoho Books Banking module as uncategorized transactions.

**Internal step-by-step logic:**

1. **Initialize**
   - Load `config/zoho_config.json`
   - Create `ZohoBooksAPI` client
   - Resolve CC account IDs for all configured cards
   - Load tracking file `output/imported_statements.json` (previous import hashes)
2. **For each configured credit card:**
   - **a. Skip if not selected** — when `selected_cards` is provided, only process matching cards
   - **b. Locate CSV** — `output/<CardName>_transactions.csv` (underscores replace spaces)
   - **c. Compute CSV hash** — MD5 of the entire CSV file
   - **d. Check if already imported** — compare hash to previously recorded hash in tracking file
     - Same hash → skip (no new data)
     - Different hash → log "CSV changed, re-importing"
   - **e. Read CSV** — parse each row into Zoho transaction format:
     ```json
     {
       "date": "2025-12-15",
       "debit_or_credit": "debit",
       "amount": 5000.00,
       "description": "AMAZONWEBSERVICESC BANGALORE"
     }
     ```
     - Negative amounts → `"debit"` (charges on card)
     - Positive amounts → `"credit"` (refunds/reversals)
   - **f. Build import payload** — compute date range from all transaction dates:
     ```json
     {
       "account_id": "<CC card's Zoho account ID>",
       "start_date": "<earliest date>",
       "end_date": "<latest date>",
       "transactions": [...]
     }
     ```
   - **g. Import via API** — `POST /bankstatements` with JSON payload
   - **h. Update tracking** — record card name, account ID, transaction count, date range, and CSV hash
3. **Save tracking file** — write `output/imported_statements.json`

**After import:** Transactions appear in Zoho Books → Banking → CC Account → **Uncategorized** tab.

**Input:** `output/<CardName>_transactions.csv`

**Output:**
- `output/imported_statements.json` — tracking file with import metadata and hashes
- Zoho Books Banking: uncategorized transactions in CC accounts

---

### Step 6: Record Vendor Payments

**Script:** `scripts/05_record_payments.py`

**Purpose:** Close bills as paid by matching each bill to its actual credit card transaction and recording a vendor payment in Zoho Books with the correct INR amount and exchange rate.

**Internal step-by-step logic:**

1. **Initialize**
   - Load `config/zoho_config.json` and `config/vendor_mappings.json`
   - Create `ZohoBooksAPI` client
   - Resolve CC account IDs for all configured cards
   - Fetch currency map from Zoho (currency_code → currency_id)
2. **Fetch unpaid bills from Zoho** (source of truth — not local JSON)
   - `GET /bills?status=unpaid` and `GET /bills?status=overdue` with full pagination
   - Deduplicate by `bill_id`
   - Covers ALL unpaid bills in Zoho, including bills created manually or outside the current pipeline session
3. **Fetch CC transactions from Zoho Banking** (source of truth — not local JSON)
   - For each CC account: `GET /bankaccounts/{account_id}/transactions?status=uncategorized`
   - Paginate through all pages until `has_more_page` is false
   - Parse forex tags from Zoho descriptions: `[USD 359.90]` → `forex_amount=359.90`, `forex_currency="USD"`
4. **Build vendor-to-merchant reverse lookup**
   - Invert `vendor_mappings.json` `mappings` dict: vendor name → list of CC merchant keywords
   - Example: `"Anthropic" → ["claude.ai subscription", "claude.aisubscription", ...]`
   - Add vendor aliases (e.g., "Anthropic" ↔ "Anthropic (Claude AI)")
5. **Load existing payments** — read `output/recorded_payments.json` as skip-cache only: entries with `status == "paid"` are skipped this session (source of truth is Zoho — paid bills won't appear in step 2 results)
6. **For each unpaid bill:**
   - **a. Skip if already paid this session** — check against skip-cache from `recorded_payments.json`
   - **b. Fetch full bill from Zoho** — `GET /bills/{bill_id}` to get accurate `date`, `total`, and `currency`
   - **c. Find matching CC transaction** (`find_cc_transaction`):

     **Matching algorithm:**
     - Build keyword list: vendor_to_merchants keywords + vendor name + vendor name without spaces
     - For each uncategorized CC transaction (skip credits, skip already-used indices):
       1. **Vendor keyword match** — check if any keyword appears in CC description (substring match OR normalized/spaceless match for concatenated CC descriptions like `AMAZONWEBSERVICESC`)
       2. **Amount match:**
          - **INR bills:** 3% tolerance (or minimum ₹10), score = 150 minus percentage difference
          - **USD bills, strategy 0:** forex tag `[USD XX.XX]` in Zoho description → exact currency+amount match (score 400, highest confidence)
          - **USD bills, strategy 1:** extract `USD XX.XX` from CC description → exact match (score 300)
          - **USD bills, strategy 2:** estimate INR as `USD × 86`, check if CC amount falls within `USD × 75` to `USD × 100` range → score based on closeness to estimate
       3. **Date filter** — CC transaction must be within ±5 days of bill date. Closer date = higher score bonus (+10 per day closer)
       4. **Best match wins** — highest score above threshold (50)
     - Return: `{inr_amount, card_name, zoho_account_id, txn_date, description}` and the matched index

   - **d. Skip if no match** — bill gets status `"unmatched"` (retried on next run when new CC data may be available)
   - **e. Build payment** — construct `POST /vendorpayments` payload:
     ```json
     {
       "vendor_id": "<from bill>",
       "payment_mode": "Credit Card",
       "date": "<CC transaction date>",
       "amount": "<bill total>",
       "paid_through_account_id": "<CC card's Zoho account ID>",
       "bills": [{"bill_id": "<bill_id>", "amount_applied": "<bill total>"}]
     }
     ```
   - **f. Handle USD exchange rate** — for USD bills:
     - `exact_rate = cc_inr_amount / bill_usd_total`
     - Iterate decimal precision from 6 to 11 until `round(rate × total, 2) == round(inr, 2)`
     - Add `currency_id` (USD) and `exchange_rate` to payment data
   - **g. Record payment** — `POST /vendorpayments` to Zoho
     - On success: bill becomes **PAID**
     - On "already been paid" error: mark as paid, skip
   - **h. Rate limit** — `sleep(0.3)` between API calls
7. **Save results** — write all payment results to `output/recorded_payments.json` (skip-cache only — Zoho is source of truth)
8. **Report** — log unmatched USD bills and unmatched bill IDs (will retry next run)

**Matching flow:**
```
For each unpaid bill:
  │
  ├─ Fetch bill details from Zoho (date, total, currency)
  │
  ├─ For each CC transaction:
  │    ├─ Skip if already used by another bill
  │    ├─ Skip if credit (amount ≤ 0)
  │    ├─ Vendor keyword match? ──no──► skip
  │    │         │ yes
  │    ├─ INR: amount within 3%?  ──no──► skip
  │    │  USD S0: forex tag [USD XX]? exact match → score 400
  │    │  USD S1: USD XX in desc? exact match → score 300
  │    │  USD S2: INR in 75-100× range? → score 100-150
  │    │  No strategy matched? ──────────────────────no──► skip
  │    │         │ yes
  │    ├─ Date within ±5 days? ──no──► skip
  │    │         │ yes
  │    └─ Score it (amount closeness + date proximity)
  │
  ├─ Best score ≥ 50? ──no──► status = "unmatched" (retry later)
  │         │ yes
  ├─ Record payment in Zoho with CC account + exchange rate
  └─ Mark CC transaction as used
```

**Input:**
- Zoho Books: all unpaid/overdue bills (fetched directly — not from local JSON)
- Zoho Books Banking: all uncategorized CC transactions (fetched directly — not from local JSON)

**Output:**
- `output/recorded_payments.json` — skip-cache only (prevents same-session re-processing; Zoho is source of truth)
- Zoho Books: bills marked as PAID with payment linked to CC account

---

### Step 7: Auto-Match Banking Transactions

**Script:** `scripts/07_auto_match.py`

**Purpose:** Automatically match uncategorized banking transactions (imported in Step 5) to their corresponding vendor payments (recorded in Step 6), moving them from Uncategorized to Categorized in Zoho.

**Internal step-by-step logic:**

1. **Initialize**
   - Load `config/zoho_config.json`
   - Create `ZohoBooksAPI` client
   - Resolve CC account IDs for all configured cards
2. **For each CC account** (`auto_match_account`):
   - **a. Fetch all uncategorized transactions**
     - `GET /bankaccounts/{account_id}/transactions?status=uncategorized`
     - Paginate through all pages until `has_more_page` is false
   - **b. For each uncategorized transaction:**
     1. **Get Zoho's suggested matches** — `GET /banktransactions/{txn_id}/matching` returns candidates (vendor payments, journal entries, etc. that could match this banking transaction)
     2. **Handle cross-currency amounts** (`_get_comparable_amount`):
        - Prefer `bcy_amount` (base currency amount = INR for Indian org)
        - Fallback to `bcy_debit`, `bcy_credit`, or other base-currency fields
        - If candidate is in a foreign currency (e.g., USD): check if the ratio of banking amount / candidate amount is in the 60-110 range (exchange rate heuristic) → treat as compatible and let Zoho API validate
     3. **Log diagnostic fields** — on first candidate, log available Zoho fields for debugging
     4. **Rank candidates** (`_rank_candidates`):
        - Calculate percentage difference between each candidate's comparable amount and the banking transaction amount
        - Calculate date penalty: exact date match = -1, within 5 days = 0, far = +1
        - Sort by (date_penalty, pct_diff, abs_diff)
        - Apply confidence threshold based on candidate count:

          | Candidates | Strategy | Threshold |
          |-----------|----------|-----------|
          | 1 | Trust Zoho's suggestion | Try directly |
          | 2-5 | Try best 2 | Within 10% (fallback: best within 20% for cross-currency) |
          | 6+ | Try best 3 | Within 5% (fallback: best 2 within 10%) |

     5. **Try matching** (`_try_match`):
        - For each ranked candidate, call `POST /banktransactions/{txn_id}/match`:
          ```json
          [{"transaction_id": "<candidate_id>", "transaction_type": "vendor_payment"}]
          ```
        - On success → matched, move to next transaction
        - On "total amount does not match" → log amount diagnostics (candidate amount, bcy_amount, currency, banking amount) and try next candidate
        - On "already matched/categorized" → treat as success
        - On other error → stop trying for this transaction
     6. **Log results** — for matched: show payee, amount, date. For unmatched: show what was tried with comparable amounts and diffs
     7. **Rate limit** — `sleep(0.3)` between transactions
3. **Report totals** — matched count and skipped count across all cards

**Matching flow:**
```
For each uncategorized banking transaction:
  │
  ├─ GET /banktransactions/{id}/matching → candidates[]
  │
  ├─ No candidates? ──► skip
  │
  ├─ For each candidate:
  │    ├─ Get comparable amount (bcy_amount or cross-currency heuristic)
  │    ├─ Calculate % difference from banking amount
  │    └─ Calculate date penalty
  │
  ├─ Rank by (date_penalty, %diff, abs_diff)
  │
  ├─ Apply threshold filter:
  │    ├─ 1 candidate: try it
  │    ├─ 2-5: best 2 within 10% (fallback 20%)
  │    └─ 6+: best 3 within 5% (fallback 10%)
  │
  ├─ No candidates pass threshold? ──► skip
  │
  └─ Try each ranked candidate:
       ├─ POST /banktransactions/{id}/match
       ├─ Success ──► matched ✓
       ├─ Amount mismatch ──► try next candidate
       └─ Other error ──► stop
```

**Input:** Zoho Books uncategorized banking transactions

**Output:** Zoho Books: banking transactions matched and categorized

---

## Running the Pipeline

### Full Pipeline (Interactive)

```bash
python run_all.py
```

Runs all 7 steps with verification pauses between each step.

### Full Pipeline (Automated)

```bash
python run_all.py --auto
```

Runs all steps without pauses.

### Run by Phase

```bash
python run_all.py --phase 1    # Invoice → Bills (Steps 1-3)
python run_all.py --phase 2    # CC Statements → Match (Steps 4-7)
```

### Start from a Specific Step

```bash
python run_all.py --from 5     # Start from Step 5
```

### Fail-Fast Mode

```bash
python run_all.py --auto --fail-fast    # Stop on first failure
```

### Individual Steps

```bash
python scripts/01_fetch_invoices.py
python scripts/02_extract_invoices.py
python scripts/03_create_vendors_bills.py
python scripts/04_parse_cc_statements.py
python scripts/05_record_payments.py
python scripts/06_import_to_banking.py
python scripts/07_auto_match.py
```

### Scheduled Loop (Windows Task Scheduler)

```bash
python run_loop.py                    # Run all phases incrementally
python run_loop.py --phase invoices   # Phase A-C only
python run_loop.py --phase cc         # Phase D-G only
python run_loop.py --dry-run          # Preview without API calls
```

The loop orchestrator:
- Runs incrementally (only processes new data since last run)
- Maintains state in `output/loop_state.json` (processed emails, PDF hashes, etc.)
- Uses file locking to prevent concurrent runs
- Tracks consecutive failures for alerting
- Designed for 10-15 minute scheduling intervals

---

## Configuration Files

| File | Purpose |
|------|---------|
| `config/zoho_config.json` | Zoho Books API credentials, organization ID, credit card definitions |
| `config/outlook_config.json` | Microsoft Graph API credentials, search period |
| `config/outlook_token.json` | Cached Outlook OAuth token (auto-managed) |
| `config/vendor_mappings.json` | CC merchant → vendor name mappings, vendor details, expense account assignments |
| `config/self_client.json` | Zoho Self Client credentials |
| `config/tokens.json` | Zoho OAuth tokens |

---

## Output Files

| File | Created By | Used By |
|------|-----------|---------|
| `input_pdfs/invoices/*.pdf` | Step 1 | Step 2, Step 3 (attachment) |
| `output/extracted_invoices.json` | Step 2 | Step 3 |
| `output/created_bills.json` | Step 3 | Step 3 (attachment lookup) |
| `input_pdfs/cc_statements/*.pdf` | Manual upload | Step 4 |
| `output/<Card>_transactions.csv` | Step 4 | Step 5 |
| `output/cc_transactions.json` | Step 4 | — (Step 6 reads from Zoho directly) |
| `output/recorded_payments.json` | Step 6 | Step 6 (skip-cache only) |
| `output/imported_statements.json` | Step 5 | Step 5 (dedup) |
| `output/loop_state.json` | run_loop.py | run_loop.py |
| `output/automation.log` | All steps | Debugging |

---

## Verification Checklist

After running the full pipeline:

| Step | What to Check |
|------|--------------|
| 1 | `input_pdfs/invoices/` has downloaded PDF files |
| 2 | `output/extracted_invoices.json` has vendor, amount, date for each invoice |
| 3 | Zoho Books → Purchases → Bills shows new entries |
| 4 | `output/` has `*_transactions.csv` and `cc_transactions.json` |
| 5 | Zoho Books → Banking → CC accounts → **Uncategorized** tab has transactions |
| 6 | Zoho Books → Purchases → Bills shows **PAID** status |
| 7 | Zoho Books → Banking → CC accounts → **Categorized** tab has matched transactions |

---

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `scripts/list_bills.py` | List all bills in Zoho Books |
| `scripts/delete_bills.py` | Delete bills from Zoho (cleanup) |
| `scripts/delete_banking_transactions.py` | Delete banking transactions (cleanup) |
| `scripts/cleanup_all.py` | Full cleanup of all created data |
| `scripts/update_bill_accounts.py` | Update expense accounts on existing bills |
| `scripts/organize_outlook.py` | Organize processed emails in Outlook |

---

## Dependencies

```
pdfplumber          — PDF text extraction
requests            — HTTP client for APIs
fuzzywuzzy          — Fuzzy string matching for vendor names
python-Levenshtein  — Fast string similarity (fuzzywuzzy backend)
flask               — Web UI dashboard
pytesseract         — OCR fallback for scanned PDFs (optional)
pdf2image           — PDF to image conversion for OCR (optional)
```

Install: `pip install -r requirements.txt`

---

## Supported Banks

| Bank | Card Format | Notes |
|------|------------|-------|
| HDFC | `DD/MM/YYYY \| HH:MM DESC C AMOUNT` | `C` = rupee symbol, `+` prefix = credit |
| Kotak | `DD Mon YYYY DESC AMOUNT CR/Dr` | Also supports `DD/MM/YYYY` format |
| IDFC FIRST (Mayura) | `DD/MM/YYYY DESC AMOUNT DR/CR` | Multi-line descriptions, password-protected |

---

## Supported Vendors (Auto-Detected)

Atlassian, Amazon Web Services, Amazon India, Anthropic, GitHub, Google, Groq, Hyperbrowser AI, Info Edge (Naukri), LinkedIn, Microsoft, Netflix, New Relic, NSTP, S2 Labs, Supabase, Vercel, Windsurf, Wispr Flow, Zoho

Unrecognized vendors are detected via company suffix patterns (Pvt Ltd, Inc, LLC, etc.) with address/GSTIN evidence nearby.

---

## Troubleshooting

| Issue | Solution |
|-------|---------|
| Outlook token expired | Run `python scripts/01_fetch_invoices.py` manually to re-authenticate |
| PDF password error | Add `pdf_password` to the card config in `zoho_config.json` |
| Vendor not detected | Add keyword to `detect_vendor()` in `02_extract_invoices.py` |
| CC merchant not mapped | Add entry to `mappings` in `config/vendor_mappings.json` |
| Bill already exists | Normal — pipeline is idempotent, skips duplicates |
| Amount mismatch in Step 7 | Multi-currency rounding; Step 7 tries multiple candidates |
| No CC match for bill | Bill stays "unmatched" and retries on next `run_loop.py` run |
| Zoho rate limit | Built-in 300ms delays; reduce batch size if needed |
