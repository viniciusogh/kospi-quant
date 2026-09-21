"""Deterministic charts/tables for the single market report. Never calls an LLM."""
import hashlib
import json
import math
from pathlib import Path
import re
import sys

SINCE = '2026-09-21'
MARKER = '그림과 표로 보는 시장'


def rt(value, bold=False):
    return [{'type':'text','text':{'content':str(value)},'annotations':{'bold':bold}}]


def heading(text):
    return {'object':'block','type':'heading_2','heading_2':{'rich_text':rt(text)}}


def table(headers, rows):
    return {'object':'block','type':'table','table':{'table_width':len(headers),
        'has_column_header':True,'has_row_header':False,'children':[
            {'object':'block','type':'table_row','table_row':{'cells':[rt(c, index==0) for c in row]}}
            for index,row in enumerate([headers]+rows)]}}


def keyword_items(record, root):
    sys.path.insert(0,str(Path(__file__).parent/'viz'))
    import keywords
    sources=record['sources']
    text='\n'.join(r['analysis'] for r in sources)
    cache=Path(root)/'latest_keyword_insight.json'
    saved=json.loads(cache.read_text()).get('items',[]) if cache.exists() else []
    counts=keywords.count_keywords(text,Path(root)/'latest_kospi_supply.csv',top=25,
                                   extra_words=[r[0] for r in saved])
    out=[]
    for word,count,kind in counts:
        # Use a literal excerpt from the same inputs, not yesterday's AI explanation.
        contexts=[re.sub(r'^[\s#*|>\-]+','',line).replace('**','').strip()
                  for source in sources for line in source['analysis'].splitlines()
                  if word in line and len(line.strip())>len(word)+8 and not line.lstrip().startswith('|')]
        context=next((s for s in contexts if not s.startswith(('🟢','🔴','🟡'))),'')
        out.append((word,count,kind,context[:150]+('…' if len(context)>150 else '')))
    if not out:raise ValueError('키워드 그림에 사용할 당일 언급 없음')
    return out


def sector_inputs(record, root):
    import pandas as pd
    import sector_dashboard as sector
    rows=record['sector']['rows'];day=record['sector']['data_date']
    fallback=None
    if any(r.get('chart_capital') is None or r.get('chart_leaders') is None for r in rows):
        # Migration of existing reports: only same-day raw rows, same universe and theme map.
        old=sector.UNI
        try:
            sector.UNI=str(Path(root)/'latest_kospi_supply.csv');fallback=sector.universe()
        finally:sector.UNI=old
        if not fallback['기준일'].astype(str).str[:10].eq(day).all():
            raise ValueError('섹터 그림의 시총/주도주 기준일 불일치')
    agg=[];tops={};groups=[]
    for row in rows:
        name=row['업종'];cap=row.get('chart_capital');leaders=row.get('chart_leaders')
        if cap is None or leaders is None:
            sub=fallback[fallback['섹터']==name]
            cap=float(sub['시가총액'].sum())
            leaders=[(r['종목명'],float(r['당일등락(%)'])/100)
                     for _,r in sub.nlargest(sector.TOP_PER_SECTOR,'시가총액').iterrows()]
        if not math.isfinite(cap) or cap<=0 or not leaders or any(not math.isfinite(float(r[1])) for r in leaders):
            raise ValueError('섹터 그림의 면적/개별 등락 누락')
        tops[name]=leaders
        groups.append((name,row['당일등락률_pct'],cap))
        agg.append({'섹터':name,'오늘':row['당일등락률_pct']/100,'d5':row['5일등락률_pct']/100,
                    'd20':row['20일등락률_pct']/100,'순매수':row['외국인기관순매수_억원']*100})
    return pd.DataFrame(agg).sort_values('오늘',ascending=False),tops,sorted(groups,key=lambda r:-r[2])


def build(record, root, write):
    """Freeze figures + native table data once. The model text is never regenerated."""
    folder=Path(root)/'.recommendation_audits'/('market-visuals-'+record['data_date'])
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=folder/'manifest.json'
    key=hashlib.sha256(json.dumps({'sources':record['sources'],'sector':record['sector']},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if path.exists():
        data=json.loads(path.read_text())
        if data['input_sha256']!=key:raise ValueError('당일 시각자료 입력 변경')
        if all(hashlib.sha256((folder/x['file']).read_bytes()).hexdigest()==x['sha256'] for x in data['images']):return data,folder
        raise ValueError('시각자료 파일 변조/손실')
    sys.path.insert(0,str(Path(__file__).parent/'viz'))
    import keywords,treemap,sector_dashboard as sector
    items=keyword_items(record,root)
    keywords.render(items,record['data_date'],str(folder/'keywords.png'),n_videos=len(record['sources']))
    agg,tops,groups=sector_inputs(record,root)
    strip={'hot_label':sector._rank_labels(agg)[0].replace('🔥','▲').replace('🔻','▼'),
           'cold_label':sector._rank_labels(agg)[1].replace('🧊','▼').replace('🔺','▲'),
           'hot':[(r['섹터'],r['오늘']*100,tops[r['섹터']]) for _,r in agg.head(6).iterrows()],
           'cold':[(r['섹터'],r['오늘']*100,tops[r['섹터']]) for _,r in agg.tail(6).iloc[::-1].iterrows()]}
    treemap.render(groups,record['sector']['data_date']+' 마감',str(folder/'sectors.png'),strip=strip)
    _,sector_tables=sector.blocks(agg,tops,record['sector']['data_date'])
    data={'input_sha256':key,'keyword_items':items,'images':[],
          'keyword_table':table(['키워드','언급 횟수','영상 분석본의 언급 맥락'],[[r[0],f'{r[1]}회',r[3]] for r in items[:15]]),
          'sector_table':sector_tables[0]}
    for file,caption in [('keywords.png',f"{record['data_date']} · 통합 글에 사용한 영상 {len(record['sources'])}편 분석본의 언급 횟수"),
                         ('sectors.png',f"{record['sector']['data_date']} 정규장 마감 · 섹터 등가중 등락률 · 면적은 시가총액 제곱근 · 상승 초록 / 하락 빨강")]:
        data['images'].append({'file':file,'caption':caption,'sha256':hashlib.sha256((folder/file).read_bytes()).hexdigest()})
    write(path,data)
    return data,folder


def text(block):
    return ''.join(r.get('plain_text',r.get('text',{}).get('content','')) for r in block[block['type']].get('rich_text',[]))


def narrative(blocks):
    end=next((i for i,b in enumerate(blocks) if b['type']=='heading_2' and text(b)==MARKER),len(blocks))
    return blocks[:end]


def ensure(record, parent, dashboard, root, write):
    data,folder=build(record,root,write)
    expected=[heading(MARKER),heading('영상에서 많이 나온 이야기'),{'image_index':0},data['keyword_table'],
              heading('섹터·테마의 실제 움직임'),{'image_index':1},data['sector_table']]
    def signature(blocks):
        out=[]
        for b in blocks:
            kind=b['type'];body=b[kind]
            if kind=='table':
                kids=body.get('children') if 'children' in body else dashboard.children(b['id'])
                out.append((kind,[[ ''.join(x.get('plain_text',x.get('text',{}).get('content','')) for x in cell)
                                 for cell in kid['table_row']['cells']] for kid in kids]))
            elif kind=='image':out.append((kind,''.join(r.get('plain_text',r.get('text',{}).get('content','')) for r in body.get('caption',[]))))
            else:out.append((kind,text(b)))
        return out
    def image_block(index,fid='expected'):
        return {'object':'block','type':'image','image':{'type':'file_upload','file_upload':{'id':fid},'caption':rt(data['images'][index]['caption'])}}
    wanted=[image_block(b['image_index']) if 'image_index' in b else b for b in expected]
    children=dashboard.children(parent);existing=children[len(narrative(children)):]
    if signature(existing)==signature(wanted):return
    # Resume only this visual section; no changes to the report narrative or model calls.
    for b in existing:
        response=dashboard.requests.delete(dashboard.API+'/blocks/'+b['id'],headers=dashboard._h(),timeout=20)
        response.raise_for_status()
    actual=[]
    for b in expected:
        if 'image_index' in b:
            item=data['images'][b['image_index']]
            fid=dashboard.upload_image((folder/item['file']).read_bytes(),item['file'])
            if not fid:raise ValueError('통합 보고서 그림 업로드 실패')
            b=image_block(b['image_index'],fid)
        actual.append(b)
    headers={**dashboard._h(),'Notion-Version':dashboard.NV_UPLOAD}
    response=dashboard.requests.patch(dashboard.API+'/blocks/'+parent+'/children',headers=headers,json={'children':actual},timeout=60)
    response.raise_for_status()
    children=dashboard.children(parent)
    if signature(children[len(narrative(children)):])!=signature(wanted):raise ValueError('통합 그림/표 게시 대조 실패')
