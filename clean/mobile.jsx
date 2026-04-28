// Mobile layout for V3 Split — iPhone-size (375x812)
// Single column: task list → tap to drill in → detail + pane.
// Bottom add-task bar for dispatch.

function CleanMobileSplit() {
  const tick    = useTickerClock();
  const workers = useWorkers();
  const [sel, setSel] = React.useState(null);
  const [detailTab, setDetailTab] = React.useState('live'); // 'live' | 'vault'
  const replyRef = React.useRef(null);
  const paneRef  = React.useRef(null);
  const paneReadyRef = React.useRef(false);

  const worker    = sel !== null ? (workers[sel] || null) : null;
  const paneHtml  = usePaneHtml(worker ? worker.session : null);
  const paneHtmlRef = React.useRef(null);

  React.useEffect(() => { renderParsedHtml(paneHtmlRef.current, paneHtml); }, [paneHtml]);

  React.useEffect(() => {
    if (!paneRef.current || !paneHtml || paneReadyRef.current) return;
    paneRef.current.scrollTop = paneRef.current.scrollHeight;
    paneReadyRef.current = true;
  }, [paneHtml]);

  React.useEffect(() => { paneReadyRef.current = false; }, [worker]);

  const handleSend = async (force=false) => {
    if (!worker || !replyRef.current) return;
    const msg = replyRef.current.value.trim();
    if (!msg) return;
    try {
      const r = await fetch('/dispatch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({task: msg, session: worker.session, force}),
      });
      if (r.ok) replyRef.current.value = '';
    } catch(e) {}
  };

  return (
    <div style={mRoot('#fffdf5')}>
      {!worker ? (
        /* Task list view */
        <>
          <div style={{padding:'22px 18px 14px'}}>
            <div style={{fontSize:24,fontWeight:700,letterSpacing:-0.5,color:'#2a251f'}}>Tasks</div>
            <div style={{display:'flex',gap:10,marginTop:5,flexWrap:'wrap'}}>
              {[['busy','running'],['waiting','asking'],['blocked','blocked'],['idle','idle'],['missing','gone']].map(([st,label])=>{
                const n = workers.filter(w=>w.status===st).length;
                if (!n && (st==='blocked'||st==='missing')) return null;
                return (
                  <span key={st} style={{display:'flex',alignItems:'center',gap:4,fontSize:11.5,color:'#8a8072'}}>
                    <span style={{width:7,height:7,borderRadius:'50%',background:STATUS_DOT[st],flexShrink:0,display:'inline-block'}}/>
                    {n} {label}
                  </span>
                );
              })}
            </div>
          </div>

          <div style={{flex:1,overflow:'auto',padding:'0 10px'}}>
            {workers.length === 0 && (
              <div style={{padding:'20px 12px',color:'#c8c0af',fontSize:12,textAlign:'center'}}>loading…</div>
            )}
            {[...workers].sort((a,b) => {
              if (!a.age && !b.age) return 0;
              if (!a.age) return 1;
              if (!b.age) return -1;
              return a.age - b.age;
            }).map((w) => {
              const i = workers.indexOf(w);
              const isBusy = w.status==='busy' || w.status==='waiting';
              return (
                <div key={w.session} onClick={()=>setSel(i)} style={{display:'flex',gap:10,padding:'11px 10px',borderRadius:8,alignItems:'flex-start',cursor:'pointer'}}>
                  <div style={{width:18,height:18,border:'1.5px solid',borderColor:isBusy?'#f5a623':'#e4dcc4',borderRadius:'50%',display:'flex',alignItems:'center',justifyContent:'center',marginTop:2,flexShrink:0}}>
                    {isBusy && <div style={{width:8,height:8,borderRadius:'50%',background:'#f5a623',animation:'nClean 1.4s ease-in-out infinite'}}/>}
                    {w.status==='idle' && <div style={{width:9,height:2,background:'#c8c0af',borderRadius:1}}/>}
                    {w.status==='blocked' && <div style={{color:'#e55a6a',fontSize:10,fontWeight:700}}>!</div>}
                    {w.status==='missing' && <div style={{color:'#c8c0af',fontSize:9}}>–</div>}
                  </div>
                  <div style={{flex:1,minWidth:0}}>
                    {w.task && w.task !== w.session ? (
                      <>
                        <div style={{fontSize:13,color:'#2a251f',fontWeight:500,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{w.task}</div>
                        <div style={{fontSize:11,color:'#a59985',marginTop:1}}>
                          <span style={{fontFamily:'"SF Mono",monospace'}}>{w.session}</span> · {w.domain}{isBusy ? ` · ${fmtMMSS(w.age+tick)}` : ''}
                        </div>
                      </>
                    ) : (
                      <>
                        <div style={{fontSize:13,color:'#2a251f',fontWeight:600,fontFamily:'"SF Mono",monospace',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{w.session}</div>
                        <div style={{fontSize:11,color:'#a59985',marginTop:1}}>
                          {w.domain}{isBusy ? ` · ${fmtMMSS(w.age+tick)}` : ''}
                        </div>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          <MobileDispatch workers={workers}/>
        </>
      ) : (
        /* Detail view */
        <>
          {/* header: back + status + title */}
          <div style={{padding:'14px 18px 10px',borderBottom:'none',flexShrink:0,background:'#fff'}}>
            <div onClick={()=>{setSel(null);setDetailTab('live');}}
              style={{fontSize:12.5,color:'#b08a2a',marginBottom:8,cursor:'pointer',display:'inline-flex',alignItems:'center',gap:4,fontWeight:600}}>
              ← Tasks
            </div>
            <div style={{display:'flex',alignItems:'center',gap:7,marginBottom:4}}>
              <span style={{width:7,height:7,borderRadius:'50%',background:STATUS_DOT[worker.status]||'#c8c4ba'}}/>
              <span style={{fontSize:10.5,letterSpacing:0.5,color:'#8a8072',fontWeight:600,textTransform:'uppercase'}}>{STATUS_LABEL[worker.status]||worker.status}</span>
              <span style={{fontSize:11,color:'#a59985'}}>· {worker.session}</span>
            </div>
            <div style={{fontSize:15,fontWeight:600,color:'#2a251f',lineHeight:1.35}}>{worker.task||worker.session}</div>
          </div>

          {/* tab bar — clean, no negative margins */}
          <div style={{display:'flex',borderBottom:'2px solid #f0e8d0',background:'#fff',flexShrink:0}}>
            {[['live','⬡ Live'],['vault','📂 Vault']].map(([id,label])=>(
              <button key={id} onClick={()=>setDetailTab(id)}
                style={{background:'none',border:'none',
                  borderBottom:detailTab===id?'2px solid #2a251f':'2px solid transparent',
                  marginBottom:'-2px',
                  padding:'8px 18px 10px',fontSize:12,fontWeight:detailTab===id?700:500,
                  color:detailTab===id?'#2a251f':'#a59985',cursor:'pointer',fontFamily:'inherit'}}>
                {label}
              </button>
            ))}
          </div>

          {detailTab==='live' && (
            <>
              <div ref={paneRef} style={{flex:1,overflow:'auto',padding:'14px 18px',background:'#fffdf5',minWidth:0}}>
                {!paneHtml && <div style={{color:'#c8c0af',fontFamily:'"SF Mono",monospace',fontSize:11}}>waiting for pane…</div>}
                <div ref={paneHtmlRef} className="clean-pane" style={{maxWidth:'100%',overflowX:'auto'}}/>
              </div>
              <div style={{padding:'10px 14px',borderTop:'1px solid #f0e8d0',flexShrink:0}}>
                <div style={{display:'flex',alignItems:'flex-end',gap:6,padding:'8px 10px',background:'#fff',border:'1px solid #ead9a3',borderRadius:9}}>
                  <textarea ref={replyRef} placeholder="Send a message…" rows={1}
                    onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();handleSend(false);}}}
                    style={{flex:1,border:'none',outline:'none',background:'transparent',fontSize:12.5,fontFamily:'inherit',resize:'none',lineHeight:1.5,maxHeight:80,overflow:'auto'}}/>
                  <button onClick={()=>handleSend(false)}
                    style={{background:'#2a251f',color:'#f5d76b',border:'none',padding:'6px 12px',borderRadius:6,fontSize:11.5,fontWeight:700,fontFamily:'inherit',cursor:'pointer',flexShrink:0}}>Send</button>
                  <button onClick={()=>handleSend(true)} title="Force — interrupts busy worker"
                    style={{background:'#7c3a3a',color:'#ffd5d5',border:'none',padding:'6px 9px',borderRadius:6,fontSize:11,fontWeight:700,fontFamily:'inherit',cursor:'pointer',flexShrink:0}}>⚡</button>
                </div>
              </div>
            </>
          )}

          {detailTab==='vault' && (
            <div style={{flex:1,overflow:'hidden',display:'flex',flexDirection:'column'}}>
              <MobileVaultBrowser/>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function MobileDispatch({workers}) {
  const liveSkills = useSkills();
  const [taskText, setTaskText] = React.useState('');
  const [dispDomain, setDispDomain] = React.useState('');
  const [dispSending, setDispSending] = React.useState(false);
  const [dispMsg, setDispMsg] = React.useState('');
  const [open, setOpen] = React.useState(false);
  const [history, setHistory] = React.useState([]);
  const [histOpen, setHistOpen] = React.useState(false);

  const _DKW2 = {
    cx:['cx','zendesk','ticket','support','customer'],
    finance:['finance','revenue','stripe','mis','billing'],
    data:['data','sql','dbt','snowflake','cohort'],
    research:['research','competitor','pricing','market'],
    code:['code','pr','deploy','bug','fix','test'],
    legal:['legal','contract','msa','compliance'],
  };

  const detect = (txt) => {
    if (!txt) return '';
    const lo = txt.toLowerCase();
    const scores = {};
    for (const [d, kws] of Object.entries(_DKW2))
      scores[d] = kws.reduce((n,k) => n+(lo.includes(k)?1:0), 0);
    const best = Object.entries(scores).sort((a,b)=>b[1]-a[1])[0];
    return (best && best[1] > 0) ? best[0] : '';
  };

  const handleDispatch = async () => {
    const task = taskText.trim();
    if (!task || dispSending) return;
    setDispSending(true);
    try {
      const body = {task};
      if (dispDomain) body.domain = dispDomain;
      const r = await fetch('/dispatch', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok) {
        setDispMsg('✓ dispatched to ' + (data.session || dispDomain || 'worker'));
        setTaskText(''); setDispDomain(''); setOpen(false);
      } else {
        setDispMsg('⚠ ' + (data.detail||'failed'));
      }
    } catch(e) { setDispMsg('⚠ network error'); }
    setDispSending(false);
  };

  if (!open) {
    return (
      <div style={{padding:'12px 16px',borderTop:'1px solid #f0e8d0',background:'rgba(255,248,220,0.6)',flexShrink:0}}>
        <div onClick={()=>setOpen(true)} style={{display:'flex',alignItems:'center',gap:10,padding:'10px 12px',background:'#fff',border:'1px solid #ead9a3',borderRadius:9,cursor:'pointer'}}>
          <div style={{width:16,height:16,borderRadius:'50%',border:'1.5px dashed #c8b878',display:'flex',alignItems:'center',justifyContent:'center',color:'#b08a2a',fontSize:12,lineHeight:1}}>+</div>
          <span style={{flex:1,fontSize:12.5,color:'#a59985'}}>Dispatch a new task…</span>
        </div>
        {dispMsg && <div style={{fontSize:11,color:dispMsg.startsWith('✓')?'#2e7d32':'#b85c00',marginTop:5,paddingLeft:4}}>{dispMsg}</div>}
      </div>
    );
  }

  return (
    <div style={{padding:'14px 16px',borderTop:'1px solid #f0e8d0',background:'rgba(255,248,220,0.9)',flexShrink:0}}>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
        <div style={{display:'flex',alignItems:'center',gap:8}}>
          <div style={{fontSize:14,fontWeight:600,color:'#2a251f'}}>New task</div>
          <button onClick={async()=>{const next=!histOpen;setHistOpen(next);if(next&&!history.length){const h=await _get('/dispatch-history'+_TQ);if(h&&Array.isArray(h))setHistory(h);}}}
            style={{background:'none',border:'1px solid #ead9a3',borderRadius:4,color:'#b08a2a',fontSize:10.5,cursor:'pointer',padding:'1px 7px',fontFamily:'inherit'}}>
            ↑ History
          </button>
        </div>
        <div onClick={()=>{setOpen(false);setHistOpen(false);}} style={{fontSize:12,color:'#a59985',cursor:'pointer'}}>✕</div>
      </div>
      {histOpen && (
        <div style={{marginBottom:8,border:'1px solid #ead9a3',borderRadius:6,overflow:'auto',maxHeight:140,background:'#fff'}}>
          {history.length===0
            ? <div style={{padding:'8px 12px',fontSize:11,color:'#a59985'}}>No history yet</div>
            : history.map((h,i)=>(
              <div key={i} onClick={()=>{setTaskText(h.task);if(h.domain)setDispDomain(h.domain);setHistOpen(false);}}
                style={{padding:'6px 10px',cursor:'pointer',borderBottom:i<history.length-1?'1px solid #f5f0e8':'none',fontSize:11.5}}>
                <div style={{color:'#2a251f',overflow:'hidden',whiteSpace:'nowrap',textOverflow:'ellipsis'}}>{h.task.substring(0,80)}{h.task.length>80?'…':''}</div>
                <div style={{fontSize:10,color:'#a59985',marginTop:1}}>{h.domain} · {h.at}</div>
              </div>
            ))
          }
        </div>
      )}
      <textarea
        autoFocus
        placeholder="Describe the task. Type / for skills."
        value={taskText}
        onChange={e=>{setTaskText(e.target.value);setDispDomain(detect(e.target.value));setDispMsg('');}}
        style={{width:'100%',minHeight:80,border:'1px solid #ead9a3',borderRadius:7,padding:'10px 12px',fontSize:13,lineHeight:1.55,color:'#2a251f',fontFamily:'inherit',resize:'none',outline:'none',background:'#fff',boxSizing:'border-box'}}
      />
      {dispDomain && !dispMsg && (
        <div style={{fontSize:11,color:'#b08a2a',marginTop:5}}>routing to: <b>{dispDomain}</b></div>
      )}
      {dispMsg && (
        <div style={{fontSize:11,color:dispMsg.startsWith('✓')?'#2e7d32':'#b85c00',marginTop:5}}>{dispMsg}</div>
      )}
      <div style={{display:'flex',gap:5,marginTop:8,flexWrap:'wrap'}}>
        {liveSkills.slice(0,4).map(s=>(
          <span key={s.n} onClick={()=>setTaskText('/'+s.n+' ')} style={{fontSize:10.5,padding:'3px 8px',borderRadius:4,background:'#fff',color:'#b08a2a',border:'1px solid #ead9a3',fontFamily:'"SF Mono",monospace',cursor:'pointer'}}>/{s.n}</span>
        ))}
      </div>
      <button onClick={handleDispatch} disabled={!taskText.trim()||dispSending}
        style={{width:'100%',marginTop:12,background:'#2a251f',color:'#f5d76b',border:'none',padding:'11px',borderRadius:8,fontSize:13,fontWeight:700,fontFamily:'inherit',cursor:!taskText.trim()||dispSending?'default':'pointer',opacity:!taskText.trim()||dispSending?0.5:1}}>
        {dispSending ? 'Sending…' : 'Send →'}
      </button>
    </div>
  );
}

function MobileVaultBrowser() {
  const [path, setPath]   = React.useState('');
  const [entries, setEntries] = React.useState(null);
  const [file, setFile]   = React.useState(null);
  const [fileHtml, setFileHtml] = React.useState('');
  const fileRef = React.useRef(null);

  const loadDir = async (p) => {
    setEntries(null); setFile(null);
    const d = await _get('/vault/ls' + _TQA + 'path=' + encodeURIComponent(p));
    if (d) { setEntries(d); setPath(p); }
  };
  const loadFile = async (p) => {
    const d = await _get('/vault/read' + _TQA + 'path=' + encodeURIComponent(p));
    if (d) { setFile(d); setFileHtml(mdParse(cleanTermOutput(d.content))); }
  };
  React.useEffect(() => { loadDir(''); }, []);
  React.useEffect(() => { renderParsedHtml(fileRef.current, fileHtml); }, [fileHtml]);

  const VAULT_NAME = 'obsidian-vault';
  const crumbs = path ? path.split('/').filter(Boolean) : [];

  return (
    <div style={{flex:1,display:'flex',flexDirection:'column',overflow:'hidden'}}>
      {/* breadcrumb */}
      <div style={{padding:'10px 18px',borderBottom:'1px solid #f0e8d0',display:'flex',alignItems:'center',gap:4,flexShrink:0,flexWrap:'wrap'}}>
        <span onClick={()=>loadDir('')} style={{fontSize:11,color:'#b08a2a',cursor:'pointer',fontWeight:600}}>Vault</span>
        {crumbs.map((c,i) => (
          <React.Fragment key={i}>
            <span style={{fontSize:11,color:'#c8c0af'}}>/</span>
            <span onClick={()=>loadDir(crumbs.slice(0,i+1).join('/'))}
              style={{fontSize:11,color:'#b08a2a',cursor:'pointer',fontWeight:600}}>{c}</span>
          </React.Fragment>
        ))}
        {file && <>
          <span style={{fontSize:11,color:'#c8c0af'}}>/</span>
          <span style={{fontSize:11,color:'#2a251f',fontWeight:700}}>{file.name.replace(/\.md$/,'')}</span>
        </>}
      </div>

      {file ? (
        <>
          <div style={{padding:'8px 18px',borderBottom:'1px solid #f0e8d0',display:'flex',gap:8,flexShrink:0}}>
            <button onClick={()=>setFile(null)} style={{background:'none',border:'1px solid #e8e4d8',borderRadius:5,padding:'4px 10px',fontSize:11,cursor:'pointer',color:'#8a8072',fontFamily:'inherit'}}>← Files</button>
            <a href={`obsidian://open?vault=${encodeURIComponent(VAULT_NAME)}&file=${encodeURIComponent(file.path.replace(/\.md$/,''))}`}
              style={{fontSize:11,padding:'4px 10px',borderRadius:5,border:'1px solid #ead9a3',color:'#b08a2a',textDecoration:'none',background:'#fff8e0',fontWeight:600}}>✎ Open in Obsidian</a>
          </div>
          <div style={{flex:1,overflow:'auto',padding:'14px 18px'}}>
            <div ref={fileRef} className="clean-pane"/>
          </div>
        </>
      ) : (
        <div style={{flex:1,overflow:'auto',padding:'8px 10px'}}>
          {!entries && <div style={{padding:'16px',color:'#c8c0af',fontSize:12,textAlign:'center'}}>loading…</div>}
          {entries && entries.map(e => (
            <div key={e.path} onClick={()=> e.is_dir ? loadDir(e.path) : loadFile(e.path)}
              style={{display:'flex',alignItems:'center',gap:8,padding:'10px 12px',borderRadius:7,cursor:'pointer',marginBottom:2,background:'#fff',border:'1px solid #f5efe0'}}>
              <span style={{fontSize:16}}>{e.is_dir ? '📁' : '📄'}</span>
              <span style={{fontSize:13,color:'#2a251f',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{e.name.replace(/\.md$/,'')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const mRoot = bg => ({position:'absolute',inset:0,background:bg,display:'flex',flexDirection:'column',overflow:'hidden',fontFamily:'-apple-system,BlinkMacSystemFont,"Inter",sans-serif',color:'#2a251f'});

Object.assign(window, {CleanMobileSplit});
