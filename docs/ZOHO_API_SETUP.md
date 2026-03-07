# Zoho Books API Setup Guide

## Step 1: Create a Self Client in Zoho API Console

1. Go to [https://api-console.zoho.in/](https://api-console.zoho.in/)
2. Click **"Add Client"** → Select **"Self Client"**
3. Note your **Client ID** and **Client Secret**

## Step 2: Generate Authorization Code

1. In the Self Client page, enter scope:
   ```
   ZohoBooks.fullaccess.all
   ```
2. Set time duration: **10 minutes**
3. Enter description: "CC Statement Automation"
4. Click **"Create"**
5. Copy the **authorization code** (valid for only a few minutes)

## Step 3: Generate Refresh Token

Run this curl command (replace placeholders):

```bash
curl -X POST "https://accounts.zoho.in/oauth/v2/token" \
  -d "code=YOUR_AUTH_CODE" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "grant_type=authorization_code"
```

Response will include:
```json
{
  "access_token": "...",
  "refresh_token": "SAVE_THIS_REFRESH_TOKEN",
  "expires_in": 3600
}
```

**Save the `refresh_token`** — this doesn't expire and is used by the scripts.

## Step 4: Get Organization ID

```bash
curl -X GET "https://www.zohoapis.in/books/v3/organizations" \
  -H "Authorization: Zoho-oauthtoken YOUR_ACCESS_TOKEN"
```

## Step 5: Get Credit Card Account IDs

```bash
curl -X GET "https://www.zohoapis.in/books/v3/bankaccounts?organization_id=YOUR_ORG_ID" \
  -H "Authorization: Zoho-oauthtoken YOUR_ACCESS_TOKEN"
```

Filter for accounts with `account_type: "credit_card"`.

## Step 6: Update Config

Add all values to `config/zoho_config.json`:
```json
{
    "zoho_books": {
        "organization_id": "YOUR_ORG_ID",
        "client_id": "YOUR_CLIENT_ID",
        "client_secret": "YOUR_CLIENT_SECRET",
        "refresh_token": "YOUR_REFRESH_TOKEN"
    }
}
```

## Security Notes
- Never commit `zoho_config.json` to version control
- Add it to `.gitignore`
- Consider using environment variables for production
