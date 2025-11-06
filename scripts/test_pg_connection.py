import psycopg2

conn = psycopg2.connect("postgresql://crawler:crawlerpass@127.0.0.1:5432/crawlerdb")
cur = conn.cursor()

cur.execute("SELECT current_database(), current_user;")
print("Connected to DB:", cur.fetchone())

cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public';")
print("Tables found:")
for row in cur.fetchall():
    print("-", row[0])

conn.close()
