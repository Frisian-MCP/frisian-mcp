"""Opt-in token-usage reporting for the dispatcher ``_usage`` envelope.

This subpackage implements the additive, opt-in ``_usage`` block described in
the TUR-1 design spec.  It is intentionally a plain helper package -- no Django
models, no migrations, no app config -- so it never collides with the
``frisian_mcp.contrib.tokens`` auth-token app and adds no runtime cost when the
feature is disabled (the default).

Public surface:

* :func:`count_tokens` / :func:`encoding_name` / :func:`count_value` /
  :func:`dumps_for_count` -- the pinned-``cl100k_base`` token counter with a
  clean character-based fallback (TUR-2).
* :func:`resolve_usage_reporting` and its layer helpers -- the three-layer
  opt-in resolver with system-``deny`` authoritative (TUR-3).
* :func:`maybe_attach_usage` / :func:`build_usage_block` -- attach the additive
  ``_usage`` sibling to a success result, opt-in gated (TUR-4).
"""

from __future__ import annotations

from frisian_mcp.usage.counter import (
    FALLBACK_ENCODING,
    TOKENIZER_ENCODING,
    count_tokens,
    count_value,
    dumps_for_count,
    encoding_name,
    tiktoken_available,
)
from frisian_mcp.usage.envelope import build_usage_block, maybe_attach_usage
from frisian_mcp.usage.resolver import (
    POLICY_ALLOW,
    POLICY_DENY,
    USAGE_CONTENT_HEADER,
    USAGE_CONTENT_HEADER_META,
    USAGE_CONTENT_QUERY_PARAM,
    USAGE_HEADER,
    USAGE_HEADER_META,
    USAGE_IN_CONTENT_SETTING,
    USAGE_POLICY_SETTING,
    USAGE_QUERY_PARAM,
    USAGE_REPORTING_SETTING,
    parse_content_request_flag,
    parse_flag_value,
    parse_request_flag,
    resolve_system_policy,
    resolve_usage_in_content,
    resolve_usage_reporting,
)

__all__ = [
    "FALLBACK_ENCODING",
    "TOKENIZER_ENCODING",
    "count_tokens",
    "count_value",
    "dumps_for_count",
    "encoding_name",
    "tiktoken_available",
    # resolver (TUR-3)
    "POLICY_ALLOW",
    "POLICY_DENY",
    "USAGE_HEADER",
    "USAGE_HEADER_META",
    "USAGE_POLICY_SETTING",
    "USAGE_QUERY_PARAM",
    "USAGE_REPORTING_SETTING",
    "parse_flag_value",
    "parse_request_flag",
    "resolve_system_policy",
    "resolve_usage_reporting",
    # content-visible line (TUR-12)
    "USAGE_IN_CONTENT_SETTING",
    "USAGE_CONTENT_HEADER",
    "USAGE_CONTENT_HEADER_META",
    "USAGE_CONTENT_QUERY_PARAM",
    "parse_content_request_flag",
    "resolve_usage_in_content",
    # envelope (TUR-4)
    "build_usage_block",
    "maybe_attach_usage",
]
