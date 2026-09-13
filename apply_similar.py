# -*- coding: utf-8 -*-
# 相似有声书检测（主线功能，版本号升到 1.3.54）
#   入口：书库顶部栏「相似检测」按钮 -> 弹窗
#   后端：GET/PUT /api/similar-workflow（缓存） + POST /api/similar-detect（扫描分组） + POST /api/similar-execute（执行）
#   前端：配置(容差/去年份/去符号) + 左侧结果分组 + 右侧处理面板(方式/保留/元数据拷贝)与信息面板(元数据+章节)
#         + 预览/执行/安全退出（>10 二次确认、已执行跳过、自动缓存）
# 注入方式：与已有 adv_routes_new / jg3d_new 一致，定义变量后在 build 期通过 src.replace / js.replace 应用，
#           避免把多行 JS 直接拼进 build.py 的 Python 源码字符串片段里（会破坏语法）。
import io

P = "build.py"
s = io.open(P, encoding="utf-8").read()
NL = "\n"

def rep1(text, old, new, tag):
    n = text.count(old)
    assert n == 1, "%s anchor count=%d" % (tag, n)
    return text.replace(old, new)

# ============ 1) 后端路由（变量模式，挂在 advanced-cache/clear 路由之前，作为同级路由） ============
# 注意：SIM_ROUTES 必须以逗号结尾，使其与后面的 advanced-cache/clear 路由用逗号连接成同级路由序列。
SIM_ROUTES = r'''s.get("/api/similar-workflow",async()=>{let wf=t.settings.similarWorkflow||null;return f({success:!0,data:wf})}),
s.put("/api/similar-workflow",async o=>{let b=typeof o.body=="string"?JSON.parse(o.body):o.body||{};t.settings.similarWorkflow=b;await t.saveSettings();return f({success:!0,data:{ok:!0}})}),
s.post("/api/similar-detect",async o=>{try{let b=typeof o.body=="string"?JSON.parse(o.body):o.body||{},tol=Number(b.tolerance==null?70:b.tolerance),sy=!!b.stripYear,ss=!!b.stripSymbols;if(t.__simDetect&&!t.__simDetect.finished)return f({success:!0,data:{async:!0,running:!0}});
let job={phase:"读取书库",done:0,total:0,finished:!1,error:"",count:0};t.__simDetect=job;
(async()=>{try{
function norm(t){let s0=String(t==null?"":t).toLowerCase().trim();if(sy)s0=s0.replace(/\d{4}/g,"");if(ss)s0=s0.replace(/[\s\-_·~!@#$%^&*()\[\]{}<>?/\\|"‘’“”《>,，。.、：:；;…—+='=$]/g,"");return s0}
function bigs(s){let st=new Set();for(let i=0;i<s.length-1;i++)st.add(s.substring(i,i+2));return st}
let arr=[];for(const bb of t.books){if(bb.virt||bb.hidden||bb.mergedInto)continue;let n=norm(bb.title),nz=n.replace(/\d+/g,"");arr.push({id:bb.id,title:bb.title||"",author:bb.author||"",category:bb.category||"",coverUrl:bb.coverUrl||"",description:bb.description||"",tags:(bb.tags||[]),folderRelPath:bb.folderRelPath||"",chapters:(bb.chapters?bb.chapters.length:((bb.chapterCount)||0)),n:n,bg:bigs(n),nz:nz,bz:bigs(nz)})}
function diceSet(a,b){if(!a.size||!b.size)return a===b?1:0;let it=0;for(const g of a)if(b.has(g))it++;return 2*it/(a.size+b.size)}function bracketCores(t){let re=/[\[【][^\]】]{1,40}[\]】]/g,o=[],m;while((m=re.exec(t))){let s=m[0].slice(1,-1).trim().toLowerCase();if(s.length>=3)o.push(s)}return o}
let par=arr.map((_,i)=>i);function find0(x){while(par[x]!==x){par[x]=par[par[x]];x=par[x]}return x}function uni0(a,b){let ra=find0(a),rb=find0(b);if(ra!==rb)par[ra]=rb}
job.phase="建立索引";let thr=tol/100,inv={},seen=new Set();
for(let i=0;i<arr.length;i++){if(!arr[i].n&&!arr[i].nz)continue;let uni=new Set();for(const g of arr[i].bg)uni.add(g);for(const g of arr[i].bz)uni.add(g);for(const g of uni)(inv[g]||(inv[g]=[])).push(i)}
let keys=Object.keys(inv);keys.sort(function(a,b){return inv[a].length-inv[b].length});job.total=keys.length;job.phase="配对计算";for(const g of keys){let lst=inv[g];if(lst.length>300)continue;for(let a=0;a<lst.length;a++)for(let c=a+1;c<lst.length;c++){let x=lst[a],y=lst[c];if(seen.has(x*1000000+y))continue;seen.add(x*1000000+y);if(find0(x)===find0(y))continue;let la=arr[x].n.length,lb=arr[y].n.length,la2=arr[x].nz.length,lb2=arr[y].nz.length,ra=la&&lb&&(la/lb>2.5||lb/la>2.5),rb=la2&&lb2&&(la2/lb2>2.5||lb2/la2>2.5);if(ra&&rb)continue;if((!ra&&diceSet(arr[x].bg,arr[y].bg)>=thr)||(!rb&&diceSet(arr[x].bz,arr[y].bz)>=thr))uni0(x,y)}job.done++}for(let i=0;i<arr.length;i++){let ca=bracketCores(arr[i].title);for(const c of ca){for(let j=0;j<arr.length;j++){if(j===i)continue;if(arr[j].title.toLowerCase().indexOf(c)<0)continue;if(find0(i)===find0(j))continue;uni0(i,j)}}}
let gm={};for(let i=0;i<arr.length;i++){let r=find0(i);(gm[r]||(gm[r]=[])).push(i)}
let groups=[],gid=0;for(const k in gm){let idx=gm[k];if(idx.length<2)continue;let books=idx.map(i=>({id:arr[i].id,title:arr[i].title,author:arr[i].author,category:arr[i].category,coverUrl:arr[i].coverUrl,description:arr[i].description,tags:arr[i].tags,folderRelPath:arr[i].folderRelPath,chapters:arr[i].chapters}));groups.push({id:"g"+(++gid),books:books})}
let wf={detectParams:{tolerance:tol,stripYear:sy,stripSymbols:ss},groups:groups,settings:{},executed:[]};t.settings.similarWorkflow=wf;await t.saveSettings();job.count=groups.length;job.finished=!0}catch(err){job.error=String(err&&err.message||err);job.finished=!0}})();return f({success:!0,data:{async:!0,running:!0}})}catch(err){return h(String(err&&err.message||err),500)}}),
s.get("/api/similar-detect-progress",async()=>{let j=t.__simDetect;return f({success:!0,data:j?{phase:j.phase,done:j.done,total:j.total,finished:j.finished,error:j.error,count:j.count}:null})}),
s.post("/api/similar-execute",async()=>{let wf=t.settings.similarWorkflow;if(!wf)return h("无工作流缓存",400);
if(!Array.isArray(wf.groups))wf.groups=[];if(!Array.isArray(wf.executed))wf.executed=[];if(!wf.settings)wf.settings={};
async function copyMeta(fromB,toB,field){if(field==="cover"){let b64=null;if(fromB.coverUrl&&/^data:/i.test(fromB.coverUrl)){let m0=fromB.coverUrl.match(/^data:[^;,]*;base64,([\s\S]*)$/);m0&&(b64=m0[1])}if(!b64&&fromB.folderRelPath){try{let d0=await songloft.fs.readFile(fromB.folderRelPath+"/cover.jpg",{encoding:"base64"});if(d0)b64=d0}catch(_){}}if(b64)await t.updateCover(toB.id,b64)}else{let val;if(field==="name")val=fromB.title;else if(field==="description")val=fromB.description;else if(field==="author")val=fromB.author;else if(field==="category")val=fromB.category;else if(field==="tags")val=fromB.tags;if(val===undefined)return;if(field==="name"){t.settings.titleOverrides||(t.settings.titleOverrides={});t.settings.titleOverrides[toB.id]=String(val);await t.saveSettings()}else{let bd={};if(field==="description")bd.description=val;if(field==="author")bd.author=val;if(field==="category")bd.category=val;if(field==="tags")bd.tags=Array.isArray(val)?val:[String(val||"")];await t.updateMetadata(toB.id,bd)}}}
let todo=[];for(const g of wf.groups){if(wf.executed.indexOf(g.id)>=0)continue;let st0=wf.settings[g.id]||{method:"none"};if(st0.method!=="none")todo.push(g)}
if(!t.__simJob||t.__simJob.finished){let total=0;for(const g of todo){let st=wf.settings[g.id]||{method:"none"};if(st.method==="delete"){let keep=(st.keepIds||[]).map(String);total+=g.books.filter(b=>keep.indexOf(b.id)<0).length}else total+=1;if(st.metaCopies&&st.metaCopies.length)total+=st.metaCopies.length}
let job={total:total,done:0,current:"",currentGroup:0,totalGroups:todo.length,errors:[],finished:!1,startedAt:Date.now()};t.__simJob=job;job.execArr=wf.executed;
(async()=>{try{for(const g of todo){if(job.finished)break;job.currentGroup++;job.current=(g.books[0]&&g.books[0].title)||g.id;let st=wf.settings[g.id]||{method:"none"};try{if(st.method==="collect"){let ids=g.books.map(b=>b.id),nm=String(st.collectName||g.books[0].title||"相似合集").slice(0,60),nid=Date.now().toString(36)+Math.random().toString(36).slice(2,6);t.settings.customCollections||(t.settings.customCollections=[]);t.settings.customCollections.push({id:nid,name:nm,memberIds:ids});await t.saveSettings();__CUSTOM_MERGE(t,t.settings);try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}}if(st.metaCopies&&st.metaCopies.length){for(const mc of st.metaCopies){let fb=g.books[mc.from]||null,tb=g.books[mc.to]||null;if(fb&&tb)await copyMeta(fb,tb,mc.field)}}if(st.method==="delete"){let keep=(st.keepIds||[]).map(String);for(const b of g.books){if(keep.indexOf(b.id)>=0)continue;try{await __delBook(t,b.id)}catch(_){}job.done++;await new Promise(r=>setTimeout(r,0))}}wf.executed.push(g.id);if(st.method!=="delete")job.done++}catch(err){job.errors.push({group:g.id,error:String(err&&err.message||err)})}await new Promise(r=>setTimeout(r,0))}}catch(err){job.errors.push({group:"__job",error:String(err&&err.message||err)})}job.finished=!0;try{await t.saveSettings()}catch(_){}})()}
return f({success:!0,data:{async:!0,total:todo.length,running:!!(t.__simJob&&!t.__simJob.finished)}})}),
s.get("/api/similar-execute-progress",async()=>{let j=t.__simJob;return f({success:!0,data:j?{total:j.total,done:j.done,current:j.current,currentGroup:j.currentGroup,totalGroups:j.totalGroups,errors:j.errors,executed:j.execArr,finished:j.finished,startedAt:j.startedAt}:null})}),'''
backend_anchor = '        src = src.replace(adv_routes_old, adv_routes_new)'
assert backend_anchor in s, "后端锚点缺失"
backend_inject = (
    backend_anchor + NL +
    "        SIM_ROUTES = r'''" + SIM_ROUTES + "'''\n" +
    '        sim_routes_old = \'s.post("/api/advanced-cache/clear",async o=>{\'' + NL +
    '        sim_routes_new = (SIM_ROUTES + sim_routes_old)' + NL +
    '        src = src.replace(sim_routes_old, sim_routes_new)' + NL
)
s = s.replace(backend_anchor, backend_inject)

# ============ 2) 前端 JS：相似检测逻辑（变量模式，挂在 __syncPlayBtns 之前） ============
SIM_JS = r'''function __esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return c==="&"?"&amp;":c==="<"?"&lt;":c===">"?"&gt;":"&quot;"})}
function __fieldName(f){return f==="cover"?"封面":f==="name"?"名称":f==="description"?"简介":f==="author"?"作者":f==="category"?"类型":f==="tags"?"标签":f}
function __methodName(m){return m==="collect"?"创建合集":m==="delete"?"删除重复":"不处理"}
function __similarInit(){if(window.__simInited)return;window.__simInited=!0;window.__sim={wf:null,openGroup:null,openBook:null};
var mask=document.getElementById("similarMask");if(!mask)return;
var bd=document.getElementById("btnSimilarDetect");if(bd)bd.onclick=__simOpen;
document.getElementById("smClose").onclick=__simClose;document.getElementById("smExit").onclick=__simClose;
mask.addEventListener("click",function(e){if(e.target===mask)__simClose()});
document.getElementById("smDetect").onclick=__simDetectRun;
document.getElementById("smPreview").onclick=__simPreview;
document.getElementById("smExec").onclick=__simExec}
async function __simLoad(){try{window.__sim.wf=await y("/api/similar-workflow")}catch(_){window.__sim.wf=null}
if(!window.__sim.wf)window.__sim.wf={detectParams:{tolerance:70,stripYear:!1,stripSymbols:!1},groups:[],settings:{},executed:[]};
var wf=window.__sim.wf;
if(!Array.isArray(wf.groups))wf.groups=[];
if(!Array.isArray(wf.executed))wf.executed=[];
if(!wf.settings||typeof wf.settings!=="object")wf.settings={};
return wf}
async function __simSave(){try{await y("/api/similar-workflow",{method:"PUT",body:JSON.stringify(window.__sim.wf)})}catch(e){}}
function __simOpen(){var m=document.getElementById("similarMask");if(m)m.hidden=false;__simResetProgress();__simLoad().then(function(wf){document.getElementById("smTol").value=(wf.detectParams&&wf.detectParams.tolerance!=null)?wf.detectParams.tolerance:70;document.getElementById("smYear").checked=!!(wf.detectParams&&wf.detectParams.stripYear);document.getElementById("smSym").checked=!!(wf.detectParams&&wf.detectParams.stripSymbols);__simRender();y("/api/similar-execute-progress").then(function(p){if(p&&!p.finished){window.__sim.execBusy=!0;__simPollJob()}}).catch(function(_){});y("/api/similar-detect-progress").then(function(p){if(p&&!p.finished){window.__sim.detectBusy=!0;__simDetectPoll()}}).catch(function(_){})})}
function __simClose(){var m=document.getElementById("similarMask");if(m)m.hidden=true}
function __simRender(){var wf=window.__sim.wf,left=document.getElementById("smLeft");
if(!wf||!wf.groups||!wf.groups.length){left.innerHTML='<div class="sm-empty">暂无检测结果。设置参数后点击「检测」。</div>';window.__sim.openGroup=null;window.__sim.openBook=null;document.getElementById("smProc").innerHTML='<div class="sm-empty">选择左侧组或书籍以设置处理方式</div>';document.getElementById("smInfo").innerHTML='<div class="sm-empty">点击书籍查看元数据与章节</div>';return}
__simRenderGroups();if(window.__sim.openGroup){var still=wf.groups.some(function(g){return g.id===window.__sim.openGroup&&!(wf.executed&&wf.executed.indexOf(g.id)>=0)});if(still)__simSelectGroup(window.__sim.openGroup,!0);else{window.__sim.openGroup=null;window.__sim.openBook=null;document.getElementById("smProc").innerHTML='<div class="sm-empty">选择左侧组或书籍以设置处理方式</div>';document.getElementById("smInfo").innerHTML='<div class="sm-empty">点击书籍查看元数据与章节</div>'}}}
function __simRenderGroups(){var wf=window.__sim.wf,left=document.getElementById("smLeft"),vis=wf.groups.filter(function(g){return!(wf.executed&&wf.executed.indexOf(g.id)>=0)});
if(!vis.length){left.innerHTML='<div class="sm-empty">没有待处理的重复组（已执行的组已自动跳过，可重新「检测」获取最新结果）。</div>';window.__sim.openGroup=null;window.__sim.openBook=null;document.getElementById("smProc").innerHTML='<div class="sm-empty">选择左侧组或书籍以设置处理方式</div>';document.getElementById("smInfo").innerHTML='<div class="sm-empty">点击书籍查看元数据与章节</div>';return}
var h='<div class="sm-groups">';
vis.forEach(function(g,gi){
h+='<div class="sm-group'+(window.__sim.openGroup===g.id?" active":"")+'" data-g="'+g.id+'">';
h+='<div class="sm-ghead" data-g="'+g.id+'">组'+(gi+1)+' <span class="sm-gcount">('+g.books.length+')</span></div><div class="sm-books">';
g.books.forEach(function(b,bi){h+='<div class="sm-book'+(window.__sim.openBook===b.id?" active":"")+'" data-g="'+g.id+'" data-b="'+b.id+'" data-i="'+bi+'"><span class="sm-bidx">'+(bi+1)+'.</span> <span class="sm-btitle">'+__esc(b.title||"(无名)")+'</span></div>'});
h+='</div></div>'});h+='</div>';left.innerHTML=h;
left.querySelectorAll(".sm-ghead").forEach(function(el){el.onclick=function(){__simSelectGroup(el.getAttribute("data-g"))}});
left.querySelectorAll(".sm-book").forEach(function(el){el.onclick=function(){__simSelectBook(el.getAttribute("data-g"),el.getAttribute("data-b"),el.getAttribute("data-i"))}})}
function __simSelectGroup(gid,silent){window.__sim.openGroup=gid;window.__sim.openBook=null;
document.querySelectorAll(".sm-group").forEach(function(e){e.classList.toggle("active",e.getAttribute("data-g")===gid)});
document.querySelectorAll(".sm-book").forEach(function(e){e.classList.remove("active")});
__simRenderProc(gid);document.getElementById("smInfo").innerHTML='<div class="sm-empty">点击书籍查看元数据与章节</div>'}
function __simSelectBook(gid,bid){window.__sim.openGroup=gid;window.__sim.openBook=bid;
document.querySelectorAll(".sm-book").forEach(function(e){e.classList.toggle("active",e.getAttribute("data-b")===bid)});
document.querySelectorAll(".sm-group").forEach(function(e){e.classList.toggle("active",e.getAttribute("data-g")===gid)});
__simRenderProc(gid);__simRenderInfo(bid)}
function __simRenderProc(gid){var wf=window.__sim.wf,g=null;for(var i=0;i<wf.groups.length;i++)if(wf.groups[i].id===gid){g=wf.groups[i];break}if(!g)return;
var st=wf.settings[gid]||{method:"none",keepIds:[],metaCopies:[]},h="";
h+='<div class="sm-proc-head">组处理设置（组内有 '+g.books.length+' 本）</div>';
h+='<div class="sm-row"><label>处理方式</label><select id="smMethod"><option value="none"'+(st.method==="none"?" selected":"")+'>不处理</option><option value="collect"'+(st.method==="collect"?" selected":"")+'>创建合集</option><option value="delete"'+(st.method==="delete"?" selected":"")+'>删除重复</option></select></div>';
if(st.method==="delete"){h+='<div class="sm-row"><label>保留（勾选的保留，未勾选的删除）</label></div><div class="sm-keep">';g.books.forEach(function(b,bi){var chk=st.keepIds.indexOf(b.id)>=0;h+='<label class="sm-keep-item"><input type="checkbox" data-keep="'+b.id+'"'+(chk?" checked":"")+'> '+(bi+1)+'. '+__esc(b.title)+'</label>'});h+='</div>'}
if(st.method==="collect"){h+='<div class="sm-row"><label>合集名称</label><input type="text" id="smCollName" value="'+__esc(st.collectName||g.books[0].title||"相似合集")+'"></div>'}
h+='<div class="sm-copy-title">元数据拷贝</div><div class="sm-copy-row"><select id="smCopyFrom">';
g.books.forEach(function(b,bi){h+='<option value="'+bi+'">'+(bi+1)+'. '+__esc(b.title)+'</option>'});
h+='</select><select id="smCopyField"><option value="cover">封面</option><option value="name">名称</option><option value="description">简介</option><option value="author">作者</option><option value="category">类型</option><option value="tags">标签</option></select><span>→</span><select id="smCopyTo">';
g.books.forEach(function(b,bi){h+='<option value="'+bi+'">'+(bi+1)+'. '+__esc(b.title)+'</option>'});
h+='</select><button class="sm-btn sm-small" id="smAddCopy">添加</button><button class="sm-btn sm-small" id="smClearCopy">清空</button></div>';
h+='<div class="sm-copy-list" id="smCopyList">';
(st.metaCopies||[]).forEach(function(mc,mi){h+='<div class="sm-copy-item">'+__esc((mc.from+1)+"→"+(mc.to+1)+" ["+__fieldName(mc.field)+"]")+' <span class="sm-copy-del" data-mi="'+mi+'">✕</span></div>'});
h+='</div>';var proc=document.getElementById("smProc");proc.innerHTML=h;
proc.querySelector("#smMethod").onchange=function(){st.method=this.value;if(st.method==="delete"&&(!st.keepIds||!st.keepIds.length))st.keepIds=g.books.map(function(b){return b.id});if(st.method!=="delete")st.keepIds=[];wf.settings[gid]=st;__simRenderProc(gid);__simSave()};
if(st.method==="delete")proc.querySelectorAll("[data-keep]").forEach(function(cb){cb.onchange=function(){var id=cb.getAttribute("data-keep");if(cb.checked){if(st.keepIds.indexOf(id)<0)st.keepIds.push(id)}else st.keepIds=st.keepIds.filter(function(x){return x!==id});wf.settings[gid]=st;__simSave()}});
if(st.method==="collect")proc.querySelector("#smCollName").oninput=function(){st.collectName=this.value;wf.settings[gid]=st;__simSave()};
proc.querySelector("#smAddCopy").onclick=function(){var from=parseInt(proc.querySelector("#smCopyFrom").value,10),to=parseInt(proc.querySelector("#smCopyTo").value,10),field=proc.querySelector("#smCopyField").value;st.metaCopies=st.metaCopies||[];st.metaCopies.push({from:from,to:to,field:field});wf.settings[gid]=st;__simRenderProc(gid);__simSave()};
proc.querySelector("#smClearCopy").onclick=function(){st.metaCopies=[];wf.settings[gid]=st;__simRenderProc(gid);__simSave()};
proc.querySelectorAll(".sm-copy-del").forEach(function(el){el.onclick=function(){var mi=parseInt(el.getAttribute("data-mi"),10);st.metaCopies.splice(mi,1);wf.settings[gid]=st;__simRenderProc(gid);__simSave()}})}
async function __simRenderInfo(bid){var info=document.getElementById("smInfo");info.innerHTML='<div class="sm-empty">加载中…</div>';
try{var b=await y("/api/books/"+encodeURIComponent(bid));
var cov=b.coverUrl?('<img class="sm-cover" src="'+__esc(b.coverUrl)+'" alt="封面">'):'<div class="sm-cover sm-cover-none">无封面</div>';
var h='<div class="sm-info-head">'+__esc(b.title||"(无名)")+'</div>';
h+='<div class="sm-info-main">'+cov+'<div class="sm-meta">';
h+='<div class="sm-line"><span class="sm-k">作者</span><span class="sm-v">'+__esc(b.author||"-")+'</span></div>';
h+='<div class="sm-line"><span class="sm-k">类型</span><span class="sm-v">'+__esc(b.category||"-")+'</span></div>';
h+='<div class="sm-line"><span class="sm-k">标签</span><span class="sm-v">'+__esc((b.tags||[]).join("、")||"-")+'</span></div>';
h+='</div></div>';
h+='<div class="sm-desc"><span class="sm-k">简介</span>'+__esc(b.description||"（无简介）")+'</div>';
var ch=b.chapters||[],tot=(b.chapterCount&&b.chapterCount!==ch.length)?(" / 共"+b.chapterCount+" 章"):"";
h+='<div class="sm-ch-title">章节（'+ch.length+tot+'）</div><div class="sm-chapters">';
(ch.slice(0,200)).forEach(function(c,i){h+='<div class="sm-ch">'+__esc(c.title||("第"+(i+1)+"章"))+'</div>'});h+='</div>';
info.innerHTML=h}catch(e){info.innerHTML='<div class="sm-empty">加载失败：'+(e&&e.message||e)+'</div>'}}
async function __simDetectRun(){if(window.__sim.detectBusy)return;var wf=window.__sim.wf,tol=parseInt(document.getElementById("smTol").value,10);if(isNaN(tol))tol=70;
var sy=document.getElementById("smYear").checked,ss=document.getElementById("smSym").checked,msg=document.getElementById("smMsg");
if(wf&&wf.groups&&wf.groups.length){if(!confirm("重新检测将覆盖现有缓存的工作流（"+wf.groups.length+" 组），确定继续？"))return}
window.__sim.detectBusy=!0;msg.textContent="检测中…";__simShowProgress(0,0,"读取书库…",!0);
try{await y("/api/similar-detect",{method:"POST",body:JSON.stringify({tolerance:tol,stripYear:sy,stripSymbols:ss})});__simDetectPoll()}catch(e){window.__sim.detectBusy=!1;__simResetProgress();msg.textContent="检测失败："+(e&&e.message||e)}}
function __simDetectPoll(){if(window.__sim._dpoll)clearInterval(window.__sim._dpoll);window.__sim._dpoll=setInterval(async function(){try{var p=await y("/api/similar-detect-progress");if(!p){clearInterval(window.__sim._dpoll);window.__sim.detectBusy=!1;__simResetProgress();return}__simShowProgress(p.done||0,p.total||0,(p.phase||"检测中")+(p.total?(" "+(p.done||0)+"/"+p.total):""),!0,!1);if(p.finished){clearInterval(window.__sim._dpoll);window.__sim.detectBusy=!1;__simResetProgress();var msg=document.getElementById("smMsg");if(p.error){msg.textContent="检测失败："+p.error;return}await __simLoad();window.__sim.openGroup=null;window.__sim.openBook=null;__simRender();msg.textContent="检测到 "+(p.count||((window.__sim.wf&&window.__sim.wf.groups)||[]).length)+" 组相似有声书";u("检测完成")}}catch(e){clearInterval(window.__sim._dpoll);window.__sim.detectBusy=!1;__simResetProgress();document.getElementById("smMsg").textContent="检测失败："+(e&&e.message||e)}},500)}
function __simPreview(){var wf=window.__sim.wf;if(!wf||!wf.groups||!wf.groups.length){u("没有可预览的工作流");return}
var lines=[],any=!1;
wf.groups.forEach(function(g,gi){if(wf.executed&&wf.executed.indexOf(g.id)>=0)return;var st=wf.settings[g.id]||{method:"none"};if(st.method==="none")return;any=!0;
var det=[];if(st.method==="collect")det.push("创建合集："+(st.collectName||g.books[0].title));
if(st.method==="delete"){var keep=st.keepIds||[],del=g.books.filter(function(b){return keep.indexOf(b.id)<0});det.push("删除 "+del.length+" 本："+del.map(function(b){return b.title}).join("、"));det.push("保留 "+keep.length+" 本")}
(st.metaCopies||[]).forEach(function(mc){det.push("拷贝 "+__fieldName(mc.field)+"："+g.books[mc.from].title+" → "+g.books[mc.to].title)});
lines.push("【组"+(gi+1)+"】"+__methodName(st.method)+"\n  - "+det.join("\n  - "))});
if(!any){u("没有需要执行的组（全部为“不处理”或已执行）");return}__simShowPreview(lines.join("\n\n"))}
function __simShowPreview(txt){var ov=document.getElementById("smPreviewBox");if(!ov)return;
ov.innerHTML='<div class="similar-preview"><div class="sp-head">工作流预览（按组，已执行的组已跳过）</div><pre class="sp-text">'+__esc(txt)+'</pre><div class="sp-foot"><button class="sm-btn" id="spClose">关闭</button></div></div>';
ov.hidden=false;ov.querySelector("#spClose").onclick=function(){ov.hidden=true};ov.addEventListener("click",function(e){if(e.target===ov)ov.hidden=true})}
async function __simExec(){var wf=window.__sim.wf;if(window.__sim.execBusy){u("执行进行中，请稍候");return}if(!wf||!wf.groups||!wf.groups.length){u("没有可执行的组");return}
var total=0;wf.groups.forEach(function(g){if(wf.executed&&wf.executed.indexOf(g.id)>=0)return;var st=wf.settings[g.id]||{method:"none"};if(st.method==="none")return;
if(st.method==="delete"){var keep=st.keepIds||[];total+=g.books.filter(function(b){return keep.indexOf(b.id)<0}).length}else total+=1;if(st.metaCopies&&st.metaCopies.length)total+=st.metaCopies.length});
if(total===0){u("没有需要执行的组（全部为“不处理”或已执行）");return}
if(total>10){if(!confirm("当前有 "+total+" 条重复记录需要处理，立即执行么？\n也可以点击「安全退出」，待服务器空闲时再处理。"))return}
window.__sim.execBusy=!0;__simShowProgress(0,total,"准备中…",!0);
try{var r=await y("/api/similar-execute",{method:"POST"});__simPollJob()}catch(e){window.__sim.execBusy=!1;__simShowProgress(0,total,"执行请求失败："+(e&&e.message||e),!1,!0);alert("执行失败："+(e&&e.message||e))}}
function __simPollJob(){if(window.__sim._poll)clearInterval(window.__sim._poll);window.__sim._poll=setInterval(async function(){try{var p=await y("/api/similar-execute-progress");if(!p){clearInterval(window.__sim._poll);window.__sim.execBusy=!1;return}__simShowProgress(p.done||0,p.total||0,(p.current||"")+(p.currentGroup?("（第"+p.currentGroup+"/"+(p.totalGroups||0)+"组）"):""),!0,p.finished);if(p.finished){clearInterval(window.__sim._poll);window.__sim.execBusy=!1;if(Array.isArray(p.executed))window.__sim.wf.executed=p.executed;await __simSave();var nd=(p.done||0)-(p.errors?p.errors.length:0),ne=(p.errors&&p.errors.length)||0;var msg="执行完成：成功 "+nd+" 组"+(ne?("，失败 "+ne+" 组"):"");if(ne&&p.errors)msg+="\n"+p.errors.map(function(x){return (x.group||"")+"："+(x.error||"")}).join("\n");__simShowProgress(p.done||0,p.total||0,msg,!1);__simRender();u("执行完成")}}catch(e){clearInterval(window.__sim._poll);window.__sim.execBusy=!1;__simShowProgress(0,0,"进度获取失败："+(e&&e.message||e),!1,!0)}},800)}
function __simShowProgress(done,total,text,running,error){var box=document.getElementById("smProgress"),fill=document.getElementById("smProgressFill"),txt=document.getElementById("smProgressText"),btn=document.getElementById("smExec");if(!box)return;box.hidden=false;if(total>0){var pct=Math.min(100,Math.round(done/total*100));if(fill)fill.style.width=pct+"%";if(txt)txt.textContent=text+"  "+done+"/"+total+" ("+pct+"%)"}else{if(txt)txt.textContent=text}if(btn){btn.disabled=!!running;btn.textContent=running?"执行中…":"执行工作流"}if(error&&box)box.classList.add("err");else if(box)box.classList.remove("err")}
function __simResetProgress(){var box=document.getElementById("smProgress"),btn=document.getElementById("smExec");if(box)box.hidden=true;if(btn){btn.disabled=!1;btn.textContent="执行工作流"}}
__similarInit();
'''
frontend_anchor = '    js = rep(js, jg3d_old, jg3d_new, "JG3d")'
assert frontend_anchor in s, "前端锚点缺失"
frontend_inject = (
    frontend_anchor + NL +
    "    sim_js_new = r'''" + SIM_JS + "'''\n" +
    '    js = rep(js, "function __syncPlayBtns(){", sim_js_new + "function __syncPlayBtns(){", "SIM-JS")' + NL
)
s = s.replace(frontend_anchor, frontend_inject)

# ============ 3) 前端 HTML：顶部栏加「相似检测」按钮（新增 html rep 行，锚定 HG4b 之后） ============
SIM_BTN_OLD = '        <button id="btnRescan" class="btn btn-ghost" title="重新扫描本地目录">重新扫描</button>\n'
SIM_BTN_NEW = SIM_BTN_OLD + '\n        <button id="btnSimilarDetect" class="btn btn-ghost" title="相似有声书检测">相似检测</button>'
html_anchor = '    html = rep(html, hg4b_old, hg4b_new, "HG4b")'
assert html_anchor in s, "HTML 注入锚点缺失"

# 静态 modal：注入到 <script> 之前，确保 bundle 同步执行时 modal 已在 DOM
html = rep(html, '  <script src="static/js/app.bundle', '    <div class="edit-overlay" id="similarMask" hidden>\n      <div class="similar-modal">\n        <div class="sm-head"><h3>相似有声书检测</h3><button class="sm-x" id="smClose" title="关闭">✕</button></div>\n        <div class="sm-config"><label>相似度容差 <input type="number" id="smTol" min="0" max="100" value="70"></label><label><input type="checkbox" id="smYear"> 去年份</label><label><input type="checkbox" id="smSym"> 去符号</label><button class="sm-btn primary" id="smDetect">检测</button><span class="sm-msg" id="smMsg"></span></div>\n        <div class="sm-body"><div class="sm-left" id="smLeft"><div class="sm-empty">点击「检测」开始扫描相似有声书</div></div><div class="sm-right"><div class="sm-proc" id="smProc"><div class="sm-empty">选择左侧组或书籍以设置处理方式</div></div><div class="sm-info" id="smInfo"><div class="sm-empty">点击书籍查看元数据与章节</div></div></div></div>\n        <div class="sm-progress" id="smProgress" hidden><div class="sm-progress-bar"><div class="sm-progress-fill" id="smProgressFill"></div></div><div class="sm-progress-text" id="smProgressText"></div></div>\n        <div class="sm-foot"><button class="sm-btn" id="smPreview">预览工作流</button><button class="sm-btn primary" id="smExec">执行工作流</button><button class="sm-btn" id="smExit" title="已做缓存处理，不会丢失工作流数据">安全退出</button></div>\n      </div>\n    </div>\n    <div class="edit-overlay" id="smPreviewBox" hidden></div>\n  <script src="static/js/app.bundle', "SIM-MODAL")
s = s.replace(html_anchor, html_anchor + '\n    html = rep(html, sim_btn_old, sim_btn_new, "SIM-BTN")')
s = s.replace('    html = rep(html, sim_btn_old, sim_btn_new, "SIM-BTN")',
              '    html = rep(html, ' + repr(SIM_BTN_OLD) + ', ' + repr(SIM_BTN_NEW) + ', "SIM-BTN")')

# ============ 4) 前端 CSS（追加到章节搜索样式块末尾，作为 raw 三引号字符串字面量） ============
SIM_CSS_RAW = r'''\n.similar-mask{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:9000;padding:16px}
.similar-mask[hidden]{display:none}
.similar-modal{background:var(--surface,#fff);color:var(--text,#222);border-radius:12px;width:min(1080px,96vw);max-height:92vh;display:flex;flex-direction:column;box-shadow:0 12px 48px rgba(0,0,0,.35);overflow:hidden}
.sm-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border,#eee)}
.sm-head h3{margin:0;font-size:16px}
.sm-x{border:none;background:none;font-size:18px;cursor:pointer;color:var(--text-3,#888);line-height:1}
.sm-config{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--border,#eee);font-size:13px}
.sm-config input[type=number]{width:64px}
.sm-msg{color:var(--primary,#3b82f6);font-size:13px}
.sm-body{display:flex;flex:1;min-height:0}
.sm-left{flex:1;min-width:240px;max-width:360px;overflow:auto;border-right:1px solid var(--border,#eee);padding:10px}
.sm-right{flex:1;display:flex;flex-direction:column;min-width:0}
.sm-proc{flex:1;overflow:auto;padding:12px;border-bottom:1px solid var(--border,#eee)}
.sm-info{flex:1;overflow:auto;padding:12px;background:var(--surface-2,#fafafa)}
.sm-groups{display:flex;flex-direction:column;gap:8px}
.sm-group{border:1px solid var(--border,#eee);border-radius:8px;overflow:hidden}
.sm-group.active{border-color:var(--primary,#3b82f6)}
.sm-group.done{opacity:.6}
.sm-ghead{background:var(--surface-2,#f5f5f5);padding:8px 10px;font-weight:600;cursor:pointer;font-size:13px}
.sm-gcount{color:var(--text-3,#888);font-weight:400}
.sm-books{padding:4px 0}
.sm-book{display:flex;gap:6px;padding:5px 10px;cursor:pointer;font-size:13px;align-items:baseline}
.sm-book:hover{background:var(--surface-2,#f5f5f5)}
.sm-book.active{background:rgba(59,130,246,.12);color:var(--primary,#3b82f6)}
.sm-bidx{color:var(--text-3,#999);min-width:18px}
.sm-btitle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sm-proc-head{font-weight:600;margin-bottom:8px;font-size:13px}
.sm-row{display:flex;align-items:center;gap:8px;margin:6px 0;flex-wrap:wrap}
.sm-row label{font-size:13px}
.sm-keep{display:flex;flex-direction:column;gap:4px;margin:4px 0}
.sm-keep-item{font-size:13px;display:flex;align-items:center;gap:6px}
.sm-copy-title{font-weight:600;margin:12px 0 6px;font-size:13px;border-top:1px dashed var(--border,#eee);padding-top:8px}
.sm-copy-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.sm-copy-row select{max-width:160px}
.sm-copy-list{margin-top:8px;display:flex;flex-direction:column;gap:4px}
.sm-copy-item{font-size:12px;background:var(--surface-2,#f5f5f5);padding:4px 8px;border-radius:6px;display:flex;justify-content:space-between;gap:8px}
.sm-copy-del{cursor:pointer;color:var(--danger,#e5484d)}
.sm-info-head{font-size:15px;font-weight:600;margin-bottom:8px;word-break:break-word}
.sm-info-main{display:flex;gap:12px;align-items:flex-start;margin-bottom:8px}
.sm-cover{width:90px;height:auto;min-height:120px;object-fit:cover;border-radius:8px;flex:none;background:var(--surface-2,#f0f0f0);display:block}
.sm-cover-none{width:90px;height:120px;flex:none;display:flex;align-items:center;justify-content:center;color:var(--text-3,#999);font-size:12px;border:1px dashed var(--border,#ddd);border-radius:8px;background:var(--surface-2,#f5f5f5)}
.sm-meta{font-size:13px;line-height:1.7;margin-bottom:6px;flex:1;min-width:0}
.sm-line{line-height:1.9;display:flex;gap:6px}
.sm-k{color:var(--text-3,#888);flex:none;min-width:36px}
.sm-v{word-break:break-word}
.sm-desc{font-size:13px;line-height:1.6;margin-bottom:8px;white-space:pre-wrap;word-break:break-word}
.sm-ch-title{font-weight:600;font-size:13px;margin:6px 0}
.sm-chapters{display:flex;flex-direction:column;gap:2px;max-height:240px;overflow:auto}
.sm-ch{font-size:12px;color:var(--text-3,#666);padding:2px 0;border-bottom:1px solid var(--border,#f0f0f0)}
.sm-foot{display:flex;gap:10px;justify-content:flex-end;padding:10px 16px;border-top:1px solid var(--border,#eee)}
.sm-progress{padding:8px 16px 0}
.sm-progress-bar{height:8px;background:var(--border,#eee);border-radius:4px;overflow:hidden}
.sm-progress-fill{height:100%;width:0;background:#3a8ee6;transition:width .3s}
.sm-progress.err .sm-progress-fill{background:#e53935}
.sm-progress-text{font-size:12px;color:var(--text-secondary,#666);margin-top:4px;white-space:pre-wrap;word-break:break-word}
#smCollName{width:320px;max-width:70%}
.sm-btn{border:1px solid var(--border,#ddd);background:none;color:var(--text,#222);border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
.sm-btn.primary{background:var(--primary,#3b82f6);border-color:var(--primary,#3b82f6);color:#fff}
.sm-btn.sm-small{padding:4px 10px;font-size:12px}
.sm-empty{color:var(--text-3,#999);font-size:13px;padding:8px 0}
.similar-preview-mask{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:9500;padding:16px}
.similar-preview-mask[hidden]{display:none}
.similar-preview{background:var(--surface,#fff);color:var(--text,#222);border-radius:12px;width:min(640px,94vw);max-height:84vh;display:flex;flex-direction:column;padding:16px;box-shadow:0 12px 48px rgba(0,0,0,.35)}
.sp-head{font-weight:600;margin-bottom:10px}
.sp-text{font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;overflow:auto;flex:1}
.sp-foot{display:flex;justify-content:flex-end;margin-top:10px}'''
css_anchor = '        ".chapter-search button{width:30px;height:30px}\\n"'
assert css_anchor in s, "CSS 注入锚点缺失"
sim_css_lit = "r'''" + SIM_CSS_RAW + "'''"
s = s.replace(css_anchor, css_anchor + " + " + sim_css_lit)

# ============ 5) 版本号升到 1.3.54（主线功能，按规则必须升版本） ============
VER_OLD = 'PLUGIN_VERSION = os.environ.get("PLUGIN_VERSION") or "1.3.60"'
VER_NEW = 'PLUGIN_VERSION = os.environ.get("PLUGIN_VERSION") or "1.3.61"'
assert VER_OLD in s, "PLUGIN_VERSION 锚点缺失"
s = s.replace(VER_OLD, VER_NEW)

io.open(P, "w", encoding="utf-8", newline="").write(s)
print("OK: 相似检测补丁已写入 build.py（变量模式，版本 1.3.54）")
