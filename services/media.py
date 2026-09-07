"""Media Service：产物缓存、MIME、分块拉取、签名 URL。

保持 v0.1.1 修复的关键行为：不同 subfolder 同名产物缓存 key 必须含 subfolder。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("astrbot_plugin_remote_link")

EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
}


class MediaService:
    def __init__(
        self,
        media_dir: Path,
        config: dict,
        token_provider: Callable[[], str],
        fetch_stream: Callable[..., Any] | None = None,
        container_ip_provider: Callable[[], str] | None = None,
    ) -> None:
        self._media_dir = Path(media_dir)
        self._config = config
        self._token_provider = token_provider
        self._fetch_stream = fetch_stream
        self._container_ip_provider = container_ip_provider or (lambda: "127.0.0.1")

    @property
    def media_dir(self) -> Path:
        return self._media_dir

    # ---------- 签名 URL ----------

    def signature(self, filename: str, expires: int) -> str:
        secret = self._token_provider()
        msg = f"{filename}:{expires}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    def verify_signature(self, request) -> bool:
        filename = request.query.get("filename", "").strip()
        expires = request.query.get("expires", "").strip()
        sig = request.query.get("sig", "").strip()
        if not filename or not expires or not sig:
            return False
        try:
            expires_int = int(expires)
        except ValueError:
            return False
        if expires_int < time.time():
            return False
        expected = self.signature(Path(filename).name, expires_int)
        return hmac.compare_digest(expected, sig)

    def signed_url(self, filename: str, ttl: int = 1800) -> str:
        from urllib.parse import urlencode

        name = Path(filename).name
        expires = int(time.time()) + ttl
        sig = self.signature(name, expires)
        qs = urlencode({"filename": name, "expires": expires, "sig": sig})
        return f"http://{self._container_ip_provider()}:8468/media?{qs}"

    def file_url(self, filename: str) -> str:
        return self.signed_url(Path(filename).name)

    # ---------- 本地缓存 ----------

    def save_media(self, files: list[dict]) -> list[dict]:
        saved = []
        for f in files:
            filename = Path(str(f.get("filename") or f"{uuid.uuid4().hex}.bin")).name
            entry = {
                "kind": f.get("kind") or "image",
                "path": "",
                "filename": filename,
                "mime": f.get("mime") or "",
                "subfolder": f.get("subfolder", ""),
                "type": f.get("type", "output"),
                "node": f.get("node", ""),
                "node_type": f.get("node_type", ""),
                "index": f.get("index", 0),
                "main": bool(f.get("main")),
            }
            b64 = f.get("base64") or ""
            if b64:
                try:
                    data = base64.b64decode(b64)
                except Exception as e:
                    logger.warning(f"[remote_link] 产物 {filename} base64 解码失败: {e}（长度 {len(b64)}）")
                    continue
                logger.info(
                    f"[remote_link] 产物落盘 {filename}: kind={entry['kind']} "
                    f"base64_len={len(b64)} bytes={len(data)}"
                )
                self._media_dir.mkdir(parents=True, exist_ok=True)
                path = self._media_dir / filename
                path.write_bytes(data)
                entry["path"] = str(path)
            saved.append(entry)
        if saved:
            self.prune_media()
        return saved

    def prune_media(self):
        try:
            if not self._media_dir.is_dir():
                return
            in_dir = self._media_dir / "inputs"
            if in_dir.is_dir():
                tmp = sorted(in_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
                for p in tmp[10:]:
                    try:
                        p.unlink()
                    except Exception:
                        pass
            files = [p for p in self._media_dir.glob("*") if p.is_file() and p.parent == self._media_dir]
            limit = int(self._config.get("media_max_files", 100) or 100)
            if len(files) > limit:
                for p in sorted(files, key=lambda p: p.stat().st_mtime)[: len(files) - limit]:
                    try:
                        p.unlink()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[remote_link] 媒体清理失败: {e}")

    async def pull_media(self, f: dict) -> dict:
        if f.get("path") and Path(str(f["path"])).is_file():
            return f
        filename = str(f.get("filename") or "")
        if not filename:
            return f
        # 缓存 key 必须含 subfolder：不同子目录可能产出同名文件
        subfolder = str(f.get("subfolder") or "")
        safe = Path(filename).name
        cache_name = f"{subfolder.replace('/', '_')}__{safe}" if subfolder else safe
        path = self._media_dir / cache_name
        if path.is_file():
            f["path"] = str(path)
            return f
        if self._fetch_stream is None:
            return f
        chunks: list[str] = []
        try:
            await self._fetch_stream(
                "media_get",
                {
                    "filename": filename,
                    "subfolder": f.get("subfolder", ""),
                    "type": f.get("type", "output"),
                },
                on_chunk=lambda t: chunks.append(t),
                timeout=300,
            )
        except Exception as e:
            logger.warning(f"[remote_link] 拉取产物 {filename} 失败: {e}")
            return f
        b64 = "".join(chunks)
        try:
            data = base64.b64decode(b64)
        except Exception as e:
            logger.warning(f"[remote_link] 产物 {filename} base64 解码失败: {e}（总长 {len(b64)}）")
            return f
        logger.info(f"[remote_link] 拉取产物 {filename}: b64_len={len(b64)} bytes={len(data)}")
        self._media_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        f["path"] = str(path)
        return f

    def media_captions(self, files: list[dict]) -> list[str]:
        if len(files) <= 1:
            return []
        lines = [f"📎 本次共 {len(files)} 个产物："]
        for i, f in enumerate(files, 1):
            tag = " ⭐主产物" if f.get("main") else ""
            node = f.get("node_type") or f.get("node") or ""
            lines.append(f"{i}) {f.get('filename', '?')}（{node}）{tag}")
        return lines
