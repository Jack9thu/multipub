# 长文示例（知乎文章）

| 文件 | 必需 | 说明 |
|---|---|---|
| `article.md` | ✔ | 正文（已附示例），Markdown：标题、粗斜体、代码块、表格、公式、脚注、图片 |
| `images/branches.png` | ✔（`article.md` 引用了它） | 正文插图；不需要的话从 `article.md` 里删掉那一行 |
| `cover.jpg` | 可选 | 文章题图；不需要就删掉 `post.yaml` 的 `cover:` 那一行 |

图片支持 jpg / png / gif / webp / bmp（webp、bmp、gif 会转成 JPEG，GIF 只保留第一帧）；
也可以直接引用 `https://` 图片，发布时会下载后转存到知乎图床。

```powershell
uv run mpub validate examples/article        # 缺图时会报「找不到正文里引用的图片」/「文件不存在」
uv run mpub publish examples/article --dry-run
```

示例里 `draft: true`：只存草稿并返回编辑页链接，到知乎编辑页检查无误后自己点「发布」。
