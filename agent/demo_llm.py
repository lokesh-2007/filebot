"""Model-free stand-in for ``LlamaServer`` used by demo mode.

It speaks the same ``chat_stream`` interface, so the real ``BotRuntime`` loop, intent
routing, tool allowlists and prompt preparation all run unchanged. It does not
generate language: it quotes the local search hits that ``BotRuntime`` already put
in the prompt, and proposes simple commands for CMD requests.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Iterator

_MARKER = "Local search results (cite paths; do not invent file contents):"
_COMMANDS = (
    (re.compile(r"\bip\b|ipconfig|\bnetwork", re.I), "ipconfig", "Show this PC's network configuration"),
    (re.compile(r"whoami|who am i|user name", re.I), "whoami", "Show the current user name"),
    (re.compile(r"\bwindows version\b|\bver\b", re.I), "ver", "Show the Windows version"),
    (re.compile(r"\blist\b|\bdir\b|\bfiles?\b|\bfolders?\b", re.I), "dir", "List files in the working folder"),
)


def _chunk(text: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": text}}]}


def _hits_from(prompt: str) -> list[dict[str, Any]]:
    if _MARKER not in prompt:
        return []
    try:
        data = json.loads(prompt.split(_MARKER, 1)[1].strip())
    except ValueError:
        return []
    return [h for h in data if isinstance(h, dict)] if isinstance(data, list) else []


_STOP = {"the", "a", "an", "is", "my", "of", "to", "in", "on", "and", "for", "what", "when", "where",
         "how", "do", "does", "me", "find", "search", "about", "with", "are", "i", "it", "this", "that"}


def _best_sentence(snippet: str, question: str, rarity: dict[str, float] | None = None) -> str:
    """Pick the sentence/line of a snippet sharing the most words with the question."""
    words = {w for w in re.findall(r"\w+", question.lower()) if w not in _STOP and len(w) > 1}
    parts = [p.strip(" -•#") for p in re.split(r"(?<=[.!?])\s+|\n+", snippet) if p.strip(" -•#")]
    if not parts:
        return " ".join(snippet.split())[:160]
    weight = rarity or {}
    best = max(parts, key=lambda part: sum(weight.get(w, 1.0) for w in words & set(re.findall(r"\w+", part.lower()))))
    return best[:200]


def compose_answer(prompt: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ("I couldn't find anything about that in your indexed files. Try different keywords, "
                "or wait for indexing to finish.")
    words = {w for w in re.findall(r"\w+", prompt.lower()) if w not in _STOP and len(w) > 1}
    texts = [str(h.get("snippet", "")).lower() for h in hits]
    rarity = {w: 1.0 / max(1, sum(w in t for t in texts)) for w in words}
    lines = [f"I found {len(hits)} relevant file{'s' if len(hits) != 1 else ''}. Best matches:"]
    for hit in hits[:3]:
        name = os.path.basename(str(hit.get("path", "")).replace("\\", "/"))
        snippet = _best_sentence(str(hit.get("snippet", "")), prompt, rarity)
        lines.append(f"• {name} — {snippet}")
    lines.append(f"Open the top result from the list, or refine your question. (Source: {hits[0].get('path', '')})")
    return "\n".join(lines)


class DemoServer:
    """Same surface as ``LlamaServer`` (start/stop/running/chat_stream), no model needed."""

    def __init__(self, token_delay: float = 0.012) -> None:
        self.token_delay = token_delay

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def running(self) -> bool:
        return True

    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                    cancel: threading.Event | None = None, timeout: float = 300.0) -> Iterator[dict[str, Any]]:
        names = {t["function"]["name"] for t in (tools or [])}
        already_called = any(m.get("role") == "tool" for m in messages)
        user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
        if "propose_cmd" in names and not already_called:
            command, reason = "dir", "List files in the working folder"
            request = user.rsplit("User request:", 1)[-1]
            for pattern, cmd, why in _COMMANDS:
                if pattern.search(request):
                    command, reason = cmd, why
                    break
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "demo_call_1",
                "function": {"name": "propose_cmd",
                             "arguments": json.dumps({"command": command, "reason": reason})}}]}}]}
            return
        if already_called:
            return  # command staged; the runtime shows its own confirmation text
        question = user.split("\n\nLocal search results", 1)[0]
        answer = compose_answer(question, _hits_from(user))
        for word in re.findall(r"\S+\s*", answer):
            if cancel is not None and cancel.is_set():
                return
            time.sleep(self.token_delay)
            yield _chunk(word)
