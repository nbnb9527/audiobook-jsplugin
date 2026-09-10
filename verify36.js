(async()=>{
const base='http://192.168.1.69:58090';
const lr=await fetch(base+'/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'admin'})});
const tok=(await lr.json()).access_token;
const h={Authorization:'Bearer '+tok};
const api=p=>base+'/api/v1/jsplugin/audiobook/api'+p;
let r=await fetch(api('/rescan'),{method:'POST',headers:{...h,'Content-Type':'application/json'},body:JSON.stringify({force:1})});
console.log('rescan:', JSON.stringify(await r.json()).slice(0,120));
let started=false;
for(let i=0;i<90;i++){
  await new Promise(z=>setTimeout(z,20000));
  let sp=(await(await fetch(api('/scan-progress'),{headers:h})).json()).data;
  if(sp.scanning)started=true;
  if(started&&!sp.scanning){
    console.log('scan done. books='+sp.books,'dirs='+sp.dirs,'fastHit='+sp.fastHit,'fastMiss='+sp.fastMiss,'reused='+sp.reused,'err='+sp.lastError);
    break;
  }
  if(i%3===0)console.log('['+i+'] scanning='+sp.scanning,'root='+sp.rootDone+'/'+sp.rootTotal,'books='+sp.books,'dirs='+sp.dirs,'current='+sp.currentDir);
}
const q=(await(await fetch(api('/books?keyword=test&pageSize=20'),{headers:h})).json()).data;
console.log('keyword=test:', q.books.map(b=>b.title+(b.id.indexOf('__misc__')>=0?'[MISC]':'')).join(' | '));
const s=(await(await fetch(api('/snapshot'),{headers:h})).json()).data;
console.log('snapshot: v'+s.version,'totalBooks='+(s.totalBooks||s.bookCount||JSON.stringify(s).length));
})().catch(e=>{console.error('ERR',e.message);process.exit(1)})
