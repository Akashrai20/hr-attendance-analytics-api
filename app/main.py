"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Database Index Initialization
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at startup to guarantee IXSCAN."""
    # employees collection indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float | Decimal, decimals: int) -> float:
    """Exact half-up rounding (R8)."""
    d = Decimal(str(value))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime instant to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (aware or naive) to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date string (YYYY-MM-DD) in IST (R1).
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end belongs
    to the shift that started the previous day.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    is_overnight = is_overnight_shift(shift_start, shift_end)
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (end_h, end_m, 0):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if shift spans across midnight (shift_end <= shift_start)."""
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    return (eh_h, eh_m) <= (sh_h, sh_m)


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600s) after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(f"{attendance_date}T{sh:02d}:{sm:02d}:00").replace(tzinfo=IST)
    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_overtime(punch_out_dt: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30 (1800s); otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    eh, em = map(int, shift_end.split(":"))
    if is_overnight_shift(shift_start, shift_end):
        next_day = dt_date.fromisoformat(attendance_date) + timedelta(days=1)
        shift_end_dt = datetime.fromisoformat(f"{next_day.isoformat()}T{eh:02d}:{em:02d}:00").replace(tzinfo=IST)
    else:
        shift_end_dt = datetime.fromisoformat(f"{attendance_date}T{eh:02d}:{em:02d}:00").replace(tzinfo=IST)
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    d_sec = Decimal(diff_sec)
    return float((d_sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on: str, month: str) -> int:
    """
    R7: Count Monday-Friday working days in month for employee starting from joined_on.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    m_start = dt_date(year, m, 1)
    m_end = dt_date(year, m, num_days)

    emp_join = dt_date.fromisoformat(joined_on)
    if emp_join > m_end:
        return 0
    eff_start = max(emp_join, m_start)

    count = 0
    curr = eff_start
    while curr <= m_end:
        if curr.weekday() < 5:  # 0..4 are Monday..Friday
            count += 1
        curr += timedelta(days=1)
    return count


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB employee document strictly adhering to openapi.yaml Employee schema."""
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)

    created_at = d.get("created_at")
    if created_at is not None and isinstance(created_at, datetime):
        created_at = dt_to_epoch_ms(created_at)

    return {
        "emp_code": d.get("emp_code"),
        "name": d.get("name"),
        "email": d.get("email"),
        "department": d.get("department"),
        "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"),
        "joined_on": d.get("joined_on"),
        "created_at": created_at,
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document strictly adhering to openapi.yaml AttendanceRecord schema."""
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)

    punch_in = d.get("punch_in")
    if punch_in is not None and isinstance(punch_in, datetime):
        punch_in = dt_to_epoch_ms(punch_in)

    punch_out = d.get("punch_out")
    if punch_out is not None and isinstance(punch_out, datetime):
        punch_out = dt_to_epoch_ms(punch_out)

    if "history" in d and d["history"]:
        serialized_hist = []
        for h in d["history"]:
            h_copy = dict(h)
            if "at" in h_copy and isinstance(h_copy["at"], datetime):
                h_copy["at"] = dt_to_epoch_ms(h_copy["at"])
            if "changes" in h_copy and isinstance(h_copy["changes"], dict):
                changes_copy = {}
                for ch_field, ch_val in h_copy["changes"].items():
                    c_dict = dict(ch_val)
                    for k in ["from", "to"]:
                        if k in c_dict and isinstance(c_dict[k], datetime):
                            c_dict[k] = dt_to_epoch_ms(c_dict[k])
                    changes_copy[ch_field] = c_dict
                h_copy["changes"] = changes_copy
            serialized_hist.append(h_copy)
        serialized_history = serialized_hist
    else:
        serialized_history = []

    return {
        "emp_code": d.get("emp_code"),
        "date": d.get("date"),
        "status": d.get("status"),
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes", 0) if d.get("late_minutes") is not None else 0,
        "overtime_minutes": d.get("overtime_minutes", 0) if d.get("overtime_minutes") is not None else 0,
        "half_day": bool(d.get("half_day", False)) if d.get("half_day") is not None else False,
        "history": serialized_history,
    }


# Backwards compatibility alias
serialize_doc = serialize_attendance


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[
    int,
    Field(
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds instant"
    )
]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None


# --------------------------------------------------------------------------- #
# System & CRUD Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code constraint."""
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="shift_start must differ from shift_end")

    try:
        dt_date.fromisoformat(body.joined_on)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid joined_on date")

    now_utc = truncate_to_seconds(datetime.now(UTC))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending with accurate pagination and filtered count."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in (R1, R2), concurrency-safe against simultaneous punch-ins."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)
    else:
        ts_utc = truncate_to_seconds(datetime.now(UTC))

    att_date = determine_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out (R3-R5).
    Closes employee's most recent record whose punch_in <= punched_at.
    Thread/race safe (atomic update returns 409 on concurrent punch-outs).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punched_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punched_out_dt = truncate_to_seconds(datetime.now(UTC))

    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not candidate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in record found for this punch-out")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Attendance record already punched out")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    diff_sec = int((punched_out_dt - punch_in_dt).total_seconds())
    if diff_sec <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at must be strictly after punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punched_out_dt)
    overtime_min = compute_overtime(punched_out_dt, emp["shift_end"], emp["shift_start"], candidate["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punched_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record was concurrently updated")

    return serialize_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc with fast indexed querying."""
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code

    if date_from or date_to:
        date_q: Dict[str, Any] = {}
        if date_from:
            try:
                dt_date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date_from")
            date_q["$gte"] = date_from
        if date_to:
            try:
                dt_date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date_to")
            date_q["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be greater than date_to")
        q["date"] = date_q

    if status_filter is not None:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, appends audit trail to history, prevents race conditions.
    """
    try:
        dt_date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date in path")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found for that date")

    cur_status = record.get("status")
    cur_punch_in = record.get("punch_in")
    cur_punch_out = record.get("punch_out")
    cur_work_hours = record.get("work_hours")
    cur_late_minutes = record.get("late_minutes", 0)
    cur_overtime_minutes = record.get("overtime_minutes", 0)
    cur_half_day = record.get("half_day", False)
    cur_history = record.get("history") or []

    if cur_punch_in is not None and cur_punch_in.tzinfo is None:
        cur_punch_in = cur_punch_in.replace(tzinfo=UTC)
    if cur_punch_out is not None and cur_punch_out.tzinfo is None:
        cur_punch_out = cur_punch_out.replace(tzinfo=UTC)

    target_status = body.status if body.status is not None else cur_status

    # Validate ABSENT/LEAVE rules
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in and punch_out cannot be supplied for ABSENT/LEAVE")
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            target_punch_in = cur_punch_in

        if target_punch_in is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires a punch_in instant")

        # punch_in must stay on the record's attendance date according to R1
        computed_date = determine_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date_str:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in does not match the attendance date")

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            target_punch_out = cur_punch_out

        if target_punch_out is not None:
            diff_sec = int((target_punch_out - target_punch_in).total_seconds())
            if diff_sec <= 0:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(target_punch_out, emp["shift_end"], emp["shift_start"], date_str)
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_str)

    # Build changes dictionary
    changes: Dict[str, Any] = {}
    if target_status != cur_status:
        changes["status"] = {"from": cur_status, "to": target_status}
    if target_punch_in != cur_punch_in:
        changes["punch_in"] = {"from": cur_punch_in, "to": target_punch_in}
    if target_punch_out != cur_punch_out:
        changes["punch_out"] = {"from": cur_punch_out, "to": target_punch_out}
    if target_work_hours != cur_work_hours:
        changes["work_hours"] = {"from": cur_work_hours, "to": target_work_hours}
    if target_late_minutes != cur_late_minutes:
        changes["late_minutes"] = {"from": cur_late_minutes, "to": target_late_minutes}
    if target_overtime_minutes != cur_overtime_minutes:
        changes["overtime_minutes"] = {"from": cur_overtime_minutes, "to": target_overtime_minutes}
    if target_half_day != cur_half_day:
        changes["half_day"] = {"from": cur_half_day, "to": target_half_day}

    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Regularization request produced no changes")

    now_utc = truncate_to_seconds(datetime.now(UTC))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency matching history array length
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(cur_history)}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent update detected")

    return serialize_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$project": {
                "status": 1,
                "date": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "date": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                                "timezone": "+05:30"
                            }
                        },
                        "timezone": "+05:30"
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                }
            }
        }
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = round_half_up(agg["present_days"], 2)
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up((Decimal(str(present_days)) / Decimal(working_days)) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "date": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                            "dayOfWeek": {
                                "$dayOfWeek": {
                                    "date": {
                                        "$dateFromString": {
                                            "dateString": "$date",
                                            "format": "%Y-%m-%d",
                                            "timezone": "+05:30"
                                        }
                                    },
                                    "timezone": "+05:30"
                                }
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": ["$$l.dayOfWeek", [2, 3, 4, 5, 6]]}
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list.work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month including employees with 0 logs (R9)."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": round_half_up(doc["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


def build_late_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month using standard competition ranking ($rank)."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> List[Dict[str, Any]]:
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=UTC)
    to_dt = datetime.fromisoformat(to_str).replace(tzinfo=UTC)
    to_next_dt = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "dept_name": "$department",
                "date_dt": from_dt
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "partitionByFields": ["dept_name"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_next_dt]
                }
            }
        },
        {
            "$project": {
                "dept_name": 1,
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_dt",
                        "timezone": "UTC"
                    }
                },
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "date": "$date_dt",
                        "timezone": "UTC"
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date_str", "dept": "$dept_name"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$d"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "dept_emps"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date_str", "codes": "$dept_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$eq": ["$date", "$$d"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs"
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {"$size": "$dept_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap filling ($densify) and 7-day moving average ($setWindowFields)."""
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    try:
        f_date = dt_date.fromisoformat(from_date)
        t_date = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format")

    if t_date < f_date:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to date cannot be earlier than from date")

    days_diff = (t_date - f_date).days + 1
    if days_diff > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for r in raw_items:
        att_rate = r.get("attendance_rate")
        if att_rate is not None:
            att_rate = round_half_up(att_rate, 4)
        m_avg = r.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(m_avg, 4)
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Executes explain with executionStats for the given endpoint's main query."""
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q: Dict[str, Any] = {}
            if date_from:
                try:
                    dt_date.fromisoformat(date_from)
                except ValueError:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date_from")
                date_q["$gte"] = date_from
            if date_to:
                try:
                    dt_date.fromisoformat(date_to)
                except ValueError:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date_to")
                date_q["$lte"] = date_to
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be greater than date_to")
            q["date"] = date_q
        if status_filter:
            q["status"] = status_filter
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        explain_data = cursor.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_data
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code required")
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="valid month required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_employee_monthly_pipeline(emp_code, f"{month}-01", f"{month}-{last_day:02d}")
        explain_data = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_data
        }

    elif endpoint == "department_summary":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="valid month required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_department_summary_pipeline(f"{month}-01", f"{month}-{last_day:02d}", department)
        explain_data = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_data
        }

    elif endpoint == "late_leaderboard":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="valid month required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_late_leaderboard_pipeline(f"{month}-01", f"{month}-{last_day:02d}", limit, department)
        explain_data = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_data
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department required")
        if not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="from and to dates required")
        try:
            f_date = dt_date.fromisoformat(from_date)
            t_date = dt_date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format")
        if t_date < f_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to date cannot be earlier than from date")
        if (t_date - f_date).days + 1 > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_data = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_data
        }
