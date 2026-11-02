import React, { useEffect, useMemo, useState } from 'react';
import { api } from './api';
import Chart, { FactTable, Kpi } from './Chart';

const keyOf=(c)=>`${c.run_id}:${c.fact_id}`;
const labelValues=(dash)=>{
  const out={};
  Object.values(dash?.data||{}).forEach(f=>f.rows?.forEach(r=>Object.entries(r.labels||{}).forEach(([k,v])=>{
    if(!out[k])out[k]=new Set(); out[k].add(String(v));
  })));
  return Object.fromEntries(Object.entries(out).map(([k,v])=>[k,[...v].sort()]));
};
const filterFact=(fact,filters)=>!fact?fact:{...fact,rows:fact.rows.filter(r=>Object.entries(filters).every(([k,v])=>!v||String(r.labels?.[k])===v))};

export default function DashboardView({ id, onClose, onError }) {
  const [dash,setDash]=useState(null), [versions,setVersions]=useState([]), [edit,setEdit]=useState(false);
  const [query,setQuery]=useState(''), [filters,setFilters]=useState({}), [dragId,setDragId]=useState(null);
  // `alive` stops a slow response for a previously opened dashboard from overwriting the current one
  async function load(alive=()=>true){try{const d=await api.dashboard(id),v=await api.versions(id);if(alive()){setDash(d);setVersions(v)}}catch(e){if(alive())onError(e.message)}}
  useEffect(()=>{let live=true;setDash(null);load(()=>live);return()=>{live=false}},[id]);
  async function save(){try{await api.updateDashboard(id,{name:dash.name,spec:dash.spec,base_version:dash.version});setEdit(false);await load()}catch(e){onError(e.message)}}
  const [exporting,setExporting]=useState(false);
  async function doExport(fmt){setExporting(true);try{await api.exportDashboard(id,fmt)}catch(e){onError(e.message)}finally{setExporting(false)}}
  async function restore(version){try{await api.restore(id,version);await load()}catch(e){onError(e.message)}}
  const updateComp=(cid,patch)=>setDash(d=>({...d,spec:{...d.spec,components:d.spec.components.map(c=>c.id===cid?{...c,...patch,layout:{...c.layout,...(patch.layout||{})}}:c)}}));
  const removeComp=cid=>setDash(d=>({...d,spec:{...d.spec,components:d.spec.components.filter(c=>c.id!==cid)}}));
  const reorder=(from,to)=>setDash(d=>{const a=[...d.spec.components],i=a.findIndex(x=>x.id===from),j=a.findIndex(x=>x.id===to);if(i<0||j<0||i===j)return d;const [x]=a.splice(i,1);a.splice(j,0,x);return {...d,spec:{...d.spec,components:a}}});
  const resize=(cid,dw,dh)=>setDash(d=>({...d,spec:{...d.spec,components:d.spec.components.map(c=>c.id===cid?{...c,layout:{...c.layout,w:Math.max(2,Math.min(12,c.layout.w+dw)),h:Math.max(2,Math.min(10,c.layout.h+dh))}}:c)}}));
  const options=useMemo(()=>labelValues(dash),[dash]);
  const shown=useMemo(()=>dash?.spec.components.filter(c=>c.title.toLowerCase().includes(query.toLowerCase()))||[],[dash,query]);
  if(!dash)return <div className="card">Loading dashboard...</div>;
  return <div className="card dashboard-card">
    <div className="toolbar"><div><input className="title-input" disabled={!edit} value={dash.name} onChange={e=>setDash({...dash,name:e.target.value})}/><small className="muted">v{dash.version} • saved evidence snapshot</small></div><div>{!edit?<button onClick={()=>setEdit(true)}>Customize</button>:<><button onClick={save}>Save changes</button><button className="secondary" onClick={()=>{setEdit(false);load()}}>Cancel</button></>}{!edit&&["pdf","pptx","xlsx","csv"].map(f=>exporting?null:<button key={f} className="secondary" onClick={()=>doExport(f)}>Export {f.toUpperCase()}</button>)}<button className="secondary" onClick={onClose}>Close</button></div></div>
    <div className="executive"><h3>Executive summary</h3><ul>{(dash.claims||[]).map((c,i)=><li key={i}>{c.text}{c.fact_ids?.length>0&&<small className="muted"> [{c.fact_ids.join(", ")}]</small>}</li>)}<li>Use the filters below to narrow every compatible visualization. All numbers are traceable to saved analysis facts.</li></ul></div>
    <div className="dashboard-filters"><input className="filter-input" placeholder="Search dashboard components..." value={query} onChange={e=>setQuery(e.target.value)}/>{Object.entries(options).map(([field,vals])=><label key={field}>{field}<select value={filters[field]||''} onChange={e=>setFilters({...filters,[field]:e.target.value})}><option value="">All</option>{vals.slice(0,50).map(v=><option key={v}>{v}</option>)}</select></label>)}{Object.keys(filters).length>0&&<button className="secondary" onClick={()=>setFilters({})}>Clear filters</button>}</div>
    <div className="grid">{shown.map(c=>{const raw=dash.data[keyOf(c)],fact=filterFact(raw,filters);return <div className="tile" key={c.id} draggable={edit} onDragStart={()=>setDragId(c.id)} onDragOver={e=>edit&&e.preventDefault()} onDrop={()=>edit&&reorder(dragId,c.id)} style={{gridColumn:`span ${c.layout.w}`,minHeight:c.layout.h*70}}><div className="tile-head"><h4>{c.title}</h4>{edit&&<div><span className="drag-handle" title="Drag to reorder">⠿</span><button className="icon" onClick={()=>resize(c.id,-1,0)}>−W</button><button className="icon" onClick={()=>resize(c.id,1,0)}>+W</button><button className="icon" onClick={()=>resize(c.id,0,-1)}>−H</button><button className="icon" onClick={()=>resize(c.id,0,1)}>+H</button><button className="icon" onClick={()=>removeComp(c.id)}>✕</button></div>}</div>{c.type==='kpi'&&<Kpi fact={fact}/>} {c.type==='table'&&<FactTable fact={fact}/>} {['bar','line','pie'].includes(c.type)&&<Chart type={c.type} fact={fact} height={Math.max(180,c.layout.h*60)}/>}<details><summary>Drill-down / filtered underlying data ({fact?.rows?.length||0} rows)</summary><FactTable fact={fact}/></details>{edit&&<div className="layout-controls"><small>Drag cards to reorder • Size: {c.layout.w}×{c.layout.h}</small></div>}<small className="muted">evidence {c.fact_id}</small></div>})}</div>
    <details><summary>Version history</summary>{versions.map(v=><div className="dash" key={v.version}><span>v{v.version} - {v.name}</span>{v.version!==dash.version&&<button className="secondary" onClick={()=>restore(v.version)}>Restore</button>}</div>)}</details>
  </div>;
}
