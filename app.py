# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, current_app
from datetime import datetime, date
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import sqlite3
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
import json

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
                    # overwrite/merge persisted flags
                    app.config["FEATURE_FLAGS"].update(data)
    except Exception:
        # ignore on failure (keep defaults)
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
MAINTENANCE_FILE = "maintenance.json"

# --- Helpers ---
def get_connection():
    """Return a DB connection. Postgres -> psycopg2 (RealDictCursor), else sqlite3."""
    if IS_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect("assignments.db")
    conn.row_factory = sqlite3.Row
    return conn


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


def execute_select(cursor, query, params=()):
    """
    Helper to run SELECT with correct placeholders depending on DB.
    For Postgres use %s, for sqlite use ?.
    `query` should already use the correct placeholder style.
    """
    cursor.execute(query, params)


def placeholder(q_postgres, q_sqlite):
    """Return query string appropriate to current DB type."""
    return q_postgres if IS_POSTGRES else q_sqlite

def get_maintenance():
    if os.path.exists(MAINTENANCE_FILE):
        with open(MAINTENANCE_FILE) as f:
            try:
                return json.load(f).get("enabled", False)
            except json.JSONDecodeError:
                return False
    return False

def set_maintenance(value: bool):
    with open(MAINTENANCE_FILE, "w") as f:
        json.dump({"enabled": value}, f)


@app.context_processor
def inject_dark_mode():
    """Make current user's dark mode preference available to templates as `dark_mode`.

    Returns False if not logged in or on error.
    """
    dark = False
    if session.get("user_id"):
        conn = get_connection()
        c = conn.cursor()
        try:
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
        finally:
            conn.close()
    return {"dark_mode": dark}

# --- DB setup ---
def init_db():
    conn = get_connection()
    c = conn.cursor()

    # users table (base columns)
    if IS_POSTGRES:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)
        # add extra columns safely
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_seen_tutorial BOOLEAN DEFAULT FALSE;")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0;")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS dark_mode BOOLEAN DEFAULT FALSE;")
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)
        # SQLite ALTER TABLE add columns (ignore if already exist)
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

    # classes table (the "real" classes used by grade tracker) - use class_name & link columns
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

    conn.commit()
    conn.close()

init_db()

# put this BEFORE your route definitions
@app.before_request
def check_for_maintenance():
    # if maintenance is not enabled, do nothing
    if not get_maintenance():
        return None

    # allow access to static assets always
    if request.path.startswith("/static/"):
        return None

    # FULL BYPASS: dev, special -1 user, or admin user
    if session.get("dev") or session.get("user_id") == -1 or session.get("is_admin") == 1:
        return None

    whitelist_endpoints = {
        "maintenance",
        "dev_dashboard",
        "dev_login",
        "dev_toggle_maintenance",
        "dev_login_activate",
        "login",
        "logout",
    }

    endpoint = (request.endpoint or "").split(".")[-1]
    if endpoint in whitelist_endpoints:
        return None

    return redirect(url_for("maintenance"))

# --- AUTH ---
@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))

    # safe feature flag check (default True)
    if not feature_enabled("register", default=True):
        return render_template("disabled.html"), 403

    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        if not username or not password:
            flash("Username and password required.")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        conn = get_connection()
        c = conn.cursor()
        try:
            if IS_POSTGRES:
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, hashed_pw))
            else:
                c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
            conn.commit()
            flash("✅ Account created — please log in.")
            return redirect(url_for("login"))
        except Exception:
            flash("Username already exists or error creating account.")
        finally:
            conn.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if not feature_enabled("login", default=True):
        return render_template("disabled.html"), 403

    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        conn = get_connection()
        c = conn.cursor()
        try:
            if IS_POSTGRES:
                c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
            else:
                # use LOWER for case-insensitive match
                c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,))
            user = c.fetchone()
            # print debug row as dict (works for sqlite3.Row and RealDictRow)
            print("DEBUG USER ROW:", dict(user) if user is not None else None)
        finally:
            conn.close()

        if user:
            stored_pw = row_val(user, "password")
            if stored_pw and check_password_hash(stored_pw, password):
                session["user_id"] = row_val(user, "id")
                session["username"] = row_val(user, "username")
                session["is_admin"] = row_val(user, "is_admin") or 0
                # has_seen_tutorial may be stored as int/bool
                has_seen_tutorial = row_val(user, "has_seen_tutorial") or False
                # normalize to Python bool
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

    conn = get_connection()
    c = conn.cursor()
    try:
        if IS_POSTGRES:
            c.execute("SELECT * FROM assignments WHERE user_id = %s ORDER BY submitted ASC, due_date ASC", (session["user_id"],))
        else:
            c.execute("SELECT * FROM assignments WHERE user_id = ? ORDER BY submitted ASC, due_date ASC", (session["user_id"],))
        rows = c.fetchall()
    finally:
        conn.close()

    today = datetime.now().date()
    annotated = []
    for r in rows:
        # convert row to dict (works for both RealDictRow and sqlite3.Row)
        try:
            row = dict(r)
        except Exception:
            row = r
        # validate due_date format
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

    conn = get_connection()
    c = conn.cursor()
    try:
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
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("index"))

@app.route("/delete/<int:id>", methods=["POST"])
def delete(id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_connection()
    c = conn.cursor()
    try:
        if IS_POSTGRES:
            c.execute("DELETE FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("DELETE FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))
        conn.commit()
    finally:
        conn.close()
    flash("Assignment deleted successfully.", "info")
    return redirect(url_for("index"))


@app.route("/submit/<int:id>", methods=["POST"])
def submit_assignment(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        if IS_POSTGRES:
            c.execute("UPDATE assignments SET submitted = TRUE WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("UPDATE assignments SET submitted = 1 WHERE id = ? AND user_id = ?", (id, session["user_id"]))
        conn.commit()
    finally:
        conn.close()

    flash("Assignment marked as submitted!", "success")
    return redirect(url_for("index"))


# --- CLASS LINKS (per-user) ---
@app.route("/classes", methods=["GET", "POST"])
def manage_classes():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not feature_enabled("manage_classes", default=True):
        return render_template("disabled.html"), 403

    conn = get_connection()
    c = conn.cursor()
    try:
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
                conn.commit()

        # load links
        if IS_POSTGRES:
            c.execute("SELECT * FROM class_links WHERE user_id = %s ORDER BY class_name ASC", (session["user_id"],))
        else:
            c.execute("SELECT * FROM class_links WHERE user_id = ? ORDER BY class_name ASC", (session["user_id"],))
        links = c.fetchall()
    finally:
        conn.close()
    return render_template("classes.html", classes=links)


@app.route("/delete_class_link/<int:id>", methods=["POST", "GET"])
def delete_class_link(id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_connection()
    c = conn.cursor()
    try:
        if IS_POSTGRES:
            c.execute("DELETE FROM class_links WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("DELETE FROM class_links WHERE id = ? AND user_id = ?", (id, session["user_id"]))
        conn.commit()
    finally:
        conn.close()
    flash("Class removed.")
    return redirect(url_for("manage_classes"))


# --- REDIRECT TO CLASS LINK ---
@app.route("/redirect/<int:id>")
def redirect_by_class(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        # get class name for assignment
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

        # find link for this user + class
        if IS_POSTGRES:
            c.execute("SELECT link FROM class_links WHERE user_id = %s AND class_name = %s", (session["user_id"], class_name))
        else:
            c.execute("SELECT link FROM class_links WHERE user_id = ? AND class_name = ?", (session["user_id"], class_name))
        link_row = c.fetchone()
    finally:
        conn.close()

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
        return render_template("disabled.html"), 403

    conn = get_connection()
    c = conn.cursor()
    try:
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            cl = request.form.get("class", "").strip()
            due_date = request.form.get("due_date", "").strip()
            notes = request.form.get("notes", "").strip()

            if not title or not cl or not due_date:
                flash("Title, class and due date are required.")
                return redirect(url_for("edit_assignment", id=id))

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
            conn.commit()
            return redirect(url_for("index"))
        else:
            if IS_POSTGRES:
                c.execute("SELECT * FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
            else:
                c.execute("SELECT * FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))
            assignment = c.fetchone()
            if not assignment:
                flash("You don't have permission to edit this assignment.")
                return redirect(url_for("index"))
    finally:
        conn.close()

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

    conn = get_connection()
    c = conn.cursor()

    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        c.execute("UPDATE users SET has_seen_tutorial = TRUE WHERE id = %s", (session["user_id"],))
    else:
        c.execute("UPDATE users SET has_seen_tutorial = 1 WHERE id = ?", (session["user_id"],))

    conn.commit()
    conn.close()

    return redirect(url_for("index"))

@app.route("/dev-login", methods=["GET", "POST"])
def dev_login():
    if request.method == "POST":
        pin = request.form.get("pin")
        if pin == os.getenv("DEV_PIN", "1234"):  # You can set DEV_PIN in .env
            session.clear()
            session["user_id"] = -1
            session["dev"] = True  # ✅ Mark this session as developer
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

    conn = get_connection()
    c = conn.cursor()

    try:
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
    finally:
        conn.close()

    return render_template(
        "dev_dashboard.html",
        total_users=total_users,
        total_assignments=total_assignments,
        total_classes=total_classes,
        recent_users=recent_users,
        maintenance_mode=get_maintenance(),
        disabled_modal=session.pop("disabled_modal", None),
        feature_keys=list(app.config.get("FEATURE_FLAGS", {}).keys())
    )


@app.route("/dev-activate", methods=["POST"])
def dev_activate():
    session["dev"] = True
    return ("", 204)  # Silent success (no content)


@app.route("/dev-stats")
def dev_stats():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("logout"))
    return render_template("dev_stats_home.html")


@app.route("/dev-stats/total")
def dev_stats_total():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT cl AS class, COUNT(*) AS total_assignments
        FROM assignments
        GROUP BY cl
        ORDER BY total_assignments DESC
    """)
    total_assignments = c.fetchall()
    print(total_assignments)


    conn.close()
    return render_template("dev_stats_total.html", total_assignments=total_assignments)


@app.route("/dev-stats/overdue")
def dev_stats_overdue():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT cl AS class, ROUND(COUNT(*) * 1.0 / COUNT(DISTINCT user_id), 2) AS avg_overdue
        FROM assignments
        WHERE due_date < CURRENT_DATE
        GROUP BY cl
        ORDER BY avg_overdue DESC
    """)
    overdue_per_class = c.fetchall()

    conn.close()
    return render_template("dev_stats_overdue.html", overdue_per_class=overdue_per_class)

@app.route("/privacy-policy")
def privacy():
    return render_template("privacy.html", current_date=date.today().strftime("%B %d, %Y"))

@app.route("/maintenance")
def maintenance():
    return render_template("maintenance.html", year=datetime.now().year), 503

@app.route("/toggle-maintenance")
def dev_toggle_maintenance():
    current = get_maintenance()
    new_state = not current
    set_maintenance(new_state)
    flash(f"Maintenance mode {'ENABLED' if new_state else 'DISABLED'}.", "info")
    return redirect(url_for("dev_dashboard"))

@app.route("/my-classes")
def my_classes():
    if "user_id" not in session:
        return redirect("/login")

    if not feature_enabled("classes_list", default=True):
        return render_template("disabled.html"), 403

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT id, class_name as name, '' as description, '' as color
        FROM classes
        WHERE user_id = ?
        ORDER BY class_name ASC
    """, (session["user_id"],))

    classes = c.fetchall()
    conn.close()

    return render_template("my_classes.html", classes=classes)

@app.route("/add-class", methods=["POST"])
def add_class():
    if "user_id" not in session:
        return redirect("/login")

    name = request.form.get("name")
    description = request.form.get("description")
    color = request.form.get("color") or "#3b82f6"  # default blue

    conn = get_connection()
    c = conn.cursor()

    # insert into classes table (class_name + link optional)
    c.execute("""
        INSERT INTO classes (user_id, class_name, link)
        VALUES (?, ?, ?)
    """, (session["user_id"], name, request.form.get("link", "")))

    conn.commit()
    conn.close()

    return redirect("/my-classes")

@app.route("/delete-class/<int:class_id>", methods=["POST"])
def delete_class(class_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        DELETE FROM classes
        WHERE id = ? AND user_id = ?
    """, (class_id, session["user_id"]))

    conn.commit()
    conn.close()

    return redirect("/my-classes")


@app.route("/account/update_settings", methods=["POST"])
def update_account_settings():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # checkbox value will be 'on' when checked
    dark = 1 if request.form.get("dark_mode") in ("1", "on", "true", "True") else 0

    conn = get_connection()
    c = conn.cursor()
    try:
        if IS_POSTGRES:
            c.execute("UPDATE users SET dark_mode = %s WHERE id = %s", (dark, session["user_id"]))
        else:
            c.execute("UPDATE users SET dark_mode = ? WHERE id = ?", (dark, session["user_id"]))
        conn.commit()
    finally:
        conn.close()

    flash("Account settings updated.", "info")
    return redirect(url_for("account"))

@app.route("/account")
def account():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not feature_enabled("account", default=True):
        return render_template("disabled.html"), 403

    conn = get_connection()
    c = conn.cursor()

    try:
        if IS_POSTGRES:
            c.execute("SELECT id, username, has_seen_tutorial, is_admin, dark_mode FROM users WHERE id = %s", (session["user_id"],))
        else:
            c.execute("SELECT id, username, has_seen_tutorial, is_admin, dark_mode FROM users WHERE id = ?", (session["user_id"],))

        user = c.fetchone()
    finally:
        conn.close()

    # normalize to plain dict for templates
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
        return render_template("disabled.html"), 403

    user_id = session["user_id"]

    if request.method == "POST":
        old_pw = request.form["old_password"]
        new_pw = request.form["new_password"]
        confirm_pw = request.form["confirm_password"]

        if new_pw != confirm_pw:
            flash("New passwords do not match.")
            return redirect(url_for("change_password"))

        conn = get_connection()
        c = conn.cursor()

        try:
            if IS_POSTGRES:
                c.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            else:
                c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            user = c.fetchone()
        finally:
            conn.close()

        if not user or not check_password_hash(row_val(user, "password"), old_pw):
            flash("Incorrect current password.")
            return redirect(url_for("change_password"))

        hashed = generate_password_hash(new_pw)

        conn = get_connection()
        c = conn.cursor()
        try:
            if IS_POSTGRES:
                c.execute("UPDATE users SET password = %s WHERE id = %s", (hashed, user_id))
            else:
                c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed, user_id))
            conn.commit()
        finally:
            conn.close()

        flash("Password updated successfully!")
        return redirect("/account")

    return render_template("change_password.html")

@app.route("/grade-tracker")
def grade_tracker():
    if not session.get("user_id"):
        return redirect("/login")

    if not feature_enabled("grade_tracker", default=True):
        return render_template("disabled.html"), 403

    conn = get_connection()
    c = conn.cursor()

    if IS_POSTGRES:
        c.execute("""
            SELECT id, class_name, link
            FROM class_links
            WHERE user_id = %s
        """, (session["user_id"],))
    else:
        c.execute("""
            SELECT id, class_name, link
            FROM class_links
            WHERE user_id = ?
        """, (session["user_id"],))


    classes = c.fetchall()
    conn.close()

    return render_template("grade_tracker.html", classes=classes)

@app.route("/dev-add-disabled-function", methods=["POST"])
def dev_add_disabled_function():
    if not session.get("dev") and session.get("user_id") != -1:
        return redirect(url_for("logout"))

    # Read submitted form values (function key, display name, and reason)
    function_key = request.form.get("function_key", "grade_tracker").strip()
    function_label = request.form.get("function_label", function_key).strip() or function_key
    reason = request.form.get("reason", "Disabled by developer").strip() or "Disabled by developer"

    # Safely update feature flag if it exists in config
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
