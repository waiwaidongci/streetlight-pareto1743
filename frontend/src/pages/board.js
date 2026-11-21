import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const REVISION_HEADER = 'incident_id,revision_no,op,site_id,report_date';
const JOB_STATE = {
  queued: '排队中', running: '正在重算', complete: '已发布', failed: '失败',
  superseded: '已让位', merged: '已合并', not_needed: '无需重算',
};

function districtLabel(value) {
  return value || '全片区';
}

function boardTable(payload) {
  const columns = [
    {key: 'rank', label: '排名'},
    {key: 'site_id', label: '灯组编号'},
    {key: 'district', label: '片区'},
    {key: 'site_name', label: '台账点位'},
    {key: 'fixture_count', label: '在册灯具数'},
    {key: 'incident_count', label: '当日工单'},
    {key: 'cumulative_share_pct', label: '累计占比（%）'},
    {key: 'rate_per_100', label: '每百盏报修数'},
    {key: 'action', label: '建议处置'},
  ];
  return table(columns, payload.rows, {caption: payload.headline, pageSize: 20});
}

function traceTable(payload) {
  return table([
    {key: 'incident_id', label: '暗区工单号'},
    {key: 'report_date', label: '报修日期'},
    {key: 'site_id', label: '灯组编号'},
    {key: 'site_name', label: '台账点位'},
    {key: 'revision_no', label: '采用修订号'},
  ], payload.trace, {caption: '当日有效工单明细', empty: '当日无有效工单（撤单工单不计入）。', pageSize: 20});
}

async function revisionSection(refresh) {
  const message = feedback();
  const batches = await workspace.revisionBatches();
  const sample = button('下载修订 CSV 表头', {class: 'quiet',
    onClick: () => {
      const blob = new Blob([`﻿${REVISION_HEADER}\r\n`], {type: 'text/csv;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const anchor = el('a', {href: url, download: 'hotline-revisions-header.csv'});
      document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    }});
  const submit = button('导入修订并触发看板重算', {type: 'submit', class: 'primary', 'data-testid': 'upload-revision'});
  const form = session.canWrite && el('form', {class: 'fields',
    onSubmit: handler(submit, message, async event => {
      const data = new FormData(event.target);
      const file = data.get('source');
      if (!file?.size) throw new Error('请选择一个修订 CSV 文件');
      if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
      const result = await workspace.uploadRevisions({filename: file.name, csv_text: await file.text()});
      message.textContent = result.created
        ? `批次 #${result.sequence} 已入库：应用 ${result.applied_count} 条，跳过旧修订 ${result.skipped_count} 条，看板正在后台重算。`
        : `批次 #${result.sequence} 内容已存在，未重复应用。`;
      await refresh();
    })},
    el('label', {class: 'field'}, el('span', {}, '热线修订 CSV'),
      el('input', {name: 'source', type: 'file', accept: '.csv,text/csv', required: true})),
    p(`列：${REVISION_HEADER}。op 支持 upsert/cancel（兼容「修订/撤单」）；撤单行留空灯组与日期。`, 'subtle'),
    submit);
  const batchTable = table([
    {key: 'sequence', label: '批次号'},
    {key: 'filename', label: '文件'},
    {key: 'row_count', label: '行数'},
    {label: '应用/跳过', render: row => `${row.applied_count} / ${row.skipped_count}`},
    {label: '看板任务', render: row => pill(JOB_STATE[row.job_status] || row.job_status, row.job_status)},
    {key: 'author', label: '操作者'},
    {label: '入库时间', render: row => date(row.created_at)},
  ], batches, {caption: '热线补传批次（按批次号认最新）', empty: '还没有修订批次，当前看板来自初始台账。'});
  return panel('热线工单修订', p('乱序补传按修订号认最新；撤单是高修订号墓碑，重放旧记录不会复活工单。', 'subtle'),
    sample, form, message, batchTable);
}

async function currentSection(refresh) {
  const boards = await workspace.currentBoards();
  const picker = el('select', {id: 'board-picker',
    onChange: event => {
      const [day, district] = event.target.value.split('||');
      location.hash = `#board/${encodeURIComponent(day)}/${encodeURIComponent(district || '')}`;
    }},
    boards.map(item => el('option', {value: `${item.report_day}||${item.district}`},
      `${item.report_day} · ${districtLabel(item.district)} · v${item.version_no}`)));
  const list = table([
    {label: '报告日', render: row => link(row.report_day,
      `#board/${encodeURIComponent(row.report_day)}/${encodeURIComponent(row.district)}`)},
    {label: '片区', render: row => districtLabel(row.district)},
    {key: 'version_no', label: '当前版本'},
    {key: 'headline', label: '看板摘要'},
    {label: '生成时间', render: row => date(row.created_at)},
    {label: '下载', render: row => button('版本 CSV', {class: 'quiet',
      onClick: () => workspace.download(`/board/versions/${row.id}/export`,
        `board-${row.report_day}-v${row.version_no}.csv`)})},
  ], boards, {caption: '当前生效看板（随修订原子切换）', empty: '看板尚未生成，请等待后台重算。'});
  return panel('当前每日巡检看板', el('div', {class: 'actions'}, el('label', {for: 'board-picker', class: 'field'},
    el('span', {}, '快速跳转'), picker)), list);
}

async function detailSection(route, refresh) {
  const day = route.id ? decodeURIComponent(route.id) : null;
  const district = route.offset == null ? '' : decodeURIComponent(route.offset);
  if (!day) return null;
  const versions = await workspace.boardVersions(day, district);
  if (!versions.length) return panel(`${day} · ${districtLabel(district)}`, empty('该日期与片区还没有看板版本。'));
  const current = versions.find(item => item.is_current) || versions[0];
  const detail = await workspace.boardVersion(current.id);
  const payload = detail.payload;
  const message = feedback();
  const handoverBtn = button('生成交班报告（钉住此版本）', {class: 'primary', 'data-testid': 'handover'});
  const handovers = await workspace.handovers();
  const pinned = handovers.find(item => item.report_day === day && item.district === (district || ''));
  const handoverBox = el('div', {class: 'actions'}, handoverBtn,
    pinned && link(`查看已交班报告 #${pinned.id}（v${current.version_no} 内容已固定）`,
      `#board/${encodeURIComponent(day)}/${encodeURIComponent(district || '')}`));
  handoverBtn.addEventListener('click', handler(handoverBtn, message, async () => {
    const note = prompt('填写夜班交接事项（可留空）：', '') ?? '';
    const result = await workspace.createHandover({report_day: day, district, shift_note: note});
    message.textContent = result.created
      ? `交班报告 #${result.id} 已生成并钉住 v${current.version_no}，之后修订不影响该下载件。`
      : `该版本已有交班报告 #${result.id}，直接复用钉版。`;
    await refresh();
  }));
  const versionPicker = table([
    {key: 'version_no', label: '版本'},
    {label: '状态', render: row => row.is_current ? pill('当前版', 'current') : pill('历史版', 'history')},
    {key: 'headline', label: '摘要'},
    {label: '生成时间', render: row => date(row.created_at)},
    {label: '下载', render: row => button('下载该版 CSV', {class: 'quiet',
      onClick: () => workspace.download(`/board/versions/${row.id}/export`,
        `board-${day}-v${row.version_no}.csv`)})},
  ], versions, {caption: '版本历史（旧版不可变，可随时回查下载）'});
  const pollUntilDone = () => {
    const status = el('p', {role: 'status'}, '该范围有新的修订批次，看板正在后台重算，完成后自动刷新。');
    poll(() => workspace.boardVersions(day, district),
      rows => rows.some(row => row.is_current && row.id !== current.id),
      () => location.hash === `#board/${encodeURIComponent(day)}/${encodeURIComponent(district || '')}`)
      .then(updated => { if (updated && status.isConnected) refresh(); })
      .catch(error => { if (status.isConnected) status.textContent = error.message; });
    return status;
  };
  return el('div', {},
    panel(`${day} · ${districtLabel(district)} · 看板 v${current.version_no}`,
      current.is_current ? pill('当前版', 'current') : pill('历史版', 'history'),
      h(2, payload.headline), p(payload.note, 'intro-copy'),
      metrics(payload.metrics), boardTable(payload), traceTable(payload),
      (payload.sections || []).map(item => panel(item.title, p(item.text))),
      handoverBox, message),
    panel('版本与交班', versionPicker, pinned && el('div', {class: 'filename'},
      p(`已交班报告 #${pinned.id}：钉住版本 #${pinned.board_version_id}，交班时间 ${date(pinned.created_at)}`, 'subtle'),
      button('下载交班时那版 CSV', {class: 'quiet',
        onClick: () => workspace.download(`/board/handovers/${pinned.id}/export`,
          `handover-${day}-${pinned.id}.csv`)})),
      detail.pinned_by_handover && p(`此版本已被交班报告 #${detail.pinned_by_handover} 钉住，内容永久保留。`, 'subtle')),
    (await workspace.revisionBatches()).some(b => ['queued', 'running'].includes(b.job_status)) && pollUntilDone());
}

export async function boardPage(route, refresh) {
  const [currentList, handovers] = await Promise.all([
    workspace.currentBoards(), workspace.handovers(),
  ]);
  const handoverPanel = panel('交班报告（已钉版）', table([
    {key: 'id', label: '报告号'},
    {key: 'report_day', label: '报告日'},
    {label: '片区', render: row => districtLabel(row.district)},
    {key: 'board_version_id', label: '钉住版本'},
    {key: 'shift_note', label: '交接事项'},
    {key: 'author', label: '交班人'},
    {label: '交班时间', render: row => date(row.created_at)},
    {label: '下载当时版本', render: row => button('下载 CSV', {class: 'quiet',
      onClick: () => workspace.download(`/board/handovers/${row.id}/export`,
        `handover-${row.report_day}-${row.id}.csv`)})},
  ], handovers, {caption: '交班后修订不改变这些报告', empty: '还没有交班报告。'}));
  const detail = await detailSection(route, refresh);
  return el('div', {class: 'two-col'},
    el('div', {class: 'wide-panel'},
      await revisionSection(refresh),
      detail || (currentList.length ? currentSection(refresh)
        : panel('每日巡检看板', empty('后台正在生成首版看板，请稍候刷新。')))),
    el('aside', {class: 'sidebar-panel'},
      await currentSection(refresh), handoverPanel));
}
