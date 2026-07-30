from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

import alphalens_brief as brief


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class AlphaLensBriefTests(unittest.TestCase):
    def test_fetch_uses_bearer_token_and_returns_text(self) -> None:
        seen: dict[str, str] = {}

        def open_request(request, timeout):
            seen["authorization"] = request.get_header("Authorization")
            seen["timeout"] = str(timeout)
            body = json.dumps(
                {"text": "AlphaLens summary", "computedAt": "2026-07-25T12:00:00Z"}
            ).encode()
            return _Response(body)

        payload = brief.fetch_brief(
            "https://alphalens.app/api/brief/digest",
            "top-secret",
            open_request=open_request,
        )

        self.assertEqual(payload["text"], "AlphaLens summary")
        self.assertEqual(seen["authorization"], "Bearer top-secret")
        self.assertEqual(seen["timeout"], "20.0")

    def test_fetch_rejects_redirect_without_reading_body(self) -> None:
        def open_request(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

        with self.assertRaisesRegex(brief.AlphaLensBriefError, "HTTP 302"):
            brief.fetch_brief(
                "https://alphalens.app/api/brief/digest",
                "secret",
                open_request=open_request,
            )

    def test_prompt_context_requires_both_report_and_wechat_integration(self) -> None:
        context = brief.format_prompt_context(
            {
                "text": "Headline\n个股焦点\n• $NVDA sample",
                "computedAt": "2026-07-25T12:00:00Z",
                "staleMs": 1000,
                "url": "https://alphalens.app/brief",
            }
        )

        self.assertIn("AlphaLens Brief 页面摘要", context)
        self.assertIn("微信速读", context)
        self.assertIn("alphalens_brief", context)
        self.assertIn("280", context)
        self.assertNotIn("top-secret", context)

    def test_settings_never_require_token_on_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.json"
            path.write_text(
                json.dumps(
                    {
                        "alphalensBriefUrl": "https://alphalens.app/api/brief/digest",
                        "alphalensBriefToken": "from-file",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                brief.load_settings(path),
                ("https://alphalens.app/api/brief/digest", "from-file"),
            )


if __name__ == "__main__":
    unittest.main()
