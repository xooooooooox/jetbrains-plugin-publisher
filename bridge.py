#!/usr/bin/env python3
import base64
import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

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


# -----------------------
# helpers
# -----------------------
def _bool(v: str):
    if v is None:
        return False
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _load_props(paths):
    """Parse simple key=value .properties from a list of paths (first wins)."""
    props = {}
    for p in paths:
        try:
            with open(p, 'r', encoding='utf-8') as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith('#') or '=' not in s:
                        continue
                    k, v = s.split('=', 1)
                    k = k.strip()
                    v = v.strip()
                    # only set first occurrence
                    if k not in props:
                        props[k] = v
        except Exception:
            pass
    return props


def _merge_env_defaults_from_props(env: dict):
    """
    Fill missing env keys using gradle.properties so server-side preflight
    has baseUrl/auth even when running from Docker Hub without extra env.
    """
    props = _load_props([
        '/app/gradle.properties',
        os.path.expanduser('~/.config/jpp/jpp.properties'),
    ])

    # Repository endpoints
    if not env.get('PUBLISHER_BASE_URL') and props.get('publisher.baseUrl'):
        env['PUBLISHER_BASE_URL'] = props['publisher.baseUrl']
    if not env.get('PUBLISHER_DOWNLOAD_PREFIX') and props.get('publisher.downloadUrlPrefix'):
        env['PUBLISHER_DOWNLOAD_PREFIX'] = props['publisher.downloadUrlPrefix']
    if not env.get('PUBLISHER_XML_NAME') and props.get('publisher.xmlName'):
        env['PUBLISHER_XML_NAME'] = props['publisher.xmlName']
    if not env.get('PUBLISHER_REPO') and props.get('publisher.repo'):
        env['PUBLISHER_REPO'] = props['publisher.repo']

    # Auth – prefer Bearer
    if not env.get('ARTIFACTORY_TOKEN') and props.get('publisher.token'):
        env['ARTIFACTORY_TOKEN'] = props['publisher.token']
    if not env.get('PUBLISHER_TOKEN') and props.get('publisher.token'):
        env['PUBLISHER_TOKEN'] = props['publisher.token']
    if not env.get('PUBLISHER_BASIC') and props.get('publisher.basic'):
        env['PUBLISHER_BASIC'] = props['publisher.basic']


def _auth_header(env):
    tok = env.get('ARTIFACTORY_TOKEN') or env.get('PUBLISHER_TOKEN')
    if tok:
        return {'Authorization': f'Bearer {tok}'}
    basic = env.get('PUBLISHER_BASIC')
    if basic:
        enc = base64.b64encode(basic.encode('utf-8')).decode('ascii')
        return {'Authorization': f'Basic {enc}'}
    return {}


def _artifact_exists(base_url: str, folder: str, filename: str, env) -> Tuple[Optional[bool], str]:
    """
    Preflight existence check with tri-state result:
      - (True, url)  -> exists
      - (False, url) -> not exists
      - (None, url)  -> unknown (e.g., missing auth, 401/403, network)
    """
    base = (base_url or '').rstrip('/')
    folder = (folder or 'unknown').strip('/')
    url = f"{base}/{folder}/{filename}"
    headers = _auth_header(env)

    # HEAD first
    try:
        req = Request(url, method='HEAD', headers=headers)
        with urlopen(req, timeout=12) as r:
            if 200 <= r.status < 300:
                return True, url
    except HTTPError as e:
        if e.code == 404:
            return False, url
        # 401/403 -> unknown (likely missing/invalid auth)
        if e.code in (401, 403):
            return None, url
        # 405/501 -> try range GET
        if e.code not in (405, 501):
            return None, url
    except Exception:
        return None, url

    # Fallback: Range GET 1 byte
    try:
        h2 = dict(headers)
        h2['Range'] = 'bytes=0-0'
        req = Request(url, method='GET', headers=h2)
        with urlopen(req, timeout=12) as r:
            if 200 <= r.status < 300:
                return True, url
    except HTTPError as e:
        if e.code == 404:
            return False, url
        if e.code == 416:  # range unsat but object exists
            return True, url
        if e.code in (401, 403):
            return None, url
    except Exception:
        return None, url

    return None, url


# -----------------------
# routes
# -----------------------
@app.route('/upload', methods=['POST', 'OPTIONS'])
def upload():
    """
    Bridge entrypoint.
    - Server preflights artifact existence.
    - If overwrite is disabled and preflight says "exists", we skip with code=409.
    - If overwrite is disabled and preflight is UNKNOWN (no baseUrl / auth / 401/403),
      we skip with code=412 (Precondition Failed) to avoid accidental overwrite.
    - Otherwise we run the Gradle task.
    """
    if request.method == 'OPTIONS':
        return _cors(('', 200))

    files = request.files.getlist('files')
    if not files:
        f = request.files.get('file')
        if f:
            files = [f]
    if not files:
        return _cors(('no file', 400))

    # Meta per-file (optional)
    try:
        meta_json = request.form.get('meta') or '[]'
        meta_list = json.loads(meta_json)
        meta_map = {m.get('name'): m for m in meta_list if isinstance(m, dict) and m.get('name')}
    except Exception:
        meta_map = {}

    # Effective environment
    env = os.environ.copy()

    # 1) Browser-supplied auth (takes precedence)
    authType = request.form.get('authType')
    authValue = request.form.get('authValue')
    if authType == 'Bearer' and authValue:
        env['ARTIFACTORY_TOKEN'] = authValue
    elif authType == 'Basic' and authValue:
        env['PUBLISHER_BASIC'] = authValue

    # 2) Fallback to gradle.properties for baseUrl/auth if still missing
    _merge_env_defaults_from_props(env)

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

            # Resolve baseUrl and folder (target path)
            base_url = (
                    m.get('baseUrl')
                    or request.form.get('baseUrl')
                    or env.get('PUBLISHER_BASE_URL')
                    or env.get('publisher.baseUrl')  # very old name, keep as fallback
            )
            folder = (
                    m.get('pluginName')
                    or m.get('pluginId')
                    or request.form.get('pluginName')
                    or request.form.get('pluginId')
                    or 'unknown'
            )

            # Server-side preflight when overwrite protection is ON
            if not allow_overwrite:
                if not base_url:
                    # We cannot even form the preflight URL; block upload to be safe
                    results.append({
                        'file': fn_orig,
                        'code': 412,
                        'cmd': 'uploadPlugin (skipped: preflight requires baseUrl)',
                        'stdout': '',
                        'stderr': f"[{fn_orig}] overwrite protection active but baseUrl is missing; "
                                  f"set PUBLISHER_BASE_URL or provide baseUrl in the request."
                    })
                    continue

                exists, target_url = _artifact_exists(base_url, folder, fn, env)
                if exists is True:
                    # Found existing object -> block
                    results.append({
                        'file': fn_orig,
                        'code': 409,
                        'cmd': 'uploadPlugin (skipped: already exists)',
                        'stdout': '',
                        'stderr': f"[{fn_orig}] already exists at {target_url}; "
                                  f"skipping to prevent overwrite (server-side preflight)."
                    })
                    continue
                elif exists is None:
                    # Unknown (auth missing/invalid or network) -> block to be safe
                    results.append({
                        'file': fn_orig,
                        'code': 412,
                        'cmd': 'uploadPlugin (skipped: preflight unknown)',
                        'stdout': '',
                        'stderr': f"[{fn_orig}] preflight could not determine existence at {target_url}; "
                                  f"to avoid accidental overwrite, upload is blocked. "
                                  f"Provide valid credentials or set PUBLISHER_BASE_URL/ARTIFACTORY_TOKEN."
                    })
                    continue
                # exists is False -> safe to proceed

            # Build Gradle args
            args = ['-q', 'uploadPlugin', f'-Pfile={tmp_path}']
            for k in ['pluginVersion', 'pluginId', 'sinceBuild', 'untilBuild', 'pluginName', 'baseUrl',
                      'downloadUrlPrefix', 'xmlName']:
                v = m.get(k) or request.form.get(k)
                if v:
                    args.append(f'-P{k}={v}')

            # Auth pass-through for the Gradle uploader
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
        # UI is served from the same origin, but allow Authorization just in case
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization'
    }
    return (body, code, hdr)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9876)
