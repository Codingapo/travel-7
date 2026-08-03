import datetime
import os
import random
import shutil
import sqlite3

from werkzeug.security import generate_password_hash

from config import DB_PATH, BACKUPS_DIR


DALANI_PACKAGES = [
    ("Cape Town", "Cape Town", 12900, 4, "Embarking on a journey to Cape Town ignites a sense of adventure, promising home bound travellers an unforgettable experience filled with diverse landscapes, vibrant culture, and endless opportunities for exploration. From the iconic Table Mountain views to the pristine Atlantic coastline, this package offers a perfect blend of urban sophistication and natural wonder.", "Available", "Africa", "https://images.unsplash.com/photo-1580619305218-8423a7ef79b4?q=80&w=1400&auto=format&fit=crop"),
    ("Zanzibar, Jambiani", "Zanzibar", 17500, 4, "Embark on a South African adventure to Jambiani, a hidden gem on the southeastern coast of Zanzibar. Known for its serene beaches, cultural charm, and laid-back atmosphere, Jambiani invites you to experience a slice of paradise. Enjoy turquoise waters, traditional dhow sailing, and the authentic spice-island hospitality that makes Zanzibar unique.", "Available", "Africa", "https://images.unsplash.com/photo-1586861635167-e5223aadc9fe?q=80&w=1400&auto=format&fit=crop"),
    ("Zanzibar, Nungwi", "Zanzibar", 22500, 4, "Embark on an unforgettable journey from South Africa to the tropical haven of Nungwi in Zanzibar. Nestled on the northern tip of the island, Nungwi is a breathtaking destination, pristine beaches, crystal-clear waters, & a vibrant local culture that promises an enriching escape. This package features premium beachfront accommodation and access to the best diving spots.", "Available", "Africa", "https://images.unsplash.com/photo-1519046904884-53103b34b206?q=80&w=1400&auto=format&fit=crop"),
    ("Zanzibar, Paje", "Zanzibar", 23000, 4, "Embark on an extraordinary journey from South Africa to Paje, Zanzibar, a coastal haven on the southeast coast of Zanzibar. Known for its pristine beaches, water sports allure, and vibrant atmosphere, Paje promises an unforgettable escape. Perfect for kite-surfers and sun-seekers alike, with world-class resorts and a lively night scene.", "Unavailable", "Africa", "https://images.unsplash.com/photo-1537996194471-e657df975ab4?q=80&w=1400&auto=format&fit=crop"),
    ("Namibia, Swakopmund", "Namibia", 17900, 4, "Our Namibia Swakopmund Travel Package invites you to discover the hidden gems of this coastal jewel. Dive into the heart of Swakopmund, where German colonial charm intertwines with African vibrancy. Experience the thrill of the desert meeting the ocean, with activities ranging from sandboarding to scenic coastal flights.", "Available", "Africa", "https://images.unsplash.com/photo-1547127796-06bb04e4b315?q=80&w=1400&auto=format&fit=crop"),
    ("Zambia, Livingston", "Zambia", 26900, 4, "Zambia, is a destination renowned for its natural beauty, rich history, and adventurous spirit. Named after the famous explorer David Livingstone, who first set eyes on the awe-inspiring Victoria Falls, Livingstone is a gateway to one of the most spectacular natural wonders of the world. Witness the smoke that thunders and explore the rich wildlife of the Zambezi.", "Available", "Africa", "https://images.unsplash.com/photo-1547471080-7cc2caa01a7e?q=80&w=1400&auto=format&fit=crop"),
    ("Dubai (4 Star)", "Dubai", 24900, 5, "Embark on a journey from South Africa to Dubai, a dazzling metropolis known for its opulence, modern marvels, and cultural richness. Indulge in the grandeur of Dubai, where luxury meets adventure in the heart of the Arabian desert. Visit the Burj Khalifa, explore the gold souks, and experience the futuristic architecture of this global hub.", "Available", "Middle East", "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?q=80&w=1400&auto=format&fit=crop"),
    ("Dubai (5 Star)", "Dubai", 29900, 5, "Embark on a journey to Dubai, where extravagance and adventure await. Dubai invites you to a world of unparalleled opulence. Book your trip now and immerse yourself in the grandeur of this dynamic city. This premium package includes stays in the world's most luxurious hotels and exclusive access to desert safaris.", "Available", "Middle East", "https://images.unsplash.com/photo-1518684079-3c830dcef090?q=80&w=1400&auto=format&fit=crop"),
    ("Bali, Seminyak", "Bali", 28900, 7, "Embark on an enchanting journey from South Africa to Seminyak, a vibrant paradise nestled on the shores of Bali. Known for its exotic charm, stunning beaches, and lively atmosphere, Seminyak beckons you to experience the best of Indonesian hospitality. Discover high-end boutiques, world-class dining, and breathtaking sunset views from coastal clubs.", "Available", "Asia", "https://images.unsplash.com/photo-1537996194471-e657df975ab4?q=80&w=1400&auto=format&fit=crop"),
    ("Bali, Seminyak & Ubud", "Bali", 30900, 7, "Embark on a Luxurious Bali Escape – 4 Nights of Coastal Bliss in Seminyak, Followed by 3 Nights of Tranquility in a Balinese Villa in Ubud – Your Exotic Getaway from South Africa! This dual-experience package lets you enjoy the energetic beach life of Seminyak and the spiritual, lush green heart of Ubud's rice terraces.", "Available", "Asia", "https://images.unsplash.com/photo-1552674605-db6ffd4facb5?q=80&w=1400&auto=format&fit=crop"),
    ("Singapore & Bali", "Singapore/Bali", 35900, 7, "Embark on an unforgettable journey to Bali and Singapore, where ancient traditions blend seamlessly with modern marvels. This dynamic duo offers a diverse array of experiences, from tranquil beach retreats to bustling urban adventures. Explore Singapore's Gardens by the Bay before heading to the spiritual temples and beaches of Bali.", "Unavailable", "Asia", "https://images.unsplash.com/photo-1525625293386-3f8f99389edd?q=80&w=1400&auto=format&fit=crop"),
    ("Thailand; Phuket", "Thailand", 26900, 7, "Welcome to the enchanting paradise of Phuket, Thailand, where a kaleidoscope of wonders awaits at every turn. This captivating destination beckons holidaymakers with its magnetic blend of culture, cuisine, adventure, and excitement. From the crystal waters of Patong Beach to the Big Buddha views, Phuket is an island dream come true.", "Available", "Asia", "https://images.unsplash.com/photo-1589308078059-be1415eab4c3?q=80&w=1400&auto=format&fit=crop"),
    ("Thailand; Phuket & Bangkok", "Thailand", 30900, 7, "Get ready to embark on an unforgettable journey to the captivating destinations of Phuket and Bangkok, Thailand. As you prepare to explore these vibrant cities, allow us to share why we love them and why we’re certain you’ll fall in love too. Experience the bustling street life of Bangkok and the serene tropical beauty of Phuket's coastline.", "Available", "Asia", "https://images.unsplash.com/photo-1552465011-b4e21bf6e79a?q=80&w=1400&auto=format&fit=crop"),
    ("Mauritius", "Mauritius", 25900, 5, "Mauritius, a captivating destination that transcends the ordinary, weaves a tapestry of enchantment, blending pristine beaches and a harmonious blend of cultures. Its warmth and genuine friendliness make visitors feel like cherished members of a vibrant community. Enjoy world-class resorts, coral reefs, and the unique flora of the Black River Gorges.", "Available", "Africa", "https://images.unsplash.com/photo-1514282401047-d79a71a590e8?q=80&w=1400&auto=format&fit=crop"),
]


FIRST_NAMES = ["Anele", "Bokang", "Chipo", "Dineo", "Esethu", "Fikile", "Gugu", "Hlompho", "Imani", "Jabu", "Khumo", "Lwandle", "Mpho", "Naledi", "Olwethu", "Palesa", "Que", "Rethabile", "Sinethemba", "Thando"]
LAST_NAMES = ["Mokoena", "Nkosi", "Pillay", "Ndlovu", "Molefe", "Khumalo", "Dlamini", "Zulu", "Mabena", "Sibanda", "Naidoo", "van Wyk", "Botha", "Adams", "Meyer"]


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_review_summary(source: str = "google"):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT source, average_rating, total_reviews, updated_at
           FROM Review_Summary
           WHERE source = ?""",
        (source,),
    )
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_review_summary(source: str, average_rating: float, total_reviews: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO Review_Summary (source, average_rating, total_reviews, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(source) DO UPDATE SET
             average_rating=excluded.average_rating,
             total_reviews=excluded.total_reviews,
             updated_at=CURRENT_TIMESTAMP""",
        (source, float(average_rating), int(total_reviews)),
    )
    conn.commit()
    conn.close()


def _ensure_column(c, table_name, column_name, sql_type_with_default):
    c.execute(f"PRAGMA table_info({table_name})")
    existing = {row["name"] for row in c.fetchall()}
    if column_name not in existing:
        c.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type_with_default}")


def _seed_packages(c):
    # First, force update all images based on the hardcoded list
    for pkg in DALANI_PACKAGES:
        c.execute(
            """UPDATE Packages
               SET image_url=?
               WHERE package_name=?""",
            (pkg[7], pkg[0]),
        )
    
    canonical_names = []
    for pkg in DALANI_PACKAGES:
        canonical_names.append(pkg[0])
        c.execute("SELECT package_id FROM Packages WHERE package_name=?", (pkg[0],))
        row = c.fetchone()
        if row:
            c.execute(
                """UPDATE Packages
                   SET destination=?, price=?, duration=?, description=?, availability_status=?, season_category=?, image_url=?
                   WHERE package_id=?""",
                (pkg[1], pkg[2], pkg[3], pkg[4], pkg[5], pkg[6], pkg[7], row["package_id"]),
            )
        else:
            c.execute(
                """INSERT INTO Packages
                   (package_name, destination, price, duration, description, availability_status, season_category, image_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                pkg,
            )

    c.execute("SELECT package_name FROM Packages")
    all_names = [r["package_name"] for r in c.fetchall()]
    extras = [n for n in all_names if n not in canonical_names]
    if extras:
        placeholders = ",".join("?" for _ in extras)
        c.execute(
            f"""SELECT COUNT(*) as cnt
                FROM Bookings b JOIN Packages p ON b.package_id = p.package_id
                WHERE p.package_name IN ({placeholders})""",
            extras,
        )
        used = int(c.fetchone()["cnt"] or 0)
        if used == 0:
            c.execute(f"DELETE FROM Packages WHERE package_name IN ({placeholders})", extras)


def _seed_scaled_demo_data(conn, c):
    # Clear existing data as requested - Commented out to prevent data loss on every restart
    # c.execute("DELETE FROM Bookings")
    # c.execute("DELETE FROM Customers")
    # c.execute("DELETE FROM Users WHERE role='customer'")
    # conn.commit()
    return


def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        """CREATE TABLE IF NOT EXISTS Users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT,
            full_name TEXT,
            email TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Customers (
            customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            payment_method TEXT,
            card_number TEXT,
            card_expiry TEXT,
            bank_name TEXT,
            account_number TEXT,
            preferences TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES Users(user_id)
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Packages (
            package_id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT,
            destination TEXT,
            price REAL,
            duration INTEGER,
            description TEXT,
            availability_status TEXT,
            season_category TEXT,
            image_url TEXT
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Bookings (
            booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            package_id INTEGER,
            booking_date DATE,
            travel_date DATE,
            number_of_travelers INTEGER,
            total_amount REAL,
            status TEXT,
            payment_method TEXT DEFAULT 'unknown',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES Customers(customer_id),
            FOREIGN KEY(package_id) REFERENCES Packages(package_id)
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            reviewer_name TEXT,
            review_text TEXT,
            rating INTEGER,
            sentiment_score REAL,
            review_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Analytics_Log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date DATE,
            prediction_value REAL,
            anomaly_flag BOOLEAN,
            alert_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT,
            description TEXT,
            severity TEXT,
            status TEXT,
            detected_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Forecasts (
            forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_date DATE,
            period_start DATE,
            period_end DATE,
            predicted_demand REAL,
            confidence REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date DATE,
            end_date DATE,
            content TEXT,
            total_bookings INTEGER,
            total_revenue REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS Review_Summary (
            source TEXT PRIMARY KEY,
            average_rating REAL,
            total_reviews INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    _ensure_column(c, "Packages", "image_url", "TEXT")
    _ensure_column(c, "Packages", "type", "TEXT DEFAULT 'standard'")
    _ensure_column(c, "Bookings", "payment_method", "TEXT DEFAULT 'unknown'")
    _ensure_column(c, "Bookings", "revenue", "REAL")
    _ensure_column(c, "Reviews", "reviewer_name", "TEXT DEFAULT 'Google Reviewer'")
    _ensure_column(c, "Users", "contact_number", "TEXT")
    _ensure_column(c, "Users", "account_status", "TEXT DEFAULT 'active'")
    # Create Sessions table for persistent logins
    c.execute(
        """CREATE TABLE IF NOT EXISTS Sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id)
        )"""
    )

    # Add missing columns to Customers table
    _ensure_column(c, "Customers", "address", "TEXT")
    _ensure_column(c, "Customers", "payment_method", "TEXT")
    _ensure_column(c, "Customers", "card_number", "TEXT")
    _ensure_column(c, "Customers", "card_expiry", "TEXT")
    _ensure_column(c, "Customers", "bank_name", "TEXT")
    _ensure_column(c, "Customers", "account_number", "TEXT")
    _ensure_column(c, "Customers", "travel_preferences", "TEXT")

    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_date ON Bookings(booking_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_customer ON Bookings(customer_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_package ON Bookings(package_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON Bookings(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_payment_method ON Bookings(payment_method)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON Alerts(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_source ON Reviews(source)")

    c.execute("SELECT * FROM Users WHERE username='admin'")
    if not c.fetchone():
        c.execute(
            """INSERT INTO Users (username, password_hash, role, full_name, email)
               VALUES (?, ?, ?, ?, ?)""",
            ("admin", generate_password_hash("TravelIntel2026!"), "admin", "System Admin", "admin@travelintel.ai"),
        )

    _seed_packages(c)
    _seed_scaled_demo_data(conn, c)

    c.execute("SELECT COUNT(*) as cnt FROM Reviews")
    review_count = int(c.fetchone()["cnt"] or 0)
    if review_count < 138:
        first_names = ["Lerato", "Sipho", "Anele", "Naledi", "Thabo", "Zanele", "Musa", "Buhle", "Jabu", "Nomsa", "Alexander", "Sarah", "Michael", "Elena", "Dmitry", "Fatima", "Chen", "Yuki", "Amara", "Kofi"]
        last_initials = ["N.", "M.", "P.", "B.", "K.", "S.", "T.", "G.", "X.", "R.", "W.", "H.", "L.", "V.", "O.", "A.", "J.", "C.", "D.", "F."]
        
        snippets = [
            "Great service and smooth booking process.",
            "Loved the package planning and communication.",
            "Helpful consultants and clear itinerary details.",
            "Good trip overall with timely updates.",
            "Fantastic support from inquiry to return.",
            "Value for money and friendly team.",
            "Exceptional attention to detail on our trip.",
            "Highly professional staff and great deals.",
            "Best travel agency we've used so far.",
            "Seamless experience from start to finish."
        ]
        payload = []
        today = datetime.date.today()
        for i in range(138 - review_count):
            review_date = today - datetime.timedelta(days=(i % 180))
            payload.append((
                "google",
                f"{random.choice(first_names)} {random.choice(last_initials)}",
                snippets[i % len(snippets)] + (f" (Ref: #{2000+i})" if i > 20 else ""),
                4 if i % 5 else 5,
                0.7 if i % 3 else 0.9,
                review_date.isoformat(),
            ))
        c.executemany(
            """INSERT INTO Reviews (source, reviewer_name, review_text, rating, sentiment_score, review_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            payload,
        )

    c.execute("SELECT source FROM Review_Summary WHERE source='google'")
    if not c.fetchone():
        c.execute(
            """INSERT INTO Review_Summary (source, average_rating, total_reviews)
               VALUES (?, ?, ?)""",
            ("google", 4.6, 138),
        )

    conn.commit()
    conn.close()


def backup_database():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUPS_DIR, f"travelintel_backup_{timestamp}.db")
    shutil.copy2(DB_PATH, backup_file)
    print(f"Database backed up to {backup_file}")

    now = datetime.datetime.now()
    for filename in os.listdir(BACKUPS_DIR):
        file_path = os.path.join(BACKUPS_DIR, filename)
        if os.path.isfile(file_path):
            file_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
            if (now - file_time).days > 7:
                os.remove(file_path)
                print(f"Removed old backup: {file_path}")


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
