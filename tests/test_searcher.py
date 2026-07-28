"""searcher 模块单元测试。

覆盖：WebSearcher 的配置读取（configure_from_settings / is_available）、
search 分发到 Tavily / BoCha / Brave 三个 provider 的五类路径
（正常响应、空结果、超时、HTTP 错误、返回体格式异常），以及 fetch_url 的
HTML 清洗与失败降级。

全程不联网：模块内的 aiohttp 由本文件的 fake 通过 monkeypatch 整体替换。
环境无 pytest-asyncio，异步方法统一用 asyncio.run 驱动。
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner import searcher as searcher_mod
from astrbot_plugin_active_learner.searcher import USER_AGENT, WebSearcher


# ---------- 测试替身：伪造 aiohttp ----------


class _FakeTimeout:
    """替代 aiohttp.ClientTimeout，仅记录 total 便于断言。"""

    def __init__(self, total=None):
        self.total = total


class _FakeResponse:
    """替代 aiohttp 响应对象，自身即 async context manager。"""

    def __init__(self, status=200, payload=None, body="", body_error=None):
        self.status = status
        self._payload = payload
        self._body = body
        self._body_error = body_error
        self.text_kwargs = None

    async def json(self):
        if self._body_error is not None:
            raise self._body_error
        return self._payload

    async def text(self, encoding=None, errors="strict"):
        self.text_kwargs = {"encoding": encoding, "errors": errors}
        if self._body_error is not None:
            raise self._body_error
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FailingCtx:
    """请求阶段就抛异常（如超时），异常在 __aenter__ 抛出。"""

    def __init__(self, error):
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response, error, calls):
        self._response = response
        self._error = error
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _ctx(self):
        if self._error is not None:
            return _FailingCtx(self._error)
        return self._response

    def post(self, url, json=None, headers=None):
        self._calls.append(
            {"method": "POST", "url": url, "json": json, "headers": headers}
        )
        return self._ctx()

    def get(self, url, headers=None, params=None):
        self._calls.append(
            {"method": "GET", "url": url, "headers": headers, "params": params}
        )
        return self._ctx()


def _install_fake_aiohttp(monkeypatch, response=None, error=None):
    """把 searcher 模块内的 aiohttp 换成 fake，返回调用记录列表。"""
    calls = []

    def client_session(timeout=None):
        calls.append({"session_timeout": timeout})
        return _FakeSession(response, error, calls)

    fake = SimpleNamespace(ClientTimeout=_FakeTimeout, ClientSession=client_session)
    monkeypatch.setattr(searcher_mod, "aiohttp", fake)
    return calls


def _requests(calls):
    """从调用记录中过滤出真正的 HTTP 请求（排除 session 构造记录）。"""
    return [c for c in calls if "method" in c]


# ---------- 各 provider 的正常响应报文与预期 ----------

_TAVILY_OK = {
    "results": [
        {"title": "标题A", "content": "内容A", "url": "https://a.example"},
    ]
}
_BOCHA_OK = {
    "data": {
        "webPages": [
            {"name": "标题A", "summary": "内容A", "url": "https://a.example"},
        ]
    }
}
_BRAVE_OK = {
    "web": {
        "results": [
            {"title": "标题A", "description": "内容A", "url": "https://a.example"},
        ]
    }
}

_NORMAL_CASES = [
    ("tavily", _TAVILY_OK, "https://api.tavily.com/search", "POST"),
    ("bocha", _BOCHA_OK, "https://api.bochaai.com/v1/web-search", "POST"),
    (
        "brave",
        _BRAVE_OK,
        "https://api.search.brave.com/res/v1/web/search",
        "GET",
    ),
]

# 空结果的多种形态：字段缺失、为 None、为空列表，都应归一为 []
_EMPTY_CASES = [
    ("tavily", {}),
    ("tavily", {"results": None}),
    ("tavily", {"results": []}),
    ("bocha", {}),
    ("bocha", {"data": None}),
    ("bocha", {"data": {"webPages": []}}),
    ("brave", {}),
    ("brave", {"web": None}),
    ("brave", {"web": {"results": []}}),
]

# 返回体格式异常：结构类型不对，或 json() 解析本身失败
_MALFORMED_CASES = [
    ("tavily", {"results": "不是列表"}, None),
    ("tavily", ["顶层不是 dict"], None),
    ("bocha", {"data": "不是字典"}, None),
    ("brave", {"web": 123}, None),
    ("tavily", None, ValueError("响应不是合法 JSON")),
    ("bocha", None, ValueError("响应不是合法 JSON")),
    ("brave", None, ValueError("响应不是合法 JSON")),
]

_PROVIDERS = ["tavily", "bocha", "brave"]


# ---------- 构造与配置 ----------


def test_constructor_lowercases_provider():
    # provider 大小写不敏感：构造时即归一为小写，避免分发时匹配不到
    s = WebSearcher("TAVILY", "key-1")
    assert s._provider == "tavily"
    assert s.is_available is True


def test_constructor_defaults_unavailable():
    # 缺 provider 与 key 时不可用，search 应短路
    assert WebSearcher().is_available is False


@pytest.mark.parametrize(
    ("settings", "provider", "key", "available"),
    [
        # 列表型 key 取第一个
        (
            {"websearch_provider": "Tavily", "websearch_tavily_key": ["k1", "k2"]},
            "tavily",
            "k1",
            True,
        ),
        # 字符串型 key 被 str() 兜底
        (
            {"websearch_provider": "bocha", "websearch_bocha_key": "bk"},
            "bocha",
            "bk",
            True,
        ),
        (
            {"websearch_provider": "brave", "websearch_brave_key": ["bv"]},
            "brave",
            "bv",
            True,
        ),
        # 空列表 → key 为空 → 不可用
        (
            {"websearch_provider": "tavily", "websearch_tavily_key": []},
            "tavily",
            "",
            False,
        ),
        # key 字段缺失
        ({"websearch_provider": "brave"}, "brave", "", False),
        # 未知 provider：不进入任何取 key 分支
        (
            {"websearch_provider": "google", "websearch_tavily_key": ["k1"]},
            "google",
            "",
            False,
        ),
        # provider 为 None → 归一为空串
        ({"websearch_provider": None}, "", "", False),
        # 空配置
        ({}, "", "", False),
    ],
)
def test_configure_from_settings(settings, provider, key, available):
    s = WebSearcher()
    s.configure_from_settings(settings)
    assert s._provider == provider
    assert s._api_key == key
    assert s.is_available is available


@pytest.mark.parametrize("bad", [None, "字符串", 123, ["列表"]])
def test_configure_from_settings_ignores_non_dict(bad):
    # 非 dict 直接返回，原有配置不能被清空
    s = WebSearcher("tavily", "k1")
    s.configure_from_settings(bad)
    assert (s._provider, s._api_key) == ("tavily", "k1")


# ---------- search 分发 ----------


def test_search_unavailable_skips_http(monkeypatch):
    # 不可用时必须在发请求前短路，一次 HTTP 都不能发
    calls = _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=_TAVILY_OK))
    s = WebSearcher()
    assert asyncio.run(s.search("查询")) == []
    assert calls == []


def test_search_unknown_provider_returns_empty(monkeypatch):
    # provider 有值但不在三家之内：三个 if 全不命中，走到函数末尾返回 []
    calls = _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=_TAVILY_OK))
    s = WebSearcher("google", "k1")
    assert s.is_available is True
    assert asyncio.run(s.search("查询")) == []
    assert calls == []


@pytest.mark.parametrize(("provider", "payload", "url", "method"), _NORMAL_CASES)
def test_search_normal_response(monkeypatch, provider, payload, url, method):
    # 正常响应：三家的字段名不同，都要归一为 title/snippet/url
    calls = _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher(provider, "key-1")
    out = asyncio.run(s.search("三体", max_results=5))
    assert out == [
        {"title": "标题A", "snippet": "内容A", "url": "https://a.example"}
    ], f"{provider} 的响应字段未被正确归一化"
    req = _requests(calls)[0]
    assert req["url"] == url
    assert req["method"] == method
    # 三家都设置 15 秒总超时
    assert calls[0]["session_timeout"].total == 15


@pytest.mark.parametrize(("provider", "payload"), _EMPTY_CASES)
def test_search_empty_results(monkeypatch, provider, payload):
    # 空结果的各种形态都应返回空列表，而不是抛异常
    _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher(provider, "key-1")
    assert asyncio.run(s.search("查询")) == []


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize(
    "error",
    [asyncio.TimeoutError(), TimeoutError("读超时"), OSError("连接被重置")],
)
def test_search_timeout_and_network_error(monkeypatch, provider, error):
    # 超时/网络异常由 search 的 try 捕获，对外表现为空结果
    _install_fake_aiohttp(monkeypatch, error=error)
    s = WebSearcher(provider, "key-1")
    assert asyncio.run(s.search("查询")) == []


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_search_http_error_status(monkeypatch, provider, status):
    # 非 200 直接返回空列表，且不应尝试解析 body
    resp = _FakeResponse(status=status, body_error=AssertionError("不应解析 body"))
    _install_fake_aiohttp(monkeypatch, resp)
    s = WebSearcher(provider, "key-1")
    assert asyncio.run(s.search("查询")) == []


@pytest.mark.parametrize(("provider", "payload", "body_error"), _MALFORMED_CASES)
def test_search_malformed_body(monkeypatch, provider, payload, body_error):
    # 返回体结构异常或 JSON 解析失败：统一降级为空列表
    resp = _FakeResponse(payload=payload, body_error=body_error)
    _install_fake_aiohttp(monkeypatch, resp)
    s = WebSearcher(provider, "key-1")
    assert asyncio.run(s.search("查询")) == []


# ---------- 各 provider 的请求参数与字段细节 ----------


def test_tavily_request_payload_and_limits(monkeypatch):
    # api_key 走 body；snippet 截断到 500；结果条数受 max_results 限制
    payload = {
        "results": [
            {"title": f"T{i}", "content": "字" * 600, "url": f"https://{i}"}
            for i in range(5)
        ]
    }
    calls = _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher("tavily", "secret-key")
    out = asyncio.run(s.search("三体", max_results=2))
    assert len(out) == 2, "结果条数应被 max_results 截断"
    assert len(out[0]["snippet"]) == 500, "snippet 应截断到 500 字"
    body = _requests(calls)[0]["json"]
    assert body == {
        "api_key": "secret-key",
        "query": "三体",
        "max_results": 2,
        "search_depth": "basic",
    }


def test_tavily_missing_fields_fallback(monkeypatch):
    # 字段缺失时用空串兜底，不能 KeyError
    payload = {"results": [{}]}
    _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher("tavily", "k")
    assert asyncio.run(s.search("查询")) == [{"title": "", "snippet": "", "url": ""}]


def test_bocha_headers_and_alias_fields(monkeypatch):
    # BoCha 用 Bearer 头；name/summary/url 缺失时回退 title/snippet/link
    payload = {
        "data": {
            "webPages": [
                {"title": "备用标题", "snippet": "备用摘要", "link": "https://b"},
                {"name": "", "summary": "", "url": ""},
            ]
        }
    }
    calls = _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher("bocha", "bk")
    out = asyncio.run(s.search("查询", max_results=5))
    assert out[0] == {
        "title": "备用标题",
        "snippet": "备用摘要",
        "url": "https://b",
    }
    assert out[1] == {"title": "", "snippet": "", "url": ""}
    req = _requests(calls)[0]
    assert req["headers"]["Authorization"] == "Bearer bk"
    assert req["headers"]["Content-Type"] == "application/json"
    assert req["json"] == {"query": "查询", "count": 5}


def test_bocha_snippet_truncated(monkeypatch):
    payload = {"data": {"webPages": [{"name": "T", "summary": "字" * 900}]}}
    _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher("bocha", "bk")
    assert len(asyncio.run(s.search("查询"))[0]["snippet"]) == 500


def test_brave_headers_params_and_snippet_fallback(monkeypatch):
    # Brave 用 X-Subscription-Token 头 + query params；description 为 None 时回退 snippet
    payload = {
        "web": {
            "results": [
                {"title": "T1", "description": None, "snippet": "备用摘要"},
                {"title": "T2", "description": "字" * 700, "url": "https://c"},
            ]
        }
    }
    calls = _install_fake_aiohttp(monkeypatch, _FakeResponse(payload=payload))
    s = WebSearcher("brave", "bv-token")
    out = asyncio.run(s.search("查询", max_results=3))
    assert out[0]["snippet"] == "备用摘要"
    assert out[0]["url"] == ""
    assert len(out[1]["snippet"]) == 500
    req = _requests(calls)[0]
    assert req["headers"]["X-Subscription-Token"] == "bv-token"
    assert req["headers"]["Accept"] == "application/json"
    assert req["params"] == {"q": "查询", "count": 3}


# ---------- fetch_url ----------


def test_fetch_url_strips_html(monkeypatch):
    html = (
        "<html><head><style>body{color:red}</style>"
        "<SCRIPT>var a = 1;</SCRIPT></head>"
        "<body><h1>标题</h1>\n\n<p>正文   内容</p></body></html>"
    )
    resp = _FakeResponse(body=html)
    calls = _install_fake_aiohttp(monkeypatch, resp)
    text = asyncio.run(WebSearcher().fetch_url("https://x.example"))
    # script/style 整段（含大小写变体）被剔除，标签变空格并压缩连续空白
    assert "var a" not in text
    assert "color:red" not in text
    assert "<" not in text and ">" not in text
    assert "标题 正文 内容" in text
    assert "  " not in text
    req = _requests(calls)[0]
    assert req["method"] == "GET"
    assert req["headers"] == {"User-Agent": USER_AGENT}
    assert resp.text_kwargs == {"encoding": "utf-8", "errors": "ignore"}


def test_fetch_url_respects_max_chars(monkeypatch):
    _install_fake_aiohttp(monkeypatch, _FakeResponse(body="<p>" + "字" * 500 + "</p>"))
    text = asyncio.run(WebSearcher().fetch_url("https://x.example", max_chars=20))
    assert len(text) == 20, "抓取文本应截断到 max_chars"


@pytest.mark.parametrize("status", [301, 403, 404, 500])
def test_fetch_url_non_200_returns_empty(monkeypatch, status):
    resp = _FakeResponse(status=status, body="<p>不该被读取</p>")
    _install_fake_aiohttp(monkeypatch, resp)
    assert asyncio.run(WebSearcher().fetch_url("https://x.example")) == ""


@pytest.mark.parametrize(
    "error",
    [
        asyncio.TimeoutError(),
        OSError("DNS 解析失败"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "非法字节"),
    ],
)
def test_fetch_url_swallows_errors(monkeypatch, error):
    # 抓取异常一律降级为空串，不向调用方抛出
    _install_fake_aiohttp(monkeypatch, error=error)
    assert asyncio.run(WebSearcher().fetch_url("https://x.example")) == ""


def test_fetch_url_body_error_returns_empty(monkeypatch):
    # 读 body 阶段失败同样被兜住
    resp = _FakeResponse(body_error=ValueError("解码失败"))
    _install_fake_aiohttp(monkeypatch, resp)
    assert asyncio.run(WebSearcher().fetch_url("https://x.example")) == ""


def test_fetch_url_empty_body(monkeypatch):
    _install_fake_aiohttp(monkeypatch, _FakeResponse(body=""))
    assert asyncio.run(WebSearcher().fetch_url("https://x.example")) == ""
