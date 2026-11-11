# AIS 船舶航迹数据分析与可视化系统

基于丹麦海事局公开 AIS 报文（`aisdk-YYYY-MM-DD.csv`，单日约 2.4 GB / 1400 万行）构建的一体化分析平台：
**数据治理 → 空间/序列挖掘 → 机器学习预测 → 可视化与报告**。

系统为纯本地部署，无外部 GIS 服务、无外部数据库依赖；前端为零构建的原生 ES Module + ECharts。

---

## 1. 核心能力

| 模块 | 技术要点 |
|---|---|
| 数据接入与治理 | 编码/分隔符/时间格式自动探测，字段映射到 26 个标准字段，去重、跳点与漂移标记、轨迹切分、驻留点提取、多维质量评分 |
| 热点区域分析 | HDBSCAN 密度聚类 + 参数敏感性（ARI 对比）+ 凸包 + 时间序列热力 |
| 船舶目标画像 | 速度/航向圆统计、活动范围、驻留行为、时间行为、轨迹复杂度，雷达对比 |
| 属性关联分析 | 以单船/航次为独立样本的相关矩阵，**Benjamini-Hochberg** 多重检验校正，滞后相关曲线 |
| 船舶编群分析 | 候选对预筛 → 统一时间栅格重采样 → 多变量 DTW（Sakoe-Chiba 带）→ average linkage 层次聚类 |
| 频繁轨迹挖掘 | 规则网格 + DDA 网格穿越补格 + 有向转移边 + 频繁航线（支持度按航次去重） |
| 航迹预测与回测 | 匀速外推 / 卡尔曼滤波 / GRU 残差学习，时间外推切分，ADE / FDE / P90 / 截尾均值 + 分层评估 |
| 可视化前端 | 10 个页面，自研 Canvas 瓦片地图（支持离线），ECharts 主题化图表 |
| 工程化 | 异步任务队列、SSE 进度、审计日志、统一响应包、65 项自动化测试 |

---

## 2. 架构（文字版）

```
                    ┌─────────────────────────────────────────────┐
  原始 CSV  ───────►│  ingest.py   编码/分隔符探测 · 字段映射      │
  data/*.csv        │              DuckDB read_csv → STAGING      │
                    └──────────────────┬──────────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │  clean.py    去重(RN) · 异常标记 · 轨迹切分  │
                    │              驻留点 · 质量评分 → CLEAN      │
                    └──────────────────┬──────────────────────────┘
                                       ▼  Parquet（按 event_date 分区）
      ┌────────────────────────────────┼────────────────────────────────┐
      ▼                ▼               ▼               ▼                ▼
 hotspot.py     profile.py     correlate.py    formation.py   frequent_route.py
 HDBSCAN        画像/圆统计    Spearman+BH     DTW+层次聚类   网格/转移/航线
      └────────────────────────────────┼────────────────────────────────┘
                                       ▼
                            predict.py  CV / Kalman / GRU
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │ tasks.py   队列 + 进度 + SSE + 审计          │
                    │ store.py   进程内共享 DuckDB + 元数据库      │
                    └──────────────────┬──────────────────────────┘
                                       ▼  FastAPI (api.py, 27 个端点)
                    ┌─────────────────────────────────────────────┐
                    │ web/  10 个页面（原生 ESM + ECharts + Canvas）│
                    └─────────────────────────────────────────────┘
```

**关键设计决策**

- **元数据库单实例**：`store.py` 在进程内只打开一次 DuckDB 文件，各操作派生子连接。
  多个 `duckdb.connect()` 指向同一文件会互相锁死（Windows 报“另一个程序正在使用此文件”）。
- **分析任务用内存库**：算法通过 `store.connect_memory()` 建内存库 + Parquet 视图，
  不触碰元数据库，从而支持并发分析任务。
- **结果不进 JSON**：大表格落 Parquet，`/summary` 只返回渲染所需的聚合指标。

---

## 3. 目录结构

```
HQ/
├── aisp/                     # 后端包
│   ├── config.py             # 配置与路径（全部以项目根为基准）
│   ├── schema.py             # 26 个标准字段、数据字典、字段映射
│   ├── ingest.py             # CSV 探测与导入
│   ├── clean.py              # 数据治理与质量评分
│   ├── geo.py                # 球面距离、ENU 投影、网格与 DDA
│   ├── store.py              # DuckDB 连接管理与元数据库
│   ├── tasks.py              # 任务队列、进度、审计
│   ├── dataservice.py        # 地图与数据集查询
│   ├── api.py                # FastAPI 应用（27 个端点）
│   ├── cli.py                # 命令行入口
│   └── algorithms/           # 六类分析算法
│       ├── common.py         # 统一过滤条件与结果写盘
│       ├── hotspot.py  profile.py  correlate.py
│       ├── formation.py  dtw.py  frequent_route.py  predict.py
├── web/                      # 前端（零构建）
│   ├── index.html
│   ├── vendor/echarts.min.js
│   └── assets/
│       ├── app.js  util.js  charts.js  state.js  taskrun.js  style.css
│       └── pages/            # 10 个页面模块
├── tests/                    # pytest（65 项）
│   ├── conftest.py           # 测试期隔离 artifacts 目录
│   ├── test_geo.py           # 空间与网格
│   ├── test_stats.py         # 统计与质量口径
│   └── test_ingest_api.py    # 导入与 API 契约
├── data/                     # 原始 CSV（不入库）
├── artifacts/                # 清洗结果、结果产物、元数据库（不入库）
├── requirements.txt
└── .env.example
```

---

## 4. 安装

### 4.1 环境要求

- Python **3.10**（本项目在 `D:\File\Anaconda\envs\py310` 实测通过）
- 磁盘：单日数据导入后约需 6–8 GB（原始 CSV + 清洗 Parquet + 中间产物）
- 内存：建议 ≥ 16 GB（DuckDB 默认上限 12 GB）

### 4.2 Windows

```powershell
cd D:\File\VSCodeFile\QDHB\develop\HQ

& "D:\File\Anaconda\envs\py310\python.exe" -m pip install -r requirements.txt

# 可选：生成本机配置
Copy-Item .env.example .env
```

### 4.3 Linux / macOS

```bash
cd /path/to/HQ
python3.10 -m pip install -r requirements.txt
cp .env.example .env
```

### 4.4 依赖说明

| 依赖 | 用途 |
|---|---|
| `duckdb` | 主分析引擎：CSV 直读、Parquet 落盘、窗口函数、近似分位数 |
| `pandas` / `numpy` / `pyarrow` | 数据框与 Parquet 读写 |
| `hdbscan` / `scikit-learn` | 热点聚类、层次聚类、标准化 |
| `scipy` | Spearman/Pearson/Kendall 相关、统计检验 |
| `shapely` / `pyproj` / `geopandas` | 凸包、几何运算、可选的地理处理 |
| `tslearn` | DTW 参考实现（本项目另有 numba 加速内核） |
| `fastapi` / `uvicorn` / `sse-starlette` | HTTP 服务与 SSE 进度推送 |
| `torch` | GRU 训练与推理（CPU 版即可） |
| `pytest` / `httpx` | 自动化测试 |

> `torch` 体积较大，若只需基线与其它分析，可先不装；`predict` 会自动跳过 GRU 并在结果中说明。

---

## 5. 快速开始（命令行）

```powershell
$PY = "D:\File\Anaconda\envs\py310\python.exe"

# 1) 导入 + 治理（导入约 76 s，治理约 180 s / 单日 2.4 GB）
& $PY -m aisp.cli import --path data/aisdk-2025-01-01.csv --clean

# 2) 一键跑完全部分析（demo 模式）
& $PY -m aisp.cli demo

# 3) 单独跑某一算法
& $PY -m aisp.cli analyze hdbscan_hotspot --dataset clean_v1_test
& $PY -m aisp.cli analyze predict --dataset clean_v1_test --params '{\"gru_epochs\":30}'

# 4) 启动 Web 服务
& $PY -m aisp.cli serve --port 8000
```

浏览器打开 <http://127.0.0.1:8000>。

### CLI 命令一览

| 命令 | 说明 |
|---|---|
| `aisp import --path CSV [--clean] [--dataset-version V]` | 导入 CSV，可选立即治理 |
| `aisp analyze {算法} [--dataset V] [--params JSON] [--filters JSON]` | 运行单个分析算法 |
| `aisp serve [--host H] [--port P] [--reload]` | 启动 Web 服务 |
| `aisp demo [--files N] [--epochs N] [--reimport]` | 端到端演示（导入+治理+全部算法） |
| `aisp rebuild [--yes]` | 重建元数据库并重新扫描产物 |

---

## 6. 前端页面

| 路由 | 页面 | 主要可视化 |
|---|---|---|
| `#/overview` | 总览工作台 | KPI、逐小时上报量、船型占比、航速构成（对数轴）、质量维度、任务状态、快捷入口 |
| `#/datacenter` | 文件与批次 | 文件预检、批次表、质量报告、异常标记分布、下载 |
| `#/map` | AIS 轨迹地图 | Canvas 瓦片地图、轨迹线、密度网格、驻留点、船型多选、船舶清单 |
| `#/hotspot` | 热点区域分析 | 网格热力 + 凸包 + 聚类标签、簇表（可仅看主要热点）、参数敏感性 ARI |
| `#/profile` | 船舶目标画像 | 船舶列表（吸顶）、速度直方、航向玫瑰、逐小时曲线、单船/多船雷达 |
| `#/correlate` | 属性关联分析 | 发散色阶相关矩阵、BH 校正边表、滞后相关曲线 |
| `#/formation` | 船舶编群分析 | 编群卡片、DTW 距离矩阵（前 40 条）、船对明细 |
| `#/routes` | 频繁轨迹挖掘 | 网格热力、有向转移边、TOP 航线高亮与航线表 |
| `#/predict` | 航迹预测 | 截尾/平均 ADE 分组柱图、逐步误差、分层表、样例轨迹、GRU 损失、上线判定 |
| `#/admin` | 系统管理 | 数据字典、源字段映射、默认参数、队列配置、审计日志 |

---

## 7. API 清单（27 个端点）

统一响应结构：成功 `{request_id, ok: true, data}`，失败 `{request_id, ok: false, error: {code, message}}`。

### 数据集与导入

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/datasets/preflight` | 文件预检（编码/分隔符/字段映射/缺失率） |
| POST | `/api/v1/datasets/import` | 导入 CSV，可选治理 |
| GET | `/api/v1/datasets` | 批次列表 |
| GET | `/api/v1/datasets/{version}/profile` | 数据集画像（含稳健 bbox） |
| GET | `/api/v1/datasets/{version}/quality` | 质量报告 |
| GET | `/api/v1/datasets/{version}/download/{name}` | 下载治理产物 |

### 地图与船舶

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/map/overview` | 总览聚合（KPI、时间分布、船型、质量） |
| GET | `/api/v1/map/trajectories` | 轨迹抽稀（RDP） |
| GET | `/api/v1/map/points` | 点位上图（按缩放聚合） |
| GET | `/api/v1/vessels` | 船舶列表 |
| GET | `/api/v1/vessels/{mmsi}/trajectory` | 单船轨迹 |
| GET | `/api/v1/staypoints` | 驻留点 |

### 分析任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/algorithms` | 算法清单与默认参数 |
| POST | `/api/v1/analysis/tasks` | 创建任务（立即返回 task_id） |
| GET | `/api/v1/tasks` | 任务列表 |
| GET | `/api/v1/tasks/{id}` | 任务状态与进度 |
| GET | `/api/v1/tasks/{id}/summary` | 结果摘要（页面渲染用） |
| GET | `/api/v1/tasks/{id}/files` | 结果文件清单 |
| GET | `/api/v1/tasks/{id}/download/{name}` | 下载结果文件 |
| POST | `/api/v1/tasks/{id}/cancel` | 取消任务 |
| GET | `/api/v1/tasks/{id}/events` | SSE 进度流 |

### 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/meta` | 数据字典与字段映射 |
| GET | `/api/v1/admin/audit` | 审计日志 |
| GET | `/api/v1/admin/config` | 运行时配置（已脱敏） |
| GET | `/api/v1/health` | 健康检查 |

另有 `GET /`（前端入口）与 `GET /favicon.ico`。

---

## 8. 实测指标（2025-01-01，丹麦 AIS）

### 8.1 数据治理

| 指标 | 数值 |
|---|---|
| 原始行数 | 14,166,456 |
| 有效位置行 | 14,067,996 |
| 清洗后行数 | **8,170,332** |
| 去重率 | 41.92% |
| 船舶数（MMSI） | **2,446** |
| 轨迹段 | 14,630（可用 6,923） |
| 驻留点 | 5,275 |
| 综合质量分 | **97.18**（异常点占比 0.33%） |
| 时间范围 | 2025-01-01 00:00:00 ~ 23:59:58 |

质量维度：完整性 92.40 / 有效性 99.30 / 时序连续性 99.83 / 空间合理性 99.86 / 字段一致性 92.93。

### 8.2 各算法结果

| 算法 | 关键结果 |
|---|---|
| 热点区域 | 419 簇（噪声 20.7%），主要热点 66 个 |
| 船舶画像 | 2,446 艘；总航程 116,523 km；平均航速中位 3.5 kn |
| 属性关联 | 2,270 独立样本，45 组检验全部显著（BH 校正后） |
| 船舶编群 | 120 序列 / 1,003 对评估，识别 3 群，DTW 耗时 3.3 s |
| 频繁轨迹 | 10×10 网格，72 高频格、145 条有向转移边、19 条航线 |
| 航迹预测 | 30,652 窗口；截尾 ADE：CV 1.59 km / Kalman 1.64 km / **GRU 297 m** |

---

## 9. 测试

```powershell
& "D:\File\Anaconda\envs\py310\python.exe" -m pytest tests -q
# 65 passed
```

覆盖范围：

| 文件 | 覆盖点 |
|---|---|
| `test_geo.py` | 球面距离、投影往返、网格左闭右开边界、外边界归并、DDA 四连通性、圆均值跨北 |
| `test_stats.py` | BH 单调性与上界、强度分档对称性、航线连续匹配语义、质量权重和为 1、加权公式复算 |
| `test_ingest_api.py` | 分隔符/时间格式探测、预检字段映射、API 响应包契约、未知资源 404 |

测试期通过 `conftest.py` 把 `artifacts/` 重定向到临时目录，避免与运行中的服务争抢 DuckDB 文件锁。

---

## 10. 已知限制

1. **数据范围**：当前仅导入 2025-01-01 单日数据。跨日趋势、周日/季节效应、长期预测需扩充数据。
2. **静止点占比高**（约 66.5%）：AIS 系泊船舶持续上报，属数据常态。预测指标须**分速度层**阅读——静止层误差仅百米级，巡航层误差达公里级。
3. **热点凸包 ≠ 水域边界**：凸包是密度簇的简化几何，不能直接作为航道或锚地边界使用。
4. **频繁航线 ≠ 官方航道**：基于规则网格与支持度的统计模式，未与官方航道数据校准。
5. **关联分析不等于因果**：结论表述为“统计依赖/正负相关”，未做因果识别设计。
6. **编群分析**：DTW 阈值需结合业务经验标定；静止船舶在位置上天然接近，可能形成“假编群”。
7. **预测模型**：GRU 数值来自单日数据、时间外推切分；上线前需在更长时段与独立数据集上复验。
8. **地图底图**：无在线底图，仅绘制经纬网格；如需真实底图可挂接离线瓦片目录。

---

## 11. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8000` | 服务监听地址 |
| `DATA_DIR` | `data` | 原始 CSV 目录（相对项目根） |
| `ARTIFACTS_DIR` | `artifacts` | 治理结果、结果产物、元数据库 |
| `DUCKDB_MEMORY_LIMIT` | `12GB` | DuckDB 内存上限 |

完整示例见 `.env.example`。`.env` 已被 `.gitignore` 忽略，不会误提交。

---

## 12. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `IO Error: Cannot open file ... meta.duckdb` | DuckDB 被另一进程占用。停止重复启动的服务；本进程内必须走 `store.q / exec_sql`，**不要**直接 `duckdb.connect` 指向同一文件 |
| 页面改动后浏览器仍显示旧版 | 服务已对静态资源加 `Cache-Control: no-store`；仍不生效时强制刷新（Ctrl+F5）或改用无痕窗口 |
| 热力图空白且控制台报 `addColorStop` | 数据未展平或色阶范围未覆盖数据 min/max；`chartHeatmap` 已内置自动展平与范围推导 |
| 地图全黑 | Canvas 位图尺寸变化后未重绘；`TileMap.resize()` 现已保证尺寸变化后必定 `draw()` |
| `predict` 结果里没有 GRU | 未安装 `torch`，或训练异常；查看 `/tasks/{id}` 的 `error_message` |
