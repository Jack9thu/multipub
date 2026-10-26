# post.yaml 参考

一条内容一个目录，目录里放一个 `post.yaml`（发布清单）和它引用的媒体文件。所有命令都接受
`post.yaml` 的路径或它所在的目录：

```powershell
uv run mpub new posts\ep12 --kind video      # 生成模板（article 还会生成 article.md）
uv run mpub validate posts\ep12              # 本地校验（不联网、不检查登录）
uv run mpub publish posts\ep12 --dry-run     # 同样的校验，按发布的格式输出
```

文件用 UTF-8 保存（带不带 BOM 都行）；PowerShell 5.1 重定向写出的 UTF-16、记事本「ANSI」（GBK）也能读，`body_file` 同样。可直接参考 [`examples/`](../examples/) 里的三个例子。

## 结构

```yaml
kind: video                 # 必填：video / note / article
title: 标题                 # ↓ 顶层字段是所有平台共用的默认值
body: |
  正文 / 简介
tags: [科技, 教程]
video: video.mp4
account: main

platforms:                  # 要发的平台；每个平台下可以覆盖顶层字段，再加平台私有参数
  bilibili:
    tid: 231                # 平台私有参数
  douyin:
    title: 抖音单独用的标题   # 覆盖顶层 title
  xiaohongshu: {}           # 不覆盖任何东西
```

## 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | 字符串，**必填** | `video` 视频 / `note` 图文（若干图片 + 一段文字）/ `article` 长文（Markdown） |
| `title` | 字符串 | 标题。各平台长度限制不同，见下文「各平台发什么」 |
| `body` | 字符串 | 正文 / 简介 / 描述。多行用 YAML 的 `\|` 块写法；首尾空行会去掉 |
| `body_file` | 路径 | 从文件读正文（UTF-8），**优先于** `body`。长文一般写 `body_file: article.md` |
| `tags` | 列表或字符串 | 标签 / 话题。列表 `[a, b]`，或字符串 `"a,b，c #d"`（按中英文逗号、分号、空白拆分）；开头结尾的 `#` 会去掉，重复的去掉 |
| `video` | 路径 | 视频文件（`kind: video` 必填） |
| `cover` | 路径 | 封面（视频封面 / 知乎文章题图）；图文类内容一般以第一张图为封面，`cover` 会被忽略并给出警告 |
| `images` | 路径列表 | 图片（`kind: note` 至少 1 张）；知乎文章会把正文里没引用到的图片依次追加在文末 |
| `schedule` | 字符串 | 定时发布，**本地时间** `"YYYY-MM-DD HH:MM"`（也接受 `YYYY-MM-DD HH:MM:SS`、`YYYY/MM/DD HH:MM`、`T` 分隔）。请加引号。必须晚于现在，各平台另有窗口（见下文）；知乎不支持 |
| `ai_generated` | 布尔，默认 `false` | 内容含 AI 生成时设为 `true`：在支持的平台上勾选 AI 声明（各平台做法见下文） |
| `account` | 字符串，默认 `default` | 账号名，见「账号」 |
| `platforms` | 映射或列表 | 目标平台，见「platforms」 |

`schedule` 不加引号且带秒（如 `2026-10-21 20:00:00`）时 YAML 会把它解析成时间对象，也能用；带时区
（如 `2026-10-21 20:00:00+08:00`）时换算成本机时间。

### YAML 的几个坑

- **`#` 前有空格就是注释**：`tags: #科技 #教程`、`body: 今天去徒步了 #徒步` 里的 `#…` 会被 YAML 整个吞掉。
  multipub 检查 `title` / `body` / `tags` 时发现这种写法会直接报错（不会悄悄发一条没有话题的内容）。
  `tags` 不用写 `#`：`tags: [科技, 教程]`；正文或标题里要带 `#话题`，给整个值加引号（`body: "今天去徒步了 #徒步"`）
  或用 `body: |` 多行写法（块里的 `#` 是普通字符）。
- **不加引号的数字、时间、yes/no**：`title: 12:30`、`title: 1.10`、`title: no`、`account: 007` 在 YAML 里分别是
  750、1.1、false、7。multipub 对 `title` `body` `tags` `account` 和各个路径字段（以及 `dynamic` `source` `collection`
  等文字参数）一律按你写的原文处理，所以不加引号也没问题；其他字段（如 `tid`、`ai_generated`）按 YAML 类型读。
- **双引号里的反斜杠是转义符**：见下文「路径」。

## platforms

```yaml
platforms:
  bilibili: {}            # 发，不覆盖（写 bilibili: 或 bilibili: true 也一样）
  douyin:
    enabled: false        # 暂时不发（写 douyin: false 也一样）
  xiaohongshu:
    title: 小红书标题
```

- 也可以写成列表 `platforms: [bilibili, douyin]`，或一行字符串 `platforms: b站, 抖音`。
- 平台名不分大小写，支持别名：`bilibili` / `bili` / `b站` / `b`；`douyin` / `dy` / `抖音`；
  `xiaohongshu` / `xhs` / `小红书` / `rednote`；`zhihu` / `zh` / `知乎`。
- 命令行 `--to bilibili,douyin` 会**替换**清单里的平台列表（包括 `enabled: false` 的、甚至清单里没写的平台——
  没写的平台只用顶层字段）。
- 平台不支持这个 `kind` 时自动跳过（结果 `status: skipped`，`error_code: unsupported`），不算失败。

### 覆盖规则

- `platforms.<平台>` 下可以覆盖的通用字段：`title` `body` `body_file` `tags` `video` `cover` `images`
  `schedule` `ai_generated` `account`。
- 覆盖值为 `null`（或不写）时用顶层的值；想对某个平台清掉顶层的值，写空值：`tags: []`、`cover: ""`。
- 正文的取法：平台写了 `body_file` → 用它；平台只写了 `body` → 用平台的 `body`（顶层 `body_file` 不再生效）；
  否则顶层 `body_file` 优先于顶层 `body`。
- 其余的键都是**平台私有参数**（见下文）。写错名字不会报错，只会在校验时给出 `warning`（「未知参数，会被忽略」）。

## 路径

- `video` `cover` `images` `body_file` 以及抖音的 `thumbnail_portrait` / `thumbnail_landscape`：
  相对路径按 **post.yaml 所在目录** 解析；支持绝对路径和 `~`。
- Windows 路径在 YAML 里请用单引号或正斜杠：`'D:\素材\第12期.mp4'` 或 `D:/素材/第12期.mp4`。
  双引号里的反斜杠是转义符，要写成 `"D:\\素材\\第12期.mp4"`。中文和空格都没问题。
- 知乎文章 Markdown 里的本地图片：相对路径先按 **`body_file` 所在目录** 找，再按 post.yaml 所在目录找；
  文件名有空格时写 `![](<我的 图.png>)`；也支持 `file:///` 和绝对路径；`http(s)` 图片会被下载后转存到知乎图床。

## 账号

- 账号名只能包含字母、数字、`_`、`-`（1–64 位），对应登录文件 `<数据目录>\accounts\<平台>\<账号>.json`，
  由 `mpub login <平台> --account <账号>` 生成；`mpub accounts` 列出已登录的账号。
- 优先级：命令行 `--account`（所有平台统一）> `platforms.<平台>.account` > 顶层 `account` > `default`。
- 不同平台可以用不同账号名；同一平台同一账号同一时间只能跑一个任务（账号锁）。

## 平台私有参数

写在 `platforms.<平台>` 下。下表是代码里实际读取的键和默认值；更细的行为见 `docs/platforms/<平台>.md`。

### B站（`platforms.bilibili`）

视频（`kind: video`，经 biliup 投稿）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `tid` | **必填** | 分区 id，如 231 计算机技术、21 日常、160 生活、95 数码、201 科学科普、138 搞笑、36 知识（以创作中心为准） |
| `tid_v2` | 不传 | 新版分区 id |
| `copyright` | `1` | 1 自制 / 2 转载 |
| `source` | 无 | 转载来源，`copyright: 2` 时必填 |
| `dynamic` | 无 | 投稿时同步发的粉丝动态文案 |
| `creation_statement` | `ai_generated: true` 时为 `1`，否则不传 | 创作声明：-1 无 / 1 AI / 2 虚构演绎 / 3 营销 / 4 个人观点（2026-10-25 真实投稿时带上、投稿成功；创作中心里显示是否正确**未核对**） |
| `no_reprint` | `false` | 禁止转载 |
| `is_only_self` | `false` | 仅自己可见 |
| `selection_reply` / `close_reply` / `close_danmu` | `false` | 精选评论 / 关闭评论 / 关闭弹幕 |
| `collection` | 无 | 投稿成功后立即加入已有合集（名称精确匹配；审核中就能加入；找不到或加不进只告警，照常投稿） |
| `extra_fields` | 无 | 映射，原样合并进投稿参数（高级用法） |
| `line` | 自动 | 上传线路：`bldsa cnbldsa andsa atdsa bda2 cnbd anbd atbd tx cntx antx attx txa alia estx akbd` |
| `limit` | 3 | 单文件并发分片数（1–64） |
| `submit` | `app` | 投稿接口：`app` / `web` / `b-cut-android` |

图文（`kind: note`，发成 B站动态）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `mode` | `browser` | `browser` 浏览器在 t.bilibili.com 发布框里发；`http` 直接调 Web 接口（实验性，更容易被风控） |
| `pic_mode` | `0` | 0 九宫格 / 1 大封面 |
| `tags_in_text` | `true` | 把 `tags` 以 `#标签#` 追加到正文末尾 |
| `confirm_rules` | 不写 | `true`：本条允许 multipub 代为确认《动态发布规范》弹窗；`false`：本条不代为确认；不写则看 config.toml 和确认记录。见 [bilibili.md「动态发布规范」](platforms/bilibili.md) |

### 抖音（`platforms.douyin`）

| 参数 | 适用 | 默认 | 说明 |
|---|---|---|---|
| `declaration` | video | 不声明；`ai_generated: true` 时为 `内容由AI生成` | 「自主声明」选项文字，必须与页面完全一致（如 `内容由AI生成`、`内容为个人观点或见解`）；`false` / `none` 表示不声明。设置失败会拒绝发布 |
| `collection` | video | 无 | 加入已有合集（名称精确匹配；找不到跳过并告警） |
| `product_link` / `product_title` | video | 无 | 商品链接与短标题（≤10 字），两项都填才生效 |
| `sync_toutiao` | video | 不写（不碰开关） | 不写保持页面默认；`true` 打开「同步到今日头条/西瓜视频」（打不开只告警）；`false` 把开着的开关关掉，关不掉则拒绝发布 |
| `thumbnail_portrait` | video | 无 | 竖封面（3:4），路径相对 post.yaml |
| `thumbnail_landscape` | video | 无 | 横封面（4:3）；与竖封面同时填时只应用竖封面 |
| `bgm` | note | 无 | 背景音乐名称（搜索后选第一条，搜不到跳过） |

没填 `thumbnail_*` 时，`cover` 按宽高自动放进横封面（宽 > 高）或竖封面。

### 小红书（`platforms.xiaohongshu`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `declaration` | 不写（跟随 `ai_generated`） | 「内容类型声明」选项文字，必须与页面一致：`虚构演绎，仅供娱乐` / `笔记含AI合成内容` / `内容包含营销广告`；`false` 表示不声明。设置不上则拒绝发布（validation） |
| `collection` | 无 | 加入已有合集（名称精确匹配；选不上只告警，照常发布） |
| `repost_source` | 无（不声明） | 填写后声明「来源转载」并填入媒体名称；设置后会核实，设置不上则拒绝发布（validation） |

### 知乎（`platforms.zhihu`）

| 参数 | 默认 | 适用 | 说明 |
|---|---|---|---|
| `as` | note → `pin`；article → `article` | note | `pin` 发想法；`article` 发专栏文章（长文只能是 `article`） |
| `draft` | `false` | 文章 | `true` 只存草稿，返回编辑页链接，由你手动点发布（想法不支持） |
| `statement` | 跟随 `ai_generated` | 文章 | 创作声明：`none` / `ai` / `spoiler` / `medical` / `fiction` / `finance` |
| `column` | 无 | 文章 | 发到自己的专栏：`c_1234567890` 或专栏链接 |
| `toc` | `false` | 文章 | 开启文章目录 |
| `tags_in_text` | `false` | 想法 | 把 `#话题#` 追加到正文末尾（是否变成真正的话题未验证） |

## 各平台发什么（kind → 平台内容）

✘ 表示自动跳过。长度按字符数计算（emoji 等可能和平台的计数不同）。

### `kind: video`

| 平台 | 发成 | 字段怎么用 |
|---|---|---|
| B站 | 视频投稿（biliup） | `title` 必填 ≤80；`body` → 简介 ≤2000（为空用标题）；`tags` 1–12 个、每个 ≤20 字、不能含逗号；`cover` jpg/png；`schedule` 晚于现在 2 小时~15 天；`ai_generated` → `creation_statement: 1` |
| 抖音 | 视频 | `title` 必填 ≤30；`body` → 作品描述（>1000 字警告）；`tags` → 话题（>5 个警告）；`cover` → 横/竖封面；`schedule` 晚于现在 2 小时以上（>14 天警告）；`ai_generated` → 自主声明「内容由AI生成」（`declaration` 优先） |
| 小红书 | 视频笔记 | `title` 必填 ≤20；`body` ≤1000；`tags` ≤10 个话题；`cover` → 封面；`schedule` 晚于现在 2 小时以上；`ai_generated` → 尽力选「笔记含AI合成内容」（选不上只告警，仍发布；`declaration` 优先，选不上拒绝发布） |
| 知乎 | ✘ | 暂不支持视频 |

视频格式：B站 `.mp4 .flv .avi .wmv .mov .webm .mpeg .mpg .mkv .m4v .ts .rm .rmvb .3gp`（其他给警告）；
抖音、小红书 `.mp4 .mov .avi .mkv .m4v .webm .flv .wmv`（其他报错）。

### `kind: note`（图文）

| 平台 | 发成 | 字段怎么用 |
|---|---|---|
| B站 | 动态（opus） | `title` 选填 ≤20；`body` + 末尾 `#标签#` ≤1000；`images` 1–9 张 jpg/png/gif、单张 ≤20MB；`schedule` 晚于现在 5 分钟~7 天；`ai_generated` → AI 声明；`cover` 忽略 |
| 抖音 | 图文 | `title` 必填 ≤20；`body` ≤1000；`images` 1–35 张 jpg/jpeg/png/webp/bmp（不支持 GIF）；`tags` → 话题；`schedule` 晚于现在 2 小时以上；`ai_generated` 不支持（警告）；`cover` 忽略 |
| 小红书 | 图文笔记 | `title` ≤20（不填用正文前 20 字）；`body` ≤1000；`images` 1–18 张 jpg/jpeg/png/webp/bmp；`tags` ≤10；`schedule` 晚于现在 2 小时以上；`ai_generated` 尽力声明；`cover` 忽略 |
| 知乎 | 想法（默认） | `body` 纯文本（一行一段）；`images` ≤9 张；`title` 选填；`tags` 默认忽略（`tags_in_text: true` 时附在正文末尾）；`cover` / `ai_generated` 忽略；`schedule` 报错 |
| 知乎 + `as: article` | 专栏文章 | `body` 按 Markdown 渲染，`images` 依次放在文末；`title` 必填 ≤100；其余同下面的 `article` |

### `kind: article`（长文）

| 平台 | 发成 | 字段怎么用 |
|---|---|---|
| 知乎 | 专栏文章 | `title` 必填 ≤100；`body` / `body_file` 为 Markdown（标题、粗斜体、代码、表格、公式、脚注、图片）；本地和远程图片先传到知乎图床；`images` 中正文没引用的追加在文末；`cover` → 题图；`tags` → 话题（尽力匹配前 3 个，匹配不到只警告）；`ai_generated` → 创作声明「包含 AI 辅助创作」（`statement` 优先）；`schedule` 报错；`draft: true` 只存草稿 |
| B站 / 抖音 / 小红书 | ✘ | 暂不支持 |

Markdown 开头的 YAML front matter 和唯一的一级标题会被丢掉（标题以 `title` 为准）。
完整的 Markdown → 知乎 HTML 规则见 [zhihu.md](platforms/zhihu.md)。

## 校验

`mpub validate` 和 `mpub publish --dry-run` 做同样的本地检查，都**不联网、不检查登录**：

- 通用：账号名合法；视频文件存在；图文至少 1 张图且文件都存在；长文正文非空；`cover` 存在；`schedule` 晚于现在。
- 平台：上面各表里的长度、数量、格式、定时窗口、参数取值，以及未知参数提示。
- 账号：登录文件不存在时给 warning（「账号 X 还没有登录」，并列出这个平台已登录的账号名）。
- `error` 会阻止发布（退出码 2）；`warning` 只提示。真正发布时 worker 里还会再校验一次（定时窗口可能在等待中过期）。
- `--dry-run` 另外会读本地发布记录，提前标出真发时会被查重跳过的平台（见「查重」）。

登录是否有效（cookie 有没有过期）要用 `mpub check <平台>` 单独检查。

## 查重

每次真正发布都会记到 `<数据目录>\history.jsonl`。同一平台、同一账号、同样的内容（`kind`、`title`、正文、
视频和图片文件的名字/大小/修改时间、平台私有参数）已经是 `published` / `submitted` / `scheduled`，
或上次结果是 `unknown_outcome`（已点发布但结果未知）时，再发会被跳过
（`status: skipped`，消息「同一内容已于 …」）。`--dry-run` 也会提前显示哪些平台会被跳过。
确认确实要再发一次时加 `--force`，并用 `--to` 只写要重发的平台（`--force` 对本次所有目标平台生效）。
只改 `tags`、`cover`、`schedule` 不算新内容。

点了发布的那一刻会先记一笔「结果待定」，所以即使 mpub 在发布途中被强行结束（关窗口、任务管理器），
下次也不会原样重发；两个 mpub 同时发同一条内容时，同一平台账号也只会发一次（另一个被跳过或报「正被另一个任务使用」）。

## 完整示例

```yaml
kind: video
title: 第12期：三分钟讲清楚 XXX
body: |
  视频简介写在这里。
tags: [科技, 教程]
video: video.mp4
cover: cover.jpg
schedule: "2026-10-25 20:00"
ai_generated: false
account: main

platforms:
  bilibili:
    tid: 231
    copyright: 1
    dynamic: 新视频上线啦
  douyin:
    title: 三分钟讲清楚 XXX
    collection: 科技小课堂
    # declaration: 内容为个人观点或见解
  xiaohongshu:
    title: 3分钟学会XXX🔥
    tags: [科技, 教程, 干货]
  zhihu: false               # 知乎不支持视频，写不写都会跳过
```
