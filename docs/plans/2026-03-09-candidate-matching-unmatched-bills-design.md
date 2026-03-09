# Candidate Matching for Unmatched Bills

**Date:** 2026-03-09
**Status:** Approved

## Problem

158 bills show as "No CC Match — Bills Only" because the vendor-gated matching algorithm cannot resolve the CC description to a bill vendor name. Many of these are online/SaaS transactions (Medium, New Relic, Info Edge/Naukri, S2 Labs) that were paid by credit card but use different merchant names in the CC statement.

## Solution: Hybrid Candidate Engine + Inline UI

A two-phase approach: backend computes candidate CC transactions for unmatched bills using amount+date matching, and the frontend displays these inline with bulk approval support.

## Architecture

```
Existing vendor-gated matching (unchanged)
        |
        v
Unmatched bills --> Candidate Engine (new)
        |               |
        v               v
    cc_only         candidates[] added
    (unchanged)     to each unmatched bill
```

## Backend: Candidate Engine

### When it runs
After vendor-gated matching completes, only on bills with `status: "unmatched"`. Searches remaining unmatched CC transactions (those in `cc_only`).

### Candidate scoring

For each unmatched bill x each unmatched CC transaction:

**Amount score (0-100):**
- Exact match (within 0.01 or 0.01%): 100
- Within 1%: 80
- Within 5%: 50
- Beyond 5%: skip (not a candidate)

**Date score (0-100):**
- 0-2 days apart: 100
- 3-5 days: 80
- 6-10 days: 60
- 11-30 days: 30
- 31-60 days: 10
- >60 days: skip

**Vendor signal score (0-100):**
- Bill vendor substring found in CC description (>=4 chars, case-insensitive): 80
- First word of bill vendor matches first word of CC description: 50
- No overlap: 0

**Uniqueness bonus:**
- Only 1 candidate at this amount within date range: +15
- 2-3 candidates: +0
- 4+ candidates: -10

**Overall = amount x 0.4 + date x 0.2 + vendor_signal x 0.3 + uniqueness x 0.1**

### Cross-currency handling
- USD bills: convert to INR using forex rate (existing logic), compare to CC INR amount
- If CC has `forex_amount` in USD: compare directly to USD bill amount (higher confidence)

### Output
Each unmatched bill gets a `candidates` array (top 5, sorted by score):

```json
{
  "status": "unmatched",
  "bill_id": "...",
  "vendor_name": "Medium",
  "bill_amount": 5.00,
  "bill_currency": "USD",
  "candidates": [
    {
      "cc_transaction_id": "T42",
      "cc_description": "MEDIUM, SAN FRANCISCO",
      "cc_inr_amount": 435.00,
      "cc_date": "2025-02-01",
      "cc_forex_amount": 5.00,
      "cc_forex_currency": "USD",
      "cc_card": "Mayura CC 9677",
      "candidate_score": 88,
      "breakdown": {"amount": 100, "date": 100, "vendor": 80, "uniqueness": 15}
    }
  ]
}
```

## Frontend: Inline Candidates + Bulk Approval

### Section header toolbar
When candidates exist, the "No CC Match" section header shows:
- Score threshold filter (dropdown, default: >= 70)
- "Select All Visible" button
- "Confirm Selected (N)" button

### Row display (collapsed)
"No CC" rows with candidates reuse the empty CC columns:
- CC Description, CC INR, CC Date, Card columns pre-filled with top candidate
- Styled distinctly (dimmed/italic) to distinguish from confirmed matches
- Confidence column shows candidate score + expandable icon
- Checkbox enabled for bulk selection

Rows without candidates remain as dashes, not selectable for bulk confirm.

### Row display (expanded, on click)
Expands below the row showing:
1. All candidates (up to 5) as compact list with scores and per-candidate Confirm button
2. Free-text CC description search box (pre-filled: amount +/-5%, date +/-30d from bill)
3. "Not CC Paid" dismiss link

### Search box behavior
- Searches ALL CC transactions (not just unmatched) by description substring
- Optional amount tolerance (default +/-5% of bill amount)
- Optional date range (default +/-30 days of bill date)
- Results show same format as candidates with Confirm Match action

### Bulk confirm flow
1. Set score threshold (e.g., >= 80)
2. Click "Select All Visible"
3. Click "Confirm Selected (N)"
4. Summary modal shows:
   - Score distribution (90+: X bills, 70-89: Y bills)
   - Number of new vendor mappings to learn
   - Cancel / Confirm All buttons
5. On confirm: records all payments, auto-learns vendor mappings, moves rows to Matched section

## Auto-learning
- Every confirmed candidate saves `CC_DESCRIPTION -> vendor_name` to `learned_vendor_mappings.json`
- Future matching runs: these bills match automatically via vendor-gated logic
- The "No CC" category shrinks over time

## Key decisions
- Candidate engine runs only as fallback after vendor-gated matching (preserves existing high-confidence matching)
- Top candidate shown inline in empty CC columns (no separate panel, reuses existing layout)
- Bulk approval with score threshold filter (handles high volume efficiently)
- Manual search box as escape hatch when candidates miss
