# 集装箱船压载水联动执行模块（ballast control）

大副录入压载舱容量、初始液位、泵组、阀门和管路连通关系；模块读取已发布配载方案
与实际装船进度，计算满足**横倾、纵倾、吃水差、舱体受力（船体梁弯矩）与 GM**
限制的分阶段调水方案；值班员按顺序启动/暂停/确认，系统用液位与流量反馈核对实际
转移量，容差与稳性双通过才放行下一步。装船重量变化、临时甩箱或液位漂移时，
**只冻结尚未开始的步骤**并生成修订，已确认转移量原样保留。

纯 Python 标准库（无第三方依赖）：线性规划用自带两阶段/大 M 单纯形，
持久化用 SQLite（WAL + 显式 `BEGIN IMMEDIATE`），HTTP 用 `http.server`。

## 快速开始

```bash
python3 run_ballast.py 8091 data/ballast.db stowage.db
# 浏览器打开 http://127.0.0.1:8091/
#   「录入与装船」→ 填入示例拓扑并保存 → 生成方案
#   「方案时间线」→ 启动 / 部分反馈 / 反馈+确认，或一键顺序执行

PYTHONPATH=. python3 scripts/demo_ballast.py      # 命令行一键演示
python3 -m unittest discover -s ballast/tests     # 32 个用例（含真实子进程崩溃恢复）
```

## 领域模型与稳性（`ballast/domain.py`）

- **压载舱 Tank**：中心坐标 (x 纵, y 横)、舱底/舱顶 z、长宽、几何容量，
  `min_vol`（泵吸空余量，低于即“抽空”）、`max_vol`（舱顶安全上限，
  默认 0.98×容量）；自由液面惯性矩 `I=l·b³/12`。
- **泵 Pump / 阀 Valve**：泵有吸口/排口节点与额定流量；阀连接两个管路节点，
  可声明 `mutex_group`（如交叉总管 `CROSS`）。拓扑用无向图 BFS 查
  “源舱→泵吸口、泵排口→目标舱”的完整资源路径（泵+阀）。
- **船 Ship / 限值**：最大横倾角、纵倾差、艏艉最小/最大吃水、最大船体梁
  弯矩、GM 区间。稳性为小角度矩形驳船近似（静水力接口可整体替换）：
  - 吃水 `d=Δ/(ρLB)`；纵倾 `trim=L·Mx/(Δ·GM_L)`，艏/艉吃水 `d±trim/2`；
  - 横倾 `heel=My/(Δ·GM_T)`，`GM_T` 扣自由液面修正 `ΣρI/Δ`；
  - 弯矩：浮力沿船长均布 + 空船均布 + 各重量点，扫掠积分取 |M| 最大值。
- `evaluate(topo, cargo, volumes)` 返回完整 `StabilityReport`（含逐项违规）。

## 分阶段方案（`ballast/planner.py`）

1. **线性化 + LP 求目标舱位**：在当前工况处把横倾/纵倾/艏艉吃水/各站弯矩
   对各舱体积增量 `dv` 做一阶展开；决策变量为各舱目标体积加 L1 偏差变量，
   约束包含**水量守恒等式**（舱间转移、无舷外水）、每舱 min/max、
   全部稳性限值；目标为总调水量最小。LP 不可行即 `STABILITY_UNREACHABLE`
   （目标稳性不可达，仍落一条 `infeasible` 方案记录供追溯）。LP 求解后
   三位小数舍入并把残差并回余量最大的舱，**严格保持水量守恒**。
2. **非线性复核**：目标点再跑一次完整 `evaluate`；不过则在目标点重新线性化
   迭代一次，仍不过按不可达处理。
3. **步骤编排**：相邻里程碑间按净增减配平（多个源舱/多目标舱贪心配对），
   每对经拓扑选流量最大的可达泵与阀路径；无路径报 `ROUTING_UNAVAILABLE`；
   大转移按单步上限拆分为有序步骤；逐步模拟确认中间液位不抽空、不灌爆、
   中间稳性全部达标（`verify_intermediate_safety`）。

每个步骤给出：源舱、目标舱、目标体积、允许误差（max(2%, 2 m³)）、
所需泵阀资源、互斥管段组、开始条件（前序步骤已确认 + 里程碑装船箱数达成）。

## 执行状态机与一致性（`ballast/executor.py`）

```
pending ──start──▶ running ──pause──▶ paused ──resume──▶ running
  │                  │  ▲                                 │
  │                  │  └──────────────反馈(部分流量)──────┘
  │                  └──反馈足量 + confirm(容差&稳性通过) ──▶ confirmed
  └──修订时未开始步骤全部置 frozen（confirmed 原样保留，running/paused 可在途完成）
```

- **命令令牌幂等**（`commands` 表，token 主键）：规范化载荷
  （plan_id/step_seq/command/operator）算 SHA-256 哈希；
  同 token 同载荷回放首次结果（接受与**拒绝都回放**，含 HTTP 状态）；
  同 token 载荷变化 → 409 `TOKEN_PAYLOAD_MISMATCH`。反馈令牌同理
  （`feedback` 表），重复上报不二次累计。
- **泵阀抢占**：启动时对步骤的全部资源（泵、阀、`GROUP:<互斥组>`）逐条
  插入租约；`leases` 上的部分唯一索引
  `CREATE UNIQUE INDEX … ON leases(resource) WHERE status='held'`
  使同一资源在全库至多一条 held 租约——两名值班员并发抢同一泵组，
  SQLite 单写者 + 索引仲裁**恰好一方成功**，负者 409 `RESOURCE_BUSY`
  （含当前持有者）。确认后租约全部 released。
- **容差与稳性双闸门**：反馈按 `min(源舱减少, 目标舱增加)` 核对转移量；
  确认时要求 `|转移量−目标| ≤ 容差`，且按确认后实测液位+当前装船
  **重新计算稳性**全部通过；不过 → 409（`OUT_OF_TOLERANCE` /
  `STABILITY_FAIL`）并写 critical 告警，不推进下一步。流量低于额定 25%
  产生 `FLOW_LOW` 告警；值班员可暂停，补齐后恢复并确认。
- **修订**：`POST /ballast/plans/{id}/revise` 先把当前修订所有 pending
  步骤置 `frozen`、方案置 `superseded`（同一 IMMEDIATE 事务），再以
  **实测液位为新基线**（已确认量并入、在途步用最后反馈液位）重算后续
  阶段，生成 revision_no+1 的新方案，`revision_of/root_id` 串链。
  对旧修订的启动请求 → 409 `SUPERSEDED_PLAN` 并返回最新方案 id（幂等回放）。

## 事件账本、检查点与崩溃恢复

所有计划、执行命令、传感器反馈、步骤确认都写**只追加事件账本** `events`
（PLAN_CREATED / STEP_START / STEP_PAUSE / FLOW_FEEDBACK / STEP_CONFIRMED /
PREEMPT_LOST / REVISION_FREEZE / RECOVERY …），并在同事务更新方案检查点
`checkpoints`（各步状态/已转移量/held 资源摘要）。

`BALLAST_CRASH=C1..C4` 在“事务提交后、响应前”调 `os._exit(91)` 模拟掉电：

| 点 | 位置 | 重启恢复（`Executor.recover()`，启动时自动执行） |
|---|---|---|
| C1 | 下发启动提交后 | STEP_START 已在账：步骤保持 running、租约保持 held；同令牌重发回放，**不重复下发**（START 事件仍只 1 条） |
| C2 | 暂停提交后 | 步骤恢复为 paused，租约仍占用；暂停令牌回放 |
| C3 | 收到部分流量提交后 | 已转移水量与实测液位不丢，步骤仍 running；反馈令牌回放、不二次累计 |
| C4 | 确认步骤提交后 | 步骤 confirmed、租约全部 released（若释放未落账则补偿释放），确认令牌回放首次结果 |

恢复后不变量：任何时刻同一泵/互斥管段至多一个 running 步骤占用；
已确认转移水量与 `tank_levels` 真值一致；全船总水量守恒。

## HTTP API

```
POST /ballast/topology             录入/激活压载拓扑（舱/泵/阀/互斥组）
GET  /ballast/topology             拓扑列表；GET /ballast/topology/{id} 详情
POST /ballast/levels               初始化/校准各舱实测液位
GET  /ballast/levels
POST /ballast/plans                按里程碑装船时间线生成分阶段方案
POST /ballast/plans/from-stowage   从配载库已发布方案+confirmations 生成时间线
GET  /ballast/plans/{id}           方案时间线（阶段稳性+步骤状态/资源/条件）
GET  /ballast/plans/root/{rid}     修订链最新方案
GET  /ballast/revisions            修订历史
POST /ballast/plans/{id}/revise    冻结未开始步骤 + 生成新修订
POST /ballast/command              {token,plan_id,step_seq,command:start|pause|resume|confirm,operator_id}
POST /ballast/feedback             {token,plan_id,step_seq,observed_src,observed_dst,flow_rate}
GET  /ballast/stability            实时稳性（最新液位+装船重算）
GET  /ballast/leases               泵阀占用租约
GET  /ballast/alerts               偏差告警；POST /ballast/alerts/{id}/ack
GET  /ballast/events               事件账本
GET  /ballast/checkpoints          检查点
POST /ballast/loading              同步装船进度（重量项+已装箱数）
POST /ballast/recover              手动触发恢复
GET  /healthz
```

错误统一 `{"error":{"code","message",...}}`，状态码语义化
（404 不存在 / 409 状态或令牌冲突 / 422 规则不可达或数据非法）。
浏览器操作页 `static/ballast.html` 提供：方案时间线（步骤条+启动/暂停/
反馈/确认）、实时稳性与液位条、压载拓扑、泵阀占用、偏差告警、事件账本与
修订历史六个视图，8 秒自动刷新。

## 目录

```
ballast/
  domain.py     舱/泵/阀拓扑、矩形驳船稳性（横倾/纵倾/吃水/弯矩/自由液面）
  linalg.py     大 M 单纯形（Bland 规则，含等式/界/不等式）
  planner.py    线性化约束、L1 目标 LP、配平编排、中间安全复核
  store.py      SQLite schema、事件账本、检查点、held 租约部分唯一索引
  executor.py   状态机、幂等令牌、抢占仲裁、容差/稳性闸门、修订、崩溃恢复
  fixture.py    录入解析、示例六舱双泵网络、配载库对接
  server.py     HTTP API + 静态页面
  tests/        32 个端到端用例 + crash_worker（真实子进程 C1..C4 掉电）
run_ballast.py  服务入口（启动即 recover）
scripts/demo_ballast.py  一键演示
static/ballast.html      浏览器操作页
```

## 测试场景覆盖（`ballast/tests/test_system.py`）

- 可行方案生成（守恒、逐阶段稳性达标、中间舱界）；
- 目标稳性不可达（单侧货载超压载调节能力，落 infeasible 记录）；
- 泵阀无路径（孤立舱 → ROUTING_UNAVAILABLE）与同泵串行规划；
- 正常逐步执行（容差+稳性双通过、方案完成、租约释放、真值液位守恒）；
- 流量不足告警与暂停/恢复后确认、部分流量保留、反馈令牌幂等；
- 开始条件闸门（前序未确认、里程碑装船箱数不足）；
- 装船变化（甩箱/重量分布改变）后仅冻结未开始步骤、保留已确认量并重算；
- 旧修订启动返回最新方案、重复令牌回放、同令牌换载荷拒绝；
- 两名值班员并发抢占同一泵组恰好一方成功、同泵不被两步共占；
- C1/C2/C3/C4 四个真实子进程掉电点的恢复（不重复下发、不丢水量、不共占泵）；
- HTTP API 全流程（含 409 语义与回放）。

## 简化假设

- 静水力为矩形驳船小角度近似（接口可替换为真实静水力曲线/邦金曲线）；
- 水只在压载舱之间转移（无注排舷外水），平均吃水不可由压载改变，
  吃水不足等工况会如实判为不可达；
- 步骤严格串行执行（互斥资源已在租约层强制；并行步骤为后续扩展）。
