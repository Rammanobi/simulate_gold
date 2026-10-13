from simgold import fallback_llm


def _client(models):
    def req(method, url, headers, body, timeout):
        if url.endswith("/models"):
            return 200, {"data": [{"id": m} for m in models]}
        return 200, {"choices": [{"message": {"content": "OK"}}]}
    return fallback_llm.Client("k", fallback_llm.load_cfg(None), request=req)


def test_unknown_ids_fall_back_to_family_names():
    c = _client(["deepseek-v5-flash", "deepseek-v5-pro"])
    assert c.candidates("fast") == ["deepseek-v5-flash"]
    assert c.candidates("thinking") == ["deepseek-v5-pro"]


def test_configured_ids_win():
    c = _client(["deepseek-chat", "deepseek-flash", "deepseek-reasoner"])
    assert c.candidates("fast")[0] == "deepseek-flash"
