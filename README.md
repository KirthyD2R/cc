# CC Statement Automation — Zoho Books

Automated pipeline to extract credit card transactions, fetch invoices from Outlook, create bills in Zoho Books, record payments, and auto-match banking transactions.


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
