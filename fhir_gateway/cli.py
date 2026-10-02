"""fhir-gateway 命令行入口。

从标准输入读取请求 JSON，一次完成资源校验、术语映射与审计留痕：

  * 成功：stdout 一行固定字段顺序的结果 JSON，退出码 0，并向 --audit-file 追加审计行。
  * 失败：stderr 一行 {"error": {"type", "message"}} JSON，退出码 2，不写审计。
     AuditWriteError 时 stdout 必须为空，因此审计追加先于 stdout 输出。
"""

import json
import sys
import uuid

from .audit import append_audit
from .core import apply_mappings, build_term_map, parse_request, validate_resource
from .errors import AuditWriteError, GatewayError


def parse_args(argv):
    """解析命令行参数，目前仅支持 --audit-file（=值 或 空格分隔两种形式）。"""
    audit_file = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--audit-file":
            if index + 1 >= len(argv):
                raise AuditWriteError("parameter --audit-file is missing a value")
            audit_file = argv[index + 1]
            index += 2
        elif arg.startswith("--audit-file="):
            audit_file = arg[len("--audit-file="):]
            index += 1
        else:
            raise AuditWriteError("unknown argument %r" % arg)

    if not audit_file:
        raise AuditWriteError("required parameter --audit-file is missing")
    return audit_file


def build_audit_record(audit_context):
    """构建审计对象：保留上下文字段，补充 decision 与非空 audit_id。"""
    return {
        "request_id": audit_context["request_id"],
        "actor": audit_context["actor"],
        "recorded_at": audit_context["recorded_at"],
        "decision": "accepted",
        "audit_id": uuid.uuid4().hex,
    }


def process(raw_text, audit_path):
    """执行完整的成功路径，返回待输出到 stdout 的一行 JSON 文本。

    审计文件在此函数内完成追加，确保追加失败时调用方尚未写出任何 stdout。
    """
    resource, term_maps, audit_context = parse_request(raw_text)
    resource_type = validate_resource(resource)
    table = build_term_map(term_maps)
    mappings = apply_mappings(resource, resource_type, table)
    audit = build_audit_record(audit_context)

    # 先落审计，再产出 stdout：AuditWriteError 时 stdout 必须为空。
    append_audit(audit_path, audit)

    result = {
        "status": "accepted",
        "resource_type": resource_type,
        "resource": resource,
        "mappings": mappings,
        "audit": audit,
    }
    return json.dumps(result, ensure_ascii=False)


def main(argv=None, stdin=None, stdout=None, stderr=None):
    if argv is None:
        argv = sys.argv[1:]
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr

    try:
        audit_path = parse_args(argv)
        raw_text = stdin.read()
        output = process(raw_text, audit_path)
    except GatewayError as exc:
        error_line = json.dumps(
            {
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc) or type(exc).__name__,
                }
            },
            ensure_ascii=False,
        )
        stderr.write(error_line + "\n")
        return 2

    stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
