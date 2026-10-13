"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Conventions
-----------
* Every instant in the API is an integer in epoch MILLISECONDS; MongoDB stores UTC datetimes.
  The client is tz-aware, so datetimes read back are UTC-aware. Instants are truncated to
  whole seconds before any calculation, and the truncated value is stored and returned.
* Calendar dates are YYYY-MM-DD (IST); months are YYYY-MM; shift times are HH:MM (IST).
* Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed by the
  API and stored; analytics read the stored values.
* _id is internal and never appears in a request or response.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # does not override real environment variables
log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

LATE_GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000

ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    specs = [
        (db.employees, [("emp_code", ASCENDING)], {"unique": True, "name": "uq_emp_code"}),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp"}),
        (db.employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined"}),
        (db.employees, [("joined_on", ASCENDING)], {"name": "joined"}),
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)],
         {"unique": True, "name": "uq_emp_date"}),
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp"}),
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
         {"name": "status_date_emp"}),
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except ServerSelectionTimeoutError as exc:
            # database unreachable: stop here instead of waiting 5 s per index (startup must stay fast)
            log.error("MongoDB unreachable, skipping index creation: %s", exc)
            return
        except PyMongoError as exc:  # keep the app up; log loudly
            log.error("index creation failed for %s %s: %s", coll.name, keys, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # pragma: no cover
        log.error("startup index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def to_api(value: Any) -> Any:
    """Recursively convert datetimes (including those inside history) to epoch ms."""
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def _round(x: Optional[float], places: str) -> Optional[float]:
    if x is None:
        return None
    return float(Decimal(str(x)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def r2(x: Optional[float]) -> Optional[float]:
    """R8: 2 decimals, half-up."""
    return _round(x, "0.01")


def r4(x: Optional[float]) -> Optional[float]:
    """R8 for rates: 4 decimals, half-up."""
    return _round(x, "0.0001")


def unprocessable(msg: str) -> HTTPException:
    """422 with FastAPI's default validation-error shape: {"detail": [ {loc, msg, type}, ... ]}."""
    return HTTPException(422, [{"loc": ["request"], "msg": msg, "type": "value_error"}])


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMP_RE = re.compile(r"^EMP\d{4,6}$")


def parse_date(value: Optional[str], name: str = "date") -> Date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise unprocessable(f"{name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} is not a valid calendar date")


def parse_month(value: Optional[str]):
    if not isinstance(value, str) or not MONTH_RE.match(value):
        raise unprocessable("month must be YYYY-MM")
    y, m = int(value[:4]), int(value[5:])
    try:
        first = Date(y, m, 1)
        last = Date(y, m, calendar.monthrange(y, m)[1])
    except ValueError:
        raise unprocessable("month is out of range")
    return first, last


def validate_instant(v: Optional[int]) -> Optional[int]:
    if v is None:
        return v
    # below 1e11 looks like epoch SECONDS; also reject out-of-range values
    if v < MS_MIN or v > MS_MAX:
        raise ValueError("instant must be an integer in epoch milliseconds")
    return v


def working_days(d1: Date, d2: Date) -> int:
    """Mon-Fri days in [d1, d2], no holidays (R7)."""
    if d2 < d1:
        return 0
    n, d = 0, d1
    while d <= d2:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def int_param(qp, name: str, default: Optional[int], lo: int, hi: Optional[int] = None) -> Optional[int]:
    raw = qp.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise unprocessable(f"{name} must be an integer")
    if v < lo or (hi is not None and v > hi):
        raise unprocessable(f"{name} out of range")
    return v


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hm(shift_end) <= _hm(shift_start)


def shift_date_of(ts: datetime, emp: dict) -> Date:
    """R1: IST date of punch-in; overnight shift punched earlier than shift_end -> previous day."""
    ist = ts.astimezone(IST)
    d = ist.date()
    if is_overnight(emp["shift_start"], emp["shift_end"]) and ist.time() < _hm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d


def shift_bounds(d: Date, emp: dict):
    start = datetime.combine(d, _hm(emp["shift_start"]), tzinfo=IST)
    end_day = d + timedelta(days=1) if is_overnight(emp["shift_start"], emp["shift_end"]) else d
    end = datetime.combine(end_day, _hm(emp["shift_end"]), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if STRICTLY more than 10:00 after start; minutes = floor since shift start."""
    diff = int((punch_in - shift_start_dt).total_seconds())
    return diff // 60 if diff > LATE_GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes (floor) after shift end, only when >= 30."""
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def derive_fields(emp: dict, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(Date.fromisoformat(date_str), emp)
    out = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, start),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = Decimal(str(wh)) < HALF_DAY_LIMIT  # R5 uses the ROUNDED value
    return out


def public_record(doc: dict) -> dict:
    """Stored document -> API shape (missing legacy fields get their defaults)."""
    return to_api({
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": doc.get("history") or [],
    })


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def _emp_code(cls, v: str) -> str:
        if not EMP_RE.match(v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not (1 <= len(v) <= 100):
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("department")
    @classmethod
    def _dept(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if len(v) > 120 or not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _hm_format(cls, v: str) -> str:
        if not HM_RE.match(v):
            raise ValueError("must be HH:MM (24h)")
        return v

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def _instant(cls, v):
        return validate_instant(v)

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in PRESENT_STATUSES:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def _instant(cls, v):
        return validate_instant(v)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def _instant(cls, v):
        return validate_instant(v)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if not (5 <= len(v) <= 200):
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def _by(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by must be 1-50 characters")
        return v

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        return JSONResponse({"detail": "database unavailable"}, status_code=503)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index: exactly one concurrent create wins
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": to_api(items), "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = shift_date_of(ts, emp)
    start, _ = shift_bounds(d, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_record(doc)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    d1 = parse_date(date_from, "date_from") if date_from else None
    d2 = parse_date(date_to, "date_to") if date_to else None
    if d1 and d2 and d1 > d2:
        raise unprocessable("date_from is after date_to")
    if d1 or d2:
        rng = {}
        if d1:
            rng["$gte"] = d1.isoformat()
        if d2:
            rng["$lte"] = d2.isoformat()
        q["date"] = rng
    if status:
        if status not in ALL_STATUSES:
            raise unprocessable(f"status must be one of {ALL_STATUSES}")
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATT_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [public_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    for _ in range(5):
        # most recent record whose punch_in is at or before punched_at (works across midnight)
        rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
            {"_id": 0},
            sort=[("date", DESCENDING)],
        )
        if not rec:
            raise HTTPException(404, "no punch-in found for this employee")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        if ts <= rec["punch_in"]:
            raise unprocessable("punched_at must be after punch_in")
        if ts - rec["punch_in"] > timedelta(hours=24):
            raise unprocessable("punched_at is more than 24 hours after punch_in")

        derived = derive_fields(emp, rec["date"], rec["punch_in"], ts)
        res = db.attendance_logs.update_one(
            {
                "emp_code": body.emp_code,
                "date": rec["date"],
                "punch_out": None,                 # atomic guard: only one punch-out can match
                "punch_in": rec["punch_in"],
                "status": rec["status"],
            },
            {"$set": {"punch_out": ts, **derived}},
        )
        if res.matched_count == 1:
            fresh = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": rec["date"]}, {"_id": 0})
            return public_record(fresh)
        # lost a race / record changed meanwhile -> re-read and re-evaluate (-> 409 if now closed)
    raise HTTPException(409, "concurrent update, please retry")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = get_employee_or_404(emp_code)
    fields = body.model_fields_set

    for _ in range(10):
        cur = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
        if not cur:
            raise HTTPException(404, "no attendance record for that date")

        new_status = body.status if "status" in fields else cur.get("status")
        new_in = from_ms(body.punch_in) if "punch_in" in fields else cur.get("punch_in")
        new_out = from_ms(body.punch_out) if "punch_out" in fields else cur.get("punch_out")

        if new_status in PRESENT_STATUSES:
            if new_in is None:
                raise unprocessable("a presence status requires a punch_in")
            if shift_date_of(new_in, emp).isoformat() != date:
                raise unprocessable("punch_in must stay on the record's attendance date")
            if new_out is not None:
                if new_out <= new_in:
                    raise unprocessable("punch_out must be after punch_in")
                if new_out - new_in > timedelta(hours=24):
                    raise unprocessable("punch_out must be within 24 hours of punch_in")
        else:  # ABSENT / LEAVE clear both punch times
            if "punch_in" in fields or "punch_out" in fields:
                raise unprocessable("ABSENT/LEAVE cannot be combined with punch times")
            new_in = new_out = None

        # a request that changes nothing is 422 (judged on the client-editable fields)
        if (new_status == cur.get("status") and new_in == cur.get("punch_in")
                and new_out == cur.get("punch_out")):
            raise unprocessable("request changes nothing")

        candidate = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                     **derive_fields(emp, date, new_in, new_out)}
        before = {
            "status": cur.get("status"),
            "punch_in": cur.get("punch_in"),
            "punch_out": cur.get("punch_out"),
            "work_hours": cur.get("work_hours"),
            "late_minutes": cur.get("late_minutes") or 0,
            "overtime_minutes": cur.get("overtime_minutes") or 0,
            "half_day": bool(cur.get("half_day", False)),
        }
        changes = {k: {"from": before[k], "to": v} for k, v in candidate.items() if before[k] != v}

        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        hist = cur.get("history")
        flt = {
            "emp_code": emp_code, "date": date,
            "status": cur.get("status"), "punch_in": cur.get("punch_in"), "punch_out": cur.get("punch_out"),
            "history": {"$size": len(hist)} if isinstance(hist, list) else {"$not": {"$type": "array"}},
        }
        if isinstance(hist, list):
            update = {"$set": candidate, "$push": {"history": entry}}
        else:
            update = {"$set": {**candidate, "history": [entry]}}
        res = db.attendance_logs.update_one(flt, update)
        if res.matched_count == 1:
            fresh = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
            return public_record(fresh)
        # someone changed the record in between: retry on the fresh state (no lost history)
    raise HTTPException(409, "concurrent update, please retry")


# --------------------------------------------------------------------------- #
# Aggregation builders (shared by the endpoints AND /admin/explain)
# --------------------------------------------------------------------------- #
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun..7=Sat
_IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
# present credit: presence status AND Mon-Fri; half day = 0.5 (R5-R7)
_CREDIT = {
    "$cond": [
        {"$and": [{"$in": ["$status", PRESENT_STATUSES]}, {"$in": ["$_dow", [2, 3, 4, 5, 6]]}]},
        {"$cond": [_IS_HALF, 0.5, 1]},
        0,
    ]
}
_LATE = {"$ifNull": ["$late_minutes", 0]}


def monthly_pipeline(emp_code: str, first: Date, last: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$addFields": {"_dow": _DOW}},
        {"$addFields": {"_credit": _CREDIT}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": "$_credit"},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(first: Date, last: Date, department: Optional[str]) -> list:
    f, l = first.isoformat(), last.isoformat()
    match: dict = {"joined_on": {"$lte": l}}  # R9: joined on/before period end, logs or not
    if department:
        match["department"] = department
    has_wh = {"$and": ["$_pres", {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": l}}},
                {"$addFields": {"_dow": _DOW}},
                {"$addFields": {"_credit": _CREDIT, "_pres": {"$in": ["$status", PRESENT_STATUSES]}}},
                {"$project": {
                    "_id": 0,
                    "_credit": 1,
                    "_wh": {"$cond": [has_wh, "$work_hours", None]},
                    "_whn": {"$cond": [has_wh, 1, 0]},
                    "_late": _LATE,
                    "_latec": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]},
                    "_leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                    "_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present": {"$sum": "$logs._credit"},
            "work_sum": {"$sum": "$logs._wh"},
            "work_n": {"$sum": "$logs._whn"},
            "late": {"$sum": "$logs._late"},
            "latec": {"$sum": "$logs._latec"},
            "leave": {"$sum": "$logs._leave"},
            "duty": {"$sum": "$logs._duty"},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present": {"$sum": "$present"},
            "work_sum": {"$sum": "$work_sum"},   # average is over RECORDS (sum / count), not of averages
            "work_n": {"$sum": "$work_n"},
            "late": {"$sum": "$late"},
            "latec": {"$sum": "$latec"},
            "leave": {"$sum": "$leave"},
            "duty": {"$sum": "$duty"},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipe: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are ignored
    ]
    if department:
        pipe.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipe += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},   # limit AFTER ranking; ties may exceed `limit` rows
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipe


def _round_half_up_4(expr: Any) -> Any:
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, 10000]}, 0.5]}}, 10000]}


def trend_pipeline(codes: list, joins: list, d_from: Date, d_to: Date) -> list:
    start = datetime.combine(d_from, time.min, tzinfo=UTC)
    end_excl = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENT_STATUSES]},
                                                 {"$cond": [_IS_HALF, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one row for every calendar day, weekends and empty days included
        {"$densify": {"field": "day",
                      "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joins}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                _round_half_up_4({"$divide": ["$present_count", "$headcount"]}),
                None,
            ]},
        }},
        # 7-day moving average over current + up to 6 preceding rows in range; $avg ignores nulls
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


def department_info(department: str):
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "department not found")
    return [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


def check_range(date_from: Optional[str], date_to: Optional[str]):
    d1, d2 = parse_date(date_from, "from"), parse_date(date_to, "to")
    if d2 < d1:
        raise unprocessable("to is before from")
    if (d2 - d1).days + 1 > 92:
        raise unprocessable("range exceeds 92 days")
    return d1, d2


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = parse_month(month)
    emp = get_employee_or_404(emp_code)
    agg = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last))), None) or {}
    joined = Date.fromisoformat(emp["joined_on"])
    wd = working_days(max(first, joined), last) if joined <= last else 0
    present = float(agg.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": r2(present),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": r2(present / wd * 100) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = parse_month(month)
    rows = list(db.employees.aggregate(summary_pipeline(first, last, department)))
    items = [{
        "department": r["_id"],
        "headcount": r["headcount"],
        "present_days": r2(float(r["present"])),
        "avg_work_hours": r2(float(r["work_sum"]) / r["work_n"]) if r["work_n"] else None,
        "late_count": r["latec"],
        "total_late_minutes": r["late"],
        "leave_count": r["leave"],
        "on_duty_count": r["duty"],
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = parse_month(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": rows}


def _trend_row_fallback(d: Date, joins: list, present: float = 0.0, late: int = 0) -> dict:
    ds = d.isoformat()
    head = sum(1 for j in joins if j <= ds)
    working = d.weekday() < 5
    rate = r4(present / head) if working and head > 0 else None
    return {"date": ds, "is_working_day": working, "headcount": head, "present_count": present,
            "late_count": late, "attendance_rate": rate}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: Optional[str] = Query(None, alias="from"),
                     date_to: Optional[str] = Query(None, alias="to")):
    d1, d2 = check_range(date_from, date_to)
    codes, joins = department_info(department)
    rows = list(db.attendance_logs.aggregate(trend_pipeline(codes, joins, d1, d2)))
    expected = (d2 - d1).days + 1
    if len(rows) != expected:
        # safety net (e.g. a server that returns nothing from $densify on empty input)
        have = {r["date"]: r for r in rows}
        rows, rates = [], []
        for i in range(expected):
            d = d1 + timedelta(days=i)
            r = have.get(d.isoformat()) or _trend_row_fallback(d, joins)
            rates.append(r["attendance_rate"])
            window = [x for x in rates[max(0, i - 6): i + 1] if x is not None]
            r["moving_avg_7d"] = sum(window) / len(window) if window else None
            rows.append(r)
    items = [{
        "date": r["date"],
        "is_working_day": bool(r["is_working_day"]),
        "headcount": r["headcount"],
        "present_count": r2(float(r["present_count"])),
        "late_count": r["late_count"],
        "attendance_rate": r4(r["attendance_rate"]),
        "moving_avg_7d": r4(r["moving_avg_7d"]),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _explain(endpoint: str, collection: str, cmd: dict) -> JSONResponse:
    res = db.command({"explain": cmd, "verbosity": "executionStats"})
    plan = json.loads(json_util.dumps(res, json_options=json_util.RELAXED_JSON_OPTIONS))
    return JSONResponse({"endpoint": endpoint, "collection": collection, "explain": plan})


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    request: Request,
):
    qp = request.query_params

    if endpoint == "attendance_list":
        q = attendance_filter(qp.get("emp_code"), qp.get("date_from"), qp.get("date_to"), qp.get("status"))
        page = int_param(qp, "page", 1, 1)
        page_size = int_param(qp, "page_size", 20, 1, 100)
        return _explain(endpoint, "attendance_logs", {
            "find": "attendance_logs", "filter": q, "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size,
        })

    if endpoint == "employee_monthly":
        emp_code = qp.get("emp_code")
        if not emp_code:
            raise unprocessable("emp_code is required")
        first, last = parse_month(qp.get("month"))
        get_employee_or_404(emp_code)
        return _explain(endpoint, "attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}})

    if endpoint == "department_summary":
        first, last = parse_month(qp.get("month"))
        return _explain(endpoint, "employees", {
            "aggregate": "employees",
            "pipeline": summary_pipeline(first, last, qp.get("department")), "cursor": {}})

    if endpoint == "late_leaderboard":
        first, last = parse_month(qp.get("month"))
        limit = int_param(qp, "limit", 10, 1, 50)
        return _explain(endpoint, "attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": leaderboard_pipeline(first, last, limit, qp.get("department")), "cursor": {}})

    # department_trend
    department = qp.get("department")
    if not department:
        raise unprocessable("department is required")
    d1, d2 = check_range(qp.get("from"), qp.get("to"))
    codes, joins = department_info(department)
    return _explain(endpoint, "attendance_logs", {
        "aggregate": "attendance_logs",
        "pipeline": trend_pipeline(codes, joins, d1, d2), "cursor": {}})