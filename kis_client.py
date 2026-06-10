import requests
import json
import time
from datetime import datetime, timezone, timedelta
import config

class KISClient:
    def __init__(self):
        self.base_url = config.KIS_BASE_URL
        self.app_key = config.KIS_APP_KEY
        self.app_secret = config.KIS_APP_SECRET
        self.account_no = config.KIS_ACCOUNT_NUMBER
        self.account_product = config.KIS_ACCOUNT_PRODUCT_CODE
        self.access_token = None
        self.token_expired_at = None

    def _get_token(self):
        """접근토큰 발급 또는 재사용"""
        if self.access_token and self.token_expired_at and datetime.now() < self.token_expired_at:
            return self.access_token

        url = f"{self.base_url}/oauth2/TokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
        data = resp.json()
        if "access_token" not in data:
            raise Exception(f"토큰 발급 실패: {data}")
        self.access_token = data["access_token"]
        # 유효기간은 서버 응답의 expires_in(초)를 사용, 바로 재발급 하지 않도록 10분 여유
        expires_in = data.get("expires_in", 86400)
        self.token_expired_at = datetime.now() + timedelta(seconds=expires_in - 600)
        return self.access_token

    def _headers(self, tr_id=None):
        h = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        if tr_id:
            h["tr_id"] = tr_id
        return h

    def get_current_price(self, stock_code: str):
        """현재가 조회 (FHKST01010100)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
        }
        headers = self._headers(tr_id="FHKST01010100")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"시세 조회 실패 [{stock_code}]: {data}")
        output = data.get("output", {})
        return {
            "stock_code": stock_code,
            "stock_name": output.get("hts_kor_isnm", ""),
            "current_price": int(output.get("stck_prpr", 0)),
            "open_price": int(output.get("stck_oprc", 0)),
            "high_price": int(output.get("stck_hgpr", 0)),
            "low_price": int(output.get("stck_lwpr", 0)),
            "prev_close": int(output.get("stck_prdy_clpr", 0)),
            "change_rate": float(output.get("prdy_ctrt", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "trading_value": int(output.get("acml_tr_pbmn", 0)),
        }

    def get_minute_candles(self, stock_code: str, period="5"):
        """5분봉 조회 (FHKST03010200) - 최대 30개"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_HOUR_1": period,
            "FID_PW_DATA_INCU_YN": "N",
        }
        headers = self._headers(tr_id="FHKST03010200")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"분봉 조회 실패 [{stock_code}]: {data}")
        return data.get("output2", [])

    def get_daily_candles(self, stock_code: str, count=60):
        """일봉 조회 (FHKST01010400) - count 만큼"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",
        }
        headers = self._headers(tr_id="FHKST01010400")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"일봉 조회 실패 [{stock_code}]: {data}")
        return data.get("output2", [])[:count]

    def get_balance(self):
        """보유 잔고 조회 (VTTC8434R)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.account_no.split("-")[0],
            "ACNT_PRDT_CD": self.account_no.split("-")[1] if "-" in self.account_no else self.account_product,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "01",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCC_ASTM_TCD": "",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        headers = self._headers(tr_id="VTTC8434R")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"잔고 조회 실패: {data}")
        # 보유 종목
        holdings = []
        for item in data.get("output1", []):
            if item.get("pdno") and item.get("pdno") != "":
                holdings.append({
                    "stock_code": item["pdno"],
                    "stock_name": item.get("prdt_name", ""),
                    "quantity": int(item.get("hldg_qty", 0)),
                    "avg_price": int(item.get("pchs_avg_pric", 0)),
                    "current_price": int(item.get("prpr", 0)),
                    "eval_amount": int(item.get("evlu_amt", 0)),
                    "profit_loss_rate": float(item.get("evlu_pfls_rt", 0)),
                })
        # 예수금
        cash = 0
        for item in data.get("output2", []):
            cash = int(item.get("dnca_tot_amt", 0))
        return {"cash": cash, "holdings": holdings}

    def order_buy(self, stock_code: str, quantity: int):
        """시장가 매수 (VTTC0802U)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO": self.account_no.split("-")[0],
            "ACNT_PRDT_CD": self.account_no.split("-")[1] if "-" in self.account_no else self.account_product,
            "PDNO": stock_code,
            "ORD_DVSN": "01",  # 시장가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",   # 시장가는 0
        }
        headers = self._headers(tr_id="VTTC0802U")
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
        data = resp.json()
        return data

    def order_sell(self, stock_code: str, quantity: int):
        """시장가 매도 (VTTC0801U)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO": self.account_no.split("-")[0],
            "ACNT_PRDT_CD": self.account_no.split("-")[1] if "-" in self.account_no else self.account_product,
            "PDNO": stock_code,
            "ORD_DVSN": "01",  # 시장가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
        }
        headers = self._headers(tr_id="VTTC0801U")
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
        data = resp.json()
        return data

    def get_us_price(self, stock_code: str, exchange: str = "NAS"):
        """해외 주식 현재가 조회 (HHDFS00000300)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/quotations/price"
        params = {
            "FID_COND_MRKT_DIV_CODE": exchange,
            "FID_INPUT_ISCD": stock_code,
        }
        headers = self._headers(tr_id="HHDFS00000300")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"해외 시세 조회 실패 [{stock_code}]: {data}")
        output = data.get("output", {})
        return {
            "stock_code": stock_code,
            "stock_name": output.get("hts_kor_isnm", ""),
            "current_price": float(output.get("last", 0)),
            "open_price": float(output.get("open", 0)),
            "high_price": float(output.get("high", 0)),
            "low_price": float(output.get("low", 0)),
            "prev_close": float(output.get("base", 0)),
            "change_rate": float(output.get("rate", 0)),
            "volume": int(output.get("tvol", 0)),
            "trading_value": int(output.get("tamt", 0)),
        }

    def get_us_daily(self, stock_code: str, exchange: str = "NAS", count=60):
        """해외 주식 일봉 조회 (HHDFS76240000)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/quotations/dailyprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": exchange,
            "FID_INPUT_ISCD": stock_code,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",
        }
        headers = self._headers(tr_id="HHDFS76240000")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"해외 일봉 조회 실패 [{stock_code}]: {data}")
        return data.get("output2", [])[:count]

    def get_us_balance(self, exchange: str = "NAS"):
        """해외주식 잔고 조회 (VTTS3012R)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        cano = self.account_no.split("-")[0]
        prdt = self.account_no.split("-")[1] if "-" in self.account_no else self.account_product
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "OVRS_EXCG_CD": exchange,
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        headers = self._headers(tr_id="VTTS3012R")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"해외 잔고 조회 실패: {data}")
        holdings = []
        for item in data.get("output1", []):
            if item.get("pdno"):
                holdings.append({
                    "stock_code": item["pdno"],
                    "stock_name": item.get("prdt_name", ""),
                    "quantity": int(item.get("ovrs_cblc_qty", 0)),
                    "avg_price": float(item.get("pchs_avg_pric", 0)),
                    "current_price": float(item.get("prpr", 0)),
                    "profit_loss_rate": float(item.get("evlu_pfls_rt", 0)),
                })
        cash = float(data.get("output2", [{}])[0].get("frcr_dncl_amt", 0))
        return {"cash": cash, "holdings": holdings}

    def order_us_buy(self, stock_code: str, quantity: int, exchange: str = "NAS"):
        """해외 시장가 매수 (VTTT1002U)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        cano = self.account_no.split("-")[0]
        prdt = self.account_no.split("-")[1] if "-" in self.account_no else self.account_product
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "OVRS_EXCG_CD": exchange,
            "PDNO": stock_code,
            "ORD_DVSN": "00",
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",
            "TR_CRCY_CD": "USD",
        }
        headers = self._headers(tr_id="VTTT1002U")
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
        return resp.json()

    def order_us_sell(self, stock_code: str, quantity: int, exchange: str = "NAS"):
        """해외 시장가 매도 (VTTT1001U)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        cano = self.account_no.split("-")[0]
        prdt = self.account_no.split("-")[1] if "-" in self.account_no else self.account_product
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "OVRS_EXCG_CD": exchange,
            "PDNO": stock_code,
            "ORD_DVSN": "00",
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",
            "TR_CRCY_CD": "USD",
        }
        headers = self._headers(tr_id="VTTT1001U")
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
        return resp.json()

    def get_us_minute(self, stock_code: str, exchange: str = "NAS", period="5"):
        """해외 주식 5분봉 (HHDFS76910000)"""
        url = f"{self.base_url}/uapi/overseas-stock/v1/quotations/inquire-time-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": exchange,
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_HOUR_1": period,
            "FID_PW_DATA_INCU_YN": "N",
        }
        headers = self._headers(tr_id="HHDFS76910000")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"해외 분봉 조회 실패 [{stock_code}]: {data}")
        return data.get("output2", [])
