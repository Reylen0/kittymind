"""文件工具路径解析一致性（P0 回归，无需 LLM）。

背景：工作区隔离是这套权限模型的唯一强约束，而权限判定
（`permission._check_path_outside`）以 `bash_cwd`（当前会话工作区）为基准。
只要有一个工具改用 `os.path.abspath`（进程 CWD）解析相对路径，权限检查与实际
落点就会脱钩——检查的是工作区里的路径（判定"合规"），写下去的却是另一个位置。

Electron 打包后服务端进程的 CWD 通常是用户主目录或安装目录，与用户选的工作区
大概率不同，所以这里显式把进程 CWD 切到另一个目录来复现。
"""

import os
from pathlib import Path

import pytest

from kittymind.tools.builtin._paths import resolve_path
from kittymind.tools.builtin.bash_tool import bash_cwd
from kittymind.tools.builtin.file_edit_tool import FileEditTool, FileEditToolParam
from kittymind.tools.builtin.file_read_tool import FileReadTool, FileReadToolParam
from kittymind.tools.builtin.file_write_tool import FileWriteTool, FileWriteToolParam
from kittymind.tools.builtin.glob_tool import GlobTool, GlobToolParam
from kittymind.tools.builtin.grep_tool import GrepTool, GrepToolParam
from kittymind.tools.builtin.ls_tool import LsTool, LsToolParam


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """工作区 = tmp_path/workspace，进程 CWD = tmp_path/elsewhere（两者刻意不同）。"""
    work = tmp_path / "workspace"
    work.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    token = bash_cwd.set(str(work))
    monkeypatch.chdir(elsewhere)
    yield work
    bash_cwd.reset(token)


def test_resolve_path_uses_workspace_not_process_cwd(ws):
    assert resolve_path("src/a.txt") == str(ws / "src" / "a.txt")
    assert resolve_path("./src/../src/a.txt") == str(ws / "src" / "a.txt")
    assert resolve_path("") == str(ws)
    assert resolve_path(str(ws / "x.txt")) == str(ws / "x.txt")


def test_file_tools_agree_on_relative_path(ws):
    """写 → 读 → 编辑 必须落在同一个绝对路径上（三者基准必须一致）。"""
    write = FileWriteTool().execute(
        FileWriteToolParam(path="notes/a.txt", content="hello\nworld\n")
    )
    assert write.ok, write.content

    target = Path(ws) / "notes" / "a.txt"
    assert target.is_file(), "file_write 未落在工作区内"
    assert not (Path(os.getcwd()) / "notes" / "a.txt").exists(), "写到了进程 CWD"

    read = FileReadTool().execute(FileReadToolParam(path="notes/a.txt"))
    assert read.ok, read.content
    assert "hello" in read.content

    edit = FileEditTool().execute(
        FileEditToolParam(path="notes/a.txt", old_string="hello", new_string="HELLO")
    )
    assert edit.ok, edit.content
    assert "HELLO" in target.read_text(encoding="utf-8")


def test_glob_does_not_escape_root(ws):
    """pattern 里的前导分隔符 / ../ 不得把搜索范围带出 root。"""
    (Path(ws) / "inside.txt").write_text("x", encoding="utf-8")
    (Path(ws).parent / "secret.txt").write_text("secret", encoding="utf-8")

    tool = GlobTool()
    for pattern in ("../*.txt", str(Path(ws).parent / "*.txt")):
        result = tool.execute(GlobToolParam(pattern=pattern, path="."))
        assert result.ok
        assert "secret.txt" not in result.content, f"pattern={pattern!r} 逃出了工作区"


def test_glob_still_finds_files_inside(ws):
    (Path(ws) / "sub").mkdir()
    (Path(ws) / "sub" / "a.py").write_text("x", encoding="utf-8")
    result = GlobTool().execute(GlobToolParam(pattern="**/*.py", path="."))
    assert result.ok and "sub/a.py" in result.content


def test_ls_and_grep_resolve_relative_to_workspace(ws):
    (Path(ws) / "sub").mkdir()
    (Path(ws) / "sub" / "f.txt").write_text("needle\n", encoding="utf-8")

    ls = LsTool().execute(LsToolParam(path="."))
    assert ls.ok and "sub/" in ls.content

    grep = GrepTool().execute(GrepToolParam(pattern="needle", path="sub"))
    assert grep.ok, grep.content
    assert "needle" in grep.content and "f.txt" in grep.content
