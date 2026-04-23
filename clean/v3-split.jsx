// V3 — Split Pane (live data, full feature parity)
// Left: task list + prominent dispatch panel
// Right: tabbed — Live terminal | Results | Manage

// Inject pane styles once (same as classic modern mode)
if (!document.getElementById('clean-pane-styles')) {
  const s = document.createElement('style');
  s.id = 'clean-pane-styles';
  s.textContent = `
    .clean-pane { font-family: -apple-system, "Inter", sans-serif; font-size: 13px; color: #2a251f; line-height: 1.6; }
    .clean-pane h1,.clean-pane h2,.clean-pane h3 { font-weight:700; margin:14px 0 6px; color:#1a1614; letter-spacing:-0.3px; }
    .clean-pane h1 { font-size:20px; border-bottom:2px solid #f0e8d0; padding-bottom:6px; }
    .clean-pane h2 { font-size:16px; border-bottom:1px solid #f0e8d0; padding-bottom:4px; }
    .clean-pane h3 { font-size:14px; }
    .clean-pane table { width:100%; border-collapse:collapse; margin:10px 0; border-radius:8px; overflow:hidden; border:1px solid #ead9a3; }
    .clean-pane th { background:#fdf5dc; color:#7c6430; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; padding:8px 12px; text-align:left; border-bottom:1px solid #ead9a3; }
    .clean-pane td { padding:7px 12px; font-size:12.5px; border-bottom:1px solid #f5f0e8; vertical-align:top; }
    .clean-pane tr:last-child td { border-bottom:none; }
    .clean-pane tr:nth-child(even) td { background:#fffdf5; }
    .clean-pane pre { background:#f8f4ec; border:1px solid #ead9a3; border-radius:7px; padding:12px 14px; overflow-x:auto; margin:8px 0; }
    .clean-pane code { font-family:"SF Mono","Fira Code",monospace; font-size:11.5px; background:#f5f0e5; padding:1px 5px; border-radius:3px; color:#7c4f20; }
    .clean-pane pre code { background:none; padding:0; color:#5a4020; font-size:11.5px; }
    .clean-pane ul,.clean-pane ol { padding-left:20px; margin:6px 0; }
    .clean-pane li { margin:3px 0; font-size:13px; }
    .clean-pane blockquote { border-left:3px solid #e8c84a; margin:8px 0; padding:4px 12px; color:#7c6840; background:#fffdf0; border-radius:0 6px 6px 0; }
    .clean-pane strong { font-weight:700; color:#1a1614; }
    .clean-pane a { color:#b08a2a; }
    .clean-pane .plain-block { white-space:pre-wrap; margin:0 0 6px; line-height:1.6; color:#2a251f; word-break:break-word; font-family:inherit; }
  `;
  document.head.appendChild(s);
}

// ── Domain detection ────────────────────────────────────────────────────────
let _DKW = {};
let _DKW_DEFAULT = '';
(async () => {
  try {
    const wd = await fetch('/worker-details').then(r => r.json());
    const map = {};
    let first = '';
    for (const w of Object.values(wd || {})) {
      if (!w.domain || w.domain.startsWith('_')) continue;
      if (!first) first = w.domain;
      if (w.handles && w.handles.length) map[w.domain] = w.handles;
    }
    _DKW = map;
    _DKW_DEFAULT = first;
  } catch(e) {}
})();
function _detectDomain(text) {
  if (!text) return '';
  const lo = text.toLowerCase();
  const scores = {};
  for (const [dom, kws] of Object.entries(_DKW))
    scores[dom] = kws.reduce((n,k) => n+(lo.includes(k)?1:0), 0);
  const best = Object.entries(scores).sort((a,b)=>b[1]-a[1])[0];
  return (best && best[1] > 0) ? best[0] : _DKW_DEFAULT;
}

// ── Dispatch panel (left bottom) ────────────────────────────────────────────
// Group skills by category for the browser
const SKILL_GROUPS = (() => {
  const g = {};
  for (const s of LIVE_SKILLS) { (g[s.c] = g[s.c]||[]).push(s); }
  return g;
})();

function DispatchPanel({domains}) {
  const [open, setOpen]         = React.useState(false);
  const [taskText, setTaskText] = React.useState('');
  const [domain, setDomain]     = React.useState('');
  const [autoDom, setAutoDom]   = React.useState('');
  const [sending, setSending]   = React.useState(false);
  const [msg, setMsg]           = React.useState('');
  // slash autocomplete
  const [slashQ, setSlashQ]     = React.useState('');   // query after /
  const [slashIdx, setSlashIdx] = React.useState(0);
  const taRef  = React.useRef(null);
  const msgRef = React.useRef('');
  msgRef.current = msg;

  // skills filtered by slash query
  const slashMatches = slashQ !== null
    ? LIVE_SKILLS.filter(s => s.n.includes(slashQ) || s.c.includes(slashQ))
    : [];

  const insertSkill = (name) => {
    const ta = taRef.current;
    if (!ta) return;
    const pos   = ta.selectionStart;
    const val   = taskText;
    const slash = val.lastIndexOf('/', pos);
    const next  = val.slice(0, slash) + '/' + name + ' ' + val.slice(pos);
    setTaskText(next);
    setSlashQ(null);
    setTimeout(() => {
      ta.focus();
      const cur = slash + name.length + 2;
      ta.setSelectionRange(cur, cur);
    }, 0);
  };

  const onInput = val => {
    setTaskText(val);
    if (!domain) setAutoDom(_detectDomain(val));
    setMsg('');
    // detect trailing /query
    const ta   = taRef.current;
    const pos  = ta ? ta.selectionStart : val.length;
    const before = val.slice(0, pos);
    const m = before.match(/\/(\w*)$/);
    if (m) { setSlashQ(m[1].toLowerCase()); setSlashIdx(0); }
    else   { setSlashQ(null); }
  };

  const onKeyDown = e => {
    if (slashMatches.length > 0) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setSlashIdx(i => Math.min(i+1, slashMatches.length-1)); return; }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setSlashIdx(i => Math.max(i-1, 0)); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); insertSkill(slashMatches[slashIdx].n); return; }
      if (e.key === 'Escape')    { setSlashQ(null); return; }
    }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) dispatch();
  };

  const effectiveDomain = domain || autoDom;

  const reset = () => { setTaskText(''); setDomain(''); setAutoDom(''); setMsg(''); setSlashQ(null); };

  const dispatch = async () => {
    const task = taskText.trim();
    if (!task || sending) return;
    setSending(true);
    try {
      const body = {task};
      if (effectiveDomain) body.domain = effectiveDomain;
      const r    = await fetch('/dispatch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
      const data = await r.json();
      if (r.ok) {
        setMsg('✓ dispatched to ' + (data.session || effectiveDomain || 'worker'));
        reset();
        setTimeout(() => { if(msgRef.current.startsWith('✓')) { setMsg(''); setOpen(false); } }, 2000);
      } else { setMsg('⚠ ' + (data.detail || 'dispatch failed')); }
    } catch(e) { setMsg('⚠ network error'); }
    setSending(false);
  };

  if (!open) return (
    <div style={{padding:'12px 14px',borderTop:'1px solid #f0e8d0',background:'#fdf8e9',flexShrink:0}}>
      <button onClick={()=>{setOpen(true);setTimeout(()=>taRef.current&&taRef.current.focus(),50);}}
        style={{width:'100%',display:'flex',alignItems:'center',gap:10,padding:'11px 14px',background:'#fff',border:'1.5px solid #e8d98a',borderRadius:9,cursor:'pointer',fontFamily:'inherit',textAlign:'left'}}>
        <div style={{width:22,height:22,borderRadius:'50%',background:'#f5d76b',display:'flex',alignItems:'center',justifyContent:'center',fontSize:15,fontWeight:700,color:'#2a251f',flexShrink:0}}>+</div>
        <span style={{fontSize:13,color:'#8a8072',flex:1}}>Dispatch a new task…</span>
        <span style={{fontSize:10,color:'#c8b878',fontFamily:'"SF Mono",monospace'}}>⌘N</span>
      </button>
      {msg && <div style={{fontSize:11,color:msg.startsWith('✓')?'#2e7d32':'#b85c00',marginTop:6,paddingLeft:4}}>{msg}</div>}
    </div>
  );

  return (
    <div style={{borderTop:'2px solid #f0c75a',background:'#fffdf5',flexShrink:0,display:'flex',flexDirection:'column',maxHeight:'70%'}}>
      {/* header */}
      <div style={{padding:'12px 16px 0',display:'flex',justifyContent:'space-between',alignItems:'center',flexShrink:0}}>
        <div style={{fontSize:13,fontWeight:700,color:'#2a251f'}}>New task</div>
        <button onClick={()=>{setOpen(false);reset();}} style={{background:'none',border:'none',color:'#a59985',fontSize:14,cursor:'pointer',padding:'0 2px'}}>✕</button>
      </div>

      {/* domain chips */}
      <div style={{padding:'8px 16px 0',display:'flex',gap:4,flexWrap:'wrap',flexShrink:0}}>
        <button onClick={()=>setDomain('')} style={{...sChipSel(!domain&&!autoDom),fontSize:10}}>
          auto{autoDom?` → ${autoDom}`:''}
        </button>
        {(domains.length ? domains : Object.keys(_DKW)).map(d=>(
          <button key={d} onClick={()=>setDomain(d)} style={{...sChipSel(domain===d),fontSize:10}}>{d}</button>
        ))}
      </div>

      {/* textarea + slash dropdown */}
      <div style={{padding:'8px 16px 0',position:'relative',flexShrink:0}}>
        <textarea ref={taRef} value={taskText}
          onChange={e=>onInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Describe the task or type / for a skill…"
          rows={3}
          style={{width:'100%',boxSizing:'border-box',border:'1px solid #ead9a3',borderRadius:8,padding:'10px 12px',fontSize:13,lineHeight:1.55,color:'#2a251f',fontFamily:'inherit',resize:'none',outline:'none',background:'#fff'}}
        />
        {slashMatches.length > 0 && (
          <div style={{position:'absolute',bottom:'calc(100% - 8px)',left:16,right:16,background:'#fff',border:'1px solid #ead9a3',borderRadius:8,boxShadow:'0 4px 16px rgba(0,0,0,0.10)',zIndex:50,maxHeight:180,overflow:'auto'}}>
            {slashMatches.map((s,i)=>(
              <div key={s.n} onMouseDown={()=>insertSkill(s.n)}
                style={{display:'flex',gap:8,alignItems:'center',padding:'7px 12px',background:i===slashIdx?'#fff8e0':'transparent',cursor:'pointer'}}>
                <span style={{fontFamily:'"SF Mono",monospace',fontSize:12,color:'#b08a2a',fontWeight:600,minWidth:120}}>/{s.n}</span>
                <span style={{fontSize:11,color:'#a59985'}}>{s.c}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* skill browser — scrollable */}
      <div style={{overflow:'auto',padding:'8px 16px',flex:1}}>
        {Object.entries(SKILL_GROUPS).map(([cat, skills])=>(
          <div key={cat} style={{marginBottom:8}}>
            <div style={{fontSize:9.5,color:'#c8b878',fontWeight:700,textTransform:'uppercase',letterSpacing:0.8,marginBottom:4}}>{cat}</div>
            <div style={{display:'flex',flexWrap:'wrap',gap:4}}>
              {skills.map(s=>(
                <span key={s.n} onMouseDown={()=>{setTaskText(t=>t+'/'+s.n+' ');setTimeout(()=>taRef.current&&taRef.current.focus(),0);}}
                  style={{fontSize:10,padding:'2px 7px',borderRadius:4,background:'#fff8e0',color:'#b08a2a',border:'1px solid #f0e4b8',fontFamily:'"SF Mono",monospace',cursor:'pointer'}}>
                  /{s.n}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* footer: routing + send */}
      <div style={{padding:'8px 16px 12px',display:'flex',justifyContent:'space-between',alignItems:'center',borderTop:'1px solid #f5f0e0',flexShrink:0}}>
        {effectiveDomain
          ? <span style={{fontSize:11,color:'#b08a2a'}}>routing to: <b>{effectiveDomain}</b></span>
          : <span style={{fontSize:11,color:'#c8c0af'}}>routing: auto-detect</span>}
        <button onClick={dispatch} disabled={!taskText.trim()||sending}
          style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'8px 20px',borderRadius:7,fontSize:12.5,fontWeight:700,cursor:!taskText.trim()||sending?'default':'pointer',fontFamily:'inherit',opacity:!taskText.trim()||sending?0.5:1}}>
          {sending?'Sending…':'Send →'}
        </button>
      </div>
      {msg && <div style={{fontSize:11,color:msg.startsWith('✓')?'#2e7d32':'#b85c00',padding:'0 16px 10px'}}>{msg}</div>}
    </div>
  );
}

const sChipSel = active => ({
  background: active?'#2a251f':'#fff',
  color: active?'#f5d76b':'#8a8072',
  border:'1px solid',
  borderColor: active?'#2a251f':'#e8e0cc',
  borderRadius:5,padding:'2px 8px',cursor:'pointer',fontFamily:'inherit',
  fontWeight: active?700:400,
});

// ── Result body with mdParse rendering ─────────────────────────────────────
function ResultBody({result}) {
  const ref = React.useRef(null);
  React.useEffect(() => { renderParsedHtml(ref.current, mdParse(cleanTermOutput(stripAnsi(result||'')))); }, [result]);
  return <div ref={ref} className="clean-pane" style={{borderTop:'1px solid #f5f0e8',padding:'12px 14px',background:'#fdf8f0',maxHeight:300,overflow:'auto'}}/>;
}

// ── Results panel ───────────────────────────────────────────────────────────
function ResultsPanel() {
  const [items, setItems] = React.useState(null);
  const [expanded, setExpanded] = React.useState(null);

  React.useEffect(() => {
    const load = async () => {
      const d = await _get('/results'+_TQ);
      if (d) setItems(d);
    };
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  if (!items) return <div style={sEmptyState}>loading results…</div>;
  if (!items.length) return <div style={sEmptyState}>No completed tasks yet.</div>;

  return (
    <div style={{flex:1,overflow:'auto',padding:'16px 24px'}}>
      {items.map((r,i) => {
        const isExp = expanded === i;
        return (
          <div key={i} style={{marginBottom:10,borderRadius:9,border:'1px solid #f0e8d0',background:'#fff',overflow:'hidden'}}>
            <div onClick={()=>setExpanded(isExp?null:i)}
              style={{padding:'11px 14px',cursor:'pointer',display:'flex',gap:10,alignItems:'flex-start'}}>
              <span style={{marginTop:2,fontSize:10,fontWeight:700,padding:'2px 6px',borderRadius:4,background:r.success?'#f0faf0':'#fff0f0',color:r.success?'#2e7d32':'#c62828',border:'1px solid',borderColor:r.success?'#c8e6c9':'#ffcdd2',flexShrink:0}}>
                {r.success?'✓':'✗'}
              </span>
              <div style={{flex:1,minWidth:0}}>
                <div style={{fontSize:13,color:'#2a251f',fontWeight:500,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{r.task||'(no task)'}</div>
                <div style={{fontSize:11,color:'#a59985',marginTop:2,display:'flex',gap:8}}>
                  <span>{r.session}</span>
                  {r.domain&&<><span>·</span><span>{r.domain}</span></>}
                  {r.completed_at&&<><span>·</span><span>{r.completed_at.slice(11,16)}</span></>}
                </div>
              </div>
              <span style={{color:'#c8c0af',fontSize:11,flexShrink:0}}>{isExp?'▲':'▼'}</span>
            </div>
            {isExp && r.result && (
              <ResultBody result={r.result}/>
            )}
            {isExp && !r.result && (
              <div style={{borderTop:'1px solid #f5f0e8',padding:'10px 14px',color:'#c8c0af',fontSize:12}}>No result captured.</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Questions panel (shown as overlay badge) ────────────────────────────────
function QuestionsOverlay() {
  const [questions, setQuestions] = React.useState([]);
  React.useEffect(() => {
    const load = async () => {
      const d = await _get('/questions');
      if (d) setQuestions(d.pending || []);
    };
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  if (!questions.length) return null;

  const answer = async (q, ans) => {
    await fetch('/answer/'+q.id, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:ans})});
    setQuestions(prev => prev.filter(x=>x.id!==q.id));
  };

  const dismiss = async (q) => {
    await fetch('/questions/'+q.id+_TQ, {method:'DELETE'});
    setQuestions(prev => prev.filter(x=>x.id!==q.id));
  };

  return (
    <div style={{position:'absolute',top:16,right:16,zIndex:100,display:'flex',flexDirection:'column',gap:8,maxWidth:360}}>
      {questions.map(q=>(
        <div key={q.id} style={{background:'#fff',border:'2px solid #f5a623',borderRadius:10,padding:'12px 14px',boxShadow:'0 4px 16px rgba(0,0,0,0.12)'}}>
          <div style={{display:'flex',justifyContent:'space-between',alignItems:'flex-start',marginBottom:4}}>
            <div style={{fontSize:10.5,color:'#f5a623',fontWeight:700,textTransform:'uppercase',letterSpacing:0.5}}>
              ⚡ {q.session||'worker'} is asking · {q.asked_at||''}
            </div>
            <button onClick={()=>dismiss(q)} title="Dismiss"
              style={{background:'none',border:'none',color:'#c8c0af',fontSize:14,cursor:'pointer',padding:'0 0 0 8px',lineHeight:1}}>✕</button>
          </div>
          <div style={{fontSize:13,color:'#2a251f',lineHeight:1.5,marginBottom:10}}>{q.message}</div>
          <div style={{display:'flex',gap:6}}>
            <input id={'qa-'+q.id} placeholder="Your answer…"
              onKeyDown={e=>{if(e.key==='Enter')answer(q,e.target.value);}}
              style={{flex:1,border:'1px solid #ead9a3',borderRadius:6,padding:'6px 10px',fontSize:12,fontFamily:'inherit',outline:'none'}}/>
            <button onClick={()=>answer(q,document.getElementById('qa-'+q.id)?.value||'')}
              style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'6px 12px',borderRadius:6,fontSize:11.5,fontWeight:700,cursor:'pointer',fontFamily:'inherit'}}>Reply</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Memory panel ────────────────────────────────────────────────────────────
function MemoryPanel() {
  const [content, setContent] = React.useState('');
  const [saved, setSaved]     = React.useState(false);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    _get('/memory'+_TQ).then(d => {
      if (d) setContent(d.content || '');
      setLoading(false);
    });
  }, []);

  const save = async () => {
    const r = await fetch('/memory'+_TQ, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({content}),
    });
    if (r.ok) { setSaved(true); setTimeout(() => setSaved(false), 2000); }
  };

  return (
    <div style={{flex:1,overflow:'auto',padding:'20px 24px',display:'flex',flexDirection:'column',gap:12}}>
      <div>
        <div style={{fontSize:13,fontWeight:700,color:'#2a251f',marginBottom:4}}>Global worker context</div>
        <div style={{fontSize:11.5,color:'#8a8072',lineHeight:1.6}}>
          This text is prepended to every task dispatched to any worker. Use it for standing instructions — tool preferences, credentials format, conventions.
        </div>
      </div>

      <div style={{fontSize:10,color:'#b08a2a',background:'#fff8e0',border:'1px solid #f0e0a0',borderRadius:6,padding:'8px 12px',lineHeight:1.6}}>
        Example entries:<br/>
        <code style={{fontFamily:'"SF Mono",monospace',fontSize:10}}>• For Google Workspace, use gws CLI (not direct API calls)</code><br/>
        <code style={{fontFamily:'"SF Mono",monospace',fontSize:10}}>• Slack as user: curl with X-Slack-User-Token. As bot: SLACK_BOT_TOKEN</code><br/>
        <code style={{fontFamily:'"SF Mono",monospace',fontSize:10}}>• Ring price is $349 — never $300</code>
      </div>

      {loading ? (
        <div style={{color:'#c8c0af',fontSize:12}}>loading…</div>
      ) : (
        <textarea
          value={content}
          onChange={e => { setContent(e.target.value); setSaved(false); }}
          placeholder={"Add standing instructions for all workers…\n\nExamples:\n- For Google Workspace, use: gws cli\n- Slack messages as user: curl with user token\n- Slack bot messages: use SLACK_BOT_TOKEN env\n- Ring price: $349"}
          style={{flex:1,minHeight:280,border:'1px solid #ead9a3',borderRadius:8,padding:'12px 14px',fontSize:12.5,lineHeight:1.7,color:'#2a251f',fontFamily:'inherit',resize:'vertical',outline:'none',background:'#fff'}}
        />
      )}

      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}>
        <span style={{fontSize:11,color:'#c8c0af'}}>{content.length} chars · injected on every dispatch</span>
        <button onClick={save} disabled={loading}
          style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'8px 20px',borderRadius:7,fontSize:12.5,fontWeight:700,cursor:'pointer',fontFamily:'inherit',opacity:loading?0.5:1}}>
          {saved ? '✓ Saved' : 'Save'}
        </button>
      </div>
    </div>
  );
}

// ── Infra status panel ──────────────────────────────────────────────────────
function InfraPanel() {
  const [infra, setInfra] = React.useState(null);

  React.useEffect(() => {
    const load = async () => {
      const d = await _get('/infra-status'+_TQ);
      if (d) setInfra(d);
    };
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  if (!infra) return <div style={sEmptyState}>loading infra…</div>;

  const LABELS = {server:'Server', supervisor:'Supervisor', watcher:'Watcher', telegram:'Telegram', monitor:'Monitor'};

  return (
    <div style={{flex:1,overflow:'auto',padding:'16px 24px'}}>
      <div style={{fontSize:10,color:'#a59985',fontWeight:700,textTransform:'uppercase',letterSpacing:0.6,marginBottom:10}}>Orchmux services</div>
      {infra.map(svc => (
        <div key={svc.name} style={{display:'flex',alignItems:'flex-start',gap:10,padding:'10px 14px',borderRadius:8,border:'1px solid #f0e8d0',background:'#fff',marginBottom:6}}>
          <span style={{width:8,height:8,borderRadius:'50%',background:svc.up?'#4caf50':'#e55a6a',flexShrink:0,marginTop:3}}/>
          <div style={{flex:1,minWidth:0}}>
            <div style={{fontSize:12.5,fontWeight:700,color:'#2a251f'}}>{LABELS[svc.name]||svc.name}</div>
            <div style={{fontSize:10.5,color:'#a59985',marginTop:1}}>{svc.description} · <code style={{fontFamily:'"SF Mono",monospace',fontSize:10}}>{svc.session}</code></div>
            {svc.last_line && (
              <div style={{fontSize:10.5,color:'#7c6840',fontFamily:'"SF Mono",monospace',marginTop:4,whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',background:'#fdf8ec',padding:'3px 7px',borderRadius:4}}>
                {svc.last_line}
              </div>
            )}
          </div>
          <span style={{fontSize:10,fontWeight:700,padding:'2px 8px',borderRadius:4,border:'1px solid',
            color:svc.up?'#2e7d32':'#c62828',borderColor:svc.up?'#c8e6c9':'#ffcdd2',
            background:svc.up?'#f0faf0':'#fff0f0',flexShrink:0}}>
            {svc.up ? 'up' : 'down'}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Worker config row (Manage > Workers) ───────────────────────────────────
function WorkerConfigRow({w, killMsg, onKill}) {
  const [slack, setSlack]   = React.useState(w.slackTarget || '');
  const [saved, setSaved]   = React.useState(false);
  const [expanded, setExpanded] = React.useState(false);

  const saveSlack = async () => {
    await fetch('/worker-meta' + _TQ, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session: w.session, slack_target: slack}),
    });
    setSaved(true); setTimeout(() => setSaved(false), 1500);
  };

  return (
    <div style={{borderRadius:8,border:'1px solid #f0e8d0',background:'#fff',overflow:'hidden'}}>
      <div style={{display:'flex',alignItems:'center',gap:10,padding:'9px 12px',cursor:'pointer'}} onClick={()=>setExpanded(x=>!x)}>
        <span style={{width:7,height:7,borderRadius:'50%',background:STATUS_DOT[w.status]||'#ccc',flexShrink:0}}/>
        <div style={{flex:1,minWidth:0}}>
          <div style={{fontSize:12.5,fontWeight:600,color:'#2a251f'}}>{w.session}</div>
          <div style={{fontSize:11,color:'#a59985'}}>{w.domain} · {STATUS_LABEL[w.status]||w.status}
            {w.slackTarget ? <span style={{marginLeft:6,color:'#4a90e2'}}>💬 {w.slackTarget}</span> : null}
          </div>
        </div>
        {killMsg
          ? <span style={{fontSize:11,color:killMsg.startsWith('✓')?'#2e7d32':'#c62828'}}>{killMsg}</span>
          : <button onClick={e=>{e.stopPropagation();onKill();}}
              style={{background:'none',border:'1px solid #f0e8d0',color:'#e55a6a',fontSize:11,padding:'3px 9px',borderRadius:5,cursor:'pointer',fontFamily:'inherit'}}>Kill</button>
        }
        <span style={{color:'#c8c0af',fontSize:10}}>{expanded?'▲':'▼'}</span>
      </div>
      {expanded && (
        <div style={{padding:'10px 12px',borderTop:'1px solid #f5f0e8',background:'#fdf8f0',display:'flex',alignItems:'center',gap:8}}>
          <span style={{fontSize:11,color:'#8a8072',flexShrink:0}}>💬 Slack target</span>
          <input value={slack} onChange={e=>setSlack(e.target.value)} placeholder="Channel or user ID (e.g. C09AQ35HG6P)"
            onKeyDown={e=>{if(e.key==='Enter')saveSlack();}}
            style={{flex:1,border:'1px solid #ead9a3',borderRadius:5,padding:'5px 9px',fontSize:12,fontFamily:'inherit',outline:'none',background:'#fff',color:'#2a251f'}}/>
          <button onClick={saveSlack}
            style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'5px 12px',borderRadius:5,fontSize:11,fontWeight:700,cursor:'pointer',fontFamily:'inherit',flexShrink:0}}>
            {saved ? '✓' : 'Save'}
          </button>
        </div>
      )}
    </div>
  );
}

async function _sendSlackUpdate(worker, paneText) {
  const target = worker.slackTarget;
  if (!target) return {ok: false, error: 'no target configured'};
  const status  = STATUS_LABEL[worker.status] || worker.status;
  const snippet = paneText ? paneText.trim().split('\n').filter(Boolean).slice(-5).join('\n') : '';
  const message = `*${worker.session}* (${worker.domain}) — ${status}\n> ${worker.task || '(idle)'}\n\`\`\`\n${snippet.slice(0,400)}\n\`\`\``;
  const r = await fetch('/slack-send' + _TQ, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({target, message}),
  });
  return r.json();
}

// ── Manage panel ────────────────────────────────────────────────────────────
function ManagePanel({workers, domains}) {
  const [sub, setSub]       = React.useState('spawn');
  const [spName, setSpName] = React.useState('');
  const [spDom, setSpDom]   = React.useState('');
  const [spModel, setSpModel] = React.useState('claude');
  const [spMsg, setSpMsg]   = React.useState('');

  const [atSess, setAtSess] = React.useState('');
  const [atDom, setAtDom]   = React.useState('');
  const [atMsg, setAtMsg]   = React.useState('');

  const [killMsg, setKillMsg] = React.useState({});
  const [restartMsg, setRestartMsg] = React.useState('');

  const spawnWorker = async () => {
    if (!spName.trim() || !spDom) { setSpMsg('⚠ name and domain required'); return; }
    const r = await fetch('/spawn-worker'+_TQ, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session:spName.trim(), domain:spDom, model:spModel})
    });
    const d = await r.json();
    setSpMsg(r.ok ? '✓ spawned '+spName : '⚠ '+(d.detail||'failed'));
    if (r.ok) { setSpName(''); }
  };

  const attachWorker = async () => {
    if (!atSess.trim() || !atDom) { setAtMsg('⚠ session and domain required'); return; }
    const r = await fetch('/attach-worker'+_TQ, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session:atSess.trim(), domain:atDom})
    });
    const d = await r.json();
    setAtMsg(r.ok ? '✓ attached '+atSess : '⚠ '+(d.detail||'failed'));
    if (r.ok) setAtSess('');
  };

  const killWorker = async (session) => {
    const r = await fetch('/kill-session/'+encodeURIComponent(session)+_TQ, {method:'DELETE'});
    const d = await r.json();
    setKillMsg(prev=>({...prev,[session]: r.ok?'✓ killed':'⚠ '+(d.detail||'failed')}));
  };

  const restartServer = async () => {
    setRestartMsg('restarting…');
    await fetch('/restart', {method:'POST'});
    setTimeout(() => setRestartMsg('✓ server restarted'), 3000);
  };

  const subBtnStyle = active => ({
    background:'none', border:'none', borderBottom: active?'2px solid #b08a2a':'2px solid transparent',
    padding:'6px 12px 8px', fontSize:11.5, fontWeight:active?700:500, color:active?'#2a251f':'#a59985',
    cursor:'pointer', fontFamily:'inherit', marginBottom:'-1px',
  });

  const inputStyle = {
    width:'100%', boxSizing:'border-box', border:'1px solid #ead9a3', borderRadius:6,
    padding:'7px 10px', fontSize:12.5, fontFamily:'inherit', outline:'none', background:'#fff', color:'#2a251f',
  };
  const selectStyle = {...inputStyle};

  return (
    <div style={{flex:1,overflow:'auto',padding:'0 24px 20px'}}>
      {/* sub-tab bar */}
      <div style={{display:'flex',borderBottom:'1px solid #f0e8d0',marginBottom:16}}>
        {[['spawn','＋ Spawn'],['attach','⇢ Attach'],['workers','⚙ Workers'],['memory','◈ Memory'],['infra','⬡ Infra'],['server','↺ Server']].map(([k,l])=>(
          <button key={k} onClick={()=>setSub(k)} style={subBtnStyle(sub===k)}>{l}</button>
        ))}
      </div>

      {sub==='spawn' && (
        <div style={{display:'flex',flexDirection:'column',gap:12,maxWidth:380}}>
          <div style={{fontSize:11.5,color:'#8a8072',lineHeight:1.5}}>Create a new tmux session and register it as a worker.</div>
          <div>
            <div style={sLabel}>SESSION NAME</div>
            <input value={spName} onChange={e=>setSpName(e.target.value)} placeholder="e.g. eng-worker-3" style={inputStyle}/>
          </div>
          <div>
            <div style={sLabel}>DOMAIN</div>
            <select value={spDom} onChange={e=>setSpDom(e.target.value)} style={selectStyle}>
              <option value="">— select —</option>
              {(domains.length?domains:Object.keys(_DKW)).map(d=><option key={d} value={d}>{d}</option>)}
            </select>
          </div>
          <div>
            <div style={sLabel}>MODEL</div>
            <select value={spModel} onChange={e=>setSpModel(e.target.value)} style={selectStyle}>
              {['claude','codex','kimi'].map(m=><option key={m} value={m}>{m}</option>)}
            </select>
          </div>
          <button onClick={spawnWorker} style={sBtnPrimary}>▶ Spawn Worker</button>
          {spMsg && <div style={{fontSize:11.5,color:spMsg.startsWith('✓')?'#2e7d32':'#c62828'}}>{spMsg}</div>}
        </div>
      )}

      {sub==='attach' && (
        <div style={{display:'flex',flexDirection:'column',gap:12,maxWidth:380}}>
          <div style={{fontSize:11.5,color:'#8a8072',lineHeight:1.5}}>Register an existing tmux session as a worker.</div>
          <div>
            <div style={sLabel}>TMUX SESSION</div>
            <input value={atSess} onChange={e=>setAtSess(e.target.value)} placeholder="e.g. my-session" style={inputStyle}/>
          </div>
          <div>
            <div style={sLabel}>DOMAIN</div>
            <select value={atDom} onChange={e=>setAtDom(e.target.value)} style={selectStyle}>
              <option value="">— select —</option>
              {(domains.length?domains:Object.keys(_DKW)).map(d=><option key={d} value={d}>{d}</option>)}
            </select>
          </div>
          <button onClick={attachWorker} style={{...sBtnPrimary,background:'#5a7a9a'}}>⇢ Attach Session</button>
          {atMsg && <div style={{fontSize:11.5,color:atMsg.startsWith('✓')?'#2e7d32':'#c62828'}}>{atMsg}</div>}
        </div>
      )}

      {sub==='workers' && (
        <div style={{display:'flex',flexDirection:'column',gap:6}}>
          {workers.length===0 && <div style={{color:'#c8c0af',fontSize:12}}>No workers loaded yet.</div>}
          {workers.map(w=>(
            <WorkerConfigRow key={w.session} w={w}
              killMsg={killMsg[w.session]}
              onKill={()=>killWorker(w.session)}/>
          ))}
        </div>
      )}

      {sub==='memory' && <MemoryPanel/>}
      {sub==='infra' && <InfraPanel/>}

      {sub==='server' && (
        <div style={{display:'flex',flexDirection:'column',gap:14,maxWidth:380}}>
          <div style={{fontSize:11.5,color:'#8a8072',lineHeight:1.55}}>Re-exec the server in-place. Picks up code changes and clears in-memory state. Workers keep running.</div>
          <button onClick={restartServer} style={{...sBtnPrimary,background:'#c62828'}}>↺ Restart Server</button>
          {restartMsg && <div style={{fontSize:12,color:restartMsg.startsWith('✓')?'#2e7d32':'#8a8072'}}>{restartMsg}</div>}
        </div>
      )}
    </div>
  );
}

const sLabel    = {fontSize:10,color:'#a59985',fontWeight:700,textTransform:'uppercase',letterSpacing:0.5,marginBottom:5};
const sBtnPrimary = {background:'#2a251f',color:'#f5d76b',border:'none',padding:'9px',borderRadius:7,fontSize:12.5,fontWeight:700,cursor:'pointer',fontFamily:'inherit'};

// ── Alerts banner ───────────────────────────────────────────────────────────
function AlertsBanner({workers}) {
  const alerts = workers.filter(w =>
    w.status === 'blocked' || w.status === 'missing' || w.auth === 'auth_error'
  );
  if (!alerts.length) return null;
  return (
    <div style={{margin:'6px 10px 0',borderRadius:7,overflow:'hidden'}}>
      {alerts.map(w => {
        const isAuth    = w.auth === 'auth_error';
        const isBlocked = w.status === 'blocked';
        const bg    = isAuth ? '#fff0f0' : isBlocked ? '#fff5e0' : '#f5f5f5';
        const color = isAuth ? '#c62828' : isBlocked ? '#b85c00' : '#6b6b6b';
        const icon  = isAuth ? '🔒' : isBlocked ? '⚠️' : '–';
        const label = isAuth ? 'auth error' : isBlocked ? 'needs input' : 'gone';
        return (
          <div key={w.session} style={{display:'flex',alignItems:'center',gap:7,padding:'6px 10px',background:bg,borderBottom:'1px solid rgba(0,0,0,0.06)'}}>
            <span style={{fontSize:12}}>{icon}</span>
            <span style={{fontSize:11,fontFamily:'"SF Mono",monospace',color,fontWeight:600}}>{w.session}</span>
            <span style={{fontSize:11,color,flex:1}}>{label}{w.task ? ` · ${w.task.slice(0,50)}` : ''}</span>
          </div>
        );
      })}
    </div>
  );
}

// ── Meta edit modal ─────────────────────────────────────────────────────────
function MetaModal({worker, onClose}) {
  const [name, setName]   = React.useState(worker.displayName || '');
  const [saved, setSaved] = React.useState(false);

  const save = async () => {
    await fetch('/worker-meta'+_TQ, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session: worker.session, display_name: name, role: ''}),
    });
    setSaved(true);
    setTimeout(onClose, 800);
  };

  return (
    <div style={{position:'fixed',inset:0,background:'rgba(0,0,0,0.35)',zIndex:200,display:'flex',alignItems:'center',justifyContent:'center'}}
         onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
      <div style={{background:'#fff',borderRadius:12,padding:'20px 24px',width:320,boxShadow:'0 8px 32px rgba(0,0,0,0.18)'}}>
        <div style={{fontSize:13,fontWeight:700,color:'#2a251f',marginBottom:4}}>Rename worker</div>
        <div style={{fontSize:11,color:'#a59985',marginBottom:14,fontFamily:'"SF Mono",monospace'}}>{worker.session}</div>
        <input
          autoFocus
          value={name}
          onChange={e=>setName(e.target.value)}
          onKeyDown={e=>{if(e.key==='Enter')save();if(e.key==='Escape')onClose();}}
          placeholder="Display name (optional)"
          style={{width:'100%',boxSizing:'border-box',border:'1px solid #ead9a3',borderRadius:7,padding:'8px 11px',fontSize:13,fontFamily:'inherit',outline:'none',color:'#2a251f'}}
        />
        <div style={{display:'flex',gap:8,marginTop:14,justifyContent:'flex-end'}}>
          <button onClick={onClose} style={{background:'none',border:'1px solid #e8e0cc',padding:'7px 14px',borderRadius:6,fontSize:12,cursor:'pointer',fontFamily:'inherit',color:'#8a8072'}}>Cancel</button>
          <button onClick={save} style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'7px 18px',borderRadius:6,fontSize:12,fontWeight:700,cursor:'pointer',fontFamily:'inherit'}}>
            {saved ? '✓ Saved' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Vault file card (WhatsApp-style inline preview) ─────────────────────────
const VAULT_NAME    = window.__ORCHMUX_VAULT_NAME__ || 'vault';
// No external Obsidian editor — inline edit writes directly to vault, Obsidian Sync propagates

function useVaultFile(vaultPath) {
  const [data, setData] = React.useState(null);
  const reload = () => _get('/vault/read' + _TQA + 'path=' + encodeURIComponent(vaultPath)).then(d => d && setData(d));
  React.useEffect(() => { reload(); }, [vaultPath]);
  return [data, reload];
}

async function saveVaultFile(path, content) {
  const r = await fetch('/vault/write' + _TQ, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({path, content}),
  });
  return r.ok;
}

function VaultCard({vaultPath, onClose}) {
  const [data, reload]    = useVaultFile(vaultPath);
  const [mode, setMode]   = React.useState('collapsed'); // collapsed | preview | edit
  const [editTab, setEditTab] = React.useState('write'); // write | preview
  const [draft, setDraft] = React.useState('');
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved]   = React.useState(false);
  const bodyRef    = React.useRef(null);
  const editPreRef = React.useRef(null);
  const html = React.useMemo(() => data ? mdParse(cleanTermOutput(data.content)) : '', [data]);
  const draftHtml  = React.useMemo(() => mdParse(draft), [draft]);

  React.useEffect(() => { if (mode === 'preview') renderParsedHtml(bodyRef.current, html); }, [mode, html]);
  React.useEffect(() => { if (mode === 'edit' && editTab === 'preview') renderParsedHtml(editPreRef.current, draftHtml); }, [editTab, draftHtml, mode]);

  const startEdit = () => { setDraft(data.content); setMode('edit'); setEditTab('write'); setSaved(false); };
  const save = async () => {
    setSaving(true);
    const ok = await saveVaultFile(vaultPath, draft);
    setSaving(false);
    if (ok) { setSaved(true); reload(); setMode('preview'); setTimeout(() => setSaved(false), 2000); }
  };

  if (!data) return <div style={sCard}><span style={{fontSize:11,color:'#c8c0af'}}>loading…</span></div>;
  const snippet = data.content.replace(/^#+\s.+\n?/m,'').replace(/\n+/g,' ').trim().slice(0,120);

  return (
    <div style={sCard}>
      <div style={{display:'flex',alignItems:'flex-start',gap:8}}>
        <span style={{fontSize:18,flexShrink:0,lineHeight:1.2}}>📄</span>
        <div style={{flex:1,minWidth:0}}>
          <div style={{fontSize:13,fontWeight:700,color:'#2a251f',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{data.name.replace(/\.md$/,'')}</div>
          <div style={{fontSize:10.5,color:'#a59985',fontFamily:'"SF Mono",monospace',marginBottom:4,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{vaultPath}</div>
          {mode==='collapsed' && snippet && (
            <div style={{fontSize:12,color:'#5a4820',lineHeight:1.5,overflow:'hidden',display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical'}}>{snippet}…</div>
          )}
        </div>
        {onClose && <button onClick={onClose} style={{background:'none',border:'none',color:'#c8c0af',fontSize:13,cursor:'pointer',padding:'0 2px',flexShrink:0}}>✕</button>}
      </div>

      {mode==='preview' && (
        <div style={{marginTop:10,borderTop:'1px solid #f0e8d0',paddingTop:10,maxHeight:320,overflow:'auto'}}>
          <div ref={bodyRef} className="clean-pane"/>
        </div>
      )}
      {mode==='edit' && (
        <div style={{marginTop:10,borderTop:'1px solid #f0e8d0',paddingTop:10}}>
          {/* write / preview tab toggle */}
          <div style={{display:'flex',gap:0,marginBottom:8,borderRadius:6,overflow:'hidden',border:'1px solid #e8ddb8',width:'fit-content'}}>
            {[['write','✍ Write'],['preview','👁 Preview']].map(([t,l])=>(
              <button key={t} onClick={()=>setEditTab(t)}
                style={{padding:'4px 12px',fontSize:11,fontWeight:editTab===t?700:400,cursor:'pointer',fontFamily:'inherit',border:'none',
                  background:editTab===t?'#2a251f':'#fffef8',color:editTab===t?'#f5d76b':'#8a8072'}}>
                {l}
              </button>
            ))}
          </div>
          {editTab==='write' ? (
            <textarea value={draft} onChange={e=>setDraft(e.target.value)}
              style={{width:'100%',boxSizing:'border-box',minHeight:220,border:'1px solid #ead9a3',borderRadius:7,padding:'10px 12px',
                fontSize:12.5,fontFamily:'"SF Mono","Fira Code",monospace',lineHeight:1.6,resize:'vertical',outline:'none',color:'#2a251f',background:'#fffef8'}}/>
          ) : (
            <div ref={editPreRef} className="clean-pane"
              style={{minHeight:120,padding:'10px 12px',border:'1px solid #f0e8d0',borderRadius:7,background:'#fff'}}/>
          )}
          <div style={{display:'flex',gap:6,marginTop:8}}>
            <button onClick={save} disabled={saving}
              style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'5px 14px',borderRadius:5,fontSize:11.5,fontWeight:700,cursor:'pointer',fontFamily:'inherit',opacity:saving?0.6:1}}>
              {saving ? 'Saving…' : saved ? '✓ Saved' : 'Save'}
            </button>
            <button onClick={()=>setMode('preview')}
              style={{background:'none',border:'1px solid #e8ddb8',borderRadius:5,padding:'5px 10px',fontSize:11.5,color:'#a59985',cursor:'pointer',fontFamily:'inherit'}}>Cancel</button>
          </div>
        </div>
      )}

      <div style={{display:'flex',gap:6,marginTop:8}}>
        {mode==='collapsed'
          ? <button onClick={()=>setMode('preview')} style={sBtnSm}>▾ Read</button>
          : <button onClick={()=>setMode('collapsed')} style={sBtnSm}>▴ Close</button>}
        {mode!=='edit' && <button onClick={startEdit} style={{...sBtnSm,color:'#b08a2a',borderColor:'#e8d070'}}>✎ Edit</button>}
      </div>
    </div>
  );
}

const sBtnSm = {background:'none',border:'1px solid #e8ddb8',borderRadius:5,padding:'3px 9px',fontSize:11,color:'#8a8072',cursor:'pointer',fontFamily:'inherit',fontWeight:600};

const sCard = {background:'#fffef5',border:'1px solid #e8d98a',borderLeft:'3px solid #f5c842',borderRadius:8,padding:'10px 12px',margin:'8px 0'};

function useVaultCards(rawText) {
  const [cards, setCards] = React.useState([]);
  React.useEffect(() => {
    if (!rawText) { setCards([]); return; }
    const found = new Set();
    const vaultPat = new RegExp('(?:~\\/)?(?:obsidian-vault|vault)\\/([^\\s\\)"\'`\\]]+\\.md)', 'g');
    const pathHits = rawText.match(vaultPat) || [];
    pathHits.forEach(m => {
      const p = m.replace(/^(?:~\/)?(?:obsidian-vault|vault)\//, '');
      if (p) found.add(p);
    });
    const linkHits = rawText.match(/obsidian:\/\/open\?[^"'\s]*file=[^&"'\s]+/g) || [];
    linkHits.forEach(m => {
      const frag = m.match(/file=([^&"'\s]+)/);
      if (frag) { try { found.add(decodeURIComponent(frag[1]) + '.md'); } catch(e) {} }
    });
    setCards([...found]);
  }, [rawText]);
  return cards;
}

function SessionNoteCard({session}) {
  const [exists, setExists] = React.useState(false);
  const today    = new Date().toISOString().slice(0,10);
  const notePath = `AI-Systems/Claude-Logs/Sessions/${today}-${session}.md`;
  React.useEffect(() => {
    _get('/vault/read' + _TQA + 'path=' + encodeURIComponent(notePath)).then(d => setExists(!!d));
  }, [session]);
  if (!exists) return null;
  return (
    <div style={{padding:'0 28px 4px'}}>
      <div style={{fontSize:10,color:'#a59985',marginBottom:2,fontWeight:600,textTransform:'uppercase',letterSpacing:0.4}}>Session note</div>
      <VaultCard vaultPath={notePath}/>
    </div>
  );
}

// ── URL categorisation ───────────────────────────────────────────────────────
function _categoriseUrl(url) {
  if (/docs\.google\.com\/document/.test(url))      return {icon:'📝', label:'Google Doc'};
  if (/docs\.google\.com\/spreadsheets/.test(url))  return {icon:'📊', label:'Google Sheet'};
  if (/docs\.google\.com\/presentation/.test(url))  return {icon:'📑', label:'Google Slides'};
  if (/drive\.google\.com/.test(url))               return {icon:'📁', label:'Google Drive'};
  if (/github\.com.*\/pull\//.test(url))            return {icon:'🔀', label:'GitHub PR'};
  if (/github\.com/.test(url))                      return {icon:'💻', label:'GitHub'};
  if (/notion\.so/.test(url))                       return {icon:'🗒', label:'Notion'};
  if (/slack\.com/.test(url))                       return {icon:'💬', label:'Slack'};
  return {icon:'🔗', label:'Link'};
}

const VAULT_TOP_DIRS = ['AI-Systems','Engineering','Data','Research','Legal','Finance','Product','Operations','Meetings','Inbox','Notes','Hiring'];

function _extractLinks(text) {
  if (!text) return {vaultPaths: [], urls: []};
  const vaultMap = new Map();  // path → {path, context}
  const urlMap   = new Map();  // url  → {url, context}

  const lines = text.split('\n');
  lines.forEach((line, lineIdx) => {
    // context = surrounding line text (trimmed, up to 80 chars)
    const ctx = line.trim().slice(0, 80);

    // Full vault paths
    const fullHits = line.match(/(?:~\/)?(?:obsidian-vault|vault)\/([^\s\)"'`\]]+\.md)/g) || [];
    fullHits.forEach(m => {
      const p = m.replace(/^(?:~\/)?(?:obsidian-vault|vault)\//, '');
      if (p && !vaultMap.has(p)) vaultMap.set(p, {path:p, context:ctx});
    });

    // Relative vault paths
    const topDirPat = new RegExp('\\b(' + VAULT_TOP_DIRS.join('|') + ')\\/([^\\s\\)"\'`\\]]+\\.md)', 'g');
    const relHits = line.match(topDirPat) || [];
    relHits.forEach(m => { if (!vaultMap.has(m)) vaultMap.set(m, {path:m, context:ctx}); });

    // URLs
    const urlHits = line.match(/https?:\/\/[^\s\)"'`\]>]+/g) || [];
    urlHits.forEach(u => {
      if (/localhost|127\.0\.0\.|100\.123\./.test(u)) return;
      const clean = u.replace(/[.,;:]+$/, '');
      if (!urlMap.has(clean)) urlMap.set(clean, {url:clean, context:ctx});
    });
  });

  return {vaultPaths: [...vaultMap.values()], urls: [...urlMap.values()]};
}

// ── Worker Docs panel — links + vault files produced in this session ─────────
function WorkerDocsPanel({worker, paneRawText}) {
  // Track firstSeen timestamps — persists across re-renders but resets on worker change
  const seenRef    = React.useRef({});
  const workerKey  = worker ? worker.session : null;
  const prevWorker = React.useRef(null);
  if (prevWorker.current !== workerKey) {
    seenRef.current = {};
    prevWorker.current = workerKey;
  }

  const {vaultPaths, urls} = React.useMemo(() => {
    const extracted = _extractLinks(paneRawText);
    const now = new Date();
    const stamp = now.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
    // Record first-seen time for any new key
    extracted.urls.forEach(({url}) => {
      if (!seenRef.current[url]) seenRef.current[url] = stamp;
    });
    extracted.vaultPaths.forEach(({path}) => {
      if (!seenRef.current[path]) seenRef.current[path] = stamp;
    });
    return extracted;
  }, [paneRawText]);

  if (!worker) return <div style={sEmptyState}>Select a task to see its docs</div>;

  const empty = vaultPaths.length === 0 && urls.length === 0;

  return (
    <div style={{flex:1,overflow:'auto',padding:'16px 20px'}}>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:14}}>
        <div>
          <div style={{fontSize:13,fontWeight:700,color:'#2a251f'}}>Docs & Links</div>
          <div style={{fontSize:11,color:'#a59985',marginTop:1,fontFamily:'"SF Mono",monospace'}}>{worker.session}</div>
        </div>
        <span style={{fontSize:11,color:'#c8c0af'}}>edits sync via Obsidian Sync</span>
      </div>

      {empty && (
        <div style={{color:'#c8c0af',fontSize:12,lineHeight:1.8}}>
          No docs or links yet for this session.<br/>
          <span style={{fontSize:11}}>Google Docs, Sheets, GitHub links, and vault files Claude creates will appear here.</span>
        </div>
      )}

      {urls.length > 0 && (
        <>
          <div style={{fontSize:10,color:'#a59985',fontWeight:700,textTransform:'uppercase',letterSpacing:0.5,marginBottom:8}}>Links created</div>
          {urls.map(({url, context}) => {
            const {icon, label} = _categoriseUrl(url);
            const display = url.replace(/^https?:\/\/(www\.)?/,'').slice(0,55);
            const ts = seenRef.current[url];
            return (
              <a key={url} href={url} target="_blank" rel="noreferrer"
                style={{display:'flex',alignItems:'center',gap:9,padding:'9px 12px',borderRadius:8,border:'1px solid #f0e8d0',background:'#fff',marginBottom:6,textDecoration:'none'}}>
                <span style={{fontSize:18,flexShrink:0}}>{icon}</span>
                <div style={{flex:1,minWidth:0}}>
                  <div style={{display:'flex',alignItems:'baseline',gap:6}}>
                    <span style={{fontSize:12,fontWeight:600,color:'#2a251f'}}>{label}</span>
                    {ts && <span style={{fontSize:10,color:'#c8b878'}}>{ts}</span>}
                  </div>
                  <div style={{fontSize:10.5,color:'#a59985',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{display}</div>
                  {context && context !== url && (
                    <div style={{fontSize:10.5,color:'#8a8072',marginTop:2,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',fontStyle:'italic'}}>{context}</div>
                  )}
                </div>
                <span style={{fontSize:11,color:'#c8c0af',flexShrink:0}}>↗</span>
              </a>
            );
          })}
        </>
      )}

      {vaultPaths.length > 0 && (
        <>
          <div style={{fontSize:10,color:'#a59985',fontWeight:700,textTransform:'uppercase',letterSpacing:0.5,margin:'14px 0 8px'}}>Vault files</div>
          {vaultPaths.map(({path, context}) => {
            const ts = seenRef.current[path];
            return <DocCard key={path} item={{path, name:path.split('/').pop(), snippet:context||'', ts}}/>;
          })}
        </>
      )}
    </div>
  );
}

function DocCard({item}) {
  const [expanded, setExpanded] = React.useState(false);
  const [html, setHtml]         = React.useState('');
  const bodyRef = React.useRef(null);

  const load = () => {
    if (html) return;
    _get('/vault/read' + _TQA + 'path=' + encodeURIComponent(item.path)).then(d => {
      if (d) setHtml(mdParse(cleanTermOutput(d.content)));
    });
  };

  React.useEffect(() => {
    if (expanded) renderParsedHtml(bodyRef.current, html);
  }, [expanded, html]);

  const toggle = () => { if (!expanded) load(); setExpanded(x => !x); };

  return (
    <div style={{borderRadius:9,border:'1px solid #f0e8d0',background:'#fff',marginBottom:8,overflow:'hidden'}}>
      <div style={{padding:'11px 14px',display:'flex',gap:10,alignItems:'flex-start',cursor:'pointer'}} onClick={toggle}>
        <span style={{fontSize:16,flexShrink:0,lineHeight:1.2}}>📄</span>
        <div style={{flex:1,minWidth:0}}>
          <div style={{display:'flex',alignItems:'baseline',gap:6}}>
            <span style={{fontSize:13,fontWeight:600,color:'#2a251f',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{item.name.replace(/\.md$/,'')}</span>
            {item.ts && <span style={{fontSize:10,color:'#c8b878',flexShrink:0}}>{item.ts}</span>}
          </div>
          <div style={{fontSize:10.5,color:'#a59985',fontFamily:'"SF Mono",monospace',marginTop:1,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{item.path}</div>
          {!expanded && item.snippet && (
            <div style={{fontSize:11.5,color:'#7c6430',marginTop:4,lineHeight:1.5}}>{item.snippet}…</div>
          )}
        </div>
        <span style={{color:'#c8c0af',fontSize:11,flexShrink:0}}>{expanded?'▲':'▼'}</span>
      </div>
      {expanded && (
        <div style={{borderTop:'1px solid #f5f0e8'}}>
          <div style={{maxHeight:400,overflow:'auto',padding:'14px 16px'}}>
            <div ref={bodyRef} className="clean-pane"/>
          </div>
          <div style={{padding:'8px 14px',borderTop:'1px solid #f5f0e8',fontSize:10.5,color:'#c8c0af'}}>
            ✓ Saved — Obsidian Sync will propagate
          </div>
        </div>
      )}
    </div>
  );
}

// ── Vault panel ─────────────────────────────────────────────────────────────
const VAULT_ROOTS = ['AI-Systems','Engineering','Data','Research','Legal','Finance','Product','Operations','Meetings','Inbox'];

function VaultPanel() {
  const [path, setPath]       = React.useState('');
  const [entries, setEntries] = React.useState(null);
  const [filePath, setFilePath] = React.useState(null);
  const [editMode, setEditMode] = React.useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = React.useState(false);
  const [fileData, reloadFile]  = useVaultFile(filePath);
  const [draft, setDraft]       = React.useState('');
  const [saving, setSaving]     = React.useState(false);
  const [saved, setSaved]       = React.useState(false);
  const fileRef    = React.useRef(null);
  const previewRef = React.useRef(null);
  const html       = React.useMemo(() => fileData ? mdParse(cleanTermOutput(fileData.content)) : '', [fileData]);
  const draftHtml  = React.useMemo(() => mdParse(draft), [draft]);

  const loadDir = async (p) => {
    setEntries(null); setFilePath(null); setEditMode(false);
    const d = await _get('/vault/ls' + _TQA + 'path=' + encodeURIComponent(p));
    if (d) { setEntries(d); setPath(p); }
  };

  React.useEffect(() => { loadDir(''); }, []);
  React.useEffect(() => { if (!editMode) renderParsedHtml(fileRef.current, html); }, [html, editMode]);
  React.useEffect(() => { if (editMode) renderParsedHtml(previewRef.current, draftHtml); }, [draftHtml, editMode]);

  const [exporting, setExporting]   = React.useState(false);
  const [exportUrl, setExportUrl]   = React.useState(null);
  const [exportErr, setExportErr]   = React.useState(null);

  const openFile = (p) => { setFilePath(p); setEditMode(false); setSaved(false); setExportUrl(null); setExportErr(null); };
  const startEdit = () => { setDraft(fileData.content); setEditMode(true); setSaved(false); };
  const save = async () => {
    setSaving(true);
    const ok = await saveVaultFile(filePath, draft);
    setSaving(false);
    if (ok) { setSaved(true); reloadFile(); setEditMode(false); setTimeout(() => setSaved(false), 3000); }
  };
  const exportToDoc = async () => {
    setExporting(true); setExportErr(null); setExportUrl(null);
    try {
      const r = await fetch('/vault/export-doc' + _TQ, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({path: filePath}),
      });
      const d = await r.json();
      if (r.ok && d.url) { setExportUrl(d.url); }
      else { setExportErr(d.detail || d.error || 'Export failed'); }
    } catch(e) { setExportErr('Network error'); }
    setExporting(false);
  };

  const crumbs = path ? path.split('/').filter(Boolean) : [];

  return (
    <div style={{flex:1,display:'flex',overflow:'hidden'}}>
      {/* tree sidebar — collapsible */}
      {sidebarCollapsed ? (
        <div style={{width:28,borderRight:'1px solid #f0e8d0',background:'#fdf8f0',display:'flex',flexDirection:'column',alignItems:'center',padding:'10px 0',flexShrink:0}}>
          <button onClick={()=>setSidebarCollapsed(false)} title="Expand folders"
            style={{background:'none',border:'none',color:'#a59985',fontSize:14,cursor:'pointer',padding:4}}>›</button>
        </div>
      ) : (
      <div style={{width:200,borderRight:'1px solid #f0e8d0',overflow:'auto',background:'#fdf8f0',flexShrink:0,display:'flex',flexDirection:'column'}}>
        {/* root shortcuts header */}
        <div style={{padding:'10px 10px 4px',flexShrink:0}}>
          <div style={{display:'flex',alignItems:'center',marginBottom:6}}>
            <div style={{flex:1,fontSize:9.5,color:'#c8b878',fontWeight:700,textTransform:'uppercase',letterSpacing:0.6}}>Quick folders</div>
            <button onClick={()=>setSidebarCollapsed(true)} title="Collapse"
              style={{background:'none',border:'none',color:'#c8c0af',fontSize:13,cursor:'pointer',padding:'0 2px'}}>‹</button>
          </div>
          {VAULT_ROOTS.map(r => (
            <div key={r} onClick={()=>loadDir(r)}
              style={{fontSize:11.5,padding:'4px 8px',borderRadius:5,cursor:'pointer',color:path===r?'#2a251f':'#7c6430',fontWeight:path===r?700:400,background:path===r?'#fff8d0':'transparent',marginBottom:2}}>
              📁 {r}
            </div>
          ))}
          <div style={{borderTop:'1px solid #f0e8d0',margin:'8px 0 4px'}}/>
          <div onClick={()=>loadDir('')}
            style={{fontSize:11.5,padding:'4px 8px',borderRadius:5,cursor:'pointer',color:path===''?'#2a251f':'#a59985',fontWeight:path===''?700:400,background:path===''?'#fff8d0':'transparent'}}>
            ⬡ All
          </div>
        </div>

        {/* entries */}
        <div style={{padding:'0 6px 12px',flex:1,overflow:'auto'}}>
          {!entries && <div style={{padding:'8px 10px',color:'#c8c0af',fontSize:11}}>loading…</div>}
          {entries && entries.map(e => (
            <div key={e.path} onClick={()=> e.is_dir ? loadDir(e.path) : openFile(e.path)}
              style={{display:'flex',alignItems:'center',gap:5,fontSize:11.5,padding:'4px 8px',borderRadius:5,cursor:'pointer',
                color:filePath===e.path?'#2a251f':'#5a4820',
                background:filePath===e.path?'#fff8d0':'transparent',marginBottom:1}}>
              <span style={{flexShrink:0,fontSize:11}}>{e.is_dir ? '📁' : '📄'}</span>
              <span style={{overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{e.name.replace(/\.md$/,'')}</span>
            </div>
          ))}
        </div>
      </div>
      )}

      {/* content pane */}
      <div style={{flex:1,overflow:'hidden',display:'flex',flexDirection:'column'}}>
        {!filePath ? (
          <div style={{flex:1,display:'flex',alignItems:'center',justifyContent:'center',color:'#c8c0af',fontSize:13,flexDirection:'column',gap:8}}>
            <span style={{fontSize:28}}>📂</span>
            <span>Select a note from the sidebar</span>
          </div>
        ) : !fileData ? (
          <div style={{flex:1,display:'flex',alignItems:'center',justifyContent:'center',color:'#c8c0af',fontSize:13}}>loading…</div>
        ) : (
          <>
            <div style={{padding:'14px 24px 10px',borderBottom:'1px solid #f0e8d0',flexShrink:0}}>
              <div style={{display:'flex',alignItems:'center',gap:10}}>
                <div style={{flex:1,minWidth:0}}>
                  <div style={{fontSize:16,fontWeight:700,color:'#2a251f',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{fileData.name.replace(/\.md$/,'')}</div>
                  <div style={{fontSize:10.5,color:'#a59985',marginTop:2,fontFamily:'"SF Mono",monospace',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{filePath}</div>
                  {saved && <div style={{fontSize:10.5,color:'#2e7d32',marginTop:2}}>✓ Saved — syncing via Obsidian Sync</div>}
                </div>
                {!editMode && (<>
                  <button onClick={startEdit}
                    style={{fontSize:11,padding:'5px 11px',borderRadius:6,border:'1px solid #ead9a3',color:'#b08a2a',background:'#fff8e0',fontWeight:600,flexShrink:0,cursor:'pointer',fontFamily:'inherit'}}>
                    ✎ Edit
                  </button>
                  <button onClick={exportToDoc} disabled={exporting}
                    style={{fontSize:11,padding:'5px 11px',borderRadius:6,border:'1px solid #c8e6c9',color:'#2e7d32',background:'#f0faf0',fontWeight:600,flexShrink:0,cursor:'pointer',fontFamily:'inherit',opacity:exporting?0.6:1}}>
                    {exporting ? '⏳ Exporting…' : '📝 → Doc'}
                  </button>
                </>)}
              </div>
              {exportUrl && (
                <div style={{marginTop:8,padding:'7px 10px',background:'#f0faf0',borderRadius:6,border:'1px solid #c8e6c9',display:'flex',alignItems:'center',gap:8}}>
                  <span style={{fontSize:11,color:'#2e7d32',fontWeight:600}}>✓ Google Doc:</span>
                  <a href={exportUrl} target="_blank" rel="noreferrer"
                    style={{fontSize:11,color:'#1a6b2a',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',flex:1}}>
                    {exportUrl}
                  </a>
                  <button onClick={()=>{navigator.clipboard.writeText(exportUrl);}}
                    style={{background:'none',border:'1px solid #c8e6c9',borderRadius:4,padding:'2px 7px',fontSize:10,color:'#2e7d32',cursor:'pointer',fontFamily:'inherit',flexShrink:0}}>
                    Copy
                  </button>
                </div>
              )}
              {exportErr && (
                <div style={{marginTop:8,padding:'7px 10px',background:'#fff0f0',borderRadius:6,border:'1px solid #ffcdd2',fontSize:11,color:'#c62828'}}>
                  ⚠ Export failed: {exportErr}
                </div>
              )}
            </div>
            {editMode ? (
              <>
                {/* edit mode toolbar — always visible */}
                <div style={{padding:'8px 16px',borderBottom:'1px solid #f0e8d0',background:'#fdf8f0',display:'flex',alignItems:'center',gap:8,flexShrink:0}}>
                  <span style={{fontSize:10,fontWeight:700,color:'#c8b878',textTransform:'uppercase',letterSpacing:0.5,flex:1}}>Editing — {fileData.name.replace(/\.md$/,'')}</span>
                  <button onClick={save} disabled={saving}
                    style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'5px 16px',borderRadius:6,fontSize:12,fontWeight:700,cursor:'pointer',fontFamily:'inherit',opacity:saving?0.6:1}}>
                    {saving ? 'Saving…' : '✓ Save'}
                  </button>
                  <button onClick={()=>setEditMode(false)}
                    style={{background:'none',border:'1px solid #e8ddb8',borderRadius:6,padding:'5px 12px',fontSize:12,color:'#8a8072',cursor:'pointer',fontFamily:'inherit'}}>
                    ✕ Cancel
                  </button>
                  {saved && <span style={{fontSize:11,color:'#2e7d32'}}>✓ Syncing</span>}
                </div>
                {/* split: markdown left, preview right */}
                <div style={{flex:1,display:'flex',minHeight:0,overflow:'hidden'}}>
                  {/* left: raw markdown */}
                  <div style={{flex:1,display:'flex',flexDirection:'column',borderRight:'1px solid #f0e8d0',minWidth:0,minHeight:0}}>
                    <div style={{padding:'4px 14px',background:'#faf6ee',borderBottom:'1px solid #f0e8d0',fontSize:9.5,fontWeight:700,color:'#c8b878',textTransform:'uppercase',letterSpacing:0.5,flexShrink:0}}>
                      Markdown
                    </div>
                    {/* wrapper with position:relative so textarea can be position:absolute */}
                    <div style={{flex:1,position:'relative',minHeight:0}}>
                      <textarea value={draft} onChange={e=>setDraft(e.target.value)}
                        style={{position:'absolute',inset:0,width:'100%',height:'100%',
                          border:'none',padding:'14px 16px',fontSize:12.5,
                          fontFamily:'"SF Mono","Fira Code",monospace',lineHeight:1.7,
                          resize:'none',outline:'none',color:'#2a251f',background:'#fffef8',
                          overflowY:'auto',boxSizing:'border-box'}}/>
                    </div>
                  </div>
                  {/* right: live preview */}
                  <div style={{flex:1,display:'flex',flexDirection:'column',minWidth:0,minHeight:0}}>
                    <div style={{padding:'4px 14px',background:'#faf6ee',borderBottom:'1px solid #f0e8d0',fontSize:9.5,fontWeight:700,color:'#c8b878',textTransform:'uppercase',letterSpacing:0.5,flexShrink:0}}>
                      Preview
                    </div>
                    <div style={{flex:1,overflow:'auto',padding:'16px 20px',minHeight:0}}>
                      <div ref={previewRef} className="clean-pane"/>
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <div style={{flex:1,overflow:'auto',padding:'20px 28px'}}>
                <div ref={fileRef} className="clean-pane"/>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── Todo panel ───────────────────────────────────────────────────────────────
function TodoPanel() {
  const [items, setItems]     = React.useState(null);
  const [input, setInput]     = React.useState('');
  const inputRef = React.useRef(null);

  React.useEffect(() => {
    _get('/todos' + _TQ).then(d => setItems(d || []));
  }, []);

  const persist = (next) => {
    setItems(next);
    fetch('/todos' + _TQ, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(next)});
  };

  const add = () => {
    const t = input.trim();
    if (!t) return;
    persist([...(items||[]), {id: Date.now(), text: t, done: false}]);
    setInput('');
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  const toggle = (id) => persist(items.map(it => it.id===id ? {...it, done:!it.done} : it));
  const remove = (id) => persist(items.filter(it => it.id!==id));
  const dispatch = (item) => {
    fetch('/dispatch', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task: item.text})});
    remove(item.id);
  };

  if (!items) return <div style={sEmptyState}>loading…</div>;

  const open = items.filter(i => !i.done);
  const done = items.filter(i => i.done);

  return (
    <div style={{flex:1,overflow:'auto',padding:'16px 24px',display:'flex',flexDirection:'column',gap:10}}>
      <div style={{fontSize:13,fontWeight:700,color:'#2a251f'}}>Quick notes & ideas</div>

      {/* add input */}
      <div style={{display:'flex',gap:8}}>
        <input ref={inputRef} value={input} onChange={e=>setInput(e.target.value)}
          onKeyDown={e=>{if(e.key==='Enter')add();}}
          placeholder="Add a note or task idea…"
          style={{flex:1,border:'1px solid #ead9a3',borderRadius:7,padding:'8px 11px',fontSize:13,fontFamily:'inherit',outline:'none',color:'#2a251f',background:'#fff'}}/>
        <button onClick={add}
          style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'8px 16px',borderRadius:7,fontSize:12,fontWeight:700,cursor:'pointer',fontFamily:'inherit'}}>＋</button>
      </div>

      {/* open items */}
      {open.length === 0 && done.length === 0 && (
        <div style={{color:'#c8c0af',fontSize:12,lineHeight:1.8}}>
          Capture ideas, reminders, things to dispatch later.<br/>
          <span style={{fontSize:11}}>Hit ↗ to dispatch directly to a worker.</span>
        </div>
      )}
      {open.map(item => (
        <div key={item.id} style={{display:'flex',alignItems:'center',gap:9,padding:'9px 12px',borderRadius:8,border:'1px solid #f0e8d0',background:'#fff',group:'true'}}>
          <input type="checkbox" checked={false} onChange={()=>toggle(item.id)} style={{flexShrink:0,cursor:'pointer'}}/>
          <span style={{flex:1,fontSize:13,color:'#2a251f',lineHeight:1.5}}>{item.text}</span>
          <button onClick={()=>dispatch(item)} title="Dispatch to a worker"
            style={{background:'none',border:'1px solid #e8d98a',borderRadius:5,padding:'3px 8px',fontSize:11,color:'#b08a2a',cursor:'pointer',fontFamily:'inherit',fontWeight:600,flexShrink:0}}>
            ↗ Dispatch
          </button>
          <button onClick={()=>remove(item.id)}
            style={{background:'none',border:'none',color:'#d0c8b8',fontSize:13,cursor:'pointer',padding:'0 2px',flexShrink:0}}>✕</button>
        </div>
      ))}

      {done.length > 0 && (
        <>
          <div style={{fontSize:10,color:'#c8c0af',fontWeight:700,textTransform:'uppercase',letterSpacing:0.5,marginTop:8}}>Done</div>
          {done.map(item => (
            <div key={item.id} style={{display:'flex',alignItems:'center',gap:9,padding:'8px 12px',borderRadius:8,border:'1px solid #f5f0e8',background:'#fdf8f0',opacity:0.7}}>
              <input type="checkbox" checked={true} onChange={()=>toggle(item.id)} style={{flexShrink:0,cursor:'pointer'}}/>
              <span style={{flex:1,fontSize:12.5,color:'#a59985',textDecoration:'line-through'}}>{item.text}</span>
              <button onClick={()=>remove(item.id)}
                style={{background:'none',border:'none',color:'#d0c8b8',fontSize:13,cursor:'pointer',padding:'0 2px',flexShrink:0}}>✕</button>
            </div>
          ))}
          <button onClick={()=>persist(open)}
            style={{background:'none',border:'1px solid #e8e0cc',borderRadius:6,padding:'5px 12px',fontSize:11,color:'#a59985',cursor:'pointer',fontFamily:'inherit',width:'fit-content'}}>
            Clear done
          </button>
        </>
      )}
    </div>
  );
}

// ── Main CleanSplit component ────────────────────────────────────────────────
function CleanSplit({selected=0, onSelect=()=>{}}) {
  const tick     = useTickerClock();
  const workers  = useWorkers();
  const domains  = useDomains();
  const [sel, setSel]         = React.useState(selected);
  const [rightTab, setRightTab] = React.useState('live');
  const [metaWorker, setMetaWorker] = React.useState(null);
  const [leftCollapsed, setLeftCollapsed] = React.useState(false);
  const [rightCollapsed, setRightCollapsed] = React.useState(false);
  const replyRef   = React.useRef(null);
  const paneRef    = React.useRef(null);
  const paneReady  = React.useRef(false);

  const worker      = workers[sel] || workers[0] || null;
  const paneHtml    = usePaneHtml(worker ? worker.session : null);
  const paneHtmlRef = React.useRef(null);
  const [dismissedCards, setDismissedCards] = React.useState(new Set());
  const [paneRawText, setPaneRawText] = React.useState('');
  const vaultCards  = useVaultCards(paneRawText);

  React.useEffect(() => {
    renderParsedHtml(paneHtmlRef.current, paneHtml);
    if (paneHtmlRef.current) setPaneRawText(paneHtmlRef.current.textContent || '');
  }, [paneHtml]);

  React.useEffect(() => {
    if (!paneRef.current || !paneHtml || paneReady.current) return;
    paneRef.current.scrollTop = paneRef.current.scrollHeight;
    paneReady.current = true;
  }, [paneHtml]);

  const prevSess = React.useRef(null);
  React.useEffect(() => {
    if (worker && worker.session !== prevSess.current) {
      paneReady.current = false;
      prevSess.current = worker.session;
      setDismissedCards(new Set());
      setPaneRawText('');
    }
  }, [worker]);

  const [slackSent, setSlackSent] = React.useState('');

  const handleSend = async (force=false) => {
    if (!worker || !replyRef.current) return;
    const msg = replyRef.current.value.trim();
    if (!msg) return;
    const r = await fetch('/dispatch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task:msg, session:worker.session, force})
    });
    if (r.ok) replyRef.current.value = '';
  };

  const handleSlackUpdate = async () => {
    if (!worker) return;
    setSlackSent('sending…');
    const res = await _sendSlackUpdate(worker, paneRawText);
    setSlackSent(res.ok ? '✓ sent' : ('⚠ ' + (res.error || 'failed')));
    setTimeout(() => setSlackSent(''), 3000);
  };

  const rTabBtn = (id, label) => (
    <button onClick={()=>setRightTab(id)} style={{
      background:'none', border:'none', borderBottom: rightTab===id?'2px solid #2a251f':'2px solid transparent',
      padding:'10px 18px', fontSize:12, fontWeight:rightTab===id?700:500,
      color:rightTab===id?'#2a251f':'#a59985', cursor:'pointer', fontFamily:'inherit', marginBottom:'-1px',
    }}>{label}</button>
  );

  const gridCols = leftCollapsed && rightCollapsed ? '36px 1fr 36px'
    : leftCollapsed ? '36px 1fr'
    : rightCollapsed ? '340px 1fr 36px'
    : '340px 1fr';

  return (
    <div style={{...sRoot, gridTemplateColumns: gridCols}}>
      <QuestionsOverlay/>
      {metaWorker && <MetaModal worker={metaWorker} onClose={()=>setMetaWorker(null)}/>}

      {/* ── left: task list ── */}
      <div style={{...sLeft, overflow: leftCollapsed ? 'visible' : 'hidden', width: leftCollapsed ? 36 : undefined}}>
        {leftCollapsed ? (
          <div style={{display:'flex',flexDirection:'column',alignItems:'center',padding:'16px 0',gap:12}}>
            <button onClick={()=>setLeftCollapsed(false)} title="Expand tasks"
              style={{background:'none',border:'none',color:'#a59985',fontSize:16,cursor:'pointer',padding:4,transform:'rotate(0deg)'}}>›</button>
            {workers.filter(w=>w.status==='busy'||w.status==='waiting').length > 0 && (
              <span style={{width:7,height:7,borderRadius:'50%',background:'#f5a623',animation:'nClean 1.4s ease-in-out infinite'}}/>
            )}
          </div>
        ) : (
        <div style={sLeftHead}>
          <div style={{flex:1}}>
            <div style={{fontSize:22,fontWeight:700,letterSpacing:-0.5,color:'#2a251f'}}>Tasks</div>
            <div style={{display:'flex',gap:8,marginTop:4,flexWrap:'wrap'}}>
              {[['busy','running'],['waiting','asking'],['blocked','blocked'],['idle','idle'],['missing','gone']].map(([st,label])=>{
                const n = workers.filter(w=>w.status===st).length;
                if (!n && (st==='blocked'||st==='missing')) return null;
                return (
                  <span key={st} style={{display:'flex',alignItems:'center',gap:3,fontSize:11,color:'#8a8072'}}>
                    <span style={{width:6,height:6,borderRadius:'50%',background:STATUS_DOT[st],flexShrink:0}}/>
                    {n} {label}
                  </span>
                );
              })}
            </div>
          </div>
          <button onClick={()=>setLeftCollapsed(true)} title="Collapse"
            style={{background:'none',border:'none',color:'#c8c0af',fontSize:16,cursor:'pointer',padding:'2px 4px'}}>‹</button>
          <a href={'/dashboard'+_TQ} style={{fontSize:10.5,color:'#a59985',textDecoration:'none',border:'1px solid #e8e4d8',borderRadius:5,padding:'3px 8px',whiteSpace:'nowrap',alignSelf:'flex-start'}}>⬡ Classic</a>
        </div>
        )}

        {!leftCollapsed && <AlertsBanner workers={workers}/>}

        {!leftCollapsed && <div style={{overflow:'auto',flex:1,padding:'4px 10px'}}>
          {workers.length===0 && (
            <div style={{padding:'24px 12px',color:'#c8c0af',fontSize:12,textAlign:'center',fontFamily:'"SF Mono",monospace'}}>loading…</div>
          )}
          {[...workers].sort((a,b) => {
            if (!a.age && !b.age) return 0;
            if (!a.age) return 1;
            if (!b.age) return -1;
            return a.age - b.age;
          }).map((w) => {
            const origIdx   = workers.indexOf(w);
            const isSel     = origIdx===sel;
            const isBusy    = w.status==='busy'||w.status==='waiting';
            const label     = w.displayName || (w.task && w.task!==w.session ? w.task : null);
            const timestamp = fmtDispatchTime(w.age);
            const hasAlert  = w.status==='blocked'||w.status==='missing'||w.auth==='auth_error';
            return (
              <div key={w.session} onClick={()=>{setSel(origIdx);onSelect(origIdx);setRightTab('live');}}
                   style={{...sTaskRow,background:isSel?'#fff8e0':hasAlert?'#fff8f0':'transparent'}}>
                <div style={{...sCircle,borderColor:isBusy?'#f5a623':hasAlert?'#e55a6a':'#e4dcc4',background:w.status==='idle'?'#fff':'transparent'}}>
                  {isBusy && <div style={{width:8,height:8,borderRadius:'50%',background:'#f5a623',animation:'nClean 1.4s ease-in-out infinite'}}/>}
                  {w.status==='idle'    && <div style={{width:10,height:2,background:'#c8c0af',borderRadius:1}}/>}
                  {w.status==='blocked' && <div style={{color:'#e55a6a',fontSize:10,fontWeight:700}}>!</div>}
                  {w.status==='missing' && <div style={{color:'#c8c0af',fontSize:9}}>–</div>}
                  {w.auth==='auth_error'&& <div style={{color:'#e55a6a',fontSize:9}}>🔒</div>}
                </div>
                <div style={{flex:1,minWidth:0}}>
                  {label ? (
                    <>
                      <div style={{fontSize:13,color:'#2a251f',fontWeight:500,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{label}</div>
                      <div style={{fontSize:11,color:'#a59985',marginTop:1,display:'flex',gap:5,alignItems:'center'}}>
                        <span style={{fontFamily:'"SF Mono",monospace'}}>{w.session}</span>
                        <span>·</span><span>{w.domain}</span>
                        {isBusy && <><span>·</span><span style={{fontVariantNumeric:'tabular-nums'}}>{fmtMMSS(w.age+tick)}</span></>}
                        {timestamp && !isBusy && <><span>·</span><span>{timestamp}</span></>}
                      </div>
                    </>
                  ) : (
                    <>
                      <div style={{fontSize:13,color:'#2a251f',fontWeight:600,fontFamily:'"SF Mono",monospace',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{w.session}</div>
                      <div style={{fontSize:11,color:'#a59985',marginTop:1,display:'flex',gap:5,alignItems:'center'}}>
                        <span>{w.domain}</span>
                        {isBusy && <><span>·</span><span style={{fontVariantNumeric:'tabular-nums'}}>{fmtMMSS(w.age+tick)}</span></>}
                        {timestamp && !isBusy && <><span>·</span><span>{timestamp}</span></>}
                      </div>
                    </>
                  )}
                </div>
                <button onClick={e=>{e.stopPropagation();setMetaWorker(w);}}
                  title="Rename" style={{background:'none',border:'none',color:'#d0c8b8',fontSize:12,cursor:'pointer',padding:'2px 4px',opacity:0,transition:'opacity 0.15s'}}
                  onMouseEnter={e=>e.currentTarget.style.opacity=1}
                  onMouseLeave={e=>e.currentTarget.style.opacity=0}>✎</button>
              </div>
            );
          })}
        </div>}

        {!leftCollapsed && <DispatchPanel domains={domains}/>}
      </div>

      {/* ── right: tabbed pane ── */}
      {rightCollapsed ? (
        <div style={{borderLeft:'1px solid #f0e8d0',display:'flex',flexDirection:'column',alignItems:'center',padding:'16px 0',gap:12,background:'#fffdf5',width:36}}>
          <button onClick={()=>setRightCollapsed(false)} title="Expand pane"
            style={{background:'none',border:'none',color:'#a59985',fontSize:16,cursor:'pointer',padding:4}}>‹</button>
          {['⬡','✓','⚙','📂','🗂','☐'].map((icon,i) => (
            <span key={i} style={{fontSize:13,color:'#c8c0af',writingMode:'vertical-rl',cursor:'pointer'}}
              onClick={()=>{setRightCollapsed(false);setRightTab(['live','results','manage','vault','docs','todo'][i]);}}>{icon}</span>
          ))}
        </div>
      ) : (
      <div style={sRight}>
        <div style={{borderBottom:'1px solid #f0e8d0',display:'flex',alignItems:'flex-end',padding:'0 8px',background:'#fff',flexShrink:0}}>
          {rTabBtn('live', '⬡ Live')}
          {rTabBtn('results', '✓ Results')}
          {rTabBtn('manage', '⚙ Manage')}
          {rTabBtn('vault', '📂 Vault')}
          {rTabBtn('docs', '🗂 Docs')}
          {rTabBtn('todo', '☐ Notes')}
          {worker && rightTab==='live' && (
            <div style={{marginLeft:'auto',padding:'0 16px 10px',display:'flex',gap:10,alignItems:'center',fontSize:11.5,color:'#8a8072'}}>
              <span style={{width:7,height:7,borderRadius:'50%',background:STATUS_DOT[worker.status]||'#ccc'}}/>
              <span style={{fontWeight:600,color:'#2a251f'}}>{worker.session}</span>
              <span>· {STATUS_LABEL[worker.status]||worker.status}</span>
              <span>· {fmtMMSS(worker.age+tick)}</span>
            </div>
          )}
          {rightTab !== 'vault' && (
            <button onClick={()=>setRightCollapsed(true)} title="Collapse pane"
              style={{marginLeft: worker&&rightTab==='live' ? 0 : 'auto',background:'none',border:'none',color:'#c8c0af',fontSize:16,cursor:'pointer',padding:'0 8px 10px'}}>›</button>
          )}
        </div>

        {rightTab==='live' && (
          !worker ? (
            <div style={{flex:1,display:'flex',alignItems:'center',justifyContent:'center',color:'#c8c0af',fontSize:13}}>
              Select a task on the left to view its live terminal
            </div>
          ) : (
            <>
              <div style={{padding:'14px 28px 10px',borderBottom:'1px solid #f8f0e0',flexShrink:0}}>
                <div style={{fontSize:16,fontWeight:600,color:'#2a251f',letterSpacing:-0.2,lineHeight:1.4}}>{worker.task||worker.session}</div>
                <div style={{fontSize:11.5,color:'#a59985',marginTop:4}}>
                  {worker.domain} · {worker.model}
                  {fmtDispatchTime(worker.age) && <> · dispatched {fmtDispatchTime(worker.age)}</>}
                </div>
              </div>
              <SessionNoteCard session={worker.session}/>
              <div ref={paneRef} style={sPane}>
                {!paneHtml && <div style={{color:'#c8c0af',fontFamily:'"SF Mono",monospace',fontSize:11.5}}>waiting for pane output…</div>}
                <div ref={paneHtmlRef} className="clean-pane"/>
                <div style={{display:'flex',alignItems:'center',gap:4,marginTop:10,fontFamily:'"SF Mono",monospace',fontSize:12.5}}>
                  <span style={{color:'#b5ad9d'}}>›</span>
                  <span style={{display:'inline-block',width:7,height:14,background:'#2a251f',animation:'nBlink 1s steps(2) infinite'}}/>
                </div>
                {vaultCards.filter(p=>!dismissedCards.has(p)).map(p=>(
                  <VaultCard key={p} vaultPath={p} onClose={()=>setDismissedCards(s=>new Set([...s,p]))}/>
                ))}
              </div>
              <div style={sReply}>
                <div style={{display:'flex',alignItems:'flex-end',gap:8,padding:'10px 14px',background:'#fff',border:'1px solid #ead9a3',borderRadius:10}}>
                  <textarea ref={replyRef} placeholder={`Send a message to ${worker.session}…`}
                    onKeyDown={e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey))handleSend(false);}}
                    style={{flex:1,border:'none',outline:'none',resize:'none',minHeight:38,fontSize:13,lineHeight:1.5,color:'#2a251f',fontFamily:'inherit',background:'transparent'}}/>
                  <button onClick={()=>handleSend(false)} style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'7px 14px',borderRadius:6,fontSize:12,fontWeight:700,cursor:'pointer',fontFamily:'inherit'}}>Send</button>
                  <button onClick={()=>handleSend(true)} title="Force — interrupts even if busy"
                    style={{background:'#7c3a3a',color:'#ffd5d5',border:'none',padding:'7px 11px',borderRadius:6,fontSize:11,fontWeight:700,cursor:'pointer',fontFamily:'inherit'}}>⚡ Force</button>
                  {worker.slackTarget && (
                    <button onClick={handleSlackUpdate} title={`Send update to ${worker.slackTarget}`}
                      style={{background:'#4a90e2',color:'#fff',border:'none',padding:'7px 11px',borderRadius:6,fontSize:11,fontWeight:700,cursor:'pointer',fontFamily:'inherit'}}>
                      📤
                    </button>
                  )}
                </div>
                <div style={{fontSize:10,color:'#c8c0af',marginTop:5,paddingLeft:2}}>
                  ⌘↵ send · <span style={{color:'#c8a0a0'}}>⚡ force</span> interrupts a busy worker
                  {slackSent && <span style={{marginLeft:8,color:slackSent.startsWith('✓')?'#2e7d32':'#b85c00'}}>{slackSent}</span>}
                </div>
              </div>
            </>
          )
        )}

        {rightTab==='results' && <ResultsPanel/>}
        {rightTab==='manage' && <ManagePanel workers={workers} domains={domains}/>}
        {rightTab==='vault' && <VaultPanel/>}
        {rightTab==='docs' && <WorkerDocsPanel worker={worker} paneRawText={paneRawText}/>}
        {rightTab==='todo' && <TodoPanel/>}
      </div>
      )}
    </div>
  );
}

const sEmptyState = {flex:1,display:'flex',alignItems:'center',justifyContent:'center',color:'#c8c0af',fontSize:13};
const sRoot      = {position:'absolute',inset:0,background:'#fffdf5',fontFamily:'-apple-system,BlinkMacSystemFont,"Inter",sans-serif',display:'grid',overflow:'hidden'};
const sLeft      = {borderRight:'1px solid #f0e8d0',display:'flex',flexDirection:'column',background:'#fdf8e9'};
const sLeftHead  = {padding:'22px 22px 14px',borderBottom:'1px solid #f0e8d0',display:'flex',alignItems:'flex-start',gap:10,flexShrink:0};
const sTaskRow   = {display:'flex',gap:10,padding:'10px 12px',borderRadius:8,cursor:'pointer',alignItems:'flex-start',marginBottom:1};
const sCircle    = {width:18,height:18,border:'1.5px solid',borderRadius:'50%',display:'flex',alignItems:'center',justifyContent:'center',marginTop:2,flexShrink:0};
const sRight     = {display:'flex',flexDirection:'column',overflow:'hidden',background:'#fffdf5'};
const sPane      = {flex:1,overflow:'auto',padding:'20px 28px'};
const sReply     = {padding:'14px 20px',borderTop:'1px solid #f0e8d0',flexShrink:0};

window.CleanSplit = CleanSplit;
