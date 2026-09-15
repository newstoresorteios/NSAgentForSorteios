from app.llm.response_composer import truncate_reply


def test_long_signed_url_is_not_emitted_as_a_broken_prefix():
    url = "https://store.example/checkout?token=" + "a"*700
    reply = truncate_reply("Finalize pelo site: " + url, 120)
    assert "https://" not in reply or url in reply
    assert truncate_reply(url, 120) == url
    assert truncate_reply("anything", 0) == ""
