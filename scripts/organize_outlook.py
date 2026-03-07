"""
Utility: Organize Outlook Inbox

After downloading invoice PDFs (Step 1), this script moves the
processed emails from Inbox to a designated folder (e.g., "Processed Invoices").

- Creates the target folder if it doesn't exist
- Only moves emails that have PDF attachments
- Skips emails already in the target folder

Usage:
    python scripts/organize_outlook.py
    python scripts/organize_outlook.py --folder "Invoices/Done"
    python scripts/organize_outlook.py --dry-run
"""

import os
import json
import argparse
import time
import requests
from utils import PROJECT_ROOT, log_action

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "outlook_config.json")
TOKEN_PATH = os.path.join(PROJECT_ROOT, "config", "outlook_token.json")

DEFAULT_FOLDER = "Processed Invoices"


def load_outlook_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def get_access_token(config):
    """Get a valid access token. Uses cached token or launches browser auth."""
    outlook = config["outlook"]
    tenant_id = outlook["tenant_id"]

    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r") as f:
            token_data = json.load(f)
        obtained_at = token_data.get("obtained_at", 0)
        expires_in = token_data.get("expires_in", 0)
        if time.time() < obtained_at + expires_in - 300:
            return token_data["access_token"]
        log_action("Cached token expired, re-authenticating...")

    import urllib.parse
    import webbrowser
    import http.server
    import threading

    client_id = outlook["client_id"]
    client_secret = outlook["client_secret"]
    redirect_uri = outlook.get("redirect_uri", "http://localhost:8080/callback")
    scopes = " ".join(outlook.get("scopes", ["Mail.Read", "User.Read"]))

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

        def log_message(self, *args):
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

    token_out = {
        "token_type": result.get("token_type", "Bearer"),
        "scope": result.get("scope", ""),
        "expires_in": result.get("expires_in", 3600),
        "access_token": result["access_token"],
        "obtained_at": time.time(),
    }
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_out, f, indent=2)

    log_action("New token obtained and cached")
    return result["access_token"]


def get_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --- Folder Management ---

def get_or_create_folder(token, folder_name):
    """Get folder ID by name, creating it (and parent folders) if needed."""
    headers = get_headers(token)
    parts = [p.strip() for p in folder_name.split("/") if p.strip()]

    folder_id = None

    for part in parts:
        if folder_id:
            search_url = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/childFolders"
        else:
            search_url = f"{GRAPH_BASE}/me/mailFolders"

        resp = requests.get(search_url, headers=headers, params={
            "$filter": f"displayName eq '{part}'",
        })
        resp.raise_for_status()
        folders = resp.json().get("value", [])

        if folders:
            folder_id = folders[0]["id"]
            log_action(f"  Found folder: {part}")
        else:
            if folder_id:
                create_url = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/childFolders"
            else:
                create_url = f"{GRAPH_BASE}/me/mailFolders"

            resp = requests.post(create_url, headers=headers, json={
                "displayName": part,
            })
            resp.raise_for_status()
            folder_id = resp.json()["id"]
            log_action(f"  Created folder: {part}")

    return folder_id


# --- Email Operations ---

def get_emails_with_pdfs(token, config):
    """Fetch emails from Inbox that have PDF attachments."""
    headers = get_headers(token)
    search_period = config.get("search_period", {})
    date_from = search_period.get("from", "2025-01-01")
    date_to = search_period.get("to", "2026-12-31")

    url = f"{GRAPH_BASE}/me/mailFolders/Inbox/messages"
    filter_str = f"hasAttachments eq true and receivedDateTime ge {date_from}T00:00:00Z and receivedDateTime le {date_to}T23:59:59Z"
    # Build OData params in URL directly to avoid double-encoding
    odata = f"$filter={filter_str}&$select=id,subject,receivedDateTime,from&$top=100"
    url = f"{url}?{odata}"

    all_messages = []
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    all_messages.extend(data.get("value", []))

    while "@odata.nextLink" in data:
        resp = requests.get(data["@odata.nextLink"], headers=headers)
        resp.raise_for_status()
        data = resp.json()
        all_messages.extend(data.get("value", []))

    # Filter: only keep emails that actually have .pdf attachments
    pdf_emails = []
    for msg in all_messages:
        att_url = f"{GRAPH_BASE}/me/messages/{msg['id']}/attachments"
        att_resp = requests.get(att_url, headers=headers, params={"$select": "name"})
        att_resp.raise_for_status()
        attachments = att_resp.json().get("value", [])
        has_pdf = any(a.get("name", "").lower().endswith(".pdf") for a in attachments)
        if has_pdf:
            pdf_emails.append(msg)

    return pdf_emails


def move_email(token, message_id, destination_folder_id):
    """Move an email to the destination folder."""
    headers = get_headers(token)
    resp = requests.post(
        f"{GRAPH_BASE}/me/messages/{message_id}/move",
        headers=headers,
        json={"destinationId": destination_folder_id},
    )
    resp.raise_for_status()
    return resp.json()


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Move processed invoice emails to a folder")
    parser.add_argument("--folder", default=DEFAULT_FOLDER,
                        help=f"Target folder name (default: '{DEFAULT_FOLDER}'). Supports nesting like 'Invoices/Done'")
    parser.add_argument("--dry-run", action="store_true", help="List emails without moving")
    args = parser.parse_args()

    log_action("=" * 50)
    log_action(f"Organize Outlook: move invoice emails -> '{args.folder}'")
    log_action("=" * 50)

    config = load_outlook_config()
    token = get_access_token(config)
    log_action("Authenticated to Microsoft Graph")

    # Get/create target folder
    target_folder_id = get_or_create_folder(token, args.folder)
    log_action(f"Target folder ready: {args.folder}")

    # Get inbox emails with PDFs
    emails = get_emails_with_pdfs(token, config)
    log_action(f"Found {len(emails)} emails with PDF attachments in Inbox")

    if not emails:
        log_action("Nothing to move.")
        return

    moved = 0
    for msg in emails:
        subject = msg.get("subject", "(no subject)")
        sender = msg.get("from", {}).get("emailAddress", {}).get("address", "unknown")
        date = msg.get("receivedDateTime", "")[:10]

        if args.dry_run:
            log_action(f"  [DRY RUN] {date} | {sender} | {subject}")
            continue

        try:
            move_email(token, msg["id"], target_folder_id)
            moved += 1
            log_action(f"  Moved: {date} | {sender} | {subject}")
        except Exception as e:
            log_action(f"  Failed to move '{subject}': {e}", "WARNING")

    if args.dry_run:
        log_action(f"Dry run complete. {len(emails)} emails would be moved.")
    else:
        log_action(f"Done. Moved {moved}/{len(emails)} emails to '{args.folder}'")


if __name__ == "__main__":
    main()
