"""token 自动生成(bootstrap)测试 — 对齐原版 OpenClaw onboarding 的钥匙生成。"""
from app.config import _bootstrap_gateway_token


def test_appends_line_when_env_has_no_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-xxx\n", encoding="utf-8")
    token = _bootstrap_gateway_token(env)
    text = env.read_text(encoding="utf-8")
    assert f"GATEWAY_TOKEN={token}" in text
    assert text.count("GATEWAY_TOKEN=") == 1
    assert len(token) == 64 and all(c in "0123456789abcdef" for c in token)


def test_replaces_existing_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GATEWAY_TOKEN=old-key\nOTHER=x\n", encoding="utf-8")
    token = _bootstrap_gateway_token(env)
    text = env.read_text(encoding="utf-8")
    assert f"GATEWAY_TOKEN={token}" in text
    assert "old-key" not in text
    assert "OTHER=x" in text
    assert text.count("GATEWAY_TOKEN=") == 1


def test_creates_env_file_when_missing(tmp_path):
    env = tmp_path / ".env"
    token = _bootstrap_gateway_token(env)
    assert env.read_text(encoding="utf-8").strip() == f"GATEWAY_TOKEN={token}"
