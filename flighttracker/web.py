"""Web server: serves the page and runs searches in the background.

Run:  python -m flighttracker.web          (then open http://localhost:8000)
Env:  FT_PROVIDER=google-flights|fake  FT_MIN_DELAY=2  FT_MAX_DELAY=5  FT_HOST=0.0.0.0  FT_PORT=8000
      FT_PASSWORD=...        owner's password; creates the owner login on first start (turns sign-in on)
      FT_OWNER=admin         the owner's sign-in name (first start only)
      FT_DATA_DIR=./data     where logins are stored
      FT_SECURE_COOKIE=1     only send the sign-in cookie over HTTPS
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts
from .auth import Gate
from .users import UserStore

from .engine import MAX_DAYS_AHEAD, MAX_PAIRS, FlexibleSearch, Throttle, estimate_minutes, pick_top, run_search
from .models import Passengers, SearchOptions, TripResult
from .providers import PriceProvider, get_provider

STATIC = Path(__file__).parent / "static"
ROOT = Path(__file__).resolve().parent.parent
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
    owner: Optional[int] = None   # user id; None when sign-in is off
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

    def active_for(self, owner: Optional[int]) -> Optional[Job]:
        return next((j for j in self.jobs.values()
                     if j.owner == owner and j.status in ("queued", "running")), None)

    def submit(self, req: FlexibleSearch, owner: Optional[int] = None) -> Job:
        self._cleanup()
        if owner is not None and self.active_for(owner) is not None:
            raise ValueError("You already have a search running. Stop it or wait for it to finish first.")
        job = Job(id=uuid.uuid4().hex[:12], request=req, owner=owner, checks_total=len(req.date_pairs()))
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

    def forget_user(self, owner: int) -> None:
        """Stop and drop everything a removed person had."""
        for jid, j in list(self.jobs.items()):
            if j.owner == owner:
                j.cancel.set()
                del self.jobs[jid]

    def _cleanup(self) -> None:
        now = time.time()
        for jid, j in list(self.jobs.items()):
            if j.finished and now - j.finished > JOB_TTL_S:
                del self.jobs[jid]
        if len(self.jobs) > MAX_JOBS:
            for j in sorted((j for j in self.jobs.values() if j.finished), key=lambda j: j.created)[: len(self.jobs) - MAX_JOBS]:
                del self.jobs[j.id]


# ---------- app ------------------------------------------------------------------------
def create_app(provider: Optional[PriceProvider] = None, throttle: Optional[Throttle] = None,
               password: Optional[str] = None, data_dir: Optional[Path] = None) -> FastAPI:
    provider = provider or get_provider(os.environ.get("FT_PROVIDER", "google-flights"))
    throttle = throttle or Throttle(float(os.environ.get("FT_MIN_DELAY", 2)), float(os.environ.get("FT_MAX_DELAY", 5)))
    password = password if password is not None else os.environ.get("FT_PASSWORD", "")
    data_dir = Path(data_dir or os.environ.get("FT_DATA_DIR") or ROOT / "data")
    users = UserStore(data_dir)
    users.ensure_owner(os.environ.get("FT_OWNER", "admin"), password)
    runner = SearchRunner(provider, throttle)
    app = FastAPI(title="Flight Price Tracker", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runner = runner
    app.state.users = users

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    gate = Gate(users, secure_cookie=os.environ.get("FT_SECURE_COOKIE") == "1")
    app.middleware("http")(gate)
    accounts.register(app, gate)

    def owner_of(request: Request) -> Optional[int]:
        u = request.state.user
        return u.id if u is not None else None

    def find_job(request: Request, job_id: str, gone: str) -> Job:
        job = runner.jobs.get(job_id)
        # Someone else's search looks the same as one that doesn't exist.
        if not job or job.owner != owner_of(request):
            raise HTTPException(404, gone)
        return job

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
    def start(body: SearchIn, request: Request):
        try:
            job = runner.submit(build(body), owner_of(request))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return job.snapshot() | {"queue_position": runner.queue_position(job)}

    @app.get("/api/searches/active")
    def active(request: Request):
        job = runner.active_for(owner_of(request))
        return {"id": job.id if job else None}

    @app.get("/api/searches/{job_id}")
    def status(job_id: str, request: Request):
        job = find_job(request, job_id, "This search has expired. Start a new one.")
        return job.snapshot() | {"queue_position": runner.queue_position(job)}

    @app.post("/api/searches/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        job = find_job(request, job_id, "This search has expired.")
        job.cancel.set()
        return {"id": job.id, "status": "cancelling"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines into the environment (values already set win)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _log_to_file(path: str) -> None:
    """Send output to a log file. Needed when running hidden (pythonw has no console)."""
    import sys
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > 5_000_000:  # keep it from growing forever
        p.replace(p.with_suffix(".old.log"))
    f = open(p, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f


def main() -> None:
    # Settings can live in flight-tracker.env next to the code (used by the Windows installer).
    load_env_file(Path(os.environ.get("FT_ENV_FILE", Path(__file__).resolve().parent.parent / "flight-tracker.env")))
    if os.environ.get("FT_LOG_FILE"):
        _log_to_file(os.environ["FT_LOG_FILE"])
    import uvicorn
    # uvicorn's default log handlers bind to sys.stderr when it starts, so they follow the redirect above.
    uvicorn.run(create_app(), host=os.environ.get("FT_HOST", "127.0.0.1"), port=int(os.environ.get("FT_PORT", 8000)))


if __name__ == "__main__":
    main()
