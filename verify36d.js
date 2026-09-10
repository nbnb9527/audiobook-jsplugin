(async()=>{
const base='http://192.168.1.69:58090';
const tok=(await(await fetch(base+'/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'admin'})})).json()).access_token;
const h={Authorization:'Bearer '+tok};
const api=p=>base+'/api/v1/jsplugin/audiobook/api'+p;
const scan=async(body,tag)=>{
  await fetch(api('/rescan'),{method:'POST',headers:{...h,'Content-Type':'application/json'},body:JSON.stringify(body)});
  let started=false;
  for(let i=0;i<110;i++){
    await new Promise(z=>setTimeout(z,15000));
    let sp=(await(await fetch(api('/scan-progress'),{headers:h})).json()).data;
    if(sp.scanning)started=true;
    if(started&&!sp.scanning){
      console.log(tag+': rootDone='+sp.rootDone+'/'+sp.rootTotal,'books='+sp.books,'fastHit='+sp.fastHit,'fastMiss='+sp.fastMiss,'reused='+sp.reused,'probe='+sp.fastProbe,'err='+sp.lastError);
      return sp;
    }
  }
  console.log(tag+': TIMEOUT');
  return null;
};
await scan({force:1},'FULL');
let d=(await(await fetch(api('/debug/shards'),{headers:h})).json()).data;
console.log('shards v2:',d.shards.filter(s=>s.v===2).length+'/'+d.count,'withMisc:',d.shards.filter(s=>s.misc>0).map(s=>s.g+':'+s.misc).join(','));
await scan({},'INCR');
const q=(await(await fetch(api('/books?keyword=test&pageSize=20'),{headers:h})).json()).data;
console.log('keyword=test:',q.books.map(b=>b.title+(b.id.indexOf('__misc__')>=0?'[MISC]':'')).join(' | '));
})().catch(e=>{console.error('ERR',e.message);process.exit(1)})
