import base64
import ipaddress
import json
from html import escape
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from flask import Response, jsonify, render_template, request
from plugin import F, PluginModuleBase
from tool import ToolUtil
from .setup import P

logger = P.logger
ModelSetting = P.ModelSetting
BASE = Path(__file__).resolve().parent
CHANNEL_FILE = BASE / "data" / "channels.json"
CACHE_TTL = 600
_resolve_cache = {}
_cache_lock = threading.RLock()

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36"
REFERERS = {
    "kbs": "https://onair.kbs.co.kr/",
    "mbc": "https://mini.imbc.com/",
    "sbs": "https://www.sbs.co.kr/radio",
    "cbs": "https://www.cbs.co.kr/",
}


def _setting(key, default):
    try:
        v = ModelSetting.get(key)
        return default if v in (None, "") else str(v).strip()
    except Exception:
        return default


def _bool_setting(key, default=False):
    return _setting(key, "True" if default else "False").lower() in ("1", "true", "yes", "on")


def _public_ff_url():
    value = (F.SystemModelSetting.get("ddns") or "").strip().rstrip("/")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return request.url_root.rstrip("/")


def _ff_api_url(path):
    value = ToolUtil.make_apikey_url(path)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return _public_ff_url() + value


def _channels():
    with CHANNEL_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    enabled = {x.strip() for x in _setting("disabled_channels", "").split(",") if x.strip()}
    return [x for x in data if x.get("id") not in enabled]


def _channel(cid):
    return next((x for x in _channels() if x.get("id") == cid), None)


def _headers(ch):
    station = ch.get("station", "")
    h = {
        "User-Agent": _setting("user_agent", DEFAULT_UA),
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    ref = REFERERS.get(station)
    if ref:
        h["Referer"] = ref
    return h


def _safe_remote(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("허용되지 않은 원본 URL")
    port = p.port or (443 if p.scheme == "https" else 80)
    for item in socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM):
        addr = ipaddress.ip_address(item[4][0])
        if not addr.is_global:
            raise ValueError("사설/로컬 원본 URL은 사용할 수 없습니다")
    return url


def _bsod_resolve(ch):
    params = {"stn": ch.get("station", "")}
    if ch.get("channel"):
        params["ch"] = ch["channel"]
    if ch.get("city"):
        params["city"] = ch["city"]
    url = "https://radio.bsod.kr/api/url?" + urlencode(params)
    r = requests.get(url, headers={"User-Agent": DEFAULT_UA, "Accept": "application/json,*/*"}, timeout=(6, 12))
    r.raise_for_status()
    try:
        payload = r.json()
        out = payload.get("url") if isinstance(payload, dict) else None
    except Exception:
        out = None
    if not out:
        m = re.search(r'https?://[^\s"\']+', r.text)
        out = m.group(0) if m else None
    if not out:
        raise ValueError("radio.bsod.kr API에서 스트림 URL을 찾지 못했습니다")
    return out.replace("\\/", "/")


def _sbs_direct_resolve(ch):
    channel = ch.get("channel")
    endpoint_map = {
        "lovefm": "https://apis.sbs.co.kr/play-api/1.0/livestream/lovepc/lovefm?protocol=hls&ssl=Y",
        "powerfm": "https://apis.sbs.co.kr/play-api/1.0/livestream/powerpc/powerfm?protocol=hls&ssl=Y",
        "dmb": "https://apis.sbs.co.kr/play-api/1.0/livestream/sbsdmbpc/sbsdmb?protocol=hls&ssl=Y",
    }
    api = endpoint_map.get(channel)
    if not api:
        raise ValueError("SBS 채널 코드 없음")
    r = requests.get(api, headers=_headers(ch), timeout=(6, 12))
    r.raise_for_status()
    url = r.text.strip().strip('"').replace("\\/", "/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("SBS 스트림 URL 추출 실패")
    return url


def _ebs_direct_resolve(ch):
    if ch.get("channel") == "bandi":
        return "https://ebsonair.ebs.co.kr/cloud1/iradio/playlist.m3u8"
    headers = _headers(ch)
    headers["Accept"] = "text/html,application/xhtml+xml"
    r = requests.get("https://www.ebs.co.kr/onair?channelCodeString=radio", headers=headers, timeout=(6, 12))
    r.raise_for_status()
    m = re.search(r"src:\s*['\"](https://liveotu\.ebs\.co\.kr/[^'\"]+)", r.text)
    if m:
        return m.group(1)
    return "https://ebsonair.ebs.co.kr/fmradiofamilypc/familypc1m/playlist.m3u8"


def _busan_mbc_datecode():
    # KST 날짜 YYMMDD를 부산MBC 전용 문자코드로 변환
    mapping = {"0":"q","1":"h","2":"s","3":"a","4":"g","5":"b","6":"j","7":"c","8":"p","9":"d"}
    kst = time.gmtime(time.time() + 9 * 60 * 60)
    raw = time.strftime("%y%m%d", kst)
    return "".join(mapping.get(c, c) for c in raw)


def _regional_mbc_resolve(ch):
    city = ch.get("city")
    channel = ch.get("channel")
    if city == "busan":
        code = _busan_mbc_datecode()
        if channel == "sfm":
            return f"https://brook.bsmbc.com/live/BusanMBC_AM_onairstream.{code}/playlist.m3u8"
        if channel == "fm4u":
            return f"https://brook.bsmbc.com/live/BusanMBC_FM_onairstream.{code}/playlist.m3u8"
    raise ValueError("지역 MBC 동적 주소 처리 대상 아님")


def _mbc_direct_resolve(ch):
    channel_map = {"sfm": "sfm", "fm4u": "mfm", "chm": "chm"}
    mbc_channel = channel_map.get(ch.get("channel"))
    if not mbc_channel:
        raise ValueError("MBC 채널 코드 없음")
    api = "https://sminiplay.imbc.com/aacplay.ashx?" + urlencode({
        "agent": "webapp",
        "channel": mbc_channel,
    })
    r = requests.get(api, headers=_headers(ch), timeout=(6, 12))
    r.raise_for_status()
    url = r.text.strip().replace("\\/", "/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("MBC 스트림 URL 추출 실패")
    return url


def _kbs_direct_resolve(ch):
    code_map = {"1radio":"21","2radio":"22","3radio":"23","1fm":"24","2fm":"25","hanminjok":"26"}
    city_map = {"busan":"10_","ulsan":"11_","changwon":"20_","jinju":"21_","daegu":"30_","andong":"31_","pohang":"32_","gwangju":"40_","mokpo":"41_","suncheon":"43_","jeonju":"50_","daejeon":"60_","cheongju":"70_","chungju":"71_","chuncheon":"80_","gangneung":"81_","wonju":"82_","jeju":"90_"}
    code = code_map.get(ch.get("channel"))
    if not code:
        raise ValueError("KBS 채널 코드 없음")
    full = city_map.get(ch.get("city", ""), "") + code
    api = "https://cfpwwwapi.kbs.co.kr/api/v1/landing/live/channel_code/" + full
    r = requests.get(api, headers=_headers(ch), timeout=(6, 12))
    r.raise_for_status()
    obj = r.json()
    candidates = []
    def walk(x):
        if isinstance(x, dict):
            if x.get("media_type") == "radio" and x.get("service_url"):
                candidates.append(x["service_url"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    if not candidates:
        raise ValueError("KBS service_url 추출 실패")
    return candidates[0]


def resolve_channel(ch, force=False):
    cid = ch["id"]
    now = time.time()
    with _cache_lock:
        cached = _resolve_cache.get(cid)
        if not force and cached and now - cached[1] < CACHE_TTL:
            return cached[0]
    if ch.get("direct"):
        url = ch["direct"]
    elif ch.get("station") == "kbs":
        # KBS는 본사/지역국 모두 공식 API를 직접 사용
        url = _kbs_direct_resolve(ch)
    elif ch.get("station") == "mbc" and ch.get("city") == "busan":
        url = _regional_mbc_resolve(ch)
    elif ch.get("station") == "mbc" and not ch.get("city") and ch.get("channel") in ("sfm", "fm4u", "chm"):
        url = _mbc_direct_resolve(ch)
    elif ch.get("station") == "sbs" and not ch.get("city"):
        url = _sbs_direct_resolve(ch)
    elif ch.get("station") == "ebs":
        url = _ebs_direct_resolve(ch)
    else:
        url = _bsod_resolve(ch)
    _safe_remote(url)
    with _cache_lock:
        _resolve_cache[cid] = (url, now)
    return url


def _b64e(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")


def _proxy_url(cid, absolute):
    return _ff_api_url("/ff_kkradio/api/proxy?" + urlencode({"id": cid, "u": _b64e(absolute)}))


def _rewrite_m3u(text, base_url, cid):
    out = []
    uri_attr = re.compile(r'URI="([^"]+)"')
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append(raw)
            continue
        if line.startswith("#"):
            def repl(m):
                absolute = urljoin(base_url, m.group(1))
                return 'URI="' + _proxy_url(cid, absolute) + '"'
            out.append(uri_attr.sub(repl, raw))
        else:
            absolute = urljoin(base_url, line)
            out.append(_proxy_url(cid, absolute))
    return "\n".join(out) + "\n"


def _fetch_remote(ch, url):
    _safe_remote(url)
    r = requests.get(url, headers=_headers(ch), timeout=(8, 25), stream=True, allow_redirects=True)
    r.raise_for_status()
    return r


def _m3u(group=None, station=None, single=None):
    base = _public_ff_url()
    rows = _channels()
    if group:
        rows = [x for x in rows if x.get("group") == group]
    if station:
        rows = [x for x in rows if x.get("station") == station]
    if single:
        rows = [x for x in rows if x.get("id") == single]
    lines = ["#EXTM3U"]
    for ch in rows:
        live = _ff_api_url("/ff_kkradio/api/live.m3u8?" + urlencode({"id": ch["id"]}))
        attrs = 'group-title="{}"'.format(ch.get("group", "기타").replace('"', ''))
        lines.append("#EXTINF:-1 {},{}".format(attrs, ch.get("name", ch["id"])))
        lines.append(live)
    return "\n".join(lines) + "\n"


class Logic(PluginModuleBase):
    def __init__(self, P):
        super(Logic, self).__init__(P, name="list", first_menu="list")
        self.db_default = {
            "user_agent": DEFAULT_UA,
            "disabled_channels": "",
            "prefer_proxy": "True",
        }

    def process_menu(self, sub, req):
        if sub == "setting":
            base = _public_ff_url()
            arg = {
                "api_m3u": _ff_api_url("/ff_kkradio/api/m3u"),
                "user_agent": _setting("user_agent", DEFAULT_UA),
                "disabled_channels": _setting("disabled_channels", ""),
            }
            return render_template("ff_kkradio_setting.html", arg=arg)
        rows = _channels()
        base = _public_ff_url()
        view_rows = []
        for seq, item in enumerate(rows, start=1):
            ch = dict(item)
            ch["seq"] = seq
            ch["live_url"] = _ff_api_url("/ff_kkradio/api/live.m3u8?" + urlencode({"id": ch["id"]}))
            # 새창 재생은 별도 JS 플레이어를 거치지 않고 FF의 실제 HLS URL을 직접 연다.
            ch["player_url"] = ch["live_url"]
            view_rows.append(ch)
        groups = sorted({x.get("group", "기타") for x in rows})
        stations = sorted({x.get("station", "") for x in rows if x.get("station")})
        group_urls = [
            {
                "name": g,
                "url": _ff_api_url("/ff_kkradio/api/m3u?" + urlencode({"group": g}))
            }
            for g in groups
        ]
        station_urls = [
            {
                "name": st,
                "url": _ff_api_url("/ff_kkradio/api/m3u?" + urlencode({"station": st}))
            }
            for st in stations
        ]
        all_m3u_url = _ff_api_url("/ff_kkradio/api/m3u")
        return render_template(
            "ff_kkradio_list.html",
            channels=view_rows,
            groups=groups,
            stations=stations,
            group_urls=group_urls,
            station_urls=station_urls,
            all_m3u_url=all_m3u_url,
        )

    def process_ajax(self, sub, req):
        if sub == "save":
            ModelSetting.set("user_agent", req.form.get("user_agent", DEFAULT_UA).strip())
            ModelSetting.set("disabled_channels", req.form.get("disabled_channels", "").strip())
            return jsonify({"ret":"success", "message":"저장했습니다."})
        if sub == "resolve":
            cid = req.form.get("id", "")
            ch = _channel(cid)
            if not ch:
                return jsonify({"ret":"error", "message":"채널을 찾을 수 없습니다."}), 404
            try:
                return jsonify({"ret":"success", "url":resolve_channel(ch, force=True)})
            except Exception as e:
                logger.exception("[KKRadio] resolve failed: %s", cid)
                return jsonify({"ret":"error", "message":str(e)}), 500
        return jsonify({"ret":"error", "message":"unknown ajax"}), 404

    def process_api(self, sub, req):
        try:
            if sub == "player":
                cid = req.args.get("id", "")
                ch = _channel(cid)
                if not ch:
                    return Response("channel not found", status=404)
                live_url = _ff_api_url("/ff_kkradio/api/live.m3u8?" + urlencode({"id": cid}))
                name = escape(ch.get("name", cid))
                src = json.dumps(live_url)
                html = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} - KK Radio</title>
<style>
body{{margin:0;background:#111;color:#eee;font-family:Arial,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}}
.wrap{{width:min(760px,92vw);background:#1d1d1d;border-radius:14px;padding:24px;box-sizing:border-box}}
h2{{margin:0 0 18px}}
audio{{width:100%}}
.url{{margin-top:16px;word-break:break-all;font-size:13px;color:#bbb}}
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
</head>
<body>
<div class="wrap">
<h2>{name}</h2>
<audio id="audio" controls autoplay></audio>
<div class="url">{url}</div>
</div>
<script>
const audio=document.getElementById('audio');
const src={src};
if (audio.canPlayType('application/vnd.apple.mpegurl')) {{
  audio.src=src;
}} else if (window.Hls && Hls.isSupported()) {{
  const hls=new Hls();
  hls.loadSource(src);
  hls.attachMedia(audio);
  hls.on(Hls.Events.MANIFEST_PARSED,()=>audio.play().catch(()=>{{}}));
}} else {{
  audio.src=src;
}}
</script>
</body>
</html>""".format(name=name, url=escape(live_url), src=src)
                return Response(html, mimetype="text/html")
            if sub == "m3u":
                return Response(_m3u(group=req.args.get("group"), station=req.args.get("station"), single=req.args.get("id")), mimetype="audio/x-mpegurl")
            if sub == "channel.m3u":
                cid = req.args.get("id", "")
                if not _channel(cid):
                    return Response("channel not found", status=404)
                return Response(_m3u(single=cid), mimetype="audio/x-mpegurl")
            if sub == "live.m3u8":
                cid = req.args.get("id", "")
                ch = _channel(cid)
                if not ch:
                    return Response("channel not found", status=404)
                url = resolve_channel(ch)
                r = _fetch_remote(ch, url)
                ctype = (r.headers.get("Content-Type") or "").lower()
                body = r.content
                if "mpegurl" in ctype or urlsplit(r.url).path.lower().endswith((".m3u8", ".m3u")) or body.startswith(b"#EXTM3U"):
                    text = body.decode("utf-8", errors="replace")
                    return Response(_rewrite_m3u(text, r.url, cid), mimetype="application/vnd.apple.mpegurl")
                headers = {}
                if r.headers.get("Content-Length"):
                    headers["Content-Length"] = r.headers["Content-Length"]
                return Response(body, status=r.status_code, content_type=r.headers.get("Content-Type", "application/octet-stream"), headers=headers)
            if sub == "proxy":
                cid = req.args.get("id", "")
                ch = _channel(cid)
                if not ch:
                    return Response("channel not found", status=404)
                try:
                    url = _b64d(req.args.get("u", ""))
                except Exception:
                    return Response("bad proxy url", status=400)
                r = _fetch_remote(ch, url)
                ctype = (r.headers.get("Content-Type") or "").lower()
                body = r.content
                if "mpegurl" in ctype or urlsplit(r.url).path.lower().endswith((".m3u8", ".m3u")) or body.startswith(b"#EXTM3U"):
                    return Response(_rewrite_m3u(body.decode("utf-8", errors="replace"), r.url, cid), mimetype="application/vnd.apple.mpegurl")
                return Response(body, status=r.status_code, content_type=r.headers.get("Content-Type", "application/octet-stream"))
        except requests.HTTPError as e:
            logger.warning("[KKRadio] upstream HTTP error: %s", e)
            return Response("upstream HTTP error: %s" % e, status=502)
        except Exception as e:
            logger.exception("[KKRadio] api error: %s", sub)
            return Response("KKRadio error: %s" % e, status=500)
        return Response("unknown api", status=404)
