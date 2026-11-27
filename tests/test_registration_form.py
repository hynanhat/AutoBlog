from __future__ import annotations

import unittest

from playwright.async_api import async_playwright

from registration_form import (
    RegistrationFormConfig,
    RegistrationFormError,
    STATUS_FORM_EXPIRED,
    STATUS_FORM_INVALID,
    STATUS_WRONG_ID,
    capture_captcha_image,
    fill_confirmation_form,
    inspect_registration_submission_state,
    prepare_confirmation_form,
    refresh_captcha_image,
    validate_registration_password,
    visible_click_position,
)


_MAIL = "reg.istration@gmail.com"
_STAGED_PASSWORD = "F0rm.Test_8"
_MOCK_IMAGE = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
_FORM = f"""
<html lang="ja"><body>
<script>window.submissions = 0;</script>
<form action="/register/complete" method="post" onsubmit="window.submissions++; return false;">
    <table>
        <tr><th>ID（メールアドレス）</th><td><strong>{_MAIL}</strong></td></tr>
        <tr><th><span>必須</span> パスワード</th><td><input type="password" id="password"></td></tr>
        <tr><th><span>必須</span> パスワードの確認</th><td><input type="password" id="confirm"></td></tr>
        <tr><th>メールマガジン</th><td>
            <input type="radio" name="newsletter" value="1" id="subscribe"> <span>受信する</span>
            <input type="radio" name="newsletter" value="0" id="no-subscribe"> 受信しない
        </td></tr>
        <tr><th><span>必須</span> 画像認証</th><td>
            <img src="{_MOCK_IMAGE}" width="40" height="10">
            <input type="text" id="captcha">
        </td></tr>
    </table>
    <div><input type="checkbox" id="terms"><a href="/terms">利用規約</a>に同意します</div>
    <div><label><input type="checkbox" id="privacy"> 個人情報の取扱いに同意します</label></div>
    <button type="submit">本登録</button>
</form>
</body></html>
"""


class PasswordRuleTests(unittest.TestCase):
    def test_password_accepts_exact_ascii_character_set_and_length(self) -> None:
        for value in ("a._-", "abcdefghijklmnop", _STAGED_PASSWORD):
            self.assertTrue(validate_registration_password(value))
        for value in ("abc", "abcdefghijklmnopq", "test 8", "abcd\n", "abc$", "Ａbc9"):
            self.assertFalse(validate_registration_password(value))
        self.assertNotIn(_STAGED_PASSWORD, repr(RegistrationFormConfig(_STAGED_PASSWORD)))


class RegistrationFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch()
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()

    async def test_fills_actual_table_labels_and_leaves_blank_captcha_without_post(self) -> None:
        await self.page.set_content(_FORM)
        await fill_confirmation_form(
            self.page, _MAIL, RegistrationFormConfig(_STAGED_PASSWORD, accept_privacy=True), 3000
        )
        self.assertEqual(await self.page.locator("#password").input_value(), _STAGED_PASSWORD)
        self.assertEqual(await self.page.locator("#confirm").input_value(), _STAGED_PASSWORD)
        self.assertTrue(await self.page.locator("#no-subscribe").is_checked())
        self.assertFalse(await self.page.locator("#terms").is_checked())
        self.assertTrue(await self.page.locator("#privacy").is_checked())
        self.assertEqual(await self.page.locator("#captcha").input_value(), "")
        self.assertEqual(await self.page.evaluate("document.activeElement.id"), "captcha")
        self.assertEqual(await self.page.evaluate("window.submissions"), 0)

    async def test_different_dotting_in_form_is_rejected_before_writing_password(self) -> None:
        await self.page.set_content(_FORM)
        with self.assertRaises(RegistrationFormError) as error:
            await fill_confirmation_form(
                self.page, "registr.ation@gmail.com", RegistrationFormConfig(_STAGED_PASSWORD), 3000
            )
        self.assertEqual(error.exception.status, STATUS_WRONG_ID)
        self.assertEqual(await self.page.locator("#password").input_value(), "")
        self.assertEqual(await self.page.evaluate("window.submissions"), 0)

    async def test_expired_html_without_form_is_reported_as_expired(self) -> None:
        await self.page.set_content("<main><p>登録用URLの有効期限が過ぎました。</p></main>")
        with self.assertRaises(RegistrationFormError) as error:
            await fill_confirmation_form(self.page, _MAIL, RegistrationFormConfig(_STAGED_PASSWORD), 3000)
        self.assertEqual(error.exception.status, STATUS_FORM_EXPIRED)

    async def test_missing_captcha_input_is_reported_before_writing_password(self) -> None:
        await self.page.set_content(_FORM.replace('<input type="text" id="captcha">', ""))
        with self.assertRaises(RegistrationFormError) as error:
            await fill_confirmation_form(self.page, _MAIL, RegistrationFormConfig(_STAGED_PASSWORD), 3000)
        self.assertEqual(error.exception.status, STATUS_FORM_INVALID)
        self.assertNotIn(_STAGED_PASSWORD, str(error.exception))
        self.assertEqual(await self.page.locator("#password").input_value(), "")

    async def test_fills_ninja_hissu_image_and_both_agreement_checkboxes(self) -> None:
        html = f"""
        <html lang="ja"><body>
        <script>window.submissions = 0;</script>
        <form action="/register/confirm" method="post" onsubmit="window.submissions++; return false;">
            <table>
                <tr>
                    <th>ID（メールアドレス）</th>
                    <td>
                        {_MAIL}<br>
                        ※仮登録時に入力されたメールアドレスを表示しています。
                    </td>
                </tr>
                <tr>
                    <th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> パスワード</th>
                    <td><input type="password" name="password" id="ninja-pw"></td>
                </tr>
                <tr>
                    <th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> パスワードの確認</th>
                    <td><input type="password" name="password_confirm" id="ninja-pw-confirm"></td>
                </tr>
                <tr>
                    <th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> メールマガジン</th>
                    <td>
                        <input type="radio" name="mag" value="1" id="mag-yes"><label>受信する</label>
                        <input type="radio" name="mag" value="0" id="mag-no" checked><label>受信しない</label>
                    </td>
                </tr>
                <tr>
                    <th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> 画像認証</th>
                    <td>
                        下の画像に表示されている文字を入力してください。<br>
                        <img src="{_MOCK_IMAGE}" width="120" height="40"><br>
                        <a href="#">読みにくいので画像変更</a><br>
                        文字の入力 <input type="text" name="authimg" class="input-authimgInput" id="ninja-captcha">
                    </td>
                </tr>
            </table>
            <div><span><input type="checkbox" id="chk-rule"></span><a href="/terms">利用規約</a>に同意します</div>
            <div><input type="checkbox" id="chk-privacy"><label>個人情報の取扱いに同意します</label></div>
            <div id="registerInputSend" class="btn-regist btn_send_preShow">
                <input type="submit" value="上記の二項目に同意して、本登録">
            </div>
        </form>
        <script>
            const syncSubmit = () => {{
                const ready = document.getElementById("chk-rule").checked && document.getElementById("chk-privacy").checked;
                document.getElementById("registerInputSend").classList.toggle("btn_send_preShow", !ready);
            }};
            document.getElementById("chk-rule").addEventListener("change", syncSubmit);
            document.getElementById("chk-privacy").addEventListener("change", syncSubmit);
        </script>
        </body></html>
        """
        await self.page.set_content(html)
        await fill_confirmation_form(
            self.page,
            _MAIL,
            RegistrationFormConfig(
                _STAGED_PASSWORD, newsletter=True, accept_terms=True, accept_privacy=True
            ),
            3000,
        )
        self.assertEqual(await self.page.locator("#ninja-pw").input_value(), _STAGED_PASSWORD)
        self.assertEqual(await self.page.locator("#ninja-pw-confirm").input_value(), _STAGED_PASSWORD)
        self.assertTrue(await self.page.locator("#mag-yes").is_checked())
        self.assertFalse(await self.page.locator("#mag-no").is_checked())
        self.assertTrue(await self.page.locator("#chk-rule").is_checked())
        self.assertTrue(await self.page.locator("#chk-privacy").is_checked())
        self.assertNotIn(
            "btn_send_preShow",
            await self.page.locator("#registerInputSend").get_attribute("class") or "",
        )
        self.assertEqual(await self.page.locator("#ninja-captcha").input_value(), "")
        self.assertEqual(await self.page.evaluate("document.activeElement.id"), "ninja-captcha")
        self.assertEqual(await self.page.evaluate("window.submissions"), 0)

    async def test_ocr_helpers_capture_refresh_sprite_and_submission_states(self) -> None:
        html = f"""
        <html lang="ja"><body>
        <style>
            .btn-regist {{ width: 300px; height: 51px; overflow: hidden; }}
            .btn-regist:hover input {{ margin-top: -51px; }}
        </style>
        <form action="/register/confirm" method="post">
            <table>
                <tr><th>ID（メールアドレス）</th><td>{_MAIL}</td></tr>
                <tr><th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> パスワード</th><td><input type="password" id="pw"></td></tr>
                <tr><th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> パスワードの確認</th><td><input type="password" id="pw2"></td></tr>
                <tr><th>メールマガジン</th><td>
                    <input type="radio" name="mag" value="1"><label>受信する</label>
                    <input type="radio" name="mag" value="0" checked><label>受信しない</label>
                </td></tr>
                <tr><th><img src="{_MOCK_IMAGE}" alt="必須" class="img-hissu" /> 画像認証</th><td>
                    <img id="authimg" src="{_MOCK_IMAGE}" width="120" height="40"><br>
                    <a href="#refresh" id="refresh-link" onclick="window.refreshed = (window.refreshed || 0) + 1; return false;">読みにくいので画像変更</a><br>
                    文字の入力 <input type="text" name="authimg" id="ninja-captcha" value="oldval">
                </td></tr>
            </table>
            <div><input type="checkbox" id="chk-rule"><label>利用規約に同意します</label></div>
            <div><input type="checkbox" id="chk-privacy"><label>個人情報の取扱いに同意します</label></div>
            <div id="registerInputSend" class="btn-regist">
                <input type="image" src="{_MOCK_IMAGE}" alt="上記の二項目に同意して、本登録" width="300" height="102">
            </div>
        </form>
        </body></html>
        """
        await self.page.set_content(html)
        controls = await prepare_confirmation_form(
            self.page,
            _MAIL,
            RegistrationFormConfig(_STAGED_PASSWORD, newsletter=False, accept_terms=True, accept_privacy=True),
            3000,
            clear_captcha=True,
        )
        self.assertEqual(await controls.captcha_input.input_value(), "")
        shot = await capture_captcha_image(self.page, controls.captcha_image, timeout_ms=2000)
        self.assertIsNotNone(shot)
        self.assertTrue(shot.startswith(b"\x89PNG\r\n\x1a\n"))

        pos = await visible_click_position(controls.submit_button, 2000)
        self.assertIsNotNone(pos)
        self.assertLess(pos["y"], 51)

        self.assertTrue(await refresh_captcha_image(self.page, controls.captcha_row, timeout_ms=2000))
        self.assertEqual(await self.page.evaluate("window.refreshed"), 1)

        state, _ = await inspect_registration_submission_state(self.page)
        self.assertEqual(state, "retry")

        await self.page.set_content("<div id='registerInputSend2'><input type='submit' value='本登録する'></div>")
        state, _ = await inspect_registration_submission_state(self.page)
        self.assertEqual(state, "confirm_page")

        await self.page.set_content("<h3 class='ttl-registFinish'>新規ユーザー登録完了です！</h3>")
        state, _ = await inspect_registration_submission_state(self.page)
        self.assertEqual(state, "finished")
