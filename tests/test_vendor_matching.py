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


# --- Vendor-gated matching algorithm tests ---

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


# --- Edge case tests ---


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


def test_fuzzy_fallback_matches_bill_vendor():
    """CC description keyword matching bill vendor name triggers fuzzy match."""
    bills = [_make_bill("Supabase Pte. Ltd", 500.00)]
    cc = [_make_cc("SUPABASE PTE LTD SINGAPORE", 500.00)]
    # No manual or learned mappings — fuzzy should find "Supabase" in bill vendor
    matches = _build_vendor_gated_matches(bills, cc, {}, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1


def test_fuzzy_fallback_no_false_positive():
    """Fuzzy fallback should NOT match unrelated vendors."""
    bills = [_make_bill("Microsoft Corporation (India) Pvt Ltd", 5000.00)]
    cc = [_make_cc("SHOE DEPT 0378, BEAUMONT", 5000.00)]
    matches = _build_vendor_gated_matches(bills, cc, {}, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 0


def test_special_chars_in_description_still_match():
    """Kotak descriptions with non-printable chars should match via normalization."""
    bills = [_make_bill("Anthropic USD", 4216.45)]
    # \ufffd simulates the replacement chars in Kotak card descriptions
    cc = [_make_cc("CLAUDE.AI\ufffdSUBSCRIPTION\ufffdANTHROPIC.COM\ufffdCA", 4216.45)]
    vendor_map = {"claude.ai subscription": "Anthropic USD"}
    matches = _build_vendor_gated_matches(bills, cc, vendor_map, {})
    matched = [m for m in matches if m["status"] == "matched"]
    assert len(matched) == 1


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


# --- Forex cache tests ---


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


# --- Forex-based matching tests ---


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
    assert matched[0]["confidence"]["amount"] >= 95


# --- Group matching tests ---


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


def test_candidate_nearest_forex_rate_fallback():
    """USD bill without forex tag uses nearest cached rate, not stale 87.0 fallback."""
    # Bill date 2025-09-29 is NOT in cache; nearest is 2025-09-25 (88.69)
    # 30 USD × 88.69 = 2660.70 INR; CC is 2661.00 → diff ~0.01% → amount >= 95
    unmatched_bills = [_make_bill("S2 Labs Inc.", 30.00, currency="USD", date="2025-09-29")]
    cc_only = [_make_cc("HYPERBROWSER.AI, SAN FRANCISCO", 2661.00, date="2025-09-28")]
    forex_rates = {"2025-09-25": {"USD_INR": 88.69}}
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only, forex_rates=forex_rates)
    assert len(results[0]["candidates"]) == 1
    cand = results[0]["candidates"][0]
    # With nearest rate, amount should score high (95+), not the old 50
    assert cand["breakdown"]["amount"] >= 95


def test_candidate_smooth_amount_gradient():
    """Amount score uses smooth gradient: 2% diff scores 70, not the old 50."""
    # 500 INR bill, CC at 510 = 2% diff → should score 70
    unmatched_bills = [_make_bill("SomeVendor", 500.00, date="2025-07-15")]
    cc_only = [_make_cc("TXN", 510.00, date="2025-07-15")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    cand = results[0]["candidates"][0]
    assert cand["breakdown"]["amount"] == 70


def test_candidate_rebalanced_weights():
    """Rebalanced weights: amount 50%, date 25%, vendor 15%, uniqueness 10%."""
    # Exact amount + close date + no vendor + single candidate
    unmatched_bills = [_make_bill("Unknown", 1000.00, date="2025-07-15")]
    cc_only = [_make_cc("RANDOM TXN", 1000.00, date="2025-07-15")]
    results = _find_candidates_for_unmatched(unmatched_bills, cc_only)
    cand = results[0]["candidates"][0]
    # amount=100×0.5 + date=100×0.25 + vendor=0×0.15 + uniqueness=15×0.1 = 76
    assert cand["candidate_score"] == 76


def test_candidate_integrates_with_vendor_gated():
    """Vendor-gated matching runs first; candidates only for leftovers."""
    bills = [
        _make_bill("Microsoft Corporation (India) Pvt Ltd", 12215.38, bill_id="B1"),
        _make_bill("S2 Labs Inc.", 30.00, currency="USD", date="2025-02-02", bill_id="B2"),
    ]
    cc = [
        _make_cc("MICROSOFTBUS, MUMBAI", 12215.38),
        _make_cc("WINDSURF.COM", 2610.00, date="2025-02-01",
                 forex_amount=30.00, forex_currency="USD"),
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
    assert results[0]["candidates"][0]["cc_description"] == "WINDSURF.COM"
    assert results[0]["candidates"][0]["candidate_score"] >= 50
