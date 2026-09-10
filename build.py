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
PLUGIN_VERSION = os.environ.get("PLUGIN_VERSION") or "1.3.36"

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


def _u(s: str) -> str:
    # 中文转 \uXXXX 转义（与官方源码风格一致，避免不同环境下 jsc 处理源码的编码差异）
    return "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in s)


def _qs(s: str) -> str:
    # 返回一个「带双引号、内部中文已转义」的 JS 字符串字面量，
    # 用于把中文安全嵌进拼接出的 JS 源码（避免 \uXXXX 脱离引号变成非法 token）。
    return '"' + _u(s) + '"'


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
    # __U/__D 必须定义在模块级：__rescanDir（指定目录重扫）也要调用 __D。
    # 原实现把两者塞在 _() 内部，目录重扫会抛 "__D is not defined" 而静默失败。
    P5_TOP = (
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
        "t.books.push(bk),t.chaptersByBookId[bk.id]=cs;"
        "if(__SH_NEW&&__SH_NEW[__SH_GRP])try{var __mm=0;"
        "try{var __dm=await songloft.fs.stat(dir);__mm=Number((__dm&&__dm.modTime)||0)}catch(_){}"
        "var __msig=await __shSig(auds,[],dir);"
        "__SH_NEW[__SH_GRP].push({rel:rel+\"/__misc__\",mt:__mm,sig:__msig,book:bk,chapters:cs})}catch(_){}}"
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
    )
    a5 = "async function gt(s,t){"
    assert src.count(a5) == 1, "P5a 锚点数量异常"
    src = src.replace(a5, P5_TOP + "async function gt(s,t,__K){")

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
               'typeof t=="string"&&(this.settings=__MS(this.settings,JSON.parse(t))),'
               'typeof __CSET!="undefined"&&(__CSET=this.settings)')
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
    p8g_new = "folderRelPath:dir,isMisc:!0,virt:\"misc\"};"
    assert src.count(p8g_old) == 1, "P8g 锚点数量异常"
    src = src.replace(p8g_old, p8g_new)

    # P8g2: 书库根目录下的“未分类音频”书（ut 生成，folderRelPath=书库根）同样标记 isMisc
    p8g2_old = "chapterCount:n.length,totalSize:e,folderRelPath:s}"
    p8g2_new = "chapterCount:n.length,totalSize:e,folderRelPath:s,isMisc:!0,virt:\"misc\"}"
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

        # P8i: 允许删除「未分类」（仅删音频文件）与「合集」（删每个成员书文件夹）
        #   - 未分类(isMisc / category==="未分类" / id 含 __misc__)：只删目录下的音频文件，保留文件夹（__RMaudio）
        #   - 合集(virt:"shorts")：遍历 memberIds 删每个成员书文件夹，再清掉合集与成员记录
        p8i_rmaudio = (
            "async function __RMaudio(p){"
            "var AUD=new Set([\".mp3\",\".m4a\",\".m4b\",\".wav\",\".aac\",\".flac\",\".ogg\",\".wma\",\".opus\",\".ape\",\".wv\",\".tta\",\".mp2\",\".ac3\"]);"
            "var es=[],failed=0,removed=0;"
            "try{es=await songloft.fs.readdir(p)||[]}catch(_){return{filesFailed:0,dirsTotal:0,dirsRemoved:0,dirGone:!1,fallback:\"\",keptFolder:!0,audioRemoved:0}}"
            "for(var x of es){if(x.isDir)continue;"
            "var ext=\".\"+(x.name.split(\".\").pop()||\"\").toLowerCase();"
            "if(!AUD.has(ext))continue;"
            "try{await songloft.fs.unlink(p+\"/\"+x.name),removed++}catch(_){failed++}}"
            "return{filesFailed:failed,dirsTotal:0,dirsRemoved:0,dirGone:!1,fallback:\"\",keptFolder:!0,audioRemoved:removed}}\n"
            # v1.3.36: 删除书籍后清除该路径所在顶层组的增量扫描分片——
            # 否则残留分片会让下一次增量扫描继续复用旧数据（已删目录的条目残留）
            "async function __SH_DROP(relp){try{if(!relp||relp.indexOf(M)!==0)return;var sg=relp.slice(M.length+1).split(\"/\")[0];if(sg)await songloft.fs.unlink(__SHD+\"/\"+ct(sg)+\".json\")}catch(_){}}\n"
        )
        p8i_old = ('async function __delBook(t,id){let o=t.books.find(b=>b.id===id);if(!o)return!1;'
                   'let p=o.folderRelPath;if(!p||typeof p!=="string"||p.indexOf(M)!==0)'
                   'throw new Error("\\u8DEF\\u5F84\\u975E\\u6CD5\\uFF0C\\u62D2\\u7EDD\\u5220\\u9664");'
                   'if(p===M||p===M+"/")throw new Error("\\u4E0D\\u80FD\\u5220\\u9664\\u4E66\\u5E93\\u6839\\u76EE\\u5F55");'
                   'if(o.isMisc||o.category==="\\u672A\\u5206\\u7C7B"||(o.id||"").indexOf("__misc__")>=0)throw new Error("\\u672A\\u5206\\u7C7B\\u5408\\u96C6\\u4E0D\\u53EF\\u6574\\u672C\\u5220\\u9664\\uFF0C\\u8BF7\\u5728\\u6587\\u4EF6\\u7BA1\\u7406\\u5668\\u4E2D\\u624B\\u52A8\\u6E05\\u7406");'
                   'let rm=await __RM(p);'
                   't.books=t.books.filter(b=>b.id!==id);delete t.chaptersByBookId[id];'
                   't.settings.favorites=t.settings.favorites.filter(x=>x!==id);'
                   'if(t.settings.titleOverrides)delete t.settings.titleOverrides[id];'
                   'if(t.settings.playbackRates)delete t.settings.playbackRates[id];'
                   'await t.saveSettings();try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
                   'return rm}\n')
        p8i_new = (p8i_rmaudio +
            'async function __delBook(t,id){var o=t.books.find(function(b){return b.id===id});if(!o)return!1;'
            'var p=o.folderRelPath;if(!p||typeof p!=="string"||p.indexOf(M)!==0)throw new Error(' + _qs("路径非法，拒绝删除") + ');'
            'if(p===M||p===M+"/")throw new Error(' + _qs("不能删除书库根目录") + ');'
            'if(o.virt==="shorts"){var mids=o.memberIds||[],agg={filesFailed:0,dirsTotal:0,dirsRemoved:0,dirGone:!1,fallback:"",keptFolder:!1,collection:!0,memberCount:mids.length};'
            'var __sgs={};'
            'for(var mi=0;mi<mids.length;mi++){var mb=t.books.find(function(b){return b.id===mids[mi]});if(!mb||!mb.folderRelPath)continue;'
            'var r2=await __RM(mb.folderRelPath);agg.filesFailed+=(r2.filesFailed||0);agg.dirsRemoved+=(r2.dirsRemoved||0);agg.dirsTotal+=(r2.dirsTotal||0);if(r2.fallback)agg.fallback=r2.fallback;'
            'try{var __sg2=mb.folderRelPath.slice(M.length+1).split("/")[0];__sgs[__sg2]=1}catch(_){}}'
            'try{for(var __gk in __sgs)await __SH_DROP(M+"/"+__gk)}catch(_){}'
            't.books=t.books.filter(function(b){return b.id!==id&&mids.indexOf(b.id)<0});'
            'for(var mi2=0;mi2<mids.length;mi2++){delete t.chaptersByBookId[mids[mi2]];if(t.settings.favorites)t.settings.favorites=t.settings.favorites.filter(function(x){return x!==mids[mi2]});if(t.settings.titleOverrides)delete t.settings.titleOverrides[mids[mi2]];if(t.settings.playbackRates)delete t.settings.playbackRates[mids[mi2]]}'
            'delete t.chaptersByBookId[id];if(t.settings.favorites)t.settings.favorites=t.settings.favorites.filter(function(x){return x!==id});if(t.settings.titleOverrides)delete t.settings.titleOverrides[id];if(t.settings.playbackRates)delete t.settings.playbackRates[id];'
            'await t.saveSettings();try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'return agg}'
            'if(o.isMisc||o.category==="\\u672A\\u5206\\u7C7B"||(o.id||"").indexOf("__misc__")>=0){var rm=await __RMaudio(p);'
            'await __SH_DROP(p);'
            't.books=t.books.filter(function(b){return b.id!==id});delete t.chaptersByBookId[id];if(t.settings.favorites)t.settings.favorites=t.settings.favorites.filter(function(x){return x!==id});if(t.settings.titleOverrides)delete t.settings.titleOverrides[id];if(t.settings.playbackRates)delete t.settings.playbackRates[id];'
            'await t.saveSettings();try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'return rm}'
            'var rm=await __RM(p);'
            'await __SH_DROP(p);'
            't.books=t.books.filter(function(b){return b.id!==id});delete t.chaptersByBookId[id];if(t.settings.favorites)t.settings.favorites=t.settings.favorites.filter(function(x){return x!==id});if(t.settings.titleOverrides)delete t.settings.titleOverrides[id];if(t.settings.playbackRates)delete t.settings.playbackRates[id];'
            'await t.saveSettings();try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'return rm}')
        assert src.count(p8i_old) == 1, "P8i 锚点数量异常"
        src = src.replace(p8i_old, p8i_new)

    # P8k: 批量编辑覆盖层（第三组）—— settings.bookEdits 按 id 存 description/category/tags/author
    #   覆盖值，不改扫描数据不写 metadata.json，重扫不丢；「重设」=删除覆盖键回落扫描值。
    #   __BK_EDITS(s,b) 返回覆盖字段对象（无覆盖返回 null，调用方 ...(x||{}) 展开）。
    if not globals().get("SKIP_P8K", False):
        p8k_old = 'var K="audiobook_settings_v1"'
        p8k_new = ('function __BK_EDITS(s,b){var e=(s.bookEdits||{})[b.id];'
                   'return e?{description:e.description!=null?e.description:b.description,'
                   'category:e.category!=null?e.category:b.category,'
                   'tags:e.tags!=null?e.tags:b.tags,'
                   'author:e.author!=null?e.author:b.author}:null}\n'
                   'var K="audiobook_settings_v1"')
        assert src.count(p8k_old) == 1, "P8k 锚点数量异常"
        src = src.replace(p8k_old, p8k_new)

        # __MS 深合并补 bookEdits 键（旧存量 settings 升级）
        p8k_ms_old = 'playbackRates:Object.assign({},a&&a.playbackRates,b.playbackRates)})}'
        p8k_ms_new = ('playbackRates:Object.assign({},a&&a.playbackRates,b.playbackRates),'
                      'bookEdits:Object.assign({},a&&a.bookEdits,b.bookEdits)})}')
        assert src.count(p8k_ms_old) == 1, "P8k ms 锚点数量异常"
        src = src.replace(p8k_ms_old, p8k_ms_new)

        # list() 的覆盖应用并入 P17a（hidden 过滤补丁在 patch_ui 后段，见 p17a）

        # getBookById：详情/元数据接口同样应用覆盖
        p8k_gb_old = ('return n?{...n,title:this.settings.titleOverrides[n.id]||n.title,'
                      'originalTitle:n.title}:n}')
        p8k_gb_new = ('return n?{...n,title:this.settings.titleOverrides[n.id]||n.title,'
                      'originalTitle:n.title,...(__BK_EDITS(this.settings,n)||{})}:n}')
        assert src.count(p8k_gb_old) == 1, "P8k gb 锚点数量异常"
        src = src.replace(p8k_gb_old, p8k_gb_new)

    # P8l: 批量操作路由 —— 全部书 id（供「全选全部」，带当前关键词/收藏过滤）/
    #   batch/favorite（显式收藏/取消，非 toggle）/ batch/edit（简介前后插、分类标签作者
    #   追加/替换/重设）/ batch/cover-reset（从 cover_backup 恢复原封面）
    if not globals().get("SKIP_P8L", False):
        p8l_old = 'return f({success:!0,data:{id:e.id,cleared:r}})}),'
        p8l_new = p8l_old + (
            's.get("/api/book-ids",async o=>{'
            'let e=k(o.query||""),kw=(e.keyword||"").trim().toLowerCase(),fav=e.favoritesOnly==="true",'
            'a=t.books.filter(function(b){return !b.hidden});'
            'if(kw)a=a.filter(c=>{var ed=(t.settings.bookEdits||{})[c.id]||{},'
            'ti=(t.settings.titleOverrides||{})[c.id]||c.title,'
            'de=ed.description!=null?ed.description:c.description,'
            'tg=ed.tags!=null?ed.tags:(c.tags||[]);'
            'return ti.toLowerCase().indexOf(kw)>=0||String(c.author||"").toLowerCase().indexOf(kw)>=0'
            '||String(de||"").toLowerCase().indexOf(kw)>=0'
            '||(tg||[]).some(u=>String(u).toLowerCase().indexOf(kw)>=0)});'
            'if(fav){var s2=new Set(t.settings.favorites||[]);a=a.filter(c=>s2.has(c.id))}'
            'return f({success:!0,data:{ids:a.map(c=>({id:c.id,title:(t.settings.titleOverrides||{})[c.id]||c.title}))}})}),'
            's.post("/api/batch/favorite",async o=>{'
            'let e=typeof o.body=="string"?JSON.parse(o.body):o.body||{},ids=e.ids||[],on=!!e.on,ch=0;'
            't.settings.favorites||(t.settings.favorites=[]);'
            'for(var i2=0;i2<ids.length;i2++){var id2=ids[i2];'
            'if(on){t.settings.favorites.indexOf(id2)<0&&(t.settings.favorites.push(id2),ch++)}'
            'else{var ix=t.settings.favorites.indexOf(id2);ix>=0&&(t.settings.favorites.splice(ix,1),ch++)}}'
            'await t.saveSettings();return f({success:!0,data:{changed:ch}})}),'
            's.post("/api/batch/edit",async o=>{'
            'let e=typeof o.body=="string"?JSON.parse(o.body):o.body||{},ids=e.ids||[],ops=e.ops||{},ch=0,skip=0;'
            't.settings.bookEdits||(t.settings.bookEdits={});'
            'function __fld(ed,b,k,op,asTags){'
            'if(!op||!op.mode)return;'
            'var v=op.value==null?"":String(op.value).trim();'
            'if(op.mode==="reset"){delete ed[k];return}'
            'if(!v){skip++;return}'
            'if(asTags){var cur=ed[k]!=null?ed[k]:(b[k]||[]);'
            'if(op.mode==="append"){var add=v.split(/[,\\uFF0C\\u3001\\s]+/).filter(Boolean);'
            'ed.tags=Array.from(new Set([].concat(cur,add)))}'
            'else{ed.tags=v.split(/[,\\uFF0C\\u3001\\s]+/).filter(Boolean)}}'
            'else{if(op.mode==="append")ed[k]=String(ed[k]!=null?ed[k]:(b[k]||""))+v;'
            'else ed[k]=v}}'
            'for(var i3=0;i3<ids.length;i3++){var id3=ids[i3],'
            'b3=t.books.find(function(x){return x.id===id3&&!x.hidden});'
            'if(!b3){skip++;continue}'
            'var ed3=t.settings.bookEdits[id3]||(t.settings.bookEdits[id3]={});'
            'if(ops.descriptionPrepend)ed3.description=String(ops.descriptionPrepend)+String(ed3.description!=null?ed3.description:(b3.description||""));'
            'if(ops.descriptionAppend)ed3.description=String(ed3.description!=null?ed3.description:(b3.description||""))+String(ops.descriptionAppend);'
            '__fld(ed3,b3,"category",ops.category,!1);'
            '__fld(ed3,b3,"tags",ops.tags,!0);'
            '__fld(ed3,b3,"author",ops.author,!1);'
            'var any=!1;for(var kk in ed3)any=!0;'
            'if(any)ch++;else delete t.settings.bookEdits[id3]}'
            'await t.saveSettings();return f({success:!0,data:{changed:ch,skipped:skip}})}),'
            's.post("/api/batch/cover-reset",async o=>{'
            'let e=typeof o.body=="string"?JSON.parse(o.body):o.body||{},ids=e.ids||[],restored=[],missing=[];'
            'for(var i4=0;i4<ids.length;i4++){var id4=ids[i4],'
            'b4=t.books.find(function(x){return x.id===id4&&!x.hidden});'
            'if(!b4)continue;'
            'var bk="cover_backup/"+ct(id4)+".jpg",ok4=!1;'
            'try{ok4=await songloft.fs.exists(bk)}catch(_){ok4=!1}'
            'if(!ok4){missing.push(id4);continue}'
            'try{var d4=await songloft.fs.readFile(bk,{encoding:"base64"});'
            'await songloft.fs.writeFile(b4.folderRelPath+"/cover.jpg",d4,{encoding:"base64"});'
            'try{await songloft.fs.unlink(bk)}catch(_){}'
            'b4.coverUrl=b4.folderRelPath+"/cover.jpg";restored.push(id4)}catch(_){missing.push(id4)}}'
            'try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'return f({success:!0,data:{restored:restored,missing:missing}})}),')
        assert src.count(p8l_old) == 1, "P8l 锚点数量异常"
        src = src.replace(p8l_old, p8l_new)

    # P8m: 自定义封面先备份 —— updateCover 覆写 cover.jpg 前把原封面存到插件目录
    #   cover_backup/<ct(id)>.jpg（仅首次，避免二次改封面时备份被自定义图覆盖）；
    #   「封面重设」从备份还原。缓存清理只清 .cache/transcode，不会动 cover_backup。
    if not globals().get("SKIP_P8M", False):
        p8m_old = ('let e=`${o.folderRelPath}/cover.jpg`;return await songloft.fs.writeFile(e,n,{encoding:"base64"}),'
                   'o.coverUrl=e,`data:image/jpeg;base64,${n}`}')
        p8m_new = ('let e=`${o.folderRelPath}/cover.jpg`;'
                   'try{var __ex=await songloft.fs.exists(e).catch(function(){return!1});'
                   'if(__ex){try{await songloft.fs.mkdir("cover_backup",{recursive:!0})}catch(__e){}'
                   'var __bk="cover_backup/"+ct(o.id)+".jpg",'
                   '__bex=await songloft.fs.exists(__bk).catch(function(){return!1});'
                   'if(!__bex){var __d=await songloft.fs.readFile(e,{encoding:"base64"});'
                   'await songloft.fs.writeFile(__bk,__d,{encoding:"base64"})}}}catch(_){}'
                   'return await songloft.fs.writeFile(e,n,{encoding:"base64"}),'
                   'o.coverUrl=e,`data:image/jpeg;base64,${n}`}')
        assert src.count(p8m_old) == 1, "P8m 锚点数量异常"
        src = src.replace(p8m_old, p8m_new)

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
                         'dirsTotal:r.dirsTotal,fallback:r.fallback||"",'
                         'keptFolder:r.keptFolder||!1,audioRemoved:r.audioRemoved||0,'
                         'collection:r.collection||!1,memberCount:r.memberCount||0}})}'
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
                         'return f({success:!0,data:{count:out.length,dirs:out}})}),'
                         # 诊断：查看/清理自动合集忽略名单（打散自动合集后可恢复）
                         # GET  → {ignoredShortsMembers:[...]}
                         # POST {action:"clear"} 清空；{action:"remove",ids:[...]} 移除指定成员
                         # 诊断：列出增量扫描分片缓存（.cache/abscan）状态
                         's.get("/api/debug/shards",async()=>{'
                         'var out=[];'
                         'try{var es=await songloft.fs.readdir(__SHD)||[];'
                         'for(var i=0;i<es.length;i++){if(es[i].isDir)continue;'
                         'try{var j=JSON.parse(await songloft.fs.readFile(__SHD+"/"+es[i].name));'
                         'out.push({name:es[i].name,v:j&&j.v,g:j&&j.g,items:j&&j.items?j.items.length:0,'
                         'misc:j&&j.items?j.items.filter(function(x){return x&&x.rel&&x.rel.indexOf("/__misc__")>=0}).length:0,'
                         'ts:j&&j.ts})}catch(e){out.push({name:es[i].name,err:String(e)})}}}catch(e){'
                         'return f({success:!0,data:{dirMissing:!0,err:String(e),shards:out}})}'
                         'return f({success:!0,data:{count:out.length,shards:out}})}),'
                         's.get("/api/debug/ignore-list",async()=>{'
                         'return f({success:!0,data:{ignoredShortsMembers:'
                         '(t.settings.ignoredShortsMembers||[])}})}),'
                         's.post("/api/debug/ignore-list",async o=>{'
                         'let e=typeof o.body=="string"?JSON.parse(o.body):o.body||{},'
                         'act=e.action||"clear";'
                         'var ig=t.settings.ignoredShortsMembers||'
                         '(t.settings.ignoredShortsMembers=[]);'
                         'if(act==="clear"){ig.length=0}'
                         'else if(act==="remove"){'
                         'var rm=(e.ids||[]).map(String);'
                         't.settings.ignoredShortsMembers='
                         'ig.filter(function(x){return rm.indexOf(x)<0})}'
                         'else return h("' + _u("未知action") + '",400);'
                         'await t.saveSettings();'
                         '__SHORTS_MERGE(t),__CUSTOM_MERGE(t,t.settings);'
                         'try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
                         'return f({success:!0,data:{action:act,'
                         'ignoredShortsMembers:t.settings.ignoredShortsMembers||[]}})})')
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
                    'var T=__SHORTS_T;'
                    'var __IGN={};(function(){var arr=((t&&t.settings||__CSET||{}).ignoredShortsMembers)||[];'
                    'for(var q=0;q<arr.length;q++)__IGN[arr[q]]=1})();'
                    # v1.3.26 复位：先恢复所有子书可见、移除上一轮短篇合集，再按当前
                    # 阈值重新合并 —— 幂等（不会重复 id），且改阈值后任意一次扫描/
                    # 重建即生效。子书不再从库中删除，只打 hidden 标记，后续「打散」
                    # 才能还原出子书。
                    'for(var i0=0;i0<t.books.length;i0++){var b0=t.books[i0];'
                    'if(b0.mergedInto){b0.mergedInto="";b0.hidden=!1}}'
                    'var nb0=[];for(var i1=0;i1<t.books.length;i1++)'
                    'if(t.books[i1].virt!=="shorts")nb0.push(t.books[i1]);'
                    't.books=nb0;'
                    'if(!(T>=1))return;'
                    'var groups={};'
                    'for(var i=0;i<t.books.length;i++){'
                    'var b=t.books[i];'
                    'if(b.isMisc||b.virt||b.hidden)continue;'
                    'if(__IGN[b.id])continue;'
                    'if((b.chapterCount||0)>T)continue;'
                    'var p=b.folderRelPath||"",ix=p.lastIndexOf("/");'
                    'if(ix<=0)continue;'
                    'var par=p.substring(0,ix);'
                    'if(par===M)continue;'
                    '(groups[par]=groups[par]||[]).push(b)}'
                    'for(var par in groups){'
                    'var subs=groups[par];if(!subs.length)continue;'
                    'subs.sort(function(a,b){return $(a.folderRelPath,b.folderRelPath)});'
                    'var cs=[],size=0,upd=0,cover=null,cat="",tags={},names=[],mids=[];'
                    'for(var j=0;j<subs.length;j++){'
                    'var b2=subs[j];mids.push(b2.id);'
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
                    'var mb={id:R(par+"/__shorts__"),virt:"shorts",memberIds:mids,'
                    'title:pn+"-\\u77ed\\u7bc7\\u5408\\u96c6",author:"\\u5408\\u96c6",coverUrl:cover,coverRatio:"",'
                    'description:"\\u7531 "+subs.length+" \\u672c\\u77ed\\u7bc7\\u6709\\u58f0\\u4e66\\u5408\\u5e76\\uff1a"+names.join("\\u3001"),'
                    'category:cat||"\\u9ed8\\u8ba4",tags:Object.keys(tags),updatedAt:upd||Date.now(),chapterCount:cs.length,totalSize:size,folderRelPath:par,isMisc:!0};'
                    'var ex=-1;for(var z2=0;z2<t.books.length;z2++)if(t.books[z2].id===mb.id){ex=z2;break}'
                    'if(ex>=0)t.books[ex]=mb;else t.books.push(mb);'
                    't.chaptersByBookId[mb.id]=cs;'
                    'for(var j3=0;j3<subs.length;j3++){subs[j3].mergedInto=mb.id,subs[j3].hidden=!0}'
                    'songloft.log.info("\\u77ed\\u7bc7\\u5408\\u5e76\\uff1a"+pn+" \\u5408\\u5e76 "+subs.length+" \\u672c / "+cs.length+" \\u7ae0")}}'
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
                    'dir=String(dir||"").replace(/\\\\/g,"/").replace(/^\\/+|\\/+$/g,"");var __mp=M.replace(/^\\/+/,"");if(dir.indexOf(__mp+"/")===0)dir=dir.slice(__mp.length+1);else if(dir===__mp)dir="";'
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

    # P13: 目录树懒加载接口 —— GET /api/list-dir?dir=相对路径（空=根）
    #   每次只列一层子目录（含各自直接子目录数，供前端决定是否显示展开箭头）。
    #   替代重扫弹窗原先对 /api/debug/dirs（一次性递归全树）的依赖：
    #   大书库（.10 约 5693 本）全树遍历过慢导致请求超时、目录列不出来。
    if not globals().get("SKIP_P13", False):
        p13_old = 'return f({success:!0,data:{count:out.length,dirs:out}})})'
        p13_new = ('return f({success:!0,data:{count:out.length,dirs:out}})}),'
                   's.get("/api/list-dir",async o=>{'
                   'let e=k(o.query||""),dir=String(e.dir||"").replace(/\\\\/g,"/").replace(/^\\/+|\\/+$/g,"");'
                   'if(!__SAFE(dir))return h("\\u65e0\\u6548\\u76ee\\u5f55",400);'
                   'var full=dir?M+"/"+dir:M;'
                   'try{await songloft.fs.stat(full)}catch(_){return h("\\u76ee\\u5f55\\u4e0d\\u5b58\\u5728",404)}'
                   'var es=[];try{es=await songloft.fs.readdir(full)||[]}catch(_){es=[]}'
                   'var out=[];'
                   'for(var i=0;i<es.length;i++){var x=es[i];'
                   'if(!x.isDir||__IGN.has(x.name)||!__SAFE(x.name))continue;'
                   'var sub=0,cs=[];'
                   'try{cs=await songloft.fs.readdir(full+"/"+x.name)||[]}catch(_){cs=[]}'
                   'for(var j2=0;j2<cs.length;j2++){var c=cs[j2];'
                   'if(c.isDir&&!__IGN.has(c.name)&&__SAFE(c.name))sub++}'
                   'out.push({name:x.name,rel:dir?dir+"/"+x.name:x.name,subdirs:sub})}'
                   'out.sort(function(a,b){return a.name.localeCompare(b.name,"zh")});'
                   'return f({success:!0,data:{path:dir,count:out.length,dirs:out}})})')
        assert src.count(p13_old) == 1, "P13 锚点数量异常"
        src = src.replace(p13_old, p13_new)

    # P14: 扫描进度 —— 模块级 __SCANP 状态 + GET /api/scan-progress 轻量接口
    #   全库扫描 _() 与目录重扫 __rescanDir 都会更新进度；
    #   rootTotal/rootDone = 顶层文件夹进度（目录重扫时为 0，前端改用 dirs 计数展示）。
    if not globals().get("SKIP_P14", False):
        p14a_old = 'async function _(){let s=M,t={books:[],chaptersByBookId:{}},n=[];'
        p14a_new = ('var __SCANP={scanning:!1,startedAt:0,rootTotal:0,rootDone:0,dirs:0,books:0,currentDir:""};'
                    'async function _(){__SCANP.scanning=!0,__SCANP.startedAt=Date.now(),'
                    '__SCANP.rootTotal=0,__SCANP.rootDone=0,__SCANP.dirs=0,__SCANP.books=0,__SCANP.currentDir="";'
                    'let s=M,t={books:[],chaptersByBookId:{}},n=[];')
        assert src.count(p14a_old) == 1, "P14a 锚点数量异常"
        src = src.replace(p14a_old, p14a_new)

        # 根目录分类完成后：顶层组总数
        p14b_old = 'r.isDir?o.push(r.name):A(r.name)&&e.push(r.name)}'
        p14b_new = 'r.isDir?o.push(r.name):A(r.name)&&e.push(r.name)}__SCANP.rootTotal=o.length,__SCANP.rootDone=0;'
        assert src.count(p14b_old) == 1, "P14b 锚点数量异常"
        src = src.replace(p14b_old, p14b_new)

        # __D 入口：目录计数 + 当前目录
        p14c_old = 'async function __D(s,rel,t,IG,d){if(d>20)return;'
        p14c_new = 'async function __D(s,rel,t,IG,d){if(d>20)return;__SCANP.dirs++,d>0&&(__SCANP.currentDir=rel);'
        assert src.count(p14c_old) == 1, "P14c 锚点数量异常"
        src = src.replace(p14c_old, p14c_new)

        # 叶子目录发现成书
        p14d_old = 'a&&a.chapters.length>0&&(t.books.push(a.book),t.chaptersByBookId[a.book.id]=a.chapters)'
        p14d_new = 'a&&a.chapters.length>0&&(t.books.push(a.book),t.chaptersByBookId[a.book.id]=a.chapters,__SCANP.books++)'
        assert src.count(p14d_old) == 1, "P14d 锚点数量异常"
        src = src.replace(p14d_old, p14d_new)

        # 主循环：每个顶层文件夹开扫时更新当前目录
        p14e_old = 'for(let r of o)try{await __D(s,r,t,__IGN,0)}catch(a){'
        p14e_new = 'for(let r of o){__SCANP.currentDir=r;try{await __D(s,r,t,__IGN,0)}catch(a){'
        assert src.count(p14e_old) == 1, "P14e 锚点数量异常"
        src = src.replace(p14e_old, p14e_new)

        # 主循环收尾：顶层组完成计数（补上 P14e 加的 for 花括号）
        p14f_old = 'if(e.length>0)try{let r=await ut(s,e);'
        p14f_new = '__SCANP.rootDone++}if(e.length>0)try{let r=await ut(s,e);'
        assert src.count(p14f_old) == 1, "P14f 锚点数量异常"
        src = src.replace(p14f_old, p14f_new)

        # 根目录散落音频成书
        p14g_old = 'r&&r.chapters.length>0&&(t.books.push(r.book),t.chaptersByBookId[r.book.id]=r.chapters)'
        p14g_new = 'r&&r.chapters.length>0&&(t.books.push(r.book),t.chaptersByBookId[r.book.id]=r.chapters,__SCANP.books++)'
        assert src.count(p14g_old) == 1, "P14g 锚点数量异常"
        src = src.replace(p14g_old, p14g_new)

        # _() 正常结束：关扫描标志
        p14h_old = 'return t.books.sort((r,a)=>a.updatedAt-r.updatedAt),songloft.log.info('
        p14h_new = 'return __SCANP.scanning=!1,__SCANP.currentDir="",t.books.sort((r,a)=>a.updatedAt-r.updatedAt),songloft.log.info('
        assert src.count(p14h_old) == 1, "P14h 锚点数量异常"
        src = src.replace(p14h_old, p14h_new)

        # 三个调用方的 finally 兜底（异常/提前返回时也要关 __SCANP.scanning）
        p14i1_old = 'finally{this.scanning=!1}}list(t){'
        p14i1_new = 'finally{this.scanning=!1,__SCANP.scanning=!1}}list(t){'
        assert src.count(p14i1_old) == 1, "P14i1 锚点数量异常"
        src = src.replace(p14i1_old, p14i1_new)

        p14i2_old = 'finally{this.scanning=!1}}getSettings(){'
        p14i2_new = 'finally{this.scanning=!1,__SCANP.scanning=!1}}getSettings(){'
        assert src.count(p14i2_old) == 1, "P14i2 锚点数量异常"
        src = src.replace(p14i2_old, p14i2_new)

        p14i3_old = 'finally{this.scanning=!1}}async rescan(){'
        p14i3_new = 'finally{this.scanning=!1,__SCANP.scanning=!1}}async rescan(){'
        assert src.count(p14i3_old) == 1, "P14i3 锚点数量异常"
        src = src.replace(p14i3_old, p14i3_new)

        # 目录重扫：重置进度（rootTotal=0 → 前端按“已扫目录数”展示）
        p14j_old = 'this.scanning=!0;var g=++this.generation;'
        p14j_new = ('__SCANP.scanning=!0,__SCANP.startedAt=Date.now(),'
                    '__SCANP.rootTotal=0,__SCANP.rootDone=0,__SCANP.dirs=0,__SCANP.books=0,__SCANP.currentDir=dir;'
                    'this.scanning=!0;var g=++this.generation;')
        assert src.count(p14j_old) == 1, "P14j 锚点数量异常"
        src = src.replace(p14j_old, p14j_new)

        # 轻量进度接口（/api/snapshot 携带全量书单，轮询太重）
        p14k_old = 's.get("/api/snapshot",async()=>f({success:!0,data:t.getSnapshot()})),'
        p14k_new = ('s.get("/api/snapshot",async()=>f({success:!0,data:t.getSnapshot()})),'
                    's.get("/api/scan-progress",()=>f({success:!0,data:Object.assign({},__SCANP,'
                    '{elapsed:__SCANP.scanning?Date.now()-__SCANP.startedAt:0})})),')
        assert src.count(p14k_old) == 1, "P14k 锚点数量异常"
        src = src.replace(p14k_old, p14k_new)

    # P15: 启动/重载时的自动扫描策略
    #   宿主自带插件自动更新机制（每天错峰触碰/重载插件），而原版初始化会无条件
    #   scanInBackground()，于是「没点重新扫描也天天全库重扫」。改为可配置策略。
    if not globals().get("SKIP_P15", False):
        # 记录上次成功扫描时间（scanInBackground / rescan 各一处）
        p15a_old = 'this.scannedAt=Date.now(),await N(n)'
        p15a_new = ('this.scannedAt=Date.now(),this.settings.lastScanAt=Date.now(),'
                    'await this.saveSettings(),await N(n)')
        assert src.count(p15a_old) == 2, "P15a 锚点数量异常"
        src = src.replace(p15a_old, p15a_new)

        _L1 = _u("有声书插件：按设置跳过启动扫描")
        _L2 = _u("有声书插件：缓存为空，启动首次扫描")
        _L3a = _u("有声书插件：距上次扫描 ")
        _L3b = _u(" 小时（超过阈值 ")
        _L3c = _u(" 小时），开始扫描")
        _L4a = _u("有声书插件：距上次扫描仅 ")
        _L4b = _u(" 小时，按设置跳过启动扫描（阈值 ")
        _L4c = _u(" 小时）")

        # 初始化改为 __initScan()（条件扫描）
        p15b_old = '__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold),this.scanInBackground()'
        p15b_new = '__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold),this.__initScan()'
        assert src.count(p15b_old) == 1, "P15b 锚点数量异常"
        src = src.replace(p15b_old, p15b_new)

        # 新增 __initScan 方法：always / stale(默认,72h) / empty / never
        p15c_old = 'async scanInBackground(){if(this.scanning)return;'
        p15c_new = ('async __initScan(){'
                    'var p=(this.settings.uiPrefs||{});'
                    'var mode=p.initScanMode||"stale";'
                    'var hrs=Number(p.initScanStaleHours||72);'
                    'if(mode==="never"){songloft.log.info("' + _L1 + '");return}'
                    'if(mode==="always"){return this.scanInBackground()}'
                    'if(!this.books.length){songloft.log.info("' + _L2 + '");return this.scanInBackground()}'
                    'if(mode==="empty"){return}'
                    'var last=this.settings.lastScanAt||0;'
                    'var age=(Date.now()-last)/3600000;'
                    'if(age>=hrs){songloft.log.info("' + _L3a + '"+age.toFixed(1)+"' + _L3b
                    + '"+hrs+"' + _L3c + '");return this.scanInBackground()}'
                    'songloft.log.info("' + _L4a + '"+age.toFixed(1)+"' + _L4b + '"+hrs+"' + _L4c + '")}'
                    'async scanInBackground(){if(this.scanning)return;')
        assert src.count(p15c_old) == 1, "P15c 锚点数量异常"
        src = src.replace(p15c_old, p15c_new)

        # scan-progress 附带 lastScanAt（前端展示“上次扫描时间”）
        p15d_old = '{elapsed:__SCANP.scanning?Date.now()-__SCANP.startedAt:0})})),'
        p15d_new = ('{elapsed:__SCANP.scanning?Date.now()-__SCANP.startedAt:0,'
                    'lastScanAt:t.settings.lastScanAt||0})})),')
        assert src.count(p15d_old) == 1, "P15d 锚点数量异常"
        src = src.replace(p15d_old, p15d_new)

    # P16: 增量 + 断点续扫
    #   - 每个「成书目录」按 (音频文件名列表 + 非音频文件名列表 + metadata.json 修改时间) 生成签名，
    #     签名未变则直接复用上次的书籍/章节，跳过全部 stat（大书库提速的关键）。
    #   - 每扫完一个顶层组就把该组结果写入分片 .cache/abscan/<hash>.json，
    #     中途被打断也不会白扫：下次运行直接复用已完成分组的分片，继续未完成的分组。
    #   - 手动触发的「重新扫描」可勾选 force 强制全量（忽略分片缓存）。
    if not globals().get("SKIP_P16", False):
        p16a_old = 'var __SCANP={scanning:!1,startedAt:0,rootTotal:0,rootDone:0,dirs:0,books:0,currentDir:""};'
        p16a_new = (p16a_old +
                    '__SCANP.fastHit=0,__SCANP.fastMiss=0,__SCANP.fastProbe="";'
                    'var __SHD=".cache/abscan";'
                    'var __SH_OLD={},__SH_NEW={},__SH_GRP="",__SH_FORCE=0,__SH_REUSE=0;'
                    'var __SH_ME=/\\/__misc__$/;'
                    'async function __shSig(auds,others,dir){'
                    'let h=ct(auds.join("|")+"#"+others.join("|"));'
                    'try{let m=await songloft.fs.stat(dir+"/metadata.json");if(m)h+="-"+Number(m.modTime||0)}catch(_){}'
                    'return h}'
                    'async function __shLoad(){let out={};'
                    'try{let es=await songloft.fs.readdir(__SHD)||[];'
                    'for(let i=0;i<es.length;i++){if(es[i].isDir)continue;'
                    'try{let j=JSON.parse(await songloft.fs.readFile(__SHD+"/"+es[i].name));'
                    'if(j&&j.items&&j.v===2)for(let k=0;k<j.items.length;k++){let it=j.items[k];if(it&&it.rel)out[it.rel]=it}}catch(_){}}}catch(_){}'
                    'return out}'
                    'async function __shSave(g,items){'
                    'try{await songloft.fs.mkdir(__SHD,{recursive:!0})}catch(_){}'
                    'try{await songloft.fs.writeFile(__SHD+"/"+ct(g)+".json",JSON.stringify({g:g,ts:Date.now(),v:2,items:items||[]}))}catch(_){}}'
                    # 快通道：整组的目录 mtime 全部未变 → 整组直接复用，连目录遍历都省掉
                    'async function __shFast(g,s){'
                    'if(__SH_FORCE)return null;'
                    'var list=[];for(var k in __SH_OLD){var it=__SH_OLD[k];'
                    'if(it&&(it.rel===g||it.rel.indexOf(g+"/")===0))list.push(it)}'
                    'if(!list.length)return null;'
                    'for(var i=0;i<list.length;i+=16){var b=list.slice(i,i+16);'
                    'var rs=await Promise.all(b.map(async it=>{'
                    'try{var __rp=(it.rel||"").replace(__SH_ME,"");var st=await songloft.fs.stat(__rp?s+"/"+__rp:s);return Number((st&&st.modTime)||0)}'
                    'catch(e){return -1}}));'
                    'for(var j=0;j<b.length;j++){var mv=rs[j];'
                    'if(!mv||mv!==b[j].mt){__SCANP.fastProbe=String(b[j].rel)+" exp"+b[j].mt+" act"+mv;return null}}}'
                    'return list}')
        assert src.count(p16a_old) == 1, "P16a 锚点数量异常"
        src = src.replace(p16a_old, p16a_new)

        # _() 根目录分类后：载入分片缓存
        p16b_old = '__SCANP.rootTotal=o.length,__SCANP.rootDone=0;'
        p16b_new = ('__SCANP.rootTotal=o.length,__SCANP.rootDone=0;'
                    '__SH_OLD=__SH_FORCE?{}:await __shLoad(),__SH_NEW={},__SH_GRP="",__SH_REUSE=0,'
                    '__SCANP.fastHit=0,__SCANP.fastMiss=0,__SCANP.fastProbe="";')
        assert src.count(p16b_old) == 1, "P16b 锚点数量异常"
        src = src.replace(p16b_old, p16b_new)

        # 顶层组循环：开扫前建分组容器，完成后写分片
        p16c_old = 'for(let r of o){__SCANP.currentDir=r;try{await __D(s,r,t,__IGN,0)}catch(a){'
        p16c_new = ('for(let r of o){__SCANP.currentDir=r;__SH_GRP=r;__SH_NEW[r]=[];'
                    'let __fl=await __shFast(r,s);__fl?__SCANP.fastHit++:__SCANP.fastMiss++;'
                    'if(__fl){for(let i=0;i<__fl.length;i++){let it=__fl[i];'
                    'if(it.book&&it.chapters){t.books.push(it.book),t.chaptersByBookId[it.book.id]=it.chapters,'
                    '__SCANP.books++,__SH_REUSE++}__SH_NEW[r].push(it)}__SCANP.dirs+=__fl.length}'
                    'else{try{await __D(s,r,t,__IGN,0)}catch(a){')
        assert src.count(p16c_old) == 1, "P16c 锚点数量异常"
        src = src.replace(p16c_old, p16c_new)

        p16d_old = '__SCANP.rootDone++}if(e.length>0)try{let r=await ut(s,e);'
        p16d_new = ('}__SCANP.rootDone++;try{await __shSave(r,__SH_NEW[r])}catch(_){}}'
                    'if(e.length>0)try{let r=await ut(s,e);')
        assert src.count(p16d_old) == 1, "P16d 锚点数量异常"
        src = src.replace(p16d_old, p16d_new)

        # __D 叶子分支：签名命中则复用，否则照常扫描并写入本组分片
        p16e_old = ('try{let a=await gt(pa,nm,rel);'
                    'a&&a.chapters.length>0&&(t.books.push(a.book),t.chaptersByBookId[a.book.id]=a.chapters,__SCANP.books++)}')
        p16e_new = ('try{let __ot=[];for(let __i=0;__i<es.length;__i++){let __x=es[__i];'
                    'if(!__x.isDir&&!A(__x.name))__ot.push(__x.name)}__ot.sort();'
                    'let __sig=await __shSig(auds,__ot,dir),__hit=__SH_OLD[rel];'
                    'if(__hit&&__hit.sig===__sig&&__hit.book&&__hit.chapters){'
                    't.books.push(__hit.book),t.chaptersByBookId[__hit.book.id]=__hit.chapters,'
                    '__SCANP.books++,__SH_REUSE++;__SH_NEW[__SH_GRP]&&'
                    '__SH_NEW[__SH_GRP].push(Object.assign({},__hit,{mt:__mt}))}'
                    'else{let a=await gt(pa,nm,rel);'
                    'if(a&&a.chapters.length>0){t.books.push(a.book),t.chaptersByBookId[a.book.id]=a.chapters,__SCANP.books++;'
                    '__SH_NEW[__SH_GRP]&&__SH_NEW[__SH_GRP].push({rel:rel,mt:__mt,sig:__sig,book:a.book,chapters:a.chapters})}}}')
        assert src.count(p16e_old) == 1, "P16e 锚点数量异常"
        src = src.replace(p16e_old, p16e_new)

        # __D：采集目录 mtime（每个目录 1 次 stat，供下次扫描的整组快通道比对）
        p16m_old = 'let subs=[],auds=[];'
        p16m_new = ('let __mt=0;try{let __ds=await songloft.fs.stat(dir);'
                    '__mt=Number((__ds&&__ds.modTime)||0)}catch(_){}'
                    'let subs=[],auds=[];')
        assert src.count(p16m_old) == 1, "P16m 锚点数量异常"
        src = src.replace(p16m_old, p16m_new)

        # 空白叶子目录也要落条目（否则快通道漏掉它，删除/新增空目录检测不到）
        p16n_old = 'if(subs.length===0){if(auds.length===0)return;'
        p16n_new = ('if(subs.length===0){if(auds.length===0){'
                    '__SH_NEW[__SH_GRP]&&__SH_NEW[__SH_GRP].push({rel:rel,mt:__mt,sig:""});return;}')
        assert src.count(p16n_old) == 1, "P16n 锚点数量异常"
        src = src.replace(p16n_old, p16n_new)

        # 非叶子目录同样落条目（只记 mtime），用于检测子目录增删
        p16o_old = 'for(let x of subs)await __D(s,rel+"/"+x,t,IG,d+1);'
        p16o_new = ('__SH_NEW[__SH_GRP]&&__SH_NEW[__SH_GRP].push({rel:rel,mt:__mt,sig:""});'
                    'for(let x of subs)await __D(s,rel+"/"+x,t,IG,d+1);')
        assert src.count(p16o_old) == 1, "P16o 锚点数量异常"
        src = src.replace(p16o_old, p16o_new)

        # force 开关：/api/rescan 传入 {force:true} 时忽略分片；目录重扫同理
        p16f_old = 'if(d&&d.dir){'
        p16f_new = '__SH_FORCE=(d&&d.force)?1:0;if(d&&d.dir){'
        assert src.count(p16f_old) == 1, "P16f 锚点数量异常"
        src = src.replace(p16f_old, p16f_new)

        # 复位 force（rescan / __rescanDir 结束）
        p16g_old = 'finally{this.scanning=!1,__SCANP.scanning=!1}}getSettings(){'
        p16g_new = 'finally{this.scanning=!1,__SCANP.scanning=!1,__SH_FORCE=0}}getSettings(){'
        assert src.count(p16g_old) == 1, "P16g 锚点数量异常"
        src = src.replace(p16g_old, p16g_new)

        p16h_old = 'finally{this.scanning=!1,__SCANP.scanning=!1}}async rescan(){'
        p16h_new = 'finally{this.scanning=!1,__SCANP.scanning=!1,__SH_FORCE=0}}async rescan(){'
        assert src.count(p16h_old) == 1, "P16h 锚点数量异常"
        src = src.replace(p16h_old, p16h_new)

        # scan-progress 附带复用计数，便于确认增量是否生效
        p16i_old = 'lastScanAt:t.settings.lastScanAt||0})})),'
        p16i_new = ('lastScanAt:t.settings.lastScanAt||0,reused:__SH_REUSE||0,'
                    'fastHit:__SCANP.fastHit||0,fastMiss:__SCANP.fastMiss||0,'
                    'fastProbe:__SCANP.fastProbe||""})})),')
        assert src.count(p16i_old) == 1, "P16i 锚点数量异常"
        src = src.replace(p16i_old, p16i_new)

    # P17: 短篇合集可重建（v1.3.26）
    #   1) 合并后子书只打 hidden/mergedInto、不再从库中删除 —— 于是：
    #      a. 任意一次扫描/重建都能按当前阈值重算（改阈值即时生效，无需强制全量）
    #      b. 「重建合集」无需读盘（纯内存重跑合并，秒级完成）
    #      c. 后续「打散合集」能还原出子书
    #   2) list()/分类/标签/书数 一律过滤 hidden，UI 表现与旧版一致
    #   3) 目录重扫时丢弃扫描范围内的虚拟书（virt），交给扫描/合并重新生成
    if not globals().get("SKIP_P17", False):
        # p17a: list() 过滤 hidden
        # list()：hidden 过滤后立即应用 bookEdits 覆盖（P8k）——
        # 关键词搜索（简介/标签）与分类过滤均基于覆盖后值，与显示一致
        p17a_old = 'a=this.books.slice();'
        p17a_new = ('a=this.books.filter(function(b){return !b.hidden}).slice()'
                    '.map(c=>({...c,...(__BK_EDITS(this.settings,c)||{})}));')
        assert src.count(p17a_old) == 1, "P17a 锚点数量异常"
        src = src.replace(p17a_old, p17a_new)

        # p17b: 快照书数同样排除被合并掉的子书
        p17b_old = 'getSnapshot(){return{books:this.books,totalBooks:this.books.length,'
        p17b_new = ('getSnapshot(){var __vb=this.books.filter(function(b){return !b.hidden});'
                    'return{books:__vb,totalBooks:__vb.length,')
        assert src.count(p17b_old) == 1, "P17b 锚点数量异常"
        src = src.replace(p17b_old, p17b_new)

        p17c_old = 'for(let n of this.books)t.add(n.category);'
        p17c_new = 'for(let n of this.books){if(n.hidden)continue;t.add(n.category)}'
        assert src.count(p17c_old) == 1, "P17c 锚点数量异常"
        src = src.replace(p17c_old, p17c_new)

        p17d_old = 'for(let n of this.books)for(let o of n.tags||[])t.add(o);'
        p17d_new = 'for(let n of this.books){if(n.hidden)continue;for(let o of n.tags||[])t.add(o)}'
        assert src.count(p17d_old) == 1, "P17d 锚点数量异常"
        src = src.replace(p17d_old, p17d_new)

        # p17e: 目录重扫 —— 扫描范围内的虚拟书（短篇/未分类合集）先丢弃，
        #       由本次扫描 + 收尾合并重新生成，避免新旧两份同 id 记录并存
        p17e_old = ('var kept=base.books.filter(function(b){'
                    'return !(b.folderRelPath&&b.folderRelPath.indexOf(prefix)===0)});')
        p17e_new = ('var kept=base.books.filter(function(b){var bp=b.folderRelPath||"";'
                    'if(b.virt&&(bp===full||bp.indexOf(prefix)===0))return !1;'
                    'return !(bp&&bp.indexOf(prefix)===0)});')
        assert src.count(p17e_old) == 1, "P17e 锚点数量异常"
        src = src.replace(p17e_old, p17e_new)

        # p17f: __remerge —— 不读盘，直接在现有书库上按当前阈值重跑短篇合并
        p17f_old = 'async __rescanDir(dir){'
        p17f_new = ('async __remerge(){'
                    'if(this.scanning)throw new Error('
                    '"\\u626b\\u63cf\\u8fdb\\u884c\\u4e2d\\uff0c\\u8bf7\\u7a0d\\u540e\\u518d\\u8bd5");'
                    'var base=await G();'
                    'if(!base||!base.books||!base.books.length)'
                    'throw new Error("\\u4e66\\u5e93\\u4e3a\\u7a7a\\uff0c\\u8bf7\\u5148\\u626b\\u63cf");'
                    '__SHORTS_T=__SHORTS_VAL((this.settings.uiPrefs||{}).shortsMergeThreshold);'
                    '__SHORTS_MERGE(base);'
                    'this.books=base.books;this.chaptersByBookId=base.chaptersByBookId;'
                    'this.scannedAt=Date.now();'
                    'await N(base);await this.__migShorts(base);'
                    'var c=0;for(var i=0;i<base.books.length;i++)'
                    'if(base.books[i].virt==="shorts")c++;'
                    'return c}'
                    + p17f_old)
        assert src.count(p17f_old) == 1, "P17f 锚点数量异常"
        src = src.replace(p17f_old, p17f_new)

        # p17g: /api/rescan 支持 {remerge:1}
        p17g_old = '__SH_FORCE=(d&&d.force)?1:0;if(d&&d.dir){'
        p17g_new = ('__SH_FORCE=(d&&d.force)?1:0;'
                    'if(d&&d.remerge){'
                    't.__remerge().then(function(c){'
                    'songloft.log.info("\\u91cd\\u5efa\\u5408\\u96c6\\u5b8c\\u6210\\uff0c'
                    '\\u77ed\\u7bc7\\u5408\\u96c6 "+c+" \\u4e2a")})'
                    '.catch(function(x){'
                    'songloft.log.warn("\\u91cd\\u5efa\\u5408\\u96c6\\u5f02\\u5e38: "+String(x))});'
                    'return f({success:!0,data:{remerge:!0}})}'
                    'if(d&&d.dir){')
        assert src.count(p17g_old) == 1, "P17g 锚点数量异常"
        src = src.replace(p17g_old, p17g_new)

        # p17h: 扫描错误诊断（目录重扫曾因 __D 作用域问题静默失败）
        #   注意：__SCANP 声明在 P14 注入、P16 又整体替换，故这里不再改声明，
        #   改为在扫描开始时动态挂载 __SCANP.lastError（JS 对象可动态加属性）。
        p17i_old = '__SCANP.rootTotal=0,__SCANP.rootDone=0,__SCANP.dirs=0,__SCANP.books=0,__SCANP.currentDir=dir;'
        p17i_new = ('__SCANP.rootTotal=0,__SCANP.rootDone=0,__SCANP.dirs=0,__SCANP.books=0,'
                    '__SCANP.currentDir=dir,__SCANP.lastError=\"\";')
        assert src.count(p17i_old) == 1, "P17i 锚点数量异常"
        src = src.replace(p17i_old, p17i_new)

        p17j_old = '.catch(x=>songloft.log.warn('
        p17j_new = '.catch(x=>{__SCANP.lastError=String(x);songloft.log.warn('
        assert src.count(p17j_old) == 1, "P17j 锚点数量异常"
        src = src.replace(p17j_old, p17j_new)

        p17k_old = '+String(x)));'
        p17k_new = '+String(x))});'
        assert src.count(p17k_old) == 1, "P17k 锚点数量异常"
        src = src.replace(p17k_old, p17k_new)

        p17l_old = 'lastScanAt:t.settings.lastScanAt||0,reused:__SH_REUSE||0,'
        p17l_new = 'lastScanAt:t.settings.lastScanAt||0,lastError:__SCANP.lastError||"",reused:__SH_REUSE||0,'
        assert src.count(p17l_old) == 1, "P17l 锚点数量异常"
        src = src.replace(p17l_old, p17l_new)

    # ---- v1.3.32 第四组：自定义合集 / 合集包 / 打散 / 主页筛选 ----
    # 设计：
    #   - 定义存 settings.customCollections=[{id,name,memberIds}] 与 collectionPacks=[{id,name,collectionIds}]，
    #     与扫描缓存完全解耦 —— 重扫/「仅忽略合集缓存」/清缓存均不影响，持久化天然成立。
    #   - __CUSTOM_MERGE(t,s)：仿 __SHORTS_MERGE，把定义物化为虚拟书（virt:"custom"/"pack"，
    #     id 前缀 __custom_/__pack_），flatten 成员章节（播放直接可用），隐藏成员（mergedInto/hidden）。
    #     幂等：先移除旧虚拟书并解除其成员隐藏，再按最新定义重建；打散=删定义后重跑。
    #   - 三处 __SHORTS_MERGE 调用点（全量扫描 / 缓存加载 / 部分重扫）之后都重建自定义合集。
    if not globals().get("SKIP_G4", False):
        # G4a: 构造函数 settings 默认值补两个数组键 + 全局设置镜像 __CSET
        g4a_old = ('this.settings={favorites:[],recentlyPlayed:[],'
                   'uiPrefs:{viewDesktop:"large",viewMobile:"large"},titleOverrides:{},playbackRates:{}}')
        g4a_new = ('this.settings={favorites:[],recentlyPlayed:[],'
                   'uiPrefs:{viewDesktop:"large",viewMobile:"large"},titleOverrides:{},playbackRates:{},'
                   'customCollections:[],collectionPacks:[],ignoredShortsMembers:[]};__CSET=this.settings')
        assert src.count(g4a_old) == 1, "G4a 锚点数量异常"
        src = src.replace(g4a_old, g4a_new)

        # G4b: __MS 深合并补两个数组键（数组必须整体替换，不能 Object.assign）
        g4b_old = 'bookEdits:Object.assign({},a&&a.bookEdits,b.bookEdits)})}'
        g4b_new = ('bookEdits:Object.assign({},a&&a.bookEdits,b.bookEdits),'
                   'customCollections:Array.isArray(b.customCollections)?b.customCollections:(a&&a.customCollections||[]),'
                   'collectionPacks:Array.isArray(b.collectionPacks)?b.collectionPacks:(a&&a.collectionPacks||[]),'
                   'ignoredShortsMembers:Array.isArray(b.ignoredShortsMembers)?b.ignoredShortsMembers:(a&&a.ignoredShortsMembers||[])})}')
        assert src.count(g4b_old) == 1, "G4b 锚点数量异常"
        src = src.replace(g4b_old, g4b_new)

        # G4c: __CUSTOM_MERGE 函数 + saveSettings 同步 __CSET
        g4c_old = 'var K="audiobook_settings_v1"'
        g4c_new = (
            'var __CSET=null;'
            'function __CUSTOM_MERGE(t,s){s=s||__CSET||{};'
            'var cols=Array.isArray(s.customCollections)?s.customCollections:[],'
            'packs=Array.isArray(s.collectionPacks)?s.collectionPacks:[];'
            'for(var i=t.books.length-1;i>=0;i--){var b=t.books[i];'
            'if(b.virt==="custom"||b.virt==="pack"){t.books.splice(i,1);delete t.chaptersByBookId[b.id]}}'
            'for(var i2=0;i2<t.books.length;i2++){var b2=t.books[i2];'
            'if(b2.virt==="custom"||b2.virt==="pack")continue;'
            'if(b2.mergedInto&&(String(b2.mergedInto).indexOf("__custom_")===0||String(b2.mergedInto).indexOf("__pack_")===0)){b2.mergedInto="";b2.hidden=!1}}'
            'var built={};'
            'for(var ci=0;ci<cols.length;ci++){var c=cols[ci];if(!c||!c.id)continue;'
            'var vid="__custom_"+c.id,cs=[],size=0,upd=0,cover="",names=[],mids=[],'
            'mem=Array.isArray(c.memberIds)?c.memberIds:[];'
            'for(var mi=0;mi<mem.length;mi++){var mb0=null;'
            'for(var z=0;z<t.books.length;z++)if(t.books[z].id===mem[mi]){mb0=t.books[z];break}'
            'if(!mb0)continue;'
            'mids.push(mb0.id);'
            'var chs=(t.chaptersByBookId[mb0.id]||[]).slice();'
            'chs.sort(function(a,c2){return (a.index||0)-(c2.index||0)});'
            'for(var m=0;m<chs.length;m++){var c3=chs[m];'
            'cs.push({id:c3.id,index:0,title:"\\u300A"+(mb0.title||"")+"\\u300B"+(c3.title||""),duration:c3.duration,fileSize:c3.fileSize,fileRelPath:c3.fileRelPath,modTime:c3.modTime})}'
            'if(!cover&&mb0.coverUrl)cover=mb0.coverUrl;'
            'names.push(mb0.title||"");size+=mb0.totalSize||0;if((mb0.updatedAt||0)>upd)upd=mb0.updatedAt}'
            'if(!mids.length)continue;'
            'for(var k=0;k<cs.length;k++)cs[k].index=k+1;'
            'var vb={id:vid,virt:"custom",memberIds:mids,title:c.name||"\\u81EA\\u5B9A\\u4E49\\u5408\\u96C6",author:"\\u5408\\u96C6",coverUrl:cover,coverRatio:"",description:"\\u81EA\\u5B9A\\u4E49\\u5408\\u96C6\\uFF0C\\u5171 "+mids.length+" \\u672C\\uFF1A"+names.join("\\u3001"),category:"\\u81EA\\u5B9A\\u4E49\\u5408\\u96C6",tags:[],updatedAt:upd||Date.now(),chapterCount:cs.length,totalSize:size,folderRelPath:"",isMisc:!0};'
            'built[vid]=vb;t.chaptersByBookId[vid]=cs;t.books.push(vb);'
            'for(var h=0;h<mids.length;h++)for(var z2=0;z2<t.books.length;z2++){var bb=t.books[z2];'
            'if(bb.id===mids[h]&&bb!==vb){bb.mergedInto=vid;bb.hidden=!0;break}}}'
            'for(var pi=0;pi<packs.length;pi++){var p=packs[pi];if(!p||!p.id)continue;'
            'var pvid="__pack_"+p.id,pcs=[],psize=0,pupd=0,pnames=[],pmids=[],pcover="",'
            'pmems=Array.isArray(p.memberIds)?p.memberIds:(Array.isArray(p.collectionIds)?p.collectionIds:[]);'
            'for(var qi=0;qi<pmems.length;qi++){var mpb=null;'
            'for(var z3=0;z3<t.books.length;z3++)if(t.books[z3].id===pmems[qi]){mpb=t.books[z3];break}'
            'if(!mpb||mpb.virt==="pack")continue;'
            'pmids.push(mpb.id);'
            'var chs2=(t.chaptersByBookId[mpb.id]||[]).slice();'
            'chs2.sort(function(a,b){return (a.index||0)-(b.index||0)});'
            'for(var m3=0;m3<chs2.length;m3++){var c4=chs2[m3];'
            'pcs.push({id:c4.id,index:0,title:"\\u300A"+(mpb.title||"")+"\\u300B"+(c4.title||""),duration:c4.duration,fileSize:c4.fileSize,fileRelPath:c4.fileRelPath,modTime:c4.modTime})}'
            'if(!pcover&&mpb.coverUrl)pcover=mpb.coverUrl;'
            'pnames.push(mpb.title||"");psize+=mpb.totalSize||0;if((mpb.updatedAt||0)>pupd)pupd=mpb.updatedAt}'
            'if(!pmids.length)continue;'
            'for(var k3=0;k3<pcs.length;k3++)pcs[k3].index=k3+1;'
            'var pv={id:pvid,virt:"pack",memberIds:pmids,title:p.name||"\\u8FDE\\u64AD\\u6E05\\u5355",author:"\\u8FDE\\u64AD",coverUrl:pcover,coverRatio:"",description:"\\u8FDE\\u64AD\\u6E05\\u5355\\uFF0C\\u6309\\u987A\\u5E8F\\u5305\\u542B "+pmids.length+" \\u672C\\uFF1A"+pnames.join("\\u3001"),category:"\\u5408\\u96C6\\u5305",tags:[],updatedAt:pupd||Date.now(),chapterCount:pcs.length,totalSize:psize,folderRelPath:"",isMisc:!0};'
            't.chaptersByBookId[pvid]=pcs;t.books.push(pv)}'
            '}\n'
            'var K="audiobook_settings_v1"')
        assert src.count(g4c_old) == 1, "G4c 锚点数量异常"
        src = src.replace(g4c_old, g4c_new)

        g4c2_old = 'async saveSettings(){try{await songloft.storage.set(K,this.settings)}catch(t){'
        g4c2_new = ('async saveSettings(){__CSET=this.settings;'
                    'try{await songloft.storage.set(K,this.settings)}catch(t){')
        assert src.count(g4c2_old) == 1, "G4c2 saveSettings 锚点数量异常"
        src = src.replace(g4c2_old, g4c2_new)

        # G4d: 三处 __SHORTS_MERGE 调用点后重建自定义合集
        g4d1_old = '__SHORTS_MERGE(t);return __SCANP.scanning=!1,'
        g4d1_new = '__SHORTS_MERGE(t),__CUSTOM_MERGE(t,t.settings);return __SCANP.scanning=!1,'
        assert src.count(g4d1_old) == 1, "G4d1 锚点数量异常"
        src = src.replace(g4d1_old, g4d1_new)

        g4d2_old = '__SHORTS_MERGE(base);this.books=base.books;'
        g4d2_new = '__SHORTS_MERGE(base),__CUSTOM_MERGE(base,this.settings);this.books=base.books;'
        assert src.count(g4d2_old) == 1, "G4d2 锚点数量异常"
        src = src.replace(g4d2_old, g4d2_new)

        g4d3_old = '__SHORTS_MERGE(out);this.books=out.books;'
        g4d3_new = '__SHORTS_MERGE(out),__CUSTOM_MERGE(out,this.settings);this.books=out.books;'
        assert src.count(g4d3_old) == 1, "G4d3 锚点数量异常"
        src = src.replace(g4d3_old, g4d3_new)

        # G4e: __delBook 支持 custom/pack —— 打散（只删定义，成员恢复显示，不删任何文件）
        g4e_old = ('var p=o.folderRelPath;if(!p||typeof p!=="string"||p.indexOf(M)!==0)throw new Error('
                   + _qs("路径非法，拒绝删除") + ');')
        g4e_new = ('if(o.virt==="custom"||o.virt==="pack"){'
                   'var __cid=(o.id||"").replace(/^__(?:custom|pack)_/,"");'
                   't.settings.customCollections=(t.settings.customCollections||[]).filter(function(c){return c.id!==__cid});'
                   't.settings.collectionPacks=(t.settings.collectionPacks||[]).filter(function(c){return c.id!==__cid});'
                   'await t.saveSettings();__CUSTOM_MERGE(t,t.settings);'
                   'try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
                   'return{filesFailed:0,dirsTotal:0,dirsRemoved:0,dirGone:!1,fallback:"",keptFolder:!0,audioRemoved:0,scatter:!0}}'
                   + g4e_old)
        assert src.count(g4e_old) == 1, "G4e 锚点数量异常"
        src = src.replace(g4e_old, g4e_new)

        # G4f: DELETE 响应补 scatter 字段
        g4f_old = 'collection:r.collection||!1,memberCount:r.memberCount||0}}'
        g4f_new = 'collection:r.collection||!1,memberCount:r.memberCount||0,scatter:r.scatter||!1}}'
        assert src.count(g4f_old) == 1, "G4f 锚点数量异常"
        src = src.replace(g4f_old, g4f_new)

        # G4g: 合集管理路由 —— 列表 / 创建（collection|pack）/ 打散
        g4g_old = 'return f({success:!0,data:{restored:restored,missing:missing}})}),'
        g4g_new = g4g_old + (
            's.get("/api/collections",async()=>f({success:!0,data:{collections:t.settings.customCollections||[],packs:t.settings.collectionPacks||[]}})),'
            's.post("/api/collections",async o=>{'
            'let e=typeof o.body=="string"?JSON.parse(o.body):o.body||{},'
            'name=String(e.name||"").trim(),type=e.type==="pack"?"pack":"collection",ids=(e.ids||[]).map(String);'
            'if(!name)return h("' + _u("名称不能为空") + '",400);'
            'if(!ids.length)return h("' + _u("请先勾选成员") + '",400);'
            'var nid=Date.now().toString(36)+Math.random().toString(36).slice(2,6);'
            'if(type==="pack"){'
            't.settings.collectionPacks||(t.settings.collectionPacks=[]);'
            't.settings.collectionPacks.push({id:nid,name:name,memberIds:ids})}'
            'else{t.settings.customCollections||(t.settings.customCollections=[]);'
            't.settings.customCollections.push({id:nid,name:name,memberIds:ids})}'
            'await t.saveSettings();__CUSTOM_MERGE(t,t.settings);'
            'try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'return f({success:!0,data:{id:nid,type:type}})}),'
            's.post("/api/collections/:id/scatter",async(o,e)=>{var cid=e.id;'
            't.settings.customCollections=(t.settings.customCollections||[]).filter(function(c){return c.id!==cid});'
            't.settings.collectionPacks=(t.settings.collectionPacks||[]).filter(function(c){return c.id!==cid});'
            'await t.saveSettings();__CUSTOM_MERGE(t,t.settings);'
            'try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'return f({success:!0,data:{id:cid,scatter:!0}})}),'
            's.get("/api/books/:id/members",async(o,e)=>{var bk=null;'
            'for(var z=0;z<t.books.length;z++)if(t.books[z].id===e.id){bk=t.books[z];break}'
            'if(!bk||!bk.virt)return h("' + _u("仅合集可打散") + '",400);'
            'var ms=[],ma=bk.memberIds||[];'
            'for(var q=0;q<ma.length;q++){var b2=null;'
            'for(var z2=0;z2<t.books.length;z2++)if(t.books[z2].id===ma[q]){b2=t.books[z2];break}'
            'ms.push({id:ma[q],title:(b2&&b2.title)||ma[q]})}'
            'return f({success:!0,data:{id:bk.id,virt:bk.virt,title:bk.title,members:ms}})}),'
            's.post("/api/books/:id/scatter",async(o,e)=>{var bk=null;'
            'for(var z=0;z<t.books.length;z++)if(t.books[z].id===e.id){bk=t.books[z];break}'
            'if(!bk||!bk.virt)return h("' + _u("仅合集可打散") + '",400);'
            'var body={};try{body=typeof o.body=="string"?JSON.parse(o.body):o.body||{}}catch(_){}'
            'var sel=(body.ids||[]).map(String),n0=(bk.memberIds||[]).length;'
            'if(bk.virt==="shorts"){'
            'var ig=t.settings.ignoredShortsMembers||(t.settings.ignoredShortsMembers=[]);'
            'var mm=sel.length?sel:(bk.memberIds||[]);'
            'for(var q2=0;q2<mm.length;q2++)if(ig.indexOf(mm[q2])<0)ig.push(mm[q2]);'
            'await t.saveSettings();__SHORTS_MERGE(t),__CUSTOM_MERGE(t,t.settings);}'
            'else{var cid2=String(e.id).replace(/^__(?:custom|pack)_/,""),cf=null,cl2=null;'
            'if(sel.length){cl2=bk.virt==="custom"?t.settings.customCollections:t.settings.collectionPacks;'
            'for(var z3=0;z3<cl2.length;z3++)if(cl2[z3].id===cid2){cf=cl2[z3];break}}'
            'if(cf){cf.memberIds=(cf.memberIds||[]).filter(function(x){return sel.indexOf(x)<0});'
            'if(!cf.memberIds.length){'
            'if(bk.virt==="custom")t.settings.customCollections=t.settings.customCollections.filter(function(c){return c.id!==cid2});'
            'else t.settings.collectionPacks=t.settings.collectionPacks.filter(function(c){return c.id!==cid2})}}'
            'else if(!sel.length){'
            't.settings.customCollections=(t.settings.customCollections||[]).filter(function(c){return c.id!==cid2});'
            't.settings.collectionPacks=(t.settings.collectionPacks||[]).filter(function(c){return c.id!==cid2})}'
            'await t.saveSettings();__SHORTS_MERGE(t),__CUSTOM_MERGE(t,t.settings);}'
            'try{await N({books:t.books,chaptersByBookId:t.chaptersByBookId})}catch(_){}'
            'var bk2=null;for(var z4=0;z4<t.books.length;z4++)if(t.books[z4].id===e.id){bk2=t.books[z4];break}'
            'return f({success:!0,data:{id:e.id,scatter:!0,virt:bk.virt,partial:sel.length>0,'
            'removed:sel.length?sel.length:n0,remaining:bk2?(bk2.memberIds||[]).length:0,gone:!bk2}})}),')
        assert src.count(g4g_old) == 1, "G4g 锚点数量异常"
        src = src.replace(g4g_old, g4g_new)

        # G4h: list() 支持 libFilter（shorts/custom/pack/fav）
        g4h_old = ',t.favoritesOnly){let c=new Set(this.settings.favorites);a=a.filter(u=>c.has(u.id))}'
        g4h_new = (g4h_old +
                   ';if(t.libFilter==="shorts")a=a.filter(c=>c.virt==="shorts");'
                   'else if(t.libFilter==="custom")a=a.filter(c=>c.virt==="custom");'
                   'else if(t.libFilter==="pack")a=a.filter(c=>c.virt==="pack");'
                   'else if(t.libFilter==="fav"&&!t.favoritesOnly){let c2=new Set(this.settings.favorites);a=a.filter(u=>c2.has(u.id))}')
        assert src.count(g4h_old) == 1, "G4h 锚点数量异常"
        src = src.replace(g4h_old, g4h_new)

        # G4i: /api/books 路由透传 libFilter
        g4i_old = 'sortBy:e.sortBy||"updatedAt",order:e.order||""'
        g4i_new = 'sortBy:e.sortBy||"updatedAt",order:e.order||"",libFilter:e.libFilter||""'
        assert src.count(g4i_old) == 1, "G4i 锚点数量异常"
        src = src.replace(g4i_old, g4i_new)

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

    # H16: 顶栏扫描进度徽标（扫描中显示进度，空闲隐藏）
    h16_old = '        <button id="btnSettings" class="btn btn-ghost" title="设置">⚙️</button>'
    h16_new = ('        <span id="scanStatus" class="scan-pill" hidden></span>\n'
               '        <button id="btnSettings" class="btn btn-ghost" title="设置">⚙️</button>')
    html = rep(html, h16_old, h16_new, "H16")

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
               '            <div class="settings-pref-desc">同一分类目录下章节数不超过阈值的有声书将自动合并为一本「短篇合集」，可显著减少书目数量（默认 ≤4 章）。修改后不会自动重新扫描：用主页「重新扫描」→「仅重建合集」即可按新阈值即时重算（不读盘，秒级）。播放进度会双向迁移，不丢失。</div>\n'
               '          </div>')
    html = rep(html, h13_old, h13_new, "H13")

    # H17: 设置弹窗新增「自动扫描」区块 —— 插件被宿主重载时是否自动全库扫描
    h17_old = ('            <div class="settings-pref-desc">同一分类目录下章节数不超过阈值的有声书将自动合并为一本「短篇合集」，'
               '可显著减少书目数量（默认 ≤4 章）。修改后不会自动重新扫描：用主页「重新扫描」→「仅重建合集」即可按新阈值即时重算（不读盘，秒级）。播放进度会双向迁移，不丢失。</div>\n'
               '          </div>')
    h17_new = (h17_old +
               '          <div class="settings-section">\n'
               '            <div class="settings-section-title">自动扫描</div>\n'
               '            <div class="settings-pref-row">\n'
               '              <label>插件重启时\n'
               '                <select id="prefInitScan">\n'
               '                  <option value="stale">距上次扫描超过阈值才扫（推荐）</option>\n'
               '                  <option value="always">每次都全库扫描（原版行为）</option>\n'
               '                  <option value="empty">仅当书库缓存为空时扫描</option>\n'
               '                  <option value="never">从不自动扫描</option>\n'
               '                </select>\n'
               '              </label>\n'
               '              <label>阈值（小时）\n'
               '                <input type="number" id="prefInitScanHours" min="1" max="720" style="width:80px" />\n'
               '              </label>\n'
               '            </div>\n'
               '            <div class="settings-pref-desc">宿主每天会自动检查并重载插件，原版每次启动都无条件全库重扫（大书库很耗时）。'
               '保留「距上次扫描超过阈值才扫」即可避免无谓扫描；新增或移动音频后，用主页「重新扫描」按目录刷新更快。</div>\n'
               '          </div>')
    html = rep(html, h17_old, h17_new, "H17")

    # H14: 主页「加载」按钮改名「重新扫描」（点击改为弹出扫描范围选择，不再直接全库重扫）
    h14_old = '重新扫描本地目录">加载</button>'
    h14_new = '重新扫描本地目录">重新扫描</button>'
    html = rep(html, h14_old, h14_new, "H14")
    if "点击右上角「加载」" in html:
        html = html.replace("点击右上角「加载」", "点击右上角「重新扫描」")

    # H15: 重新扫描弹窗（全部 / 指定文件夹 —— 可折叠目录树，懒加载逐级展开）
    h15_old = "    <!-- 设置弹窗 -->"
    h15_new = ('    <!-- 重新扫描弹窗 -->\n'
               '    <div id="rescanOverlay" class="edit-overlay" hidden>\n'
               '      <div class="edit-modal delete-modal">\n'
               '        <h3>重新扫描</h3>\n'
               '        <p class="delete-info">选择扫描范围：可扫描整个书库，或只重新扫描某个文件夹（新增/移动文件后用它按需刷新，比全库扫描快得多）。</p>\n'
               '        <div class="rtree-wrap">\n'
               '          <div class="rtree-head" id="rescanTreeHead">\n'
               '            <div class="rtree-head-text">\n'
               '              <div class="rtree-head-title">指定目录（可选）</div>\n'
               '              <div class="rtree-head-sub">仅扫描选中的目录，留空则扫描整个书库；勾选多个将依次扫描</div>\n'
               '            </div>\n'
               '            <span class="rtree-caret">▾</span>\n'
               '          </div>\n'
               '          <div class="rtree" id="rescanTree"><div class="rtree-msg">加载中...</div></div>\n'
               '          <div class="rtree-msg" id="rescanLastInfo"></div>\n'
               '          <div class="rtree-modes">\n'
               '            <label class="rtree-force"><input type="radio" name="rescanMode" value="auto" checked /> 增量扫描（默认：只重扫有变动的目录，最快）</label>\n'
               '            <label class="rtree-force"><input type="radio" name="rescanMode" value="remerge" /> 仅重建合集（不读盘，按当前阈值重新生成短篇合集，秒级完成）</label>\n'
               '            <label class="rtree-force"><input type="radio" name="rescanMode" value="force" /> 忽略缓存，强制全量扫描（音频被替换但文件名没变时用）</label>\n'
               '          </div>\n'
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

    # H16: 移除首页「全部书籍」后的 ⓘ 目录结构说明按钮（说明已迁移到重新扫描弹窗）
    h16_old = ('            <h2 class="section-title" id="booksSectionTitle">全部书籍</h2>\n'
               '            <button class="info-btn" id="dirInfoBtn" title="目录结构说明">ⓘ</button>\n')
    h16_new = '            <h2 class="section-title" id="booksSectionTitle">全部书籍</h2>\n'
    html = rep(html, h16_old, h16_new, "H16")

    # H17: 删除旧的「目录结构说明」弹窗（说明已迁移到重新扫描弹窗；HTML 整体移除，避免死节点）
    # v1.3.28 及之前的正则在第一个 "    </div>\n" 就截断，只删掉弹窗外壳，
    # 残留 dir-info-body 正文和多余闭合标签成孤儿节点直接渲染在页面底部 —— v1.3.29 改为整体移除
    import re as _re
    _di_before = html.count('id="dirInfoOverlay"')
    assert _di_before == 1, "H17 目录结构弹窗锚点异常"
    html = _re.sub(r'    <!-- 目录结构说明弹窗 -->.*?(?=    <!-- 悬浮播放器 FAB -->)', '', html, count=1, flags=_re.S)
    assert html.count('id="dirInfoOverlay"') == 0, "H17 目录结构弹窗移除异常"
    assert 'dir-info-body' not in html, "H17 目录结构正文残留"

    # H15b: 在重新扫描弹窗标题后插入「书库目录结构与扫描规则」说明（按当前扫描规则重新生成）
    h15b_old = ('        <h3>重新扫描</h3>\n'
                '        <p class="delete-info">选择扫描范围：可扫描整个书库，或只重新扫描某个文件夹'
                '（新增/移动文件后用它按需刷新，比全库扫描快得多）。</p>\n')
    h15b_new = ('        <h3>重新扫描</h3>\n'
                '        <div class="rescan-dirinfo">\n'
                '          <details open>\n'
                '            <summary>📁 书库目录结构与扫描规则</summary>\n'
                '            <ul>\n'
                '              <li>有声书放在书库根目录下，每本书一个文件夹（默认 <code>/app/audiobook</code>，取决于宿主挂载位置）。</li>\n'
                '              <li>最多递归 20 层子目录；自动忽略系统目录：<code>@eaDir</code>、<code>@SynologyResource</code>、<code>#recycle</code>、<code>@sharebin</code>、<code>_ARCHIVE_TRASH</code>、<code>_DEDUPE_TRASH</code>。</li>\n'
                '              <li>封面优先识别 <code>cover.*</code> / <code>folder.*</code> / <code>封面.*</code>；<code>metadata.json</code> 存简介/类型/标签/作者（可在详情页编辑自动生成）。</li>\n'
                '              <li>含子目录的目录：每个子目录各成一本书，其根目录下的散落音频归入「目录名-未分类」一本。</li>\n'
                '              <li>同目录下章节数 ≤ 阈值的多本书，自动合并为一本「短篇合集」（设置可调阈值，改后用下方「仅重建合集」即时生效，不读盘）。</li>\n'
                '            </ul>\n'
                '          </details>\n'
                '        </div>\n'
                '        <p class="delete-info">选择扫描范围：可扫描整个书库，或只重新扫描某个文件夹'
                '（新增/移动文件后用它按需刷新，比全库扫描快得多）。</p>\n')
    html = rep(html, h15b_old, h15b_new, "H15b")

    # H18: 设置弹窗「帮助文档」下方增加「修改版说明」，指向 GitHub README
    h18_old = ('              <div class="settings-about-row">\n'
               '                <span class="settings-about-label">帮助文档</span>\n'
               '                <a href="https://mp.weixin.qq.com/s/9eLpiWXsIzbmS1MI_eolHg" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none">📖</a>\n'
               '              </div>\n')
    h18_new = ('              <div class="settings-about-row">\n'
               '                <span class="settings-about-label">帮助文档</span>\n'
               '                <a href="https://mp.weixin.qq.com/s/9eLpiWXsIzbmS1MI_eolHg" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none">📖</a>\n'
               '              </div>\n'
               '              <div class="settings-about-row">\n'
               '                <span class="settings-about-label">修改版说明</span>\n'
               '                <a href="https://github.com/nbnb9527/audiobook-jsplugin" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none">📝 nbnb9527 修改版（GitHub）</a>\n'
               '              </div>\n')
    html = rep(html, h18_old, h18_new, "H18")

    # H19: 删除确认弹窗的警告语加上 id，供前端按书籍类型动态改写
    h19_old = ('        <p class="delete-warn">⚠️ 此操作将<strong>永久删除</strong>该书所在的整个文件夹及其全部音频、图片等文件，'
               '<strong>删除后不可恢复</strong>。</p>\n')
    h19_new = ('        <p class="delete-warn" id="delBookWarn">⚠️ 此操作将<strong>永久删除</strong>该书所在的整个文件夹及其全部音频、图片等文件，'
               '<strong>删除后不可恢复</strong>。</p>\n')
    html = rep(html, h19_old, h19_new, "H19")

    # HG3a: 「全部书籍」标题后加「多选」切换按钮（第三组）
    hg3a_old = '<h2 class="section-title" id="booksSectionTitle">全部书籍</h2>'
    hg3a_new = (hg3a_old + '\n            <button class="ms-toggle" id="msToggle" type="button" '
                'title="进入/退出多选模式">多选</button>')
    html = rep(html, hg3a_old, hg3a_new, "HG3a")

    # HG3b: bookGrid 前插入批量操作工具栏（多选模式下显示）
    hg3b_old = '<div class="book-grid" id="bookGrid" aria-live="polite"></div>'
    hg3b_new = ('<div class="batch-bar" id="batchBar" hidden>\n'
                '          <span class="batch-count" id="batchCount">已选 0 本</span>\n'
                '          <button id="msPageAll" type="button">本页全选</button>\n'
                '          <button id="msPageInvert" type="button">本页反选</button>\n'
                '          <button id="msAll" type="button">全选全部</button>\n'
                '          <button id="msAllInvert" type="button">反选全部</button>\n'
                '          <button id="msClear" type="button">清除选择</button>\n'
                '          <span class="batch-sep"></span>\n'
                '          <button id="msFavAdd" type="button">批量收藏</button>\n'
                '          <button id="msFavDel" type="button">批量取消收藏</button>\n'
                '          <button id="msEdit" type="button">批量编辑</button>\n'
                '          <button id="msSaveCol" type="button" title="存为合集：把选中的书组成一个自定义合集，成员书在列表中隐藏、只显示合集，可随时打散恢复">存为合集</button>\n'
                '          <button id="msSavePack" type="button" title="创建合集包：类似播放列表，把选中的书按勾选顺序连成连续章节连播，原书仍正常显示">创建合集包</button>\n'
                '          <button id="msDelete" type="button" class="batch-danger">批量删除</button>\n'
                '        </div>\n'
                '        ' + hg3b_old)
    html = rep(html, hg3b_old, hg3b_new, "HG3b")

    # HG3c: 批量编辑弹窗（复用 .edit-overlay/.edit-modal 样式）
    hg3c_old = '    <!-- 删除确认弹窗 -->\n'
    hg3c_new = ('    <!-- 批量编辑弹窗 -->\n'
                '    <div id="batchEditOverlay" class="edit-overlay" hidden>\n'
                '      <div class="edit-modal batch-edit-modal">\n'
                '        <h3>批量编辑（已选 <span id="beditCount">0</span> 本）</h3>\n'
                '        <div class="bedit-row"><label>简介前插</label>'
                '<textarea id="beditDescPre" rows="2" placeholder="插入到简介开头，留空跳过"></textarea></div>\n'
                '        <div class="bedit-row"><label>简介后插</label>'
                '<textarea id="beditDescApp" rows="2" placeholder="插入到简介末尾，留空跳过"></textarea></div>\n'
                '        <div class="bedit-row"><label>分类</label><div class="bedit-fields">'
                '<select id="beditCatMode"><option value="">不修改</option><option value="append">追加</option>'
                '<option value="replace">替换</option><option value="reset">重设（还原扫描值）</option></select>'
                '<input id="beditCatVal" placeholder="分类值"></div></div>\n'
                '        <div class="bedit-row"><label>标签</label><div class="bedit-fields">'
                '<select id="beditTagMode"><option value="">不修改</option><option value="append">追加</option>'
                '<option value="replace">替换</option><option value="reset">重设（还原扫描值）</option></select>'
                '<input id="beditTagVal" placeholder="多个标签用逗号分隔"></div></div>\n'
                '        <div class="bedit-row"><label>作者</label><div class="bedit-fields">'
                '<select id="beditAuthMode"><option value="">不修改</option><option value="append">追加</option>'
                '<option value="replace">替换</option><option value="reset">重设（还原扫描值）</option></select>'
                '<input id="beditAuthVal" placeholder="作者名"></div></div>\n'
                '        <div class="bedit-row"><label>封面</label><label class="bedit-cover">'
                '<input type="checkbox" id="beditCoverReset"> 重设封面（改过封面的书恢复原封面，无备份则跳过）</label></div>\n'
                '        <p class="bedit-note">简介/分类/标签/作者的修改保存在覆盖层，重新扫描不会丢失；'
                '「重设」清除覆盖恢复为扫描值。封面重设仅对改过封面且留有备份的书生效。</p>\n'
                '        <div class="edit-actions">\n'
                '          <button class="btn btn-ghost" id="beditCancel" type="button">取消</button>\n'
                '          <button class="btn btn-primary" id="beditApply" type="button">应用到已选书籍</button>\n'
                '        </div>\n'
                '      </div>\n'
                '    </div>\n'
                + hg3c_old)
    html = rep(html, hg3c_old, hg3c_new, "HG3c")

    # HG4a: 新建合集/合集包弹窗（第四组；v1.3.35 移除类型下拉——点哪个按钮固定创建哪种，
    #       弹窗只显示对应类型的说明文字）
    hg4a_old = '    <!-- 删除确认弹窗 -->\n'
    hg4a_new = ('    <!-- 新建合集/合集包弹窗 -->\n'
                '    <div id="colOverlay" class="edit-overlay" hidden>\n'
                '      <div class="edit-modal">\n'
                '        <h3 id="colTitle">存为合集</h3>\n'
                '        <div class="edit-field">\n'
                '          <label for="colName">名称</label>\n'
                '          <input type="text" id="colName" placeholder="如：睡前故事" />\n'
                '        </div>\n'
                '        <div class="col-hint" id="colHint"></div>\n'
                '        <div class="edit-actions">\n'
                '          <button class="btn btn-ghost" id="colCancel" type="button">取消</button>\n'
                '          <button class="btn btn-primary" id="colCreate" type="button">创建</button>\n'
                '        </div>\n'
                '      </div>\n'
                '    </div>\n'
                + hg4a_old)
    html = rep(html, hg4a_old, hg4a_new, "HG4a")

    # HG4j: 打散合集成员选择弹窗（v1.3.35 —— 可勾选要移出的成员，支持部分打散）
    hg4j_old = '    <!-- 删除确认弹窗 -->\n'
    hg4j_new = ('    <!-- 打散合集成员选择弹窗 -->\n'
                '    <div id="scOverlay" class="edit-overlay" hidden>\n'
                '      <div class="edit-modal">\n'
                '        <h3 id="scTitle">打散合集</h3>\n'
                '        <div class="col-hint" id="scHint"></div>\n'
                '        <div class="edit-field">\n'
                '          <div class="sc-head">\n'
                '            <label class="sc-head-title" style="margin:0">要移出的成员</label>\n'
                '            <button class="btn btn-ghost" id="scToggleAll" type="button">全不选</button>\n'
                '          </div>\n'
                '          <div class="sc-tip">不勾选直接确认 = 打散整个合集</div>\n'
                '          <div id="scList" class="sc-list"></div>\n'
                '        </div>\n'
                '        <div class="edit-actions">\n'
                '          <button class="btn btn-ghost" id="scCancel" type="button">取消</button>\n'
                '          <button class="btn btn-primary" id="scConfirm" type="button">打散选中成员</button>\n'
                '        </div>\n'
                '      </div>\n'
                '    </div>\n'
                + hg4j_old)
    html = rep(html, hg4j_old, hg4j_new, "HG4j")

    # HG4b: 主页筛选下拉（全部/收藏/自动合集/自定义合集/合集包）
    #       v1.3.35：同时删除「只看收藏」复选框——筛选下拉已含「收藏」选项，二者重复
    hg4b_old = ('      <label class="fav-toggle">\n'
                '        <input type="checkbox" id="favoritesOnly" />\n'
                '        只看收藏\n'
                '      </label>\n')
    hg4b_new = ('      <label>\n'
                '        筛选：\n'
                '        <select id="libFilter">\n'
                '          <option value="">全部</option>\n'
                '          <option value="fav">收藏</option>\n'
                '          <option value="shorts">自动合集</option>\n'
                '          <option value="custom">自定义合集</option>\n'
                '          <option value="pack">合集包</option>\n'
                '        </select>\n'
                '      </label>\n')
    html = rep(html, hg4b_old, hg4b_new, "HG4b")

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
        "function __syncViewModeUI(){let s=document.getElementById(\"viewMode\");s&&(s.value=__getViewMode());let pd=document.getElementById(\"prefViewDesktop\");pd&&(pd.value=n.uiPrefs&&n.uiPrefs.viewDesktop||\"large\");let pm=document.getElementById(\"prefViewMobile\");pm&&(pm.value=n.uiPrefs&&n.uiPrefs.viewMobile||\"large\");let pt=document.getElementById(\"prefShortsThreshold\");pt&&!pt.dataset.boundV&&(pt.dataset.boundV=\"1\",pt.addEventListener(\"change\",async()=>{let v=parseInt(pt.value,10)||0;await __savePrefs({shortsMergeThreshold:v});u(v>0?\"\\u5DF2\\u4FDD\\u5B58\\uFF1A\\u9608\\u503C \\u2264\"+v+\" \\u7AE0\\u3002\\u4E0D\\u4F1A\\u81EA\\u52A8\\u626B\\u63CF\\uFF0C\\u70B9\\u300C\\u91CD\\u65B0\\u626B\\u63CF\\u300D\\u5E76\\u9009\\u300C\\u4EC5\\u91CD\\u5EFA\\u5408\\u96C6\\u300D\\u5373\\u53EF\\u751F\\u6548\":\"\\u5DF2\\u5173\\u95ED\\u77ED\\u7BC7\\u5408\\u5E76\\uFF0C\\u70B9\\u300C\\u91CD\\u65B0\\u626B\\u63CF\\u300D\\u5E76\\u9009\\u300C\\u4EC5\\u91CD\\u5EFA\\u5408\\u96C6\\u300D\\u5373\\u53EF\\u89E3\\u6563\\u73B0\\u6709\\u5408\\u96C6\")}));let ip=document.getElementById(\"prefInitScan\");ip&&!ip.dataset.boundV&&(ip.dataset.boundV=\"1\",ip.addEventListener(\"change\",()=>__savePrefs({initScanMode:ip.value})));let hp=document.getElementById(\"prefInitScanHours\");hp&&!hp.dataset.boundV&&(hp.dataset.boundV=\"1\",hp.addEventListener(\"change\",()=>{let v=parseInt(hp.value,10);if(!v||v<1)v=72;hp.value=String(v);__savePrefs({initScanStaleHours:v})}));let cp=document.getElementById(\"editCopyPath\");cp&&!cp.dataset.boundV&&(cp.dataset.boundV=\"1\",cp.addEventListener(\"click\",async()=>{let v=document.getElementById(\"editBookPath\").value||\"\";try{await navigator.clipboard.writeText(v),u(\"\\u5DF2\\u590D\\u5236\\u8DEF\\u5F84\")}catch(e){let i=document.getElementById(\"editBookPath\");i.focus(),i.select();try{document.execCommand(\"copy\"),u(\"\\u5DF2\\u590D\\u5236\\u8DEF\\u5F84\")}catch(_){u(\"\\u590D\\u5236\\u5931\\u8D25\\uFF0C\\u8BF7\\u624B\\u52A8\\u9009\\u62E9\\u590D\\u5236\")}}}));try{let mq=window.matchMedia(\"(max-width:768px)\"),h=()=>{__syncViewModeUI(),fe()};mq.addEventListener?mq.addEventListener(\"change\",h):mq.addListener(h)}catch(_){}}\n"
        "async function __savePrefs(p){n.uiPrefs=Object.assign({viewDesktop:\"large\",viewMobile:\"large\"},n.uiPrefs||{},p),__syncViewModeUI(),fe();try{await y(\"/api/ui-prefs\",{method:\"PUT\",body:JSON.stringify(p),headers:{\"Content-Type\":\"application/json\"}})}catch(e){u(\"\\u4FDD\\u5B58\\u663E\\u793A\\u8BBE\\u7F6E\\u5931\\u8D25\\uFF1A\"+e.message)}}\n"
        "function __relPath(p){if(!p)return\"\";let lp=String(n.libraryPath||\"/app/audiobook\").replace(/\\/+$/,\"\");return p===lp?\"\":p.indexOf(lp+\"/\")===0?p.substring(lp.length+1):p}\n"
        "async function __saveRate(id,rate){if(!id)return;n.playbackRates=n.playbackRates||{};n.playbackRates[id]=rate;try{localStorage.setItem(\"ab_rate_\"+id,String(rate))}catch(_){}try{await y(\"/api/books/\"+id+\"/rate\",{method:\"POST\",body:JSON.stringify({rate:rate}),headers:{\"Content-Type\":\"application/json\"}})}catch(_){}}\n"
        "function __restoreRate(id){if(!id)return;let v=0;try{v=parseFloat(localStorage.getItem(\"ab_rate_\"+id))}catch(_){}if(!v||isNaN(v))v=(n.playbackRates||{})[id]||0;if(!v)v=1;if([.75,1,1.25,1.5,1.75,2].indexOf(v)<0)v=1;n.speed=v;let o=document.getElementById(\"btnSpeedFull\");o&&(o.textContent=v+\"x\");let r=n.audioEl||b();r&&(r.playbackRate=v)}\n"
        "async function __rescanBook(bk){if(!bk)return;let rel=__relPath(bk.folderRelPath);"
        "if(!rel){u(\"\\u8BE5\\u4E66\\u4F4D\\u4E8E\\u4E66\\u5E93\\u6839\\u76EE\\u5F55\\uFF0C"
        "\\u8BF7\\u7528\\u4E3B\\u9875\\u300C\\u91CD\\u65B0\\u626B\\u63CF\\u300D\\u626B\\u63CF\\u6574\\u4E2A\\u4E66\\u5E93\");return}"
        "try{await y(\"/api/rescan\",{method:\"POST\",body:JSON.stringify({dir:rel,force:1}),"
        "headers:{\"Content-Type\":\"application/json\"}})}"
        "catch(e){u(\"\\u542F\\u52A8\\u626B\\u63CF\\u5931\\u8D25\\uFF1A\"+e.message);return}"
        "u(\"\\u5DF2\\u5F00\\u59CB\\u91CD\\u65B0\\u626B\\u63CF\\uFF1A\"+rel);"
        "let k=0,iv=setInterval(async()=>{k++;"
        "try{let st=await y(\"/api/scan-progress\");"
        "if(st.lastError){clearInterval(iv);u(\"\\u626B\\u63CF\\u5931\\u8D25\\uFF1A\"+st.lastError);return}"
        "if(!st.scanning||k>240){clearInterval(iv);"
        "try{await w()}catch(_){}"
        "try{await R(bk.id)}catch(_){}"
        "u(\"\\u626B\\u63CF\\u5B8C\\u6210\")}}catch(e){}},2000)}\n"
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
    #     注意：此处曾插入从未定义的 __initViewMode()，Oe() 启动即抛 ReferenceError，
    #     导致其后的 Y()（最近播放渲染）从不执行、最近播放区块一直隐藏 —— v1.3.30 修复
    j3_old = "function Oe(){b(),De(),k(\"homeView\"),X(),Y()}"
    j3_new = ("function Oe(){b(),De(),k(\"homeView\"),X(),"
              "function(){try{__syncViewModeUI()}catch(_){}}(),Y()}")
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
        "let wn=document.getElementById(\"delBookWarn\");"
        "if(wn){"
        "if(b.virt===\"shorts\"){let n2=(b.memberIds||[]).length;wn.textContent=" + _qs("⚠️ 将删除该合集内 ") + "+n2+" + _qs(" 本短篇各自的文件夹，删除后不可恢复。") + ";}"
        "else if(b.isMisc||b.category===\"\\u672A\\u5206\\u7C7B\"||(b.id||\"\").indexOf(\"__misc__\")>=0){wn.textContent=" + _qs("⚠️ 将删除该文件夹下的全部音频文件，文件夹本身保留。") + ";}"
        "else{wn.textContent=" + _qs("⚠️ 此操作将永久删除该书所在的整个文件夹及其全部音频、图片等文件，删除后不可恢复。") + ";}}"
        "window.__delId=id;o.hidden=!1}\n"
        "async function __doDel(){let id=window.__delId;if(!id)return;"
        "let btn=document.getElementById(\"delConfirmBtn\");btn.disabled=!0,btn.textContent=\"\\u5220\\u9664\\u4E2D...\";"
        # 注意：y() 成功时返回的是响应体的 data 字段（已剥离 success 层），
        # 因此这里只能判断 success===false；写成 !r.success 会因 undefined 而永远误判失败。
        "try{let r=await y(`/api/books/${id}`,{method:\"DELETE\"})||{};"
        "if(r.success===!1)throw new Error(r.error||\"\\u5220\\u9664\\u5931\\u8D25\");"
        "let t=n.books.find(x=>x.id===id);"
        "document.getElementById(\"deleteOverlay\").hidden=!0,window.__delId=null,"
        "u(r.collection?(" + _qs("已删除合集「") + "+(t?t.title:\"\")+" + _qs("」及其 ") + "+r.memberCount+" + _qs(" 本短篇") + ")"
        ":r.keptFolder?(" + _qs("已删除音频文件，文件夹已保留：") + "+(t?t.title:\"\"))"
        ":r.dirGone?(" + _qs("已删除：") + "+(t?t.title:\"\"))"
        ":" + _qs("文件已删除，但文件夹未能移除，请手动清理") + "),await w()}"
        "catch(e){u(\"\\u5220\\u9664\\u5931\\u8D25\\uFF1A\"+e.message)}"
        "finally{btn.disabled=!1,btn.textContent=\"\\u786E\\u8BA4\\u5220\\u9664\"}}\n"
        "function __cardEdit(id){let b=n.books.find(x=>x.id===id);if(!b)return;"
        "window.__editFromList=!0,Fe(b)}\n"
        "async function _e(e){")
    js = rep(js, j14_old, j14_new, "J14")

    # J15: 卡片模板加编辑按钮 + 删除按钮
    #      未分类书（isMisc / category==="未分类" / id 含 __misc__）显示灰色禁用叉（disabled，点击不触发）
    #      v1.3.27 首版此处漏了 title 属性收尾引号（`:"删除"}>`），未闭合属性吞掉后续全部卡片
    #      标记，整页塌缩成一个节点（大/小图标只显示一个、列表显示异常）—— v1.3.30 修复
    j15_old = "        <button class=\"book-card-play\" data-play=\"${t.id}\" title=\"\\u64AD\\u653E\">\\u25B6</button>"
    j15_new = (j15_old + "\n"
               "        <button class=\"book-card-edit\" data-edit=\"${t.id}\" title=\"\\u7F16\\u8F91\">\\u270E</button>\n"
               "        <button class=\"book-card-del\" data-del=\"${t.id}\" title=\""
               "${(t.virt===\"shorts\")?\"\\u5220\\u9664\\u5408\\u96C6\\uFF1A\\u5220\\u9664\\u5176\\u4E0B\\u5404\\u77ED\\u7BC7\\u7684\\u6587\\u4EF6\\u5939\""
               ":(t.isMisc||t.category===\"\\u672A\\u5206\\u7C7B\"||(t.id||\"\").indexOf(\"__misc__\")>=0)?\"\\u5220\\u9664\\u672A\\u5206\\u7C7B\\uFF1A\\u4EC5\\u5220\\u9664\\u97F3\\u9891\\u6587\\u4EF6\\uFF0C\\u4FDD\\u7559\\u6587\\u4EF6\\u5939\""
               ":\"\\u5220\\u9664\"}\">\\u2716</button>")
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

    # J18: 移除旧的「目录结构说明」ⓘ 按钮绑定（说明已迁移到重新扫描弹窗，HTML 也已删除）
    j18_old = ('document.getElementById("dirInfoBtn").addEventListener("click",()=>{document.getElementById("dirInfoOverlay").hidden=!1}),'
               'document.getElementById("dirInfoClose").addEventListener("click",()=>{document.getElementById("dirInfoOverlay").hidden=!0}),'
               'document.getElementById("dirInfoOverlay").addEventListener("click",a=>{a.target===a.currentTarget&&(a.currentTarget.hidden=!0)}),')
    j18_new = ''
    js = rep(js, j18_old, j18_new, "J18")

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

    # ===== v1.3.28 第三组：书籍多选 + 批量删除/收藏/编辑 =====
    # JG3a: 卡片模板左上角加多选复选框（仅多选模式显示），选中态由 window.__sel 驱动
    jg3a_old = '<div class="book-card-cover">'
    jg3a_new = ('<div class="book-card-cover">'
                '<label class="book-card-sel">'
                '<input type="checkbox" class="ms-check" data-ms="${t.id}"'
                '${window.__sel&&window.__sel.has(t.id)?" checked":""}></label>')
    js = rep(js, jg3a_old, jg3a_new, "JG3a")

    # JG3b: 卡片选中态样式类（重渲染时保持高亮）
    jg3b_old = '<div class="book-card" data-id="${t.id}">'
    jg3b_new = '<div class="book-card${window.__sel&&window.__sel.has(t.id)?" ms-on":""}" data-id="${t.id}">'
    js = rep(js, jg3b_old, jg3b_new, "JG3b")

    # JG3c: 卡片点击在多选模式下改为切换选中（复选框/各按钮自身的点击不受影响）
    jg3c_old = ('o.target.closest("[data-fav]")||o.target.closest("[data-play]")'
                '||R(t.getAttribute("data-id"))')
    jg3c_new = ('o.target.closest("[data-fav]")||o.target.closest("[data-play]")'
                '||o.target.closest(".book-card-sel")'
                '||(window.__msMode?__toggleSel(t.getAttribute("data-id")):R(t.getAttribute("data-id")))')
    js = rep(js, jg3c_old, jg3c_new, "JG3c")

    # JG3d: 复选框 change 事件 → __msCheck（click 冒泡到卡片会切换两次，须拦截）
    jg3d_old = '__cardEdit(t.getAttribute("data-edit"))})}),__syncPlayBtns()}}'
    jg3d_new = ('__cardEdit(t.getAttribute("data-edit"))})}),'
                'e.querySelectorAll(".ms-check").forEach(t=>{'
                't.addEventListener("click",o=>o.stopPropagation()),'
                't.addEventListener("change",()=>{'
                '__msCheck(t.getAttribute("data-ms"),t.checked)})}),'
                '__syncPlayBtns()}}')
    js = rep(js, jg3d_old, jg3d_new, "JG3d")

    # JG3e: 多选核心逻辑 + 工具栏/批量编辑弹窗绑定（fe() 渲染后调用 __msBindBar 幂等绑定）
    jg3e_old = 'async function Y(){try{let t=(await y("/api/recently-played")).items||[]'
    jg3e_new = '''window.__sel=new Set(),window.__msMode=!1;
function __msCount(){let e=document.getElementById("batchCount");e&&(e.textContent="\\u5df2\\u9009 "+window.__sel.size+" \\u672c")}
function __msPaint(id){let c=document.querySelector('.ms-check[data-ms="'+id+'"]');if(c){c.checked=window.__sel.has(id);let d=c.closest(".book-card");d&&d.classList.toggle("ms-on",window.__sel.has(id))}}
function __toggleSel(id){window.__sel.has(id)?window.__sel.delete(id):window.__sel.add(id),__msCount(),__msPaint(id)}
function __msCheck(id,on){on?window.__sel.add(id):window.__sel.delete(id),__msCount(),__msPaint(id)}
function __msAfter(){__msCount(),fe()}
function __msSetMode(e){window.__msMode=e;let t=document.getElementById("batchBar");t&&(t.hidden=!e);let o=document.getElementById("bookGrid");o&&o.classList.toggle("ms-mode",e);let d=document.getElementById("msToggle");d&&d.classList.toggle("on",e),e||window.__sel.clear(),__msCount(),fe()}
async function __msSelectAll(){try{let e=(document.getElementById("favoritesOnly")||{}).checked||!1,t=await y("/api/book-ids?keyword="+encodeURIComponent(n.keyword||"")+(e?"&favoritesOnly=true":""));(t.ids||[]).forEach(e=>window.__sel.add(e.id)),__msAfter(),u("\\u5df2\\u5168\\u9009 "+(t.ids||[]).length+" \\u672c\\uff08\\u542b\\u5176\\u4ed6\\u9875\\uff09")}catch(e){u(e.message)}}
function __msFav(e){let t=Array.from(window.__sel);if(!t.length){u("\\u8bf7\\u5148\\u52fe\\u9009\\u4e66\\u7c4d");return}y("/api/batch/favorite",{method:"POST",body:JSON.stringify({ids:t,on:e})}).then(e=>{u((e.changed?"\\u5df2\\u66f4\\u65b0 "+e.changed+" \\u672c\\u6536\\u85cf\\u72b6\\u6001":"\\u6536\\u85cf\\u72b6\\u6001\\u65e0\\u53d8\\u5316")),window.__msMode&&fe(),w()}).catch(e=>u("\\u6279\\u91cf\\u6536\\u85cf\\u5931\\u8d25\\uff1a"+e.message))}
function __msBatchDel(){let e=Array.from(window.__sel);if(!e.length){u("\\u8bf7\\u5148\\u52fe\\u9009\\u4e66\\u7c4d");return}__confirmBox("\\u6279\\u91cf\\u5220\\u9664","\\u786e\\u5b9a\\u8981<strong>\\u6c38\\u4e45\\u5220\\u9664</strong>\\u9009\\u4e2d\\u7684 "+e.length+" \\u672c\\u4e66\\u5417\\uff1f<br>\\u666e\\u901a\\u4e66\\u5220\\u9664\\u6574\\u4e2a\\u6587\\u4ef6\\u5939\\uff0c\\u5408\\u96c6\\u5220\\u9664\\u5176\\u4e0b\\u5404\\u77ed\\u7bc7\\u6587\\u4ef6\\u5939\\uff0c\\u672a\\u5206\\u7c7b\\u4ec5\\u5220\\u97f3\\u9891\\u3002<br>\\u6b64\\u64cd\\u4f5c\\u4e0d\\u53ef\\u6062\\u590d\\uff01",async()=>{let t=0,o=0;for(let d=0;d<e.length;d++){try{await y("/api/books/"+encodeURIComponent(e[d]),{method:"DELETE"}),window.__sel.delete(e[d]),t++}catch(i){o++}}u("\\u6279\\u91cf\\u5220\\u9664\\u5b8c\\u6210\\uff1a\\u6210\\u529f "+t+" \\u672c"+(o?"\\uff0c\\u5931\\u8d25 "+o+" \\u672c":"")),await w()})}
function __msOpenEdit(){if(!window.__sel.size){u("\\u8bf7\\u5148\\u52fe\\u9009\\u4e66\\u7c4d");return}let e=document.getElementById("beditCount");e&&(e.textContent=window.__sel.size),["beditDescPre","beditDescApp","beditCatVal","beditTagVal","beditAuthVal"].forEach(e=>{let t=document.getElementById(e);t&&(t.value="")}),["beditCatMode","beditTagMode","beditAuthMode"].forEach(e=>{let t=document.getElementById(e);t&&(t.value="")});let t=document.getElementById("beditCoverReset");t&&(t.checked=!1);let o=document.getElementById("batchEditOverlay");o&&(o.hidden=!1)}
function __msCloseEdit(){let e=document.getElementById("batchEditOverlay");e&&(e.hidden=!0)}
async function __msApplyEdit(){let e=Array.from(window.__sel);if(!e.length){__msCloseEdit();return}
let t=i=>{let el=document.getElementById(i);return el?el.value.trim():""},m=i=>{let el=document.getElementById(i);return el?el.value:""};
let o={};let pre=t("beditDescPre"),app=t("beditDescApp");pre&&(o.descriptionPrepend=pre),app&&(o.descriptionAppend=app);
let cm=m("beditCatMode");cm&&(o.category={mode:cm,value:t("beditCatVal")});
let tm=m("beditTagMode");tm&&(o.tags={mode:tm,value:t("beditTagVal")});
let am=m("beditAuthMode");am&&(o.author={mode:am,value:t("beditAuthVal")});
let cr=document.getElementById("beditCoverReset"),cov=!(!cr||!cr.checked);
if(!Object.keys(o).length&&!cov){u("\\u6ca1\\u6709\\u586b\\u5199\\u4efb\\u4f55\\u4fee\\u6539\\u5185\\u5bb9");return}
let btn=document.getElementById("beditApply");btn&&(btn.disabled=!0);
try{if(Object.keys(o).length){let d=await y("/api/batch/edit",{method:"POST",body:JSON.stringify({ids:e,ops:o})});u("\\u6279\\u91cf\\u7f16\\u8f91\\u5b8c\\u6210\\uff1a"+(d.changed||0)+" \\u672c\\u5df2\\u66f4\\u65b0"+((d.skipped||0)?"\\uff0c"+d.skipped+" \\u9879\\u8df3\\u8fc7":""))}
if(cov){let d2=await y("/api/batch/cover-reset",{method:"POST",body:JSON.stringify({ids:e})});u("\\u5c01\\u9762\\u91cd\\u8bbe\\uff1a\\u6062\\u590d "+((d2.restored||[]).length)+" \\u672c\\uff0c\\u65e0\\u5907\\u4efd "+((d2.missing||[]).length)+" \\u672c")}
__msCloseEdit(),await w()}catch(i){u("\\u6279\\u91cf\\u7f16\\u8f91\\u5931\\u8d25\\uff1a"+i.message)}finally{btn&&(btn.disabled=!1)}}
function __msBindBar(){if(window.__msBound)return;window.__msBound=!0;
let tg=document.getElementById("msToggle");tg&&tg.addEventListener("click",()=>__msSetMode(!window.__msMode));
let pa=document.getElementById("msPageAll");pa&&pa.addEventListener("click",()=>{(n.books||[]).forEach(e=>window.__sel.add(e.id)),__msAfter()});
let pi=document.getElementById("msPageInvert");pi&&pi.addEventListener("click",()=>{(n.books||[]).forEach(e=>{window.__sel.has(e.id)?window.__sel.delete(e.id):window.__sel.add(e.id)}),__msAfter()});
let ll=document.getElementById("msAll");ll&&ll.addEventListener("click",__msSelectAll);
let ai=document.getElementById("msAllInvert");ai&&ai.addEventListener("click",async()=>{try{let e=(document.getElementById("favoritesOnly")||{}).checked||!1,t=await y("/api/book-ids?keyword="+encodeURIComponent(n.keyword||"")+(e?"&favoritesOnly=true":""));(t.ids||[]).forEach(function(b){window.__sel.has(b.id)?window.__sel.delete(b.id):window.__sel.add(b.id)}),__msAfter(),u("\\u5df2\\u5bf9\\u5168\\u90e8"+(t.ids||[]).length+"\\u672c\\u6267\\u884c\\u53cd\\u9009")}catch(e){u(e.message)}});
let pc=document.getElementById("msClear");pc&&pc.addEventListener("click",()=>{window.__sel.clear(),__msAfter()});
let fa=document.getElementById("msFavAdd");fa&&fa.addEventListener("click",()=>__msFav(!0));
let fd=document.getElementById("msFavDel");fd&&fd.addEventListener("click",()=>__msFav(!1));
let de=document.getElementById("msDelete");de&&de.addEventListener("click",__msBatchDel);
let ed=document.getElementById("msEdit");ed&&ed.addEventListener("click",__msOpenEdit);
let bc=document.getElementById("beditCancel");bc&&bc.addEventListener("click",__msCloseEdit);
let bx=document.getElementById("beditClose");bx&&bx.addEventListener("click",__msCloseEdit);
let bp=document.getElementById("beditApply");bp&&bp.addEventListener("click",__msApplyEdit);
let ov=document.getElementById("batchEditOverlay");ov&&ov.addEventListener("click",e=>{e.target===e.currentTarget&&(e.currentTarget.hidden=!0)})}
__msBindBar();
async function Y(){try{let t=(await y("/api/recently-played")).items||[]'''
    js = rep(js, jg3e_old, jg3e_new, "JG3e")

    # JG4a: viewMode 下拉 change 绑定 + 初始同步 —— v1.3.11「接线」补丁只加了
    # __getViewMode/__setViewMode/__syncViewModeUI 函数，但漏了给下拉绑 change 事件，
    # 导致「显示方式」切到大图标/小图标/列表都不生效（v1.3.29 修复）。
    # v1.3.35：favoritesOnly 复选框已从 HTML 移除（筛选下拉的「收藏」替代），
    # 原绑定无空值保护会抛错断链，改为安全绑定。
    jg4a_old = 'document.getElementById("favoritesOnly").addEventListener("change",()=>{n.page=1,w()}),'
    # 注意：此处处于逗号表达式链 A(),B(),C() 中间，只能插入「表达式」，不能出现
    # var/let/const 等语句，否则 SyntaxError: Unexpected token 'var'（v1.3.36 修复）。
    jg4a_new = ('(__fo=document.getElementById("favoritesOnly"))'
                '&&__fo.addEventListener("change",()=>{n.page=1,w()}),'
                'document.getElementById("viewMode").addEventListener("change",'
                '()=>{let v=document.getElementById("viewMode").value||"large";__setViewMode(v),fe()}),'
                'function(){try{__syncViewModeUI()}catch(_){}}(),')
    js = rep(js, jg4a_old, jg4a_new, "JG4a")

    # ---- v1.3.32 第四组前端 ----
    # JG4b: w() 查询附带 libFilter（主页筛选下拉）
    jg4b_old = '(__so?"&order="+__so:""));'
    jg4b_new = ('(__so?"&order="+__so:"")'
                '+((__lf=document.getElementById("libFilter"))&&__lf.value?"&libFilter="+encodeURIComponent(__lf.value):""));')
    js = rep(js, jg4b_old, jg4b_new, "JG4b")

    # JG4c: 筛选下拉 change 绑定
    jg4c_old = ('document.getElementById("viewMode").addEventListener("change",'
                '()=>{let v=document.getElementById("viewMode").value||"large";__setViewMode(v),fe()}),')
    jg4c_new = (jg4c_old +
                'document.getElementById("libFilter").addEventListener("change",()=>{n.page=1,w()}),')
    js = rep(js, jg4c_old, jg4c_new, "JG4c")

    # JG4d: __msBindBar 里加「存为合集」弹窗逻辑（v1.3.35：无类型下拉，
    #       两个按钮各自固定类型，弹窗仅展示对应说明）
    jg4d_old = ('let ov=document.getElementById("batchEditOverlay");'
                'ov&&ov.addEventListener("click",e=>{e.target===e.currentTarget&&(e.currentTarget.hidden=!0)})}')
    jg4d_new = ('function __colHint(){var ty=window.__colType||"collection",'
                'oh=document.getElementById("colHint");if(!oh)return;'
                'oh.textContent=ty==="pack"'
                '?'+_qs("连播合集包：选中的书按勾选顺序排成连续章节连播，原书仍正常显示，可随时移除。")+
                ':'+_qs("书籍合集：选中的书组成一个合集整体显示，成员书隐藏，可随时打散恢复。")+'}'
                'let sc=document.getElementById("msSaveCol");'
                'sc&&sc.addEventListener("click",()=>{'
                'if(!window.__sel.size){u("\\u8bf7\\u5148\\u52fe\\u9009\\u4e66\\u7c4d");return}'
                'window.__colType="collection";'
                'let cn=document.getElementById("colName");cn&&(cn.value="");'
                '__colHint();'
                'let tt=document.getElementById("colTitle");tt&&(tt.textContent="' + _u("存为合集") + '");'
                'let cb=document.getElementById("colCreate");cb&&(cb.textContent="' + _u("创建合集") + '");'
                'let ov2=document.getElementById("colOverlay");ov2&&(ov2.hidden=!1)});'
                'let sp=document.getElementById("msSavePack");'
                'sp&&sp.addEventListener("click",()=>{'
                'if(!window.__sel.size){u("\\u8bf7\\u5148\\u52fe\\u9009\\u4e66\\u7c4d");return}'
                'window.__colType="pack";'
                'let cn=document.getElementById("colName");cn&&(cn.value="");'
                '__colHint();'
                'let tt=document.getElementById("colTitle");tt&&(tt.textContent="' + _u("创建合集包") + '");'
                'let cb=document.getElementById("colCreate");cb&&(cb.textContent="' + _u("创建合集包") + '");'
                'let ov2=document.getElementById("colOverlay");ov2&&(ov2.hidden=!1)});'
                'let cc=document.getElementById("colCancel");'
                'cc&&cc.addEventListener("click",()=>{let o3=document.getElementById("colOverlay");o3&&(o3.hidden=!0)});'
                'let cx=document.getElementById("colOverlay");'
                'cx&&cx.addEventListener("click",e=>{e.target===e.currentTarget&&(e.currentTarget.hidden=!0)});'
                'let cr=document.getElementById("colCreate");'
                'cr&&cr.addEventListener("click",async()=>{'
                'let cn=document.getElementById("colName"),o4=document.getElementById("colOverlay");'
                'let name=cn?cn.value.trim():"";'
                'if(!name){u("\\u8bf7\\u586b\\u5199\\u540d\\u79f0");return}'
                'let type=window.__colType||"collection",ids=Array.from(window.__sel);'
                'try{await y("/api/collections",{method:"POST",body:JSON.stringify({name:name,type:type,ids:ids})});'
                'u(type==="pack"?' + _qs("连播清单「") + '+name+' + _qs("」已创建，可在主页筛选「合集包」中找到") + ':' + _qs("合集「") + '+name+' + _qs("」已创建") + ');'
                'o4&&(o4.hidden=!0);__msSetMode(!1),await w()}catch(e){u("\\u521b\\u5efa\\u5931\\u8d25\\uff1a"+e.message)}});'
                + jg4d_old)
    js = rep(js, jg4d_old, jg4d_new, "JG4d")

    # JG4e: 卡片删除按钮 title 加 custom/pack 分支（打散，不删文件）
    jg4e_old = 'title="${(t.virt==="shorts")?'
    jg4e_new = ('title="${t.virt==="pack"?"\\u79FB\\u9664\\u8FDE\\u64AD\\u6E05\\u5355\\uFF08\\u4E0D\\u5220\\u9664\\u6587\\u4EF6\\uFF09"'
                ':t.virt==="custom"?"\\u6253\\u6563\\u5408\\u96C6\\uFF08\\u4E0D\\u5220\\u9664\\u6587\\u4EF6\\uFF09"'
                ':(t.virt==="shorts")?')
    js = rep(js, jg4e_old, jg4e_new, "JG4e")

    # JG4f: 删除确认弹窗警告语加 pack/custom/shorts 分支
    jg4f_old = ('if(b.virt==="shorts"){let n2=(b.memberIds||[]).length;wn.textContent=')
    jg4f_new = ('if(b.virt==="pack"){wn.textContent=' + _qs("⚠️ 将移除该连播清单：原书不受影响，不会删除任何文件。") + ';}'
                'else if(b.virt==="custom"){wn.textContent=' + _qs("⚠️ 将打散该合集：成员书籍恢复显示，不会删除任何文件。") + ';}'
                'else if(b.virt==="shorts"){let n2=(b.memberIds||[]).length;wn.textContent=')
    js = rep(js, jg4f_old, jg4f_new, "JG4f")

    # JG4g: 删除结果提示加 scatter 分支
    jg4g_old = 'u(r.collection?('
    jg4g_new = ("u(r.scatter?(" + _qs("已移除「") + "+(t?t.title:\"\")+" + _qs("」，相关书籍保持不变") + "):r.collection?(")
    js = rep(js, jg4g_old, jg4g_new, "JG4g")

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

    # J45: 详情页操作栏 —— 「刷新」前插入「重新扫描」（强制重扫该书所在目录，
    #      可解决音频增删 / 音频内容替换（时长变化）/ 音频重命名等）
    j45_old = '<button class="btn btn-ghost" id="btnRefreshBook">\\u{1F504} \\u5237\\u65B0</button>'
    j45_new = ('<button class="btn btn-ghost" id="btnRescanBook" title="\\u91CD\\u65B0\\u626B\\u63CF\\u8BE5\\u4E66\\u76EE\\u5F55">'
               '\\u{1F504} \\u91CD\\u65B0\\u626B\\u63CF</button>\n'
               '          ' + j45_old)
    js = rep(js, j45_old, j45_new, "J45")

    # J46: 绑定详情页「重新扫描」按钮；「打散合集」改为弹出成员选择弹窗（v1.3.35 部分打散）
    j46_old = 'document.getElementById("btnRefreshBook").addEventListener("click",()=>{R(e.id)}),'
    j46_new = ('document.getElementById("btnRescanBook").addEventListener("click",()=>{__rescanBook(e)}),'
               'document.getElementById("btnScatterCol").onclick=async()=>{'
               'window.__scBook=e;'
               'var lb=document.getElementById("scList"),oh=document.getElementById("scHint"),'
               'ol=document.getElementById("scOverlay");if(!lb||!ol)return;lb.innerHTML="";'
               'oh.textContent=e.virt==="shorts"?'
               + _qs("勾选要移出的短篇；移出后这些书不再被自动合并，全部移出则合集消失。")
               + ':e.virt==="pack"?'
               + _qs("勾选要从连播清单中移出的书；全部移出则清单自动删除。不勾选直接确认 = 移除整个清单。")
               + ':'
               + _qs("勾选要从合集中移出的书；全部移出则合集自动删除。不勾选直接确认 = 打散整个合集。") + ';'
               'try{var r=await y("/api/books/"+encodeURIComponent(e.id)+"/members");'
               '(r.members||[]).forEach(function(m){'
               'var it=document.createElement("label");it.className="sc-item";'
               'var cb=document.createElement("input");cb.type="checkbox";cb.value=m.id;cb.checked=!0;'
               'var sp=document.createElement("span");sp.textContent=m.title;'
               'it.appendChild(cb);it.appendChild(sp);lb.appendChild(it)});'
               'var ta=document.getElementById("scToggleAll");ta&&(ta.textContent="\\u5168\\u4E0D\\u9009");'
               'ol.hidden=!1}catch(err){u("' + _u("加载成员失败：") + '"+err.message)}};'
               'var scC=document.getElementById("scConfirm");'
               'scC&&(scC.onclick=async()=>{'
               'var b=window.__scBook;if(!b)return;'
               'var ids=[];document.querySelectorAll("#scList input[type=checkbox]")'
               '.forEach(function(x){x.checked&&ids.push(x.value)});'
               'try{var r=await y("/api/books/"+encodeURIComponent(b.id)+"/scatter",'
               '{method:"POST",body:JSON.stringify({ids:ids}),headers:{"Content-Type":"application/json"}});'
               'var o5=document.getElementById("scOverlay");o5&&(o5.hidden=!0);'
               'u(r.gone?'
               + _qs("已打散「") + '+String(b.title||"")+' + _qs("」")
               + ':'
               + _qs("已移出 ") + '+r.removed+' + _qs(" 个成员，「") + '+String(b.title||"")+' + _qs("」还剩 ") + '+r.remaining+' + _qs(" 个成员") + ');'
               'k("homeView");try{await w()}catch(_){}}catch(err){u("' + _u("打散失败：") + '"+err.message)}});'
               'var scX=document.getElementById("scCancel");'
               'scX&&(scX.onclick=()=>{var o6=document.getElementById("scOverlay");o6&&(o6.hidden=!0)});'
               'var scT=document.getElementById("scToggleAll");'
               'scT&&(scT.onclick=()=>{var bs=document.querySelectorAll("#scList input[type=checkbox]");'
               'var all=bs.length&&Array.prototype.every.call(bs,function(x){return x.checked});'
               'Array.prototype.forEach.call(bs,function(x){x.checked=!all});'
               'scT.textContent=all?"\\u5168\\u9009":"\\u5168\\u4E0D\\u9009"});'
               'var scO=document.getElementById("scOverlay");'
               'scO&&(scO.onclick=ev=>{ev.target===ev.currentTarget&&(ev.currentTarget.hidden=!0)});'
               + j46_old)
    js = rep(js, j46_old, j46_new, "J46")

    # JG4h: 详情页按钮 —— 「重新扫描」对纯虚拟合集隐藏（无对应目录，misc 未分类目录书保留）；
    #       新增「打散合集」按钮（仅 shorts/custom/pack 显示，misc 不显示）。
    #       注意必须替换「完整按钮元素」——只换开始标签会把打散按钮嵌进重扫按钮内部，
    #       HTML 不允许嵌套 button，浏览器解析器会把外层截成空壳、内层错位（v1.3.33 空白按钮 bug）。
    jg4h_old = ('<button class="btn btn-ghost" id="btnRescanBook" title="\\u91CD\\u65B0\\u626B\\u63CF\\u8BE5\\u4E66\\u76EE\\u5F55">'
                '\\u{1F504} \\u91CD\\u65B0\\u626B\\u63CF</button>')
    jg4h_new = ('<button class="btn btn-ghost" id="btnRescanBook" title="\\u91CD\\u65B0\\u626B\\u63CF\\u8BE5\\u4E66\\u76EE\\u5F55"'
                '${e.virt&&e.virt!=="misc"?" hidden":""}>\\u{1F504} \\u91CD\\u65B0\\u626B\\u63CF</button>\n'
                '          <button class="btn btn-ghost" id="btnScatterCol" '
                'title="${e.virt==="shorts"?' + _qs("打散自动合集：这些书不再被自动合并（不删除文件）")
                + ':e.virt==="pack"?' + _qs("移除连播清单：原书不受影响（不删除文件）")
                + ':' + _qs("打散合集：成员书恢复显示（不删除文件）") + '}"'
                '${(e.virt==="shorts"||e.virt==="custom"||e.virt==="pack")?"":" hidden"}>\\u6253\\u6563\\u5408\\u96C6</button>')
    js = rep(js, jg4h_old, jg4h_new, "JG4h")

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
               'if(wasHidden){kw.value=document.getElementById("editTitle").value.trim()||window.__editOrig||"";'
               'kw.focus()}}))})();'
               # J43: 重扫弹窗 —— 可折叠目录树（/api/list-dir 逐级懒加载），复选框多选、依次扫描
               'function __rtreeRow(d,depth){'
               'let row=document.createElement("div");row.className="rtree-row";'
               'let cb=document.createElement("input");cb.type="checkbox";cb.className="rtree-cb";cb.value=d.rel;'
               'let ic=document.createElement("span");ic.className="rtree-folder";ic.textContent="\\ud83d\\udcc1";'
               'let lb=document.createElement("span");lb.className="rtree-label";lb.textContent=d.name;lb.title=d.rel;'
               'lb.addEventListener("click",()=>{cb.checked=!cb.checked});'
               'row.appendChild(cb);row.appendChild(ic);row.appendChild(lb);'
               'let kids=null,caret=null;'
               'if(d.subdirs>0){caret=document.createElement("span");caret.className="rtree-caret";caret.textContent="\\u25b8";'
               'caret.addEventListener("click",async ev=>{ev.stopPropagation();'
               'if(!kids){kids=document.createElement("div");kids.className="rtree-kids";'
               'kids.innerHTML=\'<div class="rtree-msg" style="padding-left:\'+(8+(depth+1)*18)+\'px">\\u52a0\\u8f7d\\u4e2d...</div>\';'
               'row.parentNode.insertBefore(kids,row.nextSibling);'
               'try{let dd=await y("/api/list-dir?dir="+encodeURIComponent(d.rel));let arr=(dd&&dd.dirs)||[];'
               'kids.innerHTML="";'
               'if(!arr.length)kids.innerHTML=\'<div class="rtree-msg" style="padding-left:\'+(8+(depth+1)*18)+\'px">\\uff08\\u65e0\\u5b50\\u76ee\\u5f55\\uff09</div>\';'
               'else arr.forEach(x=>kids.appendChild(__rtreeRow(x,depth+1)))}'
               'catch(e){kids.innerHTML=\'<div class="rtree-msg">\\u52a0\\u8f7d\\u5931\\u8d25\\uff1a\'+(e&&e.message||e)+"</div>"}'
               'kids.hidden=!1;caret.textContent="\\u25be";caret.classList.add("open")}'
               'else{kids.hidden=!kids.hidden;caret.textContent=kids.hidden?"\\u25b8":"\\u25be";caret.classList.toggle("open",!kids.hidden)}});'
               'row.appendChild(caret)}'
               'row.style.paddingLeft=(8+depth*18)+"px";'
               'return row}'
               'function __fmtAgo(ts){let d=Date.now()-ts;if(d<0)d=0;let m=Math.floor(d/60000);'
               'if(m<1)return"\\u521a\\u521a";if(m<60)return m+"\\u5206\\u949f\\u524d";'
               'let h=Math.floor(m/60);if(h<24)return h+"\\u5c0f\\u65f6\\u524d";'
               'return Math.floor(h/24)+"\\u5929\\u524d"}'
               'async function __openRescanModal(){'
               'let o=document.getElementById("rescanOverlay");if(!o)return;'
               'let tr=document.getElementById("rescanTree");'
               'tr.innerHTML=\'<div class="rtree-msg">\\u52a0\\u8f7d\\u4e2d...</div>\';'
               'o.hidden=!1;'
               '(async()=>{try{let pg=await y("/api/scan-progress");let li=document.getElementById("rescanLastInfo");'
               'if(li){li.textContent=pg.lastScanAt?("\\u4e0a\\u6b21\\u5168\\u5e93\\u626b\\u63cf\\uff1a"+__fmtAgo(pg.lastScanAt)):"\\u5c1a\\u672a\\u8fdb\\u884c\\u8fc7\\u5168\\u5e93\\u626b\\u63cf"}}catch(_){}})();'
               'try{let d=await y("/api/list-dir");let arr=(d&&d.dirs)||[];'
               'tr.innerHTML="";'
               'if(!arr.length){tr.innerHTML=\'<div class="rtree-msg">\\u4e66\\u5e93\\u6839\\u76ee\\u5f55\\u4e0b\\u6ca1\\u6709\\u5b50\\u76ee\\u5f55</div>\';return}'
               'arr.forEach(x=>tr.appendChild(__rtreeRow(x,0)))}'
               'catch(e){tr.innerHTML=\'<div class="rtree-msg">\\u76ee\\u5f55\\u52a0\\u8f7d\\u5931\\u8d25\\uff1a\'+(e&&e.message||e)+"</div>"}}'
               'function __doRescan(dir,mode){'
               'let body=dir?{dir:dir}:{};'
               'if(mode==="force")body.force=1;'
               'if(mode==="remerge"){body={remerge:1};dir=""}'
               'return y("/api/rescan",{method:"POST",body:JSON.stringify(body)}).then(()=>{'
               'u(mode==="remerge"?"\\u6b63\\u5728\\u91cd\\u5efa\\u5408\\u96c6..."'
               ':"\\u5df2\\u5f00\\u59cb\\u91cd\\u65b0\\u626b\\u63cf"+(dir?"\\uff1a"+dir:""));'
               'if(mode==="remerge"){return new Promise(res=>{setTimeout(async()=>{'
               'try{await w()}catch(_){}'
               'try{u("\\u5408\\u96c6\\u5df2\\u6309\\u5f53\\u524d\\u9608\\u503c\\u91cd\\u5efa")}catch(_){}'
               'res()},1500)})}'
               'return new Promise(res=>{let n=0,iv=setInterval(async()=>{n++;'
               'try{let s=await y("/api/scan-progress");'
               'if(!s.scanning||n>200){clearInterval(iv);w();'
               'if(!s.scanning){let sn=null;try{sn=await y("/api/snapshot")}catch(_){}'
               'u("\\u626b\\u63cf\\u5b8c\\u6210\\uff0c\\u5171 "+((sn&&sn.totalBooks)||"?")+" \\u672c")}'
               'res()}}catch(e){}},2500)})})'
               '.catch(e=>{u("\\u542f\\u52a8\\u626b\\u63cf\\u5931\\u8d25\\uff1a"+e.message)});}'
               '(function(){let ov=document.getElementById("rescanOverlay");if(!ov||ov.dataset.b)return;ov.dataset.b="1";'
               'document.getElementById("rescanCancelBtn").addEventListener("click",()=>{ov.hidden=!0});'
               'ov.addEventListener("click",e=>{e.target===ov.currentTarget&&(ov.hidden=!0)});'
               'document.getElementById("rescanTreeHead").addEventListener("click",()=>{'
               'let tr=document.getElementById("rescanTree");tr.hidden=!tr.hidden;'
               'document.getElementById("rescanTreeHead").classList.toggle("collapsed",tr.hidden)});'
               'document.getElementById("rescanOkBtn").addEventListener("click",async()=>{'
               'let cbs=Array.prototype.slice.call(document.querySelectorAll("#rescanTree .rtree-cb:checked"));'
               'let dirs=cbs.map(c=>c.value);ov.hidden=!0;'
               'let mr=document.querySelector("input[name=rescanMode]:checked"),'
               'mode=mr?mr.value:"auto";'
               'if(mode==="remerge"){await __doRescan("",mode);return}'
               'if(!dirs.length){await __doRescan("",mode);return}'
               'for(let i=0;i<dirs.length;i++)await __doRescan(dirs[i],mode)});})();'
               # J44: 全局扫描进度轮询 —— 顶栏徽标 + 扫描结束自动刷新列表
               '(function(){if(window.__scanPoll)return;window.__scanPoll=1;let was=!1;'
               'async function tick(){let st=document.getElementById("scanStatus");if(!st)return;'
               'let p=null;try{p=await y("/api/scan-progress")}catch(e){return}'
               'if(p&&p.scanning){was=!0;st.hidden=!1;'
               'let prog=(p.rootTotal>0)?((p.rootDone||0)+"/"+p.rootTotal+" \\u7ec4"):((p.dirs||0)+" \\u4e2a\\u76ee\\u5f55");'
               'let sec=Math.floor((p.elapsed||0)/1000),el=sec>=60?(Math.floor(sec/60)+"\\u5206"+(sec%60)+"\\u79d2"):(sec+"\\u79d2");'
               'st.textContent="\\u626b\\u63cf\\u4e2d "+prog+" \\u00b7 \\u5df2\\u53d1\\u73b0 "+(p.books||0)+" \\u672c"+(p.reused?"\\uff08\\u590d\\u7528 "+p.reused+"\\uff09":"")+" \\u00b7 "+el;'
               'st.title="\\u5f53\\u524d\\u76ee\\u5f55\\uff1a"+(p.currentDir||"\\u6839\\u76ee\\u5f55")}'
               'else{if(was){was=!1;w();u("\\u626b\\u63cf\\u5b8c\\u6210")}st.hidden=!0}}'
               'setInterval(tick,3000);tick()})();')
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
    # 前端语法自检：minified 单行 bundle 里的语法错误（如逗号表达式中插入 var 语句）
    # 必须在构建阶段拦下，否则只在浏览器运行时静默抛 SyntaxError、整个前端不工作。
    import subprocess as _sp
    import shutil as _shutil
    _node = _shutil.which("node") or r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-2\node.exe"
    _chk = _sp.run([_node, "--check", js_path], capture_output=True)
    if _chk.returncode != 0:
        raise AssertionError("app.bundle 前端语法检查失败:\n" + _chk.stderr.decode("utf-8", "replace")[:2000])
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
        ".book-grid.mode-list .book-card-fav:hover { color: #ffd000; background: var(--surface-2); }\n"
        "/* ===== v1.3.20 重扫弹窗：可折叠目录树 ===== */\n"
        ".rtree-wrap { border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); overflow: hidden; margin-bottom: 14px; }\n"
        ".rtree-head { display: flex; align-items: center; gap: 10px; padding: 10px 12px; cursor: pointer; user-select: none; }\n"
        ".rtree-head-text { flex: 1; min-width: 0; }\n"
        ".rtree-head-title { font-size: 14px; font-weight: 600; color: var(--text); }\n"
        ".rtree-head-sub { font-size: 12px; color: var(--text-3); margin-top: 2px; }\n"
        ".rtree-head .rtree-caret { font-size: 12px; transition: transform .15s; flex-shrink: 0; }\n"
        ".rtree-head.collapsed .rtree-caret { transform: rotate(-90deg); }\n"
        ".rtree { max-height: 320px; overflow-y: auto; border-top: 1px solid var(--border); padding: 6px 4px; background: var(--surface); }\n"
        ".rtree-head.collapsed + .rtree { display: none; }\n"
        ".rtree-row { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; font-size: 13px; color: var(--text); }\n"
        ".rtree-row:hover { background: var(--surface-2); }\n"
        ".rtree-cb { flex-shrink: 0; width: 15px; height: 15px; accent-color: var(--primary); cursor: pointer; }\n"
        ".rtree-folder { flex-shrink: 0; font-size: 14px; line-height: 1; }\n"
        ".rtree-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }\n"
        ".rtree-row > .rtree-caret { cursor: pointer; padding: 4px 6px; color: var(--text-3); font-size: 12px; }\n"
        ".rtree-row > .rtree-caret:hover { color: var(--text); }\n"
        ".rtree-msg { font-size: 12px; color: var(--text-3); padding: 6px 10px; }\n"
        ".scan-pill { display: inline-flex; align-items: center; margin-left: 6px; padding: 3px 12px;\n"
        "  border-radius: 999px; font-size: 12px; color: var(--text-2); background: var(--surface-2);\n"
        "  border: 1px solid var(--border); max-width: 420px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }\n"
        ".scan-pill::before { content: \"\\27f3\"; display: inline-block; margin-right: 6px; color: var(--primary);\n"
        "  animation: scan-spin 1.2s linear infinite; }\n"
        "@keyframes scan-spin { to { transform: rotate(360deg); } }\n"
        ".scan-pill[hidden] { display: none !important; }\n"
        # v1.3.36：按钮 hidden 硬化——详情页操作栏按钮（重新扫描/打散合集）依赖 hidden 显隐，
        # 若被组件库 display 规则或浏览器缓存影响会误显示，这里强制兜底。
        ".book-hero .btn[hidden], .book-detail .btn[hidden], #bookDetail .btn[hidden] { display: none !important; }\n"
        ".rtree-force { display: block; font-size: 12px; color: var(--text-3); padding: 6px 10px; cursor: pointer; }\n"
        "/* ===== v1.3.27 重新扫描弹窗目录结构说明 + 设置弹窗居中加宽 ===== */\n"
        ".rescan-dirinfo { margin: 0 0 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); overflow: hidden; }\n"
        ".rescan-dirinfo summary { cursor: pointer; padding: 10px 12px; font-size: 13px; font-weight: 600; color: var(--text); user-select: none; }\n"
        ".rescan-dirinfo summary:hover { color: var(--primary); }\n"
        ".rescan-dirinfo ul { margin: 0; padding: 4px 12px 12px 30px; }\n"
        ".rescan-dirinfo li { font-size: 12px; line-height: 1.7; color: var(--text-2); margin: 2px 0; }\n"
        ".rescan-dirinfo code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; background: var(--surface); padding: 1px 5px; border-radius: 4px; color: var(--text); }\n"
        "#settingsOverlay { align-items: center; }\n"
        ".settings-sheet { max-width: 600px; width: 92%; max-height: 86vh; border-radius: var(--radius-lg); animation: ab-fade-in .2s ease; }\n"
        "@keyframes ab-fade-in { from { opacity: 0; } to { opacity: 1; } }\n"
        # ---- v1.3.28 第三组：多选 + 批量操作 ----
        ".ms-toggle { padding: 2px 12px; font-size: 12px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); color: var(--text); cursor: pointer; margin-left: 10px; }\n"
        ".ms-toggle:hover { border-color: var(--primary); color: var(--primary); }\n"
        ".ms-toggle.on { background: var(--primary); color: var(--on-primary); border-color: var(--primary); }\n"
        ".batch-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 12px; padding: 8px 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-2); }\n"
        ".batch-bar[hidden] { display: none; }\n"
        ".batch-bar button { padding: 4px 10px; font-size: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); cursor: pointer; }\n"
        ".batch-bar button:hover { border-color: var(--primary); color: var(--primary); }\n"
        ".batch-bar .batch-danger { color: var(--danger); border-color: var(--danger); }\n"
        ".batch-bar .batch-danger:hover { color: #fff; background: var(--danger); border-color: var(--danger); }\n"
        ".batch-count { font-size: 12px; font-weight: 600; color: var(--text); }\n"
        ".batch-sep { width: 1px; height: 16px; background: var(--border); }\n"
        ".book-card { position: relative; }\n"
        ".book-card-sel { display: none; position: absolute; top: 6px; left: 6px; z-index: 6; width: 24px; height: 24px; border-radius: 6px; background: rgba(0,0,0,.55); align-items: center; justify-content: center; }\n"
        ".book-card-sel input { width: 15px; height: 15px; accent-color: var(--primary); cursor: pointer; margin: 0; }\n"
        ".ms-mode .book-card-sel { display: flex; }\n"
        ".book-card.ms-on { outline: 2px solid var(--primary); outline-offset: -2px; }\n"
        ".edit-modal.batch-edit-modal { width: min(560px, 92vw); max-height: 86vh; overflow: auto; }\n"
        ".bedit-row { display: flex; gap: 10px; align-items: flex-start; margin: 10px 0; }\n"
        ".bedit-row > label:first-child { flex: none; width: 60px; font-size: 13px; color: var(--text); padding-top: 7px; }\n"
        ".bedit-fields { flex: 1; display: flex; gap: 8px; min-width: 0; }\n"
        ".bedit-fields select { flex: none; }\n"
        ".bedit-fields input { flex: 1; min-width: 0; }\n"
        ".bedit-row textarea, .bedit-fields input, .bedit-fields select { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); color: var(--text); padding: 6px 8px; font-size: 13px; }\n"
        ".bedit-row textarea { flex: 1; resize: vertical; }\n"
        ".bedit-cover { flex: 1; font-size: 13px; color: var(--text); display: flex; align-items: center; gap: 6px; padding-top: 7px; }\n"
        ".bedit-note { font-size: 12px; line-height: 1.6; color: var(--text-2); margin: 10px 0 0; }\n"
        # ---- v1.3.29 修复：重新扫描弹窗加宽（默认 .edit-modal 480px / .delete-modal 420px 放不下目录树）----
        "#rescanOverlay .edit-modal { max-width: 880px; width: 94%; }\n"
        # ---- v1.3.32 第四组：合集弹窗提示 ----
        ".col-hint { font-size: 12px; color: var(--text-3); margin-top: 6px; }\n"
        # ---- v1.3.35 打散合集成员选择弹窗 ----
        # v1.3.36 修复：原版 `.edit-field label { display:block; margin-bottom:4px }` 优先级(0,1,1)
        #   高于 `.sc-head-title`(0,1,0)，会把标题压成块级并加下外边距，在 flex 居中容器里
        #   垂直错位（与右侧按钮不在一条线）。这里用 `.edit-field .sc-head .sc-head-title`(0,3,0)
        #   明确覆盖 display/margin/line-height，确保与按钮垂直居中对齐。
        ".sc-head { display: flex !important; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 2px; }\n"
        # v1.3.36 修复(三)追加：标题的 color 同样去掉 `!important`（WebF 对
        #   `!important`+嵌套 var() 解析失败会导致文字不可见）。选择器已是 (0,3,0)，
        #   本身足以压过 `.edit-field label`(0,1,1)，无需 !important。
        ".edit-field .sc-head .sc-head-title, .sc-head .sc-head-title { display: block !important; flex: 1 1 auto; min-width: 0; margin: 0 !important; padding: 0 !important; font-size: 13px; font-weight: 400; line-height: 1.4; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }\n"
        ".edit-field .sc-head .btn, .sc-head .btn { flex: none !important; margin: 0 !important; padding: 2px 10px !important; font-size: 12px !important; line-height: 1.4 !important; }\n"
        ".edit-field .sc-tip, .sc-tip { display: block; font-size: 12px; font-weight: 400; color: var(--text-3); margin: 0 0 6px; line-height: 1.4; }\n"
        ".sc-list { max-height: 320px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px 10px; }\n"
        # v1.3.36 修复(三/四)：
        #   上一轮去掉 `!important` 后用户反馈还是没书名，只剩勾选框。
        #   后端接口返回数据正常（{success,data:{members:[{id,title}]}}，y() 已拆壳），
        #   标题 "要移出的成员" 颜色正常 → 颜色/变量解析没问题。
        #   截图中勾选框孤零零在 sc-list 中间，判断是 WebF 对 flex item 子元素
        #   的宽度/overflow 处理异常：span { flex:1; min-width:0; overflow:hidden }
        #   可能被压成 0 宽，文字被完全裁剪；flex: none 在 checkbox 上也可能失效。
        #   改法：成员行放弃 flex 布局，改用 block + inline-block——这是 WebF 里
        #   最稳的基线布局；input/span 垂直居中对齐，span 用 calc 占剩余宽度。
        ".sc-list .sc-item { display: block; margin: 0; padding: 5px 0; cursor: pointer; font-size: 13px; color: var(--text); line-height: 1.4; }\n"
        ".sc-item input { display: inline-block; vertical-align: middle; width: 16px; height: 16px; margin: 0 8px 0 0; flex: none; }\n"
        ".sc-item span { display: inline-block; vertical-align: middle; max-width: calc(100% - 28px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n")
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
