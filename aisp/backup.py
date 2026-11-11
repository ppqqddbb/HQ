"""备份与恢复（技术方案 19.1 / 20.1）。

备份对象分两类，职责不同：

1. **元数据库**（`artifacts/meta.duckdb`）：批次、任务、结果索引、审计、模型注册。
   体积小、变更频繁，必须每次备份；
2. **CLEAN 明细 Parquet**：按 `dataset_version` 整体备份，体积大（单日约 1 GB），
   默认不打包，仅在显式 `include_clean=True` 时纳入。

实现上用 DuckDB 的 `EXPORT DATABASE` / `ATTACH + COPY` 而不是直接复制文件：
服务运行中 meta.duckdb 带 WAL，直接拷贝可能得到半写状态。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from .config import settings

BACKUP_MAGIC = "aisp-backup-v1"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _dataset_dirs() -> List[Path]:
    root = settings.clean_root
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _count_rows(dataset_dir: Path) -> int:
    """统计一个数据集版本的点数（只读 Parquet，不建视图）。"""
    files = list(dataset_dir.glob("event_date=*/*.parquet"))
    if not files:
        return 0
    con = duckdb.connect(database=":memory:")
    try:
        p = (dataset_dir / "event_date=*" / "*.parquet").as_posix()
        return int(con.execute(
            f"SELECT count(*) FROM read_parquet('{p}', hive_partitioning=true)"
        ).fetchone()[0])
    except Exception:
        return 0
    finally:
        con.close()


def create_backup(out_dir: Optional[Path] = None, include_clean: bool = False,
                  dataset_versions: Optional[List[str]] = None) -> Dict[str, Any]:
    """生成一个 zip 备份包，返回包路径与清单。

    include_clean=True 时把 CLEAN Parquet 一并打包（体积大，耗时随数据量增长）。
    dataset_versions 可限定只备份指定版本。
    """
    out_dir = Path(out_dir) if out_dir else (settings.artifacts_dir / "backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    zip_path = out_dir / f"aisp_backup_{stamp}.zip"
    manifest: Dict[str, Any] = {
        "magic": BACKUP_MAGIC,
        "created_at": stamp,
        "include_clean": bool(include_clean),
        "meta_db": settings.meta_db.is_file(),
        "datasets": [],
        "files": [],
    }

    # 元数据库：用 DuckDB 原生 EXPORT DATABASE 导出为 parquet + schema.sql。
    # 不能直接拷文件——Windows 上库文件被共享实例占用会报 PermissionError；
    # 也不能在同一进程内二次 ATTACH 同一文件（Unique file handle conflict）。
    meta_export_dir: Optional[Path] = None
    if settings.meta_db.is_file():
        from . import store
        try:
            store.exec_sql("CHECKPOINT")
        except Exception:
            pass
        meta_export_dir = out_dir / f"_meta_{stamp}"
        if meta_export_dir.exists():
            shutil.rmtree(meta_export_dir, ignore_errors=True)
        with store.cursor() as con:
            con.execute(f"EXPORT DATABASE '{meta_export_dir.as_posix()}' (FORMAT PARQUET)")
        manifest["meta_bytes"] = sum(
            f.stat().st_size for f in meta_export_dir.rglob("*"))

    selected = dataset_versions
    dirs = _dataset_dirs()
    if selected:
        dirs = [d for d in dirs if d.name in set(selected)]

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        if meta_export_dir and meta_export_dir.is_dir():
            for f in sorted(meta_export_dir.rglob("*")):
                if not f.is_file():
                    continue
                name = "meta/" + f.relative_to(meta_export_dir).as_posix()
                zf.write(f, name)
                manifest["files"].append(name)
        for d in dirs:
            item = {"dataset_version": d.name, "n_files": 0, "bytes": 0, "n_points": None}
            for f in sorted(d.rglob("*.parquet")):
                item["n_files"] += 1
                item["bytes"] += f.stat().st_size
                if include_clean:
                    zf.write(f, f"clean/{d.name}/{f.relative_to(d).as_posix()}")
                    manifest["files"].append(f"clean/{d.name}/{f.relative_to(d).as_posix()}")
            if include_clean:
                item["n_points"] = _count_rows(d)
            manifest["datasets"].append(item)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    if meta_export_dir and meta_export_dir.is_dir():
        shutil.rmtree(meta_export_dir, ignore_errors=True)

    manifest["zip_path"] = str(zip_path)
    manifest["zip_bytes"] = zip_path.stat().st_size
    return manifest


def read_manifest(zip_path: Path) -> Dict[str, Any]:
    """读取备份包的 manifest；不是本系统备份时抛 ValueError。"""
    zp = Path(zip_path)
    if not zp.is_file():
        raise ValueError(f"备份包不存在：{zp}")
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise ValueError("备份包缺少 manifest.json，不是本系统生成的备份")
        m = json.loads(zf.read("manifest.json").decode("utf-8"))
    if m.get("magic") != BACKUP_MAGIC:
        raise ValueError("备份包版本不匹配（magic 不符）")
    return m


def restore_backup(zip_path: Path, restore_meta: bool = True,
                   restore_clean: bool = False, overwrite: bool = False) -> Dict[str, Any]:
    """从备份包恢复。

    安全约束：
    - 恢复前校验 manifest，避免把任意压缩包解开覆盖产物目录；
    - 默认不覆盖已存在的数据集版本（overwrite=True 才替换）；
    - 恢复元数据库时先备份当前库为 `meta.duckdb.pre_restore_<ts>`，便于回滚。
    """
    m = read_manifest(zip_path)
    zp = Path(zip_path)
    done: Dict[str, Any] = {"restored_meta": False, "restored_datasets": [],
                            "skipped": [], "pre_restore_backup": None}

    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()

        if restore_meta and any(n.startswith("meta/") for n in names):
            target = settings.meta_db
            settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
            from . import store
            # 元数据库是单实例共享句柄，恢复前必须先释放，否则 Windows 会锁文件
            store.close_shared()
            tmp_dir = settings.tmp_dir / f"restore_{_stamp()}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            for n in names:
                if not n.startswith("meta/"):
                    continue
                out = tmp_dir / Path(n).relative_to("meta")
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)

            if target.is_file():
                bak = target.with_name(f"meta.duckdb.pre_restore_{_stamp()}")
                shutil.move(str(target), str(bak))
                done["pre_restore_backup"] = str(bak)
                wal = target.with_suffix(".duckdb.wal")
                if wal.is_file():
                    try:
                        wal.unlink()
                    except OSError:
                        pass
            con = duckdb.connect(str(target))
            try:
                con.execute(f"IMPORT DATABASE '{tmp_dir.as_posix()}'")
            finally:
                con.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            done["restored_meta"] = True

        if restore_clean:
            clean_files = [n for n in names if n.startswith("clean/")]
            for n in clean_files:
                rel = Path(n).relative_to("clean")
                version = rel.parts[0]
                target_dir = settings.clean_root / version
                if target_dir.exists() and not overwrite:
                    if version not in done["skipped"]:
                        done["skipped"].append(version)
                    continue
                out = settings.clean_root / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if version not in done["restored_datasets"]:
                    done["restored_datasets"].append(version)

    # 恢复后重新扫描元数据（任务/结果索引来自磁盘产物）
    if done["restored_meta"]:
        from . import store
        store.init_meta()
    done["manifest"] = m
    return done


def list_backups(out_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """列出备份目录中的备份包（按时间倒序）。"""
    d = Path(out_dir) if out_dir else (settings.artifacts_dir / "backups")
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("aisp_backup_*.zip"), reverse=True):
        item = {"path": str(p), "bytes": p.stat().st_size,
                "created_at": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
        try:
            item["manifest"] = read_manifest(p)
        except Exception as exc:
            item["error"] = str(exc)
        out.append(item)
    return out
