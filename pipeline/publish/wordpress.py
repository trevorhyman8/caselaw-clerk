"""WordPress.com publisher.

VERIFIED (2026-08-02) auth path — standard self-hosted WP `wp/v2`
Application Passwords do NOT work on WordPress.com
(`wp_is_application_passwords_available()` excludes WP.com environments).
The working path is WordPress.com's own OAuth2 gateway:

  1. One-time (SETUP.md, [HUMAN: SCOTT]): create a developer app at
     https://developer.wordpress.com/apps/ -> WPCOM_CLIENT_ID/SECRET.
     Create an account-level Application Password at
     https://wordpress.com/me/security (works alongside 2FA) -> pasted once
     into setup.
  2. Token exchange (this module, automatic): POST
     https://public-api.wordpress.com/oauth2/token with grant_type=password
     -> a bearer token. Auto re-exchanged on a 401 so a revoked/rotated
     token self-heals as long as the application password still stands.
  3. Site ID: GET public-api.wordpress.com/rest/v1.1/sites/{domain} (public,
     no auth needed) -> numeric site ID, the reliable form for the v2 posts
     endpoint.
  4. Drafts: POST public-api.wordpress.com/wp/v2/sites/{site_id}/posts with
     status="draft". This is the ONLY write this module performs without an
     explicit publish() call, and publish() itself is gated by the caller
     (pipeline/notify/state_machine.py) on Scott's two-step typed confirm —
     this module has no opinion about approval, it just executes what it's
     told, so the approval logic lives in exactly one place.

NOT LIVE-TESTED YET: needs Scott's WPCOM_CLIENT_ID/SECRET + application
password, which is a [HUMAN: SCOTT] step gated on his Phase 4 approval of
the whole system (see plan). Code is complete and the V4 gate
(verify/v4_wordpress.py) is ready to run the moment those secrets exist —
create a draft, read it back, delete it, on the LIVE site.
"""
from __future__ import annotations

import httpx

from pipeline.settings import settings

TOKEN_URL = "https://public-api.wordpress.com/oauth2/token"
API_BASE = "https://public-api.wordpress.com"


class WordPressAuthError(RuntimeError):
    pass


def exchange_password_for_token(username: str, application_password: str) -> str:
    if not (settings.wpcom_client_id and settings.wpcom_client_secret):
        raise WordPressAuthError(
            "WPCOM_CLIENT_ID / WPCOM_CLIENT_SECRET not set — create a developer app "
            "at https://developer.wordpress.com/apps/ first (see SETUP.md Phase 5)."
        )
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.wpcom_client_id,
            "client_secret": settings.wpcom_client_secret,
            "grant_type": "password",
            "username": username,
            "password": application_password,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise WordPressAuthError(f"token exchange failed ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def resolve_site_id(domain: str) -> int:
    r = httpx.get(f"{API_BASE}/rest/v1.1/sites/{domain}", timeout=30)
    r.raise_for_status()
    return r.json()["ID"]


class WordPressClient:
    def __init__(self, site_id: str | int | None = None, token: str | None = None):
        self.site_id = site_id or settings.wpcom_site_id
        self.token = token or settings.wpcom_access_token
        if not (self.site_id and self.token):
            raise WordPressAuthError(
                "WPCOM_SITE_ID / WPCOM_ACCESS_TOKEN not configured — run SETUP.md "
                "Phase 5 first. This client refuses to guess at defaults for a "
                "law firm's live WordPress site."
            )
        self._client = httpx.Client(
            base_url=f"{API_BASE}/wp/v2/sites/{self.site_id}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        r = self._client.request(method, path, **kwargs)
        if r.status_code == 401:
            raise WordPressAuthError(
                "WordPress token rejected (401) — token may have been revoked. "
                "Re-run the token exchange (SETUP.md Phase 5 step 2) with the "
                "stored application password."
            )
        r.raise_for_status()
        return r

    def list_categories(self) -> dict[str, int]:
        r = self._request("GET", "/categories", params={"per_page": 100})
        return {c["name"]: c["id"] for c in r.json()}

    def create_category(self, name: str) -> int:
        r = self._request("POST", "/categories", json={"name": name})
        return r.json()["id"]

    def resolve_category_ids(self, names: list[str], create_missing: bool = False) -> list[int]:
        existing = self.list_categories()
        ids = []
        for name in names:
            if name in existing:
                ids.append(existing[name])
            elif create_missing:
                ids.append(self.create_category(name))
            # else: silently skip — a category-vocabulary mismatch should be
            # caught at the frozen-vocabulary layer (pipeline/draft), not here
        return ids

    def create_draft(self, title: str, content_html: str, excerpt: str, category_ids: list[int]) -> dict:
        r = self._request(
            "POST", "/posts",
            json={
                "title": title, "content": content_html, "excerpt": excerpt,
                "status": "draft", "categories": category_ids,
            },
        )
        return r.json()

    def get_post(self, post_id: int) -> dict:
        r = self._request("GET", f"/posts/{post_id}")
        return r.json()

    def delete_post(self, post_id: int, force: bool = True) -> None:
        self._request("DELETE", f"/posts/{post_id}", params={"force": force})

    def publish(self, post_id: int) -> dict:
        """The only call that flips a draft live. Callers MUST have already
        confirmed Scott's explicit two-step approval (see
        pipeline/notify/state_machine.py) — this method does not check
        anything itself; it trusts the caller because approval logic
        belongs in exactly one place, not duplicated here."""
        r = self._request("POST", f"/posts/{post_id}", json={"status": "publish"})
        return r.json()
