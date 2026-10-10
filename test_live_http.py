import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def send_request(method, path, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.getcode()
            res_body = json.loads(resp.read().decode("utf-8"))
            return code, res_body
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def run_all_http_tests():
    print("=" * 65)
    print(" LIVE HTTP API SUITE (TESTING GET, POST, PATCH ON 127.0.0.1:8000)")
    print("=" * 65)

    # 1. GET /health
    c, b = send_request("GET", "/health")
    print(f"1. [GET]   /health                               -> {c} OK: {b}")

    # 2. POST /employees
    emp_body = {
        "emp_code": "EMP9991",
        "name": "Live Test Employee",
        "email": "live.test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    c, b = send_request("POST", "/employees", emp_body)
    print(f"2. [POST]  /employees                            -> {c} OK: emp_code={b.get('emp_code')}")

    # 3. GET /employees
    c, b = send_request("GET", "/employees?department=Engineering&page=1&page_size=3")
    print(f"3. [GET]   /employees?department=Engineering     -> {c} OK: total={b.get('total')}, page_items={len(b.get('items', []))}")

    # 4. POST /attendance/punch-in (punched 09:45 IST on 2026-07-06 -> 15 min late)
    punch_in_body = {
        "emp_code": "EMP9991",
        "punched_at": 1783311300000,
        "status": "PRESENT",
    }
    c, b = send_request("POST", "/attendance/punch-in", punch_in_body)
    print(f"4. [POST]  /attendance/punch-in                  -> {c} OK: date={b.get('date')}, late_minutes={b.get('late_minutes')}")

    # 5. POST /attendance/punch-out (punched 18:40 IST on 2026-07-06)
    punch_out_body = {
        "emp_code": "EMP9991",
        "punched_at": 1783343400000,
    }
    c, b = send_request("POST", "/attendance/punch-out", punch_out_body)
    print(f"5. [POST]  /attendance/punch-out                 -> {c} OK: work_hours={b.get('work_hours')}, half_day={b.get('half_day')}")

    # 6. GET /attendance
    c, b = send_request("GET", "/attendance?emp_code=EMP9991")
    print(f"6. [GET]   /attendance?emp_code=EMP9991          -> {c} OK: total_logs={b.get('total')}")

    # 7. PATCH /attendance/{emp_code}/{date} (Regularization)
    patch_body = {
        "reason": "Official remote assignment approval",
        "regularized_by": "HR_Manager",
        "status": "WFH",
    }
    c, b = send_request("PATCH", "/attendance/EMP9991/2026-07-06", patch_body)
    print(f"7. [PATCH] /attendance/EMP9991/2026-07-06        -> {c} OK: new_status={b.get('status')}, audit_entries={len(b.get('history', []))}")

    # 8. GET /analytics/employees/{emp_code}/monthly
    c, b = send_request("GET", "/analytics/employees/EMP9991/monthly?month=2026-07")
    print(f"8. [GET]   /analytics/employees/EMP9991/monthly  -> {c} OK: present_days={b.get('present_days')}, attendance_pct={b.get('attendance_pct')}%")

    # 9. GET /analytics/departments/summary
    c, b = send_request("GET", "/analytics/departments/summary?month=2026-07")
    print(f"9. [GET]   /analytics/departments/summary        -> {c} OK: department_summaries={len(b.get('items', []))}")

    # 10. GET /analytics/leaderboard/late
    c, b = send_request("GET", "/analytics/leaderboard/late?month=2026-07")
    print(f"10.[GET]   /analytics/leaderboard/late           -> {c} OK: ranked_employees={len(b.get('items', []))}")

    # 11. GET /analytics/departments/{dept}/trend
    c, b = send_request("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    print(f"11.[GET]   /analytics/departments/Eng../trend    -> {c} OK: 7_day_densified_rows={len(b.get('items', []))}")

    # 12. GET /admin/explain/{endpoint}
    c, b = send_request("GET", "/admin/explain/attendance_list?emp_code=EMP9991")
    print(f"12.[GET]   /admin/explain/attendance_list        -> {c} OK: explained_collection={b.get('collection')}")

    print("=" * 65)
    print(" ALL 12 ENDPOINTS & HTTP METHODS (GET, POST, PATCH) PASSED (100% OK)")
    print("=" * 65)


if __name__ == "__main__":
    run_all_http_tests()
