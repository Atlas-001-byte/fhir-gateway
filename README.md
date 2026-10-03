# FHIR Gateway

医疗数据互操作网关：FHIR 资源校验、术语映射与审计留痕。

一次调用依次完成：**资源校验 → 术语映射 → 审计留痕**。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

- 仅使用 Python 3 标准库，无需安装第三方依赖。
- 不引入任何外部 FHIR/术语库或同类网关实现。

## 用法

```sh
fhir-gateway --audit-file PATH < request.json
```

- `--audit-file PATH`（必需）：审计 JSON Lines 文件；文件不存在则创建，已存在则保留旧行并追加。
- 请求 JSON 从标准输入读取。

## 请求格式

单资源：

```json
{
  "resource": { "resourceType": "Patient|Observation|Condition", "id": "...", "...": "..." },
  "term_maps": [
    {
      "system": "http://loinc.org",
      "code": "8867-4",
      "target_system": "http://snomed.info/sct",
      "target_code": "8499000"
    }
  ],
  "audit_context": {
    "request_id": "req-1",
    "actor": "dr-house",
    "recorded_at": "2026-10-03T10:00:00Z"
  }
}
```

FHIR R4 Bundle（请求契约不变，仅 `resource` 换成 Bundle）：

```json
{
  "resource": {
    "resourceType": "Bundle",
    "id": "bundle-1",
    "type": "collection",
    "entry": [
      { "resource": { "resourceType": "Patient", "id": "pat-1" } },
      { "resource": { "resourceType": "Observation", "id": "obs-1", "...": "..." } }
    ]
  },
  "term_maps": [],
  "audit_context": { "request_id": "req-2", "actor": "dr-house", "recorded_at": "..." }
}
```

`type` 为 `transaction` / `batch` 的 Bundle 走批量执行语义，每个 entry 需带 `request`：

```json
{
  "resource": {
    "resourceType": "Bundle",
    "id": "bundle-2",
    "type": "transaction",
    "entry": [
      {
        "request": { "method": "POST", "url": "Patient/pat-1" },
        "resource": { "resourceType": "Patient", "id": "pat-1" }
      },
      {
        "request": { "method": "PUT", "url": "Observation/obs-1" },
        "resource": { "resourceType": "Observation", "id": "obs-1", "...": "..." }
      }
    ]
  },
  "term_maps": [],
  "audit_context": { "request_id": "req-3", "actor": "dr-house", "recorded_at": "..." }
}
```

## 校验规则

校验顺序：请求结构 → `audit_context` → 资源（Bundle → 各 `entry.resource`）→ `term_maps` → 同源冲突。

- `audit_context.request_id`、`actor`、`recorded_at` 必须为非空字符串。
- 单资源 `resource.resourceType` 仅限 `Patient`、`Observation`、`Condition`，且 `id` 为非空字符串。
- `Observation.status` 必须符合 FHIR R4 ObservationStatus（`registered`、`preliminary`、`final`、`amended`、`cancelled`、`corrected`、`entered-in-error`、`unknown`）。
- `Observation.code` 与 `Condition.clinicalStatus` 的首个 `coding` 必须含非空 `system`、`code`。
- Bundle 规则（`type` 非 `transaction`/`batch` 时）：
  - `resourceType` 为 `Bundle`，`id` 为非空字符串。
  - `type` 仅限 `document`、`message`、`transaction`、`transaction-response`、`batch`、`batch-response`、`history`、`searchset`、`collection`。
  - `entry` 可省略或为空数组；非空时必须为对象数组，每项含 `resource`，其中资源沿用上面的单资源校验；未知字段原样保留。
- `transaction`/`batch` Bundle 规则（批量执行语义）：
  - Bundle 级：`id` 非空、`type` 合法、`entry`（如有）为数组；不满足仍按 `FhirValidationError` 整体报错。
  - entry 级（逐项判定，结果写入响应与审计，不作为整体异常）：每项必须为对象且含 `request`（对象）与 `resource`；`request.method` 仅支持 `POST`、`PUT`；`request.url` 首段（`/` 分隔）必须等于 `resource.resourceType`；随后对该资源执行既有校验与术语映射。不写业务资源库。
- `term_maps` 每项 `system`、`code`、`target_system`、`target_code` 必须为非空字符串。
- 同一源编码（`system` + `code`）映射到多个不同目标时报错；重复但目标相同允许。

## 成功输出

退出码 `0`，stdout 一行 JSON，字段顺序固定：

```json
{
  "status": "accepted",
  "resource_type": "Observation",
  "resource": { "...": "原样返回，不改写" },
  "mappings": [
    {
      "source": { "system": "http://loinc.org", "code": "8867-4" },
      "status": "mapped",
      "target": { "system": "http://snomed.info/sct", "code": "8499000" }
    },
    {
      "source": { "system": "http://x", "code": "y" },
      "status": "unmapped",
      "target": null
    }
  ],
  "audit": {
    "request_id": "req-1",
    "actor": "dr-house",
    "recorded_at": "2026-10-03T10:00:00Z",
    "decision": "accepted",
    "audit_id": "非空随机ID"
  }
}
```

- 资源内所有含非空 `system`/`code` 的编码对象（含嵌套结构）逐项生成映射结果；命中给目标，未命中 `status=unmapped` 且 `target=null`。Bundle 按 `entry` 顺序、每个资源按嵌套顺序收集，重复编码逐项列出、不合并。
- `resource` 原样返回，绝不改写；Bundle 成功时 `resource_type` 为 `Bundle`。
- 审计行（不含 `resource`、`mappings`）随后追加到 `--audit-file`，一行一条 JSON；每次提交仅追加一条审计。

## transaction / batch 批量执行输出

`type` 为 `transaction` / `batch` 的 Bundle 提交后，退出码为 `0`，stdout 一行 JSON，外层字段顺序仍为 `status`、`resource_type`、`resource`、`mappings`、`audit`，但：

- `status` 为整体状态码（整数）：`transaction` 全有或全无——任一 entry 失败整体 `400`（不返回部分成功），全部通过才 `200`；`batch` 各项独立，失败项不影响其余，整体恒 `200`。
- `resource` 为响应 Bundle：`type` 为 `transaction-response` / `batch-response`，`entry` 顺序与输入一致（重复 entry 按原位置逐项处理），每项含 `index`（从 0 开始）、`resourceType`、`status`（`200`/`400`）；失败项附 `outcome`（OperationOutcome）。
- OperationOutcome 的 `issue` 含 `severity`（`error`）、`code`、`entryIndex`（从 0 开始的 entry 索引）、`expression`（字段表达式，如 `Bundle.entry[1].request.url`）与 `diagnostics`。`code` 取值：
  - `invalid`：entry 缺少 `request`/`resource`、字段非法，或 `request.url` 首段与 `resource.resourceType` 不一致。
  - `not-supported`：`request.method` 不是 `POST`/`PUT`。
  - `processing`：既有资源校验或术语映射失败。
- `mappings` 为通过校验的各 entry 的术语映射结果（按 entry 顺序拼接）。
- 审计每次提交一条，含 `request_id`、`actor`、`recorded_at`、`decision`（整体 `200` 为 `accepted`，`400` 为 `rejected`）、`audit_id`（每次提交独立生成）、`bundle_type`、`entry_total`、`entry_succeeded`、`entry_failed`、`status`（最终状态）及 `entries`（逐项 `index`、`resource_type`、`stage`、`status`、`issues` 问题代码）。`stage` 为 `request` / `validation` / `mapping` / `completed`。审计不保存资源正文、患者姓名、标识或术语映射原文。

```json
{
  "status": 400,
  "resource_type": "Bundle",
  "resource": {
    "resourceType": "Bundle",
    "type": "transaction-response",
    "entry": [
      { "index": 0, "resourceType": "Patient", "status": 200 },
      {
        "index": 1,
        "resourceType": "Observation",
        "status": 400,
        "outcome": {
          "resourceType": "OperationOutcome",
          "issue": [
            {
              "severity": "error",
              "code": "invalid",
              "entryIndex": 1,
              "expression": ["Bundle.entry[1].request.url"],
              "diagnostics": "Bundle.entry[1].request.url 首段必须与 resource.resourceType 一致"
            }
          ]
        }
      }
    ]
  },
  "mappings": [],
  "audit": { "...": "见上文审计字段" }
}
```

> 注：整体 `400` 是批量执行的正常结果（响应与审计照常产生），不是网关错误；只有顶层 JSON 无法解析、请求结构或 `audit_context` 非法、term_maps 非法、审计写入失败等才走下文的错误输出。

## 错误输出

任意受控错误：**stdout 为空**，stderr 一行 JSON，退出码 `2`，且**不写审计文件**：

```json
{"error": {"type": "InputError", "message": "非空错误信息"}}
```

| 错误类型 | 触发场景 |
| --- | --- |
| `InputError` | 标准输入不是合法 JSON、请求结构非法、缺少字段、`audit_context` 非法 |
| `FhirValidationError` | 资源类型/id/status/编码不满足校验规则 |
| `TermMappingError` | 映射项字段非法，或同一源编码存在多个不同目标 |
| `AuditWriteError` | 缺少 `--audit-file`、路径为目录、父目录不存在、无权限或追加写入不完整；此时 stdout 为空 |

> 注：审计在 stdout 输出之前落盘，因此 `AuditWriteError` 发生时 stdout 保证为空。

## 测试

```sh
python3 test_fhir_gateway.py
```

## 状态

已实现 `fhir-gateway`：资源校验、术语映射、审计留痕端到端可用，支持单资源与 FHIR R4 Bundle；`transaction`/`batch` Bundle 支持批量执行语义（逐项校验与映射、整体 200/400、OperationOutcome、逐项审计），含 54 个端到端测试。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
