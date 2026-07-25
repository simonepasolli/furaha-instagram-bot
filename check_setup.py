"""
Run this FIRST, before anything else.

It takes your Meta access token and tells you:
  1. whether the token works at all
  2. your Instagram Account ID (the number you need for IG_USER_ID)
  3. whether the token has the right permissions
  4. when the token expires

Usage:
    python check_setup.py YOUR_TOKEN_HERE
"""

import sys
from datetime import datetime

import requests

GRAPH = "https://graph.facebook.com/v23.0"

if len(sys.argv) < 2:
    sys.exit("Usage: python check_setup.py YOUR_TOKEN_HERE")

TOKEN = sys.argv[1].strip()


def get(path, **params):
    params["access_token"] = TOKEN
    response = requests.get(f"{GRAPH}/{path}", params=params, timeout=30)
    data = response.json()
    if "error" in data:
        print(f"\n  Meta said: {data['error'].get('message')}")
        return None
    return data


print("\n=== 1. Is the token valid? ===")
me = get("me", fields="id,name")
if not me:
    sys.exit("\nThe token does not work. Generate a new one and try again.")
print(f"OK - token belongs to: {me.get('name')}")


print("\n=== 2. Token permissions and expiry ===")
debug = get("debug_token", input_token=TOKEN)
if debug:
    info = debug.get("data", {})
    scopes = info.get("scopes", [])
    print("Permissions on this token:")
    for scope in sorted(scopes):
        print(f"  - {scope}")

    needed = ["instagram_basic", "instagram_content_publish", "pages_show_list"]
    missing = [n for n in needed if n not in scopes]
    if missing:
        print(f"\n  WARNING - missing permission(s): {', '.join(missing)}")
        print("  Regenerate the token and tick those boxes.")
    else:
        print("\nOK - all required permissions present.")

    expires = info.get("expires_at", 0)
    if expires == 0:
        print("OK - this token never expires. This is what you want.")
    else:
        when = datetime.fromtimestamp(expires)
        print(f"  WARNING - token expires on {when:%d %B %Y}. Put a reminder in your calendar.")


print("\n=== 3. Finding your Instagram account ===")
pages = get("me/accounts", fields="name,id,instagram_business_account")
if not pages or not pages.get("data"):
    sys.exit("\nNo Facebook Pages found. Your Instagram account must be linked to a "
             "Facebook Page. Go back to Part 1, Step 2 of the guide.")

found = False
for page in pages["data"]:
    ig = page.get("instagram_business_account")
    print(f"\nFacebook Page: {page['name']}")
    if ig:
        found = True
        profile = get(ig["id"], fields="username,followers_count")
        username = profile.get("username", "?") if profile else "?"
        print(f"  Linked Instagram: @{username}")
        print(f"\n  >>> YOUR IG_USER_ID IS: {ig['id']}")
        print("  >>> Copy that number. You need it for GitHub Secrets.")
    else:
        print("  No Instagram account linked to this Page.")

if not found:
    print("\nNo Instagram professional account is linked to any of your Pages.")
    print("Go back to Part 1, Step 2 of the guide and link them.")
else:
    print("\nAll good. Continue to Part 2 of the guide.")
