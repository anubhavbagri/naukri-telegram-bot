
import re

def extract_form_key2(js_text: str) -> str | None:
    match = re.search(r'key:"initUploader".*?d\s*=\s*"([A-Za-z0-9]+)"', js_text, re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r'd\s*=\s*"([A-Za-z0-9]{10,})"', js_text)
    return match.group(1) if match else None

APP_JS_PATTERN_URL = re.compile(r"/app_v\d+\.min\.js")


def extract_mnj_bundle_url(app_js: str, app_url: str) -> str | None:
    """Build the mnj bundle URL from the version map (`mnj:"_v330"`) in the app bundle."""
    m = re.search(r'\bmnj:"(_v\d+)"', app_js)
    if not m:
        return None
    return app_url.rsplit("/", 1)[0] + f"/mnj{m.group(1)}.min.js"


def extract_all_js_urls(html: str):
    return re.findall(r'<script[^>]+src="([^"]*\.js[^"]*)"', html)
