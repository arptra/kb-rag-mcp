# ruff: noqa: E501, RUF001
"""Dependency-free browser UI served by the graph HTTP process."""

GRAPH_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GigaCode Repository Graph</title>
  <style>
    :root { color-scheme: dark; --bg:#080b11; --panel:#10151f; --line:#273144;
      --text:#e9eef8; --muted:#8c99ad; --green:#62e6a7; --blue:#62a8ff;
      --orange:#ffad66; --pink:#f587bd; --red:#ff6f78; }
    * { box-sizing:border-box } body { margin:0; background:radial-gradient(circle at 25% 0,#172235 0,#080b11 42%);
      color:var(--text); font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif; overflow:hidden }
    button,input,select { font:inherit } header { height:64px; display:flex; align-items:center; gap:12px;
      border-bottom:1px solid var(--line); padding:0 18px; background:rgba(8,11,17,.88); backdrop-filter:blur(12px) }
    .brand { font-size:17px; font-weight:760; letter-spacing:.2px; margin-right:8px; white-space:nowrap }
    .brand i { color:var(--green); font-style:normal } .pill { color:var(--muted); border:1px solid var(--line);
      padding:5px 9px; border-radius:999px; white-space:nowrap } .controls { margin-left:auto; display:flex; gap:8px }
    input,select,button { border:1px solid var(--line); color:var(--text); background:#111824; border-radius:8px;
      min-height:36px; padding:7px 10px } input { width:min(28vw,340px) } button { cursor:pointer }
    button:hover,button.active { border-color:#4f6688; background:#18243a } button.primary { color:#07120d;
      background:var(--green); border-color:var(--green); font-weight:700 } main { height:calc(100vh - 64px);
      display:grid; grid-template-columns:270px minmax(360px,1fr) 370px }
    aside,.details { background:rgba(12,17,26,.94); min-width:0; overflow:auto } aside { border-right:1px solid var(--line);
      padding:16px } .details { border-left:1px solid var(--line); padding:18px }
    h2 { font-size:12px; text-transform:uppercase; letter-spacing:1.2px; color:var(--muted); margin:4px 0 12px }
    .metric-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:20px }
    .metric { background:#111824; border:1px solid var(--line); border-radius:10px; padding:10px }
    .metric b { display:block; font-size:20px } .metric span { color:var(--muted); font-size:11px }
    .legend { display:grid; gap:7px; margin-bottom:20px } .legend div { display:flex; align-items:center; gap:8px; color:#b7c1d2 }
    .dot { width:9px; height:9px; border-radius:50%; background:var(--c) } .hint { color:var(--muted); font-size:12px }
    .workspace { position:relative; overflow:hidden } svg { width:100%; height:100%; display:block; cursor:grab }
    svg:active { cursor:grabbing } .edge { stroke:#40506a; stroke-opacity:.65; fill:none }
    .edge.low { stroke-dasharray:5 5; stroke:var(--orange) } .edge-label { font-size:10px; fill:#7f8ca0 }
    .node circle { stroke:#071018; stroke-width:3; filter:drop-shadow(0 4px 7px #0008) }
    .node text { fill:#e8eef8; font-size:11px; text-anchor:middle; pointer-events:none }
    .node .type { fill:#8391a6; font-size:8px; letter-spacing:.7px } .node { cursor:pointer }
    .node:hover circle,.node.selected circle { stroke:#fff; stroke-width:2 }
    .empty { position:absolute; inset:0; display:grid; place-items:center; color:var(--muted); pointer-events:none }
    .title { font-size:20px; font-weight:750; word-break:break-word } .subtitle { color:var(--muted); margin:3px 0 18px;
      word-break:break-all } .badge { display:inline-block; padding:3px 7px; margin:0 5px 5px 0; border-radius:6px;
      background:#172237; border:1px solid #2c3b55; color:#b9c7dc; font-size:11px }
    .section { margin-top:20px } .section h3 { font-size:12px; text-transform:uppercase; color:var(--muted);
      letter-spacing:1px } pre { white-space:pre-wrap; word-break:break-word; background:#090d14; border:1px solid var(--line);
      border-radius:9px; padding:11px; color:#cbd5e5; max-height:280px; overflow:auto; font-size:11px }
    .item { border-left:2px solid #34435c; padding:5px 0 5px 10px; margin:7px 0; word-break:break-word }
    .item b { display:block; font-size:12px } .item small { color:var(--muted) } .evidence { border-left-color:var(--green) }
    .error { color:#ff9198 } .token { width:100%; margin:8px 0 } @media(max-width:1050px){main{grid-template-columns:220px 1fr 310px}}
    @media(max-width:760px){header{height:auto;min-height:64px;flex-wrap:wrap;padding:10px}.controls{margin-left:0;width:100%}
      input{width:100%}main{height:calc(100vh - 112px);grid-template-columns:1fr}.details,aside{display:none}}
  </style>
</head>
<body>
<header>
  <div class="brand"><i>GigaCode</i> Repository Graph</div><div id="snapshot" class="pill">loading…</div>
  <div class="controls">
    <input id="search" placeholder="сервис, операция, таблица…">
    <select id="view"><option value="services">Сервисы</option><option value="full">Полный граф</option></select>
    <button id="reload">Обновить</button>
  </div>
</header>
<main>
  <aside>
    <h2>Индекс</h2><div id="metrics" class="metric-grid"></div>
    <h2>Типы узлов</h2><div id="legend" class="legend"></div>
    <h2>Доступ</h2><div class="hint">Bearer нужен только если сервер настроен с токеном.</div>
    <input id="token" class="token" type="password" placeholder="Bearer token">
    <button id="saveToken">Сохранить локально</button>
    <div class="section"><h2>Как читать</h2><p class="hint">Сплошная связь — уверенное извлечение. Пунктир — LOW или UNRESOLVED. Кликните узел, чтобы увидеть факты и исходный код.</p></div>
  </aside>
  <section class="workspace"><svg id="graph" viewBox="0 0 1200 800"></svg><div id="empty" class="empty"></div></section>
  <section id="details" class="details"><div class="title">Выберите узел</div><p class="hint">Здесь появятся метаданные и evidence: репозиторий, commit, файл и строка.</p></section>
</main>
<script>
const colors={Service:'#62e6a7',ExternalSystem:'#ffad66',BusinessOperation:'#62a8ff',BusinessRule:'#f587bd',
EntryPoint:'#6de1e8',ExitPoint:'#ff9f72',CodeSymbol:'#7183a8',DomainEntity:'#c49aff',Table:'#ffcf66',Column:'#9da9bc',Event:'#ff7e76',Repository:'#8fa0b9'};
const state={payload:null,selected:null};
const $=s=>document.querySelector(s);
function headers(){const token=localStorage.getItem('gigacodeGraphToken');return token?{Authorization:'Bearer '+token}:{}}
async function api(path){const r=await fetch(path,{headers:headers()});if(!r.ok){let m=await r.text();try{m=JSON.parse(m).error}catch{}throw Error(m||r.statusText)}return r.json()}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function short(v,n=28){v=String(v);return v.length>n?v.slice(0,n-1)+'…':v}
function metrics(p){const s=p.stats||{};$('#metrics').innerHTML=[['nodes',s.node_count],['edges',s.edge_count],['evidence',s.evidence_count],['issues',s.issue_count]].map(x=>`<div class="metric"><b>${x[1]??0}</b><span>${x[0]}</span></div>`).join('');
  const types=[...new Set(p.nodes.map(n=>n.type))];$('#legend').innerHTML=types.map(t=>`<div><span class="dot" style="--c:${colors[t]||'#aaa'}"></span>${esc(t)}</div>`).join('')}
function layout(nodes,w,h){const groups={};nodes.forEach(n=>(groups[n.type]??=[]).push(n));const types=Object.keys(groups);const pos={};
 if($('#view').value==='services'){const radius=Math.min(w,h)*.34;nodes.forEach((n,i)=>{const a=(i/nodes.length)*Math.PI*2-Math.PI/2;pos[n.id]={x:w/2+Math.cos(a)*radius,y:h/2+Math.sin(a)*radius}})}
 else {types.forEach((t,ti)=>groups[t].forEach((n,i)=>{const cols=Math.max(1,types.length);const x=90+(w-180)*(ti/(cols-1||1));const step=(h-100)/(groups[t].length+1);pos[n.id]={x,y:50+step*(i+1)}}))}return pos}
function draw(p){state.payload=p;const svg=$('#graph'),w=1200,h=800,pos=layout(p.nodes,w,h);svg.innerHTML='';$('#empty').textContent=p.nodes.length?'': 'Граф пуст. Сначала выполните gigacode-graph index …';
 const defs=document.createElementNS('http://www.w3.org/2000/svg','defs');defs.innerHTML='<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L10 5L0 10z" fill="#53647f"/></marker>';svg.appendChild(defs);
 p.edges.forEach(e=>{if(!pos[e.source]||!pos[e.target])return;const a=pos[e.source],b=pos[e.target],g=document.createElementNS(svg.namespaceURI,'g');const line=document.createElementNS(svg.namespaceURI,'line');line.setAttribute('x1',a.x);line.setAttribute('y1',a.y);line.setAttribute('x2',b.x);line.setAttribute('y2',b.y);line.setAttribute('class','edge '+(['LOW','UNRESOLVED'].includes(e.confidence)?'low':''));line.setAttribute('marker-end','url(#arrow)');g.appendChild(line);
   if(p.nodes.length<80){const text=document.createElementNS(svg.namespaceURI,'text');text.setAttribute('x',(a.x+b.x)/2);text.setAttribute('y',(a.y+b.y)/2-4);text.setAttribute('class','edge-label');text.textContent=short(e.label||e.type,24);g.appendChild(text)}svg.appendChild(g)});
 p.nodes.forEach(n=>{const q=pos[n.id],g=document.createElementNS(svg.namespaceURI,'g');g.setAttribute('class','node'+(state.selected===n.id?' selected':''));g.setAttribute('transform',`translate(${q.x} ${q.y})`);g.dataset.id=n.id;const c=document.createElementNS(svg.namespaceURI,'circle');c.setAttribute('r',n.type==='Service'?27:20);c.setAttribute('fill',colors[n.type]||'#8190a8');g.appendChild(c);const label=document.createElementNS(svg.namespaceURI,'text');label.setAttribute('y',n.type==='Service'?43:36);label.textContent=short(n.label,26);g.appendChild(label);const type=document.createElementNS(svg.namespaceURI,'text');type.setAttribute('class','type');type.setAttribute('y',n.type==='Service'?56:49);type.textContent=n.type.toUpperCase();g.appendChild(type);g.addEventListener('click',()=>select(n));svg.appendChild(g)})}
function evidenceHtml(items){return (items||[]).map(e=>`<div class="item evidence"><b>${esc(e.repository)} · ${esc(e.file)}:${e.line}</b><small>${esc(e.extractor)} · ${esc(e.confidence)}${e.commit?' · '+esc(e.commit.slice(0,9)):''}</small><div>${esc(e.snippet)}</div></div>`).join('')||'<p class="hint">Evidence не записан для этого агрегированного узла.</p>'}
async function select(n){state.selected=n.id;draw(state.payload);const d=$('#details');d.innerHTML=`<div class="title">${esc(n.label)}</div><div class="subtitle">${esc(n.id)}</div><span class="badge">${esc(n.type)}</span>${n.service_id?`<span class="badge">${esc(n.service_id)}</span>`:''}<div class="section"><h3>Metadata</h3><pre>${esc(JSON.stringify(n.metadata,null,2))}</pre></div><div class="section"><h3>Evidence</h3><div id="nodeEvidence">loading…</div></div>`;
 try{if(n.type==='Service'){const x=await api('/api/service?service='+encodeURIComponent(n.service_id||n.id));d.innerHTML+=`<div class="section"><h3>Operations · ${x.business.operation_count}</h3>${x.business.operations.slice(0,30).map(o=>`<div class="item"><b>${esc(o.label)}</b><small>${esc(o.metadata.trigger_type)} · ${esc(o.metadata.trigger)}</small></div>`).join('')||'<p class="hint">Не извлечены</p>'}<h3>Dependencies</h3>${x.dependencies.edges.map(e=>`<div class="item"><b>${esc(e.label||e.type)}</b><small>${esc(e.source)} → ${esc(e.target)} · ${esc(e.confidence)}</small></div>`).join('')||'<p class="hint">Нет связей</p>'}</div>`}
   const e=await api('/api/evidence?ids='+encodeURIComponent((n.evidence_ids||[]).join(',')));$('#nodeEvidence').innerHTML=evidenceHtml(e.items)
 }catch(err){$('#nodeEvidence').innerHTML=`<p class="error">${esc(err.message)}</p>`}}
async function load(){try{$('#empty').textContent='Загружаю граф…';const view=$('#view').value,p=await api('/api/graph?view='+view);const overview=await api('/api/overview');p.stats=overview;$('#snapshot').textContent=new Date(p.generated_at).toLocaleString();metrics(p);draw(p)}catch(e){$('#empty').innerHTML=`<span class="error">${esc(e.message)}</span>`}}
let searchTimer;$('#search').addEventListener('input',e=>{clearTimeout(searchTimer);searchTimer=setTimeout(async()=>{const q=e.target.value.trim();if(!q){load();return}try{const x=await api('/api/search?query='+encodeURIComponent(q)+'&limit=100');const ids=new Set(x.results.map(r=>r.id));const p=await api('/api/graph?view=full');p.nodes=p.nodes.filter(n=>ids.has(n.id));const keep=new Set(p.nodes.map(n=>n.id));p.edges=p.edges.filter(e=>keep.has(e.source)&&keep.has(e.target));p.stats=await api('/api/overview');metrics(p);draw(p)}catch(err){$('#empty').textContent=err.message}},250)});
$('#view').addEventListener('change',load);$('#reload').addEventListener('click',load);$('#token').value=localStorage.getItem('gigacodeGraphToken')||'';$('#saveToken').addEventListener('click',()=>{localStorage.setItem('gigacodeGraphToken',$('#token').value);load()});load();
</script>
</body></html>"""
