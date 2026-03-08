# Vendor-Gated Bill Matching Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace amount-first matching with vendor-gated matching so only CC transactions with a confirmed vendor signal produce bill matches, eliminating false positives like SHOE→Microsoft.

**Architecture:** Vendor resolution pipeline (manual → learned → fuzzy keyword) gates all matching. Amount validates within vendor-matched pairs. A learning system auto-grows mappings from confirmed recordings and bill creation. Gateway transactions with no brand prefix are blacklisted.

**Tech Stack:** Python/Flask, rapidfuzz (already installed), JSON config files

**Design doc:** `docs/plans/2026-03-08-vendor-gated-matching-design.md`

---

### Task 1: Create test infrastructure and learned mappings module

**Files:**
- Create: `tests/test_vendor_matching.py`
- Create: `config/learned_vendor_mappings.json`
- Modify: `scripts/utils.py:26-28` (add load/save for learned mappings)

**Step 1: Create the learned mappings JSON file (empty)**

```json
{
  "_comment": "Auto-populated from confirmed recordings and bill creation. Do not edit manually.",
  "mappings": {}
}
```

Save to `config/learned_vendor_mappings.json`.

**Step 2: Add load/save functions to `scripts/utils.py`**

Add after `load_vendor_mappings` (line 28):

```python
def load_learned_vendor_mappings(path="config/learned_vendor_mappings.json"):
    full_path = os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(full_path):
        return {"mappings": {}}
    with open(full_path, "r") as f:
        return json.load(f)


def save_learned_vendor_mapping(cc_description, vendor_name, path="config/learned_vendor_mappings.json"):
    """Save a CC description → vendor name mapping learned from user confirmation."""
    full_path = os.path.join(PROJECT_ROOT, path)
    data = load_learned_vendor_mappings(path)
    # Normalize key: strip, uppercase
    key = cc_description.strip().upper()
    if not key or not vendor_name:
        return
    data["mappings"][key] = vendor_name.strip()
    with open(full_path, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
```

**Step 3: Create test file with initial tests for the learning functions**

Create `tests/test_vendor_matching.py`:

```python
"""Tests for vendor-gated matching logic."""
import json
import os
import sys
import tempfile

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.utils import (
    load_learned_vendor_mappings,
    save_learned_vendor_mapping,
    strip_vendor_stop_words,
)


def test_load_learned_mappings_missing_file():
    """Loading from non-existent file returns empty mappings."""
    result = load_learned_vendor_mappings("nonexistent.json")
    assert result == {"mappings": {}}


def test_save_and_load_learned_mapping(tmp_path):
    """Save a mapping and load it back."""
    path = str(tmp_path / "learned.json")
    # Create initial file
    with open(path, "w") as f:
        json.dump({"mappings": {}}, f)

    save_learned_vendor_mapping("IND*LINKEDIN (PGSI)", "LinkedIn Singapore Pte Ltd", path=path)

    data = load_learned_vendor_mappings(path)
    assert data["mappings"]["IND*LINKEDIN (PGSI)"] == "LinkedIn Singapore Pte Ltd"


def test_save_learned_mapping_normalizes_key(tmp_path):
    """Key is stored uppercase and stripped."""
    path = str(tmp_path / "learned.json")
    with open(path, "w") as f:
        json.dump({"mappings": {}}, f)

    save_learned_vendor_mapping("  claude.ai subscription  ", "Anthropic USD", path=path)

    data = load_learned_vendor_mappings(path)
    assert "CLAUDE.AI SUBSCRIPTION" in data["mappings"]


def test_save_learned_mapping_skips_empty():
    """Empty description or vendor is silently skipped."""
    # Should not raise
    save_learned_vendor_mapping("", "Vendor", path="/tmp/test_empty.json")
    save_learned_vendor_mapping("desc", "", path="/tmp/test_empty.json")
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: 4 tests PASS

**Step 5: Commit**

```bash
git add config/learned_vendor_mappings.json tests/test_vendor_matching.py scripts/utils.py
git commit -m "feat: add learned vendor mappings infrastructure with tests"
```

---

### Task 2: Add gateway blacklist and vendor resolution tests

**Files:**
- Modify: `scripts/utils.py:624-639` (add gateway blacklist)
- Modify: `tests/test_vendor_matching.py` (add gateway tests)

**Step 1: Write failing tests for gateway detection**

Add to `tests/test_vendor_matching.py`:

```python
from scripts.utils import is_gateway_only


def test_gateway_only_cybs():
    """Pure gateway description with no brand prefix."""
    assert is_gateway_only("CYBS SI MUMBAI IN") is True


def test_gateway_with_brand_prefix():
    """Brand + gateway is NOT gateway-only."""
    assert is_gateway_only("AMAZON INDIA CYBS SI MUMBAI") is False
    assert is_gateway_only("MICROSOFT INDIA CYBS") is False


def test_gateway_billdesk():
    assert is_gateway_only("BILLDESK BBPS") is True


def test_non_gateway_description():
    assert is_gateway_only("IND*LINKEDIN (PGSI), www.linkedin.") is False
    assert is_gateway_only("CLAUDE.AI SUBSCRIPTION") is False
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py::test_gateway_only_cybs -v`
Expected: FAIL with ImportError (is_gateway_only not defined)

**Step 3: Implement gateway blacklist in `scripts/utils.py`**

Add after `strip_vendor_stop_words` function (after line 639):

```python
GATEWAY_KEYWORDS = {
    "cybs", "billdesk", "payu", "razorpay", "ccavenue",
    "paygate", "instamojo", "cashfree", "phonepe", "paytm",
}


def is_gateway_only(description):
    """Check if CC description is a payment gateway with no brand prefix.
    Returns True only when the meaningful tokens are ALL gateway keywords
    (plus location/noise words). Returns False if a brand name is present."""
    if not description:
        return False
    tokens = description.strip().upper().split()
    # Location/noise words to ignore when checking for brand presence
    noise = {"si", "in", "mumbai", "bangalore", "chennai", "delhi",
             "india", "bbps", "cc", "payment", "rate"}
    meaningful = [t.lower() for t in tokens if t.lower() not in noise]
    if not meaningful:
        return False
    # If ALL meaningful tokens are gateway keywords, it's gateway-only
    return all(t in GATEWAY_KEYWORDS for t in meaningful)
```

**Step 4: Run all tests to verify they pass**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add scripts/utils.py tests/test_vendor_matching.py
git commit -m "feat: add gateway blacklist detection"
```

---

### Task 3: Write tests for the new vendor-gated matching algorithm

**Files:**
- Modify: `tests/test_vendor_matching.py` (add matching algorithm tests)

**Step 1: Add matching algorithm tests**

These tests define the expected behavior of the new matching. They will fail until Task 4 implements it.

Add to `tests/test_vendor_matching.py`:

```python
from app import _build_vendor_gated_matches


def _make_bill(vendor, amount, currency="INR", date="2025-07-15", bill_id="B1"):
    return {"bill_id": bill_id, "vendor_id": "V1", "vendor_name": vendor,
            "amount": amount, "currency": currency, "date": date, "file": "INV-001"}


def _make_cc(desc, amount, date="2025-07-16", card="Mayura Credit Card",
             forex_amount=None, forex_currency=None):
    cc = {"description": desc, "amount": amount, "date": date,
          "card_name": card, "transaction_id": "T1"}
    if forex_amount is not None:
        cc["forex_amount"] = forex_amount
        cc["forex_currency"] = forex_currency
    return cc


def test_exact_vendor_amount_match():
    """LinkedIn CC matches LinkedIn bill when vendor and amount match."""
    bills = [_make_bill("LinkedIn Singapore Pte Ltd", 7106.00)]
    cc = [_make_cc("IND*LINKEDIN (PGSI), www.linkedin.", 7106.00)]
    vendor_map = {"ind*linkedin": "LinkedIn"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    assert len(matches) == 1
    assert matches[0]["status"] == "matched"
    assert matches[0]["confidence"]["vendor"] >= 60


def test_no_vendor_signal_blocks_match():
    """SHOE DEPT should NOT match Microsoft even if amounts are close."""
    bills = [_make_bill("Microsoft Corporation (India) Pvt Ltd", 5288.36)]
    cc = [_make_cc("SHOE DEPT 0378, BEAUMONT", 5318.23)]
    matches = _build_vendor_gated_matches(bills, cc, {}, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 0


def test_gateway_only_blocks_match():
    """Pure gateway description without brand should not match."""
    bills = [_make_bill("R K WorldInfocom Pvt. Ltd.", 276.25)]
    cc = [_make_cc("CYBS SI MUMBAI IN", 276.25)]
    matches = _build_vendor_gated_matches(bills, cc, {}, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 0


def test_forex_strict_exact_match():
    """USD forex amounts must match exactly (penny tolerance)."""
    bills = [_make_bill("GitHub, Inc.", 103.12, currency="USD")]
    cc = [_make_cc("GITHUB, INC.GITHUB.COM USD 104.00", 9551.62,
                   forex_amount=104.00, forex_currency="USD")]
    vendor_map = {"github": "GitHub"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    # USD 104.00 != USD 103.12 — strict forex, should NOT match
    assert len(matched) == 0


def test_forex_exact_match_passes():
    """USD forex amounts that match exactly should produce a match."""
    bills = [_make_bill("Anthropic USD", 200.00, currency="USD")]
    cc = [_make_cc("CLAUDE.AI SUBSCRIPTIONANTHROPIC. USD 200", 18171.91,
                   forex_amount=200.00, forex_currency="USD")]
    vendor_map = {"claude.ai subscription": "Anthropic USD"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    assert matched[0]["confidence"]["amount"] == 100


def test_confidence_weights():
    """Overall confidence = vendor*0.5 + amount*0.4 + date*0.1."""
    bills = [_make_bill("Microsoft Corporation (India) Pvt Ltd", 12215.38)]
    cc = [_make_cc("MICROSOFTBUS, MUMBAI", 12215.38, date="2025-07-18")]
    vendor_map = {"microsoftbus": "Microsoft"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    conf = matched[0]["confidence"]
    expected = int(conf["vendor"] * 0.5 + conf["amount"] * 0.4 + conf["date"] * 0.1)
    assert conf["overall"] == expected


def test_learned_mappings_used():
    """Learned mappings resolve vendor when manual mappings don't."""
    bills = [_make_bill("Acme Corp", 500.00)]
    cc = [_make_cc("ACME PAYMENTS MUMBAI", 500.00)]
    learned = {"ACME PAYMENTS MUMBAI": "Acme Corp"}
    matches = _build_vendor_gated_matches(bills, cc, {}, learned)
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py::test_exact_vendor_amount_match -v`
Expected: FAIL with ImportError (_build_vendor_gated_matches not defined)

**Step 3: Commit test file**

```bash
git add tests/test_vendor_matching.py
git commit -m "test: add vendor-gated matching algorithm tests (red)"
```

---

### Task 4: Implement vendor-gated matching algorithm

**Files:**
- Modify: `app.py:807-842` (update `_resolve_vendor` and `_vendor_match`)
- Modify: `app.py:943-1030` (replace matching loop with vendor-gated logic)
- Modify: `app.py:878-941` (update confidence weights)

This is the core change. The matching loop in `api_payments_preview` (line 737) needs to be restructured.

**Step 1: Extract `_build_vendor_gated_matches` as a module-level function**

Add at the top of `app.py` (after imports, before routes), a new function that encapsulates the matching logic. This makes it testable independently.

```python
def _build_vendor_gated_matches(bills, cc_list, manual_vendor_map, learned_vendor_map):
    """Vendor-gated bill matching: only pairs with vendor signal are matched.

    Args:
        bills: list of dicts with keys: bill_id, vendor_id, vendor_name, amount, currency, date, file
        cc_list: list of dicts with keys: description, amount, date, card_name, transaction_id,
                 optional: forex_amount, forex_currency
        manual_vendor_map: dict of lowercase CC description -> vendor name (from vendor_mappings.json)
        learned_vendor_map: dict of uppercase CC description -> vendor name (from learned_vendor_mappings.json)

    Returns:
        list of match dicts with status "matched" or "unmatched"
    """
    from datetime import datetime as _dt
    from scripts.utils import is_gateway_only, strip_vendor_stop_words

    def _norm(s):
        return "".join(c for c in s.lower() if c.isalnum())

    # --- Build lookup structures for manual mappings ---
    vm_lower = {k.lower(): v for k, v in manual_vendor_map.items()}
    vm_norm = {_norm(k): v for k, v in manual_vendor_map.items()}
    sorted_keys = sorted(vm_lower.keys(), key=len, reverse=True)
    sorted_norm_keys = sorted(vm_norm.keys(), key=len, reverse=True)

    def _resolve_vendor(desc):
        """Resolve CC description to vendor name. Returns (vendor_name, source) or (None, None)."""
        if not desc:
            return None, None
        dl = desc.lower()
        dn = _norm(desc)
        du = desc.strip().upper()

        # Priority 1: Manual mappings (exact, normalized, substring)
        if dl in vm_lower:
            return vm_lower[dl], "manual"
        if dn in vm_norm:
            return vm_norm[dn], "manual"
        for key in sorted_keys:
            if key and len(key) >= 4 and key in dl:
                return vm_lower[key], "manual"
        for key in sorted_norm_keys:
            if key and len(key) >= 4 and key in dn:
                return vm_norm[key], "manual"

        # Priority 2: Learned mappings (exact uppercase)
        if du in learned_vendor_map:
            return learned_vendor_map[du], "learned"
        # Learned substring match
        for key in sorted(learned_vendor_map.keys(), key=len, reverse=True):
            if key and len(key) >= 4 and key in du:
                return learned_vendor_map[key], "learned"

        # Priority 3: Gateway check — if gateway-only, no vendor signal
        if is_gateway_only(desc):
            return None, "gateway"

        return None, None

    def _vendor_conf(resolved_vendor, bill_vendor):
        """Compute vendor confidence between resolved CC vendor and bill vendor."""
        if not resolved_vendor:
            return 0
        rv = _norm(resolved_vendor)
        bv = _norm(bill_vendor)
        # Also try with stop words stripped
        rv_stripped = _norm(strip_vendor_stop_words(resolved_vendor))
        bv_stripped = _norm(strip_vendor_stop_words(bill_vendor))

        if rv == bv or rv_stripped == bv_stripped:
            return 100
        if len(rv) >= 4 and (rv in bv or bv in rv):
            return 80
        if len(rv_stripped) >= 4 and (rv_stripped in bv_stripped or bv_stripped in rv_stripped):
            return 80
        # First-word match
        rv_first = _norm(resolved_vendor.split()[0]) if resolved_vendor.split() else ""
        if rv_first and len(rv_first) >= 4 and rv_first in bv:
            return 60
        return 0

    def _amount_diff(bill, cc):
        """Return (diff, match_type) or (None, None) if not comparable."""
        bill_amt = bill["amount"]
        bill_cur = bill["currency"]
        cc_inr = cc["amount"]
        fx = cc.get("forex_amount")
        fx_cur = (cc.get("forex_currency") or "").upper()

        # Mode A: Forex exact match
        if fx and fx_cur and bill_cur.upper() == fx_cur:
            diff = abs(fx - bill_amt)
            if diff >= 0.01:  # Strict: penny tolerance only
                return None, None
            return diff, f"{fx_cur} exact"

        # Mode B: INR-to-INR
        if bill_cur == "INR" and not fx:
            diff = abs(cc_inr - bill_amt)
            threshold = max(1.0, bill_amt * 0.01)
            if diff > threshold:
                return None, None
            return diff, "INR direct"

        # Mode C (variant): INR bill, CC has forex — compare INR amounts
        if bill_cur == "INR" and fx:
            diff = abs(cc_inr - bill_amt)
            threshold = max(1.0, bill_amt * 0.01)
            if diff > threshold:
                return None, None
            return diff, f"{fx_cur} → INR (forex)"

        # Mode C: USD bill, no forex tag — estimate
        if bill_cur == "USD" and not fx:
            est_min = bill_amt * 80
            est_max = bill_amt * 95
            if est_min <= cc_inr <= est_max:
                diff = abs(cc_inr - bill_amt * 87.5)  # Midpoint of 80-95
                tolerance = bill_amt * 87.5 * 0.02
                if diff > tolerance:
                    return None, None
                return diff, "USD → INR (est)"

        return None, None

    def _amount_conf(bill, cc):
        """Compute amount confidence score."""
        diff, mtype = _amount_diff(bill, cc)
        if diff is None:
            return 0, None, None
        bill_amt = bill["amount"] if bill["amount"] else 1
        pct_diff = diff / bill_amt
        if mtype and "exact" in mtype:
            conf = 100  # Forex exact always 100
        elif pct_diff < 0.001:
            conf = 100
        elif pct_diff < 0.005:
            conf = 95
        elif pct_diff < 0.01:
            conf = 90
        elif pct_diff < 0.03:
            conf = 75
        elif pct_diff < 0.05:
            conf = 60
        else:
            conf = 40
        # Cap for estimated conversions
        if mtype and "est" in mtype:
            conf = min(conf, 70)
        return conf, diff, mtype

    def _date_conf(bill, cc):
        """Compute date confidence score."""
        try:
            bd = _dt.strptime(bill["date"], "%Y-%m-%d")
            cd = _dt.strptime(cc["date"], "%Y-%m-%d")
            dd = abs((bd - cd).days)
        except Exception:
            return 0, 9999
        if dd > 60:
            return 0, dd
        if dd == 0:
            return 100, dd
        elif dd <= 2:
            return 90, dd
        elif dd <= 5:
            return 75, dd
        elif dd <= 10:
            return 50, dd
        elif dd <= 30:
            return 25, dd
        else:
            return 0, dd

    # --- Resolve all CC vendors ---
    cc_resolved = []  # (vendor_name, source) for each CC txn
    for cc in cc_list:
        cc_resolved.append(_resolve_vendor(cc.get("description", "")))

    # --- Build candidates: vendor-gated ---
    candidates = []  # (score, date_diff, bill_idx, cc_idx, v_conf, a_conf, d_conf)

    for bi, bill in enumerate(bills):
        for ci, cc in enumerate(cc_list):
            resolved_vendor, source = cc_resolved[ci]

            # GATE: No vendor signal → skip
            if not resolved_vendor:
                continue

            # GATE: Vendor must match bill vendor
            vc = _vendor_conf(resolved_vendor, bill["vendor_name"])
            if vc < 60:
                continue

            # Amount must match
            ac, diff, mtype = _amount_conf(bill, cc)
            if ac == 0:
                continue

            # Date within 60 days
            dc, dd = _date_conf(bill, cc)
            if dd > 60:
                continue

            # Score: vendor*0.5 + amount*0.4 + date*0.1
            overall = int(vc * 0.5 + ac * 0.4 + dc * 0.1)
            candidates.append((overall, dd, bi, ci, vc, ac, dc))

    # Sort: highest score first, then closest date
    candidates.sort(key=lambda x: (-x[0], x[1]))

    # Greedy assignment
    bill_matched = [False] * len(bills)
    used_cc = set()
    matches = []

    for overall, dd, bi, ci, vc, ac, dc in candidates:
        if bill_matched[bi] or ci in used_cc:
            continue
        bill_matched[bi] = True
        used_cc.add(ci)

        bill = bills[bi]
        cc = cc_list[ci]
        resolved_vendor, _ = cc_resolved[ci]

        entry = {
            "bill_id": bill["bill_id"],
            "vendor_id": bill["vendor_id"],
            "vendor_name": bill["vendor_name"],
            "bill_amount": bill["amount"],
            "bill_currency": bill["currency"],
            "bill_date": bill["date"],
            "bill_number": bill["file"],
            "status": "matched",
            "match_score": overall,
            "confidence": {
                "vendor": vc,
                "amount": ac,
                "date": dc,
                "overall": overall,
            },
            "cc_transaction_id": cc.get("transaction_id", ""),
            "cc_description": cc.get("description", ""),
            "cc_inr_amount": cc.get("amount", 0),
            "cc_date": cc.get("date", ""),
            "cc_card": cc.get("card_name", ""),
        }
        if cc.get("forex_amount"):
            entry["cc_forex_amount"] = cc["forex_amount"]
            entry["cc_forex_currency"] = cc["forex_currency"]
        matches.append(entry)

    # Unmatched bills
    for bi, bill in enumerate(bills):
        if bill_matched[bi]:
            continue
        matches.append({
            "bill_id": bill["bill_id"],
            "vendor_id": bill["vendor_id"],
            "vendor_name": bill["vendor_name"],
            "bill_amount": bill["amount"],
            "bill_currency": bill["currency"],
            "bill_date": bill["date"],
            "bill_number": bill["file"],
            "status": "unmatched",
        })

    return matches
```

**Step 2: Update `api_payments_preview` in `app.py` to call the new function**

In `app.py`, inside `api_payments_preview()` (around line 943), replace lines 943-1046 (the old matching loop from "# --- Resolve CC vendor names ---" through "# Collect unmatched CC transactions") with:

```python
        # --- Load learned vendor mappings ---
        from scripts.utils import load_learned_vendor_mappings
        learned = load_learned_vendor_mappings()
        learned_map = learned.get("mappings", {})

        # --- Vendor-gated matching ---
        matches = _build_vendor_gated_matches(bills, cc_list, vendor_map, learned_map)
        matched_count = sum(1 for m in matches if m["status"] == "matched")
        unmatched_count = sum(1 for m in matches if m["status"] == "unmatched")
        used_cc = set()
        for m in matches:
            if m["status"] == "matched":
                for ci, cc in enumerate(cc_list):
                    if cc.get("transaction_id") == m.get("cc_transaction_id") and cc.get("date") == m.get("cc_date"):
                        used_cc.add(ci)
                        break

        # Collect unmatched CC transactions
        unmatched_cc = [cc_list[i] for i in range(len(cc_list)) if i not in used_cc]
```

Keep everything else in the function unchanged (card counts, Amex matching, response building).

**Step 3: Run tests to verify they pass**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: All tests PASS

**Step 4: Manual smoke test**

Run: `cd /Users/daniel/products/cc && python app.py`
Open browser to the Record Payments screen. Verify:
- LinkedIn, Anthropic, Microsoft, Google matches still appear
- SHOE DEPT → Microsoft is gone
- AMAZON INDIA CYBS → R K WorldInfocom is gone (if Amazon India maps exist but R K WorldInfocom doesn't match)

**Step 5: Commit**

```bash
git add app.py tests/test_vendor_matching.py
git commit -m "feat: implement vendor-gated matching algorithm

Replaces amount-first matching with vendor-gated approach.
CC transactions without vendor signal are no longer matched.
Confidence weights: vendor 50%, amount 40%, date 10%.
Forex amounts use strict exact matching (penny tolerance)."
```

---

### Task 5: Wire up learning from Record Payment

**Files:**
- Modify: `app.py:1261-1360` (`api_payments_record_one`)

**Step 1: Write failing test**

Add to `tests/test_vendor_matching.py`:

```python
def test_record_payment_saves_learned_mapping(tmp_path):
    """Recording a payment should save CC desc → vendor to learned mappings."""
    path = str(tmp_path / "learned.json")
    with open(path, "w") as f:
        json.dump({"mappings": {}}, f)

    save_learned_vendor_mapping(
        "SOME NEW MERCHANT MUMBAI",
        "New Merchant Pvt Ltd",
        path=path,
    )

    data = load_learned_vendor_mappings(path)
    assert "SOME NEW MERCHANT MUMBAI" in data["mappings"]
    assert data["mappings"]["SOME NEW MERCHANT MUMBAI"] == "New Merchant Pvt Ltd"
```

**Step 2: Run test to verify it passes**

This test uses existing `save_learned_vendor_mapping` — it should already pass from Task 1.

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py::test_record_payment_saves_learned_mapping -v`
Expected: PASS

**Step 3: Add learning call to `api_payments_record_one`**

In `app.py`, in `api_payments_record_one()`, after the successful payment is recorded (after the line that sets `payment_id`), add:

```python
        # Learn CC description → vendor mapping for future matching
        cc_desc = data.get("cc_description", "")
        if cc_desc and bill.get("vendor_name"):
            from scripts.utils import save_learned_vendor_mapping
            save_learned_vendor_mapping(cc_desc, bill["vendor_name"])
```

Note: The frontend already sends `cc_description` in the POST payload (see `app.py:7281`).

**Step 4: Also add learning to `api_bills_create_and_record`**

In `app.py`, in `api_bills_create_and_record()`, after the successful payment block (after payment_id is confirmed), add:

```python
            # Learn CC description → vendor mapping for future matching
            cc_desc = cc.get("description", "")
            if cc_desc and vendor_name:
                from scripts.utils import save_learned_vendor_mapping
                save_learned_vendor_mapping(cc_desc, vendor_name)
```

**Step 5: Commit**

```bash
git add app.py tests/test_vendor_matching.py
git commit -m "feat: auto-learn vendor mappings from confirmed recordings and bill creation"
```

---

### Task 6: Update Amex matching to use vendor-gated logic

**Files:**
- Modify: `app.py:1064-1160` (Amex matching section)

**Step 1: Review current Amex matching code**

Read `app.py:1064-1160`. The Amex section duplicates the same matching logic. It should also use `_build_vendor_gated_matches`.

**Step 2: Replace Amex matching loop**

Replace the Amex matching loop (lines 1064-1160 approximately) with a call to `_build_vendor_gated_matches` using the same pattern as the main matching, but operating on unmatched bills only and Amex transactions.

```python
        # --- Amex CC matching (for exclude/reference only, not in Zoho) ---
        amex_matches = []
        amex_path = os.path.join(PROJECT_ROOT, "output", "amex_cc_transactions.json")
        if os.path.exists(amex_path):
            try:
                with open(amex_path, "r", encoding="utf-8") as f:
                    amex_txns = json.load(f)
                amex_list = [t for t in amex_txns if float(t.get("amount", 0)) > 0]

                # Only match bills that weren't matched in main pass
                unmatched_bills = [bills[bi] for bi in range(len(bills)) if not bill_matched_flags[bi]]
                amex_results = _build_vendor_gated_matches(unmatched_bills, amex_list, vendor_map, learned_map)
                amex_matches = [m for m in amex_results if m["status"] == "matched"]
            except Exception as e:
                log_action(f"Amex matching error: {e}", "WARNING")
```

Note: You'll need to track `bill_matched_flags` from the main matching results. Add after the main matching:

```python
        bill_matched_flags = [False] * len(bills)
        matched_bill_ids = {m["bill_id"] for m in matches if m["status"] == "matched"}
        for bi, bill in enumerate(bills):
            if bill["bill_id"] in matched_bill_ids:
                bill_matched_flags[bi] = True
```

**Step 3: Run full test suite**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: All tests PASS

**Step 4: Manual smoke test**

Verify Amex matches still appear in the UI for unmatched bills.

**Step 5: Commit**

```bash
git add app.py
git commit -m "refactor: unify Amex matching with vendor-gated algorithm"
```

---

### Task 7: Update USD exchange rate range and clean up old code

**Files:**
- Modify: `app.py:870-874` (old `_amount_diff` inside `api_payments_preview` — remove dead code)
- Modify: `app.py:948-993` (old matching loop — remove dead code)

**Step 1: Remove the old inline `_amount_diff`, `_vendor_match`, `_resolve_vendor`, and `_compute_confidence` functions**

These are now encapsulated in `_build_vendor_gated_matches`. The old functions inside `api_payments_preview` (lines 807-941 approximately) should be removed since they're no longer called by the matching loop.

**However** — check if `_resolve_vendor` is still used elsewhere in the same function (e.g., for Amex vendor resolution at line 1074). If so, keep it or ensure Amex also uses the new function.

**Step 2: Run tests**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: All PASS

**Step 3: Start the app and do a full end-to-end test**

Run: `cd /Users/daniel/products/cc && python app.py`

Verify on the Record Payments screen:
1. Correct matches still appear (LinkedIn, Anthropic, Microsoft, Google, GitHub)
2. False matches are gone (SHOE→Microsoft, AMAZON CYBS→R K WorldInfocom)
3. Confidence scores use new weights (50/40/10)
4. Clicking "Record" on a match successfully records and saves learned mapping

**Step 4: Commit**

```bash
git add app.py
git commit -m "refactor: remove old matching code, replaced by vendor-gated algorithm"
```

---

### Task 8: Add edge case tests and final verification

**Files:**
- Modify: `tests/test_vendor_matching.py` (edge case tests)

**Step 1: Add edge case tests**

```python
def test_inr_amount_tolerance():
    """INR amounts within 1% should match."""
    bills = [_make_bill("Google", 564.17)]
    cc = [_make_cc("GOOGLEWORKSP, MUMBAI", 564.17)]
    vendor_map = {"googleworksp": "Google"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1


def test_usd_estimate_range_80_95():
    """USD bill without forex tag: INR must be within bill*80 to bill*95."""
    bills = [_make_bill("Atlassian", 64.07, currency="USD")]

    # INR = 64.07 * 87 = 5574.09 (within 80-95 range)
    cc_good = [_make_cc("ATLASSIAN AMSTERDAM", 5574.09)]
    vendor_map = {"atlassian amsterdam": "Atlassian"}
    matches = _build_vendor_gated_matches(bills, cc_good, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1

    # INR = 64.07 * 100 = 6407.00 (outside 80-95 range)
    cc_bad = [_make_cc("ATLASSIAN AMSTERDAM", 6407.00)]
    matches2 = _build_vendor_gated_matches(bills, cc_bad, vendor_map, {})
    matched2 = [m for m in matches2 if m["status"] == "matched"]
    assert len(matched2) == 0


def test_multiple_bills_best_match_wins():
    """When multiple bills match same vendor, best amount match wins."""
    bills = [
        _make_bill("Microsoft Corporation (India) Pvt Ltd", 12215.38, bill_id="B1"),
        _make_bill("Microsoft Corporation (India) Pvt Ltd", 42116.85, bill_id="B2"),
    ]
    cc = [
        _make_cc("MICROSOFTBUS, MUMBAI", 12215.38, date="2025-07-18"),
        _make_cc("MICROSOFTBUS, MUMBAI", 42116.85, date="2025-07-18"),
    ]
    vendor_map = {"microsoftbus": "Microsoft"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 2
    # Each bill matched to its correct CC amount
    for m in matched:
        assert abs(m["bill_amount"] - m["cc_inr_amount"]) < 1.0


def test_date_over_60_days_rejected():
    """Matches beyond 60 days should be rejected even with vendor+amount match."""
    bills = [_make_bill("LinkedIn Singapore Pte Ltd", 7106.00, date="2025-01-01")]
    cc = [_make_cc("IND*LINKEDIN (PGSI)", 7106.00, date="2025-04-01")]
    vendor_map = {"ind*linkedin": "LinkedIn"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 0
```

**Step 2: Run all tests**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/test_vendor_matching.py
git commit -m "test: add edge case tests for vendor-gated matching"
```
