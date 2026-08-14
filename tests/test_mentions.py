from canopy import mentions

AGENTS = ["canopy", "arch", "qa"]


def test_mention_is_found_anywhere_in_the_line():
    assert mentions.mentioned_agents("cc @arch 看下这个", AGENTS) == ["arch"]
    assert mentions.mentioned_agents("@canopy fork X", AGENTS) == ["canopy"]


def test_no_mention_returns_empty():
    assert mentions.mentioned_agents("这个查询有点慢", AGENTS) == []


def test_email_like_text_is_not_a_mention():
    assert mentions.mentioned_agents("someone@archive.com", AGENTS) == []


def test_fork_takes_the_title():
    assert mentions.parse("@canopy fork 慢查询定位", "canopy") == ("fork", "慢查询定位")


def test_ack_return_beats_return():
    assert mentions.parse("@canopy ack return", "canopy") == ("ack return", None)
    assert mentions.parse("@canopy return", "canopy") == ("return", None)


def test_guide_accepts_both_colons():
    assert mentions.parse("@canopy guide: 只记 DB 结论", "canopy") == \
        ("guide", "只记 DB 结论")
    assert mentions.parse("@canopy guide：只记 DB 结论", "canopy") == \
        ("guide", "只记 DB 结论")


def test_plain_question_is_not_a_command():
    assert mentions.parse("@canopy 这个能不能今天出结论?", "canopy") == (None, None)


def test_command_for_another_agent_is_not_mine():
    assert mentions.parse("@arch fork X", "canopy") == (None, None)


def test_done_with_a_reason():
    assert mentions.parse("@canopy done 索引已上线", "canopy") == \
        ("done", "索引已上线")
