"""The ONLY module permitted to open cred.txt, exclusively during application login.

Never call this module against real credentials during development. No tracing/screenshots.
"""

from pathlib import Path
from urllib.parse import urlsplit


class AuthenticationError(RuntimeError):
    pass


async def ensure_login(page, settings, selectors, *, credential_file: Path | None = None):
    """Reuse browser profile; read credentials only if a genuine same-origin login form is shown."""
    try:
        await page.goto(settings.moodle_url.rstrip("/") + "/my/", wait_until="domcontentloaded")
        await page.wait_for_timeout(700)
        if (
            "/login/" not in urlsplit(page.url).path
            and not await page.locator(selectors["password"]).count()
            and await page.locator(selectors["authenticated"]).count()
        ):
            return "SESSION_REUSED"
        base = urlsplit(settings.moodle_url)
        await page.goto(settings.moodle_url.rstrip("/") + "/login/index.php", wait_until="domcontentloaded")
        here = urlsplit(page.url)
        if (here.scheme, here.netloc) != (base.scheme, base.netloc):
            raise AuthenticationError("AUTH_ORIGIN")
        user_field = page.locator(selectors["username"]).first
        pass_field = page.locator(selectors["password"]).first
        await user_field.wait_for(state="visible")
        await pass_field.wait_for(state="visible")
        # Refuse a form that would send credentials off-origin.
        action = await pass_field.evaluate("e => e.form ? e.form.action : location.href")
        target = urlsplit(action)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise AuthenticationError("AUTH_FORM_ORIGIN")
        path = credential_file if credential_file is not None else settings.root / "cred.txt"
        values = {}
        with path.open("r", encoding="utf-8-sig") as stream:
            for line in stream:
                key, sep, value = line.rstrip("\r\n").partition("=")
                if sep and key.strip() in {"username", "password"}:
                    values[key.strip()] = value
        if not values.get("username") or not values.get("password"):
            raise AuthenticationError("AUTH_FILE_FORMAT")
        await user_field.fill(values["username"])
        await pass_field.fill(values["password"])
        values.clear()
        await page.locator(selectors["login"]).first.click()
        await page.locator(selectors["authenticated"]).first.wait_for(state="attached", timeout=30000)
        return "SIGNED_IN"
    except Exception:
        # Do not preserve exception chains containing fill() call arguments.
        raise AuthenticationError(
            "AUTH_FAILED: проверьте вход вручную в браузере и настройки селекторов"
        ) from None
