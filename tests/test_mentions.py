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


def test_untrack_with_a_reason():
    """No `done`: a done state needs a reopen, and then a rule for what done
    means while a child still runs. Watching is the only thing you toggle."""
    assert mentions.parse("@canopy untrack 索引已上线", "canopy") == \
        ("untrack", "索引已上线")


def test_track_reopens_a_parked_node():
    assert mentions.parse("@canopy track", "canopy") == ("track", None)


# -- the summarizer's two-part answer -----------------------------------------

def test_parse_summary_splits_the_two_lines():
    from canopy import prompts
    got = prompts.parse_summary("CHECKPOINT: 决定加索引\nDIGEST: 在查慢查询,等 DBA")
    assert got["checkpoint"] == "决定加索引"
    assert got["digest"] == "在查慢查询,等 DBA"


def test_parse_summary_keeps_the_digest_when_the_checkpoint_is_skipped():
    from canopy import prompts
    got = prompts.parse_summary("CHECKPOINT: SKIP\nDIGEST: 还在吵,没有结论")
    assert got["checkpoint"] is None
    assert got["digest"] == "还在吵,没有结论"


def test_parse_summary_falls_back_to_a_bare_line():
    """A model answering in the old one-line shape must still cost a digest,
    never a checkpoint."""
    from canopy import prompts
    got = prompts.parse_summary("决定先加复合索引")
    assert got["checkpoint"] == "决定先加复合索引" and got["digest"] is None


def test_parse_summary_survives_a_code_fence():
    from canopy import prompts
    got = prompts.parse_summary("```\nCHECKPOINT: SKIP\nDIGEST: 现状一句话\n```")
    assert got["digest"] == "现状一句话"


def test_parse_summary_of_nothing():
    from canopy import prompts
    assert prompts.parse_summary("") == {"checkpoint": None, "digest": None}


def test_shorten_digest_caps_and_marks_the_cut():
    from canopy import prompts
    got = prompts.shorten_digest("啊" * 900)
    assert len(got) <= prompts.DIGEST_MAX + 1 and got.endswith("…")


def test_the_digest_never_carries_a_raw_slack_id():
    """The first real digest copied `<@U018KSR9C14>` straight through. It gets
    injected into another worker's prompt, where an id resolves to nothing."""
    from canopy import prompts
    got = prompts.shorten_digest("结论是 <@U018KSR9C14> 再试试，UD9S8GBRR 去问前端。")
    assert "U018KSR9C14" not in got and "UD9S8GBRR" not in got
    assert "再试试" in got and "去问前端" in got


def test_shorten_digest_keeps_ordinary_words():
    from canopy import prompts
    got = prompts.shorten_digest("UX 和 QA 都同意了")
    assert got == "UX 和 QA 都同意了"


def test_the_history_command_is_absolute_when_the_config_resolved_it():
    """cron's PATH is minimal, and the worker inherits it."""
    from canopy import prompts
    got = prompts.history_hint({"channel": "C1", "thread_ts": "1.0"},
                               slack_cli="/Users/x/.local/bin/slackcli")
    assert got.startswith("/Users/x/.local/bin/slackcli conversations read C1")


def test_the_digest_contract_demands_the_identifiers():
    from canopy import prompts
    assert "identifiers cannot be missing" in prompts.DIGEST_CONTRACT
