# 用 Git 分支管理个人项目的五个习惯

（上面这个一级标题会被丢掉，文章标题以 post.yaml 的 title 为准。）

个人项目也值得用分支：主分支随时可用，试验性的改动放在别处。

## 1. main 只放能跑的代码

每次合并前跑一遍测试，`main` 永远可以直接部署。

## 2. 一个想法一个分支

```bash
git switch -c feature/dark-mode
```

## 3. 用表格记录分支

| 分支 | 用途 | 状态 |
|---|---|---|
| `main` | 发布 | 稳定 |
| `feature/dark-mode` | 深色模式 | 进行中 |

## 4. 合并前先变基

保持提交历史整洁[^1]。

![分支示意图](images/branches.png)

## 5. 定期清理

合并后的分支及时删除：`git branch -d feature/dark-mode`。

[^1]: 公开分支不要随意改写历史。
