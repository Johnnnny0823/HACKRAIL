import sqlite3
import os

DB_PATH = './lost_found.db'

def update_db():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if transport_train column exists
    cursor.execute("PRAGMA table_info(delivery_requests)")
    columns = [info[1] for info in cursor.fetchall()]

    if "transport_train" not in columns:
        cursor.execute("ALTER TABLE delivery_requests ADD COLUMN transport_train TEXT")
        print("Added transport_train column.")
    else:
        print("transport_train column already exists.")

    if "train_staff" not in columns:
        cursor.execute("ALTER TABLE delivery_requests ADD COLUMN train_staff TEXT")
        print("Added train_staff column.")
    else:
        print("train_staff column already exists.")

    conn.commit()
    conn.close()
    print("Database update complete.")

if __name__ == "__main__":
    update_db()
