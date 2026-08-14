import sqlite3

DB = "/var/lib/kryten-webqueue/webqueue.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# Check specific item
cur = conn.execute(
    "SELECT friendly_token, title, cover_art_path, cover_art_source FROM catalog WHERE friendly_token='0A9oGeit2'"
)
print("=== Deathsport item ===")
for r in cur:
    print(dict(r))

# Overall cover art stats
print("\n=== Cover art stats ===")
cur2 = conn.execute(
    "SELECT cover_art_source, COUNT(*) as cnt FROM catalog GROUP BY cover_art_source ORDER BY cnt DESC"
)
for r in cur2:
    print(dict(r))

# How many items have no cover art at all?
cur3 = conn.execute(
    "SELECT COUNT(*) as cnt FROM catalog WHERE cover_art_path IS NULL OR cover_art_path = ''"
)
row = cur3.fetchone()
print(f"\nItems with no cover art path: {row['cnt']}")

# Sample of items with no cover art (movies - duration > 3600)
print("\n=== Sample no-art items (movies) ===")
cur4 = conn.execute(
    "SELECT friendly_token, title, cover_art_source FROM catalog WHERE (cover_art_path IS NULL OR cover_art_path = '') AND duration_sec > 3600 LIMIT 20"
)
for r in cur4:
    print(dict(r))

conn.close()
