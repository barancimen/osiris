"""The LLM summary names each setting the way the page does, so DeepSeek can find it in the manual."""

import re

from osiris import llm_summary


def test_every_setting_label_is_the_label_of_its_box_on_the_page():
    page = llm_summary.PAGE.read_text(encoding="utf-8")
    boxes = dict(re.findall(r'check\("(\w+)", "([^"]+)"\)', page))
    for key, label in llm_summary.SETTINGS:
        assert boxes.get(key) == label, key
