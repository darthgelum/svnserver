(() => {
  const now = Date.now() / 1000;
  const names = ['Example', 'Animation', 'Art', 'Environment', 'Rendering', 'Characters', 'Props', 'Lighting', 'Materials', 'Cinematics', 'Audio'];
  const heads = [2942, 1240, 63, 61, 329, 71, 56, 134, 20, 126, 4];
  window.MIRROR_DEMO = {
    mode: 'demo', generated_at: now, label: 'Second mirror', viewer: 'admin', master: 'https://svn.example:18443',
    connection: { ok: true, stale: false, checked_at: now - 4, last_success_at: now - 4, error: null },
    worker: { state: 'running', checked_at: now - 2, features: ['auto-recovery', 'idle-watchdog', 'live-progress'] },
    disk: { total: 12 * 1024 ** 4, free: 3.8 * 1024 ** 4, used: 8.2 * 1024 ** 4 },
    auth_checked_at: now - 4,
    users: [{ login: 'admin', groups: ['admins'] }, { login: 'alex', groups: ['artists', 'animation'] }, { login: 'maria', groups: ['artists'] }, { login: 'svnmirror', groups: [] }],
    groups: { admins: ['admin'], artists: ['alex', 'maria'], animation: ['alex'] },
    repositories: names.map((name, index) => {
      const head = heads[index], state = index === 0 ? 'syncing' : index === 1 ? 'error' : 'ready', local = index === 0 ? 2748 : index === 1 ? 1198 : head;
      return { name, path: '/svn/' + name, master_url: 'https://svn.example:18443/svn/' + name,
        state, published: index > 1, local_revision: local, last_merged_revision: local, master_revision: head,
        lag: head - local, last_success_at: index < 2 ? now - 4200 : now - 18 - index * 6, checked_at: now - 10,
        master_checked_at: now - 4, error: index === 1 ? 'Не удалось установить соединение с главным сервером. Повторная попытка — следующий проход.' : null,
        progress: { state: index === 0 ? 'running' : 'completed', phase: 'synchronize', checked_at: now - 2, elapsed_seconds: 1370, idle_seconds: 0, copying_revision: index === 0 ? 2749 : null, transaction_bytes: index === 0 ? 1.24 * 1024 ** 3 : null }, recovery: index === 0 ? now - 1370 : null };
    }),
    events: [{ time: now - 1370, level: 'warning', repository: 'Example', message: 'Служебная блокировка восстановлена. Незавершённая ревизия сохранена.' }, { time: now - 45, level: 'error', repository: 'Animation', message: 'Главный сервер не ответил. Передача будет повторена автоматически.' }, { time: now - 18, level: 'info', repository: 'Audio', message: 'r4/4; lag=0' }, { time: now - 2, level: 'info', repository: 'Example', message: 'Копируется ревизия 2749; в текущей транзакции 1.24 ГБ' }],
    apache_errors: ['[demo] Здесь отображаются ошибки Apache, TLS и авторизации. Это демонстрационные данные.']
  };
})();
