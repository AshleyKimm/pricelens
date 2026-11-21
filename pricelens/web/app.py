"""FastAPI app, server-rendered with Jinja2.

Modes (PRICELENS_MODE):
  local - your database, read-write, correction forms. Bind to 127.0.0.1 only.
  demo  - data/demo.db opened READ-ONLY; write routes are not registered at all; no API key used.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import db
from ..categories import CATEGORIES, category_label
from ..money import format_amount
from ..units import DEFAULT_SYSTEM, SYSTEMS
from . import views

HERE = Path(__file__).parent
CSP = ("default-src 'none'; img-src 'self'; style-src 'self'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")


def connect_readonly(path: Path) -> sqlite3.Connection:
    """The demo DB can't be written through this connection, whatever the code above it does."""
    if not path.exists():
        raise RuntimeError(f"{path} not found: run `python -m pricelens demo build` first")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        raise RuntimeError(f"{path} is an old schema: rebuild it with `python -m pricelens demo build`")
    return conn


def create_app(mode: str = "local", db_path: Path | None = None, root: Path | None = None) -> FastAPI:
    if mode not in ("local", "demo"):
        raise ValueError("mode must be 'local' or 'demo'")
    root = (root or Path.cwd()).resolve()
    db_path = Path(db_path or root / "data" / ("demo.db" if mode == "demo" else "pricelens.db"))
    images_dir = (root / "demo" / "images") if mode == "demo" else (root / "data" / "redacted")
    writable = mode == "local"

    app = FastAPI(title="PriceLens", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")  # autoescape on for .html
    templates.env.filters["money"] = lambda v, cur="USD": format_amount(v, cur)
    templates.env.filters["urlq"] = lambda v: quote(str(v), safe="")
    templates.env.filters["cat"] = category_label
    templates.env.globals.update(mode=mode, writable=writable, categories=list(CATEGORIES))

    def conn():
        if writable:
            return db.connect(db_path)
        return connect_readonly(db_path)

    def units_of(request: Request) -> str:
        u = request.cookies.get("units", DEFAULT_SYSTEM)
        return u if u in SYSTEMS else DEFAULT_SYSTEM

    def page(request: Request, name: str, **ctx):
        units = units_of(request)
        ctx.setdefault("units", units)
        ctx.setdefault("here", request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        ctx.setdefault("price_text", lambda o: views.price_text(o, units))
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/units")
    def set_units(u: str, next: str = "/"):
        """Display preference only (imperial/metric), stored in a cookie. Allowed in demo mode: it writes nothing."""
        if u not in SYSTEMS:
            raise HTTPException(400, "units must be imperial or metric")
        # Only same-site paths: "/x" yes; "//evil.example" or "https://..." no (no open redirect).
        if not next.startswith("/") or next.startswith("//") or "\\" in next:
            next = "/"
        resp = RedirectResponse(next, status_code=303)
        resp.set_cookie("units", u, max_age=365 * 24 * 3600, samesite="lax", httponly=True)
        return resp

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                return HTMLResponse("cross-origin request refused", status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = ""):
        c = conn()
        try:
            return page(request, "index.html", q=q, rows=views.search(c, q, units_of(request)))
        finally:
            c.close()

    @app.get("/product", response_class=HTMLResponse)
    def product(request: Request, name: str):
        c = conn()
        try:
            p = views.product(c, name, units_of(request))
        finally:
            c.close()
        if p is None:
            raise HTTPException(404, "no verified purchases of this product")
        return page(request, "product.html", p=p, chart=Markup(p.chart_svg), legend=Markup(p.legend))

    @app.get("/receipts", response_class=HTMLResponse)
    def receipt_list(request: Request):
        c = conn()
        try:
            return page(request, "receipts.html", rows=views.receipts(c))
        finally:
            c.close()

    @app.get("/receipts/{receipt_id}", response_class=HTMLResponse)
    def receipt_detail(request: Request, receipt_id: int):
        c = conn()
        try:
            d = views.receipt(c, receipt_id)
        finally:
            c.close()
        if d is None:
            raise HTTPException(404, "no such receipt")
        return page(request, "receipt.html", **d)

    @app.get("/receipts/{receipt_id}/image")
    def receipt_image(receipt_id: int):
        # Served by image hash from one fixed folder - never from a stored or user-supplied path.
        c = conn()
        try:
            row = c.execute("SELECT image_sha256 FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        finally:
            c.close()
        if row is None or not all(ch in "0123456789abcdef" for ch in row[0]):
            raise HTTPException(404)
        path = images_dir / f"{row[0]}.jpg"
        if not path.is_file():
            raise HTTPException(404, "image not available")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/labels", response_class=HTMLResponse)
    def label_list(request: Request, review: bool = False, msg: str = "", err: str = ""):
        c = conn()
        try:
            return page(request, "labels.html", rows=views.labels(c, review), review=review,
                        cases=views.correction_cases(root, c), msg=msg, err=err)
        finally:
            c.close()

    if writable:  # In demo mode these routes don't exist, so there's nothing to call.
        @app.post("/labels/fix")
        async def label_fix(request: Request):
            # Parsed with the stdlib instead of FastAPI's Form(), which needs the extra python-multipart package.
            from ..labeling import fix_label
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8"), max_num_fields=10).items()}
            missing = [f for f in ("store", "description", "name", "category") if not form.get(f)]
            if missing:
                return RedirectResponse(f"/labels?err={quote('missing ' + ', '.join(missing))}", status_code=303)
            description, name = form["description"], form["name"]
            c = conn()
            try:
                fix_label(c, form["store"], description, name, form["category"], root=root,
                          variety=form.get("variety", ""))
                q = f"msg={quote(f'Saved {description} -> {name}. Recorded as a regression case.')}"
            except ValueError as e:
                q = f"err={quote(str(e))}"
            finally:
                c.close()
            return RedirectResponse(f"/labels?{q}", status_code=303)

    return app
