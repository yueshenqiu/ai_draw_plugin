# -*- coding: utf-8 -*-
"""提示词规则模板"""

from .prompt_rules import (
    GENERATION_POLICY_TEXTS,
    build_prompt_generator_template,
    PROMPT_GENERATOR_TEMPLATE,
    PROMPT_GENERATOR_JSON_TEMPLATE,
    SFW_PROMPT_GENERATOR_TEMPLATE,
    SFW_PROMPT_GENERATOR_JSON_TEMPLATE,
)

__all__ = [
    "GENERATION_POLICY_TEXTS",
    "build_prompt_generator_template",
    "PROMPT_GENERATOR_TEMPLATE",
    "PROMPT_GENERATOR_JSON_TEMPLATE",
    "SFW_PROMPT_GENERATOR_TEMPLATE",
    "SFW_PROMPT_GENERATOR_JSON_TEMPLATE",
]
