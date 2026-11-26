#!/usr/bin/env python3
"""
Deploy receipt-generator to PythonAnywhere.
Usage: python deploy_pythonanywhere.py <username> <api_token>
"""
import os
import sys
import time
import requests

if len(sys.argv) < 3:
    print("Usage: python deploy_pythonanywhere.py <username> <api_token>")
    print("\nGet your API token from: https://www.pythonanywhere.com/account/#api_token")
    sys.exit(1)

USERNAME = sys.argv[1]
TOKEN = sys.argv[2]
HOST = "www.pythonanywhere.com"
API_BASE = f"https://{HOST}/api/v0/user/{USERNAME}"
HEADERS = {"Authorization": f"Token {TOKEN}"}
DOMAIN = f"{USERNAME}.pythonanywhere.com"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = f"/home/{USERNAME}/receipt-generator"

FILES_TO_UPLOAD = [
    "app.py",
    "pdf_generator.py",
    "wsgi.py",
    "requirements.txt",
    "extracted_img_0.png",
    "templates/base.html",
    "templates/index.html",
    "templates/clients.html",
    "templates/settings.html",
    "templates/login.html",
    "templates/register.html",
    "fonts/Arial.ttf",
    "fonts/Arial Bold.ttf",
]


def upload_file(local_path, remote_path):
    url = f"{API_BASE}/files/path{remote_path}"
    with open(local_path, "rb") as f:
        r = requests.post(url, files={"content": f}, headers=HEADERS)
    if r.status_code in (200, 201):
        print(f"  OK: {remote_path}")
    else:
        print(f"  FAIL ({r.status_code}): {remote_path} - {r.text[:200]}")
    return r.status_code in (200, 201)


def main():
    print(f"Deploying to {DOMAIN}...")
    print()

    # 1. Upload all files
    print("1. Uploading files...")
    for rel in FILES_TO_UPLOAD:
        local = os.path.join(PROJECT_DIR, rel)
        if not os.path.exists(local):
            print(f"  SKIP (not found): {rel}")
            continue
        remote = f"{REMOTE_DIR}/{rel}"
        upload_file(local, remote)
    print()

    # 2. Create virtualenv via console
    print("2. Setting up virtualenv and installing dependencies...")
    console_url = f"{API_BASE}/consoles/"
    r = requests.post(console_url, json={
        "executable": "bash",
        "arguments": "",
        "working_directory": REMOTE_DIR,
    }, headers=HEADERS)
    if r.status_code in (200, 201):
        console_id = r.json()["id"]
        print(f"  Console created: {console_id}")

        commands = [
            f"cd {REMOTE_DIR}\n",
            f"python3 -m venv {REMOTE_DIR}/venv\n",
            f"source {REMOTE_DIR}/venv/bin/activate && pip install -r {REMOTE_DIR}/requirements.txt\n",
        ]
        for cmd in commands:
            send_url = f"{API_BASE}/consoles/{console_id}/send_input/"
            requests.post(send_url, json={"input": cmd}, headers=HEADERS)
            time.sleep(3)

        print("  Waiting for pip install to complete...")
        time.sleep(20)

        output_url = f"{API_BASE}/consoles/{console_id}/get_latest_output/"
        r = requests.get(output_url, headers=HEADERS)
        if r.status_code == 200:
            print(f"  Console output (last): {r.json().get('output', '')[:300]}")

        requests.delete(f"{API_BASE}/consoles/{console_id}/", headers=HEADERS)
        print("  Console cleaned up")
    else:
        print(f"  Console creation failed: {r.status_code} {r.text[:200]}")
    print()

    # 3. Create/update web app
    print("3. Configuring web app...")

    r = requests.get(f"{API_BASE}/webapps/", headers=HEADERS)
    existing = [w for w in r.json() if w["domain_name"] == DOMAIN] if r.status_code == 200 else []

    if not existing:
        r = requests.post(f"{API_BASE}/webapps/", json={
            "domain_name": DOMAIN,
            "python_version": "python310",
        }, headers=HEADERS)
        if r.status_code in (200, 201):
            print(f"  Web app created: {DOMAIN}")
        else:
            print(f"  Create failed: {r.status_code} {r.text[:200]}")
    else:
        print(f"  Web app already exists: {DOMAIN}")

    r = requests.patch(f"{API_BASE}/webapps/{DOMAIN}/", json={
        "source_directory": REMOTE_DIR,
        "virtualenv_path": f"{REMOTE_DIR}/venv",
        "force_https": True,
    }, headers=HEADERS)
    if r.status_code == 200:
        print("  Web app configured")
    else:
        print(f"  Config update: {r.status_code} {r.text[:200]}")

    # 4. Upload WSGI file
    print("4. Configuring WSGI...")
    wsgi_content = f"""import sys
import os

project_home = '{REMOTE_DIR}'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

os.environ['SECRET_KEY'] = '{os.urandom(24).hex()}'

from app import app as application
"""
    wsgi_path = f"/var/www/{USERNAME.replace('-', '_')}_pythonanywhere_com_wsgi.py"
    url = f"{API_BASE}/files/path{wsgi_path}"
    r = requests.post(url, files={"content": ("wsgi.py", wsgi_content.encode())}, headers=HEADERS)
    if r.status_code in (200, 201):
        print(f"  WSGI file uploaded to {wsgi_path}")
    else:
        print(f"  WSGI upload: {r.status_code} {r.text[:200]}")
    print()

    # 5. Reload
    print("5. Reloading web app...")
    r = requests.post(f"{API_BASE}/webapps/{DOMAIN}/reload/", headers=HEADERS)
    if r.status_code == 200:
        print(f"  Reloaded successfully!")
    else:
        print(f"  Reload: {r.status_code} {r.text[:200]}")
    print()

    print(f"Done! Your app should be live at: https://{DOMAIN}")
    print(f"\nFirst visit: go to https://{DOMAIN}/register to create your user account.")


if __name__ == "__main__":
    main()
