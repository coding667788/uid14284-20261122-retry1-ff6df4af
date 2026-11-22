# 企业账号离线恢复控制器

原认证器丢失后，基于**分属不同组织/保管域的合成证明**完成高风险账号恢复的离线
演练控制器。纯 Python 3.12 标准库实现，状态落在单个 SQLite 文件中，stdout 输出
字节级可重现的 UTF-8 JSON，夹具中不包含任何真实密钥。

---

## 1. 它解决什么问题

* 账号经历**身份世代（generation）**切换，每次成功恢复推进一个世代。
* 每位受托人（trustee）有明确的组织、保管域和有效期；其签发的证明继承这些属性。
* 恢复必须从**预先批准的配方（recipe）**中满足**任意一份**；配方同时规定：
  * 各证明类型需要的数量；
  * 选中的证明是否必须分属不同组织（`distinct_organizations`）；
  * 是否必须分属不同保管域（`distinct_custody_domains`）。
* 同一证明只能被选一次，并“粘”在账号、恢复申请、身份世代、配方版本和所选新
  认证器摘要上。
* `simulate_loss` 在**尚未申请恢复**时离线计算：要在“无故障”和“任意单个保管域
  失效”的每种情形下都能恢复，最少需要预先准备哪些合成证明。

---

## 2. 目录结构

```
solution/
  __init__.py
  __main__.py        # python -m solution 入口
  runner.py          # 隔离模式（-I）下的实体入口，功能与 -m 相同
  cli.py             # 命令行、参数解析、退出码与 JSON 信封
  errors.py          # 错误码：E_VALIDATION / E_STATE / E_IDEMPOTENCY_CONFLICT ...
  validation.py      # 输入结构校验，错误带 JSONPath 风格字段位置
  jsonio.py          # 确定性 JSON（ensure_ascii=False, sort_keys, 单换行）
  selection.py       # 配方可行性、最小/最便宜证明集选择
  db.py              # SQLite schema、连接与显式事务（含提交前崩溃模拟）
  engine.py          # 事件状态机：排序、预留、冻结、异议、取消、激活、幂等
  simulate.py        # simulate_loss 分支限界求解器
tests/
  reference_model.py # 独立朴素枚举小模型（不依赖生产业务模块）
  common.py          # 测试工具
  test_*.py          # 46 个断言测试
samples/
  01..06_*.json      # 可直接运行的虚构样例
  run_demo.sh / .bat # 一键演示
```

---

## 3. 运行环境与复现步骤

* Windows 原生 Python 3.12（开发于 3.12.8），**仅标准库**，CPU 即可，无网络依赖。

```bash
# 全量断言测试
python -m unittest discover -s tests -v

# 一键演示（Git Bash）
bash samples/run_demo.sh

# 一键演示（Windows CMD / PowerShell）
samples\run_demo.bat
```

> 处理不受信任的外部输入时，建议用隔离模式运行，防止输入目录里被植入的
> `json.py` 等模块被解释器加载：`python -I solution/runner.py <命令> CASE.json`。
> 测试与演示脚本均以 `-I` 启动子进程。

### 命令行形态

```
python -m solution <命令> CASE.json [--db DB.sqlite] [--crash-before-commit]
```

* 状态类命令（除 `simulate_loss` 外的全部命令）必须提供 `--db`。
* `simulate_loss` 是纯计算，不需要 `--db`；给了 `--db` 时会额外校验该账号
  尚未发起任何恢复申请。
* `CASE.json` 要么是单事件对象，要么是 `{"events": [...]}` 批次（`ingest` 专用）。
* `--crash-before-commit` 在事务 `COMMIT` 前主动回滚并退出，用于演练“进程提交前
  退出”。

### 退出码

| 码 | 含义 |
|----|------|
| 0  | 成功 |
| 1  | 业务状态错误 / 幂等冲突 / `simulate_loss` 无解 |
| 2  | 输入结构错误（`E_VALIDATION` / `E_JSON`） |
| 3  | 用法错误（缺 `--db`、CLI 命令与用例 `cmd` 不一致等，`E_USAGE`） |
| 4  | `--crash-before-commit` 触发的模拟崩溃（`E_SIMULATED_CRASH`） |

stdout 始终是一行确定性 UTF-8 JSON，成功形如 `{"ok":true,...}`，失败形如
`{"ok":false,"error":{"code":...,"message":...,"field":...}}`。

---

## 4. 数据模型与命令接口

所有写命令都需要 `request_id`（幂等键）；事件公共字段为
`cmd / at（逻辑时刻）/ seq（批内序号，>=1）/ request_id / payload`。

### 4.1 账号与受托人

| 命令 | payload 关键字段 | 说明 |
|------|------------------|------|
| `register_account` | `account_id, initial_authenticator, security_contact` | 建立世代 1 |
| `register_trustee` | `trustee_id, organization, custody_domain, valid_from, valid_until` | `valid_until > valid_from` |
| `update_trustee` | 同上 | 新版本整体替换旧登记；历史版本不再回退使用 |

### 4.2 证明与配方

| 命令 | payload 关键字段 | 说明 |
|------|------------------|------|
| `issue_proof` | `proof_id, account_id, trustee_id, proof_type, digest, issued_at, valid_until, cost` | 签发时刻受托人必须处于有效保管期 |
| `revoke_proof` | `proof_id, revoked_at` | 逻辑撤销，保留记录 |
| `register_recipe` | `recipe_id, version, active_from, requirements[]` | 配方按 `(recipe_id, version)` 版本化 |

`requirements[]` 每项：

```json
{
  "proof_type": "hardware-key-attestation",
  "count": 2,
  "distinct_organizations": true,
  "distinct_custody_domains": true
}
```

同一配方内 `proof_type` 不可重复（数量合并到 `count`）。两个独立标志可任意组合；
当只要求组织独立时，**同一组织即便跨了保管域也不算独立**。

### 4.3 恢复申请生命周期

| 命令 | payload | 语义 |
|------|---------|------|
| `reserve` | `account_id, generation, new_authenticator_digest, freeze_until, recipe_id?(可选), recipe_version?(与 recipe_id 同时出现)` | 选证明、建申请、锁定预留，状态 `FROZEN` |
| `challenge` | `app_request_id, challenger` | `challenger ∈ {old_authenticator, security_contact}`，状态转 `CHALLENGED` |
| `cancel` | `app_request_id, reason?` | 状态转 `CANCELLED`，留下永久终止回执 |
| `activate` | `app_request_id` | 单事务复核并推进世代，状态转 `ACTIVATED` |
| `get_receipt` | `request_id` | 只读已稳定的回执 |

申请状态机：

```
FROZEN ──challenge──▶ CHALLENGED（终态，不可激活，可继续追加异议）
FROZEN ──cancel─────▶ CANCELLED（终态，永久回执）
FROZEN ──activate───▶ ACTIVATED（终态，推进世代）
同世代其它 FROZEN 申请在激活时原子转 SUPERSEDED
```

---

## 5. 核心语义规则（与需求逐条对应）

### 5.1 事件排序与同截止异议优先

* 先按逻辑时刻 `at` 分组；默认同组按输入 `seq` 升序。
* **唯一例外**：旧认证器发起的异议，若目标申请仍处于有效世代
  （账号当前世代 == 申请世代）且事件时刻**恰好等于** `freeze_until`，则该异议在
  同一时刻批次内排到所有其它事件之前，无视其 `seq`。
* 世代已失效的旧认证器异议既不享受优先，也会被业务拒绝。

### 5.2 reserve 的选择与决胜

候选证明必须：属于本账号、未被任何预留冻结、在申请时刻仍有效（未撤销、未过期、
受托人在有效保管期内）。选择规则：

1. 在所有 `active_from <= at` 的配方（或 `recipe_id/version` 指定的单一配方）中
   找可行配方；
2. 先使**证明条数最少**，平手再按**升序 proof_id 列表**字典序决胜。

### 5.3 冻结窗口与证明粘性

* 预留证明从冻结起即写入 `reservations`，**只能被该次预留使用**；申请取消或从未
  激活也不释放，后续预留永远选不到它。
* 冻结开始后只能用原配方与“当前仍有效的同一组证明”完成；证明在窗口内被撤销/
  过期/因受托人变更而失效时，`activate` 会带 `reason` 拒绝，必须重新 `reserve`
  并走完整冻结窗口。
* 激活回执中的 `proof_fingerprints` 固化每条证明的类型、摘要、组织、保管域，
  与账号、世代、配方版本、新认证器摘要绑定。

### 5.4 单事务 activate 复核

`activate` 在**同一个 SQLite 事务**里依次复核，任一失败即整体回滚：

1. 不存在已登记异议；
2. 申请仍为 `FROZEN`；
3. 账号当前世代 == 申请世代；
4. 当前时刻严格晚于 `freeze_until`；
5. 每条预留证明仍有效、属于本账号且绑定到本申请预留。

通过后提交：新身份世代、旧世代标记 superseded、同世代其余在途申请原子取消
（`SUPERSEDED`）、证明消费（撤销时间戳）、稳定回执。旧认证器与未被选中的新
认证器都不会成为新世代的有效凭证。

### 5.5 幂等

* 内容指纹 = 去掉排序游标 `seq` 后对事件做确定性 JSON（因此重放时 `seq` 可不同）。
* 相同 `request_id` + 相同内容：只读返回原回执，不重复执行。
* 相同 `request_id` + 不同内容：抛 `E_IDEMPOTENCY_CONFLICT`，**整批回滚**，
  批次里其它正常事件也不落库。

---

## 6. simulate_loss 离线演练

### 6.1 输入

```json
{
  "account_id": "acct-drill-02",
  "drill_time": 500,
  "custody_domains": ["vault-dallas", "vault-oslo"],
  "candidates": [
    {"proof_id": "future-01", "organization": "org-alpha",
     "custody_domain": "vault-dallas", "proof_type": "hardware-key-attestation",
     "cost": 120, "valid_from": 0, "valid_until": 100000}
  ],
  "recipes": [ {"recipe_id": "...", "version": 1, "requirements": [...]} ]
}
```

* 候选数最多 26、保管域最多 8（超出报 `E_VALIDATION`）；`proof_id` 唯一。
* `valid_from/valid_until` 可省略，默认 `[0, +∞)`；演练时刻不在有效期内的候选
  直接剔除。
* 情形集合固定为：无故障 `S0`，外加每个保管域一个失效情形 `S:<domain>`
  （域按字典序，保证“第一个失败情形”可重现）。

### 6.2 输出（有解）

* `prepared_proof_ids`：**一份预先固定**的准备清单；每种失效情形允许选不同配方，
  但清单本身不变。
* 决胜顺序：**总费用 → 准备条数 → 升序 ID 列表**。
* `scenario_plans[]`：每个情形实际选用的配方、证明与费用。
* `reuse.shared_proofs`：被多个情形配方共同需要的证明，准备一次即可跨情形复用。
* `search_stats`：候选数、收缩后的搜索宇宙、DFS 节点数与剪枝计数。

### 6.3 输出（无解）

只要有一个情形**连全部候选都无法恢复**，立即停止搜索并返回
`E_NO_RECOVERY_PLAN`（退出码 1），载荷含：

* `first_failed_scenario`：第一个失败情形（`S0` 或 `S:<domain>`）；
* `recipe_failures[].missing_requirements[]`：逐配方的缺失要求与阻塞原因
  （`insufficient_count` / `distinct_organizations_unsatisfied` /
  `distinct_custody_domains_unsatisfied` /
  `combined_independence_unsatisfied`）。

### 6.4 算法与“配方可行性如何跨情形复用”

1. **全候选可行性闸门**：对每个情形用全部候选跑一次最便宜证人；任一情形失败即
   输出无解诊断，不进入搜索。
2. **宇宙收缩**：任何最优清单都不会包含“没有任何情形的全候选证⼈⽤到”的证明，
   因此搜索宇宙 = 各情形全候选证人的并集。
3. **初始上界**：各情形最便宜证人的并集（天然可行）。
4. **包含/排除 DFS + 分支限界**：
   * 费用/条数剪枝：当前费用已超过上界，或费用持平而条数不少于上界，即剪枝；
   * 可行性剪枝：`当前选择 ∪ 剩余候选` 若已无法覆盖全部情形，即剪枝；
   * 每个节点对各情形复用同一套 `selection` 可行性判定，证明在情形间的复用通过
     `shared_proofs` 显式汇报。
5. 26 候选 / 8 域规模下，实测 DFS 节点数为两位数、毫秒级返回
   （见 `tests/test_simulate.py::test_26_candidates_8_domains_runs_fast`）。

---

## 7. 错误码与字段位置

结构错误统一为 `E_VALIDATION`，`field` 是 JSONPath 风格定位，例如：

```json
{"ok": false, "error": {
  "code": "E_VALIDATION",
  "message": "字段 events[0].payload.initial_authenticator 必须是字符串，实际为 integer",
  "field": "events[0].payload.initial_authenticator"}}
```

| 错误码 | 触发场景 |
|--------|----------|
| `E_VALIDATION` | 类型错误、缺字段、值域非法、候选/域超上限等 |
| `E_JSON` | 用例文件不是合法 JSON（定位到行列） |
| `E_STATE` | 结构合法但逻辑状态不允许（世代不符、窗口未结束、证明失效、账号已申请恢复等） |
| `E_IDEMPOTENCY_CONFLICT` | 同 `request_id` 异内容，整批失败 |
| `E_NO_RECOVERY_PLAN` | simulate_loss 无可行准备清单 |
| `E_USAGE` | 命令行用法错误 |
| `E_SIMULATED_CRASH` | 提交前崩溃模拟，事务已回滚 |
| `E_IO` | 输入文件不存在 |

---

## 8. 虚构样例

| 文件 | 演示内容 |
|------|----------|
| `samples/01_bootstrap.json` | 账号、3 位跨组织/跨域受托人、3 条证明、双控配方 |
| `samples/02_reserve_challenge_cancel.json` | 预留 → 取消的永久终止回执，预留证明锁定 |
| `samples/03_reserve_activate.json` | 完整冻结窗口后激活，世代 1 → 2，含证明指纹 |
| `samples/04_cutoff_challenge_priority.json` | 截止点同刻：旧认证器异议优先于 seq=1 的撤销事件 |
| `samples/05_simulate_loss.json` | 最小准备清单、各情形配方切换、跨情形复用 |
| `samples/06_simulate_no_plan.json` | 无解：同组织跨域不独立，返回失败情形与缺失要求 |

所有标识、摘要、联系人均为虚构字符串（如 `sha256:2222...`），不含真实密钥。

---

## 9. 测试策略与独立小模型

`python -m unittest discover -s tests -v` 共 46 个断言测试，关键覆盖：

* **同组织跨保管域但不独立**（`test_same_organization_cross_domain_is_not_independent`）；
* **最低成本配方失效而备用配方可用**（`test_cheapest_recipe_fails_backup_recipe_works`）；
* **准备清单选用两种配方共同需要的证明**（`test_prepared_list_uses_proof_shared_by_two_recipes`）；
* **两个恢复申请并发争用**，一次成功后另一个原子 `SUPERSEDED`
  （`test_parallel_apps_one_generation_single_success_supersedes_rest`）；
* **同截止异议优先**与非截止事件严格按 `seq`（`test_freeze_challenge.py`）；
* **进程提交前退出**（事务回滚、无部分状态、可干净重试）与**提交后响应丢失**
  （同请求重放只读原回执）（`test_crash.py`）；
* 幂等同内容重放 / 异内容整批失败、错误码与字段位置、CLI 退出码、确定性字节输出。

`tests/reference_model.py` 是**刻意用不同写法实现的独立小模型**：用
`itertools` 朴素枚举全部证明组合与全部准备清单子集，独立回放两件事——单配方
证人最优解、simulate_loss 清单全局最优解——再与生产的 DFS/贪心实现交叉对账
（`SimulateReferenceCrossCheck`），防止两套代码犯同一个错误。
