"""Opt-in native-runtime manual order test, with an entirely simulated exchange.

python tests/smoke_frozen_orders.py --runtime PATH --work SCRATCH_FOLDER
The simulation is installed only in a new temporary folder, never in the release.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4

FAKE = '''import time
class FakeExchange:
    instance = None
    has = {'fetchTickers':True,'createMarketBuyOrderWithCost':True}
    timeframes = {'1m':'1m','5m':'5m','1h':'1h','4h':'4h','1d':'1d','1w':'1w'}
    def __init__(self):
        self.orders=[]
        self.markets={s:{'spot':True,'active':True,'type':'spot','contract':False,'limits':{'cost':{'min':10},'amount':{'min':.000001}}} for s in ['BTC/USDT','ETH/USDT','SOL/USDT','DOGE/USDT','PEPE/USDT','SHIB/USDT','BONK/USDT','FLOKI/USDT']}
        self.balance={'USDT':10000,'BTC':0}
    @classmethod
    def shared(cls):
        if cls.instance is None: cls.instance=cls()
        return cls.instance
    def load_markets(self): return self.markets
    def market(self,s): return self.markets[s]
    def fetch_balance(self,*args): return {'free':dict(self.balance)}
    def fetch_ticker(self,s): return {'last':100,'bid':99.99,'ask':100.01,'timestamp':int(time.time()*1000),'quoteVolume':20000000}
    def fetch_tickers(self,symbols=None): return {s:self.fetch_ticker(s) for s in (symbols or self.markets)}
    def fetch_ohlcv(self,s,tf='1h',limit=175):
        step={'1m':60000,'5m':300000,'1h':3600000,'4h':14400000,'1d':86400000,'1w':604800000}[tf]
        end=int(time.time()*1000)//step*step
        return [[end-(limit-i)*step,100,101,99,100,10000] for i in range(limit)]
    def cost_to_precision(self,s,cost): return str(round(cost,2))
    def amount_to_precision(self,s,qty): return str(round(qty,9))
    def create_market_buy_order_with_cost(self,s,cost):
        assert cost*1.001<=self.balance['USDT']+.00001
        self.balance['USDT']-=cost*1.001
        qty=cost/100
        self.balance['BTC']+=qty
        self.orders.append('BUY')
        return {'id':str(len(self.orders)),'status':'closed','filled':qty,'cost':cost,'average':100,'fee':{'currency':'USDT','cost':cost*.001}}
    def create_order(self,s,kind,side,qty):
        assert kind=='market' and side=='sell' and qty<=self.balance['BTC']+1e-12
        self.balance['BTC']-=qty
        self.balance['USDT']+=qty*100*.999
        self.orders.append('SELL')
        return {'id':str(len(self.orders)),'status':'closed','filled':qty,'cost':qty*100,'average':100,'fee':{'currency':'USDT','cost':qty*100*.001}}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    folder=Path(tempfile.mkdtemp(prefix='cryptopilot-frozen-orders-',dir=args.work.resolve())).resolve()
    assert folder.parent==args.work.resolve()
    installed,data=folder/'install',folder/'portfolio'
    installed.mkdir();data.mkdir()
    shutil.copytree(args.runtime,installed/'runtime')
    app=installed/'app';app.mkdir()
    for name in ('app.py','trader.py','strategy.py','storage.py','credentials.py','market_data.py','launcher.py','recovery.py','requirements-lock.txt'):
        shutil.copy2(root/name,app/name)
    shutil.copytree(root/'static',app/'static')
    shutil.copytree(root/'maintenance',installed/'maintenance',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (app/'simulated_exchange.py').write_text(FAKE,encoding='utf8')
    launcher=(app/'launcher.py').read_text(encoding='utf8')
    injected=('            from simulated_exchange import FakeExchange\n'
              '            import trader, credentials\n'
              '            trader.TradingEngine._new_exchange = staticmethod(lambda *a: FakeExchange.shared())\n'
              '            credentials.load = lambda _: None\n')
    assert '            import uvicorn\n' in launcher
    (app/'launcher.py').write_text(launcher.replace('            import uvicorn\n',injected+'            import uvicorn\n'),encoding='utf8')
    with (app/'app.py').open('a',encoding='utf8') as handle:
        handle.write("\n@app.get('/api/simulation-proof')\ndef simulation_proof():\n    from simulated_exchange import FakeExchange\n    return {'fake_orders':FakeExchange.shared().orders}\n")
    env=dict(os.environ,CRYPTOPILOT_DATA_DIR=str(data),CRYPTOPILOT_NO_BROWSER='1',CRYPTOPILOT_PORT='18768',PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring')
    exe=installed/'runtime/CryptoPilotRuntime.exe'
    check=subprocess.run([str(exe),'--check-runtime'],env=env,capture_output=True,text=True,check=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    assert json.loads(check.stdout)['valid']
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base='http://127.0.0.1:18768'
    token=None
    def api(path,body=None):
        headers={'Origin':base}
        if token: headers['x-cryptopilot-token']=token
        request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
            headers={**headers,'Content-Type':'application/json'},method='POST' if body is not None else 'GET')
        with opener.open(request,timeout=30) as response:
            raw=response.read()
            return json.loads(raw) if path!='/' else raw.decode()
    with (folder/'runtime.log').open('w') as log:
        child=subprocess.Popen([str(exe)],cwd=folder,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(150):
                if child.poll() is not None: raise RuntimeError((folder/'runtime.log').read_text(errors='replace'))
                try:
                    page=api('/')
                    break
                except OSError: time.sleep(.1)
            else: raise RuntimeError('Startup timeout')
            import re
            token=json.loads(re.search(r'const CSRF=("[^"\n]+")',page).group(1))
            assert api('/api/state')['mode']=='paper'
            api('/api/config',{'settings':{'time_horizon':'rapid','scan_seconds':10,'protective_poll_seconds':2}})
            for mode in ('paper','live'):
                if mode=='live':
                    api('/api/connect',{'api_key':'simulation-only-key','api_secret':'simulation-only-secret','remember':False})
                    api('/api/mode',{'mode':'live','confirmation':'ENABLE LIVE TRADING','eligible':True})
                for _ in range(2):
                    request_id=str(uuid4())
                    context=api('/api/state')['manual_order_context']
                    payload={'symbol':'BTC/USDT','side':'buy','quote_amount':100,'request_id':request_id,
                             'confirmation':'PLACE LIVE BUY' if mode=='live' else '', 'expected_context':context}
                    result=api('/api/order',payload)
                    assert result['status']=='filled'
                    assert api('/api/order',payload)==result  # Replay is not another order.
                    result=api('/api/order',{'symbol':'BTC/USDT','side':'sell','request_id':str(uuid4()),
                                           'confirmation':'PLACE LIVE SELL' if mode=='live' else '', 'expected_context':context})
                    assert result['status']=='filled'
                state=api('/api/state')
                assert not state['positions'] and len(state['trades'])==4 and not state['live_halted']
            assert api('/api/simulation-proof')['fake_orders']==['BUY','SELL','BUY','SELL']
            api('/api/mode',{'mode':'paper'})
            print(json.dumps({'passed':True,'folder':str(folder),'paper_round_trips':2,'simulated_live_round_trips':2,
                              'duplicate_requests_replayed':True,'native_TLS_checks':True,'real_orders':0,'real_credentials':False},indent=2))
        finally:
            child.terminate();child.wait(timeout=15)


if __name__=='__main__': main()
