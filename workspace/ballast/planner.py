"""分阶段调水方案规划器。

输入：当前液位、拓扑、当前已装货、已发布方案中待装的货（分阶段）。
输出：有序步骤列表。每步：源舱、目标舱、目标体积、允许误差、所需泵阀、
      开始条件（上一步已确认 + 本阶段装船重量到位）。

不变量（生成时即保证，测试逐条断言）：
  * 水量守恒 —— 只搬运不加注/排放，各阶段总水量恒等于当前总水量；
  * 任何阶段、任何中间时刻，舱位都在 [min_level, max_level] 内
    （每舱在同一阶段内只会单向变化，端点合法 ⇒ 中间合法）；
  * 每步通路恰含一台泵，且同一步不占用两根同互斥组的管路；
  * 步骤严格串行，资源（泵/互斥管路）不会被两步同时占用。
"""
from . import stability
from .domain import find_path

EPS = 1e-6
MIN_STEP_VOLUME = 0.05          # 小于此量的调水视为噪声，不生成步骤


class Infeasible(Exception):
    """目标稳性不可达 / 拓扑不可达。details 带给前端的违规明细。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def ballast_items(tanks, levels):
    return [(levels[t.name] * t.density, t.x, t.y, t.z)
            for t in tanks.values() if levels.get(t.name, 0) > 0]


def solve_target_levels(ship, tanks, levels, cargo_items):
    """在总水量守恒、各舱 [min,max] 约束下，把液位调到全部限值内。

    贪心下降：每轮在所有可行的舱对转移里选使违规分下降最多的一笔，
    量子从粗到细多轮扫描。无法降到 0 时抛 Infeasible（目标稳性不可达）。
    """
    cur = {t.name: float(levels[t.name]) for t in tanks.values()}
    total = sum(cur.values())

    def score_of(lv):
        m = stability.metrics(ship, cargo_items, ballast_items(tanks, lv))
        return stability.violation_score(ship, m)

    if score_of(cur) <= 1e-12:
        return cur
    names = sorted(tanks)
    for frac in (0.10, 0.05, 0.02, 0.01, 0.005):
        quantum = max(0.25, total * frac)
        for _ in range(400):
            s0 = score_of(cur)
            if s0 <= 1e-12:
                return cur
            best = None
            for a in names:
                give = min(quantum, cur[a] - tanks[a].min_level)
                if give <= EPS:
                    continue
                for b in names:
                    if a == b:
                        continue
                    take = min(give, tanks[b].max_level - cur[b])
                    if take <= EPS:
                        continue
                    trial = dict(cur)
                    trial[a] -= take
                    trial[b] += take
                    s1 = score_of(trial)
                    if s1 < s0 - 1e-12 and (best is None or s1 < best[0]):
                        best = (s1, a, b, take)
            if best is None:
                break
            _, a, b, take = best
            cur[a] -= take
            cur[b] += take
    m = stability.metrics(ship, cargo_items, ballast_items(tanks, cur))
    raise Infeasible("STABILITY_UNREACHABLE",
                     "目标稳性不可达：在舱容与水量守恒约束下无法同时满足 "
                     "横倾/纵倾/吃水/受力限值",
                     {"violations": stability.violations(ship, m),
                      "levels": {k: round(v, 3) for k, v in cur.items()}})


def transfers_between(lv_from, lv_to):
    """把两版液位的差值分解为 源→目标 转移列表（每舱只进或只出）。"""
    sources = {t: lv_from[t] - lv_to[t] for t in lv_from
               if lv_from[t] - lv_to[t] > MIN_STEP_VOLUME}
    targets = {t: lv_to[t] - lv_from[t] for t in lv_from
               if lv_to[t] - lv_from[t] > MIN_STEP_VOLUME}
    out = []
    while sources and targets:
        s = max(sources, key=lambda k: (sources[k], k))
        d = max(targets, key=lambda k: (targets[k], k))
        v = min(sources[s], targets[d])
        out.append((s, d, v))
        sources[s] -= v
        targets[d] -= v
        if sources[s] <= MIN_STEP_VOLUME:
            del sources[s]
        if targets[d] <= MIN_STEP_VOLUME:
            del targets[d]
    return out


def split_stages(remaining, n_stages):
    """待装箱按纵向位置排序后均分为 n 个装船阶段（确定性）。"""
    items = sorted(remaining, key=lambda c: (c["x"], c["id"]))
    n = max(1, min(n_stages, len(items))) if items else 0
    chunks, base, extra = [], len(items) // max(n, 1), len(items) % max(n, 1)
    pos = 0
    for k in range(n):
        size = base + (1 if k < extra else 0)
        chunks.append(items[pos:pos + size])
        pos += size
    return chunks


def generate_steps(ship, topo, current_levels, cargo_now, cargo_remaining,
                   n_stages=3, tolerance=2.0):
    """返回 (steps, stage_targets)。任何不可达都抛 Infeasible。"""
    total0 = sum(current_levels.values())
    chunks = split_stages(cargo_remaining, n_stages)

    # 各阶段累计货载：stage 0 = 当前已装；stage k = 再装上第 k 批
    stage_cargo = [list(cargo_now)]
    acc = list(cargo_now)
    for ch in chunks:
        acc = acc + [(c["weight"], c["x"], c["y"], c["z"]) for c in ch]
        stage_cargo.append(acc)

    # 逐阶段求目标液位（以上一阶段目标为暖启动，尽量少泵水）
    targets = []
    lv = dict(current_levels)
    for k, cargo in enumerate(stage_cargo):
        try:
            lv = solve_target_levels(ship, topo.tanks, lv, cargo)
        except Infeasible as e:
            e.details["stage"] = k
            raise
        # 水量守恒硬校验：规划器只搬运，总量必须逐阶段不变
        assert abs(sum(lv.values()) - total0) < 1e-6, "water not conserved"
        targets.append(dict(lv))

    # 相邻阶段液位差 → 转移步骤
    steps = []
    seq = 0
    base_weight = sum(w for w, *_ in cargo_now)
    prev_levels = dict(current_levels)
    for k, target in enumerate(targets):
        # 阶段 k 的开始门：该阶段货载已上船（stage_cargo[k] = 当前 + 前 k 批）
        stage_gate_weight = base_weight + sum(
            c["weight"] for ch in chunks[:k] for c in ch)
        for src, dst, vol in transfers_between(prev_levels, target):
            path = find_path(topo, src, dst)
            if path is None:
                raise Infeasible(
                    "NO_PATH",
                    f"泵阀冲突/连通缺失：{src} → {dst} 找不到恰含一台泵、"
                    f"且不自相斥的通路",
                    {"stage": k, "source": src, "target": dst,
                     "volume": round(vol, 3)})
            seq += 1
            tol = max(0.05, min(tolerance, 0.2 * vol))
            steps.append({
                "seq": seq, "stage": k,
                "source_tank": src, "target_tank": dst,
                "target_volume": round(vol, 3), "tolerance": round(tol, 3),
                "pump_id": path["pump"], "valves": path["valves"],
                "pipes": path["pipes"],
                "mutex_groups": sorted({topo.pipes[p].mutex_group
                                        for p in path["pipes"]
                                        if topo.pipes[p].mutex_group}),
                "start_condition": {
                    "previous_step": seq - 1 if seq > 1 else None,
                    "confirmed_weight_gte": round(stage_gate_weight, 3),
                },
                "est_duration_min": round(
                    vol / topo.pumps[path["pump"]].rate * 60.0, 1),
            })
        prev_levels = dict(targets[k])
    return steps, targets
