"""AIS 船舶航迹数据分析与可视化系统核心包。

模块划分：
- config    配置与路径（以项目根目录为基准）
- schema    标准字段、数据字典与来源字段映射
- store     DuckDB / Parquet 访问层
- ingest    CSV 预检与导入
- clean     数据治理：去重、异常标记、轨迹切分、驻留点
- quality   质量评分与质量报告
- geo       球面距离、投影、规则网格
- tasks     异步任务调度与状态机
- algorithms/*  七类分析算法
- api/*     FastAPI 服务
"""

__version__ = "1.0.0"
