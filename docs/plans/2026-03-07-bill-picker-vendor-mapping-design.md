# Bill Picker: Vendor Mapping & Layout Redesign

**Date:** 2026-03-07
**Approach:** Frontend-Only Overrides (Approach A)

## Summary

Redesign the bill picker modal to support multi-vendor filtering, Zoho vendor mapping, and an improved layout. Users can select invoices, map them to specific Zoho vendors, and persist those mappings across sessions.

## Decisions

| Question | Decision |
|---|---|
| Vendor mapping persistence | Persistent — saved to `output/vendor_overrides.json` |
| Zoho Vendor column visibility | All rows |
| Parsed vendor dropdown style | Checkbox list dropdown with Select All/Clear + count badge |
| Zoho vendor dropdown placement | Separate mapping bar below filters, above table |
| Zoho vendor dropdown style | Searchable single-select (combobox with type-to-filter) |
| Apply mapping behavior | Update column + persist + change status (New Vendor -> New Bill + Existing Vendor) |

## 1. Layout Restructure

Current: filters crammed left (3:1 flex ratio), summary panel permanently on right.

New layout (top to bottom, full width):

1. **Filter bar** — full width. From, To, Vendor (checkbox dropdown), Min Amt, Max Amt, Status (checkbox dropdown), Match Type (checkbox dropdown), Clear button.
2. **Mapping bar** — full width. "Map selected to Zoho Vendor: [searchable dropdown] [Apply]"
3. **Table** — full width, scrollable. Columns: checkbox, Vendor, Date, Amount, Status, Match, Zoho Vendor (NEW), Action.
4. **Bottom bar** — summary stats (Total, In Zoho, Existing Vendor, New Vendor, Will Upload, Selected) + Create Selected / Cancel buttons, laid out horizontally.

CSS changes:
- `.bill-picker-layout` changes from `flex-row` to `flex-column`
- `.bill-picker-right` becomes a bottom bar with `flex-direction: row` and `flex-wrap: wrap`

## 2. Checkbox Vendor Filter Dropdown

Replaces native `<select multiple>` for `bfVendor`, `bfStatus`, and `bfMatchType`.

Component structure:
- Button showing label + count badge (e.g., "Vendor (3)")
- Dropdown panel with Select All / Clear links at top
- Scrollable list of checkboxes
- Click outside closes dropdown
- Each check/uncheck triggers `applyBillFilters()` immediately

## 3. Zoho Vendor Searchable Dropdown & Mapping Bar

Data source: `zoho_vendors_cache.json` loaded via `GET /api/zoho-vendors`.

Searchable single-select:
- Text input with type-to-filter (case-insensitive substring)
- Dropdown list of matching Zoho vendors
- Click to select, closes dropdown, shows name in input

Apply button logic:
1. Validate: at least one row selected + Zoho vendor chosen
2. POST to `/api/vendor-overrides` — persist `{parsed_vendor_name: {contact_id, contact_name}}`
3. Update in-memory `_matchPreviewData`: set `matched_vendor_id`, `matched_vendor_name`, change `action` from `new_vendor` to `new_bill`, set `vendor_match_method` to `"manual"`
4. Re-render table and summary counts

## 4. Zoho Vendor Column

New column after "Match" in the table.

| Row Status | Zoho Vendor Shows | Source |
|---|---|---|
| In Zoho | Vendor from matched Zoho bill | Auto from match-preview |
| New Bill + Existing Vendor | `matched_vendor_name` (auto-match) | Auto from match-preview |
| New Bill + Existing Vendor (manual) | `matched_vendor_name` (user override) | `vendor_overrides.json` |
| New Bill + New Vendor | Empty (until user maps) | — |

- Column is sortable
- Manual overrides show match type badge as "MANUAL"

## 5. Backend Changes

### New endpoints

**`GET /api/zoho-vendors`** — returns `zoho_vendors_cache.json` as `[{contact_id, contact_name}]`.

**`GET /api/vendor-overrides`** — returns `output/vendor_overrides.json` contents.

**`POST /api/vendor-overrides`** — body: `{"overrides": {"parsed_vendor_name": {"contact_id": "...", "contact_name": "..."}}}`. Merges with existing and saves.

### Modified: `createSelectedBills()` JS

Passes `vendor_overrides` alongside `selected_files`:
```js
runStepWithKwargs('3', {selected_files: files, vendor_overrides: overridesDict});
```

Where `overridesDict` is `{filename: {contact_id, contact_name}}` per selected file.

### Modified: `scripts/03_create_vendors_bills.py`

Accepts `vendor_overrides` kwarg. Before GSTIN/name/fuzzy resolution:
```python
if fname in vendor_overrides:
    vendor_id = vendor_overrides[fname]["contact_id"]
    vendor_name = vendor_overrides[fname]["contact_name"]
else:
    # existing resolution chain
```

## 6. Data Flow

```
Open Bill Picker
  -> POST /api/bills/match-preview
  -> GET /api/vendor-overrides
  -> JS applies overrides to preview in-memory
  -> Render: filters -> mapping bar -> table (with Zoho Vendor col) -> bottom summary

User maps vendors:
  -> Select rows + pick Zoho vendor + click Apply
  -> POST /api/vendor-overrides (persist)
  -> JS updates in-memory data, re-renders

User creates bills:
  -> Click Create Selected
  -> JS builds {filename: {contact_id, contact_name}} from in-memory data
  -> POST /api/run/3 with {selected_files, vendor_overrides}
  -> Step 3 uses override vendor_id, skips auto-resolution
```

## Files Changed

| File | Changes |
|---|---|
| `app.py` (CSS) | Layout restructure, checkbox dropdown styles, mapping bar, bottom summary bar |
| `app.py` (HTML) | Minimal — modal shell stays, content is JS-generated |
| `app.py` (JS) | Checkbox dropdown component, searchable dropdown, mapping bar, Zoho Vendor column, override logic |
| `app.py` (Python) | New `/api/vendor-overrides` (GET/POST), new `/api/zoho-vendors` (GET) |
| `scripts/03_create_vendors_bills.py` | Accept `vendor_overrides` kwarg, priority 0 check |

## New Files

| File | Purpose |
|---|---|
| `output/vendor_overrides.json` | Persistent vendor mappings (auto-created on first Apply) |

## What's NOT Changing

- Match-preview backend logic (no overrides applied server-side)
- Existing auto-resolution (GSTIN/name/fuzzy) untouched
- "In Zoho" rows remain non-selectable
- Individual "Create" button per row works as before
