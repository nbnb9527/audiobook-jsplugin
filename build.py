# -*- coding: utf-8 -*-
"""
从官方 audiobook.jsplugin.zip 中提取 main.js 源码，修复大规模书库扫描问题并重新打包。

修复项：
  P1  递归合并数组用 o.push(...arr) 展开，QuickJS 单次调用参数上限 65534，
      单本书音频数超限即 RangeError 整本丢失 → 改为分块 apply
  P2  递归深度硬编码 6 层，深层音频扫不到 → 提升到 20 层
  P3  逐文件串行 await fs.stat，SMB 上 19 万文件极慢且易触发宿主中断
      → 改为 16 并发批量 stat
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

# Windows CI runner 控制台默认 cp1252，中文 print 会 UnicodeEncodeError。
# 强制 UTF-8 输出，同时兼容本机运行（已有 UTF-8 环境时重配置无害）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))

# 官方原包：可通过环境变量 AUDIOBOOK_SRC_ZIP 覆盖；否则优先取仓库内 vendor/ 基线，
# 再回退到本地历史绝对路径（本地开发用）。CI 走 vendor/ 基线。
def _resolve_src_zip():
    cands = []
    e = os.environ.get("AUDIOBOOK_SRC_ZIP")
    if e:
        cands.append(e)
    cands.append(os.path.join(ROOT, "vendor", "audiobook.jsplugin.zip"))
    cands.append(r"D:\workbuddy\mimusic-jsplugin-releases-audiobook\releases\audiobook.jsplugin.zip")
    for c in cands:
        if c and os.path.exists(c):
            return c
    return cands[0]

SRC_ZIP = _resolve_src_zip()


# jsc 编译器（QuickJS 字节码编译器）：可通过环境变量 JSC_EXE 覆盖；否则按
# vendor/ -> 仓库内 node_modules（CI 用 npm i 安装）-> 本地历史绝对路径 的顺序查找。
def _resolve_jsc():
    cands = []
    e = os.environ.get("JSC_EXE")
    if e:
        cands.append(e)
    cands.append(os.path.join(ROOT, "vendor", "jsc.exe"))
    cands.append(os.path.join(ROOT, "node_modules", "@songloft", "jsc-win32-x64", "bin", "jsc.exe"))
    cands.append(r"C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules\@songloft\jsc-win32-x64\bin\jsc.exe")
    for c in cands:
        if c and os.path.exists(c):
            return c
    return cands[0]


BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
# 插件版本：同时写入 plugin.json 和 JS 源码里硬编码的 ot 常量（快照接口会返回它）。
# 可被环境变量 PLUGIN_VERSION 覆盖（CI 打 tag 时传入 tag 名，使产物版本与 tag 一致）。
PLUGIN_VERSION = os.environ.get("PLUGIN_VERSION") or "1.3.18"

# ---------- 1. 从 main.jsc 提取完整 main.js 源码 ----------
def extract_source(zf: zipfile.ZipFile) -> str:
    raw = zf.read("main.jsc")
    anchor = raw.find(b"async function J(")
    assert anchor > 0, "找不到源码锚点"
    ok = lambda b: 32 <= b < 127 or b in (9, 10, 13)
    s = anchor
    while s > 0 and ok(raw[s - 1]):
        s -= 1
    e = anchor
    while e < len(raw) and ok(raw[e]):
        e += 1
    src = raw[s:e].decode("utf-8")
    assert src.startswith("()=>{"), "源码开头异常: %r" % src[:20]
    assert src.endswith("}"), "源码结尾异常: %r" % src[-20:]
    return src


# ---------- 2. 打补丁 ----------
def patch(src: str) -> str:
    # P1: 分块 push，避免展开参数超 65534
    p1_old = "o.push(...l.audios)"
    p1_new = ("(function(__A,__O){for(let __k=0;__k<__A.length;__k+=8192)"
              "__O.push.apply(__O,__A.slice(__k,__k+8192))})(l.audios,o)")
    assert src.count(p1_old) == 1, "P1 锚点数量异常"
    src = src.replace(p1_old, p1_new)

    # P2: 递归深度 6 -> 20；内置忽略名单（废纸篓目录 + 群晖缩略图目录）；
    #     @eaDir 是 Synology 自动生成的缩略图/索引目录，几乎存在于每个文件夹下。
    #     若不忽略，扫描器会把「含 @eaDir 的书目录」误判为「有子目录」而走未分类合集
    #     分支（__U），导致整库书籍变成 xxx-未分类、封面/简介/分类全丢失。
    #     __SAFE：宿主拒绝任何含 ".." 的路径（防穿越，子串匹配），
    #     名字里带连续点的文件/文件夹会被整条拒掉，扫描端提前剪枝避免告警
    p2_old = 'var M="/app/audiobook",lt=6;'
    p2_new = ('var M="/app/audiobook",lt=20,'
              '__IGN=new Set(["_ARCHIVE_TRASH","_DEDUPE_TRASH","@eaDir",'
              '"@SynologyResource","#recycle","@sharebin"]),'
              '__SAFE=function(s){return typeof s=="string"&&s.indexOf("..")<0};')
    assert src.count(p2_old) == 1, "P2 锚点数量异常"
    src = src.replace(p2_old, p2_new)

    # P3: 串行 stat -> 16 并发批量 stat
    head = "for(let p=0;p<o.audios.length;p++){let d=o.audios[p];"
    start = src.find(head)
    assert start > 0, "P3 起始锚点未找到"
    tail = "if(e.length===0)return null;"
    end = src.find(tail, start)
    assert end > start, "P3 结束锚点未找到"
    p3_new = (
        "for(let p=0;p<o.audios.length;p+=16){"
        "let __G=o.audios.slice(p,p+16),"
        "__R=await Promise.all(__G.map(async __d=>{"
        "try{let S=await songloft.fs.stat(__d);"
        "return{d:__d,I:Number(S.size||0),B:Number(S.modTime||0)}}"
        "catch(S){songloft.log.warn(`stat \\u5931\\u8D25: ${__d} (${String(S)})`);return null}"
        "}));"
        "for(let __x=0;__x<__R.length;__x++){"
        "let __v=__R[__x];if(!__v)continue;"
        "let __q=p+__x,I=__v.I,B=__v.B;"
        "B>r&&(r=B),a+=I;"
        "let st=__v.d.substring(__v.d.lastIndexOf(\"/\")+1);"
        "e.push({id:`${R(__K||t)}-ch${String(__q+1).padStart(3,\"0\")}`,"
        "index:__q+1,title:O(st),duration:F(I),fileSize:I,"
        "fileRelPath:__v.d,modTime:B})}}"
    )
    src = src[:start] + p3_new + src[end:]

    # P4: 支持 /app/audiobook/.scanignore，跳过指定目录（应对超大素材库）。
    #     名单合并进内置 __IGN（P2 定义的废纸篓目录），而不是覆盖。
    p4_old = "let o=[],e=[];for(let r of n)r.isDir?o.push(r.name):A(r.name)&&e.push(r.name);"
    assert src.count(p4_old) == 1, "P4 锚点数量异常"
    p4_new = (
        "try{let __ig=await songloft.fs.readFile(s+\"/.scanignore\");"
        "if(__ig)for(let __l of __ig.split(/\\r?\\n/)){"
        "__l=__l.trim();if(__l&&__l.charAt(0)!==\"#\")__IGN.add(__l)}}catch(__e){}"
        "if(__IGN.size>2)songloft.log.info(`\\u6709\\u58F0\\u4E66\\uFF1A\\u5FFD\\u7565 "
        "${__IGN.size} \\u4E2A\\u76EE\\u5F55 (.scanignore+\\u5185\\u7F6E)`);"
        "let o=[],e=[];"
        "for(let r of n){if(!__SAFE(r.name))continue;"
        "if(__IGN.has(r.name))continue;"
        "r.isDir?o.push(r.name):A(r.name)&&e.push(r.name)}"
    )
    src = src.replace(p4_old, p4_new)

    # ---- P5: 嵌套书库模式 v2（默认启用，按子文件夹成书）----
    # 用户规则：
    #   目录无子目录且直接含音频 -> 该目录成书（叶子书）
    #   目录有子目录 -> 下钻；其散落音频归入 "<目录名>-未分类" 一本书
    #   纯容器目录 -> 继续下钻（Justing 多层素材库按叶子目录成书）
    # 散落音频书：id 基于相对路径（多本未分类互不冲突），章节 16 并发 stat。
    a5 = "async function gt(s,t){"
    assert src.count(a5) == 1, "P5a 锚点数量异常"
    src = src.replace(a5, "async function gt(s,t,__K){")

    a5b = "book:{id:R(t),title:t,"
    assert src.count(a5b) == 1, "P5b 锚点数量异常"
    src = src.replace(a5b, "book:{id:R(__K||t),title:t,")

    a5c = ("for(let r of o)try{let a=await gt(s,r);"
           "a&&a.chapters.length>0&&(t.books.push(a.book),"
           "t.chaptersByBookId[a.book.id]=a.chapters)}"
           "catch(a){songloft.log.warn(`\\u626B\\u63CF\\u6587\\u4EF6\\u5939 '${r}' "
           "\\u5931\\u8D25: ${String(a)}`)}")
    assert src.count(a5c) == 1, "P5c 锚点数量异常"
    p5c_new = (
        # __U: 把目录根下的散落音频构造成 "<目录名>-未分类" 书
        "async function __U(s,rel,auds,t){if(auds.length===0)return;"
        "let dir=s+\"/\"+rel;auds.sort((a,b)=>$(a,b));"
        "let cs=[],o=0,e=0;"
        "for(let a=0;a<auds.length;a+=16){"
        "let __G=auds.slice(a,a+16),"
        "__R=await Promise.all(__G.map(async __f=>{"
        "try{let S=await songloft.fs.stat(dir+\"/\"+__f);"
        "return{f:__f,I:Number(S.size||0),B:Number(S.modTime||0)}}"
        "catch(S){songloft.log.warn(`stat \\u5931\\u8D25: ${dir}/${__f} (${String(S)})`);return null}"
        "}));"
        "for(let __x=0;__x<__R.length;__x++){"
        "let __v=__R[__x];if(!__v)continue;"
        "let __q=a+__x,I=__v.I,B=__v.B;"
        "B>o&&(o=B),e+=I;"
        "cs.push({id:`${R(rel)}-u${String(__q+1).padStart(3,\"0\")}`,index:__q+1,"
        "title:O(__v.f),duration:F(I),fileSize:I,fileRelPath:dir+\"/\"+__v.f,modTime:B})}}"
        "if(cs.length===0)return;"
        "let __p=rel.lastIndexOf(\"/\"),nm=__p<0?rel:rel.substring(__p+1);"
        "let bk={id:R(rel+\"/__misc__\"),title:nm+\"-\\u672A\\u5206\\u7C7B\","
        "author:\"\\u672A\\u77E5\",coverUrl:null,coverRatio:\"\","
        "description:nm+\" \\u76EE\\u5F55\\u4E0B\\u7684\\u96F6\\u6563\\u97F3\\u9891\\u6587\\u4EF6\","
        "category:\"\\u672A\\u5206\\u7C7B\",tags:[],updatedAt:o||Date.now(),"
        "chapterCount:cs.length,totalSize:e,folderRelPath:dir};"
        "t.books.push(bk),t.chaptersByBookId[bk.id]=cs}"
        # __D: 递归判定书籍
        "async function __D(s,rel,t,IG,d){if(d>20)return;"
        "let dir=rel?s+\"/\"+rel:s,es;"
        "try{es=await songloft.fs.readdir(dir)||[]}catch(e){return}"
        "let subs=[],auds=[];"
        "for(let x of es){if(!__SAFE(x.name))continue;"
        "if(x.isDir){if(IG.has(x.name))continue;subs.push(x.name)}"
        "else A(x.name)&&auds.push(x.name)}"
        "if(subs.length===0){"
        "if(auds.length===0)return;"
        "let p=rel.lastIndexOf(\"/\"),"
        "pa=p<0?s:s+\"/\"+rel.substring(0,p),nm=p<0?rel:rel.substring(p+1);"
        "try{let a=await gt(pa,nm,rel);"
        "a&&a.chapters.length>0&&(t.books.push(a.book),"
        "t.chaptersByBookId[a.book.id]=a.chapters)}"
        "catch(e){songloft.log.warn(`\\u626B\\u63CF\\u6587\\u4EF6\\u5939 '${rel}' "
        "\\u5931\\u8D25: ${String(e)}`)}return}"
        "for(let x of subs)await __D(s,rel+\"/\"+x,t,IG,d+1);"
        "await __U(s,rel,auds,t)}"
        # 顶层：所有目录统一走 __D（.scandeep 机制废除，默认生效）
        "for(let r of o)try{await __D(s,r,t,__IGN,0)}"
        "catch(a){songloft.log.warn(`\\u626B\\u63CF\\u6587\\u4EF6\\u5939 '${r}' "
        "\\u5931\\u8D25: ${String(a)}`)}"
    )
    src = src.replace(a5c, p5c_new)

    # ---- P6: 单本书内部也跳过忽略目录 ----
    # gt -> J 递归收集音频时，遇到 _ARCHIVE_TRASH/_DEDUPE_TRASH/.scanignore 命中的
    # 子目录直接剪枝，否则书内嵌套废纸篓仍会被算进章节。
    # P7: 名字含 ".." 的条目一并剪枝（宿主 fs 会整条拒绝，且该文件本就无法播放）
    a6 = "for(let i of a){let g=`${s}/${i.name}`;if(i.isDir){"
    assert src.count(a6) == 1, "P6 锚点数量异常"
    a6_new = ("for(let i of a){let g=`${s}/${i.name}`;"
              "if(!__SAFE(i.name))continue;"
              "if(i.isDir&&__IGN.has(i.name))continue;if(i.isDir){")
    src = src.replace(a6, a6_new)

    # P7b: 根目录散落音频（未分类）统计时同样剪枝
    a7 = "for(let a=0;a<t.length;a++){let i=`${s}/${t[a]}`;try{let g=await songloft.fs.stat(i),"
    assert src.count(a7) == 1, "P7b 锚点数量异常"
    a7_new = ("for(let a=0;a<t.length;a++){if(!__SAFE(t[a]))continue;"
              "let i=`${s}/${t[a]}`;try{let g=await songloft.fs.stat(i),")
    src = src.replace(a7, a7_new)
    return src


# ---------- 2.5 P8: 后端 UI 支持补丁（别名 / 显示偏好 / 播放速度记忆） ----------
# 设计约束：全部走追加式存储（settings = audiobook_settings_v1），
# 不写 metadata.json、不改书库索引 audiobook_library_v1 —— 重扫/已扫记录完全不受影响。
# book.id = R(相对路径) 跨重扫稳定，别名按 id 覆盖不会失效。
def patch_ui(src: str) -> str:
    # P8a: settings 初始增加三个字段
    p8a_old = "this.settings={favorites:[],recentlyPlayed:[]};"
    p8a_new = ('this.settings={favorites:[],recentlyPlayed:[],'
               'uiPrefs:{viewDesktop:"large",viewMobile:"large"},'
               'titleOverrides:{},playbackRates:{}};')
    assert src.count(p8a_old) == 1, "P8a 锚点数量异常"
    src = src.replace(p8a_old, p8a_new)

    # P8b: list() 返回时应用别名（titleOverrides 优先于扫描标题）
    p8b_old = "return{total:g,books:a.slice(l,l+o),page:n,pageSize:o}"
    p8b_new = ("return{total:g,books:a.slice(l,l+o)"
               ".map(__b=>({...__b,title:this.settings.titleOverrides[__b.id]||__b.title})),"
               "page:n,pageSize:o}")
    assert src.count(p8b_old) == 1, "P8b 锚点数量异常"
    src = src.replace(p8b_old, p8b_new)

    # P8c: getBookById 附带覆盖后 title + originalTitle（详情页/编辑弹窗用）
    p8c_old = ("getBookById(t){return Y({books:this.books,"
               "chaptersByBookId:this.chaptersByBookId},t)}")
    p8c_new = ("getBookById(t){let n=Y({books:this.books,"
               "chaptersByBookId:this.chaptersByBookId},t);"
               "return n?{...n,title:this.settings.titleOverrides[n.id]||n.title,"
               "originalTitle:n.title}:n}")
    assert src.count(p8c_old) == 1, "P8c 锚点数量异常"
    src = src.replace(p8c_old, p8c_new)

    # P8d: metadata GET 响应补充 originalTitle + folderRelPath（只读展示）
    p8d_old = ('f({success:!0,data:{title:r.title,description:r.description,'
               'category:r.category,tags:r.tags,author:r.author,'
               'coverRatio:r.coverRatio||"",coverUrl:await v(r.coverUrl)}})')
    p8d_new = ('f({success:!0,data:{title:r.title,originalTitle:r.originalTitle||r.title,'
               'folderRelPath:r.folderRelPath||null,description:r.description,'
               'category:r.category,tags:r.tags,author:r.author,'
               'coverRatio:r.coverRatio||"",coverUrl:await v(r.coverUrl)}})')
    assert src.count(p8d_old) == 1, "P8d 锚点数量异常"
    src = src.replace(p8d_old, p8d_new)

    # P8e: 新路由 —— ui-prefs 读写显示偏好 / books/:id/title 别名 / books/:id/rate 播放速度
    #       别名空串=清除覆盖（回落原名）；速度合法范围 (0,5]，非法即删除记忆
    p8e_old = ('s.get("/api/categories",async()=>f({success:!0,'
               'data:{categories:t.getCategories(),tags:t.getTags()}})),')
    p8e_new = p8e_old + (
        's.get("/api/ui-prefs",async()=>f({success:!0,data:'
        'Object.assign({viewDesktop:"large",viewMobile:"large"},t.settings.uiPrefs||{})})),'
        's.put("/api/ui-prefs",async o=>{'
        'let e=typeof o.body=="string"?JSON.parse(o.body):o.body||{};'
        't.settings.uiPrefs=Object.assign({viewDesktop:"large",viewMobile:"large"},'
        't.settings.uiPrefs||{},e),await t.saveSettings();'
        'return f({success:!0,data:t.settings.uiPrefs})}),'
        's.post("/api/books/:id/title",async(o,e)=>{'
        'let r=typeof o.body=="string"?JSON.parse(o.body):o.body||{},'
        'a=String(r.title==null?"":r.title).trim(),n=t.getBookById(e.id);'
        'if(!n)return h("\\u672A\\u627E\\u5230\\u8BE5\\u4E66\\u7C4D",404);'
        't.settings.titleOverrides||(t.settings.titleOverrides={});'
        'a?t.settings.titleOverrides[e.id]=a:delete t.settings.titleOverrides[e.id],'
        'await t.saveSettings();'
        'return f({success:!0,data:{id:e.id,title:a||n.originalTitle||n.title,'
        'originalTitle:n.originalTitle||n.title}})}),'
        's.get("/api/books/:id/rate",async(o,e)=>f({success:!0,data:{id:e.id,'
        'rate:(t.settings.playbackRates||{})[e.id]||1}})),'
        's.post("/api/books/:id/rate",async(o,e)=>{'
        'let r=typeof o.body=="string"?JSON.parse(o.body):o.body||{},a=Number(r.rate);'
        't.settings.playbackRates||(t.settings.playbackRates={});'
        'a>0&&a<=5?t.settings.playbackRates[e.id]=a:delete t.settings.playbackRates[e.id],'
        'await t.saveSettings();'
        'return f({success:!0,data:{id:e.id,rate:a>0&&a<=5?a:1}})}),'
        's.post("/api/books/:id/clear-progress",async(o,e)=>{'
        'let r=0,a=Object.keys(t.progress);'
        'for(let i=0;i<a.length;i++)if(a[i].startsWith(e.id+"::")){delete t.progress[a[i]];r++}'
        'try{await songloft.storage.set(X,t.progress)}catch(i){}'
        'return f({success:!0,data:{id:e.id,cleared:r}})}),')
    assert src.count(p8e_old) == 1, "P8e 锚点数量异常"
    src = src.replace(p8e_old, p8e_new)

    # P8f: settings 恢复改为深合并 —— 旧版本(v1.2.0-)存量的 settings 没有
    #       uiPrefs/titleOverrides/playbackRates 三个字段，浅合并({...this.settings,...t})
    #       会用旧对象整体覆盖，导致新字段丢失；__MS 保证三个键必然存在且类型正确。
    p8f_old = ('t&&typeof t=="object"?this.settings={...this.settings,...t}:'
               'typeof t=="string"&&(this.settings={...this.settings,...JSON.parse(t)})')
    p8f_new = ('t&&typeof t=="object"?this.settings=__MS(this.settings,t):'
               'typeof t=="string"&&(this.settings=__MS(this.settings,JSON.parse(t)))')
    assert src.count(p8f_old) == 1, "P8f 锚点数量异常"
    src = src.replace(p8f_old, p8f_new)
    ms_helper = ('function __MS(a,b){b=b||{};'
                 'return Object.assign({},a,b,{'
                 'uiPrefs:Object.assign({viewDesktop:"large",viewMobile:"large"},'
                 'a&&a.uiPrefs,b.uiPrefs),'
                 'titleOverrides:Object.assign({},a&&a.titleOverrides,b.titleOverrides),'
                 'playbackRates:Object.assign({},a&&a.playbackRates,b.playbackRates)})}'
                 'var K="audiobook_settings_v1"')
    assert src.count('var K="audiobook_settings_v1"') == 1, "P8f helper 锚点数量异常"
    src = src.replace('var K="audiobook_settings_v1"', ms_helper)

    # ---- P9: 排序增强 ----
    # P9a: 关键词搜索同时匹配别名（显示什么就能搜什么）
    p9a_old = "a=a.filter(c=>c.title.toLowerCase().includes(e)||c.author.toLowerCase().includes(e)"
    p9a_new = ("a=a.filter(c=>(this.settings.titleOverrides[c.id]||c.title).toLowerCase().includes(e)"
               "||c.author.toLowerCase().includes(e)")
    assert src.count(p9a_old) == 1, "P9a 锚点数量异常"
    src = src.replace(p9a_old, p9a_new)

    # P9b: 排序支持 order=asc/desc 显式方向；按书名排序时用别名替换原名再排。
    #      不传 order 时完全沿用旧行为：title 升序 / chapterCount 降序 / updatedAt 降序
    p9b_old = ('let i=t.sortBy||"updatedAt";i==="title"?a.sort((c,u)=>'
               'c.title.localeCompare(u.title,"zh-CN")):i==="chapterCount"?'
               'a.sort((c,u)=>u.chapterCount-c.chapterCount):'
               'a.sort((c,u)=>u.updatedAt-c.updatedAt);')
    p9b_new = ('let i=t.sortBy||"updatedAt",d=t.order==="asc"?1:t.order==="desc"?-1:0,'
               '__T=c=>this.settings.titleOverrides[c.id]||c.title;'
               'i==="title"?a.sort((c,u)=>__T(c).localeCompare(__T(u),"zh-CN")*(d||1))'
               ':i==="chapterCount"?a.sort((c,u)=>(c.chapterCount-u.chapterCount)*(d||-1))'
               ':a.sort((c,u)=>(c.updatedAt-u.updatedAt)*(d||-1));')
    assert src.count(p9b_old) == 1, "P9b 锚点数量异常"
    src = src.replace(p9b_old, p9b_new)

    # P9c: /api/books 路由透传 order 参数
    p9c_old = 'sortBy:e.sortBy||"updatedAt"})'
    p9c_new = 'sortBy:e.sortBy||"updatedAt",order:e.order||""})'
    assert src.count(p9c_old) == 1, "P9c 锚点数量异常"
    src = src.replace(p9c_old, p9c_new)

    # P8g: 未分类合集（__U 生成的 "<目录名>-未分类" 书）标记 isMisc，
    #      前端隐藏删除按钮、后端路由拒绝删除（其 folderRelPath 是父分类目录，删了会误删整层）
    p8g_old = "folderRelPath:dir};"
    p8g_new = "folderRelPath:dir,isMisc:!0};"
    assert src.count(p8g_old) == 1, "P8g 锚点数量异常"
    src = src.replace(p8g_old, p8g_new)

    # P8g2: 书库根目录下的“未分类音频”书（ut 生成，folderRelPath=书库根）同样标记 isMisc
    p8g2_old = "chapterCount:n.length,totalSize:e,folderRelPath:s}"
    p8g2_new = "chapterCount:n.length,totalSize:e,folderRelPath:s,isMisc:!0}"
    assert src.count(p8g2_old) == 1, "P8g2 锚点数量异常"
    src = src.replace(p8g2_old, p8g2_new)

    # P8h: 删除整本书文件夹 + DELETE 路由 + 空目录清理接口
    #      songloft.fs 官方 API 只有 9 个方法（readFile/writeFile/appendFile/readdir/
    #      unlink/exists/mkdir/stat/rename），没有 rmdir/rm —— 旧版调 rmdir/rm 全部
    #      静默失败，导致文件删了但目录留下。v1.3.4 修复：
    #      1) 递归 unlink 所有文件；2) 自底向上删空目录：unlink(Go os.Remove 可删空目录)
    #         -> rmdir -> rm（宿主若额外提供）-> command.exec("rm -rf") 兜底；
    #      3) 不再吞错误，DELETE 响应返回 dirGone/filesFailed/dirsRemoved 等诊断信息。
    #      三重防护：路径必须在书库根内 / 不能等于书库根 / isMisc 拒绝
    if not globals().get("SKIP_P8H_HELPER", False):
        p8h_helper = (
            "async function __RM(p){let dirs=[],filesFailed=0;"
            "async function walk(d){let es=[];"
            "try{es=await songloft.fs.readdir(d)||[]}catch(_){return}"
            "dirs.push(d);"
            "for(let x of es){let c=d+\"/\"+x.name;try{"
            "if(x.isDir)await walk(c);else await songloft.fs.unlink(c)}"
            "catch(_){filesFailed++}}}"
            "await walk(p);"
            "let dirsRemoved=0;"
            "for(let i=dirs.length-1;i>=0;i--){let d=dirs[i],ok=!1;"
            "try{await songloft.fs.unlink(d),ok=!0}catch(_){}"
            "if(!ok)try{await songloft.fs.rmdir(d),ok=!0}catch(_){}"
            "if(!ok)try{await songloft.fs.rm(d,{recursive:!0,force:!0}),ok=!0}catch(_){}"
            "if(ok)dirsRemoved++}"
            "let gone=!1;"
            "try{gone=!(await songloft.fs.exists(p))}catch(_){}"
            "let fb=\"\";"
            "if(!gone)try{let r=await songloft.command.exec(\"rm\",[\"-rf\",p]);"
            "if(r&&r.exitCode===0)fb=\"rm-rf\";"
            "try{gone=!(await songloft.fs.exists(p))}catch(_){}}catch(_){}"
            "return{filesFailed:filesFailed,dirsTotal:dirs.length,"
            "dirsRemoved:dirsRemoved,dirGone:gone,fallback:fb}}\n"
            "async function __delBook(t,id){let o=t.books.find(b=>b.id===id);if(!o)return!1;"
            "let p=o.folderRelPath;if(!p||typeof p!==\"string\"||p.indexOf(M)!==0)"
            "throw new Error(\"\\u8DEF\\u5F84\\u975E\\u6CD5\\uFF0C\\u62D2\\u7EDD\\u5220\\u9664\");"
            "if(p===M||p===M+\"/\")throw new Error(\"\\u4E0D\\u80FD\\u5220\\u9664\\u4E66\\u5E93\\u6839\\u76EE\\u5F55\");"
            "if(o.isMisc||o.category===\"\\u672A\\u5206\\u7C7B\"||(o.id||\"\").indexOf(\"__misc__\")>=0)throw new Error(\"\\u672A\\u5206\\u7C7B\\u5408\\u96C6\\u4E0D\\u53EF\\u6574\\u672C\\u5220\\u9664\\uFF0C\\u8BF7\\u5728\\u6587\\u4EF6\\u7BA1\\u7406\\u5668\\u4E2D\\u624B\\u52A8\\u6E05\\u7406\");"
            "let rm=await __RM(p);"
            "t.books=t.books.filter(b=>b.id!==id);delete t.chaptersByBookId[id];"
            "t.settings.favorites=t.settings.favorites.filter(x=>x!==id);"
            "if(t.settings.titleOverrides)delete t.settings.titleOverrides[id];"
            "if(t.settings.playbackRates)delete t.settings.playbackRates[id];"
            "await t.saveSettings();try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}"
            "return rm}\n"
        )
        p8h_et = "function et(s,t){"
        assert src.count(p8h_et) == 1, "P8h et 锚点数量异常"
        src = src.replace(p8h_et, p8h_helper + p8h_et)

    if not globals().get("SKIP_P8H_ROUTE", False):
        # 锚点收窄：不含 et 自身的闭合 '}'（原 '})}' 末尾 '}' 由源码保留，
        # 避免在 route 片段里重新推导结尾括号数导致错配。
        p8h_route_old = ('s.post("/api/cache/clean",async()=>{let o=await V();'
                         'return f({success:!0,data:o})})')
        p8h_route_new = ('s.delete("/api/books/:id",async(o,e)=>{'
                         'try{let r=await __delBook(t,e.id);'
                         'if(r===!1)return h("\\u672A\\u627E\\u5230\\u8BE5\\u4E66\\u7C4D",404);'
                         'return f({success:!0,data:{id:e.id,dirGone:r.dirGone,'
                         'filesFailed:r.filesFailed,dirsRemoved:r.dirsRemoved,'
                         'dirsTotal:r.dirsTotal,fallback:r.fallback||""}})}'
                         'catch(err){return h(String(err&&err.message||err),500)}}),'
                         # 清理书库内空目录（只删真正空的目录，任何非空目录都不动，
                         # 跳过 _ARCHIVE_TRASH/_DEDUPE_TRASH 与 .scanignore 中的忽略项）
                         's.post("/api/clean-empty-dirs",async()=>{'
                         'let removed=[],failed=[];'
                         'async function walk(d,dp){if(dp>20)return;'
                         'let es=[];try{es=await songloft.fs.readdir(d)||[]}catch(_){return}'
                         'for(let x of es){if(!x.isDir)continue;'
                         'if(__IGN.has(x.name)||!__SAFE(x.name))continue;'
                         'await walk(d+"/"+x.name,dp+1)}'
                         'if(d===M)return;'
                         'let es2=[];try{es2=await songloft.fs.readdir(d)||[]}catch(_){return}'
                         'if(es2.length!==0)return;'
                         'let ok=!1;'
                         'try{await songloft.fs.unlink(d),ok=!0}catch(_){}'
                         'if(!ok)try{await songloft.fs.rmdir(d),ok=!0}catch(_){}'
                         'if(!ok)try{await songloft.fs.rm(d,{recursive:!0,force:!0}),ok=!0}catch(_){}'
                         'if(!ok)try{let r=await songloft.command.exec("rm",["-rf",d]);'
                         'ok=!!(r&&r.exitCode===0)}catch(_){}'
                         'ok?removed.push(d):failed.push(d)}'
                         'await walk(M,0);'
                         'return f({success:!0,data:{removedCount:removed.length,'
                         'removed:removed,failed:failed}})}),'
                         's.post("/api/cache/clean",async()=>{let o=await V();'
                         'return f({success:!0,data:o})}),'
                         # 只读调试：列出书库目录树（目录+各自条目数），排查残留
                         's.get("/api/debug/dirs",async()=>{'
                         'let out=[];'
                         'async function walk(d,rel,dp){if(dp>20)return;'
                         'let es=[];try{es=await songloft.fs.readdir(d)||[]}catch(_){return}'
                         'let files=es.filter(x=>!x.isDir).length,dirs=0;'
                         'for(let x of es){if(!x.isDir)continue;dirs++;'
                         'if(__IGN.has(x.name)||!__SAFE(x.name))continue;'
                         'await walk(d+"/"+x.name,rel+"/"+x.name,dp+1)}'
                         'out.push({path:rel,files:files,subdirs:dirs,entries:es.length})}'
                         'await walk(M,"",0);'
                         'return f({success:!0,data:{count:out.length,dirs:out}})})')
        assert src.count(p8h_route_old) == 1, "P8h route 锚点数量异常"
        src = src.replace(p8h_route_old, p8h_route_new)

    # P9: 最近播放清理接口
    #     DELETE /api/recently-played          清空全部记录
    #     DELETE /api/recently-played/:bookId  移除某一本书的记录
    #     recentlyPlayed 结构: [{bookId, chapterId, at}]，按 bookId 去重，最多 30 条
    if not globals().get("SKIP_P9", False):
        p9_old = 's.get("/api/recently-played",async()=>{'
        p9_new = ('s.delete("/api/recently-played",async()=>{'
                  'let o=t.settings.recentlyPlayed.length;'
                  't.settings.recentlyPlayed=[],await t.saveSettings();'
                  'return f({success:!0,data:{cleared:o}})}),'
                  's.delete("/api/recently-played/:bookId",async(o,e)=>{'
                  'let r=String(e.bookId||"");'
                  'try{r=decodeURIComponent(r)}catch(_){}'
                  'let a=t.settings.recentlyPlayed.length;'
                  't.settings.recentlyPlayed=t.settings.recentlyPlayed.filter(i=>i.bookId!==r),'
                  'await t.saveSettings();'
                  'return f({success:!0,data:{removed:a-t.settings.recentlyPlayed.length}})}),'
                  + p9_old)
        assert src.count(p9_old) == 1, "P9 锚点数量异常"
        src = src.replace(p9_old, p9_new)

    # P10: 封面搜索（编辑弹窗用）
    #   GET /api/cover-search?q=关键词  —— 宿主 curl 抓 Bing 图片搜索页，解析 murl(原图)/turl(缩略图)
    #   GET /api/cover-download?url=    —— 宿主 curl 下载原图到 .cache → fs.readFile(base64) 返回
    # 宿主 SDK 无原生 HTTP，只有 command.exec（ffmpeg 转码同款通道）；curl 不可用时回退 wget。
    # 下载走「curl -o 临时文件 → fs 读 base64」，避免二进制经 exec stdout 传输被 UTF-8 解码破坏。
    if not globals().get("SKIP_P10", False):
        p10_old = 's.post("/api/books/:id/cover"'
        __COVER_UA = ('"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"')
        p10_new = ('s.get("/api/cover-search",async o=>{'
                   'let e=k(o.query||""),q=(e.q||"").trim(),limit=Math.min(40,Math.max(6,parseInt(e.limit||"24",10)||24));'
                   'if(!q)return h("\\u7f3a\\u5c11\\u641c\\u7d22\\u5173\\u952e\\u8bcd",400);'
                   'let UA=' + __COVER_UA + ';'
                   'let u="https://www.bing.com/images/search?q="+encodeURIComponent(q)+"&form=HDRSC2&count="+limit+"&mkt=zh-CN";'
                   'let html="";'
                   'try{let r=await songloft.command.exec("curl",["-s","-L","-m","15","-A",UA,u],{timeout:2e4});'
                   'if(r&&r.exitCode===0&&r.stdout)html=r.stdout}catch(_){}'
                   'if(!html){try{let r=await songloft.command.exec("wget",["-q","-O","-","-T","15","-U",UA,u],{timeout:2e4});'
                   'if(r&&r.exitCode===0&&r.stdout)html=r.stdout}catch(_){}}'
                   'if(!html)return h("\\u5bbf\\u4e3b\\u8054\\u7f51\\u5931\\u8d25\\uff1a\\u9700\\u8981 curl \\u6216 wget",502);'
                   'let items=[],seen={},re=/m="([^"]+)"/g,m;'
                   'while((m=re.exec(html))&&items.length<limit){'
                   'let j=m[1].replace(/&quot;/g,String.fromCharCode(34)).replace(/&amp;/g,"&");'
                   'let tu=(j.match(/"turl":"([^"]+)"/)||[])[1],mu=(j.match(/"murl":"([^"]+)"/)||[])[1];'
                   'if(!mu||!tu||seen[mu])continue;seen[mu]=1;'
                   'items.push({thumb:tu,url:mu})}'
                   'return f({success:!0,data:{q:q,items:items}})}),'
                   's.get("/api/cover-download",async o=>{'
                   'let e=k(o.query||""),u=(e.url||"").trim();'
                   'if(!u||!/^https?:\\/\\//.test(u))return h("\\u65e0\\u6548\\u7684\\u56fe\\u7247\\u5730\\u5740",400);'
                   'let UA=' + __COVER_UA + ';'
                   'let tmp=".cache/dl_"+ct(u)+".tmp";'
                   'try{await songloft.fs.mkdir(".cache",{recursive:!0})}catch(_){}'
                   'let r=null,dbg="";'
                   'try{r=await songloft.command.exec("curl",["-s","-S","-L","-m","30","-A",UA,"-o",tmp,u],{timeout:35e3})}catch(e){dbg=String(e)}'
                   'if(!r||r.exitCode!==0){try{r=await songloft.command.exec("wget",["-q","-O",tmp,"-T","30","-U",UA,u],{timeout:35e3})}catch(e){dbg=String(e)}}'
                   'if(!r||r.exitCode!==0)return h("\\u5bbf\\u4e3b\\u4e0b\\u8f7d\\u5931\\u8d25\\uff1a"+(r?"exit="+r.exitCode+" "+String(r.stderr||"").slice(0,140):dbg||"\\u5bbf\\u4e3b\\u7f3a\\u5c11 curl/wget"),502);'
                   'let st=null;try{st=await songloft.fs.stat(tmp)}catch(_){}'
                   'if(!st||!Number(st.size))return h("\\u4e0b\\u8f7d\\u5185\\u5bb9\\u4e3a\\u7a7a",502);'
                   'if(Number(st.size)>15e6)return h("\\u56fe\\u7247\\u8fc7\\u5927\\uff08\\u8d85\\u8fc7 15MB\\uff09",413);'
                   'let b64="";'
                   'try{b64=await songloft.fs.readFile(tmp,{encoding:"base64"})}catch(_){return h("\\u8bfb\\u53d6\\u4e0b\\u8f7d\\u6587\\u4ef6\\u5931\\u8d25",500)}'
                   'let mime="image/jpeg";'
                   'b64.indexOf("iVBORw0KGgo")===0?mime="image/png":b64.indexOf("R0lGOD")===0?mime="image/gif":b64.indexOf("UklGR")===0&&(mime="image/webp");'
                   'return f({success:!0,data:{base64:b64,dataUrl:"data:"+mime+";base64,"+b64}})}),'
                   + p10_old)
        assert src.count(p10_old) == 1, "P10 锚点数量异常"
        src = src.replace(p10_old, p10_new)

    # P11: 短篇合并 —— 同一分类目录（父目录）下章节数 ≤ 阈值（默认4，设置可调）的书
    #   自动合并为一本虚拟合集「<父目录名>-短篇合集」：
    #   - 扁平化：子书全部音频变合集章节，标题加《子书名》前缀，按 文件夹名→文件名 自然排序
    #   - 章节 id 沿用子书派生（R(子书rel)-chNNN）→ 播放进度可双向迁移（合并/退出合集都不丢）
    #   - 合集 isMisc=true → 与「未分类合集」一样禁止整本删除
    #   - 书库根目录一级的短书不合并（避免巨无霸根合集），仅对分类目录生效
    #   - 阈值存 settings.uiPrefs.shortsMergeThreshold（设置弹窗可调，0=关闭），扫描时读全局 __SHORTS_T
    if not globals().get("SKIP_P11", False):
        # p11a: 全局阈值变量 + 合并函数（插在 library 缓存键声明之前）
        p11a_old = 'var W="audiobook_library_v1";'
        p11a_new = ('var __SHORTS_T=4;'
                    'function __SHORTS_VAL(v){return v==null?4:Number(v)||0}'
                    'function __SHORTS_MERGE(t){'
                    'var T=__SHORTS_T;if(!(T>=1))return;'
                    'var groups={},rem={};'
                    'for(var i=0;i<t.books.length;i++){'
                    'var b=t.books[i];'
                    'if(b.isMisc)continue;'
                    'if((b.chapterCount||0)>T)continue;'
                    'var p=b.folderRelPath||"",ix=p.lastIndexOf("/");'
                    'if(ix<=0)continue;'
                    'var par=p.substring(0,ix);'
                    'if(par===M)continue;'
                    '(groups[par]=groups[par]||[]).push(b)}'
                    'for(var par in groups){'
                    'var subs=groups[par];if(!subs.length)continue;'
                    'subs.sort(function(a,b){return $(a.folderRelPath,b.folderRelPath)});'
                    'var cs=[],size=0,upd=0,cover=null,cat="",tags={},names=[];'
                    'for(var j=0;j<subs.length;j++){'
                    'var b2=subs[j];rem[b2.id]=1;'
                    'var chs=(t.chaptersByBookId[b2.id]||[]).slice();'
                    'chs.sort(function(a,c){return $(a.fileRelPath,c.fileRelPath)});'
                    'for(var m=0;m<chs.length;m++){'
                    'var c2=chs[m];'
                    'cs.push({id:c2.id,index:0,title:"\\u300a"+b2.title+"\\u300b"+c2.title,duration:c2.duration,fileSize:c2.fileSize,fileRelPath:c2.fileRelPath,modTime:c2.modTime})}'
                    'if(!cover&&b2.coverUrl)cover=b2.coverUrl;'
                    'if(!cat||cat==="\\u9ed8\\u8ba4")cat=b2.category||cat;'
                    'names.push(b2.title);'
                    'size+=b2.totalSize||0;'
                    'if((b2.updatedAt||0)>upd)upd=b2.updatedAt;'
                    'var tg=b2.tags||[];for(var q=0;q<tg.length;q++)tags[tg[q]]=1}'
                    'for(var k2=0;k2<cs.length;k2++)cs[k2].index=k2+1;'
                    'var pi=par.lastIndexOf("/"),pn=par.substring(pi+1);'
                    'var mb={id:R(par+"/__shorts__"),title:pn+"-\\u77ed\\u7bc7\\u5408\\u96c6",author:"\\u5408\\u96c6",coverUrl:cover,coverRatio:"",'
                    'description:"\\u7531 "+subs.length+" \\u672c\\u77ed\\u7bc7\\u6709\\u58f0\\u4e66\\u5408\\u5e76\\uff1a"+names.join("\\u3001"),'
                    'category:cat||"\\u9ed8\\u8ba4",tags:Object.keys(tags),updatedAt:upd||Date.now(),chapterCount:cs.length,totalSize:size,folderRelPath:par,isMisc:!0};'
                    't.books.push(mb);'
                    't.chaptersByBookId[mb.id]=cs;'
                    'songloft.log.info("\\u77ed\\u7bc7\\u5408\\u5e76\\uff1a"+pn+" \\u5408\\u5e76 "+subs.length+" \\u672c / "+cs.length+" \\u7ae0")}'
                    'if(Object.keys(rem).length){'
                    'var nb=[];for(var z=0;z<t.books.length;z++)if(!rem[t.books[z].id])nb.push(t.books[z]);'
                    't.books=nb}}'
                    + p11a_old)
        assert src.count(p11a_old) == 1, "P11a 锚点数量异常"
        src = src.replace(p11a_old, p11a_new)

        # p11b: 扫描收尾时执行合并
        p11b_old = 'return t.books.sort((r,a)=>a.updatedAt-r.updatedAt),'
        p11b_new = '__SHORTS_MERGE(t);return t.books.sort((r,a)=>a.updatedAt-r.updatedAt),'
        assert src.count(p11b_old) == 1, "P11b 锚点数量异常"
        src = src.replace(p11b_old, p11b_new)

        # p11c: 初始化与重扫前从 settings 读取阈值
        p11c_old = 'this.scanInBackground()}async scanInBackground(){'
        p11c_new = ('__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold),'
                    'this.scanInBackground()}async scanInBackground(){')
        assert src.count(p11c_old) == 1, "P11c 锚点数量异常"
        src = src.replace(p11c_old, p11c_new)

        # p11d: 进度双向迁移方法（插在 rescan 前）+ rescan 读阈值
        p11d_old = 'async rescan(){++this.generation'
        p11d_new = ('async __migShorts(t){'
                    'var ids={},owner={};'
                    'for(var i=0;i<t.books.length;i++){ids[t.books[i].id]=1;'
                    'var cs=t.chaptersByBookId[t.books[i].id]||[];'
                    'for(var j=0;j<cs.length;j++)owner[cs[j].id]=t.books[i].id}'
                    'var changed=!1,keys=Object.keys(this.progress);'
                    'for(var k=0;k<keys.length;k++){'
                    'var K=keys[k],ix=K.indexOf("::");if(ix<0)continue;'
                    'var bid=K.substring(0,ix),cid=K.substring(ix+2);'
                    'if(ids[bid])continue;'
                    'var nb=owner[cid];'
                    'if(nb){this.progress[nb+"::"+cid]=this.progress[K];changed=!0}'
                    'delete this.progress[K]}'
                    'var fb=this.settings.favorites.length;'
                    'this.settings.favorites=this.settings.favorites.filter(function(x){return ids[x]});'
                    'if(changed)try{await songloft.storage.set(X,this.progress)}catch(_){}'
                    'if(this.settings.favorites.length!==fb)await this.saveSettings()}'
                    'async rescan(){__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold),++this.generation')
        assert src.count(p11d_old) == 1, "P11d 锚点数量异常"
        src = src.replace(p11d_old, p11d_new)

        # p11e: 两次扫描完成后执行迁移（scanInBackground / rescan 共用片段，各 1 处）
        p11e_old = 'this.scannedAt=Date.now(),await N(n),'
        p11e_new = 'this.scannedAt=Date.now(),await N(n),await this.__migShorts(n),'
        assert src.count(p11e_old) == 2, "P11e 锚点数量异常: %d" % src.count(p11e_old)
        src = src.replace(p11e_old, p11e_new)

    # P12: 指定目录重新扫描 —— POST /api/rescan 支持 {dir:"一级/二级"}，只扫该子树并与缓存合并
    #   （合并后照常走短篇合并 + 进度迁移）；dir 为空则全库重扫，行为与原版一致。
    if not globals().get("SKIP_P12", False):
        p12a_old = 'async rescan(){__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold),++this.generation'
        p12a_new = ('async __rescanDir(dir){'
                    'if(this.scanning)throw new Error("\\u626b\\u63cf\\u8fdb\\u884c\\u4e2d\\uff0c\\u8bf7\\u7a0d\\u540e\\u518d\\u8bd5");'
                    'dir=String(dir||"").replace(/\\\\/g,"/").replace(/^\\/+|\\/+$/g,"");'
                    'if(!dir||dir.indexOf("..")>=0)throw new Error("\\u65e0\\u6548\\u76ee\\u5f55");'
                    'var full=M+"/"+dir;'
                    'try{await songloft.fs.stat(full)}catch(e){throw new Error("\\u76ee\\u5f55\\u4e0d\\u5b58\\u5728: "+dir)}'
                    'this.scanning=!0;var g=++this.generation;'
                    'try{'
                    'var base=await G();'
                    'if(!base||!base.books||!base.books.length){this.scanning=!1;return await this.rescan()}'
                    'var t2={books:[],chaptersByBookId:{}};'
                    'await __D(M,dir,t2,__IGN,0);'
                    'if(g!==this.generation)return;'
                    'var prefix=full+"/";'
                    'var kept=base.books.filter(function(b){return !(b.folderRelPath&&b.folderRelPath.indexOf(prefix)===0)});'
                    'var keptCh={};'
                    'for(var i=0;i<kept.length;i++)keptCh[kept[i].id]=base.chaptersByBookId[kept[i].id]||[];'
                    'for(var j=0;j<t2.books.length;j++){kept.push(t2.books[j]);keptCh[t2.books[j].id]=t2.chaptersByBookId[t2.books[j].id]||[]}'
                    'var out={books:kept,chaptersByBookId:keptCh};'
                    '__SHORTS_MERGE(out);'
                    'this.books=out.books;this.chaptersByBookId=out.chaptersByBookId;this.scannedAt=Date.now();'
                    'await N(out);await this.__migShorts(out);'
                    'songloft.log.info("\\u76ee\\u5f55\\u91cd\\u626b\\u5b8c\\u6210: "+dir+" \\u5171 "+this.books.length+" \\u672c")'
                    '}finally{this.scanning=!1}}'
                    'async rescan(){__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold),++this.generation')
        assert src.count(p12a_old) == 1, "P12a 锚点数量异常"
        src = src.replace(p12a_old, p12a_new)

        p12b_old = ('s.post("/api/rescan",async()=>(t.rescan().catch(o=>songloft.log.warn(`\\u540E\\u53F0\\u91CD\\u626B\\u5F02\\u5E38: ${String(o)}`)),'
                    'f({success:!0,data:{scanning:!0}})))')
        p12b_new = ('s.post("/api/rescan",async o=>{'
                    'var d=null;try{d=typeof o.body=="string"?JSON.parse(o.body):o.body||{}}catch(_){}'
                    'if(d&&d.dir){'
                    't.__rescanDir(d.dir).then(()=>songloft.log.info("\\u76ee\\u5f55\\u91cd\\u626b\\u5b8c\\u6210: "+d.dir))'
                    '.catch(x=>songloft.log.warn("\\u76ee\\u5f55\\u91cd\\u626b\\u5f02\\u5e38: "+String(x)));'
                    'return f({success:!0,data:{scanning:!0,dir:d.dir}})}'
                    't.rescan().catch(o=>songloft.log.warn(`\\u540E\\u53F0\\u91CD\\u626B\\u5F02\\u5E38: ${String(o)}`));'
                    'return f({success:!0,data:{scanning:!0}})})')
        assert src.count(p12b_old) == 1, "P12b 锚点数量异常"
        src = src.replace(p12b_old, p12b_new)

    return src


# ---------- 2.6 前端 static UI 补丁 ----------
# 官方 zip 每次构建都会重新拷贝 static/，补丁必须在每次构建时重新应用（锚点 + assert 唯一）。
# JS 片段里的中文一律用 \uXXXX 转义（与原 bundle 风格一致，避免编码问题）；
# HTML/CSS 用 UTF-8 中文原文。
def patch_static(build_dir: str) -> None:
    import glob as _glob

    html_path = os.path.join(build_dir, "static", "index.html")
    js_paths = _glob.glob(os.path.join(build_dir, "static", "js", "app.bundle.*.js"))
    css_paths = _glob.glob(os.path.join(build_dir, "static", "css", "style.*.css"))
    assert len(js_paths) == 1, "app.bundle 文件数异常: %s" % js_paths
    assert len(css_paths) == 1, "style 文件数异常: %s" % css_paths
    js_path, css_path = js_paths[0], css_paths[0]

    def rep(text, old, new, tag):
        n = text.count(old)
        assert n == 1, "%s 锚点数量异常: %d" % (tag, n)
        return text.replace(old, new)

    # ===== index.html =====
    html = open(html_path, encoding="utf-8").read()

    # H1: sort-bar 增加 显示方式 + 顺序 两个下拉（排在排序方式之后）
    h1_old = ("          <option value=\"chapterCount\">章节数量</option>\n"
              "        </select>\n"
              "      </label>\n")
    h1_new = h1_old + (
        "      <label>\n"
        "        显示方式：\n"
        "        <select id=\"viewMode\">\n"
        "          <option value=\"large\">大图标</option>\n"
        "          <option value=\"small\">小图标</option>\n"
        "          <option value=\"list\">列表</option>\n"
        "        </select>\n"
        "      </label>\n"
        "      <label>\n"
        "        顺序：\n"
        "        <select id=\"sortOrder\">\n"
        "          <option value=\"\">默认</option>\n"
        "          <option value=\"asc\">正序</option>\n"
        "          <option value=\"desc\">逆序</option>\n"
        "        </select>\n"
        "      </label>\n")
    html = rep(html, h1_old, h1_new, "H1")

    # H2: 编辑弹窗增加 别名字段（含原名提示）+ 书籍路径（只读+复制）
    h2_old = ("        <h3>编辑书籍信息</h3>\n"
              "        <div class=\"edit-field\">\n"
              "          <label for=\"editDescription\">简介</label>\n")
    h2_new = ("        <h3>编辑书籍信息</h3>\n"
              "        <div class=\"edit-field\">\n"
              "          <label for=\"editTitle\">别名（显示用）</label>\n"
              "          <input type=\"text\" id=\"editTitle\" placeholder=\"留空则显示原名\" />\n"
              "          <div class=\"edit-title-orig\" id=\"editTitleOrig\"></div>\n"
              "        </div>\n"
              "        <div class=\"edit-field\">\n"
              "          <label>书籍路径（相对书库根目录）</label>\n"
              "          <div class=\"edit-path-row\">\n"
              "            <input type=\"text\" id=\"editBookPath\" readonly />\n"
              "            <button class=\"btn btn-ghost\" id=\"editCopyPath\" type=\"button\">复制路径</button>\n"
              "          </div>\n"
              "        </div>\n"
              "        <div class=\"edit-field\">\n"
              "          <label for=\"editDescription\">简介</label>\n")
    html = rep(html, h2_old, h2_new, "H2")

    # H4: 删除确认弹窗（醒目提示“删除后不可恢复”）
    h4_old = "    <!-- 设置弹窗 -->"
    h4_new = (
        "    <!-- 删除确认弹窗 -->\n"
        "    <div id=\"deleteOverlay\" class=\"edit-overlay\" hidden>\n"
        "      <div class=\"edit-modal delete-modal\">\n"
        "        <h3>删除有声书</h3>\n"
        "        <p class=\"delete-warn\">⚠️ 此操作将<strong>永久删除</strong>该书所在的整个文件夹及其全部音频、图片等文件，<strong>删除后不可恢复</strong>。</p>\n"
        "        <div class=\"delete-info\">\n"
        "          <div class=\"delete-row\"><span class=\"delete-label\">书名</span><b id=\"delBookTitle\"></b></div>\n"
        "          <div class=\"delete-row\"><span class=\"delete-label\">路径</span><code id=\"delBookPath\"></code></div>\n"
        "        </div>\n"
        "        <div class=\"edit-actions\">\n"
        "          <button class=\"btn btn-ghost\" id=\"delCancelBtn\" type=\"button\">取消</button>\n"
        "          <button class=\"btn btn-danger\" id=\"delConfirmBtn\" type=\"button\">确认删除</button>\n"
        "        </div>\n"
        "      </div>\n"
        "    </div>\n\n"
        "    <!-- 设置弹窗 -->")
    html = rep(html, h4_old, h4_new, "H4")

    # H3: 设置弹窗顶部增加 默认显示方式（桌面端/手机端）
    h3_old = ("        <div class=\"settings-body\" id=\"settingsBody\">\n"
              "          <div class=\"settings-section\">\n"
              "            <div class=\"settings-section-title\">转码缓存</div>\n")
    h3_new = ("        <div class=\"settings-body\" id=\"settingsBody\">\n"
              "          <div class=\"settings-section\">\n"
              "            <div class=\"settings-section-title\">默认显示方式</div>\n"
              "            <div class=\"settings-pref-row\">\n"
              "              <label>桌面端\n"
              "                <select id=\"prefViewDesktop\">\n"
              "                  <option value=\"large\">大图标</option>\n"
              "                  <option value=\"small\">小图标</option>\n"
              "                  <option value=\"list\">列表</option>\n"
              "                </select>\n"
              "              </label>\n"
              "              <label>手机端\n"
              "                <select id=\"prefViewMobile\">\n"
              "                  <option value=\"large\">大图标</option>\n"
              "                  <option value=\"small\">小图标</option>\n"
              "                  <option value=\"list\">列表</option>\n"
              "                </select>\n"
              "              </label>\n"
              "            </div>\n"
              "            <div class=\"settings-pref-desc\">修改后立即保存并全局生效。主页面“显示方式”下拉可临时覆盖当前设备的默认值。</div>\n"
              "          </div>\n"
              "          <div class=\"settings-section\">\n"
              "            <div class=\"settings-section-title\">转码缓存</div>\n")
    html = rep(html, h3_old, h3_new, "H3")

    # ===== v1.3.9 最近播放清理 + 页码跳转（HTML 结构）=====
    # H5: 「最近播放」标题后加「清空」按钮（复用已有的 .section-title-row 布局）
    h5_old = "          <h2 class=\"section-title\">最近播放</h2>"
    h5_new = ("          <div class=\"section-title-row\">\n            <h2 class=\"section-title\">最近播放</h2>\n            <button class=\"recent-clear-btn\" id=\"recentClearBtn\" type=\"button\" title=\"清空全部播放记录\">清空</button>\n          </div>")
    html = rep(html, h5_old, h5_new, "H5")

    # H6: 分页区加页码输入框 + 跳转按钮
    h6_old = "            <button id=\"nextPage\" class=\"btn btn-ghost\">下一页</button>"
    h6_new = ("            <button id=\"nextPage\" class=\"btn btn-ghost\">下一页</button>\n            <span class=\"page-jump\">\n              <input id=\"pageInput\" class=\"page-input\" type=\"number\" min=\"1\" step=\"1\" inputmode=\"numeric\" />\n              <button id=\"pageGoBtn\" class=\"btn btn-ghost\" type=\"button\">跳转</button>\n            </span>")
    html = rep(html, h6_old, h6_new, "H6")

    # H7: 通用二次确认弹窗（清空播放记录用，与删除书籍的 deleteOverlay 分开）
    h7_old = "    <!-- 设置弹窗 -->"
    h7_new = ("    <!-- 通用二次确认弹窗 -->\n    <div id=\"confirmOverlay\" class=\"edit-overlay\" hidden>\n      <div class=\"edit-modal delete-modal\">\n        <h3 id=\"confirmTitle\">确认操作</h3>\n        <p class=\"delete-warn\" id=\"confirmBody\"></p>\n        <div class=\"edit-actions\">\n          <button class=\"btn btn-ghost\" id=\"confirmCancelBtn\" type=\"button\">取消</button>\n          <button class=\"btn btn-danger\" id=\"confirmOkBtn\" type=\"button\">确认</button>\n        </div>\n      </div>\n    </div>\n\n    <!-- 设置弹窗 -->")
    html = rep(html, h7_old, h7_new, "H7")

    # H8: 把「顺序」下拉挪到「显示方式」下拉前面（排序 → 顺序 → 显示方式 → 只看收藏）
    h8_old = ("      <label>\n"
              "        显示方式：\n"
              "        <select id=\"viewMode\">\n"
              "          <option value=\"large\">大图标</option>\n"
              "          <option value=\"small\">小图标</option>\n"
              "          <option value=\"list\">列表</option>\n"
              "        </select>\n"
              "      </label>\n"
              "      <label>\n"
              "        顺序：\n"
              "        <select id=\"sortOrder\">\n"
              "          <option value=\"\">默认</option>\n"
              "          <option value=\"asc\">正序</option>\n"
              "          <option value=\"desc\">逆序</option>\n"
              "        </select>\n"
              "      </label>")
    h8_new = ("      <label>\n"
              "        顺序：\n"
              "        <select id=\"sortOrder\">\n"
              "          <option value=\"\">默认</option>\n"
              "          <option value=\"asc\">正序</option>\n"
              "          <option value=\"desc\">逆序</option>\n"
              "        </select>\n"
              "      </label>\n"
              "      <label>\n"
              "        显示方式：\n"
              "        <select id=\"viewMode\">\n"
              "          <option value=\"large\">大图标</option>\n"
              "          <option value=\"small\">小图标</option>\n"
              "          <option value=\"list\">列表</option>\n"
              "        </select>\n"
              "      </label>")
    html = rep(html, h8_old, h8_new, "H8")

    # H9: 主页面工具栏在「加载」与「设置」之间插入「刷新」按钮（只刷新列表，不重新扫描磁盘）
    h9_old = ('<button id="btnRescan" class="btn btn-ghost" title="重新扫描本地目录">加载</button>\n'
              '        <button id="btnSettings" class="btn btn-ghost" title="设置">⚙️</button>')
    h9_new = ('<button id="btnRescan" class="btn btn-ghost" title="重新扫描本地目录">加载</button>\n'
              '        <button id="btnHomeRefresh" class="btn btn-ghost" title="刷新列表">\U0001F504 刷新</button>\n'
              '        <button id="btnSettings" class="btn btn-ghost" title="设置">⚙️</button>')
    html = rep(html, h9_old, h9_new, "H9")

    # H10: 设置弹窗「关于」作者署名改为 MiMusic Team (修改：nb9527)
    h10_old = ('<div class="settings-about-row"><span class="settings-about-label">作者</span><span>MiMusic Team</span></div>')
    h10_new = ('<div class="settings-about-row"><span class="settings-about-label">作者</span><span>MiMusic Team (\u4fee\u6539\uff1anb9527)</span></div>')
    html = rep(html, h10_old, h10_new, "H10")

    # H11: 「项目主页」链接指向本仓库（nbnb9527/audiobook-jsplugin），
    #      并在其后新增「原项目主页」链接指向官方基线仓库
    h11_old = ('<a class="settings-project-link" href="https://github.com/mimusic-org/mimusic-jsplugin-releases/tree/audiobook" target="_blank" rel="noopener">\U0001F4DD 项目主页 · 提交 Issue</a>')
    h11_new = ('<a class="settings-project-link" href="https://github.com/nbnb9527/audiobook-jsplugin" target="_blank" rel="noopener">\U0001F4DD 项目主页 · 提交 Issue</a>\n'
               '            <a class="settings-project-link" href="https://github.com/mimusic-org/mimusic-jsplugin-releases/tree/audiobook" target="_blank" rel="noopener">\U0001F4E6 原项目主页</a>')
    html = rep(html, h11_old, h11_new, "H11")

    # H12: 编辑弹窗封面区增加「搜索封面」按钮 + 搜索结果面板（复用原版 URL→保存链路）
    h12_old = ('<input type="text" id="editCoverUrl" placeholder="或输入图片 URL..." />\n'
               '              <input type="file" id="editCoverFile" accept="image/*" />\n'
               '            </div>\n'
               '          </div>')
    h12_new = ('<input type="text" id="editCoverUrl" placeholder="或输入图片 URL..." />\n'
               '              <input type="file" id="editCoverFile" accept="image/*" />\n'
               '              <button type="button" id="editCoverSearchBtn" class="btn btn-ghost">\U0001F50D 搜索封面</button>\n'
               '            </div>\n'
               '          </div>\n'
               '          <div id="editCoverResults" class="edit-cover-results" hidden>\n'
               '            <div class="edit-cover-kwrow"><input type="text" id="editCoverKw" placeholder="可先修改关键词（默认取别名/文件名）再搜索" /><button type="button" id="editCoverGo" class="btn btn-ghost">搜索</button></div>\n'
               '            <div id="editCoverGrid" class="edit-cover-grid"></div>\n'
               '          </div>')
    html = rep(html, h12_old, h12_new, "H12")

    # H13: 设置弹窗新增「短篇合并」区块（阈值下拉，0=关闭，默认4）
    h13_old = ('主页面“显示方式”下拉可临时覆盖当前设备的默认值。</div>\n'
               '          </div>')
    _shorts_opts = "".join(
        '                  <option value="%d">%s</option>\n' % (
            v, ("关闭" if v == 0 else ("≤%d 章" % v))) for v in range(0, 11))
    h13_new = (h13_old +
               '          <div class="settings-section">\n'
               '            <div class="settings-section-title">短篇合并</div>\n'
               '            <div class="settings-pref-row">\n'
               '              <label>合并阈值\n'
               '                <select id="prefShortsThreshold">\n'
               + _shorts_opts +
               '                </select>\n'
               '              </label>\n'
               '            </div>\n'
               '            <div class="settings-pref-desc">同一分类目录下章节数不超过阈值的有声书将自动合并为一本「短篇合集」，可显著减少书目数量（默认 ≤4 章）。保存后自动重新扫描生效；播放进度会双向迁移，不丢失。</div>\n'
               '          </div>')
    html = rep(html, h13_old, h13_new, "H13")

    # H14: 主页「加载」按钮改名「重新扫描」（点击改为弹出扫描范围选择，不再直接全库重扫）
    h14_old = '重新扫描本地目录">加载</button>'
    h14_new = '重新扫描本地目录">重新扫描</button>'
    html = rep(html, h14_old, h14_new, "H14")
    if "点击右上角「加载」" in html:
        html = html.replace("点击右上角「加载」", "点击右上角「重新扫描」")

    # H15: 重新扫描弹窗（全部 / 指定文件夹）
    h15_old = "    <!-- 设置弹窗 -->"
    h15_new = ('    <!-- 重新扫描弹窗 -->\n'
               '    <div id="rescanOverlay" class="edit-overlay" hidden>\n'
               '      <div class="edit-modal delete-modal">\n'
               '        <h3>重新扫描</h3>\n'
               '        <p class="delete-info">选择扫描范围：可扫描整个书库，或只重新扫描某个文件夹（新增/移动文件后用它按需刷新，比全库扫描快得多）。</p>\n'
               '        <div class="edit-field">\n'
               '          <label for="rescanDirSel">扫描范围</label>\n'
               '          <select id="rescanDirSel" style="width:100%"><option value="">全部重新扫描（整个书库）</option></select>\n'
               '        </div>\n'
               '        <div class="edit-actions">\n'
               '          <button class="btn btn-ghost" id="rescanCancelBtn" type="button">取消</button>\n'
               '          <button class="btn btn-primary" id="rescanOkBtn" type="button">开始扫描</button>\n'
               '        </div>\n'
               '      </div>\n'
               '    </div>\n'
               '\n'
               '    <!-- 设置弹窗 -->')
    html = rep(html, h15_old, h15_new, "H15")

    open(html_path, "w", encoding="utf-8", newline="").write(html)
    print("  index.html: +显示方式/顺序下拉 +别名字段 +路径复制 +设置默认显示方式")

    # ===== app.bundle.*.js =====
    js = open(js_path, encoding="utf-8").read()

    # J1: 注入前端辅助函数（显示方式状态机 / 别名保存 / 播放速度记忆 / 路径复制），
    #     并让 fe() 渲染时应用 mode-* class
    j1_old = ("function fe(){let e=document.getElementById(\"bookGrid\");"
              "if(e){if(!n.books.length){e.innerHTML=\"\";return}")
    j1_new = (
        "function __isMobile(){return window.matchMedia&&window.matchMedia(\"(max-width:768px)\").matches}\n"
        "function __getViewMode(){let d=__isMobile()?\"viewMobile\":\"viewDesktop\";try{let v=localStorage.getItem(d===\"viewMobile\"?\"ab_view_mobile\":\"ab_view_desktop\");if(v===\"large\"||v===\"small\"||v===\"list\")return v}catch(_){}return n.uiPrefs&&n.uiPrefs[d]||\"large\"}\n"
        "function __setViewMode(v){let d=__isMobile()?\"mobile\":\"desktop\";try{localStorage.setItem(\"ab_view_\"+d,v)}catch(_){}}\n"
        "function __syncViewModeUI(){let s=document.getElementById(\"viewMode\");s&&(s.value=__getViewMode());let pd=document.getElementById(\"prefViewDesktop\");pd&&(pd.value=n.uiPrefs&&n.uiPrefs.viewDesktop||\"large\");let pm=document.getElementById(\"prefViewMobile\");pm&&(pm.value=n.uiPrefs&&n.uiPrefs.viewMobile||\"large\");let pt=document.getElementById(\"prefShortsThreshold\");pt&&(pt.value=String(n.uiPrefs&&n.uiPrefs.shortsMergeThreshold!=null?n.uiPrefs.shortsMergeThreshold:4))}\n"
        "async function __savePrefs(p){n.uiPrefs=Object.assign({viewDesktop:\"large\",viewMobile:\"large\"},n.uiPrefs||{},p),__syncViewModeUI(),fe();try{await y(\"/api/ui-prefs\",{method:\"PUT\",body:JSON.stringify(p),headers:{\"Content-Type\":\"application/json\"}})}catch(e){u(\"\\u4FDD\\u5B58\\u663E\\u793A\\u8BBE\\u7F6E\\u5931\\u8D25\\uFF1A\"+e.message)}}\n"
        "function __relPath(p){if(!p)return\"\";let lp=String(n.libraryPath||\"/app/audiobook\").replace(/\\/+$/,\"\");return p===lp?\"\":p.indexOf(lp+\"/\")===0?p.substring(lp.length+1):p}\n"
        "async function __saveRate(id,rate){if(!id)return;n.playbackRates=n.playbackRates||{};n.playbackRates[id]=rate;try{localStorage.setItem(\"ab_rate_\"+id,String(rate))}catch(_){}try{await y(\"/api/books/\"+id+\"/rate\",{method:\"POST\",body:JSON.stringify({rate:rate}),headers:{\"Content-Type\":\"application/json\"}})}catch(_){}}\n"
        "function __restoreRate(id){if(!id)return;let v=0;try{v=parseFloat(localStorage.getItem(\"ab_rate_\"+id))}catch(_){}if(!v||isNaN(v))v=(n.playbackRates||{})[id]||0;if(!v)v=1;if([.75,1,1.25,1.5,1.75,2].indexOf(v)<0)v=1;n.speed=v;let o=document.getElementById(\"btnSpeedFull\");o&&(o.textContent=v+\"x\");let r=n.audioEl||b();r&&(r.playbackRate=v)}\n"
        "function __initViewMode(){__syncViewModeUI();let s=document.getElementById(\"viewMode\");s&&!s.dataset.boundV&&(s.dataset.boundV=\"1\",s.addEventListener(\"change\",()=>{__setViewMode(s.value),fe()}));let pd=document.getElementById(\"prefViewDesktop\");pd&&!pd.dataset.boundV&&(pd.dataset.boundV=\"1\",pd.addEventListener(\"change\",()=>__savePrefs({viewDesktop:pd.value})));let pm=document.getElementById(\"prefViewMobile\");pm&&!pm.dataset.boundV&&(pm.dataset.boundV=\"1\",pm.addEventListener(\"change\",()=>__savePrefs({viewMobile:pm.value})));let pt=document.getElementById(\"prefShortsThreshold\");pt&&!pt.dataset.boundV&&(pt.dataset.boundV=\"1\",pt.addEventListener(\"change\",async()=>{let v=parseInt(pt.value,10)||0;await __savePrefs({shortsMergeThreshold:v});try{await y(\"/api/rescan\",{method:\"POST\"}),u(v>0?\"\\u5DF2\\u4FDD\\u5B58\\uFF0C\\u6B63\\u5728\\u91CD\\u65B0\\u626B\\u63CF\\u4EE5\\u5E94\\u7528\\u77ED\\u7BC7\\u5408\\u5E76\":\"\\u5DF2\\u5173\\u95ED\\u77ED\\u7BC7\\u5408\\u5E76\\uFF0C\\u6B63\\u5728\\u91CD\\u65B0\\u626B\\u63CF\")}catch(e){u(\"\\u91CD\\u65B0\\u626B\\u63CF\\u5931\\u8D25\\uFF1A\"+e.message)}}));let cp=document.getElementById(\"editCopyPath\");cp&&!cp.dataset.boundV&&(cp.dataset.boundV=\"1\",cp.addEventListener(\"click\",async()=>{let v=document.getElementById(\"editBookPath\").value||\"\";try{await navigator.clipboard.writeText(v),u(\"\\u5DF2\\u590D\\u5236\\u8DEF\\u5F84\")}catch(e){let i=document.getElementById(\"editBookPath\");i.focus(),i.select();try{document.execCommand(\"copy\"),u(\"\\u5DF2\\u590D\\u5236\\u8DEF\\u5F84\")}catch(_){u(\"\\u590D\\u5236\\u5931\\u8D25\\uFF0C\\u8BF7\\u624B\\u52A8\\u9009\\u62E9\\u590D\\u5236\")}}}));try{let mq=window.matchMedia(\"(max-width:768px)\"),h=()=>{__syncViewModeUI(),fe()};mq.addEventListener?mq.addEventListener(\"change\",h):mq.addListener(h)}catch(_){}}\n"
        "function fe(){let e=document.getElementById(\"bookGrid\");if(e){"
        "e.classList.remove(\"mode-small\",\"mode-list\");"
        "let __vm=__getViewMode();__vm!==\"large\"&&e.classList.add(\"mode-\"+__vm);"
        "if(!n.books.length){e.innerHTML=\"\";return}")
    js = rep(js, j1_old, j1_new, "J1")

    # J2: X() 快照加载时捎带 uiPrefs / playbackRates / libraryPath
    j2_old = "async function X(){try{if(!(await y(\"/api/snapshot\")).totalBooks){"
    j2_new = ("async function X(){try{let __snap=await y(\"/api/snapshot\");"
              "__snap&&__snap.settings&&(n.uiPrefs=Object.assign({viewDesktop:\"large\","
              "viewMobile:\"large\"},__snap.settings.uiPrefs||{}),"
              "__snap.settings.playbackRates&&(n.playbackRates=__snap.settings.playbackRates));"
              "__snap&&__snap.libraryPath&&(n.libraryPath=__snap.libraryPath);"
              "if(!__snap||!__snap.totalBooks){")
    js = rep(js, j2_old, j2_new, "J2")

    # J3: 初始化时绑定 显示方式/设置默认/复制路径 控件
    j3_old = "function Oe(){b(),De(),k(\"homeView\"),X(),Y()}"
    j3_new = "function Oe(){b(),De(),k(\"homeView\"),X(),__initViewMode(),Y()}"
    js = rep(js, j3_old, j3_new, "J3")

    # J4: 打开设置弹窗时同步两个默认显示方式下拉
    j4_old = ("function ge(){let e=document.getElementById(\"settingsOverlay\");"
              "e&&(e.hidden=!1,he(),Re())}")
    j4_new = ("function ge(){let e=document.getElementById(\"settingsOverlay\");"
              "e&&(__syncViewModeUI(),e.hidden=!1,he(),Re())}")
    js = rep(js, j4_old, j4_new, "J4")

    # J5: 编辑弹窗打开时填充别名 / 原名提示 / 书籍路径
    j5_old = ("function Fe(e){Q=e.id,"
              "document.getElementById(\"editDescription\").value=e.description||\"\",")
    j5_new = ("function Fe(e){Q=e.id,window.__editCur=e.title||\"\","
              "window.__editOrig=e.originalTitle||e.title||\"\","
              "document.getElementById(\"editTitle\").value=e.title||\"\","
              "document.getElementById(\"editTitleOrig\").textContent="
              "e.originalTitle?(e.originalTitle!==e.title?"
              "\"\\u522B\\u540D\\u751F\\u6548\\uFF0C\\u539F\\u540D\\uFF1A\"+e.originalTitle"
              ":\"\\u672A\\u8BBE\\u7F6E\\u522B\\u540D\\uFF0C\\u663E\\u793A\\u539F\\u540D\\uFF1A\"+e.originalTitle):\"\","
              "document.getElementById(\"editBookPath\").value=__relPath(e.folderRelPath),"
              "document.getElementById(\"editDescription\").value=e.description||\"\",")
    js = rep(js, j5_old, j5_new, "J5")

    # J6: 保存时声明 __tChanged 标记
    j6_old = ("let t=Q;if(!t)return;let o="
              "document.getElementById(\"editDescription\").value.trim(),")
    j6_new = ("let t=Q;if(!t)return;let __tChanged=!1;let o="
              "document.getElementById(\"editDescription\").value.trim(),")
    js = rep(js, j6_old, j6_new, "J6")

    # J7: 元数据保存后，若别名有变化则调用 /api/books/:id/title
    #     （与原名相同=清除覆盖；留空=清除覆盖）
    j7_old = ("await y(`/api/books/${t}/metadata`,{method:\"PUT\","
              "body:JSON.stringify({description:o,category:r,tags:c,author:i,coverRatio:s}),"
              "headers:{\"Content-Type\":\"application/json\"}});")
    j7_new = j7_old + (
        "{let __nt=document.getElementById(\"editTitle\").value.trim(),"
        "__orig=(window.__editOrig||\"\").trim();"
        "if(__nt!==window.__editCur){__tChanged=!0;"
        "let __send=__nt===__orig?\"\":__nt;"
        "await y(`/api/books/${t}/title`,{method:\"POST\","
        "body:JSON.stringify({title:__send}),"
        "headers:{\"Content-Type\":\"application/json\"}})}}")
    js = rep(js, j7_old, j7_new, "J7")

    # J8: 别名变化时保存后刷新首页列表（详情页由 R(t) 刷新）
    j8_old = "}W(),await R(t)}catch(t){"
    j8_new = "}W(),await R(t),__tChanged&&w()}catch(t){"
    js = rep(js, j8_old, j8_new, "J8")

    # J9: 打开书籍播放时恢复该书记忆的播放速度
    j9_old = ("async function B(e,t,o){L(),n.currentBookForPlayer=e,"
              "n.currentChapter=t,ke(),")
    j9_new = ("async function B(e,t,o){L(),n.currentBookForPlayer=e,"
              "__restoreRate(e.id),n.currentChapter=t,ke(),")
    js = rep(js, j9_old, j9_new, "J9")

    # J10: 切换倍速时按书保存
    j10_old = ("function se(){let e=[.75,1,1.25,1.5,1.75,2],t=e.indexOf(n.speed);"
               "n.speed=e[(t+1)%e.length];let o=document.getElementById(\"btnSpeedFull\");"
               "o&&(o.textContent=n.speed+\"x\");let r=n.audioEl||b();"
               "r&&(r.playbackRate=n.speed)}")
    j10_new = ("function se(){let e=[.75,1,1.25,1.5,1.75,2],t=e.indexOf(n.speed);"
               "n.speed=e[(t+1)%e.length];let o=document.getElementById(\"btnSpeedFull\");"
               "o&&(o.textContent=n.speed+\"x\");let r=n.audioEl||b();"
               "r&&(r.playbackRate=n.speed),"
               "__saveRate((n.currentBookForPlayer&&n.currentBookForPlayer.id)||(n.currentBook&&n.currentBook.id),n.speed)}")
    js = rep(js, j10_old, j10_new, "J10")

    # J11: w() 查询附带 order 参数
    j11_old = ("o=await y(`/api/books?page=${n.page}&pageSize=${n.pageSize}"
               "&keyword=${encodeURIComponent(n.keyword)}&sortBy=${encodeURIComponent(e)}`"
               "+(t?\"&favoritesOnly=true\":\"\"));")
    j11_new = ("__so=(document.getElementById(\"sortOrder\")||{}).value||\"\","
               "o=await y(`/api/books?page=${n.page}&pageSize=${n.pageSize}"
               "&keyword=${encodeURIComponent(n.keyword)}&sortBy=${encodeURIComponent(e)}`"
               "+(t?\"&favoritesOnly=true\":\"\")+(__so?\"&order=\"+__so:\"\"));")
    js = rep(js, j11_old, j11_new, "J11")

    # J13: sortOrder 下拉 change 事件绑定
    j13_old = ("document.getElementById(\"sortBy\").addEventListener(\"change\","
               "()=>{n.page=1,w()}),")
    j13_new = ("document.getElementById(\"sortBy\").addEventListener(\"change\","
               "()=>{n.page=1,w()}),"
               "document.getElementById(\"sortOrder\").addEventListener(\"change\","
               "()=>{n.page=1,w()}),")
    js = rep(js, j13_old, j13_new, "J13")

    # J14: 删除确认弹窗逻辑（打开/执行）
    j14_old = "async function _e(e){"
    # v1.3.10: 播放按钮“播放/暂停”切换 + 跨卡片状态同步
    # __syncPlayBtns: 根据当前播放的书籍把每张 [data-play] 卡片按钮同步为 ⏸/▶
    # __bindAudioSync: 给 audio 元素绑定 play/pause/ended/emptied 事件，状态变化时全量重绘按钮
    # __togglePlay: 点同一本已加载的书 -> ce() 切播放/暂停；点另一本 -> 走 _e() 加载并播放
    j14_new = (
        "function __syncPlayBtns(){"
        "let e=n.audioEl||document.getElementById(\"audio\");if(!e)return;"
        "let t=!e.paused&&!e.ended&&!!e.src,"
        "o=(n.currentBookForPlayer||n.currentBook||{}).id;"
        "document.querySelectorAll(\"[data-play]\").forEach(a=>{"
        "let r=t&&a.getAttribute(\"data-play\")===o;"
        "a.textContent=r?\"\\u275A\\u275A\":\"\\u25B6\","
        "a.title=r?\"\\u6682\\u505C\":\"\\u64AD\\u653E\","
        "a.classList.toggle(\"is-playing\",!!r)})}"

        "function __bindAudioSync(){let e=b();if(e.__psb)return;e.__psb=!0;"
        "[\"play\",\"pause\",\"ended\",\"emptied\"].forEach(t=>e.addEventListener(t,__syncPlayBtns))}"

        "async function __togglePlay(t){__bindAudioSync();"
        "let e=n.audioEl||document.getElementById(\"audio\"),"
        "o=(n.currentBookForPlayer||n.currentBook||{}).id;"
        "if(e&&e.src&&o===t){ce(),__syncPlayBtns();return}"
        "await _e(t),__syncPlayBtns()}"

        "async function __openDel(id){let b=n.books.find(x=>x.id===id);if(!b)return;"
        "let o=document.getElementById(\"deleteOverlay\");if(!o)return;"
        "document.getElementById(\"delBookTitle\").textContent=b.title||\"\";"
        "document.getElementById(\"delBookPath\").textContent=__relPath(b.folderRelPath)||\"(书库根目录)\";"
        "window.__delId=id;o.hidden=!1}\n"
        "async function __doDel(){let id=window.__delId;if(!id)return;"
        "let btn=document.getElementById(\"delConfirmBtn\");btn.disabled=!0,btn.textContent=\"\\u5220\\u9664\\u4E2D...\";"
        # 注意：y() 成功时返回的是响应体的 data 字段（已剥离 success 层），
        # 因此这里只能判断 success===false；写成 !r.success 会因 undefined 而永远误判失败。
        "try{let r=await y(`/api/books/${id}`,{method:\"DELETE\"})||{};"
        "if(r.success===!1)throw new Error(r.error||\"\\u5220\\u9664\\u5931\\u8D25\");"
        "let t=n.books.find(x=>x.id===id);"
        "document.getElementById(\"deleteOverlay\").hidden=!0,window.__delId=null,"
        "u(r.data&&!r.data.dirGone?"
        "\"\\u6587\\u4EF6\\u5DF2\\u5220\\u9664\\uFF0C\\u4F46\\u6587\\u4EF6\\u5939\\u672A\\u80FD\\u79FB\\u9664\\uFF0C\\u8BF7\\u624B\\u52A8\\u6E05\\u7406\""
        ":\"\\u5DF2\\u5220\\u9664\\uFF1A\"+(t?t.title:\"\")),await w()}"
        "catch(e){u(\"\\u5220\\u9664\\u5931\\u8D25\\uFF1A\"+e.message)}"
        "finally{btn.disabled=!1,btn.textContent=\"\\u786E\\u8BA4\\u5220\\u9664\"}}\n"
        "function __cardEdit(id){let b=n.books.find(x=>x.id===id);if(!b)return;"
        "window.__editFromList=!0,Fe(b)}\n"
        "async function _e(e){")
    js = rep(js, j14_old, j14_new, "J14")

    # J15: 卡片模板加编辑按钮 + 删除按钮
    #      未分类书（isMisc / category==="未分类" / id 含 __misc__）显示灰色禁用叉（disabled，点击不触发）
    j15_old = "        <button class=\"book-card-play\" data-play=\"${t.id}\" title=\"\\u64AD\\u653E\">\\u25B6</button>"
    j15_new = (j15_old + "\n"
               "        <button class=\"book-card-edit\" data-edit=\"${t.id}\" title=\"\\u7F16\\u8F91\">\\u270E</button>\n"
               "        ${(t.isMisc||t.category===\"\\u672A\\u5206\\u7C7B\"||(t.id||\"\").indexOf(\"__misc__\")>=0)"
               "?'<button class=\"book-card-del dis\" data-del=\"'+t.id+'\" title=\""
               "\\u672A\\u5206\\u7C7B\\u5408\\u96C6\\u4E0D\\u53EF\\u6574\\u672C\\u5220\\u9664\" disabled>\\u2716</button>'"
               ":'<button class=\"book-card-del\" data-del=\"'+t.id+'\" title=\"\\u5220\\u9664\">\\u2716</button>'}")
    js = rep(js, j15_old, j15_new, "J15")

    # J16: 绑定删除按钮点击事件
    # 原串 _e(...) 之后有 6 个关闭符 })})}}（}关o=>体 )关addEventListener }关t=>体 )关forEach }关外层fn1 }关fn2）
    # 兄弟 forEach 的正确收尾：前 4 个关闭符 + 逗号引出兄弟 + 新forEach(自身平衡) + 末尾 }} 补回 fn1/fn2
    j16_old = "o.stopPropagation(),_e(t.getAttribute(\"data-play\"))})})}}"
    j16_new = ("o.stopPropagation(),__togglePlay(t.getAttribute(\"data-play\"))})}),"
               "e.querySelectorAll(\"[data-del]\").forEach(t=>{t.addEventListener(\"click\",o=>{"
               "o.stopPropagation(),__openDel(t.getAttribute(\"data-del\"))})}),"
               "e.querySelectorAll(\"[data-edit]\").forEach(t=>{t.addEventListener(\"click\",o=>{"
               "o.stopPropagation(),__cardEdit(t.getAttribute(\"data-edit\"))})})"
               ",__syncPlayBtns()"
               "}}")
    js = rep(js, j16_old, j16_new, "J16")

    # J17: 绑定确认/取消按钮（在 De() 内）
    j17_old = "document.getElementById(\"settingsOverlay\").addEventListener(\"click\",a=>{a.target===a.currentTarget&&ee()}),"
    j17_new = ("document.getElementById(\"settingsOverlay\").addEventListener(\"click\",a=>{a.target===a.currentTarget&&ee()}),"
               "document.getElementById(\"delCancelBtn\").addEventListener(\"click\",()=>{"
               "document.getElementById(\"deleteOverlay\").hidden=!0,window.__delId=null}),"
               "document.getElementById(\"delConfirmBtn\").addEventListener(\"click\",__doDel),"
               "document.getElementById(\"deleteOverlay\").addEventListener(\"click\",a=>{"
               "a.target===a.currentTarget&&(a.currentTarget.hidden=!0,window.__delId=null)}),")
    js = rep(js, j17_old, j17_new, "J17")

    # J19: 从列表卡片打开编辑时（window.__editFromList），保存成功后刷新列表，
    #      而不是跳到书籍详情页（原生行为 W(),await R(t)）
    # J19: 从列表卡片打开编辑时（window.__editFromList），保存成功后只刷新列表，
    #      不跳书籍详情页。锚点是 J8 改写后的版本（含 __tChanged&&w()）。
    j19_old = ("W(),await R(t),__tChanged&&w()}catch(t){"
               "u(\"\\u4FDD\\u5B58\\u5931\\u8D25\\uFF1A\"+t.message)}")
    j19_new = ("W(),window.__editFromList?(window.__editFromList=!1,await w())"
               ": (__tChanged&&w(),await R(t))}catch(t){"
               "u(\"\\u4FDD\\u5B58\\u5931\\u8D25\\uFF1A\"+t.message)}")
    js = rep(js, j19_old, j19_new, "J19")

    # ===== v1.3.9 最近播放清理 + 页码跳转 =====
    # J20: 公共函数（注入在 Y() 之前，全部为顶层函数）
    j20_old = "async function Y(){try{let t=(await y(\"/api/recently-played\")).items||[]"
    j20_new = '''function __confirmBox(t,b,k){let o=document.getElementById("confirmOverlay");if(!o)return;document.getElementById("confirmTitle").textContent=t,document.getElementById("confirmBody").innerHTML=b,window.__confirmOk=k,o.hidden=!1}
function __closeConfirm(){let o=document.getElementById("confirmOverlay");o&&(o.hidden=!0),window.__confirmOk=null}
async function __runConfirm(){let k=window.__confirmOk;__closeConfirm();if(k)try{await k()}catch(e){u(String(e&&e.message||e))}}
function __clearRecent(){__confirmBox("\u6E05\u7A7A\u64AD\u653E\u8BB0\u5F55","\u786E\u5B9A\u8981\u6E05\u7A7A<strong>\u5168\u90E8\u6700\u8FD1\u64AD\u653E\u8BB0\u5F55</strong>\u5417\uFF1F<br>\u6B64\u64CD\u4F5C\u4E0D\u53EF\u6062\u590D\u3002",async()=>{try{await y("/api/recently-played",{method:"DELETE"}),u("\u5DF2\u6E05\u7A7A\u64AD\u653E\u8BB0\u5F55"),await Y()}catch(e){u("\u6E05\u7A7A\u5931\u8D25\uFF1A"+e.message)}})}
async function __delRecent(id){try{await y(`/api/recently-played/${encodeURIComponent(id)}`,{method:"DELETE"}),await Y()}catch(e){u("\u6E05\u9664\u5931\u8D25\uFF1A"+e.message)}}
function __goPage(){let el=document.getElementById("pageInput");if(!el)return;let tp=Math.max(1,Math.ceil(n.total/n.pageSize)),v=parseInt(el.value,10);if(!v||isNaN(v)){el.value="";return}v<1&&(v=1),v>tp&&(v=tp),el.value="",v!==n.page&&(n.page=v,w())}
async function __clearBookProgress(id){return await y(`/api/books/${id}/clear-progress`,{method:"POST"})}
async function Y(){try{let t=(await y("/api/recently-played")).items||[]'''
    js = rep(js, j20_old, j20_new, "J20")

    # J21: 最近播放卡片右上角加 ✕ 按钮
    j21_old = "        <div class=\"recent-card-info\">"
    j21_new = ("        <button class=\"recent-del\" data-recent-del=\"${a.bookId}\" title=\"\\u79FB\\u9664\\u8FD9\\u6761\\u8BB0\\u5F55\">\\u2716</button>\n        <div class=\"recent-card-info\">")
    js = rep(js, j21_old, j21_new, "J21")

    # J22: 绑定 ✕ 点击事件（stopPropagation 避免触发播放）
    j22_old = "R(a.getAttribute(\"data-book\"),!0,a.getAttribute(\"data-chapter\"))})})}catch(e){"
    j22_new = ("R(a.getAttribute(\"data-book\"),!0,a.getAttribute(\"data-chapter\"))})}),r.querySelectorAll(\"[data-recent-del]\").forEach(a=>{a.addEventListener(\"click\",async o=>{o.stopPropagation(),await __delRecent(a.getAttribute(\"data-recent-del\"))})})}catch(e){")
    js = rep(js, j22_old, j22_new, "J22")

    # J23: De() 里绑定清空/确认弹窗/跳转按钮（IIFE 隔离作用域，避免变量名冲突）
    j23_old = ("document.getElementById(\"nextPage\").addEventListener(\"click\",()=>{let a=Math.max(1,Math.ceil(n.total/n.pageSize));n.page<a&&(n.page++,w())})")
    j23_new = ("document.getElementById(\"nextPage\").addEventListener(\"click\",()=>{let a=Math.max(1,Math.ceil(n.total/n.pageSize));n.page<a&&(n.page++,w())}),function(){let a=document.getElementById(\"recentClearBtn\");a&&a.addEventListener(\"click\",__clearRecent);let b=document.getElementById(\"confirmOkBtn\");b&&b.addEventListener(\"click\",__runConfirm);let c=document.getElementById(\"confirmCancelBtn\");c&&c.addEventListener(\"click\",__closeConfirm);let d=document.getElementById(\"confirmOverlay\");d&&d.addEventListener(\"click\",e=>{e.target===d&&__closeConfirm()});let f=document.getElementById(\"pageGoBtn\");f&&f.addEventListener(\"click\",__goPage);let g=document.getElementById(\"pageInput\");g&&g.addEventListener(\"keydown\",e=>{\"Enter\"===e.key&&__goPage()})}()")
    js = rep(js, j23_old, j23_new, "J23")

    # J24: Le() 里同步页码输入框的 max 与 placeholder
    j24_old = ("document.getElementById(\"prevPage\").disabled=n.page<=1,document.getElementById(\"nextPage\").disabled=n.page>=o}")
    j24_new = ("document.getElementById(\"prevPage\").disabled=n.page<=1,document.getElementById(\"nextPage\").disabled=n.page>=o,function(){let e=document.getElementById(\"pageInput\");e&&(e.max=o,e.placeholder=n.page)}()}")
    js = rep(js, j24_old, j24_new, "J24")

    # ===== v1.3.11 详情页操作栏按钮 + 主页面刷新按钮 =====
    # J29: 全屏播放页暂停图标由 ⏸️ 改为两条竖线（与卡片/详情页保持一致）
    j29_old = 'r.textContent=e?"\\u23F8\\uFE0F":"\\u25B6\\uFE0F"'
    j29_new = 'r.textContent=e?"\\u275A\\u275A":"\\u25B6\\uFE0F"'
    js = rep(js, j29_old, j29_new, "J29")

    # J30: 详情页操作栏 - 在「从第一集播放」前插入「继续播放」，并把「从第一集播放」降为次要按钮
    j30_old = '<button class="btn btn-primary" id="btnPlayFirst">\\u25B6 \\u4ECE\\u7B2C\\u4E00\\u96C6\\u64AD\\u653E</button>'
    j30_new = ('<button class="btn btn-primary" id="btnResume">\\u25B6 \\u7EE7\\u7EED\\u64AD\\u653E</button>\n'
               '          <button class="btn btn-ghost" id="btnPlayFirst">\\u25B6 \\u4ECE\\u7B2C\\u4E00\\u96C6\\u64AD\\u653E</button>')
    js = rep(js, j30_old, j30_new, "J30")

    # J31: 详情页操作栏 - 在「收藏」前插入「暂停」与「播放速度」
    j31_old = '<button class="btn btn-ghost" id="btnToggleFav">${e.isFavorite?"\\u2605 \\u5DF2\\u6536\\u85CF":"\\u2606 \\u6536\\u85CF"}</button>'
    j31_new = ('<button class="btn btn-ghost" id="btnTogglePause">\\u275A\\u275A \\u6682\\u505C</button>\n'
               '          <button class="btn btn-ghost" id="btnSpeed">1x</button>\n'
               '          <button class="btn btn-ghost" id="btnClearProgress">\\u6E05\\u9664\\u64AD\\u653E\\u8FDB\\u5EA6</button>\n'
               '          <button class="btn btn-ghost" id="btnToggleFav">${e.isFavorite?"\\u2605 \\u5DF2\\u6536\\u85CF":"\\u2606 \\u6536\\u85CF"}</button>')
    js = rep(js, j31_old, j31_new, "J31")

    # J32a: 详情页暂停/倍速按钮的状态同步函数（追加到 cycleSpeed 之后，IIFE 顶层可用）
    j32a_old = ('function se(){let e=[.75,1,1.25,1.5,1.75,2],t=e.indexOf(n.speed);n.speed=e[(t+1)%e.length];'
                'let o=document.getElementById("btnSpeedFull");o&&(o.textContent=n.speed+"x");'
                'let r=n.audioEl||b();r&&(r.playbackRate=n.speed),__saveRate((n.currentBookForPlayer&&n.currentBookForPlayer.id)||(n.currentBook&&n.currentBook.id),n.speed)}')
    j32a_new = (j32a_old +
                'function __syncDetailPause(){let a=n.audioEl||document.getElementById("audio");'
                'let b=document.getElementById("btnTogglePause");if(!b)return;'
                'let p=!!(a&&a.src&&!a.paused&&!a.ended);'
                'b.textContent=p?"\\u275A\\u275A \\u6682\\u505C":"\\u25B6 \\u64AD\\u653E";}'
                'function __syncDetailSpeed(){let b=document.getElementById("btnSpeed");if(!b)return;'
                'let id=n.currentBook&&n.currentBook.id,v=0;'
                'if(id){try{v=parseFloat(localStorage.getItem("ab_rate_"+id))}catch(_){}'
                'if(!v||isNaN(v))v=(n.playbackRates||{})[id]||0;}'
                'if(!v||[.75,1,1.25,1.5,1.75,2].indexOf(v)<0)v=n.speed||1;'
                'b.textContent=v+"x";}')
    js = rep(js, j32a_old, j32a_new, "J32a")

    # J32b: 详情页按钮事件绑定（继续播放 / 暂停 / 倍速）
    j32b_old = 'document.getElementById("btnRefreshBook").addEventListener("click",()=>{R(e.id)}),'
    j32b_new = ('document.getElementById("btnRefreshBook").addEventListener("click",()=>{R(e.id)}),'
                '(function(){let a=b();if(a&&!a.__dpb){a.__dpb=1;'
                'a.addEventListener("play",__syncDetailPause);a.addEventListener("pause",__syncDetailPause);}'
                'let __id=e.id,__v=0;try{__v=parseFloat(localStorage.getItem("ab_rate_"+__id))}catch(_){}'
                'if(!__v||isNaN(__v))__v=(n.playbackRates||{})[__id]||0;if(!__v)__v=1;'
                'if([.75,1,1.25,1.5,1.75,2].indexOf(__v)<0)__v=1;n.speed=__v;'
                '__syncDetailPause();__syncDetailSpeed();})(),'
                'document.getElementById("btnResume").addEventListener("click",()=>{'
                'let bk=n.currentBook;if(!bk||!bk.chapters||!bk.chapters.length)return;'
                'let tg=null,mx=0;for(let i=0;i<bk.chapters.length;i++){let c=bk.chapters[i];'
                'let p=c.progress&&!c.progress.completed?(c.progress.position||0):0;'
                'if(p>mx){mx=p;tg=c;}}if(!tg)tg=bk.chapters[0];B(bk,tg,!1),__syncDetailSpeed();}),'
                'document.getElementById("btnTogglePause").addEventListener("click",()=>{ce();setTimeout(__syncDetailPause,0);}),'
                'document.getElementById("btnSpeed").addEventListener("click",()=>{se();setTimeout(__syncDetailSpeed,0);}),'
                'document.getElementById("btnClearProgress").addEventListener("click",()=>{'
                'let bk=n.currentBook;if(!bk)return;'
                '__confirmBox("\\u786E\\u8BA4\\u6E05\\u9664","\\u786E\\u5B9A\\u8981\\u6E05\\u9664<strong>"+(bk.title||bk.originalTitle||"")+"</strong>\\u7684\\u6240\\u6709\\u64AD\\u653E\\u8FDB\\u5EA6\\u5417\\uFF1F<br>\\u6B64\\u64CD\\u4F5C\\u4E0D\\u53EF\\u6062\\u590D\\u3002",async()=>{'
                'try{await __clearBookProgress(bk.id);u("\\u5DF2\\u6E05\\u9664\\u64AD\\u653E\\u8FDB\\u5EA6");await R(bk.id);}catch(err){u("\\u6E05\\u9664\\u5931\\u8D25\\uFF1A"+err.message);}});}),')
    js = rep(js, j32b_old, j32b_new, "J32b")

    # J33: 主页面「刷新」按钮（加载与设置之间）绑定到列表刷新 w()
    j33_old = 't&&t.addEventListener("click",Z),'
    j33_new = 't&&t.addEventListener("click",Z),document.getElementById("btnHomeRefresh").addEventListener("click",()=>{w()}),'
    js = rep(js, j33_old, j33_new, "J33")

    # ===== v1.3.15 封面搜索（编辑弹窗）=====
    # J38: 搜索面板逻辑。「搜索封面」打开面板并预填关键词（别名优先，其次原名），
    #      点缩略图 → 原图 URL 填入 editCoverUrl → 复用原版「URL→保存」链路。
    j38_old = ('document.getElementById("editCoverFile").addEventListener("change",e=>{'
               'let t=e.target.files[0];if(!t)return;'
               'let o=new FileReader;'
               'o.onload=r=>{document.getElementById("editCoverPreview").src=r.target.result},'
               'o.readAsDataURL(t)});')
    j38_new = (j38_old +
               'function __coverResetPanel(){let p=document.getElementById("editCoverResults");'
               'if(p){p.hidden=!0;let g=document.getElementById("editCoverGrid");g&&(g.innerHTML="")}}'
               'async function __coverDoSearch(kw){kw=(kw||"").trim();'
               'let grid=document.getElementById("editCoverGrid");if(!grid)return;'
               'if(!kw){grid.innerHTML=\'<div class="edit-cover-empty">\\u8bf7\\u8f93\\u5165\\u5173\\u952e\\u8bcd</div>\';return}'
               'grid.innerHTML=\'<div class="edit-cover-empty">\\u641c\\u7d22\\u4e2d...</div>\';'
               'try{let d=await y("/api/cover-search?q="+encodeURIComponent(kw)+"&limit=30");'
               'let its=(d&&d.items)||[];'
               'if(!its.length){grid.innerHTML=\'<div class="edit-cover-empty">\\u672a\\u627e\\u5230\\u56fe\\u7247\\uff0c\\u6362\\u4e2a\\u5173\\u952e\\u8bcd\\u8bd5\\u8bd5</div>\';return}'
               'grid.innerHTML="";'
               'its.forEach(it=>{let im=document.createElement("img");'
               'im.className="edit-cover-thumb";im.loading="lazy";im.title=it.url;'
               'im.src="data:image/gif;base64,R0lGODdhAQABAIAAAP///wAAACH5BAEAAAEALAAAAAABAAEAAAICTAEAOw==";'
               'let __ld=function(u){y("/api/cover-download?url="+encodeURIComponent(u)).then(function(d){im.src=d.dataUrl}).catch(function(){if(u!==it.url)__ld(it.url)})};'
               '__ld(it.thumb);'
               'im.addEventListener("click",()=>{'
               'document.getElementById("editCoverUrl").value=it.url;'
               'document.querySelectorAll(".edit-cover-thumb.sel").forEach(x=>x.classList.remove("sel"));'
               'im.classList.add("sel");'
               'let pv=document.getElementById("editCoverPreview");pv.src=im.src;'
               'u("\\u5df2\\u9009\\u62e9\\u5c01\\u9762\\uff0c\\u6b63\\u5728\\u52a0\\u8f7d\\u9884\\u89c8...");'
               'y("/api/cover-download?url="+encodeURIComponent(it.url)).then(function(d){pv.src=d.dataUrl;u("\\u5df2\\u9009\\u62e9\\u5c01\\u9762\\uff0c\\u70b9\\u51fb\\u4fdd\\u5b58\\u751f\\u6548")}).catch(function(){u("\\u5df2\\u9009\\u62e9\\u5c01\\u9762\\uff08\\u9884\\u89c8\\u52a0\\u8f7d\\u5931\\u8d25\\uff09\\uff0c\\u70b9\\u51fb\\u4fdd\\u5b58\\u751f\\u6548")})});'
               'grid.appendChild(im)})}catch(e){'
               'grid.innerHTML=\'<div class="edit-cover-empty">\\u641c\\u7d22\\u5931\\u8d25\\uff1a\'+(e&&e.message||e)+"</div>"}}'
               '(function(){let f=document.getElementById("editCoverFile");if(!f)return;'
               'let btn=document.getElementById("editCoverSearchBtn");'
               'btn&&!btn.dataset.bcs&&(btn.dataset.bcs="1",btn.addEventListener("click",()=>{'
               'let p=document.getElementById("editCoverResults");if(!p)return;'
               'let wasHidden=p.hidden;p.hidden=!1;'
               'let kw=document.getElementById("editCoverKw");'
               'if(!kw.dataset.b){kw.dataset.b="1";'
               'kw.addEventListener("keydown",e=>{e.key==="Enter"&&__coverDoSearch(kw.value)})}'
               'let go=document.getElementById("editCoverGo");'
               'go&&!go.dataset.b&&(go.dataset.b="1",go.addEventListener("click",()=>__coverDoSearch(kw.value)));'
               'if(wasHidden&&!kw.value){kw.value=document.getElementById("editTitle").value.trim()||window.__editOrig||"";'
               'kw.focus()}}))})();'
               'async function __openRescanModal(){'
               'let o=document.getElementById("rescanOverlay");if(!o)return;'
               'let s=document.getElementById("rescanDirSel");'
               's.innerHTML=\'<option value="">\\u5168\\u90e8\\u91cd\\u65b0\\u626b\\u63cf\\uff08\\u6574\\u4e2a\\u4e66\\u5e93\\uff09</option>\';s.value="";'
               'o.hidden=!1;'
               'try{let d=await y("/api/debug/dirs");let dirs=(d&&d.dirs)||[];let seen={};'
               'dirs.forEach(x=>{let p=(x&&x.path)||"";if(!p||seen[p])return;seen[p]=1;'
               'let depth=p.split("/").filter(Boolean).length;if(depth>2)return;'
               'let op=document.createElement("option");op.value=p;'
               'op.textContent=p+(x.files>0?"\\uff08"+x.files+" \\u4e2a\\u97f3\\u9891\\uff09":"");'
               's.appendChild(op)})}catch(e){}}'
               'async function __doRescan(dir){'
               'try{await y("/api/rescan",{method:"POST",body:JSON.stringify(dir?{dir:dir}:{})})}catch(e){u("\\u542f\\u52a8\\u626b\\u63cf\\u5931\\u8d25\\uff1a"+e.message);return}'
               'u("\\u5df2\\u5f00\\u59cb\\u91cd\\u65b0\\u626b\\u63cf"+(dir?"\\uff1a"+dir:""));'
               'let n=0,iv=setInterval(async()=>{n++;'
               'try{let s=await y("/api/snapshot");'
               'if(!s.scanning||n>200){clearInterval(iv);w();if(!s.scanning)u("\\u626b\\u63cf\\u5b8c\\u6210\\uff0c\\u5171 "+s.totalBooks+" \\u672c")}}catch(e){}},3000);}'
               '(function(){let ov=document.getElementById("rescanOverlay");if(!ov||ov.dataset.b)return;ov.dataset.b="1";'
               'document.getElementById("rescanCancelBtn").addEventListener("click",()=>{ov.hidden=!0});'
               'ov.addEventListener("click",e=>{e.target===ov.currentTarget&&(ov.hidden=!0)});'
               'document.getElementById("rescanOkBtn").addEventListener("click",()=>{let s=document.getElementById("rescanDirSel");let dir=s.value;ov.hidden=!0;dir?__doRescan(dir):Z()});})();')
    js = rep(js, j38_old, j38_new, "J38")

    # J39: 保存时 URL 下载兜底 —— 原版流程用浏览器 fetch(原图 URL)，防盗链/CORS 会失败；
    #      失败时改走后端 /api/cover-download（宿主 curl 下载 → base64），结果不变。
    j39_old = ('else if(l){let m=await fetch(l);'
               'if(!m.ok)throw new Error("\\u65E0\\u6CD5\\u4E0B\\u8F7D\\u5C01\\u9762\\u56FE\\u7247\\uFF0C\\u8BF7\\u68C0\\u67E5 URL");'
               'let p=await m.blob(),T=await new Promise((E,U)=>{let j=new FileReader;'
               'j.onload=Be=>E(Be.target.result.split(",")[1]),'
               'j.onerror=()=>U(new Error("\\u8F6C\\u6362\\u56FE\\u7247\\u5931\\u8D25")),'
               'j.readAsDataURL(p)});'
               'await y(`/api/books/${t}/cover`,{method:"POST",body:JSON.stringify({base64:T}),headers:{"Content-Type":"application/json"}})}')
    j39_new = ('else if(l){let T="";'
               'try{let m=await fetch(l);if(!m.ok)throw new Error("fetch fail");'
               'let p=await m.blob();'
               'T=await new Promise((E,U)=>{let j=new FileReader;'
               'j.onload=Be=>E(Be.target.result.split(",")[1]),'
               'j.onerror=()=>U(new Error("convert fail")),'
               'j.readAsDataURL(p)})}'
               'catch(_){let __d=await y("/api/cover-download?url="+encodeURIComponent(l));T=__d.base64}'
               'await y(`/api/books/${t}/cover`,{method:"POST",body:JSON.stringify({base64:T}),headers:{"Content-Type":"application/json"}})}')
    js = rep(js, j39_old, j39_new, "J39")

    # J40: 打开编辑弹窗时收起上次的搜索结果面板
    j40_old = ('document.getElementById("editCoverFile").value="",'
               'document.getElementById("editOverlay").hidden=!1')
    j40_new = ('document.getElementById("editCoverFile").value="",'
               '__coverResetPanel(),'
               'document.getElementById("editOverlay").hidden=!1')
    js = rep(js, j40_old, j40_new, "J40")

    # J43: 「重新扫描」按钮改为打开范围选择弹窗（Z 仍负责全库扫描）
    j43_old = 't&&t.addEventListener("click",Z),'
    j43_new = 't&&t.addEventListener("click",()=>__openRescanModal()),'
    js = rep(js, j43_old, j43_new, "J43")

    # J34: 修复播放结束自动跳章重复触发导致跳过一章的 bug
    # z() 在 ended/pause/timeupdate 三个事件下都会被调用；旧 guard O 基于 n.currentChapter.id，
    # 第一次调用后 B() 立即把 n.currentChapter 改为下一章，第二个 z() 就误判下一章也结束，从而连跳两章。
    # 修复：增加音频级 _advancing 锁，z() 进入时先检查/设置，新章节 loadedmetadata 与 B() 预加载超时时释放。
    j34_old = 'function z(){let e=n.audioEl;if(!e||!n.currentBookForPlayer||!n.currentChapter||n.miotRemote||O===n.currentChapter.id)return;'
    j34_new = 'function z(){let e=n.audioEl;if(!e||!n.currentBookForPlayer||!n.currentChapter||n.miotRemote||e._advancing||O===n.currentChapter.id)return;e._advancing=!0;'
    js = rep(js, j34_old, j34_new, "J34")

    # J35: B() 预加载超时时也要释放 _advancing 锁，否则超时后无法再次自动跳章
    j35_old = 'if(!l.data||!l.data.ready){u("\\u8F6C\\u7801\\u8D85\\u65F6\\uFF0C\\u8BF7\\u7A0D\\u540E\\u91CD\\u8BD5");return}'
    j35_new = 'if(!l.data||!l.data.ready){u("\\u8F6C\\u7801\\u8D85\\u65F6\\uFF0C\\u8BF7\\u7A0D\\u540E\\u91CD\\u8BD5");let __r=b();__r._advancing=!1;return}'
    js = rep(js, j35_old, j35_new, "J35")

    # J36: 新章节 loadedmetadata 时释放 _advancing 锁
    j36_old = 't.addEventListener("loadedmetadata",()=>{if(n.currentChapter&&isFinite(t.duration)'
    j36_new = 't.addEventListener("loadedmetadata",()=>{t._advancing=!1;if(n.currentChapter&&isFinite(t.duration)'
    js = rep(js, j36_old, j36_new, "J36")

    # J37: 从第一集播放后同步刷新详情页速度按钮（复用存储读取版 __syncDetailSpeed）
    j37_old = 'document.getElementById("btnPlayFirst").addEventListener("click",()=>{n.currentBook&&n.currentBook.chapters.length&&B(n.currentBook,n.currentBook.chapters[0],!1)}),'
    j37_new = 'document.getElementById("btnPlayFirst").addEventListener("click",()=>{n.currentBook&&n.currentBook.chapters.length&&B(n.currentBook,n.currentBook.chapters[0],!1),__syncDetailSpeed();}),'
    js = rep(js, j37_old, j37_new, "J37")

    open(js_path, "w", encoding="utf-8", newline="").write(js)
    print("  app.bundle: +显示方式/顺序/别名/路径复制/播放速度记忆 接线")

    # ===== style.*.css =====
    css = open(css_path, encoding="utf-8").read()
    css = css.rstrip() + "\n\n" + (
        "/* ===== v1.3.0 UI 补丁：显示方式 / 别名 / 书籍路径 ===== */\n"
        ".book-grid.mode-small { grid-template-columns: repeat(5, 1fr); gap: 8px; }\n"
        ".book-grid.mode-small .book-card-title { font-size: 11px; }\n"
        ".book-grid.mode-small .book-card-meta { font-size: 10px; }\n"
        "@media (max-width: 480px) {\n"
        "  .book-grid.mode-small { grid-template-columns: repeat(4, 1fr); gap: 6px; }\n"
        "}\n"
        ".book-grid.mode-list { display: flex; flex-direction: column; gap: 6px; }\n"
        ".book-grid.mode-list .book-card { border: 1px solid var(--border); }\n"
        ".book-grid.mode-list .book-card-cover {\n"
        "  aspect-ratio: auto; width: auto; height: 56px;\n"
        "  display: flex; align-items: center; gap: 10px;\n"
        "  padding: 6px 10px; background: none;\n"
        "}\n"
        ".book-grid.mode-list .book-card-cover img { width: 44px; height: 44px; border-radius: 6px; flex-shrink: 0; }\n"
        ".book-grid.mode-list .book-card-cover > div:not(.book-card-overlay) { width: 44px !important; height: 44px !important; font-size: 20px !important; border-radius: 6px; flex-shrink: 0; }\n"
        ".book-grid.mode-list .book-card-overlay { position: static; background: none; padding: 0; flex: 1; min-width: 0; display: flex; align-items: baseline; gap: 10px; }\n"
        ".book-grid.mode-list .book-card-title { color: var(--text); text-shadow: none; display: block; -webkit-line-clamp: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }\n"
        ".book-grid.mode-list .book-card-meta { color: var(--text-3); margin-top: 0; flex-shrink: 0; }\n"
        ".book-grid.mode-list .book-card-fav, .book-grid.mode-list .book-card-play { position: static; opacity: 1; flex-shrink: 0; }\n"
        ".book-grid.mode-list .book-card-fav { background: none; color: var(--text-3); }\n"
        ".book-grid.mode-list .book-card-fav.on { color: #ffd000; }\n"
        ".edit-title-orig { font-size: 12px; color: var(--text-3); margin-top: 4px; }\n"
        ".edit-path-row { display: flex; gap: 8px; }\n"
        ".edit-path-row input { flex: 1; min-width: 0; width: auto; padding: 8px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); color: var(--text-2); font-size: 12px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }\n"
        ".edit-path-row .btn { flex-shrink: 0; white-space: nowrap; }\n"
        ".settings-pref-row { display: flex; gap: 16px; flex-wrap: wrap; }\n"
        ".settings-pref-row label { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-2); cursor: pointer; }\n"
        ".settings-pref-row select { padding: 6px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg); color: var(--text); font-size: 13px; }\n"
        ".settings-pref-desc { font-size: 12px; color: var(--text-3); margin-top: 8px; line-height: 1.5; }\n"
        "/* ===== v1.3.2 删除功能 ===== */\n"
        ".book-card-del { position: absolute; right: 8px; bottom: 8px; width: 30px; height: 30px; border-radius: 50%; background: rgba(0,0,0,0.45); color: #ff5252; display: grid; place-items: center; font-size: 14px; border: none; cursor: pointer; opacity: 0; transition: opacity .2s; z-index: 2; }\n"
        ".book-card:hover .book-card-del { opacity: 1; }\n"
        ".book-card-del:hover { background: var(--danger); color: #fff; }\n"
        ".book-grid.mode-list .book-card-fav, .book-grid.mode-list .book-card-play, .book-grid.mode-list .book-card-del { position: static; opacity: 1; flex-shrink: 0; }\n"
        ".book-grid.mode-list .book-card-del { background: none; color: var(--danger); border: 1px solid var(--border); width: auto; height: auto; border-radius: 6px; padding: 2px 8px; font-size: 12px; }\n"
        ".btn-danger { background: var(--danger); color: #fff; border: none; }\n"
        ".btn-danger:hover { filter: brightness(0.92); }\n"
        ".btn-danger:disabled { opacity: 0.6; cursor: default; }\n"
        "/* ===== v1.3.5 卡片编辑按钮 + 未分类灰色禁用叉 ===== */\n"
        ".book-card-edit { position: absolute; left: 8px; bottom: 8px; width: 30px; height: 30px; border-radius: 50%; background: rgba(0,0,0,0.45); color: #fff; display: grid; place-items: center; font-size: 14px; border: none; cursor: pointer; opacity: 0; transition: opacity .2s; z-index: 2; }\n"
        ".book-card:hover .book-card-edit { opacity: 1; }\n"
        ".book-card-edit:hover { background: var(--primary); color: var(--on-primary); }\n"
        ".book-card-del.dis, .book-card-del.dis:hover { background: rgba(0,0,0,0.45); color: #9a9a9a; cursor: not-allowed; }\n"
        ".book-grid.mode-list .book-card-edit { position: static; opacity: 1; flex-shrink: 0; background: none; color: var(--text-3); border: 1px solid var(--border); width: auto; height: auto; border-radius: 6px; padding: 2px 8px; font-size: 12px; }\n"
        ".book-grid.mode-list .book-card-edit:hover { background: var(--surface-2); color: var(--text); }\n"
        ".book-grid.mode-list .book-card-del.dis, .book-grid.mode-list .book-card-del.dis:hover { background: none; color: var(--text-3); opacity: .45; cursor: not-allowed; }\n"
        "/* ===== v1.3.10 播放/暂停按钮状态高亮 ===== */\n"
        ".book-card-play.is-playing { background: var(--primary); color: var(--on-primary); }\n"
        ".book-grid:not(.mode-list) .book-card-play.is-playing { opacity: 1; }\n"
        ".delete-modal { max-width: 420px; }\n"
        ".delete-warn { font-size: 13px; line-height: 1.6; color: var(--danger); background: rgba(255,82,82,0.12); padding: 10px 12px; border-radius: 8px; margin: 8px 0 12px; }\n"
        ".delete-warn strong { font-weight: 700; }\n"
        ".delete-info { font-size: 13px; color: var(--text-2); margin-bottom: 14px; }\n"
        ".delete-row { display: flex; gap: 8px; padding: 4px 0; align-items: baseline; }\n"
        ".delete-label { color: var(--text-3); flex-shrink: 0; min-width: 36px; }\n"
        ".delete-row code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; word-break: break-all; color: var(--text); background: var(--surface-2); padding: 2px 6px; border-radius: 4px; }\n"

        "/* ===== v1.3.15 封面搜索 ===== */\n"
        ".edit-cover-results { margin-top: 8px; border: 1px solid var(--border); border-radius: 8px; padding: 8px; background: var(--surface-2); }\n"
        ".edit-cover-kwrow { display: flex; gap: 6px; margin-bottom: 8px; }\n"
        ".edit-cover-kwrow input { flex: 1; }\n"
        ".edit-cover-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(76px, 1fr)); gap: 6px; max-height: 240px; overflow-y: auto; }\n"
        ".edit-cover-thumb { width: 100%; height: 76px; object-fit: cover; border-radius: 6px; cursor: pointer; border: 2px solid transparent; background: var(--surface); }\n"
        ".edit-cover-thumb:hover { border-color: var(--primary); opacity: .9; }\n"
        ".edit-cover-thumb.sel { border-color: var(--primary); }\n"
        ".edit-cover-empty { grid-column: 1 / -1; font-size: 12px; color: var(--text-3); padding: 8px 2px; }\n"
        "/* ===== v1.3.9 最近播放清理 + 页码跳转 ===== */\n"
        ".recent-card { position: relative; }\n"
        ".recent-del { position: absolute; top: 4px; right: 4px; width: 20px; height: 20px; border-radius: 50%; border: none; background: rgba(0,0,0,0.35); color: #fff; font-size: 11px; line-height: 1; display: grid; place-items: center; cursor: pointer; opacity: 0; transition: opacity .2s; z-index: 2; }\n"
        ".recent-card:hover .recent-del { opacity: 1; }\n"
        ".recent-del:hover { background: var(--danger); color: #fff; }\n"
        ".recent-clear-btn { background: none; border: 1px solid var(--border); color: var(--text-3); border-radius: 6px; padding: 2px 10px; font-size: 12px; cursor: pointer; }\n"
        ".recent-clear-btn:hover { color: var(--danger); border-color: var(--danger); background: var(--surface-2); }\n"
        ".page-jump { display: inline-flex; align-items: center; gap: 4px; margin-left: 8px; }\n"
        ".page-input { width: 56px; padding: 4px 6px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); font-size: 12px; text-align: center; }\n"
        ".page-input:focus { outline: none; border-color: var(--primary); }\n"        "/* ===== v1.3.8 收藏按钮：网格/小卡片模式 hover 显示（官方漏写 hover 规则，只有已收藏的书才显星） ===== */\n"
        ".book-card-fav { z-index: 2; cursor: pointer; }\n"
        ".book-grid:not(.mode-list) .book-card:hover .book-card-fav { opacity: 1; color: rgba(255,255,255,.85); }\n"
        ".book-grid:not(.mode-list) .book-card:hover .book-card-fav.on { color: #ffd000; }\n"
        ".book-grid:not(.mode-list) .book-card-fav:hover { background: rgba(0,0,0,0.72); color: #ffd000; }\n"
        ".book-grid.mode-list .book-card-fav { cursor: pointer; }\n"
        ".book-grid.mode-list .book-card-fav:hover { color: #ffd000; background: var(--surface-2); }\n")
    open(css_path, "w", encoding="utf-8", newline="").write(css)
    open(css_path, "w", encoding="utf-8", newline="").write(css)
    print("  style.css: +mode-small/mode-list +别名/路径/设置行样式")


# ---------- 3. 哈希（对齐 plugin-builder/src/hash.ts） ----------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_zip_hash(build_dir: str) -> str:
    entries = []
    for dirpath, _, filenames in os.walk(build_dir):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, build_dir).replace("\\", "/")
            if rel == "plugin.json":
                continue
            entries.append((rel, sha256_hex(open(full, "rb").read())))
    entries.sort(key=lambda x: x[0])
    h = hashlib.sha256()
    for path, fh in entries:
        h.update((path + "\n" + fh + "\n").encode("utf-8"))
    return h.hexdigest()


def main():
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(BUILD)
    os.makedirs(DIST, exist_ok=True)
    zin = zipfile.ZipFile(SRC_ZIP)

    src = extract_source(zin)
    print("  提取源码: %d 字符" % len(src))
    patched = patch(src)
    patched = patch_ui(patched)
    # 同步 JS 里硬编码的版本常量（var ot="1.1.2"），否则快照接口显示旧版本
    ver_old = 'var ot="1.1.2";'
    assert patched.count(ver_old) == 1, "版本常量锚点异常"
    patched = patched.replace(ver_old, 'var ot="%s";' % PLUGIN_VERSION)
    print("  打补丁后: %d 字符 (+%d)" % (len(patched), len(patched) - len(src)))

    # main.js = esbuild IIFE 形式，自执行
    main_js = "(" + patched + ")();\n"
    main_js_bytes = main_js.encode("utf-8")
    main_js_path = os.path.join(BUILD, "main.js")
    open(main_js_path, "wb").write(main_js_bytes)

    # 额外在根目录留一份未编译源码，供 test-scan.js 使用（不进包）
    open(os.path.join(ROOT, "main.src.js"), "wb").write(main_js_bytes)
    jsc = _resolve_jsc()
    main_name = "main.js"
    if os.path.exists(jsc):
        jsc_out = os.path.join(BUILD, "main.jsc")
        r = subprocess.run([jsc, main_js_path, jsc_out], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(jsc_out):
            os.remove(main_js_path)
            main_name = "main.jsc"
            print("  编译 main.jsc: %d 字节" % os.path.getsize(jsc_out))
        else:
            print("  ⚠️ jsc 编译失败，回退为 main.js")
    else:
        print("  ⚠️ 未找到 jsc，回退为 main.js")

    # 原样拷贝 static/（随后打前端 UI 补丁）
    for name in zin.namelist():
        if name.startswith("static/") and not name.endswith("/"):
            target = os.path.join(BUILD, name.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            open(target, "wb").write(zin.read(name))
    print("  拷贝 static/ 资源")
    patch_static(BUILD)

    # plugin.json
    manifest = json.loads(zin.read("plugin.json"))
    manifest["main"] = main_name
    manifest["version"] = PLUGIN_VERSION
    # 作者署名：原始作者 + 修改者
    manifest["author"] = "MiMusic Team (修改：nb9527)"
    # 项目主页：指向本修改版仓库（原版 homepage 指向 mimusic-org 官方基线仓库）
    manifest["homepage"] = "https://github.com/nbnb9527/audiobook-jsplugin"
    manifest["description"] = (
        "本地有声书管理与播放（修复版）：修复单本音频数超 65534 时扫描崩溃、"
        "递归深度不足 6 层、大书库扫描过慢的问题。"
        "按子文件夹成书：无子目录且直接含音频的目录为一本书；"
        "有子目录的目录只做分类，其散落音频归入\"目录名-未分类\"；"
        "支持书库根目录 .scanignore 忽略名单；"
        "_ARCHIVE_TRASH/_DEDUPE_TRASH 废纸篓目录任何层级一律跳过；"
        "自动跳过名字含 .. 的文件/文件夹（宿主拒绝访问且无法播放）。"
        "UI 增强：首页显示方式切换（大图标/小图标/列表，桌面端与手机端可分别设默认值）、"
        "排序正序/逆序切换（按书名排序时别名参与排序与搜索）、"
        "书籍别名（仅显示层覆盖，不改动扫描记录与原文件）、"
        "编辑弹窗展示书籍相对路径并支持一键复制、"
        "按书记忆播放速度、"
        "列表/网格模式删除整本有声书（二次确认并醒目提示“删除后不可恢复”，"
        "仅删除专属于该书的文件夹，未分类合集与书库根目录受保护不可删）。"
    )
    # 去掉自动更新，避免被官方版本覆盖回退
    manifest.pop("updateUrl", None)
    manifest.pop("download_url", None)

    entry_bytes = open(os.path.join(BUILD, main_name), "rb").read()
    entry_hash = sha256_hex(entry_bytes)
    zip_hash = canonical_zip_hash(BUILD)
    manifest["entryHash"] = entry_hash
    manifest["zipHash"] = zip_hash
    open(os.path.join(BUILD, "plugin.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    print("  entryHash: %s" % entry_hash)
    print("  zipHash:   %s" % zip_hash)

    # 打包
    zip_path = os.path.join(DIST, "audiobook.jsplugin.zip")
    zout = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED)
    for dirpath, _, filenames in os.walk(BUILD):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, BUILD).replace("\\", "/")
            zout.write(full, rel)
    zout.close()
    print("  输出: %s (%.1f KB)" % (zip_path, os.path.getsize(zip_path) / 1024))


if __name__ == "__main__":
    main()
