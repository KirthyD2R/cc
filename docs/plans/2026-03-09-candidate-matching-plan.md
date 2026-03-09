# Candidate Matching for Unmatched Bills — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add amount+date based candidate matching as a fallback for "No CC Match" bills, with inline UI display, manual CC search, and bulk approval.

**Architecture:** After vendor-gated matching, a new `_find_candidates_for_unmatched()` function scores remaining CC transactions against unmatched bills by amount, date, and vendor signal. The frontend displays the top candidate inline in the empty CC columns, with expandable detail and bulk confirm via score threshold.

**Tech Stack:** Python (Flask backend), vanilla JavaScript (inline in app.py HTML), existing test framework (pytest).

**Note:** The frontend uses innerHTML throughout (internal tool, all data from own backend). New code follows the same established pattern.

---

### Task 1: Candidate scoring function — failing tests

**Files:**
- Modify: `tests/test_vendor_matching.py` (append new tests)

**Step 1: Write failing tests for `_find_candidates_for_unmatched`**

Add to end of `tests/test_vendor_matching.py`:

```python
# --- Candidate matching tests ---

from app import _find_candidates_for_unmatched


def test_candidate_exact_amount_date():
    """Unmatched bill finds CC with exact amount and close date."""
    unmatched_bills = [_make_bill("Medium", 5.00, currency="USD", date="2025-02-02")]
    cc_only = [_make_cc("MEDIUM, SAN FRANCISCO", 435.00, date="2025-02-01",
                        forex_amount=5.00, forex_currency="USD")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    assert len(results) == 1
    assert len(results[0]["candidates"]) == 1
    cand = results[0]["candidates"][0]
    assert cand["cc_description"] == "MEDIUM, SAN FRANCISCO"
    assert cand["candidate_score"] >= 70


def test_candidate_no_match_beyond_5pct():
    """CC amount >5% off should not appear as candidate."""
    unmatched_bills = [_make_bill("SomeVendor", 1000.00, date="2025-07-15")]
    cc_only = [_make_cc("RANDOM TXN", 1200.00, date="2025-07-16")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    assert len(results) == 1
    assert len(results[0]["candidates"]) == 0


def test_candidate_no_match_beyond_60_days():
    """CC >60 days from bill should not appear as candidate."""
    unmatched_bills = [_make_bill("SomeVendor", 500.00, date="2025-01-01")]
    cc_only = [_make_cc("SOMEVENDOR TXN", 500.00, date="2025-04-01")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    assert len(results) == 1
    assert len(results[0]["candidates"]) == 0


def test_candidate_vendor_signal_boosts_score():
    """Candidate with vendor name in CC description scores higher."""
    unmatched_bills = [_make_bill("New Relic", 10.00, currency="USD", date="2025-01-31")]
    cc_with_signal = _make_cc("NRI*NEW RELIC INC", 870.00, date="2025-01-30",
                              forex_amount=10.00, forex_currency="USD")
    cc_without_signal = _make_cc("RANDOM MERCHANT", 870.00, date="2025-01-30",
                                 forex_amount=10.00, forex_currency="USD")
    cc_without_signal["transaction_id"] = "T2"
    results = _find_candidates_for_unmatched(unmatched_bills, [cc_with_signal, cc_without_signal])
    assert len(results[0]["candidates"]) == 2
    # Candidate with vendor signal should rank first
    assert "NEW RELIC" in results[0]["candidates"][0]["cc_description"].upper()


def test_candidate_uniqueness_bonus():
    """Single candidate at matching amount gets uniqueness bonus."""
    unmatched_bills = [_make_bill("InfoEdge", 8761.50, date="2025-03-03")]
    cc_only = [_make_cc("INFO EDGE NOWCREE", 8761.50, date="2025-03-02")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    cand = results[0]["candidates"][0]
    assert cand["breakdown"]["uniqueness"] == 15


def test_candidate_multiple_candidates_no_bonus():
    """Multiple candidates at similar amount get no uniqueness bonus."""
    unmatched_bills = [_make_bill("SomeVendor", 500.00, date="2025-07-15")]
    cc1 = _make_cc("VENDOR A", 500.00, date="2025-07-15")
    cc2 = _make_cc("VENDOR B", 502.00, date="2025-07-14")
    cc2["transaction_id"] = "T2"
    cc3 = _make_cc("VENDOR C", 498.00, date="2025-07-16")
    cc3["transaction_id"] = "T3"
    results = _find_candidates_for_unmatched(unmatched_bills, [cc1, cc2, cc3])
    for cand in results[0]["candidates"]:
        assert cand["breakdown"]["uniqueness"] <= 0


def test_candidate_top5_limit():
    """At most 5 candidates returned per bill."""
    unmatched_bills = [_make_bill("SomeVendor", 100.00, date="2025-07-15")]
    cc_list = []
    for i in range(10):
        cc = _make_cc(f"TXN {i}", 100.00 + i * 0.5, date="2025-07-15")
        cc["transaction_id"] = f"T{i}"
        cc_list.append(cc)
    results = _find_candidates_for_unmatched(unmatched_bills, cc_list)
    assert len(results[0]["candidates"]) <= 5


def test_candidate_forex_direct_comparison():
    """USD bill matches CC with forex_amount directly for higher confidence."""
    unmatched_bills = [_make_bill("S2 Labs Inc.", 30.00, currency="USD", date="2025-09-29")]
    cc_only = [_make_cc("WINDSURF.COM", 2610.00, date="2025-09-28",
                        forex_amount=30.00, forex_currency="USD")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    cand = results[0]["candidates"][0]
    assert cand["breakdown"]["amount"] == 100
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -k "candidate" -v`
Expected: FAIL — `ImportError: cannot import name '_find_candidates_for_unmatched'`

**Step 3: Commit failing tests**

```bash
git add tests/test_vendor_matching.py
git commit -m "test: add failing tests for candidate matching engine"
```

---

### Task 2: Implement `_find_candidates_for_unmatched` function

**Files:**
- Modify: `app.py:391` (insert new function after `_build_vendor_gated_matches`, before `_build_group_matches`)

**Step 1: Implement the function**

Insert after line 391 (the `return matches` of `_build_vendor_gated_matches`):

```python
def _find_candidates_for_unmatched(unmatched_bills, cc_only_list, forex_rates=None):
    """Find candidate CC transactions for unmatched bills using amount+date scoring.

    Runs AFTER vendor-gated matching as a fallback for bills with no vendor match.
    Scores each (bill, cc) pair by amount proximity, date proximity, vendor name
    overlap, and uniqueness. Returns list with 'candidates' array (top 5) per bill.
    """
    from datetime import datetime as _dt

    results = []
    for bill in unmatched_bills:
        bill_amt = float(bill.get("amount", 0) or bill.get("bill_amount", 0))
        bill_cur = bill.get("currency", "INR") or bill.get("bill_currency", "INR")
        bill_date_str = bill.get("date", "") or bill.get("bill_date", "")
        bill_vendor = bill.get("vendor_name", "")

        try:
            bill_date = _dt.strptime(bill_date_str, "%Y-%m-%d")
        except Exception:
            bill_date = None

        candidates = []
        for cc in cc_only_list:
            cc_inr = float(cc.get("amount", 0))
            cc_date_str = cc.get("date", "")
            cc_desc = cc.get("description", "")
            cc_forex = cc.get("forex_amount")
            cc_forex_cur = cc.get("forex_currency")

            # --- Amount scoring ---
            if bill_cur != "INR" and cc_forex and cc_forex_cur == bill_cur:
                diff_pct = abs(float(cc_forex) - bill_amt) / max(bill_amt, 0.01) * 100
            elif bill_cur == "INR":
                diff_pct = abs(cc_inr - bill_amt) / max(bill_amt, 0.01) * 100
            elif bill_cur == "USD" and not cc_forex:
                mid_rate = 87.0
                if forex_rates and bill_date_str in forex_rates:
                    mid_rate = forex_rates[bill_date_str].get("USD_INR", 87.0)
                estimated_inr = bill_amt * mid_rate
                diff_pct = abs(cc_inr - estimated_inr) / max(estimated_inr, 0.01) * 100
            else:
                continue

            if diff_pct > 5:
                continue
            elif diff_pct <= 0.01:
                amount_score = 100
            elif diff_pct <= 1:
                amount_score = 80
            else:
                amount_score = 50

            # --- Date scoring ---
            try:
                cc_date = _dt.strptime(cc_date_str, "%Y-%m-%d")
            except Exception:
                continue
            if not bill_date:
                continue
            days_apart = abs((bill_date - cc_date).days)
            if days_apart > 60:
                continue
            elif days_apart <= 2:
                date_score = 100
            elif days_apart <= 5:
                date_score = 80
            elif days_apart <= 10:
                date_score = 60
            elif days_apart <= 30:
                date_score = 30
            else:
                date_score = 10

            # --- Vendor signal scoring ---
            vendor_score = 0
            if bill_vendor and cc_desc:
                bv_lower = bill_vendor.lower()
                cd_lower = cc_desc.lower()
                bv_words = [w for w in bv_lower.split() if len(w) >= 4]
                for word in bv_words:
                    if word in cd_lower:
                        vendor_score = 80
                        break
                if vendor_score == 0:
                    bv_first = bv_lower.split()[0] if bv_lower.split() else ""
                    cd_first = cd_lower.split()[0] if cd_lower.split() else ""
                    if bv_first and cd_first and (bv_first == cd_first or bv_first in cd_first or cd_first in bv_first):
                        vendor_score = 50

            candidates.append({
                "cc_transaction_id": cc.get("transaction_id", ""),
                "cc_description": cc_desc,
                "cc_inr_amount": cc_inr,
                "cc_date": cc_date_str,
                "cc_card": cc.get("card_name", ""),
                "cc_forex_amount": cc_forex,
                "cc_forex_currency": cc_forex_cur,
                "breakdown": {
                    "amount": amount_score,
                    "date": date_score,
                    "vendor": vendor_score,
                    "uniqueness": 0,
                },
            })

        # --- Uniqueness scoring ---
        count = len(candidates)
        for cand in candidates:
            if count == 1:
                cand["breakdown"]["uniqueness"] = 15
            elif count <= 3:
                cand["breakdown"]["uniqueness"] = 0
            else:
                cand["breakdown"]["uniqueness"] = -10

        # --- Overall score ---
        for cand in candidates:
            b = cand["breakdown"]
            cand["candidate_score"] = int(
                b["amount"] * 0.4 + b["date"] * 0.2 + b["vendor"] * 0.3 + b["uniqueness"] * 0.1
            )

        candidates.sort(key=lambda c: c["candidate_score"], reverse=True)
        candidates = candidates[:5]

        entry = {
            "bill_id": bill.get("bill_id", ""),
            "vendor_id": bill.get("vendor_id", ""),
            "vendor_name": bill_vendor,
            "bill_amount": bill_amt,
            "bill_currency": bill_cur,
            "bill_date": bill_date_str,
            "bill_number": bill.get("file", "") or bill.get("bill_number", ""),
            "status": "unmatched",
            "candidates": candidates,
        }
        results.append(entry)

    return results
```

**Step 2: Run tests to verify they pass**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -k "candidate" -v`
Expected: All 9 candidate tests PASS

**Step 3: Run full test suite**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: All existing + new tests PASS

**Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add _find_candidates_for_unmatched scoring engine"
```

---

### Task 3: Wire candidate engine into API response

**Files:**
- Modify: `app.py:1413-1414` (in `api_payments_preview`, after diagnostic loop, before bill_matched_flags)

**Step 1: Insert candidate engine call**

After line 1413 (end of `cc_item["unmatched_reason"] = best or ...`) and before line 1415 (`bill_matched_flags`), insert:

```python
        # --- Candidate matching for unmatched bills ---
        unmatched_bill_objs = [m for m in matches if m["status"] == "unmatched"]
        if unmatched_bill_objs and unmatched_cc:
            candidate_results = _find_candidates_for_unmatched(
                unmatched_bill_objs, unmatched_cc, forex_rates=forex_cache
            )
            # Replace unmatched entries with candidate-enriched versions
            candidate_by_bill = {r["bill_id"]: r for r in candidate_results}
            for i, m in enumerate(matches):
                if m["status"] == "unmatched" and m["bill_id"] in candidate_by_bill:
                    matches[i] = candidate_by_bill[m["bill_id"]]
```

**Step 2: Test manually**

Run the app and load Record Payments. In browser DevTools Network tab, check the `/api/payments/preview` response — unmatched entries should now have `candidates` arrays.

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: wire candidate engine into payments preview API"
```

---

### Task 4: Frontend — display top candidate inline in CC columns

**Files:**
- Modify: `app.py:7455-7493` (JavaScript row rendering)

**Step 1: Update unmatched confidence cell (lines 7455-7456)**

Replace:
```javascript
    } else if (m.status === 'unmatched') {
      confCell = '<span style="color:var(--yellow);font-size:10px">No CC</span>';
    }
```

With:
```javascript
    } else if (m.status === 'unmatched') {
      var topCand = (m.candidates && m.candidates.length > 0) ? m.candidates[0] : null;
      if (topCand) {
        var cs = topCand.candidate_score;
        var csColor = cs >= 80 ? 'var(--green)' : cs >= 60 ? 'var(--yellow)' : 'var(--text-dim)';
        confCell = '<div style="text-align:center;line-height:1.3;cursor:pointer" onclick="toggleCandidateDetail(\'' + m.bill_id + '\')">'
          + '<div style="font-size:13px;font-weight:700;color:' + csColor + '">' + cs + '%</div>'
          + '<div style="font-size:9px;color:var(--text-dim)">Candidate</div>'
          + '<div style="font-size:8px;color:var(--text-dim)">Amt:' + _confDot(topCand.breakdown.amount) + ' Date:' + _confDot(topCand.breakdown.date) + ' Vnd:' + _confDot(topCand.breakdown.vendor) + '</div>'
          + '</div>';
      } else {
        confCell = '<span style="color:var(--yellow);font-size:10px">No CC</span>';
      }
    }
```

**Step 2: Update CC column rendering (lines 7461-7486)**

Replace lines 7461-7486 with:
```javascript
    // CC columns (left side) — empty for unmatched/already_paid, candidate for unmatched+candidates
    var hasCc = m.status === 'matched' || m.status === 'cc_only';
    var hasCandidate = m.status === 'unmatched' && m.candidates && m.candidates.length > 0;
    var topCandRow = hasCandidate ? m.candidates[0] : null;
    var showCc = hasCc || hasCandidate;

    var ccDesc, ccDescFull, forexNote;
    if (hasCc) {
      ccDesc = m.cc_description || '-';
      ccDescFull = ccDesc;
      forexNote = m.cc_forex_amount ? ' (' + m.cc_forex_currency + ' ' + fmt(m.cc_forex_amount) + ')' : '';
    } else if (hasCandidate) {
      ccDesc = topCandRow.cc_description || '-';
      ccDescFull = ccDesc;
      forexNote = topCandRow.cc_forex_amount ? ' (' + topCandRow.cc_forex_currency + ' ' + fmt(topCandRow.cc_forex_amount) + ')' : '';
    } else {
      ccDesc = ''; ccDescFull = ''; forexNote = '';
    }
    if (ccDesc.length > 40) ccDesc = ccDesc.substring(0, 40) + '\u2026';
    var dimStyle = 'color:var(--text-dim);';
    var candidateStyle = hasCandidate ? 'font-style:italic;opacity:0.7;' : '';

    // Bill columns (right side) — empty for cc_only
    var hasBill = m.status !== 'cc_only';

    // Checkbox cell — for matched rows AND unmatched rows with candidates
    var cbCell = '';
    if (m.status === 'matched' || hasCandidate) {
      cbCell = '<td style="text-align:center;padding:5px 4px"><input type="checkbox" class="pay-cb" data-billid="' + m.bill_id + '" data-is-candidate="' + (hasCandidate ? '1' : '0') + '" onchange="togglePayCheckbox(this)"></td>';
    } else {
      cbCell = '<td style="padding:5px 4px"></td>';
    }

    tr.innerHTML = cbCell
      // --- CC LEFT ---
      + '<td style="text-align:left;padding:5px 8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' + (showCc ? candidateStyle : dimStyle) + '" title="' + ccDescFull.replace(/"/g,'&quot;') + '">' + (showCc ? ccDesc + forexNote : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="text-align:right;padding:5px 8px;font-family:monospace;' + (showCc ? candidateStyle : dimStyle) + '">' + (showCc ? fmt(hasCandidate ? topCandRow.cc_inr_amount : m.cc_inr_amount) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px;' + (showCc ? candidateStyle : dimStyle) + '">' + (showCc ? fmtDate(hasCandidate ? topCandRow.cc_date : m.cc_date) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px;font-size:10px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' + candidateStyle + '">' + (showCc ? (hasCandidate ? (topCandRow.cc_card||'-') : (m.cc_card||'-')) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      // --- BILL RIGHT ---
      + '<td style="text-align:left;padding:5px 8px;border-left:2px solid var(--border);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (m.vendor_name||'').replace(/"/g,'&quot;') + '">' + (hasBill ? (m.vendor_name||'-') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="text-align:right;padding:5px 8px;font-family:monospace">' + (hasBill ? fmt(m.bill_amount) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 4px;text-align:center">' + (hasBill ? (m.bill_currency||'INR') : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px">' + (hasBill ? fmtDate(m.bill_date) : '<span style="'+dimStyle+'">-</span>') + '</td>'
      + '<td style="padding:5px 8px">' + confCell + '</td>'
      + '<td style="padding:5px 8px">' + actionBtn + '</td>';
```

**Step 3: Test visually**

Run app → Record Payments → verify:
- "No CC" rows with candidates show italicized CC data
- Rows without candidates still show dashes
- Checkboxes on candidate rows

**Step 4: Commit**

```bash
git add app.py
git commit -m "feat: display top candidate inline in unmatched bill CC columns"
```

---

### Task 5: Frontend — expandable candidate detail row

**Files:**
- Modify: `app.py` (add JavaScript functions after existing payment functions, near line 7864)

**Step 1: Add `toggleCandidateDetail()` function**

Insert after the `recordSelectedPayments` function (after line 7863):

```javascript
// --- Candidate detail expansion ---

function toggleCandidateDetail(billId) {
  var detailRow = document.getElementById('cand-detail-' + billId);
  if (detailRow) {
    detailRow.style.display = detailRow.style.display === 'none' ? '' : 'none';
    return;
  }
  var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
  if (!m || !m.candidates) return;
  var parentRow = document.getElementById('pay-row-' + billId);
  if (!parentRow) return;

  var tr = document.createElement('tr');
  tr.id = 'cand-detail-' + billId;
  tr.style.background = 'rgba(255,200,50,0.03)';

  var td = document.createElement('td');
  td.colSpan = 11;
  td.style.padding = '10px 16px';

  // Header
  var header = document.createElement('div');
  header.style.cssText = 'font-size:11px;font-weight:700;color:var(--yellow);margin-bottom:8px';
  header.textContent = 'Suggested matches for: ' + (m.vendor_name||'') + ' ' + (m.bill_currency||'INR') + ' ' + fmt(m.bill_amount) + ' (' + fmtDate(m.bill_date) + ')';
  td.appendChild(header);

  // Candidate list
  m.candidates.forEach(function(c, ci) {
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:12px;padding:4px 0;border-bottom:1px solid var(--border)';
    var sc = c.candidate_score;
    var scColor = sc >= 80 ? 'var(--green)' : sc >= 60 ? 'var(--yellow)' : 'var(--text-dim)';

    var scoreSpan = document.createElement('span');
    scoreSpan.style.cssText = 'font-size:11px;min-width:30px;font-weight:700;color:' + scColor;
    scoreSpan.textContent = sc + '%';
    row.appendChild(scoreSpan);

    var descSpan = document.createElement('span');
    descSpan.style.cssText = 'font-size:11px;flex:1';
    descSpan.textContent = c.cc_description;
    row.appendChild(descSpan);

    var amtSpan = document.createElement('span');
    amtSpan.style.cssText = 'font-size:11px;font-family:monospace';
    amtSpan.textContent = fmt(c.cc_inr_amount);
    row.appendChild(amtSpan);

    if (c.cc_forex_amount) {
      var fxSpan = document.createElement('span');
      fxSpan.style.cssText = 'font-size:10px;color:var(--text-dim)';
      fxSpan.textContent = '(' + c.cc_forex_currency + ' ' + fmt(c.cc_forex_amount) + ')';
      row.appendChild(fxSpan);
    }

    var dateSpan = document.createElement('span');
    dateSpan.style.cssText = 'font-size:11px';
    dateSpan.textContent = fmtDate(c.cc_date);
    row.appendChild(dateSpan);

    var cardSpan = document.createElement('span');
    cardSpan.style.cssText = 'font-size:10px;color:var(--text-dim)';
    cardSpan.textContent = c.cc_card;
    row.appendChild(cardSpan);

    var brkSpan = document.createElement('span');
    brkSpan.style.cssText = 'font-size:8px;color:var(--text-dim)';
    brkSpan.textContent = 'Amt:' + c.breakdown.amount + ' Date:' + c.breakdown.date + ' Vnd:' + c.breakdown.vendor;
    row.appendChild(brkSpan);

    var confirmBtn = document.createElement('button');
    confirmBtn.className = 'bill-create-btn';
    confirmBtn.style.cssText = 'font-size:10px;padding:2px 8px';
    confirmBtn.textContent = 'Confirm';
    confirmBtn.setAttribute('data-billid', billId);
    confirmBtn.setAttribute('data-cidx', ci);
    confirmBtn.onclick = function() { confirmCandidateMatch(billId, ci); };
    row.appendChild(confirmBtn);

    td.appendChild(row);
  });

  // Search box
  var searchRow = document.createElement('div');
  searchRow.style.cssText = 'margin-top:10px;display:flex;gap:8px;align-items:center';
  var searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.id = 'cand-search-' + billId;
  searchInput.placeholder = 'Search CC descriptions...';
  searchInput.style.cssText = 'flex:1;padding:4px 8px;font-size:11px;background:var(--bg-secondary);color:var(--text);border:1px solid var(--border);border-radius:4px';
  searchInput.onkeyup = function() { searchCandidates(billId); };
  searchRow.appendChild(searchInput);
  var hintSpan = document.createElement('span');
  hintSpan.style.cssText = 'font-size:10px;color:var(--text-dim)';
  hintSpan.textContent = 'Amt \u00b15%  Date \u00b130d';
  searchRow.appendChild(hintSpan);
  td.appendChild(searchRow);

  var searchResults = document.createElement('div');
  searchResults.id = 'cand-search-results-' + billId;
  searchResults.style.marginTop = '6px';
  td.appendChild(searchResults);

  // Not CC Paid link
  var dismissDiv = document.createElement('div');
  dismissDiv.style.cssText = 'margin-top:8px;text-align:right';
  var dismissLink = document.createElement('a');
  dismissLink.href = '#';
  dismissLink.style.cssText = 'font-size:10px;color:var(--text-dim)';
  dismissLink.textContent = 'Not CC Paid';
  dismissLink.onclick = function(e) { e.preventDefault(); dismissUnmatchedBill(billId); };
  dismissDiv.appendChild(dismissLink);
  td.appendChild(dismissDiv);

  tr.appendChild(td);
  parentRow.parentNode.insertBefore(tr, parentRow.nextSibling);
}
```

**Step 2: Add `searchCandidates()` function**

```javascript
function searchCandidates(billId) {
  var input = document.getElementById('cand-search-' + billId);
  var resultsDiv = document.getElementById('cand-search-results-' + billId);
  if (!input || !resultsDiv) return;
  var query = input.value.trim().toLowerCase();
  if (query.length < 2) { resultsDiv.textContent = ''; return; }

  var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
  if (!m) return;
  var billAmt = m.bill_amount;

  var allCc = (_paymentPreviewData.unmatched_cc || []);
  var hits = allCc.filter(function(cc) {
    if (!cc.description || cc.description.toLowerCase().indexOf(query) < 0) return false;
    return true;
  }).slice(0, 10);

  resultsDiv.textContent = '';
  if (hits.length === 0) {
    var noResult = document.createElement('div');
    noResult.style.cssText = 'font-size:10px;color:var(--text-dim);padding:4px 0';
    noResult.textContent = 'No CC transactions matching "' + query + '"';
    resultsDiv.appendChild(noResult);
    return;
  }

  hits.forEach(function(cc) {
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:12px;padding:3px 0;font-size:11px';

    var descSpan = document.createElement('span');
    descSpan.style.flex = '1';
    descSpan.textContent = cc.description;
    row.appendChild(descSpan);

    var amtSpan = document.createElement('span');
    amtSpan.style.fontFamily = 'monospace';
    amtSpan.textContent = fmt(cc.amount);
    row.appendChild(amtSpan);

    if (cc.forex_amount) {
      var fxSpan = document.createElement('span');
      fxSpan.style.cssText = 'font-size:10px;color:var(--text-dim)';
      fxSpan.textContent = '(' + cc.forex_currency + ' ' + fmt(cc.forex_amount) + ')';
      row.appendChild(fxSpan);
    }

    var dateSpan = document.createElement('span');
    dateSpan.textContent = fmtDate(cc.date);
    row.appendChild(dateSpan);

    var cardSpan = document.createElement('span');
    cardSpan.style.cssText = 'font-size:10px;color:var(--text-dim)';
    cardSpan.textContent = cc.card_name || '';
    row.appendChild(cardSpan);

    var btn = document.createElement('button');
    btn.className = 'bill-create-btn';
    btn.style.cssText = 'font-size:10px;padding:2px 8px';
    btn.textContent = 'Confirm';
    btn.onclick = function() { confirmSearchMatch(billId, cc.transaction_id); };
    row.appendChild(btn);

    resultsDiv.appendChild(row);
  });
}
```

**Step 3: Add confirm and dismiss functions**

```javascript
function confirmCandidateMatch(billId, candidateIdx) {
  var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
  if (!m || !m.candidates || !m.candidates[candidateIdx]) return;
  var cand = m.candidates[candidateIdx];

  showModal('Confirm Candidate Match?',
    'Match bill ' + (m.vendor_name||'') + ' (' + (m.bill_currency||'INR') + ' ' + fmt(m.bill_amount) + ') with CC: ' + cand.cc_description + ' (' + fmt(cand.cc_inr_amount) + ')?',
    function() {
      fetch('/api/payments/record-one', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          bill_id: billId,
          cc_transaction_id: cand.cc_transaction_id,
          cc_inr_amount: cand.cc_inr_amount,
          cc_date: cand.cc_date,
          cc_card: cand.cc_card,
          cc_description: cand.cc_description,
          cc_forex_amount: cand.cc_forex_amount,
          cc_forex_currency: cand.cc_forex_currency,
        }),
      })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        var row = document.getElementById('pay-row-' + billId);
        if (data.status === 'paid') {
          if (row) row.style.background = 'rgba(80,200,120,0.15)';
          addLogLine('[Payment] Candidate confirmed: ' + (m.vendor_name||'') + ' -> ' + cand.cc_description);
          var detail = document.getElementById('cand-detail-' + billId);
          if (detail) detail.style.display = 'none';
        } else {
          addLogLine('[Payment] Error: ' + (data.message || data.error || data.status));
        }
      });
    }, true, 'Confirm Match');
}

function confirmSearchMatch(billId, ccTxnId) {
  var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
  if (!m) return;
  var allCc = (_paymentPreviewData.unmatched_cc || []);
  var cc = allCc.find(function(c) { return c.transaction_id === ccTxnId; });
  if (!cc) return;

  showModal('Confirm Search Match?',
    'Match bill ' + (m.vendor_name||'') + ' (' + (m.bill_currency||'INR') + ' ' + fmt(m.bill_amount) + ') with CC: ' + cc.description + ' (' + fmt(cc.amount) + ')?',
    function() {
      fetch('/api/payments/record-one', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          bill_id: billId,
          cc_transaction_id: cc.transaction_id,
          cc_inr_amount: cc.amount,
          cc_date: cc.date,
          cc_card: cc.card_name,
          cc_description: cc.description,
          cc_forex_amount: cc.forex_amount,
          cc_forex_currency: cc.forex_currency,
        }),
      })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        var row = document.getElementById('pay-row-' + billId);
        if (data.status === 'paid') {
          if (row) row.style.background = 'rgba(80,200,120,0.15)';
          addLogLine('[Payment] Search match confirmed: ' + (m.vendor_name||'') + ' -> ' + cc.description);
          var detail = document.getElementById('cand-detail-' + billId);
          if (detail) detail.style.display = 'none';
        } else {
          addLogLine('[Payment] Error: ' + (data.message || data.error || data.status));
        }
      });
    }, true, 'Confirm Match');
}

function dismissUnmatchedBill(billId) {
  var row = document.getElementById('pay-row-' + billId);
  var detail = document.getElementById('cand-detail-' + billId);
  if (row) row.style.display = 'none';
  if (detail) detail.style.display = 'none';
}
```

**Step 4: Test visually**

- Click candidate row → detail expands with all candidates + search box
- Search "WINDSURF" → shows matching CC transactions
- Confirm a candidate → payment recorded, row turns green
- "Not CC Paid" → hides row

**Step 5: Commit**

```bash
git add app.py
git commit -m "feat: expandable candidate detail with search and confirm actions"
```

---

### Task 6: Bulk approval with score threshold

**Files:**
- Modify: `app.py` (JavaScript — section header and bulk logic)

**Step 1: Update "No CC Match" section header (lines 7402-7405)**

Replace:
```javascript
      } else if (section === 'unmatched') {
        var umCount = matches.filter(function(x){return x.status==='unmatched'}).length;
        sepLabel = '\u26A0 No CC Match \u2014 Bills Only (' + umCount + ')';
        sepColor = 'var(--yellow)'; sepBg = 'rgba(255,200,50,0.06)';
      }
```

With (note: the section header uses innerHTML in the existing codebase for all sections):
```javascript
      } else if (section === 'unmatched') {
        var umCount = matches.filter(function(x){return x.status==='unmatched'}).length;
        var withCand = matches.filter(function(x){return x.status==='unmatched' && x.candidates && x.candidates.length > 0}).length;
        sepLabel = '\u26A0 No CC Match \u2014 Bills Only (' + umCount + ')';
        if (withCand > 0) {
          sepLabel += ' <span style="font-weight:400;font-size:10px;margin-left:12px">'
            + 'Score \u2265 <select id="candScoreThreshold" onchange="filterCandidatesByScore()" style="background:var(--bg-secondary);color:var(--text);border:1px solid var(--border);font-size:10px;padding:1px 4px;border-radius:3px">'
            + '<option value="0">All</option><option value="50">50</option><option value="60">60</option><option value="70" selected>70</option><option value="80">80</option><option value="90">90</option>'
            + '</select>'
            + ' <button onclick="selectAllCandidates()" style="font-size:10px;padding:2px 8px;margin-left:8px;background:var(--bg-secondary);color:var(--text);border:1px solid var(--border);border-radius:3px;cursor:pointer">Select Visible</button>'
            + ' <button id="confirmCandidatesBtn" onclick="confirmSelectedCandidates()" style="font-size:10px;padding:2px 8px;margin-left:4px;background:var(--green);color:#000;border:none;border-radius:3px;cursor:pointer;display:none">Confirm Selected (0)</button>'
            + '</span>';
        }
        sepColor = 'var(--yellow)'; sepBg = 'rgba(255,200,50,0.06)';
      }
```

**Step 2: Add bulk candidate functions**

Add after the `dismissUnmatchedBill` function:

```javascript
// --- Bulk candidate approval ---

function filterCandidatesByScore() {
  var threshold = parseInt(document.getElementById('candScoreThreshold').value) || 0;
  var matches = _paymentPreviewData.matches || [];
  matches.forEach(function(m) {
    if (m.status !== 'unmatched') return;
    var row = document.getElementById('pay-row-' + m.bill_id);
    if (!row) return;
    var topScore = (m.candidates && m.candidates.length > 0) ? m.candidates[0].candidate_score : 0;
    if (threshold > 0 && topScore < threshold) {
      row.style.display = 'none';
      var cb = row.querySelector('.pay-cb');
      if (cb && cb.checked) { cb.checked = false; togglePayCheckbox(cb); }
    } else {
      row.style.display = '';
    }
  });
}

function selectAllCandidates() {
  var threshold = parseInt(document.getElementById('candScoreThreshold').value) || 0;
  var matches = _paymentPreviewData.matches || [];
  matches.forEach(function(m) {
    if (m.status !== 'unmatched' || !m.candidates || m.candidates.length === 0) return;
    var topScore = m.candidates[0].candidate_score;
    if (threshold > 0 && topScore < threshold) return;
    var row = document.getElementById('pay-row-' + m.bill_id);
    if (!row || row.style.display === 'none') return;
    var cb = row.querySelector('.pay-cb');
    if (cb && !cb.checked) { cb.checked = true; togglePayCheckbox(cb); }
  });
  _updateCandidateSelectedBtn();
}

function _updateCandidateSelectedBtn() {
  var btn = document.getElementById('confirmCandidatesBtn');
  if (!btn) return;
  var count = 0;
  _paySelectedBills.forEach(function(billId) {
    var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId && x.status === 'unmatched'; });
    if (m && m.candidates && m.candidates.length > 0) count++;
  });
  if (count > 0) {
    btn.style.display = 'inline-block';
    btn.textContent = 'Confirm Selected (' + count + ')';
  } else {
    btn.style.display = 'none';
  }
}

function confirmSelectedCandidates() {
  var items = [];
  var score90 = 0, score70 = 0, scoreLow = 0;
  var newMappings = new Set();
  _paySelectedBills.forEach(function(billId) {
    var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId && x.status === 'unmatched'; });
    if (!m || !m.candidates || m.candidates.length === 0) return;
    var cand = m.candidates[0];
    items.push({
      bill_id: billId,
      cc_transaction_id: cand.cc_transaction_id,
      cc_inr_amount: cand.cc_inr_amount,
      cc_date: cand.cc_date,
      cc_card: cand.cc_card,
      cc_description: cand.cc_description,
      cc_forex_amount: cand.cc_forex_amount,
      cc_forex_currency: cand.cc_forex_currency,
    });
    if (cand.candidate_score >= 90) score90++;
    else if (cand.candidate_score >= 70) score70++;
    else scoreLow++;
    newMappings.add(cand.cc_description);
  });
  if (!items.length) return;

  var summary = 'Score 90+: ' + score90 + ' bills\nScore 70-89: ' + score70 + ' bills';
  if (scoreLow > 0) summary += '\nScore <70: ' + scoreLow + ' bills';
  summary += '\n\nNew vendor mappings to learn: ' + newMappings.size;

  showModal('Confirm ' + items.length + ' Candidate Matches?', summary, function() {
    var btn = document.getElementById('confirmCandidatesBtn');
    btn.disabled = true;
    btn.textContent = 'Recording ' + items.length + '...';

    fetch('/api/payments/record-selected', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: items}),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var results = data.results || [];
      var paidCount = 0;
      results.forEach(function(r) {
        var row = document.getElementById('pay-row-' + r.bill_id);
        if (r.status === 'paid') {
          if (row) row.style.background = 'rgba(80,200,120,0.15)';
          var cb = row ? row.querySelector('.pay-cb') : null;
          if (cb) { cb.checked = false; cb.disabled = true; }
          _paySelectedBills.delete(r.bill_id);
          paidCount++;
          var detail = document.getElementById('cand-detail-' + r.bill_id);
          if (detail) detail.style.display = 'none';
        }
      });
      btn.textContent = paidCount + '/' + items.length + ' Confirmed';
      _updatePaySelectedBtn();
      _updateCandidateSelectedBtn();
      addLogLine('[Payment] Bulk candidate confirm: ' + paidCount + '/' + items.length + ' paid');
    })
    .catch(function(err) {
      btn.textContent = 'Error';
      btn.disabled = false;
      addLogLine('[Payment] Bulk candidate error: ' + err);
    });
  }, true, 'Confirm All');
}
```

**Step 3: Update `togglePayCheckbox` to also update candidate button**

In the existing `togglePayCheckbox` function (line 7769-7774), add after `_updatePaySelectedBtn();`:

```javascript
  _updateCandidateSelectedBtn();
```

**Step 4: Test visually**

- Score threshold dropdown appears in "No CC Match" header
- Set to 80 → low-score rows hidden
- "Select Visible" → checkboxes checked
- "Confirm Selected (N)" → summary modal → confirm → payments recorded

**Step 5: Commit**

```bash
git add app.py
git commit -m "feat: bulk candidate approval with score threshold filter"
```

---

### Task 7: Integration test and final verification

**Files:**
- Modify: `tests/test_vendor_matching.py`

**Step 1: Add integration test**

```python
def test_candidate_integrates_with_vendor_gated():
    """Vendor-gated matching runs first; candidates only for leftovers."""
    bills = [
        _make_bill("Microsoft Corporation (India) Pvt Ltd", 12215.38, bill_id="B1"),
        _make_bill("Medium", 5.00, currency="USD", date="2025-02-02", bill_id="B2"),
    ]
    cc = [
        _make_cc("MICROSOFTBUS, MUMBAI", 12215.38),
        _make_cc("MEDIUM, SAN FRANCISCO", 435.00, date="2025-02-01",
                 forex_amount=5.00, forex_currency="USD"),
    ]
    cc[1]["transaction_id"] = "T2"
    vendor_map = {"microsoftbus": "Microsoft"}

    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    unmatched = [m for m in matches if m["status"] == "unmatched"]
    assert len(matched) == 1
    assert matched[0]["bill_id"] == "B1"
    assert len(unmatched) == 1
    assert unmatched[0]["bill_id"] == "B2"

    cc_only = [cc[1]]
    results = _find_candidates_for_unmatched(unmatched, cc_only)
    assert len(results) == 1
    assert results[0]["bill_id"] == "B2"
    assert len(results[0]["candidates"]) == 1
    assert results[0]["candidates"][0]["cc_description"] == "MEDIUM, SAN FRANCISCO"
    assert results[0]["candidates"][0]["candidate_score"] >= 70
```

**Step 2: Run full test suite**

Run: `cd /Users/daniel/products/cc && python -m pytest tests/test_vendor_matching.py -v`
Expected: ALL tests pass

**Step 3: Manual end-to-end test**

Run: `cd /Users/daniel/products/cc && python app.py`

Verify on Record Payments page:
1. Matched section: unchanged behavior
2. "No CC Match" section: inline candidates with italicized CC columns
3. Score threshold filter works
4. Click row → candidate detail expands
5. Search box finds CC transactions by description
6. Individual confirm → payment recorded, row turns green
7. Bulk select + confirm → summary modal → payments recorded
8. After confirm: vendor mapping saved to `learned_vendor_mappings.json`

**Step 4: Commit**

```bash
git add tests/test_vendor_matching.py
git commit -m "test: add integration test for candidate + vendor-gated flow"
```
