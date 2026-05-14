from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX

TOKEN = "t.Y93bvBr7wgPBERflnh9wa95HhZ61Zkoj-Qw3B84R605Aa4_LVNITUWFgYUn5MG8CdrSQa_AEUmm66sUaeoGUIw"  # токен песочницы

with Client(TOKEN, target=INVEST_GRPC_API_SANDBOX) as client:
    resp = client.users.get_accounts()
    account_id = resp.accounts[0].id
    print(account_id)

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.schemas import MoneyValue

ACCOUNT_ID = account_id

with Client(TOKEN, target=INVEST_GRPC_API_SANDBOX) as client:
    client.sandbox.sandbox_pay_in(
        account_id=ACCOUNT_ID,
        amount=MoneyValue(currency="rub", units=100_000, nano=0),
    )
