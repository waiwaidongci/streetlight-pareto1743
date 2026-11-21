import {request, saveFile} from '../api/http.js';
import {session} from './session.js';

const call = (path, body) => request(path, {token:session.token, body});
export const workspace = {
  spec: null,
  datasets: [],
  runs: [],
  async init() { this.spec = await request('/spec'); },
  async refresh() { [this.datasets, this.runs] = await Promise.all([call('/datasets'), call('/runs')]); },
  getDataset: (id, offset = 0) => call(`/datasets/${id}?offset=${offset}`),
  getRun: id => call(`/runs/${id}`),
  upload: data => call('/datasets', data),
  start: data => call('/runs', data),
  review: (id, data) => call(`/runs/${id}/review`, data),
  audit: () => call('/audit'),
  schedules: () => call('/schedules'),
  deliveries: () => call('/deliveries'),
  schedule: data => call('/schedules', data),
  disable: id => call(`/schedules/${id}/disable`, {}),
  revisionBatches: () => call('/board/revisions'),
  boardIncidents: () => call('/board/incidents'),
  currentBoards: () => call('/board/current'),
  boardVersions: (reportDay, district = '') =>
    call(`/board/versions?report_day=${encodeURIComponent(reportDay)}&district=${encodeURIComponent(district)}`),
  boardVersion: id => call(`/board/versions/${id}`),
  uploadRevisions: data => call('/board/revisions', data),
  handovers: () => call('/board/handovers'),
  createHandover: data => call('/board/handovers', data),
  download: (path, filename) => saveFile(path, session.token, filename),
};
