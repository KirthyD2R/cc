# Bill Picker Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the month-grouped accordion bill picker modal with a flat, filterable, sortable table with bulk selection and bulk create.

**Architecture:** Pure frontend change in the single-file Flask app `app.py`. Replace ~300 lines of CSS + JS (bill picker section) with new table-based UI. No backend changes, no new files. All filtering/sorting is client-side on the data already returned by `/api/bills/match-preview`.

**Tech Stack:** Vanilla JS, CSS (embedded in `app.py` via `render_template_string`), Flask backend (unchanged)

**Note on innerHTML:** This codebase is an internal dashboard tool. All data rendered via innerHTML comes from the app's own `/api/bills/match-preview` endpoint (not external user input). This matches the existing patterns throughout app.py.

---

### Task 1: Replace Bill Picker CSS

**Files:**
- Modify: `app.py:3198-3268` (the `/* Bill Picker */` CSS section)

**Step 1: Replace the bill picker CSS block**

Replace lines 3198-3268 (from `/* Bill Picker */` through `.bill-summary-upload-section`) with new CSS for: `.bill-filter-bar`, `.bill-filter-group`, `.bill-table-wrap`, `.bill-table` (with thead sticky, sortable headers, row-skip styling, col-checkbox/col-amount/col-action sizing), updated `.bill-picker-left` (flex column for filter+table), `.bill-picker-right` (flex:1 instead of flex:2), and `.bill-selected-count` for the selection counter. Keep `.bill-status-badge` and `.bill-create-btn` styles (updated). Keep all `.bill-summary-*` styles. See design doc for complete CSS.

Key new CSS classes:
- `.bill-filter-bar` — flex wrap container for filter controls
- `.bill-filter-group` — label + input/select pair
- `.bill-filter-clear` — reset button
- `.bill-table-wrap` — scrollable table container
- `.bill-table` — full-width table with sticky thead
- `.bill-table th.sorted .sort-arrow` — accent color for active sort
- `.bill-table tr.row-skip` — opacity 0.45 for "In Zoho" rows
- `.bill-selected-count` — shows selection count in summary panel

**Step 2: Verify no syntax errors**

Run: `cd /Users/daniel/products/cc && python -c "import app"`
Expected: No Python syntax errors.

**Step 3: Commit**

```bash
git add app.py
git commit -m "refactor: replace bill picker CSS with flat table styles"
```

---

### Task 2: Rewrite the Bill Picker JS — Core Table Rendering

**Files:**
- Modify: `app.py:4607-4709` (the `// --- Bill Picker with Match Preview ---` section through `openBillPicker()`)

**Step 1: Replace the bill picker JS from line 4607 through line 4709**

Replace the entire block from `// --- Bill Picker with Match Preview ---` (line 4607) through the closing of `openBillPicker()` (line 4709). This includes `_billPickerData`, `_matchPreviewData`, `_renderSummaryPanel()`, and `openBillPicker()`.

New code provides these functions/globals:

**State variables:**
- `_billPickerData`, `_matchPreviewData` — kept as before
- `_billFilteredRows` — current filtered subset
- `_billSortCol` / `_billSortAsc` — current sort state
- `_billSelectedFiles` — Set of selected filenames

**Helper functions:**
- `_getStatusLabel(inv)` — returns display label: "In Zoho", "New Bill + Existing Vendor", "New Bill + New Vendor"
- `_getStatusKey(inv)` — returns filter key: "skip", "new_bill", "new_vendor"
- `_getMatchTypeLabel(inv)` — returns match method label: "GSTIN", "Name", "Fuzzy" (only for new_bill rows)
- `_getMatchTypeKey(inv)` — returns match method key: "gstin", "name", "fuzzy"

**Rendering functions:**
- `_renderSummaryPanel(summary, s, totalNew)` — right panel with summary cards + "Selected: N" + "Create Selected (N)" button
- `_buildFilterBar(preview)` — generates HTML for filter bar with From/To month dropdowns, Vendor multi-select, Min/Max amount inputs, Status multi-select (3 options: In Zoho / New Bill + Existing Vendor / New Bill + New Vendor), Match Type multi-select (3 options: GSTIN / Name / Fuzzy), Clear button
- `_buildTable()` — generates table shell with sortable headers and empty tbody
- `_renderTableRows()` — fills tbody from `_billFilteredRows` with checkboxes, status badges, Create buttons

**Filter/Sort functions:**
- `applyBillFilters()` — reads all filter inputs (including Status and Match Type), filters `_matchPreviewData.preview`, stores in `_billFilteredRows`, sorts, renders. Match Type filter only applies to rows where action is "new_bill".
- `_sortFilteredRows()` — sorts `_billFilteredRows` by current sort column/direction
- `sortBillTable(col)` — click handler for column headers, toggles sort
- `clearBillFilters()` — resets all filter inputs, re-applies

**Selection functions:**
- `onBillCheckChange(cb)` — individual checkbox handler, updates `_billSelectedFiles`
- `toggleBillSelectAll(cb)` — header checkbox, selects/deselects all visible
- `_updateSelectAllCheckbox()` — syncs header checkbox state (checked/indeterminate)
- `_updateSelectionUI()` — updates count display and button text
- `_updateFilteredSummary()` — updates summary card counts for filtered view

**Action functions:**
- `createSelectedBills()` — bulk create with confirmation modal
- `createOneBillConfirm(filename, vendorName, amount)` — per-row create with confirmation modal

**Main entry:**
- `openBillPicker()` — opens modal, fetches `/api/bills/match-preview`, renders filter bar + table, attaches event listeners

**Step 2: Verify no syntax errors**

Run: `cd /Users/daniel/products/cc && python -c "import app"`
Expected: No Python syntax errors.

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: rewrite bill picker JS for flat table with filters and bulk selection"
```

---

### Task 3: Remove Old Month-Accordion Functions

**Files:**
- Modify: `app.py` (lines after the new `openBillPicker`, in the ~4774-4906 range)

**Step 1: Identify and delete obsolete functions**

Delete these functions entirely (replaced by Task 2 code):
- `createMonthBillsPreview(monthName)` — was for per-month bulk create in accordion view
- `toggleBillMonth(header)` — was for expanding/collapsing month accordions
- `createMonthBills(monthName)` — was for per-month bulk create in basic picker
- `createAllBills()` — was "Upload All New" button handler
- `createAllBillsDirect()` — was direct "Upload All" without opening picker
- `_createAllBillsBasic()` — was fallback for createAllBillsDirect

**Keep these functions unchanged:**
- `closeBillPicker()` — still needed
- `createOneBill(filename)` — still used by `_loadBasicBillPicker` fallback path
- `runStepWithKwargs(step, kwargs)` — still used by new code

**Step 2: Check for references to removed functions**

Search `app.py` for `createAllBillsDirect`, `createMonthBills`, `toggleBillMonth`, `createAllBills`. If any are called from the main dashboard HTML (outside the bill picker), update those callers to use `openBillPicker()` instead.

Known reference: search for any button onclick that calls these functions.

**Step 3: Verify no syntax errors**

Run: `cd /Users/daniel/products/cc && python -c "import app"`
Expected: No errors.

**Step 4: Commit**

```bash
git add app.py
git commit -m "refactor: remove obsolete month-accordion bill picker functions"
```

---

### Task 4: Manual Browser Verification

**Files:** None (verification only)

**Step 1: Start the app**

Run: `cd /Users/daniel/products/cc && python app.py --no-open`
Expected: Server starts on http://localhost:5000

**Step 2: Open browser and verify the bill picker**

Open http://localhost:5000 and click "Upload 1" to open bill picker. Verify:

1. Modal opens near-fullscreen with filter bar + flat table
2. Filter bar has: From dropdown, To dropdown, Vendor multi-select, Min/Max amount, Status multi-select, Clear
3. Table columns: checkbox, vendor, date, amount (right-aligned), status badge, match type badge (for new_bill rows), action button
4. "In Zoho" rows greyed out with no checkbox and no Create button
5. Selectable rows have working checkboxes and Create buttons
6. Column header click sorts (toggle asc/desc, arrow indicator)
7. Filter changes update table and summary counts instantly
8. "Select All" header checkbox works (only for visible selectable rows)
9. Checking rows updates "Selected: N" count and "Create Selected (N)" button
10. "Create Selected" shows confirmation modal with count
11. Per-row "Create" shows confirmation modal with vendor name and amount
12. "Clear" button resets all filters
13. Summary panel shows correct counts for filtered view

**Step 3: Fix any issues found, commit**

```bash
git add app.py
git commit -m "fix: bill picker UI polish after manual testing"
```

---

### Reference: Key Line Ranges in app.py (before changes)

| Section | Lines | Action |
|---------|-------|--------|
| Bill Picker CSS | 3198-3268 | Replace (Task 1) |
| Modal HTML shell | 3920-3930 | Keep as-is |
| `_renderSummaryPanel` + `openBillPicker` | 4607-4709 | Replace (Task 2) |
| `_loadBasicBillPicker` | 4711-4772 | Keep (fallback) |
| `createMonthBillsPreview` | 4774-4785 | Delete (Task 3) |
| `closeBillPicker` | 4787-4789 | Keep |
| `toggleBillMonth` | 4791-4801 | Delete (Task 3) |
| `createOneBill` | 4803-4807 | Keep |
| `createMonthBills` | 4809-4821 | Delete (Task 3) |
| `createAllBills` | 4823-4851 | Delete (Task 3) |
| `createAllBillsDirect` | 4853-4878 | Delete (Task 3) |
| `_createAllBillsBasic` | 4880-4906 | Delete (Task 3) |
| `runStepWithKwargs` | 4908-4922 | Keep |
