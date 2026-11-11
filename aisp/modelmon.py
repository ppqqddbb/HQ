"""模型卡与上线监控（技术方案 10.10）。

方案要求的两个交付物：
1. **模型卡**：训练数据范围、区域/船型覆盖、评估指标、失效场景、特征清单、
   依赖版本、授权与负责人。训练结束时自动写入，也可事后补登。
2. **上线监控**：输入分布漂移、缺失率、推理延迟、预测覆盖率、实际回填误差；
   误差持续恶化或分布变化时触发「重新训练 / 降级到基线」建议。

设计取舍：
- 不引入 MLflow 等外部依赖，模型卡与监控点都以 JSON 落盘在
  ``artifacts/models/<model_id>/``，可直接进备份包，也能被审计追溯；
- 漂移检测用 **PSI（群体稳定性指数）**：分箱固定、无需额外依赖，
  对「增速类」指标（航速 / 转向率 / 报文间隔）足够敏感且可解释；
- 所有阈值给默认值并允许配置，避免把业务判断写死在代码里。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import settings
from . import store

# ---------------------------------------------------------------- 常量与阈值

#: PSI 的经验阈值（业内通用）：<0.1 稳定，0.1~0.25 需关注，>0.25 显著漂移
PSI_WARN = 0.10
PSI_ALERT = 0.25

#: 回填误差相对首评恶化多少算「持续恶化」
ERR_WORSEN_PCT = 20.0

#: 默认监控的输入特征（与 predict 的特征清单保持一致）
DEFAULT_WATCH_FEATURES = ["sog", "cog", "rot", "heading", "report_gap_s"]

FEATURE_LABELS = {
    "sog": "对地航速 SOG (kn)",
    "cog": "对地航向 COG (°)",
    "heading": "船首向 (°)",
    "rot": "转向率 ROT (°/min)",
    "report_gap_s": "报文间隔 (s)",
    "draught": "吃水 (m)",
    "length": "船长 (m)",
}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _norm_ts(ts: Optional[str]) -> Optional[str]:
    """宽松解析用户传入的上报时间，统一成 ``YYYY-MM-DD HH:MM:SS``。

    支持 ``2026-11-12``、``2026-11-12 08:30``、ISO 的 ``T`` 分隔与末尾 ``Z``。
    解析失败直接报 ValueError，避免脏时间默默落盘后把趋势图顺序搞乱。
    """
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    s = s.replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"无法识别的时间格式：{ts}（示例：2026-11-12 08:30）")


def _model_dir(model_id: str) -> Path:
    d = settings.models_root / model_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(s))[:120]


# ---------------------------------------------------------------- 模型卡

def _dataset_span(dataset_version: str) -> Dict[str, Any]:
    """取数据集的时间范围、区域范围与船型覆盖（用于模型卡的「适用边界」）。"""
    out: Dict[str, Any] = {"dataset_version": dataset_version}
    try:
        df = store.q(
            "SELECT row_count, mmsi_count, created_at FROM dataset_version WHERE dataset_version = ?",
            [dataset_version],
        )
        if not df.empty:
            out["row_count"] = int(df.iloc[0]["row_count"] or 0)
            out["mmsi_count"] = int(df.iloc[0]["mmsi_count"] or 0)
    except Exception:
        pass
    return out


def build_model_card(
    task_id: str,
    summary: Dict[str, Any],
    *,
    owner: str = "system",
    license_note: str = "内部使用；船舶关联数据的使用范围由业务与合规部门确认",
    feature_list: Optional[Sequence[str]] = None,
    failure_modes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """根据预测任务的 summary 生成模型卡（技术方案 10.10）。

    只依赖任务 summary，不重新训练，因此可在任务结束后随时补生成。
    """
    cfg = summary.get("config") or {}
    metrics = summary.get("metrics") or {}
    verdict = summary.get("verdict") or {}
    gru = summary.get("gru") or {}
    dv = summary.get("dataset_version")

    gru_m = metrics.get("gru") or {}
    base_name = verdict.get("baseline") or "cv"
    base_m = metrics.get(base_name) or {}

    card: Dict[str, Any] = {
        "model_id": gru.get("artifact") or f"{task_id}.pt",
        "task_id": task_id,
        "algorithm": summary.get("algorithm", "predict"),
        "created_at": _now_str(),
        "owner": owner,
        "license": license_note,
        # 1) 训练数据时间范围与区域/船型覆盖
        "training_data": {
            "dataset_version": dv,
            "time_range": (summary.get("data_range") or {}).get("time_range"),
            "region": (summary.get("data_range") or {}).get("bbox"),
            "n_trajectories": cfg.get("n_trajectories"),
            "n_windows": cfg.get("n_windows"),
            "n_train": cfg.get("n_train"),
            "n_test": cfg.get("n_test"),
            "step_minutes": cfg.get("step_minutes"),
            "input_minutes": cfg.get("input_minutes"),
            "horizon_minutes": cfg.get("horizon_minutes"),
            "min_quality_score": cfg.get("min_quality_score"),
            "split": summary.get("split"),
            "coverage": _coverage_from_stratified(summary.get("stratified")),
        },
        # 2) 评估指标（与基线并列，便于准入复核）
        "evaluation": {
            "gru": {k: gru_m.get(k) for k in
                    ("ADE_m", "trimmed_ADE_m", "median_m", "FDE_m", "P90_m", "n_test")},
            "baseline": {"model": base_name,
                         **{k: base_m.get(k) for k in
                            ("ADE_m", "trimmed_ADE_m", "median_m", "FDE_m", "P90_m")}},
            "verdict": verdict,
        },
        # 3) 特征清单与依赖版本
        "features": list(feature_list or DEFAULT_WATCH_FEATURES),
        "dependencies": _dependency_versions(),
        "training": {
            "epochs": gru.get("epochs"), "hidden": gru.get("hidden"),
            "n_params": gru.get("n_params"),
            "initial_loss": gru.get("initial_loss"), "final_loss": gru.get("final_loss"),
        },
        # 4) 失效场景（可由调用方补充；默认给一组基于方案与实测的说明）
        "failure_modes": list(failure_modes or DEFAULT_FAILURE_MODES),
        "monitoring": {
            "watch_features": list(feature_list or DEFAULT_WATCH_FEATURES),
            "psi_warn": PSI_WARN, "psi_alert": PSI_ALERT,
            "error_worsen_pct": ERR_WORSEN_PCT,
        },
    }
    d = _model_dir(_safe_name(card["model_id"].replace(".pt", "")))
    (d / "model_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return card


DEFAULT_FAILURE_MODES = [
    "静止或极低速目标：位置几乎不动，方向信息退化，预测位移意义有限（看 stratified 的静止分组）。",
    "高速目标（>14 kn）：30 分钟外推距离大，误差随航速近似线性放大。",
    "稀疏报文：报文间隔超过 2×步长时，重采样窗口内插值占比高，误差偏大。",
    "频繁转向/机动：本模型为单一轨迹回归，未建模「下一步转向意图」，急转处误差峰值明显。",
    "跨区域迁移：训练区域外的航道拓扑不同，需重新训练后再上线。",
]


def _coverage_from_stratified(stratified: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从分层指标里提取船型/航速覆盖情况，写进模型卡的「覆盖」字段。"""
    if not stratified:
        return {}
    gru = stratified.get("gru") or {}
    out: Dict[str, Any] = {}
    for key in ("by_ship_type", "by_speed"):
        block = gru.get(key) or {}
        if block:
            out[key] = {k: {"n": v.get("n"), "trimmed_ADE_m": v.get("trimmed_ADE_m")}
                        for k, v in block.items()}
    return out


def _dependency_versions() -> Dict[str, str]:
    """记录关键依赖版本，便于复现（缺包时静默跳过）。"""
    out: Dict[str, str] = {}
    for mod in ("numpy", "pandas", "duckdb", "torch", "scipy", "fastapi"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 漂移检测

def psi(reference: Sequence[float], current: Sequence[float], bins: int = 10) -> Optional[float]:
    """计算群体稳定性指数 PSI。

    PSI = Σ (actual% - expected%) * ln(actual% / expected%)

    约定：
    - 以 reference 的分位点切箱（保证参照分布每箱样本量均衡）；
    - 空箱用 1e-4 兜底，避免 ln(0)；
    - 任一侧样本不足 20 时返回 None（样本太少时 PSI 无统计意义）。
    """
    ref = np.asarray([v for v in reference if v is not None and np.isfinite(v)], dtype=float)
    cur = np.asarray([v for v in current if v is not None and np.isfinite(v)], dtype=float)
    if ref.size < 20 or cur.size < 20:
        return None
    qs = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if qs.size < 3:                    # 参照分布几乎没有变化（常量），退化成两箱
        qs = np.unique(np.array([ref.min(), ref.max() + 1e-9]))
        if qs.size < 2:
            return 0.0
    edges = qs.copy()
    edges[0], edges[-1] = -np.inf, np.inf
    ref_pct = np.histogram(ref, bins=edges)[0] / ref.size
    cur_pct = np.histogram(cur, bins=edges)[0] / cur.size
    eps = 1e-4
    ref_pct = np.clip(ref_pct, eps, None)
    cur_pct = np.clip(cur_pct, eps, None)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def psi_level(v: Optional[float]) -> str:
    if v is None:
        return "insufficient"
    if v >= PSI_ALERT:
        return "alert"
    if v >= PSI_WARN:
        return "warn"
    return "stable"


def distribution_shift(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    features: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """逐特征计算 PSI 与缺失率变化，返回可直接渲染的列表。"""
    feats = [f for f in (features or DEFAULT_WATCH_FEATURES) if f in reference.columns or f in current.columns]
    rows: List[Dict[str, Any]] = []
    for f in feats:
        ref = reference[f].to_numpy(dtype=float) if f in reference.columns else np.array([])
        cur = current[f].to_numpy(dtype=float) if f in current.columns else np.array([])
        ref_nan = float(np.mean(~np.isfinite(ref))) if ref.size else None
        cur_nan = float(np.mean(~np.isfinite(cur))) if cur.size else None
        v = psi(ref, cur)
        rows.append({
            "feature": f,
            "label": FEATURE_LABELS.get(f, f),
            "psi": None if v is None else round(v, 4),
            "level": psi_level(v),
            "n_reference": int(ref.size),
            "n_current": int(cur.size),
            "missing_reference": None if ref_nan is None else round(ref_nan, 4),
            "missing_current": None if cur_nan is None else round(cur_nan, 4),
        })
    return rows


def drift_verdict(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐特征 PSI 汇总成一个可直接展示的结论。"""
    alerts = [r for r in rows if r["level"] == "alert"]
    warns = [r for r in rows if r["level"] == "warn"]
    if alerts:
        action = "retrain"
        text = f"{len(alerts)} 个特征显著漂移（PSI≥{PSI_ALERT}），建议重新训练或降级到基线模型"
    elif warns:
        action = "watch"
        text = f"{len(warns)} 个特征轻度漂移（PSI≥{PSI_WARN}），建议加密监控"
    elif any(r["level"] == "stable" for r in rows):
        action = "ok"
        text = "输入分布稳定，可继续在线使用"
    else:
        action = "insufficient"
        text = "样本不足，无法判定漂移"
    return {"action": action, "message": text,
            "n_alert": len(alerts), "n_warn": len(warns)}


# ---------------------------------------------------------------- 监控记录

def _monitor_file(model_id: str) -> Path:
    return _model_dir(_safe_name(model_id)) / "monitor_log.json"


def load_monitor_log(model_id: str) -> Dict[str, Any]:
    p = _monitor_file(model_id)
    if not p.is_file():
        return {"model_id": model_id, "points": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"model_id": model_id, "points": []}


def record_monitor_point(
    model_id: str,
    *,
    n_predictions: int = 0,
    n_backfilled: int = 0,
    latency_ms: Optional[float] = None,
    backfill_ade_m: Optional[float] = None,
    missing_rate: Optional[float] = None,
    drift: Optional[List[Dict[str, Any]]] = None,
    verdict: Optional[Dict[str, Any]] = None,
    note: str = "",
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """追加一条监控点：预测覆盖、推理延迟、回填误差、漂移摘要。

    ``verdict`` 用于把漂移结论（含“自比提醒”这类上下文）一并落盘，
    否则页面刷新后只剩逐特征表，结论会丢失。
    """
    log = load_monitor_log(model_id)
    pts: List[Dict[str, Any]] = log.get("points", [])
    coverage = None
    if n_predictions:
        coverage = round(max(0.0, min(1.0, n_backfilled / float(n_predictions))), 4)
    v = verdict if verdict is not None else (drift_verdict(drift) if drift else None)
    point = {
        "ts": _norm_ts(ts) or _now_str(),
        "n_predictions": int(n_predictions),
        "n_backfilled": int(n_backfilled),
        "coverage": coverage,
        "latency_ms": None if latency_ms is None else round(float(latency_ms), 2),
        "backfill_ade_m": None if backfill_ade_m is None else round(float(backfill_ade_m), 2),
        "missing_rate": None if missing_rate is None else round(float(missing_rate), 4),
        "drift": drift[:] if drift else [],
        "drift_action": (v or {}).get("action"),
        "drift_verdict": v,
        "note": note,
    }
    pts.append(point)
    # 按时间排序，允许事后补录历史监控点而不打乱趋势
    pts.sort(key=lambda p: str(p.get("ts") or ""))
    log = {"model_id": model_id, "points": pts[-200:]}     # 只留最近 200 条
    _monitor_file(model_id).write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return point


def error_trend(log: Dict[str, Any], first_ade: Optional[float] = None) -> Dict[str, Any]:
    """回填误差趋势：与首评（或配置的基线）对比，判断是否「持续恶化」。"""
    pts = [p for p in log.get("points", []) if p.get("backfill_ade_m") is not None]
    if not pts:
        return {"available": False, "message": "暂无回填误差记录"}
    series = [{"ts": p["ts"], "ade_m": p["backfill_ade_m"]} for p in pts]
    base = first_ade if first_ade is not None else series[0]["ade_m"]
    latest = series[-1]["ade_m"]
    worsen = None if not base else round((latest - base) / base * 100, 2)
    worsened = worsen is not None and worsen >= ERR_WORSEN_PCT
    return {
        "available": True,
        "baseline_ade_m": base,
        "latest_ade_m": latest,
        "worsen_pct": worsen,
        "worsened": worsened,
        "series": series,
        "message": (f"回填误差较首评恶化 {worsen}%，超过 {ERR_WORSEN_PCT}% 阈值，建议重新训练"
                    if worsened else "回填误差在阈值内"),
    }


def list_models() -> List[Dict[str, Any]]:
    """列出 artifacts/models 下有模型卡的模型（按创建时间倒序）。"""
    root = settings.models_root
    if not root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        card_p = d / "model_card.json"
        if not card_p.is_file():
            continue
        try:
            card = json.loads(card_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        log = load_monitor_log(d.name)
        ev = (card.get("evaluation") or {})
        out.append({
            "model_id": card.get("model_id") or d.name,
            "dir": d.name,
            "task_id": card.get("task_id"),
            "created_at": card.get("created_at"),
            "owner": card.get("owner"),
            "gru_trimmed_ade_m": (ev.get("gru") or {}).get("trimmed_ADE_m"),
            "baseline_trimmed_ade_m": (ev.get("baseline") or {}).get("trimmed_ADE_m"),
            "n_monitor_points": len(log.get("points", [])),
            "latest_drift_action": next(
                (p.get("drift_action") for p in reversed(log.get("points", []))
                 if p.get("drift_action")), None),
        })
    return sorted(out, key=lambda x: str(x.get("created_at") or ""), reverse=True)


def model_detail(model_id: str) -> Optional[Dict[str, Any]]:
    """读取模型卡 + 监控日志 + 误差趋势，供监控页展示。"""
    d = settings.models_root / _safe_name(model_id)
    card_p = d / "model_card.json"
    if not card_p.is_file():
        return None
    card = json.loads(card_p.read_text(encoding="utf-8"))
    log = load_monitor_log(_safe_name(model_id))
    ev = (card.get("evaluation") or {}).get("gru") or {}
    return {
        "card": card,
        "log": log,
        "error_trend": error_trend(log, ev.get("trimmed_ADE_m")),
        "latest_drift": next(
            (p.get("drift") for p in reversed(log.get("points", [])) if p.get("drift")), []),
        # 漂移结论同样要回传，页面刷新后才知道“结果是否有效”（例如自比提醒）
        "latest_drift_verdict": next(
            (p.get("drift_verdict") for p in reversed(log.get("points", []))
             if p.get("drift_verdict")), None),
    }
