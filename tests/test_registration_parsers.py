import unittest

from parsers import (
    GmailLine,
    belongs_to_root_gmail,
    normalize_gmail_for_ownership,
    parse_gmails,
    parse_registration_gmails,
)


class RegistrationParsersTests(unittest.TestCase):
    def test_parse_gmails_keeps_existing_behavior(self):
        raw = "\n a.b@gmail.com \n\nA.B@gmail.com\nxy.z@gmail.com\nbad-email\n"
        gmails, errors = parse_gmails(raw)
        self.assertEqual(gmails, ["a.b@gmail.com", "xy.z@gmail.com"])
        self.assertEqual(errors, ["Dòng 6: 'bad-email' không đúng định dạng email."])

    def test_ownership_only_strips_dots_for_gmail_com(self):
        self.assertEqual(normalize_gmail_for_ownership("A.B.C@gmail.com"), "abc@gmail.com")
        self.assertEqual(normalize_gmail_for_ownership("a.b@company.com"), "a.b@company.com")
        self.assertTrue(belongs_to_root_gmail("u.s.er@gmail.com", "user@gmail.com"))
        self.assertTrue(belongs_to_root_gmail("user@gmail.com", "u.ser@gmail.com"))
        self.assertFalse(belongs_to_root_gmail("other@gmail.com", "user@gmail.com"))
        self.assertFalse(belongs_to_root_gmail("u.ser@company.com", "user@company.com"))
        self.assertFalse(belongs_to_root_gmail("user@googlemail.com", "user@gmail.com"))

    def test_parse_registration_gmails_preserves_dots_order_dedup_and_line_numbers(self):
        raw = (
            "\n"
            "  my.test@gmail.com  \n"
            "\n"
            "MY.TEST@gmail.com\n"
            "bad_format\n"
            "other.box@gmail.com\n"
            "m.y.test@gmail.com\n"
            "my..test@gmail.com\n"
        )
        entries, errors = parse_registration_gmails(raw, root_gmail="mytest@gmail.com")
        self.assertEqual(
            entries,
            [
                GmailLine(line_no=2, email="my.test@gmail.com"),
                GmailLine(line_no=7, email="m.y.test@gmail.com"),
            ],
        )
        self.assertEqual(
            errors,
            [
                "Dòng 5: 'bad_format' không đúng định dạng email.",
                "Dòng 6: 'other.box@gmail.com' không thuộc hộp thư Gmail gốc (mytest@gmail.com).",
                "Dòng 8: 'my..test@gmail.com' có dấu chấm không hợp lệ trong tên email.",
            ],
        )

    def test_invalid_root_gmail_reports_error(self):
        entries, errors = parse_registration_gmails("a.b@gmail.com", root_gmail="not-an-email")
        self.assertEqual(entries, [])
        self.assertEqual(errors, ["Gmail gốc không đúng định dạng email."])


if __name__ == "__main__":
    unittest.main()
