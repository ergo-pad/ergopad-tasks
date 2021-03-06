import psycopg
import requests 

from discord import Webhook, RequestsWebhookAdapter
from urllib.parse import quote # urlencode
from celery import Celery
from os import getenv
from time import sleep, time
from ergodex import getErgodexToken
from coinex import putLatestOHLCV, cleanupHistory
from validator import height

# http://www.ines-panker.com/2020/10/29/retry-celery-tasks.html
# http://www.ines-panker.com/2020/10/28/celery-explained.html

# ADMIN_EMAIL = '' # TODO: move this to config
API_URL = getenv('API_URL')
POSTGRES_CONN = getenv('POSTGRES_CONN')
ERGOPAD_DISCORD_WEBHOOK = getenv('ERGOPAD_DISCORD_WEBHOOK')
ERGOPAD_API_URL = getenv('ERGOPAD_API_URL')
ergo_watch_api: str = f'https://ergo.watch/api/sigmausd/state'
nerg2erg = 10**9
headers = {'Content-Type': 'application/json'}
stakingBody = {'apiKey': getenv('ERGOPAD_APIKEY'), 'numBoxes': 50}

DEBUG = True

import celeryconfig
celery = Celery(__name__)
celery.config_from_object(celeryconfig)

#region LOGGING
import logging
levelname = (logging.WARN, logging.DEBUG)[DEBUG]
logging.basicConfig(format='{asctime}:{name:>8s}:{levelname:<8s}::{message}', style='{', level=levelname)

import inspect
myself = lambda: inspect.stack()[1][3]
#endregion LOGGING

class TaskFailure(Exception):
   pass

#region ROUTING
@celery.task(name="create_task")
def create_task(task_type):
    from datetime import datetime
    # sleep(int(task_type) * 10)
    now = datetime.now().isoformat()
    alertAdmin('current utc', str(now))

    return {'current time': now}

@celery.task(name='redeem_ergopad', bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def redeem_ergopad(self):
    try:
        res = requests.get('https://ergopad.io/api/vesting/redeem/Y2JDKcXN5zrz3NxpJqhGcJzgPRqQcmMhLqsX3TkkqMxQKK86Sh3hAZUuUweRZ97SLuCYLiB2duoEpYY2Zim3j5aJrDQcsvwyLG2ixLLzgMaWfBhTqxSbv1VgQQkVMKrA4Cx6AiyWJdeXSJA6UMmkGcxNCANbCw7dmrDS6KbnraTAJh6Qj6s9r56pWMeTXKWFxDQSnmB4oZ1o1y6eqyPgamRsoNuEjFBJtkTWKqYoF8FsvquvbzssZMpF6FhA1fkiH3n8oKpxARWRLjx2QwsL6W5hyydZ8VFK3SqYswFvRnCme5Ywi4GvhHeeukW4w1mhVx6sbAaJihWLHvsybRXLWToUXcqXfqYAGyVRJzD1rCeNa8kUb7KHRbzgynHCZR68Khi3G7urSunB9RPTp1EduL264YV5pmRLtoNnH9mf2hAkkmqwydi9LoULxrwsRvp', verify=False)
        if res.ok:
            return res.json()
        else:
            raise TaskFailure(f'redeem_ergopad: {res.text}')
            # return {'status': 'failed', 'message': res.text}

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)

def alertAdmin(subject, body):
    return {} 
    
    try:
        webhook = Webhook.from_url(ERGOPAD_DISCORD_WEBHOOK, adapter=RequestsWebhookAdapter())       
        webhook.send(content=f':bangbang:CELERY:bangbang:\nsubject: `{subject}`\nbody: `{body}`')

    except Exception as e:
        logging.error(f'ERR:{myself()}: cannot display discord msg ({e})')
        
    return {'status': 'emailed', 'message': 'failed to send email'}

@celery.task(name='validate_height', bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
async def validate_height(self):
    try:
        await height()

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        alertAdmin(f'FAIL: {myself()}', f'staking.snapshot\nerr: {e}')
        self.retry(exc=e)

# celery call "snapshot_staking"
@celery.task(name='snapshot_staking', bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def snapshot_staking(self):
    try:
        alertAdmin('snapshot begin', f'http://flower:5555/task/{self.request.id}')
        urlAuth = f'{ERGOPAD_API_URL}/auth/token'
        urlSnapshot = f'{ERGOPAD_API_URL}/staking/snapshot'
        username = getenv('SNAPSHOT_USERNAME')
        password = getenv('SNAPSHOT_PASSWORD')
        headers = {'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
        data = f"""grant_type=&username={quote(username)}&password={quote(password)}&scope=&client_id=&client_secret="""
        
        # auth user
        res = requests.post(urlAuth, headers=headers, data=data)
        logging.debug(res.text)
        try:
            bearerToken = res.json()['access_token']
            logging.debug(bearerToken)
        except:
            alertAdmin('snapshot error', res.text)
            raise TaskFailure(f'snapshot_staking: {res.text}')

        # call snapshot
        try: 
            res = requests.get(urlSnapshot, headers=dict(headers, **{'Authorization': f'Bearer {bearerToken}'}))
            logging.debug(res.text)
            alertAdmin('snapshot success', f"found {len(res.json()['stakers'])} stakers")
        except: 
            pass
        
        return res.json()

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        alertAdmin(f'FAIL: {myself()}', f'staking.snapshot\nerr: {e}')
        self.retry(exc=e)

# proactive notify on err
# req auth endpoint
# !! call top of hour; follow with compound call 15 mins later
@celery.task(name='emit_staking', bind=True, default_retry_delay=180, max_retries=20)
def emit_staking(self):
    try:
        res = requests.post(f'{API_URL}/staking/emit', headers=headers, json=stakingBody, verify=False)        
        if res.ok:
            return res.json()
        elif 'Too early for a new emission' in res.text:
            return {'status': 'completed', 'message': 'too early'}
        else:
           raise TaskFailure(f'{res.text}')

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        alertAdmin(f'FAIL: {myself()}', f'staking.emit\nerr: {e}')
        self.retry(exc=e)

@celery.task(name='compound_staking', bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def compound_staking(self):
    try:      
        remainingBoxes = 1
        i = 0
        while remainingBoxes > 0 and (i < 5):
            try:
                res = requests.post(f'{API_URL}/staking/compound', headers=headers, json=stakingBody, verify=False)
                if res.ok:
                    if res.json():
                        try:
                            remainingBoxes = int(res.json()['remainingBoxes'])
                            compoundTx = res.json()['compoundTx']
                            logging.debug(f'compoundTx: {compoundTx}')
                        except:
                            return {'res': res.text}
                elif 'Too early for a new emission' in res.text:
                    return {'status': 'complete', 'message': 'too early'}
                else:
                    alertAdmin(f'FAIL: {myself()}', f'staking.compound\nremainingStakers > 0 after 5 attempts ({API_URL}/staking/compound)')
                    raise TaskFailure(res.text)
                i += 1
            except:
                logging.error(f'{myself()}: error calling /staking/compound from tasks, stopping\n{e}')
                i = 5

        try: msg = res.json() 
        except: msg = res.text
        return {'status': 'complete', 'msg': msg}

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        alertAdmin(f'FAIL: {myself()}', f'staking.compound\nerr: {e}')
        self.retry(exc=e)

@celery.task(bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def hello(self, word: str) -> str:
    try:
        return {"Hello": word}
    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)

@celery.task(name='scrape_price_data', acks_late=True, bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def scrape_price_data(self):
    try:
        res = requests.get(ergo_watch_api).json()
        if res:
            sigUsdPrice = 1/(res['peg_rate_nano']/nerg2erg)
            circ_sigusd_cents = res['circ_sigusd']/100.0  # given in cents
            peg_rate_nano = res['peg_rate_nano']  # also SigUSD
            reserves = res['reserves']  # total amt in reserves (nanoerg)
            # lower of reserves or SigUSD*SigUSD_in_circulation
            liabilities = min(circ_sigusd_cents * peg_rate_nano, reserves)
            equity = reserves - liabilities  # find equity, at least 0
            if equity < 0:
                equity = 0
            if res['circ_sigrsv'] <= 1:
                sigRsvPrice = 0.0
            else:
                sigRsvPrice = equity/res['circ_sigrsv']/nerg2erg  # SigRSV
            with psycopg.connect(POSTGRES_CONN) as con:
                cur = con.cursor()
                sql = f"""
                    insert into "ergowatch_ERG/sigUSD/sigRSV_continuous_5m" (timestamp_utc, "sigUSD", "sigRSV") 
                    values (timestamp 'epoch'+{time()}*INTERVAL '1 second', {sigUsdPrice}, {sigRsvPrice})
                    returning timestamp_utc
                """
                ins = cur.execute(sql).fetchone()[0]
            return {'status': 'success', 'sql': sql, 'message (timestamp_utc)': ins}
        else:
            return {'status': 'failed', 'message': res.text}

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)

@celery.task(name='scrape_price_ergodex', acks_late=True, bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def scrape_price_ergodex(self):
    try:
        res = getErgodexToken()
        if res:
            with psycopg.connect(POSTGRES_CONN) as con:
                cur = con.cursor()
                sql = f"""
                    insert into "ergodex_ERG/ergodexToken_continuous_5m" (timestamp_utc, sigusd, sigrsv, erdoge, lunadog, ergopad, neta) 
                    values (timestamp 'epoch'+{time()}*INTERVAL '1 second', {res['sigusd'][0]}, {res['sigrsv'][0]}, {res['erdoge'][0]}, {res['lunadog'][0]}, {res['ergopad'][0]}, {res['neta'][0]})
                    returning timestamp_utc
                """
                ins = cur.execute(sql).fetchone()[0]
            return {'status': 'success', 'sql': sql, 'message (timestamp_utc)': ins}
        else:
            return {'status': 'failed', 'message': res.text}
            
    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)

@celery.task(name='cleanup_continuous_5m', acks_late=True, bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def cleanup_continuous_5m(self):
    try:
        with psycopg.connect(POSTGRES_CONN) as con:
            cur = con.cursor()
            sql1 = f"""delete from "ergodex_ERG/ergodexToken_continuous_5m" where timestamp_utc < CURRENT_DATE - INTERVAL '5 years' returning timestamp_utc"""
            sql2 = f"""delete from "ergowatch_ERG/sigUSD/sigRSV_continuous_5m" where timestamp_utc < CURRENT_DATE - INTERVAL '5 years' returning timestamp_utc"""
            dlt1 = cur.execute(sql1).fetchone()[0]
            dlt2 = cur.execute(sql2).fetchone()[0]

            return {'status': 'success', 'sql': f'{sql1}\n{sql2}', 'message (timestamp_utc)': f'{dlt1}\n{dlt2}'}
            
    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)

@celery.task(name='coinex_scrape_all', acks_late=True, bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def coinex_scrape_all(self):
    try:
        res = putLatestOHLCV()
        return res

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)

@celery.task(name='coinex_cleanup', acks_late=True, bind=True, default_retry_delay=300, max_retries=2, retry_backoff=True)
def coinex_cleanup_all(self):
    try:
        res = cleanupHistory()
        if res:
            return True
        else:
            return False

    except Exception as e:
        logging.error(f'{myself()}: {e}')
        self.retry(exc=e)
#endregion ROUTING
