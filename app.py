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

# Use DATABASE_URL if available (Render), otherwise fallback to SQLite
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- DATABASE CONNECTION HELPER ---
def get_connection():
    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    else:
        conn = sqlite3.connect("assignments.db")
        conn.row_factory = sqlite3.Row
        return conn


# --- DATABASE SETUP ---
def init_db():
    conn = get_connection()
    c = conn.cursor()
    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)
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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)
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
    conn.commit()
    conn.close()


init_db()

# --- AUTH ROUTES ---
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        hashed_pw = generate_password_hash(password)

        conn = get_connection()
        c = conn.cursor()

        try:
            if DATABASE_URL and DATABASE_URL.startswith("postgres"):
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, hashed_pw))
            else:
                c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
            conn.commit()
            flash("✅ Account created! Please log in.")
            return redirect(url_for("login"))
        except Exception:
            flash("⚠️ Username already exists.")
        finally:
            conn.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]

        conn = get_connection()
        c = conn.cursor()

        if DATABASE_URL and DATABASE_URL.startswith("postgres"):
            c.execute("SELECT * FROM users WHERE username = %s", (username,))
        else:
            c.execute("SELECT * FROM users WHERE username = ?", (username,))

        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        else:
            flash("❌ Invalid username or password.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("👋 Logged out successfully.")
    return redirect(url_for("login"))


# --- MAIN ROUTES ---
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()

    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        c.execute("SELECT * FROM assignments WHERE user_id = %s ORDER BY due_date ASC", (session["user_id"],))
    else:
        c.execute("SELECT * FROM assignments WHERE user_id = ? ORDER BY due_date ASC", (session["user_id"],))

    rows = c.fetchall()
    conn.close()

    today = datetime.now().date()
    due_soon = []
    annotated_rows = []

    for r in rows:
        row = dict(r)
        try:
            due_date = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
        except ValueError:
            continue

        days_left = (due_date - today).days
        is_due_today = days_left == 0
        is_due_tomorrow = days_left == 1
        is_past_due = due_date < today

        if is_due_today or is_due_tomorrow:
            due_soon.append(row)

        annotated_rows.append({
            "id": row["id"],
            "title": row["title"],
            "class": row["cl"],
            "due_date": row["due_date"],
            "notes": row["notes"],
            "is_past_due": is_past_due,
            "is_due_today": is_due_today,
            "is_due_tomorrow": is_due_tomorrow
        })

    return render_template("index.html", assignments=annotated_rows, due_soon=due_soon)


@app.route("/add", methods=["POST"])
def add():
    if "user_id" not in session:
        return redirect(url_for("login"))

    title = request.form["title"]
    cl = request.form["class"]
    due_date = request.form["due_date"]
    notes = request.form.get("notes", "")

    conn = get_connection()
    c = conn.cursor()

    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
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
    conn.close()
    return redirect(url_for("index"))


@app.route("/redirect/<int:id>")
def redirect_by_class(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()

    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        c.execute("SELECT cl FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
    else:
        c.execute("SELECT cl FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))

    row = c.fetchone()
    conn.close()

    if not row:
        return redirect(url_for("index"))

    class_name = (row["cl"] if isinstance(row, dict) else row["cl"]).strip().lower()

    class_links = {
        "math": "https://huhs.schoology.com/course/7898849902/materials",
        "english": "https://huhs.schoology.com/course/7898845132/materials",
        "ot": "https://huhs.schoology.com/course/7898892551/materials",
        "robotics": "https://huhs.schoology.com/course/8075753955/materials",
        "spanish": "https://huhs.schoology.com/course/7898849808/materials",
        "history": "https://huhs.schoology.com/course/7898868497/materials"
    }

    redirect_url = class_links.get(class_name, url_for("index"))
    return redirect(redirect_url)


@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit(id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    c = conn.cursor()

    if request.method == "POST":
        title = request.form["title"]
        cl = request.form["class"]
        due_date = request.form["due_date"]
        notes = request.form.get("notes", "")

        if DATABASE_URL and DATABASE_URL.startswith("postgres"):
            c.execute("""
                UPDATE assignments
                SET title = %s, cl = %s, due_date = %s, notes = %s
                WHERE id = %s AND user_id = %s
            """, (title, cl, due_date, notes, id, session["user_id"]))
        else:
            c.execute("""
                UPDATE assignments
                SET title = ?, cl = ?, due_date = ?, notes = ?
                WHERE id = ? AND user_id = ?
            """, (title, cl, due_date, notes, id, session["user_id"]))

        conn.commit()
        conn.close()
        return redirect(url_for("index"))
    else:
        if DATABASE_URL and DATABASE_URL.startswith("postgres"):
            c.execute("SELECT * FROM assignments WHERE id = %s AND user_id = %s", (id, session["user_id"]))
        else:
            c.execute("SELECT * FROM assignments WHERE id = ? AND user_id = ?", (id, session["user_id"]))

        assignment = c.fetchone()
        conn.close()

        if not assignment:
            flash("You don't have permission to edit this assignment.")
            return redirect(url_for("index"))

        return render_template("edit.html", assignment=assignment)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
