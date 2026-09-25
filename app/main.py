"""FastAPI 应用入口：临时展位三相配电排布 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, status

from . import db
from .schemas import (
    AssignmentItem,
    CalculationDetail,
    CalculationRequest,
    CalculationResult,
    CalculationSummary,
    CapacityUpgradeResult,
    FaultDisconnectedItem,
    FaultMigrationItem,
    Loads,
    Phase,
    PhaseFaultRehearsalResult,
    RobustRehearsalRequest,
    RobustRehearsalResult,
    StartupPlanRequest,
    StartupPlanResult,
    StartupStep,
)
from .solver import (
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

_MESSAGE_FEASIBLE = "计算成功：已找到三相负载最大值与最小值之差最小的分配方案"
_MESSAGE_INFEASIBLE = "无可行分配：不存在所有设备均接入且各相不超容量的方案"
_MESSAGE_UPGRADE = (
    "扩容建议：已在最小化三相扩容总量、再最小化单相最大扩容量的目标下，"
    "找到使所有设备均接入的容量与分配方案"
)
_MESSAGE_ROBUST_FEASIBLE = (
    "预演成功：已找到任意功率组合下各相均不超容量、"
    "且最坏负载差最小的稳健分配方案"
)
_MESSAGE_ROBUST_INFEASIBLE = (
    "无稳健可行分配：不存在任意功率组合下各相均不超容量的接入方案"
)
_MESSAGE_STARTUP_FEASIBLE = (
    "预演成功：已找到全过程三相负载最大值与最小值之差峰值最小的安全启动顺序"
)
_MESSAGE_STARTUP_INFEASIBLE = (
    "无安全启动顺序：不存在每个启动瞬间及稳定状态各相均不超容量的启动顺序"
)
_MESSAGE_FAULT = (
    "预演成功：健康相设备保持原分配，故障相设备在允许健康相不超容量的前提下，"
    "已找到保留总功率最大、再保留设备数最多，且相位选择序列字典序最小的保供方案"
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 应用启动时自动完成数据库初始化（建目录、建表）
    db.init_db()
    yield


app = FastAPI(
    title="临时展位三相配电排布 API",
    description=(
        "提交最多 12 台设备（唯一编号、正整数功率、非空可接入相位集合）"
        "及 A/B/C 各相容量，求解使三相负载最大值与最小值之差最小的分配方案；"
        "差值相同时取按设备编号排序后相位序列字典序最小的方案。"
        "每次计算的输入、分配结果与各相负载均持久化，可随时查询。"
        "另提供稳健分相预演：设备功率为正整数区间，求解任意功率组合下"
        "各相均不超容量、且最坏负载差最小的分配（仅预演，不持久化）。"
        "对已有可行分配的计算记录，还可做启动顺序预演：逐台给定启动瞬时功率，"
        "在固定相位与容量下求解全过程三相负载差峰值最小的一台接一台启动顺序"
        "（仅预演，不修改历史记录）。"
        "对已有可行分配的计算记录，亦可做单相故障保供预演：指定故障相后，"
        "健康相设备保持原分配，故障相设备迁至允许的健康相或断开，"
        "先最大化保留总功率、再最大化保留设备数（仅预演，不修改历史记录）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """健康检查。"""
    return {"status": "ok"}


@app.post(
    "/api/v1/calculations",
    response_model=CalculationResult,
    status_code=status.HTTP_201_CREATED,
    tags=["calculations"],
)
def create_calculation(payload: CalculationRequest) -> CalculationResult:
    """创建一次排布计算：求解最优分配并保存输入、分配与各相负载。

    无可行分配时同样保存记录，返回 ``feasible=false`` 及说明信息。
    """
    devices = [
        Device(code=d.id, power=d.power, allowed=tuple(d.allowed_phases))
        for d in payload.devices
    ]
    capacities = {
        "A": payload.capacities.A,
        "B": payload.capacities.B,
        "C": payload.capacities.C,
    }
    result = solve(devices, capacities)
    calculation_id = db.insert_calculation(payload, result)
    record = db.get_calculation(calculation_id)

    assignments = (
        [
            AssignmentItem(device_id=d["device_id"], phase=d["assigned_phase"])
            for d in record["devices"]
        ]
        if result.feasible
        else []
    )
    return CalculationResult(
        id=record["id"],
        created_at=record["created_at"],
        capacities=Loads(**record["capacities"]),
        feasible=result.feasible,
        imbalance=result.imbalance,
        loads=Loads(**record["loads"]) if result.feasible else None,
        message=_MESSAGE_FEASIBLE if result.feasible else _MESSAGE_INFEASIBLE,
        assignments=assignments,
    )


@app.get(
    "/api/v1/calculations",
    response_model=list[CalculationSummary],
    tags=["calculations"],
)
def list_calculations(
    limit: int = Query(default=100, ge=1, le=500, description="返回条数上限"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
) -> list[CalculationSummary]:
    """查询历史计算记录摘要（按创建时间倒序）。"""
    return [CalculationSummary(**item) for item in db.list_calculations(limit, offset)]


@app.get(
    "/api/v1/calculations/{calculation_id}",
    response_model=CalculationDetail,
    tags=["calculations"],
)
def get_calculation(calculation_id: int) -> CalculationDetail:
    """查询单条计算记录详情：输入设备、分配结果与各相负载。"""
    record = db.get_calculation(calculation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"计算记录不存在：id={calculation_id}",
        )
    assignments = (
        [
            AssignmentItem(device_id=d["device_id"], phase=d["assigned_phase"])
            for d in record["devices"]
        ]
        if record["feasible"]
        else []
    )
    return CalculationDetail(
        id=record["id"],
        created_at=record["created_at"],
        capacities=Loads(**record["capacities"]),
        feasible=record["feasible"],
        imbalance=record["imbalance"],
        loads=Loads(**record["loads"]) if record["feasible"] else None,
        message=_MESSAGE_FEASIBLE if record["feasible"] else _MESSAGE_INFEASIBLE,
        assignments=assignments,
        devices=record["devices"],
    )


@app.post(
    "/api/v1/calculations/{calculation_id}/capacity-upgrade",
    response_model=CapacityUpgradeResult,
    tags=["calculations"],
)
def capacity_upgrade(calculation_id: int) -> CapacityUpgradeResult:
    """针对无可行分配的计算记录，给出最小扩容建议。

    保持设备功率与允许相位不变，计算各相需要增加的非负整数容量及对应分配，
    使所有设备接入；依次最小化三相扩容总量与单相最大扩容量，仍相同时取
    按设备编号排序的相位序列字典序最小的方案。仅返回建议，原计算记录不变。

    记录不存在返回 404；原记录已有可行分配返回 409。
    """
    record = db.get_calculation(calculation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"计算记录不存在：id={calculation_id}",
        )
    if record["feasible"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"计算记录已有可行分配，无需扩容：id={calculation_id}",
        )

    devices = [
        Device(
            code=d["device_id"],
            power=d["power"],
            allowed=tuple(d["allowed_phases"]),
        )
        for d in record["devices"]
    ]
    capacities = {
        "A": record["capacities"]["A"],
        "B": record["capacities"]["B"],
        "C": record["capacities"]["C"],
    }
    upgrade = solve_capacity_upgrade(devices, capacities)

    assignments = [
        AssignmentItem(device_id=code, phase=upgrade.assignment[code])
        for code in sorted(upgrade.assignment)
    ]
    return CapacityUpgradeResult(
        id=record["id"],
        created_at=record["created_at"],
        original_capacities=Loads(**upgrade.original_capacities),
        capacity_upgrades=Loads(**upgrade.upgrades),
        suggested_capacities=Loads(**upgrade.suggested_capacities),
        total_upgrade=upgrade.total_upgrade,
        max_upgrade=upgrade.max_upgrade,
        assignments=assignments,
        loads=Loads(**upgrade.loads),
        message=_MESSAGE_UPGRADE,
    )


@app.post(
    "/api/v1/robust-rehearsals",
    response_model=RobustRehearsalResult,
    tags=["rehearsals"],
)
def robust_rehearsal(payload: RobustRehearsalRequest) -> RobustRehearsalResult:
    """稳健分相预演：设备功率为正整数区间，求解稳健最优分配。

    每台设备功率可在 [min_power, max_power] 内独立变化。寻找每台设备接入
    一个允许相位、且任意功率组合下各相均不超容量的分配；在稳健可行方案中
    最小化任意功率组合可能产生的三相最大负载与最小负载之差，同值时取
    按设备编号排序的相位序列字典序最小者。

    仅做预演，不写入历史计算记录；无稳健可行方案时返回 ``feasible=false``。
    """
    devices = [
        RobustDevice(
            code=d.id,
            min_power=d.min_power,
            max_power=d.max_power,
            allowed=tuple(d.allowed_phases),
        )
        for d in payload.devices
    ]
    capacities = {
        "A": payload.capacities.A,
        "B": payload.capacities.B,
        "C": payload.capacities.C,
    }
    result = solve_robust(devices, capacities)

    assignments = (
        [
            AssignmentItem(device_id=code, phase=result.assignment[code])
            for code in sorted(result.assignment)
        ]
        if result.feasible
        else []
    )
    return RobustRehearsalResult(
        capacities=Loads(**capacities),
        feasible=result.feasible,
        worst_imbalance=result.worst_imbalance,
        load_lower_bounds=Loads(**result.load_lower) if result.feasible else None,
        load_upper_bounds=Loads(**result.load_upper) if result.feasible else None,
        assignments=assignments,
        message=(
            _MESSAGE_ROBUST_FEASIBLE if result.feasible else _MESSAGE_ROBUST_INFEASIBLE
        ),
    )


@app.post(
    "/api/v1/calculations/{calculation_id}/startup-plan",
    response_model=StartupPlanResult,
    tags=["calculations"],
)
def startup_plan(calculation_id: int, payload: StartupPlanRequest) -> StartupPlanResult:
    """启动顺序预演：为已有可行分配的计算记录规划一台接一台的安全启动顺序。

    沿用记录中的固定相位与容量，请求逐台给出启动瞬时功率（须为不低于额定
    功率的正整数，且设备编号与记录完全一致）。设备一次启动一台：启动瞬间
    该设备按启动功率计，完成后按额定功率持续运行；每个启动瞬间及稳定状态
    各相均不得超容量。在所有安全启动顺序中，最小化全过程三相负载最大值与
    最小值之差的峰值；同峰值时取设备编号序列字典序最小者。返回启动顺序、
    逐步瞬时与稳定负载及峰值；无安全顺序时返回 ``feasible=false``。
    仅做预演，不修改历史计算记录。

    记录不存在返回 404；原记录无可行分配返回 409；启动功率低于额定功率
    或设备编号与记录不一致返回 422。
    """
    record = db.get_calculation(calculation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"计算记录不存在：id={calculation_id}",
        )
    if not record["feasible"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"计算记录无可行分配，无法规划启动顺序：id={calculation_id}",
        )

    record_devices = {d["device_id"]: d for d in record["devices"]}
    request_powers = {
        item.device_id: item.startup_power for item in payload.startup_powers
    }
    if set(request_powers) != set(record_devices):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="设备编号须与计算记录完全一致（不多不少）",
        )
    for device_id, startup_power in request_powers.items():
        rated = record_devices[device_id]["power"]
        if startup_power < rated:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"启动瞬时功率不得低于额定功率："
                    f"设备 {device_id} 额定功率为 {rated}"
                ),
            )

    devices = [
        StartupDevice(
            code=d["device_id"],
            phase=d["assigned_phase"],
            rated_power=d["power"],
            startup_power=request_powers[d["device_id"]],
        )
        for d in record["devices"]
    ]
    result = solve_startup_plan(devices, record["capacities"])

    steps = (
        [
            StartupStep(
                device_id=step.code,
                phase=step.phase,
                rated_power=step.rated_power,
                startup_power=step.startup_power,
                transient_loads=Loads(**step.transient_loads),
                transient_imbalance=step.transient_imbalance,
                steady_loads=Loads(**step.steady_loads),
                steady_imbalance=step.steady_imbalance,
            )
            for step in result.steps
        ]
        if result.feasible
        else []
    )
    return StartupPlanResult(
        id=record["id"],
        created_at=record["created_at"],
        capacities=Loads(**record["capacities"]),
        feasible=result.feasible,
        peak_imbalance=result.peak_imbalance,
        startup_order=list(result.order) if result.feasible else [],
        steps=steps,
        message=(
            _MESSAGE_STARTUP_FEASIBLE if result.feasible else _MESSAGE_STARTUP_INFEASIBLE
        ),
    )


@app.post(
    "/api/v1/calculations/{calculation_id}/phase-fault-rehearsal",
    response_model=PhaseFaultRehearsalResult,
    tags=["calculations"],
)
def phase_fault_rehearsal(
    calculation_id: int, fault_phase: Phase = Query(..., description="故障相（A/B/C）")
) -> PhaseFaultRehearsalResult:
    """单相故障保供预演：指定故障相，规划故障相设备的迁移 / 断开方案。

    健康相设备保持原分配不动；故障相设备只能迁至其允许的健康相（迁入后该相
    不得超容量）或断开，故障相最终负载为零。在所有方案中先最大化保留设备的
    总功率，再最大化保留设备数；仍同值时，按故障相设备编号排序，取相位选择
    序列字典序最小者（A＜B＜C＜断开）。返回最终各相负载、迁移分配、断开
    设备及保留功率。仅做预演，不修改历史计算记录。

    记录不存在返回 404；原记录无可行分配返回 409；故障相取值非法返回 422。
    """
    record = db.get_calculation(calculation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"计算记录不存在：id={calculation_id}",
        )
    if not record["feasible"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"计算记录无可行分配，无法进行故障保供预演：id={calculation_id}",
        )

    devices = [
        FaultRehearsalDevice(
            code=d["device_id"],
            power=d["power"],
            allowed=tuple(d["allowed_phases"]),
            original_phase=d["assigned_phase"],
        )
        for d in record["devices"]
    ]
    result = solve_phase_fault_rehearsal(
        devices, record["capacities"], fault_phase=fault_phase
    )

    migrations = [
        FaultMigrationItem(
            device_id=item.code,
            power=item.power,
            from_phase=item.from_phase,
            to_phase=item.to_phase,
        )
        for item in result.migrations
    ]
    disconnected = [
        FaultDisconnectedItem(
            device_id=item.code, power=item.power, phase=item.phase
        )
        for item in result.disconnected
    ]
    return PhaseFaultRehearsalResult(
        id=record["id"],
        created_at=record["created_at"],
        fault_phase=result.fault_phase,
        capacities=Loads(**result.capacities),
        original_loads=Loads(**result.original_loads),
        loads=Loads(**result.loads),
        migrations=migrations,
        disconnected=disconnected,
        retained_power=result.retained_power,
        retained_count=result.retained_count,
        disconnected_power=result.disconnected_power,
        disconnected_count=result.disconnected_count,
        message=_MESSAGE_FAULT,
    )
