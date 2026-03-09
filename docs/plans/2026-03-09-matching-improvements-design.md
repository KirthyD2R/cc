# Matching Algorithm Improvements Design

## Problem

Chrome-based validation of the Record Payment page (2026-03-09) identified:
- **16 matched**, **18 Amex matched**, **165 bills with no CC**, **181 CC txns unmatched**
- 9 correct matches scoring low (73-76%) due to crude USD→INR estimation
- 83 CC txns resolve to a known vendor but fail amount/date gate with no explanation
- Special characters in Kotak card descriptions as a latent risk
- Multi-invoice vendors (Amazon, Flipkart) where one CC txn maps to multiple bills

## Scope: 5 Fixes

### Fix 1: Normalize CC Description in Substring Matching

**What**: In `_resolve_vendor`, the lowercased substring loop (lines 92-94) checks `key in dl` where `dl` is just `.lower()`. Kotak descriptions contain non-printable chars (□). Apply `_norm()` to `dl` before the substring check as a safety net.

**Where**: `app.py`, `_resolve_vendor` function, lines 92-97.

**Impact**: Prevents future encoding-related misses. Current Anthropic/Amazon failures are actually amount mismatches, but this hardens the path.

### Fix 2: Parse Embedded USD from CC Description

**What**: CC descriptions like `GITHUB, INC.GITHUB.COM USD 5.43 [USD 5.43]` contain actual forex amounts but the bank doesn't set `forex_amount`. Parse `USD XX.XX` from description and inject as pseudo-forex.

**Regex**: `USD\s*([\d,]+\.?\d*)` — handles `(USD 200.00)`, `[USD 5.43]`, `USD 104.00`.

**Where**: In `_build_vendor_gated_matches`, after building CC list, before matching loop. Enrich each CC entry with parsed forex if `forex_amount` is not already set.

**Impact**: 9 low-confidence matches jump from 73-76% to 92-100% as Mode A (forex exact) kicks in instead of Mode C (estimated).

### Fix 3: Unmatched Reason Diagnostic

**What**: For each unmatched CC txn, run `_resolve_vendor`. If vendor resolves and any unmatched bill has the same vendor, report why it failed: amount mismatch (with actual amounts), date >60 days, or already used.

**Where**: After main matching pass in `api_payments_preview`. Add `unmatched_reason` field to unmatched CC entries. Display in UI's "No CC Match" section.

**UI**: Instead of bare "-" in the vendor column, show: `Microsoft (amt mismatch: ₹908 vs ₹12,215)` or `Google (date: 95 days apart)`.

**Impact**: Makes 83 known-vendor failures visible and actionable.

### Fix 4: Historical Forex Rate Lookup (Mode C Replacement)

**What**: Replace the crude 80-95x multiplier with actual USD/INR rates from a free API.

**API**: frankfurter.app — `GET /{date}?from=USD&to=INR`. Free, unlimited, ECB data.

**Cache**: `config/forex_cache.json` — keyed by date, persists across sessions. Past rates never change so cache is permanent.

```json
{"2025-10-18": {"USD_INR": 87.52}, "2025-10-19": {"USD_INR": 87.48}}
```

**Confidence scoring** (based on deviation from actual rate):

| Deviation | Amount Confidence |
|-----------|-------------------|
| < 0.5%   | 100               |
| < 1%     | 95                |
| < 2%     | 90                |
| < 3%     | 75                |
| < 5%     | 60                |
| > 5%     | 0 (reject)        |

**Fallback** (API unavailable): Compute `implied_rate = cc_inr / bill_usd`. If rate is within 70-100 range, show match with ⚠ flag and rate. Confidence capped at 70 (current Mode C behavior).

**UI**: Display implied rate in confidence column:
```
92%  Vendor:100 Amt:100 Date:25
     Rate: ₹88.28/$ (actual: ₹88.20)
```
Or if API unavailable:
```
75%  Vendor:100 Amt:40 Date:90
     Rate: ₹88.28/$ ⚠ Review
```

**Fetch strategy**: Batch unique CC dates at start of preview. Sequential with cache hits. Fail gracefully — never block UI.

**Impact**: 9 low-confidence USD matches jump to 92-100%. All future USD bills benefit automatically.

### Fix 5: Multi-Bill Grouping for Split Invoices

**What**: After the 1:1 matching pass, run a second pass for eligible vendors where multiple bills can sum to one CC transaction.

**Eligible vendors** (configurable in `vendor_mappings.json`):
```json
"multi_bill_vendors": [
    "Amazon India",
    "Amazon Retail India Private Limited",
    "Amazon Web Services",
    "Microsoft Corporation (India) Pvt Ltd",
    "Flipkart",
    "R K WorldInfocom Pvt. Ltd.",
    "R K WorldInfocom Pvt Ltd",
    "CLICKTECH RETAIL PRIVATE LIMITED",
    "COCOBLU RETAIL LIMITED"
]
```

**Algorithm**:
```
For each UNMATCHED CC transaction (after 1:1 pass):
  1. Resolve vendor (already computed)
  2. If vendor not in multi_bill_vendors → skip
  3. Find all UNMATCHED bills where:
     - Same vendor (vendor_conf ≥ 60)
     - Bill date within ±5 days of CC date
  4. Greedy subset-sum:
     - Sort candidate bills by amount descending
     - Accumulate until sum ≈ CC amount (within 1% tolerance)
     - Max 5 bills per group
     - Prune: skip bill if adding it would overshoot
  5. If valid group found → create group match entry
```

**Confidence scoring**:
```
vendor_conf: same as 1:1 (100/80/60)
amount_conf: based on sum accuracy (same tiers as Mode B)
date_conf: based on MAX date gap across all bills in group
group_penalty: -5 (ensures 1:1 exact matches always win first pass)

overall = vendor*0.5 + amount*0.4 + date*0.1 - 5
```

**UI display**:
```
AMAZON PAY INDIA PRIVA  ₹9,744.10  14-Oct  →  Amazon India (3 bills)
  ├── INV-2025-1042  ₹4,200.00  13-Oct
  ├── INV-2025-1043  ₹3,544.10  14-Oct
  └── INV-2025-1044  ₹2,000.00  14-Oct
                     ₹9,744.10  (exact match)     88% Record
```

Single "Record" button creates payments for all bills in the group.

**Edge cases**:
- Partial group: use best subset that fits within tolerance
- Overlapping groups: greedy assignment, first match wins
- Currency: INR bills only (USD group-matching adds unnecessary complexity)

**Impact**: Recovers matches for Amazon (25 CC, 6 bills), Flipkart, R K WorldInfocom, and other marketplace vendors with split invoicing.

## Files to Modify

- `app.py` — `_build_vendor_gated_matches` (fixes 1, 2), `_amount_diff`/`_amount_conf` (fix 4), `api_payments_preview` (fixes 3, 4, 5), UI JavaScript (fixes 3, 4, 5)
- `scripts/utils.py` — forex cache load/save, rate lookup function
- `config/vendor_mappings.json` — add `multi_bill_vendors` list
- `config/forex_cache.json` — new file (auto-populated)

## Testing Strategy

- Unit tests for USD parsing regex (various CC description formats)
- Unit tests for forex rate confidence scoring
- Unit tests for multi-bill grouping (exact sum, partial sum, max-5 limit, date window)
- Integration: run preview endpoint, verify match counts improve
- Manual: Chrome validation of Record Payment page after changes

## Expected Impact

| Metric | Before | After |
|--------|--------|-------|
| Matched bills | 16 | 16+ (group matches add more) |
| Low-confidence matches (< 80%) | 9 | 0 (forex lookup fixes all) |
| Unmatched CC with known vendor | 83 (silent) | 83 (with diagnostic reasons) |
| Multi-bill group matches | 0 | TBD (depends on bill amounts) |
| Amex matches | 18 | 18 (unchanged) |
