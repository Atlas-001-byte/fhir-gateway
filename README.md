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
fhir-gateway --audit-chain --audit-file PATH < request.json
fhir-gateway --check-references --audit-file PATH < request.json
fhir-gateway --verify-audit --audit-file PATH
```

- `--audit-file PATH`（必需）：审计 JSON Lines 文件；文件不存在则创建，已存在则保留旧行并追加。
- `--audit-chain`（可选）：为审计增加可离线复核的完整性链；不开启时行为完全不变。
- `--check-references`（可选）：开启本地引用完整性校验；不开启时解析、校验、映射、输出与审计行为完全不变。
- `--verify-audit`（可选）：只校验链式文件，不读取标准输入。
- 请求 JSON 从标准输入读取（校验模式除外）。

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

transaction / batch Bundle（批量执行语义，每个 entry 带 `request`）：

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
- Bundle 规则：
  - `resourceType` 为 `Bundle`，`id` 为非空字符串。
  - `type` 仅限 `document`、`message`、`transaction`、`transaction-response`、`batch`、`batch-response`、`history`、`searchset`、`collection`。
  - `entry` 可省略或为空数组；非空时必须为对象数组，每项含 `resource`，其中资源沿用上面的单资源校验；未知字段原样保留。
  - `type` 为 `transaction`/`batch` 时不走本条，entry 问题按批量执行语义进入响应（见下节）。
- `term_maps` 每项 `system`、`code`、`target_system`、`target_code` 必须为非空字符串。
- 同一源编码（`system` + `code`）映射到多个不同目标时报错；重复但目标相同允许。

## 本地引用完整性校验（--check-references）

`--check-references` 为可选开关：不开启时，资源中的 `reference` 字段不做任何检查，既有解析、校验、映射、stdout 字段顺序、审计与错误行为完全不变。开启后在**资源校验通过之后、`term_maps` 构建之前**递归检查资源 JSON 中所有键名为 `reference` 的字段（任意嵌套层级、数组内外均覆盖）。

引用形态与规则：

- `#id`（本地片段）：只能命中**同一顶层资源** `contained` 中 `id` 相同的条目；资源自身的顶层 `id` 与其他 Bundle entry 均不可作为目标；`#` 后不能为空。
- `ResourceType/id`（相对引用，恰好一个 `/` 且两段非空）：
  - 在**非 transaction、非 batch 的 Bundle** 中，必须命中 `entry.resource` 里 `resourceType` 与 `id` 都相同的资源；缺失本地目标报错；`resourceType`/`id` 任一不匹配均视为缺失。
  - 在**单资源**中视为外部引用，仅校验格式，目标无需存在。
- `http://` 或 `https://` 开头的完整 URL：外部引用，不检查目标是否存在。
- 其他形式（如 `urn:uuid:…`、缺少 `/`、多段路径等）拒绝。
- `reference` 只接受非空字符串：数字、布尔、`null`、数组、对象或空字符串一律拒绝；`Reference` 对象没有 `reference` 字段时照常接受。
- 非执行 Bundle 中若 `resourceType` 与 `id` 相同的资源在 `entry.resource` 中重复出现，指向它的相对引用按**目标不唯一**拒绝（即使其中一个位于 `contained` 也不改变 entry 索引统计；片段引用始终只在所属 entry 资源自身的 `contained` 内解析）。

不检查范围：`type` 为 `transaction`/`batch` 的 Bundle 完全旁路引用校验，逐项请求、校验、映射、审计、全有或全无与逐项独立结果均不变，也不检查可执行 Bundle 的跨 entry 引用。

校验失败属于 `FhirValidationError`：message 含引用值与唯一原因（缺少本地目标 / 目标不唯一 / 非法或形式不受支持），stdout 为空，stderr 为一行 JSON，退出码为 `2`，且不写审计文件。错误优先级不变：顶层结构与 `audit_context` 仍先报 `InputError`；资源自身校验仍先于引用校验；引用校验仍先于术语映射错误，因此资源或引用错误都会在审计写入前终止。

开启该选项不改变成功响应：仍只有 `status`、`resource_type`、`resource`、`mappings`、`audit`（可执行 Bundle 无 `mappings`），不新增字段；资源正文与引用值均不入审计。可与 `--audit-chain` 同时使用，链式规则不受影响。

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
- 审计行（不含 `resource`、`mappings`）随后追加到 `--audit-file`，一行一条 JSON；每个 Bundle 仅追加一条审计。

## transaction / batch 批量执行

`type` 为 `transaction` 或 `batch` 的 Bundle 走批量执行语义；单资源和其他类型的 Bundle 行为不变。调用方从同一入口提交，每个 entry 提供 `request.url`、`request.method` 和 `resource`。系统按 entry 顺序逐项执行既有的资源校验与术语映射，**不写业务资源库**。

entry 规则（按此顺序检查，命中首个问题即记录）：

- 缺少 `request` 或 `resource` → `invalid`。
- `request.url` 首段（`/` 分隔）必须等于 `resource.resourceType`，否则 → `invalid`。
- `request.method` 仅支持 `POST`、`PUT`，否则 → `not-supported`。
- 资源校验或术语映射失败 → `processing`。

成功输出一行 JSON（退出码 `0`），字段顺序固定：

```json
{
  "status": 200,
  "resource_type": "Bundle",
  "resource": {
    "resourceType": "Bundle",
    "id": "bundle-2",
    "type": "transaction-response",
    "entry": [
      { "resourceType": "Patient", "status": 200 },
      {
        "resourceType": "Observation",
        "status": 400,
        "outcome": {
          "resourceType": "OperationOutcome",
          "issue": [
            {
              "severity": "error",
              "code": "invalid",
              "entry": 1,
              "expression": "Bundle.entry[1].request.url",
              "diagnostics": "问题描述"
            }
          ]
        }
      }
    ]
  },
  "audit": { "...": "见下" }
}
```

- `resource` 为响应 Bundle：`type` 为 `transaction-response` 或 `batch-response`，`entry` 顺序与输入一致（重复 entry 按原位置处理），每项含 `resourceType`（取不到为 `null`）和 `status`（`200`/`400`），失败项附 `outcome`（OperationOutcome）。
- OperationOutcome 的 `issue` 含从 0 开始的 entry 索引（`entry`）和字段表达式（`expression`）；`code` 取值 `invalid`、`not-supported`、`processing`。
- **transaction 全有或全无**：任一 entry 失败整体 `status` 为 `400`，不返回部分成功的整体状态；全部通过才为 `200`。
- **batch 各项独立**：失败项 `400`，其余继续，整体始终为 `200`。
- 逐项失败不属于受控错误：退出码仍为 `0`，审计照常写入。`InputError`（顶层 JSON 无法解析、根节点非对象、请求结构或 `audit_context` 非法）等网关级错误行为不变。

审计记录（每次提交仅追加一条，重复提交生成独立 `audit_id`）：

```json
{
  "request_id": "req-3",
  "actor": "dr-house",
  "recorded_at": "...",
  "decision": "accepted",
  "audit_id": "非空随机ID",
  "bundle_type": "transaction",
  "entry_total": 2,
  "entry_succeeded": 1,
  "entry_failed": 1,
  "status": 400,
  "entries": [
    { "index": 0, "resource_type": "Patient", "phase": "completed", "status": 200, "issue_code": null },
    { "index": 1, "resource_type": "Observation", "phase": "request", "status": 400, "issue_code": "invalid" }
  ]
}
```

- `phase` 取值：`request`（结构/URL/method）、`validation`、`mapping`、`completed`。
- 审计不保存资源正文、患者姓名、标识或术语映射原文，逐项只记录索引、资源类型、阶段、结果状态和问题代码。
- 审计写入失败仍返回 `AuditWriteError` 且 stdout 为空。

## 审计完整性链（--audit-chain）

开启 `--audit-chain` 后，每次提交的审计行增加一个 `audit_digest` 字段，把历次审计串成一条可离线复核的 SHA-256 链。

- 不开启时行为完全不变：继续读取任意已有 JSON Lines，保留旧行后追加，审计对象不含 `audit_digest`。
- 开启后 **stdout 中的 `audit` 与落盘行一致**（均含 `audit_digest`）；既有字段与 transaction/batch 的批量摘要字段不变；资源正文与术语映射原文仍不入审计。
- 链式行以规范 JSON 落盘：对象键按 Unicode 码点升序、分隔符后无空格、非 ASCII 原样保留（UTF-8，不转义）。

`audit_digest` 为 SHA-256 的 64 位小写十六进制值，按 UTF-8 对以下内容计算：

```
上一条 audit_digest + "\n" + 本条移除 audit_digest 字段后的规范 JSON
```

首条的前序摘要为 64 个 `0`。因此任一行正文被篡改、行被重排或伪造，都会在复核时断链。

链式追加只接受三种目标文件：

1. 文件不存在（创建后从 genesis 开始）；
2. 文件为空（同上）；
3. 文件中**每一行都能通过链式校验**。

发现非链式行、摘要不匹配、重复 `audit_id`、缺字段（`audit_id`/`audit_digest`）或非对象行时返回 `AuditWriteError`，**不追加本条**，已有内容保持不变。

### 离线校验（--verify-audit）

```sh
fhir-gateway --verify-audit --audit-file PATH
```

只校验链式文件从首行到末行的结构、`audit_id` 唯一性与摘要连续性，**不读取标准输入**。成功时 stdout 输出一行 JSON（退出码 `0`），字段顺序固定：

```json
{"status": "valid", "line_count": 2, "last_audit_id": "...", "last_audit_digest": "..."}
```

- `status` 恒为 `"valid"`；`line_count` 为校验通过的行数。
- 空文件计数为 `0`，`last_audit_id` 与 `last_audit_digest` 为 `null`。
- 以下情形返回 `AuditVerificationError`：JSON 解析失败、行非对象、`audit_digest` 缺失或格式错误、`audit_id` 为空或重复、摘要链断裂。
- 文件缺失、路径为目录、父目录不存在或读取失败返回 `AuditReadError`。
- 两类校验错误均 **stdout 为空**、stderr 一行 JSON、退出码 `2`。

## 错误输出

任意受控错误：**stdout 为空**，stderr 一行 JSON，退出码 `2`，且**不写审计文件**：

```json
{"error": {"type": "InputError", "message": "非空错误信息"}}
```

| 错误类型 | 触发场景 |
| --- | --- |
| `InputError` | 标准输入不是合法 JSON、请求结构非法、缺少字段、`audit_context` 非法 |
| `FhirValidationError` | 资源类型/id/status/编码不满足校验规则；开启 `--check-references` 时引用缺少本地目标、目标重复、`reference` 非法或形式不受支持 |
| `TermMappingError` | 映射项字段非法，或同一源编码存在多个不同目标（transaction/batch 中降级为逐项 `processing`） |
| `AuditWriteError` | 缺少 `--audit-file`、路径为目录、父目录不存在、无权限或追加写入不完整；链式模式下目标文件存在非链式行、摘要不匹配、重复 `audit_id`、缺字段或非对象行；此时 stdout 为空 |
| `AuditReadError` | `--verify-audit` 时文件缺失、路径为目录、父目录不存在或读取失败 |
| `AuditVerificationError` | `--verify-audit` 时 JSON 解析失败、行非对象、`audit_digest` 缺失/格式错误、`audit_id` 为空/重复或摘要链断裂 |

> 注：审计在 stdout 输出之前落盘，因此 `AuditWriteError` 发生时 stdout 保证为空。

## 测试

```sh
python3 test_fhir_gateway.py
```

## 状态

已实现 `fhir-gateway`：资源校验、术语映射、审计留痕端到端可用，支持单资源与 FHIR R4 Bundle；transaction/batch Bundle 支持批量执行语义（逐项校验与映射、transaction-response/batch-response、全有或全无/逐项独立、逐项审计）；`--audit-chain` 提供基于 SHA-256 的审计完整性链，`--verify-audit` 支持离线复核；`--check-references` 提供可选的本地引用完整性校验（片段 contained 解析、非执行 Bundle 跨 entry 唯一解析、外部引用仅校验形式），含 103 个端到端测试。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
