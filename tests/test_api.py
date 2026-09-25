"""API 集成测试（需要安装 fastapi / httpx，使用临时数据库文件）。

运行方式：python3 -m pytest tests/test_api.py
"""

import os
import tempfile

# 须在导入 app 之前设置数据库路径，避免污染本地数据
_tmpdir = tempfile.mkdtemp(prefix="phase-api-test-")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

# 显式建表，兼容未通过 with 上下文触发 lifespan 的 TestClient 用法
db.init_db()
client = TestClient(app)

VALID_PAYLOAD = {
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
        {"id": "D1", "power": 10, "allowed_phases": ["A", "B", "C"]},
        {"id": "D2", "power": 20, "allowed_phases": ["A", "B", "C"]},
        {"id": "D3", "power": 30, "allowed_phases": ["A", "B", "C"]},
    ],
}


def test_create_calculation_feasible():
    resp = client.post("/api/v1/calculations", json=VALID_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert body["feasible"] is True
    assert body["imbalance"] == 20
    assert body["loads"] == {"A": 10, "B": 20, "C": 30}
    assert body["assignments"] == [
        {"device_id": "D1", "phase": "A"},
        {"device_id": "D2", "phase": "B"},
        {"device_id": "D3", "phase": "C"},
    ]
    assert body["id"] >= 1


def test_create_calculation_infeasible():
    payload = {
        "capacities": {"A": 5, "B": 0, "C": 0},
        "devices": [{"id": "BIG", "power": 50, "allowed_phases": ["A", "B", "C"]}],
    }
    resp = client.post("/api/v1/calculations", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["feasible"] is False
    assert body["imbalance"] is None
    assert body["loads"] is None
    assert body["assignments"] == []
    assert "无可行" in body["message"]


def test_list_and_get_detail():
    created = client.post("/api/v1/calculations", json=VALID_PAYLOAD).json()
    calc_id = created["id"]

    listing = client.get("/api/v1/calculations").json()
    assert any(item["id"] == calc_id for item in listing)

    detail = client.get(f"/api/v1/calculations/{calc_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["feasible"] is True
    assert body["loads"] == {"A": 10, "B": 20, "C": 30}
    assert {d["device_id"] for d in body["devices"]} == {"D1", "D2", "D3"}
    for device in body["devices"]:
        assert device["assigned_phase"] in ("A", "B", "C")


def test_get_not_found():
    resp = client.get("/api/v1/calculations/999999")
    assert resp.status_code == 404


def test_validation_duplicate_ids():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [
            {"id": "D1", "power": 5, "allowed_phases": ["A"]},
            {"id": "D1", "power": 5, "allowed_phases": ["B"]},
        ],
    }
    assert client.post("/api/v1/calculations", json=payload).status_code == 422


def test_validation_too_many_devices():
    payload = {
        "capacities": {"A": 1000, "B": 1000, "C": 1000},
        "devices": [
            {"id": f"D{i}", "power": 1, "allowed_phases": ["A"]} for i in range(13)
        ],
    }
    assert client.post("/api/v1/calculations", json=payload).status_code == 422


def test_validation_empty_allowed_phases():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [{"id": "D1", "power": 5, "allowed_phases": []}],
    }
    assert client.post("/api/v1/calculations", json=payload).status_code == 422


def test_validation_non_positive_power():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [{"id": "D1", "power": 0, "allowed_phases": ["A"]}],
    }
    assert client.post("/api/v1/calculations", json=payload).status_code == 422


def test_validation_invalid_phase():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [{"id": "D1", "power": 5, "allowed_phases": ["D"]}],
    }
    assert client.post("/api/v1/calculations", json=payload).status_code == 422


def test_allowed_phases_deduplicated():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [{"id": "D1", "power": 5, "allowed_phases": ["C", "A", "C"]}],
    }
    resp = client.post("/api/v1/calculations", json=payload)
    assert resp.status_code == 201
    detail = client.get(f"/api/v1/calculations/{resp.json()['id']}").json()
    assert detail["devices"][0]["allowed_phases"] == ["A", "C"]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


INFEASIBLE_UPGRADE_PAYLOAD = {
    "capacities": {"A": 5, "B": 0, "C": 0},
    "devices": [
        {"id": "BIG", "power": 50, "allowed_phases": ["A", "B", "C"]},
        {"id": "FIX", "power": 7, "allowed_phases": ["A"]},
    ],
}


def _create(payload):
    return client.post("/api/v1/calculations", json=payload).json()


def test_capacity_upgrade_success():
    created = _create(INFEASIBLE_UPGRADE_PAYLOAD)
    calc_id = created["id"]
    assert created["feasible"] is False

    resp = client.post(f"/api/v1/calculations/{calc_id}/capacity-upgrade")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == calc_id
    assert body["original_capacities"] == {"A": 5, "B": 0, "C": 0}
    # FIX 只能接 A：A 相负载至少 7；BIG(50) 若也放 A 则扩容 52，
    # 总量最小为 BIG 放 B：扩 B 50，A 不扩（FIX=7 > 原容量 5 -> 还需扩 A 2）
    assert body["capacity_upgrades"] == {"A": 2, "B": 50, "C": 0}
    assert body["total_upgrade"] == 52
    assert body["max_upgrade"] == 50
    assert body["suggested_capacities"] == {"A": 7, "B": 50, "C": 0}
    assert body["loads"] == {"A": 7, "B": 50, "C": 0}
    assignments = {a["device_id"]: a["phase"] for a in body["assignments"]}
    assert assignments == {"BIG": "B", "FIX": "A"}
    # 分配按设备编号排序
    assert [a["device_id"] for a in body["assignments"]] == ["BIG", "FIX"]
    assert "扩容" in body["message"]


def test_capacity_upgrade_404():
    resp = client.post("/api/v1/calculations/999999/capacity-upgrade")
    assert resp.status_code == 404


def test_capacity_upgrade_409_when_feasible():
    created = _create(VALID_PAYLOAD)
    calc_id = created["id"]
    assert created["feasible"] is True
    resp = client.post(f"/api/v1/calculations/{calc_id}/capacity-upgrade")
    assert resp.status_code == 409


def test_capacity_upgrade_does_not_modify_record():
    created = _create(INFEASIBLE_UPGRADE_PAYLOAD)
    calc_id = created["id"]
    client.post(f"/api/v1/calculations/{calc_id}/capacity-upgrade")

    detail = client.get(f"/api/v1/calculations/{calc_id}").json()
    # 原记录仍为无可行分配，容量/负载/分配均不变
    assert detail["feasible"] is False
    assert detail["capacities"] == {"A": 5, "B": 0, "C": 0}
    assert detail["loads"] is None
    assert detail["imbalance"] is None
    assert detail["assignments"] == []
    for device in detail["devices"]:
        assert device["assigned_phase"] is None

    # 再调一次结果确定且相同（幂等读操作）
    again = client.post(f"/api/v1/calculations/{calc_id}/capacity-upgrade").json()
    assert again["capacity_upgrades"] == {"A": 2, "B": 50, "C": 0}


def test_capacity_upgrade_single_forced_phase():
    payload = {
        "capacities": {"A": 100, "B": 0, "C": 0},
        "devices": [
            {"id": "X1", "power": 50, "allowed_phases": ["A"]},
            {"id": "X2", "power": 60, "allowed_phases": ["A"]},
        ],
    }
    created = _create(payload)
    assert created["feasible"] is False
    body = client.post(
        f"/api/v1/calculations/{created['id']}/capacity-upgrade"
    ).json()
    assert body["capacity_upgrades"] == {"A": 10, "B": 0, "C": 0}
    assert body["suggested_capacities"] == {"A": 110, "B": 0, "C": 0}
    assert body["loads"] == {"A": 110, "B": 0, "C": 0}
    assert body["total_upgrade"] == 10
    assert body["max_upgrade"] == 10


ROBUST_PAYLOAD = {
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
        {"id": "D1", "min_power": 5, "max_power": 10, "allowed_phases": ["A", "B", "C"]},
        {"id": "D2", "min_power": 15, "max_power": 20, "allowed_phases": ["A", "B", "C"]},
        {"id": "D3", "min_power": 25, "max_power": 30, "allowed_phases": ["B", "C"]},
    ],
}


def test_robust_rehearsal_feasible():
    resp = client.post("/api/v1/robust-rehearsals", json=ROBUST_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["capacities"] == {"A": 100, "B": 100, "C": 100}
    # 字典序最小最优分配：D1->A, D2->B, D3->C
    assert body["assignments"] == [
        {"device_id": "D1", "phase": "A"},
        {"device_id": "D2", "phase": "B"},
        {"device_id": "D3", "phase": "C"},
    ]
    assert body["load_lower_bounds"] == {"A": 5, "B": 15, "C": 25}
    assert body["load_upper_bounds"] == {"A": 10, "B": 20, "C": 30}
    # 最坏负载差 = 上界最大值 30 - 下界最小值 5 = 25
    assert body["worst_imbalance"] == 25
    assert "预演" in body["message"]


def test_robust_rehearsal_infeasible():
    payload = {
        "capacities": {"A": 40, "B": 40, "C": 40},
        "devices": [
            {"id": "BIG", "min_power": 1, "max_power": 50, "allowed_phases": ["A", "B", "C"]},
        ],
    }
    resp = client.post("/api/v1/robust-rehearsals", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    assert body["worst_imbalance"] is None
    assert body["load_lower_bounds"] is None
    assert body["load_upper_bounds"] is None
    assert body["assignments"] == []
    assert "无稳健可行" in body["message"]


def test_robust_rehearsal_does_not_persist():
    before = len(client.get("/api/v1/calculations").json())
    resp = client.post("/api/v1/robust-rehearsals", json=ROBUST_PAYLOAD)
    assert resp.status_code == 200
    after = len(client.get("/api/v1/calculations").json())
    # 预演接口不写入任何历史计算记录
    assert after == before
    assert "id" not in resp.json()


def test_robust_rehearsal_capacity_checked_on_upper_bound():
    # 下界 3 不超 A 相容量 8，但上界 10 超过，故 D1 只能接 B
    payload = {
        "capacities": {"A": 8, "B": 10, "C": 0},
        "devices": [
            {"id": "D1", "min_power": 3, "max_power": 10, "allowed_phases": ["A", "B"]},
        ],
    }
    body = client.post("/api/v1/robust-rehearsals", json=payload).json()
    assert body["feasible"] is True
    assert body["assignments"] == [{"device_id": "D1", "phase": "B"}]
    assert body["load_upper_bounds"] == {"A": 0, "B": 10, "C": 0}


def test_robust_rehearsal_validation_min_above_max():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [
            {"id": "D1", "min_power": 9, "max_power": 5, "allowed_phases": ["A"]},
        ],
    }
    assert client.post("/api/v1/robust-rehearsals", json=payload).status_code == 422


def test_robust_rehearsal_validation_non_positive_bounds():
    for field in ("min_power", "max_power"):
        payload = {
            "capacities": {"A": 10, "B": 10, "C": 10},
            "devices": [
                {"id": "D1", "min_power": 1, "max_power": 5, "allowed_phases": ["A"]},
            ],
        }
        payload["devices"][0][field] = 0
        assert (
            client.post("/api/v1/robust-rehearsals", json=payload).status_code == 422
        )


def test_robust_rehearsal_validation_duplicate_ids():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [
            {"id": "D1", "min_power": 1, "max_power": 5, "allowed_phases": ["A"]},
            {"id": "D1", "min_power": 1, "max_power": 5, "allowed_phases": ["B"]},
        ],
    }
    assert client.post("/api/v1/robust-rehearsals", json=payload).status_code == 422


def test_robust_rehearsal_validation_too_many_devices():
    payload = {
        "capacities": {"A": 1000, "B": 1000, "C": 1000},
        "devices": [
            {"id": f"D{i}", "min_power": 1, "max_power": 1, "allowed_phases": ["A"]}
            for i in range(13)
        ],
    }
    assert client.post("/api/v1/robust-rehearsals", json=payload).status_code == 422


def test_robust_rehearsal_validation_empty_allowed_phases():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [
            {"id": "D1", "min_power": 1, "max_power": 5, "allowed_phases": []},
        ],
    }
    assert client.post("/api/v1/robust-rehearsals", json=payload).status_code == 422


def test_robust_rehearsal_allowed_phases_deduplicated():
    payload = {
        "capacities": {"A": 10, "B": 10, "C": 10},
        "devices": [
            {"id": "D1", "min_power": 1, "max_power": 5, "allowed_phases": ["C", "A", "C"]},
        ],
    }
    resp = client.post("/api/v1/robust-rehearsals", json=payload)
    assert resp.status_code == 200
    assert resp.json()["feasible"] is True


STARTUP_PLAN_PAYLOAD = {
    "capacities": {"A": 10, "B": 10, "C": 10},
    "devices": [
        {"id": "D1", "power": 6, "allowed_phases": ["A"]},
        {"id": "D2", "power": 4, "allowed_phases": ["B"]},
        {"id": "D3", "power": 5, "allowed_phases": ["C"]},
    ],
}


def _startup_body(mapping):
    return {
        "startup_powers": [
            {"device_id": device_id, "startup_power": power}
            for device_id, power in mapping.items()
        ]
    }


def test_startup_plan_success():
    created = _create(STARTUP_PLAN_PAYLOAD)
    calc_id = created["id"]
    assert created["feasible"] is True

    resp = client.post(
        f"/api/v1/calculations/{calc_id}/startup-plan",
        json=_startup_body({"D1": 8, "D2": 6, "D3": 9}),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == calc_id
    assert body["capacities"] == {"A": 10, "B": 10, "C": 10}
    assert body["feasible"] is True
    # (D1,D2,D3) 与 (D2,D1,D3) 峰值同为 8，取编号序列字典序最小者
    assert body["startup_order"] == ["D1", "D2", "D3"]
    assert body["peak_imbalance"] == 8
    assert len(body["steps"]) == 3
    assert body["steps"][0] == {
        "device_id": "D1",
        "phase": "A",
        "rated_power": 6,
        "startup_power": 8,
        "transient_loads": {"A": 8, "B": 0, "C": 0},
        "transient_imbalance": 8,
        "steady_loads": {"A": 6, "B": 0, "C": 0},
        "steady_imbalance": 6,
    }
    assert body["steps"][1] == {
        "device_id": "D2",
        "phase": "B",
        "rated_power": 4,
        "startup_power": 6,
        "transient_loads": {"A": 6, "B": 6, "C": 0},
        "transient_imbalance": 6,
        "steady_loads": {"A": 6, "B": 4, "C": 0},
        "steady_imbalance": 6,
    }
    assert body["steps"][2] == {
        "device_id": "D3",
        "phase": "C",
        "rated_power": 5,
        "startup_power": 9,
        "transient_loads": {"A": 6, "B": 4, "C": 9},
        "transient_imbalance": 5,
        "steady_loads": {"A": 6, "B": 4, "C": 5},
        "steady_imbalance": 2,
    }
    assert "预演" in body["message"]


def test_startup_plan_uses_record_fixed_phases():
    # 沿用记录中的固定相位：VALID_PAYLOAD 的分配为 D1->A, D2->B, D3->C
    created = _create(VALID_PAYLOAD)
    calc_id = created["id"]
    resp = client.post(
        f"/api/v1/calculations/{calc_id}/startup-plan",
        json=_startup_body({"D1": 15, "D2": 25, "D3": 35}),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    phases = {step["device_id"]: step["phase"] for step in body["steps"]}
    assert phases == {"D1": "A", "D2": "B", "D3": "C"}
    # 末步稳定负载即原记录各相负载
    assert body["steps"][-1]["steady_loads"] == created["loads"]
    assert body["startup_order"] == ["D1", "D2", "D3"]
    assert body["peak_imbalance"] == 25


def test_startup_plan_404():
    resp = client.post(
        "/api/v1/calculations/999999/startup-plan",
        json=_startup_body({"D1": 1}),
    )
    assert resp.status_code == 404


def test_startup_plan_409_when_record_infeasible():
    created = _create(INFEASIBLE_UPGRADE_PAYLOAD)
    assert created["feasible"] is False
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json=_startup_body({"BIG": 60, "FIX": 7}),
    )
    assert resp.status_code == 409


def test_startup_plan_no_safe_order():
    # 记录可行（D1 额定 10 接 A，容量 10），但启动功率 11 超容量：无安全顺序
    created = _create({
        "capacities": {"A": 10, "B": 0, "C": 0},
        "devices": [{"id": "D1", "power": 10, "allowed_phases": ["A"]}],
    })
    assert created["feasible"] is True
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json=_startup_body({"D1": 11}),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    assert body["peak_imbalance"] is None
    assert body["startup_order"] == []
    assert body["steps"] == []
    assert "无安全启动顺序" in body["message"]


def test_startup_plan_validation_below_rated():
    created = _create(VALID_PAYLOAD)
    # D1 额定功率 10，启动功率 9 低于额定
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json=_startup_body({"D1": 9, "D2": 20, "D3": 30}),
    )
    assert resp.status_code == 422
    assert "额定功率" in resp.json()["detail"]


def test_startup_plan_validation_missing_and_extra_ids():
    created = _create(VALID_PAYLOAD)
    # 缺少 D3、多出 D9：编号须与记录完全一致
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json=_startup_body({"D1": 10, "D2": 20, "D9": 30}),
    )
    assert resp.status_code == 422


def test_startup_plan_validation_duplicate_ids():
    created = _create(VALID_PAYLOAD)
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json={"startup_powers": [
            {"device_id": "D1", "startup_power": 10},
            {"device_id": "D1", "startup_power": 12},
            {"device_id": "D2", "startup_power": 20},
            {"device_id": "D3", "startup_power": 30},
        ]},
    )
    assert resp.status_code == 422


def test_startup_plan_validation_non_positive_power():
    created = _create(VALID_PAYLOAD)
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json=_startup_body({"D1": 0, "D2": 20, "D3": 30}),
    )
    assert resp.status_code == 422


def test_startup_plan_validation_empty_body():
    created = _create(VALID_PAYLOAD)
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/startup-plan",
        json={"startup_powers": []},
    )
    assert resp.status_code == 422


def test_startup_plan_does_not_modify_record():
    created = _create(VALID_PAYLOAD)
    calc_id = created["id"]
    before = client.get(f"/api/v1/calculations/{calc_id}").json()
    count_before = len(client.get("/api/v1/calculations").json())

    resp = client.post(
        f"/api/v1/calculations/{calc_id}/startup-plan",
        json=_startup_body({"D1": 15, "D2": 25, "D3": 35}),
    )
    assert resp.status_code == 200

    # 预演不改历史：原记录内容与记录总数均不变
    after = client.get(f"/api/v1/calculations/{calc_id}").json()
    assert after == before
    assert len(client.get("/api/v1/calculations").json()) == count_before

    # 再次预演结果确定且相同（幂等读操作）
    again = client.post(
        f"/api/v1/calculations/{calc_id}/startup-plan",
        json=_startup_body({"D1": 15, "D2": 25, "D3": 35}),
    ).json()
    assert again["startup_order"] == resp.json()["startup_order"]
    assert again["peak_imbalance"] == resp.json()["peak_imbalance"]


# ---------------------------------------------------------------------------
# 单相故障保供预演
# ---------------------------------------------------------------------------

FAULT_PAYLOAD = {
    "capacities": {"A": 20, "B": 12, "C": 12},
    "devices": [
        # H1 固定 B（健康相设备，故障后保持原分配）
        {"id": "H1", "power": 7, "allowed_phases": ["B"]},
        # F1/F2 原最优分配均在 A；二者都只允许 A/B
        {"id": "F1", "power": 5, "allowed_phases": ["A", "B"]},
        {"id": "F2", "power": 5, "allowed_phases": ["A", "B"]},
    ],
}


def test_phase_fault_rehearsal_success():
    created = _create(FAULT_PAYLOAD)
    calc_id = created["id"]
    assert created["feasible"] is True
    # 原记录分配（C 无设备，imbalance 最小化下两台均在 A）：A=10，B=7，C=0
    assert created["loads"] == {"A": 10, "B": 7, "C": 0}

    resp = client.post(
        f"/api/v1/calculations/{calc_id}/phase-fault-rehearsal",
        params={"fault_phase": "A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == calc_id
    assert body["fault_phase"] == "A"
    assert body["capacities"] == {"A": 20, "B": 12, "C": 12}
    assert body["original_loads"] == {"A": 10, "B": 7, "C": 0}
    # A 故障后 B 仅剩 5 空闲：两台 5 功率设备只能迁一台（均不允许 C），
    # 同功率同数量下序列字典序最小 -> F1 迁 B、F2 断开
    assert body["loads"] == {"A": 0, "B": 12, "C": 0}
    assert body["migrations"] == [
        {"device_id": "F1", "power": 5, "from_phase": "A", "to_phase": "B"}
    ]
    assert body["disconnected"] == [
        {"device_id": "F2", "power": 5, "phase": "A"}
    ]
    assert body["retained_power"] == 12
    assert body["retained_count"] == 2
    assert body["disconnected_power"] == 5
    assert body["disconnected_count"] == 1
    assert "预演" in body["message"]
    # 健康相负载不超容量，故障相负载为零
    assert body["loads"]["A"] == 0
    assert body["loads"]["B"] <= body["capacities"]["B"]
    assert body["loads"]["C"] <= body["capacities"]["C"]


def test_phase_fault_rehearsal_disconnect_when_no_capacity():
    payload = {
        "capacities": {"A": 20, "B": 10, "C": 10},
        "devices": [
            {"id": "H1", "power": 10, "allowed_phases": ["B"]},
            {"id": "H2", "power": 10, "allowed_phases": ["C"]},
            # A 相设备只能在故障相 A，无健康相可迁，必须断开
            {"id": "F1", "power": 8, "allowed_phases": ["A"]},
            # A 相设备可迁 B/C，但两相均被占满，亦须断开
            {"id": "F2", "power": 3, "allowed_phases": ["A", "B", "C"]},
        ],
    }
    created = _create(payload)
    assert created["feasible"] is True
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/phase-fault-rehearsal",
        params={"fault_phase": "A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["loads"] == {"A": 0, "B": 10, "C": 10}
    assert body["migrations"] == []
    assert {d["device_id"] for d in body["disconnected"]} == {"F1", "F2"}
    # 断开列表按设备编号排序，携带功率与原相位
    assert [d["device_id"] for d in body["disconnected"]] == ["F1", "F2"]
    assert body["disconnected"][0] == {
        "device_id": "F1",
        "power": 8,
        "phase": "A",
    }
    assert body["disconnected_power"] == 11
    assert body["disconnected_count"] == 2
    assert body["retained_power"] == 20
    assert body["retained_count"] == 2


def test_phase_fault_rehearsal_other_fault_phases():
    created = _create(FAULT_PAYLOAD)
    calc_id = created["id"]
    # 原记录：F1/F2 在 A（各 5），H1 在 B（7），C 无设备
    # B 相故障：H1 仅允许 B，无健康相可迁，须断开；F1/F2 作为健康相设备
    # 保持原分配不动（尽管 A 尚有空闲，亦不迁移）
    resp_b = client.post(
        f"/api/v1/calculations/{calc_id}/phase-fault-rehearsal",
        params={"fault_phase": "B"},
    )
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert body_b["fault_phase"] == "B"
    assert body_b["loads"] == {"A": 10, "B": 0, "C": 0}
    assert body_b["migrations"] == []
    assert body_b["disconnected"] == [
        {"device_id": "H1", "power": 7, "phase": "B"}
    ]
    assert body_b["retained_power"] == 10
    assert body_b["retained_count"] == 2
    assert body_b["disconnected_power"] == 7

    # C 相故障：C 相本就无设备，负载与原记录一致（C 为 0）
    resp_c = client.post(
        f"/api/v1/calculations/{calc_id}/phase-fault-rehearsal",
        params={"fault_phase": "C"},
    )
    assert resp_c.status_code == 200
    body_c = resp_c.json()
    assert body_c["loads"] == {"A": 10, "B": 7, "C": 0}
    assert body_c["migrations"] == []
    assert body_c["disconnected"] == []
    assert body_c["retained_power"] == 17
    assert body_c["retained_count"] == 3


def test_phase_fault_rehearsal_404():
    resp = client.post(
        "/api/v1/calculations/999999/phase-fault-rehearsal",
        params={"fault_phase": "A"},
    )
    assert resp.status_code == 404


def test_phase_fault_rehearsal_409_when_infeasible():
    created = _create(INFEASIBLE_UPGRADE_PAYLOAD)
    assert created["feasible"] is False
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/phase-fault-rehearsal",
        params={"fault_phase": "A"},
    )
    assert resp.status_code == 409


def test_phase_fault_rehearsal_invalid_phase():
    created = _create(FAULT_PAYLOAD)
    # 故障相取值不在 A/B/C：查询参数校验返回 422
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/phase-fault-rehearsal",
        params={"fault_phase": "X"},
    )
    assert resp.status_code == 422


def test_phase_fault_rehearsal_missing_phase_param():
    created = _create(FAULT_PAYLOAD)
    # 缺少必填的 fault_phase 查询参数：422
    resp = client.post(
        f"/api/v1/calculations/{created['id']}/phase-fault-rehearsal"
    )
    assert resp.status_code == 422


def test_phase_fault_rehearsal_does_not_modify_record():
    created = _create(FAULT_PAYLOAD)
    calc_id = created["id"]
    before = client.get(f"/api/v1/calculations/{calc_id}").json()
    count_before = len(client.get("/api/v1/calculations").json())

    for phase in ("A", "B", "C"):
        resp = client.post(
            f"/api/v1/calculations/{calc_id}/phase-fault-rehearsal",
            params={"fault_phase": phase},
        )
        assert resp.status_code == 200

    # 预演不改历史：原记录内容与记录总数均不变
    after = client.get(f"/api/v1/calculations/{calc_id}").json()
    assert after == before
    assert len(client.get("/api/v1/calculations").json()) == count_before

    # 同一故障相重复预演结果确定且相同（幂等读操作）
    again = client.post(
        f"/api/v1/calculations/{calc_id}/phase-fault-rehearsal",
        params={"fault_phase": "A"},
    ).json()
    first = client.post(
        f"/api/v1/calculations/{calc_id}/phase-fault-rehearsal",
        params={"fault_phase": "A"},
    ).json()
    assert again == first
