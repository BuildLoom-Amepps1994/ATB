"""Offline local app smoke test using a substitute exchange, with no real orders."""
import math
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP_DIR))

class FakeExchange:
    orders = []
    def __init__(self,options):
        self.options=options
        self.has={'fetchTickers':True,'fetchOHLCV':True}
        self.timeframes={'1h':'1h','4h':'4h','1d':'1d','1w':'1w'}
        self.markets={s:{'symbol':s,'base':s.split('/')[0],'quote':'USDT','type':'spot','spot':True,'contract':False,'active':True,'limits':{'cost':{'min':10},'amount':{'min':.00001}}} for s in ['BTC/USDT','ETH/USDT','SOL/USDT','DOGE/USDT','PEPE/USDT','SHIB/USDT','BONK/USDT','FLOKI/USDT']}
    def load_markets(self): return self.markets
    def market(self,symbol):return self.markets[symbol]
    def fetch_balance(self,opts=None):return {'free':{'USDT':10000,'BTC':.1,'DOGE':20}}
    def fetch_ticker(self,symbol):return {'timestamp':int(time.time()*1000),'last':135.0,'quoteVolume':20000000,'bid':134.99,'ask':135.01}
    def fetch_ohlcv(self,symbol,tf='1h',limit=175):
        candles=[]
        step={'1h':3600000,'4h':14400000,'1d':86400000,'1w':604800000}[tf]
        latest=int(time.time()*1000)//step*step
        for i in range(limit):
            p=105+i*.11+2*math.sin(i*.3)
            candles.append([latest-(limit-1-i)*step,p-.4,p+1,p-1,p,500000+i*5])
        return candles
    def fetch_tickers(self,symbols=None):return {s:{'timestamp':int(time.time()*1000),'last':135,'bid':134.99,'ask':135.01,'quoteVolume':12000000} for s in self.markets if symbols is None or s in symbols}
    def cost_to_precision(self,sym,budget):return str(round(budget,2))
    def amount_to_precision(self,sym,qty):return str(round(qty,6))
    def create_market_buy_order_with_cost(self,*args):
        self.orders.append(('buy',args))
        return {'status':'closed','filled':args[1]/135,'cost':args[1],'average':135,'id':'testorder'}
    def create_order(self,*args):
        self.orders.append(('sell',args))
        return {'status':'closed','filled':args[3],'cost':args[3]*135,'average':135,'id':'testorder2'}

@pytest.fixture(scope='module')
def webapp():
    # Restore every substitute after the module, including state paths and vault
    # functions. Never contact a real exchange or the user's credential manager.
    import ccxt
    import credentials
    import storage
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as patch:
        patch.setenv('CRYPTOPILOT_DATA_DIR',tmp)
        patch.setattr(storage,'DATA_DIR',Path(tmp))
        patch.setattr(storage,'DB_PATH',Path(tmp)/'portfolio.sqlite3')
        patch.setattr(storage,'CONFIG_PATH',Path(tmp)/'settings.json')
        patch.setattr(credentials,'load',lambda exchange:None)
        patch.setattr(credentials,'save',lambda *args:None)
        patch.setattr(credentials,'delete',lambda *args:None)
        patch.setattr(ccxt,'mexc',FakeExchange)
        patch.setattr(ccxt,'kraken',FakeExchange)
        FakeExchange.orders=[]
        import app as module
        # The fixture may be imported by another test module. Give each temporary
        # database a new engine even when Python has already cached app.py.
        patch.setattr(module, 'engine', module.TradingEngine())
        yield module
        module.engine.stop()


def test_local_dashboard_and_state(webapp):
    client=TestClient(webapp.app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',50000))
    assert client.get('/').status_code==200
    assert 'CryptoPilot' in client.get('/').text
    state=client.get('/api/state')
    assert state.status_code==200
    assert state.json()['mode']=='paper'
    assert state.json()['running'] is False


def test_csrf_and_manual_scan_does_not_trade(webapp):
    client=TestClient(webapp.app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',50000))
    assert client.post('/api/scan').status_code==403
    headers={'x-cryptopilot-token':webapp.CSRF}
    r=client.post('/api/scan',headers=headers)
    assert r.status_code==200
    assert len(r.json()['markets'])==8
    assert FakeExchange.orders==[]
    assert client.get('/api/history?symbol=BTC%2FUSDT').status_code==200


def test_live_requires_credentials_and_confirmation(webapp):
    client=TestClient(webapp.app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',50000))
    headers={'x-cryptopilot-token':webapp.CSRF}
    r=client.post('/api/mode',headers=headers,json={'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True})
    assert r.status_code==400
    r=client.post('/api/connect',headers=headers,json={'api_key':'test-key-123','api_secret':'test-secret-123','remember':False})
    assert r.status_code==200
    r=client.post('/api/mode',headers=headers,json={'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':False})
    assert r.status_code==400
    r=client.post('/api/mode',headers=headers,json={'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True})
    assert r.status_code==200
    assert r.json()['mode']=='live'
    assert FakeExchange.orders==[]  # Mode activation alone never submits orders.
    client.post('/api/mode',headers=headers,json={'mode':'paper'})


def test_manual_http_paper_and_explicit_fake_live_buy_sell_with_deduplication(webapp):
    client=TestClient(webapp.app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',50000))
    headers={'x-cryptopilot-token':webapp.CSRF}
    before=len(FakeExchange.orders)
    paper_buy={'symbol':'DOGE/USDT','side':'buy','quote_amount':27,'request_id':str(uuid4()),
               'expected_context':client.get('/api/state').json()['manual_order_context']}
    response=client.post('/api/order',headers=headers,json=paper_buy)
    assert response.status_code==200, response.text
    assert response.json()['mode']=='paper'
    assert client.post('/api/order',headers=headers,json=paper_buy).json()==response.json()
    paper_sell={'symbol':'DOGE/USDT','side':'sell','request_id':str(uuid4()),
                'expected_context':paper_buy['expected_context']}
    response=client.post('/api/order',headers=headers,json=paper_sell)
    assert response.status_code==200, response.text
    assert response.json()['status']=='filled'
    assert len(FakeExchange.orders)==before

    assert client.post('/api/connect',headers=headers,json={'api_key':'fake-key-for-http','api_secret':'fake-secret-for-http'}).status_code==200
    assert client.post('/api/mode',headers=headers,json={'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True}).status_code==200
    # With the fake adapter's 2-decimal cost precision this reserves fees and
    # buys exactly 0.2 units, allowing a complete 6-decimal sell below.
    live_buy={**paper_buy,'quote_amount':27.03,'request_id':str(uuid4()),
              'expected_context':client.get('/api/state').json()['manual_order_context']}
    assert client.post('/api/order',headers=headers,json=live_buy).status_code==400
    assert len(FakeExchange.orders)==before
    live_buy.update(confirmation='PLACE LIVE BUY',request_id=str(uuid4()))
    response=client.post('/api/order',headers=headers,json=live_buy)
    assert response.status_code==200, response.text
    assert response.json()['mode']=='live'
    assert client.post('/api/order',headers=headers,json=live_buy).json()==response.json()
    assert len(FakeExchange.orders)==before+1
    live_sell={**paper_sell,'confirmation':'PLACE LIVE SELL','request_id':str(uuid4()),
               'expected_context':live_buy['expected_context']}
    response=client.post('/api/order',headers=headers,json=live_sell)
    assert response.status_code==200, response.text
    assert response.json()['status']=='filled'
    assert client.post('/api/order',headers=headers,json=live_sell).json()==response.json()
    assert len(FakeExchange.orders)==before+2
    assert not client.get('/api/state').json()['positions']
    assert client.post('/api/mode',headers=headers,json={'mode':'paper'}).status_code==200


def test_manual_http_stale_exchange_confirmation_cannot_execute_on_new_account(webapp):
    client=TestClient(webapp.app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',50000))
    headers={'x-cryptopilot-token':webapp.CSRF}
    assert client.post('/api/connect',headers=headers,json={'api_key':'fake-key-for-http','api_secret':'fake-secret-for-http'}).status_code==200
    assert client.post('/api/mode',headers=headers,json={'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True}).status_code==200
    previous_context=client.get('/api/state').json()['manual_order_context']
    stale_order={'symbol':'DOGE/USDT','side':'buy','quote_amount':27.03,'request_id':str(uuid4()),
                 'confirmation':'PLACE LIVE BUY','expected_context':previous_context}
    # A second tab switches exchange and explicitly arms its own account.
    assert client.post('/api/mode',headers=headers,json={'mode':'paper'}).status_code==200
    assert client.post('/api/config',headers=headers,json={'settings':{'exchange':'kraken'}}).status_code==200
    assert client.post('/api/connect',headers=headers,json={'api_key':'fake-context-key','api_secret':'fake-context-secret'}).status_code==200
    assert client.post('/api/mode',headers=headers,json={'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True}).status_code==200
    current=client.get('/api/state').json()
    assert current['exchange']=='kraken'
    assert current['manual_order_context']!=previous_context
    before=len(FakeExchange.orders)
    response=client.post('/api/order',headers=headers,json=stale_order)
    assert response.status_code==400
    assert 'changed' in response.json()['detail'].lower() or 'context' in response.json()['detail'].lower()
    assert len(FakeExchange.orders)==before
    assert client.post('/api/mode',headers=headers,json={'mode':'paper'}).status_code==200
