"""Local intake API. Browser observations are evidence, never instructions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .douyin_source import DouyinStore
from .douyin_media import MAX_UPLOAD_BYTES


class LinkInput(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    query: str = Field(default='', max_length=300)
    author_url: str = Field(default='', max_length=2000)


class DiscoveryInput(BaseModel):
    query: str = Field(default='', max_length=300)
    author_url: str = Field(default='', max_length=2000)
    max_results: int = Field(default=5, ge=1, le=10)


class EvidenceInput(BaseModel):
    segments: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class DiscoveryObservations(BaseModel):
    observations: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    status: str = Field(default='completed', max_length=80)
    reason: str = Field(default='', max_length=4000)
    duration_ms: int = Field(default=0, ge=0, le=3600000)


class ObservationInput(BaseModel):
    canonical_url: str | None = Field(default=None, max_length=2000)
    title: str | None = Field(default=None, max_length=2000)
    author: str | None = Field(default=None, max_length=400)
    author_url: str | None = Field(default=None, max_length=2000)
    published_at: str | None = Field(default=None, max_length=100)
    published_at_source: str | None = Field(default=None, max_length=200)
    published_at_evidence: str | None = Field(default=None, max_length=400)
    observation_method: str = Field(default='manual_browser_observation', max_length=100)
    status: str | None = Field(default=None, max_length=80)
    reason: str | None = Field(default=None, max_length=4000)
    duration_seconds: float | None = Field(default=None, ge=0, le=1800)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    metrics: dict[str, Any] | None = None


def local_origin(request: Request):
    origin = request.headers.get('origin')
    if origin:
        try:
            parsed = urlsplit(origin)
            valid = (parsed.scheme in {'http', 'https'} and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
                     and not parsed.username and not parsed.password and (parsed.port is None or 1 <= parsed.port <= 65535)
                     and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(403, '仅接受本机工作台请求')


def create_douyin_router(store: DouyinStore, pipeline, submit_review: Callable) -> APIRouter:
    router = APIRouter(prefix='/api/v1/douyin', dependencies=[Depends(local_origin)])
    workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix='douyin-media')
    futures, lock = {}, Lock()

    def present(item):
        value = dict(item)
        value.update(author_name=item.get('author', ''), date_source=item.get('published_at_source', ''),
                     status_reason=item.get('reason', ''), segments=item.get('evidence', []))
        value['media_url'] = f"/api/v1/douyin/items/{item['item_id']}/media" if item.get('media_available') else None
        value['local_media_url'] = value['media_url']
        media = [m for m in item.get('media', []) if m.get('status') == 'available']
        media.sort(key=lambda m: 0 if m['mime_type'].startswith('video/') else 1 if m['mime_type'].startswith('audio/') else 2)
        value['media_type'] = media[0]['mime_type'].split('/')[0] if media else ''
        metadata = item.get('metadata', {})
        value['metrics'] = metadata.get('metrics', {})
        value['stats'] = {k: metadata.get('metrics', {}).get(v) for k, v in [('like_count', 'likes'), ('comment_count', 'comments'), ('share_count', 'shares')] if v in metadata.get('metrics', {})}
        value['stats_observed_at'] = metadata.get('metrics_observed_at')
        case_id = item.get('case_id') or ('douyin-' + item['aweme_id'] if item.get('aweme_id') else '')
        if case_id:
            row = pipeline.db.source_item(case_id)
            if row:
                import json
                record = json.loads(row['payload_json'])
                value.update(case_id=case_id, case_status=row['status'], score=record.get('scorecard', {}).get('quality_score'),
                             retry_at=record.get('next_retry_at'), attempt_count=record.get('attempt_count', 0))
                if row['status'] in {'needs_extraction', 'needs_scoring'}:
                    reason = record.get('extraction', {}).get('reason') or record.get('quality_review', {}).get('reason')
                    if reason:
                        value['status_reason'] = reason
        if item['item_id'] in futures and not futures[item['item_id']].done():
            value['status'] = 'processing'
        return value

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, '作品或素材不存在') from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get('/items')
    def items(status: str = '', limit: int = Query(default=100, ge=1, le=500)):
        return {'items': [present(i) for i in store.list_items(status, limit)], 'source_state': store.source_state(), 'storage': store.storage_summary()}

    @router.post('/import')
    def import_link(body: LinkInput):
        return {'item': present(call(store.import_link, **body.model_dump()))}

    @router.get('/items/{item_id}')
    def item(item_id: str):
        return {'item': present(call(store.get_item, item_id))}

    @router.post('/items/{item_id}/observation')
    def observation(item_id: str, body: ObservationInput):
        return {'item': present(call(store.record_observation, item_id, body.model_dump(exclude_unset=True)))}

    @router.post('/items/{item_id}/evidence')
    def evidence(item_id: str, body: EvidenceInput):
        values = []
        for segment in body.segments:
            value = dict(segment)
            value['start_seconds'] = value.pop('start', value.get('start_seconds'))
            value['end_seconds'] = value.pop('end', value.get('end_seconds'))
            if value.get('source') and not value.get('observation_method'):
                value['observation_method'] = value['source']
            values.append(value)
        return {'item': present(call(store.record_observation, item_id, {'evidence': values, 'observation_method': 'user_evidence_import'}))}

    @router.post('/discover')
    def discover(body: DiscoveryInput):
        task = call(store.request_discovery, body.query, body.author_url, body.max_results)
        return {'task': task, 'message': '已登记本地浏览器任务。打开抖音页面正常读取后录入观察；后台尚未自动执行网页搜索。'}

    @router.get('/discovery/tasks')
    def tasks():
        return {'items': store.list_discovery_tasks(), 'source_state': store.source_state()}

    @router.post('/source/resume')
    def resume():
        return {'source_state': store.resume_source(), 'message': '已恢复待浏览器读取；此操作不表示登录或验证已成功。'}

    @router.post('/discovery/tasks/{task_id}/observations')
    def discovery_observations(task_id: str, body: DiscoveryObservations):
        return {'task': call(store.complete_discovery, task_id, **body.model_dump())}

    @router.post('/items/{item_id}/media')
    async def upload(item_id: str, request: Request, filename: str = Query(min_length=1, max_length=200)):
        call(store.get_item, item_id)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, '素材超过单文件容量上限')
        return {'item': present(call(store.import_media, item_id, bytes(data), filename))}

    @router.get('/items/{item_id}/media')
    def media(item_id: str, media_id: str = ''):
        path = call(store.media_path, item_id, media_id)
        return FileResponse(path, headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'})

    @router.post('/items/{item_id}/process', status_code=202)
    def process(item_id: str):
        saved = call(store.get_item, item_id)
        item_id = saved['item_id']
        with lock:
            for key, future in list(futures.items()):
                if future.done():
                    futures.pop(key)
            if item_id not in futures:
                if futures:
                    raise HTTPException(409, '另一条素材正在转写，请稍后再试')
                futures[item_id] = workers.submit(store.process_media, item_id)
        return {'item': present(store.get_item(item_id)), 'message': '本地素材处理已提交，结果会保存在作品记录中。'}

    @router.post('/items/{item_id}/review', status_code=202)
    def review(item_id: str):
        saved = call(store.get_item, item_id)
        if saved['status'] == 'processed':
            return {'item': present(saved), 'message': '该证据版本已完成审核；重复提交不会重新调用模型。'}
        if saved['status'] != 'ready':
            raise HTTPException(409, saved.get('reason') or '当前作品尚未准备好审核')
        document = call(store.evidence_document, item_id)
        if not document['ready']:
            raise HTTPException(409, document['reason'])
        old = pipeline.db.source_item(document['source_item_id'])
        if old:
            import json
            from .collection_health import retry_ready
            payload = json.loads(old['payload_json'])
            if payload.get('douyin_evidence_revision') == pipeline._douyin_revision(saved) and not retry_ready(payload):
                raise HTTPException(409, '当前证据版本仍在重试冷却或已达到尝试上限；请补充新证据或稍后重试')
        result = submit_review(document['source_item_id'])
        return {'job': result, 'item': present(store.get_item(item_id)), 'message': '已提交现有抽取、核验、评分和复审流程；入选门槛保持不变。'}

    return router
