"""数据导入与 API 接口的集成测试。

原则：
1. 不依赖已有的真实数据集，只在 pytest 的临时目录里生成小样本 CSV；
2. API 测试用 FastAPI TestClient（httpx），只验证协议契约（字段、状态码、错误结构），
   不触发真实的重度分析任务；
3. 测试数据使用丹麦 AIS 公开数据的原始列名，验证字段映射是否生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisp.ingest import detect_delimiter, detect_time_format, preflight


# --------------------------------------------------------------- 测试夹具

SAMPLE_HEADER = [
    "# Timestamp", "Type of mobile", "MMSI", "Latitude", "Longitude",
    "Navigational status", "ROT", "SOG", "COG", "Heading", "IMO", "Callsign",
    "Name", "Ship type", "Cargo type", "Width", "Length",
    "Type of position fixing device", "Draught", "Destination", "ETA",
]


def _write_csv(path: Path, n: int = 30, sep: str = ",", fmt: str = "%d/%m/%Y %H:%M:%S") -> Path:
    """写入一个字段完整的小样本 CSV（时间列使用 dd/mm/yyyy 格式）。"""
    rows = [SAMPLE_HEADER]
    for i in range(n):
        minute = i
        hh, mm = divmod(minute, 60)
        rows.append([
            f"01/01/2025 {hh:02d}:{mm:02d}:00", "Class A", "219000001",
            f"{55.0 + i * 0.001:.5f}", f"{12.0 + i * 0.001:.5f}",
            "Under way using engine", "0", f"{8.0 + (i % 5) * 0.5:.1f}",
            f"{(i * 5) % 360}", f"{(i * 5) % 360}", "0", "CALL1",
            "TEST VESSEL", "Cargo", "70", "20", "80",
            "GPS", "5.5", "DKCPH", "01/01/2025 20:00:00",
        ])
    path.write_text("\n".join(sep.join(r) for r in rows) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------- 探测函数

def test_detect_delimiter_comma():
    assert detect_delimiter(["a,b,c", "1,2,3"]) == ","


def test_detect_delimiter_semicolon():
    assert detect_delimiter(["a;b;c", "1;2;3"]) == ";"


def test_detect_delimiter_tab():
    assert detect_delimiter(["a\tb", "1\t2"]) == "\t"


def test_detect_delimiter_fallback_on_empty():
    assert detect_delimiter([]) == ","
    assert detect_delimiter(["abc", "def"]) == ","


def test_detect_delimiter_accepts_pre_split_rows():
    """兼容已切分行的入参（旧调用方式）。"""
    assert detect_delimiter([["a;b;c"], ["1;2;3"]]) == ";"


def test_detect_time_format_danish():
    import pandas as pd
    s = pd.Series(["01/01/2025 00:00:00", "01/01/2025 00:00:10", "02/01/2025 03:04:05"])
    fmt = detect_time_format(s)
    assert fmt is not None
    assert pd.to_datetime(s, format=fmt, errors="coerce").notna().all()


def test_detect_time_format_none_for_garbage():
    import pandas as pd
    assert detect_time_format(pd.Series(["abc", "def"])) is None


# --------------------------------------------------------------- 预检

def test_preflight_reports_shape_and_mapping(tmp_path):
    p = _write_csv(tmp_path / "ais.csv", n=25)
    info = preflight(p)

    assert info["sample_rows"], "必须能读到样本行"
    assert info["columns"], "必须解析出列名"
    assert info["delimiter"] == ","
    assert info["header_ok"] is True
    # 来源列名必须映射到标准字段，而不是全部落到 extra_*
    mapping = info["mapped_columns"]
    assert mapping["# Timestamp"] == "event_time"
    assert mapping["MMSI"] == "mmsi"
    assert mapping["SOG"] == "sog"
    assert mapping["Ship type"] == "ship_type"
    assert info["time_format"] is not None


def test_preflight_detects_semicolon_file(tmp_path):
    p = _write_csv(tmp_path / "ais_semi.csv", n=10, sep=";")
    info = preflight(p)
    assert info["delimiter"] == ";"
    assert "mmsi" in info["mapped_columns"].values()


def test_preflight_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        preflight(tmp_path / "not_exists.csv")


def test_preflight_missing_ratio_in_range(tmp_path):
    p = _write_csv(tmp_path / "ais.csv", n=15)
    info = preflight(p)
    for col, ratio in info["missing_ratio"].items():
        assert 0.0 <= ratio <= 1.0, f"{col} 缺失率越界：{ratio}"


# --------------------------------------------------------------- API 契约

@pytest.fixture(scope="module")
def client():
    """构造 TestClient，直接走 ASGI，不启动真实服务。

    产物目录已由 conftest.py 在导入前重定向到临时目录，
    因此不会与本地正在运行的开发服务器争抢 DuckDB 文件锁。
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from aisp.api import app
    with fastapi_testclient.TestClient(app) as c:
        yield c


def test_health_returns_ok_envelope(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "request_id" in body
    assert body["data"]["status"] == "ok"


def test_algorithms_lists_seven_analyses(client):
    r = client.get("/api/v1/algorithms")
    body = r.json()
    assert body["ok"] is True
    names = {a["algorithm"] for a in body["data"]}
    for expected in ("hdbscan_hotspot", "vessel_profile", "correlate",
                     "formation_dtw", "frequent_route", "predict"):
        assert expected in names, f"缺少算法 {expected}"


def test_unknown_task_returns_404_with_error_envelope(client):
    r = client.get("/api/v1/tasks/task_not_exists")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == 404
    assert body["error"]["message"]


def test_create_task_rejects_unknown_algorithm(client):
    r = client.post("/api/v1/analysis/tasks", json={"algorithm": "no_such_algo"})
    # 应在创建阶段就拒绝（400），而不是先建任务再以 NO_RUNNER 失败
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "未知算法" in body["error"]["message"]


def test_algorithms_list_is_registered(client):
    """清单里的每个算法都必须能真正执行，避免文档与注册表脱节。"""
    from aisp import tasks

    tasks._load_runners()
    names = {a["algorithm"] for a in client.get("/api/v1/algorithms").json()["data"]}
    registered = set(tasks.RUNNERS) | {"clean"}
    assert names == registered, f"清单与注册表不一致：{names ^ registered}"


def test_admin_meta_contains_dictionary_and_mapping(client):
    r = client.get("/api/v1/admin/meta")
    body = r.json()
    assert body["ok"] is True
    d = body["data"]
    fields = {f["field"] for f in d["data_dictionary"]["fields"]}
    assert {"mmsi", "event_time", "lon", "lat", "sog", "cog"} <= fields
    assert d["data_dictionary"]["source_mapping"]["MMSI"] == "mmsi"


def test_dataset_profile_404_for_unknown_version(client):
    r = client.get("/api/v1/datasets/no_such_version/profile")
    # 未知数据集也应当以业务错误返回，而不是抛出未捕获异常
    assert r.status_code >= 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["message"]


# --------------------------------------------------------------- 上传落盘

def test_safe_filename_strips_path_and_illegal_chars():
    """浏览器提交的文件名不得直接拼进系统路径（技术方案 16.2）。"""
    from aisp.ingest import safe_filename

    assert safe_filename("../../etc/passwd.csv") == "passwd.csv"
    assert safe_filename(r"C:\Windows\system32\a.csv") == "a.csv"
    assert safe_filename('bad<>:"|?*.csv') == "bad.csv"
    assert safe_filename("") == "upload.csv"
    # 超长文件名需被截断，避免触发 Windows 路径上限
    assert len(safe_filename("x" * 400 + ".csv")) <= 120


def test_save_upload_rejects_unsupported_suffix():
    from aisp.ingest import save_upload

    with pytest.raises(ValueError, match="不支持的文件类型"):
        save_upload("malware.exe", b"MZ")


def test_save_upload_rejects_empty_file():
    from aisp.ingest import save_upload

    with pytest.raises(ValueError, match="文件为空"):
        save_upload("empty.csv", b"")


def test_save_upload_lands_in_upload_dir_and_is_idempotent(tmp_path, monkeypatch):
    """同名同内容重复上传应复用同一文件；不同内容不得互相覆盖。"""
    import io

    from aisp.config import settings
    from aisp.ingest import save_upload, save_upload_stream

    monkeypatch.setattr(type(settings), "upload_root",
                        property(lambda self: tmp_path / "uploads"))

    p1 = save_upload("ais.csv", b"MMSI,Timestamp\n1,2025-01-01 00:00:00\n")
    assert p1.is_file()
    assert p1.parent == tmp_path / "uploads"
    assert p1.name.endswith("_ais.csv")

    p2 = save_upload("ais.csv", b"MMSI,Timestamp\n1,2025-01-01 00:00:00\n")
    assert p2 == p1, "同内容重复上传应复用同一落盘文件"

    p3 = save_upload_stream("ais.csv", io.BytesIO(b"MMSI,Timestamp\n2,2025-01-02 00:00:00\n"))
    assert p3 != p1, "不同内容必须生成不同文件，原始层不可覆盖"
    assert p1.read_bytes() != p3.read_bytes()


def test_upload_endpoint_saves_file_and_returns_preflight(tmp_path, monkeypatch):
    """POST /datasets/upload：落盘 + 预检回包，且路径只落在 uploads 下。"""
    from aisp.config import settings

    monkeypatch.setattr(type(settings), "upload_root",
                        property(lambda self: tmp_path / "uploads"))
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from aisp.api import app

    content = (
        "# Timestamp,MMSI,Latitude,Longitude,SOG\n"
        "01/01/2025 00:00:00,219000001,55.1,12.1,8.5\n"
        "01/01/2025 00:05:00,219000001,55.11,12.12,8.7\n"
    ).encode("utf-8")

    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/api/v1/datasets/upload",
                   files={"files": ("ais.csv", content, "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    saved = body["data"]["files"][0]
    assert saved["size"] == len(content)
    assert "uploads" in body["data"]["paths"][0]
    # 预检应识别出标准字段映射
    assert saved["preflight"]["mapped_columns"]["MMSI"] == "mmsi"
    assert saved["preflight"]["mapped_columns"]["# Timestamp"] == "event_time"


def test_upload_endpoint_hides_path_traversal_filename(tmp_path, monkeypatch):
    """构造带 ../ 的文件名，确认最终落盘不会逃出 uploads 目录。"""
    from aisp.config import settings

    up = tmp_path / "uploads"
    monkeypatch.setattr(type(settings), "upload_root", property(lambda self: up))
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from aisp.api import app

    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/api/v1/datasets/upload?preflight=false",
                   files={"files": ("../../../evil.csv", b"MMSI\n1\n", "text/csv")})
    assert r.status_code == 200
    p = Path(r.json()["data"]["paths"][0]).resolve()
    assert p.parent == up.resolve(), "上传文件必须落在 uploads 目录内"


# ------------------------------------------------- 11.2 空间筛选参数解析（API 契约）

def test_map_points_rejects_bad_polygon_json(client):
    r = client.get("/api/v1/map/points", params={
        "dataset_version": "clean_x", "polygon": "not-a-json"})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "polygon" in body["error"]["message"]


def test_map_points_rejects_polygon_with_two_vertices(client):
    r = client.get("/api/v1/map/points", params={
        "dataset_version": "clean_x", "polygon": "[[1,2],[3,4]]"})
    assert r.status_code == 400
    assert "3 个顶点" in r.json()["error"]["message"]


def test_map_points_rejects_non_positive_circle_radius(client):
    r = client.get("/api/v1/map/points", params={
        "dataset_version": "clean_x", "circle": "10.0,55.0,0"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 400


def test_map_points_rejects_bad_bbox(client):
    r = client.get("/api/v1/map/points", params={
        "dataset_version": "clean_x", "bbox": "1,2,3"})
    assert r.status_code == 400


def test_map_points_accepts_polygon_and_circle_together(client):
    """参数解析通过后，面对不存在的数据集应返回 404（而不是 400 参数错或 500）。"""
    r = client.get("/api/v1/map/points", params={
        "dataset_version": "clean_not_exists",
        "polygon": "[[9.9,54.9],[10.2,54.9],[10.2,55.2],[9.9,55.2]]",
        "circle": "10.0,55.0,10",
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == 404


# ------------------------------------------------- 10.10 模型监控（API 契约）

def test_models_list_returns_empty_or_records(client):
    """模型列表接口必须返回数组（无模型时为空数组，而不是 404/500）。"""
    r = client.get("/api/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["data"], list)


def test_unknown_model_returns_404(client):
    r = client.get("/api/v1/models/no_such_model_xyz")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == 404


def test_model_drift_rejects_unknown_dataset(client):
    """漂移检测对不存在的数据集应返回 404（模型先查、数据集后查）。"""
    r = client.post("/api/v1/models/no_such_model_xyz/drift",
                    json={"dataset_version": "clean_not_exists"})
    assert r.status_code == 404


def test_model_drift_validates_sample_bounds(client):
    r = client.post("/api/v1/models/any/drift",
                    json={"dataset_version": "x", "sample": 10})
    assert r.status_code == 422, "sample 小于下限应由 pydantic 拦下"
