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
                    notes TEXT
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
            "is_past_due": is_past_due
        })

    return render_template("index.html", assignments=annotated_rows, due_soon=due_soon)

@app.route("/add", methods=["POST"])
def add():
    title = request.form["title"]
    cl = request.form["class"]
    due_date = request.form["due_date"]
    notes = request.form.get("notes", "")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO assignments (title, cl, due_date, notes) VALUES (?, ?, ?, ?)",
              (title, cl, due_date, notes))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/delete/<int:id>")
def delete(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM assignments WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
