import sqlite3

conn = sqlite3.connect('/Users/joshmac/Desktop/teadu/templates/lost_found.db')
cursor = conn.cursor()

try:
    cursor.execute("ALTER TABLE lost_items ADD COLUMN train_type TEXT")
    print("Added train_type")
except Exception as e:
    print("Error:", e)

try:
    cursor.execute("ALTER TABLE lost_items ADD COLUMN train_number TEXT")
    print("Added train_number")
except Exception as e:
    print("Error:", e)
    
try:
    cursor.execute("ALTER TABLE lost_items ADD COLUMN seat_number TEXT")
    print("Added seat_number")
except Exception as e:
    print("Error:", e)

conn.commit()
conn.close()
print("Database update complete.")
