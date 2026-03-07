# CC Statement Automation — Requirements & Features Document

What has been built, why, and how each requirement is fulfilled.

---

## Project Summary

A Python automation system that bridges **Outlook email**, **PDF invoice/statement parsing**, and **Zoho Books accounting** — eliminating the manual work of downloading invoices, creating bills, recording credit card payments, and reconciling banking transactions.

**Target user:** Finance/accounting team at D2R AI Labs Pvt Ltd (India-based company using Zoho Books with INR base currency and multiple credit cards).

---

## Functional Requirements

### FR-1: Automated Invoice Ingestion from Email

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Automatically download vendor invoice PDFs from the company Outlook inbox |
| **Implementation** | `scripts/01_fetch_invoices.py` |
| **API** | Microsoft Graph API v1.0 (`/me/mailFolders/Inbox/messages`) |
| **Auth** | OAuth2 Authorization Code Flow with refresh token support |
| **Features delivered** | |
| | Fetches emails with attachments within configurable date range (default: last 60 days) |
| | Downloads PDF attachments directly |
| | Extracts PDFs from ZIP attachments (bundled invoices) |
| | Skips already-downloaded files (filename-based dedup) |
| | Headless mode for scheduled/unattended runs (refresh token only, no browser) |
| | Automatic token refresh with fallback to interactive re-auth |
| | Pagination support (handles inboxes with 100+ matching emails) |
| | Incremental fetching — tracks processed email IDs across runs |

---

### FR-2: Invoice Data Extraction from PDFs

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Extract vendor name, amount, date, currency, and invoice number from downloaded PDF invoices |
| **Implementation** | `scripts/02_extract_invoices.py` |
| **PDF library** | `pdfplumber` for text extraction, `pytesseract` + `pdf2image` as OCR fallback |
| **Features delivered** | |
| | 10 vendor-specific parsers with tailored regex patterns (Atlassian, AWS, GitHub, Google, Anthropic, Vercel, Microsoft, New Relic, Naukri, NSTP) |
| | Stripe-style parser covers multiple vendors (Anthropic, Vercel, Wispr Flow, Gamma) |
| | Generic fallback parser for unrecognized invoice formats |
| | Vendor fallback detection via company suffix patterns (Pvt Ltd, Inc, LLC) with address/GSTIN evidence |
| | Receipt dedup — skips `Receipt-*` and `*-receipt-*` files (prefers Invoice over Receipt for same vendor) |
| | Invoice number dedup — prevents duplicate extractions across runs |
| | Organize-step dedup — when copying to `organized_invoices/<Mon YYYY>/`, skips files with the same invoice_number already copied in that month (prevents OS-duplicate `(1)` files from polluting the organized folder) |
| | OCR fallback for scanned PDFs (when text extraction yields < 50 characters) |
| | Multi-currency support (INR, USD) with automatic currency detection |
| | Incremental processing — only extracts new PDFs not already in output |

**Supported vendors:**
Atlassian, Amazon Web Services, Anthropic, Gamma, GitHub (Invoice + Receipt), Google, Groq, Hyperbrowser AI, Info Edge (Naukri), LinkedIn, Microsoft, Netflix, New Relic, NSTP, S2 Labs, Supabase, Vercel, Windsurf, Wispr Flow, Zoho

---

### FR-3: Vendor & Bill Creation in Zoho Books

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Create vendor contacts and bills in Zoho Books for each extracted invoice, with the original PDF attached |
| **Implementation** | `scripts/03_create_vendors_bills.py` |
| **API** | Zoho Books REST API (`/contacts`, `/bills`, `/bills/{id}/attachment`) |
| **Features delivered** | |
| | Three-tier vendor resolution: exact match → fuzzy match (fuzzywuzzy) → create new |
| | Vendor details from config: company name, GST treatment, billing address, currency |
| | Intelligent expense account assignment via `VendorCategorizer` (vendor → account mapping) |
| | Bill dedup via three strategies: (1) bill number — primary, always applied; (2) vendor+date pair — secondary fallback, only when invoice has no reliable invoice_number (avoids false skips for legitimate different bills from same vendor on same date); (3) local results cross-check |
| | Detects bills deleted from Zoho and re-creates them |
| | PDF attachment to each created bill |
| | GST field retry — if Zoho rejects GST fields, retries without them |
| | Rate limiting (300ms between API calls) |

**Vendor mappings:** `config/vendor_mappings.json` provides:
- `mappings` — 70+ CC merchant name → clean vendor name translations
- `vendor_details` — 20+ vendor profiles with company name, GST treatment, currency, billing address
- `account_mappings` — vendor → expense account assignment (Software Subscriptions, Legal & Professional Fees, Recruitment & Hiring, etc.)

---

### FR-4: Credit Card Statement Parsing

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Parse credit card statement PDFs from multiple Indian banks and export structured transaction data |
| **Implementation** | `scripts/04_parse_cc_statements.py` |
| **Features delivered** | |
| | Three bank-specific parsers: HDFC, Kotak, IDFC FIRST (Mayura) |
| | Auto-discovery of statement PDFs via pattern matching, substring matching, and fuzzy filename matching (handles typos like "Kotack" for "Kotak") |
| | Password-protected PDF support (tries configured password, last 4 digits, card name, bank name) |
| | Table-based extraction fallback when regex parsers find nothing |
| | MD5 hash change detection — skips unchanged PDFs across runs |
| | Cross-PDF dedup — removes duplicate transactions when multiple monthly statements overlap |
| | Dual output: per-card CSV (for Zoho Banking import) + combined JSON (for payment matching) |
| | Correct sign convention: charges negative, credits/refunds positive in CSV |
| | Multi-line transaction support (IDFC FIRST parser handles descriptions that wrap across lines) |
| | International transaction handling (strips "Convert" tags and USD forex amounts from descriptions) |

**Supported banks:**

| Bank | Parser | Statement format |
|------|--------|-----------------|
| HDFC | `parse_hdfc` | `DD/MM/YYYY [| HH:MM] DESCRIPTION [+] C AMOUNT` |
| Kotak | `parse_kotak` | `DD Mon YYYY DESCRIPTION AMOUNT [CR|Dr]` or `DD/MM/YYYY` variant |
| IDFC FIRST | `parse_idfc_first` | `DD/MM/YYYY DESCRIPTION AMOUNT DR|CR` with multi-line support |

---

### FR-5: Banking Transaction Import

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Import CC statement transactions into Zoho Books Banking module as uncategorized transactions |
| **Implementation** | `scripts/06_import_to_banking.py` |
| **API** | Zoho Books REST API (`POST /bankstatements`) |
| **Features delivered** | |
| | Reads per-card CSVs and converts to Zoho bankstatements JSON format |
| | Correct debit/credit classification (negative = debit/charge, positive = credit/refund) |
| | Auto-computes date range from transaction dates |
| | CSV hash tracking — prevents duplicate imports when re-running |
| | Re-imports when CSV content changes (new statement data detected) |
| | Per-card import to correct Zoho CC account ID |

---

### FR-6: Vendor Payment Recording

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Match each Zoho bill to its actual credit card transaction and record a vendor payment that closes the bill as PAID |
| **Implementation** | `scripts/05_record_payments.py` |
| **API** | Zoho Books REST API (`/bills?status=unpaid`, `/bills?status=overdue`, `/bankaccounts/{id}/transactions?status=uncategorized`, `/bills/{id}`, `/vendorpayments`) |
| **Architecture** | **Zoho-first** — fetches ALL unpaid/overdue bills and ALL uncategorized CC transactions directly from Zoho on each run; does not depend on local output from Steps 3 or 4 |
| **Features delivered** | |
| | Zoho-first bill source: `GET /bills?status=unpaid` + `GET /bills?status=overdue` (paginated) — covers ALL unpaid bills, including manually created ones |
| | Zoho-first CC transaction source: `GET /bankaccounts/{id}/transactions?status=uncategorized` (paginated) — reads live Zoho Banking data |
| | Forex tag parsing: extracts `[USD 359.90]` appended to Zoho banking descriptions → `forex_amount`, `forex_currency` for Strategy 0 matching |
| | Bill-to-CC transaction matching using vendor keywords + amount + date |
| | INR matching: 3% amount tolerance (minimum Rs 10), ±5 day date window |
| | USD matching strategy 0: forex tag `[USD XX.XX]` in Zoho banking description → exact currency+amount match (score 400, highest confidence) |
| | USD matching strategy 1: extract USD amount from CC description (e.g., "ATLASSIAN USD 223.07") for exact match (score 300) |
| | USD matching strategy 2: estimate INR from USD × ~86 with sanity bounds (75-100 INR/USD range) (score 100-150) |
| | Exchange rate precision: iterates decimal precision from 6 to 11 digits until `rate × total` rounds correctly (avoids 1-3 Rs mismatch in banking) |
| | Payment date uses CC transaction date (actual charge date), not bill date |
| | Vendor keyword matching handles concatenated CC descriptions (e.g., "AMAZONWEBSERVICESC", "CLAUDE.AISUBSCRIPTION") via normalized spaceless comparison |
| | Scoring system: amount closeness + date proximity, best candidate wins above threshold (score >= 50) |
| | One-to-one matching — tracks used CC transaction indices to prevent double-matching |
| | Unmatched bills get status "unmatched" (not "failed") — retried on next run when new CC data may be available |
| | "Already paid" handling — gracefully skips bills Zoho reports as already paid |
| | `recorded_payments.json` is skip-cache only (prevents same-session re-processing); Zoho is the source of truth |

---

### FR-7: Automatic Transaction Matching

| Attribute | Detail |
|-----------|--------|
| **Requirement** | Auto-match uncategorized banking transactions to their corresponding vendor payments in Zoho |
| **Implementation** | `scripts/07_auto_match.py` |
| **API** | Zoho Books REST API (`/banktransactions/{id}/matching`, `/banktransactions/{id}/match`) |
| **Features delivered** | |
| | Fetches uncategorized transactions with pagination |
| | Uses Zoho's suggested match candidates as starting point |
| | Cross-currency amount handling: uses `bcy_amount` (base currency INR) when candidates are in USD |
| | Exchange rate heuristic (60-110 ratio) for detecting cross-currency candidates without `bcy_amount` |
| | Confidence-based ranking with tiered thresholds (1 candidate: trust, 2-5: 10% tolerance, 6+: 5% tolerance) |
| | Fallback thresholds for cross-currency rounding (20% for 2-5 candidates, 10% for 6+) |
| | Cascading match attempts — on "amount mismatch" error, tries next ranked candidate |
| | Diagnostic logging: logs candidate fields, amounts, currencies, and diffs for debugging |
| | Rate limiting (300ms between transactions) |

---

## Non-Functional Requirements

### NFR-1: Idempotency

Every step is safe to re-run:
- Step 1: skips already-downloaded PDFs (filename check)
- Step 2: skips already-extracted PDFs (incremental mode)
- Step 3: deduplicates by bill number and vendor+date against Zoho
- Step 4: skips unchanged PDFs (MD5 hash check)
- Step 5: skips unchanged CSVs (MD5 hash check)
- Step 6: skips already-paid bills (Zoho only returns unpaid/overdue; `recorded_payments.json` guards same-session re-processing)
- Step 7: handles "already categorized" responses gracefully

### NFR-2: Incremental Processing

The `run_loop.py` orchestrator supports incremental runs:
- Tracks processed email IDs, PDF filenames, and CC statement hashes in `output/loop_state.json`
- Only processes new/changed data since last run
- Retries unmatched bills on each run (new CC data may resolve them)
- State capped at 5000 entries to prevent unbounded growth

### NFR-3: Scheduled Automation

- `run_loop.py` designed for Windows Task Scheduler (10-15 minute intervals)
- `run_scheduled.bat` wrapper for Task Scheduler
- File-based locking to prevent concurrent runs (10-minute stale lock timeout)
- Headless OAuth mode (refresh token only, no browser popup)
- Consecutive failure tracking with alerting threshold

### NFR-4: Rate Limit Compliance

- 300ms delay between Zoho API calls in bulk operations (Steps 3, 6, 7)
- Zoho Books API rate limits respected via built-in pacing

### NFR-5: Multi-Currency Support

- INR (Indian Rupee) as base currency
- USD (US Dollar) for international vendors
- Automatic currency detection from invoice content
- Exchange rate calculation with high precision (up to 10 decimal places)
- Cross-currency matching in both payment recording and banking auto-match

### NFR-6: Error Resilience

- Each step handles errors independently (pipeline continues past failures)
- Failed vendor creation retries without GST fields
- Unmatched bills retry on next run (not marked as permanent failures)
- Token expiry handled gracefully (logs warning, continues with existing data)
- `--fail-fast` mode available for strict runs

### NFR-7: Observability

- All steps log to `output/automation.log` via shared `log_action()` utility
- Web UI dashboard (`app.py`) with live SSE log streaming
- Step-by-step result summaries (downloaded count, created count, matched count, etc.)
- Diagnostic logging in auto-match (candidate fields, amounts, currencies, diffs)
- Run history tracking in loop state (last 100 runs with phases and errors)

---

## Execution Modes

| Mode | Command | Description |
|------|---------|-------------|
| Interactive pipeline | `python run_all.py` | All 7 steps with verification pauses |
| Automated pipeline | `python run_all.py --auto` | All steps, no pauses |
| Phase 1 only | `python run_all.py --phase 1` | Steps 1-3 (Invoice → Bills) |
| Phase 2 only | `python run_all.py --phase 2` | Steps 4-7 (CC → Match) |
| Start from step N | `python run_all.py --from 5` | Resume from a specific step |
| Fail-fast | `python run_all.py --auto --fail-fast` | Stop on first failure |
| Scheduled loop | `python run_loop.py` | Incremental, all phases |
| Loop (invoices only) | `python run_loop.py --phase invoices` | Phases A-C |
| Loop (CC only) | `python run_loop.py --phase cc` | Phases D-G |
| Dry run | `python run_loop.py --dry-run` | Preview state, no API calls |
| Web dashboard | `python app.py` | Browser UI at localhost:5000 |
| Individual step | `python scripts/0X_*.py` | Run any single step standalone |

---

## Web UI Dashboard

**File:** `app.py` (single-file Flask app with embedded HTML/CSS/JS)

| Feature | Description |
|---------|-------------|
| Step cards | Visual cards for each pipeline step with run/status controls |
| Run All | One-click to run the entire pipeline sequentially |
| Live logs | Server-Sent Events (SSE) stream for real-time log output |
| Step results | Shows success/error status with result summaries per step |
| CC statement selector | UI for selecting which CC statement PDFs to parse |
| PDF password input | Prompt for password-protected CC statement PDFs |
| Account review | Interactive panel to review and fix expense account assignments on bills |
| Phase grouping | Steps grouped into Phase 1 (Invoice → Bills) and Phase 2 (CC → Match) |

---

## External Integrations

| System | API | Auth Method | Usage |
|--------|-----|------------|-------|
| Microsoft Outlook | Graph API v1.0 | OAuth2 Authorization Code + refresh token | Fetch invoice emails and PDF attachments |
| Zoho Books | REST API v3 | OAuth2 Client Credentials with refresh token | Vendors, bills, payments, banking, matching |
| Zoho Accounts | OAuth2 token endpoint | Region-aware (`.in`, `.com`, `.eu`, etc.) | Token refresh for API access |

---

## Configuration

### `config/zoho_config.json`
- Zoho Books API credentials (client ID, secret, refresh token)
- Organization ID and base URL (region-aware)
- Credit card definitions (name, bank, PDF file/pattern, password, Zoho account ID)

### `config/outlook_config.json`
- Azure AD app credentials (tenant ID, client ID, secret)
- Redirect URI, scopes, search period

### `config/vendor_mappings.json`
- **70+ merchant-to-vendor mappings** — translates CC statement merchant names to clean Zoho vendor names
- **20+ vendor profiles** — company name, GST treatment, currency code, billing/shipping address, website
- **15+ account mappings** — vendor to expense account assignments (Software Subscriptions, Software Licenses, Legal & Professional Fees, Recruitment & Hiring, AI & ML Services, Web Hosting, etc.)
- Default expense account and tax treatment settings

---

## Utility & Admin Scripts

| Script | Purpose |
|--------|---------|
| `scripts/list_bills.py` | List all bills in Zoho Books (audit) |
| `scripts/delete_bills.py` | Delete bills from Zoho (cleanup/reset) |
| `scripts/delete_banking_transactions.py` | Delete banking transactions (cleanup/reset) |
| `scripts/cleanup_all.py` | Full cleanup of all created data in Zoho |
| `scripts/update_bill_accounts.py` | Bulk update expense accounts on existing bills |
| `scripts/organize_outlook.py` | Move/organize processed emails in Outlook |
| `get_refresh_token.py` | Obtain initial Zoho OAuth refresh token |

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.x |
| PDF parsing | pdfplumber 0.11.4 |
| OCR fallback | pytesseract + pdf2image (optional) |
| HTTP client | requests 2.32.3 |
| Fuzzy matching | fuzzywuzzy 0.18.0 + python-Levenshtein 0.25.1 |
| Web dashboard | Flask 3.1.0 |
| Scheduling | Windows Task Scheduler + `run_scheduled.bat` |
| Version control | Git |

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Vendor-specific regex parsers over generic AI extraction | Deterministic, fast, no API cost; each vendor has a stable PDF format |
| Fuzzy vendor matching via fuzzywuzzy | Handles CC merchant name variations (abbreviations, city suffixes, concatenation) |
| Two-phase pipeline (Invoice → Bills, CC → Match) | Phases are independent; can run invoices without CC statements and vice versa |
| CSV for banking import (not direct API creation) | Zoho's bankstatements API accepts bulk JSON import; CSV is the intermediate format for auditability |
| Exchange rate precision iteration (6-11 decimals) | Zoho's `rate × total` must equal exact INR to avoid 1-3 Rs rounding mismatches in banking reconciliation |
| Unmatched = retry (not failure) | CC statements arrive on different schedules than invoices; an unmatched bill today may match next week |
| Zoho-first for Step 6 (payment recording) | Fetches bills and CC transactions directly from Zoho instead of local JSON files — eliminates stale cache issues, handles bills created outside the current pipeline run (e.g., manually in Zoho), and ensures payment matching always operates on current Zoho state |
| MD5 hash change detection | Avoid re-processing unchanged PDFs/CSVs; supports incremental scheduled runs |
| Single-file Flask dashboard | Zero-config deployment; no separate frontend build step; embedded HTML/CSS/JS |
| 300ms inter-request delay | Zoho Books rate limits (varies by plan); 300ms provides margin without sacrificing throughput |
