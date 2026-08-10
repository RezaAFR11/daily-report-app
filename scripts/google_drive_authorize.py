"""One-time local OAuth helper for the Daily Report Google Drive integration.

Run this only on a trusted computer.  It prints the refresh token so it can be
copied directly into Railway Variables; it never writes a token file.
"""

from __future__ import annotations

import os
import sys


SCOPE = "https://www.googleapis.com/auth/drive.file"


def main() -> int:
    client_id = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET before running this script.")
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install dependencies first: python -m pip install -r requirements.txt")
        return 2

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=[SCOPE],
    )
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )
    if not credentials.refresh_token:
        print("Google did not return a refresh token. Revoke the app grant and run again.")
        return 1

    print("\nAuthorization successful. Add this secret to Railway Variables:")
    print(f"GDRIVE_REFRESH_TOKEN={credentials.refresh_token}")
    print("\nDo not paste this token into GitHub or application settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
