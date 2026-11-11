"""船舶航迹预测：CV / Kalman 基线 + GRU 深度学习模型。

策略（技术方案第 10 章）：
1. 先实现常速度（CV）与卡尔曼滤波基线，确认坐标转换、切窗与评估流程正确；
2. 再训练 GRU 序列模型，直接预测未来每个时间步的局部 ENU 位移；
3. 所有模型在同一测试集、同一预测步长上比较 ADE / FDE / P90；
4. 数据切分按时间外推（较早时间训练，后续时间测试），归一化参数只在训练集拟合；
5. 评估同时给出均值与截尾均值，避免少数静止漂移/异常轨迹淹没整体结论。
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import settings
from .common import Filter, jsonable, open_dataset, write_result

ProgressCb = Optional[Callable[[float, str], None]]

MAX_SPEED_KN = 60.0
EARTH_R = 6371008.8


def _p(cb: ProgressCb, v: float, s: str) -> None:
    if cb:
        cb(v, s)


# --------------------------------------------------------------------------- 基线模型

def cv_predict(hist_xy: np.ndarray, hist_t: np.ndarray, horizon_t: np.ndarray,
               max_speed_mps: float = MAX_SPEED_KN * 0.514444) -> np.ndarray:
    """常速度模型：最近 5 点线性回归估计速度矢量，并做物理速度上限约束。"""
    n = len(hist_xy)
    if n < 2:
        return np.repeat(hist_xy[-1:], len(horizon_t), axis=0)
    k = min(5, n)
    x = hist_xy[-k:]
    t = hist_t[-k:].astype(float)
    span = t[-1] - t[0]
    if span <= 0:
        return np.repeat(hist_xy[-1:], len(horizon_t), axis=0)
    vx = float(np.polyfit(t - t[0], x[:, 0], 1)[0])
    vy = float(np.polyfit(t - t[0], x[:, 1], 1)[0])
    v = math.hypot(vx, vy)
    if v > max_speed_mps:  # 速度不可能超过物理上限，按比例压缩
        scale = max_speed_mps / v
        vx, vy = vx * scale, vy * scale
    dtt = horizon_t.astype(float) - t[-1]
    return np.stack([x[-1, 0] + vx * dtt, x[-1, 1] + vy * dtt], axis=1)


def kalman_predict(hist_xy: np.ndarray, hist_t: np.ndarray, horizon_t: np.ndarray,
                   q: float = 1.0, r: float = 25.0,
                   max_speed_mps: float = MAX_SPEED_KN * 0.514444) -> np.ndarray:
    """常速度卡尔曼滤波（状态 [x, y, vx, vy]），按不等间隔 Δt 做预测。"""
    n = len(hist_xy)
    if n < 2:
        return np.repeat(hist_xy[-1:], len(horizon_t), axis=0)
    dt0 = np.diff(hist_t.astype(float))
    dt0[dt0 <= 0] = 1.0
    v0 = np.diff(hist_xy, axis=0) / dt0[:, None]
    state = np.array([hist_xy[-1, 0], hist_xy[-1, 1],
                      float(np.median(v0[-3:, 0])), float(np.median(v0[-3:, 1]))])
    P = np.eye(4) * 50.0
    H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])
    Rm = np.eye(2) * r
    for i in range(1, n):
        dt = max(1.0, float(hist_t[i] - hist_t[i - 1]))
        F = np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
        Q = np.eye(4) * q * dt
        state = F @ state
        P = F @ P @ F.T + Q
        y = hist_xy[i] - H @ state
        S = H @ P @ H.T + Rm
        K = P @ H.T @ np.linalg.inv(S)
        state = state + K @ y
        P = (np.eye(4) - K @ H) @ P
    v = math.hypot(state[2], state[3])
    if v > max_speed_mps:
        s = max_speed_mps / v
        state[2] *= s
        state[3] *= s
    t_last = float(hist_t[-1])
    return np.array([[state[0] + state[2] * (float(tt) - t_last),
                      state[1] + state[3] * (float(tt) - t_last)] for tt in horizon_t])


# --------------------------------------------------------------------------- 数据准备

def _hav_m(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _resample_segments(df: pd.DataFrame, step_s: int, min_len: int) -> List[pd.DataFrame]:
    """逐轨迹段重采样到固定时间栅格；长缺口处切断，不跨越插值。"""
    frames: List[pd.DataFrame] = []
    for tid, g in df.groupby("traj_id", sort=False):
        t = g["event_time"].to_numpy(dtype="datetime64[s]").astype("int64")
        if len(t) < 4:
            continue
        lon = g["lon"].to_numpy(dtype=float)
        lat = g["lat"].to_numpy(dtype=float)
        sog = pd.Series(g["sog"].to_numpy(dtype=float)).ffill().bfill().fillna(0.0).to_numpy()
        cog = pd.Series(g["cog"].to_numpy(dtype=float)).ffill().bfill().fillna(0.0).to_numpy()

        # 以缺口切分为若干连续观测块
        breaks = np.where(np.diff(t) > step_s * 4)[0]
        starts = np.concatenate([[0], breaks + 1])
        ends = np.concatenate([breaks + 1, [len(t)]])
        for a, b in zip(starts, ends):
            if b - a < 4:
                continue
            tt = t[a:b]
            sub_lon, sub_lat = lon[a:b], lat[a:b]
            # 长距离跳点剔除（单步 > 30km 视为定位异常）
            if any(_hav_m(sub_lon[i], sub_lat[i], sub_lon[i + 1], sub_lat[i + 1]) > 30000
                   for i in range(len(sub_lon) - 1)):
                continue
            grid = np.arange(tt[0], tt[-1] + 1, step_s)
            if len(grid) < min_len or len(grid) > 3000:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "traj_id": str(tid),
                        "event_time": grid.astype("datetime64[s]"),
                        "lon": np.interp(grid, tt, sub_lon),
                        "lat": np.interp(grid, tt, sub_lat),
                        "sog": np.interp(grid, tt, sog[a:b]),
                        "cog": np.interp(grid, tt, cog[a:b]),
                        "block": f"{tid}:{a}-{b}",
                    }
                )
            )
    return frames


def _build_windows(rs: pd.DataFrame, input_steps: int, horizon_steps: int):
    """按连续块切窗，返回 dict：X(history xy)/TH(h_t)/Y(future xy)/TF(f_t)/SOG/COG/ids。"""
    X, TH, Y, TF, SOG, COG, TID, T0 = [], [], [], [], [], [], [], []
    needed = input_steps + horizon_steps
    for key, g in rs.groupby("block", sort=False):
        n = len(g)
        if n < needed:
            continue
        t = g["event_time"].to_numpy(dtype="datetime64[s]").astype("int64").astype(float)
        lon = g["lon"].to_numpy(dtype=float)
        lat = g["lat"].to_numpy(dtype=float)
        kx = 111320.0 * math.cos(math.radians(float(np.median(lat))))
        x = (lon - lon[0]) * kx
        y = (lat - lat[0]) * 110574.0
        sog = g["sog"].to_numpy(dtype=float)
        cog = g["cog"].to_numpy(dtype=float)
        stride = max(1, horizon_steps)
        for i in range(0, n - needed + 1, stride):
            hi = i + input_steps
            fi = hi + horizon_steps
            X.append(np.stack([x[i:hi], y[i:hi]], axis=1))
            TH.append(t[i:hi] - t[i])
            Y.append(np.stack([x[hi:fi], y[hi:fi]], axis=1))
            TF.append(t[hi:fi] - t[hi - 1])
            SOG.append(sog[i:hi])
            COG.append(cog[i:hi])
            TID.append(g["traj_id"].iloc[0])
            T0.append(g["event_time"].iloc[i])
    return {
        "X": np.array(X, dtype=np.float64) if X else np.zeros((0, input_steps, 2)),
        "TH": np.array(TH) if TH else np.zeros((0, input_steps)),
        "Y": np.array(Y, dtype=np.float64) if Y else np.zeros((0, horizon_steps, 2)),
        "TF": np.array(TF) if TF else np.zeros((0, horizon_steps)),
        "SOG": np.array(SOG) if SOG else np.zeros((0, input_steps)),
        "COG": np.array(COG) if COG else np.zeros((0, input_steps)),
        "TID": TID,
        "T0": np.array(T0) if T0 else np.zeros(0, dtype="datetime64[s]"),
    }


def _gru_factory(hidden: int, out_dim: int, in_dim: int = 7):
    import torch

    class GRUModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = torch.nn.GRU(input_size=in_dim, hidden_size=hidden, num_layers=2,
                                    batch_first=True, dropout=0.1)
            self.head = torch.nn.Sequential(
                torch.nn.Linear(hidden, hidden), torch.nn.GELU(),
                torch.nn.Linear(hidden, out_dim),
            )

        def forward(self, x):
            out, _ = self.gru(x)
            return self.head(out[:, -1, :])

    return GRUModel()


def _trimmed_stats(err: np.ndarray) -> Dict[str, float]:
    flat = err.reshape(-1)
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return {"ADE_m": None, "FDE_m": None, "P90_m": None, "trimmed_ADE_m": None, "median_m": None}
    return {
        "ADE_m": round(float(flat.mean()), 2),
        "trimmed_ADE_m": round(float(_trimmed_mean(flat, 0.95)), 2),
        "median_m": round(float(np.median(flat)), 2),
        "FDE_m": round(float(np.nanmean(err[:, -1])), 2),
        "P90_m": round(float(np.percentile(flat, 90)), 2),
    }


def _trimmed_mean(v: np.ndarray, cutoff: float) -> float:
    lo, hi = np.quantile(v, 1 - cutoff), np.quantile(v, cutoff)
    sel = v[(v >= lo) & (v <= hi)]
    return float(sel.mean()) if sel.size else float(v.mean())


def run(
    dataset_version: str,
    task_id: str,
    params: Dict[str, Any],
    filters: Optional[Dict[str, Any]] = None,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    flt = Filter(filters)
    con = open_dataset(dataset_version)
    try:
        step_min = int(params.get("step_minutes", 5))
        input_min = int(params.get("input_minutes", 60))
        horizon_min = int(params.get("horizon_minutes", 30))
        step_s = step_min * 60
        input_steps = max(4, input_min // max(1, step_min))
        horizon_steps = max(1, horizon_min // max(1, step_min))
        max_windows = int(params.get("max_windows", 60000))
        seed = int(params.get("seed", 42))
        models = list(params.get("models", ["cv", "kalman", "gru"]))
        epochs = int(params.get("gru_epochs", 40))
        hidden = int(params.get("gru_hidden", 64))
        min_q = float(params.get("min_quality_score", 60.0))
        max_traj = int(params.get("max_trajectories", 800))

        _p(progress, 0.08, f"抽取轨迹（{step_min} min 栅格，输入 {input_min} min，预测 {horizon_min} min）")
        where = flt.where()
        cand = con.execute(
            f"""
            SELECT traj_id, count(*) AS n FROM clean_points
            WHERE {where} AND coalesce(short_segment, FALSE) = FALSE
              AND coalesce(quality_score, 0) >= {min_q}
              AND coalesce(quality_flags, '') NOT LIKE '%JUMP%'
            GROUP BY 1 HAVING count(*) >= {input_steps + horizon_steps + 2}
            ORDER BY n DESC LIMIT {max_traj}
            """
        ).fetchdf()
        if cand.empty:
            return {"error": "没有满足质量要求且足够长的轨迹，请扩大筛选范围或放宽质量门槛"}
        ids = cand["traj_id"].tolist()
        lst = ",".join("'" + str(t).replace("'", "''") + "'" for t in ids)
        df = con.execute(
            f"""SELECT traj_id, event_time, lon, lat, sog, cog FROM clean_points
                WHERE traj_id IN ({lst}) AND coalesce(quality_flags,'') NOT LIKE '%JUMP%'
                ORDER BY traj_id, event_time"""
        ).fetchdf()

        _p(progress, 0.2, "重采样到统一时间栅格并构建滑窗样本")
        frames = _resample_segments(df, step_s, input_steps + horizon_steps + 2)
        if not frames:
            return {"error": "重采样后没有可用轨迹"}
        rs = pd.concat(frames, ignore_index=True)
        W = _build_windows(rs, input_steps, horizon_steps)
        X, TH, Y, TF = W["X"], W["TH"], W["Y"], W["TF"]
        if len(X) == 0:
            return {"error": "滑窗样本为 0，请降低输入窗口或放宽轨迹长度要求"}
        if len(X) > max_windows:
            sel = np.linspace(0, len(X) - 1, max_windows).astype(int)
            X, TH, Y, TF = X[sel], TH[sel], Y[sel], TF[sel]
            W["SOG"], W["COG"] = W["SOG"][sel], W["COG"][sel]
            W["TID"] = [W["TID"][i] for i in sel]
            W["T0"] = W["T0"][sel]

        # ---- 时间外推切分 ----
        _p(progress, 0.3, "按时间外推切分训练/测试集（防泄漏）")
        order = np.argsort(W["T0"])
        cut = int(len(X) * 0.7)
        train_mask = np.zeros(len(X), dtype=bool)
        train_mask[order[:cut]] = True
        test_mask = ~train_mask
        if test_mask.sum() < 20:
            return {"error": "测试样本过少，请增加数据范围"}

        scale = float(np.percentile(np.abs(X[train_mask]), 99.5)) or 1.0
        Xt, Tt, Yt, TFt = X[test_mask], TH[test_mask], Y[test_mask], TF[test_mask]

        # 测试集分层元数据：平均航速 / 船型 / 区域（技术方案 10.7 要求分层报告）
        seg_ship = rs.groupby("traj_id")["sog"].median().to_dict()
        try:
            st_rows = con.execute(
                f"SELECT traj_id, any_value(ship_type) AS st FROM clean_points WHERE traj_id IN ({lst}) GROUP BY 1"
            ).fetchdf()
            ship_of = {str(a): (None if pd.isna(b) else str(b)) for a, b in zip(st_rows["traj_id"], st_rows["st"])}
        except Exception:
            ship_of = {}
        test_tids = [W["TID"][i] for i in np.where(test_mask)[0]]
        test_sog = np.array([float(np.nanmean(W["SOG"][i])) if np.isfinite(W["SOG"][i]).any() else 0.0
                             for i in np.where(test_mask)[0]])
        speed_bucket = pd.cut(test_sog, [-0.01, 0.5, 3, 8, 14, 20, 100],
                              labels=["静止(<0.5kn)", "低速(0.5-3)", "中速(3-8)", "巡航(8-14)",
                                      "高速(14-20)", "超高速(>20)"])
        ship_arr = np.array([ship_of.get(t) or "Unknown" for t in test_tids])

        metrics: Dict[str, Any] = {}
        stratified: Dict[str, Any] = {}
        preds: Dict[str, np.ndarray] = {}

        def _eval(name: str, P: np.ndarray, extra: Optional[Dict[str, Any]] = None) -> None:
            err = np.linalg.norm(P - Yt, axis=2)
            st = _trimmed_stats(err)
            step_err = [round(float(np.mean(err[:, k])), 2) for k in range(err.shape[1])]
            entry = {"model": name, **st, "per_step_m": step_err, "n_test": int(test_mask.sum())}
            if extra:
                entry.update(extra)
            metrics[name] = entry

            # 按速度区间与船型分层
            by_speed: Dict[str, Any] = {}
            for lab in speed_bucket.categories:
                m = np.asarray(speed_bucket == lab, dtype=bool)
                if m.sum() < 5:
                    continue
                by_speed[str(lab)] = {
                    "n": int(m.sum()),
                    "trimmed_ADE_m": round(float(_trimmed_mean(err[m].reshape(-1), 0.95)), 2),
                    "median_m": round(float(np.median(err[m])), 2),
                }
            by_ship: Dict[str, Any] = {}
            for sh in pd.unique(ship_arr):
                m = ship_arr == sh
                if m.sum() < 5:
                    continue
                by_ship[str(sh)] = {
                    "n": int(m.sum()),
                    "trimmed_ADE_m": round(float(_trimmed_mean(err[m].reshape(-1), 0.95)), 2),
                }
            stratified[name] = {"by_speed": by_speed, "by_ship_type": by_ship}

        if "cv" in models:
            _p(progress, 0.4, "评估 CV 基线")
            P = np.stack([cv_predict(Xt[i], Tt[i], TFt[i]) for i in range(len(Xt))])
            preds["cv"] = P
            _eval("cv", P)

        if "kalman" in models:
            _p(progress, 0.47, "评估 Kalman 基线")
            P = np.stack([kalman_predict(Xt[i], Tt[i], TFt[i]) for i in range(len(Xt))])
            preds["kalman"] = P
            _eval("kalman", P)

        gru_info: Dict[str, Any] = {}
        if "gru" in models:
            _p(progress, 0.55, f"训练 GRU（{epochs} epochs）")
            try:
                import torch

                torch.manual_seed(seed)
                np.random.seed(seed)
                torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))

                # 特征：[Δx, Δy, 相对末点偏移, sog, sin(cog), cos(cog)]
                def _feat(idx: np.ndarray) -> np.ndarray:
                    Xb = X[idx] / scale
                    d = np.zeros_like(Xb)
                    d[:, 1:] = Xb[:, 1:] - Xb[:, :-1]
                    rel = Xb - Xb[:, -1:, :]
                    sog = np.nan_to_num(W["SOG"][idx], nan=0.0) / 30.0
                    cog = np.radians(np.nan_to_num(W["COG"][idx], nan=0.0))
                    return np.concatenate(
                        [d, rel, sog[:, :, None], np.sin(cog)[:, :, None],
                         np.cos(cog)[:, :, None]], axis=2
                    ).astype(np.float32)

                tr_idx = np.where(train_mask)[0]
                te_idx = np.where(test_mask)[0]
                Xtr = _feat(tr_idx)
                Xte = _feat(te_idx)

                # 残差学习辅助：GRU 可只学习相对 CV 基线的修正（gru_target=residual）
                def _cv_delta(mask_idx: np.ndarray) -> np.ndarray:
                    out = np.zeros((len(mask_idx), horizon_steps, 2))
                    for r_i, i in enumerate(mask_idx):
                        out[r_i] = cv_predict(X[i], TH[i], TF[i]) - X[i][-1]
                    return out

                # 训练目标：residual（CV 残差）或 direct（直接位移）
                target_mode = str(params.get("gru_target", "direct"))
                if target_mode == "residual":
                    base_tr = _cv_delta(tr_idx)
                    Ytr = ((Y[train_mask] - X[train_mask][:, -1:, :]) - base_tr) / scale
                else:
                    Ytr = (Y[train_mask] - X[train_mask][:, -1:, :]) / scale
                Ytr = Ytr.reshape(len(tr_idx), -1)

                model = _gru_factory(hidden, horizon_steps * 2, in_dim=int(Xtr.shape[2]))
                opt = torch.optim.Adam(model.parameters(), lr=2e-3, weight_decay=1e-5)
                sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, epochs))
                lossf = torch.nn.SmoothL1Loss()
                bs = 256
                n = len(Xtr)
                losses: List[float] = []
                for ep in range(epochs):
                    model.train()
                    perm = np.random.permutation(n)
                    tot = 0.0
                    for s0 in range(0, n, bs):
                        idxb = perm[s0:s0 + bs]
                        opt.zero_grad()
                        out = model(torch.from_numpy(Xtr[idxb]))
                        loss = lossf(out, torch.from_numpy(Ytr[idxb].astype(np.float32)))
                        loss.backward()
                        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                        opt.step()
                        tot += float(loss.detach()) * len(idxb)
                    sched.step()
                    losses.append(tot / max(1, n))
                    if ep % 10 == 0 or ep == epochs - 1:
                        _p(progress, 0.55 + 0.25 * (ep + 1) / epochs,
                           f"GRU epoch {ep + 1}/{epochs} loss={losses[-1]:.5f}")

                model.eval()
                with torch.no_grad():
                    out = model(torch.from_numpy(Xte)).numpy().reshape(len(te_idx), horizon_steps, 2) * scale
                P = Xt[:, -1:, :] + out
                if target_mode == "residual":
                    P = P + _cv_delta(te_idx)
                preds["gru"] = P
                _eval("gru", P, {"epochs": epochs, "hidden": hidden,
                                 "mode": "CV 残差学习" if target_mode == "residual" else "位移直接回归"})

                model_path = settings.models_root / f"gru_{task_id}.pt"
                torch.save({"state": model.state_dict(), "scale": scale, "hidden": hidden,
                            "in_dim": int(Xtr.shape[2]),
                            "input_steps": input_steps, "horizon_steps": horizon_steps},
                           model_path)
                gru_info = {
                    "epochs": epochs, "hidden": hidden,
                    "final_loss": round(losses[-1], 6),
                    "initial_loss": round(losses[0], 6),
                    "loss_curve": [round(x, 6) for x in losses],
                    "n_train": int(train_mask.sum()),
                    "n_params": int(sum(p.numel() for p in model.parameters())),
                    "artifact": model_path.name,
                }
            except Exception as exc:  # pragma: no cover
                gru_info = {"error": f"GRU 训练失败：{exc}"}

        # ---- 准入判断 ----
        _p(progress, 0.88, "对比基线与深度学习模型")
        best_base = None
        for name in ("kalman", "cv"):
            if name in metrics:
                best_base = name
                break
        verdict: Dict[str, Any] = {}
        if best_base and "gru" in metrics:
            b = metrics[best_base]["trimmed_ADE_m"]
            g = metrics["gru"]["trimmed_ADE_m"]
            verdict = {
                "baseline": best_base,
                "gru_ade": g,
                "baseline_ade": b,
                "improvement_pct": round((b - g) / b * 100, 2) if b else None,
                "can_go_online": bool(g is not None and b is not None and g < b),
                "note": "深度学习模型仅在独立时间测试集上稳定优于基线时才建议上线；"
                        "不得以训练 loss 下降作为上线依据。",
            }

        samples = []
        test_idx = np.where(test_mask)[0]
        for i in range(min(6, len(Xt))):
            samples.append(
                {
                    "traj_id": W["TID"][test_idx[i]],
                    "history": [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in Xt[i]],
                    "truth": [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in Yt[i]],
                    "predictions": {
                        k: [[round(float(q[0]), 1), round(float(q[1]), 1)] for q in v[i]]
                        for k, v in preds.items()
                    },
                }
            )

        summary = {
            "algorithm": "predict",
            "dataset_version": dataset_version,
            "filters": flt.to_dict(),
            "params": params,
            "config": {
                "step_minutes": step_min, "input_minutes": input_min,
                "horizon_minutes": horizon_min, "input_steps": input_steps,
                "horizon_steps": horizon_steps,
                "n_trajectories": int(rs["traj_id"].nunique()),
                "n_windows": int(len(X)), "n_train": int(train_mask.sum()),
                "n_test": int(test_mask.sum()),
                "min_quality_score": min_q,
            },
            "metrics": metrics,
            "stratified": stratified,
            "verdict": verdict,
            "gru": gru_info,
            "samples": samples,
            "split": {
                "method": "时间外推（按窗口起点时间排序，前 70% 训练、后 30% 测试）",
                "normalization": "仅用训练集拟合尺度参数",
            },
            "units": {"error": "m", "ADE": "平均位移误差", "FDE": "终点位移误差",
                      "P90": "90 分位误差", "trimmed_ADE": "95% 截尾平均误差"},
            "notes": [
                "坐标使用以轨迹起点为原点的局部 ENU 米制坐标，避免经度跨 180° 与纬度尺度问题。",
                "预测速度受 60 kn 物理上限约束，防止稀疏采样导致外推爆炸。",
                "同时给出均值与 95% 截尾均值，避免少数静止漂移轨迹主导总体指标。",
                "预测用于态势研判与辅助分析，不作为航行安全、避碰或执法的唯一依据。",
            ],
        }
        write_result(task_id, jsonable(summary))
        _p(progress, 1.0, "完成：" + "，".join(f"{k} ADE={v['trimmed_ADE_m']}m" for k, v in metrics.items()))
        return jsonable(summary)
    finally:
        con.close()
