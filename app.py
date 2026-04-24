"""
Web UI Dashboard — CC Statement Automation

Single-file Flask app with embedded HTML/CSS/JS.
Provides a browser-based dashboard to run pipeline steps, view live logs, and track status.

Usage:
    python app.py              # Starts server and opens browser at http://localhost:5000
    python app.py --port 8080  # Custom port
    python app.py --no-open    # Don't auto-open browser
"""

import os
import sys
import csv
import json
import re
import glob
import queue
import threading
import time
import importlib.util
import argparse
import webbrowser
import traceback
from datetime import datetime

from flask import Flask, Response, jsonify, request, render_template_string

# --- Setup paths ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from utils import PROJECT_ROOT as UTILS_ROOT, log_action, _log_subscribers

app = Flask(__name__)


# --- Vendor-gated bill matching algorithm ---

def _build_vendor_gated_matches(bills, cc_list, manual_vendor_map, learned_vendor_map, forex_rates=None):
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
    import re as _re
    from datetime import datetime as _dt
    from scripts.utils import is_gateway_only, strip_vendor_stop_words, VENDOR_STOP_WORDS, GATEWAY_KEYWORDS

    _USD_RE = _re.compile(r'USD\s*([\d,]+\.?\d*)')

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

    def _norm(s):
        return "".join(c for c in s.lower() if c.isalnum())

    # --- Build lookup structures for manual mappings ---
    vm_lower = {k.lower(): v for k, v in manual_vendor_map.items()}
    vm_norm = {_norm(k): v for k, v in manual_vendor_map.items()}
    sorted_keys = sorted(vm_lower.keys(), key=len, reverse=True)
    sorted_norm_keys = sorted(vm_norm.keys(), key=len, reverse=True)

    # --- Build bill vendor lookup for fuzzy fallback ---
    # Maps normalized first-keyword of vendor name → original vendor name
    bill_vendors = {}
    for b in bills:
        vn = b.get("vendor_name", "")
        if vn:
            bill_vendors[vn.lower()] = vn
            # Also index by first meaningful word (strip stop words)
            stripped = strip_vendor_stop_words(vn)
            first_word = stripped.split()[0].lower() if stripped.split() else ""
            if first_word and len(first_word) >= 4:
                bill_vendors[first_word] = vn

    def _resolve_vendor(desc):
        """Resolve CC description to vendor name. Returns (vendor_name, source) or (None, None)."""
        if not desc:
            return None, None
        dl = desc.lower()
        dn = _norm(desc)
        du = desc.strip().upper()
        dl_clean = "".join(c for c in dl if c.isalnum() or c == ' ')

        # Priority 1: Manual mappings (exact, normalized, substring)
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

        # Priority 2: Learned mappings (exact uppercase)
        if du in learned_vendor_map:
            return learned_vendor_map[du], "learned"
        # Learned substring match
        for key in sorted(learned_vendor_map.keys(), key=len, reverse=True):
            if key and len(key) >= 4 and key in du:
                return learned_vendor_map[key], "learned"

        # Priority 3: Fuzzy keyword match against bill vendor names
        # Extract first meaningful keyword from CC description
        _noise = {"si", "in", "mumbai", "bangalore", "chennai", "delhi",
                  "india", "ca", "us", "www", "https", "com", "pte"}
        desc_tokens = [t.lower() for t in desc.replace(",", " ").split()
                       if t.lower() not in _noise
                       and t.lower() not in VENDOR_STOP_WORDS
                       and t.lower() not in GATEWAY_KEYWORDS
                       and len(t) >= 4]
        for token in desc_tokens:
            tn = _norm(token)
            if len(tn) < 4:
                continue
            for bv_key, bv_name in bill_vendors.items():
                if tn in _norm(bv_key) or _norm(bv_key) in tn:
                    return bv_name, "fuzzy"

        # Priority 4: Gateway check — if gateway-only, no vendor signal
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
                    if bill_amt * 80 <= cc_inr <= bill_amt * 100:
                        diff = abs(cc_inr - bill_amt * 90)
                        return diff, f"{bill_cur} \u2192 INR (est)"
                return None, None

        return None, None

    def _amount_conf(bill, cc):
        """Compute amount confidence score."""
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

    # --- Enrich CC entries with parsed forex amounts ---
    for cc in cc_list:
        _enrich_forex(cc)

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

            # Score: amount*0.45 + date*0.35 + vendor*0.2 (amount+date are primary)
            overall = int(ac * 0.45 + dc * 0.35 + vc * 0.2)
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
        _, _, mtype_display = _amount_conf(bill, cc)
        if mtype_display:
            entry["match_type"] = mtype_display
        if cc.get("forex_parsed"):
            entry["forex_parsed"] = True
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


def _find_candidates_for_unmatched(unmatched_bills, cc_only_list, forex_rates=None):
    """Find candidate CC transactions for unmatched bills using amount+date scoring.

    Runs AFTER vendor-gated matching as a fallback for bills with no vendor match.
    Scores each (bill, cc) pair by amount proximity, date proximity, vendor name
    overlap, and uniqueness. Returns list with 'candidates' array (top 5) per bill.
    """
    from datetime import datetime as _dt

    results = []
    for bill in unmatched_bills:
        bill_amt = float(bill.get("bill_amount") or bill.get("amount") or 0)
        bill_cur = bill.get("bill_currency") or bill.get("currency") or "INR"
        bill_date_str = bill.get("bill_date") or bill.get("date") or ""
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
                if forex_rates:
                    if bill_date_str in forex_rates:
                        mid_rate = forex_rates[bill_date_str].get("USD_INR", 87.0)
                    elif forex_rates:
                        # Nearest-date fallback: find closest cached rate
                        nearest = min(forex_rates.keys(),
                                      key=lambda d: abs(int(d.replace("-", "")) - int(bill_date_str.replace("-", ""))))
                        mid_rate = forex_rates[nearest].get("USD_INR", 87.0)
                estimated_inr = bill_amt * mid_rate
                diff_pct = abs(cc_inr - estimated_inr) / max(estimated_inr, 0.01) * 100
            else:
                continue

            if diff_pct > 5:
                continue
            elif diff_pct <= 0.01:
                amount_score = 100
            elif diff_pct <= 0.5:
                amount_score = 95
            elif diff_pct <= 1:
                amount_score = 85
            elif diff_pct <= 2:
                amount_score = 70
            elif diff_pct <= 3:
                amount_score = 55
            else:
                amount_score = 40

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
                b["amount"] * 0.5 + b["date"] * 0.25 + b["vendor"] * 0.15 + b["uniqueness"] * 0.1
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


def _build_group_matches(bills, cc_list, manual_vendor_map, learned_vendor_map,
                         multi_bill_vendors, forex_rates=None,
                         used_bill_ids=None, used_cc_ids=None):
    """Second-pass: group multiple bills to one CC transaction.

    Two modes:
    1. Vendor-gated: CC resolves to a known multi_bill_vendor -> find same-vendor bills that sum to CC amount
    2. Auto-detect: For ANY CC txn, find 2+ bills from SAME vendor within date window that sum to CC amount
       (covers vendors not in multi_bill_vendors list, e.g. new vendors, Google, Microsoft variants)
    """
    from datetime import datetime as _dt
    from scripts.utils import strip_vendor_stop_words

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

    multi_norm = set(_norm(v) for v in (multi_bill_vendors or []))
    used_bill_ids = used_bill_ids or set()
    used_cc_ids = used_cc_ids or set()

    avail_bills = [b for b in bills if b["bill_id"] not in used_bill_ids]
    avail_cc = [c for c in cc_list if c.get("transaction_id", "") not in used_cc_ids]

    results = []
    claimed_bills = set()
    claimed_cc = set()

    # --- Build per-vendor bill index for fast lookup ---
    from collections import defaultdict
    vendor_bills = defaultdict(list)  # normalized_vendor -> list of (bill, parsed_date)
    for b in avail_bills:
        vn = b.get("vendor_name", "")
        if not vn:
            continue
        try:
            bd = _dt.strptime(b.get("date", ""), "%Y-%m-%d")
        except Exception:
            continue
        vn_norm = _norm(strip_vendor_stop_words(vn))
        vendor_bills[vn_norm].append((b, bd))

    # --- Also build a set of vendors with 2+ unmatched bills (auto-detect candidates) ---
    auto_group_vendors = set()
    for vn_norm, bill_list in vendor_bills.items():
        if len(bill_list) >= 2:
            auto_group_vendors.add(vn_norm)

    def _try_group(cc, resolved, date_window=15):
        """Try to find a group of same-vendor bills summing to CC amount."""
        cc_tid = cc.get("transaction_id", "")
        cc_inr = float(cc.get("amount", 0))
        if cc_inr <= 0:
            return None
        try:
            cc_date = _dt.strptime(cc.get("date", ""), "%Y-%m-%d")
        except Exception:
            return None

        # Find all same-vendor bills within date window
        resolved_norm = _norm(strip_vendor_stop_words(resolved))
        is_strict_vendor = resolved_norm in ("microsoft", "microsoftcorporationindiapvtltd", "apple")
        cands = []

        for vn_norm, bill_list in vendor_bills.items():
            # Check if this vendor matches the resolved vendor
            if not (vn_norm == resolved_norm
                    or (len(resolved_norm) >= 4 and (resolved_norm in vn_norm or vn_norm in resolved_norm))):
                continue
            for b, bd in bill_list:
                if b["bill_id"] in claimed_bills:
                    continue
                if b.get("currency", "INR") != "INR":
                    continue
                dd = abs((bd - cc_date).days)
                if dd > date_window:
                    continue
                vc = _vendor_match(resolved, b.get("vendor_name", ""))
                if vc < 60:
                    continue
                cands.append((b, dd, vc))

        if len(cands) < 2:
            return None

        # Subset-sum: try all combinations of 2..6 bills within tolerance
        # Microsoft: exact tally (< Rs.1) — their invoices sum precisely to CC amount
        # Others: 1% tolerance for tax/rounding differences
        from itertools import combinations
        tol = 1.0 if is_strict_vendor else max(1.0, cc_inr * 0.01)
        best_group = None
        best_diff = float('inf')
        for size in range(2, min(7, len(cands) + 1)):
            for combo in combinations(cands, size):
                total = sum(b["amount"] for b, _, _ in combo)
                diff = abs(total - cc_inr)
                if diff <= tol and diff < best_diff:
                    best_diff = diff
                    best_group = list(combo)
            if best_group:
                break  # Prefer smaller groups

        if not best_group:
            return None

        running = sum(b["amount"] for b, _, _ in best_group)
        max_dd = max(dd for _, dd, _ in best_group)
        avg_vc = sum(vc for _, _, vc in best_group) // len(best_group)
        sum_diff = abs(running - cc_inr)
        sum_pct = sum_diff / cc_inr if cc_inr else 0
        ac = 100 if sum_pct < 0.001 else 95 if sum_pct < 0.005 else 90 if sum_pct < 0.01 else 75
        # Microsoft: wider date window is normal (invoices 12th, CC payment weeks later)
        # Score date relative to the allowed window, not absolute days
        if is_strict_vendor:
            dc = 100 if max_dd <= 5 else 90 if max_dd <= 15 else 75 if max_dd <= 30 else 60 if max_dd <= 40 else 25
        else:
            dc = 100 if max_dd == 0 else 90 if max_dd <= 2 else 75 if max_dd <= 5 else 50 if max_dd <= 10 else 25
        overall = max(0, int(avg_vc * 0.2 + ac * 0.45 + dc * 0.35) - 5)

        return {
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
                for b, _, _ in best_group
            ],
            "group_sum": running,
            "vendor_name": best_group[0][0].get("vendor_name", ""),
            "vendor_id": best_group[0][0].get("vendor_id", ""),
            "bills": best_group,  # temp, for claiming
        }

    # --- Pass 1: Vendor-gated group matching (known multi_bill_vendors) ---
    for cc in avail_cc:
        cc_tid = cc.get("transaction_id", "")
        if cc_tid in claimed_cc:
            continue
        resolved = _resolve_quick(cc.get("description", ""))
        if not resolved:
            continue

        # Check eligibility against multi_bill_vendors list
        eligible = _norm(resolved) in multi_norm
        if not eligible:
            for mv in (multi_bill_vendors or []):
                if _vendor_match(resolved, mv) >= 60:
                    eligible = True
                    break
        if not eligible:
            continue

        # Microsoft/Apple get 40-day window (~1 month buffer); others get 10 days
        _dw = 40 if _norm(resolved) in ("microsoft", "microsoftcorporationindiapvtltd", "apple") else 10
        result = _try_group(cc, resolved, date_window=_dw)
        if not result:
            continue

        for b, _, _ in result.pop("bills"):
            claimed_bills.add(b["bill_id"])
        claimed_cc.add(cc_tid)
        results.append(result)

    # --- Pass 2: Auto-detect groups for ANY vendor with 2+ unmatched bills ---
    for cc in avail_cc:
        cc_tid = cc.get("transaction_id", "")
        if cc_tid in claimed_cc:
            continue
        resolved = _resolve_quick(cc.get("description", ""))
        if not resolved:
            continue

        # Check if any auto-group vendor matches
        resolved_norm = _norm(strip_vendor_stop_words(resolved))
        has_multi = False
        for vn_norm in auto_group_vendors:
            if (vn_norm == resolved_norm
                    or (len(resolved_norm) >= 4 and (resolved_norm in vn_norm or vn_norm in resolved_norm))):
                has_multi = True
                break
        if not has_multi:
            continue

        # Microsoft/Apple get 40-day window (~1 month buffer); others get 15 days
        _dw2 = 40 if _norm(resolved) in ("microsoft", "microsoftcorporationindiapvtltd", "apple") else 15
        result = _try_group(cc, resolved, date_window=_dw2)
        if not result:
            continue

        for b, _, _ in result.pop("bills"):
            claimed_bills.add(b["bill_id"])
        claimed_cc.add(cc_tid)
        results.append(result)

    # --- Pass 3: Amazon marketplace cross-vendor group matching ---
    # Amazon CC transactions bundle bills from multiple marketplace sellers.
    # Only match bills from known amazon_marketplace_vendors within ±1 day.
    # Also: Microsoft CC uses wider ±5 day window for same-vendor groups.

    # Load amazon marketplace vendors list
    _amz_vendors_raw = []
    try:
        _vm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "vendor_mappings.json")
        with open(_vm_path, "r") as _f:
            _amz_vendors_raw = json.load(_f).get("amazon_marketplace_vendors", [])
    except Exception:
        pass
    amz_vendor_norms = set(_norm(strip_vendor_stop_words(v)) for v in _amz_vendors_raw)

    def _is_amazon_cc(desc):
        """Check if CC description is an Amazon transaction."""
        dl = (desc or "").upper()
        return any(k in dl for k in [
            "AMAZON PAY", "AMAZON INDIA", "AMAZON MARK", "AMAZON MKTPL",
            "AMAZON MKTPLACE", "AMAZON PRIME", "AMAZONIN",
        ])

    def _is_microsoft_cc(desc):
        """Check if CC description is a Microsoft transaction."""
        dl = (desc or "").upper()
        return any(k in dl for k in ["MICROSOFTBUS", "MICROSOFT INDIA", "IND*MICROSOFT"])

    def _try_amazon_group(cc, date_window=1):
        """Match Amazon CC to combination of marketplace vendor bills."""
        cc_tid = cc.get("transaction_id", "")
        cc_inr = float(cc.get("amount", 0))
        if cc_inr <= 0:
            return None
        try:
            cc_date = _dt.strptime(cc.get("date", ""), "%Y-%m-%d")
        except Exception:
            return None

        # Only consider bills from known Amazon marketplace vendors
        cands = []
        for vn_norm, bill_list in vendor_bills.items():
            if vn_norm not in amz_vendor_norms:
                continue
            for b, bd in bill_list:
                if b["bill_id"] in claimed_bills:
                    continue
                if b.get("currency", "INR") != "INR":
                    continue
                dd = abs((bd - cc_date).days)
                if dd > date_window:
                    continue
                cands.append((b, dd))

        if len(cands) < 2:
            return None

        from itertools import combinations
        tol = max(1.0, cc_inr * 0.005)  # 0.5% tolerance for Amazon
        best_group = None
        best_diff = float('inf')
        cands.sort(key=lambda x: x[0]["amount"], reverse=True)
        for size in range(2, min(9, len(cands) + 1)):  # up to 8 bills
            for combo in combinations(cands, size):
                total = sum(b["amount"] for b, _ in combo)
                diff = abs(total - cc_inr)
                if diff <= tol and diff < best_diff:
                    best_diff = diff
                    best_group = list(combo)
            if best_group:
                break

        if not best_group:
            return None

        running = sum(b["amount"] for b, _ in best_group)
        max_dd = max(dd for _, dd in best_group)
        sum_diff = abs(running - cc_inr)
        sum_pct = sum_diff / cc_inr if cc_inr else 0
        ac = 100 if sum_pct < 0.001 else 95 if sum_pct < 0.005 else 90
        dc = 100 if max_dd == 0 else 90 if max_dd <= 1 else 75
        overall = max(0, int(ac * 0.50 + dc * 0.50))

        vendors_in_group = list(set(b.get("vendor_name", "") for b, _ in best_group))

        return {
            "status": "group_matched",
            "match_score": overall,
            "confidence": {"vendor": 0, "amount": ac, "date": dc, "overall": overall},
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
                for b, _ in best_group
            ],
            "group_sum": running,
            "vendor_name": "Amazon → " + ", ".join(vendors_in_group[:3]) + ("..." if len(vendors_in_group) > 3 else ""),
            "vendor_id": best_group[0][0].get("vendor_id", ""),
            "match_type": "amazon_marketplace",
            "bills": best_group,
        }

    # Pass 3a: Amazon marketplace group matching
    for cc in avail_cc:
        cc_tid = cc.get("transaction_id", "")
        if cc_tid in claimed_cc:
            continue
        if not _is_amazon_cc(cc.get("description", "")):
            continue

        result = _try_amazon_group(cc, date_window=1)
        if not result:
            continue

        for b, _ in result.pop("bills"):
            claimed_bills.add(b["bill_id"])
        claimed_cc.add(cc_tid)
        results.append(result)

    return results


# --- Script registry ---
STEPS = {
    "1": {
        "script": "01_fetch_invoices.py",
        "name": "Fetch Invoices from Inbox",
        "desc": "Download invoice PDFs from Outlook",
        "phase": 1,
        "run_kwargs": {"headless": False},
    },
    "2": {
        "script": "02_extract_invoices.py",
        "name": "Extract Data",
        "desc": "Extract vendor/amount/date from PDFs",
        "phase": 1,
        "run_kwargs": {},
    },
    "3": {
        "script": "03_create_vendors_bills.py",
        "name": "Create Bills and Vendors",
        "desc": "Create vendors & bills in Zoho Books",
        "phase": 1,
        "run_kwargs": {},
    },
    "4": {
        "script": "04_parse_cc_statements.py",
        "name": "Parse CC",
        "desc": "Parse CC statement PDFs to CSV + JSON",
        "phase": 2,
        "run_kwargs": {},
    },
    "5": {
        "script": "06_import_to_banking.py",
        "name": "Import Banking",
        "desc": "Import CC transactions to Zoho Banking",
        "phase": 2,
        "run_kwargs": {},
    },
    "6": {
        "script": "05_record_payments.py",
        "name": "Payments",
        "desc": "Record payments (match bills to CC)",
        "phase": 2,
        "run_kwargs": {},
    },
    "7": {
        "script": "07_auto_match.py",
        "name": "Auto-Match",
        "desc": "Auto-match transactions to paid bills",
        "phase": 2,
        "run_kwargs": {},
    },
    "review": {
        "script": None,
        "name": "Review Accounts",
        "desc": "Review and fix expense account assignments on bills",
        "phase": 1,
        "run_kwargs": {},
        "interactive": True,
        "skip_in_run_all": True,
    },
}

# --- In-memory state ---
_state_lock = threading.Lock()
_state = {
    "running": False,
    "current_step": None,
    "step_results": {},  # step_id -> {status, message, timestamp, result}
    "last_run_all": None,
}


def _import_script(filename):
    """Import a script from scripts/ folder by filename."""
    module_name = filename.replace(".py", "")
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(SCRIPTS_DIR, filename)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_step(step_id, extra_kwargs=None):
    """Execute a single pipeline step. Called from background thread."""
    step = STEPS[step_id]

    # Guard: interactive steps cannot be run as scripts
    if step.get("interactive"):
        log_action(f"Step '{step_id}' ({step['name']}) is interactive — use the UI panel instead.", "WARNING")
        with _state_lock:
            _state["step_results"][step_id] = {
                "status": "success",
                "message": "Interactive step — use UI panel",
                "timestamp": datetime.now().isoformat(),
            }
        return

    with _state_lock:
        _state["current_step"] = step_id
        _state["step_results"][step_id] = {
            "status": "running",
            "message": f"Running {step['name']}...",
            "timestamp": datetime.now().isoformat(),
        }

    log_action(f"=== Step {step_id}: {step['name']} ===")
    try:
        mod = _import_script(step["script"])
        kwargs = dict(step["run_kwargs"])
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        result = mod.run(**kwargs)
        result_dict = result if isinstance(result, dict) else {}

        with _state_lock:
            _state["step_results"][step_id] = {
                "status": "success",
                "message": _summarize_result(step_id, result_dict),
                "timestamp": datetime.now().isoformat(),
                "result": _safe_serialize(result_dict),
            }
        log_action(f"=== Step {step_id} DONE: {_state['step_results'][step_id]['message']} ===")
    except Exception as e:
        tb = traceback.format_exc()
        log_action(f"Step {step_id} FAILED: {e}", "ERROR")
        log_action(tb, "ERROR")
        with _state_lock:
            _state["step_results"][step_id] = {
                "status": "error",
                "message": str(e)[:200],
                "timestamp": datetime.now().isoformat(),
            }


def _safe_serialize(obj):
    """Make a result dict JSON-safe (convert sets, etc.)."""
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(v) for v in obj]
    if isinstance(obj, set):
        return list(obj)
    return obj


def _summarize_result(step_id, result):
    """Create a human-readable summary from step result dict."""
    if not result:
        return "Done"
    parts = []
    for key in ["downloaded_count", "new_count", "created_count", "total_transactions",
                 "paid_count", "imported_count", "matched_count"]:
        if key in result:
            label = key.replace("_", " ").replace("count", "").strip()
            parts.append(f"{label}: {result[key]}")
    for key in ["skipped_count", "total_count", "cards_parsed"]:
        if key in result:
            label = key.replace("_", " ").replace("count", "").strip()
            parts.append(f"{label}: {result[key]}")
    return " | ".join(parts) if parts else "Done"


def _run_step_thread(step_id, extra_kwargs=None):
    """Run a step in a background thread with lock management."""
    try:
        _run_step(step_id, extra_kwargs=extra_kwargs)
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


def _run_all_thread():
    """Run all steps sequentially in a background thread."""
    log_action("=" * 60)
    log_action("RUN ALL: Starting full pipeline (Steps 1-6)")
    log_action("=" * 60)
    with _state_lock:
        _state["last_run_all"] = datetime.now().isoformat()

    for step_id in ["1", "2", "3", "review", "4", "5", "6"]:
        step = STEPS[step_id]
        # Skip interactive steps in Run All
        if step.get("skip_in_run_all") or step.get("interactive"):
            log_action(f"Skipping '{step['name']}' (interactive step — use UI panel)")
            continue
        # Check if still supposed to be running (could be cancelled)
        with _state_lock:
            if not _state["running"]:
                log_action("Run All cancelled.", "WARNING")
                return
        _run_step(step_id)
        # Stop on error
        with _state_lock:
            if _state["step_results"].get(step_id, {}).get("status") == "error":
                log_action(f"Run All stopped: Step {step_id} failed.", "ERROR")
                break

    log_action("=" * 60)
    log_action("RUN ALL: Complete")
    log_action("=" * 60)

    with _state_lock:
        _state["running"] = False
        _state["current_step"] = None


def _run_cleanup_thread():
    """Run cleanup in a background thread."""
    log_action("=" * 60)
    log_action("CLEANUP: Starting complete cleanup")
    log_action("=" * 60)

    with _state_lock:
        _state["current_step"] = "cleanup"

    try:
        cleanup_mod = _import_script("cleanup_all.py")
        from utils import load_config, ZohoBooksAPI, resolve_account_ids

        config = load_config()
        api_obj = ZohoBooksAPI(config)
        resolve_account_ids(api_obj, config.get("credit_cards", []))

        cleanup_mod.cleanup_banking(api_obj, config)
        cleanup_mod.cleanup_vendor_payments(api_obj)
        cleanup_mod.cleanup_bills(api_obj)
        cleanup_mod.cleanup_vendors(api_obj)
        cleanup_mod.cleanup_remaining_bank_txns(api_obj, config)
        cleanup_mod.cleanup_local_files()

        log_action("CLEANUP: Complete")
        with _state_lock:
            _state["step_results"]["cleanup"] = {
                "status": "success",
                "message": "All Zoho data + local files cleaned",
                "timestamp": datetime.now().isoformat(),
            }
    except Exception as e:
        log_action(f"CLEANUP FAILED: {e}", "ERROR")
        log_action(traceback.format_exc(), "ERROR")
        with _state_lock:
            _state["step_results"]["cleanup"] = {
                "status": "error",
                "message": str(e)[:200],
                "timestamp": datetime.now().isoformat(),
            }
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


# --- Flask Routes ---

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/extract-zips", methods=["POST"])
def api_extract_zips():
    """Extract PDFs from ZIP files in 'all zips' folder, then run Step 2 to organize month-wise."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True

    def _extract_zips_thread():
        try:
            with _state_lock:
                _state["current_step"] = "extract-zips"
                _state["step_results"]["extract-zips"] = {
                    "status": "running",
                    "message": "Extracting ZIPs...",
                    "timestamp": datetime.now().isoformat(),
                }

            log_action("=== Extract ZIPs ===")

            # Extract PDFs from ZIPs into input_pdfs/invoices/
            mod_zip = _import_script("extract_zips.py")
            zip_results = mod_zip.extract_pdfs_all()

            msg = f"Extracted {zip_results['copied']} new PDFs from ZIPs into input_pdfs/invoices/"
            with _state_lock:
                _state["step_results"]["extract-zips"] = {
                    "status": "success",
                    "message": msg,
                    "timestamp": datetime.now().isoformat(),
                }
            log_action(f"=== Extract ZIPs DONE: {msg} — Run Extract Data next ===")
        except Exception as e:
            tb = traceback.format_exc()
            log_action(f"Extract ZIPs FAILED: {e}", "ERROR")
            log_action(tb, "ERROR")
            with _state_lock:
                _state["step_results"]["extract-zips"] = {
                    "status": "error",
                    "message": str(e)[:200],
                    "timestamp": datetime.now().isoformat(),
                }
        finally:
            with _state_lock:
                _state["running"] = False
                _state["current_step"] = None

    t = threading.Thread(target=_extract_zips_thread, daemon=True)
    t.start()
    return jsonify({"ok": True, "step": "extract-zips"})


@app.route("/api/extract-mail-invoices", methods=["POST"])
def api_extract_mail_invoices():
    """Extract invoices from 'input_pdfs/mail invoices' folder only, save to separate mail_extracted_invoices.json."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True

    def _extract_mail_thread():
        try:
            with _state_lock:
                _state["current_step"] = "extract-mail"
                _state["step_results"]["extract-mail"] = {
                    "status": "running",
                    "message": "Extracting mail invoices...",
                    "timestamp": datetime.now().isoformat(),
                }

            log_action("=== Fetch & Extract Mail Invoices ===")

            # Step 1: Fetch invoices from Outlook first
            try:
                fetch_mod = _import_script("01_fetch_invoices.py")
                log_action("Fetching invoice PDFs from Outlook...")
                fetch_mod.run(headless=False)
            except Exception as fetch_ex:
                log_action(f"Fetch from Outlook failed: {fetch_ex}", "WARNING")
                log_action("Continuing with existing files in mail invoices folder...")

            mail_dir = os.path.join(PROJECT_ROOT, "input_pdfs", "mail invoices")
            if not os.path.isdir(mail_dir):
                raise FileNotFoundError(f"Folder not found: {mail_dir}")

            # Find all extractable files
            exts = (".pdf", ".jpg", ".jpeg", ".png", ".eml")
            files = [(f, os.path.join(mail_dir, f)) for f in os.listdir(mail_dir)
                     if f.lower().endswith(exts)]
            log_action(f"Found {len(files)} files in input_pdfs/mail invoices/")

            # Output: separate JSON file for mail-extracted invoices
            out_path = os.path.join(PROJECT_ROOT, "output", "mail_extracted_invoices.json")

            # Load existing mail extractions to skip already-done
            existing = []
            if os.path.exists(out_path):
                with open(out_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        existing = json.loads(content)
            already_done = {inv["file"] for inv in existing}

            # Import extract function from Step 2
            mod = _import_script("02_extract_invoices.py")

            new_extracted = []
            failed_files = []
            skipped_files = []
            skipped = 0
            failed = 0
            for fname, fpath in sorted(files):
                if fname in already_done:
                    log_action(f"  Skipping (already extracted): {fname}")
                    skipped_files.append({"file": fname, "reason": "Already extracted"})
                    skipped += 1
                    continue
                log_action(f"  Extracting: {fname}")
                try:
                    invoice = mod.extract_invoice(fpath, fname)
                    if invoice:
                        if isinstance(invoice, list):
                            new_extracted.extend(invoice)
                            for inv in invoice:
                                log_action(f"    [{inv.get('invoice_number', '?')}] {inv['vendor_name']}: {inv['amount']} {inv['currency']}")
                        else:
                            new_extracted.append(invoice)
                            log_action(f"    {invoice['vendor_name']}: {invoice['amount']} {invoice['currency']}, Date: {invoice['date']}")
                    else:
                        log_action(f"    No data extracted from {fname}", "WARNING")
                        failed_files.append({"file": fname, "reason": "No data extracted"})
                        failed += 1
                except Exception as ex:
                    log_action(f"    FAILED: {fname}: {ex}", "ERROR")
                    failed_files.append({"file": fname, "reason": str(ex)[:200]})
                    failed += 1

            # Save to mail_extracted_invoices.json (append to existing)
            if new_extracted:
                existing.extend(new_extracted)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)

            # --- Also merge into extracted_invoices.json (like upload flow) ---
            ext_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
            existing_extracted = []
            if os.path.exists(ext_path) and os.path.getsize(ext_path) > 0:
                with open(ext_path, "r", encoding="utf-8") as f:
                    existing_extracted = json.load(f)
            already_done_ext = {inv.get("file") for inv in existing_extracted}

            added_to_extracted = 0
            for inv in new_extracted:
                if inv.get("file") not in already_done_ext:
                    existing_extracted.append(inv)
                    already_done_ext.add(inv.get("file"))
                    added_to_extracted += 1

            if added_to_extracted:
                with open(ext_path, "w", encoding="utf-8") as f:
                    json.dump(existing_extracted, f, indent=2, ensure_ascii=False)
                log_action(f"Updated extracted_invoices.json: +{added_to_extracted} (total {len(existing_extracted)})")

            # --- Also merge into compare_invoices.json (like upload flow) ---
            cmp_path = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")
            existing_compare = []
            if os.path.exists(cmp_path) and os.path.getsize(cmp_path) > 0:
                with open(cmp_path, "r", encoding="utf-8") as f:
                    existing_compare = json.load(f)
            already_done_cmp = {inv.get("file") for inv in existing_compare}

            generic = {"payment", "original", "invoice", "receipt", "bill", "tax", "none", "n/a", ""}
            seen_nums = set()
            for inv in existing_compare:
                num = inv.get("invoice_number", "")
                if num and num.lower().strip() not in generic:
                    seen_nums.add(num)

            added_to_compare = 0
            for inv in new_extracted:
                if inv.get("file") in already_done_cmp:
                    continue
                cmp_item = dict(inv)
                cmp_item["organized_month"] = "Mail"
                cmp_item["organized_path"] = inv.get("path", "")
                num = inv.get("invoice_number", "")
                if num and num.lower().strip() not in generic and num in seen_nums:
                    log_action(f"  Dedup: skipping {inv.get('file', '?')} (#{num} already in compare)")
                    continue
                if num and num.lower().strip() not in generic:
                    seen_nums.add(num)
                existing_compare.append(cmp_item)
                added_to_compare += 1

            if added_to_compare:
                with open(cmp_path, "w", encoding="utf-8") as f:
                    json.dump(existing_compare, f, indent=2, ensure_ascii=False)
                log_action(f"Updated compare_invoices.json: +{added_to_compare} (total {len(existing_compare)})")

            # Write preview snapshot for UI
            preview_path = os.path.join(PROJECT_ROOT, "output", "extract_preview.json")
            try:
                with open(preview_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "source": "mail",
                        "timestamp": datetime.now().isoformat(),
                        "extracted": new_extracted,
                        "failed": failed_files,
                        "skipped": skipped_files,
                    }, f, indent=2, ensure_ascii=False)
            except Exception as pe:
                log_action(f"  Failed to write extract_preview.json: {pe}", "WARNING")

            total = len(existing)
            msg = f"Extracted {len(new_extracted)} new, skipped {skipped}, failed {failed} | Total: {total} in mail_extracted_invoices.json | +{added_to_extracted} to extracted, +{added_to_compare} to compare"
            with _state_lock:
                _state["step_results"]["extract-mail"] = {
                    "status": "success",
                    "message": msg,
                    "timestamp": datetime.now().isoformat(),
                    "result": {"extracted_count": len(new_extracted), "failed_count": failed, "skipped_count": skipped},
                }
            log_action(f"=== Mail Extract DONE: {msg} ===")
        except Exception as e:
            tb = traceback.format_exc()
            log_action(f"Mail Extract FAILED: {e}", "ERROR")
            log_action(tb, "ERROR")
            with _state_lock:
                _state["step_results"]["extract-mail"] = {
                    "status": "error",
                    "message": str(e)[:200],
                    "timestamp": datetime.now().isoformat(),
                }
        finally:
            with _state_lock:
                _state["running"] = False
                _state["current_step"] = None

    t = threading.Thread(target=_extract_mail_thread, daemon=True)
    t.start()
    return jsonify({"ok": True, "step": "extract-mail"})


@app.route("/api/compare-mail-invoices", methods=["POST"])
def api_compare_mail_invoices():
    """Compare mail_extracted_invoices.json against extracted_invoices.json.
    For each mail invoice, check if it exists in the main extracted file.
    Save results to output/mail_vs_extracted_compare.json.
    """
    try:
        mail_path = os.path.join(PROJECT_ROOT, "output", "mail_extracted_invoices.json")
        ext_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
        out_path = os.path.join(PROJECT_ROOT, "output", "mail_vs_extracted_compare.json")

        if not os.path.exists(mail_path):
            return jsonify({"error": "mail_extracted_invoices.json not found. Run Mail Extract first."}), 404
        if not os.path.exists(ext_path):
            return jsonify({"error": "extracted_invoices.json not found. Run Extract Data first."}), 404

        with open(mail_path, "r", encoding="utf-8") as f:
            mail_invoices = json.load(f)
        with open(ext_path, "r", encoding="utf-8") as f:
            ext_invoices = json.load(f)

        # Build lookup indices from extracted_invoices
        ext_by_file = {}
        ext_by_invnum = {}
        ext_by_vendor_amt_date = {}
        ext_by_vendor_amt = {}
        for inv in ext_invoices:
            fn = inv.get("file", "")
            if fn:
                ext_by_file[fn.lower()] = inv
            inv_num = (inv.get("invoice_number") or "").strip()
            if inv_num and inv_num.lower() not in ("unknown", "n/a", "none", ""):
                ext_by_invnum[inv_num.lower()] = inv
            # vendor + amount + date key
            vn = (inv.get("vendor_name") or "").strip().lower()
            amt = round(float(inv.get("amount") or 0), 2)
            dt = (inv.get("date") or "").strip()
            if vn and amt:
                ext_by_vendor_amt_date[(vn, amt, dt)] = inv
                key_va = (vn, amt)
                if key_va not in ext_by_vendor_amt:
                    ext_by_vendor_amt[key_va] = []
                ext_by_vendor_amt[key_va].append(inv)

        results = []
        found_count = 0
        missing_count = 0

        for mi in mail_invoices:
            mi_file = (mi.get("file") or "").lower()
            mi_invnum = (mi.get("invoice_number") or "").strip().lower()
            mi_vendor = (mi.get("vendor_name") or "").strip().lower()
            mi_amt = round(float(mi.get("amount") or 0), 2)
            mi_date = (mi.get("date") or "").strip()

            match = None
            match_type = None

            # 1. Exact filename match
            if mi_file and mi_file in ext_by_file:
                match = ext_by_file[mi_file]
                match_type = "exact_file"
            # 2. Invoice number match
            elif mi_invnum and mi_invnum not in ("unknown", "n/a", "none", "") and mi_invnum in ext_by_invnum:
                match = ext_by_invnum[mi_invnum]
                match_type = "invoice_number"
            # 3. Vendor + amount + date match
            elif (mi_vendor, mi_amt, mi_date) in ext_by_vendor_amt_date:
                match = ext_by_vendor_amt_date[(mi_vendor, mi_amt, mi_date)]
                match_type = "vendor_amount_date"
            # 4. Vendor + amount match (any date)
            elif (mi_vendor, mi_amt) in ext_by_vendor_amt:
                match = ext_by_vendor_amt[(mi_vendor, mi_amt)][0]
                match_type = "vendor_amount"

            status = "found" if match else "missing"
            if match:
                found_count += 1
            else:
                missing_count += 1

            row = {
                "mail_file": mi.get("file"),
                "mail_vendor": mi.get("vendor_name"),
                "mail_invoice_number": mi.get("invoice_number"),
                "mail_date": mi.get("date"),
                "mail_amount": mi.get("amount"),
                "mail_currency": mi.get("currency"),
                "status": status,
                "match_type": match_type,
            }
            if match:
                row["matched_file"] = match.get("file")
                row["matched_vendor"] = match.get("vendor_name")
                row["matched_invoice_number"] = match.get("invoice_number")
                row["matched_date"] = match.get("date")
                row["matched_amount"] = match.get("amount")
                row["matched_path"] = match.get("organized_path") or match.get("path")
            results.append(row)

        summary = {
            "total_mail": len(mail_invoices),
            "total_extracted": len(ext_invoices),
            "found": found_count,
            "missing": missing_count,
            "compared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        output = {"summary": summary, "results": results}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        log_action(f"Mail vs Extracted compare: {found_count} found, {missing_count} missing out of {len(mail_invoices)} mail invoices")
        return jsonify({"status": "ok", **summary, "path": out_path})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/run/<step>", methods=["POST"])
def api_run(step):
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True

    if step == "all":
        t = threading.Thread(target=_run_all_thread, daemon=True)
    elif step == "cleanup":
        t = threading.Thread(target=_run_cleanup_thread, daemon=True)
    elif step in STEPS:
        # Guard interactive steps from being "run"
        if STEPS[step].get("interactive"):
            with _state_lock:
                _state["running"] = False
            return jsonify({"error": f"'{STEPS[step]['name']}' is interactive — use the review panel UI"}), 400

        # Accept optional run_kwargs from JSON body
        extra_kwargs = None
        if request.is_json and request.json:
            extra_kwargs = request.json.get("run_kwargs")

        t = threading.Thread(target=_run_step_thread, args=(step, extra_kwargs), daemon=True)
    else:
        with _state_lock:
            _state["running"] = False
        return jsonify({"error": f"Unknown step: {step}"}), 400

    t.start()
    return jsonify({"ok": True, "step": step})


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify({
            "running": _state["running"],
            "current_step": _state["current_step"],
            "step_results": _state["step_results"],
            "last_run_all": _state["last_run_all"],
            "summary": _get_summary(),
        })


@app.route("/api/logs")
def api_logs_sse():
    """SSE endpoint for live log streaming."""
    q = queue.Queue(maxsize=500)
    _log_subscribers.append(q)

    def stream():
        try:
            while True:
                try:
                    line = q.get(timeout=30)
                    yield f"data: {json.dumps({'line': line})}\n\n"
                except queue.Empty:
                    # Send keepalive
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                _log_subscribers.remove(q)
            except ValueError:
                pass

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/logs/history")
def api_logs_history():
    """Return last N lines from automation.log."""
    log_path = os.path.join(PROJECT_ROOT, "output", "automation.log")
    n = request.args.get("n", 200, type=int)
    lines = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                lines = [l.rstrip() for l in all_lines[-n:]]
        except Exception:
            pass
    return jsonify({"lines": lines})


def _update_vendor_account_mapping(vendor_name, account_id, account_name):
    """Update vendor_mappings.json account_mappings so future bills auto-use the corrected account."""
    mappings_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    try:
        with open(mappings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "account_mappings" not in data:
            data["account_mappings"] = {}
        data["account_mappings"][vendor_name] = {
            "account_name": account_name,
            "account_id": account_id,
        }
        with open(mappings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        log_action(f"Updated vendor mapping: {vendor_name} -> {account_name}")
    except Exception as e:
        log_action(f"Failed to update vendor mappings: {e}", "WARNING")


def _invalidate_bill_cache(bill_id, account_name=None, account_id=None):
    """Update bill detail cache entry after account change (avoids stale data on next Review load)."""
    cache_path = os.path.join(PROJECT_ROOT, "output", "bill_detail_cache.json")
    try:
        if not os.path.exists(cache_path):
            return
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if bill_id in cache:
            if account_name is not None:
                cache[bill_id]["account_name"] = account_name
            if account_id is not None:
                cache[bill_id]["account_id"] = account_id
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


@app.route("/api/review/bills")
def api_review_bills():
    """Load created bills and fetch actual account assignments from Zoho."""
    bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
    if not os.path.exists(bills_path) or os.path.getsize(bills_path) == 0:
        return jsonify({"error": "No new bills"}), 404

    try:
        with open(bills_path, "r", encoding="utf-8") as f:
            local_bills = json.load(f)
    except json.JSONDecodeError:
        return jsonify({"error": "created_bills.json is empty or corrupt. Run Step 3 first."}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to read bills file: {e}"}), 500

    # Load local mappings as fallback
    from utils import load_vendor_mappings, load_config, ZohoBooksAPI
    vendor_mappings = load_vendor_mappings()
    account_mappings = vendor_mappings.get("account_mappings", {})
    default_account = vendor_mappings.get("default_expense_account", "Credit Card Charges")

    # Load extracted invoices for real line item descriptions
    # Build multiple lookup keys for fuzzy matching (handles filename variations)
    extracted_by_file = {}
    inv_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
    try:
        if os.path.exists(inv_path):
            with open(inv_path, "r", encoding="utf-8") as f:
                for inv in json.load(f):
                    fname = inv.get("file", "")
                    extracted_by_file[fname] = inv
                    base = fname.rsplit(".pdf", 1)[0] if fname.endswith(".pdf") else fname
                    # Index without Invoice- prefix (e.g. PZLFLIEU-0005 -> Invoice-PZLFLIEU-0005.pdf)
                    if base.startswith("Invoice-"):
                        extracted_by_file[base[8:]] = inv
                        extracted_by_file[base[8:] + ".pdf"] = inv
                    # Index by first segment for multi-page files (e.g. BLR7-1536989_p1_BLR7-1536989.pdf)
                    # so that XXX_p1_XXX_p1_XXX.pdf in created_bills also matches
                    parts = base.split("_p")
                    if len(parts) >= 2:
                        extracted_by_file[parts[0]] = inv
    except Exception:
        pass

    def _find_extracted(file_name):
        """Fuzzy lookup for extracted invoice by filename."""
        if file_name in extracted_by_file:
            return extracted_by_file[file_name]
        # Try first segment before _p for multi-page files
        base = file_name.rsplit(".pdf", 1)[0] if file_name.endswith(".pdf") else file_name
        parts = base.split("_p")
        if len(parts) >= 2 and parts[0] in extracted_by_file:
            return extracted_by_file[parts[0]]
        return {}

    # Collect bill IDs to fetch
    bills_to_show = []
    for entry in local_bills:
        if entry.get("status") != "created" or not entry.get("bill_id"):
            continue
        bills_to_show.append(entry)

    # Load bill detail cache
    bill_cache_path = os.path.join(PROJECT_ROOT, "output", "bill_detail_cache.json")
    bill_cache = {}
    try:
        if os.path.exists(bill_cache_path):
            with open(bill_cache_path, "r", encoding="utf-8") as f:
                bill_cache = json.load(f)
    except Exception:
        bill_cache = {}

    # Split bills into cached vs needs-fetch
    zoho_accounts = {}  # bill_id -> (account_name, account_id)
    bills_to_fetch = []
    for e in bills_to_show:
        bid = e["bill_id"]
        if bid in bill_cache:
            c = bill_cache[bid]
            zoho_accounts[bid] = (c.get("account_name"), c.get("account_id"))
        else:
            bills_to_fetch.append(e)

    from scripts.utils import log_action
    # Fetch only uncached bills from Zoho
    if bills_to_fetch:
        try:
            config = load_config()
            api = ZohoBooksAPI(config)
            # Warm up token before concurrent requests to avoid race condition
            api.list_bills(page=1)
            log_action(f"[Review] Fetching account info for {len(bills_to_fetch)} bills from Zoho (concurrent)... ({len(bills_to_show) - len(bills_to_fetch)} cached)")
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def _fetch_one(bid):
                bill_data = api.get_bill(bid)
                bill = bill_data.get("bill", {})
                line_items = bill.get("line_items", [])
                if line_items:
                    return bid, line_items[0].get("account_name", ""), line_items[0].get("account_id", "")
                return bid, None, None
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_fetch_one, e["bill_id"]): e["bill_id"] for e in bills_to_fetch}
                for fut in as_completed(futures):
                    try:
                        bid, acct_name, acct_id = fut.result()
                        if acct_name:
                            zoho_accounts[bid] = (acct_name, acct_id)
                        # Cache the fetched result
                        bill_cache[bid] = {"account_name": acct_name, "account_id": acct_id}
                    except Exception:
                        pass
            log_action(f"[Review] Got account info for {len(zoho_accounts)}/{len(bills_to_show)} bills ({len(bills_to_fetch)} fetched, {len(bills_to_show) - len(bills_to_fetch)} cached)")
            # Save updated cache
            try:
                with open(bill_cache_path, "w", encoding="utf-8") as f:
                    json.dump(bill_cache, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
        except Exception as e:
            log_action(f"[Review] Could not fetch from Zoho, using local mappings: {e}", "WARNING")
    else:
        log_action(f"[Review] All {len(bills_to_show)} bills served from cache (0 API calls)")

    # Sync Zoho accounts back to vendor_mappings (so future bills use updated accounts)
    mappings_changed = False
    result = []
    for entry in bills_to_show:
        bid = entry["bill_id"]
        vendor_name = entry.get("vendor_name", "Unknown")

        # Prefer Zoho actual account, fall back to local mapping
        if bid in zoho_accounts:
            acct_data = zoho_accounts[bid]
            account_name = acct_data[0]
            account_id = acct_data[1]
            if account_name:
                # Update local mapping if Zoho has a different account
                local = account_mappings.get(vendor_name, {})
                if account_id and local.get("account_id") != account_id:
                    account_mappings[vendor_name] = {"account_name": account_name, "account_id": account_id}
                    mappings_changed = True
            else:
                mapping = account_mappings.get(vendor_name, {})
                account_name = mapping.get("account_name", default_account)
                account_id = mapping.get("account_id", "")
        else:
            mapping = account_mappings.get(vendor_name, {})
            account_name = mapping.get("account_name", default_account)
            account_id = mapping.get("account_id", "")

        # Line items from extracted invoices (real product/service names)
        file_name = entry.get("file", "")
        extracted = _find_extracted(file_name)
        extracted_li = extracted.get("line_items", [])
        extracted_li_descs = [li.get("description", "") for li in extracted_li if li.get("description")]

        result.append({
            "bill_id": bid,
            "vendor_name": vendor_name,
            "amount": entry.get("amount"),
            "currency": entry.get("currency", "INR"),
            "account_id": account_id,
            "account_name": account_name,
            "description": file_name,
            "line_items": extracted_li_descs,
        })

    if mappings_changed:
        try:
            vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
            vendor_mappings["account_mappings"] = account_mappings
            with open(vm_path, "w", encoding="utf-8") as f:
                json.dump(vendor_mappings, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    return jsonify({"bills": result})


@app.route("/api/review/accounts")
def api_review_accounts():
    """Return sorted list of accounts (expense, other expense, income, fixed asset, other current asset) for dropdowns."""
    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)
    try:
        all_accounts = api.get_all_accounts()
        allowed_types = {"expense", "other_expense", "cost_of_goods_sold", "income", "other_income", "fixed_asset", "other_asset", "other_current_asset", "other_current_liability"}
        sorted_accounts = sorted(
            [
                {"account_id": info["account_id"], "account_name": aname, "account_type": info["account_type"]}
                for aname, info in all_accounts.items()
                if info.get("account_type", "") in allowed_types
            ],
            key=lambda x: x["account_name"],
        )
        return jsonify({"accounts": sorted_accounts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/review/update-account", methods=["POST"])
def api_review_update_account():
    """Update a bill's expense account in Zoho and cache in vendor_mappings."""
    data = request.json
    bill_id = data.get("bill_id")
    account_id = data.get("account_id")
    account_name = data.get("account_name", "")
    vendor_name = data.get("vendor_name", "")

    if not bill_id or not account_id:
        return jsonify({"error": "bill_id and account_id are required"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    try:
        # Fetch current bill to get full line_items
        bill_resp = api.get_bill(bill_id)
        bill = bill_resp.get("bill", {})
        line_items = bill.get("line_items", [])
        if not line_items:
            return jsonify({"error": "Bill has no line items"}), 400

        # Build clean line items with only writable fields for Zoho PUT
        clean_items = []
        for i, li in enumerate(line_items):
            clean = {
                "line_item_id": li.get("line_item_id"),
                "account_id": account_id if i == 0 else li.get("account_id"),
                "description": li.get("description", ""),
                "rate": li.get("rate", 0),
                "quantity": li.get("quantity", 1),
            }
            if li.get("tax_id"):
                clean["tax_id"] = li["tax_id"]
            if li.get("item_id"):
                clean["item_id"] = li["item_id"]
            clean_items.append(clean)

        result = api.update_bill(bill_id, {"line_items": clean_items})
        updated_bill = result.get("bill", {})
        updated_items = updated_bill.get("line_items", [])
        actual_account = updated_items[0].get("account_name", "?") if updated_items else "?"
        log_action(f"Updated bill {bill_id} account to {account_name} ({account_id}) — Zoho confirmed: {actual_account}")

        # Also update vendor_mappings.json for future auto-assignment
        if vendor_name and vendor_name != "Unknown":
            _update_vendor_account_mapping(vendor_name, account_id, account_name)

        # Invalidate bill detail cache for this bill
        _invalidate_bill_cache(bill_id, account_name, account_id)

        return jsonify({"ok": True})
    except Exception as e:
        log_action(f"Failed to update bill account: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/review/bulk-update-account", methods=["POST"])
def api_review_bulk_update_account():
    """Update expense account for all bills of a vendor in one go."""
    data = request.json
    bill_ids = data.get("bill_ids", [])
    account_id = data.get("account_id")
    account_name = data.get("account_name", "")
    vendor_name = data.get("vendor_name", "")

    if not bill_ids or not account_id:
        return jsonify({"error": "bill_ids and account_id are required"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    succeeded = []
    failed = []

    for bill_id in bill_ids:
        try:
            bill_resp = api.get_bill(bill_id)
            bill = bill_resp.get("bill", {})
            line_items = bill.get("line_items", [])
            if not line_items:
                failed.append({"bill_id": bill_id, "error": "No line items"})
                continue

            # Build clean line items with only writable fields
            clean_items = []
            for i, li in enumerate(line_items):
                clean = {
                    "line_item_id": li.get("line_item_id"),
                    "account_id": account_id if i == 0 else li.get("account_id"),
                    "description": li.get("description", ""),
                    "rate": li.get("rate", 0),
                    "quantity": li.get("quantity", 1),
                }
                if li.get("tax_id"):
                    clean["tax_id"] = li["tax_id"]
                if li.get("item_id"):
                    clean["item_id"] = li["item_id"]
                clean_items.append(clean)

            api.update_bill(bill_id, {"line_items": clean_items})
            succeeded.append(bill_id)
            time.sleep(0.3)  # Rate limit
        except Exception as e:
            failed.append({"bill_id": bill_id, "error": str(e)})
            log_action(f"Bulk update failed for bill {bill_id}: {e}", "ERROR")

    # Update vendor mapping once for all bills
    if vendor_name and vendor_name != "Unknown" and succeeded:
        _update_vendor_account_mapping(vendor_name, account_id, account_name)

    # Invalidate bill detail cache for updated bills
    for bid in succeeded:
        _invalidate_bill_cache(bid, account_name, account_id)

    log_action(f"Bulk updated {len(succeeded)}/{len(bill_ids)} bills for {vendor_name} -> {account_name}")
    return jsonify({"ok": True, "succeeded": succeeded, "failed": failed})


@app.route("/api/review/create-account", methods=["POST"])
def api_review_create_account():
    """Create a new expense account in Zoho COA."""
    data = request.json
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name:
        return jsonify({"error": "Account name is required"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    try:
        result = api.create_expense_account(name, description)
        account = result.get("account", {})
        return jsonify({
            "ok": True,
            "account_id": account.get("account_id"),
            "account_name": account.get("account_name", name),
        })
    except Exception as e:
        log_action(f"Failed to create expense account: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bills/list-all")
def api_bills_list_all():
    """List all bills from Zoho for the delete panel."""
    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)
    try:
        all_bills = []
        page = 1
        while True:
            resp = api.list_bills(page=page)
            bills = resp.get("bills", [])
            if not bills:
                break
            for b in bills:
                all_bills.append({
                    "bill_id": b.get("bill_id"),
                    "vendor_name": b.get("vendor_name", ""),
                    "bill_number": b.get("bill_number", ""),
                    "date": b.get("date", ""),
                    "total": b.get("total", 0),
                    "currency_code": b.get("currency_code", "INR"),
                    "status": b.get("status", ""),
                })
            if not resp.get("page_context", {}).get("has_more_page"):
                break
            page += 1
        log_action(f"[Delete] Listed {len(all_bills)} bills from Zoho")
        return jsonify({"bills": all_bills})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bills/delete", methods=["POST"])
def api_bills_delete():
    """Delete one or more bills from Zoho."""
    data = request.json
    bill_ids = data.get("bill_ids", [])
    if not bill_ids:
        return jsonify({"error": "No bill_ids provided"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    succeeded = []
    failed = []
    for bid in bill_ids:
        try:
            api.delete_bill(bid)
            succeeded.append(bid)
        except Exception as e:
            failed.append({"bill_id": bid, "error": str(e)})
            log_action(f"[Delete] Failed to delete bill {bid}: {e}", "ERROR")

    # Also remove from created_bills.json
    if succeeded:
        bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
        try:
            if os.path.exists(bills_path):
                with open(bills_path, "r", encoding="utf-8") as f:
                    local_bills = json.load(f)
                deleted_set = set(succeeded)
                local_bills = [b for b in local_bills if b.get("bill_id") not in deleted_set]
                with open(bills_path, "w", encoding="utf-8") as f:
                    json.dump(local_bills, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        # Also remove from bill_detail_cache
        cache_path = os.path.join(PROJECT_ROOT, "output", "bill_detail_cache.json")
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                for bid in succeeded:
                    cache.pop(bid, None)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    log_action(f"[Delete] Deleted {len(succeeded)}/{len(bill_ids)} bills from Zoho")
    return jsonify({"ok": True, "succeeded": succeeded, "failed": failed})


@app.route("/api/vendors/list-all")
def api_vendors_list_all():
    """List all vendors from Zoho with bill counts."""
    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)
    try:
        all_vendors = []
        page = 1
        while True:
            resp = api.list_all_vendors(page=page)
            contacts = resp.get("contacts", [])
            if not contacts:
                break
            for c in contacts:
                all_vendors.append({
                    "contact_id": c.get("contact_id"),
                    "contact_name": c.get("contact_name", ""),
                    "company_name": c.get("company_name", ""),
                    "outstanding": c.get("outstanding_receivable_amount", 0) + c.get("outstanding_payable_amount", 0),
                    "status": c.get("status", ""),
                    "created_time": c.get("created_time", ""),
                })
            if not resp.get("page_context", {}).get("has_more_page"):
                break
            page += 1

        # Get bill counts per vendor
        bill_counts = {}
        bp = 1
        while True:
            br = api.list_bills(page=bp)
            bills = br.get("bills", [])
            if not bills:
                break
            for b in bills:
                vn = b.get("vendor_name", "")
                bill_counts[vn] = bill_counts.get(vn, 0) + 1
            if not br.get("page_context", {}).get("has_more_page"):
                break
            bp += 1

        for v in all_vendors:
            v["bill_count"] = bill_counts.get(v["contact_name"], 0)

        log_action(f"[Vendors] Listed {len(all_vendors)} vendors from Zoho")
        return jsonify({"vendors": all_vendors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vendors/delete", methods=["POST"])
def api_vendors_delete():
    """Delete one or more vendors from Zoho."""
    data = request.json
    contact_ids = data.get("contact_ids", [])
    if not contact_ids:
        return jsonify({"error": "No contact_ids provided"}), 400

    from utils import load_config, ZohoBooksAPI
    config = load_config()
    api = ZohoBooksAPI(config)

    succeeded = []
    failed = []
    for cid in contact_ids:
        try:
            api.delete_vendor(cid)
            succeeded.append(cid)
        except Exception as e:
            failed.append({"contact_id": cid, "error": str(e)})
            log_action(f"[Vendors] Failed to delete vendor {cid}: {e}", "ERROR")

    log_action(f"[Vendors] Deleted {len(succeeded)}/{len(contact_ids)} vendors from Zoho")
    return jsonify({"ok": True, "succeeded": succeeded, "failed": failed})


@app.route("/api/review/available-csvs")
def api_available_csvs():
    """List parsed CC transaction CSVs available for import.

    Pass ?include_txns=1 to include each card's parsed rows inline (used by the
    Import Picker's expandable preview).
    """
    output_dir = os.path.join(PROJECT_ROOT, "output")
    config_path = os.path.join(PROJECT_ROOT, "config", "zoho_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}

    include_txns = request.args.get("include_txns", "").lower() in ("1", "true", "yes")
    cards = config.get("credit_cards", [])
    available = []
    for card in cards:
        name = card["name"]
        safe_name = name.replace(" ", "_")
        csv_path = os.path.join(output_dir, f"{safe_name}_transactions.csv")
        if not os.path.exists(csv_path):
            continue

        row_count = 0
        transactions = []
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_count += 1
                    if include_txns:
                        try:
                            amt = float(row.get("amount") or 0)
                        except (TypeError, ValueError):
                            amt = 0
                        transactions.append({
                            "date": row.get("date", ""),
                            "description": row.get("description", ""),
                            "amount": amt,
                            "forex_ref": row.get("forex_ref", ""),
                        })
        except Exception:
            pass

        entry = {
            "card_name": name,
            "csv_file": f"{safe_name}_transactions.csv",
            "rows": row_count,
            "bank": card.get("bank", ""),
            "last_four_digits": card.get("last_four_digits", ""),
        }
        if include_txns:
            entry["transactions"] = transactions
        available.append(entry)
    return jsonify({"cards": available})


_PAYMENT_CACHE_PATH = os.path.join(PROJECT_ROOT, "output", "payment_preview_cache.json")


def _load_payment_cache():
    """Load cached payment preview data if available."""
    if os.path.exists(_PAYMENT_CACHE_PATH):
        try:
            with open(_PAYMENT_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_payment_cache(data):
    """Save payment preview data to cache."""
    try:
        os.makedirs(os.path.dirname(_PAYMENT_CACHE_PATH), exist_ok=True)
        with open(_PAYMENT_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        from scripts.utils import log_action
        log_action(f"Failed to save payment cache: {e}", "WARNING")


_PAID_BILLS_CACHE_PATH = os.path.join(PROJECT_ROOT, "output", "paid_bills_cache.json")


def _load_paid_bills_cache():
    """Load cached paid bills {bill_id: {vendor_name, amount, currency, date}}."""
    if os.path.exists(_PAID_BILLS_CACHE_PATH):
        try:
            with open(_PAID_BILLS_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_paid_bills_cache(data):
    try:
        os.makedirs(os.path.dirname(_PAID_BILLS_CACHE_PATH), exist_ok=True)
        with open(_PAID_BILLS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _add_to_paid_bills_cache(bill_id, vendor_name="", amount=0, currency="INR", date="", bill_number=""):
    """Add a single bill to paid bills cache (called after recording payment)."""
    cache = _load_paid_bills_cache()
    cache[bill_id] = {
        "vendor_name": vendor_name,
        "amount": amount,
        "currency": currency,
        "date": date,
        "bill_number": bill_number,
    }
    _save_paid_bills_cache(cache)


def _update_payment_cache_after_record(bill_ids, cc_transaction_id=None):
    """Update cache in-place after recording a payment (mark bills paid, remove used CC)."""
    cache = _load_payment_cache()
    if not cache:
        return
    if isinstance(bill_ids, str):
        bill_ids = [bill_ids]
    bill_set = set(bill_ids)

    # Mark matched bills as already_paid + add to paid bills cache
    for m in cache.get("matches", []):
        if m.get("bill_id") in bill_set:
            m["status"] = "already_paid"
            _add_to_paid_bills_cache(
                m["bill_id"],
                vendor_name=m.get("vendor_name", ""),
                amount=m.get("bill_amount", 0),
                currency=m.get("bill_currency", "INR"),
                date=m.get("bill_date", ""),
                bill_number=m.get("bill_number", ""),
            )

    # Also mark group matches as paid
    for gm in cache.get("group_matches", []):
        gm_bill_ids = set(b.get("bill_id", "") for b in gm.get("grouped_bills", []))
        if gm_bill_ids & bill_set:
            gm["status"] = "already_paid"

    # Remove used CC transaction from unmatched_cc
    if cc_transaction_id:
        cache["unmatched_cc"] = [
            cc for cc in cache.get("unmatched_cc", [])
            if cc.get("transaction_id") != cc_transaction_id
        ]

    # Update summary counts
    matches = cache.get("matches", [])
    matched_count = sum(1 for m in matches if m["status"] == "matched")
    unmatched_count = sum(1 for m in matches if m["status"] == "unmatched")
    already_paid_count = sum(1 for m in matches if m["status"] == "already_paid")
    cache["summary"] = {
        **cache.get("summary", {}),
        "matched": matched_count,
        "unmatched": unmatched_count,
        "unmatched_cc_count": len(cache.get("unmatched_cc", [])),
    }

    _save_payment_cache(cache)


@app.route("/api/payments/preview")
def api_payments_preview():
    """Preview bill-to-CC-transaction matches.

    Fetches live from Zoho: unpaid bills + CC transactions from 4 configured cards.
    Matching priority: amount (INR or USD→INR) > forex > closest date > fuzzy vendor.
    Uses cache unless ?refresh=1 is passed.
    """
    try:
        from scripts.utils import log_action

        # Check cache first (unless refresh requested)
        force_refresh = request.args.get("refresh") == "1"
        if not force_refresh:
            cached = _load_payment_cache()
            if cached:
                log_action("payments/preview: serving from cache")
                # Re-apply latest amex exclusions
                cached["amex_excluded"] = sorted(_load_amex_excluded())
                return jsonify(cached)

        mod_05 = _import_script("05_record_payments.py")
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids
        from datetime import datetime as _dt

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        log_action("payments/preview: fetching fresh from Zoho APIs...")

        # 0. Load paid bills cache to exclude already-paid bills
        paid_cache = _load_paid_bills_cache()
        paid_bill_ids = set(paid_cache.keys())
        log_action(f"payments/preview: {len(paid_bill_ids)} bills in paid cache (will exclude)")

        # 1. Fetch unpaid bills from Zoho (live)
        zoho_bills = mod_05.fetch_unpaid_bills_from_zoho(api)
        bills = []
        skipped_paid = 0
        for b in zoho_bills:
            bid = b.get("bill_id", "")
            if not bid:
                continue
            # Skip bills already in paid cache (banking-matched or previously paid)
            if bid in paid_bill_ids:
                skipped_paid += 1
                continue
            bills.append({
                "bill_id": bid,
                "vendor_id": b.get("vendor_id", ""),
                "vendor_name": b.get("vendor_name", ""),
                "amount": float(b.get("total", 0)),
                "currency": b.get("currency_code", "INR"),
                "file": b.get("bill_number", b.get("bill_id", "")),
                "date": b.get("date", ""),
            })
        if skipped_paid:
            log_action(f"payments/preview: skipped {skipped_paid} bills (already in paid cache)")


        # 2. Fetch CC transactions from Zoho Banking (live, all 4 cards)
        cc_transactions = mod_05.fetch_cc_transactions_from_zoho(api, cards)

        # Build CC list (debits only)
        cc_list = []
        for t in cc_transactions:
            if float(t.get("amount", 0)) <= 0:
                continue
            cc_entry = {
                "transaction_id": t.get("transaction_id", ""),
                "description": t.get("description", ""),
                "amount": float(t.get("amount", 0)),
                "date": t.get("date", ""),
                "card_name": t.get("card_name", ""),
                "zoho_account_id": t.get("zoho_account_id", ""),
            }
            if t.get("forex_amount"):
                cc_entry["forex_amount"] = t["forex_amount"]
                cc_entry["forex_currency"] = t["forex_currency"]
            cc_list.append(cc_entry)

        # 3. Build vendor_mappings resolver
        vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
        vendor_map = {}
        try:
            with open(vm_path, "r", encoding="utf-8") as f:
                vm = json.load(f)
            for k, v in vm.get("mappings", {}).items():
                vendor_map[k.lower()] = v
        except Exception:
            pass

        # --- Load learned vendor mappings ---
        from scripts.utils import load_learned_vendor_mappings
        learned = load_learned_vendor_mappings()
        learned_map = learned.get("mappings", {})

        # --- Prefetch forex rates for CC dates ---
        from scripts.utils import prefetch_forex_rates
        cc_dates = list(set(cc.get("date", "") for cc in cc_list if cc.get("date")))
        forex_cache = prefetch_forex_rates(cc_dates)

        # --- Vendor-gated matching (1:1 bill-to-CC) ---
        matches = _build_vendor_gated_matches(bills, cc_list, vendor_map, learned_map, forex_rates=forex_cache)
        matched_count = sum(1 for m in matches if m["status"] == "matched")
        used_cc = set()
        for m in matches:
            if m["status"] == "matched":
                for ci, cc in enumerate(cc_list):
                    if cc.get("transaction_id") == m.get("cc_transaction_id") and cc.get("date") == m.get("cc_date"):
                        used_cc.add(ci)
                        break
        matched_bill_ids = {m["bill_id"] for m in matches if m["status"] == "matched"}

        # --- Group matching (1 CC = N bills) — run EARLY so groups are found before diagnostics ---
        multi_vendors = []
        try:
            with open(vm_path, "r", encoding="utf-8") as f:
                vm_full = json.load(f)
            multi_vendors = vm_full.get("multi_bill_vendors", [])
        except Exception:
            pass

        group_matches = _build_group_matches(
            bills, cc_list, vendor_map, learned_map, multi_vendors,
            forex_rates=forex_cache,
            used_bill_ids=matched_bill_ids,
            used_cc_ids={m.get("cc_transaction_id") for m in matches if m["status"] == "matched"},
        )

        # Track group-claimed bills and CCs
        group_bill_ids = set()
        group_cc_ids = set()
        for gm in group_matches:
            for gb in gm.get("grouped_bills", []):
                group_bill_ids.add(gb["bill_id"])
            group_cc_ids.add(gm.get("cc_transaction_id", ""))

        # Update matched sets to include groups
        matched_bill_ids |= group_bill_ids
        for ci, cc in enumerate(cc_list):
            if cc.get("transaction_id", "") in group_cc_ids:
                used_cc.add(ci)

        # Collect unmatched CC transactions
        unmatched_cc = [cc_list[i] for i in range(len(cc_list)) if i not in used_cc]

        # Remove group-matched bills from the 1:1 matches list (they're in group_matches now)
        if group_bill_ids:
            matches = [m for m in matches if m["bill_id"] not in group_bill_ids]

        # --- Candidate matching for unmatched bills ---
        unmatched_bill_objs = [m for m in matches if m["status"] == "unmatched" and m["bill_id"] not in group_bill_ids]
        if unmatched_bill_objs and unmatched_cc:
            candidate_results = _find_candidates_for_unmatched(
                unmatched_bill_objs, unmatched_cc, forex_rates=forex_cache
            )
            # Replace unmatched entries with candidate-enriched versions
            candidate_by_bill = {r["bill_id"]: r for r in candidate_results}
            for i, m in enumerate(matches):
                if m["status"] == "unmatched" and m["bill_id"] in candidate_by_bill:
                    matches[i] = candidate_by_bill[m["bill_id"]]

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

        # Track which bills were matched for Amex pass
        bill_matched_flags = [False] * len(bills)
        for bi, bill in enumerate(bills):
            if bill["bill_id"] in matched_bill_ids:
                bill_matched_flags[bi] = True

        # Collect unique card names from config for filter dropdown
        card_names = [c.get("name", "") for c in cards if c.get("name")]

        # Per-card counts: total CC transactions and uncategorized (unmatched to bills)
        card_cc_total = {}
        for cc in cc_list:
            cn = cc.get("card_name", "")
            card_cc_total[cn] = card_cc_total.get(cn, 0) + 1
        card_cc_unmatched = {}
        for cc in unmatched_cc:
            cn = cc.get("card_name", "")
            card_cc_unmatched[cn] = card_cc_unmatched.get(cn, 0) + 1

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
                amex_results = _build_vendor_gated_matches(unmatched_bills, amex_list, vendor_map, learned_map, forex_rates=forex_cache)
                amex_matches = [m for m in amex_results if m["status"] == "matched"]
                log_action(f"Amex matching: {len(amex_matches)} bills matched to Amex CC")
            except Exception as e:
                log_action(f"Amex matching error: {e}", "WARNING")

        response_data = {
            "matches": matches,
            "unmatched_cc": unmatched_cc,
            "card_names": card_names,
            "card_cc_total": card_cc_total,
            "card_cc_unmatched": card_cc_unmatched,
            "amex_matches": amex_matches,
            "amex_excluded": sorted(_load_amex_excluded()),
            "group_matches": group_matches,
            "summary": {
                "total_bills": len(matches),
                "matched": sum(1 for m in matches if m["status"] == "matched"),
                "unmatched": sum(1 for m in matches if m["status"] == "unmatched"),
                "already_paid": sum(1 for m in matches if m["status"] == "already_paid"),
                "unmatched_cc_count": len(unmatched_cc),
                "amex_matched": len(amex_matches),
                "group_matched": len(group_matches),
            },
        }

        # Cache the result for future calls
        _save_payment_cache(response_data)
        _s = response_data["summary"]
        log_action(f"payments/preview: {_s['matched']} matched, {_s['group_matched']} grouped, {_s['unmatched']} no CC, {_s['amex_matched']} amex, {len(unmatched_cc)} unmatched CC")

        return jsonify(response_data)
    except Exception as e:
        from scripts.utils import log_action
        log_action(f"payments/preview error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


_AMEX_EXCLUDED_PATH = os.path.join(PROJECT_ROOT, "output", "amex_excluded_bills.json")


def _load_amex_excluded():
    """Load excluded bill IDs from JSON file."""
    if os.path.exists(_AMEX_EXCLUDED_PATH):
        try:
            with open(_AMEX_EXCLUDED_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _save_amex_excluded(excluded_set):
    """Save excluded bill IDs to JSON file."""
    os.makedirs(os.path.dirname(_AMEX_EXCLUDED_PATH), exist_ok=True)
    with open(_AMEX_EXCLUDED_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(excluded_set), f, indent=2)


@app.route("/api/amex-exclude", methods=["POST"])
def api_amex_exclude():
    """Toggle bill ID(s) in the Amex excluded list. Supports single or bulk."""
    data = request.json or {}
    bill_ids = data.get("bill_ids", [])
    bill_id = data.get("bill_id", "")
    if bill_id:
        bill_ids.append(bill_id)
    action = data.get("action", "exclude")  # "exclude" or "include"
    if not bill_ids:
        return jsonify({"error": "bill_id or bill_ids required"}), 400

    excluded = _load_amex_excluded()
    for bid in bill_ids:
        if action == "exclude":
            excluded.add(bid)
        else:
            excluded.discard(bid)
    _save_amex_excluded(excluded)
    return jsonify({"ok": True, "excluded": sorted(excluded)})


def _auto_match_banking_txn(api, cc_txn_id, payment_id, log_action,
                            account_id=None, cc_amount=None, cc_date=None):
    """After recording a vendor payment, auto-match the CC banking transaction.

    Fetches Zoho's suggested matches for the banking txn, finds the one
    matching our payment_id, and calls match_transaction to categorize it.
    If cc_txn_id is not provided, searches uncategorized transactions by amount+date.
    """
    # If no cc_txn_id, try to find it from uncategorized banking transactions
    if not cc_txn_id and account_id and cc_amount:
        try:
            from scripts.utils import log_action as _la
            result = api.list_uncategorized(account_id)
            txns = result.get("banktransactions", [])
            target_amount = round(float(cc_amount), 2)
            for t in txns:
                t_amount = abs(round(float(t.get("amount", 0)), 2))
                t_date = t.get("date", "")
                if t_amount == target_amount:
                    if not cc_date or t_date == cc_date:
                        cc_txn_id = t.get("transaction_id")
                        log_action(f"  Auto-match: found banking txn {cc_txn_id} by amount {target_amount}" +
                                   (f" on {cc_date}" if cc_date else ""))
                        break
        except Exception as e:
            log_action(f"  Auto-match: failed to search uncategorized: {e}", "WARNING")

    if not cc_txn_id:
        log_action("  Auto-match skipped: no cc_transaction_id found")
        return False
    try:
        import time
        # Small delay for Zoho to register the payment before matching
        time.sleep(0.5)

        match_result = api.get_matching_transactions(cc_txn_id)
        candidates = match_result.get("matching_transactions", [])

        if not candidates:
            log_action(f"  Auto-match: no candidates found for banking txn {cc_txn_id}")
            return False

        # Find the vendor_payment we just created
        target = None
        for c in candidates:
            if c.get("transaction_id") == payment_id:
                target = c
                break
            # Also match by transaction_type = vendor_payment (if payment_id matches reference)
            if c.get("transaction_type") == "vendor_payment" and c.get("payment_id") == payment_id:
                target = c
                break

        if not target:
            # Fallback: try the first vendor_payment candidate (likely the one we just created)
            for c in candidates:
                if c.get("transaction_type") == "vendor_payment":
                    target = c
                    log_action(f"  Auto-match: using first vendor_payment candidate")
                    break

        if target:
            match_data = [{
                "transaction_id": target.get("transaction_id"),
                "transaction_type": target.get("transaction_type", "vendor_payment"),
            }]
            api.match_transaction(cc_txn_id, match_data)
            log_action(f"  Auto-match: banking txn {cc_txn_id} -> categorized")
            return True
        else:
            log_action(f"  Auto-match: payment {payment_id} not found in {len(candidates)} candidates")
            return False

    except Exception as e:
        error_msg = str(e).lower()
        if "already" in error_msg:
            log_action(f"  Auto-match: already categorized")
            return True
        log_action(f"  Auto-match failed: {e}", "WARNING")
        return False


@app.route("/api/payments/record-one", methods=["POST"])
def api_payments_record_one():
    """Record payment for a single bill using CC transaction from preview."""
    data = request.json or {}
    bill_id = data.get("bill_id")
    if not bill_id:
        return jsonify({"error": "bill_id required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        currency_map = api.list_currencies()

        # Fetch the bill from Zoho
        bill_data = api.get_bill(bill_id)
        bill = bill_data.get("bill", {})
        bill_total = float(bill.get("total", 0))
        bill_currency = bill.get("currency_code", "INR")
        bill_status = bill.get("status", "")
        balance = float(bill.get("balance", bill_total))
        vendor_id = bill.get("vendor_id", "")

        # Check if already paid
        if bill_status == "paid" or balance <= 0:
            log_action(f"  Bill {bill_id} already paid, skipping")
            return jsonify({"status": "already_paid", "bill_id": bill_id})

        # Use CC match from preview (passed from frontend)
        cc_inr = data.get("cc_inr_amount")
        cc_date = data.get("cc_date")
        cc_card = data.get("cc_card")

        if not cc_inr or not cc_date:
            return jsonify({"status": "unmatched", "bill_id": bill_id, "message": "No CC match data provided"})

        # Resolve CC card -> zoho_account_id
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found in config"}), 400

        # Build payment (use balance, not total, in case of partial payments)
        payment_date = cc_date or bill.get("date", "")
        payment_data = {
            "vendor_id": vendor_id,
            "payment_mode": "Credit Card",
            "date": payment_date,
            "amount": balance,
            "paid_through_account_id": account_id,
            "bills": [{"bill_id": bill_id, "amount_applied": balance}],
        }

        # Handle foreign currency: calculate exchange rate from CC INR amount
        if bill_currency != "INR":
            actual_inr = float(cc_inr)
            if balance:
                exact_rate = actual_inr / balance
                for decimals in range(6, 12):
                    test_rate = round(exact_rate, decimals)
                    if round(test_rate * balance, 2) == round(actual_inr, 2):
                        exact_rate = test_rate
                        break
                else:
                    exact_rate = round(exact_rate, 10)
            else:
                exact_rate = 0
            payment_data["currency_id"] = currency_map.get(bill_currency)
            payment_data["exchange_rate"] = exact_rate
            log_action(f"  {bill_currency} {balance} -> INR {actual_inr} (rate: {exact_rate})")

        log_action(f"Recording payment: bill {bill_id} via {cc_card} on {payment_date} ({bill_currency} {balance})")

        result = api.record_vendor_payment(payment_data)
        payment = result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")

        if payment_id:
            log_action(f"  Payment recorded: {payment_id}")

            # Learn CC description → vendor mapping for future matching
            cc_desc = data.get("cc_description", "")
            if cc_desc and bill.get("vendor_name"):
                from scripts.utils import save_learned_vendor_mapping
                save_learned_vendor_mapping(cc_desc, bill["vendor_name"])

            # Update cache: mark bill paid, remove used CC
            _update_payment_cache_after_record(bill_id, data.get("cc_transaction_id"))

            return jsonify({"status": "paid", "payment_id": payment_id, "bill_id": bill_id})
        else:
            return jsonify({"status": "failed", "bill_id": bill_id, "message": "No payment_id returned"})

    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            log_action(f"  Bill {bill_id} already paid")
            _update_payment_cache_after_record(bill_id)
            return jsonify({"status": "already_paid", "bill_id": bill_id})
        from scripts.utils import log_action
        log_action(f"record-one error for {bill_id}: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/payments/record-group", methods=["POST"])
def api_payments_record_group():
    """Record payment for grouped bills (multiple bills → 1 CC transaction)."""
    data = request.json or {}
    bill_ids = data.get("bill_ids", [])
    cc_inr = data.get("cc_inr_amount")
    cc_date = data.get("cc_date")
    cc_card = data.get("cc_card")
    cc_desc = data.get("cc_description", "")

    if not bill_ids or len(bill_ids) < 2:
        return jsonify({"error": "At least 2 bill_ids required for group payment"}), 400
    if not cc_inr or not cc_date or not cc_card:
        return jsonify({"error": "cc_inr_amount, cc_date, cc_card required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # Resolve CC card -> zoho_account_id
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found in config"}), 400

        # Fetch each bill and build the bills array
        bills_list = []
        total_balance = 0
        vendor_id = None
        for bid in bill_ids:
            bill_data = api.get_bill(bid)
            bill = bill_data.get("bill", {})
            balance = float(bill.get("balance", bill.get("total", 0)))
            if not vendor_id:
                vendor_id = bill.get("vendor_id", "")
            if balance > 0:
                bills_list.append({"bill_id": bid, "amount_applied": balance})
                total_balance += balance

        if not bills_list:
            return jsonify({"error": "All bills already paid"}), 400

        payment_data = {
            "vendor_id": vendor_id,
            "payment_mode": "Credit Card",
            "date": cc_date,
            "amount": total_balance,
            "paid_through_account_id": account_id,
            "bills": bills_list,
        }

        log_action(f"Recording GROUP payment: {len(bills_list)} bills via {cc_card} on {cc_date} (INR {total_balance})")

        result = api.record_vendor_payment(payment_data)
        payment = result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")

        if payment_id:
            log_action(f"  Group payment recorded: {payment_id}")
            # Learn vendor mapping
            if cc_desc and vendor_id:
                bill_data = api.get_bill(bill_ids[0])
                vname = bill_data.get("bill", {}).get("vendor_name", "")
                if vname:
                    from scripts.utils import save_learned_vendor_mapping
                    save_learned_vendor_mapping(cc_desc, vname)
            # Update cache: mark all group bills paid, remove used CC
            _update_payment_cache_after_record(bill_ids, data.get("cc_transaction_id"))
            return jsonify({"status": "paid", "payment_id": payment_id, "bill_ids": bill_ids})
        else:
            return jsonify({"status": "failed", "message": "No payment_id returned"})

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"record-group error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/payments/record-selected", methods=["POST"])
def api_payments_record_selected():
    """Record payments for multiple bills using CC match data from preview."""
    data = request.json or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action
        import time

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)
        currency_map = api.list_currencies()

        # Build card name -> account_id map
        card_map = {c.get("name"): c.get("zoho_account_id") for c in cards}

        results = []
        for item in items:
            bill_id = item.get("bill_id")
            if not bill_id:
                continue
            try:
                bill_data = api.get_bill(bill_id)
                bill = bill_data.get("bill", {})
                bill_total = float(bill.get("total", 0))
                bill_currency = bill.get("currency_code", "INR")
                bill_status = bill.get("status", "")
                balance = float(bill.get("balance", bill_total))
                vendor_id = bill.get("vendor_id", "")

                # Skip already paid bills
                if bill_status == "paid" or balance <= 0:
                    log_action(f"  Bill {bill_id} already paid, skipping")
                    results.append({"bill_id": bill_id, "status": "already_paid"})
                    continue

                cc_card = item.get("cc_card", "")
                cc_inr = item.get("cc_inr_amount")
                cc_date = item.get("cc_date")
                account_id = card_map.get(cc_card)

                if not cc_inr or not cc_date or not account_id:
                    results.append({"bill_id": bill_id, "status": "unmatched"})
                    continue

                payment_date = cc_date
                payment_data = {
                    "vendor_id": vendor_id,
                    "payment_mode": "Credit Card",
                    "date": payment_date,
                    "amount": balance,
                    "paid_through_account_id": account_id,
                    "bills": [{"bill_id": bill_id, "amount_applied": balance}],
                }

                if bill_currency != "INR":
                    actual_inr = float(cc_inr)
                    if bill_total:
                        exact_rate = actual_inr / bill_total
                        for decimals in range(6, 12):
                            test_rate = round(exact_rate, decimals)
                            if round(test_rate * bill_total, 2) == round(actual_inr, 2):
                                exact_rate = test_rate
                                break
                        else:
                            exact_rate = round(exact_rate, 10)
                    else:
                        exact_rate = 0
                    payment_data["currency_id"] = currency_map.get(bill_currency)
                    payment_data["exchange_rate"] = exact_rate

                log_action(f"Recording payment: bill {bill_id} via {cc_card} ({bill_currency} {bill_total})")
                result = api.record_vendor_payment(payment_data)
                payment_id = result.get("vendorpayment", {}).get("payment_id")

                if payment_id:
                    log_action(f"  Payment recorded: {payment_id}")

                    results.append({"bill_id": bill_id, "status": "paid", "payment_id": payment_id})
                else:
                    results.append({"bill_id": bill_id, "status": "failed"})

            except Exception as e:
                error_msg = str(e).lower()
                if "already been paid" in error_msg or "already paid" in error_msg:
                    results.append({"bill_id": bill_id, "status": "already_paid"})
                else:
                    log_action(f"record-selected error for {bill_id}: {e}", "ERROR")
                    results.append({"bill_id": bill_id, "status": "error", "message": str(e)})

            time.sleep(0.3)

        # Bulk update cache for all paid/already_paid bills
        paid_bill_ids = set(r["bill_id"] for r in results if r["status"] in ("paid", "already_paid"))
        if paid_bill_ids:
            # Collect used CC transaction IDs from items
            cc_txn_ids = set()
            for item in items:
                if item.get("bill_id") in paid_bill_ids and item.get("cc_transaction_id"):
                    cc_txn_ids.add(item["cc_transaction_id"])
            # Update cache: mark all paid bills, remove used CCs
            cache = _load_payment_cache()
            if cache:
                for m in cache.get("matches", []):
                    if m.get("bill_id") in paid_bill_ids:
                        m["status"] = "already_paid"
                if cc_txn_ids:
                    cache["unmatched_cc"] = [
                        cc for cc in cache.get("unmatched_cc", [])
                        if cc.get("transaction_id") not in cc_txn_ids
                    ]
                _save_payment_cache(cache)

        paid = sum(1 for r in results if r["status"] == "paid")
        return jsonify({"results": results, "paid_count": paid, "total": len(results)})
    except Exception as e:
        from scripts.utils import log_action
        log_action(f"record-selected error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _update_payment_tracking(bill_entry, payment_id, cc_match):
    """Append to recorded_payments.json tracking file."""
    payments_file = os.path.join(PROJECT_ROOT, "output", "recorded_payments.json")
    payments = []
    if os.path.exists(payments_file):
        with open(payments_file, "r", encoding="utf-8") as f:
            payments = json.load(f)

    entry = {
        "file": bill_entry.get("file", ""),
        "bill_id": bill_entry["bill_id"],
        "vendor_name": bill_entry.get("vendor_name"),
        "amount": bill_entry.get("amount"),
        "currency": bill_entry.get("currency"),
        "payment_id": payment_id,
        "status": "paid",
    }
    if cc_match:
        entry["cc_inr_amount"] = cc_match.get("inr_amount")
        entry["cc_card"] = cc_match.get("card_name")

    # Remove any existing entry for this bill_id
    payments = [p for p in payments if p.get("bill_id") != bill_entry["bill_id"]]
    payments.append(entry)

    os.makedirs(os.path.dirname(payments_file), exist_ok=True)
    with open(payments_file, "w", encoding="utf-8") as f:
        json.dump(payments, f, indent=2, ensure_ascii=False)


@app.route("/api/bills/create-one", methods=["POST"])
def api_bills_create_one():
    """Create a single bill in Zoho from invoice data (used by Monthly Compare exact match rows)."""
    data = request.json or {}
    vendor_name = data.get("vendor_name", "")
    amount = data.get("amount")
    currency = data.get("currency", "INR")
    date = data.get("date")
    invoice_number = data.get("invoice_number", "")
    vendor_gstin = data.get("vendor_gstin", "")

    if not vendor_name or not amount or not date:
        return jsonify({"error": "vendor_name, amount, and date are required"}), 400

    try:
        mod_03 = _import_script("03_create_vendors_bills.py")
        from scripts.utils import load_config, load_vendor_mappings, ZohoBooksAPI, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        vendor_mappings = load_vendor_mappings()
        currency_map = api.list_currencies()

        # Resolve vendor (find or create)
        invoice = {
            "file": invoice_number or vendor_name,
            "vendor_name": vendor_name,
            "vendor_gstin": vendor_gstin,
            "amount": float(amount),
            "currency": currency,
            "date": date,
            "invoice_number": invoice_number,
        }

        vendor_id, resolved_name = mod_03.ensure_vendor(
            api, vendor_name, invoice, vendor_mappings, currency_map,
        )
        if not vendor_id:
            return jsonify({"error": f"Could not find or create vendor '{vendor_name}'"}), 500

        # Get expense accounts
        expense_accounts = api.get_expense_accounts()

        # Get tax IDs
        igst_tax_id = None
        intrastate_tax_id = None
        default_exemption_id = None
        try:
            taxes = api.list_taxes()
            for t in taxes:
                tname = t.get("tax_name", "").lower()
                if "igst" in tname and t.get("tax_percentage") == 18:
                    igst_tax_id = t.get("tax_id")
                if ("cgst" in tname or "sgst" in tname) and t.get("tax_percentage") == 9:
                    intrastate_tax_id = intrastate_tax_id or t.get("tax_id")
            exemptions = api.list_tax_exemptions()
            for ex in exemptions:
                if "non" in ex.get("exemption_reason", "").lower():
                    default_exemption_id = ex.get("tax_exemption_id")
                    break
        except Exception:
            pass

        default_expense = config.get("default_expense_account", "Credit Card Charges")
        result = mod_03.create_bill_for_invoice(
            api, invoice, vendor_id, expense_accounts, default_expense, currency_map,
            vendor_name=resolved_name,
            igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
            default_exemption_id=default_exemption_id,
        )

        if result and result[0]:
            bill_id = result[0]
            is_new = result[1]
            status = "created" if is_new else "exists"
            log_action(f"Bill {status}: {invoice_number or vendor_name} -> {bill_id}")

            # Append to created_bills.json so Review Accounts panel can see it
            bills_file = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
            try:
                existing = []
                if os.path.exists(bills_file):
                    with open(bills_file, "r") as f:
                        existing = json.load(f)
                existing.append({
                    "file": invoice_number or vendor_name,
                    "status": status,
                    "vendor_name": resolved_name or vendor_name,
                    "vendor_id": vendor_id,
                    "bill_id": bill_id,
                    "amount": float(amount),
                    "currency": currency,
                    "attached": False,
                })
                with open(bills_file, "w") as f:
                    json.dump(existing, f, indent=2)
            except Exception as ex:
                log_action(f"Warning: could not update created_bills.json: {ex}", "WARN")

            return jsonify({"status": status, "bill_id": bill_id, "bill_number": invoice_number})
        else:
            return jsonify({"error": "Failed to create bill"}), 500

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"create-one bill error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bills/create-and-record", methods=["POST"])
def api_bills_create_and_record():
    """Create bill + record payment + auto-match banking transaction in one step."""
    data = request.json or {}
    inv = data.get("invoice", {})
    cc = data.get("cc", {})

    vendor_name = inv.get("vendor_name", "")
    amount = inv.get("amount")
    currency = inv.get("currency", "INR")
    date = inv.get("date")
    invoice_number = inv.get("invoice_number", "")
    vendor_gstin = inv.get("vendor_gstin", "")

    cc_inr = cc.get("amount")
    cc_date = cc.get("date")
    cc_card = cc.get("card_name")
    cc_txn_id = cc.get("transaction_id", "")

    # For foreign currency invoices, prefer the CC-mapped vendor name if available
    # (e.g., CC maps "CLAUDE.AI SUBSCRIPTION" -> "Anthropic USD" while invoice says "Anthropic")
    cc_vendor = cc.get("vendor_name", "")
    if currency != "INR" and cc_vendor and cc_vendor != vendor_name:
        vendor_name = cc_vendor

    if not vendor_name or not amount or not date:
        return jsonify({"error": "invoice vendor_name, amount, date required"}), 400
    if not cc_inr or not cc_date or not cc_card:
        return jsonify({"error": "cc amount, date, card_name required"}), 400

    try:
        mod_03 = _import_script("03_create_vendors_bills.py")
        from scripts.utils import load_config, load_vendor_mappings, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        vendor_mappings = load_vendor_mappings()
        currency_map = api.list_currencies()
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # --- Step 1: Create bill ---
        invoice = {
            "file": invoice_number or vendor_name,
            "vendor_name": vendor_name,
            "vendor_gstin": vendor_gstin,
            "amount": float(amount),
            "currency": currency,
            "date": date,
            "invoice_number": invoice_number,
        }

        vendor_id, resolved_name = mod_03.ensure_vendor(
            api, vendor_name, invoice, vendor_mappings, currency_map,
        )
        if not vendor_id:
            return jsonify({"error": f"Could not find or create vendor '{vendor_name}'"}), 500

        expense_accounts = api.get_expense_accounts()
        igst_tax_id = None
        intrastate_tax_id = None
        default_exemption_id = None
        try:
            taxes = api.list_taxes()
            for t in taxes:
                tname = t.get("tax_name", "").lower()
                if "igst" in tname and t.get("tax_percentage") == 18:
                    igst_tax_id = t.get("tax_id")
                if ("cgst" in tname or "sgst" in tname) and t.get("tax_percentage") == 9:
                    intrastate_tax_id = intrastate_tax_id or t.get("tax_id")
            exemptions = api.list_tax_exemptions()
            for ex in exemptions:
                if "non" in ex.get("exemption_reason", "").lower():
                    default_exemption_id = ex.get("tax_exemption_id")
                    break
        except Exception:
            pass

        default_expense = config.get("default_expense_account", "Credit Card Charges")
        result = mod_03.create_bill_for_invoice(
            api, invoice, vendor_id, expense_accounts, default_expense, currency_map,
            vendor_name=resolved_name,
            igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
            default_exemption_id=default_exemption_id,
        )

        if not result or not result[0]:
            return jsonify({"error": "Failed to create bill"}), 500

        bill_id = result[0]
        is_new = result[1]
        bill_status = "created" if is_new else "exists"
        log_action(f"Bill {bill_status}: {invoice_number or vendor_name} -> {bill_id}")

        # Attach PDF
        pdf_path = inv.get("organized_path") or ""
        fname = inv.get("file", invoice_number or vendor_name)
        if not pdf_path or not os.path.exists(pdf_path):
            # Check the direct path field from invoice entry
            direct_path = inv.get("path", "")
            if direct_path and os.path.exists(direct_path):
                pdf_path = direct_path
            else:
                # Try to find in organized_invoices, input_pdfs, or new image invoices
                for base_dir in ("organized_invoices", "input_pdfs", "new image invoices"):
                    candidate = os.path.join(PROJECT_ROOT, base_dir)
                    if os.path.isdir(candidate):
                        for root_d, _dirs, files in os.walk(candidate):
                            for f_name in files:
                                if f_name == fname or os.path.splitext(f_name)[0] == os.path.splitext(fname)[0]:
                                    pdf_path = os.path.join(root_d, f_name)
                                    break
                            if pdf_path and os.path.exists(pdf_path):
                                break
        attached = False
        if pdf_path and os.path.exists(pdf_path):
            try:
                mod_03.attach_pdf(api, bill_id, pdf_path)
                attached = True
                log_action(f"  Attached PDF: {os.path.basename(pdf_path)}")
            except Exception as e:
                log_action(f"  Failed to attach PDF: {e}", "WARNING")

        # Save to created_bills.json
        bills_file = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
        try:
            existing = []
            if os.path.exists(bills_file):
                with open(bills_file, "r") as f:
                    existing = json.load(f)
            existing.append({
                "file": invoice_number or vendor_name,
                "status": bill_status,
                "vendor_name": resolved_name or vendor_name,
                "vendor_id": vendor_id,
                "bill_id": bill_id,
                "amount": float(amount),
                "currency": currency,
                "attached": attached,
            })
            with open(bills_file, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass

        # --- Step 2: Record payment ---
        bill_data = api.get_bill(bill_id)
        bill = bill_data.get("bill", {})
        bill_total = float(bill.get("total", 0))
        bill_currency = bill.get("currency_code", "INR")
        balance = float(bill.get("balance", bill_total))

        if bill.get("status") == "paid" or balance <= 0:
            log_action(f"  Bill {bill_id} already paid, skipping payment")
            return jsonify({"status": "already_paid", "bill_id": bill_id})

        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"status": "bill_created", "bill_id": bill_id, "error": f"CC card '{cc_card}' not found"})

        payment_date = cc_date or bill.get("date", "")
        payment_data = {
            "vendor_id": vendor_id,
            "payment_mode": "Credit Card",
            "date": payment_date,
            "amount": balance,
            "paid_through_account_id": account_id,
            "bills": [{"bill_id": bill_id, "amount_applied": balance}],
        }

        if bill_currency != "INR":
            actual_inr = float(cc_inr)
            if bill_total:
                exact_rate = actual_inr / bill_total
                for decimals in range(6, 12):
                    test_rate = round(exact_rate, decimals)
                    if round(test_rate * bill_total, 2) == round(actual_inr, 2):
                        exact_rate = test_rate
                        break
                else:
                    exact_rate = round(exact_rate, 10)
            else:
                exact_rate = 0
            payment_data["currency_id"] = currency_map.get(bill_currency)
            payment_data["exchange_rate"] = exact_rate
            log_action(f"  {bill_currency} {bill_total} -> INR {actual_inr} (rate: {exact_rate})")

        log_action(f"Recording payment: bill {bill_id} via {cc_card} on {payment_date}")
        pay_result = api.record_vendor_payment(payment_data)
        payment = pay_result.get("vendorpayment", {})
        payment_id = payment.get("payment_id")

        if not payment_id:
            return jsonify({"status": "bill_created", "bill_id": bill_id, "error": "Payment failed - no payment_id"})

        log_action(f"  Payment recorded: {payment_id}")

        # Learn CC description → vendor mapping for future matching
        cc_desc = cc.get("description", "")
        if cc_desc and vendor_name:
            from scripts.utils import save_learned_vendor_mapping
            save_learned_vendor_mapping(cc_desc, vendor_name)

        return jsonify({
            "status": "paid",
            "bill_id": bill_id,
            "payment_id": payment_id,
        })

    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            return jsonify({"status": "already_paid", "bill_id": data.get("bill_id", "")})
        from scripts.utils import log_action
        log_action(f"create-and-record error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bills/create-and-record-bulk", methods=["POST"])
def api_bills_create_and_record_bulk():
    """Create bills 1-by-1, record payment for each, then auto-match all payments with CC banking txn.
    Used for grouped invoices (multiple invoices matched to 1 CC charge).
    """
    data = request.json or {}
    invoices = data.get("invoices", [])
    cc = data.get("cc", {})

    cc_inr = cc.get("amount")
    cc_date = cc.get("date")
    cc_card = cc.get("card_name")
    cc_txn_id = cc.get("transaction_id", "")
    cc_vendor = cc.get("vendor_name", "")

    if not invoices:
        return jsonify({"error": "invoices array required"}), 400
    if not cc_inr or not cc_date or not cc_card:
        return jsonify({"error": "cc amount, date, card_name required"}), 400

    try:
        mod_03 = _import_script("03_create_vendors_bills.py")
        from scripts.utils import load_config, load_vendor_mappings, ZohoBooksAPI, resolve_account_ids, log_action
        import time

        config = load_config()
        api = ZohoBooksAPI(config)
        vendor_mappings = load_vendor_mappings()
        currency_map = api.list_currencies()
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # Find CC card account
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found"}), 400

        # Load tax info once
        expense_accounts = api.get_expense_accounts()
        igst_tax_id = None
        intrastate_tax_id = None
        default_exemption_id = None
        try:
            taxes = api.list_taxes()
            for t in taxes:
                tname = t.get("tax_name", "").lower()
                if "igst" in tname and t.get("tax_percentage") == 18:
                    igst_tax_id = t.get("tax_id")
                if ("cgst" in tname or "sgst" in tname) and t.get("tax_percentage") == 9:
                    intrastate_tax_id = intrastate_tax_id or t.get("tax_id")
            exemptions = api.list_tax_exemptions()
            for ex in exemptions:
                if "non" in ex.get("exemption_reason", "").lower():
                    default_exemption_id = ex.get("tax_exemption_id")
                    break
        except Exception:
            pass

        default_expense = config.get("default_expense_account", "Credit Card Charges")
        results = []
        payment_ids = []

        log_action(f"[Bulk] Creating {len(invoices)} bills for CC charge INR {cc_inr} on {cc_date}")

        for idx, inv in enumerate(invoices):
            vendor_name = inv.get("vendor_name", "")
            amount = inv.get("amount")
            currency = inv.get("currency", "INR")
            inv_date = inv.get("date")
            invoice_number = inv.get("invoice_number", "")
            vendor_gstin = inv.get("vendor_gstin", "")

            # For foreign currency, prefer CC-mapped vendor name
            if currency != "INR" and cc_vendor and cc_vendor != vendor_name:
                vendor_name = cc_vendor

            # --- Step 1: Create bill (or use existing) ---
            existing_bill_id = inv.get("zoho_bill_id", "")
            if existing_bill_id and existing_bill_id != "deleted":
                log_action(f"  [{idx+1}/{len(invoices)}] Bill already in Zoho: {invoice_number or vendor_name} -> {existing_bill_id}")
            else:
                log_action(f"  [{idx+1}/{len(invoices)}] Creating bill: {invoice_number or vendor_name} ({currency} {amount})")

            invoice_obj = {
                "file": invoice_number or vendor_name,
                "vendor_name": vendor_name,
                "vendor_gstin": vendor_gstin,
                "amount": float(amount),
                "currency": currency,
                "date": inv_date,
                "invoice_number": invoice_number,
            }

            try:
                vendor_id, resolved_name = mod_03.ensure_vendor(
                    api, vendor_name, invoice_obj, vendor_mappings, currency_map,
                )
                if not vendor_id:
                    log_action(f"  [{idx+1}] Failed: could not find/create vendor '{vendor_name}'", "ERROR")
                    results.append({"invoice_number": invoice_number, "status": "error", "error": f"Vendor not found: {vendor_name}"})
                    continue

                # Skip bill creation if already in Zoho
                bill_id = None
                is_new = False
                if existing_bill_id and existing_bill_id != "deleted":
                    bill_id = existing_bill_id
                else:
                    result = mod_03.create_bill_for_invoice(
                        api, invoice_obj, vendor_id, expense_accounts, default_expense, currency_map,
                        vendor_name=resolved_name,
                        igst_tax_id=igst_tax_id, intrastate_tax_id=intrastate_tax_id,
                        default_exemption_id=default_exemption_id,
                    )

                    if not result or not result[0]:
                        log_action(f"  [{idx+1}] Failed: could not create bill", "ERROR")
                        results.append({"invoice_number": invoice_number, "status": "error", "error": "Failed to create bill"})
                        continue

                    bill_id = result[0]
                    is_new = result[1]

                log_action(f"  [{idx+1}] Bill {'created' if is_new else 'exists'}: {bill_id}")

                # --- Attach PDF ---
                pdf_path = inv.get("organized_path") or ""
                fname = inv.get("file", invoice_number or vendor_name)
                if not pdf_path or not os.path.exists(pdf_path):
                    # Check the direct path field from invoice entry
                    direct_path = inv.get("path", "")
                    if direct_path and os.path.exists(direct_path):
                        pdf_path = direct_path
                    else:
                        for base_dir in ("organized_invoices", "input_pdfs", "new image invoices"):
                            candidate = os.path.join(PROJECT_ROOT, base_dir)
                            if os.path.isdir(candidate):
                                for root_d, _dirs, files in os.walk(candidate):
                                    for f_name in files:
                                        if f_name == fname or os.path.splitext(f_name)[0] == os.path.splitext(fname)[0]:
                                            pdf_path = os.path.join(root_d, f_name)
                                            break
                                    if pdf_path and os.path.exists(pdf_path):
                                        break
                if pdf_path and os.path.exists(pdf_path):
                    try:
                        mod_03.attach_pdf(api, bill_id, pdf_path)
                        log_action(f"  [{idx+1}] Attached PDF: {os.path.basename(pdf_path)}")
                    except Exception as e:
                        log_action(f"  [{idx+1}] Failed to attach PDF: {e}", "WARNING")

                time.sleep(0.5)  # Allow Zoho to finalize bill before recording payment

                # --- Step 2: Record payment for this bill ---
                bill_data = api.get_bill(bill_id)
                bill = bill_data.get("bill", {})
                bill_status = bill.get("status", "")
                bill_total = float(bill.get("total", 0))
                bill_currency = bill.get("currency_code", "INR")
                balance = float(bill.get("balance", bill_total))

                if bill_status == "paid" or balance <= 0:
                    log_action(f"  [{idx+1}] Already paid, skipping payment")
                    results.append({"invoice_number": invoice_number, "status": "already_paid", "bill_id": bill_id})
                    continue

                payment_date = cc_date
                payment_data = {
                    "vendor_id": vendor_id,
                    "payment_mode": "Credit Card",
                    "date": payment_date,
                    "amount": balance,
                    "paid_through_account_id": account_id,
                    "bills": [{"bill_id": bill_id, "amount_applied": balance}],
                }

                if bill_currency != "INR":
                    actual_inr_portion = float(cc_inr) * (balance / float(inv.get("amount", balance)))
                    if balance:
                        exact_rate = actual_inr_portion / balance
                        for decimals in range(6, 12):
                            test_rate = round(exact_rate, decimals)
                            if round(test_rate * balance, 2) == round(actual_inr_portion, 2):
                                exact_rate = test_rate
                                break
                        else:
                            exact_rate = round(exact_rate, 10)
                    else:
                        exact_rate = 0
                    payment_data["currency_id"] = currency_map.get(bill_currency)
                    payment_data["exchange_rate"] = exact_rate

                pay_result = api.record_vendor_payment(payment_data)
                payment = pay_result.get("vendorpayment", {})
                payment_id = payment.get("payment_id")

                if payment_id:
                    log_action(f"  [{idx+1}] Payment recorded: {payment_id}")
                    payment_ids.append(payment_id)
                    results.append({"invoice_number": invoice_number, "status": "paid", "bill_id": bill_id, "payment_id": payment_id})
                else:
                    log_action(f"  [{idx+1}] Payment failed", "ERROR")
                    results.append({"invoice_number": invoice_number, "status": "bill_created", "bill_id": bill_id, "error": "Payment failed"})

            except Exception as ex:
                error_msg = str(ex).lower()
                if "already" in error_msg and "paid" in error_msg:
                    results.append({"invoice_number": invoice_number, "status": "already_paid"})
                    log_action(f"  [{idx+1}] Already paid")
                    continue
                log_action(f"  [{idx+1}] Error: {ex}", "ERROR")
                results.append({"invoice_number": invoice_number, "status": "error", "error": str(ex)})

        created_count = sum(1 for r in results if r.get("status") in ("paid", "bill_created"))
        paid_count = sum(1 for r in results if r.get("status") == "paid")
        already_paid_count = sum(1 for r in results if r.get("status") == "already_paid")
        bill_created_only = sum(1 for r in results if r.get("status") == "bill_created")
        error_count = sum(1 for r in results if r.get("status") == "error")
        log_action(f"[Bulk] Done: {created_count} created, {paid_count} recorded, {already_paid_count} skipped (already paid), {error_count} errors")

        # --- Auto-match CC banking transaction with all recorded payments ---
        banking_matched = False
        if payment_ids and cc_txn_id:
            try:
                banking_matched = _auto_match_banking_txn_multi(
                    api, cc_txn_id, payment_ids, log_action,
                    account_id=account_id, cc_amount=cc_inr, cc_date=cc_date,
                )
                if banking_matched:
                    log_action(f"[Bulk] CC banking transaction matched: {len(payment_ids)} payments -> {cc_txn_id}")
                else:
                    log_action(f"[Bulk] CC banking auto-match failed", "WARNING")
            except Exception as e:
                log_action(f"[Bulk] CC banking auto-match error: {e}", "WARNING")

        overall_status = "paid" if paid_count > 0 and error_count == 0 else ("partial" if paid_count > 0 else "error")
        return jsonify({
            "status": overall_status,
            "results": results,
            "total": len(invoices),
            "created_count": created_count,
            "paid_count": paid_count,
            "already_paid_count": already_paid_count,
            "bill_created_only": bill_created_only,
            "error_count": error_count,
            "banking_matched": banking_matched,
        })

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"create-and-record-bulk error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _auto_match_banking_txn_multi(api, cc_txn_id, payment_ids, log_action,
                                   account_id=None, cc_amount=None, cc_date=None):
    """Auto-match a CC banking transaction against multiple vendor payments."""
    import time

    # If no cc_txn_id, try to find it from uncategorized banking transactions
    if not cc_txn_id and account_id and cc_amount:
        try:
            result = api.list_uncategorized(account_id)
            txns = result.get("banktransactions", [])
            target_amount = round(float(cc_amount), 2)
            for t in txns:
                t_amount = abs(round(float(t.get("amount", 0)), 2))
                t_date = t.get("date", "")
                if t_amount == target_amount:
                    if not cc_date or t_date == cc_date:
                        cc_txn_id = t.get("transaction_id")
                        log_action(f"  Auto-match: found banking txn {cc_txn_id} by amount {target_amount}")
                        break
        except Exception as e:
            log_action(f"  Auto-match: failed to search uncategorized: {e}", "WARNING")

    if not cc_txn_id:
        log_action("  Auto-match skipped: no cc_transaction_id found")
        return False

    try:
        time.sleep(0.5)
        match_result = api.get_matching_transactions(cc_txn_id)
        candidates = match_result.get("matching_transactions", [])

        if not candidates:
            log_action(f"  Auto-match: no candidates found for banking txn {cc_txn_id}")
            return False

        # Find ALL vendor payments that match our payment_ids
        match_data = []
        for c in candidates:
            cid = c.get("transaction_id") or c.get("payment_id", "")
            if cid in payment_ids or c.get("payment_id", "") in payment_ids:
                match_data.append({
                    "transaction_id": c.get("transaction_id"),
                    "transaction_type": c.get("transaction_type", "vendor_payment"),
                })

        # Fallback: if we couldn't match by ID, grab all vendor_payment candidates
        if not match_data:
            for c in candidates:
                if c.get("transaction_type") == "vendor_payment":
                    match_data.append({
                        "transaction_id": c.get("transaction_id"),
                        "transaction_type": "vendor_payment",
                    })
            if match_data:
                log_action(f"  Auto-match: using {len(match_data)} vendor_payment candidates (fallback)")

        if match_data:
            api.match_transaction(cc_txn_id, match_data)
            log_action(f"  Auto-match: banking txn {cc_txn_id} -> categorized with {len(match_data)} payments")
            return True
        else:
            log_action(f"  Auto-match: payments not found in {len(candidates)} candidates")
            return False

    except Exception as e:
        error_msg = str(e).lower()
        if "already" in error_msg:
            log_action(f"  Auto-match: already categorized")
            return True
        log_action(f"  Auto-match failed: {e}", "WARNING")
        return False


@app.route("/api/bills/record-only", methods=["POST"])
def api_bills_record_only():
    """Record payment + auto-match banking for existing bill(s) (skip bill creation).
    Supports multiple bill_ids for grouped invoices (e.g., 2 bills matched to 1 CC charge).
    """
    data = request.json or {}
    bill_id = data.get("bill_id", "")
    bill_ids = data.get("bill_ids", [])
    cc = data.get("cc", {})
    cc_inr = cc.get("amount")
    cc_date = cc.get("date")
    cc_card = cc.get("card_name")
    cc_txn_id = cc.get("transaction_id", "")

    # Build list of bill IDs to process
    all_bill_ids = list(bill_ids) if bill_ids else []
    if bill_id and bill_id not in all_bill_ids:
        all_bill_ids.insert(0, bill_id)
    all_bill_ids = [b for b in all_bill_ids if b]

    if not all_bill_ids:
        return jsonify({"error": "bill_id or bill_ids required", "received": {"bill_id": bill_id, "bill_ids": bill_ids}}), 400
    missing = []
    if not cc_inr and cc_inr != 0:
        missing.append("amount")
    if not cc_date:
        missing.append("date")
    if not cc_card:
        missing.append("card_name")
    if missing:
        return jsonify({"error": f"cc {', '.join(missing)} required", "received_cc": cc}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action

        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)
        currency_map = api.list_currencies()

        # Find CC card account
        account_id = None
        for card in cards:
            if card.get("name") == cc_card:
                account_id = card.get("zoho_account_id")
                break
        if not account_id:
            return jsonify({"error": f"CC card '{cc_card}' not found"}), 400

        # Fetch all bill details and build payment
        bills_to_pay = []
        total_amount = 0
        bill_currency = "INR"
        vendor_id = None
        skipped = []

        for bid in all_bill_ids:
            try:
                bill_data = api.get_bill(bid)
                bill = bill_data.get("bill", {})
                status = bill.get("status", "")
                if status == "paid":
                    log_action(f"  Bill {bid} already paid, skipping")
                    skipped.append(bid)
                    continue
                bt = float(bill.get("total", 0))
                # Use balance_due if available (handles partial payments)
                balance = float(bill.get("balance", bt))
                if balance <= 0:
                    log_action(f"  Bill {bid} has zero balance, skipping")
                    skipped.append(bid)
                    continue
                bill_vendor_id = bill.get("vendor_id", "")
                bill_currency = bill.get("currency_code", "INR")
                bills_to_pay.append({"bill_id": bid, "amount_applied": balance, "vendor_id": bill_vendor_id, "currency": bill_currency})
                total_amount += balance
                if not vendor_id:
                    vendor_id = bill_vendor_id
            except Exception as ex:
                error_msg = str(ex).lower()
                if "already" in error_msg and "paid" in error_msg:
                    skipped.append(bid)
                    continue
                raise

        if not bills_to_pay:
            if skipped:
                return jsonify({"status": "already_paid", "bill_id": all_bill_ids[0], "skipped": skipped})
            return jsonify({"error": "No bills to pay"}), 400

        if not bills_to_pay:
            return jsonify({"error": "No bills with valid vendor"}), 500

        # Group bills by vendor_id — Zoho requires one payment per vendor
        from collections import defaultdict
        vendor_groups = defaultdict(lambda: {"bills": [], "total": 0, "currency": "INR"})
        for bp in bills_to_pay:
            vid = bp.pop("vendor_id")
            cur = bp.pop("currency", "INR")
            vendor_groups[vid]["bills"].append(bp)
            vendor_groups[vid]["total"] += bp["amount_applied"]
            vendor_groups[vid]["currency"] = cur

        payment_date = cc_date
        all_payment_ids = []
        total_all = sum(g["total"] for g in vendor_groups.values())

        for vid, group in vendor_groups.items():
            group_total = round(group["total"], 2)
            group_currency = group["currency"]

            payment_data = {
                "vendor_id": vid,
                "payment_mode": "Credit Card",
                "date": payment_date,
                "amount": group_total,
                "paid_through_account_id": account_id,
                "bills": group["bills"],
            }

            if group_currency != "INR":
                # Proportion of CC INR amount for this vendor group
                proportion = group_total / total_all if total_all else 1
                actual_inr = round(float(cc_inr) * proportion, 2)
                if group_total:
                    exact_rate = actual_inr / group_total
                    for decimals in range(6, 12):
                        test_rate = round(exact_rate, decimals)
                        if round(test_rate * group_total, 2) == round(actual_inr, 2):
                            exact_rate = test_rate
                            break
                    else:
                        exact_rate = round(exact_rate, 10)
                else:
                    exact_rate = 0
                payment_data["currency_id"] = currency_map.get(group_currency)
                payment_data["exchange_rate"] = exact_rate
                log_action(f"  {group_currency} {group_total} -> INR {actual_inr} (rate: {exact_rate})")

            bill_count = len(group["bills"])
            log_action(f"Recording payment ({bill_count} bill{'s' if bill_count > 1 else ''}): {', '.join(b['bill_id'] for b in group['bills'])} via {cc_card} on {payment_date}")
            pay_result = api.record_vendor_payment(payment_data)
            payment = pay_result.get("vendorpayment", {})
            payment_id = payment.get("payment_id")

            if payment_id:
                log_action(f"  Payment recorded: {payment_id} (total: {group_total})")
                all_payment_ids.append(payment_id)
            else:
                log_action(f"  Payment failed for vendor {vid}", "ERROR")

        if not all_payment_ids:
            return jsonify({"error": "All payments failed"}), 500

        # Update cache: mark bills paid, remove used CC
        paid_bids = [b["bill_id"] for bp in vendor_groups.values() for b in bp["bills"]]
        _update_payment_cache_after_record(paid_bids, cc_txn_id)

        return jsonify({
            "status": "paid",
            "bill_id": all_bill_ids[0],
            "bill_ids": paid_bids,
            "skipped": skipped,
            "payment_id": all_payment_ids[0] if all_payment_ids else "",
            "payment_ids": all_payment_ids,
        })

    except Exception as e:
        error_msg = str(e).lower()
        if "already been paid" in error_msg or "already paid" in error_msg:
            _update_payment_cache_after_record(all_bill_ids)
            return jsonify({"status": "already_paid", "bill_id": all_bill_ids[0]})
        from scripts.utils import log_action
        log_action(f"record-only error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/payments/clear-cache", methods=["POST"])
def api_payments_clear_cache():
    """Clear recorded_payments.json so Step 6 re-tries all bills."""
    payments_file = os.path.join(PROJECT_ROOT, "output", "recorded_payments.json")
    if os.path.exists(payments_file):
        os.remove(payments_file)
        return jsonify({"status": "ok", "message": "Payments cache cleared — Step 6 will retry all bills"})
    return jsonify({"status": "ok", "message": "No payments cache to clear"})


@app.route("/api/payments/clear-preview-cache", methods=["POST"])
def api_payments_clear_preview_cache():
    """Clear payment preview cache to force fresh Zoho API fetch."""
    if os.path.exists(_PAYMENT_CACHE_PATH):
        os.remove(_PAYMENT_CACHE_PATH)
        return jsonify({"status": "ok", "message": "Preview cache cleared"})
    return jsonify({"status": "ok", "message": "No preview cache to clear"})


@app.route("/api/payments/sync-paid-bills", methods=["POST"])
def api_payments_sync_paid_bills():
    """Fetch all paid bills from Zoho and cache them, including banking-matched bills."""
    try:
        from scripts.utils import load_config, ZohoBooksAPI, log_action
        config = load_config()
        api = ZohoBooksAPI(config)

        # 1. Fetch explicitly paid bills
        paid_zoho = []
        page = 1
        while True:
            result = api.list_bills(status="paid", page=page)
            paid_zoho.extend(result.get("bills", []))
            if not result.get("page_context", {}).get("has_more_page", False):
                break
            page += 1

        cache = {}
        for b in paid_zoho:
            bid = b.get("bill_id")
            if bid:
                cache[bid] = {
                    "vendor_name": b.get("vendor_name", ""),
                    "amount": float(b.get("total", 0)),
                    "currency": b.get("currency_code", "INR"),
                    "date": b.get("date", ""),
                    "bill_number": b.get("bill_number", ""),
                }

        # 2. Also detect banking-matched bills (open/overdue with balance=0)
        banking_matched = 0
        for status in ("open", "overdue", "unpaid"):
            page = 1
            while True:
                result = api.list_bills(status=status, page=page)
                for b in result.get("bills", []):
                    bid = b.get("bill_id")
                    if not bid or bid in cache:
                        continue
                    balance = float(b.get("balance", b.get("total", 1)))
                    if balance <= 0:
                        cache[bid] = {
                            "vendor_name": b.get("vendor_name", ""),
                            "amount": float(b.get("total", 0)),
                            "currency": b.get("currency_code", "INR"),
                            "date": b.get("date", ""),
                            "bill_number": b.get("bill_number", ""),
                            "banking_matched": True,
                        }
                        banking_matched += 1
                if not result.get("page_context", {}).get("has_more_page", False):
                    break
                page += 1

        _save_paid_bills_cache(cache)
        log_action(f"Synced {len(cache)} paid bills to cache ({banking_matched} banking-matched)")
        return jsonify({"status": "ok", "count": len(cache), "banking_matched": banking_matched})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/payments/paid-bills-cache")
def api_payments_paid_bills_cache():
    """Return the paid bills cache for frontend cross-reference."""
    cache = _load_paid_bills_cache()
    return jsonify({"paid_bills": cache, "count": len(cache)})


@app.route("/api/cc/clear-parsed", methods=["POST"])
def api_cc_clear_parsed():
    """Delete cc_transactions.json and all CC CSV files so Step 4 re-parses fresh."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    removed = []
    # Remove combined JSON
    json_path = os.path.join(output_dir, "cc_transactions.json")
    if os.path.exists(json_path):
        os.remove(json_path)
        removed.append("cc_transactions.json")
    # Remove all card CSV files
    import glob
    for csv_path in glob.glob(os.path.join(output_dir, "*_transactions.csv")):
        os.remove(csv_path)
        removed.append(os.path.basename(csv_path))
    if removed:
        return jsonify({"status": "ok", "message": f"Cleared: {', '.join(removed)}"})
    return jsonify({"status": "ok", "message": "No parsed data to clear"})


@app.route("/api/banking/clear-cache", methods=["POST"])
def api_banking_clear_cache():
    """Clear the imported_statements.json tracking file so Step 5 re-imports."""
    tracking_file = os.path.join(PROJECT_ROOT, "output", "imported_statements.json")
    if os.path.exists(tracking_file):
        os.remove(tracking_file)
        return jsonify({"status": "ok", "message": "Import cache cleared"})
    return jsonify({"status": "ok", "message": "No cache to clear"})


@app.route("/api/banking/delete-transactions", methods=["POST"])
def api_banking_delete_transactions():
    """Delete all banking transactions for a specific CC card from Zoho, then clear cache."""
    data = request.json or {}
    card_name = data.get("card_name")
    if not card_name:
        return jsonify({"status": "error", "message": "card_name required"}), 400

    try:
        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        from scripts.utils import resolve_account_ids
        resolve_account_ids(api, cards)

        card = next((c for c in cards if c["name"] == card_name), None)
        if not card:
            return jsonify({"status": "error", "message": f"Card '{card_name}' not found"}), 404

        account_id = card["zoho_account_id"]

        # Fetch all transactions for this account
        all_txns = []
        page = 1
        while True:
            result = api.list_bank_transactions(account_id, page=page)
            txns = result.get("banktransactions", [])
            if not txns:
                break
            all_txns.extend(txns)
            if not result.get("page_context", {}).get("has_more_page", False):
                break
            page += 1

        deleted = 0
        for txn in all_txns:
            txn_id = txn["transaction_id"]
            status = txn.get("status", "").lower()
            try:
                if status in ("matched", "categorized"):
                    try:
                        api.uncategorize_transaction(txn_id)
                    except Exception:
                        pass
                api.delete_bank_transaction(txn_id)
                deleted += 1
            except Exception:
                pass

        # Clear import cache so Step 5 re-imports fresh
        tracking_file = os.path.join(PROJECT_ROOT, "output", "imported_statements.json")
        if os.path.exists(tracking_file):
            os.remove(tracking_file)

        return jsonify({"status": "ok", "message": f"Deleted {deleted} transactions for {card_name}", "deleted": deleted})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


_BANKING_SUMMARY_CACHE = os.path.join(PROJECT_ROOT, "output", "banking_summary_cache.json")


def _banking_summary_from_api():
    """Fetch all banking transactions per status from Zoho, cache and return."""
    from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action
    config = load_config()
    api = ZohoBooksAPI(config)
    cards = config.get("credit_cards", [])
    resolve_account_ids(api, cards)

    STATUSES = ["uncategorized", "matched", "manually_added", "categorized"]

    def _fetch_all(account_id, status):
        txns = []
        page = 1
        while True:
            result = api._request("GET", "banktransactions", params={
                "account_id": account_id, "status": status, "page": page,
            })
            batch = result.get("banktransactions", [])
            txns.extend(batch)
            if not result.get("page_context", {}).get("has_more_page", False):
                break
            page += 1
        return txns

    # Fetch per card, per status — store raw txns for cache
    all_raw = []  # list of {card, status, txn_id, date, amount, description}
    for card in cards:
        account_id = card.get("zoho_account_id")
        card_name = card.get("name", "")
        if not account_id:
            continue
        for status in STATUSES:
            txns = _fetch_all(account_id, status)
            for t in txns:
                all_raw.append({
                    "card": card_name,
                    "status": status,
                    "transaction_id": t.get("transaction_id", ""),
                    "date": t.get("date", ""),
                    "amount": abs(float(t.get("amount", 0))),
                    "description": t.get("description", ""),
                })

    # Save cache
    cache_data = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transactions": all_raw,
    }
    os.makedirs(os.path.dirname(_BANKING_SUMMARY_CACHE), exist_ok=True)
    with open(_BANKING_SUMMARY_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2, ensure_ascii=False)

    log_action(f"Banking summary: fetched {len(all_raw)} txns from Zoho, cached")
    return all_raw


def _banking_summary_build(all_raw):
    """Build month-wise summary from raw transaction list. Only current financial year (Apr-Mar)."""
    from collections import defaultdict

    # Indian financial year: Apr YYYY to Mar YYYY+1. Include current and previous FY.
    today = datetime.now()
    if today.month >= 4:
        fy_start = f"{today.year - 1}-04"
        fy_end = f"{today.year + 1}-03"
    else:
        fy_start = f"{today.year - 2}-04"
        fy_end = f"{today.year}-03"

    STATUSES = ["matched", "manually_added", "categorized", "uncategorized"]
    amt_keys = [s + "_amount" for s in STATUSES]
    agg_keys = STATUSES + ["total"] + amt_keys + ["total_amount"]

    months = defaultdict(lambda: defaultdict(lambda: {k: 0 for k in agg_keys}))

    for t in all_raw:
        date_str = t.get("date", "")
        month_key = date_str[:7] if len(date_str) >= 7 else "Unknown"
        # Filter to current financial year only
        if month_key != "Unknown" and (month_key < fy_start or month_key > fy_end):
            continue
        card_name = t.get("card", "Unknown")
        status = t.get("status", "uncategorized")
        amount = float(t.get("amount", 0))

        bucket = months[month_key][card_name]
        bucket["total"] += 1
        bucket["total_amount"] += amount
        bucket[status] += 1
        bucket[status + "_amount"] += amount

    result_months = []
    for month_key in sorted(months.keys(), reverse=True):
        card_data = months[month_key]
        month_total = {k: 0 for k in agg_keys}
        card_list = []
        for cname in sorted(card_data.keys()):
            cd = card_data[cname]
            card_list.append({"card": cname, **cd})
            for k in agg_keys:
                month_total[k] += cd.get(k, 0)
        result_months.append({"month": month_key, "totals": month_total, "cards": card_list})

    grand_total = {k: 0 for k in agg_keys}
    for m in result_months:
        for k in agg_keys:
            grand_total[k] += m["totals"].get(k, 0)

    card_names = sorted(set(t.get("card", "") for t in all_raw))
    return grand_total, result_months, card_names


@app.route("/api/banking/summary")
def api_banking_summary():
    """Month-wise summary with cache. Use ?refresh=1 to force Zoho fetch."""
    try:
        from scripts.utils import log_action
        force = request.args.get("refresh", "0") == "1"

        all_raw = None
        # Try cache first
        if not force and os.path.exists(_BANKING_SUMMARY_CACHE):
            with open(_BANKING_SUMMARY_CACHE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            all_raw = cache_data.get("transactions", [])
            fetched_at = cache_data.get("fetched_at", "")
            log_action(f"Banking summary: loaded {len(all_raw)} txns from cache ({fetched_at})")

        # No cache or forced refresh
        if all_raw is None:
            all_raw = _banking_summary_from_api()
            fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        grand_total, result_months, card_names = _banking_summary_build(all_raw)

        return jsonify({
            "grand_total": grand_total,
            "months": result_months,
            "card_names": card_names,
            "total_transactions": grand_total["total"],
            "cached": not force,
            "fetched_at": fetched_at if 'fetched_at' in dir() else "",
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/banking/vendor-breakdown")
def api_banking_vendor_breakdown():
    """Fuzzy-match uncategorized CC transactions to vendors and return breakdown."""
    try:
        from scripts.utils import load_config, log_action

        # Load banking summary cache (uncategorized txns)
        if not os.path.exists(_BANKING_SUMMARY_CACHE):
            return jsonify({"error": "No banking data cached. Open Banking Summary first."}), 400

        with open(_BANKING_SUMMARY_CACHE, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
        all_raw = cache_data.get("transactions", [])

        uncat = [t for t in all_raw if t.get("status") == "uncategorized"]

        # Load vendor mappings for resolution
        config = load_config()
        vm = config.get("vendor_mappings", {})
        cc_map = vm.get("cc_description_to_vendor", {})
        gstin_map = vm.get("gstin_to_vendor", {})

        # Build lookup: lowered key -> vendor
        vm_lower = {}
        for k, v in cc_map.items():
            vm_lower[k.lower()] = v

        # Load learned mappings
        learned_path = os.path.join(PROJECT_ROOT, "config", "learned_vendor_mappings.json")
        learned = {}
        if os.path.exists(learned_path):
            with open(learned_path, "r", encoding="utf-8") as f:
                learned = json.load(f)

        # Load bills cache for vendor list
        bills_cache = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
        bill_vendors = {}
        bills = []
        if os.path.exists(bills_cache) and os.path.getsize(bills_cache) > 0:
            try:
                with open(bills_cache, "r", encoding="utf-8") as f:
                    bills = json.load(f)
            except json.JSONDecodeError:
                bills = []
        for b in bills:
            vn = b.get("vendor_name", "")
            if vn:
                bill_vendors[vn.lower()] = vn
                words = vn.split()
                if words:
                    fw = words[0].lower()
                    if len(fw) >= 4:
                        bill_vendors[fw] = vn

        def _resolve(desc):
            if not desc:
                return "Unknown"
            dl = desc.lower()
            dn = "".join(c for c in dl if c.isalnum() or c == " ")

            # CC mapping (exact + substring)
            if dl in vm_lower:
                return vm_lower[dl]
            for key in sorted(vm_lower.keys(), key=len, reverse=True):
                if key and len(key) >= 4 and key in dl:
                    return vm_lower[key]

            # Learned (uppercase)
            du = desc.strip().upper()
            if du in learned:
                return learned[du]
            for key in sorted(learned.keys(), key=len, reverse=True):
                if key and len(key) >= 4 and key in du:
                    return learned[key]

            # Fuzzy: first keyword against bill vendors
            _noise = {"si", "in", "mumbai", "bangalore", "chennai", "delhi",
                      "india", "ca", "us", "www", "https", "com", "pte", "pvt", "ltd"}
            tokens = [t.lower() for t in desc.replace(",", " ").replace("*", " ").split()
                      if len(t) >= 3 and t.lower() not in _noise]
            for tok in tokens[:3]:
                if tok in bill_vendors:
                    return bill_vendors[tok]

            # Return first meaningful token as label
            return tokens[0].title() if tokens else "Unknown"

        # Build vendor breakdown
        from collections import defaultdict
        vendor_data = defaultdict(lambda: {"count": 0, "amount": 0, "cards": defaultdict(int), "txns": []})

        for t in uncat:
            vendor = _resolve(t.get("description", ""))
            vd = vendor_data[vendor]
            vd["count"] += 1
            vd["amount"] += t.get("amount", 0)
            vd["cards"][t.get("card", "")] += 1
            if len(vd["txns"]) < 5:  # limit sample txns
                vd["txns"].append({
                    "date": t.get("date", ""),
                    "amount": t.get("amount", 0),
                    "description": t.get("description", "")[:80],
                    "card": t.get("card", ""),
                })

        # Sort by count desc
        result = []
        for vendor, vd in sorted(vendor_data.items(), key=lambda x: -x[1]["count"]):
            result.append({
                "vendor": vendor,
                "count": vd["count"],
                "amount": round(vd["amount"], 2),
                "cards": dict(vd["cards"]),
                "txns": vd["txns"],
            })

        log_action(f"Vendor breakdown: {len(uncat)} uncategorized -> {len(result)} vendors")
        return jsonify({
            "total_uncategorized": len(uncat),
            "vendors": result,
            "fetched_at": cache_data.get("fetched_at", ""),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/banking/auto-match-preview")
def api_banking_auto_match_preview():
    """Fetch uncategorized CC transactions and their best matching suggestions."""
    try:
        from scripts.utils import load_config, ZohoBooksAPI, resolve_account_ids, log_action
        config = load_config()
        api = ZohoBooksAPI(config)
        cards = config.get("credit_cards", [])
        resolve_account_ids(api, cards)

        # Fetch uncategorized transactions from all cards (paginated)
        all_txns = []
        for card in cards:
            account_id = card.get("zoho_account_id")
            card_name = card.get("name", "")
            if not account_id:
                continue
            page = 1
            while True:
                result = api.list_uncategorized(account_id, page=page)
                txns = result.get("banktransactions", [])
                for t in txns:
                    t["_card_name"] = card_name
                    t["_account_id"] = account_id
                all_txns.extend(txns)
                if not result.get("page_context", {}).get("has_more_page", False):
                    break
                page += 1

        log_action(f"Auto-match preview: {len(all_txns)} uncategorized transactions")

        # Build response (no match fetching — lazy loaded per row)
        items = []
        card_counts = {}
        for t in all_txns:
            card_name = t.get("_card_name", "")
            card_counts[card_name] = card_counts.get(card_name, 0) + 1
            items.append({
                "transaction_id": t.get("transaction_id", ""),
                "account_id": t.get("_account_id", ""),
                "card_name": card_name,
                "description": t.get("description", "") or t.get("reference_number", ""),
                "amount": abs(float(t.get("amount", 0))),
                "date": t.get("date", ""),
                "debit_or_credit": t.get("debit_or_credit", ""),
            })

        return jsonify({
            "items": items,
            "card_names": [c.get("name", "") for c in cards],
            "card_counts": card_counts,
            "total": len(items),
        })

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"Auto-match preview error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/banking/get-matches/<transaction_id>")
def api_banking_get_matches(transaction_id):
    """Fetch matching suggestions for a single uncategorized transaction."""
    try:
        from scripts.utils import load_config, ZohoBooksAPI
        config = load_config()
        api = ZohoBooksAPI(config)
        result = api.get_matching_transactions(transaction_id)
        candidates = result.get("matching_transactions", [])
        items = []
        for c in candidates:
            items.append({
                "transaction_id": c.get("transaction_id", ""),
                "transaction_type": c.get("transaction_type", ""),
                "vendor_name": c.get("vendor_name") or c.get("payee_name") or "",
                "amount": float(c.get("amount", 0)),
                "date": c.get("date", ""),
                "reference": c.get("reference_number") or c.get("bill_number") or "",
            })
        return jsonify({"matches": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/banking/confirm-match", methods=["POST"])
def api_banking_confirm_match():
    """Confirm a match: categorize a banking transaction."""
    data = request.json or {}
    txn_id = data.get("transaction_id")
    match_txn_id = data.get("match_transaction_id")
    match_txn_type = data.get("match_transaction_type")

    if not txn_id or not match_txn_id:
        return jsonify({"error": "transaction_id and match_transaction_id required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, log_action
        config = load_config()
        api = ZohoBooksAPI(config)

        match_data = [{
            "transaction_id": match_txn_id,
            "transaction_type": match_txn_type or "vendor_payment",
        }]
        api.match_transaction(txn_id, match_data)
        log_action(f"Auto-match confirmed: {txn_id} -> {match_txn_id} ({match_txn_type})")
        return jsonify({"status": "matched", "transaction_id": txn_id})

    except Exception as e:
        from scripts.utils import log_action
        log_action(f"Auto-match confirm error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


@app.route("/api/banking/confirm-match-bulk", methods=["POST"])
def api_banking_confirm_match_bulk():
    """Confirm multiple matches at once."""
    data = request.json or {}
    matches = data.get("matches", [])
    if not matches:
        return jsonify({"error": "matches array required"}), 400

    try:
        from scripts.utils import load_config, ZohoBooksAPI, log_action
        config = load_config()
        api = ZohoBooksAPI(config)

        results = []
        for m in matches:
            txn_id = m.get("transaction_id")
            match_txn_id = m.get("match_transaction_id")
            match_txn_type = m.get("match_transaction_type", "vendor_payment")
            try:
                api.match_transaction(txn_id, [{
                    "transaction_id": match_txn_id,
                    "transaction_type": match_txn_type,
                }])
                results.append({"transaction_id": txn_id, "status": "matched"})
                log_action(f"Auto-match confirmed: {txn_id} -> {match_txn_id}")
            except Exception as e:
                results.append({"transaction_id": txn_id, "status": "error", "message": str(e)})

        ok = sum(1 for r in results if r["status"] == "matched")
        return jsonify({"status": "ok", "matched": ok, "total": len(matches), "results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/invoices/browse")
def api_invoices_browse():
    """Return all extracted invoices for browse view, grouped by month."""
    invoices_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
    if not os.path.exists(invoices_path):
        return jsonify({"months": {}})

    try:
        with open(invoices_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return jsonify({"months": {}})
        invoices = json.loads(content)
    except Exception as e:
        return jsonify({"error": f"Failed to read invoices: {e}"}), 500

    # Build month-grouped data
    months = {}
    for inv in invoices:
        date = inv.get("date", "")
        month_key = date[:7] if date and len(date) >= 7 else "Unknown"
        if month_key not in months:
            months[month_key] = []

        # Build line items description summary
        line_items_desc = ""
        if inv.get("line_items"):
            descs = [it.get("description", "")[:100] for it in inv["line_items"][:5]]
            line_items_desc = " | ".join(descs)

        months[month_key].append({
            "date": date,
            "vendor_name": inv.get("vendor_name") or "Unknown",
            "invoice_number": inv.get("invoice_number") or "-",
            "amount": inv.get("amount"),
            "currency": inv.get("currency", "INR"),
            "line_items_desc": line_items_desc,
            "line_items_count": len(inv.get("line_items", [])),
            "file": inv.get("file", ""),
        })

    # Sort months descending, invoices by date
    sorted_months = sorted(months.keys(), reverse=True)
    for m in sorted_months:
        months[m].sort(key=lambda x: x.get("date") or "")

    return jsonify({
        "months": sorted_months,
        "data": months,
        "total": len(invoices),
    })


@app.route("/api/cc/preview")
def api_cc_preview():
    """Return the last CC parse run's preview: parsed (per card) + failed + skipped."""
    preview_path = os.path.join(PROJECT_ROOT, "output", "cc_parse_preview.json")
    if not os.path.exists(preview_path):
        return jsonify({
            "source": None,
            "timestamp": None,
            "parsed": [],
            "failed": [],
            "skipped": [],
            "total_transactions": 0,
            "empty": True,
        })
    try:
        with open(preview_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read cc_parse_preview.json: {e}"}), 500

    return jsonify({
        "source": data.get("source"),
        "timestamp": data.get("timestamp"),
        "parsed": data.get("parsed", []),
        "failed": data.get("failed", []),
        "skipped": data.get("skipped", []),
        "total_transactions": data.get("total_transactions", 0),
        "empty": False,
    })


@app.route("/api/extract/preview")
def api_extract_preview():
    """Return the last extract run's preview: extracted + failed + skipped files."""
    preview_path = os.path.join(PROJECT_ROOT, "output", "extract_preview.json")
    if not os.path.exists(preview_path):
        return jsonify({
            "source": None,
            "timestamp": None,
            "extracted": [],
            "failed": [],
            "skipped": [],
            "empty": True,
        })
    try:
        with open(preview_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read extract_preview.json: {e}"}), 500

    return jsonify({
        "source": data.get("source"),
        "timestamp": data.get("timestamp"),
        "extracted": data.get("extracted", []),
        "failed": data.get("failed", []),
        "skipped": data.get("skipped", []),
        "empty": False,
    })


@app.route("/api/invoices/list")
def api_invoices_list():
    """List extracted invoices grouped by month, with bill creation status."""
    invoices_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
    bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")

    if not os.path.exists(invoices_path):
        return jsonify({"months": [], "summary": {"total": 0, "created": 0, "pending": 0}})

    try:
        with open(invoices_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return jsonify({"months": [], "summary": {"total": 0, "created": 0, "pending": 0}})
        invoices = json.loads(content)
    except Exception as e:
        return jsonify({"error": f"Failed to read invoices: {e}"}), 500

    # Build set of already-created filenames — verify against Zoho (not just local file)
    created_files = set()
    if os.path.exists(bills_path):
        try:
            with open(bills_path, "r", encoding="utf-8") as f:
                bills_content = f.read().strip()
            local_bills = json.loads(bills_content) if bills_content else []

            # Fetch actual bill IDs from Zoho to verify local tracking is accurate
            from scripts.utils import load_config, ZohoBooksAPI
            zoho_bill_ids = set()
            try:
                config = load_config()
                api = ZohoBooksAPI(config)
                page = 1
                while True:
                    result = api.list_bills() if page == 1 else api._request("GET", "bills", params={"page": page})
                    for b in result.get("bills", []):
                        zoho_bill_ids.add(b.get("bill_id"))
                    if not result.get("page_context", {}).get("has_more_page", False):
                        break
                    page += 1
            except Exception as e:
                from scripts.utils import log_action
                log_action(f"Zoho bill verification failed: {e}", "WARNING")
                zoho_bill_ids = None  # Zoho unavailable — fall back to local data

            cleaned_bills = []
            for entry in local_bills:
                if entry.get("status") == "created" and entry.get("bill_id"):
                    # Only trust "created" if bill still exists in Zoho (or Zoho check failed)
                    if zoho_bill_ids is None or entry["bill_id"] in zoho_bill_ids:
                        created_files.add(entry.get("file", ""))
                        cleaned_bills.append(entry)
                    else:
                        # Bill was deleted in Zoho — remove from local tracking
                        cleaned_bills.append({**entry, "status": "deleted_in_zoho"})
                else:
                    cleaned_bills.append(entry)

            # Update local file if any bills were removed from Zoho
            if zoho_bill_ids is not None and len(cleaned_bills) != len(local_bills):
                try:
                    with open(bills_path, "w", encoding="utf-8") as f:
                        json.dump(cleaned_bills, f, indent=2)
                except Exception:
                    pass
        except Exception:
            pass

    # Group invoices by month
    from collections import defaultdict
    month_groups = defaultdict(list)
    no_date = []
    for inv in invoices:
        date_str = inv.get("date", "")
        file_name = inv.get("file", "")
        status = "created" if file_name in created_files else "pending"
        entry = {
            "file": file_name,
            "vendor_name": inv.get("vendor_name", "Unknown"),
            "amount": inv.get("amount", 0),
            "currency": inv.get("currency", "INR"),
            "date": date_str,
            "status": status,
        }
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                month_key = dt.strftime("%b %Y")
                month_groups[month_key].append(entry)
                continue
            except ValueError:
                pass
        no_date.append(entry)

    # Sort months chronologically (newest first)
    def _month_sort_key(m):
        try:
            return datetime.strptime(m, "%b %Y")
        except ValueError:
            return datetime.min
    sorted_months = sorted(month_groups.keys(), key=_month_sort_key, reverse=True)

    months = []
    for m in sorted_months:
        months.append({"month": m, "invoices": month_groups[m]})
    if no_date:
        months.append({"month": "No Date", "invoices": no_date})

    total = len(invoices)
    pending = total - len(created_files & {inv.get("file", "") for inv in invoices})
    created = total - pending

    return jsonify({
        "months": months,
        "summary": {"total": total, "pending": pending, "created": created},
    })


@app.route("/api/upload/cc", methods=["POST"])
def api_upload_cc():
    """Receive multipart PDF files, save to input_pdfs/cc_statements/."""
    cc_dir = os.path.join(PROJECT_ROOT, "input_pdfs", "cc_statements")
    os.makedirs(cc_dir, exist_ok=True)

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    saved = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            continue
        # Secure the filename
        safe_name = os.path.basename(f.filename)
        dest = os.path.join(cc_dir, safe_name)
        f.save(dest)
        saved.append(safe_name)
        log_action(f"Uploaded CC statement: {safe_name}")

    if not saved:
        return jsonify({"error": "No valid PDF files in upload"}), 400

    return jsonify({"ok": True, "files": saved})


@app.route("/api/upload/invoices", methods=["POST"])
def api_upload_invoices():
    """Receive multipart PDF/image/EML files, save to 'new image invoices',
    then extract to extracted_invoices.json and compare_invoices.json."""
    upload_dir = os.path.join(PROJECT_ROOT, "new image invoices")
    os.makedirs(upload_dir, exist_ok=True)

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    allowed_exts = (".pdf", ".jpg", ".jpeg", ".png", ".eml")
    saved = []
    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed_exts:
            continue
        safe_name = os.path.basename(f.filename)
        dest = os.path.join(upload_dir, safe_name)
        f.save(dest)
        saved.append(safe_name)
        log_action(f"Uploaded invoice: {safe_name}")

    if not saved:
        return jsonify({"error": "No valid files in upload (PDF, JPG, PNG, EML)"}), 400

    # Start background extraction thread
    with _state_lock:
        if _state["running"]:
            return jsonify({"ok": True, "files": saved, "extract": "skipped", "reason": "A step is already running"}), 200
        _state["running"] = True

    def _upload_extract_thread():
        try:
            with _state_lock:
                _state["current_step"] = "upload-extract"
                _state["step_results"]["upload-extract"] = {
                    "status": "running",
                    "message": f"Extracting {len(saved)} uploaded file(s)...",
                    "timestamp": datetime.now().isoformat(),
                }

            log_action("=" * 50)
            log_action(f"Upload & Extract: Processing {len(saved)} file(s)")
            log_action("=" * 50)

            mod_02 = _import_script("02_extract_invoices.py")

            # --- Load existing extracted_invoices.json ---
            ext_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
            existing_extracted = []
            if os.path.exists(ext_path) and os.path.getsize(ext_path) > 0:
                with open(ext_path, "r", encoding="utf-8") as f:
                    existing_extracted = json.load(f)
            already_done_ext = {inv.get("file") for inv in existing_extracted}

            # --- Load existing compare_invoices.json ---
            cmp_path = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")
            existing_compare = []
            if os.path.exists(cmp_path) and os.path.getsize(cmp_path) > 0:
                with open(cmp_path, "r", encoding="utf-8") as f:
                    existing_compare = json.load(f)
            already_done_cmp = {inv.get("file") for inv in existing_compare}

            new_extracted = []
            new_compare = []
            failed_files = []
            skipped_files = []
            failed = 0
            skipped = 0

            for fname in saved:
                fpath = os.path.join(upload_dir, fname)
                if fname in already_done_ext:
                    log_action(f"  Skipping (already extracted): {fname}")
                    skipped_files.append({"file": fname, "reason": "Already extracted"})
                    skipped += 1
                    continue

                log_action(f"  Extracting: {fname}")
                try:
                    invoice = mod_02.extract_invoice(fpath, fname)
                    if invoice:
                        inv_list = invoice if isinstance(invoice, list) else [invoice]
                        for item in inv_list:
                            new_extracted.append(item)
                            log_action(f"    [{item.get('invoice_number', '?')}] {item.get('vendor_name', '?')}: {item.get('amount', '?')} {item.get('currency', '?')}")

                            # Also add to compare list with metadata
                            cmp_item = dict(item)
                            cmp_item["organized_month"] = "Uploaded"
                            cmp_item["organized_path"] = fpath
                            new_compare.append(cmp_item)
                    else:
                        log_action(f"    No data extracted from {fname}", "WARNING")
                        failed_files.append({"file": fname, "reason": "No data extracted"})
                        failed += 1
                except Exception as ex:
                    log_action(f"    FAILED: {fname}: {ex}", "ERROR")
                    failed_files.append({"file": fname, "reason": str(ex)[:200]})
                    failed += 1

            # --- Append to extracted_invoices.json ---
            if new_extracted:
                existing_extracted.extend(new_extracted)
                os.makedirs(os.path.dirname(ext_path), exist_ok=True)
                with open(ext_path, "w", encoding="utf-8") as f:
                    json.dump(existing_extracted, f, indent=2, ensure_ascii=False)
                log_action(f"Updated extracted_invoices.json: +{len(new_extracted)} (total {len(existing_extracted)})")

            # --- Append to compare_invoices.json (dedup by invoice_number) ---
            if new_compare:
                generic = {"payment", "original", "invoice", "receipt", "bill", "tax", "none", "n/a", ""}
                seen_nums = set()
                for inv in existing_compare:
                    num = inv.get("invoice_number", "")
                    if num and num.lower().strip() not in generic:
                        seen_nums.add(num)

                deduped_new = []
                for inv in new_compare:
                    num = inv.get("invoice_number", "")
                    if num and num.lower().strip() not in generic and num in seen_nums:
                        log_action(f"  Dedup: skipping {inv.get('file', '?')} (#{num} already in compare)")
                        continue
                    if num and num.lower().strip() not in generic:
                        seen_nums.add(num)
                    deduped_new.append(inv)

                if deduped_new:
                    existing_compare.extend(deduped_new)
                    with open(cmp_path, "w", encoding="utf-8") as f:
                        json.dump(existing_compare, f, indent=2, ensure_ascii=False)
                    log_action(f"Updated compare_invoices.json: +{len(deduped_new)} (total {len(existing_compare)})")

            # Write preview snapshot for UI
            preview_path = os.path.join(PROJECT_ROOT, "output", "extract_preview.json")
            try:
                with open(preview_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "source": "upload",
                        "timestamp": datetime.now().isoformat(),
                        "extracted": new_extracted,
                        "failed": failed_files,
                        "skipped": skipped_files,
                    }, f, indent=2, ensure_ascii=False)
            except Exception as pe:
                log_action(f"  Failed to write extract_preview.json: {pe}", "WARNING")

            msg = f"Extracted {len(new_extracted)} new, skipped {skipped}, failed {failed}"
            with _state_lock:
                _state["step_results"]["upload-extract"] = {
                    "status": "success",
                    "message": msg,
                    "timestamp": datetime.now().isoformat(),
                    "result": {"extracted_count": len(new_extracted), "failed_count": failed, "skipped_count": skipped},
                }
            log_action(f"=== Upload & Extract DONE: {msg} ===")

        except Exception as e:
            tb = traceback.format_exc()
            log_action(f"Upload & Extract FAILED: {e}", "ERROR")
            log_action(tb, "ERROR")
            with _state_lock:
                _state["step_results"]["upload-extract"] = {
                    "status": "error",
                    "message": str(e)[:200],
                    "timestamp": datetime.now().isoformat(),
                }
        finally:
            with _state_lock:
                _state["running"] = False
                _state["current_step"] = None

    t = threading.Thread(target=_upload_extract_thread, daemon=True)
    t.start()
    return jsonify({"ok": True, "files": saved})


def _get_summary():
    """Build a summary from output JSON files if they exist."""
    summary = {}
    output_dir = os.path.join(PROJECT_ROOT, "output")

    # Extracted invoices count
    invoices_path = os.path.join(output_dir, "extracted_invoices.json")
    if os.path.exists(invoices_path):
        try:
            with open(invoices_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            summary["invoices"] = len(data) if isinstance(data, list) else 0
        except Exception:
            pass

    # CC transactions count
    cc_path = os.path.join(output_dir, "cc_transactions.json")
    if os.path.exists(cc_path):
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                summary["cc_transactions"] = len(data)
            elif isinstance(data, dict):
                total = 0
                for card_txns in data.values():
                    if isinstance(card_txns, list):
                        total += len(card_txns)
                summary["cc_transactions"] = total
        except Exception:
            pass

    # Bills created
    bills_path = os.path.join(output_dir, "created_bills.json")
    if os.path.exists(bills_path):
        try:
            with open(bills_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            summary["bills"] = len(data) if isinstance(data, list) else 0
        except Exception:
            pass

    return summary


def _determine_unmatched_reason(payment, cc_transactions, reverse_vendor_map):
    """Analyze why a bill didn't match any CC transaction."""
    vendor_name = payment.get("vendor_name", "")
    if not vendor_name:
        return "No vendor name on bill"

    bill_amount = payment.get("amount", 0)
    currency = payment.get("currency", "INR")

    keywords = reverse_vendor_map.get(vendor_name.lower(), [vendor_name.lower()])

    # Search CC transactions for vendor keyword matches
    vendor_txns = []
    for txn in cc_transactions:
        desc_lower = txn.get("description", "").lower()
        for kw in keywords:
            if kw in desc_lower or desc_lower.startswith(kw[:10]):
                vendor_txns.append(txn)
                break

    if not vendor_txns:
        return "No CC transaction found for this vendor"

    if currency == "USD":
        return (f"Vendor found in CC ({len(vendor_txns)} txn) but "
                f"USD ${bill_amount} not matched (exchange rate / date mismatch)")

    amounts = [t["amount"] for t in vendor_txns]
    closest = min(amounts, key=lambda a: abs(a - bill_amount))
    diff = abs(closest - bill_amount)
    pct = (diff / bill_amount * 100) if bill_amount else 0
    return (f"Vendor in CC ({len(vendor_txns)} txn) - amount mismatch "
            f"(bill: {bill_amount:,.2f}, closest CC: {closest:,.2f}, diff: {pct:.1f}%)")


@app.route("/api/match-status")
def api_match_status():
    """Return match status data for the Match Status dashboard panel."""
    output_dir = os.path.join(PROJECT_ROOT, "output")

    payments_path = os.path.join(output_dir, "recorded_payments.json")
    payments = []
    if os.path.exists(payments_path):
        try:
            with open(payments_path, "r", encoding="utf-8") as f:
                payments = json.load(f)
        except Exception:
            return jsonify({"error": "Failed to read recorded_payments.json"}), 500

    if not payments:
        return jsonify({"error": "No payment data found. Run Step 6 (Payments) first."}), 404

    # Load CC transactions for unmatched reason analysis
    cc_path = os.path.join(output_dir, "cc_transactions.json")
    cc_transactions = []
    if os.path.exists(cc_path):
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                cc_transactions = json.load(f)
        except Exception:
            pass

    # Build reverse vendor map: vendor_name -> merchant keywords
    vendor_mappings_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    reverse_map = {}
    try:
        with open(vendor_mappings_path, "r", encoding="utf-8") as f:
            vm = json.load(f)
        for merchant, vendor in vm.get("mappings", {}).items():
            reverse_map.setdefault(vendor.lower(), []).append(merchant.lower())
    except Exception:
        pass

    matched = []
    unmatched = []

    for p in payments:
        row = {
            "file": p.get("file"),
            "vendor_name": p.get("vendor_name"),
            "amount": p.get("amount"),
            "currency": p.get("currency", "INR"),
            "cc_inr_amount": p.get("cc_inr_amount"),
            "cc_card": p.get("cc_card"),
            "status": p.get("status"),
        }

        if p.get("status") == "paid":
            matched.append(row)
        else:
            row["reason"] = _determine_unmatched_reason(
                p, cc_transactions, reverse_map
            )
            unmatched.append(row)

    return jsonify({
        "matched": matched,
        "unmatched": unmatched,
        "summary": {
            "matched_count": len(matched),
            "unmatched_count": len(unmatched),
            "total_count": len(payments),
        },
    })


@app.route("/api/check-cc-match")
def api_check_cc_match():
    """Compare cached unpaid bills with cached CC transactions for side-by-side review (no live Zoho calls)."""
    try:
        mod_05 = _import_script("05_record_payments.py")
        from scripts.utils import load_vendor_mappings

        # Load cached data
        bills_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
        cc_txns_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_cc_transactions_cache.json")

        if not os.path.exists(bills_cache_path) or not os.path.exists(cc_txns_cache_path):
            return jsonify({"error": "Cache not found. Click Sync Zoho first."}), 404

        with open(bills_cache_path, "r", encoding="utf-8") as f:
            all_bills = json.load(f)
        with open(cc_txns_cache_path, "r", encoding="utf-8") as f:
            cc_raw = json.load(f)

        # Filter to unpaid/overdue bills only
        bills = [
            {
                "vendor_name": b.get("vendor_name", ""),
                "amount": float(b.get("total", 0)),
                "currency": b.get("currency", b.get("currency_code", "INR")),
                "date": b.get("date", ""),
            }
            for b in all_bills
            if b.get("status") in ("open", "unpaid", "overdue") and b.get("vendor_name")
        ]

        # Build keyword -> vendor_name reverse map for grouping
        vendor_mappings = load_vendor_mappings()
        vendor_to_merchants = mod_05.build_vendor_to_merchants(vendor_mappings)
        keyword_to_vendor = {}
        for bill in bills:
            vname = bill["vendor_name"]
            vkey = vname.lower()
            keyword_to_vendor[vkey] = vname
            keyword_to_vendor[vkey.replace(" ", "")] = vname
            for kw in vendor_to_merchants.get(vkey, []):
                keyword_to_vendor[kw.lower()] = vname

        def _get_vendor_for_cc(desc):
            desc_lower = desc.lower()
            desc_norm = mod_05._normalize(desc)
            for kw, vname in keyword_to_vendor.items():
                if not kw:
                    continue
                if kw in desc_lower:
                    return vname
                if desc_lower.startswith(kw[:10]):
                    return vname
                kw_norm = mod_05._normalize(kw)
                if len(kw_norm) >= 6 and kw_norm in desc_norm:
                    return vname
            return None

        # Group bills by vendor
        from collections import defaultdict
        bill_groups = defaultdict(list)
        for b in bills:
            bill_groups[b["vendor_name"]].append(b)

        # Group CC transactions by matched vendor
        cc_groups = defaultdict(list)
        unmatched_cc = []
        for t in cc_raw:
            if float(t.get("amount", 0)) <= 0:
                continue
            vname = _get_vendor_for_cc(t.get("description", ""))
            entry = {
                "description": t.get("description", ""),
                "amount": t.get("amount", 0),
                "date": t.get("date", ""),
                "card_name": t.get("card_name", ""),
            }
            if vname:
                cc_groups[vname].append(entry)
            else:
                unmatched_cc.append(entry)

        # Build grouped list sorted by vendor name
        all_vendors = sorted(set(list(bill_groups.keys()) + list(cc_groups.keys())))
        grouped = [
            {
                "vendor": v,
                "bills": bill_groups.get(v, []),
                "cc_transactions": cc_groups.get(v, []),
            }
            for v in all_vendors
        ]

        total_bills = sum(len(g["bills"]) for g in grouped)
        total_cc = sum(len(g["cc_transactions"]) for g in grouped)

        return jsonify({
            "grouped": grouped,
            "unmatched_cc": unmatched_cc,
            "summary": {
                "vendors_count": len(grouped),
                "bills_count": total_bills,
                "cc_transactions_count": total_cc,
                "unmatched_cc_count": len(unmatched_cc),
            },
        })
    except Exception as e:
        log_action(f"check-cc-match error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def _parse_month_key(month_str):
    """Parse 'Feb 2026' -> sortable tuple (2026, 2). Returns (0, 0) on failure."""
    try:
        dt = datetime.strptime(month_str, "%b %Y")
        return (dt.year, dt.month)
    except Exception:
        return (0, 0)


@app.route("/api/compare/monthly")
def api_compare_monthly():
    """Compare CC transactions vs organized invoices, grouped by month."""
    from collections import defaultdict

    output_dir = os.path.join(PROJECT_ROOT, "output")

    # --- Load CC transactions ---
    cc_path = os.path.join(output_dir, "cc_transactions.json")
    cc_transactions = []
    if os.path.exists(cc_path):
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                cc_transactions = json.load(f)
        except Exception:
            pass

    # --- Load invoices (prefer compare_invoices.json from org_inv, fallback to extracted_invoices.json) ---
    inv_path = os.path.join(output_dir, "compare_invoices.json")
    if not os.path.exists(inv_path):
        inv_path = os.path.join(output_dir, "extracted_invoices.json")
    invoices = []
    if os.path.exists(inv_path):
        try:
            with open(inv_path, "r", encoding="utf-8") as f:
                invoices = json.load(f)
        except Exception:
            pass

    if not cc_transactions and not invoices:
        return jsonify({"error": "No data found. Click 'Parse All Invoices' and 'Parse All CC' first."}), 404

    # --- Month helpers ---
    month_from_path_re = re.compile(r'organized_invoices[/\\](\w+ \d{4})[/\\]')

    def get_invoice_month(inv):
        # Prefer the actual extracted date (most reliable)
        d = inv.get("date", "")
        if d and len(d) >= 7:
            try:
                dt = datetime.strptime(d[:10], "%Y-%m-%d")
                return dt.strftime("%b %Y")
            except Exception:
                pass
        # Fallback: organized_month from folder name
        om = inv.get("organized_month", "")
        if om:
            return om
        # Fallback: parse from organized_path
        op = inv.get("organized_path", "")
        m = month_from_path_re.search(op)
        if m:
            return m.group(1)
        return "Unknown"

    def get_cc_month(txn):
        d = txn.get("date", "")
        if d and len(d) >= 7:
            try:
                dt = datetime.strptime(d[:10], "%Y-%m-%d")
                return dt.strftime("%b %Y")
            except Exception:
                pass
        return "Unknown"

    # --- Vendor name resolution from vendor_mappings ---
    vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    vendor_map = {}        # lowercased keys
    vendor_map_norm = {}   # normalized keys (no punctuation/spaces)
    def _norm(s):
        return re.sub(r'[\s.\-,*()]+', '', s.lower())
    try:
        with open(vm_path, "r", encoding="utf-8") as f:
            vm = json.load(f)
        for k, v in vm.get("mappings", {}).items():
            vendor_map[k.lower()] = v
            vendor_map_norm[_norm(k)] = v
    except Exception:
        pass
    _sorted_keys = sorted(vendor_map.keys(), key=len, reverse=True)
    _sorted_norm_keys = sorted(vendor_map_norm.keys(), key=len, reverse=True)

    def _resolve_vendor(desc):
        if not desc:
            return None
        dl = desc.lower()
        dn = _norm(desc)
        # 1. Exact lowercase match
        if dl in vendor_map:
            return vendor_map[dl]
        # 2. Exact normalized match
        if dn in vendor_map_norm:
            return vendor_map_norm[dn]
        # 3. Substring match on lowercase (longest key first)
        for key in _sorted_keys:
            if key and len(key) >= 4 and key in dl:
                return vendor_map[key]
        # 4. Substring match on normalized (handles commas, dots, etc.)
        for key in _sorted_norm_keys:
            if key and len(key) >= 4 and key in dn:
                return vendor_map_norm[key]
        return None

    # --- Load Zoho uncategorized CC transactions (use as CC source if available) ---
    zoho_cc_path = os.path.join(output_dir, "zoho_cc_transactions_cache.json")
    zoho_cc_txns = []
    if os.path.exists(zoho_cc_path):
        try:
            with open(zoho_cc_path, "r", encoding="utf-8") as f:
                zoho_cc_txns = json.load(f)
        except Exception:
            pass

    # Use Zoho uncategorized CC transactions if available, otherwise fall back to parsed
    cc_source = zoho_cc_txns if zoho_cc_txns else cc_transactions

    # --- Group by month (debits only) ---
    cc_by_month = defaultdict(list)
    for t in cc_source:
        if float(t.get("amount", 0)) <= 0:
            continue  # Skip credits (refunds, payments, waivers)
        # Use forex fields if present, otherwise parse from description
        fx_amt = t.get("forex_amount")
        fx_cur = t.get("forex_currency")
        if not fx_amt:
            desc = t.get("description", "")
            fx_m = re.search(r'\[([A-Z]{3})\s+([\d,.]+)\]', desc)
            if not fx_m:
                fx_m2 = re.search(r'\(([\d,.]+)\s+([A-Z]{3})\)', desc)
                if fx_m2:
                    try:
                        fx_amt = float(fx_m2.group(1).replace(',', ''))
                        fx_cur = fx_m2.group(2)
                    except ValueError:
                        pass
                else:
                    fx_m = re.search(r'\(([A-Z]{3})\s+([\d,.]+)\)', desc)
            if fx_m:
                try:
                    fx_amt = float(fx_m.group(2).replace(',', ''))
                    fx_cur = fx_m.group(1)
                except ValueError:
                    pass
        cc_by_month[get_cc_month(t)].append({
            "transaction_id": t.get("transaction_id", ""),
            "date": t.get("date", ""),
            "description": t.get("description", ""),
            "amount": t.get("amount", 0),
            "card_name": t.get("card_name", ""),
            "forex_amount": fx_amt,
            "forex_currency": fx_cur,
            "vendor_name": _resolve_vendor(t.get("description", "")),
        })

    # --- Load Zoho bills cache + created_bills for "In Zoho" check ---
    bills_cache_path = os.path.join(output_dir, "zoho_bills_cache.json")
    zoho_bill_numbers = {}   # bill_number -> (bill_id, status)
    zoho_bill_numbers_norm = {}  # normalized -> (bill_id, status)
    zoho_bill_ids_set = set()  # all bill_ids from cache
    zoho_vendor_date_amount = {}  # (vendor_lower, date, amount) -> (bill_id, status)
    if os.path.exists(bills_cache_path):
        try:
            with open(bills_cache_path, "r", encoding="utf-8") as f:
                for b in json.load(f):
                    bn = b.get("bill_number", "")
                    bid = b.get("bill_id", "")
                    bst = b.get("status", "")
                    bill_info = (bid, bst)
                    if bid:
                        zoho_bill_ids_set.add(bid)
                    if bn:
                        zoho_bill_numbers[bn] = bill_info
                        norm = _normalize_bill_number(bn)
                        if norm:
                            zoho_bill_numbers_norm[norm] = bill_info
                        # Index individual invoice numbers from combined bills
                        # e.g. "INV-001 + INV-002 + INV-003" -> index each part
                        if " + " in bn:
                            for part in bn.split(" + "):
                                part = part.strip()
                                if part:
                                    zoho_bill_numbers[part] = bill_info
                                    pnorm = _normalize_bill_number(part)
                                    if pnorm:
                                        zoho_bill_numbers_norm[pnorm] = bill_info
                    # Index by vendor+date+amount for fallback matching
                    bv = (b.get("vendor_name") or "").strip().lower()
                    bd = b.get("date", "")
                    ba = round(float(b.get("total", 0)), 2)
                    if bv and bd and ba and bid:
                        zoho_vendor_date_amount[(bv, bd, ba)] = bill_info
        except Exception:
            pass

    # Build vendor name aliases from vendors cache (contact_name ↔ company_name)
    _vendor_aliases = defaultdict(set)  # name_lower -> set of alias names
    vendors_cache_path = os.path.join(output_dir, "zoho_vendors_cache.json")
    if os.path.exists(vendors_cache_path):
        try:
            with open(vendors_cache_path, "r", encoding="utf-8") as f:
                for v in json.load(f):
                    cn = (v.get("contact_name") or "").strip().lower()
                    comp = (v.get("company_name") or "").strip().lower()
                    if cn and comp and cn != comp:
                        _vendor_aliases[cn].add(comp)
                        _vendor_aliases[comp].add(cn)
        except Exception:
            pass

    created_bills_set = {}  # file -> bill_id
    deleted_bills_set = set()  # files intentionally deleted
    created_bills_path = os.path.join(output_dir, "created_bills.json")
    if os.path.exists(created_bills_path):
        try:
            with open(created_bills_path, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    if entry.get("status") == "created" and entry.get("bill_id"):
                        # Only trust if bill_id still exists in Zoho cache
                        if not zoho_bill_ids_set or entry["bill_id"] in zoho_bill_ids_set:
                            created_bills_set[entry.get("file", "")] = entry["bill_id"]
                    elif entry.get("status") == "deleted_in_zoho":
                        deleted_bills_set.add(entry.get("file", ""))
        except Exception:
            pass

    _GENERIC_INV = {"payment", "original", "invoice", "bill", "tax", "none", "n/a", ""}

    def _check_in_zoho(inv):
        """Check if invoice already has a bill in Zoho.
        Returns (bill_id, status) tuple or None."""
        inv_num = (inv.get("invoice_number") or "").strip()
        fname = inv.get("file", "")
        # Check created_bills.json first (includes deleted_in_zoho)
        if fname in created_bills_set:
            return (created_bills_set[fname], "")
        if fname in deleted_bills_set:
            return ("deleted", "")
        # Check by invoice number
        has_reliable = bool(inv_num and inv_num.lower().strip() not in _GENERIC_INV)
        if has_reliable:
            if inv_num in zoho_bill_numbers:
                return zoho_bill_numbers[inv_num]
            norm = _normalize_bill_number(inv_num)
            if norm and norm in zoho_bill_numbers_norm:
                return zoho_bill_numbers_norm[norm]
            # Also check with INV- prefix
            legacy = f"INV-{inv_num}"
            if legacy in zoho_bill_numbers:
                return zoho_bill_numbers[legacy]
        else:
            # Try filename without extension as bill number
            bn = re.sub(r'\.(pdf|eml)$', '', fname, flags=re.IGNORECASE)
            if bn in zoho_bill_numbers:
                return zoho_bill_numbers[bn]
            norm = _normalize_bill_number(bn)
            if norm and norm in zoho_bill_numbers_norm:
                return zoho_bill_numbers_norm[norm]
        # Fallback: match by vendor + date + amount (for bills created manually in Zoho)
        inv_vendor = (inv.get("vendor_name") or "").strip().lower()
        inv_date = inv.get("date", "")
        inv_amount = round(float(inv.get("amount") or 0), 2)
        if inv_vendor and inv_date and inv_amount:
            # Build set of vendor name variants to check
            names_to_check = {inv_vendor}
            resolved = _resolve_vendor(inv_vendor) if inv_vendor else None
            if resolved:
                names_to_check.add(resolved.strip().lower())
            # Also check vendor alias names (contact_name ↔ company_name from vendors cache)
            if inv_vendor in _vendor_aliases:
                names_to_check.update(_vendor_aliases[inv_vendor])
            for vn in names_to_check:
                key = (vn, inv_date, inv_amount)
                if key in zoho_vendor_date_amount:
                    return zoho_vendor_date_amount[key]
        return None

    inv_by_month = defaultdict(list)
    for inv in invoices:
        zoho_result = _check_in_zoho(inv)
        zoho_bid = zoho_result[0] if zoho_result else ""
        zoho_status = zoho_result[1] if zoho_result else ""
        inv_by_month[get_invoice_month(inv)].append({
            "vendor_name": inv.get("vendor_name", "") or "",
            "vendor_gstin": inv.get("vendor_gstin"),
            "amount": inv.get("amount"),
            "currency": inv.get("currency", "INR"),
            "date": inv.get("date", ""),
            "invoice_number": inv.get("invoice_number", ""),
            "file": inv.get("file", ""),
            "organized_path": inv.get("organized_path", ""),
            "amazon_entities": inv.get("amazon_entities"),
            "amazon_fc_code": inv.get("amazon_fc_code"),
            "in_zoho": bool(zoho_bid),
            "zoho_bill_id": zoho_bid,
            "zoho_bill_status": zoho_status,
        })

    # --- Build sorted month list (newest first) ---
    all_months = sorted(
        set(list(cc_by_month.keys()) + list(inv_by_month.keys())),
        key=lambda m: _parse_month_key(m),
        reverse=True,
    )

    months = []
    for mk in all_months:
        cc_list = sorted(cc_by_month.get(mk, []), key=lambda x: x.get("date", ""))
        inv_list = sorted(inv_by_month.get(mk, []), key=lambda x: x.get("date", ""))
        cc_total = sum(t["amount"] for t in cc_list if t.get("amount") and t["amount"] > 0)
        inv_total = sum(i["amount"] for i in inv_list if i.get("amount"))
        months.append({
            "month": mk,
            "cc_transactions": cc_list,
            "invoices": inv_list,
            "cc_count": len(cc_list),
            "inv_count": len(inv_list),
            "cc_total": round(cc_total, 2),
            "inv_total": round(inv_total, 2),
        })

    # Load Zoho vendor names for filter dropdown
    zoho_vendor_names = []
    vendors_cache_path = os.path.join(output_dir, "zoho_vendors_cache.json")
    if os.path.exists(vendors_cache_path):
        try:
            with open(vendors_cache_path, "r", encoding="utf-8") as f:
                zoho_vendor_names = sorted(set(
                    (v.get("contact_name") or "").strip()
                    for v in json.load(f)
                    if (v.get("contact_name") or "").strip()
                ), key=str.casefold)
        except Exception:
            pass

    return jsonify({
        "months": months,
        "summary": {
            "total_months": len(months),
            "total_cc": sum(m["cc_count"] for m in months),
            "total_invoices": sum(m["inv_count"] for m in months),
        },
        "zoho_vendors": zoho_vendor_names,
    })


def _auto_update_vendor_mappings(invoices_list, log_action):
    """Auto-discover new vendor mappings from parsed invoices + unmapped CC descriptions."""
    try:
        vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
        with open(vm_path, "r", encoding="utf-8") as f:
            vm_data = json.load(f)
        mappings = vm_data.get("mappings", {})

        def _nrm(s):
            return re.sub(r'[\s.\-,*()]+', '', s.lower())

        existing_values = set(mappings.values())  # clean vendor names
        existing_keys_norm = {_nrm(k) for k in mappings}

        # --- Step 1: Collect invoice vendor names, resolve canonical target ---
        inv_vendor_canonical = {}  # invoice vendor_name → canonical mapping target
        for inv in invoices_list:
            vn = (inv.get("vendor_name") or "").strip()
            if not vn or vn.lower() in {"unknown", "n/a", ""}:
                continue
            if vn in inv_vendor_canonical:
                continue
            if vn in existing_values:
                inv_vendor_canonical[vn] = vn
                continue
            # Check if loosely matches an existing mapping value
            vn_n = _nrm(vn)
            matched = None
            for ev in existing_values:
                ev_n = _nrm(ev)
                if ev_n and vn_n and (ev_n in vn_n or vn_n in ev_n):
                    matched = ev
                    break
            inv_vendor_canonical[vn] = matched or vn

        # --- Step 2: Build token lookup (vendor name fragments → canonical) ---
        # Combine existing mapping values + invoice vendor names
        all_canonical = set(existing_values) | set(inv_vendor_canonical.values())
        token_map = {}  # normalized token → canonical name

        # Full normalized vendor names
        for canon in all_canonical:
            cn = _nrm(canon)
            if cn and len(cn) >= 4:
                token_map[cn] = canon

        # First-word tokens — only add if UNIQUE to one vendor (skip ambiguous)
        first_word_vendors = {}  # first_word → set of vendor names
        for canon in all_canonical:
            words = [w for w in re.split(r'[\s,.\-*()]+', canon) if w]
            if words:
                fw = _nrm(words[0])
                if fw and len(fw) >= 5:
                    first_word_vendors.setdefault(fw, set()).add(canon)
        for fw, vendors in first_word_vendors.items():
            if len(vendors) == 1 and fw not in token_map:
                token_map[fw] = next(iter(vendors))

        # Existing mapping keys as tokens (these are manually curated, safe)
        for k, v in mappings.items():
            kn = _nrm(k)
            if kn and len(kn) >= 4:
                token_map[kn] = v

        sorted_tokens = sorted(token_map.keys(), key=len, reverse=True)

        # --- Step 3: Find unmapped CC descriptions & auto-map ---
        new_mappings = {}
        cc_path = os.path.join(PROJECT_ROOT, "output", "cc_transactions.json")
        if os.path.exists(cc_path):
            with open(cc_path, "r", encoding="utf-8") as f:
                cc_txns = json.load(f)

            map_lower = {k.lower(): v for k, v in mappings.items()}
            map_norm = {_nrm(k): v for k, v in mappings.items()}
            sk_low = sorted(map_lower.keys(), key=len, reverse=True)
            sk_nrm = sorted(map_norm.keys(), key=len, reverse=True)

            for t in cc_txns:
                desc = t.get("description", "").strip()
                if not desc or float(t.get("amount", 0)) <= 0:
                    continue
                dl = desc.lower()
                dn = _nrm(desc)

                # Already mapped? (replicate _resolve_vendor logic)
                if dl in map_lower or dn in map_norm:
                    continue
                found = False
                for key in sk_low:
                    if key and len(key) >= 4 and key in dl:
                        found = True
                        break
                if not found:
                    for key in sk_nrm:
                        if key and len(key) >= 4 and key in dn:
                            found = True
                            break
                if found:
                    continue

                # Try token matching (longest first for precision)
                for tok in sorted_tokens:
                    if tok in dn:
                        target = token_map[tok]
                        new_mappings[desc] = target
                        # Update local lookup so we don't double-add variants
                        map_lower[dl] = target
                        map_norm[dn] = target
                        break

        if new_mappings:
            mappings.update(new_mappings)
            vm_data["mappings"] = mappings
            with open(vm_path, "w", encoding="utf-8") as f:
                json.dump(vm_data, f, indent=4, ensure_ascii=False)
            log_action(f"Auto-mapping: {len(new_mappings)} new vendor mappings added:")
            for desc, target in sorted(new_mappings.items(), key=lambda x: x[1]):
                log_action(f"    '{desc}' → '{target}'")
        else:
            log_action("Auto-mapping: no new vendor mappings needed")
    except Exception as e:
        log_action(f"Auto-mapping failed: {e}", "WARNING")


def _parse_org_invoices_thread():
    """Background thread: extract invoice data from organized_invoices/."""
    from scripts.utils import log_action
    try:
        mod_02 = _import_script("02_extract_invoices.py")
        org_root = os.path.join(PROJECT_ROOT, "organized_invoices")
        output_path = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")

        log_action("=" * 50)
        log_action("Parse All Invoices from organized_invoices/")
        log_action("=" * 50)

        results = []
        total = 0
        for month_folder in sorted(os.listdir(org_root)):
            month_dir = os.path.join(org_root, month_folder)
            if not os.path.isdir(month_dir):
                continue
            pdfs = [f for f in os.listdir(month_dir) if f.lower().endswith((".pdf", ".eml"))]
            log_action(f"  {month_folder}: {len(pdfs)} files")
            for pdf_file in sorted(pdfs):
                pdf_path = os.path.join(month_dir, pdf_file)
                try:
                    inv = mod_02.extract_invoice(pdf_path, pdf_file)
                    if inv:
                        # Amazon India returns a list of invoices (one per page)
                        inv_list = inv if isinstance(inv, list) else [inv]
                        for item in inv_list:
                            item["organized_month"] = month_folder
                            item["organized_path"] = pdf_path
                            results.append(item)
                            total += 1
                            log_action(f"    {item.get('file', pdf_file)} -> {item.get('vendor_name', '?')}, {item.get('amount', '?')} {item.get('currency', '?')}")
                    else:
                        log_action(f"    {pdf_file} -> no data", "WARNING")
                except Exception as e:
                    log_action(f"    {pdf_file} -> FAILED: {e}", "WARNING")

        # Dedup by invoice_number (keep first occurrence)
        seen = {}
        deduped = []
        generic = {"payment", "original", "invoice", "receipt", "bill", "tax", "none", "n/a", ""}
        for inv in results:
            num = inv.get("invoice_number", "")
            if num and num.lower().strip() not in generic and num in seen:
                log_action(f"  Dedup: skipping {inv['file']} (same #{num} as {seen[num]})")
                continue
            if num and num.lower().strip() not in generic:
                seen[num] = inv["file"]
            deduped.append(inv)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(deduped, f, indent=2, ensure_ascii=False)

        log_action(f"Done: {len(deduped)} invoices extracted ({total} total, {total - len(deduped)} deduped)")

        # --- Auto-discover & update vendor mappings ---
        _auto_update_vendor_mappings(deduped, log_action)

    except Exception as e:
        log_action(f"Parse org invoices failed: {e}", "ERROR")
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


@app.route("/api/compare/parse-org-invoices", methods=["POST"])
def api_parse_org_invoices():
    """Start background extraction from organized_invoices/ folder."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True
        _state["current_step"] = "Parse Org Invoices"

    org_root = os.path.join(PROJECT_ROOT, "organized_invoices")
    if not os.path.isdir(org_root):
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None
        return jsonify({"error": "organized_invoices/ folder not found"}), 404

    t = threading.Thread(target=_parse_org_invoices_thread, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# --- Sync Zoho (bills + vendors cache) ---

def _sync_zoho_thread():
    """Background thread: fetch all bills + vendors from Zoho and save as local cache."""
    from scripts.utils import log_action, load_config, ZohoBooksAPI
    try:
        log_action("=" * 50)
        log_action("Sync Zoho: Fetching bills, vendors & CC accounts")
        log_action("=" * 50)

        config = load_config()
        api = ZohoBooksAPI(config)

        # --- Fetch ALL bills (paginated) ---
        all_bills = []
        page = 1
        while True:
            result = api.list_bills(page=page)
            bills_page = result.get("bills", [])
            if not bills_page:
                break
            for b in bills_page:
                all_bills.append({
                    "bill_id": b.get("bill_id"),
                    "bill_number": b.get("bill_number", ""),
                    "vendor_name": b.get("vendor_name", ""),
                    "vendor_id": b.get("vendor_id", ""),
                    "date": b.get("date", ""),
                    "total": b.get("total", 0),
                    "status": b.get("status", ""),
                })
            has_more = result.get("page_context", {}).get("has_more_page", False)
            if not has_more:
                break
            page += 1
            log_action(f"  Bills page {page}...")

        log_action(f"Fetched {len(all_bills)} bills from Zoho")

        # --- Fetch ALL vendors (paginated) ---
        all_vendors = []
        page = 1
        while True:
            result = api.list_all_vendors(page=page)
            contacts = result.get("contacts", [])
            if not contacts:
                break
            for v in contacts:
                all_vendors.append({
                    "contact_id": v.get("contact_id"),
                    "contact_name": v.get("contact_name", ""),
                    "company_name": v.get("company_name", ""),
                    "gst_no": v.get("gst_no", ""),
                    "gst_treatment": v.get("gst_treatment", ""),
                    "currency_code": v.get("currency_code", "INR"),
                })
            has_more = result.get("page_context", {}).get("has_more_page", False)
            if not has_more:
                break
            page += 1
            log_action(f"  Vendors page {page}...")

        log_action(f"Fetched {len(all_vendors)} vendors from Zoho")

        # --- Fetch CC bank accounts ---
        bank_accounts = api.list_bank_accounts()
        cc_accounts = [a for a in bank_accounts if a.get("account_type") == "credit_card"]
        log_action(f"Fetched {len(cc_accounts)} CC accounts from Zoho Banking")

        # --- Save caches ---
        os.makedirs(os.path.join(PROJECT_ROOT, "output"), exist_ok=True)
        bills_cache = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
        vendors_cache = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")

        with open(bills_cache, "w", encoding="utf-8") as f:
            json.dump(all_bills, f, indent=2, ensure_ascii=False)
        with open(vendors_cache, "w", encoding="utf-8") as f:
            json.dump(all_vendors, f, indent=2, ensure_ascii=False)

        cc_cache = os.path.join(PROJECT_ROOT, "output", "zoho_cc_accounts_cache.json")
        with open(cc_cache, "w", encoding="utf-8") as f:
            json.dump(cc_accounts, f, indent=2, ensure_ascii=False)

        # --- Fetch CC transactions (uncategorized) from each configured card ---
        cards = config.get("credit_cards", [])
        all_cc_txns = []
        for card in cards:
            account_id = card.get("zoho_account_id")
            if not account_id:
                log_action(f"  Skipping card '{card.get('name')}' - no zoho_account_id")
                continue
            card_name = card.get("name", "")
            page = 1
            card_txns = 0
            while True:
                result = api.list_uncategorized(account_id, page=page)
                for t in result.get("banktransactions", []):
                    amount = float(t.get("amount") or 0)
                    desc = t.get("description", "") or t.get("payee", "")
                    entry = {
                        "date": t.get("date", ""),
                        "description": desc,
                        "amount": amount,
                        "card_name": card_name,
                        "zoho_account_id": account_id,
                        "transaction_id": t.get("transaction_id", ""),
                    }
                    # Parse forex from description:
                    #   '[USD 359.90]' or '(12.74 USD)' or '(USD 12.74)'
                    import re as _re
                    fx_m = _re.search(r'\[([A-Z]{3})\s+([\d,.]+)\]', desc)
                    if not fx_m:
                        # Try '(12.74 USD)' format (amount before currency)
                        fx_m2 = _re.search(r'\(([\d,.]+)\s+([A-Z]{3})\)', desc)
                        if fx_m2:
                            try:
                                entry["forex_amount"] = float(fx_m2.group(1).replace(',', ''))
                                entry["forex_currency"] = fx_m2.group(2)
                            except ValueError:
                                pass
                        else:
                            # Try '(USD 12.74)' format
                            fx_m = _re.search(r'\(([A-Z]{3})\s+([\d,.]+)\)', desc)
                    if fx_m:
                        try:
                            entry["forex_amount"] = float(fx_m.group(2).replace(',', ''))
                            entry["forex_currency"] = fx_m.group(1)
                        except ValueError:
                            pass
                    all_cc_txns.append(entry)
                    card_txns += 1
                if not result.get("page_context", {}).get("has_more_page", False):
                    break
                page += 1
            log_action(f"  {card_name}: {card_txns} uncategorized transactions")

        cc_txns_cache = os.path.join(PROJECT_ROOT, "output", "zoho_cc_transactions_cache.json")
        with open(cc_txns_cache, "w", encoding="utf-8") as f:
            json.dump(all_cc_txns, f, indent=2, ensure_ascii=False)

        log_action(f"Saved: {len(all_bills)} bills -> zoho_bills_cache.json")
        log_action(f"Saved: {len(all_vendors)} vendors -> zoho_vendors_cache.json")
        log_action(f"Saved: {len(cc_accounts)} CC accounts -> zoho_cc_accounts_cache.json")
        log_action(f"Saved: {len(all_cc_txns)} CC transactions -> zoho_cc_transactions_cache.json")

        # Resolve/update CC account IDs in config
        from scripts.utils import resolve_account_ids
        cards = config.get("credit_cards", [])
        if cards:
            resolve_account_ids(api, cards)

        log_action("Sync Zoho complete!")

    except Exception as e:
        log_action(f"Sync Zoho failed: {e}", "ERROR")
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_step"] = None


@app.route("/api/zoho/sync", methods=["POST"])
def api_zoho_sync():
    """Start background sync of all bills + vendors from Zoho."""
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "A step is already running", "current": _state["current_step"]}), 409
        _state["running"] = True
        _state["current_step"] = "Sync Zoho"

    t = threading.Thread(target=_sync_zoho_thread, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# --- Match Preview (bill dedup analysis) ---

def _normalize_bill_number(num):
    """Normalize bill number for fuzzy matching: strip prefixes, lowercase, remove non-alphanumeric."""
    import re
    if not num:
        return ""
    s = num.strip()
    # Strip common prefixes (INV-, AWS-, vendor-name prefixes like "ABC-")
    s = re.sub(r'^(INV[-_]?)', '', s, flags=re.IGNORECASE)
    # Strip any remaining leading alpha prefix followed by separator (e.g. AWS-1234 -> 1234)
    s = re.sub(r'^[A-Za-z]+[-_]', '', s)
    # Lowercase and remove non-alphanumeric
    s = re.sub(r'[^a-z0-9]', '', s.lower())
    return s


@app.route("/api/zoho-vendors")
def api_zoho_vendors():
    """Return Zoho vendor list from cache for the UI dropdown."""
    cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")
    if not os.path.exists(cache_path):
        return jsonify([])
    with open(cache_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return jsonify([])
    vendors = json.loads(content)
    return jsonify([{"contact_id": v.get("contact_id", ""), "contact_name": v.get("contact_name", ""), "currency_code": v.get("currency_code", "INR")} for v in vendors])


@app.route("/api/vendor-overrides")
def api_vendor_overrides_get():
    """Return saved vendor overrides."""
    path = os.path.join(PROJECT_ROOT, "output", "vendor_overrides.json")
    if not os.path.exists(path):
        return jsonify({})
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return jsonify({})
    return jsonify(json.loads(content))


@app.route("/api/vendor-overrides", methods=["POST"])
def api_vendor_overrides_post():
    """Merge and save vendor overrides."""
    path = os.path.join(PROJECT_ROOT, "output", "vendor_overrides.json")
    existing = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            existing = json.loads(content)
    new_overrides = request.json.get("overrides", {})
    existing.update(new_overrides)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    return jsonify({"ok": True, "count": len(existing)})


@app.route("/api/bills/match-preview", methods=["POST"])
def api_bills_match_preview():
    """Classify each extracted invoice as skip/new_bill/new_vendor_bill against Zoho cache."""
    from scripts.utils import log_action

    # Load extracted invoices (prefer compare_invoices, fallback to extracted_invoices)
    compare_path = os.path.join(PROJECT_ROOT, "output", "compare_invoices.json")
    extract_path = os.path.join(PROJECT_ROOT, "output", "extracted_invoices.json")
    invoices_path = compare_path if os.path.exists(compare_path) else extract_path
    if not os.path.exists(invoices_path):
        return jsonify({"error": "No extracted invoices found. Run Extract Data first."}), 404

    with open(invoices_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return jsonify({"error": "Invoice file is empty. Run Extract Data first."}), 404
    invoices = json.loads(content)

    # Load Zoho caches
    bills_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_bills_cache.json")
    vendors_cache_path = os.path.join(PROJECT_ROOT, "output", "zoho_vendors_cache.json")
    if not os.path.exists(bills_cache_path) or not os.path.exists(vendors_cache_path):
        return jsonify({"error": "Zoho cache not found. Click Sync Zoho first."}), 404

    with open(bills_cache_path, "r", encoding="utf-8") as f:
        zoho_bills = json.load(f)
    with open(vendors_cache_path, "r", encoding="utf-8") as f:
        zoho_vendors = json.load(f)

    # Build indices
    # Exact bill number -> bill info
    bills_exact = {}
    # Normalized bill number -> bill info
    bills_norm = {}
    # (vendor_name_lower, date) -> bill info
    bills_vendor_date = {}
    # (vendor_name_lower, date) -> list of (amount, bill_info) for duplicate detection
    from collections import defaultdict
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

    # Vendor name -> vendor info (lowercase)
    vendor_name_map = {}
    import re
    _norm_v = lambda s: re.sub(r'[\s.\-,*()]+', '', s.lower()) if s else ""
    vendor_norm_map = {}
    for v in zoho_vendors:
        cn = (v.get("contact_name") or "").strip()
        if cn:
            vendor_name_map[cn.lower()] = v
            vendor_norm_map[_norm_v(cn)] = v
        comp = (v.get("company_name") or "").strip()
        if comp:
            vendor_name_map[comp.lower()] = v
            vendor_norm_map[_norm_v(comp)] = v

    # GSTIN -> vendor info
    vendor_gstin_map = {}
    for v in zoho_vendors:
        gst_no = (v.get("gst_no") or "").strip()
        if gst_no:
            vendor_gstin_map[gst_no] = v

    # Also load vendor_mappings for resolving names
    vm_path = os.path.join(PROJECT_ROOT, "config", "vendor_mappings.json")
    vendor_mappings_data = {}
    if os.path.exists(vm_path):
        with open(vm_path, "r", encoding="utf-8") as f:
            vendor_mappings_data = json.load(f).get("mappings", {})

    _GENERIC_NUMBERS = {"payment", "original", "invoice", "bill", "tax", "none", "n/a", ""}

    # Load created_bills.json to mark already-created bills
    created_bills_path = os.path.join(PROJECT_ROOT, "output", "created_bills.json")
    created_bills_map = {}  # file -> entry
    if os.path.exists(created_bills_path):
        try:
            with open(created_bills_path, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    if entry.get("status") == "created" and entry.get("bill_id"):
                        created_bills_map[entry["file"]] = entry
        except Exception:
            pass

    # Classify each invoice
    preview = []
    for inv in invoices:
        raw_inv_number = inv.get("invoice_number", "")
        vendor_name = inv.get("vendor_name", "")
        amount = inv.get("amount", 0)
        inv_date = inv.get("date", "")
        has_reliable = bool(raw_inv_number and raw_inv_number.lower().strip() not in _GENERIC_NUMBERS)
        # GitHub receipt files use generic receipt numbers — fall back to filename
        fname = inv.get("file", "")
        if has_reliable and fname.lower().startswith("github") and "receipt" in fname.lower():
            has_reliable = False

        inv_number = raw_inv_number if has_reliable else re.sub(r'\.(pdf|eml)$', '', fname, flags=re.IGNORECASE)
        bill_number = inv_number
        bill_number_legacy = f"INV-{inv_number}"  # for matching old bills

        entry = {
            "file": inv.get("file", ""),
            "invoice_number": raw_inv_number,
            "vendor_name": vendor_name,
            "amount": amount,
            "currency": inv.get("currency", "INR"),
            "date": inv_date,
            "organized_month": inv.get("organized_month", ""),
        }

        # --- Check if already created via Step 3 ---
        fname = inv.get("file", "")
        if fname in created_bills_map:
            cb = created_bills_map[fname]
            entry["action"] = "skip"
            entry["matched_bill_id"] = cb.get("bill_id", "")
            entry["matched_vendor_name"] = cb.get("vendor_name", "")
            entry["matched_vendor_id"] = cb.get("vendor_id", "")
            entry["match_type"] = "created_bills"
            entry["matched_bill"] = f"Created: {cb.get('vendor_name', '')}"
            preview.append(entry)
            continue

        # --- Check if bill already exists ---
        matched_bill = None
        match_type = None

        # 1. Exact bill number match (check both new format and legacy INV- prefix)
        if bill_number in bills_exact:
            matched_bill = bills_exact[bill_number]
            match_type = "exact"
        elif bill_number_legacy in bills_exact:
            matched_bill = bills_exact[bill_number_legacy]
            match_type = "exact"
        # 2. Normalized match (e.g., INV-02148314-0004 vs 02148314-0004)
        elif has_reliable:
            norm = _normalize_bill_number(inv_number)
            if norm and norm in bills_norm:
                matched_bill = bills_norm[norm]
                match_type = "normalized"
        # 3. Vendor+date fallback (only if no reliable invoice number)
        if not matched_bill and not has_reliable and vendor_name and inv_date:
            vn_lower = vendor_name.strip().lower()
            # Also try mapped vendor name
            names_to_check = {vn_lower}
            mapped_name = vendor_mappings_data.get(vendor_name) or vendor_mappings_data.get(vn_lower)
            if mapped_name:
                names_to_check.add(mapped_name.strip().lower())
            for n in names_to_check:
                if (n, inv_date) in bills_vendor_date:
                    matched_bill = bills_vendor_date[(n, inv_date)]
                    match_type = "vendor_date"
                    break

        if matched_bill:
            entry["action"] = "skip"
            entry["matched_bill"] = matched_bill.get("bill_number", "")
            entry["matched_bill_id"] = matched_bill.get("bill_id", "")
            entry["matched_vendor_name"] = matched_bill.get("vendor_name", "")
            entry["matched_vendor_id"] = matched_bill.get("vendor_id", "")
            entry["match_type"] = match_type
            preview.append(entry)
            continue

        # --- Check if vendor exists ---
        vendor_found = None
        vendor_match_method = None
        inv_gstin = (inv.get("vendor_gstin") or "").strip()

        # Priority 1: GSTIN match (most reliable)
        if inv_gstin and inv_gstin in vendor_gstin_map:
            vendor_found = vendor_gstin_map[inv_gstin]
            vendor_match_method = "gstin"

        # Priority 2: Name-based matching
        if not vendor_found and vendor_name:
            vn_lower = vendor_name.strip().lower()
            vn_norm = _norm_v(vendor_name)
            # Direct vendor match
            if vn_lower in vendor_name_map:
                vendor_found = vendor_name_map[vn_lower]
                vendor_match_method = "name"
            elif vn_norm in vendor_norm_map:
                vendor_found = vendor_norm_map[vn_norm]
                vendor_match_method = "name"
            else:
                # Try via vendor_mappings
                mapped_name = vendor_mappings_data.get(vendor_name) or vendor_mappings_data.get(vn_lower)
                if mapped_name:
                    mn_lower = mapped_name.strip().lower()
                    mn_norm = _norm_v(mapped_name)
                    if mn_lower in vendor_name_map:
                        vendor_found = vendor_name_map[mn_lower]
                        vendor_match_method = "name"
                    elif mn_norm in vendor_norm_map:
                        vendor_found = vendor_norm_map[mn_norm]
                        vendor_match_method = "name"
            # Fuzzy match against Zoho vendor names (catches "Microsoft" ≈ "Microsoft Pvt Ltd")
            if not vendor_found:
                from thefuzz import fuzz
                from scripts.utils import strip_vendor_stop_words
                best_score, best_vendor = 0, None
                for vkey, vinfo in vendor_name_map.items():
                    score = fuzz.token_set_ratio(
                        strip_vendor_stop_words(vn_lower),
                        strip_vendor_stop_words(vkey),
                    )
                    if score > best_score:
                        best_score, best_vendor = score, vinfo
                if best_score >= 85:
                    vendor_found = best_vendor
                    vendor_match_method = "fuzzy"

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

        preview.append(entry)

    # Save preview for reference
    preview_path = os.path.join(PROJECT_ROOT, "output", "bill_match_preview.json")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    with open(preview_path, "w", encoding="utf-8") as f:
        json.dump(preview, f, indent=2, ensure_ascii=False)

    # Summary counts
    skip_count = sum(1 for p in preview if p["action"] == "skip")
    dup_count = sum(1 for p in preview if p["action"] == "possible_duplicate")
    new_bill_count = sum(1 for p in preview if p["action"] == "new_bill")
    new_vendor_bill_count = sum(1 for p in preview if p["action"] == "new_vendor_bill")

    log_action(f"Match preview: {skip_count} skip, {dup_count} possible duplicates, {new_bill_count} new bills, {new_vendor_bill_count} new vendor+bill")

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


@app.route("/api/compare/save-categorize", methods=["POST"])
def api_save_categorize():
    """Save categorize check results to output/categorize_<month>.json."""
    data = request.json or {}
    month = data.get("month", "unknown")
    rows = data.get("rows", [])
    summary = data.get("summary", {})

    # "Jan 2026" -> "categorize_jan_2026.json"
    safe_name = month.lower().replace(" ", "_")
    filename = f"categorize_{safe_name}.json"
    output_path = os.path.join(PROJECT_ROOT, "output", filename)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "month": month,
            "rows": rows,
            "summary": summary,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, f, indent=2, ensure_ascii=False)

    return jsonify({"status": "ok", "month": month, "path": output_path})


@app.route("/api/compare/categorize-overall")
def api_categorize_overall():
    """Aggregate counts from all saved categorize_*.json files."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    totals = {"exact": 0, "close": 0, "cross_exact": 0, "cross_close": 0,
              "no_invoice": 0, "unmapped": 0, "no_cc": 0, "total": 0, "months_done": 0}
    months_detail = []
    for fn in sorted(glob.glob(os.path.join(output_dir, "categorize_*.json"))):
        try:
            with open(fn, encoding="utf-8") as f:
                data = json.load(f)
            counts = {"exact": 0, "close": 0, "cross_exact": 0, "cross_close": 0,
                       "no_invoice": 0, "unmapped": 0, "no_cc": 0}
            for row in data.get("rows", []):
                s = row.get("status", "")
                if s in counts:
                    counts[s] += 1
            month_total = sum(counts.values())
            for k in counts:
                totals[k] += counts[k]
            totals["total"] += month_total
            totals["months_done"] += 1
            months_detail.append({"month": data.get("month", ""), "counts": counts, "total": month_total})
        except Exception:
            continue
    return jsonify({"totals": totals, "months": months_detail})


# --- HTML Dashboard (embedded) ---

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CC Statement Automation</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #232733;
    --border: #2e3345;
    --text: #e1e4ed;
    --text-dim: #8b90a0;
    --accent: #6c8cff;
    --accent-hover: #8ba4ff;
    --green: #4ade80;
    --red: #f87171;
    --orange: #fb923c;
    --yellow: #facc15;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    overflow: hidden;
  }
  .container { display: flex; flex-direction: column; height: 100vh; padding: 16px 20px; }

  /* Header */
  .header {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 16px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .header-left { justify-self: start; }
  .header-center { justify-self: center; display: flex; align-items: center; gap: 10px; }
  .header-right { justify-self: end; display: flex; align-items: center; gap: 14px; }
  .header h1 {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.5px;
  }
  .header h1 span { color: var(--accent); }
  .status-badge {
    padding: 5px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
  }
  .header-sync-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    background: rgba(108,140,255,0.08);
    border: 1.5px dashed var(--accent);
    border-radius: 20px;
    color: var(--accent);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
  }
  .header-sync-btn:hover:not(:disabled) { background: rgba(108,140,255,0.18); }
  .header-sync-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .header-sync-btn .hdr-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    border-radius: 4px;
    background: var(--accent);
    color: #fff;
    font-size: 10px;
    font-weight: 700;
  }
  .header-summary {
    display: flex;
    gap: 14px;
    align-items: center;
    padding: 5px 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
  }
  .header-summary-item {
    font-size: 11px;
    color: var(--text-dim);
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
  }
  .header-summary-item strong {
    color: var(--text);
    font-size: 14px;
    font-weight: 600;
  }
  .status-idle { background: var(--surface2); color: var(--text-dim); }
  .status-running { background: rgba(108,140,255,0.15); color: var(--accent); animation: pulse 2s infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }

  /* Main two-column layout */
  .main-layout {
    display: flex;
    gap: 16px;
    flex: 1;
    min-height: 0;
  }

  /* Left panel — phases */
  .left-panel {
    width: 20%;
    min-width: 250px;
    display: flex;
    flex-direction: column;
    gap: 0;
    overflow: visible;
    flex-shrink: 0;
  }
  .left-panel-scroll {
    flex: 1;
    overflow-y: auto;
    overflow-x: visible;
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: calc(100vh - 60px);
    padding-right: 4px;
  }

  /* Right panel — logs (86%) */
  .right-panel {
    width: 86%;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  /* Phase sections */
  .phase {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
  }
  .phase-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .step-grid {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* Step buttons */
  .step-btn {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    min-height: 38px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
    width: 100%;
    box-sizing: border-box;
  }
  .step-btn:hover:not(:disabled) {
    border-color: var(--accent);
    background: rgba(108,140,255,0.08);
  }
  .step-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .step-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 5px;
    background: var(--bg);
    font-size: 11px;
    font-weight: 700;
    flex-shrink: 0;
  }
  .step-indicator {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-left: auto;
    flex-shrink: 0;
  }
  .ind-idle { background: var(--border); }
  .ind-running { background: var(--accent); animation: pulse 1s infinite; }
  .ind-success { background: var(--green); }
  .ind-error { background: var(--red); }

  /* Step result tooltip */
  .step-btn .step-msg {
    display: none;
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 11px;
    white-space: nowrap;
    z-index: 10;
    color: var(--text-dim);
    pointer-events: none;
  }
  .step-btn { position: relative; }
  .step-btn:hover .step-msg { display: block; }

  /* Info icon + tooltip */
  .info-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--border);
    color: var(--text-dim);
    font-size: 10px;
    font-weight: 700;
    font-style: italic;
    cursor: help;
    flex-shrink: 0;
    position: relative;
    margin-left: auto;
  }
  .info-btn:hover { background: var(--accent); color: #fff; }
  .info-tooltip {
    display: none;
    position: fixed;
    background: var(--surface2);
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 11.5px;
    font-style: normal;
    font-weight: 400;
    line-height: 1.5;
    white-space: normal;
    width: 280px;
    z-index: 9999;
    color: var(--text);
    pointer-events: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  }

  /* Action row */
  .action-row {
    display: flex;
    gap: 6px;
  }
  .btn-primary {
    padding: 8px 14px;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    flex: 1;
  }
  .btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-danger {
    padding: 8px 14px;
    background: transparent;
    color: var(--red);
    border: 1px solid rgba(248,113,113,0.3);
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    flex: 1;
  }
  .btn-danger:hover:not(:disabled) { background: rgba(248,113,113,0.1); }
  .btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Summary bar */
  .summary-bar {
    display: flex;
    gap: 12px;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    flex-wrap: wrap;
  }
  .summary-item {
    font-size: 11px;
    color: var(--text-dim);
  }
  .summary-item strong {
    color: var(--text);
    font-size: 15px;
    font-weight: 600;
    margin-right: 2px;
  }

  /* Log panel */
  .log-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .log-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
    flex-shrink: 0;
  }
  .log-header button {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .log-header button:hover { color: var(--text); }
  #logBox {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 12.5px;
    line-height: 1.7;
  }
  #logBox::-webkit-scrollbar { width: 6px; }
  #logBox::-webkit-scrollbar-track { background: transparent; }
  #logBox::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-line-INFO { color: var(--text-dim); }
  .log-line-WARNING { color: var(--orange); }
  .log-line-ERROR { color: var(--red); }

  /* Confirmation modal */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    backdrop-filter: blur(4px);
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .modal-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 28px 32px;
    min-width: 360px;
    max-width: 440px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  }
  .modal-title {
    font-size: 17px;
    font-weight: 700;
    margin-bottom: 10px;
  }
  .modal-msg {
    font-size: 13px;
    color: var(--text-dim);
    line-height: 1.6;
    margin-bottom: 24px;
  }
  .modal-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
  .modal-btn {
    padding: 9px 20px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    transition: all 0.15s;
  }
  .modal-btn-cancel {
    background: var(--surface2);
    color: var(--text-dim);
    border: 1px solid var(--border);
  }
  .modal-btn-cancel:hover { color: var(--text); background: var(--bg); }
  .modal-btn-confirm {
    background: var(--accent);
    color: #fff;
  }
  .modal-btn-confirm:hover { background: var(--accent-hover); }
  .modal-btn-confirm.danger {
    background: var(--red);
  }
  .modal-btn-confirm.danger:hover { background: #ef4444; }

  /* Import Picker — expandable card rows */
  .ip-card { border-bottom: 1px solid var(--border); }
  .ip-card-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 4px; cursor: pointer;
    font-size: 13px;
  }
  .ip-card-header:hover { background: var(--surface2); }
  .ip-caret {
    display: inline-block; width: 12px; color: var(--text-dim);
    transition: transform 0.15s;
  }
  .ip-card.open .ip-caret { transform: rotate(90deg); }
  .ip-count {
    color: var(--text-dim); margin-left: auto; font-size: 11px;
  }
  .ip-txns {
    display: none;
    padding: 4px 0 10px 28px;
    font-size: 12px;
    max-height: 260px; overflow-y: auto;
  }
  .ip-card.open .ip-txns { display: block; }
  .ip-txn-row {
    display: grid; grid-template-columns: 90px 1fr 100px;
    gap: 8px; padding: 4px 0;
    border-bottom: 1px dashed var(--border);
    color: var(--text-dim);
  }
  .ip-txn-row:last-child { border-bottom: none; }
  .ip-txn-amt { text-align: right; font-variant-numeric: tabular-nums; color: var(--text); }
  .ip-txn-forex { color: var(--text-dim); font-size: 11px; padding-left: 28px; }

  /* Bill Picker — Filter Bar */
  .bill-filter-bar {
    display: flex; flex-wrap: wrap; gap: 8px; padding: 8px 0 10px; align-items: flex-end;
  }
  .bill-filter-group { display: flex; flex-direction: column; gap: 2px; }
  .bill-filter-group label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .bill-filter-group select,
  .bill-filter-group input {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 12px; padding: 4px 8px; min-width: 90px;
  }
  .bill-filter-group input[type="number"] { width: 80px; }

  /* Checkbox Dropdown */
  .cb-dropdown { position: relative; display: inline-block; }
  .cb-dropdown-btn {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 12px; padding: 4px 10px; cursor: pointer;
    display: flex; align-items: center; gap: 6px; white-space: nowrap; min-width: 90px;
  }
  .cb-dropdown-btn:hover { border-color: var(--text-dim); }
  .cb-dropdown-btn .cb-badge {
    background: var(--accent); color: #fff; font-size: 10px; font-weight: 700;
    border-radius: 8px; padding: 0 5px; min-width: 16px; text-align: center; line-height: 16px;
  }
  .cb-dropdown-panel {
    display: none; position: absolute; top: 100%; left: 0; z-index: 20;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3); min-width: 180px; max-height: 260px;
    flex-direction: column; margin-top: 4px;
  }
  .cb-dropdown-panel.open { display: flex; }
  .cb-dropdown-actions {
    display: flex; gap: 8px; padding: 6px 10px; border-bottom: 1px solid var(--border);
    font-size: 11px;
  }
  .cb-dropdown-actions a {
    color: var(--accent); cursor: pointer; text-decoration: none;
  }
  .cb-dropdown-actions a:hover { text-decoration: underline; }
  .cb-dropdown-search {
    margin: 6px 8px 4px; padding: 4px 8px; font-size: 12px; border: 1px solid var(--border);
    border-radius: 4px; background: var(--bg); color: var(--text); outline: none;
  }
  .cb-dropdown-search:focus { border-color: var(--accent); }
  .cb-dropdown-list { overflow-y: auto; padding: 4px 0; flex: 1; }
  .cb-dropdown-list label {
    display: flex; align-items: center; gap: 6px; padding: 3px 10px; font-size: 12px;
    cursor: pointer; white-space: nowrap;
  }
  .cb-dropdown-list label:hover { background: var(--surface2); }
  .cb-dropdown-list input[type="checkbox"] { accent-color: var(--accent); }

  /* Mapping Bar */
  .bill-mapping-bar {
    display: flex; align-items: center; gap: 10px; padding: 8px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 8px; font-size: 13px;
  }
  .bill-mapping-bar label { color: var(--text-dim); white-space: nowrap; font-size: 12px; }
  .bill-mapping-bar .modal-btn { padding: 5px 14px; font-size: 12px; }

  /* Searchable Dropdown */
  .search-dropdown { position: relative; flex: 1; max-width: 300px; }
  .search-dropdown input {
    width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 12px; padding: 5px 10px; box-sizing: border-box;
  }
  .search-dropdown-list {
    display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 20;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3); max-height: 200px; overflow-y: auto;
    margin-top: 4px;
  }
  .search-dropdown-list.open { display: block; }
  .sd-item {
    padding: 5px 10px; font-size: 12px; cursor: pointer; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .sd-item:hover { background: var(--surface2); }
  .sd-item.selected { background: var(--accent); color: #fff; }
  .bill-filter-clear {
    padding: 4px 12px; border-radius: 6px; font-size: 11px; font-weight: 600;
    border: 1px solid var(--border); background: transparent; color: var(--text-dim);
    cursor: pointer; align-self: flex-end;
  }
  .bill-filter-clear:hover { color: var(--text); border-color: var(--text-dim); }

  /* Bill Picker — Table */
  .bill-table-wrap { flex: 1; overflow-y: auto; min-height: 0; }
  .bill-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .bill-table thead { position: sticky; top: 0; z-index: 2; background: var(--surface); }
  .bill-table th {
    padding: 6px 8px; text-align: left; font-size: 11px; font-weight: 600;
    color: var(--text-dim); border-bottom: 1px solid var(--border); cursor: pointer;
    user-select: none; white-space: nowrap;
  }
  .bill-table th .sort-arrow { font-size: 10px; margin-left: 2px; opacity: 0.3; }
  .bill-table th.sorted .sort-arrow { opacity: 1; color: var(--accent); }
  .bill-table td { padding: 5px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }
  .bill-table tr.row-skip { opacity: 0.45; }
  .bill-table .col-checkbox { width: 32px; text-align: center; }
  .bill-table .col-amount { text-align: right; font-family: monospace; font-size: 11px; white-space: nowrap; }
  .bill-table .col-action { width: 70px; text-align: center; }
  .bill-table .vendor-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Bill Picker — Status & Action */
  .bill-status-badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 10px; font-weight: 600; text-transform: uppercase;
  }
  .bill-status-badge.pending { background: rgba(251,191,36,0.15); color: #fbbf24; }
  .bill-status-badge.created { background: rgba(52,211,153,0.15); color: #34d399; }
  .bill-create-btn {
    padding: 3px 10px; border-radius: 6px; font-size: 11px; font-weight: 600;
    border: 1px solid var(--accent); background: transparent; color: var(--accent);
    cursor: pointer; white-space: nowrap;
  }
  .bill-create-btn:hover { background: var(--accent); color: #fff; }

  /* Drag-and-drop manual match */
  tr.pay-drag-source { cursor: grab; }
  tr.pay-drag-source:active { cursor: grabbing; }
  tr.pay-dragging { opacity: 0.4; }
  tr.pay-drop-target.drag-over { outline: 2px dashed var(--accent); background: rgba(108,140,255,0.12) !important; }
  .drag-hint { display: inline-block; font-size: 8px; color: var(--accent); margin-left: 6px; opacity: 0.7; }
  .drop-preview-banner {
    background: rgba(108,140,255,0.15); border: 1px solid var(--accent); border-radius: 6px;
    padding: 8px 14px; margin: 0; font-size: 11px; line-height: 1.6;
    animation: dropFlash 0.4s ease-out;
  }
  .drop-preview-banner .dp-label { font-weight: 700; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
  .drop-preview-banner .dp-cc { color: var(--accent); }
  .drop-preview-banner .dp-bill { color: var(--yellow); }
  .drop-preview-banner .dp-diff { color: var(--green); font-weight: 600; }
  @keyframes dropFlash {
    0% { background: rgba(108,140,255,0.35); transform: scale(1.01); }
    100% { background: rgba(108,140,255,0.15); transform: scale(1); }
  }

  /* Bill Picker — Vertical Layout */
  .bill-picker-layout { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  .bill-picker-left { display: flex; flex-direction: column; min-height: 0; flex: 1; }
  .bill-picker-right {
    display: flex; flex-direction: row; flex-wrap: wrap; align-items: center; gap: 12px;
    padding: 10px 0; border-top: 1px solid var(--border); margin-top: 8px;
  }
  .bill-selected-count { font-size: 13px; font-weight: 600; padding: 6px 0; color: var(--accent); }

  /* Bill Picker — Bottom Bar Summary */
  .bill-summary-stat {
    display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-dim);
  }
  .bill-summary-stat .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .bill-summary-stat .count { font-weight: 700; font-family: monospace; color: var(--text); }
  .bill-summary-actions { margin-left: auto; display: flex; gap: 8px; }

  /* Zoho Vendor Column */
  .col-zoho-vendor { max-width: 220px; white-space: nowrap; position: relative; }
  .zoho-vendor-display { display: flex; align-items: center; gap: 4px; }
  .zoho-vendor-display .vendor-text { overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }
  .zoho-vendor-edit-btn {
    flex-shrink: 0; border: none; background: none; color: var(--text-dim); cursor: pointer;
    font-size: 11px; padding: 1px 4px; border-radius: 4px; opacity: 0.5; line-height: 1;
  }
  .zoho-vendor-edit-btn:hover { opacity: 1; background: var(--surface2); color: var(--accent); }
  .row-vendor-dropdown {
    position: absolute; top: 100%; left: 0; z-index: 30; width: 220px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4); display: none;
  }
  .row-vendor-dropdown.open { display: block; }
  .row-vendor-dropdown input {
    width: 100%; background: var(--bg); border: none; border-bottom: 1px solid var(--border);
    color: var(--text); font-size: 12px; padding: 6px 8px; box-sizing: border-box;
    border-radius: 8px 8px 0 0;
  }
  .row-vendor-dropdown .rvd-list { max-height: 180px; overflow-y: auto; }

  /* Review badge */
  .review-badge {
    background: var(--accent) !important;
    color: #fff !important;
    font-size: 10px !important;
  }
  .review-btn {
    border-style: dashed !important;
    border-color: var(--accent) !important;
    opacity: 0.85;
  }
  .review-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }

  /* Review panel */
  .review-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .review-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
    flex-shrink: 0;
  }
  .review-close-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .review-close-btn:hover { color: var(--red); border-color: var(--red); }
  .review-create-btn {
    background: rgba(108,140,255,0.1);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .review-create-btn:hover { background: rgba(108,140,255,0.2); }
  .review-body {
    flex: 1;
    overflow: auto;
    padding: 12px 16px;
  }
  .review-loading {
    text-align: center;
    color: var(--text-dim);
    padding: 40px 0;
    font-size: 13px;
  }
  .review-table {
    min-width: 900px;
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .review-table th {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-dim);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    white-space: nowrap;
  }
  .review-table td {
    padding: 8px 10px;
    border-bottom: 1px solid rgba(46,51,69,0.5);
    vertical-align: middle;
    white-space: nowrap;
  }
  .review-table select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 5px 8px;
    font-size: 12px;
    width: 100%;
    max-width: 220px;
  }
  .review-table select:focus { border-color: var(--accent); outline: none; }
  .review-save-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
  }
  .review-save-btn:hover { border-color: var(--accent); color: var(--accent); }
  .review-save-btn.saved {
    border-color: var(--green);
    color: var(--green);
  }
  .review-save-btn.save-error {
    border-color: var(--red);
    color: var(--red);
  }
  .review-save-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .vendor-group-header td {
    background: var(--surface2);
    padding: 10px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 13px;
    color: var(--accent);
  }
  .vendor-group-header .vendor-bulk-row {
    display: flex;
    align-items: center;
    gap: 10px;
    justify-content: space-between;
  }
  .vendor-group-header .vendor-name-label {
    flex-shrink: 0;
    min-width: 120px;
  }
  .vendor-group-header .vendor-bill-count {
    font-size: 11px;
    color: var(--text-dim);
    font-weight: 400;
  }
  .vendor-bulk-select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 5px 8px;
    font-size: 12px;
    min-width: 180px;
  }
  .vendor-bulk-select:focus { border-color: var(--accent); outline: none; }
  .apply-all-btn {
    background: rgba(108,140,255,0.1);
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 5px 14px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
    white-space: nowrap;
    font-weight: 600;
  }
  .apply-all-btn:hover { background: rgba(108,140,255,0.25); }
  .apply-all-btn.saved { border-color: var(--green); color: var(--green); }
  .apply-all-btn.save-error { border-color: var(--red); color: var(--red); }
  .apply-all-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Match Status panel */
  .match-summary {
    display: flex;
    gap: 16px;
    margin-bottom: 16px;
  }
  .match-stat {
    padding: 12px 20px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    text-align: center;
    flex: 1;
  }
  .match-stat .stat-value {
    font-size: 28px;
    font-weight: 700;
  }
  .match-stat .stat-label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 2px;
  }
  .stat-matched .stat-value { color: var(--green); }
  .stat-unmatched .stat-value { color: var(--orange); }
  .stat-total .stat-value { color: var(--accent); }
  .match-tabs {
    display: flex;
    gap: 0;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .match-tab {
    padding: 8px 20px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--text-dim);
    border-bottom: 2px solid transparent;
    transition: all 0.15s;
  }
  .match-tab:hover { color: var(--text); }
  .match-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .match-tab.tab-matched.active { color: var(--green); border-bottom-color: var(--green); }
  .match-tab.tab-unmatched.active { color: var(--orange); border-bottom-color: var(--orange); }
  .match-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .match-table th {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-dim);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
    background: var(--surface2);
    z-index: 1;
  }
  .match-table td {
    padding: 8px 10px;
    border-bottom: 1px solid rgba(46,51,69,0.5);
    vertical-align: middle;
  }
  .match-table .status-paid { color: var(--green); font-weight: 600; }
  .match-table .status-unmatched { color: var(--orange); font-weight: 600; }
  .match-table .reason-cell { font-size: 11px; color: var(--red); max-width: 300px; }
  .cat-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    border: 1px solid var(--border);
  }
  .cat-table th {
    text-align: left;
    padding: 8px 10px;
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
    background: var(--surface2);
    z-index: 1;
  }
  .cat-table td {
    padding: 8px 10px;
    border: 1px solid var(--border);
    vertical-align: middle;
  }
  .match-btn {
    border-style: dashed !important;
    border-color: var(--orange) !important;
    opacity: 0.85;
  }
  .match-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }
  .check-btn {
    border-style: dashed !important;
    border-color: var(--yellow) !important;
    opacity: 0.85;
  }
  .check-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }
  .compare-btn {
    border-style: dashed !important;
    border-color: var(--green, #4ade80) !important;
    opacity: 0.85;
  }
  .compare-btn:hover:not(:disabled) {
    opacity: 1;
    border-style: solid !important;
  }

  /* Step with upload */
  .step-with-upload {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .upload-row {
    display: flex;
  }
  .upload-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    width: 100%;
    padding: 5px 10px;
    background: transparent;
    border: 1px dashed var(--border);
    border-radius: 6px;
    color: var(--text-dim);
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .upload-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(108,140,255,0.05);
  }
  .upload-step-btn {
    cursor: pointer;
    border-style: dashed;
  }
  .upload-step-btn:hover {
    border-color: var(--accent);
    background: rgba(108,140,255,0.05);
  }
  }
</style>
</head>
<body>
<div class="container">
  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <h1><span>CC</span> Statement Automation</h1>
    </div>
    <div class="header-center">
      <div id="globalStatus" class="status-badge status-idle">Idle</div>
      <button class="header-sync-btn" onclick="syncZoho()" id="btn-sync-header">
        <span class="hdr-num">S</span> Sync Zoho Books
        <span class="info-btn" onclick="event.stopPropagation()">i
          <span class="info-tooltip">Pull all zoho bank accounts, CC accounts, Bills, Vendors and Chart of Accounts.</span>
        </span>
        <span class="step-indicator ind-idle" id="ind-sync" style="margin-left:2px"></span>
      </button>
    </div>
    <div class="header-right">
      <div class="header-summary">
        <span class="header-summary-item"><strong id="sumInvoices">-</strong> Invoices</span>
        <span class="header-summary-item"><strong id="sumBills">-</strong> Bills</span>
        <span class="header-summary-item"><strong id="sumCC">-</strong> CC Txns</span>
      </div>
    </div>
  </div>
  <span id="msg-sync" style="display:none"></span>

  <!-- Two-column layout -->
  <div class="main-layout">
    <!-- Left panel: Phases -->
    <div class="left-panel">
    <div class="left-panel-scroll">
      <!-- Box 1: Banking -->
      <div class="phase">
        <div class="phase-label">Banking</div>
        <div class="step-grid">
          <div class="step-with-upload">
            <div class="upload-row">
              <input type="file" id="ccUploadInput" accept=".pdf" multiple style="display:none" onchange="handleCCUpload(this)">
              <label for="ccUploadInput" class="step-btn upload-step-btn" style="width:100%">
                <span class="step-num">1</span> Upload CC Statement
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Upload CC statement PDFs.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-4"></span>
                <span class="step-msg" id="msg-4"></span>
              </label>
            </div>
            <div style="display:flex;gap:6px;margin-top:4px">
              <button class="upload-btn" onclick="openCCPreview()" style="flex:1">Preview</button>
              <button class="upload-btn" onclick="clearParsedCC()" style="flex:1">Clear Cache</button>
            </div>
          </div>
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" data-step="5" onclick="openImportPicker()" style="width:100%">
                <span class="step-num">2</span> Import CC in Zoho banking
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Import CC transactions in Zoho Books Banking.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-5"></span>
                <span class="step-msg" id="msg-5"></span>
              </button>
            </div>
            <div style="margin-top:4px">
              <button class="upload-btn" onclick="clearImportCache()" style="width:100%">Clear Cache</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Box 2: Invoices -->
      <div class="phase">
        <div class="phase-label">Invoices &rarr; Compare</div>
        <div class="step-grid">
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" data-step="1" onclick="runStep('1')" style="width:100%">
                <span class="step-num">1</span> Download Invoices from Mailbox
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Connects to Outlook searches inbox for invoice/receipt emails, and downloads PDF attachments/</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-1"></span>
                <span class="step-msg" id="msg-1"></span>
              </button>
            </div>
          </div>
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" onclick="runExtractMail()" id="btn-extract-mail" style="width:100%;border:1.5px dashed var(--orange);background:rgba(251,146,60,0.05)">
                <span class="step-num" style="background:var(--orange);color:#000;font-size:10px">M</span> Extract mail invoice details
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Extract invoice number and other details.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-extract-mail"></span>
                <span class="step-msg" id="msg-extract-mail"></span>
              </button>
            </div>
          </div>
          <div class="step-with-upload">
            <div class="upload-row">
              <input type="file" id="invoiceUploadInput" accept=".pdf,.jpg,.jpeg,.png,.eml" multiple style="display:none" onchange="handleInvoiceUpload(this)">
              <label for="invoiceUploadInput" class="step-btn upload-step-btn" style="width:100%">
                <span class="step-num">2</span> Upload Invoices &amp; Extract details
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Upload invoices and extracts data.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-upload-extract"></span>
                <span class="step-msg" id="msg-upload-extract"></span>
              </label>
            </div>
            <div style="margin-top:4px">
              <button class="upload-btn" onclick="openExtractPreview()" style="width:100%">Preview</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Box 3: Bills -->
      <div class="phase">
        <div class="phase-label">Bills</div>
        <div class="step-grid">
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" data-step="3" onclick="openBillPicker()" style="width:100%">
                <span class="step-num">1</span> Create Bills
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Create bills.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-3"></span>
                <span class="step-msg" id="msg-3"></span>
              </button>
            </div>
          </div>
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn review-btn" onclick="openReviewPanel()" style="width:100%">
                <span class="step-num review-badge">R</span> Review Accounts
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Review and fix expense account assignments on bills.</span>
                </span>
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Box 4: Payments -->
      <div class="phase">
        <div class="phase-label">Payments</div>
        <div class="step-grid">
          <div class="step-with-upload">
            <div class="upload-row">
              <button class="step-btn" data-step="6" onclick="openPaymentPreview()" style="width:100%">
                <span class="step-num">1</span> Record Payment
                <span class="info-btn" onclick="event.stopPropagation()">i
                  <span class="info-tooltip">Record the payment for the bills individually or in bulk.</span>
                </span>
                <span class="step-indicator ind-idle" id="ind-6"></span>
                <span class="step-msg" id="msg-6"></span>
              </button>
            </div>
            <div style="margin-top:4px">
              <button class="upload-btn" onclick="clearPaymentsCache()" style="width:100%">Clear Cache</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Box 5: Others -->
      <div class="phase" style="display:none">
        <div class="phase-label">Others</div>
        <div class="step-grid">
          <button class="step-btn" onclick="runExtractZips()" id="btn-extract-zips" style="border:1.5px dashed var(--accent);background:rgba(108,140,255,0.05)">
            <span class="step-num" style="background:var(--accent);color:#fff;font-size:10px">Z</span> Extract ZIPs
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Extract PDFs from ZIP files (and loose PDFs) in 'all zips' folder, then parse and organize into month-wise folders.</span>
            </span>
            <span class="step-indicator ind-idle" id="ind-extract-zips"></span>
            <span class="step-msg" id="msg-extract-zips"></span>
          </button>
          <button class="step-btn" onclick="runCompareMail()" id="btn-compare-mail" style="border:1.5px dashed var(--yellow);background:rgba(250,204,21,0.05)">
            <span class="step-num" style="background:var(--yellow);color:#000;font-size:10px">C</span> Mail Compare
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Compare mail_extracted_invoices.json vs extracted_invoices.json. Shows which mail invoices are found/missing in the main extracted data. Saves to output/mail_vs_extracted_compare.json.</span>
            </span>
            <span id="mailCompareResult" style="font-size:10px;color:var(--text-dim);display:block;margin-top:2px"></span>
          </button>
          <button class="step-btn" onclick="openInvoiceBrowse()" style="border:1.5px dashed var(--orange);background:rgba(251,146,60,0.05)">
            <span class="step-num" style="background:var(--orange);color:#000;font-size:10px">B</span> Browse Invoices
            <span class="info-btn" onclick="event.stopPropagation()">i
              <span class="info-tooltip">Browse all extracted invoices with month-wise filtering. Shows date, vendor, invoice number, amount and line item descriptions.</span>
            </span>
          </button>
        </div>
      </div>

      <!-- Action buttons (hidden — backend logic retained) -->
      <div class="action-row" style="display:none;">
        <button class="btn-primary" id="btnRunAll" onclick="confirmRunAll()">Run All</button>
        <button class="btn-danger" id="btnCleanup" onclick="confirmCleanup()">Cleanup</button>
      </div>

      <!-- Confirmation modal -->
      <div id="confirmModal" class="modal-overlay" style="display:none;z-index:10000">
        <div class="modal-box">
          <div class="modal-title" id="modalTitle"></div>
          <div class="modal-msg" id="modalMsg"></div>
          <div class="modal-actions">
            <button class="modal-btn modal-btn-cancel" onclick="closeModal()">No, Cancel</button>
            <button class="modal-btn modal-btn-confirm" id="modalConfirmBtn">Yes, Proceed</button>
          </div>
        </div>
      </div>

    </div><!-- end left-panel-scroll -->
    </div><!-- end left-panel -->

    <!-- Right panel: Logs + Review -->
    <div class="right-panel">
      <div class="log-panel" id="logPanel">
        <div class="log-header">
          <span>Live Logs</span>
          <button onclick="clearLogs()">Clear</button>
        </div>
        <div id="logBox"></div>
      </div>

      <!-- Review panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="reviewPanel" style="display:none">
        <div class="review-header">
          <span>Review Expense Accounts</span>
          <div style="display:flex;gap:8px;align-items:center">
            <button class="review-create-btn" onclick="openCreateAccountModal()">+ New Account</button>
            <button class="review-close-btn" onclick="closeReviewPanel()">&#10005; Close</button>
          </div>
        </div>
        <div style="padding:8px 16px 0;display:none" id="reviewFilterBar">
          <select id="reviewVendorFilter" onchange="filterReviewTable()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px;min-width:250px;color-scheme:dark">
            <option value="">All Vendors</option>
          </select>
          <button onclick="document.getElementById('reviewVendorFilter').value='';filterReviewTable()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;margin-left:8px">Clear</button>
          <span id="reviewCountLabel" style="margin-left:12px;color:var(--text-dim);font-size:12px"></span>
        </div>
        <div class="review-body" id="reviewBody">
          <div class="review-loading" id="reviewLoading">Loading bills...</div>
          <table class="review-table" id="reviewTable" style="display:none">
            <thead>
              <tr>
                <th>Vendor</th>
                <th>File name</th>
                <th>Line Items</th>
                <th>Amount</th>
                <th>Currency</th>
                <th>Current Account</th>
                <th>Change To</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="reviewTableBody"></tbody>
          </table>
        </div>
      </div>

      <!-- Delete Bills panel -->
      <div class="review-panel" id="deleteBillsPanel" style="display:none">
        <div class="review-header" style="border-bottom-color:#ef4444">
          <span style="color:#ef4444">Delete Bills from Zoho</span>
          <div style="display:flex;gap:8px;align-items:center">
            <button id="deleteSelectedBtn" onclick="confirmDeleteSelected()" style="background:#ef4444;color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;display:none">Delete Selected (0)</button>
            <button class="review-close-btn" onclick="closeDeleteBillsPanel()">&#10005; Close</button>
          </div>
        </div>
        <div style="padding:8px 16px 0;display:flex;gap:12px;align-items:center">
          <select id="deleteVendorFilter" onchange="filterDeleteBills()" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px;min-width:250px;color-scheme:dark">
            <option value="">All Vendors</option>
          </select>
          <span id="deleteCountLabel" style="color:var(--text-dim);font-size:12px"></span>
        </div>
        <div class="review-body" id="deleteBillsBody">
          <div class="review-loading" id="deleteBillsLoading">Loading bills from Zoho...</div>
          <table class="review-table" id="deleteBillsTable" style="display:none">
            <thead>
              <tr>
                <th style="width:30px"><input type="checkbox" id="deleteSelectAll" onchange="toggleDeleteSelectAll(this)"></th>
                <th>Vendor</th>
                <th>Bill #</th>
                <th>Date</th>
                <th>Amount</th>
                <th>Currency</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="deleteBillsTableBody"></tbody>
          </table>
        </div>
      </div>

      <!-- Delete Vendors panel -->
      <div class="review-panel" id="deleteVendorsPanel" style="display:none">
        <div class="review-header" style="border-bottom-color:#ef4444">
          <span style="color:#ef4444">Delete Vendors from Zoho</span>
          <div style="display:flex;gap:8px;align-items:center">
            <button id="deleteVendorsSelectedBtn" onclick="confirmDeleteVendorsSelected()" style="background:#ef4444;color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;display:none">Delete Selected (0)</button>
            <button class="review-close-btn" onclick="closeDeleteVendorsPanel()">&#10005; Close</button>
          </div>
        </div>
        <div style="padding:8px 16px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <input type="text" id="vendorSearchBar" oninput="filterDeleteVendors()" placeholder="Search vendor..." style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px;min-width:250px;color-scheme:dark">
          <label style="font-size:12px;color:var(--text-dim);cursor:pointer"><input type="checkbox" id="vendorShowEmpty" onchange="filterDeleteVendors()" checked> Empty only (0 bills)</label>
          <span id="vendorCountLabel" style="color:var(--text-dim);font-size:12px"></span>
        </div>
        <div class="review-body" id="deleteVendorsBody">
          <div class="review-loading" id="deleteVendorsLoading">Loading vendors from Zoho...</div>
          <table class="review-table" id="deleteVendorsTable" style="display:none">
            <thead>
              <tr>
                <th style="width:30px"><input type="checkbox" id="vendorSelectAll" onchange="toggleVendorSelectAll(this)"></th>
                <th>Vendor Name</th>
                <th>Bills</th>
                <th>Outstanding</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="deleteVendorsTableBody"></tbody>
          </table>
        </div>
      </div>

      <!-- Auto Match Banking panel -->
      <div class="review-panel" id="autoMatchPanel" style="display:none;position:fixed;top:0;right:0;bottom:0;left:calc(14% + 16px);z-index:1000;border-radius:0;border:none">
        <div class="review-header">
          <span>Auto Match &mdash; Banking Transactions</span>
          <div style="display:flex;gap:12px;align-items:center">
            <button id="matchSelectedBtn" onclick="confirmMatchSelected()" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;display:none">Match Selected (0)</button>
            <span id="autoMatchSummary" style="font-size:12px;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeAutoMatchPanel()">&#10005; Close</button>
          </div>
        </div>
        <div style="padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02);display:flex;gap:12px;align-items:center;flex-shrink:0;font-size:12px">
          <label style="color:var(--text-dim)">Card:</label>
          <select id="amCardFilter" onchange="filterAutoMatch()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px">
            <option value="">All Cards</option>
          </select>
          <label style="color:var(--text-dim);margin-left:8px">Vendor:</label>
          <select id="amVendorFilter" onchange="filterAutoMatch()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px;max-width:200px">
            <option value="">All Vendors</option>
          </select>
          <label style="color:var(--text-dim);margin-left:8px">Status:</label>
          <select id="amStatusFilter" onchange="filterAutoMatch()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px">
            <option value="">All</option>
            <option value="has_match">Has Match</option>
            <option value="no_match">No Match</option>
          </select>
          <button onclick="document.getElementById('amCardFilter').value='';document.getElementById('amVendorFilter').value='';document.getElementById('amStatusFilter').value='';filterAutoMatch()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer">Clear</button>
          <button onclick="fetchAllMatches()" style="background:var(--accent);color:#fff;border:none;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600;margin-left:auto">Find All Matches</button>
        </div>
        <div id="autoMatchBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow-y:auto">
          <div class="review-loading" id="autoMatchLoading">Fetching uncategorized transactions...</div>
          <div id="autoMatchContent" style="display:none;flex-direction:column"></div>
        </div>
      </div>

      <!-- Banking Summary panel -->
      <div class="review-panel" id="bankingSummaryPanel" style="display:none;position:fixed;top:0;right:0;bottom:0;left:calc(14% + 16px);z-index:1000;border-radius:0;border:none">
        <div class="review-header">
          <span>Banking Summary &mdash; Month-wise Overview</span>
          <div style="display:flex;gap:12px;align-items:center">
            <span id="bsSummaryText" style="font-size:12px;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeBankingSummary()">&#10005; Close</button>
          </div>
        </div>
        <div style="padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02);display:flex;gap:12px;align-items:center;flex-shrink:0;font-size:12px">
          <label style="color:var(--text-dim)">Card:</label>
          <select id="bsCardFilter" onchange="filterBankingSummary()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px">
            <option value="">All Cards</option>
          </select>
          <label style="color:var(--text-dim);margin-left:8px">FY:</label>
          <select id="bsFyFilter" onchange="filterBankingSummary()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px">
            <option value="">All</option>
          </select>
          <button onclick="document.getElementById('bsCardFilter').value='';document.getElementById('bsFyFilter').value='';filterBankingSummary()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer">Clear</button>
          <span id="bsCacheInfo" style="font-size:10px;color:var(--text-dim);margin-left:auto"></span>
          <button id="bsVendorBtn" onclick="toggleVendorBreakdown()" style="background:var(--orange);color:#000;border:none;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">Vendor Breakdown</button>
          <button onclick="openBankingSummary(true)" style="background:var(--accent);color:#fff;border:none;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">Refresh from Zoho</button>
        </div>
        <div id="bankingSummaryBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow-y:auto;padding:12px">
          <div class="review-loading" id="bsLoading">Fetching banking data...</div>
          <div id="bsContent" style="display:none"></div>
          <div id="bsVendorContent" style="display:none"></div>
        </div>
      </div>

      <!-- Match Status panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="matchPanel" style="display:none">
        <div class="review-header">
          <span>Match Status</span>
          <button class="review-close-btn" onclick="closeMatchPanel()">&#10005; Close</button>
        </div>
        <div class="review-body" id="matchBody">
          <div class="review-loading" id="matchLoading">Loading match data...</div>
          <div id="matchContent" style="display:none">
            <div class="match-summary">
              <div class="match-stat stat-matched">
                <div class="stat-value" id="matchedCount">0</div>
                <div class="stat-label">Matched</div>
              </div>
              <div class="match-stat stat-unmatched">
                <div class="stat-value" id="unmatchedCount">0</div>
                <div class="stat-label">Unmatched</div>
              </div>
              <div class="match-stat stat-total">
                <div class="stat-value" id="totalCount">0</div>
                <div class="stat-label">Total</div>
              </div>
            </div>
            <div class="match-tabs">
              <button class="match-tab tab-matched active" onclick="switchMatchTab('matched')">Matched</button>
              <button class="match-tab tab-unmatched" onclick="switchMatchTab('unmatched')">Unmatched</button>
            </div>
            <table class="match-table">
              <thead><tr id="matchTableHead"></tr></thead>
              <tbody id="matchTableBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Check CC Match panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="checkPanel" style="display:none">
        <div class="review-header">
          <span>Check CC Match (cached)</span>
          <div style="display:flex;gap:16px;align-items:center">
            <span id="checkSummaryText" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeCheckPanel()">&#10005; Close</button>
          </div>
        </div>
        <div id="checkBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="checkLoading" style="align-self:center;width:100%;text-align:center">Fetching from Zoho...</div>
          <div id="checkContent" style="display:none;flex:1;overflow-y:auto;flex-direction:column"></div>
        </div>
      </div>

      <!-- Payment Preview panel (full-height overlay, keeps sidebar visible) -->
      <div class="review-panel" id="paymentPanel" style="display:none;position:fixed;top:0;right:0;bottom:0;left:calc(14% + 16px);z-index:1000;border-radius:0;border:none">
        <div class="review-header">
          <span>Record Payments &mdash; CC &harr; Bill Match</span>
          <div style="display:flex;gap:12px;align-items:center">
            <span id="paymentCacheIndicator" style="font-size:10px;color:var(--text-dim);padding:2px 8px;border-radius:4px;background:rgba(255,255,255,0.05)"></span>
            <button id="paymentRefreshBtn" onclick="refreshPaymentPreview()" style="background:none;border:1px solid var(--border);border-radius:6px;color:var(--text-dim);padding:4px 10px;font-size:11px;cursor:pointer" title="Fetch fresh data from Zoho APIs">&#8635; Refresh</button>
            <button id="syncPaidBtn" onclick="syncPaidBills()" style="background:none;border:1px solid var(--border);border-radius:6px;color:var(--text-dim);padding:4px 10px;font-size:11px;cursor:pointer" title="Sync paid bills from Zoho to identify already-paid bills">&#9745; Sync Paid</button>
            <select id="paymentCardFilter" onchange="filterPayments()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px;display:none">
              <option value="">All Cards</option>
            </select>
            <span id="paymentSummaryText" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button id="recordSelectedBtn" onclick="confirmRecordSelected()" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;display:none">Record Selected (0)</button>
            <button class="review-close-btn" onclick="closePaymentPanel()">&#10005; Close</button>
          </div>
        </div>
        <div id="paymentFilterBar" style="display:none;padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02);gap:12px;align-items:center;flex-shrink:0;flex-wrap:wrap;font-size:12px">
          <label style="color:var(--text-dim)">&#128269;</label>
          <input type="text" id="paymentSearchBar" oninput="filterPayments()" placeholder="Search vendor / CC desc..." style="background:var(--bg);border:1px solid var(--accent);border-radius:6px;color:var(--text);padding:4px 10px;font-size:12px;min-width:200px;color-scheme:dark">
          <label style="color:var(--text-dim)">Month:</label>
          <select id="paymentMonthFilter" onchange="applyMonthFilter()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px;min-width:220px;color-scheme:dark"><option value="">All Months</option></select>
          <label style="color:var(--text-dim);margin-left:8px">Vendor:</label>
          <select id="paymentVendorFilter" onchange="filterPayments()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px;max-width:200px"><option value="">All Vendors</option></select>
          <label style="color:var(--text-dim);margin-left:8px">From:</label>
          <input type="date" id="paymentDateFrom" onchange="filterPayments()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <label style="color:var(--text-dim)">To:</label>
          <input type="date" id="paymentDateTo" onchange="filterPayments()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <label style="color:var(--text-dim);margin-left:8px">Status:</label>
          <select id="paymentStatusFilter" onchange="filterPayments()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px">
            <option value="">All Statuses</option>
            <option value="matched">Matched</option>
            <option value="group_matched">Group Matched</option>
            <option value="unmatched">No CC Match</option>
            <option value="cc_only">CC Only</option>
            <option value="already_paid">Already Paid</option>
          </select>
          <label style="color:var(--text-dim);margin-left:8px">Amt:</label>
          <input type="text" id="paymentAmountFilter" oninput="filterPayments()" placeholder="e.g. 30000" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;width:90px">
          <button onclick="clearPaymentFilters()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer">Clear</button>
        </div>
        <div id="paymentBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow-y:auto">
          <div class="review-loading" id="paymentLoading" style="align-self:center;width:100%;text-align:center">Fetching bills &amp; CC transactions...</div>
          <div id="paymentContent" style="display:none;flex-direction:column"></div>
        </div>
      </div>

      <!-- Monthly Compare panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="comparePanel" style="display:none">
        <div class="review-header">
          <span>Monthly Compare &mdash; CC vs Invoices</span>
          <div style="display:flex;gap:12px;align-items:center">
            <button id="parseAllBillsBtn" onclick="parseOrgInvoices()" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer">Parse All Invoices</button>
            <button id="parseAllCCBtn" onclick="parseAllForCompare('4','parseAllCCBtn','CC')" style="background:var(--yellow);color:#000;border:none;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer">Parse All CC</button>
            <select id="compareMonthSelect" onchange="renderCompareMonth(this.value)" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px"></select>
            <span id="compareSummaryText" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeComparePanel()">&#10005; Close</button>
          </div>
        </div>
        <div id="compareFilterBar" style="display:none;padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02);gap:12px;align-items:center;flex-shrink:0;flex-wrap:wrap;font-size:12px">
          <label style="color:var(--text-dim)">Vendor:</label>
          <select id="compareVendorFilter" onchange="applyCompareFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px;max-width:200px"></select>
          <label style="color:var(--text-dim);margin-left:12px">From:</label>
          <input type="date" id="compareDateFrom" onchange="applyCompareFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <label style="color:var(--text-dim)">To:</label>
          <input type="date" id="compareDateTo" onchange="applyCompareFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <button onclick="clearCompareFilters()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer">Clear</button>
        </div>
        <div id="compareBody" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="compareLoading" style="align-self:center;width:100%;text-align:center">Loading data...</div>
          <div id="compareContent" style="display:none;flex:1;overflow-y:auto;flex-direction:column"></div>
        </div>
      </div>

      <!-- Invoice Browse panel (hidden by default, overlays log panel) -->
      <div class="review-panel" id="invoiceBrowsePanel" style="display:none">
        <div class="review-header">
          <span>Browse Invoices</span>
          <div style="display:flex;gap:12px;align-items:center">
            <span id="invBrowseSummary" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeInvoiceBrowse()">&#10005; Close</button>
          </div>
        </div>
        <div id="invBrowseFilterBar" style="display:none;padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.02);gap:12px;align-items:center;flex-shrink:0;flex-wrap:wrap;font-size:12px">
          <label style="color:var(--text-dim)">Month:</label>
          <select id="invBrowseMonthSelect" onchange="applyInvBrowseFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px"></select>
          <label style="color:var(--text-dim);margin-left:12px">Vendor:</label>
          <select id="invBrowseVendorSelect" onchange="applyInvBrowseFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px;max-width:220px"></select>
          <label style="color:var(--text-dim);margin-left:12px">From:</label>
          <input type="date" id="invBrowseDateFrom" onchange="applyInvBrowseFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <label style="color:var(--text-dim)">To:</label>
          <input type="date" id="invBrowseDateTo" onchange="applyInvBrowseFilters()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 6px;font-size:11px;color-scheme:dark">
          <button onclick="clearInvBrowseFilters()" style="background:transparent;color:var(--accent);border:1px dashed var(--accent);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer">Clear</button>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="invBrowseLoading" style="align-self:center;width:100%;text-align:center">Loading invoices...</div>
          <div id="invBrowseContent" style="display:none;flex:1;overflow-y:auto">
            <table class="match-table" id="invBrowseTable">
              <thead>
                <tr>
                  <th style="width:90px">Date</th>
                  <th>Vendor</th>
                  <th>Invoice #</th>
                  <th style="text-align:right;width:110px">Amount</th>
                  <th>Line Items</th>
                </tr>
              </thead>
              <tbody id="invBrowseBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Extract Preview panel -->
      <div class="review-panel" id="extractPreviewPanel" style="display:none">
        <div class="review-header">
          <span>Extract Preview <span id="extractPreviewSource" style="font-size:11px;font-weight:400;color:var(--text-dim);margin-left:8px"></span></span>
          <div style="display:flex;gap:12px;align-items:center">
            <span id="extractPreviewSummary" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-close-btn" onclick="closeExtractPreview()">&#10005; Close</button>
          </div>
        </div>
        <div id="extractPreviewTabs" style="display:flex;gap:4px;padding:8px 12px 0;border-bottom:1px solid var(--border);flex-shrink:0">
          <button class="ep-tab ep-tab-active" data-tab="extracted" onclick="switchExtractPreviewTab('extracted')" style="background:transparent;border:none;color:var(--text);font-size:12px;padding:6px 14px;cursor:pointer;border-bottom:2px solid var(--green);font-weight:500">Extracted <span id="epExtractedCount" style="color:var(--green)">0</span></button>
          <button class="ep-tab" data-tab="failed" onclick="switchExtractPreviewTab('failed')" style="background:transparent;border:none;color:var(--text-dim);font-size:12px;padding:6px 14px;cursor:pointer;border-bottom:2px solid transparent">Failed <span id="epFailedCount" style="color:var(--red)">0</span></button>
          <button class="ep-tab" data-tab="skipped" onclick="switchExtractPreviewTab('skipped')" style="background:transparent;border:none;color:var(--text-dim);font-size:12px;padding:6px 14px;cursor:pointer;border-bottom:2px solid transparent">Skipped <span id="epSkippedCount" style="color:var(--text-dim)">0</span></button>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="extractPreviewLoading" style="align-self:center;width:100%;text-align:center">Loading preview...</div>
          <div id="extractPreviewContent" style="display:none;flex:1;overflow-y:auto">
            <!-- Extracted table -->
            <table class="match-table" id="epExtractedTable">
              <thead>
                <tr>
                  <th style="width:90px">Date</th>
                  <th>Vendor</th>
                  <th>Invoice #</th>
                  <th style="text-align:right;width:110px">Amount</th>
                  <th>File</th>
                </tr>
              </thead>
              <tbody id="epExtractedBody"></tbody>
            </table>
            <!-- Failed table -->
            <table class="match-table" id="epFailedTable" style="display:none">
              <thead>
                <tr>
                  <th style="width:40%">File</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="epFailedBody"></tbody>
            </table>
            <!-- Skipped table -->
            <table class="match-table" id="epSkippedTable" style="display:none">
              <thead>
                <tr>
                  <th style="width:40%">File</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="epSkippedBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- CC Parse Preview panel -->
      <div class="review-panel" id="ccPreviewPanel" style="display:none">
        <div class="review-header">
          <span>CC Parse Preview <span id="ccPreviewSource" style="font-size:11px;font-weight:400;color:var(--text-dim);margin-left:8px"></span></span>
          <div style="display:flex;gap:12px;align-items:center">
            <span id="ccPreviewSummary" style="font-size:12px;font-weight:400;color:var(--text-dim)"></span>
            <button class="review-create-btn" onclick="openImportFromCCPreview()" style="background:var(--green);border-color:var(--green);color:#000">&rarr; Import to Banking</button>
            <button class="review-close-btn" onclick="closeCCPreview()">&#10005; Close</button>
          </div>
        </div>
        <div id="ccPreviewTabs" style="display:flex;gap:4px;padding:8px 12px 0;border-bottom:1px solid var(--border);flex-shrink:0">
          <button class="cp-tab cp-tab-active" data-tab="parsed" onclick="switchCCPreviewTab('parsed')" style="background:transparent;border:none;color:var(--text);font-size:12px;padding:6px 14px;cursor:pointer;border-bottom:2px solid var(--green);font-weight:500">Parsed <span id="cpParsedCount" style="color:var(--green)">0</span></button>
          <button class="cp-tab" data-tab="failed" onclick="switchCCPreviewTab('failed')" style="background:transparent;border:none;color:var(--text-dim);font-size:12px;padding:6px 14px;cursor:pointer;border-bottom:2px solid transparent">Failed <span id="cpFailedCount" style="color:var(--red)">0</span></button>
          <button class="cp-tab" data-tab="skipped" onclick="switchCCPreviewTab('skipped')" style="background:transparent;border:none;color:var(--text-dim);font-size:12px;padding:6px 14px;cursor:pointer;border-bottom:2px solid transparent">Skipped <span id="cpSkippedCount" style="color:var(--text-dim)">0</span></button>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
          <div class="review-loading" id="ccPreviewLoading" style="align-self:center;width:100%;text-align:center">Loading preview...</div>
          <div id="ccPreviewContent" style="display:none;flex:1;overflow-y:auto;padding:8px 12px">
            <!-- Parsed content: groups per card, expandable -->
            <div id="cpParsedContent"></div>
            <!-- Failed table -->
            <table class="match-table" id="cpFailedTable" style="display:none">
              <thead>
                <tr>
                  <th style="width:35%">File</th>
                  <th style="width:20%">Card</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="cpFailedBody"></tbody>
            </table>
            <!-- Skipped table -->
            <table class="match-table" id="cpSkippedTable" style="display:none">
              <thead>
                <tr>
                  <th style="width:35%">Card</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="cpSkippedBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Import Picker modal -->
      <div id="importPickerModal" class="modal-overlay" style="display:none">
        <div class="modal-box" style="max-width:760px;width:92vw;max-height:85vh;display:flex;flex-direction:column">
          <div class="modal-title">Select Cards to Import</div>
          <div id="importPickerBody" style="margin-bottom:16px;overflow-y:auto;flex:1;min-height:0">
            <div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading available CSVs...</div>
          </div>
          <div class="modal-actions">
            <button class="modal-btn modal-btn-cancel" onclick="closeImportPicker()">Cancel</button>
            <button class="modal-btn modal-btn-confirm" onclick="importSelectedCards()">Import Selected</button>
          </div>
        </div>
      </div>

      <!-- Bill Picker modal -->
      <div id="billPickerModal" class="modal-overlay" style="display:none">
        <div class="modal-box" style="max-width:1100px;max-height:85vh;width:95vw;display:flex;flex-direction:column">
          <div class="modal-title">Select Invoices to Create Bills</div>
          <div class="bill-picker-layout">
            <div class="bill-picker-left" id="billPickerBody">
              <div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading invoices...</div>
            </div>
            <div class="bill-picker-right" id="billPickerSummary"></div>
          </div>
        </div>
      </div>

      <!-- Create Account modal -->
      <div id="createAccountModal" class="modal-overlay" style="display:none">
        <div class="modal-box">
          <div class="modal-title">Create New Expense Account</div>
          <div style="margin-bottom:16px">
            <label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px">Account Name</label>
            <input type="text" id="newAccountName" placeholder="e.g. Cloud Infrastructure" style="width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
          </div>
          <div style="margin-bottom:20px">
            <label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px">Description (optional)</label>
            <input type="text" id="newAccountDesc" placeholder="e.g. AWS, GCP hosting costs" style="width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
          </div>
          <div class="modal-actions">
            <button class="modal-btn modal-btn-cancel" onclick="closeCreateAccountModal()">Cancel</button>
            <button class="modal-btn modal-btn-confirm" id="createAccountBtn" onclick="createNewAccount()">Create</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
function fmtDate(d) {
  if (!d) return '-';
  var parts = d.split('-');
  if (parts.length === 3) return parts[2] + '-' + parts[1] + '-' + parts[0];
  return d;
}
function fmt(n) { return n != null ? Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; }
const logBox = document.getElementById('logBox');
let autoScroll = true;

// Track scroll position — disable auto-scroll when user scrolls up
logBox.addEventListener('scroll', () => {
  const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
  autoScroll = atBottom;
});

function addLogLine(text) {
  const div = document.createElement('div');
  div.className = 'log-line';
  // Color based on level
  if (text.includes('[ERROR]')) div.className += ' log-line-ERROR';
  else if (text.includes('[WARNING]')) div.className += ' log-line-WARNING';
  else div.className += ' log-line-INFO';
  div.textContent = text;
  logBox.appendChild(div);
  // Limit to 1000 lines
  while (logBox.children.length > 1000) logBox.removeChild(logBox.firstChild);
  if (autoScroll) logBox.scrollTop = logBox.scrollHeight;
}

function clearLogs() {
  logBox.innerHTML = '';
}

// --- SSE for live logs ---
let evtSource = null;
function connectSSE() {
  evtSource = new EventSource('/api/logs');
  evtSource.onmessage = function(e) {
    try {
      const data = JSON.parse(e.data);
      if (data.line) addLogLine(data.line);
    } catch(err) {}
  };
  evtSource.onerror = function() {
    evtSource.close();
    setTimeout(connectSSE, 3000);
  };
}
connectSSE();

// --- Load log history on page load ---
fetch('/api/logs/history?n=100')
  .then(r => r.json())
  .then(data => {
    (data.lines || []).forEach(l => addLogLine(l));
  });

// --- Run step ---
function runStep(step) {
  fetch('/api/run/' + step, {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[UI] ' + data.error);
      }
      pollStatus();
    })
    .catch(err => addLogLine('[UI] Request failed: ' + err));
}

function runExtractZips() {
  fetch('/api/extract-zips', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[UI] ' + data.error);
      }
      pollStatus();
    })
    .catch(err => addLogLine('[UI] Request failed: ' + err));
}

function runExtractMail() {
  fetch('/api/extract-mail-invoices', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[UI] ' + data.error);
      }
      pollStatus();
    })
    .catch(err => addLogLine('[UI] Request failed: ' + err));
}

function runCompareMail() {
  var resultEl = document.getElementById('mailCompareResult');
  resultEl.textContent = 'Comparing...';
  resultEl.style.color = 'var(--accent)';
  fetch('/api/compare-mail-invoices', {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        resultEl.textContent = data.error;
        resultEl.style.color = 'var(--red)';
        addLogLine('[UI] Compare failed: ' + data.error);
        return;
      }
      var found = data.found || 0;
      var missing = data.missing || 0;
      var total = data.total_mail || 0;
      var pct = total > 0 ? Math.round(found / total * 100) : 0;
      resultEl.innerHTML = '<span style="color:var(--green)">' + found + ' found</span> &middot; <span style="color:var(--red)">' + missing + ' missing</span> &middot; ' + pct + '%';
      addLogLine('[INFO] Mail Compare: ' + found + ' found, ' + missing + ' missing out of ' + total + ' mail invoices. Saved to output/mail_vs_extracted_compare.json');
    })
    .catch(function(err) {
      resultEl.textContent = 'Failed';
      resultEl.style.color = 'var(--red)';
      addLogLine('[UI] Compare request failed: ' + err);
    });
}

// --- Confirmation modals ---
var _modalOnCancel = null;

function showModal(title, msg, onConfirm, isDanger, confirmText, onCancel) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalMsg').innerHTML = msg;
  const btn = document.getElementById('modalConfirmBtn');
  btn.className = 'modal-btn modal-btn-confirm' + (isDanger ? ' danger' : '');
  btn.textContent = confirmText || (isDanger ? 'Yes, Delete' : 'Yes, Proceed');
  btn.onclick = function() { _modalOnCancel = null; closeModal(); onConfirm(); };
  _modalOnCancel = onCancel || null;
  document.getElementById('confirmModal').style.display = 'flex';
}

function closeModal() {
  document.getElementById('confirmModal').style.display = 'none';
  if (_modalOnCancel) { var cb = _modalOnCancel; _modalOnCancel = null; cb(); }
}

function confirmRunAll() {
  showModal(
    'Run All Steps (1-6)?',
    'This will execute the full pipeline sequentially: Fetch Invoices, Extract Data, Create Bills, Parse CC, Record Payments, and Import Banking.',
    function() { runStep('all'); },
    false
  );
}

function confirmCleanup() {
  showModal(
    'Delete ALL Zoho Data?',
    'This will permanently DELETE all vendors, bills, payments, and bank transactions from Zoho Books, plus local output files. This cannot be undone.',
    function() { runStep('cleanup'); },
    true
  );
}

// --- Poll status ---
function pollStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(updateUI)
    .catch(() => {});
}

function updateUI(data) {
  // Global status badge
  const badge = document.getElementById('globalStatus');
  if (data.running) {
    badge.className = 'status-badge status-running';
    let label = 'Running';
    if (data.current_step && data.current_step !== 'cleanup') {
      label = 'Running Step ' + data.current_step;
    } else if (data.current_step === 'cleanup') {
      label = 'Cleaning Up';
    }
    badge.textContent = label;
  } else {
    badge.className = 'status-badge status-idle';
    badge.textContent = 'Idle';
  }

  // Disable/enable buttons
  const btns = document.querySelectorAll('.step-btn, .btn-primary, .btn-danger, .header-sync-btn');
  btns.forEach(b => b.disabled = data.running);

  // Step indicators + tooltips
  for (let i = 1; i <= 7; i++) {
    const ind = document.getElementById('ind-' + i);
    const msg = document.getElementById('msg-' + i);
    if (!ind || !msg) continue;  // skip removed steps (e.g. step 2 replaced by upload-extract)
    const res = data.step_results[String(i)];
    if (!res) {
      ind.className = 'step-indicator ind-idle';
      msg.textContent = '';
      continue;
    }
    ind.className = 'step-indicator ind-' + res.status;
    msg.textContent = res.message || '';
    msg.style.display = '';
  }

  // Extract ZIPs indicator
  const zipInd = document.getElementById('ind-extract-zips');
  const zipMsgEl = document.getElementById('msg-extract-zips');
  const zipRes = data.step_results['extract-zips'];
  if (zipRes) {
    zipInd.className = 'step-indicator ind-' + zipRes.status;
    zipMsgEl.textContent = zipRes.message || '';
  } else if (data.current_step === 'extract-zips' && data.running) {
    zipInd.className = 'step-indicator ind-running';
    zipMsgEl.textContent = 'Extracting...';
  }

  // Mail Extract indicator
  const mailInd = document.getElementById('ind-extract-mail');
  const mailMsgEl = document.getElementById('msg-extract-mail');
  const mailRes = data.step_results['extract-mail'];
  if (mailRes) {
    mailInd.className = 'step-indicator ind-' + mailRes.status;
    mailMsgEl.textContent = mailRes.message || '';
  } else if (data.current_step === 'extract-mail' && data.running) {
    mailInd.className = 'step-indicator ind-running';
    mailMsgEl.textContent = 'Extracting...';
  }

  // Upload & Extract indicator
  const ueInd = document.getElementById('ind-upload-extract');
  const ueMsgEl = document.getElementById('msg-upload-extract');
  const ueRes = data.step_results['upload-extract'];
  if (ueRes) {
    ueInd.className = 'step-indicator ind-' + ueRes.status;
    ueMsgEl.textContent = ueRes.message || '';
  } else if (data.current_step === 'upload-extract' && data.running) {
    ueInd.className = 'step-indicator ind-running';
    ueMsgEl.textContent = 'Extracting...';
  }

  // Auto-open preview panel when extract transitions to success
  _maybeAutoOpenExtractPreview('upload-extract', ueRes);
  _maybeAutoOpenExtractPreview('extract-mail', data.step_results['extract-mail']);

  // Auto-open CC preview after step 4 (Upload & Extract CC) completes
  _maybeAutoOpenCCPreview(data.step_results['4']);

  // Sync Zoho indicator
  const syncInd = document.getElementById('ind-sync');
  const syncMsg = document.getElementById('msg-sync');
  if (data.current_step === 'Sync Zoho' && data.running) {
    syncInd.className = 'step-indicator ind-running';
    syncMsg.textContent = 'Syncing...';
  } else if (syncInd.className.includes('ind-running') && !data.running) {
    syncInd.className = 'step-indicator ind-success';
    syncMsg.textContent = 'Done';
  }

  // Summary
  const s = data.summary || {};
  document.getElementById('sumInvoices').textContent = s.invoices != null ? s.invoices : '-';
  document.getElementById('sumBills').textContent = s.bills != null ? s.bills : '-';
  document.getElementById('sumCC').textContent = s.cc_transactions != null ? s.cc_transactions : '-';
}

// Poll every 2 seconds
setInterval(pollStatus, 2000);
pollStatus();

// Keep tab awake: defeat browser background throttling + request screen wake lock.
// Silent WebAudio loop keeps Chrome from throttling timers when the tab is hidden,
// so pollStatus/SSE/progress polls keep firing while the user watches logs elsewhere.
(function keepTabAwake() {
  try {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
      var ctx = new AC();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      gain.gain.value = 0;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(0);
      var resume = function() {
        if (ctx.state === 'suspended') ctx.resume();
      };
      document.addEventListener('click', resume);
      document.addEventListener('keydown', resume);
      document.addEventListener('visibilitychange', function() {
        if (document.visibilityState === 'visible') resume();
      });
    }
  } catch (e) {}

  var wakeLock = null;
  var requestWake = function() {
    if ('wakeLock' in navigator) {
      navigator.wakeLock.request('screen').then(function(lock) {
        wakeLock = lock;
        lock.addEventListener('release', function() { wakeLock = null; });
      }).catch(function() {});
    }
  };
  requestWake();
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible' && wakeLock === null) requestWake();
  });
})();

// --- Review Panel ---
let _reviewAccounts = []; // cached accounts list

const _accountTypeLabels = {
  'expense': 'Expense',
  'other_expense': 'Other Expense',
  'cost_of_goods_sold': 'Cost of Goods Sold',
  'income': 'Income',
  'other_income': 'Other Income',
  'fixed_asset': 'Fixed Asset',
  'other_asset': 'Other Asset',
  'other_current_asset': 'Other Current Asset',
  'other_current_liability': 'Other Current Liability',
};

function populateAccountSelect(selectEl, defaultText) {
  selectEl.innerHTML = '';
  const defOpt = document.createElement('option');
  defOpt.value = '';
  defOpt.textContent = defaultText || '-- select account --';
  selectEl.appendChild(defOpt);
  // Group by account_type
  const groups = {};
  _reviewAccounts.forEach(a => {
    const t = a.account_type || 'expense';
    if (!groups[t]) groups[t] = [];
    groups[t].push(a);
  });
  const typeOrder = Object.keys(_accountTypeLabels);
  typeOrder.forEach(t => {
    if (!groups[t] || !groups[t].length) return;
    const og = document.createElement('optgroup');
    og.label = _accountTypeLabels[t] || t;
    groups[t].forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.account_id;
      opt.textContent = a.account_name;
      opt.setAttribute('data-name', a.account_name);
      og.appendChild(opt);
    });
    selectEl.appendChild(og);
  });
}

function openReviewPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('invoiceBrowsePanel').style.display = 'none';
  document.getElementById('extractPreviewPanel').style.display = 'none';
  document.getElementById('ccPreviewPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'flex';
  document.getElementById('reviewLoading').style.display = 'block';
  document.getElementById('reviewTable').style.display = 'none';

  // Fetch bills + accounts in parallel
  Promise.all([
    fetch('/api/review/bills').then(r => r.json()),
    fetch('/api/review/accounts').then(r => r.json()),
  ]).then(([billsData, accountsData]) => {
    if (billsData.error) {
      document.getElementById('reviewLoading').textContent = billsData.error;
      return;
    }
    _reviewAccounts = accountsData.accounts || [];
    renderReviewTable(billsData.bills || []);
  }).catch(err => {
    document.getElementById('reviewLoading').textContent = 'Failed to load: ' + err;
  });
}

function closeReviewPanel() {
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

// ========== Delete Bills Panel ==========
var _deleteBillsData = [];
var _deleteSelectedIds = new Set();

function openDeleteBillsPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('deleteBillsPanel').style.display = 'flex';
  document.getElementById('deleteBillsLoading').style.display = 'block';
  document.getElementById('deleteBillsTable').style.display = 'none';
  _deleteBillsData = [];
  _deleteSelectedIds.clear();
  _updateDeleteBtn();

  fetch('/api/bills/list-all')
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('deleteBillsLoading').textContent = 'Error: ' + data.error;
        return;
      }
      _deleteBillsData = data.bills || [];
      renderDeleteBillsTable(_deleteBillsData);
    })
    .catch(err => {
      document.getElementById('deleteBillsLoading').textContent = 'Failed: ' + err;
    });
}

function closeDeleteBillsPanel() {
  document.getElementById('deleteBillsPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderDeleteBillsTable(bills) {
  var tbody = document.getElementById('deleteBillsTableBody');
  tbody.innerHTML = '';
  if (!bills.length) {
    document.getElementById('deleteBillsLoading').textContent = 'No bills found in Zoho.';
    document.getElementById('deleteBillsLoading').style.display = 'block';
    document.getElementById('deleteBillsTable').style.display = 'none';
    return;
  }

  // Group by vendor
  var groups = {};
  bills.forEach(function(b) {
    var v = b.vendor_name || 'Unknown';
    if (!groups[v]) groups[v] = [];
    groups[v].push(b);
  });
  var sortedVendors = Object.keys(groups).sort();

  // Populate vendor filter
  var filterSel = document.getElementById('deleteVendorFilter');
  var prevVal = filterSel.value;
  filterSel.innerHTML = '<option value="">All Vendors (' + bills.length + ')</option>';
  sortedVendors.forEach(function(v) {
    var opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v + ' (' + groups[v].length + ')';
    filterSel.appendChild(opt);
  });
  filterSel.value = prevVal;
  document.getElementById('deleteCountLabel').textContent = sortedVendors.length + ' vendors, ' + bills.length + ' bills';

  sortedVendors.forEach(function(vendorName) {
    var group = groups[vendorName];
    // Vendor header
    var headerTr = document.createElement('tr');
    headerTr.className = 'vendor-group-header';
    headerTr.setAttribute('data-vendor', vendorName);
    var headerTd = document.createElement('td');
    headerTd.colSpan = 7;
    headerTd.innerHTML = '<span class="vendor-name-label">' + vendorName + ' <span class="vendor-bill-count">(' + group.length + ' bill' + (group.length > 1 ? 's' : '') + ')</span></span>';
    headerTr.appendChild(headerTd);
    tbody.appendChild(headerTr);

    group.forEach(function(b) {
      var tr = document.createElement('tr');
      tr.setAttribute('data-vendor', vendorName);
      tr.setAttribute('data-billid', b.bill_id);

      // Checkbox
      var tdCb = document.createElement('td');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'delete-cb';
      cb.value = b.bill_id;
      cb.onchange = function() {
        if (this.checked) _deleteSelectedIds.add(b.bill_id);
        else _deleteSelectedIds.delete(b.bill_id);
        _updateDeleteBtn();
      };
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);

      // Vendor
      var tdV = document.createElement('td');
      tdV.textContent = b.vendor_name;
      tdV.style.cssText = 'color:var(--text-dim);padding-left:20px';
      tr.appendChild(tdV);

      // Bill #
      var tdNum = document.createElement('td');
      tdNum.textContent = b.bill_number || '-';
      tdNum.style.fontSize = '11px';
      tr.appendChild(tdNum);

      // Date
      var tdDate = document.createElement('td');
      tdDate.textContent = b.date || '-';
      tr.appendChild(tdDate);

      // Amount
      var tdAmt = document.createElement('td');
      tdAmt.textContent = b.total != null ? Number(b.total).toLocaleString() : '-';
      tdAmt.style.fontWeight = '600';
      tr.appendChild(tdAmt);

      // Currency
      var tdCur = document.createElement('td');
      tdCur.textContent = b.currency_code || 'INR';
      tr.appendChild(tdCur);

      // Status
      var tdStatus = document.createElement('td');
      tdStatus.textContent = b.status || '-';
      tdStatus.style.cssText = 'font-size:10px;text-transform:uppercase;color:var(--text-dim)';
      tr.appendChild(tdStatus);

      tbody.appendChild(tr);
    });
  });

  document.getElementById('deleteBillsLoading').style.display = 'none';
  document.getElementById('deleteBillsTable').style.display = 'table';
}

function filterDeleteBills() {
  var vendor = document.getElementById('deleteVendorFilter').value;
  var rows = document.querySelectorAll('#deleteBillsTableBody tr');
  var shown = 0;
  rows.forEach(function(r) {
    var rv = r.getAttribute('data-vendor');
    if (!rv) return;
    if (!vendor || rv === vendor) { r.style.display = ''; shown++; }
    else { r.style.display = 'none'; }
  });
  document.getElementById('deleteCountLabel').textContent = vendor ? shown + ' bills shown' : '';
}

function toggleDeleteSelectAll(masterCb) {
  var rows = document.querySelectorAll('#deleteBillsTableBody tr:not([style*="display: none"]):not(.vendor-group-header)');
  rows.forEach(function(r) {
    var cb = r.querySelector('.delete-cb');
    if (cb) {
      cb.checked = masterCb.checked;
      if (masterCb.checked) _deleteSelectedIds.add(cb.value);
      else _deleteSelectedIds.delete(cb.value);
    }
  });
  _updateDeleteBtn();
}

function _updateDeleteBtn() {
  var btn = document.getElementById('deleteSelectedBtn');
  if (_deleteSelectedIds.size > 0) {
    btn.style.display = '';
    btn.textContent = 'Delete Selected (' + _deleteSelectedIds.size + ')';
  } else {
    btn.style.display = 'none';
  }
}

function confirmDeleteSelected() {
  var count = _deleteSelectedIds.size;
  if (!count) return;
  if (!confirm('Are you sure you want to permanently delete ' + count + ' bill(s) from Zoho? This cannot be undone.')) return;
  var ids = Array.from(_deleteSelectedIds);
  var btn = document.getElementById('deleteSelectedBtn');
  btn.textContent = 'Deleting...';
  btn.disabled = true;

  fetch('/api/bills/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_ids: ids})
  })
  .then(r => r.json())
  .then(data => {
    btn.disabled = false;
    if (data.error) { alert('Error: ' + data.error); _updateDeleteBtn(); return; }
    var ok = (data.succeeded || []).length;
    var fail = (data.failed || []).length;
    alert('Deleted ' + ok + ' bill(s)' + (fail ? ', ' + fail + ' failed' : ''));
    // Remove deleted rows from table
    (data.succeeded || []).forEach(function(bid) {
      _deleteSelectedIds.delete(bid);
      var row = document.querySelector('#deleteBillsTableBody tr[data-billid="' + bid + '"]');
      if (row) row.remove();
    });
    _updateDeleteBtn();
    // Update counts
    _deleteBillsData = _deleteBillsData.filter(function(b) { return !(data.succeeded || []).includes(b.bill_id); });
    document.getElementById('deleteCountLabel').textContent = _deleteBillsData.length + ' bills remaining';
  })
  .catch(function(err) {
    btn.disabled = false;
    alert('Failed: ' + err);
    _updateDeleteBtn();
  });
}

// ========== Delete Vendors Panel ==========
var _deleteVendorsData = [];
var _deleteVendorSelectedIds = new Set();

function openDeleteVendorsPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('deleteBillsPanel').style.display = 'none';
  document.getElementById('deleteVendorsPanel').style.display = 'flex';
  document.getElementById('deleteVendorsLoading').style.display = 'block';
  document.getElementById('deleteVendorsTable').style.display = 'none';
  _deleteVendorsData = [];
  _deleteVendorSelectedIds.clear();
  _updateDeleteVendorsBtn();

  fetch('/api/vendors/list-all')
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('deleteVendorsLoading').textContent = 'Error: ' + data.error;
        return;
      }
      _deleteVendorsData = data.vendors || [];
      renderDeleteVendorsTable(_deleteVendorsData);
    })
    .catch(err => {
      document.getElementById('deleteVendorsLoading').textContent = 'Failed: ' + err;
    });
}

function closeDeleteVendorsPanel() {
  document.getElementById('deleteVendorsPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderDeleteVendorsTable(vendors) {
  var tbody = document.getElementById('deleteVendorsTableBody');
  tbody.innerHTML = '';
  if (!vendors.length) {
    document.getElementById('deleteVendorsLoading').textContent = 'No vendors found in Zoho.';
    document.getElementById('deleteVendorsLoading').style.display = 'block';
    document.getElementById('deleteVendorsTable').style.display = 'none';
    return;
  }

  var sorted = vendors.slice().sort(function(a, b) {
    if (a.bill_count !== b.bill_count) return a.bill_count - b.bill_count;
    return a.contact_name.localeCompare(b.contact_name);
  });

  var emptyCount = 0;
  sorted.forEach(function(v) {
    if (v.bill_count === 0) emptyCount++;
    var tr = document.createElement('tr');
    tr.setAttribute('data-vendorid', v.contact_id);
    tr.setAttribute('data-bills', v.bill_count);
    tr.setAttribute('data-name', (v.contact_name || '').toLowerCase());

    var tdCb = document.createElement('td');
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'vendor-del-cb';
    cb.value = v.contact_id;
    cb.onchange = function() {
      if (this.checked) _deleteVendorSelectedIds.add(v.contact_id);
      else _deleteVendorSelectedIds.delete(v.contact_id);
      _updateDeleteVendorsBtn();
    };
    tdCb.appendChild(cb);
    tr.appendChild(tdCb);

    var tdName = document.createElement('td');
    tdName.textContent = v.contact_name;
    tdName.style.fontWeight = '500';
    tr.appendChild(tdName);

    var tdBills = document.createElement('td');
    tdBills.textContent = v.bill_count;
    tdBills.style.cssText = v.bill_count === 0 ? 'color:#ef4444;font-weight:600' : 'color:var(--green);font-weight:600';
    tr.appendChild(tdBills);

    var tdOut = document.createElement('td');
    tdOut.textContent = v.outstanding ? Number(v.outstanding).toLocaleString() : '0';
    tr.appendChild(tdOut);

    var tdStatus = document.createElement('td');
    tdStatus.textContent = v.status || '-';
    tdStatus.style.cssText = 'font-size:10px;text-transform:uppercase;color:var(--text-dim)';
    tr.appendChild(tdStatus);

    tbody.appendChild(tr);
  });

  document.getElementById('vendorCountLabel').textContent = vendors.length + ' vendors, ' + emptyCount + ' empty';
  document.getElementById('deleteVendorsLoading').style.display = 'none';
  document.getElementById('deleteVendorsTable').style.display = 'table';
  filterDeleteVendors();
}

function filterDeleteVendors() {
  var search = (document.getElementById('vendorSearchBar').value || '').toLowerCase();
  var emptyOnly = document.getElementById('vendorShowEmpty').checked;
  var rows = document.querySelectorAll('#deleteVendorsTableBody tr');
  var shown = 0;
  rows.forEach(function(r) {
    var name = r.getAttribute('data-name') || '';
    var bills = parseInt(r.getAttribute('data-bills') || '0');
    var matchSearch = !search || name.indexOf(search) >= 0;
    var matchEmpty = !emptyOnly || bills === 0;
    if (matchSearch && matchEmpty) { r.style.display = ''; shown++; }
    else { r.style.display = 'none'; }
  });
  document.getElementById('vendorCountLabel').textContent = shown + ' shown' + (emptyOnly ? ' (empty only)' : '');
}

function toggleVendorSelectAll(masterCb) {
  var rows = document.querySelectorAll('#deleteVendorsTableBody tr:not([style*="display: none"])');
  rows.forEach(function(r) {
    var cb = r.querySelector('.vendor-del-cb');
    if (cb) {
      cb.checked = masterCb.checked;
      if (masterCb.checked) _deleteVendorSelectedIds.add(cb.value);
      else _deleteVendorSelectedIds.delete(cb.value);
    }
  });
  _updateDeleteVendorsBtn();
}

function _updateDeleteVendorsBtn() {
  var btn = document.getElementById('deleteVendorsSelectedBtn');
  if (_deleteVendorSelectedIds.size > 0) {
    btn.style.display = '';
    btn.textContent = 'Delete Selected (' + _deleteVendorSelectedIds.size + ')';
  } else {
    btn.style.display = 'none';
  }
}

function confirmDeleteVendorsSelected() {
  var count = _deleteVendorSelectedIds.size;
  if (!count) return;
  if (!confirm('Are you sure you want to permanently delete ' + count + ' vendor(s) from Zoho? This cannot be undone.')) return;
  var ids = Array.from(_deleteVendorSelectedIds);
  var btn = document.getElementById('deleteVendorsSelectedBtn');
  btn.textContent = 'Deleting...';
  btn.disabled = true;

  fetch('/api/vendors/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({contact_ids: ids})
  })
  .then(r => r.json())
  .then(data => {
    btn.disabled = false;
    if (data.error) { alert('Error: ' + data.error); _updateDeleteVendorsBtn(); return; }
    var ok = (data.succeeded || []).length;
    var fail = (data.failed || []).length;
    alert('Deleted ' + ok + ' vendor(s)' + (fail ? ', ' + fail + ' failed' : ''));
    (data.succeeded || []).forEach(function(cid) {
      _deleteVendorSelectedIds.delete(cid);
      var row = document.querySelector('#deleteVendorsTableBody tr[data-vendorid="' + cid + '"]');
      if (row) row.remove();
    });
    _updateDeleteVendorsBtn();
    _deleteVendorsData = _deleteVendorsData.filter(function(v) { return !(data.succeeded || []).includes(v.contact_id); });
    var emptyCount = _deleteVendorsData.filter(function(v) { return v.bill_count === 0; }).length;
    document.getElementById('vendorCountLabel').textContent = _deleteVendorsData.length + ' vendors, ' + emptyCount + ' empty';
  })
  .catch(function(err) {
    btn.disabled = false;
    alert('Failed: ' + err);
    _updateDeleteVendorsBtn();
  });
}

function filterReviewTable() {
  const vendor = document.getElementById('reviewVendorFilter').value;
  const rows = document.querySelectorAll('#reviewTableBody tr');
  let shown = 0, bills = 0;
  rows.forEach(r => {
    const rv = r.getAttribute('data-vendor');
    if (!rv) return;
    if (!vendor || rv === vendor) {
      r.style.display = '';
      if (!r.classList.contains('vendor-group-header')) bills++;
      shown++;
    } else {
      r.style.display = 'none';
    }
  });
  document.getElementById('reviewCountLabel').textContent = vendor ? bills + ' bills shown' : '';
}

function renderReviewTable(bills) {
  const tbody = document.getElementById('reviewTableBody');
  tbody.innerHTML = '';

  if (!bills.length) {
    const el = document.getElementById('reviewLoading');
    el.textContent = 'No bills found. Run Step 3 first.';
    el.style.display = 'block';
    document.getElementById('reviewTable').style.display = 'none';
    return;
  }

  // Group bills by vendor name
  const vendorGroups = {};
  bills.forEach((bill, idx) => {
    const vn = bill.vendor_name || 'Unknown';
    if (!vendorGroups[vn]) vendorGroups[vn] = [];
    vendorGroups[vn].push({...bill, _origIdx: idx});
  });

  // Sort vendor names alphabetically
  const sortedVendors = Object.keys(vendorGroups).sort();

  // Populate vendor filter dropdown
  const filterSel = document.getElementById('reviewVendorFilter');
  const prevVal = filterSel.value;
  filterSel.innerHTML = '<option value="">All Vendors (' + bills.length + ')</option>';
  sortedVendors.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v + ' (' + vendorGroups[v].length + ')';
    filterSel.appendChild(opt);
  });
  filterSel.value = prevVal;
  document.getElementById('reviewFilterBar').style.display = 'flex';
  document.getElementById('reviewCountLabel').textContent = sortedVendors.length + ' vendors, ' + bills.length + ' bills';

  let globalIdx = 0;
  sortedVendors.forEach(vendorName => {
    const group = vendorGroups[vendorName];

    // --- Vendor group header row with Apply All ---
    const headerTr = document.createElement('tr');
    headerTr.className = 'vendor-group-header';
    headerTr.setAttribute('data-vendor', vendorName);
    const headerTd = document.createElement('td');
    headerTd.colSpan = 8;

    const bulkRow = document.createElement('div');
    bulkRow.className = 'vendor-bulk-row';

    const nameLabel = document.createElement('span');
    nameLabel.className = 'vendor-name-label';
    nameLabel.innerHTML = vendorName + ' <span class="vendor-bill-count">(' + group.length + ' bill' + (group.length > 1 ? 's' : '') + ')</span>';
    bulkRow.appendChild(nameLabel);

    // Only show Apply All controls if vendor has multiple bills
    if (group.length > 1) {
      const bulkSelect = document.createElement('select');
      bulkSelect.className = 'vendor-bulk-select';
      bulkSelect.id = 'bulkSelect-' + vendorName.replace(/[^a-zA-Z0-9]/g, '_');
      populateAccountSelect(bulkSelect, '-- select account --');
      bulkRow.appendChild(bulkSelect);

      const applyBtn = document.createElement('button');
      applyBtn.className = 'apply-all-btn';
      applyBtn.textContent = 'Apply All';
      applyBtn.id = 'bulkBtn-' + vendorName.replace(/[^a-zA-Z0-9]/g, '_');
      applyBtn.onclick = function() { bulkSaveVendorAccount(vendorName, group); };
      bulkRow.appendChild(applyBtn);
    }

    headerTd.appendChild(bulkRow);
    headerTr.appendChild(headerTd);
    tbody.appendChild(headerTr);

    // --- Individual bill rows ---
    group.forEach(bill => {
      const idx = globalIdx++;
      const tr = document.createElement('tr');
      tr.setAttribute('data-vendor', vendorName);

      // Vendor (dimmed since header shows it)
      const tdVendor = document.createElement('td');
      tdVendor.textContent = bill.vendor_name;
      tdVendor.style.color = 'var(--text-dim)';
      tdVendor.style.paddingLeft = '20px';
      tr.appendChild(tdVendor);

      // Description
      const tdDesc = document.createElement('td');
      tdDesc.textContent = bill.description || '-';
      tdDesc.style.color = 'var(--text-dim)';
      tdDesc.style.fontSize = '11px';
      tdDesc.style.maxWidth = '250px';
      tdDesc.style.overflow = 'hidden';
      tdDesc.style.textOverflow = 'ellipsis';
      tdDesc.style.whiteSpace = 'nowrap';
      tdDesc.title = bill.description || '';
      tr.appendChild(tdDesc);

      // Line Items
      const tdLineItems = document.createElement('td');
      const liArr = bill.line_items || [];
      tdLineItems.style.cssText = 'color:var(--text-dim);font-size:10px;white-space:normal;min-width:200px';
      tdLineItems.textContent = liArr.length > 0 ? liArr.join(', ') : '-';
      tdLineItems.title = liArr.join('\n');
      tr.appendChild(tdLineItems);

      // Amount
      const tdAmount = document.createElement('td');
      tdAmount.textContent = bill.amount != null ? Number(bill.amount).toLocaleString() : '-';
      tr.appendChild(tdAmount);

      // Currency
      const tdCurrency = document.createElement('td');
      tdCurrency.textContent = bill.currency || 'INR';
      tr.appendChild(tdCurrency);

      // Current Account
      const tdCurrent = document.createElement('td');
      tdCurrent.textContent = bill.account_name || '-';
      tdCurrent.style.color = 'var(--text-dim)';
      tdCurrent.id = 'currentAcct-' + idx;
      tr.appendChild(tdCurrent);

      // Change To (dropdown)
      const tdChange = document.createElement('td');
      const select = document.createElement('select');
      select.id = 'selectAcct-' + idx;
      populateAccountSelect(select, '-- keep current --');
      tdChange.appendChild(select);
      tr.appendChild(tdChange);

      // Action (Save button)
      const tdAction = document.createElement('td');
      const btn = document.createElement('button');
      btn.className = 'review-save-btn';
      btn.textContent = 'Save';
      btn.id = 'saveBtn-' + idx;
      btn.setAttribute('data-bill-id', bill.bill_id);
      btn.onclick = function() { saveAccountChange(bill.bill_id, idx, bill.vendor_name); };
      tdAction.appendChild(btn);
      tr.appendChild(tdAction);

      tbody.appendChild(tr);
    });
  });

  // Store bills globally for bulk operations
  window._reviewBills = bills;
  window._reviewVendorGroups = vendorGroups;

  document.getElementById('reviewLoading').style.display = 'none';
  document.getElementById('reviewTable').style.display = 'table';
}

function saveAccountChange(billId, idx, vendorName) {
  const select = document.getElementById('selectAcct-' + idx);
  const btn = document.getElementById('saveBtn-' + idx);
  const accountId = select.value;
  if (!accountId) {
    addLogLine('[Review] No account selected for row ' + idx);
    return;
  }
  const accountName = select.options[select.selectedIndex].getAttribute('data-name') || '';

  btn.disabled = true;
  btn.textContent = 'Saving...';

  fetch('/api/review/update-account', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_id: billId, account_id: accountId, account_name: accountName, vendor_name: vendorName}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      btn.textContent = 'Saved';
      btn.className = 'review-save-btn saved';
      document.getElementById('currentAcct-' + idx).textContent = accountName;
    } else {
      btn.textContent = 'Error';
      btn.className = 'review-save-btn save-error';
      addLogLine('[Review] Error: ' + (data.error || 'Unknown'));
    }
    btn.disabled = false;
  })
  .catch(err => {
    btn.textContent = 'Error';
    btn.className = 'review-save-btn save-error';
    btn.disabled = false;
    addLogLine('[Review] Request failed: ' + err);
  });
}

function bulkSaveVendorAccount(vendorName, group) {
  const safeVendor = vendorName.replace(/[^a-zA-Z0-9]/g, '_');
  const select = document.getElementById('bulkSelect-' + safeVendor);
  const btn = document.getElementById('bulkBtn-' + safeVendor);
  const accountId = select.value;
  if (!accountId) {
    addLogLine('[Review] No account selected for ' + vendorName);
    return;
  }
  const accountName = select.options[select.selectedIndex].getAttribute('data-name') || '';
  const billIds = group.map(b => b.bill_id);

  btn.disabled = true;
  btn.textContent = 'Applying...';

  fetch('/api/review/bulk-update-account', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_ids: billIds, account_id: accountId, account_name: accountName, vendor_name: vendorName}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      const okCount = (data.succeeded || []).length;
      const failCount = (data.failed || []).length;
      btn.textContent = okCount + '/' + billIds.length + ' Done';
      btn.className = failCount ? 'apply-all-btn save-error' : 'apply-all-btn saved';
      addLogLine('[Review] ' + vendorName + ': ' + okCount + ' bills updated to ' + accountName);

      // Update individual row UI for succeeded bills
      document.querySelectorAll('tr[data-vendor="' + vendorName + '"]').forEach(row => {
        const saveBtn = row.querySelector('.review-save-btn');
        if (!saveBtn) return;
        const billId = saveBtn.getAttribute('data-bill-id');
        if ((data.succeeded || []).includes(billId)) {
          saveBtn.textContent = 'Saved';
          saveBtn.className = 'review-save-btn saved';
          // Update current account label
          const idx = saveBtn.id.replace('saveBtn-', '');
          const currentEl = document.getElementById('currentAcct-' + idx);
          if (currentEl) currentEl.textContent = accountName;
        }
      });
    } else {
      btn.textContent = 'Error';
      btn.className = 'apply-all-btn save-error';
      addLogLine('[Review] Bulk error for ' + vendorName + ': ' + (data.error || 'Unknown'));
    }
    btn.disabled = false;
  })
  .catch(err => {
    btn.textContent = 'Error';
    btn.className = 'apply-all-btn save-error';
    btn.disabled = false;
    addLogLine('[Review] Bulk request failed: ' + err);
  });
}

// --- Create Account Modal ---
function openCreateAccountModal() {
  document.getElementById('newAccountName').value = '';
  document.getElementById('newAccountDesc').value = '';
  document.getElementById('createAccountModal').style.display = 'flex';
}

function closeCreateAccountModal() {
  document.getElementById('createAccountModal').style.display = 'none';
}

function createNewAccount() {
  const name = document.getElementById('newAccountName').value.trim();
  const desc = document.getElementById('newAccountDesc').value.trim();
  if (!name) return;

  const btn = document.getElementById('createAccountBtn');
  btn.disabled = true;
  btn.textContent = 'Creating...';

  fetch('/api/review/create-account', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name, description: desc}),
  })
  .then(r => r.json())
  .then(data => {
    btn.disabled = false;
    btn.textContent = 'Create';
    if (data.ok) {
      // Add to cached accounts list and all dropdowns
      const newAcct = {account_id: data.account_id, account_name: data.account_name};
      _reviewAccounts.push(newAcct);
      _reviewAccounts.sort((a, b) => a.account_name.localeCompare(b.account_name));

      // Update all select dropdowns
      document.querySelectorAll('[id^="selectAcct-"]').forEach(select => {
        const opt = document.createElement('option');
        opt.value = data.account_id;
        opt.textContent = data.account_name;
        opt.setAttribute('data-name', data.account_name);
        // Insert sorted
        let inserted = false;
        for (let i = 1; i < select.options.length; i++) {
          if (select.options[i].textContent > data.account_name) {
            select.insertBefore(opt, select.options[i]);
            inserted = true;
            break;
          }
        }
        if (!inserted) select.appendChild(opt);
      });

      closeCreateAccountModal();
      addLogLine('[Review] Created account: ' + data.account_name);
    } else {
      addLogLine('[Review] Error creating account: ' + (data.error || 'Unknown'));
    }
  })
  .catch(err => {
    btn.disabled = false;
    btn.textContent = 'Create';
    addLogLine('[Review] Request failed: ' + err);
  });
}

// --- Invoice Upload & Extract ---
function handleInvoiceUpload(input) {
  const files = input.files;
  if (!files || !files.length) return;

  const formData = new FormData();
  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }

  addLogLine('[Upload] Uploading ' + files.length + ' invoice file(s)...');

  fetch('/api/upload/invoices', {method: 'POST', body: formData})
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        addLogLine('[Upload] Saved: ' + data.files.join(', '));
        addLogLine('[Upload] Extraction started in background...');
        pollStatus();
      } else {
        addLogLine('[Upload] Error: ' + (data.error || 'Unknown'));
      }
    })
    .catch(err => addLogLine('[Upload] Request failed: ' + err));

  input.value = '';
}

// --- Extract Preview ---
let _extractPreviewData = null;
let _extractPreviewTab = 'extracted';
let _extractPreviewSeen = {};  // step_id -> last timestamp we auto-opened for

function _maybeAutoOpenExtractPreview(stepId, res) {
  if (!res || res.status !== 'success') return;
  var ts = res.timestamp || '';
  if (_extractPreviewSeen[stepId] === ts) return;
  // First observation since page load: don't auto-open stale historical runs
  if (_extractPreviewSeen[stepId] === undefined) {
    _extractPreviewSeen[stepId] = ts;
    return;
  }
  _extractPreviewSeen[stepId] = ts;
  // Only auto-open if the panel isn't already showing
  if (document.getElementById('extractPreviewPanel').style.display !== 'flex') {
    openExtractPreview();
  } else {
    // Panel is open — refresh contents
    openExtractPreview();
  }
}

function openExtractPreview() {
  ['logPanel','reviewPanel','matchPanel','comparePanel','checkPanel','invoiceBrowsePanel','paymentPanel','deleteBillsPanel','deleteVendorsPanel','bankingSummaryPanel','autoMatchPanel','extractPreviewPanel','ccPreviewPanel'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.getElementById('extractPreviewPanel').style.display = 'flex';
  document.getElementById('extractPreviewLoading').style.display = 'block';
  document.getElementById('extractPreviewLoading').textContent = 'Loading preview...';
  document.getElementById('extractPreviewContent').style.display = 'none';

  fetch('/api/extract/preview')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('extractPreviewLoading').textContent = data.error;
        return;
      }
      if (data.empty) {
        document.getElementById('extractPreviewLoading').textContent = 'No extract run yet. Run Upload & Extract or Mail Extract to see a preview.';
        return;
      }
      _extractPreviewData = data;
      renderExtractPreview();
    })
    .catch(function(err) {
      document.getElementById('extractPreviewLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeExtractPreview() {
  document.getElementById('extractPreviewPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderExtractPreview() {
  if (!_extractPreviewData) return;
  var d = _extractPreviewData;
  var ext = d.extracted || [];
  var fail = d.failed || [];
  var skip = d.skipped || [];

  // Header info
  var srcLabels = {upload: 'Upload & Extract', mail: 'Mail Extract', step2: 'Step 2 Extract'};
  var srcLabel = srcLabels[d.source] || d.source || '-';
  var ts = d.timestamp ? new Date(d.timestamp).toLocaleString() : '';
  document.getElementById('extractPreviewSource').textContent = '(' + srcLabel + (ts ? ' · ' + ts : '') + ')';
  document.getElementById('extractPreviewSummary').textContent =
    ext.length + ' extracted · ' + fail.length + ' failed · ' + skip.length + ' skipped';

  document.getElementById('epExtractedCount').textContent = ext.length;
  document.getElementById('epFailedCount').textContent = fail.length;
  document.getElementById('epSkippedCount').textContent = skip.length;

  // Extracted body
  var eb = document.getElementById('epExtractedBody');
  if (ext.length === 0) {
    eb.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:16px;color:var(--text-dim)">No invoices extracted in this run</td></tr>';
  } else {
    eb.innerHTML = ext.map(function(inv) {
      var amt = inv.amount != null ? (inv.currency || '') + ' ' + fmt(inv.amount) : '-';
      return '<tr>'
        + '<td>' + escHtml(fmtDate(inv.date || '')) + '</td>'
        + '<td>' + escHtml(inv.vendor_name || '-') + '</td>'
        + '<td>' + escHtml(inv.invoice_number || '-') + '</td>'
        + '<td style="text-align:right">' + amt + '</td>'
        + '<td style="font-size:11px;color:var(--text-dim)">' + escHtml(inv.file || '-') + '</td>'
        + '</tr>';
    }).join('');
  }

  // Failed body
  var fb = document.getElementById('epFailedBody');
  if (fail.length === 0) {
    fb.innerHTML = '<tr><td colspan="2" style="text-align:center;padding:16px;color:var(--text-dim)">No failures in this run</td></tr>';
  } else {
    fb.innerHTML = fail.map(function(f) {
      return '<tr>'
        + '<td>' + escHtml(f.file || '-') + '</td>'
        + '<td style="color:var(--red)">' + escHtml(f.reason || 'Unknown') + '</td>'
        + '</tr>';
    }).join('');
  }

  // Skipped body
  var sb = document.getElementById('epSkippedBody');
  if (skip.length === 0) {
    sb.innerHTML = '<tr><td colspan="2" style="text-align:center;padding:16px;color:var(--text-dim)">Nothing skipped in this run</td></tr>';
  } else {
    sb.innerHTML = skip.map(function(s) {
      return '<tr>'
        + '<td>' + escHtml(s.file || '-') + '</td>'
        + '<td style="color:var(--text-dim)">' + escHtml(s.reason || '-') + '</td>'
        + '</tr>';
    }).join('');
  }

  document.getElementById('extractPreviewLoading').style.display = 'none';
  document.getElementById('extractPreviewContent').style.display = 'block';
  switchExtractPreviewTab(_extractPreviewTab);
}

function switchExtractPreviewTab(tab) {
  _extractPreviewTab = tab;
  var tabs = document.querySelectorAll('#extractPreviewTabs .ep-tab');
  tabs.forEach(function(t) {
    var active = t.getAttribute('data-tab') === tab;
    t.style.color = active ? 'var(--text)' : 'var(--text-dim)';
    t.style.fontWeight = active ? '500' : '400';
    var color = tab === 'extracted' ? 'var(--green)' : (tab === 'failed' ? 'var(--red)' : 'var(--text-dim)');
    t.style.borderBottom = active ? '2px solid ' + color : '2px solid transparent';
  });
  document.getElementById('epExtractedTable').style.display = tab === 'extracted' ? '' : 'none';
  document.getElementById('epFailedTable').style.display = tab === 'failed' ? '' : 'none';
  document.getElementById('epSkippedTable').style.display = tab === 'skipped' ? '' : 'none';
}

// --- CC Parse Preview ---
let _ccPreviewData = null;
let _ccPreviewTab = 'parsed';
let _ccPreviewSeenTs = undefined;

function openCCPreview() {
  ['logPanel','reviewPanel','matchPanel','comparePanel','checkPanel','invoiceBrowsePanel','paymentPanel','deleteBillsPanel','deleteVendorsPanel','bankingSummaryPanel','autoMatchPanel','extractPreviewPanel','ccPreviewPanel'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.getElementById('ccPreviewPanel').style.display = 'flex';
  document.getElementById('ccPreviewLoading').style.display = 'block';
  document.getElementById('ccPreviewLoading').textContent = 'Loading preview...';
  document.getElementById('ccPreviewContent').style.display = 'none';

  fetch('/api/cc/preview')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('ccPreviewLoading').textContent = data.error;
        return;
      }
      if (data.empty) {
        document.getElementById('ccPreviewLoading').textContent = 'No CC parse run yet. Upload & Extract CC statements to see a preview.';
        return;
      }
      _ccPreviewData = data;
      renderCCPreview();
    })
    .catch(function(err) {
      document.getElementById('ccPreviewLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeCCPreview() {
  document.getElementById('ccPreviewPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderCCPreview() {
  if (!_ccPreviewData) return;
  var d = _ccPreviewData;
  var parsed = d.parsed || [];
  var failed = d.failed || [];
  var skipped = d.skipped || [];

  var ts = d.timestamp ? new Date(d.timestamp).toLocaleString() : '';
  document.getElementById('ccPreviewSource').textContent = ts ? '(' + ts + ')' : '';
  var totalTxns = d.total_transactions || 0;
  document.getElementById('ccPreviewSummary').textContent =
    parsed.length + ' cards · ' + totalTxns + ' txns · ' + failed.length + ' failed · ' + skipped.length + ' skipped';

  document.getElementById('cpParsedCount').textContent = parsed.length;
  document.getElementById('cpFailedCount').textContent = failed.length;
  document.getElementById('cpSkippedCount').textContent = skipped.length;

  // Parsed: collapsible per-card groups
  var pc = document.getElementById('cpParsedContent');
  if (parsed.length === 0) {
    pc.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-dim)">No cards parsed in this run</div>';
  } else {
    pc.innerHTML = parsed.map(function(g, idx) {
      var debitCount = 0, creditCount = 0;
      var rows = (g.transactions || []).map(function(t) {
        var amt = Number(t.amount) || 0;
        // Prefer explicit type field; fall back to sign (parser convention: negative = credit)
        var type = (t.type || '').toLowerCase();
        if (type !== 'debit' && type !== 'credit') type = amt < 0 ? 'credit' : 'debit';
        if (type === 'credit') creditCount++; else debitCount++;
        var display = (type === 'credit' ? '+' : '') + fmt(Math.abs(amt));
        var color = type === 'credit' ? 'var(--green)' : 'var(--text)';
        var badge = type === 'credit'
          ? '<span style="background:rgba(74,222,128,0.15);color:var(--green);font-size:10px;padding:1px 6px;border-radius:10px;margin-left:6px;font-weight:500">CR</span>'
          : '<span style="background:rgba(108,140,255,0.12);color:var(--text-dim);font-size:10px;padding:1px 6px;border-radius:10px;margin-left:6px;font-weight:500">DR</span>';
        return '<tr>'
          + '<td style="width:90px">' + escHtml(fmtDate(t.date || '')) + '</td>'
          + '<td>' + escHtml(t.description || '') + '</td>'
          + '<td style="text-align:right;color:' + color + ';font-weight:' + (type === 'credit' ? '500' : '400') + '">' + display + badge + '</td>'
          + '</tr>';
      }).join('');
      var summaryCounts = debitCount + ' DR · ' + creditCount + ' CR';
      return '<details ' + (idx === 0 ? 'open' : '') + ' style="margin-bottom:8px;border:1px solid var(--border);border-radius:6px;overflow:hidden">'
        + '<summary style="padding:8px 12px;background:rgba(255,255,255,0.03);cursor:pointer;font-size:13px;font-weight:500;display:flex;justify-content:space-between;align-items:center">'
        + '<span>' + escHtml(g.card) + '</span>'
        + '<span style="color:var(--text-dim);font-size:11px;font-weight:400">' + g.count + ' txns · ' + summaryCounts + '</span>'
        + '</summary>'
        + '<table class="match-table" style="margin:0"><thead><tr><th style="width:90px">Date</th><th>Description</th><th style="text-align:right;width:160px">Amount</th></tr></thead><tbody>'
        + rows
        + '</tbody></table>'
        + '</details>';
    }).join('');
  }

  // Failed
  var fb = document.getElementById('cpFailedBody');
  if (failed.length === 0) {
    fb.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:16px;color:var(--text-dim)">No failures in this run</td></tr>';
  } else {
    fb.innerHTML = failed.map(function(f) {
      return '<tr>'
        + '<td>' + escHtml(f.file || '-') + '</td>'
        + '<td>' + escHtml(f.card || '-') + '</td>'
        + '<td style="color:var(--red)">' + escHtml(f.reason || 'Unknown') + '</td>'
        + '</tr>';
    }).join('');
  }

  // Skipped
  var sb = document.getElementById('cpSkippedBody');
  if (skipped.length === 0) {
    sb.innerHTML = '<tr><td colspan="2" style="text-align:center;padding:16px;color:var(--text-dim)">Nothing skipped in this run</td></tr>';
  } else {
    sb.innerHTML = skipped.map(function(s) {
      return '<tr>'
        + '<td>' + escHtml(s.card || '-') + '</td>'
        + '<td style="color:var(--text-dim)">' + escHtml(s.reason || '-') + '</td>'
        + '</tr>';
    }).join('');
  }

  document.getElementById('ccPreviewLoading').style.display = 'none';
  document.getElementById('ccPreviewContent').style.display = 'block';
  switchCCPreviewTab(_ccPreviewTab);
}

function switchCCPreviewTab(tab) {
  _ccPreviewTab = tab;
  var tabs = document.querySelectorAll('#ccPreviewTabs .cp-tab');
  tabs.forEach(function(t) {
    var active = t.getAttribute('data-tab') === tab;
    t.style.color = active ? 'var(--text)' : 'var(--text-dim)';
    t.style.fontWeight = active ? '500' : '400';
    var color = tab === 'parsed' ? 'var(--green)' : (tab === 'failed' ? 'var(--red)' : 'var(--text-dim)');
    t.style.borderBottom = active ? '2px solid ' + color : '2px solid transparent';
  });
  document.getElementById('cpParsedContent').style.display = tab === 'parsed' ? '' : 'none';
  document.getElementById('cpFailedTable').style.display = tab === 'failed' ? '' : 'none';
  document.getElementById('cpSkippedTable').style.display = tab === 'skipped' ? '' : 'none';
}

function openImportFromCCPreview() {
  // Close the preview panel and open the existing import picker modal
  closeCCPreview();
  openImportPicker();
}

function _maybeAutoOpenCCPreview(res) {
  if (!res || res.status !== 'success') return;
  var ts = res.timestamp || '';
  if (_ccPreviewSeenTs === ts) return;
  if (_ccPreviewSeenTs === undefined) {
    _ccPreviewSeenTs = ts;
    return;
  }
  _ccPreviewSeenTs = ts;
  openCCPreview();
}

// --- CC Upload ---
let _lastUploadedFiles = [];  // track uploaded files for import filtering

function handleCCUpload(input) {
  const files = input.files;
  if (!files || !files.length) return;

  const formData = new FormData();
  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }

  addLogLine('[Upload] Uploading ' + files.length + ' CC statement(s)...');

  fetch('/api/upload/cc', {method: 'POST', body: formData})
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        _lastUploadedFiles = data.files;
        addLogLine('[Upload] Saved: ' + data.files.join(', '));
        runStepWithKwargs('4', {selected_files: data.files});
      } else {
        addLogLine('[Upload] Error: ' + (data.error || 'Unknown'));
      }
    })
    .catch(err => addLogLine('[Upload] Request failed: ' + err));

  input.value = '';
}

// --- Import Picker ---
function clearPaymentsCache() {
  fetch('/api/payments/clear-cache', {method: 'POST'})
    .then(r => r.json())
    .then(d => appendLog('[INFO] ' + d.message))
    .catch(e => appendLog('[ERROR] ' + e));
}

function clearParsedCC() {
  showModal(
    'Clear Parsed CC Data',
    'This will delete cc_transactions.json and all CSV files so Step 4 re-parses fresh on next upload.',
    () => {
      fetch('/api/cc/clear-parsed', {method: 'POST'})
        .then(r => r.json())
        .then(d => appendLog('[INFO] ' + d.message))
        .catch(e => appendLog('[ERROR] ' + e));
    }, true, 'Yes, Clear'
  );
}

function clearImportCache() {
  fetch('/api/banking/clear-cache', {method: 'POST'})
    .then(r => r.json())
    .then(d => appendLog('[INFO] ' + d.message))
    .catch(e => appendLog('[ERROR] ' + e));
}

// --- Auto Match Banking ---
var _autoMatchData = null;
var _amSelectedMatches = new Set();

function openAutoMatchPanel() {
  // Hide other panels
  ['logPanel','reviewPanel','matchPanel','comparePanel','checkPanel','invoiceBrowsePanel','paymentPanel','deleteBillsPanel','deleteVendorsPanel','bankingSummaryPanel','extractPreviewPanel','ccPreviewPanel'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.getElementById('autoMatchPanel').style.display = 'flex';
  document.getElementById('autoMatchLoading').style.display = 'block';
  document.getElementById('autoMatchContent').style.display = 'none';
  document.getElementById('matchSelectedBtn').style.display = 'none';
  _amSelectedMatches.clear();

  fetch('/api/banking/auto-match-preview')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('autoMatchLoading').textContent = data.error;
        return;
      }
      _autoMatchData = data;
      renderAutoMatch(data);
      filterAutoMatch();
    })
    .catch(function(err) {
      document.getElementById('autoMatchLoading').textContent = 'Failed: ' + err;
    });
}

function closeAutoMatchPanel() {
  document.getElementById('autoMatchPanel').style.display = 'none';
}

/* ── Banking Summary panel ── */
var _bsData = null;

function openBankingSummary(forceRefresh) {
  ['logPanel','reviewPanel','matchPanel','comparePanel','checkPanel','invoiceBrowsePanel','paymentPanel','deleteBillsPanel','deleteVendorsPanel','autoMatchPanel','extractPreviewPanel','ccPreviewPanel'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.getElementById('bankingSummaryPanel').style.display = 'flex';
  document.getElementById('bsLoading').style.display = 'block';
  document.getElementById('bsLoading').textContent = forceRefresh ? 'Fetching from Zoho...' : 'Loading...';
  document.getElementById('bsContent').style.display = 'none';

  var url = '/api/banking/summary' + (forceRefresh ? '?refresh=1' : '');
  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('bsLoading').textContent = data.error;
        return;
      }
      _bsData = data;
      // Build card-wise uncategorized counts for filter dropdown
      var cardUncat = {};
      (data.months || []).forEach(function(m) {
        (m.cards || []).forEach(function(c) {
          cardUncat[c.card] = (cardUncat[c.card] || 0) + (c.uncategorized || 0);
        });
      });
      var sel = document.getElementById('bsCardFilter');
      sel.innerHTML = '<option value="">All Cards</option>';
      (data.card_names || []).forEach(function(name) {
        var opt = document.createElement('option');
        opt.value = name;
        var uc = cardUncat[name] || 0;
        opt.textContent = name + (uc > 0 ? ' (' + uc + ')' : '');
        if (uc > 0) opt.style.color = '#f87171';
        sel.appendChild(opt);
      });
      // Build FY dropdown from month keys present in data
      var fySet = {};
      (data.months || []).forEach(function(m) {
        var fy = _bsFyKey(m.month);
        if (fy != null) fySet[fy] = true;
      });
      var fyList = Object.keys(fySet).map(Number).sort(function(a, b) { return b - a; });
      var fySel = document.getElementById('bsFyFilter');
      var prevFy = fySel.value;
      fySel.innerHTML = '<option value="">All</option>';
      fyList.forEach(function(fy) {
        var opt = document.createElement('option');
        opt.value = String(fy);
        opt.textContent = _bsFyLabel(fy);
        fySel.appendChild(opt);
      });
      // Default to current FY if present
      var today = new Date();
      var curFy = today.getMonth() >= 3 ? today.getFullYear() : today.getFullYear() - 1;
      if (prevFy && fySet[prevFy]) {
        fySel.value = prevFy;
      } else if (fySet[curFy]) {
        fySel.value = String(curFy);
      }
      renderBankingSummary(data);
    })
    .catch(function(err) {
      document.getElementById('bsLoading').textContent = 'Failed: ' + err;
    });
}

function closeBankingSummary() {
  document.getElementById('bankingSummaryPanel').style.display = 'none';
}

function filterBankingSummary() {
  if (!_bsData) return;
  renderBankingSummary(_bsData);
}

function renderBankingSummary(data) {
  var fmt = function(n) { return n != null ? Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '0.00'; };
  var cardFilter = document.getElementById('bsCardFilter').value;
  var fyFilter = document.getElementById('bsFyFilter').value;
  var months = data.months || [];
  if (fyFilter !== '') {
    var fyNum = parseInt(fyFilter);
    months = months.filter(function(m) { return _bsFyKey(m.month) === fyNum; });
  }

  document.getElementById('bsLoading').style.display = 'none';
  document.getElementById('bsContent').style.display = 'block';

  // Aggregate filtered grand totals
  var G = {matched:0, manually_added:0, categorized:0, uncategorized:0, total:0,
           matched_amount:0, manually_added_amount:0, categorized_amount:0, uncategorized_amount:0, total_amount:0};
  months.forEach(function(m) {
    var src = cardFilter ? null : m.totals;
    if (cardFilter) {
      (m.cards || []).forEach(function(c) {
        if (c.card === cardFilter) src = c;
      });
    }
    if (!src) return;
    for (var k in G) G[k] += (src[k] || 0);
  });

  var doneCount = G.matched + G.manually_added + G.categorized;
  var donePct = G.total > 0 ? Math.round(doneCount / G.total * 100) : 0;
  document.getElementById('bsSummaryText').textContent = G.total + ' txns | ' + donePct + '% done';

  // Show cache info
  var cacheEl = document.getElementById('bsCacheInfo');
  if (data.fetched_at) cacheEl.textContent = (data.cached ? 'Cached: ' : 'Fetched: ') + data.fetched_at;

  var html = '';

  // Stat cards row
  html += '<div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">';
  html += _bsStatCard('Total', G.total, fmt(G.total_amount), 'var(--accent)');
  html += _bsStatCard('Matched', G.matched, fmt(G.matched_amount), '#60a5fa');
  html += _bsStatCard('Manually Added', G.manually_added, fmt(G.manually_added_amount), '#a78bfa');
  html += _bsStatCard('Categorized', G.categorized, fmt(G.categorized_amount), 'var(--green)');
  html += _bsStatCard('Uncategorized', G.uncategorized, fmt(G.uncategorized_amount), 'var(--red)');
  html += '</div>';

  // Stacked progress bar
  html += '<div style="background:var(--surface2);border-radius:6px;height:24px;margin-bottom:18px;overflow:hidden;display:flex">';
  if (G.total > 0) {
    var segs = [
      {n: G.matched, color: '#60a5fa', label: 'Matched'},
      {n: G.manually_added, color: '#a78bfa', label: 'Manual'},
      {n: G.categorized, color: 'var(--green)', label: 'Categorized'},
      {n: G.uncategorized, color: 'var(--red)', label: 'Uncat'}
    ];
    segs.forEach(function(s) {
      var w = (s.n / G.total * 100).toFixed(1);
      if (s.n > 0) {
        var fc = s.color === 'var(--red)' ? '#fff' : '#000';
        html += '<div title="' + s.label + ': ' + s.n + '" style="width:' + w + '%;background:' + s.color + ';display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:' + fc + '">' + (w > 5 ? s.n : '') + '</div>';
      }
    });
  }
  html += '</div>';

  // Month-wise table
  var th = 'padding:8px 6px;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim);';
  html += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
  html += '<thead><tr style="border-bottom:2px solid var(--border);text-align:left">';
  html += '<th style="' + th + '">Month</th>';
  if (!cardFilter) html += '<th style="' + th + '">Card</th>';
  html += '<th style="' + th + 'text-align:right">Total</th>';
  html += '<th style="' + th + 'text-align:right;color:#60a5fa">Matched</th>';
  html += '<th style="' + th + 'text-align:right;color:#a78bfa">Manual</th>';
  html += '<th style="' + th + 'text-align:right;color:var(--green)">Categorized</th>';
  html += '<th style="' + th + 'text-align:right;color:var(--red)">Uncategorized</th>';
  html += '<th style="' + th + 'text-align:right">Total Amt</th>';
  html += '<th style="' + th + 'text-align:right">Uncat Amt</th>';
  html += '<th style="' + th + 'text-align:center">Progress</th>';
  html += '</tr></thead><tbody>';

  months.forEach(function(m) {
    var rows = [];
    if (cardFilter) {
      (m.cards || []).forEach(function(c) {
        if (c.card === cardFilter) rows.push({card: c.card, d: c});
      });
    } else {
      if ((m.cards || []).length > 1) {
        (m.cards || []).forEach(function(c) {
          rows.push({card: c.card, d: c});
        });
      }
      rows.push({card: null, d: m.totals, isTotal: true});
    }

    rows.forEach(function(row, idx) {
      var d = row.d;
      var done = (d.matched || 0) + (d.manually_added || 0) + (d.categorized || 0);
      var pct = d.total > 0 ? Math.round(done / d.total * 100) : 0;
      var pctColor = pct === 100 ? 'var(--green)' : pct >= 70 ? 'var(--yellow)' : pct >= 40 ? 'var(--orange)' : 'var(--red)';
      var bold = row.isTotal ? 'font-weight:600;' : '';
      var bg = row.isTotal && (m.cards || []).length > 1 ? 'background:rgba(255,255,255,0.03);' : '';
      var bdr = (idx === rows.length - 1) ? 'border-bottom:1px solid var(--border);' : '';

      html += '<tr style="' + bold + bg + bdr + '">';
      var monthLabel = '';
      if (idx === 0) {
        monthLabel = _bsFormatMonth(m.month);
        var mUncat = cardFilter ? (rows.length > 0 ? (rows[0].d.uncategorized || 0) : 0) : (m.totals.uncategorized || 0);
        if (mUncat > 0) monthLabel += ' <span style="color:var(--red);font-weight:700;font-size:11px">(' + mUncat + ')</span>';
      }
      html += '<td style="padding:6px">' + monthLabel + '</td>';
      if (!cardFilter) html += '<td style="padding:6px;color:' + (row.isTotal ? 'var(--text)' : 'var(--text-dim)') + '">' + (row.card || 'All Cards') + '</td>';
      html += '<td style="padding:6px;text-align:right">' + d.total + '</td>';
      html += '<td style="padding:6px;text-align:right;color:#60a5fa">' + (d.matched || 0) + '</td>';
      html += '<td style="padding:6px;text-align:right;color:#a78bfa">' + (d.manually_added || 0) + '</td>';
      html += '<td style="padding:6px;text-align:right;color:var(--green)">' + (d.categorized || 0) + '</td>';
      html += '<td style="padding:6px;text-align:right;color:' + ((d.uncategorized || 0) > 0 ? 'var(--red)' : 'var(--text-dim)') + ';font-weight:' + ((d.uncategorized || 0) > 0 ? '700' : 'normal') + '">' + (d.uncategorized || 0) + '</td>';
      html += '<td style="padding:6px;text-align:right">' + fmt(d.total_amount) + '</td>';
      html += '<td style="padding:6px;text-align:right;color:' + ((d.uncategorized_amount || 0) > 0 ? 'var(--red)' : 'var(--text-dim)') + '">' + fmt(d.uncategorized_amount || 0) + '</td>';
      html += '<td style="padding:6px;text-align:center">';
      html += '<div style="display:flex;align-items:center;gap:6px;justify-content:center">';
      html += '<div style="width:60px;height:6px;background:var(--surface2);border-radius:3px;overflow:hidden"><div style="width:' + pct + '%;height:100%;background:' + pctColor + ';border-radius:3px"></div></div>';
      html += '<span style="font-size:10px;color:' + pctColor + '">' + pct + '%</span>';
      html += '</div></td>';
      html += '</tr>';
    });
  });

  html += '</tbody></table>';
  document.getElementById('bsContent').innerHTML = html;
}

function _bsStatCard(label, count, amount, color) {
  return '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 18px;min-width:140px;flex:1">'
    + '<div style="font-size:24px;font-weight:700;color:' + color + '">' + count + '</div>'
    + '<div style="font-size:11px;color:var(--text-dim);margin-top:2px">' + label + '</div>'
    + '<div style="font-size:12px;color:var(--text);margin-top:4px">&#8377; ' + amount + '</div>'
    + '</div>';
}

function _bsFormatMonth(ym) {
  if (!ym || ym === 'Unknown') return ym;
  var parts = ym.split('-');
  var names = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return (names[parseInt(parts[1])] || parts[1]) + ' ' + parts[0];
}

function _bsFyKey(ym) {
  if (!ym || ym === 'Unknown') return null;
  var parts = ym.split('-');
  var y = parseInt(parts[0]), m = parseInt(parts[1]);
  if (isNaN(y) || isNaN(m)) return null;
  return m >= 4 ? y : y - 1;
}

function _bsFyLabel(fyStart) {
  return 'FY ' + fyStart + '-' + String((fyStart + 1) % 100).padStart(2, '0');
}

// ========== Vendor Breakdown (inside Banking Summary) ==========
var _bsVendorMode = false;

function toggleVendorBreakdown() {
  _bsVendorMode = !_bsVendorMode;
  var btn = document.getElementById('bsVendorBtn');
  if (_bsVendorMode) {
    btn.textContent = 'Month View';
    btn.style.background = 'var(--accent)';
    btn.style.color = '#fff';
    document.getElementById('bsContent').style.display = 'none';
    document.getElementById('bsVendorContent').style.display = 'block';
    loadVendorBreakdown();
  } else {
    btn.textContent = 'Vendor Breakdown';
    btn.style.background = 'var(--orange)';
    btn.style.color = '#000';
    document.getElementById('bsContent').style.display = 'block';
    document.getElementById('bsVendorContent').style.display = 'none';
  }
}

function loadVendorBreakdown() {
  var el = document.getElementById('bsVendorContent');
  el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim)">Loading vendor breakdown...</div>';
  fetch('/api/banking/vendor-breakdown')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { el.innerHTML = '<div style="padding:20px;color:var(--red)">' + data.error + '</div>'; return; }
      renderVendorBreakdown(data);
    })
    .catch(function(err) {
      el.innerHTML = '<div style="padding:20px;color:var(--red)">Failed: ' + err + '</div>';
    });
}

function renderVendorBreakdown(data) {
  var fmt = function(n) { return n != null ? Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '0.00'; };
  var el = document.getElementById('bsVendorContent');
  var vendors = data.vendors || [];
  var total = data.total_uncategorized || 0;

  var html = '';
  // Summary
  html += '<div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">';
  html += _bsStatCard('Uncategorized', total, vendors.length + ' vendors', 'var(--red)');
  // Top 3 vendors
  vendors.slice(0, 3).forEach(function(v) {
    html += _bsStatCard(v.vendor, v.count + ' txns', '&#8377; ' + fmt(v.amount), 'var(--orange)');
  });
  html += '</div>';

  // Search bar
  html += '<div style="margin-bottom:10px"><input type="text" id="bsVendorSearch" oninput="filterVendorBreakdown()" placeholder="Search vendor..." style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px;min-width:300px;color-scheme:dark"></div>';

  // Table
  var th = 'padding:8px 6px;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim);cursor:pointer;user-select:none;';
  html += '<table id="bsVendorTable" style="width:100%;border-collapse:collapse;font-size:12px">';
  html += '<thead><tr style="border-bottom:2px solid var(--border);text-align:left">';
  html += '<th style="' + th + '">Vendor</th>';
  html += '<th style="' + th + 'text-align:right">Txns</th>';
  html += '<th style="' + th + 'text-align:right">Amount</th>';
  html += '<th style="' + th + '">Cards</th>';
  html += '<th style="' + th + '">Sample Transactions</th>';
  html += '</tr></thead><tbody>';

  vendors.forEach(function(v) {
    var cardTags = '';
    for (var c in v.cards) {
      var short = c.replace(/credit card/i, 'CC').replace(/bank /i, '');
      cardTags += '<span style="display:inline-block;background:rgba(99,102,241,0.15);color:var(--accent);border-radius:3px;padding:1px 6px;font-size:10px;margin:1px 2px">' + short + ' (' + v.cards[c] + ')</span>';
    }

    var samples = '';
    (v.txns || []).forEach(function(t) {
      samples += '<div style="font-size:10px;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:350px" title="' + (t.description || '').replace(/"/g, '&quot;') + '">' + t.date + ' | ' + fmt(t.amount) + ' | ' + (t.description || '').substring(0, 50) + '</div>';
    });

    var pct = total > 0 ? (v.count / total * 100).toFixed(1) : 0;

    html += '<tr data-vendor="' + (v.vendor || '').toLowerCase() + '" style="border-bottom:1px solid var(--border)">';
    html += '<td style="padding:8px 6px;font-weight:600">' + v.vendor + '<div style="font-size:10px;color:var(--text-dim);font-weight:normal">' + pct + '% of uncategorized</div></td>';
    html += '<td style="padding:8px 6px;text-align:right;font-weight:700;color:var(--red)">' + v.count + '</td>';
    html += '<td style="padding:8px 6px;text-align:right;font-weight:600">&#8377; ' + fmt(v.amount) + '</td>';
    html += '<td style="padding:8px 6px">' + cardTags + '</td>';
    html += '<td style="padding:8px 6px">' + samples + '</td>';
    html += '</tr>';
  });

  html += '</tbody></table>';
  el.innerHTML = html;
}

function filterVendorBreakdown() {
  var search = (document.getElementById('bsVendorSearch').value || '').toLowerCase();
  var rows = document.querySelectorAll('#bsVendorTable tbody tr');
  rows.forEach(function(r) {
    var vendor = r.getAttribute('data-vendor') || '';
    r.style.display = (!search || vendor.indexOf(search) >= 0) ? '' : 'none';
  });
}

function renderAutoMatch(data) {
  var items = data.items || [];
  var fmt = function(n) { return n != null ? Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // Populate card filter
  var cardSel = document.getElementById('amCardFilter');
  cardSel.innerHTML = '<option value="">All Cards (' + data.total + ')</option>';
  (data.card_names || []).forEach(function(name) {
    var cnt = (data.card_counts || {})[name] || 0;
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name + ' (' + cnt + ')';
    cardSel.appendChild(opt);
  });

  // Summary
  document.getElementById('autoMatchSummary').textContent = data.total + ' uncategorized';

  document.getElementById('autoMatchLoading').style.display = 'none';
  var content = document.getElementById('autoMatchContent');
  content.style.display = 'flex';
  content.innerHTML = '';

  if (!items.length) {
    content.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:40px">No uncategorized transactions found</div>';
    return;
  }

  // Build table
  var tbl = document.createElement('table');
  tbl.className = 'match-table';
  tbl.style.cssText = 'width:100%;font-size:11px';
  tbl.innerHTML = '<thead><tr>'
    + '<th style="padding:6px 4px;text-align:center;width:28px"><input type="checkbox" id="amSelectAll" onchange="toggleAmSelectAll(this)"></th>'
    + '<th style="text-align:left;padding:6px 8px">CC Description</th>'
    + '<th style="text-align:right;padding:6px 8px">Amount</th>'
    + '<th style="padding:6px 8px">Date</th>'
    + '<th style="padding:6px 8px">Card</th>'
    + '<th style="text-align:left;padding:6px 8px;border-left:2px solid var(--border)">Best Match</th>'
    + '<th style="padding:6px 8px">Type</th>'
    + '<th style="text-align:right;padding:6px 8px">Match Amt</th>'
    + '<th style="padding:6px 8px">Match Date</th>'
    + '<th style="padding:6px 8px">Action</th>'
    + '</tr></thead>';

  var tbody = document.createElement('tbody');

  items.forEach(function(item, idx) {
    var tr = document.createElement('tr');
    tr.id = 'am-row-' + idx;
    tr.setAttribute('data-card', item.card_name || '');
    tr.setAttribute('data-vendor', '');
    tr.setAttribute('data-has-match', '0');

    var desc = item.description || '-';
    var descFull = desc;
    if (desc.length > 45) desc = desc.substring(0, 45) + '\u2026';

    tr.innerHTML = '<td style="padding:5px 4px"></td>'
      + '<td style="text-align:left;padding:5px 8px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + descFull.replace(/"/g,'&quot;') + '">' + desc + '</td>'
      + '<td style="text-align:right;padding:5px 8px;font-family:monospace">' + fmt(item.amount) + '</td>'
      + '<td style="padding:5px 8px">' + fmtDate(item.date) + '</td>'
      + '<td style="padding:5px 8px;font-size:10px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (item.card_name || '-') + '</td>'
      + '<td id="am-match-' + idx + '" style="text-align:left;padding:5px 8px;border-left:2px solid var(--border);color:var(--text-dim);font-size:10px" colspan="4">'
      + '<button class="bill-create-btn" id="am-btn-' + idx + '" onclick="fetchMatchForRow(' + idx + ')" style="font-size:10px;padding:3px 10px">Find Match</button>'
      + '</td>';

    tbody.appendChild(tr);
  });

  tbl.appendChild(tbody);
  var wrap = document.createElement('div');
  wrap.style.cssText = 'flex:1;overflow-y:auto';
  wrap.appendChild(tbl);
  content.appendChild(wrap);
}

function fetchMatchForRow(idx) {
  var item = _autoMatchData.items[idx];
  if (!item) return;
  var btn = document.getElementById('am-btn-' + idx);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  fetch('/api/banking/get-matches/' + item.transaction_id)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var matches = data.matches || [];
      var cell = document.getElementById('am-match-' + idx);
      var row = document.getElementById('am-row-' + idx);
      var fmt = function(n) { return Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}); };

      if (!matches.length) {
        cell.removeAttribute('colspan');
        cell.innerHTML = '<span style="color:var(--yellow)">No match found</span>';
        // Add empty cells for remaining columns
        var parent = cell.parentElement;
        for (var c = 0; c < 3; c++) {
          var td = document.createElement('td');
          td.style.cssText = 'padding:5px 8px';
          td.textContent = '-';
          parent.appendChild(td);
        }
        return;
      }

      // Store best match on item for later confirmation
      item.best_match = matches[0];
      item.match_count = matches.length;
      row.setAttribute('data-has-match', '1');
      row.setAttribute('data-vendor', matches[0].vendor_name || '');
      row.style.background = 'rgba(80,200,120,0.04)';

      // Update checkbox cell
      var cbCell = row.querySelector('td:first-child');
      cbCell.innerHTML = '<input type="checkbox" class="am-cb" data-idx="' + idx + '" onchange="toggleAmCb(this)">';

      // Replace match cell — remove colspan, add proper cells
      cell.removeAttribute('colspan');
      var best = matches[0];
      var ref = best.reference ? '<div style="font-size:9px;color:var(--text-dim)">' + best.reference + '</div>' : '';
      cell.innerHTML = '<span title="' + (best.vendor_name||'').replace(/"/g,'&quot;') + '">' + (best.vendor_name || '-') + '</span>' + ref;
      cell.style.cssText = 'text-align:left;padding:5px 8px;border-left:2px solid var(--border);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:var(--text)';

      var parent = cell.parentElement;
      // Type cell
      var tdType = document.createElement('td');
      tdType.style.cssText = 'padding:5px 8px;font-size:10px';
      tdType.textContent = (best.transaction_type || '-').replace(/_/g, ' ');
      parent.appendChild(tdType);
      // Amount cell
      var tdAmt = document.createElement('td');
      tdAmt.style.cssText = 'text-align:right;padding:5px 8px;font-family:monospace;font-size:11px';
      tdAmt.textContent = fmt(best.amount);
      parent.appendChild(tdAmt);
      // Date cell
      var tdDate = document.createElement('td');
      tdDate.style.cssText = 'padding:5px 8px;font-size:11px';
      tdDate.textContent = fmtDate(best.date);
      parent.appendChild(tdDate);
      // Action cell — replace with Match button
      var tdAction = document.createElement('td');
      tdAction.style.cssText = 'padding:5px 8px';
      tdAction.innerHTML = '<button class="bill-create-btn" id="am-btn-' + idx + '" onclick="confirmAutoMatchOne(' + idx + ')">'
        + 'Match' + (matches.length > 1 ? ' <span style="font-size:9px;color:var(--text-dim)">+' + (matches.length-1) + '</span>' : '')
        + '</button>';
      parent.appendChild(tdAction);
    })
    .catch(function(err) {
      if (btn) { btn.textContent = 'Error'; btn.disabled = false; }
    });
}

function fetchAllMatches() {
  var items = _autoMatchData.items || [];
  var content = document.getElementById('autoMatchContent');
  var rows = content.querySelectorAll('tbody tr');
  rows.forEach(function(row, idx) {
    if (row.style.display === 'none') return;
    if (row.getAttribute('data-has-match') === '1') return;
    fetchMatchForRow(idx);
  });
}

function filterAutoMatch() {
  if (!_autoMatchData) return;
  var items = _autoMatchData.items || [];
  var content = document.getElementById('autoMatchContent');
  var rows = content.querySelectorAll('tbody tr');
  var cardFilter = document.getElementById('amCardFilter').value;
  var vendorFilter = (document.getElementById('amVendorFilter').value || '').toLowerCase();
  var statusFilter = document.getElementById('amStatusFilter').value;
  var visible = 0, visibleMatched = 0;

  rows.forEach(function(row, idx) {
    var show = true;
    var rowCard = row.getAttribute('data-card') || '';
    var rowVendor = (row.getAttribute('data-vendor') || '').toLowerCase();
    var hasMatch = row.getAttribute('data-has-match') === '1';

    if (cardFilter && rowCard !== cardFilter) show = false;
    if (show && vendorFilter && rowVendor.indexOf(vendorFilter) < 0) show = false;
    if (show && statusFilter === 'has_match' && !hasMatch) show = false;
    if (show && statusFilter === 'no_match' && hasMatch) show = false;

    row.style.display = show ? '' : 'none';
    if (show) { visible++; if (hasMatch) visibleMatched++; }
  });

  document.getElementById('autoMatchSummary').textContent =
    visible + ' shown \u00B7 ' + visibleMatched + ' with match';
}

function toggleAmCb(cb) {
  var idx = cb.getAttribute('data-idx');
  if (cb.checked) _amSelectedMatches.add(idx);
  else _amSelectedMatches.delete(idx);
  _updateMatchSelectedBtn();
}

function toggleAmSelectAll(cb) {
  var rows = document.querySelectorAll('#autoMatchContent tbody tr');
  rows.forEach(function(row) {
    if (row.style.display === 'none') return;
    var c = row.querySelector('.am-cb');
    if (c) { c.checked = cb.checked; toggleAmCb(c); }
  });
}

function _updateMatchSelectedBtn() {
  var btn = document.getElementById('matchSelectedBtn');
  var count = _amSelectedMatches.size;
  if (count > 0) {
    btn.style.display = 'inline-block';
    btn.textContent = 'Match Selected (' + count + ')';
  } else {
    btn.style.display = 'none';
  }
}

function confirmAutoMatchOne(idx) {
  var item = _autoMatchData.items[idx];
  if (!item || !item.best_match) return;
  var msg = 'Match this transaction?\n\nCC: ' + item.description + '\nAmount: ' + Number(item.amount).toLocaleString('en-IN') + '\nDate: ' + item.date
    + '\n\nTo: ' + item.best_match.vendor_name + ' (' + item.best_match.transaction_type + ')'
    + '\nAmount: ' + Number(item.best_match.amount).toLocaleString('en-IN');
  if (!confirm(msg)) return;

  var btn = document.getElementById('am-btn-' + idx);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  fetch('/api/banking/confirm-match', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      transaction_id: item.transaction_id,
      match_transaction_id: item.best_match.transaction_id,
      match_transaction_type: item.best_match.transaction_type,
    }),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var row = document.getElementById('am-row-' + idx);
    if (data.status === 'matched') {
      if (btn) { btn.textContent = '\u2713 Done'; btn.style.background = 'rgba(34,197,94,0.3)'; }
      if (row) { row.style.opacity = '0.5'; }
    } else {
      if (btn) { btn.textContent = 'Error'; btn.disabled = false; }
    }
  })
  .catch(function() { if (btn) { btn.textContent = 'Error'; btn.disabled = false; } });
}

function confirmMatchSelected() {
  var count = _amSelectedMatches.size;
  if (!count) return;
  if (!confirm('Match ' + count + ' transactions?')) return;

  var matches = [];
  _amSelectedMatches.forEach(function(idx) {
    var item = _autoMatchData.items[parseInt(idx)];
    if (item && item.best_match) {
      matches.push({
        transaction_id: item.transaction_id,
        match_transaction_id: item.best_match.transaction_id,
        match_transaction_type: item.best_match.transaction_type,
      });
    }
  });

  document.getElementById('matchSelectedBtn').disabled = true;
  document.getElementById('matchSelectedBtn').textContent = 'Matching...';

  fetch('/api/banking/confirm-match-bulk', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({matches: matches}),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.status === 'ok') {
      // Mark matched rows
      _amSelectedMatches.forEach(function(idx) {
        var row = document.getElementById('am-row-' + idx);
        var btn = document.getElementById('am-btn-' + idx);
        if (row) row.style.opacity = '0.5';
        if (btn) { btn.textContent = '\u2713 Done'; btn.style.background = 'rgba(34,197,94,0.3)'; btn.disabled = true; }
      });
      _amSelectedMatches.clear();
      _updateMatchSelectedBtn();
      document.getElementById('matchSelectedBtn').disabled = false;
      alert('Matched ' + data.matched + '/' + data.total + ' transactions');
    }
  })
  .catch(function(err) {
    document.getElementById('matchSelectedBtn').textContent = 'Error';
    document.getElementById('matchSelectedBtn').disabled = false;
  });
}

function openImportPicker() {
  document.getElementById('importPickerModal').style.display = 'flex';
  const body = document.getElementById('importPickerBody');
  body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading...</div>';

  // Get last parsed cards from step 4 result
  fetch('/api/status').then(r => r.json()).then(statusData => {
    const parsed = (statusData.step_results && statusData.step_results['4'] && statusData.step_results['4'].result)
      ? statusData.step_results['4'].result.cards_parsed || []
      : [];

    return fetch('/api/review/available-csvs?include_txns=1').then(r => r.json()).then(data => {
      let cards = data.cards || [];

      // Only show cards parsed in last Step 4 run — never show all
      cards = cards.filter(c => parsed.includes(c.card_name));

      if (!cards.length) {
        body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">No parsed CSVs found. Upload & Parse CC statements first (Step 4).</div>';
        return;
      }

      body.innerHTML = '';
      cards.forEach((c, i) => {
        const card = document.createElement('div');
        card.className = 'ip-card';

        const header = document.createElement('div');
        header.className = 'ip-card-header';

        const caret = document.createElement('span');
        caret.className = 'ip-caret';
        caret.textContent = '▶';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'import-card-cb';
        cb.value = c.card_name;
        cb.checked = true;
        cb.addEventListener('click', (e) => e.stopPropagation());

        const label = document.createElement('span');
        label.textContent = c.card_name;

        const count = document.createElement('span');
        count.className = 'ip-count';
        count.textContent = c.rows + ' txns';

        header.appendChild(caret);
        header.appendChild(cb);
        header.appendChild(label);
        header.appendChild(count);
        header.addEventListener('click', () => card.classList.toggle('open'));

        const txns = document.createElement('div');
        txns.className = 'ip-txns';
        const list = c.transactions || [];
        if (!list.length) {
          txns.innerHTML = '<div style="color:var(--text-dim);padding:6px 0">No transactions in CSV.</div>';
        } else {
          list.forEach(t => {
            const row = document.createElement('div');
            row.className = 'ip-txn-row';
            const dt = document.createElement('span');
            dt.textContent = t.date || '';
            const desc = document.createElement('span');
            desc.textContent = t.description || '';
            desc.style.overflow = 'hidden';
            desc.style.textOverflow = 'ellipsis';
            desc.style.whiteSpace = 'nowrap';
            desc.title = t.description || '';
            const amt = document.createElement('span');
            const n = Number(t.amount) || 0;
            amt.className = 'ip-txn-amt';
            amt.textContent = n.toFixed(2);
            if (t.forex_ref) amt.title = t.forex_ref;
            row.appendChild(dt);
            row.appendChild(desc);
            row.appendChild(amt);
            txns.appendChild(row);
          });
        }

        card.appendChild(header);
        card.appendChild(txns);
        body.appendChild(card);
      });
    });
  }).catch(err => {
    body.innerHTML = '<div style="color:var(--red);font-size:13px;padding:12px 0">Error: ' + err + '</div>';
  });
}

function closeImportPicker() {
  document.getElementById('importPickerModal').style.display = 'none';
}

function importSelectedCards() {
  const checkboxes = document.querySelectorAll('.import-card-cb:checked');
  const selected = Array.from(checkboxes).map(cb => cb.value);
  if (!selected.length) {
    addLogLine('[Import] No cards selected');
    return;
  }
  closeImportPicker();
  addLogLine('[Import] Importing: ' + selected.join(', '));
  runStepWithKwargs('5', {selected_cards: selected});
}

// --- Sync Zoho ---
function syncZoho() {
  addLogLine('[Sync] Starting Zoho sync (bills, vendors & CC accounts)...');
  fetch('/api/zoho/sync', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        addLogLine('[Sync] ' + data.error);
      } else {
        addLogLine('[Sync] Sync started...');
        pollStatus();
      }
    })
    .catch(err => addLogLine('[Sync] Request failed: ' + err));
}

// --- Bill Picker with Match Preview ---
let _billPickerData = null;
let _matchPreviewData = null;
let _billFilteredRows = [];
let _billSortCol = 'vendor';
let _billSortAsc = true;
var _billSelectedFiles = new Set();

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
function _getMatchTypeLabel(inv) {
  var m = inv.vendor_match_method || 'name';
  if (m === 'manual') return 'Manual';
  return m === 'gstin' ? 'GSTIN' : m === 'fuzzy' ? 'Fuzzy' : 'Name';
}
function _getMatchTypeKey(inv) {
  return inv.vendor_match_method || 'name';
}

/* --- Checkbox Dropdown Component --- */
function _buildCheckboxDropdown(id, label, options) {
  var html = '<div class="cb-dropdown" id="cbd_' + id + '">'
    + '<button type="button" class="cb-dropdown-btn" onclick="_toggleCbDropdown(\'' + id + '\')">'
    + label + ' <span class="cb-badge" id="cbd_badge_' + id + '"></span></button>'
    + '<div class="cb-dropdown-panel" id="cbd_panel_' + id + '">'
    + '<div class="cb-dropdown-actions">'
    + '<a onclick="_cbSelectAll(\'' + id + '\')">Select All</a>'
    + '<a onclick="_cbClearAll(\'' + id + '\')">Clear</a></div>'
    + '<input type="text" class="cb-dropdown-search" id="cbd_search_' + id + '" placeholder="Search..." oninput="_filterCbDropdown(\'' + id + '\')">'
    + '<div class="cb-dropdown-list">';
  options.forEach(function(o) {
    var chk = o.checked === false ? '' : ' checked';
    html += '<label><input type="checkbox"' + chk + ' value="' + o.value + '" onchange="_onCbChange(\'' + id + '\')">' + o.text + '</label>';
  });
  html += '</div></div></div>';
  return html;
}
function _toggleCbDropdown(id) {
  var panel = document.getElementById('cbd_panel_' + id);
  var isOpen = panel.classList.contains('open');
  document.querySelectorAll('.cb-dropdown-panel.open').forEach(function(p) { p.classList.remove('open'); });
  if (!isOpen) panel.classList.add('open');
}
function _onCbChange(id) {
  _updateCbBadge(id);
  applyBillFilters();
}
function _cbSelectAll(id) {
  document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]').forEach(function(cb) { cb.checked = true; });
  _updateCbBadge(id);
  applyBillFilters();
}
function _cbClearAll(id) {
  document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]').forEach(function(cb) { cb.checked = false; });
  _updateCbBadge(id);
  applyBillFilters();
}
function _updateCbBadge(id) {
  var all = document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]');
  var checked = document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]:checked');
  var badge = document.getElementById('cbd_badge_' + id);
  if (badge) badge.textContent = checked.length < all.length ? checked.length : '';
}
function _getCbValues(id) {
  var vals = [];
  document.querySelectorAll('#cbd_panel_' + id + ' input[type="checkbox"]:checked').forEach(function(cb) { vals.push(cb.value); });
  return vals;
}
function _filterCbDropdown(id) {
  var query = document.getElementById('cbd_search_' + id).value.toLowerCase();
  document.querySelectorAll('#cbd_panel_' + id + ' .cb-dropdown-list label').forEach(function(lbl) {
    lbl.style.display = lbl.textContent.toLowerCase().indexOf(query) !== -1 ? '' : 'none';
  });
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('.cb-dropdown')) {
    document.querySelectorAll('.cb-dropdown-panel.open').forEach(function(p) { p.classList.remove('open'); });
  }
});

/* --- Searchable Zoho Vendor Dropdown --- */
var _zohoVendors = [];
var _selectedZohoVendor = null;

function _loadZohoVendors() {
  return fetch('/api/zoho-vendors').then(function(r) { return r.json(); }).then(function(data) {
    _zohoVendors = data;
  });
}
function _buildSearchDropdown() {
  return '<div class="search-dropdown" id="zohoVendorSearch">'
    + '<input type="text" placeholder="Search Zoho vendor..." onfocus="_openZohoDropdown()" oninput="_filterZohoDropdown()">'
    + '<div class="search-dropdown-list" id="zohoVendorList"></div></div>';
}
function _openZohoDropdown() {
  _filterZohoDropdown();
  document.getElementById('zohoVendorList').classList.add('open');
}
function _currencyBadge(code) {
  code = code || 'INR';
  var color = code === 'INR' ? '#a3a3a3' : code === 'USD' ? '#60a5fa' : '#c084fc';
  return ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:rgba(255,255,255,0.08);color:'+color+';font-weight:600">'+code+'</span>';
}
function _filterZohoDropdown() {
  var input = document.querySelector('#zohoVendorSearch input');
  var q = (input ? input.value : '').toLowerCase();
  var list = document.getElementById('zohoVendorList');
  var items = '';
  var count = 0;
  for (var i = 0; i < _zohoVendors.length && count < 50; i++) {
    var v = _zohoVendors[i];
    if (q && v.contact_name.toLowerCase().indexOf(q) < 0) continue;
    var sel = _selectedZohoVendor && _selectedZohoVendor.contact_id === v.contact_id ? ' selected' : '';
    items += '<div class="sd-item' + sel + '" onclick="_selectZohoVendor(this)" data-id="' + v.contact_id + '" data-name="' + v.contact_name.replace(/"/g, '&quot;') + '" data-currency="' + (v.currency_code||'INR') + '">' + v.contact_name + _currencyBadge(v.currency_code) + '</div>';
    count++;
  }
  if (!items) items = '<div class="sd-item" style="color:var(--text-dim)">No matches</div>';
  // Note: vendor names come from local zoho_vendors_cache.json, not untrusted input
  list.innerHTML = items;
}
function _selectZohoVendor(el) {
  _selectedZohoVendor = { contact_id: el.getAttribute('data-id'), contact_name: el.getAttribute('data-name') };
  var input = document.querySelector('#zohoVendorSearch input');
  if (input) input.value = _selectedZohoVendor.contact_name;
  document.getElementById('zohoVendorList').classList.remove('open');
}
/* --- Per-row vendor edit --- */
var _activeRowVendorDropdown = null;
function _openRowVendorEdit(fileKey, event) {
  event.stopPropagation();
  _closeRowVendorDropdown();
  var td = event.target.closest('.col-zoho-vendor');
  if (!td) return;
  var dd = document.createElement('div');
  dd.className = 'row-vendor-dropdown open';
  dd.setAttribute('data-file', fileKey);
  dd.innerHTML = '<input type="text" placeholder="Search vendor..." oninput="_filterRowVendorDropdown(this)" autofocus>'
    + '<div class="rvd-list"></div>';
  td.appendChild(dd);
  _activeRowVendorDropdown = dd;
  var input = dd.querySelector('input');
  input.focus();
  _filterRowVendorDropdown(input);
}
function _filterRowVendorDropdown(input) {
  var q = (input ? input.value : '').toLowerCase();
  var list = input.closest('.row-vendor-dropdown').querySelector('.rvd-list');
  var fileKey = input.closest('.row-vendor-dropdown').getAttribute('data-file');
  var items = '';
  var count = 0;
  for (var i = 0; i < _zohoVendors.length && count < 50; i++) {
    var v = _zohoVendors[i];
    if (q && v.contact_name.toLowerCase().indexOf(q) < 0) continue;
    items += '<div class="sd-item" onclick="_selectRowVendor(this,\'' + fileKey.replace(/'/g, "\\'") + '\')" data-id="' + v.contact_id + '" data-name="' + v.contact_name.replace(/"/g, '&quot;') + '" data-currency="' + (v.currency_code||'INR') + '">' + v.contact_name + _currencyBadge(v.currency_code) + '</div>';
    count++;
  }
  if (!items) items = '<div class="sd-item" style="color:var(--text-dim)">No matches</div>';
  list.innerHTML = items;
}
function _selectRowVendor(el, fileKey) {
  var vendorId = el.getAttribute('data-id');
  var vendorName = el.getAttribute('data-name');
  // Find the invoice in preview data
  var inv = null;
  _matchPreviewData.preview.forEach(function(item) {
    if (item.file === fileKey) inv = item;
  });
  if (!inv) return;
  var vname = inv.vendor_name || inv.file;
  var overrides = {};
  overrides[vname] = { contact_id: vendorId, contact_name: vendorName };
  fetch('/api/vendor-overrides', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ overrides: overrides })
  }).then(function(r) { return r.json(); }).then(function() {
    Object.assign(_vendorOverrides, overrides);
    if (inv.action === 'new_vendor') inv.action = 'new_bill';
    inv.matched_vendor_id = vendorId;
    inv.matched_vendor_name = vendorName;
    inv.vendor_match_method = 'manual';
    _closeRowVendorDropdown();
    _sortFilteredRows();
    _renderTableRows();
    // Update summary
    var s = { total: 0, skip: 0, possible_duplicate: 0, new_bill: 0, new_vendor_bill: 0 };
    _matchPreviewData.preview.forEach(function(item) {
      s.total++;
      if (item.action === 'skip') s.skip++;
      else if (item.action === 'possible_duplicate') s.possible_duplicate++;
      else if (item.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
    var totalNew = s.new_bill + s.new_vendor_bill;
    var summary = document.getElementById('billPickerSummary');
    if (summary) _renderSummaryPanel(summary, s, totalNew);
    showToast('Vendor changed to ' + vendorName, 'success');
  });
}
function _closeRowVendorDropdown() {
  if (_activeRowVendorDropdown) {
    _activeRowVendorDropdown.remove();
    _activeRowVendorDropdown = null;
  }
}

document.addEventListener('click', function(e) {
  if (!e.target.closest('.search-dropdown')) {
    var list = document.getElementById('zohoVendorList');
    if (list) list.classList.remove('open');
  }
  if (!e.target.closest('.row-vendor-dropdown') && !e.target.closest('.zoho-vendor-edit-btn')) {
    _closeRowVendorDropdown();
  }
});

/* --- Vendor Overrides --- */
var _vendorOverrides = {};

function _loadVendorOverrides() {
  return fetch('/api/vendor-overrides').then(function(r) { return r.json(); }).then(function(data) {
    _vendorOverrides = data;
  });
}
function _applyOverridesToPreview() {
  if (!_matchPreviewData || !_matchPreviewData.preview) return;
  _matchPreviewData.preview.forEach(function(inv) {
    var vname = inv.vendor_name || '';
    if (_vendorOverrides[vname] && inv.action !== 'skip') {
      inv.action = 'new_bill';
      inv.matched_vendor_id = _vendorOverrides[vname].contact_id;
      inv.matched_vendor_name = _vendorOverrides[vname].contact_name;
      inv.vendor_match_method = 'manual';
    }
  });
}
function applyZohoVendorMapping() {
  if (!_selectedZohoVendor) { showToast('Select a Zoho vendor first', 'warning'); return; }
  // Collect target rows: selected (checked) rows, or all filtered non-skip rows
  var targetRows = [];
  var useSelected = false;
  _matchPreviewData.preview.forEach(function(inv) {
    if (_billSelectedFiles.has(inv.file) && inv.action !== 'skip') targetRows.push(inv);
  });
  if (targetRows.length > 0) {
    useSelected = true;
  } else {
    // Fall back to all filtered (visible) non-skip rows
    _billFilteredRows.forEach(function(inv) {
      if (inv.action !== 'skip') targetRows.push(inv);
    });
  }
  if (!targetRows.length) { showToast('No rows to map — filter or select invoices first', 'warning'); return; }
  var label = useSelected ? 'selected' : 'filtered';
  var msg = 'Change vendor for ' + targetRows.length + ' ' + label + ' invoice(s) to "' + _selectedZohoVendor.contact_name + '"?\n\nThis will override any existing vendor match.';
  if (!confirm(msg)) return;
  var overrides = {};
  targetRows.forEach(function(inv) {
    var vname = inv.vendor_name || inv.file;
    overrides[vname] = { contact_id: _selectedZohoVendor.contact_id, contact_name: _selectedZohoVendor.contact_name };
  });
  fetch('/api/vendor-overrides', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ overrides: overrides })
  }).then(function(r) { return r.json(); }).then(function() {
    Object.assign(_vendorOverrides, overrides);
    targetRows.forEach(function(inv) {
      if (inv.action === 'new_vendor') inv.action = 'new_bill';
      inv.matched_vendor_id = _selectedZohoVendor.contact_id;
      inv.matched_vendor_name = _selectedZohoVendor.contact_name;
      inv.vendor_match_method = 'manual';
    });
    _sortFilteredRows();
    _renderTableRows();
    _updateSelectionUI();
    var s = { total: 0, skip: 0, possible_duplicate: 0, new_bill: 0, new_vendor_bill: 0 };
    _matchPreviewData.preview.forEach(function(inv) {
      s.total++;
      if (inv.action === 'skip') s.skip++;
      else if (inv.action === 'possible_duplicate') s.possible_duplicate++;
      else if (inv.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
    var totalNew = s.new_bill + s.new_vendor_bill;
    var summary = document.getElementById('billPickerSummary');
    if (summary) _renderSummaryPanel(summary, s, totalNew);
    showToast('Mapped ' + targetRows.length + ' ' + label + ' invoice(s) to ' + _selectedZohoVendor.contact_name, 'success');
  });
}

function _renderSummaryPanel(summary, s, totalNew) {
  // Note: summary stats are computed from local match-preview data, not untrusted input
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

function _buildFilterBar(preview) {
  var months = [];
  var vendors = [];
  var seen = {};
  preview.forEach(function(p) {
    var m = p.organized_month || 'Unknown';
    if (!seen['m_'+m]) { months.push(m); seen['m_'+m] = 1; }
    var v = p.vendor_name || 'Unknown';
    if (!seen['v_'+v]) { vendors.push(v); seen['v_'+v] = 1; }
  });
  months.sort();
  vendors.sort(function(a,b){ return a.localeCompare(b); });

  var html = '<div class="bill-filter-bar">';
  html += '<div class="bill-filter-group"><label>From</label><select id="bfFrom"><option value="">All</option>';
  months.forEach(function(m){ html += '<option value="'+m+'">'+m+'</option>'; });
  html += '</select></div>';
  html += '<div class="bill-filter-group"><label>To</label><select id="bfTo"><option value="">All</option>';
  months.forEach(function(m){ html += '<option value="'+m+'">'+m+'</option>'; });
  html += '</select></div>';
  var vendorOpts = vendors.map(function(v){ return {value: v, text: v}; });
  html += '<div class="bill-filter-group"><label>Vendor</label>' + _buildCheckboxDropdown('vendor', 'Vendor', vendorOpts) + '</div>';
  html += '<div class="bill-filter-group"><label>Min Amt</label><input type="number" id="bfMinAmt" placeholder="0" step="any"></div>';
  html += '<div class="bill-filter-group"><label>Max Amt</label><input type="number" id="bfMaxAmt" placeholder="any" step="any"></div>';
  var statusOpts = [{value:'skip',text:'In Zoho'},{value:'possible_duplicate',text:'Possible Duplicate'},{value:'new_bill',text:'New Bill + Existing Vendor'},{value:'new_vendor',text:'New Bill + New Vendor'}];
  html += '<div class="bill-filter-group"><label>Status</label>' + _buildCheckboxDropdown('status', 'Status', statusOpts) + '</div>';
  var matchOpts = [{value:'gstin',text:'GSTIN'},{value:'name',text:'Name'},{value:'fuzzy',text:'Fuzzy'},{value:'manual',text:'Manual'}];
  html += '<div class="bill-filter-group"><label>Match Type</label>' + _buildCheckboxDropdown('matchtype', 'Match Type', matchOpts) + '</div>';
  // Financial Year filter — FY runs Apr to Mar (e.g. FY 2025-26 = Apr 2025 – Mar 2026)
  var fySet = {};
  preview.forEach(function(p) {
    if (p.date) {
      var parts = p.date.split('-');
      var y = parseInt(parts[0]), m = parseInt(parts[1]);
      var fy = m >= 4 ? y : y - 1; // Apr-Dec = same year, Jan-Mar = previous year
      fySet[fy] = 1;
    }
  });
  var currentDate = new Date();
  var currentFY = currentDate.getMonth() >= 3 ? currentDate.getFullYear() : currentDate.getFullYear() - 1;
  var fyOpts = Object.keys(fySet).sort().map(function(fy) {
    var fyNum = parseInt(fy);
    var nextYr = (fyNum + 1).toString().substring(2);
    return {value: fy, text: 'FY ' + fy + '-' + nextYr, checked: fyNum >= currentFY};
  });
  if (fyOpts.length > 1) {
    html += '<div class="bill-filter-group"><label>FY</label>' + _buildCheckboxDropdown('year', 'FY', fyOpts) + '</div>';
  }
  html += '<button class="bill-filter-clear" onclick="clearBillFilters()">Clear</button>';
  html += '</div>';
  return html;
}

function _buildTable() {
  var cols = [
    {key:'check', label:'<input type="checkbox" id="bpSelectAll" onchange="toggleBillSelectAll(this)" style="cursor:pointer;accent-color:var(--accent)">', sort:false, cls:'col-checkbox'},
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
  var html = '<div class="bill-table-wrap"><table class="bill-table"><thead><tr>';
  cols.forEach(function(c) {
    var cls = (c.cls ? ' class="'+c.cls+'"' : '');
    if (c.sort) {
      var sorted = (_billSortCol === c.key) ? ' sorted' : '';
      var arrow = (_billSortCol === c.key) ? (_billSortAsc ? '\u25B2' : '\u25BC') : '\u25B2';
      html += '<th'+cls+' class="'+(c.cls||'')+sorted+'" onclick="sortBillTable(\''+c.key+'\')" style="cursor:pointer">' + c.label + ' <span class="sort-arrow">'+arrow+'</span></th>';
    } else {
      html += '<th'+cls+'>' + c.label + '</th>';
    }
  });
  html += '</tr></thead><tbody id="bpTbody"></tbody></table></div>';
  return html;
}

function _renderTableRows() {
  // Note: all values rendered are from local JSON files (extracted invoices, Zoho cache), not user-supplied input
  var tbody = document.getElementById('bpTbody');
  if (!tbody) return;
  var html = '';
  _billFilteredRows.forEach(function(inv) {
    var isBlocked = inv.action === 'skip' || inv.action === 'possible_duplicate';
    var rowCls = isBlocked ? ' class="row-skip"' : '';
    var amt = inv.amount ? Number(inv.amount).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '0.00';
    var fileEsc = (inv.file || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
    var vendorEsc = (inv.vendor_name || 'Unknown').replace(/"/g, '&quot;');

    // Checkbox
    var cb = '';
    if (!isBlocked) {
      var checked = _billSelectedFiles.has(inv.file) ? ' checked' : '';
      cb = '<input type="checkbox" onchange="onBillCheckChange(this)" data-file="'+fileEsc+'" style="cursor:pointer;accent-color:var(--accent)"'+checked+'>';
    }

    // Status badge
    var statusBadge = '';
    if (inv.action === 'skip') {
      statusBadge = '<span class="bill-status-badge created">In Zoho</span>';
    } else if (inv.action === 'possible_duplicate') {
      statusBadge = '<span class="bill-status-badge" style="background:rgba(249,115,22,0.15);color:var(--orange,#f97316)">Possible Duplicate</span>';
    } else if (inv.action === 'new_bill') {
      var vmMethod = inv.vendor_match_method || 'name';
      var vmColor = vmMethod === 'gstin' ? 'var(--green)' : 'var(--accent)';
      var vmBg = vmMethod === 'gstin' ? 'rgba(34,197,94,0.15)' : 'rgba(108,140,255,0.15)';
      statusBadge = '<span class="bill-status-badge" style="background:'+vmBg+';color:'+vmColor+'">New Bill</span>';
    } else {
      statusBadge = '<span class="bill-status-badge" style="background:rgba(250,204,21,0.15);color:var(--yellow)">New Vendor</span>';
    }

    // Match type badge (only for new_bill)
    var matchBadge = '';
    if (inv.action === 'new_bill') {
      matchBadge = '<span class="bill-status-badge" style="background:rgba(108,140,255,0.1);color:var(--text-dim)">' + _getMatchTypeLabel(inv) + '</span>';
    }

    // Action button
    var actionBtn = '';
    if (!isBlocked) {
      actionBtn = '<button class="bill-create-btn" onclick="createOneBillConfirm(\''+fileEsc+'\',\''+vendorEsc+'\',\''+amt+'\')">Create</button>';
    }

    // Invoice # column
    var invoiceNum = inv.invoice_number || '';

    // Zoho vendor column
    var zohoVendor = '';
    if (isBlocked) {
      zohoVendor = inv.matched_vendor_name || (inv.matched_bill ? inv.matched_bill.vendor_name || '' : '');
    } else if (inv.action === 'new_bill') {
      zohoVendor = inv.matched_vendor_name || '';
    }
    var zohoVendorEsc = zohoVendor.replace(/"/g,'&quot;');
    var zohoVendorCell = '';
    if (isBlocked) {
      zohoVendorCell = '<span title="'+zohoVendorEsc+'">'+zohoVendor+'</span>';
    } else {
      var editFileKey = inv.file.replace(/'/g, "\\'").replace(/"/g,'&quot;');
      zohoVendorCell = '<div class="zoho-vendor-display">'
        + '<span class="vendor-text" title="'+zohoVendorEsc+'">'+(zohoVendor || '<span style="color:var(--text-dim);font-style:italic">—</span>')+'</span>'
        + '<button class="zoho-vendor-edit-btn" onclick="_openRowVendorEdit(\''+editFileKey+'\',event)" title="Change vendor">&#9998;</button>'
        + '</div>';
    }

    // Zoho Bill # column
    var zohoBill = '';
    if (inv.action === 'skip') {
      zohoBill = inv.matched_bill || '';
    } else if (inv.action === 'possible_duplicate') {
      zohoBill = inv.matched_bill_number || '';
    }

    html += '<tr'+rowCls+'>'
      + '<td class="col-checkbox">'+cb+'</td>'
      + '<td class="vendor-cell" title="'+vendorEsc+'">'+vendorEsc+'</td>'
      + '<td>'+invoiceNum+'</td>'
      + '<td>'+fmtDate(inv.date)+'</td>'
      + '<td class="col-amount">'+amt+' '+(inv.currency || 'INR')+'</td>'
      + '<td>'+statusBadge+'</td>'
      + '<td>'+matchBadge+'</td>'
      + '<td class="col-zoho-vendor">'+zohoVendorCell+'</td>'
      + '<td>'+zohoBill+'</td>'
      + '<td class="col-action">'+actionBtn+'</td>'
      + '</tr>';
  });
  tbody.innerHTML = html;
  _updateSelectAllCheckbox();
}

function applyBillFilters() {
  if (!_matchPreviewData) return;
  var preview = _matchPreviewData.preview || [];
  var fromVal = document.getElementById('bfFrom') ? document.getElementById('bfFrom').value : '';
  var toVal = document.getElementById('bfTo') ? document.getElementById('bfTo').value : '';
  var vendors = _getCbValues('vendor');
  var minAmt = document.getElementById('bfMinAmt') ? parseFloat(document.getElementById('bfMinAmt').value) : NaN;
  var maxAmt = document.getElementById('bfMaxAmt') ? parseFloat(document.getElementById('bfMaxAmt').value) : NaN;
  var statuses = _getCbValues('status');
  var matchTypes = _getCbValues('matchtype');
  var years = _getCbValues('year');

  // Build sorted month list for range filtering
  var allMonths = [];
  var monthSet = {};
  preview.forEach(function(p) {
    var m = p.organized_month || 'Unknown';
    if (!monthSet[m]) { allMonths.push(m); monthSet[m] = 1; }
  });
  allMonths.sort();

  var fromIdx = fromVal ? allMonths.indexOf(fromVal) : 0;
  var toIdx = toVal ? allMonths.indexOf(toVal) : allMonths.length - 1;
  if (fromIdx < 0) fromIdx = 0;
  if (toIdx < 0) toIdx = allMonths.length - 1;
  var validMonths = {};
  for (var i = fromIdx; i <= toIdx; i++) validMonths[allMonths[i]] = 1;

  _billFilteredRows = preview.filter(function(inv) {
    // Financial Year filter (Apr-Mar)
    if (years.length) {
      if (inv.date) {
        var dp = inv.date.split('-');
        var iy = parseInt(dp[0]), im = parseInt(dp[1]);
        var invFY = (im >= 4 ? iy : iy - 1).toString();
        if (years.indexOf(invFY) < 0) return false;
      }
    }
    var m = inv.organized_month || 'Unknown';
    if (fromVal || toVal) { if (!validMonths[m]) return false; }
    if (vendors.length && vendors.indexOf(inv.vendor_name || 'Unknown') < 0) return false;
    var amt = parseFloat(inv.amount) || 0;
    if (!isNaN(minAmt) && amt < minAmt) return false;
    if (!isNaN(maxAmt) && amt > maxAmt) return false;
    if (statuses.length && statuses.indexOf(_getStatusKey(inv)) < 0) return false;
    var allMatchCount = document.querySelectorAll('#cbd_panel_matchtype input[type="checkbox"]').length;
    if (matchTypes.length && matchTypes.length < allMatchCount) {
      if (inv.action !== 'new_bill') return false;
      if (matchTypes.indexOf(_getMatchTypeKey(inv)) < 0) return false;
    }
    return true;
  });
  _sortFilteredRows();
  _renderTableRows();
  _updateSelectionUI();
  _updateFilteredSummary();
}

function _sortFilteredRows() {
  var col = _billSortCol;
  var asc = _billSortAsc ? 1 : -1;
  _billFilteredRows.sort(function(a, b) {
    var va, vb;
    if (col === 'vendor') { va = (a.vendor_name||'').toLowerCase(); vb = (b.vendor_name||'').toLowerCase(); }
    else if (col === 'invoice_num') { va = (a.invoice_number||'').toLowerCase(); vb = (b.invoice_number||'').toLowerCase(); }
    else if (col === 'date') { va = a.date || ''; vb = b.date || ''; }
    else if (col === 'amount') { va = parseFloat(a.amount)||0; vb = parseFloat(b.amount)||0; }
    else if (col === 'status') { va = _getStatusKey(a); vb = _getStatusKey(b); }
    else if (col === 'match') { va = a.action==='new_bill' ? _getMatchTypeKey(a) : 'zzz'; vb = b.action==='new_bill' ? _getMatchTypeKey(b) : 'zzz'; }
    else if (col === 'zoho_vendor') { va = (a.matched_vendor_name||'').toLowerCase(); vb = (b.matched_vendor_name||'').toLowerCase(); }
    else if (col === 'zoho_bill') { va = (a.action==='skip' ? a.matched_bill : a.matched_bill_number) || ''; vb = (b.action==='skip' ? b.matched_bill : b.matched_bill_number) || ''; }
    else { va = ''; vb = ''; }
    if (va < vb) return -1 * asc;
    if (va > vb) return 1 * asc;
    return 0;
  });
}

function sortBillTable(col) {
  if (_billSortCol === col) { _billSortAsc = !_billSortAsc; }
  else { _billSortCol = col; _billSortAsc = true; }
  // Update header arrows
  document.querySelectorAll('.bill-table th').forEach(function(th) { th.classList.remove('sorted'); });
  var idx = {vendor:1,invoice_num:2,date:3,amount:4,status:5,match:6,zoho_vendor:7,zoho_bill:8}[col];
  if (idx !== undefined) {
    var ths = document.querySelectorAll('.bill-table th');
    if (ths[idx]) {
      ths[idx].classList.add('sorted');
      var arrow = ths[idx].querySelector('.sort-arrow');
      if (arrow) arrow.textContent = _billSortAsc ? '\u25B2' : '\u25BC';
    }
  }
  _sortFilteredRows();
  _renderTableRows();
}

function clearBillFilters() {
  ['bfFrom','bfTo','bfMinAmt','bfMaxAmt'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
  ['vendor','status','matchtype','year'].forEach(function(id) { _cbSelectAll(id); });
  applyBillFilters();
}

function onBillCheckChange(cb) {
  var file = cb.getAttribute('data-file');
  if (cb.checked) { _billSelectedFiles.add(file); } else { _billSelectedFiles.delete(file); }
  _updateSelectAllCheckbox();
  _updateSelectionUI();
}

function toggleBillSelectAll(cb) {
  var checkboxes = document.querySelectorAll('#bpTbody input[type="checkbox"]');
  checkboxes.forEach(function(c) {
    c.checked = cb.checked;
    var file = c.getAttribute('data-file');
    if (cb.checked) { _billSelectedFiles.add(file); } else { _billSelectedFiles.delete(file); }
  });
  _updateSelectionUI();
}

function _updateSelectAllCheckbox() {
  var sa = document.getElementById('bpSelectAll');
  if (!sa) return;
  var checkboxes = document.querySelectorAll('#bpTbody input[type="checkbox"]');
  if (!checkboxes.length) { sa.checked = false; sa.indeterminate = false; return; }
  var total = checkboxes.length;
  var checked = Array.from(checkboxes).filter(function(c){return c.checked}).length;
  sa.checked = checked === total;
  sa.indeterminate = checked > 0 && checked < total;
}

function _updateSelectionUI() {
  var n = _billSelectedFiles.size;
  var countEl = document.getElementById('bpSelectedCount');
  if (countEl) countEl.textContent = 'Selected: ' + n;
  var btn = document.getElementById('createSelectedBillsBtn');
  if (btn) {
    btn.textContent = 'Create Selected (' + n + ')';
    btn.disabled = n === 0;
  }
}

function _updateFilteredSummary() {
  var skip = 0, newBill = 0, newVendor = 0;
  _billFilteredRows.forEach(function(inv) {
    if (inv.action === 'skip') skip++;
    else if (inv.action === 'new_bill') newBill++;
    else newVendor++;
  });
  var total = _billFilteredRows.length;
  var willUpload = newBill + newVendor;
  var el;
  el = document.getElementById('bpTotal'); if (el) el.textContent = total;
  el = document.getElementById('bpSkip'); if (el) el.textContent = skip;
  el = document.getElementById('bpNewBill'); if (el) el.textContent = newBill;
  el = document.getElementById('bpNewVendor'); if (el) el.textContent = newVendor;
  el = document.getElementById('bpWillUpload'); if (el) el.textContent = willUpload;
}

function createSelectedBills() {
  if (!_billSelectedFiles.size) return;
  var n = _billSelectedFiles.size;
  var files = Array.from(_billSelectedFiles);
  var overridesDict = {};
  if (_matchPreviewData && _matchPreviewData.preview) {
    _matchPreviewData.preview.forEach(function(inv) {
      if (_billSelectedFiles.has(inv.file) && inv.matched_vendor_id) {
        overridesDict[inv.file] = { contact_id: inv.matched_vendor_id, contact_name: inv.matched_vendor_name || '' };
      }
    });
  }
  showModal(
    'Create ' + n + ' Bills?',
    n + ' bills will be created in Zoho Books.',
    function() {
      closeBillPicker();
      addLogLine('[Bills] Creating ' + n + ' selected bills...');
      runStepWithKwargs('3', {selected_files: files, vendor_overrides: overridesDict});
    },
    false,
    'Create ' + n + ' Bills'
  );
}

function createOneBillConfirm(filename, vendorName, amount) {
  showModal(
    'Create Bill?',
    'Create bill for <b>' + vendorName + '</b> (' + amount + ')?',
    function() {
      closeBillPicker();
      addLogLine('[Bills] Creating bill for: ' + filename);
      runStepWithKwargs('3', {selected_files: [filename]});
    },
    false,
    'Create Bill'
  );
}

function openBillPicker() {
  _billSelectedFiles.clear();
  _billSortCol = 'vendor';
  _billSortAsc = true;
  _selectedZohoVendor = null;
  document.getElementById('billPickerModal').style.display = 'flex';
  var body = document.getElementById('billPickerBody');
  var summary = document.getElementById('billPickerSummary');
  body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">Loading match preview...</div>';
  summary.innerHTML = '';

  Promise.all([
    fetch('/api/bills/match-preview', {method: 'POST'}).then(function(r){return r.json()}),
    _loadVendorOverrides(),
    _loadZohoVendors()
  ]).then(function(results) {
    var data = results[0];
    if (data.error) {
      addLogLine('[Bills] ' + data.error + ' — falling back to basic list');
      _loadBasicBillPicker(body, summary);
      return;
    }
    _matchPreviewData = data;
    _applyOverridesToPreview();

    // Recount after overrides
    var s = { total: 0, skip: 0, possible_duplicate: 0, new_bill: 0, new_vendor_bill: 0 };
    (data.preview || []).forEach(function(inv) {
      s.total++;
      if (inv.action === 'skip') s.skip++;
      else if (inv.action === 'possible_duplicate') s.possible_duplicate++;
      else if (inv.action === 'new_bill') s.new_bill++;
      else s.new_vendor_bill++;
    });
    var totalNew = s.new_bill + s.new_vendor_bill;
    _renderSummaryPanel(summary, s, totalNew);

    var preview = data.preview || [];
    if (!preview.length) {
      body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">No invoices found. Run Extract Data first.</div>';
      return;
    }

    // Build: filter bar + mapping bar + table
    var mappingBar = '<div class="bill-mapping-bar">'
      + '<label>Bulk change Zoho Vendor:</label>'
      + _buildSearchDropdown()
      + '<button class="modal-btn modal-btn-confirm" onclick="applyZohoVendorMapping()">Apply</button>'
      + '</div>';
    body.innerHTML = _buildFilterBar(preview) + mappingBar + _buildTable();

    // Attach filter listeners for From/To and amount inputs
    ['bfFrom','bfTo','bfMinAmt','bfMaxAmt'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('change', applyBillFilters);
    });

    // Update badge for year filter (2024 unchecked by default) and apply filters
    _updateCbBadge('year');
    _updateCbBadge('status');
    _updateCbBadge('vendor');
    _updateCbBadge('matchtype');
    applyBillFilters();
  }).catch(function(err) {
    addLogLine('[Bills] Match preview failed: ' + err + ' — falling back');
    _loadBasicBillPicker(body, summary);
  });
}

function _loadBasicBillPicker(body, summary) {
  // Fallback: flat table without match preview (no Zoho sync cache)
  fetch('/api/invoices/list').then(function(r){return r.json()}).then(function(data) {
    if (data.error) {
      body.innerHTML = '<div style="color:var(--red);font-size:13px;padding:12px 0">' + data.error + '</div>';
      return;
    }
    _billPickerData = data;
    _matchPreviewData = null;
    var s = data.summary || {};
    var totalPending = s.pending || 0;

    summary.innerHTML = ''
      + '<div class="bill-summary-total"><span>Total Invoices</span><span class="count">' + (s.total || 0) + '</span></div>'
      + '<div class="bill-summary-card"><span class="label"><span class="dot" style="background:var(--green)"></span> Created</span><span class="count" style="color:var(--green)">' + (s.created || 0) + '</span></div>'
      + '<div class="bill-summary-card"><span class="label"><span class="dot" style="background:var(--yellow)"></span> Pending</span><span class="count" style="color:var(--yellow)">' + totalPending + '</span></div>'
      + '<div class="bill-summary-divider"></div>'
      + '<div style="font-size:11px;color:var(--text-dim);padding:4px 0">Sync Zoho first for smart dedup</div>'
      + '<div class="bill-summary-upload-section">'
      + '<button class="modal-btn modal-btn-cancel" onclick="closeBillPicker()" style="width:100%">Cancel</button>'
      + '</div>';

    var months = data.months || [];
    if (!months.length) {
      body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:12px 0">No invoices found. Run Step 2 (Extract Data) first.</div>';
      return;
    }

    var html = '<div class="bill-table-wrap"><table class="bill-table"><thead><tr>'
      + '<th>Vendor</th><th>Month</th><th class="col-amount">Amount</th><th>Status</th><th class="col-action"></th>'
      + '</tr></thead><tbody>';
    months.forEach(function(group) {
      group.invoices.forEach(function(inv) {
        var isCreated = inv.status === 'created';
        var amt = inv.amount ? Number(inv.amount).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '0.00';
        var vendorEsc = (inv.vendor_name || 'Unknown').replace(/"/g, '&quot;');
        var rowCls = isCreated ? ' class="row-skip"' : '';
        html += '<tr'+rowCls+'>'
          + '<td class="vendor-cell" title="'+vendorEsc+'">'+vendorEsc+'</td>'
          + '<td>'+group.month+'</td>'
          + '<td class="col-amount">'+amt+' '+(inv.currency || 'INR')+'</td>'
          + '<td><span class="bill-status-badge '+inv.status+'">'+inv.status+'</span></td>'
          + '<td class="col-action">';
        if (!isCreated) {
          html += '<button class="bill-create-btn" onclick="createOneBill(\''+inv.file.replace(/'/g, "\\'")+'\')">Create</button>';
        }
        html += '</td></tr>';
      });
    });
    html += '</tbody></table></div>';
    body.innerHTML = html;
  }).catch(function(err) {
    body.innerHTML = '<div style="color:var(--red);font-size:13px;padding:12px 0">Error: ' + err + '</div>';
  });
}

function closeBillPicker() {
  document.getElementById('billPickerModal').style.display = 'none';
}

function createOneBill(filename) {
  closeBillPicker();
  addLogLine('[Bills] Creating bill for: ' + filename);
  runStepWithKwargs('3', {selected_files: [filename]});
}

function runStepWithKwargs(step, kwargs) {
  fetch('/api/run/' + step, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({run_kwargs: kwargs}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      addLogLine('[UI] ' + data.error);
    }
    pollStatus();
  })
  .catch(err => addLogLine('[UI] Request failed: ' + err));
}

// --- Match Status Panel ---
let _matchData = null;

function openMatchPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('invoiceBrowsePanel').style.display = 'none';
  document.getElementById('extractPreviewPanel').style.display = 'none';
  document.getElementById('ccPreviewPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'flex';
  document.getElementById('matchLoading').style.display = 'block';
  document.getElementById('matchContent').style.display = 'none';

  fetch('/api/match-status')
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('matchLoading').textContent = data.error;
        return;
      }
      _matchData = data;
      renderMatchPanel(data);
    })
    .catch(err => {
      document.getElementById('matchLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeMatchPanel() {
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function renderMatchPanel(data) {
  document.getElementById('matchedCount').textContent = data.summary.matched_count;
  document.getElementById('unmatchedCount').textContent = data.summary.unmatched_count;
  document.getElementById('totalCount').textContent = data.summary.total_count;
  document.getElementById('matchLoading').style.display = 'none';
  document.getElementById('matchContent').style.display = 'block';
  switchMatchTab('matched');
}

function switchMatchTab(tab) {
  document.querySelectorAll('.match-tab').forEach(t => t.classList.remove('active'));
  document.querySelector('.tab-' + tab).classList.add('active');

  const tbody = document.getElementById('matchTableBody');
  const thead = document.getElementById('matchTableHead');
  tbody.innerHTML = '';

  const items = tab === 'matched' ? _matchData.matched : _matchData.unmatched;

  if (tab === 'unmatched') {
    thead.innerHTML = '<th>Vendor</th><th>Invoice File</th><th>Bill Amount</th>'
      + '<th>Currency</th><th>CC Card</th><th>CC INR Amount</th><th>Status</th><th>Reason</th>';
  } else {
    thead.innerHTML = '<th>Vendor</th><th>Invoice File</th><th>Bill Amount</th>'
      + '<th>Currency</th><th>CC Card</th><th>CC INR Amount</th><th>Status</th>';
  }

  if (!items || !items.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = tab === 'unmatched' ? 8 : 7;
    td.style.textAlign = 'center';
    td.style.color = 'var(--text-dim)';
    td.style.padding = '30px';
    td.textContent = tab === 'matched' ? 'No matched payments yet' : 'All bills matched!';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  items.forEach(item => {
    const tr = document.createElement('tr');
    const addTd = (text, opts) => {
      const td = document.createElement('td');
      td.textContent = text || '-';
      if (opts) Object.assign(td.style, opts);
      tr.appendChild(td);
      return td;
    };

    addTd(item.vendor_name);
    const fileTd = addTd(item.file, {fontSize:'11px',maxWidth:'200px',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'});
    fileTd.title = item.file || '';
    addTd(item.amount != null ? Number(item.amount).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-');
    addTd(item.currency);
    addTd(item.cc_card);
    addTd(item.cc_inr_amount != null ? Number(item.cc_inr_amount).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-');

    const statusTd = addTd(item.status);
    statusTd.className = item.status === 'paid' ? 'status-paid' : 'status-unmatched';

    if (tab === 'unmatched') {
      const reasonTd = addTd(item.reason);
      reasonTd.className = 'reason-cell';
      reasonTd.title = item.reason || '';
    }

    tbody.appendChild(tr);
  });
}

// --- Check Bills & CC Panel ---
function openCheckPanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('invoiceBrowsePanel').style.display = 'none';
  document.getElementById('extractPreviewPanel').style.display = 'none';
  document.getElementById('ccPreviewPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'flex';
  document.getElementById('checkLoading').style.display = 'block';
  document.getElementById('checkContent').style.display = 'none';

  fetch('/api/check-cc-match')
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('checkLoading').textContent = data.error;
        return;
      }
      renderCheckPanel(data);
    })
    .catch(err => {
      document.getElementById('checkLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeCheckPanel() {
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

// --- Payment Preview Panel ---
var _paymentPreviewData = null;
var _paymentFromCache = false;
var _paidBillsCache = {};

function openPaymentPreview(forceRefresh) {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('invoiceBrowsePanel').style.display = 'none';
  document.getElementById('extractPreviewPanel').style.display = 'none';
  document.getElementById('ccPreviewPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'flex';
  document.getElementById('paymentLoading').style.display = 'block';
  document.getElementById('paymentLoading').textContent = forceRefresh ? 'Refreshing from Zoho...' : 'Loading...';
  document.getElementById('paymentContent').style.display = 'none';
  document.getElementById('recordSelectedBtn').style.display = 'none';

  var previewUrl = '/api/payments/preview' + (forceRefresh ? '?refresh=1' : '');
  Promise.all([
    fetch(previewUrl).then(function(r) { return r.json(); }),
    fetch('/api/payments/paid-bills-cache').then(function(r) { return r.json(); }).catch(function() { return {paid_bills: {}}; })
  ])
  .then(function(results) {
    var data = results[0];
    var paidData = results[1];
    if (data.error) {
      document.getElementById('paymentLoading').textContent = data.error;
      return;
    }
    _paidBillsCache = paidData.paid_bills || {};

    // Cross-reference: mark unmatched bills as already_paid if in paid cache
    var paidCount = 0;
    (data.matches || []).forEach(function(m) {
      if (m.status === 'unmatched' && _paidBillsCache[m.bill_id]) {
        m.status = 'already_paid';
        paidCount++;
      }
    });
    // Also check group matches
    (data.group_matches || []).forEach(function(gm) {
      var allPaid = (gm.grouped_bills || []).every(function(gb) { return _paidBillsCache[gb.bill_id]; });
      if (allPaid && gm.status !== 'already_paid') {
        gm.status = 'already_paid';
      }
    });

    // Inject paid bills from paid cache as already_paid entries
    // (these are bills already paid in Zoho, not in preview since preview only fetches unpaid)
    var existingBillIds = new Set();
    (data.matches || []).forEach(function(m) { if (m.bill_id) existingBillIds.add(m.bill_id); });
    (data.group_matches || []).forEach(function(gm) {
      (gm.grouped_bills || []).forEach(function(gb) { if (gb.bill_id) existingBillIds.add(gb.bill_id); });
    });
    var injectedCount = 0;
    Object.keys(_paidBillsCache).forEach(function(billId) {
      if (existingBillIds.has(billId)) return;
      var pb = _paidBillsCache[billId];
      data.matches.push({
        bill_id: billId,
        vendor_name: pb.vendor_name || '',
        bill_amount: pb.amount || 0,
        bill_currency: pb.currency || 'INR',
        bill_date: pb.date || '',
        bill_number: pb.bill_number || '',
        status: 'already_paid'
      });
      injectedCount++;
    });
    paidCount += injectedCount;

    if (paidCount > 0) {
      // Update summary
      data.summary = data.summary || {};
      data.summary.unmatched = (data.summary.unmatched || 0);
      data.summary.already_paid = (data.summary.already_paid || 0) + injectedCount;
      data.summary.total_bills = (data.summary.total_bills || 0) + injectedCount;
    }

    _paymentPreviewData = data;
    _paymentFromCache = !forceRefresh;
    _amexExcludedBills = new Set(data.amex_excluded || []);
    renderPaymentPreview(data);
    populateMonthFilter();
    filterPayments();
  })
  .catch(function(err) {
    document.getElementById('paymentLoading').textContent = 'Failed: ' + err;
  });
}

var _lastRefreshTime = 0;
var _REFRESH_COOLDOWN = 30; // seconds between refreshes
function refreshPaymentPreview() {
  var now = Date.now() / 1000;
  var elapsed = now - _lastRefreshTime;
  if (elapsed < _REFRESH_COOLDOWN) {
    var remaining = Math.ceil(_REFRESH_COOLDOWN - elapsed);
    var btn = document.getElementById('paymentRefreshBtn');
    if (btn) btn.textContent = 'Wait ' + remaining + 's';
    setTimeout(function() { if (btn) btn.textContent = '\u21BB Refresh'; }, remaining * 1000);
    return;
  }
  _lastRefreshTime = now;
  openPaymentPreview(true);
}

function syncPaidBills() {
  var btn = document.getElementById('syncPaidBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing...'; }
  fetch('/api/payments/sync-paid-bills', {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var label = '\u2713 Synced (' + (data.count || 0) + (data.banking_matched ? ', ' + data.banking_matched + ' banking' : '') + ')';
      if (btn) { btn.disabled = false; btn.textContent = label; }
      // Reload with new paid data
      openPaymentPreview(false);
    })
    .catch(function(err) {
      if (btn) { btn.disabled = false; btn.textContent = 'Sync Failed'; }
    });
}

function closePaymentPanel() {
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function filterPayments() {
  if (!_paymentPreviewData) return;
  var matches = _paymentPreviewData.matches || [];
  var content = document.getElementById('paymentContent');
  var dataRows = content.querySelectorAll('tbody tr[data-status]');

  var cardName = document.getElementById('paymentCardFilter').value;
  var vendorFilter = (document.getElementById('paymentVendorFilter').value || '').toLowerCase();
  var searchQuery = (document.getElementById('paymentSearchBar').value || '').toLowerCase().trim();
  var dateFrom = document.getElementById('paymentDateFrom').value || '';
  var dateTo = document.getElementById('paymentDateTo').value || '';
  var statusFilter = document.getElementById('paymentStatusFilter').value || '';
  var amtRaw = (document.getElementById('paymentAmountFilter').value || '').replace(/,/g, '').trim();
  var amtFilter = amtRaw ? parseFloat(amtRaw) : 0;

  var visibleMatched = 0, visibleGrouped = 0, visibleUnmatched = 0, visiblePaid = 0, visibleCcOnly = 0;

  dataRows.forEach(function(row, i) {
    var m = matches[i];
    if (!m) return;
    var rowCard = row.getAttribute('data-card') || '';
    var status = row.getAttribute('data-status') || '';
    var rowVendor = (row.getAttribute('data-vendor') || '').toLowerCase();
    var rowDate = row.getAttribute('data-date') || '';

    var show = true;

    // Amex excluded bills
    var rowBillId = (row.id || '').replace('pay-row-', '');
    if (rowBillId && _amexExcludedBills && _amexExcludedBills.has(rowBillId)) show = false;

    // Card filter
    if (cardName) {
      if (!rowCard || rowCard !== cardName) show = false;
    }

    // Search bar — matches across vendor name, CC description, resolved vendor, bill number
    if (show && searchQuery) {
      var ccDesc = row.getAttribute('data-ccdesc') || '';
      var resolvedV = row.getAttribute('data-resolvedvendor') || '';
      var billNum = (m.bill_number || m.file || '').toLowerCase();
      var groupedVendors = '';
      if (m.grouped_bills) {
        groupedVendors = m.grouped_bills.map(function(b) { return (b.vendor_name || '').toLowerCase(); }).join(' ');
      }
      var searchHaystack = rowVendor + ' ' + ccDesc + ' ' + resolvedV + ' ' + billNum + ' ' + groupedVendors;
      show = searchHaystack.indexOf(searchQuery) >= 0;
    }

    // Vendor filter (bidirectional substring)
    if (show && vendorFilter) {
      if (!rowVendor) { show = false; }
      else { show = rowVendor.indexOf(vendorFilter) >= 0 || vendorFilter.indexOf(rowVendor) >= 0; }
    }

    // Date range filter — both bill date AND cc date must be in range (if present)
    var rowCcDate = row.getAttribute('data-ccdate') || '';
    if (show && (dateFrom || dateTo)) {
      // Check bill date
      if (rowDate) {
        if (dateFrom && rowDate < dateFrom) show = false;
        if (dateTo && rowDate > dateTo) show = false;
      } else if (!rowCcDate) {
        // No date at all — hide
        show = false;
      }
      // Check cc date (if present and row still visible)
      if (show && rowCcDate) {
        if (dateFrom && rowCcDate < dateFrom) show = false;
        if (dateTo && rowCcDate > dateTo) show = false;
      }
    }

    // Status filter
    if (show && statusFilter) {
      if (status !== statusFilter) show = false;
    }

    // Amount filter — matches if bill amount or CC amount contains the number
    if (show && amtFilter) {
      var billAmt = parseFloat(row.getAttribute('data-billamount') || 0);
      var ccAmt = parseFloat(row.getAttribute('data-ccamount') || 0);
      // Match if either amount rounds to within 1 of the filter value
      var matched = Math.abs(billAmt - amtFilter) < 1 || Math.abs(ccAmt - amtFilter) < 1;
      // Also match partial: e.g. typing "300" matches 300, 3000, 30000 etc — check if string contains
      if (!matched) {
        var billStr = String(Math.round(billAmt));
        var ccStr = String(Math.round(ccAmt));
        matched = billStr.indexOf(amtRaw) >= 0 || ccStr.indexOf(amtRaw) >= 0;
      }
      if (!matched) show = false;
    }

    row.style.display = show ? '' : 'none';
    if (show) {
      if (status === 'matched') visibleMatched++;
      else if (status === 'group_matched') visibleGrouped++;
      else if (status === 'unmatched') visibleUnmatched++;
      else if (status === 'already_paid') visiblePaid++;
      else if (status === 'cc_only') visibleCcOnly++;
    }
  });

  // Show/hide section separators
  var seps = {matched: visibleMatched, group_matched: visibleGrouped, cc_only: visibleCcOnly, unmatched: visibleUnmatched, other: visiblePaid};
  ['matched','group_matched','cc_only','unmatched','other'].forEach(function(sec) {
    var sep = content.querySelector('.pay-sep-' + sec);
    if (sep) sep.style.display = seps[sec] > 0 ? '' : 'none';
  });

  var billCount = visibleMatched + visibleGrouped + visibleUnmatched + visiblePaid;
  document.getElementById('paymentSummaryText').textContent =
    billCount + ' bills \u00B7 ' + visibleMatched + ' matched \u00B7 ' + visibleGrouped + ' grouped \u00B7 ' + visibleUnmatched + ' no CC \u00B7 ' + visiblePaid + ' already paid \u00B7 ' + visibleCcOnly + ' no invoice';

  // Reset checkboxes on filter change
  _paySelectedBills.clear();
  var selAll = document.getElementById('paySelectAll');
  if (selAll) selAll.checked = false;
  document.querySelectorAll('.pay-cb').forEach(function(c) { c.checked = false; });
  _updatePaySelectedBtn();
}

function applyMonthFilter() {
  var sel = document.getElementById('paymentMonthFilter');
  var val = sel.value;
  if (!val) {
    document.getElementById('paymentDateFrom').value = '';
    document.getElementById('paymentDateTo').value = '';
  } else {
    // val is "YYYY-MM"
    var parts = val.split('-');
    var y = parseInt(parts[0]), m = parseInt(parts[1]);
    var firstDay = val + '-01';
    var lastDay = new Date(y, m, 0).getDate();
    document.getElementById('paymentDateFrom').value = firstDay;
    document.getElementById('paymentDateTo').value = val + '-' + String(lastDay).padStart(2, '0');
  }
  filterPayments();
}

function populateMonthFilter() {
  if (!_paymentPreviewData) return;
  var matches = _paymentPreviewData.matches || [];
  var monthBills = {}, monthCC = {};
  matches.forEach(function(m) {
    var billDate = m.bill_date || '';
    var ccDate = m.cc_date || '';
    var status = m.status || '';
    if (billDate) {
      var bm = billDate.substring(0, 7);
      if (!monthBills[bm]) monthBills[bm] = 0;
      monthBills[bm]++;
    }
    if (ccDate && status !== 'unmatched' && status !== 'already_paid') {
      var cm = ccDate.substring(0, 7);
      if (!monthCC[cm]) monthCC[cm] = 0;
      monthCC[cm]++;
    }
  });
  var allMonths = {};
  Object.keys(monthBills).forEach(function(k) { allMonths[k] = true; });
  Object.keys(monthCC).forEach(function(k) { allMonths[k] = true; });
  var sorted = Object.keys(allMonths).sort().reverse();
  var sel = document.getElementById('paymentMonthFilter');
  sel.innerHTML = '<option value="">All Months</option>';
  sorted.forEach(function(ym) {
    var opt = document.createElement('option');
    opt.value = ym;
    var label = new Date(ym + '-15').toLocaleString('default', {month: 'short', year: 'numeric'});
    var bc = monthBills[ym] || 0;
    var cc = monthCC[ym] || 0;
    opt.textContent = label + '  \u2014  ' + bc + ' bills, ' + cc + ' CC';
    sel.appendChild(opt);
  });
}

function clearPaymentFilters() {
  document.getElementById('paymentSearchBar').value = '';
  document.getElementById('paymentCardFilter').value = '';
  document.getElementById('paymentMonthFilter').value = '';
  document.getElementById('paymentVendorFilter').value = '';
  document.getElementById('paymentDateFrom').value = '';
  document.getElementById('paymentDateTo').value = '';
  document.getElementById('paymentStatusFilter').value = '';
  document.getElementById('paymentAmountFilter').value = '';
  filterPayments();
}

function renderPaymentPreview(data) {
  var matches = data.matches || [];
  var s = data.summary || {};
  var fmt = function(n) { return n != null ? Number(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // Populate card filter dropdown with uncategorized counts
  var cardFilter = document.getElementById('paymentCardFilter');
  var ccTotal = data.card_cc_total || {};
  var ccUnmatched = data.card_cc_unmatched || {};
  var totalUncatCount = (data.unmatched_cc || []).length;
  cardFilter.innerHTML = '<option value="">All Cards (' + totalUncatCount + ' uncategorized)</option>';
  var cardNames = data.card_names || [];
  if (cardNames.length > 0) {
    cardNames.forEach(function(name) {
      var opt = document.createElement('option');
      opt.value = name;
      var total = ccTotal[name] || 0;
      var uncat = ccUnmatched[name] || 0;
      opt.textContent = name + ' (' + uncat + '/' + total + ' uncategorized)';
      cardFilter.appendChild(opt);
    });
    cardFilter.style.display = 'inline-block';
  } else {
    cardFilter.style.display = 'none';
  }

  document.getElementById('paymentSummaryText').textContent =
    s.total_bills + ' bills \u00B7 ' + s.matched + ' matched \u00B7 ' + (s.group_matched || 0) + ' grouped \u00B7 ' + s.unmatched + ' no CC \u00B7 ' + (s.already_paid || 0) + ' already paid \u00B7 ' + totalUncatCount + ' no invoice';

  // Show cache indicator
  var cacheInd = document.getElementById('paymentCacheIndicator');
  if (_paymentFromCache) {
    cacheInd.textContent = '\u26A1 Cached';
    cacheInd.style.color = 'var(--yellow)';
    cacheInd.title = 'Loaded from cache. Click Refresh to fetch fresh from Zoho.';
  } else {
    cacheInd.textContent = '\u2713 Live';
    cacheInd.style.color = 'var(--green)';
    cacheInd.title = 'Freshly fetched from Zoho APIs.';
  }

  // Populate vendor filter dropdown
  var vendorSet = {};
  (matches || []).forEach(function(m) { if (m.vendor_name) vendorSet[m.vendor_name] = 1; });
  var vendorNames = Object.keys(vendorSet).sort();
  var vendorSel = document.getElementById('paymentVendorFilter');
  vendorSel.innerHTML = '<option value="">All Vendors</option>';
  vendorNames.forEach(function(v) {
    var opt = document.createElement('option');
    opt.value = v; opt.textContent = v;
    vendorSel.appendChild(opt);
  });

  // Show filter bar
  document.getElementById('paymentFilterBar').style.display = 'flex';

  // Reset filters
  document.getElementById('paymentDateFrom').value = '';
  document.getElementById('paymentDateTo').value = '';
  document.getElementById('paymentStatusFilter').value = '';

  document.getElementById('paymentLoading').style.display = 'none';
  var content = document.getElementById('paymentContent');
  content.style.display = 'flex';
  content.innerHTML = '';

  // Merge unmatched CC transactions into matches array for unified display
  var unmatchedCc = data.unmatched_cc || [];
  unmatchedCc.forEach(function(cc) {
    matches.push({
      status: 'cc_only',
      cc_transaction_id: cc.transaction_id || '',
      cc_description: cc.description || '',
      cc_inr_amount: cc.amount || 0,
      cc_date: cc.date || '',
      cc_card: cc.card_name || '',
      cc_forex_amount: cc.forex_amount || null,
      cc_forex_currency: cc.forex_currency || null,
      unmatched_reason: cc.unmatched_reason || null,
      resolved_vendor: cc.resolved_vendor || null,
    });
  });

  // Merge group matches into main array
  var gm = data.group_matches || [];
  gm.forEach(function(g) {
    matches.push({
      status: 'group_matched',
      cc_transaction_id: g.cc_transaction_id || '',
      cc_description: g.cc_description || '',
      cc_inr_amount: g.cc_inr_amount || 0,
      cc_date: g.cc_date || '',
      cc_card: g.cc_card || '',
      vendor_name: g.vendor_name || '',
      grouped_bills: g.grouped_bills || [],
      group_sum: g.group_sum || 0,
      confidence: g.confidence || {},
      match_score: g.match_score || 0,
    });
  });

  if (!matches.length) {
    content.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:40px">No unpaid bills or CC transactions found</div>';
    return;
  }

  // Sort: matched first (by confidence desc), then group_matched, then no CC, then no invoice, then already_paid
  var order = {matched: 0, group_matched: 1, unmatched: 2, cc_only: 3, already_paid: 4};
  matches.sort(function(a, b) {
    var oa = order.hasOwnProperty(a.status) ? order[a.status] : 9;
    var ob = order.hasOwnProperty(b.status) ? order[b.status] : 9;
    if (oa !== ob) return oa - ob;
    // Within matched, sort by confidence descending
    if (a.status === 'matched' && b.status === 'matched') {
      var ca = (a.confidence && a.confidence.overall) || 0;
      var cb = (b.confidence && b.confidence.overall) || 0;
      return cb - ca;
    }
    return 0;
  });

  // Reset selected checkboxes
  _paySelectedBills.clear();
  document.getElementById('recordSelectedBtn').style.display = 'none';

  // Build table — CC columns LEFT, Bill columns RIGHT
  var tbl = document.createElement('table');
  tbl.className = 'match-table';
  tbl.style.cssText = 'width:100%;font-size:11px';
  tbl.innerHTML = '<thead><tr>'
    + '<th style="padding:6px 4px;text-align:center;width:28px"><input type="checkbox" id="paySelectAll" onchange="togglePaySelectAll(this)" title="Select all matched"></th>'
    + '<th style="text-align:left;padding:6px 8px">CC Description</th>'
    + '<th style="text-align:right;padding:6px 8px">CC INR</th>'
    + '<th style="padding:6px 8px">CC Date</th>'
    + '<th style="padding:6px 8px">Card</th>'
    + '<th style="text-align:left;padding:6px 8px;border-left:2px solid var(--border)">Vendor / Invoice</th>'
    + '<th style="text-align:right;padding:6px 8px">Bill Amt</th>'
    + '<th style="padding:6px 4px">Cur</th>'
    + '<th style="padding:6px 8px">Bill Date</th>'
    + '<th style="padding:6px 6px;text-align:center" title="Amount difference: CC - Bills">Diff</th>'
    + '<th style="padding:6px 8px;text-align:center">Confidence</th>'
    + '<th style="padding:6px 8px">Action</th>'
    + '</tr></thead>';

  var tbody = document.createElement('tbody');
  var _lastSection = '';

  function _confDot(val) {
    var col = val >= 90 ? 'var(--green)' : val >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
    return '<span style="color:' + col + ';font-weight:700" title="' + val + '%">' + val + '</span>';
  }

  matches.forEach(function(m, idx) {
    // Add section separator rows
    var section = m.status === 'matched' ? 'matched' : (m.status === 'group_matched' ? 'group_matched' : (m.status === 'cc_only' ? 'cc_only' : (m.status === 'unmatched' ? 'unmatched' : 'other')));
    if (section !== _lastSection) {
      _lastSection = section;
      var sepTr = document.createElement('tr');
      sepTr.className = 'pay-section-sep pay-sep-' + section;
      var sepLabel = '', sepColor = '', sepBg = '';
      if (section === 'matched') {
        var mCount = matches.filter(function(x){return x.status==='matched'}).length;
        sepLabel = '\u2714 Matched (' + mCount + ')';
        sepColor = 'var(--green)'; sepBg = 'rgba(80,200,120,0.08)';
      } else if (section === 'group_matched') {
        var gmCount = matches.filter(function(x){return x.status==='group_matched'}).length;
        sepLabel = '\u2795 Group Matched \u2014 1 CC = N Bills (' + gmCount + ' groups)';
        sepColor = 'var(--accent)'; sepBg = 'rgba(100,100,255,0.08)';
      } else if (section === 'cc_only') {
        var ccCount = matches.filter(function(x){return x.status==='cc_only'}).length;
        sepLabel = '\u26A0 No Invoice \u2014 CC Only (' + ccCount + ')';
        sepColor = 'var(--accent)'; sepBg = 'rgba(100,150,255,0.06)';
      } else if (section === 'unmatched') {
        var umCount = matches.filter(function(x){return x.status==='unmatched'}).length;
        var withCand = matches.filter(function(x){return x.status==='unmatched' && x.candidates && x.candidates.length > 0}).length;
        sepLabel = '\u26A0 No CC Match \u2014 Bills Only (' + umCount + ')';
        if (withCand > 0) {
          sepLabel += ' <span style="font-weight:400;font-size:10px;margin-left:12px">'
            + 'Score \u2265 <select id="candScoreThreshold" onchange="filterCandidatesByScore()" style="background:var(--bg);color:var(--text);border:1px solid var(--border);font-size:10px;padding:1px 4px;border-radius:3px;color-scheme:dark">'
            + '<option value="0" selected>All</option><option value="50">50</option><option value="60">60</option><option value="70">70</option><option value="80">80</option><option value="90">90</option>'
            + '</select>'
            + ' <button onclick="selectAllCandidates()" style="font-size:10px;padding:2px 8px;margin-left:8px;background:var(--bg-secondary);color:var(--text);border:1px solid var(--border);border-radius:3px;cursor:pointer">Select Visible</button>'
            + ' <button id="confirmCandidatesBtn" onclick="confirmSelectedCandidates()" style="font-size:10px;padding:2px 8px;margin-left:4px;background:var(--green);color:#000;border:none;border-radius:3px;cursor:pointer;display:none">Confirm Selected (0)</button>'
            + '</span>';
        }
        sepColor = 'var(--yellow)'; sepBg = 'rgba(255,200,50,0.06)';
      } else {
        var apCount = matches.filter(function(x){return x.status==='already_paid'}).length;
        sepLabel = '\u2713 Already Paid (' + apCount + ')';
        sepColor = 'var(--text-dim)'; sepBg = 'rgba(150,150,150,0.06)';
      }
      sepTr.innerHTML = '<td colspan="11" style="padding:7px 10px;font-size:11px;font-weight:700;color:' + sepColor + ';border-top:2px solid var(--border);background:' + sepBg + '">' + sepLabel + '</td>';
      tbody.appendChild(sepTr);
    }

    var tr = document.createElement('tr');
    tr.id = 'pay-row-' + (m.bill_id || 'cc-' + idx);
    tr.setAttribute('data-card', m.cc_card || '');
    tr.setAttribute('data-status', m.status || '');
    tr.setAttribute('data-vendor', m.vendor_name || '');
    tr.setAttribute('data-date', m.bill_date || m.cc_date || '');
    tr.setAttribute('data-billamount', m.bill_amount || 0);
    tr.setAttribute('data-ccamount', m.cc_inr_amount || 0);
    tr.setAttribute('data-ccdate', m.cc_date || (m.candidates && m.candidates.length > 0 ? m.candidates[0].cc_date : '') || '');
    var _ccDescForSearch = (m.cc_description || '');
    if (!_ccDescForSearch && m.candidates && m.candidates.length > 0) {
      _ccDescForSearch = m.candidates.map(function(c) { return c.cc_description || ''; }).join(' ');
    }
    tr.setAttribute('data-ccdesc', _ccDescForSearch.toLowerCase());
    tr.setAttribute('data-resolvedvendor', (m.resolved_vendor || '').toLowerCase());

    // --- Drag-and-drop: CC Only rows are draggable sources ---
    if (m.status === 'cc_only') {
      tr.classList.add('pay-drag-source');
      tr.draggable = true;
      tr.setAttribute('data-drag-idx', String(idx));
      tr.addEventListener('dragstart', function(e) {
        tr.classList.add('pay-dragging');
        e.dataTransfer.effectAllowed = 'link';
        e.dataTransfer.setData('text/plain', JSON.stringify({
          idx: idx,
          cc_transaction_id: m.cc_transaction_id || '',
          cc_description: m.cc_description || '',
          cc_inr_amount: m.cc_inr_amount || 0,
          cc_date: m.cc_date || '',
          cc_card: m.cc_card || '',
          cc_forex_amount: m.cc_forex_amount || null,
          cc_forex_currency: m.cc_forex_currency || null,
          resolved_vendor: m.resolved_vendor || ''
        }));
      });
      tr.addEventListener('dragend', function() { tr.classList.remove('pay-dragging'); });
    }
    // --- Drag-and-drop: Unmatched bill rows are drop targets ---
    if (m.status === 'unmatched') {
      tr.classList.add('pay-drop-target');
      tr.setAttribute('data-drop-billid', m.bill_id);
      tr.addEventListener('dragover', function(e) { e.preventDefault(); e.dataTransfer.dropEffect = 'link'; tr.classList.add('drag-over'); });
      tr.addEventListener('dragleave', function() { tr.classList.remove('drag-over'); });
      tr.addEventListener('drop', function(e) {
        e.preventDefault();
        tr.classList.remove('drag-over');
        try {
          var ccData = JSON.parse(e.dataTransfer.getData('text/plain'));
          previewManualDrop(tr, m, ccData);
        } catch(err) { addLogLine('[Drag] Invalid drop data: ' + err); }
      });
    }

    var bgColor = 'transparent';
    if (m.status === 'matched') bgColor = 'rgba(80,200,120,0.04)';
    else if (m.status === 'group_matched') bgColor = 'rgba(100,100,255,0.04)';
    else if (m.status === 'cc_only') bgColor = 'rgba(100,150,255,0.04)';
    else if (m.status === 'unmatched') bgColor = 'rgba(255,200,50,0.04)';
    else if (m.status === 'already_paid') bgColor = 'rgba(150,150,150,0.04)';
    tr.style.background = bgColor;

    var confCell = '';
    var actionBtn = '';
    if (m.status === 'matched') {
      actionBtn = '<button class="bill-create-btn" id="pay-btn-' + m.bill_id + '" onclick="confirmRecordOne(\'' + m.bill_id + '\')">Record</button>';
      // Confidence breakdown: V=vendor, A=amount, D=date
      var c = m.confidence || {};
      var ov = c.overall || 0;
      var ovColor = ov >= 85 ? 'var(--green)' : ov >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      confCell = '<div style="text-align:center;line-height:1.4">'
        + '<div style="font-size:13px;font-weight:700;color:' + ovColor + '">' + ov + '%</div>'
        + '<div style="font-size:9px;color:var(--text-dim)">Vendor:' + _confDot(c.vendor||0) + ' Amt:' + _confDot(c.amount||0) + ' Date:' + _confDot(c.date||0) + '</div>'
        + (m.match_type && m.match_type.indexOf('rate:') >= 0
           ? '<div style="font-size:8px;color:var(--text-dim)">'
             + m.match_type.replace(/.*rate:([\d.]+)\s*actual:([\d.]+).*/, 'Rate: \u20B9$1/$ (actual: \u20B9$2)')
             + '</div>' : '')
        + (m.match_type && m.match_type.indexOf('est') >= 0
           ? '<div style="font-size:8px;color:var(--yellow)">\u26A0 Est. rate</div>' : '')
        + '</div>';
    } else if (m.status === 'cc_only') {
      var reason = m.unmatched_reason || 'No Invoice';
      var rVendor = m.resolved_vendor || '';
      confCell = '<div style="text-align:center;line-height:1.3">'
        + '<span style="color:var(--accent);font-size:10px">No Invoice</span>'
        + (rVendor ? '<div style="font-size:8px;color:var(--text-dim)" title="Resolved to: ' + rVendor + '">\u2192 ' + rVendor + '</div>' : '')
        + '<div style="font-size:8px;color:var(--yellow);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + reason + '">' + reason + '</div>'
        + '<div class="drag-hint">\u2630 drag to bill</div>'
        + '</div>';
    } else if (m.status === 'unmatched') {
      var topCand = (m.candidates && m.candidates.length > 0) ? m.candidates[0] : null;
      if (topCand) {
        var cs = topCand.candidate_score;
        var csColor = cs >= 80 ? 'var(--green)' : cs >= 60 ? 'var(--yellow)' : 'var(--text-dim)';
        confCell = '<div style="text-align:center;line-height:1.3;cursor:pointer" onclick="toggleCandidateDetail(\'' + m.bill_id + '\')">'
          + '<div style="font-size:13px;font-weight:700;color:' + csColor + '">' + cs + '%</div>'
          + '<div style="font-size:9px;color:var(--text-dim)">Candidate</div>'
          + '<div style="font-size:8px;color:var(--text-dim)">Amt:' + _confDot(topCand.breakdown.amount) + ' Date:' + _confDot(topCand.breakdown.date) + ' Vnd:' + _confDot(topCand.breakdown.vendor) + '</div>'
          + '<div class="drag-hint">\u2193 or drop CC</div>'
          + '</div>';
      } else {
        confCell = '<div style="text-align:center"><span style="color:var(--yellow);font-size:10px">No CC</span><div class="drag-hint">\u2193 drop CC here</div></div>';
      }
    } else if (m.status === 'group_matched') {
      var c = m.confidence || {};
      var ov = c.overall || 0;
      var ovColor = ov >= 85 ? 'var(--green)' : ov >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      confCell = '<div style="text-align:center;line-height:1.4">'
        + '<div style="font-size:13px;font-weight:700;color:' + ovColor + '">' + ov + '%</div>'
        + '<div style="font-size:9px;color:var(--text-dim)">V:' + _confDot(c.vendor||0) + ' A:' + _confDot(c.amount||0) + ' D:' + _confDot(c.date||0) + '</div>'
        + '</div>';
    } else if (m.status === 'already_paid') {
      confCell = '<span style="color:var(--text-dim);font-size:10px">\u2713 Paid</span>';
    }

    // --- Special rendering for group_matched ---
    if (m.status === 'group_matched') {
      var gBills = m.grouped_bills || [];
      var billsSummary = gBills.map(function(b) {
        return b.vendor_name + ' \u20B9' + Number(b.amount).toLocaleString('en-IN') + ' (' + b.date + ')';
      }).join('<br>');
      var gDesc = m.cc_description || '-';
      var gDescFull = gDesc;
      if (gDesc.length > 40) gDesc = gDesc.substring(0, 40) + '\u2026';
      var grpDiff = Math.abs(m.cc_inr_amount - m.group_sum);
      var grpDiffColor = grpDiff < 1 ? 'var(--green)' : grpDiff < 10 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      var grpDiffLabel = grpDiff < 0.01 ? '0' : grpDiff.toFixed(2);
      tr.innerHTML = '<td style="padding:5px 4px"></td>'
        + '<td style="text-align:left;padding:5px 8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + gDescFull.replace(/"/g,'&quot;') + '">' + gDesc + '</td>'
        + '<td style="text-align:right;padding:5px 8px;font-family:monospace">' + fmt(m.cc_inr_amount) + '</td>'
        + '<td style="padding:5px 8px">' + fmtDate(m.cc_date) + '</td>'
        + '<td style="padding:5px 8px;font-size:10px">' + (m.cc_card||'-') + '</td>'
        + '<td style="text-align:left;padding:5px 8px;border-left:2px solid var(--border);font-size:10px;line-height:1.6" title="' + gBills.length + ' bills">'
          + '<span style="color:var(--accent);font-weight:600">' + gBills.length + ' bills</span><br>' + billsSummary + '</td>'
        + '<td style="text-align:right;padding:5px 8px;font-family:monospace">' + fmt(m.group_sum) + '</td>'
        + '<td style="padding:5px 4px;text-align:center">INR</td>'
        + '<td style="padding:5px 8px">' + (gBills.length > 0 ? fmtDate(gBills[0].date) : '-') + '</td>'
        + '<td style="text-align:center;padding:5px 6px;font-family:monospace;font-size:11px;font-weight:700;color:' + grpDiffColor + '" title="Diff: CC - Bills">' + grpDiffLabel + '</td>'
        + '<td style="padding:5px 8px">' + confCell + '</td>'
        + '<td style="padding:5px 8px"><button class="bill-create-btn" id="pay-grp-btn-' + idx + '" style="white-space:nowrap">Record Group</button></td>';
      tbody.appendChild(tr);
      // Attach group record handler with confirmation
      (function(groupData, btnId) {
        var btn = document.getElementById(btnId);
        if (btn) btn.onclick = function() {
          var billList = groupData.grouped_bills.map(function(b) { return '  ' + b.vendor_name + ' \u20B9' + Number(b.amount).toLocaleString('en-IN'); }).join('\n');
          var msg = 'Record GROUP payment?\n\nCC: ' + groupData.cc_description + '\nCC Amount: \u20B9' + Number(groupData.cc_inr_amount).toLocaleString('en-IN') + '\nDate: ' + groupData.cc_date + '\n\n' + groupData.grouped_bills.length + ' bills:\n' + billList + '\n\nGroup Sum: \u20B9' + Number(groupData.group_sum).toLocaleString('en-IN') + '\nConfidence: ' + (groupData.confidence.overall || 0) + '%';
          if (!confirm(msg)) return;
          recordGroupPayment(groupData, btn);
        };
      })(m, 'pay-grp-btn-' + idx);
      return; // skip normal row rendering
    }

    // CC columns (left side) — empty for unmatched/already_paid, candidate for unmatched+candidates
    // Note: innerHTML used throughout this internal tool; all data from own backend
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
      + '<td style="padding:5px 8px">' + (hasBill ? fmtDate(m.bill_date) : '<span style="'+dimStyle+'">-</span>') + '</td>';
    // Diff cell for normal rows — only for INR bills (CC INR vs Bill INR)
    // For forex bills (USD etc.), CC is INR and bill is foreign currency — not comparable
    var diffCell = '<td style="padding:5px 6px;text-align:center;color:var(--text-dim)">-</td>';
    if (m.status === 'matched' && m.cc_inr_amount && m.bill_amount && (m.bill_currency||'INR') === 'INR') {
      var rowDiff = Math.abs(m.cc_inr_amount - m.bill_amount);
      var rdColor = rowDiff < 1 ? 'var(--green)' : rowDiff < 10 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      diffCell = '<td style="text-align:center;padding:5px 6px;font-family:monospace;font-size:11px;font-weight:700;color:' + rdColor + '" title="Diff: CC INR - Bill INR">' + (rowDiff < 0.01 ? '0' : rowDiff.toFixed(2)) + '</td>';
    }
    tr.innerHTML += diffCell
      + '<td style="padding:5px 8px">' + confCell + '</td>'
      + '<td style="padding:5px 8px">' + actionBtn + '</td>';

    tbody.appendChild(tr);
  });

  tbl.appendChild(tbody);

  // Wrap main table in a scrollable container (60vh max)
  var mainWrap = document.createElement('div');
  mainWrap.id = 'paymentMainWrap';
  mainWrap.style.cssText = 'flex:1;overflow-y:auto;min-height:0';
  mainWrap.appendChild(tbl);
  content.appendChild(mainWrap);

  // --- Amex CC Matches table (for exclude/reference) ---
  var amexMatches = data.amex_matches || [];
  if (amexMatches.length > 0) {
    var amexHeader = document.createElement('div');
    amexHeader.style.cssText = 'padding:6px 12px;font-size:11px;font-weight:700;color:var(--yellow);display:flex;align-items:center;gap:8px;position:sticky;top:0;background:var(--bg);z-index:1;cursor:pointer;user-select:none';
    var _amexNotExcluded = amexMatches.filter(function(am) { return !_amexExcludedBills.has(am.bill_id); }).length;
    amexHeader.innerHTML = '<span id="amexCollapseArrow" style="display:inline-block;transition:transform 0.2s;transform:rotate(0deg);font-size:10px">\u25B6</span> '
      + '\u26A0 Amex CC Matches (' + amexMatches.length + ') &mdash; <span style="font-weight:400;color:var(--text-dim)">Bills matched to Amex. Click to expand.</span>'
      + ' <span style="margin-left:auto;display:flex;gap:8px">'
      + '<button id="amexBulkExcludeBtn" onclick="event.stopPropagation();bulkAmexExclude()" style="background:var(--yellow);color:#000;border:none;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">Exclude All (' + _amexNotExcluded + ')</button>'
      + '</span>';
    amexHeader.onclick = function(e) {
      if (e.target.tagName === 'BUTTON') return;
      var tblEl = document.getElementById('amexCollapsibleBody');
      var arrow = document.getElementById('amexCollapseArrow');
      if (tblEl.style.display === 'none') {
        tblEl.style.display = '';
        arrow.style.transform = 'rotate(90deg)';
      } else {
        tblEl.style.display = 'none';
        arrow.style.transform = 'rotate(0deg)';
      }
    };

    var atbl = document.createElement('table');
    atbl.className = 'match-table';
    atbl.style.cssText = 'width:100%;font-size:10px';
    atbl.innerHTML = '<thead><tr>'
      + '<th style="padding:3px 4px;text-align:center;width:24px"><input type="checkbox" id="amexSelectAll" onchange="toggleAmexSelectAll(this)"></th>'
      + '<th style="text-align:left;padding:3px 6px">Amex CC Description</th>'
      + '<th style="text-align:right;padding:3px 6px">CC INR</th>'
      + '<th style="padding:3px 6px">CC Date</th>'
      + '<th style="text-align:left;padding:3px 6px;border-left:2px solid var(--border)">Vendor / Bill</th>'
      + '<th style="text-align:right;padding:3px 6px">Bill Amt</th>'
      + '<th style="padding:3px 4px">Cur</th>'
      + '<th style="padding:3px 6px">Bill Date</th>'
      + '<th style="padding:3px 6px;text-align:center">Confidence</th>'
      + '<th style="padding:3px 6px">Action</th>'
      + '</tr></thead>';

    var atbody = document.createElement('tbody');
    amexMatches.forEach(function(am) {
      var atr = document.createElement('tr');
      atr.id = 'amex-row-' + am.bill_id;
      atr.style.background = 'rgba(255,200,50,0.04)';
      var ccDesc = am.cc_description || '-';
      var ccDescFull = ccDesc;
      if (ccDesc.length > 45) ccDesc = ccDesc.substring(0, 45) + '\u2026';
      var forexNote = am.cc_forex_amount ? ' (' + (am.cc_forex_currency||'') + ' ' + fmt(am.cc_forex_amount) + ')' : '';
      var c = am.confidence || {};
      var ov = c.overall || 0;
      var ovColor = ov >= 85 ? 'var(--green)' : ov >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      var confCell = '<div style="text-align:center;line-height:1.2">'
        + '<div style="font-size:11px;font-weight:700;color:' + ovColor + '">' + ov + '%</div>'
        + '<div style="font-size:8px;color:var(--text-dim)">V:' + _confDot(c.vendor||0) + ' A:' + _confDot(c.amount||0) + ' D:' + _confDot(c.date||0) + '</div>'
        + '</div>';
      atr.innerHTML = ''
        + '<td style="text-align:center;padding:2px 4px"><input type="checkbox" class="amex-cb" data-billid="' + am.bill_id + '" onchange="updateAmexSelectedBtn()"></td>'
        + '<td style="text-align:left;padding:2px 6px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + ccDescFull.replace(/"/g,'&quot;') + '">' + ccDesc + forexNote + '</td>'
        + '<td style="text-align:right;padding:2px 6px;font-family:monospace">' + fmt(am.cc_inr_amount) + '</td>'
        + '<td style="padding:2px 6px">' + fmtDate(am.cc_date) + '</td>'
        + '<td style="text-align:left;padding:2px 6px;border-left:2px solid var(--border);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (am.vendor_name||'').replace(/"/g,'&quot;') + '">' + (am.vendor_name||'-') + '</td>'
        + '<td style="text-align:right;padding:2px 6px;font-family:monospace">' + fmt(am.bill_amount) + '</td>'
        + '<td style="padding:2px 4px;text-align:center">' + (am.bill_currency||'INR') + '</td>'
        + '<td style="padding:2px 6px">' + fmtDate(am.bill_date) + '</td>'
        + '<td style="padding:2px 6px">' + confCell + '</td>'
        + '<td style="padding:2px 6px"><button class="bill-create-btn" id="amex-btn-' + am.bill_id + '" onclick="toggleAmexExclude(\'' + am.bill_id + '\')" style="background:var(--yellow);color:#000;font-weight:600;padding:2px 8px;font-size:10px">Exclude</button></td>';
      atbody.appendChild(atr);
    });
    atbl.appendChild(atbody);
    var amexCollapsible = document.createElement('div');
    amexCollapsible.id = 'amexCollapsibleBody';
    amexCollapsible.style.cssText = 'display:none;max-height:130px;overflow-y:auto';
    amexCollapsible.appendChild(atbl);
    var amexWrap = document.createElement('div');
    amexWrap.id = 'paymentAmexWrap';
    amexWrap.style.cssText = 'flex-shrink:0;border-top:2px solid var(--border);max-height:180px;overflow-y:auto';
    amexWrap.appendChild(amexHeader);
    amexWrap.appendChild(amexCollapsible);
    content.appendChild(amexWrap);
    amexMatches.forEach(function(am) {
      if (_amexExcludedBills.has(am.bill_id)) _applyAmexExcludeUI(am.bill_id);
    });
  }
}

// --- Record Group Payment ---
function recordGroupPayment(groupData, btn) {
  btn.disabled = true;
  btn.textContent = 'Recording...';
  var payload = {
    bill_ids: groupData.grouped_bills.map(function(b) { return b.bill_id; }),
    cc_inr_amount: groupData.cc_inr_amount,
    cc_date: groupData.cc_date,
    cc_card: groupData.cc_card,
    cc_description: groupData.cc_description
  };
  fetch('/api/payments/record-group', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function(r) { return r.json(); })
  .then(function(res) {
    if (res.status === 'paid') {
      btn.textContent = '\u2713 Paid';
      btn.style.background = 'rgba(34,197,94,0.3)';
      btn.style.cursor = 'default';
      btn.parentElement.parentElement.style.opacity = '0.5';
    } else {
      btn.textContent = res.error || res.message || 'Failed';
      btn.style.color = 'var(--red,#ef4444)';
      btn.disabled = false;
    }
  }).catch(function(err) {
    btn.textContent = 'Error';
    btn.style.color = 'var(--red,#ef4444)';
    btn.disabled = false;
  });
}

// --- Amex Exclude/Include toggle ---
var _amexExcludedBills = new Set();

function toggleAmexExclude(billId) {
  var btn = document.getElementById('amex-btn-' + billId);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  var isExcluded = _amexExcludedBills.has(billId);
  var action = isExcluded ? 'include' : 'exclude';

  fetch('/api/amex-exclude', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_id: billId, action: action}),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.ok) {
      if (action === 'exclude') {
        _amexExcludedBills.add(billId);
      } else {
        _amexExcludedBills.delete(billId);
      }
      _applyAmexExcludeUI(billId);
      _updateAmexBulkBtn();
      filterPayments();
    }
    if (btn) btn.disabled = false;
  })
  .catch(function() { if (btn) { btn.disabled = false; btn.textContent = 'Error'; } });
}

function _applyAmexExcludeUI(billId) {
  var btn = document.getElementById('amex-btn-' + billId);
  var amexRow = document.getElementById('amex-row-' + billId);
  if (_amexExcludedBills.has(billId)) {
    if (btn) { btn.textContent = 'Include'; btn.style.background = 'var(--border)'; btn.style.color = 'var(--text-dim)'; }
    if (amexRow) amexRow.style.opacity = '0.5';
  } else {
    if (btn) { btn.textContent = 'Exclude'; btn.style.background = 'var(--yellow)'; btn.style.color = '#000'; }
    if (amexRow) amexRow.style.opacity = '1';
  }
}

function toggleAmexSelectAll(cb) {
  document.querySelectorAll('.amex-cb').forEach(function(c) {
    var row = c.closest('tr');
    if (row && row.style.opacity !== '0.5') c.checked = cb.checked;  // skip already excluded
  });
  updateAmexSelectedBtn();
}

function updateAmexSelectedBtn() {
  var checked = document.querySelectorAll('.amex-cb:checked');
  var btn = document.getElementById('amexExcludeSelectedBtn');
  if (btn) {
    if (checked.length > 0) {
      btn.style.display = '';
      btn.textContent = 'Exclude Selected (' + checked.length + ')';
    } else {
      btn.style.display = 'none';
    }
  }
}

function excludeSelectedAmex() {
  var checked = document.querySelectorAll('.amex-cb:checked');
  var ids = [];
  checked.forEach(function(c) { ids.push(c.getAttribute('data-billid')); });
  if (!ids.length) return;

  var btn = document.getElementById('amexExcludeSelectedBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Excluding...'; }

  fetch('/api/amex-exclude', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_ids: ids, action: 'exclude'}),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.ok) {
      ids.forEach(function(id) {
        _amexExcludedBills.add(id);
        _applyAmexExcludeUI(id);
      });
      // Uncheck all and update
      document.querySelectorAll('.amex-cb').forEach(function(c) { c.checked = false; });
      var selAll = document.getElementById('amexSelectAll');
      if (selAll) selAll.checked = false;
      updateAmexSelectedBtn();
      _updateAmexBulkBtn();
      filterPayments();
    }
    if (btn) btn.disabled = false;
  })
  .catch(function() { if (btn) { btn.disabled = false; btn.textContent = 'Error'; } });
}

function _updateAmexBulkBtn() {
  var amexMatches = (_paymentPreviewData && _paymentPreviewData.amex_matches) || [];
  var remaining = amexMatches.filter(function(am) { return !_amexExcludedBills.has(am.bill_id); }).length;
  var btn = document.getElementById('amexBulkExcludeBtn');
  if (btn) {
    btn.textContent = 'Exclude All (' + remaining + ')';
    if (remaining === 0) { btn.disabled = true; btn.style.background = 'var(--border)'; btn.style.color = 'var(--text-dim)'; }
  }
}

function bulkAmexExclude() {
  var amexMatches = (_paymentPreviewData && _paymentPreviewData.amex_matches) || [];
  // Collect bill IDs not yet excluded
  var toExclude = [];
  amexMatches.forEach(function(am) {
    if (!_amexExcludedBills.has(am.bill_id)) toExclude.push(am.bill_id);
  });
  if (!toExclude.length) return;

  var btn = document.getElementById('amexBulkExcludeBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Excluding...'; }

  // Send all at once
  fetch('/api/amex-exclude', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bill_ids: toExclude, action: 'exclude'}),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.ok) {
      toExclude.forEach(function(id) {
        _amexExcludedBills.add(id);
        _applyAmexExcludeUI(id);
      });
      filterPayments();
      _updateAmexBulkBtn();
    }
  })
  .catch(function() { if (btn) { btn.disabled = false; btn.textContent = 'Error'; } });
}

function confirmRecordOne(billId) {
  var m = _paymentPreviewData && _paymentPreviewData.matches
    ? _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId && x.status === 'matched'; })
    : null;
  var desc = m ? (m.vendor_name || '') + ' — ' + (m.bill_currency||'INR') + ' ' + Number(m.bill_amount).toLocaleString() + ' via ' + (m.cc_card||'CC') : billId;
  showModal('Record Payment?', 'This will mark the bill as PAID in Zoho Books: ' + desc, function() {
    recordOnePayment(billId, _paymentPreviewData);
  }, false, 'Record');
}

function recordOnePayment(billId, previewData) {
  var btn = document.getElementById('pay-btn-' + billId);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  // Find the matched CC info from preview data
  var payload = {bill_id: billId};
  if (previewData && previewData.matches) {
    var m = previewData.matches.find(function(x) { return x.bill_id === billId && x.status === 'matched'; });
    if (m) {
      payload.cc_transaction_id = m.cc_transaction_id;
      payload.cc_description = m.cc_description;
      payload.cc_inr_amount = m.cc_inr_amount;
      payload.cc_date = m.cc_date;
      payload.cc_card = m.cc_card;
      if (m.cc_forex_amount) {
        payload.cc_forex_amount = m.cc_forex_amount;
        payload.cc_forex_currency = m.cc_forex_currency;
      }
    }
  }

  fetch('/api/payments/record-one', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var row = document.getElementById('pay-row-' + billId);
    if (data.status === 'paid' || data.status === 'already_paid') {
      // Update the row to "already_paid" status
      if (row) {
        row.setAttribute('data-status', 'already_paid');
        row.style.background = 'rgba(150,150,150,0.04)';
        // Remove checkbox
        var cbCell = row.querySelector('.pay-cb');
        if (cbCell) { cbCell.parentElement.innerHTML = ''; _paySelectedBills.delete(billId); _updatePaySelectedBtn(); }
        // Update confidence cell to show paid
        var cells = row.querySelectorAll('td');
        if (cells.length >= 10) cells[9].innerHTML = '<span style="color:var(--text-dim);font-size:10px">\u2713 Paid</span>';
      }
      if (btn) { btn.textContent = data.status === 'already_paid' ? 'Already Paid' : '\u2713 Paid'; btn.disabled = true; btn.style.color = 'var(--text-dim)'; btn.onclick = null; }
      // Update underlying data
      if (_paymentPreviewData && _paymentPreviewData.matches) {
        var mi = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
        if (mi) mi.status = 'already_paid';
      }
      // Move row to already_paid section
      if (row) {
        var tbody = row.closest('tbody');
        if (tbody) {
          // Find or create already_paid separator
          var apSep = tbody.querySelector('.pay-sep-other');
          if (!apSep) {
            apSep = document.createElement('tr');
            apSep.className = 'pay-section-sep pay-sep-other';
            apSep.innerHTML = '<td colspan="11" style="padding:7px 10px;font-size:11px;font-weight:700;color:var(--text-dim);border-top:2px solid var(--border);background:rgba(150,150,150,0.06)">\u2713 Already Paid</td>';
            tbody.appendChild(apSep);
          }
          apSep.style.display = '';
          // Move row after the separator (before next section or at end)
          var nextSib = apSep.nextSibling;
          if (nextSib) tbody.insertBefore(row, nextSib);
          else tbody.appendChild(row);
        }
      }
      // Recount and update summary
      filterPayments();
      addLogLine('[Payment] ' + (data.status === 'already_paid' ? 'Already paid: ' : 'Recorded: ') + billId + (data.payment_id ? ' -> ' + data.payment_id : ''));
    } else {
      if (btn) { btn.textContent = 'Failed'; btn.disabled = false; btn.style.color = 'var(--yellow)'; }
      addLogLine('[Payment] No match for ' + billId);
    }
  })
  .catch(function(err) {
    if (btn) { btn.textContent = 'Error'; btn.disabled = false; }
    addLogLine('[Payment] Error: ' + err);
  });
}

// --- Manual drag-and-drop match ---
var _dropPreviewTimer = null;

function previewManualDrop(billRow, billMatch, ccData) {
  // Clear any previous preview
  if (_dropPreviewTimer) { clearTimeout(_dropPreviewTimer); _dropPreviewTimer = null; }
  _cancelActiveDropPreview();

  var cells = billRow.querySelectorAll('td');
  var origHtml = {};
  // Save original cells: 0=checkbox, 1-4=CC cols, 9=diff, 10=confidence, 11=action
  [0, 1, 2, 3, 4, 9, 10, 11].forEach(function(ci) {
    if (cells[ci]) origHtml[ci] = cells[ci].innerHTML;
  });

  // Fill CC columns with dropped data (accent colored)
  var ccDescShort = ccData.cc_description.length > 35 ? ccData.cc_description.substring(0, 35) + '\u2026' : ccData.cc_description;
  var fxNote = ccData.cc_forex_amount ? ' (' + ccData.cc_forex_currency + ' ' + Number(ccData.cc_forex_amount).toLocaleString() + ')' : '';
  if (cells[1]) cells[1].innerHTML = '<span style="color:var(--accent);font-weight:600" title="' + ccData.cc_description.replace(/"/g,'&quot;') + '">' + ccDescShort + fxNote + '</span>';
  if (cells[2]) cells[2].innerHTML = '<span style="color:var(--accent);font-weight:600;font-family:monospace">' + Number(ccData.cc_inr_amount).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) + '</span>';
  if (cells[3]) cells[3].innerHTML = '<span style="color:var(--accent)">' + (ccData.cc_date ? ccData.cc_date.split('-').reverse().join('-') : '-') + '</span>';
  if (cells[4]) cells[4].innerHTML = '<span style="color:var(--accent);font-size:10px">' + (ccData.cc_card || '-') + '</span>';

  // Compute diff
  var diffText = '';
  var billCur = billMatch.bill_currency || 'INR';
  if (billCur === 'INR') {
    var diff = Math.abs(ccData.cc_inr_amount - billMatch.bill_amount);
    diffText = '\u20B9' + diff.toFixed(2);
  } else if (ccData.cc_forex_currency && ccData.cc_forex_currency.toUpperCase() === billCur.toUpperCase()) {
    var diff = Math.abs(ccData.cc_forex_amount - billMatch.bill_amount);
    diffText = ccData.cc_forex_currency + ' ' + diff.toFixed(2);
  } else {
    diffText = billCur + ' vs INR';
  }
  if (cells[9]) cells[9].innerHTML = '<span style="color:var(--accent);font-size:11px;font-weight:600">' + diffText + '</span>';

  // Show OK / No buttons inline in confidence + action cells
  if (cells[10]) cells[10].innerHTML = '<button class="bill-create-btn" style="background:var(--green);color:#000;border-color:var(--green);font-size:11px;padding:4px 14px" id="drop-ok-' + billMatch.bill_id + '">OK</button>';
  if (cells[11]) cells[11].innerHTML = '<button class="bill-create-btn" style="color:var(--red,#ef4444);border-color:var(--red,#ef4444);font-size:11px;padding:4px 14px" id="drop-no-' + billMatch.bill_id + '">No</button>';

  // Flash row
  billRow.style.transition = 'background 0.3s ease';
  billRow.style.background = 'rgba(108,140,255,0.18)';
  setTimeout(function() { billRow.style.background = 'rgba(108,140,255,0.08)'; }, 400);

  // Dim source CC row
  var ccRow = document.getElementById('pay-row-cc-' + ccData.idx);
  if (ccRow) { ccRow.style.opacity = '0.3'; ccRow.style.transition = 'opacity 0.3s'; }

  // Restore function
  function restoreRow() {
    Object.keys(origHtml).forEach(function(ci) {
      if (cells[ci]) cells[ci].innerHTML = origHtml[ci];
    });
    billRow.style.background = 'rgba(255,200,50,0.04)';
    if (ccRow) ccRow.style.opacity = '1';
    _activeDropPreview = null;
  }

  // Store active preview for cleanup
  _activeDropPreview = { restore: restoreRow };

  // Wire OK button — show confirmation modal then Record
  var okBtn = document.getElementById('drop-ok-' + billMatch.bill_id);
  if (okBtn) okBtn.onclick = function() {
    _activeDropPreview = null;

    // Build diff info for modal
    var diffInfo = '';
    var _billCur = billMatch.bill_currency || 'INR';
    if (_billCur === 'INR') {
      var _d = Math.abs(ccData.cc_inr_amount - billMatch.bill_amount);
      diffInfo = 'Difference: \u20B9' + _d.toFixed(2);
    } else if (ccData.cc_forex_currency && ccData.cc_forex_currency.toUpperCase() === _billCur.toUpperCase()) {
      var _d = Math.abs(ccData.cc_forex_amount - billMatch.bill_amount);
      diffInfo = 'Difference: ' + ccData.cc_forex_currency + ' ' + _d.toFixed(2);
    } else {
      diffInfo = 'Note: Currency mismatch \u2014 manual review recommended';
    }

    var modalBody = '<div style="text-align:left;line-height:1.8">'
        + '<div style="color:var(--accent);font-weight:600">CC Transaction:</div>'
        + '<div style="margin-left:12px">' + ccData.cc_description + '</div>'
        + '<div style="margin-left:12px">\u20B9' + Number(ccData.cc_inr_amount).toLocaleString('en-IN')
          + (ccData.cc_forex_amount ? ' (' + ccData.cc_forex_currency + ' ' + Number(ccData.cc_forex_amount).toLocaleString() + ')' : '')
          + ' | ' + ccData.cc_date + ' | ' + ccData.cc_card + '</div>'
        + '<div style="margin-top:8px;color:var(--yellow);font-weight:600">Bill:</div>'
        + '<div style="margin-left:12px">' + (billMatch.vendor_name || '') + ' | #' + (billMatch.bill_number || '') + '</div>'
        + '<div style="margin-left:12px">' + (billMatch.bill_currency || 'INR') + ' ' + Number(billMatch.bill_amount).toLocaleString() + ' | ' + billMatch.bill_date + '</div>'
        + '<div style="margin-top:8px;font-weight:600;color:var(--text-dim)">' + diffInfo + '</div>'
      + '</div>';

    // Step 1: Show modal with OK button
    showModal(
      'Manual Match \u2014 Confirm?',
      modalBody,
      function() {
        // Step 2: OK clicked — show Record Payment button
        showModal(
          'Manual Match \u2014 Record Payment',
          modalBody + '<div style="margin-top:10px;padding:8px 12px;background:rgba(80,200,120,0.1);border-radius:6px;border:1px solid var(--green);font-size:11px;color:var(--green)">\u2714 Match confirmed. Click Record Payment to save to Zoho.</div>',
          function() { recordManualMatch(billMatch, ccData); },
          false, 'Record Payment',
          restoreRow
        );
      },
      false, 'OK',
      restoreRow
    );
  };

  // Wire No button — restore original
  var noBtn = document.getElementById('drop-no-' + billMatch.bill_id);
  if (noBtn) noBtn.onclick = function() { restoreRow(); };
}

var _activeDropPreview = null;
function _cancelActiveDropPreview() {
  if (_activeDropPreview && _activeDropPreview.restore) _activeDropPreview.restore();
  _activeDropPreview = null;
}

function recordManualMatch(billMatch, ccData) {
  var billId = billMatch.bill_id;
  var btn = document.getElementById('pay-btn-' + billId);

  var payload = {
    bill_id: billId,
    cc_transaction_id: ccData.cc_transaction_id || '',
    cc_description: ccData.cc_description,
    cc_inr_amount: ccData.cc_inr_amount,
    cc_date: ccData.cc_date,
    cc_card: ccData.cc_card
  };
  if (ccData.cc_forex_amount) {
    payload.cc_forex_amount = ccData.cc_forex_amount;
    payload.cc_forex_currency = ccData.cc_forex_currency;
  }

  addLogLine('[Manual Match] Recording: ' + ccData.cc_description + ' \u2192 ' + (billMatch.vendor_name || '') + ' #' + (billMatch.bill_number || ''));

  fetch('/api/payments/record-one', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var billRow = document.getElementById('pay-row-' + billId);
    var ccRow = document.getElementById('pay-row-cc-' + ccData.idx);

    if (data.status === 'paid' || data.status === 'already_paid') {
      // Update bill row to paid
      if (billRow) {
        billRow.setAttribute('data-status', 'already_paid');
        billRow.style.background = 'rgba(150,150,150,0.04)';
        billRow.classList.remove('pay-drop-target');
        var cbCell = billRow.querySelector('.pay-cb');
        if (cbCell) { cbCell.parentElement.innerHTML = ''; _paySelectedBills.delete(billId); _updatePaySelectedBtn(); }
        var cells = billRow.querySelectorAll('td');
        // Update CC columns on the bill row to show what was matched
        if (cells.length >= 5) {
          var ccDescShort = ccData.cc_description.length > 40 ? ccData.cc_description.substring(0, 40) + '\u2026' : ccData.cc_description;
          cells[1].innerHTML = '<span title="' + ccData.cc_description.replace(/"/g,'&quot;') + '">' + ccDescShort + '</span>';
          cells[1].style.color = '';
          cells[2].innerHTML = Number(ccData.cc_inr_amount).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2});
          cells[3].textContent = ccData.cc_date ? ccData.cc_date.split('-').reverse().join('-') : '-';
          cells[4].textContent = ccData.cc_card || '-';
        }
        if (cells.length >= 11) {
          cells[10].innerHTML = '<span style="color:var(--text-dim);font-size:10px">\u2713 Paid (manual)</span>';
        }
        // Move to Already Paid section
        var tbody = billRow.closest('tbody');
        if (tbody) {
          var apSep = tbody.querySelector('.pay-sep-other');
          if (!apSep) {
            apSep = document.createElement('tr');
            apSep.className = 'pay-section-sep pay-sep-other';
            apSep.innerHTML = '<td colspan="11" style="padding:7px 10px;font-size:11px;font-weight:700;color:var(--text-dim);border-top:2px solid var(--border);background:rgba(150,150,150,0.06)">\u2713 Already Paid</td>';
            tbody.appendChild(apSep);
          }
          apSep.style.display = '';
          var nextSib = apSep.nextSibling;
          if (nextSib) tbody.insertBefore(billRow, nextSib);
          else tbody.appendChild(billRow);
        }
      }
      // Hide the CC row (it's now consumed)
      if (ccRow) {
        ccRow.style.display = 'none';
        ccRow.setAttribute('data-status', 'used');
      }
      // Update underlying data
      if (_paymentPreviewData && _paymentPreviewData.matches) {
        var mi = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
        if (mi) mi.status = 'already_paid';
      }
      filterPayments();
      addLogLine('[Manual Match] \u2713 Payment recorded: ' + (billMatch.vendor_name || '') + ' #' + (billMatch.bill_number || '') + (data.payment_id ? ' -> ' + data.payment_id : ''));
    } else {
      addLogLine('[Manual Match] Failed: ' + (data.message || 'Unknown error'));
    }
  })
  .catch(function(err) {
    addLogLine('[Manual Match] Error: ' + err);
  });
}

// --- Payment checkbox selection ---
var _paySelectedBills = new Set();

function togglePayCheckbox(cb) {
  var billId = cb.getAttribute('data-billid');
  if (cb.checked) _paySelectedBills.add(billId);
  else _paySelectedBills.delete(billId);
  _updatePaySelectedBtn();
  _updateCandidateSelectedBtn();
}

function togglePaySelectAll(cb) {
  var checkboxes = document.querySelectorAll('.pay-cb');
  checkboxes.forEach(function(c) {
    // Only toggle visible (not filtered out) rows
    var row = c.closest('tr');
    if (row && row.style.display === 'none') return;
    c.checked = cb.checked;
    var billId = c.getAttribute('data-billid');
    if (cb.checked) _paySelectedBills.add(billId);
    else _paySelectedBills.delete(billId);
  });
  _updatePaySelectedBtn();
}

function _updatePaySelectedBtn() {
  var btn = document.getElementById('recordSelectedBtn');
  var count = _paySelectedBills.size;
  if (count > 0) {
    btn.style.display = 'inline-block';
    btn.textContent = 'Record Selected (' + count + ')';
    btn.disabled = false;
  } else {
    btn.style.display = 'none';
  }
}

function confirmRecordSelected() {
  var count = _paySelectedBills.size;
  if (!count) return;
  showModal('Record ' + count + ' Selected Payments?', 'This will mark ' + count + ' selected bills as PAID in Zoho Books.', function() {
    recordSelectedPayments();
  }, true, 'Record Selected');
}

function recordSelectedPayments() {
  if (!_paymentPreviewData || !_paySelectedBills.size) return;
  var selectedItems = _paymentPreviewData.matches
    .filter(function(m) { return m.status === 'matched' && _paySelectedBills.has(m.bill_id); })
    .map(function(m) {
      var item = {bill_id: m.bill_id, cc_transaction_id: m.cc_transaction_id, cc_inr_amount: m.cc_inr_amount, cc_date: m.cc_date, cc_card: m.cc_card};
      if (m.cc_forex_amount) { item.cc_forex_amount = m.cc_forex_amount; item.cc_forex_currency = m.cc_forex_currency; }
      return item;
    });

  if (!selectedItems.length) return;

  var selBtn = document.getElementById('recordSelectedBtn');
  selBtn.disabled = true;
  selBtn.textContent = 'Recording ' + selectedItems.length + '...';

  selectedItems.forEach(function(item) {
    var btn = document.getElementById('pay-btn-' + item.bill_id);
    if (btn) { btn.disabled = true; btn.textContent = '...'; }
  });

  fetch('/api/payments/record-selected', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({items: selectedItems}),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    var results = data.results || [];
    var paidCount = 0;
    results.forEach(function(r) {
      var row = document.getElementById('pay-row-' + r.bill_id);
      var btn = document.getElementById('pay-btn-' + r.bill_id);
      var cb = document.querySelector('.pay-cb[data-billid="' + r.bill_id + '"]');
      if (r.status === 'paid' || r.status === 'already_paid') {
        if (row) {
          row.setAttribute('data-status', 'already_paid');
          row.style.background = 'rgba(150,150,150,0.04)';
        }
        if (btn) { btn.textContent = '\u2713 Paid'; btn.style.color = 'var(--green)'; btn.disabled = true; btn.onclick = null; }
        if (cb) { cb.checked = false; cb.parentElement.innerHTML = ''; }
        _paySelectedBills.delete(r.bill_id);
        // Update underlying data
        if (_paymentPreviewData && _paymentPreviewData.matches) {
          var mi = _paymentPreviewData.matches.find(function(x) { return x.bill_id === r.bill_id; });
          if (mi) mi.status = 'already_paid';
        }
        paidCount++;
      } else {
        if (btn) { btn.textContent = r.status; btn.disabled = false; }
      }
    });
    selBtn.textContent = paidCount + '/' + selectedItems.length + ' Recorded';
    _updatePaySelectedBtn();
    addLogLine('[Payment] Selected record: ' + paidCount + '/' + selectedItems.length + ' paid');
    filterPayments();
  })
  .catch(function(err) {
    selBtn.textContent = 'Error';
    selBtn.disabled = false;
    addLogLine('[Payment] Selected error: ' + err);
  });
}

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

function searchCandidates(billId) {
  var input = document.getElementById('cand-search-' + billId);
  var resultsDiv = document.getElementById('cand-search-results-' + billId);
  if (!input || !resultsDiv) return;
  var query = input.value.trim().toLowerCase();
  if (query.length < 2) { resultsDiv.textContent = ''; return; }

  var m = _paymentPreviewData.matches.find(function(x) { return x.bill_id === billId; });
  if (!m) return;

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
  // Check if all visible are already selected — if so, deselect all
  var allSelected = true;
  matches.forEach(function(m) {
    if (m.status !== 'unmatched' || !m.candidates || m.candidates.length === 0) return;
    var topScore = m.candidates[0].candidate_score;
    if (threshold > 0 && topScore < threshold) return;
    var row = document.getElementById('pay-row-' + m.bill_id);
    if (!row || row.style.display === 'none') return;
    var cb = row.querySelector('.pay-cb');
    if (cb && !cb.checked) allSelected = false;
  });
  matches.forEach(function(m) {
    if (m.status !== 'unmatched' || !m.candidates || m.candidates.length === 0) return;
    var topScore = m.candidates[0].candidate_score;
    if (threshold > 0 && topScore < threshold) return;
    var row = document.getElementById('pay-row-' + m.bill_id);
    if (!row || row.style.display === 'none') return;
    var cb = row.querySelector('.pay-cb');
    if (allSelected) {
      if (cb && cb.checked) { cb.checked = false; togglePayCheckbox(cb); }
    } else {
      if (cb && !cb.checked) { cb.checked = true; togglePayCheckbox(cb); }
    }
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

function renderCheckPanel(data) {
  const grouped = data.grouped || [];
  const summary = data.summary || {};
  const fmt = n => n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-';

  document.getElementById('checkSummaryText').textContent =
    (summary.vendors_count || 0) + ' vendors · ' + (summary.bills_count || 0) + ' bills · ' + (summary.cc_transactions_count || 0) + ' CC matched · ' + (summary.unmatched_cc_count || 0) + ' CC unmatched';

  const content = document.getElementById('checkContent');
  content.innerHTML = '';

  if (!grouped.length) {
    content.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:40px">No data found</div>';
  } else {
    // Sticky column headers
    const colHeader = document.createElement('div');
    colHeader.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;position:sticky;top:0;z-index:2;background:var(--bg-panel);border-bottom:2px solid var(--border);flex-shrink:0';
    colHeader.innerHTML =
      '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--accent)">Bills</div>' +
      '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--yellow);border-left:1px solid var(--border)">CC Transactions</div>';
    content.appendChild(colHeader);

    grouped.forEach(function(group) {
      const billsLen = group.bills.length;
      const ccLen = group.cc_transactions.length;
      const hasMatch = billsLen > 0 && ccLen > 0;

      // Vendor header row
      const vendorHeader = document.createElement('div');
      vendorHeader.style.cssText = 'padding:5px 12px;font-size:12px;font-weight:600;background:rgba(255,255,255,0.04);display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);border-top:1px solid var(--border)';
      vendorHeader.innerHTML =
        '<span style="color:' + (hasMatch ? '#4ade80' : 'var(--text-dim)') + ';font-size:8px">●</span>' +
        '<span>' + group.vendor + '</span>' +
        '<span style="font-size:10px;color:var(--text-dim);font-weight:400">' + billsLen + ' bill' + (billsLen!==1?'s':'') + ' · ' + ccLen + ' CC txn' + (ccLen!==1?'s':'') + '</span>' +
        (!hasMatch ? '<span style="font-size:10px;color:#f59e0b;margin-left:auto">⚠ no match</span>' : '');

      // Two-column body
      const cols = document.createElement('div');
      cols.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--border)';

      // Bills column
      const billsCol = document.createElement('div');
      billsCol.style.cssText = 'border-right:1px solid var(--border);min-width:0';
      if (!billsLen) {
        billsCol.innerHTML = '<div style="padding:6px 12px;font-size:11px;color:var(--text-dim)">— no bills</div>';
      } else {
        const tbl = document.createElement('table');
        tbl.className = 'match-table';
        tbl.style.width = '100%';
        const thead = '<thead><tr><th>Amount</th><th>Cur</th><th>Date</th></tr></thead>';
        const tbody = document.createElement('tbody');
        group.bills.forEach(function(b) {
          const tr = document.createElement('tr');
          tr.innerHTML = '<td style="font-family:monospace;font-size:11px">' + fmt(b.amount) + '</td><td>' + (b.currency||'INR') + '</td><td>' + fmtDate(b.date) + '</td>';
          tbody.appendChild(tr);
        });
        tbl.innerHTML = thead;
        tbl.appendChild(tbody);
        billsCol.appendChild(tbl);
      }

      // CC column
      const ccCol = document.createElement('div');
      ccCol.style.cssText = 'min-width:0';
      if (!ccLen) {
        ccCol.innerHTML = '<div style="padding:6px 12px;font-size:11px;color:var(--text-dim)">— no CC match</div>';
      } else {
        const tbl = document.createElement('table');
        tbl.className = 'match-table';
        tbl.style.width = '100%';
        const thead = '<thead><tr><th>Description</th><th>INR Amt</th><th>Date</th><th>Card</th></tr></thead>';
        const tbody = document.createElement('tbody');
        group.cc_transactions.forEach(function(t) {
          const tr = document.createElement('tr');
          const cardShort = (t.card_name||'-').replace('CC ','');
          tr.innerHTML =
            '<td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px" title="' + t.description + '">' + (t.description||'-') + '</td>' +
            '<td style="font-family:monospace;font-size:11px">' + fmt(t.amount) + '</td>' +
            '<td>' + fmtDate(t.date) + '</td>' +
            '<td style="font-size:10px;color:var(--text-dim)">' + cardShort + '</td>';
          tbody.appendChild(tr);
        });
        tbl.innerHTML = thead;
        tbl.appendChild(tbody);
        ccCol.appendChild(tbl);
      }

      cols.appendChild(billsCol);
      cols.appendChild(ccCol);

      const section = document.createElement('div');
      section.appendChild(vendorHeader);
      section.appendChild(cols);
      content.appendChild(section);
    });
  }

  document.getElementById('checkLoading').style.display = 'none';
  document.getElementById('checkContent').style.display = 'block';
}

// --- Monthly Compare Panel ---
var _compareData = null;

function openComparePanel() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('invoiceBrowsePanel').style.display = 'none';
  document.getElementById('extractPreviewPanel').style.display = 'none';
  document.getElementById('ccPreviewPanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'flex';
  document.getElementById('compareLoading').style.display = 'block';
  document.getElementById('compareContent').style.display = 'none';

  fetch('/api/compare/monthly')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('compareLoading').textContent = data.error;
        return;
      }
      _compareData = data;
      renderComparePanel(data);
    })
    .catch(function(err) {
      document.getElementById('compareLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeComparePanel() {
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

// --- Invoice Browse Panel ---
var _invBrowseData = null;
var _invBrowseAllRows = [];

function openInvoiceBrowse() {
  document.getElementById('logPanel').style.display = 'none';
  document.getElementById('reviewPanel').style.display = 'none';
  document.getElementById('matchPanel').style.display = 'none';
  document.getElementById('checkPanel').style.display = 'none';
  document.getElementById('paymentPanel').style.display = 'none';
  document.getElementById('comparePanel').style.display = 'none';
  document.getElementById('bankingSummaryPanel').style.display = 'none';
  document.getElementById('autoMatchPanel').style.display = 'none';
  document.getElementById('invoiceBrowsePanel').style.display = 'flex';
  document.getElementById('invBrowseLoading').style.display = 'block';
  document.getElementById('invBrowseContent').style.display = 'none';
  document.getElementById('invBrowseFilterBar').style.display = 'none';

  fetch('/api/invoices/browse')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        document.getElementById('invBrowseLoading').textContent = data.error;
        return;
      }
      _invBrowseData = data;

      // Flatten all rows
      _invBrowseAllRows = [];
      data.months.forEach(function(m) {
        _invBrowseAllRows = _invBrowseAllRows.concat(data.data[m]);
      });

      // Populate month dropdown
      var sel = document.getElementById('invBrowseMonthSelect');
      sel.innerHTML = '<option value="all">All Months</option>';
      data.months.forEach(function(m) {
        var count = data.data[m].length;
        sel.innerHTML += '<option value="' + m + '">' + m + ' (' + count + ')</option>';
      });

      // Populate vendor dropdown (sorted unique vendors)
      var vendors = {};
      _invBrowseAllRows.forEach(function(r) { vendors[r.vendor_name] = (vendors[r.vendor_name] || 0) + 1; });
      var vendorList = Object.keys(vendors).sort(function(a, b) { return a.toLowerCase().localeCompare(b.toLowerCase()); });
      var vsel = document.getElementById('invBrowseVendorSelect');
      vsel.innerHTML = '<option value="all">All Vendors (' + vendorList.length + ')</option>';
      vendorList.forEach(function(v) {
        vsel.innerHTML += '<option value="' + escHtml(v) + '">' + escHtml(v) + ' (' + vendors[v] + ')</option>';
      });

      document.getElementById('invBrowseFilterBar').style.display = 'flex';
      applyInvBrowseFilters();
    })
    .catch(function(err) {
      document.getElementById('invBrowseLoading').textContent = 'Failed to load: ' + err;
    });
}

function closeInvoiceBrowse() {
  document.getElementById('invoiceBrowsePanel').style.display = 'none';
  document.getElementById('logPanel').style.display = 'flex';
}

function clearInvBrowseFilters() {
  document.getElementById('invBrowseMonthSelect').value = 'all';
  document.getElementById('invBrowseVendorSelect').value = 'all';
  document.getElementById('invBrowseDateFrom').value = '';
  document.getElementById('invBrowseDateTo').value = '';
  applyInvBrowseFilters();
}

function applyInvBrowseFilters() {
  if (!_invBrowseData) return;

  var month = document.getElementById('invBrowseMonthSelect').value;
  var vendor = document.getElementById('invBrowseVendorSelect').value;
  var dateFrom = document.getElementById('invBrowseDateFrom').value;
  var dateTo = document.getElementById('invBrowseDateTo').value;

  // Start with month filter
  var rows;
  if (month === 'all') {
    rows = _invBrowseAllRows.slice();
  } else {
    rows = (_invBrowseData.data[month] || []).slice();
  }

  // Vendor filter
  if (vendor !== 'all') {
    rows = rows.filter(function(r) { return r.vendor_name === vendor; });
  }

  // Date range filter
  if (dateFrom) {
    rows = rows.filter(function(r) { return (r.date || '') >= dateFrom; });
  }
  if (dateTo) {
    rows = rows.filter(function(r) { return (r.date || '') <= dateTo; });
  }

  renderInvBrowseRows(rows);
}

function renderInvBrowseRows(rows) {
  // Sort by date
  rows.sort(function(a, b) { return (a.date || '').localeCompare(b.date || ''); });

  var tbody = document.getElementById('invBrowseBody');
  tbody.innerHTML = '';

  var inrTotal = 0;
  var usdTotal = 0;

  rows.forEach(function(inv) {
    var tr = document.createElement('tr');
    var amt = inv.amount != null ? inv.amount : 0;
    if (inv.currency === 'USD') usdTotal += amt;
    else inrTotal += amt;

    var fmtAmt = inv.amount != null
      ? (inv.currency === 'USD' ? '$' : '\u20B9') + Number(inv.amount).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})
      : '-';

    var lineDesc = inv.line_items_desc || '<span style="color:var(--text-dim);font-style:italic">no line items</span>';
    if (inv.line_items_count > 0) {
      lineDesc = '<span style="color:var(--accent);font-weight:600;margin-right:4px">' + inv.line_items_count + '&times;</span>' + escHtml(inv.line_items_desc);
    }

    tr.innerHTML = '<td>' + (inv.date || '-') + '</td>'
      + '<td>' + escHtml(inv.vendor_name) + '</td>'
      + '<td style="font-family:monospace;font-size:11px">' + escHtml(inv.invoice_number) + '</td>'
      + '<td style="text-align:right;font-weight:600;white-space:nowrap">' + fmtAmt + '</td>'
      + '<td style="font-size:11px;color:var(--text-dim);max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + lineDesc + '</td>';
    tbody.appendChild(tr);
  });

  // Summary
  var parts = [rows.length + ' invoices'];
  if (inrTotal > 0) parts.push('\u20B9' + inrTotal.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
  if (usdTotal > 0) parts.push('$' + usdTotal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
  document.getElementById('invBrowseSummary').textContent = parts.join(' \u2022 ');

  document.getElementById('invBrowseLoading').style.display = 'none';
  document.getElementById('invBrowseContent').style.display = 'block';
}

function escHtml(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function parseAllForCompare(step, btnId, label) {
  var btn = document.getElementById(btnId);
  btn.disabled = true;
  btn.textContent = 'Parsing ' + label + '...';
  fetch('/api/run/' + step, { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      btn.textContent = 'Done! Refreshing...';
      // Refresh compare data
      return fetch('/api/compare/monthly').then(function(r) { return r.json(); });
    })
    .then(function(data) {
      if (data && !data.error) {
        _compareData = data;
        renderComparePanel(data);
      }
      btn.disabled = false;
      btn.textContent = label === 'CC' ? 'Parse All CC' : 'Parse All Invoices';
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = label === 'CC' ? 'Parse All CC' : 'Parse All Invoices';
      alert('Parse failed: ' + err);
    });
}

function parseOrgInvoices() {
  var btn = document.getElementById('parseAllBillsBtn');
  btn.disabled = true;
  btn.textContent = 'Parsing Invoices...';
  fetch('/api/compare/parse-org-invoices', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); btn.disabled = false; btn.textContent = 'Parse All Invoices'; return; }
      // Poll until done
      var poll = setInterval(function() {
        fetch('/api/status').then(function(r) { return r.json(); }).then(function(st) {
          if (!st.running) {
            clearInterval(poll);
            btn.textContent = 'Done! Refreshing...';
            fetch('/api/compare/monthly').then(function(r) { return r.json(); }).then(function(d) {
              if (d && !d.error) { _compareData = d; renderComparePanel(d); }
              btn.disabled = false;
              btn.textContent = 'Parse All Invoices';
            });
          }
        });
      }, 2000);
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = 'Parse All Invoices';
      alert('Parse failed: ' + err);
    });
}

function renderComparePanel(data) {
  var months = data.months || [];
  var summary = data.summary || {};

  document.getElementById('compareSummaryText').textContent =
    (summary.total_months || 0) + ' months · ' +
    (summary.total_cc || 0) + ' CC txns · ' +
    (summary.total_invoices || 0) + ' invoices';

  // Populate month dropdown
  var select = document.getElementById('compareMonthSelect');
  select.innerHTML = '';
  // "All Months" option
  var allOpt = document.createElement('option');
  allOpt.value = 'all';
  var totalCC = months.reduce(function(s, m) { return s + (m.cc_count || 0); }, 0);
  var totalInv = months.reduce(function(s, m) { return s + (m.inv_count || 0); }, 0);
  allOpt.textContent = 'All Months (' + totalCC + ' CC / ' + totalInv + ' inv)';
  select.appendChild(allOpt);
  months.forEach(function(m, idx) {
    var opt = document.createElement('option');
    opt.value = idx;
    opt.textContent = m.month + ' (' + m.cc_count + ' CC / ' + m.inv_count + ' inv)';
    select.appendChild(opt);
  });

  document.getElementById('compareLoading').style.display = 'none';
  document.getElementById('compareContent').style.display = 'block';

  if (months.length > 0) {
    renderCompareMonth('all');
  } else {
    document.getElementById('compareContent').innerHTML =
      '<div style="text-align:center;color:var(--text-dim);padding:40px">No data found</div>';
  }
}

function renderCompareMonth(idx) {
  var m;
  if (idx === 'all' || idx === 'NaN') {
    // Merge all months
    var allCC = [], allInv = [];
    var ccTot = 0, invTot = 0;
    _compareData.months.forEach(function(mo) {
      allCC = allCC.concat(mo.cc_transactions || []);
      allInv = allInv.concat(mo.invoices || []);
      ccTot += (mo.cc_total || 0);
      invTot += (mo.inv_total || 0);
    });
    m = {
      month: 'All Months',
      cc_transactions: allCC, invoices: allInv,
      cc_count: allCC.length, inv_count: allInv.length,
      cc_total: ccTot, inv_total: invTot
    };
  } else {
    idx = parseInt(idx);
    m = _compareData.months[idx];
  }
  var content = document.getElementById('compareContent');
  content.innerHTML = '';
  var fmt = function(n) { return n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };
  var fmtDate = function(d) { if (!d || d.length < 10) return d || '-'; var p = d.split('-'); return p[2] + '-' + p[1] + '-' + p[0]; };

  // Month summary bar
  var summaryBar = document.createElement('div');
  summaryBar.style.cssText = 'display:flex;gap:24px;padding:8px 12px;font-size:12px;border-bottom:1px solid var(--border);flex-shrink:0;background:rgba(255,255,255,0.03)';
  summaryBar.innerHTML =
    '<span><strong>' + m.cc_count + '</strong> CC transactions &middot; Total: <strong style="font-family:monospace">' + fmt(m.cc_total) + '</strong> INR</span>' +
    '<span style="margin-left:auto"><strong>' + m.inv_count + '</strong> invoices &middot; Total: <strong style="font-family:monospace">' + fmt(m.inv_total) + '</strong></span>';
  content.appendChild(summaryBar);

  // Sticky two-column header
  var colHeader = document.createElement('div');
  colHeader.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;position:sticky;top:0;z-index:2;background:var(--bg-panel);border-bottom:2px solid var(--border);flex-shrink:0';
  colHeader.innerHTML =
    '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--yellow)">CC Statements (' + m.cc_count + ')</div>' +
    '<div style="padding:8px 12px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.6px;color:var(--accent);border-left:1px solid var(--border)">Invoices (' + m.inv_count + ')</div>';
  content.appendChild(colHeader);

  // Two-column body
  var cols = document.createElement('div');
  cols.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;flex:1;min-height:0';

  // --- CC column ---
  var ccCol = document.createElement('div');
  ccCol.style.cssText = 'border-right:1px solid var(--border);min-width:0;overflow-y:auto';
  if (!m.cc_transactions.length) {
    ccCol.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-dim)">No CC transactions this month</div>';
  } else {
    var tbl = document.createElement('table');
    tbl.className = 'match-table';
    tbl.style.width = '100%';
    tbl.innerHTML = '<thead><tr><th>Date</th><th>Description</th><th>Amount (INR)</th><th>Forex</th><th>Card</th></tr></thead>';
    var tbody = document.createElement('tbody');
    m.cc_transactions.forEach(function(t) {
      var tr = document.createElement('tr');
      tr.setAttribute('data-vendor', (t.vendor_name || t.description || '').toLowerCase());
      tr.setAttribute('data-date', t.date || '');
      var forexText = t.forex_currency && t.forex_amount
        ? t.forex_currency + ' ' + fmt(t.forex_amount) : '-';
      var cardShort = (t.card_name || '-').replace('CC ', '');
      tr.innerHTML =
        '<td style="white-space:nowrap;font-size:11px">' + fmtDate(t.date) + '</td>' +
        '<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px" title="' + (t.description || '') + '">' + (t.description || '-') + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + fmt(t.amount) + '</td>' +
        '<td style="font-size:10px;color:var(--yellow)">' + forexText + '</td>' +
        '<td style="font-size:10px;color:var(--text-dim)">' + cardShort + '</td>';
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    ccCol.appendChild(tbl);
  }

  // --- Invoice column ---
  var invCol = document.createElement('div');
  invCol.style.cssText = 'min-width:0;overflow-y:auto';
  if (!m.invoices.length) {
    invCol.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-dim)">No invoices this month</div>';
  } else {
    var tbl2 = document.createElement('table');
    tbl2.className = 'match-table';
    tbl2.style.width = '100%';
    tbl2.innerHTML = '<thead><tr><th>Vendor</th><th>GSTIN</th><th>Amount</th><th>Date</th><th>Invoice #</th></tr></thead>';
    var tbody2 = document.createElement('tbody');
    m.invoices.forEach(function(inv) {
      var tr = document.createElement('tr');
      tr.setAttribute('data-vendor', (inv.vendor_name || '').toLowerCase());
      tr.setAttribute('data-date', inv.date || '');
      var gstinHtml = inv.vendor_gstin
        ? '<span title="' + inv.vendor_gstin + '">' + inv.vendor_gstin + '</span>'
        : '<span style="color:var(--text-dim)">-</span>';
      tr.innerHTML =
        '<td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="' + (inv.vendor_name || '') + '">' + (inv.vendor_name || '-') + '</td>' +
        '<td style="font-size:10px;font-family:monospace">' + gstinHtml + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + fmt(inv.amount) + ' <span style="font-size:9px;color:var(--text-dim)">' + (inv.currency || 'INR') + '</span></td>' +
        '<td style="white-space:nowrap;font-size:11px">' + fmtDate(inv.date) + '</td>' +
        '<td style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (inv.invoice_number || '') + '">' + (inv.invoice_number || '-') + '</td>';
      tbody2.appendChild(tr);
    });
    tbl2.appendChild(tbody2);
    invCol.appendChild(tbl2);
  }

  cols.appendChild(ccCol);
  cols.appendChild(invCol);
  content.appendChild(cols);

  // --- Categorize Check button ---
  var catBar = document.createElement('div');
  catBar.style.cssText = 'padding:10px 12px;border-top:1px solid var(--border);text-align:center;flex-shrink:0';

  var catBtn = document.createElement('button');
  catBtn.textContent = 'Categorize Check';
  catBtn.style.cssText = 'background:transparent;color:var(--accent);border:1px dashed var(--accent);padding:6px 18px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600';
  catBtn.onclick = function() { runCategorizeCheck(idx, 'cc'); };
  catBar.appendChild(catBtn);

  content.appendChild(catBar);

  // Container for categorize results
  var catResults = document.createElement('div');
  catResults.id = 'categorizeResults';
  content.appendChild(catResults);

  // Populate vendor filter dropdown from Zoho vendors
  var zohoVendors = (_compareData && _compareData.zoho_vendors) || [];
  var vSelect = document.getElementById('compareVendorFilter');
  vSelect.innerHTML = '<option value="">All Vendors</option>';
  zohoVendors.forEach(function(v) {
    var opt = document.createElement('option');
    opt.value = v.toLowerCase();
    opt.textContent = v.length > 30 ? v.substring(0, 28) + '...' : v;
    opt.title = v;
    vSelect.appendChild(opt);
  });

  // Set date range defaults from month data
  var dateFrom = document.getElementById('compareDateFrom');
  var dateTo = document.getElementById('compareDateTo');
  dateFrom.value = '';
  dateTo.value = '';

  // Show filter bar
  document.getElementById('compareFilterBar').style.display = 'flex';
}

function _vendorMatch(rowVendor, filterVal) {
  // Bidirectional: "github" matches "github, inc." and vice versa
  if (!rowVendor) return false;
  return rowVendor.indexOf(filterVal) !== -1 || filterVal.indexOf(rowVendor) !== -1;
}

function applyCompareFilters() {
  var vendorVal = (document.getElementById('compareVendorFilter').value || '').toLowerCase();
  var dateFrom = document.getElementById('compareDateFrom').value || '';
  var dateTo = document.getElementById('compareDateTo').value || '';

  function _filterRows(tbody) {
    if (!tbody) return;
    Array.prototype.forEach.call(tbody.rows, function(tr) {
      var rv = tr.getAttribute('data-vendor') || '';
      var rd = tr.getAttribute('data-date') || '';
      var show = true;
      if (vendorVal && !_vendorMatch(rv, vendorVal)) show = false;
      if (dateFrom && rd && rd < dateFrom) show = false;
      if (dateTo && rd && rd > dateTo) show = false;
      tr.style.display = show ? '' : 'none';
    });
  }

  var tables = document.querySelectorAll('#compareContent table.match-table');
  if (tables.length >= 1) _filterRows(tables[0].querySelector('tbody'));
  if (tables.length >= 2) _filterRows(tables[1].querySelector('tbody'));

  // Also filter Categorize Check rows (vendor + date + status)
  var catStatusVal = '';
  var catStatusEl = document.getElementById('catStatusFilter');
  if (catStatusEl) catStatusVal = catStatusEl.value || '';
  var catRows = document.querySelectorAll('#categorizeResults .cat-row');
  Array.prototype.forEach.call(catRows, function(tr) {
    var rv = tr.getAttribute('data-vendor') || '';
    var rd = tr.getAttribute('data-date') || '';
    var rs = tr.getAttribute('data-status') || '';
    var show = true;
    if (vendorVal && !_vendorMatch(rv, vendorVal)) show = false;
    if (dateFrom && rd && rd < dateFrom) show = false;
    if (dateTo && rd && rd > dateTo) show = false;
    if (catStatusVal && rs !== catStatusVal) show = false;
    tr.style.display = show ? '' : 'none';
  });
}

function applyCatStatusFilter() {
  applyCompareFilters();
}

function clearCompareFilters() {
  document.getElementById('compareVendorFilter').value = '';
  document.getElementById('compareDateFrom').value = '';
  document.getElementById('compareDateTo').value = '';
  var catStatusEl = document.getElementById('catStatusFilter');
  if (catStatusEl) catStatusEl.value = '';
  applyCompareFilters();
}

var _catRows = [];
var _catMonth = '';

function runCategorizeCheck(idx, mode) {
  var m;
  if (idx === 'all' || isNaN(parseInt(idx))) {
    var allCC = [], allInv = [];
    _compareData.months.forEach(function(mo) {
      allCC = allCC.concat(mo.cc_transactions || []);
      allInv = allInv.concat(mo.invoices || []);
    });
    m = { month: 'All Months', cc_transactions: allCC, invoices: allInv };
  } else {
    idx = parseInt(idx);
    m = _compareData.months[idx];
  }
  var ccList = (m.cc_transactions || []).slice();
  var invList = (m.invoices || []).slice();

  // Apply active vendor/date filters
  var vendorVal = (document.getElementById('compareVendorFilter').value || '').toLowerCase();
  var dateFrom = document.getElementById('compareDateFrom').value || '';
  var dateTo = document.getElementById('compareDateTo').value || '';
  if (vendorVal || dateFrom || dateTo) {
    ccList = ccList.filter(function(t) {
      var v = (t.vendor_name || t.description || '').toLowerCase();
      var d = t.date || '';
      if (vendorVal && v.indexOf(vendorVal) === -1) return false;
      if (dateFrom && d && d < dateFrom) return false;
      if (dateTo && d && d > dateTo) return false;
      return true;
    });
    invList = invList.filter(function(inv) {
      var v = (inv.vendor_name || '').toLowerCase();
      var d = inv.date || '';
      if (vendorVal && v.indexOf(vendorVal) === -1) return false;
      if (dateFrom && d && d < dateFrom) return false;
      if (dateTo && d && d > dateTo) return false;
      return true;
    });
  }

  var container = document.getElementById('categorizeResults');
  container.innerHTML = '';
  var fmt = function(n) { return n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // Group CC by vendor
  var ccByVendor = {};
  var unmappedCC = [];
  ccList.forEach(function(t) {
    var v = t.vendor_name;
    if (!v) { unmappedCC.push(t); return; }
    if (!ccByVendor[v]) ccByVendor[v] = [];
    ccByVendor[v].push(Object.assign({_matched: false}, t));
  });

  // Group invoices by vendor
  var invByVendor = {};
  invList.forEach(function(inv) {
    var v = inv.vendor_name || '';
    if (!v) return;
    if (!invByVendor[v]) invByVendor[v] = [];
    invByVendor[v].push(Object.assign({_matched: false}, inv));
  });

  // Merge vendor groups where one name is a substring of another
  // e.g., "Fly.io" and "Fly.io, Inc" should be the same vendor
  var ccVendors = Object.keys(ccByVendor);
  var invVendors = Object.keys(invByVendor);
  ccVendors.forEach(function(cv) {
    var cvl = cv.toLowerCase();
    invVendors.forEach(function(iv) {
      if (cv === iv) return;
      var ivl = iv.toLowerCase();
      // Bidirectional substring match (at least 4 chars overlap)
      if ((cvl.length >= 4 && ivl.indexOf(cvl) !== -1) || (ivl.length >= 4 && cvl.indexOf(ivl) !== -1)) {
        // Merge invoice group into cc vendor name (cc has resolved names)
        if (!ccByVendor[cv]) ccByVendor[cv] = [];
        ccByVendor[cv] = ccByVendor[cv].concat(invByVendor[iv] || []);
        // Also keep under inv vendor name for display, but mark as merged
        if (!invByVendor[cv]) invByVendor[cv] = [];
        invByVendor[cv] = invByVendor[cv].concat(invByVendor[iv] || []);
        delete invByVendor[iv];
      }
    });
  });

  // All vendors from both sides
  var allVendors = {};
  Object.keys(ccByVendor).forEach(function(v) { allVendors[v] = true; });
  Object.keys(invByVendor).forEach(function(v) { allVendors[v] = true; });
  var vendorList = Object.keys(allVendors).sort();

  var rows = []; // {vendor, cc, inv, matchType, status}

  vendorList.forEach(function(vendor) {
    var ccs = (ccByVendor[vendor] || []).slice();
    var invs = (invByVendor[vendor] || []).slice();

    // Sort both by date so greedy matching pairs chronologically
    ccs.sort(function(a, b) { return (a.date || '').localeCompare(b.date || ''); });
    invs.sort(function(a, b) { return (a.date || '').localeCompare(b.date || ''); });

    // Try to match each CC to an invoice (prefer closest date when amounts tie)
    ccs.forEach(function(cc) {
      var bestMatch = null;
      var bestType = '';
      var bestDiff = Infinity;
      var bestDateDiff = Infinity;
      var _ccDate = cc.date ? new Date(cc.date) : null;
      var ccForex = cc.forex_amount ? parseFloat(cc.forex_amount) : null;
      var ccCur = cc.forex_currency || null;
      var ccInr = parseFloat(cc.amount) || 0;

      invs.forEach(function(inv) {
        if (inv._matched) return;
        var invAmt = parseFloat(inv.amount) || 0;
        var invCur = (inv.currency || 'INR').toUpperCase();

        var diff = null;
        var mtype = '';

        // Case 1: USD -> USD (or same forex currency)
        if (ccCur && ccForex && invCur === ccCur) {
          diff = Math.abs(ccForex - invAmt);
          mtype = ccCur + ' \u2192 ' + invCur;
        }
        // Case 2: Forex -> INR (CC has forex, invoice is INR)
        else if (ccCur && ccForex && invCur === 'INR') {
          diff = Math.abs(ccInr - invAmt);
          mtype = ccCur + ' \u2192 INR (forex)';
        }
        // Case 3: INR -> INR (no forex)
        else if (!ccCur && invCur === 'INR') {
          diff = Math.abs(ccInr - invAmt);
          mtype = 'INR \u2192 INR';
        }
        // Case 4: Any currency -> INR with forex
        else if (ccCur && ccForex && invCur !== ccCur && invCur !== 'INR') {
          // Cross-currency — can't reliably match
          return;
        }
        // Fallback: just compare INR amounts
        else {
          diff = Math.abs(ccInr - invAmt);
          mtype = 'INR \u2192 ' + invCur;
        }

        // Date proximity as tiebreaker when amounts are equal
        var dateDiff = 9999;
        if (_ccDate && inv.date) {
          var invD = new Date(inv.date);
          dateDiff = Math.abs((_ccDate - invD) / 86400000);
        }

        if (diff !== null && (diff < bestDiff || (diff === bestDiff && dateDiff < bestDateDiff))) {
          bestDiff = diff;
          bestDateDiff = dateDiff;
          bestMatch = inv;
          bestType = mtype;
        }
      });

      // Tolerance: exact or within 1% of compared amount, AND date within ±10 days
      var _compAmt = (ccForex && ccCur) ? ccForex : ccInr;
      var threshold = Math.max(_compAmt < 100 ? 0.5 : 1, _compAmt * 0.01);
      if (bestMatch && bestDiff <= threshold && bestDateDiff <= 10) {
        bestMatch._matched = true;
        cc._matched = true;
        rows.push({
          vendor: vendor,
          cc: cc,
          inv: bestMatch,
          matchType: bestType,
          diff: bestDiff,
          status: bestDiff < 0.01 ? 'exact' : 'close'
        });
      } else {
        rows.push({
          vendor: vendor,
          cc: cc,
          inv: null,
          matchType: '',
          diff: null,
          status: 'no_invoice'
        });
      }
    });

    // Unmatched invoices
    invs.forEach(function(inv) {
      if (inv._matched) return;
      rows.push({
        vendor: vendor,
        cc: null,
        inv: inv,
        matchType: '',
        diff: null,
        status: 'no_cc'
      });
    });
  });

  // Unmapped CC (no vendor resolved)
  unmappedCC.forEach(function(cc) {
    rows.push({
      vendor: '(unmapped)',
      cc: cc,
      inv: null,
      matchType: '',
      diff: null,
      status: 'unmapped'
    });
  });

  // --- Multi-invoice sum matching (GSTIN-grouped) --- runs BEFORE vendor-agnostic 1:1
  // Logic: 1) Date: find invoices within ±5 days of CC
  //        2) GSTIN: group by same GSTIN (same supplier — vendor name may differ)
  //        3) Sum: try full GSTIN group first, then combinations within group
  //        No mixed-GSTIN fallback — GSTIN is the identity
  var _pd = function(s) { if (!s) return null; var d = new Date(s); return isNaN(d.getTime()) ? null : d; };
  var _daysDiff = function(a, b) { if (!a || !b) return 9999; return Math.abs((a - b) / 86400000); };

  (function() {
    var sumUnmatchedCC = [];
    var sumUnmatchedInv = [];
    rows.forEach(function(r, ri) {
      if ((r.status === 'no_invoice' || r.status === 'unmapped') && r.cc) sumUnmatchedCC.push(ri);
      if (r.status === 'no_cc' && r.inv) sumUnmatchedInv.push(ri);
    });

    var sumInvUsed = {};

    function sumCombinations(arr, minSize, maxSize) {
      var results = [];
      function sc(start, cur) {
        if (cur.length >= minSize) results.push(cur.slice());
        if (cur.length >= maxSize) return;
        for (var i = start; i < arr.length; i++) {
          cur.push(arr[i]);
          sc(i + 1, cur);
          cur.pop();
        }
      }
      sc(0, []);
      return results;
    }

    sumUnmatchedCC.forEach(function(ccIdx) {
      var r = rows[ccIdx];
      if (!r || (r.status !== 'no_invoice' && r.status !== 'unmapped')) return;
      var cc5 = r.cc;
      var ccInr5 = parseFloat(cc5.amount) || 0;
      var ccForex5 = cc5.forex_amount ? parseFloat(cc5.forex_amount) : null;
      var ccCur5 = cc5.forex_currency || null;
      var ccDate5 = _pd(cc5.date);

      // Step 1: Find nearby unmatched invoices (±5 days)
      var nearby5 = [];
      sumUnmatchedInv.forEach(function(invIdx) {
        if (sumInvUsed[invIdx]) return;
        var inv5 = rows[invIdx].inv;
        if (!inv5) return;
        var invDate5 = _pd(inv5.date);
        if (_daysDiff(ccDate5, invDate5) > 5) return;

        var invCur5 = (inv5.currency || 'INR').toUpperCase();
        var compatible = false;
        if (ccCur5 && ccForex5 && invCur5 === ccCur5) compatible = true;
        else if (!ccCur5 && invCur5 === 'INR') compatible = true;
        else if (ccCur5 && invCur5 === 'INR') compatible = true;
        if (!compatible) return;

        nearby5.push({invIdx: invIdx, inv: inv5});
      });

      if (nearby5.length < 2) return;

      // Step 2: Group by GSTIN (invoices without GSTIN go into '_NO_GSTIN_' group)
      var byGstin = {};
      nearby5.forEach(function(item) {
        var gstin = item.inv.vendor_gstin || '_NO_GSTIN_';
        if (!byGstin[gstin]) byGstin[gstin] = [];
        byGstin[gstin].push(item);
      });

      // Determine target amount
      var targetAmt5 = ccForex5 && ccCur5 ? ccForex5 : ccInr5;
      var useInr = !ccCur5 || (ccCur5 && nearby5[0] && (nearby5[0].inv.currency || 'INR').toUpperCase() === 'INR');
      if (useInr) targetAmt5 = ccInr5;

      var found5 = null;
      var thresh5 = Math.max(1, targetAmt5 * 0.005);

      // Step 3: Try full GSTIN group sum first, then combinations
      Object.keys(byGstin).forEach(function(gstin) {
        if (found5) return;
        var group = byGstin[gstin];
        if (group.length < 2) return;

        var fullSum = group.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
        if (Math.abs(fullSum - targetAmt5) <= thresh5) {
          found5 = group;
          return;
        }

        var combos = sumCombinations(group, 2, Math.min(6, group.length));
        for (var ci = 0; ci < combos.length; ci++) {
          var combo = combos[ci];
          var sum = combo.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
          if (Math.abs(sum - targetAmt5) <= thresh5) {
            found5 = combo;
            break;
          }
        }
      });

      // Step 4: If no GSTIN-based match, try ALL nearby invoices cross-GSTIN (date+amount only)
      if (!found5 && nearby5.length >= 2) {
        var fullSum5 = nearby5.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
        if (Math.abs(fullSum5 - targetAmt5) <= thresh5) {
          found5 = nearby5;
        } else if (nearby5.length <= 15) {
          var combos5 = sumCombinations(nearby5, 2, Math.min(10, nearby5.length));
          for (var ci5 = 0; ci5 < combos5.length; ci5++) {
            var combo5 = combos5[ci5];
            var sum5 = combo5.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
            if (Math.abs(sum5 - targetAmt5) <= thresh5) {
              found5 = combo5;
              break;
            }
          }
        }
      }

      if (found5) {
        found5.forEach(function(item) {
          sumInvUsed[item.invIdx] = true;
          if (rows[item.invIdx]) rows[item.invIdx] = null;
        });

        var totalSum = found5.reduce(function(s, item) { return s + (parseFloat(item.inv.amount) || 0); }, 0);
        var totalDiff5 = Math.abs(totalSum - targetAmt5);
        var invNumbers = found5.map(function(item) { return item.inv.invoice_number || ''; }).filter(Boolean).join(' + ');
        var gstin5 = found5[0].inv.vendor_gstin || '';
        var uniqueGstins5 = {};
        found5.forEach(function(item) { if (item.inv.vendor_gstin) uniqueGstins5[item.inv.vendor_gstin] = true; });
        var gstinCount5 = Object.keys(uniqueGstins5).length;
        var mtype5 = (ccCur5 || 'INR') + ' \u2192 ' + (found5[0].inv.currency || 'INR') + (gstinCount5 === 1 && gstin5 ? ' (GSTIN:' + gstin5.substring(0, 10) + '.. x' + found5.length + ')' : ' (Date+Amt x' + found5.length + ')');

        var anyInZoho = found5.some(function(item) { return item.inv.in_zoho; });
        var zohoBillIds = [];
        var zohoBillId = '';
        var zohoBillStatus = '';
        found5.forEach(function(item) {
          if (item.inv.zoho_bill_id && zohoBillIds.indexOf(item.inv.zoho_bill_id) === -1) {
            zohoBillIds.push(item.inv.zoho_bill_id);
          }
          if (!zohoBillId && item.inv.zoho_bill_id) {
            zohoBillId = item.inv.zoho_bill_id;
            zohoBillStatus = item.inv.zoho_bill_status || '';
          }
        });
        rows[ccIdx] = {
          vendor: found5[0].inv.vendor_name || r.vendor,
          cc: cc5,
          inv: {
            vendor_name: found5[0].inv.vendor_name || r.vendor,
            amount: totalSum,
            currency: found5[0].inv.currency || 'INR',
            date: found5[0].inv.date,
            invoice_number: invNumbers,
            vendor_gstin: gstin5,
            in_zoho: anyInZoho,
            zoho_bill_id: zohoBillId,
            zoho_bill_ids: zohoBillIds,
            zoho_bill_status: zohoBillStatus,
            _grouped_invoices: found5.map(function(item) { return item.inv; })
          },
          matchType: mtype5,
          diff: totalDiff5,
          status: totalDiff5 < 0.01 ? 'exact' : 'close'
        };
      }
    });

    // Remove nulled rows from sum matching
    for (var ri3 = rows.length - 1; ri3 >= 0; ri3--) {
      if (rows[ri3] === null) rows.splice(ri3, 1);
    }
  })();

  // --- Amount+date matching for unmatched items (vendor-agnostic) ---
  // If vendor names differ but amount/forex match and dates are close, treat as match

  var unmatchedCCIdxs = [];
  var unmatchedInvIdxs = [];
  rows.forEach(function(r, ri) {
    if ((r.status === 'no_invoice' || r.status === 'unmapped') && r.cc) unmatchedCCIdxs.push(ri);
    if (r.status === 'no_cc' && r.inv) unmatchedInvIdxs.push(ri);
  });

  var _invUsed = {};
  unmatchedCCIdxs.forEach(function(ccIdx) {
    var r = rows[ccIdx];
    var cc2 = r.cc;
    var ccInr2 = parseFloat(cc2.amount) || 0;
    var ccForex2 = cc2.forex_amount ? parseFloat(cc2.forex_amount) : null;
    var ccCur2 = cc2.forex_currency || null;
    var ccDate2 = _pd(cc2.date);
    var best2 = null, bestDiff2 = Infinity, bestInvIdx2 = -1, bestMtype2 = '';

    unmatchedInvIdxs.forEach(function(invIdx) {
      if (_invUsed[invIdx]) return;
      var inv2 = rows[invIdx].inv;
      var invAmt2 = parseFloat(inv2.amount) || 0;
      var invCur2 = (inv2.currency || 'INR').toUpperCase();
      var invDate2 = _pd(inv2.date);
      if (_daysDiff(ccDate2, invDate2) > 30) return;

      var diff2 = null, mtype2 = '', compAmt2 = ccInr2;
      if (ccCur2 && ccForex2 && invCur2 === ccCur2) {
        diff2 = Math.abs(ccForex2 - invAmt2); mtype2 = ccCur2 + ' \u2192 ' + invCur2;
        compAmt2 = ccForex2;
      } else if (ccCur2 && ccForex2 && invCur2 === 'INR') {
        diff2 = Math.abs(ccInr2 - invAmt2); mtype2 = ccCur2 + ' \u2192 INR (forex)';
      } else if (!ccCur2 && invCur2 === 'INR') {
        diff2 = Math.abs(ccInr2 - invAmt2); mtype2 = 'INR \u2192 INR';
      } else {
        // Cross-currency mismatch (e.g. INR CC vs USD invoice) — skip
        return;
      }

      var threshold2 = Math.max(compAmt2 < 100 ? 0.5 : 1, compAmt2 * 0.01);
      if (diff2 !== null && diff2 <= threshold2 && diff2 < bestDiff2) {
        bestDiff2 = diff2; best2 = inv2; bestInvIdx2 = invIdx; bestMtype2 = mtype2;
      }
    });

    if (best2 && bestInvIdx2 >= 0) {
      _invUsed[bestInvIdx2] = true;
      rows[ccIdx] = {
        vendor: (best2.vendor_name || r.vendor),
        cc: cc2, inv: best2, matchType: bestMtype2, diff: bestDiff2,
        status: bestDiff2 < 0.01 ? 'exact' : 'close'
      };
      rows[bestInvIdx2] = null; // mark for removal
    }
  });

  // Remove nulled rows
  for (var _ri = rows.length - 1; _ri >= 0; _ri--) {
    if (rows[_ri] === null) rows.splice(_ri, 1);
  }

  // --- Cross-month search for unmatched CC (limited to +/- 1 month) --- skip when viewing all months
  if (idx === 'all' || isNaN(parseInt(idx))) { /* skip cross-month for all-months view */ } else {
  var _monthNames = {Jan:0, Feb:1, Mar:2, Apr:3, May:4, Jun:5, Jul:6, Aug:7, Sep:8, Oct:9, Nov:10, Dec:11};
  var _parseMonthDate = function(mk) {
    var parts = (mk || '').split(' ');
    if (parts.length !== 2) return null;
    var mon = _monthNames[parts[0]];
    var yr = parseInt(parts[1]);
    if (mon == null || isNaN(yr)) return null;
    return new Date(yr, mon, 15);
  };
  var _curMonthDate = _parseMonthDate(m.month);

  var otherMonthInvs = [];
  _compareData.months.forEach(function(om, omIdx) {
    if (omIdx === idx) return;
    // Only include months within ~45 days (roughly 1 month gap)
    var omDate = _parseMonthDate(om.month);
    if (_curMonthDate && omDate && Math.abs(_curMonthDate - omDate) > 45 * 86400000) return;
    (om.invoices || []).forEach(function(inv) {
      otherMonthInvs.push(Object.assign({_src_month: om.month}, inv));
    });
  });

  rows.forEach(function(r, ri) {
    if (r.status !== 'no_invoice' && r.status !== 'unmapped') return;
    if (!r.cc) return;
    var cc3 = r.cc;
    var vendor3 = r.vendor;
    var ccInr3 = parseFloat(cc3.amount) || 0;
    var ccForex3 = cc3.forex_amount ? parseFloat(cc3.forex_amount) : null;
    var ccCur3 = cc3.forex_currency || null;
    var ccDate3 = _pd(cc3.date);
    var bestMatch3 = null;
    var bestType3 = '';
    var bestDiff3 = Infinity;
    var bestMonth3 = '';

    otherMonthInvs.forEach(function(inv) {
      var vendorMatch3 = (inv.vendor_name || '') === vendor3;
      var invAmt3 = parseFloat(inv.amount) || 0;
      var invCur3 = (inv.currency || 'INR').toUpperCase();
      var invDate3 = _pd(inv.date);

      // For non-vendor matches, also require date proximity (45 days)
      if (!vendorMatch3 && _daysDiff(ccDate3, invDate3) > 45) return;

      var diff3 = null;
      var mtype3 = '';
      if (ccCur3 && ccForex3 && invCur3 === ccCur3) {
        diff3 = Math.abs(ccForex3 - invAmt3);
        mtype3 = ccCur3 + ' \u2192 ' + invCur3;
      } else if (ccCur3 && ccForex3 && invCur3 === 'INR') {
        diff3 = Math.abs(ccInr3 - invAmt3);
        mtype3 = ccCur3 + ' \u2192 INR (forex)';
      } else if (!ccCur3 && invCur3 === 'INR') {
        diff3 = Math.abs(ccInr3 - invAmt3);
        mtype3 = 'INR \u2192 INR';
      } else {
        // Cross-currency mismatch — skip
        return;
      }

      var _compAmt3 = (ccCur3 && ccForex3 && invCur3 === ccCur3) ? ccForex3 : ccInr3;
      var _thresh3 = Math.max(_compAmt3 < 100 ? 0.5 : 1, _compAmt3 * 0.01);
      if (diff3 !== null && diff3 <= _thresh3 && diff3 < bestDiff3) {
        bestDiff3 = diff3;
        bestMatch3 = inv;
        bestType3 = mtype3;
        bestMonth3 = inv._src_month;
      }
    });

    if (bestMatch3 && bestDiff3 < Infinity) {
      rows[ri] = {
        vendor: (bestMatch3.vendor_name || vendor3),
        cc: cc3,
        inv: bestMatch3,
        matchType: bestType3 + ' [' + bestMonth3 + ']',
        diff: bestDiff3,
        status: bestDiff3 < 0.01 ? 'cross_exact' : 'cross_close'
      };
    }
  });
  } // end cross-month else block

  // Sort rows: Create Bill first, then In Zoho, then rest
  var statusOrder = {exact: 0, close: 0, cross_exact: 1, cross_close: 1, no_cc: 2, no_invoice: 3, unmapped: 4};
  rows.sort(function(a, b) {
    var oa = statusOrder[a.status] != null ? statusOrder[a.status] : 4;
    var ob = statusOrder[b.status] != null ? statusOrder[b.status] : 4;
    if (oa !== ob) return oa - ob;
    // Within matched: Create Bill (not in_zoho) before In Zoho
    var aZoho = (a.inv && a.inv.in_zoho) ? 1 : 0;
    var bZoho = (b.inv && b.inv.in_zoho) ? 1 : 0;
    if (aZoho !== bZoho) return aZoho - bZoho;
    return (a.vendor || '').localeCompare(b.vendor || '');
  });

  // Store globally and save
  _catRows = rows;
  _catMonth = m.month;

  var matched = rows.filter(function(r) { return r.status === 'exact' || r.status === 'close'; }).length;
  var crossMatched = rows.filter(function(r) { return r.status === 'cross_exact' || r.status === 'cross_close'; }).length;
  var noInv = rows.filter(function(r) { return r.status === 'no_invoice'; }).length;
  var noCc = rows.filter(function(r) { return r.status === 'no_cc'; }).length;
  var unmapped = rows.filter(function(r) { return r.status === 'unmapped'; }).length;

  // Save to file
  var saveRows = rows.map(function(r) {
    return {
      vendor: r.vendor,
      cc_amount_inr: r.cc ? r.cc.amount : null,
      cc_forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
      cc_forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null,
      cc_date: r.cc ? r.cc.date : null,
      cc_description: r.cc ? r.cc.description : null,
      inv_amount: r.inv ? r.inv.amount : null,
      inv_currency: r.inv ? r.inv.currency : null,
      inv_date: r.inv ? r.inv.date : null,
      inv_invoice_number: r.inv ? r.inv.invoice_number : null,
      inv_vendor_gstin: r.inv ? r.inv.vendor_gstin : null,
      match_type: r.matchType || null,
      diff: r.diff != null ? r.diff : null,
      status: r.status
    };
  });
  fetch('/api/compare/save-categorize', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      month: m.month, rows: saveRows,
      summary: {matched: matched, cross_matched: crossMatched, no_invoice: noInv, no_cc: noCc, unmapped: unmapped}
    })
  }).then(function() {
    // Refresh overall summary after save completes
    _fetchOverallCategorize();
  });

  // Render CC-based view
  renderCatView('cc');
}

function _fetchOverallCategorize() {
  fetch('/api/compare/categorize-overall').then(function(r) { return r.json(); }).then(function(data) {
    var el = document.getElementById('catOverallSummary');
    if (!el) return;
    var t = data.totals || {};
    var total = t.total || 0;
    var matched = (t.exact || 0) + (t.close || 0);
    var cross = (t.cross_exact || 0) + (t.cross_close || 0);
    var resolved = matched + cross;
    var pct = total ? Math.round(resolved / total * 100) : 0;
    el.innerHTML =
      '<strong style="font-size:13px;min-width:120px">\uD83D\uDCCA All Months (' + (t.months_done || 0) + ' done, ' + total + ' txns)</strong>' +
      '<span style="color:var(--green)">\u2713 Exact: ' + (t.exact || 0) + '</span>' +
      '<span style="color:var(--green)">\u2248 Close: ' + (t.close || 0) + '</span>' +
      '<span style="color:var(--accent)">\u2194 Other Month: ' + cross +
        (cross ? ' <span style="font-size:10px;opacity:0.7">(E:' + (t.cross_exact||0) + ' C:' + (t.cross_close||0) + ')</span>' : '') + '</span>' +
      '<span style="color:var(--yellow)">\u26A0 No Invoice: ' + (t.no_invoice || 0) + '</span>' +
      '<span style="color:var(--text-dim)">\u2753 Unmapped: ' + (t.unmapped || 0) + '</span>' +
      '<span style="color:var(--green);margin-left:auto;font-weight:bold">\u2714 Resolved: ' + resolved + '/' + total + ' (' + pct + '%)</span>';
  }).catch(function() {
    var el = document.getElementById('catOverallSummary');
    if (el) el.innerHTML = '<span style="color:var(--text-dim)">Run Categorize Check on each month to build overall summary</span>';
  });
}

function renderCatView() {
  var container = document.getElementById('categorizeResults');
  container.innerHTML = '';
  var rows = _catRows;
  if (!rows.length) return;
  var fmt = function(n) { return n != null ? Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '-'; };

  // CC-based: only rows that have a CC entry, exclude paid bills
  var filtered = rows.filter(function(r) {
    if (!r.cc) return false;
    if (r.inv && r.inv.zoho_bill_status && r.inv.zoho_bill_status.toLowerCase() === 'paid') return false;
    return true;
  });

  var exactCount = filtered.filter(function(r) { return r.status === 'exact'; }).length;
  var closeCount = filtered.filter(function(r) { return r.status === 'close'; }).length;
  var matched = exactCount + closeCount;
  var crossExact = filtered.filter(function(r) { return r.status === 'cross_exact'; }).length;
  var crossClose = filtered.filter(function(r) { return r.status === 'cross_close'; }).length;
  var crossMatched = crossExact + crossClose;
  var noInv = filtered.filter(function(r) { return r.status === 'no_invoice'; }).length;
  var unmapped = filtered.filter(function(r) { return r.status === 'unmapped'; }).length;

  // Overall summary row (all months aggregate) - placeholder, filled async
  var overallDiv = document.createElement('div');
  overallDiv.id = 'catOverallSummary';
  overallDiv.style.cssText = 'padding:10px 12px;font-size:12px;border-bottom:2px solid var(--accent);background:rgba(108,140,255,0.08);display:flex;gap:12px;flex-wrap:wrap;align-items:center';
  overallDiv.innerHTML = '<span style="color:var(--text-dim)">Loading overall summary...</span>';
  container.appendChild(overallDiv);
  _fetchOverallCategorize();

  // Header
  var hdr = document.createElement('div');
  hdr.style.cssText = 'padding:10px 12px;font-size:12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,0.03);display:flex;gap:16px;flex-wrap:wrap;align-items:center';
  hdr.innerHTML =
    '<strong style="font-size:13px">CC Based (' + filtered.length + ')</strong>' +
    '<span style="color:var(--green)">\u2713 Matched: ' + matched + '</span>' +
    (crossMatched ? '<span style="color:var(--accent)">\u2194 Other Month: ' + crossMatched + '</span>' : '') +
    '<span style="color:var(--yellow)">\u26A0 No Invoice: ' + noInv + '</span>' +
    (unmapped ? '<span style="color:var(--text-dim)">\u2753 Unmapped: ' + unmapped + '</span>' : '') +
    '<button id="catCreateSelectedBtn" onclick="confirmCatCreateSelected()" style="display:none;background:var(--accent);color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600;margin-left:auto">Create & Record (0)</button>';
  container.appendChild(hdr);

  // Status filter bar
  var catFilter = document.createElement('div');
  catFilter.style.cssText = 'padding:6px 12px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;font-size:11px';
  catFilter.innerHTML = '<label style="color:var(--text-dim);font-weight:600">Status:</label>' +
    '<select id="catStatusFilter" onchange="applyCatStatusFilter()" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:3px 8px;font-size:11px">' +
    '<option value="">All</option>' +
    '<option value="exact">Exact</option>' +
    '<option value="close">Close</option>' +
    '<option value="cross_exact">Other Month (Exact)</option>' +
    '<option value="cross_close">Other Month (Close)</option>' +
    '<option value="no_invoice">No Invoice</option>' +
    '<option value="unmapped">Unmapped</option>' +
    '</select>';
  container.appendChild(catFilter);

  // Reset selection tracking
  _catSelectedInvs = {};

  // Table
  var tbl = document.createElement('table');
  tbl.className = 'cat-table';
  tbl.innerHTML = '<thead><tr><th style="width:28px;text-align:center"><input type="checkbox" id="catSelectAll" onchange="toggleCatSelectAll(this)" title="Select all Create Bill rows"></th><th>Vendor</th><th>CC Description</th><th>CC Forex</th><th>CC Amount (INR)</th><th>CC Date</th><th>Card</th><th>Inv Amount</th><th>Inv Date</th><th>Match Type</th><th>Diff</th><th>Confidence</th><th>Status</th><th>Action</th></tr></thead>';
  var tbody = document.createElement('tbody');

  filtered.forEach(function(r) {
    var tr = document.createElement('tr');
    tr.className = 'cat-row';
    tr.setAttribute('data-vendor', (r.vendor || '').toLowerCase());
    tr.setAttribute('data-date', (r.cc && r.cc.date) || '');
    var statusHtml = '';
    var rowBg = '';
    if (r.status === 'exact') {
      statusHtml = '<span style="color:var(--green)">\u2713 Exact</span>';
      rowBg = 'rgba(80,200,120,0.06)';
    } else if (r.status === 'close') {
      statusHtml = '<span style="color:var(--green)">\u2248 Close</span>';
      rowBg = 'rgba(80,200,120,0.04)';
    } else if (r.status === 'cross_exact') {
      statusHtml = '<span style="color:var(--accent)">\u2194 Other Month (Exact)</span>';
      rowBg = 'rgba(96,165,250,0.08)';
    } else if (r.status === 'cross_close') {
      statusHtml = '<span style="color:var(--accent)">\u2194 Other Month (Close)</span>';
      rowBg = 'rgba(96,165,250,0.05)';
    } else if (r.status === 'no_invoice') {
      statusHtml = '<span style="color:var(--yellow)">\u26A0 No Invoice</span>';
      rowBg = 'rgba(255,200,50,0.06)';
    } else if (r.status === 'unmapped') {
      statusHtml = '<span style="color:var(--text-dim)">\u2753 Unmapped</span>';
      rowBg = 'rgba(150,150,150,0.06)';
    }
    if (rowBg) tr.style.background = rowBg;

    var ccDesc = r.cc ? (r.cc.description || '-') : '-';
    var ccAmt = r.cc ? fmt(r.cc.amount) : '-';
    var ccForex = r.cc && r.cc.forex_currency && r.cc.forex_amount
      ? r.cc.forex_currency + ' ' + fmt(r.cc.forex_amount) : '-';
    var ccDate = r.cc && r.cc.date ? fmtDate(r.cc.date) : '-';
    var invAmt = r.inv ? fmt(r.inv.amount) + ' <span style="font-size:9px;color:var(--text-dim)">' + (r.inv.currency || 'INR') + '</span>' : '-';
    var invDate = r.inv && r.inv.date ? fmtDate(r.inv.date) : '-';
    var diffText = r.diff != null ? fmt(r.diff) : '-';

    // Create Bill & Record button or Paid/In Zoho badge
    var actionHtml = '';
    if ((r.status === 'exact' || r.status === 'close') && r.inv) {
      // Grouped invoices: always use bulk flow (handles mix of existing + new bills)
      if (r.inv._grouped_invoices && r.inv._grouped_invoices.length > 1) {
        // Bulk: multiple invoices grouped to 1 CC charge
        var ccVendor = r.cc && r.cc.vendor_name ? r.cc.vendor_name : '';
        var bulkPayload = JSON.stringify({
          invoices: r.inv._grouped_invoices.map(function(gi) {
            return {
              vendor_name: gi.vendor_name || r.vendor,
              amount: gi.amount,
              currency: gi.currency || 'INR',
              date: gi.date,
              invoice_number: gi.invoice_number || '',
              vendor_gstin: gi.vendor_gstin || '',
              file: gi.file || '',
              organized_path: (gi.organized_path || '').replace(/\\/g, '/'),
              in_zoho: gi.in_zoho || false,
              zoho_bill_id: gi.zoho_bill_id || ''
            };
          }),
          cc: {
            transaction_id: r.cc ? r.cc.transaction_id || '' : '',
            amount: r.cc ? r.cc.amount : 0,
            date: r.cc ? r.cc.date : '',
            card_name: r.cc ? r.cc.card_name || '' : '',
            forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
            forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null,
            vendor_name: ccVendor
          }
        }).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;');
        actionHtml = '<button class="bill-create-btn" onclick="createBillAndRecordBulk(this, \'' + bulkPayload + '\')" style="font-size:10px;padding:2px 8px">Create & Record (' + r.inv._grouped_invoices.length + ')</button>';
      } else if (r.inv.in_zoho && r.inv.zoho_bill_id) {
        // Single invoice already in Zoho — Record only (if CC data is complete)
        var _hasCC = r.cc && r.cc.card_name && r.cc.amount;
        var billStatus = (r.inv.zoho_bill_status || '').toLowerCase();
        if (billStatus === 'paid') {
          actionHtml = '<span style="font-size:10px;padding:2px 8px;color:var(--green);font-weight:600">\u2705 Paid</span>';
        } else if (_hasCC) {
          var recPayload = JSON.stringify({
            bill_id: r.inv.zoho_bill_id || '',
            bill_ids: r.inv.zoho_bill_ids || [],
            vendor_name: r.inv.vendor_name || r.vendor,
            amount: r.inv.amount,
            currency: r.inv.currency || 'INR',
            date: r.inv.date,
            cc: {
              transaction_id: r.cc.transaction_id || '',
              amount: r.cc.amount,
              date: r.cc.date || '',
              card_name: r.cc.card_name || '',
              forex_amount: r.cc.forex_amount || null,
              forex_currency: r.cc.forex_currency || null
            }
          }).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;');
          var recLabel = billStatus === 'overdue' ? 'Record (Overdue)' : 'Record';
          actionHtml = '<button class="bill-create-btn" onclick="recordPaymentOnly(this, \'' + recPayload + '\')" style="font-size:10px;padding:2px 8px;background:rgba(34,197,94,0.15);color:var(--green);border:1px solid var(--green)">' + recLabel + '</button>';
        } else {
          actionHtml = '<span style="font-size:9px;color:var(--yellow)">\u25CF In Zoho</span>';
        }
      } else {
        // Single invoice — Create & Record
        var ccVendor = r.cc && r.cc.vendor_name ? r.cc.vendor_name : '';
        var payload = JSON.stringify({
          invoice: {
            vendor_name: r.inv.vendor_name || r.vendor,
            amount: r.inv.amount,
            currency: r.inv.currency || 'INR',
            date: r.inv.date,
            invoice_number: r.inv.invoice_number || '',
            vendor_gstin: r.inv.vendor_gstin || '',
            file: r.inv.file || '',
            organized_path: (r.inv.organized_path || '').replace(/\\/g, '/')
          },
          cc: {
            transaction_id: r.cc ? r.cc.transaction_id || '' : '',
            amount: r.cc ? r.cc.amount : 0,
            date: r.cc ? r.cc.date : '',
            card_name: r.cc ? r.cc.card_name || '' : '',
            forex_amount: r.cc && r.cc.forex_amount ? r.cc.forex_amount : null,
            forex_currency: r.cc && r.cc.forex_currency ? r.cc.forex_currency : null,
            vendor_name: ccVendor
          }
        }).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;');
        actionHtml = '<button class="bill-create-btn" onclick="createBillAndRecord(this, \'' + payload + '\')" style="font-size:10px;padding:2px 8px">Create & Record</button>';
      }
    }

    // Compute confidence for matched rows
    var confHtml = '-';
    if (r.cc && r.inv && (r.status === 'exact' || r.status === 'close' || r.status === 'cross_exact' || r.status === 'cross_close')) {
      // Vendor confidence
      var ccVendor = (r.cc.vendor_name || r.cc.description || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      var invVendor = (r.inv.vendor_name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      var vConf = 0;
      if (ccVendor && invVendor) {
        if (ccVendor === invVendor || ccVendor.indexOf(invVendor) !== -1 || invVendor.indexOf(ccVendor) !== -1) vConf = 100;
        else {
          var ccW = ccVendor.substring(0, Math.min(4, ccVendor.length));
          var invW = invVendor.substring(0, Math.min(4, invVendor.length));
          if (ccW === invW) vConf = 80;
        }
      }
      // Amount confidence — use same currency comparison as the match logic
      var ccForexAm = r.cc.forex_amount ? parseFloat(r.cc.forex_amount) : null;
      var ccForexCur = r.cc.forex_currency || null;
      var ccInrAm = parseFloat(r.cc.amount) || 0;
      var invAm = parseFloat(r.inv.amount) || 0;
      var invCur = (r.inv.currency || 'INR').toUpperCase();
      var ccAm, compareAm;
      if (ccForexCur && ccForexAm && invCur === ccForexCur) {
        // Same forex currency (e.g. USD vs USD)
        ccAm = ccForexAm; compareAm = invAm;
      } else if (ccForexCur && ccForexAm && invCur === 'INR') {
        // Forex CC vs INR invoice — compare INR
        ccAm = ccInrAm; compareAm = invAm;
      } else {
        ccAm = ccInrAm; compareAm = invAm;
      }
      var amDiff = Math.abs(ccAm - compareAm);
      var amPct = ccAm > 0 ? (amDiff / ccAm) : 1;
      var aConf = amPct < 0.001 ? 100 : amPct < 0.01 ? 95 : amPct < 0.05 ? 80 : amPct < 0.1 ? 60 : 40;
      // Date confidence
      var dConf = 0;
      if (r.cc.date && r.inv.date) {
        var cd = new Date(r.cc.date), id = new Date(r.inv.date);
        var daysDiff = Math.abs((cd - id) / 86400000);
        dConf = daysDiff <= 1 ? 100 : daysDiff <= 5 ? 90 : daysDiff <= 15 ? 50 : daysDiff <= 30 ? 25 : 0;
      }
      var overall = Math.round(vConf * 0.4 + aConf * 0.4 + dConf * 0.2);
      var ovColor = overall >= 85 ? 'var(--green)' : overall >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)';
      var _cd = function(v) { var c = v >= 90 ? 'var(--green)' : v >= 60 ? 'var(--yellow)' : 'var(--red,#ef4444)'; return '<span style="color:'+c+';font-weight:700">'+v+'</span>'; };
      confHtml = '<div style="text-align:center;line-height:1.4">'
        + '<div style="font-size:12px;font-weight:700;color:' + ovColor + '">' + overall + '%</div>'
        + '<div style="font-size:8px;color:var(--text-dim)">Vendor:' + _cd(vConf) + ' Amt:' + _cd(aConf) + ' Date:' + _cd(dConf) + '</div></div>';
    }

    // Checkbox for Create Bill rows
    var canCreate = (r.status === 'exact' || r.status === 'close') && r.inv && !r.inv.in_zoho;
    var cbCell = canCreate
      ? '<td style="text-align:center;padding:3px 4px"><input type="checkbox" class="cat-cb" data-catidx="' + filtered.indexOf(r) + '" onchange="toggleCatCheckbox(this)"></td>'
      : '<td style="padding:3px 4px"></td>';

      tr.innerHTML = cbCell +
        '<td style="font-size:11px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + r.vendor + '">' + r.vendor + '</td>' +
        '<td style="font-size:10px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + ccDesc + '">' + ccDesc + '</td>' +
        '<td style="font-size:10px;color:var(--yellow)">' + ccForex + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + ccAmt + '</td>' +
        '<td style="font-size:10px">' + ccDate + '</td>' +
        '<td style="font-size:10px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (r.cc && r.cc.card_name ? r.cc.card_name : '-') + '">' + (r.cc && r.cc.card_name ? r.cc.card_name : '-') + '</td>' +
        '<td style="font-family:monospace;font-size:11px;text-align:right">' + invAmt + '</td>' +
        '<td style="font-size:10px">' + invDate + '</td>' +
        '<td style="font-size:10px">' + (r.matchType || '-') + (r.inv && r.inv.amazon_fc_code ? ' <span style="font-size:8px;padding:1px 3px;border-radius:3px;background:rgba(255,255,255,0.06);color:var(--yellow)">' + r.inv.amazon_fc_code + '</span>' : '') + '</td>' +
        '<td style="font-family:monospace;font-size:10px;text-align:right">' + diffText + '</td>' +
        '<td style="padding:3px 4px">' + confHtml + '</td>' +
        '<td style="font-size:10px;white-space:nowrap">' + statusHtml + '</td>' +
        '<td style="padding:3px 6px">' + actionHtml + '</td>';
      tbody.appendChild(tr);

      // Render individual sub-rows for grouped invoices
      if (r.inv && r.inv._grouped_invoices && r.inv._grouped_invoices.length > 1) {
        var catRowIdx = _catRows.indexOf(r);
        r.inv._grouped_invoices.forEach(function(gi, giIdx) {
          var subTr = document.createElement('tr');
          subTr.className = 'cat-row cat-sub-row';
          subTr.setAttribute('data-vendor', (r.vendor || '').toLowerCase());
          subTr.setAttribute('data-date', (r.cc && r.cc.date) || '');
          subTr.setAttribute('data-status', r.status || '');
          subTr.style.background = 'rgba(108,140,255,0.04)';
          var giAmt = fmt(gi.amount) + ' <span style="font-size:9px;color:var(--text-dim)">' + (gi.currency || 'INR') + '</span>';
          var giStatus = (gi.zoho_bill_status || '').toLowerCase();
          var giStatusHtml = '';
          if (giStatus === 'paid') giStatusHtml = '<span style="color:var(--green);font-size:9px">\u2705 Paid</span>';
          else if (gi.in_zoho) giStatusHtml = '<span style="color:var(--yellow);font-size:9px">\u25CF In Zoho</span>';
          var removeBtn = '<button onclick="removeGroupedInvoice(' + catRowIdx + ',' + giIdx + ')" style="font-size:9px;padding:1px 6px;background:rgba(239,68,68,0.15);color:var(--red,#ef4444);border:1px solid rgba(239,68,68,0.3);border-radius:4px;cursor:pointer" title="Remove this invoice from group">\u2715</button>';
          // Build Amazon entity key tags (e.g., ASSPL, ARIPL)
          var entityTags = '';
          if (gi.amazon_entities) {
            entityTags = Object.keys(gi.amazon_entities).map(function(code) {
              return ' <span style="font-size:8px;padding:1px 3px;border-radius:3px;background:rgba(255,255,255,0.06);color:var(--yellow)" title="' + code + '-' + (gi.amazon_entities[code] || '') + '">' + code + '</span>';
            }).join('');
          }
          var sellerLabel = gi.vendor_name ? ' <span style="font-size:9px;color:var(--text-dim)">(' + gi.vendor_name + ')</span>' : '';
          subTr.innerHTML =
            '<td style="text-align:center">' + removeBtn + '</td>' +
            '<td colspan="5" style="font-size:10px;padding-left:24px;color:var(--text-dim)">' +
              '<span style="color:var(--accent);margin-right:4px">\u2514</span> ' +
              'Bill ' + (giIdx + 1) + ': <span style="color:var(--text)">' + (gi.invoice_number || 'N/A') + '</span>' +
              entityTags + sellerLabel +
            '</td>' +
            '<td style="font-family:monospace;font-size:10px;text-align:right">' + giAmt + '</td>' +
            '<td style="font-size:10px">' + fmtDate(gi.date) + '</td>' +
            '<td colspan="3" style="font-size:9px;color:var(--text-dim)">' + (gi.zoho_bill_id || '') + '</td>' +
            '<td style="font-size:10px">' + giStatusHtml + '</td>' +
            '<td></td>';
          tbody.appendChild(subTr);
        });
      }
  });

  tbl.appendChild(tbody);
  container.appendChild(tbl);
}

// --- Remove an invoice from a grouped row ---
function removeGroupedInvoice(catRowIdx, giIdx) {
  var r = _catRows[catRowIdx];
  if (!r || !r.inv || !r.inv._grouped_invoices) return;
  var gis = r.inv._grouped_invoices;
  if (gis.length <= 1) return; // Can't remove the last one

  var removed = gis.splice(giIdx, 1)[0];
  addLogLine('[Group] Removed: ' + (removed.invoice_number || 'N/A') + ' (' + (removed.amount || 0) + ' ' + (removed.currency || 'INR') + ')');

  // Recalculate parent row totals
  var newTotal = gis.reduce(function(s, g) { return s + (parseFloat(g.amount) || 0); }, 0);
  r.inv.amount = newTotal;
  r.inv.invoice_number = gis.map(function(g) { return g.invoice_number || ''; }).filter(Boolean).join(' + ');
  r.inv.vendor_gstin = gis[0].vendor_gstin || '';
  r.inv.date = gis[0].date;

  // Recalculate zoho status
  var anyInZoho = gis.some(function(g) { return g.in_zoho; });
  var zohoBillIds = [];
  var zohoBillId = '';
  var zohoBillStatus = '';
  gis.forEach(function(g) {
    if (g.zoho_bill_id && zohoBillIds.indexOf(g.zoho_bill_id) === -1) zohoBillIds.push(g.zoho_bill_id);
    if (!zohoBillId && g.zoho_bill_id) { zohoBillId = g.zoho_bill_id; zohoBillStatus = g.zoho_bill_status || ''; }
  });
  r.inv.in_zoho = anyInZoho;
  r.inv.zoho_bill_id = zohoBillId;
  r.inv.zoho_bill_ids = zohoBillIds;
  r.inv.zoho_bill_status = zohoBillStatus;

  // Recalculate diff
  var ccAmt = r.cc ? (r.cc.forex_amount && r.cc.forex_currency ? parseFloat(r.cc.forex_amount) : parseFloat(r.cc.amount)) : 0;
  r.diff = Math.abs(newTotal - ccAmt);
  r.status = r.diff < 0.01 ? 'exact' : 'close';

  // Update match type
  var cur = gis[0].currency || 'INR';
  var ccCur = r.cc && r.cc.forex_currency ? r.cc.forex_currency : 'INR';
  r.matchType = ccCur + ' \u2192 ' + cur + ' (GSTIN:' + (r.inv.vendor_gstin || '').substring(0, 10) + '.. x' + gis.length + ')';

  // If only 1 left, simplify back to single invoice row
  if (gis.length === 1) {
    var solo = gis[0];
    r.inv = solo;
    r.inv._grouped_invoices = undefined;
    r.matchType = (ccCur || 'INR') + ' \u2192 ' + (solo.currency || 'INR');
  }

  renderCatView();
}

// --- Create Bill & Record Payment from Monthly Compare ---
function createBillAndRecord(btn, payloadStr) {
  var payload = JSON.parse(payloadStr.replace(/&quot;/g, '"'));
  var inv = payload.invoice || {};
  var desc = (inv.vendor_name || '') + ' — ' + (inv.currency || 'INR') + ' ' + Number(inv.amount).toLocaleString() + (inv.invoice_number ? ' (' + inv.invoice_number + ')' : '');
  showModal('Create Bill & Record Payment?', 'This will create a bill and record payment in Zoho Books: ' + desc, function() {
    btn.disabled = true;
    btn.textContent = 'Creating...';
    fetch('/api/bills/create-and-record', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'paid') {
        var badge = document.createElement('span');
        badge.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600';
        badge.textContent = '\u2713 Paid';
        // Get row reference before replacing btn (which removes it from DOM)
        var row = btn.closest('tr');
        btn.parentNode.replaceChild(badge, btn);
        // Disable checkbox if present
        var cb = row ? row.querySelector('.cat-cb') : null;
        if (cb) { cb.checked = false; cb.disabled = true; }
        addLogLine('[Bill+Pay] Paid: ' + (inv.invoice_number || inv.vendor_name) + ' -> ' + (data.bill_id || '') + ' / ' + (data.payment_id || ''));
      } else if (data.status === 'bill_created') {
        btn.textContent = 'Bill OK, Pay Failed';
        btn.style.color = 'var(--yellow)';
        btn.disabled = false;
        addLogLine('[Bill+Pay] Bill created but payment failed: ' + (data.error || 'unknown'));
      } else if (data.status === 'already_paid') {
        var badge2 = document.createElement('span');
        badge2.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600';
        badge2.textContent = '\u2713 Paid';
        btn.parentNode.replaceChild(badge2, btn);
      } else {
        btn.textContent = 'Failed';
        btn.disabled = false;
        addLogLine('[Bill+Pay] Error: ' + (data.error || 'unknown'));
      }
    })
    .catch(function(err) {
      btn.textContent = 'Error';
      btn.disabled = false;
      addLogLine('[Bill+Pay] Error: ' + err);
    });
  }, false, 'Create & Record');
}

// --- Bulk Create Bills & Record Payments (grouped invoices -> 1 CC charge) ---
function createBillAndRecordBulk(btn, payloadStr) {
  var payload = JSON.parse(payloadStr.replace(/&quot;/g, '"'));
  var invoices = payload.invoices || [];
  var count = invoices.length;
  var totalAmt = invoices.reduce(function(s, i) { return s + (parseFloat(i.amount) || 0); }, 0);
  var detailList = invoices.map(function(i, idx) {
    return (idx + 1) + '. ' + (i.invoice_number || i.vendor_name) + ' (' + (i.currency || 'INR') + ' ' + Number(i.amount).toLocaleString() + ')';
  }).join('<br>');
  showModal('Create ' + count + ' Bills & Record?',
    'This will:<br>1. Create each bill one by one<br>2. Record payment for each bill<br>3. Club all ' + count + ' payments and auto-match with CC<br><br>' + detailList,
    function() {
      btn.disabled = true;
      btn.textContent = 'Processing 0/' + count + '...';
      addLogLine('[Bulk] Starting: ' + count + ' bills, total ' + totalAmt.toLocaleString(undefined, {minimumFractionDigits:2}));

      // Progress animation
      var progress = 0;
      var progressInterval = setInterval(function() {
        if (progress < count) {
          progress++;
          btn.textContent = 'Processing ' + progress + '/' + count + '...';
        }
      }, 2000);

      fetch('/api/bills/create-and-record-bulk', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        clearInterval(progressInterval);
        var row = btn.closest('tr');

        // Build summary report
        var total = data.total || count;
        var created = data.created_count || 0;
        var paid = data.paid_count || 0;
        var skipped = data.already_paid_count || 0;
        var billOnly = data.bill_created_only || 0;
        var errors = data.error_count || 0;
        var matched = data.banking_matched;

        // Log summary header
        addLogLine('');
        addLogLine('========== BULK REPORT ==========');
        addLogLine('Total bills: ' + total);
        addLogLine('Created:     ' + created);
        addLogLine('Recorded:    ' + paid);
        if (skipped > 0) addLogLine('Skipped:     ' + skipped + ' (already paid)');
        if (billOnly > 0) addLogLine('Bill only:   ' + billOnly + ' (payment failed)');
        if (errors > 0) addLogLine('Errors:      ' + errors);
        addLogLine('Auto-match:  ' + (matched ? 'YES - ' + paid + ' payments matched to 1 CC' : 'NO'));
        addLogLine('=================================');

        // Log individual results
        if (data.results) {
          data.results.forEach(function(res, ri) {
            var icon = res.status === 'paid' ? '[OK]' : res.status === 'already_paid' ? '[SKIP]' : '[FAIL]';
            addLogLine('  ' + icon + ' ' + (res.invoice_number || '?') + ': ' + res.status +
              (res.bill_id ? ' -> ' + res.bill_id : '') +
              (res.payment_id ? ' / pay:' + res.payment_id : '') +
              (res.error ? ' - ' + res.error : ''));
          });
        }

        if (data.status === 'paid' || data.status === 'partial') {
          // Replace button with summary badge
          var badgeHtml = document.createElement('div');
          badgeHtml.style.cssText = 'font-size:9px;line-height:1.4';
          badgeHtml.innerHTML =
            '<span style="color:var(--green);font-weight:600">' + paid + '/' + total + ' Paid</span>' +
            (skipped > 0 ? '<br><span style="color:var(--yellow)">' + skipped + ' skipped</span>' : '') +
            (errors > 0 ? '<br><span style="color:var(--red,#ef4444)">' + errors + ' errors</span>' : '') +
            '<br><span style="color:' + (matched ? 'var(--green)' : 'var(--yellow)') + '">' +
              (matched ? 'CC matched' : 'CC not matched') + '</span>';
          btn.parentNode.replaceChild(badgeHtml, btn);
          var cb = row ? row.querySelector('.cat-cb') : null;
          if (cb) { cb.checked = false; cb.disabled = true; }
        } else {
          btn.textContent = 'Failed (' + errors + ' errors)';
          btn.style.color = 'var(--red,#ef4444)';
          btn.disabled = false;
          addLogLine('[Bulk] Error: ' + (data.error || 'see details above'));
        }
      })
      .catch(function(err) {
        clearInterval(progressInterval);
        btn.textContent = 'Error';
        btn.disabled = false;
        addLogLine('[Bulk] Error: ' + err);
      });
    }, false, 'Create ' + count + ' Bills');
}

// --- Record Payment Only (bill already in Zoho) ---
function recordPaymentOnly(btn, payloadStr) {
  var payload = JSON.parse(payloadStr.replace(/&quot;/g, '"'));
  var desc = (payload.vendor_name || '') + ' — ' + (payload.currency || 'INR') + ' ' + Number(payload.amount).toLocaleString();
  showModal('Record Payment?', 'Bill already exists in Zoho. This will record payment and auto-match the CC banking transaction: ' + desc, function() {
    btn.disabled = true;
    btn.textContent = 'Recording...';
    fetch('/api/bills/record-only', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'paid' || data.status === 'already_paid') {
        var badge = document.createElement('span');
        badge.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600';
        badge.textContent = data.status === 'already_paid' ? '\u2705 Already Paid' : '\u2713 Paid';
        btn.parentNode.replaceChild(badge, btn);
        addLogLine('[Record] ' + (data.status === 'already_paid' ? 'Already paid' : 'Paid') + ': ' + (payload.vendor_name || '') + (data.payment_id ? ' -> payment_id=' + data.payment_id : ''));
      } else {
        btn.textContent = 'Failed';
        btn.disabled = false;
        addLogLine('[Record] Error: ' + (data.error || 'unknown') + (data.received_cc ? ' | cc=' + JSON.stringify(data.received_cc) : ''));
      }
    })
    .catch(function(err) {
      btn.textContent = 'Error';
      btn.disabled = false;
      addLogLine('[Record] Error: ' + err);
    });
  }, false, 'Record Payment');
}

// --- Categorize Check: bulk Create & Record selection ---
var _catSelectedItems = {}; // idx -> {invoice, cc} payload

function toggleCatCheckbox(cb) {
  var idx = cb.getAttribute('data-catidx');
  if (cb.checked) {
    var filtered = _catRows.filter(function(r) { return r.cc != null; });
    var r = filtered[parseInt(idx)];
    if (r && r.inv && r.cc) {
      _catSelectedItems[idx] = {
        invoice: {
          vendor_name: r.inv.vendor_name || r.vendor,
          amount: r.inv.amount,
          currency: r.inv.currency || 'INR',
          date: r.inv.date,
          invoice_number: r.inv.invoice_number || '',
          vendor_gstin: r.inv.vendor_gstin || ''
        },
        cc: {
          transaction_id: r.cc.transaction_id || '',
          amount: r.cc.amount || 0,
          date: r.cc.date || '',
          card_name: r.cc.card_name || '',
          forex_amount: r.cc.forex_amount || null,
          forex_currency: r.cc.forex_currency || null
        }
      };
    }
  } else {
    delete _catSelectedItems[idx];
  }
  _updateCatSelectedBtn();
}

function toggleCatSelectAll(cb) {
  document.querySelectorAll('.cat-cb').forEach(function(c) {
    var row = c.closest('tr');
    if (row && row.style.display === 'none') return;
    c.checked = cb.checked;
    toggleCatCheckbox(c);
  });
}

function _updateCatSelectedBtn() {
  var btn = document.getElementById('catCreateSelectedBtn');
  var count = Object.keys(_catSelectedItems).length;
  if (count > 0) {
    btn.style.display = 'inline-block';
    btn.textContent = 'Create & Record (' + count + ')';
    btn.disabled = false;
  } else {
    btn.style.display = 'none';
  }
}

function confirmCatCreateSelected() {
  var count = Object.keys(_catSelectedItems).length;
  if (!count) return;
  showModal('Create Bills & Record Payments?', 'This will create ' + count + ' bills, record payments, and auto-match banking transactions in Zoho Books.', function() {
    catCreateAndRecordSelected();
  }, true, 'Create & Record ' + count);
}

function catCreateAndRecordSelected() {
  var keys = Object.keys(_catSelectedItems);
  if (!keys.length) return;
  var btn = document.getElementById('catCreateSelectedBtn');
  btn.disabled = true;
  btn.textContent = 'Processing ' + keys.length + '...';
  var total = keys.length, done = 0, success = 0;

  // Sequential to avoid rate limits
  function processNext(i) {
    if (i >= keys.length) {
      btn.textContent = success + '/' + total + ' Paid';
      _updateCatSelectedBtn();
      addLogLine('[Bill+Pay] Bulk: ' + success + '/' + total + ' paid');
      return;
    }
    var idx = keys[i];
    var payload = _catSelectedItems[idx];
    var inv = payload.invoice;
    var cb = document.querySelector('.cat-cb[data-catidx="' + idx + '"]');
    btn.textContent = 'Processing ' + (i + 1) + '/' + total + '...';

    fetch('/api/bills/create-and-record', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      done++;
      if (data.status === 'paid' || data.status === 'already_paid') {
        success++;
        if (cb) {
          var actionTd = cb.closest('tr').querySelector('td:last-child');
          if (actionTd) {
            actionTd.innerHTML = '<span style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,0.15);color:var(--green);font-weight:600">\u2713 Paid</span>';
          }
          cb.checked = false; cb.disabled = true;
        }
        delete _catSelectedItems[idx];
        addLogLine('[Bill+Pay] Paid: ' + (inv.invoice_number || inv.vendor_name));
      } else {
        addLogLine('[Bill+Pay] Failed: ' + (inv.invoice_number || inv.vendor_name) + ' - ' + (data.error || 'unknown'));
      }
      setTimeout(function() { processNext(i + 1); }, 500);
    })
    .catch(function(err) {
      done++;
      addLogLine('[Bill+Pay] Error: ' + (inv.invoice_number || inv.vendor_name) + ' - ' + err);
      setTimeout(function() { processNext(i + 1); }, 500);
    });
  }
  processNext(0);
}

// --- Info tooltip positioning (fixed, not clipped by scroll) ---
document.querySelectorAll('.info-btn').forEach(function(btn) {
  var tip = btn.querySelector('.info-tooltip');
  if (!tip) return;
  btn.addEventListener('mouseenter', function() {
    var rect = btn.getBoundingClientRect();
    tip.style.display = 'block';
    // Position to the right of the button
    var left = rect.right + 10;
    var top = rect.top + rect.height / 2 - tip.offsetHeight / 2;
    // Keep within viewport
    if (left + 290 > window.innerWidth) left = rect.left - 290;
    if (top < 4) top = 4;
    if (top + tip.offsetHeight > window.innerHeight - 4) top = window.innerHeight - 4 - tip.offsetHeight;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  });
  btn.addEventListener('mouseleave', function() {
    tip.style.display = 'none';
  });
});
</script>
</body>
</html>
"""

# --- Main ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CC Statement Automation Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Port to run on (default: 5000)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Ensure output dir exists
    os.makedirs(os.path.join(PROJECT_ROOT, "output"), exist_ok=True)

    log_action("Dashboard starting on http://localhost:{0}".format(args.port))

    if not args.no_open and not os.environ.get("WERKZEUG_RUN_MAIN"):
        # Only open browser on initial launch, not on reloader restarts
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    # TEMPLATES_AUTO_RELOAD: pick up HTML/CSS/JS changes on browser refresh
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True, use_reloader=True)
