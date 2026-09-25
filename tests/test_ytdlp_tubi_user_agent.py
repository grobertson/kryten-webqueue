from kryten_webqueue.integrations.ytpipe import downloader


def test_wrapper_sets_generic_user_agent_at_construction():
    ydl = downloader._YoutubeDLWithJSRuntimes({"quiet": True})

    assert ydl.params["http_headers"]["User-Agent"] == downloader._TUBI_USER_AGENT


def test_wrapper_preserves_caller_user_agent():
    ydl = downloader._YoutubeDLWithJSRuntimes(
        {"quiet": True, "http_headers": {"User-Agent": "custom-agent"}}
    )

    assert ydl.params["http_headers"]["User-Agent"] == "custom-agent"
