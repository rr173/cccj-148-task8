"""大 M 法单纯形（Bland 规则），纯 Python 标准库。

用于分舱目标体积求解：
  变量 x = 各压载舱目标体积（m³），外加 L1 调水量所需的偏差变量
  约束：水量守恒（等式）、每舱 min/max（界）、横倾/纵倾/吃水/弯矩（线性不等式）
  目标：相对当前体积的总调水量最小

构造：
  不等式 a·x <= b          -> 加非负松弛变量 s；b<0 时加人工变量 a（大 M 惩罚）
  等式   a·x = b           -> 加人工变量 a（大 M 惩罚）；b<0 行翻转
初始基 = 松弛变量（b>=0 行）或人工变量；最小化时人工变量在目标中系数 M 很大，
Bland 规则将其驱出基。终态若有人工变量显著为正 -> infeasible。

solve(c, Aeq, beq, Aub, bub, bounds) -> (x, status)，
status ∈ {"optimal","infeasible"}。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

EPS = 1e-9
BIG_M = 1.0e7


class Tableau:
    def __init__(self, n: int):
        self.n = n
        self.rows: List[List[float]] = []
        self.b: List[float] = []
        self.basis: List[int] = []
        self.zrow: List[float] = [0.0] * (n + 1)  # 末列为 z（最小化）

    def pivot(self, enter: int, leave_row: int):
        piv = self.rows[leave_row][enter]
        norm_row = [v / piv for v in self.rows[leave_row]]
        norm_b = self.b[leave_row] / piv
        for i in range(len(self.rows)):
            if i == leave_row:
                continue
            f = self.rows[i][enter]
            if abs(f) < EPS:
                continue
            self.rows[i] = [a - f * e
                            for a, e in zip(self.rows[i], norm_row)]
            self.b[i] -= f * norm_b
        f = self.zrow[enter]
        self.zrow = [a - f * e
                     for a, e in zip(self.zrow, norm_row + [norm_b])]
        self.rows[leave_row] = norm_row
        self.b[leave_row] = norm_b
        self.basis[leave_row] = enter

    def bland_enter(self) -> Optional[int]:
        cand = [j for j in range(self.n) if self.zrow[j] < -EPS]
        return min(cand) if cand else None

    def ratio_test(self, enter: int) -> Optional[int]:
        best = None
        best_ratio = None
        for i in range(len(self.rows)):
            a = self.rows[i][enter]
            if a <= EPS:
                continue
            r = self.b[i] / a
            if r < -1e-7:                 # 保持基本可行解
                continue
            if best is None or r < best_ratio - EPS or \
               (abs(r - best_ratio) <= EPS and self.basis[i] < best):
                best, best_ratio = i, r
        return best


def solve(c: List[float],
          Aeq: Optional[List[List[float]]] = None,
          beq: Optional[List[float]] = None,
          Aub: Optional[List[List[float]]] = None,
          bub: Optional[List[float]] = None,
          bounds: Optional[List[Tuple[float, float]]] = None
          ) -> Tuple[Optional[List[float]], str]:
    Aeq = Aeq or []
    beq = beq or []
    Aub = Aub or []
    bub = bub or []
    n = len(c)
    if bounds is None:
        bounds = [(0.0, 1e12)] * n

    # 界转不等式：-x_j <= -lo, x_j <= hi
    extra_a, extra_b = [], []
    for j, (lo, hi) in enumerate(bounds):
        r = [0.0] * n
        r[j] = -1.0
        extra_a.append(r)
        extra_b.append(-lo)
        r2 = [0.0] * n
        r2[j] = 1.0
        extra_a.append(r2)
        extra_b.append(hi)
    all_a = Aub + extra_a + Aeq
    all_b = bub + extra_b + list(beq)
    n_ub = len(Aub) + len(extra_a)

    n_rows = len(all_a)
    # 先统计需要的列：每行一个松弛；b<0 的不等式行与等式行加人工变量
    art_rows = [i for i in range(n_rows) if i >= n_ub or all_b[i] < -EPS]
    n_slack = n_ub
    n_art = len(art_rows)
    total = n + n_slack + n_art
    art_of_row = {r: n + n_slack + k for k, r in enumerate(art_rows)}
    tab = Tableau(total)

    slack_col = n
    for i, (r, bv) in enumerate(zip(all_a, all_b)):
        row = r + [0.0] * (n_slack + n_art)
        if i < n_ub:
            row[slack_col + i] = 1.0
        if i in art_of_row:
            row[art_of_row[i]] = 1.0
        if bv < -EPS:
            row = [-v for v in row]
            bv = -bv
        tab.rows.append(row)
        tab.b.append(bv)
        tab.basis.append(art_of_row.get(i, slack_col + i))

    # 目标行：c（决策变量）+ 大 M（人工变量）；初始基消元
    zrow = c + [0.0] * n_slack + [BIG_M] * n_art + [0.0]
    for i, bv_col in enumerate(tab.basis):
        f = zrow[bv_col]
        if abs(f) < EPS:
            continue
        for j in range(total):
            zrow[j] -= f * tab.rows[i][j]
        zrow[-1] += f * tab.b[i]
    tab.zrow = zrow

    status = _run(tab)
    # 可行性：人工变量不得显著为正
    art_val = 0.0
    for i, bv_col in enumerate(tab.basis):
        if bv_col >= n + n_slack:
            art_val += tab.b[i]
    if art_val > 1e-4:
        return None, "infeasible"

    x = [0.0] * n
    for i, bv_col in enumerate(tab.basis):
        if bv_col < n:
            x[bv_col] = tab.b[i]
    # 非基决策变量（取 0）天然已为 0
    return x, "optimal"


def _run(tab: Tableau, max_iter: int = 50000) -> str:
    """最小化：zrow[j] < 0 入基。"""
    for _ in range(max_iter):
        enter = tab.bland_enter()
        if enter is None:
            return "optimal"
        row = tab.ratio_test(enter)
        if row is None:
            return "unbounded"
        tab.pivot(enter, row)
    raise RuntimeError("simplex iteration limit")
