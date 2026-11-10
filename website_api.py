"""Isolated read-only Flask blueprint for the website. No bot handlers changed."""
import os
import threading
import time
import hmac
import base64
from flask import Blueprint, jsonify, request, make_response, current_app
from sheets_api import read_current_stats

website_api = Blueprint("website_api", __name__)
_lock = threading.Lock()
_cache = {"data": None, "expires": 0}

@website_api.route('/api/website/stats', methods=['GET', 'OPTIONS'])
def website_stats():
    origin = request.headers.get('Origin', '')
    allowed = os.environ.get('WEBSITE_ALLOWED_ORIGIN', 'https://anaryann.github.io')
    if request.method == 'OPTIONS':
        response = make_response('', 204)
    else:
        encoded = request.headers.get('X-Website-Password', '')

        try:
            supplied = base64.b64decode(
                encoded, validate=True
            ).decode('utf-8')
        except (ValueError, UnicodeError):
            supplied = ''
        valid = [os.environ.get('WEBSITE_SITE_PASSWORD', ''), os.environ.get('WEBSITE_ADMIN_PASSWORD', '')]
        if not any(p and hmac.compare_digest(supplied.encode("utf-8"), p.encode("utf-8")) for p in valid):
            response = jsonify({"success": False, "unauthorized": True})
            response.status_code = 401
        else:
            try:
                with _lock:
                    if _cache['data'] is None or time.monotonic() >= _cache['expires']:
                        _cache['data'] = read_current_stats()
                        _cache['expires'] = time.monotonic() + 300
                    data = _cache['data']
                response = jsonify(data)
            except Exception:
                current_app.logger.exception('Website stats load failed')
                response = jsonify({"success": False, "error": "Не удалось загрузить статистику"})
                response.status_code = 503
    if origin == allowed:
        response.headers['Access-Control-Allow-Origin'] = allowed
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Headers'] = 'X-Website-Password'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Cache-Control'] = 'no-store'
    return response
