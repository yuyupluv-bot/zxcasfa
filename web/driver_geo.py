"""Optional isolated map service. Never used for assigning or timing orders.

Run in its own container; inputs are local OpenMapTiles-schema MBTiles and
local address index. No public OSM tile/router/geocoder fallback is permitted.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import threading
from urllib.parse import quote
import requests
from flask import Flask,request,jsonify,abort,Response,send_from_directory
from common.database import get_session
from .driver_work import authenticate

app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=8192
DATA=Path(os.getenv('DRIVER_GEO_DATA','/app/geo/data'))
CENTER=(58.3747,58.3213)
RADIUS=50_000
_route_slots=threading.BoundedSemaphore(2)

def distance(a,b):
    x1,y1,x2,y2=map(math.radians,(a[0],a[1],b[0],b[1]))
    h=math.sin((x2-x1)/2)**2+math.cos(x1)*math.cos(x2)*math.sin((y2-y1)/2)**2
    return 6371000*2*math.asin(min(1,math.sqrt(h)))

def point(value,radius=RADIUS):
    if not isinstance(value,list) or len(value)!=2:abort(400)
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in value):abort(400)
    lat,lon=value
    if not -90<=lat<=90 or not -180<=lon<=180:abort(400)
    if distance((lat,lon),CENTER)>radius:abort(422,description='Точка вне покрытия карты.')
    return lat,lon

def readonly(name):
    p=DATA/name
    if not p.is_file():abort(503,description='Данные карты района ещё не установлены.')
    connection=sqlite3.connect('file:'+quote(str(p))+'?mode=ro&immutable=1',uri=True,timeout=1)
    return connection

@app.before_request
def db_open():
    # Tile requests never consume PostgreSQL connections.
    if request.method=='POST':request.db=get_session()

@app.teardown_request
def db_close(error):
    session=getattr(request,'db',None)
    if session:session.close()

@app.after_request
def safe(response):
    response.headers['X-Content-Type-Options']='nosniff'
    if request.method=='POST':response.headers['Cache-Control']='no-store'
    return response

@app.get('/driver-map/status')
def status():
    return jsonify(center=list(CENTER),radius_m=RADIUS,
       tiles_ready=(DATA/'region.mbtiles').is_file(),addresses_ready=(DATA/'addresses.sqlite').is_file())

@app.get('/driver-map/style.json')
def style():
    if not (DATA/'region.mbtiles').is_file():abort(503,description='Карты района не установлены.')
    base=os.getenv('DRIVER_GEO_PUBLIC_URL','https://panel.bezrateadminps.ru').rstrip('/')
    if not base.startswith('https://'):abort(503)
    layers=[{'id':'background','type':'background','paint':{'background-color':'#F0F1E9'}},
      {'id':'landuse','type':'fill','source':'region','source-layer':'landuse','paint':{'fill-color':'#DEE8D0','fill-opacity':.7}},
      {'id':'water','type':'fill','source':'region','source-layer':'water','paint':{'fill-color':'#A6D2E3'}},
      {'id':'buildings','type':'fill','source':'region','source-layer':'building','minzoom':13,'paint':{'fill-color':'#D3D5C9'}},
      {'id':'road-outline','type':'line','source':'region','source-layer':'transportation','paint':{'line-color':'#D0D2C6','line-width':['interpolate',['linear'],['zoom'],8,1,16,10]}},
      {'id':'roads','type':'line','source':'region','source-layer':'transportation','paint':{'line-color':'#FFFFFF','line-width':['interpolate',['linear'],['zoom'],8,.5,16,7]}}]
    if (DATA/'fonts'/'Noto Sans Regular').is_dir():
        layers.extend([
          {'id':'road-names','type':'symbol','source':'region','source-layer':'transportation_name','minzoom':12,
           'layout':{'symbol-placement':'line','text-field':['get','name'],'text-font':['Noto Sans Regular'],'text-size':12},
           'paint':{'text-color':'#5B6354','text-halo-color':'#FFFFFF','text-halo-width':1.5}},
          {'id':'places','type':'symbol','source':'region','source-layer':'place',
           'layout':{'text-field':['get','name'],'text-font':['Noto Sans Regular'],'text-size':14},
           'paint':{'text-color':'#25301D','text-halo-color':'#FFFFFF','text-halo-width':1.5}}])
    return jsonify(version=8,name='БЕЗТАКСЫ · Горнозаводск',
      glyphs=base+'/driver-map/fonts/{fontstack}/{range}.pbf',
      sources={'region':{'type':'vector','tiles':[base+'/driver-map/tiles/{z}/{x}/{y}.pbf'],
        'minzoom':0,'maxzoom':14,'attribution':'© OpenStreetMap contributors'}},layers=layers)

@app.get('/driver-map/tiles/<int:z>/<int:x>/<int:y>.pbf')
def tile(z,x,y):
    if not 0<=z<=14 or not 0<=x<2**z or not 0<=y<2**z:abort(404)
    with closing(readonly('region.mbtiles')) as db:
        row=db.execute('SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',(z,x,2**z-1-y)).fetchone()
    if row is None:return Response(status=204)
    response=Response(row[0],content_type='application/vnd.mapbox-vector-tile')
    if row[0][:2]==b'\x1f\x8b':response.headers['Content-Encoding']='gzip'
    response.headers['Cache-Control']='public,max-age=86400'
    return response

@app.get('/driver-map/fonts/<font>/<name>.pbf')
def fonts(font,name):
    if font!='Noto Sans Regular' or not re.fullmatch(r'\d+-\d+',name):abort(404)
    return send_from_directory(DATA/'fonts'/font,name+'.pbf',max_age=86400)

@app.post('/api/driver-app/geo/search')
def search():
    body,_,_=authenticate()
    request.db.close()
    query=body.get('query','')
    if not isinstance(query,str) or not 3<=len(query.strip())<=160:abort(400)
    # Offline index stores normalized labels, coordinates and original labels.
    tokens=[t for t in re.split(r'\W+',query.casefold()) if t][:8]
    if not tokens:abort(400)
    where=' AND '.join('normalized LIKE ? ESCAPE \'\\\'' for _ in tokens)
    values=['%'+t.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' for t in tokens]
    with closing(readonly('addresses.sqlite')) as db:
        rows=db.execute('SELECT label,latitude,longitude FROM addresses WHERE '+where+' ORDER BY length(label) LIMIT 15',values).fetchall()
    return jsonify(results=[{'label':r[0],'point':[r[1],r[2]]} for r in rows if distance((r[1],r[2]),CENTER)<=RADIUS])

@app.post('/api/driver-app/geo/route')
def route():
    body,_,_=authenticate()
    request.db.close()
    a=point(body.get('from'),70_000);b=point(body.get('to'))
    base=os.getenv('DRIVER_ROUTER_URL','http://driver-router:5000').rstrip('/')
    # Caller never chooses the upstream URL. This is not an open proxy.
    if not _route_slots.acquire(blocking=False):return jsonify(error='routing_busy'),429
    try:
        reply=requests.get(f'{base}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}',
          params={'overview':'full','geometries':'geojson','steps':'true','alternatives':'false'},timeout=(2,6))
        reply.raise_for_status();data=reply.json()
        if data.get('code')!='Ok' or not data.get('routes'):return jsonify(error='route_not_found'),422
        if any(w.get('distance', float('inf'))>250 for w in data.get('waypoints', [])):
            return jsonify(error='point_far_from_road',message='Точка далеко от дороги. Уточните адрес на карте.'),422
        result=data['routes'][0]
        if len(result.get('geometry',{}).get('coordinates',[]))>60000:abort(502)
        return jsonify(route=result,traffic=False,coverage_radius_m=RADIUS)
    except (requests.RequestException,ValueError):
        return jsonify(error='routing_unavailable',message='Маршрутизация недоступна. Управление заявкой работает независимо.'),503

    finally:
        _route_slots.release()
