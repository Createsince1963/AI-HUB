"""HTTP + model-hub clients (Hugging Face, Civitai, Ollama). All functions block - run them in a worker."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator

UA = "AI-Launcher/1.0 (portable workspace)"
HF = "https://huggingface.co"
CIVITAI = "https://civitai.com"


class NoAuthCrossHost(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect leaves the original host (HF -> CDN signed URLs)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


OPENER = urllib.request.build_opener(NoAuthCrossHost)


def http_open(url: str, headers: dict | None = None, data: bytes | None = None, method: str | None = None,
              timeout: float = 20):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    return OPENER.open(req, timeout=timeout)


def http_json(url: str, headers: dict | None = None, data: dict | None = None, method: str | None = None,
              timeout: float = 20):
    body = json.dumps(data).encode() if data is not None else None
    hdr = dict(headers or {})
    if body is not None:
        hdr["Content-Type"] = "application/json"
    try:
        with http_open(url, hdr, body, method, timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason} - {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


# ------------------------------------------------------------------ Hugging Face
def hf_headers(token: str = "") -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def hf_search(query: str, pipeline: str = "", tags: list[str] | None = None, sort: str = "downloads",
              limit: int = 40, token: str = "") -> list[dict]:
    params: list[tuple[str, str]] = [("limit", str(limit)), ("sort", sort), ("direction", "-1")]
    if query:
        params.append(("search", query))
    if pipeline:
        params.append(("pipeline_tag", pipeline))
    for t in tags or []:
        params.append(("filter", t))
    for f in ("downloads", "likes", "pipeline_tag", "tags", "lastModified", "gated", "library_name"):
        params.append(("expand[]", f))
    data = http_json(f"{HF}/api/models?{urllib.parse.urlencode(params)}", hf_headers(token))
    return [{
        "id": m.get("id") or m.get("modelId"),
        "downloads": m.get("downloads"),
        "likes": m.get("likes"),
        "pipeline": m.get("pipeline_tag") or "",
        "tags": m.get("tags") or [],
        "updated": (m.get("lastModified") or "")[:10],
        "gated": bool(m.get("gated")),
        "library": m.get("library_name") or "",
    } for m in data]


def hf_files(repo: str, token: str = "") -> list[dict]:
    """All files of a repo (recursive) with sizes."""
    out: list[dict] = []
    url = f"{HF}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/main?recursive=true&limit=1000"
    data = http_json(url, hf_headers(token))
    for f in data:
        if f.get("type") == "file":
            size = (f.get("lfs") or {}).get("size") or f.get("size")
            out.append({"path": f["path"], "size": size})
    return out


def hf_download_url(repo: str, path: str) -> str:
    return f"{HF}/{urllib.parse.quote(repo, safe='/')}/resolve/main/{urllib.parse.quote(path)}?download=true"


# ------------------------------------------------------------------ Civitai
CIVITAI_TYPES = ["Checkpoint", "LORA", "TextualInversion", "Controlnet", "VAE", "Upscaler", "LoCon", "DoRA"]


def civitai_search(query: str, mtype: str = "", sort: str = "Most Downloaded", limit: int = 30, nsfw: bool = False,
                   token: str = "") -> list[dict]:
    params = [("limit", str(limit)), ("sort", sort), ("nsfw", "true" if nsfw else "false")]
    if query:
        params.append(("query", query))
    if mtype:
        params.append(("types", mtype))
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    data = http_json(f"{CIVITAI}/api/v1/models?{urllib.parse.urlencode(params)}", hdr, timeout=30)
    out = []
    for m in data.get("items", []):
        versions = []
        for v in m.get("modelVersions", []):
            files = [{"name": f.get("name", ""), "size": int((f.get("sizeKB") or 0) * 1024),
                      "url": f.get("downloadUrl", ""), "type": f.get("type", ""),
                      "format": (f.get("metadata") or {}).get("format", "")} for f in v.get("files", [])]
            versions.append({"id": v.get("id"), "name": v.get("name", ""), "base": v.get("baseModel", ""),
                             "files": files})
        st = m.get("stats") or {}
        out.append({"id": m.get("id"), "name": m.get("name", ""), "type": m.get("type", ""),
                    "downloads": st.get("downloadCount"), "likes": st.get("thumbsUpCount"),
                    "versions": versions})
    return out


def civitai_url(url: str, token: str = "") -> str:
    if token:
        return url + ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
    return url


# ------------------------------------------------------------------ Ollama
class OllamaClient:
    def __init__(self, host: str, port: int):
        h = "127.0.0.1" if host in ("0.0.0.0", "") else host
        self.base = f"http://{h}:{port}"

    def tags(self) -> list[dict]:
        return http_json(f"{self.base}/api/tags", timeout=4).get("models", [])

    def ps(self) -> list[dict]:
        return http_json(f"{self.base}/api/ps", timeout=4).get("models", [])

    def version(self) -> str:
        return http_json(f"{self.base}/api/version", timeout=3).get("version", "")

    def delete(self, name: str) -> None:
        http_json(f"{self.base}/api/delete", data={"model": name}, method="DELETE", timeout=30)

    def show(self, name: str) -> dict:
        return http_json(f"{self.base}/api/show", data={"model": name}, timeout=15)

    def unload(self, name: str) -> None:
        http_json(f"{self.base}/api/generate", data={"model": name, "keep_alive": 0}, timeout=15)

    def pull(self, name: str, cb: Callable[[str, int, int], None], cancelled: Callable[[], bool]) -> None:
        """Streaming pull. cb(status, completed, total)."""
        body = json.dumps({"model": name, "stream": True}).encode()
        last = ""
        with http_open(f"{self.base}/api/pull", {"Content-Type": "application/json"}, body, "POST",
                       timeout=60) as r:
            for line in r:
                if cancelled():
                    return
                if not line.strip():
                    continue
                d = json.loads(line)
                if "error" in d:
                    raise RuntimeError(d["error"])
                last = d.get("status", "")
                cb(last, int(d.get("completed") or 0), int(d.get("total") or 0))
        # A pull only counts as finished when Ollama reports "success"; a stream that just ends
        # (server stopped/restarted, connection lost) is an error, not "done".
        if last != "success":
            raise RuntimeError(f"pull did not finish (last status: {last or 'none'}) - is the Ollama server still running?")


# Curated suggestions for a 12 GB card (Q4 variants, weights fit fully in VRAM)
OLLAMA_SUGGESTED = [
    ("qwen3:8b", "General / reasoning, fast", "5.2 GB"),
    ("qwen3:14b", "General / reasoning, best quality that fits 12 GB", "9.3 GB"),
    ("qwen2.5-coder:14b", "Coding assistant", "9.0 GB"),
    ("gemma3:12b", "General + vision", "8.1 GB"),
    ("llama3.1:8b", "General purpose", "4.9 GB"),
    ("mistral-nemo:12b", "General, 128k context", "7.1 GB"),
    ("deepseek-r1:14b", "Reasoning", "9.0 GB"),
    ("phi4:14b", "Reasoning / STEM", "9.1 GB"),
    ("llava:13b", "Vision", "8.0 GB"),
    ("nomic-embed-text", "Embeddings (RAG)", "0.3 GB"),
]


def ollama_library_search(query: str, limit: int = 30) -> list[dict]:
    """Best-effort scrape of ollama.com/search (there is no official search API)."""
    url = f"https://ollama.com/search?q={urllib.parse.quote(query)}"
    with http_open(url, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    out = []
    for block in re.split(r"<li[^>]*x-test-model[^>]*>", html)[1:]:
        m = re.search(r'href="/(?:library/)?([^"]+)"', block)
        if not m:
            continue
        name = m.group(1)
        desc = re.search(r"<p[^>]*>\s*(.*?)\s*</p>", block, re.S)
        sizes = re.findall(r"x-test-size[^>]*>\s*([^<\s]+)\s*<", block)
        pulls = re.search(r"x-test-pull-count[^>]*>\s*([^<\s]+)\s*<", block)
        out.append({"name": name, "desc": re.sub(r"<[^>]+>", "", desc.group(1)) if desc else "",
                    "sizes": sizes, "pulls": pulls.group(1) if pulls else ""})
        if len(out) >= limit:
            break
    return out
