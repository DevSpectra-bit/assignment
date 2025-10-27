# app.py
from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import sqlite3
import os

app = Flask(__name__)

DB_NAME = "assignments.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    cl TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    notes TEXT,
                    link TEXT NOT NULL
                )''')
    conn.commit()
    conn.close()

init_db()

@app.route("/")
def index():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM assignments ORDER BY due_date ASC")
    rows = c.fetchall()
    conn.close()

    today = datetime.now().date()
    due_soon = []
    annotated_rows = []
    for r in rows:
        due_date = datetime.strptime(r[3], "%Y-%m-%d").date()  # <-- fixed index
        is_due_soon = (due_date - today).days < 1
        is_past_due = due_date < today
        if is_due_soon:
            due_soon.append(r)
        annotated_rows.append({
            "id": r[0],
            "title": r[1],
            "class": r[2],
            "due_date": r[3],
            "notes": r[4],
            "link": r[5],
            "is_past_due": is_past_due
        })

    return render_template("index.html", assignments=annotated_rows, due_soon=due_soon)

@app.route("/add", methods=["POST"])
def add():
    title = request.form["title"]
    cl = request.form["class"]
    due_date = request.form["due_date"]
    notes = request.form.get("notes", "")
    link = request.form["link"]
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO assignments (title, cl, due_date, notes, link) VALUES (?, ?, ?, ?, ?)",
              (title, cl, due_date, notes, link))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/redirect/<int:id>")
def redirect_by_class(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT cl FROM assignments WHERE id = ?", (id,))
    row = c.fetchone()
    conn.close()

    if not row:
        # fallback if no assignment found
        return redirect(url_for("index"))

    class_name = row[0].strip().lower()

    # map class names to URLs
    class_links = {
        "math": "https://huhs.schoology.com/course/7898849902/materials",
        "english": "https://huhs.schoology.com/course/7898845132/materials",
        "ot": "https://huhs.schoology.com/course/7898892551/materials",
        "robotics": "https://huhs.schoology.com/course/8075753955/materials",
        "spanish": "https://huhs.schoology.com/course/7898849808/materials",
        "history": "https://huhs.schoology.com/course/7898868497/materials"
    }

    # choose the correct URL or fallback
    redirect_url = class_links.get(class_name, url_for("index"))
    return redirect(redirect_url)


@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    if request.method == "POST":
        # update the assignment
        title = request.form["title"]
        cl = request.form["class"]
        due_date = request.form["due_date"]
        notes = request.form.get("notes", "")
        link = request.form["link"]
        c.execute("""
            UPDATE assignments
            SET title = ?, cl = ?, due_date = ?, notes = ?, link = ?
            WHERE id = ?
        """, (title, cl, due_date, notes, link, id))
        conn.commit()
        conn.close()
        return redirect(url_for("index"))
    else:
        # show the edit form
        c.execute("SELECT * FROM assignments WHERE id = ?", (id,))
        assignment = c.fetchone()
        conn.close()
        return render_template("edit.html", assignment=assignment)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
