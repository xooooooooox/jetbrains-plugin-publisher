#!/usr/bin/env python3
import base64
import json
import os
import shutil
import subprocess
import tempfile
from flask import Flask, request, jsonify, send_from_directory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='web', static_url_path='')


@app.route('/', methods=['GET'])
def index():
    return send_from_directory('web', 'index.html')


@app.route('/status', methods=['GET', 'OPTIONS'])
def status():
    return _cors((jsonify({'ok': True}), 200))


def _bool(v: str):
    if v is None: return False
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _auth_header(env):
    tok = env.get('ARTIFACTORY_TOKEN') or env.get('PUBLISHER_TOKEN')
    if tok:
        return {'Authorization': f'Bearer {tok}'}
    basic = env.get('PUBLISHER_BASIC')
    if basic:
        enc = base64.b64encode(basic.encode('utf-8')).decode('ascii')
        return {'Authorization': f'Basic {enc}'}
    return {}


def _artifact_exists(base_url: str, folder: str, filename: str, env) -> (bool, str):
    base = (base_url or '').rstrip('/')
    folder = (folder or 'unknown').strip('/')
    url = f"{base}/{folder}/{filename}"
    headers = _auth_header(env)

    # HEAD
    try:
        req = Request(url, method='HEAD', headers=headers)
        with urlopen(req, timeout=12) as r:
            if 200 <= r.status < 300:
                return True, url
    except HTTPError as e:
        if e.code == 404: return False, url
        if e.code not in (405, 501):  # 405/501 继续用 Range GET
            return False, url
    except Exception:
        pass

    # Range GET 1 字节
    try:
        h2 = dict(headers);
        h2['Range'] = 'bytes=0-0'
        req = Request(url, method='GET', headers=h2)
        with urlopen(req, timeout=12) as r:
            if 200 <= r.status < 300:
                return True, url
    except HTTPError as e:
        if e.code == 404: return False, url
        if e.code == 416: return True, url
    except Exception:
        pass

    return False, url


@app.route('/upload', methods=['POST', 'OPTIONS'])
def upload():
    """
    Bridge 入口。先对目标二进制做存在性预检；若禁止覆盖且已存在，返回 code=409 的结果项，
    其余交给 Gradle 任务执行。
    """
    if request.method == 'OPTIONS':
        return _cors(('', 200))

    files = request.files.getlist('files')
    if not files:
        f = request.files.get('file')
        if f: files = [f]
    if not files:
        return _cors(('no file', 400))

    try:
        meta_json = request.form.get('meta') or '[]'
        meta_list = json.loads(meta_json)
        meta_map = {m.get('name'): m for m in meta_list if isinstance(m, dict) and m.get('name')}
    except Exception:
        meta_map = {}

    env = os.environ.copy()
    # 前端认证优先
    authType = request.form.get('authType');
    authValue = request.form.get('authValue')
    if authType == 'Bearer' and authValue:
        env['ARTIFACTORY_TOKEN'] = authValue
    elif authType == 'Basic' and authValue:
        env['PUBLISHER_BASIC'] = authValue

    results = []
    tmp_root = tempfile.mkdtemp(prefix='ijpub-')
    try:
        for f in files:
            fn_orig = f.filename
            fn = secure_filename(fn_orig)
            tmp_path = os.path.join(tmp_root, fn)
            f.save(tmp_path)

            m = meta_map.get(fn_orig, {})
            allow_overwrite = _bool(m.get('allowOverwrite') or request.form.get('allowOverwrite'))

            base_url = m.get('baseUrl') or request.form.get('baseUrl') or env.get('PUBLISHER_BASE_URL') or env.get(
                'publisher.baseUrl')
            folder = (m.get('pluginName') or m.get('pluginId') or request.form.get('pluginName') or request.form.get(
                'pluginId') or 'unknown')

            exists, target_url = _artifact_exists(base_url, folder, fn, env) if base_url else (False, '')
            if exists and not allow_overwrite:
                results.append({
                    'file': fn_orig,
                    'code': 409,
                    'cmd': 'uploadPlugin (skipped: already exists)',
                    'stdout': '',
                    'stderr': f"[{fn_orig}] already exists at {target_url}; skipping to prevent overwrite (server-side preflight)."
                })
                continue

            args = ['-q', 'uploadPlugin', f'-Pfile={tmp_path}']
            for k in ['pluginVersion', 'pluginId', 'sinceBuild', 'untilBuild', 'pluginName', 'baseUrl',
                      'downloadUrlPrefix', 'xmlName']:
                v = m.get(k) or request.form.get(k)
                if v: args.append(f'-P{k}={v}')

            tok = env.get('ARTIFACTORY_TOKEN') or env.get('PUBLISHER_TOKEN')
            if tok and not any(s.startswith('-Ptoken=') or s.startswith('-Ppublisher.token=') for s in args):
                args.append(f'-Ptoken={tok}')
            basic = env.get('PUBLISHER_BASIC')
            if basic and not any(s.startswith('-Pbasic=') or s.startswith('-Ppublisher.basic=') for s in args):
                args.append(f'-Pbasic={basic}')

            gradlew = './gradlew' if os.path.isfile('./gradlew') else 'gradle'
            cmd_list = [gradlew] + args

            def mask(parts):
                masked = []
                for s in parts:
                    if s.startswith('-Ptoken=') or s.startswith('-Ppublisher.token='):
                        masked.append(s.split('=', 1)[0] + '=***')
                    elif s.startswith('-Pbasic=') or s.startswith('-Ppublisher.basic='):
                        masked.append(s.split('=', 1)[0] + '=***')
                    else:
                        masked.append(s)
                return ' '.join(masked)

            p = subprocess.run(cmd_list, capture_output=True, text=True, env=env)
            results.append({
                'file': fn_orig,
                'code': p.returncode,
                'cmd': mask(cmd_list),
                'stdout': p.stdout[-12000:],
                'stderr': p.stderr[-12000:]
            })

        status = 200 if all(r['code'] == 0 for r in results) else 500
        return _cors((jsonify({'results': results}), status))
    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass


def _cors(resp):
    body, code = resp if isinstance(resp, tuple) else (resp, 200)
    hdr = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    }
    return (body, code, hdr)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9876)
