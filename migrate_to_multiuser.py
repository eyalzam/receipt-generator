#!/usr/bin/env python3
"""
One-time migration: convert single-user receipts.db to multi-user schema.
Run this ONCE after updating app.py to the multi-user version.

Usage:
    python migrate_to_multiuser.py [username] [password]

Example:
    python migrate_to_multiuser.py eyal my_password
"""
import os
import sys
import sqlite3
import hashlib
import secrets

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'receipts.db')


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def needs_migration(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'users' in tables:
        count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        if count > 0:
            return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(issuer)").fetchall()}
    return 'user_id' not in cols


def migrate(username, password):
    if not os.path.exists(DB_PATH):
        print("No receipts.db found. Start the app to create a fresh database.")
        return

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if not needs_migration(conn):
        print("Database is already migrated or has users. Skipping.")
        conn.close()
        return

    print(f"Migrating existing data to multi-user schema for user '{username}'...")

    old_issuer = conn.execute('SELECT * FROM issuer WHERE id=1').fetchone()
    old_sequence = conn.execute('SELECT next_number FROM sequence WHERE id=1').fetchone()
    old_email = conn.execute('SELECT * FROM email_settings WHERE id=1').fetchone()
    old_clients = conn.execute('SELECT * FROM clients').fetchall()
    old_receipts = conn.execute('SELECT * FROM receipts').fetchall()

    issuer_data = dict(old_issuer) if old_issuer else {}
    seq_num = old_sequence['next_number'] if old_sequence else 1
    email_data = dict(old_email) if old_email else {}
    clients_data = [dict(c) for c in old_clients]
    receipts_data = [dict(r) for r in old_receipts]

    conn.execute("DROP TABLE IF EXISTS receipts")
    conn.execute("DROP TABLE IF EXISTS clients")
    conn.execute("DROP TABLE IF EXISTS issuer")
    conn.execute("DROP TABLE IF EXISTS sequence")
    conn.execute("DROP TABLE IF EXISTS email_settings")

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

    conn.execute('INSERT INTO users(username, password, display_name) VALUES(?,?,?)',
                 (username, hash_password(password), issuer_data.get('name', '')))
    user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    conn.execute(
        'INSERT INTO issuer(user_id,name,tax_id,address,phone,email,hebrew_name) VALUES(?,?,?,?,?,?,?)',
        (user_id, issuer_data.get('name', ''), issuer_data.get('tax_id', ''),
         issuer_data.get('address', ''), issuer_data.get('phone', ''),
         issuer_data.get('email', ''), '')
    )

    conn.execute('INSERT INTO sequence(user_id,next_number) VALUES(?,?)', (user_id, seq_num))

    conn.execute('INSERT INTO email_settings(user_id,smtp_email,smtp_password,smtp_host,smtp_port) VALUES(?,?,?,?,?)',
                 (user_id, email_data.get('smtp_email', ''), email_data.get('smtp_password', ''),
                  email_data.get('smtp_host', 'smtp.gmail.com'), email_data.get('smtp_port', 587)))

    old_to_new_client = {}
    for c in clients_data:
        conn.execute(
            'INSERT INTO clients(user_id,name,company_id,phone,email,nickname,active) VALUES(?,?,?,?,?,?,?)',
            (user_id, c['name'], c['company_id'], c['phone'],
             c.get('email', ''), c.get('nickname', ''), c.get('active', 1))
        )
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        old_to_new_client[c['id']] = new_id

    for r in receipts_data:
        new_client_id = old_to_new_client.get(r['client_id'], r['client_id'])
        conn.execute(
            'INSERT INTO receipts(user_id,receipt_number,client_id,date,amount,notes,for_month,email_subject,emailed,emailed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (user_id, r['receipt_number'], new_client_id, r['date'], r['amount'],
             r.get('notes', ''), r.get('for_month', ''), r.get('email_subject', ''),
             r.get('emailed', 0), r.get('emailed_at', ''), r.get('created_at', ''))
        )

    conn.commit()
    conn.close()
    print(f"Migration complete! User '{username}' created with all existing data.")
    print(f"  - {len(clients_data)} clients migrated")
    print(f"  - {len(receipts_data)} receipts migrated")
    print(f"  - Next receipt number: {seq_num}")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python migrate_to_multiuser.py <username> <password>")
        print("Example: python migrate_to_multiuser.py eyal my_password")
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
