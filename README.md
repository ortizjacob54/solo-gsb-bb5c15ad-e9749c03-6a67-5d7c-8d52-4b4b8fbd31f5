# 临时展位三相配电排布 API

基于 **Python + FastAPI + SQLite** 的临时展位三相配电排布服务。

提交最多 **12 台设备**（唯一编号、正整数功率、非空可接入相位集合）以及 **A/B/C 各相容量**，
服务在「所有设备均接入、每台设备整体接入单一允许相位、各相不超容量」的约束下求解分配方案：

1. **首要目标**：使三相负载最大值与最小值之差最小；
2. **次要目标**：差值相同时，取按设备编号排序后的相位序列字典序最小的方案；
3. 不存在可行分配时，返回明确的 `feasible: false` 结果与说明信息。

每次计算的**输入、分配结果与各相负载**均持久化到 SQLite，可随时查询历史记录。
对于原容量下无可行分配的记录，还可通过扩容接口获取「各相最小扩容量 + 对应分配」
建议（保持设备功率与允许相位不变，且不修改原记录）。

此外，服务提供**稳健分相预演**接口：每台设备功率为正整数区间
`[min_power, max_power]`，区间内可独立变化。接口求解「每台设备接入一个允许相位、
且**任意功率组合下各相均不超容量**」的分配，并在其中最小化任意功率组合可能产生的
三相最大负载与最小负载之差。预演结果**不写入历史计算记录**。

对于**已有可行分配**的计算记录，服务还提供**启动顺序预演**接口：沿用记录中的
固定相位与容量，逐台给定**不低于额定功率的启动瞬时功率**，设备一台接一台启动
（启动瞬间按启动功率计，完成后按额定功率持续运行），求解使**全过程三相负载最大值
与最小值之差的峰值**最小的启动顺序。预演同样**不修改历史记录**。

此外，服务还提供**单相故障保供预演**接口：对已有可行分配的记录指定一个故障相，
健康相设备保持原分配，故障相设备只能迁至其允许的健康相（不得超容量）或断开，
在所有应急方案中**先最大化保留设备的总功率，再最大化保留设备数**，同值时取
相位选择序列字典序最小者。预演同样**不修改历史记录**。

## 快速开始（一键启动）

前置条件：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

依赖安装与数据库初始化（自动建表）均在容器启动时完成，无需任何额外步骤。

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | 服务根端口 |
| http://localhost:8000/docs | Swagger 交互式 API 文档 |
| http://localhost:8000/health | 健康检查 |

停止服务：`docker compose down`（加 `-v` 可同时清除数据卷）。

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/app.db`；本地运行 `data/app.db` | SQLite 数据库文件路径，通过环境变量覆盖 |
| 端口映射 | `8000:8000` | 在 `docker-compose.yml` 的 `ports` 中调整宿主机端口 |

数据库文件保存在 Compose 命名卷 `app-data` 中，容器重建后历史计算记录不丢失。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/calculations` | 创建一次排布计算，返回最优分配、各相负载与差值 |
| `GET` | `/api/v1/calculations` | 查询历史计算记录摘要（支持 `limit` / `offset` 分页） |
| `GET` | `/api/v1/calculations/{id}` | 查询单条记录详情（含原始输入设备与分配结果） |
| `POST` | `/api/v1/calculations/{id}/capacity-upgrade` | 为「无可行分配」的记录给出最小扩容建议（原记录不变） |
| `POST` | `/api/v1/calculations/{id}/startup-plan` | 为「有可行分配」的记录预演负载差峰值最小的安全启动顺序（原记录不变） |
| `POST` | `/api/v1/calculations/{id}/phase-fault-rehearsal` | 为「有可行分配」的记录预演单相故障下的保供迁移 / 断开方案（原记录不变） |
| `POST` | `/api/v1/robust-rehearsals` | 稳健分相预演：设备功率为区间，求任意功率组合下均不超容量的最优分配（不持久化） |
| `GET` | `/health` | 健康检查 |

### 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
      {"id": "D1", "power": 10, "allowed_phases": ["A", "B", "C"]},
      {"id": "D2", "power": 20, "allowed_phases": ["A", "B", "C"]},
      {"id": "D3", "power": 30, "allowed_phases": ["B", "C"]}
    ]
  }'
```

### 响应示例（有可行方案）

```json
{
  "id": 1,
  "created_at": "2026-09-24T08:00:00.000000+00:00",
  "capacities": {"A": 100, "B": 100, "C": 100},
  "feasible": true,
  "imbalance": 20,
  "loads": {"A": 10, "B": 20, "C": 30},
  "message": "计算成功：已找到三相不平衡度最小的分配方案",
  "assignments": [
    {"device_id": "D1", "phase": "A"},
    {"device_id": "D2", "phase": "B"},
    {"device_id": "D3", "phase": "C"}
  ]
}
```

### 响应示例（无可行方案）

```json
{
  "id": 2,
  "created_at": "2026-09-24T08:01:00.000000+00:00",
  "capacities": {"A": 5, "B": 0, "C": 0},
  "feasible": false,
  "imbalance": null,
  "loads": null,
  "message": "无可行分配：不存在所有设备均接入且各相不超容量的方案",
  "assignments": []
}
```

## 容量扩容建议

对于返回 `feasible: false` 的计算记录，可调用扩容接口。服务**保持设备功率与允许
相位不变**，计算 A/B/C 各相需要增加的**非负整数容量**以及扩容后的设备分配，
使所有设备都能接入。优化目标依次为：

1. **三相扩容总量最小**（`total_upgrade`）；
2. 总量相同时，**单相最大扩容量最小**（`max_upgrade`）；
3. 仍相同时，取按设备编号排序的相位序列**字典序最小**的方案。

接口只返回建议，**原计算记录不做任何修改**。记录不存在返回 `404`；
原记录已有可行分配（无需扩容）返回 `409`。

### 请求示例

```bash
# 先创建一条无可行分配的记录
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 5, "B": 0, "C": 0},
    "devices": [
      {"id": "BIG", "power": 50, "allowed_phases": ["A", "B", "C"]},
      {"id": "FIX", "power": 7, "allowed_phases": ["A"]}
    ]
  }'

# 再对该记录请求扩容建议（假设记录 id 为 2）
curl -X POST http://localhost:8000/api/v1/calculations/2/capacity-upgrade
```

### 响应示例

```json
{
  "id": 2,
  "created_at": "2026-09-24T08:01:00.000000+00:00",
  "original_capacities": {"A": 5, "B": 0, "C": 0},
  "capacity_upgrades": {"A": 2, "B": 50, "C": 0},
  "suggested_capacities": {"A": 7, "B": 50, "C": 0},
  "total_upgrade": 52,
  "max_upgrade": 50,
  "assignments": [
    {"device_id": "BIG", "phase": "B"},
    {"device_id": "FIX", "phase": "A"}
  ],
  "loads": {"A": 7, "B": 50, "C": 0},
  "message": "扩容建议：已在最小化三相扩容总量、再最小化单相最大扩容量的目标下，找到使所有设备均接入的容量与分配方案"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `original_capacities` | 原各相容量 |
| `capacity_upgrades` | 各相需要增加的非负整数容量 |
| `suggested_capacities` | 建议容量 = 原容量 + 扩容量 |
| `total_upgrade` / `max_upgrade` | 三相扩容总量 / 单相最大扩容量 |
| `assignments` | 建议容量下的设备相位分配（按设备编号排序） |
| `loads` | 该分配下各相的实际负载（均不超过建议容量） |

### 输入校验规则

- 设备数量 **1 ~ 12** 台，设备编号**唯一**且非空；
- 功率为**正整数**；
- `allowed_phases` 为**非空**数组，元素取自 `A` / `B` / `C`（自动去重）；
- 各相容量为**非负整数**。

不满足规则时返回 `422` 及具体错误信息。

## 稳健分相预演

`POST /api/v1/robust-rehearsals` 用于设备功率尚未确定、只能给出波动区间时的
事前推演。每台设备的功率为正整数区间 `[min_power, max_power]`（**下界不大于
上界**），各设备功率在区间内**独立变化**。接口寻找每台设备接入一个允许相位、
且**任意功率组合下各相均不超容量**的分配（稳健可行），并在稳健可行方案中：

1. **最小化**任意功率组合可能产生的「三相最大负载与最小负载之差」
   （`worst_imbalance`）；
2. 同值时取按设备编号排序的相位序列**字典序最小**的方案。

接口**只做预演，不写入历史计算记录**；无稳健可行方案时返回
`feasible: false` 及说明信息。

### 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/robust-rehearsals \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 100, "B": 100, "C": 100},
    "devices": [
      {"id": "D1", "min_power": 5,  "max_power": 10, "allowed_phases": ["A", "B", "C"]},
      {"id": "D2", "min_power": 15, "max_power": 20, "allowed_phases": ["A", "B", "C"]},
      {"id": "D3", "min_power": 25, "max_power": 30, "allowed_phases": ["B", "C"]}
    ]
  }'
```

### 响应示例（稳健可行）

```json
{
  "capacities": {"A": 100, "B": 100, "C": 100},
  "feasible": true,
  "worst_imbalance": 25,
  "load_lower_bounds": {"A": 5, "B": 15, "C": 25},
  "load_upper_bounds": {"A": 10, "B": 20, "C": 30},
  "assignments": [
    {"device_id": "D1", "phase": "A"},
    {"device_id": "D2", "phase": "B"},
    {"device_id": "D3", "phase": "C"}
  ],
  "message": "预演成功：已找到任意功率组合下各相均不超容量、且最坏负载差最小的稳健分配方案"
}
```

### 响应示例（无稳健可行方案）

```json
{
  "capacities": {"A": 40, "B": 40, "C": 40},
  "feasible": false,
  "worst_imbalance": null,
  "load_lower_bounds": null,
  "load_upper_bounds": null,
  "assignments": [],
  "message": "无稳健可行分配：不存在任意功率组合下各相均不超容量的接入方案"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `feasible` | 是否存在稳健可行分配 |
| `worst_imbalance` | 任意功率组合下「三相最大负载 - 最小负载」的最大值 |
| `load_lower_bounds` | 各相负载下界（该相设备均取功率下界之和） |
| `load_upper_bounds` | 各相负载上界（该相设备均取功率上界之和，均不超过容量） |
| `assignments` | 稳健分配方案（按设备编号排序）；无解时为空数组 |

### 输入校验规则

- 设备数量 **1 ~ 12** 台，设备编号**唯一**且非空；
- `min_power` / `max_power` 均为**正整数**，且 `min_power` **不大于** `max_power`；
- `allowed_phases` 为**非空**数组，元素取自 `A` / `B` / `C`（自动去重）；
- 各相容量为**非负整数**。

不满足规则时返回 `422` 及具体错误信息。

## 启动顺序预演

`POST /api/v1/calculations/{id}/startup-plan` 用于在已得到可行分配后，预演设备
**一台接一台启动**的最优顺序。接口**沿用记录中的固定相位（每台设备的接入相位）
与容量**，请求中逐台给出**启动瞬时功率**：

- 启动瞬时功率为**正整数，且不低于该设备的额定功率**；
- 请求中的**设备编号须与记录完全一致**（不多、不少、不重复）。

启动过程：设备一次启动一台；**启动瞬间**该设备按启动瞬时功率计，此前已启动的设备
按额定功率持续运行；**启动完成后**该设备亦按额定功率持续运行。每个启动瞬间及每个
稳定状态下，A/B/C 各相负载均不得超过容量。接口在所有安全启动顺序中：

1. **最小化全过程**「三相负载最大值与最小值之差」的**峰值**
   （`peak_imbalance`，取所有启动瞬间与稳定状态的最大负载差）；
2. 峰值相同时，取**设备编号序列字典序最小**的启动顺序。

接口**只做预演，不修改历史计算记录**；不存在任何安全启动顺序时返回
`feasible: false` 及说明信息。记录不存在返回 `404`；原记录无可行分配返回 `409`；
启动功率低于额定功率或设备编号与记录不一致返回 `422`。

### 请求示例

```bash
# 先创建一条可行计算记录（设备 D1/D2/D3 分别固定接 A/B/C）
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 10, "B": 10, "C": 10},
    "devices": [
      {"id": "D1", "power": 6, "allowed_phases": ["A"]},
      {"id": "D2", "power": 4, "allowed_phases": ["B"]},
      {"id": "D3", "power": 5, "allowed_phases": ["C"]}
    ]
  }'

# 再对该记录请求启动顺序预演（假设记录 id 为 1）
curl -X POST http://localhost:8000/api/v1/calculations/1/startup-plan \
  -H "Content-Type: application/json" \
  -d '{
    "startup_powers": [
      {"device_id": "D1", "startup_power": 8},
      {"device_id": "D2", "startup_power": 6},
      {"device_id": "D3", "startup_power": 9}
    ]
  }'
```

### 响应示例（存在安全启动顺序）

```json
{
  "id": 1,
  "created_at": "2026-09-25T08:00:00.000000+00:00",
  "capacities": {"A": 10, "B": 10, "C": 10},
  "feasible": true,
  "peak_imbalance": 8,
  "startup_order": ["D1", "D2", "D3"],
  "steps": [
    {
      "device_id": "D1",
      "phase": "A",
      "rated_power": 6,
      "startup_power": 8,
      "transient_loads": {"A": 8, "B": 0, "C": 0},
      "transient_imbalance": 8,
      "steady_loads": {"A": 6, "B": 0, "C": 0},
      "steady_imbalance": 6
    },
    {
      "device_id": "D2",
      "phase": "B",
      "rated_power": 4,
      "startup_power": 6,
      "transient_loads": {"A": 6, "B": 6, "C": 0},
      "transient_imbalance": 6,
      "steady_loads": {"A": 6, "B": 4, "C": 0},
      "steady_imbalance": 6
    },
    {
      "device_id": "D3",
      "phase": "C",
      "rated_power": 5,
      "startup_power": 9,
      "transient_loads": {"A": 6, "B": 4, "C": 9},
      "transient_imbalance": 5,
      "steady_loads": {"A": 6, "B": 4, "C": 5},
      "steady_imbalance": 2
    }
  ],
  "message": "预演成功：已找到全过程三相负载最大值与最小值之差峰值最小的安全启动顺序"
}
```

### 响应示例（无安全启动顺序）

某设备启动功率 11，但其固定接入的 A 相容量仅为 10（额定功率 10 下记录本可行），
任何顺序都无法安全启动该设备：

```json
{
  "id": 2,
  "created_at": "2026-09-25T08:01:00.000000+00:00",
  "capacities": {"A": 10, "B": 0, "C": 0},
  "feasible": false,
  "peak_imbalance": null,
  "startup_order": [],
  "steps": [],
  "message": "无安全启动顺序：不存在每个启动瞬间及稳定状态各相均不超容量的启动顺序"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `peak_imbalance` | 全过程负载差峰值：所有启动瞬间与稳定状态的「最大相负载 − 最小相负载」的最大值 |
| `startup_order` | 最优启动顺序（设备编号）；无安全顺序时为空数组 |
| `steps[].phase` | 该设备沿用自计算记录的固定接入相位 |
| `steps[].transient_loads` | 该设备**启动瞬间**各相负载（该设备按启动功率计） |
| `steps[].transient_imbalance` | 启动瞬间三相负载最大值与最小值之差 |
| `steps[].steady_loads` | 启动**完成后**各相稳定负载（该设备转入额定功率） |
| `steps[].steady_imbalance` | 稳定状态三相负载最大值与最小值之差 |

### 输入校验规则

- `startup_powers` 为**非空**数组，设备编号**唯一**且与记录**完全一致**
  （缺失、多余均返回 `422`）；
- 每台设备的 `startup_power` 为**正整数**，且**不低于其额定功率**；
- 记录不存在返回 `404`；原记录 `feasible=false` 返回 `409`。

## 单相故障保供预演

`POST /api/v1/calculations/{id}/phase-fault-rehearsal?fault_phase=A` 用于在某一相
发生故障时预演应急保供方案。故障相通过查询参数 `fault_phase` 指定（取值 `A`/`B`/`C`）：

- **健康相上的设备保持原分配不动**（不迁移、不参与重新优化）；
- **故障相设备**只能迁至其**允许接入的健康相**，迁入后该相负载**不得超过容量**，
  也可以**断开**；故障相最终负载**必为零**。

在所有满足约束的应急方案中，目标依次为：

1. **最大化保留设备的总功率**（`retained_power`，含健康相上保持原分配的设备）；
2. 总功率相同时，**最大化保留设备数**（`retained_count`）；
3. 仍相同时，按**故障相设备编号排序**，取其**相位选择序列字典序最小**者，
   单项次序为 **A＜B＜C＜断开**（只在允许的健康相中选择）。

全部故障相设备断开总是可行，因此该接口不返回 `feasible=false`。接口**只做预演，
不修改历史计算记录**。记录不存在返回 `404`；原记录无可行分配（`feasible=false`）
返回 `409`；`fault_phase` 缺失或取值非法返回 `422`。

### 请求示例

```bash
# 先创建一条可行计算记录（F1/F2 接 A，H1 接 B）
curl -X POST http://localhost:8000/api/v1/calculations \
  -H "Content-Type: application/json" \
  -d '{
    "capacities": {"A": 20, "B": 12, "C": 12},
    "devices": [
      {"id": "H1", "power": 7, "allowed_phases": ["B"]},
      {"id": "F1", "power": 5, "allowed_phases": ["A", "B"]},
      {"id": "F2", "power": 5, "allowed_phases": ["A", "B"]}
    ]
  }'

# 再对该记录预演 A 相故障（假设记录 id 为 1）
curl -X POST "http://localhost:8000/api/v1/calculations/1/phase-fault-rehearsal?fault_phase=A"
```

原记录负载为 `{A: 10, B: 7, C: 0}`。A 相故障后，B 相仅剩 `12 - 7 = 5` 空闲，
两台功率均为 5 的故障相设备最多迁一台至 B（均不允许 C）；保留总功率与设备数
相同，故取相位选择序列字典序最小者——编号较小的 `F1` 迁 B，`F2` 断开。

### 响应示例

```json
{
  "id": 1,
  "created_at": "2026-09-25T08:00:00.000000+00:00",
  "fault_phase": "A",
  "capacities": {"A": 20, "B": 12, "C": 12},
  "original_loads": {"A": 10, "B": 7, "C": 0},
  "loads": {"A": 0, "B": 12, "C": 0},
  "migrations": [
    {"device_id": "F1", "power": 5, "from_phase": "A", "to_phase": "B"}
  ],
  "disconnected": [
    {"device_id": "F2", "power": 5, "phase": "A"}
  ],
  "retained_power": 12,
  "retained_count": 2,
  "disconnected_power": 5,
  "disconnected_count": 1,
  "message": "预演成功：健康相设备保持原分配，故障相设备在允许健康相不超容量的前提下，已找到保留总功率最大、再保留设备数最多，且相位选择序列字典序最小的保供方案"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `fault_phase` | 故障相（A/B/C） |
| `original_loads` | 故障前各相负载（原记录的分配负载，含故障相设备） |
| `loads` | 预演后各相负载（健康相不动设备 + 迁入设备；故障相恒为 `0`，各相不超容量） |
| `migrations` | 迁至健康相的故障相设备（按设备编号排序），含功率、原相位与迁入相位 |
| `disconnected` | 被断开的故障相设备（按设备编号排序），含功率与原相位 |
| `retained_power` / `retained_count` | 保留设备（健康相不动设备 + 迁移设备）的总功率 / 数量 |
| `disconnected_power` / `disconnected_count` | 断开设备的总功率 / 数量 |

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 服务地址 http://127.0.0.1:8000
```

运行测试：

```bash
pytest                               # 全部测试（求解器 + API）
python3 -m unittest tests.test_solver  # 仅求解器测试（无第三方依赖）
```

## 项目结构

```
├── app/
│   ├── main.py       # FastAPI 应用与路由
│   ├── schemas.py    # 请求 / 响应数据模型与校验
│   ├── solver.py     # 三相排布求解器（DFS + 下界剪枝，纯标准库）
│   └── db.py         # SQLite 持久化（启动时自动建表）
├── tests/            # 求解器单元测试（含随机对拍）与 API 集成测试
├── requirements.txt  # 运行依赖
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 求解算法说明

按设备编号排序后做深度优先搜索，相位按 A→B→C 顺序展开，因此完整方案按
「设备编号排序后的相位序列」字典序依次产生；仅在负载差值**严格更小**时更新最优解，
同差值时自然保留字典序最小方案。配合容量约束剪枝与负载差值下界剪枝，
12 台设备的最坏情形亦可在秒级内完成求解。

**扩容建议**（`capacity-upgrade`）同样按设备编号排序、相位按 A→B→C 展开做 DFS，
分两遍搜索：第一遍求三相扩容总量的最小值，第二遍在总量固定最小的方案中以
`(单相最大扩容量, 相位序列)` 为键选出最优（即先最小化单相最大扩容，再取字典序
最小）。搜索中利用「只允许单一相位的设备造成的被迫超载 + 自由设备功率扣除各相
空闲容量」构造扩容总量下界进行剪枝，12 台设备的最坏情形亦可在数秒内完成。

**稳健分相预演**（`robust-rehearsals`）利用如下归约：对固定分配，各相负载可
独立取遍「功率下界之和 ~ 功率上界之和」内的所有整数，因此任意功率组合下各相
均不超容量 ⟺ 各相负载**上界**不超容量；而最坏负载差
`max(max_p L_p - min_p L_p) = max_{p≠q}(上界_p - 下界_q)`（注意并非「上界最大值
- 下界最小值」：同一相不可能同时取到上界与下界，故只取相异相位对）。据此在与
主求解器相同的 DFS 框架（按设备编号排序、相位按 A→B→C 展开、仅严格更优才更新、
容量约束与下界剪枝）中直接求解，12 台设备的最坏情形亦可在数秒内完成。

**启动顺序预演**（`startup-plan`）利用「顺序即子集路径」的结构：任一启动顺序对应
子集 DAG 上 空集→全集 的一条路径——途经节点为已启动设备集合（其稳定状态负载只
取决于该集合），途经边为一次启动（瞬时负载只取决于启动前集合与该设备）。节点与
边的负载差均与到达路径无关，故「最小化全过程负载差峰值」等价于该 DAG 上的
**minimax 路径**：按子集递推各集合的最小可达峰值即可（设备不超过 12 台，状态数
`2^12 = 4096`，瞬时完成）；不存在满足容量的路径时即为无安全顺序。得到最优峰值后，
先自全集反向递推「途经负载差均不超最优峰值、且满足容量时可达全集」的集合，再从
空集出发每一步选取「后缀仍可完成」的编号最小设备，所得顺序即同峰值下设备编号
序列字典序最小者。

**单相故障保供预演**（`phase-fault-rehearsal`）只需对故障相设备（至多 12 台）做
DFS：健康相设备保持原分配，其负载预先计入健康相；每台故障相设备的可选动作按
「允许接入的健康相 A→B→C，最后断开」展开，故完整方案天然按「设备编号排序后的
相位选择序列」字典序（A＜B＜C＜断开）依次产生。目标键为 `(保留功率, 保留设备数)`，
均只统计故障相设备中被保留的部分（健康相部分为常量），仅在严格更优时更新最优解，
同值自然保留字典序最小方案。配合「剩余设备全部保留也无法超过当前最优」的功率 /
数量乐观上界剪枝，12 台设备的最坏情形亦可瞬时完成；全部断开恒可行，故障相负载
恒为零。
