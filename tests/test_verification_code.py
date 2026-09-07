import unittest
from unittest.mock import patch

from providers.common import ProviderContext, extract_verification_code, normalize_verification_code
from providers.gptmail import GPTMailProvider


class VerificationCodeParserTests(unittest.TestCase):
    def test_common_xai_formats(self):
        self.assertEqual(
            extract_verification_code("Your verification code is 482731."),
            "482731",
        )
        self.assertEqual(
            extract_verification_code("Security code: 482-731"),
            "482731",
        )
        self.assertEqual(
            extract_verification_code("Your one-time code is A7C-219"),
            "A7C219",
        )
        self.assertEqual(
            extract_verification_code("xAI confirmation code: GEU-TAB"),
            "GEUTAB",
        )
        self.assertEqual(
            extract_verification_code("Copy 482731 to continue"),
            "482731",
        )
        noisy_xai_body = (
            "GEU-TAB xAI confirmation code noreply@x.ai 96Validate "
            "96 Validate your email. Please use the code below. GEU-TAB"
        )
        self.assertEqual(extract_verification_code(noisy_xai_body), "GEUTAB")

    def test_input_normalization_removes_separators(self):
        self.assertEqual(normalize_verification_code("GEU-TAB"), "GEUTAB")
        self.assertEqual(normalize_verification_code("482 731"), "482731")

    def test_html_nodes_are_joined_before_parsing(self):
        body = "<p>Your verification code is <b>482</b><span>731</span>.</p>"
        self.assertEqual(extract_verification_code(body), "482731")

    def test_unrelated_page_numbers_are_not_codes(self):
        self.assertIsNone(extract_verification_code("Inbox 0 Page 1 Message ID 999999"))
        self.assertEqual(
            extract_verification_code(
                "Message ID 123456. Your verification code is 482731."
            ),
            "482731",
        )


class GPTMailPageExtractionTests(unittest.TestCase):
    def test_target_mail_is_selected_before_code_parsing(self):
        candidates = [
            {
                "sender": "newsletter@example.com",
                "subject": "Your verification code",
                "preview": "Your verification code is 111111",
                "text": "newsletter@example.com Your verification code 111111",
            },
            {
                "sender": "X <noreply@x.com>",
                "subject": "Verify your email",
                "preview": "Your verification code is 482731",
                "text": "X <noreply@x.com> Verify your email Your verification code is 482731",
            },
        ]
        target, ranked = GPTMailProvider._select_target_candidate(candidates)
        self.assertIs(target, candidates[1])
        self.assertGreater(ranked[0][0], ranked[1][0])

    def test_ambiguous_generic_verification_mail_is_not_guessed(self):
        candidates = [
            {"subject": "Verification code", "text": "Verification code 111111"},
            {"subject": "Security code", "text": "Security code 222222"},
        ]
        target, _ = GPTMailProvider._select_target_candidate(candidates)
        self.assertIsNone(target)

    def test_single_generic_verification_mail_is_supported(self):
        target, _ = GPTMailProvider._select_target_candidate(
            [{"subject": "Verify your email", "text": "Your code is 482731"}]
        )
        self.assertIsNotNone(target)

    def test_detail_is_scoped_to_target_message(self):
        context = ProviderContext(
            config={},
            http_get=lambda *args, **kwargs: None,
            http_post=lambda *args, **kwargs: None,
            raise_if_cancelled=lambda *args, **kwargs: None,
            sleep_with_cancel=lambda *args, **kwargs: None,
        )
        provider = GPTMailProvider(lambda: context, lambda: None)
        provider.tab = object()
        provider.address = "codexextract@example.test"

        snapshots = [
            {
                "body_text": "Inbox 0 Page 1 Message ID 999999",
                "text": "xAI Grok verification email",
                "detail_text": "",
                "candidates": [{"text": "xAI Grok | Verify your email", "id": "1"}],
            },
            {
                "body_text": "Inbox 1 Page 1 Message ID 999999",
                "text": "xAI Grok | Verify your email",
                "detail_text": "Your verification code is 482731.",
                "candidates": [{"text": "xAI Grok | Verify your email", "id": "1"}],
            },
        ]
        with patch.object(
            provider,
            "_page_state",
            return_value={"email": provider.address, "url": "https://mail.chatgpt.org.uk/"},
        ), patch.object(provider, "_click_refresh", return_value=False), patch.object(
            provider, "_scan_inbox", side_effect=snapshots
        ), patch.object(provider, "_click_message", return_value=True), patch.object(
            provider, "_close_message_detail"
        ):
            self.assertEqual(provider.read_code_from_page(), "482731")

    def test_realistic_xai_card_returns_geu_tab(self):
        context = ProviderContext(
            config={},
            http_get=lambda *args, **kwargs: None,
            http_post=lambda *args, **kwargs: None,
            raise_if_cancelled=lambda *args, **kwargs: None,
            sleep_with_cancel=lambda *args, **kwargs: None,
        )
        provider = GPTMailProvider(lambda: context, lambda: None)
        provider.tab = object()
        provider.address = "codexextract@example.test"
        card_text = (
            "GEU-TAB xAI confirmation code noreply@x.ai 96Validate "
            "96 Validate your email. Please use the code below. GEU-TAB"
        )
        snapshot = {"text": card_text, "detail_text": "", "candidates": [{"text": card_text}]}
        with patch.object(
            provider,
            "_page_state",
            return_value={"email": provider.address, "url": "https://mail.chatgpt.org.uk/"},
        ), patch.object(provider, "_click_refresh", return_value=False), patch.object(
            provider, "_scan_inbox", return_value=snapshot
        ):
            self.assertEqual(provider.read_code_from_page(), "GEUTAB")


if __name__ == "__main__":
    unittest.main()
