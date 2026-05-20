(function(){
  const PET_NAVIGATION_LAST_KEY='hermes-pet-navigation-last-id';
  let pollBusy=false;

  async function _petBridgeApi(path){
    const response=await fetch(path,{credentials:'include',cache:'no-store'});
    if(!response.ok) throw new Error(`Pet navigation failed: ${response.status}`);
    return response.json();
  }

  async function _pollPetNavigation(){
    if(pollBusy) return;
    pollBusy=true;
    try{
      const since=(()=>{try{return localStorage.getItem(PET_NAVIGATION_LAST_KEY)||'';}catch(_){return '';}})();
      const data=await _petBridgeApi('/api/pet/navigation?since='+encodeURIComponent(since));
      const command=data&&data.command;
      if(command&&command.id&&command.id!==since&&typeof window.__hermesApplyPetNavigationCommand==='function'){
        await window.__hermesApplyPetNavigationCommand(command);
        try{localStorage.setItem(PET_NAVIGATION_LAST_KEY,String(command.id));}catch(_){}
      }
    }catch(_e){
      // Desktop pet navigation is opportunistic; normal session polling remains authoritative.
    }finally{
      pollBusy=false;
    }
  }

  setTimeout(_pollPetNavigation,600);
  setInterval(_pollPetNavigation,1000);
})();
