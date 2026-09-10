"""APIRouter 拆分包。

app.py 原本把 96 个路由堆在一个文件里，其中 /api/ai/* 与 /mcp（服务外部 agent）
和 Web UI 的路由是两套完全不相交的 API：不同鉴权、不同限流、不同负载特性。
这个包按「服务对象」把它们拆开，**路由定义逐字未改**，只是把 @app.xxx 换成
@router.xxx，再由 app.py 用 include_router 按原顺序挂回去。

拆分的正确性用机械方式验证：拆分前后 /openapi.json 的 (method, path) 集合必须完全相同。
"""
