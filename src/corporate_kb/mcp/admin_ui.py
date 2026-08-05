# ruff: noqa: E501, RUF001
"""Self-contained administration page served by the FastMCP HTTP process."""

ADMIN_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Corporate RAG Admin</title>
  <style>
    :root { color-scheme:dark; --bg:#020704; --bg-soft:#06110a; --panel:#07160d;
      --panel-2:#091d11; --line:#164b28; --line-hot:#21c45b; --text:#eaffef;
      --muted:#78a986; --accent:#21e56b; --accent-hot:#72ff9d; --accent-deep:#0b7d35;
      --danger:#ff426d; --shadow:0 0 34px #00d95712; }
    * { box-sizing:border-box }
    html { min-height:100%; background:var(--bg) }
    body { min-height:100vh; margin:0; overflow-x:hidden; font:13px/1.5 ui-monospace,
      SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace; background:
      linear-gradient(#07180c55 1px,transparent 1px),linear-gradient(90deg,#07180c55 1px,transparent 1px),
      radial-gradient(circle at 12% 8%,#0c5c282e,transparent 30%),
      radial-gradient(circle at 90% 82%,#00c8531c,transparent 27%),var(--bg);
      background-size:38px 38px,38px 38px,auto,auto,auto; color:var(--text) }
    body::before { content:""; position:fixed; z-index:999; pointer-events:none; inset:0;
      background:repeating-linear-gradient(0deg,transparent 0 3px,#001b080f 4px 5px);
      mix-blend-mode:screen; opacity:.6 }
    body::after { content:""; position:fixed; z-index:-1; pointer-events:none; width:42vw; height:2px;
      top:17%; left:-45vw; background:linear-gradient(90deg,transparent,var(--accent),transparent);
      box-shadow:0 0 20px var(--accent); animation:signal-sweep 8s linear infinite }
    main { position:relative; max-width:1220px; margin:auto; padding:34px 28px 60px }
    main::before { content:"CBNII::KNOWLEDGE_NODE / STATUS:CONNECTED"; display:block; margin-bottom:18px;
      color:#4fcf75; font-size:10px; letter-spacing:.18em; opacity:.76 }
    h1,h2 { position:relative; margin:0 0 14px; font-family:inherit; text-transform:uppercase }
    h1 { font-size:26px; line-height:1.05; letter-spacing:.08em; text-shadow:0 0 22px #27e86d55 }
    h2 { display:flex; align-items:center; gap:10px; font-size:16px; letter-spacing:.09em }
    h2::before { content:"//"; color:var(--accent); text-shadow:0 0 12px var(--accent) }
    .muted { color:var(--muted) } .hidden { display:none!important }
    .panel { position:relative; isolation:isolate; overflow:hidden; background:
      linear-gradient(135deg,#0a2112f2,#061009f5 68%); border:1px solid var(--line);
      border-radius:2px; padding:22px; margin:18px 0; box-shadow:var(--shadow),inset 0 1px #48ff7d14;
      clip-path:polygon(0 0,calc(100% - 17px) 0,100% 17px,100% 100%,17px 100%,0 calc(100% - 17px)) }
    .panel::before { content:""; position:absolute; z-index:-1; width:180px; height:180px;
      right:-100px; top:-100px; border:1px solid #27e86d1d; border-radius:50%;
      box-shadow:0 0 0 20px #27e86d08,0 0 0 40px #27e86d05 }
    .panel::after { content:""; position:absolute; top:0; left:0; width:120px; height:2px;
      background:linear-gradient(90deg,var(--accent),transparent); box-shadow:0 0 12px var(--accent) }
    .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px }
    .card { position:relative; overflow:hidden; min-height:98px; background:
      linear-gradient(145deg,#0b2414e8,#050d08); border:1px solid #155d2d; border-radius:1px;
      padding:15px 16px; box-shadow:inset 0 0 24px #00c85309 }
    .card::before { content:""; position:absolute; right:-18px; top:-18px; width:48px; height:48px;
      border:1px solid #20e76855; transform:rotate(45deg) }
    .card::after { content:""; position:absolute; left:0; bottom:0; width:37%; height:2px;
      background:var(--accent); box-shadow:0 0 10px var(--accent) }
    .card .muted { font-size:10px; letter-spacing:.12em; text-transform:uppercase }
    .value { color:var(--accent-hot); font-size:27px; font-weight:800; margin-top:7px;
      text-shadow:0 0 18px #21e56b66 }
    label { display:block; margin:10px 0 6px; color:#9cd5a9; font-size:11px;
      letter-spacing:.07em; text-transform:uppercase }
    input,textarea,select,button { width:100%; border:1px solid #1b6333; border-radius:1px;
      outline:none; background:#030b06e8; color:var(--text); padding:10px 12px; font:inherit;
      transition:border-color .18s,box-shadow .18s,background .18s,transform .18s }
    input:focus,textarea:focus,select:focus { border-color:var(--accent); background:#06140b;
      box-shadow:0 0 0 2px #16db5b18,0 0 22px #11d05218 }
    input[type=file]::file-selector-button { margin:-10px 12px -10px -12px; padding:10px 12px;
      border:0; border-right:1px solid #1b6333; background:#0d321b; color:#baffca; font:inherit }
    textarea { min-height:110px; resize:vertical; font-family:inherit }
    button { position:relative; width:auto; cursor:pointer; overflow:hidden; background:
      linear-gradient(135deg,#0fb84b,#08752e); border-color:#35ef73; color:#effff3;
      font-weight:800; letter-spacing:.05em; text-transform:uppercase; box-shadow:0 0 18px #00d95720 }
    button::after { content:""; position:absolute; inset:0; background:
      linear-gradient(105deg,transparent 30%,#ffffff45 49%,transparent 68%);
      transform:translateX(-130%); transition:transform .42s }
    button:hover { border-color:#8effaa; background:linear-gradient(135deg,#17d459,#0b8e39);
      box-shadow:0 0 24px #1ee86642; transform:translateY(-1px) }
    button:hover::after { transform:translateX(130%) }
    button.secondary { background:#0b2414; border-color:#28743e; color:#9decb1 }
    button.danger { background:#4a0d1d; border-color:#b62549; color:#ffb4c5; box-shadow:none }
    button:disabled { opacity:.45; cursor:wait; transform:none }
    .row { display:flex; gap:11px; align-items:end; flex-wrap:wrap }
    .row > div { flex:1; min-width:170px }
    table { width:100%; border-collapse:collapse; margin-top:6px; font-size:12px }
    th,td { text-align:left; padding:10px 9px; border-bottom:1px solid #123d21; vertical-align:top }
    th { color:#62db82; font-size:10px; letter-spacing:.09em; text-transform:uppercase;
      background:#0b28161f }
    tbody tr { transition:background .16s,box-shadow .16s }
    tbody tr:hover { background:#0e351b66; box-shadow:inset 3px 0 var(--accent) }
    code { color:var(--accent-hot); text-shadow:0 0 10px #21e56b44 }
    .status { margin-left:10px; color:var(--muted) }
    .error { color:var(--danger); white-space:pre-wrap } .ok { color:var(--accent) }
    #login { max-width:470px; margin:10vh auto; padding:28px 30px 24px }
    #login::after { content:"SECURE ACCESS // LEVEL 04"; position:absolute; top:9px; right:25px;
      width:auto; height:auto; background:none; box-shadow:none; color:#4fcc72; font-size:9px;
      letter-spacing:.12em }
    #login h1 { padding-top:16px; color:#eaffef }
    #login h1::before { content:"◈"; color:var(--accent); margin-right:10px;
      text-shadow:0 0 16px var(--accent) }
    #dashboard > .row:first-child { position:relative; padding:18px 20px; margin-bottom:15px;
      border-left:3px solid var(--accent); background:linear-gradient(90deg,#0c321a9c,transparent 72%) }
    #dashboard > .row:first-child::after { content:"NODE_01  /  ONLINE"; position:absolute;
      right:20px; top:14px; color:var(--accent); font-size:9px; letter-spacing:.14em }
    #dashboard > .row:first-child h1 { color:#f0fff4 }
    #dashboard > .row:first-child .muted { letter-spacing:.08em; text-transform:uppercase; font-size:10px }
    .load-shell { display:grid; grid-template-columns:minmax(270px,.8fr) minmax(420px,1.7fr);
      gap:26px; align-items:center; margin-top:20px }
    .gauge-zone { position:relative; display:grid; place-items:center; min-height:260px;
      border:1px solid #174b28; background:radial-gradient(circle,#103a1d88,transparent 64%) }
    .gauge-zone::before,.gauge-zone::after { content:""; position:absolute; inset:14px;
      border:1px solid #20de6244; clip-path:polygon(0 0,31% 0,31% 1px,69% 1px,69% 0,100% 0,
        100% 31%,calc(100% - 1px) 31%,calc(100% - 1px) 69%,100% 69%,100% 100%,69% 100%,
        69% calc(100% - 1px),31% calc(100% - 1px),31% 100%,0 100%,0 69%,1px 69%,1px 31%,0 31%) }
    .gauge-zone::after { inset:28px; border-color:#37ff7644; animation:gauge-frame 8s linear infinite }
    .load-gauge { --load-angle:0deg; position:relative; width:196px; height:196px; border-radius:50%;
      display:grid; place-items:center; background:conic-gradient(from 210deg,var(--accent-hot) 0 var(--load-angle),#0b2d17 var(--load-angle) 360deg);
      filter:drop-shadow(0 0 15px #14df5b38); transition:background .5s ease }
    .load-gauge::before { content:""; position:absolute; inset:11px; border-radius:50%; background:
      radial-gradient(circle at 45% 35%,#103b1e,#040c07 68%); border:1px solid #268e45;
      box-shadow:inset 0 0 28px #00d9571c }
    .load-gauge::after { content:""; position:absolute; inset:-7px; border-radius:50%;
      border:1px dashed #37ff7666; animation:gauge-spin 15s linear infinite }
    .gauge-core { position:relative; z-index:2; text-align:center }
    .gauge-value { color:#8dffa9; font-size:42px; font-weight:900; line-height:1;
      text-shadow:0 0 25px #21e56b }
    .gauge-label { margin-top:7px; color:#6ba87b; font-size:9px; letter-spacing:.16em;
      text-transform:uppercase }
    .load-state { display:inline-flex; align-items:center; gap:7px; margin-top:14px; color:#86e99d;
      font-size:10px; letter-spacing:.12em; text-transform:uppercase }
    .pulse-dot { width:7px; height:7px; border-radius:50%; background:var(--accent);
      box-shadow:0 0 12px var(--accent); animation:status-pulse 1.2s ease-in-out infinite }
    .telemetry-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px }
    .telemetry { position:relative; min-height:98px; padding:14px; border:1px solid #174b28;
      background:linear-gradient(145deg,#0b2614,#040b06); overflow:hidden }
    .telemetry::after { content:""; position:absolute; width:38px; height:38px; right:-20px;
      bottom:-20px; border:1px solid #21e56b66; transform:rotate(45deg) }
    .telemetry-label { color:#6fa980; font-size:9px; letter-spacing:.1em; text-transform:uppercase }
    .telemetry-value { margin-top:9px; color:#c7ffd4; font-size:22px; font-weight:800;
      text-shadow:0 0 13px #19d45744 }
    .load-bars { grid-column:1/-1; padding:14px 16px; border:1px solid #174b28;
      background:#030b06aa }
    .load-bar-row { display:grid; grid-template-columns:72px 1fr 58px; gap:12px; align-items:center;
      margin:9px 0; color:#6fa980; font-size:10px; letter-spacing:.08em }
    .load-track { height:8px; overflow:hidden; border:1px solid #174b28; background:#020704 }
    .load-fill { width:0; height:100%; background:linear-gradient(90deg,#087d33,var(--accent-hot));
      box-shadow:0 0 12px var(--accent); transition:width .6s ease }
    .load-footer { display:flex; justify-content:space-between; gap:16px; margin-top:14px;
      color:#5d926b; font-size:10px; letter-spacing:.06em; text-transform:uppercase }
    .cosmic-splash { position:fixed; inset:0; z-index:1000; display:grid; place-items:center;
      overflow:hidden; background:radial-gradient(circle at 50% 55%,#0b3b1d 0,#031208 38%,#010503 76%);
      opacity:1; transition:opacity .45s ease }
    .cosmic-splash.fade-out { opacity:0 }
    .cosmic-splash::before,.cosmic-splash::after { content:""; position:absolute; inset:-40%;
      background-image:radial-gradient(circle,#fff 0 1px,transparent 1.6px);
      background-size:39px 39px; opacity:.38; animation:stars-drift 9s linear infinite }
    .cosmic-splash::after { background-size:73px 73px; opacity:.22;
      transform:rotate(23deg); animation-duration:16s; animation-direction:reverse }
    .cosmic-nebula { position:absolute; width:70vmin; height:45vmin; border-radius:50%;
      background:conic-gradient(from 120deg,#21e56b66,#0c7d3655,#9effb955,#21e56b66);
      filter:blur(70px); animation:nebula-pulse 2.4s ease-in-out infinite alternate }
    .cosmic-content { position:relative; z-index:2; text-align:center; perspective:900px }
    .planet-system { position:relative; width:280px; height:280px; margin:auto;
      display:grid; place-items:center; animation:planet-float 2.2s ease-in-out infinite }
    .planet { position:relative; z-index:2; width:205px; height:205px; border-radius:50%;
      display:grid; place-items:center; overflow:hidden;
      background:radial-gradient(circle at 32% 25%,#dbffe4 0 3%,#55f187 11%,#159545 35%,#075324 67%,#011207 100%);
      box-shadow:inset -28px -24px 45px #010a04dd,inset 20px 16px 38px #a6ffbc55,
        0 0 30px #21e56baa,0 0 75px #00c85377,0 0 140px #21e56b44;
      animation:planet-turn 5s linear infinite }
    .planet::before { content:""; position:absolute; inset:-30%;
      background:repeating-linear-gradient(118deg,transparent 0 15px,#d6ffdf2b 18px 25px,transparent 29px 46px);
      transform:rotate(-13deg); animation:cloud-bands 3.2s linear infinite }
    .planet::after { content:""; position:absolute; inset:0; border-radius:50%;
      background:radial-gradient(circle at 70% 68%,transparent 0 45%,#010a04bb 76%);
      box-shadow:inset 3px 3px 4px #fff6 }
    .planet-mark { position:relative; z-index:4; font-size:27px; font-weight:900;
      letter-spacing:.26em; margin-left:.26em; color:#f0fff4; text-shadow:0 0 8px #fff,0 0 22px #57ff88 }
    .orbit-ring { position:absolute; z-index:3; width:275px; height:92px; border:3px solid #a8ffbecc;
      border-left-color:#0b8c3b; border-right-color:#48ff7d; border-radius:50%;
      transform:rotate(-18deg); box-shadow:0 0 18px #21e56b99; animation:ring-tilt 3s ease-in-out infinite }
    .orbit-ring::after { content:""; position:absolute; width:13px; height:13px; border-radius:50%;
      background:#fff; box-shadow:0 0 12px #fff,0 0 26px #43ff79;
      left:20px; top:11px; animation:satellite-pulse .8s ease-in-out infinite alternate }
    .powered { margin-top:15px; font-size:clamp(20px,3vw,34px); font-weight:800;
      letter-spacing:.16em; text-transform:uppercase; text-shadow:0 0 18px #21e56b,0 0 42px #0e7d36 }
    .powered span { color:#72ff9d } .launch-copy { margin-top:9px; color:#8fcf9f;
      letter-spacing:.38em; text-transform:uppercase; font-size:11px }
    @keyframes signal-sweep { 0% { transform:translateX(0) } 100% { transform:translateX(150vw) } }
    @keyframes gauge-spin { to { transform:rotate(360deg) } }
    @keyframes gauge-frame { 50% { transform:rotate(1deg) scale(.98); opacity:.5 } }
    @keyframes status-pulse { 50% { transform:scale(1.6); opacity:.45 } }
    @keyframes stars-drift { to { transform:translate3d(8%,12%,0) rotate(5deg) } }
    @keyframes nebula-pulse { to { transform:scale(1.14) rotate(13deg); opacity:.72 } }
    @keyframes planet-float { 50% { transform:translateY(-12px) rotateX(4deg) } }
    @keyframes planet-turn { to { filter:hue-rotate(18deg) } }
    @keyframes cloud-bands { to { transform:translateX(42px) rotate(-13deg) } }
    @keyframes ring-tilt { 50% { transform:rotate(-13deg) scaleX(1.04) } }
    @keyframes satellite-pulse { to { transform:scale(1.55); opacity:.65 } }
    @media (max-width:850px) { .grid { grid-template-columns:repeat(2,1fr) } main { padding:20px 14px 42px }
      #dashboard > .row:first-child::after { display:none } .load-shell { grid-template-columns:1fr }
      .telemetry-grid { grid-template-columns:repeat(2,1fr) } }
    @media (max-width:520px) { .grid { grid-template-columns:1fr } .panel { padding:17px }
      .planet-system { transform:scale(.82); margin:-20px auto } .powered { padding:0 16px }
      .launch-copy { letter-spacing:.2em } .telemetry-grid { grid-template-columns:1fr }
      .load-footer { flex-direction:column } }
    @media (prefers-reduced-motion:reduce) { .cosmic-splash *,.cosmic-splash::before,
      .cosmic-splash::after { animation:none!important } }
  </style>
</head>
<body>
<section id="cosmicSplash" class="cosmic-splash hidden" aria-label="Powered by CBNII">
  <div class="cosmic-nebula"></div>
  <div class="cosmic-content">
    <div class="planet-system">
      <div class="orbit-ring"></div>
      <div class="planet"><div class="planet-mark">CBNII</div></div>
    </div>
    <div class="powered">Powered by <span>CBNII</span></div>
    <div class="launch-copy">Knowledge systems online</div>
  </div>
</section>
<main>
  <section id="login" class="panel">
    <h1>Corporate RAG Admin</h1>
    <p class="muted">Введите отдельный <code>KB_ADMIN_PASSWORD</code>.</p>
    <form id="loginForm">
      <label for="password">Пароль</label>
      <input id="password" type="password" required autocomplete="current-password">
      <p><button type="submit">Войти</button></p>
      <div id="loginError" class="error"></div>
    </form>
  </section>

  <section id="dashboard" class="hidden">
    <div class="row"><div><h1>Corporate RAG</h1><span class="muted">Управление индексом и MCP</span></div>
      <button id="logout" class="secondary">Выйти</button></div>
    <div class="grid" id="cards"></div>

    <section class="panel">
      <h2>Документы и индекс</h2>
      <div class="row">
        <div><label for="documentFile">Документ</label><input id="documentFile" type="file"
          accept=".md,.markdown,.html,.htm,.txt"></div>
        <div><label><input id="overwrite" type="checkbox" style="width:auto"> Перезаписать</label></div>
        <button id="upload">Загрузить</button>
        <button id="reindex" class="secondary">Создать новый индекс</button>
      </div>
      <p><span id="indexStatus" class="status"></span></p>
      <div id="documentMessage"></div>
      <table><thead><tr><th>Документ</th><th>Источник</th><th>Загружен</th></tr></thead>
        <tbody id="documents"></tbody></table>
    </section>

    <section class="panel" id="serverLoadPanel">
      <h2>Нагрузка сервера</h2>
      <p class="muted">Живая телеметрия процесса RAG и системной очереди CPU. Обновляется каждые 2 секунды.</p>
      <div class="load-shell">
        <div class="gauge-zone">
          <div class="load-gauge" id="loadGauge">
            <div class="gauge-core">
              <div class="gauge-value" id="loadPercent">0%</div>
              <div class="gauge-label">System load / CPU</div>
              <div class="load-state"><i class="pulse-dot"></i><span id="loadState">Норма</span></div>
            </div>
          </div>
        </div>
        <div class="telemetry-grid">
          <div class="telemetry"><div class="telemetry-label">MCP вызовов / мин</div>
            <div class="telemetry-value" id="callsMinute">0</div></div>
          <div class="telemetry"><div class="telemetry-label">Пиковая память</div>
            <div class="telemetry-value" id="peakMemory">0 MB</div></div>
          <div class="telemetry"><div class="telemetry-label">Аптайм процесса</div>
            <div class="telemetry-value" id="serverUptime">0с</div></div>
          <div class="telemetry"><div class="telemetry-label">CPU ядер</div>
            <div class="telemetry-value" id="cpuCores">0</div></div>
          <div class="telemetry"><div class="telemetry-label">Всего MCP вызовов</div>
            <div class="telemetry-value" id="totalCalls">0</div></div>
          <div class="telemetry"><div class="telemetry-label">Последний запрос</div>
            <div class="telemetry-value" id="lastRequest">—</div></div>
        </div>
        <div class="load-bars">
          <div class="load-bar-row"><span>LOAD 1M</span><div class="load-track"><div class="load-fill" id="load1Bar"></div></div><b id="load1">0.00</b></div>
          <div class="load-bar-row"><span>LOAD 5M</span><div class="load-track"><div class="load-fill" id="load5Bar"></div></div><b id="load5">0.00</b></div>
          <div class="load-bar-row"><span>LOAD 15M</span><div class="load-track"><div class="load-fill" id="load15Bar"></div></div><b id="load15">0.00</b></div>
          <div class="load-footer"><span>Источник: OS load average + process telemetry</span>
            <span id="telemetryUpdated">Синхронизация...</span></div>
        </div>
      </div>
    </section>
  </section>
</main>
<script>
  const $ = (id) => document.getElementById(id);
  let adminPassword = sessionStorage.getItem('kbAdminPassword') || '';
  let refreshTimer = null;

  async function api(path, options = {}) {
    const headers = Object.assign({'X-KB-Admin-Password': adminPassword}, options.headers || {});
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, Object.assign({}, options, {headers}));
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {error: `HTTP ${response.status}`}; }
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function cell(row, value) { const td=document.createElement('td'); td.textContent=value ?? ''; row.appendChild(td); }
  function card(label, value) { const node=document.createElement('div'); node.className='card';
    const a=document.createElement('div'); a.className='muted'; a.textContent=label;
    const b=document.createElement('div'); b.className='value'; b.textContent=value; node.append(a,b); return node; }

  function formatUptime(totalSeconds) { const seconds=Math.max(0,Number(totalSeconds)||0);
    const days=Math.floor(seconds/86400); const hours=Math.floor(seconds%86400/3600);
    const minutes=Math.floor(seconds%3600/60);
    return days ? `${days}д ${hours}ч` : hours ? `${hours}ч ${minutes}м` : `${minutes}м`; }

  function updateServerLoad(data) { const metrics=data.server_metrics; const usage=data.usage;
    const load=Math.max(0,Math.min(100,Number(metrics.load_percent)||0));
    $('loadGauge').style.setProperty('--load-angle',`${load*3.6}deg`);
    $('loadPercent').textContent=`${load.toFixed(1)}%`;
    $('loadState').textContent=load>=80?'Критическая нагрузка':load>=50?'Повышенная нагрузка':'Нагрузка в норме';
    $('callsMinute').textContent=usage.calls_last_minute; $('peakMemory').textContent=`${metrics.peak_rss_mb} MB`;
    $('serverUptime').textContent=formatUptime(metrics.uptime_seconds); $('cpuCores').textContent=metrics.cpu_cores;
    $('totalCalls').textContent=usage.total_calls; $('lastRequest').textContent=usage.last_used_at
      ? new Date(usage.last_used_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : 'Нет данных';
    for(const [period,value] of [['1',metrics.load_1m],['5',metrics.load_5m],['15',metrics.load_15m]]) {
      $(`load${period}`).textContent=Number(value).toFixed(2);
      $(`load${period}Bar`).style.width=`${Math.min(100,Number(value)/metrics.cpu_cores*100)}%`; }
    $('telemetryUpdated').textContent=`Обновлено ${new Date().toLocaleTimeString('ru-RU')}`;
  }

  async function showEntryAnimation() {
    const splash=$('cosmicSplash'); splash.classList.remove('hidden','fade-out');
    await new Promise((resolve)=>setTimeout(resolve,2350));
    splash.classList.add('fade-out');
    await new Promise((resolve)=>setTimeout(resolve,450));
    splash.classList.add('hidden'); splash.classList.remove('fade-out');
  }

  async function refresh(animateEntry = false) {
    try {
      const data = await api('/admin/api/overview');
      $('login').classList.add('hidden');
      if(animateEntry) { $('dashboard').classList.add('hidden'); await showEntryAnimation(); }
      $('dashboard').classList.remove('hidden');
      const serverLoad=Number(data.server_metrics.load_percent).toFixed(1);
      const cards = $('cards'); cards.replaceChildren(
        card('Документов', data.index.document_count), card('Чанков', data.index.chunk_count),
        card('MCP вызовов', data.usage.total_calls), card('Поисков', data.usage.search_count),
        card('Средний контекст', data.usage.average_context_tokens),
        card('Вызовов / мин', data.usage.calls_last_minute),
        card('Нагрузка', `${serverLoad}%`),
        card('Индекс', data.index_job.status));
      updateServerLoad(data);
      const job=data.index_job; $('indexStatus').textContent = job.status === 'running'
        ? `Индекс строится с ${job.started_at}`
        : job.status === 'completed' ? `Готов: ${job.documents} документов, ${job.chunks} чанков за ${job.elapsed_seconds} с`
        : job.status === 'failed' ? `Ошибка: ${job.error}` : 'Ожидает запуска';
      $('reindex').disabled = job.status === 'running';
      const docs=$('documents'); docs.replaceChildren();
      for (const doc of data.documents) { const row=document.createElement('tr');
        cell(row,doc.title); cell(row,doc.source_path); cell(row,doc.loaded_at); docs.appendChild(row); }
    } catch (error) {
      clearInterval(refreshTimer); refreshTimer=null; $('dashboard').classList.add('hidden');
      $('login').classList.remove('hidden'); $('loginError').textContent=error.message;
    }
  }

  $('loginForm').onsubmit=async(event)=>{ event.preventDefault(); adminPassword=$('password').value;
    sessionStorage.setItem('kbAdminPassword',adminPassword); await refresh(true);
    if(!refreshTimer) refreshTimer=setInterval(refresh,2000); };
  $('logout').onclick=()=>{ sessionStorage.removeItem('kbAdminPassword'); location.reload(); };
  $('upload').onclick=async()=>{ const file=$('documentFile').files[0];
    if(!file) return $('documentMessage').textContent='Выберите файл';
    try { const result=await api('/admin/api/documents',{method:'POST',body:JSON.stringify({
      path:file.name,content:await file.text(),overwrite:$('overwrite').checked})});
      $('documentMessage').className='ok'; $('documentMessage').textContent=`Загружено ${result.source_path}. Теперь перестройте индекс.`;
    } catch(error) { $('documentMessage').className='error'; $('documentMessage').textContent=error.message; } };
  $('reindex').onclick=async()=>{ try { await api('/admin/api/index',{method:'POST'}); await refresh(); }
    catch(error) { $('documentMessage').className='error'; $('documentMessage').textContent=error.message; } };
  if(adminPassword) { refresh(); refreshTimer=setInterval(refresh,2000); }
</script>
</body></html>"""
