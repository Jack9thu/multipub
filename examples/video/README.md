# 视频示例

把下面的文件放进本目录（文件名与 `post.yaml` 里一致，或者改 `post.yaml`）：

| 文件 | 必需 | 说明 |
|---|---|---|
| `video.mp4` | ✔ | 视频。B站支持常见格式；抖音、小红书只接受 `.mp4 .mov .avi .mkv .m4v .webm .flv .wmv` |
| `cover.jpg` | 可选 | 封面，jpg / png（B站只接受这两种，>5MB 会警告）。不想要封面就删掉 `cover:` 那一行 |

然后在仓库根目录运行：

```powershell
uv run mpub validate examples/video          # 本地校验；缺文件时会报「文件不存在」
uv run mpub publish examples/video --dry-run
uv run mpub publish examples/video           # 真正发布（会再问一次确认）
```

发到 B站需要先 `uv run mpub setup`（下载 biliup）。各平台限制见 [docs/manifest.md](../../docs/manifest.md)。
