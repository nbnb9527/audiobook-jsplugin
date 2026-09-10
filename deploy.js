// 部署 audiobook 插件到 Songloft 宿主：登录 -> 上传 zip（覆盖更新自动热重载）-> 校验版本
// 支持多主机：默认部署到 .88 与 .89 两台；也可用参数指定
//   node deploy.js                       # 部署到全部默认主机
//   node deploy.js 192.168.1.88:8090    # 只部署到指定主机（逗号分隔可多台）,8090为Songloft端口
const fs = require('fs');
const path = require('path');

const DEFAULT_HOSTS = 'http://192.168.1.69:58090,http://192.168.1.10:58090';
const HOSTS = (process.argv[2] || DEFAULT_HOSTS)
  .split(',').map(s => s.trim()).filter(Boolean)
  .map(h => (h.startsWith('http') ? h : 'http://' + h));
const USER = 'admin';
const PASS = 'admin';
const ZIP = path.join(__dirname, 'dist', 'audiobook.jsplugin.zip');

function findPlugin(list, id) {
  const root = (list && (list.data || list)) || list;
  const arr = Array.isArray(root) ? root
    : (Array.isArray(root.items) ? root.items
      : (Array.isArray(root.plugins) ? root.plugins : []));
  if (id != null) {
    const byId = arr.find(p => String(p.id) === String(id));
    if (byId) return byId;
    const byEntry = arr.find(p => (p.id || p.entry) === id || p.entry === id);
    if (byEntry) return byEntry;
  }
  return arr.find(p => p.entryPath === 'audiobook' || p.entry === 'audiobook'
    || (p.name || '').includes('有声书')) || null;
}

async function deployTo(HOST) {
  console.log(`\n===== ${HOST} =====`);
  // 1. 登录
  let r = await fetch(`${HOST}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USER, password: PASS }),
  });
  if (!r.ok) throw new Error(`登录失败: HTTP ${r.status}`);
  const login = await r.json();
  const token = (login.data && login.data.access_token) || login.access_token;
  if (!token) throw new Error('未获取到 access_token: ' + JSON.stringify(login).slice(0, 200));
  console.log('[1] 登录成功');

  // 2. 当前插件状态
  r = await fetch(`${HOST}/api/v1/jsplugins`, { headers: { Authorization: `Bearer ${token}` } });
  const list = await r.json();
  const cur = findPlugin(list, null);
  if (cur) {
    console.log(`[2] 当前已安装: v${cur.version} enabled=${cur.enabled} zipHash=${String(cur.zipHash || '').slice(0, 8)}...`);
  } else {
    console.log('[2] 未找到已安装的 audiobook 插件（将新装）');
  }

  // 3. 上传（覆盖更新 HTTP 200 自动热重载；新装 HTTP 201 需 enable）
  const bytes = fs.readFileSync(ZIP);
  const form = new FormData();
  form.append('file', new Blob([bytes], { type: 'application/zip' }), 'audiobook.jsplugin.zip');
  r = await fetch(`${HOST}/api/v1/jsplugins/upload`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  const upText = await r.text();
  let up;
  try { up = JSON.parse(upText); } catch { throw new Error(`上传响应异常: HTTP ${r.status} ${upText.slice(0, 300)}`); }
  const first = up.results && up.results[0];
  if (!r.ok || (first && first.success === false) || (up.success === false)) {
    throw new Error(`上传失败: HTTP ${r.status} ${upText.slice(0, 300)}`);
  }
  const plugin = (first && first.plugin) || up.plugin || cur || {};
  const isNew = r.status === 201;
  console.log(`[3] 上传成功 (HTTP ${r.status}, ${isNew ? '新装' : '覆盖更新-自动热重载'}) id=${plugin.id} v${plugin.version}`);

  // 4. 新装需要 enable
  if (isNew && plugin.id != null) {
    r = await fetch(`${HOST}/api/v1/jsplugins/${plugin.id}/enable`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!r.ok) throw new Error(`enable 失败: HTTP ${r.status}`);
    console.log('[4] 已启用插件');
  }

  // 5. 复核（用上传返回的 id 精确查找，避免 vundefined）
  r = await fetch(`${HOST}/api/v1/jsplugins`, { headers: { Authorization: `Bearer ${token}` } });
  const list2 = await r.json();
  const now = findPlugin(list2, plugin.id != null ? plugin.id : (plugin.entry || plugin.entryPath));
  console.log(`[5] 复核: v${now && now.version} enabled=${now && now.enabled} zipHash=${String((now && now.zipHash) || '').slice(0, 8)}...`);
  return (now && now.version) || (plugin.version);
}

async function main() {
  const results = [];
  for (const h of HOSTS) {
    try {
      const v = await deployTo(h);
      results.push(`${h} -> v${v}`);
    } catch (e) {
      results.push(`${h} -> 失败: ${e.message}`);
    }
  }
  console.log('\n===== 汇总 =====');
  results.forEach(x => console.log('  ' + x));
  console.log('插件热重载后将自动开始重新扫描。');
}

main().catch(e => { console.error('部署失败:', e.message); process.exit(1); });
