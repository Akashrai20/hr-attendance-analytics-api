# Employee Attendance & Analytics API

A high-performance FastAPI and MongoDB backend service implementing employee attendance tracking, manual regularization audit trails, and complex real-time aggregation analytics.

## Tech Stack
- **Python 3.11+**
- **FastAPI** (REST API, validation, error handling)
- **PyMongo** (MongoDB Driver, aggregation pipelines)
- **MongoDB 6.0+** (Aggregation framework, `$setWindowFields`, `$densify`, `$rank`, unique compound indexes)

---

## Setup & Running

### 1. Environment & Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Variables
Create or configure environment variables (defaults provided):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run the API Server
```bash
uvicorn app.main:app --port 8000 --reload
```

---

## Key Features & Business Rules Implementation

1. **Timezone & Truncation (R1)**:
   - Full IST (UTC+05:30) date determination.
   - Handles overnight shifts (e.g. 22:00 - 06:00) where punches before shift end belong to the preceding calendar day.
   - Instants truncated to whole seconds at the API boundary before storage.

2. **Accurate Derived Metrics (R2 - R5)**:
   - **Late Calculation (R2)**: 10-minute grace period with strict thresholding.
   - **Overtime Calculation (R3)**: 30-minute minimum threshold with multi-day shift end awareness.
   - **Work Hours (R4)**: Exact half-up rounding (`ROUND_HALF_UP`) via `decimal.Decimal`.
   - **Half-Day (R5)**: Triggered when rounded work hours < 4.50.

3. **Concurrency & Race Conditions**:
   - Unique compound index on `("emp_code", "date")` prevents duplicate daily records, returning 409 Conflict.
   - Atomic `find_one_and_update` on punch-out ensures single-winner closing of open records.
   - Optimistic concurrency control on regularization (`PATCH`) using history array length matching.

4. **Analytics Aggregation Pipelines**:
   - **Employee Monthly**: Aggregates presence (Mon-Fri only), working days from `joined_on`, and attendance rate.
   - **Department Summary**: Cross-collection `$lookup` grouping including 0-log employees (R9) and average work hours across records.
   - **Late Leaderboard**: Standard competition ranking (`$rank`: 1, 2, 2, 4) with ties handled and `limit` applied after ranking.
   - **Department Trend**: Calendar gap filling using `$densify` and 7-day moving averages with `$setWindowFields` inside MongoDB.

5. **Performance & Zero `COLLSCAN`**:
   - All endpoints backed by indexes, verified via `GET /admin/explain/{endpoint}` returning `IXSCAN` plans.

---

## Running Tests
```bash
pytest test_suite.py -v
```
