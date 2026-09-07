"""Standalone one-time restore of the 72-booking demo dataset.

This script is intentionally outside the FastAPI application. It does not add
any restore route, restore flag, or restore table to the database.

It uses the packages/customers already present in the database. It never creates
or changes packages. It refuses to run when bookings already exist.
"""
from __future__ import annotations
import argparse, datetime as dt, random, sqlite3
from pathlib import Path

TARGET_BOOKINGS = 72
TARGET_TRAVELLERS = 173
TARGET_REVENUE = 3_816_500.00

NAMES = ["James Anderson","Sarah Williams","Michael Brown","Emily Davis","Daniel Wilson","Olivia Taylor","David Thomas","Sophia Moore","Robert Martin","Ava Jackson","William White","Mia Harris","Joseph Clark","Isabella Lewis","Thomas Young","Amelia Walker","Charles Hall","Charlotte Allen","Christopher King","Harper Wright"]
CITIES = ["Johannesburg","Pretoria","Polokwane","Cape Town","Durban","Gqeberha","Bloemfontein"]


def clean_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="instance/travelintel.db")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    db = Path(args.db).resolve()
    if not db.exists():
        print(f"Database not found: {db}")
        return 1
    if not args.yes:
        print("This will add 72 bookings ONLY if the Bookings table is empty.")
        if input("Type RESTORE to continue: ").strip() != "RESTORE":
            print("Cancelled."); return 0
    rng = random.Random(20260815)
    conn = clean_db(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"Bookings","Customers","Packages"}
        if not required.issubset(tables):
            raise RuntimeError("Database is missing one or more required tables.")
        count = conn.execute("SELECT COUNT(*) FROM Bookings").fetchone()[0]
        if count:
            conn.rollback(); print(f"Skipped: database already has {count} bookings."); return 0
        customers = conn.execute("SELECT customer_id,name,email,phone FROM Customers ORDER BY customer_id").fetchall()
        packages = conn.execute("SELECT package_id,package_name,destination,price FROM Packages ORDER BY package_id").fetchall()
        if not customers: raise RuntimeError("No existing customers. Create customers first; this script does not create them.")
        if not packages: raise RuntimeError("No existing packages. Add packages first; this script does not create them.")

        counts=[1]*TARGET_BOOKINGS
        remaining=TARGET_TRAVELLERS-TARGET_BOOKINGS
        while remaining:
            eligible=[i for i,n in enumerate(counts) if n<5]
            if not eligible: raise RuntimeError("Could not distribute travellers.")
            counts[rng.choice(eligible)] += 1; remaining -= 1

        today=dt.date.today(); start=today-dt.timedelta(days=180)
        raw=[]
        for i in range(TARGET_BOOKINGS):
            customer=rng.choice(customers); package=rng.choice(packages)
            booking_date=start+dt.timedelta(days=rng.randint(0,180))
            travel_date=booking_date+dt.timedelta(days=rng.randint(7,90))
            travellers=counts[i]
            amount=round(max(float(package["price"] or 0),15000.0)*travellers,2)
            raw.append((customer["customer_id"],package["package_id"],booking_date.isoformat(),travel_date.isoformat(),travellers,amount))
        scale=TARGET_REVENUE/sum(r[-1] for r in raw)
        amounts=[round(r[-1]*scale,2) for r in raw]
        amounts[-1]=round(amounts[-1]+TARGET_REVENUE-sum(amounts),2)
        for r,amount in zip(raw,amounts):
            conn.execute("""INSERT INTO Bookings
                (customer_id,package_id,booking_date,travel_date,number_of_travelers,total_amount,status,payment_method,created_at,revenue)
                VALUES (?,?,?,?,?,?,?,'bank_transfer',CURRENT_TIMESTAMP,?)""",
                (*r[:5],amount,'confirmed',amount))
        final_b=conn.execute("SELECT COUNT(*) FROM Bookings").fetchone()[0]
        final_t=conn.execute("SELECT COALESCE(SUM(number_of_travelers),0) FROM Bookings WHERE status!='cancelled'").fetchone()[0]
        final_r=conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM Bookings WHERE status!='cancelled'").fetchone()[0]
        if final_b != TARGET_BOOKINGS or final_t != TARGET_TRAVELLERS or abs(final_r-TARGET_REVENUE)>0.01:
            raise RuntimeError(f"Verification failed: bookings={final_b}, travellers={final_t}, revenue={final_r}")
        conn.commit(); print(f"RESTORE COMPLETE: {final_b} bookings, {final_t} travellers, R{final_r:,.2f}"); return 0
    except Exception as exc:
        conn.rollback(); print(f"RESTORE FAILED — rolled back: {exc}"); return 1
    finally: conn.close()

if __name__ == "__main__": raise SystemExit(main())
