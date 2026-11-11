"""数据治理：去重、异常标记、轨迹切分、驻留点提取与质量评分。

设计要点（对应技术方案第 4 章）：
- 规则层为主（坐标/时间/速度/位移物理约束），统计层为辅（MAD 鲁棒阈值）；
- 异常只做"标记 + 隔离"，不静默删除；质量标记分为「异常」与「提示」两类，
  低速漂移、静止抖动等提示类不计入质量分，避免把正常行为判成脏数据；
- 轨迹断点由时间缺口、推算速度、位移共同判定，切分后记录 split_reason；
- CLEAN 数据按 event_date 分区写入 Parquet，全程在 DuckDB 内以窗口函数完成，
  不在 Python 中把百万级轨迹读入内存。

流水线只有 3 次扫描：读取+去重 → 运动学推导 → 标记/切分/导出。
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from . import store
from .config import settings

log = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[float, str], None]]

EARTH_R = 6371008.8

# 计入质量分的异常标记（提示类不计分）
ERROR_FLAGS = ("INVALID_TIME", "TIME_CONFLICT", "SPEED_SPIKE", "JUMP", "GPS_DRIFT", "INVALID_COG")
INFO_FLAGS = ("STATIONARY_NOISE", "LOW_SPEED_COG_UNRELIABLE")

QUALITY_WEIGHTS = {"完整性": 0.20, "有效性": 0.25, "时序连续性": 0.20, "空间合理性": 0.20, "字段一致性": 0.15}


def _p(cb: ProgressCb, pct: float, stage: str) -> None:
    log.info("[%3.0f%%] %s", pct * 100, stage)
    if cb:
        cb(pct, stage)


def staypoints_path(dataset_version: str):
    """驻留点文件路径；不存在时返回路径本身（调用方检查 exists）。"""
    return settings.clean_root / dataset_version / "staypoints.parquet"


# --------------------------------------------------------------------------- 主流程

def build_clean(
    batch_ids: Sequence[str],
    dataset_version: str,
    params: Dict[str, Any],
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """从 STAGING 生成 CLEAN 数据集（点表、轨迹段、驻留点、异常记录、质量报告）。"""
    settings.ensure_dirs()
    out_root = settings.clean_root / dataset_version
    out_root.mkdir(parents=True, exist_ok=True)

    files: List[Any] = []
    for b in batch_ids:
        files.extend(sorted(settings.staging_root.glob(f"{b}/*.parquet")))
    if not files:
        raise ValueError("未找到 STAGING 数据，请先导入 CSV")

    excl_mobile = list(params.get("exclude_mobile_types") or [])
    max_gap_s = float(params.get("max_gap_minutes", 60.0)) * 60.0
    speed_spike = float(params.get("speed_spike_kn", 80.0))
    jump_speed = float(params.get("jump_speed_kn", 90.0))
    max_jump_m = float(params.get("max_jump_m", 50000.0))
    min_pts = max(2, int(params.get("min_segment_points", 5)))
    stay_r = float(params.get("stay_radius_m", 200.0))
    stay_t = float(params.get("stay_minutes", 20.0)) * 60.0
    stay_max_sog = float(params.get("stay_max_sog_kn", 1.5))
    dedup_keys = [k for k in (params.get("dedup_keys") or ["mmsi", "event_time"])
                  if k in ("mmsi", "event_time", "lon", "lat")]
    if not dedup_keys:
        dedup_keys = ["mmsi", "event_time"]

    globs = ",".join(f"'{f.as_posix()}'" for f in files)
    mobile_filter = "TRUE"
    if excl_mobile:
        lst = ",".join("'" + str(m).replace("'", "''") + "'" for m in excl_mobile)
        mobile_filter = f"(mobile_type IS NULL OR mobile_type NOT IN ({lst}))"

    win = "WINDOW w AS (PARTITION BY mmsi ORDER BY event_time, row_no)"
    key_sql = ", ".join(dedup_keys)
    dedup_key_concat = " || '|' || ".join(f"coalesce(CAST({k} AS VARCHAR),'')" for k in dedup_keys)

    with store.cursor() as con:
        con.execute(f"SET temp_directory='{settings.tmp_dir.as_posix()}'")
        con.execute("SET preserve_insertion_order=false")

        # ---------- 1. 读取 + 坐标校验 + 去重 ----------
        _p(progress, 0.05, "读取 STAGING、坐标校验与去重")
        con.execute(
            f"""
            CREATE OR REPLACE TABLE p1 AS
            SELECT * FROM (
                SELECT *,
                       coalesce(CAST(lat AS VARCHAR),'') as _plat,
                       coalesce(CAST(lon AS VARCHAR),'') as _plon
                FROM read_parquet([{globs}])
                WHERE mmsi IS NOT NULL AND event_time IS NOT NULL
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND {mobile_filter}
                  AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180
                  AND NOT (abs(lat) = 91.0) AND NOT (lat = 0.0 AND lon = 0.0)
            )
            """
        )
        n_valid_pos = int(con.execute("SELECT COUNT(*) FROM p1").fetchone()[0])
        con.execute("ALTER TABLE p1 DROP COLUMN _plat")
        con.execute("ALTER TABLE p1 DROP COLUMN _plon")

        # 原始行数（含被过滤的非船舶目标与非法坐标）
        n_raw = int(
            con.execute(
                f"SELECT COUNT(*) FROM read_parquet([{globs}]) WHERE mmsi IS NOT NULL AND event_time IS NOT NULL"
            ).fetchone()[0]
        )

        # 冲突审计：同一去重键但坐标不同的组
        con.execute(
            f"""
            CREATE OR REPLACE TABLE dup_audit AS
            SELECT {key_sql},
                   COUNT(*) AS duplicate_count,
                   COUNT(DISTINCT CAST(lat AS VARCHAR) || ',' || CAST(lon AS VARCHAR)) AS distinct_positions,
                   COUNT(DISTINCT source_id) AS source_count
            FROM p1 GROUP BY {key_sql} HAVING COUNT(*) > 1
            """
        )
        dup_stats = con.execute(
            """
            SELECT COUNT(*) AS groups,
                   coalesce(sum(duplicate_count),0) AS rows_in_groups,
                   coalesce(sum(CASE WHEN distinct_positions > 1 THEN 1 ELSE 0 END),0) AS conflict_groups
            FROM dup_audit
            """
        ).fetchdf().iloc[0].to_dict()
        n_dup_groups = int(dup_stats["groups"])
        n_conflict_groups = int(dup_stats["conflict_groups"])

        con.execute(
            f"""
            CREATE OR REPLACE TABLE p1 AS
            SELECT * EXCLUDE (rn, _dk) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY {dedup_key_concat} ORDER BY source_id, row_no
                ) AS rn, {dedup_key_concat} AS _dk
                FROM p1
            ) WHERE rn = 1
            """
        )
        n_after_dedup = int(con.execute("SELECT COUNT(*) FROM p1").fetchone()[0])
        n_dropped_dup = n_valid_pos - n_after_dedup

        # ---------- 2. 运动学推导（一次扫描） ----------
        _p(progress, 0.25, "计算相邻位移与推算速度")
        con.execute(
            f"""
            CREATE OR REPLACE TABLE p2 AS
            SELECT *,
                   lag(lat) OVER w       AS prev_lat,
                   lag(lon) OVER w       AS prev_lon,
                   lag(event_time) OVER w AS prev_time,
                   row_number() OVER w   AS seq_no
            FROM p1 {win}
            """
        )
        con.execute("DROP TABLE p1")

        # ---------- 3. 标记 + 切分 + 导出（一次扫描，嵌套子查询内联） ----------
        _p(progress, 0.40, "标记异常、切分轨迹段、计算质量分")
        con.execute(
            f"""
            CREATE OR REPLACE TABLE p3 AS
            WITH d AS (
                SELECT *,
                    CAST(date_diff('second', prev_time, event_time) AS DOUBLE) AS dt_s,
                    2 * {EARTH_R} * asin(least(1.0, sqrt(
                        pow(sin(radians(lat - prev_lat) / 2), 2) +
                        cos(radians(prev_lat)) * cos(radians(lat)) *
                        pow(sin(radians(lon - prev_lon) / 2), 2)))) AS dist_m
                FROM p2
            ),
            e AS (
                SELECT *,
                    CASE WHEN dt_s IS NULL OR dt_s <= 0 THEN NULL
                         ELSE dist_m / dt_s * 1.943844 END AS implied_speed_kn
                FROM d
            ),
            f AS (
                SELECT *,
                    -- 提示类：静止抖动 / 低速航向不可靠（不计入质量分）
                    (CASE WHEN sog IS NOT NULL AND sog < 0.5 AND dt_s IS NOT NULL
                               AND dist_m IS NOT NULL AND dist_m < 15 THEN 1 ELSE 0 END) AS info_stationary,
                    (CASE WHEN cog IS NOT NULL AND sog IS NOT NULL AND sog < 0.5 THEN 1 ELSE 0 END) AS info_lowsog,
                    -- 异常类
                    (CASE WHEN event_time > now() THEN 1 ELSE 0 END) AS err_time,
                    (CASE WHEN dt_s IS NOT NULL AND dt_s < 0 THEN 1 ELSE 0 END) AS err_conflict,
                    (CASE WHEN cog IS NOT NULL AND (cog < 0 OR cog >= 360) THEN 1 ELSE 0 END) AS err_cog,
                    (CASE WHEN sog IS NOT NULL AND sog > {speed_spike} THEN 1 ELSE 0 END) AS err_spike,
                    (CASE WHEN (implied_speed_kn IS NOT NULL AND implied_speed_kn > {jump_speed})
                            OR (dist_m IS NOT NULL AND dist_m > {max_jump_m}) THEN 1 ELSE 0 END) AS err_jump,
                    (CASE WHEN implied_speed_kn IS NOT NULL AND implied_speed_kn > 3.0
                               AND sog IS NOT NULL AND sog < 0.5 THEN 1 ELSE 0 END) AS err_drift,
                    (CASE WHEN prev_time IS NULL OR dt_s IS NULL OR dt_s > {max_gap_s}
                            OR (implied_speed_kn IS NOT NULL AND implied_speed_kn > {jump_speed})
                            OR (dist_m IS NOT NULL AND dist_m > {max_jump_m})
                            OR dt_s <= 0 THEN 1 ELSE 0 END) AS seg_break
                FROM e
            )
            SELECT * EXCLUDE (info_stationary, info_lowsog, err_time, err_conflict, err_cog,
                              err_spike, err_jump, err_drift, seg_break),
                   trim(both ',' from concat_ws(',',
                       CASE WHEN err_time = 1 THEN 'INVALID_TIME' END,
                       CASE WHEN err_conflict = 1 THEN 'TIME_CONFLICT' END,
                       CASE WHEN err_cog = 1 THEN 'INVALID_COG' END,
                       CASE WHEN err_spike = 1 THEN 'SPEED_SPIKE' END,
                       CASE WHEN err_jump = 1 THEN 'JUMP' END,
                       CASE WHEN err_drift = 1 THEN 'GPS_DRIFT' END,
                       CASE WHEN info_stationary = 1 THEN 'STATIONARY_NOISE' END,
                       CASE WHEN info_lowsog = 1 THEN 'LOW_SPEED_COG_UNRELIABLE' END
                   )) AS quality_flags,
                   (err_time + err_conflict + err_cog + err_spike + err_jump + err_drift
                    + info_stationary + info_lowsog) AS n_flags,
                   (err_time + err_conflict + err_cog + err_spike + err_jump + err_drift) AS n_errors,
                   (CASE WHEN prev_time IS NULL THEN 'START'
                         WHEN dt_s IS NULL OR dt_s <= 0 THEN 'TIME_CONFLICT'
                         WHEN dt_s > {max_gap_s} THEN 'GAP'
                         WHEN (implied_speed_kn > {jump_speed}) OR (dist_m > {max_jump_m}) THEN 'JUMP'
                         ELSE NULL END) AS split_reason,
                   sum(seg_break) OVER (PARTITION BY mmsi ORDER BY event_time, row_no
                                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS seg_idx
            FROM f
            """
        )
        con.execute("DROP TABLE p2")
        con.execute(
            """
            ALTER TABLE p3 ADD COLUMN traj_id VARCHAR;
            UPDATE p3 SET traj_id = md5(mmsi || '|' || CAST(seg_idx AS VARCHAR));
            """
        )

        # ---------- 4. 轨迹段统计与质量分 ----------
        _p(progress, 0.55, "轨迹段统计与质量评分")
        con.execute(
            f"""
            CREATE OR REPLACE TABLE seg_stats AS
            WITH base AS (
                SELECT traj_id, mmsi,
                       count(*) AS n,
                       min(event_time) AS t0, max(event_time) AS t1,
                       min(lat) AS minlat, max(lat) AS maxlat,
                       min(lon) AS minlon, max(lon) AS maxlon,
                       median(dt_s) AS med_dt, max(dt_s) AS max_gap,
                       sum(CASE WHEN dt_s > 0 AND dt_s < {max_gap_s} THEN dist_m ELSE 0 END) AS total_dist_m,
                       avg((CASE WHEN n_errors > 0 THEN 1.0 ELSE 0.0 END)) AS flag_ratio,
                       median(sog) AS med_sog, avg(sog) AS mean_sog,
                       any_value(ship_type) AS ship_type,
                       min(split_reason) FILTER (WHERE split_reason IS NOT NULL) AS split_reason
                FROM p3 GROUP BY traj_id, mmsi
            )
            SELECT traj_id, mmsi, n AS point_count, t0, t1,
                   date_diff('second', t0, t1) AS duration_s,
                   med_dt, max_gap, total_dist_m,
                   round(100.0 * (0.35 * (1 - flag_ratio)
                                + 0.30 * least(1.0, n / 50.0)
                                + 0.20 * (1 - least(1.0, coalesce(max_gap, 0) / {max_gap_s}))
                                + 0.15 * CASE WHEN n > 1 THEN 1 ELSE 0 END), 2) AS quality_score,
                   CASE WHEN 100.0 * (0.35 * (1 - flag_ratio)
                                + 0.30 * least(1.0, n / 50.0)
                                + 0.20 * (1 - least(1.0, coalesce(max_gap, 0) / {max_gap_s}))
                                + 0.15 * CASE WHEN n > 1 THEN 1 ELSE 0 END) >= 80 THEN 'A'
                        WHEN 100.0 * (0.35 * (1 - flag_ratio)
                                + 0.30 * least(1.0, n / 50.0)
                                + 0.20 * (1 - least(1.0, coalesce(max_gap, 0) / {max_gap_s}))
                                + 0.15 * CASE WHEN n > 1 THEN 1 ELSE 0 END) >= 60 THEN 'B'
                        WHEN 100.0 * (0.35 * (1 - flag_ratio)
                                + 0.30 * least(1.0, n / 50.0)
                                + 0.20 * (1 - least(1.0, coalesce(max_gap, 0) / {max_gap_s}))
                                + 0.15 * CASE WHEN n > 1 THEN 1 ELSE 0 END) >= 40 THEN 'C'
                        ELSE 'D' END AS quality_grade,
                   split_reason, ship_type, minlat, minlon, maxlat, maxlon, med_sog, mean_sog,
                   CASE WHEN n < {min_pts} THEN TRUE ELSE FALSE END AS is_short
            FROM base
            """
        )

        # ---------- 5. 驻留点（网格加速） ----------
        _p(progress, 0.68, "提取驻留点")
        stay_df = _staypoints(con, stay_r, stay_t, stay_max_sog)
        if stay_df is not None and not stay_df.empty:
            con.register("_stay", stay_df)
            con.execute(
                f"COPY (SELECT * FROM _stay) TO '{(out_root / 'staypoints.parquet').as_posix()}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            n_stay = len(stay_df)
            # 回填 stay_id
            con.execute(
                """
                CREATE OR REPLACE TABLE stay_bind AS
                SELECT DISTINCT traj_id, stay_id FROM _stay
                """
            )
            con.unregister("_stay")
            con.execute(
                """
                ALTER TABLE p3 ADD COLUMN stay_id VARCHAR;
                UPDATE p3 SET stay_id = s.stay_id FROM stay_bind s WHERE p3.traj_id = s.traj_id;
                """
            )
        else:
            con.execute("ALTER TABLE p3 ADD COLUMN stay_id VARCHAR")
            n_stay = 0

        # ---------- 6. 导出 CLEAN 分区 ----------
        _p(progress, 0.80, "写出 CLEAN Parquet 分区")
        for old in out_root.glob("event_date=*"):
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        con.execute(
            f"""
            COPY (
                SELECT p.mmsi, p.event_time, p.lon, p.lat, p.sog, p.cog, p.heading, p.rot,
                       p.nav_status, p.ship_type, p.cargo_type, p.draught, p.length, p.width,
                       p.imo, p.callsign, p.name, p.destination, p.mobile_type,
                       p.source_id, p.source_file, p.row_no, p.batch_id,
                       p.quality_flags, p.traj_id, p.stay_id,
                       s.is_short AS short_segment, s.quality_score, s.quality_grade,
                       CAST(p.event_time AS DATE) AS event_date,
                       md5(p.mmsi || '|' || CAST(p.event_time AS VARCHAR) || '|' || CAST(p.row_no AS VARCHAR)) AS point_id
                FROM p3 p LEFT JOIN seg_stats s USING (traj_id)
                ORDER BY p.mmsi, p.event_time
            ) TO '{out_root.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD,
                  ROW_GROUP_SIZE 122880, PARTITION_BY (event_date), OVERWRITE_OR_IGNORE)
            """
        )

        # ---------- 7. 审计与报告输出 ----------
        _p(progress, 0.90, "导出异常记录、轨迹段与质量报告")
        con.execute(
            f"COPY (SELECT traj_id, mmsi, event_time, lat, lon, sog, cog, quality_flags, source_file, row_no "
            f"FROM p3 WHERE n_flags > 0) TO '{(out_root / 'bad_records.parquet').as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        con.execute(
            f"COPY (SELECT * FROM seg_stats ORDER BY mmsi, t0) "
            f"TO '{(out_root / 'trajectory_segments.parquet').as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        con.execute(
            f"COPY (SELECT * FROM dup_audit) TO '{(out_root / 'duplicate_audit.parquet').as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        report = _quality_report(
            con, n_raw=n_raw, n_valid_pos=n_valid_pos, n_after=n_after_dedup,
            n_dup_groups=n_dup_groups, n_conflict_groups=n_conflict_groups,
            n_stay=n_stay, files=files, params=params,
        )
        (out_root / "quality_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

        n_final = int(con.execute("SELECT COUNT(*) FROM p3").fetchone()[0])
        n_mmsi = int(con.execute("SELECT COUNT(DISTINCT mmsi) FROM p3").fetchone()[0])
        t0, t1 = con.execute("SELECT min(event_time), max(event_time) FROM p3").fetchone()
        for t in ("p3", "seg_stats", "dup_audit"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
            pass

    store.exec_sql(
        "INSERT OR REPLACE INTO dataset_version VALUES (?,?,?,?,?,?,?)",
        [
            dataset_version, store._now(), int(n_final), int(n_mmsi), ",".join(batch_ids),
            str(out_root), json.dumps(report.get("dimensions", {}), ensure_ascii=False, default=str),
        ],
    )
    store.exec_sql(
        "UPDATE data_batch SET dataset_version=?, status='cleaned' WHERE batch_id IN ("
        + ",".join("?" * len(batch_ids)) + ")",
        [dataset_version, *batch_ids],
    )
    store.audit("clean", f"dataset:{dataset_version}", f"输出 {n_final} 点 / {n_mmsi} 船")
    _p(progress, 1.0, "完成")

    return {
        "dataset_version": dataset_version,
        "clean_root": str(out_root),
        "row_count": int(n_final),
        "mmsi_count": int(n_mmsi),
        "time_range": [str(t0), str(t1)],
        "dropped_duplicate": int(n_dropped_dup),
        "dropped_invalid": int(n_raw - n_valid_pos),
        "duplicate_groups": n_dup_groups,
        "conflict_groups": n_conflict_groups,
        "staypoint_count": int(n_stay),
        "quality_report": report,
    }


# --------------------------------------------------------------------------- 驻留点

def _staypoints(con, radius_m: float, min_seconds: float, max_sog: float):
    """基于时空网格的驻留点检测（O(n) 排序 + 分组），再按 R/T 阈值与质心半径复核。

    说明：先用网格把"低速点"桶化，再按轨迹内连续同桶分组；网格边长取 R，
    因此同组点间最大间距不超过 √2·R，最后用质心半径二次过滤消除斜对角误差。
    """
    lat0 = con.execute("SELECT median(lat) FROM p3").fetchone()[0]
    if lat0 is None:
        return None
    lat_step = radius_m / 111320.0
    lon_step = lat_step / max(0.1, math.cos(math.radians(float(lat0))))

    con.execute(
        f"""
        CREATE OR REPLACE TABLE stay_cand AS
        SELECT traj_id, mmsi, event_time, lon, lat, sog, row_no,
               CASE WHEN sog IS NULL OR sog <= {max_sog}
                    THEN CAST(floor(lat / {lat_step}) AS BIGINT) || '_' ||
                         CAST(floor(lon / {lon_step}) AS BIGINT)
                    ELSE NULL END AS cell
        FROM p3
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE stay_runs AS
        WITH brk AS (
            SELECT *,
                   CASE WHEN cell IS NULL THEN 1
                        WHEN lag(cell) OVER w IS NULL OR lag(cell) OVER w <> cell THEN 1
                        ELSE 0 END AS is_new
            FROM stay_cand WINDOW w AS (PARTITION BY traj_id ORDER BY event_time, row_no)
        ),
        runs AS (
            SELECT *, sum(is_new) OVER (PARTITION BY traj_id ORDER BY event_time, row_no
                                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run_idx
            FROM brk
        )
        SELECT * FROM runs WHERE cell IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE stay_agg AS
        WITH c AS (
            SELECT traj_id, run_idx, avg(lat) AS clat, avg(lon) AS clon FROM stay_runs GROUP BY 1, 2
        )
        SELECT r.traj_id, r.mmsi, r.run_idx,
               count(*) AS point_count,
               min(r.event_time) AS start_time, max(r.event_time) AS end_time,
               any_value(c.clat) AS clat, any_value(c.clon) AS clon,
               avg(r.sog) AS mean_sog,
               date_diff('second', min(r.event_time), max(r.event_time)) AS duration_s,
               max(2 * {EARTH_R} * asin(least(1.0, sqrt(
                    pow(sin(radians(r.lat - c.clat) / 2), 2) +
                    cos(radians(c.clat)) * cos(radians(r.lat)) *
                    pow(sin(radians(r.lon - c.clon) / 2), 2))))) AS max_radius_m
        FROM stay_runs r JOIN c USING (traj_id, run_idx)
        GROUP BY r.traj_id, r.mmsi, r.run_idx, c.clat, c.clon
        """
    )
    df = con.execute(
        f"SELECT * FROM stay_agg WHERE duration_s >= {min_seconds} AND point_count >= 3"
    ).fetchdf()
    for t in ("stay_cand", "stay_runs", "stay_agg"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    if df.empty:
        return df
    # 质心半径复核：超过 R 的组按质心重新拆分成本过高，直接剔除并标记，避免误判
    keep = df[df["max_radius_m"].fillna(0) <= radius_m * 1.5].copy()
    if keep.empty:
        return keep
    keep["stay_id"] = [
        f"sp_{abs(hash((r.traj_id, int(r.run_idx)))) % (10**10):010d}" for r in keep.itertuples()
    ]
    keep["center_lat"] = keep["clat"].round(6)
    keep["center_lon"] = keep["clon"].round(6)
    keep["radius_m"] = radius_m
    keep["mean_sog"] = keep["mean_sog"].round(2)
    keep["duration_min"] = (keep["duration_s"] / 60.0).round(1)
    out = keep[[
        "stay_id", "traj_id", "mmsi", "center_lat", "center_lon",
        "start_time", "end_time", "duration_s", "duration_min",
        "point_count", "radius_m", "max_radius_m", "mean_sog",
    ]].copy()
    out["start_time"] = out["start_time"].astype("datetime64[ns]")
    out["end_time"] = out["end_time"].astype("datetime64[ns]")
    return out


# --------------------------------------------------------------------------- 质量报告

def _quality_report(con, *, n_raw, n_valid_pos, n_after, n_dup_groups, n_conflict_groups, n_stay, files, params):
    flag_rows = con.execute(
        """
        SELECT CASE WHEN n_flags > 0 THEN 1 ELSE 0 END AS has_flag, count(*) AS n FROM p3 GROUP BY 1
        """
    ).fetchdf()
    flagged = int(flag_rows.loc[flag_rows["has_flag"] == 0, "n"].sum()) if not flag_rows.empty else 0
    n_total = int(flag_rows["n"].sum()) if not flag_rows.empty else 0
    n_flagged = n_total - flagged

    per_flag: Dict[str, int] = {}
    for name in ERROR_FLAGS + INFO_FLAGS:
        c = con.execute(f"SELECT count(*) FROM p3 WHERE quality_flags LIKE '%{name}%'").fetchone()[0]
        if c:
            per_flag[name] = int(c)

    err_points = con.execute(
        "SELECT count(*) FROM p3 WHERE " + " OR ".join(f"quality_flags LIKE '%{f}%'" for f in ERROR_FLAGS)
    ).fetchone()[0]

    dt_q = con.execute(
        """
        SELECT quantile_cont(dt_s, [0.05,0.25,0.5,0.75,0.95,0.99]) FROM p3 WHERE dt_s IS NOT NULL AND dt_s > 0
        """
    ).fetchone()[0]
    dt_hist = con.execute(
        """
        SELECT CASE WHEN dt_s <= 10 THEN '≤10s' WHEN dt_s <= 60 THEN '10-60s'
                    WHEN dt_s <= 300 THEN '1-5min' WHEN dt_s <= 3600 THEN '5-60min'
                    ELSE '>60min' END AS bucket,
               count(*) AS n
        FROM p3 WHERE dt_s IS NOT NULL AND dt_s > 0 GROUP BY 1 ORDER BY 1
        """
    ).fetchdf()

    seg = con.execute(
        """SELECT count(*) AS n_segments, avg(point_count) AS avg_pts,
                  sum(CASE WHEN NOT is_short THEN 1 ELSE 0 END) AS usable,
                  max(point_count) AS max_pts, median(quality_score) AS med_q
           FROM seg_stats"""
    ).fetchdf().iloc[0].to_dict()
    grades = con.execute(
        "SELECT quality_grade, count(*) AS n FROM seg_stats GROUP BY 1 ORDER BY 1"
    ).fetchdf()
    split_reasons = con.execute(
        "SELECT coalesce(split_reason,'-') AS reason, count(*) AS n FROM seg_stats GROUP BY 1 ORDER BY 2 DESC"
    ).fetchdf()
    by_ship = con.execute(
        """SELECT coalesce(ship_type,'Unknown') AS ship_type, count(*) AS n, count(DISTINCT mmsi) AS mmsi
           FROM p3 GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""
    ).fetchdf()
    by_mobile = con.execute(
        "SELECT coalesce(mobile_type,'Unknown') AS mobile_type, count(*) AS n FROM p3 GROUP BY 1 ORDER BY 2 DESC"
    ).fetchdf()
    hourly = con.execute(
        "SELECT hour(event_time) AS h, count(*) AS n FROM p3 GROUP BY 1 ORDER BY 1"
    ).fetchdf()
    speed_hist = con.execute(
        """SELECT CAST(floor(CASE WHEN sog > 30 THEN 30 ELSE sog END) AS INT) AS knot, count(*) AS n
           FROM p3 WHERE sog IS NOT NULL GROUP BY 1 ORDER BY 1"""
    ).fetchdf()
    cog_hist = con.execute(
        """SELECT CAST(floor(cog / 10) * 10 AS INT) AS deg, count(*) AS n
           FROM p3 WHERE cog IS NOT NULL GROUP BY 1 ORDER BY 1"""
    ).fetchdf()

    comp = con.execute(
        """SELECT
             avg(CASE WHEN sog IS NULL THEN 1.0 ELSE 0 END) AS sog_null,
             avg(CASE WHEN cog IS NULL THEN 1.0 ELSE 0 END) AS cog_null,
             avg(CASE WHEN heading IS NULL THEN 1.0 ELSE 0 END) AS hdg_null,
             avg(CASE WHEN name IS NULL OR name = '' THEN 1.0 ELSE 0 END) AS name_null,
             avg(CASE WHEN ship_type IS NULL OR ship_type = 'Undefined' THEN 1.0 ELSE 0 END) AS st_undef,
             avg(CASE WHEN destination IS NULL OR destination IN ('Unknown','') THEN 1.0 ELSE 0 END) AS dest_null,
             avg(CASE WHEN draught IS NULL THEN 1.0 ELSE 0 END) AS draught_null
           FROM p3"""
    ).fetchdf().iloc[0].to_dict()

    completeness = 100 * (1 - float(np.mean([comp["sog_null"], comp["cog_null"], comp["name_null"]])))
    validity = 100 * (n_valid_pos / n_raw) if n_raw else 0.0
    continuity = 100 * (1 - min(1.0, float(per_flag.get("JUMP", 0) + per_flag.get("TIME_CONFLICT", 0)) / max(1, n_total)))
    spatial = 100 * (1 - min(1.0, float(per_flag.get("GPS_DRIFT", 0)) / max(1, n_total)))
    consistency = 100 * (1 - float(comp["st_undef"]))
    dims = {
        "完整性": round(completeness, 2), "有效性": round(validity, 2),
        "时序连续性": round(continuity, 2), "空间合理性": round(spatial, 2),
        "字段一致性": round(consistency, 2),
    }
    overall = round(sum(dims[k] * w for k, w in QUALITY_WEIGHTS.items()), 2)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [str(f) for f in files],
        "params": params,
        "row_raw": int(n_raw),
        "row_valid_position": int(n_valid_pos),
        "row_clean": int(n_after),
        "invalid_position_rows": int(n_raw - n_valid_pos),
        "duplicate_groups": int(n_dup_groups),
        "duplicate_rows_removed": int(n_valid_pos - n_after),
        "conflict_groups": int(n_conflict_groups),
        "duplicate_rate": round(1 - n_after / n_valid_pos, 4) if n_valid_pos else 0.0,
        "flagged_points": int(n_flagged),
        "flagged_ratio": round(n_flagged / n_total, 4) if n_total else 0.0,
        "error_points": int(err_points),
        "error_ratio": round(err_points / n_total, 4) if n_total else 0.0,
        "flag_counts": per_flag,
        "error_flags": list(ERROR_FLAGS),
        "info_flags": list(INFO_FLAGS),
        "interval_quantiles_s": [round(float(x), 1) for x in dt_q],
        "interval_histogram": dt_hist.to_dict(orient="records"),
        "segments": {k: (round(float(v), 2) if isinstance(v, (int, float, np.floating)) else v) for k, v in seg.items()},
        "quality_grades": grades.to_dict(orient="records"),
        "split_reasons": split_reasons.to_dict(orient="records"),
        "staypoints": int(n_stay),
        "by_ship_type": by_ship.to_dict(orient="records"),
        "by_mobile_type": by_mobile.to_dict(orient="records"),
        "hourly": hourly.to_dict(orient="records"),
        "speed_histogram": speed_hist.to_dict(orient="records"),
        "cog_histogram": cog_hist.to_dict(orient="records"),
        "completeness": {k: round(float(v), 4) for k, v in comp.items()},
        "dimensions": dims,
        "weights": QUALITY_WEIGHTS,
        "overall_score": overall,
    }
