(function(){
  const VIEWED_KEY='hermes-pet-viewed-counts';
  const DISMISSED_KEY='hermes-pet-dismissed';
  const COLLAPSED_KEY='hermes-pet-collapsed';
  const SKIN_KEY='hermes-pet-skin';
  const SKIN_MIGRATION_KEY='hermes-pet-skin-migration';
  const DEFAULT_SKIN_ID='keeper';
  const DEFAULT_SKIN_MIGRATION='keeper-default-v1';
  const INSTALL_SEEN_KEY='hermes-pet-install-seen';
  const POLL_MS=2500;
  const BUBBLE_WINDOW={width:320,height:164};
  const INSTALL_WINDOW={width:320,height:300};
  const TOAST_WINDOW={width:320,height:92};
  const bubbles=document.getElementById('petBubbles');
  const collapseBtn=document.getElementById('petCollapse');
  const install=document.getElementById('petInstall');
  const installSprite=document.getElementById('petInstallSprite');
  const installTitle=document.getElementById('petInstallTitle');
  const installStatus=document.getElementById('petInstallStatus');
  const readyToast=document.getElementById('petReadyToast');
  let sessions=[], dismissed=_readJson(DISMISSED_KEY,{}), replySid='', replyText='', replyPendingSid='', replyError='', seeded=false;
  let bubbleScrollTop=0, latestPetLayout=null, visibleMode='hidden', layoutSeq=0;
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
  function _esc(value){return String(value||'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));}
  function _clean(value){return String(value||'').replace(/\s+/g,' ').trim();}
  function _localizeStaticLabels(){
    if(typeof applyLocaleToDOM==='function') applyLocaleToDOM();
    document.title=_petT('desktop_pet_title');
    collapseBtn.setAttribute('aria-label',_petT('desktop_pet_collapse_updates'));
  }
  function _safeSkin(skin){
    if(!skin||typeof skin!=='object') return null;
    const id=String(skin.id||'').trim();
    const displayName=String(skin.displayName||id).trim()||id;
    const spritesheetUrl=String(skin.spritesheetUrl||'').trim();
    if(!/^[A-Za-z0-9_-]+$/.test(id)||!spritesheetUrl) return null;
    return {id,displayName,spritesheetUrl};
  }
  function _applyPetSkin(skinId){
    const next=petSkins.find(skin=>skin.id===skinId)||petSkins[0];
    if(!next) return;
    activeSkinId=next.id;
    if(installSprite) installSprite.style.backgroundImage=`url("${next.spritesheetUrl}")`;
  }
  async function _loadPetSkins(){
    try{
      const data=await fetch('/api/pet/skins',{cache:'no-store'}).then(res=>{if(!res.ok) throw new Error(`Pet skins failed: ${res.status}`);return res.json();});
      const skins=(Array.isArray(data.skins)?data.skins:[]).map(_safeSkin).filter(Boolean);
      if(skins.length) petSkins=skins;
      _applyPetSkin(activeSkinId);
      return true;
    }catch(err){console.warn('Failed to load pet skins',err);_applyPetSkin(activeSkinId);return false;}
  }
  async function _listenPetSkinChanges(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.listen!=='function') return;
    try{await tauri.event.listen('pet-skin-change',event=>_applyPetSkin(String(event.payload||'')));}catch(err){console.warn('Failed to listen for pet skin changes',err);}
  }
  function _setInstallStatus(statusKey){
    if(installTitle) installTitle.textContent=_petT('desktop_pet_install_title');
    if(installStatus) installStatus.textContent=_petT(statusKey);
  }
  function _isInstallVisible(){return !!(install&&!install.hidden);}
  function _isToastVisible(){return !!(readyToast&&!readyToast.hidden);}
  function _hideInstall(){if(install) install.hidden=true;render(true);}
  function _showReadyToast(){
    if(!readyToast) return;
    readyToast.textContent=_petT('desktop_pet_ready_toast');
    readyToast.hidden=false;
    _syncBubbleWindow();
    setTimeout(()=>{readyToast.hidden=true;_syncBubbleWindow();},5200);
  }
  function _runFirstStartInstall(startupPromises){
    if(!install) return;
    if(localStorage.getItem(INSTALL_SEEN_KEY)==='1'){
      _hideInstall();
      return;
    }
    install.hidden=false;
    _syncBubbleWindow('install');
    _setInstallStatus('desktop_pet_install_check_webui');
    setTimeout(()=>_setInstallStatus('desktop_pet_install_load_skins'),520);
    Promise.all(startupPromises).then(()=>{
      _setInstallStatus('desktop_pet_install_ready');
      install.classList.add('is-ready');
      try{localStorage.setItem(INSTALL_SEEN_KEY,'1');}catch(_){}
      setTimeout(()=>{_hideInstall();_showReadyToast();},860);
    }).catch(err=>{
      console.warn('Desktop pet startup check failed',err);
      _setInstallStatus('settings_desktop_pet_start_failed');
    });
  }
  function _seedViewedCounts(rows){
    if(seeded) return;
    const viewed=_readJson(VIEWED_KEY,{});
    for(const row of rows){if(!row.running&&!Object.prototype.hasOwnProperty.call(viewed,row.session_id)) viewed[row.session_id]=Number(row.message_count||0);}
    _writeJson(VIEWED_KEY,viewed);
    seeded=true;
  }
  function _attentionItems(){
    const viewed=_readJson(VIEWED_KEY,{});
    dismissed=_readJson(DISMISSED_KEY,{});
    return sessions.filter(row=>{
      const sid=row.session_id;
      const actionRequired=!!row.action_required;
      const ready=!actionRequired&&!row.running&&Number(row.message_count||0)>Number(viewed[sid]||0);
      const status=actionRequired?'action_required':(row.running?'running':(ready?'ready':'idle'));
      if(status==='idle') return false;
      return dismissed[sid]!==status;
    }).map(row=>({...row,status:row.action_required?'action_required':(row.running?'running':'ready'),actionType:_clean(row.action_required_type),text:_clean(row.process_text)||(row.action_required?_petT('desktop_pet_action_required'):(row.running?_petT('desktop_pet_thinking'):_petT('desktop_pet_ready_for_review')))})).sort((a,b)=>{
      const priority={action_required:3,ready:2,running:1};
      if(a.status!==b.status) return (priority[b.status]||0)-(priority[a.status]||0);
      return Number(b.last_message_at||0)-Number(a.last_message_at||0);
    });
  }
  function _statusHtml(item){
    const status=item&&item.status;
    if(status==='action_required'){
      const type=item&&item.actionType==='approval'?'approval':(item&&item.actionType==='clarify'?'clarify':'action');
      const symbol=type==='approval'?'!':'?';
      return `<span class="pet-action-required is-${type}" aria-label="${_esc(_petT('desktop_pet_action_required'))}">${symbol}</span>`;
    }
    if(status==='running') return `<span class="pet-spinner" aria-label="${_esc(_petT('desktop_pet_running'))}"></span>`;
    return `<span class="pet-ready" aria-label="${_esc(_petT('desktop_pet_ready'))}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg></span>`;
  }
  function _replyHtml(item){
    if(replySid!==item.session_id) return '';
    const pending=replyPendingSid===item.session_id;
    const replyLabel=_petT('desktop_pet_reply');
    const sendingLabel=_petT('desktop_pet_sending');
    const activeLabel=pending?sendingLabel:replyLabel;
    return `<form class="pet-reply" data-sid="${_esc(item.session_id)}"><input class="pet-reply-input" type="text" value="${_esc(replyText)}" placeholder="${_esc(activeLabel)}" aria-label="${_esc(replyLabel)}" autocomplete="off" ${pending?'disabled':''}><button class="pet-reply-submit" type="submit" ${pending?'disabled':''}>${_esc(activeLabel)}</button>${replyError?`<div class="pet-reply-error">${_esc(replyError)}</div>`:''}</form>`;
  }
  function _hiddenCardCount(scroller){
    if(!scroller) return 0;
    const bottom=scroller.scrollTop+scroller.clientHeight;
    return Array.from(scroller.querySelectorAll('.pet-card')).filter(card=>card.offsetTop+card.offsetHeight>bottom+2).length;
  }
  function _syncViewport(){
    const scroller=bubbles.querySelector('.pet-viewport');
    if(!scroller) return;
    const maxScroll=Math.max(0,scroller.scrollHeight-scroller.clientHeight);
    const topHidden=scroller.scrollTop>3;
    const bottomHidden=maxScroll-scroller.scrollTop>3;
    bubbles.classList.toggle('has-hidden-above',topHidden);
    bubbles.classList.toggle('has-overflow',bottomHidden);
    const latest=bubbles.querySelector('.pet-latest');
    const more=bubbles.querySelector('.pet-more');
    if(latest) latest.hidden=!topHidden;
    if(more){
      const count=Math.max(1,_hiddenCardCount(scroller));
      more.hidden=!bottomHidden;
      more.textContent=`+${count}`;
      more.setAttribute('aria-label',_petT('desktop_pet_more_sessions_below',count));
    }
  }
  function _restoreViewport(){
    const scroller=bubbles.querySelector('.pet-viewport');
    if(!scroller) return;
    const maxScroll=Math.max(0,scroller.scrollHeight-scroller.clientHeight);
    scroller.scrollTop=Math.min(maxScroll,Math.max(0,bubbleScrollTop));
    _syncViewport();
  }
  function render(force){
    if(!force&&bubbles.contains(document.activeElement)&&document.activeElement.classList.contains('pet-reply-input')) return;
    const items=_attentionItems();
    const count=items.length;
    const collapsed=localStorage.getItem(COLLAPSED_KEY)==='true';
    bubbles.hidden=!count||collapsed;
    collapseBtn.hidden=!count||collapsed;
    if(!count){bubbles.innerHTML='';_syncBubbleWindow();return;}
    bubbles.innerHTML=`<div class="pet-viewport" tabindex="0"><div class="pet-list" role="list">${items.map(item=>`<article class="pet-card" role="listitem" tabindex="0" data-sid="${_esc(item.session_id)}" data-status="${item.status}" data-action-type="${_esc(item.actionType||'')}" data-reply-open="${replySid===item.session_id?'1':'0'}"><button class="pet-dismiss" type="button" aria-label="${_esc(_petT('desktop_pet_dismiss_update'))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button><div class="pet-card-main"><div><div class="pet-card-title" title="${_esc(item.title)}">${_esc(item.title)}</div><div class="pet-card-text" title="${_esc(item.text)}">${_esc(item.text)}</div></div><div class="pet-card-status">${_statusHtml(item)}</div></div>${item.status==='action_required'||replySid===item.session_id?'':`<button class="pet-reply-toggle" type="button">${_esc(_petT('desktop_pet_reply'))}</button>`}${_replyHtml(item)}</article>`).join('')}</div></div><button class="pet-latest" type="button" hidden>${_esc(_petT('desktop_pet_latest'))}</button><button class="pet-more" type="button" hidden>+1</button>`;
    requestAnimationFrame(_restoreViewport);
    _syncBubbleWindow();
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
  function _markViewed(sid){
    const row=sessions.find(item=>item.session_id===sid);
    if(!row) return;
    const viewed=_readJson(VIEWED_KEY,{});
    viewed[sid]=Number(row.message_count||0);
    _writeJson(VIEWED_KEY,viewed);
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
  function _logicalSize(width,height){const Ctor=_tauriDpiCtor('LogicalSize');return Ctor?new Ctor(width,height):null;}
  function _physicalPosition(x,y){const Ctor=_tauriDpiCtor('PhysicalPosition');return Ctor?new Ctor(Math.round(x),Math.round(y)):null;}
  function _bubbleMode(){
    if(_isInstallVisible()) return 'install';
    if(_isToastVisible()) return 'toast';
    const count=_attentionItems().length;
    const collapsed=localStorage.getItem(COLLAPSED_KEY)==='true';
    return count&&!collapsed?'bubbles':'hidden';
  }
  function _windowForMode(mode){return mode==='install'?INSTALL_WINDOW:(mode==='toast'?TOAST_WINDOW:BUBBLE_WINDOW);}
  function _clamp(value,min,max){return Math.min(max,Math.max(min,value));}
  function _bubbleVerticalPlacement(pet,monitor,size,margin,preferredPlacement){
    const aboveY=pet.y-size.height-margin;
    const belowY=pet.y+pet.height+margin;
    if(!monitor||!monitor.width||!monitor.height) return {placement:'above',y:aboveY};
    const aboveFits=aboveY>=monitor.y+margin;
    const belowFits=belowY+size.height<=monitor.y+monitor.height-margin;
    const petCenterY=pet.y+pet.height/2;
    const preferredVertical=preferredPlacement||(petCenterY<monitor.y+monitor.height/2?'below':'above');
    let placement=preferredVertical;
    if(placement==='above'&&!aboveFits&&belowFits) placement='below';
    if(placement==='below'&&!belowFits&&aboveFits) placement='above';
    if(!aboveFits&&!belowFits){
      const spaceAbove=Math.max(0,pet.y-monitor.y-margin);
      const spaceBelow=Math.max(0,monitor.y+monitor.height-(pet.y+pet.height)-margin);
      placement=spaceBelow>spaceAbove?'below':'above';
    }
    return {placement,y:placement==='below'?belowY:aboveY};
  }
  function _petCollisionRect(pet,margin){
    return {x:pet.x-margin,y:pet.y-margin,width:pet.width+margin*2,height:pet.height+margin*2};
  }
  function _rectsOverlap(a,b){
    return a.x<b.x+b.width&&a.x+a.width>b.x&&a.y<b.y+b.height&&a.y+a.height>b.y;
  }
  function _rectFitsMonitor(rect,monitor,margin){
    if(!monitor||!monitor.width||!monitor.height) return true;
    return rect.x>=monitor.x+margin&&rect.y>=monitor.y+margin&&rect.x+rect.width<=monitor.x+monitor.width-margin&&rect.y+rect.height<=monitor.y+monitor.height-margin;
  }
  function _verticalClearance(pet,monitor,margin){
    return Math.max(margin,pet.height+margin*2);
  }
  function _bubbleCandidatePositions(pet,monitor,size,margin,mode){
    const headX=pet.x+pet.width/2;
    const centerY=pet.y+pet.height/2-size.height/2;
    const verticalGap=_verticalClearance(pet,monitor,margin);
    const vertical=_bubbleVerticalPlacement(pet,monitor,size,verticalGap,_modePreferredPlacement(mode));
    const candidates=[
      {placement:vertical.placement,x:headX-size.width/2,y:vertical.y},
      {placement:'right',x:pet.x+pet.width+margin,y:centerY},
      {placement:'left',x:pet.x-size.width-margin,y:centerY},
    ];
    const opposite=vertical.placement==='above'?'below':'above';
    candidates.push({placement:opposite,x:headX-size.width/2,y:opposite==='below'?pet.y+pet.height+verticalGap:pet.y-size.height-verticalGap});
    return candidates;
  }
  function _modePreferredPlacement(mode){
    return mode==='install'||mode==='toast'?'above':'';
  }
  function _positionWindowSize(size,monitor){
    const scale=(Number(monitor&&monitor.scale)||1)||1;
    return {width:size.width*scale,height:size.height*scale};
  }
  function _bubblePosition(layout,size,mode){
    const pet=layout&&layout.pet;
    const monitor=layout&&layout.monitor;
    if(!pet) return null;
    const margin=8*((monitor&&monitor.scale)||1);
    const windowSize=_positionWindowSize(size,monitor);
    const blocked=_petCollisionRect(pet,margin);
    const candidates=_bubbleCandidatePositions(pet,monitor,windowSize,margin,mode);
    for(const candidate of candidates){
      if(_rectFitsMonitor({...candidate,width:windowSize.width,height:windowSize.height},monitor,margin)&&!_rectsOverlap({...candidate,width:windowSize.width,height:windowSize.height},blocked)){
        return {x:candidate.x,y:candidate.y};
      }
    }
    let {x,y}=candidates[0]||{x:pet.x,y:pet.y};
    if(monitor&&monitor.width&&monitor.height){
      x=_clamp(x,monitor.x+margin,monitor.x+monitor.width-windowSize.width-margin);
      y=_clamp(y,monitor.y+margin,monitor.y+monitor.height-windowSize.height-margin);
      const clamped={x,y,width:windowSize.width,height:windowSize.height};
      if(_rectsOverlap(clamped,blocked)){
        const side=candidates.find(candidate=>candidate.placement==='right'||candidate.placement==='left');
        if(side) return {x:side.x,y:_clamp(side.y,monitor.y+margin,monitor.y+monitor.height-windowSize.height-margin)};
      }
    }
    return {x,y};
  }
  async function _syncBubbleWindow(forcedMode){
    const seq=++layoutSeq;
    const win=_currentTauriWindow();
    if(!win) return;
    const mode=forcedMode||_bubbleMode();
    try{
      if(mode==='hidden'){
        visibleMode='hidden';
        if(typeof win.hide==='function') await win.hide();
        return;
      }
      const size=_windowForMode(mode);
      const logical=_logicalSize(size.width,size.height);
      if(logical&&typeof win.setSize==='function') await win.setSize(logical);
      const pos=_bubblePosition(latestPetLayout,size,mode);
      if(seq!==layoutSeq) return;
      if(!pos){
        if(typeof win.hide==='function') await win.hide();
        return;
      }
      if(pos&&typeof win.setPosition==='function'){
        const physical=_physicalPosition(pos.x,pos.y);
        if(physical) await win.setPosition(physical);
      }
      visibleMode=mode;
      if(typeof win.show==='function') await win.show();
    }catch(err){console.warn('Failed to sync pet bubbles window',err);}
  }
  function _csrfHeaders(){
    const token=window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.csrfToken;
    return token?{'X-Hermes-CSRF-Token':token}:{};
  }
  async function _openSessionInBrowser(sid, params){
    const res=await fetch('/api/pet/open_session',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json',..._csrfHeaders()},body:JSON.stringify({session_id:sid,...(params||{})})});
    if(!res.ok) throw new Error(await res.text());
    return res.json();
  }
  function _openSession(sid,status){
    if(status!=='action_required') _markViewed(sid);
    render();
    _openSessionInBrowser(sid).catch(err=>console.warn('Failed to open session from pet',err));
  }
  async function _reply(card){
    const sid=card&&card.dataset.sid;
    const input=card&&card.querySelector('.pet-reply-input');
    const text=_clean(input&&input.value);
    if(!sid||!text){if(input) input.focus();return;}
    replyPendingSid=sid;
    replyError='';
    render(true);
    try{
      await _openSessionInBrowser(sid,{draft:text,autosend:true});
      _markViewed(sid);
      replySid='';
      replyText='';
      replyPendingSid='';
      replyError='';
      render(true);
    }catch(err){
      console.warn('Failed to reply from pet',err);
      replyPendingSid='';
      replyError=_petT('desktop_pet_failed_to_send');
      render(true);
      setTimeout(()=>document.querySelector('.pet-reply-input')?.focus(),0);
    }
  }
  bubbles.addEventListener('click',event=>{
    if(event.target.closest('.pet-latest')){event.preventDefault();event.stopPropagation();const scroller=bubbles.querySelector('.pet-viewport');if(scroller){scroller.scrollTop=0;bubbleScrollTop=0;_syncViewport();}return;}
    if(event.target.closest('.pet-more')){event.preventDefault();event.stopPropagation();const scroller=bubbles.querySelector('.pet-viewport');if(scroller){scroller.scrollTop=Math.min(scroller.scrollHeight,scroller.scrollTop+scroller.clientHeight*.85);bubbleScrollTop=scroller.scrollTop;_syncViewport();}return;}
    const card=event.target.closest('.pet-card');
    if(!card) return;
    if(event.target.closest('.pet-dismiss')){dismissed[card.dataset.sid]=card.dataset.status;_writeJson(DISMISSED_KEY,dismissed);render();return;}
    if(event.target.closest('.pet-reply-toggle')){replySid=replySid===card.dataset.sid?'':card.dataset.sid;replyText='';replyError='';render(true);setTimeout(()=>document.querySelector('.pet-reply-input')?.focus(),0);return;}
    if(event.target.closest('.pet-reply')) return;
    _openSession(card.dataset.sid,card.dataset.status);
  });
  bubbles.addEventListener('submit',event=>{event.preventDefault();_reply(event.target.closest('.pet-card'));});
  bubbles.addEventListener('input',event=>{if(event.target.classList.contains('pet-reply-input')) replyText=event.target.value;});
  bubbles.addEventListener('scroll',event=>{if(event.target.classList.contains('pet-viewport')){bubbleScrollTop=event.target.scrollTop;_syncViewport();}},true);
  bubbles.addEventListener('keydown',event=>{
    if(!event.target.classList||!event.target.classList.contains('pet-reply-input')) return;
    if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();_reply(event.target.closest('.pet-card'));}
  });
  collapseBtn.addEventListener('click',()=>{localStorage.setItem(COLLAPSED_KEY,'true');render();});
  window.addEventListener('storage',event=>{if([COLLAPSED_KEY,DISMISSED_KEY,SKIN_KEY].includes(event.key)){activeSkinId=localStorage.getItem(SKIN_KEY)||activeSkinId;_applyPetSkin(activeSkinId);render(true);}});
  async function _listenPetWindowEvents(){
    const tauri=window.__TAURI__;
    if(!tauri||!tauri.event||typeof tauri.event.listen!=='function') return;
    try{
      await tauri.event.listen('pet-layout-update',event=>{latestPetLayout=event.payload||latestPetLayout;_syncBubbleWindow();});
      await tauri.event.listen('pet-attention-update',()=>{render(true);});
    }catch(err){console.warn('Failed to listen for pet layout events',err);}
  }
  setInterval(refresh,POLL_MS);
  async function _bootBubbles(){
    _localizeStaticLabels();
    await _listenPetWindowEvents();
    const skinStartup=_loadPetSkins();
    const attentionStartup=refresh();
    _runFirstStartInstall([skinStartup,attentionStartup]);
    _listenPetSkinChanges();
  }
  _bootBubbles();
})();
