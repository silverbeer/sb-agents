import warnings

from adk_docs_client.agent import CARD_PATH, build_docs_remote, build_root_agent

warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")


def test_root_agent_delegates_to_remote_docs_agent(monkeypatch) -> None:
    monkeypatch.setenv("DOCS_AGENT_A2A_URL", "http://127.0.0.1:9999/")
    root = build_root_agent()
    assert [a.name for a in root.sub_agents] == ["docs_agent"]
    assert "docs_agent" in root.instruction


def test_remote_card_url_built_from_base() -> None:
    remote = build_docs_remote("http://127.0.0.1:8101/")
    assert remote._agent_card_source == "http://127.0.0.1:8101" + CARD_PATH
