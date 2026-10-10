import datetime
import pymongo

db = pymongo.MongoClient("mongodb://localhost:27017")["attendance_db"]

# 30-day range (July 1 to July 30 inclusive = 30 days)
from_str = "2026-07-01"
to_str = "2026-07-30"

from_dt = datetime.datetime.fromisoformat(from_str).replace(tzinfo=datetime.timezone.utc)
to_dt = datetime.datetime.fromisoformat(to_str).replace(tzinfo=datetime.timezone.utc)
to_next_dt = to_dt + datetime.timedelta(days=1)

pipeline = [
    {"$match": {"department": "Engineering"}},
    {"$limit": 1},
    {
        "$project": {
            "_id": 0,
            "date_dt": from_dt
        }
    },
    {
        "$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_next_dt]
            }
        }
    }
]

res = list(db.employees.aggregate(pipeline))
dates = [r["date_dt"].strftime("%Y-%m-%d") for r in res]

print(f"Generated {len(dates)} total dates:")
for d in dates:
    print(f"  {d}")

# Strict validation
has_duplicates = len(dates) != len(set(dates))
is_exact_30_days = len(dates) == 30

print("\n--- Validation Results ---")
print(f"Total count: {len(dates)} (Expected: 30) -> {'PASS' if is_exact_30_days else 'FAIL'}")
print(f"Duplicate check: {'FAIL (duplicates found)' if has_duplicates else 'PASS (zero duplicates)'}")
