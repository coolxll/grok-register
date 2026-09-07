"""Regression coverage for the local integrations kept on top of upstream."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app_config
import cpa_export
import mail_service
from gptmail_provider import GPTMailProvider


class ExtraMailProviderTests(unittest.TestCase):
    def test_extra_provider_names_are_valid(self):
        for provider in ("freemail", "mailtm", "gptmail"):
            cfg = dict(app_config.DEFAULT_CONFIG)
            cfg["email_provider"] = provider
            if provider == "freemail":
                cfg["freemail_api_base"] = "https://mail.example.test"
            if provider == "gptmail":
                cfg["multi_thread_enabled"] = False
            self.assertEqual(app_config.validate_run_requirements(cfg)["email_provider"], provider)

    def test_flexible_code_parser_handles_grouped_xai_code(self):
        self.assertEqual(
            mail_service.extract_flexible_verification_code(
                "xAI confirmation code: GEU-TAB. Validate your email."
            ),
            "GEUTAB",
        )
        self.assertEqual(
            mail_service.extract_flexible_verification_code(
                "Your verification code is <b>482</b><span>731</span>."
            ),
            "482731",
        )

    def test_freemail_uses_address_as_mailbox_identity(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"email": "demo@example.test"}
        original = dict(mail_service.config)
        try:
            mail_service.config.clear()
            mail_service.config.update({
                "freemail_api_base": "https://mail.example.test",
                "freemail_jwt": "token",
            })
            with patch.object(mail_service, "http_get", return_value=response, create=True) as mocked:
                self.assertEqual(
                    mail_service.freemail_get_email_and_token(),
                    ("demo@example.test", "freemail"),
                )
            headers = mocked.call_args.kwargs["headers"]
            self.assertEqual(headers["Authorization"], "Bearer token")
        finally:
            mail_service.config.clear()
            mail_service.config.update(original)

    def test_gptmail_prefers_xai_candidate(self):
        generic = {
            "sender": "newsletter@example.test",
            "subject": "Verification code",
            "text": "Verification code 111111",
        }
        xai = {
            "sender": "X <noreply@x.com>",
            "subject": "Verify your email",
            "text": "xAI Verify your email verification code 482731",
        }
        target, ranked = GPTMailProvider._select_target_candidate([generic, xai])
        self.assertIs(target, xai)
        self.assertGreater(ranked[0][0], ranked[1][0])


class CliProxyUploadTests(unittest.TestCase):
    def test_cliproxy_upload_reuses_cpa_json(self):
        cfg = dict(app_config.DEFAULT_CONFIG)
        cfg.update({
            "cliproxyapi_auto_add": True,
            "cliproxyapi_remote_base": "https://cpa.example.test",
            "cliproxyapi_management_key": "secret",
        })
        settings = cpa_export.CpaExportSettings.from_config(cfg)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "xai-test.json"
            payload = {"type": "xai", "access_token": "a", "refresh_token": "r"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            response = Mock()
            response.raise_for_status.return_value = None
            with patch.object(cpa_export.requests, "post", return_value=response) as mocked:
                self.assertEqual(cpa_export._upload_to_cliproxyapi(path, settings), "xai-test.json")
            args, kwargs = mocked.call_args
            self.assertIn("/v0/management/auth-files?name=xai-test.json", args[0])
            self.assertEqual(kwargs["json"], payload)
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
