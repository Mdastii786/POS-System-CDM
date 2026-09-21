# app.py - TuckshopPOS (single-file Flask + SQLite)
#
# Fixed version. Changes from the original:
#   1. ONE Flask app only - page routes and API routes live on the same object.
#   2. Dates stored as ISO ("YYYY-MM-DD HH:MM:SS") so LIKE / >= / date() all work.
#      A one-time migration converts any legacy "DD/MM/YYYY" rows automatically.
#   3. /api/archive/run actually works (correct column names, correct PRAGMA
#      parsing, INSERT OR REPLACE, per-table date columns, row counts returned).
#   4. Account payments: /api/customers/pay records a PAYMENT transaction and
#      reduces balance_owed; cash-up counts those payments as incoming cash/card.
#   5. POS prices come from the database, never from the client. The price and
#      cost at time of sale are frozen onto the transaction row (unit_price,
#      unit_cost, subtotal), so cash-up and exports stay correct even after
#      price changes.
#   6. Stock deduction is atomic (UPDATE ... WHERE qty_on_hand >= qty), which
#      also fixes the check-then-update race under threaded=True.
#   7. Cash-up is idempotent: re-saving the same day replaces the rows instead
#      of stacking duplicates.
#   8. Customer renames propagate to transactions/accounts so statements and
#      balances don't get orphaned.
#   9. request.get_json(silent=True) everywhere, qty/amount validation,
#      sqlite WAL mode + busy timeout, safe backups via the sqlite backup API.
#  10. If pywebview is missing, the server now actually keeps running instead
#      of printing a message and exiting.

import atexit
import csv
import io
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
from datetime import date, datetime, timedelta

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from urllib.parse import quote

# --------------------------------------------------------------------------- #
# Optional desktop window (pywebview). App still works headless without it.
# --------------------------------------------------------------------------- #
try:
    import webview
except Exception:
    webview = None

# --------------------------------------------------------------------------- #
# Paths. Two different worlds:
#   BUNDLE_DIR = read-only files packaged INTO the exe (templates, static)
#   DATA_DIR   = writable data that must SURVIVE between runs (db, backups…)
# When frozen by PyInstaller, templates/static extract to a temp folder
# (_MEIPASS) - if the database lived there too, it would be wiped on every
# launch. So data always lives NEXT TO the .exe instead.
# --------------------------------------------------------------------------- #
if getattr(sys, "frozen", False):                    # running as a .exe
    BUNDLE_DIR = sys._MEIPASS                        # bundled read-only files
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BUNDLE_DIR = os.path.abspath(os.path.dirname(__file__))
    DATA_DIR = BUNDLE_DIR

DB_PATH = os.path.join(DATA_DIR, "tuckshop.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archives")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

DATE_FORMAT = "%Y-%m-%d"                    # storage format (ISO: sortable, comparable)
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DISPLAY_DATE_FORMAT = "%d/%m/%Y"            # SA format - for display only (templates/JS)

ARCHIVE_TABLES = {                          # table -> the column holding its date
    "transactions": "datetime",
    "accounts": "datetime",
    "purchases": "datetime",                # shop purchases archive too
    "reminders": "datetime",                # WhatsApp reminder log archives too
    "budget": "date",
    "daily_summary": "date",
}

# Card markup charged TO THE CUSTOMER on card payments, in percent.
# A card sale of R100.00 charges the customer's card R102.50: the R2.50
# is stored on the transaction (card_fee column) and counts toward the
# expected card total on the cash-up page. Change the percent here.
CARD_FEE_PERCENT = 2.5

# THE BAN (account limit): an account sale may never push what a customer
# owes past this amount. Over the line = ACCOUNT BANNED until they pay some
# of it down (cash/card always works). Change the rand limit here.
ACCOUNT_LIMIT = 2500.0

app = Flask(__name__,
            static_folder=os.path.join(BUNDLE_DIR, "static"),
            template_folder=os.path.join(BUNDLE_DIR, "templates"))

# Preview mode (SET ONLY for the web preview): browsers sitting in a
# cross-site iframe refuse SameSite=Lax session cookies, which looks like
# "the login button does nothing" - the login actually works but the next
# page forgets it. SameSite=None + Secure lets the preview work over the
# HTTPS proxy. The shipped desktop app never sets this env var, so its
# localhost behaviour is untouched.
if os.environ.get("POS_PREVIEW"):
    app.config["SESSION_COOKIE_SAMESITE"] = "None"
    app.config["SESSION_COOKIE_SECURE"] = True

@app.after_request
def _no_stale_copies(resp):
    """Browsers eagerly cache /static and HTML for hours - so after an
    update the app can LOOK like the old build until someone thinks of
    Ctrl+F5 (that is exactly what made the new tray 'not show up' in the
    preview). Everything here is served over the local network, caching
    buys us nothing: always revalidate."""
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# Session secret: generated once, stored next to the database.
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")
if os.path.exists(SECRET_FILE):
    app.secret_key = open(SECRET_FILE).read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(SECRET_FILE, "w") as f:
        f.write(app.secret_key)

try:
    app.json.sort_keys = False              # Flask >= 2.2
except Exception:
    app.config["JSON_SORT_KEYS"] = False    # older Flask


# --------------------------------------------------------------------------- #
# Login / roles
# --------------------------------------------------------------------------- #
# Pages visible after login. Management additionally gets: /reports, all
# report/archive/export APIs AND the stock-in UNDO (delete) function.
# To change what Tellers can see, edit this:
def needs_management(path):
    if path == "/reports":
        return True
    if path.startswith(("/api/reports", "/api/archive")):
        return True
    if path.startswith("/api/export") and not path.startswith("/api/export/stockins"):
        return True
    if path.startswith(("/api/stock_in/reverse", "/api/stock_in/edit")):
        return True      # deleting / editing stock-ins
    if path.startswith("/api/product/delete"):     # deleting products
        return True
    if path.startswith("/api/product/adjust"):     # add/correct stock counts
        return True
    if path.startswith("/api/customers/swap"):     # exchanging account items
        return True
    if path.startswith("/api/customers/add_debt"):  # typing in old debt
        return True
    if path.startswith("/api/sales/void"):         # wiping whole test/mistake sales
        return True
    if path == "/expenses":                         # shop & losses page
        return True
    if path.startswith(("/api/expenses", "/api/backup")):
        return True                                 # money-out log, backups
    return False


@app.before_request
def guard():
    path = request.path
    if path.startswith(("/login", "/logout", "/static", "/favicon",
                        "/api/server_info")):
        return None
    if not session.get("uid"):
        if path.startswith("/api/"):
            return err("Not logged in", 401)
        return redirect(url_for("login"))
    if session.get("role") != "management" and needs_management(path):
        if path.startswith("/api/"):
            return err("Management access only", 403)
        return redirect(url_for("page_stock"))
    # FLOAT GATE: once per calendar day the first page anyone opens is the
    # cash-up screen until today's float is set - it can never be skipped.
    # APIs stay open so live-sync keeps working behind the redirect.
    if request.method == "GET" and path in ("/", "/stock", "/sales",
                                            "/customers", "/reports"):
        if not get_db().execute("SELECT 1 FROM day_float WHERE date = ?",
                                (today_str(),)).fetchone():
            return redirect(url_for("page_cashup", float="start"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    db = get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pin = request.form.get("pin", "").strip()
        row = db.execute("SELECT * FROM users WHERE username = ?",
                         (username,)).fetchone()
        if row and check_password_hash(row["pin_hash"], pin):
            session.clear()
            session["uid"] = row["id"]
            session["name"] = row["username"]
            session["role"] = row["role"]
            return redirect(url_for("page_stock"))
        error = "Wrong PIN for that role - try again"
    # The login page shows exactly two fixed choices (Management / Teller);
    # no user list is ever rendered.
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/users/set_pin", methods=["POST"])
def api_users_set_pin():
    """Change a login PIN. Tellers can only change their OWN PIN. Management
    can change EITHER PIN (target='manager' | 'teller') - but must always
    prove themselves with their own current PIN. 'confirm' must match 'new'
    so a typo can't lock anyone out."""
    data = request.get_json(silent=True) or {}
    current = str(data.get("current", ""))
    new = str(data.get("new", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    target = str(data.get("target", "")).strip().lower()
    if not (new.isdigit() and len(new) >= 4):
        return err("New PIN must be at least 4 digits (numbers only)")
    if new != confirm:
        return err("The two new PINs don't match - type them again")
    db = get_db()
    actor = db.execute("SELECT * FROM users WHERE id = ?",
                       (session["uid"],)).fetchone()
    if not actor or not check_password_hash(actor["pin_hash"], current):
        return err("Your current PIN is wrong", 403)
    if target in ("manager", "teller"):
        if actor["role"] != "management":
            return err("Management access only", 403)
        row = db.execute("SELECT * FROM users WHERE username = ?",
                         (target,)).fetchone()
        if not row:                     # fall back to whoever holds that role
            role = "management" if target == "manager" else "teller"
            row = db.execute(
                "SELECT * FROM users WHERE role = ? ORDER BY id LIMIT 1",
                (role,)).fetchone()
    else:
        row = actor                     # no target = change your own PIN
    if not row:
        return err("User not found", 404)
    db.execute("UPDATE users SET pin_hash = ? WHERE id = ?",
               (generate_password_hash(new), row["id"]))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "changed": row["username"]})


# --------------------------------------------------------------------------- #
# Database plumbing
# --------------------------------------------------------------------------- #
def get_db():
    """One connection per request, stored on Flask's `g`."""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read/write
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        cost REAL DEFAULT 0,
        price REAL DEFAULT 0,
        qty_on_hand INTEGER DEFAULT 0,
        reorder_level INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        phone TEXT,
        balance_owed REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime TEXT NOT NULL,
        barcode TEXT DEFAULT '',
        item_name TEXT DEFAULT '',
        type TEXT NOT NULL,              -- IN | OUT | PAYMENT
        qty INTEGER DEFAULT 0,
        customer TEXT DEFAULT 'CASH',
        payment TEXT DEFAULT 'CASH',     -- CASH | CARD | ACCOUNT | N/A
        unit_price REAL,                 -- price frozen at time of sale
        unit_cost REAL,                  -- cost frozen at time of sale
        subtotal REAL,                   -- OUT: price*qty, PAYMENT: amount
        card_fee REAL DEFAULT 0,         -- 2.5% markup charged on CARD payments
        note TEXT DEFAULT ''             -- audit note (edited / undo reasons…)
    );
    CREATE INDEX IF NOT EXISTS idx_tx_datetime ON transactions(datetime);
    CREATE INDEX IF NOT EXISTS idx_tx_customer ON transactions(customer);
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime TEXT,
        customer TEXT,
        total REAL,
        payment TEXT
    );
    CREATE TABLE IF NOT EXISTS daily_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        total_sales REAL,
        total_cogs REAL,
        profit REAL,
        items_sold INTEGER
    );
    CREATE TABLE IF NOT EXISTS budget (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        expected_cash REAL,
        actual_cash REAL,
        difference REAL,
        "float" REAL
    );
    CREATE TABLE IF NOT EXISTS day_float (
        date TEXT PRIMARY KEY,       -- morning float set per day
        amount REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime TEXT,               -- when it was recorded
        shop TEXT,                   -- which shop the stock was bought from
        amount REAL,                 -- how much was spent there
        note TEXT                    -- what was bought (optional)
    );
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        datetime TEXT,               -- when the reminder was fired
        customer_id INTEGER,
        customer TEXT,               -- name frozen at send time
        balance REAL,                -- balance quoted in the message
        channel TEXT DEFAULT 'WHATSAPP'
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pin_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'teller'    -- 'management' | 'teller'
    );
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value INTEGER DEFAULT 0
    );
    """)
    # data_version powers the LIVE SYNC between laptops: every change bumps it.
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('data_version', 0)")

    # Seed default logins on first run - CHANGE THESE PINS after first login.
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.execute("INSERT INTO users (username, pin_hash, role) VALUES (?,?,?)",
                     ("manager", generate_password_hash("1357"), "management"))
        conn.execute("INSERT INTO users (username, pin_hash, role) VALUES (?,?,?)",
                     ("teller", generate_password_hash("0000"), "teller"))

    conn.commit()
    _ensure_columns(conn)
    _backfill_transaction_prices(conn)
    _migrate_legacy_dates(conn)
    conn.commit()
    conn.close()


def _ensure_columns(conn):
    """Add new columns to DBs created by the old version of this app."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    for col in ("unit_price", "unit_cost", "subtotal", "card_fee"):
        if col not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} REAL DEFAULT 0")
    # BUILD 20: tracked separately so they survive on old databases too.
    if "returned_qty" not in existing:
        conn.execute("ALTER TABLE transactions ADD COLUMN "
                     "returned_qty INTEGER DEFAULT 0")
    if "active" not in {r[1] for r in conn.execute("PRAGMA table_info(customers)")}:
        conn.execute("ALTER TABLE customers ADD COLUMN active INTEGER DEFAULT 1")
    # BUILD 26: the carried-over snapshot. Balances already accumulate
    # forever (archiving never resets them); these columns just REMEMBER how
    # much of the balance was carried in from earlier months, and when.
    cust_cols = {r[1] for r in conn.execute("PRAGMA table_info(customers)")}
    if "carried_over" not in cust_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN carried_over REAL DEFAULT 0")
    if "carried_month" not in cust_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN carried_month TEXT DEFAULT ''")
    if "active" not in {r[1] for r in conn.execute("PRAGMA table_info(products)")}:
        conn.execute("ALTER TABLE products ADD COLUMN active INTEGER DEFAULT 1")
    if "note" not in existing:
        conn.execute("ALTER TABLE transactions ADD COLUMN note TEXT DEFAULT ''")
    conn.commit()


def _backfill_transaction_prices(conn):
    """Old OUT rows have no frozen prices - approximate from current products."""
    conn.execute("""
        UPDATE transactions SET unit_price = (
            SELECT price FROM products WHERE products.barcode = transactions.barcode
        ) WHERE type = 'OUT' AND unit_price IS NULL
    """)
    conn.execute("""
        UPDATE transactions SET unit_cost = (
            SELECT cost FROM products WHERE products.barcode = transactions.barcode
        ) WHERE type = 'OUT' AND unit_cost IS NULL
    """)
    conn.execute("""
        UPDATE transactions SET subtotal = unit_price * qty
        WHERE type = 'OUT' AND subtotal IS NULL AND unit_price IS NOT NULL
    """)
    conn.commit()


def _to_iso(value):
    """Convert a legacy DD/MM/YYYY (optionally with time) string to ISO."""
    if not value or not isinstance(value, str):
        return value
    if len(value) >= 5 and value[4] == "-":          # already ISO
        return value
    for old_fmt, new_fmt in (
        ("%d/%m/%Y %H:%M:%S", DATETIME_FORMAT),
        ("%d/%m/%Y", DATE_FORMAT),
    ):
        try:
            return datetime.strptime(value, old_fmt).strftime(new_fmt)
        except ValueError:
            continue
    return value


def _migrate_legacy_dates(conn):
    """One-time fix-up for rows written before the ISO switch."""
    for table, col in (("transactions", "datetime"), ("accounts", "datetime"),
                       ("budget", "date"), ("daily_summary", "date")):
        try:
            rows = conn.execute(f"SELECT id, {col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        for rid, val in rows:
            new_val = _to_iso(val)
            if new_val != val:
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?",
                             (new_val, rid))
    conn.commit()


init_db()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_str():
    return datetime.now().strftime(DATETIME_FORMAT)


def today_str():
    return date.today().strftime(DATE_FORMAT)


def wa_number(phone):
    """Normalise a SA cellphone number to wa.me international digits.
    Returns '' when the number is not usable, so the popup can say so
    BEFORE anyone tries to send. Handles '082 123 4567', '+27 82...',
    '002782...' and plain '821234567'."""
    d = re.sub(r"\D", "", str(phone or ""))
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("27") and len(d) >= 11:
        return d
    if d.startswith("0"):
        d = d[1:]
    if len(d) == 9:
        return "27" + d
    return ""


def reminder_message(name, balance):
    """The friendly default nudge: name + exact balance straight from the
    database, so nobody is ever asked to pay the wrong amount."""
    return (f"Hi {name}, a friendly reminder from the tuckshop: your "
            f"account balance is R{balance:.2f} as at "
            f"{date.today().strftime('%d %b %Y')}. Please pop in to settle "
            f"when you can. Thank you!")


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def err(message, status=400):
    return jsonify({"error": message}), status


def bump_version(db):
    """LIVE SYNC: every mutating endpoint bumps this once. All screens on
    all laptops poll /api/data_version and refresh themselves when it
    changes, so a sale on one PC shows up on the others automatically."""
    db.execute("UPDATE meta SET value = value + 1 WHERE key = 'data_version'")


@app.route("/api/data_version")
def api_data_version():
    row = get_db().execute("SELECT value FROM meta WHERE key = 'data_version'"
                           ).fetchone()
    return jsonify({"version": row["value"] if row else 0})


def _lan_ip():
    """Best-guess LAN IP of THIS laptop, for sharing the app over Wi-Fi.
    Uses a UDP 'connect' - no traffic is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "127.0.0.1"
    finally:
        s.close()
    return ip


@app.route("/api/server_info")
def api_server_info():
    """Open (no login) - the login screen shows this so a second laptop
    on the same Wi-Fi knows what address to type into its browser."""
    return jsonify({
        "lan_url": f"http://{_lan_ip()}:5000",
        "hostname": socket.gethostname(),
    })


def card_fee_for(amount):
    """CARD_FEE_PERCENT markup on an amount, rounded half-up to whole
    cents using INTEGER maths only (never float rounding), so it is
    identical to cardFee() in common.js - the popups always preview
    exactly what gets charged.  R1.00 -> R0.03, R100.00 -> R2.50."""
    cents = int(round(_f(amount) * 100))
    per_mille = int(round(CARD_FEE_PERCENT * 10))      # 2.5% -> 25
    fee_cents = (cents * per_mille + 500) // 1000      # +500 = round half up
    return fee_cents / 100.0


def compute_summary(db, day):
    """Single source of truth for a day's numbers (used by cash-up + save)."""
    rows = db.execute(
        "SELECT * FROM transactions WHERE datetime LIKE ? ORDER BY id",
        (f"{day}%",)).fetchall()

    expected_cash = expected_card = expected_account = 0.0
    total_sales = total_cogs = 0.0
    card_fees = 0.0
    items_sold = 0

    for r in rows:
        pay = (r["payment"] or "").upper()
        fee = r["card_fee"] or 0.0
        if r["type"] == "OUT":
            subtotal = r["subtotal"]
            if subtotal is None:
                subtotal = (r["unit_price"] or 0) * (r["qty"] or 0)
            total_sales += subtotal
            total_cogs += (r["unit_cost"] or 0) * (r["qty"] or 0)
            items_sold += r["qty"] or 0
            if pay == "CASH":
                expected_cash += subtotal
            elif pay == "CARD":
                expected_card += subtotal + fee   # bank gets goods + markup
                card_fees += fee
            elif pay == "ACCOUNT":
                expected_account += subtotal
        elif r["type"] == "PAYMENT":          # customer settling an account
            amount = r["subtotal"] or 0
            if pay == "CASH":
                expected_cash += amount
            elif pay == "CARD":
                expected_card += amount + fee
                card_fees += fee
        elif r["type"] == "RETURN":           # exchange/return - money reversed
            # subtotal & card_fee are stored NEGATIVE on RETURN rows, so they
            # pull the day's totals down automatically and the cash-up pages
            # stay truthful (a cash refund really leaves the drawer).
            subtotal = r["subtotal"] or 0
            total_sales += subtotal
            total_cogs -= (r["unit_cost"] or 0) * (r["qty"] or 0)
            items_sold -= r["qty"] or 0
            if pay == "CASH":
                expected_cash += subtotal
            elif pay == "CARD":
                expected_card += subtotal + fee
                card_fees += fee
            elif pay == "ACCOUNT":
                expected_account += subtotal

    row = db.execute("SELECT amount FROM day_float WHERE date = ?",
                     (day,)).fetchone()
    float_today = float(row["amount"]) if row else 0.0

    return {
        "date": day,
        "float_today": round(float_today, 2),
        "expected_cash": round(expected_cash, 2),
        "expected_till": round(expected_cash + float_today, 2),
        "expected_card": round(expected_card, 2),
        "card_fee_percent": CARD_FEE_PERCENT,
        "total_card_fees": round(card_fees, 2),
        "expected_account": round(expected_account, 2),
        "total_sales": round(total_sales, 2),
        "total_cogs": round(total_cogs, 2),
        "profit": round(total_sales - total_cogs, 2),
        "items_sold": items_sold,
        "total_returns": round(sum(
            -(r["subtotal"] or 0) for r in rows if r["type"] == "RETURN"), 2),
    }


# --------------------------------------------------------------------------- #
# Page routes - five completely separate pages sharing base.html
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return redirect(url_for("page_stock"))


@app.route("/stock")
def page_stock():
    return render_template("stock.html")


@app.route("/sales")
def page_sales():
    return render_template("sales.html")


@app.route("/customers")
def page_customers():
    return render_template("customers.html")


@app.route("/cashup")
def page_cashup():
    return render_template("cashup.html")


@app.route("/reports")
def page_reports():
    return render_template("reports.html")


# --------------------------------------------------------------------------- #
# API: Products
# --------------------------------------------------------------------------- #
@app.route("/api/product/get_by_barcode", methods=["POST"])
def api_get_by_barcode():
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    if not barcode:
        return err("Missing barcode")
    row = get_db().execute(
        "SELECT * FROM products WHERE barcode = ?", (barcode,)).fetchone()
    return jsonify(dict(row) if row else {"found": False})


@app.route("/api/product/create", methods=["POST"])
def api_product_create():
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    name = str(data.get("name", "")).strip()
    cost, price = _f(data.get("cost")), _f(data.get("price"))
    qty, reorder = _i(data.get("qty_on_hand")), _i(data.get("reorder_level"))
    if not barcode or not name:
        return err("barcode and name required")
    if cost < 0 or price < 0 or qty < 0:
        return err("cost, price and qty cannot be negative")
    db = get_db()
    try:
        db.execute("""INSERT INTO products
                      (barcode, name, cost, price, qty_on_hand, reorder_level)
                      VALUES (?,?,?,?,?,?)""",
                   (barcode, name, cost, price, qty, reorder))
        # Every unit that enters must have a receipt, or the stock reality
        # check report can't balance - so creating stock logs an IN row too.
        if qty > 0:
            db.execute("""INSERT INTO transactions
                          (datetime, barcode, item_name, type, qty, customer,
                           payment) VALUES (?,?,?,?,?,?,?)""",
                       (now_str(), barcode, name, "IN", qty, "SUPPLIER", "N/A"))
        bump_version(db)
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return err("Barcode already exists")
    return jsonify({"ok": True})


@app.route("/api/product/update", methods=["POST"])
def api_product_update():
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    if not barcode:
        return err("barcode required")
    fields, params = [], []
    for col, caster in (("name", str), ("cost", _f), ("price", _f),
                        ("qty_on_hand", _i), ("reorder_level", _i)):
        if col in data:
            fields.append(f"{col} = ?")
            params.append(caster(data[col]) if caster is not str
                          else str(data[col]).strip())
    if not fields:
        return err("Nothing to update")
    params.append(barcode)
    db = get_db()
    cur = db.execute(f"UPDATE products SET {', '.join(fields)} WHERE barcode = ?",
                     params)
    bump_version(db)
    db.commit()
    if cur.rowcount == 0:
        return err("Product not found", 404)
    return jsonify({"ok": True})


@app.route("/api/products/list")
def api_products_list():
    rows = get_db().execute(
        "SELECT * FROM products WHERE active = 1 ORDER BY lower(name)").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/product/delete", methods=["POST"])
def api_product_delete():
    """DELETE a product (MANAGEMENT ONLY). Refuses while it still has stock
    on hand unless the UI re-sends with force=true after a second confirm.
    Past sales/stock-in HISTORY stays in transactions (item names are
    frozen on each row), so reports and archives remain complete."""
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    if not barcode:
        return err("barcode required")
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE barcode = ?",
                   (barcode,)).fetchone()
    if not p:
        return err("Product not found", 404)
    on_hand = p["qty_on_hand"] or 0
    if on_hand > 0 and not data.get("force"):
        return jsonify({
            "error": f"{p['name']} still has {on_hand} on hand.",
            "needs_force": True, "on_hand": on_hand, "name": p["name"],
        }), 409
    # Audit trail: record the removal (and how much stock went with it)
    # so reports show it instead of the product silently disappearing.
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   unit_price, unit_cost, note)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (now_str(), barcode, p["name"], "WRITE_OFF", on_hand,
                "STORE", "N/A", p["price"], p["cost"],
                "product deleted - removed from system"))
    db.execute("DELETE FROM products WHERE barcode = ?", (barcode,))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "name": p["name"],
                    "stock_removed": on_hand if data.get("force") else 0})


@app.route("/api/product/adjust", methods=["POST"])
def api_product_adjust():
    """EDIT the on-hand count of ONE product (management only) - the two
    warehouse moves the owner asked for:

      mode "add" : new units arrived -> on-hand += qty, journaled as a normal
                   IN receipt so it ACCUMULATES on the stock-in Excel and in
                   the recent-receipts API exactly like a typed stock-in.
      mode "set" : stock-take correction -> on-hand becomes qty EXACTLY,
                   journaled as EDIT so reports show the correction instead
                   of the count silently changing.

    Either way: sold history is never touched, and the day's money figures
    are never moved - stock and cash stay strangers (by design)."""
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    mode = str(data.get("mode", "")).strip().lower()
    qty = _i(data.get("qty"), -1)
    if mode not in ("add", "set"):
        return err("mode must be add or set")
    if mode == "add" and qty <= 0:
        return err("Qty to add must be more than 0")
    if mode == "set" and qty < 0:
        return err("Qty cannot be negative")
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE barcode = ?",
                   (barcode,)).fetchone()
    if not p:
        return err("Product not found - refresh and try again", 404)
    now = now_str()
    old = p["qty_on_hand"] or 0
    if mode == "add":
        new = old + qty
        db.execute("UPDATE products SET qty_on_hand = qty_on_hand + ?, "
                   "active = 1 WHERE barcode = ?", (qty, barcode))
        db.execute("""INSERT INTO transactions
                      (datetime, barcode, item_name, type, qty, customer,
                       payment, unit_price, unit_cost, note)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (now, barcode, p["name"], "IN", qty, "SUPPLIER", "N/A",
                    p["price"], p["cost"], "added via inventory edit"))
        msg = f"Added {qty} \u00d7 {p['name']} - now {new} on hand"
    else:
        new = qty
        db.execute("UPDATE products SET qty_on_hand = ?, active = 1 "
                   "WHERE barcode = ?", (qty, barcode))
        db.execute("""INSERT INTO transactions
                      (datetime, barcode, item_name, type, qty, customer,
                       payment, unit_price, unit_cost, note)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (now, barcode, p["name"], "EDIT", qty - old, "STORE",
                    "N/A", p["price"], p["cost"],
                    f"stock count corrected: {old} \u2192 {qty} on shelf"))
        msg = f"Corrected {p['name']}: {old} \u2192 {qty} on hand"
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "item": p["name"], "old": old, "new": new,
                    "mode": mode, "message": msg})


# --------------------------------------------------------------------------- #
# API: Stock in
# --------------------------------------------------------------------------- #
def _auto_barcode(db, name):
    """Invisible internal barcode generated from the item name - people
    never type or see barcodes; the system just needs a unique key."""
    slug = re.sub(r"[^A-Z0-9]", "", name.upper())[:10] or "ITEM"
    code, n = slug, 2
    while db.execute("SELECT 1 FROM products WHERE barcode = ?",
                     (code,)).fetchone():
        code = f"{slug[:9]}{n}"
        n += 1
    return code


@app.route("/api/stock_in", methods=["POST"])
def api_stock_in():
    """Receive stock BY ITEM NAME - no barcodes are ever typed. A brand-new
    item is created invisibly (auto barcode) with the sell/cost prices
    given; an existing item is simply restocked (a fresh sell price also
    updates the shelf price)."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    qty = _i(data.get("qty"))
    price = round(_f(data.get("price")), 2)
    cost = round(_f(data.get("cost")), 2)
    reorder = _i(data.get("reorder_level"), -1)
    if not name:
        return err("Type the item name")
    if qty <= 0:
        return err("Qty must be > 0")
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE lower(name) = lower(?)",
                   (name,)).fetchone()
    created = False
    if p:
        barcode = p["barcode"]
        item_name = p["name"]
        # Restocking wakes an item that was archived away at month-end.
        db.execute("UPDATE products SET qty_on_hand = qty_on_hand + ?, "
                   "active = 1 WHERE barcode = ?", (qty, barcode))
        # Fresh sell/cost prices (>0) update the product's pricing too.
        if price > 0 and abs(price - (p["price"] or 0)) > 0.0001:
            db.execute("UPDATE products SET price = ? WHERE barcode = ?",
                       (price, barcode))
        if cost > 0 and abs(cost - (p["cost"] or 0)) > 0.0001:
            db.execute("UPDATE products SET cost = ? WHERE barcode = ?",
                       (cost, barcode))
        if reorder >= 0 and reorder != (p["reorder_level"] or 0):
            db.execute("UPDATE products SET reorder_level = ? WHERE barcode = ?",
                       (reorder, barcode))
    else:
        if price <= 0:
            return err("New item needs a sell price")
        barcode = _auto_barcode(db, name)
        item_name = name
        db.execute("""INSERT INTO products
                      (barcode, name, cost, price, qty_on_hand, reorder_level)
                      VALUES (?,?,?,?,?,?)""",
                   (barcode, item_name, cost, price, qty,
                    reorder if reorder >= 0 else 5))
        created = True
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   note)
                  VALUES (?,?,?,?,?,?,?,?)""",
               (now_str(), barcode, item_name, "IN", qty, "SUPPLIER", "N/A",
                ""))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "created": created, "item": item_name,
                    "qty": qty, "barcode": barcode})


@app.route("/api/stock_in/recent")
def api_stock_in_recent():
    """ALL of today's stock-ins, newest first - visible to tellers AND
    management (the undo/edit buttons themselves are management-only).
    It used to be capped at the newest 10, which hid older entries from
    the very list people fix mistakes in; a day's worth fits fine here."""
    rows = get_db().execute(
        """SELECT * FROM transactions WHERE type = 'IN'
           AND datetime LIKE ? ORDER BY id DESC""",
        (f"{today_str()}%",)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stock_in/reverse", methods=["POST"])
def api_stock_in_reverse():
    """UNDO a stock-in: removes the received units and deletes its history
    row. Refuses if the units have since been sold (would push stock
    negative) - that way a mistake can be fixed, but never hidden."""
    data = request.get_json(silent=True) or {}
    tid = _i(data.get("transaction_id"))
    if tid <= 0:
        return err("Valid transaction id required")
    db = get_db()
    row = db.execute("SELECT * FROM transactions WHERE id = ? AND type = 'IN'",
                     (tid,)).fetchone()
    if not row:
        return err("Stock-in not found (already undone or archived?)", 404)
    qty = row["qty"] or 0
    p = db.execute("SELECT name, qty_on_hand, price, cost FROM products "
                   "WHERE barcode = ?", (row["barcode"],)).fetchone()
    if not p:
        return err("Product no longer exists - cannot reverse", 400)
    if p["qty_on_hand"] < qty:
        return err(f"Cannot undo: only {p['qty_on_hand']} × {p['name']} left "
                   f"on hand, but this stock-in added {qty}. "
                   f"Some were sold since then.", 400)
    db.execute("UPDATE products SET qty_on_hand = qty_on_hand - ? WHERE barcode = ?",
               (qty, row["barcode"]))
    db.execute("DELETE FROM transactions WHERE id = ?", (tid,))
    # Audit trail: the undo is JOURNALLED (type UNDO) so daily/monthly
    # reports show the removal instead of the stock-in silently vanishing.
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   unit_price, unit_cost, note)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (now_str(), row["barcode"], p["name"], "UNDO", qty,
                "STORE", "N/A", p["price"], p["cost"],
                f"stock-in reversed (removed from system)"))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "reversed": qty, "item": p["name"],
                    "on_hand_now": p["qty_on_hand"] - qty})


@app.route("/api/stock_in/edit", methods=["POST"])
def api_stock_in_edit():
    """EDIT a stock-in (MANAGEMENT ONLY): fix the received quantity, rename
    a mistyped item, and/or correct the sell price. Stock On Hand adjusts by
    any qty difference; every change lands in the EDIT journal so reports
    show exactly what changed."""
    data = request.get_json(silent=True) or {}
    tid = _i(data.get("transaction_id"))
    new_qty = _i(data.get("qty"), -1)
    new_name = str(data.get("name", "")).strip()
    raw_price = data.get("price")
    new_price = (round(_f(raw_price), 2)
                 if raw_price is not None and str(raw_price).strip() != ""
                 else -1)
    if tid <= 0:
        return err("Valid transaction id required")
    if new_qty < 0:
        return err("Quantity cannot be negative")
    if new_qty == 0:
        return err("Setting 0 is not allowed - use UNDO to remove it completely")
    db = get_db()
    row = db.execute("SELECT * FROM transactions WHERE id = ? AND type = 'IN'",
                     (tid,)).fetchone()
    if not row:
        return err("Stock-in not found (already undone or archived?)", 404)
    p = db.execute("SELECT name, qty_on_hand, price, cost FROM products "
                   "WHERE barcode = ?", (row["barcode"],)).fetchone()
    if not p:
        return err("Product no longer exists - cannot edit", 400)
    old_qty = row["qty"] or 0
    delta = new_qty - old_qty
    changes = []
    # --- quantity correction: adjusts Stock On Hand by the difference ---
    if delta:
        if p["qty_on_hand"] + delta < 0:
            return err(f"Cannot set {new_qty}: only {p['qty_on_hand']} × "
                       f"{p['name']} left on hand (some were sold). The lowest "
                       f"you can set is {old_qty - p['qty_on_hand']}.", 400)
        db.execute("UPDATE products SET qty_on_hand = qty_on_hand + ? "
                   "WHERE barcode = ?", (delta, row["barcode"]))
        db.execute("UPDATE transactions SET qty = ?, note = 'edited' WHERE id = ?",
                   (new_qty, tid))
        changes.append(f"qty {old_qty} → {new_qty}")
    else:
        db.execute("UPDATE transactions SET note = 'edited' WHERE id = ?",
                   (tid,))
    # --- rename (typo rescue): renames the product + this receipt line ---
    if new_name and new_name.lower() != (p["name"] or "").lower():
        clash = db.execute("SELECT 1 FROM products WHERE lower(name) = lower(?) "
                           "AND barcode != ?",
                           (new_name, row["barcode"])).fetchone()
        if clash:
            return err(f'Another item called "{new_name}" already exists - '
                       f"choose a different name.", 400)
        db.execute("UPDATE products SET name = ? WHERE barcode = ?",
                   (new_name, row["barcode"]))
        db.execute("UPDATE transactions SET item_name = ? WHERE id = ?",
                   (new_name, tid))
        changes.append(f"renamed \"{p['name']}\" → \"{new_name}\"")
    # --- sell price correction: updates the shelf price ---
    if 0 <= new_price and abs(new_price - (p["price"] or 0)) > 0.0001:
        db.execute("UPDATE products SET price = ? WHERE barcode = ?",
                   (new_price, row["barcode"]))
        changes.append(f"price R{(p['price'] or 0):.2f} → R{new_price:.2f}")
    if not changes:
        return err("Nothing changed - that is already what is receipted")
    # Audit trail: an EDIT journal row with the before/after in plain words.
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   unit_price, unit_cost, note)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (now_str(), row["barcode"], new_name or p["name"], "EDIT",
                delta, "STORE", "N/A",
                new_price if new_price >= 0 else p["price"], p["cost"],
                "stock-in edited: " + "; ".join(changes)))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "item": new_name or p["name"], "old": old_qty,
                    "new": new_qty, "changes": changes,
                    "on_hand_now": p["qty_on_hand"] + delta})


@app.route("/api/stock_in/create_product", methods=["POST"])
def api_stock_in_create_product():
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    name = str(data.get("name", "")).strip()
    qty = _i(data.get("qty"))
    cost, price = _f(data.get("cost")), _f(data.get("price"))
    reorder = _i(data.get("reorder_level"))
    if not barcode or not name:
        return err("barcode and name required")
    if qty < 0:
        return err("Qty cannot be negative")
    db = get_db()
    try:
        db.execute("""INSERT INTO products
                      (barcode, name, cost, price, qty_on_hand, reorder_level)
                      VALUES (?,?,?,?,?,?)""",
                   (barcode, name, cost, price, qty, reorder))
        if qty > 0:      # only log a stock-in if stock actually arrived
            db.execute("""INSERT INTO transactions
                          (datetime, barcode, item_name, type, qty, customer, payment)
                          VALUES (?,?,?,?,?,?,?)""",
                       (now_str(), barcode, name, "IN", qty, "SUPPLIER", "N/A"))
        bump_version(db)
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return err("Barcode exists")
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API: Customers
# --------------------------------------------------------------------------- #
@app.route("/api/customers/list")
def api_customers_list():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, phone, balance_owed, carried_over, carried_month "
        "FROM customers WHERE active = 1 ORDER BY lower(name)").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_reminded"], d["times_reminded"] = _reminder_stats(db, d["id"])
        out.append(d)
    return jsonify(out)


def _reminder_stats(db, cid):
    """(last reminder datetime, how many reminders ever) for one customer."""
    rows = db.execute(
        "SELECT datetime FROM reminders WHERE customer_id = ? ORDER BY id",
        (cid,)).fetchall()
    return (rows[-1]["datetime"] if rows else "", len(rows))


@app.route("/api/reminders/preview/<int:cid>")
def api_reminders_preview(cid):
    """Everything the popup needs BEFORE offering to send: the exact live
    balance, the exact message as it will go out, and the reminder history
    (so a second nudge is a conscious choice, never an accident)."""
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id = ? AND active = 1",
                   (cid,)).fetchone()
    if not c:
        return err("Customer not found", 404)
    bal = round(c["balance_owed"] or 0, 2)
    last, count = _reminder_stats(db, cid)
    wa = wa_number(c["phone"])
    return jsonify({"ok": True, "id": cid, "name": c["name"],
                    "balance": bal, "phone": c["phone"] or "",
                    "wa": wa, "phone_ok": bool(wa),
                    "message": reminder_message(c["name"], bal),
                    "last_reminded": last, "times_reminded": count})


@app.route("/api/reminders/send", methods=["POST"])
def api_reminders_send():
    """Fire a WhatsApp payment reminder. The app builds the wa.me link and
    opens it: on the main till in that laptop's own browser (Windows), on
    any other laptop the teller's browser opens it instead. Either way the
    message only goes out when a human presses SEND in WhatsApp - and the
    reminder is LOGGED either way, so month-end you always know who was
    reminded, when, and how many times."""
    data = request.get_json(silent=True) or {}
    cid = _i(data.get("id"))
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id = ? AND active = 1",
                   (cid,)).fetchone()
    if not c:
        return err("Customer not found", 404)
    bal = round(c["balance_owed"] or 0, 2)
    if bal <= 0:
        return err(f"{c['name']} owes nothing - nothing to chase.")
    wa = wa_number(c["phone"])
    if not wa:
        return err(f"No usable cellphone number for {c['name']} - " +
                   "fix it with the EDIT button on the Customers page first.")
    url = f"https://wa.me/{wa}?text={quote(reminder_message(c['name'], bal))}"
    opened = False
    if data.get("open_here") and os.name == "nt":
        try:
            os.startfile(url)      # main till: open this laptop's own browser
            opened = True
        except Exception:
            opened = False
    db.execute("INSERT INTO reminders (datetime, customer_id, customer, "
               "balance, channel) VALUES (?,?,?,?,?)",
               (now_str(), cid, c["name"], bal, "WHATSAPP"))
    bump_version(db)
    db.commit()
    last, count = _reminder_stats(db, cid)
    return jsonify({"ok": True, "url": url, "opened": opened,
                    "last_reminded": last, "times_reminded": count})


@app.route("/api/customers/add", methods=["POST"])
def api_customers_add():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    if not name:
        return err("Name required")
    db = get_db()
    existing = db.execute("SELECT * FROM customers WHERE name = ? COLLATE NOCASE",
                          (name,)).fetchone()
    if existing:
        if existing["active"]:
            return err("Customer already exists")
        # Came back after a month-end clear-out: wake the old record so the
        # history stays attached to the same person.
        db.execute("UPDATE customers SET active = 1, phone = ? WHERE id = ?",
                   (phone or existing["phone"], existing["id"]))
        bump_version(db)
        db.commit()
        return jsonify({"ok": True, "reactivated": True})
    db.execute("INSERT INTO customers (name, phone, balance_owed) VALUES (?,?,0)",
               (name, phone))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/customers/update", methods=["POST"])
def api_customers_update():
    data = request.get_json(silent=True) or {}
    cid = _i(data.get("id"))
    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    if cid <= 0 or not name:
        return err("Valid id and name required")
    db = get_db()
    old = db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if not old:
        return err("Customer not found", 404)
    dupe = db.execute("""SELECT 1 FROM customers
                         WHERE name = ? COLLATE NOCASE AND id != ?""",
                      (name, cid)).fetchone()
    if dupe:
        return err("Another customer already has that name")
    db.execute("UPDATE customers SET name = ?, phone = ? WHERE id = ?",
               (name, phone, cid))
    if old["name"] != name:
        # Keep history attached: rename everywhere the name was denormalised.
        db.execute("UPDATE transactions SET customer = ? WHERE customer = ?",
                   (name, old["name"]))
        db.execute("UPDATE accounts SET customer = ? WHERE customer = ?",
                   (name, old["name"]))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/customers/pay", methods=["POST"])
def api_customers_pay():
    """Record a payment against a customer's account balance."""
    data = request.get_json(silent=True) or {}
    cid = _i(data.get("id"))
    amount = round(_f(data.get("amount")), 2)
    method = str(data.get("payment", "CASH")).strip().upper() or "CASH"
    if method not in ("CASH", "CARD"):
        return err("Payment method must be CASH or CARD")
    if cid <= 0:
        return err("Valid customer id required")
    if amount <= 0:
        return err("Amount must be > 0")
    db = get_db()
    cust = db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if not cust:
        return err("Customer not found", 404)
    balance = round(cust["balance_owed"] or 0, 2)
    if balance <= 0:
        return err("Customer owes nothing")
    applied = min(amount, balance)          # overpay -> physical change (cash)
    # CARD: 2.5% markup on the amount being paid - ANY amount, even R1.
    # The fee is charged on top; only `applied` comes off the account.
    card_fee = card_fee_for(amount) if method == "CARD" else 0.0
    db.execute("UPDATE customers SET balance_owed = round(balance_owed - ?, 2) WHERE id = ?",
               (applied, cid))
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   subtotal, card_fee)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (now_str(), "", "ACCOUNT PAYMENT", "PAYMENT", 0,
                cust["name"], method, applied, card_fee))
    bump_version(db)
    db.commit()
    return jsonify({
        "ok": True,
        "applied": applied,                  # what came off the account
        "change": round(amount - applied, 2) if method == "CASH" else 0,
        "card_fee": card_fee,
        "card_total": round(amount + card_fee, 2),   # charged on the card machine
        "new_balance": round(balance - applied, 2),
    })


@app.route("/api/customers/add_debt", methods=["POST"])
def api_customers_add_debt():
    """OLD DEBT capture (MANAGEMENT ONLY): type in what a customer still
    owes from previous months / the old paper book. It goes straight onto
    their account balance (so the R2,500 BAN sees it immediately), it is
    marked as carried-over debt, and it lands on their statement as
    PREVIOUS BALANCE ADDED with any note the manager typed.

    It is NOT a sale and NO money moves: till figures, card-fee totals and
    the cash-up are untouched (the row type OLD_DEBT is ignored by every
    money calculation, which all filter on OUT / PAYMENT / RETURN). When
    the customer eventually pays it off, a normal RECEIVE PAYMENT captures
    that money then - exactly when it physically arrives."""
    data = request.get_json(silent=True) or {}
    cid = _i(data.get("id"))
    amount = round(_f(data.get("amount")), 2)
    note = str(data.get("note", "")).strip()[:120]
    if cid <= 0:
        return err("Valid customer id required")
    if amount <= 0:
        return err("Amount must be more than 0")
    if amount >= 1000000:
        return err("That amount looks like a typo - check it and try again")
    db = get_db()
    cust = db.execute("SELECT * FROM customers WHERE id = ?",
                      (cid,)).fetchone()
    if not cust:
        return err("Customer not found", 404)
    new_bal = round((cust["balance_owed"] or 0) + amount, 2)
    db.execute("""UPDATE customers SET balance_owed = ?,
                  carried_over = round(COALESCE(carried_over, 0) + ?, 2)
                  WHERE id = ?""", (new_bal, amount, cid))
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   subtotal, note)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (now_str(), "", "PREVIOUS BALANCE ADDED", "OLD_DEBT", 0,
                cust["name"], "ACCOUNT", amount,
                note or "typed in by the manager from the old records"))
    bump_version(db)
    db.commit()
    msg = (f"Added R{amount:.2f} old debt to {cust['name']} - their balance "
           f"is now R{new_bal:.2f}. It sits on their statement as PREVIOUS "
           f"BALANCE ADDED and counts towards the R2,500.00 limit.")
    banned = new_bal >= ACCOUNT_LIMIT - 0.005
    if banned:
        msg += (f" WARNING: {cust['name']} is now AT/OVER the R2,500.00 "
                f"limit - BANNED from more ON ACCOUNT sales until some of "
                f"it is paid off. CASH and CARD sales still work.")
    return jsonify({"ok": True, "added": amount, "new_balance": new_bal,
                    "banned": banned, "message": msg})


@app.route("/api/customers/swap", methods=["POST"])
def api_customers_swap():
    """'Changed my mind' EXCHANGE on an ON-ACCOUNT sale line (MANAGEMENT ONLY).
    The customer gives the original item back and takes a replacement: stock
    moves back onto / off the shelf, the charge row itself is updated to the
    replacement (so EVERY report stays perfectly consistent), the account
    balance moves by the price difference, and a SWAP audit row records the
    full story in plain words."""
    data = request.get_json(silent=True) or {}
    txn_id = _i(data.get("txn_id"))
    new_barcode = str(data.get("new_barcode", "")).strip()
    new_qty = _i(data.get("new_qty"))
    if txn_id <= 0:
        return err("Pick which line is being changed")
    if not new_barcode or new_qty <= 0:
        return err("Pick the replacement item and how many")
    db = get_db()
    row = db.execute("SELECT * FROM transactions WHERE id = ?",
                     (txn_id,)).fetchone()
    if not row or row["type"] != "OUT":
        return err("That line can't be swapped", 404)
    if (row["payment"] or "").upper() != "ACCOUNT":
        return err("Only ON ACCOUNT lines can be swapped here - cash/card "
                   "sales are exchanged at the till", 400)
    cust = db.execute("SELECT * FROM customers WHERE name = ?",
                      (row["customer"],)).fetchone()
    if not cust:
        return err("Customer not found", 404)
    newp = db.execute("SELECT * FROM products WHERE barcode = ?",
                      (new_barcode,)).fetchone()
    if not newp:
        return err("Replacement item not found", 404)
    if (newp["qty_on_hand"] or 0) < new_qty:
        return err(f"Not enough stock: only {newp['qty_on_hand']} × "
                   f"{newp['name']} on the shelf", 400)
    oldp = db.execute("SELECT 1 FROM products WHERE barcode = ?",
                      (row["barcode"],)).fetchone()
    if not oldp:
        return err("The original product no longer exists in stock - "
                   "it can't be put back, so the swap was refused", 400)
    orig_qty = row["qty"] or 0
    orig_sub = round(row["subtotal"] or 0, 2)
    new_sub = round((newp["price"] or 0) * new_qty, 2)
    delta = round(new_sub - orig_sub, 2)
    # Stock: original goes back on the shelf, replacement comes off.
    db.execute("UPDATE products SET qty_on_hand = qty_on_hand + ? "
               "WHERE barcode = ?", (orig_qty, row["barcode"]))
    db.execute("UPDATE products SET qty_on_hand = qty_on_hand - ? "
               "WHERE barcode = ?", (new_qty, new_barcode))
    # Account balance moves by exactly the price difference.
    db.execute("UPDATE customers SET balance_owed = round(balance_owed + ?, 2) "
               "WHERE id = ?", (delta, cust["id"]))
    # The charge row becomes the replacement; the note keeps the story.
    desc_old = f"{orig_qty}× {row['item_name']}"
    note = ((row["note"] or "") + " " + f"[SWAPPED from {desc_old}]").strip()
    db.execute("""UPDATE transactions
                  SET barcode = ?, item_name = ?, qty = ?, unit_price = ?,
                      unit_cost = ?, subtotal = ?, note = ?
                  WHERE id = ?""",
               (new_barcode, newp["name"], new_qty, newp["price"] or 0,
                newp["cost"] or 0, new_sub, note, txn_id))
    # Audit trail: a SWAP row so reports show the exchange in plain words.
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   unit_price, unit_cost, note)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (now_str(), new_barcode, newp["name"], "SWAP", 0,
                "STORE", "N/A", newp["price"], newp["cost"],
                f"{cust['name']} gave back {desc_old} (R{orig_sub:.2f}) and "
                f"took {new_qty}× {newp['name']} (R{new_sub:.2f}) - account "
                f"{'up' if delta >= 0 else 'down'} R{abs(delta):.2f}"))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "delta": delta,
                    "new_balance": round((cust["balance_owed"] or 0) + delta, 2),
                    "gave_back": desc_old,
                    "took": f"{new_qty}× {newp['name']}"})


@app.route("/api/customers/statement/<int:cid>")
def api_customer_statement(cid):
    db = get_db()
    cust = db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if not cust:
        return err("Customer not found", 404)
    tx = db.execute("""SELECT * FROM transactions
                       WHERE customer = ? ORDER BY id DESC""",
                    (cust["name"],)).fetchall()
    # Totals per payment channel so the statement can isolate account sales
    on_account = cash_sales = card_sales = payments = 0.0
    for t in tx:
        sub = t["subtotal"] or 0
        if t["type"] == "OUT":
            pay = (t["payment"] or "").upper()
            if pay == "ACCOUNT":
                on_account += sub
            elif pay == "CARD":
                card_sales += sub
            else:
                cash_sales += sub
        elif t["type"] == "PAYMENT":
            payments += sub
    return jsonify({
        "customer": dict(cust),
        "transactions": [dict(r) for r in tx],
        "summary": {
            "charged_on_account": round(on_account, 2),
            "paid_cash_sales": round(cash_sales, 2),
            "paid_card_sales": round(card_sales, 2),
            "payments_received": round(payments, 2),
        },
    })


# --------------------------------------------------------------------------- #
# API: POS / Sales
# --------------------------------------------------------------------------- #
@app.route("/api/pos/add_sale", methods=["POST"])
def api_pos_add_sale():
    data = request.get_json(silent=True) or {}
    cart = data.get("cart") or []
    if not isinstance(cart, list) or not cart:
        return err("Cart empty")

    payment = str(data.get("payment", "CASH")).strip().upper() or "CASH"
    if payment not in ("CASH", "CARD", "ACCOUNT"):
        return err("payment must be CASH, CARD or ACCOUNT")

    customer_name = str(data.get("customer", "")).strip()
    customer_id = _i(data.get("customer_id"))
    # Free-text sale note (optional): "bought on his behalf by ..." etc.
    # Stored on every line of the sale so the tray AND the statement can
    # show it. Never money, never required.
    sale_note = str(data.get("note", "")).strip()[:140]
    db = get_db()

    if payment == "ACCOUNT":
        nm = customer_name.strip().upper()
        if customer_id <= 0 and nm in ("", "CASH", "WALK-IN"):
            return err("Account sales need a NAMED customer - pick their "
                       "name first (WALK-IN pays cash or card)")
    else:
        customer_name = customer_name or "CASH"

    now = now_str()
    total = total_cogs = 0.0
    items_sold = 0
    lines = []
    receipt = []

    try:
        # Pass 1: validate the whole cart & freeze prices from the database.
        for item in cart:
            barcode = str((item or {}).get("barcode", "")).strip()
            qty = _i((item or {}).get("qty"), 1)
            if not barcode:
                raise ValueError("Cart item missing barcode")
            if qty <= 0:
                raise ValueError(f"Qty must be > 0 for {barcode}")

            p = db.execute("SELECT name, cost, price, qty_on_hand FROM products "
                           "WHERE barcode = ?", (barcode,)).fetchone()
            if not p:
                raise LookupError(f"Product {barcode} not found")

            # Price ALWAYS comes from the database - never from the client.
            unit_price = round(float(p["price"] or 0), 2)
            unit_cost = float(p["cost"] or 0)
            subtotal = round(unit_price * qty, 2)
            if p["qty_on_hand"] < qty:
                raise ValueError(f"Insufficient stock for {p['name']} "
                                 f"(have {p['qty_on_hand']}, need {qty})")
            lines.append({"barcode": barcode, "name": p["name"], "qty": qty,
                          "unit_price": unit_price, "unit_cost": unit_cost,
                          "subtotal": subtotal})

        total = round(sum(l["subtotal"] for l in lines), 2)

        # CARD payments: the customer pays a CARD_FEE_PERCENT markup on top.
        # The fee is stored PER ROW (split so the rows always add up to the
        # exact fee on the whole sale - the last row absorbs any 1c rounding).
        card_fee = card_fee_for(total) if payment == "CARD" else 0.0
        card_total = round(total + card_fee, 2)
        if payment == "CARD":
            running = 0.0
            for i, l in enumerate(lines):
                if i < len(lines) - 1:
                    l["card_fee"] = card_fee_for(l["subtotal"])
                    running += l["card_fee"]
                else:
                    l["card_fee"] = round(card_fee - running, 2)
        else:
            for l in lines:
                l["card_fee"] = 0.0

        # THE BAN (account limit): an account sale may never push what the
        # customer owes past ACCOUNT_LIMIT. Checked BEFORE a single unit of
        # stock moves - over the line, the whole sale stops right here and
        # they must pay some of the account down first (or pay cash/card).
        if payment == "ACCOUNT":
            bc_row = None
            if customer_id > 0:
                bc_row = db.execute("SELECT name, balance_owed FROM customers "
                                    "WHERE id = ?", (customer_id,)).fetchone()
            if not bc_row and customer_name:
                bc_row = db.execute("SELECT name, balance_owed FROM customers "
                                    "WHERE name = ?", (customer_name,)).fetchone()
            bal_now = round(float(bc_row["balance_owed"]), 2) if bc_row else 0.0
            if bal_now + total > ACCOUNT_LIMIT + 0.005:
                raise ValueError(
                    f"ACCOUNT BANNED: {customer_name} already owes "
                    f"R{bal_now:.2f}. This R{total:.2f} sale would take the "
                    f"account to R{bal_now + total:.2f} - the limit is "
                    f"R{ACCOUNT_LIMIT:,.2f}. They must pay some of their "
                    "account first, or pay CASH/CARD now.")

        # Pass 2: atomic stock deduction + write the transaction rows.
        for l in lines:
            # Atomic: the UPDATE only succeeds if enough stock exists.
            cur = db.execute("""UPDATE products SET qty_on_hand = qty_on_hand - ?
                                WHERE barcode = ? AND qty_on_hand >= ?""",
                             (l["qty"], l["barcode"], l["qty"]))
            if cur.rowcount != 1:
                raise ValueError(f"Insufficient stock for {l['name']} "
                                 f"(another sale may have just happened)")

            db.execute("""INSERT INTO transactions
                          (datetime, barcode, item_name, type, qty, customer,
                           payment, unit_price, unit_cost, subtotal, card_fee,
                           note)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (now, l["barcode"], l["name"], "OUT", l["qty"],
                        customer_name, payment, l["unit_price"], l["unit_cost"],
                        l["subtotal"], l["card_fee"], sale_note))
            total_cogs += l["unit_cost"] * l["qty"]
            items_sold += l["qty"]
            receipt.append({"barcode": l["barcode"], "name": l["name"],
                            "qty": l["qty"], "unit_price": l["unit_price"],
                            "subtotal": l["subtotal"]})

        if payment == "ACCOUNT":
            cust = None
            if customer_id > 0:
                cust = db.execute("SELECT * FROM customers WHERE id = ?",
                                  (customer_id,)).fetchone()
            if not cust and customer_name:
                cust = db.execute("SELECT * FROM customers WHERE name = ?",
                                  (customer_name,)).fetchone()
            if not cust:
                db.execute("INSERT INTO customers (name, phone, balance_owed) "
                           "VALUES (?,?,?)", (customer_name, "", total))
            else:
                customer_name = cust["name"]
                db.execute("UPDATE customers SET balance_owed = round(balance_owed + ?, 2) "
                           "WHERE id = ?", (total, cust["id"]))
            db.execute("INSERT INTO accounts (datetime, customer, total, payment) "
                       "VALUES (?,?,?,?)", (now, customer_name, total, "ACCOUNT"))

        bump_version(db)
        db.commit()
    except (ValueError, LookupError) as e:
        db.rollback()
        return err(str(e), 400)
    except sqlite3.Error as e:
        db.rollback()
        return err(f"Database error: {e}", 500)

    return jsonify({
        "ok": True,
        "total": total,                        # goods total (account balance uses this)
        "card_fee": card_fee,                  # 2.5% markup (CARD only, else 0)
        "card_total": card_total,              # what actually goes on the card machine
        "total_cogs": round(total_cogs, 2),
        "profit": round(total - total_cogs, 2),
        "items_sold": items_sold,
        "receipt": receipt,
    })


# --------------------------------------------------------------------------- #
# Shop & Losses - management page + APIs.
#   Three things the owner tracks here, all auto-totalled:
#     SHOP PURCHASES : which shop stock was bought from, and how much
#     DAMAGED STOCK  : items written off (stock drops automatically)
#     BAD DEBTS      : customer left the company without paying -> balance
#                      is forgiven AND recorded as a loss (never vanishes
#                      quietly - it sits on the reports)
# --------------------------------------------------------------------------- #
@app.route("/expenses")
def page_expenses():
    return render_template("expenses.html", title="ConvergEX POS - Shop & Losses")


@app.route("/api/expenses/overview")
def api_expenses_overview():
    db = get_db()
    today = today_str()
    month = date.today().strftime("%Y-%m")

    def q(sql, like):
        return db.execute(sql, (f"{like}%",)).fetchone()[0]

    purchases = db.execute(
        "SELECT * FROM purchases ORDER BY id DESC LIMIT 100").fetchall()
    damaged = db.execute(
        """SELECT * FROM transactions
           WHERE type = 'WRITE_OFF' AND note LIKE 'damaged%'
           ORDER BY id DESC LIMIT 100""").fetchall()
    bads = db.execute(
        """SELECT * FROM transactions WHERE type = 'BAD_DEBT'
           ORDER BY id DESC LIMIT 100""").fetchall()
    debtors = db.execute(
        """SELECT id, name, balance_owed FROM customers
           WHERE active = 1 AND balance_owed > 0 ORDER BY name""").fetchall()

    return jsonify({
        "purchases": [dict(p) for p in purchases],
        "purchases_today": q("SELECT round(COALESCE(SUM(amount),0),2) "
                             "FROM purchases WHERE datetime LIKE ?", today),
        "purchases_month": q("SELECT round(COALESCE(SUM(amount),0),2) "
                             "FROM purchases WHERE datetime LIKE ?", month),
        "damaged": [dict(d) for d in damaged],
        "damaged_today": q("SELECT round(COALESCE(SUM(subtotal),0),2) "
                           "FROM transactions WHERE type = 'WRITE_OFF' "
                           "AND note LIKE 'damaged%' AND datetime LIKE ?", today),
        "damaged_month": q("SELECT round(COALESCE(SUM(subtotal),0),2) "
                           "FROM transactions WHERE type = 'WRITE_OFF' "
                           "AND note LIKE 'damaged%' AND datetime LIKE ?", month),
        "bad_debts": [dict(b) for b in bads],
        "baddebt_today": q("SELECT round(COALESCE(SUM(subtotal),0),2) "
                           "FROM transactions WHERE type = 'BAD_DEBT' "
                           "AND datetime LIKE ?", today),
        "baddebt_month": q("SELECT round(COALESCE(SUM(subtotal),0),2) "
                           "FROM transactions WHERE type = 'BAD_DEBT' "
                           "AND datetime LIKE ?", month),
        "debtors": [dict(d) for d in debtors],
    })


@app.route("/api/expenses/purchase", methods=["POST"])
def api_expenses_purchase():
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop", "")).strip()
    amount = round(_f(data.get("amount")), 2)
    note = str(data.get("note", "")).strip()
    day = str(data.get("date", "")).strip() or today_str()
    if not shop:
        return err("Which shop did you buy from? Type the shop name")
    if amount <= 0:
        return err("Amount must be more than R0")
    # Optional "Date Bought": old slips get entered weeks later but must
    # land on the day the money actually went out, so the daily/monthly
    # reports for THAT time carry it (never today's till numbers).
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return err("The date must look like 2026-06-14")
    try:
        parsed = datetime.strptime(day, DATE_FORMAT)
    except ValueError:
        return err(f"{day} is not a real date - check the day and month")
    if day > today_str():
        return err("A purchase cannot be dated in the future")
    frozen = os.path.join(ARCHIVE_DIR, f"archive_{day[:7]}.xlsx")
    if os.path.exists(frozen):
        month_name = parsed.strftime("%B %Y")
        return err(
            f"{month_name} is already archived (locked for good), so a "
            f"receipt cannot be slipped into it anymore. Date it in the "
            f"open month instead, and put the real date in "
            f"'What was bought'.")
    when = f"{day} {datetime.now().strftime('%H:%M:%S')}"
    db = get_db()
    db.execute("INSERT INTO purchases (datetime, shop, amount, note) "
               "VALUES (?,?,?,?)", (when, shop, amount, note))
    bump_version(db)
    db.commit()
    month_total = db.execute(
        "SELECT round(COALESCE(SUM(amount),0),2) s FROM purchases "
        "WHERE datetime LIKE ?",
        (today_str()[:7] + "%",)).fetchone()["s"]
    return jsonify({"ok": True, "shop": shop, "amount": amount,
                    "month_total": month_total, "date": day,
                    "dated_display": parsed.strftime(DISPLAY_DATE_FORMAT),
                    "backdated": day != today_str()})


@app.route("/api/expenses/damaged", methods=["POST"])
def api_expenses_damaged():
    data = request.get_json(silent=True) or {}
    barcode = str(data.get("barcode", "")).strip()
    qty = _i(data.get("qty"))
    reason = str(data.get("reason", "")).strip()
    if qty <= 0:
        return err("Qty must be more than 0")
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE barcode = ?",
                   (barcode,)).fetchone()
    if not p:
        return err("Item not found", 404)
    if (p["qty_on_hand"] or 0) < qty:
        return err(f"Only {p['qty_on_hand']} of {p['name']} on hand")
    value = round((p["price"] or 0) * qty, 2)
    cur = db.execute("UPDATE products SET qty_on_hand = qty_on_hand - ? "
                     "WHERE barcode = ? AND qty_on_hand >= ?",
                     (qty, barcode, qty))
    if cur.rowcount != 1:
        db.rollback()
        return err("Stock changed at the same moment - try again")
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   unit_price, unit_cost, subtotal, note)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (now_str(), barcode, p["name"], "WRITE_OFF", qty, "SHOP", "N/A",
                p["price"] or 0, p["cost"] or 0, value,
                "damaged" + (f" - {reason}" if reason else "")))
    bump_version(db)
    db.commit()
    left = db.execute("SELECT qty_on_hand FROM products WHERE barcode = ?",
                      (barcode,)).fetchone()["qty_on_hand"]
    return jsonify({"ok": True, "item": p["name"], "qty": qty, "value": value,
                    "on_hand_now": left})


@app.route("/api/expenses/bad_debt", methods=["POST"])
def api_expenses_bad_debt():
    data = request.get_json(silent=True) or {}
    cid = _i(data.get("customer_id"))
    db = get_db()
    cust = db.execute("SELECT * FROM customers WHERE id = ? AND active = 1",
                      (cid,)).fetchone()
    if not cust:
        return err("Customer not found", 404)
    bal = round(cust["balance_owed"] or 0, 2)
    if bal <= 0:
        return err(f"{cust['name']} owes nothing - nothing to write off")
    db.execute("""INSERT INTO transactions
                  (datetime, barcode, item_name, type, qty, customer, payment,
                   subtotal, note)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (now_str(), "", "BALANCE WRITTEN OFF (left without paying)",
                "BAD_DEBT", 0, cust["name"], "N/A", bal,
                "left the company without paying"))
    db.execute("UPDATE customers SET balance_owed = 0 WHERE id = ?", (cid,))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "name": cust["name"], "amount": bal})


# --------------------------------------------------------------------------- #
# Today's Sales + RETURNS / EXCHANGES.
#   Every sale lands here grouped (time + customer + method). Any line can be
#   returned - CASH refunds speak cash, CARD refunds include the fee share,
#   ACCOUNT returns come straight off the customer's balance. Stock ALWAYS
#   goes back on the shelf, and the whole thing is journaled on the reports.
# --------------------------------------------------------------------------- #
def _sales_groups_for(day):
    """All OUT sales of one calendar day (YYYY-MM-DD), grouped per sale
    (same moment + customer + payment = one sale, even with many lines)."""
    rows = get_db().execute(
        """SELECT * FROM transactions
           WHERE type = 'OUT' AND datetime LIKE ? ORDER BY id""",
        (f"{day}%",)).fetchall()
    groups, order = {}, []
    for r in rows:
        pay = (r["payment"] or "CASH").upper()
        key = (r["datetime"], r["customer"], pay)
        g = groups.get(key)
        if not g:
            try:
                clock = datetime.strptime(r["datetime"], DATETIME_FORMAT) \
                    .strftime("%H:%M")
            except (TypeError, ValueError):
                clock = r["datetime"] or ""
            g = {"datetime": r["datetime"], "time": clock,
                 "customer": r["customer"], "payment": pay,
                 "lines": [], "total": 0.0, "fee": 0.0, "grand": 0.0,
                 "exchange_born": False, "note": ""}
            groups[key] = g
            order.append(key)
        if "exchange for sale" in (r["note"] or ""):
            g["exchange_born"] = True          # created BY an exchange: no VOID
        elif (r["note"] or "").strip() and not g["note"]:
            g["note"] = r["note"].strip()      # the typed sale note (if any)
        qty = r["qty"] or 0
        ret = (r["returned_qty"] or 0) if "returned_qty" in r.keys() else 0
        sub = r["subtotal"] if r["subtotal"] is not None \
            else (r["unit_price"] or 0) * qty
        g["lines"].append({
            "id": r["id"], "item": r["item_name"], "qty": qty,
            "returned": ret, "remaining": qty - ret,
            "unit_price": float(r["unit_price"] or 0),
            "subtotal": float(sub or 0),
            "fee": float(r["card_fee"] or 0)})
        g["total"] = round(g["total"] + (sub or 0), 2)
        g["fee"] = round(g["fee"] + (r["card_fee"] or 0), 2)
        g["grand"] = round(g["total"] + g["fee"], 2)
    return [groups[k] for k in reversed(order)]


@app.route("/api/sales/today")
def api_sales_today():
    return jsonify(_sales_groups_for(today_str()))


@app.route("/api/sales/day/<day>")
def api_sales_day(day):
    """The same tray, but for any PAST day (the calendar picker). Viewing
    only - returns/voids always happen on the day itself."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return err("Day must look like 2026-08-26")
    if day > today_str():
        return err("That day is still in the future")
    return jsonify(_sales_groups_for(day))


@app.route("/api/sales/return", methods=["POST"])
def api_sales_return():
    """Return OR exchange items from a sale, with exact money routing.
    Payload: {lines: [{id, qty}]  (what comes back),
              exchange: [{barcode, qty}]  (what they take instead)}
    RETURN: the money reverses by payment method. EXCHANGE: old items come
    back, new items go out, and the DIFFERENCE is calculated to the cent -
    cash: collect or hand back; card: charge or refund incl. card-fee shares
    BOTH ways; account: nets onto the customer's balance (never below 0)."""
    data = request.get_json(silent=True) or {}
    lines = data.get("lines") or []
    exchange = data.get("exchange") or []
    if not isinstance(lines, list) or not lines:
        return err("Nothing to return")
    if not isinstance(exchange, list):
        return err("Bad exchange list")

    db = get_db()
    done, names, take_names = [], [], []
    orig_dt = pay = cust = None
    return_value = 0.0     # goods value coming back (excl. card fee)
    fee_back = 0.0         # card fee being given back (CARD only)
    new_total = 0.0        # goods value going out (excl. card fee)
    new_fee = 0.0          # card fee on the new goods (CARD only)
    now = now_str()
    try:
        # ---- pass 1: what comes back (stock in, sale lines marked) ----
        for ln in lines:
            tid = _i((ln or {}).get("id"))
            q = _i((ln or {}).get("qty"))
            if q <= 0:
                continue
            r = db.execute("SELECT * FROM transactions WHERE id = ?",
                           (tid,)).fetchone()
            if not r or r["type"] != "OUT":
                raise ValueError("Sale line not found - refresh and try again")
            this_pay = (r["payment"] or "CASH").upper()
            this_cust = r["customer"] or "CASH"
            if pay is None:
                pay, cust, orig_dt = this_pay, this_cust, r["datetime"]
            elif (this_pay, this_cust) != (pay, cust):
                raise ValueError("Handle one sale at a time - pick lines "
                                 "from a single sale")
            qty = r["qty"] or 0
            left = qty - (r["returned_qty"] or 0)
            if q > left:
                raise ValueError(f"{r['item_name']}: only {left} left to return")
            cur = db.execute(
                """UPDATE transactions SET returned_qty = returned_qty + ?
                   WHERE id = ? AND returned_qty + ? <= qty""", (q, tid, q))
            if cur.rowcount != 1:
                raise ValueError(f"{r['item_name']}: changed at the same "
                                 f"moment - refresh and try again")
            # Stock goes straight back on the shelf (and wakes the item if
            # a month-end archive had cleared it away).
            db.execute("UPDATE products SET qty_on_hand = qty_on_hand + ?, "
                       "active = 1 WHERE barcode = ?", (q, r["barcode"]))
            value = round((r["unit_price"] or 0) * q, 2)
            # Card: fee comes back too, pro-rata, integer cents half-up.
            fee_cents = int(round((r["card_fee"] or 0) * 100))
            fee = ((fee_cents * q + qty // 2) // qty) / 100.0 if qty > 0 else 0.0
            db.execute("""INSERT INTO transactions
                          (datetime, barcode, item_name, type, qty, customer,
                           payment, unit_price, unit_cost, subtotal, card_fee,
                           note)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (now, r["barcode"], r["item_name"], "RETURN", q, cust,
                        pay, r["unit_price"] or 0, r["unit_cost"] or 0,
                        -value, -fee,
                        f"return/exchange from sale at {r['datetime']}"))
            return_value = round(return_value + value, 2)
            fee_back = round(fee_back + fee, 2)
            done.append({"id": tid, "qty": q})
            names.append(f"{q}\u00d7 {r['item_name']}")
        if not done:
            return err("Nothing was picked to return")

        # ---- pass 2: what they take instead (stock out, live prices) ----
        new_lines = []
        for ex in exchange:
            bc = str((ex or {}).get("barcode", "")).strip()
            q = _i((ex or {}).get("qty"))
            if q <= 0:
                continue
            p = db.execute("SELECT name, cost, price, qty_on_hand "
                           "FROM products WHERE barcode = ?", (bc,)).fetchone()
            if not p:
                raise LookupError("Product not found - refresh and try again")
            if (p["qty_on_hand"] or 0) < q:
                raise ValueError(f"Not enough {p['name']} for the exchange "
                                 f"(have {p['qty_on_hand']}, need {q})")
            unit_price = round(float(p["price"] or 0), 2)
            new_lines.append({"barcode": bc, "name": p["name"], "qty": q,
                              "unit_price": unit_price,
                              "unit_cost": float(p["cost"] or 0),
                              "subtotal": round(unit_price * q, 2)})
        if new_lines:
            new_total = round(sum(l["subtotal"] for l in new_lines), 2)
            if pay == "CARD":
                # Fee applies to the new goods, split per row so rows always
                # add up exactly (last row absorbs any 1c).
                new_fee = card_fee_for(new_total)
                running = 0.0
                for i, l in enumerate(new_lines):
                    if i < len(new_lines) - 1:
                        l["card_fee"] = card_fee_for(l["subtotal"])
                        running += l["card_fee"]
                    else:
                        l["card_fee"] = round(new_fee - running, 2)
            for l in new_lines:
                cur = db.execute(
                    """UPDATE products SET qty_on_hand = qty_on_hand - ?
                       WHERE barcode = ? AND qty_on_hand >= ?""",
                    (l["qty"], l["barcode"], l["qty"]))
                if cur.rowcount != 1:
                    raise ValueError(f"Not enough {l['name']} - another sale "
                                     f"may have just happened")
                db.execute("""INSERT INTO transactions
                              (datetime, barcode, item_name, type, qty,
                               customer, payment, unit_price, unit_cost,
                               subtotal, card_fee, note)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (now, l["barcode"], l["name"], "OUT", l["qty"],
                            cust, pay, l["unit_price"], l["unit_cost"],
                            l["subtotal"], l.get("card_fee", 0.0),
                            f"exchange for sale at {orig_dt}"))
                take_names.append(f"{l['qty']}\u00d7 {l['name']}")
            if pay == "ACCOUNT":
                db.execute("INSERT INTO accounts "
                           "(datetime, customer, total, payment) "
                           "VALUES (?,?,?,?)",
                           (now, cust, new_total, "ACCOUNT"))

        # ---- pass 3: account balance nets ONCE (never below 0) ----
        if pay == "ACCOUNT":
            delta = round(new_total - return_value, 2)
            db.execute("""UPDATE customers SET balance_owed =
                          round(max(balance_owed + ?, 0), 2)
                          WHERE name = ?""", (delta, cust))

        bump_version(db)
        db.commit()
    except (ValueError, LookupError) as e:
        db.rollback()
        return err(str(e), 400)
    except sqlite3.Error as e:
        db.rollback()
        return err(f"Database error: {e}", 500)

    # ---- the money sentence: who pays / gets what, to the cent ----
    back_total = round(return_value + (fee_back if pay == "CARD" else 0), 2)
    take_total = round(new_total + (new_fee if pay == "CARD" else 0), 2)
    net = round(take_total - back_total, 2)   # + customer owes, - we owe
    is_exchange = bool(take_names)
    if abs(net) < 0.005:
        direction, diff = "EVEN", 0.0
    elif net > 0:
        direction, diff = "COLLECT", net
    else:
        direction, diff = "GIVE_BACK", round(-net, 2)

    if is_exchange:
        head = ("EXCHANGE RECORDED: took back " + ", ".join(names) +
                " | gave out " + ", ".join(take_names) + " - stock updated.")
    else:
        head = "RETURNED: " + ", ".join(names) + " - stock is back on the shelf."

    if pay == "CASH":
        body = ("Even exchange - no money changes hands."
                if direction == "EVEN" else
                f"Give back R{diff:.2f} CASH from the till."
                if direction == "GIVE_BACK" else
                f"Customer pays R{diff:.2f} extra CASH.")
    elif pay == "CARD":
        body = ("Even exchange - no money changes hands."
                if direction == "EVEN" else
                f"Refund R{diff:.2f} to their CARD (fees included)."
                if direction == "GIVE_BACK" else
                f"Charge their CARD R{diff:.2f} extra (fees included).")
    else:
        bal_row = get_db().execute(
            "SELECT balance_owed FROM customers WHERE name = ?",
            (cust,)).fetchone()
        bal_now = round(bal_row["balance_owed"], 2) if bal_row else 0.0
        body = (f"{cust}'s account stays at R{bal_now:.2f} (even exchange)."
                if direction == "EVEN" else
                f"R{diff:.2f} came OFF {cust}'s account "
                f"- balance now R{bal_now:.2f}."
                if direction == "GIVE_BACK" else
                f"R{diff:.2f} went ON {cust}'s account "
                f"- balance now R{bal_now:.2f}.")

    cash_back = card_refund = account_credit = 0.0
    if direction == "GIVE_BACK":
        if pay == "CASH":
            cash_back = diff
        elif pay == "CARD":
            card_refund = diff
        else:
            account_credit = diff

    return jsonify({
        "ok": True, "exchange": is_exchange, "returned": done,
        "back": back_total, "taking": take_total,
        "direction": direction, "diff": diff, "method": pay,
        "cash_back": cash_back, "card_refund": card_refund,
        "account_credit": account_credit,
        "message": head + "\n" + body,
    })


# --------------------------------------------------------------------------- #
# VOID a whole sale (management only): TEST sales and till mistakes wiped
# completely - never used for a real customer refund (that is what
# RETURN / EXCHANGE is for).
# --------------------------------------------------------------------------- #
@app.route("/api/sales/void", methods=["POST"])
def api_sales_void():
    """VOID an entire sale from TODAY: the stock goes back on the shelf, card
    fees drop off, an account balance unwinds, and the sale rows are deleted.
    A safety database backup is ALWAYS saved first (Reports page can restore
    it). Payload: {id: <any line id of the sale>} - the WHOLE sale goes.

    Hard rules, all checked before anything moves:
      today only      - older days are already inside counted cash-ups
      day still open  - after cash-up the day's figures are final
      no returns yet  - those belong to RETURN / EXCHANGE
      not exchange-born - that sale's money is tied to its return rows
      account sale    - refused if the customer already paid some back"""
    data = request.get_json(silent=True) or {}
    tid = _i(data.get("id"))
    db = get_db()
    r0 = db.execute("SELECT * FROM transactions WHERE id = ?",
                    (tid,)).fetchone()
    if not r0 or r0["type"] != "OUT":
        return err("Sale line not found - refresh and try again", 404)
    if not (r0["datetime"] or "").startswith(today_str()):
        return err("Only today's sales can be voided - older days are "
                   "already inside counted cash-ups")
    if db.execute("SELECT 1 FROM budget WHERE date = ?",
                  (today_str(),)).fetchone():
        return err("Today is already cashed up - voiding now would break "
                   "the closed figures. It can only be done before the "
                   "day is closed")
    pay = (r0["payment"] or "CASH").upper()
    cust = r0["customer"] or "CASH"
    dt = r0["datetime"]
    rows = db.execute(
        """SELECT * FROM transactions
           WHERE type = 'OUT' AND datetime = ? AND customer = ? AND payment = ?
           ORDER BY id""", (dt, cust, pay)).fetchall()
    if any((r["returned_qty"] or 0) > 0 for r in rows):
        return err("This sale already has returns/exchanges against it - "
                   "finish it with RETURN / EXCHANGE instead")
    if any("exchange for sale" in (r["note"] or "") for r in rows):
        return err("This sale is the exchange half of a swap - its money is "
                   "tied to the return that created it. Correct it with "
                   "RETURN / EXCHANGE instead")
    total = round(sum(
        (r["subtotal"] if r["subtotal"] is not None
         else (r["unit_price"] or 0) * (r["qty"] or 0)) for r in rows), 2)
    fee = round(sum((r["card_fee"] or 0) for r in rows), 2)
    if pay == "ACCOUNT":
        cr = db.execute("SELECT balance_owed FROM customers WHERE name = ?",
                        (cust,)).fetchone()
        bal = round(cr["balance_owed"], 2) if cr else 0.0
        if bal < total - 0.005:
            return err(f"{cust} has already paid some of this back - voiding "
                       "would scramble their statement. Use RETURN / EXCHANGE "
                       "(or bad debt on Shop & Losses) instead")

    # Safety net FIRST: a full database backup lands in backups\ before a
    # single row is touched. If the backup itself fails, nothing is voided.
    fname = ("tuckshop_before_void_" +
             datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".db")
    try:
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(os.path.join(BACKUP_DIR, fname))
        src.backup(dst)                      # sqlite's own safe live backup
        dst.close()
        src.close()
    except sqlite3.Error as e:
        return err(f"Safety backup failed: {e} - nothing was voided", 500)

    try:
        for r in rows:
            db.execute("UPDATE products SET qty_on_hand = qty_on_hand + ?, "
                       "active = 1 WHERE barcode = ?",
                       (r["qty"] or 0, r["barcode"]))
        if pay == "ACCOUNT":
            db.execute("UPDATE customers SET balance_owed = "
                       "round(max(balance_owed - ?, 0), 2) WHERE name = ?",
                       (total, cust))
            db.execute("""DELETE FROM accounts WHERE id =
                          (SELECT id FROM accounts
                           WHERE datetime = ? AND customer = ? AND total = ?
                           AND payment = 'ACCOUNT' LIMIT 1)""",
                       (dt, cust, total))
        ids = [r["id"] for r in rows]
        db.execute("DELETE FROM transactions WHERE id IN (" +
                   ",".join("?" * len(ids)) + ")", ids)
        bump_version(db)
        db.commit()
    except sqlite3.Error as e:
        db.rollback()
        return err(f"Database error: {e}", 500)

    items = ", ".join(f"{r['qty']}\u00d7 {r['item_name']}" for r in rows)
    amount = round(total + fee, 2) if pay == "CARD" else total
    head = f"VOIDED: {items} - R{amount:.2f} {pay} sale for {cust}."
    bits = ["stock is back on the shelf"]
    if pay == "CARD" and fee > 0:
        bits.append(f"the R{fee:.2f} card fee dropped off")
    if pay == "ACCOUNT":
        bits.append(f"R{total:.2f} came off {cust}'s account")
    bits.append("the sale is gone from today's figures")
    tail = (f"A safety backup was saved first ({fname}) - restore it from "
            "the Reports page if this was a mistake.")
    return jsonify({
        "ok": True, "total": total, "fee": fee, "amount": amount,
        "lines": len(rows), "backup": fname,
        "message": head + " " + ", ".join(bits) + ". " + tail,
    })


# --------------------------------------------------------------------------- #
# Backup & Restore (management). The app ALSO self-backs-up into backups\
# every time it closes; this is the on-demand, human-driven version.
# --------------------------------------------------------------------------- #
RESTORE_LOCK = threading.Lock()


@app.route("/api/backup/now", methods=["POST"])
def api_backup_now():
    fname = f"tuckshop_backup_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.db"
    dest = os.path.join(BACKUP_DIR, fname)
    try:
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(dest)
        src.backup(dst)                      # sqlite's own safe live backup
        dst.close()
        src.close()
    except sqlite3.Error as e:
        return err(f"Backup failed: {e}", 500)
    _open_folder(BACKUP_DIR)
    return jsonify({"ok": True, "path": dest, "file": fname})


@app.route("/api/backup/list")
def api_backup_list():
    out = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not (f.startswith("tuckshop") and f.endswith(".db")):
            continue
        p = os.path.join(BACKUP_DIR, f)
        out.append({"file": f,
                    "kb": round(os.path.getsize(p) / 1024, 1),
                    "modified": datetime.fromtimestamp(os.path.getmtime(p))
                                .strftime("%d/%m/%Y %H:%M")})
    return jsonify(out)


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """Replace the live database with a chosen backup, safely. Uses sqlite's
    own backup API so a half-copied file can never corrupt the till."""
    data = request.get_json(silent=True) or {}
    fname = os.path.basename(str(data.get("file", "")))
    path = os.path.join(BACKUP_DIR, fname)
    if not (fname.startswith("tuckshop") and fname.endswith(".db")) \
            or not os.path.exists(path):
        return err("Backup file not found", 404)
    with RESTORE_LOCK:
        try:
            src = sqlite3.connect(path, timeout=10)
            dst = sqlite3.connect(DB_PATH, timeout=10)
            src.backup(dst)
            dst.close()
            src.close()
        except sqlite3.Error as e:
            return err(f"Restore failed: {e}", 500)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute("INSERT OR IGNORE INTO meta (key, value) "
                         "VALUES ('data_version', 0)")
            bump_version(conn)
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True, "file": fname})


# --------------------------------------------------------------------------- #
# API: Cash-up / Budget
# --------------------------------------------------------------------------- #
@app.route("/api/cashup/summary")
def api_cashup_summary():
    db = get_db()
    s = compute_summary(db, today_str())
    # If today was already closed, include the close figures so the page
    # can show a "DAY CLOSED" banner instead of looking unfinished.
    row = db.execute("SELECT * FROM budget WHERE date = ?",
                     (today_str(),)).fetchone()
    s["closed"] = dict(row) if row else None
    return jsonify(s)


@app.route("/api/cashup/float", methods=["POST"])
def api_cashup_float():
    """Set this morning's float (start-of-day). Upserts today's row."""
    data = request.get_json(silent=True) or {}
    amount = round(_f(data.get("amount")), 2)
    if amount < 0:
        return err("Float cannot be negative")
    db = get_db()
    db.execute("""INSERT INTO day_float (date, amount) VALUES (?, ?)
                  ON CONFLICT(date) DO UPDATE SET amount = excluded.amount""",
               (today_str(), amount))
    bump_version(db)
    db.commit()
    return jsonify({"ok": True, "float": amount})


@app.route("/api/cashup/save", methods=["POST"])
def api_cashup_save():
    """Close the day. Expected in till is ALWAYS computed server-side as
    (this morning's float + today's cash takings), so counting the whole
    drawer - float included - still reads BALANCED."""
    data = request.get_json(silent=True) or {}
    actual_cash = round(_f(data.get("actual_cash")), 2)
    float_for_tomorrow = round(_f(data.get("float")), 2)
    today = today_str()

    db = get_db()
    summary = compute_summary(db, today)
    expected_till = summary["expected_till"]          # float + cash takings
    diff = round(actual_cash - expected_till, 2)

    db.execute("DELETE FROM budget WHERE date = ?", (today,))
    db.execute("""INSERT INTO budget (date, expected_cash, actual_cash, difference, "float")
                  VALUES (?,?,?,?,?)""",
               (today, expected_till, actual_cash, diff, float_for_tomorrow))
    # The float left in the drawer tonight becomes TOMORROW MORNING's float
    # automatically - the same R100 simply stays in the till, and tomorrow's
    # "expected in till" starts from it again. It is never counted as sales.
    if float_for_tomorrow > 0:
        tomorrow = (date.today() + timedelta(days=1)).strftime(DATE_FORMAT)
        db.execute("""INSERT INTO day_float (date, amount) VALUES (?, ?)
                      ON CONFLICT(date) DO UPDATE SET amount = excluded.amount""",
                   (tomorrow, float_for_tomorrow))
    db.execute("DELETE FROM daily_summary WHERE date = ?", (today,))
    db.execute("""INSERT INTO daily_summary (date, total_sales, total_cogs, profit, items_sold)
                  VALUES (?,?,?,?,?)""",
               (today, summary["total_sales"], summary["total_cogs"],
                summary["profit"], summary["items_sold"]))
    bump_version(db)
    db.commit()
    # What you physically take OUT of the drawer tonight (your banking):
    # the rest stays as tomorrow's float.
    banked = round(actual_cash - float_for_tomorrow, 2)
    return jsonify({"ok": True, "difference": diff, "banked": banked,
                    "expected_till": expected_till, "summary": summary})


# --------------------------------------------------------------------------- #
# API: Reports
# --------------------------------------------------------------------------- #
@app.route("/api/reports/today")
def api_reports_today():
    db = get_db()
    today = today_str()
    tx = db.execute("""SELECT * FROM transactions
                       WHERE datetime LIKE ? ORDER BY id DESC""",
                    (f"{today}%",)).fetchall()
    low = db.execute("""SELECT * FROM products
                        WHERE qty_on_hand <= reorder_level
                        ORDER BY qty_on_hand ASC""").fetchall()
    thirty = (date.today() - timedelta(days=30)).strftime(DATE_FORMAT)
    top = db.execute("""SELECT barcode, item_name,
                               SUM(qty) AS qty_sold,
                               round(SUM(subtotal), 2) AS revenue
                        FROM transactions
                        WHERE type = 'OUT' AND datetime >= ?
                        GROUP BY barcode
                        ORDER BY qty_sold DESC LIMIT 10""",
                     (thirty,)).fetchall()
    owed = db.execute("""SELECT name, phone, balance_owed FROM customers
                         WHERE balance_owed > 0
                         ORDER BY balance_owed DESC""").fetchall()
    return jsonify({
        "transactions": [dict(r) for r in tx],
        "low_stock": [dict(r) for r in low],
        "top_selling": [dict(r) for r in top],
        "accounts_owed": [dict(r) for r in owed],
        "summary": compute_summary(db, today),
    })


# --------------------------------------------------------------------------- #
# API: Archive - month-end cleaner.
#
# Running the archive moves EVERYTHING from completed months (anything before
# the 1st of the current month) out of the live database and into a styled
# Excel file per month inside  archives\   e.g.  archive_2026-06.xlsx
# Each workbook has one sheet per table (transactions, accounts, budget,
# daily_summary). The current month ALWAYS stays live. Rows are only deleted
# after their Excel file has been written successfully.
# --------------------------------------------------------------------------- #
def _build_archive_xlsx(path, month, tables, conn):
    """Write one month's rows to a neat Excel workbook (one sheet per table)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    for tbl in ARCHIVE_TABLES:                      # stable sheet order
        rows = tables.get(tbl)
        if not rows:
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
        ws = wb.create_sheet(tbl[:31])
        ws.append([f"Archived {tbl} - {month}"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append(cols)
        for c in ws[2]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9E2F3")
        for row in rows:
            ws.append(list(row))
        _autofit(ws)
    wb.save(path)


@app.route("/api/archive/run", methods=["POST"])
def api_archive_run():
    current_month = date.today().strftime("%Y-%m")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        # group everything from completed months, per month per table
        archive = {}                                 # "2026-06" -> {table: rows}
        for tbl, col in ARCHIVE_TABLES.items():
            months = conn.execute(
                f"SELECT DISTINCT substr({col}, 1, 7) FROM {tbl} "
                f"WHERE substr({col}, 1, 7) < ?", (current_month,)).fetchall()
            for (m,) in months:
                if not m:
                    continue
                rows = conn.execute(
                    f"SELECT * FROM {tbl} WHERE substr({col}, 1, 7) = ?",
                    (m,)).fetchall()
                if rows:
                    archive.setdefault(m, {})[tbl] = rows

        files, moved = [], {}
        for month, tables in sorted(archive.items()):
            fname = f"archive_{month}.xlsx"
            _build_archive_xlsx(os.path.join(ARCHIVE_DIR, fname),
                                month, tables, conn)
            # file written OK -> now it is safe to remove those rows
            for tbl, col in ARCHIVE_TABLES.items():
                if tbl in tables:
                    cur = conn.execute(
                        f"DELETE FROM {tbl} WHERE substr({col}, 1, 7) = ?",
                        (month,))
                    moved[tbl] = moved.get(tbl, 0) + cur.rowcount
            files.append(fname)

        # CARRY-OVER RULE (the owner's rule): after an archive, anything at
        # ZERO disappears from the live lists - customers who owe nothing and
        # items with no stock left vanish. Anyone still OWING money and every
        # item WITH stock carries over into the new month automatically.
        # (Nothing is deleted - "disappeared" rows wake up again the moment
        #  the person buys or the item is restocked.)
        # FIRST remember, per debtor, how much of the balance came from the
        # months just archived - the Customers page shows it as
        # "incl. R X carried over".
        conn.execute("UPDATE customers SET carried_over = round(balance_owed, 2), "
                     "carried_month = ? WHERE round(balance_owed, 2) > 0",
                     (current_month,))
        conn.execute("UPDATE customers SET carried_over = 0, carried_month = '' "
                     "WHERE round(balance_owed, 2) <= 0")
        retired_customers = conn.execute(
            "UPDATE customers SET active = 0 WHERE active = 1 "
            "AND round(balance_owed, 2) <= 0").rowcount
        kept_customers = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE active = 1").fetchone()[0]
        retired_products = conn.execute(
            "UPDATE products SET active = 0 WHERE active = 1 "
            "AND qty_on_hand <= 0").rowcount
        kept_products = conn.execute(
            "SELECT COUNT(*) FROM products WHERE active = 1").fetchone()[0]
        bump_version(conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        return err(f"Archive failed: {e}", 500)
    finally:
        conn.close()

    _open_folder(ARCHIVE_DIR)
    return jsonify({"ok": True, "files": files, "moved": moved,
                    "message": None if files else
                               "Nothing from previous months to archive",
                    "retired_customers": retired_customers,
                    "kept_customers": kept_customers,
                    "retired_products": retired_products,
                    "kept_products": kept_products})


@app.route("/api/archive/list")
def api_archive_list():
    files = sorted((f for f in os.listdir(ARCHIVE_DIR)
                    if f.endswith((".xlsx", ".db"))), reverse=True)
    return jsonify(files)


@app.route("/api/archive/open/<path:filename>")
def api_archive_open(filename):
    """Desktop app: open the archive in Excel.
       Browser fallback: send the file as a download."""
    if os.path.basename(filename) != filename or ".." in filename:
        return err("Invalid filename", 400)
    path = os.path.join(ARCHIVE_DIR, filename)
    if not os.path.exists(path):
        return err("Not found", 404)
    if os.name == "nt":
        try:
            os.startfile(path)                       # opens in Excel on Windows
            return jsonify({"ok": True, "opened": filename, "path": path})
        except Exception:
            pass
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/archive/view/<path:filename>")
def api_archive_view(filename):
    """Row counts for a legacy .db archive (kept for old archives)."""
    if os.path.basename(filename) != filename or ".." in filename:
        return err("Invalid filename", 400)
    path = os.path.join(ARCHIVE_DIR, filename)
    if not os.path.exists(path):
        return err("Not found", 404)
    if filename.endswith(".xlsx"):
        return api_archive_open(filename)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    counts = {}
    for (tbl,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    conn.close()
    return jsonify({"file": filename, "rows": counts})


def _open_folder(path):
    """Open a folder/file in the OS file manager (best effort, never fatal)."""
    try:
        if os.name == "nt":
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# API: Export monthly sales report (CSV + styled Excel .xlsx)
#
# Both formats share ONE data prep function (_month_report_data) so the
# numbers can never disagree. Layout of both files:
#   1. HEADER     - shop name, which month, when it was generated
#   2. DETAIL     - one row per item sold (SA date + time split out)
#   3. SUMMARY    - totals, profit, and a breakdown by payment method
#   4. BY ITEM    - qty + revenue per product, best sellers first
# Money columns are real NUMBERS so Excel can sum them.
# --------------------------------------------------------------------------- #
from calendar import month_name as MONTH_NAMES


def _month_report_data(year, month):
    """One month's sales, pre-shaped for reports. Returns a dict of
    detail lines + running totals, or raises ValueError on bad month."""
    if not (1 <= month <= 12):
        raise ValueError("month must be 1-12")

    ym = f"{year:04d}-{month:02d}"
    rows = get_db().execute(
        """SELECT datetime, barcode, item_name, qty, customer, payment,
                  unit_price, unit_cost, subtotal, card_fee
           FROM transactions
           WHERE type = 'OUT' AND substr(datetime, 1, 7) = ?
           ORDER BY id""", (ym,)).fetchall()

    lines = []
    total_sales = total_cogs = 0.0
    card_fees = 0.0
    items_sold = 0
    by_payment = {}
    by_item = {}

    for r in rows:
        qty = r["qty"] or 0
        unit_price = r["unit_price"] or 0
        subtotal = r["subtotal"]
        if subtotal is None:                       # pre-fix legacy rows only
            subtotal = unit_price * qty
        pay = (r["payment"] or "CASH").upper()
        if pay == "CARD":
            card_fees += r["card_fee"] or 0.0

        # "2026-08-05 09:14:03" -> "05/08/2026" + "09:14"
        try:
            dt = datetime.strptime(r["datetime"], DATETIME_FORMAT)
            day, clock = dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")
        except (TypeError, ValueError):
            day, clock = r["datetime"] or "", ""

        lines.append({
            "day": day, "clock": clock, "item": r["item_name"],
            "barcode": str(r["barcode"] or ""), "qty": qty,
            "unit_price": float(unit_price), "subtotal": float(subtotal),
            "customer": r["customer"], "pay": pay,
        })
        total_sales += subtotal
        total_cogs += (r["unit_cost"] or 0) * qty
        items_sold += qty
        by_payment[pay] = by_payment.get(pay, 0.0) + subtotal
        item = by_item.setdefault(r["item_name"], [0, 0.0])
        item[0] += qty
        item[1] += subtotal

    # Audit trail: removals & edits this month (undone stock-ins, deleted
    # products, edited receipts). These are NOT sales, so they're listed apart.
    removals = []
    for r in get_db().execute(
            """SELECT datetime, item_name, qty, unit_cost, type, note
               FROM transactions
               WHERE type IN ('UNDO', 'WRITE_OFF', 'EDIT', 'SWAP')
                 AND substr(datetime, 1, 7) = ?
               ORDER BY id""", (ym,)).fetchall():
        qty = r["qty"] or 0
        reason = ("edited" if "note" not in r.keys() else r["note"]) or ""
        if not reason or reason == "edited":
            reason = ("Stock-in reversed (removed from system)"
                      if r["type"] == "UNDO" else
                      "Stock-in edited" if r["type"] == "EDIT" else
                      "Customer swapped / exchanged an item"
                      if r["type"] == "SWAP" else
                      "Product deleted - removed from system" if qty > 0 else
                      "Product removed from catalogue")
        try:
            day = datetime.strptime(r["datetime"], DATETIME_FORMAT).strftime("%d/%m/%Y")
        except (TypeError, ValueError):
            day = r["datetime"] or ""
        removals.append({"day": day, "item": r["item_name"], "qty": abs(qty),
                         "reason": reason,
                         "value": round((r["unit_cost"] or 0) * abs(qty), 2)})

    # Accounts owed carried across months: what customers owed at the 1st of
    # this month (opening), and what they owe at month-end (closing) -
    # per customer for the current month, totals for any month.
    db = get_db()
    def balance_at(cutoff):
        row = db.execute(
            """SELECT COALESCE(SUM(
                   CASE WHEN type = 'OUT' AND payment = 'ACCOUNT' THEN subtotal
                        WHEN type = 'PAYMENT' THEN -subtotal ELSE 0 END), 0)
               FROM transactions WHERE datetime < ?""", (cutoff,)).fetchone()
        return round(row[0], 2)
    opening = balance_at(f"{ym}-01")
    next_month = (f"{year + 1:04d}-01" if month == 12
                  else f"{year:04d}-{month + 1:02d}") + "-01"
    closing = balance_at(next_month)
    payments_in = db.execute(
        """SELECT COALESCE(SUM(subtotal),0) FROM transactions
           WHERE type='PAYMENT' AND substr(datetime,1,7) = ?""", (ym,)
        ).fetchone()[0]
    acct_rows = db.execute(
        """SELECT customer,
                  COALESCE(SUM(
                    CASE WHEN type='OUT' AND payment='ACCOUNT' THEN subtotal
                         WHEN type='PAYMENT' THEN -subtotal ELSE 0 END),0) AS bal
           FROM transactions
           WHERE datetime < ? AND customer NOT IN ('CASH','SUPPLIER','STORE','')
           GROUP BY customer
           HAVING ROUND(bal, 2) != 0
           ORDER BY bal DESC""", (next_month,)).fetchall()
    accounts = {
        "opening": opening,
        "charged": round(by_payment.get("ACCOUNT", 0.0), 2),
        "payments": round(payments_in, 2),
        "closing": closing,
        "per_customer": [[r["customer"], round(r["bal"], 2)] for r in acct_rows],
    }

    # BUILD 20: money-out & losses for the month (returns, shop purchases,
    # damaged stock, bad debts) so every report tells the WHOLE money story.
    mdb = get_db()

    def _mtotal(sql):
        return round(mdb.execute(sql, (ym,)).fetchone()[0], 2)

    losses = {
        "returns": _mtotal(
            "SELECT COALESCE(SUM(-subtotal),0) FROM transactions "
            "WHERE type = 'RETURN' AND substr(datetime,1,7) = ?"),
        "damaged": _mtotal(
            "SELECT COALESCE(SUM(subtotal),0) FROM transactions "
            "WHERE type = 'WRITE_OFF' AND substr(COALESCE(note,''),1,7) = "
            "'damaged' AND substr(datetime,1,7) = ?"),
        "bad_debts": _mtotal(
            "SELECT COALESCE(SUM(subtotal),0) FROM transactions "
            "WHERE type = 'BAD_DEBT' AND substr(datetime,1,7) = ?"),
        "purchases": _mtotal(
            "SELECT COALESCE(SUM(amount),0) FROM purchases "
            "WHERE substr(datetime,1,7) = ?"),
    }

    reminders_sent = mdb.execute(
        "SELECT COUNT(*) FROM reminders WHERE substr(datetime,1,7) = ?",
        (ym,)).fetchone()[0]

    return {
        "title": f"{MONTH_NAMES[month]} {year}",
        "removals": removals,
        "accounts": accounts,
        "losses": losses,
        "reminders_sent": reminders_sent,
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "lines": lines,
        "total_sales": round(total_sales, 2),
        "total_cogs": round(total_cogs, 2),
        "profit": round(total_sales - total_cogs, 2),
        "card_fees": round(card_fees, 2),
        "items_sold": items_sold,
        "by_payment": by_payment,
        "by_item": sorted(by_item.items(), key=lambda kv: kv[1][0],
                          reverse=True),
    }


def _build_month_csv(data):
    """Plain-text CSV text of one month's report."""
    out = io.StringIO()
    w = csv.writer(out)

    w.writerow(["ConvergEX POS - Monthly Sales Report"])
    w.writerow(["Month", data["title"]])
    w.writerow(["Generated", data["generated"]])
    w.writerow([])
    w.writerow(["Date", "Time", "Item", "Barcode", "Qty",
                "Unit Price (R)", "Line Total (R)", "Customer", "Payment"])
    for L in data["lines"]:
        w.writerow([L["day"], L["clock"], L["item"], L["barcode"], L["qty"],
                    f"{L['unit_price']:.2f}", f"{L['subtotal']:.2f}",
                    L["customer"], L["pay"]])
    if not data["lines"]:
        w.writerow(["No sales recorded for this month"])

    w.writerow([])
    w.writerow(["SUMMARY"])
    w.writerow(["Total Sales (R)", f"{data['total_sales']:.2f}"])
    w.writerow(["Items Sold", data["items_sold"]])
    w.writerow(["Cost of Goods (R)", f"{data['total_cogs']:.2f}"])
    w.writerow(["Profit (R)", f"{data['profit']:.2f}"])
    w.writerow(["Card Fees Charged to Customers (2.5%) (R)",
                f"{data['card_fees']:.2f}"])
    w.writerow(["Total Collected (sales + card fees) (R)",
                f"{data['total_sales'] + data['card_fees']:.2f}"])
    w.writerow([])
    bp = data["by_payment"]
    w.writerow(["Paid Cash (R)", f"{bp.get('CASH', 0.0):.2f}"])
    w.writerow(["Paid Card (R)", f"{bp.get('CARD', 0.0):.2f}"])
    w.writerow(["On Account (R)", f"{bp.get('ACCOUNT', 0.0):.2f}"])

    w.writerow([])
    w.writerow(["MONEY OUT & LOSSES THIS MONTH"])
    lo = data["losses"]
    w.writerow(["Returns / exchanges (given back) (R)", f"{lo['returns']:.2f}"])
    w.writerow(["Shop purchases (stock bought) (R)", f"{lo['purchases']:.2f}"])
    w.writerow(["Damaged stock (value lost) (R)", f"{lo['damaged']:.2f}"])
    w.writerow(["Bad debts written off (R)", f"{lo['bad_debts']:.2f}"])

    w.writerow([])
    w.writerow(["WhatsApp payment reminders sent", data["reminders_sent"]])

    w.writerow([])
    w.writerow(["SALES BY ITEM"])
    w.writerow(["Item", "Qty Sold", "Revenue (R)"])
    for name, (qty, revenue) in data["by_item"]:
        w.writerow([name, qty, f"{revenue:.2f}"])

    w.writerow([])
    w.writerow(["REMOVED FROM SYSTEM (not sales: removals, edits, swaps)"])
    w.writerow(["Date", "Item", "Qty Removed", "What Happened", "Value at Cost (R)"])
    if not data["removals"]:
        w.writerow(["Nothing removed from system this month"])
    for rm in data["removals"]:
        w.writerow([rm["day"], rm["item"], rm["qty"], rm["reason"],
                    f"{rm['value']:.2f}"])

    acct = data["accounts"]
    w.writerow([])
    w.writerow(["ACCOUNTS OWED (carried across months)"])
    w.writerow(["Opening balance (owed at 1st of month) (R)",
                f"{acct['opening']:.2f}"])
    w.writerow(["+ Charged on account this month (R)", f"{acct['charged']:.2f}"])
    w.writerow(["- Payments received this month (R)", f"{acct['payments']:.2f}"])
    w.writerow(["Closing balance owed at month end (R)", f"{acct['closing']:.2f}"])
    if acct["per_customer"]:
        w.writerow([])
        w.writerow(["Customer", "Balance at month end (R)"])
        for name, bal in acct["per_customer"]:
            w.writerow([name, f"{bal:.2f}"])
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Daily report - one neat, human-readable Excel per day.
# Sections: SUMMARY -> SALES -> PAYMENTS RECEIVED -> STOCK RECEIVED ->
#           REMOVED / WRITTEN OFF -> DAY CLOSE (if the day was closed).
# --------------------------------------------------------------------------- #
def _daily_report_data(day):
    """Everything that happened on one calendar day ('YYYY-MM-DD'),
    pre-shaped for the daily Excel. Raises ValueError on a bad date."""
    datetime.strptime(day, DATE_FORMAT)                     # validation only
    db = get_db()
    rows = db.execute(
        "SELECT * FROM transactions WHERE datetime LIKE ? ORDER BY id",
        (f"{day}%",)).fetchall()

    sales, payments, stock_ins, removals = [], [], [], []
    for r in rows:
        t = r["type"]
        try:
            clock = datetime.strptime(r["datetime"], DATETIME_FORMAT).strftime("%H:%M")
        except (TypeError, ValueError):
            clock = r["datetime"] or ""
        if t == "OUT":
            sales.append({
                "time": clock, "item": r["item_name"], "qty": r["qty"] or 0,
                "unit_price": float(r["unit_price"] or 0),
                "subtotal": float(r["subtotal"] if r["subtotal"] is not None
                                  else (r["unit_price"] or 0) * (r["qty"] or 0)),
                "fee": float(r["card_fee"] or 0),
                "customer": r["customer"],
                "method": (r["payment"] or "CASH").upper()})
        elif t == "PAYMENT":
            amount = float(r["subtotal"] or 0)
            fee = float(r["card_fee"] or 0)
            payments.append({
                "time": clock, "customer": r["customer"], "amount": amount,
                "method": (r["payment"] or "CASH").upper(), "fee": fee,
                "charged": round(amount + fee, 2)})
        elif t == "IN":
            stock_ins.append({"time": clock, "barcode": str(r["barcode"] or ""),
                              "item": r["item_name"], "qty": r["qty"] or 0})
        elif t in ("UNDO", "WRITE_OFF", "EDIT", "SWAP"):
            qty = r["qty"] or 0
            reason = (r["note"] or "") if "note" in r.keys() else ""
            if not reason:
                reason = ("Stock-in reversed (removed from system)"
                          if t == "UNDO" else
                          "Stock-in edited" if t == "EDIT" else
                          "Customer swapped / exchanged an item"
                          if t == "SWAP" else
                          "Product deleted - removed from system" if qty > 0 else
                          "Product removed from catalogue")
            removals.append({"time": clock, "item": r["item_name"],
                             "qty": abs(qty), "reason": reason,
                             "value": round((r["unit_cost"] or 0) * abs(qty), 2)})
    def clock_of(r):
        try:
            return datetime.strptime(r["datetime"], DATETIME_FORMAT) \
                .strftime("%H:%M")
        except (TypeError, ValueError):
            return r["datetime"] or ""

    # BUILD 20 additions: returns/exchanges, day purchases, day bad debts.
    returns = [{"time": clock_of(r), "item": r["item_name"],
                "qty": r["qty"] or 0, "customer": r["customer"],
                "method": (r["payment"] or "CASH").upper(),
                "value": round(-(r["subtotal"] or 0), 2),
                "fee": round(-(r["card_fee"] or 0), 2)}
               for r in rows if r["type"] == "RETURN"]
    day_purchases = [{"time": clock_of(p), "shop": p["shop"],
                      "amount": float(p["amount"] or 0),
                      "note": p["note"] or ""}
                     for p in db.execute(
                         """SELECT * FROM purchases
                            WHERE datetime LIKE ? ORDER BY id""",
                         (f"{day}%",)).fetchall()]
    bad_debts = [{"time": clock_of(r), "customer": r["customer"],
                  "amount": round(r["subtotal"] or 0, 2)}
                 for r in rows if r["type"] == "BAD_DEBT"]
    damaged_total = round(sum(
        (r["subtotal"] or 0) for r in rows
        if r["type"] == "WRITE_OFF" and (r["note"] or "").startswith("damaged")
    ), 2)
    day_reminders = db.execute(
        "SELECT COUNT(*) FROM reminders WHERE datetime LIKE ?",
        (f"{day}%",)).fetchone()[0]

    return {
        "date": day,
        "title": datetime.strptime(day, DATE_FORMAT).strftime("%d/%m/%Y"),
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "summary": compute_summary(db, day),
        "sales": sales, "payments": payments,
        "stock_ins": stock_ins, "removals": removals,
        "units_in": sum(x["qty"] for x in stock_ins),
        "units_removed": sum(x["qty"] for x in removals),
        "returns": returns,
        "returns_total": round(sum(x["value"] for x in returns), 2),
        "day_purchases": day_purchases,
        "purchases_total": round(sum(x["amount"] for x in day_purchases), 2),
        "bad_debts": bad_debts,
        "baddebt_total": round(sum(x["amount"] for x in bad_debts), 2),
        "damaged_total": damaged_total,
        "reminders_today": day_reminders,
        "close": (dict(r2) if (r2 := db.execute(
            "SELECT * FROM budget WHERE date = ?", (day,)).fetchone()) else None),
    }


def _build_daily_xlsx(data):
    """Styled one-day workbook -> BytesIO."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    CUR = '"R "#,##0.00'
    NAVY, GREY, RED = "1F4E79", "F3F6FA", "C62828"
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Report"
    ws.merge_cells("A1:H1")
    c = ws.cell(row=1, column=1,
                value=f"ConvergEX POS - Daily Report - {data['title']}")
    c.font = Font(bold=True, size=16, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 28
    ws.cell(row=2, column=1,
            value=f"Generated: {data['generated']}").font = Font(italic=True)

    widths = [10, 24, 8, 12, 12, 12, 18, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ri = 4
    s = data["summary"]

    def section(label, color=NAVY):
        nonlocal ri
        ri += 1
        c = ws.cell(row=ri, column=1, value=label)
        c.font = Font(bold=True, size=13, color=color)

    def kv(label, value, currency=True, bold=False, indent=False):
        nonlocal ri
        ri += 1
        ws.cell(row=ri, column=2 if indent else 1,
                value=label).font = Font(bold=True)
        c = ws.cell(row=ri, column=3 if indent else 2, value=value)
        if currency:
            c.number_format = CUR
        if bold:
            c.font = Font(bold=True)

    # ---------- SUMMARY ----------
    cash_sales = sum(x["subtotal"] for x in data["sales"] if x["method"] == "CASH")
    card_sales = sum(x["subtotal"] for x in data["sales"] if x["method"] == "CARD")
    acct_sales = sum(x["subtotal"] for x in data["sales"] if x["method"] == "ACCOUNT")
    pay_cash = sum(p["amount"] for p in data["payments"] if p["method"] == "CASH")
    pay_card = sum(p["amount"] for p in data["payments"] if p["method"] == "CARD")

    section("SUMMARY OF THE DAY")
    kv("Total Sales (goods value)", s["total_sales"], bold=True)
    kv("Items Sold", s["items_sold"], currency=False, indent=True)
    kv("Profit", s["profit"], bold=True, indent=True)
    kv("Cash sales", cash_sales, indent=True)
    kv("Card sales", card_sales, indent=True)
    kv("On-Account sales (IOU)", acct_sales, indent=True)
    kv("Account payments in - CASH (IOU paid off)", pay_cash, indent=True)
    kv("Account payments in - CARD (IOU paid off)", pay_card, indent=True)
    kv(f"Card fees charged to customers ({s['card_fee_percent']}%)",
       s["total_card_fees"], bold=True, indent=True)
    kv("Expected on card machine (cards + fees)", s["expected_card"], bold=True)
    kv("Float this morning", s["float_today"])
    kv("Expected in till at close (float + cash in)", s["expected_till"], bold=True)
    kv("Units received into stock", data["units_in"], currency=False)
    kv("Units removed from system", data["units_removed"], currency=False)
    kv("Returns / exchanges today (money given back)",
       data["returns_total"], indent=True)
    section("MONEY OUT & LOSSES TODAY", color=RED)
    kv("Shop purchases today (stock bought)", data["purchases_total"],
       bold=True)
    kv("Damaged stock today (value lost)", data["damaged_total"], indent=True)
    kv("Bad debts written off today (left without paying)",
       data["baddebt_total"], indent=True)
    kv("WhatsApp payment reminders sent today", data["reminders_today"],
       currency=False)
    if data["close"]:
        section("DAY CLOSE (already cashed up)", color="2E7D32")
        kv("Counted in drawer", data["close"]["actual_cash"])
        kv("Difference", data["close"]["difference"], bold=True)
        kv("Float kept in till for tomorrow", data["close"]["float"])
        kv("Cash banking (taken out)",
           round((data["close"]["actual_cash"] or 0) -
                 (data["close"]["float"] or 0), 2), bold=True)

    # ---------- generic detail table ----------
    def table(title, headers, rows, money_cols=(), red_title=False):
        nonlocal ri
        section(title, color=RED if red_title else NAVY)
        ri += 1
        hr = ri
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=hr, column=col, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=RED if red_title else NAVY)
            c.border = border
        if not rows:
            ri += 1
            ws.cell(row=ri, column=1, value="- none -")
            return
        for idx, row in enumerate(rows):
            ri += 1
            for col, v in enumerate(row, start=1):
                c = ws.cell(row=ri, column=col, value=v)
                c.border = border
                if col in money_cols:
                    c.number_format = CUR
                if idx % 2 == 1:
                    c.fill = PatternFill("solid", fgColor=GREY)

    table("SALES (each item sold)",
          ["Time", "Item", "Qty", "Unit Price", "Line Total", "Card Fee",
           "Customer", "Paid By"],
          [[x["time"], x["item"], x["qty"], x["unit_price"], x["subtotal"],
            x["fee"] or None, x["customer"], x["method"]] for x in data["sales"]],
          money_cols=(4, 5, 6))

    table("PAYMENTS RECEIVED (account customers paying)",
          ["Time", "Customer", "Amount", "Method", "Card Fee", "Charged Total"],
          [[x["time"], x["customer"], x["amount"], x["method"],
            x["fee"] or None, x["charged"]] for x in data["payments"]],
          money_cols=(3, 5, 6))

    table("RETURNS / EXCHANGES (items that came back)",
          ["Time", "Item", "Qty", "Customer", "Was Paid By", "Value Back"],
          [[x["time"], x["item"], x["qty"], x["customer"], x["method"],
            x["value"]] for x in data["returns"]],
          money_cols=(6,), red_title=True)

    table("SHOP PURCHASES (stock bought - where & how much)",
          ["Time", "Shop", "Amount", "What Was Bought"],
          [[p["time"], p["shop"], p["amount"], p["note"]]
           for p in data["day_purchases"]],
          money_cols=(3,))

    table("LEFT WITHOUT PAYING (bad debts written off)",
          ["Time", "Customer", "Amount Written Off"],
          [[b["time"], b["customer"], b["amount"]]
           for b in data["bad_debts"]],
          money_cols=(3,), red_title=True)

    table("STOCK RECEIVED",
          ["Time", "Barcode", "Item", "Qty"],
          [[x["time"], x["barcode"], x["item"], x["qty"]] for x in data["stock_ins"]])

    table("REMOVED FROM SYSTEM (stock-ins reversed, products deleted, edits, swaps)",
          ["Time", "Item", "Qty Removed", "What Happened", "Value at Cost"],
          [[x["time"], x["item"], x["qty"], x["reason"], x["value"]]
           for x in data["removals"]],
          money_cols=(5,), red_title=True)

    _autofit(ws)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route("/api/export/daily_xlsx/<day>")
def api_export_daily_xlsx(day):
    """Daily report as styled Excel (browser download mode)."""
    try:
        data = _daily_report_data(day)
    except ValueError:
        return err("date must be YYYY-MM-DD")
    try:
        buf = _build_daily_xlsx(data)
    except ImportError:
        return err("Excel export needs openpyxl - run: pip install openpyxl", 500)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"daily_report_{day}.xlsx")


@app.route("/api/export/daily_save/<day>", methods=["POST"])
def api_export_daily_save(day):
    """Desktop mode: save the daily Excel into exports\\ and pop the folder."""
    try:
        data = _daily_report_data(day)
    except ValueError:
        return err("date must be YYYY-MM-DD")
    try:
        buf = _build_daily_xlsx(data)
    except ImportError:
        return err("Excel export needs openpyxl - run: pip install openpyxl", 500)
    fname = f"daily_report_{day}.xlsx"
    path = os.path.join(EXPORTS_DIR, fname)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    _open_folder(EXPORTS_DIR)
    return jsonify({"ok": True, "path": path, "file": fname})


@app.route("/api/export/monthly/<int:year>/<int:month>")
def api_export_monthly(year, month):
    """Download the monthly report as CSV (browser mode)."""
    try:
        data = _month_report_data(year, month)
    except ValueError as e:
        return err(str(e))
    return send_file(
        io.BytesIO(_build_month_csv(data).encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"sales_{year}_{month:02d}.csv")


def _build_month_xlsx(data):
    """Fully styled Excel workbook of one month's report -> BytesIO."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    CUR = '"R "#,##0.00'
    NAVY, GREY = "1F4E79", "F3F6FA"
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    HEADERS = ["Date", "Time", "Item", "Barcode", "Qty",
               "Unit Price", "Line Total", "Customer", "Payment"]
    WIDTHS = [11, 7, 22, 14, 6, 12, 12, 18, 11]

    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"

    # ---- title band ----
    ws.merge_cells(start_row=1, start_column=1, end_row=1,
                   end_column=len(HEADERS))
    c = ws.cell(row=1, column=1, value="ConvergEX POS - Monthly Sales Report")
    c.font = Font(bold=True, size=16, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 28
    ws.cell(row=2, column=1, value=f"Month: {data['title']}").font = Font(bold=True)
    ws.cell(row=3, column=1, value=f"Generated: {data['generated']}")

    # ---- header row ----
    hr = 5
    for col, (h, wdt) in enumerate(zip(HEADERS, WIDTHS), start=1):
        c = ws.cell(row=hr, column=col, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center")
        c.border = border
        ws.column_dimensions[get_column_letter(col)].width = wdt
    ws.freeze_panes = ws.cell(row=hr + 1, column=1)

    # ---- detail rows ----
    ri = hr
    for idx, L in enumerate(data["lines"]):
        ri += 1
        vals = [L["day"], L["clock"], L["item"], L["barcode"], L["qty"],
                L["unit_price"], L["subtotal"], L["customer"], L["pay"]]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(row=ri, column=col, value=v)
            c.border = border
            if col in (6, 7):
                c.number_format = CUR
            if col == 4:                      # barcode as text: keep zeros
                c.number_format = "@"
            if idx % 2 == 1:                  # zebra stripes
                c.fill = PatternFill("solid", fgColor=GREY)
    if not data["lines"]:
        ri += 1
        ws.cell(row=ri, column=1, value="No sales recorded for this month")
    ws.auto_filter.ref = f"A{hr}:I{ri}"

    # ---- summary + by-item blocks ----
    def section(label):
        nonlocal ri
        ri += 2
        c = ws.cell(row=ri, column=1, value=label)
        c.font = Font(bold=True, size=13, color=NAVY)

    def kv(label, value, currency=True, bold_value=False):
        nonlocal ri
        ri += 1
        ws.cell(row=ri, column=1, value=label).font = Font(bold=True)
        c = ws.cell(row=ri, column=2, value=value)
        if currency:
            c.number_format = CUR
        if bold_value:
            c.font = Font(bold=True)

    section("SUMMARY")
    kv("Total Sales", data["total_sales"])
    kv("Items Sold", data["items_sold"], currency=False)
    kv("Cost of Goods", data["total_cogs"])
    kv("Profit", data["profit"], bold_value=True)
    kv(f"Card Fees Charged to Customers ({CARD_FEE_PERCENT}%)",
       data["card_fees"])
    kv("Total Collected (sales + card fees)",
       round(data["total_sales"] + data["card_fees"], 2), bold_value=True)
    ri += 1
    bp = data["by_payment"]
    kv("Paid Cash", round(bp.get("CASH", 0.0), 2))
    kv("Paid Card", round(bp.get("CARD", 0.0), 2))
    kv("On Account", round(bp.get("ACCOUNT", 0.0), 2))

    section("MONEY OUT & LOSSES THIS MONTH")
    lo = data["losses"]
    kv("Returns / exchanges (money given back)", lo["returns"])
    kv("Shop purchases (stock bought)", lo["purchases"])
    kv("Damaged stock (value lost)", lo["damaged"])
    kv("Bad debts written off (left without paying)", lo["bad_debts"])
    kv("WhatsApp payment reminders sent", data["reminders_sent"],
       currency=False)

    section("SALES BY ITEM")
    ri += 1
    for col, h in enumerate(["Item", "Qty Sold", "Revenue"], start=1):
        c = ws.cell(row=ri, column=col, value=h)
        c.font = Font(bold=True)
        c.border = border
    for name, (qty, revenue) in data["by_item"]:
        ri += 1
        ws.cell(row=ri, column=1, value=name).border = border
        ws.cell(row=ri, column=2, value=qty).border = border
        c = ws.cell(row=ri, column=3, value=revenue)
        c.number_format = CUR
        c.border = border

    # ---- audit trail: removals & edits (never mixed into sales) ----
    section("REMOVED FROM SYSTEM (not sales: removals, edits, swaps)")
    ri += 1
    RED = "C62828"
    for col, h in enumerate(["Date", "Item", "Qty Removed", "What Happened",
                             "Value at Cost"], start=1):
        c = ws.cell(row=ri, column=col, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=RED)
        c.border = border
    if not data["removals"]:
        ri += 1
        ws.cell(row=ri, column=1,
                value="Nothing removed from system this month")
    for rm in data["removals"]:
        ri += 1
        ws.cell(row=ri, column=1, value=rm["day"]).border = border
        ws.cell(row=ri, column=2, value=rm["item"]).border = border
        ws.cell(row=ri, column=3, value=rm["qty"]).border = border
        ws.cell(row=ri, column=4, value=rm["reason"]).border = border
        c = ws.cell(row=ri, column=5, value=rm["value"])
        c.number_format = CUR
        c.border = border

    # ---- accounts owed, carried across months ----
    acct = data["accounts"]
    section("ACCOUNTS OWED (carried across months)")
    kv("Opening balance (owed at 1st of month)", acct["opening"])
    kv("+ Charged on account this month", acct["charged"])
    kv("- Payments received this month", acct["payments"])
    kv("Closing balance owed at month end", acct["closing"], bold_value=True)
    if acct["per_customer"]:
        ri += 1
        for col, h in enumerate(["Customer", "Balance at month end"], start=1):
            c = ws.cell(row=ri, column=col, value=h)
            c.font = Font(bold=True)
            c.border = border
        for name, bal in acct["per_customer"]:
            ri += 1
            ws.cell(row=ri, column=1, value=name).border = border
            c = ws.cell(row=ri, column=2, value=bal)
            c.number_format = CUR
            c.border = border

    _autofit(ws)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route("/api/export/monthly_xlsx/<int:year>/<int:month>")
def api_export_monthly_xlsx(year, month):
    """Download the monthly report as styled Excel (browser mode)."""
    try:
        data = _month_report_data(year, month)
    except ValueError as e:
        return err(str(e))
    try:
        buf = _build_month_xlsx(data)
    except ImportError:
        return err("Excel export needs openpyxl - run: pip install openpyxl", 500)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"sales_{year}_{month:02d}.xlsx")


def _autofit(ws, min_w=9, max_w=48):
    """Make every column wide enough for its content - kills Excel's
    dreaded #### display. Currency cells are measured WITH the R format."""
    from openpyxl.utils import get_column_letter
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        best = 0
        for c in col:
            v = c.value
            if v is None:
                continue
            if isinstance(v, (int, float)) and "R" in str(c.number_format):
                s = f"R {v:,.2f}"
            else:
                s = str(v)
            best = max(best, len(s))
        ws.column_dimensions[letter].width = max(min_w, min(max_w, best + 3))


def _build_stockins_xlsx(db):
    """Stock-in report that reconciles: what was scanned in vs what was
    sold vs what is ACTUALLY on the shelf now. -> BytesIO"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    NAVY, GREY, RED = "1F4E79", "F3F6FA", "C62828"
    CUR = '"R "#,##0.00'
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ---- A. reality check per product --------------------------------
    agg = {}
    for r in db.execute(
            "SELECT barcode, item_name, type, qty FROM transactions"):
        a = agg.setdefault(r["barcode"],
                           {"item": r["item_name"], "in": 0, "out": 0,
                            "removed": 0})
        q = r["qty"] or 0
        if r["type"] == "IN":
            a["in"] += q            # live receipts (already reflect edits;
        elif r["type"] == "OUT":    # undone receipts were deleted, so they
            a["out"] += q           # are NOT double-counted below)
        elif r["type"] == "WRITE_OFF":
            a["removed"] += q       # products deleted with stock
    # NOTE: UNDO rows are deliberately NOT subtracted here - their original
    # receipt row was deleted, so it never entered "Total Received".
    products = {p["barcode"]: p for p in db.execute("SELECT * FROM products")}
    for bc, p in products.items():
        agg.setdefault(bc, {"item": p["name"], "in": 0, "out": 0, "removed": 0})
    reality = []
    for bc, a in sorted(agg.items(), key=lambda kv: kv[1]["item"].lower()):
        prod = products.get(bc)
        on_hand = prod["qty_on_hand"] if prod else 0
        balances = (a["in"] - a["out"] - a["removed"] == on_hand)
        reality.append([str(bc), a["item"], a["in"], a["out"], a["removed"],
                        on_hand, "Active" if prod else "Removed from system",
                        "✓" if balances else "✗ CHECK"])

    # ---- B. receipts log ---------------------------------------------
    receipts = []
    for r in db.execute("""SELECT datetime, barcode, item_name, qty, note
                           FROM transactions WHERE type = 'IN'
                           ORDER BY id DESC"""):
        try:
            dt = datetime.strptime(r["datetime"], DATETIME_FORMAT)
            day, clock = dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")
        except (TypeError, ValueError):
            day, clock = r["datetime"] or "", ""
        note = (r["note"] or "")
        receipts.append([day, clock, str(r["barcode"] or ""), r["item_name"],
                         r["qty"] or 0,
                         "(edited ✎)" if note == "edited" else note])

    # ---- C. removed / edited entries ----------------------------------
    changes = []
    for r in db.execute("""SELECT datetime, item_name, type, qty, unit_cost,
                                  note
                           FROM transactions
                           WHERE type IN ('UNDO', 'WRITE_OFF', 'EDIT')
                           ORDER BY id"""):
        try:
            dt = datetime.strptime(r["datetime"], DATETIME_FORMAT)
            day, clock = dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")
        except (TypeError, ValueError):
            day, clock = r["datetime"] or "", ""
        what = r["note"] or ("stock-in reversed" if r["type"] == "UNDO"
                             else "removed from system")
        changes.append([day, clock, r["item_name"], r["qty"] or 0, what,
                        round((r["unit_cost"] or 0) * abs(r["qty"] or 0), 2)])

    wb = Workbook()
    ws = wb.active
    ws.title = "Stock Report"
    ws.merge_cells("A1:H1")
    c = ws.cell(row=1, column=1, value="ConvergEX POS - Stock-In Report")
    c.font = Font(bold=True, size=16, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=NAVY)
    c.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 28
    ws.cell(row=2, column=1,
            value=f"Generated: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
            ).font = Font(italic=True)
    ws.cell(row=3, column=1,
            value="On Hand Now = Received - Sold - Removed  (tick = balances)"
            ).font = Font(italic=True, size=9, color="777777")

    ri = 4
    def table(title, headers, rows, red=False, money_cols=()):
        nonlocal ri
        ri += 1
        c = ws.cell(row=ri, column=1, value=title)
        c.font = Font(bold=True, size=13, color=RED if red else NAVY)
        ri += 1
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=ri, column=col, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=RED if red else NAVY)
            c.border = border
        if not rows:
            ri += 1
            ws.cell(row=ri, column=1, value="- none -")
            return
        for idx, row in enumerate(rows):
            ri += 1
            for col, v in enumerate(row, start=1):
                c = ws.cell(row=ri, column=col, value=v)
                c.border = border
                if col in money_cols:
                    c.number_format = CUR
                if idx % 2 == 1:
                    c.fill = PatternFill("solid", fgColor=GREY)

    table("STOCK REALITY CHECK - scanned in vs sold vs on the shelf NOW",
          ["Barcode", "Item", "Total Received", "Sold", "Removed",
           "On Hand Now", "Status", "Check"],
          reality)
    table("RECEIPTS LOG - every stock receipt",
          ["Date", "Time", "Barcode", "Item", "Qty", "Note"], receipts)
    table("REMOVED / EDITED ENTRIES",
          ["Date", "Time", "Item", "Qty", "What Happened", "Value at Cost"],
          changes, red=True, money_cols=(6,))

    _autofit(ws)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route("/api/export/stockins_xlsx", methods=["POST"])
def api_export_stockins_xlsx():
    """Stock-in report (reality check + receipts + removed/edited), saved
    into exports\\ and the folder popped open (desktop-app friendly)."""
    try:
        buf = _build_stockins_xlsx(get_db())
    except ImportError:
        return err("Excel export needs openpyxl - run: pip install openpyxl", 500)
    fname = "stock_ins.xlsx"
    path = os.path.join(EXPORTS_DIR, fname)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    _open_folder(EXPORTS_DIR)
    return jsonify({"ok": True, "path": path, "file": fname})


# The desktop app window can't do browser "downloads", so these save the
# files into  exports\  next to the app and pop the folder open instead.
@app.route("/api/export/save_csv/<int:year>/<int:month>", methods=["POST"])
def api_export_save_csv(year, month):
    try:
        data = _month_report_data(year, month)
    except ValueError as e:
        return err(str(e))
    fname = f"sales_{year}_{month:02d}.csv"
    path = os.path.join(EXPORTS_DIR, fname)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(_build_month_csv(data))
    _open_folder(EXPORTS_DIR)
    return jsonify({"ok": True, "path": path, "file": fname})


@app.route("/api/export/save_xlsx/<int:year>/<int:month>", methods=["POST"])
def api_export_save_xlsx(year, month):
    try:
        data = _month_report_data(year, month)
    except ValueError as e:
        return err(str(e))
    try:
        buf = _build_month_xlsx(data)
    except ImportError:
        return err("Excel export needs openpyxl - run: pip install openpyxl", 500)
    fname = f"sales_{year}_{month:02d}.xlsx"
    path = os.path.join(EXPORTS_DIR, fname)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    _open_folder(EXPORTS_DIR)
    return jsonify({"ok": True, "path": path, "file": fname})


# --------------------------------------------------------------------------- #
# Backup on exit (uses sqlite's own backup API - safe with live connections)
# --------------------------------------------------------------------------- #
def backup_db():
    try:
        if os.path.exists(DB_PATH):
            fname = f"tuckshop_{date.today().strftime('%Y-%m-%d')}.db"
            dest = os.path.join(BACKUP_DIR, fname)
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(dest)
            src.backup(dst)
            dst.close()
            src.close()
            print("Backup created:", dest)
    except Exception as e:
        print("Backup failed:", e)


atexit.register(backup_db)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # 0.0.0.0 = listen on ALL network cards, so other laptops on the same
    # shop Wi-Fi can open the app in their browser (http://<this-ip>:5000).
    # The desktop window below still uses 127.0.0.1 - same server.
    server = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5000,
                               debug=False, threaded=True,
                               use_reloader=False),
        daemon=True)
    server.start()

    if webview:
        try:
            webview.create_window("ConvergEX POS", "http://127.0.0.1:5000",
                                  width=1366, height=768, resizable=True,
                                  text_select=True)
            webview.start()
        except Exception as e:                    # e.g. no WebView2 runtime
            print("Desktop window failed (%s) - falling back to browser." % e)
            webview = None

    if not webview:
        # Headless mode: open in the default browser and keep the server up.
        print("Open http://127.0.0.1:5000 in your browser.")
        try:
            import webbrowser
            webbrowser.open("http://127.0.0.1:5000")
        except Exception:
            pass
        threading.Event().wait()                  # block forever (Ctrl+C to quit)
