"""FastAPI 服务：数据集、地图、分析任务与系统接口。

接口约定（技术方案第 14 章）：
- 耗时分析统一异步：POST 立即返回 task_id，GET 轮询状态，SSE 可选；
- 大型结果不塞进单次 JSON，通过 /api/v1/tasks/{id}/summary 与文件下载接口获取；
- 所有接口返回统一结构 pack(...)，包含 request_id、数据版本与错误码。
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import dataservice, store, tasks
from . import auth as auth_mod
from .config import settings, default_params, DEFAULT_PARAMS
from . import ingest as ingest_mod

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用启动 / 关闭钩子（取代已弃用的 on_event）。"""
    store.init_meta()
    settings.ensure_dirs()
    _seed_demo_if_needed()
    yield


app = FastAPI(
    title="AIS 船舶航迹数据分析与可视化系统",
    version="1.0.0",
    lifespan=_lifespan,
)

STATUS_CN = {
    "queued": "排队中", "running": "运行中", "succeeded": "已完成",
    "failed": "失败", "cancelled": "已取消", "created": "已创建",
}


def pack(data: Any = None, error: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"request_id": uuid.uuid4().hex[:12], "ok": error is None}
    if error is None:
        body["data"] = data
    else:
        body["error"] = error
    if extra:
        body.update(extra)
    return body


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=pack(error={"code": exc.status_code, "message": str(exc.detail)}),
    )


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    """统一鉴权入口（技术方案 16.1）。

    `AISP_AUTH=off`（默认）时只记录身份，不做拦截，保证单机开发不被打断；
    开启后按 (方法, 路径) → 权限 → 角色 逐层判定，越权写审计并返回 401/403。
    """
    ident = auth_mod.identity_from_headers(dict(request.headers))
    request.state.identity = ident

    need = auth_mod.required_permission(request.method, request.url.path)
    if need is None or ident.can(need):
        return await call_next(request)

    status = 401 if not ident.authenticated and auth_mod.auth_enabled() else 403
    detail = ("未提供有效令牌" if status == 401
              else f"角色 {ident.role_cn} 无权限 {need}")
    try:
        store.audit("auth_denied", request.url.path, f"need={need} role={ident.role}",
                    user_id=ident.subject, result="failed",
                    ip=request.client.host if request.client else "")
    except Exception:
        pass
    return JSONResponse(status_code=status,
                        content=pack(error={"code": status, "message": detail}))


# --------------------------------------------------------------------------- 数据集

class ImportRequest(BaseModel):
    paths: List[str] = Field(default_factory=list, description="服务器本地 CSV 路径")
    source_name: str = "ais"
    mapping: Optional[Dict[str, str]] = None
    time_format: Optional[str] = None
    created_by: str = "system"
    note: str = ""
    dataset_version: Optional[str] = None
    clean: bool = True
    clean_params: Optional[Dict[str, Any]] = None


@app.post("/api/v1/datasets/preflight")
def api_preflight(req: ImportRequest) -> Dict[str, Any]:
    if not req.paths:
        raise HTTPException(400, "请提供 CSV 路径")
    out = []
    for p in req.paths:
        try:
            out.append(ingest_mod.preflight(p))
        except Exception as exc:
            out.append({"path": p, "error": str(exc)})
    return pack(out)


@app.post("/api/v1/datasets/import")
def api_import(req: ImportRequest) -> Dict[str, Any]:
    if not req.paths:
        raise HTTPException(400, "请提供 CSV 路径")
    try:
        res = ingest_mod.ingest_csv(
            req.paths, req.source_name, req.mapping, req.created_by, req.note, req.time_format
        )
    except Exception as exc:
        raise HTTPException(400, f"导入失败：{exc}")
    if req.clean:
        params = default_params("clean")
        params.update(req.clean_params or {})
        dv = req.dataset_version or f"clean_{res['batch_id']}"
        tid = tasks.create_task("clean", dv, params, {}, req.created_by)
        tasks.submit(tid, runner=_clean_runner(res["batch_id"], dv, params))
        res["clean_task_id"] = tid
        res["dataset_version"] = dv
    return pack(res)


def _clean_runner(batch_id: str, dataset_version: str, params: Dict[str, Any]):
    from . import clean as clean_mod

    def _fn(dataset_version: str, task_id: str, params: Dict[str, Any], filters: Dict[str, Any],
            progress=None, **kwargs):
        return clean_mod.build_clean([batch_id], dataset_version, params, progress)

    return _fn


@app.get("/api/v1/datasets")
def api_datasets() -> Dict[str, Any]:
    return pack(dataservice.batches())


@app.post("/api/v1/datasets/upload")
async def api_upload(files: List[UploadFile] = File(...),
                     preflight: bool = True) -> Dict[str, Any]:
    """浏览器直传 CSV（流式落盘到 data/uploads），可选返回预检结果。

    与 POST /datasets/import 分工：本接口只负责把文件安全保存到 RAW 层，
    真正的导入仍需用落盘路径调用 /datasets/import（便于用户先预检再确认）。
    """
    if not files:
        raise HTTPException(400, "请选择要上传的文件")
    saved: List[Dict[str, Any]] = []
    for f in files:
        try:
            p = ingest_mod.save_upload_stream(f.filename or "upload.csv", f.file,
                                              getattr(f, "size", None))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        finally:
            await f.close()
        item: Dict[str, Any] = {"path": str(p), "name": p.name,
                                "size": p.stat().st_size}
        if preflight:
            try:
                pf = ingest_mod.preflight(p)
                item["preflight"] = pf
            except Exception as exc:  # 预检失败不影响文件已落盘
                item["preflight_error"] = str(exc)
        saved.append(item)
    store.audit("dataset_upload", f"upload:{len(saved)}",
                ", ".join(s["name"] for s in saved), "web")
    return pack({"files": saved, "paths": [s["path"] for s in saved]})


@app.get("/api/v1/datasets/{dataset_version}/profile")
def api_profile(dataset_version: str) -> Dict[str, Any]:
    # 未治理完成的数据集直接返回 404，避免向上抛 DuckDB 的 IO 异常（变成 500）
    if not dataservice.dataset_exists(dataset_version):
        raise HTTPException(404, f"数据集 {dataset_version} 不存在或尚未完成治理")
    return pack(dataservice.overview(dataset_version))


@app.get("/api/v1/datasets/{dataset_version}/quality")
def api_quality(dataset_version: str) -> Dict[str, Any]:
    p = settings.clean_root / dataset_version / "quality_report.json"
    if not p.is_file():
        raise HTTPException(404, "质量报告不存在")
    return pack(json.loads(p.read_text(encoding="utf-8")))


@app.get("/api/v1/datasets/{dataset_version}/download/{name}")
def api_download(dataset_version: str, name: str):
    allowed = {"quality_report.json", "staypoints.parquet", "trajectory_segments.parquet",
               "bad_records.parquet", "duplicate_audit.parquet"}
    if name not in allowed:
        raise HTTPException(400, "不允许下载该文件")
    p = settings.clean_root / dataset_version / name
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p), filename=f"{dataset_version}_{name}")


# --------------------------------------------------------------------------- 地图

def _require_dataset(dataset_version: str) -> None:
    """地图类查询的前置校验：数据集未治理完成时直接 404，
    否则 DuckDB 会抛 IO Error（变成 500，前端无法区分「参数错」还是「数据集不存在」）。"""
    if not dataservice.dataset_exists(dataset_version):
        raise HTTPException(404, f"数据集 {dataset_version} 不存在或尚未完成治理")


@app.get("/api/v1/map/overview")
def api_map_overview(dataset_version: Optional[str] = None) -> Dict[str, Any]:
    return pack(dataservice.overview(dataset_version))


@app.get("/api/v1/map/trajectories")
def api_map_traj(
    dataset_version: str,
    bbox: Optional[str] = None,
    t0: Optional[str] = None,
    t1: Optional[str] = None,
    ship_types: Optional[str] = None,
    polygon: Optional[str] = None,
    circle: Optional[str] = None,
    limit: int = 120,
) -> Dict[str, Any]:
    f = _filters(bbox, t0, t1, ship_types, polygon, circle)
    _require_dataset(dataset_version)
    return pack(dataservice.trajectories(dataset_version, f, limit=limit))


@app.get("/api/v1/map/points")
def api_map_points(
    dataset_version: str,
    bbox: Optional[str] = None,
    zoom: int = 8,
    t0: Optional[str] = None,
    t1: Optional[str] = None,
    ship_types: Optional[str] = None,
    polygon: Optional[str] = None,
    circle: Optional[str] = None,
) -> Dict[str, Any]:
    bb = _parse_bbox(bbox)
    tr = [t0, t1] if t0 else None
    st = ship_types.split(",") if ship_types else None
    poly = _parse_polygon(polygon)
    circ = _parse_circle(circle)
    _require_dataset(dataset_version)
    return pack(dataservice.points_map(
        dataset_version, bb, tr, zoom, st, polygon=poly, circle=circ))


@app.get("/api/v1/vessels")
def api_vessels(dataset_version: str, bbox: Optional[str] = None, t0: Optional[str] = None,
                t1: Optional[str] = None, ship_types: Optional[str] = None,
                polygon: Optional[str] = None, circle: Optional[str] = None,
                limit: int = 500) -> Dict[str, Any]:
    f = _filters(bbox, t0, t1, ship_types, polygon, circle)
    _require_dataset(dataset_version)
    return pack(dataservice.vessels(dataset_version, f, limit))


@app.get("/api/v1/vessels/{mmsi}/trajectory")
def api_vessel_traj(mmsi: str, dataset_version: str) -> Dict[str, Any]:
    _require_dataset(dataset_version)
    return pack(dataservice.vessel_track(dataset_version, mmsi))


@app.get("/api/v1/staypoints")
def api_staypoints(dataset_version: str, limit: int = 3000) -> Dict[str, Any]:
    _require_dataset(dataset_version)
    return pack(dataservice.staypoints(dataset_version, limit))


def _parse_bbox(bbox: Optional[str]) -> Optional[List[float]]:
    if not bbox:
        return None
    try:
        bb = [float(x) for x in bbox.split(",")]
    except ValueError:
        raise HTTPException(400, "bbox 必须是逗号分隔的数值")
    if len(bb) != 4:
        raise HTTPException(400, "bbox 需要 4 个数值")
    return bb


def _parse_polygon(raw: Optional[str]) -> Optional[List[List[float]]]:
    """解析多边形：前端传 ``[[lon,lat],...]`` 的 JSON。至少 3 个顶点。"""
    if not raw:
        return None
    try:
        pts = json.loads(raw)
        pts = [[float(p[0]), float(p[1])] for p in pts]
    except (ValueError, TypeError, IndexError):
        raise HTTPException(400, "polygon 需为 [[lon,lat],...] 形式的 JSON 数组")
    if len(pts) < 3:
        raise HTTPException(400, "polygon 至少需要 3 个顶点")
    return pts


def _parse_circle(raw: Optional[str]) -> Optional[List[float]]:
    """解析圆形：``lon,lat,radius_km``。"""
    if not raw:
        return None
    try:
        c = [float(x) for x in raw.split(",")]
    except ValueError:
        raise HTTPException(400, "circle 必须是逗号分隔的数值")
    if len(c) != 3 or c[2] <= 0:
        raise HTTPException(400, "circle 需要 lon,lat,radius_km 且半径大于 0")
    return c


def _filters(bbox, t0, t1, ship_types, polygon=None, circle=None) -> Dict[str, Any]:
    f: Dict[str, Any] = {}
    bb = _parse_bbox(bbox)
    if bb:
        f["bbox"] = bb
    if t0 or t1:
        f["time_range"] = [t0, t1]
    if ship_types:
        f["ship_types"] = ship_types.split(",")
    poly = _parse_polygon(polygon)
    if poly:
        f["polygon"] = poly
    circ = _parse_circle(circle)
    if circ:
        f["circle"] = circ
    return f


# --------------------------------------------------------------------------- 分析任务

class TaskRequest(BaseModel):
    algorithm: str
    dataset_version: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    filters: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"


@app.get("/api/v1/algorithms")
def api_algorithms() -> Dict[str, Any]:
    meta = {
        "hdbscan_hotspot": {"name": "热点区域分析", "queue": "spatial",
                            "desc": "HDBSCAN 密度聚类识别交通流热点与停留热点"},
        "vessel_profile": {"name": "船舶目标画像", "queue": "sequence",
                           "desc": "速度、航向、活动范围、驻留与时间行为统计"},
        "correlate": {"name": "属性关联分析", "queue": "sequence",
                      "desc": "以船舶为单位的相关矩阵、BH 校正与滞后关系"},
        "formation_dtw": {"name": "船舶编群分析", "queue": "sequence",
                          "desc": "DTW + 层次聚类识别同一时段共同航行船舶"},
        "frequent_route": {"name": "频繁轨迹挖掘", "queue": "sequence",
                           "desc": "规则网格、有向转移与连续网格序列（频繁航线）"},
        "predict": {"name": "航迹预测", "queue": "ml-train",
                    "desc": "CV/Kalman 基线与 GRU 模型，ADE/FDE/P90 评估"},
        "clean": {"name": "数据处理", "queue": "ingest",
                  "desc": "去重、异常标记、轨迹切分与质量评分"},
    }
    return pack([
        {"algorithm": k, **v, "default_params": DEFAULT_PARAMS.get(k, {})} for k, v in meta.items()
    ])


@app.post("/api/v1/analysis/tasks")
def api_create_task(req: TaskRequest) -> Dict[str, Any]:
    dv = req.dataset_version or dataservice._latest_dataset()
    if not dv:
        raise HTTPException(400, "没有可用数据集，请先导入 CSV")
    if req.algorithm not in tasks.RUNNERS and req.algorithm != "clean":
        tasks._load_runners()
    tid = tasks.create_task(req.algorithm, dv, req.parameters, req.filters, req.created_by)
    tasks.submit(tid)
    return pack({"task_id": tid, "status": "queued", "queue": tasks.get_task(tid)["queue"]})


@app.get("/api/v1/tasks")
def api_tasks(limit: int = 50, status: Optional[str] = None) -> Dict[str, Any]:
    return pack(tasks.list_tasks(limit, status))


@app.get("/api/v1/tasks/{task_id}")
def api_task(task_id: str) -> Dict[str, Any]:
    t = tasks.get_task(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    t["status_cn"] = STATUS_CN.get(t["status"], t["status"])
    return pack(t)


@app.get("/api/v1/tasks/{task_id}/summary")
def api_task_summary(task_id: str) -> Dict[str, Any]:
    t = tasks.get_task(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    s = tasks.result_summary(task_id)
    if s is None:
        raise HTTPException(409, f"任务尚未产出结果（当前状态：{STATUS_CN.get(t['status'], t['status'])}）")
    return pack(s)


@app.get("/api/v1/tasks/{task_id}/files")
def api_task_files(task_id: str) -> Dict[str, Any]:
    d = settings.results_root / task_id
    if not d.is_dir():
        raise HTTPException(404, "结果目录不存在")
    files = [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(d.iterdir()) if p.is_file()
    ]
    return pack(files)


@app.get("/api/v1/tasks/{task_id}/download/{name}")
def api_task_download(task_id: str, name: str):
    d = settings.results_root / task_id
    p = d / name
    if not p.is_file() or p.parent != d:
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p), filename=name)


@app.get("/api/v1/tasks/{task_id}/report")
def api_task_report(task_id: str, format: str = "html") -> Response:
    """导出分析报告（技术方案 11.4）。

    - format=html：自包含可打印页面，浏览器直接打开即可打印/另存为 PDF；
    - format=md：Markdown 文本，便于归档到 Git 或知会文档；
    - 报告会同时落盘到结果目录，可用 /files 与 /download 取得。
    """
    from . import report as report_mod

    fmt = (format or "html").lower()
    if fmt in ("markdown",):
        fmt = "md"
    if fmt not in ("html", "md"):
        raise HTTPException(400, "format 仅支持 html / md")
    try:
        p = report_mod.build_report(task_id, fmt)
    except ValueError as exc:
        t = tasks.get_task(task_id)
        raise HTTPException(404 if t is None else 409, str(exc))
    media = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
    body = p.read_text(encoding="utf-8")
    headers = {"Content-Disposition": f'inline; filename="{p.name}"'}
    if fmt == "md":
        headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
    return Response(content=body, media_type=media, headers=headers)


@app.post("/api/v1/tasks/{task_id}/cancel")
def api_cancel(task_id: str) -> Dict[str, Any]:
    ok = tasks.request_cancel(task_id)
    if not ok:
        raise HTTPException(400, "任务无法取消（可能已结束）")
    return pack({"task_id": task_id, "cancel_requested": True})


@app.get("/api/v1/tasks/{task_id}/events")
def api_task_events(task_id: str) -> StreamingResponse:
    """SSE 任务状态推送。"""

    async def gen():
        import asyncio

        last = None
        for _ in range(3600):
            t = tasks.get_task(task_id)
            if t is None:
                yield "event: error\ndata: {\"message\":\"任务不存在\"}\n\n"
                return
            payload = {k: t[k] for k in ("task_id", "status", "progress", "current_stage",
                                         "error_message")}
            body = json.dumps(payload, ensure_ascii=False)
            if body != last:
                yield f"event: progress\ndata: {body}\n\n"
                last = body
            if t["status"] in ("succeeded", "failed", "cancelled"):
                yield f"event: done\ndata: {body}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- 系统

@app.get("/api/v1/admin/meta")
def api_meta() -> Dict[str, Any]:
    return pack({
        "data_dictionary": dataservice.data_dictionary(),
        "queue_limits": tasks.QUEUE_LIMITS,
        "task_queues": __import__("aisp.config", fromlist=["TASK_QUEUES"]).TASK_QUEUES,
        "datasets": dataservice._latest_dataset(),
    })


@app.get("/api/v1/admin/audit")
def api_audit(limit: int = 100) -> Dict[str, Any]:
    df = store.q("SELECT * FROM audit_log ORDER BY event_time DESC LIMIT ?", [limit])
    df["event_time"] = df["event_time"].astype(str)
    return pack(df.to_dict(orient="records"))


@app.get("/api/v1/admin/config")
def api_config() -> Dict[str, Any]:
    return pack({"settings": settings.as_dict(), "defaults": DEFAULT_PARAMS})


# --------------------------------------------------------- 模型监控（10.10）

@app.get("/api/v1/models")
def api_models() -> Dict[str, Any]:
    """已登记模型卡列表（含最新漂移结论）。"""
    from . import modelmon

    return pack(modelmon.list_models())


@app.get("/api/v1/models/{model_id}")
def api_model_detail(model_id: str) -> Dict[str, Any]:
    """模型卡 + 监控日志 + 回填误差趋势。"""
    from . import modelmon

    d = modelmon.model_detail(model_id)
    if d is None:
        raise HTTPException(404, f"模型 {model_id} 不存在或未生成模型卡")
    return pack(d)


class DriftRequest(BaseModel):
    """漂移检测：拿参照分布（训练期）与新数据对比。"""

    dataset_version: str
    reference_dataset: Optional[str] = None
    features: Optional[List[str]] = None
    sample: int = Field(20000, ge=500, le=200000)


@app.post("/api/v1/models/{model_id}/drift")
def api_model_drift(model_id: str, req: DriftRequest) -> Dict[str, Any]:
    """用 PSI 对比参照数据集与新数据集的输入分布（技术方案 10.10）。"""
    from . import modelmon

    d = modelmon.model_detail(model_id)
    if d is None:
        raise HTTPException(404, f"模型 {model_id} 不存在或未生成模型卡")
    _require_dataset(req.dataset_version)
    ref_dv = req.reference_dataset or (d["card"].get("training_data") or {}).get("dataset_version")
    if not ref_dv:
        raise HTTPException(400, "缺少参照数据集，请在请求中指定 reference_dataset")
    _require_dataset(ref_dv)

    feats = req.features or (d["card"].get("monitoring") or {}).get("watch_features") \
        or modelmon.DEFAULT_WATCH_FEATURES
    try:
        ref_df = dataservice.sample_features(ref_dv, feats, req.sample)
        cur_df = dataservice.sample_features(req.dataset_version, feats, req.sample)
    except Exception as exc:
        raise HTTPException(400, f"读取样本失败：{exc}")

    rows = modelmon.distribution_shift(ref_df, cur_df, feats)
    verdict = modelmon.drift_verdict(rows)
    # 参照与当前是同一版本时 PSI 必为 0，这种“自比”只能验证流程通，不能作为上线依据，
    # 需要显式告知使用者，否则会被误读成“分布完全稳定”。
    same = ref_dv == req.dataset_version
    if same:
        verdict = dict(verdict)
        verdict["message"] = (
            f"参照与当前均为 {ref_dv}（自比），PSI 必然为 0，仅供验证监控链路；"
            "请指定 reference_dataset 为训练期数据集、current 为新数据后再判定"
        )
        verdict["self_compare"] = True
    modelmon.record_monitor_point(model_id, drift=rows, verdict=verdict, note="手动漂移检测")
    store.audit("model_drift_check", f"model:{model_id}",
                f"ref={ref_dv} cur={req.dataset_version} action={verdict['action']}")
    return pack({"model_id": model_id, "reference_dataset": ref_dv,
                 "current_dataset": req.dataset_version, "self_compare": same,
                 "features": rows, "verdict": verdict})


class MonitorPointRequest(BaseModel):
    """手工登记一条监控点（推理延迟 / 回填误差 / 覆盖率）。"""

    n_predictions: int = 0
    n_backfilled: int = 0
    latency_ms: Optional[float] = None
    backfill_ade_m: Optional[float] = None
    missing_rate: Optional[float] = None
    note: str = ""
    # 补录历史监控点时指定上报时间；留空则用当前时间。
    # 不回填时间会让趋势图把所有点挤在同一时刻，无法看变化。
    ts: Optional[str] = None


@app.post("/api/v1/models/{model_id}/monitor")
def api_model_monitor(model_id: str, req: MonitorPointRequest) -> Dict[str, Any]:
    """追加一条监控点，并返回更新后的误差趋势。"""
    from . import modelmon

    d = modelmon.model_detail(model_id)
    if d is None:
        raise HTTPException(404, f"模型 {model_id} 不存在或未生成模型卡")
    try:
        pt = modelmon.record_monitor_point(
            model_id,
            n_predictions=req.n_predictions,
            n_backfilled=req.n_backfilled,
            latency_ms=req.latency_ms,
            backfill_ade_m=req.backfill_ade_m,
            missing_rate=req.missing_rate,
            note=req.note,
            ts=req.ts,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log = modelmon.load_monitor_log(model_id)
    base = ((d["card"].get("evaluation") or {}).get("gru") or {}).get("trimmed_ADE_m")
    return pack({"point": pt, "error_trend": modelmon.error_trend(log, base)})


@app.get("/api/v1/whoami")
def api_whoami(request: Request) -> Dict[str, Any]:
    """当前请求的身份与权限集（前端据此隐藏无权操作的按钮）。"""
    ident: auth_mod.Identity = getattr(request.state, "identity", None) or \
        auth_mod.identity_from_headers(dict(request.headers))
    return pack({
        "identity": ident.to_dict(),
        "auth_enabled": auth_mod.auth_enabled(),
        "permissions": sorted(auth_mod.ROLE_PERMS.get(ident.role, set())),
        "roles": {r: sorted(p) for r, p in auth_mod.ROLE_PERMS.items()},
    })


class BackupRequest(BaseModel):
    include_clean: bool = False
    dataset_versions: Optional[List[str]] = None


@app.post("/api/v1/admin/backup")
def api_backup(req: BackupRequest) -> Dict[str, Any]:
    """生成备份包（默认仅元数据库；include_clean=True 时含 CLEAN Parquet）。"""
    from . import backup as backup_mod

    try:
        m = backup_mod.create_backup(include_clean=req.include_clean,
                                    dataset_versions=req.dataset_versions)
    except Exception as exc:  # 磁盘满、文件锁等
        raise HTTPException(500, f"备份失败：{exc}")
    store.audit("backup_create", "backup", f"zip={m['zip_path']} clean={req.include_clean}")
    return pack(m)


@app.get("/api/v1/admin/backups")
def api_backups() -> Dict[str, Any]:
    from . import backup as backup_mod

    return pack(backup_mod.list_backups())


@app.get("/api/v1/admin/backups/{name}")
def api_backup_download(name: str) -> FileResponse:
    """下载备份包；文件名严格校验，禁止路径穿越。"""
    from . import backup as backup_mod

    if "/" in name or "\\" in name or not name.startswith("aisp_backup_"):
        raise HTTPException(400, "非法的备份文件名")
    p = (settings.artifacts_dir / "backups" / name).resolve()
    root = (settings.artifacts_dir / "backups").resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "备份包不存在")
    return FileResponse(str(p), media_type="application/zip", filename=name)


class RestoreRequest(BaseModel):
    name: str
    restore_clean: bool = False
    overwrite: bool = False


@app.post("/api/v1/admin/restore")
def api_restore(req: RestoreRequest) -> Dict[str, Any]:
    """从备份包恢复元数据库（可选恢复 CLEAN 明细）。"""
    from . import backup as backup_mod

    if "/" in req.name or "\\" in req.name or not req.name.startswith("aisp_backup_"):
        raise HTTPException(400, "非法的备份文件名")
    p = (settings.artifacts_dir / "backups" / req.name).resolve()
    root = (settings.artifacts_dir / "backups").resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "备份包不存在")
    try:
        out = backup_mod.restore_backup(p, restore_clean=req.restore_clean,
                                        overwrite=req.overwrite)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"恢复失败：{exc}")
    store.audit("backup_restore", "backup", f"zip={req.name} clean={req.restore_clean}")
    return pack(out)


@app.get("/api/v1/health")
def api_health() -> Dict[str, Any]:
    return pack({"status": "ok", "dataset": dataservice._latest_dataset()})


# --------------------------------------------------------------------------- 静态前端

class _NoCacheStatic(StaticFiles):
    """开发期静态资源：禁用浏览器缓存，避免前端修改后看到旧版本。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp


def _mount_static() -> None:
    web = settings.web_dir
    if not web.is_dir():
        return
    if (web / "vendor").is_dir():
        app.mount("/vendor", _NoCacheStatic(directory=str(web / "vendor")), name="vendor")
    if (web / "assets").is_dir():
        app.mount("/assets", _NoCacheStatic(directory=str(web / "assets")), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        idx = web / "index.html"
        if not idx.is_file():
            raise HTTPException(404, "前端未构建")
        return HTMLResponse(idx.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    def favicon():
        p = web / "favicon.svg"
        if p.is_file():
            return FileResponse(str(p), media_type="image/svg+xml")
        return Response(status_code=204)


def _seed_demo_if_needed() -> None:
    """首次启动时，若 data/ 目录有 CSV 且没有数据集，提示但不自动导入（避免意外长任务）。"""
    if dataservice._latest_dataset():
        return
    csvs = sorted(settings.data_dir.glob("*.csv"))
    if csvs:
        log.info("检测到 %d 个 CSV 文件，可通过 POST /api/v1/datasets/import 导入", len(csvs))


_mount_static()
