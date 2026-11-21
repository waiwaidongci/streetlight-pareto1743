"""热线修订与每日巡检看板接口。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.board import (
    BoardHandover,
    BoardJob,
    BoardVersion,
    IncidentState,
    RevisionBatch,
)
from src.models.identity import User
from src.services import board as board_service
from src.services.revision_io import REVISION_FIELDS, parse_revisions
from src.utils.time import iso
from src.utils.wakeup import wake

router = APIRouter(prefix="/board")


class RevisionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    filename: str = Field(min_length=1, max=160)
    csv_text: str = Field(min_length=1, max=2_000_000)


class HandoverRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    report_day: str = Field(min_length=10, max=10)
    district: str = Field(default="", max_length=100)
    shift_note: str = Field(default="", max_length=500)


def _version_view(version: BoardVersion, current_flag: bool | None = None):
    return {
        "id": version.id,
        "report_day": version.report_day,
        "district": version.district,
        "version_no": version.version_no,
        "is_current": version.is_current if current_flag is None else current_flag,
        "batch_id": version.batch_id,
        "job_id": version.job_id,
        "has_export": bool(version.export_file),
        "created_at": iso(version.created_at),
        "headline": version.payload.get("headline"),
    }


@router.get("/revision-fields")
def revision_fields(actor: User = Depends(current)):
    return {"fields": REVISION_FIELDS}


@router.post("/revisions", status_code=201)
def upload_revision(body: RevisionRequest, db: Session = Depends(connection),
                    actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    revisions = parse_revisions(body.csv_text)
    try:
        batch, created = board_service.ingest_revisions(db, body.filename, revisions, actor.username)
        db.commit()
    except ValueError:
        db.rollback()
        raise
    if created:
        wake()
    return {
        "id": batch.id, "sequence": batch.sequence, "filename": batch.filename,
        "row_count": batch.row_count, "applied_count": batch.applied_count,
        "skipped_count": batch.skipped_count, "created": created,
        "created_at": iso(batch.created_at),
    }


@router.get("/revisions")
def revision_batches(db: Session = Depends(connection), actor: User = Depends(current)):
    batches = db.scalars(select(RevisionBatch).order_by(RevisionBatch.sequence.desc()).limit(100))
    jobs = {job.batch_id: job for job in db.scalars(select(BoardJob)).all()}
    return [{
        "id": item.id, "sequence": item.sequence, "filename": item.filename,
        "row_count": item.row_count, "applied_count": item.applied_count,
        "skipped_count": item.skipped_count, "author": item.author,
        "created_at": iso(item.created_at),
        "job_status": jobs[item.id].status if item.id in jobs else "not_needed",
    } for item in batches]


@router.get("/incidents")
def incidents(db: Session = Depends(connection), actor: User = Depends(current),
              include_cancelled: bool = Query(True)):
    query = select(IncidentState).order_by(IncidentState.incident_id).limit(500)
    if not include_cancelled:
        query = query.where(IncidentState.cancelled.is_(False))
    return [{
        "incident_id": row.incident_id, "revision_no": row.revision_no,
        "site_id": row.site_id, "report_date": row.report_date,
        "cancelled": row.cancelled, "batch_id": row.batch_id,
        "updated_at": iso(row.updated_at),
    } for row in db.scalars(query)]


@router.get("/current")
def current_boards(db: Session = Depends(connection), actor: User = Depends(current)):
    return [_version_view(item) for item in board_service.list_current_boards(db)]


@router.get("/versions")
def versions(report_day: str = Query(min_length=10, max_length=10),
             district: str = Query(default="", max_length=100),
             db: Session = Depends(connection), actor: User = Depends(current)):
    return [_version_view(item) for item in board_service.list_versions(db, report_day, district)]


@router.get("/versions/{version_id}")
def version_detail(version_id: int, db: Session = Depends(connection),
                   actor: User = Depends(current)):
    version = db.get(BoardVersion, version_id)
    if not version:
        raise HTTPException(404, "找不到该看板版本")
    handover = db.scalar(select(BoardHandover).where(BoardHandover.board_version_id == version.id))
    return {**_version_view(version), "payload": version.payload,
            "pinned_by_handover": handover.id if handover else None}


@router.get("/versions/{version_id}/export")
def version_export(version_id: int, db: Session = Depends(connection),
                   actor: User = Depends(current)):
    version = db.get(BoardVersion, version_id)
    if not version or not version.export_file:
        raise HTTPException(404, "该看板版本没有可下载文件")
    path = ARTIFACTS / version.export_file
    if not path.is_file():
        raise HTTPException(410, "导出文件已不可用")
    filename = f'{SPEC["short_name"]}-board-{version.report_day}-v{version.version_no}.csv'
    return FileResponse(path, media_type="text/csv", filename=filename)


@router.post("/handovers", status_code=201)
def handover_create(body: HandoverRequest, db: Session = Depends(connection),
                    actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        item, created = board_service.create_handover(
            db, body.report_day, body.district, body.shift_note, actor.username)
        db.commit()
    except ValueError:
        db.rollback()
        raise
    return handover_view(item, created)


@router.get("/handovers")
def handover_list(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(BoardHandover).order_by(BoardHandover.id.desc()).limit(100))
    return [handover_view(item) for item in items]


def handover_view(item: BoardHandover, created: bool | None = None):
    view = {
        "id": item.id, "report_day": item.report_day, "district": item.district,
        "board_version_id": item.board_version_id, "shift_note": item.shift_note,
        "export_file": item.export_file, "author": item.author,
        "created_at": iso(item.created_at),
    }
    if created is not None:
        view["created"] = created
    return view


@router.get("/handovers/{handover_id}/export")
def handover_export(handover_id: int, db: Session = Depends(connection),
                    actor: User = Depends(current)):
    item = db.get(BoardHandover, handover_id)
    if not item:
        raise HTTPException(404, "找不到该交班报告")
    version = db.get(BoardVersion, item.board_version_id)
    path = ARTIFACTS / item.export_file
    if not path.is_file():
        raise HTTPException(410, "交班报告文件已不可用")
    filename = f'{SPEC["short_name"]}-handover-{item.report_day}-{item.id}.csv'
    # 钉版：始终返回生成时那个版本文件，即使看板当前指针已经前移。
    _ = version
    return FileResponse(path, media_type="text/csv", filename=filename)
