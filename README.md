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
| 模型卡与上线监控 | 模型卡（训练边界/特征/依赖/失效场景）、PSI 输入分布漂移、回填误差趋势、监控点台账与重训判定 |
| 可视化前端 | 11 个页面，自研 Canvas 瓦片地图（支持离线）、轨迹回放与三种空间筛选（矩形/多边形/圆形）、ECharts 主题化图表 |
| 工程化 | 异步任务队列、SSE 进度、审计日志、统一响应包、报告导出（自包含 HTML/Markdown）、144 项自动化测试 |
| 身份与权限 | RBAC 五角色（管理员/数据管理员/分析师/只读/审计），Bearer 或 `X-AIS-Token` 两种令牌传递方式，越权操作落审计 |
| 备份与恢复 | DuckDB `EXPORT DATABASE` 导出式备份（zip），CLI 与系统管理页双入口，恢复后自动校验 |
| 报告导出 | 每类分析生成离线可打开的 HTML 报告（内嵌 SVG 图表、无外部依赖）与 Markdown 版本，含参数、口径与可追溯信息 |

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
                                       ▼  FastAPI (api.py, 31 个路径 / 38 个端点)
                    ┌─────────────────────────────────────────────┐
                    │ web/  11 个页面（原生 ESM + ECharts + Canvas）│
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
│   ├── auth.py               # RBAC 角色权限与令牌校验
│   ├── backup.py             # DuckDB 导出式备份与恢复
│   ├── modelmon.py           # 模型卡、PSI 漂移、回填误差监控
│   ├── report.py             # 自包含 HTML / Markdown 报告生成
│   ├── api.py                # FastAPI 应用（31 个路径 / 38 个端点）
│   ├── cli.py                # 命令行入口（import/clean/run/backup/restore）
│   └── algorithms/           # 六类分析算法
│       ├── common.py         # 统一过滤条件（矩形/多边形/圆形）与结果写盘
│       ├── hotspot.py  profile.py  correlate.py
│       ├── formation.py  dtw.py  frequent_route.py  predict.py
├── web/                      # 前端（零构建）
│   ├── index.html
│   ├── vendor/echarts.min.js
│   └── assets/
│       ├── app.js  util.js  charts.js  state.js  taskrun.js  style.css
│       └── pages/            # 11 个页面模块（含 models.js）
├── tests/                    # pytest（144 项）
│   ├── conftest.py           # 测试期隔离 artifacts 目录
│   ├── test_geo.py           # 空间与网格
│   ├── test_stats.py         # 统计与质量口径
│   ├── test_dataservice.py   # 空间筛选 SQL 下推
│   ├── test_ingest_api.py    # 导入与 API 契约（含模型端点）
│   ├── test_auth.py          # RBAC 权限映射与令牌解析
│   ├── test_backup.py        # 备份导出/恢复
│   ├── test_tasks.py         # 孤儿任务回收与取消语义
│   ├── test_modelmon.py      # PSI、模型卡、监控点、误差趋势
│   └── test_report.py        # 报告导出与口径去重
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
| `#/datacenter` | 文件与批次 | 文件预检、浏览器上传、批次表、质量报告、异常标记分布、下载 |
| `#/map` | AIS 轨迹地图 | Canvas 瓦片地图、RDP 轨迹线、密度网格、驻留点、船型多选、轨迹回放、三种空间筛选（矩形/多边形/圆形）、船舶清单 |
| `#/hotspot` | 热点区域分析 | 网格热力 + 凸包 + 聚类标签、簇表（可仅看主要热点）、参数敏感性 ARI |
| `#/profile` | 船舶目标画像 | 船舶列表（吸顶）、速度直方、航向玫瑰、逐小时曲线、单船/多船雷达 |
| `#/correlate` | 属性关联分析 | 发散色阶相关矩阵、BH 校正边表、滞后相关曲线 |
| `#/formation` | 船舶编群分析 | 编群卡片、DTW 距离矩阵（前 40 条）、船对明细 |
| `#/routes` | 频繁轨迹挖掘 | 网格热力、有向转移边、TOP 航线高亮与航线表 |
| `#/predict` | 航迹预测 | 截尾/平均 ADE 分组柱图、逐步误差、分层表、样例轨迹、GRU 损失、上线判定 |
| `#/models` | 模型监控 | 模型卡（训练边界/特征/依赖/失效场景/分层覆盖）、PSI 漂移检测、回填误差趋势、监控点台账 |
| `#/admin` | 系统管理 | 数据字典、源字段映射、默认参数、队列配置、审计日志、备份与恢复 |

### 报告导出

任务运行成功后，任务状态条右侧会出现「HTML 报告」「Markdown」两个按钮：

- **HTML**：服务端现场生成自包含页面（样式、图表全部内联 SVG，不引用任何 CDN），
  可直接在新标签页阅读或 `Ctrl+P` 打印为 PDF；同时落盘到 `artifacts/results/{task_id}/report.html`。
- **Markdown**：下载 `.md`，含 YAML 头部（任务 ID、数据版本、代码版本、生成时间）、
  关键指标表、结果表与结论口径，便于归档或导入知识库。

报告内固定包含**可追溯三件套**：任务 ID、数据集版本、代码版本（Git 短哈希），
以及该算法的「结论口径与使用边界」，避免指标被脱离上下文引用。

### 地图空间筛选

地图页顶部「空间筛选」支持三种范围，**全部在 SQL 内下推**（不在前端过滤，也不拉全量点）：

| 方式 | 操作 | 实现 |
|---|---|---|
| 矩形 | 在画布上拖拽 | `lon/lat` 双条件区间 |
| 多边形 | 逐点点击，双击闭合；`Esc` 取消 | 射线法判定，先用外包围盒裁剪 |
| 圆形 | 拖拽定半径 | 等距圆柱近似 + 球面半径校验 |

同样参数可直接用于 `/map/trajectories`、`/map/points`、`/vessels`（`polygon=<JSON>`、`circle=lon,lat,km`）。

---

## 7. API 清单（31 个路径 / 38 个端点）

统一响应结构：成功 `{request_id, ok: true, data}`，失败 `{request_id, ok: false, error: {code, message}}`。

### 数据集与导入

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/datasets/preflight` | 文件预检（编码/分隔符/字段映射/缺失率） |
| POST | `/api/v1/datasets/import` | 导入 CSV，可选治理 |
| POST | `/api/v1/datasets/upload` | 浏览器直传 CSV（multipart，流式落盘 + SHA256 + 预检） |
| GET | `/api/v1/datasets` | 批次列表 |
| GET | `/api/v1/datasets/{version}/profile` | 数据集画像（含稳健 bbox） |
| GET | `/api/v1/datasets/{version}/quality` | 质量报告 |
| GET | `/api/v1/datasets/{version}/download/{name}` | 下载治理产物 |

### 地图与船舶

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/map/overview` | 总览聚合（KPI、时间分布、船型、质量） |
| GET | `/api/v1/map/trajectories` | 轨迹抽稀（RDP，支持 `bbox` / `polygon` / `circle`） |
| GET | `/api/v1/map/points` | 点位上图（按缩放聚合，支持 `bbox` / `polygon` / `circle`） |
| GET | `/api/v1/vessels` | 船舶列表（支持 `bbox` / `polygon` / `circle`） |
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
| GET | `/api/v1/tasks/{id}/report?format=html\|md` | 导出分析报告（HTML 内联预览 / Markdown 下载） |
| POST | `/api/v1/tasks/{id}/cancel` | 取消任务 |
| GET | `/api/v1/tasks/{id}/events` | SSE 进度流 |

### 模型监控

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/models` | 模型卡列表（按创建时间倒序） |
| GET | `/api/v1/models/{model_id}` | 模型卡 + 监控日志 + 误差趋势 + 最近漂移结论 |
| POST | `/api/v1/models/{model_id}/drift` | PSI 漂移检测（`reference_dataset` 与 `dataset_version` 不同时才有判定意义） |
| POST | `/api/v1/models/{model_id}/monitor` | 登记监控点（预测数/回填数/延迟/回填 ADE；可指定 `ts` 补录） |

### 身份、备份与系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/whoami` | 当前身份与权限集（公开，无需令牌） |
| GET | `/api/v1/admin/meta` | 数据字典与字段映射 |
| GET | `/api/v1/admin/audit` | 审计日志 |
| GET | `/api/v1/admin/config` | 运行时配置（已脱敏） |
| POST | `/api/v1/admin/backup` | 创建备份（导出式 zip） |
| GET | `/api/v1/admin/backups` | 备份列表 |
| GET | `/api/v1/admin/backups/{name}` | 下载备份 |
| POST | `/api/v1/admin/restore` | 从备份恢复 |
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
| 航迹预测 | 30,652 窗口；截尾 ADE：CV 1.59 km / Kalman 1.64 km / **GRU 297 m**（改善 81.3%，达上线门槛） |
| 模型监控 | 3 个模型卡；PSI 自比 5 个特征均为 0.0（stable）；缺失率 sog 5.1% / cog 13.0% / rot 30.9% / heading 21.6% / report_gap_s 0% |

### 8.3 2025-01-02 全量重跑（含 FROZEN_GPS 修复）

| 指标 | 数值 |
|---|---|
| 原始行数 | 14,517,030 |
| 清洗后行数 | **8,082,709** |
| 船舶数（MMSI） | **2,460** |
| 轨迹段 / 可用段 | 14,655 / 7,287 |
| 驻留点 | 5,506 |
| 综合质量分 | **96.90**（有效性 92.14） |
| 异常标记 | SPEED_SPIKE 7,299 / JUMP 14,235 / GPS_DRIFT 12,479 / **FROZEN_GPS 6,186** |
| 航迹预测 | 28,809 窗口；截尾 ADE：CV 1.87 km / **GRU 347 m**（改善 81.43%） |

**FROZEN_GPS（冻结/卡死 GPS）**：位置逐位不变却持续报告航行速度的设备故障。
典型样本为 MMSI `244710047` —— 6136 条记录坐标全为 `(-85.392, -41.696)`（大西洋），
却报告 SOG=25.4 kn 持续 17.2 小时。因为相邻点位移恰为 0，原有的跳点/推算速度/
漂移规则全部失效，错误坐标会把数据集包络拉到 `lon[-152.9,154.9] lat[-84.6,59.0]`，
并让模型卡的「训练区域」从欧洲海域失真为跨大西洋。

检测方式：按 mmsi 用 gaps-and-islands 把位置逐位未变的相邻点编为同一段，
段内 `max(sog) ≥ 2 kn`、连续点数 ≥ 20 且持续 ≥ 15 分钟即整段标记。
**真实锚泊/靠泊（SOG≈0）不会被误判**——实测 48 小时不动、8641 点的泊位样本 0 误报。
阈值可配：`frozen_gps_sog_kn` / `frozen_gps_min_points` / `frozen_gps_min_minutes`。

---

## 9. 测试

```powershell
& "D:\File\Anaconda\envs\py310\python.exe" -m pytest tests -q
# 144 passed
```

覆盖范围：

| 文件 | 覆盖点 |
|---|---|
| `test_geo.py` | 球面距离、投影往返、网格左闭右开边界、外边界归并、DDA 四连通性、圆均值跨北 |
| `test_stats.py` | BH 单调性与上界、强度分档对称性、航线连续匹配语义、质量权重和为 1、加权公式复算 |
| `test_ingest_api.py` | 分隔符/时间格式探测、预检字段映射、API 响应包契约、未知资源 404、上传文件名净化与路径穿越防护、模型端点契约 |
| `test_dataservice.py` | 矩形/多边形/圆形筛选的 SQL 下推与结果一致性 |
| `test_auth.py` | 五角色权限映射、令牌解析（Bearer / `X-AIS-Token`）与默认本机管理员 |
| `test_backup.py` | 导出式备份产物、恢复后元数据一致性 |
| `test_tasks.py` | 任务初始状态、服务重启后孤儿任务回收（queued/running→failed）、终态不被覆盖、幂等性、取消语义 |
| `test_modelmon.py` | PSI 数值与分级、模型卡字段完整性、监控点与回填趋势、补录时间排序、漂移结论落盘 |
| `test_clean_frozen.py` | FROZEN_GPS 冻结段识别、计入质量扣分；正常航行/真实锚泊/短暂停靠不被误报；阈值可配 |
| `test_report.py` | 报告自包含（无 CDN/脚本）、可追溯字段、表格与内嵌 SVG、HTML 转义、降采样保留高热度、未知/未完成任务拒绝、落盘与格式校验 |

测试期通过 `conftest.py` 把 `artifacts/` 重定向到临时目录，避免与运行中的服务争抢 DuckDB 文件锁。

---

## 10. 身份与权限（RBAC）

默认 `AISP_AUTH=off`，本机自动视为管理员，单人调试无需登录。
若设为 `on`，除 `/api/v1/health`、`/api/v1/whoami` 与静态资源外，
所有接口均需携带令牌（`Authorization: Bearer <token>` 或 `X-AIS-Token: <token>`）。

| 角色 | 中文 | 权限 |
|---|---|---|
| `admin` | 管理员 | 全部（含备份、恢复、配置、审计） |
| `data_manager` | 数据管理员 | 导入/治理/下载 + 跑分析 + 读审计与配置 |
| `analyst` | 分析人员 | 读数据集 + 跑分析 + 看报告 |
| `viewer` | 只读用户 | 读数据集/结果/报告 |
| `auditor` | 审计人员 | 读数据集/结果/报告 + 读审计日志 |

前端右上角有「身份」徒标，点击可录入令牌；令牌无效或缺失时自动弹出，
所有下载按钮统一走 `downloadFile()` 携带令牌。越权访问会返回 403 并记录 `auth_denied` 审计。

```powershell
$env:AISP_AUTH = "on"
$env:AISP_TOKENS = "adminToken=admin,analystToken=analyst"
```

---

## 11. 备份与恢复

使用 DuckDB 的 `EXPORT DATABASE` 导出元数据库（Parquet + CSV），再打为 zip：
单日数据的元数据库备份仅约 **146 KB**（不含清洗 Parquet）。

```powershell
& "D:\File\Anaconda\envs\py310\python.exe" -m aisp.cli backup
& "D:\File\Anaconda\envs\py310\python.exe" -m aisp.cli restore --name <备份文件名>
```

也可在「系统管理」页一键创建与下载。**恢复会覆盖当前元数据库**，操作前请先停服务；
接口会先校验备份包并将旧库重命名为 `.bak`，以便回滚。

---

## 12. 模型卡与上线监控

航迹预测任务成功后自动生成**模型卡**（`artifacts/models/<model_id>/model_card.json`，
`artifacts/results/<task_id>/model_card.json` 另存副本），内容包含：

- 训练数据边界（数据集版本、时间范围、经纬度范围、轨迹/窗口/训练/测试数）
- 评估指标与上线判定（GRU vs 基线的截尾 ADE、改善百分比、是否达门槛）
- 输入特征清单与依赖版本（可复现的最小信息）
- 分层覆盖（按航速/船型分组的样本量与截尾 ADE）与 5 条已知失效场景

监控页（`#/models`）提供两项在线巡检：

| 能力 | 口径 |
|---|---|
| 输入分布漂移（PSI） | 以训练期为参照分箱对比当前数据集；`<0.10` 稳定、`≥0.10` 需关注、`≥0.25` 建议重训 |
| 回填误差趋势 | 上线后实际回填的截尾 ADE 相对首评变化；恶化 `≥20%` 时提示重训 |

**注意自比陷阱**：若 `reference_dataset` 与 `dataset_version` 相同，PSI 必然为 0，
界面会显式标出「自比提醒」，避免被误读为“分布完全稳定”。真实漂移判定必须用不同时间段的数据。

监控点可用两种方式登记：接口 `/models/{id}/monitor`（支持 `ts` 补录历史点，落盘后按时间排序）
或页面上的「登记监控点」弹窗。模型上线前必须形成模型卡并通过业务与合规确认。

---

## 13. 已知限制

1. **数据范围**：当前默认数据集为 2025-01-01 单日（另已导入 2025-01-02 用于跨日漂移验证）。跨季趋势、周日/季节效应、长期预测需继续扩充数据。
2. **静止点占比高**（约 66.5%）：AIS 系泊船舶持续上报，属数据常态。预测指标须**分速度层**阅读——静止层误差仅百米级，巡航层误差达公里级。
3. **热点凸包 ≠ 水域边界**：凸包是密度簇的简化几何，不能直接作为航道或锚地边界使用。
4. **频繁航线 ≠ 官方航道**：基于规则网格与支持度的统计模式，未与官方航道数据校准。
5. **关联分析不等于因果**：结论表述为“统计依赖/正负相关”，未做因果识别设计。
6. **编群分析**：DTW 阈值需结合业务经验标定；静止船舶在位置上天然接近，可能形成“假编群”。
7. **预测模型**：GRU 数值来自单日数据、时间外推切分；上线前需在更长时段与独立数据集上复验。
8. **地图底图**：无在线底图，仅绘制经纬网格；如需真实底图可挂接离线瓦片目录。
9. **冻结 GPS 检测依赖 SOG**：若数据源整段缺失 SOG，无法区分「设备卡死」与「真实锚泊」，只能靠位置/时间规则判断，此时该段不会被标记 FROZEN_GPS。

---

## 14. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8000` | 服务监听地址 |
| `DATA_DIR` | `data` | 原始 CSV 目录（相对项目根） |
| `ARTIFACTS_DIR` | `artifacts` | 治理结果、结果产物、元数据库 |
| `DUCKDB_MEMORY_LIMIT` | `12GB` | DuckDB 内存上限 |
| `AISP_AUTH` | `off` | 是否启用令牌鉴权（`on` / `off`） |
| `AISP_TOKENS` | 空 | 令牌→角色映射，如 `tok1=admin,tok2=analyst` |

完整示例见 `.env.example`。`.env` 已被 `.gitignore` 忽略，不会误提交。

---

## 15. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `IO Error: Cannot open file ... meta.duckdb` | DuckDB 被另一进程占用。停止重复启动的服务；本进程内必须走 `store.q / exec_sql`，**不要**直接 `duckdb.connect` 指向同一文件 |
| 页面改动后浏览器仍显示旧版 | 服务已对静态资源加 `Cache-Control: no-store`；仍不生效时强制刷新（Ctrl+F5）或改用无痕窗口 |
| 热力图空白且控制台报 `addColorStop` | 数据未展平或色阶范围未覆盖数据 min/max；`chartHeatmap` 已内置自动展平与范围推导 |
| 地图全黑 | Canvas 位图尺寸变化后未重绘；`TileMap.resize()` 现已保证尺寸变化后必定 `draw()` |
| `predict` 结果里没有 GRU | 未安装 `torch`，或训练异常；查看 `/tasks/{id}` 的 `error_message` |
