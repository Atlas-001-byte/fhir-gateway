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
fhir-gateway --audit-errors --audit-file PATH < request.json
fhir-gateway --strict-types --audit-file PATH < request.json
fhir-gateway --require-mappings --audit-file PATH < request.json
fhir-gateway --mapping-file PATH --audit-file PATH < request.json
fhir-gateway --verify-audit --audit-file PATH
fhir-gateway --audit-find --audit-file PATH [--request-id ID] [--actor A] \
  [--decision accepted|rejected] [--from YYYY-MM-DDTHH:MM:SSZ] [--to YYYY-MM-DDTHH:MM:SSZ]
```

- `--audit-file PATH`（必需）：审计 JSON Lines 文件；文件不存在则创建，已存在则保留旧行并追加。
- `--audit-chain`（可选）：为审计增加可离线复核的完整性链；不开启时行为完全不变。
- `--check-references`（可选）：开启本地引用完整性校验；不开启时解析、校验、映射、输出与审计行为完全不变。
- `--audit-errors`（可选）：请求处理失败时追加一条 `decision=rejected` 的拒绝审计记录；不开启时行为完全不变。
- `--strict-types`（可选）：对 Patient/Observation/Condition 的已知字段收紧为 FHIR R4 类型；不开启时解析、校验、映射、输出与审计行为完全不变。
- `--require-mappings`（可选）：要求资源内每个源编码都在 `term_maps` 中精确命中目标；只影响请求入口，不开启时解析、校验、映射、输出与审计行为完全不变。
- `--mapping-file PATH`（可选）：提供可复用映射目录 JSON 文件（顶层为对象、`maps` 为数组，条目沿用 `system`/`code`/`target_system`/`target_code`），与请求 `term_maps` 合并后按 `system`+`code` 精确定位目标，避免每个请求重复携带相同映射；不传时解析、校验、映射、stdout、审计与错误行为完全不变。
- `--verify-audit`（可选）：只校验链式文件，不读取标准输入。
- `--audit-find`（可选）：只读检索审计 JSON Lines，不读取标准输入、不写审计、不执行校验/映射等其他功能。
- 请求 JSON 从标准输入读取（校验模式与检索模式除外）。

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
- 开启 `--strict-types` 后，在上述基线资源校验通过之后追加 Patient/Observation/Condition 已知字段的 FHIR R4 类型收紧（详见“FHIR R4 严格类型校验”一节）；不开启时本节约束即为全部资源校验。
- 开启 `--require-mappings` 后，在上述全部校验（含 `--check-references` 引用校验与 `--strict-types` 类型收紧）与 `term_maps` 索引构建通过之后，追加源编码全命中检查（详见“源编码全命中（--require-mappings）”一节）；不开启时未命中编码仍按基线输出 `unmapped`。

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

## FHIR R4 严格类型校验（--strict-types）

`--strict-types` 为可选开关：不开启时，仅应用上文的基线校验，`null`、形状错误、数字代字符串、未知字段等一律放行，解析、校验、映射、stdout 字段顺序、审计与错误行为完全不变。开启后在**基线校验通过之后**对 Patient/Observation/Condition 的**已知字段**追加 FHIR R4 类型校验；单资源、非执行 Bundle 的每个 `entry.resource` 与 transaction/batch 的逐项执行均覆盖。**未知字段原样保留、不检查类型**（包括 `name`/`telecom` 元素内部的任意字段）。

- Patient：
  - `active` 必须为布尔；`gender` 必须为 `male`、`female`、`other`、`unknown` 之一；`birthDate` 必须为 `YYYY-MM-DD` 的合法零填充日历日期字符串（如 `1980-02-29`，`1980-13-01`、`1980/01/01` 拒绝）。
  - `name`、`telecom` 必须为对象数组（元素内部字段不收紧）。
- Observation：
  - `status` 仍沿用基线 ObservationStatus 枚举校验。
  - `code`、`valueCodeableConcept` 必须为 CodeableConcept 对象。
  - `value[x]` 至多出现一个：同时给出 `valueCodeableConcept`、`valueQuantity`、`valueInteger`、`valueString`、`valueBoolean` 中的两个或更多即拒绝；`valueCodeableConcept`、`valueQuantity` 必须为对象、`valueInteger` 必须为整数（不接受布尔、浮点、数字字符串）、`valueString` 必须为字符串（不接受数字代字符串）、`valueBoolean` 必须为布尔（不接受 `0`/`1`）。
  - `effectiveDateTime`、`issued` 必须为 ISO 8601 日期时间字符串（`YYYY-MM-DDTHH:MM:SS`，可选小数秒与 `Z`/`±HH:MM` 时区；纯 `YYYY-MM-DD` 日期、非真实日期、数字均拒绝）。
- Condition：
  - `clinicalStatus`（基线已要求）、`verificationStatus`、`code` 出现时必须为 CodeableConcept 对象。
  - `onsetDateTime` 必须为 ISO 8601 日期时间字符串；`recordedDate` 必须为 `YYYY-MM-DD` 日期字符串。
- CodeableConcept：`coding` 出现时必须为对象数组，每个元素的 `system`、`code`、`display` 出现时必须为字符串，`userSelected` 出现时必须为布尔；`text` 出现时必须为字符串；其余字段保留。`coding` 可为空数组或省略（基线对 `Observation.code` 与 `Condition.clinicalStatus` 的“首个 coding 含非空 system/code”要求仍先执行）。

统一拒绝规则：字段值为 `null`、JSON 类型形状错误（对象传成数组等）、数字代字符串、字符串代布尔、无效日期、同一 `value[x]` 出现多个成员，均判为 `FhirValidationError`。错误 message 含定位（单资源为 `资源类型.字段`，Bundle 条目为 `Bundle.entry[i].resource.字段`，含数组下标）与唯一原因。

- 非执行 Bundle 与单资源：严格失败是网关级错误——stdout 为空、stderr 一行 JSON、退出码 `2`，且不写审计文件。
- transaction/batch：严格失败归入逐项 `processing`（`phase=validation`），不改变请求结构（`invalid`/`not-supported`）优先、transaction 全有或全无、batch 逐项独立、退出码 `0` 与审计摘要结构。
- 顺序与优先级不变：顶层结构与 `audit_context` 的 `InputError` 最先；资源严格类型校验先于 `--check-references` 引用校验，引用校验先于 `term_maps` 的 `TermMappingError`（即“类型先于引用，InputError 先于 TermMappingError”）。
- 与 `--audit-errors` 同用：严格失败先追加 `decision=rejected`、`phase=validation`、`status=400` 的审计记录，再原样报出 `FhirValidationError`；与 `--audit-chain` 同用时链式摘要不变。
- 成功输出字段与顺序完全不变，不新增字段；可与 `--check-references`、`--audit-chain`、`--audit-errors` 自由组合。
- `--verify-audit` 与 `--strict-types` 同用直接返回 `InputError`：不读取标准输入、不追加任何审计（文件不存在也不会创建）。

## 源编码全命中（--require-mappings）

`--require-mappings` 为可选开关，只影响请求入口：不开启时，未命中映射的编码仍按基线生成 `status=unmapped`、`target=null` 的映射结果，解析、校验、输出字段顺序与语义、审计与错误行为完全不变。

开启后，仍**先**按既有顺序完成请求结构与 `audit_context` 校验、资源校验、`--check-references` 引用校验与 `--strict-types` 严格类型校验，再构建 `term_maps` 索引（保留映射项字段非法、同一源编码多个不同目标的 `TermMappingError` 及其优先级）；这些全部通过后，**再**按资源 coding 收集顺序（单资源按嵌套顺序；Bundle 按 `entry` 顺序、entry 内按嵌套顺序，重复编码逐项列出）要求每个源编码（非空 `system` + `code`）在映射表中精确命中对应的 `target_system`/`target_code`。资源本身不改写，成功输出的 `status`、`resource_type`、`resource`、`mappings`、`audit` 字段顺序与映射语义完全不变（此时所有映射结果均为 `mapped`）。

- 单资源或非 transaction/batch Bundle：**任一**编码未命中即返回 `TermMappingError`——stdout 为空、stderr 一行 JSON、退出码 `2`，且**不写审计**。
- transaction：任一 entry 存在未命中编码时该 entry `status=400`（`outcome.issue.code=processing`、`phase=mapping`），整批 `status=400`（全有或全无语义不变）。
- batch：未命中的 entry `status=400` 且 `issue.code=processing`，其他 entry 继续执行，整批 `status=200`；entry 顺序与重复位置不变。
- transaction/batch 仍只写**一条**批量摘要审计：逐项仅含 `index`、`resource_type`、`phase`、`status`、`issue_code`，mapping 阶段、失败结果与 issue_code 与其他 mapping 失败一致，不记录编码值、映射配置或资源正文。
- 与 `--audit-errors` 同用（仅单资源/非执行 Bundle 的网关级失败）：先追加一条 `decision=rejected`、`phase=mapping`、`status=400`、`error_type=TermMappingError` 的拒绝记录，再原样报出错误；拒绝记录仍不含资源、编码原文与映射配置。与 `--audit-chain` 同用时链式摘要不变。
- 错误优先级不变：请求结构或 `audit_context` 非法先返回 `InputError`；资源与严格类型校验先于引用校验，引用校验先于术语检查（映射项非法/同源冲突与未命中同属 mapping 阶段，按 `term_maps` 索引构建 → 编码命中检查的顺序报告）。
- 缺 `--audit-file`、审计文件不可写或链式文件非法仍返回 `AuditWriteError`：stdout 为空、退出码 `2`、不写业务审计。
- `--verify-audit`、`--audit-find` 不读取请求，与 `--require-mappings` 同用直接返回 `InputError`（不读 stdin、不写审计、不创建文件）。

## 可复用映射目录（--mapping-file）

`--mapping-file PATH` 为可选参数：把一份可复用的映射目录放在文件里，多个请求无需重复携带相同的 `term_maps`。文件为 JSON 对象，顶层 `maps` 是数组，每个数组项沿用请求 `term_maps` 条目的四字段约定：

```json
{
  "maps": [
    {
      "system": "http://loinc.org",
      "code": "8867-4",
      "target_system": "http://snomed.info/sct",
      "target_code": "8499000"
    }
  ]
}
```

- 文件 `maps` 与请求 `term_maps` **合并**后按 `system` 与 `code` 精确定位目标；任一来源提供的映射都可命中。
- 相同源编码映射到**相同目标**时去重（文件内重复、文件与请求重复均允许）；相同源编码出现**不同目标**时以 `TermMappingError` 结束，错误信息指出源编码与两个冲突目标，已有目标**不被覆盖**。
- 处理顺序固定：先按既有规则校验请求结构、`audit_context`、资源（含 `--check-references` 引用校验与 `--strict-types` 类型收紧），再校验请求 `term_maps`（`term_maps` 非数组仍为 `InputError`，条目定位仍为 `term_maps[i]`），随后读取并校验映射文件、校验其条目（定位为 `maps[i]`）、合并两处映射，之后才生成结果并写审计。因此请求结构、`audit_context`、资源、引用、类型与请求 `term_maps` 的错误都先于映射文件问题报告。
- 成功 stdout 字段与顺序完全不变（单资源/非执行 Bundle 仍为 `status`、`resource_type`、`resource`、`mappings`、`audit`；transaction/batch 仍无 `mappings`）。`mappings` 按资源编码顺序输出：命中项 `status=mapped` 且 `target` 为目标编码，未命中项 `status=unmapped` 且 `target=null`；资源正文保持不变。
- `--require-mappings`、`--strict-types`、`--check-references`、`--audit-chain`、`--audit-errors` 语义不变，可与本参数自由组合。非执行 Bundle 对整份请求处理；`transaction` 仍为全有或全无，`batch` 仍为逐项独立结果。
- 审计**不写**映射文件路径或任何映射内容（源/目标编码、`target_system` 等），字段与脱敏边界不变。
- transaction/batch 中：映射文件的文件层错误（`MappingFileError`）是网关级错误（退出码 `2`、不写审计）；文件条目非法或两处映射冲突（`TermMappingError`）与请求 `term_maps` 条目非法一致，降级为逐项 `mapping`/`processing`，不改变全有或全无/逐项独立与“只写一条批量摘要”的语义。
- 不传 `--mapping-file` 时，解析、校验、映射、stdout、审计与错误行为与基线完全一致。

错误（均 **stdout 为空**、stderr 一行 JSON、退出码 `2`、不写审计文件、不改变已有审计文件）：

- `MappingFileError`：文件不存在、路径是目录、父目录不存在、无权限、读取失败、内容不是合法 JSON 对象、缺少 `maps` 或 `maps` 不是数组。
- `TermMappingError`：文件条目不是对象，或 `system`、`code`、`target_system`、`target_code` 任一缺失/不是非空字符串（信息定位 `maps[i]`）；以及合并时相同源编码出现不同目标（指出源编码与两个冲突目标）。
- `InputError`：`--mapping-file` 缺值/空值，或与 `--verify-audit`、`--audit-find` 同用（不读 stdin、不写审计、不创建文件）。
- 缺少 `--audit-file` 仍返回 `AuditWriteError`。
- 与 `--audit-errors` 同用时：仅 `TermMappingError`（条目非法、合并冲突、`--require-mappings` 未命中）会先追加 `phase=mapping` 的拒绝记录；`MappingFileError` 不产生拒绝记录。

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
- 资源校验或术语映射失败 → `processing`；开启 `--require-mappings` 后 entry 资源存在未命中映射的源编码同样 → `processing`（`phase=mapping`），其余开关关闭时行为不变。

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

## 拒绝审计（--audit-errors）

`--audit-errors` 为可选开关，仅用于请求处理，须与 `--audit-file` 同用；不开启时 stdout、stderr、退出码、审计、字段、批量结果及缺 `--audit-file` 的 `AuditWriteError` 行为完全不变。

开启后，请求处理返回非 `AuditWriteError` 错误之前，先向审计文件追加一条拒绝记录，再按原样输出错误（stdout 仍为空、stderr 一行 JSON、退出码 `2`，`error.type` 与 `message` 不变）：

```json
{"request_id": "req-1", "actor": "dr-house", "recorded_at": "...", "decision": "rejected", "audit_id": "...", "error_type": "FhirValidationError", "phase": "validation", "status": 400}
```

- 字段固定为 `request_id`、`actor`、`recorded_at`、`decision`、`audit_id`、`error_type`、`phase`、`status`：`decision` 恒为 `"rejected"`，`status` 恒为 `400`，`audit_id` 为非空随机值，`error_type` 与 stderr 中的 `error.type` 相同。
- `phase` 按失败归类：JSON 解析、根节点、请求结构、`audit_context` 失败为 `request`（`InputError`）；FHIR 资源及 `--check-references` 引用失败为 `validation`（`FhirValidationError`）；`term_maps` 字段、同源冲突或开启 `--require-mappings` 时源编码未命中失败为 `mapping`（`TermMappingError`）。
- `audit_context` 三值均为非空字符串时才保留，否则三项均为 `null`；不写入 `resource`、`term_maps`、引用、`message` 或请求正文。
- transaction/batch 的 entry 级 request、validation、mapping 失败沿用现有响应 Bundle、整体状态、审计 `entries` 与退出码，不另加拒绝行。
- 与 `--audit-chain` 同用时拒绝记录沿用规范 JSON 与 SHA-256 链；链不合法、目标不可写或追加不完整统一返回 `AuditWriteError`（stderr 一行 JSON、退出码 `2`），原文件不变且不再记录。
- `--verify-audit` 仍为只读、不读 stdin、不追加；与 `--audit-errors` 同用直接返回 `InputError`。

## 只读审计检索（--audit-find）

`--audit-find` 为只读入口：从审计 JSON Lines 文件按条件检索记录并输出，**不读取标准输入、不写审计、不执行资源校验/术语映射/引用检查等其他功能**。既有写入、链式、校验入口与行为完全不变。

```sh
fhir-gateway --audit-find --audit-file PATH [过滤项...]
```

过滤项（全部可选；多个条件**同时满足（AND）**，不带任何过滤项返回全部可见记录）：

- `--request-id ID`、`--actor A`：对记录同名字段**精确匹配**（区分大小写、非子串）；参数值不能为空。
- `--decision accepted|rejected`：精确匹配，仅接受这两个值。
- `--from TS`、`--to TS`：按 `recorded_at` 做含边界比较（`from <= recorded_at <= to`），TS 必须为 `YYYY-MM-DDTHH:MM:SSZ`（UTC、零填充）。可只给一个。记录的 `recorded_at` 无法按该格式解析时**不命中日期过滤**；不带任何日期过滤时该行仍可正常命中。
- 过滤项只能与 `--audit-find` 同用；在其他入口上使用属于 `InputError`。

成功时退出码 `0`，stdout 一行 JSON，顶层字段顺序固定为 `count`、`records`；无命中为空数组：

```json
{"count": 0, "records": []}
```

- `records` 严格保持文件中的**行序**，**不去重**（相同 `audit_id` 的多行各自保留）。
- `count` 等于 `records` 的长度。

可见记录的形态（否则该行**跳过且不计入 count**，不报错）：

- 必须是 JSON 对象；
- `audit_id`、`decision` 必须为**非空字符串**；
- `request_id`、`actor`、`recorded_at` 三个键必须存在：普通记录为字符串；`--audit-errors` 产生的拒绝记录在 `audit_context` 缺失时这三项为 `null`，仍属可见；
- 缺任一字段、字段类型不符（数字、布尔、数组、对象）或 `audit_id` 为空的行一律跳过。

输出只展示安全字段，**保持每条记录原有的字段顺序**，并且**不复制 `resource`、`term_maps`、任何引用值、`message` 或请求正文**（这些键被剔除）；其余审计字段原样保留，包括：

- 普通行的 `request_id`、`actor`、`recorded_at`、`decision`、`audit_id`；
- 链式行的 `audit_digest`（仅只读展示，**不复核摘要**）；
- transaction/batch 批量摘要（`bundle_type`、`entry_total`、`entries` 等）；
- 拒绝记录的 `error_type`、`phase`、`status`（其 `request_id`/`actor`/`recorded_at` 可能为 `null`）。

与其他开关同用（均不改变检索结果，也不触发其副作用）：

- `--audit-chain`：**跳过摘要链校验**，链式行、断链文件都按普通 JSON Lines 只读展示（是否被篡改不影响检索）。
- `--audit-errors`：检索过程中遇到坏行等错误时**不写拒绝审计**。
- `--strict-types`、`--check-references`：不做类型收紧或引用检查，结果与不加这些开关完全一致。
- `--require-mappings`：检索不读取请求、不执行映射，二者互斥，同用返回 `InputError`。
- `--mapping-file`：检索不读取请求、不加载映射文件，二者互斥，同用返回 `InputError`。
- `--verify-audit`：与 `--audit-find` 互斥，同用返回 `InputError`。

错误（三类均 **stdout 为空**、stderr 一行 JSON、退出码 `2`、**不写任何文件**）：

- `AuditReadError`：缺少 `--audit-file`、文件不存在、路径为目录、父目录不存在或读取失败；缺失文件也**不会被创建**。
- `InputError`：过滤项取值非法（`--decision` 非 `accepted`/`rejected`、过滤值为空）、`--from`/`--to` 时间格式非法、过滤项出现在非检索入口、或与 `--verify-audit`、`--require-mappings` 同用。
- `AuditVerificationError`：文件中某一行不是合法 JSON **对象**（JSON 解析失败或解析结果为数组/字符串/数字/布尔/`null`）。注意：字段缺失/类型不符/`audit_id` 为空只跳过该行，不属于本错误。

## 错误输出

任意受控错误：**stdout 为空**，stderr 一行 JSON，退出码 `2`，且**不写审计文件**（开启 `--audit-errors` 时按上节追加拒绝记录）：

```json
{"error": {"type": "InputError", "message": "非空错误信息"}}
```

| 错误类型 | 触发场景 |
| --- | --- |
| `InputError` | 标准输入不是合法 JSON、请求结构非法、缺少字段、`audit_context` 非法；`--audit-find` 时过滤值非法（`--decision` 取值越界、过滤值为空）、`--from`/`--to` 时间格式非法、过滤项脱离 `--audit-find` 使用、或 `--audit-find` 与 `--verify-audit` 同用；`--require-mappings` 与 `--verify-audit` 或 `--audit-find` 同用；`--mapping-file` 缺值/空值，或与 `--verify-audit`、`--audit-find` 同用 |
| `FhirValidationError` | 资源类型/id/status/编码不满足校验规则；开启 `--strict-types` 时已知字段为 `null`、形状错误、数字代字符串、日期非法或出现多个 `value[x]`；开启 `--check-references` 时引用缺少本地目标、目标重复、`reference` 非法或形式不受支持 |
| `TermMappingError` | 映射项字段非法，或同一源编码存在多个不同目标；`--mapping-file` 的文件条目非法（定位 `maps[i]`）或文件与请求对同一源编码给出不同目标；开启 `--require-mappings` 时单资源或非 transaction/batch Bundle 存在未命中映射的源编码（transaction/batch 中以上情况均降级为逐项 `processing`） |
| `MappingFileError` | `--mapping-file` 指向的文件不存在、是目录、父目录不存在、无权限、读取失败、内容不是合法 JSON 对象、缺少 `maps` 或 `maps` 不是数组；stdout 为空、退出码 `2`、不写审计、不改变已有审计文件，且不触发 `--audit-errors` 拒绝记录（transaction/batch 下同为网关级错误） |
| `AuditWriteError` | 写入/校验/普通处理入口缺少 `--audit-file`、路径为目录、父目录不存在、无权限或追加写入不完整；链式模式下目标文件存在非链式行、摘要不匹配、重复 `audit_id`、缺字段或非对象行；此时 stdout 为空 |
| `AuditReadError` | `--verify-audit` 时文件缺失、路径为目录、父目录不存在或读取失败；`--audit-find` 时除上述读取问题外，连缺少 `--audit-file` 也归入本类型（其他入口缺少该参数仍为 `AuditWriteError`） |
| `AuditVerificationError` | `--verify-audit` 时 JSON 解析失败、行非对象、`audit_digest` 缺失/格式错误、`audit_id` 为空/重复或摘要链断裂；`--audit-find` 时某一行不是合法 JSON 对象（解析失败或解析结果非对象） |

> 注：审计在 stdout 输出之前落盘，因此 `AuditWriteError` 发生时 stdout 保证为空。

## 测试

```sh
python3 test_fhir_gateway.py
```

## 状态

已实现 `fhir-gateway`：资源校验、术语映射、审计留痕端到端可用，支持单资源与 FHIR R4 Bundle；transaction/batch Bundle 支持批量执行语义（逐项校验与映射、transaction-response/batch-response、全有或全无/逐项独立、逐项审计）；`--audit-chain` 提供基于 SHA-256 的审计完整性链，`--verify-audit` 支持离线复核；`--check-references` 提供可选的本地引用完整性校验（片段 contained 解析、非执行 Bundle 跨 entry 唯一解析、外部引用仅校验形式）；`--audit-errors` 提供可选的拒绝审计（rejected 记录、按阶段归类、链式兼容）；`--strict-types` 提供可选的 FHIR R4 已知字段类型收紧（Patient/Observation/Condition、null 与形状拒绝、日期/枚举校验、value[x] 唯一、批量 processing、与其他开关组合）；`--require-mappings` 提供可选的源编码全命中要求（编码顺序精确命中、单资源/非执行 Bundle 未命中为 TermMappingError、transaction 全有或全无 400 与 batch 逐项 processing 200、拒绝审计 phase=mapping、与 verify/find 互斥）；`--audit-find` 提供只读审计检索（request_id/actor/decision 精确匹配、recorded_at 含边界时间窗、AND 组合、字段缺失跳过、行序保留不去重、敏感字段剔除、链式行/拒绝记录/批量摘要只读展示，缺文件为 AuditReadError、非法参数为 InputError、坏行为 AuditVerificationError，与其他开关同用不改变结果）；`--mapping-file` 提供可复用映射目录（文件 maps 与请求 term_maps 合并、system+code 精确定位、同目标去重、异目标 TermMappingError 指出源编码与冲突目标且不覆盖，文件层错误为 MappingFileError、条目错误为 TermMappingError 定位 maps[i]，请求/资源/term_maps 校验先于文件加载，审计不含文件路径与映射内容，与 verify/find 互斥，批量中文件层错误为网关级、条目/冲突逐项 processing），含 265 个端到端测试。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
