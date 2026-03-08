# Possible Duplicate Detection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a "Possible Duplicate" status to the bill match-preview when vendor + date + amount match a Zoho bill but the bill number doesn't.

**Architecture:** New action type `possible_duplicate` in `/api/bills/match-preview`. Backend builds a `(vendor_lower, date)` → list of amounts index, checks after vendor resolution. UI adds two columns (Invoice #, Zoho Bill #), a new orange badge, and updated summary/filters.

**Tech Stack:** Python/Flask backend, vanilla JS frontend, all in `app.py`.

**Note on innerHTML:** This codebase uses innerHTML throughout for rendering. All values displayed are from local JSON files (extracted invoices, Zoho cache), not user-supplied input. The existing pattern is safe in this context.

---

### Task 1: Backend — Build vendor+date+amount index

**Files:**
- Modify: `app.py:3454-3471` (index building loop)

**Step 1: Add the new index alongside existing ones**

After the existing `bills_vendor_date` index building (line 3471), add a new index that maps `(vendor_lower, date)` to a **list** of `(amount, bill_info)` tuples. Replace the existing loop at lines 3461-3471:

```python
    # (vendor_name_lower, date) -> list of (amount, bill_info) for duplicate detection
    bills_vendor_date_amounts = defaultdict(list)
    for b in zoho_bills:
        bn = b.get("bill_number", "")
        bills_exact[bn] = b
        norm = _normalize_bill_number(bn)
        if norm:
            bills_norm[norm] = b

        vn = (b.get("vendor_name") or "").strip().lower()
        bd = b.get("date", "")
        if vn and bd:
            bills_vendor_date[(vn, bd)] = b
            ba = round(float(b.get("total", 0)), 2)
            if ba:
                bills_vendor_date_amounts[(vn, bd)].append((ba, b))
```

Add `from collections import defaultdict` at the top of the function if not already imported.

**Step 2: Verify no syntax errors**

Run: `python3 -c "import ast; ast.parse(open('app.py').read())"`
Expected: No errors

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add vendor+date+amount index for duplicate detection"
```

---

### Task 2: Backend — Duplicate detection logic after vendor matching

**Files:**
- Modify: `app.py:3642-3654` (after vendor matching, before setting action)
- Modify: `app.py:3663-3678` (summary counts and return value)

**Step 1: Add duplicate check between vendor match and action assignment**

Replace the block at lines 3642-3654 with logic that checks for possible duplicates when a vendor is found:

```python
        if vendor_found:
            # --- Check for possible duplicate (vendor + date + ~amount) ---
            resolved_vn = (vendor_found.get("contact_name") or "").strip().lower()
            inv_amount = round(float(inv.get("amount") or 0), 2)
            dup_bill = None
            if resolved_vn and inv_date and inv_amount:
                candidates = bills_vendor_date_amounts.get((resolved_vn, inv_date), [])
                best_diff = float('inf')
                for (zoho_amt, zoho_bill) in candidates:
                    diff = abs(inv_amount - zoho_amt)
                    tolerance = max(1.0, zoho_amt * 0.01)
                    if diff <= tolerance and diff < best_diff:
                        best_diff = diff
                        dup_bill = zoho_bill

            if dup_bill:
                entry["action"] = "possible_duplicate"
                entry["matched_bill_number"] = dup_bill.get("bill_number", "")
                entry["matched_bill_id"] = dup_bill.get("bill_id", "")
                entry["matched_vendor_name"] = vendor_found.get("contact_name", "")
                entry["matched_vendor_id"] = vendor_found.get("contact_id", "")
                entry["vendor_match_method"] = vendor_match_method
                entry["match_type"] = "vendor_date_amount"
            else:
                entry["action"] = "new_bill"
                entry["matched_vendor_id"] = vendor_found.get("contact_id", "")
                entry["matched_vendor_name"] = vendor_found.get("contact_name", "")
                entry["vendor_match_method"] = vendor_match_method
                if vendor_match_method != "gstin" and inv_gstin:
                    zoho_gst = (vendor_found.get("gst_no") or "").strip()
                    if not zoho_gst:
                        entry["gstin_missing"] = True
        else:
            entry["action"] = "new_vendor_bill"
```

**Step 2: Update summary counts at lines 3663-3668**

```python
    skip_count = sum(1 for p in preview if p["action"] == "skip")
    dup_count = sum(1 for p in preview if p["action"] == "possible_duplicate")
    new_bill_count = sum(1 for p in preview if p["action"] == "new_bill")
    new_vendor_bill_count = sum(1 for p in preview if p["action"] == "new_vendor_bill")

    log_action(f"Match preview: {skip_count} skip, {dup_count} possible duplicates, {new_bill_count} new bills, {new_vendor_bill_count} new vendor+bill")
```

**Step 3: Update the return jsonify summary at lines 3670-3678**

```python
    return jsonify({
        "preview": preview,
        "summary": {
            "total": len(preview),
            "skip": skip_count,
            "possible_duplicate": dup_count,
            "new_bill": new_bill_count,
            "new_vendor_bill": new_vendor_bill_count,
        },
    })
```

**Step 4: Verify no syntax errors**

Run: `python3 -c "import ast; ast.parse(open('app.py').read())"`
Expected: No errors

**Step 5: Commit**

```bash
git add app.py
git commit -m "feat: detect possible duplicates by vendor+date+amount"
```

---

### Task 3: UI — Update status functions and summary counting

**Files:**
- Modify: `app.py:5631-5640` (`_getStatusLabel`, `_getStatusKey`)
- Modify: `app.py:5808-5813`, `app.py:5898-5903`, `app.py:6277-6282` (three summary counting blocks)

**Step 1: Update `_getStatusLabel` and `_getStatusKey` (lines 5631-5640)**

```javascript
function _getStatusLabel(inv) {
  if (inv.action === 'skip') return 'In Zoho';
  if (inv.action === 'possible_duplicate') return 'Possible Duplicate';
  if (inv.action === 'new_bill') return 'New Bill + Existing Vendor';
  return 'New Bill + New Vendor';
}
function _getStatusKey(inv) {
  if (inv.action === 'skip') return 'skip';
  if (inv.action === 'possible_duplicate') return 'possible_duplicate';
  if (inv.action === 'new_bill') return 'new_bill';
  return 'new_vendor';
}
```

**Step 2: Update all three JS summary counting blocks**

There are 3 identical blocks that compute `s = { total: 0, skip: 0, new_bill: 0, new_vendor_bill: 0 }`. Each must add `possible_duplicate: 0` and count it. The blocks are at approximately lines 5808, 5898, and 6277.

Each block becomes:

```javascript
    var s = { total: 0, skip: 0, possible_duplicate: 0, new_bill: 0, new_vendor_bill: 0 };
    _matchPreviewData.preview.forEach(function(inv) {
      s.total++;
      if (inv.action === 'skip') s.skip++;
      else if (inv.action === 'possible_duplicate') s.possible_duplicate++;
      else if (inv.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: update JS status functions and summary counting for possible_duplicate"
```

---

### Task 4: UI — Update summary panel rendering

**Files:**
- Modify: `app.py:5912-5924` (`_renderSummaryPanel` function)

**Step 1: Add "Possible Duplicate" counter to summary bar**

Insert the new counter between "In Zoho" and "Existing Vendor". The full function:

```javascript
function _renderSummaryPanel(summary, s, totalNew) {
  summary.innerHTML = ''
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--text-dim)"></span> Total <span class="count" id="bpTotal">' + s.total + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--green)"></span> In Zoho <span class="count" id="bpSkip">' + s.skip + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--orange,#f97316)"></span> Possible Duplicate <span class="count" id="bpDup">' + s.possible_duplicate + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--accent)"></span> Existing Vendor <span class="count" id="bpNewBill">' + s.new_bill + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--yellow)"></span> New Vendor <span class="count" id="bpNewVendor">' + s.new_vendor_bill + '</span></div>'
    + '<div class="bill-summary-stat"><span class="dot" style="background:var(--accent)"></span> Upload <span class="count" id="bpWillUpload">' + totalNew + '</span></div>'
    + '<div class="bill-summary-stat" id="bpSelectedCount" style="font-weight:600;color:var(--accent)">Selected: 0</div>'
    + '<div class="bill-summary-actions">'
    + '<button class="modal-btn modal-btn-confirm" id="createSelectedBillsBtn" onclick="createSelectedBills()" disabled>Create Selected (0)</button>'
    + '<button class="modal-btn modal-btn-cancel" onclick="closeBillPicker()">Cancel</button>'
    + '</div>';
}
```

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: add Possible Duplicate counter to summary panel"
```

---

### Task 5: UI — Add columns and update table rendering

**Files:**
- Modify: `app.py:5960-5969` (column definitions in `_buildTable`)
- Modify: `app.py:5986-6057` (`_renderTableRows`)

**Step 1: Add "Invoice #" and "Zoho Bill #" columns to `_buildTable` (lines 5960-5969)**

```javascript
function _buildTable() {
  var cols = [
    {key:'check', label:'<input ...>', sort:false, cls:'col-checkbox'},
    {key:'vendor', label:'Vendor', sort:true},
    {key:'invoice_num', label:'Invoice #', sort:true},
    {key:'date', label:'Date', sort:true},
    {key:'amount', label:'Amount', sort:true, cls:'col-amount'},
    {key:'status', label:'Status', sort:true},
    {key:'match', label:'Match', sort:true},
    {key:'zoho_vendor', label:'Zoho Vendor', sort:true, cls:'col-zoho-vendor'},
    {key:'zoho_bill', label:'Zoho Bill #', sort:true},
    {key:'action', label:'', sort:false, cls:'col-action'}
  ];
```

**Step 2: Update `_renderTableRows` to handle `possible_duplicate` and new columns**

Key changes:
- Replace `var isSkip = inv.action === 'skip'` with `var isBlocked = inv.action === 'skip' || inv.action === 'possible_duplicate'`
- Replace all `isSkip` references with `isBlocked`
- Add orange "Possible Duplicate" badge case
- Add Invoice # cell after Vendor
- Add Zoho Bill # cell after Zoho Vendor
- For Zoho Bill #: show `inv.matched_bill` for skip, `inv.matched_bill_number` for possible_duplicate

See the full replacement function in the design doc. The row HTML becomes:

```
<tr> checkbox | vendor | invoice# | date | amount | status | match | zoho_vendor | zoho_bill# | action </tr>
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add Invoice # and Zoho Bill # columns, possible_duplicate badge"
```

---

### Task 6: UI — Update filter dropdown and sort logic

**Files:**
- Modify: `app.py:5951` (status filter options)
- Modify: `app.py:6116-6122` (sort comparator in `sortBillTable`)

**Step 1: Add `possible_duplicate` to status filter options (line 5951)**

```javascript
  var statusOpts = [
    {value:'skip',text:'In Zoho'},
    {value:'possible_duplicate',text:'Possible Duplicate'},
    {value:'new_bill',text:'New Bill + Existing Vendor'},
    {value:'new_vendor',text:'New Bill + New Vendor'}
  ];
```

**Step 2: Add sort handling for new columns (around line 6116)**

Add cases for `invoice_num` and `zoho_bill` in the comparator:

```javascript
    else if (col === 'invoice_num') { va = (a.invoice_number||'').toLowerCase(); vb = (b.invoice_number||'').toLowerCase(); }
    // ... existing cases ...
    else if (col === 'zoho_bill') { va = (a.action==='skip' ? a.matched_bill : a.matched_bill_number) || ''; vb = (b.action==='skip' ? b.matched_bill : b.matched_bill_number) || ''; }
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add possible_duplicate to filter and sort for new columns"
```

---

### Task 7: Verify end-to-end

**Step 1: Parse check**

Run: `python3 -c "import ast; ast.parse(open('app.py').read())"`
Expected: No errors

**Step 2: Start the app and test manually**

Run: `python3 app.py`

1. Open the bill picker UI
2. Verify the AWS Sep 2025 invoice (amount 2,13,669.28) now shows as "Possible Duplicate" with Zoho bill number `2295334101`
3. Verify it has no checkbox and no Create button
4. Verify the summary bar shows the "Possible Duplicate" count
5. Verify the Invoice # column shows extracted invoice numbers
6. Verify the Zoho Bill # column shows bill numbers for skip and duplicate rows
7. Verify filters work with the new status
8. Verify sorting works on new columns

**Step 3: Final commit and push**

```bash
git add app.py
git commit -m "feat: possible duplicate detection — vendor+date+amount matching"
git push
```
