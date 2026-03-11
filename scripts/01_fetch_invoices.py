"""
Step 1: Fetch Invoice PDFs from Outlook Inbox

Uses Microsoft Graph API (MSAL delegated auth) to download PDF
attachments from the invoice email inbox and store them locally
in input_pdfs/mail invoices/.
"""

import os
import re
import json
import base64
import time 
import zipfile
import tempfile
import requests
from datetime import datetime, timedelta
from utils import PROJECT_ROOT, log_action


def _strip_dup_suffix(filename):
    """Strip browser download duplicate suffixes like ' (1)', ' (2)' from filenames.

    e.g. 'Invoice-IVSLM7IE-0001 (2).pdf' -> 'Invoice-IVSLM7IE-0001.pdf'
    """
    base, ext = os.path.splitext(filename)
    clean = re.sub(r'\s*\(\d+\)\s*$', '', base)
    return (clean + ext) if clean != base else filename

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "outlook_config.json")
TOKEN_PATH = os.path.join(PROJECT_ROOT, "config", "outlook_token.json")


def load_outlook_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


# --- OAuth2 Authorization Code Flow (for confidential apps) ---

def get_access_token(config, headless=False):
    """Get a valid access token. Uses cached token or launches browser auth.

    Args:
        headless: If True, raise error instead of opening browser (for scheduled runs).
    """
    outlook = config["outlook"]
    tenant_id = outlook["tenant_id"]

    # Try cached token first
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r") as f:
            token_data = json.load(f)
        obtained_at = token_data.get("obtained_at", 0)
        expires_in = token_data.get("expires_in", 0)
        if time.time() < obtained_at + expires_in - 300:
            return token_data["access_token"]

        # Issue #30: Try refresh token before falling back to browser auth
        if token_data.get("refresh_token"):
            log_action("Access token expired, attempting refresh...")
            try:
                token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
                resp = requests.post(token_url, data={
                    "client_id": outlook["client_id"],
                    "client_secret": outlook["client_secret"],
                    "refresh_token": token_data["refresh_token"],
                    "grant_type": "refresh_token",
                    "scope": " ".join(outlook.get("scopes", ["Mail.Read", "User.Read"])),
                })
                if resp.ok:
                    result = resp.json()
                    refreshed = {
                        "token_type": result.get("token_type", "Bearer"),
                        "scope": result.get("scope", ""),
                        "expires_in": result.get("expires_in", 3600),
                        "access_token": result["access_token"],
                        "refresh_token": result.get("refresh_token", token_data["refresh_token"]),
                        "obtained_at": time.time(),
                    }
                    with open(TOKEN_PATH, "w") as f:
                        json.dump(refreshed, f, indent=2)
                    log_action("Token refreshed successfully")
                    return result["access_token"]
            except Exception as e:
                log_action(f"Refresh failed ({e}), falling back to browser auth", "WARNING")

        # In headless mode, don't attempt browser auth
        if headless:
            raise RuntimeError(
                "Outlook token expired and cannot refresh. "
                "Run 'python scripts/01_fetch_invoices.py' manually to re-authenticate."
            )

        log_action("Cached token expired, re-authenticating...")
    else:
        # No cached token at all
        if headless:
            raise RuntimeError(
                "No Outlook token cached. "
                "Run 'python scripts/01_fetch_invoices.py' manually to authenticate first."
            )

    # Authorization code flow with local redirect server
    import urllib.parse
    import webbrowser
    import http.server
    import threading

    client_id = outlook["client_id"]
    client_secret = outlook["client_secret"]
    redirect_uri = outlook.get("redirect_uri", "http://localhost:8080/callback")
    scopes = " ".join(outlook.get("scopes", ["Mail.Read", "User.Read"]))

    # Build authorization URL
    auth_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "response_mode": "query",
        })
    )

    # Local server to capture the auth code callback
    auth_code = [None]
    auth_error = [None]

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            auth_code[0] = params.get("code", [None])[0]
            auth_error[0] = params.get("error_description", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if auth_code[0]:
                self.wfile.write(b"<h2>Authentication successful! You can close this tab.</h2>")
            else:
                self.wfile.write(b"<h2>Authentication failed. Check the terminal.</h2>")

        def log_message(self, format, *args):
            pass

    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or 8080

    server = http.server.HTTPServer(("localhost", port), CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    print(f"\n  Opening browser for authentication...")
    print(f"  If it doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    server_thread.join(timeout=120)
    server.server_close()

    if auth_error[0]:
        raise Exception(f"Auth denied: {auth_error[0]}")
    if not auth_code[0]:
        raise Exception("No authorization code received. Timed out or callback missed.")

    # Exchange authorization code for tokens
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    resp = requests.post(token_url, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code[0],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    result = resp.json()

    if "access_token" not in result:
        raise Exception(f"Token exchange failed: {result.get('error_description', result)}")

    # Cache the token (Issue #30: include refresh_token for unattended renewal)
    token_out = {
        "token_type": result.get("token_type", "Bearer"),
        "scope": result.get("scope", ""),
        "expires_in": result.get("expires_in", 3600),
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token"),
        "obtained_at": time.time(),
    }
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_out, f, indent=2)

    log_action("New token obtained and cached")
    return result["access_token"]


def get_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --- Email Fetching ---

def fetch_invoice_emails(token, config, since_override=None):
    """Fetch emails with PDF attachments from the search period.

    Args:
        since_override: ISO timestamp string. If provided, overrides config
            search_period.from for incremental polling.
    """
    headers = get_headers(token)
    search_period = config.get("search_period", {})
    # Issue #25: Default to last 60 days if no dates specified
    default_from = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    default_to = datetime.now().strftime("%Y-%m-%d")
    date_from = since_override or search_period.get("from", default_from)
    date_to = search_period.get("to", default_to)

    url = f"{GRAPH_BASE}/me/mailFolders/Inbox/messages"
    filter_str = f"hasAttachments eq true and receivedDateTime ge {date_from}T00:00:00Z and receivedDateTime le {date_to}T23:59:59Z"
    # Graph API: build URL manually to avoid double-encoding OData params
    odata = f"$filter={filter_str}&$select=id,subject,receivedDateTime,from&$top=100"
    if "?" in url:
        url = f"{url}&{odata}"
    else:
        url = f"{url}?{odata}"
    params = {}

    all_messages = []
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    all_messages.extend(data.get("value", []))

    # Handle pagination
    while "@odata.nextLink" in data:
        resp = requests.get(data["@odata.nextLink"], headers=headers)
        resp.raise_for_status()
        data = resp.json()
        all_messages.extend(data.get("value", []))

    log_action(f"Found {len(all_messages)} emails with attachments ({date_from} to {date_to})")
    return all_messages


def _extract_pdfs_from_zip(zip_bytes, output_dir):
    """Extract PDF files from a ZIP archive. Returns list of extracted filenames."""
    extracted = []
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(zip_bytes)
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            for entry in zf.namelist():
                # Skip directories and non-PDF files
                if entry.endswith("/") or not entry.lower().endswith(".pdf"):
                    continue

                # Use only the filename (discard nested folder paths inside ZIP)
                basename = os.path.basename(entry)
                if not basename:
                    continue

                safe_name = _strip_dup_suffix(basename.replace("/", "_").replace("\\", "_"))
                dest = os.path.join(output_dir, safe_name)
                if os.path.exists(dest):
                    log_action(f"  Already exists (from ZIP), skipping: {safe_name}")
                    continue

                with zf.open(entry) as src, open(dest, "wb") as dst:
                    dst.write(src.read())

                extracted.append(safe_name)
                log_action(f"  Extracted from ZIP: {safe_name}")
    except zipfile.BadZipFile:
        log_action(f"  Invalid ZIP file, skipping", "WARNING")
    finally:
        os.unlink(tmp_path)

    return extracted


def download_pdf_attachments(token, message, output_dir):
    """Download PDF and ZIP attachments from a single email message.

    PDFs are saved directly. ZIP files are extracted and any PDFs inside
    are saved to the output directory.
    """
    headers = get_headers(token)
    url = f"{GRAPH_BASE}/me/messages/{message['id']}/attachments"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()

    downloaded = []
    for att in resp.json().get("value", []):
        name = att.get("name", "")
        name_lower = name.lower()

        if not (name_lower.endswith(".pdf") or name_lower.endswith(".zip")):
            continue

        content = base64.b64decode(att["contentBytes"])

        # Handle ZIP attachments — extract PDFs from inside
        if name_lower.endswith(".zip"):
            log_action(f"  ZIP attachment found: {name} ({len(content)} bytes)")
            extracted = _extract_pdfs_from_zip(content, output_dir)
            downloaded.extend(extracted)
            continue

        # Handle direct PDF attachments — strip browser dup suffixes like " (1).pdf"
        safe_name = _strip_dup_suffix(name.replace("/", "_").replace("\\", "_"))
        dest = os.path.join(output_dir, safe_name)
        if os.path.exists(dest):
            log_action(f"  Already exists, skipping: {safe_name}")
            continue

        with open(dest, "wb") as f:
            f.write(content)

        downloaded.append(safe_name)
        log_action(f"  Downloaded: {safe_name} ({len(content)} bytes)")

    return downloaded


# --- Run (importable by run_loop.py) ---

def run(since_timestamp=None, known_email_ids=None, headless=False):
    """Fetch new invoice PDFs from Outlook.

    Args:
        since_timestamp: ISO timestamp string. Only fetch emails received after
            this time. Overrides config search_period.from.
        known_email_ids: set of Graph message IDs to skip (already processed).
        headless: If True, don't attempt browser auth (for scheduled runs).

    Returns:
        dict: {
            "check_timestamp": str,        # ISO timestamp of this check
            "new_email_ids": list[str],    # Graph message IDs processed this run
            "downloaded_count": int,
            "skipped_count": int,
        }
    """
    log_action("=" * 50)
    log_action("Step 1: Fetch Invoice PDFs from Outlook")
    log_action("=" * 50)

    check_timestamp = datetime.now().isoformat()
    config = load_outlook_config()

    output_dir = os.path.join(PROJECT_ROOT, "input_pdfs", "mail invoices")
    os.makedirs(output_dir, exist_ok=True)

    token = get_access_token(config, headless=headless)
    log_action("Authenticated to Microsoft Graph")

    messages = fetch_invoice_emails(token, config, since_override=since_timestamp)

    if known_email_ids is None:
        known_email_ids = set()

    total_downloaded = 0
    skipped_count = 0
    new_email_ids = []

    for msg in messages:
        msg_id = msg.get("id")

        # Skip emails already processed in previous runs
        if msg_id and msg_id in known_email_ids:
            skipped_count += 1
            continue

        subject = msg.get("subject", "(no subject)")
        sender = msg.get("from", {}).get("emailAddress", {}).get("address", "unknown")
        log_action(f"Processing: '{subject}' from {sender}")

        downloaded = download_pdf_attachments(token, msg, output_dir)
        total_downloaded += len(downloaded)

        if msg_id:
            new_email_ids.append(msg_id)

    log_action(f"Done. Downloaded {total_downloaded} new PDFs to {output_dir} (skipped {skipped_count} known emails)")

    return {
        "check_timestamp": check_timestamp,
        "new_email_ids": new_email_ids,
        "downloaded_count": total_downloaded,
        "skipped_count": skipped_count,
    }


# --- Main ---

def main():
    run()


if __name__ == "__main__":
    main()
