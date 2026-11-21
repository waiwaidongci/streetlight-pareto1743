# 002 热线修订与每日巡检看板

支持热线补传工单的乱序修订、撤单墓碑、修订驱动的每日巡检看板版本化与交班钉版。

## 新增表

- `revision_batches`：热线补传批次，`sequence` 全局单调递增，按内容 SHA-256 去重，记录应用/跳过条数。
- `incident_revisions`：修订原样留痕。只对 `(incident_id, revision_no)` 建索引、**不加唯一约束**，因为同修订号的旧记录允许跨批次重放；批次内重复由解析层拦截。
- `incident_states`：工单当前状态，主键为工单号，`revision_no` 记录已采用的最高修订号，`cancelled=true` 为撤单墓碑。
- `board_jobs`：看板重算任务，带 `attempt` 与 `lease_until` 租约。连续批次会合并进同一排队/运行任务（影响范围取并集、`source_sequence` 前移），状态含 queued/running/complete/failed/superseded/merged。
- `board_versions`：看板不可变版本，唯一约束 `(report_day, district, version_no)`，`is_current` 为当前指针；每个版本带独立的不可变 CSV 导出文件。
- `board_handovers`：交班报告，外键钉住 `board_versions.id`，导出文件在生成时固定，后续修订不改变其内容。

## 升级步骤

本项目首次启动由 SQLAlchemy `Base.metadata.create_all` 建表，新表会在后端启动时自动创建，不改动既有表。

已有演示库升级时：

1. 启动新后端，自动创建上述 6 张表。
2. 执行 `python -m scripts.seed_demo`：幂等。若 `board_versions` 为空，会用最新台账批次的工单初始化首版修订批次与看板任务，由 worker 发布首版看板；已有任意看板版本时跳过。

## 一致性约定

- 修订入库在单事务内按工单号行锁推进状态；仅当 `revision_no` 严格大于已知最大值才应用，撤单后重放的旧 upsert 不复活工单。
- 影响范围按工单修订前后报告日的并集计算，再与全部在册片区做笛卡尔积；跨日更正的多个看板版本在同一事务内切换指针。
- 看板发布采用当前指针 CAS：新版本先以 `is_current=false` 落库并写好导出文件，再在同一事务统一切换；中断重跑靠 `(report_day, district, version_no)` 唯一约束与租约 attempt 校验保持幂等。
- 仅当更新的快照已被其他任务发布或正在发布（真正并发）时旧任务才置 `superseded`；排队落后则合并范围后基于最新状态发布，避免丢更新。
- 交班报告通过 `board_handovers.board_version_id` 钉版，下载始终返回该版本的不可变文件。
