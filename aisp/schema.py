"""标准字段、数据字典与来源字段映射。

系统内部统一使用 UTC 时间、WGS84 经纬度、SOG 单位 knots、COG/Heading 单位 degree。
来源文件字段名差异通过 FIELD_MAPPING 解决，不把来源列名散落在业务代码里。
"""

from __future__ import annotations

from typing import Dict, List

# 标准字段 -> (中文名, 单位, 类型, 说明)
DATA_DICTIONARY: Dict[str, Dict[str, str]] = {
    "point_id": {"name": "点标识", "unit": "-", "dtype": "string", "desc": "批次内唯一点标识"},
    "mmsi": {"name": "海上移动通信业务标识", "unit": "-", "dtype": "string", "desc": "船舶唯一标识"},
    "event_time": {"name": "事件时间", "unit": "UTC", "dtype": "timestamp", "desc": "AIS 报文时间"},
    "lon": {"name": "经度", "unit": "degree", "dtype": "double", "desc": "WGS84 经度"},
    "lat": {"name": "纬度", "unit": "degree", "dtype": "double", "desc": "WGS84 纬度"},
    "sog": {"name": "对地航速", "unit": "kn", "dtype": "double", "desc": "Speed over ground"},
    "cog": {"name": "对地航向", "unit": "degree", "dtype": "double", "desc": "Course over ground"},
    "heading": {"name": "船首向", "unit": "degree", "dtype": "double", "desc": "True heading"},
    "rot": {"name": "转向率", "unit": "deg/min", "dtype": "double", "desc": "Rate of turn"},
    "nav_status": {"name": "航行状态", "unit": "-", "dtype": "string", "desc": "Navigational status"},
    "ship_type": {"name": "船型", "unit": "-", "dtype": "string", "desc": "Ship type"},
    "cargo_type": {"name": "货物类型", "unit": "-", "dtype": "string", "desc": "Cargo type"},
    "draught": {"name": "吃水", "unit": "m", "dtype": "double", "desc": "Draught"},
    "length": {"name": "船长", "unit": "m", "dtype": "double", "desc": "Length"},
    "width": {"name": "船宽", "unit": "m", "dtype": "double", "desc": "Width"},
    "imo": {"name": "IMO 编号", "unit": "-", "dtype": "string", "desc": "IMO number"},
    "callsign": {"name": "呼号", "unit": "-", "dtype": "string", "desc": "Callsign"},
    "name": {"name": "船名", "unit": "-", "dtype": "string", "desc": "Vessel name"},
    "destination": {"name": "目的港", "unit": "-", "dtype": "string", "desc": "Destination"},
    "eta": {"name": "预计到达", "unit": "UTC", "dtype": "timestamp", "desc": "Estimated time of arrival"},
    "mobile_type": {"name": "设备类型", "unit": "-", "dtype": "string", "desc": "Class A / B / Base Station / AtoN"},
    "nav_status_raw": {"name": "原始航行状态", "unit": "-", "dtype": "string", "desc": "来源原始枚举"},
    "source_id": {"name": "来源标识", "unit": "-", "dtype": "string", "desc": "数据来源，支持血缘"},
    "source_file": {"name": "来源文件", "unit": "-", "dtype": "string", "desc": "原始文件名"},
    "row_no": {"name": "原始行号", "unit": "-", "dtype": "bigint", "desc": "原始文件行号"},
    "batch_id": {"name": "批次标识", "unit": "-", "dtype": "string", "desc": "导入批次"},
}

# 丹麦 AIS 公开数据（aisdk yyyy-mm-dd.csv）字段映射
FIELD_MAPPING: Dict[str, str] = {
    "# Timestamp": "event_time",
    "Timestamp": "event_time",
    "Type of mobile": "mobile_type",
    "MMSI": "mmsi",
    "Latitude": "lat",
    "Longitude": "lon",
    "Navigational status": "nav_status",
    "ROT": "rot",
    "SOG": "sog",
    "COG": "cog",
    "Heading": "heading",
    "IMO": "imo",
    "Callsign": "callsign",
    "Name": "name",
    "Ship type": "ship_type",
    "Cargo type": "cargo_type",
    "Width": "width",
    "Length": "length",
    "Type of position fixing device": "pos_fixing",
    "Draught": "draught",
    "Destination": "destination",
    "ETA": "eta",
    "Data source type": "data_source_type",
    "A": "dim_a",
    "B": "dim_b",
    "C": "dim_c",
    "D": "dim_d",
}

# 标准字段在 CLEAN 表中的类型（DuckDB）
CLEAN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clean_spec (
    mmsi          VARCHAR,
    event_time    TIMESTAMP,
    lon           DOUBLE,
    lat           DOUBLE,
    sog           DOUBLE,
    cog           DOUBLE,
    heading       DOUBLE,
    rot           DOUBLE,
    nav_status    VARCHAR,
    ship_type     VARCHAR,
    cargo_type    VARCHAR,
    draught       DOUBLE,
    length        DOUBLE,
    width         DOUBLE,
    imo           VARCHAR,
    callsign      VARCHAR,
    name          VARCHAR,
    destination   VARCHAR,
    mobile_type   VARCHAR,
    source_id     VARCHAR,
    source_file   VARCHAR,
    row_no        BIGINT,
    quality_flags VARCHAR,
    traj_id       VARCHAR,
    stay_id       VARCHAR,
    batch_id      VARCHAR
);
"""

# 剔除明显非船舶目标（基站、航标等）的可选过滤规则
NON_VESSEL_MOBILE_TYPES: List[str] = ["Base Station", "AtoN", "SAR Airborne", "Search and Rescue Transponder"]

VESSEL_TYPES: List[str] = [
    "Cargo", "Tanker", "Passenger", "Fishing", "Tug", "Pilot", "Pleasure",
    "HSC", "Dredging", "SAR", "Other", "Undefined", "Law enforcement",
    "Military", "Wing in ground", "Port tender", "Anti-pollution",
]

# 船型 -> 速度上限（kn），用于速度异常联合判定
SHIP_TYPE_MAX_SPEED: Dict[str, float] = {
    "HSC": 60.0, "Passenger": 45.0, "Cargo": 30.0, "Tanker": 25.0,
    "Fishing": 20.0, "Tug": 20.0, "Pilot": 35.0, "Pleasure": 45.0,
    "Dredging": 15.0, "SAR": 45.0, "Other": 30.0, "Undefined": 40.0,
}
DEFAULT_MAX_SPEED = 45.0
