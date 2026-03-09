# Matching Algorithm Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 5 matching algorithm issues: normalize substring matching, parse embedded USD, add unmatched diagnostics, historical forex rate lookup, and multi-bill grouping for split invoices.

**Architecture:** All fixes live in the existing vendor-gated matching pipeline in `app.py:_build_vendor_gated_matches`. Fix 4 adds a forex utility in `scripts/utils.py`. Fix 5 adds a second matching pass after the 1:1 greedy pass. UI changes extend the existing JavaScript in `app.py`'s HTML template.

**Tech Stack:** Python 3, Flask, `urllib.request` (for forex API — no new dependencies), pytest.

---

### Task 1: Fix 1 — Normalize Substring Matching

**Files:**
- Modify: `app.py:92-94` (inside `_resolve_vendor` in `_build_vendor_gated_matches`)
- Test: `tests/test_vendor_matching.py`

**Step 1: Write the failing test**

Add to `tests/test_vendor_matching.py`:

```python
def test_special_chars_in_description_still_match():
    """Kotak descriptions with non-printable chars should match via normalization."""
    bills = [_make_bill("Anthropic USD", 4216.45)]
    # \ufffd simulates the replacement chars in Kotak card descriptions
    cc = [_make_cc("CLAUDE.AI\ufffdSUBSCRIPTION\ufffdANTHROPIC.COM\ufffdCA", 4216.45)]
    vendor_map = {"claude.ai subscription": "Anthropic USD"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vendor_matching.py::test_special_chars_in_description_still_match -v`
Expected: FAIL — the `\ufffd` chars prevent `"claude.ai subscription"` from matching in `dl` via substring.

**Step 3: Write minimal implementation**

In `app.py`, inside `_build_vendor_gated_matches`, in `_resolve_vendor`, add `dl_clean` and use it in the lowercased substring loop. Change lines 79-97 to:

```python
    def _resolve_vendor(desc):
        if not desc:
            return None, None
        dl = desc.lower()
        dn = _norm(desc)
        du = desc.strip().upper()
        dl_clean = "".join(c for c in dl if c.isalnum() or c == ' ')

        if dl in vm_lower:
            return vm_lower[dl], "manual"
        if dn in vm_norm:
            return vm_norm[dn], "manual"
        for key in sorted_keys:
            if key and len(key) >= 4 and (key in dl or key in dl_clean):
                return vm_lower[key], "manual"
        for key in sorted_norm_keys:
            if key and len(key) >= 4 and key in dn:
                return vm_norm[key], "manual"
```

The key change: add `dl_clean` (lowercase, non-alnum stripped except spaces) and add `or key in dl_clean` in the sorted_keys loop.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_vendor_matching.py::test_special_chars_in_description_still_match -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `python -m pytest tests/test_vendor_matching.py -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add app.py tests/test_vendor_matching.py
git commit -m "fix: normalize special chars in CC description substring matching"
```

---

### Task 2: Fix 2 — Parse Embedded USD from CC Description

**Files:**
- Modify: `app.py:41-248` (inside `_build_vendor_gated_matches`)
- Test: `tests/test_vendor_matching.py`

**Step 1: Write the failing tests**

Add to `tests/test_vendor_matching.py`:

```python
def test_parse_usd_from_description_boosts_confidence():
    """CC description with embedded USD amount should use forex exact matching."""
    bills = [_make_bill("Medium", 5.00, currency="USD")]
    cc = [_make_cc("MEDIUM MONTHLY MEDIUM.COM CA USD 5.00", 438.70)]
    vendor_map = {"medium": "Medium"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    assert matched[0]["confidence"]["amount"] == 100


def test_parse_usd_brackets():
    """USD in square brackets: [USD 5.43]."""
    bills = [_make_bill("GitHub, Inc.", 5.43, currency="USD")]
    cc = [_make_cc("GITHUB, INC.GITHUB.COM USD 5.43 [USD 5.43]", 492.84)]
    vendor_map = {"github": "GitHub, Inc."}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    assert matched[0]["confidence"]["amount"] == 100


def test_parse_usd_parentheses():
    """USD in parentheses: (USD 200.00)."""
    bills = [_make_bill("Anthropic USD", 200.00, currency="USD")]
    cc = [_make_cc("CLAUDE.AI SUBSCRIPTION SAN FRANCISCO (USD 200.00)", 18171.91)]
    vendor_map = {"claude.ai subscription": "Anthropic USD"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    assert matched[0]["confidence"]["amount"] == 100


def test_parse_usd_no_override_existing_forex():
    """If forex_amount already set by bank, don't override with parsed value."""
    bills = [_make_bill("GitHub, Inc.", 104.00, currency="USD")]
    cc = [_make_cc("GITHUB, INC. USD 104.00", 9551.62,
                   forex_amount=104.00, forex_currency="USD")]
    vendor_map = {"github": "GitHub, Inc."}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    assert matched[0]["confidence"]["amount"] == 100
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_vendor_matching.py -k "parse_usd" -v`
Expected: First 3 FAIL (amount confidence will be 40 via Mode C estimation, not 100).

**Step 3: Write minimal implementation**

In `app.py`, inside `_build_vendor_gated_matches`, after the imports (line 55), add:

```python
    import re
    _USD_RE = re.compile(r'USD\s*([\d,]+\.?\d*)')

    def _enrich_forex(cc):
        """If CC has no forex_amount, try to parse USD from description."""
        if cc.get("forex_amount"):
            return
        desc = cc.get("description", "")
        m = _USD_RE.search(desc)
        if m:
            try:
                amt = float(m.group(1).replace(",", ""))
                if amt > 0:
                    cc["forex_amount"] = amt
                    cc["forex_currency"] = "USD"
                    cc["forex_parsed"] = True
            except (ValueError, TypeError):
                pass
```

Then right before `# --- Resolve all CC vendors ---` (line 245), add:

```python
    # --- Enrich CC entries with parsed forex amounts ---
    for cc in cc_list:
        _enrich_forex(cc)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vendor_matching.py -k "parse_usd" -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `python -m pytest tests/test_vendor_matching.py -v`
Expected: All tests PASS. Verify `test_usd_estimate_range_80_95` still passes (no USD in that description).

**Step 6: Commit**

```bash
git add app.py tests/test_vendor_matching.py
git commit -m "feat: parse embedded USD from CC description for forex exact matching"
```

---

### Task 3: Fix 3 — Unmatched Reason Diagnostic

**Files:**
- Modify: `app.py:1111-1175` (in `api_payments_preview`, after matching pass)
- Modify: `app.py:7131-7134` (JavaScript `cc_only` confidence cell)

**Step 1: Add diagnostic to unmatched CC in backend**

In `app.py`, in `api_payments_preview`, after `unmatched_cc = [cc_list[i] ...]` (line 1124), add diagnostic resolution. Create a helper that resolves vendor and explains why match failed:

```python
        # --- Diagnostic: explain why unmatched CC txns didn't match ---
        from datetime import datetime as _dt_diag
        _norm_d = lambda s: "".join(c for c in s.lower() if c.isalnum())
        vm_lower_d = {k.lower(): v for k, v in vendor_map.items()}
        vm_norm_d = {_norm_d(k): v for k, v in vendor_map.items()}
        sorted_keys_d = sorted(vm_lower_d.keys(), key=len, reverse=True)

        def _diag_resolve(desc):
            if not desc:
                return None
            dl = desc.lower()
            dn = _norm_d(desc)
            dl_c = "".join(c for c in dl if c.isalnum() or c == ' ')
            if dl in vm_lower_d:
                return vm_lower_d[dl]
            if dn in vm_norm_d:
                return vm_norm_d[dn]
            for key in sorted_keys_d:
                if key and len(key) >= 4 and (key in dl or key in dl_c):
                    return vm_lower_d[key]
            du = desc.strip().upper()
            if du in learned_map:
                return learned_map[du]
            for key in sorted(learned_map.keys(), key=len, reverse=True):
                if key and len(key) >= 4 and key in du:
                    return learned_map[key]
            return None

        unmatched_bill_list = [bills[bi] for bi in range(len(bills))
                               if bills[bi]["bill_id"] not in matched_bill_ids]
        for cc_item in unmatched_cc:
            resolved = _diag_resolve(cc_item.get("description", ""))
            if not resolved:
                cc_item["unmatched_reason"] = "No vendor signal"
                continue
            cc_item["resolved_vendor"] = resolved
            cc_inr = float(cc_item.get("amount", 0))
            best = None
            for ub in unmatched_bill_list:
                rv, bv = _norm_d(resolved), _norm_d(ub.get("vendor_name", ""))
                if not (rv in bv or bv in rv or rv == bv):
                    continue
                try:
                    bd = _dt_diag.strptime(ub["date"], "%Y-%m-%d")
                    cd = _dt_diag.strptime(cc_item["date"], "%Y-%m-%d")
                    dd = abs((bd - cd).days)
                except Exception:
                    dd = 9999
                if dd > 60:
                    if not best:
                        best = f"Date: {dd}d apart"
                    continue
                bill_amt = float(ub.get("amount", 0))
                bill_cur = ub.get("currency", "INR")
                if bill_cur == "INR":
                    diff_pct = abs(cc_inr - bill_amt) / max(bill_amt, 1) * 100
                    if diff_pct > 1:
                        best = f"Amt: CC {cc_inr:,.0f} vs Bill {bill_amt:,.0f}"
                    else:
                        best = "Greedy: used by better match"
                        break
                elif bill_cur == "USD":
                    implied = cc_inr / max(bill_amt, 0.01)
                    if implied < 70 or implied > 100:
                        best = f"Amt: CC {cc_inr:,.0f} vs ${bill_amt:,.2f} (rate {implied:.0f})"
                    else:
                        best = "Greedy: used by better match"
                        break
            cc_item["unmatched_reason"] = best or f"No {resolved} bills"
```

**Step 2: Update UI to display reason**

In `app.py` JavaScript, change the `cc_only` branch (around line 7131-7134):

From:
```javascript
      confCell = '<span style="color:var(--accent);font-size:10px">No Invoice</span>';
```

To:
```javascript
      var reason = m.unmatched_reason || 'No Invoice';
      var rVendor = m.resolved_vendor || '';
      confCell = '<div style="text-align:center;line-height:1.3">'
        + '<span style="color:var(--accent);font-size:10px">No Invoice</span>'
        + (rVendor ? '<div style="font-size:8px;color:var(--text-dim)" title="Resolved to: ' + rVendor + '">\u2192 ' + rVendor + '</div>' : '')
        + '<div style="font-size:8px;color:var(--yellow);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + reason + '">' + reason + '</div>'
        + '</div>';
```

**Step 3: Test manually via Chrome**

Reload Record Payment page. "No Invoice — CC Only" rows should now show resolved vendor and mismatch reason.

**Step 4: Commit**

```bash
git add app.py
git commit -m "feat: show unmatched reason diagnostic for CC txns without bill match"
```

---

### Task 4: Fix 4 — Historical Forex Rate Lookup

**Files:**
- Create: `config/forex_cache.json`
- Modify: `scripts/utils.py` (add forex functions)
- Modify: `app.py:41` (`_build_vendor_gated_matches` signature)
- Modify: `app.py:152-220` (`_amount_diff` and `_amount_conf`)
- Modify: `app.py:1110-1112` (`api_payments_preview` — prefetch + pass rates)
- Modify: `app.py:7127-7130` (JavaScript — rate display)
- Test: `tests/test_vendor_matching.py`

**Step 1: Create empty forex cache**

```bash
echo '{}' > config/forex_cache.json
```

**Step 2: Add forex utilities to scripts/utils.py**

Add after `save_learned_vendor_mapping`:

```python
def load_forex_cache(path="config/forex_cache.json"):
    full_path = os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(full_path):
        return {}
    try:
        with open(full_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_forex_cache(cache, path="config/forex_cache.json"):
    full_path = os.path.join(PROJECT_ROOT, path)
    with open(full_path, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def fetch_forex_rate(date_str, from_cur="USD", to_cur="INR", cache=None):
    cache_key = f"{from_cur}_{to_cur}"
    if cache and date_str in cache and cache_key in cache[date_str]:
        return cache[date_str][cache_key]
    try:
        import urllib.request
        url = f"https://api.frankfurter.app/{date_str}?from={from_cur}&to={to_cur}"
        req = urllib.request.Request(url, headers={"User-Agent": "cc-automation/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        rate = data.get("rates", {}).get(to_cur)
        if rate and cache is not None:
            if date_str not in cache:
                cache[date_str] = {}
            cache[date_str][cache_key] = rate
        return rate
    except Exception:
        return None


def prefetch_forex_rates(dates, from_cur="USD", to_cur="INR"):
    cache = load_forex_cache()
    cache_key = f"{from_cur}_{to_cur}"
    missing = [d for d in set(dates) if d and (d not in cache or cache_key not in cache.get(d, {}))]
    if missing:
        for date_str in sorted(missing):
            fetch_forex_rate(date_str, from_cur, to_cur, cache)
        save_forex_cache(cache)
    return cache
```

**Step 3: Write tests for forex utilities**

Add to `tests/test_vendor_matching.py`:

```python
def test_forex_cache_roundtrip(tmp_path):
    from scripts.utils import load_forex_cache, save_forex_cache
    path = str(tmp_path / "forex.json")
    cache = {"2025-10-18": {"USD_INR": 87.52}}
    save_forex_cache(cache, path=path)
    loaded = load_forex_cache(path=path)
    assert loaded["2025-10-18"]["USD_INR"] == 87.52


def test_forex_cache_missing_file(tmp_path):
    from scripts.utils import load_forex_cache
    assert load_forex_cache(path=str(tmp_path / "nope.json")) == {}
```

Run: `python -m pytest tests/test_vendor_matching.py -k "forex_cache" -v`
Expected: PASS

**Step 4: Write failing test for forex-based amount matching**

```python
def test_forex_rate_mode_c_with_actual_rate():
    """USD bill + INR CC without forex tag: use actual forex rate for confidence."""
    bills = [_make_bill("Medium", 5.00, currency="USD", date="2025-05-02")]
    # INR 438.70 / $5.00 = rate 87.74. No forex metadata, no USD in description.
    cc = [_make_cc("MEDIUM MONTHLY MEDIUM.COM CA", 438.70, date="2025-05-02")]
    vendor_map = {"medium": "Medium"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {},
                                          forex_rates={"2025-05-02": {"USD_INR": 87.74}})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1
    # Deviation = 0% → Amt should be 100
    assert matched[0]["confidence"]["amount"] >= 95
```

Run: `python -m pytest tests/test_vendor_matching.py::test_forex_rate_mode_c_with_actual_rate -v`
Expected: FAIL — `forex_rates` param not accepted yet.

**Step 5: Implement forex-based Mode C**

a) Add `forex_rates=None` to `_build_vendor_gated_matches` signature (line 41):

```python
def _build_vendor_gated_matches(bills, cc_list, manual_vendor_map, learned_vendor_map, forex_rates=None):
```

b) Replace Mode C in `_amount_diff` (lines 183-193). Replace the `if bill_cur == "USD" and not fx:` block with:

```python
        # Mode C: Foreign currency bill, no forex tag — use actual rate or estimate
        if bill_cur != "INR" and not fx:
            if bill_amt <= 0:
                return None, None
            implied_rate = cc_inr / bill_amt
            cc_date = cc.get("date", "")
            rate_key = f"{bill_cur}_INR"
            actual_rate = None
            if forex_rates and cc_date in forex_rates:
                actual_rate = forex_rates[cc_date].get(rate_key)

            if actual_rate:
                deviation = abs(implied_rate - actual_rate) / actual_rate
                if deviation > 0.05:
                    return None, None
                diff = deviation * bill_amt
                return diff, f"{bill_cur} rate:{implied_rate:.2f} actual:{actual_rate:.2f}"
            else:
                # Fallback: no actual rate — estimate for USD only
                if bill_cur == "USD":
                    if bill_amt * 80 <= cc_inr <= bill_amt * 95:
                        diff = abs(cc_inr - bill_amt * 87.5)
                        return diff, f"{bill_cur} \u2192 INR (est)"
                return None, None
```

c) Update `_amount_conf` to handle rate-based matches. Replace the function (lines 196-220):

```python
    def _amount_conf(bill, cc):
        diff, mtype = _amount_diff(bill, cc)
        if diff is None:
            return 0, None, None
        bill_amt = bill["amount"] if bill["amount"] else 1
        if mtype and "actual:" in mtype:
            pct_dev = diff / bill_amt if bill_amt else 0
            if pct_dev < 0.005: conf = 100
            elif pct_dev < 0.01: conf = 95
            elif pct_dev < 0.02: conf = 90
            elif pct_dev < 0.03: conf = 75
            elif pct_dev < 0.05: conf = 60
            else: conf = 40
            return conf, diff, mtype
        pct_diff = diff / bill_amt
        if mtype and "exact" in mtype:
            conf = 100
        elif pct_diff < 0.001: conf = 100
        elif pct_diff < 0.005: conf = 95
        elif pct_diff < 0.01: conf = 90
        elif pct_diff < 0.03: conf = 75
        elif pct_diff < 0.05: conf = 60
        else: conf = 40
        if mtype and "est" in mtype:
            conf = min(conf, 70)
        return conf, diff, mtype
```

**Step 6: Run tests**

Run: `python -m pytest tests/test_vendor_matching.py -v`
Expected: All PASS.

**Step 7: Wire prefetch into api_payments_preview**

In `app.py`, in `api_payments_preview`, before the `_build_vendor_gated_matches` call (around line 1110), add:

```python
        from scripts.utils import prefetch_forex_rates
        cc_dates = list(set(cc.get("date", "") for cc in cc_list if cc.get("date")))
        forex_cache = prefetch_forex_rates(cc_dates)
```

Pass `forex_rates=forex_cache` to both `_build_vendor_gated_matches` calls (main + Amex).

**Step 8: Add match_type to response and rate display in UI**

In `app.py`, in the matched entry dict building (around line 318), add:

```python
        # Include match info for UI
        _, _, mtype_display = _amount_conf(bill, cc)
        if mtype_display:
            entry["match_type"] = mtype_display
        if cc.get("forex_parsed"):
            entry["forex_parsed"] = True
```

In JavaScript confidence cell (around line 7129), after the Vendor/Amt/Date line, add:

```javascript
        + (m.match_type && m.match_type.indexOf('rate:') >= 0
           ? '<div style="font-size:8px;color:var(--text-dim)">'
             + m.match_type.replace(/.*rate:([\d.]+)\s*actual:([\d.]+).*/, 'Rate: \u20B9$1/$ (actual: \u20B9$2)')
             + '</div>' : '')
        + (m.match_type && m.match_type.indexOf('est') >= 0
           ? '<div style="font-size:8px;color:var(--yellow)">\u26A0 Est. rate</div>' : '')
```

**Step 9: Commit**

```bash
git add app.py scripts/utils.py config/forex_cache.json tests/test_vendor_matching.py
git commit -m "feat: historical forex rate lookup for USD bill matching"
```

---

### Task 5: Fix 5 — Multi-Bill Grouping for Split Invoices

**Files:**
- Modify: `config/vendor_mappings.json` (add `multi_bill_vendors`)
- Modify: `app.py` (add `_build_group_matches` after `_build_vendor_gated_matches`)
- Modify: `app.py:1112-1175` (`api_payments_preview` — call group matching)
- Modify: `app.py` JavaScript (render group matches section)
- Test: `tests/test_vendor_matching.py`

**Step 1: Add multi_bill_vendors to config**

In `config/vendor_mappings.json`, add after `"default_tax_treatment": "out_of_scope"` (line 113):

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
    ],
```

**Step 2: Write failing tests**

Add to `tests/test_vendor_matching.py`:

```python
def test_group_match_exact_sum():
    """Three Amazon bills summing to one CC transaction should group-match."""
    from app import _build_group_matches
    bills = [
        _make_bill("Amazon India", 4200.00, date="2025-10-13", bill_id="B1"),
        _make_bill("Amazon India", 3544.10, date="2025-10-14", bill_id="B2"),
        _make_bill("Amazon India", 2000.00, date="2025-10-14", bill_id="B3"),
    ]
    cc = [_make_cc("AMAZON PAY INDIA PRIVA, Bangalore", 9744.10, date="2025-10-14")]
    vendor_map = {"amazon pay india priva": "Amazon India"}
    results = _build_group_matches(bills, cc, vendor_map, {}, ["Amazon India"])
    assert len(results) == 1
    assert results[0]["status"] == "group_matched"
    assert len(results[0]["grouped_bills"]) == 3
    assert abs(sum(b["amount"] for b in results[0]["grouped_bills"]) - 9744.10) < 1.0


def test_group_match_partial_sum():
    """If only 2 of 3 bills sum to CC amount, use the 2-bill group."""
    from app import _build_group_matches
    bills = [
        _make_bill("Amazon India", 5000.00, date="2025-10-14", bill_id="B1"),
        _make_bill("Amazon India", 4744.10, date="2025-10-14", bill_id="B2"),
        _make_bill("Amazon India", 9000.00, date="2025-10-14", bill_id="B3"),
    ]
    cc = [_make_cc("AMAZON PAY INDIA PRIVA, Bangalore", 9744.10, date="2025-10-14")]
    vendor_map = {"amazon pay india priva": "Amazon India"}
    results = _build_group_matches(bills, cc, vendor_map, {}, ["Amazon India"])
    assert len(results) == 1
    assert len(results[0]["grouped_bills"]) == 2


def test_group_match_non_eligible_vendor_skipped():
    """Vendors not in multi_bill_vendors should not be group-matched."""
    from app import _build_group_matches
    bills = [
        _make_bill("Medium", 200.00, date="2025-10-14", bill_id="B1"),
        _make_bill("Medium", 238.70, date="2025-10-14", bill_id="B2"),
    ]
    cc = [_make_cc("MEDIUM MONTHLY", 438.70, date="2025-10-14")]
    vendor_map = {"medium": "Medium"}
    results = _build_group_matches(bills, cc, vendor_map, {}, ["Amazon India"])
    assert len(results) == 0


def test_group_match_date_window():
    """Bills outside +/-5 day window excluded from groups."""
    from app import _build_group_matches
    bills = [
        _make_bill("Amazon India", 5000.00, date="2025-10-14", bill_id="B1"),
        _make_bill("Amazon India", 4744.10, date="2025-10-25", bill_id="B2"),
    ]
    cc = [_make_cc("AMAZON PAY INDIA PRIVA, Bangalore", 9744.10, date="2025-10-14")]
    vendor_map = {"amazon pay india priva": "Amazon India"}
    results = _build_group_matches(bills, cc, vendor_map, {}, ["Amazon India"])
    assert len(results) == 0


def test_group_match_max_5_bills():
    """Group should contain at most 5 bills."""
    from app import _build_group_matches
    bills = [_make_bill("Amazon India", 100.00, date="2025-10-14", bill_id=f"B{i}") for i in range(8)]
    cc = [_make_cc("AMAZON PAY INDIA PRIVA, Bangalore", 500.00, date="2025-10-14")]
    vendor_map = {"amazon pay india priva": "Amazon India"}
    results = _build_group_matches(bills, cc, vendor_map, {}, ["Amazon India"])
    assert len(results) == 1
    assert len(results[0]["grouped_bills"]) == 5
```

Run: `python -m pytest tests/test_vendor_matching.py -k "group_match" -v`
Expected: FAIL — `_build_group_matches` doesn't exist.

**Step 3: Implement _build_group_matches**

In `app.py`, add after `_build_vendor_gated_matches` (after line 340):

```python
def _build_group_matches(bills, cc_list, manual_vendor_map, learned_vendor_map,
                         multi_bill_vendors, forex_rates=None,
                         used_bill_ids=None, used_cc_ids=None):
    """Second-pass: group multiple bills to one CC transaction for eligible vendors."""
    from datetime import datetime as _dt
    from scripts.utils import strip_vendor_stop_words

    if not multi_bill_vendors:
        return []

    def _norm(s):
        return "".join(c for c in s.lower() if c.isalnum())

    vm_lower = {k.lower(): v for k, v in manual_vendor_map.items()}
    vm_norm = {_norm(k): v for k, v in manual_vendor_map.items()}
    sorted_keys = sorted(vm_lower.keys(), key=len, reverse=True)

    def _resolve_quick(desc):
        if not desc:
            return None
        dl = desc.lower()
        dn = _norm(desc)
        dl_c = "".join(c for c in dl if c.isalnum() or c == ' ')
        if dl in vm_lower: return vm_lower[dl]
        if dn in vm_norm: return vm_norm[dn]
        for key in sorted_keys:
            if key and len(key) >= 4 and (key in dl or key in dl_c):
                return vm_lower[key]
        du = desc.strip().upper()
        if du in learned_vendor_map: return learned_vendor_map[du]
        for key in sorted(learned_vendor_map.keys(), key=len, reverse=True):
            if key and len(key) >= 4 and key in du:
                return learned_vendor_map[key]
        return None

    def _vendor_match(resolved, bill_vendor):
        rv, bv = _norm(resolved), _norm(bill_vendor)
        rv_s = _norm(strip_vendor_stop_words(resolved))
        bv_s = _norm(strip_vendor_stop_words(bill_vendor))
        if rv == bv or rv_s == bv_s: return 100
        if len(rv) >= 4 and (rv in bv or bv in rv): return 80
        if len(rv_s) >= 4 and (rv_s in bv_s or bv_s in rv_s): return 80
        rv_f = _norm(resolved.split()[0]) if resolved.split() else ""
        if rv_f and len(rv_f) >= 4 and rv_f in bv: return 60
        return 0

    multi_norm = set(_norm(v) for v in multi_bill_vendors)
    used_bill_ids = used_bill_ids or set()
    used_cc_ids = used_cc_ids or set()

    avail_bills = [b for b in bills if b["bill_id"] not in used_bill_ids]
    avail_cc = [c for c in cc_list if c.get("transaction_id", "") not in used_cc_ids]

    results = []
    claimed_bills = set()
    claimed_cc = set()

    for cc in avail_cc:
        cc_tid = cc.get("transaction_id", "")
        if cc_tid in claimed_cc:
            continue
        resolved = _resolve_quick(cc.get("description", ""))
        if not resolved:
            continue

        # Check eligibility: resolved vendor must match a multi_bill_vendor
        eligible = _norm(resolved) in multi_norm
        if not eligible:
            for mv in multi_bill_vendors:
                if _vendor_match(resolved, mv) >= 60:
                    eligible = True
                    break
        if not eligible:
            continue

        cc_inr = float(cc.get("amount", 0))
        if cc_inr <= 0:
            continue
        try:
            cc_date = _dt.strptime(cc.get("date", ""), "%Y-%m-%d")
        except Exception:
            continue

        # Find candidate INR bills: same vendor, within +/-5 days
        cands = []
        for b in avail_bills:
            if b["bill_id"] in claimed_bills:
                continue
            if b.get("currency", "INR") != "INR":
                continue
            vc = _vendor_match(resolved, b.get("vendor_name", ""))
            if vc < 60:
                continue
            try:
                bd = _dt.strptime(b.get("date", ""), "%Y-%m-%d")
                dd = abs((bd - cc_date).days)
            except Exception:
                continue
            if dd > 5:
                continue
            cands.append((b, dd, vc))

        if len(cands) < 2:
            continue

        # Greedy subset-sum: sort by amount desc, accumulate up to 5 bills
        cands.sort(key=lambda x: x[0]["amount"], reverse=True)
        group = []
        running = 0.0
        tol = max(1.0, cc_inr * 0.01)

        for b, dd, vc in cands:
            if len(group) >= 5:
                break
            if running + b["amount"] > cc_inr + tol:
                continue
            group.append((b, dd, vc))
            running += b["amount"]
            if abs(running - cc_inr) <= tol:
                break

        if abs(running - cc_inr) > tol or len(group) < 2:
            continue

        max_dd = max(dd for _, dd, _ in group)
        avg_vc = sum(vc for _, _, vc in group) // len(group)
        sum_diff = abs(running - cc_inr)
        sum_pct = sum_diff / cc_inr if cc_inr else 0
        ac = 100 if sum_pct < 0.001 else 95 if sum_pct < 0.005 else 90 if sum_pct < 0.01 else 75
        dc = 100 if max_dd == 0 else 90 if max_dd <= 2 else 75 if max_dd <= 5 else 50
        overall = max(0, int(avg_vc * 0.5 + ac * 0.4 + dc * 0.1) - 5)

        entry = {
            "status": "group_matched",
            "match_score": overall,
            "confidence": {"vendor": avg_vc, "amount": ac, "date": dc, "overall": overall},
            "cc_transaction_id": cc_tid,
            "cc_description": cc.get("description", ""),
            "cc_inr_amount": cc_inr,
            "cc_date": cc.get("date", ""),
            "cc_card": cc.get("card_name", ""),
            "grouped_bills": [
                {"bill_id": b["bill_id"], "vendor_id": b.get("vendor_id", ""),
                 "vendor_name": b.get("vendor_name", ""), "amount": b["amount"],
                 "currency": b.get("currency", "INR"), "date": b.get("date", ""),
                 "file": b.get("file", "")}
                for b, _, _ in group
            ],
            "group_sum": running,
            "vendor_name": group[0][0].get("vendor_name", ""),
            "vendor_id": group[0][0].get("vendor_id", ""),
        }
        results.append(entry)
        claimed_cc.add(cc_tid)
        for b, _, _ in group:
            claimed_bills.add(b["bill_id"])

    return results
```

**Step 4: Run group match tests**

Run: `python -m pytest tests/test_vendor_matching.py -k "group_match" -v`
Expected: All 5 PASS

**Step 5: Wire into api_payments_preview**

In `app.py`, in `api_payments_preview`, after unmatched CC collection (around line 1124), add:

```python
        # Multi-bill group matching (second pass)
        multi_vendors = []
        try:
            with open(vm_path, "r", encoding="utf-8") as f:
                vm_full = json.load(f)
            multi_vendors = vm_full.get("multi_bill_vendors", [])
        except Exception:
            pass

        group_matches = []
        if multi_vendors:
            group_matches = _build_group_matches(
                bills, cc_list, vendor_map, learned_map, multi_vendors,
                forex_rates=forex_cache,
                used_bill_ids=matched_bill_ids,
                used_cc_ids={m.get("cc_transaction_id") for m in matches if m["status"] == "matched"},
            )
```

Add `"group_matches": group_matches` to the response JSON.

**Step 6: Render group matches in UI JavaScript**

After the Amex table section (around line 7280 in the JavaScript), add a group matches table. Use the same table structure as Amex but with grouped bill rows. Include a stub `recordGroupMatch` function that alerts (actual multi-bill recording is out of scope for this plan).

**Step 7: Run full test suite**

Run: `python -m pytest tests/test_vendor_matching.py -v`
Expected: All tests PASS

**Step 8: Commit**

```bash
git add app.py config/vendor_mappings.json tests/test_vendor_matching.py
git commit -m "feat: multi-bill grouping for split invoices (Amazon, Flipkart, etc.)"
```

---

### Task 6: Chrome Validation

**Step 1:** Reload Record Payment page in Chrome at `http://localhost:5050`

**Step 2:** Verify:
- Medium/Claude/Fly.io matches show 92-100% (parsed USD or forex rate)
- Confidence column shows implied rate for USD matches
- "No Invoice" CC rows show resolved vendor + mismatch reason
- Group Matches section appears if multi-bill groups found
- All previously-matched rows still match (no regressions)
- Amex section unchanged

**Step 3:** Commit any UI tweaks

```bash
git add app.py
git commit -m "fix: UI polish from Chrome validation"
```
