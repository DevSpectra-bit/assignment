# app.py
from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime, timedelta
import sqlite3
import os

app = Flask(__name__)

# Create database if it doesn’t exist
DB_NAME = "assignments.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    notes TEXT
                )''')
    conn.commit()
    conn.close()

init_db()

# --- ROUTES ---

@app.route("/")
def index():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM assignments ORDER BY due_date ASC")
    rows = c.fetchall()
    conn.close()

    # Check for due soon (within 1 day)
    today = datetime.now()
    due_soon = [r for r in rows if datetime.strptime(r[2], "%Y-%m-%d") - today < timedelta(days=1)]
    return render_template("index.html", assignments=rows, due_soon=due_soon)


@app.route("/add", methods=["POST"])
def add():
    title = request.form["title"]
    due_date = request.form["due_date"]
    notes = request.form.get("notes", "")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO assignments (title, due_date, notes) VALUES (?, ?, ?)",
              (title, due_date, notes))
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
