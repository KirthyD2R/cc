"""
Shared utilities for CC Statement Automation.

Provides: Zoho Books API client, config loading, logging, date/amount helpers.
"""

import json
import os
import sys
import webbrowser
import requests
import time
from datetime import datetime, timedelta
from fuzzywuzzy import fuzz

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- Config ---

def load_config(config_path="config/zoho_config.json"):
    with open(os.path.join(PROJECT_ROOT, config_path), "r") as f:
        return json.load(f)


def load_vendor_mappings(path="config/vendor_mappings.json"):
    with open(os.path.join(PROJECT_ROOT, path), "r") as f:
        return json.load(f)


def load_learned_vendor_mappings(path="config/learned_vendor_mappings.json"):
    full_path = os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(full_path):
        return {"mappings": {}}
    with open(full_path, "r") as f:
        return json.load(f)


def save_learned_vendor_mapping(cc_description, vendor_name, path="config/learned_vendor_mappings.json"):
    """Save a CC description → vendor name mapping learned from user confirmation."""
    full_path = os.path.join(PROJECT_ROOT, path)
    data = load_learned_vendor_mappings(path)
    # Normalize key: strip, uppercase
    key = cc_description.strip().upper()
    if not key or not vendor_name:
        return
    data["mappings"][key] = vendor_name.strip()
    with open(full_path, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


# --- Forex rate utilities ---

def load_forex_cache(path="config/forex_cache.json"):
    full_path = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(full_path):
        return {}
    try:
        with open(full_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_forex_cache(cache, path="config/forex_cache.json"):
    full_path = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
    with open(full_path, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def fetch_forex_rate(date_str, from_cur="USD", to_cur="INR", cache=None):
    cache_key = f"{from_cur}_{to_cur}"
    if cache and date_str in cache and cache_key in cache[date_str]:
        return cache[date_str][cache_key]
    try:
        import urllib.request
        url = f"https://api.frankfurter.app/{date_str}?from={from_cur}&to={to_cur}"
        req = urllib.request.Request(url, headers={"User-Agent": "cc-automation/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        rate = data.get("rates", {}).get(to_cur)
        if rate and cache is not None:
            if date_str not in cache:
                cache[date_str] = {}
            cache[date_str][cache_key] = rate
        return rate
    except Exception:
        return None


def prefetch_forex_rates(dates, from_cur="USD", to_cur="INR"):
    cache = load_forex_cache()
    cache_key = f"{from_cur}_{to_cur}"
    missing = [d for d in set(dates) if d and (d not in cache or cache_key not in cache.get(d, {}))]
    if missing:
        for date_str in sorted(missing):
            fetch_forex_rate(date_str, from_cur, to_cur, cache)
        save_forex_cache(cache)
    return cache


# --- Zoho OAuth2 ---

class ZohoAuth:
    # Issue #5: Derive token URL from region in config (default: .in for India)
    REGION_DOMAINS = {
        "in": "accounts.zoho.in",
        "com": "accounts.zoho.com",
        "eu": "accounts.zoho.eu",
        "com.au": "accounts.zoho.com.au",
        "jp": "accounts.zoho.jp",
    }

    def __init__(self, config):
        self.config = config["zoho_books"]
        self.access_token = None
        self.token_expiry = None
        # Derive token URL from base_url region or explicit region config
        region = self.config.get("region", "in")
        domain = self.REGION_DOMAINS.get(region, "accounts.zoho.in")
        self.token_url = f"https://{domain}/oauth/v2/token"

    def get_access_token(self):
        if self.access_token and self.token_expiry and datetime.now() < self.token_expiry:
            return self.access_token

        # Retry token refresh with backoff for transient errors (503, SSL, connection)
        last_exc = None
        for attempt in range(3):
            try:
                resp = requests.post(self.token_url, data={
                    "refresh_token": self.config["refresh_token"],
                    "client_id": self.config["client_id"],
                    "client_secret": self.config["client_secret"],
                    "grant_type": "refresh_token",
                }, timeout=15)

                if resp.status_code == 400:
                    # Refresh token expired — try auto-renewal
                    return self._auto_renew_refresh_token()

                if resp.status_code in (502, 503, 504):
                    wait = 3 * (attempt + 1)
                    log_action(f"  Token refresh got {resp.status_code}, retrying in {wait}s ({attempt+1}/3)")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.SSLError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                wait = 3 * (attempt + 1)
                log_action(f"  Token refresh connection error, retrying in {wait}s ({attempt+1}/3): {e}")
                time.sleep(wait)
                continue
        else:
            if last_exc:
                raise last_exc
            resp.raise_for_status()
        data = resp.json()

        self.access_token = data["access_token"]
        self.token_expiry = datetime.now() + timedelta(
            seconds=data.get("expires_in", 3600) - 300
        )
        return self.access_token

    def _auto_renew_refresh_token(self):
        """Auto-renew expired refresh token via self_client code or browser prompt."""
        print("\n" + "=" * 60)
        print("  ZOHO TOKEN EXPIRED - Auto-renewal")
        print("=" * 60)

        # Step 1: Try using existing code from self_client.json
        self_client_path = os.path.join(PROJECT_ROOT, "config", "self_client.json")
        code = None

        if os.path.exists(self_client_path):
            with open(self_client_path, "r") as f:
                sc = json.load(f)
            code = sc.get("code")
            if code:
                print(f"  Trying existing code from self_client.json...")
                new_token = self._exchange_code_for_token(code)
                if new_token:
                    return new_token
                print(f"  Existing code expired.")

        # Step 2: Open browser for user to generate new code
        print("\n  Opening Zoho API Console in browser...")
        print("  Steps:")
        print("    1. Click 'Self Client' in the API Console")
        print("    2. Scope: ZohoBooks.fullaccess.all")
        print("    3. Duration: 10 minutes -> Click 'Generate'")
        print("    4. Copy the generated code")
        print()

        try:
            webbrowser.open("https://api-console.zoho.in/")
        except Exception:
            print("  Could not open browser. Go to: https://api-console.zoho.in/")

        # Step 3: Prompt user for the new code
        code = input("  Paste the new code here: ").strip()
        if not code:
            raise Exception("No code provided. Cannot renew token.")

        # Save code to self_client.json for reference
        if os.path.exists(self_client_path):
            with open(self_client_path, "r") as f:
                sc = json.load(f)
            sc["code"] = code
            with open(self_client_path, "w") as f:
                json.dump(sc, f, indent=2)

        new_token = self._exchange_code_for_token(code)
        if new_token:
            return new_token

        raise Exception("Failed to renew token. Check the code and try again.")

    def _exchange_code_for_token(self, code):
        """Exchange authorization code for refresh_token + access_token."""
        resp = requests.post(self.token_url, data={
            "code": code,
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "grant_type": "authorization_code",
        })

        if not resp.ok:
            return None

        data = resp.json()
        if "error" in data:
            return None

        refresh_token = data.get("refresh_token")
        access_token = data.get("access_token")

        if not refresh_token or not access_token:
            return None

        # Auto-update zoho_config.json with new refresh_token
        config_path = os.path.join(PROJECT_ROOT, "config", "zoho_config.json")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                full_config = json.load(f)
            full_config["zoho_books"]["refresh_token"] = refresh_token
            with open(config_path, "w") as f:
                json.dump(full_config, f, indent=4)
            print(f"  Updated config/zoho_config.json with new refresh_token")

        # Also update in-memory config
        self.config["refresh_token"] = refresh_token

        # Save tokens for reference
        tokens_path = os.path.join(PROJECT_ROOT, "config", "tokens.json")
        with open(tokens_path, "w") as f:
            json.dump({
                "refresh_token": refresh_token,
                "access_token": access_token,
                "updated_at": datetime.now().isoformat(),
            }, f, indent=2)

        self.access_token = access_token
        self.token_expiry = datetime.now() + timedelta(
            seconds=data.get("expires_in", 3600) - 300
        )

        print(f"  Token renewed successfully!")
        print("=" * 60 + "\n")
        return self.access_token

    def get_headers(self):
        return {
            "Authorization": f"Zoho-oauthtoken {self.get_access_token()}",
            "Content-Type": "application/json",
            "X-com-zoho-books-organizationid": self.config["organization_id"],
        }


# --- Zoho Books API ---

class ZohoBooksAPI:
    _RETRY_MAX = 3  # Max retries for 429/503/transient errors
    _MIN_INTERVAL = 1.0  # 1 second between API calls to avoid rate limits

    def __init__(self, config):
        self.config = config["zoho_books"]
        self.auth = ZohoAuth(config)
        self.base_url = self.config["base_url"]
        self.org_id = self.config["organization_id"]
        self._last_request_time = 0

    def _throttle(self):
        """Enforce minimum interval between API calls to avoid rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._MIN_INTERVAL:
            time.sleep(self._MIN_INTERVAL - elapsed)

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}/{endpoint}"
        params = kwargs.pop("params", {})
        params["organization_id"] = self.org_id
        params.update(kwargs.pop("extra_params", {}))

        self._throttle()

        last_exc = None
        for attempt in range(self._RETRY_MAX):
            try:
                resp = requests.request(
                    method, url, headers=self.auth.get_headers(), params=params,
                    timeout=30, **kwargs
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.SSLError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                wait = 3 * (attempt + 1)
                log_action(f"  Connection error on {method} {endpoint}, retrying in {wait}s ({attempt+1}/{self._RETRY_MAX}): {type(e).__name__}")
                time.sleep(wait)
                continue
            self._last_request_time = time.time()

            if resp.status_code == 429:
                wait = min(5 * 2 ** attempt, 60)
                try:
                    wait = int(resp.headers.get("Retry-After", wait))
                except (ValueError, TypeError):
                    pass
                log_action(f"  Rate limited (429), retrying in {wait}s ({attempt+1}/{self._RETRY_MAX})")
                time.sleep(wait)
                continue

            if resp.status_code in (502, 503, 504):
                wait = 3 * (attempt + 1)
                log_action(f"  Server error ({resp.status_code}) on {method} {endpoint}, retrying in {wait}s ({attempt+1}/{self._RETRY_MAX})")
                time.sleep(wait)
                continue

            break
        else:
            if last_exc:
                raise last_exc

        if not resp.ok:
            try:
                err_body = resp.json().get("message", resp.text[:500])
            except Exception:
                err_body = resp.text[:500]
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for {method} {endpoint}: {err_body}",
                response=resp,
            )
        return resp.json()

    def _upload(self, endpoint, file_path, file_field="attachment", extra_data=None):
        """Multipart file upload (no Content-Type header — requests sets it)."""
        url = f"{self.base_url}/{endpoint}"
        params = {"organization_id": self.org_id}
        headers = {"Authorization": f"Zoho-oauthtoken {self.auth.get_access_token()}"}

        self._throttle()

        last_exc = None
        for attempt in range(self._RETRY_MAX):
            try:
                with open(file_path, "rb") as f:
                    import mimetypes
                    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
                    files = {file_field: (os.path.basename(file_path), f, mime_type)}
                    resp = requests.post(
                        url, headers=headers, params=params,
                        files=files, data=extra_data or {},
                        timeout=60,
                    )
            except (requests.exceptions.ConnectionError, requests.exceptions.SSLError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                wait = 3 * (attempt + 1)
                log_action(f"  Upload connection error, retrying in {wait}s ({attempt+1}/{self._RETRY_MAX}): {type(e).__name__}")
                time.sleep(wait)
                continue
            self._last_request_time = time.time()

            if resp.status_code == 429:
                wait = min(5 * 2 ** attempt, 60)
                try:
                    wait = int(resp.headers.get("Retry-After", wait))
                except (ValueError, TypeError):
                    pass
                log_action(f"  Rate limited (429), retrying in {wait}s ({attempt+1}/{self._RETRY_MAX})")
                time.sleep(wait)
                continue

            if resp.status_code in (502, 503, 504):
                wait = 3 * (attempt + 1)
                log_action(f"  Server error ({resp.status_code}) on upload, retrying in {wait}s ({attempt+1}/{self._RETRY_MAX})")
                time.sleep(wait)
                continue

            break
        else:
            if last_exc:
                raise last_exc

        if not resp.ok:
            try:
                err_body = resp.json().get("message", resp.text[:500])
            except Exception:
                err_body = resp.text[:500]
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for upload {endpoint}: {err_body}",
                response=resp,
            )
        return resp.json()

    # -- Vendors --
    def find_vendor(self, name):
        result = self._request("GET", "contacts", params={
            "contact_type": "vendor", "contact_name": name,
        })
        for c in result.get("contacts", []):
            if c["contact_name"].lower() == name.lower():
                return c["contact_id"]
        return None

    def find_vendor_by_gstin(self, gstin):
        """Search for a vendor by GST number. Returns (contact_id, contact_name) or (None, None)."""
        result = self._request("GET", "contacts", params={
            "contact_type": "vendor", "search_text": gstin,
        })
        for c in result.get("contacts", []):
            if c.get("gst_no") == gstin:
                return c["contact_id"], c.get("contact_name", "")
        return None, None

    def create_vendor(self, vendor_data):
        return self._request("POST", "contacts", json=vendor_data)

    def list_vendors(self):
        return self._request("GET", "contacts", params={"contact_type": "vendor"})

    # -- Bills --
    def create_bill(self, bill_data):
        return self._request("POST", "bills", json=bill_data)

    def get_bill(self, bill_id):
        return self._request("GET", f"bills/{bill_id}")

    def delete_bill(self, bill_id):
        return self._request("DELETE", f"bills/{bill_id}")

    def list_bills(self, status=None, page=1, bill_number=None, search_text=None):
        params = {"page": page}
        if status:
            params["status"] = status
        if bill_number:
            params["bill_number"] = bill_number
        if search_text:
            params["search_text"] = search_text
        return self._request("GET", "bills", params=params)

    def attach_to_bill(self, bill_id, pdf_path):
        return self._upload(f"bills/{bill_id}/attachment", pdf_path)

    # -- Vendor Payments --
    def record_vendor_payment(self, payment_data):
        return self._request("POST", "vendorpayments", json=payment_data)

    def list_vendor_payments(self, page=1):
        return self._request("GET", "vendorpayments", params={"page": page})

    def delete_vendor_payment(self, payment_id):
        return self._request("DELETE", f"vendorpayments/{payment_id}")

    # -- Contacts (Vendors) --
    def update_vendor(self, contact_id, vendor_data):
        return self._request("PUT", f"contacts/{contact_id}", json=vendor_data)

    def delete_vendor(self, contact_id):
        return self._request("DELETE", f"contacts/{contact_id}")

    def list_all_vendors(self, page=1):
        return self._request("GET", "contacts", params={
            "contact_type": "vendor", "page": page,
        })

    # -- Banking --
    def list_bank_transactions(self, account_id, page=1):
        """List all bank transactions for an account (all statuses)."""
        return self._request("GET", "banktransactions", params={
            "account_id": account_id, "page": page,
        })

    def list_uncategorized(self, account_id, page=1):
        return self._request("GET", "banktransactions", params={
            "account_id": account_id, "status": "uncategorized", "page": page,
        })

    def delete_bank_transaction(self, transaction_id):
        return self._request("DELETE", f"banktransactions/{transaction_id}")

    def unmatch_transaction(self, transaction_id):
        return self._request("POST", f"banktransactions/uncategorized/{transaction_id}/unmatch")

    def uncategorize_transaction(self, transaction_id):
        return self._request("POST", f"banktransactions/{transaction_id}/uncategorize")

    def delete_statement(self, statement_id):
        return self._request("DELETE", f"bankstatements/{statement_id}")

    def get_matching_transactions(self, transaction_id):
        return self._request(
            "GET", f"banktransactions/uncategorized/{transaction_id}/match"
        )

    def match_transaction(self, transaction_id, matches):
        """matches: list of {"transaction_id": ..., "transaction_type": ...}"""
        return self._request(
            "POST", f"banktransactions/uncategorized/{transaction_id}/match",
            json={"transactions_to_be_matched": matches},
        )

    def categorize_as_vendor_payment(self, transaction_id, vendor_id, bill_id, amount, date):
        """Categorize an uncategorized banking transaction as a vendor payment.

        This creates a new vendor payment from the banking transaction and
        applies it to the specified bill. Used when direct match fails due to
        amount mismatch (Zoho's '+ Create New Transaction' flow).
        """
        return self._request(
            "POST",
            f"banktransactions/uncategorized/{transaction_id}/categorize/vendorpayments",
            json={
                "vendor_id": vendor_id,
                "date": date,
                "amount": amount,
                "bills": [
                    {
                        "bill_id": bill_id,
                        "amount_applied": amount,
                    }
                ],
            },
        )

    def import_statement(self, file_path, account_id, column_mapping):
        """Import a CC statement CSV into Zoho Banking."""
        import json as _json
        json_string = _json.dumps({
            "account_id": account_id,
            "start_date": "", "end_date": "",
            "date_format": "yyyy-MM-dd",
            "column_mapping": column_mapping,
        })
        url = f"{self.base_url}/bankstatements"
        params = {"organization_id": self.org_id}
        headers = {"Authorization": f"Zoho-oauthtoken {self.auth.get_access_token()}"}

        self._throttle()

        last_exc = None
        for attempt in range(self._RETRY_MAX):
            try:
                with open(file_path, "rb") as f:
                    files = {"statement": (os.path.basename(file_path), f, "text/csv")}
                    resp = requests.post(
                        url, headers=headers, params=params,
                        files=files, data={"JSONString": json_string},
                        timeout=60,
                    )
            except (requests.exceptions.ConnectionError, requests.exceptions.SSLError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                wait = 3 * (attempt + 1)
                log_action(f"  Statement upload connection error, retrying in {wait}s ({attempt+1}/{self._RETRY_MAX}): {type(e).__name__}")
                time.sleep(wait)
                continue
            self._last_request_time = time.time()

            if resp.status_code == 429:
                wait = min(5 * 2 ** attempt, 60)
                try:
                    wait = int(resp.headers.get("Retry-After", wait))
                except (ValueError, TypeError):
                    pass
                log_action(f"  Rate limited (429), retrying in {wait}s ({attempt+1}/{self._RETRY_MAX})")
                time.sleep(wait)
                continue

            if resp.status_code in (502, 503, 504):
                wait = 3 * (attempt + 1)
                log_action(f"  Server error ({resp.status_code}) on statement upload, retrying in {wait}s ({attempt+1}/{self._RETRY_MAX})")
                time.sleep(wait)
                continue

            break
        else:
            if last_exc:
                raise last_exc

        if not resp.ok:
            try:
                err_body = resp.json().get("message", resp.text[:500])
            except Exception:
                err_body = resp.text[:500]
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for upload bankstatements: {err_body}",
                response=resp,
            )
        return resp.json()

    # -- Organizations --
    def list_organizations(self):
        """Fetch all organizations accessible to this Zoho account."""
        result = self._request("GET", "organizations")
        return result.get("organizations", [])

    # -- Bank Accounts --
    def list_bank_accounts(self):
        """Fetch all bank accounts from Zoho and return the list."""
        result = self._request("GET", "bankaccounts")
        return result.get("bankaccounts", [])

    def create_bank_account(self, account_name, account_type="credit_card", currency_code="INR", account_number=""):
        """Create a bank/CC account in Zoho Books Banking."""
        data = {
            "account_name": account_name,
            "account_type": account_type,
        }
        if account_number:
            data["account_number"] = account_number
        if currency_code:
            data["currency_code"] = currency_code
        return self._request("POST", "bankaccounts", json=data)

    # -- Currencies --
    def list_currencies(self):
        """Fetch all currencies from Zoho and return {currency_code: currency_id} map."""
        result = self._request("GET", "settings/currencies")
        return {
            c["currency_code"]: c["currency_id"]
            for c in result.get("currencies", [])
        }

    # -- Bills (update) --
    def update_bill(self, bill_id, bill_data):
        return self._request("PUT", f"bills/{bill_id}", json=bill_data)

    # -- Chart of Accounts --
    def get_expense_accounts(self):
        result = self._request("GET", "chartofaccounts", params={"account_type": "expense"})
        return {a["account_name"]: a["account_id"] for a in result.get("chartofaccounts", [])}

    def get_all_accounts(self):
        """Fetch all COA accounts (no type filter). Returns {name: {account_id, account_type}}."""
        result = self._request("GET", "chartofaccounts")
        return {
            a["account_name"]: {"account_id": a["account_id"], "account_type": a["account_type"]}
            for a in result.get("chartofaccounts", [])
        }

    def create_account(self, name, account_type, description=""):
        return self._request("POST", "chartofaccounts", json={
            "account_name": name, "account_type": account_type, "description": description,
        })

    def create_expense_account(self, name, description=""):
        return self._request("POST", "chartofaccounts", json={
            "account_name": name, "account_type": "expense", "description": description,
        })

    # -- Taxes --
    def list_taxes(self):
        """Fetch all taxes. Returns list of tax dicts with tax_id, tax_name, tax_percentage etc."""
        result = self._request("GET", "settings/taxes")
        return result.get("taxes", [])

    def list_tax_exemptions(self):
        """Fetch all tax exemptions. Returns list of exemption dicts."""
        result = self._request("GET", "settings/taxexemptions")
        return result.get("tax_exemptions", [])


# --- Bank Account Resolution ---

def resolve_account_ids(api, cards):
    """Resolve zoho_account_id for each card by matching against Zoho bank accounts.

    Matches by last_four_digits in the account name/number.
    If no match found, auto-creates the CC account in Zoho Banking.
    Saves new account IDs back to zoho_config.json.
    """
    bank_accounts = api.list_bank_accounts()
    log_action(f"Fetched {len(bank_accounts)} bank accounts from Zoho")

    config_changed = False

    for card in cards:
        last_four = card.get("last_four_digits", "")
        card_name = card.get("name", "")
        matched = False

        # Collect all matching accounts, prefer active ones
        candidates = []
        for acct in bank_accounts:
            acct_name = acct.get("account_name", "")
            acct_number = acct.get("account_number", "")
            # Match by exact card name first, then by last four digits
            if card_name and acct_name == card_name:
                candidates.insert(0, acct)  # exact name match = highest priority
            elif last_four and (last_four in acct_name or last_four in acct_number):
                candidates.append(acct)

        if candidates:
            # Prefer active accounts over inactive ones
            active = [a for a in candidates if a.get("is_active", True)]
            best = active[0] if active else candidates[0]
            card["zoho_account_id"] = best["account_id"]
            acct_name = best.get("account_name", "")
            log_action(f"  Matched '{card_name}' -> Zoho account '{acct_name}' ({best['account_id']})")
            matched = True
            config_changed = True

        if not matched:
            # Auto-create CC account in Zoho Banking
            log_action(f"  No match for '{card_name}', creating CC account in Zoho...")
            try:
                result = api.create_bank_account(
                    account_name=card_name,
                    account_type="credit_card",
                    account_number=last_four,
                )
                new_acct = result.get("bankaccount", {})
                new_id = new_acct.get("account_id")
                if new_id:
                    card["zoho_account_id"] = new_id
                    config_changed = True
                    log_action(f"  Created CC account: {card_name} ({new_id})")
                else:
                    log_action(f"  CC account created but no ID returned for '{card_name}'", "ERROR")
            except Exception as e:
                log_action(f"  Failed to create CC account for '{card_name}': {e}", "ERROR")
                existing_id = card.get("zoho_account_id")
                if existing_id:
                    log_action(f"  Falling back to config ID: {existing_id}", "WARNING")

    # Save updated IDs back to zoho_config.json
    if config_changed:
        _save_card_ids_to_config(cards)

    return cards


def _save_card_ids_to_config(cards):
    """Write updated zoho_account_id values back to zoho_config.json."""
    config_path = os.path.join(PROJECT_ROOT, "config", "zoho_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        # Update each card's zoho_account_id
        for cfg_card in config.get("credit_cards", []):
            for card in cards:
                if cfg_card.get("name") == card.get("name"):
                    cfg_card["zoho_account_id"] = card.get("zoho_account_id", "")
                    break
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        log_action("  Saved updated CC account IDs to zoho_config.json")
    except Exception as e:
        log_action(f"  Failed to save config: {e}", "WARNING")


# --- Fuzzy Matching ---

VENDOR_STOP_WORDS = {
    "enterprises", "enterprise", "pvt", "private", "limited", "ltd",
    "inc", "incorporated", "llp", "india", "corporation", "corp",
    "co", "company", "services", "solutions", "technologies",
    "technology", "tech", "marketing", "international", "global",
    "group", "associates", "consultants", "consulting", "traders",
    "trading", "industries", "industrial",
}


def strip_vendor_stop_words(name):
    """Remove common business suffixes for better fuzzy comparison.
    Falls back to original name if stripping would leave it empty."""
    tokens = name.strip().upper().split()
    filtered = [t for t in tokens if t.lower() not in VENDOR_STOP_WORDS]
    return " ".join(filtered) if filtered else name.strip().upper()


GATEWAY_KEYWORDS = {
    "cybs", "billdesk", "payu", "razorpay", "ccavenue",
    "paygate", "instamojo", "cashfree", "phonepe", "paytm",
}


def is_gateway_only(description):
    """Check if CC description is a payment gateway with no brand prefix.
    Returns True only when the meaningful tokens are ALL gateway keywords
    (plus location/noise words). Returns False if a brand name is present."""
    if not description:
        return False
    tokens = description.strip().upper().split()
    # Location/noise words to ignore when checking for brand presence
    noise = {"si", "in", "mumbai", "bangalore", "chennai", "delhi",
             "india", "bbps", "cc", "payment", "rate"}
    meaningful = [t.lower() for t in tokens if t.lower() not in noise]
    if not meaningful:
        return False
    # If ALL meaningful tokens are gateway keywords, it's gateway-only
    return all(t in GATEWAY_KEYWORDS for t in meaningful)


def fuzzy_match_vendor(merchant_name, vendor_mappings, threshold=75):
    """Match a CC merchant name to a known vendor using fuzzy string matching.

    Threshold of 75 balances precision (avoiding false matches on short names)
    with recall (catching bank-truncated merchant strings).
    """
    mappings = vendor_mappings.get("mappings", {})
    merchant_upper = merchant_name.strip().upper()

    for key, value in mappings.items():
        if key.upper() == merchant_upper:
            return value, 100

    best_match, best_score, best_key = None, 0, None
    for key, value in mappings.items():
        score = fuzz.token_set_ratio(
            strip_vendor_stop_words(merchant_name),
            strip_vendor_stop_words(key),
        )
        if score > best_score:
            best_score, best_match, best_key = score, value, key

    if best_score >= threshold:
        return best_match, best_score

    # Issue #15: Log near-misses (within 15 points of threshold) for tuning
    if best_score >= threshold - 15 and best_match:
        log_action(
            f"  Near-miss fuzzy match: '{merchant_name}' ~ '{best_key}' -> '{best_match}' "
            f"(score: {best_score}, threshold: {threshold})",
            "WARNING",
        )

    return None, 0


# --- Helpers ---

def parse_date(date_str, formats=None):
    if not date_str or not date_str.strip():
        return None
    if formats is None:
        formats = [
            "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
            "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
            "%d-%b-%Y", "%d-%B-%Y",
            "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
        ]
    # Normalize extra whitespace (e.g., "May 1 , 2025" → "May 1, 2025")
    import re as _re
    cleaned = _re.sub(r'\s+', ' ', date_str.strip())
    cleaned = _re.sub(r'\s+,', ',', cleaned)
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Issue #31: Log warning when date parsing fails
    log_action(f"  Could not parse date: '{date_str}'", "WARNING")
    return None


def format_amount(amount_str):
    if isinstance(amount_str, (int, float)):
        return float(amount_str)
    cleaned = amount_str.replace("\u20b9", "").replace("INR", "").replace("$", "").replace("€", "").strip()
    is_credit = "CR" in cleaned.upper()
    cleaned = cleaned.replace("CR", "").replace("Cr", "").replace("DR", "").replace("Dr", "").strip()

    # Issue #17: Detect European format (1.234,56) vs Indian/US format (1,234.56)
    # European: last separator is comma, dots are thousand separators
    # Indian/US: last separator is dot, commas are thousand separators
    import re as _re
    if _re.search(r'\d\.\d{3},\d{2}$', cleaned):
        # European format: 1.234,56 -> swap separators
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        # Indian/US format: strip commas
        cleaned = cleaned.replace(",", "")

    amount = float(cleaned)
    return -amount if is_credit else amount


# --- Logging ---

_MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
_log_subscribers = []  # list of queue.Queue objects for live log broadcast

def log_action(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] {message}"
    try:
        print(log_line)
    except (OSError, UnicodeEncodeError):
        # Windows pipe/console can't handle some chars — print ASCII-safe version
        print(log_line.encode("ascii", errors="replace").decode("ascii"))
    log_path = os.path.join(PROJECT_ROOT, "output", "automation.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # Issue #36: Rotate log if it exceeds max size
    if os.path.exists(log_path) and os.path.getsize(log_path) > _MAX_LOG_SIZE:
        rotated = log_path + ".old"
        if os.path.exists(rotated):
            os.remove(rotated)
        os.rename(log_path, rotated)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")
    # Broadcast to any SSE subscribers (non-breaking: no-op when list is empty)
    for q in _log_subscribers:
        try:
            q.put_nowait(log_line)
        except Exception:
            pass


# --- Vendor Categorization ---

class VendorCategorizer:
    """Categorizes vendors into COA accounts.

    Resolution order (optimized):
    1. Check cache in vendor_mappings.json["account_mappings"]
    2. Check KNOWN_VENDOR_CATEGORIES hard-coded map
    3. Fuzzy match vendor name against existing Zoho COA account names
    4. Classify from invoice text (raw_text_preview from PDF)
    5. Web search via DuckDuckGo (last resort)
    6. Fallback to "general"
    """

    # Hard-coded vendor → category for all known vendors (zero API calls)
    KNOWN_VENDOR_CATEGORIES = {
        "Amazon Web Services": "software_licenses",
        "Atlassian": "saas_devtools",
        "GitHub": "software_licenses",
        "Windsurf": "saas_devtools",
        "Google": "saas_productivity",
        "Microsoft": "software_licenses",
        "Zoho": "software_subscriptions",
        "New Relic": "saas_monitoring",
        "Anthropic": "ai_ml_services",
        "Anthropic (Claude AI)": "ai_ml_services",
        "Groq Inc": "ai_ml_services",
        "Hyperbrowser AI": "ai_ml_services",
        "Vercel": "hosting_deployment",
        "Info Edge (Naukri)": "recruitment",
        "NSTP": "legal_compliance",
        "Netflix": "entertainment",
        "Zepto": "food_beverages",
        "Madurai Sri Balaji Bhavan": "food_beverages",
        "Sanmith Pure Vegetarian": "food_beverages",
        "Kites": "food_beverages",
        "Krishnamachari Srikkan": "food_beverages",
        "Chandrasekar Harshavar": "food_beverages",
        "HDFC Bank - FCY Charges": "bank_charges",
        "Fuel Surcharge Waiver": "bank_charges",
        "HDFC Bank - GST": "taxes_gst",
        "HDFC Bank - CC Payment": "bank_charges",
        "BillDesk - CC Payment": "payment_processing",
        "Amazon India": "ecommerce",
        "Gamma": "ai_ml_services",
        "Wispr Flow": "ai_ml_services",
    }

    # Keyword rules for classifying from invoice text AND web search text
    KEYWORD_RULES = [
        # Cloud infrastructure
        (["cloud computing", "cloud infrastructure", "aws", "cloud services", "iaas", "paas", "cloud platform"], "cloud_infrastructure"),
        # Dev tools / SaaS
        (["developer tools", "software development", "devops", "ci/cd", "code editor", "ide", "version control", "git", "repository", "jira", "confluence"], "saas_devtools"),
        # Productivity SaaS
        (["productivity", "office suite", "email", "calendar", "workspace", "collaboration", "business email"], "saas_productivity"),
        # Monitoring
        (["monitoring", "observability", "apm", "logging", "metrics", "telemetry", "application performance"], "saas_monitoring"),
        # AI / ML
        (["artificial intelligence", "machine learning", "ai model", "llm", "large language model", "neural", "deep learning", "generative ai", "ai safety"], "ai_ml_services"),
        # Hosting / deployment
        (["web hosting", "deployment", "serverless", "cdn", "edge network", "frontend cloud", "static site"], "hosting_deployment"),
        # Recruitment
        (["recruitment", "hiring", "job portal", "job board", "staffing", "naukri", "career", "employment"], "recruitment"),
        # Office / co-working
        (["coworking", "co-working", "office space", "office rent", "incubator", "tech park", "technology park"], "office_rent"),
        # Entertainment
        (["streaming", "entertainment", "video on demand", "ott", "movies", "television"], "entertainment"),
        # Food & beverages
        (["food", "restaurant", "dining", "catering", "grocery", "meal", "beverages", "vegetarian", "bhavan", "kitchen"], "food_beverages"),
        # Bank charges
        (["bank charge", "bank fee", "finance charge", "surcharge", "foreign currency", "forex", "fcy", "markup fee"], "bank_charges"),
        # GST / taxes
        (["gst", "goods and services tax", "tax payment", "cgst", "sgst", "igst", "tax invoice"], "taxes_gst"),
        # Payment processing
        (["payment gateway", "payment processing", "bill payment", "billdesk", "payment aggregator"], "payment_processing"),
        # E-commerce
        (["ecommerce", "e-commerce", "online shopping", "marketplace", "retail", "amazon pay"], "ecommerce"),
        # Legal & compliance (government, legal services, regulatory)
        (["government", "govt", "ministry", "authority", "tribunal", "court", "legal", "advocate", "lawyer",
          "solicitor", "compliance", "regulatory", "license", "permit", "registration", "stamp duty",
          "notary", "affidavit", "roc", "mca", "registrar of companies"], "legal_compliance"),
        # Consulting & professional services
        (["consulting", "consultancy", "advisory", "professional services", "chartered accountant",
          "auditor", "audit", "accounting firm", "tax consultant", "company secretary"], "consulting"),
        # Travel & conveyance
        (["travel", "flight", "airline", "hotel", "cab", "taxi", "uber", "ola", "accommodation",
          "booking", "makemytrip", "goibibo", "cleartrip", "conveyance"], "travel"),
        # Telephone & internet
        (["telecom", "telephone", "internet", "broadband", "isp", "airtel", "jio", "vodafone",
          "bsnl", "mobile", "fiber", "wifi", "data plan"], "telecommunications"),
        # Insurance
        (["insurance", "premium", "life insurance", "health insurance", "general insurance",
          "policy", "lic", "hdfc ergo", "icici lombard"], "insurance"),
        # Utilities (electricity, water, etc.)
        (["electricity", "power", "utility", "water", "gas", "municipal", "corporation", "bescom",
          "tata power", "adani electricity"], "utilities"),
        # Training & education
        (["training", "course", "certification", "education", "learning", "workshop", "seminar",
          "conference", "webinar", "udemy", "coursera"], "training"),
    ]

    # Category → COA account name and account type
    CATEGORY_TO_ACCOUNT = {
        "cloud_infrastructure": ("Cloud Infrastructure & Hosting", "expense"),
        "software_license": ("Software License", "expense"),
        "software_subscriptions": ("Software Subscriptions", "expense"),
        "saas_devtools": ("Software Subscriptions - Dev Tools", "expense"),
        "saas_productivity": ("Software Subscriptions - Productivity", "expense"),
        "saas_monitoring": ("Software Subscriptions - Monitoring", "expense"),
        "ai_ml_services": ("AI & Machine Learning Services", "expense"),
        "hosting_deployment": ("Web Hosting & Deployment", "expense"),
        "recruitment": ("Recruitment & Hiring", "expense"),
        "office_rent": ("Office Rent & Co-working", "expense"),
        "entertainment": ("Entertainment Subscriptions", "expense"),
        "food_beverages": ("Food & Beverages", "expense"),
        "bank_charges": ("Bank Charges & Fees", "expense"),
        "taxes_gst": ("Taxes & GST", "expense"),
        "payment_processing": ("Payment Processing Fees", "expense"),
        "ecommerce": ("Office Supplies & E-commerce", "expense"),
        "legal_compliance": ("Legal & Professional Fees", "expense"),
        "consulting": ("Consulting & Professional Services", "expense"),
        "travel": ("Travel & Conveyance", "expense"),
        "telecommunications": ("Telephone & Internet", "expense"),
        "insurance": ("Insurance", "expense"),
        "utilities": ("Utilities", "expense"),
        "training": ("Training & Education", "expense"),
        "general": ("Miscellaneous Expenses", "expense"),
    }

    # COA account name keywords for fuzzy matching vendor names against existing Zoho accounts
    # Maps keywords found in COA account names → category
    COA_NAME_KEYWORDS = {
        "software license": "software_licenses", "license": "software_licenses",
        "software subscription": "software_subscriptions", "subscription": "software_subscriptions",
        "software": "saas_devtools",
        "cloud": "cloud_infrastructure", "hosting": "cloud_infrastructure", "server": "cloud_infrastructure",
        "monitoring": "saas_monitoring", "observability": "saas_monitoring",
        "ai": "ai_ml_services", "machine learning": "ai_ml_services",
        "rent": "office_rent", "co-working": "office_rent", "coworking": "office_rent",
        "entertainment": "entertainment", "streaming": "entertainment",
        "food": "food_beverages", "meal": "food_beverages", "beverages": "food_beverages",
        "bank charge": "bank_charges", "bank fee": "bank_charges",
        "gst": "taxes_gst", "tax": "taxes_gst",
        "payment processing": "payment_processing", "gateway": "payment_processing",
        "legal": "legal_compliance", "professional fee": "legal_compliance", "compliance": "legal_compliance",
        "consulting": "consulting", "advisory": "consulting", "audit": "consulting",
        "travel": "travel", "conveyance": "travel",
        "telephone": "telecommunications", "internet": "telecommunications", "telecom": "telecommunications",
        "insurance": "insurance", "premium": "insurance",
        "electricity": "utilities", "utility": "utilities", "power": "utilities",
        "training": "training", "education": "training",
        "recruitment": "recruitment", "hiring": "recruitment",
        "e-commerce": "ecommerce", "office supplies": "ecommerce",
    }

    def __init__(self, api, config_path="config/vendor_mappings.json"):
        self.api = api
        self.config_path = os.path.join(PROJECT_ROOT, config_path)
        self._coa_cache = None  # Lazy-loaded COA accounts
        self._load_account_mappings()

    def _load_account_mappings(self):
        """Load cached account_mappings from vendor_mappings.json."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._account_mappings = data.get("account_mappings", {})
        except Exception:
            self._account_mappings = {}

    def _save_account_mappings(self):
        """Save account_mappings back to vendor_mappings.json."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["account_mappings"] = self._account_mappings
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            log_action(f"  Failed to save account mappings: {e}", "WARNING")

    def _get_coa(self):
        """Lazy-load and cache all COA accounts from Zoho."""
        if self._coa_cache is None:
            self._coa_cache = self.api.get_all_accounts()
            log_action(f"Loaded {len(self._coa_cache)} COA accounts from Zoho")
        return self._coa_cache

    def _ensure_account_exists(self, account_name, account_type):
        """Find an existing COA account by exact or fuzzy name match. Never creates new accounts.

        Uses fuzzy matching to find similar account names (e.g. 'Software License'
        matches 'Software Licence', 'Software Licences', etc.).
        Returns account_id if found, None otherwise.
        """
        coa = self._get_coa()
        if account_name in coa:
            return coa[account_name]["account_id"]

        # Fuzzy match against existing COA names
        name_lower = account_name.lower()
        best_score, best_match = 0, None
        for existing_name in coa:
            score = fuzz.token_set_ratio(name_lower, existing_name.lower())
            if score > best_score:
                best_score = score
                best_match = existing_name
        if best_score >= 85 and best_match:
            log_action(f"  Fuzzy COA match: '{account_name}' -> '{best_match}' (score: {best_score})")
            return coa[best_match]["account_id"]

        log_action(f"  No matching COA account found for '{account_name}' (best: '{best_match}' score: {best_score})", "WARNING")
        return None

    def _match_against_coa(self, vendor_name):
        """Fuzzy match vendor name against existing Zoho COA account names.

        Checks if any existing COA account name is semantically related to the vendor.
        Uses fuzz.token_set_ratio for flexible matching.
        Returns (account_id, account_name) if a good match is found, else (None, None).
        """
        coa = self._get_coa()
        vendor_lower = vendor_name.lower()

        # First: check if vendor name directly contains a COA keyword
        for kw, category in self.COA_NAME_KEYWORDS.items():
            if kw in vendor_lower:
                acct_name, acct_type = self.CATEGORY_TO_ACCOUNT.get(category, (None, None))
                if acct_name and acct_name in coa:
                    log_action(f"  COA keyword match: '{vendor_name}' contains '{kw}' -> {acct_name}")
                    return coa[acct_name]["account_id"], acct_name

        # Second: fuzzy match vendor name against expense-type COA account names only
        valid_bill_types = {"expense", "other_expense", "cost_of_goods_sold", "other_current_liability", "fixed_asset", "other_current_asset"}
        best_score, best_name = 0, None
        for acct_name, info in coa.items():
            if info.get("account_type", "") not in valid_bill_types:
                continue
            score = fuzz.token_set_ratio(vendor_lower, acct_name.lower())
            if score > best_score:
                best_score = score
                best_name = acct_name

        if best_score >= 70 and best_name:
            log_action(f"  COA fuzzy match: '{vendor_name}' -> '{best_name}' (score: {best_score})")
            return coa[best_name]["account_id"], best_name

        return None, None

    def _match_invoice_against_coa(self, invoice_data):
        """Try to directly match invoice text against existing Zoho COA account names.

        Scans raw_text_preview for words/phrases that match actual COA account names.
        Returns (account_id, account_name) if a strong match is found, else (None, None).
        """
        text = invoice_data.get("raw_text_preview", "")
        if not text:
            return None, None

        text_lower = text.lower()
        coa = self._get_coa()

        # Only match against expense-type accounts (not bank/asset/liability accounts)
        _EXPENSE_TYPES = {"expense", "cost_of_goods_sold"}
        best_score, best_name = 0, None
        for acct_name, acct_info in coa.items():
            if acct_info.get("account_type", "").lower() not in _EXPENSE_TYPES:
                continue
            acct_lower = acct_name.lower()
            # Check if COA account name words appear in invoice text
            acct_words = [w for w in acct_lower.split() if len(w) > 2]
            if not acct_words:
                continue
            hits = sum(1 for w in acct_words if w in text_lower)
            score = hits / len(acct_words)  # proportion of account name words found
            if score > best_score and score >= 0.5:  # at least 50% of words match
                best_score = score
                best_name = acct_name

        if best_name:
            log_action(f"  Invoice text matched COA account: '{best_name}' (score: {best_score:.0%})")
            return coa[best_name]["account_id"], best_name

        return None, None

    def _classify_from_invoice(self, invoice_data):
        """Classify vendor category from invoice PDF text using keyword rules.

        Uses raw_text_preview (first 500 chars of invoice) to find keyword matches.
        This catches context clues like 'government', 'legal', 'consulting' etc.
        """
        text = invoice_data.get("raw_text_preview", "")
        if not text:
            return None

        category = self._classify_from_text(text)
        if category:
            log_action(f"  Classified from invoice text as '{category}'")
        return category

    def _search_vendor_info(self, vendor_name):
        """Search DuckDuckGo Instant Answer API for vendor information."""
        try:
            resp = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": vendor_name, "format": "json", "no_html": 1},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                parts = [data.get("Abstract", ""), data.get("AbstractText", "")]
                for topic in data.get("RelatedTopics", [])[:5]:
                    if isinstance(topic, dict):
                        parts.append(topic.get("Text", ""))
                text = " ".join(parts).strip()
                if text:
                    log_action(f"  DuckDuckGo result for '{vendor_name}': {text[:100]}...")
                    return text
        except Exception as e:
            log_action(f"  DuckDuckGo search failed for '{vendor_name}': {e}", "WARNING")
        return ""

    def _classify_from_text(self, text):
        """Match text against keyword rules to determine category.

        Scores each category by counting keyword hits. Returns the category
        with the most hits, or None if no keywords match.
        """
        text_lower = text.lower()
        best_category = None
        best_hits = 0
        for keywords, category in self.KEYWORD_RULES:
            hits = sum(1 for kw in keywords if kw in text_lower)
            if hits > best_hits:
                best_hits = hits
                best_category = category
        return best_category

    def get_account_for_vendor(self, vendor_name, invoice_data=None):
        """Main entry point. Returns (account_id, account_name) for a vendor.

        Args:
            vendor_name: The vendor/company name.
            invoice_data: Optional dict with invoice fields (especially raw_text_preview).

        Resolution order:
        1. Check cache in vendor_mappings.json["account_mappings"]
        2. Check KNOWN_VENDOR_CATEGORIES hard-coded map
        3. Fuzzy match vendor name against existing Zoho COA account names
        4. Match invoice PDF text against existing Zoho COA account names
        5. Classify from invoice text using keyword rules
        6. Web search via DuckDuckGo → classify from text
        7. Fallback to "general" → "Miscellaneous Expenses"
        """
        if not vendor_name:
            return None, None

        # 1. Check cache — but validate account_id exists in current org's COA
        if vendor_name in self._account_mappings:
            cached = self._account_mappings[vendor_name]
            account_id = cached.get("account_id")
            account_name = cached.get("account_name")
            if account_id and account_name:
                coa = self._get_coa()
                # Verify the cached account_id actually exists in this org AND is a valid bill account type
                coa_by_id = {v["account_id"]: v for v in coa.values()}
                valid_bill_types = {"expense", "other_expense", "cost_of_goods_sold", "other_current_liability", "fixed_asset", "other_current_asset"}
                if account_id in coa_by_id:
                    acct_type = coa_by_id[account_id].get("account_type", "")
                    if acct_type not in valid_bill_types:
                        log_action(f"  Cached account '{account_name}' is type '{acct_type}' (not valid for bills), skipping")
                        del self._account_mappings[vendor_name]
                        self._save_account_mappings()
                    else:
                        log_action(f"  Account (cached): {vendor_name} -> {account_name}")
                        return account_id, account_name
                else:
                    # Cached ID is stale (wrong org) — try to find by name in current org
                    log_action(f"  Cached account_id {account_id} not in current org, re-resolving '{account_name}'")
                    resolved_id = self._ensure_account_exists(account_name, "expense")
                    if resolved_id:
                        self._account_mappings[vendor_name]["account_id"] = resolved_id
                        self._save_account_mappings()
                        return resolved_id, account_name

        # 2. Check hard-coded known vendors
        category = self.KNOWN_VENDOR_CATEGORIES.get(vendor_name)

        # 3. Fuzzy match vendor name against existing Zoho COA accounts
        if not category:
            acct_id, acct_name = self._match_against_coa(vendor_name)
            if acct_id:
                self._account_mappings[vendor_name] = {
                    "account_name": acct_name,
                    "account_id": acct_id,
                    "category": "coa_match",
                }
                self._save_account_mappings()
                return acct_id, acct_name

        # 4. Match invoice PDF text directly against Zoho COA account names
        if not category and invoice_data:
            acct_id, acct_name = self._match_invoice_against_coa(invoice_data)
            if acct_id:
                self._account_mappings[vendor_name] = {
                    "account_name": acct_name,
                    "account_id": acct_id,
                    "category": "invoice_coa_match",
                }
                self._save_account_mappings()
                log_action(f"  Account: {vendor_name} -> {acct_name} (from invoice text + COA)")
                return acct_id, acct_name

        # 5. Classify from invoice text using keyword rules
        if not category and invoice_data:
            category = self._classify_from_invoice(invoice_data)

        # 6. Web search for unknown vendors
        if not category:
            log_action(f"  Unknown vendor '{vendor_name}', searching DuckDuckGo...")
            text = self._search_vendor_info(vendor_name)
            if text:
                category = self._classify_from_text(text)
                if category:
                    log_action(f"  Classified '{vendor_name}' as '{category}' from web search")

        # 7. Fallback
        if not category:
            category = "general"
            log_action(f"  No category found for '{vendor_name}', using fallback: general")

        # Resolve category to COA account
        account_name, account_type = self.CATEGORY_TO_ACCOUNT.get(
            category, ("Miscellaneous Expenses", "expense")
        )
        account_id = self._ensure_account_exists(account_name, account_type)

        # Cache the mapping
        if account_id:
            self._account_mappings[vendor_name] = {
                "account_name": account_name,
                "account_id": account_id,
                "category": category,
            }
            self._save_account_mappings()
            log_action(f"  Account: {vendor_name} -> {account_name} ({account_id})")

        return account_id, account_name
