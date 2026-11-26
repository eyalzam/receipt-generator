# Receipt Generator - מחולל קבלות

A simple local app for generating and emailing Hebrew receipt PDFs.

## Quick Start

### Windows
1. Install Python 3.9+ from [python.org/downloads](https://www.python.org/downloads/) — **check "Add Python to PATH"** during install!
2. Open Command Prompt (cmd), navigate to the folder, and run these commands once:
```
cd C:\path\to\receipt-generator
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
3. To start the app (every time):
```
cd C:\path\to\receipt-generator
venv\Scripts\activate
python app.py
```
4. Open http://127.0.0.1:5050 in your browser

### Mac
1. Double-click **`start.command`** (or drag it to the Dock)
2. The app opens at http://127.0.0.1:5050

If first time on Mac, run once in Terminal:
```bash
cd ~/path/to/receipt-generator
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## First Time Setup

1. Go to **הגדרות (Settings)** and fill in your business details
2. Go to **לקוחות (Clients)** and add your clients (name, nickname, company ID, phone, email)
3. Set the starting receipt number in Settings
4. For email: go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and create an App Password, then paste it in Settings

## Files

| File | Purpose |
|------|---------|
| `receipts.db` | Your data (clients, receipts, settings). **Back this up!** |
| `extracted_img_0.png` | Digital signature seal image on the PDF |
| `start.command` | Mac launcher (double-click) |

## To stop the server

Close the Terminal/Command Prompt window, or press `Ctrl+C`.
