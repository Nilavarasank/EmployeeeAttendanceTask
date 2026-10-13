# REVIEW.md

Defects in the starter `app/main.py`, and how they were fixed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | `skip = page * page_size` skips the first page. Page 1 with size 20 returns rows 21–40. | `GET /employees?page=1&page_size=2` omits the first two employees. | Skip `(page - 1) * page_size`. |
| 2 | `list_employees` | `total` counts the whole collection, ignoring `department`. | Filter `department=Engineering` and `total` still equals every employee. | `count_documents` uses the same filter as the query. |
| 3 | `list_employees` | Results are not sorted by `emp_code`. | The order changes between calls and does not match the contract. | Sort `emp_code` ascending in MongoDB. |
| 4 | `create_employee`, `punch_in`, `list_attendance` | Instants are naive datetimes. The API must use epoch milliseconds, and shift math must be IST. | `created_at` and `punch_in` come back as strings or objects, not integers. Late minutes depend on the server's local zone. | Store timezone-aware UTC datetimes truncated to seconds. Convert to and from epoch ms at the boundary. Compute the attendance date in IST. |
| 5 | `compute_late_minutes` | Late if the gap is more than 10 minutes, then that whole gap is returned. 09:40:01 on a 09:30 shift is 10 minutes late and must not be 0. 09:40:00 must be 0. The starter also keeps the punch's own timezone instead of the shift's IST start. | Punch in at 09:40:00 and at 09:40:01. Compare `late_minutes` with the examples in rule R2. | Late only when the punch is strictly after shift start plus 10 minutes. `late_minutes` is the floored minutes since shift start. |
| 6 | `compute_overtime` | Any minute past shift end is overtime. Rule R3 counts it only when the gap is at least 30 minutes. The shift end is built from a naive date, so an overnight shift ends on the wrong day. | Sample day 1 (5 minutes past 18:30) should store `overtime_minutes: 0`. A 22:00–06:00 shift punches out the next morning. | Floor the minutes after the IST shift end (next calendar day when overnight) and return 0 when that is under 30. |
| 7 | `compute_work_hours` | `round()` uses banker's rounding. Rule R4 requires half-up to 2 decimals. | A duration that lands on x.xx5 can round the wrong way. | Divide whole seconds by 3600 with `Decimal` and `ROUND_HALF_UP`. |
| 8 | `punch_in` | The attendance date is the clock date of the timestamp. For an overnight shift, a punch earlier than `shift_end` belongs to the previous day. A missing employee raises `TypeError` instead of 404. The duplicate check then insert can let two requests both insert. | Punch EMP0005 at 05:00 IST. The `date` should be the previous day. Punch an unknown code. Fire two identical punch-ins together. | Derive the date with rule R1. Return 404 when the employee is missing. Insert and map `DuplicateKeyError` on the unique `(emp_code, date)` index to 409. |
| 9 | `create_employee` | Uniqueness is only a `find_one` before insert, so two creates of the same `emp_code` can both return 201. `shift_start == shift_end` is accepted. | Two parallel `POST /employees` with the same code. | Unique index on `emp_code`. Reject equal shift times with 422. |
| 10 | `list_attendance` | The handler loads every matching document, sorts in Python, and only then slices the page. `total` is that in-memory length. On ~100k rows this is slow and can miss the `(date, emp_code)` sort. | Explain shows a collection scan, and a large date range gets slow. `date_from` after `date_to` is not rejected. | Filter, sort, skip, and limit in MongoDB. `count_documents` on the same filter. 422 when `date_from > date_to`. |
| 11 | `health` | Always returns 200. The contract returns 200 only after MongoDB answers a ping, otherwise 503. | Stop MongoDB and call `GET /health`. | Ping the server and return 503 on failure. |

Not defects:

- Default `status` of `PRESENT` on punch-in matches the contract.
- Punch-in correctly starts `work_hours` and `punch_out` as null, `overtime_minutes` as 0, `half_day` as false, and `history` as `[]`. Punch-in and punch-out must not write history; only a manual correction does.
- `_id` was already removed from employee responses. Attendance still needs `emp_code` and `date` as the public key, which the response now uses, and it does not expose `_id`.
