#!/usr/bin/env python3
"""Re-evaluate existing verified live cases from cached originals, with a backup."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.app.collection_pipeline import CollectionPipeline
from backend.app.collection_export import export_collection
from backend.app.quality_agent import QualityAgent
from backend.app.scoring import score_case,SCORING_VERSION
from backend.app.db import utc_now
from backend.app.agent_skills import skill_versions
from backend.app.config import runtime_dir


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--limit',type=int,default=20);parser.add_argument('--workers',type=int,default=3);parser.add_argument('--wait-seconds',type=int,default=0);args=parser.parse_args()
    if not 1<=args.limit<=100: parser.error('limit must be 1..100')
    if not 1<=args.workers<=3: parser.error('workers must be 1..3')
    path=runtime_dir()/'catalog.db'
    with open(str(path)+'.lock','a') as lock:
        deadline=time.monotonic()+max(0,args.wait_seconds)
        waiting=False
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline: parser.exit(2,'采集正在运行；请稍后重试，或使用 --wait-seconds 1800 等待当前任务完成。\n')
                if not waiting: print('已有采集任务运行，重评将等待它完成。',flush=True);waiting=True
                time.sleep(min(5,max(0,deadline-time.monotonic())))
        backup=path.with_name('catalog-before-v4-'+utc_now().replace(':','-')+'.db')
        with sqlite3.connect(path) as src,sqlite3.connect(backup) as dst: src.backup(dst)
        pipeline=CollectionPipeline(ROOT,path)
        result={'run_id':'regrade-'+uuid4().hex[:12],'source_mode':'live','pipeline':'quality-review',
                'status':'running','started_at':utc_now(),'items':[],'counts':{},'events':[],
                'skill_versions':skill_versions(),'score_version':SCORING_VERSION,'backup':str(backup)}
        pipeline._active=result
        try:
            candidates=[]
            for row in pipeline.db.source_items(['needs_scoring','candidate','selected','featured'],10000):
                item=json.loads(row['payload_json'])
                if item.get('discovery_mode') in {'web_search','rss'} and item.get('verification',{}).get('decision')=='pass' and score_case(item)['assessment_status']!='approved': candidates.append(item)
            result['discovered']=len(candidates)
            pipeline._event('orchestrator','quality.regrade.start','running')
            documents = []
            for item in candidates[:args.limit]:
                doc=pipeline.db.connection.execute('SELECT body FROM raw_documents WHERE document_id=?',(item.get('raw_document_id','doc-'+item['case_id']),)).fetchone()
                if not doc:
                    result.setdefault('missing_documents',[]).append(item['case_id']);continue
                item['article_hash']=hashlib.sha256(doc['body'].encode()).hexdigest()
                documents.append((item,doc['body']))
            # Workers only read evidence and call models. One coordinator owns all DB writes.
            def evaluate(pair):
                item,body=pair
                item['quality_evaluation']=QualityAgent(pipeline.model).evaluate(item,body)
                return item
            result['workers']=args.workers
            pipeline._event('scorer','quality.batch_review','running',count=len(documents))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures=[pool.submit(evaluate,pair) for pair in documents]
                for future in as_completed(futures):
                    item=future.result()
                    entry=pipeline._process(item);result['items'].append(entry)
                    result['counts'][entry['status']]=result['counts'].get(entry['status'],0)+1
                    pipeline._event('curator','persist',entry['status'],case_id=item['case_id'])
                    print(json.dumps(entry,ensure_ascii=False),flush=True)
            result['status']='partial' if result['counts'].get('needs_scoring') or result.get('missing_documents') or len(candidates)>args.limit else 'completed'
        except Exception as exc:
            result.update(status='failed',error=f'{type(exc).__name__}: {str(exc)[:200]}');raise
        finally:
            result['finished_at']=utc_now()
            result['exports']=export_collection(pipeline.db,result,ROOT/'data/runtime/exports')
            pipeline.db.save_collection_run(result);pipeline.close()
            print(json.dumps({'run_id':result['run_id'],'status':result['status'],'counts':result['counts'],'backup':str(backup)},ensure_ascii=False),flush=True)

if __name__=='__main__': main()
