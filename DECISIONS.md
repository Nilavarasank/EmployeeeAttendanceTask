# DECISIONS.md

1. **Indexes.** On startup I create a unique index on `employees.emp_code`, a unique index on `attendance_logs (emp_code, date)`, plus `(department, emp_code)`, `joined_on`, `(date, emp_code)`, `(emp_code, punch_in)`, and `(status, date)`. The two unique indexes are what make a duplicate employee or a second punch-in for the same day return 409. The date and employee indexes are what list, punch-out, and the aggregations use so those queries are index scans. I considered a separate text index on employee name and did not add it, because no endpoint searches by name.

2. **Punch-in race.** Both requests load the employee, compute the same IST attendance date, and call `insert_one`. There is no check-then-insert. The unique `(emp_code, date)` index accepts one insert, which returns 201. The other insert raises `DuplicateKeyError`, which the handler turns into 409.

3. **Ties.** The leaderboard sorts by total late minutes and uses MongoDB `$rank`, so two employees with the same total share a rank and the next rank is skipped (1, 2, 2, 4). `limit` is applied after that rank. If the tied rank is still within `limit`, every tied employee is returned, so the list can be longer than `limit`. Rows are then ordered by late minutes descending and `emp_code` ascending.

4. **Headcount.** The department summary starts from `employees` whose `joined_on` is on or before the last day of the month, then looks up that month's logs. `$unwind` keeps employees who have no logs. Headcount is the size of the set of `emp_code` values in the department, so people with zero logs still count. A department with headcount 0 is dropped.

5. **100x data.** I would keep the aggregations, but I would pre-aggregate a daily department rollup when a punch or correction is saved, and serve the trend and department summary from that smaller collection instead of scanning attendance logs for every day in the range.
