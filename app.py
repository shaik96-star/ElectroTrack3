import calendar
import csv
import io
import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from flask import (
    Flask, Response, flash, get_flashed_messages, jsonify,
    redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "DATABASE_PATH", os.path.join(app.instance_path, "electrotrack.db")
)
os.makedirs(app.instance_path, exist_ok=True)
os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
secret_key_path = os.path.join(app.instance_path, "secret_key")
if os.environ.get("SECRET_KEY"):
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
else:
    if not os.path.exists(secret_key_path):
        with open(secret_key_path, "w", encoding="utf-8") as secret_file:
            secret_file.write(secrets.token_urlsafe(48))
    with open(secret_key_path, encoding="utf-8") as secret_file:
        app.config["SECRET_KEY"] = secret_file.read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

# SQL snippet: compute effective usage from a reading row
READING_USAGE_SQL = (
    "CASE WHEN meter_readings.reading_type = 'usage' THEN meter_readings.consumption "
    "WHEN meter_readings.validation_status = 'valid' AND meter_readings.calculated_kwh IS NOT NULL "
    "THEN meter_readings.calculated_kwh ELSE 0 END"
)

# kgCO2 per kWh (India grid emission factor, approximate)
CO2_KG_PER_KWH = 0.82


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def refresh_consumer_consumption(connection, consumer_id):
    latest_by_meter = connection.execute(
        """
        SELECT meter_readings.reading_type, meter_readings.calculated_kwh,
               meter_readings.consumption, meter_readings.validation_status
        FROM meters
        JOIN meter_readings ON meter_readings.id = (
            SELECT reading.id FROM meter_readings AS reading
            WHERE reading.meter_id = meters.meter_id
              AND (reading.validation_status IN ('valid', 'baseline') OR reading.reading_type = 'usage')
            ORDER BY reading.reading_date DESC, reading.id DESC
            LIMIT 1
        )
        WHERE meters.consumer_id = ?
        """,
        (consumer_id,),
    ).fetchall()
    total = sum(
        row["consumption"] if row["reading_type"] == "usage"
        else (row["calculated_kwh"] or 0 if row["validation_status"] == "valid" else 0)
        for row in latest_by_meter
    )
    connection.execute("UPDATE consumers SET consumption = ? WHERE id = ?", (total, consumer_id))


# ---------------------------------------------------------------------------
# Database schema initialisation & migration
# ---------------------------------------------------------------------------

def migrate_multi_meter_schema():
    with sqlite3.connect(app.config["DATABASE"], isolation_level=None) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meters'"
        ).fetchone()
        if not exists:
            return
        for index in conn.execute("PRAGMA index_list(meters)").fetchall():
            if not index[2]:
                continue
            indexed_columns = [
                row[2] for row in conn.execute(f"PRAGMA index_info('{index[1]}')")
            ]
            if indexed_columns == ["consumer_id"]:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE meters_migration (
                        meter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        consumer_id INTEGER NOT NULL,
                        meter_code TEXT NOT NULL UNIQUE,
                        region TEXT NOT NULL DEFAULT 'Unassigned',
                        installed_date TEXT,
                        FOREIGN KEY (consumer_id) REFERENCES consumers (id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO meters_migration (meter_id, consumer_id, meter_code, region, installed_date) "
                    "SELECT meter_id, consumer_id, meter_code, region, installed_date FROM meters"
                )
                conn.execute("DROP TABLE meters")
                conn.execute("ALTER TABLE meters_migration RENAME TO meters")
                conn.commit()
                conn.execute("PRAGMA foreign_keys = ON")
                break


def initialize_database():
    os.makedirs(app.instance_path, exist_ok=True)
    migrate_multi_meter_schema()
    with get_db_connection() as conn:
        # ── users ──────────────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "role" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        if "security_question" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN security_question TEXT")
        if "security_answer_hash" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN security_answer_hash TEXT")
        # Keep one full administrator. Extra administrators from older copies
        # retain day-to-day editing rights as operators.
        conn.execute(
            "UPDATE users SET role = 'operator' WHERE role = 'admin' "
            "AND id <> (SELECT MIN(id) FROM users WHERE role = 'admin')"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_admin_account "
            "ON users(role) WHERE role = 'admin'"
        )

        # ── consumers ──────────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                meter_number TEXT NOT NULL UNIQUE,
                consumption INTEGER NOT NULL DEFAULT 0,
                consumer_type TEXT NOT NULL DEFAULT 'Residential',
                region TEXT NOT NULL DEFAULT 'Unassigned',
                phone TEXT,
                email TEXT,
                address TEXT,
                notes TEXT,
                tariff_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        consumer_cols = {r["name"] for r in conn.execute("PRAGMA table_info(consumers)")}
        for col, defn in {
            "consumer_type": "TEXT NOT NULL DEFAULT 'Residential'",
            "region": "TEXT NOT NULL DEFAULT 'Unassigned'",
            "phone": "TEXT",
            "email": "TEXT",
            "address": "TEXT",
            "notes": "TEXT",
            "tariff_id": "INTEGER",
            # SQLite does not allow a non-constant default when a column is
            # added to an existing table. Add it nullable, then backfill below.
            "created_at": "TEXT",
        }.items():
            if col not in consumer_cols:
                conn.execute(f"ALTER TABLE consumers ADD COLUMN {col} {defn}")
        conn.execute(
            "UPDATE consumers SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS consumers_created_at_default
            AFTER INSERT ON consumers
            WHEN NEW.created_at IS NULL
            BEGIN
                UPDATE consumers SET created_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END
            """
        )

        if conn.execute("SELECT COUNT(*) FROM consumers").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO consumers (name, meter_number, consumption, consumer_type, region) VALUES (?, ?, ?, ?, ?)",
                [
                    ("Rahul Kumar", "MTR-5001", 185, "Residential", "North"),
                    ("Priya Sharma", "MTR-5002", 420, "Residential", "West"),
                    ("Arjun Reddy", "MTR-5003", 210, "Residential", "South"),
                    ("Sneha Patel", "MTR-5004", 165, "Residential", "West"),
                    ("TechCorp Ltd", "MTR-5005", 1850, "Commercial", "North"),
                    ("GreenFarm Co-op", "MTR-5006", 540, "Agricultural", "East"),
                ],
            )

        # ── meters ─────────────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meters (
                meter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer_id INTEGER NOT NULL,
                meter_code TEXT NOT NULL UNIQUE,
                region TEXT NOT NULL DEFAULT 'Unassigned',
                installed_date TEXT,
                meter_type TEXT NOT NULL DEFAULT 'Standard',
                phase TEXT NOT NULL DEFAULT 'Single',
                capacity_kw REAL,
                FOREIGN KEY (consumer_id) REFERENCES consumers (id) ON DELETE CASCADE
            )
            """
        )
        meter_cols = {r["name"] for r in conn.execute("PRAGMA table_info(meters)")}
        for col, defn in {
            "meter_type": "TEXT NOT NULL DEFAULT 'Standard'",
            "phase": "TEXT NOT NULL DEFAULT 'Single'",
            "capacity_kw": "REAL",
        }.items():
            if col not in meter_cols:
                conn.execute(f"ALTER TABLE meters ADD COLUMN {col} {defn}")
        conn.execute(
            "INSERT OR IGNORE INTO meters (consumer_id, meter_code, region) "
            "SELECT id, meter_number, region FROM consumers"
        )

        # ── meter_readings ─────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meter_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer_id INTEGER NOT NULL,
                meter_id INTEGER,
                reading_date TEXT NOT NULL,
                consumption INTEGER NOT NULL CHECK (consumption >= 0),
                reading_type TEXT NOT NULL DEFAULT 'usage',
                cumulative_kwh INTEGER,
                calculated_kwh INTEGER,
                validation_status TEXT NOT NULL DEFAULT 'valid',
                review_type TEXT NOT NULL DEFAULT '',
                validation_note TEXT,
                is_sample INTEGER NOT NULL DEFAULT 0,
                recorded_by TEXT,
                FOREIGN KEY (consumer_id) REFERENCES consumers (id) ON DELETE CASCADE,
                FOREIGN KEY (meter_id) REFERENCES meters (meter_id) ON DELETE CASCADE
            )
            """
        )
        reading_cols = {r["name"] for r in conn.execute("PRAGMA table_info(meter_readings)")}
        for col, defn in {
            "meter_id": "INTEGER",
            "reading_type": "TEXT NOT NULL DEFAULT 'usage'",
            "cumulative_kwh": "INTEGER",
            "calculated_kwh": "INTEGER",
            "validation_status": "TEXT NOT NULL DEFAULT 'valid'",
            "review_type": "TEXT NOT NULL DEFAULT ''",
            "validation_note": "TEXT",
            "is_sample": "INTEGER NOT NULL DEFAULT 0",
            "recorded_by": "TEXT",
        }.items():
            if col not in reading_cols:
                conn.execute(f"ALTER TABLE meter_readings ADD COLUMN {col} {defn}")
        conn.execute(
            """
            UPDATE meter_readings
            SET meter_id = (SELECT meters.meter_id FROM meters WHERE meters.consumer_id = meter_readings.consumer_id)
            WHERE meter_id IS NULL
            """
        )
        conn.execute(
            """
            UPDATE meter_readings
            SET review_type = CASE
                WHEN validation_note LIKE '%decreased%' THEN 'meter_reset'
                ELSE 'usage_spike'
            END
            WHERE validation_status = 'flagged' AND review_type = ''
            """
        )

        # ── settings ───────────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )
        defaults = {
            "high_usage_threshold_kwh": "400",
            "reading_review_threshold_kwh": "600",
            "app_name": "ElectroTrack",
            "currency_symbol": "₹",
            "co2_kg_per_kwh": str(CO2_KG_PER_KWH),
            "org_name": "ElectroTrack Systems",
            "dark_mode_default": "1",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                (key, value),
            )

        # ── tariffs ────────────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                consumer_type TEXT NOT NULL DEFAULT 'Residential',
                rate_per_kwh REAL NOT NULL DEFAULT 5.5,
                fixed_charge REAL NOT NULL DEFAULT 0,
                tax_percent REAL NOT NULL DEFAULT 0,
                slab_json TEXT,
                description TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        if conn.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO tariffs (name, consumer_type, rate_per_kwh, fixed_charge, tax_percent, description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("Domestic LT", "Residential", 5.50, 50.00, 5.0, "Standard residential tariff"),
                    ("Commercial LT", "Commercial", 8.20, 150.00, 12.0, "Small & medium commercial"),
                    ("Industrial HT", "Industrial", 6.80, 500.00, 12.0, "High-tension industrial supply"),
                    ("Agricultural", "Agricultural", 1.50, 0.00, 0.0, "Subsidised agricultural tariff"),
                ],
            )

        # ── notifications / alerts log ─────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                consumer_id INTEGER,
                message TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (consumer_id) REFERENCES consumers (id) ON DELETE SET NULL
            )
            """
        )

        # ── Seed sample cumulative readings if none exist ──────────────────
        cumulative_count = conn.execute(
            "SELECT COUNT(*) FROM meter_readings WHERE reading_type = 'cumulative'"
        ).fetchone()[0]
        if cumulative_count == 0:
            sample_meters = conn.execute(
                "SELECT meter_id, consumer_id, meter_code FROM meters WHERE meter_code GLOB 'MTR-500*' ORDER BY meter_code"
            ).fetchall()
            today = date.today()
            sample_dates = [
                (today - timedelta(days=60)).isoformat(),
                (today - timedelta(days=30)).isoformat(),
                (today - timedelta(days=15)).isoformat(),
                (today - timedelta(days=1)).isoformat(),
            ]
            for si, meter in enumerate(sample_meters):
                cumulative_value = 10000 + si * 1000
                monthly_usage = 480 if meter["meter_code"] == "MTR-5002" else 180 + si * 25
                increments = [0, monthly_usage, monthly_usage + 15, 120 + si * 10]
                for ri, reading_date in enumerate(sample_dates):
                    if ri:
                        cumulative_value += increments[ri]
                    calculated = None if ri == 0 else increments[ri]
                    status = "baseline" if ri == 0 else "valid"
                    note = "Sample baseline reading." if ri == 0 else "Sample reading for demonstration."
                    conn.execute(
                        """
                        INSERT INTO meter_readings
                            (consumer_id, meter_id, reading_date, consumption, reading_type,
                             cumulative_kwh, calculated_kwh, validation_status, validation_note, is_sample)
                        VALUES (?, ?, ?, ?, 'cumulative', ?, ?, ?, ?, 1)
                        """,
                        (
                            meter["consumer_id"], meter["meter_id"], reading_date,
                            calculated or 0, cumulative_value, calculated, status, note,
                        ),
                    )
                conn.execute(
                    "UPDATE consumers SET consumption = ? WHERE id = ?",
                    (increments[-1], meter["consumer_id"]),
                )

        conn.commit()


initialize_database()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

SECURITY_QUESTIONS = (
    "What was the name of your first school?",
    "What was the name of your first pet?",
    "In what city were you born?",
    "What is the first name of your oldest cousin?",
)


def normalize_security_answer(answer):
    return " ".join((answer or "").casefold().split())


def valid_security_question(form):
    question = form.get("security_question", "").strip()
    answer = normalize_security_answer(form.get("security_answer", ""))
    if question not in SECURITY_QUESTIONS:
        return None, None, "Choose a security question."
    if len(answer) < 3:
        return None, None, "Security answers must be at least 3 characters."
    return question, answer, None

@app.before_request
def require_login():
    open_endpoints = {"login", "setup", "register", "forgot_password", "static"}
    if request.endpoint in open_endpoints:
        return None
    with get_db_connection() as conn:
        user = (
            conn.execute("SELECT id, role FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
            if session.get("user_id")
            else None
        )
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0:
        return redirect(url_for("setup"))
    if user is None:
        session.clear()
        return redirect(url_for("login"))
    session["role"] = user["role"]
    if request.method == "POST" and request.endpoint not in {
        "login", "setup", "register", "logout", "update_security_question", "delete_account"
    }:
        operator_endpoints = {
            "add_consumer", "update_consumer", "delete_consumer",
            "add_meter", "update_meter", "delete_meter",
            "add_reading", "import_readings_csv", "review_reading", "mark_alerts_read",
        }
        if user["role"] != "admin" and not (user["role"] == "operator" and request.endpoint in operator_endpoints):
            flash("This account is read-only for this action. Ask the administrator if you need more access.", "warning")
            return redirect(url_for("home"))
    return None


def _get_setting(key, default=""):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    with get_db_connection() as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        question, answer, question_error = valid_security_question(request.form)
        if not username or len(username) > 80:
            flash("Choose a username up to 80 characters.", "error")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif question_error:
            flash(question_error, "error")
        else:
            try:
                with get_db_connection() as conn:
                    cursor = conn.execute(
                        "INSERT INTO users (username, password_hash, role, security_question, security_answer_hash) VALUES (?, ?, 'admin', ?, ?)",
                        (username, generate_password_hash(password), question, generate_password_hash(answer)),
                    )
                    session.clear()
                    session["user_id"] = cursor.lastrowid
                    session["username"] = username
                    session["role"] = "admin"
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                flash("That username is already in use.", "error")
    return render_template("login.html", setup_mode=True, security_questions=SECURITY_QUESTIONS)


@app.route("/login", methods=["GET", "POST"])
def login():
    with get_db_connection() as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db_connection() as conn:
            user = conn.execute(
                "SELECT id, username, password_hash, role FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("home"))
        flash("Username or password is incorrect.", "error")
    return render_template("login.html", setup_mode=False)


@app.route("/register", methods=["GET", "POST"])
def register():
    with get_db_connection() as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        role     = request.form.get("role", "viewer").strip()
        question, answer, question_error = valid_security_question(request.form)
        if role not in {"operator", "viewer"}:
            role = "viewer"
        # Self-service registration is always read-only. Only the existing
        # administrator can choose between operator and viewer.
        if session.get("role") != "admin":
            role = "viewer"
        if not username or len(username) > 80:
            flash("Choose a username up to 80 characters.", "error")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif question_error:
            flash(question_error, "error")
        else:
            try:
                with get_db_connection() as conn:
                    cursor = conn.execute(
                        "INSERT INTO users (username, password_hash, role, security_question, security_answer_hash) VALUES (?, ?, ?, ?, ?)",
                        (username, generate_password_hash(password), role, question, generate_password_hash(answer)),
                    )
                    session.clear()
                    session["user_id"] = cursor.lastrowid
                    session["username"] = username
                    session["role"] = role
                flash("Account created successfully.", "success")
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                flash("That username is already in use.", "error")
    return render_template("login.html", setup_mode=False, register_mode=True, security_questions=SECURITY_QUESTIONS)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        step = request.form.get("step", "lookup")
        username = request.form.get("username", "").strip()
        with get_db_connection() as conn:
            user = conn.execute(
                "SELECT id, username, security_question, security_answer_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if step == "lookup":
            if user and user["security_question"] and user["security_answer_hash"]:
                return render_template("login.html", setup_mode=False, recovery_mode="answer",
                                       recovery_username=user["username"], recovery_question=user["security_question"])
            flash("If that account has recovery details, they can be used here. Older accounts must set a security question while signed in.", "error")
            return render_template("login.html", setup_mode=False, recovery_mode="lookup")

        answer = normalize_security_answer(request.form.get("security_answer", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not user or not user["security_answer_hash"] or not check_password_hash(user["security_answer_hash"], answer):
            flash("The account name or security answer does not match.", "error")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            with get_db_connection() as conn:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(password), user["id"]))
            flash("Password changed. Sign in with your new password.", "success")
            return redirect(url_for("login"))
        return render_template("login.html", setup_mode=False, recovery_mode="answer",
                               recovery_username=username,
                               recovery_question=user["security_question"] if user else "Security question")
    return render_template("login.html", setup_mode=False, recovery_mode="lookup")


@app.route("/account/security-question", methods=["POST"])
def update_security_question():
    question, answer, question_error = valid_security_question(request.form)
    current_password = request.form.get("current_password", "")
    with get_db_connection() as conn:
        user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if question_error:
            flash(question_error, "error")
        elif not user or not check_password_hash(user["password_hash"], current_password):
            flash("Enter your current password to update recovery details.", "error")
        else:
            conn.execute("UPDATE users SET security_question = ?, security_answer_hash = ? WHERE id = ?",
                         (question, generate_password_hash(answer), session["user_id"]))
            flash("Recovery question updated.", "success")
    return redirect(url_for("home", _anchor="settings"))


@app.route("/account/delete", methods=["POST"])
def delete_account():
    current_password = request.form.get("current_password", "")
    confirmation = request.form.get("confirm_delete", "").strip()
    with get_db_connection() as conn:
        user = conn.execute("SELECT id, password_hash, role FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        if not user or not check_password_hash(user["password_hash"], current_password):
            flash("Your password is incorrect. The account was not deleted.", "error")
            return redirect(url_for("home", _anchor="settings"))
        if confirmation != "DELETE":
            flash("Type DELETE exactly to confirm account removal.", "error")
            return redirect(url_for("home", _anchor="settings"))
        if user["role"] == "admin" and admin_count <= 1:
            flash("The last administrator account cannot be deleted. Create another admin account first.", "error")
            return redirect(url_for("home", _anchor="settings"))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    session.clear()
    flash("Your sign-in account was deleted. Shared consumer and meter records were kept.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

@app.route("/backup")
def download_database_backup():
    with get_db_connection() as source:
        destination = sqlite3.connect(":memory:")
        source.backup(destination)
        backup_data = destination.serialize()
        destination.close()
    filename = f"electrotrack-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    return Response(
        backup_data,
        content_type="application/vnd.sqlite3",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/reports/readings-template.csv")
def download_readings_csv_template():
    return csv_download("electrotrack-reading-import-template.csv", ["meter_code", "reading_date", "cumulative_kwh"], [])


@app.route("/backup/restore", methods=["POST"])
def restore_database_backup():
    uploaded = request.files.get("backup_file")
    if uploaded is None or not uploaded.filename:
        flash("Choose an ElectroTrack .db backup file to restore.", "error")
        return redirect(url_for("home", _anchor="reports"))
    if request.form.get("confirm_restore") != "yes":
        flash("Confirm that you understand restoring replaces the current database.", "error")
        return redirect(url_for("home", _anchor="reports"))
    database_path = app.config["DATABASE"]
    folder = os.path.dirname(database_path)
    staged_path = safety_path = None
    preserve_safety = False
    try:
        descriptor, staged_path = tempfile.mkstemp(prefix="electrotrack-restore-", suffix=".db", dir=folder)
        os.close(descriptor)
        uploaded.save(staged_path)
        with closing(sqlite3.connect(f"file:{staged_path}?mode=ro", uri=True)) as candidate:
            integrity = candidate.execute("PRAGMA integrity_check").fetchone()[0]
            fk_issue  = candidate.execute("PRAGMA foreign_key_check").fetchone()
            tables    = {row[0] for row in candidate.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required  = {"users", "consumers", "meters", "meter_readings", "settings"}
            if integrity != "ok" or fk_issue is not None or not required.issubset(tables):
                raise ValueError("Not a valid ElectroTrack database backup.")
        safety_path = os.path.join(
            folder, f"electrotrack-before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
        )
        with closing(sqlite3.connect(database_path)) as cur, closing(sqlite3.connect(safety_path)) as saf:
            cur.backup(saf)
        os.replace(staged_path, database_path)
        staged_path = None
        preserve_safety = True
        session.clear()
        flash("Database restored. Sign in with an account in that backup.", "success")
        return redirect(url_for("login"))
    except (sqlite3.Error, OSError, ValueError) as err:
        flash(f"Restore failed: {err}", "error")
        return redirect(url_for("home", _anchor="reports"))
    finally:
        for tp in (staged_path, safety_path if not preserve_safety else None):
            if tp and os.path.exists(tp):
                os.remove(tp)


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    today_date    = date.today()
    today         = today_date.isoformat()
    current_month = today[:7]
    selected_period   = request.args.get("period", "month")
    source_filter     = request.args.get("source", "all")
    compare_by        = request.args.get("compare_by", "consumer")
    if compare_by not in {"consumer", "meter"}:
        compare_by = "consumer"
    if source_filter not in {"all", "recorded", "sample"}:
        source_filter = "all"
    if selected_period not in {"week", "month", "last_month", "year"}:
        selected_period = "month"

    if selected_period == "week":
        period_start = today_date - timedelta(days=6)
        period_end   = today_date
        chart_title  = "Daily usage · last 7 days"
        chart_empty_message = "No readings in the last 7 days. Record a reading to populate the chart."
    elif selected_period == "last_month":
        current_month_start = today_date.replace(day=1)
        period_end   = current_month_start - timedelta(days=1)
        period_start = period_end.replace(day=1)
        chart_title  = f"Weekly usage · {period_start.strftime('%B %Y')}"
        chart_empty_message = f"No readings in {period_start.strftime('%B')} yet."
    elif selected_period == "year":
        period_start = today_date.replace(month=1, day=1)
        period_end   = today_date
        chart_title  = f"Monthly usage · {today_date.year}"
        chart_empty_message = f"No readings in {today_date.year} yet."
    else:  # month
        period_start = today_date.replace(day=1)
        period_end   = today_date
        chart_title  = "Weekly usage · this month"
        chart_empty_message = "No readings this month yet."

    with get_db_connection() as conn:
        # Settings
        high_usage_threshold = int(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?",
            ("high_usage_threshold_kwh",)
        ).fetchone()[0])
        reading_review_threshold = int(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?",
            ("reading_review_threshold_kwh",)
        ).fetchone()[0])
        co2_factor = float(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?",
            ("co2_kg_per_kwh",)
        ).fetchone()[0])
        currency = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'currency_symbol'"
        ).fetchone()[0]
        org_name = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'org_name'"
        ).fetchone()[0]

        # Consumers
        consumers = conn.execute(
            """
            SELECT consumers.id, consumers.name,
                   COALESCE(GROUP_CONCAT(meters.meter_code, ', '), '—') AS meter_number,
                   consumers.consumption, consumers.consumer_type, consumers.region,
                   COUNT(meters.meter_id) AS meter_count,
                   consumers.phone, consumers.email, consumers.address, consumers.notes,
                   consumers.tariff_id
            FROM consumers
            LEFT JOIN meters ON meters.consumer_id = consumers.id
            GROUP BY consumers.id
            ORDER BY consumers.id DESC
            """
        ).fetchall()
        consumer_options = conn.execute(
            "SELECT id, name FROM consumers ORDER BY name COLLATE NOCASE"
        ).fetchall()
        region_options = conn.execute(
            "SELECT DISTINCT region FROM consumers WHERE trim(region) <> '' ORDER BY region COLLATE NOCASE"
        ).fetchall()
        consumer_type_options = conn.execute(
            "SELECT DISTINCT consumer_type FROM consumers WHERE trim(consumer_type) <> '' ORDER BY consumer_type COLLATE NOCASE"
        ).fetchall()
        all_meters = conn.execute(
            """
            SELECT meters.meter_id, meters.consumer_id, meters.meter_code, meters.region,
                   meters.meter_type, meters.phase, meters.capacity_kw,
                   consumers.name, consumers.consumer_type
            FROM meters JOIN consumers ON consumers.id = meters.consumer_id
            ORDER BY consumers.name COLLATE NOCASE, meters.meter_code COLLATE NOCASE
            """
        ).fetchall()
        tariffs = conn.execute("SELECT * FROM tariffs ORDER BY name COLLATE NOCASE").fetchall()

        # Meter filter
        meter_filter_value = request.args.get("meter_id", "").strip()
        try:
            selected_meter_id = int(meter_filter_value) if meter_filter_value else None
        except ValueError:
            selected_meter_id = None
        valid_meter_ids = {row["meter_id"] for row in all_meters}
        if selected_meter_id not in valid_meter_ids:
            selected_meter_id = None

        meters_for_reading = conn.execute(
            """
            SELECT meters.meter_id, meters.meter_code, consumers.name
            FROM meters JOIN consumers ON consumers.id = meters.consumer_id
            ORDER BY consumers.name COLLATE NOCASE
            """
        ).fetchall()

        # Summary stats
        consumer_count = conn.execute("SELECT COUNT(*) FROM consumers").fetchone()[0]
        meter_count    = conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]
        today_total    = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE reading_date = ?",
            (today,),
        ).fetchone()[0]
        month_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE substr(reading_date, 1, 7) = ?",
            (current_month,),
        ).fetchone()[0]
        month_sample_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE substr(reading_date, 1, 7) = ? AND is_sample = 1",
            (current_month,),
        ).fetchone()[0]
        month_recorded_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE substr(reading_date, 1, 7) = ? AND is_sample = 0",
            (current_month,),
        ).fetchone()[0]
        year_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE substr(reading_date, 1, 4) = ?",
            (str(today_date.year),),
        ).fetchone()[0]
        has_sample_data = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM meter_readings WHERE is_sample = 1)"
        ).fetchone()[0]
        high_usage_count = conn.execute(
            "SELECT COUNT(*) FROM consumers WHERE consumption >= ?",
            (high_usage_threshold,),
        ).fetchone()[0]
        high_usage_consumers = conn.execute(
            """
            SELECT consumers.id, consumers.name,
                   COALESCE((SELECT GROUP_CONCAT(meter_code, ', ') FROM meters WHERE consumer_id = consumers.id), '—') AS meter_number,
                   consumers.consumption
            FROM consumers
            WHERE consumers.consumption >= ?
            ORDER BY consumers.consumption DESC, consumers.name COLLATE NOCASE
            LIMIT 8
            """,
            (high_usage_threshold,),
        ).fetchall()
        pending_reviews = conn.execute(
            """
            SELECT meter_readings.id, meter_readings.reading_date, meter_readings.cumulative_kwh,
                   meter_readings.calculated_kwh, meter_readings.review_type,
                   meter_readings.validation_note, consumers.name,
                   COALESCE(meters.meter_code, consumers.meter_number) AS meter_number
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            WHERE meter_readings.validation_status = 'flagged'
            ORDER BY meter_readings.reading_date, meter_readings.id
            """
        ).fetchall()

        # Extra dashboard intelligence -------------------------------------
        yesterday_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE reading_date = ?",
            ((today_date - timedelta(days=1)).isoformat(),),
        ).fetchone()[0]
        prev_month_end_date = today_date.replace(day=1) - timedelta(days=1)
        prev_month_start_date = prev_month_end_date.replace(day=1)
        prev_month_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings "
            "WHERE substr(reading_date, 1, 7) = ?",
            (prev_month_start_date.strftime("%Y-%m"),),
        ).fetchone()[0]
        month_delta_pct = (
            round((month_total - prev_month_total) * 100 / prev_month_total, 1)
            if prev_month_total else None
        )
        today_delta_pct = (
            round((today_total - yesterday_total) * 100 / yesterday_total, 1)
            if yesterday_total else None
        )
        daily_start = today_date - timedelta(days=13)
        daily_rows = conn.execute(
            f"""SELECT reading_date, COALESCE(SUM({READING_USAGE_SQL}), 0) AS total
                FROM meter_readings
                WHERE reading_date BETWEEN ? AND ?
                GROUP BY reading_date""",
            (daily_start.isoformat(), today),
        ).fetchall()
        daily_map = {row["reading_date"]: int(row["total"]) for row in daily_rows}
        daily_series = [
            {
                "date": (daily_start + timedelta(days=offset)).isoformat(),
                "total": daily_map.get((daily_start + timedelta(days=offset)).isoformat(), 0),
            }
            for offset in range(14)
        ]
        top_consumers = conn.execute(
            """
            SELECT consumers.id, consumers.name, consumers.consumption,
                   consumers.consumer_type, consumers.region,
                   COALESCE((SELECT GROUP_CONCAT(meter_code, ', ')
                             FROM meters WHERE consumer_id = consumers.id), '—') AS meter_number
            FROM consumers
            ORDER BY consumers.consumption DESC, consumers.name COLLATE NOCASE
            LIMIT 5
            """
        ).fetchall()
        total_capacity_kw = conn.execute(
            "SELECT COALESCE(SUM(capacity_kw), 0) FROM meters"
        ).fetchone()[0]
        highest_consumption = conn.execute(
            "SELECT COALESCE(MAX(consumption), 0) FROM consumers"
        ).fetchone()[0]
        healthy_pct = (
            round((consumer_count - high_usage_count) * 100 / consumer_count)
            if consumer_count else 100
        )
        average_consumer_usage = round(month_total / consumer_count) if consumer_count else 0
        month_elapsed_days = max(today_date.day, 1)
        month_days = calendar.monthrange(today_date.year, today_date.month)[1]
        month_projection = round(month_total / month_elapsed_days * month_days)

        # Consumer / region / type filter
        consumer_filter_value = request.args.get("consumer_id", "").strip()
        try:
            selected_consumer_id = int(consumer_filter_value) if consumer_filter_value else None
        except ValueError:
            selected_consumer_id = None
        valid_consumer_ids = {r["id"] for r in consumer_options}
        if selected_consumer_id not in valid_consumer_ids:
            selected_consumer_id = None
        region_filter = request.args.get("region", "")
        region_values = {r["region"] for r in region_options}
        if region_filter not in region_values:
            region_filter = ""
        consumer_type_filter = request.args.get("consumer_type", "")
        consumer_type_values = {r["consumer_type"] for r in consumer_type_options}
        if consumer_type_filter not in consumer_type_values:
            consumer_type_filter = ""

        consumer_conditions = []
        consumer_parameters = []
        if selected_consumer_id is not None:
            consumer_conditions.append("consumers.id = ?")
            consumer_parameters.append(selected_consumer_id)
        if region_filter:
            consumer_conditions.append("consumers.region = ?")
            consumer_parameters.append(region_filter)
        if consumer_type_filter:
            consumer_conditions.append("consumers.consumer_type = ?")
            consumer_parameters.append(consumer_type_filter)
        if selected_meter_id is not None:
            consumer_conditions.append("meter_readings.meter_id = ?")
            consumer_parameters.append(selected_meter_id)

        analytics_conditions = list(consumer_conditions)
        analytics_parameters = list(consumer_parameters)
        if source_filter == "recorded":
            analytics_conditions.append("meter_readings.is_sample = 0")
        elif source_filter == "sample":
            analytics_conditions.append("meter_readings.is_sample = 1")
        analytics_scope = (" AND " + " AND ".join(analytics_conditions)) if analytics_conditions else ""

        period_readings = conn.execute(
            f"""
            SELECT reading_date, {READING_USAGE_SQL} AS consumption
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            WHERE reading_date BETWEEN ? AND ? {analytics_scope}
            """,
            (period_start.isoformat(), period_end.isoformat(), *analytics_parameters),
        ).fetchall()

        recent_readings = conn.execute(
            """
            SELECT meter_readings.id, meter_readings.reading_date, meter_readings.consumption,
                   meter_readings.reading_type, meter_readings.cumulative_kwh,
                   meter_readings.calculated_kwh, meter_readings.validation_status,
                   meter_readings.validation_note,
                   consumers.name, COALESCE(meters.meter_code, consumers.meter_number) AS meter_number,
                   meter_readings.is_sample, meter_readings.recorded_by
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            ORDER BY meter_readings.reading_date DESC, meter_readings.id DESC
            LIMIT 20
            """
        ).fetchall()

        if compare_by == "meter":
            comparison_rows = conn.execute(
                f"""SELECT consumers.name || ' · ' || COALESCE(meters.meter_code, 'No meter') AS name,
                           SUM({READING_USAGE_SQL}) AS consumption
                    FROM meter_readings JOIN consumers ON consumers.id = meter_readings.consumer_id
                    LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
                    WHERE meter_readings.reading_date BETWEEN ? AND ? {analytics_scope}
                    GROUP BY meters.meter_id, consumers.name, meters.meter_code
                    HAVING SUM({READING_USAGE_SQL}) > 0
                    ORDER BY consumption DESC, name COLLATE NOCASE LIMIT 12""",
                (period_start.isoformat(), period_end.isoformat(), *analytics_parameters),
            ).fetchall()
        else:
            comparison_rows = conn.execute(
                f"""SELECT consumers.name, SUM({READING_USAGE_SQL}) AS consumption
                    FROM meter_readings JOIN consumers ON consumers.id = meter_readings.consumer_id
                    LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
                    WHERE meter_readings.reading_date BETWEEN ? AND ? {analytics_scope}
                    GROUP BY consumers.id, consumers.name
                    HAVING SUM({READING_USAGE_SQL}) > 0
                    ORDER BY consumption DESC, consumers.name COLLATE NOCASE LIMIT 8""",
                (period_start.isoformat(), period_end.isoformat(), *analytics_parameters),
            ).fetchall()

        # Consumer history
        history_value = request.args.get("history", "").strip()
        try:
            history_consumer_id = int(history_value) if history_value else None
        except ValueError:
            history_consumer_id = None
        history_consumer = history_rows = None
        history_start = request.args.get("history_start", "").strip()
        history_end   = request.args.get("history_end", "").strip()
        for dv in (history_start, history_end):
            if dv:
                try:
                    if datetime.strptime(dv, "%Y-%m-%d").date().isoformat() != dv:
                        raise ValueError
                except ValueError:
                    history_start = history_end = ""
                    break
        if history_start and history_end and history_start > history_end:
            history_start = history_end = ""
        history_meter_value = request.args.get("history_meter_id", "").strip()
        try:
            selected_history_meter_id = int(history_meter_value) if history_meter_value else None
        except ValueError:
            selected_history_meter_id = None
        history_meters = []
        history_rows   = []
        if history_consumer_id in valid_consumer_ids:
            history_consumer = conn.execute(
                "SELECT id, name, consumer_type, region FROM consumers WHERE id = ?",
                (history_consumer_id,),
            ).fetchone()
            history_meters = conn.execute(
                "SELECT meter_id, meter_code FROM meters WHERE consumer_id = ? ORDER BY meter_code COLLATE NOCASE",
                (history_consumer_id,),
            ).fetchall()
            if selected_history_meter_id not in {r["meter_id"] for r in history_meters}:
                selected_history_meter_id = None
            h_conditions  = ["meter_readings.consumer_id = ?"]
            h_parameters  = [history_consumer_id]
            if selected_history_meter_id is not None:
                h_conditions.append("meter_readings.meter_id = ?")
                h_parameters.append(selected_history_meter_id)
            if history_start:
                h_conditions.append("meter_readings.reading_date >= ?")
                h_parameters.append(history_start)
            if history_end:
                h_conditions.append("meter_readings.reading_date <= ?")
                h_parameters.append(history_end)
            history_rows = conn.execute(
                f"""SELECT meter_readings.reading_date, meter_readings.is_sample,
                           meters.meter_id, meters.meter_code,
                           SUM({READING_USAGE_SQL}) AS consumption
                    FROM meter_readings
                    LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
                    WHERE {' AND '.join(h_conditions)}
                    GROUP BY meter_readings.reading_date, meter_readings.is_sample, meters.meter_id, meters.meter_code
                    ORDER BY meter_readings.reading_date""",
                h_parameters,
            ).fetchall()

        # Reports
        report_summary = conn.execute(
            f"""SELECT COUNT(*) AS reading_count,
                       COALESCE(SUM({READING_USAGE_SQL}), 0) AS valid_usage,
                       SUM(CASE WHEN validation_status = 'flagged' THEN 1 ELSE 0 END) AS pending_count,
                       SUM(CASE WHEN is_sample = 1 THEN 1 ELSE 0 END) AS sample_count
                FROM meter_readings"""
        ).fetchone()
        report_monthly_rows = conn.execute(
            f"""SELECT substr(reading_date, 1, 7) AS month,
                       COALESCE(SUM({READING_USAGE_SQL}), 0) AS usage,
                       COUNT(*) AS reading_count
                FROM meter_readings
                WHERE substr(reading_date, 1, 4) = ?
                GROUP BY substr(reading_date, 1, 7)
                ORDER BY month DESC LIMIT 12""",
            (str(today_date.year),),
        ).fetchall()

        # Unread alert count
        unread_alerts = conn.execute(
            "SELECT COUNT(*) FROM alert_log WHERE is_read = 0"
        ).fetchone()[0]
        alert_history = conn.execute(
            """
            SELECT alert_log.id, alert_log.alert_type, alert_log.message,
                   alert_log.is_read, alert_log.created_at, consumers.name
            FROM alert_log
            LEFT JOIN consumers ON consumers.id = alert_log.consumer_id
            ORDER BY alert_log.id DESC LIMIT 50
            """
        ).fetchall()

        # Recent activity for timeline
        recent_activity = conn.execute(
            """
            SELECT meter_readings.reading_date, consumers.name, meter_readings.validation_status,
                   meter_readings.calculated_kwh, meter_readings.is_sample,
                   COALESCE(meters.meter_code, '?') AS meter_code
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            ORDER BY meter_readings.id DESC LIMIT 10
            """
        ).fetchall()

        # Billing estimates (last month per consumer)
        prev_month_start = (today_date.replace(day=1) - timedelta(days=1)).replace(day=1)
        prev_month_str   = prev_month_start.strftime("%Y-%m")
        billing_rows = conn.execute(
            f"""
            SELECT consumers.id, consumers.name,
                   COALESCE((SELECT GROUP_CONCAT(meters.meter_code, ', ')
                             FROM meters WHERE meters.consumer_id = consumers.id), '—') AS meter_number,
                   consumers.consumer_type, consumers.tariff_id,
                   COALESCE((SELECT SUM({READING_USAGE_SQL}) FROM meter_readings
                             WHERE meter_readings.consumer_id = consumers.id
                               AND substr(meter_readings.reading_date, 1, 7) = ?), 0) AS monthly_kwh
            FROM consumers
            ORDER BY monthly_kwh DESC
            """,
            (prev_month_str,),
        ).fetchall()

        tariff_map = {t["id"]: t for t in tariffs}
        billing_estimates = []
        for row in billing_rows:
            tariff = tariff_map.get(row["tariff_id"])
            kwh = int(row["monthly_kwh"] or 0)
            if tariff and kwh > 0:
                energy_charge = kwh * float(tariff["rate_per_kwh"])
                subtotal = energy_charge + float(tariff["fixed_charge"])
                tax_amount = subtotal * float(tariff["tax_percent"]) / 100
                total_charge = subtotal + tax_amount
            else:
                energy_charge = subtotal = tax_amount = total_charge = 0
            billing_estimates.append({
                "id": row["id"], "name": row["name"], "meter_number": row["meter_number"],
                "consumer_type": row["consumer_type"], "tariff_id": row["tariff_id"],
                "monthly_kwh": kwh, "tariff": tariff,
                "energy_charge": energy_charge, "fixed_charge": float(tariff["fixed_charge"]) if tariff and kwh > 0 else 0,
                "tax_amount": tax_amount, "total_charge": total_charge,
            })
        billing_estimated_total = sum(row["total_charge"] for row in billing_estimates)

        # CO2 totals
        co2_month_kg = round(month_total * co2_factor, 1)
        co2_year_kg  = round(year_total * co2_factor, 1)

    # Build Chart.js-compatible JSON data
    reading_frame = pd.DataFrame(
        [{"reading_date": r["reading_date"], "consumption": int(r["consumption"])} for r in period_readings]
    )
    period_totals = {}
    if selected_period == "week":
        chart_slots = [(( period_start + timedelta(days=d)).isoformat(), (period_start + timedelta(days=d)).strftime("%a %d")) for d in range(7)]
    elif selected_period == "year":
        chart_slots = [(str(m), calendar.month_abbr[m]) for m in range(1, 13)]
    else:
        chart_slots = [(str(w), f"Wk {w}") for w in range(1, 6)]

    if not reading_frame.empty:
        reading_frame["reading_date"] = pd.to_datetime(reading_frame["reading_date"])
        if selected_period == "week":
            reading_frame["slot"] = reading_frame["reading_date"].dt.strftime("%Y-%m-%d")
        elif selected_period == "year":
            reading_frame["slot"] = reading_frame["reading_date"].dt.month.astype(str)
        else:
            reading_frame["slot"] = ((reading_frame["reading_date"].dt.day - 1) // 7 + 1).astype(str)
        period_totals = reading_frame.groupby("slot", sort=False)["consumption"].sum().to_dict()

    chart_labels  = [label for _, label in chart_slots]
    chart_data    = [int(period_totals.get(key, 0)) for key, _ in chart_slots]
    chart_is_empty = not any(chart_data)
    largest_chart_value = max(chart_data, default=0) or 1
    chart_values = [
        {"label": label, "total": total, "width": round(total * 100 / largest_chart_value)}
        for label, total in zip(chart_labels, chart_data)
    ]

    comparison_frame = pd.DataFrame(
        [{"name": r["name"], "consumption": int(r["consumption"])} for r in comparison_rows]
    )
    if not comparison_frame.empty:
        comparison_frame = comparison_frame.sort_values("consumption", ascending=True)

    # Interactive charts are rendered client-side from these payloads; this
    # keeps the workspace lightweight and lets charts animate and respond to
    # pointer input instead of loading static PNG images.
    comparison_labels = comparison_frame["name"].tolist() if not comparison_frame.empty else []
    comparison_data = comparison_frame["consumption"].astype(int).tolist() if not comparison_frame.empty else []

    # History data for chart
    history_frame = pd.DataFrame(
        [
            {
                "reading_date": r["reading_date"],
                "is_sample": "Sample" if r["is_sample"] else "Recorded",
                "consumption": int(r["consumption"] or 0),
            }
            for r in history_rows
        ]
    )
    history_chartjs = None
    if history_consumer and not history_frame.empty:
        hf = history_frame.pivot_table(
            index="reading_date", columns="is_sample", values="consumption", aggfunc="sum", fill_value=0
        ).tail(24)
        history_chartjs = {
            "labels": hf.index.tolist(),
            "sample": hf["Sample"].tolist() if "Sample" in hf else [0] * len(hf),
            "recorded": hf["Recorded"].tolist() if "Recorded" in hf else [0] * len(hf),
        }
    history_empty = history_consumer is None or history_frame.empty

    flash_messages = get_flashed_messages(with_categories=True)
    open_form      = request.args.get("add") == "1"

    # Single serialisable payload for the interactive layer (command palette,
    # charts, reading preview and the client-side consumer search).
    et_data = {
        "chart": {
            "labels": chart_labels,
            "values": chart_data,
            "title": chart_title,
            "period": selected_period,
        },
        "comparison": {
            "labels": comparison_labels,
            "values": comparison_data,
            "title": "Meter usage comparison" if compare_by == "meter" else "Consumer usage comparison",
        },
        "history": history_chartjs,
        "daily": daily_series,
        "consumers": [
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["consumer_type"],
                "region": row["region"],
                "meters": row["meter_number"],
                "usage": row["consumption"],
                "phone": row["phone"] or "",
                "email": row["email"] or "",
            }
            for row in consumers
        ],
        "meters": [
            {
                "id": row["meter_id"],
                "code": row["meter_code"],
                "consumer": row["name"],
                "type": row["meter_type"],
                "phase": row["phase"],
                "capacity": row["capacity_kw"],
            }
            for row in all_meters
        ],
        "topConsumers": [
            {
                "name": row["name"],
                "usage": row["consumption"],
                "region": row["region"],
                "type": row["consumer_type"],
            }
            for row in top_consumers
        ],
        "activity": [
            {
                "date": row["reading_date"],
                "name": row["name"],
                "status": row["validation_status"],
                "kwh": row["calculated_kwh"] or 0,
                "sample": bool(row["is_sample"]),
                "meter": row["meter_code"],
            }
            for row in recent_activity
        ],
        "settings": {
            "highThreshold": high_usage_threshold,
            "reviewThreshold": reading_review_threshold,
            "currency": currency,
            "co2Factor": co2_factor,
            "orgName": org_name,
        },
    }

    return render_template(
        "index.html",
        consumers=consumers,
        consumer_count=consumer_count,
        meter_count=meter_count,
        today_total=today_total,
        month_total=month_total,
        year_total=year_total,
        high_usage_count=high_usage_count,
        month_sample_total=month_sample_total,
        month_recorded_total=month_recorded_total,
        has_sample_data=has_sample_data,
        high_usage_threshold=high_usage_threshold,
        reading_review_threshold=reading_review_threshold,
        high_usage_consumers=high_usage_consumers,
        chart_labels=json.dumps(chart_labels),
        chart_data=json.dumps(chart_data),
        chart_values=chart_values,
        chart_title=chart_title,
        chart_empty_message=chart_empty_message,
        chart_is_empty=chart_is_empty,
        comparison_labels=json.dumps(comparison_labels),
        comparison_data=json.dumps(comparison_data),
        comparison_title=et_data["comparison"]["title"],
        selected_period=selected_period,
        recent_readings=recent_readings,
        recent_activity=recent_activity,
        pending_reviews=pending_reviews,
        pending_count=len(pending_reviews),
        meters_for_reading=meters_for_reading,
        all_meters=all_meters,
        consumer_options=consumer_options,
        region_options=region_options,
        consumer_type_options=consumer_type_options,
        selected_consumer_id=selected_consumer_id,
        selected_region=region_filter,
        selected_consumer_type=consumer_type_filter,
        source_filter=source_filter,
        compare_by=compare_by,
        selected_meter_id=selected_meter_id,
        history_consumer=history_consumer,
        history_chartjs=json.dumps(history_chartjs) if history_chartjs else "null",
        history_empty=history_empty,
        history_meters=history_meters,
        history_rows=history_rows,
        selected_history_meter_id=selected_history_meter_id,
        history_start=history_start,
        history_end=history_end,
        report_summary=report_summary,
        report_monthly_rows=report_monthly_rows,
        today=today,
        flash_messages=flash_messages,
        open_form=open_form,
        security_questions=SECURITY_QUESTIONS,
        tariffs=tariffs,
        tariff_map=tariff_map,
        billing_rows=billing_estimates,
        billing_estimated_total=billing_estimated_total,
        prev_month_str=prev_month_str,
        co2_month_kg=co2_month_kg,
        co2_year_kg=co2_year_kg,
        co2_factor=co2_factor,
        co2_trees_year=round(co2_year_kg / 21, 1),
        currency=currency,
        org_name=org_name,
        unread_alerts=unread_alerts,
        alert_history=alert_history,
        yesterday_total=yesterday_total,
        prev_month_total=prev_month_total,
        month_delta_pct=month_delta_pct,
        today_delta_pct=today_delta_pct,
        daily_series=daily_series,
        top_consumers=top_consumers,
        total_capacity_kw=total_capacity_kw,
        highest_consumption=highest_consumption,
        healthy_pct=healthy_pct,
        average_consumer_usage=average_consumer_usage,
        month_projection=month_projection,
        month_elapsed_days=month_elapsed_days,
        month_days=month_days,
        et_data=et_data,
    )


# ---------------------------------------------------------------------------
# API endpoints (JSON)
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    today_date    = date.today()
    current_month = today_date.strftime("%Y-%m")
    with get_db_connection() as conn:
        co2_factor = float(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'co2_kg_per_kwh'"
        ).fetchone()[0])
        month_total = conn.execute(
            f"SELECT COALESCE(SUM({READING_USAGE_SQL}), 0) FROM meter_readings WHERE substr(reading_date, 1, 7) = ?",
            (current_month,),
        ).fetchone()[0]
        consumer_count = conn.execute("SELECT COUNT(*) FROM consumers").fetchone()[0]
        pending_count  = conn.execute(
            "SELECT COUNT(*) FROM meter_readings WHERE validation_status = 'flagged'"
        ).fetchone()[0]
    return jsonify({
        "month_kwh": month_total,
        "consumer_count": consumer_count,
        "pending_reviews": pending_count,
        "co2_kg": round(month_total * co2_factor, 1),
        "as_of": today_date.isoformat(),
    })


@app.route("/api/chart-data")
def api_chart_data():
    """Return period consumption data as JSON for Chart.js."""
    period = request.args.get("period", "month")
    today_date = date.today()
    if period == "week":
        period_start = today_date - timedelta(days=6)
        period_end   = today_date
    elif period == "year":
        period_start = today_date.replace(month=1, day=1)
        period_end   = today_date
    else:
        period_start = today_date.replace(day=1)
        period_end   = today_date

    with get_db_connection() as conn:
        rows = conn.execute(
            f"SELECT reading_date, {READING_USAGE_SQL} AS consumption FROM meter_readings WHERE reading_date BETWEEN ? AND ?",
            (period_start.isoformat(), period_end.isoformat()),
        ).fetchall()

    df = pd.DataFrame([{"reading_date": r["reading_date"], "consumption": int(r["consumption"])} for r in rows])
    if df.empty:
        return jsonify({"labels": [], "data": []})

    df["reading_date"] = pd.to_datetime(df["reading_date"])
    if period == "week":
        df["slot"] = df["reading_date"].dt.strftime("%a %d")
        labels = [(period_start + timedelta(days=d)).strftime("%a %d") for d in range(7)]
        totals = df.groupby("slot")["consumption"].sum().to_dict()
        data   = [int(totals.get(l, 0)) for l in labels]
    elif period == "year":
        df["slot"] = df["reading_date"].dt.month
        labels = list(calendar.month_abbr[1:])
        totals = df.groupby("slot")["consumption"].sum().to_dict()
        data   = [int(totals.get(m, 0)) for m in range(1, 13)]
    else:
        df["slot"] = ((df["reading_date"].dt.day - 1) // 7 + 1)
        labels = [f"Wk {w}" for w in range(1, 6)]
        totals = df.groupby("slot")["consumption"].sum().to_dict()
        data   = [int(totals.get(w, 0)) for w in range(1, 6)]

    return jsonify({"labels": labels, "data": data})


@app.route("/api/consumers/<int:consumer_id>/readings")
def api_consumer_readings(consumer_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""SELECT reading_date, {READING_USAGE_SQL} AS kwh, validation_status
                FROM meter_readings WHERE consumer_id = ?
                AND reading_type = 'cumulative'
                ORDER BY reading_date""",
            (consumer_id,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/meters/<int:meter_id>/latest")
def api_meter_latest(meter_id):
    """Context used by the record-reading dialog for a live usage preview."""
    with get_db_connection() as conn:
        meter = conn.execute(
            """SELECT meters.meter_id, meters.meter_code, meters.capacity_kw,
                      consumers.name AS consumer_name
               FROM meters JOIN consumers ON consumers.id = meters.consumer_id
               WHERE meters.meter_id = ?""",
            (meter_id,),
        ).fetchone()
        if meter is None:
            return jsonify({"error": "Meter not found"}), 404
        previous = conn.execute(
            """SELECT reading_date, cumulative_kwh
               FROM meter_readings
               WHERE meter_id = ? AND reading_type = 'cumulative'
                 AND validation_status IN ('baseline', 'valid')
                 AND cumulative_kwh IS NOT NULL
               ORDER BY reading_date DESC, id DESC LIMIT 1""",
            (meter_id,),
        ).fetchone()
        latest = conn.execute(
            """SELECT reading_date, cumulative_kwh
               FROM meter_readings
               WHERE meter_id = ? AND reading_type = 'cumulative'
               ORDER BY reading_date DESC, id DESC LIMIT 1""",
            (meter_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) FROM meter_readings WHERE meter_id = ? AND validation_status = 'flagged'",
            (meter_id,),
        ).fetchone()[0]
        review_threshold = int(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'reading_review_threshold_kwh'"
        ).fetchone()[0])
    return jsonify({
        "meter_id": meter["meter_id"],
        "meter_code": meter["meter_code"],
        "consumer_name": meter["consumer_name"],
        "capacity_kw": meter["capacity_kw"],
        "previous_date": previous["reading_date"] if previous else None,
        "previous_cumulative": previous["cumulative_kwh"] if previous else None,
        "latest_date": latest["reading_date"] if latest else None,
        "pending": pending,
        "review_threshold": review_threshold,
    })


# ---------------------------------------------------------------------------
# Consumers CRUD
# ---------------------------------------------------------------------------

@app.route("/consumers", methods=["POST"])
def add_consumer():
    name          = request.form.get("name", "").strip()
    meter_number  = request.form.get("meter_code", "").strip().upper()
    consumer_type = request.form.get("consumer_type", "Residential").strip()
    region        = request.form.get("region", "Unassigned").strip()
    phone         = request.form.get("phone", "").strip()
    email         = request.form.get("email", "").strip()
    address       = request.form.get("address", "").strip()
    tariff_id     = request.form.get("tariff_id", "").strip()
    try:
        tariff_id = int(tariff_id) if tariff_id else None
    except ValueError:
        tariff_id = None

    if not name or not meter_number:
        flash("Enter both a consumer name and meter number.", "error")
        return redirect(url_for("home", add=1))
    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO consumers (name, meter_number, consumer_type, region, phone, email, address, tariff_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, meter_number, consumer_type, region, phone or None, email or None, address or None, tariff_id),
            )
            conn.execute(
                "INSERT INTO meters (consumer_id, meter_code, region) VALUES (?, ?, ?)",
                (cursor.lastrowid, meter_number, region),
            )
    except sqlite3.IntegrityError:
        flash("That meter number is already registered. Enter a unique meter number.", "error")
        return redirect(url_for("home", add=1))
    flash(f"{name} was added to your consumers.", "success")
    return redirect(url_for("home"))


@app.route("/consumers/<int:consumer_id>/update", methods=["POST"])
def update_consumer(consumer_id):
    name          = request.form.get("name", "").strip()
    consumer_type = request.form.get("consumer_type", "Residential").strip()
    region        = request.form.get("region", "Unassigned").strip()
    phone         = request.form.get("phone", "").strip()
    email         = request.form.get("email", "").strip()
    address       = request.form.get("address", "").strip()
    notes         = request.form.get("notes", "").strip()
    tariff_id     = request.form.get("tariff_id", "").strip()
    try:
        tariff_id = int(tariff_id) if tariff_id else None
    except ValueError:
        tariff_id = None

    if not name or not region:
        flash("Enter a consumer name and region.", "error")
        return redirect(url_for("home", edit=consumer_id))
    with get_db_connection() as conn:
        result = conn.execute(
            "UPDATE consumers SET name = ?, consumer_type = ?, region = ?, phone = ?, email = ?, address = ?, notes = ?, tariff_id = ? WHERE id = ?",
            (name, consumer_type, region, phone or None, email or None, address or None, notes or None, tariff_id, consumer_id),
        )
        if result.rowcount == 0:
            flash("That consumer could not be found.", "error")
            return redirect(url_for("home"))
        conn.execute("UPDATE meters SET region = ? WHERE consumer_id = ?", (region, consumer_id))
    flash(f"{name}'s record was updated.", "success")
    return redirect(url_for("home"))


@app.route("/consumers/<int:consumer_id>/delete", methods=["POST"])
def delete_consumer(consumer_id):
    with get_db_connection() as conn:
        result = conn.execute("DELETE FROM consumers WHERE id = ?", (consumer_id,))
    if result.rowcount:
        flash("Consumer record deleted.", "success")
    else:
        flash("That consumer could not be found.", "error")
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# Meters CRUD
# ---------------------------------------------------------------------------

@app.route("/meters", methods=["POST"])
def add_meter():
    consumer_id_value = request.form.get("consumer_id", "").strip()
    meter_code        = request.form.get("meter_code", "").strip().upper()
    region            = request.form.get("region", "").strip()
    meter_type        = request.form.get("meter_type", "Standard").strip()
    phase             = request.form.get("phase", "Single").strip()
    capacity_raw      = request.form.get("capacity_kw", "").strip()
    try:
        consumer_id = int(consumer_id_value)
    except ValueError:
        consumer_id = 0
    try:
        capacity_kw = float(capacity_raw) if capacity_raw else None
    except ValueError:
        capacity_kw = None
    if not meter_code:
        flash("Enter a meter code.", "error")
        return redirect(url_for("home", _anchor="meters"))
    try:
        with get_db_connection() as conn:
            consumer = conn.execute("SELECT id, region FROM consumers WHERE id = ?", (consumer_id,)).fetchone()
            if consumer is None:
                flash("Choose a valid consumer for this meter.", "error")
                return redirect(url_for("home", _anchor="meters"))
            conn.execute(
                "INSERT INTO meters (consumer_id, meter_code, region, meter_type, phase, capacity_kw) VALUES (?, ?, ?, ?, ?, ?)",
                (consumer_id, meter_code, region or consumer["region"], meter_type, phase, capacity_kw),
            )
    except sqlite3.IntegrityError:
        flash("That meter code is already registered.", "error")
        return redirect(url_for("home", _anchor="meters"))
    flash(f"Meter {meter_code} was added.", "success")
    return redirect(url_for("home", _anchor="meters"))


@app.route("/meters/<int:meter_id>/update", methods=["POST"])
def update_meter(meter_id):
    meter_code = request.form.get("meter_code", "").strip().upper()
    meter_type = request.form.get("meter_type", "Standard").strip()
    phase      = request.form.get("phase", "Single").strip()
    capacity_raw = request.form.get("capacity_kw", "").strip()
    try:
        capacity_kw = float(capacity_raw) if capacity_raw else None
    except ValueError:
        capacity_kw = None
    if not meter_code:
        flash("Enter a meter code.", "error")
        return redirect(url_for("home", _anchor="meters"))
    try:
        with get_db_connection() as conn:
            result = conn.execute(
                "UPDATE meters SET meter_code = ?, meter_type = ?, phase = ?, capacity_kw = ? WHERE meter_id = ?",
                (meter_code, meter_type, phase, capacity_kw, meter_id),
            )
            if not result.rowcount:
                flash("That meter could not be found.", "error")
                return redirect(url_for("home", _anchor="meters"))
    except sqlite3.IntegrityError:
        flash("That meter code is already in use.", "error")
        return redirect(url_for("home", _anchor="meters"))
    flash(f"Meter {meter_code} updated.", "success")
    return redirect(url_for("home", _anchor="meters"))


@app.route("/meters/<int:meter_id>/delete", methods=["POST"])
def delete_meter(meter_id):
    with get_db_connection() as conn:
        reading_count = conn.execute(
            "SELECT COUNT(*) FROM meter_readings WHERE meter_id = ?", (meter_id,)
        ).fetchone()[0]
        if reading_count:
            flash("This meter has saved readings and cannot be deleted.", "error")
            return redirect(url_for("home", _anchor="meters"))
        result = conn.execute("DELETE FROM meters WHERE meter_id = ?", (meter_id,))
    if result.rowcount:
        flash("Meter deleted.", "success")
    else:
        flash("That meter could not be found.", "error")
    return redirect(url_for("home", _anchor="meters"))


# ---------------------------------------------------------------------------
# Readings CRUD
# ---------------------------------------------------------------------------

@app.route("/readings", methods=["POST"])
def add_reading():
    meter_id_value   = request.form.get("meter_id", "").strip()
    reading_date     = request.form.get("reading_date", "").strip()
    cumulative_value = request.form.get("cumulative_kwh", "").strip()
    try:
        meter_id       = int(meter_id_value)
        cumulative_kwh = int(cumulative_value)
        datetime.strptime(reading_date, "%Y-%m-%d")
        if cumulative_kwh < 0:
            raise ValueError
    except (TypeError, ValueError):
        flash("Enter a valid date and a cumulative meter value of 0 or more.", "error")
        return redirect(url_for("home", reading=1))

    if date.fromisoformat(reading_date) > date.today():
        flash("A meter reading date cannot be in the future.", "error")
        return redirect(url_for("home", reading=1))

    with get_db_connection() as conn:
        meter = conn.execute(
            "SELECT meter_id, consumer_id FROM meters WHERE meter_id = ?", (meter_id,)
        ).fetchone()
        if meter is None:
            flash("Choose a valid meter for this reading.", "error")
            return redirect(url_for("home", reading=1))
        pending = conn.execute(
            "SELECT id FROM meter_readings WHERE meter_id = ? AND validation_status = 'flagged' LIMIT 1",
            (meter_id,),
        ).fetchone()
        if pending:
            flash("Review this meter's pending reading before recording another one.", "error")
            return redirect(url_for("home", _anchor="readings"))
        reading_review_threshold = int(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'reading_review_threshold_kwh'"
        ).fetchone()[0])
        previous_reading = conn.execute(
            """
            SELECT reading_date, cumulative_kwh
            FROM meter_readings
            WHERE meter_id = ? AND reading_type = 'cumulative'
              AND validation_status IN ('baseline', 'valid')
            ORDER BY reading_date DESC, id DESC
            LIMIT 1
            """,
            (meter_id,),
        ).fetchone()
        latest_reading = conn.execute(
            "SELECT reading_date FROM meter_readings WHERE meter_id = ? AND reading_type = 'cumulative' ORDER BY reading_date DESC, id DESC LIMIT 1",
            (meter_id,),
        ).fetchone()

        if latest_reading and reading_date <= latest_reading["reading_date"]:
            flash(
                f"Choose a date after the latest saved reading ({latest_reading['reading_date']}).",
                "error",
            )
            return redirect(url_for("home", reading=1))

        if previous_reading is None:
            calculated_kwh    = None
            validation_status = "baseline"
            review_type       = ""
            validation_note   = "First cumulative reading establishes the meter baseline."
        elif cumulative_kwh < previous_reading["cumulative_kwh"]:
            calculated_kwh    = None
            validation_status = "flagged"
            review_type       = "meter_reset"
            validation_note   = "Cumulative value decreased; possible meter reset or rollover. Usage not calculated."
        else:
            calculated_kwh = int(
                np.diff(np.array([previous_reading["cumulative_kwh"], cumulative_kwh], dtype=np.int64))[0]
            )
            if calculated_kwh > reading_review_threshold:
                validation_status = "flagged"
                review_type       = "usage_spike"
                validation_note   = (
                    f"Calculated increase of {calculated_kwh:,} kWh exceeds review threshold "
                    f"of {reading_review_threshold:,} kWh. Awaiting review."
                )
            else:
                validation_status = "valid"
                review_type       = ""
                validation_note   = None

        recorded_by = session.get("username", "")
        conn.execute(
            """
            INSERT INTO meter_readings
                (consumer_id, meter_id, reading_date, consumption, reading_type, cumulative_kwh,
                 calculated_kwh, validation_status, review_type, validation_note, is_sample, recorded_by)
            VALUES (?, ?, ?, ?, 'cumulative', ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                meter["consumer_id"], meter_id, reading_date,
                calculated_kwh or 0, cumulative_kwh, calculated_kwh,
                validation_status, review_type, validation_note, recorded_by,
            ),
        )
        if validation_status in {"valid", "baseline"}:
            refresh_consumer_consumption(conn, meter["consumer_id"])

        # Auto-log alert if high usage
        if validation_status == "valid" and calculated_kwh:
            high_threshold = int(conn.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'high_usage_threshold_kwh'"
            ).fetchone()[0])
            consumer_name = conn.execute("SELECT name FROM consumers WHERE id = ?", (meter["consumer_id"],)).fetchone()["name"]
            if calculated_kwh >= high_threshold:
                conn.execute(
                    "INSERT INTO alert_log (alert_type, consumer_id, message) VALUES ('high_usage', ?, ?)",
                    (meter["consumer_id"], f"{consumer_name} recorded {calculated_kwh:,} kWh — exceeds high-usage threshold."),
                )

    if validation_status == "baseline":
        flash("Baseline saved. Usage will be calculated from the next reading.", "success")
    elif validation_status == "flagged":
        flash("Reading saved for review. It will not affect totals until approved.", "warning")
    else:
        flash(f"Reading saved. Calculated usage: {calculated_kwh:,} kWh.", "success")
    return redirect(url_for("home"))


@app.route("/readings/import", methods=["POST"])
def import_readings_csv():
    uploaded = request.files.get("readings_csv")
    if uploaded is None or not uploaded.filename or not uploaded.filename.lower().endswith(".csv"):
        flash("Choose a .csv file to import readings.", "error")
        return redirect(url_for("home", _anchor="readings"))
    raw_data = uploaded.read(2_000_001)
    if len(raw_data) > 2_000_000:
        flash("The CSV is larger than 2 MB. Split it into smaller files.", "error")
        return redirect(url_for("home", _anchor="readings"))
    try:
        text = raw_data.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("The CSV must use UTF-8 encoding.", "error")
        return redirect(url_for("home", _anchor="readings"))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash("The CSV is empty or missing column headings.", "error")
        return redirect(url_for("home", _anchor="readings"))

    def normalize_header(v):
        return "_".join(v.strip().lower().replace("(kwh)", "").replace("-", " ").split())

    available_headers = {normalize_header(h): h for h in reader.fieldnames if h}
    aliases = {
        "meter_code": ("meter_code", "meter_number", "meter"),
        "reading_date": ("reading_date", "date"),
        "cumulative_kwh": ("cumulative_kwh", "cumulative_reading", "reading"),
    }
    selected_headers = {}
    for required, candidates in aliases.items():
        selected_headers[required] = next(
            (available_headers[n] for n in candidates if n in available_headers), None
        )
    if any(h is None for h in selected_headers.values()):
        flash("CSV must include meter_code, reading_date, and cumulative_kwh. Download the template.", "error")
        return redirect(url_for("home", _anchor="readings"))

    rows = list(reader)
    if not rows:
        flash("The CSV has headings but no data rows.", "error")
        return redirect(url_for("home", _anchor="readings"))
    if len(rows) > 1000:
        flash("Import up to 1,000 rows at a time.", "error")
        return redirect(url_for("home", _anchor="readings"))

    accepted = flagged = 0
    errors = []
    refreshed_consumers = set()
    with get_db_connection() as conn:
        threshold  = int(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'reading_review_threshold_kwh'"
        ).fetchone()[0])
        meter_rows    = conn.execute("SELECT meter_id, consumer_id, meter_code FROM meters").fetchall()
        meters_by_code = {r["meter_code"].strip().casefold(): r for r in meter_rows}
        states = {}
        for line_number, csv_row in enumerate(rows, start=2):
            meter_code     = (csv_row.get(selected_headers["meter_code"]) or "").strip()
            reading_date   = (csv_row.get(selected_headers["reading_date"]) or "").strip()
            cumulative_text = (csv_row.get(selected_headers["cumulative_kwh"]) or "").strip()
            row_error = None
            parsed_date = cumulative_kwh = None
            try:
                parsed_date = datetime.strptime(reading_date, "%Y-%m-%d").date()
                cumulative_kwh = int(cumulative_text)
                if cumulative_kwh < 0 or cumulative_kwh > 9_000_000_000_000_000:
                    raise ValueError
            except ValueError:
                row_error = "use a real YYYY-MM-DD date and a whole-number cumulative kWh value"
            meter = meters_by_code.get(meter_code.casefold()) if meter_code else None
            if row_error is None and meter is None:
                row_error = f"meter code '{meter_code or '(blank)'}' not found"
            if row_error is None and parsed_date > date.today():
                row_error = "reading date cannot be in the future"
            state = None
            if row_error is None:
                mid = meter["meter_id"]
                if mid not in states:
                    latest   = conn.execute(
                        "SELECT reading_date FROM meter_readings WHERE meter_id = ? AND reading_type = 'cumulative' ORDER BY reading_date DESC, id DESC LIMIT 1",
                        (mid,),
                    ).fetchone()
                    previous = conn.execute(
                        "SELECT reading_date, cumulative_kwh FROM meter_readings WHERE meter_id = ? AND reading_type = 'cumulative' AND validation_status IN ('baseline', 'valid') ORDER BY reading_date DESC, id DESC LIMIT 1",
                        (mid,),
                    ).fetchone()
                    pending  = conn.execute(
                        "SELECT 1 FROM meter_readings WHERE meter_id = ? AND validation_status = 'flagged' LIMIT 1",
                        (mid,),
                    ).fetchone()
                    states[mid] = {
                        "latest_date": latest["reading_date"] if latest else None,
                        "previous": previous,
                        "blocked": pending is not None,
                    }
                state = states[mid]
                if state["blocked"]:
                    row_error = "pending review exists; resolve it first"
                elif state["latest_date"] and reading_date <= state["latest_date"]:
                    row_error = f"date must be after latest saved reading ({state['latest_date']})"

            if row_error:
                errors.append(f"Row {line_number}: {row_error}.")
                continue

            previous = state["previous"]
            if previous is None:
                calculated_kwh = None
                status, review_type = "baseline", ""
                note = "First cumulative reading establishes the meter baseline."
            elif cumulative_kwh < previous["cumulative_kwh"]:
                calculated_kwh = None
                status, review_type = "flagged", "meter_reset"
                note = "Cumulative value decreased; possible meter reset or rollover."
            else:
                calculated_kwh = cumulative_kwh - previous["cumulative_kwh"]
                if calculated_kwh > threshold:
                    status, review_type = "flagged", "usage_spike"
                    note = f"Calculated increase of {calculated_kwh:,} kWh exceeds threshold of {threshold:,} kWh."
                else:
                    status, review_type, note = "valid", "", None

            conn.execute(
                """INSERT INTO meter_readings
                   (consumer_id, meter_id, reading_date, consumption, reading_type, cumulative_kwh,
                    calculated_kwh, validation_status, review_type, validation_note, is_sample, recorded_by)
                   VALUES (?, ?, ?, ?, 'cumulative', ?, ?, ?, ?, ?, 0, ?)""",
                (meter["consumer_id"], meter["meter_id"], reading_date, calculated_kwh or 0,
                 cumulative_kwh, calculated_kwh, status, review_type, note, session.get("username", "")),
            )
            accepted += 1
            state["latest_date"] = reading_date
            if status in {"baseline", "valid"}:
                state["previous"] = {"reading_date": reading_date, "cumulative_kwh": cumulative_kwh}
                refreshed_consumers.add(meter["consumer_id"])
            else:
                state["blocked"] = True
                flagged += 1

        for cid in refreshed_consumers:
            refresh_consumer_consumption(conn, cid)

    if accepted:
        flash(f"Imported {accepted} reading(s); {flagged} flagged for review.", "success")
    if errors:
        detail = " ".join(errors[:8])
        if len(errors) > 8:
            detail += f" …and {len(errors) - 8} more."
        flash(f"Skipped {len(errors)} invalid row(s): {detail}", "error")
    return redirect(url_for("home", _anchor="readings"))


@app.route("/readings/<int:reading_id>/review", methods=["POST"])
def review_reading(reading_id):
    decision = request.form.get("decision", "").strip()
    if decision not in {"approve", "reject"}:
        flash("Choose approve or reject.", "error")
        return redirect(url_for("home", _anchor="readings"))
    with get_db_connection() as conn:
        reading = conn.execute(
            "SELECT id, consumer_id, meter_id, review_type, calculated_kwh, validation_note FROM meter_readings WHERE id = ? AND validation_status = 'flagged'",
            (reading_id,),
        ).fetchone()
        if reading is None:
            flash("That reading is no longer awaiting review.", "error")
            return redirect(url_for("home", _anchor="readings"))
        if decision == "approve" and (reading["review_type"] == "meter_reset" or reading["calculated_kwh"] is None):
            conn.execute(
                "UPDATE meter_readings SET validation_status = 'baseline', review_type = '', calculated_kwh = NULL, consumption = 0, validation_note = 'Approved as new baseline after review.' WHERE id = ?",
                (reading_id,),
            )
            refresh_consumer_consumption(conn, reading["consumer_id"])
            flash("Reading approved as a new baseline. No usage inferred across reset.", "success")
        elif decision == "approve":
            conn.execute(
                "UPDATE meter_readings SET validation_status = 'valid', review_type = '', validation_note = ? WHERE id = ?",
                (f"Approved after review. Original: {reading['validation_note'] or 'unusual increase'}", reading_id),
            )
            refresh_consumer_consumption(conn, reading["consumer_id"])
            flash("Reading approved and included in usage totals.", "success")
        else:
            conn.execute(
                "UPDATE meter_readings SET validation_status = 'rejected', review_type = '', validation_note = ? WHERE id = ?",
                (f"Rejected during manual review. Original: {reading['validation_note'] or 'flagged reading'}", reading_id),
            )
            refresh_consumer_consumption(conn, reading["consumer_id"])
            flash("Reading rejected and excluded from totals.", "success")
    return redirect(url_for("home", _anchor="readings"))


# ---------------------------------------------------------------------------
# Tariffs
# ---------------------------------------------------------------------------

@app.route("/tariffs", methods=["POST"])
def add_tariff():
    name          = request.form.get("name", "").strip()
    consumer_type = request.form.get("consumer_type", "Residential").strip()
    description   = request.form.get("description", "").strip()
    try:
        rate_per_kwh  = float(request.form.get("rate_per_kwh", "0"))
        fixed_charge  = float(request.form.get("fixed_charge", "0"))
        tax_percent   = float(request.form.get("tax_percent", "0"))
    except ValueError:
        flash("Enter valid numeric values for the tariff rates.", "error")
        return redirect(url_for("home", _anchor="billing"))
    if rate_per_kwh < 0 or fixed_charge < 0 or not 0 <= tax_percent <= 100:
        flash("Tariff rates and fixed charges cannot be negative; tax must be between 0 and 100%.", "error")
        return redirect(url_for("home", _anchor="billing"))
    if not name:
        flash("Enter a tariff name.", "error")
        return redirect(url_for("home", _anchor="billing"))
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO tariffs (name, consumer_type, rate_per_kwh, fixed_charge, tax_percent, description) VALUES (?, ?, ?, ?, ?, ?)",
                (name, consumer_type, rate_per_kwh, fixed_charge, tax_percent, description or None),
            )
    except sqlite3.IntegrityError:
        flash("A tariff with that name already exists.", "error")
        return redirect(url_for("home", _anchor="billing"))
    flash(f"Tariff '{name}' created.", "success")
    return redirect(url_for("home", _anchor="billing"))


@app.route("/tariffs/<int:tariff_id>/delete", methods=["POST"])
def delete_tariff(tariff_id):
    with get_db_connection() as conn:
        conn.execute("UPDATE consumers SET tariff_id = NULL WHERE tariff_id = ?", (tariff_id,))
        conn.execute("DELETE FROM tariffs WHERE id = ?", (tariff_id,))
    flash("Tariff deleted.", "success")
    return redirect(url_for("home", _anchor="billing"))


@app.route("/consumers/<int:consumer_id>/tariff", methods=["POST"])
def assign_consumer_tariff(consumer_id):
    tariff_value = request.form.get("tariff_id", "").strip()
    try:
        tariff_id = int(tariff_value) if tariff_value else None
    except ValueError:
        flash("Choose a valid tariff.", "error")
        return redirect(url_for("home", _anchor="billing"))
    with get_db_connection() as conn:
        if tariff_id is not None and not conn.execute(
            "SELECT 1 FROM tariffs WHERE id = ?", (tariff_id,)
        ).fetchone():
            flash("That tariff could not be found.", "error")
            return redirect(url_for("home", _anchor="billing"))
        updated = conn.execute(
            "UPDATE consumers SET tariff_id = ? WHERE id = ?", (tariff_id, consumer_id)
        )
    if updated.rowcount:
        flash("Consumer tariff updated.", "success")
    else:
        flash("That consumer could not be found.", "error")
    return redirect(url_for("home", _anchor="billing"))


# ---------------------------------------------------------------------------
# Alert log
# ---------------------------------------------------------------------------

@app.route("/alerts/mark-read", methods=["POST"])
def mark_alerts_read():
    with get_db_connection() as conn:
        conn.execute("UPDATE alert_log SET is_read = 1")
    return redirect(url_for("home", _anchor="alerts"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings/threshold", methods=["POST"])
def update_threshold():
    threshold_value = request.form.get("threshold_kwh", "").strip()
    try:
        threshold = int(threshold_value)
        if threshold < 1:
            raise ValueError
    except ValueError:
        flash("Enter a whole-number threshold of at least 1 kWh.", "error")
        return redirect(url_for("home", _anchor="alerts"))
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE settings SET setting_value = ? WHERE setting_key = 'high_usage_threshold_kwh'",
            (str(threshold),),
        )
    flash(f"High-usage threshold updated to {threshold:,} kWh.", "success")
    return redirect(url_for("home", _anchor="alerts"))


@app.route("/settings/reading-review-threshold", methods=["POST"])
def update_reading_review_threshold():
    threshold_value = request.form.get("reading_review_threshold_kwh", "").strip()
    try:
        threshold = int(threshold_value)
        if threshold < 1:
            raise ValueError
    except ValueError:
        flash("Enter a whole-number review threshold of at least 1 kWh.", "error")
        return redirect(url_for("home", _anchor="readings"))
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE settings SET setting_value = ? WHERE setting_key = 'reading_review_threshold_kwh'",
            (str(threshold),),
        )
    flash(f"Reading review threshold updated to {threshold:,} kWh.", "success")
    return redirect(url_for("home", _anchor="readings"))


@app.route("/settings/general", methods=["POST"])
def update_general_settings():
    org_name       = request.form.get("org_name", "").strip()
    currency       = request.form.get("currency_symbol", "₹").strip()
    co2_raw        = request.form.get("co2_kg_per_kwh", "").strip()
    try:
        co2_factor = float(co2_raw)
        if co2_factor < 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid CO₂ emission factor (kg per kWh).", "error")
        return redirect(url_for("home", _anchor="settings"))
    with get_db_connection() as conn:
        for key, val in (
            ("org_name", org_name or "ElectroTrack Systems"),
            ("currency_symbol", currency or "₹"),
            ("co2_kg_per_kwh", str(co2_factor)),
        ):
            conn.execute("UPDATE settings SET setting_value = ? WHERE setting_key = ?", (val, key))
    flash("General settings saved.", "success")
    return redirect(url_for("home", _anchor="settings"))


# ---------------------------------------------------------------------------
# CSV downloads
# ---------------------------------------------------------------------------

def csv_download(filename, headers, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(
        "\ufeff" + output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/reports/consumers.csv")
def download_consumers_report():
    with get_db_connection() as conn:
        threshold = int(conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'high_usage_threshold_kwh'"
        ).fetchone()[0])
        consumers = conn.execute(
            """SELECT consumers.id, consumers.name,
                      COALESCE((SELECT GROUP_CONCAT(meter_code, ', ') FROM meters WHERE consumer_id = consumers.id), '—') AS meter_number,
                      consumers.consumption, consumers.consumer_type, consumers.region,
                      consumers.phone, consumers.email
               FROM consumers ORDER BY consumers.id"""
        ).fetchall()
    rows = [
        (
            f"ET{c['id'] + 1000:04d}",
            c["name"], c["meter_number"], c["consumer_type"], c["region"],
            c["consumption"], "High Usage" if c["consumption"] >= threshold else "Normal",
            c["phone"] or "", c["email"] or "",
        )
        for c in consumers
    ]
    return csv_download(
        "electrotrack-consumers.csv",
        ["Consumer ID", "Name", "Meter Number", "Consumer Type", "Region",
         "Current Consumption (kWh)", "Status", "Phone", "Email"],
        rows,
    )


@app.route("/reports/readings.csv")
def download_readings_report():
    with get_db_connection() as conn:
        readings = conn.execute(
            """
            SELECT meter_readings.reading_date, consumers.id AS consumer_id, consumers.name,
                   COALESCE(meters.meter_code, consumers.meter_number) AS meter_number,
                   meter_readings.reading_type,
                   meter_readings.cumulative_kwh, meter_readings.calculated_kwh,
                   meter_readings.consumption, meter_readings.validation_status,
                   meter_readings.validation_note, meter_readings.is_sample,
                   meter_readings.recorded_by
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            ORDER BY meter_readings.reading_date DESC, meter_readings.id DESC
            """
        ).fetchall()
    rows = [
        (
            r["reading_date"], f"ET{r['consumer_id'] + 1000:04d}", r["name"], r["meter_number"],
            "Cumulative" if r["reading_type"] == "cumulative" else "Legacy",
            r["cumulative_kwh"] if r["reading_type"] == "cumulative" else "",
            r["calculated_kwh"] if r["reading_type"] == "cumulative" else r["consumption"],
            r["validation_status"] if r["reading_type"] == "cumulative" else "legacy usage",
            r["validation_note"] or "",
            "Sample" if r["is_sample"] else "Recorded",
            r["recorded_by"] or "",
        )
        for r in readings
    ]
    return csv_download(
        "electrotrack-meter-readings.csv",
        ["Reading Date", "Consumer ID", "Name", "Meter Number", "Record Type",
         "Cumulative (kWh)", "Calculated (kWh)", "Validation Status", "Validation Notes",
         "Data Source", "Recorded By"],
        rows,
    )


@app.route("/reports/monthly-consumption.csv")
def download_monthly_consumption_report():
    with get_db_connection() as conn:
        monthly_rows = conn.execute(
            f"""
            SELECT substr(meter_readings.reading_date, 1, 7) AS month,
                   consumers.id, consumers.name, COALESCE(meters.meter_code, consumers.meter_number) AS meter_number,
                   SUM({READING_USAGE_SQL}) AS monthly_consumption
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            GROUP BY month, consumers.id, consumers.name, consumers.meter_number
            HAVING monthly_consumption > 0
            ORDER BY month DESC, consumers.name COLLATE NOCASE
            """
        ).fetchall()
    rows = [
        (row["month"], f"ET{row['id'] + 1000:04d}", row["name"], row["meter_number"], row["monthly_consumption"])
        for row in monthly_rows
    ]
    return csv_download(
        "electrotrack-monthly-consumption.csv",
        ["Month", "Consumer ID", "Name", "Meter Number", "Consumption (kWh)"],
        rows,
    )


@app.route("/reports/validation.csv")
def download_validation_report():
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT meter_readings.reading_date, consumers.id AS consumer_id, consumers.name,
                   COALESCE(meters.meter_code, consumers.meter_number) AS meter_number,
                   meter_readings.cumulative_kwh, meter_readings.calculated_kwh,
                   meter_readings.validation_status, meter_readings.validation_note,
                   meter_readings.is_sample
            FROM meter_readings
            JOIN consumers ON consumers.id = meter_readings.consumer_id
            LEFT JOIN meters ON meters.meter_id = meter_readings.meter_id
            WHERE meter_readings.reading_type = 'cumulative'
            ORDER BY meter_number, meter_readings.reading_date, meter_readings.id
            """
        ).fetchall()
    report_rows = [
        (r["reading_date"], f"ET{r['consumer_id'] + 1000:04d}", r["name"], r["meter_number"],
         r["cumulative_kwh"], r["calculated_kwh"] if r["calculated_kwh"] is not None else "",
         r["validation_status"], r["validation_note"] or "", "Sample" if r["is_sample"] else "Recorded")
        for r in rows
    ]
    return csv_download(
        "electrotrack-reading-validation.csv",
        ["Reading Date", "Consumer ID", "Name", "Meter Number", "Cumulative Reading (kWh)",
         "Calculated Usage (kWh)", "Validation Status", "Validation Notes", "Data Source"],
        report_rows,
    )


@app.route("/reports/billing.csv")
def download_billing_report():
    today_date = date.today()
    prev_month_start = (today_date.replace(day=1) - timedelta(days=1)).replace(day=1)
    prev_month_str   = prev_month_start.strftime("%Y-%m")
    with get_db_connection() as conn:
        currency = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'currency_symbol'"
        ).fetchone()[0]
        billing_rows = conn.execute(
            f"""
            SELECT consumers.id, consumers.name,
                   COALESCE((SELECT GROUP_CONCAT(meters.meter_code, ', ')
                             FROM meters WHERE meters.consumer_id = consumers.id), '—') AS meter_number,
                   consumers.consumer_type, consumers.tariff_id,
                   COALESCE((SELECT SUM({READING_USAGE_SQL}) FROM meter_readings
                             WHERE meter_readings.consumer_id = consumers.id
                               AND substr(meter_readings.reading_date, 1, 7) = ?), 0) AS monthly_kwh
            FROM consumers
            ORDER BY monthly_kwh DESC
            """,
            (prev_month_str,),
        ).fetchall()
        tariffs = {t["id"]: t for t in conn.execute("SELECT * FROM tariffs").fetchall()}

    rows = []
    for b in billing_rows:
        kwh = b["monthly_kwh"]
        tariff = tariffs.get(b["tariff_id"])
        if tariff and kwh > 0:
            energy_cost = kwh * tariff["rate_per_kwh"]
            fixed       = tariff["fixed_charge"]
            subtotal    = energy_cost + fixed
            tax         = subtotal * tariff["tax_percent"] / 100
            total       = subtotal + tax
        else:
            energy_cost = fixed = subtotal = tax = total = 0
        rows.append((
            prev_month_str, f"ET{b['id'] + 1000:04d}", b["name"], b["meter_number"],
            b["consumer_type"], kwh,
            tariff["name"] if tariff else "No tariff",
            round(energy_cost, 2), round(fixed, 2), round(tax, 2), round(total, 2),
        ))
    return csv_download(
        f"electrotrack-billing-{prev_month_str}.csv",
        ["Month", "Consumer ID", "Name", "Meter Number", "Type", f"Usage (kWh)",
         "Tariff", f"Energy Charge ({currency})", f"Fixed Charge ({currency})",
         f"Tax ({currency})", f"Total Bill ({currency})"],
        rows,
    )


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", "5001")))
