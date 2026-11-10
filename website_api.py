"""Изолированные API сайта. Новая запись выключена до согласованного перехода."""
import os
import threading
import time
import hmac
import base64
from flask import Blueprint, jsonify, request, make_response, current_app
from sheets_api import read_current_stats, current_week, read_stats_history

website_api = Blueprint("website_api", __name__)
_lock = threading.Lock()
_cache = {"data": None, "expires": 0, "week": None}


def _admin():
    try:
        supplied = base64.b64decode(request.headers.get('X-Website-Password', ''), validate=True)
    except ValueError:
        return False
    expected = os.environ.get('WEBSITE_ADMIN_PASSWORD', '').encode()
    return bool(expected) and hmac.compare_digest(supplied, expected)


@website_api.route('/api/website/login', methods=['GET', 'OPTIONS'])
def website_login():
    if request.method == 'OPTIONS': return '', 204
    if not _authorized(): return jsonify(success=False, unauthorized=True, error='Неверный пароль'), 401
    return jsonify(success=True, isAdmin=_admin())


@website_api.route('/api/website/preflight', methods=['GET', 'OPTIONS'])
def website_preflight():
    if request.method == 'OPTIONS': return '', 204
    if not _authorized(): return jsonify(success=False, unauthorized=True), 401
    if not _admin(): return jsonify(success=False, forbidden=True), 403
    try:
        from native_preflight import check
        return jsonify(check())
    except Exception:
        current_app.logger.exception('Native preflight failed')
        return jsonify(success=False, error='Не удалось проверить доступ к таблицам; проверьте журнал Render'), 503


@website_api.route('/api/website/settings', methods=['GET', 'POST', 'OPTIONS'])
def website_settings():
    if request.method == 'OPTIONS': return '', 204
    if not _authorized(): return jsonify(success=False, unauthorized=True), 401
    if not _admin(): return jsonify(success=False, forbidden=True, error='Настройки доступны только администратору'), 403
    try:
        from native_storage import stats_get, stats_post
        if request.method == 'GET': return jsonify(stats_get({'action': 'settings'}))
        data = request.get_json(silent=True)
        if not isinstance(data, dict): return jsonify(success=False, error='Ожидается JSON'), 400
        return jsonify(stats_post({'action': 'saveGoal', 'goal': data.get('goal')}))
    except ValueError as exc: return jsonify(success=False, error=str(exc)), 400
    except Exception:
        current_app.logger.exception('Native website settings failed')
        return jsonify(success=False, error='Настройки временно недоступны'), 503


@website_api.route('/api/website/bonus', methods=['POST', 'OPTIONS'])
def website_bonus():
    if request.method == 'OPTIONS': return '', 204
    if not _authorized(): return jsonify(success=False, unauthorized=True), 401
    if not _admin(): return jsonify(success=False, forbidden=True), 403
    try:
        from native_storage import enabled
        enabled()
        from native_bonus import award
        from app import build_weekly_bonus_candidates, vk_send, STATS_PEER_ID
        data = request.get_json(silent=True) or {}
        threshold = data.get('threshold')
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1e9:
            raise ValueError('Некорректный порог')
        if not isinstance(data.get('preview', True), bool): raise ValueError('preview должен быть boolean')
        result = award(build_weekly_bonus_candidates(threshold, data.get('week', 'current')),
            threshold, str(data.get('rarity', 'необычный')), str(data.get('campaign', '')),
            preview=data.get('preview', True))
        if result.get('awarded'):
            text = '🦉 РОБОСЫЧИК. ВНЕОЧЕРЕДНАЯ ВЫДАЧА АРТЕФАКТОВ!\n\n'
            text += f"🐭 За факт строго больше {threshold} мышей награду получили {result['awardedCount']} сычиков!\n"
            text += '\n'.join(f"🎁 {p['name']} ({p['fact']} 🐭) — {p['artifact']}" for p in result['awarded'])
            try:
                vk_send(STATS_PEER_ID, text); result['announced'] = True
            except Exception:
                result['announced'] = False
                result['warning'] = 'Артефакты выданы, сообщение не подтверждено'
        return jsonify(result)
    except ValueError as exc: return jsonify(success=False, error=str(exc)), 400
    except Exception:
        current_app.logger.exception('Native bonus failed')
        return jsonify(success=False, error='Выдача не подтверждена. Проверьте журнал'), 503


@website_api.route('/api/website/treasury', methods=['GET', 'OPTIONS'])
def website_treasury():
    if request.method == 'OPTIONS': return '', 204
    if not _authorized(): return jsonify(success=False, unauthorized=True), 401
    return public_treasury()


@website_api.route('/api/treasury', methods=['GET', 'OPTIONS'])
def public_treasury():
    # Старый отдельный сайт имел публичный read-only API. Каталог целиком не выдаём.
    if request.method == 'OPTIONS': return '', 204
    try:
        from native_treasury import read
        return jsonify(read(request.args.get('action', 'collection'), request.args.get('vkId', '0')))
    except ValueError as exc: return jsonify(ok=False, error=str(exc)), 400
    except Exception:
        current_app.logger.exception('Native treasury failed')
        return jsonify(ok=False, error='Сокровищница временно недоступна'), 503


def _authorized():
    try:
        supplied = base64.b64decode(request.headers.get('X-Website-Password', ''),
                                   validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        return False
    return any(p and hmac.compare_digest(supplied.encode(), p.encode()) for p in
        (os.environ.get('WEBSITE_SITE_PASSWORD', ''), os.environ.get('WEBSITE_ADMIN_PASSWORD', '')))


@website_api.after_request
def website_headers(response):
    allowed = os.environ.get('WEBSITE_ALLOWED_ORIGIN', 'https://anaryann.github.io')
    if request.headers.get('Origin', '') == allowed:
        response.headers['Access-Control-Allow-Origin'] = allowed
        response.vary.add('Origin')
        response.headers['Access-Control-Allow-Headers'] = 'X-Website-Password, Content-Type'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Cache-Control'] = 'no-store'
    return response


@website_api.route('/api/website/records', methods=['POST', 'OPTIONS'])
def website_record():
    if request.method == 'OPTIONS':
        return '', 204
    if not _authorized():
        return jsonify(success=False, unauthorized=True), 401
    if os.environ.get('WEBSITE_WRITE_ENABLED', '').lower() != 'true':
        return jsonify(success=False, error='Внесение через Python ещё не включено'), 503
    if request.content_length is None or request.content_length > 4096:
        return jsonify(success=False, error='Некорректный размер запроса'), 413
    from website_store import submit_report, Busy, Conflict, Uncertain
    try:
        data = request.get_json(silent=True)
        result = submit_report(data)
        with _lock:
            _cache.update(data=None, expires=0, week=None)
        return jsonify(result)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except Conflict as exc:
        return jsonify(success=False, error=str(exc)), 409
    except Busy as exc:
        return jsonify(success=False, error=str(exc)), 503
    except Uncertain:
        return jsonify(success=False, uncertain=True,
                       error='Результат записи уточняется. Повторите тот же запрос'), 503
    except Exception:
        current_app.logger.exception('Website record failed')
        return jsonify(success=False, error='Не удалось подтвердить запись. Повторите тот же запрос'), 503


@website_api.route('/api/website/participants', methods=['GET', 'OPTIONS'])
def website_participants():
    if request.method == 'OPTIONS':
        return '', 204
    if not _authorized():
        return jsonify(success=False, unauthorized=True), 401
    try:
        from website_store import Sheets
        from sheets_api import _session, SHEET_ID
        with _session() as session:
            rows = Sheets(session).values(SHEET_ID, ["'Участники'!A2:C"])[0]
        return jsonify(success=True, people=sorted(str(r[0]).strip() for r in rows
            if r and str(r[0]).strip()))
    except Exception:
        current_app.logger.exception('Website participants failed')
        return jsonify(success=False, error='Не удалось загрузить участников'), 503


@website_api.route('/api/website/current', methods=['GET', 'OPTIONS'])
def website_current():
    if request.method == 'OPTIONS':
        return '', 204
    if not _authorized():
        return jsonify(success=False, unauthorized=True), 401
    try:
        from native_storage import stats_get
        return jsonify(stats_get({'action': 'current', 'name': request.args.get('name', '')}))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except Exception:
        current_app.logger.exception('Website current record failed')
        return jsonify(success=False, error='Не удалось прочитать запись'), 503



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
                    week = current_week()
                    if (_cache['data'] is None or time.monotonic() >= _cache['expires']
                            or _cache['week'] != week['start']):
                        _cache['data'] = read_current_stats(week=week)
                        _cache['expires'] = time.monotonic() + 300
                        _cache['week'] = week['start']
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



_history_cache = {"data": None, "expires": 0, "week": None}
_history_lock = threading.Lock()


@website_api.route('/api/website/history', methods=['GET', 'OPTIONS'])
def website_history():
    if request.method == 'OPTIONS':
        return '', 204
    if not _authorized():
        return jsonify(success=False, unauthorized=True), 401
    try:
        with _history_lock:
            week = current_week()['start']
            if (_history_cache['data'] is None or time.monotonic() >= _history_cache['expires']
                    or _history_cache['week'] != week):
                _history_cache['data'] = read_stats_history()
                _history_cache['expires'] = time.monotonic() + 300
                _history_cache['week'] = week
            return jsonify(_history_cache['data'])
    except Exception:
        current_app.logger.exception('Website history load failed')
        return jsonify(success=False, error='Не удалось загрузить историю'), 503
