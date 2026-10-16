"""渲染单测 —— 纯确定性代码，所以能被断言到底。

★ 为什么这块必须由代码渲染、且值得单测（PLAN §M0 验收 B/C）：
  表格由【构造】保证三个字段（title/company/url）都在，而不是靠 Prompt
  祈求模型守规矩。而 Markdown 有个坏脾气：一个 `|` 或一个换行就能把表拆坏，
  它【不报错】，只一声不响地渲染成别的样子。所以每一条破坏都要有断言盯着。
★ 顺带钉住 M7 用来替掉"摘要提到岗位数"的那条【确定性】指标：
  「渲染出的报告表行数 == 输入条数」—— 它不靠模型，只靠渲染器。
"""

from __future__ import annotations

from _fakes import data_rows, job

from workflows.report import _render_table, render_report


def test_table_row_carries_three_fields():
    table, missing = _render_table([job("1", title="后端工程师", company="甲公司")])
    assert "后端工程师" in table
    assert "甲公司" in table
    assert "https://example.test/1" in table
    assert missing == 0


def test_every_job_occupies_one_row():
    """★ M7 的确定性指标：表行数 == 输入条数（含那条没链接的）。"""
    jobs = [job(str(i)) for i in range(1, 6)] + [job("9", url="")]
    doc = render_report(jobs, day="2026-10-09", summary="摘要", run_id="r", generated_at="t")
    assert len(data_rows(doc)) == len(jobs) == 6


def test_empty_url_writes_dash_not_blank():
    table, missing = _render_table([job("1", url="")])
    assert "| — |" in table
    assert missing == 1


def test_missing_link_footnote_only_when_needed():
    with_missing = render_report(
        [job("1", url="")], day="d", summary="s", run_id="r", generated_at="t"
    )
    assert "其中 1 条没有直接链接" in with_missing
    all_linked = render_report([job("1")], day="d", summary="s", run_id="r", generated_at="t")
    assert "没有直接链接" not in all_linked  # 0 条时写脚注是纯噪音


def test_pipe_escaped():
    table, _ = _render_table([job("1", title="带|竖线的岗位")])
    assert "\\|" in table


def test_newline_flattened_to_one_row():
    """`\\r\\n` 必须先压 —— 反过来会把一个折行换成两个空格。"""
    table, _ = _render_table([job("1", title="带\r\n换行的岗位")])
    assert len(table.splitlines()) == 3  # 表头 + 分隔 + 1 行
    assert "带 换行的岗位" in table


def test_table_has_no_trailing_newline():
    table, _ = _render_table([job("1")])
    assert not table.endswith("\n")  # 模板里 {table} 后面已经留了一个


def test_empty_summary_becomes_placeholder():
    doc = render_report([job("1")], day="d", summary="   ", run_id="r", generated_at="t")
    assert "_（本次没有生成摘要）_" in doc
