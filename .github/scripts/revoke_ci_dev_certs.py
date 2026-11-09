"""Revoke Apple Development certificates that earlier CI runs created.

Each GitHub Actions run is a fresh Mac, so `xcodebuild -allowProvisioningUpdates`
creates a new development certificate every time and the old one's private
key is lost. Apple caps the number of certificates, so stale ones are
revoked here. Only development certificates named "Created via API"
(the ones Xcode makes through the App Store Connect API) are touched.

Env: ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT

API = "https://api.appstoreconnect.apple.com/v1"
DEV_TYPES = {"DEVELOPMENT", "IOS_DEVELOPMENT"}
# Certificates are valid for one year; the API has no creation date, so infer
# it and leave very recent certificates alone (another build may be using one).
MIN_AGE = timedelta(minutes=int(os.environ.get("MIN_AGE_MINUTES", "60")))


def created_at(expiration: str | None) -> datetime | None:
    if not expiration:
        return None
    exp = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
    return exp.replace(year=exp.year - 1)


def token() -> str:
    with open(os.environ["ASC_KEY_PATH"]) as f:
        key = f.read()
    now = int(time.time())
    return jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
        key, algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"},
    )


def call(method: str, url: str, tok: str):
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def main() -> int:
    tok = token()
    data = call("GET", f"{API}/certificates?limit=200", tok)["data"]
    revoked = 0
    for cert in data:
        a = cert["attributes"]
        label = f'{a.get("certificateType")} | {a.get("name")} | {a.get("displayName")} | expires {a.get("expirationDate")}'
        created = created_at(a.get("expirationDate"))
        too_new = created is not None and datetime.now(timezone.utc) - created < MIN_AGE
        if a.get("certificateType") in DEV_TYPES and "Created via API" in f'{a.get("name")} {a.get("displayName")}':
            if too_new:
                print(f"kept (newer than {MIN_AGE}): {label}")
                continue
            call("DELETE", f"{API}/certificates/{cert['id']}", tok)
            revoked += 1
            print(f"revoked: {label}")
        else:
            print(f"kept:    {label}")
    print(f"{revoked} stale CI development certificate(s) revoked, {len(data) - revoked} kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
