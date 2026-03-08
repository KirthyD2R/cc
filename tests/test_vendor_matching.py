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
