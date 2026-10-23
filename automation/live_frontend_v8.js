const map = L.map('map', {zoomControl:true}).setView([54.0,-114.5],6);
const primaryTiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors',maxZoom:19}).addTo(map);
const backupTiles = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',{attribution:'&copy; OpenStreetMap, &copy; CARTO',maxZoom:18});
let backupActivated=false;
primaryTiles.on('tileerror',()=>{if(!backupActivated){backupActivated=true;backupTiles.addTo(map);}});
const markers=new Map();
const esc=value=>String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const serviceSelect=document.getElementById('serviceSelect'),statusSelect=document.getElementById('statusSelect');
const LIVE_STALE_AFTER_HOURS=36,LIVE_DOWN_AFTER_HOURS=48;
function sourceAgeHours(now){return (now-new Date(LIVE_DATA.source_fetched_at))/3600000;}
function receivedWallClock(instant){
  const stamp=instant.getTime(),clock=LIVE_DATA.clock_contract;
  if(!Number.isFinite(stamp)||!clock||stamp<clock.valid_from_ms||stamp>=clock.valid_until_ms)throw new Error('Time outside the received Alberta clock range');
  let offset=clock.transitions[0][1];
  for(const [at,value] of clock.transitions){if(stamp<at)break;offset=value;}
  return new Date(stamp+offset*1000);
}
function dateKey(instant){return receivedWallClock(instant).toISOString().slice(0,10);}
function formatMt(instant){
  return new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'long',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).format(receivedWallClock(instant))+' Alberta time';
}
function runtimeStatus(item,now){
  if(item.status==='unclear')return 'unclear';
  const windows=(item.schedule_windows||[]).map(x=>({start:new Date(x.start),end:new Date(x.end)}));
  let status;
  if(windows.length){
    if(windows.some(x=>x.start<=now&&now<x.end))status='active_now';
    else if(windows.some(x=>x.start>now))status='scheduled';
    else return null;
  }else{
    const today=dateKey(now);
    if(item.end_date_key&&today>=item.end_date_key)return null;
    status=item.start_date_key&&today<item.start_date_key?'scheduled':'active_now';
  }
  const age=sourceAgeHours(now);
  return !Number.isFinite(age)||age< -1||age>=LIVE_DOWN_AFTER_HOURS?'unclear':status;
}
function statusLabel(status){return status==='active_now'?'Active now':status==='scheduled'?'Scheduled / recurring':'Status unclear';}
function currentItems(site,now){return site.disruptions.map(item=>({...item,status:runtimeStatus(item,now)})).filter(x=>x.status);}
function selectedItems(site,now){
  return currentItems(site,now).filter(item=>(statusSelect.value==='all'||item.status===statusSelect.value)&&(serviceSelect.value==='all'||item.service_layer===serviceSelect.value));
}
function matches(site,items){
  const q=document.getElementById('search').value.trim().toLowerCase();
  return items.length>0&&(!q||[site.display_name,site.community,...items.map(x=>x.service+' '+x.details)].join(' ').toLowerCase().includes(q));
}
function card(item){
  return '<div class="live-card '+esc(item.status)+'"><div class="live-status '+esc(item.status)+'">'+esc(statusLabel(item.status))+'</div><div class="service">'+esc(item.service)+'</div>'+
    (item.reduction?'<div class="details">'+esc(item.reduction)+'</div>':'')+
    (item.reason?'<div class="details">'+esc(item.reason)+'</div>':'')+
    (item.status==='unclear'?'<div class="details">The current timing or service restriction needs clarification; consult the source notice.</div>':'')+
    (item.timing_note?'<div class="details">'+esc(item.timing_note)+'</div>':'')+
    (item.start||item.end?'<div class="details">'+esc(item.start||'current')+(item.end?' – '+esc(item.end):'')+'</div>':'')+
    '<a href="'+esc(item.source_url)+'" target="_blank" rel="noopener">Source notice</a></div>';
}
function popup(site,items){return '<div class="popup-title">'+esc(site.display_name)+'</div><div class="popup-stat">'+esc(site.community)+' · '+items.length+' notice(s)</div>'+items.map(card).join('');}
function markerStyle(items){
  const kinds=new Set(items.map(x=>x.status));
  const color=kinds.size>1?'#7A3E9D':kinds.has('active_now')?'#B42318':kinds.has('scheduled')?'#C8871A':'#667085';
  return {radius:Math.min(18,6+items.length*2),color:'#0F2347',fillColor:color,fillOpacity:.78,weight:2};
}
function updateHealth(now){
  document.getElementById('liveClock').textContent='Current time: '+formatMt(now);
  const box=document.getElementById('liveHealth'),age=sourceAgeHours(now);
  if(!Number.isFinite(age)||age< -1||age>=LIVE_DOWN_AFTER_HOURS){
    box.hidden=false;box.className='live-health down';box.textContent='Live updates may be unavailable. Current status is unclear because the last source check is over 48 hours old or its observation time is invalid.';
  }else if(age>=LIVE_STALE_AFTER_HOURS){
    box.hidden=false;box.className='live-health warning';box.textContent='Live updates may be delayed. The map follows the last checked postings shown above.';
  }else box.hidden=true;
}
function render(){
  const now=new Date();updateHealth(now);
  const list=document.getElementById('list');list.innerHTML='';
  const bounds=[],selected=[],mappedSites=new Set();let unmappedCount=0;
  function addSection(site,items,unmapped){
    const section=document.createElement('section');section.className='site';
    section.innerHTML='<div class="site-name">'+esc(site.display_name)+'</div><div class="community">'+esc(site.community)+' · '+items.length+' matching notice(s)'+(unmapped?' · location not yet mapped':'')+'</div>'+items.map(card).join('');
    if(!unmapped)section.addEventListener('click',()=>map.setView([site.lat,site.lng],Math.max(map.getZoom(),8)));
    list.appendChild(section);
  }
  LIVE_DATA.sites.forEach(site=>{
    const items=selectedItems(site,now),visible=matches(site,items),marker=markers.get(site.name);
    if(marker){
      marker.setStyle(markerStyle(items)).setPopupContent(popup(site,items));
      if(visible)marker.addTo(map);else map.removeLayer(marker);
    }
    if(!visible)return;
    mappedSites.add(site.name);selected.push(...items);bounds.push([site.lat,site.lng]);addSection(site,items,false);
  });
  for(const item of LIVE_DATA.unmapped_disruptions||[]){
    const site={display_name:item.facility_name,community:item.community||'',disruptions:[item]};
    const items=selectedItems(site,now);
    if(!matches(site,items))continue;
    selected.push(...items);unmappedCount++;addSection(site,items,true);
  }
  const active=selected.filter(x=>x.status==='active_now').length,scheduled=selected.filter(x=>x.status==='scheduled').length,unclear=selected.filter(x=>x.status==='unclear').length;
  document.getElementById('summaryStats').textContent=active+' active now · '+scheduled+' scheduled / recurring'+(unclear?' · '+unclear+' status unclear':'')+' · '+mappedSites.size+' mapped sites'+(unmappedCount?' · '+unmappedCount+' unmapped notice(s)':'');
  document.getElementById('resultCount').textContent=selected.length+' notice(s) at '+mappedSites.size+' mapped site(s)'+(unmappedCount?' + '+unmappedCount+' unmapped notice(s)':'');
  if(!list.children.length)list.innerHTML='<div class="empty-state">No disruptions match this filter.</div>';
  if(bounds.length)map.fitBounds(bounds,{padding:[30,30],maxZoom:8});
}
LIVE_DATA.sites.forEach(site=>markers.set(site.name,L.circleMarker([site.lat,site.lng],markerStyle([])).bindPopup(popup(site,[]))));
document.getElementById('search').addEventListener('input',render);serviceSelect.addEventListener('change',render);statusSelect.addEventListener('change',render);
render();setInterval(render,60000);

