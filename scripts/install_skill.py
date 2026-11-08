"""把仓库里的 multipub skill 装成（或更新为）用户级的全局 skill，在任何目录都能用。

  uv run python scripts/install_skill.py            # 安装 / 更新到 ~/.claude/skills/multipub
  uv run python scripts/install_skill.py --check    # 只列出会改哪些文件，不写
  uv run python scripts/install_skill.py --dest <目录> [--force]

做的事：
1. 把仓库 .claude/skills/multipub/ 下的文件复制到目标目录；.md 里依赖「当前目录是仓库根目录」的命令和链接
   换成带仓库路径的写法（uv run mpub → uv run --project "<仓库>" mpub，脚本和 docs/ 链接写成绝对路径），
   仓库位置按本脚本所在位置自动识别；
2. 补回本机内容，都放在目标目录的 local/ 下（都可选，没有就跳过）：
   - local/description.txt：插进 SKILL.md 的 description，「Use when」之前（加本机的触发说明）；
   - local/SKILL.local.md：接在 SKILL.md 末尾；
   - local/replacements.json：本机的字符串替换，[["仓库里的原文", "换成"], ...]，在通用替换之前、
     对仓库原文做（例如把一句话换成指向本机文件的链接）；一处都没匹配上会提示；
   自己加的其他文件（如 references/my-notes.md）直接放进目标目录：仓库里没有的文件一律不碰；
3. 在 local/.installed.json 记下这次从仓库装了哪些文件及其内容的 sha256。下次更新时：
   - 装过之后被手改过的文件不覆盖，列出来（改动应放进 local/，加 --force 才覆盖）；
   - 仓库新增的文件和目标目录里已有的同名文件冲突时同样不覆盖；
   - 仓库删掉的文件，没被改过就删除，改过的只提示。

只用标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Callable, Union

Repl = Union[str, Callable[[re.Match[str]], str]]

REPO =Path(__file__).resolve().parent.parent
SKILL_REL = Path(".claude/skills/multipub")
DEFAULT_DEST = Path.home() / ".claude" / "skills" / "multipub"
MANIFEST = "local/.installed.json"


def _quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


def generic_rules(repo: Path, dest: Path, local_heading: str | None
                  ) -> tuple[list[tuple[str, str]], list[tuple[re.Pattern[str], Repl]]]:
    """通用替换：(字符串规则, 正则规则)。字符串规则每条都应该在仓库的 .md 里至少命中一次（测试会检查）。"""
    repo_posix = repo.as_posix()
    cmd = f'uv run --project "{repo_posix}"'
    note = f"，本机内容见末尾「{local_heading}」" if local_heading else ""
    strings = [
        ("在**仓库根目录**运行", "在**任意目录**运行"),
        ("仓库根目录下 `uv run", "任意目录下 `uv run"),
        ("在仓库根目录运行（不在仓库目录时用 `uv run --project <仓库路径> python <脚本路径>`）：", "在任意目录运行："),
        ("multipub 是本仓库里的命令行工具 `mpub`。",
         f"multipub 是装在 `{repo}` 的命令行工具 `mpub`（本 skill 是从仓库 `.claude\\skills\\multipub` 生成的全局版{note}）。"),
    ]
    scripts = (dest / "scripts").as_posix() + "/"
    regexes = [
        # 脚本是相对仓库根目录的路径，换成目标目录里那份的绝对路径，并带上 multipub 的环境
        (re.compile(r"uv run python \.claude/skills/multipub/scripts/(\S+)"),
         lambda m: f"{cmd} python {_quote(scripts + m.group(1))}"),
        # 只换命令，不换说明文字里的 `uv run mpub`（后面紧跟反引号的是在讲写法本身）
        (re.compile(r"uv run mpub(?!`)"), f"{cmd} mpub"),
        (re.compile(r"`docs/"), f"`{repo_posix}/docs/"),
    ]
    return strings, regexes


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(root: Path, *, skip_local: bool = False) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(root)
        if skip_local and rel.parts[0] == "local":
            continue
        out.append(rel.as_posix())
    return sorted(out)


class Installer:
    def __init__(self, repo: Path, dest: Path):
        self.repo = repo
        self.src = repo / SKILL_REL
        self.dest = dest
        self.local = dest / "local"
        self.hits: dict[str, int] = {}
        self.local_rules: list[tuple[str, str]] = []
        self.description = ""
        self.tail = ""
        heading = None
        if (self.local / "SKILL.local.md").is_file():
            self.tail = (self.local / "SKILL.local.md").read_text(encoding="utf-8")
            m = re.search(r"^#+\s*(.+?)(?:（.*）)?\s*$", self.tail, flags=re.M)
            heading = m.group(1).strip() if m else None
        if (self.local / "description.txt").is_file():
            self.description = (self.local / "description.txt").read_text(encoding="utf-8").strip()
        if (self.local / "replacements.json").is_file():
            rules = json.loads((self.local / "replacements.json").read_text(encoding="utf-8"))
            if not isinstance(rules, list) or not all(
                    isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r) for r in rules):
                raise SystemExit('local/replacements.json 应为 [["原文", "换成"], ...]')
            self.local_rules = [(a, b) for a, b in rules]
        self.strings, self.regexes = generic_rules(repo, dest, heading)

    def transform(self, rel: str, text: str) -> str:
        for old, new in self.local_rules + self.strings:
            self.hits[old] = self.hits.get(old, 0) + text.count(old)
            text = text.replace(old, new)
        for pat, new in self.regexes:
            text = pat.sub(new, text)
        if rel == "SKILL.md":
            if self.description:
                text, n = re.subn(r"^(description: .*?)(Use when)", lambda m: m.group(1) + self.description + m.group(2),
                                  text, count=1, flags=re.M)
                if not n:
                    raise SystemExit("SKILL.md 的 description 里没找到「Use when」，没法插入 local/description.txt")
            if self.tail:
                text = text.rstrip("\n") + "\n\n" + self.tail.lstrip("\n")
        return text

    def render(self, rel: str) -> bytes:
        src = self.src / rel
        if rel.endswith(".md"):
            return self.transform(rel, src.read_text(encoding="utf-8")).encode("utf-8")
        return src.read_bytes()

    def run(self, *, check: bool, force: bool) -> int:
        if not (self.src / "SKILL.md").is_file():
            print(f"找不到仓库里的 skill：{self.src}", file=sys.stderr)
            return 2
        manifest_path = self.dest / MANIFEST
        installed: dict[str, str] = {}
        if manifest_path.is_file():
            installed = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})

        repo_files = _files(self.src)
        written, conflicts, record = [], [], {}
        for rel in repo_files:
            new = self.render(rel)
            dst = self.dest / rel
            old = dst.read_bytes() if dst.is_file() else None
            record[rel] = _sha(new)
            if old == new:
                continue
            if old is not None and installed.get(rel) != _sha(old) and not force:
                why = "装过之后被改过" if rel in installed else "目标目录里已有同名文件（不是本脚本装的）"
                conflicts.append(f"{rel}（{why}）")
                if rel in installed:
                    record[rel] = installed[rel]
                else:
                    record.pop(rel)
                continue
            written.append(rel + ("" if old is not None else "（新文件）"))
            if not check:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(new)

        removed, kept = [], []
        for rel in sorted(set(installed) - set(repo_files)):
            dst = self.dest / rel
            if not dst.is_file():
                continue
            if _sha(dst.read_bytes()) == installed[rel]:
                removed.append(rel)
                if not check:
                    dst.unlink()
            else:
                kept.append(rel)
                record[rel] = installed[rel]
        own = [r for r in _files(self.dest, skip_local=True) if r not in record and r not in removed] \
            if self.dest.is_dir() else []

        if not check:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps({"repo": str(self.repo), "files": record}, ensure_ascii=False, indent=1)
                                     + "\n", encoding="utf-8")

        verb = "会" if check else "已"
        print(f"仓库：{self.repo}\n目标：{self.dest}")
        print(f"{verb}更新 {len(written)} 个文件" + "".join(f"\n  {w}" for w in written))
        if removed:
            print(f"仓库里已经删掉、{verb}从目标目录删除：" + "".join(f"\n  {r}" for r in removed))
        if kept:
            print("仓库里已经删掉、但装过之后被改过，没有删除（确认后手动删）：" + "".join(f"\n  {r}" for r in kept))
        if own:
            print("本机自己加的文件，保持不动：" + "".join(f"\n  {r}" for r in own))
        stale = [old for old, _ in self.local_rules if not self.hits.get(old)]
        if stale:
            print("注意：local/replacements.json 里这些规则一处都没匹配上（仓库的措辞可能改了）：" +
                  "".join(f"\n  {s[:60]}" for s in stale))
        if conflicts:
            print("没有覆盖（要改的内容请放进 local/，确认不要这些改动后加 --force）：" +
                  "".join(f"\n  {c}" for c in conflicts))
            return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=f"全局 skill 目录（默认 {DEFAULT_DEST}）")
    ap.add_argument("--check", action="store_true", help="只列出会改哪些文件，不写")
    ap.add_argument("--force", action="store_true", help="覆盖装过之后被改过的文件、和同名的已有文件")
    a = ap.parse_args(argv)
    return Installer(REPO, a.dest.expanduser().resolve()).run(check=a.check, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
