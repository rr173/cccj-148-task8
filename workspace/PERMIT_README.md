# 岸桥作业许可与装船波次互锁（crane permit / loading-wave interlock）

调度员把**已发布配载方案**拆成波次，为岸桥分配**舱区、箱集合、有效时段**；
系统从**堆叠顺序**推导装船前置关系，把住三道互锁：

1. 同一时段，**相邻冲突舱区 / 同舱区不能授予不同岸桥**（跨波次全局生效）；
2. **前置箱未装船的箱不进入可作业清单**，手持终端扫描直接 `PREREQ_BLOCKED`；
3. 同一**未执行**箱在全库至多归属一张有效许可（SQLite 部分唯一索引兜底）。

纯 Python 标准库 + SQLite（WAL），无第三方依赖。

## 快速开始

```bash
python3 scripts/seed_permit.py /tmp/permit.db          # 灌入演示数据
python3 run_permit.py 8070 /tmp/permit.db              # 启动（启动即对账恢复）
# 浏览器打开 http://127.0.0.1:8070/permit.html
python3 -m unittest discover -s permit/tests           # 40 个自动化用例
```

调度页展示：**时间轴**（各岸桥许可窗口 / 当前时间线 / 暂停-过期态）、
**舱区占用**（冲突舱区红框标出）、**波次堆叠图**（已装✓ / 前置阻塞⛏ /
可作业 / 待授权，附前置链）、**每张许可执行进度**、授权异常队列、
扫描装船与装船记录表。页面顶栏可注入“模拟当前时间”用于错峰演练，3 秒自动刷新。

## 领域模型

| 对象 | 表 | 说明 |
|---|---|---|
| 方案 | `plans` / `plan_entries` | 必须 `published` 才能拆波次 |
| 舱区 / 冲突 | `hatches` / `hatch_conflicts` | 冲突对对称存储，如 H1↔H2 |
| 箱 | `containers` | `stack_key` + `stack_seq`（0=堆底）决定装船先后 |
| 波次 | `waves` / `wave_containers` | 发布时按堆推导 `prereq_ids`（同堆全部下层箱） |
| 许可 | `permits` / `permit_hatches` / `permit_containers` | 编号 + **修订号** + 状态 + 时段 |
| 装船记录 | `loading_scans` | `UNIQUE(wave, container)`，携带许可编号/修订快照 |
| 授权异常 | `auth_exceptions` | open → transferred / restored / voided |
| 幂等记录 | `idem_records` | 按 `Idempotency-Key` 存原响应，重放 |

许可箱归属行有三态：

- `active`：未执行且归属本许可（部分唯一索引
  `uq_active_container_owner ON (wave_id, container_id) WHERE state='active'`
  保证一个未执行箱不可能有两个有效属主）；
- `executed`：已装船，**永远保留原归属许可**（转交不迁移）；
- `released`：许可调整/撤销/异常作废后脱离许可（行保留用于审计）。

## 扫描授权判定（码头扫描必须携带许可编号 + 修订号）

`POST /api/scans {"permit_id","revision","container_id","slot_code","scanner_id"}`

按顺序判定：

1. 许可不存在 → 异常 `UNKNOWN_PERMIT`；
2. 该箱已有装船记录 → 直接回放原记录（`duplicate=true`，天然幂等）；
3. `revision != permit.revision` → 异常 `STALE_REVISION`（引用旧修订）；
4. 许可 `suspended / revoked` → 异常 `PERMIT_SUSPENDED / PERMIT_REVOKED`；
5. 当前时间不在 `[valid_from, valid_to)` → 异常 `PERMIT_EXPIRED`；
6. 箱不属于该波次 → `NOT_IN_WAVE`；不在许可箱集合 / 已脱离 →
   `OUT_OF_HATCH` / `PERMIT_RELEASED`（即越区）；
7. 同堆下层存在未装箱 → **HTTP 409 `PREREQ_BLOCKED`**（这是作业顺序问题，
   不产生装船记录，也不算授权异常），返回 `blocked_by`；
8. 全部通过 → 原子写入装船记录 + 箱 executed + 许可箱状态 + 波次进度
   （首箱置 `in_progress/started_at`，末箱置 `completed/completed_at`）。

所有授权异常返回 HTTP 200 + `{"outcome":"exception","exception_id",...}`，
由调度员在异常队列选择：

- **转交另一张许可** `transfer`：该未执行箱的有效归属一次性迁到目标许可
  （目标须同波次、有效、覆盖该箱舱区）；
- **恢复原许可** `restore`：暂停许可恢复、过期许可需给新 `valid_to`、
  旧修订异常只回传 `latest_revision` 让终端重扫；
- **作废** `void`：释放该箱在异常引用许可上的未执行归属。

## 许可互锁与乐观修订

- 授予 / 调整许可都做时段重叠 × 舱区（相同或在 `hatch_conflicts` 中相邻）
  × 不同岸桥检查；同一岸桥自己的多张许可不互斥。冲突时 **409 同时返回
  `conflicts` 明细与 `occupancy` 最新占用快照**，调度页据此重绘。
- 两名调度员同时调整舱区 / 时段 / 箱集合：所有写库走
  `BEGIN IMMEDIATE` 串行化，且必须带 `expected_revision`；旧修订得到
  409 `STALE_REVISION` + `latest_revision` + 最新占用，**绝不覆盖新修订**。
- **许可转交** `POST /api/permits/{pid}/transfer`：目标许可须当前有效；
  `executed` 箱保留在源许可，`active` 箱一次性整体迁移（先删后插，
  部分唯一索引保证中途无双属主）；源/目标修订号各自 +1。

## 幂等

波次发布、许可转交、扫描确认、异常处置均接受请求头
`Idempotency-Key`：同键 + 同请求体回放首次的状态码与响应体；
同键不同体返回 409 `IDEM_KEY_REUSE`。不带键时，扫描仍有
`UNIQUE(wave,container)` 的天然幂等兜底。

## 中断恢复（崩溃一致性）

所有多步写都在**单个 IMMEDIATE 事务**内提交，
进程在“保存许可 / 迁移箱 / 登记扫描 / 推进波次”之间退出会整体回滚；
服务启动时再执行一次 `reconcile()`（以装船记录为事实源）兜底修复：

| 崩溃点 | 注入方式 | 崩溃瞬间 | 恢复后 |
|---|---|---|---|
| C1 保存许可提交前 | `PERMIT_CRASH=C1` | 许可/舱区/箱归属全部不存在 | 同窗口可重新授予 |
| C2 迁移未执行箱提交前 | `PERMIT_CRASH=C2` | 无箱双归属，箱仍在源许可 | 同幂等键重试转交成功 |
| C3 登记扫描提交前 | `PERMIT_CRASH=C3` | 无装船记录、波次未开始 | 同幂等键重试只装一次 |
| C4 记录已落库、进度未推进 | `PERMIT_CRASH=C4` + `PERMIT_CRASH_SCAN_SPLIT=1` | 有装船记录但波次 `released`（撕裂） | 启动对账补 executed/进度/状态；重扫幂等回放 |

`GET /api/admin/verify` 自检四类不变量：

- `DUPLICATE_ACTIVE_OWNER`：同一未执行箱属于两张有效许可；
- `OVERLAPPING_HATCH_GRANT`：相邻/同舱在重叠时段授予不同岸桥；
- `WAVE_NOT_STARTED_WITH_SCANS` / `WAVE_PROGRESS_MISMATCH`：
  装船记录已存在但波次未开始，或进度计数不一致；
- `SCAN_WITHOUT_EXECUTED_MARK`：有装船记录但箱未标 executed。

## HTTP API

```
# 底账
POST /api/plans                         建方案（hatches/conflicts/containers/entries）
POST /api/plans/{pid}/publish           发布方案
GET  /api/plans · /api/plans/{pid}
# 波次（支持 Idempotency-Key）
POST /api/plans/{pid}/waves             拆波次：{name, container_ids?}，推导前置
GET  /api/waves · /api/waves/{wid}      波次详情含 workable/blocked/boxes/permits
# 许可
POST /api/waves/{wid}/permits           授予：{crane_id,hatches,valid_from,valid_to,container_ids?}
GET  /api/permits?wave_id= · /api/permits/{pid}
POST /api/permits/{pid}/adjust          {expected_revision, hatches?, container_ids?, valid_from?, valid_to?, crane_id?}
POST /api/permits/{pid}/suspend|resume|revoke
POST /api/permits/{pid}/transfer        {target_permit_id, expected_*_revision?}（幂等）
# 扫描 / 异常
POST /api/scans                         必须带 permit_id+revision（幂等）
GET  /api/scans?wave_id=
GET  /api/exceptions?status=open
POST /api/exceptions/{eid}/resolve      {action: transfer|restore|void, ...}（幂等）
# 调度页 / 运维
GET  /api/schedule                      时间轴 + 舱区占用 + 现存冲突
GET  /api/board                         页面一次加载的全量快照
POST /api/admin/reconcile               手动对账（返回补偿动作 + verify）
GET  /api/admin/verify
GET  /healthz
```

通用请求头：`X-Actor`（操作人）、`X-Now`（覆盖当前时间，ISO8601 带时区，
供演练/测试）、`Idempotency-Key`。

## 目录

```
permit/
  store.py      SQLite 表结构 / 部分唯一索引 / WAL 连接
  service.py    波次推导、许可互锁、扫描授权、异常处置、幂等、reconcile/verify
  server.py     标准库 HTTP 服务（启动即对账）
  tests/
    fixture.py            4 舱区/4 堆/两波次夹具
    test_scenarios.py     正常波次/前置阻塞/舱区冲突/旧修订/暂停恢复/并发转交/幂等/释放再授权
    test_http.py          真实 HTTP 端到端（含两连接并发调整）
    test_recovery.py      真实子进程 C1-C4 掉电与恢复（全套 40 用例）
    crash_worker.py       os._exit(91) 崩溃注入工作进程
static/permit.html        调度操作页（时间轴/舱区/阻塞/进度/异常/扫描）
scripts/seed_permit.py    演示数据
run_permit.py             启动入口
```
