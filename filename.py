import sqlite3

conn = sqlite3.connect("assignments.db")
c = conn.cursor()
c.execute("PRAGMA table_info(assignments);")
print(c.fetchall())
conn.close()
