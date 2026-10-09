# DECISIONS.md

1. **Indexes.**
   - `employees`: unique on `emp_code` (uniqueness and fast lookups); compound on `(department, joined_on)` and `(joined_on)` (powers department listing, headcount filtering, trend `$lookup` index scans).
   - `attendance_logs`: unique compound on `(emp_code, date)` (guarantees one record per employee-day and race safety); compound on `(date: -1, emp_code: 1, status: 1)` (powers `/attendance` pagination without in-memory sort); compound on `(date: 1, late_minutes: 1)` (accelerates leaderboard pipeline); compound on `(emp_code: 1, punch_in: -1)` (fast open-punch lookup in punch-out).
   - *Rejected:* Standalone index on `status` due to low cardinality.

2. **Punch-in race.**
   Both concurrent requests compute the R1 attendance date and attempt `insert_one()`. The database unique index on `(emp_code, date)` guarantees atomic isolation: exactly one insert succeeds (201 Created), and the other fails with `DuplicateKeyError`, caught and returned as 409 Conflict.

3. **Leaderboard ties.**
   Using MongoDB `$rank` window function (standard competition ranking: 1, 2, 2, 4), tied employees receive equal rank. Filtering `rank <= limit` occurs after ranking; all tied employees at the cutoff are returned, ordered by `total_late_minutes` desc and `emp_code` asc.

4. **Zero-log headcount.**
   The pipeline roots on `employees` matching `joined_on <= month_end`, groups by department to establish full headcount, and `$lookup`s `attendance_logs`. Employees without logs remain in the grouping with headcount counted.

5. **100x scaling.**
   Pre-aggregate daily and monthly statistics into dedicated summary collections using change streams or background rollups, eliminating expensive real-time multi-collection `$lookup` joins over 10M+ raw attendance records.
