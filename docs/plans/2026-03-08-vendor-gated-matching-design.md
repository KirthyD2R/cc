# Vendor-Gated Bill Matching Design

## Problem

The current matching algorithm is amount-first: it finds all (bill, CC) pairs within 1% amount tolerance, then adds vendor as a bonus score. This produces false matches like SHOE DEPT → Microsoft and AMAZON INDIA CYBS → R K WorldInfocom, where amounts happen to be similar but vendors are completely unrelated.

## Approach: Vendor-Gated Matching

Flip the priority: resolve vendor first, gate on vendor match, then validate with amount. No vendor signal = no match shown.

## Decisions

- **No vendor signal → don't show match** (no amount-only matches)
- **Gateway blacklist** for CYBS/BillDesk/etc. when no brand prefix present
- **Strict forex amount matching** (penny tolerance, no percentage)
- **Learning system** from confirmed recordings + bill creation + manual overrides
- **INR/USD exchange rate range**: 80–95 (was 75–100)

## 1. Vendor Resolution Pipeline

Resolution cascade (in order):
1. **Manual mappings** — `config/vendor_mappings.json` (human curated, highest priority)
2. **Learned mappings** — `config/learned_vendor_mappings.json` (auto-populated)
3. **Keyword extraction + fuzzy match** — extract core brand keyword from CC description, `token_set_ratio` with stop-word stripping against Zoho vendor names
4. **Gateway blacklist** — if description is gateway-only (CYBS, BILLDESK, PAYU, etc.) with no brand prefix → return null

Vendor-to-bill comparison (normalized):
- Exact normalized match → `vendor_conf = 100`
- Substring match (4+ chars) → `vendor_conf = 80`
- First-word match (4+ chars) → `vendor_conf = 60`
- Below 60 → not a match (pair skipped)

## 2. Amount Matching Rules

**Mode A — Forex exact match (highest confidence):**
- CC has extracted forex amount in same currency as bill
- Strict: `abs(forex_amount - bill_amount) < 0.01`
- No percentage tolerance

**Mode B — INR-to-INR:**
- Both CC and bill in INR, no forex
- Tolerance: ≤ 1% of bill amount, minimum ₹1.00

**Mode C — USD bill, no forex tag:**
- Estimate: CC INR should fall within `bill_USD × 80` to `bill_USD × 95`
- Tolerance: ≤ 2% of estimated INR amount
- Amount confidence capped at 70

## 3. Confidence Scoring

Reweighted to reflect actual importance:

| Component | Weight | Previously |
|-----------|--------|-----------|
| Vendor    | 50%    | 40%       |
| Amount    | 40%    | 40%       |
| Date      | 10%    | 20%       |

`overall = vendor_conf × 0.5 + amount_conf × 0.4 + date_conf × 0.1`

Individual component scores unchanged:
- `vendor_conf`: 100 / 80 / 60 (exact / substring / first-word)
- `amount_conf`: 100 / 95 / 90 / 75 / 70(est) / 60 / 40
- `date_conf`: 100 / 90 / 75 / 50 / 25 / 0

## 4. Gateway Blacklist

```python
GATEWAY_KEYWORDS = {
    "cybs", "billdesk", "payu", "razorpay", "ccavenue",
    "paygate", "instamojo", "cashfree", "phonepe", "paytm",
}
```

Only blacklists when description is gateway-only with no brand prefix. Existing vendor_mappings.json entries like `"AMAZON INDIA CYBS" → "Amazon India"` take precedence — the blacklist is a safety net for unknown gateway transactions.

## 5. Learning System

Three input channels → `config/learned_vendor_mappings.json`:

1. **Record Payment confirmation** — user clicks Record → save CC description → Zoho vendor
2. **Bill creation vendor matching** — vendor resolved during bill creation → save mapping
3. **Manual overrides** — existing `vendor_mappings.json` (highest priority)

Resolution priority: manual > learned > fuzzy keyword.

Deduplication by normalized key. No decay/expiry — vendor names are stable.

## 6. Matching Algorithm

```
1. RESOLVE — For each CC txn, resolve vendor:
   manual_mappings → learned_mappings → keyword_fuzzy → null

2. GATE — Skip CC txns where resolved vendor is null
   or description is gateway-only (if no mapping found)

3. BUILD CANDIDATES — For each (bill, CC) pair:
   a. Vendor match? (normalized/substring/first-word) → skip if no match
   b. Amount match? (Mode A/B/C) → skip if no match
   c. Date within 60 days? → skip if not

4. SCORE — vendor_conf × 0.5 + amount_conf × 0.4 + date_conf × 0.1

5. RANK — Sort by total_score desc, date proximity tiebreaker

6. ASSIGN — Greedy best-first, each bill and CC used once

7. LEARN — After user confirms Record, save to learned mappings
```

## Expected Impact

| Row | Current | After |
|-----|---------|-------|
| LinkedIn → LinkedIn Singapore | 90% | ~89% (stable) |
| Anthropic → Anthropic USD | 85% | ~92% (improved) |
| Microsoft → Microsoft Corp | 82% | ~82% (stable) |
| Google → Google | 80% | ~85% (improved) |
| AMAZON CYBS → R K WorldInfocom | 55-58% | **dropped** |
| IND*LINKEDIN → Microsoft | 56% | **dropped** |
| SHOE DEPT → Microsoft | 51% | **dropped** |

## Files to Modify

- `app.py` — lines 807-1030 (matching algorithm core)
- `scripts/utils.py` — gateway blacklist, learning save/load functions
- `config/learned_vendor_mappings.json` — new file (auto-populated)
- Bill creation endpoint — add learning channel 2
- Record payment endpoint — add learning channel 1
