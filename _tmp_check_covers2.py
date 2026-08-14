import sqlite3

conn = sqlite3.connect("file:/var/lib/kryten-webqueue/webqueue.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== Thumbnail-source movies (sample) ===")
cur = conn.execute(
    "SELECT friendly_token, title FROM catalog "
    "WHERE cover_art_source='thumbnail' AND duration_sec > 3600 "
    "ORDER BY title LIMIT 40"
)
for r in cur:
    print(f"{r['friendly_token']:12}  {r['title']}")

print("\n=== TMDB-matched movies (sample for comparison) ===")
cur2 = conn.execute(
    "SELECT friendly_token, title FROM catalog "
    "WHERE cover_art_source='tmdb' AND duration_sec > 3600 "
    "ORDER BY title LIMIT 10"
)
for r in cur2:
    print(f"{r['friendly_token']:12}  {r['title']}")

print("\n=== thumbnail-source items where title looks clean (year + normal title) ===")
cur3 = conn.execute(
    "SELECT COUNT(*) as cnt FROM catalog "
    "WHERE cover_art_source='thumbnail' AND duration_sec > 3600 "
    "AND title GLOB '* (19[0-9][0-9])' OR title GLOB '* (20[0-9][0-9])'"
)
row = cur3.fetchone()
print(f"Items with '(YYYY)' in title: {row['cnt']}")

conn.close()
