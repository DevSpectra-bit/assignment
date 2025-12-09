# app.py — cleaned version (maintenance logic removed, shared DB helpers, kept all features)
from flask import Flask, render_template, request, redirect, url_for, session, flash, current_app
from datetime import datetime, date
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import sqlite3
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
import json
from cryptography.fernet import Fernet
from contextlib import contextmanager
from typing import Iterator
from math import isfinite
from flask import jsonify

# Load .env if available
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# try loading config.py if present
try:
    import config  # noqa: F401
    app.config.from_object("config.Config")
except ModuleNotFoundError:
    # ensure FEATURE_FLAGS exists
    app.config["FEATURE_FLAGS"] = {}

# persisted flags file
FEATURE_FLAGS_FILE = "feature_flags.json"

def load_feature_flags():
    """Load feature flags from JSON file and merge into app config FEATURE_FLAGS mapping."""
    try:
        if os.path.exists(FEATURE_FLAGS_FILE):
            with open(FEATURE_FLAGS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    app.config["FEATURE_FLAGS"].update(data)
    except Exception:
        pass

def save_feature_flags():
    """Persist current feature flags mapping to disk."""
    try:
        with open(FEATURE_FLAGS_FILE, "w") as f:
            json.dump(app.config.get("FEATURE_FLAGS", {}), f, indent=2)
    except Exception:
        pass

# load persisted flags (if any)
load_feature_flags()

# helper to read a feature flag safely
def feature_enabled(key: str, default: bool = True) -> bool:
    return app.config.get("FEATURE_FLAGS", {}).get(key, default)

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith("postgres"))

# --- DB helpers ---
def get_connection():
    """Return a DB connection. Postgres -> psycopg2 (RealDictCursor), else sqlite3."""
    if IS_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect("assignments.db")
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db_cursor() -> Iterator:
    """
    Context manager that yields a cursor and ensures commit/rollback and close.
    Use for both read and write operations. Keeps behavior consistent with previous code.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        # commit where needed; harmless for pure selects on most DBs
        try:
            conn.commit()
        except Exception:
            # some read-only contexts or special cursors might not support commit
            pass
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

def row_val(row, key):
    """Get value from either dict-like (Postgres RealDict) or sqlite3.Row.
    Returns None if missing."""
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        try:
            return getattr(row, key, None)
        except Exception:
            return None

# --- Grade Encryption Helpers ---

def load_grade_key():
    """Load or create encryption key for grades."""
    key_file = "secret.key"
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(key_file, "wb") as f:
        f.write(key)
    return key

GRADE_CIPHER = Fernet(load_grade_key())

def encrypt_grade(value: float) -> bytes:
    """Encrypt a float → bytes."""
    return GRADE_CIPHER.encrypt(str(value).encode())

def decrypt_grade(token: bytes) -> float:
    """Decrypt bytes → float."""
    return float(GRADE_CIPHER.decrypt(token).decode())

# safe decrypt helper (handles sqlite memoryview, bytes, str, None)
def decrypt_grade_safe(token):
    """
    Accepts token that may be bytes, memoryview, or str (base64) and returns float.
    Returns None on failure or if token is falsy.
    """
    if not token:
        return None

    try:
        if isinstance(token, memoryview):
            token = bytes(token)
    except NameError:
        pass

    if isinstance(token, str):
        token_bytes = token.encode()
    else:
        token_bytes = token

    try:
        plain = GRADE_CIPHER.decrypt(token_bytes)
        return float(plain.decode())
    except Exception:
        return None

# --- DB setup ---
def init_db():
    with db_cursor() as c:
        # users table (base columns)
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL
                );
            """)
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_seen_tutorial BOOLEAN DEFAULT FALSE;")
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS dark_mode BOOLEAN DEFAULT FALSE;")
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_update TEXT DEFAULT '';")
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL
                );
            """)
            try:
                c.execute("ALTER TABLE users ADD COLUMN has_seen_tutorial INTEGER DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN dark_mode INTEGER DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN last_seen_update TEXT DEFAULT '';")
            except sqlite3.OperationalError:
                pass

        # assignments table
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS assignments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    cl TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    notes TEXT,
                    submitted BOOLEAN DEFAULT FALSE
                );
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT NOT NULL,
                    cl TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    notes TEXT,
                    submitted INTEGER DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            """)

        # class_links (legacy per-user quick-links)
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS class_links (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    class_name TEXT NOT NULL,
                    link TEXT NOT NULL
                );
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS class_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    class_name TEXT NOT NULL,
                    link TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            """)

        # classes table (used by grade tracker)
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS classes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    class_name TEXT NOT NULL,
                    link TEXT
                );
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS classes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    class_name TEXT NOT NULL,
                    link TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            """)

        # grades table
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS grades (
                    id SERIAL PRIMARY KEY,
                    assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    grade BYTEA NOT NULL,
                    out_of BYTEA NOT NULL,
                    proficiency INTEGER DEFAULT 0
                );
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS grades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER,
                    user_id INTEGER,
                    grade BLOB NOT NULL,
                    out_of BLOB NOT NULL,
                    proficiency INTEGER DEFAULT 0,
                    FOREIGN KEY(assignment_id) REFERENCES assignments(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            """)

        # goals table (NEW)
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    class_id INTEGER REFERENCES classes(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    goal_type TEXT NOT NULL,      -- "grade", "completion", etc.
                    target_value REAL,
                    deadline TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    class_id INTEGER,
                    title TEXT NOT NULL,
                    goal_type TEXT NOT NULL,
                    target_value REAL,
                    deadline TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(class_id) REFERENCES classes(id)
                );
            """)
        # Feedback form
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    message TEXT,
                    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    message TEXT,
                    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

init_db()


# --- Context processors & helpers ---
@app.context_processor
def inject_dark_mode():
    """Make current user's dark mode preference available to templates as `dark_mode`."""
    dark = False
    if session.get("user_id"):
        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute("SELECT dark_mode FROM users WHERE id = %s", (session["user_id"],))
            else:
                c.execute("SELECT dark_mode FROM users WHERE id = ?", (session["user_id"],))
            r = c.fetchone()
            val = row_val(r, "dark_mode")
            if val is not None:
                try:
                    dark = bool(int(val)) if str(val) in ("0", "1") else bool(val)
                except Exception:
                    dark = bool(val)
    return {"dark_mode": dark}

# --- UPDATES helpers ---
UPDATES_VERSION = "2025.12.09"  # Change this string whenever updates.html changes

def should_show_updates(user_id):
    """Return True if user should see updates.html (hasn't seen current version)."""
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT last_seen_update FROM users WHERE id = %s", (user_id,))
        else:
            c.execute("SELECT last_seen_update FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        last_seen = row_val(row, "last_seen_update") or ""
        return last_seen != UPDATES_VERSION

def mark_updates_seen(user_id):
    """Set user's last_seen_update to current version."""
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("UPDATE users SET last_seen_update = %s WHERE id = %s", (UPDATES_VERSION, user_id))
        else:
            c.execute("UPDATE users SET last_seen_update = ? WHERE id = ?", (UPDATES_VERSION, user_id))

# --- Other Helpers ---
def get_user_goals(user_id):
    """Return list of goal rows for user_id (raw DB rows)."""
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT * FROM goals WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        else:
            c.execute("SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return c.fetchall()

def compute_class_average_for_user(class_id, user_id, cursor):
    """
    Compute current class average for a user.
    Returns float (0..100) or None if no graded items.
    Expects an open cursor; caller should handle Postgres/SQLite placeholders.
    """
    # Strategy:
    # - Find latest grade per assignment for this user (same as grade_tracker_class does)
    # - Compute average of percents
    if IS_POSTGRES:
        # fetch assignment ids for the class (case-insensitive compare)
        cursor.execute("""
            SELECT id
            FROM assignments
            WHERE user_id = %s AND LOWER(TRIM(cl)) = LOWER(TRIM(%s))
        """, (user_id, class_id))
    else:
        cursor.execute("""
            SELECT id
            FROM assignments
            WHERE user_id = ? AND TRIM(cl) = ?
        """, (user_id, class_id))
    assignment_rows = cursor.fetchall()
    assignment_ids = [row_val(r, "id") for r in assignment_rows]

    if not assignment_ids:
        return None

    percentages = []
    # For performance, fetch latest grade rows in a single query using IN (...)
    if IS_POSTGRES:
        placeholders = ",".join(["%s"] * len(assignment_ids))
        sql = f"""
            SELECT g.assignment_id, g.grade, g.out_of
            FROM grades g
            JOIN (
                SELECT assignment_id, MAX(id) AS max_id
                FROM grades
                WHERE user_id = %s AND assignment_id IN ({placeholders})
                GROUP BY assignment_id
            ) lg ON lg.max_id = g.id
            WHERE g.user_id = %s
        """
        params = [user_id] + assignment_ids + [user_id]
        cursor.execute(sql, tuple(params))
    else:
        placeholders = ",".join(["?"] * len(assignment_ids))
        sql = f"""
            SELECT g.assignment_id, g.grade, g.out_of
            FROM grades g
            JOIN (
                SELECT assignment_id, MAX(id) AS max_id
                FROM grades
                WHERE user_id = ? AND assignment_id IN ({placeholders})
                GROUP BY assignment_id
            ) lg ON lg.max_id = g.id
            WHERE g.user_id = ?
        """
        params = [user_id] + assignment_ids + [user_id]
        cursor.execute(sql, params)

    grade_rows = cursor.fetchall()
    for gr in grade_rows:
        enc_grade = row_val(gr, "grade")
        enc_out = row_val(gr, "out_of")
        g_val = decrypt_grade_safe(enc_grade)
        o_val = decrypt_grade_safe(enc_out)
        if g_val is None or o_val in (None, 0):
            continue
        try:
            pct = (float(g_val) / float(o_val)) * 100.0
            percentages.append(pct)
        except Exception:
            continue

    if not percentages:
        return None
    return round(sum(percentages) / len(percentages), 2)

def get_class_name_from_links(class_id, user_id):
    if not class_id:
        return None
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT class_name FROM class_links WHERE id = %s AND user_id = %s",
                      (class_id, user_id))
        else:
            c.execute("SELECT class_name FROM class_links WHERE id = ? AND user_id = ?",
                      (class_id, user_id))
        row = c.fetchone()
        return row_val(row, "class_name") if row else None


def compute_goal_progress(goal_row, user_id):
    """
    Given a DB row for a goal, compute progress.
    """

    # Normalize sqlite vs dict row
    try:
        goal = dict(goal_row)
    except Exception:
        goal = {
            "id": row_val(goal_row, "id"),
            "user_id": row_val(goal_row, "user_id"),
            "class_id": row_val(goal_row, "class_id"),
            "title": row_val(goal_row, "title"),
            "goal_type": row_val(goal_row, "goal_type"),
            "target_value": row_val(goal_row, "target_value"),
            "deadline": row_val(goal_row, "deadline"),
            "created_at": row_val(goal_row, "created_at"),
        }

    gtype = (goal.get("goal_type") or "").lower()
    target = goal.get("target_value")

    with db_cursor() as c:

        # ---------------------------------------------------
        # 1. GRADE GOAL (unchanged)
        # ---------------------------------------------------
        if gtype == "grade":
            class_id = goal.get("class_id")
            if not class_id:
                return {
                    "type": "grade",
                    "progress": None,
                    "target": target,
                    "percent_of_target": None
                }

            avg = compute_class_average_for_user(class_id, user_id, c)
            if avg is None:
                return {
                    "type": "grade",
                    "progress": None,
                    "target": target,
                    "percent_of_target": None
                }

            percent = None
            try:
                if target:
                    percent = round((avg / float(target)) * 100.0, 2)
            except:
                percent = None

            return {
                "type": "grade",
                "progress": avg,
                "target": target,
                "percent_of_target": percent
            }


        # ---------------------------------------------------
        # 2. COMPLETION GOAL (fixed version)
        # ---------------------------------------------------
        elif gtype == "completion":

            class_id = goal.get("class_id")
            class_name = get_class_name_from_links(class_id, user_id)

            try:
                target_num = float(target or 0)
            except:
                target_num = 0

            # Count completed submissions
            if class_name:
                # Class-specific
                if IS_POSTGRES:
                    c.execute("""
                        SELECT COUNT(*) AS done
                        FROM assignments
                        WHERE user_id = %s
                          AND LOWER(TRIM(cl)) = LOWER(TRIM(%s))
                          AND submitted = TRUE
                    """, (user_id, class_name))
                else:
                    c.execute("""
                        SELECT COUNT(*) AS done
                        FROM assignments
                        WHERE user_id = ?
                          AND TRIM(cl) = TRIM(?)
                          AND submitted = 1
                    """, (user_id, class_name))
            else:
                # All classes
                if IS_POSTGRES:
                    c.execute("""
                        SELECT COUNT(*) AS done
                        FROM assignments
                        WHERE user_id = %s
                          AND submitted = TRUE
                    """, (user_id,))
                else:
                    c.execute("""
                        SELECT COUNT(*) AS done
                        FROM assignments
                        WHERE user_id = ?
                          AND submitted = 1
                    """, (user_id,))

            done = row_val(c.fetchone(), "done") or 0

            # Calculate % of target
            if target_num > 0:
                percent = round((done / target_num) * 100.0, 2)
                if percent > 100:
                    percent = 100
            else:
                percent = 0

            return {
                "type": "completion",
                "completed": done,
                "target": target_num,
                "percent_of_target": percent
            }


        # ---------------------------------------------------
        # 3. Unknown type
        # ---------------------------------------------------
        else:
            return {"type": gtype, "progress": None, "target": target}



# --- AUTH ---
@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))

    if not feature_enabled("register", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        if not username or not password:
            flash("Username and password required.")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        with db_cursor() as c:
            try:
                if IS_POSTGRES:
                    c.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, hashed_pw))
                else:
                    c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
                flash("✅ Account created — please log in.")
                return redirect(url_for("login"))
            except Exception:
                flash("Username already exists or error creating account.")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if not feature_enabled("login", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
            else:
                c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,))
            user = c.fetchone()
            # print debug row as dict (works for sqlite3.Row and RealDictRow)
            print("DEBUG USER ROW:", dict(user) if user is not None else None)

        if user:
            stored_pw = row_val(user, "password")
            if stored_pw and check_password_hash(stored_pw, password):
                session["user_id"] = row_val(user, "id")
                session["username"] = row_val(user, "username")
                session["is_admin"] = row_val(user, "is_admin") or 0
                has_seen_tutorial = row_val(user, "has_seen_tutorial") or False
                try:
                    has_seen_tutorial = bool(int(has_seen_tutorial)) if str(has_seen_tutorial) in ("0", "1") else bool(has_seen_tutorial)
                except Exception:
                    has_seen_tutorial = bool(has_seen_tutorial)

                if not has_seen_tutorial:
                    return redirect(url_for("tutorial"))
                else:
                    return redirect(url_for("index"))

        flash("❌ Invalid username or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("👋 Logged out.")
    return redirect(url_for("login"))

# --- ASSIGNMENTS ---
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    print("Session:", dict(session))

    # Show updates.html if needed
    show_updates = should_show_updates(session["user_id"])
    if session.get("seen_updates_once"):
        show_updates = False

    if show_updates:
        mark_updates_seen(session["user_id"])
        session["seen_updates_once"] = True
        return render_template("updates.html")

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT * FROM assignments WHERE user_id = %s ORDER BY submitted ASC, due_date ASC", (session["user_id"],))
        else:
            c.execute("SELECT * FROM assignments WHERE user_id = ? ORDER BY submitted ASC, due_date ASC", (session["user_id"],))
        rows = c.fetchall()

    today = datetime.now().date()
    annotated = []
    for r in rows:
        try:
            row = dict(r)
        except Exception:
            row = r
        try:
            due_date_val = row.get("due_date")
            if isinstance(due_date_val, str):
                due_date = datetime.strptime(due_date_val.split(" ")[0], "%Y-%m-%d").date()
            else:
                due_date = due_date_val
        except Exception:
            continue
        days_left = (due_date - today).days

        submitted_val = row.get("submitted", False)
        try:
            submitted_bool = bool(int(submitted_val)) if str(submitted_val) in ("0", "1") else bool(submitted_val)
        except Exception:
            submitted_bool = bool(submitted_val)

        annotated.append({
            **row,
            "is_past_due": days_left < 0,
            "is_due_today": days_left == 0,
            "is_due_tomorrow": days_left == 1,
            "submitted": submitted_bool
        })

    return render_template("index.html", assignments=annotated)

@app.route("/add", methods=["POST"])
def add():
    if "user_id" not in session:
        return redirect(url_for("login"))

    title = request.form.get("title", "").strip()
    cl = request.form.get("class", "").strip()
    due_date = request.form.get("due_date", "").strip()
    notes = request.form.get("notes", "").strip()

    if not title or not cl or not due_date:
        flash("Title, class and due date are required.")
        return redirect(url_for("index"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute(
                "INSERT INTO assignments (user_id, title, cl, due_date, notes) VALUES (%s, %s, %s, %s, %s)",
                (session["user_id"], title, cl, due_date, notes)
            )
        else:
            c.execute(
                "INSERT INTO assignments (user_id, title, cl, due_date, notes) VALUES (?, ?, ?, ?, ?)",
                (session["user_id"], title, cl, due_date, notes)
            )

    return redirect(url_for("index"))

@app.route("/delete/<int:id>", methods=["POST"])
def delete(id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("DELETE FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("DELETE FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))
    flash("Assignment deleted successfully.", "info")
    return redirect(url_for("index"))

@app.route("/submit/<int:id>", methods=["POST"])
def submit_assignment(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("UPDATE assignments SET submitted = TRUE WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("UPDATE assignments SET submitted = 1 WHERE id = ? AND user_id = ?", (id, session["user_id"]))

    flash("Assignment marked as submitted!", "success")
    return redirect(url_for("index"))

# --- CLASS LINKS (per-user) ---
@app.route("/classes", methods=["GET", "POST"])
def manage_classes():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not feature_enabled("manage_classes", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    with db_cursor() as c:
        if request.method == "POST":
            class_name = request.form.get("class_name", "").strip().lower()
            link = request.form.get("link", "").strip()
            if not class_name or not link:
                flash("Both class name and link are required.")
            else:
                if IS_POSTGRES:
                    c.execute("INSERT INTO class_links (user_id, class_name, link) VALUES (%s, %s, %s)",
                              (session["user_id"], class_name, link))
                else:
                    c.execute("INSERT INTO class_links (user_id, class_name, link) VALUES (?, ?, ?)",
                              (session["user_id"], class_name, link))

        if IS_POSTGRES:
            c.execute("SELECT * FROM class_links WHERE user_id = %s ORDER BY class_name ASC", (session["user_id"],))
        else:
            c.execute("SELECT * FROM class_links WHERE user_id = ? ORDER BY class_name ASC", (session["user_id"],))
        links = c.fetchall()

    return render_template("classes.html", classes=links)

@app.route("/delete_class_link/<int:id>", methods=["POST", "GET"])
def delete_class_link(id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("DELETE FROM class_links WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("DELETE FROM class_links WHERE id = ? AND user_id = ?", (id, session["user_id"]))
    flash("Class removed.")
    return redirect(url_for("manage_classes"))

# --- REDIRECT TO CLASS LINK ---
@app.route("/redirect/<int:id>")
def redirect_by_class(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT cl FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("SELECT cl FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))
        r = c.fetchone()
        if not r:
            flash("Assignment not found.")
            return redirect(url_for("index"))

        class_name = row_val(r, "cl")
        if class_name:
            class_name = str(class_name).strip().lower()

        if IS_POSTGRES:
            c.execute("SELECT link FROM class_links WHERE user_id = %s AND class_name = %s", (session["user_id"], class_name))
        else:
            c.execute("SELECT link FROM class_links WHERE user_id = ? AND class_name = ?", (session["user_id"], class_name))
        link_row = c.fetchone()

    if link_row:
        link = row_val(link_row, "link")
        return redirect(link)
    flash("⚠️ No link found for this class. Add one under 'Manage Classes'.")
    return redirect(url_for("manage_classes"))

# --- EDIT ASSIGNMENT ---
@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_assignment(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not feature_enabled("edit_assignment", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        cl = request.form.get("class", "").strip()
        due_date = request.form.get("due_date", "").strip()
        notes = request.form.get("notes", "").strip()

        if not title or not cl or not due_date:
            flash("Title, class and due date are required.")
            return redirect(url_for("edit_assignment", id=id))

        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute("""
                    UPDATE assignments SET title = %s, cl = %s, due_date = %s, notes = %s
                    WHERE id = %s AND user_id = %s
                """, (title, cl, due_date, notes, id, session["user_id"]))
            else:
                c.execute("""
                    UPDATE assignments SET title = ?, cl = ?, due_date = ?, notes = ?
                    WHERE id = ? AND user_id = ?
                """, (title, cl, due_date, notes, id, session["user_id"]))
        return redirect(url_for("index"))
    else:
        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute("SELECT * FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
            else:
                c.execute("SELECT * FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))
            assignment = c.fetchone()
            if not assignment:
                flash("You don't have permission to edit this assignment.")
                return redirect(url_for("index"))

    return render_template("edit.html", assignment=assignment)

@app.route("/tutorial")
def tutorial():
    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("tutorial.html")

@app.route("/finish_tutorial", methods=["POST"])
def finish_tutorial():
    if "user_id" not in session:
        return redirect(url_for("login"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("UPDATE users SET has_seen_tutorial = TRUE WHERE id = %s", (session["user_id"],))
        else:
            c.execute("UPDATE users SET has_seen_tutorial = 1 WHERE id = ?", (session["user_id"],))

    return redirect(url_for("index"))

@app.route("/dev-login", methods=["GET", "POST"])
def dev_login():
    if request.method == "POST":
        pin = request.form.get("pin")
        if pin == os.getenv("DEV_PIN", "1234"):
            session.clear()
            session["user_id"] = -1
            session["dev"] = True
            session["username"] = "developer"
            flash("🧠 Developer mode activated.", "info")
            return redirect(url_for("dev_dashboard"))
        else:
            flash("❌ Invalid PIN.", "error")
    return render_template("dev_login.html")

@app.route("/dev-dashboard")
def dev_dashboard():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("logout"))

    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS total FROM users")
        total_users = row_val(c.fetchone(), "total") or 0

        c.execute("SELECT COUNT(*) AS total FROM assignments")
        total_assignments = row_val(c.fetchone(), "total") or 0

        c.execute("SELECT COUNT(*) AS total FROM class_links")
        total_classes = row_val(c.fetchone(), "total") or 0

        try:
            c.execute("SELECT username FROM users ORDER BY id DESC LIMIT 5")
            recent_users = [row_val(row, "username") for row in c.fetchall()]
        except Exception:
            recent_users = []

    return render_template(
        "dev_dashboard.html",
        total_users=total_users,
        total_assignments=total_assignments,
        total_classes=total_classes,
        recent_users=recent_users,
        disabled_modal=session.pop("disabled_modal", None),
        feature_keys=list(app.config.get("FEATURE_FLAGS", {}).keys())
    )

@app.route("/dev-activate", methods=["POST"])
def dev_activate():
    session["dev"] = True
    return ("", 204)

@app.route("/dev-stats")
def dev_stats():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("logout"))
    return render_template("dev_stats_home.html")

@app.route("/dev-stats/total")
def dev_stats_total():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("login"))

    with db_cursor() as c:
        c.execute("""
            SELECT cl AS class, COUNT(*) AS total_assignments
            FROM assignments
            GROUP BY cl
            ORDER BY total_assignments DESC
        """)
        total_assignments = c.fetchall()
    print(total_assignments)

    return render_template("dev_stats_total.html", total_assignments=total_assignments)

@app.route("/dev-stats/overdue")
def dev_stats_overdue():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("login"))

    with db_cursor() as c:
        c.execute("""
            SELECT cl AS class, ROUND(COUNT(*) * 1.0 / COUNT(DISTINCT user_id), 2) AS avg_overdue
            FROM assignments
            WHERE due_date < CURRENT_DATE
            GROUP BY cl
            ORDER BY avg_overdue DESC
        """)
        overdue_per_class = c.fetchall()

    return render_template("dev_stats_overdue.html", overdue_per_class=overdue_per_class)

@app.route("/privacy-policy")
def privacy():
    return render_template("privacy.html", current_date=date.today().strftime("%B %d, %Y"))

@app.route("/account/update_settings", methods=["POST"])
def update_account_settings():
    if "user_id" not in session:
        return redirect(url_for("login"))

    dark = request.form.get("dark_mode") in ("1", "on", "true", "True")
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("UPDATE users SET dark_mode = %s WHERE id = %s", (dark, session["user_id"]))
        else:
            c.execute("UPDATE users SET dark_mode = ? WHERE id = ?", (int(dark), session["user_id"]))

    flash("Account settings updated.", "info")
    return redirect(url_for("account"))

@app.route("/account")
def account():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not feature_enabled("account", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT id, username, has_seen_tutorial, is_admin, dark_mode FROM users WHERE id = %s", (session["user_id"],))
        else:
            c.execute("SELECT id, username, has_seen_tutorial, is_admin, dark_mode FROM users WHERE id = ?", (session["user_id"],))
        user = c.fetchone()

    try:
        user = dict(user) if user is not None else None
    except Exception:
        pass

    return render_template("account.html", user=user)

@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if not feature_enabled("change_password", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    user_id = session["user_id"]

    if request.method == "POST":
        old_pw = request.form["old_password"]
        new_pw = request.form["new_password"]
        confirm_pw = request.form["confirm_password"]

        if new_pw != confirm_pw:
            flash("New passwords do not match.")
            return redirect(url_for("change_password"))

        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            else:
                c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            user = c.fetchone()

        if not user or not check_password_hash(row_val(user, "password"), old_pw):
            flash("Incorrect current password.")
            return redirect(url_for("change_password"))

        hashed = generate_password_hash(new_pw)
        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute("UPDATE users SET password = %s WHERE id = %s", (hashed, user_id))
            else:
                c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed, user_id))

        flash("Password updated successfully!")
        return redirect("/account")

    return render_template("change_password.html")

@app.route("/grade-tracker")
def grade_tracker():
    if not session.get("user_id"):
        return redirect("/login")

    if not feature_enabled("grade_tracker", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    classes = []
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("""
                SELECT id, class_name, link
                FROM class_links
                WHERE user_id = %s
                ORDER BY class_name ASC
            """, (session["user_id"],))
        else:
            c.execute("""
                SELECT id, class_name, link
                FROM class_links
                WHERE user_id = ?
                ORDER BY class_name ASC
            """, (session["user_id"],))
        raw_classes = c.fetchall()

        # small helper to coerce DB value into bytes for Fernet
        def _to_bytes(val):
            if val is None:
                return None
            if isinstance(val, (bytes, bytearray)):
                return bytes(val)
            try:
                return bytes(val)
            except Exception:
                return str(val).encode()

        for cr in raw_classes:
            class_id = row_val(cr, "id")
            class_name = row_val(cr, "class_name")
            link = row_val(cr, "link")

            # get assignment ids that belong to this class for this user
            if IS_POSTGRES:
                c.execute("""
                    SELECT id
                    FROM assignments
                    WHERE user_id = %s
                      AND LOWER(TRIM(cl)) = LOWER(TRIM(%s))
                """, (session["user_id"], class_name))
            else:
                c.execute("""
                    SELECT id
                    FROM assignments
                    WHERE user_id = ? AND TRIM(cl) = TRIM(?)
                """, (session["user_id"], class_name))
            assignment_rows = c.fetchall()
            assignment_ids = [row_val(a, "id") for a in assignment_rows]

            assignments_count = len(assignment_ids)
            graded_count = 0
            percentages = []

            if assignment_ids:
                if IS_POSTGRES:
                    placeholders = ",".join(["%s"] * len(assignment_ids))
                    sql = f"SELECT grade, out_of FROM grades WHERE user_id = %s AND assignment_id IN ({placeholders})"
                    params = [session["user_id"]] + assignment_ids
                    c.execute(sql, tuple(params))
                else:
                    placeholders = ",".join(["?"] * len(assignment_ids))
                    sql = f"SELECT grade, out_of FROM grades WHERE user_id = ? AND assignment_id IN ({placeholders})"
                    params = [session["user_id"]] + assignment_ids
                    c.execute(sql, params)

                grade_rows = c.fetchall()
                for gr in grade_rows:
                    enc_grade = row_val(gr, "grade")
                    enc_out = row_val(gr, "out_of")

                    g_bytes = _to_bytes(enc_grade)
                    o_bytes = _to_bytes(enc_out)

                    if not g_bytes or not o_bytes:
                        continue

                    try:
                        g_val = decrypt_grade(g_bytes)
                        o_val = decrypt_grade(o_bytes)
                        if o_val and o_val != 0:
                            pct = (float(g_val) / float(o_val)) * 100.0
                            percentages.append(pct)
                            graded_count += 1
                    except Exception:
                        continue

            class_average = round(sum(percentages) / len(percentages), 2) if percentages else None

            classes.append({
                "id": class_id,
                "class_name": class_name,
                "link": link,
                "assignments_count": assignments_count,
                "graded_count": graded_count,
                "average": class_average
            })

    return render_template("grade_tracker.html", classes=classes)

@app.route("/dev-add-disabled-function", methods=["POST"])
def dev_add_disabled_function():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("logout"))

    function_key = request.form.get("function_key", "grade_tracker").strip()
    function_label = request.form.get("function_label", function_key).strip() or function_key
    reason = request.form.get("reason", "Disabled by developer").strip() or "Disabled by developer"

    try:
        flags = app.config.setdefault("FEATURE_FLAGS", {})
        if function_key in flags:
            flags[function_key] = False
            save_feature_flags()
            session["disabled_modal"] = {"function": function_label, "reason": reason}
            flash(f"Disabled '{function_label}' for testing.", "info")
        else:
            session["disabled_modal"] = {"function": function_label, "reason": f"Failed: unknown feature key '{function_key}'"}
            flash(f"Could not disable '{function_label}': unknown feature key.", "error")
    except Exception as e:
        session["disabled_modal"] = {"function": function_label, "reason": f"Error: {str(e)}"}
        flash(f"Error disabling '{function_label}'.", "error")

    return redirect(url_for("dev_dashboard"))

@app.route("/grade-tracker/<int:class_id>")
def grade_tracker_class(class_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    with db_cursor() as c:

        # ---------------------------------------------------------
        # 1) Get CLASS NAME from class_links (correct table)
        # ---------------------------------------------------------
        if IS_POSTGRES:
            c.execute("""
                SELECT class_name 
                FROM class_links 
                WHERE id = %s AND user_id = %s
            """, (class_id, session["user_id"]))
        else:
            c.execute("""
                SELECT class_name 
                FROM class_links 
                WHERE id = ? AND user_id = ?
            """, (class_id, session["user_id"]))

        row = c.fetchone()
        if not row:
            return "Class not found or unauthorized.", 404

        try:
            class_name = row["class_name"]
        except Exception:
            class_name = row[0]

        # Normalize class name for lookup
        class_name_norm = class_name.strip().lower()
        print("DEBUG class_name_norm:", class_name_norm)

        # ---------------------------------------------------------
        # 2) Get ASSIGNMENTS for this class
        # ---------------------------------------------------------
        if IS_POSTGRES:
            c.execute("""
                SELECT id, title, due_date, notes, submitted, cl
                FROM assignments
                WHERE user_id = %s 
                  AND LOWER(TRIM(cl)) = LOWER(TRIM(%s))
                ORDER BY due_date ASC
            """, (session["user_id"], class_name_norm))
        else:
            c.execute("""
                SELECT id, title, due_date, notes, submitted, cl
                FROM assignments
                WHERE user_id = ?
                  AND LOWER(TRIM(cl)) = LOWER(TRIM(?))
                ORDER BY due_date ASC
            """, (session["user_id"], class_name_norm))

        assignment_rows = c.fetchall()
        print("DEBUG assignment_rows count:", len(assignment_rows))

        assignments = []
        proficiency_scores = []
        regular_scores = []

        # ---------------------------------------------------------
        # 3) Load GRADES for each assignment
        # ---------------------------------------------------------
        for ar in assignment_rows:
            # normalize row → dict
            try:
                a = dict(ar)
            except Exception:
                a = {
                    "id": ar[0],
                    "title": ar[1],
                    "due_date": ar[2],
                    "notes": ar[3],
                    "submitted": ar[4],
                    "cl": ar[5] if len(ar) > 5 else None
                }

            print("DEBUG assignment cl repr:", repr(a.get("cl")))

            # latest grade entry
            if IS_POSTGRES:
                c.execute("""
                    SELECT grade, out_of, proficiency
                    FROM grades
                    WHERE assignment_id = %s AND user_id = %s
                    ORDER BY id DESC
                    LIMIT 1
                """, (a["id"], session["user_id"]))
            else:
                c.execute("""
                    SELECT grade, out_of,
                           COALESCE(proficiency, 0) AS proficiency
                    FROM grades
                    WHERE assignment_id = ? AND user_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                """, (a["id"], session["user_id"]))

            grade_row = c.fetchone()

            g_val = None
            out_val = None
            prof_flag = 0

            if grade_row:
                try:
                    g_token = grade_row["grade"]
                    out_token = grade_row["out_of"]
                    prof_flag = grade_row["proficiency"]
                except Exception:
                    g_token = grade_row[0]
                    out_token = grade_row[1]
                    prof_flag = grade_row[2]

                g_val = decrypt_grade_safe(g_token)
                out_val = decrypt_grade_safe(out_token)

            # compute percent
            percent = None
            if g_val is not None and out_val not in (None, 0):
                percent = round((g_val / out_val) * 100, 2)
                if prof_flag == 1:
                    proficiency_scores.append(percent)
                else:
                    regular_scores.append(percent)

            # enrich assignment dict
            a["grade_value"] = g_val
            a["out_of_value"] = out_val
            a["percent"] = percent
            a["proficiency"] = prof_flag

            assignments.append(a)

        # ---------------------------------------------------------
        # 4) Compute class average
        # ---------------------------------------------------------
        if proficiency_scores:
            p_avg = sum(proficiency_scores) / len(proficiency_scores)
        else:
            p_avg = None

        if regular_scores:
            r_avg = sum(regular_scores) / len(regular_scores)
        else:
            r_avg = None

        if p_avg is not None and r_avg is not None:
            class_average = (p_avg * 0.90) + (r_avg * 0.10)
        elif p_avg is not None:
            class_average = p_avg
        elif r_avg is not None:
            class_average = r_avg
        else:
            class_average = None

    return render_template(
        "grade_tracker_class.html",
        class_name=class_name,
        assignments=assignments,
        class_average=class_average,
    )


@app.route("/add-grade/<int:assignment_id>", methods=["GET", "POST"])
def add_grade(assignment_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("""
                SELECT a.id, a.user_id, a.title, a.cl, cl.id AS class_id
                FROM assignments a
                JOIN class_links cl ON LOWER(TRIM(a.cl)) = LOWER(TRIM(cl.class_name))
                WHERE a.id = %s AND a.user_id = %s
            """, (assignment_id, session["user_id"]))
        else:
            c.execute("""
                SELECT a.id, a.user_id, a.title, a.cl, cl.id AS class_id
                FROM assignments a
                JOIN class_links cl ON TRIM(a.cl) = TRIM(cl.class_name)
                WHERE a.id = ? AND a.user_id = ?
            """, (assignment_id, session["user_id"]))
        assignment = c.fetchone()

        if not assignment:
            return "Assignment not found or unauthorized.", 404

        if request.method == "POST":
            grade = request.form.get("grade")
            out_of = request.form.get("out_of")

            if not grade or not out_of:
                return "Missing fields", 400

            encrypted_grade = encrypt_grade(float(grade))
            encrypted_out_of = encrypt_grade(float(out_of))

            if IS_POSTGRES:
                c.execute("""
                    INSERT INTO grades (assignment_id, user_id, grade, out_of)
                    VALUES (%s, %s, %s, %s)
                """, (assignment_id, session["user_id"], encrypted_grade, encrypted_out_of))
            else:
                c.execute("""
                    INSERT INTO grades (assignment_id, user_id, grade, out_of)
                    VALUES (?, ?, ?, ?)
                """, (assignment_id, session["user_id"], encrypted_grade, encrypted_out_of))

            class_id = row_val(assignment, "class_id")
            return redirect(url_for("grade_tracker_class", class_id=class_id))

    return render_template("add_grade.html", assignment=assignment)

@app.route("/submitted-assignments")
def submitted_assignments():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not feature_enabled("submitted-view", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("""
                SELECT *
                FROM assignments
                WHERE user_id = %s AND submitted = TRUE
                ORDER BY due_date DESC
            """, (session["user_id"],))
        else:
            c.execute("""
                SELECT *
                FROM assignments
                WHERE user_id = ? AND submitted = 1
                ORDER BY due_date DESC
            """, (session["user_id"],))
        assignments = c.fetchall()

    return render_template("submitted_assignments.html", assignments=assignments)

@app.route("/goals")
def goals_page():
    if "user_id" not in session:
        return redirect(url_for("login"))
    
    if not feature_enabled("grade_tracker", default=True):
        if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
            pass
        else:
            return render_template("disabled.html"), 403

    user_id = session["user_id"]

    # --- NEW: fetch class list for dropdown ---
    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT id, class_name FROM class_links WHERE user_id = %s ORDER BY class_name ASC", (user_id,))
        else:
            c.execute("SELECT id, class_name FROM class_links WHERE user_id = ? ORDER BY class_name ASC", (user_id,))
        class_list = c.fetchall()
    # ------------------------------------------

    rows = get_user_goals(user_id)

    # attach progress to each goal for UI rendering
    annotated = []
    for r in rows:
        progress = compute_goal_progress(r, user_id)
        try:
            g = dict(r)
        except Exception:
            g = {
                "id": row_val(r, "id"),
                "user_id": row_val(r, "user_id"),
                "class_id": row_val(r, "class_id"),
                "title": row_val(r, "title"),
                "goal_type": row_val(r, "goal_type"),
                "target_value": row_val(r, "target_value"),
                "deadline": row_val(r, "deadline"),
                "created_at": row_val(r, "created_at"),
            }
        g["progress_meta"] = progress
        annotated.append(g)
        print("CLASS LIST:", class_list)
    return render_template("goals.html", goals=annotated, class_list=class_list)


@app.route("/goals/add", methods=["POST"])
def add_goal():
    if "user_id" not in session:
        return redirect(url_for("login"))

    title = request.form.get("title", "").strip()
    goal_type = request.form.get("goal_type", "").strip().lower()
    target_value = request.form.get("target_value", "").strip()
    deadline = request.form.get("deadline", "").strip() or None
    class_id = request.form.get("class_id") or None

    # basic validation
    if not title or not goal_type:
        flash("Title and goal type are required.")
        return redirect(url_for("goals_page"))

    # normalize numeric target
    tval = None
    if target_value:
        try:
            tval = float(target_value)
        except Exception:
            tval = None

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("""
                INSERT INTO goals (user_id, class_id, title, goal_type, target_value, deadline)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (session["user_id"], class_id, title, goal_type, tval, deadline))
        else:
            c.execute("""
                INSERT INTO goals (user_id, class_id, title, goal_type, target_value, deadline)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session["user_id"], class_id, title, goal_type, tval, deadline))

    flash("Goal added.")
    return redirect(url_for("goals_page"))


@app.route("/goals/<int:goal_id>/update", methods=["POST"])
def update_goal(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    title = request.form.get("title", "").strip()
    goal_type = request.form.get("goal_type", "").strip().lower()
    target_value = request.form.get("target_value", "").strip()
    deadline = request.form.get("deadline", "").strip() or None
    class_id = request.form.get("class_id") or None

    if not title or not goal_type:
        flash("Title and goal type required.")
        return redirect(url_for("goals_page"))

    tval = None
    if target_value:
        try:
            tval = float(target_value)
        except Exception:
            tval = None

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("""
                UPDATE goals SET title = %s, goal_type = %s, target_value = %s, deadline = %s, class_id = %s
                WHERE id = %s AND user_id = %s
            """, (title, goal_type, tval, deadline, class_id, goal_id, session["user_id"]))
        else:
            c.execute("""
                UPDATE goals SET title = ?, goal_type = ?, target_value = ?, deadline = ?, class_id = ?
                WHERE id = ? AND user_id = ?
            """, (title, goal_type, tval, deadline, class_id, goal_id, session["user_id"]))

    flash("Goal updated.")
    return redirect(url_for("goals_page"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
def delete_goal(goal_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("DELETE FROM goals WHERE id = %s AND user_id = %s", (goal_id, session["user_id"]))
        else:
            c.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, session["user_id"]))

    flash("Goal removed.")
    return redirect(url_for("goals_page"))


# JSON APIs for widgets / AJAX
@app.route("/api/goals")
def api_goals():
    if "user_id" not in session:
        return jsonify({"error": "auth_required"}), 401

    rows = get_user_goals(session["user_id"])
    out = []
    for r in rows:
        try:
            g = dict(r)
        except Exception:
            g = {
                "id": row_val(r, "id"),
                "user_id": row_val(r, "user_id"),
                "class_id": row_val(r, "class_id"),
                "title": row_val(r, "title"),
                "goal_type": row_val(r, "goal_type"),
                "target_value": row_val(r, "target_value"),
                "deadline": row_val(r, "deadline"),
                "created_at": row_val(r, "created_at"),
            }
        g["progress_meta"] = compute_goal_progress(r, session["user_id"])
        out.append(g)
    return jsonify(out)


@app.route("/api/goals/<int:goal_id>")
def api_goal_detail(goal_id):
    if "user_id" not in session:
        return jsonify({"error": "auth_required"}), 401

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("SELECT * FROM goals WHERE id = %s AND user_id = %s", (goal_id, session["user_id"]))
        else:
            c.execute("SELECT * FROM goals WHERE id = ? AND user_id = ?", (goal_id, session["user_id"]))
        row = c.fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404

    try:
        g = dict(row)
    except Exception:
        g = {
            "id": row_val(row, "id"),
            "user_id": row_val(row, "user_id"),
            "class_id": row_val(row, "class_id"),
            "title": row_val(row, "title"),
            "goal_type": row_val(row, "goal_type"),
            "target_value": row_val(row, "target_value"),
            "deadline": row_val(row, "deadline"),
            "created_at": row_val(row, "created_at"),
        }
    g["progress_meta"] = compute_goal_progress(row, session["user_id"])
    return jsonify(g)

@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    """Feedback form for users to submit feedback about the website."""
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            flash("Please enter some feedback.")
            return redirect(url_for("feedback"))

        # Get user's name from session or use username
        name = session.get("username", "Anonymous")

        with db_cursor() as c:
            if IS_POSTGRES:
                c.execute(
                    "INSERT INTO feedback (name, message, submitted_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
                    (name, message)
                )
            else:
                c.execute(
                    "INSERT INTO feedback (name, message, submitted_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (name, message)
                )
        flash("✅ Thank you for your feedback! We appreciate your input.")
        return redirect(url_for("feedback"))

    # GET request - show form
    return render_template("feedback.html")


@app.route("/feedback-list")
def feedback_list():
    """Admin view to see all feedback submitted."""
    if "user_id" not in session:
        return redirect(url_for("login"))

    # Only admins can view all feedback
    if not (session.get("is_admin") == 1 or session.get("dev")):
        flash("You do not have permission to view feedback.")
        return redirect(url_for("index"))

    with db_cursor() as c:
        if IS_POSTGRES:
            c.execute("""
                SELECT id, name, message, submitted_at
                FROM feedback
                ORDER BY submitted_at DESC
            """)
        else:
            c.execute("""
                SELECT id, name, message, submitted_at
                FROM feedback
                ORDER BY submitted_at DESC
            """)
        feedbacks = c.fetchall()

    return render_template("feedback_list.html", feedbacks=feedbacks)





if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
