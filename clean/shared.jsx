// Live data layer for orchmux clean UI

const _TQ = (() => {
  const t = new URLSearchParams(location.search).get('token');
  return t ? '?token=' + encodeURIComponent(t) : '';
})();
const _TQA = _TQ ? _TQ + '&' : '?';

async function _get(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) return null;
    return r.json();
  } catch(e) { return null; }
}

// ── Rendering (ported from classic mdParse / cleanTermOutput) ───────────────

function stripAnsi(s) {
  return s.replace(/\x1b\[[0-9;?]*[a-zA-Z~]/g,'')
          .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g,'')
          .replace(/\x1b[^\[\]]/g,'')
          .replace(/\r/g,'');
}

function cleanTermOutput(s) {
  const lines = s.split('\n');
  const out = []; let blanks = 0;
  for (const line of lines) {
    const t = line.trim();
    if (t.length > 0 && /^[─-╿\s]+$/.test(t)) continue;
    if (/^[-─━=╌]{4,}$/.test(t)) continue;
    if (/^[│╭╮╰╯]/.test(t)) continue;          // codex box-drawing header
    if (/^\[\?[0-9]+[hl]/.test(t)) continue;
    if (/^\x1b/.test(t)) continue;
    if (/^[❯›>$#]\s*$/.test(t)) continue;       // claude ❯ and codex/kimi › prompts
    if (/^gpt-[0-9]/.test(t)) continue;         // codex status bar: "gpt-5.5 high · ~/path"
    if (/OpenAI Codex|>_ OpenAI|Kimi CLI|>_ Kimi/.test(t)) continue;
    if (/^Tip:|Use \/fast|Use \/skills|\/skills to list/.test(t)) continue;
    if (/model to change|\/model to change/.test(t)) continue;
    if (t === '') { blanks++; if (blanks <= 1) out.push(''); continue; }
    blanks = 0; out.push(line);
  }
  while (out.length && out[out.length-1].trim() === '') out.pop();
  return out.join('\n');
}

function _mdInl(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g,'<em>$1</em>')
    .replace(/`([^`\n]+)`/g,'<code>$1</code>')
    .replace(/~~([^~\n]+)~~/g,'<del>$1</del>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank">$1</a>')
    .replace(/(^|[\s(])((https?:\/\/|obsidian:\/\/)[^\s<>")']+)/g,
      (_,pre,url)=>`${pre}<a href="${url}" target="_blank" rel="noreferrer" style="color:#b08a2a;word-break:break-all">${url}</a>`);
}

function mdParse(raw) {
  const lines = raw.replace(/\r/g,'').split('\n');
  const isTR  = l => { const t=l.trim(); return t.length>2&&t[0]==='|'&&t[t.length-1]==='|'; };
  const isSep = l => { const t=l.trim(); return t.length>1&&t.includes('|')&&!t.replace(/[|:\- ]/g,'').length; };
  const isLI  = l => /^([-*+]|\d+\.) /.test(l.trim());
  const splitCells = r => r.trim().slice(1,-1).split('|').map(c=>c.trim());
  const BOX_CELL = '│';
  const isBoxRow = l => { const t=l.trim(); return t.includes(BOX_CELL)&&t.split(BOX_CELL).some(c=>c.trim().length>0); };
  const boxCells = r => r.split(BOX_CELL).slice(1,-1).map(c=>c.trim());
  const html=[]; let i=0;
  while (i < lines.length) {
    const line=lines[i], t=line.trim();
    if (!t) { i++; continue; }
    // Code fence
    if (t.startsWith('```')) {
      const lang=t.slice(3).trim(); i++;
      const cl=[];
      while (i<lines.length && !lines[i].trim().startsWith('```')) { cl.push(lines[i]); i++; }
      i++;
      const code=cl.join('\n').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      html.push('<pre><code'+(lang?' class="lang-'+lang+'"':'')+'>'+code+'</code></pre>');
      continue;
    }
    // Heading
    const hm = t.match(/^(#{1,6}) (.+)$/);
    if (hm) { html.push('<h'+hm[1].length+'>'+_mdInl(hm[2])+'</h'+hm[1].length+'>'); i++; continue; }
    // Box-drawing table (Claude Code native │ separator)
    if (isBoxRow(line)) {
      const rows=[];
      while (i<lines.length && (isBoxRow(lines[i])||!lines[i].trim())) {
        if (lines[i].trim()) rows.push(lines[i]); i++;
      }
      let th='<table>';
      if (rows.length>=1) {
        th+='<thead><tr>'+boxCells(rows[0]).map(c=>'<th>'+_mdInl(c)+'</th>').join('')+'</tr></thead>';
        if (rows.length>1) th+='<tbody>'+rows.slice(1).map(r=>'<tr>'+boxCells(r).map(c=>'<td>'+_mdInl(c)+'</td>').join('')+'</tr>').join('')+'</tbody>';
      }
      html.push(th+'</table>'); continue;
    }
    // Markdown pipe table
    if (isTR(line)) {
      const rows=[];
      while (i<lines.length && (isTR(lines[i])||!lines[i].trim())) {
        if (lines[i].trim()) rows.push(lines[i].trim()); i++;
      }
      const dataRows=rows.filter(r=>!isSep(r));
      let th='<table>';
      if (dataRows.length>=1) {
        th+='<thead><tr>'+splitCells(dataRows[0]).map(c=>'<th>'+_mdInl(c)+'</th>').join('')+'</tr></thead>';
        if (dataRows.length>1) th+='<tbody>'+dataRows.slice(1).map(r=>'<tr>'+splitCells(r).map(c=>'<td>'+_mdInl(c)+'</td>').join('')+'</tr>').join('')+'</tbody>';
      }
      html.push(th+'</table>'); continue;
    }
    // List
    if (isLI(line)) {
      const items=[];
      while (i<lines.length && isLI(lines[i])) {
        let item=lines[i].trim().replace(/^([-*+]|\d+\.) /,'');
        item=item.replace(/^\[ \] /,'<input type="checkbox" disabled> ');
        item=item.replace(/^\[x\] /i,'<input type="checkbox" checked disabled> ');
        items.push(_mdInl(item)); i++;
      }
      const tag=/^\d/.test(line.trim())?'ol':'ul';
      html.push('<'+tag+'>'+items.map(it=>'<li>'+it+'</li>').join('')+'</'+tag+'>');
      continue;
    }
    // Blockquote
    if (t.startsWith('>')) {
      const bq=[];
      while (i<lines.length && lines[i].trim().startsWith('>')) { bq.push(lines[i].trim().slice(1).trim()); i++; }
      html.push('<blockquote>'+_mdInl(bq.join(' '))+'</blockquote>');
      continue;
    }
    // Plain text block — pre-wrap preserves indentation/columns
    const pl=[];
    while (i<lines.length && lines[i].trim()
           && !lines[i].trim().startsWith('```')
           && !lines[i].trim().match(/^#{1,6} /)
           && !isTR(lines[i]) && !isBoxRow(lines[i])
           && !isLI(lines[i]) && !lines[i].trim().startsWith('>')) {
      pl.push(lines[i]); i++;
    }
    if (pl.length) html.push('<div class="plain-block">'+pl.map(l=>_mdInl(l)).join('\n')+'</div>');
  }
  return html.join('\n');
}

// ── Background snapshot cache — pre-fetches ALL workers so switching is instant
let _paneSnaps = {};  // session -> {html, raw}

function usePaneSnapshots(sessions) {
  const sessKey = sessions.join(',');
  const [snaps, setSnaps] = React.useState(_paneSnaps);

  React.useEffect(() => {
    if (!sessions || sessions.length === 0) return;
    let alive = true;

    const fetchOne = async (session) => {
      const d = await _get('/pane/' + encodeURIComponent(session) + _TQA + 'lines=300');
      if (!alive || !d || !d.output) return;
      const cleaned = cleanTermOutput(stripAnsi(d.output));
      const html = mdParse(cleaned);
      _paneSnaps = { ..._paneSnaps, [session]: { html, raw: cleaned } };
      if (alive) setSnaps(s => ({ ...s, [session]: { html, raw: cleaned } }));
    };

    // Stagger initial load so we don't hammer the server
    sessions.forEach((s, i) => setTimeout(() => alive && fetchOne(s), i * 250));

    // Cycle: refresh each session every 2500ms (one per tick, rotating)
    let idx = 0;
    const interval = setInterval(() => {
      if (!alive || sessions.length === 0) return;
      fetchOne(sessions[idx % sessions.length]);
      idx++;
    }, 2500);

    return () => { alive = false; clearInterval(interval); };
  }, [sessKey]);

  return snaps;
}

// ── Pane rendering hook — returns rendered HTML string ──────────────────────
function usePaneHtml(session) {
  const [html, setHtml] = React.useState('');
  React.useEffect(() => {
    if (!session) { setHtml(''); return; }
    let alive = true;
    const tick = async () => {
      const d = await _get('/pane/' + encodeURIComponent(session) + _TQA + 'lines=300');
      if (!alive || !d || !d.output) return;
      const cleaned = cleanTermOutput(stripAnsi(d.output));
      setHtml(mdParse(cleaned));
    };
    tick();
    const t = setInterval(tick, 2500);
    return () => { alive = false; clearInterval(t); };
  }, [session]);
  return html;
}

// ── Workers / domains ───────────────────────────────────────────────────────

async function _fetchWorkers() {
  // Fetch status first — don't block on worker-details (22 tmux captures, slow)
  const status = await _get('/status');
  if (!status) return null;  // null = fetch failed, caller keeps previous state
  const workers = [];
  for (const [domain, dcfg] of Object.entries(status)) {
    for (const w of (dcfg.workers || [])) {
      workers.push({
        session:     w.session,
        domain:      domain,
        model:       dcfg.model || 'claude',
        status:      w.exists ? (w.status || 'idle') : 'missing',
        task:        w.current_task || '',
        age:         w.elapsed_seconds || 0,
        displayName: '',
        auth:        'ok',
      });
    }
  }
  // Merge details in background — enriches display_name, auth, last_task
  // but never delays or clears the worker list
  _get('/worker-details' + _TQ).then(details => {
    if (!details) return;
    // Details arrive async — trigger a re-merge via a separate signal
    _lastDetails = details;
  });
  if (_lastDetails) {
    for (const w of workers) {
      const d = _lastDetails[w.session];
      if (d) {
        if (d.domain)        w.domain      = d.domain;
        if (d.display_name)  w.displayName = d.display_name;
        if (d.auth)          w.auth        = d.auth;
        if (d.slack_target !== undefined) w.slackTarget = d.slack_target;
        if (!w.task && d.last_task) w.task = d.last_task;
        if (d.last_task_status) w.lastStatus = d.last_task_status;
        if (d.last_task_time)   w.lastTime   = d.last_task_time;
      }
    }
  }
  return workers;
}

let _lastDetails = null;

function useWorkers() {
  const [workers, setWorkers] = React.useState([]);
  React.useEffect(() => {
    let alive = true;
    const tick = async () => {
      const w = await _fetchWorkers();
      if (!alive || w === null) return;
      // Merge in-place: update existing entries, preserve object identity where unchanged
      // so React only re-renders rows that actually changed (matches classic's in-place DOM update)
      setWorkers(prev => {
        if (prev.length === 0) return w;
        const prevMap = new Map(prev.map(p => [p.session, p]));
        return w.map(nw => {
          const p = prevMap.get(nw.session);
          if (!p) return nw;
          // Return same reference if nothing changed — React skips re-render for that row
          if (p.status === nw.status && p.task === nw.task && p.age === nw.age &&
              p.auth === nw.auth && p.displayName === nw.displayName) return p;
          return nw;
        });
      });
    };
    tick();
    const t = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  return workers;
}

function useDomains() {
  const [domains, setDomains] = React.useState([]);
  React.useEffect(() => { _get('/domains').then(d => d && setDomains(d)); }, []);
  return domains;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

// LIVE_SKILLS — populated lazily; components should prefer useSkills() for reactivity
let LIVE_SKILLS = [];

function useSkills() {
  const [skills, setSkills] = React.useState(LIVE_SKILLS);
  React.useEffect(() => {
    if (LIVE_SKILLS.length > 0) { setSkills(LIVE_SKILLS); return; }
    _get('/skills').then(s => {
      if (Array.isArray(s) && s.length > 0) { LIVE_SKILLS = s; setSkills(s); }
    }).catch(() => {});
  }, []);
  return skills;
}

function useTickerClock() {
  const [n, set] = React.useState(0);
  React.useEffect(() => {
    const t = setInterval(() => set(x => x+1), 1000);
    return () => clearInterval(t);
  }, []);
  return n;
}

function fmtAgeShort(s) {
  if (!s) return '—';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm';
  return Math.floor(s/3600) + 'h';
}

function fmtMMSS(s) {
  const m = Math.floor(s/60), sec = s%60;
  return `${m}:${String(sec).padStart(2,'0')}`;
}

function fmtDispatchTime(age) {
  if (!age) return null;
  const d = new Date(Date.now() - age * 1000);
  return d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
}

const STATUS_LABEL = {busy:'running',idle:'idle',waiting:'asking',blocked:'needs input',missing:'gone'};
const STATUS_DOT   = {busy:'#f5a623',idle:'#c8c4ba',waiting:'#4a90e2',blocked:'#e55a6a',missing:'#c8c4ba'};

function renderParsedHtml(el, html) {
  if (!el) return;
  const frag = new DOMParser().parseFromString(html || '', 'text/html');
  el.replaceChildren(...frag.body.childNodes);
}

Object.assign(window, {
  useWorkers, useDomains, usePaneHtml, usePaneSnapshots, useSkills, LIVE_SKILLS,
  stripAnsi, cleanTermOutput, mdParse, _mdInl, renderParsedHtml,
  useTickerClock, fmtAgeShort, fmtMMSS, fmtDispatchTime, STATUS_LABEL, STATUS_DOT,
  _TQ, _TQA, _get,
});
