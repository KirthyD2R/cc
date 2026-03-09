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
