"""水质档案 · 盘片退役管理网页（FastAPI：HTML 页面 + JSON API）。"""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import service
from db import get_conn, init_db
from service import ServiceError

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["tojson"] = lambda v, indent=2: json.dumps(
    v, ensure_ascii=False, indent=indent)
templates.env.filters["parse_json"] = lambda s: json.loads(s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="水质档案 · 盘片退役管理", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    """JSON 接口的业务错误统一为 {"ok": false, "message": ...}。"""
    return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status)


# ---------------- HTML 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_conn() as conn:
        rows = service.list_models(conn)
    return templates.TemplateResponse(request, "index.html", {"rows": rows})


@app.post("/models")
def create_model_html(request: Request, name: str = Form(...)):
    try:
        with get_conn() as conn:
            service.create_model(conn, name)
            conn.commit()
    except ServiceError as e:
        with get_conn() as conn:
            rows = service.list_models(conn)
        return templates.TemplateResponse(
            request, "index.html", {"rows": rows, "error": str(e)}, status_code=e.status)
    return RedirectResponse(f"/models/{name.strip()}", status_code=303)


@app.get("/models/{name}", response_class=HTMLResponse)
def model_page(request: Request, name: str, notice: str | None = None):
    with get_conn() as conn:
        ctx = service.model_detail(conn, name)
    parsed_notice = json.loads(notice) if notice else None
    return templates.TemplateResponse(
        request, "model.html", {**ctx, "notice": parsed_notice})


@app.post("/models/{name}/disks", response_class=HTMLResponse)
def register_disk_html(request: Request, name: str,
                       disk_no: str = Form(...),
                       csv_text: str = Form(...),
                       note: str = Form("")):
    try:
        result = service.register_disk(name, disk_no, csv_text, note)
    except ServiceError as e:
        with get_conn() as conn:
            ctx = service.model_detail(conn, name)
        return templates.TemplateResponse(
            request, "model.html",
            {**ctx, "notice": {"ok": False, "message": str(e)}},
            status_code=e.status)
    dup = len(result["duplicates"])
    msg = (f"盘 {result['disk_no']} 登记成功，共 {result['entry_count']} 份报文"
           + (f"（自动去重 {dup} 行重复）" if dup else ""))
    return RedirectResponse(
        f"/models/{name}?notice=" + _notice_param(True, msg), status_code=303)


@app.post("/models/{name}/retire", response_class=HTMLResponse)
def retire_html(request: Request, name: str, disk_nos: list[str] = Form(default=[])):
    try:
        result = service.retire_disks(name, disk_nos)
    except ServiceError as e:
        with get_conn() as conn:
            ctx = service.model_detail(conn, name)
        return templates.TemplateResponse(
            request, "model.html",
            {**ctx, "notice": {"ok": False, "message": str(e)}},
            status_code=e.status)
    if result["ok"]:
        msg = (f"退役成功：{', '.join(result['disk_nos'])}（"
               f"{result['entry_count']} 份报文均有范围外在用盘副本），可线下拔盘")
    else:
        msg = (f"退役未生效：{len(result['missing'])} 份报文在范围外在用盘上找不到"
               "相同测次与摘要的副本，详见下方待迁移清单")
    return RedirectResponse(
        f"/models/{name}?notice=" + _notice_param(result["ok"], msg, result),
        status_code=303)


def _notice_param(ok: bool, message: str, result: dict | None = None) -> str:
    payload = {"ok": ok, "message": message}
    if result and not result["ok"]:
        payload["missing"] = result["missing"]
        payload["disk_nos"] = result["disk_nos"]
    return quote(json.dumps(payload, ensure_ascii=False))


# ---------------- JSON API ----------------

class ModelIn(BaseModel):
    name: str


class DiskIn(BaseModel):
    disk_no: str
    csv_text: str = Field(alias="csv")
    note: str = ""


class RetireIn(BaseModel):
    disk_nos: list[str]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/models", status_code=201)
def api_create_model(body: ModelIn):
    with get_conn() as conn:
        model_id = service.create_model(conn, body.name)
        conn.commit()
    return {"ok": True, "name": body.name.strip(), "id": model_id}


@app.get("/api/models/{name}")
def api_model(name: str):
    with get_conn() as conn:
        ctx = service.model_detail(conn, name)
    return {
        "name": ctx["model"]["name"],
        "disks": [{"disk_no": d["disk_no"], "status": d["status"],
                   "entry_count": d["entry_count"], "note": d["note"],
                   "created_at": d["created_at"], "retired_at": d["retired_at"]}
                  for d in ctx["disks"]],
    }


@app.get("/api/models/{name}/disks/{disk_no}/entries")
def api_disk_entries(name: str, disk_no: str):
    with get_conn() as conn:
        model = service.get_model(conn, name)
        disk = conn.execute(
            "SELECT * FROM disks WHERE model_id=? AND disk_no=?",
            (model["id"], disk_no)).fetchone()
        if disk is None:
            raise ServiceError(f"盘号 {disk_no!r} 不存在", status=404)
        entries = conn.execute(
            "SELECT test_no, sha256 FROM manifest_entries WHERE disk_id=? "
            "ORDER BY test_no, sha256", (disk["id"],)).fetchall()
    return {"disk_no": disk_no, "status": disk["status"],
            "entries": [{"test_no": r["test_no"], "sha256": r["sha256"]} for r in entries]}


@app.post("/api/models/{name}/disks", status_code=201)
def api_register(name: str, body: DiskIn):
    result = service.register_disk(name, body.disk_no, body.csv_text, body.note)
    return {"ok": True, **result}


@app.post("/api/models/{name}/retire")
def api_retire(name: str, body: RetireIn):
    result = service.retire_disks(name, body.disk_nos)
    status = 200 if result["ok"] else 409
    return JSONResponse({"ok": result["ok"], **result}, status_code=status)


@app.get("/api/models/{name}/retirements")
def api_retirements(name: str):
    with get_conn() as conn:
        ctx = service.model_detail(conn, name)
    out = []
    for r in ctx["records"]:
        out.append({"disk_nos": json.loads(r["disk_nos"]),
                    "result": r["result"],
                    "missing": json.loads(r["missing"]),
                    "entry_count": r["entry_count"],
                    "detail": r["detail"],
                    "created_at": r["created_at"]})
    return {"retirements": out}
