"""Explicit encoder and token-budgeted text chunks; HTML spans remain unchanged."""
from dataclasses import dataclass
import hashlib
from typing import Any

from chromadb.api.types import EmbeddingFunction

from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
from tokenizers import Tokenizer

from .extraction import Fragment


@dataclass(frozen=True)
class Chunk:
    text: str
    body: str
    token_count: int


class MiniLM:
    name = "all-MiniLM-L6-v2"
    max_tokens = 256

    def __init__(self):
        function = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
        # Chroma's collection methods use a multimodal generic; this project sends
        # only text. Keep the concrete encoder locally for its tokenizer property.
        self.function: EmbeddingFunction[Any] = function
        # Public encoder call ensures cached files exist before loading the tokenizer.
        self.function(["Initialize the embedding model."])
        self.tokenizer = Tokenizer.from_str(function.tokenizer.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.tokenizer_sha256 = hashlib.sha256(self.tokenizer.to_str().encode()).hexdigest()

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text).ids)  # includes special tokens


def fit_prefix(text: str, count, budget: int) -> int:
    """Return a nonempty fitting character span; final encoding is always checked."""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    if not low:
        raise ValueError("Token budget cannot hold even one character")
    return low


def chunk_fragment(fragment: Fragment, encoder: MiniLM, max_tokens: int = 256) -> list[Chunk]:
    if not 16 <= max_tokens <= encoder.max_tokens:
        raise ValueError(f"max_tokens must be between 16 and {encoder.max_tokens}")
    context = " > ".join((fragment.page.title,) + fragment.headings)
    # Bound context so even very long titles leave space for content.
    context_budget = min(64, max_tokens // 3)
    if encoder.count(context) > context_budget:
        context = context[:fit_prefix(context, encoder.count, context_budget)]
    prefix = context + "\n\n"
    blocks = list(fragment.text_blocks)
    if not blocks:
        # Heading-only and media-only fragments remain addressable and searchable by title.
        return [Chunk(context, "", encoder.count(context))] if context.strip() else []
    chunks = []
    current = ""

    def emit(body):
        text = prefix + body
        tokens = encoder.count(text)
        if tokens > max_tokens:
            raise ValueError("Chunk exceeds model token budget")
        chunks.append(Chunk(text, body, tokens))

    for block in blocks:
        candidate = current + ("\n" if current else "") + block
        if encoder.count(prefix + candidate) <= max_tokens:
            current = candidate
            continue
        if current:
            emit(current)
            current = ""
        while encoder.count(prefix + block) > max_tokens:
            end = fit_prefix(block, lambda s: encoder.count(prefix + s), max_tokens)
            # Prefer a word boundary unless it would create a very small chunk.
            space = block.rfind(" ", 0, end)
            if space > end // 2:
                end = space + 1
            emit(block[:end])
            block = block[end:]
        current = block
    if current:
        emit(current)
    return chunks
