import calendar
import datetime
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import (
    app,
    db,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    determine_attendance_date,
    round_half_up,
    count_working_days,
    init_indexes,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_indexes()
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    # Seed initial test employees
    emps = [
        {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
            "created_at": datetime.datetime(2026, 1, 5, 4, 30, tzinfo=datetime.timezone.utc)
        },
        {
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-13",
            "created_at": datetime.datetime(2026, 7, 13, 4, 0, tzinfo=datetime.timezone.utc)
        },
        {
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
            "created_at": datetime.datetime(2026, 2, 1, 4, 30, tzinfo=datetime.timezone.utc)
        },
        {
            "emp_code": "EMP0004",
            "name": "Rohit Das",
            "email": "rohit@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
            "created_at": datetime.datetime(2026, 2, 1, 4, 31, tzinfo=datetime.timezone.utc)
        },
        {
            "emp_code": "EMP0005",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-03-10",
            "created_at": datetime.datetime(2026, 3, 10, 4, 30, tzinfo=datetime.timezone.utc)
        },
        {
            "emp_code": "EMP0006",
            "name": "Karan Bedi",
            "email": "karan@example.com",
            "department": "Support",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",  # Future joiner for July
            "created_at": datetime.datetime(2026, 3, 10, 4, 31, tzinfo=datetime.timezone.utc)
        }
    ]
    db.employees.insert_many(emps)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee():
    # Duplicate create
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })
    assert res.status_code == 409

    # Valid create
    res = client.post("/employees", json={
        "emp_code": "EMP0099",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-05-01"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0099"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    assert "_id" not in data
    assert "id" not in data

    # Invalid shift_start == shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP0100",
        "name": "Bad Shift",
        "email": "bad@example.com",
        "department": "QA",
        "shift_start": "10:00",
        "shift_end": "10:00",
        "joined_on": "2026-05-01"
    })
    assert res.status_code == 422


def test_validation_epoch_millis():
    # Seconds-looking value (< 100000000000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280
    })
    assert res.status_code == 422

    # Float value
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783310280000.5
    })
    assert res.status_code == 422

    # String value
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": "1783310280000"
    })
    assert res.status_code == 422


def test_list_employees():
    res = client.get("/employees?department=Sales&page=1&page_size=20")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP0003"
    assert data["items"][1]["emp_code"] == "EMP0004"


def test_punch_in_and_out():
    # Punch in for EMP0001
    # 2026-07-06 09:28:00 IST = 2026-07-06 03:58:00 UTC = 1783310280000 ms
    t_in_ms = 1783310280000
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": t_in_ms,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-06"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data

    # Duplicate punch-in on same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": t_in_ms + 1000,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Punch out for EMP0001
    # 2026-07-06 18:35:00 IST = 2026-07-06 13:05:00 UTC = 1783343100000 ms
    t_out_ms = 1783343100000
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": t_out_ms
    })
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["punch_out"] == t_out_ms
    assert data_out["work_hours"] == 9.12
    assert data_out["overtime_minutes"] == 0  # 5 min is under 30 min minimum
    assert data_out["half_day"] is False

    # Second punch-out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": t_out_ms
    })
    assert res_out_dup.status_code == 409


def test_punch_in_overnight():
    # Overnight shift EMP0005 (22:00 - 06:00)
    # Punch in at 2026-07-07 05:45:00 IST (before shift_end 06:00)
    # 05:45 IST on 7th belongs to shift that started 2026-07-06!
    # 2026-07-07 05:45:00 IST = 2026-07-07 00:15:00 UTC = 1783383300000 ms
    t_ms = 1783383300000
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": t_ms
    })
    assert res.status_code == 201
    data = res.json()
    assert data["date"] == "2026-07-06"


def test_r2_late_exact_boundaries():
    # Shift 09:30:
    # 09:40:00 -> 0
    # 09:40:01 -> 10
    # 10:15:59 -> 45
    t_940_00 = 1783311000000  # 2026-07-06 09:40:00 IST = 04:10:00 UTC
    t_940_01 = 1783311001000  # 2026-07-06 09:40:01 IST = 04:10:01 UTC
    t_1015_59 = 1783313159000 # 2026-07-06 10:15:59 IST = 04:45:59 UTC

    dt_940_00 = datetime.datetime.fromtimestamp(t_940_00 / 1000, tz=datetime.timezone.utc)
    dt_940_01 = datetime.datetime.fromtimestamp(t_940_01 / 1000, tz=datetime.timezone.utc)
    dt_1015_59 = datetime.datetime.fromtimestamp(t_1015_59 / 1000, tz=datetime.timezone.utc)

    assert compute_late_minutes(dt_940_00, "09:30", "2026-07-06") == 0
    assert compute_late_minutes(dt_940_01, "09:30", "2026-07-06") == 10
    assert compute_late_minutes(dt_1015_59, "09:30", "2026-07-06") == 45


def test_concurrency_simultaneous_punch_in():
    # 10 simultaneous punch-ins for the same emp_code on same date
    t_in = 1783310280000

    def do_punch():
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t_in})

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(do_punch) for _ in range(10)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 9


def test_concurrency_simultaneous_punch_out():
    # One punch in
    t_in = 1783310280000
    t_out = 1783343100000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t_in})

    def do_punch_out():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": t_out})

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(10)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 9


def test_regularize_patch():
    t_in = 1783312500000
    t_out = 1783345200000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out})

    # No change in regularize -> 422
    res_no_change = client.patch("/attendance/EMP0003/2026-07-06", json={
        "status": "PRESENT",
        "reason": "testing no change",
        "regularized_by": "admin"
    })
    assert res_no_change.status_code == 422

    # Setting ABSENT with punch_in -> 422
    res_bad_absent = client.patch("/attendance/EMP0003/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": t_in,
        "reason": "testing bad absent",
        "regularized_by": "admin"
    })
    assert res_bad_absent.status_code == 422

    # Valid regularization
    new_in = 1783310280000
    res = client.patch("/attendance/EMP0003/2026-07-06", json={
        "punch_in": new_in,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.7
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "punch_in" in h["changes"]
    assert h["changes"]["punch_in"]["from"] == t_in
    assert h["changes"]["punch_in"]["to"] == new_in
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0


def test_analytics_employee_monthly():
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 3, 58, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 6, 13, 5, tzinfo=datetime.timezone.utc),
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "WFH",
            "punch_in": datetime.datetime(2026, 7, 7, 4, 0, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 7, 7, 30, tzinfo=datetime.timezone.utc),
            "work_hours": 3.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": []
        },
        {
            "emp_code": "EMP0001",
            "date": "2026-07-08",
            "status": "LEAVE",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        }
    ])

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert data["present_days"] == 1.5  # 1.0 + 0.5
    assert data["leave_days"] == 1
    assert data["attendance_pct"] == round_half_up((1.5 / 23) * 100, 2)

    # Mid-month joiner EMP0002 joined on 2026-07-13
    # From 2026-07-13 to 2026-07-31: weekdays count is 15
    res2 = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["working_days"] == 15


def test_analytics_department_summary_r9_headcount():
    # In Support department:
    # EMP0005 joined 2026-03-10 (eligible for July 2026)
    # EMP0006 joined 2026-08-01 (not eligible for July 2026)
    # EMP0005 has 0 logs, but headcount for July 2026 must be 1!
    res = client.get("/analytics/departments/summary?month=2026-07&department=Support")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Support"
    assert items[0]["headcount"] == 1
    assert items[0]["present_days"] == 0.0
    assert items[0]["avg_work_hours"] is None


def test_analytics_late_leaderboard_competition_ranking():
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0003",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 5, 10, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 6, 13, 40, tzinfo=datetime.timezone.utc),
            "work_hours": 8.5,
            "late_minutes": 70,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0004",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 5, 10, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 6, 13, 40, tzinfo=datetime.timezone.utc),
            "work_hours": 8.5,
            "late_minutes": 70,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 4, 35, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 6, 13, 5, tzinfo=datetime.timezone.utc),
            "work_hours": 8.5,
            "late_minutes": 35,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        }
    ])

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
    # Both rank 1 are returned because rank <= limit (2)
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    # EMP0003 comes before EMP0004 alphabetically
    assert items[0]["emp_code"] == "EMP0003"
    assert items[1]["emp_code"] == "EMP0004"

    # With limit=3, rank 3 (EMP0001) is included
    res3 = client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
    items3 = res3.json()["items"]
    assert len(items3) == 3
    assert items3[2]["rank"] == 3
    assert items3[2]["emp_code"] == "EMP0001"


def test_analytics_department_trend():
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0003",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 4, 35, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 6, 13, 40, tzinfo=datetime.timezone.utc),
            "work_hours": 9.08,
            "late_minutes": 35,
            "overtime_minutes": 40,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0004",
            "date": "2026-07-07",
            "status": "WFH",
            "punch_in": datetime.datetime(2026, 7, 7, 4, 0, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 7, 7, 30, tzinfo=datetime.timezone.utc),
            "work_hours": 3.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": []
        }
    ])

    res = client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 7
    assert data["items"][0]["date"] == "2026-07-01"
    assert data["items"][6]["date"] == "2026-07-07"
    assert data["items"][5]["present_count"] == 1.0  # July 6
    assert data["items"][6]["present_count"] == 0.5  # July 7


def test_admin_explain():
    res = client.get("/admin/explain/attendance_list?emp_code=EMP0001&page=1&page_size=20")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data

    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["endpoint"] == "employee_monthly"
    assert "explain" in data2

    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200

    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200

    res5 = client.get("/admin/explain/department_trend?department=Sales&from=2026-07-01&to=2026-07-07")
    assert res5.status_code == 200


def test_employee_serialization_schema():
    res = client.get("/employees?department=Engineering")
    assert res.status_code == 200
    emp = res.json()["items"][0]
    expected_keys = {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert set(emp.keys()) == expected_keys
    assert "half_day" not in emp
    assert "late_minutes" not in emp
    assert "overtime_minutes" not in emp
    assert "history" not in emp


def test_edge_cases_regularize():
    # 404 unknown employee
    res = client.patch("/attendance/EMP9999/2026-07-06", json={"reason": "Correction", "regularized_by": "HR", "status": "PRESENT"})
    assert res.status_code == 404

    # 404 unknown attendance date
    res = client.patch("/attendance/EMP0001/2026-07-06", json={"reason": "Correction", "regularized_by": "HR", "status": "PRESENT"})
    assert res.status_code == 404

    # Seed an ABSENT record
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # 422 changing nothing
    res = client.patch("/attendance/EMP0001/2026-07-06", json={"reason": "Valid reason text", "regularized_by": "HR", "status": "ABSENT"})
    assert res.status_code == 422

    # 422 setting ABSENT with punch_in
    res = client.patch("/attendance/EMP0001/2026-07-06", json={"reason": "Valid reason text", "regularized_by": "HR", "status": "ABSENT", "punch_in": 1783312500000})
    assert res.status_code == 422


def test_edge_cases_department_trend():
    # 404 for unknown department
    res = client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 404

    # 422 for to < from
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-10&to=2026-07-01")
    assert res.status_code == 422

    # 422 for range > 92 days
    res = client.get("/analytics/departments/Sales/trend?from=2026-01-01&to=2026-05-01")
    assert res.status_code == 422


def test_edge_cases_admin_explain():
    # 422 missing emp_code or month
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res.status_code == 422

    # 422 missing month for department_summary
    res = client.get("/admin/explain/department_summary")
    assert res.status_code == 422

    # 422 missing department or dates for department_trend
    res = client.get("/admin/explain/department_trend")
    assert res.status_code == 422

