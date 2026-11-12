"""命令行入口：数据导入、治理、分析、服务启动。

用法示例：
    python -m aisp.cli import --path data/aisdk-2025-01-01.csv --clean
    python -m aisp.cli analyze hotspot --dataset clean_xxx
    python -m aisp.cli serve --port 8000
    python -m aisp.cli demo            # 端到端全流程（导入→治理→七类分析）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import store, tasks


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_import(args: argparse.Namespace) -> None:
    from . import ingest, clean as clean_mod
    from .config import default_params

    store.init_meta()
    paths = [Path(p) for p in args.path]
    t0 = time.time()
    res = ingest.ingest_csv(paths, args.source, time_format=args.time_format)
    print(f"导入完成：{res['row_count']} 行 / {time.time() - t0:.1f}s → batch={res['batch_id']}")
    if args.clean:
        dv = args.dataset_version or f"clean_{res['batch_id']}"
        params = default_params("clean")
        out = clean_mod.build_clean([res["batch_id"]], dv, params,
                                    progress=lambda p, s: print(f"  [{p:>4.0%}] {s}"))
        out.pop("quality_report", None)
        _print(out)


def cmd_analyze(args: argparse.Namespace) -> None:
    from .dataservice import _latest_dataset
    from .config import default_params, DEFAULT_PARAMS

    store.init_meta()
    dv = args.dataset or _latest_dataset()
    if not dv:
        raise SystemExit("没有可用数据集，请先执行 import")
    params = default_params(args.algorithm)
    if args.params:
        params.update(json.loads(args.params))
    filters = json.loads(args.filters) if args.filters else {}
    tid = tasks.create_task(args.algorithm, dv, params, filters)
    t0 = time.time()
    res = tasks.run_sync(tid)
    print(f"任务 {tid} 用时 {time.time() - t0:.1f}s")
    if isinstance(res, dict) and res.get("error"):
        print("失败：", res["error"])
        return
    keys = [k for k in ("n_clusters", "noise_ratio", "n_groups", "n_routes", "n_edges",
                        "n_samples", "n_vessels", "metrics", "verdict", "overall_score") if isinstance(res, dict) and k in res]
    _print({k: res[k] for k in keys})


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .config import settings

    uvicorn.run("aisp.api:app", host=args.host or settings.host, port=args.port or settings.port,
                reload=args.reload, log_level="info")


def cmd_demo(args: argparse.Namespace) -> None:
    """端到端演示：导入第一份 CSV → 治理 → 六类分析 → 打印指标。"""
    from . import ingest, clean as clean_mod
    from .config import default_params, settings
    from .dataservice import _latest_dataset

    store.init_meta()
    dv = _latest_dataset()
    if not dv or args.reimport:
        csvs = sorted(settings.data_dir.glob("*.csv"))
        if not csvs:
            raise SystemExit("data/ 目录没有 CSV 文件")
        target = csvs[: args.files]
        print(f"导入 {len(target)} 个文件…")
        res = ingest.ingest_csv(target, "aisdk")
        dv = f"clean_{res['batch_id']}"
        print(f"治理数据（{res['row_count']} 行）…")
        clean_mod.build_clean([res["batch_id"]], dv, default_params("clean"),
                             progress=lambda p, s: print(f"  [{p:>4.0%}] {s}"))
    print(f"数据集：{dv}")

    plan = [
        ("hdbscan_hotspot", "hotspot", {}),
        ("vessel_profile", "profile", {}),
        ("correlate", "correlate", {}),
        ("frequent_route", "frequent_route", {}),
        ("formation_dtw", "formation", {}),
        ("predict", "predict", {"gru_epochs": args.epochs}),
    ]
    summary: Dict[str, Any] = {}
    for alg, key, extra in plan:
        params = default_params(key)
        params.update(extra)
        tid = tasks.create_task(alg, dv, params, {})
        t0 = time.time()
        res = tasks.run_sync(tid)
        dt = time.time() - t0
        if isinstance(res, dict) and res.get("error"):
            print(f"  ✗ {alg}: {res['error']}")
            summary[alg] = {"error": res["error"]}
            continue
        brief = {k: res.get(k) for k in ("n_clusters", "noise_ratio", "n_routes", "n_edges",
                                         "n_groups", "n_vessels", "n_samples") if res.get(k) is not None}
        if "metrics" in res:
            brief["metrics"] = {k: v["trimmed_ADE_m"] for k, v in res["metrics"].items()}
        print(f"  ✓ {alg}: {dt:.0f}s {brief}")
        summary[alg] = brief
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_rebuild(args: argparse.Namespace) -> None:
    """重建全部数据集（清空 artifacts 元数据与 CLEAN 目录后重跑）。"""
    import shutil

    from .config import settings

    if not args.yes:
        raise SystemExit("请加 --yes 确认清空 artifacts 下的元数据与清洗结果")
    store.close_shared()
    for name in ("meta.duckdb", "clean", "staging", "results", "tasks", "tmp"):
        p = settings.artifacts_dir / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file():
            p.unlink()
    settings.ensure_dirs()
    store.init_meta()
    print("已重置，可重新执行 import / demo")


def cmd_backup(args: argparse.Namespace) -> None:
    """生成备份包（元数据库 + 可选 CLEAN 明细）。"""
    from . import backup as backup_mod

    store.init_meta()
    t0 = time.time()
    m = backup_mod.create_backup(include_clean=args.include_clean,
                                dataset_versions=args.dataset or None)
    store.audit("backup_create", "backup", f"zip={m['zip_path']}")
    print(f"备份完成：{m['zip_path']}")
    print(f"  文件数 {len(m['files'])}，体积 {m['zip_bytes'] / 1024 / 1024:.1f} MB，"
          f"耗时 {time.time() - t0:.1f}s")
    for d in m["datasets"]:
        print(f"  数据集 {d['dataset_version']}：{d['n_files']} 个 parquet，"
              f"{d['bytes'] / 1024 / 1024:.1f} MB"
              + (f"，{d['n_points']:,} 点" if d.get("n_points") else ""))


def cmd_restore(args: argparse.Namespace) -> None:
    """从备份包恢复（先校验 manifest，避免解错包）。"""
    from pathlib import Path

    from . import backup as backup_mod

    out = backup_mod.restore_backup(Path(args.path), restore_clean=args.clean,
                                    overwrite=args.overwrite)
    print(json.dumps({k: v for k, v in out.items() if k != "manifest"},
                     ensure_ascii=False, indent=2, default=str))


def cmd_backfill_profile(args: argparse.Namespace) -> None:
    """回填历史批次的 profile_json（补 MMSI 数、编码、分隔符）。

    早期版本只把 staging 统计写进 profile_json，没有 encoding / delimiter，
    导致「文件与批次」的「编码」列永远显示「—」。这个命令可以反复执行，
    不会改动批次表的其它列。执行前必须停掉 Web 服务（DuckDB 单实例锁）。
    """
    from .config import settings as cfg
    from .ingest import detect_delimiter, detect_encoding, staging_profile

    store.init_meta()
    df = store.batch_list()
    if df.empty:
        print("没有批次，无需回填")
        return
    for r in df.to_dict(orient="records"):
        bid = r["batch_id"]
        raw = r.get("profile_json")
        prof: Dict[str, Any] = {}
        if raw:
            try:
                prof = json.loads(raw)
            except ValueError:
                prof = {}
        try:
            prof.update(staging_profile(bid))
        except Exception as exc:  # noqa: BLE001
            print(f"[{bid}] staging 统计失败：{exc}")

        src_dir = cfg.root / (r.get("source_path") or "")
        csvs = sorted(p for p in src_dir.glob("*.csv")) if src_dir.is_dir() else []
        if csvs:
            try:
                enc = detect_encoding(csvs[0])
                prof["encoding"] = enc
                with csvs[0].open("r", encoding=enc, errors="replace", newline="") as fh:
                    head_lines = [fh.readline() for _ in range(5)]
                prof["delimiter"] = detect_delimiter(head_lines)
                print(f"[{bid}] 编码 {enc} / 分隔符 {prof['delimiter']!r} / "
                      f"MMSI {prof.get('m')}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{bid}] 编码探测失败：{exc}")
        else:
            print(f"[{bid}] 未找到源 CSV，仅回填统计量")

        store.exec_sql(
            "UPDATE data_batch SET profile_json = ? WHERE batch_id = ?",
            [json.dumps(prof, ensure_ascii=False, default=str), bid],
        )
    print("回填完成")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="aisp", description="AIS 船舶航迹分析系统")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="导入 CSV 并（可选）治理")
    p_imp.add_argument("--path", nargs="+", required=True)
    p_imp.add_argument("--source", default="ais")
    p_imp.add_argument("--time-format", default=None)
    p_imp.add_argument("--clean", action="store_true")
    p_imp.add_argument("--dataset-version", default=None)
    p_imp.set_defaults(func=cmd_import)

    p_an = sub.add_parser("analyze", help="运行单个分析算法")
    p_an.add_argument("algorithm", choices=["hdbscan_hotspot", "vessel_profile", "correlate",
                                            "formation_dtw", "frequent_route", "predict"])
    p_an.add_argument("--dataset", default=None)
    p_an.add_argument("--params", default=None, help="JSON 字符串，覆盖默认参数")
    p_an.add_argument("--filters", default=None, help="JSON 字符串")
    p_an.set_defaults(func=cmd_analyze)

    p_srv = sub.add_parser("serve", help="启动 Web 服务")
    p_srv.add_argument("--host", default=None)
    p_srv.add_argument("--port", type=int, default=None)
    p_srv.add_argument("--reload", action="store_true")
    p_srv.set_defaults(func=cmd_serve)

    p_demo = sub.add_parser("demo", help="端到端演示全流程")
    p_demo.add_argument("--files", type=int, default=1)
    p_demo.add_argument("--epochs", type=int, default=30)
    p_demo.add_argument("--reimport", action="store_true")
    p_demo.set_defaults(func=cmd_demo)

    p_rb = sub.add_parser("rebuild", help="清空元数据与清洗结果")
    p_rb.add_argument("--yes", action="store_true")
    p_rb.set_defaults(func=cmd_rebuild)

    p_bk = sub.add_parser("backup", help="生成备份包（元数据库 + 可选 CLEAN 明细）")
    p_bk.add_argument("--include-clean", action="store_true",
                      help="同时打包 CLEAN Parquet（体积大）")
    p_bk.add_argument("--dataset", nargs="*", default=None,
                      help="仅备份指定数据集版本")
    p_bk.set_defaults(func=cmd_backup)

    p_rs = sub.add_parser("restore", help="从备份包恢复")
    p_rs.add_argument("--path", required=True, help="备份 zip 路径")
    p_rs.add_argument("--clean", action="store_true", help="一并恢复 CLEAN 明细")
    p_rs.add_argument("--overwrite", action="store_true", help="覆盖已存在的数据集版本")
    p_rs.set_defaults(func=cmd_restore)

    p_bf = sub.add_parser("backfill-profile",
                         help="回填历史批次的 profile_json（MMSI 数/编码/分隔符）")
    p_bf.set_defaults(func=cmd_backfill_profile)

    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
