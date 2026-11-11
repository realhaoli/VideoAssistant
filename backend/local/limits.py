"""Conservative request budgets; provider tokenizers and image costs can differ."""
import os

from .model_client import ModelLimitError


def budget(config, visual=False):
    prefix = "vlm" if visual else "llm"
    context = int(config.get(prefix + "_context_tokens") or os.getenv(prefix.upper() + "_CONTEXT_TOKENS", "32768"))
    output = int(config.get(prefix + "_max_output_tokens") or os.getenv(prefix.upper() + "_MAX_OUTPUT_TOKENS", "8192"))
    return max(2048, context), max(256, min(output, context // 2))


def request_budget(config, prompt, system, requested, visual_count=0, *, fit_available=False):
    context, output = budget(config, bool(visual_count))
    maximum = min(requested, output)
    # Byte count is deliberately conservative for text BPEs. Reserve room for
    # message framing and vision tokens; the provider's response is authoritative.
    estimate = len((prompt + system).encode("utf-8")) + 512 + visual_count * 2048
    if fit_available and context - estimate >= 256:
        return min(maximum, context - estimate)
    if estimate + maximum > context:
        raise ModelLimitError("context")
    return maximum
