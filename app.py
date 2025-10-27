# app.py
from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
import os

app = Flask(__name__)

# Connect to PostgreSQL (get URL from Render environment variable)
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Database Setup ---
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS assignments (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    cl TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    notes TEXT
                )''')
    conn.commit()
    conn.close()

init_db()

# --- ROUTES ---

@app.route("/")
def index():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("SELECT * FROM assignments ORDER BY due_date ASC")
    rows = c.fetchall()
    conn.close()

    today = datetime.now().date()
    due_soon = []
    annotated_rows = []

    for r in rows:
        due_date = datetime.strptime(r["due_date"], "%Y-%m-%d").date()
        is_due_soon = (due_date - today).days <= 1
        is_past_due = due_date < today
        if is_due_soon:
            due_soon.append(r)
        annotated_rows.append({
            "id": r["id"],
            "title": r["title"],
            "class": r["cl"],
            "due_date": r["due_date"],
            "notes": r["notes"],
            "is_past_due": is_past_due
        })

    return render_template("index.html", assignments=annotated_rows, due_soon=due_soon)


@app.route("/add", methods=["POST"])
def add():
    title = request.form["title"]
    cl = request.form["class"]
    due_date = request.form["due_date"]
    notes = request.form.get("notes", "")

    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute(
        "INSERT INTO assignments (title, cl, due_date, notes) VALUES (%s, %s, %s, %s)",
        (title, cl, due_date, notes)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/redirect/<int:id>")
def redirect_by_class(id):
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("SELECT cl FROM assignments WHERE id = %s", (id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return redirect(url_for("index"))

    class_name = row["cl"].strip().lower()

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
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    c = conn.cursor()

    if request.method == "POST":
        title = request.form["title"]
        cl = request.form["class"]
        due_date = request.form["due_date"]
        notes = request.form.get("notes", "")

        c.execute("""
            UPDATE assignments
            SET title = %s, cl = %s, due_date = %s, notes = %s
            WHERE id = %s
        """, (title, cl, due_date, notes, id))
        conn.commit()
        conn.close()
        return redirect(url_for("index"))
    else:
        c.execute("SELECT * FROM assignments WHERE id = %s", (id,))
        assignment = c.fetchone()
        conn.close()
        return render_template("edit.html", assignment=assignment)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
