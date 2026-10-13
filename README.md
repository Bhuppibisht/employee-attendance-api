# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
1. `pip install -r requirements.txt`
2. Set environment variables `MONGO_URI` and `MONGO_DB` (a local `.env` also works).
3. `uvicorn app.main:app --port 8000`

Indexes are created automatically at startup. `GET /health` returns 200 once MongoDB answers.

## Status
All endpoints in `openapi.yaml` are implemented, including the three analytics
aggregations and `GET /admin/explain/{endpoint}`. Nothing is missing.

See `REVIEW.md` for the defects found in the starter and `DECISIONS.md` for design choices.