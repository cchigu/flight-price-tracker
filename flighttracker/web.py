"""Web server: serves the page and runs searches in the background.

Run:  python -m flighttracker.web          (then open http://localhost:8000)
Env:  FT_PROVIDER=google-flights|fake  FT_MIN_DELAY=2  FT_MAX_DELAY=5  FT_HOST=0.0.0.0  FT_PORT=8000
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import MAX_DAYS_AHEAD, MAX_PAIRS, FlexibleSearch, Throttle, estimate_minutes, pick_top, run_search
from .models import Passengers, SearchOptions, TripResult
from .providers import PriceProvider, get_provider

STATIC = Path(__file__).parent / "static"
JOB_TTL_S = 6 * 3600   # finished searches are kept for 6 hours
MAX_JOBS = 50


# ---------- request body ---------------------------------------------------------------
class PassengersIn(BaseModel):
    adults: int = Field(1, ge=1, le=9)
    children: int = Field(0, ge=0, le=8)
    infants_in_seat: int = Field(0, ge=0, le=8)
    infants_on_lap: int = Field(0, ge=0, le=8)


class SearchIn(BaseModel):
    origin: str
    destination: str
    depart_from: date
    depart_to: date
    min_days: int = 7
    max_days: int = 7
    trip_type: Literal["round-trip", "one-way"] = "round-trip"
    stops: Literal["direct", "connecting", "any"] = "any"
    max_stops: Optional[int] = Field(None, ge=1, le=2)
    cabin: Literal["economy", "premium-economy", "business", "first"] = "economy"
    passengers: PassengersIn = PassengersIn()
    currency: str = Field("USD", min_length=3, max_length=3)
    top_n: int = Field(10, ge=1, le=50)
    rank_by: Literal["price", "duration"] = "price"

    def to_request(self) -> FlexibleSearch:
        p = self.passengers
        try:
            passengers = Passengers(p.adults, p.children, p.infants_in_seat, p.infants_on_lap)
        except ValueError as e:
            raise HTTPException(422, str(e))
        opts = SearchOptions(
            trip_type=self.trip_type, cabin=self.cabin, passengers=passengers,
            currency=self.currency.upper(), stops=self.stops,
            max_stops=None if self.stops == "direct" else self.max_stops,
        )
        return FlexibleSearch(self.origin, self.destination, self.depart_from, self.depart_to,
                              self.min_days, self.max_days, opts, self.top_n, self.rank_by)


# ---------- background jobs -----------------------------------------------------------
@dataclass
class Job:
    id: str
    request: FlexibleSearch
    status: Literal["queued", "running", "done", "cancelled", "failed"] = "queued"
    checks_done: int = 0
    checks_total: int = 0
    candidates: list[TripResult] = field(default_factory=list)
    report: Optional[dict] = None
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            cands = list(self.candidates)
            report = self.report
        r = self.request
        # Best fare per departure day, for the calendar.
        days: dict[str, int] = {}
        for c in cands:
            k = c.depart_date.isoformat()
            if k not in days or c.itinerary.price < days[k]:
                days[k] = c.itinerary.price
        if report is not None:
            results = report["results"]
        else:
            results = [t.to_dict() for t in pick_top(cands, r.top_n, r.rank_by)]
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "checks_done": self.checks_done, "checks_total": self.checks_total,
            "currency": r.options.currency, "round_trip": r.round_trip,
            "depart_from": r.depart_from.isoformat(), "depart_to": r.depart_to.isoformat(),
            "origin": r.origin, "destination": r.destination,
            "days": days, "results": results, "final": report is not None,
            "messages": report["messages"] if report else [],
            "blocked": bool(report and report["blocked"]),
        }


class SearchRunner:
    """Runs one search at a time (a polite pace for a free source); the rest wait in line."""

    def __init__(self, provider: PriceProvider, throttle: Throttle):
        self.provider = provider
        self.throttle = throttle
        self.jobs: dict[str, Job] = {}
        self._gate = threading.Lock()

    def submit(self, req: FlexibleSearch) -> Job:
        self._cleanup()
        job = Job(id=uuid.uuid4().hex[:12], request=req, checks_total=len(req.date_pairs()))
        self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"search-{job.id}").start()
        return job

    def queue_position(self, job: Job) -> int:
        waiting = sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: j.created)
        return next((i + 1 for i, j in enumerate(waiting) if j.id == job.id), 0)

    def _run(self, job: Job) -> None:
        with self._gate:
            if job.cancel.is_set():
                job.status, job.finished = "cancelled", time.time()
                return
            job.status = "running"

            def progress(done, total, best):
                with job.lock:
                    job.checks_done = done
                    if best is not None:
                        job.candidates.append(best)

            try:
                rep = run_search(job.request, self.provider, self.throttle, progress, job.cancel.is_set)
                with job.lock:
                    job.report = rep.to_dict()
                    job.checks_done = rep.checks_done
                job.status = "cancelled" if rep.cancelled else "done"
            except Exception as e:  # never let a thread die silently
                job.status, job.error = "failed", str(e)
            job.finished = time.time()

    def _cleanup(self) -> None:
        now = time.time()
        for jid, j in list(self.jobs.items()):
            if j.finished and now - j.finished > JOB_TTL_S:
                del self.jobs[jid]
        if len(self.jobs) > MAX_JOBS:
            for j in sorted((j for j in self.jobs.values() if j.finished), key=lambda j: j.created)[: len(self.jobs) - MAX_JOBS]:
                del self.jobs[j.id]


# ---------- app ------------------------------------------------------------------------
def create_app(provider: Optional[PriceProvider] = None, throttle: Optional[Throttle] = None) -> FastAPI:
    provider = provider or get_provider(os.environ.get("FT_PROVIDER", "google-flights"))
    throttle = throttle or Throttle(float(os.environ.get("FT_MIN_DELAY", 2)), float(os.environ.get("FT_MAX_DELAY", 5)))
    runner = SearchRunner(provider, throttle)
    app = FastAPI(title="Flight Price Tracker")
    app.state.runner = runner

    def build(body: SearchIn) -> FlexibleSearch:
        req = body.to_request()
        try:
            req.validate()
        except ValueError as e:
            raise HTTPException(422, str(e))
        return req

    @app.get("/api/config")
    def config():
        today = date.today()
        return {"provider": provider.name, "today": today.isoformat(),
                "max_date": (today + timedelta(days=MAX_DAYS_AHEAD)).isoformat(), "max_checks": MAX_PAIRS}

    @app.post("/api/estimate")
    def estimate(body: SearchIn):
        req = build(body)
        return {"checks": len(req.date_pairs()), "minutes": round(estimate_minutes(req, throttle), 1)}

    @app.post("/api/searches", status_code=201)
    def start(body: SearchIn):
        job = runner.submit(build(body))
        return job.snapshot() | {"queue_position": runner.queue_position(job)}

    @app.get("/api/searches/{job_id}")
    def status(job_id: str):
        job = runner.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "This search has expired. Start a new one.")
        return job.snapshot() | {"queue_position": runner.queue_position(job)}

    @app.post("/api/searches/{job_id}/cancel")
    def cancel(job_id: str):
        job = runner.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "This search has expired.")
        job.cancel.set()
        return {"id": job.id, "status": "cancelling"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("FT_HOST", "127.0.0.1"), port=int(os.environ.get("FT_PORT", 8000)))


if __name__ == "__main__":
    main()
