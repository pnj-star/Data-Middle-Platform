"""Vision-LLM image description generation (OpenAI-compatible chat completions).

Enabled only when the reserved ``LLM_*`` env vars are fully configured. Any
missing config, network failure, or non-2xx response degrades gracefully to an
empty description string (matching the pre-VLM behavior), so the image ingest
pipeline never fails because of the describer.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from .config import config as app_config
from .logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_PROMPT = "请用中文简要描述这张图片的内容。请只返回描述本身，不要添加额外说明。"


def is_enabled() -> bool:
    llm = app_config.llm
    return bool(llm.enabled and llm.base_url and llm.api_key and llm.model)


def _data_uri(image_path: str | Path) -> str:
    fp = Path(image_path)
    mime = mimetypes.guess_type(str(fp))[0] or "image/jpeg"
    b64 = base64.b64encode(fp.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def generate_image_description(
    image_path: str | Path,
    prompt: str | None = None,
) -> str:
    """Describe an image in Chinese via the configured vision LLM.

    Returns an empty string when disabled, misconfigured, or on any error.
    """
    if not is_enabled():
        return ""

    llm = app_config.llm
    content_blocks = [
        {"type": "text", "text": prompt or DEFAULT_PROMPT},
        {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
    ]

    try:
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=llm.api_key,
                base_url=llm.base_url,
                timeout=llm.timeout_seconds,
            )
            resp = client.chat.completions.create(
                model=llm.model,
                messages=[{"role": "user", "content": content_blocks}],
                max_tokens=llm.max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except ImportError:
            import httpx

            r = httpx.post(
                llm.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {llm.api_key}"},
                json={
                    "model": llm.model,
                    "messages": [{"role": "user", "content": content_blocks}],
                    "max_tokens": llm.max_tokens,
                },
                timeout=llm.timeout_seconds,
            )
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        _log.warning("VLM description failed for %s: %s", image_path, e)
        return ""
