# 风电场机组运维平台

面向风电场站台账、机组部件监测、变桨偏航调试、缺陷处置与上网电量结算的一体化运维后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/store.py          数据仓库（SQLite 持久化，启动时幂等灌入示例数据）
│   ├── app/devflow.py        一键流程编排：初始化、健康检查、模块自检
│   └── devflow.sh            一键流程入口脚本
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 一键流程（示例数据初始化 → 健康检查 → 模块自检）

```bash
cd backend
./devflow.sh            # 等价于 ./devflow.sh up
# 或在仓库根目录：make devflow
```

一条命令顺序完成：

1. **示例数据初始化**：把各模块示例数据幂等写入 `backend/data/ops_demo.sqlite3`，
   重复执行不会产生重复记录；服务重启后数据仍在。
2. **启动服务**：端口未监听时自动起 uvicorn（日志在 `.devflow/server.log`）；
   已有服务在跑则直接复用，不重复启动。原有 `./run.sh` 启动方式照旧可用。
3. **健康检查**：轮询 `/api/health`，确认服务在监听、示例数据已就绪。
4. **模块自检**：逐个请求 18 个业务模块的列表接口，表格直接给出每个模块的
   记录数、待处理、异常量，并与 `/api/overview`（运营概览页）的卡片数字逐项
   对账，保证自检结论与概览页同步。

也可以分步执行（自检/健康检查要求服务已在运行）：

```bash
./devflow.sh seed     # 只初始化示例数据（幂等）
./devflow.sh health   # 只做健康检查
./devflow.sh check    # 只做模块自检并与概览对账
```

**失败续跑**：任一环失败都会点名环节与业务模块（如 `模块自检失败，点名模块：
gearbox`）并以非零码退出，流程状态记在 `.devflow/state.json`；修好后重跑
`./devflow.sh`，已完成的环节与已通过的模块自动跳过，从断掉的地方接着跑。
全部通过后状态清零，下次运行为完整复验。

**自检留档**：每次自检（`check`/`up`）的结论按当时的口径写入
`backend/.devflow/archive/selfcheck-<时间戳>.json` 与同名 `.md`，包含健康检查
原始返回（原有 `ok/app/modules` 字段口径不变）、逐模块结果和概览对账表。

数据文件位置可用环境变量 `APP_DATA_DIR` 覆盖；服务地址可用 `APP_BASE_URL`、
`APP_HOST`、`APP_PORT` 覆盖。

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 风电场站 | `windfarm` | 风电场站 | 场站编码、场站名称、所在区域 |
| 风电机组 | `turbine` | 风电机组 | 机组编号、机组机型、额定功率 |
| 叶片 | `blade` | 叶片 | 叶片编号、所属机组、叶片长度 |
| 齿轮箱 | `gearbox` | 齿轮箱 | 齿轮箱编号、所属机组、油温上限 |
| 发电机 | `generator` | 发电机 | 发电机编号、所属机组、额定电压 |
| 变桨系统 | `pitch` | 变桨系统 | 系统编号、所属机组、变桨方式 |
| 偏航系统 | `yaw` | 偏航系统 | 系统编号、所属机组、偏航方式 |
| 测风塔 | `metmast` | 测风塔 | 塔架编号、所在场站、塔架高度 |
| 集电线路 | `collector` | 集电线路 | 线路编号、电压等级、起止杆塔 |
| 升压站 | `substation` | 升压站 | 站区编号、主变容量、电压等级 |
| 功率预测 | `forecast` | 功率预测单 | 预测单号、所属场站、预测日期 |
| 振动监测 | `vibration` | 振动监测记录 | 监测编号、监测部位、所属机组 |
| 缺陷登记 | `defect` | 机组缺陷 | 缺陷编号、缺陷部位、缺陷等级 |
| 检修任务 | `maintjob` | 检修任务单 | 任务编号、关联机组、检修类型 |
| 备件领用 | `spare` | 备件领用单 | 领用单号、备件名称、备件规格 |
| 巡视检查 | `patrol` | 巡视单 | 巡视单号、巡视路线、巡视人员 |
| 验收确认 | `accept` | 验收单 | 验收单号、关联任务、验收项目 |
| 电量结算 | `settle` | 电量结算单 | 结算单号、结算周期、所属场站 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
