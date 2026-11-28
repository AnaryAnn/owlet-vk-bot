"""Compact weekly distribution attachment for VK."""
from collections import Counter
from io import BytesIO
from math import ceil
import json
import logging
import time
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageDraw, ImageFont


def render_distribution(records):
    counts = Counter(record['fact'] for record in records if record.get('factEntered'))
    if not counts:
        return None
    buckets = sorted(counts.items())
    image = Image.new('RGB', (1000, 390), '#ffffff')
    draw = ImageDraw.Draw(image)
    try:
        title = ImageFont.truetype('DejaVuSans.ttf', 32)
        label = ImageFont.truetype('DejaVuSans.ttf', 23)
    except OSError:
        title = ImageFont.load_default(size=32)
        label = ImageFont.load_default(size=23)
    draw.text((24, 12), 'Распределение факта', font=title, fill='#292720')
    draw.text((74, 58), 'Сычиков', font=label, fill='#466f68')
    left, right, top, bottom = 74, 968, 100, 301
    tick = max(1, ceil(max(counts.values()) / 4))
    for i in range(5):
        y = bottom - i * (bottom - top) / 4
        draw.line((left, y, right, y), fill='#dedbd2', width=2)
        draw.text((left - 12, y), str(i * tick), font=label, fill='#466f68', anchor='rm')
    slot = (right - left) / len(buckets)
    bar_width = min(50, slot * .68)
    max_label = max(draw.textlength(str(value), font=label) for value, _ in buckets)
    spacing = max(1, ceil((max_label + 10) / slot))
    for index, (value, count) in enumerate(buckets):
        x = left + slot * (index + .5)
        y = bottom - count / (4 * tick) * (bottom - top)
        draw.rectangle((x - bar_width / 2, y, x + bar_width / 2, bottom), fill='#56857b')
        if slot >= 36:
            draw.text((x, y - 4), str(count), font=label, fill='#365b54', anchor='mb')
        if index == len(buckets) - 1 or (index % spacing == 0 and len(buckets) - 1 - index >= spacing):
            draw.text((x, bottom + 10), str(value), font=label, fill='#292720', anchor='mt')
    draw.text(((left + right) / 2, 369), 'Мышей', font=label, fill='#756f64', anchor='mm')
    output = BytesIO()
    image.save(output, 'PNG', optimize=True)
    return output.getvalue()


def upload_chart(peer_id, token, chart):
    deadline = time.monotonic() + 30

    def post(stage, url, **kwargs):
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout(f'Stats chart deadline before {stage}')
            try:
                response = requests.post(url, timeout=(min(3, remaining), min(30, remaining)), **kwargs)
                response.raise_for_status()
                payload = response.json()
                error = payload.get('error')
                if isinstance(error, dict):
                    code = error.get('error_code')
                    logging.warning('STATS_CHART_UPLOAD_VK_ERROR stage=%s attempt=%s code=%s',
                                    stage, attempt + 1, code)
                    if code in (1, 6, 9, 10, 29) and attempt == 0:
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
                        continue
                    raise RuntimeError(f'VK {stage} error {code}')
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                logging.warning('STATS_CHART_UPLOAD_NETWORK stage=%s attempt=%s type=%s',
                                stage, attempt + 1, type(exc).__name__)
                if attempt == 1:
                    raise

    def vk(method, **params):
        payload = post(method, f'https://api.vk.com/method/{method}',
                       data=dict(params, access_token=token, v='5.199'))
        return payload['response']

    url = vk('photos.getMessagesUploadServer', peer_id=peer_id)['upload_url']
    if urlsplit(url).scheme != 'https':
        raise ValueError('VK upload URL must use HTTPS')
    jpeg = BytesIO()
    with Image.open(BytesIO(chart)) as image:
        image.convert('RGB').save(jpeg, 'JPEG', quality=92, optimize=True)
    uploaded = post('photo_upload', url, files={'photo': ('statistics.jpg', jpeg.getvalue(), 'image/jpeg')})
    server, photo, upload_hash = (uploaded.get(key) for key in ('server', 'photo', 'hash'))
    if isinstance(photo, (list, dict)):
        photo = json.dumps(photo, separators=(',', ':'))
    if not server or not upload_hash or not isinstance(photo, str) or photo.strip() in ('', '[]', '{}', 'null'):
        raise ValueError('VK upload did not return a photo to save')
    saved = vk('photos.saveMessagesPhoto', server=server, photo=photo, hash=upload_hash)[0]
    access_key = saved.get('access_key')
    return f"photo{saved['owner_id']}_{saved['id']}" + (f'_{access_key}' if access_key else '')


def render_personal_progress(data):
    image = Image.new('RGB', (800, 340), '#ffffff')
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 21)
    except OSError:
        font = ImageFont.load_default(size=21)
    draw.text((28, 16), 'Мой прогресс', font=font, fill='#292720')
    left, right, top, bottom = 76, 760, 70, 270
    tick = max(1, ceil(max(value for _, plan, fact in data
                           for value in (plan, fact) if value is not None) / 4))
    for i in range(5):
        y = bottom - i * (bottom - top) / 4
        draw.line((left, y, right, y), fill='#dedbd2', width=2)
        draw.text((left - 10, y), str(i * tick), font=font, fill='#466f68', anchor='rm')
    slot = (right - left) / len(data)
    series = [(1, 'План', '#56857b'), (2, 'Факт', '#b15a38')]
    for column, label, color in series:
        previous = None
        for index, week in enumerate(data):
            value = week[column]
            if value is None:
                previous = None
                continue
            x = left + slot * (index + .5)
            y = bottom - value / (tick * 4) * (bottom - top)
            if previous:
                draw.line((*previous, x, y), fill=color, width=4)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)
            previous = (x, y)
        x = 480 if column == 1 else 615
        draw.line((x, 31, x + 25, 31), fill=color, width=4)
        draw.text((x + 34, 31), label, fill='#292720', font=font, anchor='lm')
    label_step = max(1, ceil(72 / slot))
    last = len(data) - 1
    for index, (label, _, _) in enumerate(data):
        if index != last and (index % label_step or last - index < label_step):
            continue
        x = left + slot * (index + .5)
        draw.text((x, bottom + 14), label, font=font, fill='#292720', anchor='mt')
    output = BytesIO()
    image.save(output, 'PNG', optimize=True)
    return output.getvalue()
