(async()=>{
const base='http://192.168.1.69:58090';
const tok=(await(await fetch(base+'/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'admin'})})).json()).access_token;
const h={Authorization:'Bearer '+tok};
const api=p=>base+'/api/v1/jsplugin/audiobook/api'+p;
await new Promise(z=>setTimeout(z,3000));
const r=await fetch(api('/rescan'),{method:'POST',headers:{...h,'Content-Type':'application/json'},body:JSON.stringify({})});
console.log('rescan:',JSON.stringify(await r.json()).slice(0,100));
let started=false;
for(let i=0;i<28;i++){
  await new Promise(z=>setTimeout(z,15000));
  let sp=(await(await fetch(api('/scan-progress'),{headers:h})).json()).data;
  if(sp.scanning)started=true;
  if(started&&!sp.scanning){
    console.log('done. rootDone='+sp.rootDone+'/'+sp.rootTotal,'books='+sp.books,'fastHit='+sp.fastHit,'fastMiss='+sp.fastMiss,'reused='+sp.reused,'probe='+sp.fastProbe,'err='+sp.lastError);
    break;
  }
  if(i%2===0)console.log('['+i+'] root='+sp.rootDone+'/'+sp.rootTotal,'books='+sp.books,'fastHit='+sp.fastHit,'fastMiss='+sp.fastMiss);
}
})().catch(e=>{console.error('ERR',e.message);process.exit(1)})
