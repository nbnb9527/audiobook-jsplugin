# audiobook-jsplugin（修改版）

Songloft / MiMusic 有声书插件的修改版。基于官方 `audiobook.jsplugin.zip` 原包，通过 `build.py` 自动提取源码、应用补丁、重新打包，**不改动官方包以外的任何二进制**，所有修改均可追溯（`build.py` 中每个补丁带编号与锚点断言）。

> 作者署名：**MiMusic Team (修改：nb9527)**

<img width="2297" height="1502" alt="RO5QOKGHHCP0WS_PF1FY909" src="https://github.com/user-attachments/assets/f42df531-0572-4695-8d4f-0ba28fa7f56d" />
<img width="924" height="981" alt="8_2WLLSJF6A)NJJ{466MDVO" src="https://github.com/user-attachments/assets/86f49b18-2747-4214-b86d-a7a0f038810e" />
<img width="2319" height="1428" alt="BY $CVSV8CHP(46CXN`%UZO" src="https://github.com/user-attachments/assets/9d842191-f3c2-459a-bcc9-8c69a8dc75f5" />

## 下载安装

从 [Releases](https://github.com/nbnb9527/audiobook-jsplugin/releases) 下载最新 `audiobook.jsplugin.zip`，在 Songloft 宿主插件页上传安装（或覆盖原插件目录后热重载）。

## 相对原版新增 / 修复的主要功能

### 一、大书库扫描修复（核心）

原版在书库规模上去后基本不可用，本版重点修复：

| # | 问题 | 修复 |
|---|---|---|
| P1 | 单本书音频数超过 65534 时 `push(...arr)` 参数超限，QuickJS 抛 RangeError 整本书丢失 | 改为分块 apply |
| P2 | 递归扫描深度硬编码 6 层，深层目录音频扫不到 | 提升到 20 层 |
| P3 | 逐文件串行 `await fs.stat`，SMB/NAS 上 19 万文件极慢且易触发宿主中断 | 16 并发批量 stat |
| P4 | 无法跳过超大素材目录 | 支持 `/app/audiobook/.scanignore` 配置忽略目录 |
| P5 | 嵌套书库（多级分类目录）扫描混乱 | 嵌套书库模式 v2，按子文件夹成书 |
| P6/P7 | 群晖 `@eaDir` 等缩略图目录被当成"有子目录"，每本书都生成"未分类合集"且丢封面/简介；含 `..` 的条目导致宿主 fs 整条拒绝 | 内置忽略名单（`@eaDir`、`@SynologyResource`、`#recycle`、`_ARCHIVE_TRASH`、`_DEDUPE_TRASH`、`@sharebin`），单本书内部同样跳过；`..` 条目剪枝 |

### 二、书名别名系统（解决有声书标题不能修改的问题）

- 编辑弹窗新增**别名字段**（保留原名提示），显示什么就能搜什么：关键词搜索、按书名排序均匹配别名。
- 排序支持显式 `asc/desc` 方向。
- 详情页/接口返回 `originalTitle` + `folderRelPath`，路径**只读展示 + 一键复制**。
- 旧版本存量 settings 自动深合并升级，不影响已有扫描记录与收藏/进度数据。

### 三、显示与浏览

- 主页工具栏新增**显示方式**（大图标/小图标/列表）与**顺序**下拉（排序 → 顺序 → 显示方式 → 只看收藏）。
- 主页有声书图标和列表增加收藏、编辑和删除功能
- 设置弹窗可配置**默认显示方式**（桌面端/手机端独立记忆）。
- 分页区新增**页码输入框 + 跳转按钮**。
- 主页「加载」与「设置」之间新增**刷新**按钮（只刷新列表，不重新扫描磁盘）。
- **重新扫描弹窗（可折叠目录树）**：主页「重新扫描」弹出目录树，点箭头逐级展开（懒加载，大书库秒开）；复选框勾选一个或多个目录后只重扫选中子树，留空则全库重扫。新增轻量接口 `GET /api/list-dir?dir=`（每次只列一层子目录），替代原先一次性遍历全树的目录列表——大书库（约 5700 本）不会再因遍历过超时而列不出目录。

### 四、删除与清理

- **删除整本书**：卡片新增删除按钮，醒目确认弹窗（删除后不可恢复），连带删除书库文件夹；"未分类合集"受保护不可整本删除。
- **最近播放清理**：支持一键清空全部播放记录（二次确认），单条记录卡片右上角 ✕ 单独删除。
- **清除播放进度**：详情页一键清除该书所有章节的播放记录（二次确认），配合重新听。
- 新增 `POST /api/clean-empty-dirs` 空目录清理接口。

### 五、播放体验

- **播放速度按书记忆**：倍速循环 0.75 / 1 / 1.25 / 1.5 / 1.75 / 2，按书独立保存（localStorage + 服务端 `playbackRates`），换书/刷新后自动恢复；详情页与播放器页均显示本书当前速度。
- **播放/暂停按钮切换**：卡片播放按钮播放中变两条竖线 ❚❚，点击暂停、再点恢复；播放另一本书时上一本自动恢复 ▶，跨卡片状态实时同步。
- **详情页操作栏**：`继续播放 / 从第一集播放 / 暂停 / 播放速度 / 清除播放进度 / 收藏 / 刷新`
  - 「继续播放」自动定位到**进度最大的未完成章节**续播；
  - 暂停/播放与倍速按钮随 audio 事件实时同步。
- **修复自动跳章 bug**：原版章节播放完毕时 `ended`/`pause`/兜底定时器重复触发跳章，导致连跳两章（如 1→3）且中间章节被误标 100%。已加音频级 `_advancing` 锁，在新章节 `loadedmetadata` 及预加载超时时释放。

### 六、其他

- 全部前端修改集中在 `app.bundle` 注入（`index.html` 实际只加载该文件），HTML/CSS 层同步扩展；所有补丁带单次出现断言，构建即校验。

## 构建

```bash
python build.py            # 提取 vendor/audiobook.jsplugin.zip → 应用补丁 → 输出 dist/audiobook.jsplugin.zip
```

构建路径均可环境变量覆盖：

| 变量 | 说明 | 默认 |
|---|---|---|
| `AUDIOBOOK_SRC_ZIP` | 官方原包路径 | `vendor/audiobook.jsplugin.zip` |
| `JSC_EXE` | QuickJS 字节码编译器 | 本机 `@songloft/jsc-win32-x64` 安装路径 |
| `PLUGIN_VERSION` | 插件版本号 | `1.3.14` |

### CI 自动打包

`.github/workflows/build.yml`：

- push 到 `main` / `master` → 自动构建并上传构件；
- push `v*` tag → 自动构建并**创建 GitHub Release** 附带 `audiobook.jsplugin.zip`（版本号自动取 tag 名）。

```bash
git tag v1.3.14 && git push origin v1.3.14   # 即发布一个新版本
```

### 部署测试（本机多宿主）

```bash
node deploy.js                        # 同时部署到两台已配置的 Songloft 宿主（HTTP 热重载）
node deploy.js 192.168.1.88:8090     # 只部署指定宿主（逗号分隔多台）
```

## 仓库结构

```
build.py                       # 补丁体系（P=后端 H=HTML J=前端JS/CSS），rep() 单次断言防漂移
main.src.js                    # 后端源码参考（与官方 main.jsc 对照）
orig_bundle.js / orig_main.js  # 官方原始前端/后端基线
vendor/audiobook.jsplugin.zip  # 官方原包（构建输入）
deploy.js                      # 多宿主部署脚本
.github/workflows/build.yml    # CI 自动打包 / Release
```

## 免责声明

仅供个人学习与自用，原作者版权归 MiMusic Team 所有。
