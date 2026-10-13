# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.




1. **Indexes.** On attendance logs I made an index on (emp_code, date) that must be unique. It makes sure one employee has only one record per day, and it makes per-employee queries fast. I also made (date, emp_code) for the sorted list and date filters, and (status, date, emp_code) for the status filter. On employees I made emp_code unique, and indexes on (department, joined_on) and joined_on for headcount. I did not make an index on late_minutes. The leaderboard first picks a month by date, so that index would only make writes slower.

2. **Punch-in race.** Both requests find the same date and try to insert. The unique index allows only one insert. The other one fails with a duplicate error and I return 409. I do not check first and then insert.

3. **Ties.** I rank with 1, 2, 2, 4 and keep every row whose rank is within the limit. So if two people tie at the cutoff, both are returned. The list can be longer than the limit. This is what the contract says.

4. **Headcount.** The summary starts from the employees collection, not from the logs. It keeps everyone who joined on or before the end of the month, then looks up their logs. A person with no logs still counts.

5. **At 100x data.** I would store a monthly summary per employee, updated at every punch and correction, so reports do not scan every daily record.