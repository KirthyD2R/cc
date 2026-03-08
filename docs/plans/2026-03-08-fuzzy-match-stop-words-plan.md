# Fuzzy Match Stop-Words Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate false fuzzy vendor matches caused by common business suffixes like "Enterprises", "Private Limited", "India" dominating `token_set_ratio` scores.

**Architecture:** Add a shared stop-word set and stripping function to `scripts/utils.py`. Apply it in two locations: `fuzzy_match_vendor()` (utils.py:639) and bill preview fuzzy match (app.py:3641). Both strip stop-words from vendor names before computing `fuzz.token_set_ratio`.

**Tech Stack:** Python, fuzzywuzzy/thefuzz

---

### Task 1: Add stop-word constant and helper to scripts/utils.py

**Files:**
- Modify: `scripts/utils.py:622` (insert before `fuzzy_match_vendor`)

**Step 1: Add the constant and helper function**

Insert between line 622 (`# --- Fuzzy Matching ---`) and line 624 (`def fuzzy_match_vendor`):

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
    """Remove common business suffixes for better fuzzy comparison.
    Falls back to original name if stripping would leave it empty."""
    tokens = name.strip().upper().split()
    filtered = [t for t in tokens if t.lower() not in VENDOR_STOP_WORDS]
    return " ".join(filtered) if filtered else name.strip().upper()
```

**Step 2: Verify no syntax errors**

Run: `python -c "from scripts.utils import strip_vendor_stop_words, VENDOR_STOP_WORDS; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add scripts/utils.py
git commit -m "feat: add vendor stop-words constant and strip helper for fuzzy matching"
```

---

### Task 2: Apply stop-word stripping in fuzzy_match_vendor()

**Files:**
- Modify: `scripts/utils.py:639`

**Step 1: Change the token_set_ratio call**

Current code at line 639:
```python
        score = fuzz.token_set_ratio(merchant_name.upper(), key.upper())
```

Replace with:
```python
        score = fuzz.token_set_ratio(
            strip_vendor_stop_words(merchant_name),
            strip_vendor_stop_words(key),
        )
```

**Step 2: Verify with a quick smoke test**

Run:
```bash
python -c "
from scripts.utils import fuzzy_match_vendor
mappings = {'mappings': {'Aarav Enterprises': 'Aarav Enterprises'}}
result, score = fuzzy_match_vendor('Aggarwal Enterprises', mappings, threshold=75)
print(f'Result: {result}, Score: {score}')
assert result is None, f'Should NOT match but got: {result} (score {score})'
print('PASS: false match prevented')

mappings2 = {'mappings': {'Google': 'Google'}}
result2, score2 = fuzzy_match_vendor('Google Play', mappings2, threshold=75)
print(f'Result: {result2}, Score: {score2}')
assert result2 == 'Google', f'Should match Google but got: {result2}'
print('PASS: good match preserved')
"
```
Expected: Both assertions pass.

**Step 3: Commit**

```bash
git add scripts/utils.py
git commit -m "fix: strip vendor stop-words before fuzzy matching in fuzzy_match_vendor"
```

---

### Task 3: Apply stop-word stripping in app.py bill preview

**Files:**
- Modify: `app.py:3638-3641`

**Step 1: Import the helper**

The fuzzy match block at line 3637-3641 currently reads:
```python
            if not vendor_found:
                from thefuzz import fuzz
                best_score, best_vendor = 0, None
                for vkey, vinfo in vendor_name_map.items():
                    score = fuzz.token_set_ratio(vn_lower, vkey)
```

Change to:
```python
            if not vendor_found:
                from thefuzz import fuzz
                from scripts.utils import strip_vendor_stop_words
                best_score, best_vendor = 0, None
                for vkey, vinfo in vendor_name_map.items():
                    score = fuzz.token_set_ratio(
                        strip_vendor_stop_words(vn_lower),
                        strip_vendor_stop_words(vkey),
                    )
```

**Step 2: Verify no import errors**

Run: `python -c "from app import app; print('OK')"`
Expected: `OK` (or at least no ImportError on the new import)

**Step 3: Commit**

```bash
git add app.py
git commit -m "fix: strip vendor stop-words before fuzzy matching in bill preview"
```

---

### Task 4: End-to-end verification

**Step 1: Run the bill preview and check fuzzy matches**

```bash
python -c "
from scripts.utils import strip_vendor_stop_words, VENDOR_STOP_WORDS
from thefuzz import fuzz

# Cases that should NOT match (false positives we're fixing)
bad_pairs = [
    ('Aggarwal Enterprises', 'Aarav Enterprises'),
    ('MS ENTERPRISES', 'Aarav Enterprises'),
    ('SPIGEN INDIA PRIVATE LIMITED', 'Google'),
]
for a, b in bad_pairs:
    score = fuzz.token_set_ratio(strip_vendor_stop_words(a), strip_vendor_stop_words(b))
    print(f'  {a:40s} vs {b:25s} -> {score:3d} (should be < 85)')
    assert score < 85, f'FALSE MATCH: {a} -> {b} score {score}'

# Cases that SHOULD still match (good matches we're preserving)
good_pairs = [
    ('Google Play', 'Google', 85),
    ('GitHub', 'GitHub, Inc.', 85),
    ('ETRADE MARKETING PRIVATE LIMITED', 'ETRADE MARKETING PVT LTD', 85),
    ('Groq Inc', 'GROQ', 75),
    ('LinkedIn', 'LinkedIn Singapore Pte Ltd', 75),
]
for a, b, thresh in good_pairs:
    score = fuzz.token_set_ratio(strip_vendor_stop_words(a), strip_vendor_stop_words(b))
    print(f'  {a:40s} vs {b:25s} -> {score:3d} (should be >= {thresh})')
    assert score >= thresh, f'LOST MATCH: {a} -> {b} score {score} < {thresh}'

print()
print('ALL CHECKS PASSED')
"
```

Expected: All assertions pass, no false matches, no lost good matches.

**Step 2: Commit (if any fixes were needed)**

Only if adjustments were made in previous steps.
