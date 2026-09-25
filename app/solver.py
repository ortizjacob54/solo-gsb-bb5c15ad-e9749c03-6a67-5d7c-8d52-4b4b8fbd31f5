"""三相配电最优分配求解器（纯标准库实现，便于独立测试）。

问题：每台设备不可拆分，只能接入其允许集合中的一个相位；
所有设备均须接入且各相负载不超过对应容量。
目标：最小化「三相负载最大值与最小值之差」；
差值相同时，取按设备编号排序后的相位序列字典序最小的方案。

求解方式：按设备编号排序后做深度优先搜索，相位按 A/B/C 顺序展开，
因此完整方案按相位序列的字典序依次产生；仅在差值严格更小时更新最优解，
即可保证同差值下保留字典序最小方案。同时使用容量约束与下界剪枝加速。

扩容问题（solve_capacity_upgrade）：原容量下无可行分配时，保持设备功率与
允许相位不变，逐相增加非负整数容量，使所有设备均可接入。目标依次为：
1) 三相扩容总量最小；2) 单相最大扩容量最小；3) 仍相同时取按设备编号排序的
相位序列字典序最小。枚举所有相位序列（至多 3^12），分两遍 DFS 配合
扩容总量 / 单相扩容量的上下界剪枝求解。

稳健分相预演（solve_robust）：每台设备功率为正整数区间 [min_power, max_power]，
区间内可独立取任意整数。对固定分配，某相负载可独立取遍
[该相 min_power 之和, 该相 max_power 之和] 内的所有整数，因此：
- 稳健可行（任意功率组合下各相均不超容量）⟺ 各相负载上界不超容量；
- 任意功率组合可能产生的最坏负载差
  = max_{L}(max_p L_p - min_p L_p) = max_{p≠q}(上界_p - 下界_q)
  （注意不是「上界最大值 - 下界最小值」：同一相不可能同时取到上界与下界）。
据此归约为与 solve 同构的 DFS：容量约束作用于负载上界，目标函数为上述
有序相异相位对差值的最大值，同值时取按设备编号排序的相位序列字典序最小者。

启动顺序预演（solve_startup_plan）：沿用可行计算记录的固定相位与容量，
每台设备另给定不低于额定功率的启动瞬时功率。设备一台接一台启动：启动瞬间
该设备按启动功率计，此前已启动的设备按额定功率持续运行，启动完成后该设备
亦按额定功率运行；每个启动瞬间与每个稳定状态各相负载均不得超容量。
目标：最小化全过程「三相负载最大值与最小值之差」的峰值；同峰值取设备编号
序列字典序最小的启动顺序。

关键观察：任一启动顺序对应子集 DAG 上 空集→全集 的一条路径——途经节点为
已启动设备集合（稳定状态），途经边为一次启动（瞬时状态）；节点与边的负载差
只取决于集合 / 边本身，与到达路径无关。故「最小化全过程负载差峰值」等价于
子集 DAG 上的 minimax 路径，按子集递推即可（n≤12，2^12 规模瞬时完成）；
同峰值取编号序列字典序最小：先求最优峰值 P*，再自全集反向递推「途经状态
负载差均不超 P* 且满足容量、可达全集」的子集集合，最后自空集出发按设备
编号字典序贪心选边。容量方面：稳定状态负载是最终负载的部分和（记录可行时
自动满足，求解器仍显式校验以独立成立）；启动瞬间需校验
「启动前该相负载 + 启动功率 ≤ 该相容量」（其余相与稳定状态相同）。

单相故障保供预演（solve_phase_fault_rehearsal）：给定一条已有可行分配的
记录与故障相，健康相设备保持原分配不动（其负载构成各健康相的初始负载），
故障相设备只能迁至其允许的健康相（不得使该相超容量）或断开，故障相最终
负载必为零。全部断开总是可行，故问题总有应急方案。目标依次为：
1) 保留设备的总功率最大；2) 再使保留设备数最多；3) 仍同值时，按故障相
设备编号排序，取相位选择序列字典序最小者（A＜B＜C＜断开）。只需对故障相
设备（至多 12 台）做 DFS，每台按「允许的健康相 A→B→C，最后断开」的顺序
展开，故完整方案按选择序列字典序依次产生，仅在 (保留功率, 保留设备数)
严格更优时更新，同值自然保留字典序最小方案；并以剩余设备功率和 / 数量和
做乐观上界剪枝。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Iterable, Optional

PHASES: tuple[str, str, str] = ("A", "B", "C")


@dataclass(frozen=True)
class Device:
    """待分配设备。"""

    code: str  # 设备唯一编号
    power: int  # 正整数功率
    allowed: tuple[str, ...]  # 可接入相位集合（PHASES 的非空子集）


@dataclass(frozen=True)
class SolveResult:
    """求解结果。feasible 为 False 时其余字段为 None。"""

    feasible: bool
    assignment: Optional[dict[str, str]]  # 设备编号 -> 相位
    loads: Optional[dict[str, int]]  # 相位 -> 负载
    imbalance: Optional[int]  # 最大相负载 - 最小相负载


def solve(devices: Iterable[Device], capacities: dict[str, int]) -> SolveResult:
    """求解最优分配方案。

    :param devices: 设备集合（编号唯一、功率为正整数、允许相位非空）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :return: SolveResult；无可行分配时 feasible=False。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    powers = [d.power for d in ordered]
    # 每个设备的可接入相位按 A/B/C 固定顺序排列，保证 DFS 按字典序枚举方案
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    # suffix_addable[i][p]：第 i..n-1 台设备中可接入相位 p 的功率和（剪枝用）
    suffix_addable = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        prev, cur = suffix_addable[i + 1], suffix_addable[i]
        for p in PHASES:
            cur[p] = prev[p] + (powers[i] if p in allowed[i] else 0)

    # suffix_total[i]：第 i..n-1 台设备的功率总和
    suffix_total = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_total[i] = suffix_total[i + 1] + powers[i]

    loads = {p: 0 for p in PHASES}
    current: list[Optional[str]] = [None] * n
    best_imbalance: Optional[int] = None
    best_assignment: Optional[list[str]] = None

    def dfs(i: int, used: int) -> None:
        nonlocal best_imbalance, best_assignment
        if best_imbalance == 0:
            return  # 已达成理论最优（差值为 0），无需继续搜索
        if i == n:
            values = loads.values()
            imbalance = max(values) - min(values)
            # 仅严格更优才更新：同差值时先枚举到的方案字典序更小
            if best_imbalance is None or imbalance < best_imbalance:
                best_imbalance = imbalance
                best_assignment = current.copy()
            return
        if best_imbalance is not None:
            # 下界剪枝：
            #   最终最大负载 >= max(当前最大负载, 总功率平均值上取整)
            #   最终最小负载 <= min(当前各相负载 + 该相剩余可接入功率)
            remaining = suffix_total[i]
            lb_max = max(max(loads.values()), ceil((used + remaining) / 3))
            ub_min = min(loads[p] + suffix_addable[i][p] for p in PHASES)
            if lb_max - ub_min >= best_imbalance:
                return
        for phase in allowed[i]:
            if loads[phase] + powers[i] <= caps[phase]:
                loads[phase] += powers[i]
                current[i] = phase
                dfs(i + 1, used + powers[i])
                loads[phase] -= powers[i]
                current[i] = None

    dfs(0, 0)

    if best_assignment is None:
        return SolveResult(feasible=False, assignment=None, loads=None, imbalance=None)

    final_loads = {p: 0 for p in PHASES}
    for idx, phase in enumerate(best_assignment):
        final_loads[phase] += powers[idx]
    assignment = {ordered[i].code: best_assignment[i] for i in range(n)}
    return SolveResult(
        feasible=True,
        assignment=assignment,
        loads=final_loads,
        imbalance=best_imbalance,
    )


@dataclass(frozen=True)
class UpgradeResult:
    """扩容求解结果。

    original_capacities：原各相容量；upgrades：各相增加的非负整数容量；
    suggested_capacities：原容量 + 扩容量；assignment：设备编号 -> 相位；
    loads：相位 -> 实际负载。
    """

    original_capacities: dict[str, int]
    upgrades: dict[str, int]
    suggested_capacities: dict[str, int]
    assignment: dict[str, str]
    loads: dict[str, int]
    total_upgrade: int  # 三相扩容总量
    max_upgrade: int  # 单相最大扩容量


def _upgrade_bounds(
    i: int,
    loads: dict[str, int],
    caps: dict[str, int],
    suffix_forced: list[dict[str, int]],
    suffix_free_total: list[int],
) -> tuple[int, int]:
    """返回当前部分分配状态下扩容总量与单相扩容的乐观下界。

    返回 (lb_total, lb_max)：
    lb_total —— 三相扩容总量的下界；
    lb_max   —— 单相扩容量的下界，取 max(被迫超载量, ceil(lb_total/3))。

    后续只允许单一相位的设备所落相位已确定（被迫负载 forced），其超载必须
    扩容承担；其余自由设备（允许集合至少两个相位）的功率总和 F 忽略相位
    限制（视为可任意切分），先填充各相被迫负载之外的空闲容量，填不满的
    部分才必须扩容。忽略自由设备的相位限制只会低估扩容，故为合法下界。
    """
    forced_deficit = 0  # 被迫负载造成的必扩容总量
    spare = 0  # 各相可接纳自由设备功率的空闲容量之和
    forced_max = 0
    for p in PHASES:
        forced = loads[p] + suffix_forced[i][p]
        if forced > caps[p]:
            deficit = forced - caps[p]
            forced_deficit += deficit
            if deficit > forced_max:
                forced_max = deficit
        else:
            spare += caps[p] - forced
    lb_total = forced_deficit + max(0, suffix_free_total[i] - spare)
    return lb_total, max(forced_max, ceil(lb_total / 3))


def solve_capacity_upgrade(
    devices: Iterable[Device], capacities: dict[str, int]
) -> UpgradeResult:
    """在保持设备功率与允许相位不变的前提下，求解最小扩容方案。

    目标依次为：三相扩容总量最小 → 单相最大扩容量最小 →
    按设备编号排序的相位序列字典序最小。扩容后总有可行方案
    （每台设备至少存在一个允许相位，各相扩至足够大即可接入）。

    :param devices: 设备集合（编号唯一、功率为正整数、允许相位非空）。
    :param capacities: 各相原容量，形如 {"A": int, "B": int, "C": int}。
    :return: UpgradeResult。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    powers = [d.power for d in ordered]
    # 每个设备的可接入相位按 A/B/C 固定顺序排列，保证 DFS 按字典序枚举方案
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    # suffix_forced[i][p]：第 i..n-1 台设备中只允许相位 p 的设备功率和
    # suffix_free_total[i]：第 i..n-1 台设备中允许集合非单一的设备功率总和
    suffix_forced = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    suffix_free_total = [0] * (n + 1)
    for idx in range(n - 1, -1, -1):
        prev_f, cur_f = suffix_forced[idx + 1], suffix_forced[idx]
        for p in PHASES:
            cur_f[p] = prev_f[p]
            if p in allowed[idx] and len(allowed[idx]) == 1:
                cur_f[p] += powers[idx]
        suffix_free_total[idx] = suffix_free_total[idx + 1] + (
            powers[idx] if len(allowed[idx]) > 1 else 0
        )

    loads = {p: 0 for p in PHASES}
    current: list[Optional[str]] = [None] * n

    # 第一遍：最小化三相扩容总量
    best_total: Optional[int] = None
    root_lb_total, _ = _upgrade_bounds(
        0, loads, caps, suffix_forced, suffix_free_total
    )
    optimal_total_found = False

    def dfs_total(i: int) -> None:
        nonlocal best_total, optimal_total_found
        if optimal_total_found:
            return  # 已命中空状态理论最优下界，且回溯由各层正常完成
        lb_total, _ = _upgrade_bounds(
            i, loads, caps, suffix_forced, suffix_free_total
        )
        # 已无法严格改善当前最优（下界不会随分配下降，剪枝安全）
        if best_total is not None and lb_total >= best_total:
            return
        if i == n:
            total = sum(max(0, loads[p] - caps[p]) for p in PHASES)
            if best_total is None or total < best_total:
                best_total = total
                if total == root_lb_total:
                    optimal_total_found = True
            return
        for phase in allowed[i]:
            loads[phase] += powers[i]
            current[i] = phase
            dfs_total(i + 1)
            loads[phase] -= powers[i]
            current[i] = None
            if optimal_total_found:
                break

    dfs_total(0)

    assert best_total is not None  # 扩容后必有解
    target_total = best_total

    # 第二遍：在扩容总量固定最小的方案中，以 (单相最大扩容量, 相位序列) 为
    # 键取字典序最小，即先最小化单相最大扩容、再最小化相位序列。
    best_max: Optional[int] = None
    best_assignment: Optional[list[str]] = None
    best_key: Optional[tuple[int, tuple[str, ...]]] = None

    def dfs_max(i: int) -> None:
        nonlocal best_max, best_assignment, best_key
        lb_total, lb_max = _upgrade_bounds(
            i, loads, caps, suffix_forced, suffix_free_total
        )
        if lb_total > target_total:
            return
        # 仅当子树不可能得到「严格更小」的单相最大扩容时剪枝；
        # lb_max == best_max 时子树仍可能含有同值但字典序更小的方案，不可剪枝
        if best_max is not None and lb_max > best_max:
            return
        if i == n:
            extras = {p: max(0, loads[p] - caps[p]) for p in PHASES}
            if sum(extras.values()) != target_total:
                return
            current_max = max(extras.values())
            # 以 (单相最大扩容, 相位序列) 为键严格更优才更新，
            # 同 max 时保留字典序更小的完整方案
            candidate_key = (current_max, tuple(current))
            if best_key is None or candidate_key < best_key:
                best_key = candidate_key
                best_max = current_max
                best_assignment = current.copy()
            return
        for phase in allowed[i]:
            loads[phase] += powers[i]
            current[i] = phase
            dfs_max(i + 1)
            loads[phase] -= powers[i]
            current[i] = None

    dfs_max(0)

    assert best_assignment is not None
    final_loads = {p: 0 for p in PHASES}
    for idx, phase in enumerate(best_assignment):
        final_loads[phase] += powers[idx]
    upgrades = {p: max(0, final_loads[p] - caps[p]) for p in PHASES}
    suggested = {p: caps[p] + upgrades[p] for p in PHASES}
    assignment = {ordered[i].code: best_assignment[i] for i in range(n)}
    return UpgradeResult(
        original_capacities=dict(caps),
        upgrades=upgrades,
        suggested_capacities=suggested,
        assignment=assignment,
        loads=final_loads,
        total_upgrade=sum(upgrades.values()),
        max_upgrade=max(upgrades.values()),
    )


@dataclass(frozen=True)
class RobustDevice:
    """稳健预演设备：功率为正整数区间 [min_power, max_power]。"""

    code: str  # 设备唯一编号
    min_power: int  # 功率下界（正整数）
    max_power: int  # 功率上界（正整数，不小于下界）
    allowed: tuple[str, ...]  # 可接入相位集合（PHASES 的非空子集）


@dataclass(frozen=True)
class RobustResult:
    """稳健预演求解结果。feasible 为 False 时其余字段为 None。

    load_lower / load_upper：各相负载下界 / 上界（该相设备分别取功率
    下界 / 上界之和）；worst_imbalance：任意功率组合下「三相负载最大值
    与最小值之差」可能达到的最大值。
    """

    feasible: bool
    assignment: Optional[dict[str, str]]  # 设备编号 -> 相位
    load_lower: Optional[dict[str, int]]  # 相位 -> 负载下界
    load_upper: Optional[dict[str, int]]  # 相位 -> 负载上界
    worst_imbalance: Optional[int]  # 最坏负载差


def solve_robust(
    devices: Iterable[RobustDevice], capacities: dict[str, int]
) -> RobustResult:
    """求解稳健分相预演：任意功率组合下各相均不超容量，且最坏负载差最小。

    对固定分配，各相负载独立取遍 [下界, 上界] 内的所有整数，故：
    - 稳健可行 ⟺ 各相负载上界（max_power 之和）不超过对应容量；
    - 最坏负载差 = max_{p≠q}(上界_p - 下界_q)
      （max_L(max_p L_p - min_p L_p) = max_{p≠q} max_L(L_p - L_q)，
      同一相不可能同时取到上界与下界，故只取相异相位对）。

    目标：在稳健可行方案中最小化最坏负载差；同值时取按设备编号排序的
    相位序列字典序最小者。搜索结构与 solve 相同：按设备编号排序 DFS、
    相位按 A/B/C 展开、仅严格更优才更新，配合容量约束与下界剪枝。

    :param devices: 设备集合（编号唯一、0 < min_power <= max_power、允许相位非空）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :return: RobustResult；无稳健可行分配时 feasible=False。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    lows = [d.min_power for d in ordered]
    highs = [d.max_power for d in ordered]
    # 每个设备的可接入相位按 A/B/C 固定顺序排列，保证 DFS 按字典序枚举方案
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    # suffix_addable_lo[i][p]：第 i..n-1 台设备中可接入相位 p 的功率下界和（剪枝用）
    suffix_addable_lo = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    # suffix_forced_hi[i][p]：第 i..n-1 台设备中只允许相位 p 的设备功率上界和（剪枝用）
    suffix_forced_hi = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    # suffix_forced_lo[i][p]：同上，功率下界和（剪枝用）
    suffix_forced_lo = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    # suffix_lo[i] / suffix_hi[i]：第 i..n-1 台设备的功率下界 / 上界总和
    suffix_lo = [0] * (n + 1)
    suffix_hi = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        prev_lo, cur_lo = suffix_addable_lo[i + 1], suffix_addable_lo[i]
        prev_fh, cur_fh = suffix_forced_hi[i + 1], suffix_forced_hi[i]
        prev_fl, cur_fl = suffix_forced_lo[i + 1], suffix_forced_lo[i]
        for p in PHASES:
            cur_lo[p] = prev_lo[p] + (lows[i] if p in allowed[i] else 0)
            forced = highs[i] if allowed[i] == (p,) else 0
            cur_fh[p] = prev_fh[p] + forced
            cur_fl[p] = prev_fl[p] + (lows[i] if allowed[i] == (p,) else 0)
        suffix_lo[i] = suffix_lo[i + 1] + lows[i]
        suffix_hi[i] = suffix_hi[i + 1] + highs[i]

    lower = {p: 0 for p in PHASES}
    upper = {p: 0 for p in PHASES}
    current: list[Optional[str]] = [None] * n
    best_imbalance: Optional[int] = None
    best_assignment: Optional[list[str]] = None

    def dfs(i: int, used_lo: int, used_hi: int) -> None:
        nonlocal best_imbalance, best_assignment
        if best_imbalance == 0:
            return  # 已达成理论最优（最坏负载差为 0），无需继续搜索
        if i == n:
            # 最坏负载差 = max_{p≠q}(上界_p - 下界_q)
            imbalance = max(
                upper[p] - lower[q] for p in PHASES for q in PHASES if p != q
            )
            # 仅严格更优才更新：同差值时先枚举到的方案字典序更小
            if best_imbalance is None or imbalance < best_imbalance:
                best_imbalance = imbalance
                best_assignment = current.copy()
            return
        if best_imbalance is not None:
            # 下界剪枝一（相位对）：对任意有序相异相位对 (p, q)，
            #   最终上界_p >= 当前上界_p + 剩余被迫接入 p 的上界和
            #   最终下界_q <= 当前下界_q + 剩余可接入 q 的下界和
            lb = max(
                upper[p] + suffix_forced_hi[i][p] - lower[q] - suffix_addable_lo[i][q]
                for p in PHASES
                for q in PHASES
                if p != q
            )
            # 下界剪枝二（总量平均）：设 p* 为最终上界最大的相位，则
            #   最终上界最大值 >= ceil(上界总和 / 3)
            #   其余两相最终下界的最小值 <= (下界总和 - p* 相下界) / 2
            #     <= (下界总和 - 各相被迫下界的最小值) / 2
            total_lo = used_lo + suffix_lo[i]
            total_hi = used_hi + suffix_hi[i]
            min_forced_lo = min(
                lower[p] + suffix_forced_lo[i][p] for p in PHASES
            )
            lb = max(
                lb,
                ceil(total_hi / 3) - (total_lo - min_forced_lo) // 2,
            )
            if lb >= best_imbalance:
                return
        for phase in allowed[i]:
            # 稳健可行性：任意功率组合下不超容量 ⟺ 负载上界不超容量
            if upper[phase] + highs[i] <= caps[phase]:
                lower[phase] += lows[i]
                upper[phase] += highs[i]
                current[i] = phase
                dfs(i + 1, used_lo + lows[i], used_hi + highs[i])
                lower[phase] -= lows[i]
                upper[phase] -= highs[i]
                current[i] = None

    dfs(0, 0, 0)

    if best_assignment is None:
        return RobustResult(
            feasible=False,
            assignment=None,
            load_lower=None,
            load_upper=None,
            worst_imbalance=None,
        )

    final_lower = {p: 0 for p in PHASES}
    final_upper = {p: 0 for p in PHASES}
    for idx, phase in enumerate(best_assignment):
        final_lower[phase] += lows[idx]
        final_upper[phase] += highs[idx]
    assignment = {ordered[i].code: best_assignment[i] for i in range(n)}
    return RobustResult(
        feasible=True,
        assignment=assignment,
        load_lower=final_lower,
        load_upper=final_upper,
        worst_imbalance=best_imbalance,
    )


@dataclass(frozen=True)
class StartupDevice:
    """启动顺序规划的设备：接入相位固定（沿用原计算记录）。"""

    code: str  # 设备唯一编号
    phase: str  # 固定接入相位（A/B/C）
    rated_power: int  # 额定功率（正整数）
    startup_power: int  # 启动瞬时功率（正整数，不低于额定功率）


@dataclass(frozen=True)
class StartupStepInfo:
    """启动顺序中的一步：某台设备的启动瞬间与其后的稳定状态。"""

    code: str  # 设备编号
    phase: str  # 接入相位
    rated_power: int  # 额定功率
    startup_power: int  # 启动瞬时功率
    transient_loads: dict[str, int]  # 启动瞬间各相负载
    transient_imbalance: int  # 启动瞬间负载差
    steady_loads: dict[str, int]  # 启动完成后各相稳定负载
    steady_imbalance: int  # 稳定状态负载差


@dataclass(frozen=True)
class StartupPlanResult:
    """启动顺序预演结果。feasible 为 False 时其余字段为 None。"""

    feasible: bool
    order: Optional[tuple[str, ...]]  # 启动顺序（设备编号）
    steps: Optional[tuple[StartupStepInfo, ...]]  # 逐步瞬时 / 稳定负载
    peak_imbalance: Optional[int]  # 全过程三相负载差峰值


def solve_startup_plan(
    devices: Iterable[StartupDevice], capacities: dict[str, int]
) -> StartupPlanResult:
    """求解安全启动顺序：全过程负载差峰值最小，同峰值取编号序列字典序最小。

    设备一台接一台启动，接入相位固定。启动瞬间该设备按启动功率计，此前已
    启动的设备按额定功率持续运行；启动完成后该设备亦按额定功率运行。每个
    启动瞬间与每个稳定状态各相负载均不得超容量。无安全顺序时返回
    feasible=False。

    求解结构（详见模块 docstring）：启动顺序与子集 DAG 上 空集→全集 的路径
    一一对应，节点 / 边的负载差与到达路径无关，故最小化峰值等价于 minimax
    路径，按子集递推；再据最优峰值反向求可达全集的子集集合，自空集按设备
    编号字典序贪心选出字典序最小的最优顺序。

    :param devices: 设备集合（编号唯一、相位固定、0 < 额定功率 <= 启动功率）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :return: StartupPlanResult；无安全启动顺序时 feasible=False。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    phase_of = [d.phase for d in ordered]
    rated = [d.rated_power for d in ordered]
    startup = [d.startup_power for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    size = 1 << n
    full = size - 1

    # steady_loads[mask]：已启动集合为 mask 时的稳定各相负载（额定功率部分和）
    steady_loads: list[dict[str, int]] = [
        {p: 0 for p in PHASES} for _ in range(size)
    ]
    steady_imb = [0] * size  # 各稳定状态的负载差
    steady_ok = [True] * size  # 各稳定状态是否满足容量
    for mask in range(1, size):
        lsb = mask & (-mask)
        i = lsb.bit_length() - 1
        prev = steady_loads[mask ^ lsb]
        cur = dict(prev)
        cur[phase_of[i]] += rated[i]
        steady_loads[mask] = cur
        steady_imb[mask] = max(cur.values()) - min(cur.values())
        steady_ok[mask] = all(cur[p] <= caps[p] for p in PHASES)

    def transient(mask: int, i: int) -> tuple[dict[str, int], int]:
        """状态 mask 下启动设备 i 的瞬时各相负载与负载差。"""
        cur = dict(steady_loads[mask])
        cur[phase_of[i]] += startup[i]
        return cur, max(cur.values()) - min(cur.values())

    def edge_ok(mask: int, i: int) -> bool:
        """启动瞬间容量校验：仅该设备所在相负载变化，其余相同稳定状态。"""
        p = phase_of[i]
        return steady_loads[mask][p] + startup[i] <= caps[p]

    # dist[mask]：到达 mask 的全过程负载差峰值的最小值；None 表示不可达。
    # 路径代价为途经节点（稳定状态）与边（启动瞬间）负载差的最大值（minimax）；
    # mask 的前驱均小于 mask，按整数序递推即可。
    dist: list[Optional[int]] = [None] * size
    dist[0] = 0
    for mask in range(1, size):
        if not steady_ok[mask]:
            continue
        best: Optional[int] = None
        m = mask
        while m:
            lsb = m & (-m)
            i = lsb.bit_length() - 1
            m ^= lsb
            prev_mask = mask ^ lsb
            if dist[prev_mask] is None or not edge_ok(prev_mask, i):
                continue
            _, t_imb = transient(prev_mask, i)
            candidate = max(dist[prev_mask], t_imb)
            if best is None or candidate < best:
                best = candidate
        if best is not None:
            dist[mask] = max(best, steady_imb[mask])

    peak = dist[full]
    if peak is None:
        return StartupPlanResult(
            feasible=False, order=None, steps=None, peak_imbalance=None
        )

    # can[mask]：自 mask 出发，途经状态负载差均不超 peak 且满足容量，可达全集
    can = [False] * size
    can[full] = True
    for mask in range(full - 1, -1, -1):
        if not steady_ok[mask]:
            continue
        for i in range(n):
            bit = 1 << i
            if mask & bit:
                continue
            nxt = mask | bit
            if not can[nxt] or not edge_ok(mask, i):
                continue
            _, t_imb = transient(mask, i)
            if t_imb <= peak and steady_imb[nxt] <= peak:
                can[mask] = True
                break

    # 自空集出发按设备编号字典序贪心：每步选取后缀仍可完成的最小编号设备，
    # 所得启动顺序即为同峰值下编号序列字典序最小者
    order_idx: list[int] = []
    mask = 0
    while mask != full:
        for i in range(n):  # 设备已按编号排序，即按编号序列字典序尝试
            bit = 1 << i
            if mask & bit:
                continue
            nxt = mask | bit
            if not can[nxt] or not edge_ok(mask, i):
                continue
            _, t_imb = transient(mask, i)
            if t_imb <= peak and steady_imb[nxt] <= peak:
                order_idx.append(i)
                mask = nxt
                break
        else:  # can[0] 为真时必然每步可选，不会走到这里
            raise AssertionError("贪心构造启动顺序失败：无可行后继设备")

    steps = []
    mask = 0
    for i in order_idx:
        t_loads, t_imb = transient(mask, i)
        mask |= 1 << i
        steps.append(
            StartupStepInfo(
                code=ordered[i].code,
                phase=phase_of[i],
                rated_power=rated[i],
                startup_power=startup[i],
                transient_loads=t_loads,
                transient_imbalance=t_imb,
                steady_loads=dict(steady_loads[mask]),
                steady_imbalance=steady_imb[mask],
            )
        )
    return StartupPlanResult(
        feasible=True,
        order=tuple(ordered[i].code for i in order_idx),
        steps=tuple(steps),
        peak_imbalance=peak,
    )


# ---------------------------------------------------------------------------
# 单相故障保供预演
# ---------------------------------------------------------------------------

# 相位选择的字典序：A＜B＜C＜断开（断开用 "D" 内部标记）
_DISCONNECT: str = "D"


@dataclass(frozen=True)
class FaultRehearsalDevice:
    """单相故障预演输入设备（沿用可行计算记录的功率、允许相位与原分配）。"""

    code: str  # 设备唯一编号
    power: int  # 正整数功率
    allowed: tuple[str, ...]  # 可接入相位集合（PHASES 的非空子集）
    original_phase: str  # 记录中的原接入相位（A/B/C）


@dataclass(frozen=True)
class FaultRehearsalMigration:
    """一台故障相设备的迁移结果。"""

    code: str  # 设备编号
    power: int  # 设备功率
    from_phase: str  # 原接入相位（即故障相）
    to_phase: str  # 迁入的健康相（A/B/C）


@dataclass(frozen=True)
class FaultRehearsalDisconnect:
    """一台被断开的故障相设备。"""

    code: str  # 设备编号
    power: int  # 设备功率
    phase: str  # 原接入相位（即故障相）


@dataclass(frozen=True)
class FaultRehearsalResult:
    """单相故障保供预演结果（断开总是可行，故无 feasible=False 情形）。"""

    fault_phase: str  # 故障相
    capacities: dict[str, int]  # 各相容量（沿用记录）
    original_loads: dict[str, int]  # 故障前各相负载（记录中的分配负载）
    loads: dict[str, int]  # 预演后各相负载（故障相必为零）
    migrations: tuple[FaultRehearsalMigration, ...]  # 迁移的故障相设备（按编号排序）
    disconnected: tuple[FaultRehearsalDisconnect, ...]  # 断开的故障相设备（按编号排序）
    retained_power: int  # 保留设备的总功率（含健康相不动设备）
    retained_count: int  # 保留设备数（含健康相不动设备）
    disconnected_power: int  # 断开设备的总功率
    disconnected_count: int  # 断开设备数
    choice_sequence: tuple[str, ...]  # 按故障相设备编号排序的相位选择序列


def solve_phase_fault_rehearsal(
    devices: Iterable[FaultRehearsalDevice],
    capacities: dict[str, int],
    fault_phase: str,
) -> FaultRehearsalResult:
    """求解单相故障保供预演。

    健康相设备保持原分配不动；故障相设备依次选择「允许接入的健康相（A→B→C）
    或断开」。迁至健康相时该相负载不得超过容量。目标依次为：保留设备总功率
    最大 → 保留设备数最多 → 按故障相设备编号排序的相位选择序列字典序最小
    （A＜B＜C＜断开）。故障相最终负载恒为零；全部断开恒可行。

    :param devices: 设备集合（编号唯一、功率正整数、允许相位非空且含原相位）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :param fault_phase: 故障相（"A"/"B"/"C"）。
    :return: FaultRehearsalResult。
    """
    ordered_all = sorted(devices, key=lambda d: d.code)
    healthy_devices = [d for d in ordered_all if d.original_phase != fault_phase]
    fault_devices = [d for d in ordered_all if d.original_phase == fault_phase]
    healthy_phases = [p for p in PHASES if p != fault_phase]
    caps = {p: capacities[p] for p in PHASES}

    # 故障前各相负载：全部设备按记录中的原相位累加
    original_loads = {p: 0 for p in PHASES}
    for device in ordered_all:
        original_loads[device.original_phase] += device.power

    # 健康相设备保持原分配：构成健康相的初始（不可迁出）负载
    initial_loads = {p: 0 for p in PHASES}
    retained_power_fixed = 0
    for device in healthy_devices:
        initial_loads[device.original_phase] += device.power
        retained_power_fixed += device.power
    retained_count_fixed = len(healthy_devices)

    m = len(fault_devices)
    powers = [d.power for d in fault_devices]
    # 每台故障相设备的可选动作：允许的健康相（按 A/B/C 顺序），末尾为断开
    options: list[list[str]] = []
    for device in fault_devices:
        moves = [p for p in healthy_phases if p in device.allowed]
        options.append(moves + [_DISCONNECT])

    # suffix_power[i] / suffix_count：第 i..m-1 台故障设备的功率和 / 数量（剪枝用）
    suffix_power = [0] * (m + 1)
    for i in range(m - 1, -1, -1):
        suffix_power[i] = suffix_power[i + 1] + powers[i]

    loads = dict(initial_loads)
    current: list[Optional[str]] = [None] * m
    # 最优值（仅统计故障相设备中被保留的部分）：总功率优先，设备数次之
    best_power: Optional[int] = None
    best_count: Optional[int] = None
    best_choices: Optional[list[str]] = None

    def dfs(i: int, kept_power: int, kept_count: int) -> None:
        nonlocal best_power, best_count, best_choices
        if i == m:
            # 仅 (保留功率, 保留设备数) 严格更优才更新；同值时先枚举到的
            # 方案选择序列字典序更小（选项按 A→B→C→断开 顺序展开）
            if (
                best_power is None
                or (kept_power, kept_count) > (best_power, best_count)
            ):
                best_power = kept_power
                best_count = kept_count
                best_choices = current.copy()
            return
        # 乐观上界剪枝：即使剩余故障设备全部保留
        if best_power is not None:
            upper_power = kept_power + suffix_power[i]
            if upper_power < best_power:
                return
            if upper_power == best_power and kept_count + (m - i) < (best_count or 0):
                return
        for choice in options[i]:
            if choice == _DISCONNECT:
                current[i] = choice
                dfs(i + 1, kept_power, kept_count)
                current[i] = None
            elif loads[choice] + powers[i] <= caps[choice]:
                loads[choice] += powers[i]
                current[i] = choice
                dfs(i + 1, kept_power + powers[i], kept_count + 1)
                loads[choice] -= powers[i]
                current[i] = None

    dfs(0, 0, 0)

    assert best_choices is not None  # 断开总是可行
    final_loads = dict(initial_loads)
    migrations: list[FaultRehearsalMigration] = []
    disconnected: list[FaultRehearsalDisconnect] = []
    for idx, choice in enumerate(best_choices):
        device = fault_devices[idx]
        if choice == _DISCONNECT:
            disconnected.append(
                FaultRehearsalDisconnect(
                    code=device.code, power=device.power, phase=fault_phase
                )
            )
        else:
            final_loads[choice] += powers[idx]
            migrations.append(
                FaultRehearsalMigration(
                    code=device.code,
                    power=device.power,
                    from_phase=fault_phase,
                    to_phase=choice,
                )
            )
    final_loads[fault_phase] = 0

    migrations.sort(key=lambda item: item.code)
    disconnected.sort(key=lambda item: item.code)
    return FaultRehearsalResult(
        fault_phase=fault_phase,
        capacities=dict(caps),
        original_loads=original_loads,
        loads=final_loads,
        migrations=tuple(migrations),
        disconnected=tuple(disconnected),
        retained_power=retained_power_fixed + (best_power or 0),
        retained_count=retained_count_fixed + (best_count or 0),
        disconnected_power=sum(item.power for item in disconnected),
        disconnected_count=len(disconnected),
        choice_sequence=tuple(best_choices),
    )
