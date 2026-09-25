"""求解器单元测试（仅依赖标准库，可直接运行：python3 -m unittest tests.test_solver）。"""

import itertools
import random
import unittest

from app.solver import (
    Device,
    FaultRehearsalDevice,
    RobustDevice,
    StartupDevice,
    solve,
    solve_capacity_upgrade,
    solve_phase_fault_rehearsal,
    solve_robust,
    solve_startup_plan,
)

PHASES = ("A", "B", "C")


def brute_force(devices, capacities):
    """朴素全枚举参照实现：返回 (最优差值, 字典序最小相位序列) 或 None。"""
    ordered = sorted(devices, key=lambda d: d.code)
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    best = None
    for combo in itertools.product(*allowed):
        loads = {p: 0 for p in PHASES}
        ok = True
        for device, phase in zip(ordered, combo):
            loads[phase] += device.power
            if loads[phase] > capacities[phase]:
                ok = False
                break
        if not ok:
            continue
        imbalance = max(loads.values()) - min(loads.values())
        if best is None or imbalance < best[0]:
            best = (imbalance, combo, dict(loads))
    return best


class SolveBasicTest(unittest.TestCase):
    def test_balanced_assignment(self):
        devices = [
            Device("D1", 10, ("A", "B", "C")),
            Device("D2", 20, ("A", "B", "C")),
            Device("D3", 30, ("A", "B", "C")),
        ]
        result = solve(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        # 10/20/30 无法完全均分（无子集和为 20 的拆分），最优差值为 20
        self.assertEqual(result.imbalance, 20)
        self.assertEqual(result.loads, {"A": 10, "B": 20, "C": 30})
        # 字典序最小：D1->A, D2->B, D3->C
        self.assertEqual(result.assignment, {"D1": "A", "D2": "B", "D3": "C"})

    def test_perfect_balance(self):
        devices = [Device(f"D{i}", 10, PHASES) for i in range(1, 4)]
        result = solve(devices, {"A": 10, "B": 10, "C": 10})
        self.assertTrue(result.feasible)
        self.assertEqual(result.imbalance, 0)
        self.assertEqual(result.loads, {"A": 10, "B": 10, "C": 10})

    def test_restricted_phases(self):
        devices = [
            Device("X", 5, ("A",)),
            Device("Y", 7, ("B", "C")),
            Device("Z", 6, ("A", "C")),
        ]
        result = solve(devices, {"A": 20, "B": 20, "C": 20})
        self.assertTrue(result.feasible)
        self.assertEqual(result.assignment["X"], "A")
        self.assertEqual(result.loads["A"] + result.loads["B"] + result.loads["C"], 18)

    def test_infeasible_capacity(self):
        devices = [Device("D1", 50, ("A",)), Device("D2", 60, ("A",))]
        result = solve(devices, {"A": 100, "B": 0, "C": 0})
        self.assertFalse(result.feasible)
        self.assertIsNone(result.assignment)
        self.assertIsNone(result.loads)
        self.assertIsNone(result.imbalance)

    def test_infeasible_single_device_over_capacity(self):
        devices = [Device("BIG", 101, PHASES)]
        result = solve(devices, {"A": 100, "B": 100, "C": 100})
        self.assertFalse(result.feasible)

    def test_lexicographic_tie_break(self):
        # 两台同功率设备：多种最优解，应取编号排序后相位序列字典序最小者
        devices = [Device("B2", 10, PHASES), Device("A1", 10, PHASES)]
        result = solve(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        self.assertEqual(result.imbalance, 10)
        # 按编号排序为 A1, B2；字典序最小序列为 (A, B)
        self.assertEqual(result.assignment, {"A1": "A", "B2": "B"})

    def test_device_ids_sorted_as_strings(self):
        devices = [
            Device("DEV-10", 4, PHASES),
            Device("DEV-2", 4, PHASES),
            Device("DEV-1", 4, PHASES),
        ]
        result = solve(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        self.assertEqual(result.imbalance, 0)
        # 字符串排序 DEV-1 < DEV-10 < DEV-2，字典序最小序列 (A, B, C)
        self.assertEqual(
            result.assignment, {"DEV-1": "A", "DEV-10": "B", "DEV-2": "C"}
        )

    def test_capacity_forces_imbalance(self):
        devices = [Device("D1", 8, PHASES), Device("D2", 8, PHASES)]
        result = solve(devices, {"A": 8, "B": 8, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.loads, {"A": 8, "B": 8, "C": 0})
        self.assertEqual(result.imbalance, 8)


class SolveFuzzTest(unittest.TestCase):
    """与朴素全枚举参照实现做随机对拍。"""

    def test_against_brute_force(self):
        rng = random.Random(20260924)
        for case in range(300):
            n = rng.randint(1, 7)
            devices = []
            for i in range(n):
                allowed = tuple(p for p in PHASES if rng.random() < 0.7) or ("A",)
                devices.append(Device(f"DEV-{i}", rng.randint(1, 20), allowed))
            capacities = {p: rng.randint(0, 60) for p in PHASES}

            expected = brute_force(devices, capacities)
            result = solve(devices, capacities)

            if expected is None:
                self.assertFalse(result.feasible, f"case {case}: 应为无可行解")
                continue

            self.assertTrue(result.feasible, f"case {case}: 应有可行解")
            exp_imbalance, exp_combo, exp_loads = expected
            self.assertEqual(result.imbalance, exp_imbalance, f"case {case}: 差值不符")
            self.assertEqual(result.loads, exp_loads, f"case {case}: 负载不符")
            ordered = sorted(devices, key=lambda d: d.code)
            actual_combo = tuple(result.assignment[d.code] for d in ordered)
            self.assertEqual(actual_combo, exp_combo, f"case {case}: 字典序最小方案不符")

    def test_twelve_devices_performance(self):
        # 12 台设备、全相位可接、大容量：最恶劣情形，应在可接受时间内完成
        devices = [Device(f"D{i:02d}", 2**i, PHASES) for i in range(12)]
        result = solve(devices, {"A": 10**6, "B": 10**6, "C": 10**6})
        self.assertTrue(result.feasible)
        expected = brute_force(devices, {"A": 10**6, "B": 10**6, "C": 10**6})
        self.assertEqual(result.imbalance, expected[0])
        ordered = sorted(devices, key=lambda d: d.code)
        self.assertEqual(
            tuple(result.assignment[d.code] for d in ordered), expected[1]
        )


def brute_force_upgrade(devices, capacities):
    """扩容问题的朴素全枚举：返回 (总扩容, 单相最大扩容, 相位序列, 负载, 扩容量)。"""
    ordered = sorted(devices, key=lambda d: d.code)
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    best = None
    for combo in itertools.product(*allowed):
        loads = {p: 0 for p in PHASES}
        for device, phase in zip(ordered, combo):
            loads[phase] += device.power
        upgrades = {p: max(0, loads[p] - capacities[p]) for p in PHASES}
        key = (
            sum(upgrades.values()),
            max(upgrades.values()),
            combo,
        )
        if best is None or key < best[0]:
            best = (key, dict(loads), dict(upgrades))
    return best


class CapacityUpgradeTest(unittest.TestCase):
    def test_single_oversized_device(self):
        devices = [Device("D1", 50, ("A", "B", "C"))]
        result = solve_capacity_upgrade(devices, {"A": 5, "B": 0, "C": 0})
        # 总量 45 可全部加在 A 相；字典序最小为 A
        self.assertEqual(result.upgrades, {"A": 45, "B": 0, "C": 0})
        self.assertEqual(result.suggested_capacities, {"A": 50, "B": 0, "C": 0})
        self.assertEqual(result.assignment, {"D1": "A"})
        self.assertEqual(result.loads, {"A": 50, "B": 0, "C": 0})
        self.assertEqual(result.total_upgrade, 45)
        self.assertEqual(result.max_upgrade, 45)

    def test_forced_phase_requires_upgrade(self):
        devices = [Device("X", 50, ("A",)), Device("Y", 60, ("A",))]
        result = solve_capacity_upgrade(devices, {"A": 100, "B": 0, "C": 0})
        # 两台设备都只能接 A，A 相必须扩至 110（总量、单相最大均为 10）
        self.assertEqual(result.upgrades, {"A": 10, "B": 0, "C": 0})
        self.assertEqual(result.loads, {"A": 110, "B": 0, "C": 0})
        self.assertEqual(result.total_upgrade, 10)
        self.assertEqual(result.max_upgrade, 10)

    def test_minimize_total_then_max_single_phase(self):
        # 总量 20 最优：可拆成 (20,0,0) 或 (10,10,0) 等，应取单相最大最小的
        # (10,10,0)；同 max 下相位序列字典序最小，即两台设备都接 A（(A,A)
        # 字典序小于 (A,B)），扩容量 (20,0,0) 虽使两台同接 A，但 max=20
        # 并非最优 —— 故最优 max=10 由 (A,B) 实现。
        devices = [
            Device("D1", 10, ("A", "B", "C")),
            Device("D2", 10, ("A", "B", "C")),
        ]
        result = solve_capacity_upgrade(devices, {"A": 0, "B": 0, "C": 0})
        self.assertEqual(result.total_upgrade, 20)
        self.assertEqual(result.max_upgrade, 10)
        # max=10 的方案要求两设备落在不同相位；序列字典序最小为 (A,B)
        self.assertEqual(result.upgrades, {"A": 10, "B": 10, "C": 0})
        self.assertEqual(result.assignment, {"D1": "A", "D2": "B"})
        self.assertEqual(result.loads, {"A": 10, "B": 10, "C": 0})

    def test_lexicographic_tie_break(self):
        # 两台同功率全相位设备，原容量充裕时无需扩容，max 同为 0，
        # 此时没有均衡约束，取纯字典序最小相位序列 (A,A)
        devices = [Device("B2", 10, PHASES), Device("A1", 10, PHASES)]
        result = solve_capacity_upgrade(devices, {"A": 100, "B": 100, "C": 100})
        self.assertEqual(result.upgrades, {"A": 0, "B": 0, "C": 0})
        self.assertEqual(result.total_upgrade, 0)
        self.assertEqual(result.max_upgrade, 0)
        self.assertEqual(result.assignment, {"A1": "A", "B2": "A"})

    def test_restricted_phases_shape_upgrade(self):
        devices = [
            Device("X", 30, ("A",)),
            Device("Y", 30, ("B",)),
            Device("Z", 30, ("C",)),
        ]
        result = solve_capacity_upgrade(devices, {"A": 10, "B": 10, "C": 10})
        # 每相被迫扩 20：总量 60，单相最大 20
        self.assertEqual(result.upgrades, {"A": 20, "B": 20, "C": 20})
        self.assertEqual(result.total_upgrade, 60)
        self.assertEqual(result.max_upgrade, 20)
        self.assertEqual(result.assignment, {"X": "A", "Y": "B", "Z": "C"})

    def test_existing_capacity_reused(self):
        # 总量最小为 8：两台都接 A（扩 8，序列 (A,A)）与分接 A/B（扩 B 8，
        # 序列 (A,B)）max 相同，字典序取 (A,A)
        devices = [
            Device("D1", 8, PHASES),
            Device("D2", 8, PHASES),
        ]
        result = solve_capacity_upgrade(devices, {"A": 8, "B": 0, "C": 0})
        self.assertEqual(result.total_upgrade, 8)
        self.assertEqual(result.max_upgrade, 8)
        self.assertEqual(result.upgrades, {"A": 8, "B": 0, "C": 0})
        self.assertEqual(result.assignment, {"D1": "A", "D2": "A"})
        self.assertEqual(result.loads, {"A": 16, "B": 0, "C": 0})


class CapacityUpgradeFuzzTest(unittest.TestCase):
    """扩容求解与朴素全枚举做随机对拍。"""

    def test_against_brute_force(self):
        rng = random.Random(20260925)
        for case in range(400):
            n = rng.randint(1, 7)
            devices = []
            for i in range(n):
                allowed = tuple(p for p in PHASES if rng.random() < 0.7) or ("A",)
                devices.append(Device(f"DEV-{i}", rng.randint(1, 20), allowed))
            capacities = {p: rng.randint(0, 40) for p in PHASES}

            expected = brute_force_upgrade(devices, capacities)
            result = solve_capacity_upgrade(devices, capacities)

            (exp_total, exp_max, exp_combo), exp_loads, exp_upgrades = expected
            self.assertEqual(
                result.total_upgrade, exp_total, f"case {case}: 扩容总量不符"
            )
            self.assertEqual(
                result.max_upgrade, exp_max, f"case {case}: 单相最大扩容不符"
            )
            self.assertEqual(result.upgrades, exp_upgrades, f"case {case}: 扩容量不符")
            self.assertEqual(result.loads, exp_loads, f"case {case}: 负载不符")
            ordered = sorted(devices, key=lambda d: d.code)
            actual_combo = tuple(result.assignment[d.code] for d in ordered)
            self.assertEqual(actual_combo, exp_combo, f"case {case}: 方案不符")
            # 一致性校验：扩容量与负载、建议容量自洽
            for p in PHASES:
                self.assertEqual(
                    result.suggested_capacities[p],
                    result.original_capacities[p] + result.upgrades[p],
                )
                self.assertGreaterEqual(
                    result.suggested_capacities[p], result.loads[p]
                )

    def test_twelve_devices_performance(self):
        # 12 台设备、全相位可接、原容量为 0：方案空间最大，应快速完成
        devices = [Device(f"D{i:02d}", 2**i, PHASES) for i in range(12)]
        result = solve_capacity_upgrade(devices, {"A": 0, "B": 0, "C": 0})
        expected = brute_force_upgrade(devices, {"A": 0, "B": 0, "C": 0})
        (exp_total, exp_max, exp_combo), _, _ = expected
        self.assertEqual(result.total_upgrade, exp_total)
        self.assertEqual(result.max_upgrade, exp_max)
        ordered = sorted(devices, key=lambda d: d.code)
        self.assertEqual(
            tuple(result.assignment[d.code] for d in ordered), exp_combo
        )

    def test_twelve_devices_feasible_performance(self):
        # 12 台设备在原容量下本就可行（扩容为 0）：第二遍仍须在全部可行方案
        # 中选出字典序最小者，枚举规模最大
        devices = [
            Device(f"D{i:02d}", 2 * i + 1, PHASES) for i in range(12)
        ]
        capacities = {"A": 1000, "B": 1000, "C": 1000}
        result = solve_capacity_upgrade(devices, capacities)
        expected = brute_force_upgrade(devices, capacities)
        (exp_total, exp_max, exp_combo), exp_loads, _ = expected
        self.assertEqual(result.total_upgrade, exp_total)
        self.assertEqual(result.max_upgrade, exp_max)
        self.assertEqual(result.loads, exp_loads)
        ordered = sorted(devices, key=lambda d: d.code)
        self.assertEqual(
            tuple(result.assignment[d.code] for d in ordered), exp_combo
        )


def brute_force_robust(devices, capacities):
    """稳健预演的朴素全枚举参照实现（内层枚举所有功率组合，仅适合小规模）。

    返回 (最坏负载差, 字典序最小相位序列, 各相负载下界, 各相负载上界) 或 None。
    同时验证闭式归约：最坏负载差 = max_{p≠q}(上界_p - 下界_q)。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    best = None
    for combo in itertools.product(*allowed):
        lower = {p: 0 for p in PHASES}
        upper = {p: 0 for p in PHASES}
        for device, phase in zip(ordered, combo):
            lower[phase] += device.min_power
            upper[phase] += device.max_power
        # 内层枚举所有功率组合，直接计算最坏负载差
        ranges = [range(d.min_power, d.max_power + 1) for d in ordered]
        worst = None
        for powers in itertools.product(*ranges):
            loads = {p: 0 for p in PHASES}
            for power, phase in zip(powers, combo):
                loads[phase] += power
            if any(loads[p] > capacities[p] for p in PHASES):
                worst = None  # 存在超容量的功率组合：非稳健可行
                break
            imbalance = max(loads.values()) - min(loads.values())
            if worst is None or imbalance > worst:
                worst = imbalance
        if worst is None:
            continue
        # 闭式归约正确性：最坏负载差 = max_{p≠q}(上界_p - 下界_q)
        closed_form = max(
            upper[p] - lower[q] for p in PHASES for q in PHASES if p != q
        )
        assert worst == closed_form, (worst, closed_form, lower, upper)
        if best is None or worst < best[0]:
            best = (worst, combo, dict(lower), dict(upper))
    return best


def brute_force_robust_closed_form(devices, capacities):
    """仅枚举相位序列、用闭式目标 max_{p≠q}(上界_p - 下界_q) 的参照实现。"""
    ordered = sorted(devices, key=lambda d: d.code)
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    best = None
    for combo in itertools.product(*allowed):
        lower = {p: 0 for p in PHASES}
        upper = {p: 0 for p in PHASES}
        for device, phase in zip(ordered, combo):
            lower[phase] += device.min_power
            upper[phase] += device.max_power
        # 稳健可行 ⟺ 任意功率组合下各相不超容量 ⟺ 上界不超容量
        if any(upper[p] > capacities[p] for p in PHASES):
            continue
        worst = max(
            upper[p] - lower[q] for p in PHASES for q in PHASES if p != q
        )
        if best is None or worst < best[0]:
            best = (worst, combo, dict(lower), dict(upper))
    return best


class RobustSolveTest(unittest.TestCase):
    def test_degenerates_to_fixed_power(self):
        # min == max 时应与普通求解结果一致
        devices = [
            RobustDevice("D1", 10, 10, PHASES),
            RobustDevice("D2", 20, 20, PHASES),
            RobustDevice("D3", 30, 30, PHASES),
        ]
        result = solve_robust(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        self.assertEqual(result.worst_imbalance, 20)
        self.assertEqual(result.load_lower, {"A": 10, "B": 20, "C": 30})
        self.assertEqual(result.load_upper, {"A": 10, "B": 20, "C": 30})
        self.assertEqual(result.assignment, {"D1": "A", "D2": "B", "D3": "C"})

    def test_capacity_checked_on_upper_bound(self):
        # D1 上界 10 超过 A 相容量 8，只能接 B；下界 3 不超容量不影响结论
        devices = [RobustDevice("D1", 3, 10, ("A", "B"))]
        result = solve_robust(devices, {"A": 8, "B": 10, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.assignment, {"D1": "B"})
        self.assertEqual(result.load_upper, {"A": 0, "B": 10, "C": 0})
        self.assertEqual(result.load_lower, {"A": 0, "B": 3, "C": 0})
        self.assertEqual(result.worst_imbalance, 10)

    def test_infeasible_when_upper_exceeds_every_phase(self):
        devices = [RobustDevice("BIG", 1, 50, PHASES)]
        result = solve_robust(devices, {"A": 40, "B": 40, "C": 40})
        self.assertFalse(result.feasible)
        self.assertIsNone(result.assignment)
        self.assertIsNone(result.load_lower)
        self.assertIsNone(result.load_upper)
        self.assertIsNone(result.worst_imbalance)

    def test_worst_imbalance_uses_upper_minus_lower(self):
        # 单台设备：最坏负载差 = 上界 - 0（其余两相下界为 0）
        devices = [RobustDevice("D1", 4, 9, ("A",))]
        result = solve_robust(devices, {"A": 100, "B": 0, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.worst_imbalance, 9)
        self.assertEqual(result.load_lower, {"A": 4, "B": 0, "C": 0})
        self.assertEqual(result.load_upper, {"A": 9, "B": 0, "C": 0})

    def test_worst_imbalance_excludes_same_phase_pair(self):
        # 上界最大值与下界最小值同在 A 相：同一相不可能同时取到上界与下界，
        # 最坏负载差应为 max_{p≠q}(上界_p - 下界_q) = max(100-50, 60-1, ...) = 59
        devices = [
            RobustDevice("D1", 1, 100, ("A",)),
            RobustDevice("D2", 50, 50, ("B",)),
            RobustDevice("D3", 60, 60, ("C",)),
        ]
        result = solve_robust(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        self.assertEqual(result.load_lower, {"A": 1, "B": 50, "C": 60})
        self.assertEqual(result.load_upper, {"A": 100, "B": 50, "C": 60})
        self.assertEqual(result.worst_imbalance, 59)

    def test_uncertainty_drives_assignment(self):
        # 两台设备功率区间宽度不同：固定功率下 (A,B) 与 (B,A) 等效，
        # 但区间下界不为 0 时，最坏负载差因分配而异
        devices = [
            RobustDevice("D1", 1, 10, PHASES),
            RobustDevice("D2", 9, 10, PHASES),
        ]
        result = solve_robust(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        # (D1->A, D2->B)：上界 (10,10,0)，下界 (1,9,0)，最坏差 = 10 - 0 = 10
        # 任何同相分配上界和为 20，最坏差更大
        self.assertEqual(result.worst_imbalance, 10)
        self.assertEqual(result.assignment, {"D1": "A", "D2": "B"})

    def test_lexicographic_tie_break(self):
        # 区间完全相同的两台设备：取编号排序后相位序列字典序最小者
        devices = [
            RobustDevice("B2", 2, 10, PHASES),
            RobustDevice("A1", 2, 10, PHASES),
        ]
        result = solve_robust(devices, {"A": 100, "B": 100, "C": 100})
        self.assertTrue(result.feasible)
        self.assertEqual(result.assignment, {"A1": "A", "B2": "B"})

    def test_perfect_robust_balance(self):
        devices = [RobustDevice(f"D{i}", 10, 10, PHASES) for i in range(1, 4)]
        result = solve_robust(devices, {"A": 10, "B": 10, "C": 10})
        self.assertTrue(result.feasible)
        self.assertEqual(result.worst_imbalance, 0)


class RobustSolveFuzzTest(unittest.TestCase):
    """与朴素全枚举参照实现做随机对拍。"""

    def _check_against(self, brute, rng, cases, max_devices, max_width):
        for case in range(cases):
            n = rng.randint(1, max_devices)
            devices = []
            for i in range(n):
                allowed = tuple(p for p in PHASES if rng.random() < 0.7) or ("A",)
                lo = rng.randint(1, 12)
                hi = lo + rng.randint(0, max_width)
                devices.append(RobustDevice(f"DEV-{i}", lo, hi, allowed))
            capacities = {p: rng.randint(0, 60) for p in PHASES}

            expected = brute(devices, capacities)
            result = solve_robust(devices, capacities)

            if expected is None:
                self.assertFalse(result.feasible, f"case {case}: 应为无稳健可行解")
                continue

            self.assertTrue(result.feasible, f"case {case}: 应有稳健可行解")
            exp_worst, exp_combo, exp_lower, exp_upper = expected
            self.assertEqual(
                result.worst_imbalance, exp_worst, f"case {case}: 最坏负载差不符"
            )
            self.assertEqual(result.load_lower, exp_lower, f"case {case}: 负载下界不符")
            self.assertEqual(result.load_upper, exp_upper, f"case {case}: 负载上界不符")
            ordered = sorted(devices, key=lambda d: d.code)
            actual_combo = tuple(result.assignment[d.code] for d in ordered)
            self.assertEqual(
                actual_combo, exp_combo, f"case {case}: 字典序最小方案不符"
            )

    def test_against_power_enumeration(self):
        # 小规模：参照实现内层枚举所有功率组合，同时验证闭式归约
        self._check_against(
            brute_force_robust, random.Random(20260926), cases=120, max_devices=4,
            max_width=3,
        )

    def test_against_closed_form_brute_force(self):
        # 较大规模：参照实现枚举相位序列、使用闭式目标
        self._check_against(
            brute_force_robust_closed_form,
            random.Random(20260927),
            cases=300,
            max_devices=7,
            max_width=8,
        )

    def test_twelve_devices_performance(self):
        # 12 台设备、全相位可接、大容量：最恶劣情形，应在可接受时间内完成
        devices = [
            RobustDevice(f"D{i:02d}", max(1, 2**i - 3), 2**i, PHASES)
            for i in range(12)
        ]
        capacities = {"A": 10**6, "B": 10**6, "C": 10**6}
        result = solve_robust(devices, capacities)
        self.assertTrue(result.feasible)
        expected = brute_force_robust_closed_form(devices, capacities)
        exp_worst, exp_combo, exp_lower, exp_upper = expected
        self.assertEqual(result.worst_imbalance, exp_worst)
        self.assertEqual(result.load_lower, exp_lower)
        self.assertEqual(result.load_upper, exp_upper)
        ordered = sorted(devices, key=lambda d: d.code)
        self.assertEqual(
            tuple(result.assignment[d.code] for d in ordered), exp_combo
        )


def brute_force_startup(devices, capacities):
    """启动顺序规划的朴素全排列参照实现。

    返回 (全过程负载差峰值, 编号顺序) 或 None（无安全顺序）。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    best = None
    for perm in itertools.permutations(range(len(ordered))):
        loads = {p: 0 for p in PHASES}
        peak = 0
        ok = True
        for idx in perm:
            device = ordered[idx]
            # 启动瞬间：该设备按启动功率计，此前设备按额定功率运行
            t_loads = dict(loads)
            t_loads[device.phase] += device.startup_power
            if any(t_loads[p] > capacities[p] for p in PHASES):
                ok = False
                break
            peak = max(peak, max(t_loads.values()) - min(t_loads.values()))
            # 启动完成：该设备转入额定功率持续运行
            loads[device.phase] += device.rated_power
            if any(loads[p] > capacities[p] for p in PHASES):
                ok = False
                break
            peak = max(peak, max(loads.values()) - min(loads.values()))
        if not ok:
            continue
        seq = tuple(ordered[i].code for i in perm)
        if best is None or (peak, seq) < best:
            best = (peak, seq)
    return best


class StartupPlanTest(unittest.TestCase):
    def test_single_device(self):
        devices = [StartupDevice("D1", "A", 10, 15)]
        result = solve_startup_plan(devices, {"A": 20, "B": 0, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.order, ("D1",))
        self.assertEqual(result.peak_imbalance, 15)
        step = result.steps[0]
        self.assertEqual(step.transient_loads, {"A": 15, "B": 0, "C": 0})
        self.assertEqual(result.steps[0].transient_imbalance, 15)
        self.assertEqual(step.steady_loads, {"A": 10, "B": 0, "C": 0})
        self.assertEqual(step.steady_imbalance, 10)
        self.assertEqual(step.rated_power, 10)
        self.assertEqual(step.startup_power, 15)

    def test_single_device_no_safe_order(self):
        # 唯一设备启动功率 25 超 A 相容量 20（额定 10 可行）：无安全顺序
        devices = [StartupDevice("D1", "A", 10, 25)]
        result = solve_startup_plan(devices, {"A": 20, "B": 0, "C": 0})
        self.assertFalse(result.feasible)
        self.assertIsNone(result.order)
        self.assertIsNone(result.steps)
        self.assertIsNone(result.peak_imbalance)

    def test_order_matters_for_feasibility(self):
        # D1（额定6/启动10）与 D2（额定4/启动4）同接 A，容量 10：
        # 先 D1 后 D2：10(瞬)→6(稳)→10(瞬) 安全；
        # 先 D2 后 D1：4(稳)→4+10=14(瞬) 超容量，故安全顺序唯一
        devices = [StartupDevice("D1", "A", 6, 10), StartupDevice("D2", "A", 4, 4)]
        result = solve_startup_plan(devices, {"A": 10, "B": 0, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.order, ("D1", "D2"))
        self.assertEqual(result.peak_imbalance, 10)
        self.assertEqual(
            result.steps[0].transient_loads, {"A": 10, "B": 0, "C": 0}
        )
        self.assertEqual(result.steps[0].steady_loads, {"A": 6, "B": 0, "C": 0})
        self.assertEqual(
            result.steps[1].transient_loads, {"A": 10, "B": 0, "C": 0}
        )
        self.assertEqual(result.steps[1].steady_loads, {"A": 10, "B": 0, "C": 0})

    def test_order_matters_for_peak(self):
        # 三相各一台：D3 启动功率最大，后启动可由已运行的其他相拉小负载差
        devices = [
            StartupDevice("D1", "A", 6, 8),
            StartupDevice("D2", "B", 4, 6),
            StartupDevice("D3", "C", 5, 9),
        ]
        caps = {"A": 10, "B": 10, "C": 10}
        result = solve_startup_plan(devices, caps)
        self.assertTrue(result.feasible)
        # (D1,D2,D3) 与 (D2,D1,D3) 峰值均为 8，取编号序列字典序最小者
        self.assertEqual(result.order, ("D1", "D2", "D3"))
        self.assertEqual(result.peak_imbalance, 8)

    def test_lexicographic_tie_break(self):
        # 两台完全相同的设备：两种顺序的负载过程完全一致，取字典序最小编号序列
        devices = [
            StartupDevice("B2", "A", 5, 8),
            StartupDevice("A1", "A", 5, 8),
        ]
        result = solve_startup_plan(devices, {"A": 100, "B": 0, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.order, ("A1", "B2"))

    def test_startup_power_may_equal_rated(self):
        # 启动瞬时功率允许恰好等于额定功率
        devices = [StartupDevice("D1", "A", 10, 10)]
        result = solve_startup_plan(devices, {"A": 10, "B": 0, "C": 0})
        self.assertTrue(result.feasible)
        self.assertEqual(result.peak_imbalance, 10)
        self.assertEqual(
            result.steps[0].transient_loads, result.steps[0].steady_loads
        )

    def test_final_steady_state_is_record_loads(self):
        # 末步稳定负载与全部设备额定运行的固定分配一致
        devices = [
            StartupDevice("D1", "A", 6, 9),
            StartupDevice("D2", "B", 4, 7),
            StartupDevice("D3", "C", 5, 8),
        ]
        result = solve_startup_plan(devices, {"A": 10, "B": 10, "C": 10})
        self.assertTrue(result.feasible)
        self.assertEqual(
            result.steps[-1].steady_loads, {"A": 6, "B": 4, "C": 5}
        )
        self.assertEqual(result.steps[-1].steady_imbalance, 2)
        # 全过程峰值不小于最终稳定状态的负载差
        self.assertGreaterEqual(result.peak_imbalance, 2)


class StartupPlanFuzzTest(unittest.TestCase):
    """与朴素全排列参照实现做随机对拍（含逐步负载仿真校验）。"""

    def _simulate_and_check(self, result, devices, capacities):
        """按返回顺序逐步仿真，校验瞬时/稳定负载、容量与峰值自洽。"""
        by_code = {d.code: d for d in devices}
        loads = {p: 0 for p in PHASES}
        peak = 0
        prev_code = None
        self.assertEqual(tuple(s.code for s in result.steps), result.order)
        for step in result.steps:
            device = by_code[step.code]
            self.assertEqual(step.phase, device.phase)
            self.assertEqual(step.rated_power, device.rated_power)
            self.assertEqual(step.startup_power, device.startup_power)
            t_loads = dict(loads)
            t_loads[device.phase] += device.startup_power
            self.assertEqual(step.transient_loads, t_loads)
            self.assertEqual(
                step.transient_imbalance,
                max(t_loads.values()) - min(t_loads.values()),
            )
            for p in PHASES:
                self.assertLessEqual(t_loads[p], capacities[p])
            loads[device.phase] += device.rated_power
            self.assertEqual(step.steady_loads, loads)
            self.assertEqual(
                step.steady_imbalance,
                max(loads.values()) - min(loads.values()),
            )
            for p in PHASES:
                self.assertLessEqual(loads[p], capacities[p])
            peak = max(peak, step.transient_imbalance, step.steady_imbalance)
        self.assertEqual(peak, result.peak_imbalance)

    def test_against_brute_force(self):
        rng = random.Random(20260928)
        for case in range(300):
            n = rng.randint(1, 7)
            devices = []
            for i in range(n):
                rated = rng.randint(1, 15)
                startup = rated + rng.randint(0, 10)
                devices.append(
                    StartupDevice(
                        f"DEV-{i}", rng.choice(PHASES), rated, startup
                    )
                )
            capacities = {p: rng.randint(0, 60) for p in PHASES}

            expected = brute_force_startup(devices, capacities)
            result = solve_startup_plan(devices, capacities)

            if expected is None:
                self.assertFalse(result.feasible, f"case {case}: 应为无安全启动顺序")
                continue
            exp_peak, exp_seq = expected
            self.assertTrue(result.feasible, f"case {case}: 应有安全启动顺序")
            self.assertEqual(
                result.peak_imbalance, exp_peak, f"case {case}: 峰值不符"
            )
            self.assertEqual(
                result.order, exp_seq, f"case {case}: 字典序最小顺序不符"
            )
            self._simulate_and_check(result, devices, capacities)

    def test_eight_devices_brute_force(self):
        # 8! = 40320：全排列参照仍可承受，覆盖更大状态空间
        rng = random.Random(20260929)
        devices = []
        for i in range(8):
            rated = rng.randint(1, 12)
            devices.append(
                StartupDevice(f"D{i}", PHASES[i % 3], rated, rated + i)
            )
        capacities = {"A": 80, "B": 80, "C": 80}
        result = solve_startup_plan(devices, capacities)
        expected = brute_force_startup(devices, capacities)
        self.assertEqual(result.peak_imbalance, expected[0])
        self.assertEqual(result.order, expected[1])
        self._simulate_and_check(result, devices, capacities)

    def test_twelve_devices_performance(self):
        # 12 台设备：子集 DP 状态 2^12=4096，应瞬时完成并自洽
        devices = [
            StartupDevice(
                f"D{i:02d}", PHASES[i % 3], 2 * i + 1, 2 * i + 1 + (i % 4)
            )
            for i in range(12)
        ]
        capacities = {"A": 10**6, "B": 10**6, "C": 10**6}
        result = solve_startup_plan(devices, capacities)
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.order), 12)
        self.assertEqual(set(result.order), {d.code for d in devices})
        self._simulate_and_check(result, devices, capacities)

    def test_twelve_devices_tight_capacity(self):
        # 紧容量：12 台同相设备（额定均 1；D00 启动 3，其余启动 2），
        # 额定总和 12。验证可行性剪枝、无解判定与顺序约束。
        devices = [
            StartupDevice(f"D{i:02d}", "A", 1, 3 if i == 0 else 2)
            for i in range(12)
        ]
        # 容量 12：末台瞬时 11+2=13（或 D00 末台 11+3=14）均超容量，无解
        result = solve_startup_plan(devices, {"A": 12, "B": 0, "C": 0})
        self.assertFalse(result.feasible)

        # 容量 13：非 D00 设备殿后瞬时恰为 13 可行，D00 殿后为 14 不可行，
        # 故安全顺序中 D00 不得殿后；最优峰值必为 13（末台瞬时），
        # 同峰值字典序最小顺序为 D00 最先、其余按编号排列
        caps = {"A": 13, "B": 0, "C": 0}
        result = solve_startup_plan(devices, caps)
        self.assertTrue(result.feasible)
        self.assertEqual(
            result.order, tuple(f"D{i:02d}" for i in range(12))
        )
        self.assertEqual(result.peak_imbalance, 13)
        self._simulate_and_check(result, devices, caps)


def brute_force_fault(devices, capacities, fault_phase):
    """单相故障保供预演的朴素全枚举参照实现。

    健康相设备保持原分配；故障相设备可选「允许的健康相（按 A/B/C 顺序）」
    或断开（标记 "D"）。按 (保留功率, 保留数量, 选择序列) 取最优——
    保留功率/数量越大越好，同值时序列字典序越小越好（"A"<"B"<"C"<"D"）。
    返回 (选择序列, 最终负载, 迁移字典, 断开列表, 保留功率, 保留数量)。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    fault = [d for d in ordered if d.original_phase == fault_phase]
    healthy = [d for d in ordered if d.original_phase != fault_phase]
    healthy_phases = [p for p in PHASES if p != fault_phase]

    initial = {p: 0 for p in PHASES}
    fixed_power = 0
    for d in healthy:
        initial[d.original_phase] += d.power
        fixed_power += d.power

    options = []
    for d in fault:
        moves = [p for p in healthy_phases if p in d.allowed]
        options.append(moves + ["D"])

    best = None
    for combo in itertools.product(*options):
        loads = dict(initial)
        ok = True
        kept_power = 0
        for d, choice in zip(fault, combo):
            if choice != "D":
                loads[choice] += d.power
                if loads[choice] > capacities[choice]:
                    ok = False
                    break
                kept_power += d.power
        if not ok:
            continue
        kept_count = len(healthy) + sum(1 for c in combo if c != "D")
        key = (-kept_power - fixed_power, -kept_count, combo)
        if best is None or key < best[0]:
            migrations = {
                d.code: choice
                for d, choice in zip(fault, combo)
                if choice != "D"
            }
            disconnected = [d.code for d, choice in zip(fault, combo) if choice == "D"]
            loads[fault_phase] = 0
            best = (
                key,
                combo,
                dict(loads),
                migrations,
                disconnected,
                kept_power + fixed_power,
                kept_count,
            )
    return best


class PhaseFaultRehearsalTest(unittest.TestCase):
    def test_healthy_devices_keep_assignment_and_migrate(self):
        # 健康相设备不动；两台 A 相设备迁至 B/C（B 有 5 空闲，C 有 10）
        devices = [
            FaultRehearsalDevice("H1", 5, ("B",), "B"),
            FaultRehearsalDevice("M1", 10, ("A", "B", "C"), "A"),
            FaultRehearsalDevice("M2", 6, ("B", "C"), "A"),
        ]
        caps = {"A": 100, "B": 10, "C": 10}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        self.assertEqual(result.fault_phase, "A")
        self.assertEqual(result.original_loads, {"A": 16, "B": 5, "C": 0})
        # M1(10) 只能迁 C；M2(6) 迁 C 会与 M1 一起占满 C（10+6>10），
        # 迁 B 又超容量（5+6>10），故 M2 断开
        self.assertEqual(result.loads, {"A": 0, "B": 5, "C": 10})
        self.assertEqual(
            [(m.code, m.from_phase, m.to_phase) for m in result.migrations],
            [("M1", "A", "C")],
        )
        self.assertEqual(
            [(d.code, d.power, d.phase) for d in result.disconnected],
            [("M2", 6, "A")],
        )
        self.assertEqual(result.retained_power, 15)
        self.assertEqual(result.retained_count, 2)
        self.assertEqual(result.disconnected_power, 6)
        self.assertEqual(result.disconnected_count, 1)
        self.assertEqual(result.choice_sequence, ("C", "D"))

    def test_all_devices_retained(self):
        # 两台 A 相设备均可迁 B/C 且容量充裕：全部保留；
        # 全保留方案中序列 (B,B) 字典序最小
        devices = [
            FaultRehearsalDevice("M1", 4, ("A", "B", "C"), "A"),
            FaultRehearsalDevice("M2", 4, ("B", "C"), "A"),
        ]
        caps = {"A": 100, "B": 10, "C": 10}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        self.assertEqual(result.loads, {"A": 0, "B": 8, "C": 0})
        self.assertEqual(result.retained_power, 8)
        self.assertEqual(result.retained_count, 2)
        self.assertEqual(result.disconnected, ())
        self.assertEqual(
            [(m.code, m.to_phase) for m in result.migrations],
            [("M1", "B"), ("M2", "B")],
        )
        self.assertEqual(result.choice_sequence, ("B", "B"))

    def test_maximize_power_before_count(self):
        # 只能利用 B 相（其余健康相 C 容量为 0）：留下两台小设备（共 6）或
        # 一台大设备（10），总功率优先 -> 保留大设备，尽管设备数更少
        devices = [
            FaultRehearsalDevice("M1", 3, ("A", "B"), "A"),
            FaultRehearsalDevice("M2", 3, ("A", "B"), "A"),
            FaultRehearsalDevice("M3", 10, ("A", "B"), "A"),
        ]
        caps = {"A": 100, "B": 10, "C": 0}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        self.assertEqual(result.retained_power, 10)
        self.assertEqual(result.retained_count, 1)
        self.assertEqual(result.loads, {"A": 0, "B": 10, "C": 0})
        self.assertEqual([m.code for m in result.migrations], ["M3"])
        self.assertEqual(
            [d.code for d in result.disconnected], ["M1", "M2"]
        )
        # M1、M2 断开，M3 迁 B：序列 (D, D, B)
        self.assertEqual(result.choice_sequence, ("D", "D", "B"))

    def test_maximize_count_on_equal_power(self):
        # 单台 6 与两台 3 只能竞用 B 相 6 容量：总功率相同，取设备数多者
        devices = [
            FaultRehearsalDevice("M1", 3, ("A", "B"), "A"),
            FaultRehearsalDevice("M2", 3, ("A", "B"), "A"),
            FaultRehearsalDevice("M3", 6, ("A", "B"), "A"),
        ]
        caps = {"A": 100, "B": 6, "C": 0}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        self.assertEqual(result.retained_power, 6)
        self.assertEqual(result.retained_count, 2)
        self.assertEqual(result.loads, {"A": 0, "B": 6, "C": 0})
        self.assertEqual(
            [m.code for m in result.migrations], ["M1", "M2"]
        )
        self.assertEqual([d.code for d in result.disconnected], ["M3"])
        self.assertEqual(result.choice_sequence, ("B", "B", "D"))

    def test_lexicographic_tie_break_prefers_healthy_phase(self):
        # 功率与设备数都相同的方案间：同编号设备可迁 B 或 C 时取序号更小的 B
        devices = [
            FaultRehearsalDevice("M1", 4, ("B", "C"), "A"),
            FaultRehearsalDevice("M2", 4, ("B", "C"), "A"),
        ]
        caps = {"A": 100, "B": 10, "C": 10}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        # (B,B) 与 (B,C)/(C,B)/(C,C) 同为全保留，取字典序最小 (B,B)
        self.assertEqual(result.choice_sequence, ("B", "B"))
        self.assertEqual(result.loads, {"A": 0, "B": 8, "C": 0})
        self.assertEqual(
            [(m.code, m.to_phase) for m in result.migrations],
            [("M1", "B"), ("M2", "B")],
        )

    def test_lexicographic_order_disconnect_last(self):
        # 迁移与断开在同功率方案间：A＜B＜C＜断开，能迁移时不优先断开
        devices = [
            FaultRehearsalDevice("M1", 4, ("C",), "A"),
            FaultRehearsalDevice("M2", 4, ("B",), "A"),
        ]
        caps = {"A": 100, "B": 4, "C": 4}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        # 两台均可保留（分别只能迁 C、B）：序列按编号排序为 (C, B)
        self.assertEqual(result.choice_sequence, ("C", "B"))
        self.assertEqual(result.retained_count, 2)
        self.assertEqual(result.loads, {"A": 0, "B": 4, "C": 4})

    def test_fault_only_device_without_healthy_option_disconnected(self):
        # 故障相设备的允许相位只含故障相：只能断开
        devices = [
            FaultRehearsalDevice("F1", 9, ("A",), "A"),
            FaultRehearsalDevice("H1", 3, ("B",), "B"),
        ]
        result = solve_phase_fault_rehearsal(
            devices, {"A": 100, "B": 100, "C": 100}, "A"
        )
        self.assertEqual(result.loads, {"A": 0, "B": 3, "C": 0})
        self.assertEqual(result.migrations, ())
        self.assertEqual([d.code for d in result.disconnected], ["F1"])
        self.assertEqual(result.choice_sequence, ("D",))
        self.assertEqual(result.retained_power, 3)
        self.assertEqual(result.retained_count, 1)

    def test_no_fault_devices(self):
        # 故障相本就无设备：负载不变，迁移 / 断开均为空
        devices = [
            FaultRehearsalDevice("H1", 5, ("B",), "B"),
            FaultRehearsalDevice("H2", 7, ("C",), "C"),
        ]
        result = solve_phase_fault_rehearsal(
            devices, {"A": 10, "B": 10, "C": 10}, "A"
        )
        self.assertEqual(result.original_loads, {"A": 0, "B": 5, "C": 7})
        self.assertEqual(result.loads, {"A": 0, "B": 5, "C": 7})
        self.assertEqual(result.migrations, ())
        self.assertEqual(result.disconnected, ())
        self.assertEqual(result.retained_power, 12)
        self.assertEqual(result.retained_count, 2)
        self.assertEqual(result.choice_sequence, ())

    def test_fault_with_b_and_c_healthy_phases(self):
        # B 相故障：健康相为 A、C，选项顺序 A→C→断开
        devices = [
            FaultRehearsalDevice("F1", 4, ("A", "B"), "B"),
            FaultRehearsalDevice("H1", 2, ("A",), "A"),
        ]
        result = solve_phase_fault_rehearsal(
            devices, {"A": 10, "B": 10, "C": 10}, "B"
        )
        self.assertEqual(result.loads, {"A": 6, "B": 0, "C": 0})
        self.assertEqual([m.code for m in result.migrations], ["F1"])
        self.assertEqual(result.migrations[0].to_phase, "A")
        self.assertEqual(result.choice_sequence, ("A",))

    def test_capacity_never_exceeded(self):
        # 健康相已被占满：故障相设备无容量可迁，全部断开
        devices = [
            FaultRehearsalDevice("F1", 5, ("B", "C"), "A"),
            FaultRehearsalDevice("F2", 5, ("B", "C"), "A"),
            FaultRehearsalDevice("H1", 10, ("B",), "B"),
            FaultRehearsalDevice("H2", 10, ("C",), "C"),
        ]
        caps = {"A": 100, "B": 10, "C": 10}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        self.assertEqual(result.loads, {"A": 0, "B": 10, "C": 10})
        self.assertEqual(result.migrations, ())
        self.assertEqual(
            [d.code for d in result.disconnected], ["F1", "F2"]
        )
        self.assertEqual(result.choice_sequence, ("D", "D"))
        self.assertEqual(result.retained_power, 20)
        self.assertEqual(result.retained_count, 2)


class PhaseFaultRehearsalFuzzTest(unittest.TestCase):
    """与朴素全枚举参照实现做随机对拍。"""

    def _check_against(self, devices, capacities, fault_phase):
        result = solve_phase_fault_rehearsal(devices, capacities, fault_phase)
        expected = brute_force_fault(devices, capacities, fault_phase)
        self.assertIsNotNone(expected)  # 全断开恒可行
        _, exp_combo, exp_loads, exp_migrations, exp_disconnected, exp_power, exp_count = expected
        self.assertEqual(result.loads, exp_loads)
        self.assertEqual(result.retained_power, exp_power)
        self.assertEqual(result.retained_count, exp_count)
        actual_migrations = {m.code: m.to_phase for m in result.migrations}
        self.assertEqual(actual_migrations, exp_migrations)
        self.assertEqual(
            [d.code for d in result.disconnected], exp_disconnected
        )
        ordered_fault = sorted(
            (d for d in devices if d.original_phase == fault_phase),
            key=lambda d: d.code,
        )
        self.assertEqual(
            result.choice_sequence,
            tuple(exp_combo),
        )
        self.assertEqual(len(result.choice_sequence), len(ordered_fault))
        # 自洽性：故障相负载为零，健康相负载不超容量
        self.assertEqual(result.loads[fault_phase], 0)
        for p in PHASES:
            self.assertLessEqual(result.loads[p], capacities[p])
        self.assertEqual(
            result.disconnected_power,
            sum(d.power for d in result.disconnected),
        )
        total_power = sum(d.power for d in devices)
        self.assertEqual(
            result.retained_power + result.disconnected_power, total_power
        )
        self.assertEqual(
            result.retained_count + result.disconnected_count, len(devices)
        )

    def test_against_brute_force(self):
        rng = random.Random(20260930)
        for case in range(500):
            n = rng.randint(1, 7)
            devices = []
            original_loads = {p: 0 for p in PHASES}
            for i in range(n):
                allowed = tuple(p for p in PHASES if rng.random() < 0.7) or ("A",)
                # 原相位取自允许集合（模拟可行计算记录的分配结果）
                original_phase = rng.choice(allowed)
                power = rng.randint(1, 20)
                devices.append(
                    FaultRehearsalDevice(
                        f"DEV-{i}", power, allowed, original_phase
                    )
                )
                original_loads[original_phase] += power
            # 容量不低于原分配负载（记录可行），叠加随机余量以同时覆盖
            # 「无迁移空间」与「有迁移空间」两类情形
            capacities = {
                p: original_loads[p] + rng.randint(0, 30) for p in PHASES
            }
            fault_phase = PHASES[case % 3]
            self._check_against(devices, capacities, fault_phase)

    def test_twelve_devices_performance(self):
        # 12 台设备全部位于故障相、全相位可接、紧容量：求解器须在极短时间内
        # 完成（剪枝后实际枚举远小于 4^12），并保证结果自洽
        devices = [
            FaultRehearsalDevice(
                f"D{i:02d}", 2 * i + 1, PHASES, "A"
            )
            for i in range(12)
        ]
        capacities = {"A": 10**6, "B": 40, "C": 50}
        result = solve_phase_fault_rehearsal(devices, capacities, "A")
        self.assertEqual(result.loads["A"], 0)
        for p in PHASES:
            self.assertLessEqual(result.loads[p], capacities[p])
        # 保留功率 + 断开功率 = 全部设备总功率；保留数 + 断开数 = 设备总数
        total_power = sum(d.power for d in devices)
        self.assertEqual(
            result.retained_power + result.disconnected_power, total_power
        )
        self.assertEqual(
            result.retained_count + result.disconnected_count, 12
        )
        # 选择序列长度等于故障相设备数，元素取自 B/C/D
        self.assertEqual(len(result.choice_sequence), 12)
        self.assertTrue(all(c in ("B", "C", "D") for c in result.choice_sequence))
        # B/C 上保留的功率即保留总功率（无健康相设备），且不超容量
        self.assertEqual(
            result.retained_power, result.loads["B"] + result.loads["C"]
        )

    def test_nine_devices_against_brute_force(self):
        # 9 台故障相设备对拍：4^9 ≈ 26 万全枚举，紧容量下大量组合
        # 被容量约束提前终止，覆盖较大规模下的最优性与字典序
        devices = [
            FaultRehearsalDevice(f"D{i:02d}", 2 * i + 1, PHASES, "A")
            for i in range(9)
        ]
        capacities = {"A": 10**6, "B": 30, "C": 30}
        result = solve_phase_fault_rehearsal(devices, capacities, "A")
        expected = brute_force_fault(devices, capacities, "A")
        self.assertEqual(result.retained_power, expected[5])
        self.assertEqual(result.retained_count, expected[6])
        self.assertEqual(result.choice_sequence, expected[1])
        self.assertEqual(result.loads, expected[2])

    def test_twelve_devices_all_fault_phase_tight(self):
        # 极端情形：无健康相设备，所有设备只能断开（允许相位仅故障相）
        devices = [
            FaultRehearsalDevice(f"D{i:02d}", i + 1, ("A",), "A")
            for i in range(12)
        ]
        caps = {"A": 100, "B": 100, "C": 100}
        result = solve_phase_fault_rehearsal(devices, caps, "A")
        self.assertEqual(result.loads, {"A": 0, "B": 0, "C": 0})
        self.assertEqual(result.migrations, ())
        self.assertEqual(result.disconnected_count, 12)
        self.assertEqual(result.retained_power, 0)
        self.assertEqual(result.choice_sequence, ("D",) * 12)


if __name__ == "__main__":
    unittest.main()