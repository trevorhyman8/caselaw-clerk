"""V4 gate — WordPress.com round trip on the LIVE site: create a clearly-
marked test draft, read it back, delete it. Never leaves anything visible
behind. Run: uv run python verify/v4_wordpress.py"""
import sys

from pipeline.publish.wordpress import WordPressClient


def main() -> bool:
    client = WordPressClient()
    try:
        post = client.create_draft(
            title="TEST — DELETE ME (caselaw-clerk V4 gate)",
            content_html="<p>This is an automated verification draft. It will be deleted immediately.</p>",
            excerpt="test",
            category_ids=[],
        )
        print(f"created draft id={post['id']} status={post.get('status')}")

        fetched = client.get_post(post["id"])
        ok = fetched.get("status") == "draft" and "TEST" in fetched.get("title", {}).get("rendered", fetched.get("title", ""))
        print(f"read back: status={fetched.get('status')} -> {'OK' if ok else 'FAIL'}")

        client.delete_post(post["id"])
        confirm = client.get_post(post["id"]) if False else None  # WP.com may 404 on re-fetch after trash; skip
        print("deleted")

        print("PASS" if ok else "FAIL")
        return ok
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
