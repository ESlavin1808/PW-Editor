"""
PW Editor — блочный редактор контента в стиле WordPress Gutenberg
Локальное приложение на PyWry. Данные в JSON, экспорт HTML/MD/TXT.
"""

from pywry import PyWry
import json, os, datetime, html as htmllib, re
from urllib.request import Request, urlopen
from urllib.error import URLError

# ─── Безопасное хранение API-ключа ────────────────────────
# Первичное хранилище — Windows Credential Manager (через keyring).
# Если keyring недоступен — fallback в settings.json (providers.*.key).
try:
    import keyring
    _KEYRING_OK = True
except ImportError:
    _KEYRING_OK = False

_KEYRING_SERVICE = 'PW_Editor_AI'
_SETTINGS_FALLBACK = True  # всегда дублировать ключ в settings.json

def _get_api_key(provider='default', settings_data=None):
    """Возвращает API-ключ для указанного провайдера.
    Сначала Credential Manager, затем fallback в settings.json."""
    # Попытка 1: Credential Manager
    if _KEYRING_OK:
        try:
            k = keyring.get_password(_KEYRING_SERVICE, f'api_key_{provider}')
            if k:
                return k
            # Если ключа нет в CM — не беда, пробуем fallback
        except Exception:
            pass
    # Попытка 2: fallback из settings.json
    if settings_data and 'providers' in settings_data:
        prov = settings_data['providers'].get(provider, {})
        k = prov.get('key', '')
        if k:
            return k
    return ''

def _set_api_key(key, provider='default'):
    """Сохраняет API-ключ для указанного провайдера.
    Всегда в Credential Manager, дублируется в settings.json."""
    ok = False
    if _KEYRING_OK and key:
        try:
            keyring.set_password(_KEYRING_SERVICE, f'api_key_{provider}', key)
            ok = True
        except Exception:
            pass
    return ok

def _delete_api_key(provider='default'):
    """Удаляет API-ключ для указанного провайдера."""
    if _KEYRING_OK:
        try:
            keyring.delete_password(_KEYRING_SERVICE, f'api_key_{provider}')
        except Exception:
            pass

def _get_all_provider_keys(providers, settings_data=None):
    """Собирает все сохранённые ключи для списка провайдеров."""
    keys = {}
    for p in providers:
        k = _get_api_key(p, settings_data)
        if k:
            keys[p] = k
    return keys

def _migrate_api_key(settings):
    """Переносит старый ai_key в per-provider формат (в 'default' + 'custom')."""
    old_key = settings.pop('ai_key', '')
    if old_key:
        # Сохраняем как ключ для 'default' (для обратной совместимости с custom)
        _set_api_key(old_key, 'default')
        _set_api_key(old_key, 'custom')
        del settings['ai_key']
    return settings

BASE       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE, 'data')
MEDIA_DIR  = os.path.join(BASE, 'data', 'media')
SETTINGS   = os.path.join(BASE, 'settings.json')
EXPORT_DIR = os.path.join(BASE, 'export')

for d in (DATA_DIR, MEDIA_DIR, EXPORT_DIR):
    os.makedirs(d, exist_ok=True)

CONTENT_TYPES = {
    'post':    {'icon': '📝', 'label': 'Записи'},
    'news':    {'icon': '📰', 'label': 'Новости'},
    'article': {'icon': '📄', 'label': 'Статьи'},
}

# ─── Настройки ─────────────────────────────────────────────
KNOWN_PROVIDERS = ['mistral', 'ollama', 'openai', 'deepseek', 'custom']

def load_settings():
    if os.path.exists(SETTINGS):
        with open(SETTINGS, 'r', encoding='utf-8') as f:
            s = json.load(f)
        # Миграция старого ключа при первом запуске
        if 'ai_key' in s:
            s = _migrate_api_key(s)
            save_settings(s, skip_credman=True)
        # Гарантируем providers dict
        if 'providers' not in s or not isinstance(s['providers'], dict):
            s['providers'] = {}
        # Заполняем недостающие известные провайдеры
        for p in KNOWN_PROVIDERS:
            if p not in s['providers']:
                s['providers'][p] = {'url': '', 'model': ''}
        current = s.get('current_provider', 'mistral')
        s['current_provider'] = current
        # Подтягиваем данные для текущего провайдера
        prov_data = s['providers'].get(current, {})
        s['ai_url'] = prov_data.get('url', s.get('ai_url', ''))
        s['ai_model'] = prov_data.get('model', s.get('ai_model', ''))
        # Ключ: сначала Credential Manager, потом fallback из providers
        s['ai_key'] = _get_api_key(current, s)
        return s
    return {'export_path': EXPORT_DIR, 'export_format': 'html',
            'ai_url': '', 'ai_key': '', 'ai_model': '',
            'ai_settings_collapsed': True, 'theme': 'light',
            'current_provider': 'mistral',
            'providers': {p: {'url': '', 'model': ''} for p in KNOWN_PROVIDERS}}

def save_settings(s, skip_credman=False):
    """Сохраняет настройки. API-ключи — в Credential Manager + fallback в JSON."""
    api_key = s.pop('ai_key', '')
    provider = s.get('current_provider', 'default')
    if api_key and not skip_credman:
        _set_api_key(api_key, provider)
    elif not api_key and not skip_credman:
        _delete_api_key(provider)
    # Сохраняем данные текущего провайдера в providers dict
    if 'providers' not in s:
        s['providers'] = {}
    current = s.get('current_provider', 'custom')
    if current not in s['providers']:
        s['providers'][current] = {}
    s['providers'][current]['url'] = s.get('ai_url', '')
    s['providers'][current]['model'] = s.get('ai_model', '')
    # Сохраняем ключ в JSON как fallback (только для текущего провайдера)
    if api_key:
        s['providers'][current]['key'] = api_key
    elif current in s['providers'] and 'key' in s['providers'][current]:
        # Если ключ пустой — удаляем из JSON, но не трогаем другие провайдеры
        del s['providers'][current]['key']
    # Сохраняем ключи других провайдеров, если переданы
    provider_keys = s.pop('provider_keys', {})
    if provider_keys and not skip_credman:
        for p, k in provider_keys.items():
            if p == current:
                continue  # уже сохранили выше
            if k:
                _set_api_key(k, p)
                # Fallback в JSON
                if p in s['providers']:
                    s['providers'][p]['key'] = k
                elif p not in s['providers']:
                    s['providers'][p] = {'key': k}
            else:
                _delete_api_key(p)
                # Удаляем из JSON fallback
                if p in s['providers'] and 'key' in s['providers'][p]:
                    del s['providers'][p]['key']
    # Пишем JSON
    with open(SETTINGS, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    # Возвращаем ключ обратно
    s['ai_key'] = _get_api_key(provider, s)

# ─── JSON-хранилище ───────────────────────────────────────
def load_items(ctype):
    p = os.path.join(DATA_DIR, f'{ctype}.json')
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_items(ctype, items):
    p = os.path.join(DATA_DIR, f'{ctype}.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ─── Экспорт ───────────────────────────────────────────────
def export_html(item, export_path=None, return_html=False):
    title = item.get('title', 'Без названия')
    content = item.get('content', '')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>{htmllib.escape(title)}</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:var(--text-primary);line-height:1.7}}
h1{{font-size:28px;margin-bottom:8px}}.meta{{color:var(--text-secondary);font-size:13px;margin-bottom:32px}}
h2{{font-size:22px;margin-top:28px}}h3{{font-size:18px;margin-top:24px}}
blockquote{{border-left:4px solid var(--accent);padding-left:16px;color:var(--text-tertiary)}}
pre{{background:var(--bg-body);padding:16px;border-radius:4px;overflow-x:auto}}
hr{{border:none;border-top:1px solid var(--border-light);margin:32px 0}}
img{{max-width:100%;height:auto;border-radius:4px}}
table{{width:100%;border-collapse:collapse;margin:24px 0}}
td,th{{border:1px solid var(--border);padding:8px 12px;text-align:left;vertical-align:top}}
@media print{{body{{max-width:none;margin:0;padding:20px}}}}
</style></head><body>
<h1>{title}</h1>
<div class="meta">{now}</div>
{content}
</body></html>"""
    if return_html:
        return html
    fname = re.sub(r'[\\/*?:"<>|]', '_', title)[:80] + '.html'
    fpath = os.path.join(export_path, fname)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(html)
    return fpath

def html_to_markdown(text):
    text = re.sub(r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>', r'![](\1)', text)
    text = re.sub(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'[\2](\1)', text)
    text = re.sub(r'<h2[^>]*>(.*?)</h2>', r'## \1', text, flags=re.DOTALL)
    text = re.sub(r'<h3[^>]*>(.*?)</h3>', r'### \1', text, flags=re.DOTALL)
    text = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'## \1', text, flags=re.DOTALL)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<pre><code>(.*?)</code></pre>', r'```\n\1\n```', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text)
    text = re.sub(r'<blockquote>(.*?)</blockquote>', lambda m: '> ' + m.group(1).strip().replace('\n', '\n> '), text, flags=re.DOTALL)
    text = re.sub(r'<li>(.*?)</li>', r'* \1', text, flags=re.DOTALL)
    text = re.sub(r'<hr[^>]*>', '\n---\n', text)
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def export_markdown(item, export_path):
    title = item.get('title', 'Без названия')
    md_body = html_to_markdown(item.get('content', ''))
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    md = f"---\ntitle: {title}\ndate: {now}\n---\n\n# {title}\n\n{md_body}\n"
    fname = re.sub(r'[\\/*?:"<>|]', '_', title)[:80] + '.md'
    fpath = os.path.join(export_path, fname)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(md)
    return fpath

def export_text(item, export_path):
    title = item.get('title', 'Без названия')
    text = re.sub(r'<[^>]+>', '\n', item.get('content', ''))
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    text = htmllib.unescape(text)
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    plain = f"Название: {title}\nДата: {now}\n{'='*50}\n\n{text}\n"
    fname = re.sub(r'[\\/*?:"<>|]', '_', title)[:80] + '.txt'
    fpath = os.path.join(export_path, fname)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(plain)
    return fpath

EXPORTERS = {'html': export_html, 'md': export_markdown, 'txt': export_text, 'pdf': export_html}
FORMAT_LABELS = {'html': 'HTML', 'md': 'Markdown', 'txt': 'TXT (текст)', 'pdf': 'PDF'}

# ─── AI ─────────────────────────────────────────────────────
def cb_ai_query(data, event_type, label):
    prompt = data.get('prompt', '')
    system = data.get('system', '')
    api_url = data.get('api_url', '').rstrip('/')
    api_key = data.get('api_key', '')
    model = data.get('model', 'gpt-3.5-turbo')
    provider = data.get('provider', 'openai')

    if not api_url:
        app.emit('ui:ai-result', {'error': 'Укажите URL API в настройках'}, label)
        return
    if not prompt:
        app.emit('ui:ai-result', {'error': 'Нет текста для запроса'}, label)
        return
    if not model:
        app.emit('ui:ai-result', {'error': 'Не выбрана модель. Загрузите список моделей и выберите одну.'}, label)
        return

    # Ollama использует другой endpoint
    if provider == 'ollama':
        endpoint = f'{api_url}/api/chat'
        payload = json.dumps({
            'model': model,
            'messages': ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': prompt}],
            'stream': False,
        }).encode('utf-8')
    else:
        endpoint = f'{api_url}/chat/completions'
        payload = json.dumps({
            'model': model,
            'messages': ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': prompt}],
            'temperature': 0.7,
        }).encode('utf-8')

    req = Request(endpoint, data=payload,
                  headers={'Content-Type': 'application/json'})
    if api_key and provider != 'ollama':
        req.add_header('Authorization', f'Bearer {api_key}')

    try:
        resp = urlopen(req, timeout=120)
        raw = json.loads(resp.read())
        if provider == 'ollama':
            # Ollama: {"message": {"content": "..."}}
            result = raw.get('message', {}).get('content', '')
        else:
            # OpenAI-совместимые: {"choices": [{"message": {"content": "..."}}]}
            result = raw['choices'][0]['message']['content']
        app.emit('ui:ai-result', {'text': result}, label)
    except URLError as e:
        err_msg = str(e)
        # try to read response body for more details
        try:
            body = e.read().decode('utf-8', errors='replace')[:500]
            if body:
                err_msg += '\n' + body
        except Exception:
            pass
        app.emit('ui:ai-result', {'error': err_msg}, label)
    except Exception as e:
        app.emit('ui:ai-result', {'error': str(e)}, label)

def cb_ai_list_models(data, event_type, label):
    """Получает список моделей от API провайдера."""
    api_url = data.get('api_url', '').rstrip('/')
    api_key = data.get('api_key', '')
    provider = data.get('provider', 'openai')

    if not api_url:
        app.emit('ui:ai-models', {'error': 'Укажите URL API'}, label)
        return

    try:
        # Ollama использует другой endpoint
        if provider == 'ollama':
            req = Request(f'{api_url}/api/tags',
                          headers={'Content-Type': 'application/json'})
        else:
            req = Request(f'{api_url}/models',
                          headers={'Content-Type': 'application/json'})
            if api_key:
                req.add_header('Authorization', f'Bearer {api_key}')

        resp = urlopen(req, timeout=30)
        raw = json.loads(resp.read())

        models = []
        if provider == 'ollama':
            # Ollama: {"models": [{"name": "llama3.2:3b", "details":{"families":["llama"]}, ...}]}
            for m in raw.get('models', []):
                name = m.get('name', '')
                if name:
                    caps = []
                    lname = name.lower()
                    if any(x in lname for x in ('vision', 'llava', 'bakllava', 'minicpm-v')):
                        caps.append('vision')
                    if any(x in lname for x in ('code', 'codellama', 'deepseek-coder', 'qwen2.5-coder')):
                        caps.append('code')
                    if any(x in lname for x in ('instruct', 'chat', 'text', 'nomic')):
                        caps.append('text')
                    models.append({'id': name, 'name': name, 'capabilities': caps})
        else:
            # OpenAI-совместимые: {"data": [{"id": "gpt-4", ...}]}
            for m in raw.get('data', []):
                mid = m.get('id', '')
                if mid:
                    caps = []
                    lmid = mid.lower()
                    if any(x in lmid for x in ('vision', 'gpt-4o', 'claude-3-opus', 'gemini-pro-vision')):
                        caps.append('vision')
                    if any(x in lmid for x in ('code', 'deepseek-coder', 'starcoder')):
                        caps.append('code')
                    if 'instruct' in lmid or 'chat' in lmid:
                        caps.append('text')
                    models.append({'id': mid, 'name': mid, 'capabilities': caps})

        models.sort(key=lambda x: x['name'])
        app.emit('ui:ai-models', {'models': models, 'quick': data.get('quick', False)}, label)

    except URLError as e:
        app.emit('ui:ai-models', {'error': f'Ошибка: {str(e)}'}, label)
    except Exception as e:
        app.emit('ui:ai-models', {'error': str(e)}, label)

# ─── PyWry ──────────────────────────────────────────────────
app = PyWry()

# ─── Колбэки ────────────────────────────────────────────────
def cb_switch(data, event_type, label):
    ctype = data.get('content_type', 'post')
    items = load_items(ctype)
    app.emit('ui:render-list', {'items': items, 'type': ctype}, label)

def cb_get_settings(data, event_type, label):
    s = load_settings()
    current = s.get('current_provider', 'mistral')
    # Собираем ключи всех известных провайдеров
    all_providers = list(s.get('providers', {}).keys()) + ['default']
    provider_keys = _get_all_provider_keys(all_providers, s)
    app.emit('ui:settings', {
        'export_path': s.get('export_path', EXPORT_DIR),
        'export_format': s.get('export_format', 'html'),
        'data_path': DATA_DIR,
        'ai_url': s.get('ai_url', ''),
        'ai_key': s.get('ai_key', ''),
        'ai_model': s.get('ai_model', ''),
        'ai_settings_collapsed': s.get('ai_settings_collapsed', True),
        'theme': s.get('theme', 'light'),
        'current_provider': current,
        'providers': s.get('providers', {}),
        'provider_keys': provider_keys,
    }, label)

def cb_save_settings(data, event_type, label):
    s = load_settings()
    for key in ('export_path', 'export_format', 'ai_url', 'ai_key', 'ai_model',
                'ai_settings_collapsed', 'theme', 'current_provider', 'provider_keys'):
        if key in data:
            s[key] = data[key]
    # Обновляем providers dict если передан целиком
    if 'providers' in data and isinstance(data['providers'], dict):
        if 'providers' not in s:
            s['providers'] = {}
        for p, pdata in data['providers'].items():
            if isinstance(pdata, dict):
                if p not in s['providers']:
                    s['providers'][p] = {}
                s['providers'][p].update(pdata)
    save_settings(s)
    app.emit('ui:toast', {'message': 'Настройки сохранены'}, label)

def cb_save(data, event_type, label):
    ctype = data['content_type']
    item  = data['item']
    items = load_items(ctype)
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    found = False
    for i in items:
        if i['id'] == item['id']:
            # ─── Версионирование ───
            # Сравниваем контент с последней версией
            old_versions = i.get('versions', [])
            last = old_versions[-1] if old_versions else None
            content_changed = (
                item.get('content', '') != i.get('content', '') or
                item.get('title', '') != i.get('title', '')
            )
            if content_changed:
                # Сохраняем предыдущее состояние как версию
                version = {
                    'saved_at': i.get('updated_at', now),
                    'title': i.get('title', ''),
                    'content': i.get('content', ''),
                    'tags': i.get('tags', ''),
                }
                old_versions.append(version)
                # Оставляем не больше 20 версий
                if len(old_versions) > 20:
                    old_versions = old_versions[-20:]
                item['versions'] = old_versions

            i.update(item)
            i['updated_at'] = now
            found = True
            break
    if not found:
        item['created_at'] = now
        item['updated_at'] = now
        items.append(item)

    save_items(ctype, items)

    # Экспорт
    fmt = data.get('export_format', 'html')
    exp_path = data.get('export_path', EXPORT_DIR)
    exported = None
    if fmt in EXPORTERS and item.get('title', '').strip():
        try:
            if fmt == 'pdf':
                # Для PDF: отправляем HTML в JS для печати через iframe
                html_content = export_html(item, None, return_html=True)
                app.emit('ui:print-pdf', {'html': html_content}, label)
                exported = os.path.join(exp_path, re.sub(r'[\\/*?:"<>|]', '_', item['title'])[:80] + '.pdf')
            else:
                exported = EXPORTERS[fmt](item, exp_path)
        except Exception as e:
            app.emit('ui:toast', {'message': f'Ошибка экспорта: {str(e)}'}, label)

    app.emit('ui:render-list', {'items': items, 'type': ctype}, label)
    if data.get('auto'):
        app.emit('ui:auto-saved', {}, label)
    else:
        msg = 'Сохранено'
        if exported:
            msg += f' · {os.path.basename(exported)}'
        app.emit('ui:toast', {'message': msg}, label)

def cb_delete(data, event_type, label):
    ctype   = data['content_type']
    item_id = data['id']
    items = load_items(ctype)
    # Find the item being deleted to remove its export file
    deleted_item = None
    for i in items:
        if i['id'] == item_id:
            deleted_item = i
            break
    if deleted_item:
        title = deleted_item.get('title', '')
        if title.strip():
            # Delete export files matching this title
            base_name = re.sub(r'[\\/*?:"<>|]', '_', title)[:80]
            for ext in ('.html', '.md', '.txt'):
                fpath = os.path.join(EXPORT_DIR, base_name + ext)
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        # Delete media files referenced in the document content
        content = deleted_item.get('content', '')
        if content:
            # Find all <img> tags with alt attribute (contains original filename)
            for m in re.finditer(r'<img[^>]+alt="([^"]*)"', content):
                img_name = m.group(1).strip()
                if img_name:
                    # Apply same sanitization as cb_image_upload
                    safe_name = re.sub(r'[^\w\s\-.а-яА-ЯёЁ]', '_', img_name, flags=re.UNICODE)
                    safe_name = re.sub(r'\s+', '_', safe_name).strip('_.')
                    if not safe_name:
                        continue
                    fpath = os.path.join(MEDIA_DIR, safe_name)
                    if os.path.exists(fpath):
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass
    items = [i for i in items if i['id'] != item_id]
    save_items(ctype, items)
    app.emit('ui:render-list', {'items': items, 'type': ctype}, label)

def cb_get(data, event_type, label):
    ctype   = data['content_type']
    item_id = data['id']
    items = load_items(ctype)
    for i in items:
        if i['id'] == item_id:
            app.emit('ui:open-editor', {'item': i}, label)
            return
    app.emit('pywry:alert', {'message': 'Материал не найден'}, label)

def cb_get_versions(data, event_type, label):
    """Возвращает список версий для указанного материала."""
    ctype   = data['content_type']
    item_id = data['id']
    items = load_items(ctype)
    for i in items:
        if i['id'] == item_id:
            versions = i.get('versions', [])
            app.emit('ui:versions', {'versions': versions, 'id': item_id}, label)
            return
    app.emit('ui:versions', {'versions': [], 'id': item_id}, label)

def cb_restore_version(data, event_type, label):
    """Восстанавливает содержимое материала из указанной версии."""
    ctype   = data['content_type']
    item_id = data['id']
    version_idx = data.get('version_idx', -1)
    items = load_items(ctype)
    for i in items:
        if i['id'] == item_id:
            versions = i.get('versions', [])
            if 0 <= version_idx < len(versions):
                v = versions[version_idx]
                # Обновляем текущий материал из версии
                i['title'] = v.get('title', i['title'])
                i['content'] = v.get('content', i['content'])
                i['tags'] = v.get('tags', i['tags'])
                # Удаляем версию из списка (чтобы не плодить дубли)
                versions.pop(version_idx)
                i['versions'] = versions
                now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
                i['updated_at'] = now
                save_items(ctype, items)
                app.emit('ui:open-editor', {'item': i}, label)
                app.emit('ui:toast', {'message': 'Версия восстановлена'}, label)
                return
    app.emit('ui:toast', {'message': 'Версия не найдена'}, label)

def cb_image_upload(data, event_type, label):
    """Сохраняет загруженное изображение в data/media/ с оригинальным именем (sanitized)."""
    import base64
    img_data = data.get('data', '')
    filename = data.get('filename', 'image.png')

    if img_data.startswith('data:image'):
        raw = base64.b64decode(img_data.split(',')[1])
        # Sanitize filename: remove path separators, keep latin/cyrillic/digits/hyphen/underscore/dot
        safe_name = re.sub(r'[^\w\s\-.а-яА-ЯёЁ]', '_', filename, flags=re.UNICODE)
        safe_name = re.sub(r'\s+', '_', safe_name).strip('_.')
        if not safe_name:
            safe_name = 'image.png'
        # Handle name collision: append counter if file exists
        name = safe_name
        counter = 1
        while os.path.exists(os.path.join(MEDIA_DIR, name)):
            base, ext = os.path.splitext(safe_name)
            name = f'{base}_{counter}{ext}'
            counter += 1
        path = os.path.join(MEDIA_DIR, name)
        with open(path, 'wb') as f:
            f.write(raw)
        app.emit('ui:image-saved', {'path': path, 'name': name}, label)

def cb_list_media(data, event_type, label):
    """Возвращает список изображений в data/media/ с base64 данными."""
    import base64
    files = []
    if os.path.exists(MEDIA_DIR):
        for fname in os.listdir(MEDIA_DIR):
            fpath = os.path.join(MEDIA_DIR, fname)
            if os.path.isfile(fpath):
                ext = fname.rsplit('.', 1)[-1].lower()
                if ext in ('png','jpg','jpeg','gif','webp','svg','bmp'):
                    size = os.path.getsize(fpath)
                    mtime = os.path.getmtime(fpath)
                    # load base64 (limit to first 200KB to keep response reasonable)
                    raw = open(fpath, 'rb').read()
                    mime_map = {'png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','gif':'image/gif','webp':'image/webp','svg':'image/svg+xml','bmp':'image/bmp'}
                    mime = mime_map.get(ext, 'image/png')
                    b64 = base64.b64encode(raw).decode('ascii')
                    files.append({'name': fname, 'size': size, 'mtime': mtime, 'ext': ext, 'data': f'data:{mime};base64,{b64}'})
    files.sort(key=lambda x: x['mtime'], reverse=True)
    app.emit('ui:media-list', {'files': files}, label)

def cb_get_media_base64(data, event_type, label):
    """Возвращает base64 указанного файла из data/media/."""
    import base64
    fname = data.get('name', '')
    if not fname:
        return
    fpath = os.path.join(MEDIA_DIR, fname)
    if not os.path.exists(fpath):
        app.emit('ui:toast', {'message': 'Файл не найден'}, label)
        return
    with open(fpath, 'rb') as f:
        raw = f.read()
    ext = fname.rsplit('.', 1)[-1].lower()
    mime_map = {'png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','gif':'image/gif','webp':'image/webp','svg':'image/svg+xml','bmp':'image/bmp'}
    mime = mime_map.get(ext, 'image/png')
    b64 = base64.b64encode(raw).decode('ascii')
    app.emit('ui:media-base64', {'name': fname, 'data': f'data:{mime};base64,{b64}'}, label)

def cb_delete_media(data, event_type, label):
    """Удаляет файл из data/media/."""
    fname = data.get('name', '')
    if not fname:
        return
    fpath = os.path.join(MEDIA_DIR, fname)
    if os.path.exists(fpath):
        os.remove(fpath)
    app.emit('ui:media-deleted', {'name': fname}, label)

def cb_window_action(data, event_type, label):
    """Управление окном из HTML (закрыть, свернуть, развернуть, изменить размер)."""
    action = data.get('action', '')
    if action == 'close':
        win.close()
    elif action == 'minimize':
        win.minimize()
    elif action == 'maximize':
        win.maximize()
    elif action == 'restore':
        w = data.get('width', 1100)
        h = data.get('height', 700)
        win.set_size(max(800, w), max(400, h))
    elif action == 'resize':
        w = data.get('width', 1100)
        h = data.get('height', 700)
        win.set_size(max(800, w), max(400, h))

def cb_window_bg(data, event_type, label):
    """Синхронизация фона окна с темой."""
    rgb = data.get('rgb', '240,240,241')
    parts = rgb.split(',')
    if len(parts) == 3:
        win.set_background_color(int(parts[0]), int(parts[1]), int(parts[2]))

# ─── UI ─────────────────────────────────────────────────────
UI = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PW Editor</title>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg-body:#f0f0f1; --bg-panel:#fff; --bg-input:#fff; --bg-hover:#f0f0f1; --bg-hover2:#e5e5e5; --bg-hover3:#dcdcde;
  --bg-muted:#f6f7f7; --bg-active:#e5f5fa; --bg-code:#f0f0f1; --bg-selected:#fef8ee; --bg-danger:#fcf0f1;
  --text-primary:#1d2327; --text-secondary:#50575e; --text-tertiary:#3c434a; --text-invert:#fff;
  --border:#c3c4c7; --border-light:#dcdcde;
  --accent:#2271b1; --accent-hover:#135e96; --accent-text:#2271b1;
  --danger:#e65054; --danger-hover:#b32d2e;
  --status-ok:#68de7c; --status-err:#f1adad; --status-load:#c5d9ed;
  --shadow:rgba(0,0,0,.1); --shadow-md:rgba(0,0,0,.15); --shadow-lg:rgba(0,0,0,.2); --overlay:rgba(0,0,0,.5);
  --font-mono:'Courier New',monospace;
}
body.theme-dark{
  --bg-body:#1e1e2e; --bg-panel:#2a2a3e; --bg-input:#363650; --bg-hover:#32324a; --bg-hover2:#3a3a52; --bg-hover3:#434360;
  --bg-muted:#363650; --bg-active:#2a3a5e; --bg-code:#2a2a3e; --bg-selected:#2a3a3e; --bg-danger:#3a2030;
  --text-primary:#e0e0e8; --text-secondary:#a0a0b8; --text-tertiary:#c0c0d0; --text-invert:#1e1e2e;
  --border:#3a3a50; --border-light:#4a4a60;
  --accent:#7aa2f7; --accent-hover:#5a8df5; --accent-text:#7aa2f7;
  --danger:#f7768e; --danger-hover:#e05a70;
  --status-ok:#3a9b50; --status-err:#c05050; --status-load:#3a5a80;
  --shadow:rgba(0,0,0,.3); --shadow-md:rgba(0,0,0,.4); --shadow-lg:rgba(0,0,0,.5); --overlay:rgba(0,0,0,.7);
}
body.theme-modern{
  --bg-body:#0d1117; --bg-panel:#161b22; --bg-input:#21262d; --bg-hover:#1c2128; --bg-hover2:#292e36; --bg-hover3:#343941;
  --bg-muted:#21262d; --bg-active:#1a2433; --bg-code:#161b22; --bg-selected:#1a2433; --bg-danger:#2d1c1c;
  --text-primary:#c9d1d9; --text-secondary:#8b949e; --text-tertiary:#b0b8c4; --text-invert:#0d1117;
  --border:#30363d; --border-light:#21262d;
  --accent:#58a6ff; --accent-hover:#388bfd; --accent-text:#58a6ff;
  --danger:#f85149; --danger-hover:#da3633;
  --status-ok:#3fb950; --status-err:#da3633; --status-load:#2a5a80;
  --shadow:rgba(0,0,0,.4); --shadow-md:rgba(0,0,0,.5); --shadow-lg:rgba(0,0,0,.6); --overlay:rgba(0,0,0,.8);
}
body.theme-sepia{
  --bg-body:#e8dcc4; --bg-panel:#fcf5e3; --bg-input:#fff9ed; --bg-hover:#f0e6d0; --bg-hover2:#ece0c8; --bg-hover3:#e8dcc0;
  --bg-muted:#f5e6c8; --bg-active:#f0e6cc; --bg-code:#f0e6d0; --bg-selected:#f5edd8; --bg-danger:#f5e0e0;
  --text-primary:#3a2e1e; --text-secondary:#6b5d4e; --text-tertiary:#5a4d3e; --text-invert:#fff;
  --border:#c8b898; --border-light:#d8ccb8;
  --accent:#8b6914; --accent-hover:#a67d2e; --accent-text:#8b6914;
  --danger:#b33a3a; --danger-hover:#992e2e;
  --status-ok:#5a8a3a; --status-err:#b05050; --status-load:#8a7a40;
  --shadow:rgba(0,0,0,.12); --shadow-md:rgba(0,0,0,.18); --shadow-lg:rgba(0,0,0,.25); --overlay:rgba(0,0,0,.5);
}
html{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,sans-serif;background:var(--bg-body);color:var(--text-primary);position:fixed;top:0;left:0;right:0;bottom:0;margin:0;display:flex;flex-direction:column;font-size:13px;overflow:hidden}
.main-wrap{display:flex;flex:1;min-height:0;min-width:100vw;overflow:hidden}
.win-controls{display:flex;align-items:center;gap:0;margin-left:8px;flex-shrink:0}
.win-controls button{background:none;border:none;width:36px;height:36px;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:14px;border-radius:0;color:var(--text-secondary);transition:background .1s;-webkit-app-region:no-drag}
.win-controls button:hover{background:var(--bg-hover);color:var(--text-primary)}
.win-controls .btn-close:hover{background:var(--danger);color:#fff}
/* Resize grip for frameless window */
.resize-grip{position:fixed;right:0;bottom:0;width:14px;height:14px;cursor:nwse-resize;z-index:9999;-webkit-app-region:no-drag}
.resize-grip::after{content:'';position:absolute;right:3px;bottom:3px;width:8px;height:8px;border-right:2px solid var(--text-secondary);border-bottom:2px solid var(--text-secondary);opacity:.4}
/* Language chips for translate modal */
.lang-chip{display:flex;align-items:center;justify-content:center;padding:6px 4px;border:1px solid var(--border);border-radius:6px;font-size:12px;cursor:pointer;color:var(--text-primary);background:var(--bg-body);transition:background .15s,border-color .15s;user-select:none}
.lang-chip:hover{background:var(--bg-hover);border-color:var(--accent)}
/* Top bar */
.top-bar{background:var(--bg-panel);border-bottom:1px solid var(--border);display:flex;align-items:center;height:52px;padding:0 16px;flex-shrink:0;z-index:100;-webkit-app-region:drag}
.top-bar .brand{font-size:16px;font-weight:700;color:var(--text-primary);display:flex;align-items:center;gap:8px;margin-right:20px}
.auto-save-indicator{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 6px;vertical-align:middle;background:var(--status-ok);transition:background .3s;flex-shrink:0}
.top-bar .brand span{color:var(--accent);-webkit-app-region:no-drag}
.type-selector select{appearance:none;-webkit-appearance:none;padding:6px 28px 6px 12px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-muted);cursor:pointer;min-width:140px;background-image:url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1L5 5L9 1' stroke='%2350575e' stroke-width='1.5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 8px center;color:var(--text-primary);-webkit-app-region:no-drag}
.top-bar .actions{display:flex;align-items:center;gap:4px;margin-left:auto}
.actions .act-sep{width:1px;height:20px;background:var(--border);margin:0 4px;flex-shrink:0}
.top-bar .actions button{background:none;border:1px solid transparent;border-radius:4px;padding:6px 10px;cursor:pointer;font-size:16px;line-height:1;transition:all .15s;color:var(--text-secondary);-webkit-app-region:no-drag}
.top-bar .actions button:hover{background:var(--bg-body);border-color:var(--border);color:var(--text-primary)}
.top-bar .actions button.active{background:var(--bg-body);border-color:var(--border)}
.btn-save{background:var(--accent)!important;color:#fff!important;padding:6px 16px!important;border-radius:4px!important;font-size:13px!important;font-weight:500!important;border:none!important;-webkit-app-region:no-drag}
.btn-save:hover{background:var(--accent-hover)!important;color:#fff!important}
/* Main */
.main-wrap{display:flex;flex:1;min-height:0;overflow:hidden}
/* Side panel */
.side-panel{width:180px;min-width:180px;background:var(--bg-panel);border-right:1px solid var(--border);display:flex;flex-direction:column;position:relative;transition:width .25s,min-width .25s}
.side-panel.hidden{width:0;min-width:0;overflow:hidden;border-right:none;padding:0}
.side-panel.hidden .resize-handle{display:none}
.side-panel.no-transition{transition:none!important}
.resize-handle{position:absolute;right:-3px;top:0;bottom:0;width:6px;cursor:col-resize;z-index:20;background:transparent}
.resize-handle:hover,.resize-handle.active{background:var(--accent);opacity:.3}
.side-header{padding:14px 16px;border-bottom:1px solid var(--border-light);display:flex;justify-content:space-between;align-items:center}
.side-header h3{font-size:13px;font-weight:600;color:var(--text-primary)}
.btn-add{background:var(--accent);color:#fff;border:none;width:28px;height:28px;border-radius:4px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center}
.btn-add:hover{background:var(--accent-hover)}
.side-list{flex:1;overflow-y:auto;padding:4px 0}
.side-search{padding:4px 12px;display:flex;gap:4px;align-items:center}
.side-search input{flex:1;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;background:var(--bg-input);color:var(--text-primary);outline:none;min-width:0}
.side-search input:focus{border-color:var(--accent)}
.s-item{padding:12px 16px;cursor:pointer;border-bottom:1px solid var(--bg-body);position:relative;color:var(--text-primary)}
.s-item:hover{background:var(--bg-muted)}
.s-item.active{background:var(--bg-active)}
.s-item.active::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.s-item .s-title{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:36px}
.s-item .s-meta{font-size:11px;color:var(--text-secondary);margin-top:2px}
.s-item .s-del{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:14px;color:var(--text-secondary);display:none;padding:4px;border-radius:3px}
.s-item:hover .s-del{display:block}
.s-item .s-del:hover{color:var(--danger);background:var(--bg-danger)}
.side-empty{padding:40px 20px;text-align:center;color:var(--text-secondary);font-size:13px;line-height:1.6}
/* Editor */
.editor-area{flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg-body);min-width:0;position:relative}
.toggle-side-btn,.toggle-sett-btn{position:absolute;top:8px;z-index:30;background:var(--bg-panel);border:1px solid var(--border);border-radius:5px;height:32px;width:32px;padding:0;cursor:pointer;font-size:16px;color:var(--text-secondary);display:flex;align-items:center;justify-content:center;opacity:.7;transition:opacity .2s}
.toggle-side-btn:hover,.toggle-sett-btn:hover{opacity:1;color:var(--accent);background:var(--bg-hover);border-color:var(--accent)}
.toggle-side-btn{left:4px}
.toggle-sett-btn{right:4px}
.editor-center{flex:1;overflow-y:auto;padding:24px 16px;min-width:0;min-height:0}
.editor-canvas{min-height:100%}
.block-editor{position:relative;margin-top:8px;min-height:200px;padding:0 4px}
.wp-title{margin-bottom:16px}
.wp-title input{width:100%;border:none;outline:none;font-size:32px;font-weight:700;line-height:1.3;padding:8px 0;background:transparent;color:var(--text-primary);font-family:inherit}
.wp-title input::placeholder{color:var(--text-secondary);font-weight:400}
.wp-title .title-meta{font-size:11px;color:var(--text-secondary);margin-top:4px}
.wp-title .tags-row{margin-top:8px}
.wp-title .tags-row input{font-size:12px;font-weight:400;padding:4px 0;color:var(--text-secondary)}
.wp-title .tags-row input:focus{color:var(--text-primary)}
/* Blocks */
.block-editor{position:relative;flex:1;min-height:0;padding:0 4px}
.block{position:relative;margin-bottom:1px;padding:2px 16px;border-radius:4px;border:1px solid transparent;transition:border-color .15s,background .15s;cursor:text}
.block:hover{border-color:var(--border);background:var(--bg-panel)}
.block.selected{border-color:var(--accent);background:var(--bg-panel);box-shadow:0 0 0 1px var(--accent)}
.block-adder{height:16px;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .15s;cursor:pointer;position:relative;margin:0}
.block-adder:hover,.block-adder.show{opacity:1}
.block-adder::before{content:'';position:absolute;left:0;right:0;height:1px;background:var(--border)}
.block-adder button{position:relative;z-index:1;width:24px;height:24px;border-radius:50%;background:var(--bg-panel);border:1px solid var(--border);cursor:pointer;font-size:14px;line-height:1;display:flex;align-items:center;justify-content:center;color:var(--text-secondary);transition:all .15s}
.block-adder button:hover{background:var(--accent);border-color:var(--accent);color:#fff}
/* Drag & Drop */
.drag-handle{cursor:grab;opacity:0.3;font-size:14px;padding:0 4px;user-select:none;background:none;border:none;color:var(--text-secondary);display:inline-block;line-height:1}
.block:hover .drag-handle,.block.selected .drag-handle{opacity:0.8}
.drag-handle:active{cursor:grabbing;opacity:1}
.block.drag-over{border-color:var(--accent);border-style:dashed;background:var(--bg-muted)}
.editor-hint{font-size:11px;color:var(--text-secondary);padding:4px 16px;margin-bottom:2px;text-align:center}
.editor-stats{font-size:11px;color:var(--text-secondary);padding:2px 16px 6px;display:flex;gap:16px;flex-wrap:wrap;border-top:1px solid var(--border-light);margin-top:4px}
.editor-stats span{white-space:nowrap}
.block.paragraph{font-size:16px;line-height:1.35;color:var(--text-primary)}
.block.paragraph [contenteditable]{min-height:0.5em;outline:none;padding:2px 0}
.block.heading [contenteditable]{outline:none;font-weight:600;color:var(--text-primary);min-height:0.5em;padding:2px 0}
.block.heading.h2 [contenteditable]{font-size:24px;line-height:1.4}
.block.heading.h3 [contenteditable]{font-size:20px;line-height:1.4}
.block.list ul,.block.list ol{margin-left:20px;font-size:16px;line-height:1.7;color:var(--text-primary)}
.block.quote{border-left:4px solid var(--accent);padding:12px 20px;background:var(--bg-body);font-size:16px;font-style:italic;line-height:1.7;color:var(--text-primary)}
.block.quote [contenteditable]{outline:none}
.block.separator{text-align:center;padding:4px 0;cursor:default}
.block.separator hr{border:none;border-top:1px solid var(--border);margin:0;width:100%;height:1px}
.block.image{background:var(--bg-body);border:1px dashed var(--border);text-align:center;padding:12px;border-radius:4px;color:var(--text-secondary);font-size:14px;cursor:pointer;resize:both;overflow:hidden;min-width:100px}
.block.image img{max-width:100%;max-height:300px;border-radius:4px;display:block;margin:0 auto}
.block.image .img-placeholder{padding:24px;color:var(--text-secondary)}
.block.image .img-placeholder button{background:var(--accent);color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:8px}
.block.image .img-placeholder button:hover{background:var(--accent-hover)}
.block.image .img-actions{margin-top:8px;display:flex;gap:6px;justify-content:center}
.block.image .img-actions button{background:var(--bg-body);border:1px solid var(--border);border-radius:4px;padding:4px 12px;cursor:pointer;font-size:11px}
.block.image .img-actions button:hover{background:var(--border-light)}
/* Inline images in contenteditable */
.block [contenteditable]{overflow:auto;overflow-wrap:break-word;word-wrap:break-word;word-break:break-word}
.inline-img{cursor:pointer;border:2px solid transparent;border-radius:4px;transition:border-color .15s}
.inline-img:hover{border-color:var(--accent)}
.inline-img:focus,.inline-img:active{border-color:var(--accent);outline:none}
.block [contenteditable] img{max-width:100%;height:auto}
.img-pop-btn{padding:5px 12px;border-radius:4px;cursor:pointer;font-size:11px;border:1px solid var(--border);background:var(--bg-muted);color:var(--text-primary)}
.img-pop-btn:hover{background:var(--bg-hover2)}
.img-pop-btn.del{border-color:var(--danger);color:var(--danger);background:var(--bg-panel)}
.img-pop-btn.del:hover{background:var(--bg-danger)}
/* Inline image alignment */
.block.paragraph img[style*="float: left"], .block.paragraph img[align="left"]{float:left;margin:4px 16px 8px 0;max-width:50%;border-radius:4px}
.block.paragraph img[style*="float: right"], .block.paragraph img[align="right"]{float:right;margin:4px 0 8px 16px;max-width:50%;border-radius:4px}
/* Image popup */
.img-popup{position:fixed;background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;box-shadow:0 4px 20px rgba(0,0,0,.15);z-index:9999;font-size:12px;min-width:200px}
.img-popup-row{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.img-popup-label{color:var(--text-secondary)}
.img-popup-value{min-width:40px;text-align:right;color:var(--text-primary)}
.img-popup-actions{display:flex;gap:6px;flex-wrap:wrap}

.block.paragraph img[style*="display: block"], .block.paragraph img[align="center"]{display:block;margin:16px auto;max-width:100%;border-radius:4px}
.block.code{background:var(--bg-body);border:1px solid var(--border);font-family:'Courier New',monospace;font-size:14px;line-height:1.6;padding:16px;color:var(--text-primary)}
.block.code [contenteditable]{outline:none;white-space:pre}
/* Block toolbar */
.block-toolbar{position:absolute;top:-40px;left:0;z-index:50;display:none;align-items:center;gap:2px;background:var(--bg-panel);border:1px solid var(--border);border-radius:4px;padding:3px;box-shadow:0 2px 6px rgba(0,0,0,.1);height:34px}
.block.selected .block-toolbar{display:flex}
.block-toolbar.fixed{position:fixed;z-index:200;top:56px;left:auto;right:auto;display:flex;width:auto}
.block-toolbar button{background:none;border:none;border-radius:3px;padding:3px 8px;cursor:pointer;font-size:12px;line-height:1.4;color:var(--text-secondary);white-space:nowrap}
.block-toolbar button:hover{background:var(--bg-body);color:var(--text-primary)}
.block-toolbar .sep{width:1px;height:20px;background:var(--border);margin:0 2px}
.block-toolbar button.type-active{background:var(--bg-active);color:var(--accent)}
.block-toolbar button.has-popup{position:relative}
/* Toolbar popups */
.tb-popup{display:none;position:absolute;top:100%;left:0;z-index:100;background:var(--bg-panel);border:1px solid var(--border);border-radius:6px;padding:4px;box-shadow:0 4px 16px rgba(0,0,0,.15);min-width:180px;margin-top:4px}
.tb-popup-item{padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;color:var(--text-primary);white-space:nowrap}
.tb-popup-item:hover{background:var(--bg-body)}
.tb-popup-sep{height:1px;margin:4px 0;background:var(--border)}
.tb-popup-img{left:auto;right:0}
.tb-popup-ai{left:0;right:auto;z-index:200}
.tb-popup-color{left:auto;right:0;min-width:140px;padding:8px}
.tb-popup-title{font-size:11px;color:var(--text-secondary);margin-bottom:6px;text-align:center}
.color-swatch{display:inline-block;width:20px;height:20px;border-radius:3px;cursor:pointer;margin:2px;border:1px solid var(--border);vertical-align:middle}
.color-swatch:hover{transform:scale(1.2);border-color:var(--accent)}
/* Settings bar */
.settings-bar{width:280px;min-width:280px;background:var(--bg-panel);border-left:1px solid var(--border);display:flex;flex-direction:column;position:relative;overflow-y:auto;transition:width .25s,min-width .25s}
.settings-bar.hidden{width:0;min-width:0;overflow:hidden;border-left:none;padding:0}
.settings-bar.hidden .resize-handle-settings{display:none}
.settings-bar.no-transition{transition:none!important}
.resize-handle-settings{position:absolute;left:-3px;top:0;bottom:0;width:6px;cursor:col-resize;z-index:20;background:transparent}
.resize-handle-settings:hover,.resize-handle-settings.active{background:var(--accent);opacity:.3}
.sett-tabs{display:flex;border-bottom:1px solid var(--border-light)}
.sett-tabs button{flex:1;padding:12px;border:none;cursor:pointer;font-size:12px;font-weight:500;background:none;color:var(--text-secondary);border-bottom:2px solid transparent;transition:all .15s}
.sett-tabs button:hover{background:var(--bg-body);color:var(--text-primary)}
.sett-tabs button.active{color:var(--text-primary);border-bottom-color:var(--accent);font-weight:600}
.sett-body{padding:16px;color:var(--text-primary);flex:1;overflow-y:auto}
.sett-group{margin-bottom:20px}
.sett-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--text-secondary);margin-bottom:8px}
.sett-field{margin-bottom:14px}
.sett-field label{display:block;font-size:12px;font-weight:500;color:var(--text-primary);margin-bottom:3px}
.sett-field select,.sett-field input[type="text"],.sett-field textarea{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-input);font-family:inherit;color:var(--text-primary)}
.sett-field select:focus,.sett-field input[type="text"]:focus,.sett-field textarea:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 1px var(--accent)}
.sett-field textarea{resize:vertical;min-height:60px}
.sett-field .info-text{font-size:11px;color:var(--text-secondary);margin-top:3px;word-break:break-all}
.sett-field .path-row{display:flex;gap:6px}
.sett-field .path-row input{flex:1}
.sett-field .path-row button{padding:6px 12px;border:1px solid var(--border);border-radius:4px;background:var(--bg-body);cursor:pointer;font-size:14px;color:var(--text-secondary)}
/* AI settings collapse & key visibility */
.key-row{display:flex;gap:4px;align-items:center}
.key-row input{flex:1;font-family:monospace}
.sett-field input[type="password"]{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-input);font-family:inherit;color:var(--text-primary)}
.sett-field input[type="password"]:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 1px var(--accent)}
.btn-eye{padding:2px 8px;cursor:pointer;border:1px solid var(--border);border-radius:4px;background:var(--bg-muted);font-size:14px;line-height:1.6;white-space:nowrap}
.btn-eye:hover{background:var(--bg-hover2)}
.collapsible-title{display:flex;align-items:center;justify-content:space-between;padding:0 0 10px 0;font-weight:600;font-size:13px;color:var(--text-primary)}
.collapse-btn{background:none;border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:10px;padding:2px 8px;color:var(--text-secondary);line-height:1.4;transition:transform .2s}
.collapse-btn:hover{background:var(--bg-body)}
.collapse-btn.collapsed{transform:rotate(180deg)}
.sett-field .path-row button:hover{background:var(--border-light);color:var(--text-primary)}
.btn-prim{width:100%;padding:8px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500;margin-top:6px}
.btn-prim:hover{background:var(--accent-hover)}
.btn-sec{width:100%;padding:8px;background:var(--bg-body);color:var(--text-primary);border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:13px;margin-top:6px}
.btn-sec:hover{background:var(--border-light)}
.ai-actions{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.ai-actions button{flex:1;min-width:70px;padding:6px 4px;border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:11px;background:var(--bg-muted);color:var(--text-primary);text-align:center}
.ai-actions button:hover{background:var(--border-light);border-color:var(--accent)}
.ai-result{background:var(--bg-muted);border-radius:4px;padding:10px;margin-top:8px;font-size:12px;line-height:1.5;max-height:200px;overflow-y:auto;white-space:pre-wrap}
.ai-result.error{color:var(--danger);background:var(--bg-danger)}
/* Table block */
.block.table table{width:100%;border-collapse:collapse;background:var(--bg-panel);font-size:14px;line-height:1.4}
.block.table td{border:1px solid var(--border);padding:6px 10px;min-width:60px;outline:none;vertical-align:top;color:var(--text-primary)}
.block.table td:focus{background:var(--bg-active);box-shadow:inset 0 0 0 1px var(--accent)}
/* Column action bar */
.block.table .tb-col-actions td{border:none;padding:1px 2px;text-align:center;vertical-align:middle;min-width:auto;height:22px;background:var(--bg-panel)}
.block.table .tb-col-btn,.block.table .tb-row-btn{width:30px;min-width:30px;padding:1px 0;text-align:center;vertical-align:middle}
.block.table .tb-corner{border:none;width:30px;min-width:30px}
.block.table .tb-col-actions button,.block.table .tb-row-btn button,.block.table .tb-col-btn button{border:none;background:none;cursor:pointer;font-size:11px;padding:2px 3px;border-radius:4px;color:var(--text-secondary);line-height:1;opacity:0.7;transition:opacity .1s,background .1s,transform .1s}
.block.table .tb-col-actions button:hover,.block.table .tb-row-btn button:hover,.block.table .tb-col-btn button:hover{opacity:1;background:var(--bg-hover2);color:var(--text-primary);transform:scale(1.15)}
.block.table .tb-col-actions button:active,.block.table .tb-row-btn button:active,.block.table .tb-col-btn button:active{background:var(--accent);color:#fff;opacity:1;transform:scale(1)}
.block.table .tb-col-actions button:hover,.block.table .tb-row-btn button:hover,.block.table .tb-col-btn button:hover{opacity:1;background:var(--bg-body);color:var(--text-primary)}
.block.table .tb-col-actions button:active,.block.table .tb-row-btn button:active,.block.table .tb-col-btn button:active{background:var(--border-light)}
/* Font size selector */
.fs-select{border:1px solid var(--border);border-radius:4px;padding:2px 4px;font-size:12px;background:var(--bg-input);color:var(--text-primary);cursor:pointer;outline:none;height:24px}
.fs-select:hover{border-color:var(--accent)}
.fs-select option{font-size:12px}
/* Save dialog */
.save-dialog{display:none;position:fixed;z-index:300;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.4);justify-content:center;align-items:center}
.save-dialog.show{display:flex}
.save-panel{background:var(--bg-panel);border-radius:8px;padding:28px;width:400px;box-shadow:0 8px 32px rgba(0,0,0,.2)}
.save-panel h3{font-size:18px;font-weight:600;margin-bottom:16px}
.save-panel label{display:block;font-size:12px;font-weight:500;color:var(--text-primary);margin-bottom:3px;margin-top:12px}
.save-panel select,.save-panel input{width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-input);color:var(--text-primary);font-family:inherit}
.save-panel .save-actions{display:flex;gap:8px;margin-top:20px}
.save-panel .save-actions button{flex:1;padding:10px;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500}
/* Inserter */
.inserter-popup{display:none;position:fixed;z-index:200;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.4);justify-content:center;align-items:center}
.inserter-popup.show{display:flex}
.inserter-panel{background:var(--bg-panel);border-radius:8px;padding:24px;width:380px;max-height:500px;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.2)}
.inserter-panel h3{font-size:16px;font-weight:600;margin-bottom:16px;color:var(--text-primary)}
.inserter-block{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:4px;cursor:pointer;color:var(--text-primary)}
.inserter-block:hover{background:var(--bg-body)}
.inserter-block .ib-icon{width:36px;height:36px;display:flex;align-items:center;justify-content:center;background:var(--bg-body);border-radius:4px;font-size:18px;flex-shrink:0}
.inserter-block .ib-label{font-size:14px;font-weight:500}
.inserter-block .ib-desc{font-size:11px;color:var(--text-secondary);margin-top:2px}
/* Preview */
.preview-overlay{display:none;position:fixed;z-index:300;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);overflow-y:auto}
.preview-overlay.show{display:block}
/* Media Manager */
.media-overlay{display:none;position:fixed;z-index:400;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);overflow-y:auto}
.media-overlay.show{display:block}
.media-content{background:var(--bg-panel);max-width:900px;margin:40px auto;border-radius:8px;padding:24px;box-shadow:0 4px 24px rgba(0,0,0,.2)}
.media-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.media-header h2{font-size:18px;color:var(--text-primary)}
.media-close{background:none;border:none;font-size:24px;cursor:pointer;color:var(--text-secondary);padding:4px 8px;border-radius:4px}
.media-close:hover{background:var(--bg-body)}
.media-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
.media-item{border:1px solid var(--border-light);border-radius:6px;overflow:hidden;cursor:pointer;position:relative;background:var(--bg-muted);transition:border-color .15s}
.media-item:hover{border-color:var(--accent)}
.media-item img{width:100%;height:140px;object-fit:cover;display:block}
.media-item .media-info{padding:6px 8px;font-size:11px;color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.media-item .media-del{position:absolute;top:4px;right:4px;background:rgba(0,0,0,.5);color:#fff;border:none;border-radius:50%;width:24px;height:24px;font-size:14px;cursor:pointer;display:none;align-items:center;justify-content:center;line-height:1}
.media-item:hover .media-del{display:flex}
.media-item .media-del:hover{background:rgba(200,30,30,.8)}
.media-empty{text-align:center;padding:40px;color:var(--text-secondary);font-size:14px}
.preview-bar{background:var(--text-primary);color:#fff;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:301}
.preview-bar .preview-title{font-size:15px;font-weight:500}
.preview-bar button{background:rgba(255,255,255,.15);border:none;color:#fff;padding:6px 16px;border-radius:4px;cursor:pointer;font-size:13px}
.preview-bar button:hover{background:rgba(255,255,255,.25)}
.preview-body{max-width:720px;margin:40px auto;padding:40px 48px 60px;font-family:-apple-system,'Segoe UI',sans-serif;color:var(--text-primary);line-height:1.7;font-size:16px;background:var(--bg-panel);border-radius:8px;box-shadow:0 2px 20px rgba(0,0,0,.15);overflow:hidden}
.preview-body h1{font-size:32px;font-weight:700;margin-bottom:8px;line-height:1.3}
.preview-body .preview-meta{color:var(--text-secondary);font-size:13px;margin-bottom:32px}
.preview-body h2{font-size:24px;margin-top:32px;margin-bottom:12px;font-weight:600}
.preview-body h3{font-size:20px;margin-top:28px;margin-bottom:8px;font-weight:600}
.preview-body p{font-size:16px;margin-bottom:16px;overflow:hidden}
.preview-body blockquote{border-left:4px solid var(--accent);padding-left:20px;margin:24px 0;color:var(--text-tertiary);font-style:italic}
.preview-body pre{background:var(--bg-body);padding:16px 20px;border-radius:6px;overflow-x:auto;font-size:14px;margin:24px 0}
.preview-body pre code{background:none;padding:0;font-size:14px}
.preview-body code{background:var(--bg-body);padding:2px 6px;border-radius:3px;font-size:14px}
.preview-body hr{border:none;border-top:1px solid var(--border-light);margin:32px 0}
.preview-body ul, .preview-body ol{padding-left:24px;margin:16px 0}
.preview-body li{margin-bottom:4px}
.preview-body img{max-width:100%;height:auto;border-radius:4px;margin:24px 0}
.preview-body figure{margin:24px 0}
.preview-body figure img{display:block}
.preview-body img[style*="float: left"]{float:left;margin:4px 16px 8px 0;max-width:50%}
.preview-body img[style*="float: right"]{float:right;margin:4px 0 8px 16px;max-width:50%}
.preview-body img[style*="display: block"]{display:block;margin:16px auto;max-width:100%}
.preview-body table{width:100%;border-collapse:collapse;margin:24px 0;font-size:14px}
.preview-body td,.preview-body th{border:1px solid var(--border);padding:8px 12px;text-align:left;vertical-align:top}
.preview-body th{background:var(--bg-body);font-weight:600}
/* Toast */
.toast-wrap{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px}
.toast{background:var(--text-primary);color:#fff;padding:10px 20px;border-radius:6px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.2);animation:slideIn .25s ease}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
/* AI */
.model-row{display:flex;flex-direction:column;gap:6px;width:100%}
.model-row select{width:100%}
.btn-row{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.btn-row button{flex:1;min-width:100px;font-size:12px!important;padding:6px 8px!important}
.ai-status{padding:8px 12px;border-radius:4px;margin-top:8px;font-size:12px;display:none}
.ai-status.ok{display:block;background:var(--bg-muted);border:1px solid var(--status-ok);color:var(--accent)}
.ai-status.err{display:block;background:var(--bg-danger);border:1px solid var(--status-err);color:var(--danger)}
.ai-status.loading{display:block;background:var(--bg-active);border:1px solid var(--status-load);color:var(--text-primary)}
/* Responsive */
@media(max-width:900px){
  .side-panel{width:140px;min-width:140px}
  .settings-bar{width:160px;min-width:160px}
  .editor-center{padding:16px 10px}
}
@media(max-width:640px){
  .top-bar .brand span{display:none}
  .top-bar .actions button{padding:4px 6px;font-size:14px}
  .side-panel{width:120px;min-width:120px;font-size:12px}
  .settings-bar{width:140px;min-width:140px}
  .side-header{padding:10px 12px}
  .editor-center{padding:12px 6px}
  .block{padding:2px 8px}
  .wp-title input{font-size:24px}
  .wp-title{margin-bottom:12px}
  .save-panel{width:90%;margin:20px auto;padding:20px}
  .media-content{margin:20px auto;padding:16px;max-width:95%}
  .media-grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr))}
}
@media(max-width:480px){
  .top-bar{padding:0 8px;height:44px}
  .top-bar .brand{font-size:14px;margin-right:12px}
  .type-selector select{min-width:100px;padding:4px 24px 4px 8px;font-size:12px}
  .side-panel{width:100px;min-width:100px}
  .settings-bar{width:120px;min-width:120px}
  .side-panel.hidden,.settings-bar.hidden{width:0;min-width:0}
  .editor-center{padding:8px 4px}
  .block{padding:2px 4px}
  .wp-title input{font-size:20px}
  .wp-title{margin-bottom:8px}
  .media-grid{grid-template-columns:repeat(auto-fill,minmax(80px,1fr))}
  .media-item img{height:80px}
  .win-controls button{width:30px;height:30px;font-size:12px}
}
/* Focus mode */
body.focus-mode .side-panel{width:0!important;min-width:0!important;overflow:hidden;border:none!important;padding:0!important}
body.focus-mode .settings-bar{width:0!important;min-width:0!important;overflow:hidden;border:none!important;padding:0!important}
body.focus-mode .side-panel .resize-handle{display:none}
body.focus-mode .settings-bar .resize-handle{display:none}
body.focus-mode .block-toolbar{opacity:.15;transition:opacity .3s}
body.focus-mode .block:hover .block-toolbar,
body.focus-mode .block.selected .block-toolbar{opacity:1}
body.focus-mode .auto-save-indicator{display:none}
body.focus-mode .win-controls{display:none}
body.focus-mode #btnFocus{background:var(--accent);color:#fff;border-radius:4px}
/* AI inline result — плавающая панель */
.ai-inline-result{position:fixed;bottom:20px;right:20px;width:380px;max-height:60vh;z-index:10000;border:1px solid var(--accent);border-radius:8px;background:var(--bg-panel);overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,0.35)}
.ai-inline-result.loading{border-color:var(--border);padding:12px 16px;font-size:13px;color:var(--text-secondary)}
.ai-inline-header{padding:8px 12px;font-size:12px;font-weight:600;color:var(--accent);background:var(--bg-muted);border-bottom:1px solid var(--border-light)}
.ai-inline-text{padding:12px;font-size:14px;line-height:1.6;color:var(--text-primary);max-height:200px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
.ai-inline-actions{display:flex;gap:4px;padding:6px 8px;border-top:1px solid var(--border-light)}
.ai-inline-actions button{padding:5px 12px;border:1px solid var(--border);border-radius:4px;background:var(--bg-body);cursor:pointer;font-size:12px;color:var(--text-primary)}
.ai-inline-actions button:hover{background:var(--bg-hover);border-color:var(--accent)}
/* Multi-selected block */
.block.multi-sel{outline:2px solid var(--accent);outline-offset:-2px;background:var(--bg-muted)}
/* AI chat bar — компактная строка */
.ai-chat-bar{display:flex;flex-direction:column;gap:2px;padding:4px 12px 8px;flex-shrink:0;z-index:50;background:var(--bg-panel);border-top:1px solid var(--border)}
.ai-chat-row{display:flex;gap:3px;align-items:center;flex-wrap:nowrap}
.ai-chat-provider{width:72px;flex-shrink:0;padding:2px 3px;font-size:11px;border:1px solid var(--border);border-radius:4px;background:var(--bg-body);color:var(--text-primary)}
.ai-chat-model{width:140px;flex-shrink:0;padding:2px 3px;font-size:11px;border:1px solid var(--border);border-radius:4px;background:var(--bg-body);color:var(--text-primary)}
.ai-chat-refresh{flex-shrink:0;padding:2px 5px;cursor:pointer;font-size:11px;border:1px solid var(--border);border-radius:4px;background:var(--bg-muted);line-height:1.4}
.ai-chat-refresh:hover{background:var(--bg-hover);border-color:var(--accent)}
.ai-chat-input{flex:1;min-width:60px;min-height:28px;max-height:60px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary);font-size:12px;font-family:inherit;resize:none;outline:none}
.ai-chat-input::placeholder{color:#888;opacity:1}
.ai-chat-input:focus{border-color:var(--accent)}
.ai-chat-send{flex-shrink:0;padding:2px 8px;cursor:pointer;font-size:14px;border:1px solid var(--accent);border-radius:6px;background:var(--accent);color:#fff;line-height:1.6}
.ai-chat-send:hover{opacity:.9}
.ai-chat-load{flex-shrink:0;padding:2px 6px;cursor:pointer;font-size:13px;border:1px solid var(--border);border-radius:6px;background:var(--bg-muted);line-height:1.6}
.ai-chat-load:hover{background:var(--bg-hover);border-color:var(--accent)}
.ai-result{margin-top:2px;padding:6px 8px;border-radius:4px;font-size:12px;line-height:1.5;max-height:120px;overflow-y:auto;white-space:pre-wrap;background:var(--bg-muted);border:1px solid var(--border-light)}
.ai-result.error{color:var(--danger);background:var(--bg-danger);border-color:var(--danger)}
.ai-result-text{font-size:12px;line-height:1.5;color:var(--text-primary)}
.ai-result .btn-prim{margin-top:4px;padding:3px 12px;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:#fff;cursor:pointer;font-size:11px;width:auto}
.ai-result .btn-prim:hover{opacity:.9}
</style>
</head>
<body>

<div class="top-bar">
  <div class="brand"><span>📝</span> PW Editor</div>
  <div class="type-selector">
    <select id="typeSelect" onchange="switchType(this.value)" title="Тип материала">
      <option value="post">📝 Записи</option>
      <option value="news">📰 Новости</option>
      <option value="article">📄 Статьи</option>
    </select>
  </div>
  <div class="actions">
    <!-- Файл -->
    <button onclick="newItem()" title="Создать новый материал">➕</button>
    <button onclick="importMarkdown()" title="Импорт Markdown">📥</button>
    <span class="act-sep"></span>
    <!-- Правка -->
    <button onclick="undo()" title="Отменить (Ctrl+Z)">↩️</button>
    <button onclick="redo()" title="Повторить (Ctrl+Y)">↪️</button>
    <span class="act-sep"></span>
    <!-- Вставка -->
    <button onclick="addImageBlock()" title="Добавить изображение">🖼</button>
    <button onclick="addBlock('table')" title="Добавить таблицу">📊</button>
    <button onclick="openMediaManager()" title="Медиа-менеджер">🗂️</button>
    <span class="act-sep"></span>
    <!-- Вид -->
    <button onclick="showPreview()" title="Предпросмотр">👁️</button>
    <button onclick="toggleFocus()" id="btnFocus" title="Режим фокуса">🎯</button>
    <span id="autoSaveIndicator" class="auto-save-indicator" title="Сохранено"></span>
    <!-- Сохранить -->
    <button class="btn-save" onclick="showSaveDialog()" title="Сохранить с экспортом (Ctrl+S)">💾 Сохранить</button>
    <div class="win-controls">
      <button onclick="window.pywry.emit('window:action',{action:'minimize'})" title="Свернуть" id="btnMinimize">─</button>
      <button onclick="toggleMaximize()" title="Развернуть" id="btnMaximize">🗖</button>
      <button class="btn-close" onclick="window.pywry.emit('window:action',{action:'close'})" title="Закрыть" id="btnClose">✕</button>
    </div>
  </div>
</div>

<div class="main-wrap">
  <!-- Список -->
  <div class="side-panel" id="sidePanel">
    <div class="side-header">
      <h3 id="sideTitle">📝 Записи</h3>
      <div style="display:flex;gap:4px;align-items:center">
        <select id="sortSelect" onchange="sortList()" title="Сортировка списка" style="font-size:11px;padding:2px 4px;border:1px solid var(--border);border-radius:3px;background:var(--bg-muted);color:var(--text-secondary)">
          <option value="date_desc">Новые</option>
          <option value="date_asc">Старые</option>
          <option value="alpha_asc">А-Я</option>
          <option value="alpha_desc">Я-А</option>
        </select>
        <button class="btn-add" onclick="newItem()" title="Создать новый материал">+</button>
      </div>
    </div>
    <div class="side-search">
      <input type="text" id="searchInput" placeholder="🔍 Поиск по названию..." oninput="filterList()">
      <button id="searchContentToggle" onclick="toggleSearchContent()" title="Искать по содержимому" style="background:none;border:1px solid var(--border);border-radius:4px 0 0 4px;cursor:pointer;padding:4px 6px;font-size:12px;color:var(--text-secondary);white-space:nowrap">🔍</button>
    </div>
    <div class="side-list" id="sideList"></div>
    <div class="resize-handle" id="sideResizeHandle"></div>
  </div>

  <!-- Редактор -->
  <div class="editor-area">
    <button class="toggle-side-btn" id="toggleSideBtn" onclick="toggleSide()" title="Закрыть список материалов (поиск, сортировка, теги)">☰</button>
    <button class="toggle-sett-btn" id="toggleSettBtn" onclick="toggleSettings()" title="Настройки (документ, AI, экспорт, тема)">⚙️</button>
    <div class="editor-center">
      <div class="editor-canvas">
        <div class="wp-title">
          <input type="text" id="postTitle" placeholder="Добавьте заголовок" autocomplete="off" oninput="scheduleAutoSave()">
          <div class="title-meta" id="titleMeta"></div>
          <div class="tags-row">
            <input type="text" id="postTags" placeholder="Теги через запятую (например: новости, технологии, обзор)" autocomplete="off" oninput="scheduleAutoSave()">
          </div>
        </div>
        <div class="block-editor" id="blockEditor"></div>
      <div class="editor-stats" id="editorStats"><span id="statWords">0 слов</span><span id="statChars">0 симв.</span><span id="statBlocks">0 блоков</span></div>
      </div>
    </div>
      <div class="ai-chat-bar" id="aiChatBar">
        <div class="ai-chat-row">
          <select id="aiProviderQuick" onchange="quickProviderChange()" title="Провайдер AI" class="ai-chat-provider">
            <option value="mistral">Mistral</option>
            <option value="ollama">Ollama</option>
            <option value="openai">OpenAI</option>
            <option value="deepseek">DeepSeek</option>
            <option value="custom">Custom</option>
          </select>
          <select id="aiModelQuick" onchange="quickModelChange();updateModelInfo()" title="Модель AI" class="ai-chat-model">
            <option value="">— модель —</option>
          </select>
          <button onclick="quickProviderChange()" title="Загрузить модели" class="ai-chat-refresh">🔄</button>
          <span id="modelCapsInfo" style="font-size:10px;color:var(--text-secondary);white-space:nowrap;flex-shrink:0"></span>
          <textarea class="ai-chat-input" id="aiChatInput" placeholder="Запрос к AI..." rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();aiChatSend();}"></textarea>
          <button onclick="aiChatSend()" title="Отправить" class="ai-chat-send">🤖</button>
          <button onclick="loadTextFromBlock()" title="Взять текст из блока" class="ai-chat-load">📥</button>
        </div>
        <div class="ai-result" id="aiResult" style="display:none">
          <div class="ai-result-text" id="aiResultText"></div>
          <button class="btn-prim" id="aiApplyBtn" style="display:none" onclick="applyAiResult()">📋 Вставить в редактор</button>
        </div>
      </div>
    </div>

  <!-- Правая панель -->
  <div class="settings-bar hidden" id="settingsBar">
    <div class="resize-handle-settings" id="settingsResizeHandle"></div>
    <!-- Путь экспорта — всегда видим, под ресайзером -->
    <div class="sett-export-quick">
      <div class="sett-field" style="margin:0;padding:2px 8px">
        <label style="font-size:10px;color:var(--text-secondary);display:block;margin-bottom:2px">💾 Папка экспорта</label>
        <div class="path-row" style="margin:0">
          <input type="text" id="exportPath" onchange="saveSettings()" placeholder="C:\путь\к\папке\экспорта" style="font-size:11px;flex:1;padding:2px 5px;width:0;min-width:40px">
          <button onclick="pickExportPath()" title="Выбрать папку экспорта" style="font-size:13px;padding:1px 5px;cursor:pointer;border:1px solid var(--border);border-radius:3px;background:var(--bg-muted)">📁</button>
        </div>
      </div>
    </div>
    <div class="sett-tabs">
      <button class="active" data-tab="document" onclick="switchTab('document')" title="Информация о материале">📄 Инфо</button>
      <button data-tab="ai" onclick="switchTab('ai')" title="Настройки ИИ">🤖 AI</button>
      <button data-tab="export" onclick="switchTab('export')" title="Настройки экспорта">📂 Данные</button>
    </div>
    <div class="sett-body" id="settBody">
      <!-- Инфо -->
      <div id="tabDocument">
        <div class="sett-group">
          <div class="sett-title">Информация</div>
          <div class="sett-field"><label>Тип</label><div class="info-text" id="docType"></div></div>
          <div class="sett-field"><label>Создан</label><div class="info-text" id="docCreated">—</div></div>
          <div class="sett-field"><label>Изменён</label><div class="info-text" id="docUpdated">—</div></div>
          <div class="sett-field"><label>Версии</label><button class="btn-sec" id="btnVersions" onclick="showVersions()" style="width:100%;font-size:12px">📋 История версий (0)</button></div>
        </div>
        <!-- Поля новости -->
        <div class="sett-group" id="newsFields" style="display:none">
          <div class="sett-title">📰 Новость</div>
          <div class="sett-field"><label>Дата события</label><input type="date" id="newsDate" oninput="scheduleAutoSave()"></div>
        </div>
        <!-- Поля статьи -->
        <div class="sett-group" id="articleFields" style="display:none">
          <div class="sett-title">📄 Статья</div>
          <div class="sett-field"><label>Автор</label><input type="text" id="articleAuthor" placeholder="Имя автора" oninput="scheduleAutoSave()" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-input);color:var(--text-primary);box-sizing:border-box"></div>
          <div class="sett-field"><label>Рубрика</label><input type="text" id="articleRubric" placeholder="Основная рубрика" oninput="scheduleAutoSave()" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-input);color:var(--text-primary);box-sizing:border-box"></div>
        </div>
        <div class="sett-group">
          <div class="sett-title">Блок</div>
          <div class="sett-field"><label>Тип блока</label><div class="info-text" id="blockTypeText">Нет выделенного</div></div>
          <div class="sett-field" id="blockHeadingLevel" style="display:none">
            <label>Уровень заголовка</label>
            <select onchange="changeHeadingLevel(this.value)"><option value="h2">H2</option><option value="h3">H3</option><option value="h4">H4</option></select>
          </div>
        </div>
        <div class="sett-group">
          <div class="sett-title">🎨 Тема</div>
          <div class="sett-field">
            <select id="themeSelect" onchange="setTheme(this.value)" title="Тема оформления" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px;background:var(--bg-input);color:var(--text-primary);cursor:pointer">
              <option value="light">☀️ Светлая</option>
              <option value="dark">🌙 Тёмная</option>
              <option value="modern">💎 Современная</option>
              <option value="sepia">📰 Газетная</option>
            </select>
          </div>
        </div>
      </div>

      <!-- AI -->
      <div id="tabAI" style="display:none">
        <div class="sett-group">
          <div class="collapsible-title">
            <span>⚙️ Настройки провайдера</span>
            <button class="collapse-btn collapsed" onclick="toggleAiSettings()" id="aiCollapseBtn" title="Развернуть">▼</button>
          </div>
          <div id="aiSettingsBody" style="display:none">
            <div class="sett-field"><label>Выберите API</label>
              <select id="aiProvider" onchange="onProviderChange()">
                <option value="openai">OpenAI</option>
                <option value="mistral">Mistral AI</option>
                <option value="ollama">Ollama (локальный)</option>
                <option value="deepseek">DeepSeek</option>
                <option value="custom">Свой вариант</option>
              </select>
            </div>
            <div class="sett-field"><label>URL API</label>
              <input type="text" id="aiUrl" placeholder="https://api.openai.com/v1"></div>
            <div class="sett-field"><label>API ключ</label>
              <div class="key-row">
                <input type="password" id="aiKey" placeholder="sk-... (оставьте пустым, если не нужен)">
                <button class="btn-eye" onclick="toggleKeyVisibility()" id="keyEyeBtn" title="Показать ключ">👁️</button>
              </div></div>
            <div class="sett-field" id="aiModelField">
              <label>Модель</label>
              <div class="model-row">
                <select id="aiModel" onchange="document.getElementById('aiModelText').value=this.value;syncQuickModel()">
                  <option value="">— загрузите список моделей —</option>
                </select>
                <input type="text" id="aiModelText" placeholder="или введите вручную" style="display:none">
              </div>
            </div>
            <div class="btn-row">
              <button class="btn-sec" onclick="loadModels()" title="Загрузить список доступных моделей">🔄 Загрузить модели</button>
              <button class="btn-sec" onclick="testConnection()" title="Проверить подключение к API">🔌 Проверить</button>
              <button class="btn-sec" onclick="saveAiSettings()" title="Сохранить настройки ИИ">💾 Сохранить</button>
            </div>
            <div id="aiStatus" class="ai-status" style="display:none"></div>
          </div>
        </div>
      </div>

      <!-- Экспорт / Данные -->
      <div id="tabExport" style="display:none">
        <div class="sett-group">
          <div class="sett-title">Данные</div>
          <div class="sett-field"><label>JSON-данные</label><div class="info-text" id="dataPathInfo"></div></div>
        </div>
        <button class="btn-sec" onclick="openDataFolder()" title="Открыть папку с данными">📂 Открыть папку</button>
      </div>
    </div>
  </div>
</div>

<!-- Предпросмотр -->
<div class="preview-overlay" id="previewOverlay" onclick="if(event.target===this)closePreview()">
  <div class="preview-bar">
    <div class="preview-title">👁️ Предпросмотр</div>
    <button onclick="closePreview()" title="Закрыть предпросмотр (Escape)">✕ Закрыть</button>
  </div>
  <div class="preview-body" id="previewBody"></div>
</div>

<!-- Медиа-менеджер -->
<div class="media-overlay" id="mediaOverlay" onclick="if(event.target===this)closeMediaManager()">
  <div class="media-content">
    <div class="media-header">
      <h2>🗂️ Медиа-менеджер</h2>
      <div style="display:flex;gap:8px">
        <button class="btn-sec" onclick="uploadToMediaLibrary()" title="Загрузить изображение" style="font-size:12px;padding:4px 12px;cursor:pointer;border:1px solid var(--border);border-radius:4px;background:var(--bg-muted)">📁 Загрузить</button>
        <button class="media-close" onclick="closeMediaManager()" title="Закрыть">✕</button>
      </div>
    </div>
    <div id="mediaGrid" class="media-grid"></div>
  </div>
</div>

<!-- Диалог сохранения -->
<div class="save-dialog" id="saveDialog" onclick="if(event.target===this)hideSaveDialog()">
  <div class="save-panel">
    <h3>💾 Сохранить материал</h3>
    <label>Формат экспорта</label>
    <select id="saveFormat">
      <option value="html">HTML (.html) — веб-страница</option>
      <option value="md">Markdown (.md)</option>
      <option value="txt">TXT (.txt) — чистый текст</option>
      <option value="pdf">PDF — прямая печать</option>
    </select>
    <label>Папка для экспорта</label>
    <input type="text" id="savePath" readonly>
    <div class="save-actions">
      <button class="btn-save" onclick="confirmSave()" title="Сохранить и экспортировать" style="flex:2">💾 Сохранить</button>
      <button onclick="hideSaveDialog()" title="Отменить" style="background:var(--bg-muted);border:1px solid var(--border);color:var(--text-primary)">Отмена</button>
    </div>
  </div>
</div>

<!-- Диалог подтверждения удаления -->
<div class="save-dialog" id="confirmDialog" onclick="if(event.target===this)hideConfirmDialog()" style="display:none">
  <div class="save-panel" style="max-width:360px;text-align:center">
    <h3 id="confirmTitle">Удалить материал?</h3>
    <p id="confirmMessage" style="margin:8px 0 16px;color:var(--text-secondary)">Это действие нельзя отменить.</p>
    <div class="save-actions" style="justify-content:center">
      <button class="btn-save" onclick="confirmDelete()" title="Удалить" style="flex:1;background:var(--danger,#d63638)">Удалить</button>
      <button onclick="hideConfirmDialog()" title="Отмена" style="flex:1;background:var(--bg-muted);border:1px solid var(--border);color:var(--text-primary)">Отмена</button>
    </div>
  </div>
</div>

<!-- Inserter -->
<div class="inserter-popup" id="inserterPopup" onclick="if(event.target===this)hideInserter()">
  <div class="inserter-panel">
    <h3>➕ Добавить блок</h3>
    <div class="inserter-block" onclick="addBlock('paragraph');hideInserter()"><div class="ib-icon">¶</div><div><div class="ib-label">Параграф</div><div class="ib-desc">Обычный текст</div></div></div>
    <div class="inserter-block" onclick="addBlock('heading','h2');hideInserter()"><div class="ib-icon">H</div><div><div class="ib-label">Заголовок H2</div><div class="ib-desc">Подзаголовок раздела</div></div></div>
    <div class="inserter-block" onclick="addBlock('heading','h3');hideInserter()"><div class="ib-icon">H</div><div><div class="ib-label">Заголовок H3</div><div class="ib-desc">Подзаголовок</div></div></div>
    <div class="inserter-block" onclick="addBlock('list','ul');hideInserter()"><div class="ib-icon">•</div><div><div class="ib-label">Список</div><div class="ib-desc">Маркированный список</div></div></div>
    <div class="inserter-block" onclick="addBlock('quote');hideInserter()"><div class="ib-icon">❝</div><div><div class="ib-label">Цитата</div><div class="ib-desc">Выделенная цитата</div></div></div>
    <div class="inserter-block" onclick="addBlock('separator');hideInserter()"><div class="ib-icon">—</div><div><div class="ib-label">Разделитель</div><div class="ib-desc">Горизонтальная линия</div></div></div>
    <div class="inserter-block" onclick="addBlock('code');hideInserter()"><div class="ib-icon">&lt;/&gt;</div><div><div class="ib-label">Код</div><div class="ib-desc">Фрагмент кода</div></div></div>
    <div class="inserter-block" onclick="addBlock('image');hideInserter()"><div class="ib-icon">🖼</div><div><div class="ib-label">Изображение</div><div class="ib-desc">Загрузите картинку с диска</div></div></div>
    <div class="inserter-block" onclick="addBlock('table');hideInserter()"><div class="ib-icon">📊</div><div><div class="ib-label">Таблица</div><div class="ib-desc">Редактируемая таблица 2×2</div></div></div>
  </div>
</div>

<div class="toast-wrap" id="toastWrap"></div>

<!-- Скрытый input для загрузки изображений -->
<input type="file" id="imageInput" accept="image/*" style="display:none">
<input type="color" id="cellColorPicker" value="#ffff99" style="display:none">

<!-- Модал версий -->
<div class="save-dialog" id="versionsModal" onclick="if(event.target===this)hideVersions()" style="display:none">
  <div class="save-panel" style="max-width:520px;max-height:70vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0">\uD83D\uDCCB История версий</h3>
      <button onclick="hideVersions()" style="background:none;border:none;font-size:18px;cursor:pointer;color:var(--text-secondary)">\u2715</button>
    </div>
    <div id="versionsList" style="font-size:13px">
      <div style="color:var(--text-secondary);text-align:center;padding:24px">Нет сохранённых версий</div>
    </div>
  </div>
</div>

<script>
// ОТЛАДКА в ОТДЕЛЬНОМ скрипте — ловит синтаксические ошибки в основном скрипте
window.onerror = function(msg, url, line, col, err) {
  var el = document.getElementById('jsErrorDisplay');
  if(!el){
    el = document.createElement('div');
    el.id = 'jsErrorDisplay';
    el.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#c00;color:#fff;padding:12px;font:14px monospace;z-index:99999;white-space:pre-wrap;max-height:200px;overflow:auto';
    document.body.appendChild(el);
  }
  el.textContent += 'JS ERROR: ' + msg + ' (line ' + line + ':' + col + ')\n' + (err ? err.stack : '') + '\n';
  return true;
};
window.addEventListener('unhandledrejection', function(e) {
  var el = document.getElementById('jsErrorDisplay');
  if(!el){
    el = document.createElement('div');
    el.id = 'jsErrorDisplay';
    el.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#c00;color:#fff;padding:12px;font:14px monospace;z-index:99999;white-space:pre-wrap;max-height:200px;overflow:auto';
    document.body.appendChild(el);
  }
  el.textContent += 'PROMISE ERROR: ' + (e.reason || e) + '\n';
});
</script>
<script>
var currentType='post', currentId=null, blocks=[], selectedBlock=-1, selectedCell=null, sideVisible=true, settingsVisible=false;
var blockCounter=0, expFormat='html', expPath='', aiResultText='', savedAiModel='';
var pendingAiBlockIdx=-1, pendingAiSelInfo=null, selectedBlocks=[];
var savedModels={};
var undoStack=[], redoStack=[];
var undoMax=50;
var draftCounter=parseInt(localStorage.getItem('wp_draft_counter')||'0');
function nextDraftTitle(){draftCounter++;localStorage.setItem('wp_draft_counter',String(draftCounter));return '\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a '+draftCounter;}
var TYPE_LABELS={post:'\u0417\u0430\u043f\u0438\u0441\u044c',news:'\u041d\u043e\u0432\u043e\u0441\u0442\u044c',article:'\u0421\u0442\u0430\u0442\u044c\u044f'};

(function init(){
  try {
    clearEditor();
    initInlineEditor();
    initPasteHandler();
    window.pywry.emit('content:switch',{content_type:'post'});
    window.pywry.emit('settings:get',{});
  } catch(e) {
    var el = document.getElementById('jsErrorDisplay');
    if(el) el.textContent += 'INIT ERROR: ' + (e.message || e) + '\n' + (e.stack || '') + '\n';
  }
})();

/* ─── Автосохранение при закрытии ─── */
window.addEventListener('beforeunload',function(){
  var title=document.getElementById('postTitle').value.trim();
  var hasContent=title||blocks.length>1||(blocks.length===1&&blocks[0].content);
  if(hasContent){
    // Синхронное сохранение через emit (успеет отправить до закрытия)
    var content='';
    if(typeof collectContent==='function')content=collectContent();
    window.pywry.emit('content:save',{
      content_type: currentType,
      item: {
        id: currentId||'',
        title: title,
        type: currentType,
        tags: document.getElementById('postTags').value,
        content: content,
        news_date: document.getElementById('newsDate')?document.getElementById('newsDate').value:'',
        article_author: document.getElementById('articleAuthor')?document.getElementById('articleAuthor').value:'',
        article_rubric: document.getElementById('articleRubric')?document.getElementById('articleRubric').value:'',
        export_format: expFormat
      }
    });
  }
});

function switchType(type){
  // Сохраняем текущий материал перед переключением
  var hasContent=document.getElementById('postTitle').value.trim()||blocks.length>1||(blocks.length===1&&blocks[0].content);
  if(hasContent)doImmediateSave();
  currentType=type;currentId=null;
  document.getElementById('typeSelect').value=type;
  document.getElementById('sideTitle').textContent=document.getElementById('typeSelect').options[document.getElementById('typeSelect').selectedIndex].text;
  // Показываем/скрываем поля под тип
  document.getElementById('newsFields').style.display=type==='news'?'block':'none';
  document.getElementById('articleFields').style.display=type==='article'?'block':'none';
  clearEditor();
  cachedItems=[];renderSideList(); // очищаем старый список
  window.pywry.emit('content:switch',{content_type:type});
}

/* ─── Блоки ─── */
function createBlock(type,level){
  blockCounter++;
  var c='';
  if(type==='list')c='<ul><li></li></ul>';
  if(type==='table')return{id:'b'+blockCounter+'_'+Date.now().toString(36),type:'table',level:'',content:'',rows:[[{content:''},{content:''}],[{content:''},{content:''}]],colWidths:[],imageData:null,imageName:null};
  return{id:'b'+blockCounter+'_'+Date.now().toString(36),type:type,level:level||'',content:c,imageData:null,imageName:null};
}
function addBlock(type,level,afterIdx){
  saveHistory();
  var b=createBlock(type,level);
  if(afterIdx!==undefined&&afterIdx>=0)blocks.splice(afterIdx+1,0,b);else blocks.push(b);
  renderBlocks();scheduleAutoSave();
  setTimeout(function(){var idx=blocks.indexOf(b);if(idx>=0)selectBlock(idx);},50);
}
/* ─── Сохранение выделения для тулбара ─── */
var _savedRange=null;
function saveSelection(){
  var sel=window.getSelection();
  if(sel&&sel.rangeCount>0&&!sel.isCollapsed)_savedRange=sel.getRangeAt(0).cloneRange();
  else _savedRange=null;
}
// Сохраняет любую позицию (включая caret) — для кнопок тулбара
var _savedCaret=null;
function saveCaret(){
  var sel=window.getSelection();
  if(sel&&sel.rangeCount>0){_savedCaret=sel.getRangeAt(0).cloneRange();}
  else _savedCaret=null;
}
function restoreCaret(){
  if(!_savedCaret)return;
  try{
    var sel=window.getSelection();
    sel.removeAllRanges();
    sel.addRange(_savedCaret);
  }catch(e){}
}
function restoreSelection(){
  if(!_savedRange)return;
  try{
    var sel=window.getSelection();
    sel.removeAllRanges();
    sel.addRange(_savedRange);
  }catch(e){}
}
// Автосохранение выделения при выделении текста
document.addEventListener('mouseup',function(){
  var el=document.activeElement;
  if(el&&el.isContentEditable)saveSelection();
});
// Сохраняем позицию курсора ПЕРЕД кликом по кнопке тулбара
document.addEventListener('mousedown',function(e){
  var btn=e.target.closest('.block-toolbar button');
  if(btn)saveCaret();
});
document.addEventListener('keyup',function(){
  var el=document.activeElement;
  if(el&&el.isContentEditable)saveSelection();
});
function addBlockAt(idx){addBlock('paragraph',null,idx);}
function addImageBlock(){
  saveHistory();
  var b=createBlock('image');
  blocks.push(b);
  renderBlocks();
  setTimeout(function(){
    var idx=blocks.indexOf(b);
    if(idx>=0){selectBlock(idx);uploadImage(idx);}
  },100);
}
function removeBlock(idx){
  saveHistory();
  blocks.splice(idx,1);
  if(blocks.length===0)blocks.push(createBlock('paragraph'));
  if(selectedBlock>=blocks.length)selectedBlock=blocks.length-1;
  if(selectedBlock===idx||selectedBlock>=blocks.length){selectedBlock=-1;updateBlockInfo();}
  renderBlocks();scheduleAutoSave();
}
function moveBlock(idx,dir){
  saveHistory();
  var n=idx+dir;
  if(n<0||n>=blocks.length)return;
  var b=blocks.splice(idx,1)[0];blocks.splice(n,0,b);selectedBlock=n;
  renderBlocks();scheduleAutoSave();setTimeout(function(){selectBlock(n);},50);
}
function copyBlock(idx){
  saveHistory();
  var b=blocks[idx];
  var copy=JSON.parse(JSON.stringify(b));
  blocks.splice(idx+1,0,copy);
  selectedBlock=idx+1;
  renderBlocks();scheduleAutoSave();setTimeout(function(){selectBlock(idx+1);},50);
}
function selectBlock(idx, ctrl){
  if(ctrl){
    // Ctrl+Click: toggle in selectedBlocks
    var pos=selectedBlocks.indexOf(idx);
    if(pos>=0)selectedBlocks.splice(pos,1);
    else selectedBlocks.push(idx);
    renderBlocks();
    updateBlockInfo();
    return;
  }
  if(selectedBlock===idx)return;
  selectedBlock=idx;
  selectedBlocks=[];
  renderBlocks();
  updateBlockInfo();
  setTimeout(positionToolbar,10);
}
function deselectBlock(){
  selectedBlock=-1;
  selectedBlocks=[];
  renderBlocks();
  updateBlockInfo();
}
function positionToolbar(){
  var toolbar=document.querySelector('.block.selected .block-toolbar');
  if(!toolbar){
    var old=document.querySelector('.block-toolbar.fixed');
    if(old)old.classList.remove('fixed');
    return;
  }
  var block=toolbar.closest('.block');
  if(!block)return;
  var br=block.getBoundingClientRect();
  // Если тулбар уходит за верхнюю границу окна — фиксируем
  if(br.top<50){
    toolbar.classList.add('fixed');
    // При fixed позиционировании left должен быть от viewport
    var ecRect=document.querySelector('.editor-center').getBoundingClientRect();
    toolbar.style.left=ecRect.left+'px';
    toolbar.style.top='56px';
    toolbar.style.width=(ecRect.width-10)+'px';
  }else{
    toolbar.classList.remove('fixed');
    toolbar.style.left='';
    toolbar.style.top='';
    toolbar.style.width='';
  }
}
/* ─── Отслеживание скролла для тулбара ─── */
var _posToolbarScheduled=null;
function _schedulePosToolbar(){
  if(_posToolbarScheduled)return;
  _posToolbarScheduled=requestAnimationFrame(function(){
    _posToolbarScheduled=null;
    positionToolbar();
  });
}
// Слушаем скролл везде, где может крутиться редактор
window.addEventListener('scroll',_schedulePosToolbar,{passive:true});
window.addEventListener('resize',_schedulePosToolbar,{passive:true});
// Слушаем скролл внутри редактора
var _ec=document.querySelector('.editor-center');
if(_ec)_ec.addEventListener('scroll',_schedulePosToolbar,{passive:true});
if(_ec)_ec.addEventListener('resize',_schedulePosToolbar,{passive:true});
// Дополнительно: MutationObserver на случай изменения DOM
var _posObserver=new MutationObserver(function(){_schedulePosToolbar();});
var _be=document.getElementById('blockEditor');
if(_be)_posObserver.observe(_be,{childList:true,subtree:true,attributes:false});
/* ─── Drag & Drop блоков ─── */
var _dragIdx=-1;
function onDragStart(ev,idx){
  _dragIdx=idx;
  ev.dataTransfer.effectAllowed='move';
  ev.dataTransfer.setData('text/plain',String(idx));
  // Небольшая задержка, чтобы визуал сработал
  setTimeout(function(){
    var el=document.querySelector('.block[data-idx="'+idx+'"]');
    if(el)el.style.opacity='0.4';
  },0);
}
function onDragOver(ev,idx){
  ev.preventDefault();
  ev.dataTransfer.dropEffect='move';
  // Подсветка цели
  var els=document.querySelectorAll('.block.drag-over');
  for(var i=0;i<els.length;i++)els[i].classList.remove('drag-over');
  var el=document.querySelector('.block[data-idx="'+idx+'"]');
  if(el&&idx!==_dragIdx)el.classList.add('drag-over');
}
function onDragLeave(ev){
  var el=document.querySelector('.block.drag-over');
  if(el)el.classList.remove('drag-over');
}
function onDrop(ev,idx){
  ev.preventDefault();
  onDragLeave(ev);
  // Убираем подсветку со всех
  var els=document.querySelectorAll('.block.drag-over');
  for(var i=0;i<els.length;i++)els[i].classList.remove('drag-over');
  var srcIdx=_dragIdx;
  _dragIdx=-1;
  if(srcIdx<0||srcIdx===idx||srcIdx>=blocks.length||idx>=blocks.length)return;
  // Восстанавливаем opacity
  var srcEl=document.querySelector('.block[data-idx="'+srcIdx+'"]');
  if(srcEl)srcEl.style.opacity='';
  // Переставляем блок
  saveHistory();
  var b=blocks.splice(srcIdx,1)[0];
  // После splice индекс цели мог измениться
  var targetIdx=idx;
  if(srcIdx<targetIdx)targetIdx--;
  blocks.splice(targetIdx,0,b);
  selectedBlock=targetIdx;
  renderBlocks();
  scheduleAutoSave();
  setTimeout(function(){selectBlock(targetIdx);},50);
}
function onDragEnd(ev){
  var els=document.querySelectorAll('.block.drag-over');
  for(var i=0;i<els.length;i++)els[i].classList.remove('drag-over');
  var el=document.querySelector('.block[data-idx="'+_dragIdx+'"]');
  if(el)el.style.opacity='';
  _dragIdx=-1;
}
/* document click hides all popups (they use stopPropagation to stay open) */
document.addEventListener('click',function(){hideAllPopups();});

/* ─── Undo / Redo ─── */
function saveHistory(){
  if(undoStack.length>=undoMax)undoStack.shift();
  undoStack.push(JSON.parse(JSON.stringify(blocks)));
  redoStack=[];
}
function undo(){
  if(!undoStack.length)return;
  redoStack.push(JSON.parse(JSON.stringify(blocks)));
  blocks=undoStack.pop();
  selectedBlock=-1;
  renderBlocks();
  scheduleAutoSave();
}
function redo(){
  if(!redoStack.length)return;
  undoStack.push(JSON.parse(JSON.stringify(blocks)));
  blocks=redoStack.pop();
  selectedBlock=-1;
  renderBlocks();
  scheduleAutoSave();
}

/* --- Stats --- */
function updateStats(){
  var words=0,chars=0;
  for(var i=0;i<blocks.length;i++){
    var b=blocks[i];
    if(b.type==='table'){
      for(var r=0;r<b.rows.length;r++)
        for(var c=0;c<b.rows[r].length;c++){
          var t=b.rows[r][c].content||'';
          chars+=t.length;
          words+=t.trim()?t.trim().split(/\s+/).length:0;
        }
    }else{
      var t=b.content||'';
      if(b.type==='image')continue;
      chars+=t.length;
      words+=t.trim()?t.trim().split(/\s+/).length:0;
    }
  }
  document.getElementById('statWords').textContent=words+' \u0441\u043b\u043e\u0432';
  document.getElementById('statChars').textContent=chars+' \u0441\u0438\u043c\u0432.';
  document.getElementById('statBlocks').textContent=blocks.length+' \u0431\u043b\u043e\u043a\u043e\u0432';
}

function renderBlocks(){
  var c=document.getElementById('blockEditor');if(!c)return;
  var h=[];h.push('<div class="editor-hint">\u23ce Enter — \u043d\u043e\u0432\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430 \u00b7 \u21e7\u23ce Shift+Enter — \u043d\u043e\u0432\u044b\u0439 \u0430\u0431\u0437\u0430\u0446</div>');
  h.push('<div class="block-adder show"><button onclick="addBlockAt(-1)" title="Добавить блок">+</button></div>');
  for(var i=0;i<blocks.length;i++){
    var b=blocks[i],sel=i===selectedBlock?' selected':'', multi=selectedBlocks.indexOf(i)>=0?' multi-sel':'';
    h.push('<div class="block '+b.type+(b.level?' '+b.level:'')+sel+multi+'" data-idx="'+i+'" ondragover="onDragOver(event,'+i+')" ondrop="onDrop(event,'+i+')" ondragleave="onDragLeave(event)" onclick="event.stopPropagation();if(event.ctrlKey||event.metaKey){selectBlock('+i+',true);}">');
    h.push('<div class="block-toolbar">');
    h.push('<span class="drag-handle" draggable="true" ondragstart="onDragStart(event,'+i+')" ondragend="onDragEnd(event)" title="\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438\u0442\u044c \u0431\u043b\u043e\u043a">\u22ee\u22ee</span>');
    if(b.type==='table'){
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'bold\')" title="Полужирный"><b>B</b></button>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'italic\')" title="Курсив"><i>I</i></button>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'underline\')" title="Подчёркнутый"><u>U</u></button>');
      h.push('<span class="sep"></span>');
      h.push('<select class="fs-select" title="Размер шрифта" onchange="event.stopPropagation();changeFontSize(this)" onfocus="event.stopPropagation()"><option value="">A</option><option value="10">10</option><option value="12">12</option><option value="14">14</option><option value="16">16</option><option value="18">18</option><option value="20">20</option><option value="24">24</option><option value="36">36</option></select>');
    }else{
      h.push('<div style="position:relative;display:inline-block">');
      h.push('<button onclick="event.stopPropagation();toggleAiPopup('+i+')" title="AI (\u0438\u0441\u043a\u0443\u0441\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439 \u0438\u043d\u0442\u0435\u043b\u043b\u0435\u043a\u0442)" class="has-popup">\ud83e\udd16</button>');
      h.push('<div class="tb-popup tb-popup-ai" id="aiPop'+i+'" onclick="event.stopPropagation()">');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();aiFromToolbar('+i+',\'rewrite\')">\u270d\ufe0f \u041f\u0435\u0440\u0435\u043f\u0438\u0441\u0430\u0442\u044c</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();aiFromToolbar('+i+',\'proceed\')">\u25b6\ufe0f \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();aiFromToolbar('+i+',\'fix\')">\u2714\ufe0f \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();aiFromToolbar('+i+',\'shorten\')">\u2796 \u0421\u043e\u043a\u0440\u0430\u0442\u0438\u0442\u044c</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();aiFromToolbar('+i+',\'translate\')">\ud83c\udf10 \u041f\u0435\u0440\u0435\u0432\u0435\u0441\u0442\u0438</div>');
      h.push('<div class="tb-popup-sep"></div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();aiFromToolbar('+i+',\'explain\')">\ud83d\udca1 \u041e\u0442\u0432\u0435\u0442\u0438\u0442\u044c</div>');
      h.push('</div></div>');
      h.push('<button onclick="event.stopPropagation();convertBlock('+i+',\'paragraph\')" title="Обычный текст"'+((b.type==='paragraph')?' class="type-active"':'')+'>\u00b6</button>');
      h.push('<button onclick="event.stopPropagation();convertBlock('+i+',\'heading\',\'h2\')" title="Заголовок H2"'+((b.type==='heading'&&b.level==='h2')?' class="type-active"':'')+'>H2</button>');
      h.push('<button onclick="event.stopPropagation();convertBlock('+i+',\'heading\',\'h3\')" title="Заголовок H3"'+((b.type==='heading'&&b.level==='h3')?' class="type-active"':'')+'>H3</button>');
      h.push('<div style="position:relative;display:inline-block">');
      h.push('<button onclick="event.stopPropagation();toggleSepPopup('+i+')" title="Вставить разделитель" class="has-popup">\u2500\u2500</button>');
      h.push('<div class="tb-popup" id="sepPop'+i+'" onclick="event.stopPropagation()" style="left:auto;right:0">');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();addBlock(\'separator\',null,'+(i-1)+')">\u2500\u2500 \u0412\u044b\u0448\u0435</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();addBlock(\'separator\',null,'+i+')">\u2500\u2500 \u041d\u0438\u0436\u0435</div>');
      h.push('</div></div>');
      h.push('<span class="sep"></span>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'bold\')" title="Полужирный (Ctrl+B)"><b>B</b></button>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'italic\')" title="Курсив (Ctrl+I)"><i>I</i></button>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'underline\')" title="Подчёркнутый (Ctrl+U)"><u>U</u></button>');
      h.push('<div style="position:relative;display:inline-block">');
      h.push('<button onclick="event.stopPropagation();toggleColorPopup('+i+')" title="Цвет текста" class="has-popup" style="font-size:12px">\ud83c\udfa8</button>');
      h.push('<div class="tb-popup tb-popup-color" id="colorPop'+i+'" onclick="event.stopPropagation()">');
      h.push('<div class="tb-popup-title">\u0426\u0432\u0435\u0442 \u0442\u0435\u043a\u0441\u0442\u0430</div>');
      var colors=['#e74c3c','#e67e22','#f1c40f','#2ecc71','#1abc9c','#3498db','#9b59b6','#000000','#555555','#95a5a6','#ecf0f1','#ffffff'];
      for(var ci=0;ci<colors.length;ci++){
        h.push('<div class="color-swatch" style="background:'+colors[ci]+'" onclick="event.stopPropagation();changeTextColor(\''+colors[ci]+'\', '+i+');hideAllPopups()" title="'+colors[ci]+'"></div>');
        if(ci===5)h.push('<br>');
      }
      h.push('</div></div>');
      h.push('<select class="fs-select" title="Размер шрифта" onchange="event.stopPropagation();changeFontSize(this)" onfocus="event.stopPropagation()"><option value="">A</option><option value="10">10</option><option value="12">12</option><option value="14">14</option><option value="16">16</option><option value="18">18</option><option value="20">20</option><option value="24">24</option><option value="36">36</option></select>');
      h.push('<span class="sep"></span>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'insertUnorderedList\')" title="Маркированный список">ul</button>');
      h.push('<button onclick="event.stopPropagation();fmtBlock(\'insertOrderedList\')" title="Нумерованный список">ol</button>');
      h.push('<button onclick="event.stopPropagation();insertLinkBlock()" title="Вставить ссылку (Ctrl+K)">\ud83d\udd17</button>');
      h.push('<span class="sep"></span>');
      h.push('<div style="position:relative;display:inline-block">');
      h.push('<button onclick="event.stopPropagation();toggleImagePopup('+i+')" title="Изображение" class="has-popup">\ud83d\uddbc\ufe0f</button>');
      h.push('<div class="tb-popup tb-popup-img" id="imgPop'+i+'" onclick="event.stopPropagation()">');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();inlineImage('+i+',\'left\');hideAllPopups()">\ud83d\uddbc\ufe0f \u2190 \u0421\u043b\u0435\u0432\u0430 \u043e\u0442 \u0442\u0435\u043a\u0441\u0442\u0430</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();inlineImage('+i+',\'right\');hideAllPopups()">\ud83d\uddbc\ufe0f \u2192 \u0421\u043f\u0440\u0430\u0432\u0430 \u043e\u0442 \u0442\u0435\u043a\u0441\u0442\u0430</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();inlineImage('+i+',\'center\');hideAllPopups()">\ud83d\uddbc\ufe0f \u2191 \u041f\u043e \u0446\u0435\u043d\u0442\u0440\u0443</div>');
      h.push('<div class="tb-popup-item" onclick="event.stopPropagation();insertImageBlock('+i+');hideAllPopups()">\ud83d\uddbc\ufe0f \u25a1 \u041e\u0442\u0434\u0435\u043b\u044c\u043d\u044b\u043c \u0431\u043b\u043e\u043a\u043e\u043c</div>');
      h.push('</div></div>');
      h.push('<button onclick="event.stopPropagation();addBlock(\'table\',null,'+i+')" title="Добавить таблицу">\ud83d\udcca</button>');
    }
    // Block action buttons
    h.push('<span class="sep"></span>');
    if(i>0)h.push('<button onclick="event.stopPropagation();moveBlock('+i+',-1)" title="Вверх">\u25b2</button>');
    if(i<blocks.length-1)h.push('<button onclick="event.stopPropagation();moveBlock('+i+',1)" title="Вниз">\u25bc</button>');
    h.push('<button onclick="event.stopPropagation();copyBlock('+i+')" title="Копировать блок">\ud83d\udccb</button>');
    h.push('<button onclick="event.stopPropagation();mergeBlockWithPrevious('+i+')" title="Объединить с предыдущим блоком (Backspace в начале)">\u2b06\ufe0f</button>');
    h.push('<button onclick="event.stopPropagation();splitBlockAtCursor('+i+')" title="Разделить блок на два (Shift+Enter)">\u2702\ufe0f</button>');
    h.push('<button onclick="event.stopPropagation();removeBlock('+i+')" title="Удалить блок">\u2715</button>');
    h.push('</div>');
    if(b.type==='paragraph'){
      h.push('<div contenteditable="true" spellcheck="true" onfocus="selectBlock('+i+')" oninput="onBlockInput('+i+')" onkeydown="onBlockKeydown(event,'+i+')">'+b.content+'</div>');
    }else if(b.type==='heading'){
      h.push('<div contenteditable="true" spellcheck="true" onfocus="selectBlock('+i+')" oninput="onBlockInput('+i+')" onkeydown="onBlockKeydown(event,'+i+')">'+b.content+'</div>');
    }else if(b.type==='list'){
      h.push('<div contenteditable="true" spellcheck="true" onfocus="selectBlock('+i+')" oninput="onBlockInput('+i+')" onkeydown="onBlockKeydown(event,'+i+')">'+b.content+'</div>');
    }else if(b.type==='quote'){
      h.push('<div contenteditable="true" spellcheck="true" onfocus="selectBlock('+i+')" oninput="onBlockInput('+i+')" onkeydown="onBlockKeydown(event,'+i+')">'+b.content+'</div>');
    }else if(b.type==='separator'){
      h.push('<hr>');
    }else if(b.type==='code'){
      h.push('<div contenteditable="true" spellcheck="true" onfocus="selectBlock('+i+')" oninput="onBlockInput('+i+')" onkeydown="onBlockKeydown(event,'+i+')">'+b.content+'</div>');
    }else if(b.type==='image'){
      if(b.imageData){
        h.push('<img src="'+b.imageData+'" alt="'+(b.imageName||'')+'">');
        h.push('<div class="img-actions"><button onclick="event.stopPropagation();uploadImage('+i+')" title="Заменить изображение">\ud83d\uddbc\ufe0f \u0417\u0430\u043c\u0435\u043d\u0438\u0442\u044c</button><button onclick="event.stopPropagation();removeBlock('+i+')" title="Удалить блок с изображением">\u274c \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0431\u043b\u043e\u043a</button></div>');
      } else {
        h.push('<div class="img-placeholder">\ud83d\uddbc\ufe0f <em>Изображение не выбрано</em><br><button onclick="event.stopPropagation();uploadImage('+i+')" title="Загрузить изображение с диска">\ud83d\udcc2 Загрузить с диска</button></div>');
      }
    }else if(b.type==='table'){
      h.push('<table>');
      // Определяем реальное количество столбцов (первая не-colspan строка)
      var tableCols=2;
      for(var tmp=0;tmp<b.rows.length;tmp++){
        if(b.rows[tmp].length>1){tableCols=b.rows[tmp].length;break;}
      }
      // Column action row
      h.push('<tr class="tb-col-actions">');
      h.push('<td class="tb-corner"></td>');
      for(var ci=0;ci<tableCols;ci++){
        h.push('<td class="tb-col-btn">');
        h.push('<button onclick="event.stopPropagation();addTableColAt('+i+','+ci+')" title="Добавить столбец">➕</button>');
        h.push('<button onclick="event.stopPropagation();delTableColAt('+i+','+ci+')" title="Удалить столбец">✕</button>');
        h.push('<button ondblclick="event.stopPropagation();setColWidth('+i+','+ci+')" title="Двойной клик — ширина колонки" style="font-size:9px;padding:0 2px">⇔</button>');
        h.push('</td>');
      }
      h.push('</tr>');
      // Data rows
      for(var ri=0;ri<b.rows.length;ri++){
        h.push('<tr>');
        // Row action cell
        h.push('<td class="tb-row-btn">');
        h.push('<button onclick="event.stopPropagation();addTableRowAt('+i+','+ri+')" title="Добавить строку">➕</button>');
        h.push('<button onclick="event.stopPropagation();delTableRowAt('+i+','+ri+')" title="Удалить строку">✕</button>');
        if(b.rows[ri].length===1&&tableCols>1)h.push('<button onclick="event.stopPropagation();splitSpanRow('+i+','+ri+')" title="Разбить на ячейки">⬌</button>');
        h.push('</td>');
        // Data cells
        for(var ci=0;ci<b.rows[ri].length;ci++){
          var cell=b.rows[ri][ci];
          var cellStyle=cell.bg?'background:'+cell.bg:'';
          if(b.colWidths&&b.colWidths[ci])cellStyle+=';width:'+b.colWidths[ci]+'px';
          var colspan=(b.rows[ri].length===1&&tableCols>1)?' colspan="'+tableCols+'"':'';
          h.push('<td'+colspan+' contenteditable="true" spellcheck="true" style="'+cellStyle+'" onfocus="selectBlock('+i+');selectedCell={block:'+i+',row:'+ri+',col:'+ci+'}" oninput="onTableInput('+i+','+ri+','+ci+')" onkeydown="onBlockKeydown(event,'+i+')">'+(cell.content||'')+'</td>');
        }
        h.push('</tr>');
      }
      h.push('</table>');
      h.push('<div class="table-actions">');
      h.push('<button onclick="event.stopPropagation();addTableSpanRow('+i+')" title="Добавить строку на всю ширину">➕</button>');
      h.push('<button onclick="event.stopPropagation();setCellColor()" title="Заливка ячейки цветом">🎨</button>');
      h.push('<button onclick="event.stopPropagation();removeCellColor()" title="Убрать заливку">✕</button>');
      h.push('<button onclick="event.stopPropagation();convertBlock('+i+',\'paragraph\')" title="Преобразовать в параграф">¶</button>');
      h.push('</div>');
    }
    h.push('</div>');
    h.push('<div class="block-adder"><button onclick="addBlockAt('+i+')" title="Добавить блок">+</button></div>');
  }
  c.innerHTML=h.join('');
  c.onclick=function(e){if(e.target===c)deselectBlock();};
  updateStats();
}

/* ─── Загрузка изображений ─── */
function uploadImage(idx){
  var inp=document.getElementById('imageInput');
  inp.dataset.idx=idx;
  inp.onchange=function(e){
    var file=e.target.files[0];
    if(!file){
      // user cancelled — remove the empty image block
      if(idx>=0&&idx<blocks.length&&blocks[idx].type==='image'&&!blocks[idx].imageData){
        removeBlock(idx);
      }
      return;
    }
    var reader=new FileReader();
    reader.onload=function(ev){
      var iidx=parseInt(inp.dataset.idx);
      if(iidx>=0&&iidx<blocks.length){
        blocks[iidx].imageData=ev.target.result;
        blocks[iidx].imageName=file.name;
        // save a copy to media/ so media manager sees it
        window.pywry.emit('image:upload', {data: ev.target.result, filename: file.name});
        renderBlocks();
        setTimeout(function(){selectBlock(iidx);},50);
      }
    };
    reader.readAsDataURL(file);
    inp.value='';
  };
  inp.click();
}

/* ─── Text Color ─── */
function toggleColorPopup(idx){
  // Сохраняем выделение перед открытием попапа
  saveSelection();
  var pop=document.getElementById('colorPop'+idx);
  if(!pop)return;
  var vis=pop.style.display!=='block';
  // close all color popups first
  var all=document.querySelectorAll('.tb-popup-color');
  for(var i=0;i<all.length;i++)all[i].style.display='none';
  pop.style.display=vis?'block':'none';
  // position below the button
  if(vis){
    var btn=pop.parentElement.querySelector('.has-popup');
    if(btn){
      var rect=btn.getBoundingClientRect();
      // offset relative to .block-toolbar (pop inside toolbar)
      var toolbar=pop.closest('.block-toolbar');
      if(toolbar){
        var tr=toolbar.getBoundingClientRect();
        pop.style.top=(rect.bottom-tr.top+2)+'px';
        pop.style.left='0px';
      }
    }
  }
}
function toggleSepPopup(idx){
  hideAllPopups();
  var pop=document.getElementById('sepPop'+idx);
  if(!pop)return;
  pop.style.display=pop.style.display==='block'?'none':'block';
}
function changeTextColor(color,idx){
  var el=document.querySelector('.block[data-idx="'+idx+'"]');
  if(!el)return;
  var ce=el.querySelector('[contenteditable]');
  if(!ce)return;
  // Восстанавливаем сохранённое выделение
  restoreSelection();
  var sel=window.getSelection();
  if(!sel||!sel.rangeCount)return;
  var range=sel.getRangeAt(0);
  if(range.collapsed)return;
  // Проверяем что выделение внутри нашего contenteditable
  if(!ce.contains(range.commonAncestorContainer))return;
  // Оборачиваем выделение в span с нужным цветом
  try{
    var span=document.createElement('span');
    span.style.color=color;
    var contents=range.extractContents();
    span.appendChild(contents);
    range.insertNode(span);
    // Восстанавливаем выделение внутрь span
    var nr=document.createRange();nr.selectNodeContents(span);
    sel.removeAllRanges();sel.addRange(nr);
  }catch(e){}
  // sync
  blocks[idx].content=ce.innerHTML;
  scheduleAutoSave();
  // close popup
  var pop=document.getElementById('colorPop'+idx);
  if(pop)pop.style.display='none';
}

/* ─── Paste handler — чистый текст, один блок ─── */
function initPasteHandler(){
  var be=document.getElementById('blockEditor');
  if(!be)return;
  be.addEventListener('paste',function(e){
    // Ищем contenteditable предок цели
    var ce=e.target.closest('[contenteditable]');
    if(!ce)return;
    e.preventDefault();
    // Получаем чистый текст из буфера
    var text=(e.clipboardData||window.clipboardData).getData('text/plain');
    if(!text)return;
    // Заменяем переносы строк на <br>
    var clean=text.replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
    // Вставляем в позицию курсора
    var sel=window.getSelection();
    if(sel&&sel.rangeCount>0){
      var range=sel.getRangeAt(0);
      // Если курсор внутри нашего contenteditable
      if(ce.contains(range.commonAncestorContainer)){
        range.deleteContents();
        var tmp=document.createElement('div');
        tmp.innerHTML=clean;
        var frag=document.createDocumentFragment();
        while(tmp.firstChild)frag.appendChild(tmp.firstChild);
        range.insertNode(frag);
        // перемещаем курсор в конец вставленного
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
        // синхронизируем данные блока
        var bl=ce.closest('.block');
        if(bl){
          var idx=parseInt(bl.dataset.idx);
          if(idx>=0&&idx<blocks.length){
            blocks[idx].content=ce.innerHTML;
            scheduleAutoSave();
          }
        }
      }
    }
  },true);
}

/* ─── Inline Image Popup ─── */
var activeInlineImg=null;

function initInlineEditor(){
  var be=document.getElementById('blockEditor');
  if(!be)return;
  // Используем capture:true, чтобы перехватить клик ДО того, как block.onclick сделает stopPropagation
  be.addEventListener('click',function(e){
    var img=e.target.closest('.block img');
    if(!img){
      // click outside img — close popup if clicking not on it
      var pop=document.getElementById('inlineImgPopup');
      if(pop&&!pop.contains(e.target)){
        pop.remove();activeInlineImg=null;
      }
      return;
    }
    e.stopPropagation();
    showInlinePopup(img);
  },true);
}

function showInlinePopup(img){
  activeInlineImg=img;
  var old=document.getElementById('inlineImgPopup');
  if(old)old.remove();
  var pop=document.createElement('div');
  pop.id='inlineImgPopup';
  pop.className='img-popup';
  // width slider
  var isBlockLevel=!img.closest('[contenteditable]');
  var wDiv=document.createElement('div');
  wDiv.className='img-popup-row';
  var curW=Math.round(parseInt(img.style.width)||img.naturalWidth||300);
  wDiv.innerHTML='<span class="img-popup-label">\u0428\u0438\u0440\u0438\u043d\u0430:</span><input type="range" min="40" max="'+Math.max(curW*2,800)+'" value="'+curW+'" style="flex:1"><span class="img-popup-value">'+curW+'px</span>';
  var inpR=wDiv.querySelector('input');
  inpR.oninput=function(){
    img.style.width=this.value+'px';
    img.style.maxWidth='100%';
    wDiv.querySelector('span:last-child').textContent=this.value+'px';
    if(!isBlockLevel)saveInlineContent();
  };
  pop.appendChild(wDiv);
  // actions row
  var aDiv=document.createElement('div');
  aDiv.className='img-popup-actions';
  if(isBlockLevel){
    aDiv.innerHTML='<button class="img-pop-btn del">\u2715 \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0431\u043b\u043e\u043a</button><button class="img-pop-btn rep">\ud83d\uddbc\ufe0f \u0417\u0430\u043c\u0435\u043d\u0438\u0442\u044c</button>';
    aDiv.querySelector('.del').onclick=function(){
      var bl=img.closest('.block');
      if(bl){var idx=parseInt(bl.dataset.idx);if(idx>=0)removeBlock(idx);}
      pop.remove();activeInlineImg=null;
    };
    aDiv.querySelector('.rep').onclick=function(){
      var bl=img.closest('.block');
      if(bl){var idx=parseInt(bl.dataset.idx);if(idx>=0)uploadImage(idx);}
      pop.remove();activeInlineImg=null;
    };
  }else{
    aDiv.innerHTML='<button class="img-pop-btn del">\u2715 \u0423\u0434\u0430\u043b\u0438\u0442\u044c</button><button class="img-pop-btn sep">\u25a1 \u0412 \u0431\u043b\u043e\u043a</button>';
    aDiv.querySelector('.del').onclick=function(){
      img.remove();saveInlineContent();pop.remove();activeInlineImg=null;
    };
    aDiv.querySelector('.sep').onclick=function(){
      var src=img.getAttribute('src'),alt=img.getAttribute('alt')||'';
      var ce=img.closest('[contenteditable]'),idx=-1;
      if(ce){var bl=ce.closest('.block');if(bl)idx=parseInt(bl.dataset.idx);}
      img.remove();saveInlineContent();
      if(idx>=0){
        var nb=createBlock('image');nb.imageData=src;nb.imageName=alt||'image';
        blocks.splice(idx+1,0,nb);renderBlocks();
        setTimeout(function(){var ni=blocks.indexOf(nb);if(ni>=0)selectBlock(ni);},50);
      }
      pop.remove();activeInlineImg=null;
    };
  }
  pop.appendChild(aDiv);
  document.body.appendChild(pop);
  // position
  var rect=img.getBoundingClientRect();
  var t=rect.bottom+6,b=rect.top-6;
  if(t+pop.offsetHeight>window.innerHeight&&b>0)t=b-pop.offsetHeight;
  var l=Math.max(4,Math.min(rect.left,window.innerWidth-pop.offsetWidth-4));
  pop.style.top=t+'px';pop.style.left=l+'px';
  // close on scroll
  var onscroll=function(){pop.remove();activeInlineImg=null;document.removeEventListener('scroll',onscroll,true);};
  document.addEventListener('scroll',onscroll,true);
}

function saveInlineContent(){
  if(!activeInlineImg)return;
  var ce=activeInlineImg.closest('[contenteditable]');
  if(!ce)return;
  var bl=ce.closest('.block');
  if(!bl)return;
  var i=parseInt(bl.dataset.idx);
  if(i>=0&&i<blocks.length)blocks[i].content=ce.innerHTML;
}
function inlineImage(idx,align){
  var inp=document.getElementById('imageInput');
  inp.dataset.idx=idx;
  inp.dataset.align=align;
  inp.onchange=function(e){
    var file=e.target.files[0];if(!file)return;
    var reader=new FileReader();
    reader.onload=function(ev){
      var idx2=parseInt(inp.dataset.idx);
      var al=inp.dataset.align||'left';
      var el=document.querySelector('.block[data-idx="'+idx2+'"]');
      if(!el)return;
      var ce=el.querySelector('[contenteditable]');
      if(!ce)return;
      var style='';
      if(al==='left')style='float:left; margin:4px 16px 8px 0; max-width:50%; border-radius:4px';
      else if(al==='right')style='float:right; margin:4px 0 8px 16px; max-width:50%; border-radius:4px';
      else style='display:block; margin:16px auto; max-width:100%; border-radius:4px';
      var imgHtml='<img src="'+ev.target.result+'" style="'+style+'" alt="'+esc(file.name)+'">';
      var sel=window.getSelection();
      if(sel&&sel.rangeCount>0&&ce.contains(sel.anchorNode)){
        var range=sel.getRangeAt(0);
        var frag=range.createContextualFragment(imgHtml);
        range.deleteContents();
        range.insertNode(frag);
      } else {
        ce.insertAdjacentHTML('beforeend',imgHtml);
      }
      blocks[idx2].content=ce.innerHTML;
      window.pywry.emit('image:upload', {data: ev.target.result, filename: file.name});
      updateBlockInfo();
    };
    reader.readAsDataURL(file);
    inp.value='';
  };
  inp.click();
}

/* ─── Ввод ─── */
function onBlockInput(idx){
  var el=document.querySelector('.block[data-idx="'+idx+'"]');
  if(!el)return;
  var ce=el.querySelector('[contenteditable]');
  if(ce)blocks[idx].content=ce.innerHTML;
  scheduleAutoSave();
  updateStats();
}
function splitBlockAtCursor(idx){
  var el=document.querySelector('.block[data-idx="'+idx+'"]');
  if(!el)return;
  var ce=el.querySelector('[contenteditable]');
  if(!ce)return;
  // Используем ТЕКУЩУЮ позицию курсора (не сохранённое выделение — оно может быть устаревшим)
  restoreCaret();
  var sel=window.getSelection();
  if(!sel||!sel.rangeCount)return;
  var range=sel.getRangeAt(0);
  // Проверяем, что курсор внутри нашего contenteditable
  if(!ce.contains(range.commonAncestorContainer))return;
  // Создаём range для левой части (от начала до курсора/начала выделения)
  var leftRange=document.createRange();
  leftRange.selectNodeContents(ce);
  leftRange.setEnd(range.startContainer,range.startOffset);
  // Создаём range для правой части (от курсора/начала выделения до конца)
  // ВАЖНО: используем startContainer/startOffset, а не endContainer/endOffset,
  // чтобы выделенный текст не потерялся
  var rightRange=document.createRange();
  rightRange.selectNodeContents(ce);
  rightRange.setStart(range.startContainer,range.startOffset);
  // Клонируем содержимое (не вырезаем, а копируем)
  var leftFrag=leftRange.cloneContents();
  var rightFrag=rightRange.cloneContents();
  // Если справа пусто — не разделяем
  if(!rightFrag.textContent.trim())return;
  // Получаем HTML из фрагментов (сохраняет все теги, стили, цвета)
  var leftDiv=document.createElement('div');
  leftDiv.appendChild(leftFrag);
  var rightDiv=document.createElement('div');
  rightDiv.appendChild(rightFrag);
  var leftHTML=leftDiv.innerHTML;
  var rightHTML=rightDiv.innerHTML;
  // Обновляем текущий блок левой частью
  saveHistory();
  blocks[idx].content=leftHTML;
  // Создаём новый блок с правой частью
  var nb=createBlock('paragraph');
  nb.content=rightHTML;
  blocks.splice(idx+1,0,nb);
  renderBlocks();
  setTimeout(function(){selectBlock(idx+1);},50);
  scheduleAutoSave();
}
/* ─── Объединение блоков ─── */
function mergeBlockWithPrevious(idx){
  if(idx<=0)return;
  saveHistory();
  var prev=blocks[idx-1];
  var curr=blocks[idx];
  if(!prev||!curr)return;
  // Если оба — параграфы или совместимые типы, объединяем
  var prevHTML=prev.content||'';
  var currHTML=curr.content||'';
  // Сливаем: содержимое предыдущего + br + содержимое текущего
  // Убираем пустые обёртки у обоих
  if(!currHTML.trim()){
    blocks.splice(idx,1);
  }else{
    blocks[idx-1].content=prevHTML+'<br>'+currHTML;
    blocks.splice(idx,1);
  }
  renderBlocks();
  setTimeout(function(){selectBlock(idx-1);},50);
  scheduleAutoSave();
}
function onBlockKeydown(e,idx){
  if(e.ctrlKey&&e.key==='q'){e.preventDefault();window.pywry.emit('window:action',{action:'close'});return;}
  var b=blocks[idx];
  if(e.key==='Enter'&&!e.shiftKey&&(b.type==='paragraph'||b.type==='heading'||b.type==='quote')){
    e.preventDefault();
    // insert <br> inside current block (new line, not new block)
    var sel=window.getSelection();
    if(sel&&sel.rangeCount>0){
      var range=sel.getRangeAt(0);
      var br=document.createElement('br');
      range.deleteContents();
      range.insertNode(br);
      range.setStartAfter(br);
      range.setEndAfter(br);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    // update content
    var el=document.querySelector('.block[data-idx="'+idx+'"]');
    if(el){var ce=el.querySelector('[contenteditable]');if(ce)blocks[idx].content=ce.innerHTML;}
    return;
  }
  if(e.key==='Enter'&&e.shiftKey&&(b.type==='paragraph'||b.type==='heading'||b.type==='quote'||b.type==='code')){
    e.preventDefault();saveCaret();splitBlockAtCursor(idx);
    return;
  }
  if(e.key==='Backspace'){
    var el=document.querySelector('.block[data-idx="'+idx+'"]');
    if(el){var ce=el.querySelector('[contenteditable]');
      if(ce){
        // Курсор в начале блока — объединяем с предыдущим
        if(idx>0){
          var sel=window.getSelection();
          if(sel&&sel.rangeCount>0){
            var range=sel.getRangeAt(0);
            var br=document.createRange();br.selectNodeContents(ce);
            br.setEnd(range.startContainer,range.startOffset);
            if(!br.toString().trim()){
              e.preventDefault();
              mergeBlockWithPrevious(idx);
              return;
            }
          }
        }
        // Блок пуст — удаляем
        if(ce.innerHTML===''||ce.innerHTML==='<br>'){
          e.preventDefault();removeBlock(idx);
        }
      }
    }
  }
}
function convertBlock(idx,newType,level){
  var b=blocks[idx],oc=b.content;b.type=newType;b.level=level||'';
  if(b.rows){
    var texts=[];
    for(var ri=0;ri<b.rows.length;ri++)for(var ci=0;ci<b.rows[ri].length;ci++)texts.push(b.rows[ri][ci].content||'');
    oc=texts.join(' ');
    delete b.rows;
  }
  if(newType==='list')b.content='<ul><li>'+(oc||'')+'</li></ul>';
  else if(newType==='separator')b.content='';
  else b.content=oc||'';
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function changeHeadingLevel(l){if(selectedBlock>=0){blocks[selectedBlock].level=l;renderBlocks();setTimeout(function(){selectBlock(selectedBlock);},50);}}
/* ─── Таблица ─── */
function addTableRowAt(idx,ri){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table')return;
  var cols=b.rows[0]?b.rows[0].length:2,row=[];
  for(var ci=0;ci<cols;ci++)row.push({content:''});
  b.rows.splice(ri+1,0,row);
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function addTableColAt(idx,ci){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table')return;
  var cols=2;
  for(var tmp=0;tmp<b.rows.length;tmp++){if(b.rows[tmp].length>1){cols=b.rows[tmp].length;break;}}
  for(var ri=0;ri<b.rows.length;ri++){
    if(b.rows[ri].length===1&&cols>1)continue;
    b.rows[ri].splice(ci+1,0,{content:''});
  }
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function addTableSpanRow(idx){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table')return;
  b.rows.push([{content:'',bg:''}]);
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function setColWidth(idx,ci){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table')return;
  var cur=b.colWidths&&b.colWidths[ci]?b.colWidths[ci]:'';
  var w=prompt('Ширина колонки '+(ci+1)+' (в пикселях):',cur);
  if(w===null)return;
  w=parseInt(w);
  if(!w||w<20){w='';}else{w=Math.min(w,800);}
  if(!b.colWidths)b.colWidths=[];
  b.colWidths[ci]=w||0;
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function splitSpanRow(idx,ri){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table')return;
  var cols=2;
  for(var tmp=0;tmp<b.rows.length;tmp++){if(b.rows[tmp].length>1){cols=b.rows[tmp].length;break;}}
  var cells=[];
  for(var ci=0;ci<cols;ci++)cells.push({content:'',bg:b.rows[ri][0].bg||''});
  b.rows[ri]=cells;
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function delTableRowAt(idx,ri){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table'||b.rows.length<=1)return;
  b.rows.splice(ri,1);
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function delTableColAt(idx,ci){
  saveHistory();var b=blocks[idx];if(!b||b.type!=='table'||b.rows.length<=0)return;
  var cols=2;
  for(var tmp=0;tmp<b.rows.length;tmp++){if(b.rows[tmp].length>1){cols=b.rows[tmp].length;break;}}
  if(cols<=1)return;
  for(var ri=0;ri<b.rows.length;ri++){
    if(b.rows[ri].length===1&&cols>1)continue;
    b.rows[ri].splice(ci,1);
  }
  renderBlocks();setTimeout(function(){selectBlock(idx);},50);
}
function onTableInput(idx,ri,ci){
  var td=event.target;
  blocks[idx].rows[ri][ci].content=td.innerHTML;
  scheduleAutoSave();
}
/* ─── Цвет ячейки таблицы ─── */
function setCellColor(){
  if(!selectedCell)return;
  var b=blocks[selectedCell.block];
  if(!b||b.type!=='table')return;
  var picker=document.getElementById('cellColorPicker');
  picker.dataset.block=selectedCell.block;
  picker.dataset.row=selectedCell.row;
  picker.dataset.col=selectedCell.col;
  picker.click();
}
document.getElementById('cellColorPicker').addEventListener('change',function(){
  var blk=parseInt(this.dataset.block),ri=parseInt(this.dataset.row),ci=parseInt(this.dataset.col);
  if(!isNaN(blk)&&!isNaN(ri)&&!isNaN(ci)&&blocks[blk]&&blocks[blk].rows&&blocks[blk].rows[ri]&&blocks[blk].rows[ri][ci]){
    blocks[blk].rows[ri][ci].bg=this.value;
    renderBlocks();setTimeout(function(){selectBlock(blk);},50);
  }
});
function removeCellColor(){
  if(!selectedCell)return;
  var b=blocks[selectedCell.block];
  if(!b||b.type!=='table')return;
  b.rows[selectedCell.row][selectedCell.col].bg='';
  renderBlocks();setTimeout(function(){selectBlock(selectedCell.block);},50);
}
/* ─── Размер шрифта ─── */
function changeFontSize(el){
  var v=parseInt(el.value);el.value='';
  if(!v)return;
  var sel=window.getSelection();
  if(!sel.rangeCount||sel.isCollapsed)return;
  var range=sel.getRangeAt(0);
  var span=document.createElement('span');
  span.style.fontSize=v+'px';
  try{range.surroundContents(span);}catch(e){}
  var ae=document.activeElement;
  if(ae&&ae.isContentEditable)ae.focus();
  // sync block content immediately
  if(selectedBlock>=0){
    var ce=document.querySelector('.block.selected [contenteditable]');
    if(ce)blocks[selectedBlock].content=ce.innerHTML;
  }
}
function fmtBlock(c){document.execCommand(c,false,null);var el=document.querySelector('.block.selected [contenteditable]');if(el){el.focus();if(selectedBlock>=0)blocks[selectedBlock].content=el.innerHTML;}}
function insertLinkBlock(){var u=prompt('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 URL:');if(!u)return;document.execCommand('createLink',false,u);if(selectedBlock>=0){var el=document.querySelector('.block.selected [contenteditable]');if(el)blocks[selectedBlock].content=el.innerHTML;}scheduleAutoSave();}

/* ─── Импорт Markdown ─── */
function importMarkdown(){
  var inp=document.getElementById('mdImportInput');
  if(!inp){
    inp=document.createElement('input');
    inp.type='file';inp.id='mdImportInput';inp.accept='.md,.markdown,.txt';inp.style.display='none';
    document.body.appendChild(inp);
  }
  inp.onchange=function(e){
    var file=e.target.files[0];if(!file)return;
    var reader=new FileReader();
    reader.onload=function(ev){parseMarkdown(ev.target.result);};
    reader.readAsText(file);
    inp.value='';
  };
  inp.click();
}
function parseMarkdown(md){
  var lines=md.split('\n'),newBlocks=[],i=0;
  while(i<lines.length){
    var line=lines[i],tl=line.trim();
    if(!tl){i++;continue;}
    if(tl.startsWith('### ')){var b=createBlock('heading','h3');b.content=tl.substring(4).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>');newBlocks.push(b);i++;continue;}
    if(tl.startsWith('## ')){var b=createBlock('heading','h2');b.content=tl.substring(3).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>');newBlocks.push(b);i++;continue;}
    if(tl.startsWith('# ')){var b=createBlock('heading','h2');b.content=tl.substring(2).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>');newBlocks.push(b);i++;continue;}
    if(tl.startsWith('> ')){var b=createBlock('quote');b.content=tl.substring(2).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>');newBlocks.push(b);i++;continue;}
    if(tl.startsWith('- ')||tl.startsWith('* ')){
      var items=[];
      while(i<lines.length&&(lines[i].trim().startsWith('- ')||lines[i].trim().startsWith('* '))){
        var txt=lines[i].trim().substring(2);
        items.push('<li>'+txt.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>')+'</li>');
        i++;
      }
      var b=createBlock('list');b.content='<ul>'+items.join('')+'</ul>';newBlocks.push(b);continue;
    }
    if(tl.startsWith('---')||tl.startsWith('***')){newBlocks.push(createBlock('separator'));i++;continue;}
    if(tl.startsWith('```')){
      var codeLines=[];i++;
      while(i<lines.length&&!lines[i].trim().startsWith('```')){codeLines.push(lines[i]);i++;}
      i++;var b=createBlock('code');b.content=codeLines.join('\n');newBlocks.push(b);continue;
    }
    if(tl.startsWith('![')){
      var m=tl.match(/!\[(.*?)\]\((.*?)\)/);
      if(m){var b=createBlock('image');b.imageName=m[1];b.imageData=m[2];newBlocks.push(b);}
      i++;continue;
    }
    // paragraph (may be multi-line)
    var pLines=[];
    while(i<lines.length){
      var tl2=lines[i].trim();
      if(!tl2||tl2.startsWith('#')||tl2.startsWith('>')||tl2.startsWith('- ')||tl2.startsWith('* ')||tl2.startsWith('---')||tl2.startsWith('```')||tl2.startsWith('!['))break;
      pLines.push(tl2.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>'));
      i++;
    }
    var b=createBlock('paragraph');b.content=pLines.join('<br>');newBlocks.push(b);
  }
  // filter out empty blocks
  newBlocks=newBlocks.filter(function(b){
    if(b.type==='separator')return true;
    if(b.type==='image')return !!b.imageData;
    if(b.type==='table')return true;
    return b.content.trim().length>0;
  });
  if(!newBlocks.length)newBlocks.push(createBlock('paragraph'));
  blocks=newBlocks;selectedBlock=-1;renderBlocks();
  if(blocks.length)setTimeout(function(){selectBlock(0);},50);
  showToast('Импортировано блоков: '+newBlocks.length);
}
/* Window control helpers */
var winMaximized=false,savedWinSize=null;
function toggleMaximize(){
  if(!winMaximized){
    // запоминаем текущий размер перед разворачиванием
    savedWinSize={w:window.innerWidth,h:window.innerHeight};
    winMaximized=true;
    document.getElementById('btnMaximize').textContent='🗗';
    document.getElementById('btnMaximize').title='Свернуть в окно';
    window.pywry.emit('window:action',{action:'maximize'});
  }else{
    // восстанавливаем исходный размер
    winMaximized=false;
    document.getElementById('btnMaximize').textContent='🗖';
    document.getElementById('btnMaximize').title='Развернуть';
    var sz=savedWinSize||{w:1100,h:700};
    window.pywry.emit('window:action',{action:'restore',width:sz.w,height:sz.h});
  }
}
function hideAllPopups(){
  document.querySelectorAll('.tb-popup').forEach(function(el){el.style.display='none';});
}
function toggleImagePopup(idx){
  hideAllPopups();
  var pop=document.getElementById('imgPop'+idx);
  if(pop)pop.style.display=pop.style.display==='block'?'none':'block';
}
function toggleAiPopup(idx){
  hideAllPopups();
  var pop=document.getElementById('aiPop'+idx);
  if(!pop)return;
  var show=pop.style.display!=='block';
  pop.style.display=show?'block':'none';
  if(show){
    // Сброс стилей позиции
    pop.style.top='';
    pop.style.bottom='';
    // Даём браузеру отрендерить, чтобы измерить высоту меню
    var menuH=pop.scrollHeight||120;
    var rect=pop.parentNode.getBoundingClientRect();
    if(rect.bottom+menuH>window.innerHeight-10){
      // Открываем вверх — переопределяем top:100% из CSS
      pop.style.top='auto';
      pop.style.bottom='100%';
    }
    // Иначе CSS .tb-popup{top:100%} работает как обычно (вниз)
  }
}
function aiFromToolbar(idx,action){
  hideAllPopups();
  // Сначала проверяем, есть ли выделение текста в contenteditable
  var selText='',text='';
  var sel=window.getSelection();
  if(sel&&sel.rangeCount>0&&!sel.isCollapsed){
    var el=sel.anchorNode&&(sel.anchorNode.nodeType===3?sel.anchorNode.parentNode:sel.anchorNode);
    if(el&&el.closest&&el.closest('[contenteditable]')){
      selText=sel.toString().trim();
    }
  }
  if(selText){
    text=selText;
    // Сохраняем информацию о выделении для вставки в позицию
    pendingAiSelInfo={blockIdx:selectedBlock>=0?selectedBlock:idx,text:selText};
  } else {
    pendingAiSelInfo=null;
    // Если выбрано несколько блоков — объединяем их содержимое
    var targets=selectedBlocks.length>1?selectedBlocks:[idx];
    for(var ti=0;ti<targets.length;ti++){
      var b=blocks[targets[ti]];
      if(!b)continue;
      if(b.type==='paragraph'||b.type==='heading'||b.type==='quote'||b.type==='code'||b.type==='list'){
        var d=document.createElement('div');d.innerHTML=b.content;
        var t=d.textContent||d.innerText||'';
        if(t.trim())text+=t.trim()+'\n\n';
      }
    }
    text=text.trim();
  }
  if(!text){showToast('\u041d\u0435\u0442 \u0442\u0435\u043a\u0441\u0442\u0430 \u0432 \u0431\u043b\u043e\u043a\u0430\u0445');return;}
  // Показываем загрузку под первым блоком
  pendingAiBlockIdx=selectedBlock>=0?selectedBlock:idx;
  document.getElementById('aiChatInput').value=text;
  // check model
  var model=getAiModel();
  if(!model){showToast('\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043c\u043e\u0434\u0435\u043b\u044c \u0432 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u0445 \u0438 \u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u043a\u043d\u043e\u043f\u043a\u0443 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f \u0435\u0449\u0451 \u0440\u0430\u0437');return;}
  // Показываем индикатор загрузки под блоком
  showAiLoading(pendingAiBlockIdx);
  // execute action (попап уже скрыт в начале функции)
  setTimeout(function(){aiAction(action);},200);
}
function showAiLoading(idx){
  removeAiInlineResult();
  var div=document.createElement('div');
  div.id='aiInlineResult';
  div.className='ai-inline-result loading';
  div.innerHTML='<div class="ai-inline-loading">\u23f3 \u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 AI...</div>';
  document.body.appendChild(div);
}
function showAiResultInline(text){
  removeAiInlineResult();
  var div=document.createElement('div');
  div.id='aiInlineResult';
  div.className='ai-inline-result';
  var escText=esc(text);
  div.innerHTML='<div class="ai-inline-header">\ud83e\udd16 \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 AI</div><div class="ai-inline-text">'+escText+'</div><div class="ai-inline-actions"><button onclick="replaceWithAiResult()" title="\u0417\u0430\u043c\u0435\u043d\u0438\u0442\u044c \u0442\u0435\u043a\u0443\u0449\u0438\u0439 \u0431\u043b\u043e\u043a">\ud83d\udd04 \u0417\u0430\u043c\u0435\u043d\u0438\u0442\u044c</button><button onclick="insertAiResultBelow()" title="\u0412\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043a\u0430\u043a \u043d\u043e\u0432\u044b\u0439 \u0431\u043b\u043e\u043a \u0441\u043d\u0438\u0437\u0443">➕ \u0412\u0441\u0442\u0430\u0432\u0438\u0442\u044c</button><button onclick="discardAiResult()" title="\u041e\u0442\u043c\u0435\u043d\u0430">\u2715</button></div>';
  document.body.appendChild(div);
}
function removeAiInlineResult(){
  var el=document.getElementById('aiInlineResult');
  if(el)el.parentNode.removeChild(el);
}
/* ─── Markdown → HTML / блоки (для AI) ─── */
function mdToHtml(text){
  // inline Markdown → HTML: **bold** *italic* `code` [link](url)
  return text
    .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
    .replace(/\*(.+?)\*/g,'<i>$1</i>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2">$1</a>');
}
function mdToBlocks(md){
  // Парсит Markdown в массив блоков (не трогает blocks)
  var lines=md.split('\n'),newBlocks=[],i=0;
  while(i<lines.length){
    var line=lines[i],tl=line.trim();
    if(!tl){i++;continue;}
    if(tl.startsWith('### ')){
      var b=createBlock('heading','h3');b.content=mdToHtml(tl.substring(4));newBlocks.push(b);i++;continue;
    }
    if(tl.startsWith('## ')){
      var b=createBlock('heading','h2');b.content=mdToHtml(tl.substring(3));newBlocks.push(b);i++;continue;
    }
    if(tl.startsWith('# ')){
      var b=createBlock('heading','h2');b.content=mdToHtml(tl.substring(2));newBlocks.push(b);i++;continue;
    }
    if(tl.startsWith('> ')){
      var b=createBlock('quote');b.content=mdToHtml(tl.substring(2));newBlocks.push(b);i++;continue;
    }
    if(tl.startsWith('- ')||tl.startsWith('* ')||/^\d+[\.\)]\s/.test(tl)){
      var items=[],isOrdered=/^\d+[\.\)]\s/.test(tl);
      while(i<lines.length){
        var itl=lines[i].trim();
        if(itl.startsWith('- ')||itl.startsWith('* ')){items.push('<li>'+mdToHtml(itl.substring(2))+'</li>');i++;continue;}
        if(/^\d+[\.\)]\s/.test(itl)){items.push('<li>'+mdToHtml(itl.replace(/^\d+[\.\)]\s/,''))+'</li>');i++;continue;}
        break;
      }
      var b=createBlock('list');b.content='<ul>'+items.join('')+'</ul>';newBlocks.push(b);continue;
    }
    if(tl.startsWith('---')||tl.startsWith('***')){newBlocks.push(createBlock('separator'));i++;continue;}
    if(tl.startsWith('```')){
      var codeLines=[];i++;
      while(i<lines.length&&!lines[i].trim().startsWith('```')){codeLines.push(lines[i]);i++;}
      i++;var b=createBlock('code');b.content=codeLines.join('\n');newBlocks.push(b);continue;
    }
    if(tl.startsWith('![')){
      var m=tl.match(/!\[(.*?)\]\((.*?)\)/);
      if(m){var b=createBlock('image');b.imageName=m[1];b.imageData=m[2];newBlocks.push(b);}
      i++;continue;
    }
    // параграф (может быть многострочным) — собираем строки
    var pLines=[];
    while(i<lines.length){
      var tl2=lines[i].trim();
      if(!tl2||tl2.startsWith('#')||tl2.startsWith('>')||tl2.startsWith('- ')||tl2.startsWith('* ')||/^\d+[\.\)]\s/.test(tl2)||tl2.startsWith('---')||tl2.startsWith('```')||tl2.startsWith('!['))break;
      pLines.push(mdToHtml(tl2));
      i++;
    }
    if(pLines.length){
      // Smart split: если каждая строка выглядит как отдельный пункт
      // (заканчивается на ?!.) — разделяем на отдельные блоки
      var allShort=pLines.every(function(l){return l.length<120;});
      var allEndPunct=pLines.every(function(l){return /[.?!]$/.test(l.replace(/<[^>]+>/g,''));});
      if(pLines.length>1&&allEndPunct&&allShort){
        for(var pi=0;pi<pLines.length;pi++){
          var b=createBlock('paragraph');b.content=pLines[pi];newBlocks.push(b);
        }
      } else {
        var b=createBlock('paragraph');b.content=pLines.join('<br>');newBlocks.push(b);
      }
    }
  }
  // фильтр пустых блоков
  newBlocks=newBlocks.filter(function(b){
    if(b.type==='separator')return true;
    if(b.type==='image')return !!b.imageData;
    if(b.type==='table')return true;
    return b.content.trim().length>0;
  });
  if(!newBlocks.length)newBlocks.push(createBlock('paragraph'));
  return newBlocks;
}
/* ─── Применение AI-результата с разбором Markdown ─── */
function replaceWithAiResult(){
  if(pendingAiBlockIdx<0||pendingAiBlockIdx>=blocks.length)return;
  if(!aiResultText)return;
  saveHistory();
  // Если было выделение — заменяем текст внутри блока
  if(pendingAiSelInfo&&pendingAiSelInfo.blockIdx===pendingAiBlockIdx){
    var b=blocks[pendingAiBlockIdx];
    if(b&&(b.type==='paragraph'||b.type==='heading'||b.type==='quote'||b.type==='list')){
      var html=b.content;
      var plainIdx=html.indexOf(pendingAiSelInfo.text);
      if(plainIdx>=0){
        // Заменяем выделенный текст на AI-результат
        var before=html.substring(0,plainIdx);
        var after=html.substring(plainIdx+pendingAiSelInfo.text.length);
        b.content=before+aiResultText.trim()+after;
        removeAiInlineResult();
        pendingAiBlockIdx=-1;pendingAiSelInfo=null;aiResultText='';
        renderBlocks();scheduleAutoSave();
        setTimeout(function(){selectBlock(pendingAiBlockIdx<blocks.length?pendingAiBlockIdx:blocks.length-1);},50);
        return;
      }
    }
  }
  // По умолчанию: заменяем весь блок
  var newBlocks=mdToBlocks(aiResultText);
  var args=[pendingAiBlockIdx,1].concat(newBlocks);
  blocks.splice.apply(blocks,args);
  removeAiInlineResult();
  pendingAiBlockIdx=-1;aiResultText='';
  renderBlocks();scheduleAutoSave();
  setTimeout(function(){selectBlock(pendingAiBlockIdx<blocks.length?pendingAiBlockIdx:blocks.length-1);},50);
}
function insertAiResultBelow(){
  if(pendingAiBlockIdx<0)return;
  if(!aiResultText)return;
  saveHistory();
  // Если было выделение — вставляем AI-результат ПОСЛЕ выделенного текста
  if(pendingAiSelInfo&&pendingAiSelInfo.blockIdx===pendingAiBlockIdx){
    var b=blocks[pendingAiBlockIdx];
    if(b&&(b.type==='paragraph'||b.type==='heading'||b.type==='quote'||b.type==='list')){
      var html=b.content;
      var plainIdx=html.indexOf(pendingAiSelInfo.text);
      if(plainIdx>=0){
        // Разбиваем блок: часть до выделения остаётся,
        // часть после выделения + AI-результат идут новыми блоками
        var selectionLen=pendingAiSelInfo.text.length;
        b.content=html.substring(0,plainIdx+selectionLen);
        // AI-результат как новый блок
        var aiBlocks=mdToBlocks(aiResultText);
        // Остаток после выделения — в конец
        var remainder=html.substring(plainIdx+selectionLen);
        if(remainder.trim()){
          var rb=createBlock(b.type);rb.content=remainder;
          aiBlocks.push(rb);
        }
        var insertIdx=pendingAiBlockIdx+1;
        var args=[insertIdx,0].concat(aiBlocks);
        blocks.splice.apply(blocks,args);
        removeAiInlineResult();
        pendingAiBlockIdx=-1;pendingAiSelInfo=null;aiResultText='';
        renderBlocks();scheduleAutoSave();
        setTimeout(function(){selectBlock(insertIdx<blocks.length?insertIdx:blocks.length-1);},50);
        return;
      }
    }
  }
  // По умолчанию: новый блок снизу
  var newBlocks=mdToBlocks(aiResultText);
  var insertIdx=pendingAiBlockIdx+1;
  var args=[insertIdx,0].concat(newBlocks);
  blocks.splice.apply(blocks,args);
  removeAiInlineResult();
  pendingAiBlockIdx=-1;aiResultText='';
  renderBlocks();scheduleAutoSave();
  setTimeout(function(){selectBlock(insertIdx<blocks.length?insertIdx:blocks.length-1);},50);
}
function discardAiResult(){
  removeAiInlineResult();
  pendingAiBlockIdx=-1;pendingAiSelInfo=null;
  showToast('\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e');
}
var aiChatTargetIdx=-1;
var chatSystem='\u041e\u0442\u0432\u0435\u0442\u044c \u043d\u0430 \u0437\u0430\u043f\u0440\u043e\u0441 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f. \u0420\u0430\u0437\u0431\u0438\u0432\u0430\u0439 \u043e\u0442\u0432\u0435\u0442 \u043d\u0430 \u0430\u0431\u0437\u0430\u0446\u044b: \u043a\u0430\u0436\u0434\u044b\u0439 \u043d\u043e\u0432\u044b\u0439 \u043f\u0443\u043d\u043a\u0442 \u0438\u043b\u0438 \u043c\u044b\u0441\u043b\u044c \u0441 \u043d\u043e\u0432\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438, \u043c\u0435\u0436\u0434\u0443 \u0430\u0431\u0437\u0430\u0446\u0430\u043c\u0438 \u043f\u0443\u0441\u0442\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430. \u041d\u0435 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 Markdown-\u0440\u0430\u0437\u043c\u0435\u0442\u043a\u0443 (\u043a\u0440\u043e\u043c\u0435 **\u0436\u0438\u0440\u043d\u043e\u0433\u043e**, *\u043a\u0443\u0440\u0441\u0438\u0432\u0430*). \u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u043d\u0443\u0436\u043d\u044b\u043c \u0442\u0435\u043a\u0441\u0442\u043e\u043c, \u0431\u0435\u0437 \u043f\u043e\u044f\u0441\u043d\u0435\u043d\u0438\u0439.';
function aiChatSend(){
  var input=document.getElementById('aiChatInput');
  var text=input.value.trim();
  if(!text){showToast('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441');return;}
  // Определяем целевой блок: текущий выделенный или последний
  aiChatTargetIdx=selectedBlock>=0?selectedBlock:blocks.length-1;
  if(aiChatTargetIdx<0)aiChatTargetIdx=0;
  // Используем настройки из нижней панели (независимо от правой)
  var prov=document.getElementById('aiProviderQuick').value;
  var model=document.getElementById('aiModelQuick').value;
  if(!model){showToast('\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 \u0438 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043c\u043e\u0434\u0435\u043b\u044c');return;}
  var cfg=getProviderConfig(prov);
  if(!cfg.url){showToast('\u041d\u0435\u0442 URL \u0434\u043b\u044f \u043f\u0440\u043e\u0432\u0430\u0439\u0434\u0435\u0440\u0430. \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u0442\u0435 \u0432 \u043f\u0440\u0430\u0432\u043e\u0439 \u043f\u0430\u043d\u0435\u043b\u0438');return;}
  pendingAiBlockIdx=aiChatTargetIdx;
  showAiLoading(aiChatTargetIdx);
  input.value='';
  // Подсказка по типу материала
  var typeHints={
    'news':'📰 Это новость. Пиши кратко, 1\u20133 абзаца, обязательно укажи дату события. ',
    'article':'📄 Это статья. Пиши развёрнуто, с заголовками разделов, укажи автора. ',
    'post':''
  };
  var system=(typeHints[currentType]||'')+chatSystem;
  window.pywry.emit('ai:query',{
    prompt:text,system:system,
    model:model,api_url:cfg.url,api_key:cfg.key,
    provider:prov
  });
}
function insertImageBlock(idx){
  var b=createBlock('image');
  blocks.splice(idx+1,0,b);
  renderBlocks();
  setTimeout(function(){
    var n=blocks.indexOf(b);
    if(n>=0){selectBlock(n);uploadImage(n);}
  },100);
}

/* ─── Контент ─── */
function collectContent(){
  // sync all blocks from DOM before collecting
  for(var si=0;si<blocks.length;si++){
    var sel=document.querySelector('.block[data-idx="'+si+'"]');
    if(!sel)continue;
    var ce=sel.querySelector('[contenteditable]');
    if(ce&&(blocks[si].type==='paragraph'||blocks[si].type==='heading'||blocks[si].type==='quote'||blocks[si].type==='code'))blocks[si].content=ce.innerHTML;
  }
  var title=document.getElementById('postTitle').value.trim();
  var tagsInput=document.getElementById('postTags').value.trim();
  var tags=tagsInput?tagsInput.split(',').map(function(t){return t.trim();}).filter(function(t){return t;}):[];
  var html=[];
  for(var i=0;i<blocks.length;i++){
    var b=blocks[i];
    if(b.type==='paragraph')html.push('<p>'+b.content+'</p>');
    else if(b.type==='heading')html.push('<'+b.level+'>'+b.content+'</'+b.level+'>');
    else if(b.type==='list')html.push(b.content);
    else if(b.type==='quote')html.push('<blockquote>'+b.content+'</blockquote>');
    else if(b.type==='separator')html.push('<hr>');
    else if(b.type==='code')html.push('<pre><code>'+b.content+'</code></pre>');
    else if(b.type==='image'&&b.imageData)html.push('<figure><img src="'+b.imageData+'" alt="'+(b.imageName||'')+'"></figure>');
    else if(b.type==='table'){
      // Определяем реальное количество столбцов
      var pCols=2;
      for(var tmp=0;tmp<b.rows.length;tmp++){if(b.rows[tmp].length>1){pCols=b.rows[tmp].length;break;}}
      html.push('<table>');
      for(var ri=0;ri<b.rows.length;ri++){
        html.push('<tr>');
        for(var ci=0;ci<b.rows[ri].length;ci++){
          var cell=b.rows[ri][ci];
          var pStyle=cell.bg?' style="background:'+cell.bg+'"':'';
          var pColspan=(b.rows[ri].length===1&&pCols>1)?' colspan="'+pCols+'"':'';
          var pWidth=(b.colWidths&&b.colWidths[ci])?' style="'+(pStyle?pStyle.slice(1):'')+'width:'+b.colWidths[ci]+'px"':pStyle;
          if(b.colWidths&&b.colWidths[ci]&&!cell.bg)pWidth=' width="'+b.colWidths[ci]+'"';
          html.push('<td'+pColspan+pWidth+'>'+(cell.content||'')+'</td>');
        }
        html.push('</tr>');
      }
      html.push('</table>');
    }
  }
  return{title:title,content:html.join('\n'),tags:tags};
}
function parseContent(content){
  if(!content)return[createBlock('paragraph')];
  var result=[],div=document.createElement('div');div.innerHTML=content;
  for(var i=0;i<div.children.length;i++){
    var el=div.children[i],tag=el.tagName.toLowerCase(),id='b'+(++blockCounter)+'_'+Date.now().toString(36);
    if(tag==='p')result.push({id:id,type:'paragraph',level:'',content:el.innerHTML,imageData:null,imageName:null});
    else if(tag.match(/^h[2-4]$/))result.push({id:id,type:'heading',level:tag,content:el.innerHTML,imageData:null,imageName:null});
    else if(tag==='ul'||tag==='ol')result.push({id:id,type:'list',level:tag,content:el.outerHTML,imageData:null,imageName:null});
    else if(tag==='blockquote')result.push({id:id,type:'quote',level:'',content:el.innerHTML,imageData:null,imageName:null});
    else if(tag==='hr')result.push({id:id,type:'separator',level:'',content:'',imageData:null,imageName:null});
    else if(tag==='pre'){var code=el.querySelector('code');result.push({id:id,type:'code',level:'',content:code?code.innerHTML:el.innerHTML,imageData:null,imageName:null});}
    else if(tag==='figure'){var img=el.querySelector('img');if(img){result.push({id:id,type:'image',level:'',content:'',imageData:img.src,imageName:img.alt||''});}else{result.push({id:id,type:'image',level:'',content:'',imageData:null,imageName:null});}}
    else result.push({id:id,type:'paragraph',level:'',content:el.innerHTML,imageData:null,imageName:null});
  }
  if(result.length===0)result.push(createBlock('paragraph'));
  return result;
}

/* ─── Сохранение ─── */
var autoSaveTimer=null;
function setAutoSaveStatus(status){
  var ind=document.getElementById('autoSaveIndicator');
  if(!ind)return;
  if(status==='saved'){ind.style.background='var(--status-ok)';ind.title='\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e';}
  else if(status==='saving'){ind.style.background='var(--status-load)';ind.title='\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435...';}
  else if(status==='unsaved'){ind.style.background='var(--status-err)';ind.title='\u041d\u0435\u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e';}
}
function scheduleAutoSave(){
  if(autoSaveTimer)clearTimeout(autoSaveTimer);
  setAutoSaveStatus('unsaved');
  autoSaveTimer=setTimeout(doAutoSave,2000);
}
function doImmediateSave(){
  var title=document.getElementById('postTitle').value.trim();
  if(!title){title=nextDraftTitle();document.getElementById('postTitle').value=title;}
  if(!currentId){currentId=Date.now().toString(36)+Math.random().toString(36).substring(2,6);}
  setAutoSaveStatus('saving');
  var data=collectContent();
  var item={id:currentId,title:title,content:data.content,tags:data.tags,
    news_date:document.getElementById('newsDate').value||'',
    article_author:document.getElementById('articleAuthor').value||'',
    article_rubric:document.getElementById('articleRubric').value||''};
  window.pywry.emit('content:save',{
    content_type:currentType,item:item,
    export_format:expFormat,export_path:expPath,
    auto:true
  });
}
function doAutoSave(){
  // Всегда сохраняем — генерируем ID и заголовок при необходимости
  doImmediateSave();
}
function showSaveDialog(){
  document.getElementById('saveFormat').value=expFormat;
  document.getElementById('savePath').value=expPath;
  document.getElementById('saveDialog').classList.add('show');
}
function hideSaveDialog(){document.getElementById('saveDialog').classList.remove('show');}
var pendingDeleteId=null;
function showConfirmDialog(id){
  pendingDeleteId=id;
  document.getElementById('confirmDialog').style.display='block';
  setTimeout(function(){document.getElementById('confirmDialog').classList.add('show');},10);
}
function hideConfirmDialog(){
  document.getElementById('confirmDialog').classList.remove('show');
  setTimeout(function(){document.getElementById('confirmDialog').style.display='none';},200);
  pendingDeleteId=null;
}
function confirmDelete(){
  if(pendingDeleteId){
    window.pywry.emit('content:delete',{content_type:currentType,id:pendingDeleteId});
  }
  hideConfirmDialog();
}
function confirmSave(){
  var title=document.getElementById('postTitle').value.trim();
  if(!title){showToast('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a');return;}
  var fmt=document.getElementById('saveFormat').value;
  expFormat=fmt;
  var data=collectContent();
  var item={
    id:currentId||Date.now().toString(36)+Math.random().toString(36).substring(2,6),
    title:title,content:data.content,tags:data.tags,
    news_date:document.getElementById('newsDate').value||'',
    article_author:document.getElementById('articleAuthor').value||'',
    article_rubric:document.getElementById('articleRubric').value||''
  };
  hideSaveDialog();
  window.pywry.emit('content:save',{content_type:currentType,item:item,export_format:fmt,export_path:expPath});
}

function newItem(){
  saveHistory();
  // Сначала сохраняем предыдущий материал как черновик, если он не был сохранён
  var oldTitle=document.getElementById('postTitle').value.trim();
  if(currentId||oldTitle){doImmediateSave();}
  // Создаём новый
  currentId=Date.now().toString(36)+Math.random().toString(36).substring(2,6);
  var draftTitle=nextDraftTitle();
  document.getElementById('postTitle').value=draftTitle;
  document.getElementById('postTags').value='';
  updateMeta(null);
  blocks=[createBlock('paragraph')];selectedBlock=-1;
  renderBlocks();document.getElementById('postTitle').focus();
  document.getElementById('postTitle').setSelectionRange(0,draftTitle.length);
  // Сразу ставим автосохранение, чтобы черновик сохранился
  setAutoSaveStatus('unsaved');
  if(autoSaveTimer)clearTimeout(autoSaveTimer);
  autoSaveTimer=setTimeout(doAutoSave,2000);
}
function clearEditor(){
  saveHistory();
  currentId=null;
  document.getElementById('postTitle').value='';
  document.getElementById('postTags').value='';
  updateMeta(null);
  blocks=[createBlock('paragraph')];selectedBlock=-1;renderBlocks();
}

/* ─── Версии ─── */
function showVersions(){
  var modal=document.getElementById('versionsModal');
  if(!modal)return;
  var list=document.getElementById('versionsList');
  var versions=window._versionsCache||[];
  if(!versions.length){
    list.innerHTML='<div style="color:var(--text-secondary);text-align:center;padding:24px">Нет сохранённых версий</div>';
  }else{
    var h=[];
    for(var vi=versions.length-1;vi>=0;vi--){
      var v=versions[vi];
      var preview=(v.content||'').replace(/<[^>]+>/g,'').substring(0,120);
      h.push('<div style="padding:10px 12px;border:1px solid var(--border);border-radius:4px;margin-bottom:6px">');
      h.push('<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">');
      h.push('<div><strong>'+(v.title||'Без названия')+'</strong>');
      h.push('<div style="font-size:11px;color:var(--text-secondary);margin-top:2px">'+v.saved_at+'</div>');
      if(preview)h.push('<div style="font-size:12px;color:var(--text-secondary);margin-top:4px;line-height:1.3">'+preview.substring(0,120)+'</div>');
      h.push('</div>');
      h.push('<button onclick="restoreVersion('+vi+')" style="white-space:nowrap;padding:4px 10px;font-size:12px;cursor:pointer;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:#fff">\u21a9\ufe0f Восстановить</button>');
      h.push('</div></div>');
    }
    list.innerHTML=h.join('');
  }
  modal.style.display='flex';
}
function hideVersions(){
  document.getElementById('versionsModal').style.display='none';
}
function restoreVersion(vi){
  if(!currentId)return;
  window.pywry.emit('content:restore-version',{content_type:currentType,id:currentId,version_idx:vi});
  hideVersions();
}

/* ─── Предпросмотр ─── */
function showPreview(){
  var title=document.getElementById('postTitle').value.trim()||'Без названия';
  var now=new Date();
  var dateStr=now.toLocaleString('ru-RU',{day:'numeric',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'});
  var html=[];
  html.push('<h1>'+esc(title)+'</h1>');
  html.push('<div class="preview-meta">'+dateStr+'</div>');
  for(var i=0;i<blocks.length;i++){
    var b=blocks[i];
    if(b.type==='paragraph'&&b.content)html.push('<p>'+b.content+'</p>');
    else if(b.type==='heading'&&b.content)html.push('<'+b.level+'>'+b.content+'</'+b.level+'>');
    else if(b.type==='list'&&b.content)html.push(b.content);
    else if(b.type==='quote'&&b.content)html.push('<blockquote>'+b.content+'</blockquote>');
    else if(b.type==='separator')html.push('<hr>');
    else if(b.type==='code')html.push('<pre><code>'+b.content+'</code></pre>');
    else if(b.type==='image'&&b.imageData)html.push('<figure><img src="'+b.imageData+'" alt="'+(b.imageName||'')+'"></figure>');
    else if(b.type==='table'){
      // Определяем реальное количество столбцов
      var pCols=2;
      for(var tmp=0;tmp<b.rows.length;tmp++){if(b.rows[tmp].length>1){pCols=b.rows[tmp].length;break;}}
      html.push('<table>');
      for(var ri=0;ri<b.rows.length;ri++){
        html.push('<tr>');
        for(var ci=0;ci<b.rows[ri].length;ci++){
          var cell=b.rows[ri][ci];
          var pStyle=cell.bg?' style="background:'+cell.bg+'"':'';
          var pColspan=(b.rows[ri].length===1&&pCols>1)?' colspan="'+pCols+'"':'';
          var pWidth=(b.colWidths&&b.colWidths[ci])?' style="'+(pStyle?pStyle.slice(1):'')+'width:'+b.colWidths[ci]+'px"':pStyle;
          if(b.colWidths&&b.colWidths[ci]&&!cell.bg)pWidth=' width="'+b.colWidths[ci]+'"';
          html.push('<td'+pColspan+pWidth+'>'+(cell.content||'')+'</td>');
        }
        html.push('</tr>');
      }
      html.push('</table>');
    }
  }
  document.getElementById('previewBody').innerHTML=html.join('\n');
  document.getElementById('previewOverlay').classList.add('show');
}
function closePreview(){
  document.getElementById('previewOverlay').classList.remove('show');
}

/* ─── Медиа-менеджер ─── */
var mediaList=[];
function uploadToMediaLibrary(){
  var inp=document.getElementById('mediaUploadInput');
  if(!inp){
    inp=document.createElement('input');
    inp.type='file';inp.id='mediaUploadInput';inp.accept='image/*';inp.style.display='none';
    document.body.appendChild(inp);
  }
  inp.onchange=function(e){
    var file=e.target.files[0];if(!file)return;
    var reader=new FileReader();
    reader.onload=function(ev){
      window.pywry.emit('image:upload',{data:ev.target.result,filename:file.name});
      setTimeout(function(){window.pywry.emit('media:list',{});},500);
    };
    reader.readAsDataURL(file);
    inp.value='';
  };
  inp.click();
}
function openMediaManager(){
  document.getElementById('mediaOverlay').classList.add('show');
  document.getElementById('mediaGrid').innerHTML='<div class="media-empty">Загрузка...</div>';
  window.pywry.emit('media:list',{});
}
function closeMediaManager(){
  document.getElementById('mediaOverlay').classList.remove('show');
}
function insertMediaFromManager(name){
  var item=null;
  for(var i=0;i<mediaList.length;i++){if(mediaList[i].name===name){item=mediaList[i];break;}}
  if(!item||!item.data){showToast('Ошибка: данные изображения не найдены');return;}
  var nb=createBlock('image');
  nb.imageData=item.data;
  nb.imageName=item.name;
  blocks.push(nb);
  renderBlocks();
  var n=blocks.indexOf(nb);
  if(n>=0)setTimeout(function(){selectBlock(n);},50);
  closeMediaManager();
}
function deleteMediaFromManager(name){
  if(!confirm('Удалить "'+name+'" из медиатеки?'))return;
  window.pywry.emit('media:delete',{name:name});
}

window.pywry.on('ui:media-list',function(data){
  mediaList=data.files||[];
  var grid=document.getElementById('mediaGrid');
  if(!mediaList.length){
    grid.innerHTML='<div class="media-empty">📭 Нет загруженных изображений.<br>Загрузите изображение через 🖼 в редакторе.</div>';
    return;
  }
  var h=[];
  for(var i=0;i<mediaList.length;i++){
    var f=mediaList[i];
    var sizeStr=(f.size/1024).toFixed(1)+' KB';
    var dateStr=new Date(f.mtime*1000).toLocaleDateString('ru-RU');
    h.push('<div class="media-item" onclick="insertMediaFromManager(\''+escAttr(f.name)+'\')">');
    h.push('<img src="'+escAttr(f.data)+'" alt="'+escAttr(f.name)+'">');
    h.push('<div class="media-info">'+esc(f.name)+' &middot; '+sizeStr+'</div>');
    h.push('<button class="media-del" onclick="event.stopPropagation();deleteMediaFromManager(\''+escAttr(f.name)+'\')" title="Удалить">✕</button>');
    h.push('</div>');
  }
  grid.innerHTML=h.join('');
});

window.pywry.on('ui:media-deleted',function(data){
  // refresh the list
  window.pywry.emit('media:list',{});
});
function updateMeta(item){
  document.getElementById('docType').textContent=TYPE_LABELS[currentType];
  var tm=document.getElementById('titleMeta');
  if(!item){document.getElementById('docCreated').textContent='\u2014';document.getElementById('docUpdated').textContent='\u2014';tm.textContent='';return;}
  // Поля под тип — восстанавливаем
  document.getElementById('newsDate').value=item.news_date||'';
  document.getElementById('articleAuthor').value=item.article_author||'';
  document.getElementById('articleRubric').value=item.article_rubric||'';
  if(item.created_at)document.getElementById('docCreated').textContent=item.created_at;
  if(item.updated_at)document.getElementById('docUpdated').textContent=item.updated_at;
  var p=[];
  if(item.created_at)p.push('\u0421\u043e\u0437\u0434\u0430\u043d\u043e: '+item.created_at);
  if(item.updated_at)p.push('\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u043e: '+item.updated_at);
  tm.textContent=p.join(' \u00b7 ');
}

/* ─── Тема ─── */
function setTheme(name){
  document.body.className='theme-'+name;
  var sel=document.getElementById('themeSelect');
  if(sel)sel.value=name;
  // sync window background color with theme
  var bgColors={light:'240,240,241',dark:'30,30,46',modern:'13,17,23',sepia:'232,220,196'};
  var bg=bgColors[name]||'240,240,241';
  window.pywry.emit('window:bg',{rgb:bg});
  window.pywry.emit('settings:save',{theme:name});
}

/* ─── Панели ─── */
function toggleSide(){
  if(focusMode){
    focusMode=false;
    document.body.classList.remove('focus-mode');
    document.getElementById('btnFocus').title='Режим фокуса';
    // Закрываем обе панели
    document.getElementById('sidePanel').classList.add('hidden');
    document.getElementById('settingsBar').classList.add('hidden');
    sideVisible=false;settingsVisible=false;
    // Открываем только список
    document.getElementById('sidePanel').classList.remove('hidden');
    sideVisible=true;
    document.getElementById('toggleSideBtn').innerHTML='\u2630';
    document.getElementById('toggleSideBtn').title='Закрыть список материалов';
    return;
  }
  sideVisible=!sideVisible;
  var p=document.getElementById('sidePanel');
  if(!p)return;
  if(sideVisible){p.classList.remove('hidden');}else{p.classList.add('hidden');}
  var btn=document.getElementById('toggleSideBtn');
  if(btn){
    btn.innerHTML='\u2630';
    btn.title=sideVisible?'Закрыть список материалов':'Открыть список материалов';
  }
}
function toggleSettings(){
  if(focusMode){
    focusMode=false;
    document.body.classList.remove('focus-mode');
    document.getElementById('btnFocus').title='Режим фокуса';
    // Закрываем обе панели
    document.getElementById('sidePanel').classList.add('hidden');
    document.getElementById('settingsBar').classList.add('hidden');
    sideVisible=false;settingsVisible=false;
    // Открываем только настройки
    document.getElementById('settingsBar').classList.remove('hidden');
    settingsVisible=true;
    document.getElementById('toggleSettBtn').innerHTML='\u2699\ufe0f';
    document.getElementById('toggleSettBtn').title='Закрыть настройки (документ, AI, экспорт, тема)';
    return;
  }
  settingsVisible=!settingsVisible;
  var p=document.getElementById('settingsBar');
  if(!p)return;
  if(settingsVisible){p.classList.remove('hidden');}else{p.classList.add('hidden');}
  var btn=document.getElementById('toggleSettBtn');
  if(btn){
    btn.innerHTML='\u2699\ufe0f';
    btn.title=settingsVisible?'Закрыть настройки (документ, AI, экспорт, тема)':'Открыть настройки (документ, AI, экспорт, тема)';
  }
}
var focusMode=false;
function toggleFocus(){
  focusMode=!focusMode;
  document.body.classList.toggle('focus-mode',focusMode);
  var btn=document.getElementById('btnFocus');
  btn.title=focusMode?'Выйти из режима фокуса':'Режим фокуса';
}
function switchTab(tab){
  document.querySelectorAll('.sett-tabs button').forEach(function(b){b.classList.toggle('active',b.dataset.tab===tab);});
  document.getElementById('tabDocument').style.display=tab==='document'?'block':'none';
  document.getElementById('tabAI').style.display=tab==='ai'?'block':'none';
  document.getElementById('tabExport').style.display=tab==='export'?'block':'none';
  if(tab==='ai'&&!document.getElementById('aiUrl').value)loadAiSettings();
  if(tab==='ai'&&document.getElementById('aiUrl').value){
    // auto-load models if URL is set and list is empty
    var sel=document.getElementById('aiModel');
    if(sel.options.length<=1&&!document.getElementById('aiModelText').value){
      loadModels();
    }
  }
}
function updateBlockInfo(){
  if(selectedBlock<0||!blocks[selectedBlock]){document.getElementById('blockTypeText').textContent='\u041d\u0435\u0442 \u0432\u044b\u0434\u0435\u043b\u0435\u043d\u043d\u043e\u0433\u043e';document.getElementById('blockHeadingLevel').style.display='none';return;}
  var b=blocks[selectedBlock];
  var names={paragraph:'\u041f\u0430\u0440\u0430\u0433\u0440\u0430\u0444',heading:'\u0417\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a',list:'\u0421\u043f\u0438\u0441\u043e\u043a',quote:'\u0426\u0438\u0442\u0430\u0442\u0430',separator:'\u0420\u0430\u0437\u0434\u0435\u043b\u0438\u0442\u0435\u043b\u044c',code:'\u041a\u043e\u0434',image:'\u0418\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435'};
  document.getElementById('blockTypeText').textContent=names[b.type]||b.type;
  if(b.type==='heading'){document.getElementById('blockHeadingLevel').style.display='block';document.getElementById('blockHeadingLevel').querySelector('select').value=b.level||'h2';}
  else document.getElementById('blockHeadingLevel').style.display='none';
  setTimeout(positionToolbar,10);
}

/* ─── Настройки экспорта ─── */
function saveSettings(){
  var ep=document.getElementById('exportPath');
  if(!ep){showToast('Экспорт недоступен');return;}
  expPath=ep.value;
  window.pywry.emit('settings:save',{export_path:expPath,export_format:expFormat||'html'});
}
function pickExportPath(){
  var ep=document.getElementById('exportPath');
  if(!ep)return;
  var p=prompt('\u041f\u0443\u0442\u044c \u0434\u043b\u044f \u044d\u043a\u0441\u043f\u043e\u0440\u0442\u0430:',ep.value);
  if(p){ep.value=p;saveSettings();}
}
function openDataFolder(){
  showToast('\u0414\u0430\u043d\u043d\u044b\u0435: '+(document.getElementById('dataPathInfo').textContent||''));
}

/* ─── AI ─── */
var PROVIDERS={
  openai:{url:'https://api.openai.com/v1',models:'openai'},
  mistral:{url:'https://api.mistral.ai/v1',models:'openai'},
  ollama:{url:'http://localhost:11434',models:'ollama'},
  deepseek:{url:'https://api.deepseek.com/v1',models:'openai'},
  custom:{url:'',models:'openai'}
};
// Получить URL и ключ провайдера из кэша настроек (нижняя панель, независимо от правой)
function getProviderConfig(provider){
  var urls=window._providersData||{};
  var keys=window._providerKeys||{};
  var info=PROVIDERS[provider]||PROVIDERS.custom;
  return {
    url: (urls[provider]&&urls[provider].url)||info.url||'',
    key: keys[provider]||''
  };
}
function getAiModel(){
  var sel=document.getElementById('aiModel');
  var txt=document.getElementById('aiModelText');
  return sel.value||txt.value||'';
}
function setAiModel(val){
  var sel=document.getElementById('aiModel');
  var txt=document.getElementById('aiModelText');
  // try to find in select
  for(var i=0;i<sel.options.length;i++){
    if(sel.options[i].value===val){sel.value=val;txt.value=val;txt.style.display='none';sel.style.display='block';syncQuickModel();return;}
  }
  // not in list — show manual input
  sel.value='';txt.value=val;sel.style.display='none';txt.style.display='block';
  syncQuickModel();
}
function loadAiSettings(){
  // called when settings arrive
}
function onProviderChange(){
  // Сохраняем данные текущего провайдера перед переключением
  if(window._lastProvider){
    var oldModel=getAiModel();
    if(oldModel)savedModels[window._lastProvider]=oldModel;
    // Сохраняем в per-provider кэш
    window._providersData=window._providersData||{};
    if(!window._providersData[window._lastProvider])
      window._providersData[window._lastProvider]={};
    window._providersData[window._lastProvider].model=oldModel;
    window._providersData[window._lastProvider].url=document.getElementById('aiUrl').value;
    // Сохраняем ключ
    window._providerKeys=window._providerKeys||{};
    var oldKey=document.getElementById('aiKey').value;
    if(oldKey)window._providerKeys[window._lastProvider]=oldKey;
  }

  var prov=document.getElementById('aiProvider').value;
  window._lastProvider=prov;
  var info=PROVIDERS[prov]||PROVIDERS.custom;
  if(prov!=='custom'){
    document.getElementById('aiUrl').value=info.url;
  } else {
    // Для custom восстанавливаем сохранённый URL
    window._providersData=window._providersData||{};
    if(window._providersData.custom && window._providersData.custom.url)
      document.getElementById('aiUrl').value=window._providersData.custom.url;
  }
  // Восстанавливаем ключ для нового провайдера
  window._providerKeys=window._providerKeys||{};
  document.getElementById('aiKey').value=window._providerKeys[prov]||'';
  // clear model list
  var sel=document.getElementById('aiModel');
  sel.innerHTML='<option value="">— загрузите список моделей —</option>';
  sel.style.display='block';
  document.getElementById('aiModelText').style.display='none';
  document.getElementById('aiModelText').value='';
  document.getElementById('aiStatus').style.display='none';
  // Восстанавливаем модель для нового провайдера
  window._providersData=window._providersData||{};
  var savedModel=window._providersData[prov]&&window._providersData[prov].model;
  if(savedModel){setAiModel(savedModel);}
  // Автоматически загружаем модели для нового провайдера
  setTimeout(function(){loadModels();},100);
}
function saveAiSettings(){
  var model=getAiModel();
  var prov=document.getElementById('aiProvider').value;
  // Обновляем per-provider кэш для текущего провайдера
  window._providersData=window._providersData||{};
  if(!window._providersData[prov])window._providersData[prov]={};
  window._providersData[prov].model=model;
  window._providersData[prov].url=document.getElementById('aiUrl').value;
  // Сохраняем ключ в per-provider кэш
  window._providerKeys=window._providerKeys||{};
  window._providerKeys[prov]=document.getElementById('aiKey').value;
  window.pywry.emit('settings:save',{
    current_provider:prov,
    ai_url:document.getElementById('aiUrl').value,
    ai_key:document.getElementById('aiKey').value,
    ai_model:model,
    providers:window._providersData||{},
    provider_keys:window._providerKeys||{},
    export_format:expFormat,export_path:expPath
  });
  showToast('Настройки AI сохранены');
}
// Быстрое переключение провайдера из нижнего диалога (НЕ зависит от правой панели)
function quickProviderChange(){
  var prov=document.getElementById('aiProviderQuick').value;
  // Сбрасываем модель при смене провайдера
  var mSel=document.getElementById('aiModelQuick');
  mSel.innerHTML='<option value="">— загрузите модели —</option>';
  var st=document.querySelector('.ai-chat-bar .ai-result');
  if(st)st.style.display='none';
  // Загружаем модели для выбранного провайдера из кэшированных настроек
  var cfg=getProviderConfig(prov);
  if(!cfg.url){
    mSel.innerHTML='<option value="">— нет URL —</option>';
    return;
  }
  // Пытаемся загрузить модели
  window.pywry.emit('ai:list-models',{api_url:cfg.url,api_key:cfg.key,provider:prov,quick:true});
}
// Синхронизация выбора модели
function quickModelChange(){
  updateModelInfo();
}
// Показывает возможности модели
function updateModelInfo(){
  var sel=document.getElementById('aiModelQuick');
  var info=document.getElementById('modelCapsInfo');
  if(!info)return;
  var opt=sel&&sel.options[sel.selectedIndex];
  if(opt&&opt.value){
    var caps=opt.dataset.caps;
    if(caps){
      try{
        var arr=JSON.parse(caps);
        var icons={vision:'🖼️',code:'💻',text:'📝'};
        var labels=[];
        for(var i=0;i<arr.length;i++){
          if(icons[arr[i]])labels.push(icons[arr[i]]);
        }
        info.textContent=labels.length?labels.join(' '):'';
      }catch(e){info.textContent='';}
    }else{info.textContent='';}
  }else{info.textContent='';}
}
function syncQuickModel(){}
// Кастомный выпадающий список вверх, если не хватает места внизу
function fixSelectUpward(id){
  var sel=document.getElementById(id);
  if(!sel||sel.dataset._upwardFixed)return;
  sel.dataset._upwardFixed='1';
  sel.addEventListener('mousedown',function(e){
    var rect=sel.getBoundingClientRect();
    var spaceBelow=window.innerHeight-rect.bottom;
    var optCount=sel.options.length;
    var estHeight=Math.min(optCount,10)*28+8;
    if(spaceBelow>=estHeight)return;
    e.preventDefault();
    var popup=document.getElementById('_cus_'+id);
    if(popup)popup.remove();
    popup=document.createElement('div');
    popup.id='_cus_'+id;
    popup.style.cssText='position:fixed;z-index:99999;background:var(--bg-panel);border:1px solid var(--accent);border-radius:6px;max-height:280px;overflow-y:auto;box-shadow:0 4px 20px rgba(0,0,0,.4);font-size:12px';
    popup.style.left=rect.left+'px';
    popup.style.width=Math.max(rect.width,120)+'px';
    popup.style.bottom=(window.innerHeight-rect.top)+'px';
    var list=document.createElement('div');
    for(var i=0;i<sel.options.length;i++){
      (function(idx){
        var item=document.createElement('div');
        item.textContent=sel.options[idx].text||sel.options[idx].value;
        item.style.cssText='padding:6px 12px;cursor:pointer;color:var(--text-primary);white-space:nowrap;';
        if(idx===sel.selectedIndex)item.style.background='var(--accent)';
        item.onmouseenter=function(){this.style.background='var(--bg-hover)';};
        item.onmouseleave=function(){this.style.background=idx===sel.selectedIndex?'var(--accent)':'transparent';};
        item.onclick=function(){
          sel.value=sel.options[idx].value;
          sel.dispatchEvent(new Event('change',{bubbles:true}));
          popup.remove();
        };
        list.appendChild(item);
      })(i);
    }
    popup.appendChild(list);
    document.body.appendChild(popup);
    setTimeout(function(){
      function closeHandler(ev){
        if(!popup.contains(ev.target)&&ev.target!==sel){
          popup.remove();
          document.removeEventListener('click',closeHandler);
          document.removeEventListener('scroll',scrollHandler,true);
        }
      }
      function scrollHandler(){
        if(popup&&document.activeElement&&popup.contains(document.activeElement))return;
        if(popup)popup.remove();
      }
      document.addEventListener('click',closeHandler);
      document.addEventListener('scroll',scrollHandler,true);
    },10);
  });
}
fixSelectUpward('aiProviderQuick');
fixSelectUpward('aiModelQuick');
function loadModels(){
  var url=document.getElementById('aiUrl').value.trim();
  var key=document.getElementById('aiKey').value.trim();
  var prov=document.getElementById('aiProvider').value;
  if(!url){showToast('Сначала укажите URL API');return;}
  var st=document.getElementById('aiStatus');
  st.className='ai-status loading';st.textContent='🔄 Загрузка списка моделей...';st.style.display='block';
  document.getElementById('aiModel').innerHTML='<option value="">— загрузка... —</option>';
  window.pywry.emit('ai:list-models',{api_url:url,api_key:key,provider:prov});
}
function testConnection(){
  var url=document.getElementById('aiUrl').value.trim();
  var key=document.getElementById('aiKey').value.trim();
  var model=getAiModel()||'test';
  if(!url){showToast('Сначала укажите URL API');return;}
  var st=document.getElementById('aiStatus');
  st.className='ai-status loading';st.textContent='🔄 Проверка подключения...';st.style.display='block';
  // simple test — just try to list models (lightweight request)
  window.pywry.emit('ai:list-models',{api_url:url,api_key:key,provider:document.getElementById('aiProvider').value});
}
function getSelectedText(){
  // Сначала проверяем, есть ли выделение текста в contenteditable
  var sel=window.getSelection();
  if(sel&&sel.rangeCount>0&&!sel.isCollapsed){
    var range=sel.getRangeAt(0);
    var ce=range.commonAncestorContainer;
    // Проверяем, что выделение внутри редактора (contenteditable)
    if(ce&&ce.ownerDocument&&ce.ownerDocument.getElementById('blockEditor')){
      // Проверяем через closest, что мы внутри contenteditable
      var el=ce.nodeType===3?ce.parentNode:ce;
      if(el&&el.closest&&el.closest('[contenteditable]')){
        return sel.toString().trim();
      }
    }
  }
  // Если выделения нет — берём содержимое блока
  if(selectedBlock>=0&&blocks[selectedBlock]){
    var b=blocks[selectedBlock];
    if(b.type==='paragraph'||b.type==='heading'||b.type==='quote'||b.type==='code'){
      // get plain text from HTML content
      var d=document.createElement('div');d.innerHTML=b.content;
      return d.textContent||d.innerText||'';
    }
    if(b.type==='list'){
      var d=document.createElement('div');d.innerHTML=b.content;
      return d.textContent||d.innerText||'';
    }
  }
  return '';
}
function loadTextFromBlock(){
  var text=getSelectedText();
  if(!text){showToast('\u041d\u0435\u0442 \u0432\u044b\u0434\u0435\u043b\u0435\u043d\u043d\u043e\u0433\u043e \u0431\u043b\u043e\u043a\u0430 \u0441 \u0442\u0435\u043a\u0441\u0442\u043e\u043c');return;}
  document.getElementById('aiChatInput').value=text;
}
function toggleAiSettings(){
  var body=document.getElementById('aiSettingsBody');
  var btn=document.getElementById('aiCollapseBtn');
  if(!body||!btn)return;
  if(body.style.display==='none'){
    body.style.display='block';
    btn.textContent='\u25b2';
    btn.classList.remove('collapsed');
    btn.title='\u0421\u0432\u0435\u0440\u043d\u0443\u0442\u044c';
    window.pywry.emit('settings:save',{ai_settings_collapsed:false});
  }else{
    body.style.display='none';
    btn.textContent='\u25bc';
    btn.classList.add('collapsed');
    btn.title='\u0420\u0430\u0437\u0432\u0435\u0440\u043d\u0443\u0442\u044c';
    window.pywry.emit('settings:save',{ai_settings_collapsed:true});
  }
}
function toggleKeyVisibility(){
  var key=document.getElementById('aiKey');
  var btn=document.getElementById('keyEyeBtn');
  if(!key||!btn)return;
  if(key.type==='password'){
    key.type='text';
    btn.textContent='\ud83d\ude48';
    btn.title='\u0421\u043a\u0440\u044b\u0442\u044c \u043a\u043b\u044e\u0447';
  }else{
    key.type='password';
    btn.textContent='\ud83d\udc41\ufe0f';
    btn.title='\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u043a\u043b\u044e\u0447';
  }
}
function aiAction(action){
  var prompt=document.getElementById('aiChatInput');
  var text=prompt.value.trim();
  // only fill from selected block if prompt is empty
  if(!text){
    var selected=getSelectedText();
    if(selected){prompt.value=selected;text=selected.trim();}
  }
  if(!text){showToast('\u0412\u044b\u0434\u0435\u043b\u0438\u0442\u0435 \u0431\u043b\u043e\u043a \u0438\u043b\u0438 \u0432\u0432\u0435\u0434\u0438\u0442\u0435 \u0442\u0435\u043a\u0441\u0442');return;}

  var systems={rewrite:'\u041f\u0435\u0440\u0435\u043f\u0438\u0448\u0438 \u0442\u0435\u043a\u0441\u0442, \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0432 \u0441\u043c\u044b\u0441\u043b, \u043d\u043e \u0443\u043b\u0443\u0447\u0448\u0438\u0432 \u0441\u0442\u0438\u043b\u044c \u0438 \u0433\u0440\u0430\u043c\u043c\u0430\u0442\u0438\u043a\u0443. \u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0435\u0440\u0435\u043f\u0438\u0441\u0430\u043d\u043d\u044b\u043c \u0442\u0435\u043a\u0441\u0442\u043e\u043c, \u043a\u0430\u0436\u0434\u044b\u0439 \u0430\u0431\u0437\u0430\u0446 \u0441 \u043d\u043e\u0432\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438, \u043c\u0435\u0436\u0434\u0443 \u0430\u0431\u0437\u0430\u0446\u0430\u043c\u0438 \u043f\u0443\u0441\u0442\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430.',
    proceed:'\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438 \u0442\u0435\u043a\u0441\u0442 \u0435\u0441\u0442\u0435\u0441\u0442\u0432\u0435\u043d\u043d\u043e, \u0441\u043e\u0445\u0440\u0430\u043d\u044f\u044f \u0441\u0442\u0438\u043b\u044c. \u041a\u0430\u0436\u0434\u044b\u0439 \u043d\u043e\u0432\u044b\u0439 \u043f\u0443\u043d\u043a\u0442 \u0438\u043b\u0438 \u0430\u0431\u0437\u0430\u0446 \u0441 \u043d\u043e\u0432\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438, \u043c\u0435\u0436\u0434\u0443 \u043d\u0438\u043c\u0438 \u043f\u0443\u0441\u0442\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430. \u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435\u043c \u0431\u0435\u0437 \u043f\u043e\u044f\u0441\u043d\u0435\u043d\u0438\u0439.',
    fix:'\u0418\u0441\u043f\u0440\u0430\u0432\u044c \u043e\u0440\u0444\u043e\u0433\u0440\u0430\u0444\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u0438 \u0433\u0440\u0430\u043c\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u043e\u0448\u0438\u0431\u043a\u0438 \u0432 \u0442\u0435\u043a\u0441\u0442\u0435. \u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044b\u043c \u0442\u0435\u043a\u0441\u0442\u043e\u043c, \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0432 \u0440\u0430\u0437\u0431\u0438\u0435\u043d\u0438\u0435 \u043d\u0430 \u0430\u0431\u0437\u0430\u0446\u044b.',
    shorten:'\u0421\u043e\u043a\u0440\u0430\u0442\u0438 \u0442\u0435\u043a\u0441\u0442, \u043e\u0441\u0442\u0430\u0432\u0438\u0432 \u0441\u0430\u043c\u043e\u0435 \u0433\u043b\u0430\u0432\u043d\u043e\u0435. \u041e\u0442\u0432\u0435\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u0441\u043e\u043a\u0440\u0430\u0449\u0451\u043d\u043d\u044b\u043c \u0442\u0435\u043a\u0441\u0442\u043e\u043c.',
    translate:'\u041f\u0435\u0440\u0435\u0432\u0435\u0434\u0438 \u0442\u0435\u043a\u0441\u0442 \u0441 \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u043e\u0433\u043e \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u0438\u0439. \u041e\u0442\u0432\u0435\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0435\u0440\u0435\u0432\u043e\u0434\u043e\u043c.',
    explain:'\u041e\u0442\u0432\u0435\u0442\u044c \u043d\u0430 \u0432\u043e\u043f\u0440\u043e\u0441 \u0438\u043b\u0438 \u043f\u043e\u0434\u0440\u043e\u0431\u043d\u043e \u043e\u0431\u044a\u044f\u0441\u043d\u0438 \u0442\u0435\u043c\u0443 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430. \u0421\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0438\u0440\u0443\u0439 \u043e\u0442\u0432\u0435\u0442: \u043a\u0430\u0436\u0434\u044b\u0439 \u0430\u0431\u0437\u0430\u0446 \u0441 \u043d\u043e\u0432\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438, \u043c\u0435\u0436\u0434\u0443 \u0430\u0431\u0437\u0430\u0446\u0430\u043c\u0438 \u043f\u0443\u0441\u0442\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430, \u043f\u0440\u0438 \u043d\u0435\u043e\u0431\u0445\u043e\u0434\u0438\u043c\u043e\u0441\u0442\u0438 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0438 \u0438\u043b\u0438 \u0441\u043f\u0438\u0441\u043a\u0438.'};
  var model=getAiModel();
  if(!model){showToast('\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 \u0438 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043c\u043e\u0434\u0435\u043b\u044c \u0432 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u0445 AI');return;}

  // Для перевода — спрашиваем язык (через модальное окно)
  if(action==='translate'){
    askTranslateLang(function(targetLang){
      systems.translate='\u041e\u043f\u0440\u0435\u0434\u0435\u043b\u0438 \u044f\u0437\u044b\u043a \u0438\u0441\u0445\u043e\u0434\u043d\u043e\u0433\u043e \u0442\u0435\u043a\u0441\u0442\u0430 \u0438 \u043f\u0435\u0440\u0435\u0432\u0435\u0434\u0438 \u0435\u0433\u043e \u043d\u0430 '+targetLang.trim()+'. \u041e\u0442\u0432\u0435\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0435\u0440\u0435\u0432\u043e\u0434\u043e\u043c, \u0431\u0435\u0437 \u043f\u043e\u044f\u0441\u043d\u0435\u043d\u0438\u0439.';
      sendAiQuery(text,systems[action],model);
    });
    return;
  }

  sendAiQuery(text,systems[action],model);
}
function sendAiQuery(text,system,model){
  // Подсказки по типу материала
  var typeHints={
    'news':'📰 Это новость. Пиши кратко, 1\u20133 абзаца, обязательно укажи дату события. ',
    'article':'📄 Это статья. Пиши развёрнуто, с заголовками разделов, укажи автора. ',
    'post':''
  };
  system=(typeHints[currentType]||'')+system;
  var el=document.getElementById('aiResult');el.style.display='none';
  var btn=document.getElementById('aiApplyBtn');btn.style.display='none';
  showToast('\u0417\u0430\u043f\u0440\u043e\u0441 \u043a AI...');
  window.pywry.emit('ai:query',{
    prompt:text,system:system,
    api_url:document.getElementById('aiUrl').value,
    api_key:document.getElementById('aiKey').value,
    model:model,
    provider:document.getElementById('aiProvider').value
  });
}
var pendingTranslateLang=null;
function askTranslateLang(callback){
  pendingTranslateLang=callback;
  var modal=document.getElementById('translateLangModal');
  if(!modal){showToast('\u041e\u0448\u0438\u0431\u043a\u0430 \u0438\u043d\u0438\u0446\u0438\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438 \u043e\u043a\u043d\u0430 \u043f\u0435\u0440\u0435\u0432\u043e\u0434\u0430');return;}
  modal.style.display='flex';
  var input=document.getElementById('translateLangInput');
  if(input){input.value='\u0440\u0443\u0441\u0441\u043a\u0438\u0439';setTimeout(function(){input.focus();input.select();},100);}
}
function submitTranslateLang(){
  var input=document.getElementById('translateLangInput');
  if(!input)return;
  var lang=input.value.trim();
  if(!lang){showToast('\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u044f\u0437\u044b\u043a');return;}
  document.getElementById('translateLangModal').style.display='none';
  if(pendingTranslateLang)pendingTranslateLang(lang);
  pendingTranslateLang=null;
}
function setLang(lang){
  var input=document.getElementById('translateLangInput');
  if(input){input.value=lang;input.focus();}
}
function closeTranslateLang(){
  document.getElementById('translateLangModal').style.display='none';
  pendingTranslateLang=null;
  pendingAiBlockIdx=-1;pendingAiSelInfo=null;
  removeAiInlineResult();
  showToast('\u041f\u0435\u0440\u0435\u0432\u043e\u0434 \u043e\u0442\u043c\u0435\u043d\u0451\u043d');
}
function applyAiResult(){
  if(!aiResultText)return;
  saveHistory();
  if(selectedBlock>=0&&selectedBlock<blocks.length){
    var newBlocks=mdToBlocks(aiResultText);
    var args=[selectedBlock,1].concat(newBlocks);
    blocks.splice.apply(blocks,args);
    renderBlocks();
    setTimeout(function(){selectBlock(selectedBlock<blocks.length?selectedBlock:blocks.length-1);},50);
  } else {
    // вставляем как новые блоки в конец
    var newBlocks=mdToBlocks(aiResultText);
    for(var ii=0;ii<newBlocks.length;ii++)blocks.push(newBlocks[ii]);
    renderBlocks();
  }
  showToast('\u0422\u0435\u043a\u0441\u0442 \u0432\u0441\u0442\u0430\u0432\u043b\u0435\u043d');
  document.getElementById('aiApplyBtn').style.display='none';
}

/* ─── События ─── */
var cachedItems=[];

window.pywry.on('ui:render-list',function(data){
  cachedItems=data.items||[];
  renderSideList();
});

function sortList(){
  renderSideList();
}

var searchContentMode=false;
function toggleSearchContent(){
  searchContentMode=!searchContentMode;
  var btn=document.getElementById('searchContentToggle');
  var input=document.getElementById('searchInput');
  if(searchContentMode){
    btn.style.borderColor='var(--accent)';btn.style.color='var(--accent)';
    btn.title='Отключить поиск по содержимому';
    input.placeholder='🔍 Поиск по содержимому...';
  } else {
    btn.style.borderColor='var(--border)';btn.style.color='var(--text-secondary)';
    btn.title='Искать по содержимому';
    input.placeholder='🔍 Поиск по названию...';
  }
  filterList();
}

function filterList(){
  renderSideList();
}

function renderSideList(){
  var items=cachedItems.slice();
  // Apply search filter
  var q=(document.getElementById('searchInput').value||'').trim().toLowerCase();
  if(q){
    items=items.filter(function(it){
      var t=(it.title||'').toLowerCase();
      var tags=(it.tags||[]).join(' ').toLowerCase();
      if(t.indexOf(q)>=0||tags.indexOf(q)>=0)return true;
      if(searchContentMode){
        var c=(it.content||'').toLowerCase().replace(/<[^>]*>/g,'');
        if(c.indexOf(q)>=0)return true;
      }
      return false;
    });
  }
  var sort=document.getElementById('sortSelect').value;
  if(sort==='date_desc')items.sort(function(a,b){return(b.updated_at||'').localeCompare(a.updated_at||'');});
  else if(sort==='date_asc')items.sort(function(a,b){return(a.updated_at||'').localeCompare(b.updated_at||'');});
  else if(sort==='alpha_asc')items.sort(function(a,b){return(a.title||'').localeCompare(b.title||'');});
  else if(sort==='alpha_desc')items.sort(function(a,b){return(b.title||'').localeCompare(a.title||'');});

  var container=document.getElementById('sideList');
  if(items.length===0){container.innerHTML='<div class="side-empty">\u041d\u0435\u0442 \u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b\u043e\u0432<br>\u0421\u043e\u0437\u0434\u0430\u0439\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 \u2192</div>';return;}
  var h=[];
  for(var i=0;i<items.length;i++){
    var it=items[i];
    var tags=it.tags||[];
    var tagsHtml='';
    if(tags.length>0){
      tagsHtml=' <span style="font-size:10px;color:#2271b1">'+tags.slice(0,3).map(function(t){return esc(t);}).join(', ')+'</span>';
    }
    h.push('<div class="s-item'+(it.id===currentId?' active':'')+'" data-id="'+it.id+'"><div class="s-title">'+esc(it.title||'\u0411\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f')+'</div><div class="s-meta">'+(it.updated_at?esc(it.updated_at):'')+tagsHtml+'</div><button class="s-del" data-id="'+it.id+'" title="Удалить материал">\ud83d\uddd1\ufe0f</button></div>');
  }
  container.innerHTML=h.join('');
  container.onclick=function(e){
    var el=e.target.closest('.s-item');if(!el)return;
    var id=el.dataset.id;
    if(e.target.closest('.s-del')){
      e.stopPropagation();e.preventDefault();
      showConfirmDialog(id);
      return;
    }
    window.pywry.emit('content:get',{content_type:currentType,id:id});
  };
}

window.pywry.on('ui:open-editor',function(data){
  var item=data.item;currentId=item.id;
  document.getElementById('postTitle').value=item.title||'';
  document.getElementById('postTags').value=(item.tags||[]).join(', ');
  updateMeta(item);
  blocks=parseContent(item.content);selectedBlock=-1;renderBlocks();
  undoStack=[];saveHistory(); // сохраняем начальное состояние для Undo
  // Загружаем количество версий
  window.pywry.emit('content:get-versions',{content_type:currentType,id:currentId},'__versions__');
});

window.pywry.on('ui:settings',function(data){
  expFormat=data.export_format||'html';expPath=data.export_path||'';
  document.getElementById('exportPath').value=expPath;
  document.getElementById('dataPathInfo').textContent=data.data_path||'';
  // Сохраняем per-provider данные
  window._providersData=data.providers||{};
  window._providerKeys=data.provider_keys||{};
  // Восстанавливаем провайдера
  var prov=data.current_provider||'mistral';
  document.getElementById('aiProvider').value=prov;
  window._lastProvider=prov;
  // Восстанавливаем URL, ключ и модель для текущего провайдера
  var url=data.ai_url||'';
  var key=data.ai_key||window._providerKeys[prov]||'';
  var model=data.ai_model||'';
  savedAiModel=model;
  document.getElementById('aiUrl').value=url;
  document.getElementById('aiKey').value=key;
  setAiModel(model);
  // restore AI settings collapsed state
  var collapsed=data.ai_settings_collapsed;
  if(collapsed===true||collapsed===undefined){
    var body=document.getElementById('aiSettingsBody');
    var btn=document.getElementById('aiCollapseBtn');
    if(body)body.style.display='none';
    if(btn){btn.textContent='▼';btn.classList.add('collapsed');btn.title='Развернуть';}
  }else{
    var body=document.getElementById('aiSettingsBody');
    var btn=document.getElementById('aiCollapseBtn');
    if(body)body.style.display='block';
    if(btn){btn.textContent='▲';btn.classList.remove('collapsed');btn.title='Свернуть';}
  }
  // restore theme
  var theme=data.theme||'light';
  document.body.className='theme-'+theme;
  var ts=document.getElementById('themeSelect');
  if(ts)ts.value=theme;
  // sync window bg
  var bgColors={light:'240,240,241',dark:'30,30,46',modern:'13,17,23',sepia:'232,220,196'};
  window.pywry.emit('window:bg',{rgb:bgColors[theme]||'240,240,241'});
  // Auto-load models for current provider in bottom panel
  document.getElementById('aiProviderQuick').value=prov;
  setTimeout(function(){quickProviderChange();}, 150);
});

window.pywry.on('ui:versions',function(data){
  var versions=data.versions||[];
  var btn=document.getElementById('btnVersions');
  if(btn)btn.textContent='\uD83D\uDCCB История версий ('+versions.length+')';
  window._versionsCache=versions;
});

window.pywry.on('ui:ai-models',function(data){
  var st=document.getElementById('aiStatus');
  if(data.error){
    st.className='ai-status err';st.textContent='\u274c '+data.error;st.style.display='block';
    showToast('\u041e\u0448\u0438\u0431\u043a\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438 \u043c\u043e\u0434\u0435\u043b\u0435\u0439');
    return;
  }
  var models=data.models||[];
  if(models.length===0){
    st.className='ai-status err';st.textContent='\u26a0\ufe0f \u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0445 \u043c\u043e\u0434\u0435\u043b\u0435\u0439';st.style.display='block';
    return;
  }
  function populateSelect(selId){
    var sel=document.getElementById(selId);
    if(!sel)return null;
    var prev=sel.value;
    sel.innerHTML='<option value="">\u2014 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043c\u043e\u0434\u0435\u043b\u044c \u2014</option>';
    for(var i=0;i<models.length;i++){
      var opt=document.createElement('option');
      opt.value=models[i].id;opt.textContent=models[i].name;
      if(models[i].capabilities)opt.dataset.caps=JSON.stringify(models[i].capabilities);
      sel.appendChild(opt);
    }
    if(prev&&prev!==''){try{sel.value=prev;}catch(e){}}
    return sel;
  }
  if(data.quick){
    // Загрузка из нижней панели — только быстрый селектор, не трогаем правую панель
    var qSel=populateSelect('aiModelQuick');
    // Восстанавливаем модель, сохранённую в настройках для этого провайдера
    var qProv=document.getElementById('aiProviderQuick').value;
    var urls=window._providersData||{};
    var savedModel=(urls[qProv]&&urls[qProv].model)||'';
    if(savedModel && qSel){
      var found=false;
      for(var j=0;j<qSel.options.length;j++){if(qSel.options[j].value===savedModel){qSel.value=savedModel;found=true;break;}}
    }
    updateModelInfo();
    st.className='ai-status ok';st.textContent='\u2705 \u0417\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043e '+models.length+' \u043c\u043e\u0434\u0435\u043b\u0435\u0439';st.style.display='block';
  }else{
    // Загрузка из правой панели — оба селектора
    var sel=populateSelect('aiModel');
    populateSelect('aiModelQuick');
    sel.style.display='block';
    document.getElementById('aiModelText').style.display='none';
    var prov=document.getElementById('aiProvider').value;
    var saved=savedModels[prov]||savedAiModel;
    if(saved){
      var found=false;
      for(var j=0;j<sel.options.length;j++){if(sel.options[j].value===saved){sel.value=saved;found=true;break;}}
      if(!found)sel.value='';
    }
    // Синхронизируем модель и в нижнем селекторе
    var qProv=document.getElementById('aiProviderQuick').value;
    var urls=window._providersData||{};
    var savedQ=(urls[qProv]&&urls[qProv].model)||saved||'';
    if(savedQ){
      var qSel=document.getElementById('aiModelQuick');
      if(qSel){
        var qFound=false;
        for(var j=0;j<qSel.options.length;j++){if(qSel.options[j].value===savedQ){qSel.value=savedQ;qFound=true;break;}}
      }
    }
    st.className='ai-status ok';st.textContent='\u2705 \u0417\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043e '+models.length+' \u043c\u043e\u0434\u0435\u043b\u0435\u0439';st.style.display='block';
  }
  showToast('\u0417\u0430\u0433\u0440\u0443\u0436\u0435\u043d\u043e '+models.length+' \u043c\u043e\u0434\u0435\u043b\u0435\u0439');
});

window.pywry.on('ui:toast',function(data){showToast(data.message);});
window.pywry.on('ui:print-pdf',function(data){
  // Создаём iframe для печати, чтобы не трогать редактор
  var iframe=document.createElement('iframe');
  iframe.style.cssText='position:fixed;top:-9999px;left:-9999px;width:800px;height:600px;border:none';
  document.body.appendChild(iframe);
  var doc=iframe.contentDocument||iframe.contentWindow.document;
  doc.open();
  doc.write(data.html);
  doc.close();
  // Ждём загрузки и печатаем
  setTimeout(function(){
    iframe.contentWindow.print();
    // Удаляем iframe после печати
    setTimeout(function(){document.body.removeChild(iframe);},500);
  },500);
});

window.pywry.on('ui:auto-saved',function(){
  setAutoSaveStatus('saved');
});

window.pywry.on('ui:ai-result',function(data){
  var el=document.getElementById('aiResult');
  var tEl=document.getElementById('aiResultText');
  var aBtn=document.getElementById('aiApplyBtn');
  if(data.error){
    el.className='ai-result error';
    if(tEl){tEl.textContent=data.error}else{el.textContent=data.error}
    el.style.display='block';
    if(aBtn)aBtn.style.display='none';
    showToast('\u041e\u0448\u0438\u0431\u043a\u0430 AI');
    if(pendingAiBlockIdx>=0)removeAiInlineResult();
    pendingAiBlockIdx=-1;
  } else {
    aiResultText=data.text;
    el.className='ai-result';
    if(tEl){tEl.textContent=data.text}else{el.textContent=data.text}
    el.style.display='block';
    if(aBtn)aBtn.style.display='inline-block';
    // Показываем результат под блоком, если запрос был из тулбара
    if(pendingAiBlockIdx>=0)showAiResultInline(data.text);
  }
});

window.pywry.on('ui:image-saved',function(data){
  showToast('\u0418\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e: '+data.name);
  // refresh media list if overlay is open
  if(document.getElementById('mediaOverlay').classList.contains('show')){
    setTimeout(function(){window.pywry.emit('media:list',{});},300);
  }
});

function showToast(msg){
  var w=document.getElementById('toastWrap'),t=document.createElement('div');
  t.className='toast';t.textContent=msg;w.appendChild(t);
  setTimeout(function(){if(t.parentNode)t.parentNode.removeChild(t);},2500);
}
function esc(str){var d=document.createElement('div');d.textContent=str;return d.innerHTML;}
function escAttr(str){return esc(str).replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/&/g,'&amp;');}

// Ctrl+S, Escape, formatting hotkeys
document.addEventListener('keydown',function(e){
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();showSaveDialog();}
  if(e.key==='Escape'){closePreview();}
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&e.key==='z'){e.preventDefault();undo();}
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key==='z'){e.preventDefault();redo();}
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&e.key==='y'){e.preventDefault();redo();}
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey){
    if(e.key==='b'||e.key==='B'){e.preventDefault();fmtBlock('bold');}
    else if(e.key==='i'||e.key==='I'){e.preventDefault();fmtBlock('italic');}
    else if(e.key==='u'||e.key==='U'){e.preventDefault();fmtBlock('underline');}
    else if(e.key==='k'||e.key==='K'){e.preventDefault();insertLinkBlock();}
  }
});

/* ─── Resize panels ─── */
(function(){
  function makeResize(panelId,handleId,side){
    var panel=document.getElementById(panelId),handle=document.getElementById(handleId);
    if(!panel||!handle)return;
    var startX,startW;
    function onStart(e){
      e.preventDefault();
      panel.classList.add('no-transition');
      startX=e.clientX||e.touches[0].clientX;
      startW=panel.offsetWidth;
      handle.classList.add('active');
      document.addEventListener('mousemove',onMove);
      document.addEventListener('mouseup',onEnd);
      document.addEventListener('touchmove',onMove,{passive:true});
      document.addEventListener('touchend',onEnd);
    }
    function onMove(e){
      var x=e.clientX||e.touches[0].clientX;
      var maxW=Math.min(280,window.innerWidth*0.25);
      var w;
      if(side==='left')w=Math.max(120,Math.min(maxW,startW+x-startX));
      else w=Math.max(120,Math.min(maxW,startW+startX-x));
      panel.style.width=w+'px';
      panel.style.minWidth=w+'px';
    }
    function onEnd(){
      handle.classList.remove('active');
      panel.classList.remove('no-transition');
      document.removeEventListener('mousemove',onMove);
      document.removeEventListener('mouseup',onEnd);
      document.removeEventListener('touchmove',onMove);
      document.removeEventListener('touchend',onEnd);
    }
    handle.addEventListener('mousedown',onStart);
    handle.addEventListener('touchstart',onStart,{passive:true});
  }
  makeResize('sidePanel','sideResizeHandle','right');
  makeResize('settingsBar','settingsResizeHandle','left');
  // Window resize grip for frameless window
  (function(){
    var grip=document.getElementById('windowResizeGrip');
    if(!grip)return;
    var winW=window.innerWidth,winH=window.innerHeight;
    function onStart(e){
      e.preventDefault();
      var startX=e.clientX||e.touches[0].clientX;
      var startY=e.clientY||e.touches[0].clientY;
      var startW=winW,startH=winH;
      function onMove(ev){
        var cx=ev.clientX||ev.touches[0].clientX;
        var cy=ev.clientY||ev.touches[0].clientY;
        var newW=Math.max(800,startW+(cx-startX));
        var newH=Math.max(400,startH+(cy-startY));
        grip.style.opacity='0.6';
        // send resize to Python at drag end (throttle would be better but simple is OK)
        window._resizeData={w:Math.round(newW),h:Math.round(newH)};
      }
      function onEnd(){
        grip.style.opacity='1';
        if(window._resizeData){
          window.pywry.emit('window:action',{action:'resize',width:window._resizeData.w,height:window._resizeData.h});
          winW=window._resizeData.w;winH=window._resizeData.h;
          delete window._resizeData;
        }
        document.removeEventListener('mousemove',onMove);
        document.removeEventListener('mouseup',onEnd);
        document.removeEventListener('touchmove',onMove);
        document.removeEventListener('touchend',onEnd);
      }
      document.addEventListener('mousemove',onMove);
      document.addEventListener('mouseup',onEnd);
      document.addEventListener('touchmove',onMove,{passive:true});
      document.addEventListener('touchend',onEnd);
    }
    grip.addEventListener('mousedown',onStart);
    grip.addEventListener('touchstart',onStart,{passive:true});
  })();
})();
</script>
<div class="resize-grip" id="windowResizeGrip"></div>
<!-- Модал выбора языка перевода -->
<div id="translateLangModal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;z-index:99999;background:rgba(0,0,0,0.4);align-items:center;justify-content:center" onclick="if(event.target===this)closeTranslateLang()">
  <div style="background:var(--bg-panel);padding:20px;border-radius:8px;min-width:340px;max-width:440px;box-shadow:0 4px 20px rgba(0,0,0,0.3)">
    <h3 style="margin:0 0 12px;font-size:15px;color:var(--text-primary)">Перевод</h3>
    <p style="margin:0 0 8px;font-size:13px;color:var(--text-secondary)">На какой язык перевести?</p>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px">
      <div class="lang-chip" onclick="setLang('Русский')">Русский</div>
      <div class="lang-chip" onclick="setLang('English')">English</div>
      <div class="lang-chip" onclick="setLang('中文')">中文</div>
      <div class="lang-chip" onclick="setLang('Français')">Français</div>
      <div class="lang-chip" onclick="setLang('Deutsch')">Deutsch</div>
      <div class="lang-chip" onclick="setLang('Español')">Español</div>
      <div class="lang-chip" onclick="setLang('Italiano')">Italiano</div>
      <div class="lang-chip" onclick="setLang('Português')">Português</div>
      <div class="lang-chip" onclick="setLang('日本語')">日本語</div>
      <div class="lang-chip" onclick="setLang('한국어')">한국어</div>
      <div class="lang-chip" onclick="setLang('العربية')">العربية</div>
      <div class="lang-chip" onclick="setLang('Türkçe')">Türkçe</div>
    </div>
    <input type="text" id="translateLangInput" value="русский" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:4px;font-size:14px;box-sizing:border-box;background:var(--bg-input);color:var(--text-primary);outline:none" onkeydown="if(event.key==='Enter')submitTranslateLang()">
    <p style="margin:4px 0 0;font-size:11px;color:var(--text-secondary)">Выберите язык выше или введите свой</p>
    <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
      <button onclick="closeTranslateLang()" style="padding:6px 16px;border:1px solid var(--border);border-radius:4px;background:var(--bg-body);color:var(--text-primary);cursor:pointer;font-size:13px">Отмена</button>
      <button onclick="submitTranslateLang()" style="padding:6px 16px;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:#fff;cursor:pointer;font-size:13px">Перевести</button>
    </div>
  </div>
</div>
</body>
</html>"""

# ─── Запуск ────────────────────────────────────────────────
win = app.show(
    UI, title="📝 PW Editor — блочный редактор контента",
    width=1100, height=700,
    callbacks={
        'content:switch':  cb_switch,
        'content:save':    cb_save,
        'content:delete':  cb_delete,
        'content:get':     cb_get,
        'content:get-versions':  cb_get_versions,
        'content:restore-version':  cb_restore_version,
        'settings:get':    cb_get_settings,
        'settings:save':   cb_save_settings,
        'ai:query':        cb_ai_query,
        'ai:list-models':  cb_ai_list_models,
        'image:upload':    cb_image_upload,
        'media:list':      cb_list_media,
        'media:base64':    cb_get_media_base64,
        'media:delete':    cb_delete_media,
        'window:action':   cb_window_action,
        'window:bg':       cb_window_bg,
    },
)
win.set_decorations(False)
win.set_background_color(240, 240, 241)
win.center()
app.block()
