(function(){
  const DISMISSED_KEY='hermes-pet-dismissed';
  const COLLAPSED_KEY='hermes-pet-collapsed';
  const SKIN_KEY='hermes-pet-skin';
  const SKIN_MIGRATION_KEY='hermes-pet-skin-migration';
  const DEFAULT_SKIN_ID='keeper';
  const DEFAULT_SKIN_MIGRATION='keeper-default-v1';
  const RESTART_POSITION_KEY='hermes-pet-restart-position';
  const POLL_MS=2500;
  const FRAME_MS=520;
  const PET_WINDOW_FIXED={width:128,height:139};
  const PET={cols:8,rows:9,states:['idle','running-right','running-left','waving','jumping','failed','waiting','running','review'],frameCounts:{idle:6,'running-right':8,'running-left':8,waving:4,jumping:5,failed:8,waiting:6,running:6,review:6}};
  const shell=document.getElementById('petShell');
  const badge=document.getElementById('petBadge');
  const stage=document.getElementById('petStage');
  const sprite=document.getElementById('petSprite');
  let state='idle', frame=0, sessions=[], dismissed=_readJson(DISMISSED_KEY,{}), seeded=false;
  let petSkins=[{id:'keeper',displayName:'May',spritesheetUrl:'/static/pets/keeper/spritesheet.webp'}];
  let activeSkinId=_initialPetSkinId();

  function _petT(key,...args){return typeof t==='function'?t(key,...args):key;}
  function _readJson(key,fallback){try{const parsed=JSON.parse(localStorage.getItem(key)||'null');return parsed&&typeof parsed==='object'?parsed:fallback;}catch(_){return fallback;}}
  function _writeJson(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch(_){}}
  function _initialPetSkinId(){
    const stored=localStorage.getItem(SKIN_KEY)||'';
    if(localStorage.getItem(SKIN_MIGRATION_KEY)!==DEFAULT_SKIN_MIGRATION&&(!stored||stored==='shiba')){
      localStorage.setItem(SKIN_KEY,DEFAULT_SKIN_ID);
      localStorage.setItem(SKIN_MIGRATION_KEY,DEFAULT_SKIN_MIGRATION);
      return DEFAULT_SKIN_ID;
    }
    return stored||DEFAULT_SKIN_ID;
  }
  function _clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
  function _menuLabels(){return {switchSkin:_petT('desktop_pet_switch_skin'),restartPet:_petT('desktop_pet_restart'),closePet:_petT('desktop_pet_close')};}
  function _localizeStaticLabels(){
    if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
    document.title=_petT('desktop_pet_title');
    badge.setAttribute('aria-label',_petT('desktop_pet_expand_updates'));
  }
  function _safeSkin(skin){
    if(!skin||typeof skin!=='object') return null;
    const id=String(skin.id||'').trim();
    const displayName=String(skin.displayName||id).trim()||id;
    const spritesheetUrl=String(skin.spritesheetUrl||'').trim();
    if(!/^[A-Za-z0-9_-]+$/.test(id)||!spritesheetUrl) return null;
    return {id,displayName,spritesheetUrl};
  }
  function _activeSkin(){return petSkins.find(skin=>skin.id===activeSkinId)||petSkins[0];}
  function _applyPetSkin(skinId,persist){
    const next=petSkins.find(skin=>skin.id===skinId)||petSkins[0];
    if(!next) return;
    activeSkinId=next.id;
    if(persist) localStorage.setItem(SKIN_KEY,next.id);
    sprite.style.backgroundImage=`url("${next.spritesheetUrl}")`;
    stage.setAttribute('aria-label',next.displayName);
    shell.setAttribute('aria-label',_petT('desktop_pet_shell_label',next.displayName));
  }
  async function _loadPetSkins(){
    try{
      const data=await fetch('/api/pet/skins',{cache:'no-store'}).then(res=>{if(!res.ok) throw new Error(`Pet skins failed: ${res.status}`);return res.json();});
      const skins=(Array.isArray(data.skins)?data.skins:[]).map(_safeSkin).filter(Boolean);
      if(skins.length) petSkins=skins;
      _applyPetSkin(activeSkinId,false);
      return true;
    }catch(err){console.warn('Failed to load pet skins',err);_applyPetSkin(activeSkinId,false);return false;}
  }
  async function _listenPetSkinChanges(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.listen!=='function') return;
    try{await tauri.event.listen('pet-skin-change',event=>_applyPetSkin(String(event.payload||''),true));}catch(err){console.warn('Failed to listen for pet skin changes',err);}
  }
  async function _restartPetInPlace(){
    try{await _savePetRestartPosition();}catch(err){console.warn('Failed to save pet restart position',err);}
    location.reload();
  }
  async function _listenPetRestartRequests(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.listen!=='function') return;
    try{await tauri.event.listen('pet-restart-requested',()=>_restartPetInPlace());}catch(err){console.warn('Failed to listen for pet restart requests',err);}
  }
  function _frameCount(){const count=PET.frameCounts[state]||PET.cols;return Math.min(PET.cols,count);}
  function _applyFrame(){const row=Math.max(0,PET.states.indexOf(state));const col=frame%_frameCount();sprite.style.backgroundPosition=`${(col/(PET.cols-1))*100}% ${(row/(PET.rows-1))*100}%`;}
  function _setState(next){if(state!==next){state=PET.states.includes(next)?next:'idle';frame=0;}_applyFrame();}
  function _tick(){frame=(frame+1)%_frameCount();_applyFrame();}
  function _seedViewedCounts(rows){
    if(seeded) return;
    const viewed=_readJson('hermes-pet-viewed-counts',{});
    for(const row of rows){if(!row.running&&!Object.prototype.hasOwnProperty.call(viewed,row.session_id)) viewed[row.session_id]=Number(row.message_count||0);}
    _writeJson('hermes-pet-viewed-counts',viewed);
    seeded=true;
  }
  function _attentionItems(){
    const viewed=_readJson('hermes-pet-viewed-counts',{});
    dismissed=_readJson(DISMISSED_KEY,{});
    return sessions.filter(row=>{
      const sid=row.session_id;
      const actionRequired=!!row.action_required;
      const ready=!actionRequired&&!row.running&&Number(row.message_count||0)>Number(viewed[sid]||0);
      const status=actionRequired?'action_required':(row.running?'running':(ready?'ready':'idle'));
      if(status==='idle') return false;
      return dismissed[sid]!==status;
    }).map(row=>({...row,status:row.action_required?'action_required':(row.running?'running':'ready'),text:_clean(row.process_text)})).sort((a,b)=>{
      const priority={action_required:3,ready:2,running:1};
      if(a.status!==b.status) return (priority[b.status]||0)-(priority[a.status]||0);
      return Number(b.last_message_at||0)-Number(a.last_message_at||0);
    });
  }
  function render(){
    const items=_attentionItems();
    const count=items.length;
    const collapsed=localStorage.getItem(COLLAPSED_KEY)==='true';
    badge.hidden=!count||!collapsed;
    badge.textContent=String(count);
    _setState(items.some(item=>item.status==='action_required')?'waiting':(items.some(item=>item.status==='running')?'running':'idle'));
    _emitPetAttentionUpdate(count,collapsed);
  }
  async function refresh(){
    try{
      const data=await fetch('/api/pet/attention',{cache:'no-store'}).then(res=>{if(!res.ok) throw new Error(`Pet attention failed: ${res.status}`);return res.json();});
      sessions=Array.isArray(data.sessions)?data.sessions:[];
      _seedViewedCounts(sessions);
      render();
      return true;
    }catch(_){return false;}
  }
  function _currentTauriWindow(){
    const tauri=window.__TAURI__;
    if(!tauri) return null;
    if(tauri.webviewWindow&&typeof tauri.webviewWindow.getCurrentWebviewWindow==='function') return tauri.webviewWindow.getCurrentWebviewWindow();
    if(tauri.window&&typeof tauri.window.getCurrent==='function') return tauri.window.getCurrent();
    return null;
  }
  function _tauriDpiCtor(name){
    const tauri=window.__TAURI__;
    return (tauri&&tauri.dpi&&tauri.dpi[name])||(tauri&&tauri.window&&tauri.window[name])||null;
  }
  function _physicalPosition(x,y){const Ctor=_tauriDpiCtor('PhysicalPosition');return Ctor?new Ctor(Math.round(x),Math.round(y)):null;}
  function _monitorBounds(monitor){
    const pos=monitor&&monitor.position||{};
    const size=monitor&&monitor.size||{};
    return {x:Number(pos.x||0),y:Number(pos.y||0),width:Number(size.width||0),height:Number(size.height||0),scale:Number(monitor&&monitor.scaleFactor||1)||1};
  }
  async function _windowGeometry(win){
    if(!win||typeof win.outerPosition!=='function'||typeof win.outerSize!=='function') return null;
    const pos=await win.outerPosition();
    const size=await win.outerSize();
    return {x:Number(pos&&pos.x||0),y:Number(pos&&pos.y||0),width:Number(size&&size.width||0),height:Number(size&&size.height||0)};
  }
  function _safeRestartPosition(value){
    if(!value||typeof value!=='object') return null;
    const x=Number(value.x), y=Number(value.y);
    if(!Number.isFinite(x)||!Number.isFinite(y)) return null;
    const ts=Number(value.ts||0);
    if(ts&&Date.now()-ts>5*60*1000) return null;
    return {x,y};
  }
  async function _savePetRestartPosition(){
    const win=_currentTauriWindow();
    const geo=await _windowGeometry(win);
    if(!geo) return false;
    try{localStorage.setItem(RESTART_POSITION_KEY,JSON.stringify({x:geo.x,y:geo.y,ts:Date.now()}));return true;}catch(_){return false;}
  }
  async function _restorePetRestartPosition(){
    let saved=null;
    try{saved=_safeRestartPosition(JSON.parse(localStorage.getItem(RESTART_POSITION_KEY)||'null'));}catch(_){}
    try{localStorage.removeItem(RESTART_POSITION_KEY);}catch(_){}
    if(!saved) return false;
    const win=_currentTauriWindow();
    if(!win||typeof win.setPosition!=='function') return false;
    const pos=_physicalPosition(saved.x,saved.y);
    if(!pos) return false;
    try{await win.setPosition(pos);return true;}catch(err){console.warn('Failed to restore pet restart position',err);return false;}
  }
  async function _monitorForWindow(win,geo){
    let monitor=null;
    try{if(win&&typeof win.currentMonitor==='function') monitor=await win.currentMonitor();}catch(_){}
    if(monitor) return monitor;
    try{
      const monitors=win&&typeof win.availableMonitors==='function'?await win.availableMonitors():[];
      if(Array.isArray(monitors)&&monitors.length&&geo){
        const cx=geo.x+geo.width/2, cy=geo.y+geo.height/2;
        return monitors.map(item=>{const b=_monitorBounds(item);return {item,dist:Math.hypot(cx-(b.x+b.width/2),cy-(b.y+b.height/2))};}).sort((a,b)=>a.dist-b.dist)[0].item;
      }
    }catch(_){}
    return null;
  }
  async function _clampPetWindowToMonitor(win,geo,monitor){
    if(!win||!geo||!monitor||typeof win.setPosition!=='function') return geo;
    const b=_monitorBounds(monitor);
    if(!b.width||!b.height) return geo;
    const margin=8*b.scale;
    const maxX=Math.max(b.x+margin,b.x+b.width-geo.width-margin);
    const maxY=Math.max(b.y+margin,b.y+b.height-geo.height-margin);
    const nextX=Math.min(maxX,Math.max(b.x+margin,geo.x));
    const nextY=Math.min(maxY,Math.max(b.y+margin,geo.y));
    if(Math.abs(nextX-geo.x)<1&&Math.abs(nextY-geo.y)<1) return geo;
    const pos=_physicalPosition(nextX,nextY);
    if(!pos) return geo;
    try{await win.setPosition(pos);return {...geo,x:nextX,y:nextY};}catch(err){console.warn('Failed to clamp pet window',err);return geo;}
  }
  async function _emitPetLayout(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.emit!=='function') return;
    const win=_currentTauriWindow();
    const geo=await _windowGeometry(win);
    const monitor=await _monitorForWindow(win,geo);
    const bounds=_monitorBounds(monitor);
    const clamped=await _clampPetWindowToMonitor(win,geo,monitor);
    const nextGeo=clamped||geo;
    if(!nextGeo) return;
    const centerX=nextGeo.x+nextGeo.width/2;
    const centerY=nextGeo.y+nextGeo.height/2;
    await tauri.event.emit('pet-layout-update',{pet:nextGeo,monitor:bounds,align:centerX<bounds.x+bounds.width/2?'left':'right',placement:centerY<bounds.y+bounds.height/2?'below':'above'});
  }
  function _emitPetAttentionUpdate(count,collapsed){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.emit!=='function') return;
    tauri.event.emit('pet-attention-update',{count,collapsed}).catch(()=>{});
  }
  async function _startTauriWindowDrag(event){
    if(!event||event.button!==0) return;
    const win=_currentTauriWindow();
    if(!win||typeof win.startDragging!=='function') return;
    event.preventDefault();
    try{await win.startDragging();}catch(_){}
    setTimeout(()=>_emitPetLayout(),0);
  }
  async function _openPetContextMenu(event){
    event.preventDefault();
    event.stopPropagation();
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.emit!=='function') return;
    try{
      await _loadPetSkins();
      await tauri.event.emit('pet-context-menu',{skins:petSkins,activeSkinId:(_activeSkin()||{}).id||'keeper',menuLabels:_menuLabels()});
    }catch(err){console.warn('Failed to open pet context menu',err);}
  }
  badge.addEventListener('click',()=>{localStorage.setItem(COLLAPSED_KEY,'false');render();_emitPetLayout();});
  document.addEventListener('contextmenu',_openPetContextMenu);
  shell.addEventListener('pointerdown',_startTauriWindowDrag);
  stage.addEventListener('click',()=>_setState('waving'));
  window.addEventListener('storage',event=>{if(event.key===COLLAPSED_KEY||event.key===DISMISSED_KEY) render();});
  setInterval(_tick,FRAME_MS);
  setInterval(refresh,POLL_MS);
  setInterval(_emitPetLayout,1000);
  async function _bootPet(){
    _localizeStaticLabels();
    await _restorePetRestartPosition();
    await _loadPetSkins();
    await refresh();
    _listenPetSkinChanges();
    _listenPetRestartRequests();
    _emitPetLayout();
  }
  _bootPet();
})();
