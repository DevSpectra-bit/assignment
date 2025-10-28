# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash
from datetime import datetime
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import sqlite3
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

# Load .env if available
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith("postgres"))


# --- Helpers ---
def get_connection():
    """Return a DB connection. Postgres -> psycopg2 (RealDictCursor), else sqlite3."""
    if IS_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect("assignments.db")
    conn.row_factory = sqlite3.Row
    return conn


def row_val(row, key):
    """Get value from either dict-like (Postgres RealDict) or sqlite3.Row."""
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        # fallback: try attribute
        return getattr(row, key, None)


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


# --- DB setup ---
def init_db():
    conn = get_connection()
    c = conn.cursor()

    # users
    if IS_POSTGRES:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)

    # assignments
    if IS_POSTGRES:
        c.execute("""
            CREATE TABLE IF NOT EXISTS assignments (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                cl TEXT NOT NULL,
                due_date TEXT NOT NULL,
                notes TEXT
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
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
        """)

    # class_links
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

    conn.commit()
    conn.close()


init_db()


# --- AUTH ---
@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))

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
            # Could be duplicate username; keep message generic
            flash("Username already exists or error creating account.")
        finally:
            conn.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        conn = get_connection()
        c = conn.cursor()
        try:
            if IS_POSTGRES:
                c.execute("SELECT * FROM users WHERE username = %s", (username,))
            else:
                c.execute("SELECT * FROM users WHERE username = ?", (username,))
            user = c.fetchone()
        finally:
            conn.close()

        if user:
            stored_pw = row_val(user, "password")
            if stored_pw and check_password_hash(stored_pw, password):
                session["user_id"] = row_val(user, "id")
                session["username"] = row_val(user, "username")
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

    conn = get_connection()
    c = conn.cursor()
    try:
        if IS_POSTGRES:
            c.execute("SELECT * FROM assignments WHERE user_id = %s ORDER BY due_date ASC", (session["user_id"],))
        else:
            c.execute("SELECT * FROM assignments WHERE user_id = ? ORDER BY due_date ASC", (session["user_id"],))
        rows = c.fetchall()
    finally:
        conn.close()

    today = datetime.now().date()
    annotated = []
    for r in rows:
        row = dict(r) if IS_POSTGRES else dict(r)  # both support dict() for consistent access
        # validate due_date format
        try:
            due_date = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        days_left = (due_date - today).days
        annotated.append({
            **row,
            "is_past_due": days_left < 0,
            "is_due_today": days_left == 0,
            "is_due_tomorrow": days_left == 1
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


# --- CLASS LINKS (per-user) ---
@app.route("/classes", methods=["GET", "POST"])
def manage_classes():
    if "user_id" not in session:
        return redirect(url_for("login"))

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


@app.route("/delete_class/<int:id>", methods=["POST", "GET"])
def delete_class(id):
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

        class_name = row_val(r, "cl").strip().lower()

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
def edit(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

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
                return redirect(url_for("edit", id=id))

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
