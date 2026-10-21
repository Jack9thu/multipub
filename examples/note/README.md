# 图文示例

把图片放进本目录的 `images/` 子目录：

| 文件 | 必需 | 说明 |
|---|---|---|
| `images/1.jpg` | ✔ | 第一张图（抖音、小红书用它做封面） |
| `images/2.jpg`、`images/3.jpg` | 可选 | 不需要就从 `post.yaml` 的 `images` 里删掉 |

图片数量与格式：B站 1–9 张 jpg/png/gif（单张 ≤20MB）；抖音 1–35 张 jpg/png/webp/bmp（不支持 GIF）；
小红书 1–18 张 jpg/png/webp/bmp；知乎想法 ≤9 张 jpg/png/gif/webp/bmp。

```powershell
uv run mpub validate examples/note           # 缺图时会报「文件不存在」
uv run mpub publish examples/note --dry-run
```

B站动态第一次发布时会遇到《动态发布规范》确认：默认无头模式会停下来返回 `consent_required`，
处理办法见 [docs/platforms/bilibili.md](../../docs/platforms/bilibili.md) 的「动态发布规范」一节。
