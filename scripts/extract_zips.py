"""
Utility: Extract all PDFs from ZIP files in 'all zips' folder.

- Handles nested folders inside ZIPs
- Handles nested ZIPs (ZIPs inside ZIPs)
- Skips HTML and non-PDF files
- Strips browser download suffixes like ' (1)' from filenames
- Copies to input_pdfs/invoices/ (skips if already exists)

Usage:
    python scripts/extract_zips.py
    python scripts/extract_zips.py --dry-run
"""

import os
import re
import sys
import shutil
import zipfile
import tempfile

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

ZIP_DIR = os.path.join(PROJECT_ROOT, "all zips")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "input_pdfs", "invoices")


def strip_download_suffix(filename):
    """Remove browser download suffixes like ' (1)', ' (2)' from filename."""
    name, ext = os.path.splitext(filename)
    cleaned = re.sub(r'\s*\(\d+\)$', '', name)
    return cleaned + ext


def extract_pdfs_from_zip(zip_path, dest_dir, dry_run=False):
    """Extract all PDFs from a ZIP (including nested ZIPs and folders).

    Returns list of (source_name, dest_name, status) tuples.
    """
    results = []

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                name = member.filename

                # Skip directories
                if member.is_dir():
                    continue

                basename = os.path.basename(name)
                if not basename:
                    continue

                # Handle nested ZIPs
                if basename.lower().endswith('.zip'):
                    # Extract nested zip to temp, then recurse
                    with tempfile.TemporaryDirectory() as tmpdir:
                        nested_path = zf.extract(member, tmpdir)
                        nested_results = extract_pdfs_from_zip(nested_path, dest_dir, dry_run)
                        results.extend(nested_results)
                    continue

                # Only process PDFs and EMLs
                if not basename.lower().endswith(('.pdf', '.eml')):
                    continue

                # Clean up filename
                clean_name = strip_download_suffix(basename)
                dest_path = os.path.join(dest_dir, clean_name)

                # Skip if already exists
                if os.path.exists(dest_path):
                    results.append((basename, clean_name, "exists"))
                    continue

                # Also check if original name (with suffix) already exists
                orig_dest = os.path.join(dest_dir, basename)
                if os.path.exists(orig_dest):
                    results.append((basename, basename, "exists"))
                    continue

                if dry_run:
                    results.append((basename, clean_name, "would_copy"))
                else:
                    # Extract to temp then copy with clean name
                    with tempfile.TemporaryDirectory() as tmpdir:
                        extracted = zf.extract(member, tmpdir)
                        os.makedirs(dest_dir, exist_ok=True)
                        shutil.copy2(extracted, dest_path)
                    results.append((basename, clean_name, "copied"))

    except zipfile.BadZipFile:
        print(f"  ERROR: Bad zip file: {zip_path}")
    except Exception as e:
        print(f"  ERROR: {zip_path}: {e}")

    return results


def extract_pdfs_all():
    """Extract all PDFs from ZIPs + loose PDFs in 'all zips' folder.

    Called by app.py API endpoint. Uses log_action for live UI logs.
    Returns dict with {copied, skipped} counts.
    """
    from utils import log_action

    if not os.path.isdir(ZIP_DIR):
        log_action(f"No 'all zips' folder found at {ZIP_DIR}", "WARNING")
        return {"copied": 0, "skipped": 0}

    all_entries = sorted(os.listdir(ZIP_DIR))
    zip_files = [f for f in all_entries if f.lower().endswith('.zip')]
    loose_pdfs = [f for f in all_entries if f.lower().endswith('.pdf')]
    loose_emls = [f for f in all_entries if f.lower().endswith('.eml')]

    log_action(f"Found {len(zip_files)} ZIPs, {len(loose_pdfs)} loose PDFs, {len(loose_emls)} loose EMLs in 'all zips'")

    total_copied = 0
    total_skipped = 0

    # Extract from ZIPs
    for zf_name in zip_files:
        zip_path = os.path.join(ZIP_DIR, zf_name)
        results = extract_pdfs_from_zip(zip_path, OUTPUT_DIR, dry_run=False)
        copied = sum(1 for _, _, s in results if s == "copied")
        skipped = sum(1 for _, _, s in results if s == "exists")
        for _, dest, status in results:
            if status == "copied":
                log_action(f"  + {dest}")
        if copied > 0:
            log_action(f"  {zf_name}: {copied} new PDFs extracted")
        total_copied += copied
        total_skipped += skipped

    # Copy loose PDFs and EMLs
    for loose_name in loose_pdfs + loose_emls:
        clean_name = strip_download_suffix(loose_name)
        dest_path = os.path.join(OUTPUT_DIR, clean_name)
        if os.path.exists(dest_path):
            total_skipped += 1
            continue
        src_path = os.path.join(ZIP_DIR, loose_name)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        log_action(f"  + {clean_name}")
        total_copied += 1

    log_action(f"ZIP extraction done: {total_copied} new files, {total_skipped} already existed")
    return {"copied": total_copied, "skipped": total_skipped}


def main():
    dry_run = "--dry-run" in sys.argv

    if not os.path.isdir(ZIP_DIR):
        print(f"ERROR: ZIP directory not found: {ZIP_DIR}")
        return

    zip_files = sorted([f for f in os.listdir(ZIP_DIR) if f.lower().endswith('.zip')])
    print(f"Found {len(zip_files)} ZIP files in '{ZIP_DIR}'")
    if dry_run:
        print("DRY RUN — no files will be copied\n")
    print()

    total_copied = 0
    total_skipped = 0

    for zf_name in zip_files:
        zip_path = os.path.join(ZIP_DIR, zf_name)
        print(f"=== {zf_name} ===")

        results = extract_pdfs_from_zip(zip_path, OUTPUT_DIR, dry_run)

        copied = sum(1 for _, _, s in results if s in ("copied", "would_copy"))
        skipped = sum(1 for _, _, s in results if s == "exists")

        for src, dest, status in results:
            if status == "copied":
                print(f"  + {dest}")
            elif status == "would_copy":
                print(f"  [DRY] {dest}")
            elif status == "exists":
                print(f"  . {dest} (already exists)")

        print(f"  >> {copied} new, {skipped} skipped\n")
        total_copied += copied
        total_skipped += skipped

    action = "would copy" if dry_run else "copied"
    print(f"TOTAL: {action} {total_copied} PDFs, skipped {total_skipped} (already exist)")
    print(f"Destination: {OUTPUT_DIR}")
    print(f"\nNext: run Step 2 (Extract Invoices) to parse and organize month-wise")


if __name__ == "__main__":
    main()
