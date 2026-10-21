"""提交包目标路径守卫：只使用隔离夹具，不触及真实交付目录。"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_submission_package as submission


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _synthetic_repo(tmp_path):
    """最小 git 仓库（提交包门禁会做 git 对账，没有 git 就 fail-closed）。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "launchers").mkdir()
    for name in (submission.LAUNCHER_NAME, submission.SHORTCUT_NAME):
        (root / "launchers" / name).write_text("test", encoding="utf-8")
    return root


@pytest.fixture
def package_root(tmp_path, monkeypatch):
    root = _synthetic_repo(tmp_path)
    source = root / "src/module.py"
    monkeypatch.setattr(submission, "REPO_ROOT", root)
    monkeypatch.setattr(submission.MD, "collect_delivery_files", lambda _: [source])
    # 门禁已收敛到唯一实现 MD.run_delivery_gate：夹具打桩该入口，
    # 不再逐个打桩子扫描器（否则新增判据会在夹具里被静默绕过）。
    monkeypatch.setattr(submission.MD, "run_delivery_gate", lambda *a, **k: {
        "violations": [], "secret_hits": [], "contact_hits": [],
        "colon_hits": [], "unscannable": [], "untracked": []})
    return root


@pytest.mark.parametrize("kind", ["root", "ancestor", "base", "source", "nonempty"])
def test_submission_rejects_destructive_output(package_root, kind):
    root = package_root
    out = {"root": root, "ancestor": root.parent, "base": root / "database/base",
           "source": root / "src", "nonempty": root.parent / "existing"}[kind]
    out.mkdir(parents=True, exist_ok=True)
    sentinel = out / "keep.txt"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(submission.SubmissionError):
        submission.build(out, None)
    assert sentinel.read_bytes() == b"preserve"
    assert (root / "src/module.py").is_file()


def test_submission_builds_new_dedicated_directory(package_root):
    out = package_root / "outputs/new-package"
    assert submission.build(out, None) == 0
    assert (out / "biodata-agent/src/module.py").read_text() == "VALUE = 1\n"


def test_submission_rejects_unscannable_utf16_text(tmp_path, monkeypatch, capsys):
    """UTF-16 文本（两类扫描器零命中）必须拦：提交包与 make_delivery 同一份完整门禁。"""
    root = _synthetic_repo(tmp_path)
    (root / "utf16.md").write_text("# Demo\nkey=" + ("sk-" + "test" * 5) + "\n", encoding="utf-16")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add utf16")
    monkeypatch.setattr(submission, "REPO_ROOT", root)
    out = tmp_path / "pkg"
    assert submission.build(out, None) == 1
    err = capsys.readouterr().err
    assert "不可解码文本 1" in err
    assert not out.exists()


def test_submission_rejects_untracked_required_module(tmp_path, monkeypatch, capsys):
    """未跟踪的新模块（干净 clone 里不存在，客户包会缺它）必须拦。"""
    root = _synthetic_repo(tmp_path)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    (root / "src" / "new_module.py").write_text("NEW = 1\n", encoding="utf-8")   # 未跟踪
    monkeypatch.setattr(submission, "REPO_ROOT", root)
    out = tmp_path / "pkg"
    assert submission.build(out, None) == 1
    assert "未跟踪件 1" in capsys.readouterr().err
    assert not out.exists()


# ------------------------------------------------------- 父级 junction 下的交付源 containment

def _make_dir_junction(link: Path, target: Path) -> None:
    """创建**真实**目录 junction（Windows `mklink /J`，无需 admin）或 POSIX 目录符号链接。
    （与 tests/test_delivery_safety.py 同名 helper 同源复制，改动须双同步。）"""
    if sys.platform.startswith("win"):
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            pytest.skip(f"本机无法创建目录 junction：{proc.stdout} {proc.stderr}")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"本机/该文件系统不支持创建目录符号链接：{exc}")
    assert submission.MD._is_link_like(link), "目录链接未真实创建（判据不可见）——不得当作已验证"


def _committed_repo_with_sub(tmp_path):
    """已提交的合成 repo：tracked `sub/inner.txt` + launchers；仓库外 external/ 仅 sentinel。"""
    root = _synthetic_repo(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "inner.txt").write_text("ok\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = b"OUTSIDE_BYTES_MUST_NOT_SHIP\n"
    (external / "inner.txt").write_bytes(sentinel)
    return root, external, sentinel


def test_submission_refuses_source_under_parent_junction_before_creating_output(tmp_path, monkeypatch):
    """提交包：tracked 后代位于父级 junction 时，**创建输出目录之前** fail-closed
    （真实 collect，不打桩——这是与 test_delivery_safety 同族问题的 submission 侧消费点）。"""
    root, external, sentinel = _committed_repo_with_sub(tmp_path)
    (root / "sub" / "inner.txt").unlink()
    (root / "sub").rmdir()
    _make_dir_junction(root / "sub", external)
    assert (root / "sub" / "inner.txt").read_bytes() == sentinel, "夹具前提：已能读到仓外字节"
    monkeypatch.setattr(submission, "REPO_ROOT", root)
    out = tmp_path / "pkg"
    with pytest.raises(submission.MD.DeliveryLinkError, match="符号链接"):
        submission.build(out, None)
    assert not out.exists(), "输出目录不得在复核前创建"


def test_submission_refuses_linked_launcher_before_copy(package_root, tmp_path):
    """额外 launchers 与交付集同一条 containment 闸：launchers 目录是 junction 时，
    即使 collect 被调用方打桩绕过，copy 前防线仍在——链接目标字节不进包。"""
    root = package_root
    external = tmp_path / "external-launchers"
    external.mkdir()
    sentinel = b"OUTSIDE_BYTES_MUST_NOT_SHIP\n"
    (external / submission.LAUNCHER_NAME).write_bytes(sentinel)
    (external / submission.SHORTCUT_NAME).write_text("test", encoding="utf-8")
    (root / "launchers" / submission.LAUNCHER_NAME).unlink()
    (root / "launchers" / submission.SHORTCUT_NAME).unlink()
    (root / "launchers").rmdir()
    _make_dir_junction(root / "launchers", external)
    assert (root / "launchers" / submission.LAUNCHER_NAME).read_bytes() == sentinel, \
        "夹具前提：已能读到仓外字节"
    out = tmp_path / "pkg"
    with pytest.raises(submission.MD.DeliveryLinkError, match="符号链接"):
        submission.build(out, None)
    assert not (out / submission.LAUNCHER_NAME).exists(), "链接 launcher 不得被 copy 进包"
