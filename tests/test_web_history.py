"""Tests for Web history helpers."""

from __future__ import annotations

import json
import threading

from web import history


def test_incomplete_task_round_trip(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 3)

    history.record_incomplete_task(
        "600370",
        "2026-06-02",
        status="error",
        error="quota exceeded",
        completed_stages=["market", "news"],
    )

    entries = history.get_incomplete_history()

    assert entries == [
        {
            "ticker": "600370",
            "trade_date": "2026-06-02",
            "status": "error",
            "error": "quota exceeded",
            "completed_stages": ["market", "news"],
            "updated_at": entries[0]["updated_at"],
            "checkpoint_step": 3,
        }
    ]


def test_extract_stage_ratings():
    state = {
        "investment_plan": "评级：Overweight（超配）",
        "trader_investment_decision": "评级：Hold（持有）",
        "final_trade_decision": "评级：Hold（持有）",
    }
    ratings = history.extract_stage_ratings(state)
    assert ratings == {
        "research": "Overweight",
        "trader": "Hold",
        "portfolio": "Hold",
    }
    assert history.extract_signal(state) == "Hold"
    assert ratings["portfolio"] == history.extract_signal(state)


def test_extract_stage_ratings_research_recommendation_header():
    state = {
        "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: Bull case wins.",
        "final_trade_decision": "**Rating**: Hold\n\n**Executive Summary**: Wait.",
    }
    ratings = history.extract_stage_ratings(state)
    assert ratings["research"] == "Overweight"
    assert ratings["portfolio"] == "Hold"


def test_trader_rating_ignores_research_manager_citation():
    state = {
        "trader_investment_decision": (
            "**研究经理评级：** Overweight（超配）\n"
            "| **操作** | **Buy（买入）** |\n"
        ),
        "final_trade_decision": "<!-- TRADINGAGENTS_RATING: Hold -->\n**评级：Hold（持有）**",
        "portfolio_rating": "Hold",
    }
    ratings = history.extract_stage_ratings(state)
    assert ratings["trader"] == "Buy"
    assert ratings["portfolio"] == "Hold"
    assert history.extract_signal(state) == "Hold"


def test_517400_long_preamble_rating_section():
    """Regression: PM rating declared after a long debate preamble (line 40+)."""
    state = {
        "final_trade_decision": (
            "研究经理的框架如下。\n" * 35
            + "## 三、最终评级与核心依据\n\n"
            + "# **评级：Overweight（超配）**\n\n"
            "后续若跌破止损，评级下调至 Underweight。"
        ),
    }
    assert history.extract_signal(state) == "Overweight"
    assert history.extract_stage_ratings(state)["portfolio"] == "Overweight"


def test_159570_july3_portfolio_matches_top():
    """Regression: 159570 PM Hold must match top even when body says Buy."""
    state = {
        "research_rating": "Overweight",
        "portfolio_rating": "Hold",
        "investment_plan": "<!-- TRADINGAGENTS_RATING: Overweight -->\n**Recommendation**: Overweight",
        "trader_investment_decision": (
            "**研究经理评级：** Overweight（超配）\n"
            "| **操作** | **Buy（买入）** |\n"
        ),
        "final_trade_decision": (
            "<!-- TRADINGAGENTS_RATING: Hold -->\n\n"
            "**评级：Hold（持有）**\n"
            "### 交易指令\n**Buy（买入）** 159570"
        ),
    }
    signal = history.extract_signal(dict(state))
    ratings = history.extract_stage_ratings(dict(state))
    assert signal == "Hold"
    assert ratings["portfolio"] == "Hold"
    assert ratings["research"] == "Overweight"
    assert ratings["trader"] == "Buy"
    assert history.extract_pm_immediate_action(state["final_trade_decision"]) == "Buy"


def test_resolve_report_signal_falls_back_when_extract_returns_na():
    state = {
        "final_trade_decision": "",
        "portfolio_rating": "",
    }
    assert history.extract_signal(dict(state)) == "N/A"
    assert history.resolve_report_signal(dict(state), "Overweight") == "Overweight"


def test_extract_signal_falls_back_to_risk_judge_decision():
    state = {
        "portfolio_rating": "",
        "final_trade_decision": "",
        "risk_debate_state": {
            "judge_decision": "**最终投资评级：OVERWEIGHT（超配）**\n\n建立观察仓。",
        },
    }
    assert history.extract_signal(dict(state)) == "Overweight"


def test_extract_signal_prefers_portfolio_manager():
    state = {
        "investment_plan": "评级：Overweight（超配）",
        "final_trade_decision": "评级：Hold（持有）",
    }
    assert history.extract_signal(state) == "Hold"


def test_load_analysis_persists_canonicalized_ratings(tmp_path, monkeypatch):
    from web import history

    log_dir = tmp_path / "logs" / "517400" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True)
    path = log_dir / "full_states_log_2026-07-03.json"
    payload = {
        "final_trade_decision": (
            "**评级：** **Overweight（超配）**\n"
            "一经触发，**立即全部清仓（Sell All）**。"
        ),
        "investment_plan": "**评级：** **Overweight（超配）**",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    state = history.load_analysis(str(path))
    assert state["portfolio_rating"] == "Overweight"
    assert state["research_rating"] == "Overweight"
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["portfolio_rating"] == "Overweight"
    assert "<!-- TRADINGAGENTS_RATING: Overweight -->" in reloaded["final_trade_decision"]


def test_completed_history_hides_incomplete_task(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    log_dir = logs / "600370" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True)
    (log_dir / "full_states_log_2026-06-02.json").write_text(
        json.dumps({"final_trade_decision": "HOLD"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 3)

    history.record_incomplete_task("600370", "2026-06-02", status="running")

    assert history.get_incomplete_history() == []


def test_incomplete_task_writes_are_thread_safe(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 1)

    def write_task(i: int) -> None:
        history.record_incomplete_task(
            f"60037{i % 10}",
            "2026-06-02",
            status="running",
            completed_stages=["market"],
        )

    threads = [threading.Thread(target=write_task, args=(i,)) for i in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = history.get_incomplete_history()

    assert len(entries) == 10
    assert {entry["status"] for entry in entries} == {"running"}
    assert not list(tmp_path.glob("*.tmp"))
