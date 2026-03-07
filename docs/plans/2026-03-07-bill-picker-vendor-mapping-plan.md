# Bill Picker Vendor Mapping & Layout Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the bill picker modal with full-width filters, checkbox dropdowns, a Zoho vendor mapping bar, a new Zoho Vendor column, and persistent vendor overrides that flow through to bill creation.

**Architecture:** Frontend-only overrides approach. A `vendor_overrides.json` file persists parsed-vendor-name-to-Zoho-vendor mappings. Three new Python endpoints (GET/POST vendor-overrides, GET zoho-vendors) handle persistence. The JS applies overrides in-memory before rendering. On bill creation, overrides are passed to step 3 as a priority-0 vendor resolution.

**Tech Stack:** Python/Flask (app.py), vanilla JS, CSS (all embedded in app.py ~6500 lines), Zoho Books API via scripts/utils.py.

**Design doc:** `docs/plans/2026-03-07-bill-picker-vendor-mapping-design.md`

**Note on innerHTML:** The existing codebase uses innerHTML throughout for rendering. All data originates from local JSON files (zoho_vendors_cache.json, bill_match_preview.json, vendor_overrides.json) — not untrusted external input. The plan follows existing patterns.

---

## Task 1: Backend — New Python Endpoints

**Files:**
- Modify: `app.py:2786` (insert new endpoints before the match-preview route)

**Step 1: Add the three new endpoints**

Insert before line 2786 (`@app.route("/api/bills/match-preview"...)`):

```python
@app.route("/api/zoho-vendors")
def api_zoho_vendors():
    """Return Zoho vendor list from cache for the UI dropdown."""
    cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")
    if not os.path.exists(cache_path):
        return jsonify([])
    with open(cache_path, "r", encoding="utf-8") as f:
        vendors = json.load(f)
    return jsonify([{"contact_id": v.get("contact_id", ""), "contact_name": v.get("contact_name", "")} for v in vendors])


@app.route("/api/vendor-overrides")
def api_vendor_overrides_get():
    """Return saved vendor overrides."""
    path = os.path.join(PROJECT_ROOT, "output", "vendor_overrides.json")
    if not os.path.exists(path):
        return jsonify({})
    with open(path, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/vendor-overrides", methods=["POST"])
def api_vendor_overrides_post():
    """Merge and save vendor overrides."""
    path = os.path.join(PROJECT_ROOT, "output", "vendor_overrides.json")
    existing = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    new_overrides = request.json.get("overrides", {})
    existing.update(new_overrides)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    return jsonify({"ok": True, "count": len(existing)})
```

**Step 2: Verify endpoints work**

Run the app and test with curl:
```bash
curl http://localhost:5000/api/zoho-vendors | python3 -m json.tool | head -20
curl http://localhost:5000/api/vendor-overrides
curl -X POST http://localhost:5000/api/vendor-overrides -H 'Content-Type: application/json' -d '{"overrides":{"TestVendor":{"contact_id":"123","contact_name":"Test"}}}'
curl http://localhost:5000/api/vendor-overrides
```

Expected: zoho-vendors returns list, overrides round-trips correctly.

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add /api/zoho-vendors and /api/vendor-overrides endpoints"
```

---

## Task 2: Backend — Step 3 Vendor Override Support

**Files:**
- Modify: `scripts/03_create_vendors_bills.py:331` (run function signature)
- Modify: `scripts/03_create_vendors_bills.py:497-612` (vendor resolution loop)

**Step 1: Update run() signature**

Change line 331 from:
```python
def run(selected_files=None):
```
to:
```python
def run(selected_files=None, vendor_overrides=None):
```

Add to docstring Args:
```
        vendor_overrides: Optional dict {filename: {"contact_id": str, "contact_name": str}}.
            If provided, these vendor assignments take priority over auto-resolution.
```

**Step 2: Add override check in the per-invoice loop**

After the `if fname in processed:` block (which continues at ~line 500), and before the GSTIN extraction block (around line 530), add priority-0 override. Wrap the existing vendor resolution code (lines ~530-618) inside the `else` clause:

```python
        # Priority 0: Explicit vendor override from UI
        if vendor_overrides and fname in vendor_overrides:
            vendor_id = vendor_overrides[fname]["contact_id"]
            vendor_name = vendor_overrides[fname]["contact_name"]
            log_action(f"  Using manual vendor override: {vendor_name} ({vendor_id})")
        else:
            # ... existing vendor resolution code (GSTIN -> name -> fuzzy -> create) ...
```

The indentation of the existing vendor resolution code increases by one level inside the `else`.

**Step 3: Commit**

```bash
git add scripts/03_create_vendors_bills.py
git commit -m "feat: step 3 accepts vendor_overrides for manual vendor mapping"
```

---

## Task 3: CSS — Layout Restructure & New Component Styles

**Files:**
- Modify: `app.py:3478-3546` (bill picker CSS section)

**Step 1: Replace the bill picker layout CSS**

Replace lines 3530-3546 (`.bill-picker-layout` through `.bill-summary-upload-section`) with new vertical layout and horizontal bottom bar styles. See design doc Section 1.

Key CSS classes to add/replace:
- `.bill-picker-layout` — change to `flex-direction: column`
- `.bill-picker-left` — remove `border-right`, remove `padding-right`
- `.bill-picker-right` — becomes `flex-direction: row`, `flex-wrap: wrap`, `border-top` instead of left border
- `.bill-summary-stat` — new horizontal stat items
- `.bill-summary-actions` — `margin-left: auto` to push buttons right

**Step 2: Add checkbox dropdown CSS**

After the bill-filter styles (around line 3495), add `.cb-dropdown`, `.cb-dropdown-btn`, `.cb-dropdown-panel`, `.cb-dropdown-actions`, `.cb-dropdown-list` classes.

**Step 3: Add mapping bar and searchable dropdown CSS**

Add `.bill-mapping-bar`, `.search-dropdown`, `.search-dropdown-list`, `.sd-item`, `.col-zoho-vendor` classes.

**Step 4: Commit**

```bash
git add app.py
git commit -m "feat: CSS for bill picker layout restructure, checkbox dropdown, mapping bar"
```

---

## Task 4: JS — Checkbox Dropdown Component

**Files:**
- Modify: `app.py` (insert after line 4918, after `_getMatchTypeKey`)

**Step 1: Add the reusable checkbox dropdown builder function**

Functions to add:
- `_buildCheckboxDropdown(id, label, options)` — returns HTML string for a dropdown button + panel with checkboxes
- `_toggleCbDropdown(id)` — toggles panel open/close, closes other open panels
- `_onCbChange(id)` — updates badge and calls `applyBillFilters()`
- `_cbSelectAll(id)` — checks all boxes
- `_cbClearAll(id)` — unchecks all boxes
- `_updateCbBadge(id)` — updates the count badge
- `_getCbValues(id)` — returns array of checked values
- Document click listener — closes all open dropdowns when clicking outside

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: reusable checkbox dropdown JS component"
```

---

## Task 5: JS — Searchable Dropdown Component & Zoho Vendor Loading

**Files:**
- Modify: `app.py` (insert after the checkbox dropdown code from Task 4)

**Step 1: Add Zoho vendor cache and searchable dropdown functions**

Variables:
- `_zohoVendors = []` — loaded from `/api/zoho-vendors`
- `_selectedZohoVendor = null` — currently selected Zoho vendor

Functions:
- `_loadZohoVendors()` — fetch + cache, returns Promise
- `_buildSearchDropdown()` — returns HTML for search input + dropdown list
- `_openZohoDropdown()` — shows filtered list on focus
- `_filterZohoDropdown()` — case-insensitive substring filter, limits to 50 results
- `_selectZohoVendor(el)` — sets `_selectedZohoVendor`, closes dropdown

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: searchable Zoho vendor dropdown component and data loader"
```

---

## Task 6: JS — Vendor Overrides Loading & Applying

**Files:**
- Modify: `app.py` (insert after searchable dropdown code)

**Step 1: Add override load, apply, and persist functions**

Variables:
- `_vendorOverrides = {}` — loaded from `/api/vendor-overrides`

Functions:
- `_loadVendorOverrides()` — fetch, returns Promise
- `_applyOverridesToPreview()` — for each preview row where vendor_name matches an override and action is `new_vendor`, change action to `new_bill`, set matched_vendor_id/name, set vendor_match_method to `"manual"`
- `applyZohoVendorMapping()` — validates selection, POSTs overrides, applies in-memory, re-renders

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: vendor override load, apply, and persist logic"
```

---

## Task 7: JS — Rewrite _buildFilterBar with Checkbox Dropdowns

**Files:**
- Modify: `app.py:4935-4968` (replace `_buildFilterBar` function)

**Step 1: Replace `_buildFilterBar`**

Change the Vendor filter from `<select id="bfVendor" multiple>` to `_buildCheckboxDropdown('vendor', 'Vendor', vendorOpts)`.

Change Status from `<select id="bfStatus" multiple>` to `_buildCheckboxDropdown('status', 'Status', statusOpts)`.

Change Match Type from `<select id="bfMatchType" multiple>` to `_buildCheckboxDropdown('matchtype', 'Match Type', matchOpts)`. Add `{value: 'manual', text: 'Manual'}` to the match options.

Keep From/To as `<select>` and Min/Max Amt as `<input type="number">`.

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: replace native select-multiple with checkbox dropdowns in filter bar"
```

---

## Task 8: JS — Rewrite applyBillFilters to Read Checkbox Dropdowns

**Files:**
- Modify: `app.py:5053-5101` (update `applyBillFilters`)
- Modify: `app.py:5138-5148` (update `clearBillFilters`)

**Step 1: Update `applyBillFilters`**

Replace the vendor/status/matchType reading lines that use `Array.from(el.selectedOptions)` with calls to `_getCbValues('vendor')`, `_getCbValues('status')`, `_getCbValues('matchtype')`.

**Step 2: Update `clearBillFilters`**

Replace the `['bfVendor','bfStatus','bfMatchType'].forEach` block with `['vendor','status','matchtype'].forEach(function(id){ _cbClearAll(id); })`.

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: applyBillFilters reads from checkbox dropdowns"
```

---

## Task 9: JS — Add Zoho Vendor Column to Table

**Files:**
- Modify: `app.py:4971-4994` (`_buildTable`)
- Modify: `app.py:4996-5051` (`_renderTableRows`)
- Modify: `app.py:5103-5117` (`_sortFilteredRows`)
- Modify: `app.py:5120-5135` (`sortBillTable`)

**Step 1: Add `{key:'zoho_vendor', label:'Zoho Vendor', sort:true, cls:'col-zoho-vendor'}` to the cols array in `_buildTable`, between `match` and `action`.**

**Step 2: In `_renderTableRows`, compute `zohoVendor` string per row:**
- For `skip` rows: extract from `matched_bill` or `matched_vendor_name`
- For `new_bill` rows: use `matched_vendor_name`
- For `new_vendor` rows: empty

Add `<td class="col-zoho-vendor">` between the match and action cells.

**Step 3: Add `zoho_vendor` sort comparator in `_sortFilteredRows` using `matched_vendor_name`.**

**Step 4: Update sort index map in `sortBillTable`: `{vendor:1,date:2,amount:3,status:4,match:5,zoho_vendor:6}`.**

**Step 5: Commit**

```bash
git add app.py
git commit -m "feat: Zoho Vendor column with sorting"
```

---

## Task 10: JS — Rewrite openBillPicker with New Layout Flow

**Files:**
- Modify: `app.py:5237-5283` (replace `openBillPicker`)

**Step 1: Replace `openBillPicker` to:**
1. Reset `_selectedZohoVendor = null`
2. Use `Promise.all` to load match-preview, vendor overrides, and zoho vendors in parallel
3. Call `_applyOverridesToPreview()` before rendering
4. Recount summary stats after overrides
5. Build HTML: `_buildFilterBar()` + mapping bar HTML + `_buildTable()`
6. Render summary via `_renderSummaryPanel()` in the bottom bar div
7. Attach filter listeners for `bfFrom`, `bfTo`, `bfMinAmt`, `bfMaxAmt` (the checkbox dropdowns fire events internally)

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: openBillPicker loads overrides + zoho vendors, renders mapping bar"
```

---

## Task 11: JS — Rewrite _renderSummaryPanel for Bottom Bar

**Files:**
- Modify: `app.py:4920-4933` (replace `_renderSummaryPanel`)

**Step 1: Change from vertical card layout to horizontal stat items.**

Use `.bill-summary-stat` divs (inline, with dots and counts) instead of `.bill-summary-card` blocks. Put Create Selected and Cancel buttons in `.bill-summary-actions` at the right end.

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: summary panel as horizontal bottom bar"
```

---

## Task 12: JS — Update createSelectedBills to Pass Vendor Overrides

**Files:**
- Modify: `app.py:5206-5221` (update `createSelectedBills`)

**Step 1: Before calling `runStepWithKwargs`, build `overridesDict`:**

Loop through `_matchPreviewData.preview`, for each file in `_billSelectedFiles` that has `matched_vendor_id`, add `{contact_id, contact_name}` to `overridesDict[inv.file]`.

Pass `{selected_files: files, vendor_overrides: overridesDict}` to `runStepWithKwargs`.

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: createSelectedBills passes vendor_overrides to step 3"
```

---

## Task 13: JS — Update Match Type Helpers for Manual

**Files:**
- Modify: `app.py:4912-4917`

**Step 1: Add `if (m === 'manual') return 'Manual';` to `_getMatchTypeLabel`.**

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: match type helpers support manual badge"
```

---

## Task 14: End-to-End Smoke Test

**Step 1: Start the app**
```bash
cd /Users/daniel/products/cc && python app.py
```

**Step 2: Manual test checklist**

1. Open bill picker — verify vertical layout (filters full width, mapping bar, table, bottom summary)
2. Click Vendor dropdown — checkbox list with Select All / Clear
3. Check vendors — badge updates, table filters
4. Click Status dropdown — checkboxes work
5. Select "New Bill + New Vendor" — only new vendor rows
6. Select rows, type in Zoho vendor search, select one, click Apply
7. Verify: Zoho Vendor column updates, status changes, match shows "Manual", summary counts change
8. Close and reopen — overrides persist
9. Click "Create Selected" — confirmation shows
10. Check `output/vendor_overrides.json` — mappings saved

**Step 3: Commit any fixes, then final commit**

```bash
git add -A
git commit -m "feat: bill picker vendor mapping & layout redesign complete"
```
