# 水质档案 · 盘片退役管理

按设备型号分盘归档原始报文。拔掉旧盘前，系统逐份核对报文副本，
保证每一份原始报文在所选退役范围之外的**在用盘**上仍有相同测次编号与
SHA-256 摘要的副本；缺副本时整次退役不生效，并列出待迁走的测次与摘要。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `archive_data`（容器内 `/data/archive.db`，SQLite + WAL），
  服务重启后盘片清单与退役登记均保留、可查询。

## 网页操作

1. 首页创建设备型号（如 `WQ-5000`），进入型号页。
2. 在「按盘号登记 CSV 清单」填写盘号并粘贴 CSV，每行两列：
   `测次编号,SHA-256`（可带表头，自动识别；摘要为 64 位十六进制，大小写均可）。
   - **已登记的盘号不能覆盖**（含已退役盘）；复制到新盘后用新盘号补登。
   - 同一测次的不同摘要作为不同条目分别登记、分别核对。
   - 完全重复的行自动去重并提示。
3. 在「盘片清单与退役办理」勾选一块或多块在用盘，点「办理所选盘退役」：
   - 系统对所选盘上的**每一份**（测次, 摘要）核对范围之外的在用盘是否有
     完全相同的副本；
   - 全部有副本 → 所选盘置为「已退役」，生成退役登记，档案员可线下拔盘；
   - 任一缺副本 → **整次退役不生效**，任何盘状态都不变，页面列出全部
     待迁走的测次编号、摘要及其当前所在盘，同时保留一条「拒绝」登记。
4. 「退役登记」表保留所有成功/拒绝记录（含缺副本明细），重启后仍可查询。

## 退役规则（核心不变量）

- 副本判定必须**测次编号与 SHA-256 同时相同**；同一测次的不同内容分别核对，
  摘要不同不算副本。
- 只统计「所选退役范围之外」且状态为「在用（active）」的盘；已退役盘上的
  副本不计入。
- 多块盘可一同退役，核对范围为所选集合之外；整次办理全有或全无。
- 并行办理：写操作在应用层全局串行化，并在 `BEGIN IMMEDIATE` 事务内完成
  「查副本 → 判定 → 落状态 → 写登记」。两个并行退役若会抽走最后一份副本，
  最多一个成功，另一个必然拒绝——最后一份副本始终留在在用盘上。

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/models` | 创建设备型号（重复创建幂等） |
| GET | `/api/models/{name}` | 型号下全部盘片与状态 |
| POST | `/api/models/{name}/disks` | 按盘号登记 CSV（body：`disk_no`、`csv`、可选 `note`） |
| GET | `/api/models/{name}/disks/{disk_no}/entries` | 查看某盘清单明细 |
| POST | `/api/models/{name}/retire` | 退役办理（body：`disk_nos` 数组）；成功 200，缺副本 409 并返回 `missing` |
| GET | `/api/models/{name}/retirements` | 退役登记（成功/拒绝，含缺副本明细） |

错误统一返回 `{"ok": false, "message": ...}`，状态码：404 不存在、409 冲突、422 输入无效。

## 自测

```bash
# 单元测试（CSV 解析、登记规则、退役核对、并行安全、持久化）
DB_PATH=/tmp/unit-test.db python3 tests/test_units.py

# 集成测试（先启动服务，再对运行中的服务跑关键操作）
DB_PATH=/tmp/selftest.db python3 -m uvicorn main:app --app-dir app --host 127.0.0.1 --port 8000
BASE_URL=http://127.0.0.1:8000 python3 tests/selftest.py
```

## 技术说明

- FastAPI + Jinja2 模板（HTML 表单页面）+ 原生 sqlite3（无 ORM 依赖）。
- SQLite 单文件库，WAL 模式、`busy_timeout=30s`；进程内写锁 + 即时事务，
  保证并发退役的原子性与可串行化。
- 数据表：`device_models`（型号）、`disks`（盘片，active/retired）、
  `manifest_entries`（清单条目，盘内 测次+摘要 唯一）、
  `retirement_records`（退役登记，成功/拒绝及缺副本 JSON 明细）。
