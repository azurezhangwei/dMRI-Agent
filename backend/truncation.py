from __future__ import annotations

from dataclasses import dataclass

APPROX_BYTES_PER_TOKEN = 4


@dataclass(frozen=True)
class TruncationPolicy:
    mode: str  # "bytes" or "tokens"
    limit: int

    def byte_budget(self) -> int:
        if self.mode == "tokens":
            return approx_bytes_for_tokens(self.limit)
        return self.limit

    def token_budget(self) -> int:
        if self.mode == "bytes":
            return approx_tokens_from_byte_count(self.limit)
        return self.limit


def approx_token_count(text: str) -> int:
    length = len(text.encode("utf-8"))
    return (length + (APPROX_BYTES_PER_TOKEN - 1)) // APPROX_BYTES_PER_TOKEN


def approx_bytes_for_tokens(tokens: int) -> int:
    return max(0, tokens) * APPROX_BYTES_PER_TOKEN


def approx_tokens_from_byte_count(byte_count: int) -> int:
    return (max(0, byte_count) + (APPROX_BYTES_PER_TOKEN - 1)) // APPROX_BYTES_PER_TOKEN


def formatted_truncate_text(content: str, policy: TruncationPolicy) -> str:
    if len(content.encode("utf-8")) <= policy.byte_budget():
        return content
    total_lines = content.count("\n") + (1 if content else 0)
    result = truncate_text(content, policy)
    return f"Total output lines: {total_lines}\n\n{result}"


def truncate_text(content: str, policy: TruncationPolicy) -> str:
    if policy.mode == "tokens":
        truncated, _ = truncate_with_token_budget(content, policy)
        return truncated
    return truncate_with_byte_estimate(content, policy)


def truncate_with_token_budget(content: str, policy: TruncationPolicy) -> tuple[str, int | None]:
    if not content:
        return "", None
    max_tokens = policy.token_budget()
    byte_len = len(content.encode("utf-8"))
    if max_tokens > 0 and byte_len <= approx_bytes_for_tokens(max_tokens):
        return content, None
    truncated = truncate_with_byte_estimate(content, policy)
    approx_total = approx_token_count(content)
    if truncated == content:
        return truncated, None
    return truncated, approx_total


def truncate_with_byte_estimate(content: str, policy: TruncationPolicy) -> str:
    if not content:
        return ""
    max_bytes = policy.byte_budget()
    total_bytes = len(content.encode("utf-8"))
    if max_bytes == 0:
        marker = format_truncation_marker(policy, removed_units_for_source(policy, total_bytes, 0))
        return marker
    if total_bytes <= max_bytes:
        return content
    left_budget, right_budget = split_budget(max_bytes)
    removed_chars, left, right = split_string(content, left_budget, right_budget)
    removed_units = removed_units_for_source(
        policy, max(0, total_bytes - max_bytes), removed_chars
    )
    marker = format_truncation_marker(policy, removed_units)
    return assemble_truncated_output(left, right, marker)


def split_budget(budget: int) -> tuple[int, int]:
    left = budget // 2
    return left, budget - left


def split_string(content: str, beginning_bytes: int, end_bytes: int) -> tuple[int, str, str]:
    if not content:
        return 0, "", ""
    total_bytes = len(content.encode("utf-8"))
    tail_start_target = max(0, total_bytes - end_bytes)
    prefix_end = 0
    suffix_start = len(content)
    removed_chars = 0
    suffix_started = False
    byte_offset = 0
    for idx, ch in enumerate(content):
        ch_bytes = len(ch.encode("utf-8"))
        char_end = byte_offset + ch_bytes
        if char_end <= beginning_bytes:
            prefix_end = idx + 1
        elif byte_offset >= tail_start_target:
            if not suffix_started:
                suffix_start = idx
                suffix_started = True
        else:
            removed_chars += 1
        byte_offset = char_end
    if suffix_start < prefix_end:
        suffix_start = prefix_end
    return removed_chars, content[:prefix_end], content[suffix_start:]


def removed_units_for_source(
    policy: TruncationPolicy, removed_bytes: int, removed_chars: int
) -> int:
    if policy.mode == "tokens":
        return approx_tokens_from_byte_count(removed_bytes)
    return max(0, removed_chars)


def format_truncation_marker(policy: TruncationPolicy, removed_count: int) -> str:
    if policy.mode == "tokens":
        return f"…{removed_count} tokens truncated…"
    return f"…{removed_count} chars truncated…"


def assemble_truncated_output(prefix: str, suffix: str, marker: str) -> str:
    return f"{prefix}{marker}{suffix}"
