"""审计留痕：把审计条目以 JSON Lines 形式追加到指定文件。"""

import json
import os

from .errors import AuditWriteError


def append_audit(path, audit_entry):
    """将一条审计 JSON 追加到 path。

    - 文件不存在时创建；使用 O_APPEND 打开，已有内容原样保留。
    - path 为目录、无权限、无法打开或写入不完整（含落盘失败）时抛 AuditWriteError。
    """
    line = json.dumps(audit_entry, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")

    if os.path.isdir(path):
        raise AuditWriteError("audit path %r is a directory" % path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise AuditWriteError(
            "cannot open audit file %r for appending: %s" % (path, exc)
        )

    try:
        total = 0
        while total < len(data):
            # os.write 可能只写入部分字节，必须循环直至完整追加。
            written = os.write(fd, data[total:])
            if written <= 0:
                raise AuditWriteError(
                    "incomplete append to audit file %r: %d of %d bytes written"
                    % (path, total, len(data))
                )
            total += written
        if total != len(data):
            raise AuditWriteError(
                "incomplete append to audit file %r: %d of %d bytes written"
                % (path, total, len(data))
            )
        os.fsync(fd)
    except AuditWriteError:
        raise
    except OSError as exc:
        raise AuditWriteError(
            "failed while appending audit file %r: %s" % (path, exc)
        )
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            raise AuditWriteError(
                "failed while closing audit file %r: %s" % (path, exc)
            )
