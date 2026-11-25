#!/usr/bin/env python3
"""Log in to the deployed app and verify 2FA in one go. Run it yourself:   ! python scripts/nexus_login.py

It asks for the email, password and the current 6-digit code (hidden input), logs in on the web host, verifies 2FA, and writes the session
cookie and CSRF token to a temp file (mode 600). Nothing is printed or stored in the repo. Delete the file with:  python scripts/nexus_login.py --logout
"""
import getpass
import http.cookiejar
import json
import os
import stat
import sys
import urllib.request

HOST = os.environ.get("NEXUS_HOST", "https://finbyte-nexus-web.fly.dev")
JAR = os.environ.get("NEXUS_SESSION_FILE", "/tmp/nexus_session.json")


def _post(opener, path, body, csrf=None):
    headers = {"Content-Type": "application/json", "Origin": HOST}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(HOST + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    return opener.open(request, timeout=30)


def logout() -> None:
    if os.path.exists(JAR):
        os.remove(JAR)
    print("session file removed")


def main() -> int:
    if "--logout" in sys.argv:
        logout()
        return 0
    email = os.environ.get("NEXUS_EMAIL") or input("email: ").strip()
    password = getpass.getpass("password: ")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        _post(opener, "/api/v1/auth/login", {"email": email, "password": password})
    except Exception as error:  # noqa: BLE001
        print("login failed:", getattr(error, "code", type(error).__name__))
        return 1
    finally:
        password = ""
    cookies = {c.name: c.value for c in jar}
    code = getpass.getpass("2FA code: ").strip()
    try:
        _post(opener, "/api/v1/auth/2fa/verify", {"code": code}, csrf=cookies.get("csrf_token"))
    except Exception as error:  # noqa: BLE001
        print("2FA failed:", getattr(error, "code", type(error).__name__), "(run again with a fresh code)")
        return 1
    fd = os.open(JAR, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        json.dump({"host": HOST, "session_token": cookies["session_token"], "csrf_token": cookies["csrf_token"]}, handle)
    print(f"logged in and verified; session saved to {JAR} (mode 600). Tell Claude it is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
