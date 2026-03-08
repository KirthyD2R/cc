# Possible Duplicate Detection — Design

## Problem

Invoices that already exist in Zoho but have different bill numbers show as "New Bill" in the bill picker UI. Users must manually identify duplicates, risking double-creation.

## Solution

Add a 4th action type `possible_duplicate` to the match-preview classification. When an extracted invoice's **vendor + date + amount (±1% or ±₹1)** matches a Zoho bill but the bill number doesn't match, classify it as "Possible Duplicate" — blocked from creation, with the matched Zoho bill number shown.

## Backend — Match Preview Classification

### Updated Priority Flow

1. Exact bill number → `skip` (In Zoho)
2. Normalized bill number → `skip` (In Zoho)
3. Vendor+date fallback (unreliable numbers only) → `skip` (In Zoho)
4. **Vendor + date + amount (±1% or ±₹1) → `possible_duplicate`**
5. Vendor exists → `new_bill`
6. Vendor not found → `new_vendor_bill`

### Index & Matching

- Build index: `(zoho_vendor_lower, date)` → list of `{bill_info, amount}`
- Duplicate check runs **after** vendor matching (GSTIN/name/fuzzy), using the resolved Zoho vendor name
- Amount tolerance: `abs(local - zoho) <= max(1.0, zoho * 0.01)`
- Multiple matches: pick closest amount match; prefer exact

### Entry Fields

```json
{
  "action": "possible_duplicate",
  "matched_bill_number": "2295334101",
  "matched_bill_id": "2050328000002211657",
  "matched_vendor_name": "AWS",
  "matched_vendor_id": "2050328000002211179",
  "match_type": "vendor_date_amount"
}
```

### Scope

- Match-preview endpoint only (`/api/bills/match-preview`)
- `_check_in_zoho` (invoice list page) unchanged for now

## UI Changes

### New Column: "Invoice #"

- Position: after Vendor, before Date
- Shows extracted `invoice_number` field (e.g., `AWS-2295334101`)

### New Column: "Zoho Bill #"

- Position: after Zoho Vendor
- Shows matched Zoho bill number for `skip` and `possible_duplicate` rows
- Empty for `new_bill` / `new_vendor_bill`

### Final Column Order

☐ | Vendor | Invoice # | Date | Amount | Status | Match | Zoho Vendor | Zoho Bill # | Action

### Status Badge

- "Possible Duplicate" — orange/amber styling
- Blocked: no checkbox, no Create button (same behavior as `skip`)

### Summary Bar

- New counter: "Possible Duplicate: N" with orange dot
- Position: between "In Zoho" and "Existing Vendor"

### Filter Dropdown

- Add `possible_duplicate` option to Status filter

## Edge Cases

- **Multiple Zoho matches:** Pick closest amount; prefer exact
- **Already matched by bill number:** Skip takes priority, no duplicate check
- **Vendor not matched (new_vendor_bill):** Cannot check duplicates — stays as new_vendor_bill
