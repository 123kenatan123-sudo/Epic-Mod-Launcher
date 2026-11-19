#!/usr/bin/env python3
"""
Автоматическая синхронизация каталога модов из Discord-форума в catalog.json
с зеркалированием вложений и превью в GitHub Releases (тег: catalog-assets).

Требует только стандартную библиотеку Python 3.
Переменные окружения:
  DISCORD_BOT_TOKEN        — токен Discord-бота (из GitHub Secrets)
  DISCORD_GUILD_ID         — ID сервера (по умолчанию 1566032190062727399)
  DISCORD_FORUM_CHANNEL_ID — ID форума/канала модов (по умолчанию 1566035062443610122)
  GITHUB_TOKEN             — токен GitHub Actions для загрузки ассетов в Release
  GITHUB_REPOSITORY        — владелец/репозиторий (по умолчанию AzureLalik/EpicModLauncher)
  ASSET_RELEASE_TAG        — тег релиза для хранения зеркал (по умолчанию catalog-assets)
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


DISCORD_API = "https://discord.com/api/v10"
GITHUB_API = "https://api.github.com"

MOD_EXTS = (".zip", ".dll", ".csproj")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

VERSION_PATTERNS = (
    re.compile(r"(?i)\bv(?:er(?:sion)?)?\.?\s*([0-9]+(?:\.[0-9]+){1,3}[a-z0-9\-]*)"),
    re.compile(r"(?i)версия\s*[:\-]?\s*v?([0-9]+(?:\.[0-9]+){1,3}[a-z0-9\-]*)"),
    re.compile(r"(?i)\b([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"),
)
URL_RX = re.compile(r"https?://[^\s<>()\[\]\"']+")
GH_REPO_RX = re.compile(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", re.I)


def http_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    method: str = "GET",
    body: Any = None,
    timeout: int = 30,
) -> Tuple[int, Any]:
    hdrs = {"User-Agent": "EpicModLauncherCatalogSync/2.0"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            parsed = json.loads(raw) if raw.strip() else raw
        except Exception:
            parsed = raw
        return e.code, parsed


def http_bytes(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 60) -> bytes:
    hdrs = {"User-Agent": "EpicModLauncherCatalogSync/2.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clean_discord_text(text: str) -> str:
    if not text:
        return ""
    s = text.replace("\r\n", "\n")
    s = re.sub(r"<@!?(\d+)>", "", s)
    s = re.sub(r"<@&(\d+)>", "", s)
    s = re.sub(r"<#(\d+)>", "", s)
    s = re.sub(r"<a?:([a-zA-Z0-9_]+):\d+>", r":\1:", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def gather_message_text(msg: Dict[str, Any]) -> str:
    parts: List[str] = []
    c = (msg.get("content") or "").strip()
    if c:
        parts.append(c)
    for emb in msg.get("embeds") or []:
        for k in ("title", "url", "description"):
            v = (emb.get(k) or "").strip()
            if v:
                parts.append(v)
        for fld in emb.get("fields") or []:
            v = (fld.get("value") or "").strip()
            if v:
                parts.append(v)
    return "\n".join(parts)


def extract_author_name(msg: Dict[str, Any]) -> str:
    author = msg.get("author") or {}
    return (author.get("global_name") or author.get("username") or "Unknown").strip()


def extract_version(title: str, text: str, file_name: str, timestamp: str) -> str:
    for src in (title, file_name, text):
        if not src:
            continue
        for rx in VERSION_PATTERNS:
            m = rx.search(src)
            if m:
                return m.group(1).strip()
    if timestamp and len(timestamp) >= 10:
        return timestamp[:10].replace("-", ".")
    return "1.0.0"


def safe_asset_name(prefix: str, filename: str) -> str:
    base = os.path.basename(filename or "file.bin")
    cleaned = re.sub(r"[^A-Za-z0-9._\-]+", "_", base).strip("._")
    if not cleaned:
        cleaned = "file.bin"
    name = f"{prefix}_{cleaned}"
    return name[:120]


def filename_from_url(url: str) -> str:
    if not url:
        return ""
    path = urllib.parse.urlsplit(url).path
    return os.path.basename(path)


def select_primary_asset(assets: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not assets:
        return None
    for ext in (".zip", ".dll", ".csproj"):
        for a in assets:
            if a["name"].lower().endswith(ext):
                return a
    return assets[0]


def fetch_forum_threads(guild_id: str, forum_id: str, discord_headers: Dict[str, str]) -> List[Dict[str, Any]]:
    threads_by_id: Dict[str, Dict[str, Any]] = {}
    ordered: List[str] = []

    def add_thread(th: Dict[str, Any]) -> None:
        tid = str(th.get("id") or "")
        parent = str(th.get("parent_id") or "")
        if not tid:
            return
        if parent and parent != forum_id:
            return
        if tid not in threads_by_id:
            threads_by_id[tid] = th
            ordered.append(tid)

    # 1. Активные треды на сервере
    st, data = http_json(f"{DISCORD_API}/guilds/{guild_id}/threads/active", headers=discord_headers)
    if st == 200 and isinstance(data, dict):
        for th in data.get("threads") or []:
            add_thread(th)
    else:
        print(f"[warn] active threads status={st}: {data}", file=sys.stderr)

    # 2. Архивные публичные треды форума (с пагинацией по before)
    before: Optional[str] = None
    for _ in range(15):
        qs = "limit=50"
        if before:
            qs += "&before=" + urllib.parse.quote(before)
        st, data = http_json(f"{DISCORD_API}/channels/{forum_id}/threads/archived/public?{qs}", headers=discord_headers)
        if st != 200 or not isinstance(data, dict):
            break
        batch = data.get("threads") or []
        if not batch:
            break
        for th in batch:
            add_thread(th)
        last_ts = ((batch[-1].get("thread_metadata") or {}).get("archive_timestamp")) or ""
        if not data.get("has_more") or not last_ts or last_ts == before:
            break
        before = last_ts

    return [threads_by_id[tid] for tid in ordered]


def fetch_channel_messages(channel_id: str, discord_headers: Dict[str, str], limit: int = 50) -> List[Dict[str, Any]]:
    st, data = http_json(f"{DISCORD_API}/channels/{channel_id}/messages?limit={limit}", headers=discord_headers)
    if st == 200 and isinstance(data, list):
        return data
    return []


class ReleaseMirror:
    """Зеркалирует вложения Discord в постоянные ассеты GitHub Release."""

    def __init__(self, repo: str, tag: str, gh_token: str) -> None:
        self.repo = repo
        self.tag = tag
        self.gh_token = gh_token
        self.release_id: Optional[int] = None
        self.upload_url_base: str = ""
        self.existing_assets: Dict[str, str] = {}
        self.enabled = bool(repo and gh_token)

    def ensure_release(self) -> None:
        if not self.enabled:
            return
        headers = {
            "Authorization": f"Bearer {self.gh_token}",
            "Accept": "application/vnd.github+json",
        }
        st, rel = http_json(f"{GITHUB_API}/repos/{self.repo}/releases/tags/{self.tag}", headers=headers)
        if st == 404:
            payload = {
                "tag_name": self.tag,
                "name": "Catalog Mirrored Assets",
                "body": "Автоматически зеркалированные архивы и превью модов из Discord-форума для Epic Mod Launcher.",
                "draft": False,
                "prerelease": False,
            }
            st, rel = http_json(f"{GITHUB_API}/repos/{self.repo}/releases", headers=headers, method="POST", body=payload)
        if st not in (200, 201) or not isinstance(rel, dict):
            print(f"[warn] GitHub Release unavailable (status={st}): {rel}", file=sys.stderr)
            self.enabled = False
            return

        self.release_id = int(rel["id"])
        self.upload_url_base = f"https://uploads.github.com/repos/{self.repo}/releases/{self.release_id}/assets"
        for a in rel.get("assets") or []:
            nm = str(a.get("name") or "")
            dl = str(a.get("browser_download_url") or "")
            if nm and dl:
                self.existing_assets[nm] = dl

    def mirror_url(self, src_url: str, asset_name: str) -> str:
        if not self.enabled or not src_url:
            return src_url
        if asset_name in self.existing_assets:
            return self.existing_assets[asset_name]

        try:
            raw = http_bytes(src_url, timeout=90)
        except Exception as e:
            print(f"[warn] download failed for {asset_name}: {e}", file=sys.stderr)
            return src_url

        if not raw or len(raw) > 95 * 1024 * 1024:
            return src_url

        upload_url = self.upload_url_base + "?name=" + urllib.parse.quote(asset_name)
        headers = {
            "Authorization": f"Bearer {self.gh_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/octet-stream",
        }
        st, res = http_json(upload_url, headers=headers, method="POST", body=raw, timeout=120)
        if st in (200, 201) and isinstance(res, dict) and res.get("browser_download_url"):
            dl = str(res["browser_download_url"])
            self.existing_assets[asset_name] = dl
            print(f"[mirror] uploaded {asset_name} ({len(raw)} bytes)")
            return dl

        print(f"[warn] upload failed for {asset_name} status={st}: {res}", file=sys.stderr)
        return src_url


def build_entry_from_thread(
    guild_id: str,
    thread: Dict[str, Any],
    messages_newest_first: List[Dict[str, Any]],
    mirror: Optional[ReleaseMirror] = None,
) -> Optional[Dict[str, Any]]:
    if not thread or not messages_newest_first:
        return None

    thread_id = str(thread.get("id") or "")
    thread_name = (thread.get("name") or "").strip() or f"Discord Thread {thread_id}"
    thread_url = f"https://discord.com/channels/{guild_id}/{thread_id}"

    assets: List[Dict[str, str]] = []
    seen_urls = set()
    github_repo = ""
    image_url = ""
    image_asset_name = ""
    newest_mod_ts = ""
    newest_mod_text = ""

    for msg in messages_newest_first:
        before_assets = len(assets)
        before_gh = github_repo
        for att in msg.get("attachments") or []:
            fn = (att.get("filename") or "").strip()
            att_id = str(att.get("id") or msg.get("id") or "0")
            url = (att.get("url") or att.get("proxy_url") or "").strip()
            if not url:
                continue
            low = fn.lower()
            if not image_url and low.endswith(IMG_EXTS):
                image_url = url
                image_asset_name = safe_asset_name(f"img_{thread_id}_{att_id}", fn)
                continue
            if low.endswith(MOD_EXTS) and url not in seen_urls:
                seen_urls.add(url)
                assets.append({
                    "name": fn,
                    "url": url,
                    "mirror_name": safe_asset_name(f"mod_{thread_id}_{att_id}", fn),
                })

        if not image_url:
            for emb in msg.get("embeds") or []:
                img = emb.get("image") or emb.get("thumbnail")
                if isinstance(img, dict):
                    u = (img.get("url") or img.get("proxy_url") or "").strip()
                    if u:
                        image_url = u
                        fn = filename_from_url(u) or "preview.png"
                        image_asset_name = safe_asset_name(f"img_{thread_id}", fn)
                        break

        text = gather_message_text(msg)
        for m in URL_RX.finditer(text):
            raw_url = m.group(0).rstrip(".,;)]>\"'")
            fn = filename_from_url(raw_url)
            if fn.lower().endswith(MOD_EXTS) and raw_url not in seen_urls:
                seen_urls.add(raw_url)
                assets.append({"name": fn, "url": raw_url, "mirror_name": ""})
                continue
            if not github_repo:
                gm = GH_REPO_RX.match(raw_url)
                if gm:
                    owner, repo = gm.group(1), gm.group(2).rstrip("/")
                    if owner.lower() != "bepinex":
                        github_repo = f"{owner}/{repo}"

        if (len(assets) > before_assets or (not before_gh and github_repo)) and not newest_mod_ts:
            newest_mod_ts = str(msg.get("timestamp") or "")
            newest_mod_text = str(msg.get("content") or "")

    if not assets and not github_repo:
        return None

    starter = messages_newest_first[-1]
    for m in messages_newest_first:
        if str(m.get("id") or "") == thread_id:
            starter = m
            break

    author = extract_author_name(starter)
    desc = clean_discord_text(gather_message_text(starter)) or clean_discord_text(gather_message_text(messages_newest_first[0]))
    if not newest_mod_ts:
        newest_mod_ts = str(messages_newest_first[0].get("timestamp") or "")

    primary = select_primary_asset(assets)
    version = extract_version(thread_name, newest_mod_text, primary["name"] if primary else "", newest_mod_ts)

    final_url = ""
    final_file = ""
    if primary:
        final_url = primary["url"]
        final_file = primary["name"]
        if mirror and mirror.enabled and primary.get("mirror_name"):
            final_url = mirror.mirror_url(final_url, primary["mirror_name"])

    if image_url and mirror and mirror.enabled and image_asset_name:
        if "cdn.discordapp.com" in image_url or "media.discordapp.net" in image_url:
            image_url = mirror.mirror_url(image_url, image_asset_name)

    entry: Dict[str, Any] = {
        "id": f"discord.{thread_id}",
        "name": thread_name,
        "version": version,
        "author": author,
        "description": desc,
        "threadUrl": thread_url,
    }
    if final_url:
        entry["url"] = final_url
    if github_repo:
        entry["github"] = github_repo
    if final_file:
        entry["file"] = final_file
    if image_url:
        entry["imageUrl"] = image_url
    return entry


def load_manual_entries(manual_path: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(manual_path):
        return []
    try:
        with open(manual_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("mods"), list):
            return [x for x in data["mods"] if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception as e:
        print(f"[warn] failed reading {manual_path}: {e}", file=sys.stderr)
    return []


def run_selftest() -> int:
    mock_thread = {"id": "156603999111222", "name": "Epic Mod v0.4.0"}
    mock_msgs = [
        {
            "id": "156604100000001",
            "timestamp": "2026-11-19T20:00:00Z",
            "content": "Обновление v0.4.0! Исправлен пистолет.",
            "author": {"global_name": "AzureLalik", "username": "azure"},
            "attachments": [
                {
                    "id": "9001",
                    "filename": "EpicModFix4.zip",
                    "url": "https://cdn.discordapp.com/attachments/1/2/EpicModFix4.zip",
                }
            ],
            "embeds": [],
        },
        {
            "id": "156603999111222",
            "timestamp": "2026-11-18T12:00:00Z",
            "content": "Привет! <@12345> Основной пост мода.",
            "author": {"global_name": "Kenatan", "username": "kenatan"},
            "attachments": [
                {
                    "id": "8001",
                    "filename": "preview.png",
                    "url": "https://cdn.discordapp.com/attachments/1/2/preview.png",
                }
            ],
            "embeds": [],
        },
    ]

    entry = build_entry_from_thread("1566032190062727399", mock_thread, mock_msgs, mirror=None)
    assert entry is not None, "Expected parsed entry"
    assert entry["id"] == "discord.156603999111222"
    assert entry["version"] == "0.4.0", f"Unexpected version {entry['version']}"
    assert entry["author"] == "Kenatan"
    assert entry["file"] == "EpicModFix4.zip"
    assert entry["url"].endswith("EpicModFix4.zip")
    assert entry["imageUrl"].endswith("preview.png")
    assert "<@12345>" not in entry["description"]
    payload = {"name": "Каталог модов Epic Mod Launcher", "mods": [entry]}
    dumped = json.dumps(payload, ensure_ascii=False, indent=2)
    reloaded = json.loads(dumped)
    assert len(reloaded["mods"]) == 1
    print("SELFTEST_OK")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return run_selftest()

    bot_token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not bot_token:
        print("[skip] DISCORD_BOT_TOKEN is not set in GitHub Secrets; catalog.json left unchanged.")
        return 0

    guild_id = (os.environ.get("DISCORD_GUILD_ID") or "1566032190062727399").strip()
    forum_id = (os.environ.get("DISCORD_FORUM_CHANNEL_ID") or "1566035062443610122").strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "AzureLalik/EpicModLauncher").strip()
    gh_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    release_tag = (os.environ.get("ASSET_RELEASE_TAG") or "catalog-assets").strip()

    auth_val = bot_token if bot_token.lower().startswith("bot ") else f"Bot {bot_token}"
    discord_headers = {"Authorization": auth_val}

    mirror = ReleaseMirror(repo, release_tag, gh_token)
    mirror.ensure_release()

    entries: List[Dict[str, Any]] = []
    seen_ids = set()

    threads = fetch_forum_threads(guild_id, forum_id, discord_headers)
    print(f"[sync] found {len(threads)} forum threads in {forum_id}")
    for th in threads:
        tid = str(th.get("id") or "")
        msgs = fetch_channel_messages(tid, discord_headers, limit=50)
        entry = build_entry_from_thread(guild_id, th, msgs, mirror=mirror)
        if entry and entry["id"] not in seen_ids:
            seen_ids.add(entry["id"])
            entries.append(entry)

    # Ручные записи из catalog_manual.json имеют приоритет или дополняют каталог
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for m_entry in load_manual_entries(os.path.join(root_dir, "catalog_manual.json")):
        mid = str(m_entry.get("id") or m_entry.get("name") or "")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            entries.append(m_entry)

    out_path = os.path.join(root_dir, "catalog.json")
    if len(entries) == 0:
        print("[warn] 0 entries collected; keeping existing catalog.json untouched.", file=sys.stderr)
        return 0

    payload = {
        "name": "Каталог модов Epic Mod Launcher",
        "mods": entries,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[sync] wrote {len(entries)} mods to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
