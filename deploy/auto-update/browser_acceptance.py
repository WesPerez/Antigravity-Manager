#!/usr/bin/env python3
"""Exercise the published management UI through its configured web subpath."""
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page()
        errors = []
        account_statuses = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: account_statuses.append(response.status)
                if response.url.endswith('/api/accounts') and response.request.method == 'GET' else None)
        page.goto('http://127.0.0.1:18045/antimanager/accounts', wait_until='networkidle')
        page.locator('input[type="password"]').fill('synthetic-web-password')
        with page.expect_response(lambda response: response.url.endswith('/api/accounts')
                                  and response.request.method == 'GET' and response.status == 200,
                                  timeout=15000):
            page.locator('form button[type="submit"]').click()
        page.wait_for_load_state('networkidle')
        assert 200 in account_statuses, account_statuses
        # React Router's Link to="/" produces the basename without a trailing
        # slash. nginx adds that slash only for a full document request.
        with page.expect_response(lambda response: response.url.endswith('/api/accounts')
                                  and response.request.method == 'GET' and response.status == 200,
                                  timeout=15000):
            page.locator('a[href="/antimanager"]:visible, a[href="/antimanager/"]:visible').first.click()
        page.wait_for_url(lambda url: urlparse(url).path.rstrip('/') == '/antimanager')
        account_link = page.locator('a[href="/antimanager/accounts"]:visible').first
        account_link.wait_for(timeout=15000)
        with page.expect_response(lambda response: response.url.endswith('/api/accounts')
                                  and response.request.method == 'GET' and response.status == 200,
                                  timeout=15000):
            account_link.click()
        page.wait_for_url('**/antimanager/accounts')
        with page.expect_response(lambda response: response.url.endswith('/api/accounts')
                                  and response.request.method == 'GET' and response.status == 200,
                                  timeout=15000):
            page.reload(wait_until='networkidle')
        page.wait_for_function("document.querySelector('input[type=password]') === null")
        page.locator('a[href="/antimanager/accounts"]:visible').first.wait_for(timeout=15000)
        assert not errors, errors
        assert page.locator('body').inner_text().strip()
        browser.close()


if __name__ == '__main__':
    from urllib.request import urlopen
    with urlopen('http://127.0.0.1:18045/antimanager/health', timeout=10) as response:
        assert response.status == 200
    main()
