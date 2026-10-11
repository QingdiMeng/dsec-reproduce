"""Optional same-origin static console. IAM remains the only access authority."""
from importlib.resources import files

ASSETS = {'/console/': ('index.html', 'text/html; charset=utf-8'),
          '/console/console.js': ('console.js', 'text/javascript; charset=utf-8'),
          '/console/console.css': ('console.css', 'text/css; charset=utf-8')}
HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'",
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
}


def asset(path):
    if path not in ASSETS:
        return None
    name, mime = ASSETS[path]
    return files('dsec.host').joinpath('console', name).read_bytes(), mime
