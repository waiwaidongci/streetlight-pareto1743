"""热线补传修订 CSV 的解析与校验。

修订文件列：工单号、修订号、操作（upsert/cancel，兼容「修订/撤单」）、
灯组编号、报修日期。撤单行只需要工单号和修订号。
"""
import csv
import io
from datetime import date

from src.domain.board import CANCEL, UPSERT, normalize_incident, normalize_site

REVISION_FIELDS = ["incident_id", "revision_no", "op", "site_id", "report_date"]
_OP_ALIASES = {
    UPSERT: UPSERT, "修订": UPSERT, "新建": UPSERT, "补传": UPSERT, "upsert": UPSERT,
    CANCEL: CANCEL, "撤单": CANCEL, "作废": CANCEL, "cancel": CANCEL,
}


def _date(value, label):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}须使用有效的 YYYY-MM-DD 日期。") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{label}须使用有效的 YYYY-MM-DD 日期。")
    return value


def parse_revisions(text):
    reader = csv.DictReader(io.StringIO(text.removeprefix("﻿"), newline=""), strict=True)
    headers = reader.fieldnames or []
    if set(headers) != set(REVISION_FIELDS) or len(headers) != len(set(headers)):
        raise ValueError(f"修订 CSV 表头须为：{', '.join(REVISION_FIELDS)}。")
    revisions = []
    seen = set()
    try:
        for raw in reader:
            if len(revisions) >= 5000:
                raise ValueError("一个修订批次最多导入 5000 行。")
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("工作表的列数与表头不一致。")
            line = reader.line_num
            incident_id = normalize_incident(raw.get("incident_id"))
            raw_no = (raw.get("revision_no") or "").strip()
            try:
                revision_no = int(raw_no)
            except (TypeError, ValueError):
                raise ValueError(f"CSV 第 {line} 行 · 修订号须为正整数。") from None
            if revision_no < 1:
                raise ValueError(f"CSV 第 {line} 行 · 修订号须为正整数。")
            op = _OP_ALIASES.get((raw.get("op") or "").strip())
            if not op:
                raise ValueError(f"CSV 第 {line} 行 · 操作须为 upsert 或 cancel（兼容「修订/撤单」）。")
            key = (incident_id, revision_no)
            if key in seen:
                raise ValueError(f"CSV 第 {line} 行 · 工单「{incident_id}」修订号 {revision_no} 在文件内重复。")
            seen.add(key)
            if op == CANCEL:
                site_id = report_day = None
                if (raw.get("site_id") or "").strip() or (raw.get("report_date") or "").strip():
                    raise ValueError(f"CSV 第 {line} 行 · 撤单行须留空灯组编号和报修日期。")
            else:
                site_id = normalize_site(raw.get("site_id"))
                report_day = _date((raw.get("report_date") or "").strip(), f"CSV 第 {line} 行 · 报修日期")
            revisions.append({
                "incident_id": incident_id, "revision_no": revision_no, "op": op,
                "site_id": site_id, "report_date": report_day,
            })
    except csv.Error:
        raise ValueError(f"CSV 第 {reader.line_num} 行附近格式不正确。") from None
    if not revisions:
        raise ValueError("修订 CSV 至少需要一行有效数据。")
    return revisions
