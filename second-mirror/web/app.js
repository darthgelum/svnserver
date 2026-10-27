(() => {
  const root = document.getElementById('mirror-status');
  const $ = id => root.querySelector('#sm-' + id);
  const demo = window.MIRROR_DEMO || null;
  let data = demo, view = 'overview', filter = 'all', selected = null;
  const states = { ready: 'Синхронизирован', syncing: 'Передача', error: 'Ошибка', waiting: 'Ожидает' };
  const rev = value => value === null || value === undefined ? '—' : 'r' + value;
  const age = value => {
    if (!value) return 'Ещё не было';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - value));
    return seconds < 60 ? seconds + ' с назад' : seconds < 3600 ? Math.floor(seconds / 60) + ' мин назад' : Math.floor(seconds / 3600) + ' ч назад';
  };
  const stamp = value => value ? new Date(value * 1000).toLocaleString('ru-RU') : 'Нет данных';
  const bytes = value => value === null || value === undefined ? '—' : value >= 1024 ** 4 ? (value / 1024 ** 4).toFixed(1) + ' ТБ' : value >= 1073741824 ? (value / 1073741824).toFixed(1) + ' ГБ' : (value / 1048576).toFixed(1) + ' МБ';
  const origin = () => demo ? 'http://mirror.example:18080' : location.origin;
  function el(tag, cls, text) { const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined) item.textContent = text; return item; }
  function button(text, action, cls = 'sm-button') { const b = el('button', cls, text); b.type = 'button'; b.addEventListener('click', action); return b; }
  function badge(text, state = '') { return el('span', 'sm-badge ' + state, text); }
  function notify(message) { $('toast').textContent = message; $('toast').hidden = false; setTimeout(() => { $('toast').hidden = true; }, 3500); }
  async function copy(value) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else { const input = el('textarea'); input.value = value; root.append(input); input.select(); const ok = document.execCommand('copy'); input.remove(); if (!ok) throw new Error(); }
      notify('URL скопирован');
    } catch (_) { notify('Копирование недоступно в этом окне. URL показан в карточке репозитория.'); }
  }
  function setView(next) {
    view = next;
    root.querySelectorAll('.sm-view').forEach(panel => { panel.hidden = panel.id !== 'sm-' + view; });
    root.querySelectorAll('[data-view]').forEach(b => { b.classList.toggle('active', b.dataset.view === view); });
    $('title').textContent = { overview: 'Всё зеркало на одном экране.', users: 'Кто имеет доступ к SVN.', logs: 'Что происходит с зеркалом.', connection: 'Связь с главным сервером.' }[view];
  }
  function showDetail(repo) { selected = repo.name; renderDetail(); $('detail').hidden = false; }
  function renderDetail() {
    const repo = data.repositories.find(r => r.name === selected);
    const pane = $('detail'); pane.replaceChildren(); pane.hidden = !repo;
    if (!repo) return;
    const head = el('div', 'sm-detail-head'); head.append(el('h2', '', repo.name), button('Закрыть', () => { selected = null; pane.hidden = true; }, 'sm-button sm-quiet')); pane.append(head);
    for (const [label, url] of [['Локальный URL', origin() + repo.path], ['Главный сервер', repo.master_url]]) {
      pane.append(el('small', '', label)); const row = el('div', 'sm-detail-url'); row.append(el('code', '', url), button('Копировать', () => copy(url))); pane.append(row);
    }
    const grid = el('div', 'sm-detail-grid');
    const info = [['Подтверждено svnsync', rev(repo.last_merged_revision)], ['Проверка главной ревизии', stamp(repo.master_checked_at)], ['Последняя успешная синхронизация', stamp(repo.last_success_at)], ['Текущая ревизия в передаче', rev(repo.progress.copying_revision)], ['Данные текущей транзакции', bytes(repo.progress.transaction_bytes)], ['Автовосстановление блокировки', stamp(repo.recovery)]];
    info.forEach(([label, value]) => { const cell = el('div'); cell.append(el('span', '', label), el('strong', '', value)); grid.append(cell); }); pane.append(grid);
    if (repo.error) pane.append(el('div', 'sm-errorbox', (repo.state === 'syncing' ? 'Предыдущая ошибка: ' : '') + repo.error));
    pane.append(el('p', 'sm-sectionnote', repo.published ? 'URL опубликован для клиентов. Новые ревизии доступны после докачки.' : 'URL ещё не опубликован: требуется успешный проход синхронизации.'));
    pane.append(button('События этого репозитория →', () => { $('log-repo').value = repo.name; setView('logs'); renderLogs(); }, 'sm-button sm-quiet'));
  }
  function renderRepos() {
    const needle = $('search').value.toLowerCase(); const body = $('repositories'); body.replaceChildren();
    const repos = data.repositories.filter(r => r.name.toLowerCase().includes(needle) && (filter === 'all' || (filter === 'attention' ? r.state !== 'ready' : r.state === 'syncing')));
    for (const repo of repos) {
      const tr = el('tr');
      const name = el('td'); name.append(button(repo.name, () => showDetail(repo), 'sm-repo-name'), el('span', 'sm-repo-url', repo.path));
      const status = el('td'); status.append(badge(states[repo.state], repo.state));
      const revisions = el('td', 'sm-revision'); revisions.append(document.createTextNode(rev(repo.local_revision) + ' '), el('span', '', '/ ' + rev(repo.master_revision)));
      const lag = el('td', repo.lag ? 'sm-number-warn' : '', repo.lag === null ? '—' : repo.lag === 0 ? 'Нет' : '+' + repo.lag + ' рев.');
      const last = el('td', '', age(repo.last_success_at)); const actions = el('td'); actions.append(button('URL', () => { copy(origin() + repo.path); showDetail(repo); }, 'sm-copy'));
      tr.append(name, status, revisions, lag, last, actions); body.append(tr);
    }
    if (!repos.length) { const tr = el('tr'), td = el('td', '', 'По этому фильтру репозиториев нет'); td.colSpan = 6; tr.append(td); body.append(tr); }
    $('repo-summary').textContent = data.repositories.length + ' обнаружено · ' + data.repositories.filter(r => r.published).length + ' опубликовано для клиентов';
  }
  function renderTransfer() {
    const box = $('transfer'); box.replaceChildren();
    const active = data.repositories.find(r => r.state === 'syncing');
    const top = el('div', 'sm-transfer-top');
    top.append(el('div', 'sm-transfer-title', active ? 'Сейчас передаётся · ' + active.name : 'Текущая передача'), badge(active ? 'Worker работает' : data.worker.state === 'running' ? 'Проверка репозиториев' : 'Worker остановлен', active ? 'syncing' : 'waiting')); box.append(top);
    if (!active) { box.append(el('p', 'sm-transfer-meta', 'Новая передача появится здесь автоматически. Состояние служб — в разделе «Соединение».')); return; }
    const p = active.progress;
    box.append(el('div', 'sm-transfer-meta', 'Ревизия ' + (p.copying_revision || 'уточняется') + ' · транзакция ' + bytes(p.transaction_bytes) + ' · без активности ' + (p.idle_seconds ?? '—') + ' с'));
    const ratio = active.master_revision ? Math.min(100, Math.max(0, (active.last_merged_revision || 0) / active.master_revision * 100)) : 0;
    const track = el('div', 'sm-progress'); track.setAttribute('role', 'progressbar'); track.setAttribute('aria-label', 'Подтверждённые ревизии ' + active.name); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', String(active.master_revision || 0)); track.setAttribute('aria-valuenow', String(active.last_merged_revision || 0)); const fill = el('span'); fill.style.width = ratio + '%'; track.append(fill); box.append(track);
    const foot = el('div', 'sm-progress-foot'); foot.append(el('span', '', 'Подтверждено ' + rev(active.last_merged_revision) + ' из ' + rev(active.master_revision)), el('span', '', 'В передаче ' + Math.floor((p.elapsed_seconds || 0) / 60) + ' мин · оценка оставшегося времени недоступна')); box.append(foot);
  }
  function renderLogs() {
    const events = data.events.filter(row => ($('log-level').value === 'all' || row.level === $('log-level').value) && ($('log-repo').value === 'all' || row.repository === $('log-repo').value));
    $('log-output').textContent = events.map(row => stamp(row.time) + '  ' + row.level.toUpperCase() + '  ' + (row.repository || 'SYSTEM') + '  ' + row.message).join('\n') || 'Событий по выбранному фильтру пока нет.';
    $('apache-output').textContent = data.apache_errors.join('\n') || 'Зафиксированных ошибок Apache пока нет.';
    const recent = $('recent-events'); recent.replaceChildren();
    data.events.slice(-4).reverse().forEach(row => { const line = el('div', 'sm-event'); line.append(el('time', '', new Date(row.time * 1000).toLocaleTimeString('ru-RU')), el('span', row.level, row.level === 'warning' ? 'Восстановление' : row.level === 'error' ? 'Ошибка' : 'Событие'), el('span', 'sm-event-message', (row.repository ? row.repository + ' · ' : '') + row.message)); recent.append(line); });
    if (!data.events.length) recent.append(el('p', 'sm-sectionnote', 'Новые события появятся после запуска обновлённого worker.'));
  }
  function render() {
    if (!data) return;
    const now = Date.now() / 1000, online = data.connection.ok && !data.connection.stale;
    $('host').textContent = demo ? 'mirror.example:18080' : location.host;
    $('viewer').textContent = 'Вход: ' + data.viewer + ' · Только просмотр';
    $('updated').textContent = 'Обновлено ' + new Date(data.generated_at * 1000).toLocaleTimeString('ru-RU');
    $('link-badge').textContent = online ? '● Главный сервер доступен' : '● Нет свежего подтверждения связи';
    $('link-badge').className = 'sm-badge ' + (online ? '' : 'error');
    const ready = data.repositories.filter(r => r.state === 'ready').length;
    const failed = data.repositories.filter(r => r.state === 'error').length;
    const summary = $('summary'); summary.replaceChildren();
    const stats = [['Готовы к работе', ready + ' / ' + data.repositories.length, 'По последним проверкам', ''], ['Требуют внимания', String(failed), 'Ошибки синхронизации', failed ? 'error' : ''], ['Аккаунты SVN', String(data.users.length), 'Права проверены ' + age(data.auth_checked_at), ''], ['Свободно на диске', bytes(data.disk.free), Math.round(data.disk.free / data.disk.total * 100) + '% от общего объёма', '']];
    stats.forEach(([label, value, sub, cls]) => { const card = el('div', 'sm-stat ' + cls); card.append(el('span', 'sm-stat-label', label), el('strong', 'sm-stat-value', value), el('span', 'sm-stat-sub', sub)); summary.append(card); });
    renderRepos(); renderTransfer();
    $('user-count').textContent = data.users.length;
    $('auth-time').textContent = 'Последняя успешная проверка: ' + stamp(data.auth_checked_at);
    const users = $('users-table'); users.replaceChildren();
    data.users.forEach(user => { const tr = el('tr'); tr.append(el('td', '', user.login), el('td', '', user.groups.join(', ') || 'Без именованных групп'), el('td', '', 'USVN / главный сервер')); users.append(tr); });
    const repoFilter = $('log-repo'), previous = repoFilter.value; repoFilter.replaceChildren();
    const all = el('option', '', 'Все репозитории'); all.value = 'all'; repoFilter.append(all);
    data.repositories.forEach(r => { const option = el('option', '', r.name); option.value = r.name; repoFilter.append(option); }); repoFilter.value = [...repoFilter.options].some(o => o.value === previous) ? previous : 'all';
    renderLogs(); if (selected) renderDetail();
    const connection = $('connection-details'); connection.replaceChildren();
    const staleWorker = now - (data.worker.checked_at || 0) > 120;
    const info = [['Главный сервер', data.master || 'Указан отдельно для репозиториев'], ['Проверка соединения', stamp(data.connection.checked_at)], ['Последний успешный контакт', stamp(data.connection.last_success_at)], ['Служба sync', staleWorker ? 'Нет свежего heartbeat' : data.worker.state === 'running' ? 'Работает' : 'Остановлена'], ['Автоматическое восстановление', (data.worker.features || []).includes('auto-recovery') ? 'Включено · с исключительной блокировкой' : 'Worker требует обновления'], ['Проверка аккаунтов и прав', stamp(data.auth_checked_at)], ['Режим блокировок файлов', 'Актуальное состояние с главного сервера'], ['Ошибка соединения', data.connection.error || 'Не зафиксирована']];
    info.forEach(([label, value]) => { const cell = el('div', 'sm-connection-item'); cell.append(el('span', '', label), el('strong', '', value)); connection.append(cell); });
    if (demo) { $('demo').hidden = false; $('demo-controls').hidden = false; }
  }
  async function refresh() {
    if (demo) { data.generated_at = Date.now() / 1000; render(); return; }
    // LIVE_ONLY_START
    try {
      const response = await fetch('/svn-mirror-status/api', { cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? 'Нет доступа к статусной странице. Проверьте вход и STATUS_USERS.' : 'Статусный API ответил HTTP ' + response.status);
      data = await response.json(); $('notice').hidden = true; render();
    } catch (error) { $('notice').hidden = false; $('notice').textContent = error.message + (data ? ' Показаны последние полученные данные.' : ''); }
    // LIVE_ONLY_END
  }
  root.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));
  root.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => { filter = b.dataset.filter; root.querySelectorAll('[data-filter]').forEach(other => { const active = other === b; other.classList.toggle('active', active); other.setAttribute('aria-pressed', String(active)); }); if (data) renderRepos(); }));
  root.querySelectorAll('[data-open-logs]').forEach(b => b.addEventListener('click', () => setView('logs')));
  $('search').addEventListener('input', () => { if (data) renderRepos(); });
  $('refresh').addEventListener('click', refresh);
  $('log-level').addEventListener('change', () => { if (data) renderLogs(); });
  $('log-repo').addEventListener('change', () => { if (data) renderLogs(); });
  $('download').addEventListener('click', () => { const blob = new Blob([$('log-output').textContent + '\n\nAPACHE\n' + $('apache-output').textContent], { type: 'text/plain;charset=utf-8' }); const url = URL.createObjectURL(blob), a = el('a'); a.href = url; a.download = 'svn-mirror-log.txt'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
  $('offline').addEventListener('click', () => { if (!demo) return; data.connection.ok = !data.connection.ok; data.connection.stale = false; data.connection.error = data.connection.ok ? null : 'Discovery failed: connection unavailable'; data.connection.checked_at = Date.now() / 1000; $('offline').textContent = data.connection.ok ? 'Смоделировать обрыв связи' : 'Восстановить связь'; render(); });
  $('advance').addEventListener('click', () => { if (!demo) return; const repo = data.repositories.find(r => r.state === 'syncing'); if (repo) { repo.local_revision = Math.min(repo.master_revision, repo.local_revision + 30); repo.last_merged_revision = repo.local_revision; repo.lag = repo.master_revision - repo.local_revision; repo.progress.copying_revision = repo.local_revision + 1; repo.progress.elapsed_seconds += 30; if (!repo.lag) { repo.state = 'ready'; repo.published = true; repo.last_success_at = Date.now() / 1000; } data.events.push({ time: Date.now() / 1000, level: 'info', repository: repo.name, message: 'Продвижение до ' + rev(repo.local_revision) + ' · демонстрационные данные' }); render(); } });
  refresh();
  if (!demo) setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
