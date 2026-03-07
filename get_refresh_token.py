"""
Fetch Refresh Token from Zoho API using self_client.json

Usage:
    1. Generate a NEW code in Zoho API Console → Self Client
    2. Update self_client.json with the new code
    3. Run IMMEDIATELY: python get_refresh_token.py
"""

import json
import requests
import sys
import os

def main():
    # Look for self_client.json in current dir or config/
    json_path = None
    for path in ["self_client.json", "config/self_client.json"]:
        if os.path.exists(path):
            json_path = path
            break

    if not json_path:
        print("❌ self_client.json not found!")
        print("   Place it in the project root or config/ folder")
        sys.exit(1)

    with open(json_path, "r") as f:
        data = json.load(f)

    print(f"📄 Loaded credentials from: {json_path}")
    print(f"   Client ID: {data['client_id'][:15]}...")
    print(f"   Code: {data['code'][:15]}...")
    print(f"   Scope: {data['scope']}")
    print()

    # Request refresh token
    print("🔄 Requesting refresh token from Zoho...")
    response = requests.post(
        "https://accounts.zoho.in/oauth/v2/token",
        data={
            "code": data["code"],
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "grant_type": data["grant_type"],
        },
    )

    result = response.json()

    if "error" in result:
        print(f"\n❌ Error: {result['error']}")
        if result["error"] == "invalid_code":
            print("\n   The authorization code has expired!")
            print("   Steps to fix:")
            print("   1. Go to https://api-console.zoho.in/ → Self Client")
            print("   2. Scope: ZohoBooks.fullaccess.all")
            print("   3. Duration: 10 minutes → Generate")
            print("   4. Copy the NEW code into self_client.json")
            print("   5. Run this script IMMEDIATELY (within 1-2 minutes)")
        sys.exit(1)

    # Success
    refresh_token = result.get("refresh_token")
    access_token = result.get("access_token")

    print(f"\n✅ SUCCESS!")
    print(f"   Access Token:  {access_token[:20]}...")
    print(f"   Refresh Token: {refresh_token}")
    print(f"   Expires In:    {result.get('expires_in')} seconds")

    # Auto-update zoho_config.json if it exists
    config_path = "config/zoho_config.json"
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)

        config["zoho_books"]["client_id"] = data["client_id"]
        config["zoho_books"]["client_secret"] = data["client_secret"]
        config["zoho_books"]["refresh_token"] = refresh_token

        with open(config_path, "w") as f:
            json.dump(config, f, indent=4)

        print(f"\n📝 Auto-updated {config_path} with all credentials!")
    else:
        print(f"\n⚠️  Manually add this refresh_token to config/zoho_config.json:")
        print(f'   "refresh_token": "{refresh_token}"')

    # Save tokens to a separate file for reference
    tokens_path = "config/tokens.json"
    with open(tokens_path, "w") as f:
        json.dump({
            "refresh_token": refresh_token,
            "access_token": access_token,
            "note": "refresh_token does not expire. access_token expires in 1 hour.",
        }, f, indent=2)
    print(f"💾 Tokens saved to {tokens_path}")


if __name__ == "__main__":
    main()
