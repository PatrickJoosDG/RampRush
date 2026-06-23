#!/usr/bin/env python3
"""
InternVL3 image describer (llama.cpp backend).

Talks to a running llama.cpp server (`llama-server`) over its OpenAI-compatible
HTTP API instead of loading the model in-process with transformers. This keeps
the script dependency-free apart from `requests`: llama.cpp's multimodal
projector (mmproj) handles all image preprocessing internally, so there is no
torch / torchvision / manual tiling here.

Prerequisites:
    1. Build or install llama.cpp (provides `llama-server`).
           brew install llama.cpp        # or build from source
    2. Download two GGUF files for InternVL3 (search Hugging Face for
       "InternVL3 GGUF"): the LLM weights AND the matching mmproj file.
    3. Start the server with vision enabled:
           llama-server -m InternVL3-8B-Q4_K_M.gguf \
                        --mmproj mmproj-InternVL3-8B-f16.gguf \
                        --host 127.0.0.1 --port 8080

Usage (CLI):
    python internvl3_llamacpp.py path/to/photo.jpg
    python internvl3_llamacpp.py https://example.com/photo.jpg
    python internvl3_llamacpp.py photo.jpg --prompt "Describe any visible damage."
    python internvl3_llamacpp.py photo.jpg --server http://127.0.0.1:8080

Usage (import) -- same API as the transformers version:
    from internvl3_llamacpp import describe_image
    text = describe_image("data/photos/TRK-001.jpg")

Dependencies:
    pip install requests
"""
import argparse
import base64
import mimetypes
import sys
from pathlib import Path

import requests

DEFAULT_SERVER = "http://127.0.0.1:8081"
DEFAULT_PROMPT = "Describe this image in detail."


def _image_data_url(source: str) -> str:
    """Return a base64 data URL for a local path or http(s) URL image.

    llama-server accepts an image either as a remote URL or as an inline
    base64 data URL; we always inline it so local files work too.
    """
    if source.startswith(("http://", "https://")):
        resp = requests.get(source, timeout=30)
        resp.raise_for_status()
        raw = resp.content
        mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
    else:
        raw = Path(source).read_bytes()
        mime = mimetypes.guess_type(source)[0] or "image/jpeg"

    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def describe_image(
    source: str,
    prompt: str = DEFAULT_PROMPT,
    server: str = DEFAULT_SERVER,
    max_new_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: int = 120,
) -> str:
    """Return a detailed text description of the image at `source`.

    `source` is a local file path or an http(s) URL. Requires a running
    llama-server started with an InternVL3 model and its --mmproj projector.
    """
    data_url = _image_data_url(source)

    payload = {
        "model": "internvl3",  # ignored by llama-server, but kept for OpenAI compat
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": max_new_tokens,
        "temperature": temperature,
    }

    resp = requests.post(
        f"{server.rstrip('/')}/v1/chat/completions",
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def main() -> None:
    p = argparse.ArgumentParser(description="Describe an image with InternVL3 via llama.cpp")
    p.add_argument("image", help="path or http(s) URL to an image")
    p.add_argument("--prompt", default=DEFAULT_PROMPT, help="instruction for the model")
    p.add_argument("--server", default=DEFAULT_SERVER, help="llama-server base URL")
    p.add_argument("--max-new-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.0)
    args = p.parse_args()

    if not args.image.startswith(("http://", "https://")) and not Path(args.image).exists():
        sys.exit(f"image not found: {args.image}")

    try:
        description = describe_image(
            args.image,
            prompt=args.prompt,
            server=args.server,
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
        )
    except requests.exceptions.ConnectionError:
        sys.exit(f"could not reach llama-server at {args.server} -- is it running?")
    print(description)


if __name__ == "__main__":
    main()
