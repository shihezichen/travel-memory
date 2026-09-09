#!/usr/bin/env python3
"""旅程搭子（Companion）—— 领域 API 层的最小消费者 demo

一个对话式 agent，把领域 API（/api/ai/*）当作"工具"来调用，
用 DeepSeek 做意图路由 + 最终回答合成。

用法：
  python3 companion.py "帮我回忆一下巴黎西班牙之旅"
  python3 companion.py                  # 进入交互式 REPL

环境变量：
  COMPANION_API_KEY  API Key（必填；网页端「设置 → API Key」生成，建议 read scope）
  TRAVEL_BASE_URL    领域 API 地址（默认 http://127.0.0.1:8084）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

# ---------- 配置 ----------
BASE_URL = os.environ.get("TRAVEL_BASE_URL", "http://127.0.0.1:8084")
class MissingApiKey(RuntimeError):
    """没有可用的 API Key —— 明确报错，而不是悄悄退回某个内置 key。"""


def _get_companion_key():
    """API Key 读取优先级：环境变量 > vault。

    这里**没有内置兜底 key**。原来兜底的是一把真实可用的 read scope key，
    明文写在这个文件里，而这个文件是入库、且准备开源的 —— 等于把管理员账号的
    只读权限公开出去。拿不到 key 就直接报错，让人去配，别给方便留后门。
    """
    env = os.environ.get("COMPANION_API_KEY")
    if env:
        return env
    try:
        out = subprocess.run(
            ["python3", "scripts/secrets_tool.py", "get", "TRAVEL_COMPANION_KEY"],
            cwd=os.environ.get("SECRETS_VAULT_DIR", ""),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    raise MissingApiKey(
        "没找到 API Key。请先设置环境变量 COMPANION_API_KEY，"
        "或把 TRAVEL_COMPANION_KEY 存进 secrets vault。\n"
        "Key 在网页端「设置 → API Key」里生成，建议只给 read scope。"
    )


# 延迟到真正要用的时候再解析：没配 key 的人 import 这个模块不该直接炸
API_KEY = None


def _api_key():
    global API_KEY
    if API_KEY is None:
        API_KEY = _get_companion_key()
    return API_KEY
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


def _get_ds_key():
    try:
        out = subprocess.run(
            ["python3", "scripts/secrets_tool.py", "get", "DEEPSEEK_API_KEY"],
            cwd=os.environ.get("SECRETS_VAULT_DIR", ""),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return os.environ.get("DEEPSEEK_API_KEY", "")


# ---------- 领域 API 客户端（工具实现） ----------
def _call_api(method, path, form=None, json_body=None):
    url = BASE_URL + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {_api_key()}")
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {_api_key()}")
        req.add_header("Content-Type", "application/json")
    elif form:
        body = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {_api_key()}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:200]}


def tool_list_trips():
    return _call_api("GET", "/api/ai/trips")


def tool_get_memory(trip_id):
    return _call_api("GET", f"/api/ai/trips/{trip_id}/memory")


def tool_get_memory_overview():
    return _call_api("GET", "/api/ai/memory")


def tool_recall_memory(lookahead_days=7):
    return _call_api("GET", f"/api/ai/recall?lookahead_days={lookahead_days}")


def tool_get_associations(trip_id=None, limit=5):
    path = "/api/ai/associations"
    if trip_id:
        path += f"?trip_id={trip_id}&limit={limit}"
    return _call_api("GET", path)


def tool_get_preferences():
    return _call_api("GET", "/api/ai/preferences")


def tool_generate(kind, trip_id, style="旅行者视角", language="zh"):
    return _call_api("POST", "/api/ai/generate",
                     {"kind": kind, "trip_id": trip_id, "style": style, "language": language})


def tool_create_trip(name, start_date="", end_date="", description="", status="planning"):
    return _call_api("POST", "/api/ai/trips",
                     {"name": name, "start_date": start_date,
                      "end_date": end_date, "description": description,
                      "status": status})


def tool_update_trip_status(trip_id, status):
    """更新行程状态机：planning 计划中 → traveling 进行中 → completed 已完成 → archived 已归档"""
    return _call_api("PUT", f"/api/ai/trips/{trip_id}",
                     {"name": "", "status": status})


def tool_geocode(address):
    return _call_api("GET", "/api/ai/geocode?address=" + urllib.parse.quote(address))


def tool_add_point(trip_id, name, lat="", lng="", start_date="", end_date="",
                   description="", route_mode="drive", is_waypoint="0"):
    return _call_api("POST", f"/api/ai/trips/{trip_id}/points",
                     {"name": name, "lat": lat, "lng": lng, "start_date": start_date,
                      "end_date": end_date, "description": description,
                      "route_mode": route_mode, "is_waypoint": is_waypoint})


def tool_update_point(point_id, name, lat="", lng="", start_date="", end_date="",
                      description="", route_mode="drive", is_waypoint="0"):
    return _call_api("PUT", f"/api/ai/points/{point_id}",
                     {"name": name, "lat": lat, "lng": lng, "start_date": start_date,
                      "end_date": end_date, "description": description,
                      "route_mode": route_mode, "is_waypoint": is_waypoint})


def tool_delete_point(point_id):
    return _call_api("DELETE", f"/api/ai/points/{point_id}")


def tool_delete_trip(trip_id):
    return _call_api("DELETE", f"/api/ai/trips/{trip_id}")


def tool_export_trip(trip_id):
    return _call_api("GET", f"/api/ai/trips/{trip_id}/export")


def tool_export_memory():
    return _call_api("GET", "/api/ai/memory/export")


def tool_discover_patterns():
    return _call_api("GET", "/api/ai/patterns")


def tool_find_unfinished_journeys():
    return _call_api("GET", "/api/ai/unfinished")


def tool_annual_travel_report(year=""):
    return _call_api("GET", f"/api/ai/wrapped?year={year}" if year else "/api/ai/wrapped")


def tool_shared_memory(with_username):
    return _call_api("GET", f"/api/ai/shared?with_username={with_username}")


def tool_reinterpret_trip(trip_id, lens="成长"):
    return _call_api("GET", f"/api/ai/trips/{trip_id}/reinterpret?lens={lens}")


def tool_travel_biography():
    return _call_api("GET", "/api/ai/biography")


def tool_time_travel(mode, date="", trip_id=""):
    q = f"mode={mode}"
    if date:
        q += f"&date={date}"
    if trip_id:
        q += f"&trip_id={trip_id}"
    return _call_api("GET", f"/api/ai/timetravel?{q}")


def tool_recommend_destinations(count=5, style=""):
    return _call_api("POST", "/api/ai/recommend",
                     {"count": count, "style": style})


def tool_import_memory(bundle):
    return _call_api("POST", "/api/ai/memory/import", json_body=bundle)


def tool_get_stats(trip_id):
    return _call_api("GET", f"/api/ai/trips/{trip_id}/stats")


def tool_create_share(trip_id, expire_days="", password=""):
    return _call_api("POST", f"/api/ai/trips/{trip_id}/share",
                     {"expire_days": expire_days, "password": password})


def tool_delete_share(trip_id):
    return _call_api("DELETE", f"/api/ai/trips/{trip_id}/share")


def tool_share_count(trip_id):
    return _call_api("GET", f"/api/ai/trips/{trip_id}/share-count")


def tool_list_media(point_id):
    return _call_api("GET", f"/api/ai/points/{point_id}/media")


def tool_upload_media(point_id, data_base64, filename="", content_type=""):
    return _call_api("POST", f"/api/ai/points/{point_id}/media",
                     {"data_base64": data_base64, "filename": filename,
                      "content_type": content_type})


def tool_delete_media(media_id):
    return _call_api("DELETE", f"/api/ai/media/{media_id}")


# ---------- 工具定义（function calling schema） ----------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_trips",
            "description": "列出用户的所有旅行行程（含 id 和名称）。当用户问'我去过哪里'或需要定位某次行程时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory",
            "description": "获取某次行程的结构化旅行记忆（时间线、地点、照片数、主题）。需要 trip_id。",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "integer", "description": "行程 id"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_overview",
            "description": "获取用户的旅行记忆全景（标准化 bundle）：所有行程简表、整体统计、旅行人格画像（偏好+跨行程联想）、足迹范围。当另一个 agent 需要快速理解'这个人去过哪里、是什么旅行人格'时，先调用本工具拿到全景，再按需用 get_memory/search_memory 深挖。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_associations",
            "description": "获取跨行程的联想（重访地点、主题重复、季节偏好）。可选 trip_id 限定某次行程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_preferences",
            "description": "获取用户的长期旅行偏好画像。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "唤醒用户的旅行周年纪念：找出今天或未来 N 天内「几年前的今天」用户去过的地方，返回一段有温度的唤醒语（如「去年今天你在巴黎」）。适合主动向用户推送回忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "lookahead_days": {"type": "integer", "description": "向前看的天数 0-30，默认 7"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "用自然语言检索用户的旅行记忆（如'我去过哪些海边''哪里拍过日落'）。返回结构化匹配（含坐标、日期、匹配依据）。供需要精确程序化消费记忆的 agent 调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "自然语言查询，如'我去年在海边那次'"},
                    "limit": {"type": "integer", "description": "返回条数 1-10，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_memory",
            "description": "用自然语言'问'用户的旅行记忆，返回一段有温度的对话式回答（如'我上次在巴黎吃的小馆子叫什么'）。适合直接向用户转述的对话场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "自然语言问题，如'我是不是每次去海边都住海景房'"},
                    "limit": {"type": "integer", "description": "检索候选条数 1-10，默认 5"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate",
            "description": "记忆创作引擎：根据行程生成内容。kind 可选：trip_summary(旅程总结)/route_guide(路书)/postcard(明信片)/diary(日记)/prose(旅行散文,引用真实地点+情绪元数据)/poem(旅行短诗)/story(旅行小故事)。当用户说'写一篇散文/诗/故事/总结/路书/明信片'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["trip_summary", "route_guide", "postcard", "diary"]},
                    "trip_id": {"type": "integer"},
                    "style": {"type": "string"},
                    "language": {"type": "string", "enum": ["zh", "en"], "description": "输出语言"},
                },
                "required": ["kind", "trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_trip",
            "description": "创建一次新旅行行程。当用户说'我要去XX旅行'或'记一次新旅行'时调用。name 必填，日期/描述可选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "行程名称，如'川西环线'"},
                    "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                    "description": {"type": "string"},
                    "status": {"type": "string", "enum": ["planning", "traveling", "completed", "archived"],
                               "description": "行程状态（默认 planning）"},
                    "client_request_id": {"type": "string", "description": "幂等键（可选）：网络重试时传相同值避免重复建行程，建议用 UUID"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_trip_status",
            "description": "更新行程状态机：planning 计划中 → traveling 进行中 → completed 已完成 → archived 已归档。当用户说'这个行程开始了/去过了/结束了/归档吧'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["planning", "traveling", "completed", "archived"]},
                },
                "required": ["trip_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "按地名查经纬度（WGS-84）。当用户只给地名、或需要确认某地坐标时调用，先 geocode 再 add_point。",
            "parameters": {
                "type": "object",
                "properties": {"address": {"type": "string", "description": "地名，如'杭州西湖'或'巴黎埃菲尔铁塔'"}},
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_point",
            "description": "给某次行程添加一个地点。当用户说'我们去了XX'或'把XX记进行程'时调用。只需 trip_id + 地点名；经纬度可省略（系统会自动地理编码）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "integer"},
                    "name": {"type": "string", "description": "地点名称，尽量带城市，如'杭州西湖'而非'西湖'"},
                    "lat": {"type": "number", "description": "纬度 WGS-84（可选，缺省自动地理编码）"},
                    "lng": {"type": "number", "description": "经度 WGS-84（可选，缺省自动地理编码）"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "description": {"type": "string"},
                    "route_mode": {"type": "string", "enum": ["drive", "line"]},
                    "client_request_id": {"type": "string", "description": "幂等键（可选）：网络重试时传相同值避免重复建点，建议用 UUID"},
                },
                "required": ["trip_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_point",
            "description": "修改行程中某个地点的信息（名称/坐标/日期/描述等）。当用户说'把XX改成YY'或'更新XX地点'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "point_id": {"type": "integer"},
                    "name": {"type": "string", "description": "新的地点名称"},
                    "lat": {"type": "number", "description": "纬度 WGS-84（可选，缺省自动地理编码）"},
                    "lng": {"type": "number", "description": "经度 WGS-84（可选，缺省自动地理编码）"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "description": {"type": "string"},
                    "route_mode": {"type": "string", "enum": ["drive", "line"]},
                },
                "required": ["point_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_point",
            "description": "从行程中删除一个地点。当用户明确说'删掉XX地点'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"point_id": {"type": "integer"}},
                "required": ["point_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_trip",
            "description": "删除整个行程（含所有地点和媒体）。当用户明确说'删掉XX行程'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "integer"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_trip",
            "description": "导出某次行程为 JSON（坐标 WGS-84），供备份/迁移/二次加工。当用户说'导出/备份XX行程'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "integer"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_memory",
            "description": "全量导出用户所有旅行记忆（行程+地点+情绪元数据+媒体元数据+人格画像+联想）为一个可携带 bundle（travel-memory.full-bundle.v1）。当用户说'把我的记忆全部导出/带走/备份全量数据'时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_patterns",
            "description": "跨旅程模式发现：分析用户所有行程，自动发现重访地点、季节偏好、地点类型偏好、出行节奏，产出可分享的「旅行画像」一句话总结。当用户问'我的旅行习惯/画像/我是什么旅行人格/我常去什么样的地方'时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_unfinished_journeys",
            "description": "未完成旅程识别：扫描用户记忆中的「没说出口的遗憾」——搁置超过 90 天的计划、没归档的旅程、计划过但从没去成的地方，并生成一句把遗憾变成前进动力的提示语。当用户说'我有什么没完成/没去成的地方/搁置的计划/帮我想想下一步去哪'时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "annual_travel_report",
            "description": "年度旅行报告：聚合某一年（默认当前年）的行程/地点/城市/里程/照片/情绪统计，生成一段有温度的年度叙事。当用户问'我这一年去了哪/年度总结/旅行年报/2025年都去过哪'时调用。可传 year 参数回溯历史年份。",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "年份，如 2025；留空为当前年"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shared_memory",
            "description": "共同记忆：把两个用户的足迹叠在一起——共同去过的城市、同一天/同一月的时间重叠、各自独享的目的地，生成一段「你们的故事」。当用户说'我和XX的共同记忆/一起去过哪/我们的交集'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "with_username": {"type": "string", "description": "对比的对方用户名"},
                },
                "required": ["with_username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reinterpret_trip",
            "description": "记忆再阐释：给过去的旅程换一个视角重新讲述（成长/遗憾/同行/幽默四种镜头），输出基于真实地点清单的纪实体重述。当用户说'重新讲一遍这段旅程/换个角度回忆/这段旅行现在怎么看'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "integer", "description": "行程 ID"},
                    "lens": {"type": "string", "description": "视角镜头：成长/遗憾/同行/幽默，默认成长"},
                },
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "travel_biography",
            "description": "旅行人生传记：把用户全部已成行旅程按时间排序写成一本分章节的书（markdown 格式），含全局统计（行程数/城市/里程/照片）。当用户说'给我写本旅行传记/一生的旅行/把我的旅程写成书'时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "time_travel",
            "description": "时间旅行：三种模式在记忆的时间轴穿行——rewind 回溯某天（date=YYYY-MM-DD，看那天在哪/做什么）；preview 预演未来某天的规划行程（date）；counterfactual 反事实推演（trip_id，'如果当时换一条路'，输出带【虚构推演】标注）。当用户说'回到某一天/那天我在哪/未来有什么计划/如果当时去了别处会怎样'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "rewind/preview/counterfactual"},
                    "date": {"type": "string", "description": "日期 YYYY-MM-DD（rewind/preview 需要）"},
                    "trip_id": {"type": "integer", "description": "行程 ID（counterfactual 需要）"},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_destinations",
            "description": "行程推荐：基于用户历史足迹和偏好，推荐「还没去过但可能喜欢」的目的地，每个带可解释理由（如'因为你上次在海边城市最开心'）。当用户问'我接下来该去哪儿/推荐个目的地/还有什么地方适合我'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "推荐数量（1-6，默认5）"},
                    "style": {"type": "string", "description": "推荐风格偏好（预留，可空）"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_memory",
            "description": "导入一个 full-bundle.v1 全量记忆打包（来自 export_memory），恢复/迁移/传承到当前账号。当用户说'导入/恢复/迁移记忆'并给出 bundle 时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bundle": {"type": "object", "description": "export_memory 导出的完整 bundle JSON 对象"},
                },
                "required": ["bundle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "获取某次行程的统计（地点数/照片数/视频数/起止日期）。当用户问'XX行程去了几个地方'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "integer"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_share",
            "description": "为某次行程生成只读分享链接（token），供他人查看。当用户说'把XX行程分享给朋友'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "integer"},
                    "expire_days": {"type": "integer", "description": "有效天数，留空为永久"},
                    "password": {"type": "string", "description": "访问密码（可选）"},
                },
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_share",
            "description": "撤销某次行程的全部分享链接。当用户说'取消分享'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "integer"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_count",
            "description": "查询某次行程当前有效分享链接数量。",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "integer"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_media",
            "description": "列出某地点已上传的照片/视频媒体（含缩略图、EXIF 时间/坐标）。当用户问'XX地点有什么照片'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"point_id": {"type": "integer"}},
                "required": ["point_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_media",
            "description": "给某地点上传一张照片或一段视频（data_base64 为文件内容 base64 编码）。当用户提供图片/视频要记进行程时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "point_id": {"type": "integer"},
                    "data_base64": {"type": "string", "description": "文件内容 base64 编码字符串"},
                    "filename": {"type": "string", "description": "文件名，用于推断类型，如 photo.jpg"},
                    "content_type": {"type": "string", "description": "MIME 类型，如 image/jpeg（filename 无扩展名时用）"},
                },
                "required": ["point_id", "data_base64"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_media",
            "description": "删除某条媒体（照片/视频）。当用户明确说'删掉这张照片'时调用。",
            "parameters": {
                "type": "object",
                "properties": {"media_id": {"type": "integer"}},
                "required": ["media_id"],
            },
        },
    },
]

TOOL_FNS = {
    "list_trips": lambda **kw: tool_list_trips(),
    "get_memory": lambda **kw: tool_get_memory(kw["trip_id"]),
    "get_memory_overview": lambda **kw: tool_get_memory_overview(),
    "recall_memory": lambda **kw: tool_recall_memory(kw.get("lookahead_days", 7)),
    "get_associations": lambda **kw: tool_get_associations(kw.get("trip_id"), kw.get("limit", 5)),
    "get_preferences": lambda **kw: tool_get_preferences(),
    "generate": lambda **kw: tool_generate(kw["kind"], kw["trip_id"], kw.get("style", "旅行者视角"), kw.get("language", "zh")),
    "geocode": lambda **kw: tool_geocode(kw["address"]),
    "create_trip": lambda **kw: tool_create_trip(kw["name"], kw.get("start_date", ""),
                                                 kw.get("end_date", ""), kw.get("description", ""),
                                                 kw.get("status", "planning")),
    "update_trip_status": lambda **kw: tool_update_trip_status(kw["trip_id"], kw["status"]),
    "add_point": lambda **kw: tool_add_point(kw["trip_id"], kw["name"], kw.get("lat", ""),
                                             kw.get("lng", ""), kw.get("start_date", ""),
                                             kw.get("end_date", ""), kw.get("description", ""),
                                             kw.get("route_mode", "drive")),
    "update_point": lambda **kw: tool_update_point(kw["point_id"], kw["name"], kw.get("lat", ""),
                                                   kw.get("lng", ""), kw.get("start_date", ""),
                                                   kw.get("end_date", ""), kw.get("description", ""),
                                                   kw.get("route_mode", "drive")),
    "delete_point": lambda **kw: tool_delete_point(kw["point_id"]),
    "delete_trip": lambda **kw: tool_delete_trip(kw["trip_id"]),
    "export_trip": lambda **kw: tool_export_trip(kw["trip_id"]),
    "export_memory": lambda **kw: tool_export_memory(),
    "import_memory": lambda **kw: tool_import_memory(kw["bundle"]),
    "discover_patterns": lambda **kw: tool_discover_patterns(),
    "find_unfinished_journeys": lambda **kw: tool_find_unfinished_journeys(),
    "annual_travel_report": lambda **kw: tool_annual_travel_report(kw.get("year", "")),
    "shared_memory": lambda **kw: tool_shared_memory(kw["with_username"]),
    "reinterpret_trip": lambda **kw: tool_reinterpret_trip(kw["trip_id"], kw.get("lens", "成长")),
    "travel_biography": lambda **kw: tool_travel_biography(),
    "time_travel": lambda **kw: tool_time_travel(kw["mode"], kw.get("date", ""), kw.get("trip_id", "")),
    "recommend_destinations": lambda **kw: tool_recommend_destinations(kw.get("count", 5), kw.get("style", "")),
    "get_stats": lambda **kw: tool_get_stats(kw["trip_id"]),
    "create_share": lambda **kw: tool_create_share(kw["trip_id"], kw.get("expire_days", ""),
                                                   kw.get("password", "")),
    "delete_share": lambda **kw: tool_delete_share(kw["trip_id"]),
    "share_count": lambda **kw: tool_share_count(kw["trip_id"]),
    "list_media": lambda **kw: tool_list_media(kw["point_id"]),
    "upload_media": lambda **kw: tool_upload_media(kw["point_id"], kw["data_base64"],
                                                   kw.get("filename", ""), kw.get("content_type", "")),
    "delete_media": lambda **kw: tool_delete_media(kw["media_id"]),
}

SYSTEM = (
    "你是「旅程搭子」，陈刚的旅行记忆助手。"
    f"今天是 {__import__('datetime').date.today().isoformat()}，用户说相对日期时默认按今年理解。"
    "你能访问他的旅行数据库（行程/记忆/联想/偏好），既能读也能写："
    "读——查行程、看记忆、找联想、生成总结/路书/明信片/日记、看统计、导出备份；"
    "写——新建/删除行程、添加/删除地点；分享——生成或撤销分享链接。"
    "回答时：1) 先判断需要哪些工具来获取或修改信息，按需调用；"
    "2) 基于工具返回的真实数据回答，不要编造；"
    "3) 当用户表达'记一下/去了/要去'等记忆意图时，主动用写工具帮他记下来，"
    "并在完成后确认'已记下'；"
    "4) 语气温暖、像朋友聊天；5) 用中文。"
    "如果用户只是想闲聊，直接回答即可，不必调用工具。"
)


def _llm(messages, tools=None, tool_choice="auto", max_tokens=1024):
    api_key = _get_ds_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    payload = {"model": MODEL, "messages": messages, "temperature": 0.6,
               "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    data = json.dumps(payload).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


# 单轮问答中工具迭代上限（防 LLM 死循环调用工具）
MAX_TOOL_ROUNDS = 3


def ask(question: str, history=None) -> str:
    """单轮问答：LLM 决定工具调用 → 执行 → 合成最终回答。"""
    messages = [{"role": "system", "content": SYSTEM}]
    if history:
        messages += history
    messages.append({"role": "user", "content": question})

    # 第一轮：带工具，让 LLM 决定是否调用
    resp = _llm(messages, tools=TOOLS)
    msg = resp["choices"][0]["message"]

    # 循环处理工具调用（上限 MAX_TOOL_ROUNDS 轮）
    for _ in range(MAX_TOOL_ROUNDS):
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        messages.append(msg)  # 追加 assistant 的 tool_calls 消息
        for tc in tool_calls:
            fn = tc["function"]
            name, args = fn["name"], json.loads(fn.get("arguments", "{}") or "{}")
            result = TOOL_FNS[name](**args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False)[:4000],
            })
        resp = _llm(messages, tools=TOOLS)
        msg = resp["choices"][0]["message"]

    content = msg.get("content")
    if content:
        return content.strip()
    # 兜底：工具循环耗尽仍未产出回答时，不再继续迭代，明确告知
    return "(工具调用次数已达上限，请换一种问法或拆成更小的请求)"


def repl():
    print("🧳 旅程搭子已上线（输入 exit 退出）\n")
    history = []
    while True:
        try:
            q = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "退出"):
            break
        ans = ask(q, history)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": ans})
        print(f"\n搭子：{ans}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(ask(" ".join(sys.argv[1:])))
    else:
        repl()
