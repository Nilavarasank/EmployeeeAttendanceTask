"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUS = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


_indexes_ready = False


def _create_index(collection, keys, **kwargs) -> None:
    try:
        collection.create_index(keys, **kwargs)
    except OperationFailure as exc:
        if exc.code != 85:
            raise


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    _create_index(db.employees, "emp_code", unique=True, name="emp_code_unique")
    _create_index(db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    _create_index(db.employees, "joined_on", name="joined_on")
    _create_index(
        db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    _create_index(db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    _create_index(db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _create_index(db.attendance_logs, [("status", ASCENDING), ("date", ASCENDING)], name="status_date")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time and rounding
# --------------------------------------------------------------------------- #
def round_half_up(value, places: int):
    quant = Decimal("1").scaleb(-places)
    rounded = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return float(rounded)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M") < shift_end:
        local = local - timedelta(days=1)
    return local.date().isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    hour, minute = parse_hhmm(shift_start)
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    hour, minute = parse_hhmm(shift_end)
    year, month, day = (int(p) for p in date_str.split("-"))
    end = datetime(year, month, day, hour, minute, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    delta = punch_in - shift_start_dt(date_str, shift_start)
    if delta <= timedelta(minutes=10):
        return 0
    return int(delta.total_seconds() // 60)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    minutes = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(p) for p in month.split("-"))
    start = datetime(year, mon, 1).date()
    if mon == 12:
        end = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        end = datetime(year, mon + 1, 1).date() - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def working_days_count(joined_on: str, month: str) -> int:
    start_s, end_s = month_bounds(month)
    begin = max(datetime.fromisoformat(start_s).date(), datetime.fromisoformat(joined_on).date())
    end = datetime.fromisoformat(end_s).date()
    if begin > end:
        return 0
    count = 0
    day = begin
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def weekday_present_expr(prefix: str = "") -> dict:
    status = f"${prefix}status" if prefix else "$status"
    half = f"${prefix}half_day" if prefix else "$half_day"
    date = f"${prefix}date" if prefix else "$date"
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [status, list(PRESENCE)]},
                    {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": date}}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                ]
            },
            {"$cond": [{"$eq": [half, True]}, 0.5, 1]},
            0,
        ]
    }


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc["created_at"]),
    }


def serialize_changes(changes: dict) -> dict:
    out = {}
    for key, pair in (changes or {}).items():
        old, new = pair.get("from"), pair.get("to")
        if key in ("punch_in", "punch_out"):
            old, new = to_ms(old), to_ms(new)
        out[key] = {"from": old, "to": new}
    return out


def serialize_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history") or []:
        history.append(
            {
                "at": to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": serialize_changes(entry.get("changes") or {}),
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def derived_for(status: str, punch_in, punch_out, shift_start: str, shift_end: str, date_str: str) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, shift_start, date_str) if punch_in else 0
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, shift_start, shift_end, date_str),
        "half_day": hours < 4.50,
    }


def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def check_epoch(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, f"{field} must be epoch milliseconds")
    if value < EPOCH_MIN or value > EPOCH_MAX:
        raise HTTPException(422, f"{field} is out of range")
    return value


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_is_date(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def presence_only(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value

    @field_validator("punched_at")
    @classmethod
    def epoch(cls, value):
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < EPOCH_MIN or value > EPOCH_MAX:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def epoch(cls, value):
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < EPOCH_MIN or value > EPOCH_MAX:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def known_status(cls, value):
        if value is not None and value not in ALL_STATUS:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def epoch(cls, value):
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < EPOCH_MIN or value > EPOCH_MAX:
            raise ValueError("punch time must be epoch milliseconds")
        return value


# --------------------------------------------------------------------------- #
# Query builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUS:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    return query


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": weekday_present_expr()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department
    present = weekday_present_expr("logs.")
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$gte": ["$$log.date", start]},
                                {"$lte": ["$$log.date", end]},
                            ]
                        },
                    }
                }
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "people": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {"$cond": [{"$ifNull": ["$logs.emp_code", False]}, present, 0]}
                },
                "work_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$people"},
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_values"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": start}},
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [start, end + timedelta(days=1)]},
            }
        },
        {"$match": {"day": {"$lte": end}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$project": {"emp_code": 1, "_id": 0}},
                ],
                "as": "staff",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "codes": "$staff.emp_code",
                "headcount": {"$size": "$staff"},
                "logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {"$in": ["$$log.emp_code", "$staff.emp_code"]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", list(PRESENCE)]},
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def shape_number(value, places: int):
    if value is None:
        return None
    return round_half_up(value, places)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "mongodb unavailable") from exc
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], date_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    current = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if not current:
        raise HTTPException(404, "no punch-in found")
    punch_in = current["punch_in"]
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if ts <= punch_in or ts > punch_in + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = derived_for(
        current["status"], punch_in, ts, emp["shift_start"], emp["shift_end"], current["date"]
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    emp = require_employee(emp_code)
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not current:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status if body.status is not None else current["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE cannot include punch times")
        new_in = None
        new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else current.get("punch_in")
        new_out = from_ms(body.punch_out) if body.punch_out is not None else current.get("punch_out")
        if new_in is not None and new_in.tzinfo is None:
            new_in = new_in.replace(tzinfo=timezone.utc)
        if new_out is not None and new_out.tzinfo is None:
            new_out = new_out.replace(tzinfo=timezone.utc)
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on this attendance date")
        if new_out is not None and (new_out <= new_in or new_out > new_in + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = derived_for(new_status, new_in, new_out, emp["shift_start"], emp["shift_end"], date)
    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }
    changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old = current.get(key)
        if key == "half_day":
            old = bool(old) if old is not None else False
        if key in ("late_minutes", "overtime_minutes"):
            old = old or 0
        new = proposed[key]
        if key in ("punch_in", "punch_out") and old is not None and getattr(old, "tzinfo", None) is None:
            old = old.replace(tzinfo=timezone.utc)
        if old != new:
            changes[key] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_len = len(current.get("history") or [])
    history_filter = {"history": {"$size": history_len}} if "history" in current else {"history": {"$exists": False}}
    entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], **history_filter},
        {"$set": proposed, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was updated concurrently")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = require_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {}
    working = working_days_count(emp["joined_on"], month)
    present = stats.get("present_days", 0) or 0
    pct = None if working == 0 else shape_number(present / working * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": stats.get("leave_days", 0) or 0,
        "late_count": stats.get("late_count", 0) or 0,
        "total_late_minutes": stats.get("total_late_minutes", 0) or 0,
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0) or 0,
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        avg = row.get("avg_work_hours")
        items.append(
            {
                "department": row["department"],
                "headcount": row["headcount"],
                "present_days": row.get("present_days") or 0,
                "avg_work_hours": None if avg is None else shape_number(avg, 2),
                "late_count": row.get("late_count") or 0,
                "total_late_minutes": row.get("total_late_minutes") or 0,
                "leave_count": row.get("leave_count") or 0,
                "on_duty_count": row.get("on_duty_count") or 0,
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = []
    for row in db.attendance_logs.aggregate(late_leaderboard_pipeline(month, department, limit)):
        items.append(
            {
                "rank": row["rank"],
                "emp_code": row["_id"],
                "name": row["emp"]["name"],
                "department": row["emp"]["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
):
    try:
        start = datetime.fromisoformat(date_from).date()
        end = datetime.fromisoformat(date_to).date()
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    items = []
    for row in db.employees.aggregate(department_trend_pipeline(department, date_from, date_to)):
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": row["headcount"],
                "present_count": row.get("present_count") or 0,
                "late_count": row.get("late_count") or 0,
                "attendance_rate": None if rate is None else shape_number(rate, 4),
                "moving_avg_7d": None if moving is None else shape_number(moving, 4),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    date_from_alias: Optional[str] = Query(None, alias="from"),
    date_to_alias: Optional[str] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(422, "unknown endpoint")
    if date_from_alias:
        date_from = date_from or date_from_alias
    trend_from = date_from_alias
    trend_to = date_to_alias

    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        command = {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(emp_code, month),
            "cursor": {},
        }
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        command = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(month, department),
            "cursor": {},
        }
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        command = {
            "aggregate": "attendance_logs",
            "pipeline": late_leaderboard_pipeline(month, department, limit),
            "cursor": {},
        }
        collection = "attendance_logs"
    else:
        if not department or not trend_from or not trend_to:
            raise HTTPException(422, "department, from and to are required")
        command = {
            "aggregate": "employees",
            "pipeline": department_trend_pipeline(department, trend_from, trend_to),
            "cursor": {},
        }
        collection = "employees"

    explained = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": explained}
