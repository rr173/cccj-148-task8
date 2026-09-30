# 集装箱船危险品配载与航次变更系统

纯 Python 标准库实现（无第三方依赖），包含：REST API、舱位可视化页面、
约束校验引擎、候选配载求解器、装船锁定、航次变更倒箱规划、方案版本管理、
**可审计的方案差异审批与安全回滚**。

## 快速开始

```bash
python3 run.py 8080          # 启动服务
# 浏览器打开 http://localhost:8080/  → 点击「导入示例航次」→「生成候选配载」
```

```bash
python3 -m unittest discover -s tests   # 运行测试（38 个用例）
python3 scripts/make_sample.py > sample_import.json   # 重新生成示例数据
```

## 领域模型

- **舱位坐标**：`bay-row-tier`（如 `02-03-82`）。舱内层 01..n，甲板层 82/84…。
  每个堆位(stack)有承重上限、层数、冷藏插座数；系统由 bay/row/tier 推导
  三维坐标（米），用于隔离距离计算。
- **箱**：重量、卸港(pod)、危险类别（如 `3`、`5.1`）、是否冷藏、是否到港、
  是否锁定、是否已卸。
- **方案(plan)**：一组 `箱→槽位` 映射，状态机
  `draft → published → history`（被取代的草稿为 `superseded`）。
  数据库级约束 `UNIQUE(plan_id, container_id)`、`UNIQUE(plan_id, slot_id)`
  保证同一箱不会占两个位置、同一位置不会放两箱。

## 校验规则（`stowage/validate.py`）

| 规则 | 说明 |
|---|---|
| 堆叠承重 | 每 stack 总重 ≤ `max_weight`；不得悬空（下层必须填满） |
| 冷藏供电 | 每 stack 冷藏箱数 ≤ 插座数 |
| 港序可达 | 后卸港的箱不得压在前卸港的箱之上（否则产生倒箱） |
| 危险品隔离 | 三维距离 / 不同堆 / 不同舱（`stowage/rules.py`，可配置） |
| 稳性 | GM 在 `[min_gm, max_gm]`，纵倾 ≤ `max_trim`（始终全船计算） |

**人工调整后的重校验不是只查被移动的箱**：影响域 = 源 stack ∪ 目标 stack
的堆叠规则 + 被移动箱与全部危险品箱的隔离关系 + 全船稳性，见
`service.adjust_plan` 返回的 `scope` 字段。

## 关键流程

### 候选配载（求解器）
`POST /api/voyages/{id}/solve` 创建后台 job（可 `POST /api/jobs/{id}/cancel`
中断）。求解器按「卸港最晚→危险品→冷藏→最重」排序贪心放置，逐步检查承重、
港序、隔离，最后整体校验 + 稳性闸门。**中断或失败不会留下半成品方案**：
方案与条目在同一事务中提交，取消即回滚。已锁定的箱作为 fixed 输入保留原位，
只重排未锁定部分。

### 装船确认（逐步锁定）
`POST /api/voyages/{id}/confirm`：只能对已发布方案、槽位号与实物一致才锁定；
`UNIQUE(voyage_id, container_id)` + 幂等返回使**重复确认安全**（并发 5 次只产生
1 条记录）。锁定后的箱不可被调整、不可甩箱。

### 晚到箱重 / 临时甩箱
`POST .../late-weight`、`POST .../roll`：只允许改动未锁定部分；自动从当前方案
派生新草稿并整体重校验，已锁定箱位原样保留。

### 航中改港（倒箱）
`POST .../resequence`：以新港序检查现有堆位能否安全卸载；不能满足时生成
**明确的逐步倒箱方案**（利用空位做缓冲，输出 `{container, from, to, reason}`
有序列表），并派生校验通过的新草稿。新港序之外的箱会被拒绝并提示先甩箱。

### 并发与一致性
- 所有多步写入在单个 `BEGIN IMMEDIATE` 事务中完成；SQLite 串行化写者，
  乐观检查（方案仍是草稿？箱仍未锁定？版本号是否前进？）在同一事务内执行，
  负者收到带 `retryable:true` 的 409 `VERSION_CONFLICT`。
- 两名计划员同时调整同一草稿：串行生效，各自收到自己改动的影响域校验结果。
- 两名计划员同时审批/回滚/发布：只有一人赢，输的一方拿到**可重试的版本错误**
  （含 `current_version` / `current_plan_id`），旧版本操作永不覆盖较新草稿。
- 重复发布凭同一令牌幂等；重复确认幂等；求解中断/回滚失败无残留。

### 方案差异、审批与安全回滚
**差异视图**（`stowage/diff.py`）：草稿对最近发布版本（或任意指定历史版本）
生成四类分类差异，页面与 API 均显示双方版本号、操作者与时间：

| 分类 | 内容 |
|---|---|
| 箱位变化 | 每箱 added / removed / moved（箱号、危险类别、锁定标记、from/to 槽位） |
| 危险品隔离 | 隔离对的违规 introduced / resolved、规则与三维距离变化 |
| 稳性指标 | GM/KG/LCG/纵倾/排水量 的前后值与 Δ，及限值达标情况 |
| 锁定箱变化 | 已确认箱 preserved / moved / added / missing |

差异摘要经规范化 JSON 计算 **SHA-256 digest**，作为审批令牌的绑定依据。

**一次性审批令牌**（`approval_tokens` 表）：
- 发布前必须 `POST .../approvals` 申请令牌；令牌绑定**草稿版本号 + 差异 digest**。
- 同一草稿内容重复申请返回原令牌（幂等）；草稿变化后再申请，旧令牌置 `voided`
  并写 `APPROVAL_VOID` 审计事件，签发新令牌。
- 发布时在事务内**重算 digest**：版本前进或差异摘要变化 → 409 `DIFF_MISMATCH`
  / `VERSION_CONFLICT`（均可重试），拒绝发布。
- 重复提交同一令牌：第一次消费并发布，后续调用回放同一发布结果（幂等），
  不会二次发布、不会改写首个消费者的审计记录。
- 校验不过（`PLAN_INVALID`）时事务回滚，令牌保持 active 供修正后重试。

**安全回滚**：`POST /api/plans/{id}/rollback` 将未发布草稿回滚到指定历史版本——
- 已锁定/已确认箱的物理槽位**强制保持不变**；历史方案中占用这些槽位的未锁定
  箱会被自动挪到合法缓冲位（`displaced_parked` 明示每一步）。
- 回滚结果执行**全船校验**（承重/悬空/冷藏/港序/隔离/稳性），生成**新版本草稿**
  与 `ROLLBACK` 事件（含源版本、目标版本、新版本、锁定保持数）。
- 整个回滚在单个 IMMEDIATE 事务内完成：任何失败（无缓冲位、版本不存在、版本
  冲突）都不会留下半成品版本；恢复后差异视图与审批状态与数据库最终版本一致。

## API 一览

```
POST /api/import                        导入航次（舱位结构+箱+船体参数）
GET  /api/voyages/{id}                  航次全貌（结构/箱/方案列表）
GET  /api/voyages/{id}/events           操作审计日志
GET  /api/voyages/{id}/versions         方案版本记录（版本号/状态/操作者/时间）
GET  /api/voyages/{id}/approvals        航次下全部审批令牌（含状态与时间）
POST /api/voyages/{id}/solve            生成候选配载（返回 job）
POST /api/voyages/{id}/resequence       改港评估 + 倒箱方案
POST /api/voyages/{id}/late-weight      晚到箱重
POST /api/voyages/{id}/roll             临时甩箱
POST /api/voyages/{id}/discharge        到港卸箱
POST /api/voyages/{id}/confirm          装船确认（幂等，逐步锁定）
GET  /api/jobs/{id}                     求解任务状态
POST /api/jobs/{id}/cancel              中断求解
GET  /api/plans/{id}                    方案明细（含稳性指标、最近审批令牌）
POST /api/plans/{id}/adjust             人工调整（返回影响域重校验）
POST /api/plans/{id}/validate           整体校验
GET  /api/plans/{id}/diff?base_version= 分类差异视图（默认对最近发布版本）
POST /api/plans/{id}/approvals          申请一次性审批令牌（绑定版本+差异摘要）
POST /api/plans/{id}/publish            凭令牌发布（令牌一次性；重复提交幂等）
POST /api/plans/{id}/rollback           安全回滚到历史版本（锁定箱不动+全船校验）
GET  /api/approvals/{token}             查询令牌状态（active/consumed/voided）
```

版本冲突错误体示例：
`{"error":{"code":"VERSION_CONFLICT","message":"...","retryable":true,
"current_version":4,"current_plan_id":"pln_..."}}`。

错误统一为 `{"error": {"code": ..., "message": ...}}`，HTTP 状态码语义化
（404 不存在 / 409 状态冲突 / 422 规则不满足）。

## 目录结构

```
stowage/
  db.py        SQLite schema、连接、ServiceError（含 approval_tokens 表）
  domain.py    导入解析、结构/箱模型、三维几何
  rules.py     危险品隔离规则表（可配置）
  validate.py  校验引擎（支持影响域 scope）
  diff.py      分类差异引擎（箱位/隔离/稳性/锁定 + SHA-256 digest）
  solver.py    贪心候选配载求解器（可中断）
  rehandle.py  改港倒箱规划
  service.py   业务服务层（事务、锁定、幂等、版本、审批、回滚）
  server.py    HTTP API + 静态页面（标准库 http.server）
static/index.html   舱位可视化、差异/审批/回滚操作界面
tests/test_system.py 38 个端到端用例
scripts/make_sample.py 示例数据生成器
```

## 简化假设

- 稳性为简化模型：KG/GM 由垂向力矩、纵倾由纵向力矩近似，KM 为常数；
  接口（`min_gm/max_gm/max_trim`）与真实稳性计算可平滑替换。
- 箱长按 40ft 槽位处理（20ft 箱占一个槽位，不跨槽）。
- 隔离规则表为常见类别的简化子集，可在 `rules.py` 中扩展。
