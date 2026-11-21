export const pages = ['overview', 'inputs', 'run', 'results', 'board', 'review', 'delivery'];

export function route() {
  const [page, id, offset] = location.hash.slice(1).split('/');
  if (page === 'board') {
    // 看板页第三段是片区（可能为中文，需保留原样），不做数字偏移转换。
    return {page, id: id || null, offset: offset === undefined ? null : offset};
  }
  return {page:pages.includes(page) ? page : 'overview', id:Number(id) || null, offset:Math.max(0, Number(offset) || 0)};
}
