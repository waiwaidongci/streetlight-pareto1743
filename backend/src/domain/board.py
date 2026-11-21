"""每日巡检看板的纯领域逻辑：修订归并、影响范围、看板构建与统计。

全部函数不触碰数据库，便于离线复算与单元测试。
口径与 ``domain/analysis.py`` 保持一致：
- 灯组编号去首尾空格并转大写；
- 基尼系数用全部灯组排名 Σ[(2i−n−1)xᵢ]/(nS)；
- 每百盏报修数按未舍入值比较。
"""
from collections import Counter
from fractions import Fraction

from .spec import SPEC

_REGISTER = SPEC["fields"][0]["options"][0]
_DISPATCH, _OBSERVE, _ARCHIVE = SPEC["modes"]

# 看板统一采用业务默认门槛，保证不同修订批次下结果可比较。
BOARD_DEFAULTS = {
    "top_percent": next(field["default"] for field in SPEC["params"] if field["key"] == "top_percent"),
    "min_incidents": next(field["default"] for field in SPEC["params"] if field["key"] == "min_incidents"),
    "min_rate": next(field["default"] for field in SPEC["params"] if field["key"] == "min_rate"),
}

UPSERT, CANCEL = "upsert", "cancel"
OPS = (UPSERT, CANCEL)


def normalize_site(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("灯组编号不能为空。")
    return value.strip().upper()


def normalize_incident(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("暗区工单号不能为空。")
    return value.strip().upper()


def reduce_states(existing, revisions):
    """把一批修订折叠进当前状态字典，返回 (新状态, 应用条数, 跳过条数)。

    规则：
    - 仅当修订号严格大于该工单已知最大修订号时才应用；
    - 撤单（墓碑）同样按修订号比较，撤单后重放的旧记录不再复活工单。
    ``existing`` 与返回值都以工单号为键，值为状态字典（拷贝，不就地修改）。
    """
    states = {key: dict(value) for key, value in existing.items()}
    applied = skipped = 0
    for revision in revisions:
        incident_id = revision["incident_id"]
        revision_no = revision["revision_no"]
        current = states.get(incident_id)
        if current is not None and revision_no <= current["revision_no"]:
            skipped += 1
            continue
        if revision["op"] == CANCEL:
            states[incident_id] = {
                "incident_id": incident_id, "revision_no": revision_no,
                "batch_id": revision["batch_id"], "site_id": None,
                "report_date": None, "cancelled": True,
            }
        else:
            states[incident_id] = {
                "incident_id": incident_id, "revision_no": revision_no,
                "batch_id": revision["batch_id"], "site_id": revision["site_id"],
                "report_date": revision["report_date"], "cancelled": False,
            }
        applied += 1
    return states, applied, skipped


def active_incidents(states):
    """按工单号排序返回未撤单工单。"""
    return sorted(
        (state for state in states.values() if not state["cancelled"]),
        key=lambda state: state["incident_id"],
    )


def affected_scopes(states_before, states_after, known_districts, site_district):
    """计算修订影响到的（报告日 × 片区）集合。

    按每个工单修订前后状态的日期并集取天数：撤单会清空新状态上的日期，
    只看新状态会漏掉被撤工单原本所在的报告日，因此旧状态日期也必须纳入。
    再与全部在册片区（含空串=全片区）做笛卡尔积，使跨日更正同时命中两天、
    所有片区看板，发布时一起切换，不会只改一半。
    """
    incident_ids = set(states_before) | set(states_after)
    days = set()
    for incident_id in incident_ids:
        old = states_before.get(incident_id)
        new = states_after.get(incident_id)
        for state in (old, new):
            if state and not state["cancelled"] and state["report_date"]:
                days.add(state["report_date"])
    if not days:
        return [], []
    districts = [""] + [name for name in sorted(known_districts)]
    # site_district 仅用于文档化：片区看板覆盖该片区全部在册灯组，因此按全片区集合重算。
    _ = site_district
    return sorted(days), districts


def gini(counts_ascending, total, n):
    if not total:
        return None
    numerator = sum((2 * rank - n - 1) * count for rank, count in enumerate(counts_ascending, start=1))
    return round(numerator / (n * total), 6)


def _triage(ranked, counts, fixtures, min_incidents, min_rate):
    queue = []
    threshold = Fraction(str(min_rate))
    for site_id in ranked:
        count = counts[site_id]
        rate = Fraction(100 * count, fixtures[site_id])
        if count >= min_incidents and rate >= threshold:
            priority, action = 0, _DISPATCH
            next_step = "对照本组工单，夜间逐盏核查并回填灯杆编号和熄灯时段。"
        elif count:
            priority, action = 1, _OBSERVE
            deficit = f"距起派单数还差 {min_incidents - count} 单" if count < min_incidents else "每百盏报修数未达门槛"
            next_step = f"{deficit}，下个周期用新增工单复核。"
        else:
            priority, action = 2, _ARCHIVE
            next_step = "保留在册点位，纳入下一周期分母。"
        queue.append((priority, -rate, -count, site_id, action, next_step, round(float(rate), 2)))
    return queue


def build_board(report_day, district, register_sites, states, params=None):
    """基于在册灯组和工单当前状态，构建某一天、某片区的看板载荷（纯数据）。

    看板按「报告日 == 当天」统计当日有效工单；在册灯组始终全量进入分母，
    零报修灯组保留为常规归档。
    """
    options = {**BOARD_DEFAULTS, **(params or {})}
    sites = {
        site_id: site for site_id, site in register_sites.items()
        if not district or site["district"] == district
    }
    if not sites:
        raise ValueError(f"台账中没有「{district or '全部片区'}」的灯组。")

    incidents = [
        state for state in active_incidents(states)
        if state["report_date"] == report_day and state["site_id"] in sites
    ]
    counts = Counter(state["site_id"] for state in incidents)
    ranked = sorted(sites, key=lambda site_id: (-counts[site_id], site_id))
    n, total = len(ranked), len(incidents)
    zero_count = n - len(counts)

    top_percent = options["top_percent"]
    top_count = (n * top_percent + 99) // 100
    descending = [counts[site_id] for site_id in ranked]
    top_incidents = sum(descending[:top_count])
    gini_value = gini(descending[::-1], total, n)
    top_share = round(100 * top_incidents / total, 6) if total else None

    fixtures = {site_id: sites[site_id]["fixture_count"] for site_id in ranked}
    triage = _triage(ranked, counts, fixtures, options["min_incidents"], options["min_rate"])
    rows = []
    for priority, neg_rate, neg_count, site_id, action, next_step, rate in triage:
        cumulative = sum(counts[other] for other in ranked[: ranked.index(site_id) + 1])
        rows.append({
            "rank": ranked.index(site_id) + 1,
            "site_id": site_id,
            "district": sites[site_id]["district"],
            "site_name": sites[site_id]["site_name"],
            "fixture_count": fixtures[site_id],
            "incident_count": counts[site_id],
            "cumulative_count": cumulative,
            "cumulative_share_pct": round(100 * cumulative / total, 6) if total else None,
            "rate_per_100": rate,
            "action": action,
            "next_step": next_step,
        })
    dispatch_count = sum(row["action"] == _DISPATCH for row in rows)
    trace = sorted(
        (
            {
                "incident_id": state["incident_id"], "report_date": state["report_date"],
                "site_id": state["site_id"], "site_name": sites[state["site_id"]]["site_name"],
                "revision_no": state["revision_no"],
            }
            for state in incidents
        ),
        key=lambda row: (row["incident_id"],),
    )

    if dispatch_count:
        headline = f"{report_day} {district or '全片区'}：当日 {total} 张有效工单，建议夜巡 {dispatch_count} 组。"
        next_step = f"优先安排 {dispatch_count} 个达门槛灯组的夜巡，按每百盏报修数排序交接。"
    elif total:
        headline = f"{report_day} {district or '全片区'}：当日 {total} 张有效工单，暂无灯组同时达到夜巡门槛。"
        next_step = "保留有工单的点位观察，零报修点位常规归档。"
    else:
        headline = f"{report_day} {district or '全片区'}：当日无有效工单，全部灯组常规归档。"
        next_step = "当日无报修工单，按台账归档并在下一周期复核。"

    return {
        "report_day": report_day,
        "district": district,
        "headline": headline,
        "note": (
            f"{district or '全部片区'} · 报告日 {report_day}。"
            f"零报修灯组 {zero_count} 组，均计入在册灯组分母。"
            f"门槛：至少 {options['min_incidents']} 单且每百盏至少 {options['min_rate']:g} 单。"
        ),
        "params": options,
        "metrics": [
            {"label": "在册灯组", "value": n, "detail": f"在册灯具 {sum(fixtures.values())} 盏"},
            {"label": "当日有效工单", "value": total, "detail": f"涉及 {len(counts)} 个灯组"},
            {"label": "零报修灯组", "value": zero_count, "detail": f"占在册灯组 {zero_count / n:.1%}"},
            {"label": "基尼系数", "value": gini_value, "detail": "按各灯组当日工单数测算" if total else "当日总工单数为零，基尼未定义"},
            {"label": "头部工单份额（%）", "value": top_share, "detail": f"头部 {top_percent}% 取 {top_count} 组，共 {top_incidents} 单"},
            {"label": "建议夜巡", "value": dispatch_count, "detail": f"至少 {options['min_incidents']} 单且每百盏至少 {options['min_rate']:g} 单"},
        ],
        "rows": rows,
        "trace": trace,
        "sections": [{"title": "夜巡交接与复核", "text": next_step}],
        "generated_from_revision": max((state["revision_no"] for state in states.values()), default=0),
    }


def register_sites_from_rows(rows):
    """从台账/工单混合明细中抽取在册灯组字典（兼容演示数据结构）。"""
    sites = {}
    for row in rows:
        if row.get("record_type") != _REGISTER:
            continue
        site_id = normalize_site(row["site_id"])
        sites[site_id] = {
            "site_id": site_id,
            "district": row["district"].strip(),
            "site_name": row["site_name"].strip(),
            "fixture_count": row["fixture_count"],
        }
    return sites
