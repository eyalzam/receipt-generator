import os
import io
import re
import json
import base64
import sqlite3
import smtplib
import imaplib
import hashlib
import secrets
import email as emaillib
from functools import wraps
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from email.header import decode_header
from datetime import datetime, date
from flask import Flask, g, render_template, request, redirect, url_for, send_file, flash, jsonify, session
from cryptography.fernet import Fernet

from pdf_generator import generate_receipt_pdf

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'receipt-generator-secret-key-change-me')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'receipts.db')

HEBREW_MONTHS = {
    1: 'ינואר', 2: 'פברואר', 3: 'מרץ', 4: 'אפריל',
    5: 'מאי', 6: 'יוני', 7: 'יולי', 8: 'אוגוסט',
    9: 'ספטמבר', 10: 'אוקטובר', 11: 'נובמבר', 12: 'דצמבר'
}


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def check_password(stored, password):
    if ':' not in stored:
        return False
    salt, h = stored.split(':', 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


def _get_fernet():
    key_source = app.secret_key
    key = base64.urlsafe_b64encode(hashlib.sha256(key_source.encode()).digest())
    return Fernet(key)


def encrypt_value(plain_text):
    if not plain_text:
        return ''
    return _get_fernet().encrypt(plain_text.encode()).decode()


def decrypt_value(encrypted_text):
    if not encrypted_text:
        return ''
    try:
        return _get_fernet().decrypt(encrypted_text.encode()).decode()
    except Exception:
        # If decryption fails, the value is likely still plain text (pre-migration)
        return encrypted_text


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS issuer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL, tax_id TEXT NOT NULL,
            address TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL,
            hebrew_name TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL, company_id TEXT NOT NULL,
            phone TEXT NOT NULL, email TEXT DEFAULT '',
            nickname TEXT DEFAULT '', active INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            receipt_number INTEGER NOT NULL,
            client_id INTEGER NOT NULL, date TEXT NOT NULL,
            amount REAL NOT NULL, notes TEXT,
            for_month TEXT, email_subject TEXT,
            emailed INTEGER DEFAULT 0, emailed_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (client_id) REFERENCES clients(id),
            UNIQUE(user_id, receipt_number)
        );
        CREATE TABLE IF NOT EXISTS sequence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            next_number INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS email_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            smtp_email TEXT NOT NULL DEFAULT '',
            smtp_password TEXT NOT NULL DEFAULT '',
            smtp_host TEXT NOT NULL DEFAULT 'smtp.gmail.com',
            smtp_port INTEGER NOT NULL DEFAULT 587,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    ''')

    # Migration: add hebrew_name to issuer if missing
    cols = {r[1] for r in conn.execute("PRAGMA table_info(issuer)").fetchall()}
    if 'hebrew_name' not in cols:
        conn.execute("ALTER TABLE issuer ADD COLUMN hebrew_name TEXT DEFAULT ''")

    conn.commit()
    conn.close()


init_db()


# --- Auth ---

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session.get('user_id')


@app.context_processor
def inject_user():
    if 'user_id' in session:
        conn = get_db()
        u = conn.execute('SELECT username, display_name FROM users WHERE id=?', (current_user_id(),)).fetchone()
        conn.close()
        return dict(current_user=dict(u) if u else None)
    return dict(current_user=None)


@app.teardown_appcontext
def close_db_on_teardown(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# --- Per-user helpers ---

def get_issuer():
    conn = get_db()
    row = conn.execute('SELECT * FROM issuer WHERE user_id=?', (current_user_id(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_next_receipt_number():
    conn = get_db()
    row = conn.execute('SELECT next_number FROM sequence WHERE user_id=?', (current_user_id(),)).fetchone()
    conn.close()
    return row['next_number'] if row else 1


def set_receipt_number(num):
    conn = get_db()
    conn.execute('UPDATE sequence SET next_number=? WHERE user_id=?', (num, current_user_id()))
    conn.commit()
    conn.close()


def get_email_settings():
    conn = get_db()
    row = conn.execute('SELECT * FROM email_settings WHERE user_id=?', (current_user_id(),)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d['smtp_password'] = decrypt_value(d.get('smtp_password', ''))
    return d


def format_month_hebrew(for_month):
    if not for_month or '-' not in for_month:
        return for_month or ''
    try:
        parts = for_month.split('-')
        return f"{HEBREW_MONTHS.get(int(parts[1]), '')} {parts[0]}"
    except (ValueError, IndexError):
        return for_month


def build_email_subject(hebrew_name, nickname, for_month):
    month_heb = format_month_hebrew(for_month)
    parts = ['שכר']
    if nickname:
        parts.append(nickname)
    if month_heb:
        parts.append(month_heb)
    if hebrew_name:
        parts.append(f'- {hebrew_name}')
    return ' '.join(parts)


HEBREW_MONTH_TO_NUM = {v: k for k, v in HEBREW_MONTHS.items()}


def parse_message(text, clients):
    """Parse email body or WhatsApp message to extract amount, month, email, description, and client match."""
    result = {'amount': None, 'for_month': None, 'send_to_email': None,
              'description': '', 'client_id': None, 'client_name': None, 'source_type': None}
    if not text:
        return result

    lines = text.strip().split('\n')

    # Detect amount: patterns like "450 שח", "₪750", "750.00 ש״ח", "סכום: 450", "סך לתשלום...450"
    amount_patterns = [
        r'(\d[\d,]*\.?\d*)\s*(?:שח|ש״ח|ש"ח|₪|שקל|שקלים|nis|ils)',
        r'(?:₪)\s*(\d[\d,]*\.?\d*)',
        r'(?:סכום|סך|לתשלום|amount)[:\s]*(\d[\d,]*\.?\d*)',
    ]
    for pat in amount_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result['amount'] = float(m.group(1).replace(',', ''))
            break

    # Detect month: Hebrew month name + optional year
    for heb_name, month_num in HEBREW_MONTH_TO_NUM.items():
        pat = rf'{heb_name}\s*(\d{{4}})?'
        m = re.search(pat, text)
        if m:
            year = m.group(1) if m.group(1) else str(date.today().year)
            result['for_month'] = f'{year}-{month_num:02d}'
            break

    # Detect email address (strip Hebrew prefix like ל before the address)
    email_match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', text)
    if email_match:
        result['send_to_email'] = email_match.group(1)

    # Try to match a client by nickname in text, or by email
    if clients:
        for c in clients:
            if c.get('nickname') and c['nickname'] in text:
                result['client_id'] = c['id']
                result['client_name'] = c['name']
                break
        if not result['client_id'] and result['send_to_email']:
            for c in clients:
                if c.get('email') and c['email'].lower() == result['send_to_email'].lower():
                    result['client_id'] = c['id']
                    result['client_name'] = c['name']
                    break
        if not result['client_id']:
            for c in clients:
                if c.get('name') and c['name'] in text:
                    result['client_id'] = c['id']
                    result['client_name'] = c['name']
                    break

    # Detect source type
    has_greeting = any(kw in text for kw in ['היי ', 'אודה ל', 'בוקר טוב'])
    has_email_subject_format = text.strip().startswith('שכר ')
    if has_email_subject_format:
        result['source_type'] = 'email'
    elif has_greeting:
        result['source_type'] = 'whatsapp'
    elif '@' in text and any(kw in text for kw in ['שכר', 'קבלה', 'תשלום', 'חשבונית']):
        result['source_type'] = 'email'

    # Description: use the full text, cleaned up
    result['description'] = text.strip()

    return result


def send_receipt_email(to_email, subject, body_text, pdf_buffer, receipt_number):
    settings = get_email_settings()
    if not settings or not settings['smtp_password']:
        return False, 'Gmail App Password not configured. Go to Settings.'
    msg = MIMEMultipart()
    msg['From'] = settings['smtp_email']
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    part = MIMEBase('application', 'pdf')
    part.set_payload(pdf_buffer.read())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="receipt-{receipt_number}.pdf"')
    msg.attach(part)
    try:
        srv = smtplib.SMTP(settings['smtp_host'], settings['smtp_port'])
        srv.starttls()
        srv.login(settings['smtp_email'], settings['smtp_password'])
        srv.send_message(msg)
        srv.quit()
        return True, 'OK'
    except Exception as e:
        return False, str(e)


# --- Auth Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        conn.close()
        if user and check_password(user['password'], password):
            session['user_id'] = user['id']
            return redirect(url_for('index'))
        flash('שם משתמש או סיסמה שגויים', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        display_name = request.form.get('display_name', '').strip()
        if len(username) < 3 or len(password) < 4:
            flash('שם משתמש (3+ תווים) וסיסמה (4+ תווים)', 'error')
            return render_template('register.html')
        conn = get_db()
        existing = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if existing:
            conn.close()
            flash('שם משתמש תפוס', 'error')
            return render_template('register.html')
        conn.execute('INSERT INTO users(username, password, display_name) VALUES(?,?,?)',
                     (username, hash_password(password), display_name))
        user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT INTO issuer(user_id,name,tax_id,address,phone,email,hebrew_name) VALUES(?,'שם העסק שלי','000000000','כתובת','0500000000','email@example.com','')", (user_id,))
        conn.execute('INSERT INTO sequence(user_id,next_number) VALUES(?,1)', (user_id,))
        conn.execute("INSERT INTO email_settings(user_id,smtp_email,smtp_password) VALUES(?,'','')", (user_id,))
        conn.commit()
        conn.close()
        session['user_id'] = user_id
        flash('!ברוכים הבאים', 'success')
        return redirect(url_for('settings_page'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- Main Routes ---

@app.route('/')
@login_required
def index():
    uid = current_user_id()
    conn = get_db()
    clients = conn.execute('SELECT * FROM clients WHERE user_id=? AND active=1 ORDER BY name', (uid,)).fetchall()
    receipts = conn.execute('''
        SELECT r.*, c.name as client_name
        FROM receipts r JOIN clients c ON r.client_id=c.id
        WHERE r.user_id=? ORDER BY r.receipt_number DESC LIMIT 50
    ''', (uid,)).fetchall()
    conn.close()
    issuer = get_issuer()
    return render_template('index.html',
                           clients=[dict(c) for c in clients],
                           receipts=[dict(r) for r in receipts],
                           next_number=get_next_receipt_number(),
                           today=date.today().strftime('%Y-%m-%d'),
                           issuer_hebrew_name=issuer.get('hebrew_name', '') if issuer else '')


@app.route('/api/client/<int:client_id>')
@login_required
def api_client(client_id):
    conn = get_db()
    c = conn.execute('SELECT nickname FROM clients WHERE id=? AND user_id=?', (client_id, current_user_id())).fetchone()
    conn.close()
    return jsonify(nickname=c['nickname'] or '' if c else '')


@app.route('/create', methods=['POST'])
@login_required
def create_receipt():
    uid = current_user_id()
    client_id = int(request.form['client_id'])
    receipt_date = request.form['date']
    amount = float(request.form['amount'])
    notes = request.form.get('notes', '').strip()
    for_month = request.form.get('for_month', '').strip()
    email_subject = request.form.get('email_subject', '').strip()
    use_custom = request.form.get('use_custom_number') == 'on'
    receipt_number = int(request.form.get('receipt_number', 0))
    if not use_custom:
        receipt_number = get_next_receipt_number()
    conn = get_db()
    existing = conn.execute('SELECT id FROM receipts WHERE user_id=? AND receipt_number=?', (uid, receipt_number)).fetchone()
    if existing:
        conn.execute('UPDATE receipts SET client_id=?,date=?,amount=?,notes=?,for_month=?,email_subject=? WHERE user_id=? AND receipt_number=?',
                     (client_id, receipt_date, amount, notes, for_month, email_subject, uid, receipt_number))
    else:
        conn.execute('INSERT INTO receipts(user_id,receipt_number,client_id,date,amount,notes,for_month,email_subject) VALUES(?,?,?,?,?,?,?,?)',
                     (uid, receipt_number, client_id, receipt_date, amount, notes, for_month, email_subject))
        if not use_custom:
            conn.execute('UPDATE sequence SET next_number=next_number+1 WHERE user_id=?', (uid,))
    conn.commit()
    conn.close()
    return redirect(url_for('download_receipt', receipt_number=receipt_number))


@app.route('/download/<int:receipt_number>')
@login_required
def download_receipt(receipt_number):
    uid = current_user_id()
    conn = get_db()
    r = conn.execute('''
        SELECT r.*, c.name as client_name, c.company_id as client_company_id, c.phone as client_phone
        FROM receipts r JOIN clients c ON r.client_id=c.id
        WHERE r.user_id=? AND r.receipt_number=?
    ''', (uid, receipt_number)).fetchone()
    issuer = conn.execute('SELECT * FROM issuer WHERE user_id=?', (uid,)).fetchone()
    conn.close()
    if not r:
        flash('Receipt not found', 'error')
        return redirect(url_for('index'))
    buf = generate_receipt_pdf(dict(issuer), dict(r))
    return send_file(buf, as_attachment=True, download_name=f'receipt-{receipt_number}.pdf', mimetype='application/pdf')


@app.route('/preview/<int:receipt_number>')
@login_required
def preview_receipt(receipt_number):
    uid = current_user_id()
    conn = get_db()
    r = conn.execute('''
        SELECT r.*, c.name as client_name, c.company_id as client_company_id, c.phone as client_phone
        FROM receipts r JOIN clients c ON r.client_id=c.id
        WHERE r.user_id=? AND r.receipt_number=?
    ''', (uid, receipt_number)).fetchone()
    issuer = conn.execute('SELECT * FROM issuer WHERE user_id=?', (uid,)).fetchone()
    conn.close()
    if not r:
        flash('Receipt not found', 'error')
        return redirect(url_for('index'))
    buf = generate_receipt_pdf(dict(issuer), dict(r))
    return send_file(buf, as_attachment=False, download_name=f'receipt-{receipt_number}.pdf', mimetype='application/pdf')


@app.route('/send_email/<int:receipt_number>', methods=['POST'])
@login_required
def send_email(receipt_number):
    uid = current_user_id()
    conn = get_db()
    r = conn.execute('''
        SELECT r.*, c.name as client_name, c.company_id as client_company_id,
               c.phone as client_phone, c.email as client_email, c.nickname as client_nickname
        FROM receipts r JOIN clients c ON r.client_id=c.id
        WHERE r.user_id=? AND r.receipt_number=?
    ''', (uid, receipt_number)).fetchone()
    if not r:
        conn.close()
        flash('Receipt not found', 'error')
        return redirect(url_for('index'))
    rd = dict(r)
    issuer = dict(conn.execute('SELECT * FROM issuer WHERE user_id=?', (uid,)).fetchone())
    conn.close()

    to_email = request.form.get('email', rd.get('client_email', '')).strip()
    if not to_email:
        flash('No email for this client. Add it in the Clients page.', 'error')
        return redirect(url_for('index'))

    subject = rd.get('email_subject', '') or build_email_subject(issuer.get('hebrew_name', ''), rd.get('client_nickname', ''), rd.get('for_month', ''))

    month_heb = format_month_hebrew(rd.get('for_month', ''))
    body = f"שלום,\n\nמצורפת קבלה מספר {receipt_number}"
    if month_heb:
        body += f" עבור {month_heb}"
    body += "."
    notes = rd.get('notes', '')
    if notes:
        body += f"\n\n{notes}"
    body += f"\n\nבברכה,\n{issuer['name']}\n{issuer['phone']}"

    buf = generate_receipt_pdf(issuer, rd)
    ok, msg = send_receipt_email(to_email, subject, body, buf, receipt_number)
    if ok:
        conn2 = get_db()
        conn2.execute("UPDATE receipts SET emailed=1, emailed_at=datetime('now') WHERE user_id=? AND receipt_number=?", (uid, receipt_number))
        conn2.commit()
        conn2.close()
        flash(f'Email sent to {to_email}', 'success')
    else:
        flash(f'Email failed: {msg}', 'error')
    return redirect(url_for('index'))


# --- Gmail IMAP ---

def decode_mime_header(raw):
    """Decode MIME-encoded header (subject, from, etc.)."""
    parts = decode_header(raw or '')
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or 'utf-8', errors='replace'))
        else:
            decoded.append(data)
    return ''.join(decoded)


def get_email_body(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get('Content-Disposition', ''))
            if ct == 'text/plain' and 'attachment' not in disp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                return payload.decode(charset, errors='replace')
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get('Content-Disposition', ''))
            if ct == 'text/html' and 'attachment' not in disp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                html = payload.decode(charset, errors='replace')
                return re.sub(r'<[^>]+>', '', html).strip()
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        return payload.decode(charset, errors='replace') if payload else ''
    return ''


def _imap_connect():
    settings = get_email_settings()
    if not settings or not settings['smtp_email'] or not settings['smtp_password']:
        return None, None, 'Gmail not configured'
    imap_host = settings['smtp_host'].replace('smtp.', 'imap.')
    try:
        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(settings['smtp_email'], settings['smtp_password'])
        mail.select('[Gmail]/Sent Mail', readonly=True)
        return mail, settings, None
    except Exception as e:
        # Fallback to INBOX if sent mail folder name differs
        try:
            mail = imaplib.IMAP4_SSL(imap_host)
            mail.login(settings['smtp_email'], settings['smtp_password'])
            mail.select('"[Gmail]/Sent Mail"', readonly=True)
            return mail, settings, None
        except:
            pass
        return None, None, str(e)


def _find_receipt_attachment(msg):
    """Find receipt-600xx PDF attachment in email."""
    if not msg.is_multipart():
        return None
    for part in msg.walk():
        disp = str(part.get('Content-Disposition', ''))
        if 'attachment' not in disp:
            continue
        filename = part.get_filename()
        if filename:
            filename = decode_mime_header(filename)
            if re.search(r'(?:receipt|קבלה)[\-_]?600\d+', filename, re.IGNORECASE):
                return filename
    return None


def fetch_gmail_emails(max_results=30):
    """Find sent emails containing receipt-600xx PDF attachments."""
    mail, settings, error = _imap_connect()
    if error:
        return [], error

    try:
        status, data = mail.search(None, 'ALL')
        if status != 'OK':
            mail.logout()
            return [], 'Search failed'

        all_ids = data[0].split()
        # Scan from newest
        all_ids.reverse()

        results = []
        scanned = 0
        for eid in all_ids:
            if len(results) >= max_results or scanned > 300:
                break
            scanned += 1
            status, msg_data = mail.fetch(eid, '(RFC822)')
            if status != 'OK':
                continue
            msg = emaillib.message_from_bytes(msg_data[0][1])
            attachment_name = _find_receipt_attachment(msg)
            if not attachment_name:
                continue

            subject = decode_mime_header(msg.get('Subject', ''))
            from_addr = decode_mime_header(msg.get('From', ''))
            date_raw = msg.get('Date', '')
            # Parse date for display
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_raw)
                date_short = dt.strftime('%d/%m/%Y')
            except:
                date_short = date_raw[:16]

            results.append({
                'id': eid.decode(),
                'subject': subject,
                'from': from_addr,
                'date': date_raw,
                'date_short': date_short,
                'attachment': attachment_name,
            })

        mail.logout()
        return results, None
    except Exception as e:
        try:
            mail.logout()
        except:
            pass
        return [], str(e)


def fetch_single_email(email_id):
    """Fetch full email body and attachment info by ID."""
    mail, settings, error = _imap_connect()
    if error:
        return None, error

    try:
        status, msg_data = mail.fetch(email_id.encode(), '(RFC822)')
        if status != 'OK':
            mail.logout()
            return None, 'Email not found'

        msg = emaillib.message_from_bytes(msg_data[0][1])
        subject = decode_mime_header(msg.get('Subject', ''))
        body = get_email_body(msg)
        from_addr = decode_mime_header(msg.get('From', ''))
        attachment_name = _find_receipt_attachment(msg)

        mail.logout()
        return {
            'subject': subject,
            'body': body,
            'from': from_addr,
            'full_text': f"{subject}\n{body}",
            'attachment_name': attachment_name,
            'has_attachment': bool(attachment_name),
        }, None
    except Exception as e:
        try:
            mail.logout()
        except:
            pass
        return None, str(e)


def fetch_email_attachment(email_id):
    """Download the receipt PDF attachment from an email."""
    mail, settings, error = _imap_connect()
    if error:
        return None, None, error

    try:
        status, msg_data = mail.fetch(email_id.encode(), '(RFC822)')
        if status != 'OK':
            mail.logout()
            return None, None, 'Email not found'

        msg = emaillib.message_from_bytes(msg_data[0][1])
        for part in msg.walk():
            disp = str(part.get('Content-Disposition', ''))
            if 'attachment' not in disp:
                continue
            filename = part.get_filename()
            if filename:
                filename = decode_mime_header(filename)
                if re.search(r'(?:receipt|קבלה)[\-_]?600\d+', filename, re.IGNORECASE):
                    payload = part.get_payload(decode=True)
                    mail.logout()
                    return payload, filename, None

        mail.logout()
        return None, None, 'Attachment not found'
    except Exception as e:
        try:
            mail.logout()
        except:
            pass
        return None, None, str(e)


# --- From Message ---

@app.route('/from-message')
@login_required
def from_message_page():
    uid = current_user_id()
    conn = get_db()
    clients = conn.execute('SELECT * FROM clients WHERE user_id=? AND active=1 ORDER BY name', (uid,)).fetchall()
    conn.close()
    issuer = get_issuer()
    return render_template('from_message.html',
                           clients=[dict(c) for c in clients],
                           next_number=get_next_receipt_number(),
                           today=date.today().strftime('%Y-%m-%d'),
                           issuer_hebrew_name=issuer.get('hebrew_name', '') if issuer else '')


@app.route('/api/parse-message', methods=['POST'])
@login_required
def api_parse_message():
    text = request.json.get('text', '')
    uid = current_user_id()
    conn = get_db()
    clients = [dict(c) for c in conn.execute('SELECT * FROM clients WHERE user_id=? AND active=1', (uid,)).fetchall()]
    conn.close()
    result = parse_message(text, clients)
    return jsonify(result)


@app.route('/api/gmail/list')
@login_required
def api_gmail_list():
    emails, error = fetch_gmail_emails()
    if error:
        return jsonify(emails=[], error=error)
    return jsonify(emails=emails, error=None)


@app.route('/api/gmail/read/<email_id>')
@login_required
def api_gmail_read(email_id):
    email_data, error = fetch_single_email(email_id)
    if error:
        return jsonify(error=error)
    uid = current_user_id()
    conn = get_db()
    clients = [dict(c) for c in conn.execute('SELECT * FROM clients WHERE user_id=? AND active=1', (uid,)).fetchall()]
    conn.close()
    parsed = parse_message(email_data['full_text'], clients)
    return jsonify(
        subject=email_data['subject'],
        body=email_data['body'],
        parsed=parsed,
        attachment_name=email_data.get('attachment_name'),
        has_attachment=email_data.get('has_attachment', False),
        error=None,
    )


@app.route('/api/gmail/attachment/<email_id>')
@login_required
def api_gmail_attachment(email_id):
    data, filename, error = fetch_email_attachment(email_id)
    if error:
        flash(f'Error downloading attachment: {error}', 'error')
        return redirect(url_for('from_message_page'))
    buf = io.BytesIO(data)
    return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/pdf')


# --- Client Management ---

@app.route('/clients')
@login_required
def clients_page():
    conn = get_db()
    clients = conn.execute('SELECT * FROM clients WHERE user_id=? ORDER BY active DESC, name', (current_user_id(),)).fetchall()
    conn.close()
    return render_template('clients.html', clients=[dict(c) for c in clients])


@app.route('/clients/add', methods=['POST'])
@login_required
def add_client():
    conn = get_db()
    conn.execute('INSERT INTO clients(user_id,name,company_id,phone,email,nickname) VALUES(?,?,?,?,?,?)',
                 (current_user_id(), request.form['name'].strip(), request.form['company_id'].strip(),
                  request.form['phone'].strip(), request.form.get('email', '').strip(),
                  request.form.get('nickname', '').strip()))
    conn.commit()
    conn.close()
    flash('Client added', 'success')
    return redirect(url_for('clients_page'))


@app.route('/clients/edit/<int:client_id>', methods=['POST'])
@login_required
def edit_client(client_id):
    active = 1 if request.form.get('active') == 'on' else 0
    conn = get_db()
    conn.execute('UPDATE clients SET name=?,company_id=?,phone=?,email=?,nickname=?,active=? WHERE id=? AND user_id=?',
                 (request.form['name'].strip(), request.form['company_id'].strip(),
                  request.form['phone'].strip(), request.form.get('email', '').strip(),
                  request.form.get('nickname', '').strip(), active, client_id, current_user_id()))
    conn.commit()
    conn.close()
    flash('Client updated', 'success')
    return redirect(url_for('clients_page'))


# --- Settings ---

@app.route('/settings')
@login_required
def settings_page():
    return render_template('settings.html',
                           issuer=get_issuer(),
                           next_number=get_next_receipt_number(),
                           email_settings=get_email_settings())


@app.route('/settings/issuer', methods=['POST'])
@login_required
def update_issuer():
    conn = get_db()
    conn.execute('UPDATE issuer SET name=?,tax_id=?,address=?,phone=?,email=?,hebrew_name=? WHERE user_id=?',
                 (request.form['name'].strip(), request.form['tax_id'].strip(),
                  request.form['address'].strip(), request.form['phone'].strip(),
                  request.form['email'].strip(), request.form.get('hebrew_name', '').strip(),
                  current_user_id()))
    conn.commit()
    conn.close()
    flash('Issuer details updated', 'success')
    return redirect(url_for('settings_page'))


@app.route('/settings/sequence', methods=['POST'])
@login_required
def update_sequence():
    set_receipt_number(int(request.form['next_number']))
    flash('Next receipt number updated', 'success')
    return redirect(url_for('settings_page'))


@app.route('/settings/email', methods=['POST'])
@login_required
def update_email_settings():
    raw_password = request.form['smtp_password'].strip()
    encrypted_password = encrypt_value(raw_password)
    conn = get_db()
    conn.execute('UPDATE email_settings SET smtp_email=?,smtp_password=?,smtp_host=?,smtp_port=? WHERE user_id=?',
                 (request.form['smtp_email'].strip(), encrypted_password,
                  request.form.get('smtp_host', 'smtp.gmail.com').strip(),
                  int(request.form.get('smtp_port', 587)), current_user_id()))
    conn.commit()
    conn.close()
    flash('Email settings saved (password encrypted)', 'success')
    return redirect(url_for('settings_page'))


@app.route('/settings/backup')
@login_required
def download_backup():
    buf = io.BytesIO()
    conn = get_db()
    uid = current_user_id()
    data = {
        'issuer': dict(conn.execute('SELECT * FROM issuer WHERE user_id=?', (uid,)).fetchone() or {}),
        'sequence': dict(conn.execute('SELECT * FROM sequence WHERE user_id=?', (uid,)).fetchone() or {}),
        'email_settings': dict(conn.execute('SELECT * FROM email_settings WHERE user_id=?', (uid,)).fetchone() or {}),
        'clients': [dict(r) for r in conn.execute('SELECT * FROM clients WHERE user_id=?', (uid,)).fetchall()],
        'receipts': [dict(r) for r in conn.execute('SELECT * FROM receipts WHERE user_id=?', (uid,)).fetchall()],
    }
    conn.close()
    buf.write(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f'backup-{date.today()}.json', mimetype='application/json')


if __name__ == '__main__':
    app.run(debug=True, port=5050, use_reloader=False)
