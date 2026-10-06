"""AI job-match scoring and application Q&A for daily board cards.

Provider-switchable: the active provider (Anthropic Claude or OpenAI) is chosen in
Settings. Anthropic uses the official ``anthropic`` SDK; OpenAI uses the Chat
Completions REST API over stdlib HTTP (no extra dependency). The candidate's
selected resume is compared against the card's job description to produce a match
score, draft answers, and cover letters.

Provider SDK/HTTP imports are deferred so the app still launches if a package is
missing; calls then raise a clear, user-facing error.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QSettings, pyqtSignal

import app_variant

AI_PROVIDER_SETTINGS_KEY = "ai_provider"
ANTHROPIC_API_KEY_SETTINGS_KEY = "anthropic_api_key"
OPENAI_API_KEY_SETTINGS_KEY = "openai_api_key"

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"  # answers, cover letters (quality-first)
MATCH_ANTHROPIC_MODEL = "claude-sonnet-5"    # match scoring: near-Opus quality, faster
FAST_ANTHROPIC_MODEL = "claude-haiku-4-5"    # factual job Q&A + posting parsing (short)
DEFAULT_OPENAI_MODEL = "gpt-4o"

# User-selectable primary model per provider (Settings > App settings). The combo
# boxes are editable, so these are presets, not a hard whitelist.
ANTHROPIC_MODEL_SETTINGS_KEY = "anthropic_model"
OPENAI_MODEL_SETTINGS_KEY = "openai_model"
ANTHROPIC_MODELS = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-fable-5",
)
OPENAI_MODELS = (
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4-turbo",
)

_SETTINGS_ORG = app_variant.SETTINGS_ORG
_SETTINGS_APP = app_variant.SETTINGS_APP
_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# Keep prompts well under the context window; trim very long inputs.
_MAX_RESUME_CHARS = 24000
_MAX_JOB_CHARS = 24000
_MAX_QUESTION_CHARS = 4000


# --- provider / credentials -------------------------------------------------


def _settings() -> QSettings:
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def ai_provider() -> str:
    """The active AI provider ('anthropic' or 'openai'), default Anthropic."""
    val = _settings().value(AI_PROVIDER_SETTINGS_KEY, PROVIDER_ANTHROPIC)
    val = val.strip().lower() if isinstance(val, str) else PROVIDER_ANTHROPIC
    return PROVIDER_OPENAI if val == PROVIDER_OPENAI else PROVIDER_ANTHROPIC


def provider_label(provider: Optional[str] = None) -> str:
    return "OpenAI" if (provider or ai_provider()) == PROVIDER_OPENAI else "Anthropic (Claude)"


def _anthropic_api_key() -> str:
    val = _settings().value(ANTHROPIC_API_KEY_SETTINGS_KEY, "")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _openai_api_key() -> str:
    val = _settings().value(OPENAI_API_KEY_SETTINGS_KEY, "")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return os.environ.get("OPENAI_API_KEY", "").strip()


def api_key(provider: Optional[str] = None) -> str:
    """The API key for the given (or active) provider."""
    p = provider or ai_provider()
    return _openai_api_key() if p == PROVIDER_OPENAI else _anthropic_api_key()


def has_api_key(provider: Optional[str] = None) -> bool:
    return bool(api_key(provider))


def default_model(provider: Optional[str] = None) -> str:
    """The built-in primary model for the given (or active) provider."""
    p = provider or ai_provider()
    return DEFAULT_OPENAI_MODEL if p == PROVIDER_OPENAI else DEFAULT_ANTHROPIC_MODEL


def models_for(provider: Optional[str] = None) -> tuple[str, ...]:
    """Preset model ids offered in the settings combo for the given provider."""
    p = provider or ai_provider()
    return OPENAI_MODELS if p == PROVIDER_OPENAI else ANTHROPIC_MODELS


def _model_settings_key(provider: str) -> str:
    return OPENAI_MODEL_SETTINGS_KEY if provider == PROVIDER_OPENAI else ANTHROPIC_MODEL_SETTINGS_KEY


def selected_model(provider: Optional[str] = None) -> str:
    """The user-chosen primary model for the provider, or the built-in default."""
    p = provider or ai_provider()
    val = _settings().value(_model_settings_key(p), "")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return default_model(p)


def set_selected_model(provider: str, model: Optional[str]) -> None:
    """Persist (or clear, when blank/default) the provider's primary model."""
    key = _model_settings_key(provider)
    s = _settings()
    model = (model or "").strip()
    if model and model != default_model(provider):
        s.setValue(key, model)
    else:
        s.remove(key)


def current_model(provider: Optional[str] = None) -> str:
    return selected_model(provider)


# --- performance logging (opt-in) -------------------------------------------
# Off by default. Turn on to measure per-call latency and token/cache usage:
#   - set the env var AI_PERF_LOG=1, or
#   - set the QSettings key "ai_perf_log" to true.
# Each call then logs one line to stderr, e.g.:
#   [AI] anthropic claude-opus-4-8 8421ms  ttft=1503ms  in=95  out=612  cache_read=5312

_perf_log = logging.getLogger("ai_assistant.perf")


def _perf_enabled() -> bool:
    if os.environ.get("AI_PERF_LOG", "").strip():
        return True
    val = _settings().value("ai_perf_log", False)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _emit_perf(
    provider: str,
    model: str,
    elapsed: float,
    ttft: Optional[float] = None,
    input_tokens=None,
    output_tokens=None,
    cache_read=None,
    cache_write=None,
) -> None:
    """Log one timing/usage line for a completed call (no-op unless enabled)."""
    if not _perf_enabled():
        return
    if not _perf_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        _perf_log.addHandler(handler)
        _perf_log.setLevel(logging.INFO)
        _perf_log.propagate = False
    bits = [f"{provider} {model} {elapsed * 1000:.0f}ms"]
    if ttft is not None:
        bits.append(f"ttft={ttft * 1000:.0f}ms")
    if input_tokens is not None:
        bits.append(f"in={input_tokens}")
    if output_tokens is not None:
        bits.append(f"out={output_tokens}")
    if cache_read:
        bits.append(f"cache_read={cache_read}")
    if cache_write:
        bits.append(f"cache_write={cache_write}")
    _perf_log.info("[AI] " + "  ".join(bits))


def _anthropic_usage_fields(usage):
    """(input, output, cache_read, cache_write) from an Anthropic usage object."""
    def g(name):
        return getattr(usage, name, None) if usage is not None else None

    return (
        g("input_tokens"),
        g("output_tokens"),
        g("cache_read_input_tokens"),
        g("cache_creation_input_tokens"),
    )


def _emit_openai_perf(model: str, elapsed: float, ttft: Optional[float], usage) -> None:
    usage = usage or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    _emit_perf(
        "openai", model, elapsed, ttft,
        usage.get("prompt_tokens"), usage.get("completion_tokens"), cached, None,
    )


# --- helpers ----------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...(truncated)..."


# Replace "fancy" typographic characters the model tends to emit with plain
# keyboard-typeable ASCII, so answers can be edited/retyped easily.
_PUNCT_TABLE = str.maketrans(
    {
        "—": "-",   # em dash —
        "–": "-",   # en dash –
        "‒": "-",   # figure dash
        "―": "-",   # horizontal bar
        "−": "-",   # minus sign −
        "‘": "'",   # left single quote ‘
        "’": "'",   # right single quote / apostrophe ’
        "‚": "'",   # single low quote ‚
        "‛": "'",
        "“": '"',   # left double quote “
        "”": '"',   # right double quote ”
        "„": '"',   # double low quote „
        "…": "...",  # ellipsis …
        "•": "-",   # bullet •
        "·": "-",   # middle dot ·
        "→": "->",  # arrow →
        " ": " ",   # non-breaking space
    }
)


def normalize_punctuation(text: str) -> str:
    """Swap em/en dashes, curly quotes, ellipsis, etc. for plain ASCII."""
    return text.translate(_PUNCT_TABLE) if text else text


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF resume (used for OpenAI, which has no PDF input)."""
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt6.QtPdf import QPdfDocument

        qba = QByteArray(pdf_bytes)
        buf = QBuffer(qba)
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        doc = QPdfDocument(None)
        doc.load(buf)
        parts = []
        for i in range(doc.pageCount()):
            sel = doc.getAllText(i)
            t = sel.text() if sel is not None else ""
            if t:
                parts.append(t)
        return "\n\n".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


def _resume_blocks(resume_text: str, resume_pdf: Optional[bytes]) -> list:
    """Anthropic content blocks for the resume.

    Prefer extracted/stored text: it is fewer input tokens, needs no server-side
    PDF parse, and yields a much faster time-to-first-token than re-uploading the
    PDF bytes on every call. Fall back to the native PDF document block only when
    no text can be extracted (e.g. a scanned PDF with no selectable text)."""
    text = (resume_text or "").strip()
    if not text and resume_pdf:
        text = _pdf_to_text(resume_pdf)  # local extraction — cheap vs. re-sending the PDF
    if text:
        return [{"type": "text", "text": f"=== CANDIDATE RESUME ===\n{_clip(text, _MAX_RESUME_CHARS)}"}]
    if resume_pdf:
        data = base64.standard_b64encode(resume_pdf).decode("ascii")
        return [
            {"type": "text", "text": "=== CANDIDATE RESUME (attached PDF) ==="},
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data,
                },
            },
        ]
    return [{"type": "text", "text": "=== CANDIDATE RESUME ===\n"}]


def _resume_user_text(resume_text: str, resume_pdf: Optional[bytes]) -> str:
    """Plain-text resume for OpenAI — extracted from the PDF when present."""
    if resume_pdf:
        extracted = _pdf_to_text(resume_pdf)
        if extracted:
            return _clip(extracted, _MAX_RESUME_CHARS)
    return _clip(resume_text or "", _MAX_RESUME_CHARS)


# --- provider back ends -----------------------------------------------------


def _anthropic_complete(
    system: str,
    content_blocks: list,
    max_tokens: int,
    model: Optional[str] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    import anthropic  # deferred — optional dependency

    client = anthropic.Anthropic(api_key=_anthropic_api_key())
    # Task-specific callers pass an explicit model (match/fast); the primary
    # answer/cover-letter calls pass none and use the user's selected model.
    model_id = model or selected_model(PROVIDER_ANTHROPIC)
    kwargs = dict(
        model=model_id,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content_blocks}],
    )
    t0 = time.perf_counter()
    if on_chunk is None:
        resp = client.messages.create(**kwargs)
        _emit_perf(
            "anthropic", model_id, time.perf_counter() - t0, None,
            *_anthropic_usage_fields(getattr(resp, "usage", None)),
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
    # Stream: surface text as it arrives so long answers appear immediately.
    parts: list[str] = []
    ttft: Optional[float] = None
    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            if ttft is None:
                ttft = time.perf_counter() - t0
            parts.append(text)
            on_chunk(text)
        final = stream.get_final_message()
    _emit_perf(
        "anthropic", model_id, time.perf_counter() - t0, ttft,
        *_anthropic_usage_fields(getattr(final, "usage", None)),
    )
    return "".join(parts).strip()


def _openai_error_message(code: int, body: str) -> str:
    msg = None
    try:
        msg = ((json.loads(body) or {}).get("error") or {}).get("message")
    except (ValueError, TypeError, AttributeError):
        pass
    if code == 401:
        return "Invalid OpenAI API key. Check it in Settings."
    if code == 429:
        return "OpenAI rate limit or quota exceeded. Try again later."
    if code == 404:
        return msg or "OpenAI model not found for this key."
    return msg or f"OpenAI request failed (HTTP {code})."


def _openai_complete(
    system: str,
    user_text: str,
    max_tokens: int,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    import urllib.error
    import urllib.request

    model_id = selected_model(PROVIDER_OPENAI)
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": max_tokens,
    }
    if on_chunk is not None:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    req = urllib.request.Request(
        _OPENAI_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_openai_api_key()}",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            if on_chunk is None:
                data = json.loads(resp.read().decode("utf-8"))
                _emit_openai_perf(
                    model_id, time.perf_counter() - t0, None, data.get("usage")
                )
                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError("OpenAI returned no choices.")
                return (choices[0].get("message", {}).get("content") or "").strip()
            # Streaming: parse the SSE 'data:' lines and emit content deltas.
            parts: list[str] = []
            ttft: Optional[float] = None
            usage = None
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]  # final chunk (stream_options.include_usage)
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    parts.append(delta)
                    on_chunk(delta)
            _emit_openai_perf(model_id, time.perf_counter() - t0, ttft, usage)
            return "".join(parts).strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(_openai_error_message(e.code, e.read().decode("utf-8", "ignore")))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach OpenAI: {getattr(e, 'reason', e)}")


def _complete(
    system: str,
    max_tokens: int,
    volatile_tail: str,
    *,
    cached_tail: Optional[str] = None,
    resume_text: Optional[str] = None,
    resume_pdf: Optional[bytes] = None,
    model: Optional[str] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    """Dispatch one completion to the active provider.

    ``cached_tail`` is stable per card (the job description); together with the
    resume it forms the cacheable prefix, so repeat actions on the same card reuse
    it instead of reprocessing it. ``volatile_tail`` is the per-request
    instruction/question that follows. ``on_chunk`` streams text as it arrives."""
    has_resume = resume_text is not None or resume_pdf is not None
    if ai_provider() == PROVIDER_OPENAI:
        parts: list[str] = []
        if has_resume:
            parts.append(
                f"=== CANDIDATE RESUME ===\n{_resume_user_text(resume_text or '', resume_pdf)}"
            )
        if cached_tail:
            parts.append(cached_tail)
        parts.append(volatile_tail)
        return _openai_complete(system, "\n\n".join(parts), max_tokens, on_chunk=on_chunk)
    content = _resume_blocks(resume_text or "", resume_pdf) if has_resume else []
    if cached_tail:
        content.append({"type": "text", "text": cached_tail})
    if content:
        # Cache the stable prefix (resume + job description). Repeat actions on the
        # same card then read it (~0.1x cost, faster TTFT) instead of reprocessing.
        stable = dict(content[-1])
        stable["cache_control"] = {"type": "ephemeral"}
        content[-1] = stable
    content.append({"type": "text", "text": volatile_tail})
    return _anthropic_complete(system, content, max_tokens, model=model, on_chunk=on_chunk)


# --- features ---------------------------------------------------------------


@dataclass
class MatchResult:
    score: int
    summary: str
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


_MATCH_SYSTEM = (
    "You are an expert technical recruiter and career coach. Assess how well the "
    "candidate's resume matches the job description. Be realistic, specific, and "
    "concise. Respond with ONLY a JSON object (no markdown, no prose) of the form:\n"
    '{"score": <integer 0-100>, "summary": "<one or two sentences>", '
    '"strengths": ["..."], "gaps": ["..."]}\n'
    "score is the overall fit where 100 is a perfect match. strengths are concrete "
    "matches; gaps are concrete missing or weak areas. Keep each list to 3-6 items. "
    "Use only plain ASCII punctuation: a hyphen (-) instead of em/en dashes, straight "
    "quotes, and ... instead of an ellipsis character."
)


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first {...} block
    if m:
        return json.loads(m.group(0))
    raise ValueError("The model did not return a parseable match result.")


def match_score(resume: str, job: str, resume_pdf: Optional[bytes] = None) -> MatchResult:
    text = _complete(
        _MATCH_SYSTEM,
        2000,
        "Return the match assessment as JSON only.",
        cached_tail=f"=== JOB DESCRIPTION ===\n{_clip(job, _MAX_JOB_CHARS)}",
        resume_text=resume,
        resume_pdf=resume_pdf,
        model=MATCH_ANTHROPIC_MODEL,
    )
    data = _extract_json(text)
    try:
        score = max(0, min(100, int(round(float(data.get("score", 0))))))
    except (TypeError, ValueError):
        score = 0
    strengths = [normalize_punctuation(str(s)) for s in (data.get("strengths") or []) if str(s).strip()]
    gaps = [normalize_punctuation(str(s)) for s in (data.get("gaps") or []) if str(s).strip()]
    summary = normalize_punctuation(str(data.get("summary", "")).strip())
    return MatchResult(score, summary, strengths, gaps)


# Shared tone guidance. Job-application text from an LLM tends to read like a
# recruiter template - inflated, generic, full of tells like "leverage" and
# "aligns perfectly". These rules push the writing back toward how an actual
# engineer talks about their work: specific, plain, and grounded.
_HUMAN_VOICE = (
    "VOICE - write as the engineer would actually speak, not as a cover-letter "
    "generator. The writing should say 'I have built these things and I want "
    "to solve this specific problem,' NOT 'I am excited about your mission.' "
    "Concretely:\n"
    "- Be direct, specific and practical. Name the real systems worked on, the "
    "problems solved, and how you work with a team. Concrete detail beats "
    "praise.\n"
    "- Show, don't claim. Instead of saying work is 'high-impact' or that you "
    "'thrive' on it, name the actual work: designing the architecture, "
    "improving performance, keeping services available, owning a service "
    "end to end. Let the specifics carry the enthusiasm.\n"
    "- Use plain verbs: built, worked on, shipped, maintained, debugged, "
    "designed, supported. Avoid resume/marketing verbs like leverage, utilize, "
    "spearhead, empower, craft, or 'deliver impactful solutions'.\n"
    "- Cut inflated intensifiers and absolutes: perfectly, significantly, "
    "extremely, seamlessly, robust, resilient, cutting-edge, world-class, "
    "foundational, high-impact, passionate, enticing, thrive.\n"
    "- Don't stack adjectives. 'a reliable and resilient platform in a "
    "regulated environment' piles on qualifiers - use at most one plain "
    "adjective before a noun, or better, say what you actually did with it.\n"
    "- Avoid these cliches AND any close variant - if a sentence could appear "
    "unchanged in a hundred other applications, rewrite it: 'the role aligns "
    "with my experience' / 'aligns perfectly with my expertise' (don't use "
    "'align' at all), 'foundational systems', 'high-impact work I thrive on', "
    "'contribute significantly to your team', 'delivering impactful "
    "solutions', 'leverage my skills to create robust, user-friendly "
    "products', 'proven track record', 'fast-paced environment', 'passionate "
    "about', 'excited about the opportunity to'.\n"
    "- Favor ownership, systems, trade-offs, collaboration and delivery - the "
    "things a hiring engineer actually cares about.\n"
    "- Sound human: vary sentence length, allow a plain honest sentence, and "
    "do not start every paragraph the same way. It should read like the "
    "candidate wrote it, not a template.\n"
    "- Stay truthful and grounded in the resume: no invented employers, tools, "
    "numbers, or credentials, and no exaggerated claims.\n"
    "- Contractions are normal speech - I've, don't, wasn't, it's. Prose with "
    "none of them reads as machine-written.\n"
    "- Avoid the three-item list as a default rhythm ('scalable, maintainable "
    "and testable'). Real people name one thing and move on.\n"
    "- No 'Not only X, but also Y' and no 'It's not just X, it's Y'. Both are "
    "tells.\n"
    "- Do not end with a summarising or eager sign-off sentence ('I'd welcome "
    "the chance to...', 'I'm confident I would...'). Stop when the point is "
    "made."
)


# The answer box is where the machine-written smell shows up worst: a question
# arrives with no context, and a model with nothing specific to say falls back
# on fluent generalities. The fix is not more prohibitions - there are plenty
# above - but forcing the answer to rest on something that actually happened,
# which is in the resume the person picked.
_ANSWER_CRAFT = (
    "GROUND EVERY ANSWER IN THE RESUME.\n"
    "- Before writing, find the work in the resume that genuinely bears on the "
    "question: a role, a project, a system, a technology, a problem that was "
    "fixed. Write from that. An answer that would read the same for any "
    "candidate is a failed answer.\n"
    "- Prefer the work history over the skills list. 'I maintained the "
    "payments service at <the employer in the resume>' beats 'I am proficient "
    "in Python'.\n"
    "- Name real specifics from the resume - the actual employer, product, "
    "stack or scale. Never invent one. If the resume genuinely has nothing on "
    "the topic, say the nearest true thing from it and keep the answer short "
    "rather than padding it out.\n"
    "- If the resume shows how long something was worked on, or what the "
    "outcome was, use it. Numbers already in the resume are good; new ones are "
    "not.\n\n"
    "DO NOT DESCRIBE THE COMPANY TO ITSELF.\n"
    "- They know what they do. Characterising their market, mission, scale or "
    "impact back to them - 'your role in large-scale delivery networks', 'your "
    "innovative platform' - carries no information and is the clearest sign an "
    "answer was generated rather than written. Cut those sentences entirely.\n"
    "- Never guess at their systems, stack or architecture. Phrases of the form "
    "'services like the ones you use for X' claim knowledge the candidate does "
    "not have and a real applicant would not risk.\n"
    "- Do not echo the posting's vocabulary back at them. Naming the problem it "
    "describes is fine, once, in your own plain words; stacking its terms into "
    "a phrase - 'advancing your test automation for data science microservices' "
    "- is keyword matching, and it reads as machine-written because that is "
    "exactly what it is. A term should appear because it is in the resume and "
    "the candidate did it, never because it appeared in the ad. Describe what "
    "they built; do not mirror what the company called it.\n"
    "- Interest, where the question actually asks for it, comes from the work: "
    "the problem named in the posting is one they have solved before, or want "
    "to. Not from the company's mission.\n"
    "- Test each sentence: does it carry a fact about the candidate, or a fact "
    "the posting stated? A sentence with neither is filler - delete it.\n\n"
    "SHAPE.\n"
    "- Open on the candidate's own work, not on the company and not on how they "
    "feel about it. 'I'm interested in <company> because...' is the standard "
    "generated opener and gives the first sentence away; so do 'As a...' and "
    "'I am a...'. Start with what they have done, then connect it.\n"
    "- First sentence answers the question. Do not restate the question.\n"
    "- No enthusiasm vocabulary anywhere: exciting, excited, thrilled, "
    "opportunity, 'a new and exciting challenge', 'I would love to'. If the "
    "work is interesting, the specifics show it; saying so does not.\n"
    "- Then the evidence: what was built or handled, and what made it "
    "awkward. One worked example beats three claims.\n"
    "- Avoid the four-beat application-letter arc - my background, your needs "
    "match it, I want to solve your problems, your mission excites me. It is "
    "the shape every generated answer takes. Stay on the candidate's own work "
    "and stop when the question is answered.\n"
    "- Length follows the question. A short question gets a few sentences, not "
    "an essay.\n\n"
    "STYLE EXAMPLES - these show the difference in voice only. Never copy "
    "their content, and never reuse the placeholder names; every specific in "
    "your answer must come from the resume you were given.\n"
    "Q: Why are you interested in this role?\n"
    "Weak (machine-written): 'I am excited about the opportunity to leverage "
    "my expertise in distributed systems to deliver impactful solutions and "
    "contribute significantly to your innovative team.'\n"
    "Better (human): 'The posting says the hard part is the migration off the "
    "shared database. That is the same problem I spent most of last year on at "
    "<employer from resume> - splitting one Postgres instance behind <service "
    "from resume> without downtime. I would rather work on that than another "
    "greenfield CRUD service.'\n"
    "Q: Describe a challenge you faced.\n"
    "Weak (machine-written): 'I faced a challenging situation where I "
    "leveraged my problem-solving skills to seamlessly resolve a critical "
    "issue, demonstrating my ability to thrive under pressure.'\n"
    "Better (human): 'We had a nightly job at <employer from resume> that "
    "silently dropped records when the upstream feed changed shape. It took "
    "two days to find because the job reported success either way. I added a "
    "row-count check against the source and made it fail loudly instead.'\n"
    "Q: Why do you want to work at <company>?\n"
    "Weak (machine-written - it explains the company to itself and guesses at "
    "their systems): '<company>'s role in large-scale delivery networks "
    "intrigues me, as does the chance to improve the reliability of your "
    "systems. I have built backend services, like the ones <company> uses for "
    "logistics, and would love to contribute to your mission.'\n"
    "Better (human - opens on their own work, names the overlap in plain words, "
    "and stops): 'Most of my last two years at <employer from resume> went on "
    "keeping <service from resume> up: cutting API response times and getting "
    "the on-call pages down. The posting asks for someone to do that here, and "
    "it is the part of the job I am actually good at.'"
)


_ANSWER_SYSTEM = (
    "You are the software engineer whose resume follows, answering a question "
    "on a job application. Write it as they would: first person, truthful, "
    "built out of their own work history, and aimed at this particular job. "
    "You are not writing on their behalf in a professional register - you are "
    "writing what they would type.\n\n"
    f"{_HUMAN_VOICE}\n\n"
    f"{_ANSWER_CRAFT}\n\n"
    "Before you answer, check it against two questions: could this have been "
    "written by someone who never read the resume, and could it be sent to a "
    "different company unchanged? If either is yes, rewrite it around "
    "something specific from the resume.\n\n"
    "Output only the answer text - no preamble, labels, or quotation marks. "
    "Use only plain ASCII punctuation: a hyphen (-) instead of em/en dashes, "
    "straight quotes, and ... instead of an ellipsis character."
)


def _output_directive(language: str = "English", max_chars: Optional[int] = None) -> str:
    """Trailing instruction to control the answer's language and length.

    Always emits an explicit language instruction (English included) so the
    answer is in the selected language even when the job description or question
    is written in a different language."""
    parts: list[str] = []
    lang = (language or "").strip()
    low = lang.lower()
    if low in ("english and spanish", "spanish and english", "en&es", "en & es"):
        parts.append(
            "Write the full response in both English and Spanish, regardless of "
            "the language of the job description or the question: first the "
            "complete English version under a line reading 'English:', then the "
            "complete Spanish version under a line reading 'Spanish:'."
        )
    else:
        if low in ("spanish", "es"):
            target = "Spanish"
        elif low in ("", "english", "en"):
            target = "English"
        else:
            target = lang
        parts.append(
            f"Write the entire response in {target} only, regardless of the "
            "language of the job description or the question."
        )
    if max_chars and max_chars > 0:
        parts.append(
            f"Keep the response to at most {max_chars} characters; "
            "be concise and do not exceed this limit."
        )
    return ("\n\n" + " ".join(parts)) if parts else ""


def answer_question(
    resume: str,
    job: str,
    question: str,
    resume_pdf: Optional[bytes] = None,
    *,
    language: str = "English",
    max_chars: Optional[int] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    volatile = (
        f"=== APPLICATION QUESTION ===\n{_clip(question, _MAX_QUESTION_CHARS)}\n\n"
        "Write the answer only."
        f"{_output_directive(language, max_chars)}"
    )
    return normalize_punctuation(
        _complete(
            _ANSWER_SYSTEM,
            2000,
            volatile,
            cached_tail=f"=== JOB DESCRIPTION ===\n{_clip(job, _MAX_JOB_CHARS)}",
            resume_text=resume,
            resume_pdf=resume_pdf,
            on_chunk=on_chunk,
        )
    )


_COVER_LETTER_SYSTEM = (
    "You write a concise cover letter for a software engineer, tailored to the "
    "job and grounded ONLY in the candidate's resume. Use 3-4 short paragraphs: "
    "a brief hook, why the candidate fits (specific skills and experience that "
    "match the role), why this company/role, and a short close. Write in the "
    "first person.\n\n"
    f"{_HUMAN_VOICE}\n\n"
    "Start with a greeting (e.g. 'Dear Hiring Manager,') and output only the "
    "letter body - no date, addresses, or placeholders like [Your Name]. Use "
    "only plain ASCII punctuation: a hyphen (-) instead of em/en dashes, "
    "straight quotes, and ... instead of an ellipsis character."
)


def write_cover_letter(
    resume: str,
    job: str,
    resume_pdf: Optional[bytes] = None,
    *,
    language: str = "English",
    max_chars: Optional[int] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    volatile = (
        "Write the cover letter only."
        f"{_output_directive(language, max_chars)}"
    )
    return normalize_punctuation(
        _complete(
            _COVER_LETTER_SYSTEM,
            3000,
            volatile,
            cached_tail=f"=== JOB DESCRIPTION ===\n{_clip(job, _MAX_JOB_CHARS)}",
            resume_text=resume,
            resume_pdf=resume_pdf,
            on_chunk=on_chunk,
        )
    )


_JOB_INFO_SYSTEM = (
    "You answer factual questions about a job posting based ONLY on its description. "
    "Be concise and direct (1-3 sentences). Lead with a clear yes / no / unclear when "
    "the question is yes/no, then the supporting detail (e.g. location or time-zone "
    "constraints). If the description does not state the answer, say so plainly rather "
    "than guessing. Use only plain ASCII punctuation: a hyphen (-) instead of em/en "
    "dashes, straight quotes, and ... instead of an ellipsis character."
)


_JOB_PARSE_SYSTEM = (
    "You extract structured details from raw text the user copied from a job "
    "posting (a listing, email, recruiter message, or web page). Output PLAIN "
    "TEXT only, in EXACTLY this layout - the labeled lines first (every label, "
    "in this order), then a blank line, then the job description:\n"
    "Company: <hiring company name>\n"
    "Role: <job title>\n"
    "Location: <city / country, 'Remote', or hybrid details>\n"
    "Salary: <pay or salary range with currency and period, e.g. "
    "'$120k-$150k / year' or '$60 / hour'>\n"
    "Employment type: <full-time, part-time, contract, internship, etc.>\n"
    "Contact: <recruiter or contact person name and/or email, if present>\n"
    "URL: <application or posting URL, if present>\n"
    "\n"
    "<a clean, readable plain-text version of the job description>\n"
    "\n"
    "Rules:\n"
    "- Always output every label line, in the order above. Leave a value blank "
    "ONLY when the text genuinely does not contain it - never drop the label.\n"
    "- Find the COMPANY even when it appears only in a signature, an email "
    "domain, a 'we / our team at X' phrase, the sender, or the subject line.\n"
    "- Find the SALARY / compensation in any phrasing - a range, 'up to', "
    "'from', hourly, monthly, or annual - and include the currency and period.\n"
    "- Do not invent details that are not in the text.\n"
    "- Keep the labels exactly as shown. Use only plain ASCII punctuation: a "
    "hyphen (-) instead of em/en dashes, straight quotes, and ... instead of an "
    "ellipsis character."
)


def parse_job_posting(text: str) -> str:
    """Parse copied job-posting text into the app's labeled card-body format.

    Uses the mid-tier model (not the fastest) so messy pastes still yield the
    company, salary, and other fields reliably."""
    volatile = (
        f"=== COPIED TEXT ===\n{_clip(text, _MAX_JOB_CHARS)}\n\n"
        "Extract every labeled field and the description as specified. Do not "
        "omit any label line."
    )
    return normalize_punctuation(
        _complete(_JOB_PARSE_SYSTEM, 4000, volatile, model=MATCH_ANTHROPIC_MODEL)
    )


def ask_job_question(
    job: str,
    question: str,
    *,
    language: str = "English",
    max_chars: Optional[int] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> str:
    """Answer a factual question about the job posting (no resume involved)."""
    volatile = (
        f"=== QUESTION ===\n{_clip(question, _MAX_QUESTION_CHARS)}\n\n"
        "Answer concisely based only on the job description."
        f"{_output_directive(language, max_chars)}"
    )
    return normalize_punctuation(
        _complete(
            _JOB_INFO_SYSTEM,
            1000,
            volatile,
            cached_tail=f"=== JOB DESCRIPTION ===\n{_clip(job, _MAX_JOB_CHARS)}",
            model=FAST_ANTHROPIC_MODEL,
            on_chunk=on_chunk,
        )
    )


def format_error(exc: BaseException) -> str:
    """A short, user-facing message for an AI call failure."""
    if isinstance(exc, ModuleNotFoundError):
        return "The 'anthropic' package is not installed."
    name = type(exc).__name__
    if name == "AuthenticationError":
        return "Invalid Anthropic API key. Check it in Settings."
    if name == "PermissionDeniedError":
        return "This API key lacks permission for the selected model."
    if name == "RateLimitError":
        return "Rate limited by the Anthropic API. Try again shortly."
    if name == "APIConnectionError":
        return "Could not reach the Anthropic API. Check your connection."
    return str(exc) or name


class AIWorker(QObject):
    """Runs a callable on a worker thread; emits (result, error_message).

    The callable receives one argument: an ``on_chunk`` callback it may invoke with
    incremental text (emitted via the ``chunk`` signal for live streaming). Callables
    that don't stream simply ignore it."""

    finished = pyqtSignal(object, str)
    chunk = pyqtSignal(str)

    def __init__(self, fn) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn(self.chunk.emit)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.finished.emit(None, format_error(exc))
            return
        self.finished.emit(result, "")
