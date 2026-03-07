# Bill Picker Redesign: Flat Table with Filters

**Date:** 2026-03-07
**Scope:** Frontend-only change in `app.py` (embedded HTML/CSS/JS)

## Problem

The current bill picker modal uses month-grouped accordions that require expanding each month to see invoices. No filtering, no bulk selection, no date range support. Users must click "Create" per-invoice or "Upload (N)" per-month.

## Design Decisions

- **Approach:** Flat table with horizontal filter bar (over card view or grouped table)
- **Modal size:** Near-fullscreen (`95vw x 90vh`), keeps modal pattern (not a new page)
- **Default date range:** All time, user narrows down as needed
- **"In Zoho" invoices:** Shown but greyed out, non-selectable (disabled checkboxes)
- **Vendor filter:** Multi-select dropdown
- **Amount filter:** Min/Max number inputs
- **Actions:** Both per-row "Create" button AND bulk "Create Selected (N)" button
- **Confirmation:** Always show confirmation modal before creating bills

## Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Select Invoices to Create Bills                                    [X]  │
├─────────────────────────────────────────────────┬────────────────────────┤
│ Filter Bar                                      │ Summary Panel          │
│ [From ▼] [To ▼] [Vendor ▼] [Min][Max] [Status] │                        │
│ [Clear Filters]                                 │ Total Invoices    320  │
│                                                 │ ● In Zoho          93  │
│ ┌────┬────────────┬──────────┬───────┬────┬───┐ │ ● New Bill+Exist  150  │
│ │ ☐  │ Vendor     │ Date     │Amount │Stat│Act│ │ ● New Bill+New     77  │
│ ├────┼────────────┼──────────┼───────┼────┼───┤ │                        │
│ │    │ AWS        │2025-04-02│40,513 │Zoho│   │ │ Selected: 15           │
│ │ ☑  │ Google Play│2025-04-15│ 1,950 │NVnd│[C]│ │                        │
│ │ ☐  │ R K World  │2025-04-29│   331 │NGST│[C]│ │ [Create Selected (15)] │
│ │ ...│            │          │       │    │   │ │ [Cancel]               │
│ └────┴────────────┴──────────┴───────┴────┴───┘ │                        │
└─────────────────────────────────────────────────┴────────────────────────┘
```

- Left panel (~75%): Filter bar + scrollable data table
- Right panel (~25%): Summary cards + bulk action buttons

## Table Columns

| Column | Content |
|--------|---------|
| Checkbox | Disabled for "In Zoho" rows |
| Vendor | `vendor_name`, truncated with tooltip |
| Date | `date` field |
| Amount | Formatted with currency suffix |
| Status | Badge: "In Zoho" (green), "New Bill - GSTIN/Name/Fuzzy" (blue), "New Vendor" (yellow) |
| Action | "Create" button (only for selectable rows) |

- Column headers are sortable (click to toggle asc/desc)
- "Select All" checkbox in header selects all visible, selectable rows

## Filters

| Filter | Control | Maps to |
|--------|---------|---------|
| Date From/To | Month dropdowns | `organized_month` |
| Vendor | Multi-select dropdown | `vendor_name` |
| Amount Min/Max | Number inputs | `amount` |
| Status | Multi-select dropdown | `action` + `vendor_match_method` |
| Clear | Button | Resets all filters |

All filtering is client-side (data already loaded from API).
Summary panel counts update to reflect filtered subset.

## Interactions

1. **Modal open** -> calls `/api/bills/match-preview` (existing endpoint, no changes)
2. **Filters change** -> table + summary update instantly (client-side)
3. **Sort column** -> click header to toggle sort direction
4. **Select all** -> checks all visible selectable rows
5. **Create Selected (N)** -> confirmation modal -> `runStepWithKwargs('3', {selected_files: [...]})`
6. **Per-row Create** -> confirmation modal -> `runStepWithKwargs('3', {selected_files: [file]})`

## What Changes

- `openBillPicker()` JS function — rewritten for flat table + filters
- CSS — new styles for table, filter bar, checkboxes, sortable headers
- New JS functions: `applyBillFilters()`, `sortBillTable()`, `updateBillSelection()`, `createSelectedBills()`
- `_renderSummaryPanel()` — updated with "Create Selected (N)" and selection count

## What Stays the Same

- Modal HTML shell (`billPickerModal`, `billPickerBody`, `billPickerSummary`)
- `/api/bills/match-preview` endpoint (no backend changes)
- `createOneBill()`, `runStepWithKwargs()` functions
- Fallback `_loadBasicBillPicker()` path
- Confirmation modal system (`showModal`)

## What Gets Removed

- `toggleBillMonth()` — no more month accordions
- `createMonthBillsPreview()` / `createMonthBills()` — no more per-month bulk actions
- Month-grouping rendering logic in `openBillPicker()`

## Scope Estimate

~300 lines of JS/CSS replaced in `app.py`. Pure frontend change, no new files, no backend modifications.
