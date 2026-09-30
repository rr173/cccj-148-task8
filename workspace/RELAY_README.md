# 船岸断网事件中继器（ship-shore disconnect relay）

码头手持终端在断网期间把装船扫描（箱号 / 实际槽位 / 采集时间）写入**只追加的
本地段文件**；网络恢复后，中继器按**设备会话 + 递增序号**把段文件上传到接收
服务。接收端保存**原始包**与**确认游标**，对已接受记录再调用**现有装船确认
入口**（`stowage.service.confirm_load`）。全程保证：

| 情形 | 行为 |
|---|---|
| 相同(会话,序号,内容)再次到达 | 回**原确认**，不重复写明细、不重复装船确认 |
| 相同序号、不同内容 | 进**隔离队列**，游标不动 |
| 序号断层（缺中间序号） | 先**暂存**，但**不推进游标**；补齐后连续处理 |
| 校验和错误 | **整包拒绝**，不写任何记录（原件留 rejected 台账） |
| 包内任一记录非法 | **整包拒绝**，绝不只写入部分记录 |
| 两台终端报同一箱 / 箱已在别处确认 / 计划已更替 | 只保留**一项可执行结果**，其余进**待处理** |
| 操作员重复提交同一处置请求 | 按 `request_id` 幂等，**只记一次** |
| 落盘 / 调业务入口 / 更新游标之间退出 | 重启**从事件日志恢复**，收件状态/装船确认/游标一致 |

纯 Python 标准库，无第三方依赖。

## 快速开始

```bash
# 1) 准备一个已发布配载航次（对接现有配载系统）
python3 -m stowage ...                      # 或使用测试夹具（见下）

# 2) 启动接收服务（启动时自动执行恢复）
STOWAGE_DB=stowage.db python3 run_relay.py 8090 data/relay.db stowage.db

# 3) 边缘模拟器：断网采集 → 封段 → 恢复后按序上传
python3 -m relay.emulator init  /tmp/spool HH-01
python3 -m relay.emulator scan  /tmp/spool HH-01 MSKU0000109 02-01-01
python3 -m relay.emulator scan  /tmp/spool HH-01 MSKU0000095 02-02-01
python3 -m relay.emulator seal  /tmp/spool HH-01 --voyage voy_xxx
python3 -m relay.emulator upload /tmp/spool HH-01 --url http://127.0.0.1:8090 --all

# 4) 操作台：四栏总览 / 航次映射 / 处置 / 一致性自检
python3 -m relay.ctl --url http://127.0.0.1:8090 board
python3 -m relay.ctl --url http://127.0.0.1:8090 map --voyage voy_xxx
python3 -m relay.ctl --url http://127.0.0.1:8090 verify
```

一键演示（临时目录内覆盖全部场景）：

```bash
python3 scripts/demo_relay.py
python3 -m unittest discover -s relay/tests   # 20 个端到端用例
```

## 段包线格式

一个封闭段 = 一个上传包。校验和覆盖除 `checksum` 外全部字段
（`sort_keys` UTF-8 JSON 的 SHA-256），接收端用同一份规范字节复算：

```json
{
  "device_id": "HH-01",
  "session": "HH-01:2026-09-28T08:00:00+08:00",
  "seq": 7,
  "voyage_id": "voy_...",
  "records": [
    {"record_id": "rec_uuid", "container": "MSKU0000109",
     "slot": "02-01-01", "collected_at": "2026-09-28T09:10:11+08:00"}
  ],
  "checksum": "<sha256-hex>"
}
```

记录合法性（任一不过即整包拒绝）：

- `container`：ISO 6346 箱号（4 字母 + 7 数字，**含第 11 位校验位核对**）
- `slot`：`bay-row-tier` 形如 `02-01-82`
- `collected_at`：ISO 8601 时间
- `record_id`：非空、包内唯一

## 收件流水线与游标

游标 `cursor` = 该会话**已连续确认的最大序号**（初始 -1，首包序号为 0，
下一个期望 = cursor+1）。一个连续序号包到达后：

1. 校验和 → 逐记录合法性（失败：`rejected`，原件留档，不占序号）；
2. 相同(序号,内容哈希) → 回放原确认（`duplicate=true`）；
3. 同序号不同内容 → `quarantine`；
4. `seq > cursor+1` → `staged`（暂存原件，不写明细）；
5. `seq == cursor+1`：
   - 事务 A 原子写入 `raw_packets`（accepted）+ **全部** `scan_records`；
   - 逐条做争用/计划解析并调用现有装船确认入口（每条独立事务）；
   - 推进游标；
   - 随后尝试排空暂存区：若 cursor+1 的暂存包已到，升级并重复 5。

每条记录映射到当前航次后的终态：

| status | 含义 |
|---|---|
| `applied` | 已调用装船入口并拿到 `confirmation_id` |
| `pending` | 争用/计划更替/别处确认/槽位冲突，等待操作员处置 |
| `ignored` | 操作员忽略（保留业务侧已有的装船确认，不卸船） |

`pending` 原因码：`DUPLICATE_CONTAINER` / `CONFIRMED_ELSEWHERE` /
`PLAN_CHANGED` / `NOT_IN_PLAN` / `NO_PUBLISHED_PLAN` / `SLOT_OCCUPIED`。

## 操作台处置（幂等）

`POST /relay/dispose`，body 中 `request_id` 为幂等键；同一请求只产生一条
`dispositions` 与一次业务效果：

- `ignore`：放弃该扫描（不撤销任何已完成的装船确认）；
- `remap` + `new_slot`：重新映射到**当前已发布方案**为该箱指定的槽位并确认；
  目标槽位与方案不符返回 409（`REMAP_SLOT_MISMATCH`）；
- `reconfirm`：重新调用装船入口；若条件仍不满足或仍命中既有确认，记录保持
  `pending`（`outcome=noop`）并返回 409。

## 三个崩溃恢复点

`RELAY_CRASH=P1|P2|P3` 时，每个点在进程生命周期内触发一次，
通过 `os._exit(91)` 模拟掉电（不执行 finally/atexit）：

| 点 | 位置 | 崩溃瞬间状态 | 重启恢复 |
|---|---|---|---|
| P1 | 落盘事务提交前 | 原始包/明细**整体回滚**，无残留 | 无可补偿内容；终端重传即正常接受 |
| P2 | 逐条调业务入口途中 | 包+全明细已落盘，部分箱已确认，游标未推进 | 补处理剩余 `received` 记录；已确认记录靠业务入口 `UNIQUE(voyage,container)` 幂等安全跳过；推进游标 |
| P3 | 游标事务提交后、响应前 | 全部工作已完成 | 纯重放，不产生重复确认 |

恢复在接收服务启动与 `POST /relay/recover` 时执行；恢复结束后继续排空暂存
断层。`GET /relay/verify` 校验：游标连续性、accepted 包明细完整、applied
记录在业务确认表中真实存在、同航次同箱至多一项 applied。

## HTTP API

```
POST /relay/upload                 段包上传（200 接受 / 400 结构错 / 422 校验和或记录非法 / 409 隔离）
GET  /relay/sessions               会话/游标/暂存/隔离概览
GET  /relay/pending|gaps|quarantine|applied|rejected
GET  /relay/voyages/{vid}/map      扫描记录 → 当前航次方案的映射
GET  /relay/events                 事件日志（恢复依据）
POST /relay/dispose                忽略 / 重映射 / 重调（request_id 幂等）
POST /relay/config                 {"active_voyage": "..."}
POST /relay/recover                手动触发恢复（返回补偿动作 + verify 结果）
GET  /relay/verify                 收件状态/装船确认/游标一致性自检
GET  /healthz
```

## 目录

```
relay/
  protocol.py   线格式、SHA-256 校验和、ISO 6346 箱号与记录合法性
  segment.py    终端只追加段文件（scan→fsync→seal 不可变包）、上传状态
  store.py      接收端 SQLite（sessions/raw_packets/scan_records/dispositions/relay_events）
  loading.py    现有装船确认入口网关与当前方案解析
  receiver.py   收件流水线、争用判定、断层排空、崩溃恢复
  console.py    操作台处置（ignore/remap/reconfirm，幂等）
  views.py      四栏视图、航次映射、一致性自检
  server.py     HTTP 服务（标准库）
  emulator.py   边缘模拟器 CLI（含 tamper / bad-record 演练）
  ctl.py        操作台 CLI
  tests/        20 个端到端用例（含真实子进程 P1/P2/P3 崩溃恢复）
run_relay.py    接收服务启动入口（启动即 recover）
scripts/demo_relay.py  一键全场景演练
```

## 边缘段文件布局

```
<spool>/<device_id>/
  session.txt             当前会话（每次开工新建，切换前自动封段）
  current.seg             开放段，只追加 JSONL；每条扫描 flush+fsync
  sealed/000007.json      已封不可变段（带 checksum 的上传包）
  state.json              各段 pending|sent|acked 状态与原确认
```

待传 = `sealed/` 中状态为 pending/sent 的段，严格按序号递增上传。
