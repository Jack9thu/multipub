"""scripts/install_skill.py：把仓库 skill 装成全局 skill（全部离线，装到临时目录）。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / ".claude" / "skills" / "multipub"


def _load():
    spec = importlib.util.spec_from_file_location("install_skill", REPO / "scripts" / "install_skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inst = _load()


def run(dest: Path, *args: str) -> int:
    return inst.main(["--dest", str(dest), *args])


def md_texts(dest: Path) -> dict[str, str]:
    return {p.relative_to(dest).as_posix(): p.read_text(encoding="utf-8") for p in dest.rglob("*.md")
            if p.relative_to(dest).parts[0] != "local"}


def test_generic_rules_still_match_repo_wording(tmp_path: Path):
    """仓库文档改了措辞时，通用替换会悄悄失效：每条字符串规则都要命中，且装完不剩依赖仓库根目录的命令。"""
    assert run(tmp_path / "g") == 0
    installer = inst.Installer(REPO, tmp_path / "g")
    for rel in inst._files(SKILL):
        if rel.endswith(".md"):
            installer.transform(rel, (SKILL / rel).read_text(encoding="utf-8"))
    missed = [old for old, _ in installer.strings if not installer.hits.get(old)]
    assert not missed, f"这些替换规则在仓库文档里一处都没匹配上，改 install_skill.generic_rules：{missed}"
    for rel, text in md_texts(tmp_path / "g").items():
        assert not re.search(r"uv run mpub(?!`)", text), rel
        assert "uv run python .claude/skills/multipub/scripts/" not in text, rel
        assert "`docs/" not in text, rel


def test_install_fresh_then_idempotent(tmp_path: Path, capsys):
    dest = tmp_path / "skills" / "multipub"
    assert run(dest, "--check") == 0
    assert not dest.exists()  # --check 不写
    assert run(dest) == 0
    skill = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert f'uv run --project "{REPO.as_posix()}" mpub' in skill
    assert f'python {(dest / "scripts").as_posix()}/content_check.py' in skill
    assert (dest / "scripts" / "content_check.py").read_bytes() == (SKILL / "scripts" / "content_check.py").read_bytes()
    record = json.loads((dest / "local" / ".installed.json").read_text(encoding="utf-8"))["files"]
    assert set(record) == set(inst._files(SKILL))
    capsys.readouterr()
    assert run(dest) == 0
    assert "已更新 0 个文件" in capsys.readouterr().out


def test_local_overlay(tmp_path: Path, capsys):
    dest = tmp_path / "multipub"
    (dest / "local").mkdir(parents=True)
    (dest / "local" / "description.txt").write_text("本机触发：投稿到合集。", encoding="utf-8")
    (dest / "local" / "SKILL.local.md").write_text("## 本机补充（说明）\n\n本机内容。\n", encoding="utf-8")
    (dest / "local" / "replacements.json").write_text(json.dumps([
        ["需要在自己的全局 skill 里补充的内容，见 SKILL.md「本机适配」。", "本机的做法见 [my.md](my.md)。"],
        ["仓库里没有这句话", "x"]], ensure_ascii=False), encoding="utf-8")
    (dest / "references").mkdir()
    (dest / "references" / "my.md").write_text("我的规范", encoding="utf-8")
    assert run(dest) == 0
    out = capsys.readouterr().out
    skill = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"^description: .*本机触发：投稿到合集。Use when", skill, flags=re.M)
    assert skill.rstrip().endswith("本机内容。") and "\n\n## 本机补充（说明）" in skill
    assert "本机内容见末尾「本机补充」" in skill
    assert "本机的做法见 [my.md](my.md)。" in (dest / "references" / "prepare.md").read_text(encoding="utf-8")
    assert (dest / "references" / "my.md").read_text(encoding="utf-8") == "我的规范"
    assert "references/my.md" in out and "仓库里没有这句话" in out


def test_hand_edits_and_clashes_are_not_overwritten(tmp_path: Path, capsys):
    dest = tmp_path / "multipub"
    assert run(dest) == 0
    prepare = dest / "references" / "prepare.md"
    prepare.write_text("手改过", encoding="utf-8")
    capsys.readouterr()
    assert run(dest) == 1
    assert "references/prepare.md（装过之后被改过）" in capsys.readouterr().out
    assert prepare.read_text(encoding="utf-8") == "手改过"
    assert run(dest) == 1  # 记录没被这次跳过冲掉，下次还会拦
    assert run(dest, "--force") == 0
    assert prepare.read_text(encoding="utf-8") != "手改过"

    # 第一次装到已有目录（例如以前手动复制的）：内容不同的同名文件也不覆盖
    other = tmp_path / "old"
    (other / "references").mkdir(parents=True)
    (other / "references" / "prepare.md").write_text("以前手动复制、改过的", encoding="utf-8")
    capsys.readouterr()
    assert run(other) == 1
    assert "不是本脚本装的" in capsys.readouterr().out
    assert (other / "SKILL.md").is_file()  # 其他文件照常装上


def test_files_removed_from_repo(tmp_path: Path, capsys):
    dest = tmp_path / "multipub"
    assert run(dest) == 0
    manifest = dest / "local" / ".installed.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for name, text in (("references/gone.md", "旧文件"), ("references/gone-edited.md", "旧文件")):
        (dest / name).write_text(text, encoding="utf-8")
        data["files"][name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (dest / "references" / "gone-edited.md").write_text("改过", encoding="utf-8")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert run(dest) == 0
    out = capsys.readouterr().out
    assert not (dest / "references" / "gone.md").exists()
    assert (dest / "references" / "gone-edited.md").read_text(encoding="utf-8") == "改过"
    assert "references/gone-edited.md" in out


def test_paths_with_spaces_are_quoted(tmp_path: Path):
    dest = tmp_path / "my skills" / "multipub"
    assert run(dest) == 0
    skill = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert f'python "{(dest / "scripts").as_posix()}/content_check.py"' in skill


def test_bad_replacements_file(tmp_path: Path):
    dest = tmp_path / "multipub"
    (dest / "local").mkdir(parents=True)
    (dest / "local" / "replacements.json").write_text('{"a": "b"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="replacements.json"):
        run(dest)
