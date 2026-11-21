"""热线工单修订、每日巡检看板版本与交班记录的表模型。

设计要点：
- 工单按 ``incident_id`` 归并，以 ``revision_no`` 单调递增认最新；撤单是高修订号的墓碑。
- 看板以「报告日 × 片区」为粒度做不可变版本，指针切换是行级比较交换（CAS），保证
  跨日更正原子生效、晚到的旧计算无法覆盖新版本。
- 交班报告一旦生成即钉住所引用的看板版本，后续修订不影响其下载内容。
"""
from datetime import datetime
from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class RevisionBatch(Base):
    """热线补传批次：一次导入的一组工单修订。序号全局单调递增。"""

    __tablename__ = 'revision_batches'

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(unique=True)
    filename: Mapped[str] = mapped_column(String(160))
    row_count: Mapped[int]
    applied_count: Mapped[int]
    skipped_count: Mapped[int]
    checksum: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class IncidentRevision(Base):
    """每条修订原样留痕；同一工单号只保留修订号最大的一条为当前状态。

    热线可能跨批次重放同修订号的旧记录，因此这里只建索引、不加唯一约束；
    同一批次内的（工单，修订号）重复由解析层拦截。
    """

    __tablename__ = 'incident_revisions'
    __table_args__ = (
        Index('ix_incident_revisions_lookup', 'incident_id', 'revision_no'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('revision_batches.id'), index=True)
    incident_id: Mapped[str] = mapped_column(String(64), index=True)
    revision_no: Mapped[int]
    site_id: Mapped[str | None] = mapped_column(String(64))
    report_date: Mapped[str | None] = mapped_column(String(10))
    op: Mapped[str] = mapped_column(String(20))
    cancelled: Mapped[bool] = mapped_column(default=False)
    applied: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=now)


class IncidentState(Base):
    """工单当前状态表，由修订服务按修订号推进；撤单墓碑永不被旧记录复活。"""

    __tablename__ = 'incident_states'

    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_no: Mapped[int]
    batch_id: Mapped[int] = mapped_column(ForeignKey('revision_batches.id'))
    site_id: Mapped[str | None] = mapped_column(String(64), index=True)
    report_date: Mapped[str | None] = mapped_column(String(10), index=True)
    cancelled: Mapped[bool] = mapped_column(default=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=now)


class BoardJob(Base):
    """修订驱动的看板重算任务。

    连续到达的修订批次会合并进同一个排队/运行任务，因此 batch_id 只记录触发
    最新一次合并的批次，不做唯一约束。
    """

    __tablename__ = 'board_jobs'

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('revision_batches.id'), index=True)
    # 拍快照时的最新批次号；发布时若已落后则整体放弃，防止旧计算覆盖新批次。
    source_sequence: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    affected_days: Mapped[list] = mapped_column(JSON)
    affected_districts: Mapped[list] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]


class BoardVersion(Base):
    """看板不可变版本。同一「报告日 × 片区」可有多个版本，仅一个为当前版本。"""

    __tablename__ = 'board_versions'
    __table_args__ = (
        UniqueConstraint('report_day', 'district', 'version_no'),
        Index('ix_board_versions_lookup', 'report_day', 'district', 'is_current'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_day: Mapped[str] = mapped_column(String(10), index=True)
    district: Mapped[str] = mapped_column(String(100), default='')
    version_no: Mapped[int]
    batch_id: Mapped[int] = mapped_column(ForeignKey('revision_batches.id'))
    job_id: Mapped[int] = mapped_column(ForeignKey('board_jobs.id'))
    is_current: Mapped[bool] = mapped_column(default=False, index=True)
    export_file: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=now)


class BoardHandover(Base):
    """交班报告：生成时刻钉住所引用的看板版本，事后修订不改变其内容与下载件。"""

    __tablename__ = 'board_handovers'

    id: Mapped[int] = mapped_column(primary_key=True)
    report_day: Mapped[str] = mapped_column(String(10), index=True)
    district: Mapped[str] = mapped_column(String(100), default='')
    board_version_id: Mapped[int] = mapped_column(ForeignKey('board_versions.id'))
    shift_note: Mapped[str | None] = mapped_column(String(500))
    export_file: Mapped[str] = mapped_column(String(120))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
