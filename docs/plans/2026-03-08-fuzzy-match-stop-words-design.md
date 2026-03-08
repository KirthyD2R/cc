# Fuzzy Match Stop-Words Design

**Date**: 2026-03-08
**Status**: Approved

## Problem

`fuzz.token_set_ratio` produces false vendor matches when common business
suffixes (e.g. "Enterprises", "Private Limited", "India") dominate the token
intersection. Examples:

- "Aggarwal Enterprises" → "Aarav Enterprises" (score ~90, wrong)
- "MS ENTERPRISES" → "Aarav Enterprises" (score ~86, wrong)
- "SPIGEN INDIA PRIVATE LIMITED" → "Google" (wrong)

## Solution: Stop-word stripping before fuzzy comparison

Strip common business suffixes from both sides before computing
`token_set_ratio`. If stripping would leave the name empty, fall back to the
original name.

### Stop-word list

```
enterprises, enterprise, pvt, private, limited, ltd, inc, incorporated,
llp, india, corporation, corp, co, company, services, solutions,
technologies, technology, tech, marketing, international, global,
group, associates, consultants, consulting, traders, trading,
industries, industrial
```

### Shared helper (scripts/utils.py)

```python
VENDOR_STOP_WORDS = {
    "enterprises", "enterprise", "pvt", "private", "limited", "ltd",
    "inc", "incorporated", "llp", "india", "corporation", "corp",
    "co", "company", "services", "solutions", "technologies",
    "technology", "tech", "marketing", "international", "global",
    "group", "associates", "consultants", "consulting", "traders",
    "trading", "industries", "industrial",
}

def strip_vendor_stop_words(name):
    tokens = name.strip().upper().split()
    filtered = [t for t in tokens if t.lower() not in VENDOR_STOP_WORDS]
    return " ".join(filtered) if filtered else name.strip().upper()
```

### Changes

1. **scripts/utils.py:639** — `fuzzy_match_vendor()`: wrap both sides in
   `strip_vendor_stop_words()` before `fuzz.token_set_ratio`
2. **app.py:3639-3641** — bill preview fuzzy match: same wrapping, import
   helper from `scripts.utils`

### Expected impact

| Invoice Vendor | Zoho Vendor | Before | After |
|---|---|---|---|
| Aggarwal Enterprises | Aarav Enterprises | ~90 ✗ | ~0 ✓ |
| MS ENTERPRISES | Aarav Enterprises | ~86 ✗ | ~0 ✓ |
| Google Play | Google | ~86 ✓ | ~86 ✓ |
| GitHub | GitHub, Inc. | ~90 ✓ | ~90 ✓ |
| ETRADE MARKETING PVT LTD | ETRADE MARKETING PRIVATE LIMITED | ~95 ✓ | ~100 ✓ |

### Not changed

- `_vendor_match()` in app.py:773-790 — uses substring matching, not fuzzywuzzy
- Thresholds remain at 75 (utils) and 85 (app.py)
