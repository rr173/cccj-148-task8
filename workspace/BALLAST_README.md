# 集装箱船压载水联动执行模块

纯 Python 标准库实现（无第三方依赖），与配载系统共用同一 SQLite 库：
**读取已发布配载方案与实际装船进度**，计算并执行分阶段调水方案。

```bash
python3 run_ballast.py 8060            # 启动（与配载同库，启动即崩溃恢复）
# 浏览器打开 http://localhost:8060/        → 压载操作页
#            http://localhost:8060/stowage → 配载页
```

```bash
python3 -m unittest tests.test_ballast   # 22 个端到端用例
```

## 领域模型

- **压载拓扑**（大副录入，`ballast/domain.py`）：舱（容量/初始液位/抽空保护
  下限/安全上限/重心坐标）、泵组（额定流量）、阀门、管路（无向边，可声明
  `mutex_group` 互斥组）。一步调水 = 源舱→目标舱的一条**恰含一台泵**且
  不自相斥的通路，通路上的泵/阀/管即该步资源。
- **稳性模型**（`ballast/stability.py`）：在配载系统的 KG/GM/纵倾公式上
  扩展横倾（tanθ = M_t/(Δ·GM)）、吃水（Δ/TPC，艏艉吃水与吃水差）、
  舱体受力（船体梁弯矩代理 + 每舱液位上下限）。限值 = 航次
  `min_gm/max_gm/max_trim` + 压载配置 `max_heel_deg/max_draft/min_draft/max_bm`。
- **方案（修订链）**：`ballast_plans` 每航次递增修订号，`active` 唯一；
  步骤状态机 `pending → running → paused → confirmed`，被取代修订的
  未开始步骤为 `frozen`（已确认步骤与其转移量永远保留）。

## 方案生成（`ballast/planner.py`）

1. 读取已发布配载方案中未装船的箱（随晚到箱重/甩箱实时变化），按纵向
   位置均分为 N 个装船阶段；阶段 0 = 当前已装状态。
2. 逐阶段在**水量守恒**（只搬运不加注/排放）与舱位上下限内贪心求解
   目标液位，使横倾/纵倾/吃水/受力全部达标；解不出 →
   422 `STABILITY_UNREACHABLE`（目标稳性不可达，附违规明细）。
3. 相邻阶段液位差分解为转移步骤（每舱在同一阶段只进或只出 ⇒ 任何
   中间时刻不抽空、不越上限）；为每步 BFS 寻路，无通路 →
   422 `NO_PATH`（泵阀冲突/连通缺失）。
4. 每步输出：**源舱、目标舱、目标体积、允许误差、所需泵/阀/管、
   开始条件**（上一步已确认 + 本阶段装船重量到位 `confirmed_weight_gte`）。

## 执行与一致性

- **顺序推进**：上一步未确认不能启动下一步；同一时间只允许一步在调水。
- **泵组独占**：部分唯一索引 `uq_ballast_seizure_active` 保证两名值班员
  同时抢占同一泵组只有一方成功，负者 409 `PUMP_BUSY`（附持有者）。
- **反馈核对**：液位/流量反馈实时计算实际转移量；源/目标两侧计量不一致
  或非本步舱位异动 → `DEVIATION` 告警；流量低于阈值 → **自动暂停** +
  `FLOW_LOW` 告警；无步骤运行时液位漂移 → `DRIFT` 告警并自动复核。
- **确认闸门**：实际转移量达容差 **且** 稳性按当前实际液位+实际货载
  重算通过，才能确认推进；否则 422 并保持现状。
- **修订**：装船变化/漂移使剩余方案不再安全（逐步推演会抽空/越限，或
  终态稳性越限）时，只冻结未开始步骤并以**当前实际液位**为基线生成
  新修订；已确认转移量保留在旧修订中，水量守恒自然延续。
- **执行令牌**：所有执行命令凭令牌幂等 —— 同令牌同载荷回放首次结果，
  同令牌不同载荷 409 拒绝；旧修订的启动请求不报错，直接返回最新方案
  （`revision_moved`）。

## 事件账本、检查点与崩溃恢复

计划、执行命令、传感器反馈、步骤确认全部写入 `ballast_ledger`（只追加）
并在每次状态跃迁后落 `ballast_checkpoints` 快照。四个注入点可模拟进程
终止（测试用 `simulate_crash_at`，服务器用环境变量
`BALLAST_CRASH=after_seize|after_dispatch|after_partial_flow|after_confirm`
真实 `os._exit`）：

| 崩溃点 | 恢复后保证 |
|---|---|
| 抢占设备后 | 占用记录唯一；同令牌重试回放，他人仍抢不到 |
| 下发启动后 | 步骤保持 running、泵仍独占；同令牌重试**不重复下发** |
| 收到部分流量后 | 已转移水量按最新液位重算，**不丢水** |
| 确认步骤后 | 确认结果可回放，泵已释放，下一步可推进 |

`recover()`（服务器启动时自动执行）按账本 + 最新液位恢复唯一状态：
运行中步骤至多一个、孤儿泵占用释放、写 RECOVERY 事件与新检查点。

## API 一览

```
POST /api/ballast/{vid}/topology        录入/替换压载拓扑（含限值配置）
GET  /api/ballast/{vid}/topology        拓扑 + 当前液位 + 总水量
POST /api/ballast/{vid}/plan            生成分阶段调水方案（新修订）
GET  /api/ballast/{vid}/plan            当前修订 + 方案时间线
GET  /api/ballast/{vid}/revisions       修订历史（含各状态步骤数/已确认量）
POST /api/ballast/{vid}/recheck         复核剩余方案，不安全则冻结重算
POST /api/ballast/{vid}/exec/start      启动步骤（令牌；旧修订→最新方案）
POST /api/ballast/{vid}/exec/pause      暂停（释放泵组，保留已转移量）
POST /api/ballast/{vid}/exec/resume     继续（重新占用泵组）
POST /api/ballast/{vid}/exec/confirm    确认（容差+稳性双闸门）
POST /api/ballast/{vid}/seize           人工抢占泵组（并发只有一方成功）
POST /api/ballast/{vid}/release         释放泵组
POST /api/ballast/{vid}/feedback        液位/流量反馈（偏差/流量/漂移处理）
GET  /api/ballast/{vid}/stability       实时稳性（指标/限值/违规）
GET  /api/ballast/{vid}/resources       泵阀占用
GET  /api/ballast/{vid}/alarms          偏差告警
POST /api/ballast/{vid}/alarms/{id}/ack 告警确认
GET  /api/ballast/{vid}/events          事件账本
GET  /api/ballast/{vid}/checkpoints     检查点快照
POST /api/ballast/recover               崩溃恢复
```

错误统一为 `{"error": {"code": ..., "message": ...}}`
（404 不存在 / 409 状态冲突 / 422 规则不满足）。

## 目录结构

```
ballast/
  db.py         ballast_* 表结构（与 stowage 同库，只读其方案/确认表）
  domain.py     拓扑模型、录入校验、通路透传（BFS，恰一泵、不自互斥）
  stability.py  横倾/纵倾/吃水/受力指标与限值判定（纯函数）
  planner.py    阶段目标求解 + 转移分解 + 步骤生成（守恒/上下限不变量）
  service.py    事务、幂等令牌、抢占、账本、检查点、恢复、崩溃注入
  server.py     HTTP API（复用配载路由）+ 静态页
run_ballast.py            启动器（默认 8060，启动即恢复）
static/ballast.html       浏览器操作页
tests/test_ballast.py     22 个端到端用例
```

## 简化假设

- 稳性为简化模型（KM/TPC 为常数，弯矩为代理量），接口与真实稳性
  计算可平滑替换；横倾/纵倾公式与配载系统口径一致。
- 调水只在舱间搬运，不加注/排放；传感器若报来总量变化，只告警
  （`WATER_UNBALANCED`）不阻断。
- 步骤严格串行；阶段门以装船重量为阈值，确认时的稳性重算是最终
  安全闸门。
