"""修订驱动看板的事务服务：入库、重算租约、版本 CAS 发布与交班钉版。

并发与一致性保证：
- 修订入库在单事务内按工单号加锁推进状态，重复提交同一批次靠校验和去重；
- 连续到达的批次合并进同一个排队/运行任务，影响范围取并集、快照批次号前移，
  调度延迟只会让发布稍晚，不会让任何一天的重算丢失；
- 重算任务带租约，中断后可重领重跑；看板版本靠 (报告日,片区,版本号) 唯一约束
  和当前指针 CAS 保证幂等，重跑不会重复扣加或重复出版本；
- 仅当更新的快照已被其他任务发布或正在发布（真正并发）时，旧任务才标记
  superseded 让位，晚到的旧计算不能覆盖新结果；
- 跨日/跨片区的多个版本在同一事务内切换，要么全部生效要么全部保留旧版；
- 交班报告钉住生成时的版本及其不可变导出文件，后续修订不影响其下载内容。
"""
import csv
import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from src.config.database import Session
from src.domain.board import (
    affected_scopes,
    build_board,
    normalize_site,
    reduce_states,
    register_sites_from_rows,
)
from src.models.board import (
    BoardHandover,
    BoardJob,
    BoardVersion,
    IncidentRevision,
    IncidentState,
    RevisionBatch,
)
from src.models.source import Dataset
from src.services.audit import record
from src.utils.time import now

LEASE_MINUTES = 3
MAX_ATTEMPTS = 3


def checksum(revisions):
    raw = json.dumps(revisions, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _state_dict(db):
    rows = db.scalars(select(IncidentState)).all()
    return {
        row.incident_id: {
            "incident_id": row.incident_id, "revision_no": row.revision_no,
            "batch_id": row.batch_id, "site_id": row.site_id,
            "report_date": row.report_date, "cancelled": row.cancelled,
        }
        for row in rows
    }


def _register_sites(db):
    dataset = db.scalar(select(Dataset).order_by(Dataset.id.desc()).limit(1))
    if not dataset:
        return {}
    return register_sites_from_rows(dataset.rows)


def _known_districts(sites):
    return sorted({site["district"] for site in sites.values()})


def ingest_revisions(db, filename, revisions, actor):
    """在调用方事务内应用一个修订批次，返回 (批次, 是否新建)。"""
    signature = checksum(revisions)
    duplicate = db.scalar(select(RevisionBatch).where(RevisionBatch.checksum == signature))
    if duplicate:
        return duplicate, False

    # 行锁顺序按工单号，保证并发入库时修订号比较不会丢更新。
    incident_ids = sorted({item["incident_id"] for item in revisions})
    locked = {
        row.incident_id: row
        for row in db.scalars(
            select(IncidentState).where(IncidentState.incident_id.in_(incident_ids)).with_for_update()
        )
    }
    before = {
        key: {
            "incident_id": row.incident_id, "revision_no": row.revision_no, "batch_id": row.batch_id,
            "site_id": row.site_id, "report_date": row.report_date, "cancelled": row.cancelled,
        }
        for key, row in locked.items()
    }

    sequence = (db.scalar(select(RevisionBatch.sequence).order_by(RevisionBatch.sequence.desc()).limit(1)) or 0) + 1
    batch = RevisionBatch(
        sequence=sequence, filename=filename, row_count=len(revisions),
        applied_count=0, skipped_count=0, checksum=signature, author=actor,
    )
    db.add(batch)
    db.flush()

    annotated = [{**item, "batch_id": batch.id} for item in revisions]
    after, applied, skipped = reduce_states(before, annotated)
    batch.applied_count, batch.skipped_count = applied, skipped

    for item in annotated:
        db.add(IncidentRevision(
            batch_id=batch.id, incident_id=item["incident_id"], revision_no=item["revision_no"],
            site_id=item["site_id"], report_date=item["report_date"], op=item["op"],
            cancelled=item["op"] == "cancel",
            applied=after[item["incident_id"]]["revision_no"] == item["revision_no"],
        ))
    for incident_id, state in after.items():
        row = locked.get(incident_id)
        if row is None:
            row = IncidentState(incident_id=incident_id)
            db.add(row)
        row.revision_no = state["revision_no"]
        row.batch_id = state["batch_id"]
        row.site_id = state["site_id"]
        row.report_date = state["report_date"]
        row.cancelled = state["cancelled"]
        row.updated_at = now()

    sites = _register_sites(db)
    known = _known_districts(sites)
    days, districts = affected_scopes(before, after, known, {})
    # 修订引用了未知灯组时提前拦截，避免看板统计到台账外点位。
    known_site_ids = set(sites)
    for state in after.values():
        if not state["cancelled"] and state["site_id"] not in known_site_ids:
            raise ValueError(f"工单「{state['incident_id']}」引用的灯组「{state['site_id']}」不在台账中，请先补录台账。")

    job = None
    # 只有真正改变了当前状态的修订才需要重算看板；全是被跳过的旧记录时
    # 不建任务、不产生与当前内容相同的新版本。
    if days and applied:
        # 已有排队/运行中的任务时合并影响范围并前移快照批次号：
        # 排队任务不会因新批次而被整体丢弃，保证任何一天的重算都不丢失。
        job = db.scalar(select(BoardJob).where(BoardJob.status.in_(("queued", "running")))
                        .order_by(BoardJob.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            job = BoardJob(batch_id=batch.id, source_sequence=sequence, status="queued",
                           affected_days=days, affected_districts=districts)
            db.add(job)
        else:
            job.batch_id = batch.id
            job.source_sequence = sequence
            job.affected_days = sorted(set(job.affected_days) | set(days))
            job.affected_districts = sorted(set(job.affected_districts) | set(districts))
            if job.status == "running":
                # 已在计算中的任务让位：停止当前尝试，回到队列，按合并后的范围重跑。
                job.status = "queued"
                job.lease_until = None
    db.flush()
    record(db, actor, "revision.ingested", batch.id,
           sequence=sequence, applied=applied, skipped=skipped,
           days=days, districts=districts, job_id=job.id if job else None)
    return batch, True


def claim_board_job(stamp):
    """领取待处理或租约过期的看板任务，返回任务上下文或 None。"""
    with Session.begin() as db:
        job = db.scalar(
            select(BoardJob).where(
                (BoardJob.status == "queued")
                | ((BoardJob.status == "running") & (BoardJob.lease_until < stamp))
            ).order_by(BoardJob.id).limit(1).with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        if job.attempt >= MAX_ATTEMPTS:
            job.status = "failed"
            job.error = "看板重算多次中断，请联系管理员检查后重新触发"
            job.finished_at = stamp
            record(db, "worker", "board.failed", job.id, reason=job.error)
            return None
        job.status = "running"
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=LEASE_MINUTES)
        return {
            "job_id": job.id, "attempt": job.attempt, "batch_id": job.batch_id,
            "source_sequence": job.source_sequence,
            "days": list(job.affected_days), "districts": list(job.affected_districts),
        }


def _write_board_csv(version_id, payload):
    from src.config.settings import ARTIFACTS
    from src.utils.exports import safe_cell

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f"board-{version_id}.csv"
    path = ARTIFACTS / name
    staging = path.with_suffix(".tmp")
    with staging.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow([safe_cell(payload["headline"])])
        writer.writerow([safe_cell(payload["note"])])
        for metric in payload["metrics"]:
            writer.writerow([safe_cell(metric["label"]), safe_cell(metric["value"]),
                             safe_cell(metric.get("detail", ""))])
        writer.writerow([])
        columns = [
            ("rank", "排名"), ("site_id", "灯组编号"), ("district", "片区"),
            ("site_name", "台账点位"), ("fixture_count", "在册灯具数"),
            ("incident_count", "工单数"), ("cumulative_count", "累计工单数"),
            ("cumulative_share_pct", "累计工单占比（%）"),
            ("rate_per_100", "每百盏报修数"), ("action", "建议处置"), ("next_step", "交接事项"),
        ]
        writer.writerow([safe_cell(label) for _, label in columns])
        for row in payload["rows"]:
            writer.writerow([safe_cell(row.get(key)) for key, _ in columns])
        writer.writerow([])
        writer.writerow(["当日有效工单明细（含修订号）"])
        writer.writerow(["暗区工单号", "报修日期", "灯组编号", "台账点位", "修订号"])
        for item in payload["trace"]:
            writer.writerow([safe_cell(item[key]) for key in
                             ("incident_id", "report_date", "site_id", "site_name", "revision_no")])
    staging.replace(path)
    return name


def execute_board_job(context, stamp):
    """重算受影响看板并原子发布。失败返回原因，成功或任务已被接管返回 None。"""
    job_id = context["job_id"]
    attempt = context["attempt"]
    with Session.begin() as db:
        job = db.get(BoardJob, job_id, with_for_update=True)
        if job is None or job.attempt != attempt or job.status != "running":
            # 租约已被其他进程接管或任务已结束，本次执行作废，避免重复发布。
            return None
        latest = db.scalar(select(RevisionBatch.sequence).order_by(RevisionBatch.sequence.desc()).limit(1))
        # 入队后又有新批次：判断更新的快照是否已经被其他任务发布或正在发布。
        if latest != context["source_sequence"]:
            newer_peers = db.scalars(select(BoardJob).where(
                BoardJob.id != job_id,
                BoardJob.source_sequence > context["source_sequence"],
            ).with_for_update(skip_locked=True)).all()
            newer_published = any(peer.status == "complete" for peer in newer_peers)
            newer_running = any(peer.status == "running" for peer in newer_peers)
            if newer_published or newer_running:
                # 真正并发：更新的快照已经（或即将）生效，本任务让位，绝不覆盖。
                job.status = "superseded"
                job.error = f"任务基于修订批次 {context['source_sequence']}，更新的批次 {latest} 已由其他任务处理"
                job.finished_at = stamp
                job.lease_until = None
                record(db, "worker", "board.superseded", job.id,
                       source=context["source_sequence"], latest=latest)
                return job.error
            # 没有并发发布者：把排队中的对等任务合并进来，快照前移后基于最新状态发布，
            # 保证调度延迟不会让任何一天的重算丢失。
            pending_peers = [peer for peer in newer_peers if peer.status in ("queued", "running")]
            for peer in pending_peers:
                job.affected_days = sorted(set(job.affected_days) | set(peer.affected_days))
                job.affected_districts = sorted(set(job.affected_districts) | set(peer.affected_districts))
                peer.status = "merged"
                peer.error = f"影响范围已合并进任务 {job_id}（修订批次推进到 {latest}）"
                peer.finished_at = stamp
                peer.lease_until = None
            job.source_sequence = latest
            context = {**context, "source_sequence": latest,
                       "days": list(job.affected_days), "districts": list(job.affected_districts)}
            record(db, "worker", "board.merged", job.id, latest=latest,
                   merged_jobs=[peer.id for peer in pending_peers])

        sites = _register_sites(db)
        if not sites:
            failure = "缺少灯组台账，无法构建巡检看板"
            job.status, job.error, job.finished_at, job.lease_until = "failed", failure, stamp, None
            record(db, "worker", "board.failed", job.id, reason=failure)
            return failure
        states = _state_dict(db)
        batch_id = job.batch_id

        try:
            staged = []
            for day in context["days"]:
                for district in context["districts"]:
                    payload = build_board(day, district, sites, states)
                    current = db.scalar(select(BoardVersion).where(
                        BoardVersion.report_day == day,
                        BoardVersion.district == district,
                        BoardVersion.is_current.is_(True),
                    ).with_for_update())
                    next_no = (current.version_no + 1) if current else 1
                    version = BoardVersion(
                        report_day=day, district=district, version_no=next_no,
                        batch_id=batch_id, job_id=job_id, is_current=False, payload=payload,
                    )
                    db.add(version)
                    db.flush()  # 取得 version.id 用于命名不可变导出文件
                    version.export_file = _write_board_csv(version.id, payload)
                    staged.append((version, current))
        except ValueError as error:
            failure = str(error)[:500]
            job.status, job.error, job.finished_at, job.lease_until = "failed", failure, stamp, None
            record(db, "worker", "board.failed", job.id, reason=failure)
            return failure

        # 单事务内切换全部受影响指针：跨日更正两天（及各片区）一起生效，
        # 事务提交前看板仍指向整套旧版本。
        for version, current in staged:
            if current is not None:
                current.is_current = False
            version.is_current = True

        job.status = "complete"
        job.error = None
        job.finished_at = stamp
        job.lease_until = None
        record(db, "worker", "board.published", job.id,
               batch_id=batch_id, versions=[version.id for version, _ in staged])
        return None


def current_board(db, report_day, district):
    return db.scalar(select(BoardVersion).where(
        BoardVersion.report_day == report_day,
        BoardVersion.district == (district or ""),
        BoardVersion.is_current.is_(True),
    ))


def list_current_boards(db):
    return db.scalars(select(BoardVersion).where(
        BoardVersion.is_current.is_(True)).order_by(
        BoardVersion.report_day.desc(), BoardVersion.district)).all()


def list_versions(db, report_day, district):
    return db.scalars(select(BoardVersion).where(
        BoardVersion.report_day == report_day,
        BoardVersion.district == (district or ""),
    ).order_by(BoardVersion.version_no.desc())).all()


def create_handover(db, report_day, district, shift_note, actor):
    """钉住当前看板版本生成交班报告；返回已存在的同版报告以保证幂等。"""
    board = current_board(db, report_day, district)
    if board is None:
        raise ValueError("该日期与片区还没有已发布的看板版本。")
    existing = db.scalar(select(BoardHandover).where(
        BoardHandover.board_version_id == board.id).with_for_update())
    if existing:
        return existing, False
    handover = BoardHandover(
        report_day=report_day, district=district or "",
        board_version_id=board.id, shift_note=(shift_note or None),
        export_file=board.export_file, author=actor,
    )
    db.add(handover)
    db.flush()
    record(db, actor, "board.handover", handover.id,
           board_version_id=board.id, report_day=report_day, district=district or "")
    return handover, True


def seed_board_from_dataset(db, dataset, actor="seed"):
    """用既有台账+工单明细初始化首版看板（幂等，已有任意版本时跳过）。"""
    sites = register_sites_from_rows(dataset.rows)
    if not sites or db.scalar(select(BoardVersion.id).limit(1)):
        return []
    sequence = (db.scalar(select(RevisionBatch.sequence).order_by(RevisionBatch.sequence.desc()).limit(1)) or 0) + 1
    batch = RevisionBatch(sequence=sequence, filename=dataset.filename, row_count=dataset.row_count,
                          applied_count=dataset.row_count, skipped_count=0,
                          checksum=dataset.checksum, author=actor)
    db.add(batch)
    db.flush()

    states = {}
    for row in dataset.rows:
        if row.get("record_type") != "暗区工单":
            continue
        incident_id = row["incident_id"].strip().upper()
        site_id = normalize_site(row["site_id"])
        states[incident_id] = {
            "incident_id": incident_id, "revision_no": 1, "batch_id": batch.id,
            "site_id": site_id, "report_date": row["report_date"], "cancelled": False,
        }
        db.add(IncidentRevision(batch_id=batch.id, incident_id=incident_id, revision_no=1,
                                site_id=site_id, report_date=row["report_date"],
                                op="upsert", applied=True))
    for state in states.values():
        db.add(IncidentState(**state))

    days = sorted({state["report_date"] for state in states.values()})
    districts = [""] + _known_districts(sites)
    job = BoardJob(batch_id=batch.id, source_sequence=sequence, status="queued",
                   affected_days=days, affected_districts=districts)
    db.add(job)
    db.flush()
    return [job.id]
