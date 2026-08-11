#!/usr/bin/env python3
"""Monitor GitHub releases and send notifications to a Feishu bot."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_CONFIG_PATH = "config/repositories.json"
DEFAULT_STATE_PATH = "state/releases.json"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class MonitorError(RuntimeError):
    """An expected error that should be shown without a traceback."""


@dataclass(frozen=True)
class RepositoryConfig:
    repo: str
    name: str
    include_prereleases: bool = False


@dataclass(frozen=True)
class MonitorConfig:
    repositories: list[RepositoryConfig]
    notify_on_first_run: bool = False


def config_fingerprint(config: MonitorConfig) -> str:
    """Return a stable identifier for the set of monitored repositories."""
    normalized = {
        "repositories": sorted(
            (
                {
                    "repo": repository.repo.lower(),
                    "include_prereleases": repository.include_prereleases,
                }
                for repository in config.repositories
            ),
            key=lambda repository: repository["repo"],
        )
    }
    content = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_json(path: Path, *, description: str) -> Any:
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError as error:
        raise MonitorError(f"{description}不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise MonitorError(
            f"{description}不是有效的 JSON：{path}:{error.lineno}:{error.colno}"
        ) from error


def load_config(path: Path) -> MonitorConfig:
    raw = load_json(path, description="配置文件")
    if not isinstance(raw, dict):
        raise MonitorError("配置文件的顶层必须是 JSON 对象")

    raw_repositories = raw.get("repositories")
    if not isinstance(raw_repositories, list):
        raise MonitorError("配置项 repositories 必须是数组")

    notify_on_first_run = raw.get("notify_on_first_run", False)
    if not isinstance(notify_on_first_run, bool):
        raise MonitorError("配置项 notify_on_first_run 必须是布尔值")

    repositories: list[RepositoryConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_repositories):
        location = f"repositories[{index}]"
        if isinstance(item, str):
            repo = item.strip()
            name = repo
            include_prereleases = False
        elif isinstance(item, dict):
            repo_value = item.get("repo")
            if not isinstance(repo_value, str):
                raise MonitorError(f"{location}.repo 必须是字符串")
            repo = repo_value.strip()

            name_value = item.get("name", repo)
            if not isinstance(name_value, str) or not name_value.strip():
                raise MonitorError(f"{location}.name 必须是非空字符串")
            name = name_value.strip()

            include_prereleases = item.get("include_prereleases", False)
            if not isinstance(include_prereleases, bool):
                raise MonitorError(f"{location}.include_prereleases 必须是布尔值")
        else:
            raise MonitorError(f"{location} 必须是仓库字符串或配置对象")

        if not REPOSITORY_PATTERN.fullmatch(repo):
            raise MonitorError(f"{location}.repo 格式错误，应为 owner/repository：{repo!r}")

        repo_key = repo.lower()
        if repo_key in seen:
            raise MonitorError(f"仓库配置重复：{repo}")
        seen.add(repo_key)
        repositories.append(RepositoryConfig(repo, name, include_prereleases))

    return MonitorConfig(repositories, notify_on_first_run)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "repositories": {}}
    raw = load_json(path, description="状态文件")
    if not isinstance(raw, dict) or not isinstance(raw.get("repositories"), dict):
        raise MonitorError("状态文件格式错误：repositories 必须是对象")
    return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = 1
    state["updated_at"] = utc_now()
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            file.write(content)
            temp_path = file.name
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 20,
    attempts: int = 3,
    error_target: str | None = None,
) -> Any:
    safe_target = error_target or url
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=utf-8")

    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            if error.code not in RETRYABLE_STATUS_CODES or attempt == attempts - 1:
                detail = response_body[:500].strip()
                raise MonitorError(
                    f"HTTP {error.code} 请求失败：{safe_target}"
                    + (f"；{detail}" if detail else "")
                ) from error
            last_error = error
        except (URLError, TimeoutError) as error:
            if attempt == attempts - 1:
                raise MonitorError(f"网络请求失败：{safe_target}；{error}") from error
            last_error = error
        except json.JSONDecodeError as error:
            raise MonitorError(f"接口返回了无效 JSON：{safe_target}") from error

        time.sleep(2**attempt)

    raise MonitorError(f"网络请求失败：{safe_target}；{last_error}")


class GitHubClient:
    def __init__(self, token: str | None = None, api_url: str = DEFAULT_GITHUB_API_URL):
        self.api_url = api_url.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-release-monitor",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def latest_release(self, repository: RepositoryConfig) -> dict[str, Any] | None:
        repo_path = quote(repository.repo, safe="/")
        url = f"{self.api_url}/repos/{repo_path}/releases?per_page=100"
        releases = request_json(url, headers=self.headers)
        if not isinstance(releases, list):
            raise MonitorError(f"GitHub API 返回格式异常：{repository.repo}")

        candidates = [
            release
            for release in releases
            if isinstance(release, dict)
            and not release.get("draft", False)
            and (repository.include_prereleases or not release.get("prerelease", False))
        ]
        if not candidates:
            return None

        return max(
            candidates,
            key=lambda release: parse_timestamp(
                release.get("published_at") or release.get("created_at")
            )
            or datetime.min.replace(tzinfo=timezone.utc),
        )


def release_state(release: dict[str, Any]) -> dict[str, Any]:
    return {
        "release_id": release.get("id"),
        "tag_name": release.get("tag_name", ""),
        "name": release.get("name") or "",
        "published_at": release.get("published_at") or release.get("created_at") or "",
        "url": release.get("html_url") or "",
        "prerelease": bool(release.get("prerelease", False)),
        "recorded_at": utc_now(),
    }


def same_release(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    current_id = current.get("id")
    previous_id = previous.get("release_id")
    if current_id is not None and previous_id is not None:
        return current_id == previous_id
    return bool(current.get("tag_name")) and current.get("tag_name") == previous.get("tag_name")


def is_newer_release(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if same_release(current, previous):
        return False

    current_time = parse_timestamp(current.get("published_at") or current.get("created_at"))
    previous_time = parse_timestamp(previous.get("published_at"))
    if current_time and previous_time and current_time != previous_time:
        return current_time > previous_time

    current_id = current.get("id")
    previous_id = previous.get("release_id")
    if isinstance(current_id, int) and isinstance(previous_id, int):
        return current_id > previous_id

    return current.get("tag_name") != previous.get("tag_name")


def truncate_release_notes(body: Any, limit: int = 1200) -> str:
    if not isinstance(body, str) or not body.strip():
        return "本次发布没有提供更新说明。"
    normalized = body.strip().replace("\r\n", "\n")
    # Feishu uses <at ...> as a mention token. Release notes are controlled by
    # the monitored repository and must not be able to mention a chat member.
    normalized = re.sub(r"<\s*at(?=[\s>])", "&lt;at", normalized, flags=re.IGNORECASE)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def build_feishu_card(repository: RepositoryConfig, release: dict[str, Any]) -> dict[str, Any]:
    tag = str(release.get("tag_name") or "未知版本")
    title = str(release.get("name") or tag)
    published_at = str(release.get("published_at") or release.get("created_at") or "未知")
    release_type = "预发布版本" if release.get("prerelease") else "正式版本"
    release_url = str(release.get("html_url") or f"https://github.com/{repository.repo}/releases")

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange" if release.get("prerelease") else "green",
                "title": {"tag": "plain_text", "content": f"{repository.name} 发布了 {tag}"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**仓库：** {repository.repo}\n"
                            f"**版本：** {title}\n"
                            f"**类型：** {release_type}\n"
                            f"**发布时间：** {published_at}"
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**更新说明**\n{truncate_release_notes(release.get('body'))}",
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "查看 GitHub Release"},
                            "url": release_url,
                        }
                    ],
                },
            ],
        },
    }


def feishu_signature(secret: str, timestamp: int) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def send_feishu_notification(
    webhook_url: str,
    repository: RepositoryConfig,
    release: dict[str, Any],
    *,
    webhook_secret: str | None = None,
) -> None:
    payload = build_feishu_card(repository, release)
    if webhook_secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = feishu_signature(webhook_secret, timestamp)

    response = request_json(
        webhook_url,
        method="POST",
        payload=payload,
        error_target="飞书机器人 Webhook",
    )
    if not isinstance(response, dict):
        raise MonitorError("飞书机器人返回格式异常")

    if "code" in response:
        code = response["code"]
    elif "StatusCode" in response:
        code = response["StatusCode"]
    else:
        raise MonitorError("飞书机器人响应中缺少状态码")

    if code not in (0, "0"):
        message = response.get("msg") or response.get("StatusMessage") or "未知错误"
        raise MonitorError(f"飞书机器人发送失败（code={code}）：{message}")


def monitor(
    config: MonitorConfig,
    state: dict[str, Any],
    *,
    github_client: GitHubClient,
    webhook_url: str | None,
    webhook_secret: str | None,
    force_notify: bool = False,
    dry_run: bool = False,
) -> tuple[bool, list[str]]:
    stored_states = state.setdefault("repositories", {})
    expected_fingerprint = config_fingerprint(config)
    stored_fingerprint = state.get("config_fingerprint")
    configured_repositories = {repository.repo.lower() for repository in config.repositories}
    stored_repositories = {str(repository).lower() for repository in stored_states}

    # Older state files do not have a fingerprint. If their repository keys do
    # not match the current config, treat them as data from another deployment.
    legacy_config_changed = (
        stored_fingerprint is None and stored_repositories != configured_repositories
    )
    configuration_changed = (
        (stored_fingerprint is not None and stored_fingerprint != expected_fingerprint)
        or legacy_config_changed
    )
    repository_states = {} if configuration_changed else stored_states
    changed = False
    errors: list[str] = []

    if configuration_changed:
        print("检测到监控仓库配置变化，将清理旧状态并重新建立基线")
    if not dry_run:
        if configuration_changed:
            state["repositories"] = repository_states
            changed = True
        if stored_fingerprint != expected_fingerprint:
            state["config_fingerprint"] = expected_fingerprint
            changed = True

    for repository in config.repositories:
        try:
            print(f"检查 {repository.repo} ...", flush=True)
            release = github_client.latest_release(repository)
            if release is None:
                print("  未找到符合条件的 Release")
                continue

            tag = release.get("tag_name") or "未知版本"
            previous = repository_states.get(repository.repo)
            if not isinstance(previous, dict):
                previous = None

            if previous and same_release(release, previous) and not force_notify:
                print(f"  已是最新版本：{tag}")
                continue

            if previous and not force_notify and not is_newer_release(release, previous):
                print(f"  当前版本 {tag} 不晚于已记录版本，保留原状态")
                continue

            first_run = previous is None
            should_notify = force_notify or not first_run or config.notify_on_first_run

            if should_notify:
                if dry_run:
                    print(f"  [dry-run] 将发送飞书通知：{tag}")
                    continue
                if not webhook_url:
                    raise MonitorError("需要发送通知，但未设置 FEISHU_WEBHOOK_URL")
                send_feishu_notification(
                    webhook_url,
                    repository,
                    release,
                    webhook_secret=webhook_secret,
                )
                print(f"  已发送飞书通知：{tag}")
            else:
                print(f"  首次记录基线版本，不发送通知：{tag}")

            if not dry_run:
                repository_states[repository.repo] = release_state(release)
                changed = True
        except MonitorError as error:
            message = f"{repository.repo}: {error}"
            errors.append(message)
            print(f"  错误：{error}", file=sys.stderr)

    return changed, errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监控多个 GitHub 仓库的新 Release，并通过飞书通知")
    parser.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG_PATH), help="仓库配置文件")
    parser.add_argument("--state", type=Path, default=Path(DEFAULT_STATE_PATH), help="状态文件")
    parser.add_argument("--force-notify", action="store_true", help="忽略状态并通知各仓库当前版本")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不通知且不写入状态")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        state = load_state(args.state)
        github_client = GitHubClient(
            token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
            api_url=os.environ.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL),
        )
        changed, errors = monitor(
            config,
            state,
            github_client=github_client,
            webhook_url=os.environ.get("FEISHU_WEBHOOK_URL"),
            webhook_secret=os.environ.get("FEISHU_WEBHOOK_SECRET"),
            force_notify=args.force_notify,
            dry_run=args.dry_run,
        )
        if changed:
            save_state(args.state, state)
            print(f"状态已更新：{args.state}")
        elif args.dry_run:
            print("dry-run 完成，未发送通知或修改状态")
        else:
            print("没有状态变更")

        if errors:
            print(f"检查完成，但有 {len(errors)} 个仓库失败：", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        return 0
    except MonitorError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
